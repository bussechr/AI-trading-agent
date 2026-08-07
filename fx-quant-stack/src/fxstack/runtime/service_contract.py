# AGENT: ROLE: Lightweight final-entry approval and external runtime-service protocol contracts.
# AGENT: CALLED BY: `runtime.runner`, `runtime.scalp_live_loop`, and `runtime.service`.
# AGENT: SIDE EFFECTS: none; scalp-authority validation imports lazily only for a supplied scalp authority.
"""Explicit Protocol for what the bridge + runtime expect from a runtime service.

``fxstack.runtime.service.RuntimeService`` is the concrete implementation, but
multiple consumers (the FastAPI bridge, the live runtime loop, and roughly
sixteen test fakes) all depend on an implicit subset of its surface. Without
a written contract:

* Test fakes drift — they stub two or three methods, then a caller adds a new
  method, and the next test using the fake hits ``AttributeError`` at runtime.
* It's unclear which methods are "the bridge's public service API" versus
  internal helpers; reviewing changes to ``service.py`` requires reading
  every call site.
* Alternative implementations (e.g. an in-memory paper-only service for
  research notebooks) have no signature target.

This module fixes that by codifying the public surface that external consumers
actually call. It is a :class:`typing.Protocol` (structural
typing), so the existing :class:`RuntimeService` satisfies it by duck-typing
and tests can declare their fakes via ``# type: ignore`` or by implementing
just the methods their code exercises.

Marked :func:`typing.runtime_checkable` so tests can do
``assert isinstance(svc, RuntimeServiceProtocol)`` as a smoke check. Note
that ``runtime_checkable`` only verifies attribute presence, not signature
compatibility — that's fine for catching the common "stub missing a method"
drift, which is the actual failure mode.

What's deliberately **excluded** from this protocol:

* ``_simulate_paper_execution`` and ``get_latest_tick`` — internal to
  ``submit_command``'s paper-mode path.
* Feature-push outbox accessors (``get_feature_push_outbox``,
  ``get_feature_push_audit``, ``record_feature_push_*``) — only the
  outbox worker itself calls these.
* Experiment registry CRUD (``get_experiment_*``, ``upsert_experiment_*``)
  — unused by bridge + runner today.
* ``record_governance_event``, ``record_feature_parity``, etc. — internal
  audit hooks called by ``submit_command`` and the worker.

If a method moves from "internal" to "external" (i.e. starts being called
from outside ``service.py``), add it here and update consumers to refer to
the protocol type rather than the concrete class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
from typing import Any, Protocol, runtime_checkable

from fxstack.risk.constants import ROLLOUT_EXECUTION_MODES
from fxstack.runtime._util import safe_float, safe_int


_ENTRY_TRANSPORT_FIELDS = {
    "correlation_id",
    "trace_id",
    "thread_id",
    "schema_version",
    "orchestration_meta_json",
    "idempotency_key",
    "expected_account_mode",
    "expected_account_scope",
    "expected_authority_revision",
}


@dataclass(frozen=True, slots=True)
class FinalEntryApproval:
    """In-process proof that an entry survived the canonical authority chain."""

    pair: str
    side: str
    risk_approved_payload: dict[str, Any] = field(repr=False)
    canonical_ready: bool = False
    governed_allowed: bool = False
    rollout_active: bool = False
    rollout_mode: str = ""
    rollout_pair_allowlisted: bool = False
    correlation_id: str = ""
    trace_id: str = ""
    broker_account_mode: str = ""
    broker_account_scope: str = ""
    authority_revision: int = 0
    release_generation_id: str = ""
    release_request_sha256: str = ""
    model_identity_sha256: str = ""
    manifest_file_sha256: str = ""
    runtime_boot_id: str = ""
    sleeve: str = ""
    strategy_authority: dict[str, Any] = field(default_factory=dict, repr=False)

    def validation_error(self, payload: dict[str, Any]) -> str:
        final_payload = dict(payload or {})
        approved = dict(self.risk_approved_payload or {})
        if not bool(self.canonical_ready):
            return "canonical_entry_not_ready"
        if not bool(self.governed_allowed):
            return "committee_or_governor_not_approved"
        if (
            not bool(self.rollout_active)
            or str(self.rollout_mode).strip().lower() not in ROLLOUT_EXECUTION_MODES
        ):
            return "live_rollout_inactive"
        if not bool(self.rollout_pair_allowlisted):
            return "live_rollout_pair_blocked"
        if (
            not str(self.correlation_id or "").strip()
            or not str(self.trace_id or "").strip()
        ):
            return "live_trace_missing"
        if str(self.broker_account_mode or "").strip().lower() not in {"demo", "real"}:
            return "broker_account_mode_unattested"
        if not str(self.broker_account_scope or "").strip():
            return "broker_account_scope_unattested"
        if safe_int(self.authority_revision) <= 0:
            return "live_authority_revision_unattested"
        if not str(self.sleeve or "").strip():
            return "live_sleeve_unattested"
        strategy_authority = dict(self.strategy_authority or {})
        if strategy_authority:
            # The model-stack lane never carries this authority. Keep the
            # comparatively broad exact-22 authority graph cold for it.
            from fxstack.runtime.scalp_execution_authority import (
                SCALP_SLEEVE,
                authority_error,
                expectation_from_authority,
            )

            expectation = expectation_from_authority(strategy_authority)
            strategy_error = authority_error(
                strategy_authority,
                expectation=expectation,
            )
            if strategy_error:
                return str(strategy_error)
            if (
                str(self.runtime_boot_id or "").strip()
                != str(expectation.runtime_boot_id).strip()
            ):
                return "scalp_authority_runtime_boot_approval_mismatch"
            if safe_int(self.authority_revision) != safe_int(
                expectation.authority_revision
            ):
                return "scalp_authority_revision_approval_mismatch"
            if str(self.sleeve or "").strip().lower() != SCALP_SLEEVE:
                return "scalp_authority_sleeve_invalid"
            if str(self.pair or "").strip().upper() not in set(
                expectation.symbol_scope
            ):
                return "scalp_authority_symbol_not_covered"
        expected_pair = str(self.pair or "").strip().upper()
        expected_side = str(self.side or "").strip().upper()
        if expected_side not in {"BUY", "SELL"}:
            return "approval_side_invalid"
        if str(final_payload.get("symbol") or "").strip().upper() != expected_pair:
            return "approval_symbol_mismatch"
        if (
            str(final_payload.get("cmd") or final_payload.get("side") or "")
            .strip()
            .upper()
            != expected_side
        ):
            return "approval_side_mismatch"
        if (
            str(approved.get("symbol") or expected_pair).strip().upper()
            != expected_pair
        ):
            return "risk_approval_symbol_mismatch"
        if (
            str(approved.get("cmd") or approved.get("side") or "").strip().upper()
            != expected_side
        ):
            return "risk_approval_side_mismatch"
        production_scalper_claimed = bool(
            str(final_payload.get("strategy_lane") or "").strip().lower()
            == "production_scalper"
            or str(final_payload.get("intent") or "").strip().lower()
            == "production_scalper_entry"
            or str(approved.get("strategy_lane") or "").strip().lower()
            == "production_scalper"
            or str(approved.get("intent") or "").strip().lower()
            == "production_scalper_entry"
        )
        if production_scalper_claimed:
            for candidate in (approved, final_payload):
                if (
                    str(candidate.get("execution_type") or "").strip().lower()
                    != "market"
                ):
                    return "scalp_market_entry_execution_type_invalid"
                if candidate.get("pending_orders_forbidden") is not True:
                    return "scalp_market_entry_pending_orders_not_forbidden"
                raw_deadline = candidate.get("entry_deadline_epoch")
                if isinstance(raw_deadline, bool):
                    return "scalp_market_entry_deadline_invalid"
                try:
                    deadline_number = float(raw_deadline)
                except (TypeError, ValueError, OverflowError):
                    return "scalp_market_entry_deadline_invalid"
                if (
                    not math.isfinite(deadline_number)
                    or deadline_number <= 0.0
                    or not deadline_number.is_integer()
                    or deadline_number > 2_147_483_647
                ):
                    return "scalp_market_entry_deadline_invalid"
            approved_deadline = int(float(approved["entry_deadline_epoch"]))
            final_deadline = int(float(final_payload["entry_deadline_epoch"]))
            if approved_deadline != final_deadline:
                return "scalp_market_entry_deadline_changed"
            if final_deadline <= time.time():
                return "scalp_market_entry_deadline_expired"
        approved_lots = safe_float(approved.get("lots"))
        final_lots = safe_float(final_payload.get("lots"))
        if (
            approved_lots <= 0.0
            or final_lots <= 0.0
            or final_lots > approved_lots + 1e-9
        ):
            return "risk_approval_lots_mismatch"
        final_business = {
            key: value
            for key, value in final_payload.items()
            if key not in _ENTRY_TRANSPORT_FIELDS
        }
        for key, value in approved.items():
            if key == "lots" or key in _ENTRY_TRANSPORT_FIELDS:
                continue
            if final_business.get(key) != value:
                return "risk_approval_payload_mismatch"
        if set(final_business) - set(approved):
            return "risk_approval_payload_mutation"
        if str(final_payload.get("correlation_id") or "") != str(self.correlation_id):
            return "approval_correlation_mismatch"
        if str(final_payload.get("trace_id") or "") != str(self.trace_id):
            return "approval_trace_mismatch"
        orchestration_meta = dict(final_payload.get("orchestration_meta_json") or {})
        if str(orchestration_meta.get("trace_id") or "") != str(self.trace_id):
            return "approval_trace_mismatch"
        if safe_int(orchestration_meta.get("authority_revision")) != safe_int(
            self.authority_revision
        ):
            return "approval_authority_revision_mismatch"
        if (
            str(orchestration_meta.get("adaptive_sleeve") or "")
            != str(self.sleeve).strip().lower()
        ):
            return "approval_adaptive_sleeve_mismatch"
        return ""


@runtime_checkable
class RuntimeServiceProtocol(Protocol):
    """Contract that the bridge HTTP layer + runtime loop depend on.

    All methods are documented in detail on the concrete implementation at
    :class:`fxstack.runtime.service.RuntimeService`. The signatures here
    should match the concrete methods — the parity test
    ``test_runtime_service_satisfies_protocol`` pins that.
    """

    # ------------------------------------------------------------------
    # Shutdown fence
    # ------------------------------------------------------------------
    @property
    def draining(self) -> bool: ...

    def drain(self) -> None: ...

    # ------------------------------------------------------------------
    # Command lifecycle (write + read)
    # ------------------------------------------------------------------
    def submit_command(
        self, payload: dict[str, Any], *, proto: str = "v2"
    ) -> tuple[dict[str, Any], int]: ...

    def submit_approved_command(
        self,
        payload: dict[str, Any],
        *,
        approval: FinalEntryApproval,
        proto: str = "v2",
    ) -> tuple[dict[str, Any], int]: ...

    def ack_command(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]: ...

    def poll_command(
        self, *, as_line: bool = False
    ) -> tuple[str | dict[str, Any], int]: ...

    def purge_pending_commands(
        self,
        *,
        reason: str,
        intents: set[str] | None = None,
        include_delivered: bool = True,
        preserve_queued_exposure_reducing: bool = False,
    ) -> int: ...

    def quarantine_stale_delivered(self, *, age_secs: float) -> int: ...

    def get_execution_uncertainty(
        self,
        *,
        limit: int = 20,
        symbol: str = "",
    ) -> dict[str, Any]: ...

    def get_command(self, command_id: str) -> dict[str, Any] | None: ...

    def get_commands(self, limit: int = 200) -> list[dict[str, Any]]: ...

    def get_scalp_reconciliation_commands(
        self,
        *,
        include_historical: bool,
        limit: int = 5000,
    ) -> list[dict[str, Any]]: ...

    def get_command_window_summary(
        self, *, start_ts: float, end_ts: float
    ) -> dict[str, Any]: ...

    def get_command_events(
        self, *, command_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    # ------------------------------------------------------------------
    # Market data + reports
    # ------------------------------------------------------------------
    def record_tick(self, payload: dict[str, Any]) -> None: ...

    def record_ticks(self, payloads: list[dict[str, Any]]) -> None: ...

    def record_report(
        self, report_text: str, report_json: dict[str, Any] | None = None
    ) -> None: ...

    def get_reports(self, limit: int = 200) -> list[dict[str, Any]]: ...

    def get_closed_trade_reports(self, limit: int = 200) -> list[dict[str, Any]]: ...

    # ------------------------------------------------------------------
    # Runtime state (write + read)
    # ------------------------------------------------------------------
    def patch_state(
        self,
        patch: dict[str, Any],
        *,
        runtime_diag_patch: dict[str, Any] | None = None,
        runtime_diag_remove: tuple[str, ...] = (),
    ) -> None: ...

    def commit_state_and_decisions(
        self,
        patch: dict[str, Any],
        *,
        runtime_diag_patch: dict[str, Any] | None = None,
        runtime_diag_remove: tuple[str, ...] = (),
        decisions: list[dict[str, Any]],
        vol: float,
        diagnostics: dict[str, Any],
    ) -> None: ...

    def compare_and_set_release_authority(
        self,
        *,
        next_authority: dict[str, Any],
        expected_generation_id: str = "",
        expected_status: str = "",
        safety_dominant: bool = False,
    ) -> dict[str, Any]: ...

    def disable_execution_egress(
        self,
        *,
        reason: str,
        revoke_release: bool = True,
        preserve_queued_exposure_reducing: bool = False,
    ) -> dict[str, Any]: ...

    def enable_production_execution_egress(
        self,
        *,
        runtime_boot_id: str,
    ) -> dict[str, Any]: ...

    def get_state(self) -> dict[str, Any]: ...

    def get_state_and_metrics(self) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def get_state_metrics_and_latest_decision_diagnostics(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]: ...

    def get_state_and_governance_metrics(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def store_decisions(
        self,
        *,
        decisions: list[dict[str, Any]],
        vol: float,
        diagnostics: dict[str, Any],
    ) -> None: ...

    def get_decision_snapshots(self, limit: int = 200) -> list[dict[str, Any]]: ...

    def get_open_positions(self) -> list[dict[str, Any]]: ...

    # ------------------------------------------------------------------
    # Orchestration (read)
    # ------------------------------------------------------------------
    def get_orchestration_runs(
        self,
        *,
        limit: int = 200,
        pair: str = "",
        runtime_mode: str = "",
        cycle_id: str = "",
    ) -> list[dict[str, Any]]: ...

    def get_orchestration_traces(
        self,
        *,
        limit: int = 200,
        run_id: str = "",
        pair: str = "",
    ) -> list[dict[str, Any]]: ...

    # ------------------------------------------------------------------
    # Health + observability
    # ------------------------------------------------------------------
    def get_health(self) -> dict[str, Any]: ...

    def get_metrics(self) -> dict[str, Any]: ...

    def get_governance_events(self, limit: int = 200) -> list[dict[str, Any]]: ...

    # ------------------------------------------------------------------
    # Boot recovery (runtime-only)
    # ------------------------------------------------------------------
    def record_runtime_boot_state(
        self,
        *,
        boot: dict[str, Any],
        patch: dict[str, Any] | None = None,
        prune_state: bool = False,
        preserve_queued_exposure_reducing: bool = False,
    ) -> None: ...

    def record_runtime_boot_failure(
        self,
        *,
        boot: dict[str, Any],
        failure_reason: str,
        failed_at: Any | None = None,
        patch: dict[str, Any] | None = None,
        prune_state: bool = False,
        preserve_queued_exposure_reducing: bool = False,
    ) -> None: ...

    # ------------------------------------------------------------------
    # Model registry (runtime-only)
    # ------------------------------------------------------------------
    def get_active_model_set(self, pair: str) -> dict[str, Any] | None: ...

    def get_active_model_sets(
        self, *, enabled_only: bool = True
    ) -> dict[str, dict[str, Any]]: ...

    def upsert_active_model_set(
        self,
        *,
        pair: str,
        model_set_id: str,
        registry_path: str,
        artifacts: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> None: ...

    # ------------------------------------------------------------------
    # Feature push (runtime-only)
    # ------------------------------------------------------------------
    def enqueue_feature_push(self, payload: dict[str, Any]) -> dict[str, Any]: ...


__all__ = ["FinalEntryApproval", "RuntimeServiceProtocol"]
