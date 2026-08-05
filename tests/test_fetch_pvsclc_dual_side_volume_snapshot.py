from __future__ import annotations

import csv
import hashlib
import inspect
import json
import os
import socket
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tools import fetch_pvsclc_dual_side_volume_snapshot as acquisition


START = datetime(2023, 1, 1, 0, 0, tzinfo=timezone.utc)
END = datetime(2023, 1, 1, 0, 2, tzinfo=timezone.utc)
STARTED = datetime(2026, 8, 3, 4, 12, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 3, 4, 12, 37, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_production_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if (
            key.upper() in acquisition._EXPLICIT_FORBIDDEN_ENV_KEYS
            or key.upper() in acquisition._FORBIDDEN_TRANSPORT_ENV_KEYS
            or acquisition._SENSITIVE_ENV_KEY_RE.fullmatch(key.upper())
            or key in {"FXSTACK_EXECUTION_PROVIDER", "FXSTACK_MARKET_DATA_PROVIDER"}
        ):
            monkeypatch.delenv(key, raising=False)


def _side_frame(side: str = "B") -> pd.DataFrame:
    index = pd.DatetimeIndex(
        [START, START + timedelta(minutes=1)],
        tz="UTC",
        name="timestamp",
    )
    side_offset = 0.0002 if side == "A" else 0.0
    opens = np.array([1.1010, 1.1020]) + side_offset
    volumes = np.array([2.5, 0.0]) if side == "A" else np.array([0.0, 1.25])
    return pd.DataFrame(
        {
            "open": opens,
            "high": opens + 0.0010,
            "low": opens - 0.0010,
            "close": opens + 0.0004,
            "volume": volumes,
        },
        index=index,
    )


class FakeFetch:
    def __init__(
        self,
        mutate: Callable[[str, str, pd.DataFrame, int], pd.DataFrame] | None = None,
        fail_at_call: int | None = None,
    ) -> None:
        self.mutate = mutate
        self.fail_at_call = fail_at_call
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        instrument: str,
        interval: str,
        offer_side: str,
        start: datetime,
        end: datetime,
        *,
        max_retries: int,
        limit: int,
        debug: bool,
    ) -> pd.DataFrame:
        call_index = len(self.calls)
        self.calls.append(
            {
                "instrument": instrument,
                "interval": interval,
                "offer_side": offer_side,
                "start": start,
                "end": end,
                "max_retries": max_retries,
                "limit": limit,
                "debug": debug,
            }
        )
        if self.fail_at_call == call_index:
            raise RuntimeError("synthetic provider failure")
        symbol = instrument.replace("/", "")
        frame = _side_frame(offer_side)
        if self.mutate is not None:
            frame = self.mutate(symbol, offer_side, frame, call_index)
        return frame.copy(deep=True)


def _dependencies(fetch: FakeFetch) -> acquisition.ProviderDependencies:
    return acquisition.ProviderDependencies(
        fetch=fetch,
        resolve_instrument=lambda symbol: (
            f"{symbol[:3]}/{symbol[3:]}",
            f"INSTRUMENT_TEST_{symbol}",
        ),
        interval_m1="1MIN",
        offer_side_bid="B",
        offer_side_ask="A",
        library_version="4.0.1-test",
        library_module_sha256="a" * 64,
        instruments_module_sha256="b" * 64,
        requests_version="2.32.4-test",
        pandas_version=pd.__version__,
        python_version="3.12.10-test",
    )


def _config(target: Path) -> acquisition.AcquisitionConfig:
    return acquisition.AcquisitionConfig(
        target=target,
        start=START,
        end=END,
    )


def _acquire(target: Path, fetch: FakeFetch) -> Path:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            acquisition, "_load_default_dependencies", lambda: _dependencies(fetch)
        )
        clock = iter((STARTED, NOW))
        patch.setattr(acquisition, "_utc_now", lambda: next(clock))
        patch.setattr(acquisition, "MINIMUM_ROWS_PER_SYMBOL", 2)
        return acquisition.acquire_snapshot(_config(target))


