# AGENT: ROLE: Portfolio risk for running every pair at once -- currency-cluster caps that stop concurrent positions from becoming one bet.
# AGENT: ENTRYPOINT: `CurrencyBook.admit` (returns "" or a refusal reason).
# AGENT: PRIMARY INPUTS: open scalp positions, the candidate intent, config caps.
# AGENT: PRIMARY OUTPUTS: admit/refuse with a stable reason recorded in the ledger.
# AGENT: CALLED BY: `fxstack/scalp/loop.py` before opening, `fxstack/scalp/backtest.py` (multi-symbol mode).
"""Currency/asset-cluster risk: the price of trading every pair simultaneously.

Running 18 pairs concurrently is only diversification if the positions are
independent, and FX positions are not. Long EURUSD, long GBPUSD, long AUDUSD
and short USDJPY is one short-dollar bet worn four ways: correlated stops
resolve together, so what looks like 4 x 1R of independent risk is closer to
1 x 4R of concentrated risk.

The book therefore tracks NET exposure per CURRENCY OR BASE ASSET, not per
pair. Each position contributes +1R to its base leg and -1R to its quote leg
(scaled by its own risk), so the dollar leg of every FX or crypto pair
aggregates into one number that can be capped.

Two caps, both fail-closed:

- ``max_currency_net_r``: no currency may carry more than this net R. This is
  the one that stops the four-ways-of-one-bet failure.
- ``max_total_gross_r``: total risk across the book, which bounds a
  correlated-everything day.

Refusals return a stable reason so the ledger records WHY a pair did not
trade -- a silent cap is indistinguishable from a strategy that had no signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fxstack.scalp.panel import PAIR_LEGS


@dataclass(slots=True)
class Exposure:
    """Net R per currency and gross R across the book."""

    per_currency: dict[str, float] = field(default_factory=dict)
    gross_r: float = 0.0

    def add(self, *, symbol: str, side: str, risk_r: float) -> None:
        base, quote = PAIR_LEGS.get(symbol.upper(), ("", ""))
        if not base or not quote:
            return
        sign = 1.0 if side.upper() == "BUY" else -1.0
        self.per_currency[base] = self.per_currency.get(base, 0.0) + sign * risk_r
        self.per_currency[quote] = self.per_currency.get(quote, 0.0) - sign * risk_r
        self.gross_r += abs(risk_r)

    def net(self, currency: str) -> float:
        return self.per_currency.get(currency.upper(), 0.0)


class CurrencyBook:
    """Admission control for concurrent positions across many pairs."""

    def __init__(
        self,
        *,
        max_currency_net_r: float = 2.0,
        max_total_gross_r: float = 6.0,
        max_concurrent: int = 8,
    ) -> None:
        self.max_currency_net_r = max(0.0, float(max_currency_net_r))
        self.max_total_gross_r = max(0.0, float(max_total_gross_r))
        self.max_concurrent = max(1, int(max_concurrent))
        self._open: dict[str, tuple[str, float]] = {}  # symbol -> (side, risk_r)

    # ------------------------------------------------------------- accounting

    def exposure(self) -> Exposure:
        exp = Exposure()
        for symbol, (side, risk_r) in self._open.items():
            exp.add(symbol=symbol, side=side, risk_r=risk_r)
        return exp

    def open_position(self, *, symbol: str, side: str, risk_r: float = 1.0) -> None:
        self._open[symbol.upper()] = (side.upper(), float(risk_r))

    def close_position(self, symbol: str) -> None:
        self._open.pop(symbol.upper(), None)

    @property
    def open_count(self) -> int:
        return len(self._open)

    # -------------------------------------------------------------- admission

    def admit(self, *, symbol: str, side: str, risk_r: float = 1.0) -> str:
        """"" if this position may open, else a stable refusal reason."""
        sym = symbol.upper()
        if sym in self._open:
            return "position_already_open"
        base, quote = PAIR_LEGS.get(sym, ("", ""))
        if not base or not quote:
            return "unknown_currency_legs"
        if len(self._open) >= self.max_concurrent:
            return "max_concurrent"

        exp = self.exposure()
        if exp.gross_r + abs(risk_r) > self.max_total_gross_r + 1e-9:
            return "book_gross_risk_cap"

        sign = 1.0 if side.upper() == "BUY" else -1.0
        # The candidate's own contribution, then check BOTH legs. Checking
        # only the base would let a book load up on one quote currency.
        for currency, delta in ((base, sign * risk_r), (quote, -sign * risk_r)):
            after = exp.net(currency) + delta
            if abs(after) > self.max_currency_net_r + 1e-9:
                return f"currency_net_cap:{currency}"
        return ""
