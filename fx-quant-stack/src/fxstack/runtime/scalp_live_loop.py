# AGENT: ROLE: Canonical exact-22 IG MT4 production-scalper runtime loop.
# AGENT: ENTRYPOINT: `run_production_scalp_loop`, selected by `runtime.runner`.
# AGENT: PRIMARY INPUTS: signed MTVCLC release, bridge readiness/ticks/M1 bars, broker state and durable commands.
# AGENT: PRIMARY OUTPUTS: exit-first strict ticket commands, risk-approved entry commands, durable cycle diagnostics.
# AGENT: STATE / SIDE EFFECTS: owns the production scalp boot/cycle state and is the only scalp entry submitter.
# AGENT: HANDSHAKES: validation -> authority -> restart join -> lifecycle -> qualification -> quote -> risk -> queue.
"""Canonical installed-runtime loop for the IG MT4 scalp strategy.

The excluded ``fxstack.scalp`` package remains research-only.  This module
composes the small production-owned primitives and deliberately has no model
manifest, research ledger, certificate issuer, or alternate command channel.
New exposure can cross the queue only after the exact 22-symbol batch, the
selected qualification mode, fresh adverse quote, broker-native risk sizing,
and the DB-owned production-scalp authority all agree. Exact-ticket protective
exits remain available after entry authority is revoked.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import hashlib
import math
import os
import time
from types import SimpleNamespace
from typing import Any
import uuid

from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION
from fxstack.data.live_quotes import (
    fetch_exact_scalp_bar_batch,
    fetch_market_ticks,
)
from fxstack.live.policy import session_bucket_from_ts
from fxstack.portfolio import evaluate_portfolio_allocation
from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.risk.sizing import account_value_per_price_unit
from fxstack.runtime.broker_contract_state import (
    AccountConversionRateProjection,
    BrokerContractUniverse,
    account_conversion_tick_symbols,
    broker_contract_sizing_metadata,
    PRODUCTION_SCALP_MAX_CASH_RISK_FRACTION,
    project_account_conversion_rates,
    project_ig_mt4_contract_universe,
    project_ig_mt4_selected_contract_universe,
)
from fxstack.runtime.governance import compute_binding_capital_governance_snapshot
from fxstack.runtime.market_source_identity import (
    AuthenticatedMarketSource,
    current_authenticated_market_source,
    market_source_row_error,
)
from fxstack.runtime.orchestration_bridge import (
    build_command_id,
    live_mode_enabled,
    stamp_orchestration_payload,
)
from fxstack.runtime.release_authority import runtime_config_sha256
from fxstack.runtime.mtvclc_cycle_capacity import (
    MTVCLCCycleSymbolDiagnostic,
    plan_mtvclc_cycle_capacity,
)
from fxstack.runtime.mtvclc_entry_qualification import (
    QualifiedMTVCLCEntryCandidate,
    qualify_mtvclc_entry_candidate,
)
from fxstack.runtime.mtvclc_entry_quote import (
    RefreshedMTVCLCEntryCandidate,
    refresh_mtvclc_entry_quote,
)
from fxstack.runtime.scalp_execution_boundary import (
    ScalpBrokerEntryPlan,
    ScalpBrokerEntryCostModel,
    build_scalp_broker_entry_plan,
)
from fxstack.runtime.scalp_execution_authority import (
    SCALP_ENTRY_INTENT,
    SCALP_EXECUTION_LANE,
    SCALP_SLEEVE,
)
from fxstack.runtime.scalp_position_lifecycle import (
    TICKET_OWNER_CONTRACT,
    evaluate_scalp_position_lifecycle,
)
from fxstack.runtime.mtvclc_proposal_batch import (
    M1_SECONDS,
    MTVCLC_RUNTIME_PROFILE_ID,
    MTVCLCProposalBatchResult,
    MTVCLCSymbolProposalDiagnostic,
    evaluate_mtvclc_profile_batch,
)
from fxstack.runtime.scalp_restart_reconciliation import (
    ScalpRestartReconciliationResult,
    reconcile_scalp_restart,
)
from fxstack.runtime.scalp_rollover_guard import (
    DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY,
    PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION,
    evaluate_production_scalp_rollover_guard,
)
from fxstack.runtime.scalp_runtime_admission import (
    ScalpRuntimeAdmission,
    verify_configured_scalp_runtime_admission,
)
from fxstack.runtime.scalp_runtime_control import (
    ScalpAuthorityActivationResult,
    build_scalp_runtime_attestation,
    ensure_production_scalp_authority,
    ensure_production_scalp_protective_management_egress,
    revoke_production_scalp_authority,
    scalp_live_command_admission,
)
from fxstack.strategy.mtvclc import (
    FROZEN_MTVCLC_POLICY,
    MT4_BID_PRICE_BASIS,
    MT4_IVOLUME_SOURCE,
    TIME_STOP_M1_BARS,
)


SCALP_LIVE_LOOP_SCHEMA = "fxstack.production_scalp_live_loop.v2"
_MAX_DURABLE_COMMAND_ROWS = 5000


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _parse_bar_epoch(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = _finite(value, -1.0)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=UTC)
        parsed = parsed_dt.timestamp()
    if parsed < 0.0 or int(parsed) % M1_SECONDS:
        return None
    return int(parsed)


def _parse_timestamp_epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = _finite(value, -1.0)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=UTC)
        parsed = parsed_dt.timestamp()
    return parsed if parsed >= 0.0 and math.isfinite(parsed) else None


def _scalp_market_ready_snapshot(
    *,
    state: Mapping[str, Any],
    now_epoch: float,
    tick_scope_reasons: tuple[str, ...],
) -> dict[str, Any]:
    """Build the entry-lane health view without the fat ops-ready report."""

    heartbeat_epoch = _parse_timestamp_epoch(state.get("last_heartbeat"))
    stale_after = max(
        1.0,
        _finite(state.get("heartbeat_stale_after_secs"), 30.0),
    )
    heartbeat_age = (
        max(0.0, float(now_epoch) - heartbeat_epoch)
        if heartbeat_epoch is not None
        else math.inf
    )
    mt4_fresh = bool(
        str(state.get("system_status") or "").strip().lower() == "connected"
        and heartbeat_age <= stale_after
    )
    ticks_fresh = not tick_scope_reasons
    reason = (
        "ok"
        if mt4_fresh and ticks_fresh
        else ("mt4_heartbeat_stale" if not mt4_fresh else "tick_feed_stale")
    )
    return {
        "status": "ok" if mt4_fresh and ticks_fresh else "degraded",
        "reason": reason,
        "bridge_up": True,
        "mt4_fresh": mt4_fresh,
        "ticks_fresh": ticks_fresh,
        "heartbeat_age_secs": heartbeat_age if math.isfinite(heartbeat_age) else None,
        "heartbeat_stale_after_secs": stale_after,
    }


def _signal_bar_receipt_missing_symbols(
    bars_by_symbol: Mapping[str, list[dict[str, Any]]],
    *,
    signal_minute_epoch: int,
) -> tuple[str, ...]:
    """Return symbols whose just-closed direct-MT4 shift-1 bar is unavailable."""

    close_epoch = int(signal_minute_epoch) + M1_SECONDS
    missing: list[str] = []
    for symbol in IG_MT4_SCALP_SYMBOLS:
        ready = False
        for row in list(bars_by_symbol.get(symbol) or []):
            if not isinstance(row, Mapping):
                continue
            if _parse_bar_epoch(row.get("time", row.get("ts"))) != int(
                signal_minute_epoch
            ):
                continue
            receipt = _finite(row.get("received_at_epoch"), -1.0)
            ready = (
                row.get("volume_source") == MT4_IVOLUME_SOURCE
                and row.get("price_basis") == MT4_BID_PRICE_BASIS
                and close_epoch <= receipt
            )
            if ready:
                break
        if not ready:
            missing.append(symbol)
    return tuple(missing)


def _symbol_decision_context(
    *,
    entry_global_reasons: list[str],
    entry_resource_reasons: tuple[str, ...] = (),
    proposal: Any,
    proposal_diagnostic: MTVCLCSymbolProposalDiagnostic | None,
    qualification_diagnostic: Mapping[str, Any],
    capacity_diagnostic: MTVCLCCycleSymbolDiagnostic | None,
) -> dict[str, Any]:
    """Project one symbol's abstention layer without creating a global gate."""

    if entry_global_reasons:
        reasons = tuple(str(reason) for reason in entry_global_reasons if str(reason))
    elif entry_resource_reasons:
        reasons = tuple(str(reason) for reason in entry_resource_reasons if str(reason))
    elif proposal_diagnostic is not None and not proposal_diagnostic.structural_ready:
        reasons = tuple(
            reason
            for reason in (
                capacity_diagnostic.refusal_reasons
                if capacity_diagnostic is not None
                else ()
            )
            if str(reason).startswith("structural_unready")
        ) or tuple(
            f"structural_unready:{reason}"
            for reason in proposal_diagnostic.structural_reasons
        )
    elif proposal is not None and proposal.reasons:
        reasons = tuple(str(reason) for reason in proposal.reasons if str(reason))
    else:
        qualification_reasons = tuple(
            str(reason)
            for reason in list(qualification_diagnostic.get("reasons") or [])
            if str(reason)
        )
        if qualification_reasons:
            reasons = qualification_reasons
        elif proposal_diagnostic is not None and proposal_diagnostic.evaluation_reasons:
            reasons = tuple(proposal_diagnostic.evaluation_reasons)
        else:
            reasons = tuple(
                capacity_diagnostic.refusal_reasons
                if capacity_diagnostic is not None
                else ()
            )

    return {
        "reasons": list(dict.fromkeys(reasons)),
        "proposal_batch_symbol_diagnostic": (
            asdict(proposal_diagnostic) if proposal_diagnostic is not None else {}
        ),
        "capacity_symbol_diagnostic": (
            asdict(capacity_diagnostic) if capacity_diagnostic is not None else {}
        ),
    }


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


def _diagnostic_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    serializer = getattr(value, "to_dict", None)
    if callable(serializer):
        serialized = serializer()
        return dict(serialized) if isinstance(serialized, Mapping) else {}
    try:
        return dict(vars(value))
    except (TypeError, ValueError):
        return {}


