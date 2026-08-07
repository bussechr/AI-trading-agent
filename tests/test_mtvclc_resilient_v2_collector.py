from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
import hashlib
import importlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import pytest

from tools import capture_ig_mt4_m1_activity_resilient_v2 as capture


ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = ROOT / "tests"
FXSTACK_SRC = ROOT / "fx-quant-stack" / "src"
for path in (TESTS_ROOT, FXSTACK_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

preserved_cases = importlib.import_module("test_capture_ig_mt4_m1_activity")
from fxstack.scalp import (  # noqa: E402
    screen_mt4_tick_volume_close_location_continuation as frozen_screen,
)


NOW = preserved_cases.NOW
TARGET_SYMBOL = "USDCAD"
GAP_INDEX = 300


def _file_identity(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _preregistration_payload() -> dict[str, Any]:
    original = preserved_cases._preregistration_payload()
    body = {
        key: copy.deepcopy(value)
        for key, value in original.items()
        if key != "preregistration_body_sha256"
    }
    body["capture_integrity_contract"] = capture.expected_capture_integrity_contract()
    identities = body["source_identities"]
    identities["collector_source"] = _file_identity(capture.TOOL_PATH)
    identities["collector_support_source"] = _file_identity(capture.SUPPORT_PATH)
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


def _binding(*, t0_epoch: float = NOW) -> capture.ProspectiveBinding:
    end = t0_epoch + capture.PROSPECTIVE_WINDOW_DAYS * 86_400.0
    return capture.ProspectiveBinding(
        preregistration_body_sha256="1" * 64,
        preregistration_artifact_sha256="2" * 64,
        t0_utc=datetime.fromtimestamp(t0_epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_utc_exclusive=datetime.fromtimestamp(end, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        t0_epoch=t0_epoch,
        end_epoch_exclusive=end,
    )


def _collector(
    output: Path,
    transport: preserved_cases.FakeBridgeTransport,
    *,
    clock: preserved_cases.ManualClock,
    binding: capture.ProspectiveBinding,
) -> tuple[capture.ProspectiveActivityCollector, capture.ManifestLedger]:
    writer_lock = capture.ExclusiveDataWriterLock(output).acquire()
    ledger = capture.ManifestLedger(output, writer_lock=writer_lock)
    gap_ledger = capture.LateGapLedger(
        output,
        binding=binding,
        writer_lock=writer_lock,
        last_bar_epoch_by_symbol=ledger.last_bar_epoch_by_symbol,
        bar_epoch_coverage_by_symbol=ledger.bar_epoch_coverage_by_symbol,
    )
    collector = capture.ProspectiveActivityCollector(
        client=capture.BridgeReadClient(
            base_url="http://127.0.0.1:58710",
            api_key=preserved_cases.API_KEY,
            timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
            transport=transport,
        ),
        ledger=ledger,
        gap_ledger=gap_ledger,
        binding=binding,
        policy=capture.CollectionPolicy(),
        clock=clock,
    )
    return collector, ledger


def _shift_rows(rows: list[dict[str, Any]], *, start: int, minutes: int) -> None:
    for row in rows[start:]:
        parsed = datetime.fromisoformat(str(row["time"]))
        row["time"] = (parsed + timedelta(minutes=minutes)).isoformat()


def _rounds_with_one_late_gap(
    source: Any,
) -> tuple[dict[str, Any], dict[str, Any], int, int, int]:
    original = preserved_cases._bar_round(
        source,
        count=capture.DEFAULT_BAR_LIMIT,
        observed_at=NOW,
    )
    gapped = copy.deepcopy(original)
    _shift_rows(gapped[TARGET_SYMBOL]["bars"], start=GAP_INDEX, minutes=1)
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
    covered_tail = copy.deepcopy(gapped[TARGET_SYMBOL]["bars"][-1])
    fresh_tail = copy.deepcopy(covered_tail)
    fresh_time = datetime.fromisoformat(str(fresh_tail["time"])) + timedelta(minutes=1)
    fresh_tail["time"] = fresh_time.isoformat()
    fresh_tail["volume"] += 1
    target_rows.extend((covered_tail, fresh_tail))
    later[TARGET_SYMBOL]["bars"] = target_rows

    covered_row = gapped[TARGET_SYMBOL]["bars"][GAP_INDEX + 10]
    covered_epoch = int(datetime.fromisoformat(str(covered_row["time"])).timestamp())
    first_volume = int(covered_row["volume"])
    for row in later[TARGET_SYMBOL]["bars"]:
        if int(datetime.fromisoformat(str(row["time"])).timestamp()) == covered_epoch:
            row["volume"] = first_volume + 999
            break
    return gapped, later, missing_epoch, covered_epoch, first_volume


def _transport(
    source: Any,
    *,
    now: float,
    bar_round: Mapping[str, Mapping[str, Any]],
    event_sequence: int,
) -> preserved_cases.FakeBridgeTransport:
    return preserved_cases.FakeBridgeTransport(
        states=[preserved_cases._state(source, now=now)] * 2,
        ticks=[
            preserved_cases._ticks(
                source,
                received_at=now,
                token_epoch=int(now) - 1,
                event_sequence=event_sequence,
            )
        ],
        bar_rounds=[bar_round],
    )


def _chunk(ledger: capture.ManifestLedger, index: int) -> dict[str, Any]:
    entry = ledger.entries[index]
    return json.loads((ledger.root / entry["chunk_path"]).read_text(encoding="utf-8"))


def _gap_records(output: Path) -> list[dict[str, Any]]:
    path = output / capture.LATE_GAP_LEDGER_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_preregistration_binds_explicit_gap_and_start_edge_contract(
    tmp_path: Path,
) -> None:
    payload = _preregistration_payload()
    path = _write_preregistration(tmp_path / "prereg.json", payload)

    binding = capture.load_preregistration(path)

    assert binding.preregistration_body_sha256 == payload["preregistration_body_sha256"]
    contract = payload["capture_integrity_contract"]
    assert contract == capture.expected_capture_integrity_contract()
    assert contract["late_unseen_epoch_is_never_backfilled"] is True
    assert contract["late_unseen_epoch_is_never_baseline_eligible"] is True
    assert contract["late_gap_event_is_fsynced_hash_chained_and_immutable"] is True
    assert contract["maximum_start_edge_lag_seconds"] == 30.0


def test_restart_declares_late_unseen_gap_keeps_first_rows_and_continues(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    gapped, later, missing_epoch, covered_epoch, first_volume = (
        _rounds_with_one_late_gap(source)
    )
    binding = _binding()
    first, first_ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bar_round=gapped, event_sequence=7),
        clock=preserved_cases.ManualClock(NOW),
        binding=binding,
    )
    first.capture_cycle(include_bars=True)
    first_chunk_path = first_ledger.root / first_ledger.entries[0]["chunk_path"]
    first_chunk_bytes = first_chunk_path.read_bytes()
    first_ledger.writer_lock.release()

    resumed, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW + 180.0, bar_round=later, event_sequence=8),
        clock=preserved_cases.ManualClock(NOW + 180.0),
        binding=binding,
    )
    resumed.capture_cycle(include_bars=True)
    ledger.finalize_active()

    records = _gap_records(tmp_path)
    assert len(records) == 1
    assert records[0]["previous_gap_entry_sha256"] == "0" * 64
    assert records[0]["gap_entry_sha256"] == capture.canonical_sha256(
        {key: value for key, value in records[0].items() if key != "gap_entry_sha256"}
    )
    assert records[0]["events"] == [
        {
            "symbol": TARGET_SYMBOL,
            "minute_epoch": missing_epoch,
            "watermark_epoch": first_ledger.last_bar_epoch_by_symbol[TARGET_SYMBOL],
            "late_payload_sha256": records[0]["events"][0]["late_payload_sha256"],
            "disposition": "permanent_gap_not_backfilled",
            "baseline_eligible": False,
        }
    ]
    assert first_chunk_path.read_bytes() == first_chunk_bytes

    all_bars = [
        bar
        for index in range(len(ledger.entries))
        for bar in _chunk(ledger, index)["bars"]
    ]
    target_bars = [bar for bar in all_bars if bar["symbol"] == TARGET_SYMBOL]
    assert all(bar["minute_epoch"] != missing_epoch for bar in target_bars)
    covered = [bar for bar in target_bars if bar["minute_epoch"] == covered_epoch]
    assert len(covered) == 1
    assert covered[0]["tick_volume"] == first_volume
    assert (
        ledger.last_bar_epoch_by_symbol[TARGET_SYMBOL]
        > (records[0]["events"][0]["watermark_epoch"])
    )
    ledger.writer_lock.release()


def test_declared_gap_never_becomes_retroactive_baseline_input(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    gapped, later, missing_epoch, _covered_epoch, _first_volume = (
        _rounds_with_one_late_gap(source)
    )
    binding = _binding()
    first, first_ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bar_round=gapped, event_sequence=7),
        clock=preserved_cases.ManualClock(NOW),
        binding=binding,
    )
    first.capture_cycle(include_bars=True)
    first_ledger.writer_lock.release()
    resumed, ledger = _collector(
        tmp_path,
        _transport(source, now=NOW + 180.0, bar_round=later, event_sequence=8),
        clock=preserved_cases.ManualClock(NOW + 180.0),
        binding=binding,
    )
    resumed.capture_cycle(include_bars=True)
    ledger.finalize_active()

    bars = [
        frozen_screen.MT4BidBar(
            epoch=int(row["minute_epoch"]),
            bid_open=float(row["bid_open"]),
            bid_high=float(row["bid_high"]),
            bid_low=float(row["bid_low"]),
            bid_close=float(row["bid_close"]),
            tick_volume=int(row["tick_volume"]),
        )
        for index in range(len(ledger.entries))
        for row in _chunk(ledger, index)["bars"]
        if row["symbol"] == TARGET_SYMBOL
    ]
    prepared = frozen_screen.prepare_bars(bars)

    assert all(bar.epoch != missing_epoch for bar in prepared)
    assert (
        frozen_screen.baseline_volume_v90(
            prepared,
            signal_index=len(prepared) - 1,
        )
        is None
    )
    assert _gap_records(tmp_path)[0]["events"][0]["baseline_eligible"] is False
    ledger.writer_lock.release()


