from __future__ import annotations

from types import SimpleNamespace

import fxstack.runtime.runner as runtime_runner


def test_finalize_entry_submissions_live_scope_block_does_not_fallback_to_baseline() -> None:
    class Settings:
        agent_mode = "live"
        agent_live_pair_allowlist = ["EURUSD"]
        agent_live_sleeve_allowlist = ["trend"]
        agent_live_intent_allowlist = ["enter"]
        agent_decision_timeout_ms = 250
        adaptive_execution_enabled = True
        adaptive_shadow_enabled = True

    class DummyService:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def submit_command(self, payload, proto="v2"):
            self.payloads.append(dict(payload))
            return {"status": "queued", "action": payload.get("action"), "command_id": payload.get("command_id")}, None

    decisions = [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "execution_ready": True,
            "reasons": [],
            "metadata": {
                "pair": "EURUSD",
                "strict_entry_ready": True,
                "strict_entry_blocking_reasons": [],
                "strict_rejection_reason": "none",
                "entry_ready": True,
                "entry_blocking_reasons": [],
                "rejection_reason": "none",
                "adaptive_shadow_would_trade": True,
                "adaptive_shadow_rejection_reason": "none",
                "lifecycle_action": "entry",
                "lifecycle_reason": "entry_approved",
                "adaptive_sleeve": "trend",
                "rollout_active": False,
                "rollout_mode": "",
                "rollout_pair_allowlisted": False,
                "mt4_fresh": True,
                "ticks_fresh": True,
            },
        }
    ]
    svc = DummyService()
    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            {
                "index": 0,
                "pair": "EURUSD",
                "ts_value": "2026-03-25T10:00:00Z",
                "action_key": "entry:2026-03-25T10:00:00Z",
                "payload": {"command_id": "baseline-live-scope", "action": "entry", "symbol": "EURUSD", "lots": 0.10},
                "approved_order": {"command_id": "baseline-live-scope", "action": "entry", "symbol": "EURUSD", "cmd": "BUY", "side": "BUY", "lots": 0.10},
                "orchestration": {
                    "enabled": True,
                    "correlation_id": "EURUSD:live:scope",
                    "thread_id": "EURUSD:live:scope",
                    "run_id": "live-run-scope",
                    "trace_id": "live-trace-scope",
                    "latency_ms": 12,
                    "fallback_used": False,
                    "fault_classification": "",
                    "governed_selected_action": "enter",
                    "governed_allowed": True,
                    "approval_state": "auto",
                    "governed_decision": {
                        "selected_action": "enter",
                        "allowed": True,
                        "approval_state": "auto",
                        "blocking_reasons": [],
                        "command_preview": {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.22, "intent": "ENTRY_MODEL", "action": "enter"},
                    },
                },
            }
        ],
        svc=svc,
        last_action_key={},
        settings=Settings(),
        runtime_state={"runtime_diag": {"orchestration_live": {"runtime_enabled": True, "queue_kill_active": False}}},
    )

    assert svc.payloads == []
    assert diag["live_governed_submitted_count"] == 0
    assert diag["live_baseline_fallback_count"] == 0
    assert decisions[0]["execution_ready"] is False
    assert decisions[0]["metadata"]["entry_ready"] is False
    assert decisions[0]["metadata"]["orchestration_live_command_source"] == "governed_live_blocked"
    assert decisions[0]["metadata"]["orchestration_live_fallback_reason"] == "live_canary_inactive"
    assert decisions[0]["metadata"]["enqueue"]["status"] == "skipped"
    assert decisions[0]["metadata"]["enqueue"]["reason"] == "live_canary_inactive"


def test_runtime_belief_shadow_skips_loaded_model_when_adaptive_row_missing(monkeypatch) -> None:
    def _unexpected_compute(**_: object):  # pragma: no cover - should never be called
        raise AssertionError("compute_directional_belief should not run without an adaptive row")

    monkeypatch.setattr(runtime_runner, "compute_directional_belief", _unexpected_compute)

    decisions = [
        {
            "symbol": "EURUSD",
            "metadata": {
                "pair": "EURUSD",
                "ts": "2026-03-26T12:00:00Z",
                "adaptive_playbook": "trend_pullback",
                "adaptive_environment_state": "PersistentTrend",
                "adaptive_playbook_score": 0.78,
                "adaptive_location_score": 0.66,
                "adaptive_trigger_score": 0.61,
                "adaptive_macro_coherence_score": 0.69,
                "adaptive_hostility_score": 0.08,
                "uncertainty_score": 0.12,
                "model_disagreement_score": 0.10,
                "extension_penalty_score": 0.15,
                "regime_prob": 0.81,
                "swing_prob": 0.72,
                "entry_prob": 0.63,
                "trade_prob": 0.69,
            },
        }
    ]

    cycle, metrics = runtime_runner._attach_directional_belief_shadow(
        decisions=decisions,
        loaded_model_sets={"EURUSD": SimpleNamespace(belief_model=object())},
        adaptive_rows_by_pair={},
        settings=SimpleNamespace(belief_shadow_enabled=True),
    )

    meta = decisions[0]["metadata"]
    assert meta["belief_source_mode"] == "artifact_missing"
    assert cycle["candidate_count_with_belief"] == 0
    assert metrics["belief_loaded_share"] == 0.0


def test_attach_directional_belief_shadow_keeps_telemetry_only_cross_pair_adjustment_neutral() -> None:
    class Settings:
        belief_shadow_enabled = False
        belief_influence_mode = "hard_gate"

    decisions = [
        {
            "symbol": "EURUSD",
            "metadata": {
                "pair": "EURUSD",
                "ts": "2026-04-07T12:00:00Z",
                "belief_primary_side": "long",
                "belief_primary_scenario": "trend_pullback",
                "belief_primary_score": 0.14,
                "belief_primary_rank_score": 0.12,
                "belief_primary_ev_above_hurdle_prob": 0.09,
                "belief_gap": 0.03,
                "belief_horizon_alignment_score": 0.10,
                "belief_regime_fit_score": 0.08,
                "belief_fragility_score": 0.92,
                "usd_strength_basket_ret_1": 0.0,
                "cross_pair_dispersion": 0.98,
            },
        },
        {
            "symbol": "GBPUSD",
            "metadata": {
                "pair": "GBPUSD",
                "ts": "2026-04-07T12:00:00Z",
                "belief_primary_side": "short",
                "belief_primary_scenario": "breakout_expansion",
                "belief_primary_score": 0.11,
                "belief_primary_rank_score": 0.09,
                "belief_primary_ev_above_hurdle_prob": 0.07,
                "belief_gap": 0.02,
                "belief_horizon_alignment_score": 0.06,
                "belief_regime_fit_score": 0.05,
                "belief_fragility_score": 0.95,
                "usd_strength_basket_ret_1": 0.0,
                "cross_pair_dispersion": 0.97,
            },
        },
    ]

    runtime_runner._attach_directional_belief_shadow(
        decisions=decisions,
        loaded_model_sets={},
        adaptive_rows_by_pair={},
        settings=Settings(),
    )

    for decision in decisions:
        meta = decision["metadata"]
        assert meta["cross_pair_source_mode"] == "telemetry_only"
        assert meta["cross_pair_influence_adjustment"] == 0.0
        assert meta["cross_pair_soft_block"] is False
        assert meta["cross_pair_hard_block"] is False
