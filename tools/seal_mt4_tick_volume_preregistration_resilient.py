"""Seal the next MTVCLC experiment after collection-only failures.

The d80e6cc9 and 07b78ce6 experiments are preserved as failed operational
attempts.  This sealer does not restart or extend either window.  It creates a
new 180-day window, counts both prior 44-cell attempts in the family, and binds
the restart-resilient collector's first-observation-or-absence-wins contract.

No signal, outcome, performance, signing, activation, registry, broker, or
order capability is present here.
"""

from __future__ import annotations

# AGENT: ROLE: offline sealer for the watermark-resilient MTVCLC replacement.
# AGENT: HANDSHAKE: frozen v1 declaration inputs + two explicit failed attempts -> one immutable declaration.
# AGENT: ISOLATION: local files only; no capture inspection, credentials, issuer, runtime, or broker access.
# AGENT: SIDE EFFECTS: one atomic, exclusive, content-addressed JSON write only.

import argparse
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import seal_mt4_tick_volume_preregistration as base  # noqa: E402
from fxstack.providers.ig_mt4_catalog import (  # noqa: E402
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_VENUE_ID,
)
from fxstack.scalp import (  # noqa: E402
    screen_mt4_tick_volume_close_location_continuation_replacement as replacement_screen,
)


TOOL_PATH = Path(__file__).resolve()
BASE_SEALER_PATH = REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration.py"
COLLECTOR_PATH = REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient.py"
COLLECTOR_SUPPORT_PATH = REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity.py"
SCREEN_PATH = (
    REPO_ROOT
    / "fx-quant-stack"
    / "src"
    / "fxstack"
    / "scalp"
    / "screen_mt4_tick_volume_close_location_continuation_replacement.py"
)
SCREEN_SUPPORT_PATH = (
    REPO_ROOT
    / "fx-quant-stack"
    / "src"
    / "fxstack"
    / "scalp"
    / "screen_mt4_tick_volume_close_location_continuation.py"
)
BASE_SEALER_SHA256 = "e4248f6f8484435a7e1bb4994363811edf32335ce04f31bfa25cbe844eba0ef1"
COLLECTOR_SUPPORT_SHA256 = (
    "87af2452ae3f0f3964c35b2904ad35fda28f3675e1f614e941ee46fd693bda5d"
)
SCREEN_SUPPORT_SHA256 = (
    "7e3dd3e829a4429e1316be2925f17d6ec3c9c0e3257e529cefd153e660558059"
)
REPLACED_PREREGISTRATION_BODY_SHA256 = (
    "d80e6cc9f05726ff2d2e851890ec06ca1b2df8e08efb4066bc83e31c216f17f3"
)
REPLACED_PREREGISTRATION_ARTIFACT_SHA256 = (
    "566c85789fdf1f8639a4c010421d27f3b02e6dd2f9a1912968ca8f9b500c51b3"
)
REPLACED_MANIFEST_SHA256 = (
    "86a28670961de97980a09018ea69a9190223459581ba5bfdf00a01aef0b22a6e"
)
REPLACED_MANIFEST_TAIL_SHA256 = (
    "ec8f23c7b35a79ccce1cfb269ccd287301800c7e3c845710b775001147f1b163"
)
PRIOR_ATTEMPTED_CELLS_LOWER_BOUND = 4_742
CURRENT_ATTEMPTED_CELLS = 44
CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND = 4_786
EXPECTED_SYMBOLS: tuple[str, ...] = (
    "EURUSD",
    "USDJPY",
    "AUDUSD",
    "GBPUSD",
    "USDCAD",
    "USDCHF",
    "EURGBP",
    "EURJPY",
    "NZDUSD",
    "AUDJPY",
    "CADJPY",
    "CHFJPY",
    "EURAUD",
    "EURCAD",
    "EURCHF",
    "GBPCAD",
    "GBPCHF",
    "GBPJPY",
    "BTCUSD",
    "ETHUSD",
    "AUDCAD",
    "NZDJPY",
)