def _production_scalp_cash_risk_error(
    approved_order: Mapping[str, Any] | None,
    *,
    equity: float,
) -> str:
    """Defend the strategy seam if a future risk adapter drops the equity cap."""

    approved = dict(approved_order or {})
    proof = approved.get("broker_contract_sizing")
    if not isinstance(proof, Mapping):
        return "production_scalp_cash_risk_proof_missing"
    money_at_risk = _finite(dict(proof).get("money_at_risk"), -1.0)
    hard_cash_cap = _finite(equity, 0.0) * float(
        PRODUCTION_SCALP_MAX_CASH_RISK_FRACTION
    )
    if not (
        hard_cash_cap > 0.0
        and 0.0 < money_at_risk <= hard_cash_cap + 1e-9
    ):
        return "production_scalp_cash_risk_cap_invalid"
    return ""


def _is_live(settings: Any) -> bool:
    return bool(live_mode_enabled(settings))


def _protective_management_egress_active(
    state: Mapping[str, Any],
    *,
    runtime_boot_id: str,
) -> bool:
    egress = dict(state.get("execution_egress_authority") or {})
    runtime_diag = dict(state.get("runtime_diag") or {})
    live = dict(runtime_diag.get("orchestration_live") or {})
    return bool(
        state.get("execution_egress_enabled") is True
        and egress.get("enabled") is True
        and egress.get("protective_management_only") is True
        and str(egress.get("runtime_boot_id") or "").strip()
        == str(runtime_boot_id or "").strip()
        and set(egress.get("pair_scope") or []) == set(IG_MT4_SCALP_SYMBOLS)
        and set(egress.get("sleeve_scope") or []) == {SCALP_SLEEVE}
        and set(egress.get("intent_scope") or []) == {"exit"}
        and bool(live.get("enabled", False))
        and str(live.get("mode") or "").strip().lower() == "live"
        and bool(live.get("runtime_enabled", False))
        and not bool(live.get("queue_kill_active", False))
    )


def _runtime_service(settings: Any) -> Any:
    from fxstack.runtime.service import RuntimeService

    return RuntimeService(
        database_url=settings.database_url,
        default_session_id=settings.default_session_id,
        command_ttl_secs=settings.command_ttl_secs,
        requeue_age_secs=settings.startup_requeue_age_secs,
        db_connect_retries=settings.db_connect_retries,
    )


def _startup_log(message: str) -> None:
    print(f"[fxstack-scalp-runtime] {message}", flush=True)


def _boot_state(
    *, runtime_boot_id: str, phase: str, now_epoch: float
) -> dict[str, Any]:
    return {
        "boot_id": str(runtime_boot_id),
        "booted_at": datetime.fromtimestamp(now_epoch, UTC).isoformat(),
        "runtime_pid": int(os.getpid()),
        "phase": str(phase),
        "phase_pair": "",
        "phase_index": 0,
        "phase_total": len(IG_MT4_SCALP_SYMBOLS),
        "last_progress_ts": float(now_epoch),
        "failure_component": "",
        "failure_pair": "",
        "failure_reason": "",
        "failed_at": "",
        "pending_command_policy": "purge_queued_quarantine_delivered",
    }


def _account_conversion_tick_symbols(account_currency: Any) -> tuple[str, ...]:
    """Return market-data-only direct/inverse crosses needed for cash sizing."""

    return account_conversion_tick_symbols(
        account_currency,
        required_symbols=IG_MT4_SCALP_SYMBOLS,
    )


def _cycle_account_conversion_projection(
    ticks: Any,
    *,
    account_currency: Any,
    allowed_market_data_symbols: Any = (),
    required_symbols: Any = IG_MT4_SCALP_SYMBOLS,
) -> AccountConversionRateProjection:
    """Project rates only from exact or explicitly conversion-only ticks."""

    allowed = set(IG_MT4_SCALP_SYMBOLS)
    allowed.update(
        str(item or "").strip().upper()
        for item in list(allowed_market_data_symbols or [])
        if str(item or "").strip()
    )
    scoped_ticks: dict[str, dict[str, Any]] = {}
    if isinstance(ticks, Mapping):
        for raw_symbol, raw_tick in ticks.items():
            symbol = str(raw_symbol or "").strip().upper()
            if symbol not in allowed or not isinstance(raw_tick, Mapping):
                continue
            scoped_ticks[symbol] = dict(raw_tick)
    return project_account_conversion_rates(
        scoped_ticks,
        account_currency=account_currency,
        required_symbols=tuple(required_symbols or ()),
        require_market_event_fresh=True,
    )


def _aggregate_broker_contract_global_errors(
    universe: BrokerContractUniverse,
) -> tuple[str, ...]:
    """Separate common account/snapshot failures from exact-row failures."""

    symbol_suffixes = tuple(f":{symbol}" for symbol in IG_MT4_SCALP_SYMBOLS)
    return tuple(
        error for error in universe.errors if not str(error).endswith(symbol_suffixes)
    )


def _aggregate_account_conversion_global_errors(
    projection: AccountConversionRateProjection,
) -> tuple[str, ...]:
    """Only account identity is global; quote coverage is symbol-scoped."""

    return tuple(
        error
        for error in projection.errors
        if error == "scalp_account_conversion_account_currency_invalid"
    )


def _symbol_market_tick_errors(
    ticks: Mapping[str, Any],
    *,
    symbol: str,
) -> tuple[str, ...]:
    """Project the current per-symbol quote/event gate before capacity ranking."""

    pair = str(symbol or "").strip().upper()
    raw_tick = ticks.get(pair)
    if not isinstance(raw_tick, Mapping):
        return (f"scalp_market_tick_missing:{pair}",)
    bid = _finite(raw_tick.get("bid"))
    ask = _finite(raw_tick.get("ask"))
    reasons: list[str] = []
    if bid <= 0.0 or ask <= 0.0 or ask < bid:
        reasons.append(f"scalp_market_tick_invalid:{pair}")
    if raw_tick.get("market_event_fresh") is not True:
        reasons.append(f"scalp_market_event_not_fresh:{pair}")
    return tuple(reasons)


