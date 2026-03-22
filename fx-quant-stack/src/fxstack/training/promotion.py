from __future__ import annotations

from dataclasses import dataclass

from fxstack.settings import get_settings


@dataclass(slots=True)
class PromotionThresholds:
    min_cv_score: float
    min_wf_score: float
    max_calibration_error: float
    min_delta: float
    throughput_floor: float


def default_thresholds() -> PromotionThresholds:
    s = get_settings()
    return PromotionThresholds(
        min_cv_score=float(s.promotion_min_cv_score),
        min_wf_score=float(s.promotion_min_wf_score),
        max_calibration_error=float(s.promotion_max_calibration_error),
        min_delta=float(s.promotion_min_delta),
        throughput_floor=float(s.throughput_floor),
    )


def evaluate_promotion(
    *,
    report: dict,
    champion_metric: float = 0.0,
    thresholds: PromotionThresholds | None = None,
    policy: str | None = None,
) -> dict[str, object]:
    thr = thresholds or default_thresholds()
    policy_name = str(policy or get_settings().promotion_policy)

    cv_score = float(report.get("cv_score", 0.0) or 0.0)
    wf_score = float(report.get("wf_score", 0.0) or 0.0)
    calibration_error = float(report.get("calibration_error", 1.0) or 1.0)
    candidate_metric = float(report.get("candidate_metric", cv_score) or 0.0)
    throughput = float(report.get("throughput", 0.0) or 0.0)
    delta = float(candidate_metric - float(champion_metric))

    gates = {
        "cv": cv_score >= float(thr.min_cv_score),
        "wf": wf_score >= float(thr.min_wf_score),
        "calibration": calibration_error <= float(thr.max_calibration_error),
        "delta": delta >= float(thr.min_delta),
        "throughput": throughput >= float(thr.throughput_floor),
    }
    status = "eligible" if all(gates.values()) else "research_only"
    return {
        "status": status,
        "policy": policy_name,
        "candidate_metric": candidate_metric,
        "champion_metric": float(champion_metric),
        "delta": delta,
        "gates": gates,
        "thresholds": {
            "min_cv_score": float(thr.min_cv_score),
            "min_wf_score": float(thr.min_wf_score),
            "max_calibration_error": float(thr.max_calibration_error),
            "min_delta": float(thr.min_delta),
            "throughput_floor": float(thr.throughput_floor),
        },
    }
