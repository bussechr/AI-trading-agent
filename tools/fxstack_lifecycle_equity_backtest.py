from __future__ import annotations

import argparse
import json
import math
from collections import OrderedDict
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fxstack.io.parquet_store import ParquetStore
from fxstack.live.scorer import LiveScorer
from fxstack.runtime.runner import (
    LoadedModelSet,
    _PolicyModelRouter,
    _artifact_value,
    _build_lifecycle_row,
    _entry_order_lots,
    _exit_action_labels,
    _load_artifact_meta,
    _partial_close_plan,
    _required_model_feature_columns,
    _resolve_optional_path,
    _safe_load,
    _score_binary_lifecycle_model,
    _score_exit_policy_model,
    _timeframe_to_seconds,
)
from fxstack.settings import get_settings
from fxstack.training.activation import load_manifest


LOT_UNITS = 100_000.0


@dataclass(slots=True)
class PositionState:
    pair: str
    side: str
    lots: float
    entry_lots: float
    entry_price: float
    open_ts: pd.Timestamp
    open_equity_usd: float
    entry_trade_prob: float
    realized_pnl_usd: float = 0.0
    partial_exit_events: int = 0


@dataclass(slots=True)
class ClosedTrade:
    pair: str
    side: str
    open_ts: str
    close_ts: str
    entry_price: float
    exit_price: float
    lots: float
    realized_pnl_usd: float
    holding_bars: int
    partial_exit_events: int
    close_reason: str
    entry_trade_prob: float
    exit_action_selected: str
    reversal_failure_prob: float
    reversal_opportunity_prob: float


TRADE_COLUMNS = [field.name for field in ClosedTrade.__dataclass_fields__.values()]


@dataclass(slots=True)
class LifecycleCacheEntry:
    matrix: np.ndarray
    col_index: dict[str, int]
    index_cache: dict[int, np.ndarray]

    def indices_for(self, model: Any) -> np.ndarray:
        key = id(model)
        cached = self.index_cache.get(key)
        if cached is not None:
            return cached
        cols = list(getattr(model, "feature_columns", []) or [])
        if not cols:
            cols = list(self.col_index.keys())
        idx = np.asarray([self.col_index[col] for col in cols], dtype=np.int32)
        self.index_cache[key] = idx
        return idx


def _parse_pairs(raw: str, default: list[str]) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").split(","):
        pair = str(part).strip().upper()
        if pair:
            out.append(pair)
    return out or list(default)


def _vector_meta_input(
    model: Any,
    base_df: pd.DataFrame,
    *,
    regime_prob: pd.Series,
    swing_prob: pd.Series,
    entry_prob: pd.Series,
    side: pd.Series,
) -> pd.DataFrame:
    x = base_df.copy()
    required = set(getattr(model, "feature_columns", []) or [])
    side_norm = side.astype(str).str.strip().str.lower()
    side_flag = np.where(side_norm.eq("long"), 1.0, -1.0)
    derived: dict[str, Any] = {
        "regime_prob": regime_prob.astype(float),
        "swing_prob": swing_prob.astype(float),
        "entry_prob": entry_prob.astype(float),
        "candidate_side": side_flag,
        "side_long": side_norm.eq("long").astype(float),
        "side_short": side_norm.eq("short").astype(float),
    }
    for key, values in derived.items():
        if key in x.columns:
            continue
        if required and key not in required:
            continue
        x[key] = values
    return x.select_dtypes(include=["number"]).copy()


def _context_input(df: pd.DataFrame, *, model: Any, prefix: str) -> pd.DataFrame:
    required = list(getattr(model, "feature_columns", []) or [])
    if not required and str(getattr(model, "name", "")) == "regime_hmm":
        required = ["ret_1", "ret_5", "vol_20", "vol_60", "trend_slope_20"]
    data: dict[str, Any] = {}
    missing: list[str] = []
    for col in required:
        prefixed = f"{prefix}{col}"
        if prefixed in df.columns:
            data[col] = df[prefixed]
        elif col in df.columns:
            data[col] = df[col]
        else:
            missing.append(col)
    if missing:
        raise ValueError(f"missing {prefix} context columns: {','.join(missing)}")
    out = pd.DataFrame(data, index=df.index)
    return LiveScorer._model_input(model, out)


def _expected_edge_bps_frame(
    df: pd.DataFrame,
    *,
    regime_prob: pd.Series,
    swing_prob: pd.Series,
    entry_prob: pd.Series,
    trade_prob: pd.Series,
    side: pd.Series,
) -> pd.Series:
    mid = df["mid_close"].abs().clip(lower=1e-9)
    atr_bps = (df["atr_14"].abs() / mid) * 10000.0
    trend_bps = pd.concat(
        [
            df["trend_slope_20"].abs() * 10000.0,
            df["trend_slope_60"].abs() * 10000.0,
        ],
        axis=1,
    ).max(axis=1)
    vol_bps = pd.concat(
        [
            df["vol_20"].abs() * 10000.0,
            df["vol_60"].abs() * 10000.0,
        ],
        axis=1,
    ).max(axis=1)
    opportunity_bps = pd.concat([atr_bps, trend_bps, vol_bps], axis=1).max(axis=1)
    directional_prob = np.where(side.astype(str).str.lower().eq("short"), 1.0 - swing_prob, swing_prob)
    blended = (0.35 * directional_prob) + (0.25 * entry_prob) + (0.25 * trade_prob) + (0.15 * regime_prob)
    conviction = ((blended - 0.5) * 2.0).clip(lower=0.0, upper=1.0)
    return opportunity_bps * conviction


def _score_binary_lifecycle_model_fast(model: Any, row_vec: np.ndarray, entry: LifecycleCacheEntry) -> float:
    if model is None:
        return 0.0
    raw = np.asarray(model.model.predict_proba(row_vec[entry.indices_for(model)].reshape(1, -1)), dtype=float)
    p1 = raw[:, 1]
    calibrator = getattr(model, "calibrator", None)
    if calibrator is not None:
        p1 = np.asarray(calibrator.transform(p1), dtype=float)
    return float(np.clip(p1[0], 0.0, 1.0))


