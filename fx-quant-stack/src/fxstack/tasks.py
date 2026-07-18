from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.data.ingest import ingest_dukascopy_csv, load_silver_bars
from fxstack.features.build import build_features, leakage_guard
from fxstack.features.multi_tf_contract import build_multi_tf_rows, write_data_contract_profile
from fxstack.features.session_contract import feature_contract_mismatches
from fxstack.io.parquet_store import ParquetStore
from fxstack.feast.offline_builder import build_historical_feature_frame
from fxstack.labels.exit_labels import ExitLabelConfig, build_exit_labels
from fxstack.labels.meta_label import build_meta_labels
from fxstack.labels.reversal_labels import ReversalLabelConfig, build_reversal_labels
from fxstack.labels.triple_barrier import TripleBarrierConfig, triple_barrier_labels
from fxstack.models.exit_policy_xgb import ExitPolicyXGB
from fxstack.models.belief_horizon_xgb import BeliefHorizonXGB
from fxstack.models.belief_scenario_xgb import BeliefScenarioXGB
from fxstack.models.intraday_tcn import IntradayTCN
from fxstack.models.artifact_contract import artifact_lock, stamp_artifact_payload_digest
from fxstack.models.patchtst import IntradayPatchTST, SwingPatchTST, patchtst_dependencies_available, patchtst_dependency_error_detail
from fxstack.models.intraday_xgb import IntradayXGB
from fxstack.models.meta_filter import MetaFilterXGB
from fxstack.models.regime_hmm import RegimeHMM
from fxstack.models.reversal_failure_xgb import ReversalFailureXGB
from fxstack.models.reversal_opportunity_xgb import ReversalOpportunityXGB
from fxstack.models.swing_transformer import SwingTransformer
from fxstack.models.swing_xgb import SwingXGB
from fxstack.settings import get_settings
from fxstack.training.belief import export_directional_belief_dataset, train_directional_belief
from fxstack.training.phase4_types import ChallengerSpec
from fxstack.training.lifecycle_validation import validate_candidate
from fxstack.training.sequence_dataset import build_sequence_dataset_manifest


def _provider() -> str:
    return get_settings().normalized_data_provider


def _resolve_csv_path(*, pair: str, granularity: str, csv_path: str, source_root: str, file_pattern: str) -> Path:
    s = get_settings()
    if str(csv_path or "").strip():
        return Path(str(csv_path)).expanduser()

    root_txt = str(source_root or s.dukascopy_source_root).strip()
    if not root_txt:
        raise RuntimeError("dukascopy source root is not configured")
    root = Path(root_txt).expanduser()
    pattern = str(file_pattern or s.dukascopy_file_pattern).strip()
    if not pattern:
        pattern = "{pair}_{granularity}.csv"

    try:
        file_name = pattern.format(
            pair=str(pair).upper(),
            granularity=str(granularity).upper(),
            timeframe=str(granularity).upper(),
        )
    except Exception as exc:
        raise RuntimeError(f"invalid dukascopy file pattern '{pattern}': {exc}") from exc
    return root / file_name


def ingest_task(
    *,
    pair: str,
    granularity: str,
    store_root: str,
    csv_path: str = "",
    source_root: str = "",
    file_pattern: str = "",
) -> dict:
    p = _resolve_csv_path(
        pair=pair,
        granularity=granularity,
        csv_path=csv_path,
        source_root=source_root,
        file_pattern=file_pattern,
    )
    if not p.exists():
        raise RuntimeError(f"csv source not found: {p}")
    res = ingest_dukascopy_csv(
        store_root=Path(store_root),
        pair=pair,
        timeframe=granularity,
        csv_path=p,
        provider=_provider(),
    )
    return {
        "pair": res.pair,
        "timeframe": res.timeframe,
        "rows": res.rows,
        "path": res.path,
        "csv_path": str(p),
    }


def build_features_task(*, pair: str, timeframe: str, input_root: str, output_root: str) -> dict:
    bars = load_silver_bars(store_root=Path(input_root), pair=pair, timeframe=timeframe, provider=_provider())
    feats = build_features(bars)
    leakage_guard(feats)
    out = ParquetStore(Path(output_root)).write_partitioned(feats, provider=_provider(), pair=pair, timeframe=timeframe)
    return {"rows": len(feats), "path": str(out)}


def build_labels_task(*, pair: str, timeframe: str, feature_root: str, label_root: str, horizon_bars: int, tp_mult: float, sl_mult: float) -> dict:
    feats = ParquetStore(Path(feature_root)).read_pair_timeframe(provider=_provider(), pair=pair, timeframe=timeframe)
    labels = triple_barrier_labels(
        feats,
        TripleBarrierConfig(horizon_bars=horizon_bars, tp_atr_mult=tp_mult, sl_atr_mult=sl_mult),
    )
    out = ParquetStore(Path(label_root)).write_partitioned(labels, provider=_provider(), pair=pair, timeframe=timeframe)
    return {"rows": len(labels), "path": str(out)}


def build_fx_lifecycle_features_task(
    *,
    pair: str,
    input_root: str,
    output_root: str,
    anchor_timeframe: str = "M5",
    context_timeframes: list[str] | None = None,
    report_root: str | None = None,
) -> dict:
    s = get_settings()
    feats, report = build_multi_tf_rows(
        pair=pair,
        raw_store_root=Path(input_root),
        provider=_provider(),
        anchor_timeframe=str(anchor_timeframe).upper(),
        context_timeframes=context_timeframes,
        all_pairs=list(s.pairs),
    )
    if feats.empty:
        raise RuntimeError(f"no lifecycle feature rows for {pair}")
    leakage_guard(feats)
    out = ParquetStore(Path(output_root)).replace_partitioned(
        feats,
        provider=_provider(),
        pair=pair,
        timeframe=str(anchor_timeframe).upper(),
    )
    report_dir = Path(report_root) if report_root else (get_settings().project_root / "artifacts" / str(pair).lower() / "reports")
    write_data_contract_profile(report_dir / "data_contract_profile.json", report)
    return {"rows": len(feats), "path": str(out), "report_path": str(report_dir / "data_contract_profile.json")}


def _label_store_root(label_root: str, dataset_name: str) -> Path:
    return Path(label_root) / str(dataset_name)


def _frame_prefix(*, timeframe: str, anchor_timeframe: str) -> str:
    tf = str(timeframe).upper()
    anchor = str(anchor_timeframe).upper()
    return "" if tf == anchor else f"{tf.lower()}_"