CAPTURE_INTEGRITY_POLICY: dict[str, Any] = {
    "contract_id": (
        "authenticated_finalized_m1_first_observation_or_absence_wins_across_"
        "same_source_restart.v2"
    ),
    "first_authenticated_finalized_observation_is_immutable": True,
    "matching_later_overlap_is_ignored": True,
    "revised_later_overlap_is_ignored": True,
    "overlap_never_overwrites_or_duplicates_a_bar": True,
    "late_unseen_overlap_at_or_before_watermark_is_ignored": True,
    "durable_per_symbol_watermark_never_regresses": True,
    "market_source_id_rollover_refuses": True,
    "downtime_gaps_are_preserved": True,
    "t0_reset_forbidden": True,
    "capture_restart_does_not_restart_or_extend_the_experiment": True,
    "tick_interval_seconds": 2.0,
    "bar_interval_seconds": 60.0,
    "bar_limit": 400,
    "http_timeout_seconds": 5.0,
    "persistence_mode": (
        "fsynced_hash_chained_active_hour_journal_then_immutable_hour_chunk"
    ),
    "bootstrap_first_cycle_finalized_immediately": True,
    "active_hour_journal_filename": "active-hour.journal.sha256.jsonl",
    "portable_chunk_schema_version": ("fxstack.external_ig_mt4_m1_activity_chunk.v2"),
    "portable_manifest_schema_version": (
        "fxstack.external_ig_mt4_m1_activity_manifest_entry.v2"
    ),
    "exclusive_output_data_writer_lock_required": True,
}


def capture_integrity_contract(collector_source_sha256: str) -> dict[str, Any]:
    """Bind the declared overlap policy to both executable source layers."""

    return {
        "schema_version": "fxstack.scalp.mtvclc_capture_integrity_contract.v2",
        **CAPTURE_INTEGRITY_POLICY,
        "collector_source_sha256": collector_source_sha256,
        "collector_support_source_sha256": COLLECTOR_SUPPORT_SHA256,
    }


FIRST_REPLACED_ATTEMPT: dict[str, Any] = {
    "preregistration_body_sha256": REPLACED_PREREGISTRATION_BODY_SHA256,
    "artifact_file_sha256": REPLACED_PREREGISTRATION_ARTIFACT_SHA256,
    "reason": "authenticated_mt4_restart_revised_completed_bar_history",
    "eligible_observations_emitted": True,
    "manifest_entries_emitted": 1_448,
    "manifest_file_sha256": REPLACED_MANIFEST_SHA256,
    "final_manifest_entry_sha256": REPLACED_MANIFEST_TAIL_SHA256,
    "attempted_cells_increment": 44,
    "signal_evaluation_performed": False,
    "outcome_evaluation_performed": False,
    "performance_statistics_computed": False,
    "success_claim_evaluated": False,
}

SECOND_REPLACED_ATTEMPT: dict[str, Any] = {
    "preregistration_body_sha256": (
        "07b78ce6d697a61db308560c325f547e90984143f2b71c1613bb8ee12b9f879c"
    ),
    "artifact_file_sha256": (
        "127d3647ab6378962c8f938e84522b16e3f82dbf6057ef553b7a087e6a4f4cba"
    ),
    "reason": "same_source_restart_late_unseen_completed_bar_backfill",
    "eligible_observations_emitted": True,
    "manifest_entries_emitted": 3,
    "manifest_file_sha256": (
        "7d3e925f819e8e49c071e088a062263dba9354467308c44e536e75b6382d737a"
    ),
    "final_manifest_entry_sha256": (
        "27feebe74d2cb172e68f70278dddd25c6ada8f5b9c1166483f8e8bbc3ccad7a9"
    ),
    "attempted_cells_increment": 44,
    "signal_evaluation_performed": False,
    "outcome_evaluation_performed": False,
    "performance_statistics_computed": False,
    "success_claim_evaluated": False,
}

# Compatibility alias for callers that referred to the previously latest
# failed attempt.  New lineage code uses the explicit ordered pair above.
REPLACED_ATTEMPT = FIRST_REPLACED_ATTEMPT

REPLACEMENT_LINEAGE: dict[str, Any] = {
    "replaces_preregistration_body_sha256": (
        SECOND_REPLACED_ATTEMPT["preregistration_body_sha256"]
    ),
    "replacement_reason": (
        "late_backfill_watermark_contract_hardened_before_evaluation"
    ),
    "old_window_restart_or_extension": False,
    "new_independent_window_required": True,
    "old_capture_used_for_signal_outcome_or_performance_selection": False,
    "old_attempt_counted_in_multiplicity_family": True,
}


