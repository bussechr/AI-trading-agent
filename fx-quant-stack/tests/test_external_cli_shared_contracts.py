from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from fxstack.backtest.smoke import run_baseline_smoke
from fxstack.training.activation_cli import activate_models


def test_baseline_smoke_uses_shared_policy_normalization(monkeypatch) -> None:
    features = pd.DataFrame([{"pair": "EURUSD", "ts": 1}, {"pair": "EURUSD", "ts": 2}])
    read_calls: list[dict[str, str]] = []

    class _Store:
        def __init__(self, _root):
            pass

        def read_pair_timeframe(self, **kwargs):
            read_calls.append(kwargs)
            return features

    monkeypatch.setattr("fxstack.io.parquet_store.ParquetStore", _Store)
    monkeypatch.setattr(
        "fxstack.settings.get_settings",
        lambda: SimpleNamespace(normalized_data_provider="dukascopy", policy_version="policy-v-test"),
    )
    monkeypatch.setattr("fxstack.live.policy.compute_expected_edge_bps", lambda _row: 4.5)
    monkeypatch.setattr("fxstack.live.policy.normalize_spread_bps", lambda **_kwargs: (1.25, "test"))
    monkeypatch.setattr("fxstack.backtest.engine.evaluate_signals", lambda frame: frame)
    monkeypatch.setattr("fxstack.backtest.reports.summarize_backtest", lambda frame: {"rows": len(frame)})

    report, return_code = run_baseline_smoke(pair="eurusd", timeframe="m5", feature_root="features")

    assert return_code == 0
    assert read_calls == [{"provider": "dukascopy", "pair": "EURUSD", "timeframe": "M5"}]
    assert report == {
        "rows": 2,
        "policy_version": "policy-v-test",
        "edge_formula_id": "prob_weighted_opportunity_v2",
        "spread_conversion_method": "normalize_spread_bps",
    }


def test_activation_shared_entrypoint_preserves_compat_default_and_require_all(monkeypatch) -> None:
    # Import the lazy activation implementation before replacing the settings
    # factory used by the CLI. Otherwise activation and registry can bind this
    # deliberately incomplete test stub at first import and leak it into every
    # later test in the same process.
    from fxstack.training import activation as activation_module

    settings = SimpleNamespace(
        database_url="sqlite:///runtime.db",
        registry_root="registry",
        model_activation_manifest="active.json",
        pairs=["EURUSD", "USDJPY"],
        default_session_id="session",
        command_ttl_secs=60,
    )
    calls: list[dict] = []
    monkeypatch.setattr("fxstack.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        activation_module,
        "activate_pairs",
        lambda **kwargs: calls.append(kwargs) or [{"pair": "EURUSD"}],
    )
    monkeypatch.setattr(activation_module, "activate_mlflow_alias", lambda **_kwargs: [])

    report, return_code = activate_models(source="", alias="", require_all=True)

    assert return_code == 1
    assert report["source"] == "compat"
    assert report["missing_pairs"] == ["USDJPY"]
    assert calls[0]["pairs"] == ["EURUSD", "USDJPY"]
