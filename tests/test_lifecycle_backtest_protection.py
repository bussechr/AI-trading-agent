from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from fxstack.backtest.research_support import entry_protection_prices, partial_close_guard
from fxstack.runtime.positions import partial_close_guard as runtime_partial_close_guard
from fxstack.runtime.runner import _entry_protection_prices as runtime_entry_protection_prices


REPO_ROOT = Path(__file__).resolve().parents[1]


def _module():
    path = REPO_ROOT / "tools" / "fxstack_lifecycle_equity_backtest.py"
    spec = importlib.util.spec_from_file_location("fxstack_lifecycle_backtest_protection", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _position(module, *, side: str, sl: float, tp: float):
    return module.PositionState(
        pair="EURUSD",
        side=side,
        lots=0.06,
        entry_lots=0.06,
        entry_price=1.10,
        open_ts=pd.Timestamp("2026-07-20T00:00:00Z"),
        open_equity_usd=10_000.0,
        entry_trade_prob=0.7,
        sl_price=sl,
        tp_price=tp,
    )


def test_long_intrabar_ambiguity_is_stop_first_and_gap_aware() -> None:
    module = _module()
    position = _position(module, side="long", sl=1.09, tp=1.12)

    reason, price, action = module._broker_protection_fill(
        position=position,
        bid_open=1.08,
        bid_high=1.13,
        bid_low=1.07,
        ask_open=1.0802,
        ask_high=1.1302,
        ask_low=1.0702,
    )

    assert (reason, price, action) == ("broker_stop_loss", 1.08, "long_close")


def test_short_intrabar_ambiguity_is_stop_first_and_gap_aware() -> None:
    module = _module()
    position = _position(module, side="short", sl=1.12, tp=1.09)

    reason, price, action = module._broker_protection_fill(
        position=position,
        bid_open=1.1298,
        bid_high=1.1398,
        bid_low=1.0798,
        ask_open=1.13,
        ask_high=1.14,
        ask_low=1.08,
    )

    assert (reason, price, action) == ("broker_stop_loss", 1.13, "short_close")


def test_target_and_no_hit_paths_use_executable_quote_side() -> None:
    module = _module()
    long_position = _position(module, side="long", sl=1.09, tp=1.12)
    assert module._broker_protection_fill(
        position=long_position,
        bid_open=1.11,
        bid_high=1.121,
        bid_low=1.105,
        ask_open=1.1102,
        ask_high=1.1212,
        ask_low=1.1052,
    ) == ("broker_take_profit", 1.12, "long_close")

    short_position = _position(module, side="short", sl=1.12, tp=1.09)
    assert module._broker_protection_fill(
        position=short_position,
        bid_open=1.10,
        bid_high=1.11,
        bid_low=1.095,
        ask_open=1.1002,
        ask_high=1.1102,
        ask_low=1.0952,
    ) == ("", 0.0, "short_close")


def test_research_protection_geometry_matches_runtime_contract() -> None:
    settings = SimpleNamespace(
        entry_stop_atr_multiple=1.2,
        entry_take_profit_atr_multiple=1.5,
        entry_min_stop_pips=5.0,
    )
    tick = {
        "bid": 1.10100,
        "ask": 1.10120,
        "digits": 5,
        "point": 0.00001,
        "stops_level": 15,
    }
    row = {"atr_14": 0.0008}
    for side in ("BUY", "SELL"):
        assert entry_protection_prices(
            pair="EURUSD",
            side=side,
            tick=tick,
            row=row,
            settings=settings,
        ) == runtime_entry_protection_prices(
            pair="EURUSD",
            side=side,
            tick=tick,
            row=row,
            settings=settings,
        )


def test_research_partial_guard_matches_runtime_contract() -> None:
    settings = SimpleNamespace(
        max_partial_closes_per_position=2,
        partial_close_cooldown_secs=1800.0,
    )
    states = (
        ({"count": 0, "last_partial_ts": 0.0}, 5000.0),
        ({"count": 1, "last_partial_ts": 4500.0}, 5000.0),
        ({"count": 2, "last_partial_ts": 1000.0}, 5000.0),
    )
    for tracker_state, loop_ts in states:
        assert partial_close_guard(
            tracker_state=tracker_state,
            loop_ts=loop_ts,
            settings=settings,
        ) == runtime_partial_close_guard(
            tracker_state=tracker_state,
            loop_ts=loop_ts,
            settings=settings,
        )
