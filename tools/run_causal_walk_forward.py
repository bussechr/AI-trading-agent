from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_ROOT = REPO_ROOT / "fx-quant-stack"
FXSTACK_SRC = FXSTACK_ROOT / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))
if str(REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools"))

from build_walk_forward_snapshot import build_raw_snapshot, build_snapshot  # noqa: E402
from fxstack.features.fx_lifecycle import timeframe_to_timedelta  # noqa: E402
from fxstack.io.parquet_store import ParquetStore  # noqa: E402
from fxstack.training.activation import build_research_manifest  # noqa: E402


TIMEFRAMES = ["M5", "M15", "H1", "H4", "D"]
TRAINED_FEATURE_TIMEFRAMES = ["M5", "H4", "D"]
PRIMARY_HORIZONS = {"M5": 18, "D": 24}
LIFECYCLE_HORIZON = 24


def _utc(value: str) -> pd.Timestamp:
    parsed = pd.Timestamp(value)
    return parsed.tz_localize("UTC") if parsed.tzinfo is None else parsed.tz_convert("UTC")


def _pairs(value: str) -> list[str]:
    out = list(dict.fromkeys(item.strip().upper() for item in str(value).split(",") if item.strip()))
    if not out:
        raise ValueError("at least one pair is required")
    return out


def _window(value: str, index: int) -> dict[str, Any]:
    parts = [item.strip() for item in str(value).split(",")]
    if len(parts) == 3:
        name = f"window_{index:02d}"
        train_end, test_start, test_end = parts
    elif len(parts) == 4:
        name, train_end, test_start, test_end = parts
    else:
        raise ValueError("window must be TRAIN_END,TEST_START,TEST_END or NAME,TRAIN_END,TEST_START,TEST_END")
    train_ts = _utc(train_end)
    test_start_ts = _utc(test_start)
    test_end_ts = _utc(test_end)
    if not train_ts < test_start_ts < test_end_ts:
        raise ValueError(f"invalid causal window ordering: {value}")
    return {
        "name": name,
        "train_end": train_ts,
        "test_start": test_start_ts,
        "test_end": test_end_ts,
    }


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in Path(root).rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, env: dict[str, str]) -> None:
    print(f"[causal-wf] run={' '.join(command)}", flush=True)
    subprocess.run(command, cwd=str(REPO_ROOT), env=env, check=True)


def _status(window: str, stage: str) -> None:
    print(f"[causal-wf] window={window} stage={stage}", flush=True)


def _max_knowledge_ts(
    *,
    root: Path,
    provider: str,
    pairs: list[str],
    timeframes: list[str],
    delays: dict[str, int],
) -> pd.Timestamp:
    maxima: list[pd.Timestamp] = []
    store = ParquetStore(root)
    for pair in pairs:
        for timeframe in timeframes:
            frame = store.read_pair_timeframe(provider=provider, pair=pair, timeframe=timeframe)
            if frame.empty:
                raise RuntimeError(f"causal audit missing rows: {root}:{pair}:{timeframe}")
            timestamps = pd.to_datetime(frame["ts"], utc=True, errors="coerce").dropna()
            if timestamps.empty:
                raise RuntimeError(f"causal audit missing timestamps: {root}:{pair}:{timeframe}")
            maxima.append(
                pd.Timestamp(timestamps.max())
                + (timeframe_to_timedelta(timeframe) * max(1, int(delays[timeframe])))
            )
    return max(maxima)


def _training_data_audit(*, data_root: Path, provider: str, pairs: list[str], cutoff: pd.Timestamp) -> dict[str, Any]:
    raw_max = _max_knowledge_ts(
        root=data_root / "raw",
        provider=provider,
        pairs=pairs,
        timeframes=TIMEFRAMES,
        delays={timeframe: 1 for timeframe in TIMEFRAMES},
    )
    feature_max = _max_knowledge_ts(
        root=data_root / "features",
        provider=provider,
        pairs=pairs,
        timeframes=TRAINED_FEATURE_TIMEFRAMES,
        delays={timeframe: 1 for timeframe in TRAINED_FEATURE_TIMEFRAMES},
    )
    label_max = _max_knowledge_ts(
        root=data_root / "labels",
        provider=provider,
        pairs=pairs,
        timeframes=list(PRIMARY_HORIZONS),
        delays={timeframe: horizon + 1 for timeframe, horizon in PRIMARY_HORIZONS.items()},
    )
    exit_max = _max_knowledge_ts(
        root=data_root / "labels" / "exit",
        provider=provider,
        pairs=pairs,
        timeframes=["M5"],
        delays={"M5": LIFECYCLE_HORIZON + 1},
    )
    reversal_max = _max_knowledge_ts(
        root=data_root / "labels" / "reversal",
        provider=provider,
        pairs=pairs,
        timeframes=["M5"],
        delays={"M5": LIFECYCLE_HORIZON + 1},
    )
    maxima = {
        "raw_max_knowledge_ts": str(raw_max),
        "feature_max_knowledge_ts": str(feature_max),
        "label_max_knowledge_ts": str(label_max),
        "exit_label_max_knowledge_ts": str(exit_max),
        "reversal_label_max_knowledge_ts": str(reversal_max),
    }
    violations = [key for key, value in maxima.items() if _utc(value) > cutoff]
    if violations:
        raise RuntimeError(f"training data crosses cutoff: {','.join(violations)}")
    return {**maxima, "cutoff": str(cutoff), "passed": True}


