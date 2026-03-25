from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
import sys


REPO_ROOT = Path("/mnt/d/Development/Trading Agent")
TOOL_PATH = REPO_ROOT / "tools" / "fxstack_digital_twin_backtest.py"
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.backtest.adaptive_policy import (
    adaptive_lifecycle_decision,
    adaptive_reentry_block,
    adaptive_replacement_keep_score,
    adaptive_tempo_gap_active,
    evaluate_adaptive_entry,
)
from fxstack.settings import get_settings


def _load_module():
    spec = importlib.util.spec_from_file_location("fxstack_digital_twin_backtest_test_adaptive", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_adaptive_twin_smoke_outputs(tmp_path):
    mod = _load_module()
    out_dir = tmp_path / "adaptive_twin"
    args = Namespace(
        pairs="EURUSD,USDJPY",
        feature_root=str(REPO_ROOT / "fx-quant-stack" / "data" / "features"),
        start_equity=10000.0,
        slippage_bps=0.25,
        start_ts="2026-03-20",
        end_ts="2026-03-21",
        lifecycle_cache_pairs=4,
        out_dir=str(out_dir),
        validate_live_overlap=False,
        validation_limit=10,
        emit_decision_history=False,
        max_decision_history_rows=200,
        recommendations=True,
        exec_mode="adaptive_multi_playbook",
        adaptive_compare_baseline=True,
        adaptive_playbooks="trend_pullback,range_mean_reversion,breakout_expansion,failed_breakout_reversal",
        adaptive_entry_ratio_floor=0.90,
        adaptive_entry_ratio_cap=1.35,
        adaptive_slot_util_floor=0.90,
        adaptive_slot_util_cap=1.20,
        adaptive_aggressive_fallback_margin=0.08,
        adaptive_use_risk_multipliers=False,
        bridge_url="http://127.0.0.1:58710",
        live_api_key="",
        shadow_tier1_structure_rescue_margin=None,
        shadow_pair_aware_spread_caps=False,
        shadow_spread_cap_quantile=0.75,
        shadow_spread_cap_multiplier=1.25,
        shadow_spread_cap_max_bps=5.0,
    )
    result = mod.run_twin(args)
    aggregate = dict(result["aggregate"])

    assert aggregate["exec_mode"] == "adaptive_multi_playbook"
    assert "baseline_compare" in aggregate
    assert Path(result["environment_summary_path"]).exists()
    assert Path(result["playbook_summary_path"]).exists()
    assert Path(result["portfolio_crowding_summary_path"]).exists()
    assert Path(result["adaptive_baseline_comparison_path"]).exists()
    assert Path(result["adaptive_aggressiveness_guardrails_path"]).exists()
    assert Path(result["twin_validation_path"]).exists()
    assert Path(result["recent_live_comparison_path"]).exists()


def test_adaptive_entry_uses_aggressive_fallback_when_close_to_floor():
    settings = get_settings()
    decision = evaluate_adaptive_entry(
        row={
            "pair": "EURUSD",
            "side": "long",
            "signal_side": "long",
            "session_bucket": "london_open",
            "session_entry_blocked": False,
            "session_entry_block_reason": "",
            "spread_bps": 1.0,
            "uncertainty_score": 0.15,
            "playbook": "no_trade",
            "playbook_score": 0.0,
            "location_score": 0.72,
            "trigger_score": 0.68,
            "macro_coherence_score": 0.62,
            "environment_state": "PersistentTrend",
            "extreme_chase": False,
            "adaptive_base_rejection_reason": "low_playbook_score",
            "calibrated_ev_bps_shadow": settings.min_expected_edge_bps * 2.2,
        },
        strict_ready=True,
        open_positions={},
        settings=settings,
        fallback_margin=0.08,
    )

    assert decision["adaptive_allowed"] is True
    assert decision["aggressive_fallback_used"] is True
    assert decision["playbook"] == "trend_pullback"


def test_adaptive_entry_preserves_strict_fill_when_router_has_no_trade():
    settings = get_settings()
    decision = evaluate_adaptive_entry(
        row={
            "pair": "AUDUSD",
            "side": "long",
            "signal_side": "long",
            "baseline_rejection_reason": "none",
            "session_bucket": "asia",
            "session_entry_blocked": False,
            "session_entry_block_reason": "",
            "spread_bps": 1.1,
            "uncertainty_score": 0.10,
            "playbook": "no_trade",
            "playbook_score": 0.0,
            "location_score": 0.0,
            "trigger_score": 0.0,
            "macro_coherence_score": 0.50,
            "environment_state": "CompressionPreBreakout",
            "extreme_chase": False,
            "adaptive_base_rejection_reason": "low_playbook_score",
            "calibrated_ev_bps_shadow": settings.min_expected_edge_bps * 3.0,
        },
        strict_ready=True,
        open_positions={},
        settings=settings,
        fallback_margin=0.08,
    )

    assert decision["adaptive_allowed"] is True
    assert decision["aggressive_fallback_used"] is True
    assert decision["playbook"] == "breakout_expansion"


def test_adaptive_only_trade_requires_exceptional_quality():
    settings = get_settings()
    decision = evaluate_adaptive_entry(
        row={
            "pair": "NZDUSD",
            "side": "short",
            "signal_side": "short",
            "baseline_rejection_reason": "meta_reject",
            "session_bucket": "asia",
            "session_entry_blocked": False,
            "session_entry_block_reason": "",
            "spread_bps": 1.0,
            "uncertainty_score": 0.08,
            "playbook": "trend_pullback",
            "playbook_score": 0.74,
            "location_score": 0.62,
            "trigger_score": 0.83,
            "macro_coherence_score": 1.0,
            "environment_state": "PersistentTrend",
            "extreme_chase": False,
            "adaptive_base_rejection_reason": "approved",
            "calibrated_ev_bps_shadow": settings.min_expected_edge_bps * 3.0,
        },
        strict_ready=False,
        open_positions={},
        settings=settings,
        fallback_margin=0.08,
    )

    assert decision["adaptive_allowed"] is False
    assert decision["adaptive_rejection_reason"] == "adaptive_only_quality_gate"


def test_adaptive_reentry_block_prevents_same_side_churn():
    block = adaptive_reentry_block(
        pair="EURUSD",
        side="long",
        playbook="trend_pullback",
        bar_idx=104,
        exit_registry={
            "EURUSD": {
                "bar_idx": 100,
                "side": "long",
                "playbook": "trend_pullback",
                "reason": "adaptive_playbook_exit",
            }
        },
    )

    assert block["blocked"] is True
    assert block["reason"] == "adaptive_reentry_cooldown"


def test_adaptive_tempo_gap_detects_under_rotation():
    assert adaptive_tempo_gap_active(baseline_entries_so_far=12, adaptive_entries_so_far=6) is True
    assert adaptive_tempo_gap_active(baseline_entries_so_far=12, adaptive_entries_so_far=9) is False


def test_adaptive_replacement_keep_score_penalizes_baseline_floor_holds():
    weak = adaptive_replacement_keep_score(
        lifecycle_action="hold",
        lifecycle_reason="adaptive_hold_baseline_floor",
        playbook_score=0.40,
        location_score=0.35,
        trigger_score=0.30,
        entry_trade_prob=0.42,
        entry_macro_coherence_score=0.45,
        aggressive_fallback_used=False,
    )
    strong = adaptive_replacement_keep_score(
        lifecycle_action="hold",
        lifecycle_reason="adaptive_hold",
        playbook_score=0.75,
        location_score=0.70,
        trigger_score=0.68,
        entry_trade_prob=0.78,
        entry_macro_coherence_score=0.72,
        aggressive_fallback_used=False,
    )

    assert weak < strong


def test_adaptive_breakout_lifecycle_fails_fast():
    position = SimpleNamespace(
        playbook="breakout_expansion",
        open_equity_usd=10000.0,
        environment_state_at_entry="ExpansionBreakout",
        partial_count=0,
        last_partial_bar_index=None,
    )
    lifecycle = adaptive_lifecycle_decision(
        position=position,
        row={
            "playbook": "breakout_expansion",
            "playbook_score": 0.61,
            "location_score": 0.42,
            "trigger_score": 0.22,
            "hostility_score": 0.18,
            "macro_coherence_score": 0.57,
            "extension_penalty_score": 0.33,
            "environment_state": "ExpansionBreakout",
        },
        unrealized_pnl_usd=-15.0,
        age_bars=2.0,
        bar_idx=4,
        exit_action_probs={"hold": 0.20, "partial_tp": 0.05, "exit": 0.30},
        reversal_context_active=False,
        reversal_ready=False,
        reversal_failure_prob=0.0,
        reversal_opportunity_prob=0.0,
    )

    assert lifecycle["action"] == "exit"
    assert lifecycle["reason"] == "adaptive_breakout_follow_through_failed"


def test_trend_pullback_lifecycle_holds_through_early_noise():
    position = SimpleNamespace(
        playbook="trend_pullback",
        open_equity_usd=10000.0,
        environment_state_at_entry="CorrectiveTrend",
        partial_count=0,
        last_partial_bar_index=None,
    )
    lifecycle = adaptive_lifecycle_decision(
        position=position,
        row={
            "playbook": "trend_pullback",
            "playbook_score": 0.48,
            "location_score": 0.40,
            "trigger_score": 0.30,
            "hostility_score": 0.22,
            "macro_coherence_score": 0.58,
            "extension_penalty_score": 0.25,
            "environment_state": "CorrectiveTrend",
        },
        unrealized_pnl_usd=-20.0,
        age_bars=2.0,
        bar_idx=5,
        exit_action_probs={"hold": 0.18, "partial_tp": 0.10, "exit": 0.42},
        reversal_context_active=False,
        reversal_ready=False,
        reversal_failure_prob=0.0,
        reversal_opportunity_prob=0.0,
    )

    assert lifecycle["action"] == "hold"
    assert lifecycle["reason"] == "adaptive_hold_min_age"
