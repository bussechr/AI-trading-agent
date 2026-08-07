from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
import time
from types import MappingProxyType
from typing import Any

from fxstack._lazy import lazy_numpy as np, lazy_pandas as pd

from fxstack._serialization import clone_flat_dataclass, flat_dataclass_dict
from fxstack.providers.catalog import infer_instrument_ref


@dataclass(slots=True)
class CorrelationSnapshot:
    symbol: str = ""
    method: str = "heuristic"
    window_bars: int = 0
    min_obs: int = 0
    sample_count: int = 0
    freshness_secs: float | None = None
    max_abs_corr: float = 0.0
    avg_abs_corr: float = 0.0
    correlated_symbols: dict[str, float] = field(default_factory=dict)
    estimator: str = "heuristic"
    active_pair_count: int = 0
    realized_pair_count: int = 0
    coverage_ratio: float = 0.0
    pair_sample_counts: dict[str, int] = field(default_factory=dict)
    pair_observation_coverage: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return flat_dataclass_dict(self)


class _PreparedReturnSeriesMap(Mapping[str, "pd.Series"]):
    """Read-only return series that already satisfy correlation invariants."""

    __slots__ = ("_correlation_snapshots", "_pair_statistics", "_series")

    def __init__(self, series: Mapping[str, pd.Series]) -> None:
        self._series = MappingProxyType(dict(series))
        self._pair_statistics: dict[
            tuple[str, str],
            tuple[
                tuple[int, int],
                tuple[int, float | None, pd.Timestamp | None],
            ],
        ] = {}
        self._correlation_snapshots: dict[
            tuple[str, tuple[str, ...], str, int, int],
            tuple[CorrelationSnapshot, float | None],
        ] = {}

    def __getitem__(self, key: str) -> pd.Series:
        return self._series[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._series)

    def __len__(self) -> int:
        return len(self._series)

    def pair_statistics(
        self,
        left_key: str,
        right_key: str,
        *,
        window_bars: int,
        required_obs: int,
    ) -> tuple[int, float | None, pd.Timestamp | None]:
        left, right = sorted((str(left_key), str(right_key)))
        cache_key = (left, right)
        configuration = (
            max(0, int(window_bars)),
            max(2, int(required_obs)),
        )
        cached = self._pair_statistics.get(cache_key)
        if cached is not None and cached[0] == configuration:
            return cached[1]
        statistics = _compute_pair_statistics(
            self._series[left],
            self._series[right],
            window_bars=configuration[0],
            required_obs=configuration[1],
        )
        self._pair_statistics[cache_key] = (configuration, statistics)
        return statistics

    def correlation_snapshot(
        self,
        key: tuple[str, tuple[str, ...], str, int, int],
    ) -> CorrelationSnapshot | None:
        cached = self._correlation_snapshots.get(key)
        if cached is None:
            return None
        template, freshness_epoch = cached
        snapshot = clone_flat_dataclass(template)
        snapshot.freshness_secs = (
            None
            if freshness_epoch is None
            else max(0.0, float(time.time()) - float(freshness_epoch))
        )
        return snapshot

    def cache_correlation_snapshot(
        self,
        key: tuple[str, tuple[str, ...], str, int, int],
        snapshot: CorrelationSnapshot,
        freshness_epoch: float | None,
    ) -> None:
        self._correlation_snapshots[key] = (
            clone_flat_dataclass(snapshot),
            freshness_epoch,
        )


@lru_cache(maxsize=4096)
def _cached_heuristic_overlap(symbol: str, other: str) -> float:
    left = infer_instrument_ref(symbol)
    right = infer_instrument_ref(other)
    if left.canonical_symbol == right.canonical_symbol:
        return 1.0
    if left.asset_class != right.asset_class:
        return 0.1
    if left.asset_class == "fx":
        shared = {left.base_ccy, left.quote_ccy} & {right.base_ccy, right.quote_ccy}
        if len(shared) == 2:
            return 0.95
        if len(shared) == 1:
            return 0.6
        return 0.15
    if left.asset_class == "crypto":
        if left.quote_ccy and left.quote_ccy == right.quote_ccy:
            return 0.55
        if left.base_ccy and left.base_ccy == right.base_ccy:
            return 0.45
        return 0.2
    return 0.1


