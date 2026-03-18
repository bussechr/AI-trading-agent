from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _fxstack_src() -> Path:
    return _repo_root() / "fx-quant-stack" / "src"


def _ensure_fxstack_path() -> bool:
    src = _fxstack_src()
    if not src.exists():
        return False
    src_str = str(src)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)
    return True


def _run_python_main(module_name: str, func_name: str = "main", argv: list[str] | None = None) -> int:
    mod = importlib.import_module(module_name)
    fn: Callable[[], None] = getattr(mod, func_name)
    prev = list(sys.argv)
    sys.argv = [module_name] + list(argv or [])
    try:
        fn()
    finally:
        sys.argv = prev
    return 0


def _runtime_run(args: argparse.Namespace) -> int:
    runtime_impl = str(os.environ.get("TRADER_RUNTIME_IMPL", "fxstack")).strip().lower()
    argv = ["--equity", str(args.equity), "--sleep", str(args.sleep)]
    cfg = str(args.config).strip()
    if cfg:
        argv = ["--config", cfg] + argv

    if runtime_impl == "legacy":
        raise SystemExit("Legacy runtime implementation is no longer supported. Set TRADER_RUNTIME_IMPL=fxstack.")

    if runtime_impl != "fxstack":
        raise SystemExit(f"Unsupported TRADER_RUNTIME_IMPL='{runtime_impl}'. Expected 'fxstack'.")

    if not _ensure_fxstack_path():
        raise SystemExit("fx-quant-stack/src not found; cannot start fxstack runtime.")

    try:
        return _run_python_main("fxstack.runtime.runner", argv=argv)
    except Exception as exc:
        raise SystemExit(f"fxstack runtime startup failed: {exc}")


def _bridge_serve(args: argparse.Namespace) -> int:
    bridge_impl = str(os.environ.get("TRADER_BRIDGE_IMPL", "fxstack")).strip().lower()
    host = str(args.host)
    port = int(args.port)
    if bridge_impl == "legacy":
        raise SystemExit("Legacy bridge implementation is no longer supported. Set TRADER_BRIDGE_IMPL=fxstack.")

    if bridge_impl != "fxstack":
        raise SystemExit(f"Unsupported TRADER_BRIDGE_IMPL='{bridge_impl}'. Expected 'fxstack'.")

    if not _ensure_fxstack_path():
        raise SystemExit("fx-quant-stack/src not found; cannot start fxstack bridge.")

    try:
        import uvicorn
        from fxstack.api.app import app as fxstack_app
    except Exception as exc:
        raise SystemExit(f"fxstack bridge import failed: {exc}")

    uvicorn.run(fxstack_app, host=host, port=port, log_level="info")
    return 0


def _monitor_confidence(args: argparse.Namespace) -> int:
    import requests

    base = str(args.bridge_url).rstrip("/")
    poll = float(max(0.2, args.poll_seconds))
    print(f"Monitoring: {base} every {poll:.1f}s (Ctrl+C to stop)")
    while True:
        t0 = time.time()
        try:
            mon = requests.get(f"{base}/v2/monitor", timeout=2).json()
            met = requests.get(f"{base}/v2/metrics", timeout=2).json()
            entry = dict((mon.get("monitor", {}) or {}).get("entry", {}) or {})
            close = dict((mon.get("monitor", {}) or {}).get("close", {}) or {})
            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"status={mon.get('bridge', {}).get('system_status', 'unknown')} "
                f"eq={float(mon.get('account', {}).get('equity', 0.0)):.2f} "
                f"pending={int((met.get('pending', {}) or {}).get('count', 0))} "
                f"entry={entry.get('symbol', 'N/A')}:{entry.get('side', 'N/A')} "
                f"close_reason={close.get('dominant_close_reason', 'none')}"
            )
        except Exception as exc:
            print(f"[{time.strftime('%H:%M:%S')}] monitor error: {exc}")
        dt = time.time() - t0
        time.sleep(max(0.0, poll - dt))


