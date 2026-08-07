# AGENT: ROLE: Managed-position state: serialization, restart hydration, and broker-confirmed reconciliation of partial/exit ledgers.
# AGENT: ENTRYPOINT: imported by `fxstack/runtime/runner.py`; no independent process.
# AGENT: PRIMARY INPUTS: durable command/ACK history, live bridge positions, adaptive/campaign/sleeve registries.
# AGENT: PRIMARY OUTPUTS: restart-safe managed state, reconciled partial-close and exit ledgers, sleeve trade observations.
# AGENT: DEPENDS ON: `fxstack/runtime/positions.py`, `fxstack/strategy/campaign.py`, `fxstack/strategy/sleeve_governance.py`.
# AGENT: CALLED BY: `fxstack/runtime/runner.py`.
# AGENT: STATE / SIDE EFFECTS: mutates caller-owned registries and trackers in place; performs no IO of its own.
# AGENT: HANDSHAKES: `runtime.management_confirmation`, `command.ack`, `runtime.sleeve_governance`.
# AGENT: SEE: `docs/architecture/REFACTOR_PLAN.md` -> `fxstack/runtime/runner.py` -> `docs/agents/runtime-loop.md`
"""Managed-position state, restart hydration, and broker-confirmed reconciliation.

Carved out of ``fxstack.runtime.runner`` -- this is the block that keeps the
runtime's in-memory view of open positions honest across a restart and across
partial fills. It is cohesive on one axis: everything here reads durable command
and ACK history and reconciles it against what the broker actually reports.

Nothing in this module talks to the network, the database, or the clock beyond
what its callers hand it, so every function is testable from plain dicts.

The runner re-imports each public name under its original underscored alias, so
existing call sites -- and tests that reached into ``runner`` -- keep working.
"""

from __future__ import annotations

from collections import Counter
import math
from types import SimpleNamespace
from typing import Any

# Aliased: local variables in this module are already called
# ``position_signature``, and importing the function under that name would
# shadow it mid-function.
from fxstack.runtime.positions import position_signature as compute_position_signature
from fxstack.runtime._util import safe_float
from fxstack.strategy.constants import PLAYBOOK_TREND_PULLBACK, playbook_to_sleeve
from fxstack.strategy.campaign_types import CampaignRegistryEntry
from fxstack.strategy.campaign import (
    CAMPAIGN_STATE_INACTIVE,
    apply_campaign_registry_snapshot,
    build_thesis_id,
    campaign_state_after_close,
    campaign_transition_if_changed,
    serialize_campaign_entry,
)
from fxstack.strategy.sleeve_governance import SleeveGovernanceTracker


# AGENT STATE: Adaptive registries reconcile runtime decisions with live bridge positions so cooldowns and replacement logic persist across bars.
_MANAGED_POSITION_STATE_SCHEMA = "fxstack_managed_position_state_v1"
_MANAGED_POSITION_ABSENCE_GRACE_CYCLES = 3
_PENDING_PARTIAL_STATE_KEYS = (
    "pending_command_id",
    "pending_pair",
    "pending_position_signature",
    "pending_open_lots",
    "pending_close_lots",
    "pending_submitted_ts",
    "pending_bar_index",
)


def managed_state_json_value(value: Any) -> Any:
    """Return a database-JSON-safe representation of managed runtime state."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return float(value) if math.isfinite(float(value)) else 0.0
    if isinstance(value, dict):
        return {
            str(key): managed_state_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [managed_state_json_value(item) for item in value]
    return str(value)


def clear_pending_partial_state(state: dict[str, Any]) -> None:
    for key in _PENDING_PARTIAL_STATE_KEYS:
        state.pop(key, None)


def append_tracker_command_id(
    state: dict[str, Any],
    *,
    field_name: str,
    command_id: str,
) -> None:
    command_ids = [
        str(item)
        for item in list(state.get(field_name) or [])
        if str(item).strip()
    ]
    if str(command_id).strip() not in command_ids:
        command_ids.append(str(command_id).strip())
    state[field_name] = command_ids[-16:]


def serialize_managed_position_state(
    *,
    adaptive_position_registry: dict[str, SimpleNamespace],
    partial_close_tracker: dict[str, dict[str, Any]],
    campaign_registry: dict[str, CampaignRegistryEntry],
    saved_at: float,
    adaptive_pending_entry_registry: dict[str, dict[str, Any]] | None = None,
    adaptive_recent_exit_registry: dict[str, dict[str, Any]] | None = None,
    exit_command_ledger: dict[str, dict[str, Any]] | None = None,
    sleeve_governance_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the strategy memory that belongs to broker position identity."""

    return {
        "schema": _MANAGED_POSITION_STATE_SCHEMA,
        "saved_at": float(saved_at),
        "adaptive_position_registry": {
            str(pair).upper(): managed_state_json_value(dict(vars(position_state)))
            for pair, position_state in sorted(adaptive_position_registry.items())
            if str(pair).strip()
        },
        "partial_close_tracker": {
            str(signature): managed_state_json_value(dict(tracker_state or {}))
            for signature, tracker_state in sorted(partial_close_tracker.items())
            if str(signature).strip()
        },
        "campaign_registry": {
            str(key): managed_state_json_value(serialize_campaign_entry(entry))
            for key, entry in sorted(campaign_registry.items())
            if str(key).strip()
        },
        "adaptive_pending_entry_registry": {
            str(pair).upper(): managed_state_json_value(dict(entry_state or {}))
            for pair, entry_state in sorted(
                dict(adaptive_pending_entry_registry or {}).items()
            )
            if str(pair).strip()
        },
        "adaptive_recent_exit_registry": {
            str(pair).upper(): managed_state_json_value(dict(exit_state or {}))
            for pair, exit_state in sorted(
                dict(adaptive_recent_exit_registry or {}).items()
            )
            if str(pair).strip()
        },
        "exit_command_ledger": {
            str(command_id): managed_state_json_value(dict(ledger_state or {}))
            for command_id, ledger_state in sorted(
                dict(exit_command_ledger or {}).items()
            )
            if str(command_id).strip()
        },
        "sleeve_governance_state": managed_state_json_value(
            dict(sleeve_governance_state or {})
        ),
    }


