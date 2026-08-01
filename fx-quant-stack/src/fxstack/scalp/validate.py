# AGENT: ROLE: The scalp arming gate -- runs the statistical battery over backtest/ledger trades and is the ONLY issuer of arming certificates.
# AGENT: ENTRYPOINT: `evaluate_family` (pure verdict); `issue_arming_certificate` (writes cert IFF passed); CLI `python -m fxstack.scalp.validate`.
# AGENT: PRIMARY INPUTS: backtest result JSONs (per-config summaries + per-trade R series) or scalp ledger fills.
# AGENT: PRIMARY OUTPUTS: ArmingVerdict; data/scalp/arming_certificate.json on pass.
# AGENT: DEPENDS ON: fxstack.validation (bootstrap, deflated Sharpe), fxstack.scalp.authority.
# AGENT: CALLED BY: operator/research loop; `fxstack/runtime/service.py` reads the certificate it writes.
"""Scalp validation battery -> arming certificate.

Live mode cannot be configured on; it must be EARNED here. The battery takes
the falsification dataset (backtest trades at venue-realistic costs, or the
live shadow ledger) plus the honest count of every configuration tried, and
issues a certificate only when:

- bootstrap 95% CI lower bound of mean R  > 0
- deflated Sharpe (correcting for the number of trials) >= threshold
- at least ``min_trades`` trades
- costs were venue-realistic (``venue != interbank_raw``)

The certificate is sha-bound to the exact scalp config, symbol-scoped, and
expires: re-validation is a cadence, not a one-off. A family that fails gets
a verdict object -- never a certificate, never a partial one.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import time
from pathlib import Path
from typing import Any

from fxstack.scalp.authority import certificate_body_sha256, config_sha256
from fxstack.scalp.backtest import bootstrap_ci_mean
from fxstack.scalp.config import ScalpConfig
from fxstack.validation.overfitting import deflated_sharpe_ratio

DEFAULT_CERT_RELPATH = "arming_certificate.json"
CERT_VALIDITY_SECS = 7 * 86_400.0


@dataclasses.dataclass(slots=True)
class ArmingVerdict:
    passed: bool
    reasons: list[str]
    trades: int
    mean_r: float
    ci_lo: float
    ci_hi: float
    deflated_sharpe: float
    trials: int
    venue: str
    independent_days: int = 0
    quarter_stats: dict[str, dict[str, float]] = dataclasses.field(default_factory=dict)
    side_means: dict[str, float] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _quarter_key(epoch: float) -> str:
    import datetime as dt

    t = dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc)
    return f"{t.year}Q{(t.month - 1) // 3 + 1}"


def _day_key(epoch: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc).strftime("%Y-%m-%d")


def clustered_bootstrap_ci_mean(
    trades: list[dict[str, Any]], *, n_boot: int = 5000, seed: int = 1337
) -> tuple[float, float]:
    """95% CI of mean R, resampling DAYS rather than trades.

    Scalp trades are not independent draws: several pairs fire on the same
    news minute, and one session's regime produces a whole cluster of
    correlated outcomes. An iid trade bootstrap understates the CI by roughly
    the square root of the cluster size -- enough for a family with 42 trades
    on 6 days to look like n=42 and pass a battery it should fail.

    Resampling whole days preserves within-day correlation, so the interval
    reflects how many INDEPENDENT things were actually observed.
    """
    if not trades:
        return 0.0, 0.0
    by_day: dict[str, list[float]] = {}
    for t in trades:
        epoch = t.get("epoch")
        key = _day_key(epoch) if isinstance(epoch, (int, float)) and epoch > 0 else "_"
        by_day.setdefault(key, []).append(float(t.get("r") or 0.0))
    day_keys = sorted(by_day)
    if len(day_keys) < 2:
        values = [r for rs in by_day.values() for r in rs]
        return bootstrap_ci_mean(values, n_boot=n_boot, seed=seed)
    rng = random.Random(seed)
    n_days = len(day_keys)
    means: list[float] = []
    for _ in range(n_boot):
        pooled: list[float] = []
        for _ in range(n_days):
            pooled.extend(by_day[day_keys[rng.randrange(n_days)]])
        if pooled:
            means.append(sum(pooled) / len(pooled))
    if not means:
        return 0.0, 0.0
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def evaluate_family(
    *,
    trades: list[dict[str, Any]],
    trials: int,
    venue: str,
    min_trades: int = 300,
    dsr_threshold: float = 0.95,
    min_positive_quarter_fraction: float = 0.6,
    max_quarter_share: float = 0.4,
    min_independent_days: int = 60,
) -> ArmingVerdict:
    """Pure verdict over a family's trades ({r, epoch, side} each).

    ``trials`` is the honest count of EVERY configuration evaluated while
    searching -- understating it inflates the deflated Sharpe and is the
    classic way backtests lie to their owners.

    Anti-overfit slicing: pooled statistics alone let one lucky trend carry a
    dead strategy, so the edge must ALSO hold across time and direction --
    most quarters positive, no single quarter dominating total R, and both
    BUY and SELL sides independently positive. Quick-fire directional
    execution that only worked long-in-an-uptrend dies here, before live.
    """
    reasons: list[str] = []
    trade_rs = [float(t.get("r") or 0.0) for t in trades]
    n = len(trade_rs)
    mean_r = sum(trade_rs) / n if n else 0.0
    # Day-clustered, NOT iid over trades: correlated same-session outcomes
    # must not masquerade as independent evidence.
    ci_lo, ci_hi = clustered_bootstrap_ci_mean(trades) if n else (0.0, 0.0)
    independent_days = len(
        {
            _day_key(t["epoch"])
            for t in trades
            if isinstance(t.get("epoch"), (int, float)) and t["epoch"] > 0
        }
    )
    if n < min_trades:
        reasons.append(f"insufficient_trades:{n}<{min_trades}")
    if independent_days < min_independent_days:
        # Trade count is not evidence count. A family that fires on a handful
        # of days has seen a handful of markets, whatever its trade tally.
        reasons.append(
            f"insufficient_independent_days:{independent_days}<{min_independent_days}"
        )
    if str(venue) == "interbank_raw":
        reasons.append("costs_not_venue_realistic")
    if ci_lo <= 0.0:
        reasons.append(f"ci_lower_bound_not_positive:{ci_lo:.4f}")

    # Time slices: the edge must repeat, not have happened once.
    by_quarter: dict[str, list[float]] = {}
    for t in trades:
        epoch = t.get("epoch")
        if isinstance(epoch, (int, float)) and float(epoch) > 0:
            by_quarter.setdefault(_quarter_key(epoch), []).append(float(t.get("r") or 0.0))
    days_by_quarter: dict[str, set[str]] = {}
    for t in trades:
        epoch = t.get("epoch")
        if isinstance(epoch, (int, float)) and float(epoch) > 0:
            days_by_quarter.setdefault(_quarter_key(epoch), set()).add(_day_key(epoch))
    quarter_stats = {
        q: {
            "trades": float(len(rs)),
            "total_r": sum(rs),
            "mean_r": sum(rs) / len(rs),
            "days": float(len(days_by_quarter.get(q, ()))),
        }
        for q, rs in sorted(by_quarter.items())
    }
    # A quarter counts as evidence only if it saw enough distinct DAYS.
    scored = {
        q: s for q, s in quarter_stats.items() if s["trades"] >= 10 and s["days"] >= 5
    }
    if len(scored) < 4:
        reasons.append(f"insufficient_time_slices:{len(scored)}<4")
    else:
        positive = sum(1 for s in scored.values() if s["mean_r"] > 0.0)
        fraction = positive / len(scored)
        if fraction < min_positive_quarter_fraction:
            reasons.append(
                f"edge_not_repeatable_across_quarters:{fraction:.2f}<"
                f"{min_positive_quarter_fraction}"
            )
        total_r = sum(s["total_r"] for s in scored.values())
        if total_r > 0.0:
            top_share = max(s["total_r"] for s in scored.values()) / total_r
            if top_share > max_quarter_share:
                reasons.append(
                    f"single_quarter_dependence:{top_share:.2f}>{max_quarter_share}"
                )

    # Direction slices: long-only or short-only profit is a trend bet in
    # disguise, not directional execution.
    side_means: dict[str, float] = {}
    side_days: dict[str, int] = {}
    for side in ("BUY", "SELL"):
        side_trades = [t for t in trades if str(t.get("side") or "").upper() == side]
        if not side_trades:
            continue
        side_means[side] = sum(float(t.get("r") or 0.0) for t in side_trades) / len(side_trades)
        side_days[side] = len(
            {
                _day_key(t["epoch"])
                for t in side_trades
                if isinstance(t.get("epoch"), (int, float)) and t["epoch"] > 0
            }
        )
    if len(side_means) < 2:
        reasons.append("one_sided_trade_population")
    else:
        for side, side_mean in side_means.items():
            if side_mean <= 0.0:
                reasons.append(f"direction_dependent_edge:{side}:{side_mean:.4f}")
            # Each direction needs its own independent evidence, or "both
            # sides positive" is satisfied by one lucky session per side.
            if side_days.get(side, 0) < max(10, min_independent_days // 4):
                reasons.append(
                    f"direction_evidence_too_thin:{side}:{side_days.get(side, 0)}d"
                )
    dsr = 0.0
    if n >= 2:
        variance = sum((r - mean_r) ** 2 for r in trade_rs) / (n - 1)
        std = variance**0.5
        if std > 0.0:
            skew = sum((r - mean_r) ** 3 for r in trade_rs) / (n * std**3)
            kurt = sum((r - mean_r) ** 4 for r in trade_rs) / (n * std**4)
            # Variance of Sharpe across trials: without the per-config series
            # here, use the conservative iid approximation 1/n per trial.
            result = deflated_sharpe_ratio(
                sharpe_per_period=mean_r / std,
                n_obs=n,
                n_trials=max(1, int(trials)),
                sharpe_variance_across_trials=1.0 / max(1, n),
                skew=skew,
                kurtosis=kurt,
            )
            dsr = float(dict(result).get("dsr") or 0.0)
    if dsr < dsr_threshold:
        reasons.append(f"deflated_sharpe_below_threshold:{dsr:.3f}<{dsr_threshold}")
    return ArmingVerdict(
        passed=not reasons,
        reasons=reasons,
        trades=n,
        mean_r=mean_r,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        deflated_sharpe=dsr,
        trials=int(trials),
        venue=str(venue),
        independent_days=independent_days,
        quarter_stats=quarter_stats,
        side_means=side_means,
    )


def issue_arming_certificate(
    *,
    verdict: ArmingVerdict,
    config: ScalpConfig,
    family: str,
    symbols: list[str],
    data_root: Path | None = None,
    now_epoch: float | None = None,
) -> Path:
    """Write the certificate IFF the verdict passed; raises otherwise.

    The refusal is an exception, not a return value, so no code path can
    accidentally treat a failed battery as armed.
    """
    if not verdict.passed:
        raise PermissionError(
            "arming refused: " + "; ".join(verdict.reasons or ["battery_failed"])
        )
    now = float(now_epoch if now_epoch is not None else time.time())
    root = Path(data_root if data_root is not None else config.data_root)
    root.mkdir(parents=True, exist_ok=True)
    cert: dict[str, Any] = {
        "family": str(family),
        "config_sha256": scalp_config_sha256(config),
        "symbols": [str(s).upper() for s in symbols],
        "issued_at_epoch": now,
        "expires_at_epoch": now + CERT_VALIDITY_SECS,
        "evidence": verdict.to_dict() | {"passed": True},
    }
    cert["cert_sha256"] = certificate_body_sha256(cert)
    path = root / DEFAULT_CERT_RELPATH
    path.write_text(json.dumps(cert, indent=1), encoding="utf-8")
    return path


def scalp_config_sha256(config: ScalpConfig) -> str:
    """Sha over the execution-semantic scalp config fields."""
    payload = {
        field.name: getattr(config, field.name)
        for field in dataclasses.fields(config)
        if field.name not in {"bridge_url", "api_key_file", "data_root"}
    }
    return config_sha256(payload)


def load_certificate(data_root: str | Path) -> dict[str, Any] | None:
    path = Path(data_root) / DEFAULT_CERT_RELPATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def trade_rs_from_backtest_json(paths: list[Path]) -> tuple[list[float], str]:
    """Not implemented from summaries: summaries carry aggregates, not trades.

    Kept explicit so nobody 'helpfully' reconstructs a trade series from
    aggregate stats -- rerun the backtest with a per-trade dump instead.
    """
    raise NotImplementedError(
        "arming needs per-trade R series; rerun fxstack.scalp.backtest with "
        "--trades-out and feed that file"
    )


def trades_from_ledger(ledger_dir: Path) -> list[dict[str, Any]]:
    """Per-trade records from the live shadow ledger (fill records)."""
    out: list[dict[str, Any]] = []
    for path in sorted(Path(ledger_dir).glob("ledger_*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("kind") != "fill":
                        continue
                    value = rec.get("pnl_r")
                    if isinstance(value, (int, float)):
                        out.append(
                            {
                                "r": float(value),
                                "epoch": float(rec.get("exit_epoch") or 0.0),
                                "side": str(rec.get("side") or ""),
                            }
                        )
        except OSError:
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trades-json", default=None,
                    help="JSON file: {'trade_rs': [...], 'venue': '...'}")
    ap.add_argument("--ledger-dir", default=None,
                    help="scalp ledger dir; uses live shadow fills (venue=live)")
    ap.add_argument("--trials", type=int, required=True,
                    help="HONEST count of every config evaluated in the search")
    ap.add_argument("--family", required=True)
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--issue", action="store_true",
                    help="write the certificate on pass (default: verdict only)")
    args = ap.parse_args(argv)

    if args.trades_json:
        payload = json.loads(Path(args.trades_json).read_text(encoding="utf-8"))
        trades = [dict(t) for t in payload.get("trades") or []]
        venue = str(payload.get("venue") or "unknown")
    elif args.ledger_dir:
        trades = trades_from_ledger(Path(args.ledger_dir))
        venue = "live_shadow"
    else:
        raise SystemExit("one of --trades-json / --ledger-dir is required")

    verdict = evaluate_family(trades=trades, trials=args.trials, venue=venue)
    print(json.dumps(verdict.to_dict(), indent=1))
    if args.issue and verdict.passed:
        config = ScalpConfig()
        path = issue_arming_certificate(
            verdict=verdict,
            config=config,
            family=args.family,
            symbols=[s.strip().upper() for s in args.symbols.split(",") if s.strip()],
        )
        print(f"certificate written: {path}")
    elif args.issue:
        print("certificate REFUSED: " + "; ".join(verdict.reasons))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