def _heuristic_overlap(symbol: str, other: str) -> float:
    left, right = sorted(
        (
            str(symbol or "").strip().upper(),
            str(other or "").strip().upper(),
        )
    )
    return _cached_heuristic_overlap(left, right)


def _coerce_return_series_map(
    realized_returns_by_pair: Any,
) -> Mapping[str, pd.Series]:
    if isinstance(realized_returns_by_pair, _PreparedReturnSeriesMap):
        return realized_returns_by_pair
    if realized_returns_by_pair is None:
        return {}
    if isinstance(realized_returns_by_pair, pd.DataFrame):
        frame = realized_returns_by_pair.copy()
        pair_col = next((col for col in ("pair", "symbol") if col in frame.columns), "")
        value_col = next((col for col in ("ret_1", "returns", "return", "value") if col in frame.columns), "")
        if not pair_col or not value_col:
            return {}
        frame[pair_col] = frame[pair_col].astype(str).str.upper()
        frame[value_col] = pd.to_numeric(frame[value_col], errors="coerce")
        if "ts" in frame.columns:
            frame["ts"] = pd.to_datetime(frame["ts"], utc=True, errors="coerce")
            pivot = frame.dropna(subset=["ts", value_col]).pivot_table(index="ts", columns=pair_col, values=value_col, aggfunc="last")
            return {str(col).upper(): pivot[col].dropna().astype(float) for col in pivot.columns}
        frame["_row"] = frame.groupby(pair_col).cumcount()
        pivot = frame.dropna(subset=[value_col]).pivot_table(index="_row", columns=pair_col, values=value_col, aggfunc="last")
        return {str(col).upper(): pivot[col].dropna().astype(float) for col in pivot.columns}
    if isinstance(realized_returns_by_pair, Mapping):
        out: dict[str, pd.Series] = {}
        for key, value in realized_returns_by_pair.items():
            pair = str(key or "").strip().upper()
            if not pair:
                continue
            if isinstance(value, pd.Series):
                series = value
                if getattr(series.dtype, "kind", "") != "f":
                    series = pd.to_numeric(series, errors="coerce").astype(float)
            else:
                try:
                    series = pd.Series(list(value), dtype=float)
                except Exception:
                    continue
            values = series.to_numpy(
                dtype=float,
                copy=False,
                na_value=float("nan"),
            )
            finite = np.isfinite(values)
            if not bool(finite.all()):
                series = series.iloc[np.flatnonzero(finite)]
            if series.index.has_duplicates:
                series = series[~series.index.duplicated(keep="last")]
            if isinstance(series.index, pd.DatetimeIndex) and not series.index.is_monotonic_increasing:
                series = series.sort_index()
            if not series.empty:
                out[pair] = series
        return out
    return {}


def prepare_return_series_map(
    realized_returns_by_pair: Any,
) -> Mapping[str, pd.Series]:
    """Validate return series once for reuse across one evaluation cycle."""

    if isinstance(realized_returns_by_pair, _PreparedReturnSeriesMap):
        return realized_returns_by_pair
    return _PreparedReturnSeriesMap(_coerce_return_series_map(realized_returns_by_pair))


