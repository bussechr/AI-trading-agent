from __future__ import annotations

from src.trader import cli as trader_cli
from src.trader.cli import build_parser


def test_cli_parses_data_ingest():
    ns = build_parser().parse_args(["data", "ingest", "--pair", "EURUSD"])
    assert ns.cmd == "data"
    assert ns.data_cmd == "ingest"
    assert ns.pair == "EURUSD"
    assert str(ns.csv_path) == ""
    assert str(ns.source_root) == ""
    assert str(ns.file_pattern) == ""


def test_cli_parses_data_migrate_provider():
    ns = build_parser().parse_args(["data", "migrate-provider", "--store-root", "fx-quant-stack/data/raw", "--apply"])
    assert ns.cmd == "data"
    assert ns.data_cmd == "migrate-provider"
    assert bool(ns.apply) is True


def test_cli_parses_data_fetch_dukascopy_matrix():
    ns = build_parser().parse_args(["data", "fetch-dukascopy-matrix", "--", "--pairs", "EURUSD,USDJPY"])
    assert ns.cmd == "data"
    assert ns.data_cmd == "fetch-dukascopy-matrix"
    assert "--pairs" in ns.tool_args


def test_cli_parses_features_build():
    ns = build_parser().parse_args(["features", "build", "--pair", "EURUSD"])
    assert ns.cmd == "features"
    assert ns.features_cmd == "build"


def test_cli_parses_train_meta():
    ns = build_parser().parse_args(["train", "meta", "--pair", "EURUSD"])
    assert ns.cmd == "train"
    assert ns.train_cmd == "meta"


def test_cli_parses_train_all():
    ns = build_parser().parse_args(["train", "all", "--pair", "EURUSD"])
    assert ns.cmd == "train"
    assert ns.train_cmd == "all"


def test_train_all_forwards_no_with_belief(monkeypatch):
    captured: dict[str, object] = {}
    ns = build_parser().parse_args(["train", "all", "--pair", "EURUSD", "--no-with-belief"])
    monkeypatch.setattr(trader_cli, "_fxstack_guard", lambda: None)

    def _call(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = dict(kwargs)
        return 0

    monkeypatch.setattr(trader_cli.subprocess, "call", _call)

    assert trader_cli._train_all(ns) == 0
    assert "--no-with-belief" in captured["cmd"]
    assert "--no-belief" not in captured["cmd"]


def test_cli_parses_train_swing_transformer():
    ns = build_parser().parse_args(["train", "swing-transformer", "--pair", "EURUSD"])
    assert ns.cmd == "train"
    assert ns.train_cmd == "swing-transformer"


def test_cli_parses_train_intraday_tcn():
    ns = build_parser().parse_args(["train", "intraday-tcn", "--pair", "EURUSD"])
    assert ns.cmd == "train"
    assert ns.train_cmd == "intraday-tcn"


def test_cli_parses_train_deep_stale():
    ns = build_parser().parse_args(["train", "deep-stale"])
    assert ns.cmd == "train"
    assert ns.train_cmd == "deep-stale"


def test_cli_parses_live_score():
    ns = build_parser().parse_args(["live", "score", "--pair", "EURUSD"])
    assert ns.cmd == "live"
    assert ns.live_cmd == "score"


def test_cli_parses_backtest_full():
    ns = build_parser().parse_args(["backtest", "full", "--", "--pairs", "EURUSD,USDJPY"])
    assert ns.cmd == "backtest"
    assert ns.backtest_cmd == "full"
    assert "--pairs" in ns.tool_args


def test_cli_parses_db_migrate():
    ns = build_parser().parse_args(["db", "migrate"])
    assert ns.cmd == "db"
    assert ns.db_cmd == "migrate"


def test_cli_parses_models_activate():
    ns = build_parser().parse_args(["models", "activate", "--pair", "EURUSD"])
    assert ns.cmd == "models"
    assert ns.models_cmd == "activate"
    assert ns.source == "compat"


def test_cli_parses_stack_preflight():
    ns = build_parser().parse_args(["stack", "preflight"])
    assert ns.cmd == "stack"
    assert ns.stack_cmd == "preflight"


def test_cli_parses_stack_gpu_check():
    ns = build_parser().parse_args(["stack", "gpu-check"])
    assert ns.cmd == "stack"
    assert ns.stack_cmd == "gpu-check"


def test_cli_parses_audit_dukascopy_gate():
    ns = build_parser().parse_args(["audit", "dukascopy-gate", "--", "--pairs", "EURUSD,USDJPY"])
    assert ns.cmd == "audit"
    assert ns.audit_cmd == "dukascopy-gate"
    assert "--pairs" in ns.tool_args


def test_cli_parses_audit_live_stack_check():
    ns = build_parser().parse_args(["audit", "live-stack-check", "--", "--require-acked-command"])
    assert ns.cmd == "audit"
    assert ns.audit_cmd == "live-stack-check"
    assert "--require-acked-command" in ns.tool_args
