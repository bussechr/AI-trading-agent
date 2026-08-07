from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "fxstack_causal_research_backtest.py"
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))


def _load_module():
    spec = importlib.util.spec_from_file_location("fxstack_causal_research_overlay_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_research_overlay_diagnostics_are_local_and_deterministic() -> None:
    module = _load_module()
    rows = [
        {
            "belief_overlay_enabled": True,
            "belief_overlay_source": "artifact",
            "belief_overlay_adjustment": 0.10,
            "belief_primary_scenario": "trend_pullback",
        },
        {
            "belief_overlay_enabled": True,
            "belief_overlay_source": "artifact",
            "belief_overlay_adjustment": -0.05,
            "belief_primary_scenario": "range_mean_reversion",
        },
    ]

    first = module._shared_overlay_diagnostics(rows)
    second = module._shared_overlay_diagnostics(list(rows))

    assert first == second
    assert isinstance(first, dict)


def test_research_overlay_test_has_no_runtime_dependency() -> None:
    source = TOOL_PATH.read_text(encoding="utf-8")

    assert "fxstack.runtime.runner" not in source
    assert "/v2/decision-snapshots" not in source