def restore_managed_position_state(
    *,
    payload: dict[str, Any] | None,
    adaptive_position_registry: dict[str, SimpleNamespace],
    partial_close_tracker: dict[str, dict[str, Any]],
    campaign_registry: dict[str, CampaignRegistryEntry],
    allowed_pairs: set[str] | None = None,
    adaptive_pending_entry_registry: dict[str, dict[str, Any]] | None = None,
    adaptive_recent_exit_registry: dict[str, dict[str, Any]] | None = None,
    exit_command_ledger: dict[str, dict[str, Any]] | None = None,
    sleeve_tracker: SleeveGovernanceTracker | None = None,
) -> dict[str, Any]:
    """Restore only versioned, position-keyed lifecycle memory at startup."""

    raw = dict(payload or {})
    schema = str(raw.get("schema") or "")
    if schema != _MANAGED_POSITION_STATE_SCHEMA:
        return {
            "status": "absent" if not raw else "schema_mismatch",
            "schema": schema,
            "adaptive_position_count": 0,
            "partial_tracker_count": 0,
            "campaign_count": 0,
            "pending_entry_count": 0,
            "recent_exit_count": 0,
            "exit_command_count": 0,
            "sleeve_trade_event_count": 0,
        }

    allowed = {
        str(pair).strip().upper()
        for pair in set(allowed_pairs or set())
        if str(pair).strip()
    }
    restored_positions = 0
    restored_partials = 0
    restored_campaigns = 0

    for raw_pair, raw_state in dict(raw.get("adaptive_position_registry") or {}).items():
        pair = str(raw_pair).strip().upper()
        state = dict(raw_state or {})
        signature = str(state.get("position_signature") or "").strip()
        if not pair or not signature or (allowed and pair not in allowed):
            continue
        state["pair"] = pair
        state["position_signature"] = signature
        state["_missing_position_cycles"] = max(
            0, int(safe_float(state.get("_missing_position_cycles"), 0.0))
        )
        adaptive_position_registry[pair] = SimpleNamespace(**state)
        restored_positions += 1

    for raw_signature, raw_state in dict(raw.get("partial_close_tracker") or {}).items():
        signature = str(raw_signature).strip()
        state = dict(raw_state or {})
        pair = str(state.get("pending_pair") or signature.split("|", 1)[0]).strip().upper()
        if not signature or (allowed and pair and pair not in allowed):
            continue
        state["count"] = max(0, int(safe_float(state.get("count"), 0.0)))
        state["_missing_position_cycles"] = max(
            0, int(safe_float(state.get("_missing_position_cycles"), 0.0))
        )
        last_confirmed = str(state.get("last_partial_cmd_id") or "").strip()
        if last_confirmed and int(state["count"]) > 0:
            append_tracker_command_id(
                state,
                field_name="confirmed_command_ids",
                command_id=last_confirmed,
            )
            append_tracker_command_id(
                state,
                field_name="resolved_command_ids",
                command_id=last_confirmed,
            )
        partial_close_tracker[signature] = state
        restored_partials += 1

    campaign_fields = set(CampaignRegistryEntry.__dataclass_fields__.keys())
    for raw_key, raw_entry in dict(raw.get("campaign_registry") or {}).items():
        entry_payload = {
            str(key): value
            for key, value in dict(raw_entry or {}).items()
            if str(key) in campaign_fields
        }
        pair = str(entry_payload.get("pair") or "").strip().upper()
        if not pair or (allowed and pair not in allowed):
            continue
        entry_payload["pair"] = pair
        if not all(
            str(entry_payload.get(required) or "").strip()
            for required in ("thesis_id", "side", "sleeve")
        ):
            continue
        try:
            campaign_registry[str(raw_key)] = CampaignRegistryEntry(**entry_payload)
        except (TypeError, ValueError):
            continue
        restored_campaigns += 1

    restored_pending_entries = 0
    if adaptive_pending_entry_registry is not None:
        for raw_pair, raw_entry in dict(
            raw.get("adaptive_pending_entry_registry") or {}
        ).items():
            pair = str(raw_pair).strip().upper()
            if not pair or (allowed and pair not in allowed):
                continue
            adaptive_pending_entry_registry[pair] = dict(raw_entry or {})
            restored_pending_entries += 1

    restored_recent_exits = 0
    if adaptive_recent_exit_registry is not None:
        for raw_pair, raw_exit in dict(
            raw.get("adaptive_recent_exit_registry") or {}
        ).items():
            pair = str(raw_pair).strip().upper()
            if not pair or (allowed and pair not in allowed):
                continue
            adaptive_recent_exit_registry[pair] = dict(raw_exit or {})
            restored_recent_exits += 1

    restored_exit_commands = 0
    if exit_command_ledger is not None:
        for raw_command_id, raw_ledger_state in dict(
            raw.get("exit_command_ledger") or {}
        ).items():
            command_id = str(raw_command_id).strip()
            ledger_state = dict(raw_ledger_state or {})
            pair = str(ledger_state.get("pair") or "").strip().upper()
            signature = str(
                ledger_state.get("position_signature") or ""
            ).strip()
            if (
                not command_id
                or not pair
                or not signature
                or (allowed and pair not in allowed)
            ):
                continue
            ledger_state["pair"] = pair
            ledger_state["command_id"] = command_id
            ledger_state["resolved"] = bool(ledger_state.get("resolved", False))
            exit_command_ledger[command_id] = ledger_state
            restored_exit_commands += 1

    restored_sleeve_events = 0
    if sleeve_tracker is not None:
        sleeve_tracker.restore_state(raw.get("sleeve_governance_state"))
        restored_sleeve_events = int(
            sum(snapshot.trades for snapshot in sleeve_tracker.snapshot().values())
        )

    return {
        "status": "restored",
        "schema": schema,
        "saved_at": float(safe_float(raw.get("saved_at"), 0.0)),
        "adaptive_position_count": int(restored_positions),
        "partial_tracker_count": int(restored_partials),
        "campaign_count": int(restored_campaigns),
        "pending_entry_count": int(restored_pending_entries),
        "recent_exit_count": int(restored_recent_exits),
        "exit_command_count": int(restored_exit_commands),
        "sleeve_trade_event_count": int(restored_sleeve_events),
    }


