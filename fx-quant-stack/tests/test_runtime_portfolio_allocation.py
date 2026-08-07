from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

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


def test_runtime_risk_trace_payloads_prefer_batch_contract_and_fall_back() -> None:
    class _LegacyTrace:
        def to_dict(self) -> dict[str, object]:
            return {"serializer": "public"}

    class _CompactTrace(_LegacyTrace):
        def to_runtime_dict(self) -> dict[str, object]:
            return {"serializer": "runtime"}

    class _LegacyDecision:
        trace = [_CompactTrace(), _LegacyTrace()]

    class _BatchDecision(_LegacyDecision):
        def _to_runtime_trace_payloads(self) -> list[dict[str, object]]:
            return [{"serializer": "batch"}]

    assert runtime_runner._runtime_risk_trace_payloads(_LegacyDecision()) == [
        {"serializer": "runtime"},
        {"serializer": "public"},
    ]
    assert runtime_runner._runtime_risk_trace_payloads(_BatchDecision()) == [
        {"serializer": "batch"}
    ]


def test_realized_return_loader_preserves_utc_timestamps_for_pairwise_alignment() -> None:
    class _FakeStore:
        def read_recent_rows(self, **kwargs):
            assert kwargs["pair"] == "EURUSD"
            return pd.DataFrame(
                {
                    "ts": [
                        "2026-04-08T00:10:00Z",
                        "2026-04-08T00:00:00Z",
                        "2026-04-08T00:05:00Z",
                        "2026-04-08T00:05:00Z",
                    ],
                    "ret_1": [0.003, 0.001, 0.002, 0.0025],
                }
            )

    result = runtime_runner._pair_realized_returns_by_symbol(
        store=_FakeStore(),
        provider="test",
        symbols=["eurusd"],
        timeframe="M5",
        max_rows=32,
    )

    series = result["EURUSD"]
    assert isinstance(series.index, pd.DatetimeIndex)
    assert str(series.index.tz) == "UTC"
    assert series.index.is_monotonic_increasing
    assert series.index.is_unique
    assert series.tolist() == [0.001, 0.0025, 0.003]


def test_realized_return_loader_supports_all_sources_without_reconversion() -> None:
    timestamps = pd.date_range(
        "2026-04-08T00:00:00Z",
        periods=3,
        freq="5min",
    )
    frames = {
        "DIRECT": pd.DataFrame({"ts": timestamps, "ret_1": [0.01, 0.02, 0.03]}),
        "LOGRET": pd.DataFrame({"ts": timestamps, "log_ret_1": [0.04, 0.05, 0.06]}),
        "CLOSES": pd.DataFrame({"ts": timestamps, "close": [100.0, 101.0, 102.0]}),
        "MIDVAL": pd.DataFrame({"ts": timestamps, "mid": [200.0, 202.0, 204.0]}),
        "MISSING": pd.DataFrame({"ts": timestamps, "spread": [1.0, 1.0, 1.0]}),
    }

    class _FakeStore:
        def read_recent_rows(self, **kwargs):
            return frames[kwargs["pair"]]

    result = runtime_runner._pair_realized_returns_by_symbol(
        store=_FakeStore(),
        provider="test",
        symbols=list(frames),
        timeframe="M5",
        max_rows=32,
    )

    assert result["DIRECT"].tolist() == [0.01, 0.02, 0.03]
    assert result["LOGRET"].tolist() == [0.04, 0.05, 0.06]
    assert result["CLOSES"].tolist() == pytest.approx([0.01, 1.0 / 101.0])
    assert result["MIDVAL"].tolist() == pytest.approx([0.01, 2.0 / 202.0])
    assert "MISSING" not in result
    assert all(series.dtype == float for series in result.values())