def _freshness_measure_from_aligned_latest(
    latest_timestamps: list[pd.Timestamp],
) -> tuple[float | None, float | None]:
    if not latest_timestamps:
        return None, None
    try:
        # Pair-statistics producers already return typed timestamps. Keep their
        # hot path free of repeat validation while retaining the normalization
        # fallback below for direct/external callers.
        latest = min(
            value
            for value in latest_timestamps
            if value is not None
        )
        if latest is pd.NaT:
            raise ValueError("missing_freshness_timestamp")
        if latest.tzinfo is None:
            latest = latest.tz_localize("UTC")
        latest_epoch = float(latest.timestamp())
        return max(0.0, float(time.time()) - latest_epoch), latest_epoch
    except (AttributeError, TypeError, ValueError):
        pass

    valid: list[pd.Timestamp] = []
    for value in latest_timestamps:
        try:
            if bool(pd.isna(value)):
                continue
            timestamp = (
                value if isinstance(value, pd.Timestamp) else pd.Timestamp(value)
            )
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize("UTC")
            valid.append(timestamp)
        except Exception:
            continue
    if not valid:
        return None, None
    try:
        # The oldest contributing pair is the conservative freshness bound.
        latest_epoch = float(min(valid).timestamp())
        return max(0.0, float(time.time()) - latest_epoch), latest_epoch
    except Exception:
        return None, None


def _freshness_secs_from_aligned_latest(latest_timestamps: list[pd.Timestamp]) -> float | None:
    return _freshness_measure_from_aligned_latest(latest_timestamps)[0]


def _aligned_pair_frame(left: pd.Series, right: pd.Series, *, window_bars: int) -> pd.DataFrame:
    frame = pd.concat(
        {"candidate": left, "peer": right},
        axis=1,
        join="inner",
        copy=False,
    )
    if isinstance(frame.index, pd.DatetimeIndex) and not frame.index.is_monotonic_increasing:
        frame = frame.sort_index()
    if int(window_bars) > 0:
        frame = frame.tail(int(window_bars))
    return frame


def _winsorize(values: np.ndarray) -> np.ndarray:
    clean = np.asarray(values, dtype=float)
    if clean.size < 8:
        return clean
    tail_count = max(1, int(np.floor(clean.size * 0.05)))
    if tail_count * 2 >= clean.size:
        return clean
    ordered = np.sort(clean)
    lower = float(ordered[tail_count])
    upper = float(ordered[-tail_count - 1])
    return np.clip(clean, lower, upper)


def _winsorized_pearson(left: pd.Series, right: pd.Series) -> float | None:
    x = _winsorize(left.to_numpy(dtype=float, copy=True))
    y = _winsorize(right.to_numpy(dtype=float, copy=True))
    if x.size != y.size or x.size < 2:
        return None

    # Scale before centering so very large but finite observations cannot
    # overflow the covariance calculation.
    x_scale = float(np.max(np.abs(x)))
    y_scale = float(np.max(np.abs(y)))
    if not np.isfinite(x_scale) or not np.isfinite(y_scale) or x_scale <= 0.0 or y_scale <= 0.0:
        return None
    x_centered = (x / x_scale) - float(np.mean(x / x_scale))
    y_centered = (y / y_scale) - float(np.mean(y / y_scale))
    denominator = float(np.sqrt(np.dot(x_centered, x_centered) * np.dot(y_centered, y_centered)))
    if not np.isfinite(denominator) or denominator <= np.finfo(float).eps:
        return None
    value = float(np.dot(x_centered, y_centered) / denominator)
    if not np.isfinite(value):
        return None
    return float(np.clip(value, -1.0, 1.0))


def _compute_pair_statistics(
    left: pd.Series,
    right: pd.Series,
    *,
    window_bars: int,
    required_obs: int,
) -> tuple[int, float | None, pd.Timestamp | None]:
    frame = _aligned_pair_frame(left, right, window_bars=int(window_bars))
    pair_count = int(len(frame))
    value = (
        _winsorized_pearson(frame["candidate"], frame["peer"])
        if pair_count >= max(2, int(required_obs))
        else None
    )
    latest = (
        pd.Timestamp(frame.index.max())
        if isinstance(frame.index, pd.DatetimeIndex) and not frame.index.empty
        else None
    )
    return pair_count, value, latest