def hydrate_partial_close_tracker_from_commands(
    *,
    commands: list[dict[str, Any]],
    partial_close_tracker: dict[str, dict[str, Any]],
    adaptive_position_registry: dict[str, SimpleNamespace],
    allowed_pairs: set[str] | None = None,
    after_ts: float = 0.0,
) -> dict[str, Any]:
    """Recover the enqueue-to-state-patch crash window from the durable queue."""

    allowed = {
        str(pair).strip().upper()
        for pair in set(allowed_pairs or set())
        if str(pair).strip()
    }
    recovered_acked = 0
    recovered_pending = 0
    recovered_undelivered_expired = 0
    skipped_resolved = 0
    skipped_before_watermark = 0
    rows = sorted(
        [dict(row or {}) for row in list(commands or [])],
        key=lambda row: float(safe_float(row.get("created_at"), 0.0)),
    )

    for row in rows:
        command_change_ts = max(
            float(safe_float(row.get("created_at"), 0.0)),
            float(safe_float(row.get("updated_at"), 0.0)),
        )
        if command_change_ts <= float(after_ts):
            skipped_before_watermark += 1
            continue
        if str(row.get("cmd") or "").strip().upper() != "CLOSE_PARTIAL":
            continue
        payload = dict(row.get("payload_json") or row.get("payload") or {})
        context = dict(payload.get("management_context") or {})
        if str(context.get("schema") or "") != "fxstack_lifecycle_command_context_v1":
            continue
        if str(context.get("lifecycle_action") or "").strip().lower() != "partial_tp":
            continue
        pair = str(context.get("pair") or row.get("symbol") or "").strip().upper()
        signature = str(context.get("position_signature") or "").strip()
        command_id = str(row.get("command_id") or payload.get("command_id") or "").strip()
        if (
            not pair
            or not signature
            or not command_id
            or (allowed and pair not in allowed)
        ):
            continue

        tracker_state = dict(partial_close_tracker.get(signature, {}) or {})
        tracker_state["count"] = max(
            0, int(safe_float(tracker_state.get("count"), 0.0))
        )
        resolved_ids = {
            str(item)
            for item in list(tracker_state.get("resolved_command_ids") or [])
            if str(item).strip()
        }
        if command_id in resolved_ids:
            skipped_resolved += 1
            partial_close_tracker[signature] = tracker_state
            continue

        status = str(row.get("status") or "").strip().lower()
        delivered_count = int(safe_float(row.get("delivered_count"), 0.0))
        if status == "acked":
            tracker_state["count"] = int(tracker_state["count"]) + 1
            tracker_state["last_partial_ts"] = float(
                safe_float(row.get("updated_at"), context.get("submitted_ts", 0.0))
            )
            tracker_state["last_partial_cmd_id"] = command_id
            tracker_state["last_partial_confirmation"] = "durable_broker_ack"
            recovered_bar_index = int(
                safe_float(context.get("bar_index"), -1.0)
            )
            if recovered_bar_index >= 0:
                tracker_state["last_partial_bar_index"] = recovered_bar_index
            append_tracker_command_id(
                tracker_state,
                field_name="confirmed_command_ids",
                command_id=command_id,
            )
            append_tracker_command_id(
                tracker_state,
                field_name="resolved_command_ids",
                command_id=command_id,
            )
            if str(tracker_state.get("pending_command_id") or "") == command_id:
                clear_pending_partial_state(tracker_state)
            registry_state = adaptive_position_registry.get(pair)
            if registry_state is not None and str(
                getattr(registry_state, "position_signature", "") or ""
            ) == signature:
                registry_state.partial_count = int(tracker_state["count"])
                if recovered_bar_index >= 0:
                    registry_state.last_partial_bar_index = recovered_bar_index
            recovered_acked += 1
        elif status == "expired" and delivered_count <= 0:
            append_tracker_command_id(
                tracker_state,
                field_name="resolved_command_ids",
                command_id=command_id,
            )
            recovered_undelivered_expired += 1
        elif not str(tracker_state.get("pending_command_id") or "").strip():
            tracker_state.update(
                {
                    "pending_command_id": command_id,
                    "pending_pair": pair,
                    "pending_position_signature": signature,
                    "pending_open_lots": float(
                        safe_float(context.get("lots_open"), 0.0)
                    ),
                    "pending_close_lots": float(
                        safe_float(
                            context.get("close_lots"),
                            row.get("lots", 0.0),
                        )
                    ),
                    "pending_submitted_ts": float(
                        safe_float(
                            context.get("submitted_ts"),
                            row.get("created_at", 0.0),
                        )
                    ),
                    "pending_bar_index": int(
                        safe_float(context.get("bar_index"), -1.0)
                    ),
                    "_missing_position_cycles": 0,
                }
            )
            recovered_pending += 1
        partial_close_tracker[signature] = tracker_state

    return {
        "durable_partial_ack_recovered_count": int(recovered_acked),
        "durable_partial_pending_recovered_count": int(recovered_pending),
        "durable_partial_undelivered_expired_count": int(
            recovered_undelivered_expired
        ),
        "durable_partial_resolved_skip_count": int(skipped_resolved),
        "durable_partial_watermark_skip_count": int(skipped_before_watermark),
    }


