# AGENT: ROLE: Orchestration bridge: agent-mode scoping, live command admission, governed payload construction, and shadow-cycle capture.
# AGENT: ENTRYPOINT: imported by `fxstack/runtime/runner.py`; no independent process.
# AGENT: PRIMARY INPUTS: settings agent-mode allowlists, runtime state, decisions, pending entries/position actions.
# AGENT: PRIMARY OUTPUTS: governed command payloads, live-admission diagnostics, orchestration packets and runtime diags.
# AGENT: DEPENDS ON: `fxstack/orchestration/*`, `fxstack/strategy/allocator.py`.
# AGENT: CALLED BY: `fxstack/runtime/runner.py`.
# AGENT: STATE / SIDE EFFECTS: writes orchestration bundles through the caller-supplied service; owns no state itself.
# AGENT: HANDSHAKES: `command.production_egress`, `runtime.live_command_admission`, `research.advisory`.
# AGENT: SEE: `docs/architecture/REFACTOR_PLAN.md` -> `fxstack/runtime/runner.py` -> `docs/agents/runtime-loop.md`
"""Agent-mode scoping, live command admission, and governed payload construction.

Carved out of ``fxstack.runtime.runner``. Cohesive on one axis: everything here
decides *whether a decision is allowed to become a command in the current agent
mode*, and what that command's payload must look like. It spans the shadow,
paper, and live modes uniformly so the three cannot drift apart.

The governed-payload validators are the load-bearing part. They exist to prove
that what reaches the broker is exactly what the risk kernel approved -- not a
re-derived lookalike -- so any change here must preserve that equality.

The runner re-imports each name under its original underscored alias, so
existing call sites and tests that reached into ``runner`` keep working.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
import hashlib
import math
import time
from typing import TYPE_CHECKING, Any
import uuid

from fxstack._lazy import lazy_pandas as pd

from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION
from fxstack.risk.constants import ROLLOUT_EXECUTION_MODES
from fxstack.runtime._util import safe_float as _safe_float
from fxstack.strategy.constants import playbook_to_sleeve

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from fxstack.orchestration.graph_runtime import ShadowGraphRuntime
    from fxstack.runtime.runner import LoadedModelSet


def normalize_agent_mode(raw: Any) -> str:
    mode = str(raw or "off").strip().lower()
    if mode in {"shadow", "paper", "live"}:
        return mode
    return "off"


def safe_authority_revision(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0

def build_command_id(*, pair: str, ts_value: str, action_tag: str) -> str:
    ts_parsed = pd.to_datetime(ts_value, utc=True, errors="coerce")
    if pd.isna(ts_parsed):
        # Keep fallback deterministic across processes and restarts.
        ts_key = hashlib.sha1(str(ts_value).encode("utf-8")).hexdigest()[:16]
    else:
        ts_key = str(int(ts_parsed.timestamp() * 1000.0))
    if str(action_tag).strip().lower() == "entry":
        return f"fxs-{pair.lower()}-{ts_key}"
    return f"fxs-{action_tag}-{pair.lower()}-{ts_key}"


def payload_from_approved_order(*, order: dict[str, Any], pair: str, ts_value: str, action_tag: str) -> dict[str, Any]:
    cmd_id = build_command_id(pair=pair, ts_value=ts_value, action_tag=action_tag)
    payload = dict(order or {})
    payload["command_id"] = str(payload.get("command_id") or cmd_id)
    payload["trace_id"] = str(payload.get("trace_id") or cmd_id)
    payload["symbol"] = str(payload.get("symbol") or pair).upper()
    payload["cmd"] = str(payload.get("cmd") or payload.get("command") or "").upper()
    payload["action"] = str(payload.get("action") or action_tag)
    payload["side"] = str(payload.get("side") or "").upper()
    payload["lots"] = float(_safe_float(payload.get("lots"), 0.0))
    payload["close_lots"] = float(_safe_float(payload.get("close_lots"), 0.0))
    cmd = str(payload.get("cmd") or "").upper()
    if cmd in {"BUY", "SELL"}:
        for price_key in ("tp_price", "sl_price"):
            if price_key not in payload:
                continue
            price_value = _safe_float(payload.get(price_key), 0.0)
            payload[price_key] = float(price_value) if math.isfinite(float(price_value)) and float(price_value) > 0.0 else None
    return payload


OPERATIONAL_HARD_ENTRY_BLOCK_REASONS = {
    "mt4_stale",
    "tick_feed_stale",
    "missing_live_tick",
    "missing_spread_input",
    "governance_paused",
    "governance_entries_only",
    "governance_shadow_only",
    "pair_exposure_cap",
    "portfolio_exposure_cap",
    # A model set with no passing validation certificate may not OPEN new risk.
    #
    # This belongs in the operational set, not the strategy set: it is not an
    # opinion about whether a setup looks good, it is the statement that nothing
    # has demonstrated this model can tell a good setup from a bad one. The
    # adaptive policy is allowed to override strategy opinions; it must not be
    # able to override the absence of a warrant to trade at all.
    "models_uncertified",
    # exploration_demo certification mode permits uncertified entries ONLY on a
    # broker-attested DEMO account. A real or unattested account fails closed,
    # and no strategy opinion may override that fence.
    "exploration_demo_requires_demo_account_attestation",
    # Governance zeroed entry capital (paused/shadow band). Every sizing path
    # multiplies by this scale, so no adaptive selection can size around it.
    "portfolio_budget_scale_zero",
}


def is_operational_hard_entry_block_reason(reason: Any) -> bool:
    """Separate execution/risk invariants from strategy evidence.

    Model probabilities, edge, spread quality, session, regime, uncertainty,
    structure, belief, and overlay opinions are deliberately absent.  They are
    inputs to the intelligent action comparison, not independent vetoes.
    """

    token = str(reason or "").strip().lower()
    if not token:
        return False
    if token in OPERATIONAL_HARD_ENTRY_BLOCK_REASONS:
        return True
    return token.startswith(
        (
            "stale_feature",
            "stale_adaptive",
            "entry_protection_",
            "final_entry_risk_",
            "broker_",
        )
    )




def orchestration_cycle_id(loop_ts: float) -> str:
    return str(int(round(float(loop_ts) * 1000.0)))


#: Process-wide shadow graph runtime. Lazily constructed so importing this
#: module stays free, and shared so every cycle reuses one warm instance.
_ORCHESTRATION_GRAPH_RUNTIME: ShadowGraphRuntime | None = None


def get_orchestration_graph_runtime() -> ShadowGraphRuntime:
    global _ORCHESTRATION_GRAPH_RUNTIME
    if _ORCHESTRATION_GRAPH_RUNTIME is None:
        from fxstack.orchestration.graph_runtime import ShadowGraphRuntime

        _ORCHESTRATION_GRAPH_RUNTIME = ShadowGraphRuntime()
    return _ORCHESTRATION_GRAPH_RUNTIME


def orchestration_percentile(values: list[int], pct: float) -> int:
    ordered = sorted(int(value) for value in list(values or []))
    if not ordered:
        return 0
    if len(ordered) == 1:
        return int(ordered[0])
    rank = max(0.0, min(1.0, float(pct))) * float(len(ordered) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return int(ordered[lower])
    weight = rank - float(lower)
    return int(round(float(ordered[lower]) * (1.0 - weight) + float(ordered[upper]) * weight))


def orchestration_shadow_pair_enabled(*, pair: str, settings: Any) -> bool:
    allowlist = {
        str(item).upper()
        for item in list(getattr(settings, "agent_shadow_pair_allowlist", []) or [])
        if str(item).strip()
    }
    if not allowlist:
        return True
    return str(pair or "").upper() in allowlist


def paper_mode_enabled(settings: Any) -> bool:
    return normalize_agent_mode(getattr(settings, "agent_mode", "off")) == "paper"


def live_mode_enabled(settings: Any) -> bool:
    return normalize_agent_mode(getattr(settings, "agent_mode", "off")) == "live"


def orchestration_paper_pair_enabled(*, pair: str, settings: Any) -> bool:
    allowlist = {
        str(item).upper()
        for item in list(
            getattr(settings, "agent_paper_pair_allowlist", None)
            or getattr(settings, "agent_shadow_pair_allowlist", [])
            or []
        )
        if str(item).strip()
    }
    if not allowlist:
        return True
    return str(pair or "").upper() in allowlist


def orchestration_paper_sleeve_enabled(*, sleeve: str, settings: Any) -> bool:
    allowlist = {
        str(item).strip().lower()
        for item in list(getattr(settings, "agent_paper_sleeve_allowlist", []) or [])
        if str(item).strip()
    }
    if not allowlist:
        return True
    return str(sleeve or "").strip().lower() in allowlist


def orchestration_paper_intent_enabled(*, intent: str, settings: Any) -> bool:
    allowlist = {
        str(item).strip().lower()
        for item in list(getattr(settings, "agent_paper_intent_allowlist", []) or [])
        if str(item).strip()
    }
    if not allowlist:
        return True
    return str(intent or "").strip().lower() in allowlist


def orchestration_live_pair_enabled(*, pair: str, settings: Any) -> bool:
    allowlist = {
        str(item).upper()
        for item in list(getattr(settings, "agent_live_pair_allowlist", []) or [])
        if str(item).strip()
    }
    if not allowlist:
        return False
    return str(pair or "").upper() in allowlist


def orchestration_live_sleeve_enabled(*, sleeve: str, settings: Any) -> bool:
    allowlist = {
        str(item).strip().lower()
        for item in list(getattr(settings, "agent_live_sleeve_allowlist", []) or [])
        if str(item).strip()
    }
    if not allowlist:
        return False
    return str(sleeve or "").strip().lower() in allowlist


def orchestration_live_intent_enabled(*, intent: str, settings: Any) -> bool:
    allowlist = {
        str(item).strip().lower()
        for item in list(getattr(settings, "agent_live_intent_allowlist", []) or [])
        if str(item).strip()
    }
    if not allowlist:
        return False
    return str(intent or "").strip().lower() in allowlist


def live_command_admission_diagnostics(
    *,
    settings: Any,
    model_sets: dict[str, Any],
) -> dict[str, Any]:
    """Prove at startup that live entry authority is actually reachable."""

    if not live_mode_enabled(settings):
        return {
            "required": False,
            "allowed": True,
            "status": "not_applicable",
            "blockers": [],
            "pairs": {},
        }

    configured_pairs = [
        str(item).strip().upper()
        for item in list(getattr(settings, "pairs", []) or [])
        if str(item).strip()
    ]
    pair_scope = {
        str(item).strip().upper()
        for item in list(getattr(settings, "agent_live_pair_allowlist", []) or [])
        if str(item).strip()
    }
    sleeve_scope = {
        str(item).strip().lower()
        for item in list(getattr(settings, "agent_live_sleeve_allowlist", []) or [])
        if str(item).strip()
    }
    intent_scope = {
        str(item).strip().lower()
        for item in list(getattr(settings, "agent_live_intent_allowlist", []) or [])
        if str(item).strip()
    }
    supported_intents = {"enter", "exit", "reduce", "tighten_stop"}
    entry_strategy_family = str(
        getattr(settings, "entry_strategy_family", "model_stack")
        or "model_stack"
    ).strip().lower()
    if entry_strategy_family == "mtvclc":
        # MTVCLC has one broker-native bracket and
        # one full time-stop exit.  It has no partial-reduce or stop-adjust
        # producer, so requiring those scopes would create unused authority.
        required_intents = {"enter", "exit"}
    else:
        required_intents = {"enter"}
        if bool(getattr(settings, "enable_lifecycle_actions", True)):
            required_intents.update({"exit", "reduce"})
        if bool(getattr(settings, "enable_adjust_actions", False)):
            required_intents.add("tighten_stop")

    blockers: list[str] = []
    unknown_intents = sorted(intent_scope - supported_intents)
    missing_intents = sorted(required_intents - intent_scope)
    if unknown_intents:
        blockers.append("unknown_live_intents:" + ",".join(unknown_intents))
    if missing_intents:
        blockers.append("missing_live_intents:" + ",".join(missing_intents))
    if not sleeve_scope:
        blockers.append("live_sleeve_scope_empty")

    pair_diag: dict[str, Any] = {}
    for pair in configured_pairs:
        loaded = model_sets.get(pair)
        rollout = dict(getattr(loaded, "rollout_policy", {}) or {}) if loaded is not None else {}
        pair_blockers: list[str] = []
        if loaded is None:
            pair_blockers.append("model_set_missing")
        if pair not in pair_scope:
            pair_blockers.append("pair_not_in_live_scope")
        if not bool(rollout.get("configured", False)):
            pair_blockers.append("rollout_not_configured")
        if str(rollout.get("source") or "").strip() not in {
            "main_runtime_rollout",
            "phase5_runtime_rollout",
            "runtime_rollout",
            "release_authority",
            "production_operator_scope",
        } or str(rollout.get("budget_reason") or "").strip() == "phase5_gate_default":
            pair_blockers.append("rollout_not_explicit")
        if str(rollout.get("mode") or "").strip().lower() not in ROLLOUT_EXECUTION_MODES:
            pair_blockers.append("rollout_mode_invalid")
        if not bool(rollout.get("active", False)):
            pair_blockers.append("rollout_inactive")
        if not bool(rollout.get("pair_allowlisted", False)):
            pair_blockers.append("rollout_pair_blocked")
        if float(_safe_float(rollout.get("budget_scale"), 0.0)) <= 0.0:
            pair_blockers.append("rollout_budget_zero")
        pair_diag[pair] = {
            "allowed": not pair_blockers,
            "blockers": list(pair_blockers),
            "model_set_id": str(getattr(loaded, "model_set_id", "") or ""),
            "rollout": dict(rollout),
        }
        blockers.extend(f"{pair}:{reason}" for reason in pair_blockers)

    return {
        "required": True,
        "allowed": not blockers,
        "status": "ready" if not blockers else "blocked",
        "blockers": list(blockers),
        "pair_scope": sorted(pair_scope),
        "sleeve_scope": sorted(sleeve_scope),
        "intent_scope": sorted(intent_scope),
        "required_intents": sorted(required_intents),
        "pairs": pair_diag,
    }


def orchestration_live_runtime_state(state: dict[str, Any] | None) -> dict[str, Any]:
    runtime_diag = dict(dict(state or {}).get("runtime_diag") or {})
    live = dict(runtime_diag.get("orchestration_live") or {})
    return {
        "authority_revision": safe_authority_revision(
            live.get("authority_revision")
        ),
        "enabled": bool(live.get("enabled", False)),
        "mode": str(live.get("mode") or live.get("agent_mode") or "").strip().lower(),
        "runtime_enabled": bool(live.get("runtime_enabled", True)),
        "queue_kill_active": bool(live.get("queue_kill_active", False)),
        "queue_kill_reason": str(live.get("queue_kill_reason") or ""),
        "queue_killed_at": live.get("queue_killed_at"),
        "active_pair_scope_configured": bool(
            live.get(
                "active_pair_scope_configured",
                "active_pair_scope" in live,
            )
        ),
        "active_pair_scope": [
            str(item).strip().upper()
            for item in list(live.get("active_pair_scope") or [])
            if str(item).strip()
        ],
        "active_sleeve_scope_configured": bool(
            live.get(
                "active_sleeve_scope_configured",
                "active_sleeve_scope" in live,
            )
        ),
        "active_sleeve_scope": [
            str(item).strip().lower()
            for item in list(live.get("active_sleeve_scope") or [])
            if str(item).strip()
        ],
        "active_intent_scope_configured": bool(
            live.get(
                "active_intent_scope_configured",
                "active_intent_scope" in live,
            )
        ),
        "active_intent_scope": [
            str(item).strip().lower()
            for item in list(live.get("active_intent_scope") or [])
            if str(item).strip()
        ],
    }


def paper_mode_rollback(*, svc: Any, reason: str) -> int:
    purge = getattr(svc, "purge_pending_commands", None)
    if not callable(purge):
        return 0
    return int(purge(reason=str(reason or "paper_mode_rollback")))


def update_orchestration_shadow_command_flow(
    *,
    meta: dict[str, Any],
    orchestration: dict[str, Any],
    enqueue_out: dict[str, Any],
) -> dict[str, Any]:
    out = dict(meta or {})
    shadow = dict(out.get("orchestration_shadow") or {})
    governed = dict(shadow.get("governed_decision") or {})
    governed.setdefault("selected_action", str(orchestration.get("governed_selected_action") or ""))
    governed.setdefault("allowed", bool(orchestration.get("governed_allowed", False)))
    governed.setdefault("approval_state", str(orchestration.get("approval_state") or "auto"))
    governed["command_id"] = str(enqueue_out.get("command_id") or "")
    governed["command_status"] = str(enqueue_out.get("status") or "")
    shadow["governed_decision"] = governed
    shadow["command_flow"] = {
        "command_id": str(enqueue_out.get("command_id") or ""),
        "status": str(enqueue_out.get("status") or ""),
        "execution_provider": str(enqueue_out.get("execution_provider") or ""),
        "line_present": bool(str(enqueue_out.get("line") or "").strip()),
        "paper_execution": dict(enqueue_out.get("paper_execution") or {}),
        "command_source": str(enqueue_out.get("command_source") or ""),
        "baseline_fallback": bool(enqueue_out.get("baseline_fallback", False)),
        "fallback_reason": str(enqueue_out.get("fallback_reason") or ""),
    }
    out["orchestration_shadow"] = shadow
    return out


def paper_command_preview_payload(
    *,
    preview: dict[str, Any],
    pair: str,
    ts_value: str,
    action_tag: str,
) -> dict[str, Any]:
    preview_map = dict(preview or {})
    approved_order = dict(preview_map.get("approved_order") or {})
    source = approved_order or preview_map
    if not source:
        return {}
    return payload_from_approved_order(
        order=source,
        pair=str(pair).upper(),
        ts_value=str(ts_value),
        action_tag=str(action_tag),
    )


def reconcile_governed_payload(
    *,
    payload: dict[str, Any],
    decision: dict[str, Any],
    pair: str,
    selected_action: str,
    mode_name: str,
) -> tuple[dict[str, Any], str]:
    out = dict(payload or {})
    if not out:
        return {}, ""
    expected_pair = str(pair or "").strip().upper()
    payload_symbol = str(out.get("symbol") or expected_pair).strip().upper()
    if expected_pair and payload_symbol != expected_pair:
        return {}, f"{mode_name}_preview_symbol_mismatch"
    out["symbol"] = str(expected_pair or payload_symbol)
    if str(selected_action).strip().lower() == "enter":
        meta = dict(decision.get("metadata", {}) or {})
        expected_side = str(decision.get("side") or meta.get("side") or "").strip().upper()
        payload_side = str(out.get("side") or out.get("cmd") or "").strip().upper()
        if expected_side not in {"BUY", "SELL"}:
            if payload_side not in {"BUY", "SELL"}:
                return {}, f"{mode_name}_preview_side_missing"
            expected_side = str(payload_side)
        if payload_side in {"BUY", "SELL"} and payload_side != expected_side:
            return {}, f"{mode_name}_preview_side_mismatch"
        out["side"] = str(expected_side)
        out["cmd"] = str(expected_side)
    return out, ""


def validate_final_entry_payload_against_risk_approval(
    *,
    payload: dict[str, Any],
    risk_approved_payload: dict[str, Any],
) -> str:
    """Reject any post-risk entry mutation except a finite lot reduction."""
    final_payload = dict(payload or {})
    approved_payload = dict(risk_approved_payload or {})
    if not approved_payload:
        return "risk_kernel_missing_order"
    approved_lots = float(_safe_float(approved_payload.get("lots"), float("nan")))
    final_lots = float(_safe_float(final_payload.get("lots"), float("nan")))
    if not math.isfinite(approved_lots) or approved_lots <= 0.0:
        return "risk_approved_lots_invalid"
    if not math.isfinite(final_lots) or final_lots <= 0.0:
        return "post_risk_lots_invalid"
    if final_lots > approved_lots + 1e-9:
        return "post_risk_lots_exceed_approval"
    if set(final_payload) - set(approved_payload):
        return "post_risk_payload_mutation"
    for key, approved_value in approved_payload.items():
        if key == "lots":
            continue
        if final_payload.get(key) != approved_value:
            return "post_risk_payload_mutation"
    return ""


def governed_action_for_risk_approved_payload(payload: dict[str, Any]) -> str:
    command = str(dict(payload or {}).get("cmd") or "").strip().upper()
    return {
        "BUY": "enter",
        "SELL": "enter",
        "CLOSE": "exit",
        "CLOSE_PARTIAL": "reduce",
        "MODIFY_SL": "tighten_stop",
    }.get(command, "")


def validate_final_lifecycle_payload_against_risk_approval(
    *,
    lifecycle_action: str,
    action_item: dict[str, Any],
    approved_order: dict[str, Any],
) -> str:
    action = str(lifecycle_action or "").strip().lower()
    approved = dict(approved_order or {})
    approved_action = {
        "exit": "exit",
        "reduce": "partial_tp",
        "tighten_stop": "tighten_stop",
    }.get(governed_action_for_risk_approved_payload(approved), "")
    if action != approved_action:
        return "final_lifecycle_risk_payload_mismatch"
    if action == "partial_tp":
        requested = float(_safe_float(action_item.get("close_lots"), float("nan")))
        risk_approved = float(_safe_float(approved.get("close_lots", approved.get("lots")), float("nan")))
        if (
            not math.isfinite(requested)
            or not math.isfinite(risk_approved)
            or requested <= 0.0
            or abs(requested - risk_approved) > 1e-9
        ):
            return "final_lifecycle_risk_payload_mismatch"
    if action == "tighten_stop":
        requested = float(_safe_float(action_item.get("sl_price"), float("nan")))
        risk_approved = float(_safe_float(approved.get("sl_price"), float("nan")))
        if (
            not math.isfinite(requested)
            or not math.isfinite(risk_approved)
            or requested <= 0.0
            or abs(requested - risk_approved) > 1e-12
        ):
            return "final_lifecycle_risk_payload_mismatch"
    return ""


def governed_command_payload_for_mode(
    *,
    decision: dict[str, Any],
    orchestration: dict[str, Any],
    pair: str,
    ts_value: str,
    default_payload: dict[str, Any],
    default_action_tag: str,
    settings: Any,
    mode: str,
    runtime_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    meta = dict(decision.get("metadata", {}) or {})
    orch = dict(orchestration or {})
    governed = dict(orch.get("governed_decision") or {})
    mode_name = str(mode or "paper").strip().lower() or "paper"
    selected_action = str(
        governed.get("selected_action")
        or orch.get("governed_selected_action")
        or orch.get("shadow_action")
        or ""
    ).strip().lower()
    approval_state = str(governed.get("approval_state") or orch.get("approval_state") or "auto").strip().lower()
    blocking_reasons = [
        str(item)
        for item in list(governed.get("blocking_reasons") or orch.get("blocking_reasons") or [])
        if str(item).strip()
    ]
    governed_preview = dict(governed.get("command_preview") or orch.get("command_preview") or {})
    if not bool(orch.get("enabled", False)):
        return {}, f"{mode_name}_orchestration_missing"
    if not str(orch.get("correlation_id") or "").strip() or not str(orch.get("thread_id") or "").strip():
        return {}, f"{mode_name}_missing_correlation"
    if approval_state not in {"auto", "approved"}:
        return {}, f"{mode_name}_approval_required"
    # A governor veto is authoritative regardless of which safe action the
    # committee selected.  In particular, ``allowed=False`` commonly arrives
    # with ``hold``/``no_trade``; treating those actions as a baseline fallback
    # would silently resurrect the vetoed order.
    if not bool(governed.get("allowed", True)):
        return {}, str(blocking_reasons[0] if blocking_reasons else f"{mode_name}_governor_blocked")
    sleeve = str(
        meta.get("adaptive_sleeve")
        or playbook_to_sleeve(meta.get("adaptive_playbook") or "")
        or ""
    ).strip().lower()
    if mode_name == "paper":
        if not orchestration_paper_pair_enabled(pair=pair, settings=settings):
            return {}, "paper_pair_not_allowlisted"
        if sleeve and not orchestration_paper_sleeve_enabled(sleeve=sleeve, settings=settings):
            return {}, "paper_sleeve_not_allowlisted"
        if selected_action and not orchestration_paper_intent_enabled(intent=selected_action, settings=settings):
            return {}, "paper_intent_not_allowlisted"
        # Paper callers predating the committee contract may carry only the
        # correlation envelope.  Preserve their already risk-approved command;
        # live mode never takes this compatibility path.
        if not governed and not selected_action:
            payload = dict(default_payload or {})
            return payload, ("paper_missing_command_preview" if not payload else "")
    elif mode_name == "live":
        live_runtime = orchestration_live_runtime_state(runtime_state)
        persisted_live_mode = str(live_runtime.get("mode") or "").strip().lower()
        persisted_scopes_are_authoritative = persisted_live_mode == "live"
        live_pair_scope = (
            set(list(live_runtime.get("active_pair_scope") or []))
            if bool(live_runtime.get("active_pair_scope_configured", False))
            and persisted_scopes_are_authoritative
            else {
                str(item).strip().upper()
                for item in list(getattr(settings, "agent_live_pair_allowlist", []) or [])
                if str(item).strip()
            }
        )
        live_sleeve_scope = (
            set(list(live_runtime.get("active_sleeve_scope") or []))
            if bool(live_runtime.get("active_sleeve_scope_configured", False))
            and persisted_scopes_are_authoritative
            else {
                str(item).strip().lower()
                for item in list(getattr(settings, "agent_live_sleeve_allowlist", []) or [])
                if str(item).strip()
            }
        )
        live_intent_scope = (
            set(list(live_runtime.get("active_intent_scope") or []))
            if bool(live_runtime.get("active_intent_scope_configured", False))
            and persisted_scopes_are_authoritative
            else {
                str(item).strip().lower()
                for item in list(getattr(settings, "agent_live_intent_allowlist", []) or [])
                if str(item).strip()
            }
        )
        if not bool(live_runtime.get("runtime_enabled", True)):
            return {}, "live_runtime_killed"
        if (
            not bool(live_runtime.get("enabled", False))
            or persisted_live_mode != "live"
        ):
            return {}, "live_mode_disabled"
        if bool(live_runtime.get("queue_kill_active", False)):
            return {}, "live_queue_killed"
        if safe_authority_revision(live_runtime.get("authority_revision")) <= 0:
            return {}, "live_authority_revision_unattested"
        if selected_action == "enter":
            broker_account_mode = str(
                dict(runtime_state or {}).get("broker_account_mode") or ""
            ).strip().lower()
            broker_account_scope = str(
                dict(runtime_state or {}).get("broker_account_scope") or ""
            ).strip()
            if broker_account_mode not in {"demo", "real"} or not broker_account_scope:
                return {}, "live_broker_account_unattested"
            expected_account_mode = str(
                getattr(settings, "live_expected_account_mode", "") or ""
            ).strip().lower()
            if (
                expected_account_mode not in {"demo", "real"}
                or broker_account_mode != expected_account_mode
            ):
                return {}, "live_broker_account_mode_mismatch"
            if (
                not bool(meta.get("rollout_active", False))
                or str(meta.get("rollout_mode") or "").strip().lower()
                not in ROLLOUT_EXECUTION_MODES
            ):
                return {}, "live_rollout_inactive"
            if not bool(meta.get("rollout_pair_allowlisted", False)):
                return {}, "live_rollout_pair_blocked"
        if not live_pair_scope or str(pair).strip().upper() not in live_pair_scope:
            return {}, "live_pair_not_allowlisted"
        if selected_action == "enter" and sleeve and (not live_sleeve_scope or sleeve not in live_sleeve_scope):
            return {}, "live_sleeve_not_allowlisted"
        # Feed freshness is an entry-admission requirement. Protective actions
        # must remain deliverable when market-data health degrades; their final
        # risk payload, trace, scopes, and committee verdict are still checked.
        if selected_action == "enter" and (
            not bool(meta.get("mt4_fresh", False))
            or not bool(meta.get("ticks_fresh", False))
        ):
            return {}, "live_readiness_unhealthy"
        if not str(orch.get("run_id") or "").strip() or not str(orch.get("trace_id") or "").strip():
            return {}, "live_trace_missing"
        # A committee/orchestration fault cannot be made trustworthy merely by
        # carrying a preview from a degraded path.  Live admission is fail
        # closed on every fallback, classified fault, and timeout.
        latency_budget_state = dict(orch.get("latency_budget_state") or {})
        if (
            bool(orch.get("fallback_used", False))
            or str(orch.get("fault_classification") or "").strip()
            or bool(latency_budget_state.get("budget_exceeded", False))
        ):
            return {}, "live_shadow_fault"
        if int(_safe_float(orch.get("latency_ms"), 0.0)) >= int(getattr(settings, "agent_decision_timeout_ms", 250) or 250):
            return {}, "live_budget_exceeded"
        if selected_action in {"hold", "no_trade", ""}:
            return {}, f"{mode_name}_governed_{selected_action or 'hold'}"
        if not live_intent_scope or selected_action not in live_intent_scope:
            return {}, "live_intent_not_allowlisted"

    if selected_action in {"hold", "no_trade", ""}:
        return {}, f"{mode_name}_governed_{selected_action or 'hold'}"

    risk_approved_action = governed_action_for_risk_approved_payload(default_payload)
    if not risk_approved_action:
        return {}, "risk_kernel_missing_order"
    if selected_action != risk_approved_action:
        return {}, f"{mode_name}_governed_action_mismatch"

    if selected_action == "enter":
        preview_payload = paper_command_preview_payload(
            preview=governed_preview,
            pair=pair,
            ts_value=ts_value,
            action_tag="entry",
        )
        if not preview_payload and mode_name != "paper":
            return {}, f"{mode_name}_missing_command_preview"
        if preview_payload:
            _, payload_reason = reconcile_governed_payload(
                payload=preview_payload,
                decision=decision,
                pair=pair,
                selected_action=selected_action,
                mode_name=mode_name,
            )
            if payload_reason:
                return {}, str(payload_reason)
        # The committee selects or vetoes an action; it is not an execution
        # risk authority.  Broker fields therefore come only from the exact
        # order approved by the risk kernel.  A later controlled RL stage may
        # reduce lots, and the final handoff revalidates that bound.
        payload = dict(default_payload or {})
        payload, payload_reason = reconcile_governed_payload(
            payload=payload,
            decision=decision,
            pair=pair,
            selected_action=selected_action,
            mode_name=mode_name,
        )
        if payload_reason:
            return {}, str(payload_reason)
        return payload, (f"{mode_name}_missing_command_preview" if not payload else "")
    if selected_action == "exit":
        preview_payload = paper_command_preview_payload(
            preview=governed_preview,
            pair=pair,
            ts_value=ts_value,
            action_tag="exit",
            )
        if not preview_payload and mode_name != "paper":
            return {}, f"{mode_name}_missing_command_preview"
        if preview_payload:
            _, payload_reason = reconcile_governed_payload(
                payload=preview_payload,
                decision=decision,
                pair=pair,
                selected_action=selected_action,
                mode_name=mode_name,
            )
            if payload_reason:
                return {}, str(payload_reason)
        payload = dict(default_payload or {})
        payload, payload_reason = reconcile_governed_payload(
            payload=payload,
            decision=decision,
            pair=pair,
            selected_action=selected_action,
            mode_name=mode_name,
        )
        if payload_reason:
            return {}, str(payload_reason)
        return payload, (f"{mode_name}_missing_command_preview" if not payload else "")
    if selected_action == "reduce":
        preview_payload = paper_command_preview_payload(
            preview=governed_preview,
            pair=pair,
            ts_value=ts_value,
            action_tag="close_partial",
        )
        if not preview_payload and mode_name != "paper":
            return {}, f"{mode_name}_missing_command_preview"
        if preview_payload:
            _, payload_reason = reconcile_governed_payload(
                payload=preview_payload,
                decision=decision,
                pair=pair,
                selected_action=selected_action,
                mode_name=mode_name,
            )
            if payload_reason:
                return {}, str(payload_reason)
        payload = dict(default_payload or {})
        payload, payload_reason = reconcile_governed_payload(
            payload=payload,
            decision=decision,
            pair=pair,
            selected_action=selected_action,
            mode_name=mode_name,
        )
        if payload_reason:
            return {}, str(payload_reason)
        return payload, (f"{mode_name}_missing_command_preview" if not payload else "")
    if selected_action == "tighten_stop":
        preview_payload = paper_command_preview_payload(
            preview=governed_preview,
            pair=pair,
            ts_value=ts_value,
            action_tag="adjust_sl",
        )
        if not preview_payload and mode_name != "paper":
            return {}, f"{mode_name}_missing_command_preview"
        if preview_payload:
            _, payload_reason = reconcile_governed_payload(
                payload=preview_payload,
                decision=decision,
                pair=pair,
                selected_action=selected_action,
                mode_name=mode_name,
            )
            if payload_reason:
                return {}, str(payload_reason)
        payload = dict(default_payload or {})
        payload, payload_reason = reconcile_governed_payload(
            payload=payload,
            decision=decision,
            pair=pair,
            selected_action=selected_action,
            mode_name=mode_name,
        )
        if payload_reason:
            return {}, str(payload_reason)
        return payload, (f"{mode_name}_missing_command_preview" if not payload else "")
    payload = dict(default_payload or {})
    return payload, (f"{mode_name}_unsupported_selected_action" if not payload else "")


def paper_governed_command_payload(
    *,
    decision: dict[str, Any],
    orchestration: dict[str, Any],
    pair: str,
    ts_value: str,
    default_payload: dict[str, Any],
    default_action_tag: str,
    settings: Any,
) -> tuple[dict[str, Any], str]:
    return governed_command_payload_for_mode(
        decision=decision,
        orchestration=orchestration,
        pair=pair,
        ts_value=ts_value,
        default_payload=default_payload,
        default_action_tag=default_action_tag,
        settings=settings,
        mode="paper",
    )


def live_governed_command_payload(
    *,
    decision: dict[str, Any],
    orchestration: dict[str, Any],
    pair: str,
    ts_value: str,
    default_payload: dict[str, Any],
    default_action_tag: str,
    settings: Any,
    runtime_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    return governed_command_payload_for_mode(
        decision=decision,
        orchestration=orchestration,
        pair=pair,
        ts_value=ts_value,
        default_payload=default_payload,
        default_action_tag=default_action_tag,
        settings=settings,
        mode="live",
        runtime_state=runtime_state,
    )


def decision_meta_position_open(meta: dict[str, Any] | None) -> bool:
    meta_map = dict(meta or {})
    if "position_open" in meta_map:
        return bool(meta_map.get("position_open"))
    if "has_open_position" in meta_map:
        return bool(meta_map.get("has_open_position"))
    return bool(
        int(_safe_float(meta_map.get("position_count_pair", 0), 0.0)) > 0
        or str(meta_map.get("position_signature", "")).strip()
    )


def orchestration_baseline_action(
    *,
    decision: dict[str, Any],
    pending_entry: dict[str, Any] | None,
    pending_position_action: dict[str, Any] | None,
) -> dict[str, Any]:
    meta = dict(decision.get("metadata", {}) or {})
    position_open = decision_meta_position_open(meta)
    symbol = str(decision.get("symbol") or meta.get("pair") or "").upper()
    score = float(_safe_float(decision.get("score"), 0.0))
    reasons = [str(item) for item in list(decision.get("reasons") or []) if str(item).strip()]
    side = str(decision.get("side") or meta.get("position_side") or "").upper()
    if pending_position_action:
        lifecycle_action = str(pending_position_action.get("lifecycle_action") or "hold").strip().lower()
        action = "reduce" if lifecycle_action == "partial_tp" else lifecycle_action
        if action not in {"exit", "reduce", "tighten_stop"}:
            action = "hold"
        return {
            "symbol": symbol,
            "pair": symbol,
            "side": "FLAT" if action == "exit" else side,
            "action": action,
            "intent": action,
            "score": score,
            "position_open": True,
            "blocking_reasons": reasons,
            "command_preview": {
                "close_lots": float(_safe_float(pending_position_action.get("close_lots"), 0.0)),
                "sl_price": float(_safe_float(pending_position_action.get("sl_price"), 0.0)),
                "approved_order": dict(pending_position_action.get("approved_order") or meta.get("approved_order") or {}),
            },
        }
    entry_preview = dict(
        (pending_entry or {}).get("risk_approved_order")
        or (pending_entry or {}).get("approved_order")
        or (pending_entry or {}).get("payload")
        or meta.get("risk_approved_order")
        or {}
    )
    canonical_ready = bool(
        meta.get("canonical_entry_ready")
        if "canonical_entry_ready" in meta
        else decision.get("execution_ready", False)
    )
    if pending_entry and canonical_ready and entry_preview:
        return {
            "symbol": symbol,
            "pair": symbol,
            "side": side,
            "action": "enter",
            "intent": "enter",
            "score": score,
            "position_open": False,
            "blocking_reasons": [],
            "command_preview": dict(entry_preview),
        }
    if pending_entry and canonical_ready and not entry_preview:
        reasons = ["final_entry_risk_missing_order"]
    if reasons:
        return {
            "symbol": symbol,
            "pair": symbol,
            "side": "FLAT",
            "action": "no_trade",
            "intent": "no_trade",
            "score": score,
            "position_open": bool(position_open),
            "blocking_reasons": reasons,
            "command_preview": {},
        }
    return {
        "symbol": symbol,
        "pair": symbol,
        "side": side if bool(position_open) else "FLAT",
        "action": "hold",
        "intent": "hold",
        "score": score,
        "position_open": bool(position_open),
        "blocking_reasons": reasons,
        "command_preview": {},
    }


def orchestration_model_bundle_version(
    *,
    pair: str,
    settings: Any,
    model_sets: dict[str, LoadedModelSet],
) -> str:
    configured = str(getattr(settings, "model_bundle_version", "") or "").strip()
    if configured:
        return configured
    loaded = model_sets.get(str(pair).upper())
    if loaded is not None:
        return str(getattr(loaded, "model_set_id", "") or "")
    return ""


def stamp_orchestration_payload(
    *,
    payload: dict[str, Any],
    orchestration: dict[str, Any] | None,
    live_authority: dict[str, Any] | None = None,
    release_authority: dict[str, Any] | None = None,
    sleeve: str = "",
) -> dict[str, Any]:
    orch = dict(orchestration or {})
    if not orch or not bool(orch.get("enabled", False)):
        return dict(payload or {})
    from fxstack.orchestration.context_builder import build_idempotency_key

    stamped = dict(payload or {})
    stamped["correlation_id"] = str(orch.get("correlation_id") or "")
    stamped["trace_id"] = str(orch.get("trace_id") or "")
    stamped["thread_id"] = str(orch.get("thread_id") or "")
    stamped["schema_version"] = str(orch.get("schema_version") or ORCHESTRATION_SCHEMA_VERSION)
    stamped["orchestration_meta_json"] = {
        "enabled": True,
        "agent_mode": str(orch.get("agent_mode") or ""),
        "cycle_id": str(orch.get("cycle_id") or ""),
        "run_id": str(orch.get("run_id") or ""),
        "trace_id": str(orch.get("trace_id") or ""),
        "pair": str(orch.get("pair") or stamped.get("symbol") or "").upper(),
        "fallback_used": bool(orch.get("fallback_used", False)),
        "shadow_action": str(orch.get("shadow_action") or ""),
        "governed_selected_action": str(orch.get("governed_selected_action") or ""),
        "approval_state": str(orch.get("approval_state") or "auto"),
        "divergence_reason": str(orch.get("divergence_reason") or ""),
        "fault_classification": str(orch.get("fault_classification") or ""),
        "adaptive_sleeve": str(sleeve or "").strip().lower(),
    }
    if live_authority is not None:
        live = dict(live_authority or {})
        authority_revision = safe_authority_revision(
            live.get("authority_revision")
        )
        stamped["expected_authority_revision"] = int(authority_revision)
        stamped["orchestration_meta_json"]["authority_revision"] = int(
            authority_revision
        )
        stamped["orchestration_meta_json"]["bundle_run_id"] = str(
            live.get("bundle_run_id") or ""
        )
        stamped["orchestration_meta_json"]["stage_index"] = int(
            _safe_float(live.get("current_stage_index"), 0.0)
        )
    if release_authority is not None:
        release = dict(release_authority or {})
        request = dict(release.get("request") or {})
        ack = dict(release.get("ack") or {})
        stamped["orchestration_meta_json"].update(
            {
                "release_generation_id": str(
                    request.get("generation_id") or ""
                ),
                "release_request_sha256": str(
                    request.get("request_sha256") or ""
                ),
                "release_model_identity_sha256": str(
                    request.get("model_identity_sha256") or ""
                ),
                "release_manifest_file_sha256": str(
                    request.get("manifest_file_sha256") or ""
                ),
                "release_runtime_boot_id": str(
                    ack.get("runtime_boot_id") or ""
                ),
            }
        )
    stamped["idempotency_key"] = build_idempotency_key(
        pair=str(orch.get("pair") or stamped.get("symbol") or ""),
        cycle_id=str(orch.get("cycle_id") or ""),
        runtime_mode=str(orch.get("agent_mode") or "off"),
        payload=stamped,
    )
    return stamped


def capture_orchestration_cycle(
    *,
    decisions: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
    pending_position_actions: list[dict[str, Any]],
    svc: Any,
    settings: Any,
    loop_ts: float,
    state: dict[str, Any],
    portfolio_state: dict[str, Any],
    governance: dict[str, Any],
    model_sets: dict[str, LoadedModelSet],
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    agent_mode = normalize_agent_mode(getattr(settings, "agent_mode", "off"))
    if agent_mode == "off" or not decisions:
        return {}, {
            "enabled": False,
            "agent_mode": "off",
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "pair_count": 0,
            "packet_count": 0,
            "trace_count": 0,
            "fault_count": 0,
            "p50_ms": 0,
            "p95_ms": 0,
            "p99_ms": 0,
            "divergence_counts": {},
            "fault_counts": {},
            "per_node_latency_ms": {},
        }

    from fxstack.orchestration.context_builder import (
        build_decision_context,
        build_version_bundle,
    )
    from fxstack.orchestration.telemetry import (
        record_persistence_failure as _record_orchestration_persistence_failure,
        record_run as _record_orchestration_run,
        start_span as _orchestration_span,
    )

    runtime = get_orchestration_graph_runtime()
    cycle_id = orchestration_cycle_id(loop_ts)
    timeout_ms = max(1, int(getattr(settings, "agent_decision_timeout_ms", 250) or 250))
    node_timeout_ms = max(1, int(getattr(settings, "agent_max_node_ms", 50) or 50))
    max_parallel_proposals = max(1, int(getattr(settings, "agent_max_parallel_proposals", 8) or 8))
    records_by_index: dict[int, dict[str, Any]] = {}
    latencies: list[int] = []
    divergence_counts: Counter[str] = Counter()
    fault_counts: Counter[str] = Counter()
    per_node_samples: dict[str, list[int]] = defaultdict(list)
    eligible_pairs = 0
    packet_count = 0
    trace_count = 0
    summary = {
        "enabled": True,
        "agent_mode": str(agent_mode),
        "schema_version": ORCHESTRATION_SCHEMA_VERSION,
        "pair_count": 0,
        "packet_count": 0,
        "trace_count": 0,
        "fault_count": 0,
        "p50_ms": 0,
        "p95_ms": 0,
        "p99_ms": 0,
        "divergence_counts": {},
        "fault_counts": {},
        "per_node_latency_ms": {},
    }
    entry_by_index = {int(item.get("index", -1)): dict(item or {}) for item in list(pending_entries or [])}
    action_by_index = {int(item.get("index", -1)): dict(item or {}) for item in list(pending_position_actions or [])}

    for index, decision in enumerate(list(decisions or [])):
        meta = dict(decision.get("metadata", {}) or {})
        pair = str(meta.get("pair") or decision.get("symbol") or "").upper()
        if not pair:
            continue
        if agent_mode == "shadow" and not orchestration_shadow_pair_enabled(pair=pair, settings=settings):
            records_by_index[int(index)] = {
                "enabled": False,
                "agent_mode": str(agent_mode),
                "cycle_id": str(cycle_id),
                "pair": str(pair),
                "run_id": "",
                "trace_id": "",
                "correlation_id": "",
                "thread_id": "",
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                "fallback_used": False,
                "shadow_action": "disabled",
                "divergence_reason": "pair_not_allowlisted",
                "blocking_reasons": ["pair_not_allowlisted"],
                "proposal_votes": {"total": 0, "by_intent": {}, "by_side": {}, "by_agent": {}},
                "fault_classification": "",
                "latency_ms": 0,
            }
            continue
        eligible_pairs += 1
        model_bundle_version = orchestration_model_bundle_version(pair=pair, settings=settings, model_sets=model_sets)
        attrs = {
            "fxstack.pair": str(pair),
            "fxstack.cycle_id": str(cycle_id),
            "fxstack.runtime_mode": str(agent_mode),
            "fxstack.policy_version": str(getattr(settings, "policy_version", "") or ""),
            "fxstack.model_bundle_version": str(model_bundle_version),
            "fxstack.schema_version": ORCHESTRATION_SCHEMA_VERSION,
        }
        pending_entry = entry_by_index.get(int(index))
        pending_position_action = action_by_index.get(int(index))
        baseline_action = orchestration_baseline_action(
            decision=decision,
            pending_entry=pending_entry,
            pending_position_action=pending_position_action,
        )
        with _orchestration_span("execute_tool", attributes={**attrs, "fxstack.tool": "context_builder"}):
            context = build_decision_context(
                pair=pair,
                cycle_id=cycle_id,
                runtime_mode=agent_mode,
                tick={
                    "bid": meta.get("bid"),
                    "ask": meta.get("ask"),
                    "spread_bps": meta.get("spread_bps"),
                    "ts": meta.get("ts"),
                },
                feature_refs={
                    "model_set_id": str(meta.get("model_set_id") or ""),
                    "registry_path": str(meta.get("registry_path") or ""),
                    "feature_service": str(meta.get("feature_service") or ""),
                    "feature_ts": str(meta.get("feature_ts") or meta.get("ts") or ""),
                },
                live_signal={
                    "symbol": str(decision.get("symbol") or pair),
                    "side": str(decision.get("side") or ""),
                    "score": float(_safe_float(decision.get("score"), 0.0)),
                    "confidence": float(_safe_float(decision.get("confidence"), 0.0)),
                    "trade_prob": float(_safe_float(meta.get("trade_prob"), 0.0)),
                    "expected_edge_bps": float(_safe_float(meta.get("expected_edge_bps"), 0.0)),
                    "uncertainty_score": float(_safe_float(meta.get("uncertainty_score"), 0.0)),
                },
                policy_state={
                    "execution_ready": bool(decision.get("execution_ready", False)),
                    "reasons": list(decision.get("reasons") or []),
                    "strategy_engine_mode": str(meta.get("strategy_engine_mode") or getattr(settings, "strategy_engine_mode", "supervised_legacy")),
                    "execution_mode": str(meta.get("execution_mode") or ""),
                    "position_open": bool(decision_meta_position_open(meta)),
                    "position_side": str(meta.get("position_side") or ""),
                    "position_profit": float(_safe_float(meta.get("position_profit"), 0.0)),
                    "lifecycle_action": str(
                        meta.get("lifecycle_action")
                        or ((pending_position_action or {}).get("lifecycle_action"))
                        or ""
                    ),
                    "lifecycle_reason": str(
                        meta.get("lifecycle_reason")
                        or ((pending_position_action or {}).get("lifecycle_reason"))
                        or ""
                    ),
                    "exit_action_score": float(_safe_float(meta.get("exit_action_score"), 0.0)),
                    "allocator_selected": bool(meta.get("allocator_selected", False)),
                    "allocator_rejection_reason": str(meta.get("allocator_rejection_reason") or ""),
                    "portfolio_posture": str(meta.get("portfolio_posture") or ""),
                    "playbook": str(meta.get("playbook") or meta.get("adaptive_playbook") or ""),
                    "adaptive_playbook": str(meta.get("adaptive_playbook") or meta.get("playbook") or ""),
                    "adaptive_playbook_score": float(_safe_float(meta.get("adaptive_playbook_score"), _safe_float(meta.get("playbook_score"), 0.0))),
                    "adaptive_location_score": float(_safe_float(meta.get("adaptive_location_score"), _safe_float(meta.get("location_score"), 0.0))),
                    "adaptive_trigger_score": float(_safe_float(meta.get("adaptive_trigger_score"), _safe_float(meta.get("trigger_score"), 0.0))),
                    "adaptive_entry_quality": float(_safe_float(meta.get("adaptive_entry_quality"), _safe_float(meta.get("entry_quality_score"), 0.0))),
                    "intelligent_decision": dict(meta.get("intelligent_decision") or {}),
                    "adaptive_size_scale": float(_safe_float(meta.get("adaptive_size_scale"), 1.0)),
                    "adaptive_advisories": list(meta.get("adaptive_advisories") or []),
                    "hard_entry_blocking_reasons": [
                        str(reason)
                        for reason in list(decision.get("reasons") or [])
                        if (
                            is_operational_hard_entry_block_reason(reason)
                            or (
                                bool(meta.get("adaptive_selected", False))
                                and not bool(meta.get("final_entry_risk_approved", False))
                            )
                        )
                    ],
                    "entry_margin": float(_safe_float(meta.get("entry_margin"), 0.0)),
                    "meta_margin": float(_safe_float(meta.get("meta_margin"), 0.0)),
                    "reversal_should_exit": bool(meta.get("reversal_should_exit", False)),
                    "reversal_ready": bool(meta.get("reversal_ready", False)),
                    "spread_bps": float(_safe_float(meta.get("spread_bps"), 0.0)),
                    "max_allowed_spread_bps": float(_safe_float(meta.get("max_allowed_spread_bps"), getattr(settings, "max_allowed_spread_bps", 0.0))),
                },
                portfolio_state={
                    **dict(portfolio_state or {}),
                    "portfolio_posture": str(meta.get("portfolio_posture") or portfolio_state.get("portfolio_posture") or ""),
                    "replacement_pressure": float(_safe_float(meta.get("replacement_urgency"), _safe_float(portfolio_state.get("replacement_pressure"), 0.0))),
                },
                risk_envelope={
                    "governance": dict(governance or {}),
                    "approved_order": dict(meta.get("approved_order") or {}),
                },
                runtime_state={
                    "runtime_status": str(state.get("runtime_status") or ""),
                    "runtime_last_cycle_ts": state.get("runtime_last_cycle_ts"),
                    "decision_timeout_ms": int(timeout_ms),
                    "max_node_ms": int(node_timeout_ms),
                    "max_parallel_proposals": int(max_parallel_proposals),
                    "require_human_approval": bool(getattr(settings, "agent_require_human_approval", True)),
                    "pair_tier": str(settings.pair_tier(pair)) if hasattr(settings, "pair_tier") else "",
                    "configured_pairs": list(getattr(settings, "pairs", []) or []),
                },
                version_bundle=build_version_bundle(
                    policy_version=str(getattr(settings, "policy_version", "") or ""),
                    model_bundle_version=str(model_bundle_version),
                ),
                ts_utc=datetime.fromtimestamp(float(loop_ts), tz=UTC),
            )

        fallback_used = False
        error_class: str | None = None
        graph_latency_ms = 0
        try:
            with _orchestration_span("invoke_agent", attributes={**attrs, "fxstack.thread_id": context.thread_id, "fxstack.correlation_id": context.correlation_id}):
                graph_result = runtime.invoke(
                    thread_id=context.thread_id,
                    state={
                        "thread_id": context.thread_id,
                        "run_id": str(context.run_id),
                        "pair": context.pair,
                        "cycle_id": context.cycle_id,
                        "runtime_mode": context.runtime_mode,
                        "trace_id": f"orch-{uuid.uuid4()}",
                        "decision_context": context.model_dump(mode="json"),
                        "baseline_action": dict(baseline_action or {}),
                        "agent_proposals": [],
                        "proposal_votes": {},
                        "shadow_action": {},
                        "divergence_reason": "",
                        "blocking_reasons": [],
                        "fault_classification": None,
                        "latency_budget_state": {
                            "cycle_budget_ms": int(timeout_ms),
                            "max_node_ms": int(node_timeout_ms),
                            "max_parallel_proposals": int(max_parallel_proposals),
                        },
                        "node_spans": [],
                        "tool_calls": [],
                        "model_calls": [],
                    },
                    service=svc,
                    runtime_mode=agent_mode,
                    fallback_used=False,
                    durability=str(getattr(settings, "agent_durability", "async") or "async"),
                )
            graph_latency_ms = int(graph_result.latency_ms)
        except Exception as exc:
            fallback_used = True
            error_class = type(exc).__name__
            _record_orchestration_persistence_failure(attributes=attrs)
            graph_result = None

        graph_state = dict(getattr(graph_result, "state", {}) or {})
        trace_id = str(graph_state.get("trace_id") or "")
        shadow_action = dict(graph_state.get("shadow_action") or {})
        packet_payload = dict(graph_state.get("packet") or {})
        governed_payload = dict(packet_payload.get("governed_decision") or {})
        divergence_reason = str(graph_state.get("divergence_reason") or ("shadow_fault" if error_class else "no_shadow_output"))
        blocking_reasons = [
            str(item)
            for item in list(graph_state.get("blocking_reasons") or baseline_action.get("blocking_reasons") or [])
            if str(item).strip()
        ]
        proposal_votes = dict(graph_state.get("proposal_votes") or {"total": 0, "by_intent": {}, "by_side": {}, "by_agent": {}})
        fault_classification = str(graph_state.get("fault_classification") or error_class or "").strip()
        node_spans = list(graph_state.get("node_spans") or [])
        command_preview = dict(graph_state.get("command_preview") or {})
        if graph_latency_ms > timeout_ms:
            fault_classification = fault_classification or "latency_budget_exceeded"
        if fault_classification:
            fallback_used = True
        latencies.append(int(graph_latency_ms))
        if divergence_reason:
            divergence_counts[str(divergence_reason)] += 1
        if fault_classification:
            fault_counts[str(fault_classification)] += 1
        for span in node_spans:
            node_name = str(dict(span or {}).get("node") or "")
            node_latency = int(_safe_float(dict(span or {}).get("latency_ms"), 0.0))
            if node_name:
                per_node_samples[node_name].append(node_latency)
        if bool(graph_state.get("persisted")) and graph_state.get("packet"):
            packet_count += 1
        if bool(graph_state.get("persisted")) and trace_id:
            trace_count += 1
        _record_orchestration_run(
            latency_ms=int(graph_latency_ms),
            attributes={
                **attrs,
                "fxstack.fallback_used": bool(fallback_used),
                "fxstack.correlation_id": context.correlation_id,
                "fxstack.thread_id": context.thread_id,
            },
            fallback_used=bool(fallback_used),
        )

        record = {
            "enabled": True,
            "agent_mode": str(agent_mode),
            "cycle_id": str(cycle_id),
            "pair": str(pair),
            "run_id": str(context.run_id),
            "trace_id": str(trace_id),
            "correlation_id": str(context.correlation_id),
            "thread_id": str(context.thread_id),
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "fallback_used": bool(fallback_used),
            "baseline_action": str(baseline_action.get("action") or baseline_action.get("intent") or ""),
            "baseline_action_payload": dict(baseline_action or {}),
            "shadow_action": str(shadow_action.get("action") or ""),
            "shadow_action_payload": dict(shadow_action or {}),
            "divergence_reason": str(divergence_reason),
            "blocking_reasons": list(blocking_reasons),
            "proposal_votes": dict(proposal_votes or {}),
            "fault_classification": str(fault_classification),
            "latency_ms": int(graph_latency_ms),
            "latency_budget_state": dict(graph_state.get("latency_budget_state") or {}),
            "winning_proposal_id": str(packet_payload.get("winning_proposal_id") or governed_payload.get("winning_proposal_id") or ""),
            "ranked_proposal_ids": list(packet_payload.get("ranked_proposal_ids") or governed_payload.get("ranked_proposal_ids") or []),
            "arbiter_stage": str(packet_payload.get("arbiter_stage") or governed_payload.get("arbiter_stage") or ""),
            "arbiter_rationale": str(packet_payload.get("arbiter_rationale") or governed_payload.get("arbiter_rationale") or ""),
            "score_path": list(packet_payload.get("score_path") or governed_payload.get("score_path") or []),
            "invariant_results": dict(packet_payload.get("invariant_results") or governed_payload.get("invariant_results") or {}),
            "committee_summary": dict(graph_state.get("committee_summary") or {}),
            "governed_decision": dict(governed_payload or {}),
            "governed_allowed": bool(governed_payload.get("allowed", False)),
            "governed_selected_action": str(governed_payload.get("selected_action") or ""),
            "approval_state": str(governed_payload.get("approval_state") or "auto"),
            "command_preview": dict(command_preview or governed_payload.get("command_preview") or {}),
        }
        records_by_index[int(index)] = record
    summary["pair_count"] = int(eligible_pairs)
    summary["packet_count"] = int(packet_count)
    summary["trace_count"] = int(trace_count)
    summary["fault_count"] = int(sum(fault_counts.values()))
    summary["p50_ms"] = orchestration_percentile(latencies, 0.50)
    summary["p95_ms"] = orchestration_percentile(latencies, 0.95)
    summary["p99_ms"] = orchestration_percentile(latencies, 0.99)
    summary["divergence_counts"] = dict(divergence_counts)
    summary["fault_counts"] = dict(fault_counts)
    summary["per_node_latency_ms"] = {
        str(node): {
            "p50_ms": orchestration_percentile(values, 0.50),
            "p95_ms": orchestration_percentile(values, 0.95),
            "p99_ms": orchestration_percentile(values, 0.99),
        }
        for node, values in sorted(per_node_samples.items())
    }
    return records_by_index, summary


def build_orchestration_phase1_diag(
    *,
    orchestration_diag: dict[str, Any],
    records_by_index: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    first_enabled = next(
        (
            dict(record or {})
            for _, record in sorted(dict(records_by_index or {}).items(), key=lambda item: int(item[0]))
            if bool(dict(record or {}).get("enabled", False))
        ),
        {},
    )
    return {
        "enabled": bool(orchestration_diag.get("enabled", False)),
        "agent_mode": str(orchestration_diag.get("agent_mode") or "off"),
        "schema_version": str(orchestration_diag.get("schema_version") or ORCHESTRATION_SCHEMA_VERSION),
        "correlation_id": str(first_enabled.get("correlation_id") or ""),
        "thread_id": str(first_enabled.get("thread_id") or ""),
        "fallback_used": bool(first_enabled.get("fallback_used", False)),
        "run_id": str(first_enabled.get("run_id") or ""),
        "trace_id": str(first_enabled.get("trace_id") or ""),
    }


def build_orchestration_snapshot_payload(
    *,
    orchestration_diag: dict[str, Any],
    records_by_index: dict[int, dict[str, Any]] | None = None,
    phase2_sections: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep the Phase 1 orchestration snapshot stable and hang additive Phase 2 data off a dedicated bucket."""

    orchestration_phase1 = build_orchestration_phase1_diag(
        orchestration_diag=dict(orchestration_diag or {}),
        records_by_index=dict(records_by_index or {}),
    )
    orchestration_shadow = dict(orchestration_diag or {})
    phase2_payload = {
        str(key): dict(value or {})
        for key, value in dict(phase2_sections or {}).items()
        if value not in ({}, None, [], "")
    }
    if phase2_payload:
        orchestration_shadow["phase2"] = phase2_payload
    return orchestration_phase1, orchestration_shadow


