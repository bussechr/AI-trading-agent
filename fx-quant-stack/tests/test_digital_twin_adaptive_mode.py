from __future__ import annotations

import importlib.util
from argparse import Namespace
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from fxstack.mlops.model_uri import normalize_artifact_ref


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "fxstack_digital_twin_backtest.py"
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))


def _smoke_artifact_path(value: object) -> str:
    return str(normalize_artifact_ref(value).get("path") or "").strip()


def _require_twin_smoke_assets(*, pairs: list[str]) -> None:
    manifest_path = REPO_ROOT / "fx-quant-stack" / "artifacts" / "active_models.json"
    feature_root = REPO_ROOT / "fx-quant-stack" / "data" / "features"
    if not manifest_path.exists():
        pytest.skip("digital twin smoke test requires a local active model manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    active = dict(manifest.get("active_model_sets") or {})
    for pair in pairs:
        feature_pair_root = feature_root / "provider=dukascopy" / f"pair={pair}"
        if not feature_pair_root.exists():
            pytest.skip(f"digital twin smoke test requires local feature data for {pair}")
        item = dict(active.get(pair, {}) or {})
        if not item:
            pytest.skip(f"digital twin smoke test requires an activated model set for {pair}")
        artifacts = dict(item.get("artifacts") or {})
        for key in ["regime", "meta", "swing_xgb", "intraday_xgb"]:
            rel = _smoke_artifact_path(artifacts.get(key))
            if not rel or not (REPO_ROOT / rel).exists():
                pytest.skip(f"digital twin smoke test requires local artifact '{key}' for {pair}")

from fxstack.backtest.adaptive_policy import (
    adaptive_lifecycle_decision,
    adaptive_reentry_block,
    adaptive_replacement_keep_score,
    adaptive_tempo_gap_active,
    evaluate_adaptive_entry,
)
from fxstack.settings import get_settings
from fxstack.strategy.allocator import (
    allocate_candidates,
    build_allocator_candidate,
    playbook_to_sleeve,
)
from fxstack.strategy.allocator_types import AllocatorConfig, AllocatorOpenPosition
from fxstack.strategy.campaign import (
    CAMPAIGN_STATE_ABANDONED,
    CAMPAIGN_STATE_CONFIRMED,
    CAMPAIGN_STATE_INACTIVE,
    CAMPAIGN_STATE_PRESS,
    CAMPAIGN_STATE_PROBE,
    CAMPAIGN_STATE_REATTACK_READY,
    campaign_config_from_settings,
    campaign_state_after_close,
    evaluate_entry_campaign_memory,
    evaluate_open_campaign,
    start_campaign_on_entry,
)
from fxstack.strategy.campaign_types import CampaignRegistryEntry
from fxstack.strategy.sleeve_governance import SleeveGovernanceTracker


def _load_module():
    spec = importlib.util.spec_from_file_location("fxstack_digital_twin_backtest_test_adaptive", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_adaptive_twin_smoke_outputs(tmp_path):
    _require_twin_smoke_assets(pairs=["EURUSD", "USDJPY"])
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
        belief_overlay=True,
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
    assert Path(result["allocator_summary_path"]).exists()
    assert Path(result["sleeve_health_summary_path"]).exists()
    assert Path(result["replacement_summary_path"]).exists()
    assert Path(result["campaign_summary_path"]).exists()
    assert Path(result["campaign_state_summary_path"]).exists()
    assert Path(result["belief_summary_path"]).exists()
    assert Path(result["belief_deciles_path"]).exists()
    assert Path(result["belief_overlay_comparison_path"]).exists()
    assert Path(result["belief_decision_history_path"]).exists()
    assert Path(result["hypothesis_rows_path"]).exists()
    assert Path(result["thesis_campaigns_path"]).exists()
    assert Path(result["allocator_decision_history_path"]).exists()
    assert Path(result["adaptive_baseline_comparison_path"]).exists()
    assert Path(result["adaptive_aggressiveness_guardrails_path"]).exists()
    assert Path(result["twin_validation_path"]).exists()
    assert Path(result["recent_live_comparison_path"]).exists()
    assert "top_ev_prob_quintile_expectancy_usd" in result["belief_summary"]
    assert "ev_above_hurdle_prob" in result["belief_deciles"]


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
            "model_disagreement_score": 0.12,
            "playbook": "no_trade",
            "playbook_score": 0.0,
            "location_score": 0.72,
            "trigger_score": 0.68,
            "macro_coherence_score": 0.62,
            "regime_prob": 0.66,
            "swing_prob": 0.68,
            "entry_prob": 0.64,
            "trade_prob": 0.67,
            "expected_edge_bps": settings.min_expected_edge_bps * 1.2,
            "structure_timing_score": 0.71,
            "extension_penalty_score": 0.18,
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
    assert decision["fallback_used"] is True
    assert decision["fallback_reason"] == "aggressive_fallback"
    assert decision["strategy_engine_mode"] == "supervised_legacy"
    assert decision["playbook"] == "trend_pullback"
    assert float(decision["model_intelligence_score"]) > float(decision["heuristic_penalty_score"])
    assert decision["decision_source_chain"][0] == "strategy_engine_mode:supervised_legacy"
    assert decision["decision_source_chain"][-1] == "fallback:aggressive_fallback"


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
            "model_disagreement_score": 0.10,
            "playbook": "no_trade",
            "playbook_score": 0.0,
            "location_score": 0.0,
            "trigger_score": 0.0,
            "macro_coherence_score": 0.50,
            "regime_prob": 0.70,
            "swing_prob": 0.72,
            "entry_prob": 0.68,
            "trade_prob": 0.69,
            "expected_edge_bps": settings.min_expected_edge_bps * 1.5,
            "structure_timing_score": 0.69,
            "extension_penalty_score": 0.16,
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
    assert decision["fallback_used"] is True
    assert decision["fallback_reason"] == "aggressive_fallback"
    assert decision["strategy_engine_mode"] == "supervised_legacy"
    assert decision["playbook"] == "breakout_expansion"
    assert float(decision["model_intelligence_score"]) > float(decision["heuristic_penalty_score"])
    assert decision["decision_source_chain"][0] == "strategy_engine_mode:supervised_legacy"
    assert decision["decision_source_chain"][-1] == "fallback:aggressive_fallback"


def test_adaptive_entry_honors_scorer_quality_proxy_for_no_trade_playbook():
    settings = get_settings()
    decision = evaluate_adaptive_entry(
        row={
            "pair": "EURUSD",
            "side": "long",
            "signal_side": "long",
            "baseline_rejection_reason": "low_playbook_score",
            "session_bucket": "london_open",
            "session_entry_blocked": False,
            "session_entry_block_reason": "",
            "spread_bps": 0.9,
            "uncertainty_score": 0.10,
            "model_disagreement_score": 0.10,
            "playbook": "no_trade",
            "playbook_score": 0.0,
            "location_score": 0.68,
            "trigger_score": 0.63,
            "macro_coherence_score": 0.64,
            "regime_prob": 0.55,
            "swing_prob": 0.56,
            "entry_prob": 0.54,
            "trade_prob": 0.55,
            "expected_edge_bps": settings.min_expected_edge_bps * 1.25,
            "structure_timing_score": 0.66,
            "extension_penalty_score": 0.14,
            "environment_state": "CompressionPreBreakout",
            "extreme_chase": False,
            "adaptive_base_rejection_reason": "low_playbook_score",
            "adaptive_entry_quality": 0.86,
            "entry_quality_score_shadow": 0.86,
            "calibrated_ev_bps_shadow": settings.min_expected_edge_bps * 2.0,
        },
        strict_ready=True,
        open_positions={},
        settings=settings,
        fallback_margin=0.08,
    )

    assert decision["adaptive_allowed"] is True
    assert decision["adaptive_rejection_reason"] == "approved"
    assert decision["aggressive_fallback_used"] is False
    assert decision["adaptive_entry_quality_source"] == "adaptive_entry_quality"
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
            "model_disagreement_score": 0.08,
            "playbook": "trend_pullback",
            "playbook_score": 0.74,
            "location_score": 0.62,
            "trigger_score": 0.83,
            "macro_coherence_score": 1.0,
            "regime_prob": 0.76,
            "swing_prob": 0.78,
            "entry_prob": 0.75,
            "trade_prob": 0.77,
            "expected_edge_bps": settings.min_expected_edge_bps * 3.0,
            "structure_timing_score": 0.72,
            "extension_penalty_score": 0.12,
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
    assert decision["adaptive_rejection_reason"] in {"adaptive_only_quality_gate", "low_adaptive_quality"}
    assert float(decision["model_intelligence_score"]) > 0.0


def test_adaptive_entry_reflects_non_legacy_strategy_engine_mode():
    class Settings:
        strategy_engine_mode = "hybrid_candidate"
        min_expected_edge_bps = 3.0
        max_allowed_spread_bps = 2.5
        adaptive_entry_quality_floor = 0.52
        adaptive_aggressive_fallback_margin = 0.08

    decision = evaluate_adaptive_entry(
        row={
            "pair": "EURUSD",
            "side": "long",
            "signal_side": "long",
            "session_bucket": "london_open",
            "session_entry_blocked": False,
            "session_entry_block_reason": "",
            "spread_bps": 1.0,
            "uncertainty_score": 0.12,
            "model_disagreement_score": 0.10,
            "playbook": "no_trade",
            "playbook_score": 0.0,
            "location_score": 0.72,
            "trigger_score": 0.68,
            "macro_coherence_score": 0.62,
            "regime_prob": 0.66,
            "swing_prob": 0.68,
            "entry_prob": 0.64,
            "trade_prob": 0.67,
            "expected_edge_bps": 6.0,
            "structure_timing_score": 0.71,
            "extension_penalty_score": 0.18,
            "environment_state": "PersistentTrend",
            "extreme_chase": False,
            "adaptive_base_rejection_reason": "low_playbook_score",
            "calibrated_ev_bps_shadow": 6.0,
        },
        strict_ready=True,
        open_positions={},
        settings=Settings(),
        fallback_margin=0.08,
    )

    assert decision["strategy_engine_mode"] == "hybrid_candidate"
    assert decision["fallback_reason"] == "hybrid_candidate:aggressive_fallback"
    assert decision["decision_source_chain"][0] == "strategy_engine_mode:hybrid_candidate"
    assert decision["decision_source_chain"][-1] == "fallback:hybrid_candidate:aggressive_fallback"
    assert decision["adaptive_allowed"] is True


def test_adaptive_entry_recovers_high_conviction_no_order_required_baseline() -> None:
    settings = get_settings()
    decision = evaluate_adaptive_entry(
        row={
            "pair": "CHFJPY",
            "side": "long",
            "signal_side": "long",
            "baseline_rejection_reason": "no_order_required",
            "session_bucket": "london_open",
            "session_entry_blocked": False,
            "session_entry_block_reason": "",
            "spread_bps": 1.2,
            "uncertainty_score": 0.10,
            "model_disagreement_score": 0.08,
            "playbook": "trend_pullback",
            "playbook_score": 0.80,
            "location_score": 0.69,
            "trigger_score": 0.81,
            "macro_coherence_score": 0.74,
            "regime_prob": 0.74,
            "swing_prob": 0.76,
            "entry_prob": 0.71,
            "trade_prob": 0.66,
            "expected_edge_bps": settings.min_expected_edge_bps * 2.0,
            "structure_timing_score": 0.72,
            "extension_penalty_score": 0.08,
            "environment_state": "PersistentTrend",
            "extreme_chase": False,
            "adaptive_base_rejection_reason": "low_adaptive_quality",
            "calibrated_ev_bps_shadow": settings.min_expected_edge_bps * 2.0,
        },
        strict_ready=False,
        open_positions={},
        settings=settings,
        fallback_margin=0.08,
    )

    assert decision["adaptive_allowed"] is True
    assert decision["aggressive_fallback_used"] is False
    assert decision["fallback_reason"] == "none"
    assert decision["adaptive_rejection_reason"] == "approved"


def test_adaptive_entry_does_not_rescue_when_model_intelligence_is_too_weak() -> None:
    settings = get_settings()
    decision = evaluate_adaptive_entry(
        row={
            "pair": "USDCHF",
            "side": "long",
            "signal_side": "long",
            "baseline_rejection_reason": "none",
            "session_bucket": "london_open",
            "session_entry_blocked": False,
            "session_entry_block_reason": "",
            "spread_bps": 0.9,
            "uncertainty_score": 0.09,
            "model_disagreement_score": 0.06,
            "playbook": "no_trade",
            "playbook_score": 0.95,
            "location_score": 0.94,
            "trigger_score": 0.96,
            "macro_coherence_score": 0.97,
            "regime_prob": 0.18,
            "swing_prob": 0.20,
            "entry_prob": 0.19,
            "trade_prob": 0.21,
            "expected_edge_bps": settings.min_expected_edge_bps * 0.4,
            "structure_timing_score": 0.64,
            "extension_penalty_score": 0.08,
            "environment_state": "PersistentTrend",
            "extreme_chase": False,
            "adaptive_base_rejection_reason": "low_playbook_score",
            "calibrated_ev_bps_shadow": settings.min_expected_edge_bps * 0.4,
        },
        strict_ready=True,
        open_positions={},
        settings=settings,
        fallback_margin=0.08,
    )

    assert decision["adaptive_allowed"] is False
    assert decision["aggressive_fallback_used"] is False
    assert decision["fallback_used"] is False
    assert decision["fallback_reason"] == "none"
    assert decision["adaptive_rejection_reason"] == "low_playbook_score"


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


def test_adaptive_breakout_lifecycle_holds_when_feature_bar_is_stale():
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
            "feature_bar": {"stale": True, "reason": "stale_feature_bar"},
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

    assert lifecycle["action"] == "hold"
    assert lifecycle["reason"] == "stale_feature_bar"


def test_adaptive_breakout_lifecycle_holds_when_adaptive_row_is_partial():
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
            "trigger_score": 0.22,
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

    assert lifecycle["action"] == "hold"
    assert lifecycle["reason"] == "adaptive_row_partial"


def test_campaign_trend_pullback_uses_memory_then_fill_time_probe():
    settings = get_settings()
    config = campaign_config_from_settings(settings)
    config.enabled = True
    registry: dict[str, CampaignRegistryEntry] = {}
    memory = evaluate_entry_campaign_memory(
        pair="EURUSD",
        side="long",
        sleeve="trend_pullback",
        row={
            "playbook_score": 0.72,
            "location_score": 0.66,
            "trigger_score": 0.61,
            "macro_coherence_score": 0.64,
            "hostility_score": 0.12,
            "extension_penalty_score": 0.28,
            "environment_state": "CorrectiveTrend",
        },
        bar_idx=10,
        ts="2026-03-20T10:00:00Z",
        registry=registry,
        config=config,
    )
    assert memory.state == CAMPAIGN_STATE_INACTIVE

    entry = start_campaign_on_entry(
        pair="EURUSD",
        side="long",
        sleeve="trend_pullback",
        row={
            "playbook_score": 0.72,
            "location_score": 0.66,
            "trigger_score": 0.61,
            "macro_coherence_score": 0.64,
            "hostility_score": 0.12,
            "extension_penalty_score": 0.28,
            "environment_state": "CorrectiveTrend",
        },
        bar_idx=10,
        ts="2026-03-20T10:00:00Z",
        registry=registry,
        prior_snapshot=memory,
    )
    assert entry.state == CAMPAIGN_STATE_PROBE
    assert entry.entry_kind == "fresh_probe"
    assert entry.campaign_seq == 1

    confirmed = evaluate_open_campaign(
        pair="EURUSD",
        side="long",
        sleeve="trend_pullback",
        current_state=CAMPAIGN_STATE_PROBE,
        row={
            "playbook_score": 0.78,
            "location_score": 0.71,
            "trigger_score": 0.59,
            "macro_coherence_score": 0.63,
            "hostility_score": 0.10,
            "extension_penalty_score": 0.30,
            "environment_state": "CorrectiveTrend",
        },
        unrealized_pnl_usd=22.0,
        age_bars=2.0,
        open_equity_usd=10_000.0,
        bar_idx=12,
        ts="2026-03-20T10:10:00Z",
        lifecycle_action="hold",
        lifecycle_reason="adaptive_hold",
        reversal_ready=False,
        severe_invalidation=False,
        config=config,
        campaign_seq=entry.campaign_seq,
        entry_kind=entry.entry_kind,
    )
    assert confirmed.state == CAMPAIGN_STATE_CONFIRMED

    press = evaluate_open_campaign(
        pair="EURUSD",
        side="long",
        sleeve="trend_pullback",
        current_state=CAMPAIGN_STATE_CONFIRMED,
        row={
            "playbook_score": 0.90,
            "location_score": 0.82,
            "trigger_score": 0.79,
            "macro_coherence_score": 0.76,
            "hostility_score": 0.05,
            "extension_penalty_score": 0.25,
            "environment_state": "PersistentTrend",
        },
        unrealized_pnl_usd=260.0,
        age_bars=4.0,
        open_equity_usd=10_000.0,
        bar_idx=14,
        ts="2026-03-20T10:20:00Z",
        lifecycle_action="hold",
        lifecycle_reason="adaptive_hold",
        reversal_ready=False,
        severe_invalidation=False,
        config=config,
        campaign_seq=entry.campaign_seq,
        entry_kind=entry.entry_kind,
    )
    assert press.state == CAMPAIGN_STATE_PRESS


def test_campaign_non_trend_sleeves_participate_in_lifecycle_memory():
    config = campaign_config_from_settings(get_settings())
    config.enabled = True
    memory = evaluate_entry_campaign_memory(
        pair="GBPUSD",
        side="long",
        sleeve="breakout_expansion",
        row={
            "playbook_score": 0.58,
            "location_score": 0.43,
            "trigger_score": 0.18,
            "macro_coherence_score": 0.52,
            "hostility_score": 0.18,
            "extension_penalty_score": 0.36,
            "environment_state": "ExpansionBreakout",
        },
        bar_idx=8,
        ts="2026-03-20T11:00:00Z",
        registry={},
        config=config,
    )
    assert memory.state == CAMPAIGN_STATE_INACTIVE

    open_state = evaluate_open_campaign(
        pair="GBPUSD",
        side="long",
        sleeve="breakout_expansion",
        current_state=CAMPAIGN_STATE_PROBE,
        row={
            "playbook_score": 0.58,
            "location_score": 0.43,
            "trigger_score": 0.18,
            "macro_coherence_score": 0.52,
            "hostility_score": 0.18,
            "extension_penalty_score": 0.36,
            "environment_state": "ExpansionBreakout",
        },
        unrealized_pnl_usd=-35.0,
        age_bars=2.0,
        open_equity_usd=10_000.0,
        bar_idx=8,
        ts="2026-03-20T11:00:00Z",
        lifecycle_action="exit",
        lifecycle_reason="adaptive_breakout_follow_through_failed",
        reversal_ready=False,
        severe_invalidation=True,
        config=config,
        campaign_seq=0,
        entry_kind="",
    )
    assert open_state.state == CAMPAIGN_STATE_ABANDONED
    assert open_state.state_reason == "campaign_probe_abandoned"


def test_campaign_harvest_close_can_become_reattack_ready():
    config = campaign_config_from_settings(get_settings())
    config.enabled = True
    close = campaign_state_after_close(
        position_state="harvest",
        pair="AUDUSD",
        side="long",
        sleeve="trend_pullback",
        row={
            "playbook_score": 0.74,
            "location_score": 0.73,
            "trigger_score": 0.67,
            "macro_coherence_score": 0.61,
            "hostility_score": 0.10,
            "extension_penalty_score": 0.28,
            "environment_state": "CorrectiveTrend",
        },
        lifecycle_reason="adaptive_campaign_harvest",
        realized_pnl_usd=84.0,
        bar_idx=21,
        ts="2026-03-20T12:00:00Z",
        config=config,
        campaign_seq=2,
        entry_kind="fresh_probe",
    )
    assert close.state == CAMPAIGN_STATE_REATTACK_READY


def test_allocator_prefers_lower_crowding_candidate_when_quality_is_close():
    config = AllocatorConfig(
        max_total_positions=6,
        max_pair_positions=1,
        max_new_entries=1,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
    )
    sleeve_tracker = SleeveGovernanceTracker(sleeves=[playbook_to_sleeve("trend_pullback")])
    snapshot = sleeve_tracker.snapshot()[playbook_to_sleeve("trend_pullback")]
    crowded = build_allocator_candidate(
        candidate_id="EURUSD",
        index=0,
        pair="EURUSD",
        ts="2026-03-20T10:00:00Z",
        side="BUY",
        sleeve=playbook_to_sleeve("trend_pullback"),
        environment_state="PersistentTrend",
        session_bucket="london",
        baseline_allowed=True,
        adaptive_allowed=True,
        playbook_score=0.70,
        location_score=0.66,
        trigger_score=0.61,
        adaptive_entry_quality=0.71,
        expected_edge_bps=8.2,
        uncertainty_score=0.10,
        spread_bps=1.0,
        max_spread_bps=2.5,
        macro_coherence_score=0.65,
        currency_crowding_penalty=0.40,
        playbook_diversification_penalty=0.0,
        config=config,
        open_positions=[],
        sleeve_health=snapshot,
    )
    cleaner = build_allocator_candidate(
        candidate_id="GBPJPY",
        index=1,
        pair="GBPJPY",
        ts="2026-03-20T10:00:00Z",
        side="BUY",
        sleeve=playbook_to_sleeve("trend_pullback"),
        environment_state="PersistentTrend",
        session_bucket="london",
        baseline_allowed=True,
        adaptive_allowed=True,
        playbook_score=0.69,
        location_score=0.65,
        trigger_score=0.61,
        adaptive_entry_quality=0.70,
        expected_edge_bps=8.0,
        uncertainty_score=0.10,
        spread_bps=1.0,
        max_spread_bps=2.5,
        macro_coherence_score=0.65,
        currency_crowding_penalty=0.05,
        playbook_diversification_penalty=0.0,
        config=config,
        open_positions=[],
        sleeve_health=snapshot,
    )
    ranked, summary = allocate_candidates(
        candidates=[crowded, cleaner],
        open_positions=[],
        remaining_slots=1,
        config=config,
        tempo_gap_active=False,
    )
    assert summary.selected_count == 1
    assert ranked[0].pair == "GBPJPY"
    assert ranked[0].allocator_selected is True
    assert ranked[1].allocator_selected is False


def test_sleeve_governance_degrades_materially_negative_sleeve():
    tracker = SleeveGovernanceTracker(sleeves=[playbook_to_sleeve("breakout_expansion")], max_trades=16)
    sleeve = playbook_to_sleeve("breakout_expansion")
    for idx in range(6):
        tracker.record_trade(
            sleeve=sleeve,
            realized_pnl_usd=-25.0 if idx < 5 else 5.0,
            holding_bars=3.0,
            partial_exit_events=0,
            close_reason="adaptive_playbook_exit",
            session_bucket="asia",
            pair="AUDUSD",
        )
    snap = tracker.snapshot()[sleeve]
    assert snap.state in {"watch", "degraded"}
    assert snap.expectancy_usd < 0.0


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