def _project_frame_features(
    feats: pd.DataFrame,
    *,
    timeframe: str,
    anchor_timeframe: str,
    feature_columns: list[str] | None = None,
) -> pd.DataFrame:
    alias_map = {
        "trend_slope_20": "trend_strength_20",
    }
    prefix = _frame_prefix(timeframe=timeframe, anchor_timeframe=anchor_timeframe)
    if feature_columns:
        out: dict[str, Any] = {}
        for col in feature_columns:
            src = f"{prefix}{col}" if prefix else str(col)
            if src not in feats.columns:
                alias = alias_map.get(str(col), "")
                alias_src = f"{prefix}{alias}" if prefix and alias else alias
                if alias_src and alias_src in feats.columns:
                    src = alias_src
                else:
                    raise RuntimeError(f"missing contextual feature column '{src}' for timeframe={timeframe}")
            out[str(col)] = feats[src]
        return pd.DataFrame(out, index=feats.index)

    if not prefix:
        cols = [c for c in feats.columns if pd.api.types.is_numeric_dtype(feats[c])]
        return feats[cols].copy()

    prefixed = [c for c in feats.columns if str(c).startswith(prefix) and pd.api.types.is_numeric_dtype(feats[c])]
    projected = {str(c)[len(prefix) :]: feats[c] for c in prefixed}
    return pd.DataFrame(projected, index=feats.index)


def _align_frame_to_anchor(anchor_feats: pd.DataFrame, frame_feats: pd.DataFrame) -> pd.DataFrame:
    if frame_feats.empty:
        return pd.DataFrame(index=anchor_feats.index)

    left_key = "anchor_close_ts" if "anchor_close_ts" in anchor_feats.columns else ("close_ts" if "close_ts" in anchor_feats.columns else "ts")
    right_key = "close_ts" if "close_ts" in frame_feats.columns else "ts"

    left = anchor_feats[[left_key]].copy()
    left["__idx__"] = range(len(left))
    left["__join_ts__"] = pd.to_datetime(left[left_key], utc=True, errors="coerce")
    left = left.dropna(subset=["__join_ts__"]).sort_values("__join_ts__")

    right = frame_feats.copy()
    right["__join_ts__"] = pd.to_datetime(right[right_key], utc=True, errors="coerce")
    right = right.dropna(subset=["__join_ts__"]).sort_values("__join_ts__")

    merged = pd.merge_asof(
        left[["__idx__", "__join_ts__"]],
        right,
        on="__join_ts__",
        direction="backward",
        allow_exact_matches=True,
    )
    merged = merged.sort_values("__idx__").set_index("__idx__")
    merged = merged.drop(columns=["__join_ts__"], errors="ignore")
    return merged.reindex(range(len(anchor_feats)))


def _annotate_candidate_scores(
    feats: pd.DataFrame,
    *,
    anchor_timeframe: str,
    regime_timeframe: str,
    swing_timeframe: str,
    intraday_timeframe: str,
    regime_model_path: str,
    swing_model_path: str,
    intraday_model_path: str,
    regime_frame_feats: pd.DataFrame | None = None,
    swing_frame_feats: pd.DataFrame | None = None,
    intraday_frame_feats: pd.DataFrame | None = None,
) -> pd.DataFrame:
    regime_model = RegimeHMM.load(Path(str(regime_model_path)))
    swing_model = SwingXGB.load(Path(str(swing_model_path)))
    intraday_model = IntradayXGB.load(Path(str(intraday_model_path)))

    regime_cols = list(getattr(regime_model, "feature_columns", []) or ["ret_1", "ret_5", "vol_20", "vol_60", "trend_slope_20"])
    swing_cols = list(getattr(swing_model, "feature_columns", []) or [])
    intraday_cols = list(getattr(intraday_model, "feature_columns", []) or [])

    regime_source = (
        _align_frame_to_anchor(feats, regime_frame_feats)
        if regime_frame_feats is not None and not regime_frame_feats.empty
        else feats
    )
    swing_source = (
        _align_frame_to_anchor(feats, swing_frame_feats)
        if swing_frame_feats is not None and not swing_frame_feats.empty
        else feats
    )
    intraday_source = (
        _align_frame_to_anchor(feats, intraday_frame_feats)
        if intraday_frame_feats is not None and not intraday_frame_feats.empty and str(intraday_timeframe).upper() != str(anchor_timeframe).upper()
        else feats
    )

    regime_x = _project_frame_features(
        regime_source,
        timeframe=str(regime_timeframe).upper(),
        anchor_timeframe=str(regime_timeframe).upper() if regime_source is not feats else str(anchor_timeframe).upper(),
        feature_columns=regime_cols,
    )
    swing_x = _project_frame_features(
        swing_source,
        timeframe=str(swing_timeframe).upper(),
        anchor_timeframe=str(swing_timeframe).upper() if swing_source is not feats else str(anchor_timeframe).upper(),
        feature_columns=swing_cols or None,
    )
    intraday_x = _project_frame_features(
        intraday_source,
        timeframe=str(intraday_timeframe).upper(),
        anchor_timeframe=str(intraday_timeframe).upper() if intraday_source is not feats else str(anchor_timeframe).upper(),
        feature_columns=intraday_cols or None,
    )

    out = feats.copy()
    out["regime_prob"] = regime_model.predict_proba(regime_x).max(axis=1).astype(float).to_numpy()
    out["swing_prob"] = swing_model.predict_proba(swing_x)["p1"].astype(float).to_numpy()
    out["entry_prob"] = intraday_model.predict_proba(intraday_x)["p1"].astype(float).to_numpy()
    out["side"] = out["swing_prob"].apply(lambda v: "long" if float(v) >= 0.5 else "short")
    out["candidate_side"] = out["side"]
    return out


def _infer_candidate_side(feats: pd.DataFrame) -> pd.Series:
    if "candidate_side" in feats.columns:
        side = feats["candidate_side"].astype(str).str.lower()
        return side.map({"long": 1.0, "buy": 1.0, "short": -1.0, "sell": -1.0}).fillna(1.0)
    if "side" in feats.columns:
        side = feats["side"].astype(str).str.lower()
        return side.map({"long": 1.0, "buy": 1.0, "short": -1.0, "sell": -1.0}).fillna(1.0)
    if "swing_prob" in feats.columns:
        return feats["swing_prob"].astype(float).apply(lambda v: 1.0 if v >= 0.5 else -1.0)
    if "ret_5" in feats.columns:
        return feats["ret_5"].astype(float).apply(lambda v: 1.0 if v >= 0.0 else -1.0)
    return feats.get("ret_1", pd.Series(0.0, index=feats.index)).astype(float).apply(lambda v: 1.0 if v >= 0.0 else -1.0)


