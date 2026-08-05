"""Verify a completed MTVCLC prospective capture without evaluating outcomes.

This tool is deliberately post-window and offline.  It accepts only a sealed
preregistration plus the collector's local manifest/chunk tree.  It has no
network, credential, database, broker, signer, registry, activation, or order
surface.  The verifier streams the manifest so the fixed 180-day capture does
not have to fit in memory.

The resulting handoff is an integrity inventory for a later physically
isolated evaluator.  It is not strategy evidence and every authority bit is
false.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import time
from typing import Any, Mapping, Sequence


PREREGISTRATION_SCHEMA = "fxstack.scalp.mtvclc_preregistration.v1"
COLLECTOR_SCHEMA = "fxstack.external_ig_mt4_m1_activity_collector.v2"
REPLACEMENT_COLLECTOR_SCHEMA = (
    "fxstack.external_ig_mt4_m1_activity_resilient_collector.v2"
)
CHUNK_SCHEMA = "fxstack.external_ig_mt4_m1_activity_chunk.v2"
MANIFEST_SCHEMA = "fxstack.external_ig_mt4_m1_activity_manifest_entry.v2"
HANDOFF_SCHEMA = "fxstack.scalp.mtvclc_capture_handoff.v1"

PROFILE_AUTO = "auto"
PROFILE_BASE = "base"
PROFILE_REPLACEMENT = "replacement_resilient_v1"

STRATEGY_ID = "ig_mt4_tick_volume_close_location_continuation"
STRATEGY_VERSION = "mtvclc.v1"
CONFIG_ID = "mtvclc_v1_vq90_cl80_b1k_h30_t4k_s8k"
FROZEN_CONFIGURATION = {
    "baseline_m1_bars": 240,
    "volume_quantile": 0.90,
    "close_location_threshold": 0.80,
    "target_cost_multiple": 4.0,
    "stop_cost_multiple": 8.0,
    "outcome_horizon_m1_bars": 30,
    "maximum_entry_delay_seconds": 5,
    "maximum_quote_gap_seconds": 5,
    "rollover_entry_blackout_start_second": 73_200,
    "rollover_entry_blackout_end_second": 79_800,
}
FROZEN_CONFIG_SHA256 = (
    "aac8e4bd98d71243b41993983d63ab1da1f8836b74b9243c7e60232f0eb00701"
)
SOURCE_CONTRACT_ID = (
    "authenticated_ig_mt4_bid_m1_ohlc_ivolume_plus_bid_ask_transport_snapshots.v1"
)
ACTIVITY_METRIC_ID = "mt4_m1_ivolume_tick_volume.v1"
MARKET_SOURCE_SCHEMA = "fxstack_authenticated_broker_market_source_v2"
SCOPE_VERSION = "fxstack.ig_mt4.scalp_scope.v3"
VENUE_ID = "ig_mt4"
BRIDGE_PROTOCOL_VERSION = "v3.0.0"
PRICE_BASIS = "mt4_bid_ohlc_v1"
VOLUME_SOURCE = "mt4_ivolume_tick_count_v1"
TIMEFRAME = "M1"

PROSPECTIVE_WINDOW_DAYS = 180
MINIMUM_M1_BARS = 241
MAXIMUM_QUOTE_GAP_SECONDS = 5.0
# The sealed collector stops starting quote cycles inside its three-request,
# five-second default request budget.  These fixed edge tolerances leave room
# for that bounded final cycle without allowing an early-stopped run to pass.
MAXIMUM_WINDOW_EDGE_LAG_SECONDS = 30.0
MAXIMUM_FINAL_BAR_LAG_SECONDS = 300.0

MANIFEST_FILENAME = "manifest.sha256.jsonl"
CHUNK_DIRECTORY = "chunks"
MAXIMUM_PREREGISTRATION_BYTES = 1024 * 1024
MAXIMUM_MANIFEST_LINE_BYTES = 4 * 1024 * 1024
MAXIMUM_CHUNK_BYTES = 32 * 1024 * 1024
ZERO_SHA256 = "0" * 64

ACTIVE_HOUR_JOURNAL_FILENAME = "active-hour.journal.sha256.jsonl"
DATA_WRITER_LOCK_FILENAME = "collector-data-writer.lock"
GUARD_IDENTITY_FILENAME = "collector-guard.identity.resilient.v1.json"
GUARD_IDENTITY_SCHEMA = "fxstack.mtvclc_collector_guard_identity.resilient.v1"
SUPERVISION_DIRECTORY = "supervision-resilient"
SUPERVISOR_LOCK_FILENAME = "collector-writer.lock"
SUPERVISOR_STDOUT_FILENAME = "collector.stdout.log"
SUPERVISOR_STDERR_FILENAME = "collector.stderr.log"

SYMBOLS: tuple[str, ...] = (
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
SYMBOL_SET = frozenset(SYMBOLS)

FALSE_AUTHORITY = {
    "research_process_authorized": False,
    "outcome_access_authorized": False,
    "success_claim_authorized": False,
    "promotion_authorized": False,
    "activation_authorized": False,
    "registry_write_authorized": False,
    "runtime_authorized": False,
    "issuer_authorized": False,
    "signature_authorized": False,
    "broker_access_authorized": False,
    "order_authorized": False,
}

REPLACEMENT_ATTEMPT_ACCOUNTING = {
    "prior_attempted_cells_lower_bound": 4_742,
    "current_attempted_cells": 44,
    "cumulative_attempted_cells_lower_bound": 4_786,
}
REPLACEMENT_BONFERRONI_T_THRESHOLD = 4.645748208417252
REPLACEMENT_LINEAGE = {
    "replaces_preregistration_body_sha256": (
        "07b78ce6d697a61db308560c325f547e90984143f2b71c1613bb8ee12b9f879c"
    ),
    "replacement_reason": (
        "late_backfill_watermark_contract_hardened_before_evaluation"
    ),
    "old_window_restart_or_extension": False,
    "new_independent_window_required": True,
    "old_capture_used_for_signal_outcome_or_performance_selection": False,
    "old_attempt_counted_in_multiplicity_family": True,
}
PRESERVED_COLLECTOR_SHA256 = (
    "87af2452ae3f0f3964c35b2904ad35fda28f3675e1f614e941ee46fd693bda5d"
)
PRESERVED_COLLECTOR_SIZE_BYTES = 80_405
REPLACEMENT_COLLECTOR_SHA256 = (
    "b38e69933e4910956dc92477252d33f4d5a92458e70036520654bc53102357f5"
)
REPLACEMENT_COLLECTOR_SIZE_BYTES = 77_496
PRESERVED_SCREEN_SHA256 = (
    "7e3dd3e829a4429e1316be2925f17d6ec3c9c0e3257e529cefd153e660558059"
)
PRESERVED_SCREEN_SIZE_BYTES = 40_614
REPLACEMENT_SCREEN_SHA256 = (
    "e67d39b2bc0cce3699bcab9fa5056207cfcd7a3cf4e8c40a29aaca73cfb2d807"
)
REPLACEMENT_SCREEN_SIZE_BYTES = 7_648
BASE_SEALER_SHA256 = "e4248f6f8484435a7e1bb4994363811edf32335ce04f31bfa25cbe844eba0ef1"
BASE_SEALER_SIZE_BYTES = 44_047
REPLACEMENT_SEALER_SHA256 = (
    "94adcb20a3068b7c7a914072412f2b5bfc7050a1e8a6748ff385aa5931b08ab1"
)
REPLACEMENT_SEALER_SIZE_BYTES = 35_609
RESILIENT_CONTINUITY_INSPECTOR_SHA256 = (
    "3dbdd701c0632acf66c71dfef8c3d8d10c468d03a9160ded6ec19a84d8ef309e"
)

FIRST_REPLACED_ATTEMPT = {
    "preregistration_body_sha256": (
        "d80e6cc9f05726ff2d2e851890ec06ca1b2df8e08efb4066bc83e31c216f17f3"
    ),
    "artifact_file_sha256": (
        "566c85789fdf1f8639a4c010421d27f3b02e6dd2f9a1912968ca8f9b500c51b3"
    ),
    "reason": "authenticated_mt4_restart_revised_completed_bar_history",
    "eligible_observations_emitted": True,
    "manifest_entries_emitted": 1_448,
    "manifest_file_sha256": (
        "86a28670961de97980a09018ea69a9190223459581ba5bfdf00a01aef0b22a6e"
    ),
    "final_manifest_entry_sha256": (
        "ec8f23c7b35a79ccce1cfb269ccd287301800c7e3c845710b775001147f1b163"
    ),
    "attempted_cells_increment": 44,
    "signal_evaluation_performed": False,
    "outcome_evaluation_performed": False,
    "performance_statistics_computed": False,
    "success_claim_evaluated": False,
}
SECOND_REPLACED_ATTEMPT = {
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
REPLACED_ATTEMPT = FIRST_REPLACED_ATTEMPT
REPLACEMENT_ABANDONED_PREREGISTRATIONS = [
    {
        "preregistration_body_sha256": (
            "5ff534bff7013d5836ab0034d2ef13d0739c56046d4d2637a5fd73df917c97d7"
        ),
        "artifact_file_sha256": (
            "fd32199fe13f6989743e7ad1b607ef39c2129bb3a7d20d24661eb13793347f9b"
        ),
        "reason": "atomic_publish_temp_hardlink_retained",
        "eligible_observations_emitted": False,
        "manifest_entries_emitted": 0,
        "attempted_cells_increment": 0,
    },
    {
        "preregistration_body_sha256": (
            "0511ee9c98204edc6dfd5166abade98eb47ec4f10c6364c36f504c7151853d96"
        ),
        "artifact_file_sha256": (
            "3cf1b36e4cd311425515ba8fe2268626f0c8e84ed750264e9793dd4e83c283a3"
        ),
        "reason": "first_cycle_refused_opaque_broker_token_misclassified_as_utc",
        "eligible_observations_emitted": False,
        "manifest_entries_emitted": 0,
        "attempted_cells_increment": 0,
    },
    FIRST_REPLACED_ATTEMPT,
    SECOND_REPLACED_ATTEMPT,
]

REPLACEMENT_CAPTURE_INTEGRITY_POLICY = {
    "schema_version": "fxstack.scalp.mtvclc_capture_integrity_contract.v2",
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
    "active_hour_journal_filename": ACTIVE_HOUR_JOURNAL_FILENAME,
    "portable_chunk_schema_version": CHUNK_SCHEMA,
    "portable_manifest_schema_version": MANIFEST_SCHEMA,
    "exclusive_output_data_writer_lock_required": True,
}

SOURCE_DOCUMENT_URLS = {
    "ig_mt4_forex_product_details": (
        "https://www.ig.com/uk/help-and-support/articles/"
        "681915-mt4-forex-product-details"
    ),
    "ig_mt4_crypto_product_details": (
        "https://www.ig.com/en/help-and-support/articles/"
        "681844-cryptocurrencies-mt4-product-details"
    ),
    "ig_spread_betting_cfd_product_details": (
        "https://www.ig.com/uk/help-and-support/articles/"
        "682149-what-are-ig-s-spread-betting-and-cfd-product-details-for-each-market"
    ),
}

MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "previous_entry_sha256",
        "chunk_path",
        "chunk_sha256",
        "chunk_size_bytes",
        "chunk_schema_version",
        "utc_hour",
        "segment_index",
        "market_source_id",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "bar_rows",
        "quote_rows",
        "last_bar_epoch_by_symbol",
        "last_tick_sequence_by_symbol",
        "last_tick_transport_epoch_by_symbol",
        "last_tick_snapshot_sha256_by_symbol",
        "manifest_entry_sha256",
    }
)

CHUNK_FIELDS = frozenset(
    {
        "schema_version",
        "collector_schema_version",
        "source_contract_id",
        "activity_metric_id",
        "scope_version",
        "symbol_scope",
        "timeframe",
        "minimum_m1_history_bars",
        "maximum_quote_gap_seconds",
        "requested_bar_limit",
        "configured_tick_interval_seconds",
        "utc_hour",
        "segment_index",
        "collector_cycle_started_at_epoch",
        "collector_cycle_completed_at_epoch",
        "observed_at_epoch",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "source",
        "bars",
        "quotes",
        "last_bar_epoch_by_symbol",
        "last_tick_sequence_by_symbol",
        "last_tick_transport_epoch_by_symbol",
        "last_tick_snapshot_sha256_by_symbol",
        "collection_only",
        "evaluation_performed",
        "success_claim_authorized",
        "authority_granted",
        "activation_authorized",
        "order_authorized",
    }
)

SOURCE_FIELDS = frozenset(
    {
        "market_source_schema",
        "market_source_id",
        "market_source_authenticated",
        "broker_account_mode",
        "broker_venue_id",
        "broker_account_scope_sha256",
        "producer_identity_sha256",
        "producer_instance_id_sha256",
        "terminal_lease_scope_sha256",
        "credential_generation_id_sha256",
        "bridge_protocol_version",
    }
)

BAR_FIELDS = frozenset(
    {
        "symbol",
        "minute_epoch",
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "tick_volume",
        "price_basis",
        "volume_source",
    }
)

QUOTE_FIELDS = frozenset(
    {
        "symbol",
        "observation_sequence",
        "observation_epoch",
        "observed_at_epoch",
        "bid",
        "ask",
        "transport_received_at_epoch",
        "market_event_received_at_epoch",
        "market_event_sequence",
        "source_event_token_sha256",
        "snapshot_sha256",
    }
)


class HandoffRefusal(RuntimeError):
    """Stable fail-closed refusal emitted without publishing a handoff."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise HandoffRefusal("noncanonical_json_value") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HandoffRefusal("json_duplicate_key")
        result[key] = value
    return result


