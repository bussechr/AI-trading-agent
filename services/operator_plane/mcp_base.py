from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable


JsonHandler = Callable[[dict[str, Any]], dict[str, Any]]
ResourceReader = Callable[[str], dict[str, Any]]
ToolRunner = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ResourceSpec:
    uri: str
    name: str
    title: str
    description: str
    mime_type: str = "application/json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "mimeType": self.mime_type,
        }


@dataclass(frozen=True)
class PromptSpec:
    name: str
    title: str
    description: str
    arguments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "arguments": list(self.arguments),
        }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
            "annotations": {"readOnlyHint": True},
        }


class ReadOnlyMCPServer:
    def __init__(
        self,
        *,
        name: str,
        title: str,
        version: str,
        enabled: bool,
        transport: str,
        resources: list[ResourceSpec],
        prompts: list[PromptSpec],
        tools: list[ToolSpec],
        resource_readers: dict[str, ResourceReader],
        prompt_renderer: Callable[[str, dict[str, Any]], dict[str, Any]],
        tool_runners: dict[str, ToolRunner],
    ) -> None:
        self.name = str(name)
        self.title = str(title)
        self.version = str(version)
        self.enabled = bool(enabled)
        self.transport = str(transport)
        self.resources = list(resources)
        self.prompts = list(prompts)
        self.tools = list(tools)
        self._resource_readers = dict(resource_readers)
        self._prompt_renderer = prompt_renderer
        self._tool_runners = dict(tool_runners)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "enabled": bool(self.enabled),
            "transport": self.transport,
            "resources": [item.to_dict() for item in self.resources],
            "prompts": [item.to_dict() for item in self.prompts],
            "tools": [item.to_dict() for item in self.tools],
        }

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        method = str(request.get("method") or "").strip()
        params = dict(request.get("params") or {})
        request_id = request.get("id")
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"resources": {}, "prompts": {}, "tools": {}},
                "serverInfo": {"name": self.name, "title": self.title, "version": self.version},
            }
        elif method == "resources/list":
            result = {"resources": [item.to_dict() for item in self.resources]}
        elif method == "resources/read":
            uri = str(params.get("uri") or "").strip()
            handler = self._resource_readers.get(uri)
            if handler is None:
                raise KeyError(f"unknown resource uri: {uri}")
            result = {"contents": [handler(uri)]}
        elif method == "prompts/list":
            result = {"prompts": [item.to_dict() for item in self.prompts]}
        elif method == "prompts/get":
            name = str(params.get("name") or "").strip()
            result = self._prompt_renderer(name, params)
        elif method == "tools/list":
            result = {"tools": [item.to_dict() for item in self.tools]}
        elif method == "tools/call":
            name = str(params.get("name") or "").strip()
            arguments = dict(params.get("arguments") or {})
            handler = self._tool_runners.get(name)
            if handler is None:
                raise KeyError(f"unknown tool name: {name}")
            result = {"content": [handler(arguments)]}
        else:
            raise KeyError(f"unsupported method: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def serve_stdio(self) -> int:
        for line in sys.stdin:
            raw = str(line).strip()
            if not raw:
                continue
            request = json.loads(raw)
            response = self.handle_request(request)
            sys.stdout.write(json.dumps(response, sort_keys=True) + "\n")
            sys.stdout.flush()
        return 0


def build_cli(server_factory: Callable[[], ReadOnlyMCPServer]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run or describe a read-only MCP server.")
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--request-json", default="")
    return parser