def build_orchestration_live_runtime_diag(
    *,
    state: dict[str, Any],
    settings: Any,
    orchestration_diag: dict[str, Any],
    entry_execution_diag: dict[str, Any],
    risk_cycle_diag: dict[str, Any],
) -> dict[str, Any]:
    previous = dict(dict(dict(state or {}).get("runtime_diag") or {}).get("orchestration_live") or {})
    previous_mode = str(previous.get("mode") or previous.get("agent_mode") or "").strip().lower()
    preserve_live_scopes = previous_mode == "live"
    ramp_steps = list(getattr(settings, "phase6b_canary_ramp_steps_pct", []) or [1, 5, 10])
    current_stage_index = max(0, int(_safe_float(previous.get("current_stage_index"), 0.0)))
    default_stage_pct = int(ramp_steps[min(current_stage_index, max(0, len(ramp_steps) - 1))]) if ramp_steps else 0
    current_stage_pct = int(
        _safe_float(previous.get("current_stage_pct"), float(default_stage_pct))
    )
    fault_counts = {
        str(key): int(_safe_float(value, 0.0))
        for key, value in dict(orchestration_diag.get("fault_counts") or {}).items()
        if str(key).strip()
    }
    trace_persistence_failure_count = int(
        sum(value for key, value in fault_counts.items() if "persist" in str(key).lower())
    )
    release_bundle_run_id = str(previous.get("bundle_run_id") or "").strip()
    entry_evidence_by_pair: dict[str, dict[str, Any]] = {}
    for raw_pair, raw_evidence in dict(previous.get("entry_evidence_by_pair") or {}).items():
        pair = str(raw_pair).upper().strip()
        evidence = dict(raw_evidence or {})
        if (
            not pair
            or int(_safe_float(evidence.get("stage_index"), -1.0)) != current_stage_index
            or int(_safe_float(evidence.get("stage_pct"), -1.0)) != current_stage_pct
            or str(evidence.get("bundle_run_id") or "").strip() != release_bundle_run_id
        ):
            continue
        approved_keys = {
            str(value) for value in list(evidence.get("approved_action_keys") or []) if str(value).strip()
        }
        submitted_keys = {
            str(value) for value in list(evidence.get("submitted_action_keys") or []) if str(value).strip()
        } & approved_keys
        accepted_keys = {
            str(value) for value in list(evidence.get("accepted_action_keys") or []) if str(value).strip()
        } & submitted_keys
        entry_evidence_by_pair[pair] = {
            "approved_action_keys": approved_keys,
            "submitted_action_keys": submitted_keys,
            "accepted_action_keys": accepted_keys,
            "observed_at": float(_safe_float(evidence.get("observed_at"), 0.0)),
        }

    for raw_event in list(entry_execution_diag.get("entry_evidence_events") or []):
        event = dict(raw_event or {})
        pair = str(event.get("pair") or "").upper().strip()
        action_key = str(event.get("action_key") or "").strip()
        if not pair or not action_key or not bool(event.get("approved", False)):
            continue
        evidence = entry_evidence_by_pair.setdefault(
            pair,
            {
                "approved_action_keys": set(),
                "submitted_action_keys": set(),
                "accepted_action_keys": set(),
                "observed_at": 0.0,
            },
        )
        evidence["approved_action_keys"].add(action_key)
        if bool(event.get("submitted", False)):
            evidence["submitted_action_keys"].add(action_key)
        if bool(event.get("accepted", False)):
            evidence["accepted_action_keys"].add(action_key)
        evidence["observed_at"] = max(
            float(_safe_float(evidence.get("observed_at"), 0.0)),
            float(_safe_float(event.get("observed_at"), time.time())),
        )

    serialized_entry_evidence: dict[str, dict[str, Any]] = {}
    for pair, evidence in sorted(entry_evidence_by_pair.items()):
        approved_keys = sorted(set(evidence.get("approved_action_keys") or []))[-2000:]
        approved_key_set = set(approved_keys)
        submitted_keys = sorted(set(evidence.get("submitted_action_keys") or []) & approved_key_set)
        accepted_keys = sorted(set(evidence.get("accepted_action_keys") or []) & set(submitted_keys))
        approved_count = len(approved_keys)
        accepted_count = len(accepted_keys)
        serialized_entry_evidence[pair] = {
            "pair": str(pair),
            "bundle_run_id": str(release_bundle_run_id),
            "stage_index": int(current_stage_index),
            "stage_pct": int(current_stage_pct),
            "approved_action_keys": approved_keys,
            "submitted_action_keys": submitted_keys,
            "accepted_action_keys": accepted_keys,
            "approved_count": int(approved_count),
            "submitted_count": int(len(submitted_keys)),
            "accepted_count": int(accepted_count),
            "entry_ratio_vs_baseline": (
                float(accepted_count) / float(approved_count) if approved_count > 0 else 0.0
            ),
            "entry_ratio_evaluable": bool(approved_count > 0),
            "observed_at": float(_safe_float(evidence.get("observed_at"), 0.0)),
        }

    if serialized_entry_evidence:
        approved_entry_count = sum(
            int(value.get("approved_count") or 0) for value in serialized_entry_evidence.values()
        )
        submitted_entry_count = sum(
            int(value.get("submitted_count") or 0) for value in serialized_entry_evidence.values()
        )
        accepted_entry_count = sum(
            int(value.get("accepted_count") or 0) for value in serialized_entry_evidence.values()
        )
        entry_ratio_observed_at = max(
            float(value.get("observed_at") or 0.0) for value in serialized_entry_evidence.values()
        )
    else:
        # Keep aggregate diagnostics useful for isolated callers, but live
        # canary advancement only trusts the release-bound per-pair ledger.
        approved_entry_count = int(_safe_float(entry_execution_diag.get("approved_entry_count"), 0.0))
        submitted_entry_count = int(_safe_float(entry_execution_diag.get("submitted_entry_count"), 0.0))
        accepted_entry_count = int(
            _safe_float(
                entry_execution_diag.get("accepted_entry_count"),
                float(submitted_entry_count),
            )
        )
        entry_ratio_observed_at = 0.0
    entry_ratio_evaluable = bool(approved_entry_count > 0)
    entry_ratio_vs_baseline = (
        float(accepted_entry_count) / float(approved_entry_count)
        if entry_ratio_evaluable
        else 0.0
    )
    return {
        "authority_revision": safe_authority_revision(
            previous.get("authority_revision")
        ),
        "enabled": bool(live_mode_enabled(settings)),
        "mode": str(normalize_agent_mode(getattr(settings, "agent_mode", "off"))),
        "live_scope": "entries_only",
        "runtime_enabled": bool(previous.get("runtime_enabled", True)),
        "queue_kill_active": bool(previous.get("queue_kill_active", False)),
        "queue_kill_reason": str(previous.get("queue_kill_reason") or ""),
        "queue_killed_at": previous.get("queue_killed_at"),
        "last_kill_reason": str(previous.get("last_kill_reason") or ""),
        "last_kill_at": previous.get("last_kill_at"),
        "purged_command_count": int(
            _safe_float(previous.get("purged_command_count"), 0.0)
        ),
        "active_pair_scope": (
            list(previous.get("active_pair_scope") or [])
            if "active_pair_scope" in previous and preserve_live_scopes
            else list(getattr(settings, "agent_live_pair_allowlist", []) or [])
        ),
        "active_pair_scope_configured": bool(
            preserve_live_scopes
            and previous.get(
                "active_pair_scope_configured",
                "active_pair_scope" in previous,
            )
        ),
        "active_sleeve_scope": (
            list(previous.get("active_sleeve_scope") or [])
            if "active_sleeve_scope" in previous and preserve_live_scopes
            else list(getattr(settings, "agent_live_sleeve_allowlist", []) or [])
        ),
        "active_sleeve_scope_configured": bool(
            preserve_live_scopes
            and previous.get(
                "active_sleeve_scope_configured",
                "active_sleeve_scope" in previous,
            )
        ),
        "active_intent_scope": (
            list(previous.get("active_intent_scope") or [])
            if "active_intent_scope" in previous and preserve_live_scopes
            else list(getattr(settings, "agent_live_intent_allowlist", []) or [])
        ),
        "active_intent_scope_configured": bool(
            preserve_live_scopes
            and previous.get(
                "active_intent_scope_configured",
                "active_intent_scope" in previous,
            )
        ),
        "ramp_steps_pct": list(previous.get("ramp_steps_pct") or ramp_steps),
        "current_stage_index": int(_safe_float(previous.get("current_stage_index"), 0.0)),
        "current_stage_pct": int(current_stage_pct),
        "bundle_run_id": str(release_bundle_run_id),
        "release_status": str(previous.get("release_status") or ""),
        "promotion_pack_path": str(previous.get("promotion_pack_path") or ""),
        "signoff_records": list(previous.get("signoff_records") or []),
        "budget_scale": float(
            _safe_float(
                previous.get("budget_scale"),
                _safe_float(dict(risk_cycle_diag.get("rollout") or {}).get("avg_budget_scale"), 0.0),
            )
        ),
        "p95_ms": int(_safe_float(orchestration_diag.get("p95_ms"), 0.0)),
        "p99_ms": int(_safe_float(orchestration_diag.get("p99_ms"), 0.0)),
        "graph_fault_count": int(_safe_float(orchestration_diag.get("fault_count"), 0.0)),
        "repeated_graph_fault_count": int(_safe_float(orchestration_diag.get("fault_count"), 0.0)),
        "trace_persistence_failure_count": int(trace_persistence_failure_count),
        "baseline_fallback_count": int(_safe_float(entry_execution_diag.get("live_baseline_fallback_count"), 0.0)),
        "governed_eligible_count": int(_safe_float(entry_execution_diag.get("live_governed_eligible_count"), 0.0)),
        "governed_submitted_count": int(_safe_float(entry_execution_diag.get("live_governed_submitted_count"), 0.0)),
        "governed_blocked_count": int(_safe_float(entry_execution_diag.get("live_governed_blocked_count"), 0.0)),
        "fallback_reason_counts": dict(entry_execution_diag.get("live_fallback_reason_counts") or {}),
        "entry_ratio_vs_baseline": float(entry_ratio_vs_baseline),
        "entry_ratio_evaluable": bool(entry_ratio_evaluable),
        "entry_ratio_status": "observed" if entry_ratio_evaluable else "insufficient_evidence",
        "entry_ratio_approved_count": int(approved_entry_count),
        "entry_ratio_submitted_count": int(submitted_entry_count),
        "entry_ratio_accepted_count": int(accepted_entry_count),
        "entry_ratio_observed_at": float(entry_ratio_observed_at),
        "entry_ratio_stage_index": int(current_stage_index),
        "entry_ratio_stage_pct": int(current_stage_pct),
        "entry_evidence_by_pair": serialized_entry_evidence,
        "slot_utilisation_vs_baseline": float(_safe_float(previous.get("slot_utilisation_vs_baseline"), 1.0)),
        "drawdown_deterioration_pct": float(_safe_float(previous.get("drawdown_deterioration_pct"), 0.0)),
        "ack_success_rate": float(_safe_float(previous.get("ack_success_rate"), 0.0)),
        "ack_timeout_rate": float(_safe_float(previous.get("ack_timeout_rate"), 0.0)),
        "orphan_command_count": int(_safe_float(previous.get("orphan_command_count"), 0.0)),
    }