def hydrate_exit_command_ledger_from_commands(
    *,
    commands: list[dict[str, Any]],
    exit_command_ledger: dict[str, dict[str, Any]],
    allowed_pairs: set[str] | None = None,
    after_ts: float = 0.0,
) -> dict[str, Any]:
    """Recover queued or terminal managed exits from durable command rows."""

    allowed = {
        str(pair).strip().upper()
        for pair in set(allowed_pairs or set())
        if str(pair).strip()
    }
    recovered = 0
    skipped_resolved = 0
    skipped_before_watermark = 0
    for raw_row in list(commands or []):
        row = dict(raw_row or {})
        command_change_ts = max(
            float(safe_float(row.get("created_at"), 0.0)),
            float(safe_float(row.get("updated_at"), 0.0)),
        )
        if command_change_ts <= float(after_ts):
            skipped_before_watermark += 1
            continue
        if str(row.get("cmd") or "").strip().upper() != "CLOSE":
            continue
        payload = dict(row.get("payload_json") or row.get("payload") or {})
        context = dict(payload.get("management_context") or {})
        if str(context.get("schema") or "") != "fxstack_lifecycle_command_context_v1":
            continue
        if str(context.get("lifecycle_action") or "").strip().lower() != "exit":
            continue
        command_id = str(row.get("command_id") or payload.get("command_id") or "").strip()
        pair = str(context.get("pair") or row.get("symbol") or "").strip().upper()
        signature = str(context.get("position_signature") or "").strip()
        if (
            not command_id
            or not pair
            or not signature
            or (allowed and pair not in allowed)
        ):
            continue
        existing = dict(exit_command_ledger.get(command_id, {}) or {})
        if bool(existing.get("resolved", False)):
            skipped_resolved += 1
            continue
        if existing:
            continue
        status = str(row.get("status") or "").strip().lower()
        delivered_count = int(safe_float(row.get("delivered_count"), 0.0))
        exit_command_ledger[command_id] = {
            **context,
            "command_id": command_id,
            "resolved": bool(status == "expired" and delivered_count <= 0),
            "resolution": (
                "undelivered_expired"
                if status == "expired" and delivered_count <= 0
                else "recovered_from_durable_command"
            ),
            "resolved_ts": (
                float(safe_float(row.get("updated_at"), 0.0))
                if status == "expired" and delivered_count <= 0
                else 0.0
            ),
        }
        recovered += 1
    return {
        "durable_exit_recovered_count": int(recovered),
        "durable_exit_resolved_skip_count": int(skipped_resolved),
        "durable_exit_watermark_skip_count": int(skipped_before_watermark),
    }


