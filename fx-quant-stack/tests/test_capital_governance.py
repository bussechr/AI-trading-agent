from __future__ import annotations

from types import SimpleNamespace

from fxstack.runtime.governance import compute_capital_governance_state


def _settings(**overrides):
    base = {
        "capital_band_mode": "micro_live",
        "capital_entries_only": False,
        "capital_governance_enabled": True,
        "provider_shadow_only": False,
        "phase5_canary_latency_budget_ms": 100.0,
        "capital_max_stale_feature_count": 0,
        "capital_max_operational_fault_count": 0,
        "capital_max_concentration_share": 0.6,
        "capital_min_shadow_alignment_share": 0.5,
        "capital_rollout_budget_scale_micro_live": 0.1,
        "capital_rollout_budget_scale_low_risk": 0.25,
        "capital_rollout_budget_scale_full_risk": 1.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_compute_capital_governance_state_flags_breaches_and_rollback_actions() -> None:
    state = compute_capital_governance_state(
        settings=_settings(),
        runtime_diag={
            "loop_latency_ms": 200.0,
            "feature_serving": {"stale": True},
            "risk_cycle_summary": {"rollout_breach_count": 2},
            "shadow_policy": {"divergenceCounts": {"agreeReady": 1, "liveOnly": 4}},
        },
        metrics={"feature_parity": {"breaches": 3}},
        portfolio_telemetry={"concentration": {"top_symbol_share": 0.8}},
        provider_health={"market_data_provider": {"status": "degraded"}},
    )

    payload = state.to_dict()

    assert payload["mode"] == "paused"
    assert payload["paused"] is True
    assert payload["entries_only"] is True
    assert sorted(payload["reasons"]) == sorted(
        ["latency_breach", "stale_features", "parity_breach", "rollout_breach", "portfolio_concentration", "shadow_alignment"]
    )
    assert any(item["action"] == "model_rollback" and item["armed"] for item in payload["rollback_actions"])
    assert any(item["action"] == "global_rollback" and item["armed"] for item in payload["rollback_actions"])


def test_compute_capital_governance_state_defaults_paper_to_shadow_only() -> None:
    state = compute_capital_governance_state(
        settings=_settings(capital_band_mode="paper"),
        runtime_diag={"loop_latency_ms": 10.0, "feature_serving": {}, "risk_cycle_summary": {}, "shadow_policy": {}},
        metrics={"feature_parity": {"breaches": 0}},
        portfolio_telemetry={"concentration": {"top_symbol_share": 0.1}},
        provider_health={},
    )

    payload = state.to_dict()
    assert payload["capital_band"] == "paper"
    assert payload["shadow_only"] is True
    assert payload["budget_scale"] == 0.0


def test_compute_capital_governance_state_is_passive_when_disabled() -> None:
    state = compute_capital_governance_state(
        settings=_settings(
            capital_governance_enabled=False,
            capital_band_mode="paper",
        ),
        runtime_diag={
            "loop_latency_ms": 5000.0,
            "feature_serving": {"stale": True},
            "risk_cycle_summary": {"rollout_breach_count": 2},
            "shadow_policy": {"divergenceCounts": {"liveOnly": 4}},
        },
        metrics={"feature_parity": {"breaches": 3}},
        portfolio_telemetry={"concentration": {"top_symbol_share": 0.95}},
        provider_health={},
    )

    payload = state.to_dict()
    assert payload["capital_band"] == "paper"
    assert payload["mode"] == "normal"
    assert payload["entries_only"] is False
    assert payload["shadow_only"] is False
    assert payload["reasons"] == []
    assert payload["budget_scale"] == 1.0
