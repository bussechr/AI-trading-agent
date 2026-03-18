from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from tools import fetch_dukascopy_matrix
from tools import fxstack_full_backtest



def _m1_frame(rows: int = 20) -> pd.DataFrame:
    ts = pd.date_range("2026-01-01 00:00:00", periods=rows, freq="1min", tz="UTC")
    base = 1.1
    return pd.DataFrame(
        {
            "timestamp": ts,
            "bid_open": [base + (i * 0.00001) for i in range(rows)],
            "bid_high": [base + (i * 0.00001) + 0.0001 for i in range(rows)],
            "bid_low": [base + (i * 0.00001) - 0.0001 for i in range(rows)],
            "bid_close": [base + (i * 0.00001) + 0.00002 for i in range(rows)],
            "ask_open": [base + (i * 0.00001) + 0.00003 for i in range(rows)],
            "ask_high": [base + (i * 0.00001) + 0.00012 for i in range(rows)],
            "ask_low": [base + (i * 0.00001) - 0.00007 for i in range(rows)],
            "ask_close": [base + (i * 0.00001) + 0.00005 for i in range(rows)],
            "volume": [100.0 + i for i in range(rows)],
        }
    )



def test_fetch_dukascopy_matrix_writes_resampled_files(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fetch_dukascopy_matrix, "_resolve_instrument", lambda pair: ("EURUSD", "INSTRUMENT_FX_MAJORS_EUR_USD"))
    monkeypatch.setattr(
        fetch_dukascopy_matrix,
        "_fetch_m1_bid_ask",
        lambda **kwargs: _m1_frame(30),
    )

    out = tmp_path / "summary.json"
    args = argparse.Namespace(
        source_root=str(tmp_path / "dukascopy"),
        pairs="EURUSD",
        timeframes="M1,M5,M15,H4,D",
        start="2024-01-01T00:00:00Z",
        end="2024-02-01T00:00:00Z",
        max_retries=2,
        limit=1000,
        debug=False,
        mid_only_fallback=False,
        resume=True,
        overwrite=False,
        out=str(out),
    )
    rc = fetch_dukascopy_matrix.run(args)
    assert rc == 0

    summary = json.loads(out.read_text(encoding="utf-8"))
    assert bool(summary["passed"]) is True
    result = summary["results"][0]
    assert result["status"] == "ok"
    for tf in ("M1", "M5", "M15", "H4", "D"):
        p = Path(result["files"][tf]["path"])
        assert p.exists(), tf



def test_fetch_dukascopy_matrix_reports_pair_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fetch_dukascopy_matrix, "_resolve_instrument", lambda pair: ("EURUSD", "INSTRUMENT_FX_MAJORS_EUR_USD"))

    def _boom(**kwargs):
        raise RuntimeError("fetch failed")

    monkeypatch.setattr(fetch_dukascopy_matrix, "_fetch_m1_bid_ask", _boom)

    args = argparse.Namespace(
        source_root=str(tmp_path / "dukascopy"),
        pairs="EURUSD",
        timeframes="M1,M5",
        start="2024-01-01T00:00:00Z",
        end="2024-02-01T00:00:00Z",
        max_retries=2,
        limit=1000,
        debug=False,
        mid_only_fallback=False,
        resume=True,
        overwrite=False,
        out="",
    )
    rc = fetch_dukascopy_matrix.run(args)
    assert rc == 2



def test_full_backtest_writes_artifacts(monkeypatch, tmp_path: Path):
    class _FakeSettings:
        normalized_data_provider = "dukascopy"
        swing_model_policy = "transformer_primary_xgb_fallback"
        intraday_model_policy = "tcn_primary_xgb_fallback"

    monkeypatch.setattr(fxstack_full_backtest, "_score_pair", lambda **kwargs: (
        fxstack_full_backtest.PairBacktestResult(
            pair="EURUSD",
            status="ok",
            rows_total=100,
            rows_scored=100,
            trades=4.0,
            mean_net_edge_bps=2.5,
            positive_share=0.6,
            allowed_count=20,
            rejected_count=80,
            error="",
        ),
        pd.DataFrame(
            [
                {
                    "pair": "EURUSD",
                    "ts": "2026-01-01T00:00:00Z",
                    "allowed": True,
                    "rejection_reason": "none",
                    "side": "long",
                    "expected_edge_bps": 5.0,
                    "spread_bps": 1.0,
                    "regime_prob": 0.7,
                    "swing_prob": 0.8,
                    "entry_prob": 0.75,
                    "trade_prob": 0.74,
                }
            ]
        ),
    ))

    monkeypatch.setattr(fxstack_full_backtest, "_load_settings", lambda: _FakeSettings())

    out_dir = tmp_path / "backtest"
    args = argparse.Namespace(
        pairs="EURUSD",
        timeframe="M5",
        feature_root=str(tmp_path / "features"),
        artifact_root=str(tmp_path / "artifacts"),
        out_dir=str(out_dir),
        max_rows_per_pair=0,
        sample_rows_per_pair=10,
        require_nonzero_trades=True,
    )

    rc = fxstack_full_backtest.run(args)
    assert rc == 0
    assert (out_dir / "per_pair.json").exists()
    assert (out_dir / "aggregate.json").exists()
    assert (out_dir / "signals_sample.csv").exists()



def test_full_backtest_fails_when_nonzero_trade_required(monkeypatch, tmp_path: Path):
    class _FakeSettings:
        normalized_data_provider = "dukascopy"
        swing_model_policy = "transformer_primary_xgb_fallback"
        intraday_model_policy = "tcn_primary_xgb_fallback"

    monkeypatch.setattr(fxstack_full_backtest, "_score_pair", lambda **kwargs: (
        fxstack_full_backtest.PairBacktestResult(
            pair="EURUSD",
            status="ok",
            rows_total=100,
            rows_scored=100,
            trades=0.0,
            mean_net_edge_bps=0.0,
            positive_share=0.0,
            allowed_count=0,
            rejected_count=100,
            error="",
        ),
        pd.DataFrame(),
    ))

    monkeypatch.setattr(fxstack_full_backtest, "_load_settings", lambda: _FakeSettings())

    out_dir = tmp_path / "backtest"
    args = argparse.Namespace(
        pairs="EURUSD",
        timeframe="M5",
        feature_root=str(tmp_path / "features"),
        artifact_root=str(tmp_path / "artifacts"),
        out_dir=str(out_dir),
        max_rows_per_pair=0,
        sample_rows_per_pair=10,
        require_nonzero_trades=True,
    )

    rc = fxstack_full_backtest.run(args)
    assert rc == 2
