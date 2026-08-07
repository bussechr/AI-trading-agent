from __future__ import annotations

import ast
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable, Iterator, Mapping

import pytest

from tools import verify_mt4_tick_volume_capture_handoff as handoff


T0 = datetime(2020, 1, 1, tzinfo=UTC)
END = T0 + timedelta(days=handoff.PROSPECTIVE_WINDOW_DAYS)


def _canonical(value: Any) -> bytes:
    return handoff.canonical_json_bytes(value)


def _identity(filename: str, marker: str) -> dict[str, Any]:
    return {
        "filename": filename,
        "sha256": hashlib.sha256(marker.encode("ascii")).hexdigest(),
        "size_bytes": len(marker),
    }


def _fixed_identity(filename: str, sha256: str, size_bytes: int) -> dict[str, Any]:
    return {
        "filename": filename,
        "sha256": sha256,
        "size_bytes": size_bytes,
    }


def _replacement_cost_policy() -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for symbol in handoff.SYMBOLS:
        conversion_applies = symbol[3:] != "USD"
        conversion = 0.005 if conversion_applies else 0.0
        geometry = 3.5
        target = 4.0 * geometry
        stop = 8.0 * geometry
        p_star = (stop * (1.0 + conversion) + geometry) / (
            target * (1.0 - conversion) + stop * (1.0 + conversion)
        )
        rows[symbol] = {
            "p90_ig_spread_bps": 2.0,
            "commission_bps_per_round_trip": 0.5,
            "financing_bps_per_trade": 0.0,
            "fixed_adverse_execution_debit_bps": 1.0,
            "pre_conversion_geometry_cost_bps": geometry,
            "profit_loss_currency": symbol[3:],
            "account_currency": "USD",
            "conversion_rate_of_absolute_profit_or_loss": 0.005,
            "convert_on_close_charge_fraction_for_screen": conversion,
            "conversion_applies": conversion_applies,
            "conversion_adjusted_break_even_win_probability": p_star,
            "commission_status": "conservative_upper_bound",
            "financing_status": ("structurally_avoided_by_fixed_rollover_guard"),
            "conversion_status": (
                "debit_absolute_profit_or_loss_when_account_currency_differs"
            ),
        }
    return {
        "formula": (
            "net_bps=gross_quote_bps-(p90_ig_spread_bps+commission_bps_per_"
            "round_trip+financing_bps_per_trade+1.0bps_adverse_execution_debit)-"
            "conversion_rate*abs(gross_quote_bps)_when_profit_loss_currency_"
            "differs_from_account_currency"
        ),
        "conversion_treatment": (
            "debit_the_attested_rate_on_absolute_profit_or_loss_for_both_wins_"
            "and_losses;never_credit_conversion;zero_only_when_the_profit_loss_"
            "currency_equals_the_attested_account_currency"
        ),
        "geometry_uses_pre_conversion_cost": True,
        "final_cell_mean_uses_conversion_adjusted_net": True,
        "unknown_commission_financing_or_conversion_refuses_evaluation": True,
        "fee_schedule_change_or_source_uncertainty_refuses_evaluation": True,
        "symbols": rows,
    }


