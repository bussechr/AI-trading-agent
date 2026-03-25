from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path
import sys


REPO_ROOT = Path("/mnt/d/Development/Trading Agent")
TOOL_PATH = REPO_ROOT / "tools" / "fxstack_digital_twin_backtest.py"
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))


def _load_module():
    spec = importlib.util.spec_from_file_location("fxstack_digital_twin_backtest_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_digital_twin_smoke_outputs(tmp_path):
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
        bridge_url="http://127.0.0.1:58710",
        live_api_key="",
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
    assert Path(result["twin_validation_path"]).exists()
    assert Path(result["recent_live_comparison_path"]).exists()
    assert Path(result["improvements_path"]).exists()
    assert Path(result["decision_history_path"]).exists()
