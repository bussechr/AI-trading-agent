from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import time
from typing import Any

import pandas as pd

from fxstack.features.multi_tf_contract import build_latest_multi_tf_row
from fxstack.io.parquet_store import ParquetStore
from fxstack.settings import get_settings


@dataclass(slots=True)
class FeatureServingTelemetry:
    source: str = "raw_contract_fallback"
    source_chain: list[str] = field(default_factory=lambda: ["feast_online", "parquet_fallback", "raw_contract_fallback"])
    feature_service: str = ""
    cache_hit: bool = False
    freshness_secs: float | None = None
    stale: bool = False
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_chain": list(self.source_chain),
            "feature_service": self.feature_service,
            "cache_hit": bool(self.cache_hit),
            "freshness_secs": self.freshness_secs,
            "stale": bool(self.stale),
            "reason": str(self.reason or ""),
            "details": dict(self.details),
        }


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _feature_service_name(pair: str, timeframe: str) -> str:
    tf = str(timeframe).upper()
    component = "intraday_xgb"
    if tf == "H4":
        component = "regime_hmm"
    elif tf == "D":
        component = "swing_xgb"
    return f"fx_{str(pair).lower()}_{component}_{tf.lower()}"


@lru_cache(maxsize=8)
def _cached_feature_store_handle(repo_root: str, tracking_uri: str, registry_uri: str) -> Any:
    try:
        from feast import FeatureStore  # type: ignore
    except Exception:
        return None

    repo = Path(repo_root).expanduser()
    if not repo.exists():
        return None
    try:
        return FeatureStore(repo_path=str(repo))
    except Exception:
        return None


def _resolve_feast_online_row(
    *,
    pair: str,
    timeframe: str,
    raw_store: ParquetStore,
    provider: str,
    feature_repo_root: Path | None = None,
    feature_service_name: str | None = None,
) -> tuple[pd.DataFrame, FeatureServingTelemetry]:
    settings = get_settings()
    repo_root = Path(feature_repo_root or settings.feast_repo_root)
    canonical_service_name = _feature_service_name(pair, timeframe)
    requested_service_name = _safe_str(feature_service_name)
    service_candidates = [name for name in [requested_service_name, canonical_service_name] if name]
    if not service_candidates:
        service_candidates = [canonical_service_name]
    handle = _cached_feature_store_handle(str(repo_root), str(settings.mlflow_tracking_uri), str(settings.mlflow_registry_uri or settings.mlflow_tracking_uri))
    if handle is None:
        return pd.DataFrame(), FeatureServingTelemetry(
            source="parquet_fallback",
            feature_service=requested_service_name or canonical_service_name,
            reason="feast_unavailable",
            details={"feast_available": False},
        )

    last_error: Exception | None = None
    attempted_services: list[str] = []
    for service_name in service_candidates:
        attempted_services.append(service_name)
        try:
            entity_rows = [{"pair": str(pair).upper()}]
            if hasattr(handle, "get_feature_service"):
                feature_service = handle.get_feature_service(service_name)
                retrieval = handle.get_online_features(features=feature_service, entity_rows=entity_rows)
            else:
                retrieval = handle.get_online_features(features=[service_name], entity_rows=entity_rows)
            features = retrieval.to_df()
            if not features.empty:
                out = features.copy()
                if "ts" not in out.columns and "event_timestamp" in out.columns:
                    out["ts"] = out["event_timestamp"]
                details: dict[str, Any] = {}
                if requested_service_name and requested_service_name != service_name:
                    details["fallback_from_service"] = requested_service_name
                return out, FeatureServingTelemetry(
                    source="feast_online",
                    feature_service=service_name,
                    cache_hit=True,
                    reason="ok",
                    details=details,
                )
        except Exception as exc:
            last_error = exc
            continue
    if last_error is None:
        return pd.DataFrame(), FeatureServingTelemetry(
            source="parquet_fallback",
            feature_service=requested_service_name or canonical_service_name,
            reason="feast_empty",
            details={"attempted_services": attempted_services},
        )
    return pd.DataFrame(), FeatureServingTelemetry(
        source="parquet_fallback",
        feature_service=requested_service_name or canonical_service_name,
        reason=f"feast_error:{type(last_error).__name__}",
        details={"error": str(last_error), "attempted_services": attempted_services},
    )


def _resolve_parquet_latest_row(*, store: ParquetStore, provider: str, pair: str, timeframe: str) -> pd.DataFrame:
    if hasattr(store, "read_latest_row"):
        row = store.read_latest_row(provider=provider, pair=pair, timeframe=timeframe, tail_files=3)
        if not row.empty:
            return row
    df = store.read_pair_timeframe(provider=provider, pair=pair, timeframe=timeframe)
    if df.empty:
        return pd.DataFrame()
    return df.sort_values("ts").tail(1).copy()