def test_runtime_risk_kernel_uses_scorer_uncertainty_for_portfolio_allocation(monkeypatch) -> None:
    captured: dict[str, float] = {}

    def _serialized(name: str) -> dict[str, object]:
        captured[name] = captured.get(name, 0.0) + 1.0
        return {}

    class _FakeBudget:
        budget_scale = 1.0
        reason = "ok"

    class _FakeAllocation:
        allowed = True
        budget = _FakeBudget()
        book = SimpleNamespace(
            gross_exposure=0.0,
            net_exposure=0.0,
            to_dict=lambda: _serialized("book_serializations"),
        )
        concentration = SimpleNamespace(
            to_dict=lambda: _serialized("concentration_serializations")
        )
        correlation = SimpleNamespace(
            to_dict=lambda: _serialized("correlation_serializations")
        )
        stress = SimpleNamespace(
            to_dict=lambda: _serialized("stress_serializations")
        )
        telemetry = {}

        def to_dict(self) -> dict[str, object]:
            captured["allocation_serializations"] = (
                captured.get("allocation_serializations", 0.0) + 1.0
            )
            return {
                "allowed": self.allowed,
                "budget": {"budget_scale": self.budget.budget_scale, "reason": self.budget.reason},
                "book": self.book.to_dict(),
                "concentration": self.concentration.to_dict(),
                "correlation": self.correlation.to_dict(),
                "stress": self.stress.to_dict(),
                "telemetry": dict(self.telemetry),
            }

        def to_runtime_dict(self) -> dict[str, object]:
            captured["runtime_allocation_serializations"] = (
                captured.get("runtime_allocation_serializations", 0.0) + 1.0
            )
            return {
                "allowed": self.allowed,
                "budget": {
                    "budget_scale": self.budget.budget_scale,
                    "reason": self.budget.reason,
                },
                "telemetry": {
                    "concentration": {},
                    "correlation": {},
                    "stress": {},
                },
            }

    def _fake_evaluate_portfolio_allocation(*, uncertainty_score, **kwargs):
        captured["uncertainty_score"] = float(uncertainty_score)
        captured["runtime_read_only"] = kwargs.get("_runtime_read_only") is True
        return _FakeAllocation()

    def _fake_evaluate_risk_decision(*, policy_intent, market_state, portfolio_state, config):
        captured["allowed_spread_bps"] = float(market_state.allowed_spread_bps)
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
        sl_price=1.0990,
        tp_price=1.1040,
        rejection_reasons=[],
        state={"equity_peak": 10400.0, "balance": 10050.0, "positions": []},
        settings=SimpleNamespace(max_total_positions=8, max_pair_positions=3, max_allowed_spread_bps=3.0),
        portfolio_positions=[],
        governance_policy={"capital_band": "micro_live", "mode": "normal", "budget_scale": 1.0},
        pending_entries=[],
        allowed_spread_bps=5.5,
    )

    assert captured["uncertainty_score"] == 0.17
    assert captured["runtime_read_only"] is True
    assert captured["allowed_spread_bps"] == 5.5
    assert captured["runtime_allocation_serializations"] == 1.0
    assert "allocation_serializations" not in captured
    assert "book_serializations" not in captured
    assert "concentration_serializations" not in captured
    assert "correlation_serializations" not in captured
    assert "stress_serializations" not in captured
    assert out["portfolio_allocation"]["budget"]["budget_scale"] == 1.0