def _partial_directories(target: Path) -> list[Path]:
    return sorted(target.parent.glob(f".{target.name}.partial-*"))


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def _side_stream_hash(rows: list[list[str]], side: str) -> str:
    header = "timestamp,open,high,low,close,volume\n"
    if side == "bid":
        indices = (0, 1, 2, 3, 4, 9)
    else:
        indices = (0, 5, 6, 7, 8, 10)
    body = "".join(",".join(row[index] for index in indices) + "\n" for row in rows[1:])
    return _sha256((header + body).encode("utf-8"))


def test_canonical_snapshot_preserves_component_volumes_and_exact_window(
    tmp_path: Path,
) -> None:
    target = tmp_path / "bundle"
    fetch = FakeFetch()

    assert _acquire(target, fetch) == target.resolve()

    assert target.is_dir()
    assert not _partial_directories(target)
    assert {path.name for path in target.iterdir()} == {
        "input",
        "input_sha256.txt",
        "volume_provenance.json",
    }
    input_files = sorted((target / "input").iterdir())
    assert [path.name for path in input_files] == [
        f"{symbol}_M1.csv" for symbol in acquisition.PVSCLC_SYMBOLS
    ]
    assert len(fetch.calls) == 2 * len(acquisition.PVSCLC_SYMBOLS)
    for index, symbol in enumerate(acquisition.PVSCLC_SYMBOLS):
        bid_call, ask_call = fetch.calls[index * 2 : index * 2 + 2]
        assert bid_call == {
            "instrument": f"{symbol[:3]}/{symbol[3:]}",
            "interval": "1MIN",
            "offer_side": "B",
            "start": START,
            "end": END - timedelta(minutes=1),
            "max_retries": 7,
            "limit": 5_000,
            "debug": False,
        }
        assert ask_call == {**bid_call, "offer_side": "A"}

    rows = _read_rows(target / "input" / "EURUSD_M1.csv")
    assert tuple(rows[0]) == acquisition.CSV_HEADER
    assert len(rows) == 3
    assert rows[1][0] == "2023-01-01T00:00:00Z"
    assert rows[2][0] == "2023-01-01T00:01:00Z"
    assert [row[9] for row in rows[1:]] == ["0.0", "1.25"]
    assert [row[10] for row in rows[1:]] == ["2.5", "0.0"]
    assert "volume" not in rows[0]
    assert "total_activity" not in rows[0]