def _pair_statistics(
    series_map: Mapping[str, pd.Series],
    left_key: str,
    right_key: str,
    *,
    window_bars: int,
    required_obs: int,
) -> tuple[int, float | None, pd.Timestamp | None]:
    if isinstance(series_map, _PreparedReturnSeriesMap):
        return series_map.pair_statistics(
            left_key,
            right_key,
            window_bars=int(window_bars),
            required_obs=int(required_obs),
        )
    return _compute_pair_statistics(
        series_map[left_key],
        series_map[right_key],
        window_bars=int(window_bars),
        required_obs=int(required_obs),
    )


def _realized_pair_corr(
    *,
    symbol: str,
    active_symbols: list[str],
    realized_returns_by_pair: Any,
    window_bars: int,
    min_obs: int,
    mode: str,
) -> CorrelationSnapshot | None:
    series_map = _coerce_return_series_map(realized_returns_by_pair)
    symbol_key = str(symbol or "").strip().upper()
    active_keys = sorted(
        {
            str(item or "").strip().upper()
            for item in list(active_symbols or [])
            if str(item or "").strip() and str(item or "").strip().upper() != symbol_key
        }
    )
    if not symbol_key or not active_keys or symbol_key not in series_map:
        return None

    required_obs = max(2, int(min_obs) if int(min_obs) > 1 else 2)
    cache_key = (
        symbol_key,
        tuple(active_keys),
        str(mode),
        int(window_bars),
        int(min_obs),
    )
    if isinstance(series_map, _PreparedReturnSeriesMap):
        cached_snapshot = series_map.correlation_snapshot(cache_key)
        if cached_snapshot is not None:
            return cached_snapshot
    pair_sample_counts = {other: 0 for other in active_keys}
    pair_observation_coverage = {other: 0.0 for other in active_keys}
    realized_scores: dict[str, float] = {}
    contributing_latest: list[pd.Timestamp] = []
    for other in active_keys:
        peer = series_map.get(other)
        if peer is None:
            continue
        pair_count, value, latest = _pair_statistics(
            series_map,
            symbol_key,
            other,
            window_bars=int(window_bars),
            required_obs=required_obs,
        )
        pair_sample_counts[other] = pair_count
        coverage_denominator = max(int(window_bars), int(min_obs), pair_count, 1)
        pair_observation_coverage[other] = min(1.0, float(pair_count) / float(coverage_denominator))
        if pair_count < required_obs:
            continue
        if value is None:
            continue
        realized_scores[other] = float(value)
        if latest is not None:
            contributing_latest.append(latest)
    if not realized_scores:
        return None

    method = "realized" if str(mode) == "realized" else "hybrid"
    scores = dict(realized_scores)
    estimator = "winsorized_pearson"
    if method == "realized":
        missing_peers = [other for other in active_keys if other not in scores]
        for other in missing_peers:
            scores[other] = float(_heuristic_overlap(symbol_key, other))
        if missing_peers:
            estimator = "winsorized_pearson_with_heuristic_fallback"
    else:
        scores = {}
        for other in active_keys:
            heuristic_score = float(_heuristic_overlap(symbol_key, other))
            realized_score = realized_scores.get(other)
            if realized_score is None:
                scores[other] = heuristic_score
                continue
            denominator = float(max(int(window_bars), int(min_obs), 1))
            realized_confidence = max(
                0.0,
                min(1.0, float(pair_sample_counts.get(other, 0)) / denominator),
            )
            # The structural heuristic estimates overlap magnitude, not return
            # direction. Preserve the realized sign while shrinking magnitudes;
            # blending signed values would make a strong negative correlation
            # cancel against the positive heuristic and understate concentration.
            realized_sign = -1.0 if float(realized_score) < 0.0 else 1.0
            blended_magnitude = (
                (1.0 - realized_confidence) * abs(heuristic_score)
                + realized_confidence * abs(float(realized_score))
            )
            scores[other] = float(realized_sign * blended_magnitude)

    contributing_counts = [pair_sample_counts[key] for key in realized_scores]
    sample_count = min(contributing_counts) if contributing_counts else 0
    freshness_secs, freshness_epoch = _freshness_measure_from_aligned_latest(
        contributing_latest
    )
    max_abs_corr = max(abs(float(value)) for value in scores.values())
    avg_abs_corr = sum(abs(float(value)) for value in scores.values()) / max(1, len(scores))
    snapshot = CorrelationSnapshot(
        symbol=symbol_key,
        method=method,
        estimator=estimator,
        window_bars=int(window_bars),
        min_obs=int(min_obs),
        sample_count=int(sample_count),
        freshness_secs=freshness_secs,
        max_abs_corr=float(max_abs_corr),
        avg_abs_corr=float(avg_abs_corr),
        correlated_symbols={str(k): float(v) for k, v in sorted(scores.items())},
        active_pair_count=int(len(active_keys)),
        realized_pair_count=int(len(realized_scores)),
        coverage_ratio=float(len(realized_scores) / len(active_keys)) if active_keys else 0.0,
        pair_sample_counts={str(k): int(v) for k, v in sorted(pair_sample_counts.items())},
        pair_observation_coverage={
            str(k): float(v) for k, v in sorted(pair_observation_coverage.items())
        },
    )
    if isinstance(series_map, _PreparedReturnSeriesMap):
        series_map.cache_correlation_snapshot(
            cache_key,
            snapshot,
            freshness_epoch,
        )
    return snapshot