def test_runtime_risk_kernel_keeps_portfolio_diagnostics_out_of_broker_payload() -> None:
    out = runtime_runner._evaluate_runtime_risk_kernel(
        pair="EURUSD",
        ts_value="2026-08-05T12:00:00Z",
        side="BUY",
        signal=SimpleNamespace(
            trade_prob=0.99,
            uncertainty_score=0.01,
            session_bucket="london",
            reversal_ready=False,
        ),
        expected_edge_bps=18.0,
        spread_bps=1.2,
        feature_bar={
            "stale_after_secs": 180.0,
            "age_secs": 12.0,
            "stale": False,
            "reason": "fresh",
        },
        tick={"bid": 1.1010, "ask": 1.1012},
        spread_unit_source="live",
        mt4_fresh=True,
        ticks_fresh=True,
        paused=False,
        positions=[],
        pair_count=0,
        total_count=0,
        current_equity=10_000.0,
        planned_entry_lots=0.15,
        lifecycle_action="entry",
        lifecycle_reason="entry",
        lifecycle_action_score=0.99,
        close_lots=0.0,
        sl_price=1.0990,
        tp_price=1.1040,
        rejection_reasons=[],
        state={"equity_peak": 10_000.0, "balance": 10_000.0, "positions": []},
        settings=SimpleNamespace(
            max_total_positions=8,
            max_pair_positions=3,
            max_allowed_spread_bps=3.0,
            account_currency="USD",
            max_drawdown_pct=50.0,
            max_gross_exposure=10.0,
            max_net_exposure=10.0,
            rollout_mode="live",
            rollout_pair_allowlisted=True,
            rollout_budget_scale=1.0,
            min_lots=0.01,
            lot_step=0.01,
            max_lots=100.0,
        ),
        portfolio_positions=[],
        governance_policy={
            "capital_band": "full_risk_live",
            "mode": "normal",
            "budget_scale": 1.0,
        },
        pending_entries=[],
    )

    approved_order = dict(out["approved_order"])
    allocation_telemetry = dict(out["portfolio_allocation"]["telemetry"])
    diagnostic_keys = (
        "portfolio_concentration",
        "portfolio_correlation",
        "portfolio_stress",
    )

    assert approved_order["cmd"] == "BUY"
    assert "decision" not in out
    for key in diagnostic_keys:
        assert key not in approved_order
    assert allocation_telemetry["concentration"]
    assert allocation_telemetry["correlation"]
    assert allocation_telemetry["stress"]


def test_portfolio_budget_scale_binds_on_target_risk_pct_path(monkeypatch) -> None:
    """Capital-band budget scale must shrink the risk fraction the kernel sizes
    from, not just the legacy requested_lots path. Regression for the audit
    finding that micro_live/low_risk bands were telemetry on risk-sized entries."""

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
            return {"allowed": True, "budget": {"budget_scale": 1.0, "reason": "ok"}}

    captured_metadata: list[dict[str, object]] = []

    def _fake_evaluate_portfolio_allocation(**kwargs):
        return _FakeAllocation()

    def _fake_evaluate_risk_decision(*, policy_intent, market_state, portfolio_state, config):
        captured_metadata.append(dict(policy_intent.metadata))
        return _FakeDecision()

    monkeypatch.setattr(runtime_runner, "evaluate_portfolio_allocation", _fake_evaluate_portfolio_allocation)
    import fxstack.risk.envelope as risk_envelope

    monkeypatch.setattr(risk_envelope, "evaluate_risk_decision", _fake_evaluate_risk_decision)

    def _run(governance_policy: dict[str, object]) -> None:
        runtime_runner._evaluate_runtime_risk_kernel(
            pair="EURUSD",
            ts_value="2026-07-31T12:00:00Z",
            side="BUY",
            signal=SimpleNamespace(trade_prob=0.62, uncertainty_score=0.2, session_bucket="london", reversal_ready=False),
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
            lifecycle_action_score=0.62,
            close_lots=0.0,
            sl_price=1.0990,
            tp_price=1.1040,
            rejection_reasons=[],
            state={"equity_peak": 10000.0, "balance": 10050.0, "positions": []},
            settings=SimpleNamespace(
                max_total_positions=8,
                max_pair_positions=3,
                max_allowed_spread_bps=3.0,
                account_currency="USD",
            ),
            portfolio_positions=[],
            governance_policy=governance_policy,
            pending_entries=[],
        )

    _run({"capital_band": "full_risk_live", "mode": "normal", "budget_scale": 1.0})
    _run({"capital_band": "micro_live", "mode": "normal", "budget_scale": 0.1})

    baseline, micro = captured_metadata
    # Risk-sizing path engaged: lots zeroed, fraction handed to the kernel.
    assert baseline["requested_lots"] == 0.0
    assert micro["requested_lots"] == 0.0
    assert baseline["target_risk_pct"] > 0.0
    # Identical inputs, so the unscaled fraction matches across runs...
    assert micro["target_risk_pct_prescale"] == baseline["target_risk_pct_prescale"]
    # ...and the band's budget scale is what shrinks the bound fraction.
    assert baseline["target_risk_pct"] == baseline["target_risk_pct_prescale"]
    assert micro["target_risk_pct"] == baseline["target_risk_pct_prescale"] * 0.1
    # Every risk decision carries the certification-mode stamp (default:
    # required, since neither mode setting is present on these test doubles).
    assert baseline["entry_certification_mode"] == "required"
    assert micro["entry_certification_mode"] == "required"


