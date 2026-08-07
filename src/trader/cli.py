"""Repository-only facade for isolated research and offline security tools.

Production, operator, training, activation, database, and live-data commands have
focused entrypoints under ``fx-quant-stack`` or ``ops``.  They are intentionally
absent here so this compatibility module cannot become a second launch plane.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
from collections.abc import Callable
from pathlib import Path


Handler = Callable[[argparse.Namespace], int]


def _require_fxstack() -> None:
    """Require the authoritative development environment without path injection."""

    try:
        importlib.import_module("fxstack")
    except ModuleNotFoundError as exc:
        if exc.name != "fxstack":
            raise
        raise SystemExit(
            "fxstack is unavailable. Run this source-only facade through "
            "`uv run --project fx-quant-stack python -m src.trader.cli ...`."
        ) from None


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _agent_llm_check(_args: argparse.Namespace) -> int:
    _require_fxstack()
    from fxstack.llm.client import build_llm_client
    from fxstack.settings import get_settings

    _print_json(build_llm_client(get_settings()).health().as_dict())
    return 0


def _agent_propose(args: argparse.Namespace) -> int:
    _require_fxstack()
    from fxstack.improve.evaluator import build_synthetic_dataset, evaluate_config
    from fxstack.improve.knobs import default_config, knob_values
    from fxstack.improve.objective import score_metrics
    from fxstack.improve.proposer import (
        HeuristicProposer,
        ImprovementContext,
        LLMProposer,
        propose_with_fallback,
    )
    from fxstack.llm.client import build_llm_client
    from fxstack.settings import get_settings

    seed = int(args.seed)
    settings = get_settings()
    config = default_config(settings)
    metrics = evaluate_config(config, build_synthetic_dataset(seed=seed))
    score = score_metrics(
        metrics,
        min_trades=int(settings.improve_min_trades),
        max_drawdown_pct=float(settings.improve_max_drawdown_pct),
    )
    client = build_llm_client(settings)
    llm_proposer = LLMProposer(client) if getattr(client, "backend", "null") != "null" else None
    context = ImprovementContext(
        incumbent_config=config,
        incumbent_metrics=metrics,
        incumbent_objective=score.objective,
        iteration=seed % 7,
        seed=seed,
        recent_reflections=[],
        tried_signatures=set(),
    )
    proposal, fallback = propose_with_fallback(
        llm_proposer=llm_proposer,
        heuristic_proposer=HeuristicProposer(),
        ctx=context,
    )
    _print_json(
        {
            "incumbent_objective": score.objective,
            "incumbent_knobs": knob_values(config),
            "proposal": {
                "hypothesis": proposal.hypothesis,
                "change_set": proposal.change_set,
                "proposer": proposal.proposer,
                "model_id": proposal.model_id,
            },
            "fallback_reason": fallback,
        }
    )
    return 0


def _agent_improve(args: argparse.Namespace) -> int:
    _require_fxstack()
    from fxstack.improve.evaluator import load_parquet_dataset
    from fxstack.improve.loop import run_improvement_campaign, run_improvement_loop
    from fxstack.settings import get_settings

    settings = get_settings()
    dataset = load_parquet_dataset(str(args.dataset)) if str(args.dataset).strip() else None
    artifact_dir = str(args.out_dir).strip() or str(
        Path(str(settings.improve_artifact_root)) / "runs" / str(args.run_name)
    )
    iterations = int(args.iterations) if int(args.iterations) > 0 else None
    seed = int(args.seed) if int(args.seed) >= 0 else None
    restarts = max(1, int(args.restarts))

    if str(args.runner) == "graph":
        from fxstack.improve.graph import run_improvement_graph

        _print_json(
            run_improvement_graph(
                dataset=dataset,
                settings=settings,
                seed=seed,
                max_iterations=iterations,
            )
        )
        return 0

    common = {
        "dataset": dataset,
        "settings": settings,
        "iterations": iterations,
        "artifact_dir": artifact_dir,
        "emit_experiment": not bool(args.no_experiment),
        "experiment_id": str(args.experiment_id),
    }
    if restarts > 1:
        result = run_improvement_campaign(restarts=restarts, base_seed=seed, **common)
    else:
        memory_path = str(args.memory).strip() or str(Path(artifact_dir) / "reflection_memory.jsonl")
        result = run_improvement_loop(memory_path=memory_path, seed=seed, **common)
    _print_json(result.as_dict())
    return 0


def _agent_build_dataset(args: argparse.Namespace) -> int:
    _require_fxstack()
    from fxstack.improve.dataset_builder import ColumnMap, build_from_parquet, write_scored_signals

    columns = ColumnMap(
        swing_prob=str(args.swing_col),
        entry_prob=str(args.entry_col),
        trade_prob=str(args.trade_col),
        spread=str(args.spread_col),
        fwd_ret=str(args.fwd_ret_col),
        pair=str(args.pair_col),
        ts=str(args.ts_col),
        expected_edge=str(args.edge_col).strip() or None,
    )
    frame = build_from_parquet(
        str(args.features),
        columns=columns,
        spread_unit=str(args.spread_unit),
        fwd_ret_unit=str(args.fwd_ret_unit),
        edge_scale_bps=float(args.edge_scale_bps),
    )
    _print_json(write_scored_signals(frame, str(args.out)))
    return 0


def _agent_robustness(args: argparse.Namespace) -> int:
    _require_fxstack()
    from fxstack.improve.evaluator import build_synthetic_dataset, load_parquet_dataset
    from fxstack.improve.robustness import robustness_report
    from fxstack.settings import get_settings

    settings = get_settings()
    run_dir = Path(str(args.run_dir))
    best_config_path = run_dir / "best_config.json"
    if not best_config_path.exists():
        _print_json({"error": f"no best_config.json under {run_dir}"})
        return 1
    config = json.loads(best_config_path.read_text(encoding="utf-8"))
    dataset = (
        load_parquet_dataset(str(args.dataset))
        if str(args.dataset).strip()
        else build_synthetic_dataset(
            seed=int(args.seed) if int(args.seed) >= 0 else int(settings.improve_seed)
        )
    )
    _print_json(
        robustness_report(
            config,
            dataset,
            min_trades=int(settings.improve_min_trades),
            max_drawdown_pct=float(settings.improve_max_drawdown_pct),
        )
    )
    return 0


def _agent_explain(args: argparse.Namespace) -> int:
    _require_fxstack()
    from fxstack.improve.explain import explain_run
    from fxstack.settings import get_settings

    run_dir = Path(str(args.run_dir))
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        _print_json({"error": f"no summary.json under {run_dir}"})
        return 1
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    entries: list[dict] = []
    jsonl_path = run_dir / "reflection_memory.jsonl"
    json_path = run_dir / "reflection_memory.json"
    if jsonl_path.exists():
        for raw_line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    elif json_path.exists():
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        entries = list(payload.get("entries") or [])

    _print_json(explain_run(summary=summary, entries=entries, settings=get_settings()))
    return 0


def _agent_verify_weights(args: argparse.Namespace) -> int:
    _require_fxstack()
    from fxstack.llm.weights import load_manifest, verify_manifest

    report = verify_manifest(load_manifest(str(args.manifest)))
    _print_json(report)
    return 0 if report.get("ok") else 2


def _agent_metrics(args: argparse.Namespace) -> int:
    _require_fxstack()
    from fxstack.improve.evaluator import build_synthetic_dataset, load_parquet_dataset
    from fxstack.research.vectorbt_harness import run_vectorbt_research
    from fxstack.settings import get_settings

    settings = get_settings()
    config_path = Path(str(args.run_dir)) / "best_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    dataset = (
        load_parquet_dataset(str(args.dataset))
        if str(args.dataset).strip()
        else build_synthetic_dataset(
            seed=int(args.seed) if int(args.seed) >= 0 else int(settings.improve_seed)
        )
    )
    _print_json(run_vectorbt_research(config, dataset))
    return 0


def _security_secret(args: argparse.Namespace) -> int:
    _require_fxstack()
    if args.value is not None and not args.set:
        _print_json({"error": "--value is valid only with --set"})
        return 2
    from fxstack.security.secrets import SecretStore

    store = SecretStore(directory=str(args.dir).strip() or None)
    if args.set:
        value = str(args.value) if args.value is not None else os.environ.get("FXSTACK_SECRET_VALUE", "")
        if not value:
            _print_json({"error": "provide --value or set FXSTACK_SECRET_VALUE"})
            return 1
        store.set(str(args.set), value)
        _print_json({"ok": True, "action": "set", "name": str(args.set), "backend": store.backend})
        return 0
    if args.get:
        found = store.get(str(args.get))
        _print_json({"name": str(args.get), "present": found is not None})
        return 0 if found is not None else 1
    if args.delete:
        removed = store.delete(str(args.delete))
        _print_json(
            {"ok": True, "action": "delete", "name": str(args.delete), "removed": bool(removed)}
        )
        return 0
    _print_json({"names": store.names(), "backend": store.backend})
    return 0


def _security_validate_offline(args: argparse.Namespace) -> int:
    _require_fxstack()
    from fxstack.security.egress import validate_offline_compose_file
    from fxstack.settings import get_settings

    compose = str(args.compose).strip() or str(
        Path(get_settings().project_root) / "docker" / "docker-compose.offline.yml"
    )
    report = validate_offline_compose_file(compose)
    _print_json(report)
    return 0 if report.get("ok") else 2


def _backtest_export_lean(args: argparse.Namespace) -> int:
    _require_fxstack()
    from fxstack.backtest.harness.lean_codegen import write_lean_project
    from fxstack.settings import get_settings

    settings = get_settings()
    config_path = Path(str(args.run_dir)) / "best_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    pairs = [pair.strip().upper() for pair in str(args.pairs).split(",") if pair.strip()]
    report = write_lean_project(
        config,
        str(args.out),
        pairs=pairs or list(settings.pairs),
        start=str(args.start),
        end=str(args.end),
        cash=float(args.cash),
    )
    _print_json(report)
    return 0


def _add_agent_commands(parent: argparse._SubParsersAction) -> None:
    agent = parent.add_parser("agent", help="Isolated advisory research commands")
    commands = agent.add_subparsers(dest="agent_cmd", required=True)

    improve = commands.add_parser("improve", help="Run the isolated self-improvement loop")
    improve.add_argument("--dataset", default="", help="Scored-signals parquet (default: synthetic)")
    improve.add_argument("--out-dir", default="", help="Advisory artifact destination")
    improve.add_argument("--run-name", default="loop")
    improve.add_argument("--memory", default="", help="Reflection-memory JSONL path")
    improve.add_argument("--iterations", type=int, default=0)
    improve.add_argument("--seed", type=int, default=-1)
    improve.add_argument("--restarts", type=int, default=1)
    improve.add_argument("--runner", choices=["loop", "graph"], default="loop")
    improve.add_argument("--experiment-id", default="")
    improve.add_argument("--no-experiment", action="store_true")
    improve.set_defaults(_fn=_agent_improve)

    propose = commands.add_parser("propose", help="Emit one advisory proposal")
    propose.add_argument("--seed", type=int, default=1729)
    propose.set_defaults(_fn=_agent_propose)

    llm_check = commands.add_parser("llm-check", help="Report local LLM backend health")
    llm_check.set_defaults(_fn=_agent_llm_check)

    explain = commands.add_parser("explain", help="Explain a prior improvement run")
    explain.add_argument("--run-dir", required=True)
    explain.set_defaults(_fn=_agent_explain)

    robustness = commands.add_parser("robustness", help="Measure tuned-config fragility")
    robustness.add_argument("--run-dir", required=True)
    robustness.add_argument("--dataset", default="")
    robustness.add_argument("--seed", type=int, default=-1)
    robustness.set_defaults(_fn=_agent_robustness)

    dataset = commands.add_parser("build-dataset", help="Build the scored-signals research schema")
    dataset.add_argument("--features", required=True)
    dataset.add_argument("--out", required=True)
    dataset.add_argument("--swing-col", default="swing_prob")
    dataset.add_argument("--entry-col", default="entry_prob")
    dataset.add_argument("--trade-col", default="trade_prob")
    dataset.add_argument("--spread-col", default="spread")
    dataset.add_argument("--fwd-ret-col", default="fwd_ret")
    dataset.add_argument("--pair-col", default="pair")
    dataset.add_argument("--ts-col", default="ts")
    dataset.add_argument("--edge-col", default="")
    dataset.add_argument("--spread-unit", choices=["fraction", "bps", "pct"], default="fraction")
    dataset.add_argument("--fwd-ret-unit", choices=["fraction", "bps", "pct"], default="fraction")
    dataset.add_argument("--edge-scale-bps", type=float, default=12.0)
    dataset.set_defaults(_fn=_agent_build_dataset)

    weights = commands.add_parser("verify-weights", help="Verify staged weights by checksum")
    weights.add_argument("--manifest", required=True)
    weights.set_defaults(_fn=_agent_verify_weights)

    metrics = commands.add_parser("metrics", help="Compute research metrics for a tuned config")
    metrics.add_argument("--run-dir", required=True)
    metrics.add_argument("--dataset", default="")
    metrics.add_argument("--seed", type=int, default=-1)
    metrics.set_defaults(_fn=_agent_metrics)


def _add_security_commands(parent: argparse._SubParsersAction) -> None:
    security = parent.add_parser("security", help="Offline secret and air-gap checks")
    commands = security.add_subparsers(dest="security_cmd", required=True)

    secret = commands.add_parser("secret", help="Operate on the local encrypted secret store")
    secret.add_argument("--dir", default="")
    actions = secret.add_mutually_exclusive_group()
    actions.add_argument("--set", metavar="NAME", default="")
    actions.add_argument("--get", metavar="NAME", default="")
    actions.add_argument("--delete", metavar="NAME", default="")
    actions.add_argument("--list", action="store_true", help="List secret names (never values)")
    secret.add_argument("--value", default=None, help="Prefer FXSTACK_SECRET_VALUE on shared shells")
    secret.set_defaults(_fn=_security_secret)

    validate = commands.add_parser("validate-offline", help="Validate offline compose isolation")
    validate.add_argument("--compose", default="")
    validate.set_defaults(_fn=_security_validate_offline)


def _add_backtest_commands(parent: argparse._SubParsersAction) -> None:
    backtest = parent.add_parser("backtest", help="Offline export commands")
    commands = backtest.add_subparsers(dest="backtest_cmd", required=True)
    export = commands.add_parser("export-lean", help="Export a tuned config to a Lean project")
    export.add_argument("--run-dir", required=True)
    export.add_argument("--out", required=True)
    export.add_argument("--pairs", default="")
    export.add_argument("--start", default="2022-01-01")
    export.add_argument("--end", default="2023-01-01")
    export.add_argument("--cash", type=float, default=100000.0)
    export.set_defaults(_fn=_backtest_export_lean)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.trader.cli",
        description="Source-only isolated research and offline security facade.",
        epilog="Runtime, bridge, database, training, activation, and ops commands use focused entrypoints.",
    )
    commands = parser.add_subparsers(dest="cmd", required=True)
    _add_agent_commands(commands)
    _add_security_commands(commands)
    _add_backtest_commands(commands)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    handler: Handler = args._fn
    raise SystemExit(handler(args))


if __name__ == "__main__":
    main()