def build_meta_labels_task(
    *,
    pair: str,
    timeframe: str,
    feature_root: str,
    label_root: str,
    cost_stress_levels: tuple[float, ...] = (1.0, 1.25, 1.5),
    horizon_bars: int = 12,
    anchor_timeframe: str = "M5",
    swing_timeframe: str = "D",
    regime_timeframe: str = "H4",
    regime_model_path: str = "",
    swing_model_path: str = "",
    intraday_model_path: str = "",
    allow_heuristic_labels: bool | None = None,
) -> dict:
    s = get_settings()
    allow_heuristic = bool(s.allow_heuristic_meta_labels) if allow_heuristic_labels is None else bool(allow_heuristic_labels)
    model_paths = {
        "regime_model_path": str(regime_model_path).strip(),
        "swing_model_path": str(swing_model_path).strip(),
        "intraday_model_path": str(intraday_model_path).strip(),
    }
    supplied_paths = [name for name, value in model_paths.items() if value]
    if supplied_paths and len(supplied_paths) != len(model_paths):
        missing = [name for name, value in model_paths.items() if not value]
        raise RuntimeError(f"meta label build requires all model paths together; missing: {','.join(missing)}")
    if not supplied_paths and not allow_heuristic:
        raise RuntimeError(
            "meta label build requires trained regime/swing/intraday model paths unless FXSTACK_ALLOW_HEURISTIC_META_LABELS=1"
        )
    feats, retrieval_meta = build_historical_feature_frame(
        feature_root=feature_root,
        pair=pair,
        timeframe=timeframe,
        feature_service_name=f"fx_{pair.lower()}_meta_filter_{str(timeframe).lower()}",
        feature_view_names=["anchor_m5", "context_m15", "context_h1", "context_h4", "context_d", "cross_pair_context"],
    )
    if feats.empty:
        raise RuntimeError("features are empty")
    df = feats.copy()
    df.attrs["feature_retrieval"] = dict(retrieval_meta or {})
    if supplied_paths:
        regime_frame_feats, _ = build_historical_feature_frame(
            feature_root=feature_root,
            pair=pair,
            timeframe=str(regime_timeframe).upper(),
            feature_service_name=f"fx_{pair.lower()}_regime_hmm_{str(regime_timeframe).lower()}",
            feature_view_names=["anchor_h4"],
        )
        swing_frame_feats, _ = build_historical_feature_frame(
            feature_root=feature_root,
            pair=pair,
            timeframe=str(swing_timeframe).upper(),
            feature_service_name=f"fx_{pair.lower()}_swing_xgb_{str(swing_timeframe).lower()}",
            feature_view_names=["anchor_d"],
        )
        df = _annotate_candidate_scores(
            df,
            anchor_timeframe=str(anchor_timeframe).upper(),
            regime_timeframe=str(regime_timeframe).upper(),
            swing_timeframe=str(swing_timeframe).upper(),
            intraday_timeframe=str(timeframe).upper(),
            regime_model_path=str(regime_model_path),
            swing_model_path=str(swing_model_path),
            intraday_model_path=str(intraday_model_path),
            regime_frame_feats=regime_frame_feats,
            swing_frame_feats=swing_frame_feats,
        )
    spread_bps = df["spread_bps"].astype(float) if "spread_bps" in df.columns else pd.Series(0.0, index=df.index)
    if "mid_close" not in df.columns:
        raise RuntimeError("missing mid_close for meta label build")
    direction = _infer_candidate_side(df).astype(float)
    future_mid = df["mid_close"].astype(float).shift(-int(horizon_bars))
    forward_ret = ((future_mid / df["mid_close"].astype(float)) - 1.0).fillna(0.0)
    df["realized_edge_bps"] = (forward_ret * direction * 10000.0) - spread_bps
    df = df.iloc[:-int(horizon_bars)].copy() if int(horizon_bars) > 0 and len(df) > int(horizon_bars) else df.copy()
    labels = build_meta_labels(df, pnl_col="realized_edge_bps", cost_stress_levels=tuple(cost_stress_levels))
    out = ParquetStore(_label_store_root(label_root, "meta")).write_partitioned(
        labels,
        provider=_provider(),
        pair=pair,
        timeframe=timeframe,
    )
    return {
        "rows": len(labels),
        "path": str(out),
        **dict(df.attrs.get("feature_retrieval") or {}),
    }


def build_exit_labels_task(
    *,
    pair: str,
    timeframe: str,
    feature_root: str,
    label_root: str,
    method: str = "trade_outcome",
    horizon_bars: int = 24,
) -> dict:
    feats = ParquetStore(Path(feature_root)).read_pair_timeframe(provider=_provider(), pair=pair, timeframe=timeframe)
    if feats.empty:
        raise RuntimeError("features are empty")
    labels = build_exit_labels(feats, ExitLabelConfig(horizon_bars=int(horizon_bars)), method=str(method))
    out = ParquetStore(_label_store_root(label_root, "exit")).write_partitioned(
        labels,
        provider=_provider(),
        pair=pair,
        timeframe=timeframe,
    )
    return {"rows": len(labels), "path": str(out), "method": str(method)}


def build_reversal_labels_task(
    *,
    pair: str,
    timeframe: str,
    feature_root: str,
    label_root: str,
    horizon_bars: int = 24,
) -> dict:
    feats = ParquetStore(Path(feature_root)).read_pair_timeframe(provider=_provider(), pair=pair, timeframe=timeframe)
    if feats.empty:
        raise RuntimeError("features are empty")
    labels = build_reversal_labels(feats, ReversalLabelConfig(horizon_bars=int(horizon_bars)))
    out = ParquetStore(_label_store_root(label_root, "reversal")).write_partitioned(
        labels,
        provider=_provider(),
        pair=pair,
        timeframe=timeframe,
    )
    return {"rows": len(labels), "path": str(out)}


def _report_dir_from_artifact(out: str) -> Path:
    artifact_path = Path(out)
    return artifact_path / "reports"


def _report_path_from_artifact(out: str, name: str = "training_report.json") -> Path:
    return _report_dir_from_artifact(out) / name


def _artifact_meta_path(path: Path) -> Path:
    return Path(path) / "meta.json"


def _load_artifact_meta(path: Path) -> dict[str, Any]:
    meta_path = _artifact_meta_path(path)
    if not meta_path.exists():
        return {}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _merge_artifact_meta(path: Path, extra: dict[str, Any]) -> None:
    meta_path = _artifact_meta_path(path)
    if not meta_path.exists():
        return
    meta = _load_artifact_meta(path)
    meta.update(dict(extra or {}))
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")


def _annotate_validation_result(*, artifact_path: str, report: dict[str, Any]) -> str:
    report_path = _report_path_from_artifact(artifact_path)
    promotion = dict(report.get("promotion_decision") or {})
    status = str(promotion.get("status") or "unknown")
    with artifact_lock(artifact_path):
        _merge_artifact_meta(
            Path(artifact_path),
            {
                "report_path": str(report_path),
                "promotion_status": status,
                "promotion_decision": promotion,
            },
        )
        stamp_artifact_payload_digest(artifact_path)
    return status


