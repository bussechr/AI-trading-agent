# AGENT: ROLE: Join anchor intraday rows with M15/H1/H4/D context rows and preserve join-integrity diagnostics.
# AGENT: ENTRYPOINT: imported by runtime row preparation and twin preprocessing.
# AGENT: PRIMARY INPUTS: raw parquet bars, provider/pair/timeframe configuration.
# AGENT: PRIMARY OUTPUTS: context-enriched multi-timeframe rows plus coverage diagnostics.
# AGENT: DEPENDS ON: `fxstack/features/fx_lifecycle.py`, `fxstack/io/parquet_store.py`.
# AGENT: CALLED BY: `fxstack/runtime/runner.py`, `tools/fxstack_digital_twin_backtest.py`.
# AGENT: STATE / SIDE EFFECTS: parquet reads only.
# AGENT: HANDSHAKES: multi-timeframe feature contract consumed by live scorer and lifecycle rows.
# AGENT: SEE: `docs/agents/model-stack-and-feature-flow.md` -> `fxstack/features/fx_lifecycle.py` -> `docs/agents/runtime-loop.md`
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from fxstack.features.fx_lifecycle import (
    add_fx_lifecycle_features,
    timeframe_to_timedelta,
)
from fxstack.io.parquet_store import ParquetStore


_DEFAULT_CONTEXT_TIMEFRAMES = ["M15", "H1", "H4", "D"]
_RAW_BAR_COLUMNS = [
    "pair",
    "ts",
    "timeframe",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
    "mid_open",
    "mid_high",
    "mid_low",
    "mid_close",
    "volume",
    "spread",
    "date",
]
_CONTEXT_FEATURE_COLUMNS = [
    "ret_1",
    "ret_5",
    "vol_20",
    "vol_60",
    "atr_14",
    "trend_slope_20",
    "trend_strength_20",
    "trend_slope_60",
    "session_tag",
    "regime_bucket",
    "scenario_bucket",
    "spread_bps",
]
_DERIVED_CONTRACT_COLUMNS = {
    "anchor_close_ts",
    "context_frame_profile",
    "h1_available",
    "usd_strength_basket_ret_1",
    "usd_strength_available",
    "usd_strength_observed_count",
    "usd_strength_coverage",
    "cross_pair_dispersion",
    "cross_pair_available",
    "cross_pair_observed_count",
    "cross_pair_coverage",
    "cross_pair_max_age_secs",
}
_TIMEFRAME_HISTORY_PADDING = {
    "M1": pd.Timedelta(days=2),
    "M5": pd.Timedelta(days=10),
    "M15": pd.Timedelta(days=14),
    "H1": pd.Timedelta(days=21),
    "H4": pd.Timedelta(days=45),
    "D": pd.Timedelta(days=180),
}


def _strip_existing_multi_tf_contract_columns(
    df: pd.DataFrame, *, context_timeframes: list[str]
) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    prefixes = tuple(f"{str(tf).lower()}_" for tf in list(context_timeframes))
    drop_cols = [
        col
        for col in df.columns
        if str(col) in _DERIVED_CONTRACT_COLUMNS or str(col).startswith(prefixes)
    ]
    if not drop_cols:
        return df.copy()
    return df.drop(columns=drop_cols, errors="ignore").copy()


def _sanitize_raw_bar_frame(source: pd.DataFrame) -> pd.DataFrame:
    if source.empty:
        return source.copy()
    keep = [col for col in _RAW_BAR_COLUMNS if col in source.columns]
    return source[keep].copy()