def _model_audit(manifest_path: Path, *, pairs: list[str], cutoff: pd.Timestamp) -> dict[str, Any]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not bool(payload.get("research_only")) or bool(payload.get("runtime_store_updated", True)):
        raise RuntimeError("walk-forward manifest is not research-only")
    active = dict(payload.get("active_model_sets") or {})
    end_by_pair: dict[str, dict[str, str]] = {}
    for pair in pairs:
        artifacts = dict((active.get(pair) or {}).get("artifacts") or {})
        component_ends: dict[str, str] = {}
        seen_paths: set[str] = set()
        for component, raw_ref in artifacts.items():
            raw_path = (
                str(raw_ref.get("path") or raw_ref.get("artifact_path") or "")
                if isinstance(raw_ref, dict)
                else str(raw_ref or "")
            )
            if not raw_path or raw_path in seen_paths:
                continue
            seen_paths.add(raw_path)
            artifact_path = Path(raw_path)
            if not artifact_path.is_absolute():
                artifact_path = (REPO_ROOT / artifact_path).resolve()
            meta_path = artifact_path / "meta.json" if artifact_path.is_dir() else artifact_path.parent / "meta.json"
            if not meta_path.exists():
                raise RuntimeError(f"model metadata missing for {pair}:{component}: {meta_path}")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            summary = dict(meta.get("training_window_summary") or {})
            end_value = str(summary.get("end_ts") or meta.get("data_window_end") or "")
            end_ts = pd.to_datetime(end_value, utc=True, errors="coerce")
            if pd.isna(end_ts):
                raise RuntimeError(f"model training end missing for {pair}:{component}")
            if pd.Timestamp(end_ts) > cutoff:
                raise RuntimeError(f"model training end crosses cutoff for {pair}:{component}: {end_ts}")
            component_ends[str(component)] = str(end_ts)
        if not component_ends:
            raise RuntimeError(f"no component training windows found for {pair}")
        end_by_pair[pair] = component_ends
    return {"research_only": True, "runtime_store_updated": False, "training_end_by_pair": end_by_pair, "passed": True}


def _replay_audit(
    *,
    aggregate_path: Path,
    decisions_path: Path,
    raw_root: Path,
    provider: str,
    pairs: list[str],
    test_start: pd.Timestamp,
    test_end: pd.Timestamp,
) -> dict[str, Any]:
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    causal = dict(aggregate.get("causal_replay") or {})
    if not bool(causal.get("enabled")) or str(causal.get("future_data_access")) != "forbidden":
        raise RuntimeError("replay did not emit a causal contract")
    if int(causal.get("fill_delay_bars") or 0) < 1:
        raise RuntimeError("replay used a same-bar fill")
    configured_root = Path(str((aggregate.get("data_roots") or {}).get("raw_root") or "")).resolve()
    if configured_root != raw_root.resolve():
        raise RuntimeError(f"replay escaped point-in-time raw root: {configured_root}")
    visible_max = _max_knowledge_ts(
        root=raw_root,
        provider=provider,
        pairs=pairs,
        timeframes=TIMEFRAMES,
        delays={timeframe: 1 for timeframe in TIMEFRAMES},
    )
    if visible_max > test_end:
        raise RuntimeError(f"replay raw snapshot crosses test end: {visible_max}")
    decisions = pd.read_csv(decisions_path)
    source = pd.to_datetime(decisions["decision_source_ts"], utc=True)
    available = pd.to_datetime(decisions["decision_available_ts"], utc=True)
    bar_open = pd.to_datetime(decisions["execution_bar_open_ts"], utc=True)
    fill = pd.to_datetime(decisions["ts"], utc=True)
    if not bool((source < available).all() and (available < fill).all() and (bar_open < fill).all()):
        raise RuntimeError("decision/fill event clock is not strictly causal")
    if fill.min() < test_start or fill.max() > test_end:
        raise RuntimeError(f"replay fills escaped requested window: {fill.min()}..{fill.max()}")
    return {
        "visible_raw_max_knowledge_ts": str(visible_max),
        "decision_rows": int(len(decisions)),
        "decision_source_before_available": bool((source < available).all()),
        "decision_available_before_fill": bool((available < fill).all()),
        "fill_start_ts": str(fill.min()),
        "fill_end_ts": str(fill.max()),
        "trades": int(aggregate.get("trades") or 0),
        "net_pnl_usd": float(aggregate.get("net_pnl_usd") or 0.0),
        "max_drawdown_pct": float(aggregate.get("max_drawdown_pct") or 0.0),
        "passed": True,
    }


