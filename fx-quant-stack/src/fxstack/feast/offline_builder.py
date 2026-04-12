from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.feast.repository import artifact_feature_service_ref, component_default_timeframe, feature_repo_root
from fxstack.feast.types import FeatureServiceRef, HistoricalDatasetProvenance
from fxstack.io.parquet_store import ParquetStore
from fxstack.settings import get_settings

_REQUIRED_CROSS_PAIR_CONTEXT_COLUMNS = ("usd_strength_basket_ret_1", "cross_pair_dispersion")


def _normalize_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def _coerce_entity_frame(entity_df: pd.DataFrame, *, pair: str) -> pd.DataFrame:
    frame = entity_df.copy()
    if "event_timestamp" not in frame.columns:
        if "ts" in frame.columns:
            frame["event_timestamp"] = frame["ts"]
        else:
            raise RuntimeError("entity dataframe must include event_timestamp or ts")
    frame["event_timestamp"] = _normalize_ts(frame["event_timestamp"])
    frame = frame.loc[frame["event_timestamp"].notna()].copy()
    if "pair" not in frame.columns:
        frame["pair"] = str(pair).upper()
    else:
        frame["pair"] = frame["pair"].astype(str).str.upper()
    return frame


def _component_key_from_service_name(service_name: str, *, timeframe: str) -> str:
    raw = str(service_name or "").strip().lower()
    if not raw:
        return str(timeframe or "feature_frame").strip().lower() or "feature_frame"
    for marker in [
        "directional_belief",
        "reversal_opportunity_xgb",
        "reversal_failure_xgb",
        "exit_policy_xgb",
        "intraday_xgb",
        "intraday_tcn",
        "swing_transformer",
        "swing_xgb",
        "meta_filter",
        "regime_hmm",
    ]:
        if marker in raw:
            return marker
    return "feature_frame"


def build_entity_dataframe(
    rows: pd.DataFrame,
    *,
    pair: str,
    ts_col: str = "ts",
) -> pd.DataFrame:
    frame = rows.copy()
    if ts_col not in frame.columns:
        raise RuntimeError(f"missing timestamp column '{ts_col}'")
    entity_df = pd.DataFrame(
        {
            "pair": str(pair).upper(),
            "event_timestamp": _normalize_ts(frame[ts_col]),
        }
    )
    return entity_df.loc[entity_df["event_timestamp"].notna()].reset_index(drop=True)


@lru_cache(maxsize=4)
def _cached_feature_store(repo_root: str) -> Any:
    try:
        from feast import FeatureStore  # type: ignore
    except Exception:
        return None
    repo = Path(repo_root)
    if not repo.exists():
        return None
    try:
        return FeatureStore(repo_path=str(repo))
    except Exception:
        return None


def _feast_historical(
    *,
    service_ref: FeatureServiceRef,
    entity_df: pd.DataFrame,
) -> tuple[pd.DataFrame, str]:
    store = _cached_feature_store(str(feature_repo_root()))
    if store is None:
        return pd.DataFrame(), "feast_unavailable"
    try:
        feature_service = store.get_feature_service(service_ref.name) if hasattr(store, "get_feature_service") else None
        if feature_service is not None:
            retrieval = store.get_historical_features(entity_df=entity_df, features=feature_service)
        else:
            retrieval = store.get_historical_features(
                entity_df=entity_df,
                features=[f"{service_ref.name}:{name}" for name in service_ref.feature_columns],
            )
        frame = retrieval.to_df()
        if "event_timestamp" in frame.columns and "ts" not in frame.columns:
            frame["ts"] = frame["event_timestamp"]
        return frame, "feast_historical"
    except Exception as exc:
        return pd.DataFrame(), f"feast_error:{type(exc).__name__}"


