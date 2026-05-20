from __future__ import annotations

from types import SimpleNamespace

import fxstack.runtime.runner as runtime_runner


class _FakeDecision:
    verdict = "allow"
    reason = "ok"
    lifecycle_action = "entry"
    close_lots = 0.0
    final_lots = 0.0
    approved_order = None
    metadata = {"rollout": {}}
    trace: list[object] = []

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


def test_runtime_risk_kernel_uses_scorer_uncertainty_for_portfolio_allocation(monkeypatch) -> None:
    captured: dict[str, float] = {}

    class _FakeBudget:
        budget_scale = 1.0
        reason = "ok"

    class _FakeAllocation:
        allowed = True
        budget = _FakeBudget()
        book = SimpleNamespace(gross_exposure=0.0, net_exposure=0.0, to_dict=lambda: {})
        concentration = SimpleNamespace(to_dict=lambda: {})
        correlation = SimpleNamespace(to_dict=lambda: {})
        stress = SimpleNamespace(to_dict=lambda: {})
        telemetry = {}

        def to_dict(self) -> dict[str, object]:
            return {
                "allowed": self.allowed,
                "budget": {"budget_scale": self.budget.budget_scale, "reason": self.budget.reason},
                "book": self.book.to_dict(),
                "concentration": self.concentration.to_dict(),
                "correlation": self.correlation.to_dict(),
                "stress": self.stress.to_dict(),
                "telemetry": dict(self.telemetry),
            }

    def _fake_evaluate_portfolio_allocation(*, uncertainty_score, **kwargs):
        captured["uncertainty_score"] = float(uncertainty_score)
        return _FakeAllocation()

    def _fake_evaluate_risk_decision(*, policy_intent, market_state, portfolio_state, config):
        return _FakeDecision()

    monkeypatch.setattr(runtime_runner, "evaluate_portfolio_allocation", _fake_evaluate_portfolio_allocation)
    # Risk kernel is now invoked through fxstack.risk.envelope; patch at the
    # new seam so the fake substitution still takes effect.
    import fxstack.risk.envelope as risk_envelope
    monkeypatch.setattr(risk_envelope, "evaluate_risk_decision", _fake_evaluate_risk_decision)

    out = runtime_runner._evaluate_runtime_risk_kernel(
        pair="EURUSD",
        ts_value="2026-04-07T12:00:00Z",
        side="BUY",
        signal=SimpleNamespace(trade_prob=0.22, uncertainty_score=0.17, session_bucket="london", reversal_ready=False),
        expected_edge_bps=8.0,
        spread_bps=1.2,
        feature_bar={"stale_after_secs": 180.0, "age_secs": 12.0, "stale": False, "reason": "fresh"},
        tick={"bid": 1.1010, "ask": 1.1012},
        spread_unit_source="live",
        mt4_fresh=True,
        ticks_fresh=True,
        paused=False,
        positions=[],
        pair_count=0,
        total_count=0,
        current_equity=10000.0,
        planned_entry_lots=0.15,
        lifecycle_action="hold",
        lifecycle_reason="hold",
        lifecycle_action_score=0.22,
        close_lots=0.0,
        sl_price=0.0,
        rejection_reasons=[],
        state={"equity_peak": 10400.0, "balance": 10050.0, "positions": []},
        settings=SimpleNamespace(max_total_positions=8, max_pair_positions=3, max_allowed_spread_bps=3.0),
        portfolio_positions=[],
        governance_policy={"capital_band": "micro_live", "mode": "normal", "budget_scale": 1.0},
        pending_entries=[],
    )

    assert captured["uncertainty_score"] == 0.17
    assert out["portfolio_allocation"]["budget"]["budget_scale"] == 1.0
