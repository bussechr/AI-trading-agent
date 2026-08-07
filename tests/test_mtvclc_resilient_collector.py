from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

import pytest

from tools import capture_ig_mt4_m1_activity_resilient as capture
from tools import check_mt4_tick_volume_collector_continuity_resilient as continuity
from tools import seal_mt4_tick_volume_preregistration_resilient as replacement_sealer
from tools import verify_mt4_tick_volume_capture_handoff as handoff


ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = ROOT / "tests"
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))
preserved_cases = importlib.import_module("test_capture_ig_mt4_m1_activity")
NOW = preserved_cases.NOW
ZERO_SHA256 = "0" * 64


def _file_identity(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _preregistration_payload() -> dict[str, Any]:
    payload = preserved_cases._preregistration_payload()
    body = {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key != "preregistration_body_sha256"
    }
    body["capture_integrity_contract"] = capture.expected_capture_integrity_contract()
    identities = body["source_identities"]
    identities["collector_source"] = _file_identity(capture.TOOL_PATH)
    identities["collector_support_source"] = _file_identity(
        capture.PRESERVED_SUPPORT_PATH
    )
    return {
        **body,
        "preregistration_body_sha256": capture.canonical_sha256(body),
    }


def _write_preregistration(path: Path, payload: Mapping[str, Any]) -> Path:
    path.write_text(
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _collector(
    output: Path,
    transport: preserved_cases.FakeBridgeTransport,
    *,
    clock: preserved_cases.ManualClock | None = None,
    binding: capture.ProspectiveBinding | None = None,
) -> tuple[
    capture.ProspectiveActivityCollector,
    capture.ManifestLedger,
    preserved_cases.ManualClock,
]:
    active_clock = clock or preserved_cases.ManualClock()
    writer_lock = capture.ExclusiveDataWriterLock(output).acquire()
    ledger = capture.ManifestLedger(output, writer_lock=writer_lock)
    policy = capture.CollectionPolicy(
        bar_limit=400,
        tick_interval_secs=2.0,
        bar_interval_secs=60.0,
        rollover_mode="refuse",
    )
    return (
        capture.ProspectiveActivityCollector(
            client=capture.BridgeReadClient(
                base_url="http://127.0.0.1:58710",
                api_key=preserved_cases.API_KEY,
                timeout_secs=5.0,
                transport=transport,
            ),
            ledger=ledger,
            binding=binding or preserved_cases._binding(),
            policy=policy,
            clock=active_clock,
        ),
        ledger,
        active_clock,
    )


def _chunk(ledger: capture.ManifestLedger, index: int = -1) -> dict[str, Any]:
    entry = ledger.entries[index]
    return json.loads((ledger.root / entry["chunk_path"]).read_text(encoding="utf-8"))


def _shift_rows_after_gap(rows: list[dict[str, Any]], *, gap_index: int) -> None:
    for row in rows[gap_index:]:
        parsed = datetime.fromisoformat(str(row["time"]))
        row["time"] = (parsed + timedelta(minutes=1)).isoformat()


def _shift_round(
    bar_round: Mapping[str, Mapping[str, Any]], *, minutes: int
) -> dict[str, dict[str, Any]]:
    shifted = copy.deepcopy(bar_round)
    for payload in shifted.values():
        for row in payload["bars"]:
            parsed = datetime.fromisoformat(str(row["time"]))
            row["time"] = (parsed + timedelta(minutes=minutes)).isoformat()
    return shifted


def _empty_integrity_chunk(
    binding: capture.ProspectiveBinding,
) -> dict[str, Any]:
    return {
        "schema_version": capture.CHUNK_SCHEMA_VERSION,
        "collector_schema_version": capture.COLLECTOR_SCHEMA_VERSION,
        "source_contract_id": capture.SOURCE_CONTRACT_ID,
        "activity_metric_id": capture.ACTIVITY_METRIC_ID,
        "scope_version": capture.SCOPE_VERSION,
        "symbol_scope": list(capture.SYMBOLS),
        "timeframe": capture.TIMEFRAME,
        "minimum_m1_history_bars": capture.MINIMUM_M1_BARS,
        "maximum_quote_gap_seconds": capture.MAXIMUM_TICK_INTERVAL_SECS,
        "requested_bar_limit": capture.DEFAULT_BAR_LIMIT,
        "configured_tick_interval_seconds": capture.DEFAULT_TICK_INTERVAL_SECS,
        "utc_hour": datetime.fromtimestamp(binding.t0_epoch + 1.0, tz=UTC).strftime(
            "%Y%m%dT%H"
        ),
        "segment_index": 1,
        "collector_cycle_started_at_epoch": binding.t0_epoch + 1.0,
        "collector_cycle_completed_at_epoch": binding.t0_epoch + 1.0,
        "observed_at_epoch": binding.t0_epoch + 1.0,
        **binding.chunk_fields(),
        "source": {"market_source_id": "a" * 64},
        "bars": [],
        "quotes": [],
        "last_bar_epoch_by_symbol": {symbol: 0 for symbol in capture.SYMBOLS},
        "last_tick_sequence_by_symbol": {symbol: 0 for symbol in capture.SYMBOLS},
        "last_tick_transport_epoch_by_symbol": {
            symbol: 0.0 for symbol in capture.SYMBOLS
        },
        "last_tick_snapshot_sha256_by_symbol": {
            symbol: ZERO_SHA256 for symbol in capture.SYMBOLS
        },
        "collection_only": True,
        "evaluation_performed": False,
        "success_claim_authorized": False,
        "authority_granted": False,
        "activation_authorized": False,
        "order_authorized": False,
    }


def test_preregistration_requires_exact_first_observation_contract_and_sources(
    tmp_path: Path,
) -> None:
    payload = _preregistration_payload()
    preregistration = _write_preregistration(tmp_path / "prereg.json", payload)

    binding = capture.load_preregistration(preregistration)

    assert binding.preregistration_body_sha256 == payload["preregistration_body_sha256"]
    contract = payload["capture_integrity_contract"]
    assert contract == capture.expected_capture_integrity_contract()
    assert contract["schema_version"] == (
        "fxstack.scalp.mtvclc_capture_integrity_contract.v2"
    )
    assert contract["contract_id"] == (
        "authenticated_finalized_m1_first_observation_or_absence_wins_"
        "across_same_source_restart.v2"
    )
    assert contract["late_unseen_overlap_at_or_before_watermark_is_ignored"] is True
    assert contract["durable_per_symbol_watermark_never_regresses"] is True
    assert "unverifiable_overlap_at_or_before_last_epoch_refuses" not in contract
    assert capture.COLLECTOR_SCHEMA_VERSION == (
        "fxstack.external_ig_mt4_m1_activity_resilient_collector.v2"
    )
    assert contract["collector_source_sha256"] == capture.collector_source_sha256()
    assert contract["collector_support_source_sha256"] == (
        capture.PRESERVED_SUPPORT_SHA256
    )

    missing_policy = copy.deepcopy(payload)
    missing_policy.pop("capture_integrity_contract")
    missing_policy["preregistration_body_sha256"] = capture.canonical_sha256(
        {
            key: value
            for key, value in missing_policy.items()
            if key != "preregistration_body_sha256"
        }
    )
    missing_path = _write_preregistration(
        tmp_path / "missing-policy.json", missing_policy
    )
    with pytest.raises(capture.CollectionRefusal, match="contract_invalid"):
        capture.load_preregistration(missing_path)

    wrong_support = copy.deepcopy(payload)
    wrong_support["source_identities"]["collector_support_source"]["size_bytes"] += 1
    wrong_support["preregistration_body_sha256"] = capture.canonical_sha256(
        {
            key: value
            for key, value in wrong_support.items()
            if key != "preregistration_body_sha256"
        }
    )
    wrong_path = _write_preregistration(tmp_path / "wrong-support.json", wrong_support)
    with pytest.raises(capture.CollectionRefusal, match="contract_invalid"):
        capture.load_preregistration(wrong_path)


def test_same_source_restart_keeps_first_bar_when_later_payload_is_revised(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    first_round = preserved_cases._bar_round(source)
    first_transport = preserved_cases.FakeBridgeTransport(
        states=[preserved_cases._state(source)] * 2,
        ticks=[
            preserved_cases._ticks(
                source,
                received_at=NOW - 2.0,
                token_epoch=int(NOW) - 20,
                event_sequence=7,
            )
        ],
        bar_rounds=[first_round],
    )
    collector, ledger, _clock = _collector(tmp_path, first_transport)
    collector.capture_cycle(include_bars=True)
    first_entry = dict(ledger.entries[0])
    first_chunk_path = ledger.root / first_entry["chunk_path"]
    first_chunk_bytes = first_chunk_path.read_bytes()
    first_volume = _chunk(ledger)["bars"][capture.MINIMUM_M1_BARS - 1]["tick_volume"]
    ledger.writer_lock.release()
    revised_round = copy.deepcopy(first_round)
    revised_round["EURUSD"]["bars"][-1]["volume"] += 999
    resumed_clock = preserved_cases.ManualClock(NOW + 1.0)
    resumed_transport = preserved_cases.FakeBridgeTransport(
        states=[preserved_cases._state(source, now=NOW + 1.0)] * 2,
        ticks=[
            preserved_cases._ticks(
                source,
                received_at=NOW - 1.0,
                token_epoch=int(NOW) - 19,
                event_sequence=8,
            )
        ],
        bar_rounds=[revised_round],
    )
    resumed, resumed_ledger, _clock = _collector(
        tmp_path, resumed_transport, clock=resumed_clock
    )

    resumed.capture_cycle(include_bars=True)
    resumed_ledger.finalize_active()

    assert len(resumed_ledger.entries) == 2
    assert _chunk(resumed_ledger, 1)["bars"] == []
    assert first_chunk_path.read_bytes() == first_chunk_bytes
    assert (
        _chunk(resumed_ledger, 0)["bars"][capture.MINIMUM_M1_BARS - 1]["tick_volume"]
        == first_volume
    )
    assert (
        len(resumed_ledger.hash_chained_bar_epochs_by_symbol["EURUSD"])
        == capture.MINIMUM_M1_BARS
    )
    resumed_ledger.writer_lock.release()


def test_unseen_late_backfill_behind_watermark_is_ignored_and_gap_stays_visible(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    gapped_round = preserved_cases._bar_round(source)
    for payload in gapped_round.values():
        _shift_rows_after_gap(payload["bars"], gap_index=120)
    normal_round = preserved_cases._bar_round(source)
    missing_epoch = int(
        datetime.fromisoformat(
            str(normal_round["EURUSD"]["bars"][120]["time"])
        ).timestamp()
    )
    transport = preserved_cases.FakeBridgeTransport(
        states=[preserved_cases._state(source)] * 4,
        ticks=[
            preserved_cases._ticks(
                source,
                received_at=NOW - 2.0,
                token_epoch=int(NOW) - 20,
                event_sequence=7,
            ),
            preserved_cases._ticks(
                source,
                received_at=NOW - 1.0,
                token_epoch=int(NOW) - 19,
                event_sequence=8,
            ),
        ],
        bar_rounds=[gapped_round, normal_round],
    )
    collector, ledger, clock = _collector(tmp_path, transport)
    collector.capture_cycle(include_bars=True)
    first_chunk_bytes = (ledger.root / ledger.entries[0]["chunk_path"]).read_bytes()
    first_watermark = ledger.last_bar_epoch_by_symbol["EURUSD"]
    clock.now += 1.0

    collector.capture_cycle(include_bars=True)
    ledger.finalize_active()

    assert len(ledger.entries) == 2
    assert _chunk(ledger, 1)["bars"] == []
    assert (ledger.root / ledger.entries[0]["chunk_path"]).read_bytes() == (
        first_chunk_bytes
    )
    assert ledger.last_bar_epoch_by_symbol["EURUSD"] == first_watermark
    assert (
        ledger.hash_chained_bar_epochs_by_symbol["EURUSD"].contains(missing_epoch)
        is False
    )
    ledger.writer_lock.release()


def test_rows_strictly_above_durable_watermark_are_accepted(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    first_round = preserved_cases._bar_round(source)
    advanced_round = _shift_round(first_round, minutes=1)
    transport = preserved_cases.FakeBridgeTransport(
        states=[preserved_cases._state(source)] * 4,
        ticks=[
            preserved_cases._ticks(
                source,
                received_at=NOW - 2.0,
                token_epoch=int(NOW) - 20,
                event_sequence=7,
            ),
            preserved_cases._ticks(
                source,
                received_at=NOW - 1.0,
                token_epoch=int(NOW) - 19,
                event_sequence=8,
            ),
        ],
        bar_rounds=[first_round, advanced_round],
    )
    collector, ledger, clock = _collector(tmp_path, transport)
    collector.capture_cycle(include_bars=True)
    first_watermarks = dict(ledger.last_bar_epoch_by_symbol)
    clock.now += 1.0

    collector.capture_cycle(include_bars=True)
    ledger.finalize_active()

    fresh_bars = _chunk(ledger, 1)["bars"]
    assert len(fresh_bars) == len(capture.SYMBOLS)
    assert {row["symbol"] for row in fresh_bars} == set(capture.SYMBOLS)
    assert all(
        row["minute_epoch"] == first_watermarks[row["symbol"]] + 60
        for row in fresh_bars
    )
    assert ledger.last_bar_epoch_by_symbol == {
        symbol: epoch + 60 for symbol, epoch in first_watermarks.items()
    }
    ledger.writer_lock.release()


def test_restart_reconstructs_durable_watermark_before_bar_admission(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    first_round = preserved_cases._bar_round(source)
    first_transport = preserved_cases.FakeBridgeTransport(
        states=[preserved_cases._state(source)] * 2,
        ticks=[
            preserved_cases._ticks(
                source,
                received_at=NOW - 2.0,
                token_epoch=int(NOW) - 20,
                event_sequence=7,
            )
        ],
        bar_rounds=[first_round],
    )
    collector, ledger, _clock = _collector(tmp_path, first_transport)
    collector.capture_cycle(include_bars=True)
    durable_watermarks = dict(ledger.last_bar_epoch_by_symbol)
    ledger.writer_lock.release()

    advanced_round = _shift_round(first_round, minutes=1)
    resumed_transport = preserved_cases.FakeBridgeTransport(
        states=[preserved_cases._state(source, now=NOW + 1.0)] * 2,
        ticks=[
            preserved_cases._ticks(
                source,
                received_at=NOW - 1.0,
                token_epoch=int(NOW) - 19,
                event_sequence=8,
            )
        ],
        bar_rounds=[advanced_round],
    )
    resumed, resumed_ledger, _clock = _collector(
        tmp_path,
        resumed_transport,
        clock=preserved_cases.ManualClock(NOW + 1.0),
    )

    assert resumed_ledger.last_bar_epoch_by_symbol == durable_watermarks
    assert resumed.last_bar_epoch_by_symbol == durable_watermarks
    resumed.capture_cycle(include_bars=True)
    resumed_ledger.finalize_active()

    fresh_bars = _chunk(resumed_ledger, 1)["bars"]
    assert len(fresh_bars) == len(capture.SYMBOLS)
    assert resumed_ledger.last_bar_epoch_by_symbol == {
        symbol: epoch + 60 for symbol, epoch in durable_watermarks.items()
    }
    resumed_ledger.writer_lock.release()


def test_restart_preserves_t0_binding_and_refuses_source_rollover(
    tmp_path: Path,
) -> None:
    source_a = preserved_cases._source(suffix="a")
    first_transport = preserved_cases.FakeBridgeTransport(
        states=[preserved_cases._state(source_a)] * 2,
        ticks=[
            preserved_cases._ticks(
                source_a,
                received_at=NOW - 2.0,
                token_epoch=int(NOW) - 20,
                event_sequence=7,
            )
        ],
    )
    original_binding = preserved_cases._binding()
    collector, ledger, _clock = _collector(
        tmp_path, first_transport, binding=original_binding
    )
    collector.capture_cycle(include_bars=False)
    ledger.writer_lock.release()
    source_b = preserved_cases._source(suffix="b")
    resumed_transport = preserved_cases.FakeBridgeTransport(
        states=[preserved_cases._state(source_b, now=NOW + 1.0)],
        ticks=[],
    )
    resumed, resumed_ledger, _clock = _collector(
        tmp_path,
        resumed_transport,
        clock=preserved_cases.ManualClock(NOW + 1.0),
        binding=original_binding,
    )

    assert resumed.binding.t0_epoch == original_binding.t0_epoch
    with pytest.raises(capture.CollectionRefusal, match="source_rollover_refused"):
        resumed.capture_cycle(include_bars=False)
    assert len(resumed_ledger.entries) == 1
    assert _chunk(resumed_ledger)["prospective_t0_utc_inclusive"] == (
        original_binding.t0_utc
    )
    resumed_ledger.writer_lock.release()


def test_collector_transport_is_get_only_and_chunks_keep_all_authority_false(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    transport = preserved_cases.FakeBridgeTransport(
        states=[preserved_cases._state(source)] * 2,
        ticks=[
            preserved_cases._ticks(
                source,
                received_at=NOW - 1.0,
                token_epoch=int(NOW) - 10,
            )
        ],
    )
    collector, ledger, _clock = _collector(tmp_path, transport)
    collector.capture_cycle(include_bars=False)
    chunk = _chunk(ledger)

    assert {call["method"] for call in transport.calls} == {"GET"}
    assert {call["path"] for call in transport.calls} <= {
        "/v2/state",
        "/v2/market/ticks",
        "/v2/market/bars",
    }
    for field in (
        "evaluation_performed",
        "success_claim_authorized",
        "authority_granted",
        "activation_authorized",
        "order_authorized",
    ):
        assert chunk[field] is False
    ledger.writer_lock.release()


def test_continuity_identity_binds_resilient_source_contract_and_config(
    tmp_path: Path,
) -> None:
    preregistration = _write_preregistration(
        tmp_path / "preregistration.json", _preregistration_payload()
    )
    api_key = tmp_path / "api-key.txt"
    api_key.write_text("must-not-enter-identity\n", encoding="utf-8")
    output = tmp_path / "capture"
    output.mkdir()
    binding = capture.load_preregistration(preregistration)
    writer_lock = capture.ExclusiveDataWriterLock(output).acquire()
    ledger = capture.ManifestLedger(output, writer_lock=writer_lock)
    ledger.append_cycle(_empty_integrity_chunk(binding))
    writer_lock.release()
    policy = continuity.GuardPolicy(
        tick_interval_secs=2.0,
        bar_interval_secs=60.0,
        bar_limit=400,
        http_timeout_secs=5.0,
    )

    first = continuity.inspect_continuity(
        preregistration=preregistration,
        output_dir=output,
        api_key_file=api_key,
        base_url="http://127.0.0.1:58710",
        policy=policy,
        initialize_guard=True,
    )
    second = continuity.inspect_continuity(
        preregistration=preregistration,
        output_dir=output,
        api_key_file=api_key,
        base_url="http://127.0.0.1:58710",
        policy=policy,
        require_guard=True,
    )
    identity_bytes = (output / continuity.GUARD_IDENTITY_FILENAME).read_bytes()
    identity = json.loads(identity_bytes)

    assert first["guard_identity_sha256"] == second["guard_identity_sha256"]
    assert second["manifest_sequence"] == 1
    assert identity["collector_source_sha256"] == capture.collector_source_sha256()
    assert identity["capture_integrity_contract"] == (
        capture.expected_capture_integrity_contract()
    )
    assert identity["policy"] == {
        "bar_interval_secs": 60.0,
        "bar_limit": 400,
        "http_timeout_secs": 5.0,
        "rollover_mode": "refuse",
        "tick_interval_secs": 2.0,
    }
    assert (
        identity["resume_contract"]["first_authenticated_finalized_observation_wins"]
        is True
    )
    assert b"must-not-enter-identity" not in identity_bytes


def _three_cycle_transport(
    source: capture.SourceIdentity,
) -> preserved_cases.FakeBridgeTransport:
    return preserved_cases.FakeBridgeTransport(
        states=[
            preserved_cases._state(source, now=NOW),
            preserved_cases._state(source, now=NOW),
            preserved_cases._state(source, now=NOW + 1.0),
            preserved_cases._state(source, now=NOW + 1.0),
            preserved_cases._state(source, now=NOW + 2.0),
            preserved_cases._state(source, now=NOW + 2.0),
        ],
        ticks=[
            preserved_cases._ticks(
                source,
                received_at=NOW - 3.0,
                token_epoch=int(NOW) - 30,
                event_sequence=7,
            ),
            preserved_cases._ticks(
                source,
                received_at=NOW - 2.0,
                token_epoch=int(NOW) - 29,
                event_sequence=8,
            ),
            preserved_cases._ticks(
                source,
                received_at=NOW - 1.0,
                token_epoch=int(NOW) - 28,
                event_sequence=9,
            ),
        ],
    )


def test_hour_journal_fsyncs_cycles_then_compacts_to_one_portable_chunk(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    collector, ledger, clock = _collector(tmp_path, _three_cycle_transport(source))

    collector.capture_cycle(include_bars=False)
    assert len(ledger.entries) == 1
    assert not ledger.journal_path.exists()
    clock.now += 1.0
    collector.capture_cycle(include_bars=False)
    clock.now += 1.0
    collector.capture_cycle(include_bars=False)

    assert len(ledger.entries) == 1
    assert ledger.journal_path.is_file()
    assert len(ledger.journal_path.read_bytes().splitlines()) == 2
    assert len(list(ledger.chunks_root.rglob("*.json"))) == 1

    ledger.finalize_active()

    assert len(ledger.entries) == 2
    assert not ledger.journal_path.exists()
    assert len(list(ledger.chunks_root.rglob("*.json"))) == 2
    compacted = _chunk(ledger)
    assert set(compacted) == handoff.CHUNK_FIELDS
    assert compacted["collector_schema_version"] == (capture.COLLECTOR_SCHEMA_VERSION)
    assert len(compacted["quotes"]) == 2 * len(capture.SYMBOLS)
    assert len(ledger.manifest_path.read_bytes().splitlines()) == 2
    ledger.writer_lock.release()


def test_restart_recovers_and_finalizes_complete_active_journal(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    collector, ledger, clock = _collector(tmp_path, _three_cycle_transport(source))
    collector.capture_cycle(include_bars=False)
    clock.now += 1.0
    collector.capture_cycle(include_bars=False)
    assert ledger.journal_path.is_file()
    assert len(ledger.entries) == 1
    ledger.writer_lock.release()

    resumed_lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    resumed = capture.ManifestLedger(tmp_path, writer_lock=resumed_lock)

    assert len(resumed.entries) == 2
    assert not resumed.journal_path.exists()
    assert resumed.last_tick_sequence_by_symbol == {
        symbol: 2 for symbol in capture.SYMBOLS
    }
    assert len(_chunk(resumed)["quotes"]) == len(capture.SYMBOLS)
    resumed_lock.release()


def test_restart_refuses_unverifiable_partial_journal_tail_without_truncation(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    collector, ledger, clock = _collector(tmp_path, _three_cycle_transport(source))
    collector.capture_cycle(include_bars=False)
    clock.now += 1.0
    collector.capture_cycle(include_bars=False)
    clock.now += 1.0
    collector.capture_cycle(include_bars=False)
    complete_journal = ledger.journal_path.read_bytes()
    with ledger.journal_path.open("ab") as handle:
        handle.write(b'{"partial_crash_record":')
        handle.flush()
        os.fsync(handle.fileno())
    ledger.writer_lock.release()

    torn_journal = ledger.journal_path.read_bytes()
    assert complete_journal.endswith(b"\n")
    resumed_lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    try:
        with pytest.raises(
            capture.CollectionRefusal,
            match="active_journal_partial_record_unverifiable",
        ):
            capture.ManifestLedger(tmp_path, writer_lock=resumed_lock)
        assert ledger.journal_path.read_bytes() == torn_journal
    finally:
        resumed_lock.release()


def test_loaded_collector_source_drift_refuses_append_finalize_and_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_copy = tmp_path / "loaded-collector-source.py"
    source_bytes = capture.TOOL_PATH.read_bytes()
    source_copy.write_bytes(source_bytes)
    source_stat = source_copy.stat()
    monkeypatch.setattr(capture, "TOOL_PATH", source_copy)
    monkeypatch.setattr(
        capture,
        "_MODULE_START_COLLECTOR_SOURCE_SHA256",
        hashlib.sha256(source_bytes).hexdigest(),
    )
    monkeypatch.setattr(
        capture,
        "_MODULE_START_COLLECTOR_SOURCE_SIZE_BYTES",
        len(source_bytes),
    )
    monkeypatch.setattr(
        capture,
        "_MODULE_START_COLLECTOR_SOURCE_STAT_IDENTITY",
        capture._source_stat_identity(source_stat),
    )

    output = tmp_path / "capture"
    source = preserved_cases._source()
    collector, ledger, clock = _collector(output, _three_cycle_transport(source))
    collector.capture_cycle(include_bars=False)
    clock.now += 1.0
    collector.capture_cycle(include_bars=False)
    _target, _previous, _tail, records = ledger._read_active_journal()

    source_copy.write_bytes(source_bytes + b"\n# source drift\n")
    with pytest.raises(capture.CollectionRefusal, match="collector_source_path_drift"):
        capture.collector_source_sha256()
    with pytest.raises(capture.CollectionRefusal, match="collector_source_path_drift"):
        ledger._append_journal_wrapper(records[-1])
    with pytest.raises(capture.CollectionRefusal, match="collector_source_path_drift"):
        ledger.finalize_active()
    ledger.writer_lock.release()

    resumed_lock = capture.ExclusiveDataWriterLock(output).acquire()
    try:
        with pytest.raises(
            capture.CollectionRefusal, match="collector_source_path_drift"
        ):
            capture.ManifestLedger(output, writer_lock=resumed_lock)
    finally:
        resumed_lock.release()


def test_restart_adopts_matching_orphan_chunk_from_pre_manifest_crash(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    collector, ledger, clock = _collector(tmp_path, _three_cycle_transport(source))
    collector.capture_cycle(include_bars=False)
    clock.now += 1.0
    collector.capture_cycle(include_bars=False)
    target, previous, _tail, records = ledger._read_active_journal()
    aggregate = ledger._aggregate_journal_cycles(records)
    chunk_bytes = capture.canonical_json_bytes(aggregate) + b"\n"
    expected_entry = ledger._expected_manifest_entry(
        aggregate,
        chunk_bytes,
        sequence=target,
        previous_hash=previous,
    )
    orphan = ledger.root.joinpath(*Path(str(expected_entry["chunk_path"])).parts)
    capture._atomic_write_new(orphan, chunk_bytes)
    ledger.writer_lock.release()

    resumed_lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    resumed = capture.ManifestLedger(tmp_path, writer_lock=resumed_lock)

    assert len(resumed.entries) == 2
    assert resumed.entries[-1] == expected_entry
    assert not resumed.journal_path.exists()
    assert orphan.is_file()
    resumed_lock.release()


def test_data_writer_lock_and_all_collection_parameters_are_exact(
    tmp_path: Path,
) -> None:
    first = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    with pytest.raises(capture.CollectionRefusal, match="data_writer_lock_unavailable"):
        capture.ExclusiveDataWriterLock(tmp_path).acquire()
    with pytest.raises(capture.CollectionRefusal, match="data_writer_lock_required"):
        capture.ManifestLedger(
            tmp_path, writer_lock=capture.ExclusiveDataWriterLock(tmp_path)
        )
    first.release()

    for policy in (
        capture.CollectionPolicy(tick_interval_secs=2.01),
        capture.CollectionPolicy(bar_interval_secs=59.0),
        capture.CollectionPolicy(bar_limit=401),
    ):
        with pytest.raises(
            capture.CollectionRefusal,
            match="collection_policy_must_match_preregistration",
        ):
            policy.validate()

    transport = preserved_cases.FakeBridgeTransport()
    lock = capture.ExclusiveDataWriterLock(tmp_path / "timeout").acquire()
    ledger = capture.ManifestLedger(tmp_path / "timeout", writer_lock=lock)
    with pytest.raises(
        capture.CollectionRefusal, match="http_timeout_must_match_sealed_policy"
    ):
        capture.ProspectiveActivityCollector(
            client=capture.BridgeReadClient(
                base_url="http://127.0.0.1:58710",
                api_key=preserved_cases.API_KEY,
                timeout_secs=4.99,
                transport=transport,
            ),
            ledger=ledger,
            binding=preserved_cases._binding(),
        )
    lock.release()


def test_capture_refuses_before_t0_and_when_end_deadline_is_insufficient(
    tmp_path: Path,
) -> None:
    transport = preserved_cases.FakeBridgeTransport()
    future_binding = preserved_cases._binding(t0_epoch=NOW + 60.0)
    future, future_ledger, _clock = _collector(
        tmp_path / "future",
        transport,
        clock=preserved_cases.ManualClock(NOW),
        binding=future_binding,
    )
    with pytest.raises(capture.CollectionRefusal, match="window_not_started"):
        future.capture_cycle(include_bars=False)
    assert transport.calls == []
    future_ledger.writer_lock.release()

    end = NOW + 10.0
    closing_binding = preserved_cases._binding(
        t0_epoch=end - capture.PROSPECTIVE_WINDOW_DAYS * 86_400.0,
        end_epoch=end,
    )
    closing_transport = preserved_cases.FakeBridgeTransport()
    closing, closing_ledger, _clock = _collector(
        tmp_path / "closing",
        closing_transport,
        clock=preserved_cases.ManualClock(NOW),
        binding=closing_binding,
    )
    with pytest.raises(capture.CollectionRefusal, match="deadline_insufficient"):
        closing.capture_cycle(include_bars=False)
    assert closing_transport.calls == []
    closing_ledger.writer_lock.release()


def test_sealer_and_collector_share_exact_persistence_contract() -> None:
    source_sha = capture.collector_source_sha256()
    assert capture.expected_capture_integrity_contract() == (
        replacement_sealer.capture_integrity_contract(source_sha)
    )


def test_continuity_health_uses_fresh_active_journal_between_hourly_segments(
    tmp_path: Path,
) -> None:
    preregistration = _write_preregistration(
        tmp_path / "preregistration.json", _preregistration_payload()
    )
    binding = capture.load_preregistration(preregistration)
    api_key = tmp_path / "api-key.txt"
    api_key.write_text("journal-health-secret\n", encoding="utf-8")
    output = tmp_path / "capture"
    source = preserved_cases._source()
    collector, ledger, clock = _collector(
        output,
        _three_cycle_transport(source),
        binding=binding,
    )
    collector.capture_cycle(include_bars=False)
    clock.now += 1.0
    collector.capture_cycle(include_bars=False)

    report = continuity.inspect_continuity(
        preregistration=preregistration,
        output_dir=output,
        api_key_file=api_key,
        base_url="http://127.0.0.1:58710",
        policy=continuity.GuardPolicy(
            tick_interval_secs=2.0,
            bar_interval_secs=60.0,
            bar_limit=400,
            http_timeout_secs=5.0,
        ),
    )

    assert report["manifest_sequence"] == 1
    assert report["active_journal_present"] is True
    assert report["active_journal_sequence"] == 1
    assert report["active_journal_manifest_target"] == 2
    assert (
        report["collector_activity_last_write_epoch"]
        == (report["active_journal_last_write_epoch"])
    )
    ledger.writer_lock.release()