def _strict_json_object(raw: bytes, *, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                HandoffRefusal("json_nonfinite_value")
            ),
        )
    except HandoffRefusal:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffRefusal(reason) from exc
    if not isinstance(value, dict):
        raise HandoffRefusal(reason)
    return value


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _finite(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HandoffRefusal(reason)
    number = float(value)
    if not math.isfinite(number):
        raise HandoffRefusal(reason)
    return number


def _positive(value: Any, reason: str) -> float:
    number = _finite(value, reason)
    if number <= 0.0:
        raise HandoffRefusal(reason)
    return number


def _strict_int(value: Any, reason: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HandoffRefusal(reason)
    return int(value)


def _parse_utc_second(value: Any, reason: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise HandoffRefusal(reason) from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != text:
        raise HandoffRefusal(reason)
    return parsed


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def _regular_file(
    path: str | Path,
    *,
    reason: str,
    maximum_bytes: int | None = None,
) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or _is_reparse_point(candidate):
        raise HandoffRefusal(reason)
    try:
        resolved = candidate.resolve(strict=True)
        size = resolved.stat().st_size
    except OSError as exc:
        raise HandoffRefusal(reason) from exc
    if (
        not resolved.is_file()
        or resolved.is_symlink()
        or _is_reparse_point(resolved)
        or size <= 0
        or (maximum_bytes is not None and size > maximum_bytes)
    ):
        raise HandoffRefusal(reason)
    return resolved


def _read_regular_file(
    path: str | Path,
    *,
    reason: str,
    maximum_bytes: int,
) -> tuple[Path, bytes]:
    resolved = _regular_file(path, reason=reason, maximum_bytes=maximum_bytes)
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise HandoffRefusal(reason) from exc
    if not raw or len(raw) > maximum_bytes:
        raise HandoffRefusal(reason)
    return resolved, raw


def _identity_row_valid(value: Any, *, filename: str | None = None) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "filename",
        "sha256",
        "size_bytes",
    }:
        return False
    if filename is not None and value.get("filename") != filename:
        return False
    size = value.get("size_bytes")
    return (
        _is_sha256(value.get("sha256"))
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size > 0
    )


def _strict_identity_row_valid(
    value: Any,
    *,
    filename: str,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> bool:
    """Validate one path-free identity without pretending its bytes were transferred."""

    if not _identity_row_valid(value, filename=filename):
        return False
    assert isinstance(value, Mapping)
    observed_sha = value.get("sha256")
    observed_size = value.get("size_bytes")
    return bool(
        isinstance(observed_sha, str)
        and observed_sha == observed_sha.lower()
        and (sha256 is None or hmac.compare_digest(observed_sha, sha256))
        and (size_bytes is None or observed_size == size_bytes)
    )


def _replacement_attempt_manifest() -> dict[str, Any]:
    return {
        "schema_version": "fxstack.scalp.mtvclc_attempt_manifest.v1",
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "config_ids": [CONFIG_ID],
        "parameter_configurations": 1,
        "configuration": {
            "baseline_m1_bars": 240,
            "volume_quantile": 0.90,
            "volume_quantile_method": "type7",
            "close_location_threshold": 0.80,
            "body_floor": "recorded_cost_bps",
            "recorded_cost_formula": (
                "ig_mt4_p90_spread_bps+commission_bps_per_round_trip+"
                "financing_bps_per_trade+1.0bps_adverse_execution_debit"
            ),
            "convert_on_close_treatment": (
                "debit 0.5% of absolute realized gross quote-currency P/L "
                "when pnl_currency differs from the IG account currency"
            ),
            "convert_on_close_charge_fraction": 0.005,
            "target_cost_multiple": 4.0,
            "stop_cost_multiple": 8.0,
            "outcome_horizon_m1_bars": 30,
            "maximum_entry_delay_seconds": 5,
            "maximum_quote_gap_seconds": 5,
            "quote_gap_clock": "authenticated_transport_snapshot_epoch",
            "repeated_unchanged_broker_event_allowed": True,
            "maximum_entries_per_symbol_utc_day": 1,
            "rollover_guard": {
                "funding_boundary_london_minute": 1_320,
                "earliest_funding_boundary_utc_minute": 1_260,
                "latest_funding_boundary_utc_minute": 1_320,
                "maximum_holding_seconds": 1_800,
                "close_slack_seconds": 600,
                "entry_blackout_start_second": 73_200,
                "entry_blackout_end_second": 79_800,
                "entry_blackout_utc": "[20:20:00,22:10:00)",
                "half_open": True,
                "signals_inside_blackout_reserve": False,
            },
            "execution_type": "market",
            "pending_orders_forbidden": True,
            "base_cost_break_even_win_probability": 0.75,
            "two_x_cost_break_even_win_probability": 10.0 / 12.0,
        },
        "symbol_scope": list(SYMBOLS),
        "sides": ["BUY", "SELL"],
        **REPLACEMENT_ATTEMPT_ACCOUNTING,
        "descriptive_df99_bonferroni_abs_t_threshold": (
            REPLACEMENT_BONFERRONI_T_THRESHOLD
        ),
        "anytime_valid": False,
        "loop_until_pass_forbidden": True,
        "source_contract_id": SOURCE_CONTRACT_ID,
        "activity_metric_id": ACTIVITY_METRIC_ID,
        "provider_volume_interchangeable": False,
        "mt4_history_export_required": True,
        "contemporaneous_tick_outcomes_required": True,
        "historical_synthesized_ask_bars_forbidden": True,
        "research_only": True,
        "success_claim_authorized": False,
        "holdout_access_authorized": False,
        "activation_authorized": False,
        "order_authorized": False,
        "win_probability_familywise_attempted_cells": 4_786,
        "win_probability_alpha_allocation": "one_sided_0.05_over_4786",
    }


REPLACEMENT_ATTEMPT_MANIFEST = _replacement_attempt_manifest()


def _replacement_capture_integrity_contract(
    collector_source_sha256: str,
) -> dict[str, Any]:
    return {
        **REPLACEMENT_CAPTURE_INTEGRITY_POLICY,
        "collector_source_sha256": collector_source_sha256,
        "collector_support_source_sha256": PRESERVED_COLLECTOR_SHA256,
    }


def _validate_replacement_cost_policy(value: Any) -> None:
    fields = {
        "formula",
        "conversion_treatment",
        "geometry_uses_pre_conversion_cost",
        "final_cell_mean_uses_conversion_adjusted_net",
        "unknown_commission_financing_or_conversion_refuses_evaluation",
        "fee_schedule_change_or_source_uncertainty_refuses_evaluation",
        "symbols",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise HandoffRefusal("preregistration_cost_policy_invalid")
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
        or any(
            value.get(field) is not True
            for field in (
                "geometry_uses_pre_conversion_cost",
                "final_cell_mean_uses_conversion_adjusted_net",
                "unknown_commission_financing_or_conversion_refuses_evaluation",
                "fee_schedule_change_or_source_uncertainty_refuses_evaluation",
            )
        )
    ):
        raise HandoffRefusal("preregistration_cost_policy_invalid")
    symbols = value.get("symbols")
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
    if not isinstance(symbols, Mapping) or set(symbols) != SYMBOL_SET:
        raise HandoffRefusal("preregistration_cost_policy_invalid")
    for symbol in SYMBOLS:
        row = symbols.get(symbol)
        if not isinstance(row, Mapping) or set(row) != row_fields:
            raise HandoffRefusal("preregistration_cost_policy_invalid")
        p90 = _finite(
            row.get("p90_ig_spread_bps"), "preregistration_cost_policy_invalid"
        )
        commission = _finite(
            row.get("commission_bps_per_round_trip"),
            "preregistration_cost_policy_invalid",
        )
        financing = _finite(
            row.get("financing_bps_per_trade"),
            "preregistration_cost_policy_invalid",
        )
        adverse = _finite(
            row.get("fixed_adverse_execution_debit_bps"),
            "preregistration_cost_policy_invalid",
        )
        geometry = _finite(
            row.get("pre_conversion_geometry_cost_bps"),
            "preregistration_cost_policy_invalid",
        )
        conversion = _finite(
            row.get("conversion_rate_of_absolute_profit_or_loss"),
            "preregistration_cost_policy_invalid",
        )
        screen_conversion = _finite(
            row.get("convert_on_close_charge_fraction_for_screen"),
            "preregistration_cost_policy_invalid",
        )
        p_star = _finite(
            row.get("conversion_adjusted_break_even_win_probability"),
            "preregistration_cost_policy_invalid",
        )
        pnl_currency = symbol[3:]
        conversion_applies = pnl_currency != "USD"
        if (
            p90 <= 0.0
            or min(commission, financing, adverse, geometry, conversion) < 0.0
            or adverse != 1.0
            or conversion != 0.005
            or screen_conversion != (0.005 if conversion_applies else 0.0)
            or not 0.0 < p_star < 1.0
            or row.get("profit_loss_currency") != pnl_currency
            or row.get("account_currency") != "USD"
            or row.get("conversion_applies") is not conversion_applies
            or row.get("commission_status")
            not in {"explicit_source_attested", "conservative_upper_bound"}
            or row.get("financing_status")
            not in {
                "structurally_avoided_by_fixed_rollover_guard",
                "conservative_upper_bound",
            }
            or (
                commission == 0.0
                and row.get("commission_status") != "explicit_source_attested"
            )
            or (
                financing == 0.0
                and row.get("financing_status")
                != "structurally_avoided_by_fixed_rollover_guard"
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
            raise HandoffRefusal("preregistration_cost_policy_invalid")
        target = 4.0 * geometry
        stop = 8.0 * geometry
        expected_p_star = (stop * (1.0 + screen_conversion) + geometry) / (
            target * (1.0 - screen_conversion) + stop * (1.0 + screen_conversion)
        )
        if not math.isclose(p_star, expected_p_star, rel_tol=0.0, abs_tol=1e-15):
            raise HandoffRefusal("preregistration_cost_policy_invalid")


def _validate_engine_identity(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "engine_sha256",
        "component_sha256",
        "schema_version",
    }:
        return False
    if value.get(
        "schema_version"
    ) != "fxstack.production_scalp_engine_identity.v3" or not _is_sha256(
        value.get("engine_sha256")
    ):
        return False
    components = value.get("component_sha256")
    if not isinstance(components, list) or not components:
        return False
    normalized: list[dict[str, str]] = []
    observed_paths: set[str] = set()
    for row in components:
        if not isinstance(row, list) or len(row) != 2:
            return False
        relative, digest = row
        if (
            not isinstance(relative, str)
            or not relative
            or relative in observed_paths
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or not isinstance(digest, str)
            or digest != digest.lower()
            or not _is_sha256(digest)
        ):
            return False
        observed_paths.add(relative)
        normalized.append({"path": relative, "sha256": digest})
    expected = canonical_sha256(
        {
            "schema_version": "fxstack.production_scalp_engine_identity.v3",
            "components": normalized,
        }
    )
    return hmac.compare_digest(str(value.get("engine_sha256")), expected)


def _validate_replacement_source_identities(value: Any) -> str:
    expected_fields = {
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
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise HandoffRefusal("preregistration_identity_contract_invalid")
    collector = value.get("collector_source")
    if not _strict_identity_row_valid(
        collector,
        filename="capture_ig_mt4_m1_activity_resilient.py",
        sha256=REPLACEMENT_COLLECTOR_SHA256,
        size_bytes=REPLACEMENT_COLLECTOR_SIZE_BYTES,
    ):
        raise HandoffRefusal("preregistration_identity_contract_invalid")
    assert isinstance(collector, Mapping)
    collector_sha = str(collector.get("sha256"))
    if (
        not _strict_identity_row_valid(
            value.get("collector_support_source"),
            filename="capture_ig_mt4_m1_activity.py",
            sha256=PRESERVED_COLLECTOR_SHA256,
            size_bytes=PRESERVED_COLLECTOR_SIZE_BYTES,
        )
        or not _strict_identity_row_valid(
            value.get("screen_source"),
            filename=(
                "screen_mt4_tick_volume_close_location_continuation_replacement.py"
            ),
            sha256=REPLACEMENT_SCREEN_SHA256,
            size_bytes=REPLACEMENT_SCREEN_SIZE_BYTES,
        )
        or not _strict_identity_row_valid(
            value.get("screen_support_source"),
            filename="screen_mt4_tick_volume_close_location_continuation.py",
            sha256=PRESERVED_SCREEN_SHA256,
            size_bytes=PRESERVED_SCREEN_SIZE_BYTES,
        )
        or not _strict_identity_row_valid(
            value.get("sealer_source"),
            filename="seal_mt4_tick_volume_preregistration_resilient.py",
            sha256=REPLACEMENT_SEALER_SHA256,
            size_bytes=REPLACEMENT_SEALER_SIZE_BYTES,
        )
        or not _strict_identity_row_valid(
            value.get("base_sealer_source"),
            filename="seal_mt4_tick_volume_preregistration.py",
            sha256=BASE_SEALER_SHA256,
            size_bytes=BASE_SEALER_SIZE_BYTES,
        )
        or not _strict_identity_row_valid(
            value.get("scope_catalog_source"), filename="ig_mt4_catalog.py"
        )
    ):
        raise HandoffRefusal("preregistration_identity_contract_invalid")

    cost_capture = value.get("cost_capture")
    if (
        not isinstance(cost_capture, Mapping)
        or set(cost_capture)
        != {
            "capture_json",
            "capture_mode",
            "capture_npz",
            "capture_payload_sha256",
            "scope_version",
            "venue_id",
        }
        or cost_capture.get("capture_mode") != "authenticated_same_source_db_history"
        or cost_capture.get("scope_version") != SCOPE_VERSION
        or cost_capture.get("venue_id") != VENUE_ID
        or not _is_sha256(cost_capture.get("capture_payload_sha256"))
        or not _strict_identity_row_valid(
            cost_capture.get("capture_json"),
            filename="ig_mt4_bid_ask_capture.json",
        )
        or not _strict_identity_row_valid(
            cost_capture.get("capture_npz"),
            filename="ig_mt4_bid_ask_samples.npz",
        )
    ):
        raise HandoffRefusal("preregistration_identity_contract_invalid")

    fee = value.get("fee_attestation")
    if (
        not isinstance(fee, Mapping)
        or set(fee)
        != {
            "account_currency",
            "attestation",
            "attested_at_utc",
            "effective_at_utc",
            "operator_attestation_sha256",
            "source_documents",
        }
        or fee.get("account_currency") != "USD"
        or not _strict_identity_row_valid(
            fee.get("attestation"), filename="mtvclc_fee_attestation.json"
        )
        or not _is_sha256(fee.get("operator_attestation_sha256"))
    ):
        raise HandoffRefusal("preregistration_identity_contract_invalid")
    _parse_utc_second(
        fee.get("attested_at_utc"), "preregistration_identity_contract_invalid"
    )
    _parse_utc_second(
        fee.get("effective_at_utc"), "preregistration_identity_contract_invalid"
    )
    documents = fee.get("source_documents")
    if not isinstance(documents, list) or len(documents) != len(SOURCE_DOCUMENT_URLS):
        raise HandoffRefusal("preregistration_identity_contract_invalid")
    observed_roles: set[str] = set()
    for document in documents:
        if not isinstance(document, Mapping) or set(document) != {
            "filename",
            "sha256",
            "size_bytes",
            "role",
            "url",
            "retrieved_at_utc",
        }:
            raise HandoffRefusal("preregistration_identity_contract_invalid")
        role = str(document.get("role") or "")
        filename = str(document.get("filename") or "")
        size = document.get("size_bytes")
        if (
            role in observed_roles
            or role not in SOURCE_DOCUMENT_URLS
            or document.get("url") != SOURCE_DOCUMENT_URLS.get(role)
            or not filename
            or Path(filename).name != filename
            or not isinstance(document.get("sha256"), str)
            or document.get("sha256") != str(document.get("sha256")).lower()
            or not _is_sha256(document.get("sha256"))
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
        ):
            raise HandoffRefusal("preregistration_identity_contract_invalid")
        _parse_utc_second(
            document.get("retrieved_at_utc"),
            "preregistration_identity_contract_invalid",
        )
        observed_roles.add(role)
    if observed_roles != set(SOURCE_DOCUMENT_URLS):
        raise HandoffRefusal("preregistration_identity_contract_invalid")

    context = value.get("production_runtime_context")
    if (
        not isinstance(context, Mapping)
        or set(context)
        != {
            "relationship",
            "engine_identity",
            "active_strategy_family_context",
            "active_strategy_version_context",
            "active_policy_config_sha256_context",
        }
        or context.get("relationship")
        != "context_only_successor_not_integrated_or_authorized"
        or context.get("active_strategy_family_context") != "scalp_dislocation"
        or context.get("active_strategy_version_context")
        != "fxstack.strategy.scalp_dislocation.v4"
        or not _is_sha256(context.get("active_policy_config_sha256_context"))
        or not _validate_engine_identity(context.get("engine_identity"))
    ):
        raise HandoffRefusal("preregistration_identity_contract_invalid")
    return collector_sha


def _validate_replacement_preregistration(
    body: Mapping[str, Any],
) -> tuple[datetime, datetime]:
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
    if set(body) != expected_top:
        raise HandoffRefusal("preregistration_replacement_contract_invalid")
    identities = body.get("source_identities")
    collector_sha = _validate_replacement_source_identities(identities)
    expected_manifest = _replacement_attempt_manifest()
    expected_strategy = {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "config_id": CONFIG_ID,
        "config_sha256": FROZEN_CONFIG_SHA256,
        "source_contract_id": SOURCE_CONTRACT_ID,
        "activity_metric_id": ACTIVITY_METRIC_ID,
        "attempt_manifest": expected_manifest,
        "attempt_manifest_sha256": canonical_sha256(expected_manifest),
    }
    expected_scope = {
        "venue_id": VENUE_ID,
        "scope_version": SCOPE_VERSION,
        "ordered_symbols": list(SYMBOLS),
        "sides": ["BUY", "SELL"],
        "cell_order": [
            {"config_id": CONFIG_ID, "symbol": symbol, "side": side}
            for symbol in SYMBOLS
            for side in ("BUY", "SELL")
        ],
    }
    expected_execution = {
        "entry_type": "immediate_market",
        "pending_orders_forbidden": True,
        "maximum_entries_per_symbol_utc_day": 1,
        "outcome_horizon_m1_bars": 30,
        "rollover_entry_blackout_utc": "[20:20:00,22:10:00)",
        "rollover_entry_blackout_half_open": True,
        "signals_inside_blackout_reserve": False,
    }
    expected_gates = {
        "all_44_cells_must_pass": True,
        "minimum_trades_per_cell": 30,
        "minimum_independent_utc_days_per_cell": 10,
        "cell_win_probability_interval": (
            "one_sided_wilson_family_adjusted_over_4786_attempted_cells"
        ),
        "cell_win_probability_family_confidence": 0.95,
        "cell_win_probability_lower_bound_strictly_greater_than": (
            "the_exact_per_symbol_conversion_adjusted_break_even_win_"
            "probability_in_cost_policy.symbols"
        ),
        "minimum_unconverted_break_even_win_probability": 0.75,
        "cell_conversion_adjusted_mean_net_bps_strictly_greater_than": 0.0,
        "minimum_total_trades": 300,
        "minimum_total_independent_utc_days": 60,
        "source_scope_ready_required": True,
        "source_errors_required": [],
        "descriptive_df99_bonferroni_abs_t_threshold": (
            REPLACEMENT_BONFERRONI_T_THRESHOLD
        ),
    }
    expected_isolation = {
        "artifact_grants_no_input_or_outcome_access": True,
        "prospective_collection_is_a_separate_get_only_operator_action": True,
        "outcome_evaluation_requires_a_physically_isolated_research_host": True,
        "production_database_bridge_broker_credentials_registry_and_issuer_"
        "must_not_be_mounted": True,
        "transfer_to_isolation_must_verify_preregistration_body_sha256": True,
    }
    if (
        body.get("schema_version") != PREREGISTRATION_SCHEMA
        or body.get("research_only") is not True
        or body.get("authority") != FALSE_AUTHORITY
        or body.get("strategy") != expected_strategy
        or canonical_sha256(FROZEN_CONFIGURATION) != FROZEN_CONFIG_SHA256
        or body.get("scope") != expected_scope
        or body.get("attempt_accounting") != REPLACEMENT_ATTEMPT_ACCOUNTING
        or body.get("abandoned_preregistrations")
        != REPLACEMENT_ABANDONED_PREREGISTRATIONS
        or body.get("replacement_lineage") != REPLACEMENT_LINEAGE
        or body.get("capture_integrity_contract")
        != _replacement_capture_integrity_contract(collector_sha)
        or body.get("execution_contract") != expected_execution
        or body.get("fixed_success_gates") != expected_gates
        or body.get("isolation_contract") != expected_isolation
    ):
        raise HandoffRefusal("preregistration_replacement_contract_invalid")
    _validate_replacement_cost_policy(body.get("cost_policy"))

    window = body.get("prospective_window")
    if not isinstance(window, Mapping) or set(window) != {
        "t0_utc_inclusive",
        "end_utc_exclusive",
        "consecutive_days",
        "fixed_before_any_eligible_observation",
        "observations_before_t0_forbidden",
        "observations_at_or_after_end_forbidden",
        "interim_signal_or_outcome_evaluation_forbidden",
        "interim_performance_statistics_forbidden",
        "early_success_forbidden",
        "success_evaluation_not_before_utc",
        "no_optional_extension_or_restart_after_failure",
        "data_quality_monitoring_must_not_compute_performance",
    }:
        raise HandoffRefusal("preregistration_time_invalid")
    sealed = _parse_utc_second(
        body.get("sealed_at_utc"), "preregistration_time_invalid"
    )
    t0 = _parse_utc_second(
        window.get("t0_utc_inclusive"), "preregistration_time_invalid"
    )
    end = _parse_utc_second(
        window.get("end_utc_exclusive"), "preregistration_time_invalid"
    )
    expected_window_flags = {
        "consecutive_days": PROSPECTIVE_WINDOW_DAYS,
        "fixed_before_any_eligible_observation": True,
        "observations_before_t0_forbidden": True,
        "observations_at_or_after_end_forbidden": True,
        "interim_signal_or_outcome_evaluation_forbidden": True,
        "interim_performance_statistics_forbidden": True,
        "early_success_forbidden": True,
        "success_evaluation_not_before_utc": window.get("end_utc_exclusive"),
        "no_optional_extension_or_restart_after_failure": True,
        "data_quality_monitoring_must_not_compute_performance": True,
    }
    if (
        t0 <= sealed
        or end - t0 != timedelta(days=PROSPECTIVE_WINDOW_DAYS)
        or any(window.get(key) != value for key, value in expected_window_flags.items())
    ):
        raise HandoffRefusal("preregistration_time_invalid")
    return t0, end


def _detected_profile(body: Mapping[str, Any]) -> str:
    identities = body.get("source_identities")
    strategy = body.get("strategy")
    accounting = body.get("attempt_accounting")
    collector_filename = ""
    screen_filename = ""
    if isinstance(identities, Mapping):
        collector = identities.get("collector_source")
        screen = identities.get("screen_source")
        if isinstance(collector, Mapping):
            collector_filename = str(collector.get("filename") or "")
        if isinstance(screen, Mapping):
            screen_filename = str(screen.get("filename") or "")
    manifest = (
        strategy.get("attempt_manifest") if isinstance(strategy, Mapping) else None
    )
    replacement_markers = (
        "replacement_lineage" in body,
        "capture_integrity_contract" in body,
        accounting == REPLACEMENT_ATTEMPT_ACCOUNTING,
        collector_filename == "capture_ig_mt4_m1_activity_resilient.py",
        screen_filename
        == "screen_mt4_tick_volume_close_location_continuation_replacement.py",
        isinstance(manifest, Mapping)
        and manifest.get("cumulative_attempted_cells_lower_bound") == 4_786,
    )
    return PROFILE_REPLACEMENT if any(replacement_markers) else PROFILE_BASE


@dataclass(frozen=True, slots=True)
class ProspectiveBinding:
    preregistration_path: Path
    preregistration_body_sha256: str
    preregistration_artifact_sha256: str
    t0_utc: str
    end_utc_exclusive: str
    t0_epoch: float
    end_epoch_exclusive: float
    profile: str = PROFILE_BASE
    collector_schema_version: str = COLLECTOR_SCHEMA
    collector_source_sha256: str = ""
    collector_support_source_sha256: str = ""
    capture_integrity_contract_sha256: str = ""

    @property
    def tuple(self) -> tuple[str, str, str, str]:
        return (
            self.preregistration_body_sha256,
            self.preregistration_artifact_sha256,
            self.t0_utc,
            self.end_utc_exclusive,
        )


def load_preregistration(
    path: str | Path,
    *,
    profile: str = PROFILE_AUTO,
) -> ProspectiveBinding:
    target, raw = _read_regular_file(
        path,
        reason="preregistration_file_invalid",
        maximum_bytes=MAXIMUM_PREREGISTRATION_BYTES,
    )
    payload = _strict_json_object(raw, reason="preregistration_json_invalid")
    body = dict(payload)
    claimed = str(body.pop("preregistration_body_sha256", "")).lower()
    if not _is_sha256(claimed) or not hmac.compare_digest(
        claimed, canonical_sha256(body)
    ):
        raise HandoffRefusal("preregistration_body_hash_invalid")
    if not target.name.endswith(f"{claimed}.json"):
        raise HandoffRefusal("preregistration_content_addressed_name_invalid")
    if (
        body.get("schema_version") != PREREGISTRATION_SCHEMA
        or body.get("research_only") is not True
        or body.get("authority") != FALSE_AUTHORITY
    ):
        raise HandoffRefusal("preregistration_contract_invalid")

    strategy = body.get("strategy")
    scope = body.get("scope")
    accounting = body.get("attempt_accounting")
    window = body.get("prospective_window")
    execution = body.get("execution_contract")
    gates = body.get("fixed_success_gates")
    identities = body.get("source_identities")
    isolation = body.get("isolation_contract")
    if not all(
        isinstance(value, Mapping)
        for value in (
            strategy,
            scope,
            accounting,
            window,
            execution,
            gates,
            identities,
            isolation,
        )
    ):
        raise HandoffRefusal("preregistration_contract_invalid")
    assert isinstance(strategy, Mapping)
    assert isinstance(scope, Mapping)
    assert isinstance(accounting, Mapping)
    assert isinstance(window, Mapping)
    assert isinstance(execution, Mapping)
    assert isinstance(gates, Mapping)
    assert isinstance(identities, Mapping)
    assert isinstance(isolation, Mapping)

    detected_profile = _detected_profile(body)
    if profile not in {PROFILE_AUTO, PROFILE_BASE, PROFILE_REPLACEMENT}:
        raise HandoffRefusal("preregistration_profile_invalid")
    if profile != PROFILE_AUTO and profile != detected_profile:
        raise HandoffRefusal("preregistration_profile_mismatch")
    if detected_profile == PROFILE_REPLACEMENT:
        t0, end = _validate_replacement_preregistration(body)
        replacement_identities = body["source_identities"]
        replacement_integrity = body["capture_integrity_contract"]
        assert isinstance(replacement_identities, Mapping)
        assert isinstance(replacement_integrity, Mapping)
        replacement_collector = replacement_identities["collector_source"]
        assert isinstance(replacement_collector, Mapping)
        return ProspectiveBinding(
            preregistration_path=target,
            preregistration_body_sha256=claimed,
            preregistration_artifact_sha256=hashlib.sha256(raw).hexdigest(),
            t0_utc=t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_utc_exclusive=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            t0_epoch=t0.timestamp(),
            end_epoch_exclusive=end.timestamp(),
            profile=PROFILE_REPLACEMENT,
            collector_schema_version=REPLACEMENT_COLLECTOR_SCHEMA,
            collector_source_sha256=str(replacement_collector["sha256"]),
            collector_support_source_sha256=PRESERVED_COLLECTOR_SHA256,
            capture_integrity_contract_sha256=canonical_sha256(replacement_integrity),
        )

    expected_cells = [
        {"config_id": CONFIG_ID, "symbol": symbol, "side": side}
        for symbol in SYMBOLS
        for side in ("BUY", "SELL")
    ]
    attempt_manifest = strategy.get("attempt_manifest")
    attempt_manifest_sha = str(strategy.get("attempt_manifest_sha256") or "").lower()
    if (
        strategy.get("strategy_id") != STRATEGY_ID
        or strategy.get("strategy_version") != STRATEGY_VERSION
        or strategy.get("config_id") != CONFIG_ID
        or canonical_sha256(FROZEN_CONFIGURATION) != FROZEN_CONFIG_SHA256
        or str(strategy.get("config_sha256") or "").lower() != FROZEN_CONFIG_SHA256
        or strategy.get("source_contract_id") != SOURCE_CONTRACT_ID
        or strategy.get("activity_metric_id") != ACTIVITY_METRIC_ID
        or not isinstance(attempt_manifest, Mapping)
        or not _is_sha256(attempt_manifest_sha)
        or not hmac.compare_digest(
            attempt_manifest_sha, canonical_sha256(attempt_manifest)
        )
        or scope.get("venue_id") != VENUE_ID
        or scope.get("scope_version") != SCOPE_VERSION
        or scope.get("ordered_symbols") != list(SYMBOLS)
        or scope.get("sides") != ["BUY", "SELL"]
        or scope.get("cell_order") != expected_cells
        or accounting
        != {
            "prior_attempted_cells_lower_bound": 4_654,
            "current_attempted_cells": 44,
            "cumulative_attempted_cells_lower_bound": 4_698,
        }
    ):
        raise HandoffRefusal("preregistration_scope_invalid")

    sealed_at = _parse_utc_second(
        body.get("sealed_at_utc"), "preregistration_time_invalid"
    )
    t0 = _parse_utc_second(
        window.get("t0_utc_inclusive"), "preregistration_time_invalid"
    )
    end = _parse_utc_second(
        window.get("end_utc_exclusive"), "preregistration_time_invalid"
    )
    if (
        t0 <= sealed_at
        or end - t0 != timedelta(days=PROSPECTIVE_WINDOW_DAYS)
        or window.get("consecutive_days") != PROSPECTIVE_WINDOW_DAYS
        or window.get("fixed_before_any_eligible_observation") is not True
        or window.get("observations_before_t0_forbidden") is not True
        or window.get("observations_at_or_after_end_forbidden") is not True
        or window.get("interim_signal_or_outcome_evaluation_forbidden") is not True
        or window.get("interim_performance_statistics_forbidden") is not True
        or window.get("early_success_forbidden") is not True
        or window.get("success_evaluation_not_before_utc")
        != window.get("end_utc_exclusive")
        or window.get("no_optional_extension_or_restart_after_failure") is not True
    ):
        raise HandoffRefusal("preregistration_time_invalid")
    if (
        execution.get("entry_type") != "immediate_market"
        or execution.get("pending_orders_forbidden") is not True
        or execution.get("maximum_entries_per_symbol_utc_day") != 1
        or execution.get("outcome_horizon_m1_bars") != 30
        or execution.get("rollover_entry_blackout_utc") != "[20:20:00,22:10:00)"
        or execution.get("rollover_entry_blackout_half_open") is not True
        or gates.get("all_44_cells_must_pass") is not True
        or gates.get("minimum_trades_per_cell") != 30
        or gates.get("minimum_independent_utc_days_per_cell") != 10
        or gates.get("minimum_total_trades") != 300
        or gates.get("minimum_total_independent_utc_days") != 60
        or gates.get("source_scope_ready_required") is not True
        or gates.get("source_errors_required") != []
    ):
        raise HandoffRefusal("preregistration_gate_contract_invalid")

    if (
        not _identity_row_valid(
            identities.get("screen_source"),
            filename="screen_mt4_tick_volume_close_location_continuation.py",
        )
        or not _identity_row_valid(
            identities.get("collector_source"),
            filename="capture_ig_mt4_m1_activity.py",
        )
        or not _identity_row_valid(
            identities.get("sealer_source"),
            filename="seal_mt4_tick_volume_preregistration.py",
        )
        or not _identity_row_valid(
            identities.get("scope_catalog_source"),
            filename="ig_mt4_catalog.py",
        )
        or isolation.get("artifact_grants_no_input_or_outcome_access") is not True
        or isolation.get(
            "outcome_evaluation_requires_a_physically_isolated_research_host"
        )
        is not True
        or isolation.get(
            "production_database_bridge_broker_credentials_registry_and_issuer_must_not_be_mounted"
        )
        is not True
    ):
        raise HandoffRefusal("preregistration_identity_contract_invalid")

    return ProspectiveBinding(
        preregistration_path=target,
        preregistration_body_sha256=claimed,
        preregistration_artifact_sha256=hashlib.sha256(raw).hexdigest(),
        t0_utc=t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_utc_exclusive=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        t0_epoch=t0.timestamp(),
        end_epoch_exclusive=end.timestamp(),
        profile=PROFILE_BASE,
        collector_schema_version=COLLECTOR_SCHEMA,
    )


def _binding_tuple(value: Mapping[str, Any], reason: str) -> tuple[str, str, str, str]:
    result = (
        str(value.get("preregistration_body_sha256") or "").lower(),
        str(value.get("preregistration_artifact_sha256") or "").lower(),
        str(value.get("prospective_t0_utc_inclusive") or ""),
        str(value.get("prospective_end_utc_exclusive") or ""),
    )
    if not _is_sha256(result[0]) or not _is_sha256(result[1]):
        raise HandoffRefusal(reason)
    _parse_utc_second(result[2], reason)
    _parse_utc_second(result[3], reason)
    return result


def _symbol_int_map(value: Any, reason: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != SYMBOL_SET:
        raise HandoffRefusal(reason)
    return {symbol: _strict_int(value[symbol], reason, minimum=0) for symbol in SYMBOLS}


def _symbol_float_map(value: Any, reason: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != SYMBOL_SET:
        raise HandoffRefusal(reason)
    result = {symbol: _finite(value[symbol], reason) for symbol in SYMBOLS}
    if any(number < 0.0 for number in result.values()):
        raise HandoffRefusal(reason)
    return result


def _symbol_hash_map(value: Any, reason: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != SYMBOL_SET:
        raise HandoffRefusal(reason)
    result = {symbol: str(value[symbol] or "").lower() for symbol in SYMBOLS}
    if any(not _is_sha256(item) for item in result.values()):
        raise HandoffRefusal(reason)
    return result


def _validate_source(value: Any, *, expected_id: str) -> None:
    if not isinstance(value, Mapping) or set(value) != SOURCE_FIELDS:
        raise HandoffRefusal("capture_source_scope_invalid")
    if (
        value.get("market_source_schema") != MARKET_SOURCE_SCHEMA
        or str(value.get("market_source_id") or "").lower() != expected_id
        or value.get("market_source_authenticated") is not True
        or value.get("broker_account_mode") != "demo"
        or value.get("broker_venue_id") != VENUE_ID
        or value.get("bridge_protocol_version") != BRIDGE_PROTOCOL_VERSION
    ):
        raise HandoffRefusal("capture_source_contract_invalid")
    for field in (
        "broker_account_scope_sha256",
        "producer_identity_sha256",
        "producer_instance_id_sha256",
        "terminal_lease_scope_sha256",
        "credential_generation_id_sha256",
    ):
        if not _is_sha256(value.get(field)):
            raise HandoffRefusal("capture_source_identity_invalid")


def _hour_for_epoch(epoch: float, reason: str) -> str:
    try:
        return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y%m%dT%H")
    except (OSError, OverflowError, ValueError) as exc:
        raise HandoffRefusal(reason) from exc


def _validate_bar(
    raw: Any,
    *,
    cycle_started: float,
    last_bar: dict[str, int],
) -> str:
    if not isinstance(raw, Mapping) or set(raw) != BAR_FIELDS:
        raise HandoffRefusal("capture_bar_scope_invalid")
    symbol = str(raw.get("symbol") or "").strip().upper()
    if symbol not in SYMBOL_SET:
        raise HandoffRefusal("capture_bar_symbol_invalid")
    minute = _strict_int(raw.get("minute_epoch"), "capture_bar_time_invalid", minimum=1)
    if (
        minute % 60 != 0
        or minute <= last_bar[symbol]
        or minute + 60 > cycle_started + 1e-6
    ):
        raise HandoffRefusal("capture_bar_time_invalid")
    prices = {
        name: _positive(raw.get(name), "capture_bar_price_invalid")
        for name in ("bid_open", "bid_high", "bid_low", "bid_close")
    }
    if prices["bid_high"] < max(prices.values()) or prices["bid_low"] > min(
        prices.values()
    ):
        raise HandoffRefusal("capture_bar_geometry_invalid")
    _strict_int(raw.get("tick_volume"), "capture_bar_volume_invalid", minimum=0)
    if (
        raw.get("price_basis") != PRICE_BASIS
        or raw.get("volume_source") != VOLUME_SOURCE
    ):
        raise HandoffRefusal("capture_bar_provenance_invalid")
    last_bar[symbol] = minute
    return symbol


def _validate_quote(
    raw: Any,
    *,
    utc_hour: str,
    binding: ProspectiveBinding,
    last_sequence: dict[str, int],
    last_transport: dict[str, float],
    last_snapshot: dict[str, str],
) -> tuple[str, float, float]:
    if not isinstance(raw, Mapping) or set(raw) != QUOTE_FIELDS:
        raise HandoffRefusal("capture_quote_scope_invalid")
    symbol = str(raw.get("symbol") or "").strip().upper()
    if symbol not in SYMBOL_SET:
        raise HandoffRefusal("capture_quote_symbol_invalid")
    sequence = _strict_int(
        raw.get("observation_sequence"),
        "capture_quote_sequence_invalid",
        minimum=1,
    )
    transport = _positive(
        raw.get("transport_received_at_epoch"), "capture_quote_time_invalid"
    )
    observed = _positive(raw.get("observed_at_epoch"), "capture_quote_time_invalid")
    observation_epoch = _strict_int(
        raw.get("observation_epoch"), "capture_quote_time_invalid", minimum=1
    )
    if (
        sequence != last_sequence[symbol] + 1
        or transport <= last_transport[symbol]
        or not math.isclose(observed, transport, rel_tol=0.0, abs_tol=1e-9)
        or observation_epoch != math.floor(transport)
        or not binding.t0_epoch <= transport < binding.end_epoch_exclusive
        or _hour_for_epoch(transport, "capture_quote_time_invalid") != utc_hour
    ):
        raise HandoffRefusal("capture_quote_time_or_sequence_invalid")
    bid = _positive(raw.get("bid"), "capture_quote_price_invalid")
    ask = _positive(raw.get("ask"), "capture_quote_price_invalid")
    if ask < bid:
        raise HandoffRefusal("capture_quote_crossed")
    event_sequence = _strict_int(
        raw.get("market_event_sequence"),
        "capture_quote_event_invalid",
        minimum=0,
    )
    event_received_raw = raw.get("market_event_received_at_epoch")
    event_received: float | None
    if event_received_raw is None:
        event_received = None
    else:
        event_received = _positive(event_received_raw, "capture_quote_event_invalid")
        if event_received > transport:
            raise HandoffRefusal("capture_quote_event_invalid")
    if (event_sequence == 0) != (event_received is None):
        raise HandoffRefusal("capture_quote_event_invalid")
    token_hash = str(raw.get("source_event_token_sha256") or "").lower()
    snapshot_hash = str(raw.get("snapshot_sha256") or "").lower()
    if not _is_sha256(token_hash) or not _is_sha256(snapshot_hash):
        raise HandoffRefusal("capture_quote_hash_invalid")
    expected_snapshot = canonical_sha256(
        {
            "symbol": symbol,
            "transport_received_at_epoch": transport,
            "bid": bid,
            "ask": ask,
            "market_event_received_at_epoch": event_received,
            "market_event_sequence": event_sequence,
            "source_event_token_sha256": token_hash,
        }
    )
    if not hmac.compare_digest(snapshot_hash, expected_snapshot):
        raise HandoffRefusal("capture_quote_snapshot_hash_invalid")
    gap = transport - last_transport[symbol] if last_transport[symbol] > 0.0 else 0.0
    last_sequence[symbol] = sequence
    last_transport[symbol] = transport
    last_snapshot[symbol] = snapshot_hash
    return symbol, transport, gap


def _safe_chunk_path(root: Path, relative: str, *, expected: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or relative != expected:
        raise HandoffRefusal("manifest_chunk_path_invalid")
    candidate = root.joinpath(*pure.parts)
    resolved = _regular_file(
        candidate,
        reason="manifest_chunk_invalid",
        maximum_bytes=MAXIMUM_CHUNK_BYTES,
    )
    try:
        if not resolved.is_relative_to(root):
            raise HandoffRefusal("manifest_chunk_path_escape")
    except AttributeError:  # pragma: no cover - Python >=3.11 in production
        if not str(resolved).startswith(str(root) + os.sep):
            raise HandoffRefusal("manifest_chunk_path_escape")
    return resolved


def _count_exact_chunk_tree(root: Path) -> int:
    """Count only the collector's two-level chunk layout and reject orphans."""

    chunks_root = root / CHUNK_DIRECTORY
    if (
        not chunks_root.is_dir()
        or chunks_root.is_symlink()
        or _is_reparse_point(chunks_root)
    ):
        raise HandoffRefusal("capture_chunk_directory_invalid")
    count = 0
    try:
        hour_paths = list(chunks_root.iterdir())
    except OSError as exc:
        raise HandoffRefusal("capture_chunk_directory_unreadable") from exc
    for hour_path in hour_paths:
        if (
            not hour_path.is_dir()
            or hour_path.is_symlink()
            or _is_reparse_point(hour_path)
        ):
            raise HandoffRefusal("capture_orphan_or_invalid_chunk_detected")
        try:
            parsed_hour = datetime.strptime(hour_path.name, "%Y%m%dT%H")
        except ValueError:
            raise HandoffRefusal("capture_orphan_or_invalid_chunk_detected") from None
        if parsed_hour.strftime("%Y%m%dT%H") != hour_path.name:
            raise HandoffRefusal("capture_orphan_or_invalid_chunk_detected")
        try:
            children = hour_path.iterdir()
            for child in children:
                if (
                    not child.is_file()
                    or child.is_symlink()
                    or _is_reparse_point(child)
                    or not child.name.startswith("ig-mt4-m1-activity-s")
                    or not child.name.endswith(".json")
                ):
                    raise HandoffRefusal("capture_orphan_or_invalid_chunk_detected")
                count += 1
        except HandoffRefusal:
            raise
        except OSError as exc:
            raise HandoffRefusal("capture_chunk_directory_unreadable") from exc
    return count


def _regular_metadata_path(path: Path, *, reason: str) -> Path:
    if path.is_symlink() or _is_reparse_point(path):
        raise HandoffRefusal(reason)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise HandoffRefusal(reason) from exc
    if not resolved.is_file() or resolved.is_symlink() or _is_reparse_point(resolved):
        raise HandoffRefusal(reason)
    return resolved


def _probe_byte_lock_available(path: Path) -> None:
    acquired = False
    handle: Any = None
    lock_api: Any = None
    try:
        handle = path.open("rb")
        handle.seek(0)
        if os.name == "nt":
            lock_api = __import__("msvcrt")
            lock_api.locking(handle.fileno(), int(lock_api.LK_NBRLCK), 1)
        else:
            lock_api = __import__("fcntl")
            lock_api.flock(
                handle.fileno(), int(lock_api.LOCK_SH) | int(lock_api.LOCK_NB)
            )
        acquired = True
    except (OSError, ImportError) as exc:
        raise HandoffRefusal("capture_data_writer_still_active") from exc
    finally:
        if acquired and handle is not None and lock_api is not None:
            try:
                handle.seek(0)
                if os.name == "nt":
                    lock_api.locking(handle.fileno(), int(lock_api.LK_UNLCK), 1)
                else:
                    lock_api.flock(handle.fileno(), int(lock_api.LOCK_UN))
            except OSError as exc:
                if handle is not None:
                    handle.close()
                raise HandoffRefusal("capture_data_writer_lock_probe_failed") from exc
        if handle is not None and not handle.closed:
            handle.close()


def _probe_supervisor_lock_available(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise HandoffRefusal("capture_supervisor_writer_still_active") from exc


def _guard_canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise HandoffRefusal("capture_guard_identity_not_canonical") from exc


def _portable_absolute_path(value: Any, *, reason: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    absolute = text.startswith("/") or (
        len(text) >= 3 and text[0].isalpha() and text[1] == ":" and text[2] == "/"
    )
    if not text or not absolute or "\0" in text or ".." in PurePosixPath(text).parts:
        raise HandoffRefusal(reason)
    return text.rstrip("/")


def _validate_loopback_root_url(value: Any) -> bool:
    text = str(value or "")
    prefixes = (
        "http://127.0.0.1:",
        "http://localhost:",
        "http://[::1]:",
    )
    prefix = next((item for item in prefixes if text.startswith(item)), "")
    if not prefix:
        return False
    port = text[len(prefix) :]
    return port.isdecimal() and 1 <= int(port) <= 65_535


def _validate_replacement_guard_identity(
    path: Path,
    *,
    binding: ProspectiveBinding,
) -> str:
    resolved, raw = _read_regular_file(
        path,
        reason="capture_guard_identity_invalid",
        maximum_bytes=MAXIMUM_PREREGISTRATION_BYTES,
    )
    if not raw.endswith(b"\n"):
        raise HandoffRefusal("capture_guard_identity_not_canonical")
    payload = _strict_json_object(
        raw[:-1], reason="capture_guard_identity_json_invalid"
    )
    if raw != _guard_canonical_json_bytes(payload) + b"\n":
        raise HandoffRefusal("capture_guard_identity_not_canonical")
    fields = {
        "activation_authorized",
        "api_key_file_path",
        "authority_granted",
        "bridge_base_url",
        "broker_access_authorized",
        "capture_integrity_contract",
        "capture_integrity_contract_sha256",
        "collection_only",
        "collector_source_path",
        "collector_source_sha256",
        "collector_support_source_path",
        "collector_support_source_sha256",
        "continuity_inspector_source_path",
        "continuity_inspector_source_sha256",
        "data_writer_lock_path",
        "evaluation_performed",
        "issuer_authorized",
        "order_authorized",
        "outcome_access_authorized",
        "output_root",
        "performance_computation_authorized",
        "policy",
        "policy_sha256",
        "preregistration_artifact_sha256",
        "preregistration_body_sha256",
        "preregistration_path",
        "prospective_end_utc_exclusive",
        "prospective_t0_utc_inclusive",
        "resume_contract",
        "runtime_authorized",
        "schema_version",
        "signal_computation_authorized",
        "signature_authorized",
        "success_claim_authorized",
    }
    false_fields = (
        "activation_authorized",
        "authority_granted",
        "broker_access_authorized",
        "evaluation_performed",
        "issuer_authorized",
        "order_authorized",
        "outcome_access_authorized",
        "performance_computation_authorized",
        "runtime_authorized",
        "signal_computation_authorized",
        "signature_authorized",
        "success_claim_authorized",
    )
    policy = {
        "tick_interval_secs": 2.0,
        "bar_interval_secs": 60.0,
        "bar_limit": 400,
        "http_timeout_secs": 5.0,
        "rollover_mode": "refuse",
    }
    resume_contract = {
        "same_preregistration_required": True,
        "same_output_root_required": True,
        "same_collector_source_required": True,
        "same_collector_support_source_required": True,
        "first_authenticated_finalized_observation_wins": True,
        "hash_chained_bar_epochs_skipped_without_payload_comparison": True,
        "unverifiable_nonincreasing_bar_epochs_refused": True,
        "market_source_rollover_refused": True,
        "t0_reset_forbidden": True,
        "observed_gaps_preserved": True,
        "exclusive_output_data_writer_lock_required": True,
    }
    expected_integrity = _replacement_capture_integrity_contract(
        binding.collector_source_sha256
    )
    collector_path = _portable_absolute_path(
        payload.get("collector_source_path"), reason="capture_guard_path_invalid"
    )
    collector_support_path = _portable_absolute_path(
        payload.get("collector_support_source_path"),
        reason="capture_guard_path_invalid",
    )
    inspector_path = _portable_absolute_path(
        payload.get("continuity_inspector_source_path"),
        reason="capture_guard_path_invalid",
    )
    preregistration_path = _portable_absolute_path(
        payload.get("preregistration_path"), reason="capture_guard_path_invalid"
    )
    output_root = _portable_absolute_path(
        payload.get("output_root"), reason="capture_guard_path_invalid"
    )
    data_lock_path = _portable_absolute_path(
        payload.get("data_writer_lock_path"), reason="capture_guard_path_invalid"
    )
    _portable_absolute_path(
        payload.get("api_key_file_path"), reason="capture_guard_path_invalid"
    )
    inspector_sha = payload.get("continuity_inspector_source_sha256")
    if (
        set(payload) != fields
        or payload.get("schema_version") != GUARD_IDENTITY_SCHEMA
        or payload.get("collection_only") is not True
        or any(payload.get(field) is not False for field in false_fields)
        or payload.get("collector_source_sha256") != binding.collector_source_sha256
        or payload.get("collector_support_source_sha256")
        != binding.collector_support_source_sha256
        or not isinstance(inspector_sha, str)
        or inspector_sha != inspector_sha.lower()
        or not _is_sha256(inspector_sha)
        or inspector_sha != RESILIENT_CONTINUITY_INSPECTOR_SHA256
        or payload.get("capture_integrity_contract") != expected_integrity
        or payload.get("capture_integrity_contract_sha256")
        != binding.capture_integrity_contract_sha256
        or payload.get("preregistration_body_sha256")
        != binding.preregistration_body_sha256
        or payload.get("preregistration_artifact_sha256")
        != binding.preregistration_artifact_sha256
        or payload.get("prospective_t0_utc_inclusive") != binding.t0_utc
        or payload.get("prospective_end_utc_exclusive") != binding.end_utc_exclusive
        or payload.get("policy") != policy
        or payload.get("policy_sha256") != canonical_sha256(policy)
        or payload.get("resume_contract") != resume_contract
        or PurePosixPath(collector_path).name
        != "capture_ig_mt4_m1_activity_resilient.py"
        or PurePosixPath(collector_support_path).name != "capture_ig_mt4_m1_activity.py"
        or PurePosixPath(inspector_path).name
        != "check_mt4_tick_volume_collector_continuity_resilient.py"
        or PurePosixPath(preregistration_path).name
        != f"mtvclc_v1_preregistration_{binding.preregistration_body_sha256}.json"
        or data_lock_path != f"{output_root}/{DATA_WRITER_LOCK_FILENAME}"
        or not _validate_loopback_root_url(payload.get("bridge_base_url"))
    ):
        raise HandoffRefusal("capture_guard_identity_contract_invalid")
    return hashlib.sha256(raw).hexdigest()


def _validate_replacement_capture_final_state(
    root: Path,
    *,
    binding: ProspectiveBinding,
) -> str:
    """Require the resilient collector's closed, portable final topology."""

    active_journal = root / ACTIVE_HOUR_JOURNAL_FILENAME
    if active_journal.exists() or active_journal.is_symlink():
        raise HandoffRefusal("capture_active_hour_journal_present")
    try:
        children = {child.name: child for child in root.iterdir()}
    except OSError as exc:
        raise HandoffRefusal("capture_root_unreadable") from exc
    allowed = {
        MANIFEST_FILENAME,
        CHUNK_DIRECTORY,
        DATA_WRITER_LOCK_FILENAME,
        GUARD_IDENTITY_FILENAME,
        SUPERVISION_DIRECTORY,
    }
    required = {
        MANIFEST_FILENAME,
        CHUNK_DIRECTORY,
        DATA_WRITER_LOCK_FILENAME,
        GUARD_IDENTITY_FILENAME,
    }
    if not required.issubset(children) or not set(children).issubset(allowed):
        raise HandoffRefusal("capture_final_topology_invalid")

    data_lock = _regular_metadata_path(
        children[DATA_WRITER_LOCK_FILENAME],
        reason="capture_data_writer_lock_invalid",
    )
    _probe_byte_lock_available(data_lock)
    try:
        if data_lock.read_bytes() != b"\0":
            raise HandoffRefusal("capture_data_writer_lock_invalid")
    except OSError as exc:
        raise HandoffRefusal("capture_data_writer_lock_invalid") from exc

    guard_sha256 = _validate_replacement_guard_identity(
        children[GUARD_IDENTITY_FILENAME], binding=binding
    )

    supervision = children.get(SUPERVISION_DIRECTORY)
    if supervision is None:
        return guard_sha256
    if (
        not supervision.is_dir()
        or supervision.is_symlink()
        or _is_reparse_point(supervision)
    ):
        raise HandoffRefusal("capture_supervision_topology_invalid")
    try:
        supervision_children = {child.name: child for child in supervision.iterdir()}
    except OSError as exc:
        raise HandoffRefusal("capture_supervision_topology_invalid") from exc
    expected_supervision = {
        SUPERVISOR_LOCK_FILENAME,
        SUPERVISOR_STDOUT_FILENAME,
        SUPERVISOR_STDERR_FILENAME,
    }
    if set(supervision_children) != expected_supervision:
        raise HandoffRefusal("capture_supervision_topology_invalid")
    for name, path in supervision_children.items():
        _regular_metadata_path(
            path,
            reason=f"capture_supervision_artifact_invalid:{name}",
        )
    _probe_supervisor_lock_available(supervision_children[SUPERVISOR_LOCK_FILENAME])
    return guard_sha256


def verify_capture_handoff(
    *,
    preregistration_path: str | Path,
    capture_root: str | Path,
    now_epoch: float | None = None,
    profile: str = PROFILE_AUTO,
) -> dict[str, Any]:
    """Verify the complete immutable capture and return an authority-free index."""

    binding = load_preregistration(preregistration_path, profile=profile)
    now = _finite(
        time.time() if now_epoch is None else now_epoch, "verification_clock_invalid"
    )
    if now < binding.end_epoch_exclusive:
        raise HandoffRefusal("prospective_window_not_closed")

    root_candidate = Path(capture_root).expanduser()
    if root_candidate.is_symlink() or _is_reparse_point(root_candidate):
        raise HandoffRefusal("capture_root_invalid")
    try:
        root = root_candidate.resolve(strict=True)
    except OSError as exc:
        raise HandoffRefusal("capture_root_invalid") from exc
    if not root.is_dir() or root.is_symlink() or _is_reparse_point(root):
        raise HandoffRefusal("capture_root_invalid")
    guard_identity_sha256 = ""
    if binding.profile == PROFILE_REPLACEMENT:
        guard_identity_sha256 = _validate_replacement_capture_final_state(
            root, binding=binding
        )
    manifest_path = _regular_file(
        root / MANIFEST_FILENAME,
        reason="capture_manifest_invalid",
    )

    manifest_digest = hashlib.sha256()
    previous_entry_hash = ZERO_SHA256
    entries = 0
    bar_rows = 0
    quote_rows = 0
    source_id = ""
    first_cycle_started = math.inf
    last_cycle_completed = 0.0
    first_quote = {symbol: math.inf for symbol in SYMBOLS}
    last_quote = {symbol: 0.0 for symbol in SYMBOLS}
    max_quote_gap = {symbol: 0.0 for symbol in SYMBOLS}
    quote_gap_count = {symbol: 0 for symbol in SYMBOLS}
    bar_count = {symbol: 0 for symbol in SYMBOLS}
    first_bar = {symbol: 0 for symbol in SYMBOLS}
    last_bar = {symbol: 0 for symbol in SYMBOLS}
    last_sequence = {symbol: 0 for symbol in SYMBOLS}
    last_transport = {symbol: 0.0 for symbol in SYMBOLS}
    last_snapshot = {symbol: ZERO_SHA256 for symbol in SYMBOLS}

    try:
        handle = manifest_path.open("rb")
    except OSError as exc:
        raise HandoffRefusal("capture_manifest_unreadable") from exc
    with handle:
        for raw_line in handle:
            if (
                not raw_line.endswith(b"\n")
                or len(raw_line) > MAXIMUM_MANIFEST_LINE_BYTES
            ):
                raise HandoffRefusal("capture_manifest_line_invalid")
            manifest_digest.update(raw_line)
            entry = _strict_json_object(
                raw_line[:-1], reason="capture_manifest_json_invalid"
            )
            if set(entry) != MANIFEST_FIELDS:
                raise HandoffRefusal("capture_manifest_entry_scope_invalid")
            if raw_line != canonical_json_bytes(entry) + b"\n":
                raise HandoffRefusal("capture_manifest_entry_not_canonical")
            entries += 1
            sequence = _strict_int(
                entry.get("sequence"), "capture_manifest_sequence_invalid", minimum=1
            )
            if sequence != entries:
                raise HandoffRefusal("capture_manifest_sequence_invalid")
            claimed_entry_hash = str(entry.get("manifest_entry_sha256") or "").lower()
            entry_body = dict(entry)
            entry_body.pop("manifest_entry_sha256", None)
            if (
                entry.get("schema_version") != MANIFEST_SCHEMA
                or entry.get("chunk_schema_version") != CHUNK_SCHEMA
                or entry.get("previous_entry_sha256") != previous_entry_hash
                or not _is_sha256(claimed_entry_hash)
                or not hmac.compare_digest(
                    claimed_entry_hash, canonical_sha256(entry_body)
                )
                or _binding_tuple(entry, "manifest_preregistration_binding_invalid")
                != binding.tuple
            ):
                raise HandoffRefusal("capture_manifest_chain_invalid")

            utc_hour = str(entry.get("utc_hour") or "")
            try:
                parsed_hour = datetime.strptime(utc_hour, "%Y%m%dT%H")
            except ValueError:
                raise HandoffRefusal("capture_manifest_hour_invalid") from None
            if parsed_hour.strftime("%Y%m%dT%H") != utc_hour:
                raise HandoffRefusal("capture_manifest_hour_invalid")
            segment = _strict_int(
                entry.get("segment_index"), "capture_source_segment_invalid", minimum=1
            )
            entry_source_id = str(entry.get("market_source_id") or "").lower()
            if (
                segment != 1
                or not _is_sha256(entry_source_id)
                or (source_id and entry_source_id != source_id)
            ):
                raise HandoffRefusal("capture_market_source_changed")
            source_id = source_id or entry_source_id
            expected_relative = PurePosixPath(
                CHUNK_DIRECTORY,
                utc_hour,
                f"ig-mt4-m1-activity-s{segment:04d}-q{sequence:010d}.json",
            ).as_posix()
            chunk_path = _safe_chunk_path(
                root,
                str(entry.get("chunk_path") or ""),
                expected=expected_relative,
            )
            try:
                chunk_raw = chunk_path.read_bytes()
            except OSError as exc:
                raise HandoffRefusal("manifest_chunk_unreadable") from exc
            chunk_size = _strict_int(
                entry.get("chunk_size_bytes"), "manifest_chunk_size_invalid", minimum=1
            )
            chunk_sha = str(entry.get("chunk_sha256") or "").lower()
            if (
                len(chunk_raw) != chunk_size
                or not _is_sha256(chunk_sha)
                or not hmac.compare_digest(
                    hashlib.sha256(chunk_raw).hexdigest(), chunk_sha
                )
            ):
                raise HandoffRefusal("manifest_chunk_hash_or_size_mismatch")
            chunk = _strict_json_object(chunk_raw, reason="manifest_chunk_json_invalid")
            if set(chunk) != CHUNK_FIELDS:
                raise HandoffRefusal("manifest_chunk_scope_invalid")
            if chunk_raw != canonical_json_bytes(chunk) + b"\n":
                raise HandoffRefusal("manifest_chunk_not_canonical")
            if (
                chunk.get("schema_version") != CHUNK_SCHEMA
                or chunk.get("collector_schema_version")
                != binding.collector_schema_version
                or chunk.get("source_contract_id") != SOURCE_CONTRACT_ID
                or chunk.get("activity_metric_id") != ACTIVITY_METRIC_ID
                or chunk.get("scope_version") != SCOPE_VERSION
                or chunk.get("symbol_scope") != list(SYMBOLS)
                or chunk.get("timeframe") != TIMEFRAME
                or chunk.get("minimum_m1_history_bars") != MINIMUM_M1_BARS
                or not math.isclose(
                    _finite(
                        chunk.get("maximum_quote_gap_seconds"),
                        "capture_chunk_policy_invalid",
                    ),
                    MAXIMUM_QUOTE_GAP_SECONDS,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                or (
                    binding.profile == PROFILE_BASE
                    and not MINIMUM_M1_BARS
                    <= _strict_int(
                        chunk.get("requested_bar_limit"),
                        "capture_chunk_policy_invalid",
                        minimum=1,
                    )
                    <= 2_000
                )
                or (
                    binding.profile == PROFILE_REPLACEMENT
                    and _strict_int(
                        chunk.get("requested_bar_limit"),
                        "capture_chunk_policy_invalid",
                        minimum=1,
                    )
                    != 400
                )
                or (
                    binding.profile == PROFILE_BASE
                    and not 0.0
                    < _finite(
                        chunk.get("configured_tick_interval_seconds"),
                        "capture_chunk_policy_invalid",
                    )
                    <= MAXIMUM_QUOTE_GAP_SECONDS
                )
                or (
                    binding.profile == PROFILE_REPLACEMENT
                    and not math.isclose(
                        _finite(
                            chunk.get("configured_tick_interval_seconds"),
                            "capture_chunk_policy_invalid",
                        ),
                        2.0,
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    )
                )
                or chunk.get("utc_hour") != utc_hour
                or chunk.get("segment_index") != segment
                or _binding_tuple(chunk, "chunk_preregistration_binding_invalid")
                != binding.tuple
                or chunk.get("collection_only") is not True
                or any(
                    chunk.get(field) is not False
                    for field in (
                        "evaluation_performed",
                        "success_claim_authorized",
                        "authority_granted",
                        "activation_authorized",
                        "order_authorized",
                    )
                )
            ):
                raise HandoffRefusal("capture_chunk_contract_invalid")
            _validate_source(chunk.get("source"), expected_id=source_id)

            cycle_started = _positive(
                chunk.get("collector_cycle_started_at_epoch"),
                "capture_cycle_time_invalid",
            )
            cycle_completed = _positive(
                chunk.get("collector_cycle_completed_at_epoch"),
                "capture_cycle_time_invalid",
            )
            observed_at = _positive(
                chunk.get("observed_at_epoch"), "capture_cycle_time_invalid"
            )
            if (
                cycle_started < binding.t0_epoch
                or cycle_completed < cycle_started
                or cycle_completed >= binding.end_epoch_exclusive
                or not math.isclose(
                    observed_at, cycle_completed, rel_tol=0.0, abs_tol=1e-9
                )
            ):
                raise HandoffRefusal("capture_cycle_outside_window")
            first_cycle_started = min(first_cycle_started, cycle_started)
            last_cycle_completed = max(last_cycle_completed, cycle_completed)

            raw_bars = chunk.get("bars")
            raw_quotes = chunk.get("quotes")
            if not isinstance(raw_bars, list) or not isinstance(raw_quotes, list):
                raise HandoffRefusal("capture_chunk_rows_invalid")
            if entry.get("bar_rows") != len(raw_bars) or entry.get("quote_rows") != len(
                raw_quotes
            ):
                raise HandoffRefusal("capture_chunk_row_count_mismatch")
            for raw_bar in raw_bars:
                symbol = _validate_bar(
                    raw_bar,
                    cycle_started=cycle_started,
                    last_bar=last_bar,
                )
                if first_bar[symbol] == 0:
                    first_bar[symbol] = int(raw_bar["minute_epoch"])
                bar_count[symbol] += 1
                bar_rows += 1
            for raw_quote in raw_quotes:
                symbol, transport, gap = _validate_quote(
                    raw_quote,
                    utc_hour=utc_hour,
                    binding=binding,
                    last_sequence=last_sequence,
                    last_transport=last_transport,
                    last_snapshot=last_snapshot,
                )
                first_quote[symbol] = min(first_quote[symbol], transport)
                last_quote[symbol] = max(last_quote[symbol], transport)
                if gap > 0.0:
                    max_quote_gap[symbol] = max(max_quote_gap[symbol], gap)
                    if gap > MAXIMUM_QUOTE_GAP_SECONDS:
                        quote_gap_count[symbol] += 1
                quote_rows += 1

            chunk_last_bar = _symbol_int_map(
                chunk.get("last_bar_epoch_by_symbol"), "chunk_state_map_invalid"
            )
            chunk_last_sequence = _symbol_int_map(
                chunk.get("last_tick_sequence_by_symbol"), "chunk_state_map_invalid"
            )
            chunk_last_transport = _symbol_float_map(
                chunk.get("last_tick_transport_epoch_by_symbol"),
                "chunk_state_map_invalid",
            )
            chunk_last_snapshot = _symbol_hash_map(
                chunk.get("last_tick_snapshot_sha256_by_symbol"),
                "chunk_state_map_invalid",
            )
            entry_maps = (
                _symbol_int_map(
                    entry.get("last_bar_epoch_by_symbol"),
                    "manifest_state_map_invalid",
                ),
                _symbol_int_map(
                    entry.get("last_tick_sequence_by_symbol"),
                    "manifest_state_map_invalid",
                ),
                _symbol_float_map(
                    entry.get("last_tick_transport_epoch_by_symbol"),
                    "manifest_state_map_invalid",
                ),
                _symbol_hash_map(
                    entry.get("last_tick_snapshot_sha256_by_symbol"),
                    "manifest_state_map_invalid",
                ),
            )
            expected_maps = (last_bar, last_sequence, last_transport, last_snapshot)
            chunk_maps = (
                chunk_last_bar,
                chunk_last_sequence,
                chunk_last_transport,
                chunk_last_snapshot,
            )
            if entry_maps != expected_maps or chunk_maps != expected_maps:
                raise HandoffRefusal("capture_state_map_mismatch")
            previous_entry_hash = claimed_entry_hash

    if entries <= 0 or not source_id:
        raise HandoffRefusal("capture_manifest_empty")
    if _count_exact_chunk_tree(root) != entries:
        raise HandoffRefusal("capture_orphan_or_invalid_chunk_detected")
    if sum(last_sequence.values()) != quote_rows:
        raise HandoffRefusal("capture_quote_sequence_count_mismatch")
    if first_cycle_started > binding.t0_epoch + MAXIMUM_WINDOW_EDGE_LAG_SECONDS:
        raise HandoffRefusal("capture_window_start_not_closed")
    if last_cycle_completed < (
        binding.end_epoch_exclusive - MAXIMUM_WINDOW_EDGE_LAG_SECONDS
    ):
        raise HandoffRefusal("capture_window_end_not_closed")
    for symbol in SYMBOLS:
        if (
            not math.isfinite(first_quote[symbol])
            or first_quote[symbol] > binding.t0_epoch + MAXIMUM_WINDOW_EDGE_LAG_SECONDS
            or last_quote[symbol]
            < binding.end_epoch_exclusive - MAXIMUM_WINDOW_EDGE_LAG_SECONDS
        ):
            raise HandoffRefusal(f"capture_quote_window_not_closed:{symbol}")
        if bar_count[symbol] < MINIMUM_M1_BARS:
            raise HandoffRefusal(f"capture_bar_coverage_insufficient:{symbol}")
        if last_bar[symbol] + 60 < (
            binding.end_epoch_exclusive - MAXIMUM_FINAL_BAR_LAG_SECONDS
        ):
            raise HandoffRefusal(f"capture_bar_window_not_closed:{symbol}")

    if binding.profile == PROFILE_REPLACEMENT:
        final_guard_sha256 = _validate_replacement_capture_final_state(
            root, binding=binding
        )
        if not hmac.compare_digest(final_guard_sha256, guard_identity_sha256):
            raise HandoffRefusal("capture_guard_identity_changed_during_verification")

    manifest_sha = manifest_digest.hexdigest()
    inventory = {
        "preregistration_body_sha256": binding.preregistration_body_sha256,
        "preregistration_artifact_sha256": binding.preregistration_artifact_sha256,
        "prospective_t0_utc_inclusive": binding.t0_utc,
        "prospective_end_utc_exclusive": binding.end_utc_exclusive,
        "manifest_sha256": manifest_sha,
        "manifest_head_sha256": previous_entry_hash,
        "manifest_entries": entries,
        "market_source_id": source_id,
        "segment_count": 1,
        "bar_rows": bar_rows,
        "quote_rows": quote_rows,
        "bar_rows_by_symbol": dict(bar_count),
        "quote_rows_by_symbol": dict(last_sequence),
        "first_bar_epoch_by_symbol": dict(first_bar),
        "first_quote_epoch_by_symbol": dict(first_quote),
        "last_quote_epoch_by_symbol": dict(last_quote),
        "last_bar_epoch_by_symbol": dict(last_bar),
        "maximum_transport_gap_seconds_by_symbol": dict(max_quote_gap),
        "transport_gap_count_over_five_seconds_by_symbol": dict(quote_gap_count),
        "referenced_chunk_files": entries,
        "orphan_chunk_files": 0,
    }
    if binding.profile == PROFILE_REPLACEMENT:
        inventory["guard_identity_sha256"] = guard_identity_sha256
    result: dict[str, Any] = {
        "schema_version": HANDOFF_SCHEMA,
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "config_id": CONFIG_ID,
        "venue_id": VENUE_ID,
        "scope_version": SCOPE_VERSION,
        "symbol_scope": list(SYMBOLS),
        "source_contract_id": SOURCE_CONTRACT_ID,
        "activity_metric_id": ACTIVITY_METRIC_ID,
        "capture_inventory": inventory,
        "capture_inventory_sha256": canonical_sha256(inventory),
        "window_closed": True,
        "manifest_and_chunks_verified": True,
        "outcome_evaluation_performed": False,
        "performance_statistics_computed": False,
        "research_only": True,
        "authority": dict(FALSE_AUTHORITY),
    }
    result["handoff_body_sha256"] = canonical_sha256(result)
    return result


def publish_handoff(*, output_root: str | Path, handoff: Mapping[str, Any]) -> Path:
    root = Path(output_root).expanduser().resolve(strict=False)
    if root.exists() and (
        not root.is_dir() or root.is_symlink() or _is_reparse_point(root)
    ):
        raise HandoffRefusal("handoff_output_root_invalid")
    root.mkdir(parents=True, exist_ok=True)
    body_sha = str(handoff.get("handoff_body_sha256") or "").lower()
    body = dict(handoff)
    body.pop("handoff_body_sha256", None)
    body["handoff_body_sha256"] = body_sha
    check = dict(body)
    claimed = str(check.pop("handoff_body_sha256", "")).lower()
    if not _is_sha256(claimed) or claimed != canonical_sha256(check):
        raise HandoffRefusal("handoff_body_hash_invalid")
    target = root / f"mtvclc_capture_handoff_{body_sha}.json"
    if target.exists():
        raise HandoffRefusal("handoff_output_exists")
    payload = canonical_json_bytes(body) + b"\n"
    temporary = root / f".{target.name}.{os.getpid()}.tmp"
    if temporary.exists():
        raise HandoffRefusal("handoff_temporary_exists")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
        temporary.unlink()
        target.chmod(stat.S_IREAD)
    except OSError as exc:
        try:
            if temporary.exists():
                temporary.chmod(stat.S_IWRITE | stat.S_IREAD)
                temporary.unlink()
        except OSError:
            pass
        raise HandoffRefusal("handoff_atomic_publish_failed") from exc
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a closed MTVCLC capture and publish an authority-free "
            "isolated-evaluation handoff inventory."
        )
    )
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument(
        "--profile",
        choices=(PROFILE_AUTO, PROFILE_BASE, PROFILE_REPLACEMENT),
        default=PROFILE_AUTO,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        handoff = verify_capture_handoff(
            preregistration_path=args.preregistration,
            capture_root=args.capture_root,
            profile=args.profile,
        )
        output = publish_handoff(output_root=args.output_root, handoff=handoff)
    except (HandoffRefusal, OSError, ValueError) as exc:
        print(f"MTVCLC capture handoff refused: {exc}")
        return 2
    print(
        json.dumps(
            {
                "status": "verified",
                "output": str(output),
                "handoff_body_sha256": handoff["handoff_body_sha256"],
                "capture_inventory_sha256": handoff["capture_inventory_sha256"],
                "outcome_evaluation_performed": False,
                "authority_granted": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
