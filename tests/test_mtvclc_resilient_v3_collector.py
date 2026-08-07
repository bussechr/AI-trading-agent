from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tools import capture_ig_mt4_m1_activity_resilient_v3 as capture

ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = ROOT / "tests"
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))
preserved_cases = importlib.import_module("test_capture_ig_mt4_m1_activity")

NOW = float(int(preserved_cases.NOW // 60) * 60)
TARGET_SYMBOL = "USDCAD"
GAP_INDEX = 300
TEST_EX4_BYTES = b"test-only compiled BridgeEA identity"


def _file_identity(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _bytes_identity(filename: str, raw: bytes) -> dict[str, Any]:
    return {
        "filename": filename,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _producer_contract(
    repository: Mapping[str, Any],
    deployed: Mapping[str, Any],
    deployed_ex4: Mapping[str, Any],
) -> dict[str, Any]:
    body = {
        "schema_version": capture.UPSTREAM_PRODUCER_SOFTWARE_SCHEMA_VERSION,
        "repository_source": dict(repository),
        "deployed_source": dict(deployed),
        "deployed_ex4": dict(deployed_ex4),
        "repository_and_deployed_source_bytes_identical_at_seal_time": True,
        "all_three_identities_rechecked_before_publication": True,
        "collector_cycle_revalidation_claimed": True,
        "runtime_or_broker_authority_derived_from_identity": False,
    }
    return {
        **body,
        "producer_software_body_sha256": capture.canonical_sha256(body),
    }


def _producer_paths(root: Path) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    repository = capture.BRIDGE_EA_REPOSITORY_SOURCE_PATH
    deployed = root / "BridgeEA.mq4"
    deployed_ex4 = root / "BridgeEA.ex4"
    if not deployed.exists():
        deployed.write_bytes(repository.read_bytes())
    if not deployed_ex4.exists():
        deployed_ex4.write_bytes(TEST_EX4_BYTES)
    return repository, deployed, deployed_ex4


def _producer_binding(
    root: Path,
) -> tuple[capture.UpstreamProducerSoftware, capture.ProducerSoftwareMonitor]:
    repository, deployed, deployed_ex4 = _producer_paths(root)
    identities = {
        "production_engine_component:MQL4/Experts/BridgeEA.mq4": _file_identity(
            repository
        ),
        "bridge_ea_deployed_source": _file_identity(deployed),
        "bridge_ea_deployed_ex4": _file_identity(deployed_ex4),
    }
    contract = capture.UpstreamProducerSoftware.parse(
        _producer_contract(
            identities["production_engine_component:MQL4/Experts/BridgeEA.mq4"],
            identities["bridge_ea_deployed_source"],
            identities["bridge_ea_deployed_ex4"],
        ),
        source_identities=identities,
    )
    monitor = capture.ProducerSoftwareMonitor.bind(
        contract,
        repository_source_path=repository,
        deployed_source_path=deployed,
        deployed_ex4_path=deployed_ex4,
    )
    return contract, monitor


def _preregistration_payload() -> dict[str, Any]:
    original = preserved_cases._preregistration_payload()
    body = {
        key: copy.deepcopy(value)
        for key, value in original.items()
        if key != "preregistration_body_sha256"
    }
    body["sealed_at_utc"] = datetime.fromtimestamp(NOW - 900, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    body["prospective_window"]["t0_utc_inclusive"] = datetime.fromtimestamp(
        NOW, tz=UTC
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    body["prospective_window"]["end_utc_exclusive"] = datetime.fromtimestamp(
        NOW + capture.PROSPECTIVE_WINDOW_DAYS * 86_400, tz=UTC
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    body["capture_integrity_contract"] = capture.expected_capture_integrity_contract()
    body["declaration_revision"] = capture.DECLARATION_REVISION
    body["capture_profile_id"] = capture.CAPTURE_PROFILE_ID
    body["attempt_accounting"] = dict(capture.ATTEMPT_ACCOUNTING)
    body["abandoned_preregistrations"] = (
        capture.expected_abandoned_preregistrations()
    )
    body["replacement_lineage"] = dict(capture.REPLACEMENT_LINEAGE)
    body["preservation_filename_contract"] = (
        capture.expected_preservation_filename_contract()
    )
    body["scope"]["sides"] = ["BUY", "SELL"]
    for cell in body["scope"]["cell_order"]:
        cell["config_id"] = capture.CONFIG_ID
    attempt_manifest = {
        **capture.ATTEMPT_ACCOUNTING,
        "descriptive_df99_bonferroni_abs_t_threshold": (
            capture.BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
        ),
        "win_probability_familywise_attempted_cells": (
            capture.CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND
        ),
        "win_probability_alpha_allocation": "one_sided_0.05_over_4830",
        "cell_summary_source": (
            "exclusive_recomputation_from_complete_reservation_and_outcome_ledgers"
        ),
        "empty_missing_duplicate_or_inconsistent_ledgers_refuse": True,
    }
    body["strategy"].update(
        {
            "strategy_id": capture.STRATEGY_ID,
            "strategy_version": capture.STRATEGY_VERSION,
            "config_id": capture.CONFIG_ID,
            "config_sha256": "a" * 64,
            "attempt_manifest": attempt_manifest,
            "attempt_manifest_sha256": capture.canonical_sha256(attempt_manifest),
        }
    )
    body["fixed_success_gates"] = {
        "all_44_cells_must_pass": True,
        "minimum_trades_per_cell": 30,
        "minimum_independent_utc_days_per_cell": 10,
        "cell_win_probability_interval": (
            "one_sided_wilson_family_adjusted_over_4830_attempted_cells"
        ),
        "cell_win_probability_family_confidence": 0.95,
        "descriptive_df99_bonferroni_abs_t_threshold": (
            capture.BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
        ),
    }
    body["prospective_window"].update(
        {
            "early_success_forbidden": True,
            "no_optional_extension_or_restart_after_failure": True,
        }
    )
    identities = body["source_identities"]
    identities["collector_source"] = _file_identity(capture.TOOL_PATH)
    identities["collector_support_source"] = _file_identity(capture.SUPPORT_PATH)
    identities["collector_base_source"] = _file_identity(capture.BASE_SUPPORT_PATH)
    repository_identity = _file_identity(capture.BRIDGE_EA_REPOSITORY_SOURCE_PATH)
    deployed_ex4_identity = _bytes_identity("BridgeEA.ex4", TEST_EX4_BYTES)
    identities[
        "production_engine_component:MQL4/Experts/BridgeEA.mq4"
    ] = repository_identity
    identities["bridge_ea_deployed_source"] = dict(repository_identity)
    identities["bridge_ea_deployed_ex4"] = deployed_ex4_identity
    body["upstream_producer_software"] = _producer_contract(
        repository_identity,
        repository_identity,
        deployed_ex4_identity,
    )
    return {**body, "preregistration_body_sha256": capture.canonical_sha256(body)}


def _write_preregistration(
    path: Path,
    payload: Mapping[str, Any] | None = None,
) -> Path:
    payload = dict(payload or _preregistration_payload())
    path.write_text(
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _rehash_preregistration(payload: dict[str, Any]) -> None:
    body = dict(payload)
    body.pop("preregistration_body_sha256", None)
    payload["preregistration_body_sha256"] = capture.canonical_sha256(body)


def _binding(output: Path) -> capture.ProspectiveBinding:
    end = NOW + capture.PROSPECTIVE_WINDOW_DAYS * 86_400
    producer, monitor = _producer_binding(output / ".test-producer-inputs")
    return capture.ProspectiveBinding(
        preregistration_body_sha256="1" * 64,
        preregistration_artifact_sha256="2" * 64,
        t0_utc=datetime.fromtimestamp(NOW, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_utc_exclusive=datetime.fromtimestamp(end, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        t0_epoch=NOW,
        end_epoch_exclusive=end,
        upstream_producer_software=producer,
        producer_software_monitor=monitor,
    )


def _bar_round(source: Any, *, now: float = NOW) -> dict[str, Any]:
    return preserved_cases._bar_round(
        source,
        count=capture.DEFAULT_BAR_LIMIT,
        observed_at=now,
    )


def _transport(
    source: Any,
    *,
    now: float,
    bars: Mapping[str, Mapping[str, Any]],
    event_sequence: int,
    final_state_now: float | None = None,
) -> preserved_cases.FakeBridgeTransport:
    return preserved_cases.FakeBridgeTransport(
        states=[
            preserved_cases._state(source, now=now),
            preserved_cases._state(
                source,
                now=now if final_state_now is None else final_state_now,
            ),
        ],
        ticks=[
            preserved_cases._ticks(
                source,
                received_at=now,
                token_epoch=int(now),
                event_sequence=event_sequence,
            )
        ],
        bar_rounds=[bars],
    )


def _collector(
    output: Path,
    transport: preserved_cases.FakeBridgeTransport,
    *,
    clock: preserved_cases.ManualClock,
) -> tuple[capture.ProspectiveActivityCollector, capture.ManifestLedger]:
    writer_lock = capture.ExclusiveDataWriterLock(output).acquire()
    ledger = capture.ManifestLedger(output, writer_lock=writer_lock)
    binding = _binding(output)
    receipt = capture.StartEdgeDurabilityReceipt(
        output,
        binding=binding,
        ledger=ledger,
        writer_lock=writer_lock,
    )
    collector = capture.ProspectiveActivityCollector(
        client=capture.BridgeReadClient(
            base_url="http://127.0.0.1:58710",
            api_key=preserved_cases.API_KEY,
            timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
            transport=transport,
        ),
        ledger=ledger,
        binding=binding,
        receipt=receipt,
        policy=capture.CollectionPolicy(),
        clock=clock,
    )
    return collector, ledger


def _collector_with_active_second_cycle(
    output: Path,
) -> tuple[capture.ProspectiveActivityCollector, capture.ManifestLedger]:
    source = preserved_cases._source()
    collector, ledger = _collector(
        output,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)
    _configure_next_cycle(collector)
    collector.capture_cycle(include_bars=True)
    assert ledger.journal_path.is_file()
    return collector, ledger


def _configure_next_cycle(
    collector: capture.ProspectiveActivityCollector,
    *,
    seconds: int = 180,
    event_sequence: int = 8,
) -> None:
    source = preserved_cases._source()
    collector.client = capture.BridgeReadClient(
        base_url="http://127.0.0.1:58710",
        api_key=preserved_cases.API_KEY,
        timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
        transport=_transport(
            source,
            now=NOW + seconds,
            bars=_bar_round(source),
            event_sequence=event_sequence,
        ),
    )
    collector.clock = preserved_cases.ManualClock(NOW + seconds)


def _chunk(ledger: capture.ManifestLedger, index: int) -> dict[str, Any]:
    entry = ledger.entries[index]
    return json.loads((ledger.root / entry["chunk_path"]).read_text(encoding="utf-8"))


def _shift_rows(rows: list[dict[str, Any]], *, start: int, minutes: int) -> None:
    for row in rows[start:]:
        parsed = datetime.fromisoformat(str(row["time"]))
        row["time"] = (parsed + timedelta(minutes=minutes)).isoformat()


def _gap_rounds(source: Any) -> tuple[dict[str, Any], dict[str, Any], int]:
    original = _bar_round(source)
    first = copy.deepcopy(original)
    _shift_rows(first[TARGET_SYMBOL]["bars"], start=GAP_INDEX, minutes=1)
    missing_epoch = int(
        datetime.fromisoformat(
            str(original[TARGET_SYMBOL]["bars"][GAP_INDEX]["time"])
        ).timestamp()
    )
    later = copy.deepcopy(original)
    for symbol in capture.SYMBOLS:
        if symbol != TARGET_SYMBOL:
            _shift_rows(later[symbol]["bars"], start=0, minutes=1)
    target_rows = copy.deepcopy(original[TARGET_SYMBOL]["bars"][2:])
    covered_tail = copy.deepcopy(first[TARGET_SYMBOL]["bars"][-1])
    fresh_tail = copy.deepcopy(covered_tail)
    fresh_tail["time"] = (
        datetime.fromisoformat(str(fresh_tail["time"])) + timedelta(minutes=1)
    ).isoformat()
    fresh_tail["volume"] += 1
    target_rows.extend((covered_tail, fresh_tail))
    later[TARGET_SYMBOL]["bars"] = target_rows
    return first, later, missing_epoch


def test_contract_binds_collector_wrapper_base_and_exact_scope(tmp_path: Path) -> None:
    path = _write_preregistration(tmp_path / "candidate.json")
    producer_paths = _producer_paths(tmp_path / "deployed")

    binding = capture.load_preregistration(
        path,
        bridge_ea_repository_source=producer_paths[0],
        bridge_ea_deployed_source=producer_paths[1],
        bridge_ea_deployed_ex4=producer_paths[2],
    )
    contract = capture.expected_capture_integrity_contract()

    assert binding.t0_epoch == NOW
    assert len(capture.SYMBOLS) == 22
    assert "XRPUSD" not in capture.SYMBOLS
    assert contract["collector_wrapper_source_sha256"] == capture.SUPPORT_SHA256
    assert contract["collector_base_source_sha256"] == capture.BASE_SUPPORT_SHA256
    assert contract["independently_mutable_late_gap_sidecar_forbidden"] is True
    assert contract[
        "late_gap_main_chain_deletion_truncation_or_rollback_relative_to_"
        "tail_commitment_refuses"
    ] is True
    assert contract[
        "main_chain_rollback_or_deletion_is_detected_relative_to_tail_commitment"
    ] is True
    assert contract["coordinated_tail_commitment_and_data_rollback_detection_claimed"] is False
    assert contract["manifest_append_never_rereads_or_replaces_complete_manifest"] is True
    assert contract["first_cycle_durable_commit_must_complete_by_t0_plus_30_seconds"] is True
    assert binding.producer_software_monitor is not None
    assert (
        binding.upstream_producer_software.body_sha256
        == _preregistration_payload()["upstream_producer_software"][
            "producer_software_body_sha256"
        ]
    )


@pytest.mark.parametrize(
    "field",
    (
        "declaration_revision",
        "capture_profile_id",
        "attempt_accounting",
        "replacement_lineage",
        "upstream_producer_software",
    ),
)
def test_null_or_old_successor_envelope_is_rejected(
    tmp_path: Path,
    field: str,
) -> None:
    payload = _preregistration_payload()
    payload[field] = None
    _rehash_preregistration(payload)
    path = _write_preregistration(tmp_path / f"null-{field}.json", payload)

    with pytest.raises(capture.CollectionRefusal, match="contract_invalid|producer"):
        capture.load_preregistration(path)


def test_forged_deployed_ex4_identity_is_rejected_against_explicit_path(
    tmp_path: Path,
) -> None:
    payload = _preregistration_payload()
    forged = _bytes_identity("BridgeEA.ex4", b"forged compiled identity")
    payload["source_identities"]["bridge_ea_deployed_ex4"] = forged
    producer = payload["upstream_producer_software"]
    producer["deployed_ex4"] = forged
    producer_body = dict(producer)
    producer_body.pop("producer_software_body_sha256", None)
    producer["producer_software_body_sha256"] = capture.canonical_sha256(
        producer_body
    )
    _rehash_preregistration(payload)
    path = _write_preregistration(tmp_path / "forged-ex4.json", payload)
    producer_paths = _producer_paths(tmp_path / "deployed")

    with pytest.raises(capture.CollectionRefusal, match="path_or_identity_invalid"):
        capture.load_preregistration(
            path,
            bridge_ea_repository_source=producer_paths[0],
            bridge_ea_deployed_source=producer_paths[1],
            bridge_ea_deployed_ex4=producer_paths[2],
        )


def test_fixed_4830_family_and_gates_cannot_be_rehashed_away(tmp_path: Path) -> None:
    payload = _preregistration_payload()
    payload["attempt_accounting"]["cumulative_attempted_cells_lower_bound"] = 44
    payload["fixed_success_gates"]["cell_win_probability_interval"] = (
        "one_sided_wilson_family_adjusted_over_44_cells"
    )
    _rehash_preregistration(payload)
    path = _write_preregistration(tmp_path / "forged-family.json", payload)

    with pytest.raises(capture.CollectionRefusal, match="contract_invalid"):
        capture.load_preregistration(path)


def test_preregistration_symlink_is_rejected_before_resolve(tmp_path: Path) -> None:
    target = _write_preregistration(tmp_path / "target.json")
    link = tmp_path / "candidate-link.json"
    try:
        os.symlink(target, link)
    except OSError as exc:  # pragma: no cover - host policy may forbid symlinks
        pytest.skip(f"host forbids symlink creation: {exc}")

    with pytest.raises(capture.CollectionRefusal, match="symlink_forbidden"):
        capture.load_preregistration(link)


def test_first_full_scope_cycle_is_main_fsynced_then_receipted(tmp_path: Path) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )

    collector.capture_cycle(include_bars=True)

    assert len(ledger.entries) == 1
    assert (tmp_path / capture.START_EDGE_RECEIPT_FILENAME).is_file()
    chunk = _chunk(ledger, 0)
    assert {row["symbol"] for row in chunk["bars"]} == set(capture.SYMBOLS)
    assert chunk["gap_chain_count"] == 0
    assert chunk["gap_event_count"] == 0
    assert chunk["gap_source_id"] == source.source_id
    assert ledger.entries[0]["gap_source_id"] == source.source_id
    receipt_value = json.loads(
        (tmp_path / capture.START_EDGE_RECEIPT_FILENAME).read_text(encoding="utf-8")
    )
    assert receipt_value["upstream_producer_software_body_sha256"] == (
        collector.binding.upstream_producer_software.body_sha256
    )
    ledger.writer_lock.release()
    before = {
        path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    proof = capture.validate_start_edge_durability_receipt(
        tmp_path,
        binding=collector.binding,
        ledger=ledger,
    )
    after = {
        path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert proof["status"] == "valid"
    assert proof["first_cycle_completed_at_epoch"] <= proof[
        "first_cycle_durable_at_epoch"
    ]
    assert before == after

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    receipt = capture.StartEdgeDurabilityReceipt(
        tmp_path,
        binding=_binding(tmp_path),
        ledger=recovered,
        writer_lock=lock,
    )
    assert receipt.committed is True
    assert recovered.gap_anchor.source_id == source.source_id
    lock.release()


def test_slow_complete_22_get_cycle_refuses_before_any_durable_capture(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    clock = preserved_cases.ManualClock(NOW)
    inner = _transport(
        source,
        now=NOW,
        bars=_bar_round(source),
        event_sequence=7,
        final_state_now=NOW + 31,
    )

    class AdvancingTransport:
        def __call__(self, request: Any, timeout_secs: float, maximum_bytes: int):  # type: ignore[no-untyped-def]
            result = inner(request, timeout_secs, maximum_bytes)
            if "/v2/market/bars" in request.full_url:
                clock.now += 1.41
            return result

    collector, ledger = _collector(
        tmp_path,
        AdvancingTransport(),  # type: ignore[arg-type]
        clock=clock,
    )

    with pytest.raises(capture.CollectionRefusal, match="first_cycle_work_late"):
        collector.capture_cycle(include_bars=True)
    assert not ledger.entries
    assert not ledger.manifest_path.exists()
    assert not (tmp_path / capture.START_EDGE_RECEIPT_FILENAME).exists()
    ledger.writer_lock.release()


def test_producer_ex4_drift_refuses_before_first_network_get(tmp_path: Path) -> None:
    source = preserved_cases._source()
    transport = _transport(
        source,
        now=NOW,
        bars=_bar_round(source),
        event_sequence=7,
    )
    collector, ledger = _collector(
        tmp_path,
        transport,
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    monitor = collector.binding.producer_software_monitor
    assert monitor is not None
    monitor.deployed_ex4.path.write_bytes(b"changed after binding")

    with pytest.raises(capture.CollectionRefusal, match="producer_software_drift"):
        collector.capture_cycle(include_bars=True)
    assert transport.calls == []
    assert not ledger.entries
    ledger.writer_lock.release()


def test_pre_t0_authenticated_quote_receipt_is_never_persisted(tmp_path: Path) -> None:
    source = preserved_cases._source()
    transport = preserved_cases.FakeBridgeTransport(
        states=[
            preserved_cases._state(source, now=NOW),
            preserved_cases._state(source, now=NOW),
        ],
        ticks=[
            preserved_cases._ticks(
                source,
                received_at=NOW - 1,
                token_epoch=int(NOW - 1),
                event_sequence=7,
            )
        ],
        bar_rounds=[_bar_round(source)],
    )
    collector, ledger = _collector(
        tmp_path,
        transport,
        clock=preserved_cases.ManualClock(NOW + 1),
    )

    with pytest.raises(capture.CollectionRefusal, match="observation_before_t0"):
        collector.capture_cycle(include_bars=True)
    assert not ledger.entries
    assert not ledger.manifest_path.exists()
    assert not (tmp_path / capture.START_EDGE_RECEIPT_FILENAME).exists()
    ledger.writer_lock.release()


def test_401_rows_under_declared_limit_400_refuses_before_journal(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    oversized = preserved_cases._bar_round(
        source,
        count=capture.DEFAULT_BAR_LIMIT + 1,
        observed_at=NOW,
    )
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=oversized, event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )

    with pytest.raises(capture.CollectionRefusal, match="exceeds_sealed_limit"):
        collector.capture_cycle(include_bars=True)
    assert not ledger.entries
    assert not ledger.journal_path.exists()
    ledger.writer_lock.release()


def test_regressed_post_chain_clock_cannot_commit_start_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = preserved_cases._source()
    clock = preserved_cases.ManualClock(NOW + 1)
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=clock,
    )
    real_append = ledger.append_cycle

    def append_then_regress(cycle: Mapping[str, Any]):  # type: ignore[no-untyped-def]
        result = real_append(cycle)
        clock.now = NOW - 1
        return result

    monkeypatch.setattr(ledger, "append_cycle", append_then_regress)

    with pytest.raises(capture.CollectionRefusal, match="durable_clock_regressed"):
        collector.capture_cycle(include_bars=True)
    assert len(ledger.entries) == 1
    assert not (tmp_path / capture.START_EDGE_RECEIPT_FILENAME).exists()
    ledger.writer_lock.release()


def test_late_post_fsync_clock_refuses_and_missing_receipt_blocks_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = preserved_cases._source()
    clock = preserved_cases.ManualClock(NOW + 1)
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=clock,
    )
    real_append = ledger.append_cycle

    def append_then_cross_deadline(cycle: Mapping[str, Any]):  # type: ignore[no-untyped-def]
        result = real_append(cycle)
        clock.now = NOW + 30.001
        return result

    monkeypatch.setattr(ledger, "append_cycle", append_then_cross_deadline)

    with pytest.raises(capture.CollectionRefusal, match="first_durable_commit_late"):
        collector.capture_cycle(include_bars=True)
    assert len(ledger.entries) == 1
    assert ledger.manifest_path.is_file()
    assert not (tmp_path / capture.START_EDGE_RECEIPT_FILENAME).exists()
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    with pytest.raises(capture.CollectionRefusal, match="durable_receipt_missing"):
        capture.StartEdgeDurabilityReceipt(
            tmp_path,
            binding=_binding(tmp_path),
            ledger=recovered,
            writer_lock=lock,
        )
    lock.release()


def test_late_gap_is_embedded_and_source_bound_even_on_no_event_cycles(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    first_round, later_round, missing_epoch = _gap_rounds(source)
    first, first_ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=first_round, event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    first.capture_cycle(include_bars=True)
    first_ledger.writer_lock.release()

    resumed, ledger = _collector(
        tmp_path,
        _transport(
            source,
            now=NOW + 180,
            bars=later_round,
            event_sequence=8,
        ),
        clock=preserved_cases.ManualClock(NOW + 180),
    )
    resumed.capture_cycle(include_bars=True)
    ledger.finalize_active()

    second = _chunk(ledger, 1)
    assert second["gap_chain_count"] == 1
    assert second["gap_event_count"] == 1
    assert second["gap_source_id"] == source.source_id
    assert second["late_gap_records"][0]["events"][0]["minute_epoch"] == missing_epoch
    assert ledger.entries[1]["gap_tail_sha256"] == second["gap_tail_sha256"]
    assert not any(path.name.startswith("late-unseen-bar-gaps") for path in tmp_path.iterdir())
    ledger.writer_lock.release()


def test_active_journal_truncation_is_detected_before_next_append(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)
    collector.client = capture.BridgeReadClient(
        base_url="http://127.0.0.1:58710",
        api_key=preserved_cases.API_KEY,
        timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
        transport=_transport(
            source,
            now=NOW + 180,
            bars=_bar_round(source),
            event_sequence=8,
        ),
    )
    collector.clock = preserved_cases.ManualClock(NOW + 180)
    collector.capture_cycle(include_bars=True)
    assert ledger.journal_path.is_file()
    assert ledger._journal_expected_size > 0

    ledger.journal_path.write_bytes(b"")
    collector.client = capture.BridgeReadClient(
        base_url="http://127.0.0.1:58710",
        api_key=preserved_cases.API_KEY,
        timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
        transport=_transport(
            source,
            now=NOW + 181,
            bars=_bar_round(source),
            event_sequence=9,
        ),
    )
    collector.clock = preserved_cases.ManualClock(NOW + 181)
    with pytest.raises(capture.CollectionRefusal, match="active_journal_path_drift"):
        collector.capture_cycle(include_bars=False)
    ledger.writer_lock.release()


def test_gap_record_deletion_with_rehashed_chunk_is_rejected_by_main_anchor(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    first_round, later_round, _missing_epoch = _gap_rounds(source)
    first, first_ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=first_round, event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    first.capture_cycle(include_bars=True)
    first_ledger.writer_lock.release()
    resumed, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW + 180, bars=later_round, event_sequence=8),
        clock=preserved_cases.ManualClock(NOW + 180),
    )
    resumed.capture_cycle(include_bars=True)
    ledger.finalize_active()
    second_entry = ledger.entries[1]
    second_path = ledger.root / second_entry["chunk_path"]
    second = json.loads(second_path.read_text(encoding="utf-8"))
    second["late_gap_records"] = []
    encoded = capture.canonical_json_bytes(second) + b"\n"
    second_path.write_bytes(encoded)
    # Rehashing the chunk reference cannot make the unchanged gap anchors true.
    lines = ledger.manifest_path.read_text(encoding="utf-8").splitlines()
    manifest_rows = [json.loads(line) for line in lines]
    forged = manifest_rows[1]
    forged["chunk_sha256"] = hashlib.sha256(encoded).hexdigest()
    forged["chunk_size_bytes"] = len(encoded)
    forged_body = dict(forged)
    forged_body.pop("manifest_entry_sha256")
    forged["manifest_entry_sha256"] = capture.canonical_sha256(forged_body)
    ledger.manifest_path.write_bytes(
        b"".join(capture.canonical_json_bytes(row) + b"\n" for row in manifest_rows)
    )
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    with pytest.raises(
        capture.CollectionRefusal,
        match=(
            "gap_chain_rollback_or_truncation_detected|"
            "tail_commitment_manifest_projection_mismatch"
        ),
    ):
        capture.ManifestLedger(tmp_path, writer_lock=lock)
    lock.release()


def test_gap_epoch_memory_is_interval_compacted_not_a_multi_million_set() -> None:
    coverage = capture._GapEpochCoverage()
    start = 2_000_000_000
    for index in range(25_000):
        coverage.add(start + index * 60)

    assert coverage.contains(start + 24_999 * 60)
    assert len(coverage._starts) == 1
    assert len(coverage._ends) == 1


def test_first_cycle_without_direct_m1_scope_refuses_before_transport(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    transport = _transport(
        source,
        now=NOW,
        bars=_bar_round(source),
        event_sequence=7,
    )
    collector, ledger = _collector(
        tmp_path,
        transport,
        clock=preserved_cases.ManualClock(NOW),
    )

    with pytest.raises(capture.CollectionRefusal, match="complete_direct_m1_scope"):
        collector.capture_cycle(include_bars=False)
    assert transport.calls == []
    ledger.writer_lock.release()


def test_tail_registry_proof_binds_committed_main_tail(tmp_path: Path) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)

    proof = capture.validate_tail_commitment_registry(tmp_path)

    assert proof["status"] == "valid"
    assert proof["filename"] == capture.TAIL_COMMITMENT_FILENAME
    assert proof["record_count"] == 6
    assert proof["committed_state_kind"] == "manifest"
    assert proof["manifest_sequence"] == 1
    assert proof["manifest_entry_sha256"] == ledger.entries[0][
        "manifest_entry_sha256"
    ]
    assert proof["gap_source_id"] == source.source_id
    assert proof["preregistration_body_sha256"] == collector.binding.preregistration_body_sha256
    assert proof["prospective_t0_utc_inclusive"] == collector.binding.t0_utc
    assert proof["cycle_reservation_sequence"] == 1
    assert proof["last_cycle_reservation_sha256"] != capture.ZERO_SHA256
    assert proof["unresolved_cycle_reservation_sha256"] == capture.ZERO_SHA256
    assert proof["attempt_failure_sha256"] == capture.ZERO_SHA256
    reconstructed = {
        "state_kind": proof["committed_state_kind"],
        **{
            field: proof[field]
            for field in capture._TAIL_STATE_FIELDS
            if field != "state_kind"
        },
    }
    assert capture.canonical_sha256(reconstructed) == proof[
        "committed_state_sha256"
    ]
    tampered = dict(reconstructed)
    prior = str(tampered["last_cycle_reservation_sha256"])
    tampered["last_cycle_reservation_sha256"] = (
        ("0" if prior[0] != "0" else "1") + prior[1:]
    )
    assert capture.canonical_sha256(tampered) != proof["committed_state_sha256"]
    assert proof["pending_operation"] is False
    assert proof["physical_journal_present"] is False
    ledger.writer_lock.release()


def test_valid_prefix_manifest_and_newest_chunk_rollback_is_refused(
    tmp_path: Path,
) -> None:
    _collector_value, ledger = _collector_with_active_second_cycle(tmp_path)
    ledger.finalize_active()
    newest_chunk = ledger.root / ledger.entries[-1]["chunk_path"]
    manifest_lines = ledger.manifest_path.read_bytes().splitlines(keepends=True)
    assert len(manifest_lines) == 2
    newest_chunk.unlink()
    ledger.manifest_path.write_bytes(manifest_lines[0])
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    try:
        with pytest.raises(
            capture.CollectionRefusal,
            match="tail_commitment_manifest_projection_mismatch",
        ):
            capture.ManifestLedger(tmp_path, writer_lock=lock)
    finally:
        lock.release()


def test_whole_active_journal_deletion_is_refused_on_restart(tmp_path: Path) -> None:
    _collector_value, ledger = _collector_with_active_second_cycle(tmp_path)
    ledger.journal_path.unlink()
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    try:
        with pytest.raises(
            capture.CollectionRefusal,
            match="tail_commitment_journal_projection_mismatch",
        ):
            capture.ManifestLedger(tmp_path, writer_lock=lock)
    finally:
        lock.release()


def test_tail_registry_deletion_refuses_existing_capture(tmp_path: Path) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)
    ledger.tail_commitment_path.unlink()
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    try:
        with pytest.raises(
            capture.CollectionRefusal,
            match="tail_commitment_missing_for_existing_capture",
        ):
            capture.ManifestLedger(tmp_path, writer_lock=lock)
    finally:
        lock.release()


def test_chunks_ancestor_symlink_escape_is_rejected(tmp_path: Path) -> None:
    lock = capture.ExclusiveDataWriterLock(tmp_path / "capture").acquire()
    ledger = capture.ManifestLedger(tmp_path / "capture", writer_lock=lock)
    outside = tmp_path / "outside"
    outside.mkdir()
    ledger.chunks_root.rmdir()
    try:
        os.symlink(outside, ledger.chunks_root, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - host policy may forbid symlinks
        lock.release()
        pytest.skip(f"host forbids symlink creation: {exc}")
    try:
        with pytest.raises(capture.CollectionRefusal, match="reparse|identity|path_drift"):
            capture._durable_atomic_write_new(
                ledger.chunks_root / "20260101T00" / "escape.json", b"{}\n"
            )
        assert list(outside.iterdir()) == []
    finally:
        lock.release()


@pytest.mark.skipif(os.name != "nt", reason="Windows namespace durability contract")
def test_windows_namespace_publication_uses_write_through_for_new_and_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    real_publish = capture._windows_namespace_publish

    def observed_publish(source: Path, target: Path, *, replace: bool) -> None:
        calls.append(replace)
        real_publish(source, target, replace=replace)

    monkeypatch.setattr(capture, "_windows_namespace_publish", observed_publish)
    target = tmp_path / "published.bin"
    capture._durable_atomic_write_new(target, b"one")
    capture._durable_replace(target, b"two")

    assert calls == [False, True]
    assert target.read_bytes() == b"two"
    contract = capture.expected_capture_integrity_contract()
    assert contract["windows_namespace_publication_uses_movefileex_write_through"] is True
    assert contract["windows_directory_fsync_claimed"] is False


@pytest.mark.parametrize(
    ("filename", "maximum_bytes", "reason"),
    (
        (
            capture.MANIFEST_FILENAME,
            capture.MAXIMUM_MANIFEST_BYTES,
            "manifest_size_limit_exceeded",
        ),
        (
            capture.ACTIVE_JOURNAL_FILENAME,
            capture.MAXIMUM_ACTIVE_JOURNAL_BYTES,
            "active_journal_size_limit_exceeded",
        ),
    ),
)
def test_oversize_manifest_or_journal_is_rejected_before_read_allocation(
    tmp_path: Path,
    filename: str,
    maximum_bytes: int,
    reason: str,
) -> None:
    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    capture.ManifestLedger(tmp_path, writer_lock=lock)
    lock.release()
    with (tmp_path / filename).open("wb") as handle:
        handle.truncate(maximum_bytes + 1)

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    try:
        with pytest.raises(capture.CollectionRefusal, match=reason):
            capture.ManifestLedger(tmp_path, writer_lock=lock)
    finally:
        lock.release()


def test_oversize_chunk_is_rejected_before_chunk_read_allocation(tmp_path: Path) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)
    chunk_path = ledger.root / ledger.entries[0]["chunk_path"]
    ledger.writer_lock.release()
    with chunk_path.open("r+b") as handle:
        handle.truncate(capture.MAXIMUM_CHUNK_BYTES + 1)

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    try:
        with pytest.raises(
            capture.CollectionRefusal,
            match="manifest_chunk_size_limit_exceeded",
        ):
            capture.ManifestLedger(tmp_path, writer_lock=lock)
    finally:
        lock.release()


def test_manifest_append_does_not_reread_or_replace_whole_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _collector_value, ledger = _collector_with_active_second_cycle(tmp_path)
    real_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path == ledger.manifest_path:
            raise AssertionError("manifest append reread the complete manifest")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    ledger.finalize_active()

    assert len(ledger.entries) == 2
    ledger.writer_lock.release()


def test_restart_completes_prepare_when_journal_was_not_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)
    _configure_next_cycle(collector)
    real_append = capture._append_fsynced_line
    armed = True

    def fail_before_journal(path: Path, line: bytes, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal armed
        if armed and path == ledger.journal_path:
            armed = False
            raise capture.CollectionRefusal("injected_crash_before_journal")
        return real_append(path, line, **kwargs)

    monkeypatch.setattr(capture, "_append_fsynced_line", fail_before_journal)
    with pytest.raises(capture.CollectionRefusal, match="injected_crash"):
        collector.capture_cycle(include_bars=True)
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    assert len(recovered.entries) == 2
    assert not recovered.journal_path.exists()
    assert capture.validate_tail_commitment_registry(tmp_path)["pending_operation"] is False
    lock.release()


def test_restart_commits_exact_journal_published_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)
    _configure_next_cycle(collector)
    real_finish = ledger._finish_tail_operation
    armed = True

    def crash_before_journal_commit(*, phase: str) -> None:
        nonlocal armed
        pending = ledger._tail_pending_record
        if (
            armed
            and phase == "commit"
            and pending is not None
            and pending["operation_kind"] == "journal_append"
        ):
            armed = False
            raise capture.CollectionRefusal("injected_crash_before_journal_commit")
        real_finish(phase=phase)

    monkeypatch.setattr(ledger, "_finish_tail_operation", crash_before_journal_commit)
    with pytest.raises(capture.CollectionRefusal, match="injected_crash"):
        collector.capture_cycle(include_bars=True)
    assert ledger.journal_path.is_file()
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    assert len(recovered.entries) == 2
    assert not recovered.journal_path.exists()
    lock.release()


def test_restart_finishes_manifest_when_chunk_was_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _collector_value, ledger = _collector_with_active_second_cycle(tmp_path)

    def crash_before_manifest(_entry: Mapping[str, Any]) -> None:
        raise capture.CollectionRefusal("injected_crash_before_manifest")

    monkeypatch.setattr(ledger, "_append_manifest_entry", crash_before_manifest)
    with pytest.raises(capture.CollectionRefusal, match="injected_crash"):
        ledger.finalize_active()
    assert len(ledger.entries) == 1
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    assert len(recovered.entries) == 2
    assert not recovered.journal_path.exists()
    lock.release()


def test_restart_aborts_manifest_prepare_before_chunk_then_refinalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _collector_value, ledger = _collector_with_active_second_cycle(tmp_path)
    real_publish = capture._durable_atomic_write_new
    armed = True

    def crash_before_chunk(path: Path, payload: bytes) -> None:
        nonlocal armed
        if armed and path.parent.parent == ledger.chunks_root:
            armed = False
            raise capture.CollectionRefusal("injected_crash_before_chunk")
        real_publish(path, payload)

    monkeypatch.setattr(capture, "_durable_atomic_write_new", crash_before_chunk)
    with pytest.raises(capture.CollectionRefusal, match="injected_crash"):
        ledger.finalize_active()
    assert len(ledger.entries) == 1
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    assert len(recovered.entries) == 2
    assert not recovered.journal_path.exists()
    lock.release()


def test_restart_commits_manifest_published_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _collector_value, ledger = _collector_with_active_second_cycle(tmp_path)
    real_finish = ledger._finish_tail_operation
    armed = True

    def crash_before_manifest_commit(*, phase: str) -> None:
        nonlocal armed
        pending = ledger._tail_pending_record
        if (
            armed
            and phase == "commit"
            and pending is not None
            and pending["operation_kind"] == "manifest_finalize"
        ):
            armed = False
            raise capture.CollectionRefusal("injected_crash_before_manifest_commit")
        real_finish(phase=phase)

    monkeypatch.setattr(ledger, "_finish_tail_operation", crash_before_manifest_commit)
    with pytest.raises(capture.CollectionRefusal, match="injected_crash"):
        ledger.finalize_active()
    assert len(ledger.entries) == 1
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    assert len(recovered.entries) == 2
    assert not recovered.journal_path.exists()
    lock.release()


def test_restart_clears_exact_covered_journal_after_manifest_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _collector_value, ledger = _collector_with_active_second_cycle(tmp_path)

    def crash_before_clear() -> None:
        raise capture.CollectionRefusal("injected_crash_before_journal_clear")

    monkeypatch.setattr(ledger, "_clear_active_journal", crash_before_clear)
    with pytest.raises(capture.CollectionRefusal, match="injected_crash"):
        ledger.finalize_active()
    assert ledger.journal_path.is_file()
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    assert len(recovered.entries) == 2
    assert not recovered.journal_path.exists()
    lock.release()


def test_repository_and_deployed_producer_paths_must_be_distinct(
    tmp_path: Path,
) -> None:
    producer, _monitor = _producer_binding(tmp_path / "producer")
    _repository, _deployed, deployed_ex4 = _producer_paths(tmp_path / "producer")

    with pytest.raises(capture.CollectionRefusal, match="path_or_identity_invalid"):
        capture.ProducerSoftwareMonitor.bind(
            producer,
            repository_source_path=capture.BRIDGE_EA_REPOSITORY_SOURCE_PATH,
            deployed_source_path=capture.BRIDGE_EA_REPOSITORY_SOURCE_PATH,
            deployed_ex4_path=deployed_ex4,
        )


def test_every_first_cycle_get_observes_fsynced_current_reservation(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    inner = _transport(
        source,
        now=NOW,
        bars=_bar_round(source),
        event_sequence=7,
    )
    witnesses: list[tuple[str, str, bool]] = []

    class WitnessTransport:
        def __call__(self, request: Any, timeout_secs: float, maximum_bytes: int):  # type: ignore[no-untyped-def]
            view = capture._stream_tail_commitment_registry(
                tmp_path / capture.TAIL_COMMITMENT_FILENAME
            )
            witnesses.append(
                (
                    request.full_url,
                    view.committed_state[
                        "unresolved_cycle_reservation_sha256"
                    ],
                    view.pending_record is None,
                )
            )
            return inner(request, timeout_secs, maximum_bytes)

    collector, ledger = _collector(
        tmp_path,
        WitnessTransport(),  # type: ignore[arg-type]
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)

    assert len(witnesses) == 26
    assert witnesses[0][0].endswith("/v2/state")
    assert all(value != capture.ZERO_SHA256 for _url, value, _idle in witnesses)
    assert all(idle for _url, _value, idle in witnesses)
    tail_lines = [
        json.loads(line)
        for line in (tmp_path / capture.TAIL_COMMITMENT_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert tail_lines[1]["operation_kind"] == "cycle_reserve"
    assert tail_lines[1]["phase"] == "event"
    ledger.writer_lock.release()


def test_unresolved_first_reservation_terminally_fails_attempt(
    tmp_path: Path,
) -> None:
    class CrashBeforeFirstGet:
        def __call__(self, request: Any, timeout_secs: float, maximum_bytes: int):  # type: ignore[no-untyped-def]
            raise capture.CollectionRefusal("injected_crash_after_reservation")

    collector, ledger = _collector(
        tmp_path,
        CrashBeforeFirstGet(),  # type: ignore[arg-type]
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    with pytest.raises(capture.CollectionRefusal, match="authentication_not_proven"):
        collector.capture_cycle(include_bars=True)
    unresolved = capture._stream_tail_commitment_registry(
        ledger.tail_commitment_path
    ).committed_state["unresolved_cycle_reservation_sha256"]
    assert unresolved != capture.ZERO_SHA256
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    binding = _binding(tmp_path)
    with pytest.raises(capture.CollectionRefusal, match="permanently_failed"):
        recovered.recover_unresolved_cycle(
            binding=binding,
            recovered_at_epoch=NOW + 3,
        )
    view = capture._stream_tail_commitment_registry(
        recovered.tail_commitment_path
    )
    assert view.attempt_failure is not None
    assert view.committed_state["attempt_failure_sha256"] != capture.ZERO_SHA256
    assert (
        view.committed_state["unresolved_cycle_reservation_sha256"]
        == capture.ZERO_SHA256
    )
    with pytest.raises(capture.CollectionRefusal, match="permanently_failed"):
        recovered.reserve_cycle(
            binding=binding,
            producer_monitor_proof=(
                binding.producer_software_monitor.current_proof()  # type: ignore[union-attr]
            ),
            include_bars=True,
            first_cycle=True,
            cycle_started_at_epoch=NOW + 4,
        )
    lock.release()


def test_unresolved_later_reservation_gaps_every_minute_for_all_22(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)

    class CrashBeforeLaterGet:
        calls = 0

        def __call__(self, request: Any, timeout_secs: float, maximum_bytes: int):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise capture.CollectionRefusal("injected_later_crash")

    crash = CrashBeforeLaterGet()
    collector.client = capture.BridgeReadClient(
        base_url="http://127.0.0.1:58710",
        api_key=preserved_cases.API_KEY,
        timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
        transport=crash,
    )
    collector.clock = preserved_cases.ManualClock(NOW + 180)
    with pytest.raises(capture.CollectionRefusal, match="later_crash"):
        collector.capture_cycle(include_bars=False)
    assert crash.calls == 1
    binding = collector.binding
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    assert recovered.recover_unresolved_cycle(
        binding=binding,
        recovered_at_epoch=NOW + 300,
    ) is True
    recovered.finalize_active()
    latest = _chunk(recovered, len(recovered.entries) - 1)
    evidence = latest["interrupted_cycle_gap_evidence"]
    assert {row["symbol"] for row in evidence} == set(capture.SYMBOLS)
    assert len(evidence) == len(capture.SYMBOLS)
    assert {row["start_minute_epoch"] for row in evidence} == {int(NOW + 180)}
    assert {row["end_minute_epoch"] for row in evidence} == {int(NOW + 300)}
    assert {row["minute_count"] for row in evidence} == {3}
    assert all(row["baseline_eligible"] is False for row in evidence)
    assert all(
        recovered.contains_gap(symbol, int(NOW + 240))
        for symbol in capture.SYMBOLS
    )
    view = capture._stream_tail_commitment_registry(
        recovered.tail_commitment_path
    )
    assert (
        view.committed_state["unresolved_cycle_reservation_sha256"]
        == capture.ZERO_SHA256
    )
    lock.release()


def test_partial_multi_part_cycle_is_completed_only_by_final_part(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)
    monitor = collector.binding.producer_software_monitor
    assert monitor is not None
    reservation = ledger.reserve_cycle(
        binding=collector.binding,
        producer_monitor_proof=monitor.current_proof(),
        include_bars=False,
        first_cycle=False,
        cycle_started_at_epoch=NOW + 180,
    )
    first = _chunk(ledger, 0)
    part = {
        "reservation_sequence": reservation["reservation_sequence"],
        "reservation_sha256": reservation["reservation_sha256"],
        "part_index": 1,
        "part_count": 2,
        "final": False,
    }
    first.update(
        {
            "bars": [],
            "quotes": [],
            "collector_cycle_started_at_epoch": NOW + 180,
            "collector_cycle_completed_at_epoch": NOW + 180,
            "observed_at_epoch": NOW + 180,
            "utc_hour": datetime.fromtimestamp(NOW + 180, tz=UTC).strftime(
                "%Y%m%dT%H"
            ),
            "last_bar_epoch_by_symbol": dict(ledger.last_bar_epoch_by_symbol),
            "last_tick_sequence_by_symbol": dict(
                ledger.last_tick_sequence_by_symbol
            ),
            "last_tick_transport_epoch_by_symbol": dict(
                ledger.last_tick_transport_epoch_by_symbol
            ),
            "last_tick_snapshot_sha256_by_symbol": dict(
                ledger.last_tick_snapshot_sha256_by_symbol
            ),
            **capture._anchor_fields(ledger.gap_anchor),
            "late_gap_records": [],
            "cycle_reservation_sequence": reservation["reservation_sequence"],
            "cycle_reservation_sha256": reservation["reservation_sha256"],
            "cycle_part_index": 1,
            "cycle_part_count": 2,
            "cycle_part_final": False,
            "cycle_reservation_parts": [part],
            "interrupted_cycle_gap_evidence": [],
        }
    )
    ledger.append_cycle(first)
    assert (
        ledger._tail_committed_state["unresolved_cycle_reservation_sha256"]
        == reservation["reservation_sha256"]
    )
    binding = collector.binding
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    recovered.recover_unresolved_cycle(
        binding=binding,
        recovered_at_epoch=NOW + 300,
    )
    recovered.finalize_active()
    parts = [
        row
        for entry_index in range(len(recovered.entries))
        for row in _chunk(recovered, entry_index)["cycle_reservation_parts"]
        if row["reservation_sha256"] == reservation["reservation_sha256"]
    ]
    assert [row["part_index"] for row in parts] == [1, 2]
    assert [row["final"] for row in parts] == [False, True]
    assert (
        recovered._tail_committed_state[
            "unresolved_cycle_reservation_sha256"
        ]
        == capture.ZERO_SHA256
    )
    lock.release()


def test_reservation_cadence_and_full_window_capacity_survive_restart(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)
    ledger.writer_lock.release()

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    binding = _binding(tmp_path)
    monitor = binding.producer_software_monitor
    assert monitor is not None
    with pytest.raises(capture.CollectionRefusal, match="cadence_too_fast"):
        recovered.reserve_cycle(
            binding=binding,
            producer_monitor_proof=monitor.current_proof(),
            include_bars=False,
            first_cycle=False,
            cycle_started_at_epoch=NOW + 2,
        )
    reservation = recovered.reserve_cycle(
        binding=binding,
        producer_monitor_proof=monitor.current_proof(),
        include_bars=False,
        first_cycle=False,
        cycle_started_at_epoch=NOW + 3,
    )
    assert reservation["reservation_sequence"] == 2
    assert capture.SEALED_MAXIMUM_CYCLE_RESERVATIONS == 7_776_000
    assert capture.MAXIMUM_TAIL_COMMITMENT_BYTES == (
        capture.SEALED_MAXIMUM_CYCLE_RESERVATIONS
        * capture.MAXIMUM_BASE_TAIL_BYTES_PER_RESERVED_CYCLE
        + capture.MAXIMUM_WINDOW_BAR_CYCLES
        * capture.MAXIMUM_INCREMENTAL_BAR_TAIL_BYTES_PER_BAR_CYCLE
        + capture.TAIL_BOOTSTRAP_AND_FIXED_BYTE_MARGIN
    )
    assert capture.MAXIMUM_TAIL_COMMITMENT_BYTES < (
        capture.MAXIMUM_TAIL_COMMITMENT_RECORDS
        * capture.MAXIMUM_TAIL_COMMITMENT_LINE_BYTES
    )
    lock.release()


def _loaded_binding_and_paths(
    root: Path,
) -> tuple[capture.ProspectiveBinding, Path, tuple[Path, Path, Path]]:
    root.mkdir(parents=True, exist_ok=True)
    preregistration = _write_preregistration(root / "preregistration.json")
    producer_paths = _producer_paths(root / "producer")
    binding = capture.load_preregistration(
        preregistration,
        bridge_ea_repository_source=producer_paths[0],
        bridge_ea_deployed_source=producer_paths[1],
        bridge_ea_deployed_ex4=producer_paths[2],
    )
    return binding, preregistration, producer_paths


def test_offline_post_window_finalizer_is_network_free_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, preregistration, producer_paths = _loaded_binding_and_paths(
        tmp_path / "inputs"
    )
    output = tmp_path / "capture"
    source = preserved_cases._source()
    lock = capture.ExclusiveDataWriterLock(output).acquire()
    ledger = capture.ManifestLedger(output, writer_lock=lock)
    receipt = capture.StartEdgeDurabilityReceipt(
        output,
        binding=binding,
        ledger=ledger,
        writer_lock=lock,
    )
    collector = capture.ProspectiveActivityCollector(
        client=capture.BridgeReadClient(
            base_url="http://127.0.0.1:58710",
            api_key=preserved_cases.API_KEY,
            timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
            transport=_transport(
                source,
                now=NOW,
                bars=_bar_round(source),
                event_sequence=7,
            ),
        ),
        ledger=ledger,
        binding=binding,
        receipt=receipt,
        policy=capture.CollectionPolicy(),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)
    _configure_next_cycle(collector)
    collector.capture_cycle(include_bars=True)
    assert ledger.journal_path.is_file()
    lock.release()

    def forbidden_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("offline finalizer constructed a network client")

    def forbidden_key(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("offline finalizer read an API key")

    monkeypatch.setattr(capture.BridgeReadClient, "__init__", forbidden_client)
    monkeypatch.setattr(capture, "read_api_key_file", forbidden_key)
    kwargs = {
        "output_root": output,
        "preregistration_path": preregistration,
        "bridge_ea_repository_source": producer_paths[0],
        "bridge_ea_deployed_source": producer_paths[1],
        "bridge_ea_deployed_ex4": producer_paths[2],
        "clock": lambda: binding.end_epoch_exclusive,
    }
    first = capture.finalize_capture_after_window(**kwargs)
    assert first["network_requests_performed"] is False
    assert first["evaluation_performed"] is False
    assert first["authority_granted"] is False
    assert first["order_authorized"] is False
    assert not (output / capture.ACTIVE_JOURNAL_FILENAME).exists()
    before = {
        path.relative_to(output).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in output.rglob("*")
        if path.is_file() and path.name != capture.DATA_WRITER_LOCK_FILENAME
    }
    second = capture.finalize_capture_after_window(**kwargs)
    after = {
        path.relative_to(output).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in output.rglob("*")
        if path.is_file() and path.name != capture.DATA_WRITER_LOCK_FILENAME
    }
    assert second == first
    assert after == before


def test_offline_finalizer_refuses_before_end_before_ledger_or_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, preregistration, producer_paths = _loaded_binding_and_paths(
        tmp_path / "inputs"
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("pre-end finalizer crossed offline boundary")

    monkeypatch.setattr(capture, "ManifestLedger", forbidden)
    monkeypatch.setattr(capture.BridgeReadClient, "__init__", forbidden)
    with pytest.raises(capture.CollectionRefusal, match="before_end"):
        capture.finalize_capture_after_window(
            output_root=tmp_path / "capture",
            preregistration_path=preregistration,
            bridge_ea_repository_source=producer_paths[0],
            bridge_ea_deployed_source=producer_paths[1],
            bridge_ea_deployed_ex4=producer_paths[2],
            clock=lambda: binding.end_epoch_exclusive - 1,
        )
    assert not (tmp_path / "capture").exists()


def test_offline_finalizer_recovers_post_end_unresolved_cycle_compactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, preregistration, producer_paths = _loaded_binding_and_paths(
        tmp_path / "inputs"
    )
    output = tmp_path / "capture"
    source = preserved_cases._source()
    lock = capture.ExclusiveDataWriterLock(output).acquire()
    ledger = capture.ManifestLedger(output, writer_lock=lock)
    collector = capture.ProspectiveActivityCollector(
        client=capture.BridgeReadClient(
            base_url="http://127.0.0.1:58710",
            api_key=preserved_cases.API_KEY,
            timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
            transport=_transport(
                source,
                now=NOW,
                bars=_bar_round(source),
                event_sequence=7,
            ),
        ),
        ledger=ledger,
        binding=binding,
        policy=capture.CollectionPolicy(),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)

    class CrashLater:
        def __call__(self, request: Any, timeout_secs: float, maximum_bytes: int):  # type: ignore[no-untyped-def]
            raise capture.CollectionRefusal("injected_post_end_recovery")

    collector.client = capture.BridgeReadClient(
        base_url="http://127.0.0.1:58710",
        api_key=preserved_cases.API_KEY,
        timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
        transport=CrashLater(),
    )
    collector.clock = preserved_cases.ManualClock(NOW + 180)
    with pytest.raises(capture.CollectionRefusal, match="post_end_recovery"):
        collector.capture_cycle(include_bars=False)
    lock.release()

    def forbidden_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("post-end recovery constructed a client")

    monkeypatch.setattr(capture.BridgeReadClient, "__init__", forbidden_client)
    result = capture.finalize_capture_after_window(
        output_root=output,
        preregistration_path=preregistration,
        bridge_ea_repository_source=producer_paths[0],
        bridge_ea_deployed_source=producer_paths[1],
        bridge_ea_deployed_ex4=producer_paths[2],
        clock=lambda: binding.end_epoch_exclusive,
    )
    assert result["network_requests_performed"] is False
    lock = capture.ExclusiveDataWriterLock(output).acquire()
    recovered = capture.ManifestLedger(output, writer_lock=lock)
    evidence = [
        row
        for index in range(len(recovered.entries))
        for row in _chunk(recovered, index)["interrupted_cycle_gap_evidence"]
    ]
    assert len(evidence) == len(capture.SYMBOLS)
    assert {row["end_minute_epoch"] for row in evidence} == {
        int(binding.end_epoch_exclusive) - 60
    }
    assert all(row["minute_count"] > 250_000 for row in evidence)
    assert (
        recovered._tail_committed_state[
            "unresolved_cycle_reservation_sha256"
        ]
        == capture.ZERO_SHA256
    )
    lock.release()


def test_cli_finalize_branch_needs_no_base_url_key_or_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: list[dict[str, Any]] = []

    def fake_finalize(**kwargs: Any) -> dict[str, Any]:
        called.append(kwargs)
        return {
            "status": "finalized",
            "collection_only": True,
            "evaluation_performed": False,
            "authority_granted": False,
            "order_authorized": False,
        }

    def forbidden_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("CLI finalizer constructed a client")

    monkeypatch.setattr(capture, "finalize_capture_after_window", fake_finalize)
    monkeypatch.setattr(capture.BridgeReadClient, "__init__", forbidden_client)
    paths = [tmp_path / name for name in ("pre.json", "repo", "deployed", "ex4")]
    result = capture.main(
        [
            "--finalize-after-window",
            "--output-dir",
            str(tmp_path / "output"),
            "--preregistration",
            str(paths[0]),
            "--bridge-ea-repository-source",
            str(paths[1]),
            "--bridge-ea-deployed-source",
            str(paths[2]),
            "--bridge-ea-deployed-ex4",
            str(paths[3]),
        ]
    )
    assert result == 0
    assert len(called) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "finalized"


def test_pending_journal_partial_append_is_completed_from_tail_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)
    _configure_next_cycle(collector)
    real_append = capture._append_fsynced_line
    armed = True

    def tear_journal(path: Path, line: bytes, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal armed
        if armed and path == ledger.journal_path:
            armed = False
            path.write_bytes(line[: len(line) // 2])
            raise capture.CollectionRefusal("injected_partial_journal")
        return real_append(path, line, **kwargs)

    monkeypatch.setattr(capture, "_append_fsynced_line", tear_journal)
    with pytest.raises(capture.CollectionRefusal, match="partial_journal"):
        collector.capture_cycle(include_bars=True)
    ledger.writer_lock.release()
    monkeypatch.setattr(capture, "_append_fsynced_line", real_append)

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    assert len(recovered.entries) == 2
    assert not recovered.journal_path.exists()
    assert recovered._tail_pending_record is None
    lock.release()


def test_start_receipt_oversize_refuses_before_allocation(tmp_path: Path) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)
    ledger.writer_lock.release()
    receipt_path = tmp_path / capture.START_EDGE_RECEIPT_FILENAME
    with receipt_path.open("r+b") as handle:
        handle.truncate(capture.MAXIMUM_START_EDGE_RECEIPT_BYTES + 1)

    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=lock)
    with pytest.raises(capture.CollectionRefusal, match="receipt_.*oversize"):
        capture.StartEdgeDurabilityReceipt(
            tmp_path,
            binding=collector.binding,
            ledger=recovered,
            writer_lock=lock,
        )
    lock.release()


def test_abandoned_preregistrations_must_preserve_exact_lineage(
    tmp_path: Path,
) -> None:
    payload = _preregistration_payload()
    payload["abandoned_preregistrations"].pop(0)
    _rehash_preregistration(payload)
    path = _write_preregistration(tmp_path / "truncated-lineage.json", payload)
    with pytest.raises(capture.CollectionRefusal, match="contract_invalid"):
        capture.load_preregistration(path)


def test_real_v3_sealer_validated_lineage_loads_in_collector(
    tmp_path: Path,
) -> None:
    from tools import seal_mt4_tick_volume_preregistration_resilient_v3 as sealer

    repository, deployed, deployed_ex4 = _producer_paths(tmp_path / "producer")
    payload = _preregistration_payload()
    payload.pop("preregistration_body_sha256")
    real_lineage = [
        *(copy.deepcopy(row) for row in sealer._v2._abandoned_attempts()[:-1]),
        sealer._corrected_crossed_attempt(),
    ]
    assert len(real_lineage) == 5
    assert real_lineage == capture.expected_abandoned_preregistrations()
    payload["abandoned_preregistrations"] = real_lineage
    payload["execution_provenance_contract"] = (
        sealer.execution_provenance_contract()
    )
    payload["preservation_filename_contract"] = (
        sealer.preservation_filename_contract()
    )
    manifest = copy.deepcopy(sealer.screen.attempt_manifest())
    payload["strategy"]["attempt_manifest"] = manifest
    payload["strategy"]["attempt_manifest_sha256"] = (
        sealer.base.canonical_sha256(manifest)
    )

    identities = {
        name: _file_identity(path) for name, path in sealer._source_paths().items()
    }
    repository_identity = _file_identity(repository)
    deployed_identity = _file_identity(deployed)
    deployed_ex4_identity = _file_identity(deployed_ex4)
    identities["production_engine_component:MQL4/Experts/BridgeEA.mq4"] = (
        repository_identity
    )
    identities["bridge_ea_deployed_source"] = deployed_identity
    identities["bridge_ea_deployed_ex4"] = deployed_ex4_identity
    identities["cost_capture"] = {
        "capture_json": _bytes_identity("ig_mt4_bid_ask_capture.json", b"json"),
        "capture_npz": _bytes_identity("ig_mt4_bid_ask_samples.npz", b"npz"),
    }
    identities["fee_attestation"] = {
        "attestation": _bytes_identity("mtvclc_fee_attestation.json", b"fee")
    }
    identities["production_runtime_context"] = {}
    payload["source_identities"] = identities
    payload["upstream_producer_software"] = _producer_contract(
        repository_identity,
        deployed_identity,
        deployed_ex4_identity,
    )
    payload["preregistration_body_sha256"] = sealer.base.canonical_sha256(payload)

    assert sealer.validate_preregistration(payload) is True
    preregistration = _write_preregistration(
        tmp_path / "real-sealer-validated.json", payload
    )
    binding = capture.load_preregistration(
        preregistration,
        bridge_ea_repository_source=repository,
        bridge_ea_deployed_source=deployed,
        bridge_ea_deployed_ex4=deployed_ex4,
    )
    assert binding.preregistration_body_sha256 == payload[
        "preregistration_body_sha256"
    ]


def test_lock_reparse_ancestor_refuses_before_outside_mutation(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "redirect"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - host policy may forbid symlinks
        pytest.skip(f"host forbids symlink creation: {exc}")
    target = link / "new-capture"
    with pytest.raises(capture.CollectionRefusal, match="lock_path_invalid"):
        capture.ExclusiveDataWriterLock(target).acquire()
    assert not (outside / "new-capture").exists()


def test_inherited_support_verifier_uses_bounded_descriptor_not_read_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, int, int | None]] = []
    real = capture._read_bounded_source_descriptor

    def observed(
        path: Path,
        *,
        maximum_bytes: int,
        expected_size: int | None,
    ) -> tuple[bytes, tuple[int, int, int, int]]:
        calls.append((path, maximum_bytes, expected_size))
        return real(
            path,
            maximum_bytes=maximum_bytes,
            expected_size=expected_size,
        )

    def forbidden_read_bytes(path: Path) -> bytes:
        raise AssertionError("inherited verifier used Path.read_bytes")

    monkeypatch.setattr(capture, "_read_bounded_source_descriptor", observed)
    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    assert capture.support._verified_support_sha256() == capture.BASE_SUPPORT_SHA256
    assert calls == [
        (
            capture.BASE_SUPPORT_PATH,
            capture.BASE_SUPPORT_SIZE_BYTES,
            capture.BASE_SUPPORT_SIZE_BYTES,
        )
    ]


def test_chunk_swap_between_preflight_and_replay_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = preserved_cases._source()
    collector, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bars=_bar_round(source), event_sequence=7),
        clock=preserved_cases.ManualClock(NOW + 1),
    )
    collector.capture_cycle(include_bars=True)
    chunk_path = ledger.root / ledger.entries[0]["chunk_path"]
    ledger.writer_lock.release()
    real_read = capture._read_bounded_regular_file
    swapped = False

    def swap_after_manifest(path: Path, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal swapped
        result = real_read(path, **kwargs)
        if path == tmp_path / capture.MANIFEST_FILENAME and not swapped:
            swapped = True
            replacement = chunk_path.with_suffix(".replacement")
            replacement.write_bytes(chunk_path.read_bytes())
            os.replace(replacement, chunk_path)
        return result

    monkeypatch.setattr(capture, "_read_bounded_regular_file", swap_after_manifest)
    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    try:
        with pytest.raises(capture.CollectionRefusal, match="hash_mismatch"):
            capture.ManifestLedger(tmp_path, writer_lock=lock)
    finally:
        lock.release()


def test_pinned_dependency_execution_ignores_valid_header_forged_pyc(
    tmp_path: Path,
) -> None:
    import importlib._bootstrap_external as bootstrap_external
    import importlib.util

    tools_root = tmp_path / "isolated" / "tools"
    tools_root.mkdir(parents=True)
    copied: dict[Path, Path] = {}
    for source in (
        capture.TOOL_PATH,
        capture.SUPPORT_PATH,
        capture.BASE_SUPPORT_PATH,
    ):
        target = tools_root / source.name
        target.write_bytes(source.read_bytes())
        copied[source] = target
    marker = tmp_path / "forged-pyc-executed"
    for source in (
        copied[capture.SUPPORT_PATH],
        copied[capture.BASE_SUPPORT_PATH],
    ):
        source_stat = source.stat()
        malicious = compile(
            (
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
                "raise RuntimeError('forged pyc executed')\n"
            ),
            str(source),
            "exec",
        )
        pyc = Path(importlib.util.cache_from_source(str(source)))
        pyc.parent.mkdir(parents=True, exist_ok=True)
        pyc.write_bytes(
            bootstrap_external._code_to_timestamp_pyc(  # type: ignore[attr-defined]
                malicious,
                int(source_stat.st_mtime),
                source_stat.st_size,
            )
        )
    module_name = "_isolated_v3_pyc_regression"
    support_module_name = (
        "_fxstack_mtvclc_resilient_v3_support_"
        + capture.SUPPORT_SHA256[:16]
    )
    prior_support_bound = support_module_name in sys.modules
    prior_support = sys.modules.get(support_module_name)
    repository_string = str(tmp_path / "isolated")
    spec = importlib.util.spec_from_file_location(
        module_name, copied[capture.TOOL_PATH]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        assert module.SUPPORT_SHA256 == capture.SUPPORT_SHA256
        assert module.BASE_SUPPORT_SHA256 == capture.BASE_SUPPORT_SHA256
        assert not marker.exists()
        assert (support_module_name in sys.modules) is prior_support_bound
        if prior_support_bound:
            assert sys.modules[support_module_name] is prior_support
    finally:
        sys.modules.pop(module_name, None)
        if prior_support_bound:
            sys.modules[support_module_name] = prior_support
        else:
            sys.modules.pop(support_module_name, None)
        while repository_string in sys.path:
            sys.path.remove(repository_string)


def test_import_restores_preexisting_private_support_module_binding() -> None:
    support_module_name = (
        "_fxstack_mtvclc_resilient_v3_support_"
        + capture.SUPPORT_SHA256[:16]
    )
    module_name = "_isolated_v3_support_binding_regression"
    prior_support_bound = support_module_name in sys.modules
    prior_support = sys.modules.get(support_module_name)
    sentinel = capture.support
    spec = importlib.util.spec_from_file_location(module_name, capture.TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[support_module_name] = sentinel
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        assert sys.modules[support_module_name] is sentinel
    finally:
        sys.modules.pop(module_name, None)
        if prior_support_bound:
            sys.modules[support_module_name] = prior_support
        else:
            sys.modules.pop(support_module_name, None)


def test_live_writer_supervision_fast_path_does_not_rescan_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    ledger = capture.ManifestLedger(tmp_path, writer_lock=lock)

    def forbidden_scan(path: Path) -> Any:
        raise AssertionError("live fast path rescanned the complete tail")

    monkeypatch.setattr(capture, "_stream_tail_commitment_registry", forbidden_scan)
    proof = ledger.live_writer_supervision_fast_path()
    assert proof["process_id"] == os.getpid()
    assert proof["writer_lock_held"] is True
    assert proof["full_tail_scan_performed"] is False
    assert proof["artifacts"]["tail"]["present"] is True
    assert proof["evaluation_performed"] is False
    assert proof["authority_granted"] is False
    lock.release()
