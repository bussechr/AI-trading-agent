from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb


@lru_cache(maxsize=1)
def probe_xgb_cuda_capability() -> dict[str, object]:
    """Return whether this process can train an XGBoost model on CUDA."""
    try:
        features = np.array(
            [
                [0.1, 1.0, 0.0, 0.2],
                [0.2, 0.9, 0.1, 0.3],
                [0.8, 0.2, 0.9, 0.6],
                [0.9, 0.1, 0.8, 0.7],
                [0.3, 0.7, 0.2, 0.4],
                [0.7, 0.3, 0.7, 0.5],
            ],
            dtype=np.float32,
        )
        labels = np.array([0, 0, 1, 1, 0, 1], dtype=np.int32)
        model = xgb.XGBClassifier(
            objective="binary:logistic",
            n_estimators=4,
            max_depth=2,
            learning_rate=0.2,
            tree_method="hist",
            device="cuda",
        )
        model.fit(features, labels)
        return {"ok": True, "detail": "cuda_fit_ok"}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def normalize_xgb_device(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"cuda", "cpu", "auto"} else "auto"


def normalize_sample_weight(
    values: pd.Series | None, *, index: pd.Index
) -> np.ndarray | None:
    if values is None:
        return None
    weights = pd.Series(values, index=index).astype(float)
    weights = weights.replace([np.inf, -np.inf], np.nan).fillna(1.0)
    return weights.clip(lower=1e-6).to_numpy(dtype=float)


def build_xgb_runtime(
    *,
    requested_device: object,
    tree_method: object,
    allow_cpu_fallback: object,
    cuda_probe: Callable[[], dict[str, object]] = probe_xgb_cuda_capability,
) -> dict[str, object]:
    requested = normalize_xgb_device(requested_device)
    tree = str(tree_method or "hist").strip().lower() or "hist"
    allow_fallback = truthy(allow_cpu_fallback)

    if requested == "cpu":
        probe_result: dict[str, object] = {"ok": False, "detail": "not_requested"}
        selected = "cpu"
        note = ""
    else:
        probe_result = dict(cuda_probe())
        if bool(probe_result.get("ok")):
            selected = "cuda"
            note = ""
        elif requested == "cuda" and not allow_fallback:
            raise RuntimeError(
                f"XGBoost CUDA requested but unavailable: {probe_result.get('detail', '')}"
            )
        else:
            selected = "cpu"
            prefix = (
                "cuda_unavailable_fallback"
                if requested == "cuda"
                else "cuda_probe_failed"
            )
            note = f"{prefix}:{probe_result.get('detail', '')}"

    return {
        "requested_device": requested,
        "tree_method": tree,
        "allow_cpu_fallback": allow_fallback,
        "selected_device": selected,
        "used_device": selected,
        "inference_device": selected,
        "fallback_used": False,
        "fallback_reason": note,
        "cuda_probe": probe_result,
    }


def fit_xgb_estimator(
    estimator: Callable[..., Any],
    *,
    model_params: Mapping[str, object],
    X: pd.DataFrame,
    y: pd.Series,
    fit_kwargs: Mapping[str, object] | None,
    selected_device: object,
    allow_cpu_fallback: object,
) -> tuple[Any, str, bool, str]:
    """Fit on the selected device and enforce the configured fallback policy."""
    selected = str(selected_device or "cpu")
    allow_fallback = truthy(allow_cpu_fallback)
    attempts: list[tuple[str, str | None, bool]] = [("primary", selected, False)]
    if selected == "cuda":
        if allow_fallback:
            attempts.extend([("cpu_fallback", "cpu", True), ("legacy_cpu", None, True)])
    else:
        attempts.append(("legacy_cpu", None, True))

    errors: list[str] = []
    for name, device, is_fallback in attempts:
        params = dict(model_params)
        if device is None:
            params.pop("device", None)
        else:
            params["device"] = device
        try:
            fitted = estimator(**params)
            fitted.fit(X, y, **dict(fit_kwargs or {}))
            used_device = "cpu_legacy" if device is None else str(device)
            reason = f"{name}:{';'.join(errors)}" if is_fallback else ""
            return fitted, used_device, is_fallback, reason
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}:{exc}")
    raise RuntimeError("xgb_fit_failed:" + ";".join(errors))


def record_xgb_fit_runtime(
    runtime: dict[str, object],
    *,
    used_device: str,
    fallback_used: bool,
    fallback_reason: str,
) -> None:
    runtime["used_device"] = used_device
    runtime["inference_device"] = used_device
    if fallback_used:
        runtime["fallback_used"] = True
        runtime["fallback_reason"] = fallback_reason


def _uses_cuda(device: object) -> bool:
    return str(device or "").strip().lower().startswith("cuda")


def predict_xgb_probabilities(
    estimator: Any, X: pd.DataFrame, *, device: object
) -> np.ndarray:
    """Predict probabilities without triggering XGBoost's CPU/CUDA inplace fallback."""
    if not _uses_cuda(device):
        return np.asarray(estimator.predict_proba(X), dtype=float)
    raw = np.asarray(estimator.get_booster().predict(xgb.DMatrix(X)), dtype=float)
    if raw.ndim == 1:
        return np.column_stack((1.0 - raw, raw))
    return raw


def predict_xgb_values(
    estimator: Any, X: pd.DataFrame, *, device: object
) -> np.ndarray:
    """Predict scores without invoking CPU inplace prediction on a CUDA booster."""
    if not _uses_cuda(device):
        return np.asarray(estimator.predict(X))
    return np.asarray(estimator.get_booster().predict(xgb.DMatrix(X)))


def pin_xgb_cpu_inference(estimator: Any) -> None:
    """Pin a loaded estimator and its restored booster to CPU inference."""
    estimator.set_params(device="cpu")
    estimator.get_booster().set_param({"device": "cpu"})
