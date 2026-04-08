from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fxstack.settings import get_settings
from services.operator_plane.common import list_dir_names, read_json, repo_root
from services.operator_plane.mcp_base import PromptSpec, ReadOnlyMCPServer, ResourceSpec, ToolSpec, build_cli


@dataclass(frozen=True)
class ReleaseRegistryServerConfig:
    enabled: bool
    transport: str
    manifest_path: Path
    registry_root: Path
    release_root: Path


def default_config() -> ReleaseRegistryServerConfig:
    settings = get_settings()
    root = repo_root()
    return ReleaseRegistryServerConfig(
        enabled=bool(settings.mcp_enabled),
        transport=str(settings.mcp_transport or "stdio"),
        manifest_path=(root / str(settings.model_activation_manifest)).resolve(),
        registry_root=(root / str(settings.registry_root)).resolve(),
        release_root=(root / str(settings.phase5_release_root)).resolve(),
    )


class ReleaseRegistryMCPServer:
    def __init__(self, *, config: ReleaseRegistryServerConfig | None = None) -> None:
        self.config = config or default_config()

    def _promotion_ledger_root(self) -> Path:
        return (self.config.manifest_path.parent.parent).resolve()

    def _promotion_ledger_entries(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        root = self._promotion_ledger_root()
        if root.exists():
            for path in sorted(root.rglob("reports/promotion_decision.json")):
                pair = path.parent.parent.name.upper()
                report = read_json(path)
                items.append(
                    {
                        "pair": pair,
                        "report_path": str(path),
                        "status": str(report.get("status") or ""),
                        "policy": str(report.get("policy") or ""),
                        "delta": float(report.get("delta") or 0.0),
                        "gates": sorted(list((dict(report.get("gates") or {})).keys())),
                    }
                )
        return items

    def _resource_manifest(self, _uri: str) -> dict[str, Any]:
        return {
            "uri": "release://active-manifest",
            "mimeType": "application/json",
            "text": json.dumps(read_json(self.config.manifest_path), sort_keys=True),
        }

    def _resource_registry(self, _uri: str) -> dict[str, Any]:
        items = []
        if self.config.registry_root.exists():
            for path in sorted(self.config.registry_root.glob("*.json")):
                items.append({"path": str(path), "name": path.name})
        return {"uri": "release://registry/index", "mimeType": "application/json", "text": json.dumps({"items": items}, sort_keys=True)}

    def _resource_candidates(self, _uri: str) -> dict[str, Any]:
        items = []
        if self.config.release_root.exists():
            for pair_dir in sorted(path for path in self.config.release_root.iterdir() if path.is_dir()):
                items.append({"pair": pair_dir.name, "bundles": list_dir_names(pair_dir)})
        return {"uri": "release://candidates/index", "mimeType": "application/json", "text": json.dumps({"items": items}, sort_keys=True)}

    def _resource_approval_refs(self, _uri: str) -> dict[str, Any]:
        items = []
        if self.config.release_root.exists():
            for path in sorted(self.config.release_root.rglob("*.md")):
                items.append(str(path))
        return {"uri": "release://approval-packs/index", "mimeType": "application/json", "text": json.dumps({"items": items}, sort_keys=True)}

    def _resource_promotion_ledger(self, _uri: str) -> dict[str, Any]:
        return {
            "uri": "release://promotion-ledger/index",
            "mimeType": "application/json",
            "text": json.dumps({"items": self._promotion_ledger_entries()}, sort_keys=True),
        }

    def _render_prompt(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(params.get("arguments") or {})
        pair = str(arguments.get("pair") or "").strip().upper()
        if name == "release-inspection":
            text = f"Inspect active-model manifest, registry entries, and release candidates for {pair or 'all pairs'}."
        elif name == "approval-pack-draft":
            text = f"Draft an approval-pack review checklist for {pair or 'the selected release candidate'}."
        elif name == "promotion-ledger-review":
            text = f"Review promotion decision ledger entries, gate status, and deltas for {pair or 'all pairs'}."
        else:
            raise KeyError(f"unknown prompt: {name}")
        return {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}

    def _tool_resolve_active_model_set(self, arguments: dict[str, Any]) -> dict[str, Any]:
        pair = str(arguments.get("pair") or "").strip().upper()
        manifest = read_json(self.config.manifest_path)
        active = dict(manifest.get("active_model_sets") or {})
        return {"pair": pair, "item": dict(active.get(pair) or {})}

    def _tool_list_release_candidates(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(arguments.get("limit", 50) or 50), 500))
        items = []
        if self.config.release_root.exists():
            for path in sorted(self.config.release_root.rglob("*.json")):
                items.append(str(path))
        return {"items": items[:limit]}

    def _tool_inspect_manifest_consistency(self, arguments: dict[str, Any]) -> dict[str, Any]:
        manifest = read_json(self.config.manifest_path)
        active = dict(manifest.get("active_model_sets") or {})
        missing_pairs = []
        registry_hits = {}
        for pair, payload in sorted(active.items()):
            registry_path = Path(str((dict(payload or {})).get("registry_path") or "").strip())
            exists = registry_path.exists() if registry_path.is_absolute() else (self.config.registry_root / registry_path.name).exists()
            registry_hits[str(pair).upper()] = bool(exists)
            if not exists:
                missing_pairs.append(str(pair).upper())
        return {"missing_pairs": missing_pairs, "registry_hits": registry_hits}

    def _tool_list_promotion_ledger_entries(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(arguments.get("limit", 50) or 50), 500))
        pair = str(arguments.get("pair") or "").strip().upper()
        items = self._promotion_ledger_entries()
        if pair:
            items = [item for item in items if str(item.get("pair") or "").strip().upper() == pair]
        return {"items": items[:limit]}

    def _tool_read_promotion_ledger_entry(self, arguments: dict[str, Any]) -> dict[str, Any]:
        pair = str(arguments.get("pair") or "").strip().upper()
        for item in self._promotion_ledger_entries():
            if str(item.get("pair") or "").strip().upper() == pair:
                return {"item": item}
        return {"item": {}}

    def build_server(self) -> ReadOnlyMCPServer:
        return ReadOnlyMCPServer(
            name="mcp_release_registry",
            title="Release Registry MCP",
            version="phase5.v1",
            enabled=bool(self.config.enabled),
            transport=str(self.config.transport or "stdio"),
            resources=[
                ResourceSpec(
                    "release://active-manifest",
                    "release.active_manifest",
                    "Active Model Manifest",
                    "Read the active model manifest.",
                ),
                ResourceSpec(
                    "release://registry/index",
                    "release.registry_index",
                    "Registry Index",
                    "List registry bundle metadata files.",
                ),
                ResourceSpec(
                    "release://candidates/index",
                    "release.candidates_index",
                    "Release Candidate Index",
                    "List release candidates under the release root.",
                ),
                ResourceSpec(
                    "release://approval-packs/index",
                    "release.approval_pack_index",
                    "Approval Pack Index",
                    "List markdown approval and release-note artefacts.",
                ),
                ResourceSpec(
                    "release://promotion-ledger/index",
                    "release.promotion_ledger_index",
                    "Promotion Ledger Index",
                    "List promotion decision report entries for visibility into gate outcomes.",
                ),
            ],
            prompts=[
                PromptSpec(
                    "release-inspection",
                    "Release Inspection",
                    "Guide an operator through release-manifest inspection.",
                    arguments=[{"name": "pair", "required": False}],
                ),
                PromptSpec(
                    "approval-pack-draft",
                    "Approval Pack Draft",
                    "Guide an operator through approval-pack drafting.",
                    arguments=[{"name": "pair", "required": False}],
                ),
                PromptSpec(
                    "promotion-ledger-review",
                    "Promotion Ledger Review",
                    "Guide review of promotion decision ledger entries and gate outcomes.",
                    arguments=[{"name": "pair", "required": False}],
                ),
            ],
            tools=[
                ToolSpec(
                    "resolve_active_model_set",
                    "Resolve Active Model Set",
                    "Resolve one active model-set entry from the active manifest.",
                    {"type": "object", "properties": {"pair": {"type": "string"}}, "required": ["pair"]},
                ),
                ToolSpec(
                    "list_release_candidates",
                    "List Release Candidates",
                    "List release candidate artefacts under the release root.",
                    {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 500}}},
                ),
                ToolSpec(
                    "inspect_manifest_consistency",
                    "Inspect Manifest Consistency",
                    "Check whether active model-set registry paths resolve to registry entries.",
                    {"type": "object", "properties": {}},
                ),
                ToolSpec(
                    "list_promotion_ledger_entries",
                    "List Promotion Ledger Entries",
                    "List promotion decision report entries with gate outcomes and deltas.",
                    {
                        "type": "object",
                        "properties": {
                            "pair": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                        },
                    },
                ),
                ToolSpec(
                    "read_promotion_ledger_entry",
                    "Read Promotion Ledger Entry",
                    "Read one promotion decision report entry by pair.",
                    {"type": "object", "properties": {"pair": {"type": "string"}}, "required": ["pair"]},
                ),
            ],
            resource_readers={
                "release://active-manifest": self._resource_manifest,
                "release://registry/index": self._resource_registry,
                "release://candidates/index": self._resource_candidates,
                "release://approval-packs/index": self._resource_approval_refs,
                "release://promotion-ledger/index": self._resource_promotion_ledger,
            },
            prompt_renderer=self._render_prompt,
            tool_runners={
                "resolve_active_model_set": self._tool_resolve_active_model_set,
                "list_release_candidates": self._tool_list_release_candidates,
                "inspect_manifest_consistency": self._tool_inspect_manifest_consistency,
                "list_promotion_ledger_entries": self._tool_list_promotion_ledger_entries,
                "read_promotion_ledger_entry": self._tool_read_promotion_ledger_entry,
            },
        )


def main(argv: list[str] | None = None) -> int:
    server = ReleaseRegistryMCPServer().build_server()
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
