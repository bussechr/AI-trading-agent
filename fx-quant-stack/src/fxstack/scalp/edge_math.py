# AGENT: ROLE: The arithmetic that decides whether a scalp configuration is worth testing at all -- skill required vs skill available.
# AGENT: ENTRYPOINT: `skill_requirement`; CLI `python -m fxstack.scalp.edge_math`.
# AGENT: PRIMARY INPUTS: measured spread (bps), stop/target geometry, broker stop floor.
# AGENT: PRIMARY OUTPUTS: SkillRequirement (zero-skill win rate, breakeven win rate, the GAP that must be earned, cost drag in R).
# AGENT: CALLED BY: research loop before any backtest; `fxstack/scalp/validate.py` context.
"""How much directional skill does this configuration DEMAND?

Measured across ~7,700 backtested trades (dislocation and opening-range, M1
and M5, 2024-2026): every family's win rate landed at the pure-geometry
zero-skill rate, and every family's mean R landed at "zero skill minus
cost". Nothing carried directional information; the P&L was the spread.

That reframes the problem. For a driftless path with a TP/SL bracket:

    zero-skill win rate   p0 = SL / (TP + SL)
    breakeven win rate    p* = (SL + cost) / (TP + SL)
    skill gap             p* - p0 = cost / (TP + SL)
    cost drag per trade   cost / SL   (in R)

The gap is what a strategy must EARN in directional accuracy before it makes
a cent. It shrinks when the bracket is wide relative to cost -- which is why
a broker minimum stop measured in pips, against spreads measured in fractions
of a pip, sets the whole game. Computing this BEFORE a backtest tells you
whether the cell is worth searching at all; computing it after tells you
whether a positive result was skill or luck.

This module is deliberately parameter-free arithmetic: no fitting, no data,
nothing to overfit. It is the honest constraint the search must respect.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from typing import Any

#: Broker minimum stop distances measured live from IG demo (MODE_STOPLEVEL
#: x MODE_POINT, published by the EA 2026-08-01), in bps of mid.
MEASURED_STOP_FLOOR_BPS: dict[str, float] = {
    "EURUSD": 3.5,   # 40 points x 1e-5 / 1.15
    "USDJPY": 2.7,   # 40 points x 1e-3 / 148
    "GBPUSD": 3.7,   # 50 points
    "AUDUSD": 9.2,   # 60 points x 1e-5 / 0.65
    "USDCHF": 8.8,   # 70 points
    "USDCAD": 5.8,   # 80 points
    "NZDUSD": 8.5,   # 50 points
    "EURJPY": 3.5,   # 60 points
    "EURGBP": 6.7,   # 50 points
    "GBPJPY": 4.0,   # 90 points
}

#: Measured IG demo spreads (bps of mid) -- the cost side of the same trade.
MEASURED_SPREAD_BPS: dict[str, float] = {
    "EURUSD": 1.2, "USDJPY": 1.3, "AUDUSD": 1.5, "GBPUSD": 2.0,
    "USDCAD": 2.2, "USDCHF": 2.2, "EURGBP": 2.0, "EURJPY": 2.2,
    "NZDUSD": 2.2, "GBPJPY": 2.2,
}


@dataclass(slots=True)
class SkillRequirement:
    symbol: str
    stop_bps: float
    target_bps: float
    cost_bps: float
    zero_skill_win_rate: float
    breakeven_win_rate: float
    skill_gap_pp: float
    cost_drag_r: float
    reward_risk: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def plausible(self) -> bool:
        """A gap beyond ~5 points of win rate is not plausibly earnable at
        short horizons in FX -- that is a claim to predict direction better
        than the market by a wide margin, sustained over thousands of trades."""
        return self.skill_gap_pp <= 5.0


def skill_requirement(
    *, symbol: str, stop_bps: float, target_bps: float, cost_bps: float
) -> SkillRequirement:
    stop = max(1e-9, float(stop_bps))
    target = max(1e-9, float(target_bps))
    cost = max(0.0, float(cost_bps))
    p0 = stop / (target + stop)
    p_star = (stop + cost) / (target + stop)
    return SkillRequirement(
        symbol=str(symbol).upper(),
        stop_bps=stop,
        target_bps=target,
        cost_bps=cost,
        zero_skill_win_rate=p0,
        breakeven_win_rate=p_star,
        skill_gap_pp=(p_star - p0) * 100.0,
        cost_drag_r=cost / stop,
        reward_risk=target / stop,
    )


def survey(
    *, reward_risk: float = 1.5, stop_multiples: tuple[float, ...] = (1.0, 2.0, 4.0)
) -> list[SkillRequirement]:
    """Skill demanded per pair at the broker floor and at wider stops.

    Wider stops do not create edge -- they dilute COST. The survey shows
    exactly how much of the requirement is the venue rather than the market.
    """
    out: list[SkillRequirement] = []
    for symbol, floor in sorted(MEASURED_STOP_FLOOR_BPS.items()):
        cost = MEASURED_SPREAD_BPS.get(symbol, 2.0)
        for mult in stop_multiples:
            stop = floor * mult
            out.append(
                skill_requirement(
                    symbol=symbol,
                    stop_bps=stop,
                    target_bps=stop * reward_risk,
                    cost_bps=cost,
                )
            )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reward-risk", type=float, default=1.5)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    rows = survey(reward_risk=args.reward_risk)
    print(
        f"Skill required at reward:risk = {args.reward_risk}:1 "
        f"(measured IG stop floors and spreads)\n"
    )
    print(f"{'pair':<8}{'stop_bps':>9}{'cost_bps':>9}{'zero%':>8}{'breakeven%':>12}"
          f"{'GAP_pp':>8}{'drag_R':>8}  verdict")
    for r in rows:
        verdict = "plausible" if r.plausible else "implausible skill demand"
        print(f"{r.symbol:<8}{r.stop_bps:>9.1f}{r.cost_bps:>9.1f}"
              f"{r.zero_skill_win_rate*100:>7.1f}%{r.breakeven_win_rate*100:>11.1f}%"
              f"{r.skill_gap_pp:>8.1f}{r.cost_drag_r:>8.3f}  {verdict}")
    plausible = [r for r in rows if r.plausible]
    print(
        f"\n{len(plausible)}/{len(rows)} cells demand <= 5pp of directional skill."
    )
    if plausible:
        best = min(plausible, key=lambda r: r.skill_gap_pp)
        print(
            f"Lowest bar: {best.symbol} at stop {best.stop_bps:.1f}bps -> "
            f"needs {best.skill_gap_pp:.1f}pp over random ({best.cost_drag_r:.3f}R drag)."
        )
    if args.json_out:
        from pathlib import Path

        Path(args.json_out).write_text(
            json.dumps([r.to_dict() for r in rows], indent=1), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
