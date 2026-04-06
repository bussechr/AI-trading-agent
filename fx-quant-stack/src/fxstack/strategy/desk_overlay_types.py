from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ConvictionBand = Literal["low", "medium", "high", "extreme"]
ThesisStage = Literal["scout", "core", "press", "harvest", "stand_down"]
PortfolioPosture = Literal[
    "capital_preservation",
    "balanced_probe",
    "constructive_rotation",
    "selective_press",
]
BudgetTilt = Literal["reduce", "neutral", "add", "concentrate"]


@dataclass(slots=True)
class SleevePolicyProfile:
    sleeve: str
    aggression_bias: float = 0.5
    press_min_conviction: float = 0.72
    press_min_confirmation: float = 0.60
    harvest_maturity: float = 0.68
    stand_down_fail_fast: float = 0.58


@dataclass(slots=True)
class DeskOverlayInputs:
    belief_metrics: dict[str, Any] = field(default_factory=dict)
    adaptive_playbook_metrics: dict[str, Any] = field(default_factory=dict)
    campaign_state: dict[str, Any] = field(default_factory=dict)
    sleeve_health: dict[str, Any] = field(default_factory=dict)
    crowding: dict[str, Any] = field(default_factory=dict)
    recent_performance: dict[str, Any] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DecisionTraceStage:
    stage: str
    score: float
    note: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SleeveBudgetGuidance:
    sleeve: str
    tilt: str = "neutral"
    target_share: float = 0.0
    max_share: float = 0.0
    min_share: float = 0.0
    reason: str = ""


@dataclass(slots=True)
class DeskOverlayDecision:
    conviction_score: float
    conviction_band: str
    thesis_stage: str
    portfolio_posture: str
    sleeve_budget_guidance: dict[str, SleeveBudgetGuidance] = field(default_factory=dict)
    replacement_urgency: float = 0.0
    policy_profile: SleevePolicyProfile | None = None
    trace: list[DecisionTraceStage] = field(default_factory=list)


DeskOverlayTraceStage = DecisionTraceStage
DeskOverlayOutput = DeskOverlayDecision
