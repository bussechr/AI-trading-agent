"""Collect restart-resilient prospective IG-MT4 inputs with explicit gaps.

This version never inserts a finalized bar behind a symbol's durable watermark.
If such a bar appears after a bridge or terminal restart, the collector records
one immutable, hash-chained late-gap event and permanently excludes that epoch
from the bar stream.  Covered matching or revised rows remain ignored in favour
of the first hash-chained observation.

The module is collection-only and GET-only.  It has no signal, outcome,
performance, signing, activation, registry, broker-order, or runtime surface.
"""

from __future__ import annotations

# AGENT: ROLE: versioned restart-safe MTVCLC input collector with auditable permanent gaps.
# AGENT: HANDSHAKE: sealed v2 contract -> authenticated GETs -> portable chunks plus immutable late-gap ledger.
# AGENT: ISOLATION: collection only; no signal, outcome, performance, issuer, activation, or order authority.

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

SUPPORT_PATH = (
    REPOSITORY_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient.py"
).resolve()
PINNED_SUPPORT_SHA256 = (
    "b38e69933e4910956dc92477252d33f4d5a92458e70036520654bc53102357f5"
)
PINNED_SUPPORT_SIZE_BYTES = 77_496
try:
    _support_stat_before = SUPPORT_PATH.stat()
    _support_bytes_before = SUPPORT_PATH.read_bytes()
except OSError as exc:  # pragma: no cover - import-time fail-closed boundary
    raise RuntimeError("collector_support_source_unreadable_at_module_start") from exc
if (
    len(_support_bytes_before) != PINNED_SUPPORT_SIZE_BYTES
    or hashlib.sha256(_support_bytes_before).hexdigest() != PINNED_SUPPORT_SHA256
):  # pragma: no cover - exact sealed support boundary
    raise RuntimeError("collector_support_source_identity_mismatch")

_support_module_name = (
    "_fxstack_mtvclc_resilient_v2_support_"
    + hashlib.sha256(_support_bytes_before).hexdigest()[:16]
)
_support_spec = importlib.util.spec_from_file_location(
    _support_module_name,
    SUPPORT_PATH,
)
if _support_spec is None or _support_spec.loader is None:  # pragma: no cover
    raise RuntimeError("collector_support_source_import_invalid")
support = importlib.util.module_from_spec(_support_spec)
sys.modules[_support_module_name] = support
try:
    _support_spec.loader.exec_module(support)
except Exception:
    sys.modules.pop(_support_module_name, None)
    raise

try:
    _support_stat_after = SUPPORT_PATH.stat()
    _support_bytes_after = SUPPORT_PATH.read_bytes()
except OSError as exc:  # pragma: no cover - import-time fail-closed boundary
    raise RuntimeError("collector_support_source_unreadable_at_module_start") from exc
if (
    _support_stat_before.st_size != _support_stat_after.st_size
    or _support_stat_before.st_mtime_ns != _support_stat_after.st_mtime_ns
    or _support_bytes_before != _support_bytes_after
):  # pragma: no cover - import-time race boundary
    raise RuntimeError("collector_support_source_changed_during_module_start")


TOOL_PATH = Path(__file__).resolve()
SUPPORT_SHA256 = hashlib.sha256(_support_bytes_after).hexdigest()
SUPPORT_SIZE_BYTES = len(_support_bytes_after)
SUPPORT_STAT_IDENTITY = (
    int(_support_stat_after.st_dev),
    int(_support_stat_after.st_ino),
    int(_support_stat_after.st_size),
    int(_support_stat_after.st_mtime_ns),
)
del _support_bytes_before
del _support_bytes_after
del _support_stat_before
del _support_stat_after

try:
    _source_stat_before = TOOL_PATH.stat()
    _source_bytes = TOOL_PATH.read_bytes()
    _source_stat_after = TOOL_PATH.stat()
except OSError as exc:  # pragma: no cover - import-time fail-closed boundary
    raise RuntimeError("collector_source_unreadable_at_module_start") from exc
if (
    _source_stat_before.st_size != _source_stat_after.st_size
    or _source_stat_before.st_mtime_ns != _source_stat_after.st_mtime_ns
):  # pragma: no cover - import-time race boundary
    raise RuntimeError("collector_source_changed_during_module_start")
MODULE_SOURCE_SHA256 = hashlib.sha256(_source_bytes).hexdigest()
MODULE_SOURCE_SIZE_BYTES = len(_source_bytes)
MODULE_SOURCE_STAT_IDENTITY = (
    int(_source_stat_after.st_dev),
    int(_source_stat_after.st_ino),
    int(_source_stat_after.st_size),
    int(_source_stat_after.st_mtime_ns),
)
del _source_bytes
del _source_stat_before
del _source_stat_after


COLLECTOR_SCHEMA_VERSION = "fxstack.external_ig_mt4_m1_activity_resilient_collector.v3"
CAPTURE_INTEGRITY_SCHEMA_VERSION = "fxstack.scalp.mtvclc_capture_integrity_contract.v3"
CAPTURE_INTEGRITY_POLICY_ID = (
    "first_authenticated_finalized_observation_or_declared_absence_wins.v3"
)
LATE_GAP_LEDGER_SCHEMA_VERSION = "fxstack.external_ig_mt4_m1_late_gap_ledger.v1"
LATE_GAP_LEDGER_FILENAME = "late-unseen-bar-gaps.sha256.jsonl"
MAXIMUM_LATE_GAP_LINE_BYTES = 4 * 1024 * 1024
MAXIMUM_START_EDGE_LAG_SECONDS = 30.0
ZERO_SHA256 = "0" * 64

