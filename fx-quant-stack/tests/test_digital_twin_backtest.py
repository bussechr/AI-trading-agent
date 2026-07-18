from __future__ import annotations

import importlib.util
from argparse import Namespace
import json
from pathlib import Path
import sys

import pytest

from fxstack.belief.engine import validate_directional_belief_artifact_contract
from fxstack.models.artifact_contract import validate_artifact_contract


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "fxstack_digital_twin_backtest.py"
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.mlops.model_uri import normalize_artifact_ref


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
        feature_schema = dict(
            item.get("feature_schema")
            or (item.get("metadata") or {}).get("feature_schema")
            or {}
        )
        for key in ["regime", "meta", "swing_xgb", "intraday_xgb"]:
            rel = _smoke_artifact_path(artifacts.get(key))
            if not rel or not (REPO_ROOT / rel).exists():
                pytest.skip(f"digital twin smoke test requires local artifact '{key}' for {pair}")
        for key in [
            "regime",
            "meta",
            "swing_transformer",
            "swing_xgb",
            "intraday_tcn",
            "intraday_xgb",
            "exit_policy",
            "reversal_failure",
            "reversal_opportunity",
        ]:
            rel = _smoke_artifact_path(artifacts.get(key))
            if not rel:
                continue
            try:
                validate_artifact_contract(
                    REPO_ROOT / rel,
                    label=f"digital_twin_smoke:{pair}:{key}",
                )
            except (OSError, TypeError, ValueError) as exc:
                pytest.skip(
                    f"digital twin smoke test requires a current '{key}' artifact for {pair}; "
                    f"legacy/incompatible fixture must be retrained ({exc})"
                )
        belief_rel = _smoke_artifact_path(artifacts.get("directional_belief"))
        if belief_rel:
            try:
                validate_directional_belief_artifact_contract(
                    REPO_ROOT / belief_rel,
                    expected_contract=str(feature_schema.get("belief_contract") or "").strip(),
                )
            except (OSError, TypeError, ValueError) as exc:
                pytest.skip(
                    f"digital twin smoke test requires a current directional-belief artifact for {pair}; "
                    f"legacy/incompatible fixture must be retrained ({exc})"
                )


def _load_module():
    spec = importlib.util.spec_from_file_location("fxstack_digital_twin_backtest_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_digital_twin_smoke_outputs(tmp_path):
    _require_twin_smoke_assets(pairs=["EURUSD", "USDJPY"])
    mod = _load_module()
    out_dir = tmp_path / "twin"
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
        emit_decision_history=True,
        max_decision_history_rows=200,
        recommendations=True,
        exec_mode="strict_live_mirror",
        adaptive_compare_baseline=False,
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

    assert aggregate["twin_version"] == "fxstack_digital_twin_v1"
    assert aggregate["decision_count"] > 0
    assert Path(result["aggregate_path"]).exists()
    assert Path(result["trades_path"]).exists()
    assert Path(result["equity_path"]).exists()
    assert Path(result["per_pair_path"]).exists()
    assert Path(result["side_path"]).exists()
    assert Path(result["rejections_by_pair_path"]).exists()
    assert Path(result["rejections_by_session_path"]).exists()
    assert Path(result["lifecycle_summary_path"]).exists()
    assert Path(result["structure_summary_path"]).exists()
    assert Path(result["uncertainty_summary_path"]).exists()
    assert Path(result["environment_summary_path"]).exists()
    assert Path(result["playbook_summary_path"]).exists()
    assert Path(result["portfolio_crowding_summary_path"]).exists()
    assert Path(result["allocator_summary_path"]).exists()
    assert Path(result["sleeve_health_summary_path"]).exists()
    assert Path(result["replacement_summary_path"]).exists()
    assert Path(result["campaign_summary_path"]).exists()
    assert Path(result["campaign_state_summary_path"]).exists()
    assert Path(result["allocator_decision_history_path"]).exists()
    assert Path(result["thesis_campaigns_path"]).exists()
    assert Path(result["twin_validation_path"]).exists()
    assert Path(result["recent_live_comparison_path"]).exists()
    assert Path(result["improvements_path"]).exists()
    assert Path(result["decision_history_path"]).exists()


def test_smoke_artifact_path_supports_dict_manifest_entries() -> None:
    rel = _smoke_artifact_path(
        {
            "model_uri": "models:/fx.meta_filter.EURUSD.M5@champion",
            "evidence_refs": {"artifact_path": "fx-quant-stack/artifacts_shadow/full_20260323/eurusd/meta_filter"},
        }
    )

    assert rel == "fx-quant-stack/artifacts_shadow/full_20260323/eurusd/meta_filter"
