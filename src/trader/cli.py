from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

_FXSTACK_REEXEC_ENV = "TRADER_FXSTACK_REEXEC"


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


def _fxstack_python_candidates() -> list[Path]:
    repo_root = _repo_root()
    candidates: list[Path] = []
    for raw in [
        os.environ.get("TRADER_FXSTACK_PYTHON", ""),
        os.environ.get("FXSTACK_PYTHON", ""),
        str(repo_root / ".venv" / "bin" / "python"),
        str(repo_root / ".venv" / "Scripts" / "python.exe"),
        str(repo_root / ".venv-linux" / "bin" / "python"),
        str(repo_root / "fx-quant-stack" / ".venv" / "bin" / "python"),
        str(repo_root / "fx-quant-stack" / ".venv" / "Scripts" / "python.exe"),
        sys.executable,
    ]:
        txt = str(raw or "").strip()
        if not txt:
            continue
        path = Path(txt).expanduser()
        try:
            exists = path.exists()
        except OSError:
            # WSL-leftover symlinks raise WinError 1920 on Windows. Treat as
            # non-existent so candidate resolution continues to the next path.
            exists = False
        if exists:
            if not path.is_absolute():
                path = (repo_root / path).absolute()
            else:
                path = path.absolute()
            candidates.append(path)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _probe_fxstack_python(python_executable: Path) -> tuple[bool, str]:
    probe = [
        str(python_executable),
        "-c",
        (
            "import sys; "
            f"sys.path.insert(0, {str(_fxstack_src())!r}); "
            "import fxstack.settings"
        ),
    ]
    try:
        proc = subprocess.run(
            probe,
            cwd=str(_repo_root()),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if int(proc.returncode) == 0:
        return True, ""
    detail = str(proc.stderr or proc.stdout or "").strip()
    return False, detail or f"exit_code={proc.returncode}"


def _ensure_fxstack_runtime() -> None:
    if not _ensure_fxstack_path():
        raise SystemExit("fx-quant-stack/src not found; create the nested v2 project first.")
    current_ok, _ = _probe_fxstack_python(Path(sys.executable))
    if current_ok:
        return
    if str(os.environ.get(_FXSTACK_REEXEC_ENV, "")).strip() == "1":
        raise SystemExit(
            "fxstack dependencies are unavailable in the selected Python interpreter. "
            "Set `TRADER_FXSTACK_PYTHON` to a repo environment with fxstack installed."
        )
    for candidate in _fxstack_python_candidates():
        ok, _ = _probe_fxstack_python(candidate)
        if not ok:
            continue
        env = dict(os.environ)
        env[_FXSTACK_REEXEC_ENV] = "1"
        os.execve(
            str(candidate),
            [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]],
            env,
        )
    raise SystemExit(
        "No fxstack-ready Python interpreter was found. "
        "Tried repo virtual environments and the current interpreter."
    )


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
    _ensure_fxstack_runtime()
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
    _ensure_fxstack_runtime()
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
    api_key = os.environ.get("FXSTACK_BRIDGE_API_KEY", "")
    headers = {"X-API-Key": api_key} if api_key else None
    print(f"Monitoring: {base} every {poll:.1f}s (Ctrl+C to stop)")
    while True:
        t0 = time.time()
        try:
            mon = requests.get(f"{base}/v2/monitor", headers=headers, timeout=2).json()
            met = requests.get(f"{base}/v2/metrics", headers=headers, timeout=2).json()
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
    _ensure_fxstack_runtime()
    tool_args = list(args.tool_args or [])
    # Accept shell-style delimiter from docs: `trader ... -- <tool args>`.
    if tool_args and tool_args[0] == "--":
        tool_args = tool_args[1:]
    return _run_python_main(module_name, argv=tool_args)


def _module_passthrough(module_name: str, args: argparse.Namespace) -> int:
    _ensure_fxstack_runtime()
    module_args = list(args.module_args or [])
    if module_args and module_args[0] == "--":
        module_args = module_args[1:]
    return _run_python_main(module_name, argv=module_args)


def _fxstack_guard() -> None:
    _ensure_fxstack_runtime()


def _backtest_internal_pnl(args: argparse.Namespace) -> int:
    return _tool_passthrough("tools.fxstack_lifecycle_equity_backtest", args)


def _backtest_nautilus(args: argparse.Namespace) -> int:
    return _module_passthrough("fxstack.backtest.harness.nautilus", args)


def _backtest_lean(args: argparse.Namespace) -> int:
    return _module_passthrough("fxstack.backtest.harness.lean", args)


def _backtest_stress(args: argparse.Namespace) -> int:
    return _module_passthrough("fxstack.backtest.harness.stress", args)


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


