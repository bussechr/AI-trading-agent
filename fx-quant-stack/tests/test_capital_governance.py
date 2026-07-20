from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from fxstack.runtime import runner as runtime_runner
from fxstack.runtime.governance import (
    CAPITAL_GOVERNANCE_SCHEMA_VERSION,
    compute_binding_capital_governance_snapshot,
    compute_capital_governance_state,
)


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
            "shadow_policy": {
                "shadow_live_divergence_counts": {
                    "agree_ready": 1,
                    "agree_blocked": 0,
                    "live_only": 4,
                    "shadow_only": 0,
                }
            },
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
        [
            "latency_breach",
            "stale_features",
            "parity_breach",
            "rollout_breach",
            "portfolio_concentration",
            "market_pressure_high",
            "shadow_alignment",
        ]
    )
    assert any(item["action"] == "model_rollback" and item["armed"] for item in payload["rollback_actions"])
    assert any(item["action"] == "global_rollback" and item["armed"] for item in payload["rollback_actions"])


def test_compute_capital_governance_state_ignores_single_pair_stale_feature_telemetry() -> None:
    state = compute_capital_governance_state(
        settings=_settings(),
        runtime_diag={
            "loop_latency_ms": 12.0,
            "feature_serving": {
                "stale": True,
                "details": {
                    "selected_pairs_count": 2,
                    "selected_stale_count": 1,
                    "all_stale_count": 1,
                },
            },
            "risk_cycle_summary": {"rollout_breach_count": 0},
            "shadow_policy": {
                "shadow_live_divergence_counts": {
                    "agree_ready": 2,
                    "agree_blocked": 1,
                    "live_only": 0,
                    "shadow_only": 0,
                }
            },
        },
        metrics={"feature_parity": {"breaches": 0}},
        portfolio_telemetry={"concentration": {"top_symbol_share": 0.1}},
        provider_health={},
    )

    payload = state.to_dict()

    assert payload["mode"] == "normal"
    assert payload["paused"] is False
    assert payload["entries_only"] is False
    assert payload["reasons"] == []
    assert payload["metrics"]["stale_feature_count"] == 0
    assert payload["metrics"]["selected_feature_count"] == 2
    assert payload["metrics"]["selected_stale_feature_count"] == 1


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
            "shadow_policy": {
                "shadow_live_divergence_counts": {
                    "agree_ready": 0,
                    "agree_blocked": 0,
                    "live_only": 4,
                    "shadow_only": 0,
                }
            },
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


def test_compute_capital_governance_state_degrades_on_market_pressure_without_pause() -> None:
    state = compute_capital_governance_state(
        settings=_settings(),
        runtime_diag={"loop_latency_ms": 10.0, "feature_serving": {}, "risk_cycle_summary": {}, "shadow_policy": {}},
        metrics={"feature_parity": {"breaches": 0}},
        portfolio_telemetry={
            "gross_exposure": 100000.0,
            "gross_lot_exposure": 1.0,
            "net_exposure": 32000.0,
            "net_lot_exposure": 0.32,
            "concentration": {
                "top_symbol_share": 0.55,
                "top_currency_share": 0.48,
                "symbol_hhi": 0.51,
                "currency_hhi": 0.50,
            },
            "correlation": {
                "method": "realized",
                "window_bars": 16,
                "min_obs": 4,
                "sample_count": 16,
                "max_abs_corr": 0.62,
                "avg_abs_corr": 0.58,
            },
            "budget": {
                "correlation_method": "realized",
                "correlation_sample_count": 16,
                "budget_scale": 0.08,
            },
        },
        provider_health={},
    )

    payload = state.to_dict()
    assert payload["mode"] == "degraded"
    assert payload["paused"] is False
    assert payload["entries_only"] is False
    assert payload["budget_scale"] == pytest.approx(0.08)
    assert "market_pressure_degraded" in payload["reasons"]
    assert "realized_correlation" in payload["reasons"]
    assert "portfolio_concentration" in payload["reasons"]
    assert payload["metrics"]["correlation_method"] == "realized"
    assert payload["metrics"]["session_peak_share"] == pytest.approx(0.0)
    assert payload["metrics"]["session_penalty"] == pytest.approx(0.0)
    assert payload["metrics"]["rebalance_pressure"] >= payload["metrics"]["resize_pressure"]
    assert payload["metrics"]["currency_stress"] >= payload["metrics"]["top_currency_share"]
    assert payload["metrics"]["market_pressure"] > 0.6


def test_compute_capital_governance_state_enters_entries_only_for_extreme_market_pressure() -> None:
    state = compute_capital_governance_state(
        settings=_settings(),
        runtime_diag={"loop_latency_ms": 10.0, "feature_serving": {}, "risk_cycle_summary": {}, "shadow_policy": {}},
        metrics={"feature_parity": {"breaches": 0}},
        portfolio_telemetry={
            "gross_exposure": 100000.0,
            "gross_lot_exposure": 1.0,
            "net_exposure": 80000.0,
            "net_lot_exposure": 0.8,
            "concentration": {
                "top_symbol_share": 0.9,
                "top_currency_share": 0.9,
                "symbol_hhi": 0.9,
                "currency_hhi": 0.9,
            },
            "correlation": {
                "method": "hybrid",
                "window_bars": 32,
                "min_obs": 8,
                "sample_count": 32,
                "max_abs_corr": 0.95,
                "avg_abs_corr": 0.90,
            },
            "budget": {
                "correlation_method": "hybrid",
                "correlation_sample_count": 32,
                "budget_scale": 0.05,
            },
        },
        provider_health={},
    )

    payload = state.to_dict()
    assert payload["mode"] == "entries_only"
    assert payload["paused"] is False
    assert payload["entries_only"] is True
    assert payload["budget_scale"] == pytest.approx(0.045)
    assert "market_pressure_high" in payload["reasons"]
    assert "realized_correlation" in payload["reasons"]
    assert "portfolio_concentration" in payload["reasons"]
    assert payload["metrics"]["session_peak_share"] == pytest.approx(0.0)
    assert payload["metrics"]["session_penalty"] == pytest.approx(0.0)
    assert payload["metrics"]["rebalance_pressure"] >= payload["metrics"]["resize_pressure"]
    assert payload["metrics"]["currency_stress"] >= payload["metrics"]["top_currency_share"]