def _parquet_point_in_time(
    *,
    pair: str,
    timeframe: str,
    feature_root: str,
    entity_df: pd.DataFrame,
) -> pd.DataFrame:
    s = get_settings()
    feats = ParquetStore(Path(feature_root)).read_pair_timeframe(
        provider=s.normalized_data_provider,
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
    )
    if feats.empty:
        return pd.DataFrame()
    left = entity_df.copy()
    left["__entity_idx__"] = range(len(left))
    left["__event_ts__"] = _normalize_ts(left["event_timestamp"])
    left = left.loc[left["__event_ts__"].notna()].sort_values("__event_ts__")

    right = feats.copy()
    right["__feature_ts__"] = _normalize_ts(right["ts"])
    right = right.loc[right["__feature_ts__"].notna()].sort_values("__feature_ts__")

    merged = pd.merge_asof(
        left,
        right,
        left_on="__event_ts__",
        right_on="__feature_ts__",
        by="pair",
        direction="backward",
        allow_exact_matches=True,
    )
    merged = merged.sort_values("__entity_idx__").drop(columns=["__entity_idx__", "__event_ts__", "__feature_ts__"], errors="ignore")
    if "event_timestamp" in merged.columns and "ts" not in merged.columns:
        merged["ts"] = merged["event_timestamp"]
    return merged.reset_index(drop=True)


def retrieve_historical_features(
    *,
    pair: str,
    component_key: str,
    feature_root: str,
    timeframe: str | None = None,
    entity_df: pd.DataFrame | None = None,
    feature_columns: list[str] | None = None,
    artifact_meta: dict[str, Any] | None = None,
    all_pairs: list[str] | None = None,
    context_timeframes: list[str] | None = None,
) -> tuple[pd.DataFrame, HistoricalDatasetProvenance]:
    tf = str(timeframe or component_default_timeframe(component_key)).upper()
    service_ref = artifact_feature_service_ref(
        pair=pair,
        component_key=component_key,
        artifact_meta=artifact_meta or {"feature_columns": list(feature_columns or []), "timeframe": tf},
        timeframe=tf,
    )
    if entity_df is None:
        base_frame = ParquetStore(Path(feature_root)).read_pair_timeframe(
            provider=get_settings().normalized_data_provider,
            pair=str(pair).upper(),
            timeframe=tf,
        )
        if base_frame.empty:
            provenance = HistoricalDatasetProvenance(
                pair=str(pair).upper(),
                timeframe=tf,
                component_key=str(component_key),
                feature_service_name=service_ref.name,
                feature_service_version=service_ref.version,
                feature_contract_hash=service_ref.feature_contract_hash,
                feature_view_names=list(service_ref.feature_view_names),
                retrieval_source="empty",
                point_in_time_key=service_ref.point_in_time_key,
                provider=get_settings().normalized_data_provider,
                repo_root=str(feature_repo_root()),
                all_pairs=[str(item).upper() for item in list(all_pairs or [])],
                context_timeframes=[str(item).upper() for item in list(context_timeframes or [])],
            )
            return pd.DataFrame(), provenance
        entity_df = build_entity_dataframe(base_frame[["ts"]], pair=pair, ts_col="ts")
    entity_df = _coerce_entity_frame(entity_df, pair=pair)

    frame, retrieval_source = _feast_historical(service_ref=service_ref, entity_df=entity_df)
    fallback_reason = ""
    if frame.empty:
        frame = _parquet_point_in_time(pair=pair, timeframe=tf, feature_root=feature_root, entity_df=entity_df)
        fallback_reason = retrieval_source if retrieval_source != "feast_historical" else ""
        retrieval_source = "single_frame_parquet"

    provenance = HistoricalDatasetProvenance(
        pair=str(pair).upper(),
        timeframe=tf,
        component_key=str(component_key),
        feature_service_name=service_ref.name,
        feature_service_version=service_ref.version,
        feature_contract_hash=service_ref.feature_contract_hash,
        feature_view_names=list(service_ref.feature_view_names),
        retrieval_source=retrieval_source,
        point_in_time_key=service_ref.point_in_time_key,
        provider=get_settings().normalized_data_provider,
        repo_root=str(feature_repo_root()),
        fallback_reason=fallback_reason,
        entity_rows=int(len(entity_df)),
        matched_rows=int(len(frame)),
        all_pairs=[str(item).upper() for item in list(all_pairs or [])],
        context_timeframes=[str(item).upper() for item in list(context_timeframes or [])],
        details={"feature_columns": list(service_ref.feature_columns)},
    )
    return frame, provenance


