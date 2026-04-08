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
