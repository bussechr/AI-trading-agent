from __future__ import annotations

import os
from pathlib import Path

from fxstack.risk import MarketState, PolicyIntent, PortfolioState, RiskKernelConfig, evaluate_risk_decision
from fxstack.runtime.db_tools import migrate_database
from fxstack.runtime.runner import _resolve_main_runtime_rollout_policy, _risk_cycle_summary
from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings


def _fresh_service(tmp_path: Path) -> RuntimeService:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    os.environ["FXSTACK_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    out = migrate_database(database_url=database_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    get_settings.cache_clear()
    return RuntimeService(database_url=database_url)


def test_resolve_main_runtime_rollout_policy_prefers_explicit_canary_metadata() -> None:
    rollout = _resolve_main_runtime_rollout_policy(
        pair="EURUSD",
        metadata={
            "phase5_rollout": {
                "mode": "canary",
                "enabled": True,
                "allowlisted_pairs": ["EURUSD"],
                "budget_scale": 0.4,
                "max_total_positions": 2,
            },
            "phase5_gate_bundle": {
                "canary_gate": {"passed": True},
            },
        },
    )

    assert rollout["configured"] is True
    assert rollout["active"] is True
    assert rollout["pair_allowlisted"] is True
    assert rollout["budget_scale"] == 0.4
    assert rollout["max_total_positions"] == 2
    assert rollout["source"] == "phase5_rollout"


def test_risk_kernel_reduces_canary_entry_budget_for_allowlisted_pair() -> None:
    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="EURUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.71,
            confidence=0.71,
            expected_edge_bps=8.0,
            metadata={"requested_lots": 0.40, "policy_allowed": True},
        ),
        market_state=MarketState(
            pair="EURUSD",
            ts="2026-04-07T12:00:00Z",
            spread_bps=1.1,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=True,
        ),
        portfolio_state=PortfolioState(
            equity=15000.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
        config=RiskKernelConfig(
            max_total_positions=6,
            max_pair_positions=1,
            min_lots=0.01,
            lot_step=0.01,
            rollout_mode="canary",
            rollout_pair_allowlisted=True,
            rollout_budget_scale=0.25,
        ),
    )

    rollout = dict(decision.metadata.get("rollout") or {})
    assert decision.verdict == "allow"
    assert decision.approved_order is not None
    assert abs(decision.approved_order.lots - 0.1) < 1e-9
    assert rollout["active"] is True
    assert rollout["reduced_budget"] is True
    assert rollout["breach"] is True
    assert rollout["breach_reason"] == "rollout_budget_reduced"
    assert any(item.rule == "rollout_canary" and item.verdict == "reduce" for item in decision.trace)


def test_risk_kernel_blocks_canary_pair_when_not_allowlisted() -> None:
    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="GBPUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.68,
            confidence=0.68,
            expected_edge_bps=6.0,
            metadata={"requested_lots": 0.12, "policy_allowed": True},
        ),
        market_state=MarketState(
            pair="GBPUSD",
            ts="2026-04-07T12:05:00Z",
            spread_bps=1.2,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=True,
        ),
        portfolio_state=PortfolioState(
            equity=12000.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
        config=RiskKernelConfig(
            max_total_positions=6,
            max_pair_positions=1,
            min_lots=0.01,
            lot_step=0.01,
            rollout_mode="canary",
            rollout_pair_allowlisted=False,
            rollout_budget_scale=0.25,
        ),
    )

    rollout = dict(decision.metadata.get("rollout") or {})
    assert decision.verdict == "block"
    assert decision.reason == "rollout_pair_not_allowlisted"
    assert decision.approved_order is None
    assert rollout["active"] is False
    assert rollout["breach"] is True
    assert rollout["breach_reason"] == "rollout_pair_not_allowlisted"


def test_risk_cycle_summary_and_metrics_surface_rollout_state(tmp_path: Path) -> None:
    summary = _risk_cycle_summary(
        decisions=[
            {
                "symbol": "EURUSD",
                "execution_ready": True,
                "metadata": {
                    "pair": "EURUSD",
                    "lifecycle_action": "entry",
                    "risk_verdict": "allow",
                    "risk_reason": "approved",
                    "approved_order": {"cmd": "BUY"},
                    "risk_trace": [{"rule": "rollout_canary"}],
                    "rollout": {
                        "mode": "canary",
                        "active": True,
                        "pair_allowlisted": True,
                        "budget_scale": 0.25,
                        "reduced_budget": True,
                        "breach": True,
                        "breach_reason": "rollout_budget_reduced",
                    },
                },
            }
        ]
    )
    assert summary["rollout_active_count"] == 1
    assert summary["rollout_reduced_budget_count"] == 1
    assert summary["rollout_breach_count"] == 1
    assert summary["rollout"]["dominant_breach_reason"] == "rollout_budget_reduced"

    service = _fresh_service(tmp_path)
    service.patch_state(
        {
            "runtime_diag": {
                "rollout_policy": {
                    "configured_pairs": ["EURUSD"],
                    "active_pairs": ["EURUSD"],
                    "pair_budget_scale": {"EURUSD": 0.25},
                },
                "rollout_summary": dict(summary.get("rollout") or {}),
                "risk_cycle_summary": dict(summary),
            }
        }
    )
    metrics = service.get_metrics()
    assert metrics["rollout"]["active_pairs"] == ["EURUSD"]
    assert metrics["rollout"]["policy"]["pair_budget_scale"]["EURUSD"] == 0.25
    assert metrics["rollout"]["breach_count"] == 1
