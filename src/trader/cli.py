from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
from typing import Callable


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
    argv = [
        "--config",
        str(args.config),
        "--equity",
        str(args.equity),
        "--sleep",
        str(args.sleep),
    ]
    if bool(args.skip_validation):
        argv.append("--skip-validation")
    return _run_python_main("src.run_fx", argv=argv)


def _bridge_serve(args: argparse.Namespace) -> int:
    from bridge_api.bridge import app

    host = str(args.host)
    port = int(args.port)
    app.run(host=host, port=port, debug=False)
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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="trader", description="Unified trading system CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    runtime = sub.add_parser("runtime", help="Runtime controls")
    runtime_sub = runtime.add_subparsers(dest="runtime_cmd", required=True)
    rr = runtime_sub.add_parser("run", help="Run live runtime loop")
    rr.add_argument("--config", default="src/config/fx_el_minis.yaml")
    rr.add_argument("--equity", type=float, required=True)
    rr.add_argument("--sleep", type=int, default=5)
    rr.add_argument("--skip-validation", action="store_true")
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
    bwf = backtest_sub.add_parser("walk-forward", help="Run walk-forward tuner")
    bwf.add_argument("tool_args", nargs=argparse.REMAINDER)
    bwf.set_defaults(_fn=lambda a: _tool_passthrough("tools.walk_forward_tune", a))

    audit = sub.add_parser("audit", help="Audit commands")
    audit_sub = audit.add_subparsers(dest="audit_cmd", required=True)
    asc = audit_sub.add_parser("strategy-conflict", help="Run strategy conflict audit")
    asc.add_argument("tool_args", nargs=argparse.REMAINDER)
    asc.set_defaults(_fn=lambda a: _tool_passthrough("tools.strategy_conflict_audit", a))
    ai = audit_sub.add_parser("interop", help="Run interop efficiency audit")
    ai.add_argument("tool_args", nargs=argparse.REMAINDER)
    ai.set_defaults(_fn=lambda a: _tool_passthrough("tools.mt4_interop_efficiency_audit", a))
    bf = audit_sub.add_parser("baseline-freeze", help="Generate baseline KPI + contract freeze artifacts")
    bf.add_argument("tool_args", nargs=argparse.REMAINDER)
    bf.set_defaults(_fn=lambda a: _tool_passthrough("tools.baseline_freeze", a))

    opt = sub.add_parser("optimize", help="Optimization commands")
    opt_sub = opt.add_subparsers(dest="opt_cmd", required=True)
    op = opt_sub.add_parser("profile", help="Run profile optimizer")
    op.add_argument("tool_args", nargs=argparse.REMAINDER)
    op.set_defaults(_fn=lambda a: _tool_passthrough("tools.optimize_eurusd_profile", a))

    scen = sub.add_parser("scenario", help="Scenario commands")
    scen_sub = scen.add_subparsers(dest="scenario_cmd", required=True)
    sm = scen_sub.add_parser("matrix", help="Run scenario matrix")
    sm.add_argument("tool_args", nargs=argparse.REMAINDER)
    sm.set_defaults(_fn=lambda a: _tool_passthrough("tools.scenario_backtest_matrix", a))
    dr = scen_sub.add_parser("dual-run-compare", help="Compare dual-run trace artifacts")
    dr.add_argument("tool_args", nargs=argparse.REMAINDER)
    dr.set_defaults(_fn=lambda a: _tool_passthrough("tools.dual_run_compare", a))
    sr = scen_sub.add_parser("shadow-run", help="Run live baseline/candidate shadow dual-run with canary gates")
    sr.add_argument("tool_args", nargs=argparse.REMAINDER)
    sr.set_defaults(_fn=lambda a: _tool_passthrough("tools.shadow_dual_run", a))

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
