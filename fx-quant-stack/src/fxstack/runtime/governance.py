from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

_MARKET_PRESSURE_DEGRADE_THRESHOLD = 0.35
_MARKET_PRESSURE_ENTRY_THRESHOLD = 0.75
_CONCENTRATION_SOFT_LIMIT = 0.45
_CONCENTRATION_HARD_LIMIT = 0.75
_CORRELATION_SOFT_LIMIT = 0.45
_CORRELATION_HARD_LIMIT = 0.80
_EXPOSURE_SOFT_LIMIT = 0.25
_EXPOSURE_HARD_LIMIT = 0.60
_SHADOW_ALIGNMENT_MIN = 0.25


@dataclass(slots=True)
class ProviderHealthSnapshot:
    provider: str
    role: str
    status: str = "unknown"
    freshness_secs: float | None = None
    latency_ms: float | None = None
    missing_rate: float = 0.0
    fallback_mode: str = ""
    reason: str = ""
    provenance: str = ""
    shadow_only: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RollbackAction:
    action: str
    armed: bool = False
    reason: str = ""
    scope: str = "runtime"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CapitalGovernanceState:
    capital_band: str = "paper"
    mode: str = "normal"
    paused: bool = False
    entries_only: bool = False
    shadow_only: bool = False
    budget_scale: float = 1.0
    reasons: list[str] = field(default_factory=list)
    eligible_for_upgrade: bool = False
    rollback_actions: list[RollbackAction] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rollback_actions"] = [item.to_dict() for item in self.rollback_actions]
        return payload


def capital_band_budget_scale(capital_band: str, settings: Any) -> float:
    band = str(capital_band or "paper").strip().lower()
    if band == "micro_live":
        return float(getattr(settings, "capital_rollout_budget_scale_micro_live", 0.1) or 0.1)
    if band == "low_risk_live":
        return float(getattr(settings, "capital_rollout_budget_scale_low_risk", 0.25) or 0.25)
    if band == "full_risk_live":
        return float(getattr(settings, "capital_rollout_budget_scale_full_risk", 1.0) or 1.0)
    return 0.0 if band == "shadow_only" else 1.0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return float(default)
    return float(default) if number != number else float(number)


def _normalized_excess(value: float, soft_limit: float, hard_limit: float) -> float:
    value = max(0.0, float(value))
    soft = max(0.0, float(soft_limit))
    hard = max(float(hard_limit), soft + 1e-9)
    if value <= soft:
        return 0.0
    if value >= hard:
        return 1.0
    return float((value - soft) / (hard - soft))