def test_manifest_and_provenance_are_strict_and_independently_replayable(
    tmp_path: Path,
) -> None:
    target = tmp_path / "bundle"
    _acquire(target, FakeFetch())

    manifest_bytes = (target / "input_sha256.txt").read_bytes()
    assert b"\r" not in manifest_bytes
    assert not manifest_bytes.startswith(b"\xef\xbb\xbf")
    lines = manifest_bytes.decode("ascii").splitlines(keepends=True)
    assert len(lines) == 18
    expected_lines: list[str] = []
    for symbol in acquisition.PVSCLC_SYMBOLS:
        relative_path = f"input/{symbol}_M1.csv"
        digest = _sha256((target / "input" / f"{symbol}_M1.csv").read_bytes())
        expected_lines.append(f"{digest}  {relative_path}\n")
    assert lines == sorted(expected_lines)

    provenance_bytes = (target / "volume_provenance.json").read_bytes()
    provenance = json.loads(provenance_bytes)
    assert set(provenance) == {
        "schema_version",
        "activity_metric_id",
        "provider",
        "acquisition_started_at_utc",
        "as_of_utc",
        "window",
        "coverage_contract",
        "request_contract",
        "merge_contract",
        "fill_contract",
        "tool_contract",
        "input_manifest_sha256",
        "symbols",
    }
    assert provenance["schema_version"] == acquisition.PROVENANCE_SCHEMA_VERSION
    assert provenance["activity_metric_id"] == acquisition.ACTIVITY_METRIC_ID
    assert provenance["provider"] == acquisition.PROVIDER_NAME
    assert provenance["acquisition_started_at_utc"] == "2026-08-03T04:12:01Z"
    assert provenance["as_of_utc"] == "2026-08-03T04:12:37Z"
    assert provenance["window"] == {
        "start_inclusive": "2023-01-01T00:00:00Z",
        "end_exclusive": "2023-01-01T00:02:00Z",
    }
    assert provenance["coverage_contract"] == {
        "rows_preserved_without_filtering": True,
        "minimum_rows_per_symbol": 2,
        "maximum_boundary_lag_seconds": 604_800,
        "maximum_gap_seconds": 345_600,
        "timestamp_set_hash_grammar": (
            "sha256_ascii_lf_header_timestamp_then_canonical_rows_v1"
        ),
    }
    assert provenance["request_contract"] == {
        "endpoint": acquisition.DUKASCOPY_ENDPOINT,
        "timeframe": "1MIN",
        "bid_offer_side": "B",
        "ask_offer_side": "A",
        "separate_requests": True,
        "resume": False,
        "max_retries": 7,
        "limit": 5_000,
        "provider_end_inclusive_adjustment_minutes": -1,
        "out_of_window_rows_policy": "reject",
    }
    assert provenance["merge_contract"] == {
        "method": "exact_timestamp_inner_join_with_equal_side_sets",
        "unmatched_bid_rows": 0,
        "unmatched_ask_rows": 0,
    }
    assert provenance["fill_contract"] == {
        "mid_only_fallback": False,
        "zero_fill": False,
        "synthetic_side": False,
    }
    assert provenance["input_manifest_sha256"] == _sha256(manifest_bytes)
    assert tuple(provenance["symbols"]) == acquisition.PVSCLC_SYMBOLS

    symbol_keys = {
        "instrument_id",
        "output_file",
        "output_sha256",
        "rows",
        "bid_stream_sha256",
        "ask_stream_sha256",
        "unmatched_bid_rows",
        "unmatched_ask_rows",
        "bid_volume",
        "ask_volume",
        "first_timestamp",
        "last_timestamp",
        "timestamp_set_sha256",
        "gap_count",
        "maximum_gap_seconds",
    }
    for symbol, expected_line in zip(acquisition.PVSCLC_SYMBOLS, lines, strict=True):
        record = provenance["symbols"][symbol]
        assert set(record) == symbol_keys
        assert record["instrument_id"] == f"{symbol[:3]}/{symbol[3:]}"
        assert record["output_file"] == f"input/{symbol}_M1.csv"
        assert record["output_sha256"] == expected_line[:64]
        assert record["rows"] == 2
        assert record["unmatched_bid_rows"] == 0
        assert record["unmatched_ask_rows"] == 0
        assert record["first_timestamp"] == "2023-01-01T00:00:00Z"
        assert record["last_timestamp"] == "2023-01-01T00:01:00Z"
        timestamp_bytes = b"timestamp\n2023-01-01T00:00:00Z\n2023-01-01T00:01:00Z\n"
        assert record["timestamp_set_sha256"] == _sha256(timestamp_bytes)
        assert record["gap_count"] == 0
        assert record["maximum_gap_seconds"] == 60
        assert record["bid_volume"] == {
            "rows": 2,
            "finite": True,
            "nonnegative": True,
            "missing": 0,
            "zero_count": 1,
        }
        assert record["ask_volume"] == {
            "rows": 2,
            "finite": True,
            "nonnegative": True,
            "missing": 0,
            "zero_count": 1,
        }
        output_rows = _read_rows(target / "input" / f"{symbol}_M1.csv")
        assert record["bid_stream_sha256"] == _side_stream_hash(output_rows, "bid")
        assert record["ask_stream_sha256"] == _side_stream_hash(output_rows, "ask")

    tool = provenance["tool_contract"]
    assert set(tool) == {
        "tool_path",
        "tool_sha256",
        "dukascopy_python_version",
        "dukascopy_python_module_sha256",
        "dukascopy_python_instruments_module_sha256",
        "pandas_version",
        "requests_version",
        "python_version",
    }
    assert tool["tool_path"] == acquisition.TOOL_REPOSITORY_PATH
    assert tool["tool_sha256"] == _sha256(Path(acquisition.__file__).read_bytes())
    assert tool["dukascopy_python_module_sha256"] == "a" * 64
    assert tool["dukascopy_python_instruments_module_sha256"] == "b" * 64