def _normalized_pair_set(pair_set: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in list(pair_set or []):
        symbol = str(value or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def _usd_return_orientation(symbol: str) -> float:
    pair = str(symbol or "").strip().upper()
    if pair.startswith("USD"):
        return 1.0
    if pair.endswith("USD"):
        return -1.0
    return 0.0


def _attach_point_in_time_cross_pair_context(
    anchor: pd.DataFrame,
    *,
    pair_set: list[str],
    anchor_timeframe: str,
    load_raw: Callable[[str], pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Attach synchronized cross-pair context without forward-looking fills.

    Each requested symbol is aligned backward to every anchor timestamp with a
    one-anchor-bar tolerance. USD strength uses signed log returns, for which
    inversion is exact, and averages only the symbols observed at that row.
    Coverage and age columns make partial baskets distinguishable from a true
    zero-return basket.
    """

    out = anchor.copy()
    requested = _normalized_pair_set(pair_set)
    # A one-symbol universe is not cross-pair context. Keep the public feature
    # columns present, but mark the basket unavailable in both batch and latest.
    expected = requested if len(requested) > 1 else []
    tolerance = timeframe_to_timedelta(str(anchor_timeframe).upper())
    row_count = int(len(out))
    expected_count = int(len(expected))
    usd_expected_count = int(
        sum(_usd_return_orientation(symbol) != 0.0 for symbol in expected)
    )

    aligned_returns: list[pd.Series] = []
    aligned_usd_returns: list[pd.Series] = []
    aligned_ages: list[pd.Series] = []
    loaded_symbols: list[str] = []
    aligned_symbols: list[str] = []

    if row_count > 0 and expected:
        anchor_keys = pd.DataFrame(
            {
                "_anchor_pos": np.arange(row_count, dtype=int),
                "_anchor_ts": pd.to_datetime(out["ts"], utc=True, errors="coerce"),
            }
        )
        anchor_keys = anchor_keys.dropna(subset=["_anchor_ts"]).sort_values(
            "_anchor_ts"
        )

        for symbol in expected:
            raw = load_raw(symbol)
            if raw is None or raw.empty:
                continue
            features = add_fx_lifecycle_features(_sanitize_raw_bar_frame(raw))
            if features.empty or "ret_1" not in features.columns:
                continue

            simple_return = pd.to_numeric(features["ret_1"], errors="coerce")
            log_return = pd.Series(np.nan, index=features.index, dtype=float)
            valid_return = (
                simple_return.notna()
                & np.isfinite(simple_return)
                & (simple_return > -1.0)
            )
            log_return.loc[valid_return] = np.log1p(simple_return.loc[valid_return])
            peer = pd.DataFrame(
                {
                    "_peer_ts": pd.to_datetime(
                        features["ts"], utc=True, errors="coerce"
                    ),
                    "_log_return": log_return,
                }
            )
            peer = (
                peer.replace([np.inf, -np.inf], np.nan)
                .dropna(subset=["_peer_ts", "_log_return"])
                .sort_values("_peer_ts")
                .drop_duplicates(subset=["_peer_ts"], keep="last")
            )
            if peer.empty:
                continue
            loaded_symbols.append(symbol)

            aligned = pd.merge_asof(
                anchor_keys,
                peer,
                left_on="_anchor_ts",
                right_on="_peer_ts",
                direction="backward",
                tolerance=tolerance,
                allow_exact_matches=True,
            )
            values = pd.Series(np.nan, index=pd.RangeIndex(row_count), dtype=float)
            ages = pd.Series(np.nan, index=pd.RangeIndex(row_count), dtype=float)
            observed = aligned["_peer_ts"].notna() & aligned["_log_return"].notna()
            if observed.any():
                positions = aligned.loc[observed, "_anchor_pos"].astype(int).to_numpy()
                values.iloc[positions] = aligned.loc[observed, "_log_return"].to_numpy(
                    dtype=float
                )
                ages.iloc[positions] = (
                    (
                        aligned.loc[observed, "_anchor_ts"]
                        - aligned.loc[observed, "_peer_ts"]
                    )
                    .dt.total_seconds()
                    .to_numpy(dtype=float)
                )
                aligned_symbols.append(symbol)
            aligned_returns.append(values.rename(symbol))
            aligned_ages.append(ages.rename(symbol))
            orientation = _usd_return_orientation(symbol)
            if orientation != 0.0:
                aligned_usd_returns.append((values * float(orientation)).rename(symbol))

    if aligned_returns:
        return_frame = pd.concat(aligned_returns, axis=1)
        observed_count = return_frame.notna().sum(axis=1).astype(int)
        dispersion = (
            return_frame.std(axis=1, ddof=0).where(observed_count > 0, 0.0).fillna(0.0)
        )
    else:
        observed_count = pd.Series(0, index=pd.RangeIndex(row_count), dtype=int)
        dispersion = pd.Series(0.0, index=pd.RangeIndex(row_count), dtype=float)

    if aligned_usd_returns:
        usd_frame = pd.concat(aligned_usd_returns, axis=1)
        usd_observed_count = usd_frame.notna().sum(axis=1).astype(int)
        usd_strength = (
            usd_frame.sum(axis=1, skipna=True)
            / usd_observed_count.replace(0, np.nan).astype(float)
        ).fillna(0.0)
    else:
        usd_observed_count = pd.Series(0, index=pd.RangeIndex(row_count), dtype=int)
        usd_strength = pd.Series(0.0, index=pd.RangeIndex(row_count), dtype=float)

    if aligned_ages:
        age_frame = pd.concat(aligned_ages, axis=1)
        max_age_secs = age_frame.max(axis=1, skipna=True).fillna(0.0)
    else:
        max_age_secs = pd.Series(0.0, index=pd.RangeIndex(row_count), dtype=float)

    cross_coverage = (
        observed_count.astype(float) / float(expected_count)
        if expected_count > 0
        else pd.Series(0.0, index=pd.RangeIndex(row_count), dtype=float)
    )
    usd_coverage = (
        usd_observed_count.astype(float) / float(usd_expected_count)
        if usd_expected_count > 0
        else pd.Series(0.0, index=pd.RangeIndex(row_count), dtype=float)
    )

    out["usd_strength_basket_ret_1"] = usd_strength.to_numpy(dtype=float)
    out["cross_pair_dispersion"] = dispersion.to_numpy(dtype=float)
    out["cross_pair_available"] = (observed_count > 0).astype(int).to_numpy()
    out["cross_pair_observed_count"] = observed_count.to_numpy(dtype=int)
    out["cross_pair_coverage"] = cross_coverage.to_numpy(dtype=float)
    out["cross_pair_max_age_secs"] = max_age_secs.to_numpy(dtype=float)
    out["usd_strength_available"] = (usd_observed_count > 0).astype(int).to_numpy()
    out["usd_strength_observed_count"] = usd_observed_count.to_numpy(dtype=int)
    out["usd_strength_coverage"] = usd_coverage.to_numpy(dtype=float)

    report = {
        "return_convention": "signed_log_return",
        "alignment": "backward_asof",
        "tolerance_secs": float(tolerance.total_seconds()),
        "requested_symbols": requested,
        "expected_symbols": expected,
        "loaded_symbols": loaded_symbols,
        "aligned_symbols": aligned_symbols,
        "expected_symbol_count": expected_count,
        "usd_expected_symbol_count": usd_expected_count,
        "min_coverage": float(cross_coverage.min()) if row_count > 0 else 0.0,
        "latest_coverage": float(cross_coverage.iloc[-1]) if row_count > 0 else 0.0,
        "latest_observed_count": int(observed_count.iloc[-1]) if row_count > 0 else 0,
        "latest_max_age_secs": float(max_age_secs.iloc[-1]) if row_count > 0 else 0.0,
    }
    return out, report


def _bounded_start(start_ts: Any | None, *, timeframe: str) -> pd.Timestamp | None:
    parsed = pd.to_datetime(start_ts, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    pad = _TIMEFRAME_HISTORY_PADDING.get(str(timeframe).upper(), pd.Timedelta(days=30))
    return pd.Timestamp(parsed) - pad


def _prepare_anchor_contract_frame(
    anchor_raw: pd.DataFrame, *, context_timeframes: list[str]
) -> pd.DataFrame:
    anchor = add_fx_lifecycle_features(_sanitize_raw_bar_frame(anchor_raw))
    if anchor.empty:
        return pd.DataFrame()
    anchor = _strip_existing_multi_tf_contract_columns(
        anchor, context_timeframes=context_timeframes
    )
    return anchor.rename(columns={"close_ts": "anchor_close_ts"})


def _prepare_context_contract_frame(
    source: pd.DataFrame, *, timeframe: str
) -> pd.DataFrame:
    tf_txt = str(timeframe).upper()
    ctx = add_fx_lifecycle_features(_sanitize_raw_bar_frame(source))
    if ctx.empty:
        return pd.DataFrame()
    close_ts_col = f"{tf_txt.lower()}_close_ts"
    ts_col = f"{tf_txt.lower()}_ts"
    ctx = ctx.drop(columns=[close_ts_col, ts_col], errors="ignore")
    ctx = ctx.rename(columns={"close_ts": close_ts_col})
    cols = ["ts", close_ts_col, *_CONTEXT_FEATURE_COLUMNS]
    keep = [c for c in cols if c in ctx.columns]
    ctx = ctx[keep].copy()
    rename = {"ts": ts_col}
    rename.update(
        {
            c: f"{tf_txt.lower()}_{c}"
            for c in ctx.columns
            if c not in {ts_col, close_ts_col}
        }
    )
    return ctx.rename(columns=rename).sort_values(close_ts_col)


def resample_bars(df: pd.DataFrame, target_timeframe: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    tf = str(target_timeframe).upper()
    freq_map = {
        "M1": "1min",
        "M5": "5min",
        "M15": "15min",
        "M30": "30min",
        "H1": "1h",
        "H4": "4h",
        "D": "1d",
    }
    if tf not in freq_map:
        raise ValueError(f"unsupported timeframe: {target_timeframe}")

    x = df.copy()
    x["ts"] = pd.to_datetime(x["ts"], utc=True)
    x = x.sort_values("ts").set_index("ts")
    agg = x.resample(freq_map[tf], label="left", closed="left").agg(
        {
            "pair": "last",
            "bid_open": "first",
            "bid_high": "max",
            "bid_low": "min",
            "bid_close": "last",
            "ask_open": "first",
            "ask_high": "max",
            "ask_low": "min",
            "ask_close": "last",
            "mid_open": "first",
            "mid_high": "max",
            "mid_low": "min",
            "mid_close": "last",
            "volume": "sum",
            "spread": "mean",
        }
    )
    agg = agg.dropna(
        subset=["mid_open", "mid_high", "mid_low", "mid_close"]
    ).reset_index()
    agg["timeframe"] = tf
    agg["date"] = pd.to_datetime(agg["ts"], utc=True).dt.strftime("%Y-%m-%d")
    return agg


def build_multi_tf_rows(
    *,
    pair: str,
    raw_store_root: Path,
    provider: str,
    anchor_timeframe: str = "M5",
    context_timeframes: list[str] | None = None,
    all_pairs: list[str] | None = None,
    start_ts: Any | None = None,
    end_ts: Any | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    context_timeframes = list(context_timeframes or _DEFAULT_CONTEXT_TIMEFRAMES)
    store = ParquetStore(Path(raw_store_root))
    anchor_tf = str(anchor_timeframe).upper()
    anchor_raw = store.read_pair_timeframe(
        provider=provider,
        pair=str(pair).upper(),
        timeframe=anchor_tf,
        start_ts=_bounded_start(start_ts, timeframe=anchor_tf),
        end_ts=end_ts,
    )
    if anchor_raw.empty:
        return pd.DataFrame(), {
            "pair": str(pair).upper(),
            "anchor_timeframe": anchor_tf,
            "error": "no_anchor_rows",
        }

    anchor = _prepare_anchor_contract_frame(
        anchor_raw, context_timeframes=context_timeframes
    )
    if anchor.empty:
        return pd.DataFrame(), {
            "pair": str(pair).upper(),
            "anchor_timeframe": anchor_tf,
            "error": "no_anchor_features",
        }
    report: dict[str, Any] = {
        "pair": str(pair).upper(),
        "anchor_timeframe": anchor_tf,
        "context_timeframes": list(context_timeframes),
        "coverage": {"anchor_rows": int(len(anchor))},
        "null_rates": {},
        "join_integrity": {},
    }

    join_keys = []
    for tf in context_timeframes:
        tf_txt = str(tf).upper()
        source = store.read_pair_timeframe(
            provider=provider,
            pair=str(pair).upper(),
            timeframe=tf_txt,
            start_ts=_bounded_start(start_ts, timeframe=tf_txt),
            end_ts=end_ts,
        )
        if source.empty and tf_txt in {"M15", "H1"}:
            source = resample_bars(anchor_raw, tf_txt)
        if source.empty:
            report["coverage"][tf_txt] = 0
            continue
        ctx = _prepare_context_contract_frame(source, timeframe=tf_txt)
        if ctx.empty:
            report["coverage"][tf_txt] = 0
            continue
        anchor = pd.merge_asof(
            anchor.sort_values("anchor_close_ts"),
            ctx,
            left_on="anchor_close_ts",
            right_on=f"{tf_txt.lower()}_close_ts",
            direction="backward",
            allow_exact_matches=True,
        )
        join_keys.append(tf_txt)
        report["coverage"][tf_txt] = int(len(source))
        report["join_integrity"][tf_txt] = {
            "null_rate": float(anchor[f"{tf_txt.lower()}_ret_1"].isna().mean())
            if f"{tf_txt.lower()}_ret_1" in anchor.columns
            else 1.0,
            "max_lag_secs": float(
                (anchor["anchor_close_ts"] - anchor[f"{tf_txt.lower()}_close_ts"])
                .dt.total_seconds()
                .fillna(0.0)
                .max()
            )
            if f"{tf_txt.lower()}_close_ts" in anchor.columns
            else 0.0,
        }

    pair_set = list(all_pairs or [str(pair).upper()])

    def _load_cross_history(symbol: str) -> pd.DataFrame:
        return store.read_pair_timeframe(
            provider=provider,
            pair=str(symbol).upper(),
            timeframe=anchor_tf,
            start_ts=_bounded_start(start_ts, timeframe=anchor_tf),
            end_ts=end_ts,
        )

    anchor, cross_pair_report = _attach_point_in_time_cross_pair_context(
        anchor,
        pair_set=pair_set,
        anchor_timeframe=anchor_tf,
        load_raw=_load_cross_history,
    )
    report["cross_pair_context"] = cross_pair_report

    anchor["context_frame_profile"] = "hierarchical_v1"
    anchor["h1_available"] = 1 if "h1_ret_1" in anchor.columns else 0
    anchor["date"] = pd.to_datetime(anchor["ts"], utc=True).dt.strftime("%Y-%m-%d")

    numeric = anchor.select_dtypes(include=["number"])
    report["null_rates"] = {
        col: float(numeric[col].isna().mean()) for col in numeric.columns
    }
    report["join_integrity"]["joined_contexts"] = join_keys
    return anchor.dropna().reset_index(drop=True), report


def build_latest_multi_tf_row(
    *,
    pair: str,
    raw_store_root: Path,
    provider: str,
    anchor_timeframe: str = "M5",
    context_timeframes: list[str] | None = None,
    all_pairs: list[str] | None = None,
    anchor_max_rows: int = 2000,
    context_max_rows: int = 2000,
    cross_max_rows: int = 64,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    context_timeframes = list(context_timeframes or _DEFAULT_CONTEXT_TIMEFRAMES)
    pair_txt = str(pair).upper()
    anchor_tf = str(anchor_timeframe).upper()
    store = ParquetStore(Path(raw_store_root))
    anchor_raw = store.read_recent_rows(
        provider=provider,
        pair=pair_txt,
        timeframe=anchor_tf,
        tail_files=14,
        max_rows=max(200, int(anchor_max_rows)),
    )
    if anchor_raw.empty:
        return pd.DataFrame(), {
            "pair": pair_txt,
            "anchor_timeframe": anchor_tf,
            "error": "no_anchor_rows",
        }

    anchor = _prepare_anchor_contract_frame(
        anchor_raw, context_timeframes=context_timeframes
    )
    if anchor.empty:
        return pd.DataFrame(), {
            "pair": pair_txt,
            "anchor_timeframe": anchor_tf,
            "error": "no_anchor_features",
        }
    anchor = anchor.tail(1).copy()
    report: dict[str, Any] = {
        "pair": pair_txt,
        "anchor_timeframe": anchor_tf,
        "context_timeframes": list(context_timeframes),
        "coverage": {"anchor_rows": int(len(anchor_raw))},
        "null_rates": {},
        "join_integrity": {},
    }

    join_keys = []
    for tf in context_timeframes:
        tf_txt = str(tf).upper()
        source = store.read_recent_rows(
            provider=provider,
            pair=pair_txt,
            timeframe=tf_txt,
            tail_files=14,
            max_rows=max(120, int(context_max_rows)),
        )
        if source.empty and tf_txt in {"M15", "H1"}:
            source = resample_bars(anchor_raw, tf_txt)
        if source.empty:
            report["coverage"][tf_txt] = 0
            continue
        ctx = _prepare_context_contract_frame(source, timeframe=tf_txt)
        if ctx.empty:
            report["coverage"][tf_txt] = 0
            continue
        anchor = pd.merge_asof(
            anchor.sort_values("anchor_close_ts"),
            ctx,
            left_on="anchor_close_ts",
            right_on=f"{tf_txt.lower()}_close_ts",
            direction="backward",
            allow_exact_matches=True,
        )
        join_keys.append(tf_txt)
        report["coverage"][tf_txt] = int(len(source))
        report["join_integrity"][tf_txt] = {
            "null_rate": float(anchor[f"{tf_txt.lower()}_ret_1"].isna().mean())
            if f"{tf_txt.lower()}_ret_1" in anchor.columns
            else 1.0,
            "max_lag_secs": float(
                (anchor["anchor_close_ts"] - anchor[f"{tf_txt.lower()}_close_ts"])
                .dt.total_seconds()
                .fillna(0.0)
                .max()
            )
            if f"{tf_txt.lower()}_close_ts" in anchor.columns
            else 0.0,
        }

    pair_set = [str(sym).upper() for sym in list(all_pairs or [pair_txt])]

    def _load_recent_cross_history(symbol: str) -> pd.DataFrame:
        return store.read_recent_rows(
            provider=provider,
            pair=str(symbol).upper(),
            timeframe=anchor_tf,
            tail_files=4,
            max_rows=max(8, int(cross_max_rows)),
        )

    anchor, cross_pair_report = _attach_point_in_time_cross_pair_context(
        anchor,
        pair_set=pair_set,
        anchor_timeframe=anchor_tf,
        load_raw=_load_recent_cross_history,
    )
    report["cross_pair_context"] = cross_pair_report

    anchor["context_frame_profile"] = "hierarchical_v1_latest"
    anchor["h1_available"] = 1 if "h1_ret_1" in anchor.columns else 0
    anchor["date"] = pd.to_datetime(anchor["ts"], utc=True).dt.strftime("%Y-%m-%d")

    numeric = anchor.select_dtypes(include=["number"])
    report["null_rates"] = {
        col: float(numeric[col].isna().mean()) for col in numeric.columns
    }
    report["join_integrity"]["joined_contexts"] = join_keys
    return anchor.dropna().tail(1).reset_index(drop=True), report


def write_data_contract_profile(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
