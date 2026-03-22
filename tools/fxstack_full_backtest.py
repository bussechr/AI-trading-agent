from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_PAIRS = [
    "EURUSD",
    "USDJPY",
    "GBPUSD",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
    "EURGBP",
    "EURJPY",
    "NZDUSD",
]


@dataclass(slots=True)
class PairBacktestResult:
    pair: str
    status: str
    rows_total: int
    rows_scored: int
    trades: float
    mean_net_edge_bps: float
    positive_share: float
    allowed_count: int
    rejected_count: int
    error: str = ""



def _parse_csv_list(raw: str) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").split(","):
        sym = str(part).strip().upper()
        if sym:
            out.append(sym)
    return out



def _finite_or_zero(v: object) -> float:
    try:
        f = float(v)
    except Exception:
        return 0.0
    return f if math.isfinite(f) else 0.0



def _load_models_for_pair(pair: str, artifact_root: Path, swing_policy: str, intraday_policy: str):
    from fxstack.models.intraday_tcn import IntradayTCN
    from fxstack.models.intraday_xgb import IntradayXGB
    from fxstack.models.meta_filter import MetaFilterXGB
    from fxstack.models.regime_hmm import RegimeHMM
    from fxstack.models.swing_transformer import SwingTransformer
    from fxstack.models.swing_xgb import SwingXGB

    pair_root = artifact_root / str(pair).lower()
    regime = RegimeHMM.load(pair_root / "regime_hmm")
    meta = MetaFilterXGB.load(pair_root / "meta_filter")

    swing_primary = None
    swing_secondary = None
    if str(swing_policy).strip().lower() == "transformer_primary_xgb_fallback":
        try:
            swing_primary = SwingTransformer.load(pair_root / "swing_transformer")
        except Exception:
            swing_primary = None
        swing_secondary = SwingXGB.load(pair_root / "swing_xgb")
    else:
        swing_primary = SwingXGB.load(pair_root / "swing_xgb")

    intraday_primary = None
    intraday_secondary = None
    if str(intraday_policy).strip().lower() == "tcn_primary_xgb_fallback":
        try:
            intraday_primary = IntradayTCN.load(pair_root / "intraday_tcn")
        except Exception:
            intraday_primary = None
        intraday_secondary = IntradayXGB.load(pair_root / "intraday_xgb")
    else:
        intraday_primary = IntradayXGB.load(pair_root / "intraday_xgb")

    if swing_primary is None:
        if swing_secondary is None:
            raise RuntimeError(f"{pair}: no swing model available")
        swing_primary = swing_secondary

    if intraday_primary is None:
        if intraday_secondary is None:
            raise RuntimeError(f"{pair}: no intraday model available")
        intraday_primary = intraday_secondary

    return regime, swing_primary, intraday_primary, meta