def _replacement_source_identities() -> dict[str, Any]:
    collector = _fixed_identity(
        "capture_ig_mt4_m1_activity_resilient.py",
        handoff.REPLACEMENT_COLLECTOR_SHA256,
        handoff.REPLACEMENT_COLLECTOR_SIZE_BYTES,
    )
    components = [["runtime/runner.py", "a" * 64]]
    engine_sha = handoff.canonical_sha256(
        {
            "schema_version": "fxstack.production_scalp_engine_identity.v3",
            "components": [
                {"path": path, "sha256": digest} for path, digest in components
            ],
        }
    )
    documents = [
        {
            "filename": f"{role}.html",
            "sha256": hashlib.sha256(role.encode("ascii")).hexdigest(),
            "size_bytes": len(role),
            "role": role,
            "url": url,
            "retrieved_at_utc": (T0 - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        for role, url in handoff.SOURCE_DOCUMENT_URLS.items()
    ]
    return {
        "collector_source": collector,
        "collector_support_source": _fixed_identity(
            "capture_ig_mt4_m1_activity.py",
            handoff.PRESERVED_COLLECTOR_SHA256,
            handoff.PRESERVED_COLLECTOR_SIZE_BYTES,
        ),
        "screen_source": _fixed_identity(
            "screen_mt4_tick_volume_close_location_continuation_replacement.py",
            handoff.REPLACEMENT_SCREEN_SHA256,
            handoff.REPLACEMENT_SCREEN_SIZE_BYTES,
        ),
        "screen_support_source": _fixed_identity(
            "screen_mt4_tick_volume_close_location_continuation.py",
            handoff.PRESERVED_SCREEN_SHA256,
            handoff.PRESERVED_SCREEN_SIZE_BYTES,
        ),
        "sealer_source": _fixed_identity(
            "seal_mt4_tick_volume_preregistration_resilient.py",
            handoff.REPLACEMENT_SEALER_SHA256,
            handoff.REPLACEMENT_SEALER_SIZE_BYTES,
        ),
        "base_sealer_source": _fixed_identity(
            "seal_mt4_tick_volume_preregistration.py",
            handoff.BASE_SEALER_SHA256,
            handoff.BASE_SEALER_SIZE_BYTES,
        ),
        "scope_catalog_source": _identity("ig_mt4_catalog.py", "catalog"),
        "cost_capture": {
            "capture_json": _identity("ig_mt4_bid_ask_capture.json", "cost-json"),
            "capture_mode": "authenticated_same_source_db_history",
            "capture_npz": _identity("ig_mt4_bid_ask_samples.npz", "cost-npz"),
            "capture_payload_sha256": "b" * 64,
            "scope_version": handoff.SCOPE_VERSION,
            "venue_id": handoff.VENUE_ID,
        },
        "fee_attestation": {
            "account_currency": "USD",
            "attestation": _identity("mtvclc_fee_attestation.json", "fee-attestation"),
            "attested_at_utc": (T0 - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "effective_at_utc": (T0 - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "operator_attestation_sha256": "c" * 64,
            "source_documents": documents,
        },
        "production_runtime_context": {
            "relationship": "context_only_successor_not_integrated_or_authorized",
            "engine_identity": {
                "engine_sha256": engine_sha,
                "component_sha256": components,
                "schema_version": "fxstack.production_scalp_engine_identity.v3",
            },
            "active_strategy_family_context": "scalp_dislocation",
            "active_strategy_version_context": (
                "fxstack.strategy.scalp_dislocation.v5"
            ),
            "active_policy_config_sha256_context": "d" * 64,
        },
    }


def _write_preregistration(root: Path) -> Path:
    strategy_manifest = {"fixed": True}
    body: dict[str, Any] = {
        "schema_version": handoff.PREREGISTRATION_SCHEMA,
        "sealed_at_utc": (T0 - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "research_only": True,
        "strategy": {
            "strategy_id": handoff.STRATEGY_ID,
            "strategy_version": handoff.STRATEGY_VERSION,
            "config_id": handoff.CONFIG_ID,
            "config_sha256": handoff.FROZEN_CONFIG_SHA256,
            "source_contract_id": handoff.SOURCE_CONTRACT_ID,
            "activity_metric_id": handoff.ACTIVITY_METRIC_ID,
            "attempt_manifest": strategy_manifest,
            "attempt_manifest_sha256": handoff.canonical_sha256(strategy_manifest),
        },
        "scope": {
            "venue_id": handoff.VENUE_ID,
            "scope_version": handoff.SCOPE_VERSION,
            "ordered_symbols": list(handoff.SYMBOLS),
            "sides": ["BUY", "SELL"],
            "cell_order": [
                {
                    "config_id": handoff.CONFIG_ID,
                    "symbol": symbol,
                    "side": side,
                }
                for symbol in handoff.SYMBOLS
                for side in ("BUY", "SELL")
            ],
        },
        "attempt_accounting": {
            "prior_attempted_cells_lower_bound": 4_654,
            "current_attempted_cells": 44,
            "cumulative_attempted_cells_lower_bound": 4_698,
        },
        "prospective_window": {
            "t0_utc_inclusive": T0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc_exclusive": END.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "consecutive_days": handoff.PROSPECTIVE_WINDOW_DAYS,
            "fixed_before_any_eligible_observation": True,
            "observations_before_t0_forbidden": True,
            "observations_at_or_after_end_forbidden": True,
            "interim_signal_or_outcome_evaluation_forbidden": True,
            "interim_performance_statistics_forbidden": True,
            "early_success_forbidden": True,
            "success_evaluation_not_before_utc": END.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "no_optional_extension_or_restart_after_failure": True,
        },
        "execution_contract": {
            "entry_type": "immediate_market",
            "pending_orders_forbidden": True,
            "maximum_entries_per_symbol_utc_day": 1,
            "outcome_horizon_m1_bars": 30,
            "rollover_entry_blackout_utc": "[20:20:00,22:10:00)",
            "rollover_entry_blackout_half_open": True,
        },
        "fixed_success_gates": {
            "all_44_cells_must_pass": True,
            "minimum_trades_per_cell": 30,
            "minimum_independent_utc_days_per_cell": 10,
            "minimum_total_trades": 300,
            "minimum_total_independent_utc_days": 60,
            "source_scope_ready_required": True,
            "source_errors_required": [],
        },
        "source_identities": {
            "screen_source": _identity(
                "screen_mt4_tick_volume_close_location_continuation.py",
                "screen",
            ),
            "collector_source": _identity("capture_ig_mt4_m1_activity.py", "collector"),
            "sealer_source": _identity(
                "seal_mt4_tick_volume_preregistration.py", "sealer"
            ),
            "scope_catalog_source": _identity("ig_mt4_catalog.py", "catalog"),
        },
        "isolation_contract": {
            "artifact_grants_no_input_or_outcome_access": True,
            "outcome_evaluation_requires_a_physically_isolated_research_host": True,
            "production_database_bridge_broker_credentials_registry_and_issuer_must_not_be_mounted": True,
        },
        "authority": dict(handoff.FALSE_AUTHORITY),
    }
    body_sha = handoff.canonical_sha256(body)
    payload = {**body, "preregistration_body_sha256": body_sha}
    path = root / f"mtvclc_v1_preregistration_{body_sha}.json"
    path.write_bytes(_canonical(payload))
    return path


def _write_replacement_preregistration(root: Path) -> Path:
    base_path = _write_preregistration(root)
    payload = json.loads(base_path.read_text(encoding="utf-8"))
    body = {
        key: value
        for key, value in payload.items()
        if key != "preregistration_body_sha256"
    }
    identities = _replacement_source_identities()
    collector_sha = identities["collector_source"]["sha256"]
    attempt_manifest = json.loads(json.dumps(handoff.REPLACEMENT_ATTEMPT_MANIFEST))
    body["strategy"]["attempt_manifest"] = attempt_manifest
    body["strategy"]["attempt_manifest_sha256"] = handoff.canonical_sha256(
        attempt_manifest
    )
    body["attempt_accounting"] = dict(handoff.REPLACEMENT_ATTEMPT_ACCOUNTING)
    body["abandoned_preregistrations"] = json.loads(
        json.dumps(handoff.REPLACEMENT_ABANDONED_PREREGISTRATIONS)
    )
    body["replacement_lineage"] = dict(handoff.REPLACEMENT_LINEAGE)
    body["capture_integrity_contract"] = (
        handoff._replacement_capture_integrity_contract(collector_sha)
    )
    body["prospective_window"][
        "data_quality_monitoring_must_not_compute_performance"
    ] = True
    body["execution_contract"]["signals_inside_blackout_reserve"] = False
    body["cost_policy"] = _replacement_cost_policy()
    body["fixed_success_gates"] = {
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
            handoff.REPLACEMENT_BONFERRONI_T_THRESHOLD
        ),
    }
    body["source_identities"] = identities
    body["isolation_contract"] = {
        "artifact_grants_no_input_or_outcome_access": True,
        "prospective_collection_is_a_separate_get_only_operator_action": True,
        "outcome_evaluation_requires_a_physically_isolated_research_host": True,
        "production_database_bridge_broker_credentials_registry_and_issuer_"
        "must_not_be_mounted": True,
        "transfer_to_isolation_must_verify_preregistration_body_sha256": True,
    }
    body_sha = handoff.canonical_sha256(body)
    replacement = {
        **body,
        "preregistration_body_sha256": body_sha,
    }
    path = root / f"mtvclc_v1_preregistration_{body_sha}.json"
    path.write_bytes(_canonical(replacement))
    return path


def _rewrite_preregistration(
    path: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> Path:
    payload = json.loads(path.read_text(encoding="utf-8"))
    body = {
        key: value
        for key, value in payload.items()
        if key != "preregistration_body_sha256"
    }
    mutate(body)
    body_sha = handoff.canonical_sha256(body)
    rewritten = {**body, "preregistration_body_sha256": body_sha}
    target = path.parent / f"mtvclc_v1_preregistration_{body_sha}.json"
    target.write_bytes(_canonical(rewritten))
    return target


def _source(source_id: str) -> dict[str, Any]:
    return {
        "market_source_schema": handoff.MARKET_SOURCE_SCHEMA,
        "market_source_id": source_id,
        "market_source_authenticated": True,
        "broker_account_mode": "demo",
        "broker_venue_id": handoff.VENUE_ID,
        "broker_account_scope_sha256": "2" * 64,
        "producer_identity_sha256": "3" * 64,
        "producer_instance_id_sha256": "4" * 64,
        "terminal_lease_scope_sha256": "5" * 64,
        "credential_generation_id_sha256": "6" * 64,
        "bridge_protocol_version": handoff.BRIDGE_PROTOCOL_VERSION,
    }


def _bar(symbol: str, minute: int) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "minute_epoch": minute,
        "bid_open": 1.0,
        "bid_high": 1.2,
        "bid_low": 0.9,
        "bid_close": 1.1,
        "tick_volume": 10,
        "price_basis": handoff.PRICE_BASIS,
        "volume_source": handoff.VOLUME_SOURCE,
    }


def _quote(symbol: str, sequence: int, epoch: float) -> dict[str, Any]:
    token_hash = hashlib.sha256(f"{symbol}:{sequence}".encode("ascii")).hexdigest()
    event_received = epoch - 0.1
    snapshot = {
        "symbol": symbol,
        "transport_received_at_epoch": epoch,
        "bid": 1.0,
        "ask": 1.0002,
        "market_event_received_at_epoch": event_received,
        "market_event_sequence": sequence,
        "source_event_token_sha256": token_hash,
    }
    return {
        "symbol": symbol,
        "observation_sequence": sequence,
        "observation_epoch": int(epoch),
        "observed_at_epoch": epoch,
        "bid": 1.0,
        "ask": 1.0002,
        "transport_received_at_epoch": epoch,
        "market_event_received_at_epoch": event_received,
        "market_event_sequence": sequence,
        "source_event_token_sha256": token_hash,
        "snapshot_sha256": handoff.canonical_sha256(snapshot),
    }


def _maps(
    *,
    bars: Mapping[str, int],
    sequences: Mapping[str, int],
    transports: Mapping[str, float],
    snapshots: Mapping[str, str],
) -> tuple[dict[str, int], dict[str, int], dict[str, float], dict[str, str]]:
    return (
        {symbol: int(bars[symbol]) for symbol in handoff.SYMBOLS},
        {symbol: int(sequences[symbol]) for symbol in handoff.SYMBOLS},
        {symbol: float(transports[symbol]) for symbol in handoff.SYMBOLS},
        {symbol: str(snapshots[symbol]) for symbol in handoff.SYMBOLS},
    )


def _chunk(
    *,
    binding: handoff.ProspectiveBinding,
    source_id: str,
    utc_hour: str,
    cycle_started: float,
    cycle_completed: float,
    bars: list[dict[str, Any]],
    quotes: list[dict[str, Any]],
    state_maps: tuple[dict[str, int], dict[str, int], dict[str, float], dict[str, str]],
) -> dict[str, Any]:
    last_bars, last_sequences, last_transports, last_snapshots = state_maps
    return {
        "schema_version": handoff.CHUNK_SCHEMA,
        "collector_schema_version": binding.collector_schema_version,
        "source_contract_id": handoff.SOURCE_CONTRACT_ID,
        "activity_metric_id": handoff.ACTIVITY_METRIC_ID,
        "scope_version": handoff.SCOPE_VERSION,
        "symbol_scope": list(handoff.SYMBOLS),
        "timeframe": handoff.TIMEFRAME,
        "minimum_m1_history_bars": handoff.MINIMUM_M1_BARS,
        "maximum_quote_gap_seconds": handoff.MAXIMUM_QUOTE_GAP_SECONDS,
        "requested_bar_limit": 400,
        "configured_tick_interval_seconds": 2.0,
        "utc_hour": utc_hour,
        "segment_index": 1,
        "collector_cycle_started_at_epoch": cycle_started,
        "collector_cycle_completed_at_epoch": cycle_completed,
        "observed_at_epoch": cycle_completed,
        "preregistration_body_sha256": binding.preregistration_body_sha256,
        "preregistration_artifact_sha256": (binding.preregistration_artifact_sha256),
        "prospective_t0_utc_inclusive": binding.t0_utc,
        "prospective_end_utc_exclusive": binding.end_utc_exclusive,
        "source": _source(source_id),
        "bars": bars,
        "quotes": quotes,
        "last_bar_epoch_by_symbol": last_bars,
        "last_tick_sequence_by_symbol": last_sequences,
        "last_tick_transport_epoch_by_symbol": last_transports,
        "last_tick_snapshot_sha256_by_symbol": last_snapshots,
        "collection_only": True,
        "evaluation_performed": False,
        "success_claim_authorized": False,
        "authority_granted": False,
        "activation_authorized": False,
        "order_authorized": False,
    }


def _write_capture(
    root: Path,
    preregistration: Path,
    *,
    last_quote_lag_seconds: float = 19.0,
    final_quote_sequence: int = 2,
) -> Path:
    binding = handoff.load_preregistration(preregistration)
    capture_root = root / "capture"
    chunks_root = capture_root / handoff.CHUNK_DIRECTORY
    chunks_root.mkdir(parents=True)
    source_id = "7" * 64
    t0_epoch = T0.timestamp()
    end_epoch = END.timestamp()
    first_quote_epoch = t0_epoch + 1.0
    last_quote_epoch = end_epoch - last_quote_lag_seconds

    initial_bars = [
        _bar(symbol, minute)
        for symbol in handoff.SYMBOLS
        for minute in range(
            int(t0_epoch) - handoff.MINIMUM_M1_BARS * 60,
            int(t0_epoch),
            60,
        )
    ]
    initial_quotes = [
        _quote(symbol, 1, first_quote_epoch) for symbol in handoff.SYMBOLS
    ]
    state_bars = {symbol: int(t0_epoch) - 60 for symbol in handoff.SYMBOLS}
    state_sequences = {symbol: 1 for symbol in handoff.SYMBOLS}
    state_transports = {symbol: first_quote_epoch for symbol in handoff.SYMBOLS}
    state_snapshots = {
        quote["symbol"]: quote["snapshot_sha256"] for quote in initial_quotes
    }
    first_maps = _maps(
        bars=state_bars,
        sequences=state_sequences,
        transports=state_transports,
        snapshots=state_snapshots,
    )
    first_hour = datetime.fromtimestamp(first_quote_epoch, tz=UTC).strftime("%Y%m%dT%H")
    first_chunk = _chunk(
        binding=binding,
        source_id=source_id,
        utc_hour=first_hour,
        cycle_started=t0_epoch + 0.5,
        cycle_completed=t0_epoch + 2.0,
        bars=initial_bars,
        quotes=initial_quotes,
        state_maps=first_maps,
    )

    final_cycle_started = last_quote_epoch - 1.0
    final_minute = int((final_cycle_started - 60.0) // 60) * 60
    final_bars = [_bar(symbol, final_minute) for symbol in handoff.SYMBOLS]
    final_quotes = [
        _quote(symbol, final_quote_sequence, last_quote_epoch)
        for symbol in handoff.SYMBOLS
    ]
    state_bars = {symbol: final_minute for symbol in handoff.SYMBOLS}
    state_sequences = {symbol: final_quote_sequence for symbol in handoff.SYMBOLS}
    state_transports = {symbol: last_quote_epoch for symbol in handoff.SYMBOLS}
    state_snapshots = {
        quote["symbol"]: quote["snapshot_sha256"] for quote in final_quotes
    }
    final_maps = _maps(
        bars=state_bars,
        sequences=state_sequences,
        transports=state_transports,
        snapshots=state_snapshots,
    )
    final_hour = datetime.fromtimestamp(last_quote_epoch, tz=UTC).strftime("%Y%m%dT%H")
    final_chunk = _chunk(
        binding=binding,
        source_id=source_id,
        utc_hour=final_hour,
        cycle_started=final_cycle_started,
        cycle_completed=last_quote_epoch + 1.0,
        bars=final_bars,
        quotes=final_quotes,
        state_maps=final_maps,
    )

    manifest_lines: list[bytes] = []
    previous = handoff.ZERO_SHA256
    for sequence, (utc_hour, chunk, maps) in enumerate(
        (
            (first_hour, first_chunk, first_maps),
            (final_hour, final_chunk, final_maps),
        ),
        start=1,
    ):
        relative = Path(
            handoff.CHUNK_DIRECTORY,
            utc_hour,
            f"ig-mt4-m1-activity-s0001-q{sequence:010d}.json",
        ).as_posix()
        chunk_path = capture_root.joinpath(*Path(relative).parts)
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_raw = _canonical(chunk) + b"\n"
        chunk_path.write_bytes(chunk_raw)
        last_bars, last_sequences, last_transports, last_snapshots = maps
        entry_body = {
            "schema_version": handoff.MANIFEST_SCHEMA,
            "sequence": sequence,
            "previous_entry_sha256": previous,
            "chunk_path": relative,
            "chunk_sha256": hashlib.sha256(chunk_raw).hexdigest(),
            "chunk_size_bytes": len(chunk_raw),
            "chunk_schema_version": handoff.CHUNK_SCHEMA,
            "utc_hour": utc_hour,
            "segment_index": 1,
            "market_source_id": source_id,
            "preregistration_body_sha256": binding.preregistration_body_sha256,
            "preregistration_artifact_sha256": (
                binding.preregistration_artifact_sha256
            ),
            "prospective_t0_utc_inclusive": binding.t0_utc,
            "prospective_end_utc_exclusive": binding.end_utc_exclusive,
            "bar_rows": len(chunk["bars"]),
            "quote_rows": len(chunk["quotes"]),
            "last_bar_epoch_by_symbol": last_bars,
            "last_tick_sequence_by_symbol": last_sequences,
            "last_tick_transport_epoch_by_symbol": last_transports,
            "last_tick_snapshot_sha256_by_symbol": last_snapshots,
        }
        previous = handoff.canonical_sha256(entry_body)
        manifest_lines.append(
            _canonical({**entry_body, "manifest_entry_sha256": previous}) + b"\n"
        )
    (capture_root / handoff.MANIFEST_FILENAME).write_bytes(b"".join(manifest_lines))
    if binding.profile == handoff.PROFILE_REPLACEMENT:
        (capture_root / handoff.DATA_WRITER_LOCK_FILENAME).write_bytes(b"\0")
        _write_guard_identity(capture_root, binding)
    return capture_root


def _write_guard_identity(
    capture_root: Path,
    binding: handoff.ProspectiveBinding,
) -> Path:
    policy = {
        "tick_interval_secs": 2.0,
        "bar_interval_secs": 60.0,
        "bar_limit": 400,
        "http_timeout_secs": 5.0,
        "rollover_mode": "refuse",
    }
    integrity = handoff._replacement_capture_integrity_contract(
        binding.collector_source_sha256
    )
    resume = {
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
    source_root = capture_root.parent.resolve()
    payload = {
        "schema_version": handoff.GUARD_IDENTITY_SCHEMA,
        "collection_only": True,
        "evaluation_performed": False,
        "signal_computation_authorized": False,
        "outcome_access_authorized": False,
        "performance_computation_authorized": False,
        "success_claim_authorized": False,
        "issuer_authorized": False,
        "signature_authorized": False,
        "authority_granted": False,
        "runtime_authorized": False,
        "activation_authorized": False,
        "broker_access_authorized": False,
        "order_authorized": False,
        "collector_source_path": str(
            source_root / "capture_ig_mt4_m1_activity_resilient.py"
        ),
        "collector_source_sha256": binding.collector_source_sha256,
        "collector_support_source_path": str(
            source_root / "capture_ig_mt4_m1_activity.py"
        ),
        "collector_support_source_sha256": (binding.collector_support_source_sha256),
        "continuity_inspector_source_path": str(
            source_root / "check_mt4_tick_volume_collector_continuity_resilient.py"
        ),
        "continuity_inspector_source_sha256": (
            handoff.RESILIENT_CONTINUITY_INSPECTOR_SHA256
        ),
        "capture_integrity_contract": integrity,
        "capture_integrity_contract_sha256": handoff.canonical_sha256(integrity),
        "preregistration_path": str(binding.preregistration_path),
        "preregistration_body_sha256": binding.preregistration_body_sha256,
        "preregistration_artifact_sha256": (binding.preregistration_artifact_sha256),
        "output_root": str(capture_root.resolve()),
        "data_writer_lock_path": str(
            (capture_root / handoff.DATA_WRITER_LOCK_FILENAME).resolve()
        ),
        "api_key_file_path": str(source_root / "bridge_api_key.txt"),
        "bridge_base_url": "http://127.0.0.1:58710",
        "prospective_t0_utc_inclusive": binding.t0_utc,
        "prospective_end_utc_exclusive": binding.end_utc_exclusive,
        "policy": policy,
        "policy_sha256": handoff.canonical_sha256(policy),
        "resume_contract": resume,
    }
    target = capture_root / handoff.GUARD_IDENTITY_FILENAME
    target.write_bytes(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    return target


def _rewrite_final_chunk_and_manifest(
    capture_root: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    manifest_path = capture_root / handoff.MANIFEST_FILENAME
    entries = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
    ]
    final_entry = entries[-1]
    chunk_path = capture_root.joinpath(*Path(final_entry["chunk_path"]).parts)
    chunk = json.loads(chunk_path.read_text(encoding="utf-8"))
    mutate(chunk)
    chunk_raw = _canonical(chunk) + b"\n"
    chunk_path.write_bytes(chunk_raw)
    final_entry["chunk_sha256"] = hashlib.sha256(chunk_raw).hexdigest()
    final_entry["chunk_size_bytes"] = len(chunk_raw)
    final_body = dict(final_entry)
    final_body.pop("manifest_entry_sha256")
    final_entry["manifest_entry_sha256"] = handoff.canonical_sha256(final_body)
    manifest_path.write_bytes(b"".join(_canonical(entry) + b"\n" for entry in entries))


@contextmanager
def _held_byte_lock(path: Path) -> Iterator[None]:
    handle = path.open("r+b")
    lock_api: Any
    try:
        handle.seek(0)
        if os.name == "nt":
            lock_api = __import__("msvcrt")
            lock_api.locking(handle.fileno(), int(lock_api.LK_NBLCK), 1)
        else:
            lock_api = __import__("fcntl")
            lock_api.flock(
                handle.fileno(), int(lock_api.LOCK_EX) | int(lock_api.LOCK_NB)
            )
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                lock_api.locking(handle.fileno(), int(lock_api.LK_UNLCK), 1)
            else:
                lock_api.flock(handle.fileno(), int(lock_api.LOCK_UN))
    finally:
        handle.close()


def test_post_window_handoff_verifies_every_bound_row_without_outcomes(
    tmp_path: Path,
) -> None:
    preregistration = _write_preregistration(tmp_path)
    capture_root = _write_capture(tmp_path, preregistration)

    result = handoff.verify_capture_handoff(
        preregistration_path=preregistration,
        capture_root=capture_root,
        now_epoch=END.timestamp(),
    )

    inventory = result["capture_inventory"]
    assert result["window_closed"] is True
    assert result["manifest_and_chunks_verified"] is True
    assert result["outcome_evaluation_performed"] is False
    assert result["performance_statistics_computed"] is False
    assert result["authority"] == handoff.FALSE_AUTHORITY
    assert inventory["manifest_entries"] == 2
    assert inventory["quote_rows"] == len(handoff.SYMBOLS) * 2
    assert inventory["bar_rows"] == len(handoff.SYMBOLS) * (handoff.MINIMUM_M1_BARS + 1)
    assert all(
        epoch < T0.timestamp()
        for epoch in inventory["first_bar_epoch_by_symbol"].values()
    )
    assert inventory["quote_rows_by_symbol"] == {
        symbol: 2 for symbol in handoff.SYMBOLS
    }
    assert inventory["referenced_chunk_files"] == 2
    assert inventory["orphan_chunk_files"] == 0
    assert result["handoff_body_sha256"] == handoff.canonical_sha256(
        {key: value for key, value in result.items() if key != "handoff_body_sha256"}
    )


def test_replacement_profile_verifies_with_unchanged_v1_handoff_topology(
    tmp_path: Path,
) -> None:
    preregistration = _write_replacement_preregistration(tmp_path)
    binding = handoff.load_preregistration(preregistration)
    capture_root = _write_capture(tmp_path, preregistration)

    result = handoff.verify_capture_handoff(
        preregistration_path=preregistration,
        capture_root=capture_root,
        now_epoch=END.timestamp(),
    )

    assert binding.profile == handoff.PROFILE_REPLACEMENT
    assert binding.collector_schema_version == handoff.REPLACEMENT_COLLECTOR_SCHEMA
    assert set(result) == {
        "schema_version",
        "strategy_id",
        "strategy_version",
        "config_id",
        "venue_id",
        "scope_version",
        "symbol_scope",
        "source_contract_id",
        "activity_metric_id",
        "capture_inventory",
        "capture_inventory_sha256",
        "window_closed",
        "manifest_and_chunks_verified",
        "outcome_evaluation_performed",
        "performance_statistics_computed",
        "research_only",
        "authority",
        "handoff_body_sha256",
    }
    assert result["schema_version"] == handoff.HANDOFF_SCHEMA
    assert result["capture_inventory"]["manifest_entries"] == 2
    guard_path = capture_root / handoff.GUARD_IDENTITY_FILENAME
    assert result["capture_inventory"]["guard_identity_sha256"] == (
        hashlib.sha256(guard_path.read_bytes()).hexdigest()
    )
    assert not any(result["authority"].values())
    assert (
        handoff.REPLACEMENT_ATTEMPT_MANIFEST["win_probability_alpha_allocation"]
        == "one_sided_0.05_over_4786"
    )
    assert handoff.REPLACEMENT_ATTEMPT_MANIFEST[
        "descriptive_df99_bonferroni_abs_t_threshold"
    ] == pytest.approx(4.645748208417252, rel=0.0, abs=0.0)


def _mutate_attempt_alpha(body: dict[str, Any]) -> None:
    manifest = body["strategy"]["attempt_manifest"]
    manifest["win_probability_alpha_allocation"] = "one_sided_0.05_over_4698"
    body["strategy"]["attempt_manifest_sha256"] = handoff.canonical_sha256(manifest)


def _mutate_attempt_threshold(body: dict[str, Any]) -> None:
    manifest = body["strategy"]["attempt_manifest"]
    manifest["descriptive_df99_bonferroni_abs_t_threshold"] = 4.641077835714861
    body["strategy"]["attempt_manifest_sha256"] = handoff.canonical_sha256(manifest)
    body["fixed_success_gates"]["descriptive_df99_bonferroni_abs_t_threshold"] = (
        4.641077835714861
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda body: body["attempt_accounting"].__setitem__(
                "prior_attempted_cells_lower_bound", 4_654
            ),
            id="accounting",
        ),
        pytest.param(
            lambda body: body["replacement_lineage"].__setitem__(
                "old_window_restart_or_extension", True
            ),
            id="replacement-lineage",
        ),
        pytest.param(
            lambda body: body["capture_integrity_contract"].__setitem__(
                "revised_later_overlap_is_ignored", False
            ),
            id="capture-integrity",
        ),
        pytest.param(_mutate_attempt_alpha, id="attempt-alpha"),
        pytest.param(_mutate_attempt_threshold, id="attempt-threshold"),
        pytest.param(
            lambda body: body["strategy"].__setitem__("config_sha256", "0" * 64),
            id="frozen-config",
        ),
        pytest.param(
            lambda body: body["source_identities"]["collector_source"].__setitem__(
                "filename", "capture_ig_mt4_m1_activity.py"
            ),
            id="collector-identity-topology",
        ),
        pytest.param(
            lambda body: body["source_identities"]["screen_support_source"].__setitem__(
                "sha256", "0" * 64
            ),
            id="preserved-screen-identity",
        ),
        pytest.param(
            lambda body: body["cost_policy"]["symbols"]["EURUSD"].__setitem__(
                "pre_conversion_geometry_cost_bps", 3.6
            ),
            id="cost-policy",
        ),
        pytest.param(
            lambda body: body["scope"]["cell_order"][0].__setitem__("side", "SELL"),
            id="cell-scope",
        ),
    ],
)
def test_replacement_profile_refuses_rehashed_contract_mutation(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    preregistration = _write_replacement_preregistration(tmp_path)
    mutated = _rewrite_preregistration(preregistration, mutate)

    with pytest.raises(handoff.HandoffRefusal, match="preregistration_"):
        handoff.load_preregistration(mutated)


def test_replacement_profile_cannot_be_selected_as_base(tmp_path: Path) -> None:
    preregistration = _write_replacement_preregistration(tmp_path)
    with pytest.raises(
        handoff.HandoffRefusal, match="preregistration_profile_mismatch"
    ):
        handoff.load_preregistration(
            preregistration,
            profile=handoff.PROFILE_BASE,
        )


def test_replacement_refuses_before_window_without_opening_capture(
    tmp_path: Path,
) -> None:
    preregistration = _write_replacement_preregistration(tmp_path)
    with pytest.raises(handoff.HandoffRefusal, match="prospective_window_not_closed"):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=tmp_path / "not-transferred",
            now_epoch=END.timestamp() - 0.001,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("collector_schema_version", handoff.COLLECTOR_SCHEMA),
        ("requested_bar_limit", 399),
        ("configured_tick_interval_seconds", 3.0),
    ],
)
def test_replacement_profile_refuses_rehashed_mixed_schema_or_policy(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    preregistration = _write_replacement_preregistration(tmp_path)
    capture_root = _write_capture(tmp_path, preregistration)
    _rewrite_final_chunk_and_manifest(
        capture_root,
        lambda chunk: chunk.__setitem__(field, value),
    )

    with pytest.raises(handoff.HandoffRefusal, match="capture_chunk_contract_invalid"):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=capture_root,
            now_epoch=END.timestamp(),
        )


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        (
            handoff.ACTIVE_HOUR_JOURNAL_FILENAME,
            "capture_active_hour_journal_present",
        ),
        (".orphan.tmp", "capture_final_topology_invalid"),
        ("manifest.extra.jsonl", "capture_final_topology_invalid"),
    ],
)
def test_replacement_profile_refuses_unfinalized_or_extra_root_artifact(
    tmp_path: Path,
    name: str,
    reason: str,
) -> None:
    preregistration = _write_replacement_preregistration(tmp_path)
    capture_root = _write_capture(tmp_path, preregistration)
    (capture_root / name).write_text("orphan\n", encoding="utf-8")

    with pytest.raises(handoff.HandoffRefusal, match=reason):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=capture_root,
            now_epoch=END.timestamp(),
        )


def test_replacement_profile_refuses_missing_or_held_data_writer_lock(
    tmp_path: Path,
) -> None:
    preregistration = _write_replacement_preregistration(tmp_path)
    capture_root = _write_capture(tmp_path, preregistration)
    lock_path = capture_root / handoff.DATA_WRITER_LOCK_FILENAME
    lock_path.unlink()
    with pytest.raises(handoff.HandoffRefusal, match="capture_final_topology_invalid"):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=capture_root,
            now_epoch=END.timestamp(),
        )

    lock_path.write_bytes(b"\0")
    with _held_byte_lock(lock_path):
        with pytest.raises(
            handoff.HandoffRefusal, match="capture_data_writer_still_active"
        ):
            handoff.verify_capture_handoff(
                preregistration_path=preregistration,
                capture_root=capture_root,
                now_epoch=END.timestamp(),
            )