def test_missing_volume_column_fails_without_promotion(tmp_path: Path) -> None:
    def mutate(
        _symbol: str, side: str, frame: pd.DataFrame, _call: int
    ) -> pd.DataFrame:
        return frame.drop(columns="volume") if side == "B" else frame

    target = tmp_path / "bundle"
    with pytest.raises(ValueError, match="expected exact columns"):
        _acquire(target, FakeFetch(mutate))
    assert not target.exists()
    assert len(_partial_directories(target)) == 1


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("volume", np.nan, "missing or non-finite"),
        ("volume", np.inf, "missing or non-finite"),
        ("volume", -0.01, "negative values"),
        ("open", np.nan, "missing or non-finite"),
        ("close", 0.0, "finite and positive"),
    ],
)
def test_invalid_numeric_component_fails_closed(
    tmp_path: Path, column: str, value: float, message: str
) -> None:
    def mutate(
        _symbol: str, side: str, frame: pd.DataFrame, _call: int
    ) -> pd.DataFrame:
        if side == "B":
            frame.iloc[1, frame.columns.get_loc(column)] = value
        return frame

    target = tmp_path / "bundle"
    with pytest.raises(ValueError, match=message):
        _acquire(target, FakeFetch(mutate))
    assert not target.exists()


@pytest.mark.parametrize(
    "kind", ["duplicate", "decreasing", "non_minute", "naive", "non_utc"]
)
def test_invalid_timestamp_stream_fails_closed(tmp_path: Path, kind: str) -> None:
    def mutate(
        _symbol: str, side: str, frame: pd.DataFrame, _call: int
    ) -> pd.DataFrame:
        if side != "B":
            return frame
        timestamps = list(frame.index)
        if kind == "duplicate":
            timestamps[1] = timestamps[0]
        elif kind == "decreasing":
            timestamps[0], timestamps[1] = timestamps[1], timestamps[0]
        elif kind == "non_minute":
            timestamps[1] = timestamps[1] + timedelta(seconds=1)
        elif kind == "naive":
            frame.index = frame.index.tz_localize(None)
            return frame
        elif kind == "non_utc":
            frame.index = frame.index.tz_convert("Europe/Berlin")
            return frame
        frame.index = pd.DatetimeIndex(timestamps, name="timestamp")
        return frame

    target = tmp_path / "bundle"
    expected = {
        "duplicate": "duplicate timestamps",
        "decreasing": "strictly increasing",
        "non_minute": "exact whole minutes",
        "naive": "UTC-aware",
        "non_utc": "timestamps must be UTC",
    }[kind]
    with pytest.raises(ValueError, match=expected):
        _acquire(target, FakeFetch(mutate))
    assert not target.exists()


def test_bid_ask_timestamp_mismatch_fails_with_exact_unmatched_counts(
    tmp_path: Path,
) -> None:
    def mutate(
        _symbol: str, side: str, frame: pd.DataFrame, _call: int
    ) -> pd.DataFrame:
        return frame.drop(frame.index[1]) if side == "A" else frame

    target = tmp_path / "bundle"
    with pytest.raises(
        RuntimeError,
        match=r"unmatched_bid_rows=1, unmatched_ask_rows=0",
    ):
        _acquire(target, FakeFetch(mutate))
    assert not target.exists()


