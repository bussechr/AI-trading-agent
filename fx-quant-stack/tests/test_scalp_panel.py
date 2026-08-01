"""Cross-sectional panel tests.

The dollar's sign convention and the completeness of the cross-section are
the two places where cross-pair work silently produces confident nonsense,
so both are pinned here with constructed cases.
"""

from __future__ import annotations

import datetime as dt

import pytest

from fxstack.scalp.panel import (
    PanelBar,
    cross_features,
    usd_direction,
    usd_factor_bps,
)

E0 = int(dt.datetime(2025, 3, 4, 9, 0, tzinfo=dt.timezone.utc).timestamp())


def _bar(prev_mid: float, mid: float, *, spread_bps: float = 1.0) -> PanelBar:
    half = mid * spread_bps / 1e4 / 2.0
    return PanelBar(epoch=E0, bid=mid - half, ask=mid + half, prev_mid=prev_mid,
                    volume=100.0)


def test_usd_direction_convention():
    # A rise in EURUSD is a WEAKER dollar; a rise in USDJPY is a STRONGER one.
    assert usd_direction("EURUSD") == 1.0
    assert usd_direction("GBPUSD") == 1.0
    assert usd_direction("USDJPY") == -1.0
    assert usd_direction("USDCHF") == -1.0
    # Crosses carry no direct dollar leg.
    assert usd_direction("EURGBP") == 0.0
    assert usd_direction("AUDJPY") == 0.0


def test_broad_dollar_rally_is_measured_with_the_right_sign():
    """Every USD pair moves as a stronger dollar would move it."""
    per_symbol = {
        # USD-quoted pairs FALL when the dollar strengthens.
        "EURUSD": {E0: _bar(1.1000, 1.0989)},   # -10bps
        "GBPUSD": {E0: _bar(1.2500, 1.2488)},   # -9.6bps
        "AUDUSD": {E0: _bar(0.6500, 0.6494)},   # -9.2bps
        # USD-based pairs RISE when the dollar strengthens.
        "USDJPY": {E0: _bar(150.00, 150.15)},   # +10bps
        "USDCHF": {E0: _bar(0.9000, 0.9009)},   # +10bps
    }
    factor, coherence, n = usd_factor_bps(E0, per_symbol)
    assert n == 5
    assert factor > 0  # positive factor == stronger dollar
    assert factor == pytest.approx(9.8, abs=1.0)
    assert coherence == 1.0  # every pair agrees


def test_disagreeing_cross_section_has_low_coherence():
    per_symbol = {
        "EURUSD": {E0: _bar(1.1000, 1.0989)},   # dollar up
        "GBPUSD": {E0: _bar(1.2500, 1.2512)},   # dollar down
        "AUDUSD": {E0: _bar(0.6500, 0.6506)},   # dollar down
        "USDJPY": {E0: _bar(150.00, 150.15)},   # dollar up
    }
    _factor, coherence, n = usd_factor_bps(E0, per_symbol)
    assert n == 4
    assert coherence <= 0.75  # no broad statement about the dollar


def test_residual_isolates_what_the_pair_did_beyond_the_dollar():
    """The whole point: a pair that ignored a broad dollar move.

    EURUSD is flat while every other USD pair prices a stronger dollar, so
    EURUSD's residual must be POSITIVE (it outperformed the dollar move that
    was implied for it) -- a statement impossible to make from EURUSD alone.
    """
    per_symbol = {
        "EURUSD": {E0: _bar(1.1000, 1.1000)},   # flat
        "GBPUSD": {E0: _bar(1.2500, 1.2488)},   # -9.6bps
        "AUDUSD": {E0: _bar(0.6500, 0.6494)},   # -9.2bps
        "USDJPY": {E0: _bar(150.00, 150.15)},   # +10bps
        "USDCHF": {E0: _bar(0.9000, 0.9009)},   # +10bps
    }
    cf = cross_features(symbol="EURUSD", epoch=E0, per_symbol=per_symbol)
    assert cf["usd_factor"] < 0  # EURUSD "should" have fallen
    assert cf["residual"] > 0  # it did not -- that is the signal
    assert cf["usd_coherence"] >= 0.75


def test_thin_cross_section_yields_no_factor():
    per_symbol = {"EURUSD": {E0: _bar(1.1000, 1.0989)}}
    factor, coherence, n = usd_factor_bps(E0, per_symbol)
    assert n < 3 and factor == 0.0 and coherence == 0.0
    cf = cross_features(symbol="EURUSD", epoch=E0, per_symbol=per_symbol)
    assert cf == {"usd_factor": 0.0, "usd_coherence": 0.0, "residual": 0.0}


def test_alignment_keeps_only_fully_observed_timestamps(tmp_path):
    from fxstack.scalp.panel import load_panel

    header = ("timestamp,bid_open,bid_high,bid_low,bid_close,"
              "ask_open,ask_high,ask_low,ask_close,volume\n")

    def _rows(minutes: list[int]) -> str:
        out = header
        for m in minutes:
            ts = dt.datetime(2025, 3, 4, 9, m, tzinfo=dt.timezone.utc)
            out += (f"{ts.strftime('%Y-%m-%dT%H:%M:%SZ')},"
                    "1.1000,1.1001,1.0999,1.1000,"
                    "1.1001,1.1002,1.1000,1.1001,10\n")
        return out

    # EURUSD has minutes 0-19; USDJPY is missing 10-19.
    (tmp_path / "EURUSD_M1.csv").write_text(_rows(list(range(20))), encoding="utf-8")
    (tmp_path / "USDJPY_M1.csv").write_text(_rows(list(range(10))), encoding="utf-8")
    epochs, per_symbol = load_panel(
        symbols=["EURUSD", "USDJPY"], csv_root=tmp_path, bar_minutes=5
    )
    assert set(per_symbol) == {"EURUSD", "USDJPY"}
    # Only timestamps present for BOTH pairs survive.
    for epoch in epochs:
        assert epoch in per_symbol["EURUSD"] and epoch in per_symbol["USDJPY"]
    assert len(epochs) <= 2
