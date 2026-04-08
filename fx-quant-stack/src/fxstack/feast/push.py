from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.runtime.postgres_store import PostgresRuntimeStore
from fxstack.settings import get_settings


def _client_store(client: Any) -> PostgresRuntimeStore:
    if isinstance(client, PostgresRuntimeStore):
        return client
    store = getattr(client, "store", None)
    if isinstance(store, PostgresRuntimeStore):
        return store
    raise TypeError("feature push client must be a PostgresRuntimeStore or RuntimeService")


def build_outbox_key(*, pair: str, feature_service: str, entity_key: str, event_timestamp: float, feature_version: str = "") -> str:
    parts = [
        str(pair or "").upper().strip(),
        str(feature_service or "").strip(),
        str(entity_key or "").strip(),
        f"{float(event_timestamp):.6f}",
        str(feature_version or "").strip(),
    ]
    return "|".join(parts)


def _json_ready(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    if pd.isna(value):
        return None
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return value


def build_push_payload(
    *,
    pair: str,
    feature_service: str,
    entity_key: str,
    event_timestamp: float,
    feature_values: dict[str, Any],
    feature_version: str = "",
    checksum: str = "",
    source: str = "feast",
) -> dict[str, Any]:
    payload = {
        "pair": str(pair).upper().strip(),
        "feature_service": str(feature_service).strip(),
        "entity_key": str(entity_key).strip(),
        "event_timestamp": float(event_timestamp),
        "feature_version": str(feature_version or ""),
        "checksum": str(checksum or ""),
        "source": str(source or "feast"),
        "feature_values": _json_ready(dict(feature_values or {})),
    }
    payload["outbox_key"] = build_outbox_key(
        pair=payload["pair"],
        feature_service=payload["feature_service"],
        entity_key=payload["entity_key"],
        event_timestamp=payload["event_timestamp"],
        feature_version=payload["feature_version"],
    )
    return payload


def enqueue_feature_push(store: PostgresRuntimeStore, payload: dict[str, Any]) -> dict[str, Any]:
    return _client_store(store).enqueue_feature_push(payload)


def claim_feature_push_batch(
    store: PostgresRuntimeStore,
    *,
    worker_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return _client_store(store).claim_feature_push_batch(worker_id=worker_id, limit=limit)


def record_feature_push_success(
    store: PostgresRuntimeStore,
    *,
    outbox_key: str,
    worker_id: str | None = None,
    payload: dict[str, Any] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    return _client_store(store).mark_feature_push_success(
        outbox_key=outbox_key,
        worker_id=worker_id,
        payload=payload,
        message=message,
    )


def record_feature_push_failure(
    store: PostgresRuntimeStore,
    *,
    outbox_key: str,
    worker_id: str | None = None,
    message: str,
    payload: dict[str, Any] | None = None,
    retryable: bool = True,
) -> dict[str, Any]:
    return _client_store(store).mark_feature_push_failure(
        outbox_key=outbox_key,
        worker_id=worker_id,
        message=message,
        payload=payload,
        retryable=retryable,
    )


def record_feature_parity(
    store: PostgresRuntimeStore,
    *,
    pair: str,
    feature_service: str,
    entity_key: str,
    event_timestamp: float,
    parity_ok: bool,
    payload: dict[str, Any],
    source: str = "online",
    drift_score: float | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    return _client_store(store).record_feature_parity_audit(
        pair=pair,
        feature_service=feature_service,
        entity_key=entity_key,
        event_timestamp=event_timestamp,
        source=source,
        parity_ok=parity_ok,
        payload=payload,
        drift_score=drift_score,
        message=message,
    )


@dataclass(slots=True)
class FeaturePushWorker:
    store: Any
    worker_id: str

    def claim(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return claim_feature_push_batch(self.store, worker_id=self.worker_id, limit=limit)

    def success(self, *, outbox_key: str, payload: dict[str, Any] | None = None, message: str | None = None) -> dict[str, Any]:
        return record_feature_push_success(self.store, outbox_key=outbox_key, worker_id=self.worker_id, payload=payload, message=message)

    def failure(
        self,
        *,
        outbox_key: str,
        message: str,
        payload: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> dict[str, Any]:
        return record_feature_push_failure(
            self.store,
            outbox_key=outbox_key,
            worker_id=self.worker_id,
            message=message,
            payload=payload,
            retryable=retryable,
        )


@lru_cache(maxsize=4)
def _feature_store_handle(repo_root: str) -> Any:
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


def publish_feature_payload(
    *,
    payload: dict[str, Any],
    repo_root: str | None = None,
) -> tuple[bool, str]:
    settings = get_settings()
    handle = _feature_store_handle(str(repo_root or settings.feast_repo_root))
    if handle is None:
        return False, "feast_unavailable"
    values = dict(payload.get("feature_values") or {})
    values = {
        str(key): _json_ready(value)
        for key, value in values.items()
        if str(key) not in {"pair", "provider", "timeframe", "ts", "event_timestamp", "date"}
    }
    values = {
        key: float(int(value)) if isinstance(value, bool) else float(value) if isinstance(value, (int, float)) else value
        for key, value in values.items()
        if value is not None
    }
    if not values:
        return False, "feature_values_missing"
    feature_service = str(payload.get("feature_service") or "").strip()
    field_types: dict[str, str] = {}
    try:
        if feature_service and hasattr(handle, "get_feature_view"):
            feature_view = handle.get_feature_view(f"{feature_service}__fv")
            for field in list(getattr(feature_view, "schema", []) or []):
                name = str(getattr(field, "name", "")).strip()
                if not name:
                    continue
                dtype = getattr(field, "dtype", None)
                value_type = getattr(dtype, "to_value_type", lambda: dtype)()
                field_types[name] = str(value_type)
    except Exception:
        field_types = {}
    if field_types:
        values = {key: value for key, value in values.items() if key in field_types}
        for field_name, field_type in sorted(field_types.items()):
            if field_name in values:
                if "STRING" in field_type:
                    values[field_name] = str(values[field_name])
                elif "BOOL" in field_type:
                    values[field_name] = bool(values[field_name])
                elif values[field_name] is not None:
                    values[field_name] = float(values[field_name])
                continue
            if "STRING" in field_type:
                values[field_name] = ""
            elif "BOOL" in field_type:
                values[field_name] = False
            else:
                values[field_name] = float("nan")
    if not values:
        return False, "feature_values_not_in_schema"
    event_timestamp = pd.to_datetime(float(payload.get("event_timestamp") or 0.0), unit="s", utc=True, errors="coerce")
    if pd.isna(event_timestamp):
        return False, "event_timestamp_invalid"
    frame = pd.DataFrame(
        [
            {
                "pair": str(payload.get("pair") or "").upper(),
                "event_timestamp": event_timestamp,
                **values,
            }
        ]
    )
    push_source_name = str(payload.get("push_source") or f"{feature_service}__push").strip()
    feature_view_name = f"{feature_service}__fv" if feature_service else ""
    try:
        if feature_view_name and hasattr(handle, "write_to_online_store"):
            handle.write_to_online_store(feature_view_name, frame)  # type: ignore[arg-type]
            return True, "online_store_written"
        if hasattr(handle, "push"):
            handle.push(push_source_name, frame)  # type: ignore[arg-type]
            return True, "pushed"
        return False, "feature_store_push_api_unavailable"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def drain_feature_push_outbox(
    store: Any,
    *,
    worker_id: str,
    limit: int = 50,
    repo_root: str | None = None,
    dry_run: bool = False,
    max_retries: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    retry_limit = max(1, int(max_retries or settings.feature_push_max_retries))
    service = store if hasattr(store, "record_governance_event") else None
    worker = FeaturePushWorker(store=store, worker_id=worker_id)
    claimed = worker.claim(limit=limit)
    summary: dict[str, Any] = {
        "worker_id": worker_id,
        "claimed": int(len(claimed)),
        "succeeded": 0,
        "failed": 0,
        "retried": 0,
        "dry_run": bool(dry_run),
        "results": [],
    }
    for row in claimed:
        outbox_key = str(row.get("outbox_key") or "")
        payload = dict(row.get("payload_json") or {})
        if dry_run:
            worker.success(outbox_key=outbox_key, payload=payload, message="dry_run")
            summary["succeeded"] = int(summary["succeeded"]) + 1
            summary["results"].append({"outbox_key": outbox_key, "status": "succeeded", "message": "dry_run"})
            continue
        ok, message = publish_feature_payload(payload=payload, repo_root=repo_root)
        if ok:
            worker.success(outbox_key=outbox_key, payload=payload, message=message)
            summary["succeeded"] = int(summary["succeeded"]) + 1
            summary["results"].append({"outbox_key": outbox_key, "status": "succeeded", "message": message})
            continue
        retryable = int(row.get("attempt_count") or 0) < retry_limit
        worker.failure(outbox_key=outbox_key, message=message, payload=payload, retryable=retryable)
        if service is not None:
            service.record_governance_event(
                event_type="feature_push_retry" if bool(retryable) else "feature_push_failed",
                reason=str(message or ""),
                payload={
                    "outbox_key": outbox_key,
                    "worker_id": str(worker_id),
                    "feature_service": str(payload.get("feature_service") or ""),
                    "pair": str(payload.get("pair") or "").upper(),
                    "retryable": bool(retryable),
                },
            )
        if retryable:
            summary["retried"] = int(summary["retried"]) + 1
            status = "retry"
        else:
            summary["failed"] = int(summary["failed"]) + 1
            status = "failed"
        summary["results"].append({"outbox_key": outbox_key, "status": status, "message": message})
    return summary