def _score_pair(
    *,
    pair: str,
    timeframe: str,
    feature_root: Path,
    artifact_root: Path,
    provider: str,
    swing_policy: str,
    intraday_policy: str,
    max_rows_per_pair: int,
    sample_rows: int,
) -> tuple[PairBacktestResult, pd.DataFrame]:
    from fxstack.backtest.engine import evaluate_signals
    from fxstack.backtest.reports import summarize_backtest
    from fxstack.io.parquet_store import ParquetStore
    from fxstack.live.policy import compute_expected_edge_bps, normalize_spread_bps
    from fxstack.live.scorer import LiveScorer

    store = ParquetStore(feature_root)
    feats = store.read_pair_timeframe(provider=provider, pair=str(pair).upper(), timeframe=str(timeframe).upper())
    if feats.empty:
        return (
            PairBacktestResult(
                pair=str(pair).upper(),
                status="error",
                rows_total=0,
                rows_scored=0,
                trades=0.0,
                mean_net_edge_bps=0.0,
                positive_share=0.0,
                allowed_count=0,
                rejected_count=0,
                error="no_feature_rows",
            ),
            pd.DataFrame(),
        )

    feats = feats.sort_values("ts").reset_index(drop=True)
    if int(max_rows_per_pair) > 0 and len(feats) > int(max_rows_per_pair):
        feats = feats.tail(int(max_rows_per_pair)).reset_index(drop=True)

    rows_total = int(len(feats))
    regime, swing, intraday, meta = _load_models_for_pair(
        pair=pair,
        artifact_root=artifact_root,
        swing_policy=swing_policy,
        intraday_policy=intraday_policy,
    )
    scorer = LiveScorer(regime_model=regime, swing_model=swing, intraday_model=intraday, meta_model=meta)

    signal_rows: list[dict[str, Any]] = []
    for _, row in feats.iterrows():
        row_df = pd.DataFrame([row])
        expected_edge_bps = float(compute_expected_edge_bps(row_df))
        spread_bps, spread_unit_source = normalize_spread_bps(row=dict(row), pair=str(pair).upper())
        sig = scorer.score(
            row_df,
            spread_bps=float(spread_bps),
            expected_edge_bps=float(expected_edge_bps),
            spread_unit_source=str(spread_unit_source),
        )
        signal_rows.append(
            {
                "pair": str(pair).upper(),
                "ts": str(row.get("ts", "")),
                "allowed": bool(sig.allowed),
                "rejection_reason": str(sig.rejection_reason),
                "side": str(sig.side),
                "expected_edge_bps": float(sig.expected_edge_bps),
                "spread_bps": float(sig.spread_bps),
                "regime_prob": float(sig.regime_prob),
                "swing_prob": float(sig.swing_prob),
                "entry_prob": float(sig.entry_prob),
                "trade_prob": float(sig.trade_prob),
                "policy_version": str(sig.policy_version),
                "edge_formula_id": str(sig.edge_formula_id),
                "threshold_snapshot": dict(sig.threshold_snapshot),
                "spread_unit_source": str(sig.spread_unit_source),
                "spread_conversion_method": str(spread_unit_source),
            }
        )

    signals = pd.DataFrame(signal_rows)
    scored = evaluate_signals(signals[["pair", "ts", "expected_edge_bps", "spread_bps", "allowed"]])
    summary = summarize_backtest(scored)

    trades = _finite_or_zero(summary.get("trades", 0.0))
    mean_net = _finite_or_zero(summary.get("mean_net_edge_bps", 0.0))
    positive_share = _finite_or_zero(summary.get("positive_share", 0.0))

    if not all(math.isfinite(x) for x in [trades, mean_net, positive_share]):
        return (
            PairBacktestResult(
                pair=str(pair).upper(),
                status="error",
                rows_total=rows_total,
                rows_scored=0,
                trades=0.0,
                mean_net_edge_bps=0.0,
                positive_share=0.0,
                allowed_count=0,
                rejected_count=0,
                error="non_finite_metrics",
            ),
            pd.DataFrame(),
        )

    sample = signals.head(max(1, int(sample_rows))).copy()
    return (
        PairBacktestResult(
            pair=str(pair).upper(),
            status="ok",
            rows_total=rows_total,
            rows_scored=int(len(signals)),
            trades=float(trades),
            mean_net_edge_bps=float(mean_net),
            positive_share=float(positive_share),
            allowed_count=int((signals["allowed"] == True).sum()),
            rejected_count=int((signals["allowed"] != True).sum()),
            error="",
        ),
        sample,
    )



def _load_settings():
    from fxstack.settings import get_settings

    return get_settings()