def test_replacement_profile_probes_stopped_read_only_data_lock(
    tmp_path: Path,
) -> None:
    preregistration = _write_replacement_preregistration(tmp_path)
    capture_root = _write_capture(tmp_path, preregistration)
    lock_path = capture_root / handoff.DATA_WRITER_LOCK_FILENAME
    lock_path.chmod(stat.S_IREAD)
    try:
        result = handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=capture_root,
            now_epoch=END.timestamp(),
        )
    finally:
        lock_path.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert result["manifest_and_chunks_verified"] is True


def test_replacement_profile_requires_exact_hash_bound_guard_identity(
    tmp_path: Path,
) -> None:
    preregistration = _write_replacement_preregistration(tmp_path)
    capture_root = _write_capture(tmp_path, preregistration)
    guard_path = capture_root / handoff.GUARD_IDENTITY_FILENAME
    guard_path.unlink()
    with pytest.raises(handoff.HandoffRefusal, match="capture_final_topology_invalid"):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=capture_root,
            now_epoch=END.timestamp(),
        )

    binding = handoff.load_preregistration(preregistration)
    _write_guard_identity(capture_root, binding)
    payload = json.loads(guard_path.read_text(encoding="utf-8"))
    payload["collector_source_sha256"] = "f" * 64
    guard_path.write_bytes(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    with pytest.raises(
        handoff.HandoffRefusal, match="capture_guard_identity_contract_invalid"
    ):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=capture_root,
            now_epoch=END.timestamp(),
        )


