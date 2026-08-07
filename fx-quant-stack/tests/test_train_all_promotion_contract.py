from __future__ import annotations

import importlib.util
from pathlib import Path


def _train_all_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "train_all.py"
    spec = importlib.util.spec_from_file_location("fxstack_train_all_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _eligible_statuses() -> dict[str, str]:
    return {
        "swing_xgb": "eligible",
        "intraday_xgb": "eligible",
        "meta": "eligible",
        "exit": "eligible",
        "reversal_failure": "eligible",
        "reversal_opportunity": "eligible",
    }


def test_tier1_bundle_requires_every_binding_component_to_be_eligible() -> None:
    module = _train_all_module()
    statuses = _eligible_statuses()
    assert module._aggregate_promotion_status(
        tier="tier1",
        lifecycle_complete=True,
        component_statuses=statuses,
    ) == "eligible"

    for component in tuple(statuses):
        failed = {**statuses, component: "research_only"}
        assert module._aggregate_promotion_status(
            tier="tier1",
            lifecycle_complete=True,
            component_statuses=failed,
        ) == "research_only"


def test_tier2_still_requires_the_complete_entry_stack() -> None:
    module = _train_all_module()
    statuses = _eligible_statuses()
    statuses["exit"] = "unknown"
    statuses["reversal_failure"] = "unknown"
    statuses["reversal_opportunity"] = "unknown"
    assert module._aggregate_promotion_status(
        tier="tier2",
        lifecycle_complete=False,
        component_statuses=statuses,
    ) == "eligible"

    statuses["swing_xgb"] = "unknown"
    assert module._aggregate_promotion_status(
        tier="tier2",
        lifecycle_complete=False,
        component_statuses=statuses,
    ) == "unknown"