def run(args: argparse.Namespace) -> int:
    s = _load_settings()
    provider = str(s.normalized_data_provider)

    pairs = _parse_csv_list(args.pairs) or list(DEFAULT_PAIRS)
    timeframe = str(args.timeframe).upper()
    feature_root = Path(str(args.feature_root))
    artifact_root = Path(str(args.artifact_root))
    out_dir = Path(str(args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)

    per_pair: list[dict[str, Any]] = []
    sample_frames: list[pd.DataFrame] = []

    for pair in pairs:
        try:
            result, sample = _score_pair(
                pair=pair,
                timeframe=timeframe,
                feature_root=feature_root,
                artifact_root=artifact_root,
                provider=provider,
                swing_policy=str(s.swing_model_policy),
                intraday_policy=str(s.intraday_model_policy),
                max_rows_per_pair=int(args.max_rows_per_pair),
                sample_rows=int(args.sample_rows_per_pair),
            )
            per_pair.append(asdict(result))
            if not sample.empty:
                sample_frames.append(sample)
        except Exception as exc:
            per_pair.append(
                asdict(
                    PairBacktestResult(
                        pair=str(pair).upper(),
                        status="error",
                        rows_total=0,
                        rows_scored=0,
                        trades=0.0,
                        mean_net_edge_bps=0.0,
                        positive_share=0.0,
                        allowed_count=0,
                        rejected_count=0,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            )

    ok_rows = [r for r in per_pair if str(r.get("status")) == "ok"]
    failed_rows = [r for r in per_pair if str(r.get("status")) != "ok"]

    total_trades = float(sum(_finite_or_zero(r.get("trades", 0.0)) for r in ok_rows))
    mean_net_values = [_finite_or_zero(r.get("mean_net_edge_bps", 0.0)) for r in ok_rows]
    mean_positive_values = [_finite_or_zero(r.get("positive_share", 0.0)) for r in ok_rows]

    aggregate = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pairs_requested": pairs,
        "pairs_ok": [str(r.get("pair")) for r in ok_rows],
        "pairs_failed": [str(r.get("pair")) for r in failed_rows],
        "ok_count": int(len(ok_rows)),
        "failed_count": int(len(failed_rows)),
        "total_trades": float(total_trades),
        "mean_net_edge_bps": float(sum(mean_net_values) / len(mean_net_values)) if mean_net_values else 0.0,
        "mean_positive_share": float(sum(mean_positive_values) / len(mean_positive_values)) if mean_positive_values else 0.0,
        "any_nonzero_trades": bool(any(_finite_or_zero(r.get("trades", 0.0)) > 0.0 for r in ok_rows)),
        "all_metrics_finite": bool(
            all(
                math.isfinite(_finite_or_zero(r.get("trades", 0.0)))
                and math.isfinite(_finite_or_zero(r.get("mean_net_edge_bps", 0.0)))
                and math.isfinite(_finite_or_zero(r.get("positive_share", 0.0)))
                for r in ok_rows
            )
        ),
        "provider": provider,
        "timeframe": timeframe,
        "feature_root": str(feature_root),
        "artifact_root": str(artifact_root),
        "policy_version": str(getattr(s, "policy_version", "fxstack_policy_v1")),
        "edge_formula_id": "ret_1_bps_v1",
        "spread_conversion_method": "normalize_spread_bps",
    }

    (out_dir / "per_pair.json").write_text(json.dumps(per_pair, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "aggregate.json").write_text(json.dumps(aggregate, indent=2, sort_keys=True), encoding="utf-8")

    if sample_frames:
        sample_df = pd.concat(sample_frames, axis=0, ignore_index=True)
    else:
        sample_df = pd.DataFrame(
            columns=[
                "pair",
                "ts",
                "allowed",
                "rejection_reason",
                "side",
                "expected_edge_bps",
                "spread_bps",
                "regime_prob",
                "swing_prob",
                "entry_prob",
                "trade_prob",
                "policy_version",
                "edge_formula_id",
                "threshold_snapshot",
                "spread_unit_source",
                "spread_conversion_method",
            ]
        )
    sample_df.to_csv(out_dir / "signals_sample.csv", index=False)

    payload = {
        "aggregate": aggregate,
        "per_pair_path": str(out_dir / "per_pair.json"),
        "aggregate_path": str(out_dir / "aggregate.json"),
        "signals_sample_path": str(out_dir / "signals_sample.csv"),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))

    if failed_rows:
        return 2
    if bool(args.require_nonzero_trades) and not bool(aggregate["any_nonzero_trades"]):
        return 2
    if not bool(aggregate["all_metrics_finite"]):
        return 2
    return 0



def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run multi-pair model-driven offline backtest and write artifacts.")
    ap.add_argument("--pairs", default=",".join(DEFAULT_PAIRS))
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--feature-root", default="fx-quant-stack/data/features")
    ap.add_argument("--artifact-root", default="fx-quant-stack/artifacts")
    ap.add_argument("--out-dir", default="docs/backtests/latest/backtest_full")
    ap.add_argument("--max-rows-per-pair", type=int, default=0)
    ap.add_argument("--sample-rows-per-pair", type=int, default=25)
    ap.add_argument("--require-nonzero-trades", action="store_true", default=False)
    return ap



def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(int(run(args) or 0))


if __name__ == "__main__":
    main()
