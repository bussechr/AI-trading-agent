# AGENT: ROLE: DB-backed runtime store for commands, ticks, reports, decisions, governance events, and bridge state patches.
# AGENT: ENTRYPOINT: constructed by `fxstack/runtime/service.py`.
# AGENT: PRIMARY INPUTS: `ExecutionCommand`, `ExecutionAck`, state patch dicts, decision payloads, tick/report payloads.
# AGENT: PRIMARY OUTPUTS: durable queue rows, command events, state snapshots, tick history, reports.
# AGENT: DEPENDS ON: `fxstack/runtime/dto.py`, `fxstack/settings.py`, SQLAlchemy/Alembic.
# AGENT: CALLED BY: `fxstack/runtime/service.py`.
# AGENT: STATE / SIDE EFFECTS: owns command queue tables and the bridge-visible runtime state store.
# AGENT: HANDSHAKES: command enqueue/poll/ack, runtime patch persistence, readiness/state reads used by bridge and ops.
# AGENT: SEE: `docs/agents/runtime-loop.md` -> `fxstack/runtime/service.py` -> `docs/agents/bridge-and-api-handshakes.md`
from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
import json
import math
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4
import zlib

from sqlalchemy import (
    Boolean,
    JSON,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    case,
    create_engine,
    delete,
    func,
    inspect,
    or_,
    select,
    text,
    union_all,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.types import TypeDecorator

from fxstack.api.protocol_identity import BRIDGE_PROTOCOL_VERSION
from fxstack.runtime.db_tools import load_migration_heads
from fxstack.runtime.dto import (
    TICKET_OWNER_CONTRACT,
    ExecutionAck,
    ExecutionCommand,
)
from fxstack.runtime.execution_ack_attestation import (
    EXECUTION_ACK_ATTESTATION_SCHEMA,
    classify_execution_ack,
)
from fxstack.runtime.scalp_execution_authority import (
    MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
    SCALP_ADMISSION_MODE_DIRECT_DEMO,
    SCALP_ADMISSION_MODE_SIGNED,
    SCALP_ENTRY_INTENT,
    SCALP_EXECUTION_AUTHORITY_SCHEMA,
    SCALP_EXECUTION_LANE,
    authority_error as scalp_authority_error,
    command_binding_error as scalp_command_binding_error,
    expectation_from_authority as scalp_expectation_from_authority,
    expectation_from_command as scalp_expectation_from_command,
    protective_history_binding_error as scalp_protective_history_binding_error,
    validation_witness_error as scalp_validation_witness_error,
)
from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.runtime.market_source_identity import (
    AuthenticatedMarketSource,
    authenticated_market_source_from_row,
    current_authenticated_market_source,
)
from fxstack.runtime.broker_contract_state import (
    MAX_PRODUCTION_SCALP_BROKER_CONTRACT_AGE_SECS,
    account_conversion_source_symbols,
    account_conversion_tick_symbols,
    broker_contract_command_binding_error,
    broker_contract_order_cash_risk_error,
    broker_contract_order_geometry_error,
    project_account_conversion_rates,
    project_ig_mt4_authority_contract_universe,
    project_ig_mt4_selected_contract_universe,
)
from fxstack.runtime.scalp_daily_budget import classify_scalp_daily_budget_row
from fxstack.runtime.scalp_execution_boundary import (
    production_scalp_entry_deadline_epoch,
    production_scalp_immediate_entry_contract_error,
    production_scalp_market_entry_envelope_error,
)
from fxstack.runtime.scalp_rollover_guard import (
    DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY,
    PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION,
    evaluate_production_scalp_rollover_guard,
)
from fxstack.runtime.sqlite_url import ensure_sqlite_database_dir


def _get_settings() -> Any:
    from fxstack.settings import get_settings

    return get_settings()


class _CompressedJSON(TypeDecorator):
    """JSON-compatible storage with transparent compression for large values."""

    impl = JSON
    cache_ok = True
    _encoding = "zlib-json-v1"
    _encoding_key = "__fxstack_json_encoding__"
    _payload_key = "payload_base64"
    _size_key = "raw_size"
    _crc_key = "crc32"
    _minimum_size = 1024
    _maximum_size = 64 * 1024 * 1024

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        del dialect
        if value is None:
            return None
        raw = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(raw) < self._minimum_size:
            return value
        payload = base64.b64encode(zlib.compress(raw, level=6)).decode("ascii")
        wrapper = {
            self._encoding_key: self._encoding,
            self._payload_key: payload,
            self._size_key: len(raw),
            self._crc_key: int(zlib.crc32(raw)),
        }
        return wrapper if len(payload) + 128 < len(raw) else value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        del dialect
        if not isinstance(value, dict) or set(value) != {
            self._encoding_key,
            self._payload_key,
            self._size_key,
            self._crc_key,
        }:
            return value
        if value.get(self._encoding_key) != self._encoding:
            return value
        try:
            expected_size = int(value[self._size_key])
            expected_crc = int(value[self._crc_key])
            if expected_size < 0 or expected_size > self._maximum_size:
                raise ValueError("compressed_json_size_invalid")
            compressed = base64.b64decode(
                str(value[self._payload_key]).encode("ascii"),
                validate=True,
            )
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(compressed, expected_size + 1)
            if (
                len(raw) != expected_size
                or decompressor.unconsumed_tail
                or not decompressor.eof
                or decompressor.unused_data
                or int(zlib.crc32(raw)) != expected_crc
            ):
                raise ValueError("compressed_json_integrity_invalid")
            return json.loads(raw.decode("utf-8"))
        except (
            binascii.Error,
            UnicodeError,
            ValueError,
            TypeError,
            zlib.error,
        ) as exc:
            raise ValueError("compressed_json_payload_invalid") from exc


def _now() -> float:
    return float(time.time())


def _parse_iso_ts(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    txt = str(value).strip()
    if not txt:
        return 0.0
    try:
        return float(datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0.0


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


_DISABLED_SCALP_ENTRY_REASON = (
    "execution_egress_scalp_live_ingress_disabled_unvalidated_authority"
)
_EXPOSURE_REDUCING_MANAGEMENT_COMMANDS = frozenset(
    {"CLOSE", "CLOSE_ALL", "CLOSE_PARTIAL"}
)
_MT4_POSITIONS_SNAPSHOT_SCHEMA = "fxstack_mt4_positions_snapshot_v2"
def _paper_ack_attestation(
    *,
    row: dict[str, Any],
    status: str,
    ack_payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Describe an explicitly simulated paper ACK without broker semantics."""

    command_payload = dict(row.get("payload_json") or {})
    orchestration_meta = dict(ack_payload.get("orchestration_meta_json") or {})
    if not (
        str(command_payload.get("_execution_provider") or "").strip().lower() == "paper"
        and str(orchestration_meta.get("execution_provider") or "").strip().lower()
        == "paper"
        and orchestration_meta.get("paper_simulated") is True
    ):
        return None
    effective_status = str(status or "").strip().lower()
    return {
        "schema_version": EXECUTION_ACK_ATTESTATION_SCHEMA,
        "policy_scope": "paper_simulation",
        "effective_status": effective_status,
        "reported_status": effective_status,
        "mutation_state": "simulated",
        "broker_mutating": False,
        "attested": effective_status == "acked",
        "terminal": effective_status in {"acked", "failed", "duplicate"},
        "reasons": ["paper_simulation_non_broker"],
        "actuals": {
            "command_id": str(ack_payload.get("command_id") or ""),
            "cmd": str(ack_payload.get("cmd") or "").strip().upper(),
            "symbol": str(ack_payload.get("symbol") or "").strip().upper(),
            "broker_symbol": "",
            "side": str(ack_payload.get("side") or "").strip().upper(),
            "execution_type": "simulation",
            "ticket": _safe_int(ack_payload.get("ticket"), -1),
            "target_ticket": _safe_int(ack_payload.get("target_ticket"), -1),
            "magic": _safe_int(ack_payload.get("magic"), -1),
            "owner_token": str(ack_payload.get("owner_token") or ""),
            "order_comment": str(ack_payload.get("order_comment") or ""),
            "lots": ack_payload.get("actual_lots"),
            "sl_price": ack_payload.get("actual_sl_price"),
            "tp_price": ack_payload.get("actual_tp_price"),
            "open_price": orchestration_meta.get("paper_fill_price"),
        },
    }


def _execution_ack_semantic_identity(
    *,
    attestation: dict[str, Any],
    ack_payload: dict[str, Any],
) -> dict[str, Any]:
    """Return replay-stable ACK meaning, excluding transport timestamps/text."""

    return {
        "schema_version": str(attestation.get("schema_version") or ""),
        "policy_scope": str(attestation.get("policy_scope") or ""),
        "effective_status": str(attestation.get("effective_status") or ""),
        "mutation_state": str(attestation.get("mutation_state") or ""),
        "broker_mutating": bool(attestation.get("broker_mutating", False)),
        "attested": bool(attestation.get("attested", False)),
        "terminal": bool(attestation.get("terminal", False)),
        "reasons": list(attestation.get("reasons") or []),
        "actuals": dict(attestation.get("actuals") or {}),
        "error_code": _safe_int(ack_payload.get("error_code"), 0),
    }


def _execution_ack_terminal_safe(
    *,
    attestation: dict[str, Any],
    status: str,
    ticket: int,
) -> bool:
    """Return whether typed durable evidence proves a terminal broker outcome."""

    durable_status = str(status or "").strip().lower()
    if durable_status not in {"acked", "failed", "duplicate"}:
        return False
    if attestation.get("terminal") is not True:
        return False
    if attestation.get("broker_mutating") is not True:
        return True
    if durable_status in {"failed", "duplicate"}:
        return bool(
            int(ticket) <= 0
            and str(attestation.get("mutation_state") or "").strip().lower()
            == "not_attempted"
        )
    policy_scope = str(attestation.get("policy_scope") or "").strip()
    trusted_success_scope = policy_scope in {
        "production_mt4_exact",
        "paper_simulation",
    }
    return bool(
        int(ticket) > 0
        and attestation.get("attested") is True
        and trusted_success_scope
    )


def _enriched_execution_ack(
    *,
    ack_payload: dict[str, Any],
    attestation: dict[str, Any],
    status: str,
    count_as_trade: bool,
    store_reasons: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Persist normalized broker evidence and its durable queue decision."""

    enriched = dict(ack_payload)
    enriched["reported_status"] = str(attestation.get("reported_status") or "")
    enriched["status"] = str(status)
    enriched["mutation_state"] = str(attestation.get("mutation_state") or "")
    enriched["count_as_trade"] = bool(count_as_trade)
    enriched["execution_ack_attestation"] = dict(attestation)
    enriched["execution_ack_semantic_identity"] = _execution_ack_semantic_identity(
        attestation=attestation,
        ack_payload=ack_payload,
    )
    if store_reasons:
        enriched["store_reconciliation_reasons"] = list(store_reasons)
    return enriched


def _preserve_queued_exposure_reducing_command(
    row: dict[str, Any],
    *,
    enabled: bool,
) -> bool:
    """Return whether boot recovery may leave this protective command queued."""

    return bool(
        enabled is True
        and str(row.get("status") or "").strip().lower() == "queued"
        and str(row.get("cmd") or "").strip().upper()
        in _EXPOSURE_REDUCING_MANAGEMENT_COMMANDS
    )


def _is_exact_expired_exposure_reducing_retry(
    row: dict[str, Any],
    command: ExecutionCommand,
) -> bool:
    """Match the immutable business payload of a never-delivered exit retry."""

    if (
        str(command.cmd or "").strip().upper()
        not in _EXPOSURE_REDUCING_MANAGEMENT_COMMANDS
        or str(row.get("status") or "").strip().lower() != "expired"
        or int(row.get("delivered_count") or 0) != 0
    ):
        return False
    return bool(
        str(row.get("session_id") or "") == str(command.session_id or "")
        and str(row.get("proto") or "") == str(command.proto or "")
        and str(row.get("cmd") or "") == str(command.cmd or "")
        and str(row.get("symbol") or "") == str(command.symbol or "")
        and row.get("lots") == command.lots
        and row.get("tp_cash") == command.tp_cash
        and row.get("tp_price") == command.tp_price
        and row.get("sl_price") == command.sl_price
        and int(row.get("magic") or 0) == int(command.magic)
        and str(row.get("intent") or "") == str(command.intent or "")
        and str(row.get("trace_id") or "") == str(command.trace_id or "")
        and str(row.get("correlation_id") or "") == str(command.correlation_id or "")
        and str(row.get("thread_id") or "") == str(command.thread_id or "")
        and str(row.get("idempotency_key") or "") == str(command.idempotency_key or "")
        and str(row.get("schema_version") or "") == str(command.schema_version or "")
        and dict(row.get("orchestration_meta_json") or {})
        == dict(command.orchestration_meta_json or {})
        and dict(row.get("payload_json") or {}) == dict(command.payload or {})
    )


def _is_identifiable_scalp_entry(
    *,
    cmd: Any,
    command_id: Any,
    intent: Any,
    payload: Any,
) -> bool:
    """Identify the retired standalone-scalp BUY/SELL ingress fail closed.

    The authoritative client stamped both ``scalp:<...>`` command IDs and the
    ``scalp_live_entry`` intent, while older iterations also carried
    ``scalp_*`` certificate/config fields.  Restrict the fence to
    exposure-increasing verbs so a protective command can never be withheld
    merely because it retains scalp provenance.
    """

    if str(cmd or "").strip().upper() not in {"BUY", "SELL"}:
        return False
    normalized_intent = str(intent or "").strip().lower()
    normalized_command_id = str(command_id or "").strip().lower()
    raw_payload = dict(payload) if isinstance(payload, dict) else {}
    payload_intent = str(raw_payload.get("intent") or "").strip().lower()
    # The active production loop necessarily carries ``scalp_*`` audit fields.
    # It is not the retired standalone ingress when its canonical lane/intent
    # and signed authority binding are all present. Such entries have already
    # passed FinalEntryApproval and are revalidated atomically below against
    # the active strategy authority and execution-egress generation.
    canonical_signed_entry = bool(
        normalized_intent == SCALP_ENTRY_INTENT
        and payload_intent == SCALP_ENTRY_INTENT
        and str(raw_payload.get("strategy_lane") or "").strip().lower()
        == SCALP_EXECUTION_LANE
        and str(
            raw_payload.get("expected_strategy_admission_mode") or ""
        ).strip().lower()
        == SCALP_ADMISSION_MODE_SIGNED
    )
    if canonical_signed_entry:
        return False
    return bool(
        normalized_intent == "scalp_live_entry"
        or payload_intent == "scalp_live_entry"
        or normalized_command_id.startswith("scalp:")
        or any(
            str(key or "").strip().lower().startswith("scalp_") for key in raw_payload
        )
    )


def _is_legacy_direct_demo_entry(
    *,
    cmd: Any,
    payload: Any,
) -> bool:
    """Identify a retired direct-demo BUY/SELL before broker delivery."""

    if str(cmd or "").strip().upper() not in {"BUY", "SELL"}:
        return False
    raw_payload = dict(payload) if isinstance(payload, dict) else {}
    return (
        str(raw_payload.get("expected_strategy_admission_mode") or "").strip().lower()
        == SCALP_ADMISSION_MODE_DIRECT_DEMO
    )


def _timestamp_age_secs(
    value: Any,
    *,
    now_ts: float,
    max_future_skew_secs: float = 5.0,
) -> float | None:
    parsed = _parse_iso_ts(value)
    now = float(now_ts)
    if not math.isfinite(parsed) or parsed <= 0.0 or not math.isfinite(now):
        return None
    age = now - parsed
    if age < -max(0.0, float(max_future_skew_secs)):
        return None
    return max(0.0, float(age))


# These fields are written by operator/release transactions and must never be
# rolled back by a runtime cycle that started from an older state snapshot.
_ORCHESTRATION_LIVE_AUTHORITY_FIELDS = (
    "authority_revision",
    "enabled",
    "mode",
    "runtime_enabled",
    "queue_kill_active",
    "queue_kill_reason",
    "queue_killed_at",
    "active_pair_scope",
    "active_sleeve_scope",
    "active_intent_scope",
    "active_pair_scope_configured",
    "active_sleeve_scope_configured",
    "active_intent_scope_configured",
    "ramp_steps_pct",
    "current_stage_index",
    "current_stage_pct",
    "budget_scale",
    "promotion_pack_path",
    "signoff_records",
    "release_status",
    "bundle_run_id",
    "last_kill_reason",
    "last_kill_at",
    "purged_command_count",
)

_ORCHESTRATION_LIVE_REVISION_TRIGGER_FIELDS = tuple(
    field
    for field in _ORCHESTRATION_LIVE_AUTHORITY_FIELDS
    if field != "authority_revision"
)

_ORCHESTRATION_LIVE_STAGE_IDENTITY_FIELDS = (
    "current_stage_index",
    "current_stage_pct",
    "bundle_run_id",
)

_ORCHESTRATION_LIVE_ENTRY_EVIDENCE_FIELDS = (
    "entry_ratio_vs_baseline",
    "entry_ratio_evaluable",
    "entry_ratio_status",
    "entry_ratio_approved_count",
    "entry_ratio_submitted_count",
    "entry_ratio_accepted_count",
    "entry_ratio_observed_at",
    "entry_ratio_stage_index",
    "entry_ratio_stage_pct",
    "entry_evidence_by_pair",
)

_EXECUTION_EGRESS_SCHEMA = "fxstack_execution_egress_authority_v1"
_RELEASE_AUTHORITY_STATE_SCHEMA = "fxstack_live_release_authority_state_v1"
_RELEASE_AUTHORITY_REQUEST_SCHEMA = "fxstack_live_release_authority_request_v1"
_RELEASE_AUTHORITY_ACK_SCHEMA = "fxstack_live_release_authority_ack_v1"
_EXTERNAL_RELEASE_WITNESS_SCHEMA = "fxstack_external_release_witness_v1"
_EXECUTION_EGRESS_RUNNER_LEASE_SECS = 30.0
_RELEASE_EGRESS_IDENTITY_FIELDS = (
    "source_sha256",
    "package_merkle_sha256",
    "config_sha256",
    "manifest_file_sha256",
    "model_identity_sha256",
    "artifact_set_sha256",
    "model_set_id",
)


def _selected_state_fields(
    payload: dict[str, Any], fields: tuple[str, ...]
) -> dict[str, Any]:
    return {field: payload.get(field) for field in fields}


def _preserve_selected_state_fields(
    *,
    incoming: dict[str, Any],
    current: dict[str, Any],
    fields: tuple[str, ...],
) -> None:
    for field in fields:
        if field in current:
            incoming[field] = current[field]
        else:
            incoming.pop(field, None)


class PostgresRuntimeStore:
    # Transaction-scoped PostgreSQL advisory lock shared by every API/runtime
    # process that can change command state. The per-instance RLock only
    # protects threads in one process and cannot make the reconciliation fence
    # atomic across multiple workers.
    _EXECUTION_QUEUE_ADVISORY_LOCK_KEY = 5068883552507806257
    _LOCKED_RUNTIME_STATE_CACHE_KEY = "fxstack_locked_runtime_state_v1"

    def __init__(
        self,
        database_url: str,
        *,
        requeue_age_secs: float = 90.0,
        connect_retries: int = 5,
    ) -> None:
        self._migration_root, self._expected_migration_heads = load_migration_heads()
        self.database_url = ensure_sqlite_database_dir(
            database_url, base_dir=Path.cwd()
        )
        self.requeue_age_secs = float(max(5.0, requeue_age_secs))
        self.engine: Engine = create_engine(
            self.database_url,
            future=True,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        self.meta = MetaData()
        self._lock = threading.RLock()

        # AGENT STATE: Table definitions here form the durable contract that backs bridge `/v2/state`, `/v2/commands`, and runtime recovery.
        self.commands = Table(
            "commands",
            self.meta,
            Column("command_id", String(128), primary_key=True),
            Column("session_id", String(64), nullable=False),
            Column("proto", String(16), nullable=False, default="v2"),
            Column("cmd", String(32), nullable=False),
            Column("symbol", String(16), nullable=True),
            Column("lots", Float, nullable=True),
            Column("tp_cash", Float, nullable=True),
            Column("tp_price", Float, nullable=True),
            Column("sl_price", Float, nullable=True),
            Column("magic", Integer, nullable=True),
            Column("intent", String(32), nullable=True),
            Column("trace_id", String(128), nullable=True),
            Column("correlation_id", String(192), nullable=True),
            Column("thread_id", String(192), nullable=True),
            Column("idempotency_key", String(128), nullable=True),
            Column("schema_version", String(64), nullable=True),
            Column("orchestration_meta_json", JSON, nullable=True),
            Column("status", String(32), nullable=False),
            Column("created_at", Float, nullable=False),
            Column("updated_at", Float, nullable=False),
            Column("expires_at", Float, nullable=False),
            Column("delivered_count", Integer, nullable=False, default=0),
            Column("reason", Text, nullable=True),
            Column("payload_json", JSON, nullable=True),
            Column("ack_json", JSON, nullable=True),
            Column("ack_policy_scope", String(64), nullable=True),
            Column("ack_attestation_schema", String(96), nullable=True),
            Column(
                "ack_terminal_safe",
                Boolean,
                nullable=False,
                default=False,
                server_default=text("false"),
            ),
            Column("ack_ticket", Integer, nullable=True),
            Column("ack_mutation_state", String(32), nullable=True),
        )
        Index("ix_commands_status", self.commands.c.status)
        Index("ix_commands_created", self.commands.c.created_at)
        Index("ix_commands_expires", self.commands.c.expires_at)
        Index(
            "ix_commands_status_ack_terminal_safe",
            self.commands.c.status,
            self.commands.c.ack_terminal_safe,
        )

        self.command_events = Table(
            "command_events",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("command_id", String(128), nullable=False),
            Column("event_status", String(32), nullable=False),
            Column("reason", Text, nullable=True),
            Column("ts", Float, nullable=False),
            Column("event_json", JSON, nullable=True),
        )
        Index("ix_command_events_command_id", self.command_events.c.command_id)
        Index("ix_command_events_ts", self.command_events.c.ts)

        self.market_ticks = Table(
            "market_ticks",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("symbol", String(16), nullable=False),
            Column("bid", Float, nullable=True),
            Column("ask", Float, nullable=True),
            Column("spread", Float, nullable=True),
            Column("ts", Float, nullable=False),
            Column("market_source_schema", String(96), nullable=True),
            Column("market_source_id", String(64), nullable=True),
            Column("market_source_authenticated", Integer, nullable=False, default=0),
            Column("broker_account_scope", String(128), nullable=True),
            Column("broker_venue_id", String(64), nullable=True),
            Column("producer_identity", String(128), nullable=True),
            Column("producer_instance_id", String(128), nullable=True),
            Column("terminal_lease_scope", String(128), nullable=True),
            Column("credential_generation_id", String(128), nullable=True),
            Column("bridge_protocol_version", String(32), nullable=True),
            Column("raw_json", JSON, nullable=True),
        )
        Index("ix_market_ticks_symbol", self.market_ticks.c.symbol)
        Index("ix_market_ticks_ts", self.market_ticks.c.ts)
        Index(
            "ix_market_ticks_source_symbol_ts",
            self.market_ticks.c.market_source_id,
            self.market_ticks.c.symbol,
            self.market_ticks.c.ts,
        )

        self.reports = Table(
            "reports",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", Float, nullable=False),
            Column("report_text", Text, nullable=True),
            Column("report_json", JSON, nullable=True),
        )
        Index("ix_reports_ts", self.reports.c.ts)

        self.decision_snapshots = Table(
            "decision_snapshots",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", Float, nullable=False),
            Column("vol", Float, nullable=True),
            Column("decisions_json", _CompressedJSON(), nullable=True),
            Column("diagnostics_json", _CompressedJSON(), nullable=True),
        )
        Index("ix_decision_snapshots_ts", self.decision_snapshots.c.ts)

        self.orchestration_runs = Table(
            "orchestration_runs",
            self.meta,
            Column("run_id", String(64), primary_key=True),
            Column("cycle_id", String(128), nullable=False),
            Column("thread_id", String(192), nullable=False),
            Column("correlation_id", String(192), nullable=False),
            Column("pair", String(16), nullable=False),
            Column("ts_utc", Float, nullable=False),
            Column("runtime_mode", String(16), nullable=False),
            Column("latency_ms", Integer, nullable=False),
            Column("fallback_used", Integer, nullable=False, default=0),
            Column("version_bundle_json", JSON, nullable=False),
            Column("packet_json", JSON, nullable=False),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_orchestration_runs_pair", self.orchestration_runs.c.pair)
        Index("ix_orchestration_runs_cycle_id", self.orchestration_runs.c.cycle_id)
        Index("ix_orchestration_runs_thread_id", self.orchestration_runs.c.thread_id)
        Index("ix_orchestration_runs_ts_utc", self.orchestration_runs.c.ts_utc)
        Index(
            "ix_orchestration_runs_runtime_mode", self.orchestration_runs.c.runtime_mode
        )
        Index(
            "ix_orchestration_runs_correlation_id",
            self.orchestration_runs.c.correlation_id,
            unique=True,
        )

        self.agent_proposals = Table(
            "agent_proposals",
            self.meta,
            Column("proposal_id", String(64), primary_key=True),
            Column("run_id", String(64), nullable=False),
            Column("agent_id", String(128), nullable=False),
            Column("phase", String(64), nullable=False),
            Column("intent", String(32), nullable=False),
            Column("side", String(16), nullable=False),
            Column("confidence", Float, nullable=False),
            Column("expected_edge_bps", Float, nullable=False),
            Column("uncertainty", Float, nullable=False),
            Column("risk_cost", Float, nullable=False),
            Column("ttl_ms", Integer, nullable=False),
            Column("evidence_json", JSON, nullable=False),
            Column("constraints_json", JSON, nullable=False),
            Column("advisory_only", Integer, nullable=False, default=1),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_agent_proposals_run_id", self.agent_proposals.c.run_id)
        Index("ix_agent_proposals_agent_id", self.agent_proposals.c.agent_id)
        Index("ix_agent_proposals_phase", self.agent_proposals.c.phase)

        self.governed_decisions = Table(
            "governed_decisions",
            self.meta,
            Column("decision_id", String(64), primary_key=True),
            Column("run_id", String(64), nullable=False),
            Column("runtime_mode", String(16), nullable=False, default="shadow"),
            Column("allowed", Integer, nullable=False),
            Column("selected_action", String(64), nullable=False),
            Column("command_preview_json", JSON, nullable=True),
            Column("blocking_reasons_json", JSON, nullable=False),
            Column("approval_state", String(32), nullable=False),
            Column("governor_version", String(128), nullable=False),
            Column("version_bundle_json", JSON, nullable=True),
            Column("invariants_ok", Integer, nullable=False),
            Column("created_at", Float, nullable=False),
        )
        Index(
            "ix_governed_decisions_run_id",
            self.governed_decisions.c.run_id,
            unique=True,
        )
        Index(
            "ix_governed_decisions_runtime_mode", self.governed_decisions.c.runtime_mode
        )

        self.agent_traces = Table(
            "agent_traces",
            self.meta,
            Column("trace_id", String(128), primary_key=True),
            Column("run_id", String(64), nullable=False),
            Column("pair", String(16), nullable=True),
            Column("trace_json", JSON, nullable=False),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_agent_traces_run_id", self.agent_traces.c.run_id)
        Index("ix_agent_traces_created_at", self.agent_traces.c.created_at)

        self.approval_events = Table(
            "approval_events",
            self.meta,
            Column("event_id", String(64), primary_key=True),
            Column("subject_type", String(64), nullable=False),
            Column("subject_id", String(128), nullable=False),
            Column("approver", String(128), nullable=False),
            Column("decision", String(32), nullable=False),
            Column("reason", Text, nullable=True),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_approval_events_subject_type", self.approval_events.c.subject_type)
        Index("ix_approval_events_subject_id", self.approval_events.c.subject_id)
        Index("ix_approval_events_created_at", self.approval_events.c.created_at)

        self.experiment_proposals = Table(
            "experiment_proposals",
            self.meta,
            Column("experiment_id", String(64), primary_key=True),
            Column("source_run_id", String(64), nullable=True),
            Column("hypothesis", Text, nullable=False),
            Column("change_set_json", JSON, nullable=False),
            Column("evaluation_plan_json", JSON, nullable=False),
            Column("risk_notes_json", JSON, nullable=False),
            Column("evidence_refs_json", JSON, nullable=False),
            Column("prompt_hash", String(128), nullable=False, default=""),
            Column("tool_trace_hash", String(128), nullable=False, default=""),
            Column("model_id", String(128), nullable=False, default=""),
            Column("decision_seed", Integer, nullable=False, default=0),
            Column("input_artefact_refs_json", JSON, nullable=False),
            Column("config_diff_json", JSON, nullable=False),
            Column("replay_window", String(128), nullable=False, default=""),
            Column("artifact_root", Text, nullable=False, default=""),
            Column("latest_stage", String(64), nullable=False, default=""),
            Column("latest_promotion_id", String(64), nullable=False, default=""),
            Column("approval_status", String(32), nullable=False),
            Column("created_at", Float, nullable=False),
        )
        Index(
            "ix_experiment_proposals_approval_status",
            self.experiment_proposals.c.approval_status,
        )
        Index(
            "ix_experiment_proposals_created_at", self.experiment_proposals.c.created_at
        )
        Index(
            "ix_experiment_proposals_source_run_id",
            self.experiment_proposals.c.source_run_id,
        )

        self.experiment_promotions = Table(
            "experiment_promotions",
            self.meta,
            Column("promotion_id", String(64), primary_key=True),
            Column("experiment_id", String(64), nullable=False),
            Column("prompt_hash", String(128), nullable=False, default=""),
            Column("tool_trace_hash", String(128), nullable=False, default=""),
            Column("model_id", String(128), nullable=False, default=""),
            Column("config_diff_json", JSON, nullable=False),
            Column("replay_window", String(128), nullable=False, default=""),
            Column("replay_results_json", JSON, nullable=False),
            Column("approval_records_json", JSON, nullable=False),
            Column("paper_results_json", JSON, nullable=False),
            Column("canary_results_json", JSON, nullable=False),
            Column("release_manifest_ref", Text, nullable=False, default=""),
            Column("rollback_metadata_json", JSON, nullable=False),
            Column("artefact_hashes_json", JSON, nullable=False),
            Column("status", String(32), nullable=False),
            Column("created_at", Float, nullable=False),
            Column("updated_at", Float, nullable=False),
        )
        Index(
            "ix_experiment_promotions_experiment_id",
            self.experiment_promotions.c.experiment_id,
        )
        Index("ix_experiment_promotions_status", self.experiment_promotions.c.status)
        Index(
            "ix_experiment_promotions_created_at",
            self.experiment_promotions.c.created_at,
        )

        self.experiment_lineage = Table(
            "experiment_lineage",
            self.meta,
            Column("experiment_id", String(64), primary_key=True),
            Column("proposal_ref", Text, nullable=False, default=""),
            Column("review_ref", Text, nullable=False, default=""),
            Column("replay_refs_json", JSON, nullable=False),
            Column("paper_pack_ref", Text, nullable=False, default=""),
            Column("canary_pack_ref", Text, nullable=False, default=""),
            Column("promotion_decision_ref", Text, nullable=False, default=""),
            Column("rollback_plan_ref", Text, nullable=False, default=""),
            Column("release_manifest_ref", Text, nullable=False, default=""),
            Column("reflection_memory_ref", Text, nullable=False, default=""),
            Column("latest_stage", String(64), nullable=False, default=""),
            Column("latest_promotion_id", String(64), nullable=False, default=""),
            Column("approval_status", String(32), nullable=False, default=""),
            Column("evidence_refs_json", JSON, nullable=False),
            Column("promotion_ids_json", JSON, nullable=False),
            Column("approval_event_ids_json", JSON, nullable=False),
            Column("updated_at", Float, nullable=False),
        )
        Index(
            "ix_experiment_lineage_latest_stage", self.experiment_lineage.c.latest_stage
        )
        Index("ix_experiment_lineage_updated_at", self.experiment_lineage.c.updated_at)

        self.governance_events = Table(
            "governance_events",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", Float, nullable=False),
            Column("event_type", String(64), nullable=False),
            Column("reason", Text, nullable=True),
            Column("payload_json", JSON, nullable=True),
        )
        Index("ix_governance_events_ts", self.governance_events.c.ts)
        Index("ix_governance_events_type", self.governance_events.c.event_type)

        self.feature_push_outbox = Table(
            "feature_push_outbox",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("outbox_key", String(128), nullable=False),
            Column("pair", String(16), nullable=False),
            Column("feature_service", String(128), nullable=False),
            Column("entity_key", String(128), nullable=False),
            Column("event_timestamp", Float, nullable=False),
            Column("feature_version", String(128), nullable=True),
            Column("checksum", String(128), nullable=True),
            Column("payload_json", JSON, nullable=False),
            Column("status", String(32), nullable=False),
            Column("attempt_count", Integer, nullable=False, default=0),
            Column("claimed_by", String(128), nullable=True),
            Column("claimed_at", Float, nullable=True),
            Column("last_error", Text, nullable=True),
            Column("created_at", Float, nullable=False),
            Column("updated_at", Float, nullable=False),
            Column("delivered_at", Float, nullable=True),
        )
        Index(
            "ix_feature_push_outbox_key",
            self.feature_push_outbox.c.outbox_key,
            unique=True,
        )
        Index("ix_feature_push_outbox_status", self.feature_push_outbox.c.status)
        Index("ix_feature_push_outbox_pair", self.feature_push_outbox.c.pair)
        Index(
            "ix_feature_push_outbox_created_at", self.feature_push_outbox.c.created_at
        )
        Index(
            "ix_feature_push_outbox_entity_key", self.feature_push_outbox.c.entity_key
        )

        self.feature_push_audit = Table(
            "feature_push_audit",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("outbox_key", String(128), nullable=False),
            Column("pair", String(16), nullable=False),
            Column("feature_service", String(128), nullable=False),
            Column("entity_key", String(128), nullable=False),
            Column("event_timestamp", Float, nullable=False),
            Column("status", String(32), nullable=False),
            Column("worker_id", String(128), nullable=True),
            Column("message", Text, nullable=True),
            Column("payload_json", JSON, nullable=False),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_feature_push_audit_outbox_key", self.feature_push_audit.c.outbox_key)
        Index("ix_feature_push_audit_status", self.feature_push_audit.c.status)
        Index("ix_feature_push_audit_created_at", self.feature_push_audit.c.created_at)

        self.feature_parity_audit = Table(
            "feature_parity_audit",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("pair", String(16), nullable=False),
            Column("feature_service", String(128), nullable=False),
            Column("entity_key", String(128), nullable=False),
            Column("event_timestamp", Float, nullable=False),
            Column("source", String(32), nullable=False),
            Column("parity_ok", Integer, nullable=False),
            Column("drift_score", Float, nullable=True),
            Column("message", Text, nullable=True),
            Column("payload_json", JSON, nullable=False),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_feature_parity_audit_pair", self.feature_parity_audit.c.pair)
        Index(
            "ix_feature_parity_audit_service",
            self.feature_parity_audit.c.feature_service,
        )
        Index(
            "ix_feature_parity_audit_created_at", self.feature_parity_audit.c.created_at
        )

        self.runtime_state = Table(
            "runtime_state",
            self.meta,
            Column("id", Integer, primary_key=True),
            Column("snapshot_json", JSON, nullable=False),
            Column("updated_at", Float, nullable=False),
        )

        self.model_runs = Table(
            "model_runs",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("run_id", String(128), nullable=False),
            Column("pair", String(16), nullable=False),
            Column("timeframe", String(16), nullable=True),
            Column("model_family", String(64), nullable=False),
            Column("artifact_path", Text, nullable=False),
            Column("metadata_json", JSON, nullable=True),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_model_runs_run_id", self.model_runs.c.run_id, unique=True)
        Index("ix_model_runs_pair", self.model_runs.c.pair)

        self.model_artifacts = Table(
            "model_artifacts",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("model_set_id", String(128), nullable=False),
            Column("pair", String(16), nullable=False),
            Column("artifact_type", String(64), nullable=False),
            Column("artifact_path", Text, nullable=False),
            Column("checksum", String(128), nullable=True),
            Column("metadata_json", JSON, nullable=True),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_model_artifacts_set", self.model_artifacts.c.model_set_id)

        self.active_model_sets = Table(
            "active_model_sets",
            self.meta,
            Column("pair", String(16), primary_key=True),
            Column("model_set_id", String(128), nullable=False),
            Column("registry_path", Text, nullable=False),
            Column("artifacts_json", JSON, nullable=False),
            Column("metadata_json", JSON, nullable=True),
            Column("enabled", Integer, nullable=False, default=1),
            Column("updated_at", Float, nullable=False),
        )
        Index("ix_active_model_sets_enabled", self.active_model_sets.c.enabled)

        self._connect_with_retry(max(1, int(connect_retries)))
        self._bootstrap_schema()
        self._ensure_state_row()
        self.cleanup_expired_commands()

    def _bootstrap_schema(self) -> None:
        s = _get_settings()
        allow_create_all = bool(getattr(s, "runtime_allow_create_all", False))
        check = self.verify_required_tables()
        missing = list(check.get("missing_tables", check.get("missing", [])) or [])
        migration = dict(check.get("migration") or {})
        if missing and allow_create_all and not str(migration.get("error") or ""):
            self.meta.create_all(self.engine)
            check = self.verify_required_tables()
            missing = list(check.get("missing_tables", check.get("missing", [])) or [])
        if not bool(check.get("ok")):
            migration = dict(check.get("migration") or {})
            migration_error = str(migration.get("error") or "")
            raise RuntimeError(
                "runtime schema verification failed: "
                + f"missing_tables={sorted(missing)} "
                + f"migration_ok={bool(migration.get('ok'))} "
                + (f"migration_error={migration_error} " if migration_error else "")
                + "Run `python -I -m fxstack.runtime.db_tools migrate` with "
                + "FXSTACK_PROJECT_ROOT set before starting runtime/bridge."
            )

    def _connect_with_retry(self, retries: int) -> None:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                with self.engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return
            except Exception as exc:  # pragma: no cover - environment dependent
                last_exc = exc
                if attempt >= retries:
                    break
                time.sleep(min(5.0, 0.5 * attempt))
        if last_exc is not None:
            raise last_exc

    def verify_required_tables(self) -> dict[str, Any]:
        required = {
            "commands",
            "command_events",
            "runtime_state",
            "market_ticks",
            "reports",
            "decision_snapshots",
            "orchestration_runs",
            "agent_proposals",
            "governed_decisions",
            "agent_traces",
            "approval_events",
            "experiment_proposals",
            "experiment_promotions",
            "experiment_lineage",
            "governance_events",
            "feature_push_outbox",
            "feature_push_audit",
            "feature_parity_audit",
            "model_runs",
            "model_artifacts",
            "active_model_sets",
        }
        expected_heads = list(getattr(self, "_expected_migration_heads", []) or [])
        migration_error = ""
        if not expected_heads:
            try:
                _, expected_heads = load_migration_heads()
            except Exception as exc:
                migration_error = f"{type(exc).__name__}: {exc}"
                missing = sorted(required)
                return {
                    "required": missing,
                    "present": [],
                    "missing": missing,
                    "missing_tables": missing,
                    "migration": {
                        "ok": False,
                        "expected_heads": [],
                        "current_revisions": [],
                        "error": migration_error,
                    },
                    "ok": False,
                }

        inspector = inspect(self.engine)
        present = set(inspector.get_table_names())
        missing = sorted(required - present)
        current_revisions: list[str] = []
        migration_ok = False
        try:
            if "alembic_version" in present:
                with self.engine.connect() as conn:
                    rows = conn.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).fetchall()
                current_revisions = sorted({str(r[0]) for r in rows if r and r[0]})
            migration_ok = bool(expected_heads) and set(current_revisions) == set(
                expected_heads
            )
        except Exception as exc:
            migration_error = f"{type(exc).__name__}: {exc}"

        ok = len(missing) == 0 and bool(migration_ok)
        return {
            "required": sorted(required),
            "present": sorted(present),
            "missing": missing,
            "missing_tables": missing,
            "migration": {
                "ok": bool(migration_ok),
                "expected_heads": expected_heads,
                "current_revisions": current_revisions,
                "error": migration_error,
            },
            "ok": ok,
        }

    def _ensure_state_row(self) -> None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(
                    self.runtime_state.c.id,
                    self.runtime_state.c.snapshot_json,
                ).where(self.runtime_state.c.id == 1)
            ).fetchone()
            if row is None:
                conn.execute(
                    self.runtime_state.insert().values(
                        id=1,
                        snapshot_json={
                            "system_status": "starting",
                            "last_heartbeat": None,
                            "equity": 0.0,
                            "margin": 0.0,
                            "freemargin": 0.0,
                            "leverage": 0.0,
                            "positions": [],
                            "signals_sent": 0,
                            "trades_executed": 0,
                            "last_signal": None,
                            "last_ack": None,
                            "agent_decisions": [],
                            "agent_diagnostics": {},
                            "monitor": {},
                            "vol": 0.0,
                            "governance": {},
                            "risk_envelope": {},
                            "current_thought": "",
                            # Broker egress is fail-closed until an externally
                            # witnessed release generation is acknowledged by
                            # the exact runner boot and atomically activated.
                            "execution_egress_enabled": False,
                            "execution_egress_authority": {
                                "schema_version": _EXECUTION_EGRESS_SCHEMA,
                                "enabled": False,
                                "reason": "release_authority_not_active",
                                "generation_id": "",
                                "request_sha256": "",
                                "runtime_boot_id": "",
                                "updated_at": _now(),
                            },
                            "last_update": _now(),
                        },
                        updated_at=_now(),
                    )
                )
            else:
                snapshot = dict(row[1] if isinstance(row[1], dict) else {})
                if "execution_egress_enabled" not in snapshot:
                    snapshot["execution_egress_enabled"] = False
                    snapshot["execution_egress_authority"] = {
                        "schema_version": _EXECUTION_EGRESS_SCHEMA,
                        "enabled": False,
                        "reason": "legacy_state_fail_closed",
                        "generation_id": "",
                        "request_sha256": "",
                        "runtime_boot_id": "",
                        "updated_at": _now(),
                    }
                    snapshot["last_update"] = _now()
                    conn.execute(
                        update(self.runtime_state)
                        .where(self.runtime_state.c.id == 1)
                        .values(
                            snapshot_json=snapshot,
                            updated_at=float(snapshot["last_update"]),
                        )
                    )

    def _append_command_events(
        self,
        *,
        events: list[dict[str, Any]],
        conn,
    ) -> None:
        if not events:
            return
        event_ts = _now()
        conn.execute(
            self.command_events.insert(),
            [
                {
                    "command_id": event["command_id"],
                    "event_status": event["event_status"],
                    "reason": event["reason"],
                    "ts": event_ts,
                    "event_json": event.get("payload") or {},
                }
                for event in events
            ],
        )

    def _append_command_event(
        self,
        *,
        command_id: str,
        event_status: str,
        reason: str,
        payload: dict[str, Any] | None = None,
        conn=None,
    ) -> None:
        event = {
            "command_id": command_id,
            "event_status": event_status,
            "reason": reason,
            "payload": payload or {},
        }
        if conn is not None:
            self._append_command_events(events=[event], conn=conn)
            return
        with self.engine.begin() as _conn:
            self._append_command_events(events=[event], conn=_conn)

    def cleanup_expired_commands(self) -> int:
        now = _now()
        expired_rows: list[dict[str, Any]] = []
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                rows = (
                    conn.execute(
                        select(self.commands)
                        .where(self.commands.c.status.in_(["queued", "delivered"]))
                        .where(self.commands.c.expires_at < now)
                    )
                    .mappings()
                    .all()
                )
                if not rows:
                    return 0

                expired_rows = [
                    dict(row) for row in rows if str(row.get("command_id") or "")
                ]
                if not expired_rows:
                    return 0
                conn.execute(
                    update(self.commands)
                    .where(self.commands.c.status.in_(["queued", "delivered"]))
                    .where(self.commands.c.expires_at < now)
                    .values(status="expired", updated_at=now, reason="ttl_expired")
                )
                self._append_command_events(
                    conn=conn,
                    events=[
                        {
                            "command_id": str(row["command_id"]),
                            "event_status": "expired",
                            "reason": "ttl_expired",
                            "payload": {"expired_at": now},
                        }
                        for row in expired_rows
                    ],
                )
        return len(expired_rows)

    def quarantine_stale_delivered(self, *, age_secs: float) -> int:
        """Fence stale deliveries whose broker outcome is unknown.

        A delivered command may already have changed broker state even when
        its ACK never reached the bridge. Redelivering it would therefore risk
        repeating an OPEN, CLOSE, or CLOSE_ALL after a restart. Quarantined
        rows remain durable and ACK-reconcilable, but ``poll_next_command``
        cannot select them because their status is no longer ``queued``.
        """
        now = _now()
        cutoff = now - max(1.0, float(age_secs))
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                rows = (
                    conn.execute(
                        update(self.commands)
                        .where(self.commands.c.status == "delivered")
                        .where(self.commands.c.updated_at <= cutoff)
                        .where(self.commands.c.expires_at >= now)
                        .values(
                            status="reconcile_required",
                            updated_at=now,
                            reason="stale_delivery_outcome_unknown",
                        )
                        .returning(
                            self.commands.c.command_id,
                            self.commands.c.delivered_count,
                        )
                    )
                    .mappings()
                    .all()
                )
                if not rows:
                    return 0
                self._append_command_events(
                    conn=conn,
                    events=[
                        {
                            "command_id": str(row["command_id"]),
                            "event_status": "reconcile_required",
                            "reason": "stale_delivery_outcome_unknown",
                            "payload": {
                                "quarantined_at": now,
                                "previous_status": "delivered",
                                "delivered_count": int(
                                    row.get("delivered_count", 0) or 0
                                ),
                                "reconciliation_required": True,
                            },
                        }
                        for row in rows
                    ],
                )
        return len(rows)

    def purge_pending_commands(
        self,
        *,
        reason: str,
        intents: set[str] | None = None,
        include_delivered: bool = True,
        preserve_queued_exposure_reducing: bool = False,
    ) -> int:
        now = _now()
        normalized_reason = (
            str(reason or "runtime_restart_purged").strip() or "runtime_restart_purged"
        )
        normalized_intents = {
            str(item or "").strip().upper()
            for item in (intents or set())
            if str(item or "").strip()
        }
        updated = 0
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                purge_statuses = (
                    ["queued", "delivered"] if include_delivered else ["queued"]
                )
                stmt = select(self.commands).where(
                    self.commands.c.status.in_(purge_statuses)
                )
                if normalized_intents:
                    stmt = stmt.where(
                        func.upper(func.coalesce(self.commands.c.intent, "")).in_(
                            sorted(normalized_intents)
                        )
                    )
                rows = conn.execute(stmt).mappings().all()
                purge_rows = [
                    dict(row)
                    for row in rows
                    if str(row.get("command_id") or "")
                    and not _preserve_queued_exposure_reducing_command(
                        dict(row),
                        enabled=preserve_queued_exposure_reducing,
                    )
                ]
                if not purge_rows:
                    return 0
                conn.execute(
                    update(self.commands)
                    .where(
                        self.commands.c.command_id.in_(
                            [str(row["command_id"]) for row in purge_rows]
                        )
                    )
                    .values(status="expired", updated_at=now, reason=normalized_reason)
                )
                self._append_command_events(
                    conn=conn,
                    events=[
                        {
                            "command_id": str(row["command_id"]),
                            "event_status": "expired",
                            "reason": normalized_reason,
                            "payload": {
                                "purged_at": now,
                                "purge_reason": normalized_reason,
                                "previous_status": str(row.get("status") or ""),
                                "intent": str(row.get("intent") or ""),
                            },
                        }
                        for row in purge_rows
                    ],
                )
                updated = len(purge_rows)
        return updated

    def disable_execution_egress(
        self,
        *,
        reason: str,
        revoke_release: bool = True,
        preserve_queued_exposure_reducing: bool = False,
    ) -> dict[str, Any]:
        """Atomically revoke broker egress and quarantine outstanding work."""

        now_ts = _now()
        normalized_reason = str(reason or "execution_egress_disabled").strip()
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                row = conn.execute(
                    select(self.runtime_state.c.snapshot_json)
                    .where(self.runtime_state.c.id == 1)
                    .with_for_update()
                ).first()
                merged = dict(row[0] if row and isinstance(row[0], dict) else {})
                self._disable_execution_egress_in_state(
                    merged,
                    reason=normalized_reason,
                    now_ts=now_ts,
                )
                self._revoke_production_scalp_authority_in_state(
                    merged,
                    reason=normalized_reason,
                    now_ts=now_ts,
                )
                runtime_diag = dict(merged.get("runtime_diag") or {})
                live = dict(runtime_diag.get("orchestration_live") or {})
                live.update(
                    {
                        "runtime_enabled": False,
                        "queue_kill_active": True,
                        "queue_kill_reason": normalized_reason,
                        "queue_killed_at": now_ts,
                        "last_kill_reason": normalized_reason,
                        "last_kill_at": now_ts,
                        "authority_revision": max(
                            0,
                            _safe_int(live.get("authority_revision"), 0),
                        )
                        + 1,
                    }
                )
                runtime_diag["orchestration_live"] = live
                merged["runtime_diag"] = runtime_diag
                if revoke_release:
                    current = dict(merged.get("release_authority") or {})
                    current_status = str(current.get("status") or "").strip().lower()
                    if current_status in {"pending", "acknowledged", "active"}:
                        merged["release_authority"] = {
                            **current,
                            "status": "revoked",
                            "errors": list(
                                dict.fromkeys(
                                    [
                                        *list(current.get("errors") or []),
                                        normalized_reason,
                                    ]
                                )
                            ),
                            "updated_at": now_ts,
                        }
                quarantined = self._quarantine_execution_queue(
                    conn,
                    reason=normalized_reason,
                    now_ts=now_ts,
                    preserve_queued_exposure_reducing=(
                        preserve_queued_exposure_reducing
                    ),
                )
                merged["last_update"] = now_ts
                if row is None:
                    conn.execute(
                        self.runtime_state.insert().values(
                            id=1,
                            snapshot_json=merged,
                            updated_at=now_ts,
                        )
                    )
                else:
                    conn.execute(
                        update(self.runtime_state)
                        .where(self.runtime_state.c.id == 1)
                        .values(snapshot_json=merged, updated_at=now_ts)
                    )
        return {
            "execution_egress_enabled": False,
            "reason": normalized_reason,
            "quarantined_command_count": int(quarantined),
        }

    def enable_production_execution_egress(
        self,
        *,
        runtime_boot_id: str,
    ) -> dict[str, Any]:
        """Bind broker egress to the booted production runtime, never research."""

        boot_id = str(runtime_boot_id or "").strip()
        if not boot_id:
            raise ValueError("runtime_boot_id_required")
        now_ts = _now()
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                row = conn.execute(
                    select(self.runtime_state.c.snapshot_json)
                    .where(self.runtime_state.c.id == 1)
                    .with_for_update()
                ).first()
                merged = dict(row[0] if row and isinstance(row[0], dict) else {})
                runtime_diag = dict(merged.get("runtime_diag") or {})
                live = dict(runtime_diag.get("orchestration_live") or {})
                startup = dict(merged.get("runtime_startup") or {})
                if (
                    not bool(live.get("enabled", False))
                    or str(live.get("mode") or "").strip().lower() != "live"
                    or not bool(live.get("runtime_enabled", False))
                    or bool(live.get("queue_kill_active", False))
                ):
                    raise RuntimeError("production_live_authority_inactive")
                revision = max(0, _safe_int(live.get("authority_revision"), 0))
                if revision <= 0:
                    raise RuntimeError("production_live_authority_unattested")
                if str(startup.get("boot_id") or "").strip() != boot_id:
                    raise RuntimeError("production_runtime_boot_mismatch")
                if str(merged.get("runtime_status") or "").strip().lower() != "running":
                    raise RuntimeError("production_runtime_not_running")
                pair_scope = sorted(
                    {
                        str(item).strip().upper()
                        for item in list(live.get("active_pair_scope") or [])
                        if str(item).strip()
                    }
                )
                sleeve_scope = sorted(
                    {
                        str(item).strip().lower()
                        for item in list(live.get("active_sleeve_scope") or [])
                        if str(item).strip()
                    }
                )
                intent_scope = sorted(
                    {
                        str(item).strip().lower()
                        for item in list(live.get("active_intent_scope") or [])
                        if str(item).strip()
                    }
                )
                if not pair_scope or not sleeve_scope or not intent_scope:
                    raise RuntimeError("production_live_scope_incomplete")
                protective_management_only = bool(
                    set(pair_scope) == set(IG_MT4_SCALP_SYMBOLS)
                    and sleeve_scope == ["scalp"]
                    and intent_scope == ["exit"]
                )
                merged["execution_egress_enabled"] = True
                merged["execution_egress_authority"] = {
                    "schema_version": _EXECUTION_EGRESS_SCHEMA,
                    "enabled": True,
                    "source": "production_runtime",
                    "runtime_boot_id": boot_id,
                    "authority_revision": revision,
                    "pair_scope": pair_scope,
                    "sleeve_scope": sleeve_scope,
                    "intent_scope": intent_scope,
                    "protective_management_only": protective_management_only,
                    "reason": (
                        "signed_scalp_entry_invalid_protective_management_only"
                        if protective_management_only
                        else "operator_armed_production_runtime"
                    ),
                    "updated_at": now_ts,
                }
                merged["last_update"] = now_ts
                conn.execute(
                    update(self.runtime_state)
                    .where(self.runtime_state.c.id == 1)
                    .values(snapshot_json=merged, updated_at=now_ts)
                )
        return {
            "execution_egress_enabled": True,
            "source": "production_runtime",
            "runtime_boot_id": boot_id,
            "authority_revision": revision,
        }

    def compare_and_set_production_scalp_authority(
        self,
        *,
        next_authority: dict[str, Any],
        validation_witness: dict[str, Any] | None = None,
        expected_generation_id: str = "",
        expected_status: str = "",
        safety_dominant: bool = False,
    ) -> dict[str, Any]:
        """Publish or revoke the DB-owned production-scalper generation.

        Activation is possible only while the canonical production runtime
        owns broker egress for the same boot and authority revision, and only
        when every one of the 22 IG symbols has a complete authenticated
        contract row.  Per-symbol broker tradeability remains an entry-time
        gate and cannot revoke unrelated symbols.  Generic state patches
        cannot write this field.
        """

        incoming = dict(next_authority or {})
        now_ts = _now()
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                row = conn.execute(
                    select(self.runtime_state.c.snapshot_json)
                    .where(self.runtime_state.c.id == 1)
                    .with_for_update()
                ).first()
                merged = dict(row[0] if row and isinstance(row[0], dict) else {})
                current = dict(merged.get("production_scalp_authority") or {})
                current_generation = str(current.get("generation_id") or "")
                current_status = str(current.get("status") or "").strip().lower()
                incoming_status = str(incoming.get("status") or "").strip().lower()
                incoming_generation = str(incoming.get("generation_id") or "")

                if safety_dominant:
                    if incoming_status != "revoked":
                        return {
                            "updated": False,
                            "reason": "scalp_safety_transition_must_revoke",
                            "authority": current,
                        }
                    if current:
                        incoming = {
                            **current,
                            "status": "revoked",
                            "reason": str(
                                incoming.get("reason")
                                or "scalp_authority_safety_revoked"
                            ),
                            "updated_at": now_ts,
                        }
                    else:
                        incoming = {
                            "schema_version": SCALP_EXECUTION_AUTHORITY_SCHEMA,
                            "status": "revoked",
                            "source": "production_runtime",
                            "generation_id": incoming_generation,
                            "reason": str(
                                incoming.get("reason")
                                or "scalp_authority_safety_revoked"
                            ),
                            "updated_at": now_ts,
                        }
                else:
                    if expected_generation_id and current_generation != str(
                        expected_generation_id
                    ):
                        return {
                            "updated": False,
                            "reason": "scalp_authority_generation_changed",
                            "authority": current,
                        }
                    if (
                        expected_status
                        and current_status != str(expected_status).strip().lower()
                    ):
                        return {
                            "updated": False,
                            "reason": "scalp_authority_status_changed",
                            "authority": current,
                        }
                    if incoming_status != "active":
                        return {
                            "updated": False,
                            "reason": "scalp_authority_activation_invalid",
                            "authority": current,
                        }
                    if (
                        current_status == "active"
                        and current_generation
                        and current_generation != incoming_generation
                    ):
                        return {
                            "updated": False,
                            "reason": "scalp_authority_singleton_busy",
                            "authority": current,
                        }
                    expectation = scalp_expectation_from_authority(incoming)
                    validation_failure = scalp_authority_error(
                        incoming,
                        expectation=expectation,
                    )
                    if validation_failure:
                        return {
                            "updated": False,
                            "reason": str(validation_failure),
                            "authority": current,
                        }
                    validation_witness_failure = scalp_validation_witness_error(
                        validation_witness,
                        authority=incoming,
                        now_epoch=now_ts,
                    )
                    if validation_witness_failure:
                        return {
                            "updated": False,
                            "reason": str(validation_witness_failure),
                            "authority": current,
                        }

                    egress = dict(merged.get("execution_egress_authority") or {})
                    if (
                        not bool(merged.get("execution_egress_enabled", False))
                        or not bool(egress.get("enabled", False))
                        or str(egress.get("source") or "").strip().lower()
                        != "production_runtime"
                    ):
                        return {
                            "updated": False,
                            "reason": "scalp_authority_production_egress_inactive",
                            "authority": current,
                        }
                    if (
                        str(egress.get("runtime_boot_id") or "").strip()
                        != str(expectation.runtime_boot_id).strip()
                    ):
                        return {
                            "updated": False,
                            "reason": "scalp_authority_runtime_boot_changed",
                            "authority": current,
                        }
                    if _safe_int(egress.get("authority_revision")) != _safe_int(
                        expectation.authority_revision
                    ):
                        return {
                            "updated": False,
                            "reason": "scalp_authority_revision_changed",
                            "authority": current,
                        }
                    startup = dict(merged.get("runtime_startup") or {})
                    if (
                        str(startup.get("boot_id") or "").strip()
                        != str(expectation.runtime_boot_id).strip()
                        or str(merged.get("runtime_status") or "").strip().lower()
                        != "running"
                    ):
                        return {
                            "updated": False,
                            "reason": "scalp_authority_runtime_not_running",
                            "authority": current,
                        }
                    runtime_diag = dict(merged.get("runtime_diag") or {})
                    live = dict(runtime_diag.get("orchestration_live") or {})
                    if (
                        not bool(live.get("enabled", False))
                        or str(live.get("mode") or "").strip().lower() != "live"
                        or not bool(live.get("runtime_enabled", False))
                        or bool(live.get("queue_kill_active", False))
                    ):
                        return {
                            "updated": False,
                            "reason": "scalp_authority_live_plane_inactive",
                            "authority": current,
                        }
                    active_pairs = {
                        str(value or "").strip().upper()
                        for value in list(live.get("active_pair_scope") or [])
                        if str(value or "").strip()
                    }
                    active_sleeves = {
                        str(value or "").strip().lower()
                        for value in list(live.get("active_sleeve_scope") or [])
                        if str(value or "").strip()
                    }
                    active_intents = {
                        str(value or "").strip().lower()
                        for value in list(live.get("active_intent_scope") or [])
                        if str(value or "").strip()
                    }
                    expected_pairs = set(expectation.symbol_scope)
                    if not expected_pairs.issubset(active_pairs):
                        return {
                            "updated": False,
                            "reason": "scalp_authority_live_pair_scope_incomplete",
                            "authority": current,
                        }
                    if "scalp" not in active_sleeves or "enter" not in active_intents:
                        return {
                            "updated": False,
                            "reason": "scalp_authority_live_scope_incomplete",
                            "authority": current,
                        }
                    admission = dict(runtime_diag.get("live_command_admission") or {})
                    pair_admission = dict(admission.get("pairs") or {})
                    if not bool(admission.get("allowed", False)) or any(
                        not bool(
                            dict(pair_admission.get(symbol) or {}).get("allowed", False)
                        )
                        for symbol in expectation.symbol_scope
                    ):
                        return {
                            "updated": False,
                            "reason": "scalp_authority_pair_admission_incomplete",
                            "authority": current,
                        }
                    if (
                        str(merged.get("broker_account_mode") or "").strip().lower()
                        not in {"demo", "real"}
                        or not str(merged.get("broker_account_scope") or "").strip()
                    ):
                        return {
                            "updated": False,
                            "reason": "scalp_authority_broker_identity_unattested",
                            "authority": current,
                        }
                    if (
                        str(merged.get("broker_account_mode") or "").strip().lower()
                        != str(expectation.account_mode or "").strip().lower()
                    ):
                        return {
                            "updated": False,
                            "reason": "scalp_authority_broker_account_mode_changed",
                            "authority": current,
                        }
                    broker_contracts = project_ig_mt4_authority_contract_universe(
                        merged,
                        now_ts=now_ts,
                        max_age_secs=(MAX_PRODUCTION_SCALP_BROKER_CONTRACT_AGE_SECS),
                    )
                    if not broker_contracts.ok:
                        return {
                            "updated": False,
                            "reason": str(broker_contracts.errors[0]),
                            "authority": current,
                        }

                merged["production_scalp_authority"] = incoming
                merged["last_update"] = now_ts
                if row is None:
                    conn.execute(
                        self.runtime_state.insert().values(
                            id=1,
                            snapshot_json=merged,
                            updated_at=now_ts,
                        )
                    )
                else:
                    conn.execute(
                        update(self.runtime_state)
                        .where(self.runtime_state.c.id == 1)
                        .values(snapshot_json=merged, updated_at=now_ts)
                    )
                return {
                    "updated": True,
                    "reason": "updated",
                    "authority": incoming,
                }

    def record_runtime_boot_state(
        self,
        *,
        boot: dict[str, Any],
        patch: dict[str, Any] | None = None,
        prune_state: bool = False,
        preserve_queued_exposure_reducing: bool = False,
    ) -> None:
        self.disable_execution_egress(
            reason="runtime_boot_requires_new_release_ack",
            revoke_release=True,
            preserve_queued_exposure_reducing=(preserve_queued_exposure_reducing),
        )
        payload = dict(patch or {})
        payload["runtime_startup"] = dict(boot or {})
        if prune_state:
            payload["__prune_stale__"] = True
        self.update_state_patch(payload)

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
        self.disable_execution_egress(
            reason="runtime_boot_failed",
            revoke_release=True,
            preserve_queued_exposure_reducing=(preserve_queued_exposure_reducing),
        )
        failure_ts = float(_now()) if failed_at is None else None
        payload = dict(patch or {})
        boot_state = dict(boot or {})
        boot_state["failure_reason"] = str(failure_reason or "")
        if failed_at is None:
            boot_state["failed_at"] = float(failure_ts)
        else:
            boot_state["failed_at"] = failed_at
        payload["runtime_startup"] = boot_state
        if prune_state:
            payload["__prune_stale__"] = True
        self.update_state_patch(payload)
        self.record_governance_event(
            event_type="runtime_startup_failed",
            reason=str(failure_reason or ""),
            payload=boot_state,
            ts=failure_ts,
        )

    def record_governance_event(
        self,
        *,
        event_type: str,
        reason: str = "",
        payload: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        event_name = str(event_type or "").strip()
        if not event_name:
            raise ValueError("event_type is required")
        event_ts = float(_now() if ts is None else ts)
        with self.engine.begin() as conn:
            conn.execute(
                self.governance_events.insert().values(
                    ts=event_ts,
                    event_type=event_name,
                    reason=str(reason or ""),
                    payload_json=dict(payload or {}),
                )
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
        row = {
            "event_id": str(event_id or uuid4()),
            "subject_type": str(subject_type or ""),
            "subject_id": str(subject_id or ""),
            "approver": str(approver or ""),
            "decision": str(decision or ""),
            "reason": str(reason or ""),
            "created_at": _parse_iso_ts(created_at)
            if created_at is not None
            else _now(),
        }
        if (
            not row["subject_type"]
            or not row["subject_id"]
            or not row["approver"]
            or not row["decision"]
        ):
            raise ValueError(
                "subject_type, subject_id, approver, and decision are required"
            )
        with self.engine.begin() as conn:
            conn.execute(self.approval_events.insert().values(**row))
        return dict(row)

    def upsert_experiment_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = dict(payload or {})
        experiment_id = str(row.get("experiment_id") or "").strip()
        if not experiment_id:
            raise ValueError("experiment_id is required")
        created_at = _parse_iso_ts(row.get("created_at")) or _now()
        stored = {
            "experiment_id": experiment_id,
            "source_run_id": str(row.get("source_run_id") or "") or None,
            "hypothesis": str(row.get("hypothesis") or ""),
            "change_set_json": list(row.get("change_set") or []),
            "evaluation_plan_json": dict(row.get("evaluation_plan") or {}),
            "risk_notes_json": list(row.get("risk_notes") or []),
            "evidence_refs_json": list(row.get("evidence_refs") or []),
            "prompt_hash": str(row.get("prompt_hash") or ""),
            "tool_trace_hash": str(row.get("tool_trace_hash") or ""),
            "model_id": str(row.get("model_id") or ""),
            "decision_seed": int(row.get("decision_seed") or 0),
            "input_artefact_refs_json": list(row.get("input_artefact_refs") or []),
            "config_diff_json": dict(row.get("config_diff") or {}),
            "replay_window": str(row.get("replay_window") or ""),
            "artifact_root": str(row.get("artifact_root") or ""),
            "latest_stage": str(row.get("latest_stage") or ""),
            "latest_promotion_id": str(row.get("latest_promotion_id") or ""),
            "approval_status": str(row.get("approval_status") or "draft"),
            "created_at": created_at,
        }
        with self.engine.begin() as conn:
            conn.execute(
                delete(self.experiment_proposals).where(
                    self.experiment_proposals.c.experiment_id == experiment_id
                )
            )
            conn.execute(self.experiment_proposals.insert().values(**stored))
        return self.get_experiment_proposal(experiment_id) or {}

    def get_experiment_proposal(self, experiment_id: str) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(self.experiment_proposals).where(
                        self.experiment_proposals.c.experiment_id == str(experiment_id)
                    )
                )
                .mappings()
                .first()
            )
        return self._normalize_experiment_proposal_row(row) if row else None

    def get_experiment_proposals(
        self,
        *,
        limit: int = 200,
        approval_status: str = "",
        source_run_id: str = "",
    ) -> list[dict[str, Any]]:
        stmt = select(self.experiment_proposals)
        if str(approval_status).strip():
            stmt = stmt.where(
                self.experiment_proposals.c.approval_status == str(approval_status)
            )
        if str(source_run_id).strip():
            stmt = stmt.where(
                self.experiment_proposals.c.source_run_id == str(source_run_id)
            )
        stmt = stmt.order_by(self.experiment_proposals.c.created_at.desc()).limit(
            max(1, min(limit, 5000))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._normalize_experiment_proposal_row(row) for row in rows]

    def upsert_experiment_promotion(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = dict(payload or {})
        promotion_id = str(row.get("promotion_id") or "").strip()
        experiment_id = str(row.get("experiment_id") or "").strip()
        if not promotion_id or not experiment_id:
            raise ValueError("promotion_id and experiment_id are required")
        created_at = _parse_iso_ts(row.get("created_at")) or _now()
        updated_at = _parse_iso_ts(row.get("updated_at")) or created_at
        stored = {
            "promotion_id": promotion_id,
            "experiment_id": experiment_id,
            "prompt_hash": str(row.get("prompt_hash") or ""),
            "tool_trace_hash": str(row.get("tool_trace_hash") or ""),
            "model_id": str(row.get("model_id") or ""),
            "config_diff_json": dict(row.get("config_diff") or {}),
            "replay_window": str(row.get("replay_window") or ""),
            "replay_results_json": dict(row.get("replay_results") or {}),
            "approval_records_json": list(row.get("approval_records") or []),
            "paper_results_json": dict(row.get("paper_results") or {}),
            "canary_results_json": dict(row.get("canary_results") or {}),
            "release_manifest_ref": str(row.get("release_manifest_ref") or ""),
            "rollback_metadata_json": dict(row.get("rollback_metadata") or {}),
            "artefact_hashes_json": {
                str(key): str(value)
                for key, value in dict(row.get("artefact_hashes") or {}).items()
            },
            "status": str(row.get("status") or ""),
            "created_at": created_at,
            "updated_at": updated_at,
        }
        with self.engine.begin() as conn:
            conn.execute(
                delete(self.experiment_promotions).where(
                    self.experiment_promotions.c.promotion_id == promotion_id
                )
            )
            conn.execute(self.experiment_promotions.insert().values(**stored))
        return self.get_experiment_promotion(promotion_id) or {}

    def get_experiment_promotion(self, promotion_id: str) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(self.experiment_promotions).where(
                        self.experiment_promotions.c.promotion_id == str(promotion_id)
                    )
                )
                .mappings()
                .first()
            )
        return self._normalize_experiment_promotion_row(row) if row else None

    def get_experiment_promotions(
        self,
        *,
        limit: int = 200,
        experiment_id: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]:
        stmt = select(self.experiment_promotions)
        if str(experiment_id).strip():
            stmt = stmt.where(
                self.experiment_promotions.c.experiment_id == str(experiment_id)
            )
        if str(status).strip():
            stmt = stmt.where(self.experiment_promotions.c.status == str(status))
        stmt = stmt.order_by(self.experiment_promotions.c.created_at.desc()).limit(
            max(1, min(limit, 5000))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._normalize_experiment_promotion_row(row) for row in rows]

    def get_approval_events(
        self,
        *,
        limit: int = 200,
        subject_type: str = "",
        subject_id: str = "",
    ) -> list[dict[str, Any]]:
        stmt = select(self.approval_events)
        if str(subject_type).strip():
            stmt = stmt.where(self.approval_events.c.subject_type == str(subject_type))
        if str(subject_id).strip():
            stmt = stmt.where(self.approval_events.c.subject_id == str(subject_id))
        stmt = stmt.order_by(self.approval_events.c.created_at.desc()).limit(
            max(1, min(limit, 5000))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(row) for row in rows]

    def upsert_experiment_lineage(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = dict(payload or {})
        experiment_id = str(row.get("experiment_id") or "").strip()
        if not experiment_id:
            raise ValueError("experiment_id is required")
        updated_at = _parse_iso_ts(row.get("updated_at")) or _now()
        stored = {
            "experiment_id": experiment_id,
            "proposal_ref": str(row.get("proposal_ref") or ""),
            "review_ref": str(row.get("review_ref") or ""),
            "replay_refs_json": list(row.get("replay_refs") or []),
            "paper_pack_ref": str(row.get("paper_pack_ref") or ""),
            "canary_pack_ref": str(row.get("canary_pack_ref") or ""),
            "promotion_decision_ref": str(row.get("promotion_decision_ref") or ""),
            "rollback_plan_ref": str(row.get("rollback_plan_ref") or ""),
            "release_manifest_ref": str(row.get("release_manifest_ref") or ""),
            "reflection_memory_ref": str(row.get("reflection_memory_ref") or ""),
            "latest_stage": str(row.get("latest_stage") or ""),
            "latest_promotion_id": str(row.get("latest_promotion_id") or ""),
            "approval_status": str(row.get("approval_status") or ""),
            "evidence_refs_json": list(row.get("evidence_refs") or []),
            "promotion_ids_json": list(row.get("promotion_ids") or []),
            "approval_event_ids_json": list(row.get("approval_event_ids") or []),
            "updated_at": updated_at,
        }
        with self.engine.begin() as conn:
            conn.execute(
                delete(self.experiment_lineage).where(
                    self.experiment_lineage.c.experiment_id == experiment_id
                )
            )
            conn.execute(self.experiment_lineage.insert().values(**stored))
        return self.get_experiment_lineage(experiment_id) or {}

    def get_experiment_lineage(self, experiment_id: str) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(self.experiment_lineage).where(
                        self.experiment_lineage.c.experiment_id == str(experiment_id)
                    )
                )
                .mappings()
                .first()
            )
        return self._normalize_experiment_lineage_row(row) if row else None

    def get_experiment_lineages(
        self,
        *,
        limit: int = 200,
        latest_stage: str = "",
        approval_status: str = "",
    ) -> list[dict[str, Any]]:
        stmt = select(self.experiment_lineage)
        if str(latest_stage).strip():
            stmt = stmt.where(
                self.experiment_lineage.c.latest_stage == str(latest_stage)
            )
        if str(approval_status).strip():
            stmt = stmt.where(
                self.experiment_lineage.c.approval_status == str(approval_status)
            )
        stmt = stmt.order_by(self.experiment_lineage.c.updated_at.desc()).limit(
            max(1, min(limit, 5000))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._normalize_experiment_lineage_row(row) for row in rows]

    def enqueue_feature_push(self, payload: dict[str, Any]) -> dict[str, Any]:
        outbox_key = str(payload.get("outbox_key") or "").strip()
        pair = str(payload.get("pair") or "").upper().strip()
        feature_service = str(payload.get("feature_service") or "").strip()
        entity_key = str(payload.get("entity_key") or "").strip()
        if not outbox_key or not pair or not feature_service or not entity_key:
            raise ValueError(
                "outbox_key, pair, feature_service, and entity_key are required"
            )
        now = _now()
        row = {
            "outbox_key": outbox_key,
            "pair": pair,
            "feature_service": feature_service,
            "entity_key": entity_key,
            "event_timestamp": float(payload.get("event_timestamp") or now),
            "feature_version": str(payload.get("feature_version") or ""),
            "checksum": str(payload.get("checksum") or ""),
            "payload_json": dict(payload.get("payload_json") or payload),
            "status": str(payload.get("status") or "queued"),
            "attempt_count": int(payload.get("attempt_count") or 0),
            "claimed_by": payload.get("claimed_by"),
            "claimed_at": payload.get("claimed_at"),
            "last_error": str(payload.get("last_error") or ""),
            "created_at": now,
            "updated_at": now,
            "delivered_at": payload.get("delivered_at"),
        }
        with self._lock:
            with self.engine.begin() as conn:
                existing = conn.execute(
                    select(self.feature_push_outbox.c.id).where(
                        self.feature_push_outbox.c.outbox_key == outbox_key
                    )
                ).first()
                if existing is None:
                    conn.execute(self.feature_push_outbox.insert().values(**row))
                else:
                    conn.execute(
                        update(self.feature_push_outbox)
                        .where(self.feature_push_outbox.c.outbox_key == outbox_key)
                        .values(**{k: v for k, v in row.items() if k != "created_at"})
                    )
        return dict(row)

    def claim_feature_push_batch(
        self,
        *,
        worker_id: str,
        limit: int = 50,
        statuses: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        worker = str(worker_id or "").strip()
        if not worker:
            raise ValueError("worker_id is required")
        allowed = {
            str(item or "").strip().lower()
            for item in (statuses or {"queued", "retry"})
            if str(item or "").strip()
        }
        direct_claimable = {item for item in allowed if item != "claimed"}
        settings = _get_settings()
        now = _now()
        claim_timeout_secs = float(
            max(
                30.0,
                float(
                    getattr(settings, "feature_push_claim_timeout_secs", 120.0) or 120.0
                ),
            )
        )
        reclaim_before = float(now - claim_timeout_secs)
        bounded_limit = max(1, min(limit, 500))
        with self._lock:
            with self.engine.begin() as conn:
                claimable = or_(
                    self.feature_push_outbox.c.status.in_(sorted(direct_claimable)),
                    and_(
                        self.feature_push_outbox.c.status == "claimed",
                        self.feature_push_outbox.c.claimed_at.is_not(None),
                        self.feature_push_outbox.c.claimed_at <= reclaim_before,
                    ),
                    and_(
                        self.feature_push_outbox.c.status == "claimed",
                        self.feature_push_outbox.c.claimed_at.is_(None),
                        self.feature_push_outbox.c.updated_at <= reclaim_before,
                    ),
                )
                candidate_ids = (
                    select(self.feature_push_outbox.c.id)
                    .where(claimable)
                    .order_by(
                        self.feature_push_outbox.c.created_at.asc(),
                        self.feature_push_outbox.c.id.asc(),
                    )
                    .limit(bounded_limit)
                )
                if conn.dialect.name == "postgresql":
                    candidate_ids = candidate_ids.with_for_update(skip_locked=True)
                claimed = [
                    dict(row)
                    for row in conn.execute(
                        update(self.feature_push_outbox)
                        .where(self.feature_push_outbox.c.id.in_(candidate_ids))
                        .values(
                            status="claimed",
                            claimed_by=worker,
                            claimed_at=now,
                            updated_at=now,
                            attempt_count=func.coalesce(
                                self.feature_push_outbox.c.attempt_count, 0
                            )
                            + 1,
                        )
                        .returning(*self.feature_push_outbox.c)
                    )
                    .mappings()
                    .all()
                ]
        return sorted(
            claimed,
            key=lambda row: (
                float(row.get("created_at") or 0.0),
                int(row.get("id") or 0),
            ),
        )

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
        conn=None,
    ) -> dict[str, Any]:
        now = _now()
        row = {
            "outbox_key": str(outbox_key),
            "pair": str(pair).upper().strip(),
            "feature_service": str(feature_service),
            "entity_key": str(entity_key),
            "event_timestamp": float(event_timestamp),
            "status": str(status).lower().strip(),
            "worker_id": str(worker_id) if worker_id else None,
            "message": str(message or ""),
            "payload_json": dict(payload or {}),
            "created_at": now,
        }
        if conn is not None:
            conn.execute(self.feature_push_audit.insert().values(**row))
        else:
            with self.engine.begin() as inner:
                inner.execute(self.feature_push_audit.insert().values(**row))
        return row

    def mark_feature_push_success(
        self,
        *,
        outbox_key: str,
        worker_id: str | None = None,
        payload: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            with self.engine.begin() as conn:
                row = (
                    conn.execute(
                        select(self.feature_push_outbox).where(
                            self.feature_push_outbox.c.outbox_key == str(outbox_key)
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise KeyError(f"unknown outbox_key: {outbox_key}")
                conn.execute(
                    update(self.feature_push_outbox)
                    .where(self.feature_push_outbox.c.outbox_key == str(outbox_key))
                    .values(
                        status="succeeded",
                        delivered_at=now,
                        updated_at=now,
                        last_error="",
                    )
                )
                self.record_feature_push_audit(
                    outbox_key=str(outbox_key),
                    pair=str(row.get("pair") or ""),
                    feature_service=str(row.get("feature_service") or ""),
                    entity_key=str(row.get("entity_key") or ""),
                    event_timestamp=float(row.get("event_timestamp") or now),
                    status="succeeded",
                    payload=payload or dict(row.get("payload_json") or {}),
                    worker_id=worker_id,
                    message=message,
                    conn=conn,
                )
                result = dict(row)
                result.update(
                    {"status": "succeeded", "delivered_at": now, "updated_at": now}
                )
                return result

    def mark_feature_push_failure(
        self,
        *,
        outbox_key: str,
        worker_id: str | None = None,
        message: str,
        payload: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> dict[str, Any]:
        now = _now()
        next_status = "retry" if retryable else "failed"
        with self._lock:
            with self.engine.begin() as conn:
                row = (
                    conn.execute(
                        select(self.feature_push_outbox).where(
                            self.feature_push_outbox.c.outbox_key == str(outbox_key)
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise KeyError(f"unknown outbox_key: {outbox_key}")
                conn.execute(
                    update(self.feature_push_outbox)
                    .where(self.feature_push_outbox.c.outbox_key == str(outbox_key))
                    .values(
                        status=next_status,
                        updated_at=now,
                        last_error=str(message or ""),
                    )
                )
                self.record_feature_push_audit(
                    outbox_key=str(outbox_key),
                    pair=str(row.get("pair") or ""),
                    feature_service=str(row.get("feature_service") or ""),
                    entity_key=str(row.get("entity_key") or ""),
                    event_timestamp=float(row.get("event_timestamp") or now),
                    status=next_status,
                    payload=payload or dict(row.get("payload_json") or {}),
                    worker_id=worker_id,
                    message=message,
                    conn=conn,
                )
                result = dict(row)
                result.update(
                    {
                        "status": next_status,
                        "updated_at": now,
                        "last_error": str(message or ""),
                    }
                )
                return result

    def record_feature_parity_audit(
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
        now = _now()
        row = {
            "pair": str(pair).upper().strip(),
            "feature_service": str(feature_service),
            "entity_key": str(entity_key),
            "event_timestamp": float(event_timestamp),
            "source": str(source),
            "parity_ok": 1 if bool(parity_ok) else 0,
            "drift_score": drift_score,
            "message": str(message or ""),
            "payload_json": dict(payload or {}),
            "created_at": now,
        }
        with self.engine.begin() as conn:
            conn.execute(self.feature_parity_audit.insert().values(**row))
        return row

    def get_feature_push_outbox(
        self, *, limit: int = 200, statuses: set[str] | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(self.feature_push_outbox)
        if statuses:
            stmt = stmt.where(
                self.feature_push_outbox.c.status.in_(
                    sorted({str(s).lower().strip() for s in statuses if str(s).strip()})
                )
            )
        stmt = stmt.order_by(self.feature_push_outbox.c.id.desc()).limit(
            max(1, min(limit, 5000))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_feature_push_audit(
        self, *, limit: int = 200, statuses: set[str] | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(self.feature_push_audit)
        if statuses:
            stmt = stmt.where(
                self.feature_push_audit.c.status.in_(
                    sorted({str(s).lower().strip() for s in statuses if str(s).strip()})
                )
            )
        stmt = stmt.order_by(self.feature_push_audit.c.id.desc()).limit(
            max(1, min(limit, 5000))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_feature_parity_audit(
        self, *, limit: int = 200, pair: str | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(self.feature_parity_audit)
        if pair:
            stmt = stmt.where(
                self.feature_parity_audit.c.pair == str(pair).upper().strip()
            )
        stmt = stmt.order_by(self.feature_parity_audit.c.id.desc()).limit(
            max(1, min(limit, 5000))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_feature_push_rollup(self) -> dict[str, Any]:
        with self.engine.begin() as conn:
            outbox_counts = conn.execute(
                select(self.feature_push_outbox.c.status, func.count()).group_by(
                    self.feature_push_outbox.c.status
                )
            ).all()
            audit_counts = conn.execute(
                select(self.feature_push_audit.c.status, func.count()).group_by(
                    self.feature_push_audit.c.status
                )
            ).all()
            parity_counts = conn.execute(
                select(self.feature_parity_audit.c.parity_ok, func.count()).group_by(
                    self.feature_parity_audit.c.parity_ok
                )
            ).all()
        pending = {"queued", "claimed", "retry"}
        return {
            "outbox": {
                "count": int(sum(int(v) for _, v in outbox_counts)),
                "pending": int(
                    sum(int(v) for k, v in outbox_counts if str(k) in pending)
                ),
                "by_status": {str(k): int(v) for k, v in outbox_counts},
            },
            "audit": {
                "count": int(sum(int(v) for _, v in audit_counts)),
                "by_status": {str(k): int(v) for k, v in audit_counts},
            },
            "parity": {
                "count": int(sum(int(v) for _, v in parity_counts)),
                "ok": int(sum(int(v) for k, v in parity_counts if int(k or 0) == 1)),
                "drift": int(sum(int(v) for k, v in parity_counts if int(k or 0) == 0)),
            },
        }

    def record_model_run(
        self,
        *,
        run_id: str,
        pair: str,
        timeframe: str,
        model_family: str,
        artifact_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "run_id": str(run_id),
            "pair": str(pair).upper(),
            "timeframe": str(timeframe).upper(),
            "model_family": str(model_family),
            "artifact_path": str(artifact_path),
            "metadata_json": dict(metadata or {}),
            "created_at": _now(),
        }
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(self.model_runs.c.id).where(
                    self.model_runs.c.run_id == payload["run_id"]
                )
            )
            if existing.first() is None:
                conn.execute(self.model_runs.insert().values(**payload))

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
        symbol = str(pair).upper().strip()
        if not symbol:
            raise ValueError("pair is required")
        now = _now()
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.active_model_sets.c.pair).where(
                    self.active_model_sets.c.pair == symbol
                )
            ).first()
            payload = {
                "pair": symbol,
                "model_set_id": str(model_set_id),
                "registry_path": str(registry_path),
                "artifacts_json": dict(artifacts or {}),
                "metadata_json": dict(metadata or {}),
                "enabled": 1 if enabled else 0,
                "updated_at": now,
            }
            if row is None:
                conn.execute(self.active_model_sets.insert().values(**payload))
            else:
                conn.execute(
                    update(self.active_model_sets)
                    .where(self.active_model_sets.c.pair == symbol)
                    .values(**payload)
                )

    def get_active_model_set(self, pair: str) -> dict[str, Any] | None:
        symbol = str(pair).upper().strip()
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(self.active_model_sets).where(
                        self.active_model_sets.c.pair == symbol
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def get_active_model_sets(
        self, *, enabled_only: bool = True
    ) -> dict[str, dict[str, Any]]:
        stmt = select(self.active_model_sets)
        if enabled_only:
            stmt = stmt.where(self.active_model_sets.c.enabled == 1)
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            pair = str(row.get("pair") or "").upper()
            if not pair:
                continue
            out[pair] = dict(row)
        return out

    def _execution_uncertainty_predicate(self):
        """Rows whose broker outcome cannot be proven terminal.

        ``expired`` is normally safe when a command was never delivered, but
        it remains execution-uncertain when ``delivered_count`` proves the EA
        received it before the queue lifetime ended. A typed durable safety
        bit is set only by ``ack_command`` after the selected policy proves a
        terminal outcome. Legacy terminal rows and malformed/mixed-version
        writes default false and remain fenced without JSON casts here.
        """

        resolved_statuses = ("acked", "failed", "duplicate")
        known_statuses = (
            "queued",
            "delivered",
            "reconcile_required",
            "acked",
            "failed",
            "duplicate",
            "expired",
        )
        mutates_broker_state = or_(
            self.commands.c.cmd.is_(None),
            func.upper(self.commands.c.cmd) != "INFO",
        )
        unsafe_terminal = and_(
            self.commands.c.status.in_(resolved_statuses),
            self.commands.c.ack_terminal_safe.is_(False),
        )
        return and_(
            mutates_broker_state,
            or_(
                self.commands.c.status.in_(("delivered", "reconcile_required")),
                unsafe_terminal,
                and_(
                    self.commands.c.delivered_count > 0,
                    ~self.commands.c.status.in_(resolved_statuses),
                ),
                ~self.commands.c.status.in_(known_statuses),
            ),
        )

    def _execution_uncertainty_scope(
        self,
        conn,
        *,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        """Contain unresolved outcomes only after newer broker book evidence.

        An unresolved broker-mutating command is normally an account-wide
        fence.  A current authoritative positions snapshot received after
        every unresolved transition makes the possible exposure visible to
        the normal portfolio and margin controls.  At that point only exact
        affected symbols remain quarantined; ambiguous multi-symbol commands
        (notably ``CLOSE_ALL``) always retain the account-wide fence.
        """

        predicate = self._execution_uncertainty_predicate()
        rows = [
            dict(row)
            for row in conn.execute(
                select(
                    self.commands.c.command_id,
                    self.commands.c.cmd,
                    self.commands.c.symbol,
                    self.commands.c.status,
                    self.commands.c.delivered_count,
                    self.commands.c.updated_at,
                )
                .where(predicate)
                .order_by(self.commands.c.updated_at.asc())
            )
            .mappings()
            .all()
        ]
        if not rows:
            return {
                "present": False,
                "global_blocked": False,
                "scope_contained": False,
                "scope_reason": "",
                "blocked_symbols": [],
                "rows": [],
            }

        exact_symbols: set[str] = set()
        for row in rows:
            command_verb = str(row.get("cmd") or "").strip().upper()
            symbol = str(row.get("symbol") or "").strip().upper()
            if command_verb == "CLOSE_ALL" or not symbol:
                return {
                    "present": True,
                    "global_blocked": True,
                    "scope_contained": False,
                    "scope_reason": "uncertain_command_scope_not_exact",
                    "blocked_symbols": sorted(exact_symbols),
                    "rows": rows,
                }
            exact_symbols.add(symbol)

        state_row = conn.execute(
            select(self.runtime_state.c.snapshot_json).where(
                self.runtime_state.c.id == 1
            )
        ).first()
        state = dict(
            state_row[0] if state_row and isinstance(state_row[0], dict) else {}
        )
        current_scope = str(state.get("broker_account_scope") or "").strip()
        snapshot_scope = str(
            state.get("positions_snapshot_account_scope") or ""
        ).strip()
        snapshot_received_at = _parse_iso_ts(
            state.get("positions_snapshot_received_at")
        )
        snapshot_source_ts = _parse_iso_ts(state.get("positions_snapshot_source_ts"))
        latest_uncertain_at = max(_parse_iso_ts(row.get("updated_at")) for row in rows)
        evaluated_at = float(_now() if now_ts is None else now_ts)
        settings = _get_settings()
        maximum_snapshot_age = max(
            1.0,
            float(settings.bridge_stale_heartbeat_secs),
        )
        received_age = _timestamp_age_secs(
            snapshot_received_at,
            now_ts=evaluated_at,
        )
        source_age = _timestamp_age_secs(
            snapshot_source_ts,
            now_ts=evaluated_at,
        )
        containment_checks = (
            (
                state.get("positions_snapshot_authoritative") is True,
                "positions_snapshot_not_authoritative",
            ),
            (
                state.get("positions_snapshot_source") == "positions_snapshot",
                "positions_snapshot_source_invalid",
            ),
            (
                state.get("positions_snapshot_schema")
                == _MT4_POSITIONS_SNAPSHOT_SCHEMA,
                "positions_snapshot_schema_invalid",
            ),
            (
                state.get("positions_snapshot_contract_current") is True,
                "positions_snapshot_contract_stale",
            ),
            (
                bool(str(state.get("positions_snapshot_token") or "").strip()),
                "positions_snapshot_token_missing",
            ),
            (
                bool(current_scope) and snapshot_scope == current_scope,
                "positions_snapshot_account_scope_mismatch",
            ),
            (
                isinstance(state.get("positions"), list),
                "positions_snapshot_book_invalid",
            ),
            (
                received_age is not None and received_age <= maximum_snapshot_age,
                "positions_snapshot_receipt_stale",
            ),
            (
                source_age is not None and source_age <= maximum_snapshot_age,
                "positions_snapshot_source_stale",
            ),
            (
                snapshot_received_at > latest_uncertain_at,
                "positions_snapshot_receipt_not_after_uncertainty",
            ),
            (
                snapshot_source_ts > latest_uncertain_at,
                "positions_snapshot_source_not_after_uncertainty",
            ),
        )
        for passed, failure in containment_checks:
            if not passed:
                return {
                    "present": True,
                    "global_blocked": True,
                    "scope_contained": False,
                    "scope_reason": failure,
                    "blocked_symbols": sorted(exact_symbols),
                    "rows": rows,
                }
        return {
            "present": True,
            "global_blocked": False,
            "scope_contained": True,
            "scope_reason": "newer_authoritative_positions_snapshot",
            "blocked_symbols": sorted(exact_symbols),
            "rows": rows,
        }

    @staticmethod
    def _execution_uncertainty_blocks_symbol(
        uncertainty: dict[str, Any],
        *,
        symbol: str = "",
    ) -> bool:
        if bool(uncertainty.get("global_blocked")):
            return True
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return False
        return normalized_symbol in {
            str(item or "").strip().upper()
            for item in list(uncertainty.get("blocked_symbols") or [])
        }

    def _has_execution_uncertainty(self, conn, *, symbol: str = "") -> bool:
        uncertainty = self._execution_uncertainty_scope(conn)
        return self._execution_uncertainty_blocks_symbol(
            uncertainty,
            symbol=symbol,
        )

    def _acquire_execution_queue_lock(self, conn) -> None:
        """Serialize queue admission, broker delivery, and ACK transitions."""

        if str(conn.dialect.name).strip().lower() != "postgresql":
            return
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": self._EXECUTION_QUEUE_ADVISORY_LOCK_KEY},
        )

    @staticmethod
    def _release_egress_binding_error(authority: dict[str, Any]) -> str:
        """Return why a release state cannot authorize any broker command.

        Cryptographic and evidence validation happens before the release CAS
        in :class:`RuntimeService`. This is the transaction-local structural
        check: the exact witnessed request and exact runner boot ACK must still
        agree when the durable egress bit is changed.
        """

        state = dict(authority or {})
        if str(state.get("schema_version") or "") != _RELEASE_AUTHORITY_STATE_SCHEMA:
            return "release_authority_state_schema_invalid"
        if str(state.get("status") or "").strip().lower() != "active":
            return "release_authority_not_active"
        request = dict(state.get("request") or {})
        ack = dict(state.get("ack") or {})
        witness = dict(request.get("external_witness") or {})
        if (
            str(request.get("schema_version") or "")
            != _RELEASE_AUTHORITY_REQUEST_SCHEMA
        ):
            return "release_authority_request_schema_invalid"
        if str(ack.get("schema_version") or "") != _RELEASE_AUTHORITY_ACK_SCHEMA:
            return "release_authority_ack_schema_invalid"
        if str(witness.get("schema_version") or "") != _EXTERNAL_RELEASE_WITNESS_SCHEMA:
            return "release_witness_schema_invalid"
        if not str(witness.get("signature") or "").strip():
            return "release_witness_signature_missing"
        generation_id = str(request.get("generation_id") or "").strip()
        request_sha256 = str(request.get("request_sha256") or "").strip().lower()
        runtime_boot_id = str(ack.get("runtime_boot_id") or "").strip()
        if not generation_id or not request_sha256:
            return "release_authority_identity_missing"
        if str(ack.get("generation_id") or "").strip() != generation_id:
            return "release_authority_ack_generation_mismatch"
        if str(ack.get("request_sha256") or "").strip().lower() != request_sha256:
            return "release_authority_ack_request_mismatch"
        if not runtime_boot_id:
            return "release_authority_ack_boot_missing"
        for field in _RELEASE_EGRESS_IDENTITY_FIELDS:
            request_value = str(request.get(field) or "").strip()
            ack_value = str(ack.get(field) or "").strip()
            if not request_value or ack_value != request_value:
                return f"release_authority_ack_{field}_mismatch"
        return ""

    def _locked_runtime_state(self, conn) -> dict[str, Any]:
        """Read runtime authority once per row-locked transaction."""

        transaction = conn.get_transaction()
        cached = (
            conn.info.get(self._LOCKED_RUNTIME_STATE_CACHE_KEY)
            if transaction is not None
            else None
        )
        if (
            isinstance(cached, tuple)
            and len(cached) == 2
            and cached[0] is transaction
            and isinstance(cached[1], dict)
        ):
            return dict(cached[1])

        state_row = conn.execute(
            select(self.runtime_state.c.snapshot_json)
            .where(self.runtime_state.c.id == 1)
            .with_for_update()
        ).first()
        state = dict(
            state_row[0] if state_row and isinstance(state_row[0], dict) else {}
        )
        if transaction is not None:
            # Connection ``info`` survives pool checkout, so bind cached
            # evidence to the exact RootTransaction object. A later
            # transaction must lock and read current authority again.
            conn.info[self._LOCKED_RUNTIME_STATE_CACHE_KEY] = (
                transaction,
                state,
            )
        return dict(state)

    @staticmethod
    def _execution_egress_authorization_failure_from_state(
        state: dict[str, Any],
        *,
        now_ts: float | None = None,
        command: ExecutionCommand | None = None,
    ) -> str:
        snapshot = dict(state or {})
        now = float(_now() if now_ts is None else now_ts)
        if snapshot.get("execution_egress_enabled") is not True:
            return "execution_egress_disabled"
        egress = dict(snapshot.get("execution_egress_authority") or {})
        if (
            str(egress.get("schema_version") or "") != _EXECUTION_EGRESS_SCHEMA
            or egress.get("enabled") is not True
        ):
            return "execution_egress_authority_invalid"
        if str(egress.get("source") or "").strip().lower() == "production_runtime":
            runtime_startup = dict(snapshot.get("runtime_startup") or {})
            runtime_attestation = dict(snapshot.get("runtime_attestation") or {})
            runtime_boot_id = str(egress.get("runtime_boot_id") or "").strip()
            if (
                not runtime_boot_id
                or str(runtime_startup.get("boot_id") or "").strip() != runtime_boot_id
                or str(runtime_attestation.get("runtime_boot_id") or "").strip()
                != runtime_boot_id
            ):
                return "execution_egress_current_boot_mismatch"
            if str(snapshot.get("runtime_status") or "").strip().lower() != "running":
                return "execution_egress_runner_not_running"
            cycle_age = _timestamp_age_secs(
                snapshot.get("runtime_last_cycle_ts"),
                now_ts=now,
            )
            if cycle_age is None or cycle_age > _EXECUTION_EGRESS_RUNNER_LEASE_SECS:
                return "execution_egress_runner_lease_stale"
            runtime_diag = dict(snapshot.get("runtime_diag") or {})
            live = dict(runtime_diag.get("orchestration_live") or {})
            if (
                not bool(live.get("enabled", False))
                or str(live.get("mode") or "").strip().lower() != "live"
                or not bool(live.get("runtime_enabled", False))
                or bool(live.get("queue_kill_active", False))
            ):
                return "execution_egress_runtime_disabled"
            if _safe_int(live.get("authority_revision"), 0) != _safe_int(
                egress.get("authority_revision"),
                0,
            ):
                return "execution_egress_authority_revision_changed"
            pair_scope = {
                str(item).strip().upper()
                for item in list(egress.get("pair_scope") or [])
                if str(item).strip()
            }
            sleeve_scope = {
                str(item).strip().lower()
                for item in list(egress.get("sleeve_scope") or [])
                if str(item).strip()
            }
            intent_scope = {
                str(item).strip().lower()
                for item in list(egress.get("intent_scope") or [])
                if str(item).strip()
            }
            if pair_scope != {
                str(item).strip().upper()
                for item in list(live.get("active_pair_scope") or [])
                if str(item).strip()
            }:
                return "execution_egress_pair_scope_changed"
            if sleeve_scope != {
                str(item).strip().lower()
                for item in list(live.get("active_sleeve_scope") or [])
                if str(item).strip()
            }:
                return "execution_egress_sleeve_scope_changed"
            if intent_scope != {
                str(item).strip().lower()
                for item in list(live.get("active_intent_scope") or [])
                if str(item).strip()
            }:
                return "execution_egress_intent_scope_changed"
            if command is None:
                return ""
            cmd = str(command.cmd or "").strip().upper()
            symbol = str(command.symbol or "").strip().upper()
            if egress.get("protective_management_only") is True:
                if (
                    pair_scope != set(IG_MT4_SCALP_SYMBOLS)
                    or sleeve_scope != {"scalp"}
                    or intent_scope != {"exit"}
                ):
                    return "execution_egress_protective_scope_invalid"
                if cmd != "CLOSE":
                    return "execution_egress_protective_command_blocked"
                payload = dict(command.payload or {})
                target_ticket = _safe_int(
                    command.target_ticket
                    if int(command.target_ticket) > 0
                    else payload.get("target_ticket"),
                    0,
                )
                owner_token = str(
                    command.owner_token or payload.get("owner_token") or ""
                ).strip()
                ownership_contract = str(
                    command.ownership_contract
                    or payload.get("ownership_contract")
                    or ""
                ).strip()
                if target_ticket <= 0:
                    return "execution_egress_protective_target_ticket_missing"
                if _safe_int(command.magic, 0) <= 0 or not owner_token:
                    return "execution_egress_protective_owner_identity_missing"
                if ownership_contract != TICKET_OWNER_CONTRACT:
                    return "execution_egress_protective_ownership_contract_invalid"
                if not str(payload.get("managed_entry_command_id") or "").strip():
                    return "execution_egress_protective_entry_command_missing"
            required_intent = {
                "BUY": "enter",
                "SELL": "enter",
                "CLOSE": "exit",
                "CLOSE_ALL": "exit",
                "CLOSE_PARTIAL": "reduce",
                "MODIFY_SL": "tighten_stop",
            }.get(cmd, "")
            if required_intent and required_intent not in intent_scope:
                return "execution_egress_command_intent_blocked"
            if cmd not in {"INFO", "CLOSE_ALL"} and (
                not symbol or symbol not in pair_scope
            ):
                return "execution_egress_command_pair_blocked"
            if cmd in {"BUY", "SELL"}:
                meta = dict(command.orchestration_meta_json or {})
                payload = dict(command.payload or {})
                sleeve = (
                    str(
                        payload.get("sleeve")
                        or payload.get("adaptive_sleeve")
                        or meta.get("sleeve")
                        or meta.get("adaptive_sleeve")
                        or ""
                    )
                    .strip()
                    .lower()
                )
                if not sleeve or sleeve not in sleeve_scope:
                    return "execution_egress_command_sleeve_blocked"
            return ""
        release = dict(snapshot.get("release_authority") or {})
        binding_error = PostgresRuntimeStore._release_egress_binding_error(release)
        if binding_error:
            return binding_error
        request = dict(release.get("request") or {})
        ack = dict(release.get("ack") or {})
        witness = dict(request.get("external_witness") or {})
        witness_expires_at = _parse_iso_ts(witness.get("expires_at"))
        if witness_expires_at <= now:
            return "execution_egress_witness_expired"
        if str(egress.get("generation_id") or "") != str(
            request.get("generation_id") or ""
        ):
            return "execution_egress_generation_mismatch"
        if str(egress.get("request_sha256") or "") != str(
            request.get("request_sha256") or ""
        ):
            return "execution_egress_request_mismatch"
        if str(egress.get("runtime_boot_id") or "") != str(
            ack.get("runtime_boot_id") or ""
        ):
            return "execution_egress_boot_mismatch"

        runtime_startup = dict(snapshot.get("runtime_startup") or {})
        runtime_attestation = dict(snapshot.get("runtime_attestation") or {})
        current_boot_id = str(runtime_startup.get("boot_id") or "").strip()
        acknowledged_boot_id = str(ack.get("runtime_boot_id") or "").strip()
        if (
            not current_boot_id
            or current_boot_id != acknowledged_boot_id
            or str(runtime_attestation.get("runtime_boot_id") or "").strip()
            != acknowledged_boot_id
        ):
            return "execution_egress_current_boot_mismatch"
        if str(snapshot.get("runtime_status") or "").strip().lower() != "running":
            return "execution_egress_runner_not_running"
        cycle_age = _timestamp_age_secs(
            snapshot.get("runtime_last_cycle_ts"),
            now_ts=now,
        )
        if cycle_age is None or cycle_age > _EXECUTION_EGRESS_RUNNER_LEASE_SECS:
            return "execution_egress_runner_lease_stale"

        execution = dict(request.get("authorized_execution") or {})
        signed_account_mode = str(execution.get("account_mode") or "").strip().lower()
        signed_account_scope = str(execution.get("account_scope") or "").strip()
        if (
            str(snapshot.get("broker_account_mode") or "").strip().lower()
            != signed_account_mode
        ):
            return "execution_egress_account_mode_changed"
        if (
            str(snapshot.get("broker_account_scope") or "").strip()
            != signed_account_scope
        ):
            return "execution_egress_account_scope_changed"

        runtime_diag = dict(snapshot.get("runtime_diag") or {})
        live = dict(runtime_diag.get("orchestration_live") or {})
        signed_pairs = {
            str(item).strip().upper()
            for item in list(execution.get("pair_scope") or [])
            if str(item).strip()
        }
        signed_sleeves = {
            str(item).strip().lower()
            for item in list(execution.get("sleeve_scope") or [])
            if str(item).strip()
        }
        signed_intents = {
            str(item).strip().lower()
            for item in list(execution.get("intent_scope") or [])
            if str(item).strip()
        }
        current_pairs = {
            str(item).strip().upper()
            for item in list(live.get("active_pair_scope") or [])
            if str(item).strip()
        }
        current_sleeves = {
            str(item).strip().lower()
            for item in list(live.get("active_sleeve_scope") or [])
            if str(item).strip()
        }
        current_intents = {
            str(item).strip().lower()
            for item in list(live.get("active_intent_scope") or [])
            if str(item).strip()
        }
        if (
            not current_pairs
            or not current_pairs.issubset(signed_pairs)
            or not current_sleeves
            or not current_sleeves.issubset(signed_sleeves)
            or not current_intents
            or not current_intents.issubset(signed_intents)
        ):
            return "execution_egress_runtime_scope_widened"

        if command is not None:
            cmd = str(command.cmd or "").strip().upper()
            symbol = str(command.symbol or "").strip().upper()
            command_meta = dict(command.orchestration_meta_json or {})
            command_release_expectations = {
                "release_generation_id": str(request.get("generation_id") or ""),
                "release_request_sha256": str(request.get("request_sha256") or ""),
                "release_model_identity_sha256": str(
                    request.get("model_identity_sha256") or ""
                ),
                "release_manifest_file_sha256": str(
                    request.get("manifest_file_sha256") or ""
                ),
                "release_runtime_boot_id": str(ack.get("runtime_boot_id") or ""),
            }
            for field_name, expected_value in command_release_expectations.items():
                if (
                    not expected_value
                    or str(command_meta.get(field_name) or "") != expected_value
                ):
                    return f"execution_egress_command_{field_name}_mismatch"
            if cmd == "CLOSE_ALL":
                # Account-wide flatten is deliberately distinct from normal
                # singleton pair authority and must be present in the exact
                # externally signed execution contract.
                if execution.get("emergency_flatten_all") is not True:
                    return "execution_egress_emergency_flatten_unauthorized"
                return ""
            if symbol and symbol not in signed_pairs:
                return "execution_egress_command_pair_blocked"
            if cmd not in {"INFO"} and not symbol:
                return "execution_egress_command_pair_missing"
            if cmd == "INFO":
                return ""
            protective_intents = {
                str(item).strip().lower()
                for item in list(execution.get("protective_intent_scope") or [])
                if str(item).strip()
            }
            if cmd in {"CLOSE", "CLOSE_PARTIAL"}:
                if "exit" not in protective_intents:
                    return "execution_egress_protective_exit_unauthorized"
                return ""
            if cmd == "MODIFY_SL":
                if "adjust" not in protective_intents:
                    return "execution_egress_protective_adjust_unauthorized"
                return ""
            if cmd not in {"BUY", "SELL"} or "enter" not in signed_intents:
                return "execution_egress_command_intent_blocked"
            payload = dict(command.payload or {})
            meta = command_meta
            command_sleeve = (
                str(
                    payload.get("sleeve")
                    or payload.get("adaptive_sleeve")
                    or meta.get("sleeve")
                    or meta.get("adaptive_sleeve")
                    or ""
                )
                .strip()
                .lower()
            )
            if cmd != "INFO" and (
                not command_sleeve or command_sleeve not in signed_sleeves
            ):
                return "execution_egress_command_sleeve_blocked"
        return ""

    def _execution_egress_authorization_failure(
        self,
        conn,
        *,
        now_ts: float | None = None,
        command: ExecutionCommand | None = None,
    ) -> str:
        state = self._locked_runtime_state(conn)
        failure = self._execution_egress_authorization_failure_from_state(
            state,
            now_ts=now_ts,
            command=command,
        )
        if failure or command is None:
            return failure
        egress = dict(state.get("execution_egress_authority") or {})
        if egress.get("protective_management_only") is True:
            return self._protective_management_history_failure(
                conn,
                state=state,
                command=command,
                now_ts=float(_now() if now_ts is None else now_ts),
            )
        return ""

    def _protective_management_history_failure(
        self,
        conn,
        *,
        state: dict[str, Any],
        command: ExecutionCommand,
        now_ts: float,
    ) -> str:
        """Join CLOSE to an exact entry plus ACK or a fresh position snapshot."""

        close_payload = dict(command.payload or {})
        entry_command_id = str(
            close_payload.get("managed_entry_command_id") or ""
        ).strip()
        row = (
            conn.execute(
                select(self.commands)
                .where(self.commands.c.command_id == entry_command_id)
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if row is None:
            return "execution_egress_protective_entry_command_missing"
        entry = dict(row)
        entry_payload = dict(entry.get("payload_json") or {})
        entry_ack = dict(entry.get("ack_json") or {})
        row_command_id = str(entry.get("command_id") or "").strip()
        row_cmd = str(entry.get("cmd") or "").strip().upper()
        row_symbol = str(entry.get("symbol") or "").strip().upper()
        if (
            row_command_id != entry_command_id
            or str(entry_payload.get("command_id") or "").strip() != entry_command_id
        ):
            return "execution_egress_protective_entry_command_id_changed"
        if (
            row_cmd not in {"BUY", "SELL"}
            or str(entry_payload.get("cmd") or "").strip().upper() != row_cmd
        ):
            return "execution_egress_protective_entry_command_invalid"
        if str(entry.get("intent") or "").strip().lower() != SCALP_ENTRY_INTENT:
            return "execution_egress_protective_entry_intent_invalid"

        symbol = (
            str(command.symbol or close_payload.get("symbol") or "").strip().upper()
        )
        owner_token = str(
            command.owner_token or close_payload.get("owner_token") or ""
        ).strip()
        target_ticket = _safe_int(
            command.target_ticket
            if int(command.target_ticket) > 0
            else close_payload.get("target_ticket"),
            0,
        )
        magic = _safe_int(command.magic, 0)
        current_account_scope = str(state.get("broker_account_scope") or "").strip()
        if (
            not current_account_scope
            or str(entry_payload.get("expected_account_scope") or "").strip()
            != current_account_scope
        ):
            return "execution_egress_protective_account_scope_changed"
        if (
            row_symbol != symbol
            or str(entry_payload.get("symbol") or "").strip().upper() != symbol
            or str(close_payload.get("symbol") or "").strip().upper() != symbol
        ):
            return "execution_egress_protective_symbol_changed"
        if (
            _safe_int(entry.get("magic"), 0) != magic
            or _safe_int(entry_payload.get("magic"), 0) != magic
        ):
            return "execution_egress_protective_magic_changed"
        if str(entry_payload.get("owner_token") or "").strip() != owner_token:
            return "execution_egress_protective_ticket_owner_changed"
        if (
            str(entry_payload.get("ownership_contract") or "").strip()
            != TICKET_OWNER_CONTRACT
        ):
            return "execution_egress_protective_entry_ownership_contract_invalid"
        binding_failure = scalp_protective_history_binding_error(
            entry_payload,
            close_payload=close_payload,
            symbol=symbol,
        )
        if binding_failure:
            return f"execution_egress_protective_{binding_failure}"

        ack_status = str(entry_ack.get("status") or "").strip().lower()
        ack_success = ack_status in {
            "acked",
            "ok",
            "success",
            "done",
            "executed",
            "filled",
        }
        ack_command_id = str(entry_ack.get("command_id") or "").strip()
        ack_joined = bool(
            str(entry.get("status") or "").strip().lower() == "acked"
            and ack_success
            and (not ack_command_id or ack_command_id == entry_command_id)
            and str(entry_ack.get("symbol") or "").strip().upper() == symbol
            and _safe_int(entry_ack.get("ticket"), 0) == target_ticket
            and _safe_int(entry_ack.get("magic"), 0) == magic
            and str(entry_ack.get("owner_token") or "").strip() == owner_token
        )
        snapshot_age = _timestamp_age_secs(
            state.get("positions_snapshot_received_at"),
            now_ts=float(now_ts),
        )
        settings = _get_settings()
        snapshot_fresh = bool(
            state.get("positions_snapshot_authoritative") is True
            and state.get("positions_snapshot_source") == "positions_snapshot"
            and state.get("positions_snapshot_schema") == _MT4_POSITIONS_SNAPSHOT_SCHEMA
            and state.get("positions_snapshot_contract_current") is True
            and str(state.get("positions_snapshot_token") or "").strip()
            and str(state.get("positions_snapshot_account_scope") or "").strip()
            == current_account_scope
            and snapshot_age is not None
            and snapshot_age <= max(1.0, float(settings.bridge_stale_heartbeat_secs))
        )
        position_matches = []
        if snapshot_fresh:
            for raw_position in list(state.get("positions") or []):
                if not isinstance(raw_position, dict):
                    continue
                if (
                    str(raw_position.get("symbol") or raw_position.get("pair") or "")
                    .strip()
                    .upper()
                    == symbol
                    and _safe_int(raw_position.get("ticket"), 0) == target_ticket
                    and _safe_int(raw_position.get("magic"), 0) == magic
                    and str(
                        raw_position.get("order_comment")
                        or raw_position.get("comment")
                        or raw_position.get("owner_token")
                        or ""
                    ).strip()
                    == owner_token
                ):
                    position_matches.append(raw_position)
        snapshot_joined = bool(len(position_matches) == 1)
        if not ack_joined and not snapshot_joined:
            return "execution_egress_protective_entry_ownership_unconfirmed"
        return ""

    def _quarantine_execution_queue(
        self,
        conn,
        *,
        reason: str,
        now_ts: float,
        preserve_queued_exposure_reducing: bool = False,
    ) -> int:
        """Expire undelivered work and quarantine unknown delivered outcomes.

        A boot caller may explicitly retain queued exposure-reducing commands.
        Delivered commands are never retained because their broker outcome is
        unknown, including when the delivered command itself reduces exposure.
        """

        rows = (
            conn.execute(
                select(self.commands).where(
                    self.commands.c.status.in_(["queued", "delivered"])
                )
            )
            .mappings()
            .all()
        )
        normalized_reason = str(reason or "execution_egress_disabled")
        command_ids_by_status: dict[str, list[str]] = {
            "queued": [],
            "delivered": [],
        }
        for raw_row in rows:
            row = dict(raw_row)
            if _preserve_queued_exposure_reducing_command(
                row,
                enabled=preserve_queued_exposure_reducing,
            ):
                continue
            command_id = str(row.get("command_id") or "")
            previous_status = str(row.get("status") or "")
            if not command_id or previous_status not in command_ids_by_status:
                continue
            command_ids_by_status[previous_status].append(command_id)

        events: list[dict[str, Any]] = []
        for previous_status, command_ids in command_ids_by_status.items():
            if not command_ids:
                continue
            next_status = (
                "reconcile_required" if previous_status == "delivered" else "expired"
            )
            row_reason = (
                f"{normalized_reason}:broker_outcome_unknown"
                if previous_status == "delivered"
                else normalized_reason
            )
            transitioned_ids = (
                conn.execute(
                    update(self.commands)
                    .where(
                        and_(
                            self.commands.c.command_id.in_(command_ids),
                            self.commands.c.status == previous_status,
                        )
                    )
                    .values(
                        status=next_status,
                        updated_at=float(now_ts),
                        reason=row_reason,
                    )
                    .returning(self.commands.c.command_id)
                )
                .scalars()
                .all()
            )
            events.extend(
                {
                    "command_id": str(command_id),
                    "event_status": next_status,
                    "reason": row_reason,
                    "payload": {
                        "execution_egress_enabled": False,
                        "previous_status": previous_status,
                        "delivery_attempted": previous_status == "delivered",
                    },
                }
                for command_id in transitioned_ids
            )
        self._append_command_events(events=events, conn=conn)
        return len(events)

    def _quarantine_disabled_scalp_entries(
        self,
        conn,
        *,
        now_ts: float,
    ) -> int:
        """Fence retired standalone and direct-demo entries before broker poll.

        Queued rows were never handed to the EA and are terminally expired.
        Delivered rows may already have changed broker state, so they remain
        late-ACK-reconcilable under ``reconcile_required``.
        """

        normalized_cmd = func.upper(self.commands.c.cmd)
        rows = (
            conn.execute(
                select(
                    self.commands.c.command_id,
                    self.commands.c.status,
                    self.commands.c.cmd,
                    self.commands.c.intent,
                    self.commands.c.payload_json,
                ).where(
                    and_(
                        self.commands.c.status.in_(["queued", "delivered"]),
                        # The Python recognizers below accept only BUY/SELL.
                        # Use a conservative SQL superset so malformed casing
                        # or surrounding whitespace is still fenced fail closed
                        # without hydrating every protective queue row.
                        or_(
                            normalized_cmd.like("%BUY%"),
                            normalized_cmd.like("%SELL%"),
                        ),
                    )
                )
            )
            .mappings()
            .all()
        )
        command_ids_by_transition: dict[tuple[str, str], list[str]] = {}
        for raw_row in rows:
            row = dict(raw_row)
            disabled_standalone = _is_identifiable_scalp_entry(
                cmd=row.get("cmd"),
                command_id=row.get("command_id"),
                intent=row.get("intent"),
                payload=row.get("payload_json"),
            )
            legacy_direct_demo = _is_legacy_direct_demo_entry(
                cmd=row.get("cmd"),
                payload=row.get("payload_json"),
            )
            if not disabled_standalone and not legacy_direct_demo:
                continue
            command_id = str(row.get("command_id") or "")
            previous_status = str(row.get("status") or "")
            if not command_id or previous_status not in {"queued", "delivered"}:
                continue
            authorization_failure = (
                "scalp_strategy_admission_mode_signed_validation_required"
                if legacy_direct_demo
                else _DISABLED_SCALP_ENTRY_REASON
            )
            command_ids_by_transition.setdefault(
                (previous_status, authorization_failure), []
            ).append(command_id)

        events: list[dict[str, Any]] = []
        for (
            previous_status,
            authorization_failure,
        ), command_ids in command_ids_by_transition.items():
            delivered = previous_status == "delivered"
            next_status = "reconcile_required" if delivered else "expired"
            reason = f"poll_authority_revoked:{authorization_failure}" + (
                ":broker_outcome_unknown" if delivered else ""
            )
            transitioned_ids = (
                conn.execute(
                    update(self.commands)
                    .where(
                        and_(
                            self.commands.c.command_id.in_(command_ids),
                            self.commands.c.status == previous_status,
                        )
                    )
                    .values(
                        status=next_status,
                        updated_at=float(now_ts),
                        reason=reason,
                    )
                    .returning(self.commands.c.command_id)
                )
                .scalars()
                .all()
            )
            if not transitioned_ids:
                continue
            events.extend(
                {
                    "command_id": str(command_id),
                    "event_status": next_status,
                    "reason": reason,
                    "payload": {
                        "authorization_failure": authorization_failure,
                        "previous_status": previous_status,
                        "delivery_attempted": delivered,
                        "reconciliation_required": delivered,
                    },
                }
                for command_id in transitioned_ids
            )
        self._append_command_events(events=events, conn=conn)
        return len(events)

    def _expire_poll_rejections(
        self,
        conn,
        *,
        rejections: list[tuple[str, str, str, float]],
    ) -> int:
        """Expire queued poll refusals with set-based writes and ordered audit."""

        command_ids_by_transition: dict[tuple[str, float], list[str]] = {}
        normalized_rejections: list[tuple[str, str, str, float]] = []
        for command_id, reason, authorization_failure, rejected_at in rejections:
            normalized_command_id = str(command_id or "").strip()
            normalized_failure = str(
                authorization_failure or "poll_authority_check_failed"
            )
            normalized_reason = str(reason or "").strip() or (
                f"poll_authority_revoked:{normalized_failure}"
            )
            if not normalized_command_id:
                continue
            rejection = (
                normalized_command_id,
                normalized_reason,
                normalized_failure,
                float(rejected_at),
            )
            normalized_rejections.append(rejection)
            command_ids_by_transition.setdefault(
                (normalized_reason, float(rejected_at)), []
            ).append(normalized_command_id)

        transitioned_ids: set[str] = set()
        for (
            reason,
            rejected_at,
        ), command_ids in command_ids_by_transition.items():
            transitioned_ids.update(
                str(command_id)
                for command_id in (
                    conn.execute(
                        update(self.commands)
                        .where(
                            and_(
                                self.commands.c.command_id.in_(command_ids),
                                self.commands.c.status == "queued",
                            )
                        )
                        .values(
                            status="expired",
                            updated_at=rejected_at,
                            reason=reason,
                        )
                        .returning(self.commands.c.command_id)
                    )
                    .scalars()
                    .all()
                )
            )

        events = [
            {
                "command_id": command_id,
                "event_status": "expired",
                "reason": reason,
                "payload": {
                    "authorization_failure": authorization_failure,
                    "delivery_attempted": False,
                },
            }
            for command_id, reason, authorization_failure, _rejected_at in normalized_rejections
            if command_id in transitioned_ids
        ]
        self._append_command_events(events=events, conn=conn)
        return len(events)

    @staticmethod
    def _disable_execution_egress_in_state(
        state: dict[str, Any],
        *,
        reason: str,
        now_ts: float,
    ) -> None:
        snapshot = state
        snapshot["execution_egress_enabled"] = False
        snapshot["execution_egress_authority"] = {
            "schema_version": _EXECUTION_EGRESS_SCHEMA,
            "enabled": False,
            "reason": str(reason or "execution_egress_disabled"),
            "generation_id": "",
            "request_sha256": "",
            "runtime_boot_id": "",
            "updated_at": float(now_ts),
        }

    @staticmethod
    def _revoke_production_scalp_authority_in_state(
        state: dict[str, Any],
        *,
        reason: str,
        now_ts: float,
    ) -> None:
        current = dict(state.get("production_scalp_authority") or {})
        if not current:
            return
        state["production_scalp_authority"] = {
            **current,
            "status": "revoked",
            "reason": str(reason or "execution_egress_disabled"),
            "updated_at": float(now_ts),
        }

    def _latest_market_ticks_for_symbols(
        self,
        conn,
        *,
        symbols: set[str],
        market_source: AuthenticatedMarketSource | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Read one latest persisted quote per canonical symbol/source atomically."""

        normalized = sorted(
            {
                str(symbol or "").strip().upper()
                for symbol in symbols
                if str(symbol or "").strip()
            }
        )
        if not normalized:
            return {}
        source_predicates = []
        if market_source is not None:
            source_fields = market_source.to_fields()
            source_predicates.extend(
                [
                    self.market_ticks.c.market_source_authenticated == 1,
                    self.market_ticks.c.market_source_schema
                    == source_fields["market_source_schema"],
                    self.market_ticks.c.market_source_id == market_source.source_id,
                    self.market_ticks.c.broker_account_scope
                    == market_source.broker_account_scope,
                    self.market_ticks.c.broker_venue_id
                    == market_source.broker_venue_id,
                    self.market_ticks.c.producer_identity
                    == market_source.producer_identity,
                    self.market_ticks.c.producer_instance_id
                    == market_source.producer_instance_id,
                    self.market_ticks.c.terminal_lease_scope
                    == market_source.terminal_lease_scope,
                    self.market_ticks.c.credential_generation_id
                    == market_source.credential_generation_id,
                    self.market_ticks.c.bridge_protocol_version
                    == market_source.bridge_protocol_version,
                ]
            )
        latest_queries = []
        for symbol in normalized:
            latest = (
                select(
                    self.market_ticks.c.symbol,
                    self.market_ticks.c.bid,
                    self.market_ticks.c.ask,
                    self.market_ticks.c.ts,
                )
                .where(
                    and_(
                        self.market_ticks.c.symbol == symbol,
                        *source_predicates,
                    )
                )
                .order_by(
                    self.market_ticks.c.ts.desc(),
                    self.market_ticks.c.id.desc(),
                )
                .limit(1)
                .subquery()
            )
            latest_queries.append(
                select(
                    latest.c.symbol,
                    latest.c.bid,
                    latest.c.ask,
                    latest.c.ts,
                )
            )
        statement = (
            latest_queries[0]
            if len(latest_queries) == 1
            else union_all(*latest_queries)
        )
        rows = conn.execute(statement).mappings()
        return {
            str(row.get("symbol") or "").strip().upper(): dict(row)
            for row in rows
            if str(row.get("symbol") or "").strip()
        }

    def _production_scalp_authorization_failure(
        self,
        conn,
        *,
        state: dict[str, Any],
        pair: str,
        payload: dict[str, Any],
        command_id: str,
        expected_strategy_authority: dict[str, Any] | None,
        now_ts: float,
    ) -> str:
        """Recheck the production-scalper identity and daily cell budget.

        Either the intent or lane claiming scalper provenance makes the full
        contract mandatory.  This prevents a malformed command from falling
        through to the ordinary model lane merely because one of its two
        discriminator fields was removed.
        """

        raw = dict(payload or {})
        intent = str(raw.get("intent") or "").strip().lower()
        lane = str(raw.get("strategy_lane") or "").strip().lower()
        claims_scalper = bool(
            intent == SCALP_ENTRY_INTENT or lane == SCALP_EXECUTION_LANE
        )
        if not claims_scalper:
            return ""
        if intent != SCALP_ENTRY_INTENT:
            return "scalp_command_intent_invalid"
        if lane != SCALP_EXECUTION_LANE:
            return "scalp_command_lane_invalid"
        if (
            str(raw.get("expected_strategy_authority_schema") or "")
            != SCALP_EXECUTION_AUTHORITY_SCHEMA
        ):
            return "expected_strategy_authority_schema_changed"

        durable_authority = dict(state.get("production_scalp_authority") or {})
        bound_admission_modes = {
            str(durable_authority.get("admission_mode") or "").strip().lower(),
            str(raw.get("expected_strategy_admission_mode") or "").strip().lower(),
        }
        if expected_strategy_authority is not None:
            bound_admission_modes.add(
                str(dict(expected_strategy_authority).get("admission_mode") or "")
                .strip()
                .lower()
            )
        if bound_admission_modes != {SCALP_ADMISSION_MODE_SIGNED}:
            return "scalp_strategy_admission_mode_signed_validation_required"
        bound_account_modes = {
            str(durable_authority.get("account_mode") or "").strip().lower(),
            str(raw.get("expected_strategy_account_mode") or "").strip().lower(),
            str(raw.get("expected_account_mode") or "").strip().lower(),
            str(state.get("broker_account_mode") or "").strip().lower(),
        }
        if len(bound_account_modes) != 1 or not bound_account_modes.issubset(
            {"demo", "real"}
        ):
            return "scalp_strategy_account_mode_changed"
        if expected_strategy_authority is not None:
            expected = scalp_expectation_from_authority(expected_strategy_authority)
            supplied_binding = (
                str(dict(expected_strategy_authority).get("binding_sha256") or "")
                .strip()
                .lower()
            )
            if (
                supplied_binding
                != str(raw.get("expected_strategy_binding_sha256") or "")
                .strip()
                .lower()
            ):
                return "scalp_authority_approval_binding_mismatch"
        else:
            expected = scalp_expectation_from_command(raw)
        authority_failure = scalp_authority_error(
            durable_authority,
            expectation=expected,
        )
        if authority_failure:
            return str(authority_failure)
        binding_failure = scalp_command_binding_error(
            raw,
            authority=durable_authority,
            symbol=pair,
        )
        if binding_failure:
            return str(binding_failure)

        # Re-read the selected symbol under the same state/queue lock as
        # enqueue or delivery. Account, venue, source, and snapshot age remain
        # global invariants; an unrelated closed/malformed symbol must not
        # suppress a valid instant trade on this pair.
        broker_contracts = project_ig_mt4_selected_contract_universe(
            state,
            selected_symbols=(pair,),
            now_ts=now_ts,
            max_age_secs=MAX_PRODUCTION_SCALP_BROKER_CONTRACT_AGE_SECS,
        )
        if not broker_contracts.ok:
            return str(broker_contracts.errors[0])
        broker_binding_failure = broker_contract_command_binding_error(
            raw,
            universe=broker_contracts,
            symbol=pair,
        )
        if broker_binding_failure:
            return str(broker_binding_failure)

        day_start = (
            datetime.fromtimestamp(float(now_ts), UTC)
            .replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            .timestamp()
        )
        daily_predicates = [
            func.lower(self.commands.c.intent) == SCALP_ENTRY_INTENT,
            self.commands.c.symbol == str(pair or "").strip().upper(),
            self.commands.c.created_at >= float(day_start),
        ]
        normalized_command_id = str(command_id or "").strip()
        if normalized_command_id:
            daily_predicates.append(self.commands.c.command_id != normalized_command_id)
        prior_entry_rows = conn.execute(
            select(
                self.commands.c.command_id,
                self.commands.c.status,
                self.commands.c.delivered_count,
                self.commands.c.ack_json,
            ).where(and_(*daily_predicates))
        ).mappings()
        prior_entries = sum(
            1
            for row in prior_entry_rows
            if classify_scalp_daily_budget_row(row).consumed
        )
        daily_limit = _safe_int(
            durable_authority.get("max_entries_per_symbol_utc_day"),
            MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
        )
        if daily_limit != MAX_ENTRIES_PER_SYMBOL_UTC_DAY:
            return "scalp_authority_daily_frequency_changed"
        if prior_entries >= daily_limit:
            return "scalp_daily_entry_frequency_exhausted"
        return ""

    def _live_entry_authorization_failure(
        self,
        conn,
        *,
        pair: str,
        expected_account_mode: str,
        expected_account_scope: str,
        expected_authority_revision: int,
        now_ts: float,
        expected_release_generation_id: str = "",
        expected_release_request_sha256: str = "",
        expected_model_identity_sha256: str = "",
        expected_manifest_file_sha256: str = "",
        expected_runtime_boot_id: str = "",
        command_payload: dict[str, Any] | None = None,
        command_id: str = "",
        expected_strategy_authority: dict[str, Any] | None = None,
    ) -> str:
        """Return the current reason an entry may not cross the broker edge.

        This check intentionally reads and locks the durable runtime state in
        the same transaction that either enqueues or delivers the command. A
        prior in-process approval is evidence of what was authorized, not a
        lease that survives a kill switch, broker identity drift, or stale
        transport data.
        """

        symbol = str(pair or "").strip().upper()
        expected_mode = str(expected_account_mode or "").strip().lower()
        expected_scope = str(expected_account_scope or "").strip()
        expected_revision = _safe_int(expected_authority_revision, 0)
        if expected_mode not in {"demo", "real"}:
            return "broker_account_mode_unattested"
        if not expected_scope:
            return "broker_account_scope_unattested"
        if expected_revision <= 0:
            return "live_authority_revision_unattested"
        if not symbol:
            return "live_pair_not_allowlisted"

        state = self._locked_runtime_state(conn)
        runtime_diag = dict(state.get("runtime_diag") or {})
        egress = dict(state.get("execution_egress_authority") or {})
        if str(egress.get("source") or "").strip().lower() != "production_runtime":
            release = dict(state.get("release_authority") or {})
            release_request = dict(release.get("request") or {})
            release_ack = dict(release.get("ack") or {})
            release_expectations = {
                "generation_id": (
                    str(expected_release_generation_id or ""),
                    str(release_request.get("generation_id") or ""),
                ),
                "request_sha256": (
                    str(expected_release_request_sha256 or ""),
                    str(release_request.get("request_sha256") or ""),
                ),
                "model_identity_sha256": (
                    str(expected_model_identity_sha256 or ""),
                    str(release_request.get("model_identity_sha256") or ""),
                ),
                "manifest_file_sha256": (
                    str(expected_manifest_file_sha256 or ""),
                    str(release_request.get("manifest_file_sha256") or ""),
                ),
                "runtime_boot_id": (
                    str(expected_runtime_boot_id or ""),
                    str(release_ack.get("runtime_boot_id") or ""),
                ),
            }
            for field_name, (
                expected_value,
                current_value,
            ) in release_expectations.items():
                if not expected_value:
                    return f"release_authority_{field_name}_unattested"
                if expected_value != current_value:
                    return f"release_authority_{field_name}_changed"
        live = dict(runtime_diag.get("orchestration_live") or {})
        admission = dict(runtime_diag.get("live_command_admission") or {})
        if not bool(live.get("enabled", False)):
            return "live_mode_disabled"
        if str(live.get("mode") or "").strip().lower() != "live":
            return "live_mode_disabled"
        if not bool(live.get("runtime_enabled", False)):
            return "live_runtime_killed"
        if bool(live.get("queue_kill_active", False)):
            return "live_queue_killed"
        if _safe_int(live.get("authority_revision"), 0) != expected_revision:
            return "live_authority_revision_changed"
        if not bool(admission.get("allowed", False)):
            return "live_command_admission_blocked"
        pair_admission = dict(dict(admission.get("pairs") or {}).get(symbol) or {})
        if not bool(pair_admission.get("allowed", False)):
            return "live_rollout_pair_blocked"
        active_pairs = {
            str(item).strip().upper()
            for item in list(live.get("active_pair_scope") or [])
            if str(item).strip()
        }
        active_intents = {
            str(item).strip().lower()
            for item in list(live.get("active_intent_scope") or [])
            if str(item).strip()
        }
        if symbol not in active_pairs:
            return "live_pair_not_allowlisted"
        if "enter" not in active_intents:
            return "live_intent_not_allowlisted"
        if str(state.get("broker_account_mode") or "").strip().lower() != expected_mode:
            return "broker_account_mode_changed"
        if str(state.get("broker_account_scope") or "").strip() != expected_scope:
            return "broker_account_scope_changed"

        strategy_failure = self._production_scalp_authorization_failure(
            conn,
            state=state,
            pair=symbol,
            payload=dict(command_payload or {}),
            command_id=str(command_id or ""),
            expected_strategy_authority=expected_strategy_authority,
            now_ts=now_ts,
        )
        if strategy_failure:
            return strategy_failure

        if str(state.get("system_status") or "").strip().lower() != "connected":
            return "broker_heartbeat_disconnected"
        settings = _get_settings()
        heartbeat_age = _timestamp_age_secs(
            state.get("last_heartbeat"),
            now_ts=now_ts,
        )
        heartbeat_stale_after = max(
            1.0,
            float(settings.bridge_stale_heartbeat_secs),
        )
        if heartbeat_age is None:
            return "broker_heartbeat_invalid"
        if heartbeat_age > heartbeat_stale_after:
            return "broker_heartbeat_stale"

        raw_payload = dict(command_payload or {})
        production_scalper = bool(
            str(raw_payload.get("strategy_lane") or "").strip().lower()
            == SCALP_EXECUTION_LANE
            or str(raw_payload.get("intent") or "").strip().lower()
            == SCALP_ENTRY_INTENT
        )
        if production_scalper:
            if (
                str(raw_payload.get("rollover_guard_schema_version") or "")
                != PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION
            ):
                return "production_scalp_rollover_guard_schema_mismatch"
            if (
                str(raw_payload.get("rollover_guard_config_sha256") or "")
                != DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY.config_sha256()
            ):
                return "production_scalp_rollover_guard_config_mismatch"
            rollover_guard = evaluate_production_scalp_rollover_guard(now_ts)
            if not rollover_guard.entry_allowed:
                return str(
                    rollover_guard.reason or "production_scalp_rollover_guard_invalid"
                )
        requested_tick_symbols = {symbol}
        account_currency = (
            str(state.get("broker_account_currency") or "").strip().upper()
        )
        quote_currency = symbol[3:6] if len(symbol) >= 6 else ""
        if (
            production_scalper
            and len(account_currency) == 3
            and len(quote_currency) == 3
            and quote_currency != account_currency
        ):
            allowed_conversion_sources = set(IG_MT4_SCALP_SYMBOLS)
            allowed_conversion_sources.update(
                account_conversion_tick_symbols(
                    account_currency,
                    required_symbols=IG_MT4_SCALP_SYMBOLS,
                )
            )
            requested_tick_symbols.update(
                symbol_candidate
                for symbol_candidate in account_conversion_source_symbols(
                    account_currency=account_currency,
                    quote_currency=quote_currency,
                )
                if symbol_candidate in allowed_conversion_sources
            )
        pinned_market_source: AuthenticatedMarketSource | None = None
        if production_scalper:
            pinned_market_source, market_source_error = (
                current_authenticated_market_source(
                    state,
                    now_epoch=now_ts,
                    require_active_lease=True,
                    expected_protocol_version=BRIDGE_PROTOCOL_VERSION,
                )
            )
            if pinned_market_source is None:
                return str(market_source_error or "market_source_unattested")
        latest_ticks = self._latest_market_ticks_for_symbols(
            conn,
            symbols=requested_tick_symbols,
            market_source=pinned_market_source,
        )
        tick = latest_ticks.get(symbol)
        if tick is None:
            return "market_tick_missing"
        tick_age = _timestamp_age_secs(tick.get("ts"), now_ts=now_ts)
        tick_stale_after = max(1.0, float(settings.bridge_stale_tick_secs))
        if tick_age is None:
            return "market_tick_invalid"
        if tick_age > tick_stale_after:
            return "market_tick_stale"
        bid_raw = tick.get("bid")
        ask_raw = tick.get("ask")
        if bid_raw is None or ask_raw is None:
            return "market_tick_invalid"
        try:
            bid = float(bid_raw)
            ask = float(ask_raw)
        except (TypeError, ValueError, OverflowError):
            return "market_tick_invalid"
        if (
            not math.isfinite(bid)
            or not math.isfinite(ask)
            or bid <= 0.0
            or ask <= 0.0
            or ask < bid
        ):
            return "market_tick_invalid"
        if production_scalper:
            current_contracts = project_ig_mt4_selected_contract_universe(
                state,
                selected_symbols=(symbol,),
                now_ts=now_ts,
                max_age_secs=(MAX_PRODUCTION_SCALP_BROKER_CONTRACT_AGE_SECS),
            )
            if not current_contracts.ok:
                return str(current_contracts.errors[0])
            entry_price = (
                ask if str(raw_payload.get("cmd") or "").upper() == "BUY" else bid
            )
            try:
                account_equity = float(state.get("equity") or 0.0)
            except (TypeError, ValueError, OverflowError):
                account_equity = 0.0
            geometry_failure = broker_contract_order_geometry_error(
                raw_payload,
                universe=current_contracts,
                symbol=symbol,
                entry_price=entry_price,
                equity=account_equity,
                side=str(raw_payload.get("cmd") or ""),
                current_bid=bid,
                current_ask=ask,
            )
            if geometry_failure:
                return str(geometry_failure)
            contract = current_contracts.contract_for(symbol)
            if contract is None:
                return "broker_contract_command_spec_missing"
            envelope_failure = production_scalp_market_entry_envelope_error(
                raw_payload,
                symbol=symbol,
                side=str(raw_payload.get("cmd") or ""),
                contract=contract,
                current_bid=bid,
                current_ask=ask,
                now_epoch=now_ts,
            )
            if envelope_failure:
                return str(envelope_failure)
            # AGENT HANDSHAKE: the risk-kernel cash cap is not a lease over the
            # enqueue/poll quote. Revalue it from one fresh persisted quote set
            # before either queue transition can cross the broker boundary.
            conversion_ticks: dict[str, dict[str, Any]] = {}
            for rate_symbol, rate_tick in latest_ticks.items():
                projected_tick = dict(rate_tick)
                rate_age = _timestamp_age_secs(
                    rate_tick.get("ts"),
                    now_ts=now_ts,
                )
                projected_tick["market_event_fresh"] = bool(
                    rate_age is not None and rate_age <= tick_stale_after
                )
                projected_tick["market_event_reason"] = (
                    "ok"
                    if projected_tick["market_event_fresh"]
                    else (
                        "broker_market_event_timestamp_invalid"
                        if rate_age is None
                        else "broker_market_event_stale"
                    )
                )
                conversion_ticks[rate_symbol] = projected_tick
            conversion_projection = project_account_conversion_rates(
                conversion_ticks,
                account_currency=current_contracts.account_currency,
                required_symbols=(symbol,),
                require_market_event_fresh=True,
            )
            cash_risk_failure = broker_contract_order_cash_risk_error(
                raw_payload,
                universe=current_contracts,
                symbol=symbol,
                entry_price=entry_price,
                equity=account_equity,
                quote_rates=conversion_projection.rates,
            )
            if cash_risk_failure:
                return str(cash_risk_failure)
        return ""

    def _poll_entry_authorization_failure(
        self,
        conn,
        *,
        row: dict[str, Any],
        now_ts: float,
    ) -> str:
        """Reauthorize a durable BUY/SELL immediately before delivery."""

        payload = dict(row.get("payload_json") or {})
        return self._live_entry_authorization_failure(
            conn,
            pair=str(row.get("symbol") or payload.get("symbol") or ""),
            expected_account_mode=str(payload.get("expected_account_mode") or ""),
            expected_account_scope=str(payload.get("expected_account_scope") or ""),
            expected_authority_revision=_safe_int(
                payload.get("expected_authority_revision"),
                0,
            ),
            now_ts=now_ts,
            expected_release_generation_id=str(
                payload.get("expected_release_generation_id") or ""
            ),
            expected_release_request_sha256=str(
                payload.get("expected_release_request_sha256") or ""
            ),
            expected_model_identity_sha256=str(
                payload.get("expected_model_identity_sha256") or ""
            ),
            expected_manifest_file_sha256=str(
                payload.get("expected_manifest_file_sha256") or ""
            ),
            expected_runtime_boot_id=str(payload.get("expected_runtime_boot_id") or ""),
            command_payload=payload,
            command_id=str(row.get("command_id") or ""),
        )

    def get_execution_uncertainty(
        self,
        *,
        limit: int = 20,
        symbol: str = "",
    ) -> dict[str, Any]:
        """Return a bounded diagnostic for unresolved broker outcomes.

        This is intentionally a read-only store query. Admission enforcement
        performs the same predicate again inside the enqueue transaction so a
        clear preflight cannot race a delivery transition.
        """

        bounded_limit = max(1, min(int(limit), 100))
        with self._lock:
            with self.engine.begin() as conn:
                uncertainty = self._execution_uncertainty_scope(conn)
        rows = list(uncertainty.get("rows") or [])
        statuses: dict[str, int] = {}
        for row in rows:
            status = str(dict(row or {}).get("status") or "")
            statuses[status] = int(statuses.get(status, 0)) + 1
        blocked = self._execution_uncertainty_blocks_symbol(
            uncertainty,
            symbol=symbol,
        )
        requested_symbol = str(symbol or "").strip().upper()
        return {
            "present": bool(uncertainty.get("present")),
            "blocked": bool(blocked),
            "reason": ("broker_execution_outcome_unresolved" if blocked else ""),
            "count": len(rows),
            "statuses": statuses,
            "scope_contained": bool(uncertainty.get("scope_contained")),
            "scope_reason": str(uncertainty.get("scope_reason") or ""),
            "blocked_symbols": list(uncertainty.get("blocked_symbols") or []),
            "requested_symbol": requested_symbol,
            "commands": [dict(row) for row in rows[:bounded_limit]],
        }

    def enqueue_command(
        self,
        cmd: ExecutionCommand,
        *,
        require_resolved_execution: bool = False,
        required_live_admission: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        state_patch: dict[str, Any] | None = None
        now = _now()
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                command_payload = dict(cmd.payload or {})
                command_verb = str(cmd.cmd or "").strip().upper()
                claimed_admission_mode = (
                    str(command_payload.get("expected_strategy_admission_mode") or "")
                    .strip()
                    .lower()
                )
                if (
                    command_verb in {"BUY", "SELL"}
                    and claimed_admission_mode
                    and claimed_admission_mode != SCALP_ADMISSION_MODE_SIGNED
                ):
                    return (
                        False,
                        "scalp_strategy_admission_mode_signed_validation_required",
                    )
                claims_production_scalper = bool(
                    command_verb in {"BUY", "SELL"}
                    and (
                        str(command_payload.get("strategy_lane") or "").strip().lower()
                        == SCALP_EXECUTION_LANE
                        or str(command_payload.get("intent") or cmd.intent or "")
                        .strip()
                        .lower()
                        == SCALP_ENTRY_INTENT
                    )
                )
                if claims_production_scalper:
                    immediate_failure = production_scalp_immediate_entry_contract_error(
                        command_payload,
                        now_epoch=now,
                    )
                    if immediate_failure:
                        return False, str(immediate_failure)
                    entry_deadline = production_scalp_entry_deadline_epoch(
                        command_payload
                    )
                    if entry_deadline is None or entry_deadline <= now:
                        return False, "scalp_market_entry_deadline_expired"
                    # Repeat the DTO cap while holding the durable queue lock.
                    # A hand-built command object and a caller TTL therefore
                    # cannot outlive the strategy deadline at persistence.
                    cmd.expires_at = min(
                        float(cmd.expires_at),
                        float(entry_deadline),
                    )
                    if cmd.expires_at <= now:
                        return False, "scalp_market_entry_deadline_expired"
                if _is_identifiable_scalp_entry(
                    cmd=cmd.cmd,
                    command_id=cmd.command_id,
                    intent=cmd.intent,
                    payload=cmd.payload,
                ):
                    return False, _DISABLED_SCALP_ENTRY_REASON
                egress_failure = self._execution_egress_authorization_failure(
                    conn,
                    now_ts=now,
                    command=cmd,
                )
                if egress_failure:
                    return False, egress_failure
                existing = (
                    conn.execute(
                        select(self.commands)
                        .where(self.commands.c.command_id == cmd.command_id)
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    existing_row = dict(existing)
                    if _is_exact_expired_exposure_reducing_retry(
                        existing_row,
                        cmd,
                    ):
                        previous_reason = str(existing_row.get("reason") or "")
                        previous_created_at = float(
                            existing_row.get("created_at") or 0.0
                        )
                        previous_updated_at = float(
                            existing_row.get("updated_at") or 0.0
                        )
                        previous_expires_at = float(
                            existing_row.get("expires_at") or 0.0
                        )
                        requeue_reason = "expired_never_delivered_requeued"
                        result = conn.execute(
                            update(self.commands)
                            .where(
                                and_(
                                    self.commands.c.command_id == cmd.command_id,
                                    self.commands.c.status == "expired",
                                    self.commands.c.delivered_count == 0,
                                )
                            )
                            .values(
                                status="queued",
                                created_at=cmd.created_at,
                                updated_at=cmd.updated_at,
                                expires_at=cmd.expires_at,
                                reason=requeue_reason,
                            )
                        )
                        if int(result.rowcount or 0) == 1:
                            self._append_command_event(
                                command_id=cmd.command_id,
                                event_status="requeued",
                                reason=requeue_reason,
                                payload={
                                    "previous_status": "expired",
                                    "previous_reason": previous_reason,
                                    "previous_created_at": previous_created_at,
                                    "previous_updated_at": previous_updated_at,
                                    "previous_expires_at": previous_expires_at,
                                    "requeued_at": float(cmd.updated_at),
                                    "new_expires_at": float(cmd.expires_at),
                                    "delivered_count": 0,
                                },
                                conn=conn,
                            )
                            return True, "queued"
                    return False, str(existing_row.get("status") or "")
                if str(cmd.idempotency_key or "").strip():
                    existing = conn.execute(
                        select(self.commands.c.command_id, self.commands.c.status)
                        .where(
                            and_(
                                self.commands.c.idempotency_key
                                == str(cmd.idempotency_key),
                                self.commands.c.created_at <= now,
                                self.commands.c.expires_at >= now,
                            )
                        )
                        .order_by(self.commands.c.created_at.desc())
                        .limit(1)
                    ).fetchone()
                    if existing is not None:
                        return False, str(existing[1])

                # Duplicate checks deliberately precede the fence: an exact
                # idempotent retry cannot increase exposure and must retain
                # its existing command identity. Every genuinely new entry is
                # checked in this same lock/transaction as the insert.
                if bool(require_resolved_execution) and self._has_execution_uncertainty(
                    conn,
                    symbol=str(cmd.symbol or ""),
                ):
                    return False, "reconciliation_required"

                if required_live_admission:
                    required = dict(required_live_admission or {})
                    expected_mode = (
                        str(required.get("broker_account_mode") or "").strip().lower()
                    )
                    expected_scope = str(
                        required.get("broker_account_scope") or ""
                    ).strip()
                    expected_revision = _safe_int(
                        required.get("authority_revision"),
                        0,
                    )
                    expected_release_generation_id = str(
                        required.get("release_generation_id") or ""
                    )
                    expected_release_request_sha256 = str(
                        required.get("release_request_sha256") or ""
                    )
                    expected_model_identity_sha256 = str(
                        required.get("model_identity_sha256") or ""
                    )
                    expected_manifest_file_sha256 = str(
                        required.get("manifest_file_sha256") or ""
                    )
                    expected_runtime_boot_id = str(
                        required.get("runtime_boot_id") or ""
                    )
                    pair = str(required.get("pair") or cmd.symbol or "").strip().upper()
                    payload = dict(cmd.payload or {})
                    if str(cmd.cmd or "").strip().upper() not in {"BUY", "SELL"}:
                        return False, "final_entry_approval_missing"
                    if str(cmd.symbol or "").strip().upper() != pair:
                        return False, "live_pair_not_allowlisted"
                    if (
                        str(payload.get("expected_account_mode") or "").strip().lower()
                        != expected_mode
                    ):
                        return False, "broker_account_mode_approval_mismatch"
                    if (
                        str(payload.get("expected_account_scope") or "").strip()
                        != expected_scope
                    ):
                        return False, "broker_account_scope_approval_mismatch"
                    if (
                        _safe_int(payload.get("expected_authority_revision"), 0)
                        != expected_revision
                    ):
                        return False, "live_authority_revision_approval_mismatch"
                    admission_failure = self._live_entry_authorization_failure(
                        conn,
                        pair=pair,
                        expected_account_mode=expected_mode,
                        expected_account_scope=expected_scope,
                        expected_authority_revision=expected_revision,
                        now_ts=now,
                        expected_release_generation_id=expected_release_generation_id,
                        expected_release_request_sha256=expected_release_request_sha256,
                        expected_model_identity_sha256=expected_model_identity_sha256,
                        expected_manifest_file_sha256=expected_manifest_file_sha256,
                        expected_runtime_boot_id=expected_runtime_boot_id,
                        command_payload=payload,
                        command_id=str(cmd.command_id or ""),
                        expected_strategy_authority=(
                            dict(required.get("strategy_authority") or {})
                            if required.get("strategy_authority")
                            else None
                        ),
                    )
                    if admission_failure:
                        return False, admission_failure

                conn.execute(
                    self.commands.insert().values(
                        command_id=cmd.command_id,
                        session_id=cmd.session_id,
                        proto=cmd.proto,
                        cmd=cmd.cmd,
                        symbol=cmd.symbol,
                        lots=cmd.lots,
                        tp_cash=cmd.tp_cash,
                        tp_price=cmd.tp_price,
                        sl_price=cmd.sl_price,
                        magic=cmd.magic,
                        intent=cmd.intent,
                        trace_id=cmd.trace_id,
                        correlation_id=cmd.correlation_id,
                        thread_id=cmd.thread_id,
                        idempotency_key=cmd.idempotency_key,
                        schema_version=cmd.schema_version,
                        orchestration_meta_json=dict(cmd.orchestration_meta_json or {}),
                        status="queued",
                        created_at=cmd.created_at,
                        updated_at=cmd.updated_at,
                        expires_at=cmd.expires_at,
                        delivered_count=0,
                        reason="",
                        payload_json=cmd.payload,
                        ack_json={},
                    )
                )
                self._append_command_event(
                    command_id=cmd.command_id,
                    event_status="queued",
                    reason="queued",
                    payload=cmd.to_dict(),
                    conn=conn,
                )
                state_patch = {
                    "command_id": cmd.command_id,
                    "cmd": cmd.cmd,
                    "symbol": cmd.symbol,
                }

            if state_patch is not None:
                state = self.get_state()
                self.update_state_patch(
                    {
                        "signals_sent": int(state.get("signals_sent", 0)) + 1,
                        "last_signal": {
                            "command_id": str(state_patch["command_id"]),
                            "cmd": str(state_patch["cmd"]),
                            "symbol": str(state_patch["symbol"]),
                            "ts": _now(),
                        },
                    }
                )
            return True, "queued"

    def get_active_command_by_idempotency_key(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        now = _now()
        with self._lock:
            with self.engine.begin() as conn:
                row = (
                    conn.execute(
                        select(self.commands)
                        .where(
                            and_(
                                self.commands.c.idempotency_key == key,
                                self.commands.c.created_at <= now,
                                self.commands.c.expires_at >= now,
                            )
                        )
                        .order_by(self.commands.c.created_at.desc())
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    return None
                return dict(row)

    def get_latest_tick(self, symbol: str) -> dict[str, Any] | None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        stmt = (
            select(self.market_ticks)
            .where(self.market_ticks.c.symbol == sym)
            .order_by(self.market_ticks.c.ts.desc())
            .limit(1)
        )
        with self.engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            return None
        out = dict(row)
        raw = dict(out.get("raw_json") or {})
        nested_raw = (
            dict(raw.get("raw") or {}) if isinstance(raw.get("raw"), dict) else {}
        )
        for key in ("bid", "ask", "mid"):
            try:
                current = float(out.get(key))
            except (TypeError, ValueError, OverflowError):
                current = 0.0
            if math.isfinite(current) and current > 0.0:
                continue
            for source in (raw, nested_raw):
                try:
                    candidate = float(source.get(key))
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(candidate) and candidate > 0.0:
                    out[key] = candidate
                    break
        return out

    def poll_next_command(self) -> ExecutionCommand | None:
        now = _now()
        with self._lock:
            self.cleanup_expired_commands()
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                self._quarantine_disabled_scalp_entries(conn, now_ts=now)
                egress_failure = self._execution_egress_authorization_failure(
                    conn,
                    now_ts=now,
                )
                if egress_failure:
                    self._quarantine_execution_queue(
                        conn,
                        reason=f"poll_egress_revoked:{egress_failure}",
                        now_ts=now,
                        # Boot recovery can intentionally retain exact queued
                        # protective exits. A poll while broker egress is
                        # disabled must not erase them before the new boot is
                        # armed; the early return still forbids delivery.
                        preserve_queued_exposure_reducing=True,
                    )
                    return None
                execution_uncertainty = self._execution_uncertainty_scope(
                    conn,
                    now_ts=now,
                )
                rows = (
                    conn.execute(
                        select(self.commands)
                        .where(
                            and_(
                                self.commands.c.status == "queued",
                                self.commands.c.created_at <= now,
                                self.commands.c.expires_at >= now,
                            )
                        )
                        .order_by(self.commands.c.created_at.asc())
                    )
                    .mappings()
                    .all()
                )
                row: dict[str, Any] | None = None
                pending_rejections: list[tuple[str, str, str, float]] = []
                for queued_row in rows:
                    candidate = dict(queued_row)
                    candidate_command = ExecutionCommand(
                        command_id=str(candidate.get("command_id") or ""),
                        session_id=str(candidate.get("session_id") or ""),
                        proto=str(candidate.get("proto") or "v2"),
                        cmd=str(candidate.get("cmd") or ""),
                        symbol=str(candidate.get("symbol") or ""),
                        lots=float(candidate.get("lots") or 0.0),
                        magic=_safe_int(candidate.get("magic"), 0),
                        target_ticket=_safe_int(
                            dict(candidate.get("payload_json") or {}).get(
                                "target_ticket"
                            ),
                            -1,
                        ),
                        owner_token=str(
                            dict(candidate.get("payload_json") or {}).get("owner_token")
                            or ""
                        ),
                        ownership_contract=str(
                            dict(candidate.get("payload_json") or {}).get(
                                "ownership_contract"
                            )
                            or ""
                        ),
                        intent=str(candidate.get("intent") or "UNKNOWN"),
                        orchestration_meta_json=dict(
                            candidate.get("orchestration_meta_json") or {}
                        ),
                        payload=dict(candidate.get("payload_json") or {}),
                    )
                    command_egress_failure = (
                        self._execution_egress_authorization_failure(
                            conn,
                            now_ts=now,
                            command=candidate_command,
                        )
                    )
                    if command_egress_failure:
                        pending_rejections.append(
                            (
                                str(candidate["command_id"]),
                                f"poll_egress_revoked:{command_egress_failure}",
                                str(command_egress_failure),
                                now,
                            )
                        )
                        continue
                    exposure_increasing = str(
                        candidate.get("cmd") or ""
                    ).strip().upper() in {"BUY", "SELL"}
                    if exposure_increasing:
                        if pending_rejections:
                            # Daily entry-budget checks count queued rows. Make
                            # every earlier refusal visible before evaluating a
                            # later entry, while still batching the write burst.
                            self._expire_poll_rejections(
                                conn,
                                rejections=pending_rejections,
                            )
                            pending_rejections.clear()
                        try:
                            authorization_failure = (
                                self._poll_entry_authorization_failure(
                                    conn,
                                    row=candidate,
                                    now_ts=now,
                                )
                            )
                        except Exception:
                            # An unavailable authority check is itself a hard
                            # failure for new exposure. Expire the command so
                            # it cannot become executable after a later poll;
                            # protective commands behind it remain available.
                            authorization_failure = "poll_authority_check_failed"
                        if authorization_failure:
                            pending_rejections.append(
                                (
                                    str(candidate["command_id"]),
                                    f"poll_authority_revoked:{authorization_failure}",
                                    str(authorization_failure),
                                    now,
                                )
                            )
                            continue
                        if self._execution_uncertainty_blocks_symbol(
                            execution_uncertainty,
                            symbol=str(candidate.get("symbol") or ""),
                        ):
                            # A prior broker outcome is unresolved. The entry
                            # remains queued, but exits and protection may pass.
                            continue
                        # AGENT HOT PATH: authority evaluation can cross the
                        # immutable scalp deadline.  Resample immediately
                        # before delivery and repeat entry authorization so a
                        # command that was valid at poll start cannot be
                        # handed to MT4 after its execution window closes.
                        delivery_now = _now()
                        try:
                            authorization_failure = (
                                self._poll_entry_authorization_failure(
                                    conn,
                                    row=candidate,
                                    now_ts=delivery_now,
                                )
                            )
                        except Exception:
                            authorization_failure = "poll_authority_check_failed"
                        if authorization_failure:
                            pending_rejections.append(
                                (
                                    str(candidate["command_id"]),
                                    f"poll_authority_revoked:{authorization_failure}",
                                    str(authorization_failure),
                                    delivery_now,
                                )
                            )
                            continue
                        now = delivery_now
                    row = candidate
                    break
                if pending_rejections:
                    self._expire_poll_rejections(
                        conn,
                        rejections=pending_rejections,
                    )
                if row is None:
                    return None

                conn.execute(
                    update(self.commands)
                    .where(self.commands.c.command_id == row["command_id"])
                    .values(
                        status="delivered",
                        updated_at=now,
                        delivered_count=int(row.get("delivered_count", 0)) + 1,
                    )
                )
                self._append_command_event(
                    command_id=str(row["command_id"]),
                    event_status="delivered",
                    reason="polled",
                    conn=conn,
                )

                row["status"] = "delivered"
                row["updated_at"] = now
                row["delivered_count"] = int(row.get("delivered_count", 0)) + 1
                return ExecutionCommand(
                    command_id=str(row["command_id"]),
                    session_id=str(row["session_id"]),
                    proto=str(row["proto"]),
                    cmd=str(row["cmd"]),
                    symbol=str(row.get("symbol") or ""),
                    lots=float(row.get("lots") or 0.0),
                    tp_cash=row.get("tp_cash"),
                    tp_price=row.get("tp_price"),
                    sl_price=row.get("sl_price"),
                    close_lots=float(
                        (dict(row.get("payload_json") or {})).get("close_lots", 0.0)
                        or 0.0
                    ),
                    magic=int(row.get("magic") or 246810),
                    target_ticket=_safe_int(
                        dict(row.get("payload_json") or {}).get("target_ticket"),
                        -1,
                    ),
                    owner_token=str(
                        dict(row.get("payload_json") or {}).get("owner_token") or ""
                    ),
                    ownership_contract=str(
                        dict(row.get("payload_json") or {}).get("ownership_contract")
                        or ""
                    ),
                    intent=str(row.get("intent") or "UNKNOWN"),
                    trace_id=str(row.get("trace_id") or ""),
                    correlation_id=str(row.get("correlation_id") or ""),
                    thread_id=str(row.get("thread_id") or ""),
                    idempotency_key=str(row.get("idempotency_key") or ""),
                    schema_version=str(row.get("schema_version") or ""),
                    orchestration_meta_json=dict(
                        row.get("orchestration_meta_json") or {}
                    ),
                    action=str(
                        (dict(row.get("payload_json") or {})).get("action") or ""
                    ),
                    action_score=float(
                        (dict(row.get("payload_json") or {})).get("action_score", 0.0)
                        or 0.0
                    ),
                    reversal_token=str(
                        (dict(row.get("payload_json") or {})).get("reversal_token")
                        or ""
                    ),
                    status="delivered",
                    created_at=float(row.get("created_at") or now),
                    updated_at=now,
                    expires_at=float(row.get("expires_at") or now),
                    delivered_count=int(row.get("delivered_count") or 1),
                    payload=dict(row.get("payload_json") or {}),
                )

    def ack_command(self, ack: ExecutionAck) -> tuple[dict[str, Any], int]:
        command_id = str(ack.command_id or "").strip()
        idempotency_key = str(ack.idempotency_key or "").strip()
        if not command_id and not idempotency_key:
            return {"status": "error", "reason": "missing_command_id"}, 400

        reported_status = str(ack.status).lower().strip()
        if reported_status not in {
            "delivered",
            "acked",
            "failed",
            "duplicate",
            "reconcile_required",
        }:
            reported_status = "reconcile_required"

        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                row = None
                if command_id:
                    row = (
                        conn.execute(
                            select(self.commands).where(
                                self.commands.c.command_id == command_id
                            )
                        )
                        .mappings()
                        .first()
                    )
                if row is None and idempotency_key:
                    row = (
                        conn.execute(
                            select(self.commands)
                            .where(
                                and_(
                                    self.commands.c.idempotency_key == idempotency_key,
                                    self.commands.c.expires_at >= _now(),
                                )
                            )
                            .order_by(self.commands.c.created_at.desc())
                            .limit(1)
                        )
                        .mappings()
                        .first()
                    )
                if row is None:
                    out = {"status": "not_found"}
                    if command_id:
                        out["command_id"] = command_id
                    if idempotency_key:
                        out["idempotency_key"] = idempotency_key
                    return out, 404

                command_id = str(row["command_id"])
                cur = str(row["status"])
                delivered_before = int(row.get("delivered_count", 0) or 0) > 0
                ack_payload = ack.to_dict()
                paper_attestation = _paper_ack_attestation(
                    row=dict(row),
                    status=reported_status,
                    ack_payload=ack_payload,
                )
                if paper_attestation is not None:
                    attestation = paper_attestation
                else:
                    # Every active non-paper provider mutates the MT4 broker.
                    # A bare positive ticket is not proof that the broker
                    # executed this command or respected its economic bounds,
                    # so all such ACKs share the exact source-attested policy.
                    attestation = classify_execution_ack(
                        dict(row), ack_payload
                    ).to_dict()
                    attestation["policy_scope"] = "production_mt4_exact"
                requested_status = str(
                    attestation.get("effective_status") or "reconcile_required"
                )
                incoming_semantics = _execution_ack_semantic_identity(
                    attestation=attestation,
                    ack_payload=ack_payload,
                )
                policy_scope = str(attestation.get("policy_scope") or "")
                actual_ticket = _safe_int(
                    dict(attestation.get("actuals") or {}).get("ticket"), -1
                )
                store_reasons: tuple[str, ...] = ()
                expired_exact_resolution = False

                # An expired row is terminal only when it was never handed to
                # the EA and a late ACK independently confirms no mutation. A
                # broker ticket or attempted/confirmed outcome contradicts the
                # queue history and can never be discarded as idempotent.
                if cur == "expired" and not delivered_before:
                    exact_success = bool(
                        requested_status == "acked"
                        and attestation.get("attested") is True
                        and actual_ticket > 0
                        and policy_scope
                        in {"production_mt4_exact", "paper_simulation"}
                    )
                    conclusive_no_mutation = bool(
                        requested_status in {"failed", "duplicate", "delivered"}
                        and actual_ticket <= 0
                        and str(attestation.get("mutation_state") or "")
                        == "not_attempted"
                    )
                    if exact_success:
                        expired_exact_resolution = True
                    elif conclusive_no_mutation:
                        return {
                            "status": cur,
                            "command_id": command_id,
                            "idempotent": True,
                        }, 200
                    else:
                        requested_status = "reconcile_required"
                        store_reasons = ("expired_never_delivered_ack_contradiction",)

                existing_ack = dict(row.get("ack_json") or {})
                existing_semantics = dict(
                    existing_ack.get("execution_ack_semantic_identity") or {}
                )
                if cur in {"acked", "failed", "duplicate"}:
                    if (
                        requested_status == cur
                        and existing_semantics
                        and incoming_semantics == existing_semantics
                    ):
                        return {
                            "status": cur,
                            "command_id": command_id,
                            "idempotent": True,
                        }, 200
                    requested_status = "reconcile_required"
                    store_reasons = tuple(
                        dict.fromkeys((*store_reasons, "terminal_ack_contradiction"))
                    )

                can_finalize = (
                    cur in {"delivered", "reconcile_required"}
                    or (cur in {"queued", "expired"} and delivered_before)
                    or expired_exact_resolution
                )
                if (
                    requested_status in {"acked", "failed", "duplicate"}
                    and not can_finalize
                ):
                    return {
                        "status": "invalid_transition",
                        "command_id": command_id,
                        "current": cur,
                        "requested": requested_status,
                        "allowed": ["delivered"]
                        if cur == "queued"
                        else [
                            "delivered",
                            "acked",
                            "failed",
                            "duplicate",
                            "reconcile_required",
                        ],
                    }, 409

                # Once a command is uncertain, a later ordinary refusal cannot
                # prove that a previously attempted broker mutation did not
                # happen. Only an exact positive-ticket success attestation can
                # resolve that ambiguity through this ACK endpoint.
                if cur == "reconcile_required" and not (
                    requested_status == "acked"
                    and attestation.get("attested") is True
                    and actual_ticket > 0
                    and policy_scope in {"production_mt4_exact", "paper_simulation"}
                ):
                    if existing_semantics and incoming_semantics == existing_semantics:
                        return {
                            "status": "reconcile_required",
                            "command_id": command_id,
                            "idempotent": True,
                        }, 200
                    requested_status = "reconcile_required"
                    store_reasons = tuple(
                        dict.fromkeys((*store_reasons, "reconciliation_sticky"))
                    )

                entry_trade = bool(
                    str(row.get("cmd") or "").strip().upper() in {"BUY", "SELL"}
                    and requested_status == "acked"
                    and attestation.get("attested") is True
                    and actual_ticket > 0
                )
                terminal_safe = _execution_ack_terminal_safe(
                    attestation=attestation,
                    status=requested_status,
                    ticket=actual_ticket,
                )
                attestation_schema = str(
                    attestation.get("schema_version") or ""
                ).strip()
                mutation_state = str(attestation.get("mutation_state") or "").strip()
                enriched_ack = _enriched_execution_ack(
                    ack_payload=ack_payload,
                    attestation=attestation,
                    status=requested_status,
                    count_as_trade=entry_trade,
                    store_reasons=store_reasons,
                )
                attestation_reasons = tuple(attestation.get("reasons") or ())
                durable_reasons = tuple(
                    dict.fromkeys((*store_reasons, *attestation_reasons))
                )
                reason = str(ack.message or "").strip()
                if requested_status == "reconcile_required" and durable_reasons:
                    reason = ":".join(
                        (
                            "execution_ack_reconciliation_required",
                            ",".join(durable_reasons),
                        )
                    )

                prior_acked_event = conn.execute(
                    select(self.command_events.c.id)
                    .where(
                        and_(
                            self.command_events.c.command_id == command_id,
                            self.command_events.c.event_status == "acked",
                        )
                    )
                    .limit(1)
                ).first()
                count_as_trade = bool(
                    requested_status == "acked"
                    and enriched_ack.get("count_as_trade") is True
                    and prior_acked_event is None
                )

                conn.execute(
                    update(self.commands)
                    .where(self.commands.c.command_id == command_id)
                    .values(
                        status=requested_status,
                        updated_at=ack.updated_at,
                        ack_json=enriched_ack,
                        ack_policy_scope=policy_scope or None,
                        ack_attestation_schema=attestation_schema or None,
                        ack_terminal_safe=bool(terminal_safe),
                        ack_ticket=(actual_ticket if actual_ticket > 0 else None),
                        ack_mutation_state=mutation_state or None,
                        reason=reason,
                        delivered_count=(
                            int(row.get("delivered_count", 0) or 0) + 1
                            if requested_status == "delivered"
                            else int(row.get("delivered_count", 0) or 0)
                        ),
                    )
                )
                self._append_command_event(
                    command_id=command_id,
                    event_status=requested_status,
                    reason=reason,
                    payload=enriched_ack,
                    conn=conn,
                )
                state_row = conn.execute(
                    select(self.runtime_state.c.snapshot_json)
                    .where(self.runtime_state.c.id == 1)
                    .with_for_update()
                ).first()
                state = dict(
                    state_row[0] if state_row and isinstance(state_row[0], dict) else {}
                )
                state["last_ack"] = enriched_ack
                if count_as_trade:
                    state["trades_executed"] = (
                        int(state.get("trades_executed", 0) or 0) + 1
                    )
                state["last_update"] = _now()
                if state_row is None:
                    conn.execute(
                        self.runtime_state.insert().values(
                            id=1,
                            snapshot_json=state,
                            updated_at=float(state["last_update"]),
                        )
                    )
                else:
                    conn.execute(
                        update(self.runtime_state)
                        .where(self.runtime_state.c.id == 1)
                        .values(
                            snapshot_json=state,
                            updated_at=float(state["last_update"]),
                        )
                    )
            out = {"status": requested_status, "command_id": command_id}
            if requested_status == "reconcile_required":
                out["reported_status"] = reported_status
                out["reasons"] = list(
                    dict.fromkeys(
                        (
                            *tuple(store_reasons),
                            *tuple(attestation.get("reasons") or ()),
                        )
                    )
                )
            if idempotency_key and not command_id == str(ack.command_id or "").strip():
                out["idempotency_key"] = idempotency_key
            return out, 200

    def record_tick(self, payload: dict[str, Any]) -> None:
        self.record_ticks([payload])

    def record_ticks(self, payloads: list[dict[str, Any]]) -> None:
        if not payloads:
            return
        received_at = _now()

        def _positive_or_none(value: Any) -> float | None:
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return number if math.isfinite(number) and number > 0.0 else None

        rows: list[dict[str, Any]] = []
        for payload in payloads:
            sym = str(payload.get("symbol", "")).strip().upper()
            if not sym:
                continue
            observed_at = _parse_iso_ts(
                payload.get("time") or payload.get("ts") or payload.get("timestamp")
            )
            if (
                not math.isfinite(observed_at)
                or observed_at <= 0.0
                or observed_at > received_at + 5.0
            ):
                observed_at = received_at

            market_source, _market_source_error = authenticated_market_source_from_row(
                payload
            )
            source_fields = (
                market_source.to_fields() if market_source is not None else {}
            )
            rows.append(
                {
                    "symbol": sym,
                    "bid": _positive_or_none(payload.get("bid")),
                    "ask": _positive_or_none(payload.get("ask")),
                    "spread": float(payload.get("spread", 0.0) or 0.0),
                    "ts": float(observed_at),
                    "market_source_schema": str(
                        source_fields.get("market_source_schema") or ""
                    )
                    or None,
                    "market_source_id": str(source_fields.get("market_source_id") or "")
                    or None,
                    "market_source_authenticated": (
                        1 if market_source is not None else 0
                    ),
                    "broker_account_scope": str(
                        source_fields.get("broker_account_scope") or ""
                    )
                    or None,
                    "broker_venue_id": str(source_fields.get("broker_venue_id") or "")
                    or None,
                    "producer_identity": str(
                        source_fields.get("producer_identity") or ""
                    )
                    or None,
                    "producer_instance_id": str(
                        source_fields.get("producer_instance_id") or ""
                    )
                    or None,
                    "terminal_lease_scope": str(
                        source_fields.get("terminal_lease_scope") or ""
                    )
                    or None,
                    "credential_generation_id": str(
                        source_fields.get("credential_generation_id") or ""
                    )
                    or None,
                    "bridge_protocol_version": str(
                        source_fields.get("bridge_protocol_version") or ""
                    )
                    or None,
                    "raw_json": dict(payload),
                }
            )

        if not rows:
            return
        with self.engine.begin() as conn:
            conn.execute(self.market_ticks.insert(), rows)

    def record_report(
        self, report_text: str, report_json: dict[str, Any] | None = None
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                self.reports.insert().values(
                    ts=_now(), report_text=report_text, report_json=report_json or {}
                )
            )

    def store_decisions(
        self,
        *,
        decisions: list[dict[str, Any]],
        vol: float,
        diagnostics: dict[str, Any],
    ) -> None:
        """Append decision telemetry without expanding the authority state row.

        ``runtime_state`` is read and row-locked by command admission, broker
        polling, and operator safety mutations.  Decision payloads already
        have a dedicated indexed history table, so copying them into that hot
        row makes every unrelated state operation deserialize and rewrite the
        same large telemetry document.
        """

        with self.engine.begin() as conn:
            conn.execute(
                self.decision_snapshots.insert().values(
                    ts=_now(),
                    vol=float(vol),
                    decisions_json=list(decisions or []),
                    diagnostics_json=dict(diagnostics or {}),
                )
            )

    def store_orchestration_bundle(
        self,
        *,
        context: dict[str, Any],
        packet: dict[str, Any],
        trace: dict[str, Any],
        runtime_mode: str,
        fallback_used: bool,
    ) -> None:
        context_json = dict(context or {})
        packet_json = dict(packet or {})
        trace_json = dict(trace or {})
        packet_fallback_used = bool(
            packet_json.get("fallback_used", False) or fallback_used
        )
        packet_json["fallback_used"] = bool(packet_fallback_used)
        governed = dict(packet_json.get("governed_decision") or {})
        proposals = list(packet_json.get("proposals") or [])
        run_id = str(packet_json.get("run_id") or "")
        if not run_id:
            raise ValueError("packet run_id is required")
        version_bundle = dict(context_json.get("version_bundle") or {})
        trace_run_id = str(trace_json.get("run_id") or run_id)
        ts_utc = context_json.get("ts_utc") or packet_json.get("ts_utc")
        ts_value = _parse_iso_ts(ts_utc)
        now = _now()
        proposal_rows: list[dict[str, Any]] = []
        for proposal in proposals:
            item = dict(proposal or {})
            proposal_rows.append(
                {
                    "proposal_id": str(item.get("proposal_id") or ""),
                    "run_id": run_id,
                    "agent_id": str(item.get("agent_id") or ""),
                    "phase": str(item.get("phase") or ""),
                    "intent": str(item.get("intent") or ""),
                    "side": str(item.get("side") or ""),
                    "confidence": float(item.get("confidence") or 0.0),
                    "expected_edge_bps": float(item.get("expected_edge_bps") or 0.0),
                    "uncertainty": float(item.get("uncertainty") or 0.0),
                    "risk_cost": float(item.get("risk_cost") or 0.0),
                    "ttl_ms": int(item.get("ttl_ms") or 0),
                    "evidence_json": list(item.get("evidence_refs") or []),
                    "constraints_json": dict(item.get("constraints") or {}),
                    "advisory_only": 1 if bool(item.get("advisory_only", True)) else 0,
                    "created_at": now,
                }
            )
        with self.engine.begin() as conn:
            conn.execute(
                delete(self.agent_proposals).where(
                    self.agent_proposals.c.run_id == run_id
                )
            )
            conn.execute(
                delete(self.agent_traces).where(self.agent_traces.c.run_id == run_id)
            )
            conn.execute(
                delete(self.governed_decisions).where(
                    self.governed_decisions.c.run_id == run_id
                )
            )
            conn.execute(
                delete(self.orchestration_runs).where(
                    self.orchestration_runs.c.run_id == run_id
                )
            )

            conn.execute(
                self.orchestration_runs.insert().values(
                    run_id=run_id,
                    cycle_id=str(context_json.get("cycle_id") or ""),
                    thread_id=str(context_json.get("thread_id") or ""),
                    correlation_id=str(context_json.get("correlation_id") or ""),
                    pair=str(context_json.get("pair") or packet_json.get("pair") or ""),
                    ts_utc=float(ts_value if ts_value > 0 else now),
                    runtime_mode=str(runtime_mode),
                    latency_ms=int(packet_json.get("latency_ms") or 0),
                    fallback_used=1 if bool(packet_fallback_used) else 0,
                    version_bundle_json=version_bundle,
                    packet_json=packet_json,
                    created_at=now,
                )
            )
            if governed:
                conn.execute(
                    self.governed_decisions.insert().values(
                        decision_id=str(governed.get("decision_id") or ""),
                        run_id=run_id,
                        runtime_mode=str(runtime_mode),
                        allowed=1 if bool(governed.get("allowed", False)) else 0,
                        selected_action=str(governed.get("selected_action") or ""),
                        command_preview_json=dict(governed.get("command_preview") or {})
                        or None,
                        blocking_reasons_json=list(
                            governed.get("blocking_reasons") or []
                        ),
                        approval_state=str(governed.get("approval_state") or "auto"),
                        governor_version=str(governed.get("governor_version") or ""),
                        version_bundle_json=version_bundle or None,
                        invariants_ok=1
                        if bool(governed.get("invariants_ok", False))
                        else 0,
                        created_at=now,
                    )
                )
            if proposal_rows:
                conn.execute(self.agent_proposals.insert(), proposal_rows)
            conn.execute(
                self.agent_traces.insert().values(
                    trace_id=str(trace_json.get("trace_id") or ""),
                    run_id=trace_run_id,
                    pair=str(context_json.get("pair") or packet_json.get("pair") or ""),
                    trace_json=trace_json,
                    created_at=now,
                )
            )

    def update_state_patch(
        self,
        patch: dict[str, Any],
        *,
        runtime_diag_patch: dict[str, Any] | None = None,
        runtime_diag_remove: tuple[str, ...] = (),
    ) -> None:
        self._commit_state_patch(
            patch,
            runtime_diag_patch=runtime_diag_patch,
            runtime_diag_remove=runtime_diag_remove,
            decision_snapshot=None,
        )

    def commit_state_and_decisions(
        self,
        patch: dict[str, Any],
        *,
        runtime_diag_patch: dict[str, Any] | None = None,
        runtime_diag_remove: tuple[str, ...] = (),
        decisions: list[dict[str, Any]],
        vol: float,
        diagnostics: dict[str, Any],
    ) -> None:
        self._commit_state_patch(
            patch,
            runtime_diag_patch=runtime_diag_patch,
            runtime_diag_remove=runtime_diag_remove,
            decision_snapshot=(
                list(decisions or []),
                float(vol),
                dict(diagnostics or {}),
            ),
        )

    def _commit_state_patch(
        self,
        patch: dict[str, Any],
        *,
        runtime_diag_patch: dict[str, Any] | None,
        runtime_diag_remove: tuple[str, ...],
        decision_snapshot: tuple[
            list[dict[str, Any]],
            float,
            dict[str, Any],
        ]
        | None,
    ) -> None:
        incoming = dict(patch or {})
        if (runtime_diag_patch is not None or runtime_diag_remove) and (
            "runtime_diag" in incoming
        ):
            raise ValueError(
                "runtime_diag cannot be supplied with a nested diagnostic mutation"
            )
        incoming_runtime_diag_patch = (
            dict(runtime_diag_patch) if runtime_diag_patch is not None else None
        )
        incoming_runtime_diag_remove = tuple(
            dict.fromkeys(str(key) for key in runtime_diag_remove if str(key))
        )
        force_prune = bool(incoming.pop("__prune_stale__", False))
        expected_live_authority = incoming.pop(
            "__expected_orchestration_live_authority__",
            None,
        )
        # These fields are release-CAS owned. A runner cycle, bridge caller,
        # or generic state repair may disable trading through the dedicated
        # safety path, but can never mint or re-enable broker egress by
        # overwriting JSON state.
        incoming.pop("release_authority", None)
        incoming.pop("release_witness_nonce_ledger", None)
        incoming.pop("execution_egress_enabled", None)
        incoming.pop("execution_egress_authority", None)
        incoming.pop("production_scalp_authority", None)
        with self._lock:
            with self.engine.begin() as conn:
                row = conn.execute(
                    select(self.runtime_state.c.snapshot_json)
                    .where(self.runtime_state.c.id == 1)
                    .with_for_update()
                ).first()
                merged = dict(row[0] if row and isinstance(row[0], dict) else {})
                if (
                    incoming_runtime_diag_patch is not None
                    or incoming_runtime_diag_remove
                ):
                    merged_runtime_diag = dict(merged.get("runtime_diag") or {})
                    for key in incoming_runtime_diag_remove:
                        merged_runtime_diag.pop(key, None)
                    if incoming_runtime_diag_patch is not None:
                        merged_runtime_diag.update(incoming_runtime_diag_patch)
                    incoming["runtime_diag"] = merged_runtime_diag
                if isinstance(incoming.get("runtime_diag"), dict):
                    current_runtime_diag = dict(merged.get("runtime_diag") or {})
                    current_live = dict(
                        current_runtime_diag.get("orchestration_live") or {}
                    )
                    incoming_runtime_diag = dict(incoming.get("runtime_diag") or {})
                    if "orchestration_live" in incoming_runtime_diag:
                        incoming_live = dict(
                            incoming_runtime_diag.get("orchestration_live") or {}
                        )
                        release_is_active = (
                            str(
                                dict(merged.get("release_authority") or {}).get(
                                    "status"
                                )
                                or ""
                            )
                            .strip()
                            .lower()
                            == "active"
                        )
                        if release_is_active or bool(
                            merged.get("execution_egress_enabled", False)
                        ):
                            # Live scopes are part of the signed release
                            # request. Runtime telemetry patches may update
                            # observations, never authority or scope.
                            _preserve_selected_state_fields(
                                incoming=incoming_live,
                                current=current_live,
                                fields=_ORCHESTRATION_LIVE_AUTHORITY_FIELDS,
                            )
                        if isinstance(expected_live_authority, dict):
                            current_authority = _selected_state_fields(
                                current_live,
                                _ORCHESTRATION_LIVE_AUTHORITY_FIELDS,
                            )
                            expected_authority = _selected_state_fields(
                                expected_live_authority,
                                _ORCHESTRATION_LIVE_AUTHORITY_FIELDS,
                            )
                            if current_authority != expected_authority:
                                _preserve_selected_state_fields(
                                    incoming=incoming_live,
                                    current=current_live,
                                    fields=_ORCHESTRATION_LIVE_AUTHORITY_FIELDS,
                                )
                                current_stage_identity = _selected_state_fields(
                                    current_live,
                                    _ORCHESTRATION_LIVE_STAGE_IDENTITY_FIELDS,
                                )
                                expected_stage_identity = _selected_state_fields(
                                    expected_live_authority,
                                    _ORCHESTRATION_LIVE_STAGE_IDENTITY_FIELDS,
                                )
                                if current_stage_identity != expected_stage_identity:
                                    _preserve_selected_state_fields(
                                        incoming=incoming_live,
                                        current=current_live,
                                        fields=_ORCHESTRATION_LIVE_ENTRY_EVIDENCE_FIELDS,
                                    )
                        current_revision = max(
                            0,
                            _safe_int(current_live.get("authority_revision"), 0),
                        )
                        authority_changed = _selected_state_fields(
                            incoming_live,
                            _ORCHESTRATION_LIVE_REVISION_TRIGGER_FIELDS,
                        ) != _selected_state_fields(
                            current_live,
                            _ORCHESTRATION_LIVE_REVISION_TRIGGER_FIELDS,
                        )
                        admission_changed = (
                            "live_command_admission" in incoming_runtime_diag
                            and dict(
                                incoming_runtime_diag.get("live_command_admission")
                                or {}
                            )
                            != dict(
                                current_runtime_diag.get("live_command_admission") or {}
                            )
                        )
                        incoming_live["authority_revision"] = int(
                            current_revision
                            + (1 if authority_changed or admission_changed else 0)
                        )
                        incoming_runtime_diag["orchestration_live"] = incoming_live
                        incoming["runtime_diag"] = incoming_runtime_diag
                previous_profile = str(merged.get("runtime_profile", "") or "")
                merged.update(incoming)
                next_profile = str(merged.get("runtime_profile", "") or "")
                s = _get_settings()
                should_prune = bool(force_prune) or (
                    bool(s.runtime_state_prune_stale_keys)
                    and bool(next_profile)
                    and next_profile != previous_profile
                )
                if should_prune:
                    protected_state_keys = {
                        "release_authority",
                        "release_witness_nonce_ledger",
                        "execution_egress_enabled",
                        "execution_egress_authority",
                        "production_scalp_authority",
                        "runtime_attestation",
                    }
                    for stale_key in s.runtime_state_stale_keys:
                        if (
                            stale_key
                            and stale_key not in protected_state_keys
                            and stale_key not in incoming
                            and stale_key in merged
                        ):
                            merged.pop(stale_key, None)
                merged["last_update"] = _now()
                if row is None:
                    conn.execute(
                        self.runtime_state.insert().values(
                            id=1,
                            snapshot_json=merged,
                            updated_at=float(merged["last_update"]),
                        )
                    )
                else:
                    conn.execute(
                        update(self.runtime_state)
                        .where(self.runtime_state.c.id == 1)
                        .values(
                            snapshot_json=merged,
                            updated_at=float(merged["last_update"]),
                        )
                    )
                if decision_snapshot is not None:
                    decisions, vol, diagnostics = decision_snapshot
                    conn.execute(
                        self.decision_snapshots.insert().values(
                            ts=float(merged["last_update"]),
                            vol=vol,
                            decisions_json=decisions,
                            diagnostics_json=diagnostics,
                        )
                    )

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
        """Atomically admit exactly one EA consumer for the terminal scope."""

        identity = str(consumer_identity or "").strip()
        instance_id = str(producer_instance_id or "").strip()
        scope = str(terminal_lease_scope or "").strip()
        generation = str(credential_generation_id or "").strip()
        protocol_version = str(bridge_protocol_version or "").strip()
        channel_name = str(channel or "").strip().lower()
        if (
            not identity
            or not instance_id
            or not scope
            or not generation
            or not protocol_version
        ):
            return {"ok": False, "reason": "bridge_consumer_identity_incomplete"}
        if channel_name not in {"poll", "ack", "heartbeat", "tick", "bars", "report"}:
            return {"ok": False, "reason": "bridge_consumer_channel_invalid"}
        ttl = min(120.0, max(5.0, float(lease_secs)))
        now_ts = _now()
        with self._lock:
            with self.engine.begin() as conn:
                row = conn.execute(
                    select(self.runtime_state.c.snapshot_json)
                    .where(self.runtime_state.c.id == 1)
                    .with_for_update()
                ).first()
                merged = dict(row[0] if row and isinstance(row[0], dict) else {})
                current = dict(merged.get("bridge_consumer_lease") or {})
                current_fresh = float(current.get("expires_at") or 0.0) > now_ts
                same_identity = bool(
                    str(current.get("consumer_identity") or "") == identity
                    and str(current.get("producer_instance_id") or "") == instance_id
                    and str(current.get("terminal_lease_scope") or "") == scope
                    and str(current.get("credential_generation_id") or "") == generation
                    and str(current.get("bridge_protocol_version") or "")
                    == protocol_version
                )
                if current_fresh and not same_identity:
                    return {
                        "ok": False,
                        "reason": "bridge_consumer_lease_busy",
                        "expires_at": float(current.get("expires_at") or 0.0),
                    }
                lease = {
                    **current,
                    "schema_version": "fxstack_bridge_consumer_lease_v2",
                    "consumer_identity": identity,
                    "producer_instance_id": instance_id,
                    "terminal_lease_scope": scope,
                    "credential_generation_id": generation,
                    "bridge_protocol_version": protocol_version,
                    "acquired_at": float(
                        (current.get("acquired_at") or now_ts)
                        if same_identity
                        else now_ts
                    ),
                    "renewed_at": now_ts,
                    "expires_at": now_ts + ttl,
                    f"{channel_name}_authenticated_at": now_ts,
                }
                merged["bridge_consumer_lease"] = lease
                merged["last_update"] = now_ts
                if row is None:
                    conn.execute(
                        self.runtime_state.insert().values(
                            id=1,
                            snapshot_json=merged,
                            updated_at=now_ts,
                        )
                    )
                else:
                    conn.execute(
                        update(self.runtime_state)
                        .where(self.runtime_state.c.id == 1)
                        .values(snapshot_json=merged, updated_at=now_ts)
                    )
                return {"ok": True, "lease": lease}

    def compare_and_set_release_authority(
        self,
        *,
        next_authority: dict[str, Any],
        expected_generation_id: str = "",
        expected_status: str = "",
        safety_dominant: bool = False,
    ) -> dict[str, Any]:
        """Atomically replace release authority and its broker-egress lease.

        Witness/evidence verification is deliberately performed while the
        durable state row and execution queue are locked. A structurally
        plausible payload supplied by another in-process caller is not an
        authorization capability.
        """

        incoming = dict(next_authority or {})
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                row = conn.execute(
                    select(self.runtime_state.c.snapshot_json)
                    .where(self.runtime_state.c.id == 1)
                    .with_for_update()
                ).first()
                merged = dict(row[0] if row and isinstance(row[0], dict) else {})
                current = dict(merged.get("release_authority") or {})
                current_request = dict(current.get("request") or {})
                current_generation = str(current_request.get("generation_id") or "")
                current_status = str(current.get("status") or "").strip().lower()
                incoming_status = str(incoming.get("status") or "").strip().lower()
                incoming_request = dict(incoming.get("request") or {})
                incoming_generation = str(incoming_request.get("generation_id") or "")
                if safety_dominant:
                    # Safety dominance is monotonic. It can revoke authority
                    # despite a stale expected generation, but it can never
                    # publish, acknowledge, or activate one.
                    if str(
                        incoming.get("schema_version") or ""
                    ) != _RELEASE_AUTHORITY_STATE_SCHEMA or incoming_status not in {
                        "revoked",
                        "rejected",
                    }:
                        return {
                            "updated": False,
                            "reason": "release_safety_transition_must_revoke",
                            "authority": current,
                        }
                else:
                    if expected_generation_id and current_generation != str(
                        expected_generation_id
                    ):
                        return {
                            "updated": False,
                            "reason": "release_generation_changed",
                            "authority": current,
                        }
                    if (
                        expected_status
                        and current_status != str(expected_status).strip().lower()
                    ):
                        return {
                            "updated": False,
                            "reason": "release_status_changed",
                            "authority": current,
                        }
                    incoming_pair = (
                        str(incoming_request.get("pair") or "").strip().upper()
                    )
                    current_pair = (
                        str(current_request.get("pair") or "").strip().upper()
                    )
                    if (
                        current_status in {"pending", "acknowledged", "active"}
                        and current_generation
                        and current_generation != incoming_generation
                    ):
                        return {
                            "updated": False,
                            "reason": "release_authority_singleton_busy",
                            "authority": current,
                        }
                    if current_pair and incoming_pair and current_pair != incoming_pair:
                        return {
                            "updated": False,
                            "reason": "release_authority_scope_conflict",
                            "authority": current,
                        }
                    if incoming_status == "active" and current_status != "acknowledged":
                        return {
                            "updated": False,
                            "reason": "release_activation_requires_acknowledged",
                            "authority": current,
                        }
                    if (
                        incoming_status == "acknowledged"
                        and current_status != "pending"
                    ):
                        return {
                            "updated": False,
                            "reason": "release_ack_requires_pending",
                            "authority": current,
                        }
                    if incoming_status in {"acknowledged", "active"} and (
                        incoming_generation != current_generation
                        or str(incoming_request.get("request_sha256") or "")
                        != str(current_request.get("request_sha256") or "")
                    ):
                        return {
                            "updated": False,
                            "reason": "release_transition_request_changed",
                            "authority": current,
                        }
                    if incoming_status == "pending":
                        witness = dict(incoming_request.get("external_witness") or {})
                        nonce = str(witness.get("nonce") or "").strip()
                        expires_at = _parse_iso_ts(witness.get("expires_at"))
                        if not nonce or expires_at <= _now():
                            return {
                                "updated": False,
                                "reason": "release_witness_invalid",
                                "authority": current,
                            }
                        ledger = {
                            str(key): float(value)
                            for key, value in dict(
                                merged.get("release_witness_nonce_ledger") or {}
                            ).items()
                            if str(key).strip() and _parse_iso_ts(value) > _now()
                        }
                        if nonce in ledger:
                            return {
                                "updated": False,
                                "reason": "release_witness_replayed",
                                "authority": current,
                            }
                        ledger[nonce] = float(expires_at)
                        merged["release_witness_nonce_ledger"] = ledger

                    if incoming_status not in {
                        "pending",
                        "acknowledged",
                        "active",
                        "revoked",
                        "rejected",
                    }:
                        return {
                            "updated": False,
                            "reason": "release_authority_status_invalid",
                            "authority": current,
                        }

                    if incoming_status in {"pending", "acknowledged", "active"}:
                        # Local import avoids a store/module import cycle while
                        # keeping cryptographic and immutable-evidence checks
                        # inseparable from this state/queue transaction.
                        from fxstack.runtime.release_authority import (
                            active_authority_errors,
                            authority_request_errors,
                        )

                        pair = str(incoming_request.get("pair") or "").strip().upper()
                        active_db_row = (
                            conn.execute(
                                select(self.active_model_sets).where(
                                    self.active_model_sets.c.pair == pair
                                )
                            )
                            .mappings()
                            .first()
                        )
                        if incoming_status == "active":
                            runtime_attestation = dict(
                                merged.get("runtime_attestation") or {}
                            )
                            pair_attestation = dict(
                                dict(runtime_attestation.get("pairs") or {}).get(pair)
                                or {}
                            )
                            if pair_attestation:
                                runtime_attestation = {
                                    **runtime_attestation,
                                    **pair_attestation,
                                }
                            runtime_boot_id = str(
                                runtime_attestation.get("runtime_boot_id")
                                or merged.get("runtime_boot_id")
                                or ""
                            )
                            validation_errors = active_authority_errors(
                                incoming,
                                active_db_row=(
                                    dict(active_db_row)
                                    if active_db_row is not None
                                    else None
                                ),
                                runtime_boot_id=runtime_boot_id,
                                runtime_attestation=runtime_attestation,
                                expected_generation_id=incoming_generation,
                                expected_request_sha256=str(
                                    incoming_request.get("request_sha256") or ""
                                ),
                                validate_evidence=True,
                            )
                        else:
                            validation_errors = authority_request_errors(
                                incoming_request,
                                active_db_row=(
                                    dict(active_db_row)
                                    if active_db_row is not None
                                    else None
                                ),
                                validate_evidence=True,
                            )
                            if incoming_status == "acknowledged":
                                binding_probe = {**incoming, "status": "active"}
                                binding_error = self._release_egress_binding_error(
                                    binding_probe
                                )
                                if binding_error:
                                    validation_errors.append(binding_error)
                        if validation_errors:
                            return {
                                "updated": False,
                                "reason": "release_authority_validation_failed",
                                "errors": list(dict.fromkeys(validation_errors)),
                                "authority": current,
                            }

                merged["release_authority"] = incoming
                now_ts = _now()
                if incoming_status == "active":
                    binding_error = self._release_egress_binding_error(incoming)
                    if binding_error:
                        return {
                            "updated": False,
                            "reason": binding_error,
                            "authority": current,
                        }
                    ack = dict(incoming.get("ack") or {})
                    merged["execution_egress_enabled"] = True
                    merged["execution_egress_authority"] = {
                        "schema_version": _EXECUTION_EGRESS_SCHEMA,
                        "enabled": True,
                        "reason": "externally_witnessed_runner_ack",
                        "generation_id": incoming_generation,
                        "request_sha256": str(
                            incoming_request.get("request_sha256") or ""
                        ),
                        "runtime_boot_id": str(ack.get("runtime_boot_id") or ""),
                        "updated_at": now_ts,
                    }
                else:
                    self._disable_execution_egress_in_state(
                        merged,
                        reason=f"release_authority_{incoming_status or 'invalid'}",
                        now_ts=now_ts,
                    )
                    self._quarantine_execution_queue(
                        conn,
                        reason=f"execution_egress_{incoming_status or 'disabled'}",
                        now_ts=now_ts,
                    )
                merged["last_update"] = now_ts
                if row is None:
                    conn.execute(
                        self.runtime_state.insert().values(
                            id=1,
                            snapshot_json=merged,
                            updated_at=float(merged["last_update"]),
                        )
                    )
                else:
                    conn.execute(
                        update(self.runtime_state)
                        .where(self.runtime_state.c.id == 1)
                        .values(
                            snapshot_json=merged,
                            updated_at=float(merged["last_update"]),
                        )
                    )
        return {
            "updated": True,
            "reason": "updated",
            "authority": incoming,
            "execution_egress_enabled": incoming_status == "active",
        }

    def patch_orchestration_live_state(
        self,
        *,
        updates: dict[str, Any],
        expected_live_authority: dict[str, Any] | None,
        safety_dominant: bool = False,
        allow_reenable: bool = False,
    ) -> dict[str, Any]:
        """Atomically mutate operator/release-owned live authority.

        Enabling/ramp mutations use optimistic authority matching. Safety
        mutations are deliberately dominant, but must leave entry authority
        disabled so an older enable/ramp operation can never resurrect it.
        """

        authority_updates = {
            str(key): value for key, value in dict(updates or {}).items()
        }
        if not safety_dominant and not isinstance(expected_live_authority, dict):
            raise ValueError("expected_live_authority_required")
        if safety_dominant:
            next_runtime_enabled = bool(authority_updates.get("runtime_enabled", True))
            next_queue_kill = bool(authority_updates.get("queue_kill_active", False))
            if next_runtime_enabled and not next_queue_kill:
                raise ValueError("safety_dominant_mutation_must_disable_entries")

        with self._lock:
            with self.engine.begin() as conn:
                row = conn.execute(
                    select(self.runtime_state.c.snapshot_json)
                    .where(self.runtime_state.c.id == 1)
                    .with_for_update()
                ).first()
                merged = dict(row[0] if row and isinstance(row[0], dict) else {})
                runtime_diag = dict(merged.get("runtime_diag") or {})
                current_live = dict(runtime_diag.get("orchestration_live") or {})
                if not safety_dominant:
                    current_authority = _selected_state_fields(
                        current_live,
                        _ORCHESTRATION_LIVE_AUTHORITY_FIELDS,
                    )
                    expected_authority = _selected_state_fields(
                        dict(expected_live_authority or {}),
                        _ORCHESTRATION_LIVE_AUTHORITY_FIELDS,
                    )
                    if current_authority != expected_authority:
                        raise RuntimeError("orchestration_live_authority_conflict")
                    requests_enabled = bool(
                        authority_updates.get(
                            "runtime_enabled",
                            current_live.get("runtime_enabled", False),
                        )
                    ) and not bool(
                        authority_updates.get(
                            "queue_kill_active",
                            current_live.get("queue_kill_active", False),
                        )
                    )
                    currently_disabled = not bool(
                        current_live.get("runtime_enabled", False)
                    ) or bool(current_live.get("queue_kill_active", False))
                    if requests_enabled and currently_disabled and not allow_reenable:
                        raise RuntimeError("orchestration_live_reenable_requires_start")
                current_revision = max(
                    0,
                    _safe_int(current_live.get("authority_revision"), 0),
                )
                current_live.update(authority_updates)
                current_live["authority_revision"] = int(current_revision + 1)
                runtime_diag["orchestration_live"] = current_live
                merged["runtime_diag"] = runtime_diag
                merged["last_update"] = _now()
                if row is None:
                    conn.execute(
                        self.runtime_state.insert().values(
                            id=1,
                            snapshot_json=merged,
                            updated_at=float(merged["last_update"]),
                        )
                    )
                else:
                    conn.execute(
                        update(self.runtime_state)
                        .where(self.runtime_state.c.id == 1)
                        .values(
                            snapshot_json=merged,
                            updated_at=float(merged["last_update"]),
                        )
                    )
                return dict(current_live)

    def get_state(self) -> dict[str, Any]:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.runtime_state.c.snapshot_json).where(
                    self.runtime_state.c.id == 1
                )
            ).first()
            return dict(row[0] if row else {})

    def get_reports(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.engine.begin() as conn:
            rows = (
                conn.execute(
                    select(self.reports)
                    .order_by(self.reports.c.id.desc())
                    .limit(max(1, min(limit, 5000)))
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def get_decision_snapshots(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.engine.begin() as conn:
            rows = (
                conn.execute(
                    select(self.decision_snapshots)
                    .order_by(self.decision_snapshots.c.id.desc())
                    .limit(max(1, min(limit, 5000)))
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def _normalize_experiment_proposal_row(self, row: Any) -> dict[str, Any]:
        data = dict(row or {})
        return {
            "experiment_id": str(data.get("experiment_id") or ""),
            "source_run_id": str(data.get("source_run_id") or "") or None,
            "hypothesis": str(data.get("hypothesis") or ""),
            "change_set": list(data.get("change_set_json") or []),
            "evaluation_plan": dict(data.get("evaluation_plan_json") or {}),
            "risk_notes": list(data.get("risk_notes_json") or []),
            "evidence_refs": list(data.get("evidence_refs_json") or []),
            "prompt_hash": str(data.get("prompt_hash") or ""),
            "tool_trace_hash": str(data.get("tool_trace_hash") or ""),
            "model_id": str(data.get("model_id") or ""),
            "decision_seed": int(data.get("decision_seed") or 0),
            "input_artefact_refs": list(data.get("input_artefact_refs_json") or []),
            "config_diff": dict(data.get("config_diff_json") or {}),
            "replay_window": str(data.get("replay_window") or ""),
            "artifact_root": str(data.get("artifact_root") or ""),
            "latest_stage": str(data.get("latest_stage") or ""),
            "latest_promotion_id": str(data.get("latest_promotion_id") or ""),
            "approval_status": str(data.get("approval_status") or ""),
            "created_at": float(data.get("created_at") or 0.0),
        }

    def _normalize_experiment_promotion_row(self, row: Any) -> dict[str, Any]:
        data = dict(row or {})
        return {
            "promotion_id": str(data.get("promotion_id") or ""),
            "experiment_id": str(data.get("experiment_id") or ""),
            "prompt_hash": str(data.get("prompt_hash") or ""),
            "tool_trace_hash": str(data.get("tool_trace_hash") or ""),
            "model_id": str(data.get("model_id") or ""),
            "config_diff": dict(data.get("config_diff_json") or {}),
            "replay_window": str(data.get("replay_window") or ""),
            "replay_results": dict(data.get("replay_results_json") or {}),
            "approval_records": list(data.get("approval_records_json") or []),
            "paper_results": dict(data.get("paper_results_json") or {}),
            "canary_results": dict(data.get("canary_results_json") or {}),
            "release_manifest_ref": str(data.get("release_manifest_ref") or ""),
            "rollback_metadata": dict(data.get("rollback_metadata_json") or {}),
            "artefact_hashes": {
                str(key): str(value)
                for key, value in dict(data.get("artefact_hashes_json") or {}).items()
            },
            "status": str(data.get("status") or ""),
            "created_at": float(data.get("created_at") or 0.0),
            "updated_at": float(data.get("updated_at") or 0.0),
        }

    def _normalize_experiment_lineage_row(self, row: Any) -> dict[str, Any]:
        data = dict(row or {})
        return {
            "experiment_id": str(data.get("experiment_id") or ""),
            "proposal_ref": str(data.get("proposal_ref") or ""),
            "review_ref": str(data.get("review_ref") or ""),
            "replay_refs": list(data.get("replay_refs_json") or []),
            "paper_pack_ref": str(data.get("paper_pack_ref") or ""),
            "canary_pack_ref": str(data.get("canary_pack_ref") or ""),
            "promotion_decision_ref": str(data.get("promotion_decision_ref") or ""),
            "rollback_plan_ref": str(data.get("rollback_plan_ref") or ""),
            "release_manifest_ref": str(data.get("release_manifest_ref") or ""),
            "reflection_memory_ref": str(data.get("reflection_memory_ref") or ""),
            "latest_stage": str(data.get("latest_stage") or ""),
            "latest_promotion_id": str(data.get("latest_promotion_id") or ""),
            "approval_status": str(data.get("approval_status") or ""),
            "evidence_refs": list(data.get("evidence_refs_json") or []),
            "promotion_ids": list(data.get("promotion_ids_json") or []),
            "approval_event_ids": list(data.get("approval_event_ids_json") or []),
            "updated_at": float(data.get("updated_at") or 0.0),
        }

    def get_orchestration_runs(
        self,
        *,
        limit: int = 200,
        pair: str = "",
        runtime_mode: str = "",
        cycle_id: str = "",
    ) -> list[dict[str, Any]]:
        stmt = select(self.orchestration_runs)
        if str(pair).strip():
            stmt = stmt.where(self.orchestration_runs.c.pair == str(pair).upper())
        if str(runtime_mode).strip():
            stmt = stmt.where(
                self.orchestration_runs.c.runtime_mode == str(runtime_mode)
            )
        if str(cycle_id).strip():
            stmt = stmt.where(self.orchestration_runs.c.cycle_id == str(cycle_id))
        stmt = stmt.order_by(self.orchestration_runs.c.created_at.desc()).limit(
            max(1, min(limit, 5000))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_orchestration_traces(
        self,
        *,
        limit: int = 200,
        run_id: str = "",
        pair: str = "",
    ) -> list[dict[str, Any]]:
        stmt = select(self.agent_traces)
        if str(run_id).strip():
            stmt = stmt.where(self.agent_traces.c.run_id == str(run_id))
        if str(pair).strip():
            stmt = stmt.where(self.agent_traces.c.pair == str(pair).upper())
        stmt = stmt.order_by(self.agent_traces.c.created_at.desc()).limit(
            max(1, min(limit, 5000))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_closed_trade_reports(self, limit: int = 200) -> list[dict[str, Any]]:
        stmt = (
            select(self.reports)
            .where(
                or_(
                    self.reports.c.report_text.like('%"report_type":"closed_trade"%'),
                    self.reports.c.report_text.like('%"report_type": "closed_trade"%'),
                )
            )
            .order_by(self.reports.c.id.desc())
            .limit(max(1, min(limit, 5000)))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_commands(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.engine.begin() as conn:
            rows = (
                conn.execute(
                    select(self.commands)
                    .order_by(self.commands.c.created_at.desc())
                    .limit(max(1, min(limit, 5000)))
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def get_scalp_reconciliation_commands(
        self,
        *,
        include_historical: bool,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Read only command rows that can affect scalp reconciliation."""
        known_statuses = (
            "queued",
            "delivered",
            "acked",
            "failed",
            "expired",
            "duplicate",
            "reconcile_required",
        )
        conditions = [
            self.commands.c.status.in_(("queued", "delivered", "reconcile_required")),
            self.commands.c.status.not_in(known_statuses),
        ]
        if include_historical:
            # Historical rows can join an open broker position only through an
            # entry or ticket-management verb. Include every status for those
            # verbs so contradictory terminal evidence remains fail-closed.
            conditions.append(
                self.commands.c.cmd.in_(
                    ("BUY", "SELL", "CLOSE", "CLOSE_PARTIAL", "MODIFY_SL")
                )
            )
        stmt = (
            select(
                self.commands.c.command_id,
                self.commands.c.cmd,
                self.commands.c.symbol,
                self.commands.c.magic,
                self.commands.c.intent,
                self.commands.c.status,
                self.commands.c.payload_json,
                self.commands.c.ack_json,
            )
            .where(or_(*conditions))
            .order_by(self.commands.c.created_at.desc())
            .limit(max(1, min(limit, 5000)))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(row) for row in rows]

    def get_command_window_summary(
        self, *, start_ts: float, end_ts: float
    ) -> dict[str, Any]:
        """Aggregate the complete durable command queue for an exact time window.

        Unlike ``get_commands``, this proof endpoint is intentionally uncapped.
        The query returns bounded cardinality (command/status groups), while the
        database counts every matching row in one transaction snapshot.
        """

        start = float(start_ts)
        end = float(end_ts)
        query_started_at = _now()
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start <= 0.0
            or end < start
            or end > query_started_at + 5.0
        ):
            raise ValueError("invalid_command_window")
        window = and_(
            self.commands.c.created_at >= start,
            self.commands.c.created_at <= end,
        )
        normalized_cmd = func.upper(self.commands.c.cmd).label("normalized_cmd")
        grouped_stmt = (
            select(
                normalized_cmd,
                self.commands.c.status,
                func.count().label("row_count"),
            )
            .where(window)
            .group_by(normalized_cmd, self.commands.c.status)
        )
        bounds_stmt = select(
            func.count().label("total_commands"),
            func.min(self.commands.c.created_at).label("first_created_at"),
            func.max(self.commands.c.created_at).label("last_created_at"),
        ).where(window)
        with self.engine.begin() as conn:
            groups = conn.execute(grouped_stmt).mappings().all()
            bounds = dict(conn.execute(bounds_stmt).mappings().one())

        status_counts: dict[str, int] = {}
        entry_status_counts: dict[str, int] = {}
        control_status_counts: dict[str, int] = {}
        command_counts: dict[str, int] = {}
        for raw in groups:
            command = str(raw.get("normalized_cmd") or "").strip().upper()
            status = str(raw.get("status") or "").strip().lower()
            count = int(raw.get("row_count") or 0)
            command_counts[command] = command_counts.get(command, 0) + count
            status_counts[status] = status_counts.get(status, 0) + count
            target = (
                entry_status_counts
                if command in {"BUY", "SELL"}
                else control_status_counts
            )
            target[status] = target.get(status, 0) + count
        entry_commands = sum(
            command_counts.get(command, 0) for command in ("BUY", "SELL")
        )
        total_commands = int(bounds.get("total_commands") or 0)
        return {
            "schema_version": "fxstack_command_window_summary_v1",
            "start_ts": start,
            "end_ts": end,
            "queried_at": _now(),
            "window_complete": True,
            "total_commands": total_commands,
            "entry_commands": int(entry_commands),
            "control_commands": int(total_commands - entry_commands),
            "first_created_at": (
                float(bounds["first_created_at"])
                if bounds.get("first_created_at") is not None
                else None
            ),
            "last_created_at": (
                float(bounds["last_created_at"])
                if bounds.get("last_created_at") is not None
                else None
            ),
            "status_counts": dict(sorted(status_counts.items())),
            "entry_status_counts": dict(sorted(entry_status_counts.items())),
            "control_status_counts": dict(sorted(control_status_counts.items())),
            "command_counts": dict(sorted(command_counts.items())),
        }

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(self.commands).where(
                        self.commands.c.command_id == command_id
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def get_command_events(
        self, *, command_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        stmt = select(self.command_events)
        if command_id:
            stmt = stmt.where(self.command_events.c.command_id == command_id)
        stmt = stmt.order_by(self.command_events.c.id.desc()).limit(
            max(1, min(limit, 5000))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_governance_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.engine.begin() as conn:
            rows = (
                conn.execute(
                    select(self.governance_events)
                    .order_by(self.governance_events.c.id.desc())
                    .limit(max(1, min(limit, 5000)))
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def _get_state_and_metrics(
        self,
        conn: Any,
        *,
        include_latest_decision_diagnostics: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        metrics_now = _now()
        decision_window_start = metrics_now - 300.0
        parity_summary = (
            select(
                func.count().label("total"),
                func.coalesce(
                    func.sum(
                        case(
                            (self.feature_parity_audit.c.parity_ok == 0, 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("breaches"),
            )
            .select_from(self.feature_parity_audit)
            .subquery()
        )
        summary_columns = [
            select(func.count())
            .select_from(self.decision_snapshots)
            .where(
                self.decision_snapshots.c.ts.between(
                    decision_window_start,
                    metrics_now,
                )
            )
            .scalar_subquery()
            .label("snapshots"),
            select(func.count())
            .select_from(self.command_events)
            .scalar_subquery()
            .label("events"),
            select(func.count())
            .select_from(self.active_model_sets)
            .where(self.active_model_sets.c.enabled == 1)
            .scalar_subquery()
            .label("active_sets"),
            select(self.runtime_state.c.snapshot_json)
            .where(self.runtime_state.c.id == 1)
            .scalar_subquery()
            .label("state"),
            select(func.count())
            .select_from(self.feature_push_audit)
            .scalar_subquery()
            .label("push_audit"),
            parity_summary.c.total.label("parity_total"),
            parity_summary.c.breaches.label("parity_breaches"),
        ]
        if include_latest_decision_diagnostics:
            summary_columns.extend(
                [
                    select(self.decision_snapshots.c.ts)
                    .order_by(self.decision_snapshots.c.id.desc())
                    .limit(1)
                    .scalar_subquery()
                    .label("latest_decision_ts"),
                    select(self.decision_snapshots.c.diagnostics_json)
                    .order_by(self.decision_snapshots.c.id.desc())
                    .limit(1)
                    .scalar_subquery()
                    .label("latest_decision_diagnostics"),
                ]
            )
        summary_stmt = select(*summary_columns)
        by_status = conn.execute(
            select(self.commands.c.status, func.count()).group_by(
                self.commands.c.status
            )
        ).all()
        push_by_status = conn.execute(
            select(self.feature_push_outbox.c.status, func.count()).group_by(
                self.feature_push_outbox.c.status
            )
        ).all()
        summary = conn.execute(summary_stmt).mappings().one()
        command_counts = {str(key): int(value) for key, value in by_status}
        pending = sum(command_counts.get(status, 0) for status in ("queued", "delivered"))
        push_counts = {str(key): int(value) for key, value in push_by_status}
        push_backlog = sum(
            push_counts.get(status, 0) for status in ("queued", "retry", "claimed")
        )
        state = dict(summary["state"] if isinstance(summary["state"], dict) else {})
        runtime_diag = dict(state.get("runtime_diag") or {})
        rollout_summary = dict(
            runtime_diag.get("rollout_summary")
            or dict(runtime_diag.get("risk_cycle_summary") or {}).get("rollout")
            or {}
        )
        rollout_policy = dict(
            runtime_diag.get("rollout_policy")
            or runtime_diag.get("canary_rollout_policy")
            or {}
        )
        provider_health = dict(runtime_diag.get("provider_health") or {})
        provider_roles = dict(runtime_diag.get("provider_roles") or {})
        portfolio_intelligence = dict(runtime_diag.get("portfolio_intelligence") or {})
        capital_governance = dict(runtime_diag.get("capital_governance") or {})
        metrics = {
            "commands": command_counts,
            "pending": {"count": int(pending)},
            "decision_pipeline": {
                "snapshots_5m": int(summary["snapshots"]),
                "stage_attribution": {"pipeline_rows": []},
            },
            "command_events": {"count": int(summary["events"])},
            "models": {"active_sets": int(summary["active_sets"])},
            "feature_push": {
                "outbox": push_counts,
                "backlog": int(push_backlog),
                "audit_rows": int(summary["push_audit"]),
            },
            "feature_parity": {
                "total": int(summary["parity_total"]),
                "breaches": int(summary["parity_breaches"]),
            },
            "rollout": {
                **rollout_summary,
                "policy": rollout_policy,
            },
            "provider_health": dict(provider_health),
            "provider_roles": dict(provider_roles),
            "portfolio_intelligence": dict(portfolio_intelligence),
            "capital_governance": dict(capital_governance),
        }
        latest_diagnostics = {}
        if (
            include_latest_decision_diagnostics
            and summary["latest_decision_ts"] is not None
        ):
            latest_diagnostics = {
                "ts": float(summary["latest_decision_ts"]),
                "diagnostics_json": dict(
                    summary["latest_decision_diagnostics"] or {}
                ),
            }
        return state, metrics, latest_diagnostics

    def get_state_and_metrics(self) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.engine.begin() as conn:
            state, metrics, _ = self._get_state_and_metrics(conn)
        return state, metrics

    def get_state_metrics_and_latest_decision_diagnostics(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Read readiness state, metrics, and diagnostics in one transaction."""

        with self.engine.begin() as conn:
            return self._get_state_and_metrics(
                conn,
                include_latest_decision_diagnostics=True,
            )

    def get_state_and_governance_metrics(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Read the coherent state and metric subset used by capital governance."""
        # AGENT HOT PATH: the one-second scalp cycle needs only state plus the
        # feature-parity gate. Keep this as one database snapshot and do not
        # compute dashboard-wide command/model/outbox aggregates here.
        stmt = select(
            select(self.runtime_state.c.snapshot_json)
            .where(self.runtime_state.c.id == 1)
            .scalar_subquery()
            .label("state"),
            select(func.count())
            .select_from(self.feature_parity_audit)
            .where(self.feature_parity_audit.c.parity_ok == 0)
            .scalar_subquery()
            .label("parity_breaches"),
        )
        with self.engine.begin() as conn:
            summary = conn.execute(stmt).mappings().one()
        state = dict(summary["state"] if isinstance(summary["state"], dict) else {})
        return state, {
            "feature_parity": {
                "breaches": int(summary["parity_breaches"]),
            }
        }

    def get_metrics(self) -> dict[str, Any]:
        _, metrics = self.get_state_and_metrics()
        return metrics