def reconcile_exit_command_ledger(
    *,
    exit_command_ledger: dict[str, dict[str, Any]],
    state: dict[str, Any],
    svc: Any,
    loop_ts: float,
    position_snapshot_advanced: bool,
    position_snapshot_received_at: float,
    adaptive_recent_exit_registry: dict[str, dict[str, Any]],
    campaign_registry: dict[str, CampaignRegistryEntry],
    campaign_transition_counts: dict[str, int],
    campaign_config: Any,
    sleeve_tracker: SleeveGovernanceTracker | None = None,
) -> dict[str, Any]:
    """Commit exit-side strategy state only after broker-confirmed closure."""

    active_signatures = {
        str(compute_position_signature(dict(raw_position or {})))
        for raw_position in list(dict(state or {}).get("positions", []) or [])
    }
    confirmed = 0
    confirmed_by_absence = 0
    resolved_without_close = 0
    pending = 0
    lookup_errors = 0
    status_counts: Counter[str] = Counter()

    for command_id in list(exit_command_ledger.keys()):
        ledger_state = dict(exit_command_ledger.get(command_id, {}) or {})
        if bool(ledger_state.get("resolved", False)):
            continue
        signature = str(ledger_state.get("position_signature") or "").strip()
        expected_signatures = {
            str(item).strip()
            for item in list(ledger_state.get("position_signatures") or [])
            if str(item).strip()
        }
        if not expected_signatures and signature:
            expected_signatures = {signature}
        submitted_ts = float(safe_float(ledger_state.get("submitted_ts"), 0.0))
        command_row: dict[str, Any] = {}
        try:
            command_row = dict(svc.get_command(command_id) or {})
            command_status = str(command_row.get("status") or "missing").strip().lower()
        except Exception:
            command_status = "lookup_error"
            lookup_errors += 1
        status_counts[command_status or "missing"] += 1

        snapshot_after_submission = bool(
            position_snapshot_advanced
            and float(position_snapshot_received_at) > submitted_ts
        )
        broker_position_absent = bool(
            expected_signatures
            and not (expected_signatures & active_signatures)
            and snapshot_after_submission
        )
        confirmation_source = ""
        if command_status == "acked":
            confirmation_source = "broker_ack"
        elif broker_position_absent:
            confirmation_source = "broker_position_absent"

        if confirmation_source:
            pair = str(ledger_state.get("pair") or "").strip().upper()
            side = str(ledger_state.get("position_side") or "").strip().lower()
            playbook = str(
                ledger_state.get("playbook") or PLAYBOOK_TREND_PULLBACK
            )
            sleeve = str(
                ledger_state.get("sleeve") or playbook_to_sleeve(playbook) or ""
            )
            lifecycle_reason = str(
                ledger_state.get("lifecycle_reason") or "managed_exit"
            )
            bar_index = int(safe_float(ledger_state.get("bar_index"), -1.0))
            decision_ts = str(ledger_state.get("decision_ts") or "")
            realized_pnl_usd = float(
                safe_float(ledger_state.get("unrealized_pnl_usd"), 0.0)
            )
            age_bars = float(safe_float(ledger_state.get("age_bars"), 0.0))
            campaign_state = str(
                ledger_state.get("campaign_state") or CAMPAIGN_STATE_INACTIVE
            )
            adaptive_recent_exit_registry[pair] = {
                "bar_idx": int(bar_index),
                "side": side,
                "playbook": playbook,
                "reason": lifecycle_reason,
                "thesis_id": str(ledger_state.get("thesis_id") or ""),
                "campaign_state": campaign_state,
                "command_id": str(command_id),
                "confirmation": confirmation_source,
            }
            if campaign_config is not None:
                close_campaign = campaign_state_after_close(
                    position_state=campaign_state,
                    pair=pair,
                    side=side,
                    sleeve=sleeve,
                    row={
                        "playbook_score": float(
                            safe_float(ledger_state.get("playbook_score"), 0.0)
                        ),
                        "location_score": float(
                            safe_float(ledger_state.get("location_score"), 0.0)
                        ),
                        "trigger_score": float(
                            safe_float(ledger_state.get("trigger_score"), 0.0)
                        ),
                        "macro_coherence_score": float(
                            safe_float(
                                ledger_state.get("macro_coherence_score"), 0.0
                            )
                        ),
                        "hostility_score": float(
                            safe_float(ledger_state.get("hostility_score"), 0.0)
                        ),
                        "extension_penalty_score": float(
                            safe_float(
                                ledger_state.get("extension_penalty_score"), 0.0
                            )
                        ),
                        "environment_state": str(
                            ledger_state.get("environment_state") or ""
                        ),
                    },
                    lifecycle_reason=lifecycle_reason,
                    realized_pnl_usd=realized_pnl_usd,
                    bar_idx=bar_index,
                    ts=decision_ts,
                    config=campaign_config,
                )
                transition = campaign_transition_if_changed(
                    prior_state=campaign_state,
                    snapshot=close_campaign,
                    bar_idx=bar_index,
                    ts=decision_ts,
                    realized_pnl_usd=realized_pnl_usd,
                    holding_bars=age_bars,
                )
                if transition is not None:
                    transition_key = (
                        f"{transition.prior_state}->{transition.new_state}"
                    )
                    campaign_transition_counts[transition_key] = int(
                        campaign_transition_counts.get(transition_key, 0)
                    ) + 1
                apply_campaign_registry_snapshot(
                    campaign_registry,
                    snapshot=close_campaign,
                    bar_idx=bar_index,
                    ts=decision_ts,
                    active_position=False,
                    realized_pnl_usd=realized_pnl_usd,
                )
            if sleeve_tracker is not None:
                sleeve_tracker.record_trade(
                    sleeve=sleeve,
                    realized_pnl_usd=realized_pnl_usd,
                    holding_bars=age_bars,
                    partial_exit_events=int(
                        safe_float(ledger_state.get("partial_count"), 0.0)
                    ),
                    close_reason=lifecycle_reason,
                    session_bucket=str(ledger_state.get("session_bucket") or ""),
                    pair=pair,
                )
            ledger_state.update(
                {
                    "resolved": True,
                    "resolution": confirmation_source,
                    "resolved_ts": float(loop_ts),
                }
            )
            confirmed += 1
            confirmed_by_absence += int(
                confirmation_source == "broker_position_absent"
            )
        elif command_status == "expired" and int(
            safe_float(command_row.get("delivered_count"), 0.0)
        ) <= 0:
            ledger_state.update(
                {
                    "resolved": True,
                    "resolution": "undelivered_expired",
                    "resolved_ts": float(loop_ts),
                }
            )
            resolved_without_close += 1
        elif command_status in {
            "failed",
            "duplicate",
            "delivered",
            "reconcile_required",
            "expired",
        }:
            command_updated_at = float(
                safe_float(command_row.get("updated_at"), submitted_ts)
            )
            if (
                position_snapshot_advanced
                and float(position_snapshot_received_at) > command_updated_at
                and bool(expected_signatures & active_signatures)
            ):
                ledger_state.update(
                    {
                        "resolved": True,
                        "resolution": f"broker_position_still_open:{command_status}",
                        "resolved_ts": float(loop_ts),
                    }
                )
                resolved_without_close += 1

        if not bool(ledger_state.get("resolved", False)):
            pending += 1
        exit_command_ledger[command_id] = ledger_state

    resolved_rows = sorted(
        (
            (command_id, dict(ledger_state or {}))
            for command_id, ledger_state in exit_command_ledger.items()
            if bool(dict(ledger_state or {}).get("resolved", False))
        ),
        key=lambda item: float(safe_float(item[1].get("resolved_ts"), 0.0)),
        reverse=True,
    )
    for command_id, _ in resolved_rows[128:]:
        exit_command_ledger.pop(command_id, None)

    return {
        "exit_ack_confirmed_count": int(confirmed),
        "exit_ack_confirmed_by_absence_count": int(confirmed_by_absence),
        "exit_ack_resolved_without_close_count": int(resolved_without_close),
        "exit_ack_pending_count": int(pending),
        "exit_ack_lookup_error_count": int(lookup_errors),
        "exit_ack_status_counts": dict(sorted(status_counts.items())),
    }