CHUNK_SCHEMA_VERSION = support.CHUNK_SCHEMA_VERSION
MANIFEST_SCHEMA_VERSION = support.MANIFEST_SCHEMA_VERSION
PREREGISTRATION_SCHEMA_VERSION = support.PREREGISTRATION_SCHEMA_VERSION
SOURCE_CONTRACT_ID = support.SOURCE_CONTRACT_ID
ACTIVITY_METRIC_ID = support.ACTIVITY_METRIC_ID
SCOPE_VERSION = support.SCOPE_VERSION
VENUE_ID = support.VENUE_ID
TIMEFRAME = support.TIMEFRAME
SYMBOLS = support.SYMBOLS
SYMBOL_SET = frozenset(SYMBOLS)
MINIMUM_M1_BARS = support.MINIMUM_M1_BARS
DEFAULT_BAR_LIMIT = support.DEFAULT_BAR_LIMIT
DEFAULT_TICK_INTERVAL_SECS = support.DEFAULT_TICK_INTERVAL_SECS
DEFAULT_BAR_INTERVAL_SECS = support.DEFAULT_BAR_INTERVAL_SECS
DEFAULT_HTTP_TIMEOUT_SECS = support.DEFAULT_HTTP_TIMEOUT_SECS
MAXIMUM_TICK_INTERVAL_SECS = support.MAXIMUM_TICK_INTERVAL_SECS
MAXIMUM_PREREGISTRATION_BYTES = support.MAXIMUM_PREREGISTRATION_BYTES
PROSPECTIVE_WINDOW_DAYS = support.PROSPECTIVE_WINDOW_DAYS
MANIFEST_FILENAME = support.MANIFEST_FILENAME
ACTIVE_JOURNAL_FILENAME = support.ACTIVE_JOURNAL_FILENAME
DATA_WRITER_LOCK_FILENAME = support.DATA_WRITER_LOCK_FILENAME
COLLECTION_AUTHORITY = dict(support._FIXED_FALSE_AUTHORITY)

CollectionRefusal = support.CollectionRefusal
ProspectiveBinding = support.ProspectiveBinding
BridgeReadClient = support.BridgeReadClient
SourceIdentity = support.SourceIdentity
TickObservation = support.TickObservation
M1ActivityBar = support.M1ActivityBar
ExclusiveDataWriterLock = support.ExclusiveDataWriterLock
CollectionPolicy = support.CollectionPolicy
canonical_json_bytes = support.canonical_json_bytes
canonical_sha256 = support.canonical_sha256
source_from_state = support.source_from_state
validate_tick_snapshot = support.validate_tick_snapshot
validate_m1_bar_response = support.validate_m1_bar_response
read_api_key_file = support.read_api_key_file
_positive_float = support._positive_float
_strict_positive_int = support._strict_positive_int
_strict_json_object = support._strict_json_object
_is_sha256 = support._is_sha256
_atomic_write_new = support._atomic_write_new


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _assert_support_source_unchanged() -> None:
    try:
        before = SUPPORT_PATH.stat()
        raw = SUPPORT_PATH.read_bytes()
        after = SUPPORT_PATH.stat()
    except OSError as exc:
        raise CollectionRefusal("collector_support_source_path_drift") from exc
    if (
        _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != SUPPORT_STAT_IDENTITY
        or len(raw) != SUPPORT_SIZE_BYTES
        or hashlib.sha256(raw).hexdigest() != SUPPORT_SHA256
    ):
        raise CollectionRefusal("collector_support_source_path_drift")


def _assert_collector_source_unchanged() -> None:
    try:
        before = TOOL_PATH.stat()
        raw = TOOL_PATH.read_bytes()
        after = TOOL_PATH.stat()
    except OSError as exc:
        raise CollectionRefusal("collector_source_path_drift") from exc
    if (
        _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != MODULE_SOURCE_STAT_IDENTITY
        or len(raw) != MODULE_SOURCE_SIZE_BYTES
        or hashlib.sha256(raw).hexdigest() != MODULE_SOURCE_SHA256
    ):
        raise CollectionRefusal("collector_source_path_drift")
    _assert_support_source_unchanged()


def collector_source_sha256() -> str:
    _assert_collector_source_unchanged()
    return MODULE_SOURCE_SHA256


