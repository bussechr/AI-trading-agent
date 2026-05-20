"""Tests for the grouped config views on :class:`fxstack.settings.Settings`.

Pins two contracts:
* Each group is a frozen, hashable dataclass that projects from the flat
  ``FXSTACK_*`` fields.
* The projections honor env overrides — setting an env var changes both the
  flat attribute and the matching group attribute.
"""

from __future__ import annotations

import pytest

from fxstack.config_groups import (
    AgentRuntimeConfig,
    BridgeConfig,
    CanaryConfig,
    CapitalGovernanceConfig,
    FeastConfig,
    GatesConfig,
    PortfolioConfig,
    RLConfig,
    RiskCapsConfig,
)
from fxstack.settings import Settings


@pytest.fixture
def default_settings() -> Settings:
    """Defaults only — no .env override, no environment variables."""
    return Settings(_env_file=None)


def test_gates_view_projects_defaults(default_settings: Settings) -> None:
    gates = default_settings.gates
    assert isinstance(gates, GatesConfig)
    assert gates.min_swing_prob == pytest.approx(0.58)
    assert gates.min_entry_prob == pytest.approx(0.62)
    assert gates.min_trade_prob == pytest.approx(0.60)
    assert gates.use_uncertainty_gate is True


def test_risk_caps_view_projects_defaults(default_settings: Settings) -> None:
    caps = default_settings.risk_caps
    assert isinstance(caps, RiskCapsConfig)
    assert caps.max_pair_positions == 1
    assert caps.max_total_positions == 6
    assert caps.max_allowed_spread_bps == pytest.approx(3.0)
    assert caps.order_lot_step == pytest.approx(0.01)


def test_capital_view_projects_defaults(default_settings: Settings) -> None:
    cap = default_settings.capital
    assert isinstance(cap, CapitalGovernanceConfig)
    assert cap.band_mode == "paper"
    assert cap.max_drawdown_micro_live_pct == pytest.approx(3.0)
    assert cap.rollout_budget_scale_full_risk == pytest.approx(1.0)


def test_feast_view_projects_defaults(default_settings: Settings) -> None:
    feast = default_settings.feast
    assert isinstance(feast, FeastConfig)
    assert feast.enabled is False
    assert feast.online_stale_secs == pytest.approx(600.0)
    assert feast.push_worker_id == "feature-push-worker"


def test_agent_view_projects_defaults(default_settings: Settings) -> None:
    agent = default_settings.agent
    assert isinstance(agent, AgentRuntimeConfig)
    assert agent.mode == "off"
    assert agent.runtime == "langgraph"
    assert agent.require_human_approval is True


def test_rl_view_projects_defaults(default_settings: Settings) -> None:
    rl = default_settings.rl
    assert isinstance(rl, RLConfig)
    assert rl.online_worker_count == 4
    assert rl.supervised_fallback_required is True


def test_bridge_view_projects_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The repo-level conftest sets FXSTACK_BRIDGE_AUTH_REQUIRED=false to keep
    # API integration tests auth-free; for the defaults check we have to
    # clear that to actually see the default.
    monkeypatch.delenv("FXSTACK_BRIDGE_AUTH_REQUIRED", raising=False)
    s = Settings(_env_file=None)
    bridge = s.bridge
    assert isinstance(bridge, BridgeConfig)
    assert bridge.auth_required is True
    assert bridge.basket_tp_pct == pytest.approx(0.01)
    assert bridge.command_ttl_secs == pytest.approx(120.0)


def test_portfolio_view_projects_defaults(default_settings: Settings) -> None:
    pf = default_settings.portfolio
    assert isinstance(pf, PortfolioConfig)
    assert pf.corr_mode == "heuristic"
    assert pf.use_portfolio_ranking is True


def test_canary_view_projects_defaults(default_settings: Settings) -> None:
    canary = default_settings.canary
    assert isinstance(canary, CanaryConfig)
    assert canary.p95_overhead_ms == pytest.approx(250.0)
    assert canary.ack_success_floor == pytest.approx(0.995)
    assert canary.ramp_steps_pct == (1, 5, 10)


def test_groups_are_frozen(default_settings: Settings) -> None:
    """Grouped configs are read-only views; rebinding a field must raise."""
    gates = default_settings.gates
    with pytest.raises(Exception):  # frozen dataclass raises FrozenInstanceError
        gates.min_entry_prob = 0.99  # type: ignore[misc]


def test_env_override_flows_through_to_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flat field and the grouped view stay in sync under env overrides."""
    monkeypatch.setenv("FXSTACK_MIN_ENTRY_PROB", "0.77")
    monkeypatch.setenv("FXSTACK_BRIDGE_AUTH_REQUIRED", "false")
    monkeypatch.setenv("FXSTACK_BASKET_TP_PCT", "0.025")
    s = Settings(_env_file=None)
    assert s.min_entry_prob == pytest.approx(0.77)
    assert s.gates.min_entry_prob == pytest.approx(0.77)
    assert s.bridge_auth_required is False
    assert s.bridge.auth_required is False
    assert s.bridge.basket_tp_pct == pytest.approx(0.025)


def test_canary_ramp_steps_pct_uses_settings_property(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CanaryConfig consumes Settings' parsed list, not the raw CSV string."""
    monkeypatch.setenv("FXSTACK_PHASE6B_CANARY_RAMP_STEPS_PCT", "2, 7, 12, 25")
    s = Settings(_env_file=None)
    assert s.canary.ramp_steps_pct == (2, 7, 12, 25)
