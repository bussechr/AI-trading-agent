# AGENT: ROLE: Measured venue cost table -- turns observed IG spreads into the per-pair pads backtests must use.
# AGENT: ENTRYPOINT: `measure_from_bars`, `load_cost_table`, CLI `python -m fxstack.scalp.costs`.
# AGENT: PRIMARY INPUTS: data/scalp/bars/*_M1.jsonl (live-collected, real IG spreads).
# AGENT: PRIMARY OUTPUTS: data/scalp/measured_costs.json {symbol: {median/p75/p90 bps, samples, hours}}.
# AGENT: CALLED BY: `fxstack/scalp/backtest.py` (--cost-table), operator research loop.
"""Measured venue costs.

The blind run exposed the hazard: Dukascopy quotes are interbank, IG's are
retail, and a backtest that silently uses the former overstates every edge.
Hand-picked pads were the stopgap; this module replaces them with
measurement.

Live M1 bars persisted by the scalper carry the real IG spread per minute, so
the cost table is simply their distribution. Backtests then pad interbank
data by (measured_venue - interbank_observed) PER PAIR AND HOUR, which is the
only honest way to price a strategy for the venue that will actually fill it.

p75 is the default pad basis rather than the median: entries cluster in
volatile minutes where spreads widen, so the median flatters the fills a
strategy actually gets.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
from pathlib import Path
from typing import Any

DEFAULT_TABLE_RELPATH = "measured_costs.json"
#: A pair needs at least this many observed minutes before its measurement is
#: allowed to price anything; below it the table records the samples but
#: marks the pair unmeasured, and callers must fail closed.
MIN_SAMPLES = 200


def measure_from_bars(bars_dir: Path) -> dict[str, dict[str, Any]]:
    """Spread distribution per symbol (and per UTC hour) from live M1 bars."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(bars_dir).glob("*_M1.jsonl")):
        symbol = path.name.split("_")[0].upper()
        spreads: list[float] = []
        by_hour: dict[int, list[float]] = {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not rec.get("valid"):
                        continue
                    value = rec.get("spread_close_bps")
                    if not isinstance(value, (int, float)) or value <= 0:
                        continue
                    spreads.append(float(value))
                    hour = dt.datetime.fromtimestamp(
                        float(rec.get("minute_epoch") or 0), dt.timezone.utc
                    ).hour
                    by_hour.setdefault(hour, []).append(float(value))
        except OSError:
            continue
        if not spreads:
            continue
        spreads.sort()
        out[symbol] = {
            "median_bps": statistics.median(spreads),
            "p75_bps": _quantile(spreads, 0.75),
            "p90_bps": _quantile(spreads, 0.90),
            "samples": len(spreads),
            "measured": len(spreads) >= MIN_SAMPLES,
            "by_hour_p75_bps": {
                str(hour): _quantile(sorted(values), 0.75)
                for hour, values in sorted(by_hour.items())
                if len(values) >= 20
            },
        }
    return out


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(q * len(sorted_values))))
    return float(sorted_values[idx])


def load_cost_table(path: str | Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def venue_pad_bps(
    table: dict[str, dict[str, Any]],
    *,
    symbol: str,
    interbank_bps: float,
    basis: str = "p75_bps",
) -> tuple[float, str]:
    """Pad to apply to interbank data for this symbol; ("", reason) if unmeasured.

    Returns (pad_bps, reason). A pair with no measurement returns reason
    "unmeasured" and the caller MUST refuse to treat the run as
    venue-realistic -- that refusal is what the arming battery checks.
    """
    entry = dict(table.get(str(symbol).upper()) or {})
    if not entry or not entry.get("measured"):
        return 0.0, "unmeasured"
    venue = float(entry.get(basis) or 0.0)
    if venue <= 0.0:
        return 0.0, "unmeasured"
    return max(0.0, venue - max(0.0, float(interbank_bps))), ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bars-dir", default="data/scalp/bars")
    ap.add_argument("--out", default=None, help="default: <bars-dir>/../measured_costs.json")
    args = ap.parse_args(argv)

    bars_dir = Path(args.bars_dir)
    table = measure_from_bars(bars_dir)
    out_path = Path(args.out) if args.out else bars_dir.parent / DEFAULT_TABLE_RELPATH
    out_path.write_text(json.dumps(table, indent=1), encoding="utf-8")
    measured = sum(1 for v in table.values() if v.get("measured"))
    print(f"wrote {out_path}: {len(table)} symbols, {measured} with >= {MIN_SAMPLES} samples")
    for sym, v in sorted(table.items()):
        flag = "" if v.get("measured") else "  (UNMEASURED - cannot price a backtest)"
        print(
            f"  {sym:<8} median={v['median_bps']:.2f} p75={v['p75_bps']:.2f} "
            f"p90={v['p90_bps']:.2f} n={v['samples']}{flag}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