def test_replacement_profile_refuses_rehashed_guard_policy_drift(
    tmp_path: Path,
) -> None:
    preregistration = _write_replacement_preregistration(tmp_path)
    capture_root = _write_capture(tmp_path, preregistration)
    guard_path = capture_root / handoff.GUARD_IDENTITY_FILENAME
    payload = json.loads(guard_path.read_text(encoding="utf-8"))
    payload["policy"]["tick_interval_secs"] = 3.0
    payload["policy_sha256"] = handoff.canonical_sha256(payload["policy"])
    guard_path.write_bytes(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    with pytest.raises(
        handoff.HandoffRefusal, match="capture_guard_identity_contract_invalid"
    ):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=capture_root,
            now_epoch=END.timestamp(),
        )


def test_replacement_profile_refuses_orphan_chunk(tmp_path: Path) -> None:
    preregistration = _write_replacement_preregistration(tmp_path)
    capture_root = _write_capture(tmp_path, preregistration)
    hour_root = next((capture_root / handoff.CHUNK_DIRECTORY).iterdir())
    (hour_root / "ig-mt4-m1-activity-s0001-q9999999999.json").write_text(
        "{}\n", encoding="utf-8"
    )
    with pytest.raises(
        handoff.HandoffRefusal,
        match="capture_orphan_or_invalid_chunk_detected",
    ):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=capture_root,
            now_epoch=END.timestamp(),
        )


