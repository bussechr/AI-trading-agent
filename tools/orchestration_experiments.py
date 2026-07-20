from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))


HELPER_MODULES = (
    "fxstack.orchestration.experiments",
    "fxstack.orchestration.replay",
    "fxstack.training.release_workflow",
)

HELPER_NAMES: dict[str, tuple[str, ...]] = {
    "draft": ("draft_experiment", "draft_orchestration_experiment", "build_draft_pack", "draft"),
    "review": ("review_experiment", "review_orchestration_experiment", "review_pack", "review"),
    "research-replay": ("run_experiment",),
    "paper-pack": ("paper_pack_experiment", "build_paper_pack", "paper_pack"),
    "canary-pack": ("canary_pack_experiment", "build_canary_pack", "canary_pack"),
    "promote": ("promote_experiment", "promote_release", "promote"),
    "trace": ("trace_experiment", "build_trace_pack", "export_trace_pack", "trace"),
}


def _default_experiment_id(command: str) -> str:
    safe = str(command or "experiment").replace("-", "_")
    return f"orchestration_{safe}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"


def _resolve_helper(command: str) -> tuple[Callable[..., Any], str, str]:
    candidates = HELPER_NAMES.get(command, (command.replace("-", "_"),))
    module_names = (
        ("fxstack.orchestration.replay",)
        if command == "research-replay"
        else HELPER_MODULES
    )
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        for helper_name in candidates:
            helper = getattr(module, helper_name, None)
            if callable(helper):
                return helper, module_name, helper_name
    raise RuntimeError(f"unable to resolve orchestration helper for command '{command}'")


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
        try:
            return _jsonable(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return _jsonable({key: item for key, item in vars(value).items() if not key.startswith("_")})
    return str(value)


def _helper_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    kwargs = {
        "config_path": args.config,
        "config": args.config,
        "profile_path": args.config,
        "profile": args.config,
        "experiment_id": args.experiment_id,
        "experimentId": args.experiment_id,
        "window": args.window,
        "window_name": args.window,
        "seed": args.seed,
        "out_dir": args.out_dir,
        "output_root": args.out_dir,
        "pair": getattr(args, "pair", ""),
        "bundle_run_id": getattr(args, "bundle_run_id", ""),
        "bundleRunId": getattr(args, "bundle_run_id", ""),
        "manifest_path": getattr(args, "manifest_path", ""),
        "manifestPath": getattr(args, "manifest_path", ""),
        "promotion_pack_path": getattr(args, "promotion_pack_path", ""),
        "promotionPackPath": getattr(args, "promotion_pack_path", ""),
        "author": getattr(args, "author", ""),
        "note": getattr(args, "note", ""),
        "trace_id": getattr(args, "trace_id", ""),
        "traceId": getattr(args, "trace_id", ""),
        "limit": getattr(args, "limit", None),
    }
    return {key: value for key, value in kwargs.items() if value not in (None, "")}


def _filter_kwargs(helper: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(helper)
    except (TypeError, ValueError):
        return kwargs
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return kwargs
    allowed = set(signature.parameters)
    return {key: value for key, value in kwargs.items() if key in allowed}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage orchestration experiments and run detached, advisory offline research replays."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser, *, research_replay: bool) -> None:
        subparser.add_argument("--config", default=str(REPO_ROOT / "fx-quant-stack" / "config" / "orchestration_replay_profiles.json"))
        subparser.add_argument("--experiment-id", default="")
        subparser.add_argument("--window", choices=["calm", "trend", "shock", "all"], default="all")
        subparser.add_argument("--seed", type=int, default=None)
        subparser.add_argument("--out-dir", default=str(REPO_ROOT / "artifacts" / "orchestration"))
        if research_replay:
            return
        subparser.add_argument("--pair", default="")
        subparser.add_argument("--bundle-run-id", default="")
        subparser.add_argument("--manifest-path", default="")
        subparser.add_argument("--promotion-pack-path", default="")
        subparser.add_argument("--author", default="")
        subparser.add_argument("--note", default="")
        subparser.add_argument("--trace-id", default="")
        subparser.add_argument("--limit", type=int, default=200)

    for command in HELPER_NAMES:
        sub = subparsers.add_parser(command, help=f"{command.replace('-', ' ')} orchestration artifacts")
        add_common(sub, research_replay=command == "research-replay")

    return parser


def _run_command(args: argparse.Namespace) -> dict[str, Any]:
    helper, module_name, helper_name = _resolve_helper(str(args.command))
    kwargs = _filter_kwargs(helper, _helper_kwargs(args))
    result = helper(**kwargs)
    payload = {
        "ok": bool(result.get("ok", True)) if isinstance(result, dict) else True,
        "command": str(args.command),
        "helper": helper_name,
        "module": module_name,
        "result": _jsonable(result),
    }
    if str(args.command) == "research-replay":
        payload.update(
            {
                "advisory_only": True,
                "authorizes_activation": False,
                "runtime_equivalence": "not_assessed",
            }
        )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not str(args.experiment_id or "").strip():
        args.experiment_id = _default_experiment_id(str(args.command))
    payload = _run_command(args)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if bool(payload.get("ok", True)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
