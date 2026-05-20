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

This module fixes that by codifying the **28 methods + 1 property** that
external consumers actually call. It is a :class:`typing.Protocol` (structural
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

from typing import Any, Protocol, runtime_checkable


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
    ) -> int: ...

    def requeue_stale_delivered(self, *, age_secs: float) -> int: ...

    def get_commands(self, limit: int = 200) -> list[dict[str, Any]]: ...

    def get_command_events(
        self, *, command_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    # ------------------------------------------------------------------
    # Market data + reports
    # ------------------------------------------------------------------
    def record_tick(self, payload: dict[str, Any]) -> None: ...

    def record_report(
        self, report_text: str, report_json: dict[str, Any] | None = None
    ) -> None: ...

    def get_reports(self, limit: int = 200) -> list[dict[str, Any]]: ...

    def get_closed_trade_reports(self, limit: int = 200) -> list[dict[str, Any]]: ...

    # ------------------------------------------------------------------
    # Runtime state (write + read)
    # ------------------------------------------------------------------
    def patch_state(self, patch: dict[str, Any]) -> None: ...

    def get_state(self) -> dict[str, Any]: ...

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
    ) -> None: ...

    def record_runtime_boot_failure(
        self,
        *,
        boot: dict[str, Any],
        failure_reason: str,
        failed_at: Any | None = None,
        patch: dict[str, Any] | None = None,
        prune_state: bool = False,
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


__all__ = ["RuntimeServiceProtocol"]