def _collapse_exit_action_labels(labels: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    if labels.empty:
        return labels, {"action_map": {}, "class_balance_before": {}, "class_balance_after": {}}
    out = labels.copy()
    if "exit_action" not in out.columns:
        return out, {"action_map": {}, "class_balance_before": {}, "class_balance_after": {}}
    action_map = {
        "exit": "exit",
        "partial_tp": "partial_tp",
        "reduce": "partial_tp",
        "tighten_stop": "hold",
        "hold": "hold",
    }
    before = out["exit_action"].astype(str).value_counts(normalize=True).round(6).to_dict()
    out["exit_action_collapsed"] = out["exit_action"].astype(str).map(action_map).fillna("hold")
    collapsed_actions = ["hold", "partial_tp", "exit"]
    collapsed_map = {name: idx for idx, name in enumerate(collapsed_actions)}
    out["exit_action_id_collapsed"] = out["exit_action_collapsed"].map(collapsed_map).astype(int)
    after = out["exit_action_collapsed"].astype(str).value_counts(normalize=True).round(6).to_dict()
    return out, {
        "action_map": action_map,
        "class_balance_before": before,
        "class_balance_after": after,
    }


def _frame_summary(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {"rows": 0, "start_ts": "", "end_ts": ""}
    if "ts" not in df.columns:
        return {"rows": int(len(df)), "start_ts": "", "end_ts": ""}
    ts = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    ts = ts[ts.notna()]
    if ts.empty:
        return {"rows": int(len(df)), "start_ts": "", "end_ts": ""}
    return {
        "rows": int(len(df)),
        "start_ts": str(ts.min().isoformat()),
        "end_ts": str(ts.max().isoformat()),
    }


def _coerce_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce", utc=True)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return ts


def _rows_after_data_window(df: pd.DataFrame, *, data_window_end: Any) -> int:
    if df.empty or "ts" not in df.columns:
        return 0
    end_ts = _coerce_timestamp(data_window_end)
    if end_ts is None:
        return int(len(df))
    cur = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    return int((cur > end_ts).sum())


def _force_weekly_retrain_today() -> bool:
    s = get_settings()
    day = str(s.force_weekly_retrain_day or "").strip().lower()
    if not day:
        return False
    week = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if day not in week:
        return False
    return week[time.gmtime().tm_wday] == day


def artifact_retrain_decision(
    *,
    dataset: pd.DataFrame,
    artifact_path: Path,
    min_new_rows: int,
    max_age_hours: float | None = None,
    weekly_only: bool = False,
) -> dict[str, Any]:
    force_weekly = _force_weekly_retrain_today()
    meta = _load_artifact_meta(artifact_path)
    exists = bool(meta)
    contract_mismatches = feature_contract_mismatches(meta) if exists else {}
    age_hours = _artifact_age_hours(artifact_path) if exists else None
    new_rows = _rows_after_data_window(dataset, data_window_end=meta.get("data_window_end")) if exists else int(len(dataset))

    should_retrain = not exists
    reason = "artifact_missing" if not exists else "up_to_date"
    if force_weekly:
        should_retrain = True
        reason = "force_weekly"
    elif contract_mismatches:
        should_retrain = True
        reason = "feature_contract_mismatch"
    elif weekly_only:
        should_retrain = False if exists else True
        reason = "weekly_only_skip" if exists else "artifact_missing"
    elif new_rows >= max(0, int(min_new_rows)):
        should_retrain = True
        reason = "new_rows_threshold"
    elif max_age_hours is not None and age_hours is not None and float(age_hours) >= float(max_age_hours):
        should_retrain = True
        reason = "max_age_exceeded"

    return {
        "exists": exists,
        "should_retrain": bool(should_retrain),
        "reason": reason,
        "new_rows": int(new_rows),
        "age_hours": None if age_hours is None else float(age_hours),
        "force_weekly": bool(force_weekly),
        "feature_contract_mismatches": {
            key: {"expected": expected, "actual": actual}
            for key, (expected, actual) in sorted(contract_mismatches.items())
        },
    }


def _annotate_supervised_artifact(
    *,
    out: str,
    pair: str,
    timeframe: str,
    rows: int,
    feature_columns: list[str],
    dataset_summary: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "pair": str(pair).upper(),
        "timeframe": str(timeframe).upper(),
        "trained_at": float(time.time()),
        "train_rows": int(rows),
        "data_window_start": str(dataset_summary.get("start_ts") or ""),
        "data_window_end": str(dataset_summary.get("end_ts") or ""),
        "training_window_summary": dict(dataset_summary),
        "feature_columns": list(feature_columns),
    }
    payload.update(dict(extra or {}))
    with artifact_lock(out):
        _merge_artifact_meta(Path(out), payload)
        stamp_artifact_payload_digest(out)


def _with_mlops_fields(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload or {})
    out.setdefault("mlflow_run_id", "")
    out.setdefault("model_name", "")
    out.setdefault("model_version", "")
    out.setdefault("model_uri", "")
    out.setdefault("bundle_run_id", "")
    out.setdefault("dataset_fingerprint", "")
    out.setdefault("feature_service_name", "")
    out.setdefault("feature_service_version", "")
    out.setdefault("feature_contract_hash", "")
    out.setdefault("feature_view_names", [])
    out.setdefault("feature_retrieval", "")
    out.setdefault("source", "")
    out.setdefault("fallback_reason", "")
    out.setdefault("point_in_time_key", "")
    out.setdefault("provider", "")
    out.setdefault("repo_root", "")
    out.setdefault("all_pairs", [])
    out.setdefault("context_timeframes", [])
    out.setdefault("sequence_dataset_manifest", "")
    out.setdefault("portfolio_report", "")
    out.setdefault("challenger_head_to_head", "")
    out.setdefault("portfolio_disagreement", "")
    return out


def _load_lifecycle_dataset(*, root: Path, pair: str, timeframe: str) -> object:
    return ParquetStore(root).read_pair_timeframe(provider=_provider(), pair=pair, timeframe=timeframe)


def _train_xy(
    *,
    pair: str,
    timeframe: str,
    feature_root: str,
    label_root: str,
    feature_service_name: str = "",
    feature_view_names: list[str] | None = None,
):
    feats, retrieval_meta = build_historical_feature_frame(
        feature_root=feature_root,
        pair=pair,
        timeframe=timeframe,
        feature_service_name=feature_service_name,
        feature_view_names=list(feature_view_names or []),
    )
    labels = ParquetStore(Path(label_root)).read_pair_timeframe(provider=_provider(), pair=pair, timeframe=timeframe)
    if feats.empty or labels.empty:
        raise RuntimeError("features or labels are empty")
    feats = feats.sort_values("ts").reset_index(drop=True)
    labels = labels.sort_values("ts").reset_index(drop=True)
    if feats["ts"].duplicated().any():
        raise RuntimeError("feature frame contains duplicated timestamps")
    if labels["ts"].duplicated().any():
        raise RuntimeError("label frame contains duplicated timestamps")
    df = feats.merge(labels[["ts", "label"]], on="ts", how="inner")
    df = df[df["label"].isin([-1, 1])].copy()
    if df.empty:
        raise RuntimeError("no train rows after joining features/labels")
    df["y"] = (df["label"] > 0).astype(int)
    drop = {"pair", "timeframe", "date", "label", "y", "t1_index", "ts"}
    X = df[[c for c in df.columns if c not in drop and pd.api.types.is_numeric_dtype(df[c])]]
    y = df["y"]
    X.attrs["feature_retrieval"] = dict(retrieval_meta or {})
    df.attrs["feature_retrieval"] = dict(retrieval_meta or {})
    return X, y, df


def _prepare_lifecycle_xy(df, *, target_col: str, drop_extra: set[str] | None = None):
    if df.empty:
        raise RuntimeError("lifecycle dataset is empty")
    x = df.copy().sort_values("ts").reset_index(drop=True)
    if str(target_col) not in x.columns:
        raise RuntimeError(f"missing lifecycle target column: {target_col}")
    y = x[target_col]
    drop = {
        "pair",
        "timeframe",
        "date",
        "ts",
        "close_ts",
        "anchor_close_ts",
        "session_tag",
        "regime_bucket",
        "scenario_bucket",
        str(target_col),
    }
    drop.update(set(drop_extra or set()))
    X = x[[c for c in x.columns if c not in drop and pd.api.types.is_numeric_dtype(x[c])]].copy()
    meta = x[[c for c in x.columns if c not in X.columns]].copy()
    weights = x["sample_weight"].astype(float) if "sample_weight" in x.columns else None
    return X, y, meta, weights


def _seeded_meta_factory(seed: int):
    return lambda: MetaFilterXGB(params={"random_state": int(seed)})


def _ensure_patchtst_stack() -> None:
    if patchtst_dependencies_available():
        return
    raise RuntimeError(
        "PatchTST commands require the research stack in the selected interpreter. "
        f"Details: {patchtst_dependency_error_detail()}"
    )


def _patchtst_label_config(*, timeframe: str) -> dict[str, Any]:
    return {
        "task": "binary",
        "timeframe": str(timeframe).upper(),
        "label_domain": "triple_barrier_binary",
    }


def _swing_deep_feature_views() -> list[str]:
    return ["anchor_d", "cross_pair_context"]


def _artifact_age_hours(path: Path) -> float | None:
    meta = path / "meta.json"
    if not meta.exists():
        return None
    try:
        payload = json.loads(meta.read_text(encoding="utf-8"))
        created = float(payload.get("created_at", 0.0) or 0.0)
        if created <= 0:
            created = float(meta.stat().st_mtime)
    except Exception:
        created = float(meta.stat().st_mtime)
    return max(0.0, (time.time() - created) / 3600.0)


def _is_stale(path: Path, max_age_hours: float) -> tuple[bool, float | None]:
    age_hours = _artifact_age_hours(Path(path))
    if age_hours is None:
        return True, None
    return bool(age_hours > float(max_age_hours)), float(age_hours)


def train_regime_task(*, pair: str, timeframe: str, feature_root: str, out: str) -> dict:
    feats, retrieval_meta = build_historical_feature_frame(
        feature_root=feature_root,
        pair=pair,
        timeframe=timeframe,
        feature_service_name=f"fx_{pair.lower()}_regime_hmm_{str(timeframe).lower()}",
        feature_view_names=["anchor_h4"],
    )
    cols = [c for c in ["ret_1", "ret_5", "vol_20", "vol_60", "trend_slope_20"] if c in feats.columns]
    model = RegimeHMM()
    model.fit(feats[cols])
    model.save(Path(out))
    _annotate_supervised_artifact(
        out=out,
        pair=pair,
        timeframe=timeframe,
        rows=len(feats),
        feature_columns=cols,
        dataset_summary=_frame_summary(feats),
        extra={"model_family": "regime", "feature_retrieval": dict(retrieval_meta or {})},
    )
    return _with_mlops_fields({"model": "regime_hmm", "rows": len(feats), "path": out, **dict(retrieval_meta or {})})


def train_swing_task(*, pair: str, timeframe: str, feature_root: str, label_root: str, out: str) -> dict:
    X, y, df = _train_xy(
        pair=pair,
        timeframe=timeframe,
        feature_root=feature_root,
        label_root=label_root,
        feature_service_name=f"fx_{pair.lower()}_swing_xgb_{str(timeframe).lower()}",
        feature_view_names=["anchor_d"],
    )
    model = SwingXGB()
    model.fit(X, y)
    model.save(Path(out))
    _annotate_supervised_artifact(
        out=out,
        pair=pair,
        timeframe=timeframe,
        rows=len(X),
        feature_columns=list(X.columns),
        dataset_summary=_frame_summary(df),
        extra={"model_family": "swing", "feature_retrieval": dict(X.attrs.get("feature_retrieval") or {})},
    )
    return _with_mlops_fields({"model": "swing_xgb", "rows": len(X), "path": out, **dict(X.attrs.get("feature_retrieval") or {})})


def train_intraday_task(*, pair: str, timeframe: str, feature_root: str, label_root: str, out: str) -> dict:
    X, y, df = _train_xy(
        pair=pair,
        timeframe=timeframe,
        feature_root=feature_root,
        label_root=label_root,
        feature_service_name=f"fx_{pair.lower()}_intraday_xgb_{str(timeframe).lower()}",
        feature_view_names=["anchor_m5", "context_m15", "context_h1", "context_h4", "context_d", "cross_pair_context"],
    )
    model = IntradayXGB()
    model.fit(X, y)
    model.save(Path(out))
    _annotate_supervised_artifact(
        out=out,
        pair=pair,
        timeframe=timeframe,
        rows=len(X),
        feature_columns=list(X.columns),
        dataset_summary=_frame_summary(df),
        extra={"model_family": "intraday", "feature_retrieval": dict(X.attrs.get("feature_retrieval") or {})},
    )
    return _with_mlops_fields({"model": "intraday_xgb", "rows": len(X), "path": out, **dict(X.attrs.get("feature_retrieval") or {})})


def train_swing_transformer_task(*, pair: str, timeframe: str, feature_root: str, label_root: str, out: str) -> dict:
    X, y, df = _train_xy(
        pair=pair,
        timeframe=timeframe,
        feature_root=feature_root,
        label_root=label_root,
        feature_service_name=f"fx_{pair.lower()}_swing_transformer_{str(timeframe).lower()}",
        feature_view_names=_swing_deep_feature_views(),
    )
    s = get_settings()
    model = SwingTransformer(
        window_size=int(s.transformer_window_size),
        epochs=int(s.deep_train_epochs),
        batch_size=int(s.deep_batch_size),
        require_cuda=bool(s.require_cuda),
    )
    model.fit(X, y)
    model.save(Path(out))
    _annotate_supervised_artifact(
        out=out,
        pair=pair,
        timeframe=timeframe,
        rows=len(X),
        feature_columns=list(X.columns),
        dataset_summary=_frame_summary(df),
        extra={"model_family": "swing_deep", "feature_retrieval": dict(X.attrs.get("feature_retrieval") or {})},
    )
    return _with_mlops_fields({"model": "swing_transformer", "rows": len(X), "path": out, **dict(X.attrs.get("feature_retrieval") or {})})


def train_intraday_tcn_task(*, pair: str, timeframe: str, feature_root: str, label_root: str, out: str) -> dict:
    X, y, df = _train_xy(
        pair=pair,
        timeframe=timeframe,
        feature_root=feature_root,
        label_root=label_root,
        feature_service_name=f"fx_{pair.lower()}_intraday_tcn_{str(timeframe).lower()}",
        feature_view_names=["anchor_m5", "context_m15", "context_h1", "context_h4", "context_d", "cross_pair_context"],
    )
    s = get_settings()
    model = IntradayTCN(
        window_size=int(s.tcn_window_size),
        epochs=int(s.deep_train_epochs),
        batch_size=int(s.deep_batch_size),
        require_cuda=bool(s.require_cuda),
    )
    model.fit(X, y)
    model.save(Path(out))
    _annotate_supervised_artifact(
        out=out,
        pair=pair,
        timeframe=timeframe,
        rows=len(X),
        feature_columns=list(X.columns),
        dataset_summary=_frame_summary(df),
        extra={"model_family": "intraday_deep", "feature_retrieval": dict(X.attrs.get("feature_retrieval") or {})},
    )
    return _with_mlops_fields({"model": "intraday_tcn", "rows": len(X), "path": out, **dict(X.attrs.get("feature_retrieval") or {})})


def _train_patchtst_task(
    *,
    pair: str,
    timeframe: str,
    feature_root: str,
    label_root: str,
    out: str,
    model_key: str,
) -> dict:
    _ensure_patchtst_stack()
    pair_u = str(pair).upper()
    tf_u = str(timeframe).upper()
    s = get_settings()
    feature_views = _swing_deep_feature_views() if str(model_key) == "swing_patchtst" else ["anchor_m5", "context_m15", "context_h1", "context_h4", "context_d", "cross_pair_context"]
    X, y, df = _train_xy(
        pair=pair_u,
        timeframe=tf_u,
        feature_root=feature_root,
        label_root=label_root,
        feature_service_name=f"fx_{pair_u.lower()}_{str(model_key).lower()}_{tf_u.lower()}",
        feature_view_names=feature_views,
    )
    window_size = int(s.transformer_window_size if str(model_key) == "swing_patchtst" else s.tcn_window_size)
    sequence_manifest = build_sequence_dataset_manifest(
        X=X,
        y=y,
        timestamps=df["ts"],
        pair=pair_u,
        timeframe=tf_u,
        window_size=window_size,
        dataset_fingerprint=str(X.attrs.get("dataset_fingerprint") or ""),
        feature_retrieval=dict(X.attrs.get("feature_retrieval") or {}),
        label_config=_patchtst_label_config(timeframe=tf_u),
    )
    if str(model_key) == "swing_patchtst":
        model = SwingPatchTST(
            window_size=window_size,
            patch_length=int(s.patchtst_patch_length),
            stride=int(s.patchtst_stride),
            d_model=int(s.patchtst_d_model),
            num_layers=int(s.patchtst_num_layers),
            num_heads=int(s.patchtst_num_heads),
            dropout=float(s.patchtst_dropout),
            epochs=int(s.deep_train_epochs),
            batch_size=int(s.deep_batch_size),
            require_cuda=bool(s.require_cuda),
        )
        incumbent = ChallengerSpec(
            name="incumbent_swing_xgb",
            factory=lambda: SwingXGB(),
            model_family="swing_xgb",
            runtime_role="incumbent",
        )
        model_family = "swing_patchtst"
    else:
        model = IntradayPatchTST(
            window_size=window_size,
            patch_length=int(s.patchtst_patch_length),
            stride=int(s.patchtst_stride),
            d_model=int(s.patchtst_d_model),
            num_layers=int(s.patchtst_num_layers),
            num_heads=int(s.patchtst_num_heads),
            dropout=float(s.patchtst_dropout),
            epochs=int(s.deep_train_epochs),
            batch_size=int(s.deep_batch_size),
            require_cuda=bool(s.require_cuda),
        )
        incumbent = ChallengerSpec(
            name="incumbent_intraday_xgb",
            factory=lambda: IntradayXGB(),
            model_family="intraday_xgb",
            runtime_role="incumbent",
        )
        model_family = "intraday_patchtst"

    model.fit(X, y)
    model.save(Path(out))
    report_dir = _report_dir_from_artifact(out)
    report = validate_candidate(
        model_factory=lambda: type(model)(
            window_size=window_size,
            patch_length=int(s.patchtst_patch_length),
            stride=int(s.patchtst_stride),
            d_model=int(s.patchtst_d_model),
            num_layers=int(s.patchtst_num_layers),
            num_heads=int(s.patchtst_num_heads),
            dropout=float(s.patchtst_dropout),
            epochs=int(s.deep_train_epochs),
            batch_size=int(s.deep_batch_size),
            require_cuda=bool(s.require_cuda),
        ),
        named_challengers=[incumbent],
        portfolio_champion_name=str(incumbent.name),
        X=X,
        y=y,
        timestamps=df["ts"],
        meta=df[["ts", "pair", "timeframe"]].assign(
            session_tag=df.get("session_tag", "unknown"),
            regime_bucket=df.get("regime_bucket", "unknown"),
            scenario_bucket=df.get("scenario_bucket", "unknown"),
        ),
        task="binary",
        report_root=report_dir,
        cv_splits=int(get_settings().cv_splits),
        embargo_pct=float(get_settings().cv_embargo_pct),
        wf_train_months=int(get_settings().wf_train_months),
        wf_test_months=int(get_settings().wf_test_months),
        wf_step_months=int(get_settings().wf_step_months),
    )
    promotion_status = _annotate_validation_result(artifact_path=out, report=report)
    _annotate_supervised_artifact(
        out=out,
        pair=pair_u,
        timeframe=tf_u,
        rows=len(X),
        feature_columns=list(X.columns),
        dataset_summary=_frame_summary(df),
        extra={
            "model_family": model_family,
            "feature_retrieval": dict(X.attrs.get("feature_retrieval") or {}),
            "sequence_dataset_manifest": str(sequence_manifest.manifest_path),
            "portfolio_report": str(report_dir / "portfolio_report.json"),
            "challenger_head_to_head": str(report_dir / "challenger_head_to_head.json"),
            "portfolio_disagreement": str(report_dir / "portfolio_disagreement.json"),
            "shadow_only": True,
        },
    )
    return _with_mlops_fields(
        {
            "model": model_family,
            "rows": len(X),
            "path": out,
            "report_path": str(_report_path_from_artifact(out)),
            "promotion_status": promotion_status,
            "sequence_dataset_manifest": str(sequence_manifest.manifest_path),
            "portfolio_report": str(report_dir / "portfolio_report.json"),
            "challenger_head_to_head": str(report_dir / "challenger_head_to_head.json"),
            "portfolio_disagreement": str(report_dir / "portfolio_disagreement.json"),
            **dict(X.attrs.get("feature_retrieval") or {}),
        }
    )


def train_swing_patchtst_task(*, pair: str, timeframe: str, feature_root: str, label_root: str, out: str) -> dict:
    return _train_patchtst_task(
        pair=pair,
        timeframe=timeframe,
        feature_root=feature_root,
        label_root=label_root,
        out=out,
        model_key="swing_patchtst",
    )


def train_intraday_patchtst_task(*, pair: str, timeframe: str, feature_root: str, label_root: str, out: str) -> dict:
    return _train_patchtst_task(
        pair=pair,
        timeframe=timeframe,
        feature_root=feature_root,
        label_root=label_root,
        out=out,
        model_key="intraday_patchtst",
    )


def train_belief_task(
    *,
    timeframe: str,
    feature_root: str,
    out: str,
    pairs: list[str] | None = None,
    dataset_out: str | None = None,
    max_queries_per_pair: int = 20000,
) -> dict:
    out_payload = train_directional_belief(
        timeframe=timeframe,
        feature_root=feature_root,
        out=out,
        pairs=pairs,
        dataset_out=dataset_out,
        max_queries_per_pair=max_queries_per_pair,
    )
    _annotate_supervised_artifact(
        out=out,
        pair="GLOBAL",
        timeframe=timeframe,
        rows=int(out_payload.get("rows", 0) or 0),
        feature_columns=list(json.loads((Path(out) / "meta.json").read_text(encoding="utf-8")).get("feature_columns") or []),
        dataset_summary=dict(json.loads((Path(out) / "meta.json").read_text(encoding="utf-8")).get("training_window_summary") or {}),
        extra={
            "model_family": "directional_belief_v2",
            "validation_metrics": dict(out_payload.get("validation_metrics") or {}),
        },
    )
    return _with_mlops_fields(out_payload)


def build_belief_dataset_task(
    *,
    timeframe: str,
    feature_root: str,
    out: str,
    pairs: list[str] | None = None,
    max_queries_per_pair: int = 20000,
) -> dict:
    return export_directional_belief_dataset(
        timeframe=timeframe,
        feature_root=feature_root,
        out=out,
        pairs=pairs,
        max_queries_per_pair=max_queries_per_pair,
    )


def train_deep_stale_task(
    *,
    pair: str,
    swing_timeframe: str,
    intraday_timeframe: str,
    feature_root: str,
    label_root: str,
    artifact_root: str,
    stale_hours: float | None = None,
) -> dict:
    s = get_settings()
    stale_cutoff = float(s.deep_retrain_max_age_hours if stale_hours is None else stale_hours)
    pair_root = Path(artifact_root) / str(pair).lower()
    swing_path = pair_root / "swing_transformer"
    intraday_path = pair_root / "intraday_tcn"
    swing_features = ParquetStore(Path(feature_root)).read_pair_timeframe(
        provider=_provider(),
        pair=pair,
        timeframe=str(swing_timeframe).upper(),
    )
    intraday_features = ParquetStore(Path(feature_root)).read_pair_timeframe(
        provider=_provider(),
        pair=pair,
        timeframe=str(intraday_timeframe).upper(),
    )
    swing_decision = artifact_retrain_decision(
        dataset=swing_features,
        artifact_path=swing_path,
        min_new_rows=max(1, int(s.deep_retrain_min_new_rows)),
        max_age_hours=stale_cutoff,
    )
    intraday_decision = artifact_retrain_decision(
        dataset=intraday_features,
        artifact_path=intraday_path,
        min_new_rows=max(1, int(s.deep_retrain_min_new_rows)),
        max_age_hours=stale_cutoff,
    )

    out: dict[str, dict[str, object]] = {
        "swing_transformer": {
            "stale": bool(swing_decision["should_retrain"]),
            "age_hours": swing_decision["age_hours"],
            "path": str(swing_path),
            "action": "skip",
            "reason": str(swing_decision["reason"]),
            "new_rows": int(swing_decision["new_rows"]),
        },
        "intraday_tcn": {
            "stale": bool(intraday_decision["should_retrain"]),
            "age_hours": intraday_decision["age_hours"],
            "path": str(intraday_path),
            "action": "skip",
            "reason": str(intraday_decision["reason"]),
            "new_rows": int(intraday_decision["new_rows"]),
        },
    }

    if bool(swing_decision["should_retrain"]):
        out["swing_transformer"] = dict(
            train_swing_transformer_task(
                pair=pair,
                timeframe=str(swing_timeframe).upper(),
                feature_root=feature_root,
                label_root=label_root,
                out=str(swing_path),
            ),
            stale=True,
            action="retrained",
            reason=str(swing_decision["reason"]),
            new_rows=int(swing_decision["new_rows"]),
        )
    if bool(intraday_decision["should_retrain"]):
        out["intraday_tcn"] = dict(
            train_intraday_tcn_task(
                pair=pair,
                timeframe=str(intraday_timeframe).upper(),
                feature_root=feature_root,
                label_root=label_root,
                out=str(intraday_path),
            ),
            stale=True,
            action="retrained",
            reason=str(intraday_decision["reason"]),
            new_rows=int(intraday_decision["new_rows"]),
        )

    return {
        "pair": str(pair).upper(),
        "stale_hours": float(stale_cutoff),
        "min_new_rows": int(s.deep_retrain_min_new_rows),
        "result": out,
    }


def train_meta_task(
    *,
    pair: str,
    timeframe: str,
    feature_root: str,
    out: str,
    label_root: str | None = None,
    champion_metric: float = 0.0,
    regime_model_path: str = "",
    swing_model_path: str = "",
    intraday_model_path: str = "",
    allow_heuristic_labels: bool | None = None,
) -> dict:
    s = get_settings()
    allow_heuristic = bool(s.allow_heuristic_meta_labels) if allow_heuristic_labels is None else bool(allow_heuristic_labels)
    model_paths = {
        "regime_model_path": str(regime_model_path).strip(),
        "swing_model_path": str(swing_model_path).strip(),
        "intraday_model_path": str(intraday_model_path).strip(),
    }
    supplied_paths = [name for name, value in model_paths.items() if value]
    if label_root:
        labels = _load_lifecycle_dataset(root=_label_store_root(label_root, "meta"), pair=pair, timeframe=timeframe)
    else:
        labels = pd.DataFrame()
    if labels.empty:
        if supplied_paths and len(supplied_paths) != len(model_paths):
            missing = [name for name, value in model_paths.items() if not value]
            raise RuntimeError(f"train_meta requires all model paths together; missing: {','.join(missing)}")
        if not supplied_paths and not allow_heuristic:
            raise RuntimeError(
                "meta labels are missing; rebuild them with trained regime/swing/intraday model paths or enable FXSTACK_ALLOW_HEURISTIC_META_LABELS=1"
            )
        build_meta_labels_task(
            pair=pair,
            timeframe=timeframe,
            feature_root=feature_root,
            label_root=label_root or "fx-quant-stack/data/labels",
            regime_model_path=model_paths["regime_model_path"],
            swing_model_path=model_paths["swing_model_path"],
            intraday_model_path=model_paths["intraday_model_path"],
            allow_heuristic_labels=allow_heuristic,
        )
        labels = _load_lifecycle_dataset(
            root=_label_store_root(label_root or "fx-quant-stack/data/labels", "meta"),
            pair=pair,
            timeframe=timeframe,
        )
    if not allow_heuristic and "candidate_side" not in labels.columns:
        raise RuntimeError(
            "meta label dataset is missing candidate_side; rebuild via labels build-meta with trained model paths"
        )
    X, y, meta, weights = _prepare_lifecycle_xy(
        labels,
        target_col="meta_label",
        drop_extra={
            "meta_label_stressed",
            "sample_weight",
            "realized_edge_bps",
            "realized_edge_after_costs",
            *{c for c in labels.columns if str(c).startswith("realized_edge_after_costs_")},
        },
    )
    model = MetaFilterXGB()
    model.fit(X, y, sample_weight=weights)
    model.save(Path(out))
    _annotate_supervised_artifact(
        out=out,
        pair=pair,
        timeframe=timeframe,
        rows=len(X),
        feature_columns=list(X.columns),
        dataset_summary=_frame_summary(meta),
        extra={"model_family": "meta"},
    )
    report = validate_candidate(
        model_factory=lambda: MetaFilterXGB(),
        challenger_factories=[_seeded_meta_factory(11), _seeded_meta_factory(19)],
        X=X,
        y=y,
        timestamps=meta["ts"],
        meta=meta,
        sample_weight=weights,
        task="binary",
        report_root=_report_dir_from_artifact(out),
        champion_metric=float(champion_metric),
        cost_stress_cols=[c for c in labels.columns if c.startswith("realized_edge_after_costs_")],
        cv_splits=int(get_settings().cv_splits),
        embargo_pct=float(get_settings().cv_embargo_pct),
        wf_train_months=int(get_settings().wf_train_months),
        wf_test_months=int(get_settings().wf_test_months),
        wf_step_months=int(get_settings().wf_step_months),
    )
    promotion_status = _annotate_validation_result(artifact_path=out, report=report)
    return _with_mlops_fields({
        "model": "meta_filter",
        "rows": len(X),
        "path": out,
        "report_path": str(_report_path_from_artifact(out)),
        "promotion_status": promotion_status,
    })


def train_exit_task(
    *,
    pair: str,
    timeframe: str,
    feature_root: str,
    label_root: str,
    out: str,
    champion_metric: float = 0.0,
) -> dict:
    labels = _load_lifecycle_dataset(root=_label_store_root(label_root, "exit"), pair=pair, timeframe=timeframe)
    if labels.empty:
        build_exit_labels_task(pair=pair, timeframe=timeframe, feature_root=feature_root, label_root=label_root)
        labels = _load_lifecycle_dataset(root=_label_store_root(label_root, "exit"), pair=pair, timeframe=timeframe)
    labels, exit_collapse = _collapse_exit_action_labels(labels)
    X, y, meta, weights = _prepare_lifecycle_xy(
        labels,
        target_col="exit_action_id_collapsed",
        drop_extra={
            "exit_action",
            "exit_action_id",
            "exit_action_collapsed",
            "sample_weight",
            "realized_r",
            "mae_r",
            "mfe_r",
            "time_to_best_bars",
            "good_entry",
            "bad_hold",
            "bad_exit",
            "false_reversal",
        },
    )
    model = ExitPolicyXGB()
    model.fit(X, y, sample_weight=weights)
    model.save(Path(out))
    _annotate_supervised_artifact(
        out=out,
        pair=pair,
        timeframe=timeframe,
        rows=len(X),
        feature_columns=list(X.columns),
        dataset_summary=_frame_summary(meta),
        extra={"model_family": "exit", "exit_action_collapse": exit_collapse},
    )
    report = validate_candidate(
        model_factory=lambda: ExitPolicyXGB(),
        X=X,
        y=y,
        timestamps=meta["ts"],
        meta=meta,
        sample_weight=weights,
        task="multiclass",
        report_root=_report_dir_from_artifact(out),
        champion_metric=float(champion_metric),
        cv_splits=int(get_settings().cv_splits),
        embargo_pct=float(get_settings().cv_embargo_pct),
        wf_train_months=int(get_settings().wf_train_months),
        wf_test_months=int(get_settings().wf_test_months),
        wf_step_months=int(get_settings().wf_step_months),
    )
    promotion_status = _annotate_validation_result(artifact_path=out, report=report)
    return _with_mlops_fields({
        "model": "exit_policy_xgb",
        "rows": len(X),
        "path": out,
        "report_path": str(_report_path_from_artifact(out)),
        "promotion_status": promotion_status,
    })


def train_reversal_task(
    *,
    pair: str,
    timeframe: str,
    feature_root: str,
    label_root: str,
    out_failure: str,
    out_opportunity: str,
    champion_metric: float = 0.0,
) -> dict:
    labels = _load_lifecycle_dataset(root=_label_store_root(label_root, "reversal"), pair=pair, timeframe=timeframe)
    if labels.empty:
        build_reversal_labels_task(pair=pair, timeframe=timeframe, feature_root=feature_root, label_root=label_root)
        labels = _load_lifecycle_dataset(root=_label_store_root(label_root, "reversal"), pair=pair, timeframe=timeframe)

    Xf, yf, metaf, wf = _prepare_lifecycle_xy(labels, target_col="thesis_failure", drop_extra={"sample_weight", "opposite_opportunity", "reversal_timing_quality"})
    failure_model = ReversalFailureXGB()
    failure_model.fit(Xf, yf, sample_weight=wf)
    failure_model.save(Path(out_failure))
    _annotate_supervised_artifact(
        out=out_failure,
        pair=pair,
        timeframe=timeframe,
        rows=len(Xf),
        feature_columns=list(Xf.columns),
        dataset_summary=_frame_summary(metaf),
        extra={"model_family": "reversal_failure"},
    )
    failure_report = validate_candidate(
        model_factory=lambda: ReversalFailureXGB(),
        X=Xf,
        y=yf,
        timestamps=metaf["ts"],
        meta=metaf,
        sample_weight=wf,
        task="binary",
        report_root=_report_dir_from_artifact(out_failure),
        champion_metric=float(champion_metric),
        cv_splits=int(get_settings().cv_splits),
        embargo_pct=float(get_settings().cv_embargo_pct),
        wf_train_months=int(get_settings().wf_train_months),
        wf_test_months=int(get_settings().wf_test_months),
        wf_step_months=int(get_settings().wf_step_months),
    )
    failure_promotion_status = _annotate_validation_result(artifact_path=out_failure, report=failure_report)

    Xo, yo, metao, wo = _prepare_lifecycle_xy(labels, target_col="opposite_opportunity", drop_extra={"sample_weight", "thesis_failure", "reversal_timing_quality"})
    opp_model = ReversalOpportunityXGB()
    opp_model.fit(Xo, yo, sample_weight=wo)
    opp_model.save(Path(out_opportunity))
    _annotate_supervised_artifact(
        out=out_opportunity,
        pair=pair,
        timeframe=timeframe,
        rows=len(Xo),
        feature_columns=list(Xo.columns),
        dataset_summary=_frame_summary(metao),
        extra={"model_family": "reversal_opportunity"},
    )
    opp_report = validate_candidate(
        model_factory=lambda: ReversalOpportunityXGB(),
        X=Xo,
        y=yo,
        timestamps=metao["ts"],
        meta=metao,
        sample_weight=wo,
        task="binary",
        report_root=_report_dir_from_artifact(out_opportunity),
        champion_metric=float(champion_metric),
        cv_splits=int(get_settings().cv_splits),
        embargo_pct=float(get_settings().cv_embargo_pct),
        wf_train_months=int(get_settings().wf_train_months),
        wf_test_months=int(get_settings().wf_test_months),
        wf_step_months=int(get_settings().wf_step_months),
    )
    opp_promotion_status = _annotate_validation_result(artifact_path=out_opportunity, report=opp_report)
    return _with_mlops_fields({
        "failure_model": {
            "path": out_failure,
            "report_path": str(_report_path_from_artifact(out_failure)),
            "promotion_status": failure_promotion_status,
        },
        "opportunity_model": {
            "path": out_opportunity,
            "report_path": str(_report_path_from_artifact(out_opportunity)),
            "promotion_status": opp_promotion_status,
        },
    })
