"""Direct unit tests for :mod:`fxstack.runtime.positions`.

Pins the lot-sizing + partial-close + signature contracts so future carve-outs
or refactors of this layer can't silently regress.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fxstack.runtime.positions import (
    active_position_signatures,
    partial_close_guard,
    partial_close_plan,
    position_side,
    position_signature,
    prune_partial_close_tracker,
    round_lot_size,
)


# ---------------------------------------------------------------------------
# round_lot_size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lots", "min_lot", "lot_step", "max_lot", "expected"),
    [
        (0.05, 0.01, 0.01, 1.0, 0.05),
        (0.123, 0.01, 0.01, 1.0, 0.12),  # floors to lot_step grid
        (0.005, 0.01, 0.01, 1.0, 0.01),  # below min clamps to min
        (0.999, 0.01, 0.01, 0.5, 0.50),  # above max clamps to max
        (0.0, 0.01, 0.01, 1.0, 0.01),  # zero clamps to min
    ],
)
def test_round_lot_size_quantizes_to_step(
    lots: float, min_lot: float, lot_step: float, max_lot: float, expected: float
) -> None:
    assert round_lot_size(
        lots=lots, min_lot=min_lot, lot_step=lot_step, max_lot=max_lot
    ) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# position_side
# ---------------------------------------------------------------------------


def test_position_side_empty_list_returns_flat() -> None:
    assert position_side([]) == "flat"


@pytest.mark.parametrize(
    ("pos", "expected"),
    [
        ({"type": 0}, "long"),
        ({"type": 1}, "short"),
        ({"type": "0"}, "long"),
        ({"type": "1"}, "short"),
        ({"type": "OP_BUY"}, "long"),
        ({"type": "OP_SELL"}, "short"),
        ({"side": "BUY"}, "long"),
        ({"side": "sell"}, "short"),
        ({"position_side": "LONG"}, "long"),
        ({"direction": "short"}, "short"),
    ],
)
def test_position_side_recognizes_all_known_encodings(pos: dict, expected: str) -> None:
    assert position_side([pos]) == expected


def test_position_side_unrecognized_returns_flat() -> None:
    assert position_side([{"side": "??"}]) == "flat"


# ---------------------------------------------------------------------------
# partial_close_plan
# ---------------------------------------------------------------------------


def test_partial_close_plan_zero_lots_returns_hold() -> None:
    s = SimpleNamespace(min_order_lots=0.01, order_lot_step=0.01)
    assert partial_close_plan(lots_open=0.0, fraction=0.5, settings=s) == ("hold", 0.0)


def test_partial_close_plan_zero_fraction_returns_hold() -> None:
    s = SimpleNamespace(min_order_lots=0.01, order_lot_step=0.01)
    assert partial_close_plan(lots_open=1.0, fraction=0.0, settings=s) == ("hold", 0.0)


def test_partial_close_plan_normal_returns_partial_tp() -> None:
    s = SimpleNamespace(min_order_lots=0.01, order_lot_step=0.01)
    verdict, lots = partial_close_plan(lots_open=1.0, fraction=0.3, settings=s)
    assert verdict == "partial_tp"
    assert lots == pytest.approx(0.30)


def test_partial_close_plan_above_remainder_floor_promotes_to_exit() -> None:
    """If residue would be below ``min_order_lots``, promote to a full exit.

    The broker can't hold a sub-minimum lot residue; rolling the partial
    close into a full exit avoids a rejected follow-up order.
    """
    s = SimpleNamespace(min_order_lots=0.5, order_lot_step=0.01)
    # Open 1.0, close 0.6 → residue 0.4 < min_lot 0.5 → full exit.
    verdict, lots = partial_close_plan(lots_open=1.0, fraction=0.6, settings=s)
    assert verdict == "exit"
    assert lots == pytest.approx(1.0)


def test_partial_close_plan_full_fraction_returns_exit() -> None:
    s = SimpleNamespace(min_order_lots=0.01, order_lot_step=0.01)
    verdict, lots = partial_close_plan(lots_open=1.0, fraction=1.0, settings=s)
    assert verdict == "exit"
    assert lots == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# position_signature
# ---------------------------------------------------------------------------


def test_position_signature_is_stable_and_unique() -> None:
    a = {"symbol": "EURUSD", "type": 0, "open_time": 1700000000, "open_price": 1.1010, "magic": 42}
    b = {"symbol": "EURUSD", "type": 0, "open_time": 1700000001, "open_price": 1.1010, "magic": 42}
    assert position_signature(a) == position_signature(a)
    assert position_signature(a) != position_signature(b)


def test_position_signature_includes_symbol_and_side() -> None:
    sig = position_signature(
        {"symbol": "EURUSD", "type": 1, "open_time": 1700000000, "open_price": 1.0, "magic": 0}
    )
    assert "EURUSD" in sig
    assert "short" in sig


def test_active_position_signatures_collects_all_open() -> None:
    state = {
        "positions": [
            {"symbol": "EURUSD", "type": 0, "open_time": 100, "open_price": 1.1, "magic": 0},
            {"symbol": "USDJPY", "type": 1, "open_time": 101, "open_price": 100.0, "magic": 0},
        ]
    }
    sigs = active_position_signatures(state)
    assert len(sigs) == 2


# ---------------------------------------------------------------------------
# prune_partial_close_tracker
# ---------------------------------------------------------------------------


def test_prune_partial_close_tracker_drops_orphans() -> None:
    tracker = {"sig_a": {"count": 1}, "sig_b": {"count": 2}, "sig_c": {"count": 1}}
    prune_partial_close_tracker(tracker, active_signatures={"sig_a", "sig_c"})
    assert set(tracker.keys()) == {"sig_a", "sig_c"}


def test_prune_partial_close_tracker_is_noop_when_all_active() -> None:
    tracker = {"sig_a": {"count": 1}, "sig_b": {"count": 2}}
    prune_partial_close_tracker(tracker, active_signatures={"sig_a", "sig_b"})
    assert set(tracker.keys()) == {"sig_a", "sig_b"}


# ---------------------------------------------------------------------------
# partial_close_guard
# ---------------------------------------------------------------------------


def test_partial_close_guard_allows_when_no_history() -> None:
    s = SimpleNamespace(max_partial_closes_per_position=3, partial_close_cooldown_secs=600.0)
    allowed, reason, remaining = partial_close_guard(tracker_state={}, loop_ts=1000.0, settings=s)
    assert allowed is True
    assert reason == ""
    assert remaining == 0.0


def test_partial_close_guard_blocks_when_limit_reached() -> None:
    s = SimpleNamespace(max_partial_closes_per_position=2, partial_close_cooldown_secs=0.0)
    state = {"count": 2, "last_partial_ts": 0.0}
    allowed, reason, _ = partial_close_guard(tracker_state=state, loop_ts=1000.0, settings=s)
    assert allowed is False
    assert reason == "partial_tp_limit_reached"


def test_partial_close_guard_blocks_during_cooldown() -> None:
    s = SimpleNamespace(max_partial_closes_per_position=0, partial_close_cooldown_secs=600.0)
    state = {"count": 1, "last_partial_ts": 1000.0}
    allowed, reason, remaining = partial_close_guard(
        tracker_state=state, loop_ts=1300.0, settings=s  # 5min elapsed of 10min cooldown
    )
    assert allowed is False
    assert reason == "partial_tp_cooldown_active"
    assert remaining == pytest.approx(300.0)


def test_partial_close_guard_allows_after_cooldown() -> None:
    s = SimpleNamespace(max_partial_closes_per_position=0, partial_close_cooldown_secs=600.0)
    state = {"count": 1, "last_partial_ts": 1000.0}
    allowed, _, _ = partial_close_guard(tracker_state=state, loop_ts=1700.0, settings=s)
    assert allowed is True
