from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

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

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _heuristic_overlap(symbol: str, other: str) -> float:
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


def _coerce_return_series_map(realized_returns_by_pair: Any) -> dict[str, pd.Series]:
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
                series = pd.to_numeric(value, errors="coerce").astype(float)
            else:
                try:
                    series = pd.Series(list(value), dtype=float)
                except Exception:
                    continue
            series = series.replace([np.inf, -np.inf], np.nan).dropna()
            if not series.empty:
                out[pair] = series.astype(float)
        return out
    return {}


def _freshness_secs_from_series_map(series_map: dict[str, pd.Series]) -> float | None:
    latest_ts: pd.Timestamp | None = None
    for series in series_map.values():
        if isinstance(series.index, pd.DatetimeIndex) and not series.index.empty:
            ts = pd.Timestamp(series.index.max())
            if pd.notna(ts) and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
    if latest_ts is None:
        return None
    try:
        now = pd.Timestamp.now(tz="UTC")
        latest = latest_ts
        if latest.tzinfo is None:
            latest = latest.tz_localize("UTC")
        else:
            latest = latest.tz_convert("UTC")
        return max(0.0, float((now - latest).total_seconds()))
    except Exception:
        return None


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
    active_keys = [str(item or "").strip().upper() for item in list(active_symbols or []) if str(item or "").strip()]
    active_keys = [item for item in active_keys if item != symbol_key]
    if not symbol_key or not active_keys or symbol_key not in series_map:
        return None

    selected_keys = [symbol_key] + [item for item in active_keys if item in series_map]
    if len(selected_keys) < 2:
        return None
    frame = pd.concat({key: series_map[key].astype(float) for key in selected_keys}, axis=1)
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(how="any")
    if window_bars > 0:
        frame = frame.tail(int(window_bars))
    sample_count = int(len(frame))
    if sample_count <= 0:
        return None
    if sample_count < int(min_obs) and str(mode) == "realized":
        return None

    corr_matrix = frame.corr(method="pearson", min_periods=max(2, int(min_obs) if int(min_obs) > 1 else 2))
    realized_scores: dict[str, float] = {}
    for other in active_keys:
        if other not in corr_matrix.columns:
            continue
        value = corr_matrix.loc[symbol_key, other]
        if pd.notna(value):
            realized_scores[other] = float(value)
    if not realized_scores:
        return None
    method = "realized" if str(mode) == "realized" else "hybrid"
    if str(mode) == "hybrid":
        heuristic_scores = {other: float(_heuristic_overlap(symbol_key, other)) for other in active_keys}
        denominator = float(max(int(window_bars), int(min_obs), 1))
        realized_confidence = max(0.0, min(1.0, float(sample_count) / denominator))
        realized_confidence = 0.0 if sample_count < int(min_obs) else realized_confidence
        realized_scores = {
            other: float(
                ((1.0 - realized_confidence) * float(heuristic_scores.get(other, 0.0)))
                + (realized_confidence * float(score))
            )
            for other, score in realized_scores.items()
        }
    freshness_secs = _freshness_secs_from_series_map(series_map)
    max_abs_corr = max(abs(float(value)) for value in realized_scores.values())
    avg_abs_corr = sum(abs(float(value)) for value in realized_scores.values()) / max(1, len(realized_scores))
    return CorrelationSnapshot(
        symbol=symbol_key,
        method=method,
        window_bars=int(window_bars),
        min_obs=int(min_obs),
        sample_count=int(sample_count),
        freshness_secs=freshness_secs,
        max_abs_corr=float(max_abs_corr),
        avg_abs_corr=float(avg_abs_corr),
        correlated_symbols={str(k): float(v) for k, v in sorted(realized_scores.items())},
    )


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
    realized_snapshot = _realized_pair_corr(
        symbol=symbol_key,
        active_symbols=active_symbols,
        realized_returns_by_pair=realized_returns_by_pair,
        window_bars=int(window_bars),
        min_obs=int(min_obs),
        mode=mode_key,
    )
    if realized_snapshot is not None and mode_key in {"realized", "hybrid"}:
        return realized_snapshot

    for other in list(active_symbols or []):
        other_key = str(other or "").strip().upper()
        if not other_key or other_key == symbol_key:
            continue
        scores[other_key] = float(_heuristic_overlap(symbol_key, other_key))
    if not scores:
        return CorrelationSnapshot(symbol=symbol_key, method="heuristic", window_bars=int(window_bars), min_obs=int(min_obs), sample_count=0, freshness_secs=None)
    max_abs_corr = max(abs(float(value)) for value in scores.values())
    avg_abs_corr = sum(abs(float(value)) for value in scores.values()) / max(1, len(scores))
    return CorrelationSnapshot(
        symbol=symbol_key,
        method="heuristic",
        window_bars=int(window_bars),
        min_obs=int(min_obs),
        sample_count=0,
        freshness_secs=None,
        max_abs_corr=float(max_abs_corr),
        avg_abs_corr=float(avg_abs_corr),
        correlated_symbols={str(k): float(v) for k, v in sorted(scores.items())},
    )
