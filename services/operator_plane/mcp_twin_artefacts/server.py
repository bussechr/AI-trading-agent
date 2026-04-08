from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fxstack.settings import get_settings
from services.operator_plane.common import list_dir_names, read_json, read_text, repo_root
from services.operator_plane.mcp_base import PromptSpec, ReadOnlyMCPServer, ResourceSpec, ToolSpec, build_cli


@dataclass(frozen=True)
class TwinArtefactsServerConfig:
    enabled: bool
    transport: str
    artifacts_root: Path


def default_config() -> TwinArtefactsServerConfig:
    root = repo_root()
    settings = get_settings()
    return TwinArtefactsServerConfig(
        enabled=bool(settings.mcp_enabled),
        transport=str(settings.mcp_transport or "stdio"),
        artifacts_root=(root / "artifacts" / "orchestration").resolve(),
    )


class TwinArtefactsMCPServer:
    def __init__(self, *, config: TwinArtefactsServerConfig | None = None) -> None:
        self.config = config or default_config()

    def _bundle_dir(self, experiment_id: str, window: str) -> Path:
        return self.config.artifacts_root / str(experiment_id) / str(window)

    def _bundle_summary(self, experiment_id: str, window: str) -> dict[str, Any]:
        bundle_dir = self._bundle_dir(experiment_id, window)
        aggregate = read_json(bundle_dir / "aggregate.json")
        guardrails = read_json(bundle_dir / "guardrails.json")
        return {
            "experiment_id": experiment_id,
            "window": window,
            "bundle_dir": str(bundle_dir),
            "exists": bool(bundle_dir.exists()),
            "window_status": str((aggregate.get("window_status") or {}).get("status") or ""),
            "comparable_cycle_count": int((aggregate.get("comparison") or {}).get("comparable_cycle_count") or 0),
            "guardrail_checks": sorted(list((guardrails.get("checks") or {}).keys())),
            "aggregate_path": str(bundle_dir / "aggregate.json"),
            "guardrails_path": str(bundle_dir / "guardrails.json"),
            "promotion_pack_path": str(bundle_dir / "promotion_pack.md"),
        }

    def _resource_index(self, _uri: str) -> dict[str, Any]:
        experiments = []
        for experiment_id in list_dir_names(self.config.artifacts_root):
            experiment_dir = self.config.artifacts_root / experiment_id
            experiments.append({"experiment_id": experiment_id, "windows": list_dir_names(experiment_dir)})
        return {"uri": "twin://orchestration/index", "mimeType": "application/json", "text": json.dumps({"items": experiments}, sort_keys=True)}

    def _resource_summary(self, _uri: str) -> dict[str, Any]:
        experiments = []
        for experiment_id in list_dir_names(self.config.artifacts_root):
            experiment_dir = self.config.artifacts_root / experiment_id
            summary = read_json(experiment_dir / "experiment_summary.json")
            experiments.append({"experiment_id": experiment_id, "summary": summary})
        return {"uri": "twin://orchestration/summary", "mimeType": "application/json", "text": json.dumps({"items": experiments}, sort_keys=True)}

    def _resource_bundles(self, _uri: str) -> dict[str, Any]:
        items = []
        for experiment_id in list_dir_names(self.config.artifacts_root):
            for window in list_dir_names(self.config.artifacts_root / experiment_id):
                items.append(self._bundle_summary(experiment_id, window))
        return {"uri": "twin://orchestration/bundles/index", "mimeType": "application/json", "text": json.dumps({"items": items}, sort_keys=True)}

    def _render_prompt(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(params.get("arguments") or {})
        experiment_id = str(arguments.get("experiment_id") or "").strip()
        window = str(arguments.get("window") or "").strip()
        if name == "replay-analysis":
            text = f"Review orchestration replay evidence for experiment {experiment_id or '<latest>'} and window {window or '<all>'}."
        elif name == "divergence-review":
            text = f"Review divergence.csv, guardrails, and proposal votes for experiment {experiment_id or '<latest>'} and window {window or '<all>'}."
        elif name == "bundle-review":
            text = f"Review the experiment bundle, guardrails, and promotion pack for experiment {experiment_id or '<latest>'} and window {window or '<all>'}."
        else:
            raise KeyError(f"unknown prompt: {name}")
        return {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}

    def _tool_list_experiments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(arguments.get("limit", 20) or 20), 200))
        items = []
        for experiment_id in list_dir_names(self.config.artifacts_root):
            experiment_dir = self.config.artifacts_root / experiment_id
            items.append({"experiment_id": experiment_id, "windows": list_dir_names(experiment_dir)})
        return {"items": items[:limit]}

    def _artifact_path(self, experiment_id: str, window: str, artifact_name: str) -> Path:
        safe_name = Path(str(artifact_name)).name
        return self.config.artifacts_root / str(experiment_id) / str(window) / safe_name

    def _tool_read_artifact_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        experiment_id = str(arguments.get("experiment_id") or "").strip()
        window = str(arguments.get("window") or "").strip()
        artifact_name = str(arguments.get("artifact_name") or "").strip()
        path = self._artifact_path(experiment_id, window, artifact_name)
        if path.suffix.lower() == ".json":
            payload: Any = read_json(path)
        else:
            payload = read_text(path)
        return {"path": str(path), "content": payload}

    def _tool_summarize_window(self, arguments: dict[str, Any]) -> dict[str, Any]:
        experiment_id = str(arguments.get("experiment_id") or "").strip()
        window = str(arguments.get("window") or "").strip()
        window_dir = self.config.artifacts_root / experiment_id / window
        aggregate = read_json(window_dir / "aggregate.json")
        guardrails = read_json(window_dir / "guardrails.json")
        return {
            "experiment_id": experiment_id,
            "window": window,
            "status": str((aggregate.get("window_status") or {}).get("status") or ""),
            "comparable_cycle_count": int((aggregate.get("comparison") or {}).get("comparable_cycle_count") or 0),
            "guardrail_checks": sorted(list((guardrails.get("checks") or {}).keys())),
        }

    def _tool_list_experiment_bundles(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(arguments.get("limit", 20) or 20), 500))
        items = []
        for experiment_id in list_dir_names(self.config.artifacts_root):
            for window in list_dir_names(self.config.artifacts_root / experiment_id):
                items.append(self._bundle_summary(experiment_id, window))
        return {"items": items[:limit]}

    def _tool_read_experiment_bundle(self, arguments: dict[str, Any]) -> dict[str, Any]:
        experiment_id = str(arguments.get("experiment_id") or "").strip()
        window = str(arguments.get("window") or "").strip()
        bundle_dir = self._bundle_dir(experiment_id, window)
        bundle = self._bundle_summary(experiment_id, window)
        bundle["aggregate"] = read_json(bundle_dir / "aggregate.json")
        bundle["guardrails"] = read_json(bundle_dir / "guardrails.json")
        bundle["promotion_pack"] = read_text(bundle_dir / "promotion_pack.md")
        return bundle

    def build_server(self) -> ReadOnlyMCPServer:
        return ReadOnlyMCPServer(
            name="mcp_twin_artefacts",
            title="Twin Artefacts MCP",
            version="phase5.v1",
            enabled=bool(self.config.enabled),
            transport=str(self.config.transport or "stdio"),
            resources=[
                ResourceSpec(
                    "twin://orchestration/index",
                    "twin.orchestration.index",
                    "Replay Experiment Index",
                    "List orchestration replay experiments and windows.",
                ),
                ResourceSpec(
                    "twin://orchestration/summary",
                    "twin.orchestration.summary",
                    "Replay Experiment Summary",
                    "List orchestration replay experiment summaries.",
                ),
                ResourceSpec(
                    "twin://orchestration/bundles/index",
                    "twin.orchestration.bundles.index",
                    "Experiment Bundle Index",
                    "List bundle-level experiment evidence with guardrails and promotion pack visibility.",
                ),
            ],
            prompts=[
                PromptSpec(
                    "replay-analysis",
                    "Replay Analysis",
                    "Guide replay inspection for one experiment and window.",
                    arguments=[{"name": "experiment_id", "required": False}, {"name": "window", "required": False}],
                ),
                PromptSpec(
                    "divergence-review",
                    "Divergence Review",
                    "Guide divergence review for one experiment and window.",
                    arguments=[{"name": "experiment_id", "required": False}, {"name": "window", "required": False}],
                ),
                PromptSpec(
                    "bundle-review",
                    "Bundle Review",
                    "Guide review of a complete experiment bundle and promotion pack.",
                    arguments=[{"name": "experiment_id", "required": False}, {"name": "window", "required": False}],
                ),
            ],
            tools=[
                ToolSpec(
                    "list_experiments",
                    "List Experiments",
                    "Return orchestration replay experiments and windows.",
                    {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}}},
                ),
                ToolSpec(
                    "read_artifact_file",
                    "Read Artifact File",
                    "Read one replay artifact file by experiment, window, and filename.",
                    {
                        "type": "object",
                        "properties": {
                            "experiment_id": {"type": "string"},
                            "window": {"type": "string"},
                            "artifact_name": {"type": "string"},
                        },
                        "required": ["experiment_id", "window", "artifact_name"],
                    },
                ),
                ToolSpec(
                    "summarize_window",
                    "Summarize Window",
                    "Return a bounded summary of one replay window.",
                    {
                        "type": "object",
                        "properties": {"experiment_id": {"type": "string"}, "window": {"type": "string"}},
                        "required": ["experiment_id", "window"],
                    },
                ),
                ToolSpec(
                    "list_experiment_bundles",
                    "List Experiment Bundles",
                    "Return bundle-level experiment evidence across all windows.",
                    {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 500}}},
                ),
                ToolSpec(
                    "read_experiment_bundle",
                    "Read Experiment Bundle",
                    "Read one experiment bundle and its evidence files.",
                    {
                        "type": "object",
                        "properties": {
                            "experiment_id": {"type": "string"},
                            "window": {"type": "string"},
                        },
                        "required": ["experiment_id", "window"],
                    },
                ),
            ],
            resource_readers={
                "twin://orchestration/index": self._resource_index,
                "twin://orchestration/summary": self._resource_summary,
                "twin://orchestration/bundles/index": self._resource_bundles,
            },
            prompt_renderer=self._render_prompt,
            tool_runners={
                "list_experiments": self._tool_list_experiments,
                "read_artifact_file": self._tool_read_artifact_file,
                "summarize_window": self._tool_summarize_window,
                "list_experiment_bundles": self._tool_list_experiment_bundles,
                "read_experiment_bundle": self._tool_read_experiment_bundle,
            },
        )


def main(argv: list[str] | None = None) -> int:
    server = TwinArtefactsMCPServer().build_server()
    parser = build_cli(lambda: server)
    args = parser.parse_args(argv)
    if args.describe:
        print(json.dumps(server.describe(), indent=2, sort_keys=True))
        return 0
    if str(args.request_json).strip():
        response = server.handle_request(json.loads(str(args.request_json)))
        print(json.dumps(response, indent=2, sort_keys=True))
        return 0
    return server.serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