def seed_adaptive_position_state(
    *,
    pair: str,
    position: dict[str, Any],
    pending_entry_registry: dict[str, dict[str, Any]],
    current_meta: dict[str, Any],
    current_row: dict[str, Any] | None,
    current_equity: float,
) -> SimpleNamespace:
    pair_key = str(pair).upper()
    seeded = dict(pending_entry_registry.pop(pair_key, {}) or {})
    row = dict(current_row or {})
    side = str(current_meta.get("position_side") or "").strip().lower()
    if side not in {"long", "short"}:
        pos_type = str(position.get("type", "")).strip()
        side = "long" if pos_type in {"0", "buy", "long"} else "short"
    position_signature = str(compute_position_signature(position))
    open_price = float(safe_float(position.get("open_price"), 0.0))
    initial_sl_price = float(
        safe_float(
            seeded.get("initial_sl_price", seeded.get("sl_price", position.get("sl", position.get("sl_price", 0.0)))),
            0.0,
        )
    )
    initial_tp_price = float(
        safe_float(
            seeded.get("initial_tp_price", seeded.get("tp_price", position.get("tp", position.get("tp_price", 0.0)))),
            0.0,
        )
    )
    return SimpleNamespace(
        pair=pair_key,
        side=str(side or "long"),
        position_signature=str(position_signature),
        open_price=float(open_price),
        current_lots=float(safe_float(position.get("lots"), seeded.get("approved_lots", 0.0))),
        initial_sl_price=float(initial_sl_price),
        initial_tp_price=float(initial_tp_price),
        initial_risk_price=float(
            abs(float(open_price) - float(initial_sl_price))
            if open_price > 0.0 and initial_sl_price > 0.0
            else 0.0
        ),
        playbook=str(seeded.get("playbook") or row.get("playbook") or current_meta.get("adaptive_playbook") or PLAYBOOK_TREND_PULLBACK),
        sleeve=str(seeded.get("sleeve") or current_meta.get("adaptive_sleeve") or playbook_to_sleeve(seeded.get("playbook") or row.get("playbook") or current_meta.get("adaptive_playbook") or PLAYBOOK_TREND_PULLBACK)),
        open_equity_usd=float(safe_float(seeded.get("open_equity_usd"), current_equity)),
        entry_trade_prob=float(safe_float(seeded.get("entry_trade_prob"), current_meta.get("trade_prob", 0.0))),
        entry_session_bucket=str(seeded.get("entry_session_bucket") or current_meta.get("session_bucket") or row.get("session_bucket") or ""),
        entry_scenario_bucket=str(seeded.get("entry_scenario_bucket") or current_meta.get("scenario_bucket") or row.get("scenario_bucket") or ""),
        entry_regime_bucket=str(seeded.get("entry_regime_bucket") or row.get("regime_bucket") or ""),
        entry_uncertainty_score=float(safe_float(seeded.get("entry_uncertainty_score"), current_meta.get("uncertainty_score", 0.0))),
        entry_structure_timing_score=float(safe_float(seeded.get("entry_structure_timing_score"), current_meta.get("structure_timing_score", 0.0))),
        pair_tier=str(seeded.get("pair_tier") or current_meta.get("pair_tier") or "tier2"),
        environment_state_at_entry=str(seeded.get("environment_state_at_entry") or row.get("environment_state") or current_meta.get("adaptive_environment_state") or ""),
        entry_location_score=float(safe_float(seeded.get("entry_location_score"), row.get("location_score", current_meta.get("adaptive_location_score", 0.0)))),
        entry_trigger_score=float(safe_float(seeded.get("entry_trigger_score"), row.get("trigger_score", current_meta.get("adaptive_trigger_score", 0.0)))),
        entry_macro_coherence_score=float(
            safe_float(seeded.get("entry_macro_coherence_score"), row.get("macro_coherence_score", current_meta.get("adaptive_macro_coherence_score", 0.0)))
        ),
        thesis_id=str(seeded.get("thesis_id") or current_meta.get("thesis_id") or build_thesis_id(pair_key, side, seeded.get("sleeve") or current_meta.get("adaptive_sleeve") or playbook_to_sleeve(seeded.get("playbook") or row.get("playbook") or current_meta.get("adaptive_playbook") or PLAYBOOK_TREND_PULLBACK))),
        campaign_state=str(seeded.get("campaign_state") or current_meta.get("campaign_state") or "probe"),
        campaign_state_reason=str(seeded.get("campaign_state_reason") or current_meta.get("campaign_state_reason") or ""),
        campaign_state_entered_bar=int(safe_float(seeded.get("campaign_state_entered_bar"), 0.0)),
        campaign_harvest_count=int(safe_float(seeded.get("campaign_harvest_count"), 0.0)),
        campaign_reattack_count=int(safe_float(seeded.get("campaign_reattack_count"), 0.0)),
        campaign_abandoned_at_bar=seeded.get("campaign_abandoned_at_bar"),
        sleeve_health_score=float(safe_float(seeded.get("sleeve_health_score"), current_meta.get("sleeve_health_score", 0.5))),
        sleeve_health_state=str(seeded.get("sleeve_health_state") or current_meta.get("sleeve_health_state") or "healthy"),
        allocator_score=float(safe_float(seeded.get("allocator_score"), current_meta.get("allocator_score", 0.0))),
        conviction_score=float(safe_float(seeded.get("conviction_score"), current_meta.get("conviction_score", 0.0))),
        conviction_band=str(seeded.get("conviction_band") or current_meta.get("conviction_band") or ""),
        thesis_stage=str(seeded.get("thesis_stage") or current_meta.get("thesis_stage") or "stand_down"),
        portfolio_posture=str(seeded.get("portfolio_posture") or current_meta.get("portfolio_posture") or "balanced_probe"),
        replacement_urgency=float(safe_float(seeded.get("replacement_urgency"), current_meta.get("replacement_urgency", 0.0))),
        aggressive_fallback_used=bool(seeded.get("aggressive_fallback_used", current_meta.get("adaptive_aggressive_fallback_used", False))),
        partial_count=int(safe_float(seeded.get("partial_count"), 0.0)),
        last_partial_bar_index=seeded.get("last_partial_bar_index"),
    )


