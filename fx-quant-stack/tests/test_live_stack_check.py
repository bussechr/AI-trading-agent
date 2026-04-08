from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "live_stack_check.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("live_stack_check_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_stack_check_defaults_to_non_invasive_info_probe() -> None:
    tool = _load_tool()
    args = tool.build_parser().parse_args([])

    assert args.command == "INFO"
    assert args.lots == 0.0
    assert args.require_acked_command is False
    assert args.require_paper_boundary is False