__all__ = [
    "OPERATIONAL_HARD_ENTRY_BLOCK_REASONS",
    "build_command_id",
    "build_orchestration_live_runtime_diag",
    "build_orchestration_phase1_diag",
    "build_orchestration_snapshot_payload",
    "capture_orchestration_cycle",
    "decision_meta_position_open",
    "get_orchestration_graph_runtime",
    "governed_action_for_risk_approved_payload",
    "governed_command_payload_for_mode",
    "is_operational_hard_entry_block_reason",
    "live_command_admission_diagnostics",
    "live_governed_command_payload",
    "live_mode_enabled",
    "normalize_agent_mode",
    "orchestration_baseline_action",
    "orchestration_cycle_id",
    "orchestration_live_intent_enabled",
    "orchestration_live_pair_enabled",
    "orchestration_live_runtime_state",
    "orchestration_live_sleeve_enabled",
    "orchestration_model_bundle_version",
    "orchestration_paper_intent_enabled",
    "orchestration_paper_pair_enabled",
    "orchestration_paper_sleeve_enabled",
    "orchestration_percentile",
    "orchestration_shadow_pair_enabled",
    "paper_command_preview_payload",
    "paper_governed_command_payload",
    "paper_mode_enabled",
    "paper_mode_rollback",
    "payload_from_approved_order",
    "reconcile_governed_payload",
    "safe_authority_revision",
    "stamp_orchestration_payload",
    "update_orchestration_shadow_command_flow",
    "validate_final_entry_payload_against_risk_approval",
    "validate_final_lifecycle_payload_against_risk_approval",
]