def test_handoff_refuses_before_fixed_window_end_without_opening_capture(
    tmp_path: Path,
) -> None:
    preregistration = _write_preregistration(tmp_path)
    with pytest.raises(handoff.HandoffRefusal, match="prospective_window_not_closed"):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=tmp_path / "does-not-exist",
            now_epoch=END.timestamp() - 0.001,
        )


def test_handoff_refuses_early_stopped_capture(tmp_path: Path) -> None:
    preregistration = _write_preregistration(tmp_path)
    capture_root = _write_capture(
        tmp_path,
        preregistration,
        last_quote_lag_seconds=60.0,
    )
    with pytest.raises(handoff.HandoffRefusal, match="window_end_not_closed"):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=capture_root,
            now_epoch=END.timestamp(),
        )


def test_handoff_refuses_chunk_tamper(tmp_path: Path) -> None:
    preregistration = _write_preregistration(tmp_path)
    capture_root = _write_capture(tmp_path, preregistration)
    first_chunk = next((capture_root / handoff.CHUNK_DIRECTORY).rglob("*.json"))
    raw = first_chunk.read_bytes()
    first_chunk.write_bytes(raw.replace(b'"tick_volume":10', b'"tick_volume":11', 1))

    with pytest.raises(
        handoff.HandoffRefusal, match="manifest_chunk_hash_or_size_mismatch"
    ):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=capture_root,
            now_epoch=END.timestamp(),
        )


