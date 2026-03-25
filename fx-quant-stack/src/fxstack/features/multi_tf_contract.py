from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.features.fx_lifecycle import add_fx_lifecycle_features
from fxstack.io.parquet_store import ParquetStore


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
    agg = agg.dropna(subset=["mid_open", "mid_high", "mid_low", "mid_close"]).reset_index()
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
) -> tuple[pd.DataFrame, dict[str, Any]]:
    context_timeframes = list(context_timeframes or ["M15", "H1", "H4", "D"])
    store = ParquetStore(Path(raw_store_root))
    anchor_raw = store.read_pair_timeframe(provider=provider, pair=str(pair).upper(), timeframe=str(anchor_timeframe).upper())
    if anchor_raw.empty:
        return pd.DataFrame(), {"pair": str(pair).upper(), "anchor_timeframe": str(anchor_timeframe).upper(), "error": "no_anchor_rows"}

    anchor = add_fx_lifecycle_features(anchor_raw)
    anchor = anchor.rename(columns={"close_ts": "anchor_close_ts"})
    report: dict[str, Any] = {
        "pair": str(pair).upper(),
        "anchor_timeframe": str(anchor_timeframe).upper(),
        "context_timeframes": list(context_timeframes),
        "coverage": {"anchor_rows": int(len(anchor))},
        "null_rates": {},
        "join_integrity": {},
    }

    join_keys = []
    for tf in context_timeframes:
        source = store.read_pair_timeframe(provider=provider, pair=str(pair).upper(), timeframe=str(tf).upper())
        if source.empty and tf in {"M15", "H1"}:
            source = resample_bars(anchor_raw, str(tf).upper())
        if source.empty:
            report["coverage"][tf] = 0
            continue
        ctx = add_fx_lifecycle_features(source)
        ctx = ctx.rename(columns={"close_ts": f"{tf.lower()}_close_ts"})
        cols = [
            "ts",
            f"{tf.lower()}_close_ts",
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
        keep = [c for c in cols if c in ctx.columns]
        ctx = ctx[keep].copy()
        rename = {"ts": f"{tf.lower()}_ts"}
        rename.update({c: f"{tf.lower()}_{c}" for c in ctx.columns if c not in {"ts", f"{tf.lower()}_close_ts"}})
        ctx = ctx.rename(columns=rename).sort_values(f"{tf.lower()}_close_ts")
        anchor = pd.merge_asof(
            anchor.sort_values("anchor_close_ts"),
            ctx,
            left_on="anchor_close_ts",
            right_on=f"{tf.lower()}_close_ts",
            direction="backward",
            allow_exact_matches=True,
        )
        join_keys.append(tf)
        report["coverage"][tf] = int(len(source))
        report["join_integrity"][tf] = {
            "null_rate": float(anchor[f"{tf.lower()}_ret_1"].isna().mean()) if f"{tf.lower()}_ret_1" in anchor.columns else 1.0,
            "max_lag_secs": float(
                (
                    anchor["anchor_close_ts"] - anchor[f"{tf.lower()}_close_ts"]
                ).dt.total_seconds().fillna(0.0).max()
            )
            if f"{tf.lower()}_close_ts" in anchor.columns
            else 0.0,
        }

    pair_set = list(all_pairs or [str(pair).upper()])
    if len(pair_set) > 1:
        cross_frames: list[pd.DataFrame] = []
        for other in pair_set:
            other_raw = store.read_pair_timeframe(provider=provider, pair=str(other).upper(), timeframe=str(anchor_timeframe).upper())
            if other_raw.empty:
                continue
            other_feat = add_fx_lifecycle_features(other_raw)[["ts", "pair", "ret_1", "ret_5", "vol_20"]].copy()
            cross_frames.append(other_feat)
        if cross_frames:
            cross = pd.concat(cross_frames, ignore_index=True)
            pivot = cross.pivot_table(index="ts", columns="pair", values="ret_1")
            if not pivot.empty:
                orient = {}
                for sym in pivot.columns:
                    if sym.startswith("USD"):
                        orient[sym] = 1.0
                    elif sym.endswith("USD"):
                        orient[sym] = -1.0
                    else:
                        orient[sym] = 0.0
                usd_strength = sum(pivot[sym].fillna(0.0) * float(orient.get(sym, 0.0)) for sym in pivot.columns) / max(
                    1,
                    sum(1 for sym in pivot.columns if orient.get(sym, 0.0) != 0.0),
                )
                dispersion = pivot.std(axis=1, ddof=0).fillna(0.0)
                anchor = anchor.merge(
                    pd.DataFrame(
                        {
                            "ts": usd_strength.index,
                            "usd_strength_basket_ret_1": usd_strength.values,
                            "cross_pair_dispersion": dispersion.values,
                        }
                    ),
                    on="ts",
                    how="left",
                )

    anchor["context_frame_profile"] = "hierarchical_v1"
    anchor["h1_available"] = 1 if "h1_ret_1" in anchor.columns else 0
    anchor["date"] = pd.to_datetime(anchor["ts"], utc=True).dt.strftime("%Y-%m-%d")

    numeric = anchor.select_dtypes(include=["number"])
    report["null_rates"] = {col: float(numeric[col].isna().mean()) for col in numeric.columns}
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
    context_timeframes = list(context_timeframes or ["M15", "H1", "H4", "D"])
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
        return pd.DataFrame(), {"pair": pair_txt, "anchor_timeframe": anchor_tf, "error": "no_anchor_rows"}

    anchor = add_fx_lifecycle_features(anchor_raw)
    if anchor.empty:
        return pd.DataFrame(), {"pair": pair_txt, "anchor_timeframe": anchor_tf, "error": "no_anchor_features"}
    anchor = anchor.rename(columns={"close_ts": "anchor_close_ts"})
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
        ctx = add_fx_lifecycle_features(source)
        if ctx.empty:
            report["coverage"][tf_txt] = 0
            continue
        ctx = ctx.rename(columns={"close_ts": f"{tf_txt.lower()}_close_ts"})
        cols = [
            "ts",
            f"{tf_txt.lower()}_close_ts",
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
        keep = [c for c in cols if c in ctx.columns]
        ctx = ctx[keep].copy()
        rename = {"ts": f"{tf_txt.lower()}_ts"}
        rename.update({c: f"{tf_txt.lower()}_{c}" for c in ctx.columns if c not in {"ts", f"{tf_txt.lower()}_close_ts"}})
        ctx = ctx.rename(columns=rename).sort_values(f"{tf_txt.lower()}_close_ts")
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
            "null_rate": float(anchor[f"{tf_txt.lower()}_ret_1"].isna().mean()) if f"{tf_txt.lower()}_ret_1" in anchor.columns else 1.0,
            "max_lag_secs": float(
                (anchor["anchor_close_ts"] - anchor[f"{tf_txt.lower()}_close_ts"]).dt.total_seconds().fillna(0.0).max()
            )
            if f"{tf_txt.lower()}_close_ts" in anchor.columns
            else 0.0,
        }

    pair_set = [str(sym).upper() for sym in list(all_pairs or [pair_txt])]
    if len(pair_set) > 1:
        cross_frames: list[pd.DataFrame] = []
        for other in pair_set:
            other_raw = store.read_recent_rows(
                provider=provider,
                pair=other,
                timeframe=anchor_tf,
                tail_files=4,
                max_rows=max(8, int(cross_max_rows)),
            )
            if other_raw.empty:
                continue
            other_feat = add_fx_lifecycle_features(other_raw)
            if other_feat.empty:
                continue
            cross_frames.append(other_feat[["ts", "pair", "ret_1", "ret_5", "vol_20"]].tail(1).copy())
        if cross_frames:
            cross = pd.concat(cross_frames, ignore_index=True)
            pivot = cross.pivot_table(index="ts", columns="pair", values="ret_1")
            if not pivot.empty:
                orient = {}
                for sym in pivot.columns:
                    if sym.startswith("USD"):
                        orient[sym] = 1.0
                    elif sym.endswith("USD"):
                        orient[sym] = -1.0
                    else:
                        orient[sym] = 0.0
                usd_strength = sum(pivot[sym].fillna(0.0) * float(orient.get(sym, 0.0)) for sym in pivot.columns) / max(
                    1,
                    sum(1 for sym in pivot.columns if orient.get(sym, 0.0) != 0.0),
                )
                dispersion = pivot.std(axis=1, ddof=0).fillna(0.0)
                anchor = anchor.merge(
                    pd.DataFrame(
                        {
                            "ts": usd_strength.index,
                            "usd_strength_basket_ret_1": usd_strength.values,
                            "cross_pair_dispersion": dispersion.values,
                        }
                    ),
                    on="ts",
                    how="left",
                )

    anchor["context_frame_profile"] = "hierarchical_v1_latest"
    anchor["h1_available"] = 1 if "h1_ret_1" in anchor.columns else 0
    anchor["date"] = pd.to_datetime(anchor["ts"], utc=True).dt.strftime("%Y-%m-%d")

    numeric = anchor.select_dtypes(include=["number"])
    report["null_rates"] = {col: float(numeric[col].isna().mean()) for col in numeric.columns}
    report["join_integrity"]["joined_contexts"] = join_keys
    return anchor.dropna().tail(1).reset_index(drop=True), report


def write_data_contract_profile(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