def _score_exit_policy_model_fast(model: Any, row_vec: np.ndarray, entry: LifecycleCacheEntry, *, action_labels: dict[int, str]) -> dict[str, Any]:
    if model is None:
        return {"selected": "hold", "score": 0.0, "probs": {}}
    raw = np.asarray(model.model.predict_proba(row_vec[entry.indices_for(model)].reshape(1, -1)), dtype=float)
    classes = [int(x) for x in list(getattr(model, "classes_", []) or [])]
    if not classes:
        classes = list(range(raw.shape[1]))
    probs: dict[str, float] = {}
    for idx, class_id in enumerate(classes):
        label = str(action_labels.get(int(class_id), f"class_{class_id}"))
        probs[label] = float(raw[0, idx])
    selected = max(probs.items(), key=lambda item: float(item[1]))[0] if probs else "hold"
    return {"selected": str(selected), "score": float(probs.get(selected, 0.0)), "probs": probs}


def _gate_frame(
    *,
    spread_bps: pd.Series,
    expected_edge_bps: pd.Series,
    swing_prob: pd.Series,
    entry_prob: pd.Series,
    trade_prob: pd.Series,
    side: pd.Series,
    settings: Any,
) -> pd.DataFrame:
    directional_swing = np.where(side.astype(str).str.lower().eq("short"), 1.0 - swing_prob, swing_prob)
    allowed = pd.Series(True, index=spread_bps.index, dtype=bool)
    reason = pd.Series("none", index=spread_bps.index, dtype="object")

    mask = spread_bps.astype(float) > float(settings.max_allowed_spread_bps)
    reason.loc[mask] = "spread_too_wide"
    allowed.loc[mask] = False

    mask = allowed & (expected_edge_bps.astype(float) < float(settings.min_expected_edge_bps))
    reason.loc[mask] = "edge_below_hurdle"
    allowed.loc[mask] = False

    mask = allowed & (directional_swing.astype(float) < float(settings.min_swing_prob))
    reason.loc[mask] = "weak_swing"
    allowed.loc[mask] = False

    mask = allowed & (entry_prob.astype(float) < float(settings.min_entry_prob))
    reason.loc[mask] = "weak_entry"
    allowed.loc[mask] = False

    mask = allowed & (trade_prob.astype(float) < float(settings.min_trade_prob))
    reason.loc[mask] = "meta_reject"
    allowed.loc[mask] = False

    return pd.DataFrame(
        {
            "allowed": allowed.astype(bool),
            "rejection_reason": reason.astype(str),
            "directional_swing_prob": directional_swing.astype(float),
        },
        index=spread_bps.index,
    )