def _exact_tick_scope(
    ticks: Any,
    *,
    allowed_market_data_symbols: Any = (),
    expected_market_source: AuthenticatedMarketSource | None = None,
    require_authenticated_source: bool = False,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    if require_authenticated_source and expected_market_source is None:
        return {}, ("scalp_market_source_unattested",)
    if not isinstance(ticks, Mapping):
        return {}, ("scalp_tick_universe_invalid",)
    normalized: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    invalid_keys = False
    for raw_symbol, raw_tick in ticks.items():
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or not isinstance(raw_tick, Mapping):
            invalid_keys = True
            continue
        if expected_market_source is not None:
            source_error = market_source_row_error(
                raw_tick,
                expected=expected_market_source,
            )
            if source_error:
                invalid_keys = True
                continue
        if symbol in normalized:
            duplicates.add(symbol)
        normalized[symbol] = dict(raw_tick)
    reasons: list[str] = []
    if invalid_keys:
        reasons.append("scalp_tick_row_invalid")
    reasons.extend(f"scalp_tick_symbol_duplicate:{item}" for item in sorted(duplicates))
    expected = set(IG_MT4_SCALP_SYMBOLS)
    allowed_extras = {
        str(item or "").strip().upper()
        for item in list(allowed_market_data_symbols or [])
        if str(item or "").strip()
    }
    observed = set(normalized)
    reasons.extend(
        f"scalp_tick_symbol_missing:{item}"
        for item in IG_MT4_SCALP_SYMBOLS
        if item not in observed
    )
    reasons.extend(
        f"scalp_tick_symbol_extra:{item}"
        for item in sorted(observed - expected - allowed_extras)
    )
    return (
        {
            symbol: normalized[symbol]
            for symbol in IG_MT4_SCALP_SYMBOLS
            if symbol in normalized
        },
        tuple(dict.fromkeys(reasons)),
    )


def _fetch_exact_m1_bars(
    *,
    settings: Any,
    limit: int,
    expected_market_source: AuthenticatedMarketSource | None = None,
    require_authenticated_source: bool = False,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Fetch all symbols atomically without shrinking the scope."""

    bars = _empty_exact_m1_bar_scope()
    errors: dict[str, str] = {}
    if require_authenticated_source and expected_market_source is None:
        return bars, {
            symbol: "bar_market_source_unattested" for symbol in IG_MT4_SCALP_SYMBOLS
        }

    rows_by_symbol = fetch_exact_scalp_bar_batch(
        settings.mt4_bridge_url,
        timeframe="M1",
        limit=limit,
        provider="mt4_bridge",
        settings=settings,
    )
    if tuple(rows_by_symbol) != IG_MT4_SCALP_SYMBOLS:
        return bars, {
            symbol: "bar_batch_scope_not_exact_ordered_22"
            for symbol in IG_MT4_SCALP_SYMBOLS
        }
    for symbol in IG_MT4_SCALP_SYMBOLS:
        rows = list(rows_by_symbol.get(symbol) or [])
        bars[symbol] = [dict(row) for row in rows if isinstance(row, Mapping)]
        if len(bars[symbol]) != len(rows):
            errors[symbol] = "bar_row_invalid"
            continue
        if expected_market_source is not None:
            source_errors = {
                market_source_row_error(
                    row,
                    expected=expected_market_source,
                )
                for row in bars[symbol]
            }
            source_errors.discard("")
            if source_errors:
                bars[symbol] = []
                errors[symbol] = "bar_" + sorted(source_errors)[0]
    return bars, errors


def _empty_exact_m1_bar_scope() -> dict[str, list[dict[str, Any]]]:
    """Keep the evaluator's ordered universe exact while withholding stale input."""

    return {symbol: [] for symbol in IG_MT4_SCALP_SYMBOLS}


def _merge_m1_bar_history(
    history: dict[str, list[dict[str, Any]]],
    updates: Mapping[str, list[dict[str, Any]]],
    *,
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    """Merge an exact-scope M1 tail into the runtime-owned warm history."""

    retained_limit = max(1, int(limit))
    merged_scope = _empty_exact_m1_bar_scope()
    for symbol in IG_MT4_SCALP_SYMBOLS:
        by_epoch: dict[int, dict[str, Any]] = {}
        for rows in (history.get(symbol) or (), updates.get(symbol) or ()):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                epoch = _parse_bar_epoch(row.get("time", row.get("ts")))
                if epoch is not None:
                    by_epoch[epoch] = dict(row)
        merged_scope[symbol] = [
            by_epoch[epoch]
            for epoch in sorted(by_epoch)[-retained_limit:]
        ]
    history.clear()
    history.update(merged_scope)
    return history


def _m1_bar_history_is_warm(
    history: Mapping[str, list[dict[str, Any]]],
    *,
    limit: int,
) -> bool:
    required = max(1, int(limit) - 1)
    return tuple(history) == IG_MT4_SCALP_SYMBOLS and all(
        len(history.get(symbol) or ()) >= required
        for symbol in IG_MT4_SCALP_SYMBOLS
    )


def _state_gate_reasons(
    *,
    state: Mapping[str, Any],
    settings: Any,
    ready: Mapping[str, Any],
    tick_scope_reasons: tuple[str, ...],
    magic: int,
) -> tuple[str, ...]:
    reasons: list[str] = list(tick_scope_reasons)
    if str(state.get("broker_venue_id") or "").strip().lower() != IG_MT4_VENUE_ID:
        reasons.append("scalp_broker_venue_unattested")
    account_mode = str(state.get("broker_account_mode") or "").strip().lower()
    expected_mode = (
        str(getattr(settings, "live_expected_account_mode", "") or "").strip().lower()
    )
    normalized_expected_mode = "real" if expected_mode == "live" else expected_mode
    if account_mode not in {"demo", "real"}:
        reasons.append("scalp_broker_account_mode_unattested")
    elif normalized_expected_mode and account_mode != normalized_expected_mode:
        reasons.append("scalp_broker_account_mode_mismatch")
    if not str(state.get("broker_account_scope") or "").strip():
        reasons.append("scalp_broker_account_scope_unattested")
    if magic <= 0:
        reasons.append("scalp_broker_account_magic_unattested")
    if str(state.get("system_status") or "").strip().lower() != "connected":
        reasons.append("scalp_broker_heartbeat_disconnected")
    if ready.get("mt4_fresh") is not True:
        reasons.append("scalp_bridge_heartbeat_stale")
    if ready.get("ticks_fresh") is not True:
        reasons.append("scalp_bridge_ticks_stale")
    return tuple(dict.fromkeys(reasons))


def _qualified_batch(
    batch: MTVCLCProposalBatchResult,
    *,
    admission: ScalpRuntimeAdmission,
    as_of_epoch: float,
) -> tuple[
    MTVCLCProposalBatchResult,
    dict[str, QualifiedMTVCLCEntryCandidate],
    dict[str, dict[str, Any]],
]:
    qualified: dict[str, QualifiedMTVCLCEntryCandidate] = {}
    diagnostics: dict[str, dict[str, Any]] = {}
    for proposal in batch.proposals:
        admitted_cost = admission.cost_calibration_for(proposal.symbol)
        if admitted_cost is None:
            diagnostics[proposal.symbol] = {
                "qualified": False,
                "reasons": ["mtvclc_qualification_admitted_cost_missing"],
                "schema_version": "",
            }
            continue
        result = qualify_mtvclc_entry_candidate(
            proposal,
            admission.verification,
            admitted_cost,
            as_of_epoch=as_of_epoch,
        )
        diagnostics[proposal.symbol] = {
            "qualified": bool(result.qualified),
            "reasons": list(result.reasons),
            "schema_version": str(result.schema_version),
        }
        if result.qualified_candidate is not None and result.qualified:
            qualified[proposal.symbol] = result.qualified_candidate
    return (
        replace(
            batch,
            proposals=tuple(
                proposal for proposal in batch.proposals if proposal.symbol in qualified
            ),
        ),
        qualified,
        diagnostics,
    )


def _effective_cycle_cap(*, settings: Any, occupied_count: int) -> int:
    configured = int(getattr(settings, "max_new_entries_per_cycle", 0) or 0)
    if configured > 0:
        return configured
    total = max(0, int(getattr(settings, "max_total_positions", 0) or 0))
    return max(0, total - max(0, occupied_count))


def _owner_token(*, generation_id: str, command_id: str, magic: int) -> str:
    material = f"{generation_id}|{command_id}|{magic}".encode("utf-8")
    # Keep the immutable ownership prefix short enough for a broker-added
    # OrderComment suffix; restart joins require this prefix plus ticket,
    # Magic, symbol, and authenticated snapshot identity.
    return "fxs-s-" + hashlib.sha256(material).hexdigest()[:16]


def _entry_orchestration(
    *,
    symbol: str,
    cycle_id: str,
    command_id: str,
    generation_id: str,
    live: bool,
) -> dict[str, Any]:
    seed = f"{generation_id}|{cycle_id}|{symbol}|{command_id}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return {
        "enabled": True,
        "agent_mode": "live" if live else "shadow",
        "schema_version": SCALP_LIVE_LOOP_SCHEMA,
        "cycle_id": str(cycle_id),
        "run_id": f"scalp-{digest[:24]}",
        "trace_id": f"scalp-trace-{digest[:32]}",
        "correlation_id": f"scalp-corr-{digest[:32]}",
        "thread_id": f"scalp:{symbol}",
        "pair": str(symbol),
        "fallback_used": False,
        "fault_classification": "",
        "shadow_action": "enter",
        "governed_selected_action": "enter",
        "approval_state": "auto",
        "divergence_reason": "",
    }


def _rollout_policy(
    *, settings: Any, symbol: str, admission: ScalpRuntimeAdmission
) -> dict[str, Any]:
    allowed = bool(
        _is_live(settings)
        and admission.valid
        and symbol in set(getattr(settings, "agent_live_pair_allowlist", []) or [])
    )
    budget_scale = max(
        0.0,
        min(
            1.0,
            _finite(
                getattr(settings, "capital_rollout_budget_scale_full_risk", 1.0),
                1.0,
            ),
        ),
    )
    return {
        "configured": allowed,
        "enabled": allowed,
        "active": allowed,
        "mode": "live" if allowed else "off",
        "pair": symbol,
        "pair_allowlisted": allowed,
        "allowlisted_pairs": [symbol] if allowed else [],
        "budget_scale": budget_scale if allowed else 0.0,
        "source": "external_scalp_validation",
    }


def _governance_policy(*, state: Mapping[str, Any], settings: Any) -> dict[str, Any]:
    configured = bool(getattr(settings, "capital_governance_enabled", False))
    persisted = state.get("governance")
    if configured and isinstance(persisted, Mapping):
        return dict(persisted)
    if configured:
        return {
            "mode": "missing",
            "capital_band": "shadow_only",
            "budget_scale": 0.0,
            "paused": True,
            "shadow_only": True,
            "reasons": ["capital_governance_state_missing"],
        }
    return {
        "mode": "normal",
        "capital_band": "full_risk_live",
        "budget_scale": 1.0,
        "paused": False,
        "entries_only": False,
        "shadow_only": False,
        "reasons": [],
    }


def _positions_with_current_contract_value(
    *,
    positions: Any,
    contract_universe: BrokerContractUniverse,
    quote_rates: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Price open-position stop risk with the current broker contract truth."""

    out: list[dict[str, Any]] = []
    for item in list(positions or []):
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        row.pop("value_per_price_unit", None)
        symbol = str(row.get("symbol") or row.get("pair") or "").strip().upper()
        contract = contract_universe.contract_for(symbol)
        if contract is not None:
            value = account_value_per_price_unit(
                pair=symbol,
                rates=dict(quote_rates or {}),
                account_currency=contract_universe.account_currency,
                contract_units=contract.lot_size,
            )
            if value > 0.0:
                row["value_per_price_unit"] = float(value)
        out.append(row)
    return out


def _binding_governance_policy(
    *,
    service: Any,
    state: Mapping[str, Any],
    settings: Any,
    contract_universe: BrokerContractUniverse,
    quote_rates: Mapping[str, Any],
    ready: Mapping[str, Any],
    computed_at: float,
    max_source_age_secs: float,
) -> dict[str, Any]:
    """Compute the one cycle governance contract before any entry approval."""

    positions = _positions_with_current_contract_value(
        positions=state.get("positions"),
        contract_universe=contract_universe,
        quote_rates=quote_rates,
    )
    try:
        allocation = evaluate_portfolio_allocation(
            symbol=IG_MT4_SCALP_SYMBOLS[0],
            session_bucket="",
            expected_edge_bps=0.0,
            uncertainty_score=0.0,
            positions=positions,
            pending_entries=[],
            max_total_positions=int(settings.max_total_positions),
            max_pair_positions=int(settings.max_pair_positions),
            governance={},
            corr_mode=str(getattr(settings, "portfolio_corr_mode", "heuristic")),
            realized_returns_by_pair=None,
            corr_window_bars=int(
                getattr(settings, "portfolio_realized_corr_window_bars", 0) or 0
            ),
            corr_min_obs=int(
                getattr(settings, "portfolio_realized_corr_min_obs", 0) or 0
            ),
        )
        portfolio_telemetry = dict(allocation.telemetry)
    except Exception as exc:
        portfolio_telemetry = {
            "numeric_inputs_valid": False,
            "book_numeric_inputs_valid": False,
            "book_numeric_input_errors": [
                f"production_scalp_portfolio_snapshot:{type(exc).__name__}"
            ],
            "concentration": {},
            "correlation": {},
            "budget": {},
            "stress": {},
        }
    runtime_diag = dict(state.get("runtime_diag") or {})
    return compute_binding_capital_governance_snapshot(
        settings=settings,
        runtime_diag={
            "loop_latency_ms": float(_finite(runtime_diag.get("loop_latency_ms"))),
            # This strategy consumes versioned bridge bars, not the model
            # feature-serving tail.  Do not inherit stale model-runtime state.
            "feature_serving": {
                "stale": False,
                "details": {
                    "selected_pairs_count": len(IG_MT4_SCALP_SYMBOLS),
                    "selected_stale_count": 0,
                },
            },
            "risk_cycle_summary": dict(runtime_diag.get("risk_cycle_summary") or {}),
            "current_equity": float(_finite(state.get("equity"))),
        },
        metrics=dict(service.get_metrics() or {}),
        portfolio_telemetry=portfolio_telemetry,
        provider_health={IG_MT4_VENUE_ID: dict(ready or {})},
        previous_governance=dict(state.get("governance") or {}),
        previous_cycle_ts=state.get("runtime_last_cycle_ts"),
        computed_at=float(computed_at),
        max_source_age_secs=float(max_source_age_secs),
    )


def _risk_entry(
    *,
    settings: Any,
    state: dict[str, Any],
    candidate: RefreshedMTVCLCEntryCandidate,
    broker_entry_plan: ScalpBrokerEntryPlan,
    tick: dict[str, Any],
    contract_universe: BrokerContractUniverse,
    quote_rates: dict[str, float],
    authority: dict[str, Any],
    runtime_boot_id: str,
    cycle_id: str,
    admission: ScalpRuntimeAdmission,
    pending_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Invoke the existing canonical risk kernel; never reimplement sizing."""

    from fxstack.runtime import runner as runtime_runner

    proposal = candidate.proposal
    symbol = candidate.symbol
    side = candidate.side
    minute_epoch = int(proposal.signal_epoch or 0)
    ts_value = datetime.fromtimestamp(minute_epoch, UTC).isoformat()
    command_id = build_command_id(
        pair=symbol,
        ts_value=ts_value,
        action_tag="entry",
    )
    magic = _positive_int(state.get("broker_account_magic"))
    owner_token = _owner_token(
        generation_id=admission.verification.generation_id,
        command_id=command_id,
        magic=magic,
    )
    sizing = broker_contract_sizing_metadata(
        contract_universe,
        symbol=symbol,
        margin_utilization_cap=float(settings.production_scalp_margin_utilization_cap),
        quote_rates=quote_rates,
    )
    equity = _finite(state.get("equity"), 0.0)
    production_scalp_cash_risk_cap = equity * float(
        PRODUCTION_SCALP_MAX_CASH_RISK_FRACTION
    )
    sizing.update(
        {
            "command_id": command_id,
            "magic": magic,
            "owner_token": owner_token,
            "ownership_contract": TICKET_OWNER_CONTRACT,
            "strategy_lane": SCALP_EXECUTION_LANE,
            "intent": SCALP_ENTRY_INTENT,
            "sleeve": SCALP_SLEEVE,
            "adaptive_sleeve": SCALP_SLEEVE,
            "strategy_id": proposal.strategy_id,
            "strategy_version": proposal.strategy_version,
            "strategy_config_sha256": proposal.config_sha256,
            "strategy_generation_id": admission.verification.generation_id,
            "scalp_admission_mode": str(admission.verification.admission_mode),
            "qualification_probability_source": str(candidate.probability_source),
            "strategy_validation_evidence_sha256": (
                admission.verification.certificate_sha256
            ),
            "rollover_guard_schema_version": (
                PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION
            ),
            "rollover_guard_config_sha256": (
                DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY.config_sha256()
            ),
            "proposal_minute_epoch": minute_epoch,
            "proposal_signal_epoch": minute_epoch,
            "time_stop_bars": int(proposal.time_stop_bars or 0),
            "entry_quote_timestamp_epoch": float(candidate.quote_timestamp_epoch),
            "entry_price": float(broker_entry_plan.worst_fill_price),
            "sl_price": float(broker_entry_plan.sl_price),
            "tp_price": float(broker_entry_plan.tp_price),
            "stop_distance": float(broker_entry_plan.stop_distance_price),
            "live_p_star": float(broker_entry_plan.live_p_star),
            "current_spread_bps": float(candidate.current_spread_bps),
            "adverse_slippage_bps": float(candidate.adverse_slippage_bps),
            "production_scalp_cash_risk_cap_account_ccy": float(
                production_scalp_cash_risk_cap
            ),
        }
    )
    sizing.update(broker_entry_plan.command_fields())
    positions_all = _positions_with_current_contract_value(
        positions=state.get("positions"),
        contract_universe=contract_universe,
        quote_rates=quote_rates,
    )
    pair_positions = [
        item
        for item in positions_all
        if str(item.get("symbol") or "").strip().upper() == symbol
    ]
    governance = _governance_policy(state=state, settings=settings)
    paused = bool(
        governance.get("paused")
        or governance.get("shadow_only")
        or governance.get("entries_only")
    )
    rollout = _rollout_policy(settings=settings, symbol=symbol, admission=admission)
    signal = SimpleNamespace(
        trade_prob=float(candidate.win_probability_lower_bound),
        uncertainty_score=float(1.0 - candidate.win_probability_lower_bound),
        session_bucket=session_bucket_from_ts(ts_value),
        session_entry_blocked=False,
        reversal_ready=False,
    )
    risk_out = runtime_runner._evaluate_runtime_risk_kernel(
        pair=symbol,
        ts_value=ts_value,
        side=side,
        signal=signal,
        expected_edge_bps=float(broker_entry_plan.conservative_expected_edge_bps),
        spread_bps=float(candidate.current_spread_bps),
        feature_bar={
            "stale": False,
            "age_secs": float(candidate.quote_age_secs),
            "stale_after_secs": float(settings.bridge_stale_tick_secs),
            "reason": "fresh_ig_mt4_market_event",
        },
        tick=tick,
        spread_unit_source="canonical_bid_ask",
        mt4_fresh=True,
        ticks_fresh=True,
        paused=paused,
        positions=pair_positions,
        portfolio_positions=positions_all,
        pair_count=len(pair_positions),
        total_count=len(positions_all),
        current_equity=equity,
        planned_entry_lots=0.0,
        lifecycle_action="entry",
        lifecycle_reason=str(candidate.probability_source),
        lifecycle_action_score=float(candidate.win_probability_lower_bound),
        close_lots=0.0,
        sl_price=float(broker_entry_plan.sl_price),
        tp_price=float(broker_entry_plan.tp_price),
        rejection_reasons=[],
        state=state,
        settings=settings,
        rollout_policy=rollout,
        governance_policy=governance,
        pending_entries=pending_entries,
        realized_returns_by_pair=None,
        quote_rates=quote_rates,
        entry_size_scale=1.0,
        broker_contract_metadata=sizing,
        entry_cash_risk_cap=float(production_scalp_cash_risk_cap),
    )
    approved = dict(risk_out.get("approved_order") or {})
    if approved and str(risk_out.get("verdict") or "") == "allow":
        cash_risk_error = _production_scalp_cash_risk_error(
            approved,
            equity=equity,
        )
        if cash_risk_error:
            proof = approved.get("broker_contract_sizing")
            money_at_risk = (
                _finite(dict(proof).get("money_at_risk"), -1.0)
                if isinstance(proof, Mapping)
                else -1.0
            )
            blocked = dict(risk_out)
            blocked["approved_order"] = None
            blocked["verdict"] = "block"
            blocked["reason"] = cash_risk_error
            blocked["trace"] = [
                *list(risk_out.get("trace") or []),
                {
                    "rule": "production_scalp_cash_risk_cap",
                    "allowed": False,
                    "reason": cash_risk_error,
                    "details": {
                        "money_at_risk": money_at_risk,
                        "hard_cap": float(production_scalp_cash_risk_cap),
                        "hard_cap_fraction": float(
                            PRODUCTION_SCALP_MAX_CASH_RISK_FRACTION
                        ),
                    },
                },
            ]
            return blocked
    return risk_out


def _submit_entry(
    *,
    service: Any,
    settings: Any,
    state: dict[str, Any],
    risk_out: dict[str, Any],
    candidate: RefreshedMTVCLCEntryCandidate,
    authority: dict[str, Any],
    runtime_boot_id: str,
    cycle_id: str,
    admission: ScalpRuntimeAdmission,
) -> dict[str, Any]:
    approved = dict(risk_out.get("approved_order") or {})
    symbol = candidate.symbol
    side = candidate.side
    command_id = str(approved.get("command_id") or "")
    orchestration = _entry_orchestration(
        symbol=symbol,
        cycle_id=cycle_id,
        command_id=command_id,
        generation_id=admission.verification.generation_id,
        live=True,
    )
    live_authority = dict(
        dict(state.get("runtime_diag") or {}).get("orchestration_live") or {}
    )
    payload = stamp_orchestration_payload(
        payload=approved,
        orchestration=orchestration,
        live_authority=live_authority,
        sleeve=SCALP_SLEEVE,
    )
    from fxstack.runtime.service import FinalEntryApproval

    governance = dict(risk_out.get("governance") or {})
    rollout = dict(risk_out.get("rollout") or {})
    governed_allowed = bool(
        not governance.get("paused")
        and not governance.get("shadow_only")
        and not governance.get("entries_only")
    )
    approval = FinalEntryApproval(
        pair=symbol,
        side=side,
        risk_approved_payload=approved,
        canonical_ready=bool(
            approved and str(risk_out.get("verdict") or "") == "allow"
        ),
        governed_allowed=governed_allowed,
        rollout_active=bool(rollout.get("active")),
        rollout_mode=str(rollout.get("mode") or ""),
        rollout_pair_allowlisted=bool(rollout.get("pair_allowlisted")),
        correlation_id=str(orchestration["correlation_id"]),
        trace_id=str(orchestration["trace_id"]),
        broker_account_mode=str(state.get("broker_account_mode") or ""),
        broker_account_scope=str(state.get("broker_account_scope") or ""),
        authority_revision=int(live_authority.get("authority_revision") or 0),
        runtime_boot_id=str(runtime_boot_id),
        sleeve=SCALP_SLEEVE,
        strategy_authority=dict(authority),
    )
    response, status_code = service.submit_approved_command(
        payload,
        approval=approval,
        proto="v2",
    )
    response_status = str(response.get("status") or "").strip().lower()
    duplicate_state = str(response.get("state") or "").strip().lower()
    return {
        "status_code": int(status_code),
        "accepted": response_status == "queued"
        or (
            response_status == "duplicate"
            and duplicate_state in {"queued", "delivered"}
        ),
        "response": dict(response),
        "command_id": command_id,
    }


def _exit_payload(*, decision: Any, owned_position: Any) -> dict[str, Any]:
    reason = str(getattr(decision, "reason", "") or "")
    action_slug = (
        "rollover-funding-guard" if reason == "rollover_funding_guard" else "time-stop"
    )
    command_id = f"fxs-exit-{int(decision.target_ticket)}-{action_slug}"
    digest = hashlib.sha256(command_id.encode("utf-8")).hexdigest()
    historical_binding = dict(
        getattr(owned_position, "entry_authority_binding", ()) or ()
    )
    payload = {
        "command_id": command_id,
        "cmd": "CLOSE",
        "symbol": str(decision.symbol),
        "lots": 0.0,
        "close_lots": 0.0,
        "target_ticket": int(decision.target_ticket),
        "expected_target_lots": float(owned_position.lots),
        "expected_broker_contract_broker_symbol": str(owned_position.broker_symbol),
        "magic": int(decision.magic),
        "owner_token": str(decision.owner_token),
        "ownership_contract": TICKET_OWNER_CONTRACT,
        "intent": "EXIT",
        "action": reason,
        "management_strategy": str(
            historical_binding.get("expected_strategy_id") or ""
        ),
        "managed_entry_command_id": str(
            getattr(owned_position, "entry_command_id", "") or ""
        ),
        "trace_id": f"scalp-exit-{digest[:32]}",
        "correlation_id": f"scalp-exit-{digest[:32]}",
        "thread_id": f"scalp:{decision.symbol}:{decision.target_ticket}",
    }
    if owned_position.open_price is not None:
        payload["expected_open_price"] = float(owned_position.open_price)
    if owned_position.tp is not None and float(owned_position.tp) > 0.0:
        payload["expected_tp_price"] = float(owned_position.tp)
    # CLOSE is protective management, not a new entry.  Preserve the exact
    # authority identity used to open the ticket without claiming the
    # production entry lane/intent reserved for FinalEntryApproval commands.
    payload.update(historical_binding)
    return payload


def _submit_time_stop_exits(
    *,
    service: Any,
    live: bool,
    reconciliation: ScalpRestartReconciliationResult,
    lifecycle: Any,
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    active = set(reconciliation.active_exit_tickets)
    broker_confirmed = set(reconciliation.broker_confirmed_exit_symbols)
    owned_by_ticket = {
        int(position.ticket): position
        for position in tuple(getattr(reconciliation, "owned_positions", ()) or ())
    }
    for decision in lifecycle.close_decisions:
        if decision.symbol in broker_confirmed:
            outcomes.append(
                {
                    "ticket": decision.target_ticket,
                    "symbol": decision.symbol,
                    "status": "broker_confirmed",
                    "submitted": False,
                }
            )
            continue
        if decision.target_ticket in active:
            outcomes.append(
                {
                    "ticket": decision.target_ticket,
                    "symbol": decision.symbol,
                    "status": "already_active",
                    "submitted": False,
                }
            )
            continue
        owned_position = owned_by_ticket.get(int(decision.target_ticket))
        if owned_position is None:
            outcomes.append(
                {
                    "ticket": decision.target_ticket,
                    "symbol": decision.symbol,
                    "status": "historical_owner_binding_missing",
                    "submitted": False,
                }
            )
            continue
        payload = _exit_payload(
            decision=decision,
            owned_position=owned_position,
        )
        if not live:
            outcomes.append(
                {
                    "ticket": decision.target_ticket,
                    "symbol": decision.symbol,
                    "status": "shadow_preview",
                    "submitted": False,
                    "payload": payload,
                }
            )
            continue
        response, status_code = service.submit_command(payload, proto="v2")
        outcomes.append(
            {
                "ticket": decision.target_ticket,
                "symbol": decision.symbol,
                "status": str(response.get("status") or ""),
                "submitted": True,
                "status_code": int(status_code),
                "response": dict(response),
            }
        )
    return outcomes


@dataclass(frozen=True, slots=True)
class ScalpCycleResult:
    cycle_id: str
    live: bool
    admission_valid: bool
    authority_active: bool
    proposal_count: int
    qualified_count: int
    selected_count: int
    exit_due_count: int
    exit_submit_count: int
    entry_submit_count: int
    entry_accept_count: int
    diagnostics: dict[str, Any]
    schema_version: str = SCALP_LIVE_LOOP_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def execute_production_scalp_cycle(
    *,
    service: Any,
    settings: Any,
    runtime_boot_id: str,
    runtime_config_hash: str,
    equity_seed: float,
    entry_revoked_latch: bool,
    now_epoch: float | None = None,
    governance_max_source_age_secs: float = 60.0,
    bar_history_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[ScalpCycleResult, bool]:
    """Execute one exact-scope cycle and return the sticky revocation latch."""

    now = _finite(time.time() if now_epoch is None else now_epoch)
    cycle_id = str(int(now * 1000.0))
    live = _is_live(settings)
    policy = FROZEN_MTVCLC_POLICY
    rollover_guard = evaluate_production_scalp_rollover_guard(now)
    admission = verify_configured_scalp_runtime_admission(
        settings,
        now_epoch=now,
        policy=policy,
    )
    command_admission = scalp_live_command_admission(
        settings=settings,
        admission=admission,
    )
    protective_activation: dict[str, Any] = {}
    if live and (not admission.valid or entry_revoked_latch):
        reason = (
            admission.reason
            if not admission.valid
            else "scalp_entry_authority_restart_required"
        )
        entry_revoked_latch = True
        protective_state = dict(service.get_state() or {})
        if _protective_management_egress_active(
            protective_state,
            runtime_boot_id=runtime_boot_id,
        ):
            protective_activation = {
                "active": True,
                "reason": "already_active",
                "errors": [],
                "authority": dict(
                    protective_state.get("production_scalp_authority") or {}
                ),
                "egress": dict(
                    protective_state.get("execution_egress_authority") or {}
                ),
            }
        elif not admission.valid:
            protective_activation = _diagnostic_mapping(
                ensure_production_scalp_protective_management_egress(
                    service=service,
                    settings=settings,
                    runtime_boot_id=runtime_boot_id,
                    admission=admission,
                )
            )
        else:
            revoke_production_scalp_authority(service=service, reason=reason)
            protective_activation = {
                "active": False,
                "reason": "scalp_entry_authority_restart_required",
                "errors": ["scalp_entry_authority_restart_required"],
            }

    tick_scope_state = dict(service.get_state() or {})
    require_authenticated_market_source = _is_live(settings)
    expected_market_source: AuthenticatedMarketSource | None = None
    market_source_reason = ""
    if require_authenticated_market_source:
        expected_market_source, market_source_reason = (
            current_authenticated_market_source(
                tick_scope_state,
                now_epoch=now,
                require_active_lease=True,
                expected_protocol_version=BRIDGE_PROTOCOL_VERSION,
            )
        )
    conversion_tick_symbols = _account_conversion_tick_symbols(
        tick_scope_state.get("broker_account_currency")
    )
    ticks_raw = fetch_market_ticks(
        settings.mt4_bridge_url,
        provider="mt4_bridge",
        symbols=[*IG_MT4_SCALP_SYMBOLS, *conversion_tick_symbols],
        settings=settings,
    )
    ticks, tick_scope_reasons = _exact_tick_scope(
        ticks_raw,
        allowed_market_data_symbols=conversion_tick_symbols,
        expected_market_source=expected_market_source,
        require_authenticated_source=require_authenticated_market_source,
    )
    if require_authenticated_market_source and market_source_reason:
        tick_scope_reasons = tuple(
            dict.fromkeys(
                [
                    *tick_scope_reasons,
                    f"scalp_{market_source_reason}",
                ]
            )
        )
    ready = _scalp_market_ready_snapshot(
        state=tick_scope_state,
        now_epoch=now,
        tick_scope_reasons=tick_scope_reasons,
    )
    # A tick-built M1 bar becomes observable only when the next tick arrives.
    # Fetch every cycle and evaluate that newly finalized bar immediately;
    # never reject it merely because the next tick arrived after wall-clock T+5.
    bar_fetch_window_open = True
    signal_minute_epoch = int(now // M1_SECONDS) * M1_SECONDS - M1_SECONDS
    bar_signal_receipt_poll_attempts = 0
    bar_signal_receipt_missing_symbols: tuple[str, ...] = ()
    history_limit = int(settings.production_scalp_bar_history_limit)
    fetch_limit = (
        2
        if bar_history_cache is not None
        and _m1_bar_history_is_warm(
            bar_history_cache,
            limit=history_limit,
        )
        else history_limit
    )
    bars, bar_fetch_errors = _fetch_exact_m1_bars(
        settings=settings,
        limit=fetch_limit,
        expected_market_source=expected_market_source,
        require_authenticated_source=require_authenticated_market_source,
    )
    if bar_history_cache is not None:
        bars = _merge_m1_bar_history(
            bar_history_cache,
            bars,
            limit=history_limit,
        )
    bar_signal_receipt_poll_attempts = 1
    bar_signal_receipt_missing_symbols = _signal_bar_receipt_missing_symbols(
        bars,
        signal_minute_epoch=signal_minute_epoch,
    )
    # Entry clocks must be sampled after market-data I/O. Ticks are stamped by
    # the bridge while the request is in flight, so reusing the cycle-start
    # clock falsely classifies a newly received tick as "from the future".
    # Explicit clocks remain fixed for deterministic replay/tests.
    entry_boundary_now = _finite(
        time.time() if now_epoch is None else now_epoch
    )
    runtime_cost_snapshot: dict[str, Any] = {
        "applicable": bool(admission.valid),
        "valid": bool(admission.valid),
        "authority": False,
        "execution_eligible": bool(admission.valid and live),
        "qualification_eligible": bool(admission.valid),
        "reason": "runtime_native_costs_selected" if admission.valid else admission.reason,
        "cost_symbol_count": len(admission.cost_calibrations),
    }
    if admission.valid or live:
        costs_by_symbol = {
            symbol: cost
            for symbol in IG_MT4_SCALP_SYMBOLS
            if (cost := admission.cost_calibration_for(symbol)) is not None
        }
    else:
        costs_by_symbol = {}
    batch = evaluate_mtvclc_profile_batch(
        strategy_profile=MTVCLC_RUNTIME_PROFILE_ID,
        raw_bars_by_symbol=bars,
        raw_quotes_by_symbol=ticks,
        costs_by_symbol=costs_by_symbol,
        market_source_state=tick_scope_state,
        as_of_epoch=entry_boundary_now,
        policy=policy,
    )

    state = dict(service.get_state() or {})
    authority = dict(state.get("production_scalp_authority") or {})
    activation = ScalpAuthorityActivationResult(
        active=False,
        reason="not_live_mode" if not live else "not_activated",
        errors=(),
        authority=authority,
        live_command_admission=command_admission,
    )
    if live and admission.valid and not entry_revoked_latch:
        activation = ensure_production_scalp_authority(
            service=service,
            settings=settings,
            runtime_boot_id=runtime_boot_id,
            admission=admission,
            now_epoch=now,
        )
        state = dict(service.get_state() or {})
        authority = dict(state.get("production_scalp_authority") or {})

    if require_authenticated_market_source:
        cycle_market_source, cycle_market_source_reason = (
            current_authenticated_market_source(
                state,
                now_epoch=now,
                require_active_lease=True,
                expected_protocol_version=BRIDGE_PROTOCOL_VERSION,
            )
        )
        source_cycle_reasons: list[str] = []
        if cycle_market_source is None:
            source_cycle_reasons.append(
                f"scalp_{cycle_market_source_reason or 'market_source_unattested'}"
            )
        elif expected_market_source != cycle_market_source:
            source_cycle_reasons.append("scalp_market_source_changed_during_cycle")
        tick_scope_reasons = tuple(
            dict.fromkeys([*tick_scope_reasons, *source_cycle_reasons])
        )

    magic = _positive_int(state.get("broker_account_magic"))
    state_gate_reasons = _state_gate_reasons(
        state=state,
        settings=settings,
        ready=ready,
        tick_scope_reasons=tick_scope_reasons,
        magic=magic,
    )
    max_contract_age = min(
        float(settings.production_scalp_contract_max_age_secs),
        120.0,
    )
    # The bridge can stamp broker contract state while the preceding requests
    # are in flight.  Use the same post-state-read clock that validates broker
    # snapshots; the cycle-start clock would transiently classify fresh specs
    # as coming from the future.
    snapshot_validation_now = _finite(
        time.time() if now_epoch is None else now_epoch
    )
    contracts = project_ig_mt4_contract_universe(
        state,
        now_ts=snapshot_validation_now,
        max_age_secs=max_contract_age,
    )
    symbol_contracts = {
        symbol: project_ig_mt4_selected_contract_universe(
            state,
            selected_symbols=(symbol,),
            now_ts=snapshot_validation_now,
            max_age_secs=max_contract_age,
        )
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    durable_commands = list(service.get_commands(limit=_MAX_DURABLE_COMMAND_ROWS) or [])
    # The bridge stamps positions with its UTC receipt clock.  Refresh the
    # validation clock after reading state and durable commands so a snapshot
    # received during this cycle's preceding HTTP work cannot appear to come
    # from the future relative to the cycle-start timestamp.  Explicit clocks
    # remain fixed for deterministic replay/tests.
    reconciliation = reconcile_scalp_restart(
        state_snapshot=state,
        durable_command_rows=durable_commands,
        production_scalp_authority=authority,
        now_epoch=snapshot_validation_now,
        max_snapshot_age_secs=max(1.0, float(settings.bridge_stale_heartbeat_secs)),
        expected_magic=magic,
        expected_account_scope=str(state.get("broker_account_scope") or ""),
    )

    finalized_minute = batch.diagnostics.common_closed_minute_epoch
    if finalized_minute is None:
        lifecycle = evaluate_scalp_position_lifecycle(
            authoritative_owned_positions=reconciliation.owned_positions,
            finalized_common_minute_epoch=0,
            as_of_epoch=now,
            expected_magic=magic,
            expected_ownership_contract=TICKET_OWNER_CONTRACT,
            time_stop_bars=TIME_STOP_M1_BARS,
            rollover_guard_decision=rollover_guard,
        )
    else:
        lifecycle = evaluate_scalp_position_lifecycle(
            authoritative_owned_positions=reconciliation.owned_positions,
            finalized_common_minute_epoch=finalized_minute,
            as_of_epoch=now,
            expected_magic=magic,
            expected_ownership_contract=TICKET_OWNER_CONTRACT,
            time_stop_bars=TIME_STOP_M1_BARS,
            rollover_guard_decision=rollover_guard,
        )
    exit_outcomes = _submit_time_stop_exits(
        service=service,
        live=live,
        reconciliation=reconciliation,
        lifecycle=lifecycle,
    )

    qualified_batch, qualified, qualification_diag = _qualified_batch(
        batch,
        admission=admission,
        as_of_epoch=snapshot_validation_now,
    )
    conversion_projection = _cycle_account_conversion_projection(
        ticks_raw,
        account_currency=state.get("broker_account_currency"),
        allowed_market_data_symbols=conversion_tick_symbols,
    )
    symbol_conversions = {
        symbol: _cycle_account_conversion_projection(
            ticks_raw,
            account_currency=state.get("broker_account_currency"),
            allowed_market_data_symbols=conversion_tick_symbols,
            required_symbols=(symbol,),
        )
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    symbol_tick_errors = {
        symbol: _symbol_market_tick_errors(ticks, symbol=symbol)
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    symbol_resource_reasons = {
        symbol: tuple(
            dict.fromkeys(
                [
                    *symbol_tick_errors[symbol],
                    *symbol_contracts[symbol].errors,
                    *symbol_conversions[symbol].errors,
                ]
            )
        )
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    resource_ready_qualified_batch = replace(
        qualified_batch,
        proposals=tuple(
            proposal
            for proposal in qualified_batch.proposals
            if not symbol_resource_reasons[proposal.symbol]
        ),
    )
    occupied_count = len(
        set(reconciliation.authoritative_open_symbols)
        | set(reconciliation.active_queued_entry_symbols)
    )
    capacity = plan_mtvclc_cycle_capacity(
        proposal_batch=resource_ready_qualified_batch,
        authoritative_open_symbols=reconciliation.authoritative_open_symbols,
        active_queued_entry_symbols=reconciliation.active_queued_entry_symbols,
        projected_broker_confirmed_exit_symbols=(
            reconciliation.broker_confirmed_exit_symbols
        ),
        max_total_positions=int(settings.max_total_positions),
        max_pair_positions=int(settings.max_pair_positions),
        max_new_entries_per_cycle=_effective_cycle_cap(
            settings=settings,
            occupied_count=occupied_count,
        ),
    )

    aggregate_quote_rates = dict(conversion_projection.rates)

    governance = _binding_governance_policy(
        service=service,
        state=state,
        settings=settings,
        contract_universe=contracts,
        quote_rates=aggregate_quote_rates,
        ready=ready,
        computed_at=now,
        max_source_age_secs=max(1.0, float(governance_max_source_age_secs)),
    )
    # Every risk decision consumes this exact governance snapshot; candidate
    # sizing below consumes only its own contract and conversion projections.
    state["governance"] = dict(governance)

    entry_global_reasons: list[str] = []
    if not rollover_guard.entry_allowed:
        entry_global_reasons.append(
            rollover_guard.reason or "production_scalp_rollover_guard_invalid"
        )
    entry_global_reasons.extend(state_gate_reasons)
    entry_global_reasons.extend(_aggregate_broker_contract_global_errors(contracts))
    entry_global_reasons.extend(
        _aggregate_account_conversion_global_errors(conversion_projection)
    )
    if not admission.valid:
        entry_global_reasons.append(admission.reason or "scalp_validation_invalid")
    if live and not activation.active:
        entry_global_reasons.append(activation.reason or "scalp_authority_inactive")
    if live and entry_revoked_latch:
        entry_global_reasons.append("scalp_entry_authority_restart_required")
    if not reconciliation.entry_admission_ready:
        entry_global_reasons.extend(reconciliation.quarantine_reasons)
    if not batch.diagnostics.accepted:
        entry_global_reasons.extend(batch.diagnostics.reasons)
    entry_global_reasons = list(
        dict.fromkeys(str(item) for item in entry_global_reasons if str(item))
    )

    quote_diag: dict[str, dict[str, Any]] = {}
    broker_entry_plan_diag: dict[str, dict[str, Any]] = {}
    risk_diag: dict[str, dict[str, Any]] = {}
    entry_outcomes: list[dict[str, Any]] = []
    pending_entries: list[dict[str, Any]] = []
    if not entry_global_reasons:
        for proposal in capacity.selected_proposals:
            candidate = qualified.get(proposal.symbol)
            if candidate is None:
                continue
            refresh = refresh_mtvclc_entry_quote(
                candidate,
                ticks.get(proposal.symbol, {}),
                as_of_epoch=snapshot_validation_now,
                max_tick_age_secs=min(
                    5.0,
                    float(settings.bridge_stale_tick_secs),
                ),
            )
            quote_diag[proposal.symbol] = refresh.diagnostics.to_dict()
            refreshed = refresh.refreshed_candidate
            if refreshed is None or not refresh.accepted:
                continue
            contract = symbol_contracts[proposal.symbol].contract_for(proposal.symbol)
            if contract is None:
                broker_entry_plan_diag[proposal.symbol] = {
                    "accepted": False,
                    "reasons": ["scalp_broker_entry_contract_missing"],
                    "plan": None,
                }
                continue
            broker_entry_result = build_scalp_broker_entry_plan(
                symbol=proposal.symbol,
                side=proposal.side,
                execution_type=proposal.execution_type,
                pending_orders_forbidden=proposal.pending_orders_forbidden,
                entry_deadline_epoch=int(proposal.entry_deadline_epoch or 0),
                as_of_epoch=snapshot_validation_now,
                quote_entry_price=refreshed.refreshed_entry_price,
                reference_mid=refreshed.mid_price,
                stop_distance_price=refreshed.stop_distance_price,
                target_distance_price=refreshed.target_distance_price,
                current_spread_bps=refreshed.current_spread_bps,
                win_probability_lower_bound=(refreshed.win_probability_lower_bound),
                contract=contract,
                current_bid=refreshed.bid_price,
                current_ask=refreshed.ask_price,
                cost_model=ScalpBrokerEntryCostModel.mtvclc(
                    p90_spread_bps=refreshed.admitted_cost.p90_spread_bps,
                    commission_bps_per_round_trip=(
                        refreshed.admitted_cost.commission_bps_per_round_trip
                    ),
                    financing_bps_per_trade=(
                        refreshed.admitted_cost.financing_bps_per_trade
                    ),
                    convert_on_close_charge_fraction=(
                        refreshed.admitted_cost.convert_on_close_charge_fraction
                    ),
                ),
            )
            broker_entry_plan_diag[proposal.symbol] = broker_entry_result.to_dict()
            broker_entry_plan = broker_entry_result.plan
            if broker_entry_plan is None or not broker_entry_result.accepted:
                continue
            risk_out = _risk_entry(
                settings=settings,
                state=state,
                candidate=refreshed,
                broker_entry_plan=broker_entry_plan,
                tick=ticks[proposal.symbol],
                contract_universe=symbol_contracts[proposal.symbol],
                quote_rates=dict(symbol_conversions[proposal.symbol].rates),
                authority=authority,
                runtime_boot_id=runtime_boot_id,
                cycle_id=cycle_id,
                admission=admission,
                pending_entries=pending_entries,
            )
            approved = dict(risk_out.get("approved_order") or {})
            risk_diag[proposal.symbol] = {
                "verdict": str(risk_out.get("verdict") or ""),
                "reason": str(risk_out.get("reason") or ""),
                "approved": bool(approved),
                "trace": list(risk_out.get("trace") or []),
                "approved_order": approved,
            }
            if not approved or str(risk_out.get("verdict") or "") != "allow":
                continue
            pending_entries.append(
                {
                    "symbol": proposal.symbol,
                    "pair": proposal.symbol,
                    "lots": float(approved.get("lots") or 0.0),
                    "side": proposal.side,
                    "portfolio_slot_reserved": True,
                    "approved_order": approved,
                }
            )
            if not live:
                entry_outcomes.append(
                    {
                        "symbol": proposal.symbol,
                        "status": "shadow_preview",
                        "accepted": False,
                        "command_id": str(approved.get("command_id") or ""),
                    }
                )
                continue
            # Re-read protected live authority immediately before constructing
            # FinalEntryApproval; store enqueue and broker poll repeat the check.
            submit_state = dict(service.get_state() or {})
            entry_outcomes.append(
                _submit_entry(
                    service=service,
                    settings=settings,
                    state=submit_state,
                    risk_out=risk_out,
                    candidate=refreshed,
                    authority=authority,
                    runtime_boot_id=runtime_boot_id,
                    cycle_id=cycle_id,
                    admission=admission,
                )
            )

    proposal_by_symbol = {proposal.symbol: proposal for proposal in batch.proposals}
    proposal_diagnostic_by_symbol = {
        diagnostic.symbol: diagnostic
        for diagnostic in getattr(batch.diagnostics, "symbol_diagnostics", ())
    }
    capacity_diagnostics = getattr(capacity, "diagnostics", None)
    capacity_diagnostic_by_symbol = {
        diagnostic.symbol: diagnostic
        for diagnostic in getattr(capacity_diagnostics, "symbol_diagnostics", ())
    }
    decisions: list[dict[str, Any]] = []
    selected_symbols = {proposal.symbol for proposal in capacity.selected_proposals}
    entry_by_symbol = {str(item.get("symbol") or ""): item for item in entry_outcomes}
    for symbol in IG_MT4_SCALP_SYMBOLS:
        proposal = proposal_by_symbol.get(symbol)
        proposal_diagnostic = proposal_diagnostic_by_symbol.get(symbol)
        capacity_diagnostic = capacity_diagnostic_by_symbol.get(symbol)
        qualification = qualification_diag.get(symbol, {})
        decision_context = _symbol_decision_context(
            entry_global_reasons=entry_global_reasons,
            entry_resource_reasons=symbol_resource_reasons[symbol],
            proposal=proposal,
            proposal_diagnostic=proposal_diagnostic,
            qualification_diagnostic=qualification,
            capacity_diagnostic=capacity_diagnostic,
        )
        decisions.append(
            {
                "symbol": symbol,
                "side": str(proposal.side or "FLAT")
                if proposal is not None
                else "FLAT",
                "ts": datetime.fromtimestamp(now, UTC).isoformat(),
                "allowed": bool(proposal is not None and proposal.allowed),
                "execution_ready": bool(
                    symbol in selected_symbols
                    and symbol in risk_diag
                    and risk_diag[symbol].get("approved")
                    and not entry_global_reasons
                ),
                "reasons": decision_context["reasons"],
                "metadata": {
                    "strategy_id": str(
                        proposal.strategy_id if proposal else policy.__class__.__name__
                    ),
                    "strategy_version": str(
                        proposal.strategy_version if proposal else ""
                    ),
                    "entry_strategy_family": "mtvclc",
                    "venue_id": IG_MT4_VENUE_ID,
                    "proposal": proposal.to_dict() if proposal else {},
                    "qualification": qualification,
                    "proposal_batch_symbol_diagnostic": decision_context[
                        "proposal_batch_symbol_diagnostic"
                    ],
                    "capacity_symbol_diagnostic": decision_context[
                        "capacity_symbol_diagnostic"
                    ],
                    "quote_refresh": quote_diag.get(symbol, {}),
                    "broker_entry_plan": broker_entry_plan_diag.get(symbol, {}),
                    "risk": risk_diag.get(symbol, {}),
                    "capacity_selected": symbol in selected_symbols,
                    "enqueue": entry_by_symbol.get(symbol, {}),
                },
            }
        )

    symbol_execution_readiness: list[dict[str, Any]] = []
    for symbol in IG_MT4_SCALP_SYMBOLS:
        proposal_diagnostic = proposal_diagnostic_by_symbol.get(symbol)
        structural_ready = bool(
            proposal_diagnostic is not None and proposal_diagnostic.structural_ready
        )
        structural_reasons = tuple(
            f"structural_unready:{reason}"
            for reason in (
                proposal_diagnostic.structural_reasons
                if proposal_diagnostic is not None
                else ("proposal_batch_symbol_diagnostic_missing",)
            )
        )
        execution_reasons = tuple(
            dict.fromkeys(
                [
                    *entry_global_reasons,
                    *structural_reasons,
                    *symbol_resource_reasons[symbol],
                ]
            )
        )
        symbol_contract = symbol_contracts[symbol]
        symbol_conversion = symbol_conversions[symbol]
        symbol_execution_readiness.append(
            {
                "symbol": symbol,
                "structural_ready": structural_ready,
                "market_tick_ready": not symbol_tick_errors[symbol],
                "market_tick_errors": list(symbol_tick_errors[symbol]),
                "broker_contract_ready": bool(symbol_contract.ok),
                "broker_contract_errors": list(symbol_contract.errors),
                "broker_contract_present": bool(
                    symbol_contract.contract_for(symbol) is not None
                ),
                "account_conversion_ready": bool(symbol_conversion.ok),
                "account_conversion": symbol_conversion.to_dict(),
                "execution_ready": not execution_reasons,
                "execution_reasons": list(execution_reasons),
                "strategy_qualified": symbol in qualified,
                "capacity_selected": symbol in selected_symbols,
                "risk_approved": bool(risk_diag.get(symbol, {}).get("approved", False)),
                "entry_accepted": bool(
                    entry_by_symbol.get(symbol, {}).get("accepted", False)
                ),
            }
        )

    any_pair_broker_contract_ready = any(
        item["broker_contract_ready"] for item in symbol_execution_readiness
    )
    all_pairs_broker_contract_ready = all(
        item["broker_contract_ready"] for item in symbol_execution_readiness
    )
    any_pair_account_conversion_ready = any(
        item["account_conversion_ready"] for item in symbol_execution_readiness
    )
    all_pairs_account_conversion_ready = all(
        item["account_conversion_ready"] for item in symbol_execution_readiness
    )
    any_pair_execution_ready = any(
        item["execution_ready"] for item in symbol_execution_readiness
    )
    all_pairs_execution_ready = all(
        item["execution_ready"] for item in symbol_execution_readiness
    )

    current_state = dict(service.get_state() or {})
    current_runtime_diag = dict(current_state.get("runtime_diag") or {})
    current_live = dict(current_runtime_diag.get("orchestration_live") or {})
    attestation = build_scalp_runtime_attestation(
        runtime_boot_id=runtime_boot_id,
        runtime_pid=int(os.getpid()),
        runtime_config_sha256=runtime_config_hash,
        admission=admission,
        attested_at=now,
    )
    cycle_diagnostics = {
        "schema_version": SCALP_LIVE_LOOP_SCHEMA,
        "cycle_id": cycle_id,
        "entry_strategy_family": "mtvclc",
        "venue_id": IG_MT4_VENUE_ID,
        "configured_symbols": list(IG_MT4_SCALP_SYMBOLS),
        "live": live,
        "entry_revoked_latch": bool(entry_revoked_latch),
        "ready": ready,
        "tick_scope_reasons": list(tick_scope_reasons),
        "account_conversion_tick_symbols": list(conversion_tick_symbols),
        "account_conversion": conversion_projection.to_dict(),
        "symbol_execution_readiness": symbol_execution_readiness,
        "any_pair_broker_contract_ready": any_pair_broker_contract_ready,
        "all_pairs_broker_contract_ready": all_pairs_broker_contract_ready,
        "any_pair_account_conversion_ready": any_pair_account_conversion_ready,
        "all_pairs_account_conversion_ready": all_pairs_account_conversion_ready,
        "any_pair_execution_ready": any_pair_execution_ready,
        "all_pairs_execution_ready": all_pairs_execution_ready,
        "bar_fetch_window_open": bar_fetch_window_open,
        "bar_fetch_errors": bar_fetch_errors,
        "bar_signal_minute_epoch": signal_minute_epoch,
        "bar_signal_receipt_poll_attempts": bar_signal_receipt_poll_attempts,
        "bar_signal_receipt_missing_symbols": list(
            bar_signal_receipt_missing_symbols
        ),
        "runtime_cost_snapshot": runtime_cost_snapshot,
        "proposal_batch": batch.diagnostics.to_dict(),
        "qualification": qualification_diag,
        "restart_reconciliation": reconciliation.to_dict(),
        "rollover_guard": rollover_guard.to_dict(),
        "position_lifecycle": lifecycle.to_dict(),
        "exit_outcomes": exit_outcomes,
        "capacity": capacity.to_dict(),
        "quote_refresh": quote_diag,
        "broker_entry_plan": broker_entry_plan_diag,
        "risk": risk_diag,
        "entry_outcomes": entry_outcomes,
        "entry_global_reasons": entry_global_reasons,
        "broker_contracts": {
            "ok": contracts.ok,
            "errors": list(contracts.errors),
            "observed_at": contracts.observed_at,
            "age_secs": contracts.age_secs
            if math.isfinite(contracts.age_secs)
            else None,
            "symbol_count": len(contracts.contracts),
        },
        "governance": dict(governance),
        "validation": admission.to_dict(),
        "authority_activation": activation.to_dict(),
        "protective_management_activation": protective_activation,
    }
    current_runtime_diag["live_command_admission"] = dict(command_admission)
    current_runtime_diag["production_scalp"] = cycle_diagnostics
    service.patch_state(
        {
            "__expected_orchestration_live_authority__": current_live,
            "runtime_profile": str(
                getattr(settings, "policy_version", "mtvclc")
            ),
            "runtime_status": "running",
            "runtime_last_cycle_ts": now,
            "runtime_boot_id": runtime_boot_id,
            "runtime_attestation": attestation,
            "runtime_diag": current_runtime_diag,
            "governance": dict(governance),
            "scalp_account_conversion_ready": bool(conversion_projection.ok),
            "scalp_account_conversion_errors": list(conversion_projection.errors),
            "configured_pairs": list(IG_MT4_SCALP_SYMBOLS),
            "runtime_equity_seed": float(equity_seed),
        }
    )
    service.store_decisions(
        decisions=decisions,
        vol=0.0,
        diagnostics={
            "runtime": "fxstack_production_scalp",
            "schema_version": SCALP_LIVE_LOOP_SCHEMA,
            "pairs": list(IG_MT4_SCALP_SYMBOLS),
            "loop_ts": now,
            "production_scalp": cycle_diagnostics,
        },
    )
    result = ScalpCycleResult(
        cycle_id=cycle_id,
        live=live,
        admission_valid=bool(admission.valid),
        authority_active=bool(activation.active),
        proposal_count=len(batch.proposals),
        qualified_count=len(qualified),
        selected_count=len(capacity.selected_proposals),
        exit_due_count=len(lifecycle.close_decisions),
        exit_submit_count=sum(bool(item.get("submitted")) for item in exit_outcomes),
        entry_submit_count=sum("status_code" in item for item in entry_outcomes),
        entry_accept_count=sum(bool(item.get("accepted")) for item in entry_outcomes),
        diagnostics=cycle_diagnostics,
    )
    return result, entry_revoked_latch


def run_production_scalp_loop(
    *,
    settings: Any,
    startup_preflight: Mapping[str, Any],
    equity: float,
    sleep_secs: int,
    feature_root: str,
    max_cycles: int | None = None,
) -> None:
    """Boot and run the sole production scalp loop selected by the runner."""

    del feature_root  # The strategy consumes versioned bridge M1 bars directly.
    if (
        str(getattr(settings, "entry_strategy_family", "")).strip().lower()
        != "mtvclc"
    ):
        raise RuntimeError("production_scalp_loop_strategy_family_mismatch")
    if tuple(str(item).upper() for item in list(settings.pairs or [])) != tuple(
        IG_MT4_SCALP_SYMBOLS
    ):
        raise RuntimeError("production_scalp_loop_symbol_scope_mismatch")
    if not bool(dict(startup_preflight or {}).get("settings_validated", False)):
        raise RuntimeError("production_scalp_loop_startup_preflight_missing")

    from fxstack.runtime.startup import perform_startup_bridge_checks

    perform_startup_bridge_checks(settings)
    service = _runtime_service(settings)
    runtime_boot_id = str(uuid.uuid4())
    boot_now = time.time()
    boot = _boot_state(
        runtime_boot_id=runtime_boot_id,
        phase="production_scalp_boot",
        now_epoch=boot_now,
    )
    config_hash = runtime_config_sha256(settings)
    boot_recorded = False
    startup_phase = "record_boot"
    startup_management_only = False
    startup_activation: dict[str, Any] = {}
    try:
        service.record_runtime_boot_state(
            boot=boot,
            patch={
                "runtime_profile": str(
                    getattr(settings, "policy_version", "mtvclc")
                ),
                "runtime_status": "starting",
                "runtime_last_cycle_ts": 0.0,
                "runtime_boot_id": runtime_boot_id,
                "runtime_equity_seed": float(equity),
                "configured_pairs": list(IG_MT4_SCALP_SYMBOLS),
                "agent_decisions": [],
                "agent_diagnostics": {},
                "monitor": {},
                "vol": 0.0,
            },
            prune_state=True,
            preserve_queued_exposure_reducing=True,
        )
        boot_recorded = True
        startup_phase = "purge_stale_entries"
        purged = int(
            service.purge_pending_commands(
                reason="production_scalp_runtime_restart_purged",
                intents={SCALP_ENTRY_INTENT},
                include_delivered=False,
                preserve_queued_exposure_reducing=True,
            )
        )
        quarantined = int(
            service.quarantine_stale_delivered(
                age_secs=float(settings.startup_requeue_age_secs)
            )
        )
        startup_phase = "validation"
        admission = verify_configured_scalp_runtime_admission(
            settings,
            now_epoch=time.time(),
            policy=FROZEN_MTVCLC_POLICY,
        )
        startup_attestation = build_scalp_runtime_attestation(
            runtime_boot_id=runtime_boot_id,
            runtime_pid=int(os.getpid()),
            runtime_config_sha256=config_hash,
            admission=admission,
            attested_at=time.time(),
        )
        state = dict(service.get_state() or {})
        runtime_diag = dict(state.get("runtime_diag") or {})
        current_live = dict(runtime_diag.get("orchestration_live") or {})
        runtime_diag["production_scalp_startup"] = {
            "schema_version": SCALP_LIVE_LOOP_SCHEMA,
            "pending_commands_purged": purged,
            "delivered_commands_quarantined": quarantined,
            "validation": admission.to_dict(),
            "startup_preflight": dict(startup_preflight),
        }
        runtime_diag["live_command_admission"] = scalp_live_command_admission(
            settings=settings,
            admission=admission,
        )
        startup_phase = "authority_activation"
        # The store requires the protected live plane to be in running posture
        # before authority CAS, but /ready must remain false until CAS succeeds.
        service.patch_state(
            {
                "__expected_orchestration_live_authority__": current_live,
                "runtime_status": "running",
                "runtime_last_cycle_ts": 0.0,
                "runtime_attestation": startup_attestation,
                "runtime_diag": runtime_diag,
                "runtime_startup": {**boot, "phase": startup_phase},
            }
        )
        if _is_live(settings):
            if admission.valid:
                activation = ensure_production_scalp_authority(
                    service=service,
                    settings=settings,
                    runtime_boot_id=runtime_boot_id,
                    admission=admission,
                    now_epoch=time.time(),
                )
                startup_activation = _diagnostic_mapping(activation)
                if not activation.active:
                    raise RuntimeError(
                        "production_scalp_authority_startup_failed:" + activation.reason
                    )
            else:
                protective = ensure_production_scalp_protective_management_egress(
                    service=service,
                    settings=settings,
                    runtime_boot_id=runtime_boot_id,
                    admission=admission,
                )
                startup_activation = _diagnostic_mapping(protective)
                if not protective.active:
                    raise RuntimeError(
                        "production_scalp_protective_management_startup_failed:"
                        + protective.reason
                    )
                startup_management_only = True

        startup_phase = "main_loop"
        ready_state = dict(service.get_state() or {})
        ready_runtime_diag = dict(ready_state.get("runtime_diag") or {})
        ready_live = dict(ready_runtime_diag.get("orchestration_live") or {})
        ready_startup_diag = dict(
            ready_runtime_diag.get("production_scalp_startup") or {}
        )
        ready_startup_diag["authority_activation"] = dict(startup_activation)
        ready_startup_diag["protective_management_only"] = bool(startup_management_only)
        ready_runtime_diag["production_scalp_startup"] = ready_startup_diag
        service.patch_state(
            {
                "__expected_orchestration_live_authority__": ready_live,
                "runtime_status": "running",
                "runtime_last_cycle_ts": time.time(),
                "runtime_attestation": startup_attestation,
                "runtime_diag": ready_runtime_diag,
                "runtime_startup": {**boot, "phase": startup_phase},
            }
        )
    except Exception as exc:
        if boot_recorded:
            failure_reason = (
                f"production_scalp_startup_failed:{startup_phase}:{type(exc).__name__}"
            )
            try:
                service.record_runtime_boot_failure(
                    boot={**boot, "phase": startup_phase},
                    failure_reason=failure_reason,
                    patch={
                        "runtime_status": "failed",
                        "runtime_last_cycle_ts": 0.0,
                        "runtime_boot_id": runtime_boot_id,
                    },
                    prune_state=False,
                    preserve_queued_exposure_reducing=True,
                )
            except Exception:
                # Preserve the original startup exception; the boot egress was
                # already revoked by record_runtime_boot_state.
                pass
        raise
    _startup_log(
        f"ready boot={runtime_boot_id} symbols={len(IG_MT4_SCALP_SYMBOLS)} "
        f"mode={('live-protective' if startup_management_only else 'live') if _is_live(settings) else 'shadow'}"
    )

    cycles = 0
    entry_revoked_latch = bool(startup_management_only)
    bar_history_cache = _empty_exact_m1_bar_scope()
    try:
        while max_cycles is None or cycles < max_cycles:
            _, entry_revoked_latch = execute_production_scalp_cycle(
                service=service,
                settings=settings,
                runtime_boot_id=runtime_boot_id,
                runtime_config_hash=config_hash,
                equity_seed=float(equity),
                entry_revoked_latch=entry_revoked_latch,
                governance_max_source_age_secs=max(
                    60.0,
                    float(max(1, int(sleep_secs))) * 3.0,
                ),
                bar_history_cache=bar_history_cache,
            )
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            # Keep thinking throughout the minute. A tick-built M1 bar may not
            # become observable at the nominal boundary; the next cycle picks
            # it up as soon as the broker emits the first post-close tick.
            time.sleep(max(0.1, float(sleep_secs)))
    except BaseException as exc:
        failure_reason = f"production_scalp_runtime_failed:{type(exc).__name__}"
        try:
            service.record_runtime_boot_failure(
                boot={**boot, "phase": "main_loop"},
                failure_reason=failure_reason,
                patch={
                    "runtime_status": "failed",
                    "runtime_last_cycle_ts": 0.0,
                    "runtime_boot_id": runtime_boot_id,
                },
                prune_state=False,
                preserve_queued_exposure_reducing=True,
            )
        except Exception:
            pass
        raise


__all__ = [
    "SCALP_LIVE_LOOP_SCHEMA",
    "ScalpCycleResult",
    "execute_production_scalp_cycle",
    "run_production_scalp_loop",
]
