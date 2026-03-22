from __future__ import annotations

import time
from typing import Any

from src.trader.infrastructure.runtime_store import RuntimeStore
from src.trader.interfaces.config import TraderConfig
from src.trader.interfaces.dto import ExecutionAck, ExecutionCommand
from src.trader.interfaces.protocol import command_to_mt4_line


class RuntimeService:
    """Application service for command lifecycle, state, and telemetry."""

    def __init__(self, config: TraderConfig) -> None:
        self.config = config
        self.store = RuntimeStore(
            config.runtime_db_path,
            soft_band=(float(config.soft_dd_min), float(config.soft_dd_max)),
            hard_band=(float(config.hard_dd_min), float(config.hard_dd_max)),
            daily_band=(float(config.daily_breaker_min), float(config.daily_breaker_max)),
            sizing_band=(float(config.base_lot), float(config.min_lot), float(config.max_lot)),
        )

    def submit_command(self, payload: dict[str, Any], *, proto: str = "v2") -> tuple[dict[str, Any], int]:
        data = dict(payload or {})
        if "ttl_secs" not in data:
            data["ttl_secs"] = float(self.config.command_ttl_secs)

        cmd = ExecutionCommand.from_payload(
            data,
            default_session_id=self.config.default_session_id,
            proto=proto,
            now_ts=time.time(),
        )
        ok, state = self.store.enqueue_command(cmd)
        if not ok:
            return {
                "status": "duplicate",
                "command_id": cmd.command_id,
                "state": state,
            }, 200

        return {
            "status": "queued",
            "command_id": cmd.command_id,
            "command": cmd.to_dict(),
            "line": command_to_mt4_line(cmd),
        }, 200

    def poll_command(self, *, as_line: bool = False) -> tuple[str | dict[str, Any], int]:
        cmd = self.store.poll_next_command()
        if cmd is None:
            if as_line:
                return "", 200
            return {"status": "empty"}, 200

        line = command_to_mt4_line(cmd)
        if as_line:
            return line, 200

        return {
            "status": "ok",
            "command": cmd.to_dict(),
            "line": line,
        }, 200

    def ack_command(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        ack = ExecutionAck.from_payload(payload, now_ts=time.time())
        return self.store.ack_command(ack)

    def record_tick(self, payload: dict[str, Any]) -> None:
        self.store.record_tick(payload)

    def record_report(self, report_text: str, report_json: dict[str, Any] | None = None) -> None:
        self.store.record_report(report_text, report_json)

    def store_decisions(self, *, decisions: list[dict[str, Any]], vol: float, diagnostics: dict[str, Any]) -> None:
        self.store.store_decisions(decisions=decisions, vol=vol, diagnostics=diagnostics)

    def patch_state(self, patch: dict[str, Any]) -> None:
        self.store.update_state_patch(patch)

    def get_state(self) -> dict[str, Any]:
        return self.store.get_state()

    def get_metrics(self) -> dict[str, Any]:
        return self.store.get_metrics()

    def get_health(self) -> dict[str, Any]:
        return self.store.get_health()

    def get_reports(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.get_reports(limit=limit)

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        return self.store.get_command(command_id)

    def get_commands(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.get_commands(limit=limit)

    def get_command_events(self, *, command_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        return self.store.get_command_events(command_id=command_id, limit=limit)

    def get_governance_events(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.get_governance_events(limit=limit)