def _load_model_sets_from_manifest(*, pairs: list[str], project_root: Path) -> dict[str, LoadedModelSet]:
    from fxstack.models.exit_policy_xgb import ExitPolicyXGB
    from fxstack.models.intraday_xgb import IntradayXGB
    from fxstack.models.meta_filter import MetaFilterXGB
    from fxstack.models.regime_hmm import RegimeHMM
    from fxstack.models.reversal_failure_xgb import ReversalFailureXGB
    from fxstack.models.reversal_opportunity_xgb import ReversalOpportunityXGB
    from fxstack.models.swing_xgb import SwingXGB

    s = get_settings()
    manifest_path = _resolve_optional_path(str(s.model_activation_manifest), project_root)
    if manifest_path is None:
        raise FileNotFoundError(f"missing manifest: {s.model_activation_manifest}")
    manifest = load_manifest(manifest_path)
    active = dict(manifest.get("active_model_sets") or {})
    out: dict[str, LoadedModelSet] = {}

    for pair in pairs:
        row = dict(active.get(pair, {}) or {})
        if not row or not bool(row.get("enabled", True)):
            raise RuntimeError(f"pair missing from active manifest: {pair}")
        art = dict(row.get("artifacts") or {})
        meta_json = dict(row.get("metadata") or {})
        policy_json = dict(meta_json.get("policies") or row.get("policies") or {})

        configured_swing_policy = str(s.swing_model_policy or "").strip()
        configured_intraday_policy = str(s.intraday_model_policy or "").strip()
        manifest_swing_policy = str(policy_json.get("swing") or "").strip()
        manifest_intraday_policy = str(policy_json.get("intraday") or "").strip()
        swing_policy = configured_swing_policy or manifest_swing_policy
        intraday_policy = configured_intraday_policy or manifest_intraday_policy
        if str(configured_swing_policy).lower() != "xgb_only" and manifest_swing_policy:
            swing_policy = manifest_swing_policy
        if str(configured_intraday_policy).lower() != "xgb_only" and manifest_intraday_policy:
            intraday_policy = manifest_intraday_policy

        regime, regime_err = _safe_load(RegimeHMM, _artifact_value(art, "regime"), project_root)
        meta_model, meta_err = _safe_load(MetaFilterXGB, _artifact_value(art, "meta"), project_root)
        if regime is None or meta_model is None:
            raise RuntimeError(f"failed loading core models for {pair}: regime={regime_err}, meta={meta_err}")

        swing_tf = None
        swing_xgb = None
        if str(swing_policy).lower() == "transformer_primary_xgb_fallback":
            from fxstack.models.swing_transformer import SwingTransformer

            swing_tf, _ = _safe_load(SwingTransformer, _artifact_value(art, "swing_transformer"), project_root)
            swing_xgb, swing_err = _safe_load(SwingXGB, _artifact_value(art, "swing_xgb", "swing"), project_root)
        else:
            swing_xgb, swing_err = _safe_load(SwingXGB, _artifact_value(art, "swing_xgb", "swing"), project_root)
        if swing_tf is None and swing_xgb is None:
            raise RuntimeError(f"failed loading swing models for {pair}: {swing_err}")

        intraday_tcn = None
        intraday_xgb = None
        if str(intraday_policy).lower() == "tcn_primary_xgb_fallback":
            from fxstack.models.intraday_tcn import IntradayTCN

            intraday_tcn, _ = _safe_load(IntradayTCN, _artifact_value(art, "intraday_tcn"), project_root)
            intraday_xgb, intraday_err = _safe_load(
                IntradayXGB,
                _artifact_value(art, "intraday_xgb", "intraday"),
                project_root,
            )
        else:
            intraday_xgb, intraday_err = _safe_load(
                IntradayXGB,
                _artifact_value(art, "intraday_xgb", "intraday"),
                project_root,
            )
        if intraday_tcn is None and intraday_xgb is None:
            raise RuntimeError(f"failed loading intraday models for {pair}: {intraday_err}")

        exit_path = _artifact_value(art, "exit_policy", "exit", "exit_model")
        exit_model, exit_err = _safe_load(ExitPolicyXGB, exit_path, project_root)
        if str(exit_path).strip() and exit_model is None:
            raise RuntimeError(f"failed loading exit model for {pair}: {exit_err}")
        exit_meta = _load_artifact_meta(exit_path, project_root) if str(exit_path).strip() else {}
        if exit_model is not None and not getattr(exit_model, "feature_columns", None):
            setattr(exit_model, "feature_columns", list(exit_meta.get("feature_columns") or []))
        exit_action_labels = _exit_action_labels(exit_meta, getattr(exit_model, "classes_", None))

        reversal_failure_model, failure_err = _safe_load(
            ReversalFailureXGB,
            _artifact_value(art, "reversal_failure", "reversal_failure_xgb"),
            project_root,
        )
        reversal_opportunity_model, opportunity_err = _safe_load(
            ReversalOpportunityXGB,
            _artifact_value(art, "reversal_opportunity", "reversal_opportunity_xgb"),
            project_root,
        )
        if _artifact_value(art, "reversal_failure", "reversal_failure_xgb") and reversal_failure_model is None:
            raise RuntimeError(f"failed loading reversal failure model for {pair}: {failure_err}")
        if _artifact_value(art, "reversal_opportunity", "reversal_opportunity_xgb") and reversal_opportunity_model is None:
            raise RuntimeError(f"failed loading reversal opportunity model for {pair}: {opportunity_err}")

        swing_router = _PolicyModelRouter(
            policy=swing_policy,
            family="swing",
            primary_name="swing_transformer" if str(swing_policy).lower() == "transformer_primary_xgb_fallback" else "swing_xgb",
            primary_model=swing_tf if str(swing_policy).lower() == "transformer_primary_xgb_fallback" else swing_xgb,
            fallback_name="swing_xgb",
            fallback_model=swing_xgb if str(swing_policy).lower() == "transformer_primary_xgb_fallback" else None,
        )
        intraday_router = _PolicyModelRouter(
            policy=intraday_policy,
            family="intraday",
            primary_name="intraday_tcn" if str(intraday_policy).lower() == "tcn_primary_xgb_fallback" else "intraday_xgb",
            primary_model=intraday_tcn if str(intraday_policy).lower() == "tcn_primary_xgb_fallback" else intraday_xgb,
            fallback_name="intraday_xgb",
            fallback_model=intraday_xgb if str(intraday_policy).lower() == "tcn_primary_xgb_fallback" else None,
        )

        has_exit_model = bool(exit_model is not None)
        has_reversal_models = bool(reversal_failure_model is not None and reversal_opportunity_model is not None)
        out[pair] = LoadedModelSet(
            pair=pair,
            model_set_id=str(row.get("model_set_id") or "unknown"),
            registry_path=str(row.get("registry_path") or ""),
            scorer=LiveScorer(
                regime_model=regime,
                swing_model=swing_router,
                intraday_model=intraday_router,
                meta_model=meta_model,
            ),
            swing_router=swing_router,
            intraday_router=intraday_router,
            exit_model=exit_model,
            reversal_failure_model=reversal_failure_model,
            reversal_opportunity_model=reversal_opportunity_model,
            exit_action_labels=exit_action_labels,
            lifecycle_activation_mode="model_driven" if (has_exit_model or has_reversal_models) else "runtime_soft",
            has_exit_model=has_exit_model,
            has_reversal_models=has_reversal_models,
        )
    return out