def compute_correlation_snapshot(
    *,
    symbol: str,
    active_symbols: list[str],
    mode: str = "heuristic",
    realized_returns_by_pair: Any = None,
    window_bars: int = 0,
    min_obs: int = 0,
) -> CorrelationSnapshot:
    symbol_key = str(symbol or "").strip().upper()
    scores: dict[str, float] = {}
    mode_key = str(mode or "heuristic").strip().lower()
    if mode_key in {"realized", "hybrid"}:
        realized_snapshot = _realized_pair_corr(
            symbol=symbol_key,
            active_symbols=active_symbols,
            realized_returns_by_pair=realized_returns_by_pair,
            window_bars=int(window_bars),
            min_obs=int(min_obs),
            mode=mode_key,
        )
        if realized_snapshot is not None:
            return realized_snapshot

    for other in list(active_symbols or []):
        other_key = str(other or "").strip().upper()
        if not other_key or other_key == symbol_key:
            continue
        scores[other_key] = float(_heuristic_overlap(symbol_key, other_key))
    if not scores:
        return CorrelationSnapshot(
            symbol=symbol_key,
            method="heuristic",
            estimator="heuristic",
            window_bars=int(window_bars),
            min_obs=int(min_obs),
            sample_count=0,
            freshness_secs=None,
        )
    max_abs_corr = max(abs(float(value)) for value in scores.values())
    avg_abs_corr = sum(abs(float(value)) for value in scores.values()) / max(1, len(scores))
    return CorrelationSnapshot(
        symbol=symbol_key,
        method="heuristic",
        estimator="heuristic",
        window_bars=int(window_bars),
        min_obs=int(min_obs),
        sample_count=0,
        freshness_secs=None,
        max_abs_corr=float(max_abs_corr),
        avg_abs_corr=float(avg_abs_corr),
        correlated_symbols={str(k): float(v) for k, v in sorted(scores.items())},
        active_pair_count=int(len(scores)),
        realized_pair_count=0,
        coverage_ratio=0.0,
        pair_sample_counts={str(k): 0 for k in sorted(scores)},
        pair_observation_coverage={str(k): 0.0 for k in sorted(scores)},
    )
