"""Helpers for deterministic orchestration context assembly."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fxstack.orchestration.contracts import DecisionContext, VersionBundle
from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION


ORCHESTRATOR_VERSION = "orchestration.phase4.v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def build_thread_id(*, pair: str, cycle_id: str, runtime_mode: str) -> str:
    return f"{str(pair).upper()}:{str(cycle_id)}:{str(runtime_mode)}"


def build_correlation_id(*, thread_id: str) -> str:
    return str(thread_id)


def build_idempotency_key(
    *,
    pair: str,
    cycle_id: str,
    runtime_mode: str,
    payload: dict[str, Any],
) -> str:
    material = {
        "pair": str(pair).upper(),
        "cycle_id": str(cycle_id),
        "runtime_mode": str(runtime_mode),
        "cmd": str(payload.get("cmd") or "").upper(),
        "symbol": str(payload.get("symbol") or "").upper(),
        "lots": float(payload.get("lots", 0.0) or 0.0),
        "close_lots": float(payload.get("close_lots", 0.0) or 0.0),
        "tp_cash": payload.get("tp_cash"),
        "tp_price": payload.get("tp_price"),
        "sl_price": payload.get("sl_price"),
        "intent": str(payload.get("intent") or ""),
        "action": str(payload.get("action") or ""),
        "reversal_token": str(payload.get("reversal_token") or ""),
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def build_version_bundle(
    *,
    policy_version: str,
    model_bundle_version: str,
    orchestrator_version: str = ORCHESTRATOR_VERSION,
) -> VersionBundle:
    return VersionBundle(
        schema_version=ORCHESTRATION_SCHEMA_VERSION,
        policy_version=str(policy_version or ""),
        model_bundle_version=str(model_bundle_version or ""),
        orchestrator_version=str(orchestrator_version or ORCHESTRATOR_VERSION),
    )


def build_decision_context(
    *,
    pair: str,
    cycle_id: str,
    runtime_mode: str,
    tick: dict[str, Any] | None,
    feature_refs: dict[str, Any] | None,
    live_signal: dict[str, Any] | None,
    policy_state: dict[str, Any] | None,
    portfolio_state: dict[str, Any] | None,
    risk_envelope: dict[str, Any] | None,
    runtime_state: dict[str, Any] | None,
    version_bundle: VersionBundle,
    ts_utc: datetime | None = None,
    run_id: UUID | None = None,
) -> DecisionContext:
    timestamp = ts_utc.astimezone(UTC) if ts_utc is not None else datetime.now(UTC)
    thread_id = build_thread_id(pair=str(pair), cycle_id=str(cycle_id), runtime_mode=str(runtime_mode))
    correlation_id = build_correlation_id(thread_id=thread_id)
    return DecisionContext(
        run_id=run_id or uuid5(NAMESPACE_URL, correlation_id),
        cycle_id=str(cycle_id),
        thread_id=thread_id,
        correlation_id=correlation_id,
        ts_utc=timestamp,
        pair=str(pair).upper(),
        runtime_mode=str(runtime_mode),
        tick=dict(tick or {}),
        feature_refs=dict(feature_refs or {}),
        live_signal=dict(live_signal or {}),
        policy_state=dict(policy_state or {}),
        portfolio_state=dict(portfolio_state or {}),
        risk_envelope=dict(risk_envelope or {}),
        runtime_state=dict(runtime_state or {}),
        version_bundle=version_bundle,
    )