def _features_build_fx_lifecycle(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.settings import get_settings
    from fxstack.tasks import build_fx_lifecycle_features_task

    s = get_settings()
    out = build_fx_lifecycle_features_task(
        pair=str(args.pair).upper(),
        input_root=str(args.input_root),
        output_root=str(args.output_root),
        anchor_timeframe=str(args.anchor_timeframe).upper(),
        context_timeframes=[str(x).upper() for x in (args.context_timeframes or ["M15", "H1", "H4", "D"])],
        report_root=str(args.report_root or (s.project_root / "artifacts" / str(args.pair).lower() / "reports")),
    )
    print(out)
    return 0


def _features_compact_feast(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.feast.compaction import compact_feature_lake_to_feast
    from fxstack.settings import get_settings

    s = get_settings()
    pairs = [str(item).upper() for item in list(getattr(args, "pair", []) or [])]
    if not pairs:
        pairs = [str(item).upper() for item in list(s.pairs)]
    result = compact_feature_lake_to_feast(
        source_root=Path(str(args.feature_root)),
        output_root=Path(str(args.output_root or (s.feast_repo_root / "offline_store"))),
        provider=str(args.provider or s.normalized_data_provider),
        pairs=pairs,
    )
    out = {
        "ok": True,
        "provider": result.provider,
        "pairs": list(result.pairs),
        "output_root": str(result.output_root),
        "artifacts": [
            {
                "pair": item.pair,
                "view_name": item.view_name,
                "timeframe": item.timeframe,
                "rows": item.rows,
                "output_path": str(item.output_path),
            }
            for item in result.artifacts
        ],
    }
    print(out)
    return 0


def _features_push_worker(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.feast.push import drain_feature_push_outbox
    from fxstack.runtime.service import RuntimeService
    from fxstack.settings import get_settings

    s = get_settings()
    service = RuntimeService(database_url=str(args.database_url or s.database_url))
    out = drain_feature_push_outbox(
        service,
        worker_id=str(args.worker_id or s.feature_push_worker_id),
        limit=int(args.limit or s.feature_push_batch_size),
        repo_root=str(args.repo_root or s.feast_repo_root),
        dry_run=bool(args.dry_run),
        max_retries=int(args.max_retries or s.feature_push_max_retries),
    )
    print(out)
    return 0 if int(out.get("failed") or 0) == 0 else 1


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


def _labels_build_meta(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import build_meta_labels_task

    out = build_meta_labels_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        cost_stress_levels=tuple(float(x) for x in (args.cost_stress_levels or [1.0, 1.25, 1.5])),
        regime_model_path=str(args.regime_model),
        swing_model_path=str(args.swing_model),
        intraday_model_path=str(args.intraday_model),
        allow_heuristic_labels=bool(args.allow_heuristic_meta_labels),
    )
    print(out)
    return 0


def _labels_build_exit(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import build_exit_labels_task

    out = build_exit_labels_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        method=str(args.method),
        horizon_bars=int(args.horizon_bars),
    )
    print(out)
    return 0


def _labels_build_reversal(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import build_reversal_labels_task

    out = build_reversal_labels_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        horizon_bars=int(args.horizon_bars),
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


def _train_swing_patchtst(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import train_swing_patchtst_task

    out = train_swing_patchtst_task(
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


def _train_intraday_patchtst(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import train_intraday_patchtst_task

    out = train_intraday_patchtst_task(
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
        label_root=str(args.label_root),
        regime_model_path=str(args.regime_model),
        swing_model_path=str(args.swing_model),
        intraday_model_path=str(args.intraday_model),
        allow_heuristic_labels=bool(args.allow_heuristic_meta_labels),
    )
    print(out)
    return 0


def _train_exit(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import train_exit_task

    out = train_exit_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        out=str(args.out),
    )
    print(out)
    return 0


def _train_reversal(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from pathlib import Path

    from fxstack.tasks import train_reversal_task

    out_root = Path(str(args.out_root)) / str(args.pair).lower()
    out = train_reversal_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        out_failure=str(out_root / "reversal_failure_xgb"),
        out_opportunity=str(out_root / "reversal_opportunity_xgb"),
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
    if bool(getattr(args, "force_retrain", False)):
        cmd.append("--force-retrain")
    if bool(getattr(args, "lifecycle_only", False)):
        cmd.append("--lifecycle-only")
    if not bool(getattr(args, "with_belief", True)):
        cmd.append("--no-belief")
    if bool(getattr(args, "with_patchtst", False)):
        cmd.append("--with-patchtst")
    env = dict(os.environ)
    src_path = str(_fxstack_src())
    env["PYTHONPATH"] = f"{src_path}{os.pathsep}{env.get('PYTHONPATH', '')}" if env.get("PYTHONPATH") else src_path
    return int(subprocess.call(cmd, cwd=str(_repo_root()), env=env))


def _train_belief(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import train_belief_task

    pair_list = [str(x).upper() for x in (getattr(args, "pairs", []) or [])]
    if getattr(args, "pair", ""):
        pair_list = [str(args.pair).upper()]
    out = train_belief_task(
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        out=str(args.out),
        pairs=pair_list or None,
        dataset_out=str(getattr(args, "dataset_out", "") or "") or None,
        max_queries_per_pair=int(getattr(args, "max_queries_per_pair", 20000)),
    )
    print(out)
    return 0


def _train_belief_dataset(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.tasks import build_belief_dataset_task

    pair_list = [str(x).upper() for x in (getattr(args, "pairs", []) or [])]
    if getattr(args, "pair", ""):
        pair_list = [str(args.pair).upper()]
    out = build_belief_dataset_task(
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        out=str(args.out),
        pairs=pair_list or None,
        max_queries_per_pair=int(getattr(args, "max_queries_per_pair", 20000)),
    )
    print(out)
    return 0


def _backtest_run(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.backtest.engine import evaluate_signals
    from fxstack.backtest.reports import summarize_backtest
    from fxstack.io.parquet_store import ParquetStore
    from fxstack.live.policy import EDGE_FORMULA_ID, compute_expected_edge_bps, normalize_spread_bps
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
    signals["expected_edge_bps"] = feats.apply(lambda r: compute_expected_edge_bps(r), axis=1).astype(float)
    spread_norm = feats.apply(
        lambda r: normalize_spread_bps(row=r, pair=str(r.get("pair", "")).upper()),
        axis=1,
        result_type="expand",
    )
    signals["spread_bps"] = spread_norm[0].astype(float)
    signals["spread_unit_source"] = spread_norm[1].astype(str)
    signals["allowed"] = True
    scored = evaluate_signals(signals)
    summary = summarize_backtest(scored)
    summary["policy_version"] = str(get_settings().policy_version)
    summary["edge_formula_id"] = EDGE_FORMULA_ID
    summary["spread_conversion_method"] = "normalize_spread_bps"
    print(summary)
    return 0


def _backtest_full(args: argparse.Namespace) -> int:
    return _tool_passthrough("tools.fxstack_full_backtest", args)


def _live_score(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.data.live_quotes import fetch_bridge_ticks
    from fxstack.io.parquet_store import ParquetStore
    from fxstack.live.policy import normalize_spread_bps
    from fxstack.live.scorer import LiveScorer
    from fxstack.models.intraday_xgb import IntradayXGB
    from fxstack.models.meta_filter import MetaFilterXGB
    from fxstack.models.regime_hmm import RegimeHMM
    from fxstack.models.swing_xgb import SwingXGB
    from fxstack.settings import get_settings

    settings = get_settings()
    provider = settings.normalized_data_provider
    pair = str(args.pair).upper()
    intraday_timeframe = str(args.timeframe or settings.intraday_timeframe).upper()
    swing_timeframe = str(settings.swing_timeframe).upper()
    regime_timeframe = str(settings.regime_timeframe).upper()
    store = ParquetStore(Path(str(args.feature_root)))
    regime_row = store.read_pair_timeframe(provider=provider, pair=pair, timeframe=regime_timeframe).tail(1).copy()
    swing_row = store.read_pair_timeframe(provider=provider, pair=pair, timeframe=swing_timeframe).tail(1).copy()
    intraday_row = store.read_pair_timeframe(provider=provider, pair=pair, timeframe=intraday_timeframe).tail(1).copy()
    if regime_row.empty or swing_row.empty or intraday_row.empty:
        print(
            {
                "error": "missing_feature_rows",
                "pair": pair,
                "required_timeframes": {
                    "regime": regime_timeframe,
                    "swing": swing_timeframe,
                    "intraday": intraday_timeframe,
                },
            }
        )
        return 1
    ticks = fetch_bridge_ticks(settings.mt4_bridge_url)
    tick = dict(ticks.get(pair, {})) if isinstance(ticks, dict) else {}
    spread_bps, spread_unit_source = normalize_spread_bps(tick=tick, row=intraday_row.iloc[0], pair=pair)
    regime = RegimeHMM.load(Path(str(args.regime_model)))
    swing = SwingXGB.load(Path(str(args.swing_model)))
    intraday = IntradayXGB.load(Path(str(args.intraday_model)))
    meta = MetaFilterXGB.load(Path(str(args.meta_model)))
    scorer = LiveScorer(regime_model=regime, swing_model=swing, intraday_model=intraday, meta_model=meta)
    signal = scorer.score(
        regime_row=regime_row,
        swing_row=swing_row,
        intraday_row=intraday_row,
        meta_row=intraday_row,
        spread_bps=float(spread_bps),
        expected_edge_bps=None,
        spread_unit_source=str(spread_unit_source),
    )
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
    ok = bool(out.get("ok")) and int(out.get("return_code", 1)) == 0
    print(
        {
            "ok": ok,
            "return_code": int(out.get("return_code", 1)),
            "stderr": str(out.get("stderr", "")).strip()[-5000:],
            "stdout_tail": str(out.get("stdout", "")).strip()[-5000:],
        }
    )
    return 0 if ok else 1


def _db_ping(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.runtime.db_tools import ping_database
    from fxstack.settings import get_settings

    s = get_settings()
    database_url = str(args.database_url or s.database_url)
    allow_sqlite = bool(args.allow_sqlite or s.allow_sqlite)
    if database_url.lower().startswith("sqlite") and not allow_sqlite:
        print({"ok": False, "error": "sqlite_blocked", "database_url": database_url})
        return 2
    try:
        out = ping_database(database_url=database_url)
    except Exception as exc:
        print({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    print(out)
    return 0 if bool(out.get("ok")) else 1


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
    from fxstack.training.activation import activate_mlflow_alias, activate_pairs, activate_registry_file

    s = get_settings()
    database_url = str(args.database_url or s.database_url)
    registry_root = Path(str(args.registry_root or s.registry_root))
    manifest_path = Path(str(args.manifest or s.model_activation_manifest))
    pairs = [str(p).upper() for p in (args.pair or [])] or list(s.pairs)
    source = str(getattr(args, "source", "") or "compat").strip().lower()
    alias = str(getattr(args, "alias", "") or "champion").strip().lower()

    activated: list[dict] = []
    if source == "mlflow":
        activated = activate_mlflow_alias(
            database_url=database_url,
            manifest_path=manifest_path,
            pairs=pairs,
            alias=alias,
            default_session_id=s.default_session_id,
            command_ttl_secs=s.command_ttl_secs,
        )
    elif args.registry_file:
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
        "source": source,
        "alias": alias if source == "mlflow" else "",
        "activated_count": len(activated),
        "activated_pairs": sorted(list(activated_pairs)),
        "missing_pairs": missing,
    }
    print(out)
    if bool(args.require_all) and missing:
        return 1
    return 0


def _models_backfill_mlflow(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.settings import get_settings
    from fxstack.training.activation import backfill_mlflow_state

    s = get_settings()
    manifest_path = Path(str(args.manifest or s.model_activation_manifest))
    registry_root = Path(str(args.registry_root or s.registry_root))
    shadow_root = Path(str(args.shadow_root or "fx-quant-stack/artifacts_shadow"))
    out = backfill_mlflow_state(
        active_manifest_path=manifest_path,
        registry_root=registry_root,
        shadow_root=shadow_root,
    )
    print(out)
    return 0 if bool(out.get("ok")) else 1


def _models_set_alias(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow
    from fxstack.settings import get_settings
    from fxstack.training.activation import latest_registry_for_pair, parse_registry_entry

    s = get_settings()
    registry_root = Path(str(args.registry_root or s.registry_root))
    registry_file = Path(str(args.registry_file)) if str(args.registry_file or "").strip() else None
    pair = str(getattr(args, "pair", "") or "").upper().strip()
    if registry_file is None:
        if not pair:
            print({"ok": False, "error": "pair_or_registry_file_required"})
            return 2
        registry_file = latest_registry_for_pair(registry_root=registry_root, pair=pair)
    if registry_file is None or not registry_file.exists():
        print({"ok": False, "error": "registry_file_not_found", "registry_file": str(registry_file or "")})
        return 2
    payload = parse_registry_entry(registry_file)
    bundle = import_compat_bundle_to_mlflow(payload["metadata"], intended_alias=str(args.alias).strip().lower())
    print(
        {
            "ok": True,
            "alias": str(args.alias).strip().lower(),
            "pair": str(bundle.pair).upper(),
            "bundle_run_id": str(bundle.bundle_run_id),
            "registry_file": str(registry_file),
            "components": sorted(list(bundle.components.keys())),
        }
    )
    return 0


def _models_stage_release(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.training.release_workflow import stage_release

    allowlisted_pairs = [str(item).upper() for item in list(getattr(args, "allowlisted_pair", []) or [])]
    out = stage_release(
        pair=str(args.pair).upper(),
        alias=str(args.alias or "shadow").strip().lower(),
        title=str(args.title or ""),
        summary=str(args.summary or ""),
        author=str(args.author or ""),
        allowlisted_pairs=allowlisted_pairs or None,
        budget_scale=(None if args.budget_scale is None else float(args.budget_scale)),
        duration_minutes=(None if args.duration_minutes is None else int(args.duration_minutes)),
    )
    print(out)
    return 0 if bool(out.get("ok")) else 1


def _models_promote(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.training.release_workflow import promote_release

    out = promote_release(
        pair=str(args.pair).upper(),
        author=str(args.author or ""),
        bundle_run_id=str(args.bundle_run_id or ""),
    )
    print(out)
    return 0 if bool(out.get("ok")) else 1


def _models_shadow_accept(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.training.release_workflow import shadow_accept

    out = shadow_accept(
        pair=str(args.pair).upper(),
        bundle_run_id=str(args.bundle_run_id or ""),
    )
    print(out)
    return 0 if bool(out.get("ok")) else 1


def _models_canary_start(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.settings import get_settings
    from fxstack.training.release_workflow import canary_start

    s = get_settings()
    out = canary_start(
        pair=str(args.pair).upper(),
        database_url=str(args.database_url or s.database_url),
        manifest_path=Path(str(args.manifest or s.model_activation_manifest)),
        bundle_run_id=str(args.bundle_run_id or ""),
    )
    print(out)
    return 0 if bool(out.get("ok")) else 1


def _models_canary_monitor(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.settings import get_settings
    from fxstack.training.release_workflow import monitor_canary

    s = get_settings()
    out = monitor_canary(
        pair=str(args.pair).upper(),
        database_url=str(args.database_url or s.database_url),
        manifest_path=Path(str(args.manifest or s.model_activation_manifest)),
        bundle_run_id=str(args.bundle_run_id or ""),
    )
    print(out)
    return 0 if str(out.get("status") or "ok").strip().lower() == "ok" else 1


def _models_canary_close(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.settings import get_settings
    from fxstack.training.release_workflow import close_canary

    s = get_settings()
    out = close_canary(
        pair=str(args.pair).upper(),
        database_url=str(args.database_url or s.database_url),
        manifest_path=Path(str(args.manifest or s.model_activation_manifest)),
        outcome=str(args.outcome or "").strip().lower(),
        bundle_run_id=str(args.bundle_run_id or ""),
    )
    print(out)
    return 0 if bool(out.get("ok")) else 1


def _models_rollback(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.settings import get_settings
    from fxstack.training.release_workflow import rollback_release

    s = get_settings()
    out = rollback_release(
        pair=str(args.pair).upper(),
        database_url=str(args.database_url or s.database_url),
        manifest_path=Path(str(args.manifest or s.model_activation_manifest)),
        bundle_run_id=str(args.bundle_run_id or ""),
        reason=str(args.reason or ""),
    )
    print(out)
    return 0 if bool(out.get("ok")) else 1


def _models_release_status(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.settings import get_settings
    from fxstack.training.release_workflow import release_status

    s = get_settings()
    out = release_status(
        pair=str(args.pair).upper(),
        database_url=str(args.database_url or s.database_url),
        bundle_run_id=str(args.bundle_run_id or ""),
    )
    print(out)
    return 0 if bool(out.get("ok")) else 1


def _rl_export_transitions(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.rl.export_replay import export_replay_dataset

    input_path = Path(str(args.input))
    if not input_path.exists():
        print({"ok": False, "error": "input_not_found", "input": str(input_path)})
        return 2
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    out = export_replay_dataset(
        payload,
        out_dir=Path(str(args.out_dir)),
        dataset_name=str(args.dataset_name or "replay_transitions"),
        source_name=str(args.source_name or "decision_snapshots"),
        metadata={"input_path": str(input_path)},
    )
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def _rl_train_online(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.rl.train_online import run_online_training

    out = run_online_training(
        dataset_path=Path(str(args.dataset)),
        out_dir=Path(str(args.out_dir)),
        run_name=str(args.run_name),
        max_rows=int(args.max_rows),
        exploration_rate=float(args.exploration_rate),
    )
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if str(out.get("status") or "").lower() == "ok" else 1


def _rl_train_offline(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.rl.train_offline import run_offline_training

    out = run_offline_training(
        dataset_path=Path(str(args.dataset)),
        out_dir=Path(str(args.out_dir)),
        run_name=str(args.run_name),
        reward_scale=float(args.reward_scale),
    )
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if str(out.get("status") or "").lower() == "ok" else 1


def _rl_evaluate(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.rl.evaluate import evaluate_replay

    out = evaluate_replay(
        dataset_path=Path(str(args.dataset)),
        benchmark_path=Path(str(args.benchmark)) if str(args.benchmark or "").strip() else None,
        out_dir=Path(str(args.out_dir)),
        run_name=str(args.run_name),
    )
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if str(out.get("status") or "").lower() == "ok" else 1


def _agent_llm_check(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.llm.client import build_llm_client
    from fxstack.settings import get_settings

    client = build_llm_client(get_settings())
    health = client.health()
    print(json.dumps(health.as_dict(), indent=2, sort_keys=True))
    return 0


def _agent_propose(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.improve.evaluator import build_synthetic_dataset, evaluate_config
    from fxstack.improve.knobs import default_config, knob_values
    from fxstack.improve.objective import score_metrics
    from fxstack.improve.proposer import HeuristicProposer, ImprovementContext, LLMProposer, propose_with_fallback
    from fxstack.llm.client import build_llm_client
    from fxstack.settings import get_settings

    settings = get_settings()
    config = default_config(settings)
    dataset = build_synthetic_dataset(seed=int(args.seed))
    metrics = evaluate_config(config, dataset)
    score = score_metrics(metrics, min_trades=int(settings.improve_min_trades),
                          max_drawdown_pct=float(settings.improve_max_drawdown_pct))
    client = build_llm_client(settings)
    llm_proposer = LLMProposer(client) if getattr(client, "backend", "null") != "null" else None
    ctx = ImprovementContext(
        incumbent_config=config, incumbent_metrics=metrics, incumbent_objective=score.objective,
        iteration=int(args.seed) % 7, seed=int(args.seed), recent_reflections=[], tried_signatures=set(),
    )
    proposal, fallback = propose_with_fallback(
        llm_proposer=llm_proposer, heuristic_proposer=HeuristicProposer(), ctx=ctx
    )
    print(json.dumps({
        "incumbent_objective": score.objective,
        "incumbent_knobs": knob_values(config),
        "proposal": {
            "hypothesis": proposal.hypothesis,
            "change_set": proposal.change_set,
            "proposer": proposal.proposer,
            "model_id": proposal.model_id,
        },
        "fallback_reason": fallback,
    }, indent=2, sort_keys=True, default=str))
    return 0


def _agent_improve(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.improve.evaluator import load_parquet_dataset
    from fxstack.improve.loop import run_improvement_campaign, run_improvement_loop
    from fxstack.settings import get_settings

    settings = get_settings()
    dataset = load_parquet_dataset(str(args.dataset)) if str(args.dataset or "").strip() else None
    artifact_dir = str(args.out_dir or "").strip() or str(
        Path(str(settings.improve_artifact_root)) / "runs" / str(args.run_name)
    )
    iterations = int(args.iterations) if int(args.iterations) > 0 else None
    seed = int(args.seed) if int(args.seed) >= 0 else None
    restarts = max(1, int(args.restarts))

    if str(getattr(args, "runner", "loop")) == "graph":
        from fxstack.improve.graph import run_improvement_graph

        out = run_improvement_graph(dataset=dataset, settings=settings, seed=seed, max_iterations=iterations)
        print(json.dumps(out, indent=2, sort_keys=True, default=str))
        return 0
    common = dict(
        dataset=dataset,
        settings=settings,
        iterations=iterations,
        artifact_dir=artifact_dir,
        emit_experiment=not bool(args.no_experiment),
        experiment_id=str(args.experiment_id or ""),
        register_experiment=bool(args.register),
        experiment_base_dir=str(args.experiment_base_dir or "").strip() or None,
        upsert_service=not bool(args.no_service_upsert),
    )
    if restarts > 1:
        # Multi-restart campaign: explore the same landscape from several seeds and
        # keep the global OOS-validated best. Memory is per-run, so no shared file.
        result = run_improvement_campaign(restarts=restarts, base_seed=seed, **common)
    else:
        memory_path = str(args.memory or "").strip() or str(Path(artifact_dir) / "reflection_memory.jsonl")
        result = run_improvement_loop(memory_path=memory_path, seed=seed, **common)
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
    return 0


def _agent_build_dataset(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.improve.dataset_builder import ColumnMap, build_from_parquet, write_scored_signals

    columns = ColumnMap(
        swing_prob=str(args.swing_col), entry_prob=str(args.entry_col), trade_prob=str(args.trade_col),
        spread=str(args.spread_col), fwd_ret=str(args.fwd_ret_col), pair=str(args.pair_col), ts=str(args.ts_col),
        expected_edge=(str(args.edge_col).strip() or None),
    )
    frame = build_from_parquet(
        str(args.features), columns=columns, spread_unit=str(args.spread_unit),
        fwd_ret_unit=str(args.fwd_ret_unit), edge_scale_bps=float(args.edge_scale_bps),
    )
    out = write_scored_signals(frame, str(args.out))
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


def _agent_robustness(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.improve.evaluator import build_synthetic_dataset, load_parquet_dataset
    from fxstack.improve.robustness import robustness_report
    from fxstack.settings import get_settings

    settings = get_settings()
    run_dir = Path(str(args.run_dir))
    best_config_path = run_dir / "best_config.json"
    if not best_config_path.exists():
        print(json.dumps({"error": f"no best_config.json under {run_dir}"}))
        return 1
    config = json.loads(best_config_path.read_text(encoding="utf-8"))
    dataset = (
        load_parquet_dataset(str(args.dataset))
        if str(args.dataset or "").strip()
        else build_synthetic_dataset(seed=int(args.seed) if int(args.seed) >= 0 else int(settings.improve_seed))
    )
    report = robustness_report(
        config, dataset,
        min_trades=int(settings.improve_min_trades),
        max_drawdown_pct=float(settings.improve_max_drawdown_pct),
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


def _agent_explain(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.improve.explain import explain_run
    from fxstack.settings import get_settings

    run_dir = Path(str(args.run_dir))
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        print(json.dumps({"error": f"no summary.json under {run_dir}"}))
        return 1
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    entries: list[dict] = []
    jsonl = run_dir / "reflection_memory.jsonl"
    js = run_dir / "reflection_memory.json"
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    elif js.exists():
        payload = json.loads(js.read_text(encoding="utf-8"))
        entries = list(payload.get("entries") or [])

    out = explain_run(summary=summary, entries=entries, settings=get_settings())
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


def _security_secret(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.security.secrets import SecretStore

    store = SecretStore(directory=(str(args.dir).strip() or None))
    if args.set:
        value = str(args.value) if args.value is not None else os.environ.get("FXSTACK_SECRET_VALUE", "")
        if not value:
            print(json.dumps({"error": "provide --value or set FXSTACK_SECRET_VALUE"}))
            return 1
        store.set(str(args.set), value)
        print(json.dumps({"ok": True, "action": "set", "name": str(args.set), "backend": store.backend}))
        return 0
    if args.get:
        found = store.get(str(args.get))
        print(json.dumps({"name": str(args.get), "present": found is not None}))
        return 0 if found is not None else 1
    if args.delete:
        removed = store.delete(str(args.delete))
        print(json.dumps({"ok": True, "action": "delete", "name": str(args.delete), "removed": bool(removed)}))
        return 0
    print(json.dumps({"names": store.names(), "backend": store.backend}, indent=2, sort_keys=True))
    return 0


def _security_validate_offline(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.security.egress import validate_offline_compose_file
    from fxstack.settings import get_settings

    compose = str(args.compose or "").strip() or str(
        Path(get_settings().project_root) / "docker" / "docker-compose.offline.yml"
    )
    report = validate_offline_compose_file(compose)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 2


def _agent_verify_weights(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.llm.weights import load_manifest, verify_manifest

    report = verify_manifest(load_manifest(str(args.manifest)))
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 2


def _agent_metrics(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.improve.evaluator import build_synthetic_dataset, load_parquet_dataset
    from fxstack.research.vectorbt_harness import run_vectorbt_research
    from fxstack.settings import get_settings

    settings = get_settings()
    config = json.loads((Path(str(args.run_dir)) / "best_config.json").read_text(encoding="utf-8"))
    dataset = (
        load_parquet_dataset(str(args.dataset))
        if str(args.dataset or "").strip()
        else build_synthetic_dataset(seed=int(args.seed) if int(args.seed) >= 0 else int(settings.improve_seed))
    )
    print(json.dumps(run_vectorbt_research(config, dataset), indent=2, sort_keys=True, default=str))
    return 0


def _backtest_export_lean(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.backtest.harness.lean_codegen import write_lean_project
    from fxstack.settings import get_settings

    settings = get_settings()
    config = json.loads((Path(str(args.run_dir)) / "best_config.json").read_text(encoding="utf-8"))
    pairs = [p.strip().upper() for p in str(args.pairs).split(",") if p.strip()] or list(settings.pairs)
    out = write_lean_project(config, str(args.out), pairs=pairs, start=str(args.start), end=str(args.end),
                            cash=float(args.cash))
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


def _stack_preflight(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.settings import get_settings

    s = get_settings()
    allow_sqlite = bool(args.allow_sqlite or s.allow_sqlite)
    package_mode = str(os.environ.get("FXSTACK_PACKAGE_MODE", "")).strip().lower() not in {"", "0", "false", "no"}
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
    node_path = str(os.environ.get("NODE_EXE") or shutil.which("node") or "").strip()
    _push("node_available", bool(node_path), node_path)
    if package_mode:
        _push("pnpm_available", True, "not-required-in-package-mode")
    else:
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
        "dukascopy_python",
    ]
    swing_policy = str(getattr(s, "swing_model_policy", "") or "").strip().lower()
    intraday_policy = str(getattr(s, "intraday_model_policy", "") or "").strip().lower()
    require_deep_stack = (
        bool(s.require_cuda)
        or swing_policy == "transformer_primary_xgb_fallback"
        or intraday_policy == "tcn_primary_xgb_fallback"
        or bool(getattr(s, "sequence_shadow_enabled", False))
    )
    if require_deep_stack:
        required_modules.extend(["torch", "transformers", "pytorch_tcn"])
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


def _stack_sequence_research_check(args: argparse.Namespace) -> int:
    _fxstack_guard()
    from fxstack.research.sequence_runner import research_runner_diagnostics

    out = research_runner_diagnostics()
    print(out)
    return 0 if bool(out.get("ok", False)) else 2


def _ops_probe_endpoint(
    url: str, *, timeout: float, api_key: str
) -> dict[str, object]:
    """Hit a bridge JSON endpoint; return ``{ok, status_code, body, error}``."""
    import urllib.error
    import urllib.request

    out: dict[str, object] = {
        "ok": False,
        "status_code": None,
        "body": None,
        "error": None,
    }
    try:
        req = urllib.request.Request(url)
        if api_key:
            req.add_header("X-API-Key", api_key)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or ""
            out["status_code"] = int(resp.status)
            out["ok"] = int(resp.status) == 200
            try:
                out["body"] = json.loads(raw) if raw else {}
            except Exception:
                out["body"] = {"raw": raw}
    except urllib.error.HTTPError as exc:
        # 503 with a JSON body is the readyz "not ready" case — surface it.
        out["status_code"] = int(exc.code)
        try:
            raw = exc.read().decode("utf-8") or ""
            out["body"] = json.loads(raw) if raw else {}
        except Exception:
            out["error"] = f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        out["error"] = f"unreachable: {exc!s}"
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _ops_status(args: argparse.Namespace) -> int:
    """Probe a running bridge: livez + readyz, print pass/fail summary.

    Exit codes:
        0 — ready (livez OK + readyz=true)
        1 — alive but not ready
        2 — unreachable / livez failed
    """
    _fxstack_guard()
    from fxstack.settings import get_settings

    s = get_settings()
    base_url = str(getattr(args, "url", "") or s.mt4_bridge_url or "http://127.0.0.1:58710").rstrip("/")
    api_key = str(getattr(args, "api_key", "") or s.bridge_api_key or "").strip()
    timeout = float(getattr(args, "timeout", 5.0))

    livez = _ops_probe_endpoint(f"{base_url}/v2/livez", timeout=timeout, api_key=api_key)
    readyz = _ops_probe_endpoint(f"{base_url}/v2/readyz", timeout=timeout, api_key=api_key)

    summary: dict[str, object] = {
        "url": base_url,
        "livez": livez,
        "readyz": readyz,
    }

    if bool(getattr(args, "json", False)):
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"Bridge: {base_url}")
        if livez.get("ok"):
            print("  livez:    OK (200)")
        else:
            err = livez.get("error") or f"status={livez.get('status_code')}"
            print(f"  livez:    FAIL ({err})")
        raw_body = readyz.get("body")
        readyz_body: dict = raw_body if isinstance(raw_body, dict) else {}
        if readyz.get("error"):
            print(f"  readyz:   ERROR ({readyz['error']})")
        else:
            ready = bool(readyz_body.get("ready"))
            verdict = "READY (200)" if ready else f"NOT READY ({readyz.get('status_code')})"
            print(f"  readyz:   {verdict}")
            checks = readyz_body.get("checks")
            if isinstance(checks, dict):
                for name, value in checks.items():
                    print(f"            {name}: {'OK' if value else 'FAIL'}")

    if not livez.get("ok"):
        return 2
    final_body = readyz.get("body")
    if not (isinstance(final_body, dict) and bool(final_body.get("ready"))):
        return 1
    return 0


def _ops_validate_config(args: argparse.Namespace) -> int:
    """Run Settings.validate_for_startup() without starting anything.

    Exit codes:
        0 — config OK
        1 — one or more cross-field errors (each printed)
    """
    _fxstack_guard()
    from fxstack.settings import Settings

    s = Settings()
    errors = s.validate_for_startup()
    use_json = bool(getattr(args, "json", False))
    if not errors:
        if use_json:
            print(json.dumps({"ok": True, "errors": []}))
        else:
            print("config OK")
        return 0
    if use_json:
        print(json.dumps({"ok": False, "errors": errors}, indent=2))
    else:
        print(f"{len(errors)} config error(s):")
        for err in errors:
            print(f"  - {err}")
    return 1


def _port_from_url(url: str) -> int:
    """Extract the integer port from a URL like ``http://127.0.0.1:58710``."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(str(url or ""))
        return int(parsed.port or 0)
    except Exception:
        return 0


def _port_in_use(host: str, port: int) -> bool:
    """True if a TCP connection to ``(host, port)`` succeeds within 1 second."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        try:
            sock.connect((str(host), int(port)))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def _stack_deploy(args: argparse.Namespace) -> int:
    """One-command deploy: validate config → migrate DB → start bridge → wait until ready.

    This composes existing ``trader`` subcommands and the new ``ops`` probes
    into a single workflow operators run to bring the bridge up cleanly.

    Exit codes:
        0 — bridge is up and ``/v2/readyz`` returned 200 within the timeout
        1 — readiness timed out (bridge process is still running for inspection)
        2 — pre-flight failure (validate-config, migrate, or early bridge exit)
    """
    _fxstack_guard()
    from fxstack.settings import get_settings

    s = get_settings()

    print("[1/4] validating config…")
    rc = _ops_validate_config(argparse.Namespace(json=False))
    if rc != 0:
        print("config validation FAILED — fix the errors above and retry")
        return 2

    print("[2/4] running db migrations…")
    db_args = argparse.Namespace(database_url=str(getattr(args, "database_url", "") or ""))
    rc = _db_migrate(db_args)
    if rc != 0:
        print("db migration FAILED — check db connectivity and credentials")
        return 2

    bind_host = str(getattr(args, "host", "") or "127.0.0.1")
    bind_port = int(getattr(args, "port", 0) or _port_from_url(s.mt4_bridge_url) or 58710)
    allow_reuse = bool(getattr(args, "allow_reuse_port", False))
    if _port_in_use(bind_host, bind_port) and not allow_reuse:
        print(
            f"port {bind_host}:{bind_port} is already bound — pass --allow-reuse-port "
            "if you intend to attach to an existing bridge"
        )
        return 2

    log_dir = Path(str(getattr(args, "log_dir", "") or "") or (_repo_root() / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"bridge_{bind_port}.log"
    err_path = log_dir / f"bridge_{bind_port}.err.log"
    pid_path = log_dir / f"bridge_{bind_port}.pid"

    print(f"[3/4] starting bridge on {bind_host}:{bind_port}…")
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "src.trader.cli",
        "bridge",
        "serve",
        "--host",
        bind_host,
        "--port",
        str(bind_port),
    ]
    log_handle = open(log_path, "wb")
    err_handle = open(err_path, "wb")
    try:
        proc = subprocess.Popen(  # noqa: S603 - args are program-controlled
            cmd,
            stdout=log_handle,
            stderr=err_handle,
            cwd=str(_repo_root()),
        )
    except Exception as exc:
        log_handle.close()
        err_handle.close()
        print(f"failed to spawn bridge process: {exc!r}")
        return 2
    pid_path.write_text(str(proc.pid), encoding="utf-8")

    print(f"[4/4] polling /v2/readyz (pid={proc.pid}, log={log_path})…")
    timeout = float(getattr(args, "timeout", 60.0))
    api_key = str(s.bridge_api_key or "").strip()
    base_url = f"http://{bind_host}:{bind_port}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(
                f"bridge exited early with code {proc.returncode}; "
                f"see {log_path} and {err_path}"
            )
            return 2
        livez = _ops_probe_endpoint(f"{base_url}/v2/livez", timeout=2.0, api_key=api_key)
        if livez.get("ok"):
            readyz = _ops_probe_endpoint(f"{base_url}/v2/readyz", timeout=5.0, api_key=api_key)
            body = readyz.get("body") if isinstance(readyz.get("body"), dict) else {}
            if readyz.get("ok") and isinstance(body, dict) and bool(body.get("ready")):
                print(f"bridge READY  pid={proc.pid}  url={base_url}")
                return 0
        time.sleep(1.0)

    print(
        f"readiness timeout after {timeout}s; bridge still running at pid={proc.pid}. "
        f"Inspect with: trader ops status --url {base_url}"
    )
    return 1


def _ops_tail_logs(args: argparse.Namespace) -> int:
    """Read JSON log lines from stdin or a file; print human-readable.

    Each line is JSON-parsed; non-JSON lines pass through unchanged so
    mixed-format streams (e.g. tracebacks interleaved with structured logs)
    still render cleanly. Extra fields beyond the standard ts/level/logger/
    rid/msg/exc are appended as a compact JSON suffix so context survives.
    """
    file_arg = str(getattr(args, "file", "-"))
    source = sys.stdin if file_arg == "-" else open(file_arg, "r", encoding="utf-8")
    standard_keys = {"ts", "level", "logger", "rid", "msg", "exc", "stack"}
    try:
        for raw_line in source:
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                print(line)
                continue
            ts = str(obj.get("ts") or "")
            level = str(obj.get("level") or "INFO")
            logger = str(obj.get("logger") or "")
            rid = str(obj.get("rid") or "-")
            msg = str(obj.get("msg") or "")
            extras = {k: v for k, v in obj.items() if k not in standard_keys}
            extra_str = (
                " " + json.dumps(extras, separators=(",", ":"), default=str)
                if extras
                else ""
            )
            print(f"{ts} {level:5s} [{logger}] [rid={rid}] {msg}{extra_str}")
            if obj.get("exc"):
                print(str(obj["exc"]))
    finally:
        if source is not sys.stdin:
            source.close()
    return 0


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
    bi = backtest_sub.add_parser("internal-pnl", help="Run the internal lifecycle/equity simulator")
    bi.add_argument("tool_args", nargs=argparse.REMAINDER)
    bi.set_defaults(_fn=_backtest_internal_pnl)
    bn = backtest_sub.add_parser("nautilus", help="Plan or run the Nautilus Phase 3 harness")
    bn.add_argument("module_args", nargs=argparse.REMAINDER)
    bn.set_defaults(_fn=_backtest_nautilus)
    bl = backtest_sub.add_parser("lean", help="Plan or run the LEAN Phase 3 harness")
    bl.add_argument("module_args", nargs=argparse.REMAINDER)
    bl.set_defaults(_fn=_backtest_lean)
    bstress = backtest_sub.add_parser("stress", help="Apply Phase 3 stress scenarios to a normalized report")
    bstress.add_argument("module_args", nargs=argparse.REMAINDER)
    bstress.set_defaults(_fn=_backtest_stress)
    bxl = backtest_sub.add_parser("export-lean", help="Export a tuned config to a runnable QuantConnect Lean project")
    bxl.add_argument("--run-dir", required=True, help="Artifact dir containing best_config.json")
    bxl.add_argument("--out", required=True, help="Destination Lean project directory")
    bxl.add_argument("--pairs", default="", help="Comma-separated pairs (default: settings.pairs)")
    bxl.add_argument("--start", default="2022-01-01", help="Backtest start date (YYYY-MM-DD)")
    bxl.add_argument("--end", default="2023-01-01", help="Backtest end date (YYYY-MM-DD)")
    bxl.add_argument("--cash", type=float, default=100000.0, help="Starting cash")
    bxl.set_defaults(_fn=_backtest_export_lean)

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
    fbl = features_sub.add_parser("build-fx-lifecycle", help="Build hierarchical FX lifecycle features")
    fbl.add_argument("--pair", required=True)
    fbl.add_argument("--anchor-timeframe", default="M5")
    fbl.add_argument("--context-timeframes", nargs="*", default=["M15", "H1", "H4", "D"])
    fbl.add_argument("--input-root", default="fx-quant-stack/data/raw")
    fbl.add_argument("--output-root", default="fx-quant-stack/data/features")
    fbl.add_argument("--report-root", default="")
    fbl.set_defaults(_fn=_features_build_fx_lifecycle)
    fcf = features_sub.add_parser("compact-feast", help="Compact lifecycle feature lake into Feast-ready parquet views")
    fcf.add_argument("--pair", nargs="*", default=[])
    fcf.add_argument("--feature-root", default="fx-quant-stack/data/features")
    fcf.add_argument("--output-root", default="")
    fcf.add_argument("--provider", default="")
    fcf.set_defaults(_fn=_features_compact_feast)
    fpw = features_sub.add_parser("push-worker", help="Drain queued feature push intents into the Feast online store")
    fpw.add_argument("--database-url", default="")
    fpw.add_argument("--repo-root", default="")
    fpw.add_argument("--worker-id", default="")
    fpw.add_argument("--limit", type=int, default=50)
    fpw.add_argument("--max-retries", type=int, default=0)
    fpw.add_argument("--dry-run", action="store_true")
    fpw.set_defaults(_fn=_features_push_worker)

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
    lbm = labels_sub.add_parser("build-meta", help="Build cost-aware meta labels")
    lbm.add_argument("--pair", required=True)
    lbm.add_argument("--timeframe", default="M5")
    lbm.add_argument("--feature-root", default="fx-quant-stack/data/features")
    lbm.add_argument("--label-root", default="fx-quant-stack/data/labels")
    lbm.add_argument("--cost-stress-levels", nargs="*", type=float, default=[1.0, 1.25, 1.5])
    lbm.add_argument("--regime-model", default="")
    lbm.add_argument("--swing-model", default="")
    lbm.add_argument("--intraday-model", default="")
    lbm.add_argument("--allow-heuristic-meta-labels", action="store_true")
    lbm.set_defaults(_fn=_labels_build_meta)
    lbe = labels_sub.add_parser("build-exit", help="Build lifecycle exit labels")
    lbe.add_argument("--pair", required=True)
    lbe.add_argument("--timeframe", default="M5")
    lbe.add_argument("--feature-root", default="fx-quant-stack/data/features")
    lbe.add_argument("--label-root", default="fx-quant-stack/data/labels")
    lbe.add_argument("--method", default="trade_outcome")
    lbe.add_argument("--horizon-bars", type=int, default=24)
    lbe.set_defaults(_fn=_labels_build_exit)
    lbr = labels_sub.add_parser("build-reversal", help="Build reversal labels")
    lbr.add_argument("--pair", required=True)
    lbr.add_argument("--timeframe", default="M5")
    lbr.add_argument("--feature-root", default="fx-quant-stack/data/features")
    lbr.add_argument("--label-root", default="fx-quant-stack/data/labels")
    lbr.add_argument("--horizon-bars", type=int, default=24)
    lbr.set_defaults(_fn=_labels_build_reversal)

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

    tsp = train_sub.add_parser("swing-patchtst", help="Train swing PatchTST challenger model")
    tsp.add_argument("--pair", required=True)
    tsp.add_argument("--timeframe", default="D")
    tsp.add_argument("--feature-root", default="fx-quant-stack/data/features")
    tsp.add_argument("--label-root", default="fx-quant-stack/data/labels")
    tsp.add_argument("--out", default="fx-quant-stack/artifacts/swing_patchtst")
    tsp.set_defaults(_fn=_train_swing_patchtst)

    tit = train_sub.add_parser("intraday-tcn", help="Train intraday TCN model")
    tit.add_argument("--pair", required=True)
    tit.add_argument("--timeframe", default="M5")
    tit.add_argument("--feature-root", default="fx-quant-stack/data/features")
    tit.add_argument("--label-root", default="fx-quant-stack/data/labels")
    tit.add_argument("--out", default="fx-quant-stack/artifacts/intraday_tcn")
    tit.set_defaults(_fn=_train_intraday_tcn)

    tip = train_sub.add_parser("intraday-patchtst", help="Train intraday PatchTST challenger model")
    tip.add_argument("--pair", required=True)
    tip.add_argument("--timeframe", default="M5")
    tip.add_argument("--feature-root", default="fx-quant-stack/data/features")
    tip.add_argument("--label-root", default="fx-quant-stack/data/labels")
    tip.add_argument("--out", default="fx-quant-stack/artifacts/intraday_patchtst")
    tip.set_defaults(_fn=_train_intraday_patchtst)

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
    tm.add_argument("--label-root", default="fx-quant-stack/data/labels")
    tm.add_argument("--out", default="fx-quant-stack/artifacts/meta_filter")
    tm.add_argument("--regime-model", default="")
    tm.add_argument("--swing-model", default="")
    tm.add_argument("--intraday-model", default="")
    tm.add_argument("--allow-heuristic-meta-labels", action="store_true")
    tm.set_defaults(_fn=_train_meta)
    te = train_sub.add_parser("exit", help="Train lifecycle exit policy model")
    te.add_argument("--pair", required=True)
    te.add_argument("--timeframe", default="M5")
    te.add_argument("--feature-root", default="fx-quant-stack/data/features")
    te.add_argument("--label-root", default="fx-quant-stack/data/labels")
    te.add_argument("--out", default="fx-quant-stack/artifacts/exit_policy_xgb")
    te.set_defaults(_fn=_train_exit)
    trv = train_sub.add_parser("reversal", help="Train reversal failure/opportunity models")
    trv.add_argument("--pair", required=True)
    trv.add_argument("--timeframe", default="M5")
    trv.add_argument("--feature-root", default="fx-quant-stack/data/features")
    trv.add_argument("--label-root", default="fx-quant-stack/data/labels")
    trv.add_argument("--out-root", default="fx-quant-stack/artifacts")
    trv.set_defaults(_fn=_train_reversal)
    tbelief = train_sub.add_parser("belief", help="Train directional belief v2 model bundle")
    tbelief.add_argument("--pair", default="")
    tbelief.add_argument("--pairs", nargs="*", default=[])
    tbelief.add_argument("--timeframe", default="M5")
    tbelief.add_argument("--feature-root", default="fx-quant-stack/data/features")
    tbelief.add_argument("--out", default="fx-quant-stack/artifacts/directional_belief")
    tbelief.add_argument("--dataset-out", default="")
    tbelief.add_argument("--max-queries-per-pair", type=int, default=20000)
    tbelief.set_defaults(_fn=_train_belief)
    tbeliefv2 = train_sub.add_parser("belief-v2", help="Train directional belief v2 model bundle")
    tbeliefv2.add_argument("--pair", default="")
    tbeliefv2.add_argument("--pairs", nargs="*", default=[])
    tbeliefv2.add_argument("--timeframe", default="M5")
    tbeliefv2.add_argument("--feature-root", default="fx-quant-stack/data/features")
    tbeliefv2.add_argument("--out", default="fx-quant-stack/artifacts/directional_belief")
    tbeliefv2.add_argument("--dataset-out", default="")
    tbeliefv2.add_argument("--max-queries-per-pair", type=int, default=20000)
    tbeliefv2.set_defaults(_fn=_train_belief)
    tbeliefds = train_sub.add_parser("belief-dataset", help="Export directional belief v2 hypothesis dataset")
    tbeliefds.add_argument("--pair", default="")
    tbeliefds.add_argument("--pairs", nargs="*", default=[])
    tbeliefds.add_argument("--timeframe", default="M5")
    tbeliefds.add_argument("--feature-root", default="fx-quant-stack/data/features")
    tbeliefds.add_argument("--out", default="fx-quant-stack/artifacts/directional_belief_dataset.csv.gz")
    tbeliefds.add_argument("--max-queries-per-pair", type=int, default=20000)
    tbeliefds.set_defaults(_fn=_train_belief_dataset)

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
    ta.add_argument("--deep-stale-hours", type=float, default=72.0)
    ta.add_argument("--force-retrain", action="store_true")
    ta.add_argument("--lifecycle-only", action="store_true")
    ta.add_argument("--with-belief", action=argparse.BooleanOptionalAction, default=True)
    ta.add_argument("--with-patchtst", action="store_true")
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

    dp = db_sub.add_parser("ping", help="Verify database connectivity without requiring schema")
    dp.add_argument("--database-url", default="")
    dp.add_argument("--allow-sqlite", action="store_true")
    dp.set_defaults(_fn=_db_ping)

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
    ma.add_argument("--source", choices=["compat", "mlflow"], default="compat")
    ma.add_argument("--alias", choices=["champion", "shadow"], default="champion")
    ma.add_argument("--require-all", action="store_true")
    ma.set_defaults(_fn=_models_activate)
    mb = models_sub.add_parser("backfill-mlflow", help="Import current active and latest shadow registries into MLflow")
    mb.add_argument("--manifest", default="")
    mb.add_argument("--registry-root", default="")
    mb.add_argument("--shadow-root", default="fx-quant-stack/artifacts_shadow")
    mb.set_defaults(_fn=_models_backfill_mlflow)
    ms = models_sub.add_parser("set-alias", help="Assign a full pair bundle alias in MLflow from a compatibility registry file")
    ms.add_argument("--registry-root", default="")
    ms.add_argument("--registry-file", default="")
    ms.add_argument("--pair", default="")
    ms.add_argument("--alias", choices=["champion", "shadow"], required=True)
    ms.set_defaults(_fn=_models_set_alias)
    mst = models_sub.add_parser("stage-release", help="Create a Phase 5 activation package and release note")
    mst.add_argument("--pair", required=True)
    mst.add_argument("--alias", choices=["champion", "shadow"], default="shadow")
    mst.add_argument("--title", default="")
    mst.add_argument("--summary", default="")
    mst.add_argument("--author", default="")
    mst.add_argument("--allowlisted-pair", action="append", default=[])
    mst.add_argument("--budget-scale", type=float, default=None)
    mst.add_argument("--duration-minutes", type=int, default=None)
    mst.set_defaults(_fn=_models_stage_release)
    mpr = models_sub.add_parser("promote", help="Record operator signoff for a staged release")
    mpr.add_argument("--pair", required=True)
    mpr.add_argument("--bundle-run-id", default="")
    mpr.add_argument("--author", required=True)
    mpr.set_defaults(_fn=_models_promote)
    msa = models_sub.add_parser("shadow-accept", help="Mark a staged release as shadow-accepted")
    msa.add_argument("--pair", required=True)
    msa.add_argument("--bundle-run-id", default="")
    msa.set_defaults(_fn=_models_shadow_accept)
    mcs = models_sub.add_parser("canary-start", help="Activate the shadow candidate on allowlisted pairs in the main runtime")
    mcs.add_argument("--pair", required=True)
    mcs.add_argument("--bundle-run-id", default="")
    mcs.add_argument("--database-url", default="")
    mcs.add_argument("--manifest", default="")
    mcs.set_defaults(_fn=_models_canary_start)
    mcm = models_sub.add_parser("canary-monitor", help="Evaluate active canary health and trigger rollback if needed")
    mcm.add_argument("--pair", required=True)
    mcm.add_argument("--bundle-run-id", default="")
    mcm.add_argument("--database-url", default="")
    mcm.add_argument("--manifest", default="")
    mcm.set_defaults(_fn=_models_canary_monitor)
    mcc = models_sub.add_parser("canary-close", help="Close a canary by graduating or rejecting the candidate")
    mcc.add_argument("--pair", required=True)
    mcc.add_argument("--bundle-run-id", default="")
    mcc.add_argument("--database-url", default="")
    mcc.add_argument("--manifest", default="")
    mcc.add_argument("--outcome", choices=["graduate", "reject"], required=True)
    mcc.set_defaults(_fn=_models_canary_close)
    mrb = models_sub.add_parser("rollback", help="Rollback a release to its recorded champion target")
    mrb.add_argument("--pair", required=True)
    mrb.add_argument("--bundle-run-id", default="")
    mrb.add_argument("--database-url", default="")
    mrb.add_argument("--manifest", default="")
    mrb.add_argument("--reason", default="")
    mrb.set_defaults(_fn=_models_rollback)
    mrs = models_sub.add_parser("release-status", help="Show the current Phase 5 release package and runtime status")
    mrs.add_argument("--pair", required=True)
    mrs.add_argument("--bundle-run-id", default="")
    mrs.add_argument("--database-url", default="")
    mrs.set_defaults(_fn=_models_release_status)

    rl = sub.add_parser("rl", help="Phase 6 RL research commands")
    rl_sub = rl.add_subparsers(dest="rl_cmd", required=True)
    rle = rl_sub.add_parser("export-transitions", help="Export replay/decision snapshots into a Phase 6 transition dataset")
    rle.add_argument("--input", required=True)
    rle.add_argument("--out-dir", default="fx-quant-stack/artifacts/rl/datasets/export")
    rle.add_argument("--dataset-name", default="replay_transitions")
    rle.add_argument("--source-name", default="decision_snapshots")
    rle.set_defaults(_fn=_rl_export_transitions)
    rlp = rl_sub.add_parser("train-ppo", help="Run the PPO-flavored online RL research lane")
    rlp.add_argument("--dataset", required=True)
    rlp.add_argument("--out-dir", default="fx-quant-stack/artifacts/rl/online/ppo")
    rlp.add_argument("--run-name", default="ppo_research")
    rlp.add_argument("--max-rows", type=int, default=5000)
    rlp.add_argument("--exploration-rate", type=float, default=0.1)
    rlp.set_defaults(_fn=_rl_train_online)
    rls = rl_sub.add_parser("train-sac", help="Run the SAC-flavored online RL research lane")
    rls.add_argument("--dataset", required=True)
    rls.add_argument("--out-dir", default="fx-quant-stack/artifacts/rl/online/sac")
    rls.add_argument("--run-name", default="sac_research")
    rls.add_argument("--max-rows", type=int, default=5000)
    rls.add_argument("--exploration-rate", type=float, default=0.1)
    rls.set_defaults(_fn=_rl_train_online)
    rlc = rl_sub.add_parser("train-cql", help="Run the CQL-flavored offline RL research lane")
    rlc.add_argument("--dataset", required=True)
    rlc.add_argument("--out-dir", default="fx-quant-stack/artifacts/rl/offline/cql")
    rlc.add_argument("--run-name", default="cql_research")
    rlc.add_argument("--reward-scale", type=float, default=1.0)
    rlc.set_defaults(_fn=_rl_train_offline)
    rli = rl_sub.add_parser("train-iql", help="Run the IQL-flavored offline RL research lane")
    rli.add_argument("--dataset", required=True)
    rli.add_argument("--out-dir", default="fx-quant-stack/artifacts/rl/offline/iql")
    rli.add_argument("--run-name", default="iql_research")
    rli.add_argument("--reward-scale", type=float, default=1.0)
    rli.set_defaults(_fn=_rl_train_offline)
    rla = rl_sub.add_parser("train-awac", help="Run the AWAC-flavored offline RL research lane")
    rla.add_argument("--dataset", required=True)
    rla.add_argument("--out-dir", default="fx-quant-stack/artifacts/rl/offline/awac")
    rla.add_argument("--run-name", default="awac_research")
    rla.add_argument("--reward-scale", type=float, default=1.0)
    rla.set_defaults(_fn=_rl_train_offline)
    rlv = rl_sub.add_parser("evaluate", help="Evaluate RL replay output against a benchmark dataset")
    rlv.add_argument("--dataset", required=True)
    rlv.add_argument("--benchmark", default="")
    rlv.add_argument("--out-dir", default="fx-quant-stack/artifacts/rl/eval")
    rlv.add_argument("--run-name", default="rl_research_eval")
    rlv.set_defaults(_fn=_rl_evaluate)

    agent = sub.add_parser("agent", help="Self-improving research loop (LLM proposes; code disposes)")
    agent_sub = agent.add_subparsers(dest="agent_cmd", required=True)
    ai_imp = agent_sub.add_parser("improve", help="Run the self-improvement loop and emit an experiment proposal")
    ai_imp.add_argument("--dataset", default="", help="Scored-signals parquet path (default: deterministic synthetic)")
    ai_imp.add_argument("--out-dir", default="", help="Artifact dir (default: <improve_artifact_root>/runs/<run-name>)")
    ai_imp.add_argument("--run-name", default="loop", help="Run name used in the default artifact path")
    ai_imp.add_argument("--memory", default="", help="Reflection-memory JSONL path (default: <out-dir>/reflection_memory.jsonl)")
    ai_imp.add_argument("--iterations", type=int, default=0, help="Iterations (default: settings.improve_max_iterations)")
    ai_imp.add_argument("--seed", type=int, default=-1, help="Seed (default: settings.improve_seed)")
    ai_imp.add_argument("--restarts", type=int, default=1, help="Multi-restart campaign size; >1 keeps the global OOS-validated best")
    ai_imp.add_argument("--runner", choices=["loop", "graph"], default="loop", help="Execution runner: plain loop or LangGraph StateGraph")
    ai_imp.add_argument("--experiment-id", default="", help="Experiment id (default: derived from best change-set)")
    ai_imp.add_argument("--no-experiment", action="store_true", help="Skip emitting the Phase-7 ExperimentProposal")
    ai_imp.add_argument("--register", action="store_true", help="Register the proposal as a draft in the experiment factory")
    ai_imp.add_argument("--experiment-base-dir", default="", help="Override the experiment factory bundle root (for tests/sandboxes)")
    ai_imp.add_argument("--no-service-upsert", action="store_true", help="Do not attempt a runtime-service upsert during registration")
    ai_imp.set_defaults(_fn=_agent_improve)
    ai_prop = agent_sub.add_parser("propose", help="Emit a single proposal for the seed config (no evaluation loop)")
    ai_prop.add_argument("--seed", type=int, default=1729, help="Proposal seed")
    ai_prop.set_defaults(_fn=_agent_propose)
    ai_llm = agent_sub.add_parser("llm-check", help="Report the configured local LLM backend health (offline-safe)")
    ai_llm.set_defaults(_fn=_agent_llm_check)
    ai_exp = agent_sub.add_parser("explain", help="Explain a prior improvement run in plain language (offline-safe)")
    ai_exp.add_argument("--run-dir", required=True, help="Artifact dir of a prior `agent improve` run")
    ai_exp.set_defaults(_fn=_agent_explain)
    ai_rob = agent_sub.add_parser("robustness", help="Measure objective sensitivity of a tuned config (fragility check)")
    ai_rob.add_argument("--run-dir", required=True, help="Artifact dir containing best_config.json")
    ai_rob.add_argument("--dataset", default="", help="Scored-signals parquet (default: deterministic synthetic)")
    ai_rob.add_argument("--seed", type=int, default=-1, help="Synthetic dataset seed (default: settings.improve_seed)")
    ai_rob.set_defaults(_fn=_agent_robustness)
    ai_bd = agent_sub.add_parser("build-dataset", help="Convert a scored feature parquet into the loop's scored-signals schema")
    ai_bd.add_argument("--features", required=True, help="Source parquet (file or dir) of scored features")
    ai_bd.add_argument("--out", required=True, help="Destination scored-signals parquet path")
    ai_bd.add_argument("--swing-col", default="swing_prob")
    ai_bd.add_argument("--entry-col", default="entry_prob")
    ai_bd.add_argument("--trade-col", default="trade_prob")
    ai_bd.add_argument("--spread-col", default="spread")
    ai_bd.add_argument("--fwd-ret-col", default="fwd_ret")
    ai_bd.add_argument("--pair-col", default="pair")
    ai_bd.add_argument("--ts-col", default="ts")
    ai_bd.add_argument("--edge-col", default="", help="Explicit expected-edge column in bps (default: derive from trade_prob)")
    ai_bd.add_argument("--spread-unit", default="fraction", choices=["fraction", "bps", "pct"])
    ai_bd.add_argument("--fwd-ret-unit", default="fraction", choices=["fraction", "bps", "pct"])
    ai_bd.add_argument("--edge-scale-bps", type=float, default=12.0)
    ai_bd.set_defaults(_fn=_agent_build_dataset)
    ai_vw = agent_sub.add_parser("verify-weights", help="Verify staged model weights against a checksum manifest")
    ai_vw.add_argument("--manifest", required=True, help="Path to a weight manifest JSON")
    ai_vw.set_defaults(_fn=_agent_verify_weights)
    ai_met = agent_sub.add_parser("metrics", help="Richer research metrics (vectorbt harness) for a tuned config")
    ai_met.add_argument("--run-dir", required=True, help="Artifact dir containing best_config.json")
    ai_met.add_argument("--dataset", default="", help="Scored-signals parquet (default: synthetic)")
    ai_met.add_argument("--seed", type=int, default=-1, help="Synthetic dataset seed (default: settings.improve_seed)")
    ai_met.set_defaults(_fn=_agent_metrics)

    security = sub.add_parser("security", help="Offline security: secret manager + air-gap validation")
    security_sub = security.add_subparsers(dest="security_cmd", required=True)
    sec_s = security_sub.add_parser("secret", help="Local encrypted secret store (set/get/delete/list)")
    sec_s.add_argument("--dir", default="", help="Secret store directory (default: package .secrets/)")
    sec_s.add_argument("--set", default="", help="Secret name to set (value via --value or FXSTACK_SECRET_VALUE)")
    sec_s.add_argument("--value", default=None, help="Secret value (avoid on shared shells; prefer the env var)")
    sec_s.add_argument("--get", default="", help="Secret name to check presence (never prints the value)")
    sec_s.add_argument("--delete", default="", help="Secret name to delete")
    sec_s.set_defaults(_fn=_security_secret)
    sec_v = security_sub.add_parser("validate-offline", help="Assert the offline compose air-gap invariants")
    sec_v.add_argument("--compose", default="", help="Compose path (default: docker/docker-compose.offline.yml)")
    sec_v.set_defaults(_fn=_security_validate_offline)

    stack = sub.add_parser("stack", help="Stack orchestration helpers")
    stack_sub = stack.add_subparsers(dest="stack_cmd", required=True)
    sp = stack_sub.add_parser("preflight", help="Validate environment, dependencies, and key settings")
    sp.add_argument("--allow-sqlite", action="store_true")
    sp.set_defaults(_fn=_stack_preflight)
    sg = stack_sub.add_parser("gpu-check", help="Validate CUDA availability for deep-model runtime")
    sg.set_defaults(_fn=_stack_gpu_check)
    ssr = stack_sub.add_parser("sequence-research-check", help="Validate the PatchTST/iTransformer research runner environment")
    ssr.set_defaults(_fn=_stack_sequence_research_check)

    sd = stack_sub.add_parser(
        "deploy",
        help="One-command deploy: validate config -> migrate DB -> start bridge -> wait until ready",
    )
    sd.add_argument("--host", default="127.0.0.1", help="Bridge bind host (default: 127.0.0.1)")
    sd.add_argument("--port", type=int, default=0, help="Bridge bind port (default: parsed from MT4_BRIDGE_URL or 58710)")
    sd.add_argument("--timeout", type=float, default=60.0, help="Readiness poll timeout seconds (default: 60)")
    sd.add_argument("--log-dir", default="", help="Log directory (default: <repo_root>/logs)")
    sd.add_argument("--database-url", default="", help="Override FXSTACK_DATABASE_URL for the migration step")
    sd.add_argument("--allow-reuse-port", action="store_true", help="Skip the port-in-use preflight check")
    sd.set_defaults(_fn=_stack_deploy)

    ops = sub.add_parser("ops", help="Operator daily-use commands (status, config, logs)")
    ops_sub = ops.add_subparsers(dest="ops_cmd", required=True)

    ostatus = ops_sub.add_parser(
        "status",
        help="Probe a running bridge: livez + readyz, print pass/fail summary",
    )
    ostatus.add_argument("--url", default="", help="Bridge base URL (default: settings.mt4_bridge_url)")
    ostatus.add_argument("--api-key", default="", help="X-API-Key header value (default: settings.bridge_api_key)")
    ostatus.add_argument("--timeout", default=5.0, type=float, help="HTTP timeout seconds (default: 5.0)")
    ostatus.add_argument("--json", action="store_true", help="Emit raw JSON instead of human-readable")
    ostatus.set_defaults(_fn=_ops_status)

    ovc = ops_sub.add_parser(
        "validate-config",
        help="Run Settings cross-field validators; non-zero exit on any error",
    )
    ovc.add_argument("--json", action="store_true", help="Emit raw JSON instead of human-readable")
    ovc.set_defaults(_fn=_ops_validate_config)

    otl = ops_sub.add_parser(
        "tail-logs",
        help="Pretty-print JSON log lines from stdin or a file (FXSTACK_LOG_FORMAT=json output)",
    )
    otl.add_argument("file", nargs="?", default="-", help="Log file path, or '-' for stdin (default: -)")
    otl.set_defaults(_fn=_ops_tail_logs)

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
