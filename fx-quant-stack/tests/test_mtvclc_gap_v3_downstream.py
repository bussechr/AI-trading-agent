from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SEALER_TEST_PATH = Path(__file__).with_name(
    "test_seal_mt4_tick_volume_preregistration_resilient_v3.py"
)
COLLECTOR_HELPER_PATH = REPO_ROOT / "tests" / "test_capture_ig_mt4_m1_activity.py"


def _load(name: str, path: Path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sealer_helpers = _load("mtvclc_gap_v3_sealer_test_helpers", SEALER_TEST_PATH)
collector_helpers = _load("mtvclc_gap_v3_capture_test_helpers", COLLECTOR_HELPER_PATH)

from fxstack.runtime import mtvclc_validation_evidence_v2 as public  # noqa: E402
from fxstack.scalp import (  # noqa: E402
    screen_mt4_tick_volume_close_location_continuation as base_screen,
)

from tools import evaluate_mt4_tick_volume_post_window_v3 as evaluator  # noqa: E402
from tools import mtvclc_validation_release_v3 as release  # noqa: E402
from tools import verify_mt4_tick_volume_capture_handoff_v3 as handoff  # noqa: E402


def _write_preregistration(
    root: Path,
) -> tuple[Path, dict[str, Any], tuple[Path, ...]]:
    sealer_root = root / "sealer"
    sealer_root.mkdir()
    payload, inputs = sealer_helpers._payload(sealer_root)
    path = root / (
        "mtvclc_gap_v3_preregistration_"
        f"{payload['preregistration_body_sha256']}.json"
    )
    path.write_text(
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path, payload, inputs


def _inventory(
    *,
    preregistration_path: Path,
    preregistration: Mapping[str, Any],
    guard_identity_sha256: str = "4" * 64,
) -> dict[str, Any]:
    t0 = datetime.strptime(
        preregistration["prospective_window"]["t0_utc_inclusive"],
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=UTC).timestamp()
    end = datetime.strptime(
        preregistration["prospective_window"]["end_utc_exclusive"],
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=UTC).timestamp()
    symbols = handoff.SYMBOLS
    market_source = "3" * 64
    producer = preregistration["upstream_producer_software"]
    inventory: dict[str, Any] = {
        "preregistration_body_sha256": preregistration[
            "preregistration_body_sha256"
        ],
        "preregistration_artifact_sha256": hashlib.sha256(
            preregistration_path.read_bytes()
        ).hexdigest(),
        "prospective_t0_utc_inclusive": preregistration["prospective_window"][
            "t0_utc_inclusive"
        ],
        "prospective_end_utc_exclusive": preregistration["prospective_window"][
            "end_utc_exclusive"
        ],
        "manifest_sha256": "1" * 64,
        "manifest_head_sha256": "2" * 64,
        "manifest_entries": 2,
        "market_source_id": market_source,
        "segment_count": 1,
        "bar_rows": len(symbols) * 2,
        "quote_rows": len(symbols) * 2,
        "bar_rows_by_symbol": {symbol: 2 for symbol in symbols},
        "quote_rows_by_symbol": {symbol: 2 for symbol in symbols},
        "first_bar_epoch_by_symbol": {symbol: int(t0) for symbol in symbols},
        "first_quote_epoch_by_symbol": {symbol: t0 for symbol in symbols},
        "last_quote_epoch_by_symbol": {symbol: end - 1.0 for symbol in symbols},
        "last_bar_epoch_by_symbol": {symbol: int(end) - 60 for symbol in symbols},
        "maximum_transport_gap_seconds_by_symbol": {
            symbol: 2.0 for symbol in symbols
        },
        "transport_gap_count_over_five_seconds_by_symbol": {
            symbol: 0 for symbol in symbols
        },
        "referenced_chunk_files": 2,
        "orphan_chunk_files": 0,
        "guard_identity_sha256": guard_identity_sha256,
        "collector_source_sha256": handoff.collector.MODULE_SOURCE_SHA256,
        "collector_wrapper_source_sha256": handoff.collector.SUPPORT_SHA256,
        "collector_base_source_sha256": handoff.collector.BASE_SUPPORT_SHA256,
        "gap_chain_schema_version": handoff.collector.LATE_GAP_RECORD_SCHEMA_VERSION,
        "gap_chain_count": 0,
        "gap_event_count": 0,
        "gap_root_sha256": handoff.ZERO_SHA256,
        "gap_tail_sha256": handoff.ZERO_SHA256,
        "gap_source_id": market_source,
        "start_edge_receipt_sha256": "5" * 64,
        "first_cycle_durable_at_epoch": t0 + 1.0,
        "post_window_finalization_receipt_artifact_sha256": "b" * 64,
        "post_window_finalization_receipt_sha256": "c" * 64,
        "post_window_finalized_after_end_observed_at_epoch": end,
        "upstream_producer_software_body_sha256": producer[
            "producer_software_body_sha256"
        ],
        "bridge_ea_repository_source_identity": dict(producer["repository_source"]),
        "bridge_ea_deployed_source_identity": dict(producer["deployed_source"]),
        "bridge_ea_deployed_ex4_identity": dict(producer["deployed_ex4"]),
    }
    state = {
        "state_kind": "manifest",
        "manifest_sequence": inventory["manifest_entries"],
        "manifest_entry_sha256": inventory["manifest_head_sha256"],
        "manifest_size_bytes": 2_000,
        "journal_manifest_sequence_target": 0,
        "journal_sequence": 0,
        "journal_entry_sha256": handoff.ZERO_SHA256,
        "journal_size_bytes": 0,
        "covered_journal_manifest_sequence_target": inventory["manifest_entries"],
        "covered_journal_sequence": 1,
        "covered_journal_entry_sha256": "6" * 64,
        "covered_journal_size_bytes": 1_000,
        "latest_chunk_path": "chunks/20260804T00/segment-000002.json",
        "latest_chunk_sha256": "7" * 64,
        "latest_chunk_size_bytes": 1_000,
        "gap_chain_count": inventory["gap_chain_count"],
        "gap_event_count": inventory["gap_event_count"],
        "gap_root_sha256": inventory["gap_root_sha256"],
        "gap_tail_sha256": inventory["gap_tail_sha256"],
        "gap_source_id": inventory["gap_source_id"],
        "preregistration_body_sha256": inventory["preregistration_body_sha256"],
        "preregistration_artifact_sha256": inventory[
            "preregistration_artifact_sha256"
        ],
        "prospective_t0_utc_inclusive": inventory[
            "prospective_t0_utc_inclusive"
        ],
        "prospective_end_utc_exclusive": inventory[
            "prospective_end_utc_exclusive"
        ],
        "cycle_reservation_sequence": 1,
        "last_cycle_reservation_sha256": "d" * 64,
        "unresolved_cycle_reservation_sha256": handoff.ZERO_SHA256,
        "attempt_failure_sha256": handoff.ZERO_SHA256,
    }
    inventory["capture_tail_commitment_proof"] = {
        "status": "valid",
        "schema_version": handoff.collector.TAIL_COMMITMENT_SCHEMA_VERSION,
        "filename": handoff.TAIL_COMMITMENT_FILENAME,
        "capture_integrity_contract_sha256": handoff.canonical_sha256(
            preregistration["capture_integrity_contract"]
        ),
        "collector_source_sha256": handoff.collector.MODULE_SOURCE_SHA256,
        "collector_wrapper_source_sha256": handoff.collector.SUPPORT_SHA256,
        "collector_base_source_sha256": handoff.collector.BASE_SUPPORT_SHA256,
        "artifact_sha256": "8" * 64,
        "artifact_size_bytes": 5_000,
        "record_count": 6,
        "registry_sequence": 6,
        "genesis_entry_sha256": "9" * 64,
        "tail_entry_sha256": "a" * 64,
        "committed_state_sha256": handoff.canonical_sha256(state),
        "committed_state_kind": state["state_kind"],
        **{key: value for key, value in state.items() if key != "state_kind"},
        "physical_journal_present": False,
        "pending_operation": False,
    }
    return inventory


def _write_handoff(
    root: Path,
    *,
    preregistration_path: Path,
    preregistration: Mapping[str, Any],
    guard_identity_sha256: str = "4" * 64,
) -> tuple[Path, dict[str, Any]]:
    inventory = _inventory(
        preregistration_path=preregistration_path,
        preregistration=preregistration,
        guard_identity_sha256=guard_identity_sha256,
    )
    body: dict[str, Any] = {
        "schema_version": handoff.HANDOFF_SCHEMA,
        "strategy_id": handoff.STRATEGY_ID,
        "strategy_version": handoff.STRATEGY_VERSION,
        "config_id": handoff.CONFIG_ID,
        "venue_id": handoff.VENUE_ID,
        "scope_version": handoff.SCOPE_VERSION,
        "symbol_scope": list(handoff.SYMBOLS),
        "source_contract_id": handoff.SOURCE_CONTRACT_ID,
        "activity_metric_id": handoff.ACTIVITY_METRIC_ID,
        "capture_inventory": inventory,
        "capture_inventory_sha256": handoff.canonical_sha256(inventory),
        "window_closed": True,
        "manifest_and_chunks_verified": True,
        "outcome_evaluation_performed": False,
        "performance_statistics_computed": False,
        "research_only": True,
        "authority": dict(handoff.FALSE_AUTHORITY),
    }
    body["handoff_body_sha256"] = handoff.canonical_sha256(body)
    output = root / "handoff"
    output.mkdir()
    return handoff.publish_handoff(output_root=output, handoff=body), body


def _initialize_guard(
    root: Path,
    *,
    preregistration_path: Path,
) -> tuple[Path, dict[str, Any]]:
    """Publish the smallest legacy guard artifact the retained verifier accepts.

    The gap-v3 supervisor/inspector implementation is intentionally retired.  These
    downstream compatibility tests need only its immutable handoff artifact, not an
    executable supervision path.
    """

    capture_root = root / "capture"
    capture_root.mkdir()
    binding = handoff.load_preregistration(preregistration_path)
    guard = {
        "schema_version": "fxstack.research.collector-guard.identity.gap-v3.v1",
        "collector_source_sha256": binding.collector_source_sha256,
        "preregistration_body_sha256": binding.preregistration_body_sha256,
        "preregistration_artifact_sha256": binding.preregistration_artifact_sha256,
        "prospective_t0_utc_inclusive": binding.t0_utc,
        "prospective_end_utc_exclusive": binding.end_utc_exclusive,
        **binding.producer_receipt_fields(),
        **dict(handoff.FALSE_AUTHORITY),
        "collection_only": True,
    }
    guard_path = capture_root / handoff.GUARD_IDENTITY_FILENAME
    guard_raw = handoff.canonical_json_bytes(guard) + b"\n"
    guard_path.write_bytes(guard_raw)
    return capture_root, {
        **guard,
        "guard_identity_sha256": hashlib.sha256(guard_raw).hexdigest(),
    }


def _rewrite_handoff(root: Path, payload: dict[str, Any]) -> Path:
    payload["capture_inventory_sha256"] = handoff.canonical_sha256(
        payload["capture_inventory"]
    )
    payload.pop("handoff_body_sha256", None)
    payload["handoff_body_sha256"] = handoff.canonical_sha256(payload)
    path = root / (
        f"mtvclc_capture_handoff_v2_{payload['handoff_body_sha256']}.json"
    )
    path.write_bytes(handoff.canonical_json_bytes(payload) + b"\n")
    return path


def _passing_ledgers(
    preregistration: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    cost_rows = preregistration["cost_policy"]["symbols"]
    costs = release._screen_costs(cost_rows)
    reservations: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    start = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    for symbol in handoff.SYMBOLS:
        calibration = release.screen.MT4CostCalibration(**costs[symbol])
        for side_index, side in enumerate(("BUY", "SELL")):
            for index in range(60):
                signal_time = start + timedelta(days=side_index * 60 + index)
                signal_epoch = int(signal_time.timestamp())
                entry_epoch = signal_epoch + 60
                day = datetime.fromtimestamp(entry_epoch, tz=UTC).date().isoformat()
                reservation = {
                    "config_id": handoff.CONFIG_ID,
                    "symbol": symbol,
                    "side": side,
                    "signal_index": base_screen.BASELINE_M1_BARS + index,
                    "signal_epoch": signal_epoch,
                    "expected_entry_epoch": entry_epoch,
                    "entry_day": day,
                    "volume_v90": 100.0,
                    "signal_tick_volume": 101,
                    "bid_body_bps": 2.0,
                    "bid_close_location": 0.9,
                    "p90_spread_bps": calibration.p90_spread_bps,
                    "recorded_cost_bps": calibration.recorded_cost_bps,
                    "convert_on_close_charge_fraction": (
                        calibration.convert_on_close_charge_fraction
                    ),
                    "target_bps": (
                        base_screen.TARGET_COST_MULTIPLE
                        * calibration.recorded_cost_bps
                    ),
                    "stop_bps": (
                        base_screen.STOP_COST_MULTIPLE
                        * calibration.recorded_cost_bps
                    ),
                    "p_star": calibration.break_even_win_probability,
                    "entry_status": "admitted",
                }
                gross = float(reservation["target_bps"])
                conversion = (
                    abs(gross) * calibration.convert_on_close_charge_fraction
                )
                entry_price = 1.0
                exit_price = (
                    entry_price * (1.0 + gross / 1e4)
                    if side == "BUY"
                    else entry_price * (1.0 - gross / 1e4)
                )
                outcome = {
                    "config_id": handoff.CONFIG_ID,
                    "symbol": symbol,
                    "side": side,
                    "signal_epoch": signal_epoch,
                    "entry_day": day,
                    "entry_epoch": entry_epoch,
                    "exit_epoch": entry_epoch + 60,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "exit_reason": "TAKE_PROFIT",
                    "full_target_hit_first": True,
                    "gross_quote_bps": gross,
                    "recorded_cost_bps": calibration.recorded_cost_bps,
                    "currency_conversion_debit_bps": conversion,
                    "net_bps": gross
                    - calibration.recorded_cost_bps
                    - conversion,
                }
                reservations.append(reservation)
                outcomes.append(outcome)
    ready = {symbol: True for symbol in handoff.SYMBOLS}
    cells = release.screen.recompute_cells_from_ledgers(
        reservation_ledger=reservations,
        outcome_ledger=outcomes,
        costs=costs,
        source_ready_by_symbol=ready,
    )
    assert cells is not None
    assert all(cell["passes_fixed_cell_screen"] for cell in cells)
    return reservations, outcomes, cells


def _write_ledger(
    root: Path,
    *,
    kind: str,
    rows: list[dict[str, Any]],
    evidence_binding_sha256: str,
    handoff_body_sha256: str,
    capture_inventory_sha256: str,
) -> tuple[Path, dict[str, Any]]:
    lines: list[bytes] = []
    for sequence, row in enumerate(rows, start=1):
        wrapper = {
            "schema_version": release.LEDGER_ROW_SCHEMA,
            "ledger_kind": kind,
            "sequence": sequence,
            "evidence_binding_sha256": evidence_binding_sha256,
            "handoff_body_sha256": handoff_body_sha256,
            "capture_inventory_sha256": capture_inventory_sha256,
            "record": row,
            "research_only": True,
            "authority": dict(release._core._FALSE_RESEARCH_AUTHORITY),
        }
        lines.append(public.canonical_json_bytes(wrapper) + b"\n")
    raw = b"".join(lines)
    digest = hashlib.sha256(raw).hexdigest()
    path = root / f"mtvclc_{kind}_ledger_v2_{digest}.jsonl"
    path.write_bytes(raw)
    return path, {
        "filename": path.name,
        "sha256": digest,
        "size_bytes": len(raw),
        "rows": len(rows),
    }


def _write_release_bundle(
    root: Path,
    *,
    preregistration_path: Path,
    preregistration: Mapping[str, Any],
    handoff_path: Path,
    handoff_payload: Mapping[str, Any],
) -> dict[str, Path]:
    inventory = handoff_payload["capture_inventory"]
    evaluator_raw = evaluator.TOOL_PATH.read_bytes()
    report_binding = {
        "preregistration_body_sha256": preregistration[
            "preregistration_body_sha256"
        ],
        "preregistration_artifact_sha256": hashlib.sha256(
            preregistration_path.read_bytes()
        ).hexdigest(),
        "handoff_body_sha256": handoff_payload["handoff_body_sha256"],
        "handoff_artifact_sha256": hashlib.sha256(
            handoff_path.read_bytes()
        ).hexdigest(),
        "capture_inventory_sha256": handoff_payload["capture_inventory_sha256"],
        "manifest_sha256": inventory["manifest_sha256"],
        "manifest_head_sha256": inventory["manifest_head_sha256"],
        "market_source_id": inventory["market_source_id"],
        "screen_source_filename": preregistration["source_identities"][
            "screen_source"
        ]["filename"],
        "frozen_screen_sha256": preregistration["source_identities"][
            "screen_source"
        ]["sha256"],
        "frozen_screen_support_sha256": preregistration["source_identities"][
            "screen_support_source"
        ]["sha256"],
        "handoff_verifier_sha256": preregistration["source_identities"][
            "handoff_verifier_source"
        ]["sha256"],
        "evaluator_source_sha256": hashlib.sha256(evaluator_raw).hexdigest(),
        "cost_source_sha256": preregistration["source_identities"]["cost_capture"][
            "capture_json"
        ]["sha256"],
        "source_sha256_by_symbol": {
            symbol: hashlib.sha256(f"source:{symbol}".encode()).hexdigest()
            for symbol in handoff.SYMBOLS
        },
        "guard_identity_sha256": inventory["guard_identity_sha256"],
    }
    binding_sha = public.canonical_sha256(report_binding)
    reservations, outcomes, cells = _passing_ledgers(preregistration)
    reservation_path, reservation_identity = _write_ledger(
        root,
        kind="reservation",
        rows=reservations,
        evidence_binding_sha256=binding_sha,
        handoff_body_sha256=handoff_payload["handoff_body_sha256"],
        capture_inventory_sha256=handoff_payload["capture_inventory_sha256"],
    )
    outcome_path, outcome_identity = _write_ledger(
        root,
        kind="outcome",
        rows=outcomes,
        evidence_binding_sha256=binding_sha,
        handoff_body_sha256=handoff_payload["handoff_body_sha256"],
        capture_inventory_sha256=handoff_payload["capture_inventory_sha256"],
    )
    cell_path, cell_identity = _write_ledger(
        root,
        kind="cell",
        rows=cells,
        evidence_binding_sha256=binding_sha,
        handoff_body_sha256=handoff_payload["handoff_body_sha256"],
        capture_inventory_sha256=handoff_payload["capture_inventory_sha256"],
    )
    days = len({row["entry_day"] for row in outcomes})
    total = len(outcomes)
    report: dict[str, Any] = {
        "schema_version": release.POST_WINDOW_REPORT_SCHEMA,
        "evaluator_schema_version": release.POST_WINDOW_EVALUATOR_SCHEMA,
        "strategy_id": handoff.STRATEGY_ID,
        "strategy_version": handoff.STRATEGY_VERSION,
        "config_id": handoff.CONFIG_ID,
        "symbol_scope": list(handoff.SYMBOLS),
        "prospective_t0_utc_inclusive": preregistration["prospective_window"][
            "t0_utc_inclusive"
        ],
        "prospective_end_utc_exclusive": preregistration["prospective_window"][
            "end_utc_exclusive"
        ],
        "window_closed_before_capture_access": True,
        "capture_snapshot_required_stopped_and_read_only": True,
        "capture_snapshot_active_hour_journal_absent": True,
        "capture_snapshot_data_writer_lock_required": True,
        "capture_snapshot_data_writer_lock_proven_free": True,
        "capture_snapshot_reverified_after_materialization": True,
        "capture_snapshot_fence_rechecked_before_publication": True,
        "bootstrap_context_bars_retained": 240,
        "pre_t0_signal_bars_evaluated": 0,
        "pre_t0_quotes_evaluated": 0,
        "frozen_screen_semantics_executed": True,
        "frozen_screen_hash_is_exact_executed_bytes": True,
        "handoff_verifier_hash_is_exact_executed_bytes": True,
        "screen_result_valid": True,
        "screen_source_scope_ready": True,
        "screen_source_errors": [],
        "screen_all_cells_pass_fixed_screen": True,
        "global_success_gates": {
            "all_44_cells_pass": True,
            "source_scope_ready": True,
            "total_trades": total,
            "minimum_total_trades": 300,
            "total_independent_utc_days": days,
            "minimum_total_independent_utc_days": 60,
            "global_trade_gate_pass": True,
            "global_day_gate_pass": True,
            "all_preregistered_success_gates_pass": True,
        },
        "preregistered_success_criteria_observed": True,
        "evidence_binding": report_binding,
        "evidence_binding_sha256": binding_sha,
        "ledgers": {
            "reservation": reservation_identity,
            "outcome": outcome_identity,
            "cell": cell_identity,
        },
        "capture_materialization": {
            "schema_version": "fxstack.scalp.mtvclc_binary_materialization.v1",
            "bounded_memory": True,
            "disk_backed_per_symbol": True,
            "full_universe_loaded_in_memory": False,
            "manifest_entries": inventory["manifest_entries"],
            "projected_size_bytes": 1,
            "bar_rows_by_symbol": inventory["bar_rows_by_symbol"],
            "quote_rows_by_symbol": inventory["quote_rows_by_symbol"],
            "snapshot_files_verified_read_only": True,
            "snapshot_directories_verified_read_only": True,
            "scratch_retained": False,
        },
        "pbo_dsr_lineage_evaluated": False,
        "issuer_adapter_present": False,
        "evaluation_performed": True,
        "performance_statistics_computed": True,
        "research_only": True,
        "authority": dict(release._core._FALSE_RESEARCH_AUTHORITY),
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
    report["report_body_sha256"] = public.canonical_sha256(report)
    report_raw = public.canonical_json_bytes(report) + b"\n"
    report_path = root / (
        "mtvclc_post_window_report_v3_"
        f"{hashlib.sha256(report_raw).hexdigest()}.json"
    )
    report_path.write_bytes(report_raw)
    return {
        "report_path": report_path,
        "reservation_ledger_path": reservation_path,
        "outcome_ledger_path": outcome_path,
        "cell_ledger_path": cell_path,
    }


def test_sealed_handoff_selects_exact_v3_evaluator_inputs(tmp_path: Path) -> None:
    preregistration_path, preregistration, _inputs = _write_preregistration(tmp_path)
    handoff_path, handoff_payload = _write_handoff(
        tmp_path,
        preregistration_path=preregistration_path,
        preregistration=preregistration,
    )
    binding = handoff.load_preregistration(preregistration_path)
    loaded, artifact_sha = handoff.load_handoff_artifact(
        handoff_path,
        binding=binding,
    )
    sealed = evaluator._core.load_sealed_inputs(
        preregistration_path=preregistration_path,
        handoff_path=handoff_path,
        now_epoch=binding.end_epoch_exclusive,
    )

    assert loaded == handoff_payload
    assert artifact_sha == hashlib.sha256(handoff_path.read_bytes()).hexdigest()
    assert sealed.screen_source_filename == evaluator.SCREEN_PATH.name
    assert sealed.screen_source_sha256 == preregistration["source_identities"][
        "screen_source"
    ]["sha256"]
    assert sealed.handoff_source_sha256 == preregistration["source_identities"][
        "handoff_verifier_source"
    ]["sha256"]
    assert sealed.preregistration["attempt_accounting"] == {
        "prior_attempted_cells_lower_bound": 4_786,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_830,
    }


def test_real_collector_receipt_gap_chain_and_guard_are_handoff_valid(
    tmp_path: Path,
) -> None:
    preregistration_path, _preregistration, inputs = _write_preregistration(tmp_path)
    capture_root, _guard_report = _initialize_guard(
        tmp_path,
        preregistration_path=preregistration_path,
    )
    binding = handoff.collector.load_preregistration(
        preregistration_path,
        bridge_ea_repository_source=(
            sealer_helpers.seal.BRIDGE_EA_REPOSITORY_SOURCE_PATH
        ),
        bridge_ea_deployed_source=inputs[3],
        bridge_ea_deployed_ex4=inputs[4],
    )
    source = collector_helpers._source()
    now = binding.t0_epoch
    transport = collector_helpers.FakeBridgeTransport(
        states=[
            collector_helpers._state(source, now=now),
            collector_helpers._state(source, now=now),
        ],
        ticks=[
            collector_helpers._ticks(
                source,
                received_at=now,
                token_epoch=int(now),
                event_sequence=7,
            )
        ],
        bar_rounds=[
            collector_helpers._bar_round(
                source,
                count=handoff.collector.DEFAULT_BAR_LIMIT,
                observed_at=now,
            )
        ],
    )
    writer_lock = handoff.collector.ExclusiveDataWriterLock(capture_root).acquire()
    ledger = handoff.collector.ManifestLedger(
        capture_root,
        writer_lock=writer_lock,
    )
    receipt = handoff.collector.StartEdgeDurabilityReceipt(
        capture_root,
        binding=binding,
        ledger=ledger,
        writer_lock=writer_lock,
    )
    collector = handoff.collector.ProspectiveActivityCollector(
        client=handoff.collector.BridgeReadClient(
            base_url="http://127.0.0.1:58710",
            api_key=collector_helpers.API_KEY,
            timeout_secs=handoff.collector.DEFAULT_HTTP_TIMEOUT_SECS,
            transport=transport,
        ),
        ledger=ledger,
        binding=binding,
        receipt=receipt,
        policy=handoff.collector.CollectionPolicy(),
        clock=collector_helpers.ManualClock(now + 1.0),
    )
    collector.capture_cycle(include_bars=True)
    ledger.finalize_active()
    writer_lock.release()
    finalization = handoff.collector.finalize_capture_after_window(
        output_root=capture_root,
        preregistration_path=preregistration_path,
        bridge_ea_repository_source=(
            sealer_helpers.seal.BRIDGE_EA_REPOSITORY_SOURCE_PATH
        ),
        bridge_ea_deployed_source=inputs[3],
        bridge_ea_deployed_ex4=inputs[4],
        clock=lambda: binding.end_epoch_exclusive,
    )

    handoff_binding = handoff.load_preregistration(preregistration_path)
    receipt_sha, durable_at = handoff._validate_start_receipt(
        capture_root,
        binding=handoff_binding,
    )
    anchor = handoff._validate_gap_chain(capture_root, binding=handoff_binding)
    tail_proof = handoff._validate_tail_commitment(
        capture_root,
        binding=handoff_binding,
    )
    finalization_proof = handoff._validate_post_window_finalization(
        capture_root,
        binding=handoff_binding,
    )
    guard_sha = handoff._validate_final_state(
        capture_root,
        binding=handoff_binding,
    )

    assert handoff._is_sha256(receipt_sha)
    assert durable_at == now + 1.0
    assert anchor.count == 0
    assert anchor.event_count == 0
    assert anchor.source_id == source.source_id
    assert tail_proof["manifest_sequence"] == 1
    assert tail_proof["record_count"] == 6
    assert tail_proof["gap_chain_count"] == anchor.count
    assert tail_proof["gap_event_count"] == anchor.event_count
    assert tail_proof["gap_source_id"] == anchor.source_id
    assert tail_proof["pending_operation"] is False
    assert tail_proof["physical_journal_present"] is False
    assert tail_proof["cycle_reservation_sequence"] == 1
    assert tail_proof["last_cycle_reservation_sha256"] != handoff.ZERO_SHA256
    assert tail_proof["unresolved_cycle_reservation_sha256"] == handoff.ZERO_SHA256
    assert tail_proof["attempt_failure_sha256"] == handoff.ZERO_SHA256
    assert finalization["status"] == "finalized"
    assert finalization_proof[
        "post_window_finalization_receipt_sha256"
    ] == finalization["receipt_sha256"]
    assert handoff._is_sha256(guard_sha)
    expected_producer = handoff_binding.producer_receipt_fields()
    receipt_path = capture_root / handoff.START_EDGE_RECEIPT_FILENAME
    receipt_payload = json.loads(receipt_path.read_bytes())
    guard_path = capture_root / handoff.GUARD_IDENTITY_FILENAME
    guard_payload = json.loads(guard_path.read_bytes())
    assert all(
        receipt_payload[field] == expected
        and guard_payload[field] == expected
        for field, expected in expected_producer.items()
    )

    tail_path = capture_root / handoff.TAIL_COMMITMENT_FILENAME
    tail_raw = tail_path.read_bytes()
    tail_path.write_bytes(tail_raw + b"{}\n")
    with pytest.raises(handoff.HandoffRefusal, match="capture_tail_commitment"):
        handoff._validate_tail_commitment(capture_root, binding=handoff_binding)
    tail_path.write_bytes(tail_raw)

    finalization_path = capture_root / handoff.POST_WINDOW_FINALIZATION_FILENAME
    finalization_raw = finalization_path.read_bytes()
    forged_finalization = json.loads(finalization_raw)
    forged_finalization["network_requests_performed"] = True
    forged_finalization.pop("receipt_sha256", None)
    forged_finalization["receipt_sha256"] = handoff.canonical_sha256(
        forged_finalization
    )
    finalization_path.chmod(0o600)
    finalization_path.write_bytes(
        handoff.canonical_json_bytes(forged_finalization) + b"\n"
    )
    with pytest.raises(
        handoff.HandoffRefusal,
        match="post_window_finalization_receipt",
    ):
        handoff._validate_post_window_finalization(
            capture_root,
            binding=handoff_binding,
        )
    finalization_path.write_bytes(finalization_raw)
    finalization_path.chmod(0o400)

    forged_receipt = deepcopy(receipt_payload)
    forged_receipt["bridge_ea_deployed_ex4_identity"]["sha256"] = "f" * 64
    forged_receipt.pop("receipt_sha256", None)
    forged_receipt["receipt_sha256"] = handoff.canonical_sha256(forged_receipt)
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(handoff.canonical_json_bytes(forged_receipt) + b"\n")
    with pytest.raises(handoff.HandoffRefusal, match="start_edge_durable_receipt"):
        handoff._validate_start_receipt(capture_root, binding=handoff_binding)

    forged_guard = deepcopy(guard_payload)
    forged_guard["bridge_ea_deployed_ex4_identity"]["sha256"] = "f" * 64
    guard_path.chmod(0o600)
    guard_path.write_bytes(handoff.canonical_json_bytes(forged_guard) + b"\n")
    with pytest.raises(handoff.HandoffRefusal, match="capture_guard_identity_contract"):
        handoff._validate_guard_identity(guard_path, binding=handoff_binding)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("collector_source_sha256", "f" * 64),
        ("gap_event_count", 1),
        ("first_cycle_durable_at_epoch", 0.0),
        ("post_window_finalization_receipt_artifact_sha256", "not-a-hash"),
        ("post_window_finalization_receipt_sha256", "not-a-hash"),
        ("post_window_finalized_after_end_observed_at_epoch", 0.0),
        ("orphan_chunk_files", 1),
        ("upstream_producer_software_body_sha256", "f" * 64),
        (
            "bridge_ea_deployed_ex4_identity",
            {"filename": "BridgeEA.ex4", "sha256": "f" * 64, "size_bytes": 1},
        ),
    ),
)
def test_rehashed_handoff_inventory_forgery_refuses(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    preregistration_path, preregistration, _inputs = _write_preregistration(tmp_path)
    _handoff_path, payload = _write_handoff(
        tmp_path,
        preregistration_path=preregistration_path,
        preregistration=preregistration,
    )
    forged = deepcopy(payload)
    forged["capture_inventory"][field] = value
    path = _rewrite_handoff(tmp_path, forged)

    with pytest.raises(handoff.HandoffRefusal, match="capture_handoff_contract"):
        handoff.load_handoff_artifact(
            path,
            binding=handoff.load_preregistration(preregistration_path),
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "pending_operation",
        "manifest_sequence",
        "gap_source",
        "committed_state_hash",
        "cycle_reservation_sequence",
        "last_cycle_reservation",
        "unresolved_cycle_reservation",
        "attempt_failure",
        "missing_field",
    ),
)
def test_rehashed_tail_commitment_proof_forgery_refuses(
    tmp_path: Path,
    mutation: str,
) -> None:
    preregistration_path, preregistration, _inputs = _write_preregistration(tmp_path)
    _handoff_path, payload = _write_handoff(
        tmp_path,
        preregistration_path=preregistration_path,
        preregistration=preregistration,
    )
    forged = deepcopy(payload)
    proof = forged["capture_inventory"]["capture_tail_commitment_proof"]
    if mutation == "pending_operation":
        proof["pending_operation"] = True
    elif mutation == "manifest_sequence":
        proof["manifest_sequence"] += 1
    elif mutation == "gap_source":
        proof["gap_source_id"] = "f" * 64
    elif mutation == "committed_state_hash":
        proof["committed_state_sha256"] = "f" * 64
    elif mutation == "cycle_reservation_sequence":
        proof["cycle_reservation_sequence"] = 0
    elif mutation == "last_cycle_reservation":
        proof["last_cycle_reservation_sha256"] = handoff.ZERO_SHA256
    elif mutation == "unresolved_cycle_reservation":
        proof["unresolved_cycle_reservation_sha256"] = "f" * 64
    elif mutation == "attempt_failure":
        proof["attempt_failure_sha256"] = "f" * 64
    elif mutation == "missing_field":
        proof.pop("tail_entry_sha256")
    else:  # pragma: no cover
        raise AssertionError(mutation)
    path = _rewrite_handoff(tmp_path, forged)

    with pytest.raises(handoff.HandoffRefusal, match="capture_handoff_contract"):
        handoff.load_handoff_artifact(
            path,
            binding=handoff.load_preregistration(preregistration_path),
        )


def test_public_verifier_is_pinned_to_corrected_family() -> None:
    assert public.EXPECTED_ATTEMPT_ACCOUNTING == {
        "prior_attempted_cells_lower_bound": 4_786,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_830,
    }
    assert public.WILSON_FAMILY_ATTEMPTED_CELLS == 4_830
    assert public.WILSON_ALPHA_ALLOCATION == "one_sided_0.05_over_4830"
    assert release.POST_WINDOW_REPORT_SCHEMA.endswith(".v3")
    assert public.EXECUTED_DEPENDENCY_IDENTITIES[
        "public_verifier_v1_support_source"
    ] == {
        "filename": public._V1_PATH.name,
        "sha256": hashlib.sha256(public._V1_PATH.read_bytes()).hexdigest(),
        "size_bytes": public._V1_PATH.stat().st_size,
    }
    assert not any(
        name.startswith("_fxstack_mtvclc_v2_exact") for name in sys.modules
    )


def test_downstream_exact_loader_ignores_timestamp_size_valid_forged_pyc(
    tmp_path: Path,
) -> None:
    source = sealer_helpers._forged_timestamp_pyc(tmp_path)
    conventional_name = "gap_v3_downstream_forged_pyc_control"
    conventional = _load(conventional_name, source)
    assert conventional.MARKER == "timestamp-size-valid-pyc"

    exact_name = "_gap_v3_downstream_exact_source_pyc_regression"
    sys.modules.pop(exact_name, None)
    image = handoff._read_exact_source(source, reason="test_source_invalid")
    exact = handoff._execute_exact_source(image, module_name=exact_name)
    assert exact.MARKER == "descriptor-source"
    assert exact_name not in sys.modules
    sys.modules.pop(conventional_name, None)


def test_downstream_executed_dependency_identities_are_exposed() -> None:
    handoff_identities = handoff.executed_source_identities()
    evaluator_identities = evaluator.executed_source_identities()
    release_identities = release.executed_source_identities()

    assert {
        "collector_source",
        "sealer_source",
        "handoff_verifier_source",
        "legacy_handoff_support_source",
    } <= set(handoff_identities)
    assert {
        "evaluator_source",
        "legacy_evaluator_support_source",
        "screen_support_source",
    } <= set(evaluator_identities)
    assert {
        "release_source",
        "legacy_release_support_source",
        "public_verifier_source",
        "public_verifier_v1_support_source",
    } <= set(release_identities)
    handoff._assert_executed_sources_unchanged()
    evaluator._assert_executed_sources_unchanged()
    release._assert_executed_sources_unchanged()
    assert sys.modules["fxstack.runtime.mtvclc_validation_evidence_v2"] is public
    assert sys.modules[
        "fxstack.scalp.screen_mt4_tick_volume_close_location_continuation"
    ] is base_screen
    assert not any(
        name.startswith(
            (
                "_fxstack_mtvclc_gap_v3_handoff",
                "_fxstack_mtvclc_gap_v3_evaluator",
                "_fxstack_mtvclc_gap_v3_release",
                "_fxstack_mtvclc_v2_exact",
                "_fxstack_gap_v3_exact",
                "_fxstack_gap_v3_sealed_collector",
                "_fxstack_mtvclc_gap_sealer_support",
            )
        )
        for name in sys.modules
    )


def test_release_public_validation_recomputes_complete_v3_ledgers(
    tmp_path: Path,
) -> None:
    preregistration_path, preregistration, inputs = _write_preregistration(tmp_path)
    capture_root, _guard_report = _initialize_guard(
        tmp_path,
        preregistration_path=preregistration_path,
    )
    guard_path = capture_root / handoff.GUARD_IDENTITY_FILENAME
    guard_artifact_sha256 = hashlib.sha256(guard_path.read_bytes()).hexdigest()
    handoff_path, handoff_payload = _write_handoff(
        tmp_path,
        preregistration_path=preregistration_path,
        preregistration=preregistration,
        guard_identity_sha256=guard_artifact_sha256,
    )
    artifacts = _write_release_bundle(
        tmp_path,
        preregistration_path=preregistration_path,
        preregistration=preregistration,
        handoff_path=handoff_path,
        handoff_payload=handoff_payload,
    )
    binding = handoff.load_preregistration(preregistration_path)
    assert handoff._validate_guard_identity(guard_path, binding=binding) == (
        guard_artifact_sha256
    )
    selected = evaluator._core.load_sealed_inputs(
        preregistration_path=preregistration_path,
        handoff_path=handoff_path,
        now_epoch=binding.end_epoch_exclusive,
    )
    assert selected.screen_source_filename == evaluator.SCREEN_PATH.name
    paths = {
        "preregistration_path": preregistration_path,
        "handoff_path": handoff_path,
        **artifacts,
        "cost_capture_json_path": inputs[0],
        "cost_capture_npz_path": inputs[1],
        "fee_attestation_path": inputs[2],
    }
    validated = release.validate_public_inputs(
        **paths,
        now_epoch=binding.end_epoch_exclusive,
    )

    assert validated.evidence["attempt_accounting"] == {
        "prior_attempted_cells_lower_bound": 4_786,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_830,
    }
    assert validated.evidence["overall"]["all_44_cells_pass"] is True
    expected_ledger_authentication = {
        "reservation_rows": 2_640,
        "outcome_rows": 2_640,
        "cell_rows": 44,
        "duplicate_reservation_keys": 0,
        "duplicate_outcome_keys": 0,
        "missing_outcomes": 0,
        "orphan_outcomes": 0,
        "inconsistent_pairs": 0,
        "empty_ledgers_rejected": True,
        "cell_summaries_recomputed_exclusively_from_ledgers": True,
        "screen_result_bundle_validated": True,
    }
    assert expected_ledger_authentication.items() <= validated.evidence[
        "ledger_authentication"
    ].items()
    assert public.mtvclc_evidence_error(validated.evidence) == ""

    request_path, request = release.prepare_issuance_request(
        public_input_paths=paths,
        generation_id="offline-gap-v3-integration",
        validity_secs=3_600.0,
        output_path=tmp_path / "issuance-request-v2.json",
        now_epoch=binding.end_epoch_exclusive,
    )
    assert request_path.is_file()
    assert request["schema_version"] == release.ISSUANCE_REQUEST_SCHEMA
    assert request["certificate_claims"]["authority"] == public.NO_RUNTIME_AUTHORITY


def test_release_rejects_fully_rehashed_fabricated_cell_summary(
    tmp_path: Path,
) -> None:
    preregistration_path, preregistration, inputs = _write_preregistration(tmp_path)
    handoff_path, handoff_payload = _write_handoff(
        tmp_path,
        preregistration_path=preregistration_path,
        preregistration=preregistration,
    )
    artifacts = _write_release_bundle(
        tmp_path,
        preregistration_path=preregistration_path,
        preregistration=preregistration,
        handoff_path=handoff_path,
        handoff_payload=handoff_payload,
    )
    cell_wrappers = [
        json.loads(line)
        for line in artifacts["cell_ledger_path"].read_bytes().splitlines()
    ]
    cell_wrappers[0]["record"]["wins"] -= 1
    cell_raw = b"".join(
        public.canonical_json_bytes(row) + b"\n" for row in cell_wrappers
    )
    cell_sha = hashlib.sha256(cell_raw).hexdigest()
    cell_path = tmp_path / f"mtvclc_cell_ledger_v2_{cell_sha}.jsonl"
    cell_path.write_bytes(cell_raw)

    report = json.loads(artifacts["report_path"].read_bytes())
    report["ledgers"]["cell"] = {
        "filename": cell_path.name,
        "sha256": cell_sha,
        "size_bytes": len(cell_raw),
        "rows": len(cell_wrappers),
    }
    report.pop("report_body_sha256", None)
    report["report_body_sha256"] = public.canonical_sha256(report)
    report_raw = public.canonical_json_bytes(report) + b"\n"
    report_path = tmp_path / (
        "mtvclc_post_window_report_v3_"
        f"{hashlib.sha256(report_raw).hexdigest()}.json"
    )
    report_path.write_bytes(report_raw)
    binding = handoff.load_preregistration(preregistration_path)

    with pytest.raises(
        release.MTVCLCReleaseRefusal,
        match="cell_ledger_not_exclusively_recomputed",
    ):
        release.validate_public_inputs(
            preregistration_path=preregistration_path,
            handoff_path=handoff_path,
            report_path=report_path,
            reservation_ledger_path=artifacts["reservation_ledger_path"],
            outcome_ledger_path=artifacts["outcome_ledger_path"],
            cell_ledger_path=cell_path,
            cost_capture_json_path=inputs[0],
            cost_capture_npz_path=inputs[1],
            fee_attestation_path=inputs[2],
            now_epoch=binding.end_epoch_exclusive,
        )