def run_window(args: argparse.Namespace, *, window: dict[str, Any], pairs: list[str], root: Path) -> dict[str, Any]:
    name = str(window["name"])
    window_root = root / name
    resume = bool(getattr(args, "resume", False))
    if window_root.exists() and any(window_root.iterdir()) and not resume:
        raise FileExistsError(f"window output must be empty: {window_root}")
    data_root = window_root / "training_data"
    artifact_root = window_root / "artifacts"
    registry_root = window_root / "registry"
    replay_raw_root = window_root / "replay_raw"
    replay_root = window_root / "replays"
    manifest_path = window_root / "research_models.json"
    window_root.mkdir(parents=True, exist_ok=True)

    _status(name, "training_snapshot")
    snapshot_args = argparse.Namespace(
        train_end=str(window["train_end"]),
        test_start=str(window["test_start"]),
        out_root=str(data_root),
        pairs=",".join(pairs),
        provider=args.provider,
        raw_root=str(Path(args.raw_root).resolve()),
        feature_root=str(Path(args.feature_root).resolve()),
        label_root=str(Path(args.label_root).resolve()),
        raw_timeframes=",".join(TIMEFRAMES),
        feature_timeframes=",".join(TRAINED_FEATURE_TIMEFRAMES),
        label_timeframes="M5,D",
        label_horizons="M5=18,D=24",
        lifecycle_horizon_bars=LIFECYCLE_HORIZON,
        reversal_horizon_bars=LIFECYCLE_HORIZON,
    )
    training_snapshot_path = data_root / "walk_forward_snapshot.json"
    if resume and training_snapshot_path.exists():
        training_snapshot = json.loads(training_snapshot_path.read_text(encoding="utf-8"))
        training_snapshot["manifest_path"] = str(training_snapshot_path)
    else:
        training_snapshot = build_snapshot(snapshot_args)
    _status(name, "replay_snapshot")
    replay_snapshot_path = replay_raw_root / "point_in_time_raw_snapshot.json"
    if resume and replay_snapshot_path.exists():
        replay_snapshot = json.loads(replay_snapshot_path.read_text(encoding="utf-8"))
        replay_snapshot["manifest_path"] = str(replay_snapshot_path)
    else:
        replay_snapshot = build_raw_snapshot(
            source_root=Path(args.raw_root),
            output_root=replay_raw_root,
            provider=args.provider,
            pairs=pairs,
            timeframes=TIMEFRAMES,
            cutoff=window["test_end"],
        )
    _status(name, "hash_training_raw_before")
    raw_hash_before = _tree_hash(data_root / "raw")

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(FXSTACK_SRC), str(REPO_ROOT), env.get("PYTHONPATH", "")]).strip(os.pathsep)
    env["FXSTACK_PAIRS"] = ",".join(pairs)
    env["FXSTACK_REQUIRE_CUDA"] = "0"
    env["FXSTACK_MODEL_ACTIVATION_MANIFEST"] = str(manifest_path.resolve())
    registry_complete = all(any(registry_root.glob(f"{pair.lower()}_*.json")) for pair in pairs)
    for pair in ([] if resume and registry_complete else pairs):
        _status(name, f"train_{pair.lower()}")
        _run(
            [
                sys.executable,
                str(FXSTACK_ROOT / "scripts" / "train_all.py"),
                "--pair",
                pair,
                "--swing-timeframe",
                "D",
                "--intraday-timeframe",
                "M5",
                "--regime-timeframe",
                "H4",
                "--raw-root",
                str(data_root / "raw"),
                "--feature-root",
                str(data_root / "features"),
                "--label-root",
                str(data_root / "labels"),
                "--artifact-root",
                str(artifact_root),
                "--registry-root",
                str(registry_root),
                "--training-config",
                str(FXSTACK_ROOT / "configs" / "training.yaml"),
                "--force-retrain",
                "--no-with-belief",
                "--no-allow-ingest",
            ],
            env=env,
        )
    _status(name, "hash_training_raw_after")
    raw_hash_after = _tree_hash(data_root / "raw")
    if raw_hash_after != raw_hash_before:
        raise RuntimeError("training mutated its point-in-time raw snapshot")

    _status(name, "research_manifest_and_audit")
    build_research_manifest(
        registry_root=registry_root,
        manifest_path=manifest_path,
        pairs=pairs,
        metadata={
            "causal_walk_forward": True,
            "train_end": str(window["train_end"]),
            "test_start": str(window["test_start"]),
            "test_end": str(window["test_end"]),
            "training_snapshot_manifest": str(training_snapshot["manifest_path"]),
        },
    )

    training_audit = _training_data_audit(
        data_root=data_root,
        provider=args.provider,
        pairs=pairs,
        cutoff=window["train_end"],
    )
    model_audit = _model_audit(manifest_path, pairs=pairs, cutoff=window["train_end"])
    modes: dict[str, Any] = {}
    for mode in args.mode:
        _status(name, f"replay_{mode}")
        out_dir = replay_root / mode
        command = [
            sys.executable,
            str(REPO_ROOT / "tools" / "fxstack_digital_twin_backtest.py"),
            "--pairs",
            ",".join(pairs),
            "--raw-root",
            str(replay_raw_root),
            "--start-ts",
            str(window["test_start"]),
            "--end-ts",
            str(window["test_end"]),
            "--fill-delay-bars",
            str(args.fill_delay_bars),
            "--exec-mode",
            mode,
            "--no-validate-live-overlap",
            "--emit-decision-history",
            "--out-dir",
            str(out_dir),
        ]
        if not (resume and (out_dir / "aggregate.json").exists() and (out_dir / "decision_history.csv.gz").exists()):
            _run(command, env=env)
        modes[mode] = _replay_audit(
            aggregate_path=out_dir / "aggregate.json",
            decisions_path=out_dir / "decision_history.csv.gz",
            raw_root=replay_raw_root,
            provider=args.provider,
            pairs=pairs,
            test_start=window["test_start"],
            test_end=window["test_end"],
        )

    _status(name, "write_audit")
    audit = {
        "version": "causal_walk_forward_window_v1",
        "name": name,
        "pairs": pairs,
        "timeframes": TIMEFRAMES,
        "train_end": str(window["train_end"]),
        "test_start": str(window["test_start"]),
        "test_end": str(window["test_end"]),
        "embargo_seconds": float((window["test_start"] - window["train_end"]).total_seconds()),
        "future_data_access": "forbidden",
        "runtime_store_updated": False,
        "training_raw_tree_immutable": True,
        "training_snapshot": training_snapshot,
        "replay_snapshot": replay_snapshot,
        "training_data_audit": training_audit,
        "model_audit": model_audit,
        "replays": modes,
        "passed": True,
    }
    audit_path = window_root / "point_in_time_audit.json"
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")
    audit["audit_path"] = str(audit_path)
    return audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train and replay isolated point-in-time walk-forward windows.")
    parser.add_argument("--pairs", required=True)
    parser.add_argument(
        "--window",
        action="append",
        required=True,
        help="TRAIN_END,TEST_START,TEST_END or NAME,TRAIN_END,TEST_START,TEST_END; repeat for multiple windows.",
    )
    parser.add_argument("--out-root", required=True)
    parser.add_argument("--provider", default="dukascopy")
    parser.add_argument("--raw-root", default="fx-quant-stack/data/raw")
    parser.add_argument("--feature-root", default="fx-quant-stack/data/features")
    parser.add_argument("--label-root", default="fx-quant-stack/data/labels")
    parser.add_argument(
        "--mode",
        action="append",
        choices=["strict_live_mirror", "adaptive_multi_playbook"],
        default=None,
    )
    parser.add_argument("--fill-delay-bars", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.mode = list(dict.fromkeys(args.mode or ["strict_live_mirror"]))
    if int(args.fill_delay_bars) < 1:
        raise ValueError("fill_delay_bars must be at least 1")
    pairs = _pairs(args.pairs)
    windows = [_window(value, index + 1) for index, value in enumerate(args.window)]
    root = Path(args.out_root).resolve()
    if root.exists() and any(root.iterdir()) and not bool(args.resume):
        raise FileExistsError(f"walk-forward run output must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    results = [run_window(args, window=window, pairs=pairs, root=root) for window in windows]
    summary = {
        "version": "causal_walk_forward_run_v1",
        "future_data_access": "forbidden",
        "runtime_store_updated": False,
        "pairs": pairs,
        "timeframes": TIMEFRAMES,
        "fill_delay_bars": int(args.fill_delay_bars),
        "windows": results,
        "passed": all(bool(result.get("passed")) for result in results),
    }
    summary_path = root / "causal_walk_forward_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"passed": summary["passed"], "summary": str(summary_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
