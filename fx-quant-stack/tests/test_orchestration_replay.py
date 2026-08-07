from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from fxstack.orchestration import replay


def _profile() -> replay.ReplayProfile:
    return replay.ReplayProfile(
        profile_id="unit",
        pairs=["EURUSD"],
        feature_contract_id="fxstack.test.v1",
        feature_root="research-inputs/raw",
        research_manifest_path="research-inputs/models/research_manifest.json",
        start_equity=10_000.0,
        slippage_bps=0.25,
        seed=42,
        reduce_fraction=0.5,
        research_validation_limit=10,
        orchestration_source={"kind": "capture_dir", "path": "research-inputs/orchestration"},
        thresholds=replay.ResearchThresholds(
            entry_ratio_floor=0.90,
            slot_utilisation_floor=0.90,
            trace_completeness_floor=0.99,
            action_overlap_floor=0.95,
            decision_divergence_rate_ceiling=0.05,
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


def test_build_divergence_rows_computes_expected_action_diagnostics() -> None:
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
    assert metrics["action_overlap_rate"] == 0.0
    assert metrics["decision_divergence_rate"] == 1.0
    assert metrics["baseline_policy_block_rate"] == 0.0
    assert metrics["orchestrated_policy_block_rate"] == 1.0


def test_simulate_orchestration_reconstruction_emits_positive_trade_metrics() -> None:
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

    aggregate, history, trace_summary = replay.simulate_orchestration_reconstruction(
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


@pytest.mark.parametrize("source_kind", ["database", "live", "api"])
def test_offline_source_rejects_connected_source_kinds(source_kind: str) -> None:
    profile = _profile()
    profile.orchestration_source = {"kind": source_kind, "path": "research-inputs/source.json"}

    with pytest.raises(ValueError, match="offline orchestration research source kind"):
        replay.load_source_bundle(profile=profile, window=profile.windows["calm"])


def test_offline_source_rejects_connection_fields() -> None:
    profile = _profile()
    profile.orchestration_source = {
        "kind": "immutable_bundle",
        "path": "research-inputs/source.json",
        "database_url": "sqlite:///runtime.db",
    }

    with pytest.raises(ValueError, match="forbid live connection fields"):
        replay.load_source_bundle(profile=profile, window=profile.windows["calm"])


def test_load_source_bundle_reads_explicit_immutable_json(tmp_path: Path) -> None:
    bundle_path = tmp_path / "orchestration-input.json"
    bundle_path.write_text(
        json.dumps(
            {
                "bundle": {
                    "runs": [
                        {
                            "run_id": "run-1",
                            "ts_utc": replay._utc_epoch("2026-03-20T12:00:00Z"),
                        },
                        {
                            "run_id": "run-2",
                            "ts_utc": replay._utc_epoch("2026-03-20T12:05:00Z"),
                        },
                    ],
                    "traces": [
                        {"run_id": "run-1", "trace": {"trace_id": "trace-1"}},
                        {"run_id": "run-2", "trace": {"trace_id": "trace-2"}},
                    ],
                    "snapshots": [
                        {"ts": replay._utc_epoch("2026-03-20T12:00:00Z")},
                        {"ts": replay._utc_epoch("2026-03-20T12:05:00Z")},
                    ],
                    "state": {"feature_contract_id": "fxstack.test.v1"},
                }
            }
        ),
        encoding="utf-8",
    )
    profile = _profile()
    profile.research_validation_limit = 1
    profile.orchestration_source = {"kind": "immutable_bundle", "path": str(bundle_path)}

    bundle = replay.load_source_bundle(profile=profile, window=profile.windows["calm"])

    assert len(bundle["runs"]) == 1
    assert bundle["runs"][0]["run_id"] == "run-1"
    assert bundle["traces"][0]["run_id"] == "run-1"
    assert bundle["source_kind"] == "immutable_bundle"
    assert bundle["source_path"] == str(bundle_path.resolve())
    assert bundle["research_validation_limit"] == 1
    assert bundle["source_item_counts"] == {"runs": 2, "traces": 2, "snapshots": 2}
    assert bundle["loaded_item_counts"] == {"runs": 1, "traces": 1, "snapshots": 1}


def test_default_research_profile_is_offline_and_advisory() -> None:
    profile = replay.load_replay_profile(replay.REPO_ROOT / replay.DEFAULT_PROFILE_PATH)

    assert profile.orchestration_source["kind"] in replay.OFFLINE_SOURCE_KINDS
    assert profile.feature_root == "research-inputs/raw"
    assert profile.research_manifest_path == "research-inputs/models/research_manifest.json"
    assert profile.research_validation_limit == 500


def test_research_adapter_passes_only_explicit_offline_inputs(tmp_path: Path, monkeypatch) -> None:
    profile = _profile()
    window = profile.windows["calm"]
    research_module = replay._load_research_tool()
    for key in research_module._FORBIDDEN_LIVE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FXSTACK_EXECUTION_PROVIDER", "offline")
    monkeypatch.setenv("FXSTACK_MARKET_DATA_PROVIDER", "offline")

    args = replay._build_research_args(
        research_mod=research_module,
        profile=profile,
        window=window,
        out_dir=tmp_path,
        exec_mode=research_module.STRICT_EXEC_MODE,
    )

    assert Path(args.raw_root) == (replay.REPO_ROOT / profile.feature_root).resolve()
    assert Path(args.manifest_path) == (replay.REPO_ROOT / profile.research_manifest_path).resolve()
    assert args.fill_delay_bars == 1
    assert not hasattr(args, "bridge_url")
    assert not hasattr(args, "live_api_key")
    assert not hasattr(args, "database_url")


def test_research_tool_refuses_connected_environment() -> None:
    research_module = replay._load_research_tool()

    with pytest.raises(RuntimeError, match="offline research refuses live endpoints"):
        research_module._assert_offline_environment(
            {
                "FXSTACK_DATABASE_URL": "sqlite:///runtime.db",
                "MT4_BRIDGE_URL": "http://127.0.0.1:58710",
            }
        )