def expected_capture_integrity_contract() -> dict[str, Any]:
    return {
        "schema_version": CAPTURE_INTEGRITY_SCHEMA_VERSION,
        "contract_id": CAPTURE_INTEGRITY_POLICY_ID,
        "collector_source_sha256": collector_source_sha256(),
        "collector_support_source_sha256": SUPPORT_SHA256,
        "first_authenticated_finalized_observation_is_immutable": True,
        "covered_matching_later_overlap_is_ignored": True,
        "covered_revised_later_overlap_is_ignored": True,
        "covered_overlap_never_overwrites_or_duplicates_a_bar": True,
        "late_unseen_epoch_at_or_before_watermark_is_permanent_gap": True,
        "late_unseen_epoch_is_never_backfilled": True,
        "late_unseen_epoch_is_never_baseline_eligible": True,
        "late_gap_event_is_fsynced_hash_chained_and_immutable": True,
        "late_gap_ledger_filename": LATE_GAP_LEDGER_FILENAME,
        "late_gap_ledger_schema_version": LATE_GAP_LEDGER_SCHEMA_VERSION,
        "durable_per_symbol_watermark_never_regresses": True,
        "market_source_id_rollover_refuses": True,
        "downtime_gaps_are_preserved": True,
        "maximum_start_edge_lag_seconds": MAXIMUM_START_EDGE_LAG_SECONDS,
        "start_edge_miss_refuses_before_collection": True,
        "t0_reset_forbidden": True,
        "capture_restart_does_not_restart_or_extend_the_experiment": True,
        "tick_interval_seconds": DEFAULT_TICK_INTERVAL_SECS,
        "bar_interval_seconds": DEFAULT_BAR_INTERVAL_SECS,
        "bar_limit": DEFAULT_BAR_LIMIT,
        "http_timeout_seconds": DEFAULT_HTTP_TIMEOUT_SECS,
        "persistence_mode": (
            "fsynced_hash_chained_active_hour_journal_then_immutable_hour_chunk"
        ),
        "bootstrap_first_cycle_finalized_immediately": True,
        "active_hour_journal_filename": ACTIVE_JOURNAL_FILENAME,
        "portable_chunk_schema_version": CHUNK_SCHEMA_VERSION,
        "portable_manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "exclusive_output_data_writer_lock_required": True,
    }


# Rebind the reusable v1 ledger to this exact executable/support identity.  The
# imported module lives only inside this collector process; no source is edited.
support.COLLECTOR_SCHEMA_VERSION = COLLECTOR_SCHEMA_VERSION
support.PRESERVED_SUPPORT_PATH = SUPPORT_PATH
support.PRESERVED_SUPPORT_SHA256 = SUPPORT_SHA256
support.PRESERVED_SUPPORT_SIZE_BYTES = SUPPORT_SIZE_BYTES
support._assert_collector_source_unchanged = _assert_collector_source_unchanged
support._verified_support_sha256 = lambda: (
    _assert_support_source_unchanged() or SUPPORT_SHA256
)
support.collector_source_sha256 = collector_source_sha256
support.expected_capture_integrity_contract = expected_capture_integrity_contract
ManifestLedger = support.ManifestLedger


def _parse_utc_second(value: Any, reason: str) -> datetime:
    return support._parse_utc_second(value, reason)


def load_preregistration(path: str | Path) -> ProspectiveBinding:
    """Load only a declaration that exactly binds this v2 collector."""

    _assert_collector_source_unchanged()
    target = Path(path).expanduser().resolve(strict=False)
    if not target.is_file() or target.is_symlink():
        raise CollectionRefusal("preregistration_file_invalid")
    try:
        size = target.stat().st_size
        if size <= 0 or size > MAXIMUM_PREREGISTRATION_BYTES:
            raise CollectionRefusal("preregistration_file_size_invalid")
        raw = target.read_bytes()
    except OSError as exc:
        raise CollectionRefusal("preregistration_file_unreadable") from exc
    payload = _strict_json_object(raw, reason="preregistration_json_invalid")
    body = dict(payload)
    claimed = str(body.pop("preregistration_body_sha256", "")).lower()
    if not _is_sha256(claimed) or claimed != canonical_sha256(body):
        raise CollectionRefusal("preregistration_body_hash_invalid")
    if (
        body.get("schema_version") != PREREGISTRATION_SCHEMA_VERSION
        or body.get("research_only") is not True
        or body.get("authority") != COLLECTION_AUTHORITY
        or body.get("capture_integrity_contract")
        != expected_capture_integrity_contract()
    ):
        raise CollectionRefusal("preregistration_contract_invalid")

    scope = body.get("scope")
    strategy = body.get("strategy")
    execution = body.get("execution_contract")
    window = body.get("prospective_window")
    identities = body.get("source_identities")
    if not all(
        isinstance(value, Mapping)
        for value in (scope, strategy, execution, window, identities)
    ):
        raise CollectionRefusal("preregistration_contract_invalid")
    assert isinstance(scope, Mapping)
    assert isinstance(strategy, Mapping)
    assert isinstance(execution, Mapping)
    assert isinstance(window, Mapping)
    assert isinstance(identities, Mapping)

    observed_cells: list[tuple[str, str]] = []
    cells = scope.get("cell_order")
    if isinstance(cells, list):
        for row in cells:
            if not isinstance(row, Mapping):
                raise CollectionRefusal("preregistration_scope_invalid")
            observed_cells.append(
                (
                    str(row.get("symbol") or "").strip().upper(),
                    str(row.get("side") or "").strip().upper(),
                )
            )
    expected_cells = [(symbol, side) for symbol in SYMBOLS for side in ("BUY", "SELL")]
    collector_identity = identities.get("collector_source")
    support_identity = identities.get("collector_support_source")
    if (
        scope.get("ordered_symbols") != list(SYMBOLS)
        or scope.get("scope_version") != SCOPE_VERSION
        or scope.get("venue_id") != VENUE_ID
        or observed_cells != expected_cells
        or strategy.get("source_contract_id") != SOURCE_CONTRACT_ID
        or strategy.get("activity_metric_id") != ACTIVITY_METRIC_ID
        or execution.get("entry_type") != "immediate_market"
        or execution.get("pending_orders_forbidden") is not True
        or execution.get("maximum_entries_per_symbol_utc_day") != 1
        or not isinstance(collector_identity, Mapping)
        or collector_identity.get("filename") != TOOL_PATH.name
        or str(collector_identity.get("sha256") or "").lower() != MODULE_SOURCE_SHA256
        or collector_identity.get("size_bytes") != MODULE_SOURCE_SIZE_BYTES
        or not isinstance(support_identity, Mapping)
        or support_identity.get("filename") != SUPPORT_PATH.name
        or str(support_identity.get("sha256") or "").lower() != SUPPORT_SHA256
        or support_identity.get("size_bytes") != SUPPORT_SIZE_BYTES
        or window.get("consecutive_days") != PROSPECTIVE_WINDOW_DAYS
        or window.get("fixed_before_any_eligible_observation") is not True
        or window.get("observations_before_t0_forbidden") is not True
        or window.get("observations_at_or_after_end_forbidden") is not True
        or window.get("interim_signal_or_outcome_evaluation_forbidden") is not True
    ):
        raise CollectionRefusal("preregistration_contract_invalid")

    sealed_at = _parse_utc_second(
        body.get("sealed_at_utc"), "preregistration_time_invalid"
    )
    t0 = _parse_utc_second(
        window.get("t0_utc_inclusive"), "preregistration_time_invalid"
    )
    end = _parse_utc_second(
        window.get("end_utc_exclusive"), "preregistration_time_invalid"
    )
    if t0 <= sealed_at or end - t0 != timedelta(days=PROSPECTIVE_WINDOW_DAYS):
        raise CollectionRefusal("preregistration_time_invalid")
    return ProspectiveBinding(
        preregistration_body_sha256=claimed,
        preregistration_artifact_sha256=hashlib.sha256(raw).hexdigest(),
        t0_utc=t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_utc_exclusive=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        t0_epoch=t0.timestamp(),
        end_epoch_exclusive=end.timestamp(),
    )