def _tool_passthrough(module_name: str, args: argparse.Namespace) -> int:
    tool_args = list(args.tool_args or [])
    # Accept shell-style delimiter from docs: `trader ... -- <tool args>`.
    if tool_args and tool_args[0] == "--":
        tool_args = tool_args[1:]
    return _run_python_main(module_name, argv=tool_args)


def _fxstack_guard() -> None:
    if not _ensure_fxstack_path():
        raise SystemExit("fx-quant-stack/src not found; create the nested v2 project first.")


def _data_ingest(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import ingest_task

    out = ingest_task(
        pair=str(args.pair).upper(),
        granularity=str(args.granularity).upper(),
        store_root=str(args.store_root),
        csv_path=str(args.csv_path),
        source_root=str(args.source_root),
        file_pattern=str(args.file_pattern),
    )
    print(out)
    return 0


def _data_migrate_provider(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.data.provider_migration import migrate_provider_partitions

    dry_run = not bool(args.apply)
    if bool(args.dry_run):
        dry_run = True
    out = migrate_provider_partitions(
        store_root=Path(str(args.store_root)),
        source_provider=str(args.source_provider).strip().lower(),
        target_provider=str(args.target_provider).strip().lower(),
        dry_run=dry_run,
        remove_source=bool(args.remove_source),
    )
    print(out)
    return 0


def _data_fetch_dukascopy_matrix(args: argparse.Namespace) -> int:
    return _tool_passthrough("tools.fetch_dukascopy_matrix", args)


def _features_build(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import build_features_task

    out = build_features_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        input_root=str(args.input_root),
        output_root=str(args.output_root),
    )
    print(out)
    return 0


def _labels_build(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import build_labels_task

    out = build_labels_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        horizon_bars=int(args.horizon_bars),
        tp_mult=float(args.tp_atr_mult),
        sl_mult=float(args.sl_atr_mult),
    )
    print(out)
    return 0


def _train_regime(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import train_regime_task

    out = train_regime_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        out=str(args.out),
    )
    print(out)
    return 0


def _train_swing(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import train_swing_task

    out = train_swing_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        out=str(args.out),
    )
    print(out)
    return 0


def _train_intraday(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import train_intraday_task

    out = train_intraday_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        out=str(args.out),
    )
    print(out)
    return 0


def _train_swing_transformer(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import train_swing_transformer_task

    out = train_swing_transformer_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        out=str(args.out),
    )
    print(out)
    return 0


def _train_intraday_tcn(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import train_intraday_tcn_task

    out = train_intraday_tcn_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        out=str(args.out),
    )
    print(out)
    return 0


def _train_deep_stale(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.settings import get_settings
    from fxstack.tasks import train_deep_stale_task

    s = get_settings()
    pairs = [str(p).upper() for p in (args.pair or [])] or list(s.pairs)
    out = []
    for pair in pairs:
        out.append(
            train_deep_stale_task(
                pair=pair,
                swing_timeframe=str(args.swing_timeframe).upper(),
                intraday_timeframe=str(args.intraday_timeframe).upper(),
                feature_root=str(args.feature_root),
                label_root=str(args.label_root),
                artifact_root=str(args.artifact_root),
                stale_hours=float(args.stale_hours),
            )
        )
    print({"pairs": pairs, "stale_hours": float(args.stale_hours), "results": out})
    return 0


def _train_meta(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import train_meta_task

    out = train_meta_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        out=str(args.out),
    )
    print(out)
    return 0


def _train_all(args: argparse.Namespace) -> int:
    _fxstack_guard()
    script = _repo_root() / "fx-quant-stack" / "scripts" / "train_all.py"
    cmd = [
        sys.executable,
        str(script),
        "--pair",
        str(args.pair).upper(),
        "--swing-timeframe",
        str(args.swing_timeframe).upper(),
        "--intraday-timeframe",
        str(args.intraday_timeframe).upper(),
        "--regime-timeframe",
        str(args.regime_timeframe).upper(),
        "--feature-root",
        str(args.feature_root),
        "--label-root",
        str(args.label_root),
        "--artifact-root",
        str(args.artifact_root),
        "--training-config",
        str(args.training_config),
        "--registry-root",
        str(args.registry_root),
        "--deep-stale-hours",
        str(args.deep_stale_hours),
    ]
    env = dict(os.environ)
    src_path = str(_fxstack_src())
    env["PYTHONPATH"] = f"{src_path}{os.pathsep}{env.get('PYTHONPATH', '')}" if env.get("PYTHONPATH") else src_path
    return int(subprocess.call(cmd, cwd=str(_repo_root()), env=env))


def _backtest_run(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.backtest.engine import evaluate_signals
    from fxstack.backtest.reports import summarize_backtest
    from fxstack.io.parquet_store import ParquetStore
    from fxstack.settings import get_settings

    provider = get_settings().normalized_data_provider
    feats = ParquetStore(Path(str(args.feature_root))).read_pair_timeframe(
        provider=provider,
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
    )
    if feats.empty:
        print({"error": "no feature rows"})
        return 1

    signals = feats[["pair", "ts"]].copy()
    signals["expected_edge_bps"] = feats["ret_1"].astype(float) * 10000.0
    signals["spread_bps"] = feats.get("spread", 0.0).astype(float) * 10000.0
    signals["allowed"] = True
    scored = evaluate_signals(signals)
    print(summarize_backtest(scored))
    return 0


def _backtest_full(args: argparse.Namespace) -> int:
    return _tool_passthrough("tools.fxstack_full_backtest", args)


def _live_score(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.data.live_quotes import fetch_bridge_ticks
    from fxstack.io.parquet_store import ParquetStore
    from fxstack.live.scorer import LiveScorer
    from fxstack.models.intraday_xgb import IntradayXGB
    from fxstack.models.meta_filter import MetaFilterXGB
    from fxstack.models.regime_hmm import RegimeHMM
    from fxstack.models.swing_xgb import SwingXGB
    from fxstack.settings import get_settings

    provider = get_settings().normalized_data_provider
    pair = str(args.pair).upper()
    timeframe = str(args.timeframe).upper()
    feats = ParquetStore(Path(str(args.feature_root))).read_pair_timeframe(provider=provider, pair=pair, timeframe=timeframe)
    if feats.empty:
        print({"error": "no feature rows"})
        return 1
    row = feats.tail(1).copy()
    ticks = fetch_bridge_ticks(get_settings().mt4_bridge_url)
    tick = dict(ticks.get(pair, {}))
    spread_bps = float(tick.get("spread", 0.0) or 0.0)

    regime = RegimeHMM.load(Path(str(args.regime_model)))
    swing = SwingXGB.load(Path(str(args.swing_model)))
    intraday = IntradayXGB.load(Path(str(args.intraday_model)))
    meta = MetaFilterXGB.load(Path(str(args.meta_model)))
    scorer = LiveScorer(regime_model=regime, swing_model=swing, intraday_model=intraday, meta_model=meta)
    signal = scorer.score(row, spread_bps=spread_bps, expected_edge_bps=float(row["ret_1"].iloc[0] * 10000.0))
    print(signal.to_dict())
    return 0


def _db_migrate(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.runtime.db_tools import migrate_database
    from fxstack.settings import get_settings

    s = get_settings()
    database_url = str(args.database_url or s.database_url)
    allow_sqlite = bool(args.allow_sqlite or s.allow_sqlite)
    if database_url.lower().startswith("sqlite") and not allow_sqlite:
        print({"ok": False, "error": "sqlite_blocked", "database_url": database_url})
        return 2

    try:
        out = migrate_database(database_url=database_url, root=_repo_root() / "fx-quant-stack")
    except Exception as exc:
        print({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    print(
        {
            "ok": bool(out.get("ok")),
            "return_code": int(out.get("return_code", 1)),
            "stderr": str(out.get("stderr", "")).strip()[-5000:],
            "stdout_tail": str(out.get("stdout", "")).strip()[-5000:],
        }
    )
    return 0 if bool(out.get("ok")) else int(out.get("return_code", 1) or 1)


def _db_verify(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.runtime.db_tools import verify_database
    from fxstack.settings import get_settings

    s = get_settings()
    database_url = str(args.database_url or s.database_url)
    allow_sqlite = bool(args.allow_sqlite or s.allow_sqlite)
    if database_url.lower().startswith("sqlite") and not allow_sqlite:
        print({"ok": False, "error": "sqlite_blocked", "database_url": database_url})
        return 2
    try:
        out = verify_database(database_url=database_url)
    except Exception as exc:
        print({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    print(out)
    return 0 if bool(out.get("ok")) else 1


def _models_activate(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.settings import get_settings
    from fxstack.training.activation import activate_pairs, activate_registry_file

    s = get_settings()
    database_url = str(args.database_url or s.database_url)
    registry_root = Path(str(args.registry_root or s.registry_root))
    manifest_path = Path(str(args.manifest or s.model_activation_manifest))
    pairs = [str(p).upper() for p in (args.pair or [])] or list(s.pairs)

    activated: list[dict] = []
    if args.registry_file:
        entry = activate_registry_file(
            database_url=database_url,
            registry_file=Path(str(args.registry_file)),
            manifest_path=manifest_path,
            default_session_id=s.default_session_id,
            command_ttl_secs=s.command_ttl_secs,
            enabled=True,
        )
        activated.append(entry)
    else:
        activated = activate_pairs(
            database_url=database_url,
            registry_root=registry_root,
            manifest_path=manifest_path,
            pairs=pairs,
            default_session_id=s.default_session_id,
            command_ttl_secs=s.command_ttl_secs,
        )

    activated_pairs = {str(x.get("pair", "")).upper() for x in activated}
    missing = [p for p in pairs if p not in activated_pairs]
    out = {
        "database_url": database_url,
        "registry_root": str(registry_root),
        "manifest": str(manifest_path),
        "activated_count": len(activated),
        "activated_pairs": sorted(list(activated_pairs)),
        "missing_pairs": missing,
    }
    print(out)
    if bool(args.require_all) and missing:
        return 1
    return 0


def _stack_preflight(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.settings import get_settings

    s = get_settings()
    allow_sqlite = bool(args.allow_sqlite or s.allow_sqlite)
    checks: list[dict[str, object]] = []

    def _push(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    _push("python_executable", bool(sys.executable), sys.executable)
    uv_path = shutil.which("uv")
    _push(
        "uv_available_optional",
        True,
        str(uv_path) if uv_path else "missing; using pip/venv fallback",
    )
    _push("node_available", shutil.which("node") is not None, str(shutil.which("node") or ""))
    _push("pnpm_available", shutil.which("pnpm") is not None, str(shutil.which("pnpm") or ""))
    provider = str(s.normalized_data_provider)
    _push("data_provider_supported", provider in {"dukascopy"}, provider)
    source_root = Path(str(s.dukascopy_source_root).strip()).expanduser()
    _push("dukascopy_source_root_exists", source_root.exists(), str(source_root))
    _push("dukascopy_file_pattern_set", bool(str(s.dukascopy_file_pattern).strip()), str(s.dukascopy_file_pattern))
    _push("database_url_set", bool(str(s.database_url).strip()), str(s.database_url))
    if str(s.database_url).lower().startswith("sqlite") and not allow_sqlite:
        _push("sqlite_block", False, "FXSTACK_DATABASE_URL points to sqlite and allow_sqlite is false")
    else:
        _push("sqlite_block", True, "")

    required_modules = [
        "fastapi",
        "uvicorn",
        "sqlalchemy",
        "pydantic",
        "pydantic_settings",
        "requests",
        "xgboost",
        "hmmlearn",
        "optuna",
        "torch",
        "transformers",
        "pytorch_tcn",
        "dukascopy_python",
    ]
    for mod in required_modules:
        _push(f"module:{mod}", importlib.util.find_spec(mod) is not None, "")

    cuda_ok = True
    cuda_detail = "not-required"
    if bool(s.require_cuda):
        try:
            import torch

            cuda_ok = bool(torch.cuda.is_available())
            cuda_detail = f"required={int(bool(s.require_cuda))},available={int(cuda_ok)}"
        except Exception as exc:
            cuda_ok = False
            cuda_detail = f"torch_import_error:{type(exc).__name__}: {exc}"
    _push("cuda_available", cuda_ok, cuda_detail)

    ok = all(bool(x.get("ok")) for x in checks)
    print({"ok": ok, "checks": checks, "settings": s.to_public_dict()})
    return 0 if ok else 2


def _stack_gpu_check(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.settings import get_settings

    s = get_settings()
    try:
        import torch
    except Exception as exc:
        print({"ok": False, "error": f"torch_import_error:{type(exc).__name__}: {exc}"})
        return 2

    available = bool(torch.cuda.is_available())
    out = {
        "ok": available or not bool(s.require_cuda),
        "require_cuda": bool(s.require_cuda),
        "cuda_available": available,
        "cuda_device_count": int(torch.cuda.device_count() if available else 0),
        "cuda_devices": [str(torch.cuda.get_device_name(i)) for i in range(torch.cuda.device_count())] if available else [],
    }
    print(out)
    return 0 if bool(out["ok"]) else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="trader", description="Unified trading system CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    runtime = sub.add_parser("runtime", help="Runtime controls")
    runtime_sub = runtime.add_subparsers(dest="runtime_cmd", required=True)
    rr = runtime_sub.add_parser("run", help="Run live runtime loop")
    rr.add_argument("--config", default="")
    rr.add_argument("--equity", type=float, required=True)
    rr.add_argument("--sleep", type=int, default=5)
    rr.set_defaults(_fn=_runtime_run)

    bridge = sub.add_parser("bridge", help="Bridge API controls")
    bridge_sub = bridge.add_subparsers(dest="bridge_cmd", required=True)
    bs = bridge_sub.add_parser("serve", help="Run bridge API")
    bs.add_argument("--host", default=os.environ.get("TRADER_BRIDGE_HOST", "127.0.0.1"))
    bs.add_argument("--port", type=int, default=int(os.environ.get("TRADER_BRIDGE_PORT", "58710")))
    bs.set_defaults(_fn=_bridge_serve)

    monitor = sub.add_parser("monitor", help="Monitoring commands")
    monitor_sub = monitor.add_subparsers(dest="monitor_cmd", required=True)
    mc = monitor_sub.add_parser("confidence", help="Poll confidence/v2 monitor endpoints")
    mc.add_argument("--bridge-url", default=os.environ.get("MT4_BRIDGE_URL", "http://127.0.0.1:58710"))
    mc.add_argument("--poll-seconds", type=float, default=2.0)
    mc.set_defaults(_fn=_monitor_confidence)

    backtest = sub.add_parser("backtest", help="Backtesting commands")
    backtest_sub = backtest.add_subparsers(dest="backtest_cmd", required=True)
    br = backtest_sub.add_parser("run", help="Run fxstack baseline backtest")
    br.add_argument("--pair", required=True)
    br.add_argument("--timeframe", default="M5")
    br.add_argument("--feature-root", default="fx-quant-stack/data/features")
    br.set_defaults(_fn=_backtest_run)
    bf = backtest_sub.add_parser("full", help="Run full multi-pair model-driven offline backtest and emit artifacts")
    bf.add_argument("tool_args", nargs=argparse.REMAINDER)
    bf.set_defaults(_fn=_backtest_full)

    audit = sub.add_parser("audit", help="Audit commands")
    audit_sub = audit.add_subparsers(dest="audit_cmd", required=True)
    ai = audit_sub.add_parser("interop", help="Run interop efficiency audit")
    ai.add_argument("tool_args", nargs=argparse.REMAINDER)
    ai.set_defaults(_fn=lambda a: _tool_passthrough("tools.mt4_interop_efficiency_audit", a))
    bf = audit_sub.add_parser("baseline-freeze", help="Generate baseline KPI + contract freeze artifacts")
    bf.add_argument("tool_args", nargs=argparse.REMAINDER)
    bf.set_defaults(_fn=lambda a: _tool_passthrough("tools.baseline_freeze", a))
    fp = audit_sub.add_parser("full-process", help="Bootstrap full-process audit evidence and static checks")
    fp.add_argument("tool_args", nargs=argparse.REMAINDER)
    fp.set_defaults(_fn=lambda a: _tool_passthrough("tools.full_process_audit", a))
    fbld = audit_sub.add_parser("finalize-build", help="Finalize audit artifacts and emit GO/HOLD decision")
    fbld.add_argument("tool_args", nargs=argparse.REMAINDER)
    fbld.set_defaults(_fn=lambda a: _tool_passthrough("tools.finalize_build", a))
    dg = audit_sub.add_parser("dukascopy-gate", help="Validate Dukascopy CSV coverage and row thresholds")
    dg.add_argument("tool_args", nargs=argparse.REMAINDER)
    dg.set_defaults(_fn=lambda a: _tool_passthrough("tools.dukascopy_coverage_gate", a))
    lsc = audit_sub.add_parser("live-stack-check", help="Verify live v2 bridge/runtime heartbeat + command ACK lifecycle")
    lsc.add_argument("tool_args", nargs=argparse.REMAINDER)
    lsc.set_defaults(_fn=lambda a: _tool_passthrough("tools.live_stack_check", a))

    scen = sub.add_parser("scenario", help="Scenario commands")
    scen_sub = scen.add_subparsers(dest="scenario_cmd", required=True)
    dr = scen_sub.add_parser("dual-run-compare", help="Compare dual-run trace artifacts")
    dr.add_argument("tool_args", nargs=argparse.REMAINDER)
    dr.set_defaults(_fn=lambda a: _tool_passthrough("tools.dual_run_compare", a))
    sr = scen_sub.add_parser("shadow-run", help="Run live baseline/candidate shadow dual-run with canary gates")
    sr.add_argument("tool_args", nargs=argparse.REMAINDER)
    sr.set_defaults(_fn=lambda a: _tool_passthrough("tools.shadow_dual_run", a))

    data = sub.add_parser("data", help="Data ingestion commands")
    data_sub = data.add_subparsers(dest="data_cmd", required=True)
    di = data_sub.add_parser("ingest", help="Ingest Dukascopy CSV bars into parquet")
    di.add_argument("--pair", required=True)
    di.add_argument("--granularity", default="M5")
    di.add_argument("--csv-path", default="")
    di.add_argument("--source-root", default="")
    di.add_argument("--file-pattern", default="")
    di.add_argument("--store-root", default="fx-quant-stack/data/raw")
    di.set_defaults(_fn=_data_ingest)
    dm = data_sub.add_parser("migrate-provider", help="Migrate parquet partitions between providers")
    dm.add_argument("--store-root", default="fx-quant-stack/data/raw")
    dm.add_argument("--source-provider", default="oanda")
    dm.add_argument("--target-provider", default="dukascopy")
    mode = dm.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    dm.add_argument("--remove-source", action="store_true")
    dm.set_defaults(_fn=_data_migrate_provider)
    dfm = data_sub.add_parser("fetch-dukascopy-matrix", help="Download Dukascopy M1 bid/ask and build M5/M15/H4/D matrix CSVs")
    dfm.add_argument("tool_args", nargs=argparse.REMAINDER)
    dfm.set_defaults(_fn=_data_fetch_dukascopy_matrix)

    features = sub.add_parser("features", help="Feature engineering commands")
    features_sub = features.add_subparsers(dest="features_cmd", required=True)
    fb = features_sub.add_parser("build", help="Build PIT feature set")
    fb.add_argument("--pair", required=True)
    fb.add_argument("--timeframe", default="M5")
    fb.add_argument("--input-root", default="fx-quant-stack/data/raw")
    fb.add_argument("--output-root", default="fx-quant-stack/data/features")
    fb.set_defaults(_fn=_features_build)

    labels = sub.add_parser("labels", help="Label generation commands")
    labels_sub = labels.add_subparsers(dest="labels_cmd", required=True)
    lb = labels_sub.add_parser("build", help="Build triple-barrier labels")
    lb.add_argument("--pair", required=True)
    lb.add_argument("--timeframe", default="M5")
    lb.add_argument("--feature-root", default="fx-quant-stack/data/features")
    lb.add_argument("--label-root", default="fx-quant-stack/data/labels")
    lb.add_argument("--horizon-bars", type=int, default=24)
    lb.add_argument("--tp-atr-mult", type=float, default=2.0)
    lb.add_argument("--sl-atr-mult", type=float, default=1.5)
    lb.set_defaults(_fn=_labels_build)

    train = sub.add_parser("train", help="Model training commands")
    train_sub = train.add_subparsers(dest="train_cmd", required=True)

    tr = train_sub.add_parser("regime", help="Train HMM regime model")
    tr.add_argument("--pair", required=True)
    tr.add_argument("--timeframe", default="H4")
    tr.add_argument("--feature-root", default="fx-quant-stack/data/features")
    tr.add_argument("--out", default="fx-quant-stack/artifacts/regime_hmm")
    tr.set_defaults(_fn=_train_regime)

    ts = train_sub.add_parser("swing", help="Train swing XGBoost model")
    ts.add_argument("--pair", required=True)
    ts.add_argument("--timeframe", default="D")
    ts.add_argument("--feature-root", default="fx-quant-stack/data/features")
    ts.add_argument("--label-root", default="fx-quant-stack/data/labels")
    ts.add_argument("--out", default="fx-quant-stack/artifacts/swing_xgb")
    ts.set_defaults(_fn=_train_swing)

    ti = train_sub.add_parser("intraday", help="Train intraday XGBoost model")
    ti.add_argument("--pair", required=True)
    ti.add_argument("--timeframe", default="M5")
    ti.add_argument("--feature-root", default="fx-quant-stack/data/features")
    ti.add_argument("--label-root", default="fx-quant-stack/data/labels")
    ti.add_argument("--out", default="fx-quant-stack/artifacts/intraday_xgb")
    ti.set_defaults(_fn=_train_intraday)

    tst = train_sub.add_parser("swing-transformer", help="Train swing transformer model")
    tst.add_argument("--pair", required=True)
    tst.add_argument("--timeframe", default="D")
    tst.add_argument("--feature-root", default="fx-quant-stack/data/features")
    tst.add_argument("--label-root", default="fx-quant-stack/data/labels")
    tst.add_argument("--out", default="fx-quant-stack/artifacts/swing_transformer")
    tst.set_defaults(_fn=_train_swing_transformer)

    tit = train_sub.add_parser("intraday-tcn", help="Train intraday TCN model")
    tit.add_argument("--pair", required=True)
    tit.add_argument("--timeframe", default="M5")
    tit.add_argument("--feature-root", default="fx-quant-stack/data/features")
    tit.add_argument("--label-root", default="fx-quant-stack/data/labels")
    tit.add_argument("--out", default="fx-quant-stack/artifacts/intraday_tcn")
    tit.set_defaults(_fn=_train_intraday_tcn)

    tds = train_sub.add_parser("deep-stale", help="Retrain deep models only when stale")
    tds.add_argument("--pair", action="append", default=[])
    tds.add_argument("--swing-timeframe", default="D")
    tds.add_argument("--intraday-timeframe", default="M5")
    tds.add_argument("--feature-root", default="fx-quant-stack/data/features")
    tds.add_argument("--label-root", default="fx-quant-stack/data/labels")
    tds.add_argument("--artifact-root", default="fx-quant-stack/artifacts")
    tds.add_argument("--stale-hours", type=float, default=24.0)
    tds.set_defaults(_fn=_train_deep_stale)

    tm = train_sub.add_parser("meta", help="Train meta-label XGBoost model")
    tm.add_argument("--pair", required=True)
    tm.add_argument("--timeframe", default="M5")
    tm.add_argument("--feature-root", default="fx-quant-stack/data/features")
    tm.add_argument("--out", default="fx-quant-stack/artifacts/meta_filter")
    tm.set_defaults(_fn=_train_meta)

    ta = train_sub.add_parser("all", help="Train full baseline stack and register artifacts")
    ta.add_argument("--pair", required=True)
    ta.add_argument("--swing-timeframe", default="D")
    ta.add_argument("--intraday-timeframe", default="M5")
    ta.add_argument("--regime-timeframe", default="H4")
    ta.add_argument("--feature-root", default="fx-quant-stack/data/features")
    ta.add_argument("--label-root", default="fx-quant-stack/data/labels")
    ta.add_argument("--artifact-root", default="fx-quant-stack/artifacts")
    ta.add_argument("--training-config", default="fx-quant-stack/configs/training.yaml")
    ta.add_argument("--registry-root", default="fx-quant-stack/artifacts/registry")
    ta.add_argument("--deep-stale-hours", type=float, default=24.0)
    ta.set_defaults(_fn=_train_all)

    live = sub.add_parser("live", help="Live scoring commands")
    live_sub = live.add_subparsers(dest="live_cmd", required=True)
    ls = live_sub.add_parser("score", help="Score latest live snapshot")
    ls.add_argument("--pair", required=True)
    ls.add_argument("--timeframe", default="M5")
    ls.add_argument("--feature-root", default="fx-quant-stack/data/features")
    ls.add_argument("--regime-model", default="fx-quant-stack/artifacts/regime_hmm")
    ls.add_argument("--swing-model", default="fx-quant-stack/artifacts/swing_xgb")
    ls.add_argument("--intraday-model", default="fx-quant-stack/artifacts/intraday_xgb")
    ls.add_argument("--meta-model", default="fx-quant-stack/artifacts/meta_filter")
    ls.set_defaults(_fn=_live_score)

    db = sub.add_parser("db", help="Database lifecycle commands")
    db_sub = db.add_subparsers(dest="db_cmd", required=True)
    dm = db_sub.add_parser("migrate", help="Run Alembic migrations for fxstack")
    dm.add_argument("--database-url", default="")
    dm.add_argument("--allow-sqlite", action="store_true")
    dm.set_defaults(_fn=_db_migrate)

    dv = db_sub.add_parser("verify", help="Verify required runtime/model tables exist")
    dv.add_argument("--database-url", default="")
    dv.add_argument("--allow-sqlite", action="store_true")
    dv.set_defaults(_fn=_db_verify)

    models = sub.add_parser("models", help="Model registry activation commands")
    models_sub = models.add_subparsers(dest="models_cmd", required=True)
    ma = models_sub.add_parser("activate", help="Activate registry model sets into runtime store")
    ma.add_argument("--database-url", default="")
    ma.add_argument("--registry-root", default="")
    ma.add_argument("--manifest", default="")
    ma.add_argument("--registry-file", default="")
    ma.add_argument("--pair", action="append", default=[])
    ma.add_argument("--require-all", action="store_true")
    ma.set_defaults(_fn=_models_activate)

    stack = sub.add_parser("stack", help="Stack orchestration helpers")
    stack_sub = stack.add_subparsers(dest="stack_cmd", required=True)
    sp = stack_sub.add_parser("preflight", help="Validate environment, dependencies, and key settings")
    sp.add_argument("--allow-sqlite", action="store_true")
    sp.set_defaults(_fn=_stack_preflight)
    sg = stack_sub.add_parser("gpu-check", help="Validate CUDA availability for deep-model runtime")
    sg.set_defaults(_fn=_stack_gpu_check)

    return ap


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    fn = getattr(args, "_fn", None)
    if fn is None:
        parser.print_help()
        raise SystemExit(2)
    code = int(fn(args) or 0)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