@pytest.mark.parametrize(
    "outside_timestamp",
    [START - timedelta(minutes=1), END],
)
def test_out_of_window_provider_rows_are_rejected_not_cropped(
    tmp_path: Path, outside_timestamp: datetime
) -> None:
    def mutate(
        _symbol: str, side: str, frame: pd.DataFrame, _call: int
    ) -> pd.DataFrame:
        if side != "B":
            return frame
        extra = frame.iloc[[0]].copy()
        extra.index = pd.DatetimeIndex([outside_timestamp], name="timestamp")
        return pd.concat([frame, extra]).sort_index()

    target = tmp_path / "bundle"
    with pytest.raises(ValueError, match="outside the requested window"):
        _acquire(target, FakeFetch(mutate))
    assert not target.exists()


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("high", 1.0),
        ("low", 2.0),
    ],
)
def test_invalid_ohlc_geometry_is_not_repaired(
    tmp_path: Path, column: str, replacement: float
) -> None:
    def mutate(
        _symbol: str, side: str, frame: pd.DataFrame, _call: int
    ) -> pd.DataFrame:
        if side == "B":
            frame.iloc[1, frame.columns.get_loc(column)] = replacement
        return frame

    target = tmp_path / "bundle"
    with pytest.raises(ValueError, match="OHLC geometry is inconsistent"):
        _acquire(target, FakeFetch(mutate))
    assert not target.exists()


def test_cross_side_inversion_is_not_repaired(tmp_path: Path) -> None:
    def mutate(
        _symbol: str, side: str, frame: pd.DataFrame, _call: int
    ) -> pd.DataFrame:
        if side == "A":
            for column in ("open", "high", "low", "close"):
                frame[column] = frame[column] - 0.001
        return frame

    target = tmp_path / "bundle"
    with pytest.raises(ValueError, match="ASK OHLC must be greater"):
        _acquire(target, FakeFetch(mutate))
    assert not target.exists()


def test_component_volumes_cannot_overflow_recomputed_total_activity(
    tmp_path: Path,
) -> None:
    def mutate(
        _symbol: str, _side: str, frame: pd.DataFrame, _call: int
    ) -> pd.DataFrame:
        frame.iloc[0, frame.columns.get_loc("volume")] = 1e308
        return frame

    target = tmp_path / "bundle"
    with pytest.raises(ValueError, match="total activity would be non-finite"):
        _acquire(target, FakeFetch(mutate))
    assert not target.exists()


def test_extra_or_reordered_side_columns_fail_closed(tmp_path: Path) -> None:
    def mutate(
        _symbol: str, side: str, frame: pd.DataFrame, _call: int
    ) -> pd.DataFrame:
        if side == "B":
            frame["unexpected"] = 1.0
        return frame

    target = tmp_path / "bundle"
    with pytest.raises(ValueError, match="expected exact columns"):
        _acquire(target, FakeFetch(mutate))
    assert not target.exists()


def test_relative_existing_and_repository_targets_are_rejected_before_fetch(
    tmp_path: Path,
) -> None:
    fetch = FakeFetch()
    with pytest.raises(ValueError, match="absolute path"):
        acquisition.acquire_snapshot(_config(Path("relative-bundle")))

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        acquisition.acquire_snapshot(_config(existing))

    repo_target = acquisition.REPOSITORY_ROOT / "forbidden-source-bundle"
    assert not repo_target.exists()
    with pytest.raises(ValueError, match="outside the repository"):
        acquisition.acquire_snapshot(_config(repo_target))
    assert not repo_target.exists()
    assert fetch.calls == []


def test_production_environment_is_rejected_before_target_or_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "bundle"
    fetch = FakeFetch()
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "postgresql://production")
    with pytest.raises(RuntimeError, match="FXSTACK_DATABASE_URL"):
        acquisition.acquire_snapshot(_config(target))
    assert fetch.calls == []
    assert not target.exists()
    assert not _partial_directories(target)


