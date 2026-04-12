from __future__ import annotations

import importlib.util
from argparse import Namespace
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "fxstack_digital_twin_backtest.py"
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.runtime.runner import _overlay_inputs_for_decision
from fxstack.mlops.model_uri import normalize_artifact_ref
from fxstack.settings import get_settings
from fxstack.strategy.desk_overlay import build_desk_overlay


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


def _load_module():
    spec = importlib.util.spec_from_file_location("fxstack_digital_twin_backtest_test_overlay", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_shared_overlay_diagnostics_normalize_twin_and_runtime_style_rows() -> None:
    mod = _load_module()
    twin_style_rows = [
        {
            "pair": "EURUSD",
            "belief_source_mode": "artifact",
            "belief_overlay_adjustment": 0.08,
            "belief_primary_scenario": "trend_pullback",
        },
        {
            "pair": "USDJPY",
            "belief_source_mode": "artifact",
            "belief_overlay_adjustment": -0.06,
            "belief_primary_scenario": "failed_breakout_reversal",
        },
    ]
    runtime_style_rows = [
        {
            "metadata": {
                "pair": "EURUSD",
                "belief_source_mode": "artifact",
                "belief_overlay_adjustment": 0.08,
                "belief_primary_scenario": "trend_pullback",
            }
        },
        {
            "metadata": {
                "pair": "USDJPY",
                "belief_source_mode": "artifact",
                "belief_overlay_adjustment": -0.06,
                "belief_primary_scenario": "failed_breakout_reversal",
            }
        },
    ]

    twin_diag = mod._shared_overlay_diagnostics(twin_style_rows)
    runtime_diag = mod._shared_overlay_diagnostics([row["metadata"] for row in runtime_style_rows])

    assert twin_diag == runtime_diag
    assert twin_diag["overlay_enabled_rows"] == 2
    assert twin_diag["overlay_adjustment"]["positive_count"] == 1
    assert twin_diag["overlay_adjustment"]["negative_count"] == 1
    assert twin_diag["overlay_source_modes"]["artifact"] == 2
    assert twin_diag["overlay_primary_scenario_counts"]["trend_pullback"] == 1


def test_desk_overlay_inputs_match_between_twin_and_runtime_helpers() -> None:
    mod = _load_module()

    runtime_meta = {
        "adaptive_sleeve": "breakout_expansion",
        "adaptive_playbook": "breakout_expansion",
        "belief_primary_rank_score": 0.84,
        "belief_gap": 0.58,
        "belief_primary_ev_above_hurdle_prob": 0.74,
        "belief_primary_confirm_prob": 0.68,
        "belief_fragility_score": 0.16,
        "structure_timing_score": 0.71,
        "belief_primary_fail_fast_prob": 0.22,
        "belief_primary_expected_net_ev_bps": 8.6,
        "adaptive_entry_quality": 0.78,
        "adaptive_playbook_score": 0.8,
        "adaptive_location_score": 0.73,
        "adaptive_trigger_score": 0.76,
        "adaptive_hostility_score": 0.18,
        "campaign_state": "press",
        "campaign_proof_score": 0.82,
        "campaign_maturity_score": 0.61,
        "campaign_reset_quality": 0.69,
        "campaign_priority_boost": 0.08,
        "position_count_pair": 0,
        "belief_opposing_scenario": "failed_breakout_reversal",
    }
    sleeve_snapshot = SimpleNamespace(
        score=0.77,
        state="healthy",
        win_rate=0.61,
        expectancy_usd=36.0,
        profit_factor=1.31,
    )
    allocator_open_positions = [
        SimpleNamespace(keep_score=0.63),
        SimpleNamespace(keep_score=0.71),
    ]

    runtime_inputs = _overlay_inputs_for_decision(
        meta=dict(runtime_meta),
        current_row={
            "playbook_score": 0.8,
            "location_score": 0.73,
            "trigger_score": 0.76,
            "hostility_score": 0.18,
        },
        sleeve_snapshot=sleeve_snapshot,
        open_position_count=1,
        allocator_open_positions=allocator_open_positions,
        settings=SimpleNamespace(max_pair_positions=1, max_total_positions=4),
    )
    twin_inputs = mod._desk_overlay_inputs_for_action(
        action={
            "sleeve": "breakout_expansion",
            "entry_playbook": "breakout_expansion",
            "belief_primary_rank_score": 0.84,
            "belief_gap": 0.58,
            "belief_primary_ev_above_hurdle_prob": 0.74,
            "belief_primary_confirm_prob": 0.68,
            "belief_fragility_score": 0.16,
            "entry_structure_timing_score": 0.71,
            "belief_primary_fail_fast_prob": 0.22,
            "belief_primary_expected_net_ev_bps": 8.6,
            "adaptive_entry_quality": 0.78,
            "playbook_score": 0.8,
            "location_score": 0.73,
            "trigger_score": 0.76,
            "hostility_score": 0.18,
            "campaign_state": "press",
            "campaign_proof_score": 0.82,
            "campaign_maturity_score": 0.61,
            "campaign_reset_quality": 0.69,
            "campaign_priority_boost": 0.08,
            "belief_opposing_scenario": "failed_breakout_reversal",
        },
        sleeve_snapshot=sleeve_snapshot,
        open_position_count=1,
        allocator_open_positions=allocator_open_positions,
        settings=SimpleNamespace(max_pair_positions=1, max_total_positions=4),
    )

    runtime_out = build_desk_overlay(runtime_inputs)
    twin_out = build_desk_overlay(twin_inputs)

    assert runtime_out.conviction_band == twin_out.conviction_band
    assert runtime_out.thesis_stage == twin_out.thesis_stage
    assert runtime_out.portfolio_posture == twin_out.portfolio_posture
    assert round(runtime_out.conviction_score, 6) == round(twin_out.conviction_score, 6)
    assert runtime_out.sleeve_budget_guidance["breakout_expansion"].tilt == twin_out.sleeve_budget_guidance["breakout_expansion"].tilt


def test_cross_pair_admission_overlay_applies_prod_style_hard_gate_in_twin(monkeypatch) -> None:
    mod = _load_module()
    base_settings = get_settings()
    campaign_config = mod.campaign_config_from_settings(base_settings)
    settings = SimpleNamespace(
        max_allowed_spread_bps=base_settings.max_allowed_spread_bps,
        min_expected_edge_bps=base_settings.min_expected_edge_bps,
        strategy_engine_mode=getattr(base_settings, "strategy_engine_mode", "supervised_legacy"),
        belief_influence_mode="hard_gate",
    )

    def _action(pair: str) -> dict[str, object]:
        return {
            "pair": pair,
            "ts": "2026-04-07T12:00:00Z",
            "pos_snapshot": None,
            "baseline_allowed": True,
            "adaptive_allowed": True,
            "adaptive_entry_quality": 0.72,
            "adaptive_rejection_reason": "approved",
            "adaptive_eval": {
                "adaptive_allowed": True,
                "adaptive_entry_quality": 0.72,
                "adaptive_rejection_reason": "approved",
                "playbook": "trend_pullback",
            },
            "adaptive_eval_row": {
                "pair": pair,
                "side": "long",
                "signal_side": "long",
                "baseline_rejection_reason": "none",
                "session_bucket": "london_open",
                "session_entry_blocked": False,
                "session_entry_block_reason": "",
                "spread_bps": 0.9,
                "uncertainty_score": 0.12,
                "model_disagreement_score": 0.10,
                "playbook": "trend_pullback",
                "playbook_score": 0.72,
                "location_score": 0.70,
                "trigger_score": 0.71,
                "macro_coherence_score": 0.66,
                "environment_state": "PersistentTrend",
                "extreme_chase": False,
                "adaptive_base_rejection_reason": "approved",
                "calibrated_ev_bps_shadow": float(base_settings.min_expected_edge_bps * 2.0),
                "regime_prob": 0.7,
                "swing_prob": 0.7,
                "entry_prob": 0.72,
                "trade_prob": 0.74,
                "expected_edge_bps": float(base_settings.min_expected_edge_bps * 1.8),
                "structure_timing_score": 0.72,
                "extension_penalty_score": 0.14,
                "adaptive_entry_quality": 0.72,
            },
            "entry_hard_reasons": [],
            "entry_playbook": "trend_pullback",
            "sleeve": "trend_pullback",
            "playbook_score": 0.72,
            "location_score": 0.70,
            "trigger_score": 0.71,
            "entry_macro_coherence_score": 0.66,
            "macro_coherence_score": 0.66,
            "hostility_score": 0.12,
            "extension_penalty_score": 0.14,
            "trade_prob": 0.74,
            "side": "BUY",
            "ready": True,
            "decision_reasons": [],
            "campaign_state": "inactive",
            "campaign_state_reason": "",
            "campaign_seq": 0,
            "campaign_entry_kind": "",
            "campaign_proof_score": 0.0,
            "campaign_maturity_score": 0.0,
            "campaign_reset_quality": 0.0,
            "campaign_priority_boost": 0.0,
            "campaign_reentry_blocked": False,
        }

    pending_actions = [_action("EURUSD"), _action("GBPUSD"), _action("USDJPY")]
    collector_rows = [
        {"pair": str(action["pair"]), "allowed": True, "rejection_reason": "none", "rejection_reasons": [], "lifecycle_action": "entry", "lifecycle_reason": "entry_approved"}
        for action in pending_actions
    ]
    shadow_inputs = [
        {"execution_ready": True, "reasons": [], "metadata": {"pair": str(action["pair"]), "ts": str(action["ts"]), "entry_blocking_reasons": []}}
        for action in pending_actions
    ]

    monkeypatch.setattr(
        mod,
        "build_cross_pair_influence_records",
        lambda _rows: [
            SimpleNamespace(
                pair="EURUSD",
                ts="2026-04-07T12:00:00Z",
                rank_position=1,
                influence_score=0.91,
                recommendation_strength=0.94,
                influenced_by_pairs=["GBPUSD"],
                cross_pair_reason_codes=["local_edge", "peer_confluence"],
                source_mode="artifact",
            ),
            SimpleNamespace(
                pair="GBPUSD",
                ts="2026-04-07T12:00:00Z",
                rank_position=2,
                influence_score=0.84,
                recommendation_strength=0.88,
                influenced_by_pairs=["EURUSD"],
                cross_pair_reason_codes=["local_edge"],
                source_mode="artifact",
            ),
            SimpleNamespace(
                pair="USDJPY",
                ts="2026-04-07T12:00:00Z",
                rank_position=3,
                influence_score=0.14,
                recommendation_strength=0.18,
                influenced_by_pairs=["EURUSD", "GBPUSD"],
                cross_pair_reason_codes=["weak_cross_pair_signal"],
                source_mode="artifact",
            ),
        ],
    )

    summary = mod._apply_cross_pair_admission_overlay(
        pending_actions=pending_actions,
        collector_rows_for_bar=collector_rows,
        shadow_inputs_for_bar=shadow_inputs,
        open_positions={},
        exit_registry={},
        campaign_registry={},
        campaign_config=campaign_config,
        bar_idx=12,
        settings=settings,
        fallback_margin=0.08,
    )

    weak = next(action for action in pending_actions if action["pair"] == "USDJPY")
    assert summary["cross_pair_influence_mode"] == "hard_gate"
    assert summary["cross_pair_gated_count"] == 1
    assert weak["adaptive_allowed"] is False
    assert weak["adaptive_rejection_reason"] == "cross_pair_hard_gate"
    assert weak["ready"] is False
    assert weak["decision_reasons"] == ["cross_pair_hard_gate"]
    assert weak["cross_pair_hard_block"] is True
    assert collector_rows[2]["rejection_reason"] == "cross_pair_hard_gate"
    assert collector_rows[2]["lifecycle_action"] == "hold"
    assert shadow_inputs[2]["execution_ready"] is False
    assert shadow_inputs[2]["metadata"]["cross_pair_hard_block"] is True


def test_cross_pair_admission_overlay_reruns_reentry_block_after_quality_override(monkeypatch) -> None:
    mod = _load_module()
    base_settings = get_settings()
    campaign_config = mod.campaign_config_from_settings(base_settings)
    settings = SimpleNamespace(
        max_allowed_spread_bps=base_settings.max_allowed_spread_bps,
        min_expected_edge_bps=base_settings.min_expected_edge_bps,
        strategy_engine_mode=getattr(base_settings, "strategy_engine_mode", "supervised_legacy"),
        belief_influence_mode="soft_gate",
    )
    pending_actions = [
        {
            "pair": "EURUSD",
            "ts": "2026-04-07T12:00:00Z",
            "pos_snapshot": None,
            "baseline_allowed": False,
            "adaptive_allowed": False,
            "adaptive_entry_quality": 0.61,
            "adaptive_rejection_reason": "low_adaptive_quality",
            "adaptive_eval": {},
            "adaptive_eval_row": {
                "pair": "EURUSD",
                "side": "long",
                "signal_side": "long",
                "baseline_rejection_reason": "low_trade_prob",
                "session_bucket": "london_open",
                "session_entry_blocked": False,
                "session_entry_block_reason": "",
                "spread_bps": 0.9,
                "uncertainty_score": 0.08,
                "model_disagreement_score": 0.08,
                "playbook": "trend_pullback",
                "playbook_score": 0.74,
                "location_score": 0.71,
                "trigger_score": 0.72,
                "macro_coherence_score": 0.69,
                "environment_state": "PersistentTrend",
                "extreme_chase": False,
                "adaptive_base_rejection_reason": "low_adaptive_quality",
                "calibrated_ev_bps_shadow": float(base_settings.min_expected_edge_bps * 2.0),
                "regime_prob": 0.73,
                "swing_prob": 0.71,
                "entry_prob": 0.72,
                "trade_prob": 0.74,
                "expected_edge_bps": float(base_settings.min_expected_edge_bps * 1.8),
                "structure_timing_score": 0.72,
                "extension_penalty_score": 0.14,
                "adaptive_entry_quality": 0.61,
            },
            "entry_hard_reasons": [],
            "entry_playbook": "trend_pullback",
            "sleeve": "trend_pullback",
            "playbook_score": 0.74,
            "location_score": 0.71,
            "trigger_score": 0.72,
            "entry_macro_coherence_score": 0.69,
            "macro_coherence_score": 0.69,
            "hostility_score": 0.12,
            "extension_penalty_score": 0.14,
            "trade_prob": 0.74,
            "side": "BUY",
            "ready": False,
            "decision_reasons": ["low_adaptive_quality"],
            "campaign_state": "inactive",
            "campaign_state_reason": "",
            "campaign_seq": 0,
            "campaign_entry_kind": "",
            "campaign_proof_score": 0.0,
            "campaign_maturity_score": 0.0,
            "campaign_reset_quality": 0.0,
            "campaign_priority_boost": 0.0,
            "campaign_reentry_blocked": False,
        }
    ]
    collector_rows = [
        {
            "pair": "EURUSD",
            "allowed": False,
            "rejection_reason": "low_adaptive_quality",
            "rejection_reasons": ["low_adaptive_quality"],
            "lifecycle_action": "hold",
            "lifecycle_reason": "low_adaptive_quality",
        }
    ]
    shadow_inputs = [
        {
            "execution_ready": False,
            "reasons": ["low_adaptive_quality"],
            "metadata": {
                "pair": "EURUSD",
                "ts": "2026-04-07T12:00:00Z",
                "entry_blocking_reasons": ["low_adaptive_quality"],
            },
        }
    ]

    monkeypatch.setattr(
        mod,
        "build_cross_pair_influence_records",
        lambda _rows: [
            SimpleNamespace(
                pair="EURUSD",
                ts="2026-04-07T12:00:00Z",
                rank_position=1,
                influence_score=0.93,
                recommendation_strength=0.95,
                influenced_by_pairs=[],
                cross_pair_reason_codes=["local_edge"],
                source_mode="artifact",
            )
        ],
    )
    monkeypatch.setattr(
        mod,
        "_evaluate_adaptive_entry_with_quality_override",
        lambda **_: {
            "adaptive_allowed": True,
            "adaptive_entry_quality": 0.73,
            "adaptive_rejection_reason": "approved",
            "playbook": "trend_pullback",
        },
    )
    monkeypatch.setattr(
        mod,
        "evaluate_entry_campaign_memory",
        lambda **_: SimpleNamespace(
            thesis_id="thesis-eurusd",
            campaign_seq=1,
            entry_kind="probe",
            state="probe",
            state_reason="",
            proof_score=0.4,
            maturity_score=0.3,
            reset_quality=0.2,
            priority_boost=0.0,
            reentry_blocked=False,
            reentry_block_reason="",
        ),
    )
    monkeypatch.setattr(
        mod,
        "adaptive_reentry_block",
        lambda **_: {"blocked": True, "reason": "adaptive_reentry_cooldown"},
    )

    mod._apply_cross_pair_admission_overlay(
        pending_actions=pending_actions,
        collector_rows_for_bar=collector_rows,
        shadow_inputs_for_bar=shadow_inputs,
        open_positions={},
        exit_registry={},
        campaign_registry={},
        campaign_config=campaign_config,
        bar_idx=12,
        settings=settings,
        fallback_margin=0.08,
    )

    action = pending_actions[0]
    assert action["adaptive_allowed"] is False
    assert action["adaptive_rejection_reason"] == "adaptive_reentry_cooldown"
    assert action["ready"] is False
    assert collector_rows[0]["rejection_reason"] == "adaptive_reentry_cooldown"
    assert shadow_inputs[0]["metadata"]["campaign_seq"] == 1


def test_adaptive_twin_exports_shared_overlay_diagnostics(tmp_path) -> None:
    _require_twin_smoke_assets(pairs=["EURUSD", "USDJPY"])
    mod = _load_module()
    out_dir = tmp_path / "adaptive_twin_overlay"
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
        recommendations=False,
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

    assert "shared_overlay_diagnostics" in result["aggregate"]
    assert "overlay_adjustment" in result["shared_overlay_diagnostics"]
    assert Path(result["belief_overlay_comparison_path"]).exists()
