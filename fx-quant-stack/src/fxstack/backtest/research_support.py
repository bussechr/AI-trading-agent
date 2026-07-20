"""Pure support contracts for offline research backtests.

This module deliberately duplicates a small amount of production behavior. It
must stay free of runtime, bridge, API, database, provider-execution, and
environment-backed settings imports so causal research can run in isolation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.live.scorer import LiveScorer
from fxstack.mlops.model_uri import normalize_artifact_ref, resolve_model_artifact_path
from fxstack.models.artifact_contract import validate_artifact_contract_read_only


@dataclass(slots=True)
class ResearchModelSet:
    pair: str
    model_set_id: str
    registry_path: str
    scorer: LiveScorer
    swing_router: "PolicyModelRouter"
    intraday_router: "PolicyModelRouter"
    exit_model: Any | None
    reversal_failure_model: Any | None
    reversal_opportunity_model: Any | None
    belief_model: Any | None
    exit_action_labels: dict[int, str]
    lifecycle_activation_mode: str
    has_exit_model: bool
    has_reversal_models: bool
    has_directional_belief: bool
    swing_shadow_model: Any | None = None
    intraday_shadow_model: Any | None = None
    shadow_bundle_run_id: str = ""
    shadow_component_refs: dict[str, Any] = field(default_factory=dict)
    component_feature_services: dict[str, Any] = field(default_factory=dict)
    rollout_policy: dict[str, Any] = field(default_factory=dict)
    rl_checkpoint_path: str = ""
    rl_checkpoint_content_sha256: str = ""


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    return float(out) if math.isfinite(out) else float(default)


def resolve_optional_path(raw: str, project_root: Path) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    variants = list(dict.fromkeys((text, text.replace("\\", "/"))))
    for value in variants:
        path = Path(value).expanduser()
        for candidate in (path, project_root / path, project_root.parent / path):
            if candidate.exists():
                return candidate.resolve()
    return None


def artifact_path(raw: Any) -> str:
    ref = normalize_artifact_ref(raw)
    return str(ref.get("path") or ref.get("model_uri") or "")


def artifact_value(artifacts: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = artifact_path(artifacts.get(key))
        if value.strip():
            return value
    return ""


def artifact_ref_value(artifacts: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        raw_ref = artifacts.get(key)
        if artifact_path(raw_ref).strip():
            return raw_ref
    return ""


def load_artifact_meta(raw_path: Any, project_root: Path) -> dict[str, Any]:
    ref = normalize_artifact_ref(raw_path)
    if not str(ref.get("path") or ref.get("model_uri") or "").strip():
        return {}
    expected_digest = (
        str(ref.get("artifact_hash") or "").strip().lower()
        if isinstance(raw_path, dict)
        else None
    )
    path = resolve_model_artifact_path(raw_path, project_root=project_root)
    label = f"research_artifact_meta:{path}"
    return validate_artifact_contract_read_only(
        path,
        label=label,
        expected_digest=expected_digest,
    )


def required_model_feature_columns(*models: Any) -> list[str]:
    columns: list[str] = []
    for model in models:
        for column in list(getattr(model, "feature_columns", []) or []):
            name = str(column or "").strip()
            if name and name not in columns:
                columns.append(name)
    return columns


def exit_action_labels(exit_meta: dict[str, Any], classes: list[int] | None) -> dict[int, str]:
    ordered = ["hold", "partial_tp", "exit"]
    class_ids = [int(value) for value in list(classes or [])] or [0, 1, 2]
    labels = {
        class_id: ordered[index] if index < len(ordered) else f"class_{class_id}"
        for index, class_id in enumerate(class_ids)
    }
    collapse = dict(exit_meta.get("exit_action_collapse") or {})
    collapsed_actions = list(dict(collapse.get("class_balance_after") or {}).keys())
    if collapsed_actions and len(collapsed_actions) == len(class_ids):
        labels.update(
            {class_id: str(collapsed_actions[index]) for index, class_id in enumerate(class_ids)}
        )
    return labels


class PolicyModelRouter:
    def __init__(
        self,
        *,
        policy: str,
        family: str,
        primary_name: str,
        primary_model: Any | None,
        fallback_name: str,
        fallback_model: Any | None,
    ) -> None:
        self.policy = str(policy)
        self.family = str(family)
        self.primary_name = str(primary_name)
        self.primary_model = primary_model
        self.fallback_name = str(fallback_name)
        self.fallback_model = fallback_model
        self.last_selected_model = ""
        self.last_fallback_reason = ""

    @property
    def feature_columns(self) -> list[str]:
        return required_model_feature_columns(self.primary_model, self.fallback_model)

    def predict_proba(self, frame: pd.DataFrame) -> Any:
        self.last_selected_model = ""
        self.last_fallback_reason = ""
        primary_error = ""
        if self.primary_model is not None:
            try:
                output = self.primary_model.predict_proba(frame)
                self.last_selected_model = self.primary_name
                return output
            except Exception as exc:
                primary_error = f"{self.primary_name}_inference_error:{type(exc).__name__}"
                self.last_fallback_reason = primary_error
        if self.fallback_model is not None:
            try:
                output = self.fallback_model.predict_proba(frame)
                self.last_selected_model = self.fallback_name
                if not self.last_fallback_reason:
                    self.last_fallback_reason = f"{self.primary_name}_missing"
                return output
            except Exception as exc:
                detail = f"{self.fallback_name}_inference_error:{type(exc).__name__}"
                if self.last_fallback_reason:
                    detail = f"{self.last_fallback_reason};{detail}"
                raise RuntimeError(f"{self.family} routing failed: {detail}") from exc
        if primary_error:
            raise RuntimeError(f"{self.family} routing failed: {primary_error}")
        raise RuntimeError(f"{self.family} routing failed: no_available_model")

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        probabilities = self.predict_proba(frame)
        return (probabilities["p1"] >= 0.5).astype(int)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "selected_model": self.last_selected_model,
            "used_fallback": bool(
                self.last_selected_model and self.last_selected_model != self.primary_name
            ),
            "fallback_reason": self.last_fallback_reason or "none",
        }


def safe_load_model(model_cls: Any, raw_path: Any, project_root: Path) -> tuple[Any | None, str]:
    value = artifact_path(raw_path).strip()
    if not value:
        return None, "missing_path"
    try:
        ref = normalize_artifact_ref(raw_path)
        expected_digest = (
            str(ref.get("artifact_hash") or "").strip().lower()
            if isinstance(raw_path, dict)
            else None
        )
        expected_name = str(
            getattr(model_cls, "name", getattr(model_cls, "__name__", "")) or ""
        ).strip()
        path = resolve_model_artifact_path(raw_path, project_root=project_root)
        label = f"{expected_name or 'research_model'}:{path}"
        validate_artifact_contract_read_only(
            path,
            label=label,
            expected_digest=expected_digest,
            expected_name=expected_name or None,
        )
        model = model_cls.load(path)
        validate_artifact_contract_read_only(
            path,
            label=label,
            expected_digest=expected_digest,
            expected_name=expected_name or None,
        )
        return model, ""
    except Exception as exc:
        return None, f"load_error:{type(exc).__name__}"


def round_lot_size(*, lots: float, min_lot: float, lot_step: float, max_lot: float) -> float:
    step = max(1e-9, float(lot_step))
    minimum = max(0.0, float(min_lot))
    maximum = max(0.0, float(max_lot))
    quantized = math.floor((max(0.0, float(lots)) / step) + 1e-9) * step
    quantized = max(minimum, quantized)
    if maximum > 0.0:
        quantized = min(maximum, quantized)
    decimals = max(0, int(round(-math.log10(step)))) if step < 1.0 else 0
    return round(float(quantized), decimals)


def entry_order_lots(*, state: dict[str, Any], settings: Any, equity_seed: float) -> tuple[float, dict[str, Any]]:
    current_equity = _safe_float(state.get("equity", 0.0), 0.0)
    equity_value = current_equity if current_equity > 0.0 else _safe_float(equity_seed, 0.0)
    coefficient = max(0.0, _safe_float(getattr(settings, "equity_lots_per_usd", 0.0), 0.0))
    if equity_value > 0.0 and coefficient > 0.0:
        raw_lots = equity_value * coefficient
        sizing_mode = "equity_scaled"
    else:
        raw_lots = max(0.0, _safe_float(getattr(settings, "default_order_lots", 0.0), 0.0))
        sizing_mode = "fixed_default"
    rounded_lots = round_lot_size(
        lots=raw_lots,
        min_lot=max(0.0, _safe_float(getattr(settings, "min_order_lots", 0.01), 0.01)),
        lot_step=max(1e-9, _safe_float(getattr(settings, "order_lot_step", 0.01), 0.01)),
        max_lot=max(0.0, _safe_float(getattr(settings, "max_order_lots", 0.0), 0.0)),
    )
    return rounded_lots, {
        "mode": sizing_mode,
        "equity": float(equity_value),
        "coefficient": float(coefficient),
        "raw_lots": float(raw_lots),
        "rounded_lots": float(rounded_lots),
    }


def partial_close_plan(*, lots_open: float, fraction: float, settings: Any) -> tuple[str, float]:
    open_lots = max(0.0, float(lots_open))
    close_fraction = max(0.0, float(fraction))
    if open_lots <= 0.0 or close_fraction <= 0.0:
        return "hold", 0.0
    min_lot = max(0.0, _safe_float(getattr(settings, "min_order_lots", 0.01), 0.01))
    lot_step = max(1e-9, _safe_float(getattr(settings, "order_lot_step", 0.01), 0.01))
    rounded_close = round_lot_size(
        lots=open_lots * close_fraction,
        min_lot=min_lot,
        lot_step=lot_step,
        max_lot=open_lots,
    )
    tolerance = max(1e-9, lot_step / 10.0)
    remaining_lots = max(0.0, open_lots - rounded_close)
    if rounded_close <= 0.0:
        return "hold", 0.0
    if rounded_close >= (open_lots - tolerance) or 0.0 < remaining_lots < (min_lot - tolerance):
        return "exit", round(float(open_lots), 8)
    return "partial_tp", round(float(rounded_close), 8)


def timeframe_to_seconds(timeframe: str) -> int:
    text = str(timeframe or "").strip().upper()
    if text == "D":
        return 86_400
    if text == "W":
        return 604_800
    if text in {"MN", "MN1"}:
        return 2_592_000
    if not text:
        return 0
    try:
        magnitude = int(text[1:] or "1")
    except Exception:
        return 0
    scale = {"S": 1, "M": 60, "H": 3_600, "D": 86_400}.get(text[:1], 0)
    return int(magnitude * scale) if scale else 0