class ReplacementPreregistrationRefusal(RuntimeError):
    """Stable fail-closed refusal raised before publication."""


def _exact_keys(value: Mapping[str, Any], expected: set[str]) -> bool:
    return set(value) == expected


def _identity_valid(value: Any, *, filename: str | None = None) -> bool:
    if not isinstance(value, Mapping) or not _exact_keys(
        value, {"filename", "sha256", "size_bytes"}
    ):
        return False
    name = str(value.get("filename") or "")
    size = value.get("size_bytes")
    return bool(
        (filename is None or name == filename)
        and name
        and base._is_sha256(value.get("sha256"))
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size > 0
    )


def _finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0.0 else None


def _validate_cost_policy(value: Any) -> bool:
    if not isinstance(value, Mapping) or not _exact_keys(
        value,
        {
            "formula",
            "conversion_treatment",
            "geometry_uses_pre_conversion_cost",
            "final_cell_mean_uses_conversion_adjusted_net",
            "unknown_commission_financing_or_conversion_refuses_evaluation",
            "fee_schedule_change_or_source_uncertainty_refuses_evaluation",
            "symbols",
        },
    ):
        return False
    if (
        value.get("formula")
        != "net_bps=gross_quote_bps-(p90_ig_spread_bps+commission_bps_per_"
        "round_trip+financing_bps_per_trade+1.0bps_adverse_execution_debit)-"
        "conversion_rate*abs(gross_quote_bps)_when_profit_loss_currency_"
        "differs_from_account_currency"
        or value.get("conversion_treatment")
        != "debit_the_attested_rate_on_absolute_profit_or_loss_for_both_wins_"
        "and_losses;never_credit_conversion;zero_only_when_the_profit_loss_"
        "currency_equals_the_attested_account_currency"
        or value.get("geometry_uses_pre_conversion_cost") is not True
        or value.get("final_cell_mean_uses_conversion_adjusted_net") is not True
        or value.get("unknown_commission_financing_or_conversion_refuses_evaluation")
        is not True
        or value.get("fee_schedule_change_or_source_uncertainty_refuses_evaluation")
        is not True
    ):
        return False
    symbols = value.get("symbols")
    if not isinstance(symbols, Mapping) or set(symbols) != set(EXPECTED_SYMBOLS):
        return False
    row_fields = {
        "p90_ig_spread_bps",
        "commission_bps_per_round_trip",
        "financing_bps_per_trade",
        "fixed_adverse_execution_debit_bps",
        "pre_conversion_geometry_cost_bps",
        "profit_loss_currency",
        "account_currency",
        "conversion_rate_of_absolute_profit_or_loss",
        "convert_on_close_charge_fraction_for_screen",
        "conversion_applies",
        "conversion_adjusted_break_even_win_probability",
        "commission_status",
        "financing_status",
        "conversion_status",
    }
    for symbol in EXPECTED_SYMBOLS:
        row = symbols.get(symbol)
        if not isinstance(row, Mapping) or not _exact_keys(row, row_fields):
            return False
        p90 = _finite_nonnegative(row.get("p90_ig_spread_bps"))
        commission = _finite_nonnegative(row.get("commission_bps_per_round_trip"))
        financing = _finite_nonnegative(row.get("financing_bps_per_trade"))
        adverse = _finite_nonnegative(row.get("fixed_adverse_execution_debit_bps"))
        geometry = _finite_nonnegative(row.get("pre_conversion_geometry_cost_bps"))
        conversion = _finite_nonnegative(
            row.get("conversion_rate_of_absolute_profit_or_loss")
        )
        screen_conversion = _finite_nonnegative(
            row.get("convert_on_close_charge_fraction_for_screen")
        )
        p_star = _finite_nonnegative(
            row.get("conversion_adjusted_break_even_win_probability")
        )
        pnl_currency = symbol[3:]
        conversion_applies = pnl_currency != "USD"
        commission_status = row.get("commission_status")
        financing_status = row.get("financing_status")
        if (
            p90 is None
            or p90 <= 0.0
            or commission is None
            or financing is None
            or adverse != 1.0
            or geometry is None
            or conversion != 0.005
            or screen_conversion != (0.005 if conversion_applies else 0.0)
            or p_star is None
            or not 0.0 < p_star < 1.0
            or row.get("profit_loss_currency") != pnl_currency
            or row.get("account_currency") != "USD"
            or row.get("conversion_applies") is not conversion_applies
            or commission_status
            not in {"explicit_source_attested", "conservative_upper_bound"}
            or financing_status
            not in {
                "structurally_avoided_by_fixed_rollover_guard",
                "conservative_upper_bound",
            }
            or (commission == 0.0 and commission_status != "explicit_source_attested")
            or (
                financing == 0.0
                and financing_status != "structurally_avoided_by_fixed_rollover_guard"
            )
            or row.get("conversion_status")
            != "debit_absolute_profit_or_loss_when_account_currency_differs"
            or not math.isclose(
                geometry,
                p90 + commission + financing + adverse,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            return False
        calibration = replacement_screen.MT4CostCalibration(
            symbol=symbol,
            p90_spread_bps=p90,
            commission_bps_per_round_trip=commission,
            financing_bps_per_trade=financing,
            account_currency="USD",
            pnl_currency=pnl_currency,
            convert_on_close_charge_fraction=screen_conversion,
            source_sha256="0" * 64,
        )
        if not math.isclose(
            p_star,
            calibration.break_even_win_probability,
            rel_tol=0.0,
            abs_tol=1e-15,
        ):
            return False
    return True


def _validate_static_contract(body: Mapping[str, Any]) -> bool:
    expected_top = {
        "schema_version",
        "sealed_at_utc",
        "research_only",
        "strategy",
        "scope",
        "attempt_accounting",
        "abandoned_preregistrations",
        "replacement_lineage",
        "capture_integrity_contract",
        "prospective_window",
        "execution_contract",
        "cost_policy",
        "fixed_success_gates",
        "source_identities",
        "isolation_contract",
        "authority",
    }
    if not _exact_keys(body, expected_top):
        return False
    strategy = body.get("strategy")
    scope = body.get("scope")
    window = body.get("prospective_window")
    execution = body.get("execution_contract")
    gates = body.get("fixed_success_gates")
    isolation = body.get("isolation_contract")
    if not all(
        isinstance(value, Mapping)
        for value in (strategy, scope, window, execution, gates, isolation)
    ):
        return False
    assert isinstance(strategy, Mapping)
    assert isinstance(scope, Mapping)
    assert isinstance(window, Mapping)
    assert isinstance(execution, Mapping)
    assert isinstance(gates, Mapping)
    assert isinstance(isolation, Mapping)
    manifest = replacement_screen.attempt_manifest()
    expected_cells = [
        {
            "config_id": replacement_screen.CONFIG_ID,
            "symbol": symbol,
            "side": side,
        }
        for symbol in EXPECTED_SYMBOLS
        for side in ("BUY", "SELL")
    ]
    if strategy != {
        "strategy_id": replacement_screen.STRATEGY_ID,
        "strategy_version": replacement_screen.STRATEGY_VERSION,
        "config_id": replacement_screen.CONFIG_ID,
        "config_sha256": base.canonical_sha256(asdict(replacement_screen.GRID[0])),
        "source_contract_id": replacement_screen.SOURCE_CONTRACT_ID,
        "activity_metric_id": replacement_screen.ACTIVITY_METRIC_ID,
        "attempt_manifest": manifest,
        "attempt_manifest_sha256": base.canonical_sha256(manifest),
    }:
        return False
    if scope != {
        "venue_id": IG_MT4_VENUE_ID,
        "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
        "ordered_symbols": list(EXPECTED_SYMBOLS),
        "sides": ["BUY", "SELL"],
        "cell_order": expected_cells,
    }:
        return False
    if execution != {
        "entry_type": "immediate_market",
        "pending_orders_forbidden": True,
        "maximum_entries_per_symbol_utc_day": 1,
        "outcome_horizon_m1_bars": 30,
        "rollover_entry_blackout_utc": "[20:20:00,22:10:00)",
        "rollover_entry_blackout_half_open": True,
        "signals_inside_blackout_reserve": False,
    }:
        return False
    expected_window_flags = {
        "consecutive_days": base.PROSPECTIVE_WINDOW_DAYS,
        "fixed_before_any_eligible_observation": True,
        "observations_before_t0_forbidden": True,
        "observations_at_or_after_end_forbidden": True,
        "interim_signal_or_outcome_evaluation_forbidden": True,
        "interim_performance_statistics_forbidden": True,
        "early_success_forbidden": True,
        "no_optional_extension_or_restart_after_failure": True,
        "data_quality_monitoring_must_not_compute_performance": True,
    }
    if any(window.get(key) != value for key, value in expected_window_flags.items()):
        return False
    if window.get("success_evaluation_not_before_utc") != window.get(
        "end_utc_exclusive"
    ):
        return False
    expected_gates = {
        "all_44_cells_must_pass": True,
        "minimum_trades_per_cell": replacement_screen.MIN_TRADES_PER_CELL,
        "minimum_independent_utc_days_per_cell": (
            replacement_screen.MIN_INDEPENDENT_DAYS_PER_CELL
        ),
        "cell_win_probability_interval": (
            "one_sided_wilson_family_adjusted_over_4786_attempted_cells"
        ),
        "cell_win_probability_family_confidence": (
            replacement_screen.WIN_PROBABILITY_FAMILY_CONFIDENCE
        ),
        "cell_win_probability_lower_bound_strictly_greater_than": (
            "the_exact_per_symbol_conversion_adjusted_break_even_win_"
            "probability_in_cost_policy.symbols"
        ),
        "minimum_unconverted_break_even_win_probability": (
            replacement_screen.BASE_COST_BREAK_EVEN_WIN_PROBABILITY
        ),
        "cell_conversion_adjusted_mean_net_bps_strictly_greater_than": 0.0,
        "minimum_total_trades": base.MINIMUM_TOTAL_TRADES,
        "minimum_total_independent_utc_days": (base.MINIMUM_TOTAL_INDEPENDENT_DAYS),
        "source_scope_ready_required": True,
        "source_errors_required": [],
        "descriptive_df99_bonferroni_abs_t_threshold": (
            replacement_screen.BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
        ),
    }
    if gates != expected_gates:
        return False
    if isolation != {
        "artifact_grants_no_input_or_outcome_access": True,
        "prospective_collection_is_a_separate_get_only_operator_action": True,
        "outcome_evaluation_requires_a_physically_isolated_research_host": True,
        "production_database_bridge_broker_credentials_registry_and_issuer_"
        "must_not_be_mounted": True,
        "transfer_to_isolation_must_verify_preregistration_body_sha256": True,
    }:
        return False
    return _validate_cost_policy(body.get("cost_policy"))


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReplacementPreregistrationRefusal("source_file_unreadable") from exc


def _file_identity(path: Path, *, label: str) -> dict[str, Any]:
    try:
        return base._file_identity(path, label=label)
    except base.PreregistrationRefusal as exc:
        raise ReplacementPreregistrationRefusal(str(exc)) from None


def _abandoned_attempts() -> list[dict[str, Any]]:
    return [
        *(dict(row) for row in base.ABANDONED_PREREGISTRATION_AUDIT),
        dict(FIRST_REPLACED_ATTEMPT),
        dict(SECOND_REPLACED_ATTEMPT),
    ]


def build_preregistration(
    *,
    cost_capture_json: str | Path,
    cost_capture_npz: str | Path,
    fee_attestation: str | Path,
    sealed_at: datetime,
    start_delay_seconds: int = base.DEFAULT_START_DELAY_SECONDS,
) -> dict[str, Any]:
    """Build a new declaration without reading the stopped capture."""

    if not hmac.compare_digest(_sha256_file(BASE_SEALER_PATH), BASE_SEALER_SHA256):
        raise ReplacementPreregistrationRefusal("base_sealer_source_identity_mismatch")
    if not hmac.compare_digest(
        _sha256_file(COLLECTOR_SUPPORT_PATH), COLLECTOR_SUPPORT_SHA256
    ):
        raise ReplacementPreregistrationRefusal(
            "collector_support_source_identity_mismatch"
        )
    if not hmac.compare_digest(
        _sha256_file(SCREEN_SUPPORT_PATH), SCREEN_SUPPORT_SHA256
    ):
        raise ReplacementPreregistrationRefusal(
            "screen_support_source_identity_mismatch"
        )
    collector_identity = _file_identity(
        COLLECTOR_PATH, label="restart_resilient_collector_source"
    )
    try:
        payload = base.build_preregistration(
            cost_capture_json=cost_capture_json,
            cost_capture_npz=cost_capture_npz,
            fee_attestation=fee_attestation,
            sealed_at=sealed_at,
            start_delay_seconds=start_delay_seconds,
        )
    except base.PreregistrationRefusal as exc:
        raise ReplacementPreregistrationRefusal(str(exc)) from None

    payload.pop("preregistration_body_sha256", None)
    payload["attempt_accounting"] = {
        "prior_attempted_cells_lower_bound": PRIOR_ATTEMPTED_CELLS_LOWER_BOUND,
        "current_attempted_cells": CURRENT_ATTEMPTED_CELLS,
        "cumulative_attempted_cells_lower_bound": (
            CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND
        ),
    }
    payload["abandoned_preregistrations"] = _abandoned_attempts()
    payload["replacement_lineage"] = dict(REPLACEMENT_LINEAGE)
    payload["capture_integrity_contract"] = capture_integrity_contract(
        str(collector_identity["sha256"])
    )

    strategy = payload.get("strategy")
    gates = payload.get("fixed_success_gates")
    if not isinstance(strategy, dict) or not isinstance(gates, dict):
        raise ReplacementPreregistrationRefusal("strategy_contract_invalid")
    replacement_manifest = replacement_screen.attempt_manifest()
    strategy["attempt_manifest"] = replacement_manifest
    strategy["attempt_manifest_sha256"] = base.canonical_sha256(replacement_manifest)
    gates["descriptive_df99_bonferroni_abs_t_threshold"] = (
        replacement_screen.BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
    )
    gates["cell_win_probability_interval"] = (
        "one_sided_wilson_family_adjusted_over_4786_attempted_cells"
    )

    identities = payload.get("source_identities")
    if not isinstance(identities, dict):
        raise ReplacementPreregistrationRefusal("source_identities_invalid")
    identities["collector_source"] = collector_identity
    identities["collector_support_source"] = _file_identity(
        COLLECTOR_SUPPORT_PATH, label="collector_support_source"
    )
    identities["screen_source"] = _file_identity(
        SCREEN_PATH, label="replacement_screen_source"
    )
    identities["screen_support_source"] = _file_identity(
        SCREEN_SUPPORT_PATH, label="screen_support_source"
    )
    identities["sealer_source"] = _file_identity(
        TOOL_PATH, label="replacement_sealer_source"
    )
    identities["base_sealer_source"] = _file_identity(
        BASE_SEALER_PATH, label="base_sealer_source"
    )
    payload["preregistration_body_sha256"] = base.canonical_sha256(payload)
    if not validate_preregistration(payload):
        raise ReplacementPreregistrationRefusal("replacement_envelope_invalid")
    return payload


def validate_preregistration(payload: Mapping[str, Any]) -> bool:
    """Validate the replacement envelope without reading mutable dependencies."""

    body = dict(payload)
    claimed = str(body.pop("preregistration_body_sha256", "")).lower()
    if not base._is_sha256(claimed) or not hmac.compare_digest(
        claimed, base.canonical_sha256(body)
    ):
        return False
    if (
        body.get("schema_version") != base.PREREGISTRATION_SCHEMA
        or body.get("research_only") is not True
        or body.get("authority") != base.FIXED_AUTHORITY_FLAGS
        or body.get("replacement_lineage") != REPLACEMENT_LINEAGE
        or body.get("abandoned_preregistrations") != _abandoned_attempts()
        or body.get("attempt_accounting")
        != {
            "prior_attempted_cells_lower_bound": PRIOR_ATTEMPTED_CELLS_LOWER_BOUND,
            "current_attempted_cells": CURRENT_ATTEMPTED_CELLS,
            "cumulative_attempted_cells_lower_bound": (
                CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND
            ),
        }
    ):
        return False
    if not _validate_static_contract(body):
        return False

    scope = body.get("scope")
    window = body.get("prospective_window")
    execution = body.get("execution_contract")
    strategy = body.get("strategy")
    gates = body.get("fixed_success_gates")
    identities = body.get("source_identities")
    if not all(
        isinstance(value, Mapping)
        for value in (scope, window, execution, strategy, gates, identities)
    ):
        return False
    assert isinstance(scope, Mapping)
    assert isinstance(window, Mapping)
    assert isinstance(execution, Mapping)
    assert isinstance(strategy, Mapping)
    assert isinstance(gates, Mapping)
    assert isinstance(identities, Mapping)
    collector_identity = identities.get("collector_source")
    collector_support_identity = identities.get("collector_support_source")
    screen_identity = identities.get("screen_source")
    screen_support_identity = identities.get("screen_support_source")
    sealer_identity = identities.get("sealer_source")
    base_identity = identities.get("base_sealer_source")
    catalog_identity = identities.get("scope_catalog_source")
    cost_capture_identity = identities.get("cost_capture")
    fee_identity = identities.get("fee_attestation")
    production_context = identities.get("production_runtime_context")
    collector_sha = (
        str(collector_identity.get("sha256") or "")
        if isinstance(collector_identity, Mapping)
        else ""
    )
    collector_support_sha = (
        str(collector_support_identity.get("sha256") or "")
        if isinstance(collector_support_identity, Mapping)
        else ""
    )
    screen_support_sha = (
        str(screen_support_identity.get("sha256") or "")
        if isinstance(screen_support_identity, Mapping)
        else ""
    )
    base_sealer_sha = (
        str(base_identity.get("sha256") or "")
        if isinstance(base_identity, Mapping)
        else ""
    )
    if (
        set(identities)
        != {
            "collector_source",
            "collector_support_source",
            "screen_source",
            "screen_support_source",
            "sealer_source",
            "base_sealer_source",
            "scope_catalog_source",
            "cost_capture",
            "fee_attestation",
            "production_runtime_context",
        }
        or body.get("capture_integrity_contract")
        != capture_integrity_contract(collector_sha)
        or not _identity_valid(collector_identity, filename=COLLECTOR_PATH.name)
        or not _identity_valid(
            collector_support_identity, filename=COLLECTOR_SUPPORT_PATH.name
        )
        or collector_support_sha != COLLECTOR_SUPPORT_SHA256
        or not _identity_valid(screen_identity, filename=SCREEN_PATH.name)
        or not _identity_valid(
            screen_support_identity, filename=SCREEN_SUPPORT_PATH.name
        )
        or screen_support_sha != SCREEN_SUPPORT_SHA256
        or not _identity_valid(sealer_identity, filename=TOOL_PATH.name)
        or not _identity_valid(base_identity, filename=BASE_SEALER_PATH.name)
        or base_sealer_sha != BASE_SEALER_SHA256
        or not _identity_valid(catalog_identity, filename="ig_mt4_catalog.py")
        or not isinstance(cost_capture_identity, Mapping)
        or not _exact_keys(
            cost_capture_identity,
            {
                "capture_json",
                "capture_mode",
                "capture_npz",
                "capture_payload_sha256",
                "scope_version",
                "venue_id",
            },
        )
        or cost_capture_identity.get("capture_mode")
        != "authenticated_same_source_db_history"
        or cost_capture_identity.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or cost_capture_identity.get("venue_id") != IG_MT4_VENUE_ID
        or not base._is_sha256(cost_capture_identity.get("capture_payload_sha256"))
        or not _identity_valid(
            cost_capture_identity.get("capture_json"),
            filename="ig_mt4_bid_ask_capture.json",
        )
        or not _identity_valid(
            cost_capture_identity.get("capture_npz"),
            filename="ig_mt4_bid_ask_samples.npz",
        )
        or not isinstance(fee_identity, Mapping)
        or not _exact_keys(
            fee_identity,
            {
                "account_currency",
                "attestation",
                "attested_at_utc",
                "effective_at_utc",
                "operator_attestation_sha256",
                "source_documents",
            },
        )
        or fee_identity.get("account_currency") != "USD"
        or not _identity_valid(
            fee_identity.get("attestation"),
            filename="mtvclc_fee_attestation.json",
        )
        or not base._is_sha256(fee_identity.get("operator_attestation_sha256"))
        or not isinstance(production_context, Mapping)
        or production_context.get("relationship")
        != "context_only_successor_not_integrated_or_authorized"
    ):
        return False
    documents = fee_identity.get("source_documents")
    if not isinstance(documents, list) or len(documents) != len(
        base.SOURCE_DOCUMENT_URLS
    ):
        return False
    observed_roles: set[str] = set()
    for document in documents:
        if not isinstance(document, Mapping) or not _exact_keys(
            document,
            {"filename", "sha256", "size_bytes", "role", "url", "retrieved_at_utc"},
        ):
            return False
        role = str(document.get("role") or "")
        size = document.get("size_bytes")
        if (
            role in observed_roles
            or role not in base.SOURCE_DOCUMENT_URLS
            or document.get("url") != base.SOURCE_DOCUMENT_URLS[role]
            or not str(document.get("filename") or "")
            or not base._is_sha256(document.get("sha256"))
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
        ):
            return False
        observed_roles.add(role)
    if observed_roles != set(base.SOURCE_DOCUMENT_URLS):
        return False
    try:
        sealed = base._parse_utc_second(body.get("sealed_at_utc"), label="sealed")
        t0 = base._parse_utc_second(window.get("t0_utc_inclusive"), label="t0")
        end = base._parse_utc_second(window.get("end_utc_exclusive"), label="end")
    except base.PreregistrationRefusal:
        return False
    return bool(
        t0 > sealed and end - t0 == timedelta(days=base.PROSPECTIVE_WINDOW_DAYS)
    )


def _validate_output_root(
    output_root: str | Path, *, input_paths: Sequence[str | Path]
) -> Path:
    try:
        return base._validate_output_root(output_root, input_paths=input_paths)
    except base.PreregistrationRefusal as exc:
        raise ReplacementPreregistrationRefusal(str(exc)) from None


def atomic_publish(
    *,
    output_root: str | Path,
    payload: Mapping[str, Any],
    input_paths: Sequence[str | Path],
) -> Path:
    """Publish once with no overwrite primitive."""

    if not validate_preregistration(payload):
        raise ReplacementPreregistrationRefusal("replacement_envelope_invalid")
    root = _validate_output_root(output_root, input_paths=input_paths)
    digest = str(payload["preregistration_body_sha256"])
    target = root / f"mtvclc_v1_preregistration_{digest}.json"
    if target.exists() or target.is_symlink() or base._is_reparse_point(target):
        raise ReplacementPreregistrationRefusal("output_already_exists")
    encoded = (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temp = root / f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    published = False
    try:
        descriptor = os.open(temp, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, target)
        except FileExistsError as exc:
            raise ReplacementPreregistrationRefusal("output_already_exists") from exc
        except OSError as exc:
            raise ReplacementPreregistrationRefusal(
                "atomic_no_overwrite_publish_failed"
            ) from exc
        published = True
        if target.read_bytes() != encoded:
            raise ReplacementPreregistrationRefusal("output_verification_failed")
        temp.unlink()
        os.chmod(target, 0o400)
    except Exception:
        if published:
            try:
                os.chmod(target, 0o600)
                target.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        if temp.exists():
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seal a new authority-free restart-resilient MTVCLC 180-day declaration."
        )
    )
    parser.add_argument("--cost-capture-json", required=True)
    parser.add_argument("--cost-capture-npz", required=True)
    parser.add_argument("--fee-attestation", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument(
        "--start-delay-seconds",
        type=int,
        default=base.DEFAULT_START_DELAY_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = build_preregistration(
            cost_capture_json=args.cost_capture_json,
            cost_capture_npz=args.cost_capture_npz,
            fee_attestation=args.fee_attestation,
            sealed_at=datetime.now(UTC),
            start_delay_seconds=args.start_delay_seconds,
        )
        output = atomic_publish(
            output_root=args.output_root,
            payload=payload,
            input_paths=(
                args.cost_capture_json,
                args.cost_capture_npz,
                args.fee_attestation,
            ),
        )
    except ReplacementPreregistrationRefusal as exc:
        print(f"replacement preregistration refused: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "preregistration_body_sha256": payload["preregistration_body_sha256"],
                "artifact_file_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "authority": base.FIXED_AUTHORITY_FLAGS,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