@pytest.mark.parametrize(
    "environment",
    [
        {"TRADER_BRIDGE_HOST": "127.0.0.1"},
        {"IG_API_KEY": "secret"},
        {"FXSTACK_REGISTRY_ROOT": "C:/active"},
        {"TRADER_RUNTIME_DB_PATH": "C:/runtime.db"},
        {"FXSTACK_POSTGRES_DSN": "postgresql://production"},
        {"HTTPS_PROXY": "http://proxy.invalid"},
        {"ALL_PROXY": "socks5://proxy.invalid"},
        {"REQUESTS_CA_BUNDLE": "C:/unbound-ca.pem"},
        {"PYTHONPATH": "C:/unbound-imports"},
        {"PYTHONHOME": "C:/unbound-python"},
        {"FXSTACK_EXECUTION_PROVIDER": "mt4"},
        {"FXSTACK_MARKET_DATA_PROVIDER": "ig"},
    ],
)
def test_production_environment_patterns_fail_closed(
    tmp_path: Path, environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = FakeFetch()
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match="public-source acquisition refuses"):
        acquisition.acquire_snapshot(_config(tmp_path / "bundle"))
    assert fetch.calls == []


def test_partial_provider_failure_is_never_promoted_or_deleted(tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    fetch = FakeFetch(fail_at_call=2)
    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        _acquire(target, fetch)

    assert not target.exists()
    partials = _partial_directories(target)
    assert len(partials) == 1
    partial = partials[0]
    assert partial.name == ".bundle.partial-one-shot"
    assert (partial / "input" / "AUDJPY_M1.csv").is_file()
    assert sorted((partial / "input").iterdir()) == [
        partial / "input" / "AUDJPY_M1.csv"
    ]
    assert not (partial / "input_sha256.txt").exists()
    assert not (partial / "volume_provenance.json").exists()


def test_existing_partial_blocks_a_second_attempt_before_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "bundle"
    prior_partial = tmp_path / ".bundle.partial-priorattempt"
    prior_partial.mkdir()
    (prior_partial / "evidence.txt").write_text("preserve", encoding="utf-8")
    fetch = FakeFetch()
    monkeypatch.setattr(
        acquisition, "_load_default_dependencies", lambda: _dependencies(fetch)
    )
    with pytest.raises(FileExistsError, match="prior partial acquisition exists"):
        acquisition.acquire_snapshot(_config(target))
    assert fetch.calls == []
    assert not target.exists()
    assert (prior_partial / "evidence.txt").read_text(encoding="utf-8") == "preserve"
    assert _partial_directories(target) == [prior_partial]


def test_noncanonical_instrument_id_fails_before_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = FakeFetch()
    deps = _dependencies(fetch)
    bad_dependencies = acquisition.ProviderDependencies(
        fetch=deps.fetch,
        resolve_instrument=lambda symbol: (symbol, f"BAD_{symbol}"),
        interval_m1=deps.interval_m1,
        offer_side_bid=deps.offer_side_bid,
        offer_side_ask=deps.offer_side_ask,
        library_version=deps.library_version,
        library_module_sha256=deps.library_module_sha256,
        instruments_module_sha256=deps.instruments_module_sha256,
        requests_version=deps.requests_version,
        pandas_version=deps.pandas_version,
        python_version=deps.python_version,
    )
    monkeypatch.setattr(
        acquisition, "_load_default_dependencies", lambda: bad_dependencies
    )
    target = tmp_path / "bundle"
    with pytest.raises(RuntimeError, match="instrument id must equal AUD/JPY"):
        acquisition.acquire_snapshot(_config(target))
    assert fetch.calls == []
    assert not target.exists()
    assert len(_partial_directories(target)) == 1


def test_tool_source_drift_blocks_provenance_and_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hashes = iter(("b" * 64, "c" * 64))
    monkeypatch.setattr(acquisition, "_sha256_file", lambda _path: next(hashes))
    target = tmp_path / "bundle"
    with pytest.raises(RuntimeError, match="source changed during the fetch"):
        _acquire(target, FakeFetch())
    assert not target.exists()
    partials = _partial_directories(target)
    assert len(partials) == 1
    assert not (partials[0] / "volume_provenance.json").exists()


def test_tool_source_drift_after_provenance_still_blocks_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hashes = iter(("b" * 64, "b" * 64, "c" * 64))
    monkeypatch.setattr(acquisition, "_sha256_file", lambda _path: next(hashes))
    target = tmp_path / "bundle"
    with pytest.raises(RuntimeError, match="source changed before promotion"):
        _acquire(target, FakeFetch())
    assert not target.exists()
    partials = _partial_directories(target)
    assert len(partials) == 1
    assert (partials[0] / "volume_provenance.json").is_file()


def test_manifest_tamper_blocks_promotion_and_leaves_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = acquisition._write_exclusive

    def tampering_write(path: Path, payload: bytes) -> None:
        if path.name == "input_sha256.txt":
            payload += b"\n"
        original(path, payload)

    monkeypatch.setattr(acquisition, "_write_exclusive", tampering_write)
    target = tmp_path / "bundle"
    with pytest.raises(RuntimeError, match="manifest bytes changed"):
        _acquire(target, FakeFetch())
    assert not target.exists()
    assert len(_partial_directories(target)) == 1


def test_atomic_promotion_refuses_a_target_that_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = acquisition._atomic_promote_no_replace
    target = tmp_path / "bundle"

    def racing_promote(source: Path, destination: Path) -> None:
        destination.mkdir()
        (destination / "sentinel.txt").write_text("do not overwrite", encoding="utf-8")
        original(source, destination)

    monkeypatch.setattr(acquisition, "_atomic_promote_no_replace", racing_promote)
    with pytest.raises(FileExistsError, match="appeared before promotion"):
        _acquire(target, FakeFetch())
    assert (target / "sentinel.txt").read_text(encoding="utf-8") == "do not overwrite"
    assert len(_partial_directories(target)) == 1


def test_provenance_validator_rejects_extra_fields(tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    _acquire(target, FakeFetch())
    payload = json.loads(
        (target / "volume_provenance.json").read_text(encoding="utf-8")
    )
    payload["unapproved"] = True
    with pytest.raises(RuntimeError, match="root schema mismatch"):
        acquisition._validate_provenance(payload)

    payload.pop("unapproved")
    payload["acquisition_started_at_utc"] = "2026-08-03T04:12:38Z"
    with pytest.raises(RuntimeError, match="chronology is inverted"):
        acquisition._validate_provenance(payload)

    payload["acquisition_started_at_utc"] = "2026-08-03T04:12:01Z"
    with pytest.raises(RuntimeError, match="coverage contract mismatch"):
        acquisition._validate_provenance(payload)


def test_cli_contract_freezes_intended_window_and_has_no_unsafe_modes(
    tmp_path: Path,
) -> None:
    parser = acquisition.build_parser()
    args = parser.parse_args(
        [
            "--target",
            str(tmp_path / "bundle"),
            "--start",
            "2023-01-01T00:00:00Z",
            "--end",
            "2023-07-01T00:00:00Z",
        ]
    )
    assert args.start == START
    assert args.end == datetime(2023, 7, 1, tzinfo=timezone.utc)
    assert not hasattr(args, "resume")
    assert not hasattr(args, "overwrite")
    assert not hasattr(args, "mid_only_fallback")
    assert not hasattr(args, "pairs")
    assert not hasattr(args, "max_retries")
    assert not hasattr(args, "limit")
    assert acquisition.MAX_RETRIES == 7
    assert acquisition.FETCH_LIMIT == 5_000
    assert acquisition.PROVIDER_END_INCLUSIVE_ADJUSTMENT_MINUTES == -1
    assert acquisition.INTENDED_WSL_TARGET == (
        "/var/tmp/fxscalp-20260803-pvsclc-2023h1-source-v1"
    )
    assert acquisition.INTENDED_WINDOWS_TARGET == (
        r"\\wsl.localhost\Ubuntu-22.04\var\tmp\fxscalp-20260803-pvsclc-2023h1-source-v1"
    )

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--target",
                str(tmp_path / "bundle"),
                "--start",
                "2023-01-01T00:00:01Z",
                "--end",
                "2023-07-01T00:00:00Z",
            ]
        )


