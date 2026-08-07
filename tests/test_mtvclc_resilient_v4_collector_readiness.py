from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from tools import capture_ig_mt4_m1_activity_resilient_v4 as capture


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "tests" / "test_mtvclc_resilient_v3_collector.py"
_HELPER_NAME = "_mtvclc_v5_readiness_test_helpers"
_spec = importlib.util.spec_from_file_location(_HELPER_NAME, HELPER_PATH)
assert _spec is not None and _spec.loader is not None
helpers = importlib.util.module_from_spec(_spec)
sys.modules[_HELPER_NAME] = helpers
_spec.loader.exec_module(helpers)
helpers.capture = capture

NOW = helpers.NOW


def _sleep_with_clock(
    monkeypatch: pytest.MonkeyPatch,
    clock: Any,
) -> list[float]:
    calls: list[float] = []

    def advance(seconds: float) -> None:
        calls.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(
        capture._implementation,
        "_start_edge_readiness_sleep",
        advance,
    )
    return calls


def _first_chunk(ledger: Any) -> dict[str, Any]:
    assert ledger.entries
    entry = ledger.entries[0]
    return json.loads(
        (ledger.root / entry["chunk_path"]).read_text(encoding="utf-8")
    )


def test_pre_t0_snapshot_retries_once_under_same_durable_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = helpers.preserved_cases._source()
    clock = helpers.preserved_cases.ManualClock(NOW + 0.25)
    transport = helpers.preserved_cases.FakeBridgeTransport(
        states=[
            helpers.preserved_cases._state(source, now=NOW + 0.25),
            helpers.preserved_cases._state(source, now=NOW + 1.25),
            helpers.preserved_cases._state(source, now=NOW + 1.25),
        ],
        ticks=[
            helpers.preserved_cases._ticks(
                source,
                received_at=NOW - 0.25,
                token_epoch=int(NOW - 1),
                event_sequence=7,
            ),
            helpers.preserved_cases._ticks(
                source,
                received_at=NOW + 1.0,
                token_epoch=int(NOW + 1),
                event_sequence=8,
            ),
        ],
        bar_rounds=[helpers._bar_round(source)],
    )
    witnesses: list[tuple[str, str, bool]] = []

    class WitnessTransport:
        def __call__(
            self,
            request: Any,
            timeout_secs: float,
            maximum_bytes: int,
        ) -> tuple[int, bytes]:
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
            return transport(request, timeout_secs, maximum_bytes)

    collector, ledger = helpers._collector(
        tmp_path,
        WitnessTransport(),  # type: ignore[arg-type]
        clock=clock,
    )
    sleeps = _sleep_with_clock(monkeypatch, clock)
    try:
        collector.capture_cycle(include_bars=True)
        chunk = _first_chunk(ledger)
        proof = chunk["start_edge_readiness_proof"]

        assert sleeps == [1.0]
        assert proof == {
            "schema_version": capture.START_EDGE_READINESS_PROOF_SCHEMA_VERSION,
            "first_cycle": True,
            "poll_count": 2,
            "discarded_pre_t0_snapshot_count": 1,
            "first_poll_started_at_epoch": NOW + 0.25,
            "qualifying_poll_completed_at_epoch": NOW + 1.25,
            "retry_seconds": 1.0,
            "same_reservation_for_every_poll": True,
            "first_qualifying_snapshot_selected": True,
            "pre_t0_snapshots_persisted": False,
        }
        assert len(chunk["quotes"]) == len(capture.SYMBOLS) == 22
        assert {
            row["market_event_sequence"] for row in chunk["quotes"]
        } == {8}
        assert min(
            row["transport_received_at_epoch"] for row in chunk["quotes"]
        ) >= NOW
        paths = [row["path"] for row in transport.calls]
        tick_indexes = [
            index for index, path in enumerate(paths) if path == "/v2/market/ticks"
        ]
        first_bar_index = next(
            index for index, path in enumerate(paths) if path == "/v2/market/bars"
        )
        assert len(tick_indexes) == 2
        assert first_bar_index > tick_indexes[-1]
        reservation_hashes = {value for _url, value, _idle in witnesses}
        assert len(reservation_hashes) == 1
        assert reservation_hashes != {capture.ZERO_SHA256}
        assert all(idle for _url, _value, idle in witnesses)
        tail_records = [
            json.loads(line)
            for line in (tmp_path / capture.TAIL_COMMITMENT_FILENAME)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert sum(
            row.get("operation_kind") == "cycle_reserve"
            and row.get("phase") == "event"
            for row in tail_records
        ) == 1
    finally:
        ledger.writer_lock.release()


def test_source_drift_during_readiness_is_terminal_without_second_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_source = helpers.preserved_cases._source(suffix="a")
    drifted_source = helpers.preserved_cases._source(suffix="b")
    clock = helpers.preserved_cases.ManualClock(NOW + 0.25)
    transport = helpers.preserved_cases.FakeBridgeTransport(
        states=[
            helpers.preserved_cases._state(first_source, now=NOW + 0.25),
            helpers.preserved_cases._state(drifted_source, now=NOW + 1.25),
        ],
        ticks=[
            helpers.preserved_cases._ticks(
                first_source,
                received_at=NOW - 0.25,
                token_epoch=int(NOW - 1),
            )
        ],
    )
    collector, ledger = helpers._collector(tmp_path, transport, clock=clock)
    sleeps = _sleep_with_clock(monkeypatch, clock)
    try:
        with pytest.raises(
            capture.CollectionRefusal,
            match="market_source_rollover_during_start_edge_readiness",
        ):
            collector.capture_cycle(include_bars=True)
        assert sleeps == [1.0]
        assert [row["path"] for row in transport.calls].count(
            "/v2/market/ticks"
        ) == 1
        assert transport.bar_calls == 0
        assert not ledger.entries
        assert not ledger.manifest_path.exists()
    finally:
        ledger.writer_lock.release()


def test_non_time_tick_validation_failure_is_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = helpers.preserved_cases._source()
    invalid_ticks = copy.deepcopy(
        helpers.preserved_cases._ticks(
            source,
            received_at=NOW - 0.25,
            token_epoch=int(NOW - 1),
        )
    )
    invalid_ticks.pop(capture.SYMBOLS[-1])
    clock = helpers.preserved_cases.ManualClock(NOW + 0.25)
    transport = helpers.preserved_cases.FakeBridgeTransport(
        states=[helpers.preserved_cases._state(source, now=NOW + 0.25)],
        ticks=[invalid_ticks],
    )
    collector, ledger = helpers._collector(tmp_path, transport, clock=clock)
    sleeps = _sleep_with_clock(monkeypatch, clock)
    try:
        with pytest.raises(capture.CollectionRefusal, match="tick_scope_incomplete"):
            collector.capture_cycle(include_bars=True)
        assert sleeps == []
        assert [row["path"] for row in transport.calls].count(
            "/v2/market/ticks"
        ) == 1
        assert transport.bar_calls == 0
        assert not ledger.entries
    finally:
        ledger.writer_lock.release()


def test_never_eligible_snapshot_fails_at_deadline_without_bars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = helpers.preserved_cases._source()
    clock = helpers.preserved_cases.ManualClock(NOW + 0.25)
    state = helpers.preserved_cases._state(source, now=NOW + 0.25)
    stale = helpers.preserved_cases._ticks(
        source,
        received_at=NOW - 0.25,
        token_epoch=int(NOW - 1),
    )
    transport = helpers.preserved_cases.FakeBridgeTransport(
        states=[state] * 30,
        ticks=[stale] * 30,
    )
    collector, ledger = helpers._collector(tmp_path, transport, clock=clock)
    sleeps = _sleep_with_clock(monkeypatch, clock)
    try:
        with pytest.raises(
            capture.CollectionRefusal,
            match="prospective_window_quote_start_edge_missed",
        ):
            collector.capture_cycle(include_bars=True)
        assert sleeps == [1.0] * 30
        assert clock.now == NOW + 30.25
        assert transport.bar_calls == 0
        assert not ledger.entries
        assert not ledger.manifest_path.exists()
    finally:
        ledger.writer_lock.release()


def test_near_deadline_qualifying_quotes_still_fail_if_bar_cycle_finishes_late(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = helpers.preserved_cases._source()
    clock = helpers.preserved_cases.ManualClock(NOW + 29.0)
    inner = helpers.preserved_cases.FakeBridgeTransport(
        states=[
            helpers.preserved_cases._state(source, now=NOW + 29.0),
            helpers.preserved_cases._state(source, now=NOW + 31.2),
        ],
        ticks=[
            helpers.preserved_cases._ticks(
                source,
                received_at=NOW + 29.0,
                token_epoch=int(NOW + 29),
            )
        ],
        bar_rounds=[helpers._bar_round(source)],
    )

    class SlowBarTransport:
        def __call__(
            self,
            request: Any,
            timeout_secs: float,
            maximum_bytes: int,
        ) -> tuple[int, bytes]:
            result = inner(request, timeout_secs, maximum_bytes)
            if "/v2/market/bars" in request.full_url:
                clock.now += 0.1
            return result

    collector, ledger = helpers._collector(
        tmp_path,
        SlowBarTransport(),  # type: ignore[arg-type]
        clock=clock,
    )
    sleeps = _sleep_with_clock(monkeypatch, clock)
    try:
        with pytest.raises(
            capture.CollectionRefusal,
            match="prospective_window_first_cycle_work_late",
        ):
            collector.capture_cycle(include_bars=True)
        assert sleeps == []
        assert inner.bar_calls == len(capture.SYMBOLS) == 22
        assert clock.now > NOW + 30.0
        assert not ledger.entries
        assert not ledger.manifest_path.exists()
        assert not (tmp_path / capture.START_EDGE_RECEIPT_FILENAME).exists()
    finally:
        ledger.writer_lock.release()