def test_restart_recovers_journal_and_does_not_duplicate_gap_event(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    gapped, later, missing_epoch, _covered_epoch, _first_volume = (
        _rounds_with_one_late_gap(source)
    )
    binding = _binding()
    first, first_ledger = _collector(
        tmp_path,
        _transport(source, now=NOW, bar_round=gapped, event_sequence=7),
        clock=preserved_cases.ManualClock(NOW),
        binding=binding,
    )
    first.capture_cycle(include_bars=True)
    first_ledger.writer_lock.release()

    second, second_ledger = _collector(
        tmp_path,
        _transport(source, now=NOW + 180.0, bar_round=later, event_sequence=8),
        clock=preserved_cases.ManualClock(NOW + 180.0),
        binding=binding,
    )
    second.capture_cycle(include_bars=True)
    assert second_ledger.journal_path.is_file()
    assert len(_gap_records(tmp_path)) == 1
    second_ledger.writer_lock.release()

    writer_lock = capture.ExclusiveDataWriterLock(tmp_path).acquire()
    recovered = capture.ManifestLedger(tmp_path, writer_lock=writer_lock)
    recovered_gaps = capture.LateGapLedger(
        tmp_path,
        binding=binding,
        writer_lock=writer_lock,
        last_bar_epoch_by_symbol=recovered.last_bar_epoch_by_symbol,
        bar_epoch_coverage_by_symbol=recovered.bar_epoch_coverage_by_symbol,
    )

    assert len(recovered.entries) == 2
    assert not recovered.journal_path.exists()
    assert recovered_gaps.sequence == 1
    assert recovered_gaps.contains(TARGET_SYMBOL, missing_epoch)
    assert len(_gap_records(tmp_path)) == 1

    third_transport = _transport(
        source,
        now=NOW + 181.0,
        bar_round=later,
        event_sequence=9,
    )
    third = capture.ProspectiveActivityCollector(
        client=capture.BridgeReadClient(
            base_url="http://127.0.0.1:58710",
            api_key=preserved_cases.API_KEY,
            timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
            transport=third_transport,
        ),
        ledger=recovered,
        gap_ledger=recovered_gaps,
        binding=binding,
        policy=capture.CollectionPolicy(),
        clock=preserved_cases.ManualClock(NOW + 181.0),
    )
    third.capture_cycle(include_bars=True)
    assert recovered_gaps.sequence == 1
    assert len(_gap_records(tmp_path)) == 1
    writer_lock.release()


def test_pristine_start_edge_accepts_30_seconds_and_refuses_any_later(
    tmp_path: Path,
) -> None:
    source = preserved_cases._source()
    binding = _binding()
    accepted_now = binding.t0_epoch + 30.0
    accepted_transport = preserved_cases.FakeBridgeTransport(
        states=[preserved_cases._state(source, now=accepted_now)] * 2,
        ticks=[
            preserved_cases._ticks(
                source,
                received_at=accepted_now,
                token_epoch=int(accepted_now),
                event_sequence=7,
            )
        ],
    )
    accepted, accepted_ledger = _collector(
        tmp_path / "accepted",
        accepted_transport,
        clock=preserved_cases.ManualClock(accepted_now),
        binding=binding,
    )

    accepted.capture_cycle(include_bars=False)

    assert len(accepted_ledger.entries) == 1
    accepted_ledger.writer_lock.release()

    refused_now = binding.t0_epoch + 30.001
    refused_transport = preserved_cases.FakeBridgeTransport()
    refused, refused_ledger = _collector(
        tmp_path / "refused",
        refused_transport,
        clock=preserved_cases.ManualClock(refused_now),
        binding=binding,
    )
    with pytest.raises(capture.CollectionRefusal, match="start_edge_missed"):
        refused.capture_cycle(include_bars=False)
    assert refused_transport.calls == []
    refused_ledger.writer_lock.release()
