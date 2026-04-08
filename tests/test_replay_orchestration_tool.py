from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

from fxstack.orchestration import replay


def _write_history(path: Path, rows: list[dict[str, object]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_run_window_replay_writes_phase3_artifacts(tmp_path, monkeypatch) -> None:
    profile = replay.ReplayProfile(
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
    window = profile.windows["calm"]

    baseline_history_path = tmp_path / "baseline_history.csv.gz"
    adaptive_history_path = tmp_path / "adaptive_history.csv.gz"
    _write_history(
        baseline_history_path,
        [
            {
                "pair": "EURUSD",
                "ts": "2026-03-20T12:00:00Z",
                "allowed": True,
                "side": "BUY",
                "lifecycle_action": "hold",
                "position_side": "",
            }
        ],
    )
    _write_history(
        adaptive_history_path,
        [
            {
                "pair": "EURUSD",
                "ts": "2026-03-20T12:00:00Z",
                "allowed": True,
                "side": "BUY",
                "lifecycle_action": "hold",
                "position_side": "",
            }
        ],
    )

    monkeypatch.setattr(
        replay,
        "run_baseline_and_adaptive_lanes",
        lambda **kwargs: (
            {
                "aggregate": {
                    "entries": 1,
                    "trades": 1,
                    "net_pnl_usd": 50.0,
                    "max_drawdown_pct": 1.0,
                    "win_rate": 1.0,
                    "slot_utilization_rate": 0.25,
                },
                "decision_history_path": baseline_history_path,
            },
            {
                "aggregate": {
                    "entries": 1,
                    "trades": 1,
                    "net_pnl_usd": 40.0,
                    "max_drawdown_pct": 1.1,
                    "win_rate": 1.0,
                    "slot_utilization_rate": 0.25,
                },
                "decision_history_path": adaptive_history_path,
            },
        ),
    )
    monkeypatch.setattr(
        replay,
        "load_source_bundle",
        lambda **kwargs: {
            "runs": [
                {
                    "run_id": "run-1",
                    "pair": "EURUSD",
                    "ts_utc": replay._utc_epoch("2026-03-20T12:00:00Z"),
                    "packet_json": {
                        "pair": "EURUSD",
                        "ts_utc": "2026-03-20T12:00:00Z",
                        "baseline_action": {"action": "enter", "side": "BUY"},
                        "shadow_action": {"action": "enter", "side": "BUY"},
                        "proposal_votes": {"total": 1},
                        "proposals": [{"agent_id": "signal_agent", "intent": "enter", "side": "BUY"}],
                        "governed_decision": {"selected_action": "enter", "blocking_reasons": [], "command_preview": {"lots": 0.1}},
                        "divergence_reason": "agree",
                        "latency_ms": 21,
                        "fallback_used": False,
                    },
                }
            ],
            "traces": [{"run_id": "run-1", "trace_json": {"trace_id": "trace-1"}}],
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
                                    "run_id": "run-1",
                                    "trace_id": "trace-1",
                                    "baseline_action": {"action": "enter", "side": "BUY"},
                                    "shadow_action": {"action": "enter", "side": "BUY"},
                                },
                            },
                        }
                    ],
                }
            ],
            "state": {},
            "source_kind": "capture_dir",
        },
    )
    monkeypatch.setattr(
        replay,
        "_load_price_lookup",
        lambda **kwargs: {
            "EURUSD": {
                replay._utc_iso("2026-03-20T12:00:00Z"): {"bid": 1.1000, "ask": 1.1002, "mid": 1.1001},
            }
        },
    )

    result = replay.run_window_replay(
        profile=profile,
        window=window,
        experiment_id="exp-unit",
        output_root=tmp_path,
        seed=42,
    )

    window_dir = tmp_path / "exp-unit" / "calm"
    assert (window_dir / "aggregate.json").exists()
    assert (window_dir / "guardrails.json").exists()
    assert (window_dir / "divergence.csv").exists()
    assert (window_dir / "proposal_votes.json").exists()
    assert (window_dir / "promotion_pack.md").exists()
    assert (window_dir / "config.json").exists()
    assert (window_dir / "baseline").exists()
    assert (window_dir / "adaptive").exists()
    assert (window_dir / "orchestrated_shadow").exists()

    aggregate = json.loads((window_dir / "aggregate.json").read_text(encoding="utf-8"))
    assert aggregate["resolved_config"]["feature_contract_id"] == "fxstack.test.v1"
    assert aggregate["comparison"]["comparable_cycle_count"] == 1
    assert aggregate["window_status"]["status"] in {"GO", "HOLD"}

    guardrails = json.loads((window_dir / "guardrails.json").read_text(encoding="utf-8"))
    assert "entry_ratio_floor" in guardrails["checks"]

    proposal_votes = json.loads((window_dir / "proposal_votes.json").read_text(encoding="utf-8"))
    assert proposal_votes["total"] == 1
    assert result["window_id"] == "calm"