_GAP_EVENT_FIELDS = frozenset(
    {
        "symbol",
        "minute_epoch",
        "watermark_epoch",
        "late_payload_sha256",
        "disposition",
        "baseline_eligible",
    }
)
_GAP_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "gap_sequence",
        "previous_gap_entry_sha256",
        "capture_integrity_contract_sha256",
        "collector_source_sha256",
        "collector_support_source_sha256",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "market_source_id",
        "observed_at_epoch",
        "events",
        "gap_entry_sha256",
    }
)


class LateGapLedger:
    """Append-only evidence for late epochs that must remain permanently absent."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        binding: ProspectiveBinding,
        writer_lock: ExclusiveDataWriterLock,
        last_bar_epoch_by_symbol: Mapping[str, int],
        bar_epoch_coverage_by_symbol: Mapping[str, Any],
    ) -> None:
        if not writer_lock.authorizes(output_root):
            raise CollectionRefusal("exclusive_data_writer_lock_required")
        self.root = Path(output_root).expanduser().resolve(strict=False)
        self.path = self.root / LATE_GAP_LEDGER_FILENAME
        if self.path.is_symlink():
            raise CollectionRefusal("late_gap_ledger_symlink_forbidden")
        self.binding = binding
        self.sequence = 0
        self.tail_hash = ZERO_SHA256
        self.source_id = ""
        self.file_object_identity: tuple[int, int] | None = None
        self.expected_file_size = 0
        self.declared: dict[str, set[int]] = {symbol: set() for symbol in SYMBOLS}
        self._load(
            last_bar_epoch_by_symbol,
            bar_epoch_coverage_by_symbol,
        )

    def contains(self, symbol: str, minute_epoch: int) -> bool:
        return minute_epoch in self.declared[symbol]

    def _binding_fields(self) -> dict[str, str]:
        return {
            "preregistration_body_sha256": (self.binding.preregistration_body_sha256),
            "preregistration_artifact_sha256": (
                self.binding.preregistration_artifact_sha256
            ),
            "prospective_t0_utc_inclusive": self.binding.t0_utc,
            "prospective_end_utc_exclusive": self.binding.end_utc_exclusive,
        }

    def _validate_event(
        self,
        value: Any,
        *,
        last_bar_epoch_by_symbol: Mapping[str, int],
        bar_epoch_coverage_by_symbol: Mapping[str, Any],
    ) -> tuple[str, int]:
        if not isinstance(value, Mapping) or set(value) != _GAP_EVENT_FIELDS:
            raise CollectionRefusal("late_gap_event_invalid")
        symbol = str(value.get("symbol") or "").strip().upper()
        epoch = _strict_positive_int(
            value.get("minute_epoch"), "late_gap_event_invalid"
        )
        watermark = _strict_positive_int(
            value.get("watermark_epoch"), "late_gap_event_invalid"
        )
        digest = str(value.get("late_payload_sha256") or "").lower()
        if (
            symbol not in SYMBOL_SET
            or epoch % 60 != 0
            or watermark % 60 != 0
            or epoch > watermark
            or watermark > int(last_bar_epoch_by_symbol.get(symbol, 0))
            or symbol not in bar_epoch_coverage_by_symbol
            or bar_epoch_coverage_by_symbol[symbol].contains(epoch)
            or not _is_sha256(digest)
            or value.get("disposition") != "permanent_gap_not_backfilled"
            or value.get("baseline_eligible") is not False
        ):
            raise CollectionRefusal("late_gap_event_invalid")
        return symbol, epoch

    def _load(
        self,
        last_bar_epoch_by_symbol: Mapping[str, int],
        bar_epoch_coverage_by_symbol: Mapping[str, Any],
    ) -> None:
        if set(bar_epoch_coverage_by_symbol) != SYMBOL_SET:
            raise CollectionRefusal("late_gap_coverage_scope_invalid")
        if not self.path.exists():
            return
        if not self.path.is_file() or self.path.is_symlink():
            raise CollectionRefusal("late_gap_ledger_invalid")
        try:
            before = self.path.stat()
            raw = self.path.read_bytes()
            after = self.path.stat()
        except OSError as exc:
            raise CollectionRefusal("late_gap_ledger_unreadable") from exc
        if _stat_identity(before) != _stat_identity(after) or len(raw) != after.st_size:
            raise CollectionRefusal("late_gap_ledger_changed_during_read")
        if not raw.endswith(b"\n"):
            raise CollectionRefusal("late_gap_ledger_partial_record_unverifiable")
        prior = ZERO_SHA256
        for expected_sequence, line in enumerate(
            raw.splitlines(keepends=True), start=1
        ):
            if (
                not line.endswith(b"\n")
                or line == b"\n"
                or len(line) > MAXIMUM_LATE_GAP_LINE_BYTES
            ):
                raise CollectionRefusal("late_gap_ledger_line_invalid")
            record = _strict_json_object(line, reason="late_gap_ledger_invalid")
            if set(record) != _GAP_RECORD_FIELDS:
                raise CollectionRefusal("late_gap_ledger_scope_invalid")
            if line != canonical_json_bytes(record) + b"\n":
                raise CollectionRefusal("late_gap_ledger_not_canonical")
            body = dict(record)
            claimed = str(body.pop("gap_entry_sha256", "")).lower()
            events = record.get("events")
            observed_at = _positive_float(
                record.get("observed_at_epoch"), "late_gap_record_invalid"
            )
            source_id = str(record.get("market_source_id") or "").lower()
            if (
                record.get("schema_version") != LATE_GAP_LEDGER_SCHEMA_VERSION
                or record.get("gap_sequence") != expected_sequence
                or record.get("previous_gap_entry_sha256") != prior
                or record.get("capture_integrity_contract_sha256")
                != canonical_sha256(expected_capture_integrity_contract())
                or record.get("collector_source_sha256") != collector_source_sha256()
                or record.get("collector_support_source_sha256") != SUPPORT_SHA256
                or any(
                    record.get(key) != value
                    for key, value in self._binding_fields().items()
                )
                or not _is_sha256(source_id)
                or (self.source_id and source_id != self.source_id)
                or observed_at < self.binding.t0_epoch
                or observed_at >= self.binding.end_epoch_exclusive
                or not isinstance(events, list)
                or not events
                or not _is_sha256(claimed)
                or canonical_sha256(body) != claimed
            ):
                raise CollectionRefusal("late_gap_record_invalid")
            for event in events:
                symbol, epoch = self._validate_event(
                    event,
                    last_bar_epoch_by_symbol=last_bar_epoch_by_symbol,
                    bar_epoch_coverage_by_symbol=bar_epoch_coverage_by_symbol,
                )
                if epoch in self.declared[symbol]:
                    raise CollectionRefusal("late_gap_event_duplicate")
                self.declared[symbol].add(epoch)
            self.sequence = expected_sequence
            self.tail_hash = claimed
            self.source_id = source_id
            prior = claimed
        self.file_object_identity = (int(after.st_dev), int(after.st_ino))
        self.expected_file_size = len(raw)

    def append(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        source_id: str,
        observed_at_epoch: float,
        last_bar_epoch_by_symbol: Mapping[str, int],
        bar_epoch_coverage_by_symbol: Mapping[str, Any],
    ) -> None:
        _assert_collector_source_unchanged()
        normalized: list[dict[str, Any]] = []
        for raw in events:
            event = dict(raw)
            symbol, epoch = self._validate_event(
                event,
                last_bar_epoch_by_symbol=last_bar_epoch_by_symbol,
                bar_epoch_coverage_by_symbol=bar_epoch_coverage_by_symbol,
            )
            if epoch in self.declared[symbol]:
                continue
            normalized.append(event)
        if not normalized:
            return
        normalized.sort(
            key=lambda row: (
                SYMBOLS.index(str(row["symbol"])),
                int(row["minute_epoch"]),
            )
        )
        source_id = str(source_id or "").lower()
        observed_at = _positive_float(observed_at_epoch, "late_gap_record_invalid")
        if (
            not _is_sha256(source_id)
            or (self.source_id and source_id != self.source_id)
            or observed_at < self.binding.t0_epoch
            or observed_at >= self.binding.end_epoch_exclusive
        ):
            raise CollectionRefusal("late_gap_record_invalid")
        body = {
            "schema_version": LATE_GAP_LEDGER_SCHEMA_VERSION,
            "gap_sequence": self.sequence + 1,
            "previous_gap_entry_sha256": self.tail_hash,
            "capture_integrity_contract_sha256": canonical_sha256(
                expected_capture_integrity_contract()
            ),
            "collector_source_sha256": collector_source_sha256(),
            "collector_support_source_sha256": SUPPORT_SHA256,
            **self._binding_fields(),
            "market_source_id": source_id,
            "observed_at_epoch": observed_at,
            "events": normalized,
        }
        record = {**body, "gap_entry_sha256": canonical_sha256(body)}
        line = canonical_json_bytes(record) + b"\n"
        if len(line) > MAXIMUM_LATE_GAP_LINE_BYTES:
            raise CollectionRefusal("late_gap_ledger_line_too_large")
        try:
            if self.sequence == 0:
                _atomic_write_new(self.path, line)
            else:
                before = self.path.stat()
                if (
                    not self.path.is_file()
                    or self.path.is_symlink()
                    or self.file_object_identity
                    != (int(before.st_dev), int(before.st_ino))
                    or before.st_size != self.expected_file_size
                ):
                    raise CollectionRefusal("late_gap_ledger_path_drift")
                descriptor = os.open(
                    self.path,
                    os.O_APPEND | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                try:
                    if os.write(descriptor, line) != len(line):
                        raise OSError("short late-gap append")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            after = self.path.stat()
            if (
                not self.path.is_file()
                or self.path.is_symlink()
                or after.st_size != self.expected_file_size + len(line)
                or (
                    self.file_object_identity is not None
                    and self.file_object_identity
                    != (int(after.st_dev), int(after.st_ino))
                )
            ):
                raise CollectionRefusal("late_gap_ledger_path_drift")
        except OSError as exc:
            raise CollectionRefusal("late_gap_ledger_append_failed") from exc
        for event in normalized:
            self.declared[str(event["symbol"])].add(int(event["minute_epoch"]))
        self.sequence += 1
        self.tail_hash = str(record["gap_entry_sha256"])
        self.source_id = source_id
        self.file_object_identity = (int(after.st_dev), int(after.st_ino))
        self.expected_file_size = int(after.st_size)


class ProspectiveActivityCollector:
    """Emit first bars and explicit permanent gaps without retroactive insertion."""

    def __init__(
        self,
        *,
        client: BridgeReadClient,
        ledger: ManifestLedger,
        gap_ledger: LateGapLedger,
        binding: ProspectiveBinding,
        policy: CollectionPolicy = CollectionPolicy(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        policy.validate()
        if client.timeout_secs != DEFAULT_HTTP_TIMEOUT_SECS:
            raise CollectionRefusal("http_timeout_must_match_sealed_policy")
        expected_binding = (
            binding.preregistration_body_sha256,
            binding.preregistration_artifact_sha256,
            binding.t0_utc,
            binding.end_utc_exclusive,
        )
        if (
            ledger.binding_tuple is not None
            and ledger.binding_tuple != expected_binding
        ):
            raise CollectionRefusal("ledger_preregistration_binding_mismatch")
        self.client = client
        self.ledger = ledger
        self.gap_ledger = gap_ledger
        self.binding = binding
        self.policy = policy
        self.clock = clock
        self.current_source_id = ledger.last_source_id
        self.segment_index = ledger.last_segment_index
        self.last_bar_epoch_by_symbol = dict(ledger.last_bar_epoch_by_symbol)
        self.last_tick_sequence_by_symbol = dict(ledger.last_tick_sequence_by_symbol)
        self.last_tick_transport_epoch_by_symbol = dict(
            ledger.last_tick_transport_epoch_by_symbol
        )
        self.last_tick_snapshot_sha256_by_symbol = dict(
            ledger.last_tick_snapshot_sha256_by_symbol
        )

    @property
    def is_pristine(self) -> bool:
        return self.ledger.next_sequence == 1 and not self.ledger.active_hour

    def assert_start_edge_open(self, now: float) -> None:
        if (
            self.is_pristine
            and now > self.binding.t0_epoch + MAXIMUM_START_EDGE_LAG_SECONDS
        ):
            raise CollectionRefusal("prospective_window_start_edge_missed")

    def _candidate_segment(self, source: SourceIdentity) -> tuple[int, bool]:
        if not self.current_source_id:
            return 1, True
        if self.current_source_id != source.source_id:
            raise CollectionRefusal("market_source_rollover_refused")
        return self.segment_index, False

    def capture_cycle(self, *, include_bars: bool) -> dict[str, Any] | None:
        cycle_started_at = _positive_float(self.clock(), "collector_clock_invalid")
        if cycle_started_at < self.binding.t0_epoch:
            raise CollectionRefusal("prospective_window_not_started")
        self.assert_start_edge_open(cycle_started_at)
        request_count = 3 + (len(SYMBOLS) if include_bars else 0)
        latest_safe_start = self.binding.end_epoch_exclusive - (
            request_count * self.client.timeout_secs
        )
        if cycle_started_at >= latest_safe_start:
            raise CollectionRefusal("prospective_cycle_deadline_insufficient")

        state = self.client.get_state()
        source = source_from_state(state, observed_at_epoch=cycle_started_at)
        candidate_segment, reset_segment = self._candidate_segment(source)
        base_last_bar = (
            {symbol: 0 for symbol in SYMBOLS}
            if reset_segment
            else dict(self.last_bar_epoch_by_symbol)
        )
        base_tick_sequence = (
            {symbol: 0 for symbol in SYMBOLS}
            if reset_segment
            else dict(self.last_tick_sequence_by_symbol)
        )
        base_tick_transport = (
            {symbol: 0.0 for symbol in SYMBOLS}
            if reset_segment
            else dict(self.last_tick_transport_epoch_by_symbol)
        )
        base_snapshot_hash = (
            {symbol: ZERO_SHA256 for symbol in SYMBOLS}
            if reset_segment
            else dict(self.last_tick_snapshot_sha256_by_symbol)
        )

        ticks_payload = self.client.get_ticks()
        quotes = validate_tick_snapshot(
            ticks_payload,
            expected_source=source,
            observed_at_epoch=cycle_started_at,
            prior_sequences=base_tick_sequence,
            prior_transport_epochs=base_tick_transport,
            prior_snapshot_sha256=base_snapshot_hash,
        )
        if self.is_pristine and (
            not quotes
            or {quote.symbol for quote in quotes} != SYMBOL_SET
            or max(quote.transport_received_at_epoch for quote in quotes)
            > self.binding.t0_epoch + MAXIMUM_START_EDGE_LAG_SECONDS
        ):
            raise CollectionRefusal("prospective_window_quote_start_edge_missed")

        candidate_bars: dict[str, tuple[M1ActivityBar, ...]] = {}
        final_last_bar = dict(base_last_bar)
        late_events: list[dict[str, Any]] = []
        if include_bars:
            for symbol in SYMBOLS:
                raw = self.client.get_m1_bars(symbol, limit=self.policy.bar_limit)
                validated = validate_m1_bar_response(
                    raw,
                    symbol=symbol,
                    expected_source=source,
                    observed_at_epoch=cycle_started_at,
                    requested_limit=self.policy.bar_limit,
                )
                last_epoch = int(base_last_bar[symbol])
                seen_epochs = self.ledger.bar_epoch_coverage_by_symbol[symbol]
                fresh_rows: list[M1ActivityBar] = []
                for bar in validated:
                    if seen_epochs.contains(bar.minute_epoch):
                        continue
                    if self.gap_ledger.contains(symbol, bar.minute_epoch):
                        continue
                    if bar.minute_epoch <= last_epoch:
                        late_events.append(
                            {
                                "symbol": symbol,
                                "minute_epoch": bar.minute_epoch,
                                "watermark_epoch": last_epoch,
                                "late_payload_sha256": canonical_sha256(bar.to_dict()),
                                "disposition": "permanent_gap_not_backfilled",
                                "baseline_eligible": False,
                            }
                        )
                        continue
                    fresh_rows.append(bar)
                fresh = tuple(fresh_rows)
                candidate_bars[symbol] = fresh
                if fresh:
                    final_last_bar[symbol] = fresh[-1].minute_epoch

        cycle_completed_at = _positive_float(self.clock(), "collector_clock_invalid")
        if cycle_completed_at < cycle_started_at:
            raise CollectionRefusal("collector_clock_regressed")
        if cycle_completed_at >= self.binding.end_epoch_exclusive:
            raise CollectionRefusal("prospective_window_closed_during_cycle")
        final_state = self.client.get_state()
        final_source = source_from_state(
            final_state, observed_at_epoch=cycle_completed_at
        )
        if final_source != source:
            raise CollectionRefusal("market_source_rollover_during_cycle")

        self.gap_ledger.append(
            late_events,
            source_id=source.source_id,
            observed_at_epoch=cycle_completed_at,
            last_bar_epoch_by_symbol=base_last_bar,
            bar_epoch_coverage_by_symbol=(self.ledger.bar_epoch_coverage_by_symbol),
        )

        flat_bars = [
            bar.to_dict()
            for symbol in SYMBOLS
            for bar in candidate_bars.get(symbol, ())
        ]
        quote_groups: dict[str, list[TickObservation]] = {}
        for quote in quotes:
            utc_hour = datetime.fromtimestamp(
                quote.transport_received_at_epoch, tz=UTC
            ).strftime("%Y%m%dT%H")
            quote_groups.setdefault(utc_hour, []).append(quote)
        bar_hour = datetime.fromtimestamp(cycle_completed_at, tz=UTC).strftime(
            "%Y%m%dT%H"
        )
        hours = sorted(set(quote_groups) | ({bar_hour} if flat_bars else set()))
        if not hours:
            return None

        running_last_bar = dict(base_last_bar)
        running_tick_sequence = dict(base_tick_sequence)
        running_tick_transport = dict(base_tick_transport)
        running_snapshot_hash = dict(base_snapshot_hash)
        latest_entry: dict[str, Any] | None = None
        for utc_hour in hours:
            hour_quotes = quote_groups.get(utc_hour, [])
            for quote in hour_quotes:
                running_tick_sequence[quote.symbol] = quote.observation_sequence
                running_tick_transport[quote.symbol] = quote.transport_received_at_epoch
                running_snapshot_hash[quote.symbol] = quote.snapshot_sha256
            hour_bars = flat_bars if utc_hour == bar_hour else []
            if hour_bars:
                running_last_bar = dict(final_last_bar)
            chunk = {
                "schema_version": CHUNK_SCHEMA_VERSION,
                "collector_schema_version": COLLECTOR_SCHEMA_VERSION,
                "source_contract_id": SOURCE_CONTRACT_ID,
                "activity_metric_id": ACTIVITY_METRIC_ID,
                "scope_version": SCOPE_VERSION,
                "symbol_scope": list(SYMBOLS),
                "timeframe": TIMEFRAME,
                "minimum_m1_history_bars": MINIMUM_M1_BARS,
                "maximum_quote_gap_seconds": MAXIMUM_TICK_INTERVAL_SECS,
                "requested_bar_limit": self.policy.bar_limit,
                "configured_tick_interval_seconds": self.policy.tick_interval_secs,
                "utc_hour": utc_hour,
                "segment_index": candidate_segment,
                "collector_cycle_started_at_epoch": cycle_started_at,
                "collector_cycle_completed_at_epoch": cycle_completed_at,
                "observed_at_epoch": cycle_completed_at,
                **self.binding.chunk_fields(),
                "source": source.sanitized(),
                "bars": hour_bars,
                "quotes": [quote.to_dict() for quote in hour_quotes],
                "last_bar_epoch_by_symbol": dict(running_last_bar),
                "last_tick_sequence_by_symbol": dict(running_tick_sequence),
                "last_tick_transport_epoch_by_symbol": dict(running_tick_transport),
                "last_tick_snapshot_sha256_by_symbol": dict(running_snapshot_hash),
                "collection_only": True,
                "evaluation_performed": False,
                "success_claim_authorized": False,
                "authority_granted": False,
                "activation_authorized": False,
                "order_authorized": False,
            }
            emitted = self.ledger.append_cycle(chunk)
            if emitted is not None:
                latest_entry = emitted
            self.current_source_id = source.source_id
            self.segment_index = candidate_segment
            self.last_bar_epoch_by_symbol = dict(running_last_bar)
            self.last_tick_sequence_by_symbol = dict(running_tick_sequence)
            self.last_tick_transport_epoch_by_symbol = dict(running_tick_transport)
            self.last_tick_snapshot_sha256_by_symbol = dict(running_snapshot_hash)
        return latest_entry


def run_collection(
    *,
    collector: ProspectiveActivityCollector,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> int:
    binding = collector.binding
    now = _positive_float(clock(), "collector_clock_invalid")
    while now < binding.t0_epoch:
        sleep(min(1.0, binding.t0_epoch - now))
        now = _positive_float(clock(), "collector_clock_invalid")
    if now >= binding.end_epoch_exclusive:
        raise CollectionRefusal("prospective_window_closed")
    collector.assert_start_edge_open(now)
    next_bar_capture = now
    cycles = 0
    tick_budget = 3 * collector.client.timeout_secs
    bar_budget = (3 + len(SYMBOLS)) * collector.client.timeout_secs
    while True:
        cycle_started = _positive_float(clock(), "collector_clock_invalid")
        if cycle_started >= binding.end_epoch_exclusive - tick_budget:
            break
        include_bars = cycle_started >= next_bar_capture
        if include_bars and cycle_started >= binding.end_epoch_exclusive - bar_budget:
            include_bars = False
        collector.capture_cycle(include_bars=include_bars)
        cycles += 1
        if include_bars:
            next_bar_capture = cycle_started + collector.policy.bar_interval_secs
        current = _positive_float(clock(), "collector_clock_invalid")
        remaining = binding.end_epoch_exclusive - tick_budget - current
        if remaining <= 0.0:
            break
        sleep_for = min(
            max(
                0.0,
                collector.policy.tick_interval_secs - (current - cycle_started),
            ),
            remaining,
        )
        if sleep_for > 0.0:
            sleep(sleep_for)
    collector.ledger.finalize_active()
    return cycles


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect restart-resilient prospective IG-MT4 M1 inputs with "
            "immutable explicit late gaps."
        )
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--preregistration", required=True)
    parser.add_argument(
        "--tick-interval-secs", type=float, default=DEFAULT_TICK_INTERVAL_SECS
    )
    parser.add_argument(
        "--bar-interval-secs", type=float, default=DEFAULT_BAR_INTERVAL_SECS
    )
    parser.add_argument("--bar-limit", type=int, default=DEFAULT_BAR_LIMIT)
    parser.add_argument(
        "--http-timeout-secs", type=float, default=DEFAULT_HTTP_TIMEOUT_SECS
    )
    parser.add_argument("--rollover-mode", choices=("refuse",), default="refuse")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        binding = load_preregistration(args.preregistration)
        api_key = read_api_key_file(args.api_key_file)
        client = BridgeReadClient(
            base_url=args.base_url,
            api_key=api_key,
            timeout_secs=args.http_timeout_secs,
        )
        with ExclusiveDataWriterLock(args.output_dir) as writer_lock:
            ledger = ManifestLedger(args.output_dir, writer_lock=writer_lock)
            gap_ledger = LateGapLedger(
                args.output_dir,
                binding=binding,
                writer_lock=writer_lock,
                last_bar_epoch_by_symbol=ledger.last_bar_epoch_by_symbol,
                bar_epoch_coverage_by_symbol=(ledger.bar_epoch_coverage_by_symbol),
            )
            policy = CollectionPolicy(
                bar_limit=args.bar_limit,
                tick_interval_secs=args.tick_interval_secs,
                bar_interval_secs=args.bar_interval_secs,
                rollover_mode=args.rollover_mode,
            )
            collector = ProspectiveActivityCollector(
                client=client,
                ledger=ledger,
                gap_ledger=gap_ledger,
                binding=binding,
                policy=policy,
            )
            now = time.time()
            while now < binding.t0_epoch:
                time.sleep(min(1.0, binding.t0_epoch - now))
                now = time.time()
            if now >= binding.end_epoch_exclusive:
                raise CollectionRefusal("prospective_window_closed")
            collector.assert_start_edge_open(now)
            client.prove_authentication_required()
            cycles = run_collection(collector=collector)
    except CollectionRefusal as exc:
        print(f"capture refused: {exc}")
        return 2
    print(
        json.dumps(
            {
                "status": "collected",
                "collection_only": True,
                "evaluation_performed": False,
                "authority_granted": False,
                "order_authorized": False,
                "cycles": cycles,
                "capture_integrity_policy_id": CAPTURE_INTEGRITY_POLICY_ID,
                "preregistration_body_sha256": (binding.preregistration_body_sha256),
                "manifest": str((Path(args.output_dir) / MANIFEST_FILENAME).resolve()),
                "late_gap_ledger": str(
                    (Path(args.output_dir) / LATE_GAP_LEDGER_FILENAME).resolve()
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