def test_explicit_cash_risk_cap_binds_before_broker_sizing(monkeypatch) -> None:
    class _FakeBudget:
        budget_scale = 1.0
        reason = "ok"

    class _FakeAllocation:
        allowed = True
        budget = _FakeBudget()
        book = SimpleNamespace(
            gross_exposure=0.0,
            net_exposure=0.0,
            to_dict=lambda: {},
        )
        concentration = SimpleNamespace(to_dict=lambda: {})
        correlation = SimpleNamespace(to_dict=lambda: {})
        stress = SimpleNamespace(to_dict=lambda: {})
        telemetry = {}

        def to_dict(self) -> dict[str, object]:
            return {
                "allowed": True,
                "budget": {"budget_scale": 1.0, "reason": "ok"},
            }

    captured: dict[str, object] = {}

    def _fake_evaluate_risk_decision(
        *, policy_intent, market_state, portfolio_state, config
    ):
        del market_state, portfolio_state, config
        captured.update(dict(policy_intent.metadata))
        return _FakeDecision()

    monkeypatch.setattr(
        runtime_runner,
        "evaluate_portfolio_allocation",
        lambda **kwargs: _FakeAllocation(),
    )
    import fxstack.risk.envelope as risk_envelope

    monkeypatch.setattr(
        risk_envelope,
        "evaluate_risk_decision",
        _fake_evaluate_risk_decision,
    )

    runtime_runner._evaluate_runtime_risk_kernel(
        pair="EURUSD",
        ts_value="2026-08-03T12:00:00Z",
        side="BUY",
        signal=SimpleNamespace(
            trade_prob=0.99,
            uncertainty_score=0.01,
            session_bucket="london",
            reversal_ready=False,
        ),
        expected_edge_bps=8.0,
        spread_bps=1.0,
        feature_bar={
            "stale_after_secs": 180.0,
            "age_secs": 1.0,
            "stale": False,
            "reason": "fresh",
        },
        tick={"bid": 1.1000, "ask": 1.1001},
        spread_unit_source="live",
        mt4_fresh=True,
        ticks_fresh=True,
        paused=False,
        positions=[],
        pair_count=0,
        total_count=0,
        current_equity=10_000.0,
        planned_entry_lots=0.0,
        lifecycle_action="entry",
        lifecycle_reason="probe",
        lifecycle_action_score=0.99,
        close_lots=0.0,
        sl_price=1.0991,
        tp_price=1.1041,
        rejection_reasons=[],
        state={"equity_peak": 10_000.0, "balance": 10_000.0, "positions": []},
        settings=SimpleNamespace(
            max_total_positions=8,
            max_pair_positions=1,
            max_allowed_spread_bps=3.0,
            account_currency="USD",
        ),
        portfolio_positions=[],
        governance_policy={
            "capital_band": "full_risk_live",
            "mode": "normal",
            "budget_scale": 1.0,
        },
        pending_entries=[],
        entry_cash_risk_cap=1.0,
    )

    assert captured["entry_cash_risk_cap"] == 1.0
    assert captured["entry_cash_risk_cap_applied"] is True
    assert captured["target_risk_pct_prescale"] == 0.0001
    assert captured["target_risk_pct"] == 0.0001


