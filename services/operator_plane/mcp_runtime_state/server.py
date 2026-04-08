from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from fxstack.settings import get_settings
from services.operator_plane.mcp_base import PromptSpec, ReadOnlyMCPServer, ResourceSpec, ToolSpec, build_cli


FetchJson = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class RuntimeStateServerConfig:
    enabled: bool
    transport: str
    base_url: str
    api_key: str


def default_config() -> RuntimeStateServerConfig:
    settings = get_settings()
    return RuntimeStateServerConfig(
        enabled=bool(settings.mcp_enabled),
        transport=str(settings.mcp_transport or "stdio"),
        base_url=str(settings.mt4_bridge_url).rstrip("/"),
        api_key=str(settings.bridge_api_key or ""),
    )


def _default_fetcher(config: RuntimeStateServerConfig) -> FetchJson:
    headers = {"x-api-key": config.api_key} if str(config.api_key).strip() else {}

    def _fetch(path: str) -> dict[str, Any]:
        response = requests.get(f"{config.base_url}{path}", headers=headers, timeout=3.0)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    return _fetch


class RuntimeStateMCPServer:
    PATH_READY = "/v2/ready"
    PATH_STATE = "/v2/state"
    PATH_SNAPSHOTS = "/v2/decision-snapshots"
    PATH_RUNS = "/v2/orchestration/runs"
    PATH_TRACES = "/v2/orchestration/traces"

    def __init__(self, *, config: RuntimeStateServerConfig | None = None, fetch_json: FetchJson | None = None) -> None:
        self.config = config or default_config()
        self.fetch_json = fetch_json or _default_fetcher(self.config)
        self.allowed_paths = [
            self.PATH_READY,
            self.PATH_STATE,
            self.PATH_SNAPSHOTS,
            self.PATH_RUNS,
            self.PATH_TRACES,
        ]

    def _resource_ready(self, _uri: str) -> dict[str, Any]:
        return {"uri": "runtime://ready", "mimeType": "application/json", "text": json.dumps(self.fetch_json(self.PATH_READY), sort_keys=True)}

    def _resource_state(self, _uri: str) -> dict[str, Any]:
        return {"uri": "runtime://state", "mimeType": "application/json", "text": json.dumps(self.fetch_json(self.PATH_STATE), sort_keys=True)}

    def _resource_snapshots(self, _uri: str) -> dict[str, Any]:
        return {
            "uri": "runtime://decision-snapshots/recent",
            "mimeType": "application/json",
            "text": json.dumps(self.fetch_json(self.PATH_SNAPSHOTS), sort_keys=True),
        }

    def _resource_runs(self, _uri: str) -> dict[str, Any]:
        return {"uri": "runtime://orchestration/runs", "mimeType": "application/json", "text": json.dumps(self.fetch_json(self.PATH_RUNS), sort_keys=True)}

    def _resource_traces(self, _uri: str) -> dict[str, Any]:
        return {"uri": "runtime://orchestration/traces", "mimeType": "application/json", "text": json.dumps(self.fetch_json(self.PATH_TRACES), sort_keys=True)}

    def _resource_health(self, _uri: str) -> dict[str, Any]:
        ready = self.fetch_json(self.PATH_READY)
        state = self.fetch_json(self.PATH_STATE)
        trade_flow = dict(state.get("tradeFlowSummary") or state.get("trade_flow_summary") or {})
        if not trade_flow:
            runtime_diag = dict(state.get("runtime_diag") or {})
            entry_policy = dict(
                state.get("entryExecutionPolicy")
                or state.get("entry_execution_policy")
                or runtime_diag.get("entry_execution_policy")
                or {}
            )
            shadow_policy = dict(state.get("shadowPolicy") or state.get("shadow_policy") or runtime_diag.get("shadow_policy") or {})
            adaptive_shadow_policy = dict(
                state.get("adaptiveShadowPolicy")
                or state.get("adaptive_shadow_policy")
                or runtime_diag.get("adaptive_shadow_policy")
                or {}
            )
            orchestration_live = dict(state.get("orchestrationLive") or state.get("orchestration_live") or {})
            capital_governance = dict(state.get("capital_governance") or state.get("capitalGovernance") or {})
            canary_active = bool(
                capital_governance.get("canary_active", False)
                or orchestration_live.get("enabled", False)
                or orchestration_live.get("runtime_enabled", False)
            )
            trade_flow = {
                "signalsSent": int(state.get("signals_sent", state.get("signalsSent", 0)) or 0),
                "approvedEntryCount": int(entry_policy.get("approved_entry_count", entry_policy.get("approvedEntryCount", 0)) or 0),
                "submittedEntryCount": int(entry_policy.get("submitted_entry_count", entry_policy.get("submittedEntryCount", 0)) or 0),
                "canaryActive": canary_active,
                "canaryStagePct": int(orchestration_live.get("current_stage_pct", orchestration_live.get("currentStagePct", 0)) or 0),
                "canaryHealth": {
                    "featureOnlineReady": bool(state.get("feature_online_ready", state.get("featureOnlineReady", False))),
                    "featureDataFresh": bool(state.get("feature_data_fresh", state.get("featureDataFresh", False))),
                    "featurePushBacklog": int(state.get("feature_push_backlog", state.get("featurePushBacklog", 0)) or 0),
                    "featureBlockerReason": str(state.get("feature_blocker_reason", state.get("featureBlockerReason", "")) or ""),
                },
                "divergenceCounts": {
                    "shadowLiveOnly": int((shadow_policy.get("divergence_counts") or shadow_policy.get("divergenceCounts") or {}).get("liveOnly", 0) or 0),
                    "adaptiveLiveOnly": int((adaptive_shadow_policy.get("divergence_counts") or adaptive_shadow_policy.get("divergenceCounts") or {}).get("liveOnly", 0) or 0),
                    "orchestratorFaultCount": int((state.get("shadow_orchestrator") or state.get("shadowOrchestrator") or {}).get("fault_count", (state.get("shadow_orchestrator") or state.get("shadowOrchestrator") or {}).get("faultCount", 0)) or 0),
                },
            }
        divergence_counts = dict(trade_flow.get("divergenceCounts") or {})
        canary_health = dict(trade_flow.get("canaryHealth") or {})
        divergence_live_only = int(divergence_counts.get("shadowLiveOnly", 0) or 0) + int(
            divergence_counts.get("adaptiveLiveOnly", 0) or 0
        )
        summary = {
            "runtime_status": str(ready.get("runtime_status") or state.get("runtime_status") or ""),
            "runtime_phase": str(ready.get("runtime_phase") or state.get("runtime_phase") or ""),
            "runtime_ready": bool(ready.get("runtime_ready", False)),
            "bridge_up": bool(ready.get("bridge_up", False)),
            "pending_command_count": int(state.get("pending_command_count", 0) or 0),
            "submitted_entry_count": int(state.get("submitted_entry_count", 0) or 0),
            "approved_entry_count": int(state.get("approved_entry_count", 0) or trade_flow.get("approvedEntryCount", 0) or 0),
            "acked_entry_count": int(trade_flow.get("signalsSent", 0) or state.get("signals_sent", 0) or state.get("signalsSent", 0) or 0),
            "last_ack_status": str(trade_flow.get("lastAckStatus") or ""),
            "canary_active": bool(trade_flow.get("canaryActive", False)),
            "canary_stage_pct": int(trade_flow.get("canaryStagePct", 0) or 0),
            "canary_runtime_enabled": bool(trade_flow.get("canaryRuntimeEnabled", True)),
            "canary_queue_kill_active": bool(trade_flow.get("canaryQueueKillActive", False)),
            "divergence_live_only": divergence_live_only,
            "divergence_spike_count": int(divergence_live_only + int(divergence_counts.get("orchestratorFaultCount", 0) or 0)),
            "feature_online_ready": bool(canary_health.get("featureOnlineReady", False)),
            "feature_data_fresh": bool(canary_health.get("featureDataFresh", False)),
            "feature_push_backlog": int(canary_health.get("featurePushBacklog", 0) or 0),
            "feature_blocker_reason": str(canary_health.get("featureBlockerReason") or ""),
        }
        return {"uri": "runtime://health/summary", "mimeType": "application/json", "text": json.dumps(summary, sort_keys=True)}

    def _render_prompt(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(params.get("arguments") or {})
        if name == "runtime-triage":
            pair = str(arguments.get("pair") or "").strip().upper()
            text = (
                "Investigate runtime readiness, recent snapshots, and orchestration traces. "
                f"Focus pair: {pair or 'all'}."
            )
        elif name == "shadow-divergence-review":
            pair = str(arguments.get("pair") or "").strip().upper()
            text = (
                "Review shadow-orchestrator divergence using decision snapshots, orchestration runs, "
                f"and traces for {pair or 'the selected pair set'}."
            )
        else:
            raise KeyError(f"unknown prompt: {name}")
        return {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}

    def _tool_list_recent_snapshots(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(arguments.get("limit", 10) or 10), 200))
        payload = self.fetch_json(self.PATH_SNAPSHOTS)
        items = list(payload.get("items") or [])
        return {"items": items[-limit:], "count": min(len(items), limit)}

    def _tool_get_orchestration_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        run_id = str(arguments.get("run_id") or "").strip()
        items = list((self.fetch_json(self.PATH_RUNS).get("items") or []))
        for item in items:
            if str(item.get("run_id") or "").strip() == run_id:
                return {"item": item}
        return {"item": {}}

    def _tool_get_orchestration_trace(self, arguments: dict[str, Any]) -> dict[str, Any]:
        trace_id = str(arguments.get("trace_id") or "").strip()
        run_id = str(arguments.get("run_id") or "").strip()
        items = list((self.fetch_json(self.PATH_TRACES).get("items") or []))
        for item in items:
            if trace_id and str(item.get("trace_id") or "").strip() == trace_id:
                return {"item": item}
            if run_id and str(item.get("run_id") or "").strip() == run_id:
                return {"item": item}
        return {"item": {}}

    def build_server(self) -> ReadOnlyMCPServer:
        return ReadOnlyMCPServer(
            name="mcp_runtime_state",
            title="Runtime State MCP",
            version="phase5.v1",
            enabled=bool(self.config.enabled),
            transport=str(self.config.transport or "stdio"),
            resources=[
                ResourceSpec("runtime://ready", "runtime.ready", "Runtime Ready", "Read the current /v2/ready payload."),
                ResourceSpec("runtime://state", "runtime.state", "Runtime State", "Read the current /v2/state payload."),
                ResourceSpec(
                    "runtime://decision-snapshots/recent",
                    "runtime.snapshots.recent",
                    "Recent Decision Snapshots",
                    "Read the recent decision-snapshot payload.",
                ),
                ResourceSpec(
                    "runtime://orchestration/runs",
                    "runtime.orchestration.runs",
                    "Recent Orchestration Runs",
                    "Read the recent orchestration runs payload.",
                ),
                ResourceSpec(
                    "runtime://orchestration/traces",
                    "runtime.orchestration.traces",
                    "Recent Orchestration Traces",
                    "Read the recent orchestration traces payload.",
                ),
                ResourceSpec(
                    "runtime://health/summary",
                    "runtime.health.summary",
                    "Runtime Health Summary",
                    "Read a normalized runtime readiness summary.",
                ),
            ],
            prompts=[
                PromptSpec(
                    "runtime-triage",
                    "Runtime Triage",
                    "Guide an operator through runtime readiness and state triage.",
                    arguments=[{"name": "pair", "required": False}],
                ),
                PromptSpec(
                    "shadow-divergence-review",
                    "Shadow Divergence Review",
                    "Guide an operator through shadow divergence analysis.",
                    arguments=[{"name": "pair", "required": False}],
                ),
            ],
            tools=[
                ToolSpec(
                    "list_recent_snapshots",
                    "List Recent Snapshots",
                    "Return a bounded subset of recent decision snapshots.",
                    {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}}},
                ),
                ToolSpec(
                    "get_orchestration_run",
                    "Get Orchestration Run",
                    "Lookup one orchestration run by run_id.",
                    {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
                ),
                ToolSpec(
                    "get_orchestration_trace",
                    "Get Orchestration Trace",
                    "Lookup one orchestration trace by trace_id or run_id.",
                    {
                        "type": "object",
                        "properties": {"trace_id": {"type": "string"}, "run_id": {"type": "string"}},
                    },
                ),
            ],
            resource_readers={
                "runtime://ready": self._resource_ready,
                "runtime://state": self._resource_state,
                "runtime://decision-snapshots/recent": self._resource_snapshots,
                "runtime://orchestration/runs": self._resource_runs,
                "runtime://orchestration/traces": self._resource_traces,
                "runtime://health/summary": self._resource_health,
            },
            prompt_renderer=self._render_prompt,
            tool_runners={
                "list_recent_snapshots": self._tool_list_recent_snapshots,
                "get_orchestration_run": self._tool_get_orchestration_run,
                "get_orchestration_trace": self._tool_get_orchestration_trace,
            },
        )


def main(argv: list[str] | None = None) -> int:
    server = RuntimeStateMCPServer().build_server()
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
