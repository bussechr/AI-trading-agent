from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from fxstack.rl._common import _json_dump
from fxstack.rl.export_replay import ReplayTransition, export_replay_dataset, normalize_replay_transitions
from fxstack.rl.offline_dataset import load_offline_dataset, summarize_offline_dataset


@dataclass(slots=True)
class RLStressScenario:
    name: str
    reward_scale: float = 1.0
    terminal_penalty: float = 0.0
    latency_ms: float = 0.0
    spread_multiplier: float = 1.0
    slippage_multiplier: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_RL_STRESS_SCENARIOS = [
    RLStressScenario(name="base_case"),
    RLStressScenario(name="wide_spread", spread_multiplier=1.5),
    RLStressScenario(name="slippage_shock", slippage_multiplier=2.0),
    RLStressScenario(name="latency_shock", latency_ms=500.0),
    RLStressScenario(name="reward_decay", reward_scale=0.8, terminal_penalty=0.1),
]


def apply_stress_scenario(frame: pd.DataFrame, scenario: RLStressScenario) -> pd.DataFrame:
    out = frame.copy()
    if out.empty:
        return out
    if "reward" in out.columns:
        out["reward"] = out["reward"].astype(float) * float(scenario.reward_scale) - float(scenario.terminal_penalty)
    if "risk_trace_json" in out.columns:
        out["risk_trace_json"] = out["risk_trace_json"].astype(str)
    if "execution_trace_json" in out.columns:
        out["execution_trace_json"] = out["execution_trace_json"].astype(str)
    out["stress_scenario"] = str(scenario.name)
    out["stress_latency_ms"] = float(scenario.latency_ms)
    out["stress_spread_multiplier"] = float(scenario.spread_multiplier)
    out["stress_slippage_multiplier"] = float(scenario.slippage_multiplier)
    return out


def summarize_stress_harness(
    *,
    base_frame: pd.DataFrame,
    stressed_frames: dict[str, pd.DataFrame],
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_summary = summarize_offline_dataset(base_frame, manifest=manifest)
    scenarios = {
        name: summarize_offline_dataset(frame, manifest=manifest)
        for name, frame in sorted(stressed_frames.items())
    }
    return {
        "status": "ok",
        "base": base_summary,
        "scenarios": scenarios,
        "scenario_count": int(len(stressed_frames)),
        "manifest": dict(manifest or {}),
    }


def build_stress_bundle(
    snapshots: dict[str, Any] | Iterable[dict[str, Any]],
    *,
    out_dir: Path,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundle = export_replay_dataset(snapshots, out_dir=out_dir, metadata=metadata)
    frame, manifest = load_offline_dataset(out_dir)
    stressed_reports: dict[str, pd.DataFrame] = {}
    for scenario in DEFAULT_RL_STRESS_SCENARIOS:
        stressed_reports[scenario.name] = apply_stress_scenario(frame, scenario)
    summary = summarize_stress_harness(base_frame=frame, stressed_frames=stressed_reports, manifest=manifest)
    _json_dump(out_dir / "stress_summary.json", summary)
    return {
        "bundle": bundle,
        "summary": summary,
        "stress_summary_path": str(out_dir / "stress_summary.json"),
    }