def test_intelligent_entry_size_scale_binds_on_risk_path(monkeypatch) -> None:
    """adaptive_size_scale x sleeve_expectancy_scale used to shrink only the
    legacy planned lots, which the risk path zeroes -- a losing sleeve never
    actually shrank. The scale now multiplies the bound risk fraction."""

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
            return {"allowed": True, "budget": {"budget_scale": 1.0, "reason": "ok"}}

    captured_metadata: list[dict[str, object]] = []

    def _fake_evaluate_risk_decision(*, policy_intent, market_state, portfolio_state, config):
        captured_metadata.append(dict(policy_intent.metadata))
        return _FakeDecision()

    monkeypatch.setattr(runtime_runner, "evaluate_portfolio_allocation", lambda **kwargs: _FakeAllocation())
    import fxstack.risk.envelope as risk_envelope

    monkeypatch.setattr(risk_envelope, "evaluate_risk_decision", _fake_evaluate_risk_decision)

    common = dict(
        pair="EURUSD",
        ts_value="2026-07-31T12:00:00Z",
        side="BUY",
        signal=SimpleNamespace(trade_prob=0.62, uncertainty_score=0.2, session_bucket="london", reversal_ready=False),
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
        lifecycle_action_score=0.62,
        close_lots=0.0,
        sl_price=1.0990,
        tp_price=1.1040,
        rejection_reasons=[],
        state={"equity_peak": 10000.0, "balance": 10050.0, "positions": []},
        settings=SimpleNamespace(
            max_total_positions=8,
            max_pair_positions=3,
            max_allowed_spread_bps=3.0,
            account_currency="USD",
        ),
        portfolio_positions=[],
        governance_policy={"capital_band": "full_risk_live", "mode": "normal", "budget_scale": 1.0},
        pending_entries=[],
    )
    runtime_runner._evaluate_runtime_risk_kernel(**common)
    runtime_runner._evaluate_runtime_risk_kernel(**common, entry_size_scale=0.5)

    unscaled, halved = captured_metadata
    assert unscaled["target_risk_pct"] > 0.0
    assert halved["target_risk_pct_prescale"] == unscaled["target_risk_pct_prescale"]
    assert halved["target_risk_pct"] == unscaled["target_risk_pct"] * 0.5
    assert halved["entry_size_scale"] == 0.5


def test_zero_portfolio_budget_scale_is_a_loud_policy_rejection(monkeypatch) -> None:
    """budget_scale == 0 (paused/shadow) must surface as a named rejection, not
    a silent 0-lot approval -- the historical failure mode is trading silently
    disabled."""

    class _FakeBudget:
        budget_scale = 0.0
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
            return {"allowed": True, "budget": {"budget_scale": 0.0, "reason": "ok"}}

    captured_metadata: list[dict[str, object]] = []

    def _fake_evaluate_risk_decision(*, policy_intent, market_state, portfolio_state, config):
        captured_metadata.append(dict(policy_intent.metadata))
        return _FakeDecision()

    monkeypatch.setattr(runtime_runner, "evaluate_portfolio_allocation", lambda **kwargs: _FakeAllocation())
    import fxstack.risk.envelope as risk_envelope

    monkeypatch.setattr(risk_envelope, "evaluate_risk_decision", _fake_evaluate_risk_decision)

    runtime_runner._evaluate_runtime_risk_kernel(
        pair="EURUSD",
        ts_value="2026-07-31T12:00:00Z",
        side="BUY",
        signal=SimpleNamespace(trade_prob=0.62, uncertainty_score=0.2, session_bucket="london", reversal_ready=False),
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
        lifecycle_action_score=0.62,
        close_lots=0.0,
        sl_price=1.0990,
        tp_price=1.1040,
        rejection_reasons=[],
        state={"equity_peak": 10000.0, "balance": 10050.0, "positions": []},
        settings=SimpleNamespace(
            max_total_positions=8,
            max_pair_positions=3,
            max_allowed_spread_bps=3.0,
            account_currency="USD",
        ),
        portfolio_positions=[],
        governance_policy={"capital_band": "shadow_only", "mode": "shadow_only", "budget_scale": 0.0},
        pending_entries=[],
    )

    (metadata,) = captured_metadata
    assert metadata["policy_allowed"] is False
    assert "portfolio_budget_scale_zero" in list(metadata["strict_reasons"])


