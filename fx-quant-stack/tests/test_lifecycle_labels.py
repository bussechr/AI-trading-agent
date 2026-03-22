from __future__ import annotations

import pandas as pd

from fxstack.labels.exit_labels import build_exit_labels
from fxstack.labels.meta_label import build_meta_labels
from fxstack.labels.reversal_labels import build_reversal_labels


def _feature_frame(rows: int = 240) -> pd.DataFrame:
    out = []
    px = 1.10
    for i in range(rows):
        px += 0.00015 if i % 7 != 0 else -0.00005
        out.append(
            {
                "pair": "EURUSD",
                "ts": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=5 * i),
                "timeframe": "M5",
                "mid_open": px - 0.0001,
                "mid_high": px + 0.0003,
                "mid_low": px - 0.0003,
                "mid_close": px,
                "spread_bps": 0.8 + (0.01 * (i % 5)),
                "ret_1": 0.0002 if i % 3 else -0.0001,
                "vol_20": 0.0008 + (0.00001 * (i % 10)),
                "atr_14": 0.0009,
                "swing_prob": 0.7 if i % 2 == 0 else 0.3,
                "mae_proxy_12": -0.2,
            }
        )
    return pd.DataFrame(out)


def test_meta_labels_add_stress_targets_and_weights() -> None:
    df = _feature_frame()
    df["realized_edge_bps"] = (df["ret_1"] * 10000.0) - df["spread_bps"]
    out = build_meta_labels(df)
    assert "meta_label" in out.columns
    assert "meta_label_stressed" in out.columns
    assert "sample_weight" in out.columns
    assert out["sample_weight"].gt(0).all()


def test_exit_labels_produce_action_ids_and_quality_tags() -> None:
    out = build_exit_labels(_feature_frame(), method="trade_outcome")
    assert "exit_action" in out.columns
    assert "exit_action_id" in out.columns
    assert set(out["exit_action"].unique()).issubset({"hold", "reduce", "partial_tp", "tighten_stop", "exit"})
    assert {"good_entry", "bad_hold", "bad_exit", "false_reversal"}.issubset(set(out.columns))


def test_reversal_labels_separate_failure_and_opportunity() -> None:
    out = build_reversal_labels(_feature_frame())
    assert {"thesis_failure", "opposite_opportunity", "reversal_timing_quality"}.issubset(set(out.columns))
    assert set(out["thesis_failure"].unique()).issubset({0, 1})
    assert set(out["opposite_opportunity"].unique()).issubset({0, 1})
