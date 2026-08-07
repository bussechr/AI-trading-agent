"""Currency-cluster risk tests.

The failure this exists to prevent: many correlated pairs opening at once and
adding up to one enormous directional bet while every per-pair check passes.
"""

from __future__ import annotations

import pytest

from fxstack.scalp.config import CONFIGURED_CRYPTO_SYMBOLS, CONFIGURED_SYMBOLS
from fxstack.scalp.panel import PAIR_LEGS
from fxstack.scalp.portfolio import CurrencyBook


def _book(**over) -> CurrencyBook:
    kwargs = {"max_currency_net_r": 2.0, "max_total_gross_r": 6.0, "max_concurrent": 8}
    kwargs.update(over)
    return CurrencyBook(**kwargs)


def test_four_ways_of_one_dollar_bet_is_refused():
    """Long EUR/GBP/AUD vs USD plus short USDJPY is ONE short-dollar bet."""
    book = _book()
    for symbol in ("EURUSD", "GBPUSD"):
        assert book.admit(symbol=symbol, side="BUY") == ""
        book.open_position(symbol=symbol, side="BUY")
    # USD is now -2R net (short dollar twice). A third short-dollar position
    # breaches the currency cap even though no pair repeats.
    reason = book.admit(symbol="AUDUSD", side="BUY")
    assert reason == "currency_net_cap:USD"
    # Selling USDJPY is ALSO short dollar -- the cap must catch the base leg.
    assert book.admit(symbol="USDJPY", side="SELL") == "currency_net_cap:USD"


def test_opposing_positions_free_up_currency_capacity():
    book = _book()
    for symbol in ("EURUSD", "GBPUSD"):
        book.open_position(symbol=symbol, side="BUY")
    assert book.admit(symbol="AUDUSD", side="BUY") == "currency_net_cap:USD"
    # A long-dollar position brings the net back inside the cap.
    assert book.admit(symbol="USDJPY", side="BUY") == ""
    book.open_position(symbol="USDJPY", side="BUY")
    assert book.exposure().net("USD") == pytest.approx(-1.0)
    assert book.admit(symbol="AUDUSD", side="BUY") == ""


def test_uncorrelated_pairs_can_all_run_simultaneously():
    """The cap must not block genuine diversification -- that is the point of
    trading every pair."""
    book = _book(max_concurrent=8)
    admitted = []
    for symbol in ("EURUSD", "USDJPY", "GBPCHF", "AUDJPY", "EURGBP"):
        side = "BUY" if symbol in {"EURUSD", "GBPCHF", "EURGBP"} else "SELL"
        if book.admit(symbol=symbol, side=side) == "":
            book.open_position(symbol=symbol, side=side)
            admitted.append(symbol)
    assert len(admitted) >= 4
    exp = book.exposure()
    assert all(abs(v) <= 2.0 + 1e-9 for v in exp.per_currency.values())


def test_gross_risk_cap_bounds_a_correlated_everything_day():
    book = _book(max_currency_net_r=99.0, max_total_gross_r=3.0)
    opened = 0
    for symbol in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY"):
        if book.admit(symbol=symbol, side="BUY") == "":
            book.open_position(symbol=symbol, side="BUY")
            opened += 1
    assert opened == 3
    assert book.admit(symbol="USDCHF", side="BUY") == "book_gross_risk_cap"


def test_max_concurrent_and_duplicate_refusals():
    book = _book(max_currency_net_r=99.0, max_total_gross_r=99.0, max_concurrent=2)
    book.open_position(symbol="EURUSD", side="BUY")
    assert book.admit(symbol="EURUSD", side="SELL") == "position_already_open"
    book.open_position(symbol="AUDJPY", side="BUY")
    assert book.admit(symbol="GBPCHF", side="BUY") == "max_concurrent"


def test_unknown_symbol_is_refused_not_ignored():
    book = _book()
    assert book.admit(symbol="XAUUSD", side="BUY") == "unknown_currency_legs"


@pytest.mark.parametrize("symbol", CONFIGURED_SYMBOLS)
def test_every_configured_symbol_has_base_quote_exposure(symbol: str):
    book = _book(
        max_currency_net_r=99.0,
        max_total_gross_r=99.0,
        max_concurrent=len(CONFIGURED_SYMBOLS),
    )
    assert book.admit(symbol=symbol, side="BUY") == ""
    book.open_position(symbol=symbol, side="BUY")
    base, quote = PAIR_LEGS[symbol]
    exposure = book.exposure()
    assert base and quote and base != quote
    assert exposure.net(base) == pytest.approx(1.0)
    assert exposure.net(quote) == pytest.approx(-1.0)


def test_crypto_pairs_share_usd_risk_while_retaining_their_base_assets():
    book = _book(max_concurrent=len(CONFIGURED_SYMBOLS))
    for symbol in CONFIGURED_CRYPTO_SYMBOLS[:2]:
        assert book.admit(symbol=symbol, side="BUY") == ""
        book.open_position(symbol=symbol, side="BUY")
    exposure = book.exposure()
    assert exposure.net("BTC") == pytest.approx(1.0)
    assert exposure.net("ETH") == pytest.approx(1.0)
    assert exposure.net("USD") == pytest.approx(-2.0)
    assert book.admit(symbol="EURUSD", side="BUY") == "currency_net_cap:USD"
    assert book.admit(symbol="EURUSD", side="SELL") == ""


def test_closing_releases_exposure():
    book = _book()
    book.open_position(symbol="EURUSD", side="BUY")
    book.open_position(symbol="GBPUSD", side="BUY")
    assert book.admit(symbol="AUDUSD", side="BUY") == "currency_net_cap:USD"
    book.close_position("EURUSD")
    assert book.admit(symbol="AUDUSD", side="BUY") == ""
    assert book.open_count == 1


def test_risk_weighted_positions_scale_the_caps():
    # Half-sized positions should allow twice as many before the cap binds.
    book = _book()
    for symbol in ("EURUSD", "GBPUSD", "AUDUSD"):
        assert book.admit(symbol=symbol, side="BUY", risk_r=0.5) == ""
        book.open_position(symbol=symbol, side="BUY", risk_r=0.5)
    assert book.exposure().net("USD") == pytest.approx(-1.5)
    assert book.admit(symbol="NZDUSD", side="BUY", risk_r=0.5) == ""
    book.open_position(symbol="NZDUSD", side="BUY", risk_r=0.5)
    assert book.admit(symbol="USDCHF", side="SELL", risk_r=0.5) == "currency_net_cap:USD"
