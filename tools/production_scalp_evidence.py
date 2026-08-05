from __future__ import annotations

"""Causal evidence producer for the exact production IG-MT4 scalper.

This program belongs on the physically isolated validation host.  It imports
the pure production strategy evaluator, but never imports or contacts the live
runtime, database, bridge, broker, credential store, registry, or signer.

The producer is deliberately fail closed:

* the exact catalog universe and every historical source file are mandatory;
* the production policy is the preselected attempt in a sealed, fixed matrix;
* signals use only finalized consecutive M1 bars and fill on the next M1 open;
* ambiguous intrabar exits resolve stop-first and gaps book the adverse open;
* one entry per symbol per UTC day is enforced during replay;
* Dukascopy history and authenticated IG-DEMO calibration are distinct,
  byte-bound inputs; and
* files are published only after the independent issuer recomputes every
  ledger, cell, cost, MCPT, PBO, DSR, confidence, and drawdown assertion.

The code is stdlib-only until the final independent issuer parity check.  The
issuer intentionally requires NumPy/SciPy on the release host to recompute the
NPZ-backed statistics through a separately owned implementation.
"""

import argparse
import ast
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import itertools
import json
import math
import os
from pathlib import Path
import random
import shutil
import struct
import sys
import tempfile
import time
import types
from typing import Any, BinaryIO, Iterable, Iterator, Mapping, Sequence
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_PACKAGE_ROOT = Path(
    os.environ.get(
        "FXSTACK_EVIDENCE_PACKAGE_ROOT",
        str(REPO_ROOT / "fx-quant-stack" / "src" / "fxstack"),
    )
).resolve()
ENGINE_REPOSITORY_ROOT = Path(
    os.environ.get("FXSTACK_EVIDENCE_REPOSITORY_ROOT", str(REPO_ROOT))
).resolve()
VALIDATION_TOOL_ROOT = Path(
    os.environ.get("FXSTACK_EVIDENCE_TOOL_REPOSITORY_ROOT", str(REPO_ROOT))
).resolve()
FXSTACK_SRC = ENGINE_PACKAGE_ROOT.parent
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

# The production package's public ``__init__`` imports runtime configuration
# dependencies that are intentionally absent from the isolated replay host.
# Load only the four pure-code namespaces required by this evaluator.  Their
# concrete modules still come byte-for-byte from ``ENGINE_PACKAGE_ROOT`` and
# the imported evaluator path is verified immediately below.
if "fxstack" not in sys.modules:
    for _namespace, _namespace_path in (
        ("fxstack", ENGINE_PACKAGE_ROOT),
        ("fxstack.providers", ENGINE_PACKAGE_ROOT / "providers"),
        ("fxstack.runtime", ENGINE_PACKAGE_ROOT / "runtime"),
        ("fxstack.schemas", ENGINE_PACKAGE_ROOT / "schemas"),
        ("fxstack.strategy", ENGINE_PACKAGE_ROOT / "strategy"),
    ):
        _module = types.ModuleType(_namespace)
        _module.__file__ = str(_namespace_path / "__init__.py")
        _module.__package__ = _namespace
        _module.__path__ = [str(_namespace_path)]
        sys.modules[_namespace] = _module

