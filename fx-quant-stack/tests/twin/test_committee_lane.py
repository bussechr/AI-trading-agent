from __future__ import annotations

from pathlib import Path

from fxstack.orchestration import replay


def _profile() -> replay.ReplayProfile:
    return replay.ReplayProfile(
        profile_id="committee",
        pairs=["EURUSD"],
        feature_contract_id="fxstack.test.v1",
        feature_root="fx-quant-stack/data/raw",
        start_equity=10_000.0,
        slippage_bps=0.25,
        seed=7,
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


def test_committee_lane_preserves_replay_metadata_and_divergence_fields() -> None:
    profile = _profile()
    window = profile.windows["calm"]
    bundle = {
        "runs": [
            {
                "run_id": "run-committee",
                "pair": "EURUSD",
                "ts_utc": replay._utc_epoch("2026-03-20T12:00:00Z"),
                "packet_json": {
                    "pair": "EURUSD",
                    "ts_utc": "2026-03-20T12:00:00Z",
                    "baseline_action": {"action": "enter", "side": "BUY"},
                    "shadow_action": {"action": "no_trade", "side": "FLAT"},
                    "divergence_reason": "baseline_enter_shadow_block",
                    "proposal_votes": {"total": 3, "by_intent": {"enter": 2, "no_trade": 1}},
                    "proposals": [
                        {
                            "agent_id": "committee.trend_pullback",
                            "proposal_role": "playbook_entry",
                            "intent": "enter",
                            "side": "BUY",
                        },
                        {
                            "agent_id": "committee.spread_microstructure",
                            "proposal_role": "microstructure_gate",
                            "intent": "no_trade",
                            "side": "FLAT",
                        },
                    ],
                    "governed_decision": {
                        "selected_action": "no_trade",
                        "blocking_reasons": ["spread_too_wide"],
                        "command_preview": {},
                        "winning_proposal_id": "00000000-0000-0000-0000-000000000110",
                        "arbiter_stage": "entry_ranking",
                        "arbiter_rationale": "entry-quality or microstructure gates blocked the candidate",
                    },
                    "winning_proposal_id": "00000000-0000-0000-0000-000000000110",
                    "arbiter_stage": "entry_ranking",
                    "arbiter_rationale": "entry-quality or microstructure gates blocked the candidate",
                    "score_path": [
                        {
                            "proposal_id": "00000000-0000-0000-0000-000000000110",
                            "agent_id": "committee.spread_microstructure",
                        }
                    ],
                    "latency_ms": 21,
                    "fallback_used": False,
                },
            }
        ],
        "traces": [
            {
                "run_id": "run-committee",
                "trace_json": {"trace_id": "trace-committee"},
            }
        ],
        "snapshots": [],
        "state": {},
        "source_kind": "capture_dir",
    }

    cycles, summary = replay.build_orchestration_cycles(
        profile=profile,
        window=window,
        bundle=bundle,
        seed=7,
    )
    assert summary["persisted_count"] == 1
    assert cycles[0].winning_agent == "committee.spread_microstructure"
    assert cycles[0].arbiter_stage == "entry_ranking"

    divergence_rows, metrics = replay.build_divergence_rows(
        baseline_history_rows=[
            {"pair": "EURUSD", "ts": "2026-03-20T12:00:00Z", "allowed": True, "side": "BUY", "lifecycle_action": "hold", "position_side": ""}
        ],
        adaptive_history_rows=[
            {"pair": "EURUSD", "ts": "2026-03-20T12:00:00Z", "allowed": True, "side": "BUY", "lifecycle_action": "hold", "position_side": ""}
        ],
        cycles=cycles,
        feature_contract_id="fxstack.test.v1",
    )
    assert divergence_rows[0]["winning_agent"] == "committee.spread_microstructure"
    assert divergence_rows[0]["arbiter_stage"] == "entry_ranking"
    assert metrics["command_divergence_rate"] == 1.0