def sync_adaptive_position_registry(
    *,
    decisions: list[dict[str, Any]],
    state: dict[str, Any],
    adaptive_rows_by_pair: dict[str, dict[str, Any]],
    adaptive_pending_entry_registry: dict[str, dict[str, Any]],
    adaptive_position_registry: dict[str, SimpleNamespace],
    current_equity: float,
    position_snapshot_authoritative: bool = True,
    absence_grace_cycles: int = _MANAGED_POSITION_ABSENCE_GRACE_CYCLES,
    partial_close_tracker: dict[str, dict[str, Any]] | None = None,
) -> None:
    positions_by_pair: dict[str, dict[str, Any]] = {}
    for raw in list(state.get("positions", []) or []):
        pos = dict(raw or {})
        pair = str(pos.get("symbol") or "").upper()
        if pair and pair not in positions_by_pair:
            positions_by_pair[pair] = pos

    active_pairs: set[str] = set(positions_by_pair)
    for decision in decisions:
        meta = dict(decision.get("metadata", {}) or {})
        pair = str(meta.get("pair") or decision.get("symbol") or "").upper()
        if not pair:
            continue
        position_open = bool(int(safe_float(meta.get("position_count_pair", 0), 0.0)) > 0 or str(meta.get("position_signature", "")).strip())
        if not position_open:
            continue
        position = dict(positions_by_pair.get(pair, {}) or {})
        if not position:
            continue
        position_signature = str(compute_position_signature(position))
        existing = adaptive_position_registry.get(pair)
        existing_signature = str(
            getattr(existing, "position_signature", "") if existing is not None else ""
        )
        if existing is not None and (
            existing_signature == position_signature or not existing_signature
        ):
            # Campaign state, entry facts, and partial-close memory belong to
            # the broker position signature.  Refresh broker-current fields
            # without rebuilding the strategy state on every poll.
            existing.position_signature = str(position_signature)
            existing.current_lots = float(
                safe_float(position.get("lots"), getattr(existing, "current_lots", 0.0))
            )
            existing.open_price = float(
                safe_float(position.get("open_price"), getattr(existing, "open_price", 0.0))
            )
            existing.current_sl_price = float(
                safe_float(
                    position.get("sl", position.get("sl_price", 0.0)),
                    getattr(existing, "current_sl_price", 0.0),
                )
            )
            existing.current_tp_price = float(
                safe_float(
                    position.get("tp", position.get("tp_price", 0.0)),
                    getattr(existing, "current_tp_price", 0.0),
                )
            )
            existing._missing_position_cycles = 0
        else:
            adaptive_position_registry[pair] = seed_adaptive_position_state(
                pair=pair,
                position=position,
                pending_entry_registry=adaptive_pending_entry_registry,
                current_meta=meta,
                current_row=adaptive_rows_by_pair.get(pair, {}),
                current_equity=float(current_equity),
            )
            adaptive_position_registry[pair]._missing_position_cycles = 0
        active_pairs.add(pair)

    for pair in list(adaptive_position_registry.keys()):
        pair_key = str(pair).upper()
        position_state = adaptive_position_registry.get(pair_key)
        if pair_key in active_pairs:
            if position_state is not None:
                position_state._missing_position_cycles = 0
            continue
        if not position_snapshot_authoritative or position_state is None:
            continue
        missing_cycles = max(
            0,
            int(safe_float(getattr(position_state, "_missing_position_cycles", 0), 0.0)),
        ) + 1
        position_state._missing_position_cycles = int(missing_cycles)
        if missing_cycles >= max(1, int(absence_grace_cycles)):
            adaptive_position_registry.pop(pair_key, None)

    tracker = dict(partial_close_tracker or {})
    for position_state in adaptive_position_registry.values():
        signature = str(
            getattr(position_state, "position_signature", "") or ""
        ).strip()
        tracker_state = dict(tracker.get(signature, {}) or {})
        if not tracker_state:
            continue
        position_state.partial_count = max(
            int(safe_float(getattr(position_state, "partial_count", 0), 0.0)),
            int(safe_float(tracker_state.get("count"), 0.0)),
        )
        tracker_bar_index = int(
            safe_float(tracker_state.get("last_partial_bar_index"), -1.0)
        )
        if tracker_bar_index >= 0:
            position_state.last_partial_bar_index = tracker_bar_index


