from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fxstack.orchestration import replay


def _profile() -> replay.ReplayProfile:
    return replay.ReplayProfile(
        profile_id="unit",
        pairs=["EURUSD"],
        feature_contract_id="fxstack.test.v1",
        feature_root="fx-quant-stack/data/raw",
        start_equity=10_000.0,
        slippage_bps=0.25,
        seed=42,
        reduce_fraction=0.5,
        twin_validation_limit=10,
        bridge_url="http://127.0.0.1:58710",
        live_api_key="",
        orchestration_source={"kind": "capture_dir"},
        thresholds=replay.PromotionThresholds(
            entry_ratio_floor=0.90,
            slot_utilisation_floor=0.90,
            trace_completeness_floor=0.99,
            parity_overlap_floor=0.95,
            command_divergence_rate_ceiling=0.05,
            max_drawdown_deterioration_pct=1.5,
        ),
        windows={
            "calm": replay.ReplayWindow(
                window_id="calm",
                start_ts="2026-03-20T00:00:00Z",
                end_ts="2026-03-21T00:00:00Z",
            )
        },
        metadata={},
    )


def test_build_orchestration_cycles_prefers_persisted_runs_over_snapshot_reconstruction() -> None:
    profile = _profile()
    window = profile.windows["calm"]
    run_id = str(uuid4())
    trace_id = f"trace-{run_id}"
    bundle = {
        "runs": [
            {
                "run_id": run_id,
                "pair": "EURUSD",
                "ts_utc": replay._utc_epoch("2026-03-20T12:00:00Z"),
                "packet_json": {
                    "pair": "EURUSD",
                    "ts_utc": "2026-03-20T12:00:00Z",
                    "baseline_action": {"action": "enter", "side": "BUY"},
                    "shadow_action": {"action": "hold", "side": "FLAT"},
                    "divergence_reason": "baseline_enter_shadow_block",
                    "proposal_votes": {"total": 2},
                    "proposals": [{"agent_id": "signal_agent", "intent": "hold", "side": "FLAT"}],
                    "governed_decision": {
                        "selected_action": "hold",
                        "blocking_reasons": ["shadow_meta_reject"],
                        "command_preview": {},
                    },
                    "latency_ms": 34,
                    "fallback_used": False,
                },
            }
        ],
        "traces": [
            {
                "run_id": run_id,
                "trace_json": {"trace_id": trace_id},
            }
        ],
        "snapshots": [
            {
                "ts": replay._utc_epoch("2026-03-20T12:00:00Z"),
                "decisions_json": [
                    {
                        "symbol": "EURUSD",
                        "metadata": {
                            "pair": "EURUSD",
                            "ts": "2026-03-20T12:00:00Z",
                            "orchestration_shadow": {
                                "run_id": run_id,
                                "trace_id": trace_id,
                                "baseline_action": {"action": "enter", "side": "BUY"},
                                "shadow_action": {"action": "no_trade", "side": "FLAT"},
                            },
                        },
                    }
                ],
            }
        ],
        "state": {},
        "source_kind": "capture_dir",
    }

    cycles, summary = replay.build_orchestration_cycles(
        profile=profile,
        window=window,
        bundle=bundle,
        seed=42,
    )

    assert len(cycles) == 1
    assert cycles[0].context_source == "persisted"
    assert cycles[0].orchestrated_action_class == "hold"
    assert summary["persisted_count"] == 1
    assert summary["reconstructed_count"] == 0
    assert summary["snapshot_overlap_valid"] is True