def compute_capital_governance_state(
    *,
    settings: Any,
    runtime_diag: dict[str, Any],
    metrics: dict[str, Any],
    portfolio_telemetry: dict[str, Any] | None = None,
    provider_health: dict[str, Any] | None = None,
) -> CapitalGovernanceState:
    capital_band = str(getattr(settings, "capital_band_mode", "paper") or "paper").strip().lower()
    governance_enabled = bool(getattr(settings, "capital_governance_enabled", False))
    reasons: list[str] = []
    paused = False
    entries_only = bool(getattr(settings, "capital_entries_only", False))
    shadow_only = bool(getattr(settings, "provider_shadow_only", False))
    runtime_diag = dict(runtime_diag or {})
    metrics = dict(metrics or {})
    portfolio = dict(portfolio_telemetry or {})
    concentration = dict(portfolio.get("concentration") or {})
    correlation = dict(portfolio.get("correlation") or {})
    budget = dict(portfolio.get("budget") or {})
    loop_latency_ms = _safe_float(runtime_diag.get("loop_latency_ms", 0.0), 0.0)
    latency_budget = float(getattr(settings, "phase5_canary_latency_budget_ms", 0.0) or 0.0)
    stale_features = 1 if bool(dict(runtime_diag.get("feature_serving") or {}).get("stale", False)) else 0
    parity_breaches = int(dict(metrics.get("feature_parity") or {}).get("breaches") or 0)
    rollout_breaches = int(dict(runtime_diag.get("risk_cycle_summary") or {}).get("rollout_breach_count") or 0)
    top_concentration_share = _safe_float(concentration.get("top_symbol_share", 0.0), 0.0)
    top_currency_share = _safe_float(concentration.get("top_currency_share", 0.0), 0.0)
    symbol_hhi = _safe_float(concentration.get("symbol_hhi", 0.0), 0.0)
    currency_hhi = _safe_float(concentration.get("currency_hhi", 0.0), 0.0)
    concentration_hard_limit = _safe_float(
        getattr(settings, "capital_max_concentration_share", _CONCENTRATION_HARD_LIMIT),
        _CONCENTRATION_HARD_LIMIT,
    )
    concentration_soft_limit = min(_CONCENTRATION_SOFT_LIMIT, max(0.0, concentration_hard_limit - 0.15))
    gross_exposure = abs(_safe_float(portfolio.get("gross_exposure", 0.0), 0.0))
    gross_lot_exposure = abs(_safe_float(portfolio.get("gross_lot_exposure", 0.0), 0.0))
    net_exposure = abs(_safe_float(portfolio.get("net_exposure", 0.0), 0.0))
    net_lot_exposure = abs(_safe_float(portfolio.get("net_lot_exposure", 0.0), 0.0))
    net_exposure_share = float(net_exposure / gross_exposure) if gross_exposure > 0.0 else 0.0
    net_lot_exposure_share = float(net_lot_exposure / gross_lot_exposure) if gross_lot_exposure > 0.0 else 0.0
    correlation_method = str(budget.get("correlation_method") or correlation.get("method") or "heuristic").strip().lower()
    correlation_sample_count = int(budget.get("correlation_sample_count") or correlation.get("sample_count") or 0)
    correlation_window_bars = int(correlation.get("window_bars") or 0)
    correlation_min_obs = int(correlation.get("min_obs") or 0)
    max_abs_corr = _safe_float(correlation.get("max_abs_corr", 0.0), 0.0)
    avg_abs_corr = _safe_float(correlation.get("avg_abs_corr", 0.0), 0.0)
    configured_corr_hard_limit = _safe_float(
        getattr(settings, "capital_max_realized_corr_share", _CORRELATION_HARD_LIMIT),
        _CORRELATION_HARD_LIMIT,
    )
    configured_corr_soft_limit = min(_CORRELATION_SOFT_LIMIT, max(0.0, configured_corr_hard_limit - 0.20))
    correlation_strength = max(0.0, min(1.0, 0.65 * max_abs_corr + 0.35 * avg_abs_corr))
    if correlation_method in {"realized", "hybrid"} and correlation_sample_count > 0:
        confidence_denominator = max(int(correlation_window_bars or correlation_min_obs or 8), 8)
        confidence = min(1.0, float(correlation_sample_count) / float(confidence_denominator))
        correlation_strength *= confidence
    else:
        correlation_strength *= 0.85
    concentration_strength = max(top_concentration_share, top_currency_share, symbol_hhi, currency_hhi)
    exposure_strength = max(net_exposure_share, net_lot_exposure_share)
    session_peak_share = _safe_float(concentration.get("session_peak_share", 0.0), 0.0)
    sleeve_peak_share = _safe_float(concentration.get("sleeve_peak_share", 0.0), 0.0)
    session_penalty = _safe_float(budget.get("session_penalty", 0.0), 0.0)
    resize_pressure = _safe_float(budget.get("resize_pressure", 0.0), 0.0)
    flip_pressure = _safe_float(budget.get("flip_pressure", 0.0), 0.0)
    rebalance_pressure = _safe_float(budget.get("rebalance_pressure", 0.0), 0.0)
    concentration_stress = _safe_float(budget.get("concentration_stress", concentration_strength), concentration_strength)
    currency_stress = _safe_float(budget.get("currency_stress", max(top_currency_share, currency_hhi)), max(top_currency_share, currency_hhi))
    session_stress = _safe_float(budget.get("session_stress", max(session_peak_share, session_penalty)), max(session_peak_share, session_penalty))
    correlation_pressure = _normalized_excess(correlation_strength, configured_corr_soft_limit, configured_corr_hard_limit)
    concentration_pressure = _normalized_excess(concentration_strength, concentration_soft_limit, concentration_hard_limit)
    exposure_pressure = _normalized_excess(exposure_strength, _EXPOSURE_SOFT_LIMIT, _EXPOSURE_HARD_LIMIT)
    market_pressure = max(correlation_pressure, concentration_pressure, exposure_pressure)
    shadow_alignment = 1.0
    divergence_counts = dict(dict(runtime_diag.get("shadow_policy") or {}).get("divergenceCounts") or {})
    total_divergence = sum(int(value or 0) for value in divergence_counts.values())
    if total_divergence > 0:
        aligned = int(divergence_counts.get("agreeReady", 0) or 0) + int(divergence_counts.get("agreeBlocked", 0) or 0)
        shadow_alignment = float(aligned) / float(total_divergence)
    effective_market_pressure = market_pressure if governance_enabled else 0.0
    if governance_enabled:
        latency_breaches = 1 if latency_budget > 0.0 and loop_latency_ms > latency_budget else 0
        if latency_breaches > int(getattr(settings, "capital_max_latency_breach_count", 0) or 0):
            reasons.append("latency_breach")
        if stale_features > int(getattr(settings, "capital_max_stale_feature_count", 0) or 0):
            reasons.append("stale_features")
        if parity_breaches > int(getattr(settings, "capital_max_operational_fault_count", 0) or 0):
            reasons.append("parity_breach")
        if rollout_breaches > 0:
            reasons.append("rollout_breach")
        if shadow_alignment < max(_SHADOW_ALIGNMENT_MIN, float(getattr(settings, "capital_min_shadow_alignment_share", 0.0) or 0.0)):
            reasons.append("shadow_alignment")
        if effective_market_pressure >= _MARKET_PRESSURE_ENTRY_THRESHOLD:
            if correlation_pressure > 0.0:
                reasons.append("realized_correlation" if correlation_method in {"realized", "hybrid"} else "heuristic_correlation")
            if concentration_pressure > 0.0:
                reasons.append("portfolio_concentration")
            if exposure_pressure > 0.0:
                reasons.append("net_exposure_imbalance")
            reasons.append("market_pressure_high")
            entries_only = True
        elif effective_market_pressure >= _MARKET_PRESSURE_DEGRADE_THRESHOLD:
            if correlation_pressure > 0.0:
                reasons.append("realized_correlation" if correlation_method in {"realized", "hybrid"} else "heuristic_correlation")
            if concentration_pressure > 0.0:
                reasons.append("portfolio_concentration")
            if exposure_pressure > 0.0:
                reasons.append("net_exposure_imbalance")
            reasons.append("market_pressure_degraded")
        if str(capital_band) == "paper":
            shadow_only = True
        operational_faults = {"latency_breach", "stale_features", "parity_breach", "rollout_breach", "shadow_alignment"}
        if any(reason in operational_faults for reason in reasons):
            paused = True
        if paused:
            entries_only = True
    mode = (
        "paused"
        if paused
        else (
            "shadow_only"
            if shadow_only
            else ("entries_only" if entries_only else ("degraded" if effective_market_pressure >= _MARKET_PRESSURE_DEGRADE_THRESHOLD else "normal"))
        )
    )
    rollback_actions = [
        RollbackAction(action="feature_rollback", armed=bool("stale_features" in reasons), reason="stale_features", scope="features"),
        RollbackAction(action="model_rollback", armed=bool("parity_breach" in reasons), reason="parity_breach", scope="mlflow"),
        RollbackAction(action="execution_rollback", armed=bool(entries_only), reason="entries_only", scope="execution"),
        RollbackAction(action="global_rollback", armed=bool(paused), reason="governance_pause", scope="runtime"),
    ]
    budget_scale = capital_band_budget_scale(capital_band, settings)
    if not paused and not shadow_only:
        if effective_market_pressure >= _MARKET_PRESSURE_ENTRY_THRESHOLD:
            budget_scale *= 0.45
        elif effective_market_pressure >= _MARKET_PRESSURE_DEGRADE_THRESHOLD:
            budget_scale *= 0.80
    if paused or shadow_only:
        budget_scale = 0.0
    budget_scale = max(0.0, min(1.0, float(budget_scale)))
    return CapitalGovernanceState(
        capital_band=capital_band,
        mode=mode,
        paused=bool(paused),
        entries_only=bool(entries_only),
        shadow_only=bool(shadow_only),
        budget_scale=float(budget_scale),
        reasons=list(reasons),
        eligible_for_upgrade=bool(not reasons and capital_band in {"micro_live", "low_risk_live", "full_risk_live"}),
        rollback_actions=rollback_actions,
        metrics={
            "loop_latency_ms": float(loop_latency_ms),
            "latency_budget_ms": float(latency_budget),
            "feature_parity_breaches": int(parity_breaches),
            "stale_feature_count": int(stale_features),
            "rollout_breach_count": int(rollout_breaches),
            "top_concentration_share": float(top_concentration_share),
            "top_currency_share": float(top_currency_share),
            "symbol_hhi": float(symbol_hhi),
            "currency_hhi": float(currency_hhi),
            "net_exposure_share": float(net_exposure_share),
            "net_lot_exposure_share": float(net_lot_exposure_share),
            "correlation_method": str(correlation_method),
            "correlation_sample_count": int(correlation_sample_count),
            "correlation_window_bars": int(correlation_window_bars),
            "correlation_min_obs": int(correlation_min_obs),
            "correlation_strength": float(correlation_strength),
            "correlation_soft_limit": float(configured_corr_soft_limit),
            "correlation_hard_limit": float(configured_corr_hard_limit),
            "correlation_pressure": float(correlation_pressure),
            "concentration_pressure": float(concentration_pressure),
            "exposure_pressure": float(exposure_pressure),
            "session_peak_share": float(session_peak_share),
            "sleeve_peak_share": float(sleeve_peak_share),
            "session_penalty": float(session_penalty),
            "resize_pressure": float(resize_pressure),
            "flip_pressure": float(flip_pressure),
            "rebalance_pressure": float(rebalance_pressure),
            "concentration_stress": float(concentration_stress),
            "currency_stress": float(currency_stress),
            "session_stress": float(session_stress),
            "market_pressure": float(market_pressure),
            "shadow_alignment_share": float(shadow_alignment),
            "provider_health": dict(provider_health or {}),
        },
    )