def reconcile_partial_close_tracker(
    *,
    partial_close_tracker: dict[str, dict[str, Any]],
    adaptive_position_registry: dict[str, SimpleNamespace],
    state: dict[str, Any],
    svc: Any,
    loop_ts: float,
    settings: Any,
    position_snapshot_advanced: bool,
    position_snapshot_received_at: float,
    absence_grace_cycles: int = _MANAGED_POSITION_ABSENCE_GRACE_CYCLES,
) -> dict[str, Any]:
    """Bind partial-close accounting to broker ACKs or observed lot reduction."""

    positions_by_signature: dict[str, dict[str, Any]] = {}
    for raw_position in list(dict(state or {}).get("positions", []) or []):
        position = dict(raw_position or {})
        signature = str(compute_position_signature(position))
        if signature:
            positions_by_signature[signature] = position

    committed = 0
    observed_reduction = 0
    failed = 0
    resolved_unchanged = 0
    expired_undelivered = 0
    lookup_errors = 0
    pruned = 0
    status_counts: Counter[str] = Counter()
    lot_step = max(
        1e-9,
        float(safe_float(getattr(settings, "order_lot_step", 0.01), 0.01)),
    )
    lot_tolerance = max(1e-9, lot_step / 10.0)

    for signature in list(partial_close_tracker.keys()):
        tracker_state = dict(partial_close_tracker.get(signature, {}) or {})
        position = positions_by_signature.get(str(signature))
        pending_command_id = str(tracker_state.get("pending_command_id") or "").strip()
        command_status = ""
        command_row: dict[str, Any] = {}
        reduced_on_broker = False

        if position is not None:
            tracker_state["_missing_position_cycles"] = 0
            pending_open_lots = float(
                safe_float(tracker_state.get("pending_open_lots"), 0.0)
            )
            current_lots = float(safe_float(position.get("lots"), 0.0))
            pending_submitted_ts = float(
                safe_float(tracker_state.get("pending_submitted_ts"), 0.0)
            )
            reduced_on_broker = bool(
                pending_command_id
                and pending_open_lots > 0.0
                and current_lots < (pending_open_lots - lot_tolerance)
                and position_snapshot_advanced
                and float(position_snapshot_received_at) > pending_submitted_ts
            )

        if pending_command_id:
            try:
                command_row = dict(svc.get_command(pending_command_id) or {})
                command_status = str(command_row.get("status") or "missing").strip().lower()
            except Exception:
                command_status = "lookup_error"
                lookup_errors += 1
            status_counts[command_status or "missing"] += 1

            confirmation_source = ""
            if command_status == "acked":
                confirmation_source = "broker_ack"
            elif reduced_on_broker:
                confirmation_source = "broker_lot_reduction"

            if confirmation_source:
                tracker_state["count"] = max(
                    0, int(safe_float(tracker_state.get("count"), 0.0))
                ) + 1
                tracker_state["last_partial_ts"] = float(loop_ts)
                tracker_state["last_partial_cmd_id"] = pending_command_id
                tracker_state["last_partial_confirmation"] = confirmation_source
                append_tracker_command_id(
                    tracker_state,
                    field_name="confirmed_command_ids",
                    command_id=pending_command_id,
                )
                append_tracker_command_id(
                    tracker_state,
                    field_name="resolved_command_ids",
                    command_id=pending_command_id,
                )
                pair = str(
                    tracker_state.get("pending_pair")
                    or dict(position or {}).get("symbol")
                    or ""
                ).strip().upper()
                pending_bar_index = int(
                    safe_float(tracker_state.get("pending_bar_index"), -1.0)
                )
                if pending_bar_index >= 0:
                    tracker_state["last_partial_bar_index"] = pending_bar_index
                registry_state = adaptive_position_registry.get(pair)
                if registry_state is not None and str(
                    getattr(registry_state, "position_signature", "") or ""
                ) == str(signature):
                    registry_state.partial_count = int(tracker_state["count"])
                    if pending_bar_index >= 0:
                        registry_state.last_partial_bar_index = pending_bar_index
                clear_pending_partial_state(tracker_state)
                committed += 1
                observed_reduction += int(confirmation_source == "broker_lot_reduction")
            elif command_status == "expired" and int(
                safe_float(command_row.get("delivered_count"), 0.0)
            ) <= 0:
                append_tracker_command_id(
                    tracker_state,
                    field_name="resolved_command_ids",
                    command_id=pending_command_id,
                )
                clear_pending_partial_state(tracker_state)
                expired_undelivered += 1
            elif command_status in {
                "failed",
                "duplicate",
                "delivered",
                "reconcile_required",
                "expired",
            }:
                terminal_updated_at = float(
                    safe_float(
                        command_row.get("updated_at"),
                        tracker_state.get("pending_submitted_ts", 0.0),
                    )
                )
                if (
                    position_snapshot_advanced
                    and float(position_snapshot_received_at) > terminal_updated_at
                ):
                    append_tracker_command_id(
                        tracker_state,
                        field_name="resolved_command_ids",
                        command_id=pending_command_id,
                    )
                    tracker_state["last_partial_resolution"] = (
                        f"broker_snapshot_unchanged:{command_status}"
                    )
                    clear_pending_partial_state(tracker_state)
                    if command_status == "failed":
                        failed += 1
                    else:
                        resolved_unchanged += 1
        if position is None and position_snapshot_advanced:
            missing_cycles = max(
                0,
                int(safe_float(tracker_state.get("_missing_position_cycles"), 0.0)),
            ) + 1
            tracker_state["_missing_position_cycles"] = int(missing_cycles)
            if missing_cycles >= max(1, int(absence_grace_cycles)):
                partial_close_tracker.pop(signature, None)
                pruned += 1
                continue

        partial_close_tracker[signature] = tracker_state

    pending_count = sum(
        1
        for tracker_state in partial_close_tracker.values()
        if str(dict(tracker_state or {}).get("pending_command_id") or "").strip()
    )
    return {
        "partial_ack_committed_count": int(committed),
        "partial_ack_observed_reduction_count": int(observed_reduction),
        "partial_ack_failed_count": int(failed),
        "partial_ack_resolved_unchanged_count": int(resolved_unchanged),
        "partial_ack_expired_undelivered_count": int(expired_undelivered),
        "partial_ack_lookup_error_count": int(lookup_errors),
        "partial_ack_pending_count": int(pending_count),
        "partial_tracker_pruned_count": int(pruned),
        "partial_ack_status_counts": dict(sorted(status_counts.items())),
    }


__all__ = [
    "append_tracker_command_id",
    "clear_pending_partial_state",
    "hydrate_exit_command_ledger_from_commands",
    "hydrate_partial_close_tracker_from_commands",
    "managed_state_json_value",
    "reconcile_exit_command_ledger",
    "reconcile_partial_close_tracker",
    "restore_managed_position_state",
    "seed_adaptive_position_state",
    "serialize_managed_position_state",
    "sync_adaptive_position_registry",
]