def test_production_acquisition_api_has_no_dependency_or_clock_injection() -> None:
    assert tuple(inspect.signature(acquisition.acquire_snapshot).parameters) == (
        "config",
    )
    assert acquisition._canonical_utc_second(NOW) == "2026-08-03T04:12:37Z"
    with pytest.raises(ValueError, match="whole UTC second"):
        acquisition._canonical_utc_second(NOW.replace(microsecond=1))


def test_frozen_production_coverage_policy_accepts_a_dense_h1_timestamp_set() -> None:
    assert acquisition.MINIMUM_ROWS_PER_SYMBOL == 175_000
    assert acquisition.MAXIMUM_BOUNDARY_LAG_SECONDS == 604_800
    assert acquisition.MAXIMUM_GAP_SECONDS == 345_600
    total_minutes = int(
        (datetime(2023, 7, 1, tzinfo=timezone.utc) - START).total_seconds() // 60
    )
    gap_count = acquisition.MINIMUM_ROWS_PER_SYMBOL - 1
    steps = np.ones(gap_count, dtype=np.int64)
    steps[: total_minutes - 1 - gap_count] += 1
    offsets = np.concatenate((np.array([0], dtype=np.int64), np.cumsum(steps)))
    timestamps = pd.DatetimeIndex(
        pd.to_datetime(int(START.timestamp()) + offsets * 60, unit="s", utc=True),
        name="timestamp",
    )
    end = datetime(2023, 7, 1, tzinfo=timezone.utc)
    diagnostics = acquisition._coverage_diagnostics(timestamps, start=START, end=end)
    assert diagnostics.first_timestamp == "2023-01-01T00:00:00Z"
    assert diagnostics.last_timestamp == "2023-06-30T23:59:00Z"
    assert diagnostics.gap_count == total_minutes - 1 - gap_count
    assert diagnostics.maximum_gap_seconds == 120
    assert len(diagnostics.timestamp_set_sha256) == 64