def test_runtime_equity_peak_is_monotonic_across_cycles_and_restarts() -> None:
    peak = runtime_runner._advance_runtime_equity_peak(
        persisted_peak=None,
        current_equity=10_000.0,
        fallback_equity=9_000.0,
    )
    peak = runtime_runner._advance_runtime_equity_peak(
        persisted_peak=peak,
        current_equity=10_400.0,
        fallback_equity=9_000.0,
    )
    restarted_peak = runtime_runner._advance_runtime_equity_peak(
        persisted_peak=peak,
        current_equity=9_800.0,
        fallback_equity=9_800.0,
    )

    assert peak == 10_400.0
    assert restarted_peak == 10_400.0


def test_runtime_boot_patch_persists_peak_so_stale_pruning_cannot_reset_drawdown() -> None:
    live_authority = {
        "mode": "live",
        "runtime_enabled": False,
        "queue_kill_active": True,
        "queue_kill_reason": "operator_kill",
        "current_stage_index": 1,
        "current_stage_pct": 5,
        "bundle_run_id": "bundle-live",
        "entry_evidence_by_pair": {},
    }
    patch = runtime_runner._runtime_boot_reset_patch(
        runtime_profile="live",
        equity_seed=9_800.0,
        equity_peak=10_400.0,
        pairs=["EURUSD"],
        startup_state={"boot_id": "boot-1"},
        runtime_diag={"model_preflight": {"ok": True}},
        preserved_orchestration_live=live_authority,
    )

    assert patch["__prune_stale__"] is True
    assert patch["equity_peak"] == 10_400.0
    assert patch["runtime_diag"]["model_preflight"] == {"ok": True}
    assert patch["runtime_diag"]["orchestration_live"] == live_authority
    assert patch["__expected_orchestration_live_authority__"] == live_authority


def test_startup_progress_and_failure_patches_preserve_live_authority() -> None:
    live_authority = {
        "mode": "live",
        "runtime_enabled": False,
        "queue_kill_active": True,
        "queue_kill_reason": "operator_kill",
        "current_stage_index": 2,
        "current_stage_pct": 10,
        "bundle_run_id": "bundle-live",
        "entry_ratio_evaluable": False,
        "entry_evidence_by_pair": {},
    }

    class _RecordingService:
        def __init__(self) -> None:
            self.boot_patch: dict[str, object] = {}
            self.failure_patch: dict[str, object] = {}

        def get_state(self) -> dict[str, object]:
            return {
                "runtime_diag": {
                    "orchestration_live": dict(live_authority),
                }
            }

        def record_runtime_boot_state(self, *, boot, patch, prune_state) -> None:
            self.boot_patch = dict(patch)

        def record_runtime_boot_failure(
            self,
            *,
            boot,
            failure_reason,
            failed_at,
            patch,
            prune_state,
        ) -> None:
            self.failure_patch = dict(patch)

    svc = _RecordingService()
    startup_state = {
        "boot_id": "boot-1",
        "booted_at": "2026-07-20T00:00:00+00:00",
        "runtime_pid": 123,
        "pending_command_policy": "purge_and_mark_stale",
    }

    next_state = runtime_runner._touch_runtime_startup_progress(
        svc=svc,
        startup_state=startup_state,
        phase="model_load",
        runtime_diag={"model_load": {"ok": True}},
    )
    runtime_runner._record_runtime_startup_failure(
        svc=svc,
        startup_state=next_state,
        failure_reason="model_load_failed",
        runtime_diag={"model_load": {"ok": False}},
    )

    for patch in (svc.boot_patch, svc.failure_patch):
        runtime_diag = dict(patch["runtime_diag"])
        assert runtime_diag["orchestration_live"] == live_authority
        assert patch["__expected_orchestration_live_authority__"] == live_authority