def join_labels_with_features(
    *,
    pair: str,
    component_key: str,
    feature_root: str,
    label_frame: pd.DataFrame,
    timeframe: str | None = None,
    feature_columns: list[str] | None = None,
    artifact_meta: dict[str, Any] | None = None,
    all_pairs: list[str] | None = None,
    context_timeframes: list[str] | None = None,
) -> tuple[pd.DataFrame, HistoricalDatasetProvenance]:
    if label_frame.empty:
        raise RuntimeError("labels are empty")
    entity_df = build_entity_dataframe(label_frame[["ts"]], pair=pair, ts_col="ts")
    features, provenance = retrieve_historical_features(
        pair=pair,
        component_key=component_key,
        feature_root=feature_root,
        timeframe=timeframe,
        entity_df=entity_df,
        feature_columns=feature_columns,
        artifact_meta=artifact_meta,
        all_pairs=all_pairs,
        context_timeframes=context_timeframes,
    )
    if features.empty:
        raise RuntimeError("historical feature retrieval returned no rows")
    left = label_frame.copy().sort_values("ts").reset_index(drop=True)
    right = features.copy().sort_values("ts").reset_index(drop=True)
    merged = left.merge(right, on=["pair", "ts"], how="inner", suffixes=("", "_feature"))
    merged.attrs["dataset_provenance"] = provenance.to_dict()
    return merged, provenance


def load_pair_feature_frame(
    *,
    pair: str,
    component_key: str,
    feature_root: str,
    timeframe: str | None = None,
    feature_columns: list[str] | None = None,
    artifact_meta: dict[str, Any] | None = None,
    all_pairs: list[str] | None = None,
    context_timeframes: list[str] | None = None,
) -> tuple[pd.DataFrame, HistoricalDatasetProvenance]:
    return retrieve_historical_features(
        pair=pair,
        component_key=component_key,
        feature_root=feature_root,
        timeframe=timeframe,
        entity_df=None,
        feature_columns=feature_columns,
        artifact_meta=artifact_meta,
        all_pairs=all_pairs,
        context_timeframes=context_timeframes,
    )


def build_historical_feature_frame(
    *,
    feature_root: str,
    pair: str,
    timeframe: str,
    feature_service_name: str = "",
    feature_view_names: list[str] | None = None,
    entity_df: pd.DataFrame | None = None,
    all_pairs: list[str] | None = None,
    context_timeframes: list[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    component_key = _component_key_from_service_name(str(feature_service_name), timeframe=str(timeframe))
    frame, provenance = retrieve_historical_features(
        pair=pair,
        component_key=component_key,
        feature_root=feature_root,
        timeframe=timeframe,
        entity_df=entity_df,
        feature_columns=None,
        artifact_meta={
            "feature_columns": [],
            "timeframe": str(timeframe).upper(),
        },
        all_pairs=all_pairs,
        context_timeframes=context_timeframes,
    )
    meta = provenance.to_dict()
    if not str(feature_service_name).strip():
        feature_service_name = f"fx_{str(pair).lower()}_{str(timeframe).lower()}"
    if str(feature_service_name).strip():
        meta["feature_service_name"] = str(feature_service_name).strip()
        provenance.feature_service_name = str(feature_service_name).strip()
    if feature_view_names:
        meta["feature_view_names"] = [str(item) for item in feature_view_names]
        provenance.feature_view_names = [str(item) for item in feature_view_names]
    requested_cross_pair_context = "cross_pair_context" in {str(item) for item in list(feature_view_names or [])}
    if requested_cross_pair_context:
        available_columns = [name for name in _REQUIRED_CROSS_PAIR_CONTEXT_COLUMNS if name in frame.columns]
        missing_columns = [name for name in _REQUIRED_CROSS_PAIR_CONTEXT_COLUMNS if name not in frame.columns]
        meta["cross_pair_context_requested"] = True
        meta["cross_pair_context_available"] = not missing_columns
        meta["cross_pair_context_columns"] = available_columns
        meta["cross_pair_context_missing_columns"] = missing_columns
    else:
        meta["cross_pair_context_requested"] = False
        meta["cross_pair_context_available"] = False
        meta["cross_pair_context_columns"] = []
        meta["cross_pair_context_missing_columns"] = []
    meta["feature_retrieval"] = str(provenance.retrieval_source)
    meta["source"] = str(provenance.retrieval_source)
    return frame, meta
