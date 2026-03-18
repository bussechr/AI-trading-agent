from __future__ import annotations

from typing import Any

from fxstack.runtime.dto import ExecutionAck, ExecutionCommand
from fxstack.runtime.postgres_store import PostgresRuntimeStore
from fxstack.runtime.protocol import command_to_mt4_line


class RuntimeService:
    def __init__(
        self,
        *,
        database_url: str,
        default_session_id: str = "default",
        command_ttl_secs: float = 120.0,
        requeue_age_secs: float = 90.0,
        db_connect_retries: int = 5,
    ) -> None:
        self.default_session_id = default_session_id
        self.command_ttl_secs = float(command_ttl_secs)
        self.store = PostgresRuntimeStore(
            database_url,
            requeue_age_secs=float(requeue_age_secs),
            connect_retries=int(db_connect_retries),
        )

    def submit_command(self, payload: dict[str, Any], *, proto: str = "v2") -> tuple[dict[str, Any], int]:
        cmd = ExecutionCommand.from_payload(
            dict(payload or {}),
            default_session_id=self.default_session_id,
            ttl_secs=self.command_ttl_secs,
        )
        cmd.proto = str(proto)
        ok, state = self.store.enqueue_command(cmd)
        if not ok:
            return {"status": "duplicate", "command_id": cmd.command_id, "state": state}, 200
        return {
            "status": "queued",
            "command_id": cmd.command_id,
            "command": cmd.to_dict(),
            "line": command_to_mt4_line(cmd),
        }, 200

    def poll_command(self, *, as_line: bool = False) -> tuple[str | dict[str, Any], int]:
        cmd = self.store.poll_next_command()
        if cmd is None:
            return ("", 200) if as_line else ({"status": "empty"}, 200)

        line = command_to_mt4_line(cmd)
        if as_line:
            return line, 200
        return {"status": "ok", "command": cmd.to_dict(), "line": line}, 200

    def ack_command(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        ack = ExecutionAck.from_payload(payload)
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
        tables = self.store.verify_required_tables()
        return {
            "status": "ok" if bool(tables.get("ok")) else "degraded",
            "database": "up" if bool(tables.get("ok")) else "degraded",
            "service": "fxstack-runtime",
            "tables_ok": bool(tables.get("ok")),
            "missing_tables": list(tables.get("missing", []) or []),
        }

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

    def verify_tables(self) -> dict[str, Any]:
        return self.store.verify_required_tables()

    def upsert_active_model_set(
        self,
        *,
        pair: str,
        model_set_id: str,
        registry_path: str,
        artifacts: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> None:
        self.store.upsert_active_model_set(
            pair=pair,
            model_set_id=model_set_id,
            registry_path=registry_path,
            artifacts=artifacts,
            metadata=metadata,
            enabled=enabled,
        )

    def get_active_model_set(self, pair: str) -> dict[str, Any] | None:
        return self.store.get_active_model_set(pair)

    def get_active_model_sets(self, *, enabled_only: bool = True) -> dict[str, dict[str, Any]]:
        return self.store.get_active_model_sets(enabled_only=enabled_only)
