from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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
    loop_latency_ms = float((runtime_diag or {}).get("loop_latency_ms", 0.0) or 0.0)
    latency_budget = float(getattr(settings, "phase5_canary_latency_budget_ms", 0.0) or 0.0)
    stale_features = 1 if bool(dict(runtime_diag.get("feature_serving") or {}).get("stale", False)) else 0
    parity_breaches = int(dict(metrics.get("feature_parity") or {}).get("breaches") or 0)
    rollout_breaches = int(dict(runtime_diag.get("risk_cycle_summary") or {}).get("rollout_breach_count") or 0)
    top_concentration_share = float(
        dict((portfolio_telemetry or {}).get("concentration") or {}).get("top_symbol_share", 0.0) or 0.0
    )
    shadow_alignment = 1.0
    divergence_counts = dict(dict(runtime_diag.get("shadow_policy") or {}).get("divergenceCounts") or {})
    total_divergence = sum(int(value or 0) for value in divergence_counts.values())
    if total_divergence > 0:
        aligned = int(divergence_counts.get("agreeReady", 0) or 0) + int(divergence_counts.get("agreeBlocked", 0) or 0)
        shadow_alignment = float(aligned) / float(total_divergence)
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
        if top_concentration_share > float(getattr(settings, "capital_max_concentration_share", 1.0) or 1.0):
            reasons.append("portfolio_concentration")
        if shadow_alignment < float(getattr(settings, "capital_min_shadow_alignment_share", 0.0) or 0.0):
            reasons.append("shadow_alignment")
        if str(capital_band) == "paper":
            shadow_only = True
        if bool(reasons):
            entries_only = True
            if any(reason in {"rollout_breach", "parity_breach"} for reason in reasons):
                paused = True
    mode = "paused" if paused else ("entries_only" if entries_only else ("shadow_only" if shadow_only else "normal"))
    rollback_actions = [
        RollbackAction(action="feature_rollback", armed=bool("stale_features" in reasons), reason="stale_features", scope="features"),
        RollbackAction(action="model_rollback", armed=bool("parity_breach" in reasons), reason="parity_breach", scope="mlflow"),
        RollbackAction(action="execution_rollback", armed=bool(entries_only), reason="entries_only", scope="execution"),
        RollbackAction(action="global_rollback", armed=bool(paused), reason="governance_pause", scope="runtime"),
    ]
    budget_scale = capital_band_budget_scale(capital_band, settings)
    if entries_only or paused or shadow_only:
        budget_scale = min(float(budget_scale), 0.0 if shadow_only else float(budget_scale))
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
            "shadow_alignment_share": float(shadow_alignment),
            "provider_health": dict(provider_health or {}),
        },
    )
