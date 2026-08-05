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

from dataclasses import dataclass, field
import hashlib
from importlib import import_module
import json
import math
import time
from typing import Any

from fxstack.risk.kernel import ROLLOUT_EXECUTION_MODES
from fxstack.runtime.dto import ExecutionAck, ExecutionCommand
from fxstack.runtime.postgres_store import PostgresRuntimeStore
from fxstack.runtime.protocol import command_to_provider_line
from fxstack.runtime.mtvclc_runtime_release import (
    MTVCLCRuntimeReleaseVerification,
)
from fxstack.runtime.scalp_execution_authority import (
    SCALP_SLEEVE,
    authority_error as scalp_authority_error,
    command_binding_fields as scalp_command_binding_fields,
    expectation_from_authority as scalp_expectation_from_authority,
    validation_witness_error as scalp_validation_witness_error,
)
from fxstack.settings import get_settings


_ACTIVE_EXECUTION_PROVIDERS = {"mt4", "paper"}
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
        if not str(self.correlation_id or "").strip() or not str(self.trace_id or "").strip():
            return "live_trace_missing"
        if str(self.broker_account_mode or "").strip().lower() not in {"demo", "real"}:
            return "broker_account_mode_unattested"
        if not str(self.broker_account_scope or "").strip():
            return "broker_account_scope_unattested"
        if _safe_int(self.authority_revision) <= 0:
            return "live_authority_revision_unattested"
        if not str(self.sleeve or "").strip():
            return "live_sleeve_unattested"
        strategy_authority = dict(self.strategy_authority or {})
        if strategy_authority:
            expectation = scalp_expectation_from_authority(strategy_authority)
            strategy_error = scalp_authority_error(
                strategy_authority,
                expectation=expectation,
            )
            if strategy_error:
                return str(strategy_error)
            if str(self.runtime_boot_id or "").strip() != str(
                expectation.runtime_boot_id
            ).strip():
                return "scalp_authority_runtime_boot_approval_mismatch"
            if _safe_int(self.authority_revision) != _safe_int(
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
        if str(final_payload.get("cmd") or final_payload.get("side") or "").strip().upper() != expected_side:
            return "approval_side_mismatch"
        if str(approved.get("symbol") or expected_pair).strip().upper() != expected_pair:
            return "risk_approval_symbol_mismatch"
        if str(approved.get("cmd") or approved.get("side") or "").strip().upper() != expected_side:
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
                if str(candidate.get("execution_type") or "").strip().lower() != "market":
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
        approved_lots = _safe_float(approved.get("lots"))
        final_lots = _safe_float(final_payload.get("lots"))
        if approved_lots <= 0.0 or final_lots <= 0.0 or final_lots > approved_lots + 1e-9:
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
        if _safe_int(orchestration_meta.get("authority_revision")) != _safe_int(
            self.authority_revision
        ):
            return "approval_authority_revision_mismatch"
        if str(orchestration_meta.get("adaptive_sleeve") or "") != str(
            self.sleeve
        ).strip().lower():
            return "approval_adaptive_sleeve_mismatch"
        return ""


def _paper_execution_adapter() -> Any:
    try:
        module = import_module("fxstack.providers.execution.paper")
        build_ack_payloads = module.build_simulated_ack_payloads
    except (AttributeError, ImportError) as exc:
        raise RuntimeError(
            "paper execution provider is unavailable in this runtime distribution"
        ) from exc
    if not callable(build_ack_payloads):
        raise RuntimeError(
            "paper execution provider is unavailable in this runtime distribution"
        )
    return module


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


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
        "execution_type": str(payload.get("execution_type") or ""),
        "pending_orders_forbidden": payload.get("pending_orders_forbidden"),
        "entry_deadline_epoch": payload.get("entry_deadline_epoch"),
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


class RuntimeService:
    # Class-level default so test fixtures that build a RuntimeService via
    # ``__new__`` (bypassing __init__ for unit isolation) still get a safe
    # value for the draining fence.
    _draining: bool = False
    _require_entry_approval: bool = True

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
        runtime_settings = get_settings() if not str(execution_provider or "").strip() else None
        self.execution_provider = str(
            execution_provider
            or getattr(runtime_settings, "normalized_execution_provider", "")
        )
        # Exposure-increasing MT4 queue ingress is always internal-only. This
        # must not vary with process posture or constructor call style because
        # a staged-safe bridge is still connected to the broker queue.
        self._require_entry_approval = bool(
            str(self.execution_provider).strip().lower() == "mt4"
        )
        if str(self.execution_provider).strip().lower() == "paper":
            _paper_execution_adapter()
        self.store = PostgresRuntimeStore(
            database_url,
            requeue_age_secs=float(requeue_age_secs),
            connect_retries=int(db_connect_retries),
        )
        # Set during graceful shutdown via :meth:`drain`. While true, new
        # command submissions are rejected with 503 so the EA backs off and
        # the queue is not racing ASGI teardown.
        self._draining: bool = False

    @property
    def draining(self) -> bool:
        """True after :meth:`drain` has been called; fence for new writes."""
        return self._draining

    # AGENT HANDSHAKE: Public command ingress cannot increase exposure. The
    # runner uses submit_approved_command after canonical risk + governance.
    def submit_command(self, payload: dict[str, Any], *, proto: str = "v2") -> tuple[dict[str, Any], int]:
        return self._submit_command(payload, proto=proto, entry_approval=None)

    # AGENT HANDSHAKE: Standalone scalp research has no production entry lane.
    # The installed production loop uses ``submit_approved_command`` with the
    # signed, DB-generation-bound scalp authority; this legacy compatibility
    # method remains permanently fail closed so research cannot mint a naked
    # entry bypass.
    def submit_scalp_command(
        self, payload: dict[str, Any], *, proto: str = "v2"
    ) -> tuple[dict[str, Any], int]:
        del payload, proto
        return {
            "status": "forbidden",
            "error": "scalp_live_ingress_disabled_unvalidated_authority",
        }, 403

    def submit_approved_command(
        self,
        payload: dict[str, Any],
        *,
        approval: FinalEntryApproval,
        proto: str = "v2",
    ) -> tuple[dict[str, Any], int]:
        if not isinstance(approval, FinalEntryApproval):
            return {
                "status": "forbidden",
                "error": "final_entry_approval_required",
            }, 403
        approval_error = approval.validation_error(dict(payload or {}))
        if approval_error:
            return {
                "status": "forbidden",
                "error": str(approval_error),
            }, 403
        try:
            state = self.get_state()
        except Exception:
            return {
                "status": "unavailable",
                "error": "broker_account_attestation_unavailable",
            }, 503
        state_account_mode = str(state.get("broker_account_mode") or "").strip().lower()
        state_account_scope = str(state.get("broker_account_scope") or "").strip()
        state_runtime_diag = dict(state.get("runtime_diag") or {})
        state_live = dict(state_runtime_diag.get("orchestration_live") or {})
        state_admission = dict(state_runtime_diag.get("live_command_admission") or {})
        if not bool(state_live.get("enabled", False)) or str(
            state_live.get("mode") or ""
        ).strip().lower() != "live":
            return {"status": "forbidden", "error": "live_mode_disabled"}, 403
        if not bool(state_live.get("runtime_enabled", False)):
            return {"status": "forbidden", "error": "live_runtime_killed"}, 403
        if bool(state_live.get("queue_kill_active", False)):
            return {"status": "forbidden", "error": "live_queue_killed"}, 403
        if _safe_int(state_live.get("authority_revision")) != _safe_int(
            approval.authority_revision
        ):
            return {
                "status": "forbidden",
                "error": "live_authority_revision_changed",
            }, 403
        if not bool(state_admission.get("allowed", False)):
            return {
                "status": "forbidden",
                "error": "live_command_admission_blocked",
            }, 403
        if state_account_mode != str(approval.broker_account_mode).strip().lower():
            return {
                "status": "forbidden",
                "error": "broker_account_mode_changed",
            }, 403
        if state_account_scope != str(approval.broker_account_scope).strip():
            return {
                "status": "forbidden",
                "error": "broker_account_scope_changed",
            }, 403
        if approval.strategy_authority:
            expected_strategy = scalp_expectation_from_authority(
                approval.strategy_authority
            )
            strategy_error = scalp_authority_error(
                dict(state.get("production_scalp_authority") or {}),
                expectation=expected_strategy,
            )
            if strategy_error:
                return {
                    "status": "forbidden",
                    "error": str(strategy_error),
                }, 403
        return self._submit_command(payload, proto=proto, entry_approval=approval)

    def _submit_command(
        self,
        payload: dict[str, Any],
        *,
        proto: str = "v2",
        entry_approval: FinalEntryApproval | None,
    ) -> tuple[dict[str, Any], int]:
        if self._draining:
            # Service has begun shutdown; tell callers to retry against a
            # restarted instance. 503 is the contract orchestrators expect.
            return {"status": "draining", "error": "bridge_shutting_down"}, 503
        raw_payload = dict(payload or {})
        if entry_approval is not None:
            raw_payload["expected_account_mode"] = str(
                entry_approval.broker_account_mode
            ).strip().lower()
            raw_payload["expected_account_scope"] = str(
                entry_approval.broker_account_scope
            ).strip()
            raw_payload["expected_authority_revision"] = _safe_int(
                entry_approval.authority_revision
            )
            # Retained for compatibility with imported externally witnessed
            # releases. Production-owned authority does not require them.
            raw_payload["expected_release_generation_id"] = str(
                entry_approval.release_generation_id
            )
            raw_payload["expected_release_request_sha256"] = str(
                entry_approval.release_request_sha256
            )
            raw_payload["expected_model_identity_sha256"] = str(
                entry_approval.model_identity_sha256
            )
            raw_payload["expected_manifest_file_sha256"] = str(
                entry_approval.manifest_file_sha256
            )
            raw_payload["expected_runtime_boot_id"] = str(
                entry_approval.runtime_boot_id
            )
            if entry_approval.strategy_authority:
                raw_payload.update(
                    scalp_command_binding_fields(
                        dict(entry_approval.strategy_authority)
                    )
                )
        provider_name = str(self.execution_provider).strip().lower()
        if provider_name not in _ACTIVE_EXECUTION_PROVIDERS:
            return {
                "status": "invalid",
                "error": (
                    f"unsupported execution provider: {self.execution_provider} has no active runtime adapter; "
                    f"active providers are {','.join(sorted(_ACTIVE_EXECUTION_PROVIDERS))}"
                ),
                "execution_provider": str(self.execution_provider),
            }, 400
        if provider_name == "paper":
            try:
                _paper_execution_adapter()
            except RuntimeError as exc:
                return {
                    "status": "invalid",
                    "error": str(exc),
                    "execution_provider": str(self.execution_provider),
                }, 400
        # Server-owned durable provenance. ACK classification must never trust
        # a caller-supplied claim that a broker command was merely simulated.
        raw_payload["_execution_provider"] = provider_name
        if (
            bool(getattr(self, "_require_entry_approval", True))
            and provider_name == "mt4"
            and str(raw_payload.get("cmd") or "").strip().upper() in {"BUY", "SELL"}
            and entry_approval is None
        ):
            return {
                "status": "forbidden",
                "error": "final_entry_approval_required",
            }, 403
        if str(raw_payload.get("cmd") or "").strip().upper() in {"BUY", "SELL"}:
            # Active queue ingress always requires broker-native stop and
            # target protection, irrespective of an operator-provided flag.
            raw_payload["entry_protection_required"] = True
        if (
            not str(
                raw_payload.get("command_id")
                or raw_payload.get("id")
                or raw_payload.get("signal_id")
                or ""
            ).strip()
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
        except (TypeError, ValueError, OverflowError) as exc:
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
        exposure_increasing = str(cmd.cmd).strip().upper() in {"BUY", "SELL"}
        execution_uncertainty: dict[str, Any] | None = None
        if exposure_increasing:
            try:
                # This read supplies a stable caller diagnostic. The store
                # repeats the predicate atomically with enqueue below.
                execution_uncertainty = self.store.get_execution_uncertainty(
                    symbol=str(cmd.symbol or ""),
                )
            except Exception:
                return {
                    "status": "reconciliation_check_failed",
                    "error": "unable_to_prove_prior_execution_outcomes_resolved",
                    "command_id": cmd.command_id,
                    "command": cmd.to_dict(),
                }, 503
        try:
            if exposure_increasing:
                enqueue_kwargs: dict[str, Any] = {
                    "require_resolved_execution": True,
                }
                if entry_approval is not None:
                    required_live_admission = {
                        "pair": str(entry_approval.pair),
                        "broker_account_mode": str(entry_approval.broker_account_mode),
                        "broker_account_scope": str(entry_approval.broker_account_scope),
                        "authority_revision": _safe_int(
                            entry_approval.authority_revision
                        ),
                        "release_generation_id": str(
                            entry_approval.release_generation_id
                        ),
                        "release_request_sha256": str(
                            entry_approval.release_request_sha256
                        ),
                        "model_identity_sha256": str(
                            entry_approval.model_identity_sha256
                        ),
                        "manifest_file_sha256": str(
                            entry_approval.manifest_file_sha256
                        ),
                        "runtime_boot_id": str(
                            entry_approval.runtime_boot_id
                        ),
                    }
                    if entry_approval.strategy_authority:
                        required_live_admission["strategy_authority"] = dict(
                            entry_approval.strategy_authority
                        )
                    enqueue_kwargs["required_live_admission"] = (
                        required_live_admission
                    )
                ok, state = self.store.enqueue_command(cmd, **enqueue_kwargs)
            else:
                ok, state = self.store.enqueue_command(cmd)
        except Exception:
            if exposure_increasing:
                return {
                    "status": "reconciliation_check_failed",
                    "error": "unable_to_prove_prior_execution_outcomes_resolved",
                    "command_id": cmd.command_id,
                    "command": cmd.to_dict(),
                }, 503
            raise
        if not ok:
            if str(state).startswith(
                (
                    "execution_egress_",
                    "release_authority_",
                    "release_witness_",
                    "scalp_authority_",
                    "scalp_command_",
                    "expected_strategy_",
                    "expected_broker_contract_",
                    "broker_contract_command_",
                    "broker_contract_order_",
                    "broker_contract_trade_not_allowed:",
                    "scalp_market_entry_",
                )
            ) or state in {
                "execution_egress_disabled",
                "execution_egress_authority_invalid",
                "execution_egress_generation_mismatch",
                "execution_egress_request_mismatch",
                "execution_egress_boot_mismatch",
                "release_authority_not_active",
                "release_authority_state_schema_invalid",
                "release_authority_request_schema_invalid",
                "release_authority_ack_schema_invalid",
                "release_authority_ack_generation_mismatch",
                "release_authority_ack_request_mismatch",
                "release_authority_ack_boot_missing",
                "release_witness_schema_invalid",
                "release_witness_signature_missing",
                "live_runtime_killed",
                "live_mode_disabled",
                "live_queue_killed",
                "live_command_admission_blocked",
                "live_rollout_pair_blocked",
                "live_pair_not_allowlisted",
                "live_intent_not_allowlisted",
                "broker_account_mode_changed",
                "broker_account_scope_changed",
                "broker_account_mode_unattested",
                "broker_account_scope_unattested",
                "broker_account_mode_approval_mismatch",
                "broker_account_scope_approval_mismatch",
                "live_authority_revision_unattested",
                "live_authority_revision_changed",
                "live_authority_revision_approval_mismatch",
                "final_entry_approval_missing",
                "scalp_daily_entry_frequency_exhausted",
            }:
                return {
                    "status": "forbidden",
                    "error": str(state),
                    "command_id": cmd.command_id,
                }, 403
            if str(state).startswith(
                (
                    "broker_contract_specs_",
                    "broker_contract_spec_missing:",
                    "broker_contract_ig_mt4_venue_",
                    "broker_contract_account_currency_",
                    "broker_contract_free_margin_",
                )
            ) or state in {
                "broker_heartbeat_disconnected",
                "broker_heartbeat_invalid",
                "broker_heartbeat_stale",
                "market_tick_missing",
                "market_tick_invalid",
                "market_tick_stale",
            }:
                return {
                    "status": "unavailable",
                    "error": str(state),
                    "command_id": cmd.command_id,
                }, 503
            if state == "reconciliation_required":
                if not bool((execution_uncertainty or {}).get("blocked")):
                    try:
                        execution_uncertainty = self.store.get_execution_uncertainty(
                            symbol=str(cmd.symbol or ""),
                        )
                    except Exception:
                        return {
                            "status": "reconciliation_check_failed",
                            "error": "unable_to_read_unresolved_execution_outcomes",
                            "command_id": cmd.command_id,
                            "command": cmd.to_dict(),
                        }, 503
                return {
                    "status": "reconciliation_required",
                    "error": "new_exposure_blocked_by_unresolved_execution_outcome",
                    "command_id": cmd.command_id,
                    "command": cmd.to_dict(),
                    "execution_uncertainty": dict(execution_uncertainty or {}),
                }, 409
            existing = None
            if str(cmd.idempotency_key or "").strip():
                existing = self.store.get_active_command_by_idempotency_key(cmd.idempotency_key)
            if existing is None:
                existing = self.store.get_command(cmd.command_id)
            duplicate_command_id = str((existing or {}).get("command_id") or cmd.command_id)
            return {"status": "duplicate", "command_id": duplicate_command_id, "state": state}, 200
        paper_execution: dict[str, Any] | None = None
        if provider_name == "paper":
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
            try:
                _paper_execution_adapter()
            except RuntimeError as exc:
                error = str(exc)
                return ("", 400) if as_line else (
                    {
                        "status": "invalid",
                        "error": error,
                        "execution_provider": str(self.execution_provider),
                    },
                    400,
                )
            return ("", 200) if as_line else ({"status": "empty", "execution_provider": "paper"}, 200)
        if provider_name not in {"mt4"}:
            error = f"unsupported execution provider for polling: {self.execution_provider}"
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
        except (TypeError, ValueError, OverflowError) as exc:
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

    def claim_bridge_consumer_lease(
        self,
        *,
        consumer_identity: str,
        producer_instance_id: str,
        terminal_lease_scope: str,
        credential_generation_id: str,
        bridge_protocol_version: str,
        channel: str,
        lease_secs: float,
    ) -> dict[str, Any]:
        return self.store.claim_bridge_consumer_lease(
            consumer_identity=consumer_identity,
            producer_instance_id=producer_instance_id,
            terminal_lease_scope=terminal_lease_scope,
            credential_generation_id=credential_generation_id,
            bridge_protocol_version=bridge_protocol_version,
            channel=channel,
            lease_secs=lease_secs,
        )

    def compare_and_set_release_authority(
        self,
        *,
        next_authority: dict[str, Any],
        expected_generation_id: str = "",
        expected_status: str = "",
        safety_dominant: bool = False,
    ) -> dict[str, Any]:
        return self.store.compare_and_set_release_authority(
            next_authority=next_authority,
            expected_generation_id=expected_generation_id,
            expected_status=expected_status,
            safety_dominant=safety_dominant,
        )

    def compare_and_set_production_scalp_authority(
        self,
        *,
        next_authority: dict[str, Any],
        validation_verification: MTVCLCRuntimeReleaseVerification | None = None,
        expected_generation_id: str = "",
        expected_status: str = "",
        safety_dominant: bool = False,
    ) -> dict[str, Any]:
        validation_witness: dict[str, Any] | None = None
        if not safety_dominant:
            if not isinstance(
                validation_verification,
                MTVCLCRuntimeReleaseVerification,
            ):
                return {
                    "updated": False,
                    "reason": "scalp_validation_witness_missing",
                    "authority": dict(
                        self.get_state().get("production_scalp_authority") or {}
                    ),
                }
            validation_witness = validation_verification.to_dict()
            witness_failure = scalp_validation_witness_error(
                validation_witness,
                authority=dict(next_authority or {}),
            )
            if witness_failure:
                return {
                    "updated": False,
                    "reason": str(witness_failure),
                    "authority": dict(
                        self.get_state().get("production_scalp_authority") or {}
                    ),
                }
        return self.store.compare_and_set_production_scalp_authority(
            next_authority=next_authority,
            validation_witness=validation_witness,
            expected_generation_id=expected_generation_id,
            expected_status=expected_status,
            safety_dominant=safety_dominant,
        )

    def disable_execution_egress(
        self,
        *,
        reason: str,
        revoke_release: bool = True,
        preserve_queued_exposure_reducing: bool = False,
    ) -> dict[str, Any]:
        return self.store.disable_execution_egress(
            reason=reason,
            revoke_release=revoke_release,
            preserve_queued_exposure_reducing=(
                preserve_queued_exposure_reducing
            ),
        )

    def enable_production_execution_egress(
        self,
        *,
        runtime_boot_id: str,
    ) -> dict[str, Any]:
        return self.store.enable_production_execution_egress(
            runtime_boot_id=runtime_boot_id,
        )

    def patch_orchestration_live_state(
        self,
        *,
        updates: dict[str, Any],
        expected_live_authority: dict[str, Any] | None,
        safety_dominant: bool = False,
        allow_reenable: bool = False,
    ) -> dict[str, Any]:
        return self.store.patch_orchestration_live_state(
            updates=updates,
            expected_live_authority=expected_live_authority,
            safety_dominant=safety_dominant,
            allow_reenable=allow_reenable,
        )

    def purge_pending_commands(
        self,
        *,
        reason: str,
        intents: set[str] | None = None,
        include_delivered: bool = True,
        preserve_queued_exposure_reducing: bool = False,
    ) -> int:
        return self.store.purge_pending_commands(
            reason=reason,
            intents=intents,
            include_delivered=include_delivered,
            preserve_queued_exposure_reducing=(
                preserve_queued_exposure_reducing
            ),
        )

    def quarantine_stale_delivered(self, *, age_secs: float) -> int:
        return self.store.quarantine_stale_delivered(age_secs=age_secs)

    def get_execution_uncertainty(
        self,
        *,
        limit: int = 20,
        symbol: str = "",
    ) -> dict[str, Any]:
        return self.store.get_execution_uncertainty(
            limit=limit,
            symbol=symbol,
        )

    def record_runtime_boot_state(
        self,
        *,
        boot: dict[str, Any],
        patch: dict[str, Any] | None = None,
        prune_state: bool = False,
        preserve_queued_exposure_reducing: bool = False,
    ) -> None:
        self.store.record_runtime_boot_state(
            boot=boot,
            patch=patch,
            prune_state=prune_state,
            preserve_queued_exposure_reducing=(
                preserve_queued_exposure_reducing
            ),
        )

    def record_runtime_boot_failure(
        self,
        *,
        boot: dict[str, Any],
        failure_reason: str,
        failed_at: Any | None = None,
        patch: dict[str, Any] | None = None,
        prune_state: bool = False,
        preserve_queued_exposure_reducing: bool = False,
    ) -> None:
        self.store.record_runtime_boot_failure(
            boot=boot,
            failure_reason=failure_reason,
            failed_at=failed_at,
            patch=patch,
            prune_state=prune_state,
            preserve_queued_exposure_reducing=(
                preserve_queued_exposure_reducing
            ),
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

    # AGENT HANDSHAKE: Bridge-visible view of open positions. Returns a stable
    # list-of-dicts regardless of how the underlying state shapes the data.
    # Used by ``GET /v2/positions/reconcile`` and any caller that needs an
    # explicit positions surface rather than digging into ``get_state()``.
    def get_open_positions(self) -> list[dict[str, Any]]:
        state = self.get_state() or {}
        raw = state.get("positions")
        if raw is None:
            raw = state.get("open_positions")
        if raw is None:
            raw = state.get("openPositions")
        out: list[dict[str, Any]] = []
        if isinstance(raw, dict):
            for sym, pos in raw.items():
                if not isinstance(pos, dict):
                    continue
                item = dict(pos)
                if "symbol" not in item or not str(item.get("symbol") or "").strip():
                    item["symbol"] = str(sym)
                out.append(item)
        elif isinstance(raw, list):
            for pos in raw:
                if isinstance(pos, dict):
                    out.append(dict(pos))
        return out

    # AGENT HANDSHAKE: Best-effort drain hook invoked by the bridge during
    # graceful shutdown. Today this is a no-op because command persistence is
    # synchronous (SQLAlchemy commit-per-call); the explicit method exists so
    # the contract is visible and future asynchronous primitives (batch
    # writers, outbox flushers) have a single insertion point.
    def drain(self) -> None:
        """Signal graceful shutdown — fence further command submissions.

        ``submit_command`` is synchronous, so any in-flight call completes on
        its own thread before ASGI teardown. What this flag prevents is the
        opposite race: an inbound POST arriving *after* the lifespan begins
        shutdown but before the socket closes. With the fence, those writes
        get a clear 503 and the EA can back off cleanly.

        Reads (``/v2/state``, ``/v2/health``, etc.) are intentionally not
        fenced — operators want visibility right up to the last second.
        """
        self._draining = True

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

    def get_command_window_summary(self, *, start_ts: float, end_ts: float) -> dict[str, Any]:
        return self.store.get_command_window_summary(start_ts=start_ts, end_ts=end_ts)

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
        paper_adapter = _paper_execution_adapter()
        delivered_payload, acked_payload = paper_adapter.build_simulated_ack_payloads(
            cmd,
            tick=tick,
        )
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