def _prepare_pair_decisions(
    *,
    pair: str,
    loaded: LoadedModelSet,
    feature_store: ParquetStore,
    provider: str,
    intraday_timeframe: str,
    start_ts: pd.Timestamp | None = None,
    end_ts: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    df = feature_store.read_pair_timeframe(provider=provider, pair=pair, timeframe=intraday_timeframe)
    if df.empty:
        raise RuntimeError(f"no feature rows for {pair} {intraday_timeframe}")
    df = df.sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df[df["ts"].notna()].reset_index(drop=True)
    if start_ts is not None:
        df = df[df["ts"] >= start_ts].reset_index(drop=True)
    if end_ts is not None:
        df = df[df["ts"] <= end_ts].reset_index(drop=True)
    if df.empty:
        raise RuntimeError(f"no timestamped feature rows for {pair}")

    regime_input = _context_input(df, model=loaded.scorer.regime_model, prefix="h4_")
    swing_input = _context_input(df, model=loaded.swing_router.primary_model or loaded.swing_router.fallback_model, prefix="d_")
    intraday_input = df.select_dtypes(include=["number"]).copy()
    scorer = loaded.scorer

    regime_proba = loaded.scorer.regime_model.predict_proba(regime_input)
    swing_proba = loaded.scorer.swing_model.predict_proba(swing_input)
    intraday_proba = loaded.scorer.intraday_model.predict_proba(scorer._model_input(loaded.scorer.intraday_model, intraday_input))

    regime_prob = regime_proba.max(axis=1).astype(float)
    swing_prob = swing_proba["p1"].astype(float)
    entry_prob = intraday_proba["p1"].astype(float)
    side = pd.Series(np.where(swing_prob >= 0.5, "long", "short"), index=df.index, dtype="object")

    meta_input = _vector_meta_input(
        loaded.scorer.meta_model,
        df,
        regime_prob=regime_prob,
        swing_prob=swing_prob,
        entry_prob=entry_prob,
        side=side,
    )
    meta_proba = loaded.scorer.meta_model.predict_proba(scorer._model_input(loaded.scorer.meta_model, meta_input))
    trade_prob = meta_proba["p1"].astype(float)

    spread_bps = (
        df["spread_bps"].astype(float)
        if "spread_bps" in df.columns
        else ((df["ask_close"] - df["bid_close"]).abs() / df["mid_close"].abs().clip(lower=1e-9) * 10000.0)
    )
    expected_edge_bps = _expected_edge_bps_frame(
        df,
        regime_prob=regime_prob,
        swing_prob=swing_prob,
        entry_prob=entry_prob,
        trade_prob=trade_prob,
        side=side,
    )
    gate = _gate_frame(
        spread_bps=spread_bps,
        expected_edge_bps=expected_edge_bps,
        swing_prob=swing_prob,
        entry_prob=entry_prob,
        trade_prob=trade_prob,
        side=side,
        settings=get_settings(),
    )

    decisions = pd.DataFrame(
        {
            "ts": df["ts"],
            "side": pd.Series(np.where(side.eq("long"), "BUY", "SELL"), index=df.index),
            "signal_side": side,
            "expected_edge_bps": expected_edge_bps.astype(float),
            "spread_bps": spread_bps.astype(float),
            "regime_prob": regime_prob.astype(float),
            "swing_prob": swing_prob.astype(float),
            "entry_prob": entry_prob.astype(float),
            "trade_prob": trade_prob.astype(float),
            "allowed": gate["allowed"].astype(bool),
            "rejection_reason": gate["rejection_reason"].astype(str),
            "bid_close": df["bid_close"].astype(float),
            "ask_close": df["ask_close"].astype(float),
            "mid_close": df["mid_close"].astype(float),
        }
    ).set_index("ts")

    lifecycle_columns = sorted(
        set(_required_model_feature_columns(loaded.exit_model, loaded.reversal_failure_model, loaded.reversal_opportunity_model))
        | {"pair", "ts", "bid_close", "ask_close", "mid_close"}
    )
    lifecycle_columns = [col for col in lifecycle_columns if col in df.columns]
    return decisions, df[["ts", "bid_close", "ask_close", "mid_close"]].copy(), lifecycle_columns


def _quote_to_usd_rate(*, quote_currency: str, bar_idx: int, mid_arrays: dict[str, np.ndarray]) -> float:
    quote = str(quote_currency).upper()
    if quote == "USD":
        return 1.0
    direct = {
        "EUR": "EURUSD",
        "GBP": "GBPUSD",
        "AUD": "AUDUSD",
        "NZD": "NZDUSD",
    }
    inverse = {
        "JPY": "USDJPY",
        "CHF": "USDCHF",
        "CAD": "USDCAD",
    }
    if quote in direct:
        pair = direct[quote]
        value = float(mid_arrays[pair][bar_idx])
        return value if math.isfinite(value) and value > 0.0 else 0.0
    if quote in inverse:
        pair = inverse[quote]
        value = float(mid_arrays[pair][bar_idx])
        return (1.0 / value) if math.isfinite(value) and value > 0.0 else 0.0
    raise KeyError(f"unsupported quote currency for usd conversion: {quote}")


def _realized_pnl_usd(*, pair: str, side: str, entry_price: float, exit_price: float, lots: float, bar_idx: int, mid_arrays: dict[str, np.ndarray]) -> float:
    units = float(lots) * LOT_UNITS
    if str(side).lower() == "long":
        pnl_quote = (float(exit_price) - float(entry_price)) * units
    else:
        pnl_quote = (float(entry_price) - float(exit_price)) * units
    quote_ccy = str(pair)[3:6]
    fx = _quote_to_usd_rate(quote_currency=quote_ccy, bar_idx=bar_idx, mid_arrays=mid_arrays)
    return float(pnl_quote * fx)


def _apply_slippage(*, price: float, action: str, slippage_bps: float) -> float:
    px = float(price)
    bps = max(0.0, float(slippage_bps))
    if px <= 0.0 or bps <= 0.0:
        return px
    factor = bps / 10000.0
    if action in {"buy_open", "short_close"}:
        return px * (1.0 + factor)
    if action in {"sell_open", "long_close"}:
        return px * (1.0 - factor)
    return px


class LifecycleFrameCache:
    def __init__(
        self,
        *,
        feature_store: ParquetStore,
        provider: str,
        timeframe: str,
        column_map: dict[str, list[str]],
        timeline: pd.Index,
        max_pairs: int,
    ) -> None:
        self.feature_store = feature_store
        self.provider = provider
        self.timeframe = timeframe
        self.column_map = column_map
        self.timeline = timeline
        self.max_pairs = max(1, int(max_pairs))
        self.cache: OrderedDict[str, LifecycleCacheEntry] = OrderedDict()

    def get(self, pair: str) -> LifecycleCacheEntry:
        key = str(pair).upper()
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        df = self.feature_store.read_pair_timeframe(provider=self.provider, pair=key, timeframe=self.timeframe)
        if df.empty:
            raise RuntimeError(f"missing lifecycle frame for {key}")
        frame = df.copy()
        if "live_edge_decay" not in frame.columns:
            frame["live_edge_decay"] = frame.get("edge_decay_12", 0.0)
        if "h1_available" not in frame.columns:
            frame["h1_available"] = 1.0 if any(str(col).startswith("h1_") for col in frame.columns) else 0.0
        if "time_in_trade_bars" not in frame.columns:
            frame["time_in_trade_bars"] = 0.0
        if "open_position_count" not in frame.columns:
            frame["open_position_count"] = 0.0
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True, errors="coerce")
        frame = frame[frame["ts"].notna()].sort_values("ts").set_index("ts").reindex(self.timeline)
        cols = [col for col in self.column_map.get(key, []) if col in frame.columns and col not in {"pair", "ts"}]
        numeric = frame[cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
        entry = LifecycleCacheEntry(
            matrix=numeric.to_numpy(dtype=np.float32, copy=False),
            col_index={col: idx for idx, col in enumerate(numeric.columns)},
            index_cache={},
        )
        self.cache[key] = entry
        while len(self.cache) > self.max_pairs:
            self.cache.popitem(last=False)
        return entry


def _mark_equity(
    *,
    cash_balance: float,
    open_positions: dict[str, PositionState],
    bar_idx: int,
    bid_arrays: dict[str, np.ndarray],
    ask_arrays: dict[str, np.ndarray],
    mid_arrays: dict[str, np.ndarray],
) -> float:
    equity = float(cash_balance)
    for pos in open_positions.values():
        if pos.side == "long":
            exit_price = float(bid_arrays[pos.pair][bar_idx])
        else:
            exit_price = float(ask_arrays[pos.pair][bar_idx])
        equity += _realized_pnl_usd(
            pair=pos.pair,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            lots=pos.lots,
            bar_idx=bar_idx,
            mid_arrays=mid_arrays,
        )
    return float(equity)


def run_backtest(args: argparse.Namespace) -> dict[str, Any]:
    s = get_settings()
    project_root = Path(s.project_root)
    feature_root = Path(str(args.feature_root or (project_root / "data" / "features")))
    out_dir = Path(str(args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs = _parse_pairs(args.pairs, s.pairs)
    provider = str(s.normalized_data_provider)
    intraday_timeframe = str(s.intraday_timeframe).upper()

    feature_store = ParquetStore(feature_root)
    model_sets = _load_model_sets_from_manifest(pairs=pairs, project_root=project_root)
    start_bound = pd.to_datetime(args.start_ts, utc=True) if str(args.start_ts or "").strip() else None
    end_bound = pd.to_datetime(args.end_ts, utc=True) if str(args.end_ts or "").strip() else None

    decision_frames: dict[str, pd.DataFrame] = {}
    price_frames: dict[str, pd.DataFrame] = {}
    lifecycle_columns: dict[str, list[str]] = {}
    for pair in pairs:
        print(f"[backtest] precompute pair={pair}", flush=True)
        decisions, prices, life_cols = _prepare_pair_decisions(
            pair=pair,
            loaded=model_sets[pair],
            feature_store=feature_store,
            provider=provider,
            intraday_timeframe=intraday_timeframe,
            start_ts=start_bound,
            end_ts=end_bound,
        )
        decision_frames[pair] = decisions
        price_frames[pair] = prices.set_index("ts")
        lifecycle_columns[pair] = life_cols

    start_ts = max(df.index.min() for df in decision_frames.values())
    end_ts = min(df.index.max() for df in decision_frames.values())
    if str(args.start_ts or "").strip():
        start_ts = max(start_ts, pd.to_datetime(args.start_ts, utc=True))
    if str(args.end_ts or "").strip():
        end_ts = min(end_ts, pd.to_datetime(args.end_ts, utc=True))
    if start_ts >= end_ts:
        raise RuntimeError("invalid backtest range after overlap trim")

    for pair in pairs:
        decision_frames[pair] = decision_frames[pair].loc[(decision_frames[pair].index >= start_ts) & (decision_frames[pair].index <= end_ts)]
        price_frames[pair] = price_frames[pair].loc[(price_frames[pair].index >= start_ts) & (price_frames[pair].index <= end_ts)]

    timeline = decision_frames[pairs[0]].index
    for pair in pairs[1:]:
        timeline = timeline.intersection(decision_frames[pair].index)
    timeline = timeline.sort_values()
    if len(timeline) == 0:
        raise RuntimeError("no common timestamps across selected pairs")
    timeline = pd.Index(timeline)
    decision_arrays: dict[str, dict[str, np.ndarray]] = {}
    bid_arrays: dict[str, np.ndarray] = {}
    ask_arrays: dict[str, np.ndarray] = {}
    mid_arrays: dict[str, np.ndarray] = {}
    for pair in pairs:
        frame = decision_frames[pair].reindex(timeline)
        decision_arrays[pair] = {col: frame[col].to_numpy() for col in frame.columns}
        prices = price_frames[pair].reindex(timeline).ffill()
        bid_arrays[pair] = prices["bid_close"].to_numpy(dtype=float)
        ask_arrays[pair] = prices["ask_close"].to_numpy(dtype=float)
        mid_arrays[pair] = prices["mid_close"].to_numpy(dtype=float)

    lifecycle_cache = LifecycleFrameCache(
        feature_store=feature_store,
        provider=provider,
        timeframe=intraday_timeframe,
        column_map=lifecycle_columns,
        timeline=timeline,
        max_pairs=max(6, int(args.lifecycle_cache_pairs)),
    )

    cash_balance = float(args.start_equity)
    equity_curve: list[dict[str, Any]] = []
    open_positions: dict[str, PositionState] = {}
    closed_trades: list[ClosedTrade] = []
    rejection_counts: dict[str, int] = {}
    entry_count = 0
    partial_exit_count = 0
    reversal_exit_count = 0
    holding_bar_secs = max(1, int(_timeframe_to_seconds(intraday_timeframe) or 300))

    timeline_total = int(len(timeline))
    for idx, ts in enumerate(timeline, start=1):
        if idx == 1 or idx % 5000 == 0 or idx == timeline_total:
            print(f"[backtest] simulate bars={idx}/{timeline_total} open_positions={len(open_positions)}", flush=True)
        ts_dt = pd.Timestamp(ts).tz_convert("UTC") if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts, tz="UTC")
        bar_idx = idx - 1
        current_equity = _mark_equity(
            cash_balance=cash_balance,
            open_positions=open_positions,
            bar_idx=bar_idx,
            bid_arrays=bid_arrays,
            ask_arrays=ask_arrays,
            mid_arrays=mid_arrays,
        )
        positions_snapshot = dict(open_positions)
        total_count_snapshot = len(positions_snapshot)

        for pair in pairs:
            signal_row = decision_arrays[pair]
            signal = {
                "side": signal_row["side"][bar_idx],
                "trade_prob": float(signal_row["trade_prob"][bar_idx]),
                "allowed": bool(signal_row["allowed"][bar_idx]),
                "rejection_reason": signal_row["rejection_reason"][bar_idx],
            }
            loaded = model_sets[pair]
            pos_snapshot = positions_snapshot.get(pair)
            live_pos = open_positions.get(pair)
            pair_count = 1 if pos_snapshot is not None else 0
            total_count = int(total_count_snapshot)
            decision_reasons: list[str] = []
            if not bool(signal["allowed"]):
                decision_reasons.append(str(signal["rejection_reason"]))
            if pair_count >= int(s.max_pair_positions):
                decision_reasons.append("pair_exposure_cap")
            if total_count >= int(s.max_total_positions):
                decision_reasons.append("portfolio_exposure_cap")
            decision_reasons = list(dict.fromkeys(decision_reasons))
            ready = len(decision_reasons) == 0
            desired_side = "long" if str(signal["side"]) == "BUY" else "short"
            pos_side = str(pos_snapshot.side) if pos_snapshot is not None else "flat"
            reversal_blocking_reasons = [r for r in decision_reasons if r not in {"pair_exposure_cap", "portfolio_exposure_cap"}]
            reversal_context_active = desired_side != "flat" and pos_side != "flat" and desired_side != pos_side
            lifecycle_action = "hold"
            lifecycle_reason = "hold"
            exit_action_selected = "hold"
            reversal_failure_prob = 0.0
            reversal_opportunity_prob = 0.0
            exit_action_score = 0.0
            close_lots = 0.0

            if pos_snapshot is not None and bool(s.enable_lifecycle_actions):
                life_entry = lifecycle_cache.get(pair)
                if bar_idx >= len(life_entry.matrix):
                    continue
                lifecycle_row = life_entry.matrix[bar_idx].copy()
                time_idx = life_entry.col_index.get("time_in_trade_bars")
                if time_idx is not None:
                    lifecycle_row[time_idx] = max(
                        0.0,
                        float(ts_dt.timestamp() - pos_snapshot.open_ts.timestamp()) / float(holding_bar_secs),
                    )
                count_idx = life_entry.col_index.get("open_position_count")
                if count_idx is not None:
                    lifecycle_row[count_idx] = float(total_count)
                if loaded.exit_model is not None:
                    exit_diag = _score_exit_policy_model_fast(
                        loaded.exit_model,
                        lifecycle_row,
                        life_entry,
                        action_labels=loaded.exit_action_labels,
                    )
                    exit_action_selected = str(exit_diag.get("selected") or "hold")
                    exit_action_score = float(exit_diag.get("score") or 0.0)
                if loaded.reversal_failure_model is not None:
                    reversal_failure_prob = _score_binary_lifecycle_model_fast(
                        loaded.reversal_failure_model,
                        lifecycle_row,
                        life_entry,
                    )
                if loaded.reversal_opportunity_model is not None:
                    reversal_opportunity_prob = _score_binary_lifecycle_model_fast(
                        loaded.reversal_opportunity_model,
                        lifecycle_row,
                        life_entry,
                    )

                if reversal_context_active and loaded.has_reversal_models:
                    if float(reversal_failure_prob) < float(s.reversal_failure_min_prob):
                        reversal_blocking_reasons.append("reversal_failure_below_threshold")
                    if float(reversal_opportunity_prob) < float(s.reversal_opportunity_min_prob):
                        reversal_blocking_reasons.append("reversal_opportunity_below_threshold")
                reversal_blocking_reasons = list(dict.fromkeys(reversal_blocking_reasons))
                reversal_ready = (
                    reversal_context_active
                    and bool(signal["allowed"])
                    and len(reversal_blocking_reasons) == 0
                    and (
                        not loaded.has_reversal_models
                        or (
                            float(reversal_failure_prob) >= float(s.reversal_failure_min_prob)
                            and float(reversal_opportunity_prob) >= float(s.reversal_opportunity_min_prob)
                        )
                    )
                )

                if reversal_ready:
                    lifecycle_action = "exit"
                    lifecycle_reason = "reversal_models_exit"
                elif loaded.has_exit_model and str(exit_action_selected) in {"partial_tp", "exit"} and float(exit_action_score) >= float(s.lifecycle_model_action_min_prob):
                    if str(exit_action_selected) == "partial_tp":
                        lifecycle_action, close_lots = _partial_close_plan(
                            lots_open=float(pos_snapshot.lots),
                            fraction=float(s.partial_close_fraction),
                            settings=s,
                        )
                        if lifecycle_action in {"partial_tp", "exit"} and close_lots > 0.0:
                            lifecycle_reason = "exit_model_partial_tp" if lifecycle_action == "partial_tp" else "exit_model_reduce_to_flat"
                    else:
                        lifecycle_action = "exit"
                        lifecycle_reason = "exit_model_exit"
                elif not loaded.has_exit_model and float(signal["trade_prob"]) < float(s.min_trade_prob * 0.8):
                    lifecycle_action, close_lots = _partial_close_plan(
                        lots_open=float(pos_snapshot.lots),
                        fraction=float(s.partial_close_fraction),
                        settings=s,
                    )
                    if lifecycle_action in {"partial_tp", "exit"} and close_lots > 0.0:
                        lifecycle_reason = "exit_model_reduce" if lifecycle_action == "partial_tp" else "exit_model_reduce_to_flat"
                else:
                    lifecycle_reason = "position_open_hold"

                if lifecycle_action in {"partial_tp", "exit"}:
                    if live_pos is None:
                        continue
                    if live_pos.side == "long":
                        raw_exit = float(bid_arrays[pair][bar_idx])
                        exit_price = _apply_slippage(price=raw_exit, action="long_close", slippage_bps=float(args.slippage_bps))
                    else:
                        raw_exit = float(ask_arrays[pair][bar_idx])
                        exit_price = _apply_slippage(price=raw_exit, action="short_close", slippage_bps=float(args.slippage_bps))
                    lots_to_close = float(live_pos.lots) if lifecycle_action == "exit" else float(close_lots)
                    realized = _realized_pnl_usd(
                        pair=pair,
                        side=live_pos.side,
                        entry_price=float(live_pos.entry_price),
                        exit_price=float(exit_price),
                        lots=lots_to_close,
                        bar_idx=bar_idx,
                        mid_arrays=mid_arrays,
                    )
                    cash_balance += realized
                    live_pos.realized_pnl_usd += realized
                    if lifecycle_action == "partial_tp":
                        live_pos.lots = round(max(0.0, float(live_pos.lots) - lots_to_close), 8)
                        live_pos.partial_exit_events += 1
                        partial_exit_count += 1
                        if live_pos.lots <= 0.0:
                            lifecycle_action = "exit"
                    if lifecycle_action == "exit":
                        if lifecycle_reason == "reversal_models_exit":
                            reversal_exit_count += 1
                        closed_trades.append(
                            ClosedTrade(
                                pair=pair,
                                side=live_pos.side,
                                open_ts=str(live_pos.open_ts),
                                close_ts=str(ts_dt),
                                entry_price=float(live_pos.entry_price),
                                exit_price=float(exit_price),
                                lots=float(live_pos.entry_lots),
                                realized_pnl_usd=float(live_pos.realized_pnl_usd),
                                holding_bars=max(1, int((ts_dt - live_pos.open_ts).total_seconds() // holding_bar_secs)),
                                partial_exit_events=int(live_pos.partial_exit_events),
                                close_reason=str(lifecycle_reason),
                                entry_trade_prob=float(live_pos.entry_trade_prob),
                                exit_action_selected=str(exit_action_selected),
                                reversal_failure_prob=float(reversal_failure_prob),
                                reversal_opportunity_prob=float(reversal_opportunity_prob),
                            )
                        )
                        open_positions.pop(pair, None)
                elif lifecycle_action == "hold":
                    pass

            if pos_snapshot is None and ready:
                lots, _ = _entry_order_lots(state={"equity": current_equity}, settings=s, equity_seed=float(args.start_equity))
                if float(lots) >= float(s.min_order_lots):
                    if str(signal["side"]) == "BUY":
                        entry_price = _apply_slippage(
                            price=float(ask_arrays[pair][bar_idx]),
                            action="buy_open",
                            slippage_bps=float(args.slippage_bps),
                        )
                        side = "long"
                    else:
                        entry_price = _apply_slippage(
                            price=float(bid_arrays[pair][bar_idx]),
                            action="sell_open",
                            slippage_bps=float(args.slippage_bps),
                        )
                        side = "short"
                    open_positions[pair] = PositionState(
                        pair=pair,
                        side=side,
                        lots=float(lots),
                        entry_lots=float(lots),
                        entry_price=float(entry_price),
                        open_ts=ts_dt,
                        open_equity_usd=float(current_equity),
                        entry_trade_prob=float(signal["trade_prob"]),
                    )
                    entry_count += 1
            elif not ready:
                for reason in decision_reasons:
                    rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1

        equity_curve.append(
            {
                "ts": str(ts_dt),
                "balance_usd": float(cash_balance),
                "equity_usd": float(
                    _mark_equity(
                        cash_balance=cash_balance,
                        open_positions=open_positions,
                        bar_idx=bar_idx,
                        bid_arrays=bid_arrays,
                        ask_arrays=ask_arrays,
                        mid_arrays=mid_arrays,
                    )
                ),
                "open_positions": int(len(open_positions)),
            }
        )

    final_ts = timeline[-1]
    final_bar_idx = len(timeline) - 1
    for pair, pos in list(open_positions.items()):
        if pos.side == "long":
            exit_price = _apply_slippage(price=float(bid_arrays[pair][final_bar_idx]), action="long_close", slippage_bps=float(args.slippage_bps))
        else:
            exit_price = _apply_slippage(price=float(ask_arrays[pair][final_bar_idx]), action="short_close", slippage_bps=float(args.slippage_bps))
        realized = _realized_pnl_usd(
            pair=pair,
            side=pos.side,
            entry_price=float(pos.entry_price),
            exit_price=float(exit_price),
            lots=float(pos.lots),
            bar_idx=final_bar_idx,
            mid_arrays=mid_arrays,
        )
        cash_balance += realized
        pos.realized_pnl_usd += realized
        closed_trades.append(
            ClosedTrade(
                pair=pair,
                side=pos.side,
                open_ts=str(pos.open_ts),
                close_ts=str(final_ts),
                entry_price=float(pos.entry_price),
                exit_price=float(exit_price),
                lots=float(pos.entry_lots),
                realized_pnl_usd=float(pos.realized_pnl_usd),
                holding_bars=max(1, int((final_ts - pos.open_ts).total_seconds() // holding_bar_secs)),
                partial_exit_events=int(pos.partial_exit_events),
                close_reason="forced_final_close",
                entry_trade_prob=float(pos.entry_trade_prob),
                exit_action_selected="forced_final_close",
                reversal_failure_prob=0.0,
                reversal_opportunity_prob=0.0,
            )
        )
        open_positions.pop(pair, None)

    equity_df = pd.DataFrame(equity_curve)
    equity_df = pd.concat(
        [
            equity_df,
            pd.DataFrame(
                [
                    {
                        "ts": str(final_ts),
                        "balance_usd": float(cash_balance),
                        "equity_usd": float(cash_balance),
                        "open_positions": 0,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    trades_df = pd.DataFrame([asdict(t) for t in closed_trades], columns=TRADE_COLUMNS)
    if not trades_df.empty:
        trades_df = trades_df.sort_values(["close_ts", "pair"]).reset_index(drop=True)
    if equity_df.empty:
        raise RuntimeError("equity curve is empty")
    equity_df["equity_peak"] = equity_df["equity_usd"].cummax()
    equity_df["drawdown_pct"] = np.where(
        equity_df["equity_peak"] > 0.0,
        ((equity_df["equity_usd"] / equity_df["equity_peak"]) - 1.0) * 100.0,
        0.0,
    )

    gross_profit = float(trades_df.loc[trades_df["realized_pnl_usd"] > 0.0, "realized_pnl_usd"].sum()) if not trades_df.empty else 0.0
    gross_loss = float(trades_df.loc[trades_df["realized_pnl_usd"] < 0.0, "realized_pnl_usd"].sum()) if not trades_df.empty else 0.0
    wins = int((trades_df["realized_pnl_usd"] > 0.0).sum()) if not trades_df.empty else 0
    losses = int((trades_df["realized_pnl_usd"] < 0.0).sum()) if not trades_df.empty else 0
    flats = int((trades_df["realized_pnl_usd"] == 0.0).sum()) if not trades_df.empty else 0
    total_trades = int(len(trades_df))
    total_return_pct = ((float(cash_balance) / float(args.start_equity)) - 1.0) * 100.0 if float(args.start_equity) > 0.0 else 0.0

    per_pair_records: list[dict[str, Any]] = []
    for pair in pairs:
        pair_df = trades_df[trades_df["pair"] == pair].copy()
        gross_profit_pair = float(pair_df.loc[pair_df["realized_pnl_usd"] > 0.0, "realized_pnl_usd"].sum()) if not pair_df.empty else 0.0
        gross_loss_pair = float(pair_df.loc[pair_df["realized_pnl_usd"] < 0.0, "realized_pnl_usd"].sum()) if not pair_df.empty else 0.0
        per_pair_records.append(
            {
                "pair": pair,
                "trades": int(len(pair_df)),
                "wins": int((pair_df["realized_pnl_usd"] > 0.0).sum()) if not pair_df.empty else 0,
                "losses": int((pair_df["realized_pnl_usd"] < 0.0).sum()) if not pair_df.empty else 0,
                "win_rate": float((pair_df["realized_pnl_usd"] > 0.0).mean()) if not pair_df.empty else 0.0,
                "net_pnl_usd": float(pair_df["realized_pnl_usd"].sum()) if not pair_df.empty else 0.0,
                "profit_factor": float(gross_profit_pair / abs(gross_loss_pair)) if gross_loss_pair < 0.0 else (float("inf") if gross_profit_pair > 0.0 else 0.0),
                "avg_trade_pnl_usd": float(pair_df["realized_pnl_usd"].mean()) if not pair_df.empty else 0.0,
                "median_trade_pnl_usd": float(pair_df["realized_pnl_usd"].median()) if not pair_df.empty else 0.0,
                "avg_holding_bars": float(pair_df["holding_bars"].mean()) if not pair_df.empty else 0.0,
                "partial_exit_events": int(pair_df["partial_exit_events"].sum()) if not pair_df.empty else 0,
                "long_trades": int((pair_df["side"] == "long").sum()) if not pair_df.empty else 0,
                "short_trades": int((pair_df["side"] == "short").sum()) if not pair_df.empty else 0,
            }
        )
    per_pair_df = pd.DataFrame(per_pair_records).sort_values(["net_pnl_usd", "profit_factor"], ascending=[False, False]).reset_index(drop=True)

    side_breakdown = (
        trades_df.groupby("side")["realized_pnl_usd"].agg(["count", "sum", "mean"]).reset_index().rename(
            columns={"count": "trades", "sum": "net_pnl_usd", "mean": "avg_trade_pnl_usd"}
        )
        if not trades_df.empty
        else pd.DataFrame(columns=["side", "trades", "net_pnl_usd", "avg_trade_pnl_usd"])
    )

    aggregate = {
        "pairs": pairs,
        "start_ts": str(start_ts),
        "end_ts": str(end_ts),
        "start_equity_usd": float(args.start_equity),
        "end_equity_usd": float(cash_balance),
        "total_return_pct": float(total_return_pct),
        "trades": total_trades,
        "entries": int(entry_count),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": float((wins / total_trades) if total_trades > 0 else 0.0),
        "gross_profit_usd": float(gross_profit),
        "gross_loss_usd": float(gross_loss),
        "net_pnl_usd": float(cash_balance - float(args.start_equity)),
        "profit_factor": float(gross_profit / abs(gross_loss)) if gross_loss < 0.0 else (float("inf") if gross_profit > 0.0 else 0.0),
        "avg_trade_pnl_usd": float(trades_df["realized_pnl_usd"].mean()) if not trades_df.empty else 0.0,
        "median_trade_pnl_usd": float(trades_df["realized_pnl_usd"].median()) if not trades_df.empty else 0.0,
        "avg_holding_bars": float(trades_df["holding_bars"].mean()) if not trades_df.empty else 0.0,
        "max_drawdown_pct": float(equity_df["drawdown_pct"].min()),
        "partial_exit_events": int(partial_exit_count),
        "reversal_exit_events": int(reversal_exit_count),
        "open_positions_forced_closed": int((trades_df["close_reason"] == "forced_final_close").sum()) if not trades_df.empty else 0,
        "slippage_bps_per_execution": float(args.slippage_bps),
        "rejection_counts": {k: int(v) for k, v in sorted(rejection_counts.items(), key=lambda item: (-item[1], item[0]))},
    }

    trades_path = out_dir / "trades.csv"
    equity_path = out_dir / "equity_curve.csv"
    aggregate_path = out_dir / "aggregate.json"
    per_pair_path = out_dir / "per_pair.json"
    side_path = out_dir / "by_side.json"
    trades_df.to_csv(trades_path, index=False)
    equity_df.to_csv(equity_path, index=False)
    aggregate_path.write_text(json.dumps(aggregate, indent=2, sort_keys=True), encoding="utf-8")
    per_pair_path.write_text(json.dumps(per_pair_df.to_dict(orient="records"), indent=2), encoding="utf-8")
    side_path.write_text(json.dumps(side_breakdown.to_dict(orient="records"), indent=2), encoding="utf-8")

    return {
        "aggregate": aggregate,
        "per_pair": per_pair_df,
        "side_breakdown": side_breakdown,
        "trades_path": trades_path,
        "equity_path": equity_path,
        "aggregate_path": aggregate_path,
        "per_pair_path": per_pair_path,
        "side_path": side_path,
    }


def build_parser() -> argparse.ArgumentParser:
    s = get_settings()
    default_out = Path(s.project_root) / "artifacts" / "reports" / "backtests" / f"lifecycle_equity_{pd.Timestamp.utcnow().strftime('%Y%m%d_%H%M%S')}"
    parser = argparse.ArgumentParser(description="Run a lifecycle/equity FXStack backtest from the active manifest.")
    parser.add_argument("--pairs", default=",".join(s.pairs))
    parser.add_argument("--feature-root", default=str(Path(s.project_root) / "data" / "features"))
    parser.add_argument("--start-equity", type=float, default=10000.0)
    parser.add_argument("--slippage-bps", type=float, default=0.25)
    parser.add_argument("--start-ts", default="")
    parser.add_argument("--end-ts", default="")
    parser.add_argument("--lifecycle-cache-pairs", type=int, default=6)
    parser.add_argument("--out-dir", default=str(default_out))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = run_backtest(args)
    print(json.dumps(result["aggregate"], indent=2, sort_keys=True))
    print(f"trades_csv={result['trades_path']}")
    print(f"equity_curve_csv={result['equity_path']}")
    print(f"per_pair_json={result['per_pair_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
