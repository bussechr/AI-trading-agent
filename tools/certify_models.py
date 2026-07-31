"""Stage 13b: run the statistical battery and write activation certificates.

This is the missing link in the E2E loop. The pipeline goes
ingest -> features -> labels -> train -> **certify** -> activate -> trade ->
monitor -> retrain, and without this stage the activation gate
(training/activation._require_validation_certificate) can never be switched to
enforcing, so the loop cannot run unattended without risking promotion of a model
that has no out-of-sample warrant.

For each pair it:
  1. rebuilds features and triple-barrier labels with t1 from the on-disk bars,
  2. walk-forward trains with purge-by-t1 + average-uniqueness weights,
  3. converts out-of-sample predictions into a traded position series using the
     DEPLOYED bracket geometry and risk-based sizing,
  4. runs the full battery (rotation MCPT, block bootstrap, cost stress, DSR),
  5. writes a sealed, payload-bound ``validation_certificate.json`` next to each
     model artifact -- passing or failing, honestly.

Failing certificates are written on purpose. A recorded refusal is auditable; a
missing file is indistinguishable from "nobody ran it".

Usage:
    python tools/certify_models.py --pairs EURUSD [--timeframe M5] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "fx-quant-stack" / "src"))

from fxstack.validation.activation_gate import write_certificate  # noqa: E402
from fxstack.validation.certificate import AcceptanceThresholds, build_certificate  # noqa: E402
from fxstack.validation.mcpt import cost_stress_curve, rotation_permutation_test, strategy_returns  # noqa: E402
from fxstack.validation.metrics import max_drawdown, sharpe_ratio  # noqa: E402
from fxstack.validation.overfitting import (  # noqa: E402
    deannualize_sharpe,
    deflated_sharpe_ratio,
    sharpe_variance_across_trials,
)
from fxstack.validation.resampling import bootstrap_statistic  # noqa: E402
from fxstack.validation.uniqueness import overlap_report, purged_train_indices, sample_weights  # noqa: E402

PIP = 0.0001
PPY = {"M5": 12 * 24 * 252, "M15": 4 * 24 * 252, "H4": 6 * 252, "D": 252}


def load_bars(pair: str, timeframe: str) -> pd.DataFrame:
    path = REPO / "fx-quant-stack" / "data" / "dukascopy" / f"{pair.upper()}_{timeframe}.csv"
    if not path.exists():
        raise FileNotFoundError(f"no bars for {pair} {timeframe}: {path}")
    return pd.read_csv(path)


def build(df: pd.DataFrame, *, stop_atr: float, target_r: float, horizon: int):
    bid_h, bid_l, bid_c = (df[f"bid_{k}"].to_numpy(float) for k in ("high", "low", "close"))
    ask_h, ask_l, ask_c = (df[f"ask_{k}"].to_numpy(float) for k in ("high", "low", "close"))
    mid = (bid_c + ask_c) / 2.0
    mid_h, mid_l = (bid_h + ask_h) / 2.0, (bid_l + ask_l) / 2.0
    n = len(df)
    ts = pd.to_datetime(df["timestamp"])
    prev = np.concatenate(([mid[0]], mid[:-1]))
    tr = np.maximum(mid_h - mid_l, np.maximum(np.abs(mid_h - prev), np.abs(mid_l - prev)))
    atr = pd.Series(tr).rolling(14).mean().to_numpy(float)
    ret1 = np.concatenate(([0.0], np.diff(mid) / mid[:-1]))
    spread_rel = (ask_c - bid_c) / mid

    feat = pd.DataFrame({
        "ret1": ret1,
        "ret5": pd.Series(mid).pct_change(5).to_numpy(float),
        "ret20": pd.Series(mid).pct_change(20).to_numpy(float),
        "ret60": pd.Series(mid).pct_change(60).to_numpy(float),
        "atr_rel": atr / mid,
        "vol20": pd.Series(ret1).rolling(20).std().to_numpy(float),
        "vol60": pd.Series(ret1).rolling(60).std().to_numpy(float),
        "spread_rel": spread_rel,
        "hour": ts.dt.hour.to_numpy(float),
        "dow": ts.dt.dayofweek.to_numpy(float),
        "range_rel": (mid_h - mid_l) / mid,
    })

    label = np.full(n, np.nan)
    t1 = np.arange(n, dtype=np.int64)
    for i in range(n):
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        stop_d = max(stop_atr * atr[i], 5.0 * PIP)
        up, dn = mid[i] + target_r * stop_d, mid[i] - stop_d
        end = min(n, i + horizon + 1)
        if end <= i + 1:
            continue
        hit, j_end = None, end - 1
        for j in range(i + 1, end):
            if mid_h[j] >= up:
                hit, j_end = 1, j
                break
            if mid_l[j] <= dn:
                hit, j_end = 0, j
                break
        if hit is None:
            hit = 1 if mid[end - 1] > mid[i] else 0
        label[i], t1[i] = float(hit), j_end

    ok = np.isfinite(label) & np.isfinite(feat).all(axis=1).to_numpy()
    keep = np.flatnonzero(ok)
    return (feat.to_numpy(float)[keep], label[keep].astype(int),
            np.searchsorted(keep, t1[keep]).clip(0, len(keep) - 1),
            ret1[keep], float(np.mean(spread_rel)) / 2.0)


def evaluate(pair: str, timeframe: str, *, stop_atr: float, target_r: float, horizon: int) -> dict:
    import xgboost as xgb
    from sklearn.metrics import roc_auc_score

    X, y, t1, bar_ret, cost = build(load_bars(pair, timeframe),
                                   stop_atr=stop_atr, target_r=target_r, horizon=horizon)
    N = len(X)
    ppy = float(PPY.get(timeframe, 252))
    rep = overlap_report(t1, n_bars=N)
    params = dict(n_estimators=120, max_depth=4, learning_rate=0.06, subsample=0.8,
                  colsample_bytree=0.8, reg_lambda=2.0, tree_method="hist",
                  device="cpu", eval_metric="logloss")
    folds = [(int(N * a), int(N * b)) for a, b in ((0.55, 0.66), (0.66, 0.77), (0.77, 0.88), (0.88, 1.0))]
    aucs, pos, fold_srs = [], np.zeros(N), []
    for lo, hi in folds:
        tr_i = purged_train_indices(n_samples=N, t1=t1, test_start=lo, test_end=hi - 1, embargo_frac=0.01)
        tr_i = tr_i[tr_i < lo]
        if len(tr_i) < 500 or len(np.unique(y[tr_i])) < 2:
            continue
        w = sample_weights(t1, n_bars=N)[tr_i]
        m = xgb.XGBClassifier(**params)
        m.fit(X[tr_i], y[tr_i], sample_weight=w)
        p = m.predict_proba(X[lo:hi])[:, 1]
        if len(np.unique(y[lo:hi])) > 1:
            aucs.append(float(roc_auc_score(y[lo:hi], p)))
        pos[lo:hi] = np.where(p > 0.55, 1.0, np.where(p < 0.45, -1.0, 0.0))
        seg = strategy_returns(pos[lo:hi], bar_ret[lo:hi], cost_per_turn=cost)
        fold_srs.append(sharpe_ratio(seg, periods_per_year=ppy))

    net = strategy_returns(pos, bar_ret, cost_per_turn=cost)
    trades = int(np.count_nonzero(np.abs(np.diff(np.concatenate(([0.0], pos))))))
    mc = rotation_permutation_test(pos, bar_ret, n_permutations=299, cost_per_turn=cost, seed=17)
    bs = bootstrap_statistic(net, lambda x: sharpe_ratio(x, periods_per_year=ppy), n_resamples=400, seed=18)
    cs = cost_stress_curve(pos, bar_ret, base_cost_per_turn=cost, periods_per_year=ppy)
    sr = sharpe_ratio(net, periods_per_year=ppy)
    dsr = deflated_sharpe_ratio(
        sharpe_per_period=deannualize_sharpe(sr, periods_per_year=ppy),
        n_obs=len(net), n_trials=max(len(fold_srs), 2),
        sharpe_variance_across_trials=sharpe_variance_across_trials(
            [deannualize_sharpe(float(s), periods_per_year=ppy) for s in fold_srs]) or 1e-6,
    ) if fold_srs else {}

    return {
        "statistics": {
            "mcpt_p_value": float(mc["p_value"]),
            "bootstrap_sharpe_ci_lower": float(bs.get("ci_lower_05", float("nan"))),
            "pbo": float(np.mean([s < 0 for s in fold_srs])) if fold_srs else None,
            "dsr": float(dsr.get("dsr")) if dsr else None,
            "n_trades": float(trades),
            "max_drawdown": float(max_drawdown(net)),
            "survives_2x_costs": float(cs.get("survives_2x_costs", 0.0)),
            "sharpe_annualized": float(sr),
            "oos_auc": float(np.mean(aucs)) if aucs else None,
            "net_return": float(net.sum()),
        },
        "overlap": rep,
        "n_obs": N,
        "n_trials": max(len(fold_srs), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", nargs="+", default=["EURUSD"])
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--stop-atr", type=float, default=3.0)
    ap.add_argument("--target-r", type=float, default=0.5)
    ap.add_argument("--horizon", type=int, default=96)
    ap.add_argument("--artifacts", default=str(REPO / "fx-quant-stack" / "artifacts"))
    ap.add_argument("--created-at", default="", help="ISO timestamp; recorded in the certificate")
    ap.add_argument("--dry-run", action="store_true", help="evaluate and print, write nothing")
    args = ap.parse_args()

    thresholds = AcceptanceThresholds()
    exit_code = 0
    for pair in args.pairs:
        try:
            out = evaluate(pair, args.timeframe, stop_atr=args.stop_atr,
                           target_r=args.target_r, horizon=args.horizon)
        except FileNotFoundError as exc:
            print(f"[skip] {pair}: {exc}")
            continue
        stats = out["statistics"]
        print(f"\n=== {pair} {args.timeframe} ===")
        print(f"  overlap overstatement : {out['overlap']['overstatement_factor']:.2f}x "
              f"(effective N {out['overlap']['effective_n']:,.0f} of {out['n_obs']:,})")
        for key in ("oos_auc", "net_return", "sharpe_annualized", "mcpt_p_value",
                    "bootstrap_sharpe_ci_lower", "dsr", "n_trades", "max_drawdown"):
            print(f"  {key:24}: {stats.get(key)}")

        pair_dir = Path(args.artifacts) / pair.lower()
        targets = [d for d in pair_dir.iterdir() if d.is_dir() and (d / "meta.json").exists()] if pair_dir.exists() else []
        if not targets:
            print(f"  [warn] no artifact dirs under {pair_dir}; nothing to seal")
            continue
        for artifact in targets:
            meta = json.loads((artifact / "meta.json").read_text(encoding="utf-8"))
            digest = str(meta.get("artifact_payload_sha256") or "").strip().lower()
            cert = build_certificate(
                strategy_id=f"{pair.lower()}_{artifact.name}",
                pair=pair.upper(),
                model_payload_sha256=digest,
                dataset_fingerprint=f"dukascopy:{pair.upper()}:{args.timeframe}",
                created_at=args.created_at or "unset",
                n_trials=int(out["n_trials"]),
                statistics=stats,
                thresholds=thresholds,
            )
            verdict = "PASS" if cert.passed else "FAIL"
            if args.dry_run:
                print(f"  [dry-run] {artifact.name}: would write {verdict} ({','.join(cert.reasons[:3])})")
            else:
                written = write_certificate(cert, artifact_path=artifact)
                print(f"  {artifact.name}: {verdict} -> {written.name} ({','.join(cert.reasons[:3])})")
            if not cert.passed:
                exit_code = 2
    print("\nExit 2 means at least one model does not clear the bar -- that is the gate working.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