def test_build_divergence_rows_computes_expected_parity_metrics() -> None:
    cycles = [
        replay.OrchestrationCycle(
            pair="EURUSD",
            ts=replay._utc_iso("2026-03-20T12:00:00Z"),
            feature_contract_id="fxstack.test.v1",
            context_source="persisted",
            trace_complete=True,
            decision_seed=42,
            run_id="run-1",
            trace_id="trace-1",
            baseline_action_class="enter_buy",
            orchestrated_action_class="no_trade",
            governor_outcome="no_trade",
            divergence_reason="baseline_enter_shadow_block",
            blocking_reasons=["shadow_meta_reject"],
            latency_ms=25.0,
            proposal_votes={"total": 1},
            proposals=[],
            fallback_used=False,
            packet={},
            trace={},
        )
    ]
    baseline_rows = [
        {
            "pair": "EURUSD",
            "ts": "2026-03-20T12:00:00Z",
            "allowed": "true",
            "side": "BUY",
            "lifecycle_action": "hold",
            "position_side": "",
        }
    ]
    adaptive_rows = [
        {
            "pair": "EURUSD",
            "ts": "2026-03-20T12:00:00Z",
            "allowed": "false",
            "side": "BUY",
            "lifecycle_action": "hold",
            "position_side": "",
        }
    ]

    divergence_rows, metrics = replay.build_divergence_rows(
        baseline_history_rows=baseline_rows,
        adaptive_history_rows=adaptive_rows,
        cycles=cycles,
        feature_contract_id="fxstack.test.v1",
    )

    assert len(divergence_rows) == 1
    assert divergence_rows[0]["baseline_action_class"] == "enter_buy"
    assert divergence_rows[0]["adaptive_action_class"] == "no_trade"
    assert divergence_rows[0]["orchestrated_action_class"] == "no_trade"
    assert metrics["parity_overlap"] == 0.0
    assert metrics["command_divergence_rate"] == 1.0
    assert metrics["baseline_policy_block_rate"] == 0.0
    assert metrics["orchestrated_policy_block_rate"] == 1.0


def test_simulate_orchestrated_shadow_lane_emits_positive_trade_metrics() -> None:
    profile = _profile()
    cycles = [
        replay.OrchestrationCycle(
            pair="EURUSD",
            ts=replay._utc_iso("2026-03-20T12:00:00Z"),
            feature_contract_id="fxstack.test.v1",
            context_source="persisted",
            trace_complete=True,
            decision_seed=42,
            run_id="run-1",
            trace_id="trace-1",
            baseline_action_class="enter_buy",
            orchestrated_action_class="enter_buy",
            governor_outcome="enter_buy",
            divergence_reason="agree",
            blocking_reasons=[],
            latency_ms=20.0,
            proposal_votes={"total": 1},
            proposals=[],
            fallback_used=False,
            packet={"governed_decision": {"command_preview": {"lots": 0.1}}},
            trace={},
        ),
        replay.OrchestrationCycle(
            pair="EURUSD",
            ts=replay._utc_iso("2026-03-20T12:05:00Z"),
            feature_contract_id="fxstack.test.v1",
            context_source="persisted",
            trace_complete=True,
            decision_seed=42,
            run_id="run-2",
            trace_id="trace-2",
            baseline_action_class="exit",
            orchestrated_action_class="exit",
            governor_outcome="exit",
            divergence_reason="agree",
            blocking_reasons=[],
            latency_ms=24.0,
            proposal_votes={"total": 1},
            proposals=[],
            fallback_used=False,
            packet={},
            trace={},
        ),
    ]
    price_lookup = {
        "EURUSD": {
            replay._utc_iso("2026-03-20T12:00:00Z"): {"bid": 1.1000, "ask": 1.1002, "mid": 1.1001},
            replay._utc_iso("2026-03-20T12:05:00Z"): {"bid": 1.1010, "ask": 1.1012, "mid": 1.1011},
        }
    }

    aggregate, history, trace_summary = replay.simulate_orchestrated_shadow_lane(
        profile=profile,
        cycles=cycles,
        price_lookup=price_lookup,
    )

    assert aggregate["entries"] == 1
    assert aggregate["trades"] == 1
    assert aggregate["net_pnl_usd"] > 0.0
    assert aggregate["latency_p95_ms"] >= 20.0
    assert len(history) == 2
    assert trace_summary["trace_completeness_rate"] == 1.0

