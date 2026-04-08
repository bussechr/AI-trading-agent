# AGENT: ROLE: Thin runtime facade for command queue, state patching, report ingest, and decision persistence.
# AGENT: ENTRYPOINT: imported by runtime loop and bridge API handlers.
# AGENT: PRIMARY INPUTS: execution payloads, ACK payloads, state patches, decision lists, governance events.
# AGENT: PRIMARY OUTPUTS: queued commands, DB-backed state updates, ACK state transitions.
# AGENT: DEPENDS ON: `fxstack/runtime/postgres_store.py`, `fxstack/runtime/protocol.py`, `fxstack/runtime/dto.py`.
# AGENT: CALLED BY: `fxstack/runtime/runner.py`, `fxstack/api/app.py`.
# AGENT: STATE / SIDE EFFECTS: mutates command queue tables, runtime state rows, reports, ticks, governance events.
# AGENT: HANDSHAKES: MT4 command queue submit/poll/ack, runtime state patch path, dashboard-visible decision persistence.
# AGENT: SEE: `docs/agents/runtime-loop.md` -> `fxstack/runtime/postgres_store.py` -> `docs/agents/bridge-and-api-handshakes.md`
from __future__ import annotations

import hashlib
import json
from typing import Any

from fxstack.providers.execution.paper import build_simulated_ack_payloads
from fxstack.runtime.dto import ExecutionAck, ExecutionCommand
from fxstack.runtime.postgres_store import PostgresRuntimeStore
from fxstack.runtime.protocol import command_to_provider_line
from fxstack.settings import get_settings


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _derive_direct_command_idempotency_key(*, payload: dict[str, Any], default_session_id: str) -> str:
    material = {
        "session_id": str(payload.get("session_id") or default_session_id or ""),
        "cmd": str(payload.get("cmd") or "").upper(),
        "symbol": str(payload.get("symbol") or "").upper(),
        "lots": _safe_float(payload.get("lots")),
        "close_lots": _safe_float(payload.get("close_lots")),
        "tp_cash": payload.get("tp_cash"),
        "tp_price": payload.get("tp_price"),
        "sl_price": payload.get("sl_price"),
        "magic": payload.get("magic"),
        "intent": str(payload.get("intent") or ""),
        "action": str(payload.get("action") or ""),
        "reversal_token": str(payload.get("reversal_token") or ""),
        "position_id": str(payload.get("position_id") or ""),
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


class RuntimeService:
    def __init__(
        self,
        *,
        database_url: str,
        default_session_id: str = "default",
        command_ttl_secs: float = 120.0,
        requeue_age_secs: float = 90.0,
        db_connect_retries: int = 5,
        execution_provider: str = "",
    ) -> None:
        self.default_session_id = default_session_id
        self.command_ttl_secs = float(command_ttl_secs)
        self.execution_provider = str(execution_provider or get_settings().normalized_execution_provider)
        self.store = PostgresRuntimeStore(
            database_url,
            requeue_age_secs=float(requeue_age_secs),
            connect_retries=int(db_connect_retries),
        )

    # AGENT HANDSHAKE: `submit_command` is the only place that turns high-level runtime payloads into validated queue records plus MT4 wire lines.
    def submit_command(self, payload: dict[str, Any], *, proto: str = "v2") -> tuple[dict[str, Any], int]:
        raw_payload = dict(payload or {})
        if (
            not str(raw_payload.get("command_id") or raw_payload.get("signal_id") or "").strip()
            and not str(raw_payload.get("idempotency_key") or "").strip()
        ):
            raw_payload["idempotency_key"] = _derive_direct_command_idempotency_key(
                payload=raw_payload,
                default_session_id=self.default_session_id,
            )
        try:
            cmd = ExecutionCommand.from_payload(
                raw_payload,
                default_session_id=self.default_session_id,
                ttl_secs=self.command_ttl_secs,
            )
        except ValueError as exc:
            return {"status": "invalid", "error": str(exc), "payload": raw_payload}, 400
        cmd.proto = str(proto)
        try:
            line = command_to_provider_line(cmd, provider=self.execution_provider)
        except ValueError as exc:
            return {
                "status": "invalid",
                "error": str(exc),
                "execution_provider": str(self.execution_provider),
                "command": cmd.to_dict(),
            }, 400
        ok, state = self.store.enqueue_command(cmd)
        if not ok:
            existing = None
            if str(cmd.idempotency_key or "").strip():
                existing = self.store.get_active_command_by_idempotency_key(cmd.idempotency_key)
            if existing is None:
                existing = self.store.get_command(cmd.command_id)
            duplicate_command_id = str((existing or {}).get("command_id") or cmd.command_id)
            return {"status": "duplicate", "command_id": duplicate_command_id, "state": state}, 200
        paper_execution: dict[str, Any] | None = None
        if str(self.execution_provider).strip().lower() == "paper":
            paper_execution = self._simulate_paper_execution(cmd)
        return {
            "status": "queued",
            "command_id": cmd.command_id,
            "execution_provider": str(self.execution_provider),
            "command": cmd.to_dict(),
            "line": line,
            **({"paper_execution": paper_execution} if paper_execution is not None else {}),
        }, 200

    # AGENT HANDSHAKE: MT4 polls through this method; queue state and duplicate suppression live in the store layer below.
    def poll_command(self, *, as_line: bool = False) -> tuple[str | dict[str, Any], int]:
        provider_name = str(self.execution_provider).strip().lower()
        if provider_name == "paper":
            return ("", 200) if as_line else ({"status": "empty", "execution_provider": "paper"}, 200)
        if provider_name not in {"mt4"}:
            error = f"unsupported execution provider: {self.execution_provider}"
            return ("", 400) if as_line else ({"status": "invalid", "error": error, "execution_provider": str(self.execution_provider)}, 400)
        cmd = self.store.poll_next_command()
        if cmd is None:
            return ("", 200) if as_line else ({"status": "empty"}, 200)

        line = command_to_provider_line(cmd, provider=self.execution_provider)
        if as_line:
            return line, 200
        return {"status": "ok", "execution_provider": str(self.execution_provider), "command": cmd.to_dict(), "line": line}, 200

    # AGENT HANDSHAKE: Broker ACKs close the submission loop and persist the audit trail used by ops and dashboard views.
    def ack_command(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        try:
            ack = ExecutionAck.from_payload(payload)
        except ValueError as exc:
            return {"status": "invalid", "error": str(exc), "payload": dict(payload or {})}, 400
        return self.store.ack_command(ack)

    def record_tick(self, payload: dict[str, Any]) -> None:
        self.store.record_tick(payload)

    def record_report(self, report_text: str, report_json: dict[str, Any] | None = None) -> None:
        self.store.record_report(report_text, report_json)

    def store_decisions(self, *, decisions: list[dict[str, Any]], vol: float, diagnostics: dict[str, Any]) -> None:
        self.store.store_decisions(decisions=decisions, vol=vol, diagnostics=diagnostics)

    def store_orchestration_bundle(
        self,
        *,
        context: dict[str, Any],
        packet: dict[str, Any],
        trace: dict[str, Any],
        runtime_mode: str,
        fallback_used: bool,
    ) -> None:
        self.store.store_orchestration_bundle(
            context=context,
            packet=packet,
            trace=trace,
            runtime_mode=runtime_mode,
            fallback_used=fallback_used,
        )

    def patch_state(self, patch: dict[str, Any]) -> None:
        self.store.update_state_patch(patch)

    def purge_pending_commands(
        self,
        *,
        reason: str,
        intents: set[str] | None = None,
        include_delivered: bool = True,
    ) -> int:
        return self.store.purge_pending_commands(reason=reason, intents=intents, include_delivered=include_delivered)

    def requeue_stale_delivered(self, *, age_secs: float) -> int:
        return self.store.requeue_stale_delivered(age_secs=age_secs)

    def record_runtime_boot_state(
        self,
        *,
        boot: dict[str, Any],
        patch: dict[str, Any] | None = None,
        prune_state: bool = False,
    ) -> None:
        self.store.record_runtime_boot_state(boot=boot, patch=patch, prune_state=prune_state)

    def record_runtime_boot_failure(
        self,
        *,
        boot: dict[str, Any],
        failure_reason: str,
        failed_at: Any | None = None,
        patch: dict[str, Any] | None = None,
        prune_state: bool = False,
    ) -> None:
        self.store.record_runtime_boot_failure(
            boot=boot,
            failure_reason=failure_reason,
            failed_at=failed_at,
            patch=patch,
            prune_state=prune_state,
        )

    def record_governance_event(
        self,
        *,
        event_type: str,
        reason: str = "",
        payload: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        self.store.record_governance_event(
            event_type=event_type,
            reason=reason,
            payload=payload,
            ts=ts,
        )

    def record_approval_event(
        self,
        *,
        subject_type: str,
        subject_id: str,
        approver: str,
        decision: str,
        reason: str = "",
        event_id: str | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        return self.store.record_approval_event(
            subject_type=subject_type,
            subject_id=subject_id,
            approver=approver,
            decision=decision,
            reason=reason,
            event_id=event_id,
            created_at=created_at,
        )

    def upsert_experiment_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert_experiment_proposal(payload)

    def get_experiment_proposal(self, experiment_id: str) -> dict[str, Any] | None:
        return self.store.get_experiment_proposal(experiment_id)

    def get_experiment_proposals(
        self,
        *,
        limit: int = 200,
        approval_status: str = "",
        source_run_id: str = "",
    ) -> list[dict[str, Any]]:
        return self.store.get_experiment_proposals(
            limit=limit,
            approval_status=approval_status,
            source_run_id=source_run_id,
        )

    def upsert_experiment_promotion(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert_experiment_promotion(payload)

    def get_experiment_promotion(self, promotion_id: str) -> dict[str, Any] | None:
        return self.store.get_experiment_promotion(promotion_id)

    def get_experiment_promotions(
        self,
        *,
        limit: int = 200,
        experiment_id: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]:
        return self.store.get_experiment_promotions(limit=limit, experiment_id=experiment_id, status=status)

    def upsert_experiment_lineage(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert_experiment_lineage(payload)

    def get_experiment_lineage(self, experiment_id: str) -> dict[str, Any] | None:
        return self.store.get_experiment_lineage(experiment_id)

    def get_experiment_lineages(
        self,
        *,
        limit: int = 200,
        latest_stage: str = "",
        approval_status: str = "",
    ) -> list[dict[str, Any]]:
        return self.store.get_experiment_lineages(
            limit=limit,
            latest_stage=latest_stage,
            approval_status=approval_status,
        )

    def get_approval_events(
        self,
        *,
        limit: int = 200,
        subject_type: str = "",
        subject_id: str = "",
    ) -> list[dict[str, Any]]:
        return self.store.get_approval_events(limit=limit, subject_type=subject_type, subject_id=subject_id)

    def enqueue_feature_push(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.enqueue_feature_push(payload)

    def claim_feature_push_batch(self, *, worker_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.claim_feature_push_batch(worker_id=worker_id, limit=limit)

    def record_feature_push_audit(
        self,
        *,
        outbox_key: str,
        pair: str,
        feature_service: str,
        entity_key: str,
        event_timestamp: float,
        status: str,
        payload: dict[str, Any],
        worker_id: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        return self.store.record_feature_push_audit(
            outbox_key=outbox_key,
            pair=pair,
            feature_service=feature_service,
            entity_key=entity_key,
            event_timestamp=event_timestamp,
            status=status,
            payload=payload,
            worker_id=worker_id,
            message=message,
        )

    def record_feature_push_success(
        self,
        *,
        outbox_key: str,
        worker_id: str | None = None,
        payload: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        return self.store.mark_feature_push_success(
            outbox_key=outbox_key,
            worker_id=worker_id,
            payload=payload,
            message=message,
        )

    def record_feature_push_failure(
        self,
        *,
        outbox_key: str,
        worker_id: str | None = None,
        message: str,
        payload: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> dict[str, Any]:
        out = self.store.mark_feature_push_failure(
            outbox_key=outbox_key,
            worker_id=worker_id,
            message=message,
            payload=payload,
            retryable=retryable,
        )
        self.record_governance_event(
            event_type="feature_push_retry" if bool(retryable) else "feature_push_failed",
            reason=str(message or ""),
            payload={
                "outbox_key": str(outbox_key),
                "worker_id": str(worker_id or ""),
                "retryable": bool(retryable),
                "payload": dict(payload or {}),
            },
        )
        return out

    def record_feature_parity(
        self,
        *,
        pair: str,
        feature_service: str,
        entity_key: str,
        event_timestamp: float,
        source: str,
        parity_ok: bool,
        payload: dict[str, Any],
        drift_score: float | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        out = self.store.record_feature_parity_audit(
            pair=pair,
            feature_service=feature_service,
            entity_key=entity_key,
            event_timestamp=event_timestamp,
            source=source,
            parity_ok=parity_ok,
            payload=payload,
            drift_score=drift_score,
            message=message,
        )
        if not bool(parity_ok):
            self.record_governance_event(
                event_type="feature_parity_breach",
                reason=str(message or "feature_parity_breach"),
                payload={
                    "pair": str(pair).upper(),
                    "feature_service": str(feature_service),
                    "entity_key": str(entity_key),
                    "event_timestamp": float(event_timestamp),
                    "source": str(source),
                    "drift_score": drift_score,
                    "payload": dict(payload or {}),
                },
            )
        return out

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

    def get_decision_snapshots(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.get_decision_snapshots(limit=limit)

    def get_orchestration_runs(
        self,
        *,
        limit: int = 200,
        pair: str = "",
        runtime_mode: str = "",
        cycle_id: str = "",
    ) -> list[dict[str, Any]]:
        return self.store.get_orchestration_runs(
            limit=limit,
            pair=pair,
            runtime_mode=runtime_mode,
            cycle_id=cycle_id,
        )

    def get_orchestration_traces(
        self,
        *,
        limit: int = 200,
        run_id: str = "",
        pair: str = "",
    ) -> list[dict[str, Any]]:
        return self.store.get_orchestration_traces(limit=limit, run_id=run_id, pair=pair)

    def get_closed_trade_reports(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.get_closed_trade_reports(limit=limit)

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        return self.store.get_command(command_id)

    def get_commands(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.get_commands(limit=limit)

    def get_command_events(self, *, command_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        return self.store.get_command_events(command_id=command_id, limit=limit)

    def get_governance_events(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.get_governance_events(limit=limit)

    def get_latest_tick(self, symbol: str) -> dict[str, Any] | None:
        return self.store.get_latest_tick(symbol)

    def verify_tables(self) -> dict[str, Any]:
        return self.store.verify_required_tables()

    def _simulate_paper_execution(self, cmd: ExecutionCommand) -> dict[str, Any]:
        tick = self.get_latest_tick(cmd.symbol)
        delivered_payload, acked_payload = build_simulated_ack_payloads(cmd, tick=tick)
        delivered_out, delivered_code = self.ack_command(delivered_payload)
        acked_out, acked_code = self.ack_command(acked_payload)
        return {
            "delivery": {"code": int(delivered_code), "status": str(delivered_out.get("status") or "")},
            "ack": {"code": int(acked_code), "status": str(acked_out.get("status") or "")},
            "fill_price": dict(acked_payload.get("orchestration_meta_json") or {}).get("paper_fill_price"),
            "fill_source": dict(acked_payload.get("orchestration_meta_json") or {}).get("paper_fill_source"),
        }

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

    def get_feature_push_outbox(self, *, limit: int = 200, statuses: set[str] | None = None) -> list[dict[str, Any]]:
        return self.store.get_feature_push_outbox(limit=limit, statuses=statuses)

    def get_feature_push_audit(self, *, limit: int = 200, statuses: set[str] | None = None) -> list[dict[str, Any]]:
        return self.store.get_feature_push_audit(limit=limit, statuses=statuses)

    def get_feature_parity_audit(self, *, limit: int = 200, pair: str | None = None) -> list[dict[str, Any]]:
        return self.store.get_feature_parity_audit(limit=limit, pair=pair)

    def get_feature_push_rollup(self) -> dict[str, Any]:
        return self.store.get_feature_push_rollup()

    def drain_feature_push_outbox(
        self,
        *,
        worker_id: str,
        limit: int = 50,
        repo_root: str | None = None,
        dry_run: bool = False,
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        from fxstack.feast.push import drain_feature_push_outbox

        return drain_feature_push_outbox(
            self,
            worker_id=worker_id,
            limit=limit,
            repo_root=repo_root,
            dry_run=dry_run,
            max_retries=max_retries,
        )
