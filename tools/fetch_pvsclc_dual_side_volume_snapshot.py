from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import platform
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype


# AGENT: ISOLATION: This public-source acquisition tool does not read repository
# raw data, runtime configuration, registries, credentials, or broker state.
PVSCLC_SYMBOLS: tuple[str, ...] = (
    "AUDJPY",
    "AUDUSD",
    "CADJPY",
    "CHFJPY",
    "EURAUD",
    "EURCAD",
    "EURCHF",
    "EURGBP",
    "EURJPY",
    "EURUSD",
    "GBPCAD",
    "GBPCHF",
    "GBPJPY",
    "GBPUSD",
    "NZDUSD",
    "USDCAD",
    "USDCHF",
    "USDJPY",
)
CSV_HEADER: tuple[str, ...] = (
    "timestamp",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
    "bid_volume",
    "ask_volume",
)
SIDE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
SIDE_STREAM_HEADER: tuple[str, ...] = ("timestamp",) + SIDE_COLUMNS
PROVENANCE_SCHEMA_VERSION = "fxstack.scalp.dual_side_volume_snapshot_provenance.v1"
ACTIVITY_METRIC_ID = "dukascopy_m1_bid_volume_plus_ask_volume_v1"
PROVIDER_NAME = "Dukascopy public historical feed"
DUKASCOPY_ENDPOINT = "https://freeserv.dukascopy.com/2.0/index.php"
DUKASCOPY_QUERY_PATH = "chart/json3"
MAX_RETRIES = 7
FETCH_LIMIT = 5_000
PROVIDER_END_INCLUSIVE_ADJUSTMENT_MINUTES = -1
MINIMUM_ROWS_PER_SYMBOL = 175_000
MAXIMUM_BOUNDARY_LAG_SECONDS = 604_800
MAXIMUM_GAP_SECONDS = 345_600
TIMESTAMP_SET_HASH_GRAMMAR = "sha256_ascii_lf_header_timestamp_then_canonical_rows_v1"
TOOL_REPOSITORY_PATH = "tools/fetch_pvsclc_dual_side_volume_snapshot.py"
INTENDED_WSL_TARGET = "/var/tmp/fxscalp-20260803-pvsclc-2023h1-source-v1"
INTENDED_WINDOWS_TARGET = (
    r"\\wsl.localhost\Ubuntu-22.04\var\tmp\fxscalp-20260803-pvsclc-2023h1-source-v1"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_UTC_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})T(?P<hour>\d{2}):(?P<minute>\d{2}):00Z\Z"
)
_CANONICAL_UTC_SECOND_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:[0-5]\dZ\Z")
_EXPLICIT_FORBIDDEN_ENV_KEYS = frozenset(
    {
        "BRIDGE_URL",
        "DATABASE_URL",
        "FXSTACK_BRIDGE_API_KEY",
        "FXSTACK_DATABASE_URL",
        "FXSTACK_MODEL_ACTIVATION_MANIFEST",
        "MT4_BRIDGE_URL",
        "TRADER_BRIDGE_API_KEY",
        "TRADER_BRIDGE_URL",
    }
)
_FORBIDDEN_TRANSPORT_ENV_KEYS = frozenset(
    {
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NETRC",
        "PYTHONHOME",
        "PYTHONPATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    }
)
_SENSITIVE_ENV_KEY_RE = re.compile(
    r"^(?:FXSTACK|TRADER|MT4|MT5|IG|OANDA|IBKR)_[A-Z0-9_]*"
    r"(?:DATABASE|POSTGRES|DSN|DB(?:_|$)|BRIDGE|BROKER|CREDENTIAL|REGISTRY|ACTIVATION|API_KEY|SECRET|PASSWORD|TOKEN|ACCOUNT)"
    r"[A-Z0-9_]*$"
)


@dataclass(frozen=True, slots=True)
class ProviderDependencies:
    fetch: Callable[..., pd.DataFrame]
    resolve_instrument: Callable[[str], tuple[str, str]]
    interval_m1: str
    offer_side_bid: str
    offer_side_ask: str
    library_version: str
    library_module_sha256: str
    instruments_module_sha256: str
    requests_version: str
    pandas_version: str
    python_version: str
    endpoint: str = DUKASCOPY_ENDPOINT


@dataclass(frozen=True, slots=True)
class AcquisitionConfig:
    target: Path
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class NormalizedSide:
    timestamps: pd.DatetimeIndex
    numeric: np.ndarray
    raw_rows: int
    stream_sha256: str
    volume_zero_count: int

    @property
    def rows(self) -> int:
        return int(len(self.timestamps))


@dataclass(frozen=True, slots=True)
class CoverageDiagnostics:
    first_timestamp: str
    last_timestamp: str
    timestamp_set_sha256: str
    gap_count: int
    maximum_gap_seconds: int


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_utc_minute(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC datetime must be timezone-aware")
    utc = value.astimezone(timezone.utc)
    if utc.second != 0 or utc.microsecond != 0:
        raise ValueError("datetime must be an exact whole UTC minute")
    return utc.strftime("%Y-%m-%dT%H:%M:00Z")


def _canonical_utc_second(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC datetime must be timezone-aware")
    utc = value.astimezone(timezone.utc)
    if utc.microsecond != 0:
        raise ValueError(
            "acquisition completion time must be an exact whole UTC second"
        )
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_utc_minute(raw: str) -> datetime:
    text = str(raw)
    if not _CANONICAL_UTC_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(
            "timestamp must use canonical UTC whole-minute form YYYY-MM-DDTHH:MM:00Z"
        )
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp is not a valid UTC minute") from exc
    return parsed


def _assert_safe_environment() -> None:
    source = dict(os.environ)
    forbidden = {
        key
        for key, value in source.items()
        if str(value or "").strip()
        and (
            key.upper() in _EXPLICIT_FORBIDDEN_ENV_KEYS
            or key.upper() in _FORBIDDEN_TRANSPORT_ENV_KEYS
            or _SENSITIVE_ENV_KEY_RE.fullmatch(key.upper())
        )
    }
    for key in ("FXSTACK_EXECUTION_PROVIDER", "FXSTACK_MARKET_DATA_PROVIDER"):
        value = str(source.get(key) or "").strip().lower()
        if value and value not in {"disabled", "offline", "research"}:
            forbidden.add(key)
    if forbidden:
        raise RuntimeError(
            "public-source acquisition refuses production or ambient transport/import "
            "environment settings: " + ",".join(sorted(forbidden))
        )


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_target(target: Path, *, repository_root: Path = REPOSITORY_ROOT) -> Path:
    candidate = Path(target)
    if not candidate.is_absolute():
        raise ValueError("target must be an absolute path")
    if not candidate.name or candidate == Path(candidate.anchor):
        raise ValueError("target must name a non-root bundle directory")
    if candidate.exists() or candidate.is_symlink():
        raise FileExistsError(f"target already exists: {candidate}")
    parent = candidate.parent
    if not parent.exists() or not parent.is_dir():
        raise ValueError("target parent must already exist as a directory")
    if parent.is_symlink():
        raise ValueError("target parent must not be a symbolic link")
    resolved_parent = parent.resolve(strict=True)
    resolved_candidate = resolved_parent / candidate.name
    resolved_repository = repository_root.resolve(strict=True)
    if _path_is_within(resolved_candidate, resolved_repository):
        raise ValueError("target must be physically outside the repository")
    return resolved_candidate


def _make_unique_partial(target: Path) -> Path:
    prefix = f".{target.name}.partial-"
    if any(path.name.startswith(prefix) for path in target.parent.iterdir()):
        raise FileExistsError(
            "a prior partial acquisition exists; use a new versioned target"
        )
    partial = target.parent / f"{prefix}one-shot"
    try:
        partial.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError(
            "the one-shot partial acquisition was already claimed"
        ) from exc
    return partial


def _resolve_from_module(instruments: Any, symbol: str) -> tuple[str, str]:
    if symbol not in PVSCLC_SYMBOLS:
        raise ValueError(f"unsupported PVSCLC symbol: {symbol}")
    base, quote = symbol[:3], symbol[3:]
    candidates = (
        f"INSTRUMENT_FX_MAJORS_{base}_{quote}",
        f"INSTRUMENT_FX_MINORS_{base}_{quote}",
        f"INSTRUMENT_FX_EXOTICS_{base}_{quote}",
    )
    for key in candidates:
        if hasattr(instruments, key):
            value = str(getattr(instruments, key))
            if value:
                return value, key
    suffix = f"_{base}_{quote}"
    matches = sorted(
        key
        for key in dir(instruments)
        if key.startswith("INSTRUMENT_FX_") and key.endswith(suffix)
    )
    if len(matches) != 1:
        raise RuntimeError(f"exact Dukascopy instrument resolution failed for {symbol}")
    key = matches[0]
    value = str(getattr(instruments, key))
    if not value:
        raise RuntimeError(f"empty Dukascopy instrument id for {symbol}")
    return value, key


def _load_default_dependencies() -> ProviderDependencies:
    import importlib.metadata

    import dukascopy_python as dukascopy
    import dukascopy_python.instruments as instruments
    import requests

    module_path = Path(str(dukascopy.__file__)).resolve(strict=True)
    module_bytes = module_path.read_bytes()
    module_text = module_bytes.decode("utf-8")
    if DUKASCOPY_ENDPOINT not in module_text or DUKASCOPY_QUERY_PATH not in module_text:
        raise RuntimeError("dukascopy-python endpoint identity changed")
    instruments_path = Path(str(instruments.__file__)).resolve(strict=True)
    instruments_bytes = instruments_path.read_bytes()
    return ProviderDependencies(
        fetch=dukascopy.fetch,
        resolve_instrument=lambda symbol: _resolve_from_module(instruments, symbol),
        interval_m1=str(dukascopy.INTERVAL_MIN_1),
        offer_side_bid=str(dukascopy.OFFER_SIDE_BID),
        offer_side_ask=str(dukascopy.OFFER_SIDE_ASK),
        library_version=importlib.metadata.version("dukascopy-python"),
        library_module_sha256=_sha256_bytes(module_bytes),
        instruments_module_sha256=_sha256_bytes(instruments_bytes),
        requests_version=str(requests.__version__),
        pandas_version=str(pd.__version__),
        python_version=platform.python_version(),
    )


def _validate_dependencies(dependencies: ProviderDependencies) -> None:
    if dependencies.endpoint != DUKASCOPY_ENDPOINT:
        raise RuntimeError("only the frozen Dukascopy public endpoint is allowed")
    if dependencies.interval_m1 != "1MIN":
        raise RuntimeError("Dukascopy M1 interval identity changed")
    if dependencies.offer_side_bid != "B" or dependencies.offer_side_ask != "A":
        raise RuntimeError("Dukascopy BID/ASK offer-side identity changed")
    if not _SHA256_RE.fullmatch(dependencies.library_module_sha256):
        raise RuntimeError("invalid dukascopy-python module SHA-256 identity")
    if not _SHA256_RE.fullmatch(dependencies.instruments_module_sha256):
        raise RuntimeError("invalid dukascopy-python instruments SHA-256 identity")
    for name, value in (
        ("dukascopy-python", dependencies.library_version),
        ("requests", dependencies.requests_version),
        ("pandas", dependencies.pandas_version),
        ("Python", dependencies.python_version),
    ):
        if not str(value).strip():
            raise RuntimeError(f"missing {name} version identity")


def _format_number(value: float) -> str:
    number = float(value)
    if not np.isfinite(number):
        raise ValueError("cannot serialize a non-finite number")
    return repr(number)


def _timestamp_strings(index: pd.DatetimeIndex) -> list[str]:
    return [timestamp.strftime("%Y-%m-%dT%H:%M:00Z") for timestamp in index]


def _encode_rows(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> bytes:
    lines = [",".join(header)]
    lines.extend(",".join(row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _normalize_side(
    frame: pd.DataFrame,
    *,
    symbol: str,
    side: str,
    start: datetime,
    end: datetime,
) -> NormalizedSide:
    label = f"{symbol} {side}"
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{label}: fetch result must be a pandas DataFrame")
    if tuple(frame.columns) != SIDE_COLUMNS:
        raise ValueError(f"{label}: expected exact columns {SIDE_COLUMNS}")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.name != "timestamp":
        raise ValueError(f"{label}: timestamp must be the named DatetimeIndex")
    if frame.index.tz is None:
        raise ValueError(f"{label}: timestamps must be UTC-aware")
    index = frame.index
    if any(timestamp.utcoffset().total_seconds() != 0 for timestamp in index):
        raise ValueError(f"{label}: timestamps must be UTC")
    index = index.tz_convert("UTC")
    if not index.is_unique:
        raise ValueError(f"{label}: duplicate timestamps are forbidden")
    if not index.is_monotonic_increasing:
        raise ValueError(f"{label}: timestamps must be strictly increasing")
    if any(
        timestamp.second != 0 or timestamp.microsecond != 0 or timestamp.nanosecond != 0
        for timestamp in index
    ):
        raise ValueError(f"{label}: timestamps must be exact whole minutes")

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if ((index < start_ts) | (index >= end_ts)).any():
        raise ValueError(
            f"{label}: provider returned a timestamp outside the requested window"
        )
    selected = frame
    selected_index = index
    if selected.empty:
        raise ValueError(
            f"{label}: no rows in the requested start-inclusive/end-exclusive window"
        )

    arrays: list[np.ndarray] = []
    for column in SIDE_COLUMNS:
        series = selected[column]
        if not is_numeric_dtype(series.dtype):
            raise ValueError(f"{label}: {column} must have a numeric dtype")
        values = series.to_numpy(dtype=np.float64, na_value=np.nan, copy=True)
        if not np.isfinite(values).all():
            raise ValueError(f"{label}: {column} contains missing or non-finite values")
        if column == "volume":
            if (values < 0.0).any():
                raise ValueError(f"{label}: volume contains negative values")
        elif (values <= 0.0).any():
            raise ValueError(f"{label}: {column} must be finite and positive")
        arrays.append(values)
    numeric = np.column_stack(arrays)
    opens = numeric[:, 0]
    highs = numeric[:, 1]
    lows = numeric[:, 2]
    closes = numeric[:, 3]
    if (
        (highs < np.maximum(opens, closes)).any()
        or (lows > np.minimum(opens, closes)).any()
        or (highs < lows).any()
    ):
        raise ValueError(f"{label}: OHLC geometry is inconsistent")

    timestamp_text = _timestamp_strings(selected_index)
    stream_rows = [
        (timestamp_text[row], *(_format_number(value) for value in numeric[row]))
        for row in range(len(selected_index))
    ]
    stream_sha256 = _sha256_bytes(_encode_rows(SIDE_STREAM_HEADER, stream_rows))
    return NormalizedSide(
        timestamps=selected_index,
        numeric=numeric,
        raw_rows=int(len(frame)),
        stream_sha256=stream_sha256,
        volume_zero_count=int(np.count_nonzero(numeric[:, 4] == 0.0)),
    )


def _build_output_bytes(bid: NormalizedSide, ask: NormalizedSide) -> bytes:
    timestamps = _timestamp_strings(bid.timestamps)
    rows = [
        (
            timestamps[row],
            *(_format_number(value) for value in bid.numeric[row, :4]),
            *(_format_number(value) for value in ask.numeric[row, :4]),
            _format_number(bid.numeric[row, 4]),
            _format_number(ask.numeric[row, 4]),
        )
        for row in range(bid.rows)
    ]
    return _encode_rows(CSV_HEADER, rows)


def _coverage_diagnostics(
    timestamps: pd.DatetimeIndex, *, start: datetime, end: datetime
) -> CoverageDiagnostics:
    rows = int(len(timestamps))
    if rows < MINIMUM_ROWS_PER_SYMBOL:
        raise ValueError(
            f"coverage has {rows} rows; minimum is {MINIMUM_ROWS_PER_SYMBOL}"
        )
    canonical = _timestamp_strings(timestamps)
    first_epoch = int(timestamps[0].timestamp())
    last_epoch = int(timestamps[-1].timestamp())
    start_epoch = int(start.timestamp())
    end_epoch = int(end.timestamp())
    if not start_epoch <= first_epoch < start_epoch + MAXIMUM_BOUNDARY_LAG_SECONDS:
        raise ValueError("first timestamp exceeds the frozen start-boundary lag")
    if not end_epoch - MAXIMUM_BOUNDARY_LAG_SECONDS <= last_epoch < end_epoch:
        raise ValueError("last timestamp exceeds the frozen end-boundary lag")
    epochs = [int(timestamp.timestamp()) for timestamp in timestamps]
    gaps = [current - previous for previous, current in zip(epochs, epochs[1:])]
    if any(gap <= 0 or gap % 60 != 0 for gap in gaps):
        raise ValueError("timestamp gaps must be positive whole-minute multiples")
    maximum_gap = max(gaps, default=0)
    if maximum_gap > MAXIMUM_GAP_SECONDS:
        raise ValueError("timestamp coverage exceeds the frozen maximum gap")
    timestamp_bytes = (
        "timestamp\n" + "".join(f"{value}\n" for value in canonical)
    ).encode("ascii")
    return CoverageDiagnostics(
        first_timestamp=canonical[0],
        last_timestamp=canonical[-1],
        timestamp_set_sha256=_sha256_bytes(timestamp_bytes),
        gap_count=sum(gap != 60 for gap in gaps),
        maximum_gap_seconds=maximum_gap,
    )


def _write_exclusive(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _strict_json_loads(payload: bytes) -> Any:
    def _reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_reject_duplicate,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant: {value}")
        ),
    )


def _validate_provenance(payload: Mapping[str, Any]) -> None:
    root_keys = {
        "schema_version",
        "activity_metric_id",
        "provider",
        "acquisition_started_at_utc",
        "as_of_utc",
        "window",
        "request_contract",
        "coverage_contract",
        "merge_contract",
        "fill_contract",
        "tool_contract",
        "input_manifest_sha256",
        "symbols",
    }
    if set(payload) != root_keys:
        raise RuntimeError("volume provenance root schema mismatch")
    if payload["schema_version"] != PROVENANCE_SCHEMA_VERSION:
        raise RuntimeError("volume provenance schema version mismatch")
    if (
        payload["activity_metric_id"] != ACTIVITY_METRIC_ID
        or payload["provider"] != PROVIDER_NAME
    ):
        raise RuntimeError("volume provenance source identity mismatch")
    if not _CANONICAL_UTC_SECOND_RE.fullmatch(
        str(payload["acquisition_started_at_utc"])
    ):
        raise RuntimeError("volume provenance acquisition start is not canonical")
    if not _CANONICAL_UTC_SECOND_RE.fullmatch(str(payload["as_of_utc"])):
        raise RuntimeError("volume provenance as-of timestamp is not canonical")
    acquisition_started = datetime.strptime(
        str(payload["acquisition_started_at_utc"]), "%Y-%m-%dT%H:%M:%SZ"
    )
    acquisition_completed = datetime.strptime(
        str(payload["as_of_utc"]), "%Y-%m-%dT%H:%M:%SZ"
    )
    if acquisition_started > acquisition_completed:
        raise RuntimeError("volume provenance acquisition chronology is inverted")
    if set(payload["window"]) != {"start_inclusive", "end_exclusive"}:
        raise RuntimeError("volume provenance window schema mismatch")
    window_start_text = str(payload["window"]["start_inclusive"])
    window_end_text = str(payload["window"]["end_exclusive"])
    if not _CANONICAL_UTC_RE.fullmatch(
        window_start_text
    ) or not _CANONICAL_UTC_RE.fullmatch(window_end_text):
        raise RuntimeError("volume provenance window timestamp is invalid")
    window_start = datetime.strptime(window_start_text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    window_end = datetime.strptime(window_end_text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    if window_start >= window_end:
        raise RuntimeError("volume provenance window chronology is invalid")
    coverage = payload["coverage_contract"]
    if set(coverage) != {
        "rows_preserved_without_filtering",
        "minimum_rows_per_symbol",
        "maximum_boundary_lag_seconds",
        "maximum_gap_seconds",
        "timestamp_set_hash_grammar",
    } or coverage != {
        "rows_preserved_without_filtering": True,
        "minimum_rows_per_symbol": MINIMUM_ROWS_PER_SYMBOL,
        "maximum_boundary_lag_seconds": MAXIMUM_BOUNDARY_LAG_SECONDS,
        "maximum_gap_seconds": MAXIMUM_GAP_SECONDS,
        "timestamp_set_hash_grammar": TIMESTAMP_SET_HASH_GRAMMAR,
    }:
        raise RuntimeError("volume provenance coverage contract mismatch")
    request = payload["request_contract"]
    if set(request) != {
        "endpoint",
        "timeframe",
        "bid_offer_side",
        "ask_offer_side",
        "separate_requests",
        "resume",
        "max_retries",
        "limit",
        "provider_end_inclusive_adjustment_minutes",
        "out_of_window_rows_policy",
    } or request != {
        "endpoint": DUKASCOPY_ENDPOINT,
        "timeframe": "1MIN",
        "bid_offer_side": "B",
        "ask_offer_side": "A",
        "separate_requests": True,
        "resume": False,
        "max_retries": MAX_RETRIES,
        "limit": FETCH_LIMIT,
        "provider_end_inclusive_adjustment_minutes": (
            PROVIDER_END_INCLUSIVE_ADJUSTMENT_MINUTES
        ),
        "out_of_window_rows_policy": "reject",
    }:
        raise RuntimeError("volume provenance request contract mismatch")
    merge = payload["merge_contract"]
    if set(merge) != {
        "method",
        "unmatched_bid_rows",
        "unmatched_ask_rows",
    } or merge != {
        "method": "exact_timestamp_inner_join_with_equal_side_sets",
        "unmatched_bid_rows": 0,
        "unmatched_ask_rows": 0,
    }:
        raise RuntimeError("volume provenance merge contract mismatch")
    fill = payload["fill_contract"]
    if set(fill) != {"mid_only_fallback", "zero_fill", "synthetic_side"} or fill != {
        "mid_only_fallback": False,
        "zero_fill": False,
        "synthetic_side": False,
    }:
        raise RuntimeError("volume provenance fill contract mismatch")
    tool = payload["tool_contract"]
    if set(tool) != {
        "tool_path",
        "tool_sha256",
        "dukascopy_python_version",
        "dukascopy_python_module_sha256",
        "dukascopy_python_instruments_module_sha256",
        "pandas_version",
        "requests_version",
        "python_version",
    }:
        raise RuntimeError("volume provenance tool contract mismatch")
    if tool["tool_path"] != TOOL_REPOSITORY_PATH:
        raise RuntimeError("volume provenance tool path mismatch")
    for key in (
        "tool_sha256",
        "dukascopy_python_module_sha256",
        "dukascopy_python_instruments_module_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(tool[key])):
            raise RuntimeError(f"volume provenance {key} is invalid")
    if not _SHA256_RE.fullmatch(str(payload["input_manifest_sha256"])):
        raise RuntimeError("volume provenance input manifest hash is invalid")
    symbols = payload["symbols"]
    if not isinstance(symbols, dict) or tuple(symbols) != PVSCLC_SYMBOLS:
        raise RuntimeError("volume provenance symbol universe/order mismatch")
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
    volume_keys = {"rows", "finite", "nonnegative", "missing", "zero_count"}
    for symbol in PVSCLC_SYMBOLS:
        record = symbols[symbol]
        if set(record) != symbol_keys:
            raise RuntimeError(f"{symbol}: volume provenance symbol schema mismatch")
        if record["instrument_id"] != f"{symbol[:3]}/{symbol[3:]}":
            raise RuntimeError(f"{symbol}: instrument id mismatch")
        if record["output_file"] != f"input/{symbol}_M1.csv":
            raise RuntimeError(f"{symbol}: output path mismatch")
        for key in (
            "output_sha256",
            "bid_stream_sha256",
            "ask_stream_sha256",
            "timestamp_set_sha256",
        ):
            if not _SHA256_RE.fullmatch(str(record[key])):
                raise RuntimeError(f"{symbol}: invalid {key}")
        if type(record["rows"]) is not int or record["rows"] <= 0:
            raise RuntimeError(f"{symbol}: invalid row count")
        if record["rows"] < MINIMUM_ROWS_PER_SYMBOL:
            raise RuntimeError(f"{symbol}: coverage row count is below the minimum")
        if not _CANONICAL_UTC_RE.fullmatch(str(record["first_timestamp"])) or not (
            _CANONICAL_UTC_RE.fullmatch(str(record["last_timestamp"]))
        ):
            raise RuntimeError(f"{symbol}: coverage boundary timestamp is invalid")
        first_timestamp = datetime.strptime(
            str(record["first_timestamp"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        last_timestamp = datetime.strptime(
            str(record["last_timestamp"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        if not (
            window_start
            <= first_timestamp
            < window_start + timedelta(seconds=MAXIMUM_BOUNDARY_LAG_SECONDS)
            and window_end - timedelta(seconds=MAXIMUM_BOUNDARY_LAG_SECONDS)
            <= last_timestamp
            < window_end
            and first_timestamp <= last_timestamp
        ):
            raise RuntimeError(f"{symbol}: coverage boundary lag is invalid")
        if (
            type(record["gap_count"]) is not int
            or not 0 <= record["gap_count"] <= record["rows"] - 1
            or type(record["maximum_gap_seconds"]) is not int
            or not 0 <= record["maximum_gap_seconds"] <= MAXIMUM_GAP_SECONDS
            or (record["rows"] == 1) != (record["maximum_gap_seconds"] == 0)
            or (
                record["maximum_gap_seconds"] > 0
                and record["maximum_gap_seconds"] % 60 != 0
            )
            or (record["gap_count"] == 0 and record["maximum_gap_seconds"] != 60)
            or (record["gap_count"] > 0 and record["maximum_gap_seconds"] <= 60)
        ):
            raise RuntimeError(f"{symbol}: coverage gap diagnostics are invalid")
        if record["unmatched_bid_rows"] != 0 or record["unmatched_ask_rows"] != 0:
            raise RuntimeError(f"{symbol}: side timestamps are unmatched")
        for side_key in ("bid_volume", "ask_volume"):
            stats = record[side_key]
            if set(stats) != volume_keys:
                raise RuntimeError(f"{symbol}: {side_key} schema mismatch")
            if (
                stats["rows"] != record["rows"]
                or stats["finite"] is not True
                or stats["nonnegative"] is not True
                or stats["missing"] != 0
                or type(stats["zero_count"]) is not int
                or not 0 <= stats["zero_count"] <= record["rows"]
            ):
                raise RuntimeError(f"{symbol}: {side_key} integrity mismatch")


def _validate_staged_bundle(
    partial: Path,
    *,
    expected_csv_bytes: Mapping[str, bytes],
    manifest_bytes: bytes,
    provenance_bytes: bytes,
    provenance: Mapping[str, Any],
) -> None:
    expected_root = {"input", "input_sha256.txt", "volume_provenance.json"}
    if {path.name for path in partial.iterdir()} != expected_root:
        raise RuntimeError("staged bundle root topology mismatch")
    input_root = partial / "input"
    if input_root.is_symlink() or not input_root.is_dir():
        raise RuntimeError("staged input directory is invalid")
    expected_names = {f"{symbol}_M1.csv" for symbol in PVSCLC_SYMBOLS}
    if {path.name for path in input_root.iterdir()} != expected_names:
        raise RuntimeError("staged input file topology mismatch")
    if any(path.is_symlink() or not path.is_file() for path in input_root.iterdir()):
        raise RuntimeError("staged input contains a non-regular file")

    manifest_path = partial / "input_sha256.txt"
    provenance_path = partial / "volume_provenance.json"
    if manifest_path.read_bytes() != manifest_bytes:
        raise RuntimeError("staged input manifest bytes changed")
    if provenance_path.read_bytes() != provenance_bytes:
        raise RuntimeError("staged volume provenance bytes changed")
    if _strict_json_loads(provenance_bytes) != provenance:
        raise RuntimeError("staged volume provenance strict JSON replay failed")
    _validate_provenance(provenance)

    expected_manifest_lines: list[str] = []
    for relative_path in sorted(expected_csv_bytes):
        path = partial / Path(relative_path)
        payload = path.read_bytes()
        if payload != expected_csv_bytes[relative_path]:
            raise RuntimeError(f"staged CSV bytes changed: {relative_path}")
        digest = _sha256_bytes(payload)
        symbol = Path(relative_path).name[:6]
        symbol_provenance = provenance["symbols"][symbol]
        if digest != symbol_provenance["output_sha256"]:
            raise RuntimeError(f"staged CSV provenance hash mismatch: {relative_path}")
        lines = payload.decode("ascii").splitlines()
        timestamps = [line.split(",", 1)[0] for line in lines[1:]]
        timestamp_bytes = (
            "timestamp\n" + "".join(f"{value}\n" for value in timestamps)
        ).encode("ascii")
        if _sha256_bytes(timestamp_bytes) != symbol_provenance["timestamp_set_sha256"]:
            raise RuntimeError(
                f"staged CSV timestamp-set hash mismatch: {relative_path}"
            )
        timestamp_index = pd.DatetimeIndex(
            pd.to_datetime(timestamps, utc=True, errors="raise"),
            name="timestamp",
        )
        window_start = datetime.strptime(
            str(provenance["window"]["start_inclusive"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        window_end = datetime.strptime(
            str(provenance["window"]["end_exclusive"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        coverage = _coverage_diagnostics(
            timestamp_index,
            start=window_start,
            end=window_end,
        )
        if (
            symbol_provenance["rows"] != len(timestamps)
            or symbol_provenance["first_timestamp"] != coverage.first_timestamp
            or symbol_provenance["last_timestamp"] != coverage.last_timestamp
            or symbol_provenance["gap_count"] != coverage.gap_count
            or symbol_provenance["maximum_gap_seconds"] != coverage.maximum_gap_seconds
        ):
            raise RuntimeError(
                f"staged CSV coverage diagnostics mismatch: {relative_path}"
            )
        expected_manifest_lines.append(f"{digest}  {relative_path}")
    expected_manifest = ("\n".join(expected_manifest_lines) + "\n").encode("utf-8")
    if expected_manifest != manifest_bytes:
        raise RuntimeError("staged input manifest is not canonical")
    if _sha256_bytes(manifest_bytes) != provenance["input_manifest_sha256"]:
        raise RuntimeError("staged input manifest provenance hash mismatch")


def _atomic_promote_no_replace(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"target appeared before promotion: {target}")
    if os.name == "nt":
        # Windows MoveFile semantics used by os.rename fail when the target exists.
        os.rename(source, target)
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise RuntimeError("atomic no-replace directory promotion is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise FileExistsError(f"target appeared before promotion: {target}")
            raise OSError(error_number, os.strerror(error_number), str(target))
        return
    raise RuntimeError(
        "atomic no-replace directory promotion is unsupported on this platform"
    )


def acquire_snapshot(
    config: AcquisitionConfig,
) -> Path:
    """Acquire and atomically publish one strict, immutable 18-pair source bundle."""

    _assert_safe_environment()
    tool_source_path = Path(__file__).resolve(strict=True)
    tool_sha256 = _sha256_file(tool_source_path)
    target = _validate_target(config.target)
    start_text = _canonical_utc_minute(config.start)
    end_text = _canonical_utc_minute(config.end)
    if config.end <= config.start:
        raise ValueError("end must be after start")

    deps = _load_default_dependencies()
    _validate_dependencies(deps)
    acquisition_started_at_utc = _canonical_utc_second(_utc_now())
    partial = _make_unique_partial(target)
    input_root = partial / "input"
    input_root.mkdir(mode=0o700, exist_ok=False)

    expected_csv_bytes: dict[str, bytes] = {}
    symbols: dict[str, dict[str, Any]] = {}
    total_unmatched_bid = 0
    total_unmatched_ask = 0
    provider_end = config.end + timedelta(
        minutes=PROVIDER_END_INCLUSIVE_ADJUSTMENT_MINUTES
    )
    if provider_end < config.start:
        raise ValueError("requested window is too short for inclusive-end adjustment")
    for symbol in PVSCLC_SYMBOLS:
        instrument_id, _instrument_key = deps.resolve_instrument(symbol)
        expected_instrument_id = f"{symbol[:3]}/{symbol[3:]}"
        if str(instrument_id) != expected_instrument_id:
            raise RuntimeError(
                f"{symbol}: instrument id must equal {expected_instrument_id}"
            )
        bid_frame = deps.fetch(
            str(instrument_id),
            deps.interval_m1,
            deps.offer_side_bid,
            config.start,
            provider_end,
            max_retries=MAX_RETRIES,
            limit=FETCH_LIMIT,
            debug=False,
        )
        ask_frame = deps.fetch(
            str(instrument_id),
            deps.interval_m1,
            deps.offer_side_ask,
            config.start,
            provider_end,
            max_retries=MAX_RETRIES,
            limit=FETCH_LIMIT,
            debug=False,
        )
        bid = _normalize_side(
            bid_frame,
            symbol=symbol,
            side="BID",
            start=config.start,
            end=config.end,
        )
        ask = _normalize_side(
            ask_frame,
            symbol=symbol,
            side="ASK",
            start=config.start,
            end=config.end,
        )
        bid_only = bid.timestamps.difference(ask.timestamps)
        ask_only = ask.timestamps.difference(bid.timestamps)
        unmatched_bid = int(len(bid_only))
        unmatched_ask = int(len(ask_only))
        total_unmatched_bid += unmatched_bid
        total_unmatched_ask += unmatched_ask
        if unmatched_bid or unmatched_ask or not bid.timestamps.equals(ask.timestamps):
            raise RuntimeError(
                f"{symbol}: BID/ASK timestamp sets differ "
                f"(unmatched_bid_rows={unmatched_bid}, unmatched_ask_rows={unmatched_ask})"
            )
        if (ask.numeric[:, :4] < bid.numeric[:, :4]).any():
            raise ValueError(
                f"{symbol}: ASK OHLC must be greater than or equal to BID OHLC"
            )
        if (bid.numeric[:, 4] > np.finfo(np.float64).max - ask.numeric[:, 4]).any():
            raise ValueError(f"{symbol}: recomputed total activity would be non-finite")
        coverage = _coverage_diagnostics(
            bid.timestamps,
            start=config.start,
            end=config.end,
        )

        output_bytes = _build_output_bytes(bid, ask)
        relative_path = f"input/{symbol}_M1.csv"
        output_sha256 = _sha256_bytes(output_bytes)
        _write_exclusive(input_root / f"{symbol}_M1.csv", output_bytes)
        expected_csv_bytes[relative_path] = output_bytes
        symbols[symbol] = {
            "instrument_id": str(instrument_id),
            "output_file": relative_path,
            "output_sha256": output_sha256,
            "rows": bid.rows,
            "bid_stream_sha256": bid.stream_sha256,
            "ask_stream_sha256": ask.stream_sha256,
            "unmatched_bid_rows": unmatched_bid,
            "unmatched_ask_rows": unmatched_ask,
            "first_timestamp": coverage.first_timestamp,
            "last_timestamp": coverage.last_timestamp,
            "timestamp_set_sha256": coverage.timestamp_set_sha256,
            "gap_count": coverage.gap_count,
            "maximum_gap_seconds": coverage.maximum_gap_seconds,
            "bid_volume": {
                "rows": bid.rows,
                "finite": True,
                "nonnegative": True,
                "missing": 0,
                "zero_count": bid.volume_zero_count,
            },
            "ask_volume": {
                "rows": ask.rows,
                "finite": True,
                "nonnegative": True,
                "missing": 0,
                "zero_count": ask.volume_zero_count,
            },
        }

    manifest_lines = [
        f"{_sha256_bytes(expected_csv_bytes[path])}  {path}"
        for path in sorted(expected_csv_bytes)
    ]
    manifest_bytes = ("\n".join(manifest_lines) + "\n").encode("utf-8")
    _write_exclusive(partial / "input_sha256.txt", manifest_bytes)

    if _sha256_file(tool_source_path) != tool_sha256:
        raise RuntimeError("acquisition tool source changed during the fetch")
    as_of_utc = _canonical_utc_second(_utc_now())
    provenance: dict[str, Any] = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "activity_metric_id": ACTIVITY_METRIC_ID,
        "provider": PROVIDER_NAME,
        "acquisition_started_at_utc": acquisition_started_at_utc,
        "as_of_utc": as_of_utc,
        "window": {"start_inclusive": start_text, "end_exclusive": end_text},
        "coverage_contract": {
            "rows_preserved_without_filtering": True,
            "minimum_rows_per_symbol": MINIMUM_ROWS_PER_SYMBOL,
            "maximum_boundary_lag_seconds": MAXIMUM_BOUNDARY_LAG_SECONDS,
            "maximum_gap_seconds": MAXIMUM_GAP_SECONDS,
            "timestamp_set_hash_grammar": TIMESTAMP_SET_HASH_GRAMMAR,
        },
        "request_contract": {
            "endpoint": DUKASCOPY_ENDPOINT,
            "timeframe": "1MIN",
            "bid_offer_side": "B",
            "ask_offer_side": "A",
            "separate_requests": True,
            "resume": False,
            "max_retries": MAX_RETRIES,
            "limit": FETCH_LIMIT,
            "provider_end_inclusive_adjustment_minutes": (
                PROVIDER_END_INCLUSIVE_ADJUSTMENT_MINUTES
            ),
            "out_of_window_rows_policy": "reject",
        },
        "merge_contract": {
            "method": "exact_timestamp_inner_join_with_equal_side_sets",
            "unmatched_bid_rows": total_unmatched_bid,
            "unmatched_ask_rows": total_unmatched_ask,
        },
        "fill_contract": {
            "mid_only_fallback": False,
            "zero_fill": False,
            "synthetic_side": False,
        },
        "tool_contract": {
            "tool_path": TOOL_REPOSITORY_PATH,
            "tool_sha256": tool_sha256,
            "dukascopy_python_version": deps.library_version,
            "dukascopy_python_module_sha256": deps.library_module_sha256,
            "dukascopy_python_instruments_module_sha256": (
                deps.instruments_module_sha256
            ),
            "pandas_version": deps.pandas_version,
            "requests_version": deps.requests_version,
            "python_version": deps.python_version,
        },
        "input_manifest_sha256": _sha256_bytes(manifest_bytes),
        "symbols": symbols,
    }
    _validate_provenance(provenance)
    provenance_bytes = (
        json.dumps(provenance, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    _write_exclusive(partial / "volume_provenance.json", provenance_bytes)
    _validate_staged_bundle(
        partial,
        expected_csv_bytes=expected_csv_bytes,
        manifest_bytes=manifest_bytes,
        provenance_bytes=provenance_bytes,
        provenance=provenance,
    )
    if _sha256_file(tool_source_path) != tool_sha256:
        raise RuntimeError("acquisition tool source changed before promotion")
    _atomic_promote_no_replace(partial, target)
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch strict separate Dukascopy BID and ASK M1 component-volume streams "
            "for the frozen 18-pair PVSCLC universe."
        ),
        epilog=(
            "Intended one-shot target: "
            + INTENDED_WINDOWS_TARGET
            + " (WSL: "
            + INTENDED_WSL_TARGET
            + ")"
        ),
    )
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--start", type=_parse_utc_minute, required=True)
    parser.add_argument("--end", type=_parse_utc_minute, required=True)
    return parser


def run(args: argparse.Namespace) -> int:
    target = acquire_snapshot(
        AcquisitionConfig(
            target=Path(args.target),
            start=args.start,
            end=args.end,
        )
    )
    print(str(target))
    return 0


def main() -> None:
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (FileExistsError, RuntimeError, TypeError, ValueError) as exc:
        print(f"acquisition failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