from fxstack.providers.ig_mt4_catalog import (  # noqa: E402
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.runtime.scalp_engine_identity import (  # noqa: E402
    production_scalp_engine_identity,
)
from fxstack.schemas.entry import EntryBar, EntryEvaluationRequest, EntryProposal  # noqa: E402
from fxstack.strategy.scalp_dislocation import (  # noqa: E402
    SCALP_DISLOCATION_STRATEGY_ID,
    SCALP_DISLOCATION_STRATEGY_VERSION,
    DislocationPolicy,
    evaluate_dislocation,
)


_IMPORTED_EVALUATOR_PATH = Path(
    str(sys.modules[evaluate_dislocation.__module__].__file__)
).resolve()
try:
    _EVALUATOR_IS_FROZEN = _IMPORTED_EVALUATOR_PATH.is_relative_to(
        ENGINE_PACKAGE_ROOT
    )
except AttributeError:  # pragma: no cover - Python >=3.11 in validation
    _EVALUATOR_IS_FROZEN = str(_IMPORTED_EVALUATOR_PATH).startswith(
        str(ENGINE_PACKAGE_ROOT) + os.sep
    )
if not _EVALUATOR_IS_FROZEN:
    raise RuntimeError(
        "production_evaluator_import_outside_frozen_package:"
        f"{_IMPORTED_EVALUATOR_PATH}"
    )


COST_MODEL_SCHEMA = "fxstack.external_scalp_cost_model.v2"
TRADE_LEDGER_SCHEMA = "fxstack.external_scalp_trade_ledger.v1"
CELL_EVIDENCE_SCHEMA = "fxstack.external_scalp_cell_evidence.v1"
STATISTICAL_REPORT_SCHEMA = "fxstack.external_scalp_statistical_report.v1"
ATTEMPT_MANIFEST_SCHEMA = "fxstack.external_scalp_attempt_manifest.v1"
IG_CAPTURE_SCHEMA = "fxstack.external_ig_mt4_bid_ask_capture.v1"

COST_DEFINITION = (
    "max_source_or_ig_p90_spread_plus_adverse_next_open_slippage_fees.v1"
)
SOURCE_QUOTE_DEFINITION = "dukascopy_executable_bid_ask_m1.v1"
SOURCE_SAMPLING_CONTRACT = (
    "utc_epoch_mod_300_plus_all_decision_and_fill_minutes.v1"
)
IG_CALIBRATION_DEFINITION = "authenticated_ig_demo_live_quote_calibration.v1"
RISK_SIZING_METHOD = "production_runtime_risk_kernel_replay.v1"
WIN_DEFINITION = "full_target_hit_first.v1"
MCPT_METHOD = "panel_common_circular_shift_sharpe.v1"
PBO_DSR_METHOD = "cscv_pbo_deflated_sharpe_complete_trials.v1"

SOURCE_NPZ_NAME = "dukascopy_source_quotes.npz"
IG_NPZ_NAME = "ig_demo_calibration.npz"
MCPT_NPZ_NAME = "mcpt_inputs.npz"
PBO_NPZ_NAME = "pbo_dsr_inputs.npz"
ATTEMPT_MANIFEST_NAME = "attempt_manifest.json"
COST_MODEL_NAME = "cost_model.json"
TRADE_LEDGER_NAME = "trade_ledger.json"
CELL_EVIDENCE_NAME = "cell_evidence.json"
STATISTICAL_REPORT_NAME = "statistical_report.json"
EVIDENCE_NAME = "validation_evidence.json"

EXECUTION_MAX_SLIPPAGE_POINTS = 20
BASE_COST_MULTIPLIER = 1.0
TWO_X_COST_MULTIPLIER = 2.0
MCPT_SEED = 7331
MCPT_PERMUTATIONS = 999
PBO_SPLITS = 10
PBO_MAX_COMBINATIONS = 512
SOURCE_SAMPLE_SECONDS = 300
MIN_SOURCE_DAYS = 60
MIN_IG_SAMPLES_PER_SYMBOL = 300
MIN_IG_HISTORY_SAMPLES_PER_SYMBOL = 100
MIN_IG_CAPTURE_DURATION_SECS = 300.0
MAX_IG_SAMPLE_GAP_SECS = 5.0
DEFAULT_INITIAL_EQUITY = 100_000.0
DEFAULT_RISK_FRACTION = 0.005
DEFAULT_ACCOUNT_CURRENCY = "EUR"
MIN_TOTAL_TRADES = 300
MIN_TOTAL_DAYS = 60
MIN_CELL_TRADES = 30
MIN_CELL_DAYS = 10

SOURCE_ID = "external_dukascopy_bid_ask_m1"
SOURCE_VERSION = "v1"


class EvidenceRefusal(RuntimeError):
    """Stable fail-closed error raised before an evidence set is published."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _finite(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise EvidenceRefusal(f"{label}_invalid")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceRefusal(f"{label}_invalid") from exc
    if not math.isfinite(number):
        raise EvidenceRefusal(f"{label}_invalid")
    return number


def _positive(value: Any, *, label: str) -> float:
    number = _finite(value, label=label)
    if number <= 0.0:
        raise EvidenceRefusal(f"{label}_invalid")
    return number


def _nonnegative(value: Any, *, label: str) -> float:
    number = _finite(value, label=label)
    if number < 0.0:
        raise EvidenceRefusal(f"{label}_invalid")
    return number


def _strict_int(value: Any, *, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceRefusal(f"{label}_invalid")
    number = int(value)
    if minimum is not None and number < minimum:
        raise EvidenceRefusal(f"{label}_invalid")
    return number


def _parse_epoch(text: str, *, label: str) -> int:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceRefusal(f"{label}_invalid") from exc
    if parsed.tzinfo is None:
        raise EvidenceRefusal(f"{label}_timezone_missing")
    epoch = parsed.astimezone(timezone.utc).timestamp()
    rounded = round(epoch)
    if not math.isfinite(epoch) or abs(epoch - rounded) > 1e-6:
        raise EvidenceRefusal(f"{label}_not_integral")
    return int(rounded)


def _parse_cli_epoch(text: str | None, *, end: bool = False) -> int | None:
    if not text:
        return None
    normalized = str(text).strip()
    if len(normalized) == 10:
        normalized += "T00:00:00+00:00"
    parsed = _parse_epoch(normalized, label="replay_time")
    del end
    return parsed


def _utc_day(epoch: float | int) -> str:
    return datetime.fromtimestamp(float(epoch), timezone.utc).strftime("%Y-%m-%d")


def _quantile_nearest_rank(values: Sequence[float], probability: float) -> float:
    if not values:
        raise EvidenceRefusal("quantile_sample_empty")
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(float(probability) * len(ordered)))
    return float(ordered[min(rank - 1, len(ordered) - 1)])


def _linear_quantile(values: Sequence[float], probability: float) -> float:
    """Match ``numpy.quantile(..., method='linear')`` without NumPy."""

    if not values:
        raise EvidenceRefusal("quantile_sample_empty")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * float(probability)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def _median(values: Sequence[float]) -> float:
    if not values:
        raise EvidenceRefusal("median_sample_empty")
    ordered = sorted(float(value) for value in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return float((ordered[middle - 1] + ordered[middle]) / 2.0)


def _npy_header(descr: str, shape: tuple[int, ...]) -> bytes:
    shape_text = repr(tuple(int(item) for item in shape))
    header = (
        "{'descr': "
        + repr(descr)
        + ", 'fortran_order': False, 'shape': "
        + shape_text
        + ", }"
    ).encode("ascii")
    padding = (-((10 + len(header) + 1) % 16)) % 16
    body = header + (b" " * padding) + b"\n"
    if len(body) > 65535:
        raise EvidenceRefusal("npy_header_too_large")
    return b"\x93NUMPY\x01\x00" + struct.pack("<H", len(body)) + body


_DTYPE_FORMATS: dict[str, tuple[str, int]] = {
    "<f8": ("d", 8),
    "<i8": ("q", 8),
    "<i4": ("i", 4),
    "<i2": ("h", 2),
    "|u1": ("B", 1),
    "|b1": ("?", 1),
}


def _write_values(handle: BinaryIO, descr: str, values: Iterable[Any]) -> int:
    if descr not in _DTYPE_FORMATS:
        raise EvidenceRefusal(f"unsupported_npy_dtype:{descr}")
    code, _ = _DTYPE_FORMATS[descr]
    count = 0
    batch: list[Any] = []
    for value in values:
        batch.append(value)
        if len(batch) >= 8192:
            handle.write(struct.pack("<" + code * len(batch), *batch))
            count += len(batch)
            batch.clear()
    if batch:
        handle.write(struct.pack("<" + code * len(batch), *batch))
        count += len(batch)
    return count


@dataclass(frozen=True, slots=True)
class RawNpyArray:
    descr: str
    shape: tuple[int, ...]
    path: Path


def _write_raw_array(
    path: Path,
    *,
    descr: str,
    shape: tuple[int, ...],
    values: Iterable[Any],
) -> RawNpyArray:
    expected = math.prod(shape)
    with path.open("wb") as handle:
        observed = _write_values(handle, descr, values)
    if observed != expected:
        raise EvidenceRefusal(
            f"raw_array_shape_mismatch:{path.name}:{observed}!={expected}"
        )
    return RawNpyArray(descr=descr, shape=shape, path=path)


def _write_npz_from_raw(path: Path, arrays: Mapping[str, RawNpyArray]) -> None:
    if not arrays:
        raise EvidenceRefusal("npz_arrays_empty")
    with zipfile.ZipFile(
        path,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for name, array in arrays.items():
            if not name or "/" in name or "\\" in name:
                raise EvidenceRefusal("npz_array_name_invalid")
            with archive.open(f"{name}.npy", mode="w", force_zip64=True) as target:
                target.write(_npy_header(array.descr, array.shape))
                with array.path.open("rb") as source:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
    path.chmod(0o600)


@dataclass(frozen=True, slots=True)
class LoadedNpy:
    descr: str
    shape: tuple[int, ...]
    data: bytes

    def values(self) -> tuple[Any, ...]:
        if self.descr.startswith("|S"):
            width = int(self.descr[2:])
            expected = math.prod(self.shape) * width
            if len(self.data) != expected:
                raise EvidenceRefusal("npy_data_size_invalid")
            return tuple(
                self.data[offset : offset + width].rstrip(b"\x00")
                for offset in range(0, len(self.data), width)
            )
        if self.descr not in _DTYPE_FORMATS:
            raise EvidenceRefusal(f"unsupported_npy_dtype:{self.descr}")
        code, width = _DTYPE_FORMATS[self.descr]
        expected = math.prod(self.shape) * width
        if len(self.data) != expected:
            raise EvidenceRefusal("npy_data_size_invalid")
        if not self.data:
            return ()
        return tuple(struct.unpack("<" + code * math.prod(self.shape), self.data))


def _read_npy(payload: bytes) -> LoadedNpy:
    if len(payload) < 10 or payload[:8] != b"\x93NUMPY\x01\x00":
        raise EvidenceRefusal("npy_format_invalid")
    header_size = struct.unpack("<H", payload[8:10])[0]
    header_end = 10 + header_size
    if header_end > len(payload):
        raise EvidenceRefusal("npy_header_invalid")
    try:
        header = ast.literal_eval(payload[10:header_end].decode("ascii").strip())
    except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
        raise EvidenceRefusal("npy_header_invalid") from exc
    if (
        not isinstance(header, dict)
        or set(header) != {"descr", "fortran_order", "shape"}
        or header["fortran_order"] is not False
        or not isinstance(header["shape"], tuple)
        or any(not isinstance(item, int) or item < 0 for item in header["shape"])
    ):
        raise EvidenceRefusal("npy_header_invalid")
    return LoadedNpy(
        descr=str(header["descr"]),
        shape=tuple(header["shape"]),
        data=payload[header_end:],
    )


def _read_npz(path: Path, *, expected_names: set[str]) -> dict[str, LoadedNpy]:
    if not path.is_file() or path.is_symlink():
        raise EvidenceRefusal("npz_input_not_regular")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = archive.namelist()
            expected_members = {f"{name}.npy" for name in expected_names}
            if set(members) != expected_members or len(members) != len(set(members)):
                raise EvidenceRefusal("npz_input_array_scope_invalid")
            result = {
                member[:-4]: _read_npy(archive.read(member)) for member in members
            }
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise EvidenceRefusal("npz_input_invalid") from exc
    return result


def _write_json_new(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        dict(payload),
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True, slots=True)
class TrialDefinition:
    attempt_id: str
    policy: DislocationPolicy


def _sealed_trials() -> tuple[TrialDefinition, ...]:
    """Return every disclosed attempt, not merely the eventual selection.

    Before this producer was completed, an explicitly rejection-only grid was
    inspected on EURUSD/BTCUSD/LTCUSD/NZDJPY: modes {revert,momentum}, z
    {1.5,2,2.5,3}, TP {1,1.5,2,3,4,6,8}, SL {.75,1,1.5,2}, fixed 20-bar
    stop.  Those 224 policies remain multiple-testing attempts even though no
    survivor was selected.  They are therefore replayed over the complete
    aligned universe and included in PBO/DSR.  The eight nearby policies that
    were preregistered while designing this harness are included as well.
    """

    current = DislocationPolicy()
    trials: list[TrialDefinition] = [
        TrialDefinition("production_current_v1", current)
    ]
    seen = {current.config_sha256()}
    for mode, z_entry, tp_mult, sl_mult in itertools.product(
        ("revert", "momentum"),
        (1.5, 2.0, 2.5, 3.0),
        (1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0),
        (0.75, 1.0, 1.5, 2.0),
    ):
        policy = replace(
            current,
            signal_mode=mode,
            z_entry=z_entry,
            tp_atr_mult=tp_mult,
            sl_atr_mult=sl_mult,
            time_stop_bars=20,
        )
        digest = policy.config_sha256()
        if digest in seen:
            continue
        seen.add(digest)
        attempt_id = (
            f"disclosed_grid_{mode}_z{z_entry:g}_tp{tp_mult:g}_sl{sl_mult:g}"
            .replace(".", "p")
        )
        trials.append(TrialDefinition(attempt_id, policy))
    nearby = (
        ("preregistered_z_entry_1p8", replace(current, z_entry=1.8)),
        ("preregistered_z_entry_2p2", replace(current, z_entry=2.2)),
        ("preregistered_tp_atr_1p35", replace(current, tp_atr_mult=1.35)),
        ("preregistered_tp_atr_1p65", replace(current, tp_atr_mult=1.65)),
        ("preregistered_sl_atr_0p9", replace(current, sl_atr_mult=0.9)),
        ("preregistered_sl_atr_1p1", replace(current, sl_atr_mult=1.1)),
        ("preregistered_time_stop_15", replace(current, time_stop_bars=15)),
        ("preregistered_time_stop_25", replace(current, time_stop_bars=25)),
    )
    for attempt_id, policy in nearby:
        digest = policy.config_sha256()
        if digest not in seen:
            seen.add(digest)
            trials.append(TrialDefinition(attempt_id, policy))
    return tuple(trials)


FIXED_TRIALS: tuple[TrialDefinition, ...] = _sealed_trials()
SELECTED_ATTEMPT_ID = FIXED_TRIALS[0].attempt_id


def _validate_trial_matrix() -> None:
    if len(FIXED_TRIALS) < 2 or FIXED_TRIALS[0].policy != DislocationPolicy():
        raise RuntimeError("sealed trial matrix lost production-current selection")
    identifiers = [trial.attempt_id for trial in FIXED_TRIALS]
    hashes = [trial.policy.config_sha256() for trial in FIXED_TRIALS]
    if len(set(identifiers)) != len(identifiers) or len(set(hashes)) != len(hashes):
        raise RuntimeError("sealed trial matrix contains duplicate attempts")


_validate_trial_matrix()


@dataclass(frozen=True, slots=True)
class HistoricalBar:
    symbol: str
    minute_epoch: int
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float
    strategy_valid: bool

    @property
    def mid_open(self) -> float:
        return (self.bid_open + self.ask_open) / 2.0

    @property
    def mid_high(self) -> float:
        return (self.bid_high + self.ask_high) / 2.0

    @property
    def mid_low(self) -> float:
        return (self.bid_low + self.ask_low) / 2.0

    @property
    def mid_close(self) -> float:
        return (self.bid_close + self.ask_close) / 2.0

    @property
    def open_spread_bps(self) -> float:
        return (self.ask_open - self.bid_open) / self.mid_open * 1e4

    @property
    def close_spread_bps(self) -> float:
        return (self.ask_close - self.bid_close) / self.mid_close * 1e4

    def entry_bar(self) -> EntryBar:
        return EntryBar(
            symbol=self.symbol,
            venue_id=IG_MT4_VENUE_ID,
            source_id=SOURCE_ID,
            source_version=SOURCE_VERSION,
            minute_epoch=self.minute_epoch,
            bar_seconds=60,
            open=self.mid_open,
            high=self.mid_high,
            low=self.mid_low,
            close=self.mid_close,
            bid_close=self.bid_close,
            ask_close=self.ask_close,
            closed=True,
            quality_flags=(),
        )


_CSV_PREFIX = (
    "timestamp",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
)


def _historical_bars(
    path: Path,
    *,
    symbol: str,
    start_epoch: int | None,
    end_epoch: int | None,
) -> Iterator[HistoricalBar]:
    import csv

    if not path.is_file() or path.is_symlink():
        raise EvidenceRefusal(f"historical_source_missing:{symbol}")
    previous_epoch: int | None = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header or tuple(header[: len(_CSV_PREFIX)]) != _CSV_PREFIX:
            raise EvidenceRefusal(f"historical_source_header_invalid:{symbol}")
        for row_number, row in enumerate(reader, start=2):
            if len(row) < len(_CSV_PREFIX):
                raise EvidenceRefusal(
                    f"historical_source_row_short:{symbol}:{row_number}"
                )
            epoch = _parse_epoch(
                row[0], label=f"historical_source_time:{symbol}:{row_number}"
            )
            if epoch % 60 != 0:
                raise EvidenceRefusal(
                    f"historical_source_not_m1_aligned:{symbol}:{row_number}"
                )
            if previous_epoch is not None and epoch <= previous_epoch:
                raise EvidenceRefusal(
                    f"historical_source_not_strictly_ordered:{symbol}:{row_number}"
                )
            previous_epoch = epoch
            if start_epoch is not None and epoch < start_epoch:
                continue
            if end_epoch is not None and epoch >= end_epoch:
                break
            try:
                prices = tuple(float(row[index]) for index in range(1, 9))
            except ValueError as exc:
                raise EvidenceRefusal(
                    f"historical_source_price_invalid:{symbol}:{row_number}"
                ) from exc
            if any(not math.isfinite(value) or value <= 0.0 for value in prices):
                raise EvidenceRefusal(
                    f"historical_source_price_invalid:{symbol}:{row_number}"
                )
            bo, bh, bl, bc, ao, ah, al, ac = prices
            if (
                bl > min(bo, bc)
                or bh < max(bo, bc)
                or bh < bl
                or al > min(ao, ac)
                or ah < max(ao, ac)
                or ah < al
                or ao < bo
                or ac < bc
                or ah < bh
                or al < bl
            ):
                raise EvidenceRefusal(
                    f"historical_source_geometry_invalid:{symbol}:{row_number}"
                )
            frozen = bh == bl and ah == al and bo == bc and ao == ac
            yield HistoricalBar(
                symbol=symbol,
                minute_epoch=epoch,
                bid_open=bo,
                bid_high=bh,
                bid_low=bl,
                bid_close=bc,
                ask_open=ao,
                ask_high=ah,
                ask_low=al,
                ask_close=ac,
                strategy_valid=not frozen,
            )


@dataclass(frozen=True, slots=True)
class SourceFileIdentity:
    symbol: str
    path: Path
    relative_path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    manifest: dict[str, Any]
    sha256: str
    files: Mapping[str, SourceFileIdentity]


def _source_snapshot(
    *,
    csv_root: Path,
    start_epoch: int | None,
    end_epoch: int | None,
) -> SourceSnapshot:
    if start_epoch is None or end_epoch is None or end_epoch <= start_epoch:
        raise EvidenceRefusal("historical_source_window_required")
    resolved_root = csv_root.resolve(strict=True)
    if not resolved_root.is_dir() or resolved_root.is_symlink():
        raise EvidenceRefusal("historical_source_root_invalid")
    identities: dict[str, SourceFileIdentity] = {}
    rows: list[dict[str, Any]] = []
    for symbol in IG_MT4_SCALP_SYMBOLS:
        path = (resolved_root / f"{symbol}_M1.csv").resolve(strict=True)
        if not path.is_file() or path.is_symlink() or not path.is_relative_to(
            resolved_root
        ):
            raise EvidenceRefusal(f"historical_source_missing:{symbol}")
        size = path.stat().st_size
        if size <= 0:
            raise EvidenceRefusal(f"historical_source_empty:{symbol}")
        digest = _file_sha256(path)
        relative = path.relative_to(resolved_root).as_posix()
        identity = SourceFileIdentity(
            symbol=symbol,
            path=path,
            relative_path=relative,
            sha256=digest,
            size=size,
        )
        identities[symbol] = identity
        rows.append(
            {
                "symbol": symbol,
                "sha256": digest,
                "size": size,
            }
        )
    manifest = {
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
        "files": rows,
    }
    return SourceSnapshot(
        manifest=manifest,
        sha256=_canonical_sha256(manifest),
        files=identities,
    )


@dataclass(slots=True)
class PendingEntry:
    proposal: EntryProposal
    decision_bid: float
    decision_ask: float
    decision_epoch: int


@dataclass(slots=True)
class OpenPosition:
    trial_id: str
    symbol: str
    side: str
    decision_epoch: int
    fill_epoch: int
    entry_epoch: float
    decision_bid: float
    decision_ask: float
    next_open_bid: float
    next_open_ask: float
    entry_price: float
    initial_sl_price: float
    initial_tp_price: float
    bars_held: int
    time_stop_bars: int
    source_snapshot_sha256: str


@dataclass(slots=True)
class RawTrade:
    trial_id: str
    symbol: str
    side: str
    decision_epoch: int
    fill_epoch: int
    entry_epoch: float
    exit_epoch: float
    decision_bid: float
    decision_ask: float
    next_open_bid: float
    next_open_ask: float
    entry_price: float
    initial_sl_price: float
    initial_tp_price: float
    exit_price: float
    exit_reason: str
    source_snapshot_sha256: str


@dataclass(slots=True)
class TrialState:
    definition: TrialDefinition
    pending: PendingEntry | None = None
    position: OpenPosition | None = None
    entry_days: set[str] = field(default_factory=set)
    trades: list[RawTrade] = field(default_factory=list)


@dataclass(slots=True)
class DailyPanelRow:
    first_mid_open: float
    last_mid_close: float
    signed_exposure_sum: float = 0.0
    bars: int = 0

    def add(self, *, mid_close: float, signed_exposure: float) -> None:
        self.last_mid_close = mid_close
        self.signed_exposure_sum += signed_exposure
        self.bars += 1


@dataclass(frozen=True, slots=True)
class SignalFeatures:
    atr_bps: float
    disp_z: float
    bar_direction: float


def _signal_features(history: Sequence[EntryBar], policy: DislocationPolicy) -> SignalFeatures:
    bars = tuple(history)[-int(policy.min_history_bars) :]
    true_ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        / current.close
        * 1e4
        for previous, current in zip(bars, bars[1:])
    ]
    atr_values = true_ranges[-int(policy.atr_bars) :]
    atr = math.fsum(atr_values) / len(atr_values) if atr_values else 0.0
    weight = 2.0 / (float(int(policy.ema_bars)) + 1.0)
    average = bars[0].close
    for bar in bars[1:]:
        average = bar.close * weight + average * (1.0 - weight)
    last = bars[-1]
    disp = ((last.close - average) / average * 1e4) / atr if atr > 0.0 else 0.0
    return SignalFeatures(
        atr_bps=atr,
        disp_z=disp,
        bar_direction=last.close - last.open,
    )


def _fast_policy_can_signal(
    *, policy: DislocationPolicy, features: SignalFeatures, spread_bps: float
) -> bool:
    if features.atr_bps < float(policy.atr_floor_bps):
        return False
    if abs(features.disp_z) < float(policy.z_entry):
        return False
    if policy.signal_mode == "momentum":
        if (features.disp_z > 0.0 and features.bar_direction <= 0.0) or (
            features.disp_z < 0.0 and features.bar_direction >= 0.0
        ):
            return False
    else:
        if (features.disp_z > 0.0 and features.bar_direction >= 0.0) or (
            features.disp_z < 0.0 and features.bar_direction <= 0.0
        ):
            return False
    stop_bps = max(
        float(policy.sl_atr_mult) * features.atr_bps,
        float(policy.min_stop_bps),
    )
    target_bps = float(policy.tp_atr_mult) * features.atr_bps
    p_star = (stop_bps + max(0.0, spread_bps)) / (target_bps + stop_bps)
    if p_star > float(policy.p_star_max):
        return False
    return not (
        float(policy.min_tp_cost_ratio) > 0.0
        and spread_bps > 0.0
        and target_bps < float(policy.min_tp_cost_ratio) * spread_bps
    )


@dataclass(frozen=True, slots=True)
class SymbolReplayResult:
    symbol: str
    trial_trades: Mapping[str, tuple[RawTrade, ...]]
    daily_panel: Mapping[str, DailyPanelRow]
    source_samples: int
    source_days: int
    source_first_epoch: int
    source_last_epoch: int
    source_spreads_bps: tuple[float, ...]


@dataclass(slots=True)
class SourceRawWriters:
    root: Path
    handles: dict[str, BinaryIO] = field(init=False)
    count: int = 0

    def __post_init__(self) -> None:
        self.handles = {
            name: (self.root / f"source_{name}.raw").open("wb")
            for name in (
                "symbol_index",
                "minute_epoch",
                "bid_open",
                "ask_open",
                "bid_close",
                "ask_close",
            )
        }

    def append(self, *, symbol_index: int, bar: HistoricalBar) -> None:
        self.handles["symbol_index"].write(struct.pack("<h", symbol_index))
        self.handles["minute_epoch"].write(struct.pack("<q", bar.minute_epoch))
        for name, value in (
            ("bid_open", bar.bid_open),
            ("ask_open", bar.ask_open),
            ("bid_close", bar.bid_close),
            ("ask_close", bar.ask_close),
        ):
            self.handles[name].write(struct.pack("<d", value))
        self.count += 1

    def close(self) -> None:
        for handle in self.handles.values():
            handle.close()

    def arrays(self) -> dict[str, RawNpyArray]:
        return {
            "symbol_index": RawNpyArray(
                descr="<i2",
                shape=(self.count,),
                path=self.root / "source_symbol_index.raw",
            ),
            "minute_epoch": RawNpyArray(
                descr="<i8",
                shape=(self.count,),
                path=self.root / "source_minute_epoch.raw",
            ),
            **{
                name: RawNpyArray(
                    descr="<f8",
                    shape=(self.count,),
                    path=self.root / f"source_{name}.raw",
                )
                for name in ("bid_open", "ask_open", "bid_close", "ask_close")
            },
        }


def _signed_position(position: OpenPosition | None) -> float:
    if position is None:
        return 0.0
    return 1.0 if position.side == "BUY" else -1.0


def _close_position(
    state: TrialState,
    *,
    exit_price: float,
    exit_reason: str,
    exit_epoch: float,
) -> None:
    position = state.position
    if position is None:
        raise EvidenceRefusal("replay_close_without_position")
    if exit_epoch <= position.entry_epoch or exit_price <= 0.0:
        raise EvidenceRefusal("replay_exit_invalid")
    state.trades.append(
        RawTrade(
            trial_id=position.trial_id,
            symbol=position.symbol,
            side=position.side,
            decision_epoch=position.decision_epoch,
            fill_epoch=position.fill_epoch,
            entry_epoch=position.entry_epoch,
            exit_epoch=exit_epoch,
            decision_bid=position.decision_bid,
            decision_ask=position.decision_ask,
            next_open_bid=position.next_open_bid,
            next_open_ask=position.next_open_ask,
            entry_price=position.entry_price,
            initial_sl_price=position.initial_sl_price,
            initial_tp_price=position.initial_tp_price,
            exit_price=exit_price,
            exit_reason=exit_reason,
            source_snapshot_sha256=position.source_snapshot_sha256,
        )
    )
    state.position = None


def _fill_pending(
    state: TrialState,
    bar: HistoricalBar,
    *,
    source_file_sha256: str,
) -> bool:
    pending = state.pending
    if pending is None:
        return False
    state.pending = None
    if bar.minute_epoch != pending.decision_epoch + 60:
        return False
    day = _utc_day(bar.minute_epoch)
    if day in state.entry_days:
        return False
    proposal = pending.proposal
    if proposal.side not in {"BUY", "SELL"}:
        raise EvidenceRefusal("replay_pending_side_invalid")
    if (
        proposal.entry_price is None
        or proposal.ref_mid is None
        or proposal.stop_bps is None
        or proposal.target_bps is None
        or proposal.time_stop_bars is None
    ):
        raise EvidenceRefusal("replay_pending_geometry_missing")
    if proposal.side == "BUY":
        entry = max(float(proposal.entry_price), bar.ask_open)
    else:
        entry = min(float(proposal.entry_price), bar.bid_open)
    stop_distance = float(proposal.stop_bps) / 1e4 * float(proposal.ref_mid)
    target_distance = float(proposal.target_bps) / 1e4 * float(proposal.ref_mid)
    if proposal.side == "BUY":
        stop = entry - stop_distance
        target = entry + target_distance
    else:
        stop = entry + stop_distance
        target = entry - target_distance
    if min(entry, stop, target) <= 0.0:
        raise EvidenceRefusal("replay_entry_geometry_invalid")
    state.position = OpenPosition(
        trial_id=state.definition.attempt_id,
        symbol=bar.symbol,
        side=proposal.side,
        decision_epoch=pending.decision_epoch,
        fill_epoch=bar.minute_epoch,
        entry_epoch=float(bar.minute_epoch),
        decision_bid=pending.decision_bid,
        decision_ask=pending.decision_ask,
        next_open_bid=bar.bid_open,
        next_open_ask=bar.ask_open,
        entry_price=entry,
        initial_sl_price=stop,
        initial_tp_price=target,
        bars_held=0,
        time_stop_bars=int(proposal.time_stop_bars),
        source_snapshot_sha256=source_file_sha256,
    )
    state.entry_days.add(day)
    return True


def _manage_position(
    state: TrialState,
    bar: HistoricalBar,
    *,
    entry_bar: bool,
) -> None:
    position = state.position
    if position is None:
        return
    buy = position.side == "BUY"
    if not entry_bar:
        adverse_open = bar.bid_open if buy else bar.ask_open
        opened_through_stop = (
            adverse_open <= position.initial_sl_price
            if buy
            else adverse_open >= position.initial_sl_price
        )
        if opened_through_stop:
            _close_position(
                state,
                exit_price=adverse_open,
                exit_reason="STOP_LOSS",
                exit_epoch=float(bar.minute_epoch),
            )
            return
        opened_through_target = (
            adverse_open >= position.initial_tp_price
            if buy
            else adverse_open <= position.initial_tp_price
        )
        if opened_through_target:
            _close_position(
                state,
                exit_price=position.initial_tp_price,
                exit_reason="TAKE_PROFIT",
                exit_epoch=float(bar.minute_epoch),
            )
            return
    adverse_extreme = bar.bid_low if buy else bar.ask_high
    favorable_extreme = bar.bid_high if buy else bar.ask_low
    stop_hit = (
        adverse_extreme <= position.initial_sl_price
        if buy
        else adverse_extreme >= position.initial_sl_price
    )
    # Not crediting a target on the entry bar is intentionally conservative:
    # M1 OHLC cannot prove whether its favorable extreme followed the fill.
    target_hit = False if entry_bar else (
        favorable_extreme >= position.initial_tp_price
        if buy
        else favorable_extreme <= position.initial_tp_price
    )
    if stop_hit:
        _close_position(
            state,
            exit_price=position.initial_sl_price,
            exit_reason="STOP_LOSS",
            exit_epoch=float(bar.minute_epoch + 60),
        )
        return
    if target_hit:
        _close_position(
            state,
            exit_price=position.initial_tp_price,
            exit_reason="TAKE_PROFIT",
            exit_epoch=float(bar.minute_epoch + 60),
        )
        return
    position.bars_held += 1
    if position.bars_held >= position.time_stop_bars:
        _close_position(
            state,
            exit_price=bar.bid_close if buy else bar.ask_close,
            exit_reason="TIME_STOP",
            exit_epoch=float(bar.minute_epoch + 60),
        )


def _evaluate_candidate(
    state: TrialState,
    *,
    history: Sequence[EntryBar],
    bar: HistoricalBar,
    features: SignalFeatures,
) -> bool:
    if state.pending is not None or state.position is not None:
        return False
    if _utc_day(bar.minute_epoch) in state.entry_days:
        return False
    required = int(state.definition.policy.min_history_bars)
    if len(history) < required:
        return False
    if not _fast_policy_can_signal(
        policy=state.definition.policy,
        features=features,
        spread_bps=bar.close_spread_bps,
    ):
        return False
    selected = tuple(history)[-required:]
    request = EntryEvaluationRequest(
        symbol=bar.symbol,
        bars=selected,
        spread_bps=bar.close_spread_bps,
    )
    proposal = evaluate_dislocation(request, state.definition.policy)
    if not proposal.allowed:
        return False
    state.pending = PendingEntry(
        proposal=proposal,
        decision_bid=bar.bid_close,
        decision_ask=bar.ask_close,
        decision_epoch=bar.minute_epoch,
    )
    return True


def _run_symbol(
    *,
    symbol: str,
    symbol_index: int,
    source: SourceFileIdentity,
    start_epoch: int | None,
    end_epoch: int | None,
    source_writer: SourceRawWriters,
    trials: Sequence[TrialDefinition] = FIXED_TRIALS,
) -> SymbolReplayResult:
    if not trials:
        raise EvidenceRefusal("replay_trial_set_empty")
    states = [TrialState(definition=trial) for trial in trials]
    selected = states[0]
    max_history = max(int(trial.policy.min_history_bars) for trial in trials)
    history: deque[EntryBar] = deque(maxlen=max_history)
    previous_epoch: int | None = None
    daily: dict[str, DailyPanelRow] = {}
    sampled_spreads: list[float] = []
    sampled_days: set[str] = set()
    first_sample = 0
    last_sample = 0
    bars_seen = 0
    for bar in _historical_bars(
        source.path,
        symbol=symbol,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
    ):
        bars_seen += 1
        gap = previous_epoch is not None and bar.minute_epoch != previous_epoch + 60
        previous_epoch = bar.minute_epoch
        if gap:
            history.clear()
            for state in states:
                state.pending = None
                if state.position is not None:
                    raise EvidenceRefusal(
                        f"historical_gap_during_open_trade:{symbol}:{bar.minute_epoch}:"
                        f"{state.definition.attempt_id}"
                    )

        selected_fill = False
        exposure_at_open = _signed_position(selected.position)
        for state in states:
            filled = _fill_pending(
                state,
                bar,
                source_file_sha256=source.sha256,
            )
            if state is selected and filled:
                selected_fill = True
                exposure_at_open = _signed_position(selected.position)
            _manage_position(state, bar, entry_bar=filled)

        day = _utc_day(bar.minute_epoch)
        panel = daily.get(day)
        if panel is None:
            panel = DailyPanelRow(
                first_mid_open=bar.mid_open,
                last_mid_close=bar.mid_close,
            )
            daily[day] = panel
        panel.add(mid_close=bar.mid_close, signed_exposure=exposure_at_open)

        selected_decision = False
        if not bar.strategy_valid:
            history.clear()
        else:
            history.append(bar.entry_bar())
            features = (
                _signal_features(history, FIXED_TRIALS[0].policy)
                if len(history) >= max_history
                else None
            )
            for state in states:
                if features is None:
                    continue
                decided = _evaluate_candidate(
                    state, history=history, bar=bar, features=features
                )
                if state is selected and decided:
                    selected_decision = True

        include_source_sample = (
            bar.minute_epoch % SOURCE_SAMPLE_SECONDS == 0
            or selected_fill
            or selected_decision
        )
        if include_source_sample:
            source_writer.append(symbol_index=symbol_index, bar=bar)
            sampled_spreads.append(bar.open_spread_bps)
            sampled_days.add(day)
            first_sample = first_sample or bar.minute_epoch
            last_sample = bar.minute_epoch

    if bars_seen == 0:
        raise EvidenceRefusal(f"historical_source_range_empty:{symbol}")
    for state in states:
        if state.pending is not None:
            state.pending = None
        if state.position is not None:
            raise EvidenceRefusal(
                f"historical_end_during_open_trade:{symbol}:{state.definition.attempt_id}"
            )
    if len(sampled_days) < MIN_SOURCE_DAYS:
        raise EvidenceRefusal(
            f"historical_source_days_insufficient:{symbol}:{len(sampled_days)}"
        )
    return SymbolReplayResult(
        symbol=symbol,
        trial_trades={
            state.definition.attempt_id: tuple(state.trades) for state in states
        },
        daily_panel=daily,
        source_samples=len(sampled_spreads),
        source_days=len(sampled_days),
        source_first_epoch=first_sample,
        source_last_epoch=last_sample,
        source_spreads_bps=tuple(sampled_spreads),
    )


@dataclass(frozen=True, slots=True)
class IgSymbolCalibration:
    observations: int
    first_epoch: float
    last_epoch: float
    duration_secs: float
    max_gap_secs: float
    median_spread_bps: float
    p90_spread_bps: float
    max_spread_bps: float
    point: float
    price_tick_size: float
    digits: int
    trade_allowed: bool


@dataclass(frozen=True, slots=True)
class IgCalibration:
    capture: Mapping[str, Any]
    npz_path: Path
    npz_sha256: str
    symbols: Mapping[str, IgSymbolCalibration]


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    payload: Mapping[str, Any]
    path: Path
    source_document_path: Path
    rows: Mapping[str, Mapping[str, float]]


_IG_CAPTURE_FIELDS = {
    "schema_version",
    "capture_definition",
    "capture_mode",
    "source_errors",
    "venue_id",
    "account_mode",
    "source_id",
    "source_version",
    "scope_version",
    "symbol_scope",
    "capture_start_epoch",
    "capture_end_epoch",
    "created_at_epoch",
    "account_scope_sha256",
    "terminal_producer_instance_sha256",
    "market_source_audit",
    "market_source_audit_sha256",
    "broker_contract_audit",
    "broker_contract_snapshot_sha256",
    "point_in_time_audit",
    "point_in_time_audit_sha256",
    "execution_contract",
    "npz_path",
    "npz_sha256",
    "npz_size_bytes",
    "npz_arrays",
    "symbols",
    "capture_payload_sha256",
}
_IG_ARRAY_NAMES = {
    "symbol_index",
    "sample_epoch",
    "broker_quote_epoch",
    "received_at_epoch",
    "market_event_received_at_epoch",
    "source_event_sequence",
    "source_event_token_sha256",
    "bid",
    "ask",
    "point",
    "price_tick_size",
    "digits",
    "trade_allowed",
}


def _capture_body_sha256(payload: Mapping[str, Any]) -> str:
    return _canonical_sha256(
        {key: value for key, value in payload.items() if key != "capture_payload_sha256"}
    )


def _mapping_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, Mapping):
        raise EvidenceRefusal(f"{label}_invalid")
    return _canonical_sha256(dict(value))


def _contract_row(audit: Mapping[str, Any], symbol: str) -> Mapping[str, Any]:
    for container_name in ("symbols", "contracts", "instruments"):
        container = audit.get(container_name)
        if isinstance(container, Mapping) and isinstance(container.get(symbol), Mapping):
            return dict(container[symbol])
        if isinstance(container, list):
            for item in container:
                if (
                    isinstance(item, Mapping)
                    and str(item.get("symbol") or item.get("canonical_symbol") or "").upper()
                    == symbol
                ):
                    return dict(item)
    raise EvidenceRefusal(f"ig_capture_broker_contract_missing:{symbol}")


def _fee_from_contract(row: Mapping[str, Any], *, names: Sequence[str], label: str) -> float:
    for name in names:
        if name in row:
            return _nonnegative(row[name], label=label)
    raise EvidenceRefusal(f"{label}_missing")


def _load_ig_capture(path: Path) -> IgCalibration:
    if not path.is_file() or path.is_symlink():
        raise EvidenceRefusal("ig_capture_json_invalid")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceRefusal("ig_capture_json_invalid") from exc
    if not isinstance(decoded, Mapping) or set(decoded) != _IG_CAPTURE_FIELDS:
        raise EvidenceRefusal("ig_capture_scope_invalid")
    capture = dict(decoded)
    capture_mode = str(capture.get("capture_mode") or "")
    if (
        capture.get("schema_version") != IG_CAPTURE_SCHEMA
        or capture.get("capture_definition") != IG_CALIBRATION_DEFINITION
        or capture.get("source_errors") != []
        or capture.get("venue_id") != IG_MT4_VENUE_ID
        or capture.get("account_mode") != "demo"
        or capture.get("source_id") != "authenticated_ig_demo_mt4_bridge"
        or not str(capture.get("source_version") or "").strip()
        or capture.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or capture.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or capture_mode
        not in {"live_endpoint", "authenticated_same_source_db_history"}
    ):
        raise EvidenceRefusal("ig_capture_identity_invalid")
    claimed_body_sha = str(capture.get("capture_payload_sha256") or "").lower()
    if not _is_sha256(claimed_body_sha) or claimed_body_sha != _capture_body_sha256(
        capture
    ):
        raise EvidenceRefusal("ig_capture_payload_sha256_invalid")
    for value_field, sha_field in (
        ("market_source_audit", "market_source_audit_sha256"),
        ("broker_contract_audit", "broker_contract_snapshot_sha256"),
        ("point_in_time_audit", "point_in_time_audit_sha256"),
    ):
        claimed = str(capture.get(sha_field) or "").lower()
        if not _is_sha256(claimed) or claimed != _mapping_sha256(
            capture.get(value_field), label=f"ig_capture_{value_field}"
        ):
            raise EvidenceRefusal(f"ig_capture_{sha_field}_invalid")
    point_in_time_audit = capture.get("point_in_time_audit")
    if not isinstance(point_in_time_audit, Mapping):
        raise EvidenceRefusal("ig_capture_point_in_time_audit_invalid")
    expected_gap_enforced = capture_mode == "live_endpoint"
    expected_database_read_only = (
        capture_mode == "authenticated_same_source_db_history"
    )
    audit_minimum_samples = _strict_int(
        point_in_time_audit.get("minimum_samples_per_symbol"),
        label="ig_capture_minimum_samples",
        minimum=0,
    )
    audit_minimum_duration = _positive(
        point_in_time_audit.get("minimum_duration_secs"),
        label="ig_capture_minimum_duration",
    )
    audit_maximum_gap = _positive(
        point_in_time_audit.get("maximum_sample_gap_secs"),
        label="ig_capture_maximum_gap",
    )
    required_audit_samples = (
        MIN_IG_HISTORY_SAMPLES_PER_SYMBOL
        if expected_database_read_only
        else MIN_IG_SAMPLES_PER_SYMBOL
    )
    if (
        point_in_time_audit.get("passed") is not True
        or point_in_time_audit.get("errors") != []
        or audit_minimum_samples < required_audit_samples
        or audit_minimum_duration < MIN_IG_CAPTURE_DURATION_SECS
        or (
            expected_gap_enforced
            and audit_maximum_gap > MAX_IG_SAMPLE_GAP_SECS
        )
        or point_in_time_audit.get("maximum_sample_gap_enforced")
        is not expected_gap_enforced
        or point_in_time_audit.get("requires_fresh_authenticated_source_events")
        is not True
        or point_in_time_audit.get("latest_scope_market_event_fresh") is not True
        or point_in_time_audit.get("database_read_only")
        is not expected_database_read_only
        or point_in_time_audit.get("sample_source") != capture_mode
    ):
        raise EvidenceRefusal("ig_capture_point_in_time_audit_invalid")
    for field_name in (
        "account_scope_sha256",
        "terminal_producer_instance_sha256",
    ):
        if not _is_sha256(capture.get(field_name)):
            raise EvidenceRefusal(f"ig_capture_{field_name}_invalid")
    start = _positive(capture.get("capture_start_epoch"), label="ig_capture_start")
    end = _positive(capture.get("capture_end_epoch"), label="ig_capture_end")
    created = _positive(capture.get("created_at_epoch"), label="ig_capture_created")
    if (
        end < start
        or (
            capture_mode == "live_endpoint"
            and end - start < MIN_IG_CAPTURE_DURATION_SECS
        )
        or created < end
    ):
        raise EvidenceRefusal("ig_capture_window_invalid")
    execution = capture.get("execution_contract")
    if not isinstance(execution, Mapping):
        raise EvidenceRefusal("ig_capture_execution_contract_invalid")
    if (
        execution.get("max_slippage_points") != EXECUTION_MAX_SLIPPAGE_POINTS
        or execution.get("semantics")
        != "configured_broker_execution_tolerance_not_observed_slippage"
        or execution.get("used_as_observed_cost") is not False
    ):
        raise EvidenceRefusal("ig_capture_execution_contract_invalid")
    raw_npz_name = str(capture.get("npz_path") or "").strip()
    relative = Path(raw_npz_name)
    if (
        not raw_npz_name
        or relative.is_absolute()
        or len(relative.parts) != 1
        or relative.name != raw_npz_name
    ):
        raise EvidenceRefusal("ig_capture_npz_path_invalid")
    npz_path = (path.parent / relative).resolve(strict=True)
    if npz_path.parent != path.parent.resolve(strict=True):
        raise EvidenceRefusal("ig_capture_npz_path_invalid")
    npz_sha = _file_sha256(npz_path)
    if (
        not _is_sha256(capture.get("npz_sha256"))
        or npz_sha != str(capture["npz_sha256"]).lower()
        or npz_path.stat().st_size != capture.get("npz_size_bytes")
    ):
        raise EvidenceRefusal("ig_capture_npz_identity_invalid")
    declaration = capture.get("npz_arrays")
    expected_declaration = {
        "symbol_index": "int64",
        "sample_epoch": "float64",
        "broker_quote_epoch": "float64",
        "received_at_epoch": "float64",
        "market_event_received_at_epoch": "float64",
        "source_event_sequence": "int64",
        "source_event_token_sha256": "S64",
        "bid": "float64",
        "ask": "float64",
        "point": "float64",
        "price_tick_size": "float64",
        "digits": "int64",
        "trade_allowed": "bool",
    }
    if declaration != expected_declaration:
        raise EvidenceRefusal("ig_capture_npz_declaration_invalid")
    arrays = _read_npz(npz_path, expected_names=_IG_ARRAY_NAMES)
    count = math.prod(arrays["symbol_index"].shape)
    if count <= 0 or any(array.shape != (count,) for array in arrays.values()):
        raise EvidenceRefusal("ig_capture_npz_shape_invalid")
    expected_descr = {
        "symbol_index": "<i8",
        "sample_epoch": "<f8",
        "broker_quote_epoch": "<f8",
        "received_at_epoch": "<f8",
        "market_event_received_at_epoch": "<f8",
        "source_event_sequence": "<i8",
        "source_event_token_sha256": "|S64",
        "bid": "<f8",
        "ask": "<f8",
        "point": "<f8",
        "price_tick_size": "<f8",
        "digits": "<i8",
        "trade_allowed": "|b1",
    }
    if any(arrays[name].descr != descr for name, descr in expected_descr.items()):
        raise EvidenceRefusal("ig_capture_npz_dtype_invalid")
    values = {name: arrays[name].values() for name in _IG_ARRAY_NAMES}
    broker_audit = capture.get("broker_contract_audit")
    assert isinstance(broker_audit, Mapping)
    calibrated: dict[str, IgSymbolCalibration] = {}
    minimum_samples = (
        MIN_IG_HISTORY_SAMPLES_PER_SYMBOL
        if capture_mode == "authenticated_same_source_db_history"
        else MIN_IG_SAMPLES_PER_SYMBOL
    )
    for symbol_index, symbol in enumerate(IG_MT4_SCALP_SYMBOLS):
        positions = [
            index
            for index, observed in enumerate(values["symbol_index"])
            if int(observed) == symbol_index
        ]
        if len(positions) < minimum_samples:
            raise EvidenceRefusal(
                f"ig_capture_samples_insufficient:{symbol}:{len(positions)}"
            )
        epochs = [float(values["sample_epoch"][index]) for index in positions]
        if any(not math.isfinite(epoch) or epoch <= 0.0 for epoch in epochs) or any(
            current <= previous for previous, current in zip(epochs, epochs[1:])
        ):
            raise EvidenceRefusal(f"ig_capture_epoch_invalid:{symbol}")
        if capture_mode == "live_endpoint" and any(
            epoch < start or epoch > end for epoch in epochs
        ):
            raise EvidenceRefusal(f"ig_capture_epoch_invalid:{symbol}")
        if capture_mode == "authenticated_same_source_db_history" and any(
            epoch > start + 5.0 for epoch in epochs
        ):
            raise EvidenceRefusal(f"ig_capture_epoch_invalid:{symbol}")
        gaps = [current - previous for previous, current in zip(epochs, epochs[1:])]
        duration = epochs[-1] - epochs[0]
        maximum_gap = max(gaps, default=0.0)
        if duration < MIN_IG_CAPTURE_DURATION_SECS or (
            capture_mode == "live_endpoint"
            and maximum_gap > MAX_IG_SAMPLE_GAP_SECS
        ):
            raise EvidenceRefusal(f"ig_capture_freshness_invalid:{symbol}")
        spreads: list[float] = []
        points: set[float] = set()
        ticks: set[float] = set()
        digits_values: set[int] = set()
        previous_sequence: int | None = None
        for index in positions:
            sample_epoch = float(values["sample_epoch"][index])
            broker_epoch = float(values["broker_quote_epoch"][index])
            received_epoch = float(values["received_at_epoch"][index])
            market_received = float(values["market_event_received_at_epoch"][index])
            sequence = int(values["source_event_sequence"][index])
            token = values["source_event_token_sha256"][index]
            bid = float(values["bid"][index])
            ask = float(values["ask"][index])
            point = float(values["point"][index])
            tick = float(values["price_tick_size"][index])
            digits = int(values["digits"][index])
            allowed = bool(values["trade_allowed"][index])
            if (
                not all(
                    math.isfinite(item)
                    for item in (
                        sample_epoch,
                        broker_epoch,
                        received_epoch,
                        market_received,
                        bid,
                        ask,
                        point,
                        tick,
                    )
                )
                or bid <= 0.0
                or ask < bid
                or point <= 0.0
                or tick <= 0.0
                or digits < 0
                or not allowed
                or sequence < 0
                or (previous_sequence is not None and sequence <= previous_sequence)
                or not isinstance(token, bytes)
                or not _is_sha256(token.decode("ascii", errors="ignore"))
                or broker_epoch > received_epoch + 5.0
                or market_received > received_epoch + 1e-6
                or received_epoch > sample_epoch + 5.0
                or sample_epoch - received_epoch > 5.0
                or sample_epoch - market_received > 5.0
            ):
                raise EvidenceRefusal(f"ig_capture_event_invalid:{symbol}:{index}")
            previous_sequence = sequence
            mid = (bid + ask) / 2.0
            spreads.append((ask - bid) / mid * 1e4)
            points.add(point)
            ticks.add(tick)
            digits_values.add(digits)
        if len(points) != 1 or len(ticks) != 1 or len(digits_values) != 1:
            raise EvidenceRefusal(f"ig_capture_contract_changed:{symbol}")
        calibrated[symbol] = IgSymbolCalibration(
            observations=len(positions),
            first_epoch=epochs[0],
            last_epoch=epochs[-1],
            duration_secs=duration,
            max_gap_secs=maximum_gap,
            median_spread_bps=_median(spreads),
            p90_spread_bps=_linear_quantile(spreads, 0.90),
            max_spread_bps=max(spreads),
            point=next(iter(points)),
            price_tick_size=next(iter(ticks)),
            digits=next(iter(digits_values)),
            trade_allowed=True,
        )
    observed_indices = {int(item) for item in values["symbol_index"]}
    if observed_indices != set(range(len(IG_MT4_SCALP_SYMBOLS))):
        raise EvidenceRefusal("ig_capture_symbol_index_scope_invalid")
    return IgCalibration(
        capture=capture,
        npz_path=npz_path,
        npz_sha256=npz_sha,
        symbols=calibrated,
    )


def _load_fee_schedule(path: Path) -> FeeSchedule:
    if not path.is_file() or path.is_symlink():
        raise EvidenceRefusal("fee_schedule_file_invalid")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceRefusal("fee_schedule_json_invalid") from exc
    expected = {
        "schema_version",
        "source_errors",
        "venue_id",
        "account_mode",
        "symbol_scope",
        "effective_at_epoch",
        "source_document_path",
        "source_document_sha256",
        "operator_attestation_sha256",
        "symbols",
    }
    if not isinstance(decoded, Mapping) or set(decoded) != expected:
        raise EvidenceRefusal("fee_schedule_scope_invalid")
    payload = dict(decoded)
    body = {
        key: value
        for key, value in payload.items()
        if key != "operator_attestation_sha256"
    }
    if (
        payload.get("schema_version") != "fxstack.external_ig_mt4_fee_schedule.v1"
        or payload.get("source_errors") != []
        or payload.get("venue_id") != IG_MT4_VENUE_ID
        or payload.get("account_mode") != "demo"
        or payload.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or _positive(payload.get("effective_at_epoch"), label="fee_effective") <= 0.0
        or not _is_sha256(payload.get("source_document_sha256"))
        or str(payload.get("operator_attestation_sha256") or "").lower()
        != _canonical_sha256(body)
    ):
        raise EvidenceRefusal("fee_schedule_identity_invalid")
    source_name = str(payload.get("source_document_path") or "").strip()
    if Path(source_name).name != source_name:
        raise EvidenceRefusal("fee_schedule_source_path_invalid")
    source_path = (path.parent / source_name).resolve(strict=True)
    if source_path.parent != path.parent.resolve(strict=True) or not source_path.is_file():
        raise EvidenceRefusal("fee_schedule_source_path_invalid")
    if _file_sha256(source_path) != str(payload["source_document_sha256"]).lower():
        raise EvidenceRefusal("fee_schedule_source_sha256_invalid")
    raw_rows = payload.get("symbols")
    if not isinstance(raw_rows, Mapping) or set(raw_rows) != set(IG_MT4_SCALP_SYMBOLS):
        raise EvidenceRefusal("fee_schedule_symbol_scope_invalid")
    rows: dict[str, dict[str, float]] = {}
    for symbol in IG_MT4_SCALP_SYMBOLS:
        raw = raw_rows[symbol]
        if not isinstance(raw, Mapping) or set(raw) != {
            "commission_bps_per_round_trip",
            "configured_financing_bps_per_trade",
        }:
            raise EvidenceRefusal(f"fee_schedule_symbol_invalid:{symbol}")
        rows[symbol] = {
            "commission_bps_per_round_trip": _nonnegative(
                raw["commission_bps_per_round_trip"],
                label=f"fee_commission:{symbol}",
            ),
            "configured_financing_bps_per_trade": _nonnegative(
                raw["configured_financing_bps_per_trade"],
                label=f"fee_financing:{symbol}",
            ),
        }
    return FeeSchedule(
        payload=payload,
        path=path.resolve(strict=True),
        source_document_path=source_path,
        rows=rows,
    )


def _sample_standard_deviation(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = math.fsum(values) / len(values)
    variance = math.fsum((value - mean) ** 2 for value in values) / (
        len(values) - 1
    )
    return math.sqrt(max(0.0, variance))


def _period_sharpe(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    standard_deviation = _sample_standard_deviation(values)
    if standard_deviation <= 0.0 or not math.isfinite(standard_deviation):
        return 0.0
    return float((math.fsum(values) / len(values)) / standard_deviation)


def _skewness(values: Sequence[float]) -> float:
    if len(values) < 3:
        return 0.0
    mean = math.fsum(values) / len(values)
    standard_deviation = _sample_standard_deviation(values)
    if standard_deviation <= 0.0:
        return 0.0
    return float(
        math.fsum(((value - mean) / standard_deviation) ** 3 for value in values)
        / len(values)
    )


def _kurtosis(values: Sequence[float]) -> float:
    if len(values) < 4:
        return 3.0
    mean = math.fsum(values) / len(values)
    standard_deviation = _sample_standard_deviation(values)
    if standard_deviation <= 0.0:
        return 3.0
    return float(
        math.fsum(((value - mean) / standard_deviation) ** 4 for value in values)
        / len(values)
    )


def _sharpe_variance(sharpes: Sequence[float]) -> float:
    standard_deviation = _sample_standard_deviation(sharpes)
    return float(standard_deviation * standard_deviation)


def _expected_max_sharpe(*, trials: int, variance: float) -> float:
    if trials <= 1 or variance <= 0.0:
        return 0.0
    normal = __import__("statistics").NormalDist()
    euler_mascheroni = 0.5772156649015329
    z_one = normal.inv_cdf(1.0 - 1.0 / trials)
    z_two = normal.inv_cdf(1.0 - 1.0 / (trials * math.e))
    return math.sqrt(variance) * (
        (1.0 - euler_mascheroni) * z_one + euler_mascheroni * z_two
    )


def _deflated_sharpe(
    *,
    selected_returns: Sequence[float],
    selected_sharpe: float,
    trial_sharpes: Sequence[float],
) -> tuple[float, float]:
    variance = _sharpe_variance(trial_sharpes)
    if len(selected_returns) < 3:
        return 0.0, variance
    benchmark = _expected_max_sharpe(
        trials=len(trial_sharpes), variance=variance
    )
    skew = _skewness(selected_returns)
    kurtosis = _kurtosis(selected_returns)
    variance_term = (
        1.0
        - skew * selected_sharpe
        + ((kurtosis - 1.0) / 4.0) * (selected_sharpe**2)
    )
    if variance_term <= 0.0 or not math.isfinite(variance_term):
        return 0.0, variance
    z_value = (
        (selected_sharpe - benchmark)
        * math.sqrt(len(selected_returns) - 1)
        / math.sqrt(variance_term)
    )
    normal = __import__("statistics").NormalDist()
    return float(normal.cdf(z_value)), variance


def _column_sharpes(
    matrix: Sequence[Sequence[float]], rows: Sequence[int] | None = None
) -> list[float]:
    if not matrix:
        return []
    selected_rows = list(range(len(matrix))) if rows is None else list(rows)
    columns = len(matrix[0])
    return [
        _period_sharpe([float(matrix[row][column]) for row in selected_rows])
        for column in range(columns)
    ]


def _pbo(
    matrix: Sequence[Sequence[float]], *, n_splits: int, max_combinations: int
) -> float:
    observations = len(matrix)
    configurations = len(matrix[0]) if matrix else 0
    if observations < 2 * n_splits or configurations < 2:
        raise EvidenceRefusal("pbo_observations_insufficient")
    splits = int(n_splits)
    if splits % 2:
        splits -= 1
    bounds = [(observations * index) // splits for index in range(splits + 1)]
    blocks = [list(range(bounds[index], bounds[index + 1])) for index in range(splits)]
    combinations = list(itertools.combinations(range(splits), splits // 2))
    if len(combinations) > max_combinations:
        step = max(1, len(combinations) // max_combinations)
        combinations = combinations[::step][:max_combinations]
    below_median = 0
    for in_sample_blocks in combinations:
        selected_blocks = set(in_sample_blocks)
        in_sample_rows = [
            row for index in in_sample_blocks for row in blocks[index]
        ]
        out_sample_rows = [
            row
            for index in range(splits)
            if index not in selected_blocks
            for row in blocks[index]
        ]
        in_sample = _column_sharpes(matrix, in_sample_rows)
        out_sample = _column_sharpes(matrix, out_sample_rows)
        best = max(range(configurations), key=lambda index: in_sample[index])
        rank = sum(value <= out_sample[best] for value in out_sample)
        omega = rank / float(configurations + 1)
        omega = min(
            max(omega, 1.0 / float(configurations + 1)),
            1.0 - 1.0 / float(configurations + 1),
        )
        if math.log(omega / (1.0 - omega)) < 0.0:
            below_median += 1
    if not combinations:
        raise EvidenceRefusal("pbo_combinations_empty")
    return float(below_median / len(combinations))


def _mcpt_statistic(
    exposure: Sequence[Sequence[float]],
    returns: Sequence[Sequence[float]],
    costs: Sequence[Sequence[float]],
    *,
    shift: int = 0,
) -> float:
    observations = len(exposure)
    if observations < 2:
        return 0.0
    symbols = len(exposure[0])
    net: list[float] = []
    for row in range(observations):
        selected_row = (row - shift) % observations
        previous_row = (row - 1 - shift) % observations
        total = 0.0
        for column in range(symbols):
            current_exposure = float(exposure[selected_row][column])
            previous_exposure = float(exposure[previous_row][column])
            turnover = abs(current_exposure - previous_exposure)
            total += (
                current_exposure * float(returns[row][column])
                - turnover * float(costs[row][column])
            )
        net.append(total)
    return _period_sharpe(net)


def _mcpt(
    exposure: Sequence[Sequence[float]],
    returns: Sequence[Sequence[float]],
    costs: Sequence[Sequence[float]],
    *,
    seed: int,
    permutations: int,
) -> tuple[float, list[float], float]:
    observed = _mcpt_statistic(exposure, returns, costs)
    rng = random.Random(seed)
    null = [
        _mcpt_statistic(
            exposure,
            returns,
            costs,
            shift=rng.randrange(1, len(exposure)),
        )
        for _ in range(permutations)
    ]
    p_value = (1.0 + sum(value >= observed for value in null)) / (
        permutations + 1.0
    )
    return observed, null, float(p_value)


def _trade_core(
    trade: RawTrade,
    *,
    cost_row: Mapping[str, Any],
    engine_sha256: str,
    config_sha256: str,
) -> dict[str, Any]:
    if trade.side == "BUY":
        slippage = (
            (trade.entry_price - trade.next_open_ask)
            / trade.next_open_ask
            * 1e4
        )
        gross = (
            (trade.exit_price - trade.entry_price) / trade.entry_price * 1e4
        )
        stop_bps = (
            (trade.entry_price - trade.initial_sl_price)
            / trade.entry_price
            * 1e4
        )
        target_bps = (
            (trade.initial_tp_price - trade.entry_price)
            / trade.entry_price
            * 1e4
        )
    else:
        slippage = (
            (trade.next_open_bid - trade.entry_price)
            / trade.next_open_bid
            * 1e4
        )
        gross = (
            (trade.entry_price - trade.exit_price) / trade.entry_price * 1e4
        )
        stop_bps = (
            (trade.initial_sl_price - trade.entry_price)
            / trade.entry_price
            * 1e4
        )
        target_bps = (
            (trade.entry_price - trade.initial_tp_price)
            / trade.entry_price
            * 1e4
        )
    if min(stop_bps, target_bps) <= 0.0 or slippage < -1e-9:
        raise EvidenceRefusal("trade_geometry_invalid")
    slippage = max(0.0, slippage)
    source_mid = (trade.next_open_bid + trade.next_open_ask) / 2.0
    source_spread = (
        (trade.next_open_ask - trade.next_open_bid) / source_mid * 1e4
    )
    ig_pad = max(
        0.0,
        float(cost_row["ig_p90_spread_bps"]) - source_spread,
    )
    commission = float(cost_row["commission_bps_per_round_trip"])
    financing = float(cost_row["configured_financing_bps_per_trade"])
    base_cost = source_spread + ig_pad + slippage + commission + financing
    two_x_cost = TWO_X_COST_MULTIPLIER * base_cost
    net_base = gross - base_cost
    net_two_x = gross - two_x_cost
    initial_risk = stop_bps + base_cost
    p_star = initial_risk / (target_bps + stop_bps)
    if initial_risk <= 0.0 or not 0.0 <= p_star < 1.0:
        raise EvidenceRefusal(
            f"trade_cost_dead:{trade.symbol}:{trade.fill_epoch}:{p_star:.9f}"
        )
    material = {
        "symbol": trade.symbol,
        "side": trade.side,
        "decision_epoch": trade.decision_epoch,
        "fill_epoch": trade.fill_epoch,
        "exit_epoch": trade.exit_epoch,
        "config_sha256": config_sha256,
    }
    trade_id = "scalp-" + _canonical_sha256(material)[:32]
    return {
        "trade_id": trade_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "strategy_id": SCALP_DISLOCATION_STRATEGY_ID,
        "strategy_version": SCALP_DISLOCATION_STRATEGY_VERSION,
        "engine_sha256": engine_sha256,
        "config_sha256": config_sha256,
        "source_snapshot_sha256": trade.source_snapshot_sha256,
        "decision_bar_open_epoch": trade.decision_epoch,
        "fill_bar_open_epoch": trade.fill_epoch,
        "entry_epoch": trade.entry_epoch,
        "exit_epoch": trade.exit_epoch,
        "entry_utc_day": _utc_day(trade.entry_epoch),
        "decision_bid": trade.decision_bid,
        "decision_ask": trade.decision_ask,
        "next_open_bid": trade.next_open_bid,
        "next_open_ask": trade.next_open_ask,
        "entry_price": trade.entry_price,
        "initial_sl_price": trade.initial_sl_price,
        "initial_tp_price": trade.initial_tp_price,
        "exit_price": trade.exit_price,
        "exit_reason": trade.exit_reason,
        "stop_bps": stop_bps,
        "target_bps": target_bps,
        "p_star": p_star,
        "gross_mid_pnl_bps": gross,
        "source_spread_cost_bps": source_spread,
        "ig_cost_pad_bps": ig_pad,
        "slippage_bps": slippage,
        "commission_bps": commission,
        "financing_bps": financing,
        "base_total_cost_bps": base_cost,
        "two_x_total_cost_bps": two_x_cost,
        "net_pnl_bps_base": net_base,
        "net_pnl_bps_2x": net_two_x,
        "initial_risk_bps": initial_risk,
        "net_r_base": net_base / initial_risk,
        "net_r_2x": net_two_x / initial_risk,
        "full_target_hit_first": trade.exit_reason == "TAKE_PROFIT",
    }


def _apply_portfolio_equity(
    records: list[dict[str, Any]],
    *,
    initial_equity: float,
    risk_fraction: float,
) -> tuple[list[dict[str, Any]], int]:
    if initial_equity <= 0.0 or not 0.0 < risk_fraction < 1.0:
        raise EvidenceRefusal("portfolio_parameters_invalid")
    by_id = {str(record["trade_id"]): record for record in records}
    if len(by_id) != len(records):
        raise EvidenceRefusal("trade_ids_not_unique")
    events: list[tuple[float, int, str]] = []
    for record in records:
        events.append((float(record["entry_epoch"]), 1, str(record["trade_id"])))
        events.append((float(record["exit_epoch"]), 0, str(record["trade_id"])))
    equity = float(initial_equity)
    peak = equity
    concurrent = 0
    maximum_concurrent = 0
    for _, event_kind, trade_id in sorted(events):
        record = by_id[trade_id]
        if event_kind == 1:
            initial_risk_bps = float(record["initial_risk_bps"])
            if initial_risk_bps <= 0.0:
                raise EvidenceRefusal("portfolio_trade_risk_invalid")
            notional = equity * risk_fraction / (initial_risk_bps / 1e4)
            if notional <= 0.0 or not math.isfinite(notional):
                raise EvidenceRefusal("portfolio_notional_invalid")
            record["notional_account_ccy"] = notional
            concurrent += 1
            maximum_concurrent = max(maximum_concurrent, concurrent)
            continue
        concurrent -= 1
        if concurrent < 0 or "notional_account_ccy" not in record:
            raise EvidenceRefusal("portfolio_event_order_invalid")
        realized = (
            float(record["net_pnl_bps_base"])
            / 1e4
            * float(record["notional_account_ccy"])
        )
        record["equity_before"] = equity
        record["realized_pnl_account_ccy"] = realized
        equity += realized
        if equity <= 0.0 or not math.isfinite(equity):
            raise EvidenceRefusal("portfolio_equity_exhausted")
        peak = max(peak, equity)
        record["equity_after"] = equity
        record["peak_equity_after"] = peak
        record["drawdown_pct_after"] = (peak - equity) / peak * 100.0
    if concurrent != 0:
        raise EvidenceRefusal("portfolio_positions_unclosed")
    ordered = sorted(
        records, key=lambda row: (float(row["exit_epoch"]), str(row["trade_id"]))
    )
    # Equity was applied in this same exit ordering (exits precede entries at
    # ties), so the sequence is directly auditable by the issuer.
    return ordered, max(1, maximum_concurrent)


def _trial_daily_returns(
    trades: Iterable[RawTrade],
    *,
    cost_rows: Mapping[str, Mapping[str, Any]],
    engine_sha256: str,
    config_sha256: str,
    risk_fraction: float,
) -> dict[str, float]:
    by_day: dict[str, float] = defaultdict(float)
    for trade in trades:
        core = _trade_core(
            trade,
            cost_row=cost_rows[trade.symbol],
            engine_sha256=engine_sha256,
            config_sha256=config_sha256,
        )
        by_day[str(core["entry_utc_day"])] += (
            float(core["net_r_base"]) * risk_fraction
        )
    return dict(by_day)


def _flatten(matrix: Sequence[Sequence[float]]) -> Iterator[float]:
    for row in matrix:
        yield from row


def _build_cost_rows(
    *,
    replay_results: Mapping[str, SymbolReplayResult],
    source_snapshot: SourceSnapshot,
    ig_calibration: IgCalibration,
    fee_schedule: FeeSchedule,
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for symbol in IG_MT4_SCALP_SYMBOLS:
        replay = replay_results[symbol]
        ig = ig_calibration.symbols[symbol]
        rows[symbol] = {
            "source_observations": replay.source_samples,
            "source_file_sha256": source_snapshot.files[symbol].sha256,
            "source_independent_days": replay.source_days,
            "source_first_epoch": replay.source_first_epoch,
            "source_last_epoch": replay.source_last_epoch,
            "source_median_spread_bps": _median(replay.source_spreads_bps),
            "source_p90_spread_bps": _linear_quantile(
                replay.source_spreads_bps, 0.90
            ),
            "ig_observations": ig.observations,
            "ig_duration_secs": ig.duration_secs,
            "ig_median_spread_bps": ig.median_spread_bps,
            "ig_p90_spread_bps": ig.p90_spread_bps,
            "ig_max_spread_bps": ig.max_spread_bps,
            "point": ig.point,
            "price_tick_size": ig.price_tick_size,
            "digits": ig.digits,
            "trade_allowed": ig.trade_allowed,
            "commission_bps_per_round_trip": fee_schedule.rows[symbol][
                "commission_bps_per_round_trip"
            ],
            "configured_financing_bps_per_trade": fee_schedule.rows[symbol][
                "configured_financing_bps_per_trade"
            ],
        }
    return rows


def _build_cost_model(
    *,
    staging: Path,
    source_snapshot: SourceSnapshot,
    source_npz_path: Path,
    point_in_time_audit_path: Path,
    ig_calibration: IgCalibration,
    fee_schedule: FeeSchedule,
    cost_rows: Mapping[str, Mapping[str, Any]],
    created_at_epoch: float,
) -> dict[str, Any]:
    capture_npz_name = str(ig_calibration.capture.get("npz_path") or "").strip()
    if Path(capture_npz_name).name != capture_npz_name:
        raise EvidenceRefusal("ig_calibration_capture_npz_basename_invalid")
    target_ig_npz = staging / capture_npz_name
    if target_ig_npz.exists():
        raise EvidenceRefusal("ig_calibration_output_exists")
    shutil.copyfile(ig_calibration.npz_path, target_ig_npz)
    copied_ig_sha = _file_sha256(target_ig_npz)
    if copied_ig_sha != ig_calibration.npz_sha256:
        raise EvidenceRefusal("ig_calibration_copy_sha256_mismatch")
    fee_name = fee_schedule.path.name
    source_document_name = fee_schedule.source_document_path.name
    if fee_name == source_document_name or (staging / fee_name).exists():
        raise EvidenceRefusal("fee_schedule_output_name_invalid")
    shutil.copyfile(fee_schedule.path, staging / fee_name)
    shutil.copyfile(
        fee_schedule.source_document_path,
        staging / source_document_name,
    )
    if _file_sha256(staging / fee_name) != _file_sha256(fee_schedule.path):
        raise EvidenceRefusal("fee_schedule_copy_sha256_mismatch")
    if _file_sha256(staging / source_document_name) != _file_sha256(
        fee_schedule.source_document_path
    ):
        raise EvidenceRefusal("fee_source_document_copy_sha256_mismatch")
    source_capture = {
        "schema_version": "fxstack.external_dukascopy_bid_ask_m1_source.v1",
        "source": "dukascopy",
        "created_at_epoch": created_at_epoch,
        "source_snapshot_sha256": source_snapshot.sha256,
        "source_snapshot_manifest": source_snapshot.manifest,
        "source_file_paths": {
            symbol: source_snapshot.files[symbol].relative_path
            for symbol in IG_MT4_SCALP_SYMBOLS
        },
        "point_in_time_audit_path": point_in_time_audit_path.name,
        "point_in_time_audit_sha256": _file_sha256(point_in_time_audit_path),
        "source_errors": [],
    }
    return {
        "schema_version": COST_MODEL_SCHEMA,
        "source_errors": [],
        "venue_id": IG_MT4_VENUE_ID,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "source_quote_definition": SOURCE_QUOTE_DEFINITION,
        "source_sampling_contract": SOURCE_SAMPLING_CONTRACT,
        "ig_calibration_definition": IG_CALIBRATION_DEFINITION,
        "cost_definition": COST_DEFINITION,
        "base_cost_multiplier": BASE_COST_MULTIPLIER,
        "two_x_cost_multiplier": TWO_X_COST_MULTIPLIER,
        "execution_max_slippage_points": EXECUTION_MAX_SLIPPAGE_POINTS,
        "components": {
            "source_bid_ask": True,
            "ig_cost_pad": True,
            "slippage": True,
            "commission": True,
            "financing": True,
        },
        "source_quote_npz_path": source_npz_path.name,
        "source_quote_npz_sha256": _file_sha256(source_npz_path),
        "source_snapshot_sha256": source_snapshot.sha256,
        "ig_calibration_npz_path": target_ig_npz.name,
        "ig_calibration_npz_sha256": copied_ig_sha,
        "fee_schedule_path": fee_name,
        "fee_schedule_sha256": _file_sha256(staging / fee_name),
        "source_capture": source_capture,
        "ig_capture": dict(ig_calibration.capture),
        "symbols": {symbol: dict(cost_rows[symbol]) for symbol in IG_MT4_SCALP_SYMBOLS},
    }


def _selected_records(
    *,
    replay_results: Mapping[str, SymbolReplayResult],
    cost_rows: Mapping[str, Mapping[str, Any]],
    engine_sha256: str,
    initial_equity: float,
    risk_fraction: float,
) -> tuple[list[dict[str, Any]], int, int]:
    records: list[dict[str, Any]] = []
    cost_dead = 0
    config_sha = DislocationPolicy().config_sha256()
    for symbol in IG_MT4_SCALP_SYMBOLS:
        for trade in replay_results[symbol].trial_trades[SELECTED_ATTEMPT_ID]:
            try:
                records.append(
                    _trade_core(
                        trade,
                        cost_row=cost_rows[symbol],
                        engine_sha256=engine_sha256,
                        config_sha256=config_sha,
                    )
                )
            except EvidenceRefusal as exc:
                if str(exc).startswith("trade_cost_dead:"):
                    cost_dead += 1
                    continue
                raise
    if not records:
        raise EvidenceRefusal("selected_policy_has_no_cost_live_trades")
    ordered, maximum_concurrent = _apply_portfolio_equity(
        records,
        initial_equity=initial_equity,
        risk_fraction=risk_fraction,
    )
    return ordered, maximum_concurrent, cost_dead


def _build_cell_evidence(
    *, records: Sequence[Mapping[str, Any]], ledger_sha256: str, cost_sha256: str
) -> dict[str, Any]:
    cells = {
        symbol: {
            side: {
                "trade_ids": [
                    str(record["trade_id"])
                    for record in records
                    if record["symbol"] == symbol and record["side"] == side
                ]
            }
            for side in ("BUY", "SELL")
        }
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    return {
        "schema_version": CELL_EVIDENCE_SCHEMA,
        "source_errors": [],
        "trade_ledger_sha256": ledger_sha256,
        "cost_model_sha256": cost_sha256,
        "win_definition": WIN_DEFINITION,
        "max_entries_per_symbol_utc_day": 1,
        "cells": cells,
    }


def _day_epoch(day: str) -> int:
    return int(
        datetime.strptime(day, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def _panel_inputs(
    *,
    replay_results: Mapping[str, SymbolReplayResult],
    cost_rows: Mapping[str, Mapping[str, Any]],
) -> tuple[list[int], list[list[float]], list[list[float]], list[list[float]]]:
    common_days: set[str] | None = None
    for symbol in IG_MT4_SCALP_SYMBOLS:
        days = set(replay_results[symbol].daily_panel)
        common_days = days if common_days is None else common_days & days
    ordered_days = sorted(common_days or ())
    if len(ordered_days) < 20:
        raise EvidenceRefusal("mcpt_common_panel_insufficient")
    timestamps = [_day_epoch(day) for day in ordered_days]
    exposure: list[list[float]] = []
    bar_returns: list[list[float]] = []
    costs: list[list[float]] = []
    for day in ordered_days:
        exposure_row: list[float] = []
        returns_row: list[float] = []
        cost_row_values: list[float] = []
        for symbol in IG_MT4_SCALP_SYMBOLS:
            panel = replay_results[symbol].daily_panel[day]
            exposure_row.append(
                panel.signed_exposure_sum / panel.bars if panel.bars else 0.0
            )
            returns_row.append(
                panel.last_mid_close / panel.first_mid_open - 1.0
            )
            row = cost_rows[symbol]
            full_cost_bps = (
                max(
                    float(row["source_p90_spread_bps"]),
                    float(row["ig_p90_spread_bps"]),
                )
                + float(row["commission_bps_per_round_trip"])
                + float(row["configured_financing_bps_per_trade"])
            )
            cost_row_values.append(full_cost_bps / 1e4)
        exposure.append(exposure_row)
        bar_returns.append(returns_row)
        costs.append(cost_row_values)
    return timestamps, exposure, bar_returns, costs


def _attempt_returns(
    *,
    replay_results: Mapping[str, SymbolReplayResult],
    cost_rows: Mapping[str, Mapping[str, Any]],
    engine_sha256: str,
    days: Sequence[str],
    risk_fraction: float,
) -> tuple[list[list[float]], list[float], int]:
    per_attempt: dict[str, dict[str, float]] = {}
    cost_dead = 0
    for trial in FIXED_TRIALS:
        all_trades = (
            trade
            for symbol in IG_MT4_SCALP_SYMBOLS
            for trade in replay_results[symbol].trial_trades[trial.attempt_id]
        )
        by_day: dict[str, float] = defaultdict(float)
        for trade in all_trades:
            try:
                core = _trade_core(
                    trade,
                    cost_row=cost_rows[trade.symbol],
                    engine_sha256=engine_sha256,
                    config_sha256=trial.policy.config_sha256(),
                )
            except EvidenceRefusal as exc:
                if str(exc).startswith("trade_cost_dead:"):
                    cost_dead += 1
                    continue
                raise
            by_day[str(core["entry_utc_day"])] += (
                float(core["net_r_base"]) * risk_fraction
            )
        per_attempt[trial.attempt_id] = dict(by_day)
    matrix = [
        [per_attempt[trial.attempt_id].get(day, 0.0) for trial in FIXED_TRIALS]
        for day in days
    ]
    sharpes = _column_sharpes(matrix)
    return matrix, sharpes, cost_dead


def _build_statistical_report(
    *,
    staging: Path,
    replay_results: Mapping[str, SymbolReplayResult],
    cost_rows: Mapping[str, Mapping[str, Any]],
    engine_sha256: str,
    ledger_sha256: str,
    cost_sha256: str,
    cell_sha256: str,
    risk_fraction: float,
) -> tuple[dict[str, Any], int]:
    timestamps, exposure, bar_returns, costs = _panel_inputs(
        replay_results=replay_results, cost_rows=cost_rows
    )
    scratch = staging / ".stats_raw"
    scratch.mkdir()
    mcpt_arrays = {
        "timestamps": _write_raw_array(
            scratch / "mcpt_timestamps.raw",
            descr="<i8",
            shape=(len(timestamps),),
            values=timestamps,
        ),
        "lagged_signed_exposure": _write_raw_array(
            scratch / "mcpt_exposure.raw",
            descr="<f8",
            shape=(len(timestamps), len(IG_MT4_SCALP_SYMBOLS)),
            values=_flatten(exposure),
        ),
        "bar_returns": _write_raw_array(
            scratch / "mcpt_returns.raw",
            descr="<f8",
            shape=(len(timestamps), len(IG_MT4_SCALP_SYMBOLS)),
            values=_flatten(bar_returns),
        ),
        "cost_per_turn": _write_raw_array(
            scratch / "mcpt_costs.raw",
            descr="<f8",
            shape=(len(timestamps), len(IG_MT4_SCALP_SYMBOLS)),
            values=_flatten(costs),
        ),
    }
    mcpt_path = staging / MCPT_NPZ_NAME
    _write_npz_from_raw(mcpt_path, mcpt_arrays)
    observed, null, p_value = _mcpt(
        exposure,
        bar_returns,
        costs,
        seed=MCPT_SEED,
        permutations=MCPT_PERMUTATIONS,
    )

    days = [_utc_day(timestamp) for timestamp in timestamps]
    returns_matrix, trial_sharpes, cost_dead = _attempt_returns(
        replay_results=replay_results,
        cost_rows=cost_rows,
        engine_sha256=engine_sha256,
        days=days,
        risk_fraction=risk_fraction,
    )
    pbo_path = staging / PBO_NPZ_NAME
    pbo_arrays = {
        "aligned_returns": _write_raw_array(
            scratch / "pbo_returns.raw",
            descr="<f8",
            shape=(len(returns_matrix), len(FIXED_TRIALS)),
            values=_flatten(returns_matrix),
        ),
        "trial_sharpes": _write_raw_array(
            scratch / "pbo_sharpes.raw",
            descr="<f8",
            shape=(len(FIXED_TRIALS),),
            values=trial_sharpes,
        ),
    }
    _write_npz_from_raw(pbo_path, pbo_arrays)
    pbo = _pbo(
        returns_matrix,
        n_splits=PBO_SPLITS,
        max_combinations=PBO_MAX_COMBINATIONS,
    )
    selected_index = [trial.attempt_id for trial in FIXED_TRIALS].index(
        SELECTED_ATTEMPT_ID
    )
    selected_returns = [row[selected_index] for row in returns_matrix]
    selected_sharpe = trial_sharpes[selected_index]
    dsr, sharpe_variance = _deflated_sharpe(
        selected_returns=selected_returns,
        selected_sharpe=selected_sharpe,
        trial_sharpes=trial_sharpes,
    )
    shutil.rmtree(scratch)
    report = {
        "schema_version": STATISTICAL_REPORT_SCHEMA,
        "source_errors": [],
        "trade_ledger_sha256": ledger_sha256,
        "cost_model_sha256": cost_sha256,
        "cell_evidence_sha256": cell_sha256,
        "mcpt": {
            "method": MCPT_METHOD,
            "seed": MCPT_SEED,
            "n_permutations": MCPT_PERMUTATIONS,
            "input_npz_path": mcpt_path.name,
            "input_npz_sha256": _file_sha256(mcpt_path),
            "observed_statistic": observed,
            "null_statistics": null,
            "p_value": p_value,
        },
        "pbo_dsr": {
            "method": PBO_DSR_METHOD,
            "input_npz_path": pbo_path.name,
            "input_npz_sha256": _file_sha256(pbo_path),
            "attempt_manifest_path": ATTEMPT_MANIFEST_NAME,
            "attempt_manifest_sha256": _file_sha256(
                staging / ATTEMPT_MANIFEST_NAME
            ),
            "attempt_ids": [trial.attempt_id for trial in FIXED_TRIALS],
            "selected_attempt_id": SELECTED_ATTEMPT_ID,
            "n_splits": PBO_SPLITS,
            "max_combinations": PBO_MAX_COMBINATIONS,
            "pbo": pbo,
            "dsr": dsr,
            "selected_sharpe": selected_sharpe,
            "sharpe_variance_across_trials": sharpe_variance,
        },
        "statistics": {
            "mcpt_p_value": p_value,
            "pbo": pbo,
            "dsr": dsr,
            "mcpt_observations": len(timestamps),
            "return_observations": len(returns_matrix),
            "attempts": len(FIXED_TRIALS),
            "selected_attempt_id": SELECTED_ATTEMPT_ID,
        },
    }
    return report, cost_dead


def _attempt_manifest_payload(
    *,
    source_snapshot_sha256: str,
    created_at_epoch: float,
    replay_started_at_epoch: float,
) -> dict[str, Any]:
    return {
        "schema_version": ATTEMPT_MANIFEST_SCHEMA,
        "sealed_before_replay": True,
        "created_at_epoch": created_at_epoch,
        "replay_started_at_epoch": replay_started_at_epoch,
        "source_snapshot_sha256": source_snapshot_sha256,
        "selected_attempt_id": SELECTED_ATTEMPT_ID,
        "attempts": [
            {
                "attempt_id": trial.attempt_id,
                "policy": trial.policy.to_canonical_dict(),
                "config_sha256": trial.policy.config_sha256(),
            }
            for trial in FIXED_TRIALS
        ],
    }


def _point_in_time_audit_payload(
    *, source_snapshot: SourceSnapshot, created_at_epoch: float
) -> dict[str, Any]:
    families_path = ENGINE_PACKAGE_ROOT / "scalp" / "families.py"
    return {
        "schema_version": "fxstack.external_scalp_point_in_time_audit.v1",
        "created_at_epoch": created_at_epoch,
        "source_snapshot_sha256": source_snapshot.sha256,
        "causal_checks": {
            "signals_use_closed_bars_only": True,
            "entry_uses_next_m1_open": True,
            "ambiguous_intrabar_exit_is_stop_first": True,
            "one_entry_per_symbol_utc_day": True,
            "holdout_tuning_forbidden": True,
        },
        "prior_control_status": {
            "status": "falsified_research_control",
            "source_path": "fx-quant-stack/src/fxstack/scalp/families.py",
            "source_sha256": _file_sha256(families_path),
            "summary": (
                "The repository records blind-2026 price dislocation as "
                "negative in both fade and momentum directions."
            ),
        },
        "prior_search_disclosure": {
            "purpose": "diagnostic_rejection_only",
            "symbols": ["EURUSD", "BTCUSD", "LTCUSD", "NZDJPY"],
            "signal_modes": ["revert", "momentum"],
            "z_entries": [1.5, 2.0, 2.5, 3.0],
            "tp_atr_multipliers": [1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0],
            "sl_atr_multipliers": [0.75, 1.0, 1.5, 2.0],
            "time_stop_bars": 20,
            "policies": 224,
            "survivors_selected": 0,
            "accounted_in_attempt_manifest": True,
        },
        "errors": [],
    }


def _load_release_tool() -> Any:
    tool_path = VALIDATION_TOOL_ROOT / "tools" / "external_scalp_validation_release.py"
    spec = importlib.util.spec_from_file_location(
        "fxstack_external_scalp_validation_release_for_evidence", tool_path
    )
    if spec is None or spec.loader is None:
        raise EvidenceRefusal("external_release_tool_import_invalid")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, RuntimeError) as exc:
        raise EvidenceRefusal(
            "external_release_tool_unavailable; validation host requires numpy/scipy"
        ) from exc
    return module


def _safe_staging_directory(output: Path) -> Path:
    target = output.resolve(strict=False)
    parent = target.parent.resolve(strict=True)
    if not parent.is_dir() or parent.is_symlink() or target.exists():
        raise EvidenceRefusal("output_directory_invalid_or_exists")
    created = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=parent))
    if created.parent != parent:
        raise EvidenceRefusal("staging_directory_invalid")
    return created


def _write_source_npz(staging: Path, writer: SourceRawWriters) -> Path:
    path = staging / SOURCE_NPZ_NAME
    _write_npz_from_raw(path, writer.arrays())
    shutil.rmtree(writer.root)
    return path


def _gross_quote_bps(trade: RawTrade) -> float:
    direction = 1.0 if trade.side == "BUY" else -1.0
    return (
        (trade.exit_price - trade.entry_price)
        * direction
        / trade.entry_price
        * 1e4
    )


def _current_refusal_payload(
    *,
    replay_results: Mapping[str, SymbolReplayResult],
    source_snapshot: SourceSnapshot,
    source_npz_path: Path,
    engine_sha256: str,
    started_at_epoch: float,
    completed_at_epoch: float,
) -> dict[str, Any]:
    cells: dict[str, dict[str, dict[str, Any]]] = {}
    reasons: list[str] = []
    all_trades: list[RawTrade] = []
    for symbol in IG_MT4_SCALP_SYMBOLS:
        trades = list(replay_results[symbol].trial_trades[SELECTED_ATTEMPT_ID])
        all_trades.extend(trades)
        cells[symbol] = {}
        for side in ("BUY", "SELL"):
            selected = [trade for trade in trades if trade.side == side]
            gross = [_gross_quote_bps(trade) for trade in selected]
            days = len({_utc_day(trade.entry_epoch) for trade in selected})
            wins = sum(trade.exit_reason == "TAKE_PROFIT" for trade in selected)
            cell = {
                "trades": len(selected),
                "independent_days": days,
                "full_target_first_wins": wins,
                "full_target_first_rate": wins / len(selected) if selected else 0.0,
                "mean_quote_side_gross_bps_before_explicit_costs": (
                    math.fsum(gross) / len(gross) if gross else 0.0
                ),
            }
            cells[symbol][side] = cell
            if len(selected) < MIN_CELL_TRADES:
                reasons.append(
                    f"cell_trade_sample_insufficient:{symbol}:{side}:"
                    f"{len(selected)}<{MIN_CELL_TRADES}"
                )
            if days < MIN_CELL_DAYS:
                reasons.append(
                    f"cell_day_sample_insufficient:{symbol}:{side}:{days}<{MIN_CELL_DAYS}"
                )
    gross_all = [_gross_quote_bps(trade) for trade in all_trades]
    mean_gross = math.fsum(gross_all) / len(gross_all) if gross_all else 0.0
    independent_days = len({_utc_day(trade.entry_epoch) for trade in all_trades})
    if len(all_trades) < MIN_TOTAL_TRADES:
        reasons.append(
            f"overall_trade_sample_insufficient:{len(all_trades)}<{MIN_TOTAL_TRADES}"
        )
    if independent_days < MIN_TOTAL_DAYS:
        reasons.append(
            f"overall_day_sample_insufficient:{independent_days}<{MIN_TOTAL_DAYS}"
        )
    if mean_gross <= 0.0:
        reasons.append("nonpositive_before_explicit_ig_costs")
    return {
        "schema_version": "fxstack.external_scalp_current_policy_refusal.v1",
        "eligible_for_release": False,
        "status": "definitive_rejection" if reasons else "full_validation_required",
        "source_errors": [],
        "refusal_reasons": reasons,
        "venue_id": IG_MT4_VENUE_ID,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "strategy_id": SCALP_DISLOCATION_STRATEGY_ID,
        "strategy_version": SCALP_DISLOCATION_STRATEGY_VERSION,
        "engine_sha256": engine_sha256,
        "config_sha256": DislocationPolicy().config_sha256(),
        "source_snapshot_sha256": source_snapshot.sha256,
        "source_snapshot_manifest": source_snapshot.manifest,
        "source_quote_npz_path": source_npz_path.name,
        "source_quote_npz_sha256": _file_sha256(source_npz_path),
        "source_sampling_contract": SOURCE_SAMPLING_CONTRACT,
        "replay_started_at_epoch": started_at_epoch,
        "replay_completed_at_epoch": completed_at_epoch,
        "max_entries_per_symbol_utc_day": 1,
        "execution_model": {
            "entry": "adverse_of_decision_quote_and_next_m1_open.v1",
            "intrabar_ambiguity": "stop_first.v1",
            "target_on_entry_bar": False,
            "gap_stop_fill": "adverse_open.v1",
            "time_stop_bars": DislocationPolicy().time_stop_bars,
        },
        "attempt_accounting": {
            "complete_disclosed_attempts": len(FIXED_TRIALS),
            "attempt_manifest_path": ATTEMPT_MANIFEST_NAME,
            "attempt_manifest_sha256": _file_sha256(
                source_npz_path.parent / ATTEMPT_MANIFEST_NAME
            ),
            "nonselected_attempt_replay": "deferred_after_selected_policy_rejection",
            "selection_claimed": False,
        },
        "overall": {
            "trades": len(all_trades),
            "independent_days": independent_days,
            "full_target_first_wins": sum(
                trade.exit_reason == "TAKE_PROFIT" for trade in all_trades
            ),
            "mean_quote_side_gross_bps_before_explicit_costs": mean_gross,
        },
        "cells": cells,
    }


def audit_current_policy(
    *,
    csv_root: Path,
    start_epoch: int,
    end_epoch: int,
    output_dir: Path,
) -> dict[str, Any]:
    """Replay production-current once over all 22 and publish refusal evidence."""

    source_snapshot = _source_snapshot(
        csv_root=csv_root,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
    )
    engine_before = production_scalp_engine_identity(
        package_root=ENGINE_PACKAGE_ROOT, repository_root=ENGINE_REPOSITORY_ROOT
    )
    staging = _safe_staging_directory(output_dir)
    writer: SourceRawWriters | None = None
    try:
        audit_created = time.time()
        _write_json_new(
            staging / "source_point_in_time_audit.json",
            _point_in_time_audit_payload(
                source_snapshot=source_snapshot, created_at_epoch=audit_created
            ),
        )
        manifest_created = time.time()
        scheduled_start = manifest_created + 0.001
        _write_json_new(
            staging / ATTEMPT_MANIFEST_NAME,
            _attempt_manifest_payload(
                source_snapshot_sha256=source_snapshot.sha256,
                created_at_epoch=manifest_created,
                replay_started_at_epoch=scheduled_start,
            ),
        )
        while time.time() < scheduled_start:
            pass
        raw_root = staging / ".source_raw"
        raw_root.mkdir()
        writer = SourceRawWriters(raw_root)
        results: dict[str, SymbolReplayResult] = {}
        for symbol_index, symbol in enumerate(IG_MT4_SCALP_SYMBOLS):
            results[symbol] = _run_symbol(
                symbol=symbol,
                symbol_index=symbol_index,
                source=source_snapshot.files[symbol],
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                source_writer=writer,
                trials=(FIXED_TRIALS[0],),
            )
        writer.close()
        source_npz = _write_source_npz(staging, writer)
        writer = None
        completed = time.time()
        raw_trades = sorted(
            (
                trade
                for symbol in IG_MT4_SCALP_SYMBOLS
                for trade in results[symbol].trial_trades[SELECTED_ATTEMPT_ID]
            ),
            key=lambda trade: (trade.exit_epoch, trade.symbol, trade.side),
        )
        raw_ledger = {
            "schema_version": "fxstack.external_scalp_current_policy_raw_ledger.v1",
            "source_errors": [],
            "strategy_id": SCALP_DISLOCATION_STRATEGY_ID,
            "strategy_version": SCALP_DISLOCATION_STRATEGY_VERSION,
            "engine_sha256": engine_before.engine_sha256,
            "config_sha256": DislocationPolicy().config_sha256(),
            "source_snapshot_sha256": source_snapshot.sha256,
            "records": [asdict(trade) for trade in raw_trades],
        }
        _write_json_new(staging / "current_policy_raw_ledger.json", raw_ledger)
        refusal = _current_refusal_payload(
            replay_results=results,
            source_snapshot=source_snapshot,
            source_npz_path=source_npz,
            engine_sha256=engine_before.engine_sha256,
            started_at_epoch=scheduled_start,
            completed_at_epoch=completed,
        )
        refusal["raw_ledger_path"] = "current_policy_raw_ledger.json"
        refusal["raw_ledger_sha256"] = _file_sha256(
            staging / "current_policy_raw_ledger.json"
        )
        _write_json_new(staging / "current_policy_refusal.json", refusal)
        engine_after = production_scalp_engine_identity(
            package_root=ENGINE_PACKAGE_ROOT,
            repository_root=ENGINE_REPOSITORY_ROOT,
        )
        if engine_after.engine_sha256 != engine_before.engine_sha256:
            raise EvidenceRefusal("production_engine_changed_during_replay")
        target = output_dir.resolve(strict=False)
        os.replace(staging, target)
        return refusal
    except Exception:
        if writer is not None:
            writer.close()
        shutil.rmtree(staging, ignore_errors=True)
        raise


def build_evidence(
    *,
    csv_root: Path,
    ig_capture_path: Path,
    fee_schedule_path: Path,
    start_epoch: int,
    end_epoch: int,
    output_dir: Path,
    initial_equity: float = DEFAULT_INITIAL_EQUITY,
    risk_fraction: float = DEFAULT_RISK_FRACTION,
    account_currency: str = DEFAULT_ACCOUNT_CURRENCY,
) -> dict[str, Any]:
    """Replay all disclosed attempts and publish only issuer-verified evidence."""

    source_snapshot = _source_snapshot(
        csv_root=csv_root,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
    )
    ig_calibration = _load_ig_capture(ig_capture_path.resolve(strict=True))
    fee_schedule = _load_fee_schedule(fee_schedule_path.resolve(strict=True))
    if float(fee_schedule.payload["effective_at_epoch"]) > float(
        ig_calibration.capture["capture_end_epoch"]
    ):
        raise EvidenceRefusal("fee_schedule_not_effective_at_calibration")
    engine_before = production_scalp_engine_identity(
        package_root=ENGINE_PACKAGE_ROOT, repository_root=ENGINE_REPOSITORY_ROOT
    )
    staging = _safe_staging_directory(output_dir)
    writer: SourceRawWriters | None = None
    artifacts_written = False
    try:
        audit_created = time.time()
        audit_path = staging / "source_point_in_time_audit.json"
        _write_json_new(
            audit_path,
            _point_in_time_audit_payload(
                source_snapshot=source_snapshot, created_at_epoch=audit_created
            ),
        )
        manifest_created = time.time()
        scheduled_start = manifest_created + 0.001
        _write_json_new(
            staging / ATTEMPT_MANIFEST_NAME,
            _attempt_manifest_payload(
                source_snapshot_sha256=source_snapshot.sha256,
                created_at_epoch=manifest_created,
                replay_started_at_epoch=scheduled_start,
            ),
        )
        while time.time() < scheduled_start:
            pass
        raw_root = staging / ".source_raw"
        raw_root.mkdir()
        writer = SourceRawWriters(raw_root)
        replay_results: dict[str, SymbolReplayResult] = {}
        for symbol_index, symbol in enumerate(IG_MT4_SCALP_SYMBOLS):
            replay_results[symbol] = _run_symbol(
                symbol=symbol,
                symbol_index=symbol_index,
                source=source_snapshot.files[symbol],
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                source_writer=writer,
            )
        writer.close()
        source_npz = _write_source_npz(staging, writer)
        writer = None
        cost_rows = _build_cost_rows(
            replay_results=replay_results,
            source_snapshot=source_snapshot,
            ig_calibration=ig_calibration,
            fee_schedule=fee_schedule,
        )
        cost_model = _build_cost_model(
            staging=staging,
            source_snapshot=source_snapshot,
            source_npz_path=source_npz,
            point_in_time_audit_path=audit_path,
            ig_calibration=ig_calibration,
            fee_schedule=fee_schedule,
            cost_rows=cost_rows,
            created_at_epoch=time.time(),
        )
        cost_path = staging / COST_MODEL_NAME
        _write_json_new(cost_path, cost_model)
        cost_sha = _file_sha256(cost_path)
        records, maximum_concurrent, selected_cost_dead = _selected_records(
            replay_results=replay_results,
            cost_rows=cost_rows,
            engine_sha256=engine_before.engine_sha256,
            initial_equity=initial_equity,
            risk_fraction=risk_fraction,
        )
        ledger = {
            "schema_version": TRADE_LEDGER_SCHEMA,
            "source_errors": [],
            "strategy_id": SCALP_DISLOCATION_STRATEGY_ID,
            "strategy_version": SCALP_DISLOCATION_STRATEGY_VERSION,
            "engine_sha256": engine_before.engine_sha256,
            "config_sha256": DislocationPolicy().config_sha256(),
            "venue_id": IG_MT4_VENUE_ID,
            "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
            "cost_model_sha256": cost_sha,
            "portfolio_contract": {
                "initial_equity": initial_equity,
                "account_currency": str(account_currency).strip().upper(),
                "risk_sizing": RISK_SIZING_METHOD,
                "max_concurrent_positions": maximum_concurrent,
            },
            "records": records,
        }
        ledger_path = staging / TRADE_LEDGER_NAME
        _write_json_new(ledger_path, ledger)
        ledger_sha = _file_sha256(ledger_path)
        cells = _build_cell_evidence(
            records=records, ledger_sha256=ledger_sha, cost_sha256=cost_sha
        )
        cell_path = staging / CELL_EVIDENCE_NAME
        _write_json_new(cell_path, cells)
        cell_sha = _file_sha256(cell_path)
        statistical_report, trial_cost_dead = _build_statistical_report(
            staging=staging,
            replay_results=replay_results,
            cost_rows=cost_rows,
            engine_sha256=engine_before.engine_sha256,
            ledger_sha256=ledger_sha,
            cost_sha256=cost_sha,
            cell_sha256=cell_sha,
            risk_fraction=risk_fraction,
        )
        statistical_path = staging / STATISTICAL_REPORT_NAME
        _write_json_new(statistical_path, statistical_report)
        artifacts_written = True
        artifact_paths = {
            "trade_ledger": ledger_path,
            "cost_model": cost_path,
            "statistical_report": statistical_path,
            "cell_evidence": cell_path,
        }
        release = _load_release_tool()
        try:
            evidence, manifest = release.derive_evidence_from_artifacts(
                artifact_paths=artifact_paths,
                source_root=csv_root,
                expected_engine_sha256=engine_before.engine_sha256,
                expected_config_sha256=DislocationPolicy().config_sha256(),
            )
        except Exception as exc:
            refusal = {
                "schema_version": "fxstack.external_scalp_full_validation_refusal.v1",
                "eligible_for_release": False,
                "status": "independent_validation_refused",
                "reason": str(exc),
                "engine_sha256": engine_before.engine_sha256,
                "config_sha256": DislocationPolicy().config_sha256(),
                "source_snapshot_sha256": source_snapshot.sha256,
                "attempts_accounted": len(FIXED_TRIALS),
                "selected_cost_dead_signals_omitted": selected_cost_dead,
                "all_trial_cost_dead_signals_omitted": trial_cost_dead,
                "artifact_sha256": {
                    role: _file_sha256(path) for role, path in artifact_paths.items()
                },
            }
            _write_json_new(staging / "NON_ISSUABLE_REFUSAL.json", refusal)
            result: dict[str, Any] = refusal
        else:
            _write_json_new(staging / EVIDENCE_NAME, evidence)
            result = {
                "status": "verified",
                "eligible_for_release": True,
                "evidence": evidence,
                "artifact_manifest": manifest,
            }
        engine_after = production_scalp_engine_identity(
            package_root=ENGINE_PACKAGE_ROOT,
            repository_root=ENGINE_REPOSITORY_ROOT,
        )
        if engine_after.engine_sha256 != engine_before.engine_sha256:
            raise EvidenceRefusal("production_engine_changed_during_replay")
        target = output_dir.resolve(strict=False)
        os.replace(staging, target)
        return result
    except Exception:
        if writer is not None:
            writer.close()
        # A completed but nonqualifying battery is published only through the
        # explicit NON_ISSUABLE_REFUSAL branch above.  Input, replay, or coding
        # failures never leave a partial directory behind.
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay and independently verify production-scalp evidence on an "
            "isolated validation host."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_history(target: argparse.ArgumentParser) -> None:
        target.add_argument("--csv-root", required=True)
        target.add_argument("--start", required=True, help="inclusive UTC date/time")
        target.add_argument("--end", required=True, help="exclusive UTC date/time")
        target.add_argument("--output-dir", required=True)

    audit = subparsers.add_parser(
        "audit-current",
        help=(
            "replay production-current once across the exact 22 and publish "
            "non-issuable refusal evidence"
        ),
    )
    add_history(audit)

    build = subparsers.add_parser(
        "build",
        help="run all disclosed attempts and the full independent evidence battery",
    )
    add_history(build)
    build.add_argument("--ig-capture", required=True)
    build.add_argument("--fee-schedule", required=True)
    build.add_argument("--initial-equity", type=float, default=DEFAULT_INITIAL_EQUITY)
    build.add_argument("--risk-fraction", type=float, default=DEFAULT_RISK_FRACTION)
    build.add_argument("--account-currency", default=DEFAULT_ACCOUNT_CURRENCY)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        start = _parse_cli_epoch(args.start)
        end = _parse_cli_epoch(args.end, end=True)
        if start is None or end is None or end <= start:
            raise EvidenceRefusal("replay_window_invalid")
        if args.command == "audit-current":
            result = audit_current_policy(
                csv_root=Path(args.csv_root),
                start_epoch=start,
                end_epoch=end,
                output_dir=Path(args.output_dir),
            )
        else:
            result = build_evidence(
                csv_root=Path(args.csv_root),
                ig_capture_path=Path(args.ig_capture),
                fee_schedule_path=Path(args.fee_schedule),
                start_epoch=start,
                end_epoch=end,
                output_dir=Path(args.output_dir),
                initial_equity=float(args.initial_equity),
                risk_fraction=float(args.risk_fraction),
                account_currency=str(args.account_currency),
            )
        summary = {
            "status": result.get("status"),
            "eligible_for_release": bool(result.get("eligible_for_release")),
            "output_dir": str(Path(args.output_dir).resolve(strict=False)),
            "attempts_accounted": len(FIXED_TRIALS),
        }
        if "refusal_reasons" in result:
            summary["refusal_reasons"] = result["refusal_reasons"]
        if "reason" in result:
            summary["reason"] = result["reason"]
        print(json.dumps(summary, sort_keys=True))
        return 0 if summary["eligible_for_release"] else 3
    except (EvidenceRefusal, OSError, RuntimeError, ValueError) as exc:
        print(f"production scalp evidence refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