def test_coverage_policy_rejects_short_or_excessively_gapped_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    short = pd.DatetimeIndex([START, START + timedelta(minutes=1)], name="timestamp")
    with pytest.raises(ValueError, match="minimum is 175000"):
        acquisition._coverage_diagnostics(short, start=START, end=END)

    monkeypatch.setattr(acquisition, "MINIMUM_ROWS_PER_SYMBOL", 2)
    too_large_gap = pd.DatetimeIndex(
        [START, START + timedelta(seconds=345_660)],
        name="timestamp",
    )
    with pytest.raises(ValueError, match="maximum gap"):
        acquisition._coverage_diagnostics(
            too_large_gap,
            start=START,
            end=START + timedelta(seconds=345_720),
        )


def test_installed_dependency_identity_and_instruments_load_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_socket(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dependency identity loading attempted network access")

    monkeypatch.setattr(socket, "socket", forbid_socket)
    dependencies = acquisition._load_default_dependencies()
    assert dependencies.endpoint == acquisition.DUKASCOPY_ENDPOINT
    assert dependencies.interval_m1 == "1MIN"
    assert dependencies.offer_side_bid == "B"
    assert dependencies.offer_side_ask == "A"
    assert len(dependencies.library_module_sha256) == 64
    assert len(dependencies.instruments_module_sha256) == 64
    for symbol in acquisition.PVSCLC_SYMBOLS:
        instrument_id, instrument_key = dependencies.resolve_instrument(symbol)
        assert instrument_id == f"{symbol[:3]}/{symbol[3:]}"
        assert instrument_key.startswith("INSTRUMENT_FX_")


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC syntax check")
def test_intended_wsl_unc_target_is_syntactically_absolute_without_inspection() -> None:
    assert Path(acquisition.INTENDED_WINDOWS_TARGET).is_absolute()
