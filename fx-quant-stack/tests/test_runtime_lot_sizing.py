from __future__ import annotations

from types import SimpleNamespace

from fxstack.runtime.runner import _partial_close_plan


def _settings(*, min_lot: float = 0.01, lot_step: float = 0.01):
    return SimpleNamespace(
        min_order_lots=min_lot,
        order_lot_step=lot_step,
    )


def test_partial_close_plan_rounds_to_broker_step():
    action, lots = _partial_close_plan(
        lots_open=0.31,
        fraction=0.5,
        settings=_settings(),
    )
    assert action == "partial_tp"
    assert lots == 0.15


def test_partial_close_plan_switches_tiny_position_to_full_exit():
    action, lots = _partial_close_plan(
        lots_open=0.01,
        fraction=0.5,
        settings=_settings(),
    )
    assert action == "exit"
    assert lots == 0.01


def test_partial_close_plan_preserves_valid_remainder():
    action, lots = _partial_close_plan(
        lots_open=0.03,
        fraction=0.5,
        settings=_settings(),
    )
    assert action == "partial_tp"
    assert lots == 0.01
