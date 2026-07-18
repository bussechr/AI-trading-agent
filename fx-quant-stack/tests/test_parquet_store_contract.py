from __future__ import annotations

import os
from pathlib import Path
import threading

import pandas as pd
import pytest

from fxstack.io.parquet_store import ParquetStore


def _rows(ts: list[object], *, dates: list[str] | None = None) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "pair": ["EURUSD"] * len(ts),
            "ts": ts,
            "timeframe": ["M5"] * len(ts),
            "mid_close": [1.1 + (idx * 0.0001) for idx in range(len(ts))],
        }
    )
    if dates is not None:
        frame["date"] = dates
    return frame


def test_write_uses_utc_timestamp_for_partitions_and_bounded_reads(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    frame = _rows(
        ["2024-01-02T00:00:00Z", "2024-01-03T00:00:00+00:00"],
        dates=["1999-12-31", "1999-12-31"],
    )

    store.write_partitioned(frame, provider="dukascopy", pair="EURUSD", timeframe="M5")

    base = tmp_path / "provider=dukascopy" / "pair=EURUSD" / "timeframe=M5"
    assert not (base / "date=1999-12-31").exists()
    assert (base / "date=2024-01-02" / "bars.parquet").exists()
    assert (base / "date=2024-01-03" / "bars.parquet").exists()
    bounded = store.read_pair_timeframe(
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
        start_ts="2024-01-03T00:00:00Z",
        end_ts="2024-01-03T00:00:00Z",
    )
    assert len(bounded) == 1
    assert bounded.iloc[0]["date"] == "2024-01-03"
    assert bounded.iloc[0]["ts"] == pd.Timestamp("2024-01-03T00:00:00Z")


def test_equivalent_instants_are_deduplicated_after_utc_normalization(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    frame = _rows(
        ["2024-01-02T00:00:00Z", "2024-01-01T19:00:00-05:00"],
        dates=["2024-01-02", "2024-01-01"],
    )

    store.write_partitioned(frame, provider="dukascopy", pair="EURUSD", timeframe="M5")

    out = store.read_pair_timeframe(provider="dukascopy", pair="EURUSD", timeframe="M5")
    assert len(out) == 1
    assert out.iloc[0]["ts"] == pd.Timestamp("2024-01-02T00:00:00Z")
    assert out.iloc[0]["date"] == "2024-01-02"
    assert out.iloc[0]["mid_close"] == pytest.approx(1.1001)


def test_write_rejects_invalid_timestamps_instead_of_creating_bad_partitions(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)

    with pytest.raises(ValueError, match="invalid UTC timestamp"):
        store.write_partitioned(
            _rows(["2024-01-02T00:00:00Z", "not-a-timestamp"]),
            provider="dukascopy",
            pair="EURUSD",
            timeframe="M5",
        )

    assert not list(tmp_path.rglob("bars.parquet"))


def test_replace_partitioned_purges_rows_omitted_by_new_snapshot(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    legacy = _rows(["2024-01-02T00:00:00Z", "2024-01-03T00:00:00Z"])
    legacy["context_frame_profile"] = "hierarchical_v1"
    store.write_partitioned(
        legacy,
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )

    replacement = _rows(["2024-01-03T00:00:00Z"])
    replacement["context_frame_profile"] = "hierarchical_v2"
    store.replace_partitioned(
        replacement,
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )

    out = store.read_pair_timeframe(
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    assert out["ts"].tolist() == [pd.Timestamp("2024-01-03T00:00:00Z")]
    assert out["context_frame_profile"].tolist() == ["hierarchical_v2"]
    base = tmp_path / "provider=dukascopy" / "pair=EURUSD" / "timeframe=M5"
    assert not (base / "date=2024-01-02").exists()


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("provider", {"provider": "safe/../provider=dukascopy"}),
        ("pair", {"pair": "SAFE/../pair=EURUSD"}),
        ("timeframe", {"timeframe": "safe/../timeframe=M5"}),
        ("pair", {"pair": ".."}),
    ],
)
def test_replace_partitioned_rejects_cross_scope_identifier_traversal(
    tmp_path: Path,
    field: str,
    overrides: dict[str, str],
) -> None:
    store = ParquetStore(tmp_path)
    victim = _rows(["2024-01-02T00:00:00Z"])
    store.write_partitioned(
        victim,
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    replacement = victim.copy()
    replacement["mid_close"] = 9.9
    identifiers = {
        "provider": "dukascopy",
        "pair": "EURUSD",
        "timeframe": "M5",
        **overrides,
    }

    with pytest.raises(ValueError, match=f"invalid partition {field}"):
        store.replace_partitioned(replacement, **identifiers)

    out = store.read_pair_timeframe(
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    assert out["mid_close"].tolist() == victim["mid_close"].tolist()


def test_read_recovers_snapshot_left_in_interrupted_swap_backup(tmp_path: Path) -> None:
    writer = ParquetStore(tmp_path)
    frame = _rows(["2024-01-02T00:00:00Z", "2024-01-03T00:00:00Z"])
    writer.write_partitioned(
        frame,
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    target = tmp_path / "provider=dukascopy" / "pair=EURUSD" / "timeframe=M5"
    backup = target.parent / ".timeframe=M5.backup-interrupted"
    target.replace(backup)
    assert not target.exists()

    recovered = ParquetStore(tmp_path).read_pair_timeframe(
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )

    assert recovered["ts"].tolist() == [
        pd.Timestamp("2024-01-02T00:00:00Z"),
        pd.Timestamp("2024-01-03T00:00:00Z"),
    ]
    assert target.exists()
    assert not backup.exists()


def test_concurrent_distinct_upserts_do_not_lose_rows(tmp_path: Path) -> None:
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def _write(ts: str, mid_close: float) -> None:
        try:
            row = _rows([ts])
            row["mid_close"] = mid_close
            start.wait(timeout=5.0)
            ParquetStore(tmp_path).write_partitioned(
                row,
                provider="dukascopy",
                pair="EURUSD",
                timeframe="M5",
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(
            target=_write,
            args=("2024-01-02T00:00:00Z", 1.1),
        ),
        threading.Thread(
            target=_write,
            args=("2024-01-02T00:05:00Z", 1.2),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    store = ParquetStore(tmp_path)
    out = store.read_pair_timeframe(
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    contract = store.source_contract(
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    assert out["ts"].tolist() == [
        pd.Timestamp("2024-01-02T00:00:00Z"),
        pd.Timestamp("2024-01-02T00:05:00Z"),
    ]
    assert contract["generation"] == 2


def test_failed_pending_write_preserves_prior_partition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ParquetStore(tmp_path)
    original = _rows(["2024-01-02T00:00:00Z"])
    store.write_partitioned(
        original,
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    partition = (
        tmp_path
        / "provider=dukascopy"
        / "pair=EURUSD"
        / "timeframe=M5"
        / "date=2024-01-02"
        / "bars.parquet"
    )
    prior_bytes = partition.read_bytes()
    prior_contract = store.source_contract(
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )

    def _fail_to_parquet(
        _frame: pd.DataFrame,
        path: object,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        Path(path).write_bytes(b"partial pending parquet")
        raise OSError("injected parquet serialization failure")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", _fail_to_parquet)
    revised = original.copy()
    revised["mid_close"] = 9.9

    with pytest.raises(OSError, match="injected parquet serialization failure"):
        store.write_partitioned(
            revised,
            provider="dukascopy",
            pair="EURUSD",
            timeframe="M5",
        )

    assert partition.read_bytes() == prior_bytes
    assert not list(partition.parent.glob(".bars.parquet.tmp-*"))
    after_contract = store.source_contract(
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    assert after_contract["fingerprint"] == prior_contract["fingerprint"]
    assert after_contract["generation"] == prior_contract["generation"]


def test_transient_read_error_fails_closed_without_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ParquetStore(tmp_path)
    store.write_partitioned(
        _rows(["2024-01-02T00:00:00Z"]),
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    partition = (
        tmp_path
        / "provider=dukascopy"
        / "pair=EURUSD"
        / "timeframe=M5"
        / "date=2024-01-02"
        / "bars.parquet"
    )
    monkeypatch.setattr(
        pd,
        "read_parquet",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("transient sharing violation")
        ),
    )

    with pytest.raises(PermissionError, match="transient sharing violation"):
        store.read_pair_timeframe(
            provider="dukascopy",
            pair="EURUSD",
            timeframe="M5",
        )

    assert partition.exists()
    assert not list(partition.parent.glob("*.corrupt.*"))


def test_append_waits_for_scope_replacement_and_is_not_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ParquetStore(tmp_path)
    store.write_partitioned(
        _rows(["2024-01-01T00:00:00Z"]),
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    live_target = tmp_path / "provider=dukascopy" / "pair=EURUSD" / "timeframe=M5"
    replacement = _rows(["2024-01-02T00:00:00Z"])
    appended = _rows(["2024-01-03T00:00:00Z"])
    entered_gap = threading.Event()
    release_gap = threading.Event()
    append_done = threading.Event()
    errors: list[BaseException] = []
    original_replace = Path.replace

    def _pause_after_live_moves_to_backup(self: Path, target: object) -> Path:
        result = original_replace(self, target)
        target_path = Path(target)
        if self == live_target and target_path.name.startswith(".timeframe=M5.backup-"):
            entered_gap.set()
            if not release_gap.wait(timeout=5.0):
                raise TimeoutError("replacement gap was not released")
        return result

    monkeypatch.setattr(Path, "replace", _pause_after_live_moves_to_backup)

    def _replace() -> None:
        try:
            ParquetStore(tmp_path).replace_partitioned(
                replacement,
                provider="dukascopy",
                pair="EURUSD",
                timeframe="M5",
            )
        except BaseException as exc:
            errors.append(exc)

    def _append() -> None:
        try:
            ParquetStore(tmp_path).write_partitioned(
                appended,
                provider="dukascopy",
                pair="EURUSD",
                timeframe="M5",
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            append_done.set()

    replacement_thread = threading.Thread(target=_replace)
    append_thread = threading.Thread(target=_append)
    replacement_thread.start()
    try:
        assert entered_gap.wait(timeout=5.0)
        append_thread.start()
        assert not append_done.wait(timeout=0.2)
    finally:
        release_gap.set()
    replacement_thread.join(timeout=10.0)
    append_thread.join(timeout=10.0)

    assert errors == []
    assert not replacement_thread.is_alive()
    assert not append_thread.is_alive()
    out = ParquetStore(tmp_path).read_pair_timeframe(
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    assert out["ts"].tolist() == [
        pd.Timestamp("2024-01-02T00:00:00Z"),
        pd.Timestamp("2024-01-03T00:00:00Z"),
    ]


def test_reader_waits_through_scope_swap_and_sees_committed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ParquetStore(tmp_path)
    store.write_partitioned(
        _rows(["2024-01-01T00:00:00Z"]),
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    live_target = tmp_path / "provider=dukascopy" / "pair=EURUSD" / "timeframe=M5"
    replacement = _rows(["2024-01-02T00:00:00Z"])
    entered_gap = threading.Event()
    release_gap = threading.Event()
    reader_done = threading.Event()
    errors: list[BaseException] = []
    read_results: list[pd.DataFrame] = []
    original_replace = Path.replace

    def _pause_after_live_moves_to_backup(self: Path, target: object) -> Path:
        result = original_replace(self, target)
        target_path = Path(target)
        if self == live_target and target_path.name.startswith(".timeframe=M5.backup-"):
            entered_gap.set()
            if not release_gap.wait(timeout=5.0):
                raise TimeoutError("replacement gap was not released")
        return result

    monkeypatch.setattr(Path, "replace", _pause_after_live_moves_to_backup)

    def _replace() -> None:
        try:
            ParquetStore(tmp_path).replace_partitioned(
                replacement,
                provider="dukascopy",
                pair="EURUSD",
                timeframe="M5",
            )
        except BaseException as exc:
            errors.append(exc)

    def _read() -> None:
        try:
            read_results.append(
                ParquetStore(tmp_path).read_pair_timeframe(
                    provider="dukascopy",
                    pair="EURUSD",
                    timeframe="M5",
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            reader_done.set()

    replacement_thread = threading.Thread(target=_replace)
    reader_thread = threading.Thread(target=_read)
    replacement_thread.start()
    try:
        assert entered_gap.wait(timeout=5.0)
        reader_thread.start()
        assert not reader_done.wait(timeout=0.2)
    finally:
        release_gap.set()
    replacement_thread.join(timeout=10.0)
    reader_thread.join(timeout=10.0)

    assert errors == []
    assert len(read_results) == 1
    assert read_results[0]["ts"].tolist() == [
        pd.Timestamp("2024-01-02T00:00:00Z")
    ]


def test_source_contract_is_stable_for_noop_upsert_and_changes_with_data(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    frame = _rows(["2024-01-02T00:00:00Z"])
    store.write_partitioned(frame, provider="dukascopy", pair="EURUSD", timeframe="M5")
    initial = store.source_contract(provider="dukascopy", pair="EURUSD", timeframe="M5")

    store.write_partitioned(frame, provider="dukascopy", pair="EURUSD", timeframe="M5")
    unchanged = store.source_contract(provider="dukascopy", pair="EURUSD", timeframe="M5")

    revised = frame.copy()
    revised["mid_close"] = 1.25
    store.write_partitioned(revised, provider="dukascopy", pair="EURUSD", timeframe="M5")
    changed = store.source_contract(provider="dukascopy", pair="EURUSD", timeframe="M5")

    assert initial["watermark"] == "2024-01-02T00:00:00+00:00"
    assert initial["contract_version"] == "parquet_source_v2"
    assert initial["content_hash_algorithm"] == "sha256_file_bytes_v1"
    assert initial["generation_contract"] == "parquet_scope_generation_v1"
    assert unchanged["generation"] == initial["generation"]
    assert changed["generation"] == initial["generation"] + 1
    assert unchanged["fingerprint"] == initial["fingerprint"]
    assert changed["fingerprint"] != initial["fingerprint"]


def test_scope_generation_detects_same_bytes_restored_after_intervening_commit(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    original = _rows(["2024-01-02T00:00:00Z"])
    store.write_partitioned(
        original,
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    initial = store.source_contract(
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    changed = original.copy()
    changed["mid_close"] = 1.2
    store.write_partitioned(
        changed,
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    store.write_partitioned(
        original,
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )
    restored = store.source_contract(
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )

    assert restored["partitions"][0]["sha256"] == initial["partitions"][0]["sha256"]
    assert restored["generation"] == initial["generation"] + 2
    assert restored["fingerprint"] != initial["fingerprint"]


def test_source_contract_full_and_tail_scopes_cannot_collide(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    store.write_partitioned(
        _rows(["2024-01-02T00:00:00Z"]),
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
    )

    full = store.source_contract(provider="dukascopy", pair="EURUSD", timeframe="M5")
    tail = store.source_contract(
        provider="dukascopy",
        pair="EURUSD",
        timeframe="M5",
        tail_files=14,
    )

    assert full["partitions"] == tail["partitions"]
    assert full["partition_scope"] == "all"
    assert tail["partition_scope"] == "tail:14"
    assert full["fingerprint"] != tail["fingerprint"]


def test_source_contract_detects_changed_content_with_forged_size_and_mtime(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    frame = _rows(["2024-01-02T00:00:00Z"])
    store.write_partitioned(frame, provider="dukascopy", pair="EURUSD", timeframe="M5")
    initial = store.source_contract(provider="dukascopy", pair="EURUSD", timeframe="M5")
    partition = (
        tmp_path
        / "provider=dukascopy"
        / "pair=EURUSD"
        / "timeframe=M5"
        / "date=2024-01-02"
        / "bars.parquet"
    )
    original_stat = partition.stat()

    revised = frame.copy()
    revised["mid_close"] = 1.2
    revised["ts"] = pd.to_datetime(revised["ts"], utc=True)
    revised["date"] = "2024-01-02"
    revised.to_parquet(partition, index=False)
    assert partition.stat().st_size == original_stat.st_size
    os.utime(
        partition,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    changed = store.source_contract(provider="dukascopy", pair="EURUSD", timeframe="M5")

    assert changed["watermark"] == initial["watermark"]
    assert changed["partitions"][0]["size"] == initial["partitions"][0]["size"]
    assert changed["partitions"][0]["sha256"] != initial["partitions"][0]["sha256"]
    assert changed["fingerprint"] != initial["fingerprint"]