def _binding_snapshot(**overrides):
    kwargs = {
        "settings": _settings(),
        "runtime_diag": {
            "loop_latency_ms": 10.0,
            "feature_serving": {},
            "risk_cycle_summary": {},
            "shadow_policy": {},
        },
        "metrics": {"feature_parity": {"breaches": 0}},
        "portfolio_telemetry": {"numeric_inputs_valid": True},
        "provider_health": {},
        "previous_governance": {"schema_version": CAPITAL_GOVERNANCE_SCHEMA_VERSION},
        "previous_cycle_ts": 90.0,
        "computed_at": 100.0,
        "max_source_age_secs": 60.0,
    }
    kwargs.update(overrides)
    return compute_binding_capital_governance_snapshot(**kwargs)


def test_binding_capital_governance_snapshot_is_fresh_versioned_and_admissible() -> None:
    payload = _binding_snapshot()

    assert payload["schema_version"] == CAPITAL_GOVERNANCE_SCHEMA_VERSION
    assert payload["binding"] is True
    assert payload["source_age_secs"] == pytest.approx(10.0)
    assert payload["mode"] == "normal"
    assert payload["paused"] is False


def test_binding_capital_governance_consumes_runtime_shadow_policy_producer_shape() -> None:
    payload = _binding_snapshot(
        runtime_diag={
            "loop_latency_ms": 10.0,
            "feature_serving": {},
            "risk_cycle_summary": {},
            "shadow_policy": {
                "shadow_policy_enabled": True,
                "shadow_candidate_count": 8,
                "shadow_ranked_count": 4,
                "shadow_would_trade_count": 3,
                "shadow_live_divergence_counts": {
                    "agree_ready": 1,
                    "agree_blocked": 1,
                    "live_only": 4,
                    "shadow_only": 2,
                    "open_position": 99,
                },
                "shadow_rejection_reason_counts": {"ranked_out": 3},
            },
        }
    )

    assert payload["metrics"]["shadow_alignment_source"] == "shadow_live_divergence_counts"
    assert payload["metrics"]["shadow_divergence_counts"] == {
        "agree_ready": 1,
        "agree_blocked": 1,
        "live_only": 4,
        "shadow_only": 2,
    }
    assert payload["metrics"]["shadow_alignment_share"] == pytest.approx(0.25)
    assert "shadow_alignment" in payload["reasons"]
    assert payload["paused"] is True


def test_binding_capital_governance_reads_legacy_camelcase_counts_during_rolling_upgrade() -> None:
    payload = _binding_snapshot(
        runtime_diag={
            "loop_latency_ms": 10.0,
            "feature_serving": {},
            "risk_cycle_summary": {},
            "shadow_policy": {
                "divergenceCounts": {
                    "agreeReady": 2,
                    "agreeBlocked": 1,
                    "liveOnly": 2,
                    "shadowOnly": 1,
                }
            },
        }
    )

    assert payload["metrics"]["shadow_alignment_source"] == "legacy:divergenceCounts"
    assert payload["metrics"]["shadow_alignment_share"] == pytest.approx(0.5)
    assert "shadow_alignment" not in payload["reasons"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"previous_governance": {}, "previous_cycle_ts": 0.0}, "governance_bootstrap"),
        ({"previous_governance": {"schema_version": "legacy"}}, "governance_contract_mismatch"),
        ({"previous_cycle_ts": 1.0}, "governance_source_stale"),
        ({"portfolio_telemetry": {"numeric_inputs_valid": False}}, "portfolio_numeric_inputs_invalid"),
    ],
)
def test_binding_capital_governance_snapshot_fails_closed(overrides, reason) -> None:
    payload = _binding_snapshot(**overrides)

    assert payload["paused"] is True
    assert payload["entries_only"] is True
    assert payload["budget_scale"] == 0.0
    assert reason in payload["reasons"]
    assert any(item["action"] == "global_rollback" and item["armed"] for item in payload["rollback_actions"])


def test_binding_capital_governance_snapshot_remains_passive_when_disabled() -> None:
    payload = _binding_snapshot(
        settings=_settings(capital_governance_enabled=False),
        previous_governance={},
        previous_cycle_ts=0.0,
    )

    assert payload["binding"] is False
    assert payload["paused"] is False
    assert "governance_bootstrap" not in payload["reasons"]


def test_runner_computes_and_binds_one_governance_snapshot_before_pair_admission() -> None:
    source = inspect.getsource(runtime_runner.run_loop)
    compute_index = source.index("capital_governance = compute_binding_capital_governance_snapshot(")
    admission_index = source.index("# AGENT HOT PATH: Per-pair evaluation", compute_index)

    assert compute_index < admission_index
    assert source.count("compute_binding_capital_governance_snapshot(") == 1
    assert "governance_policy=dict(risk_governance_policy)" in source
    assert 'state_patch["governance"] = dict(capital_governance)' in source