def _merge_latest_row(base_row: pd.DataFrame, latest_row: pd.DataFrame) -> pd.DataFrame:
    if base_row.empty:
        return latest_row.copy()
    if latest_row.empty:
        return base_row.copy()
    base = base_row.reset_index(drop=True).iloc[0].to_dict()
    src = latest_row.reset_index(drop=True).iloc[0].to_dict()
    for col, value in src.items():
        current = base.get(col)
        if col not in base or pd.isna(current):
            base[col] = value
    return pd.DataFrame([base])


def resolve_latest_feature_row(
    *,
    store: ParquetStore,
    raw_store: ParquetStore,
    pair: str,
    timeframe: str,
    provider: str | None = None,
    feature_repo_root: Path | None = None,
    feature_service_name: str | None = None,
    all_pairs: list[str] | None = None,
) -> tuple[pd.DataFrame, FeatureServingTelemetry]:
    started = time.perf_counter()
    provider_value = str(provider or get_settings().normalized_data_provider)
    online_row, telemetry = _resolve_feast_online_row(
        pair=pair,
        timeframe=timeframe,
        raw_store=raw_store,
        provider=provider_value,
        feature_repo_root=feature_repo_root,
        feature_service_name=feature_service_name,
    )
    if not online_row.empty:
        enriched = online_row.copy()
        parquet_row = _resolve_parquet_latest_row(store=store, provider=provider_value, pair=pair, timeframe=timeframe)
        if not parquet_row.empty:
            enriched = _merge_latest_row(enriched, parquet_row)
            telemetry.details["parquet_enriched"] = True

        if all_pairs:
            raw_df, _ = build_latest_multi_tf_row(
                pair=str(pair).upper(),
                raw_store_root=Path(raw_store.root),
                provider=provider_value,
                anchor_timeframe=str(timeframe).upper(),
                context_timeframes=["M15", "H1", "H4", "D"],
                all_pairs=list(all_pairs or []),
            )
            if not raw_df.empty:
                latest_raw = raw_df.sort_values("ts").tail(1).copy()
                enriched = _merge_latest_row(enriched, latest_raw)
                telemetry.details["raw_contract_enriched"] = True

        ts = pd.to_datetime(enriched.iloc[0].get("ts"), utc=True, errors="coerce")
        if pd.notna(ts):
            telemetry.freshness_secs = max(0.0, time.time() - float(pd.Timestamp(ts).timestamp()))
            telemetry.stale = bool(telemetry.freshness_secs > float(get_settings().feast_online_stale_secs))
        telemetry.details["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        return enriched, telemetry

    parquet_row = _resolve_parquet_latest_row(store=store, provider=provider_value, pair=pair, timeframe=timeframe)
    if not parquet_row.empty:
        parquet_tel = FeatureServingTelemetry(
            source="parquet_fallback",
            feature_service=telemetry.feature_service,
            reason=telemetry.reason or "parquet_hit",
            details={"fallback_from": telemetry.source, "latency_ms": round((time.perf_counter() - started) * 1000.0, 3)},
        )
        ts = pd.to_datetime(parquet_row.iloc[0].get("ts"), utc=True, errors="coerce")
        if pd.notna(ts):
            parquet_tel.freshness_secs = max(0.0, time.time() - float(pd.Timestamp(ts).timestamp()))
        return parquet_row, parquet_tel

    raw_df, _ = build_latest_multi_tf_row(
        pair=str(pair).upper(),
        raw_store_root=Path(raw_store.root),
        provider=provider_value,
        anchor_timeframe=str(timeframe).upper(),
        context_timeframes=["M15", "H1", "H4", "D"],
        all_pairs=list(all_pairs or []),
    )
    if not raw_df.empty:
        return raw_df, FeatureServingTelemetry(
            source="raw_contract_fallback",
            feature_service=telemetry.feature_service,
            reason="raw_contract_rebuild",
            details={"fallback_from": telemetry.source, "latency_ms": round((time.perf_counter() - started) * 1000.0, 3)},
        )

    return pd.DataFrame(), FeatureServingTelemetry(
        source="raw_contract_fallback",
        feature_service=telemetry.feature_service,
        reason="no_feature_rows",
        details={"fallback_from": telemetry.source, "latency_ms": round((time.perf_counter() - started) * 1000.0, 3)},
    )