def test_handoff_refuses_orphan_chunk(tmp_path: Path) -> None:
    preregistration = _write_preregistration(tmp_path)
    capture_root = _write_capture(tmp_path, preregistration)
    hour_root = next((capture_root / handoff.CHUNK_DIRECTORY).iterdir())
    (hour_root / "ig-mt4-m1-activity-s0001-q9999999999.json").write_text(
        "{}\n", encoding="utf-8"
    )

    with pytest.raises(
        handoff.HandoffRefusal,
        match="capture_orphan_or_invalid_chunk_detected",
    ):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=capture_root,
            now_epoch=END.timestamp(),
        )


def test_handoff_refuses_quote_sequence_gap_even_when_files_are_rehashed(
    tmp_path: Path,
) -> None:
    preregistration = _write_preregistration(tmp_path)
    capture_root = _write_capture(
        tmp_path,
        preregistration,
        final_quote_sequence=3,
    )
    with pytest.raises(
        handoff.HandoffRefusal,
        match="capture_quote_time_or_sequence_invalid",
    ):
        handoff.verify_capture_handoff(
            preregistration_path=preregistration,
            capture_root=capture_root,
            now_epoch=END.timestamp(),
        )


def test_published_handoff_is_content_addressed_and_no_overwrite(
    tmp_path: Path,
) -> None:
    preregistration = _write_preregistration(tmp_path)
    capture_root = _write_capture(tmp_path, preregistration)
    result = handoff.verify_capture_handoff(
        preregistration_path=preregistration,
        capture_root=capture_root,
        now_epoch=END.timestamp(),
    )
    output_root = tmp_path / "handoff"
    output = handoff.publish_handoff(output_root=output_root, handoff=result)
    assert output.name == f"mtvclc_capture_handoff_{result['handoff_body_sha256']}.json"
    assert json.loads(output.read_text(encoding="utf-8")) == result
    with pytest.raises(handoff.HandoffRefusal, match="handoff_output_exists"):
        handoff.publish_handoff(output_root=output_root, handoff=result)


def test_handoff_tool_has_no_live_or_evaluation_surface() -> None:
    source = Path(handoff.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    assert imported_roots.isdisjoint(
        {
            "requests",
            "urllib",
            "socket",
            "httpx",
            "sqlalchemy",
            "psycopg",
            "fxstack",
        }
    )
    lowered = source.lower()
    assert "--base-url" not in lowered
    assert "--api-key" not in lowered
    assert "signing-key" not in lowered
    assert "screen_universe(" not in source
