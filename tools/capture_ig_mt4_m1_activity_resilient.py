"""Collect restart-resilient prospective authenticated IG-MT4 M1 inputs.

This is the versioned successor to ``capture_ig_mt4_m1_activity.py``.  The
legacy source remains immutable because an earlier preregistration binds its
exact bytes.  This collector reuses only that pinned validation/transport
support and changes the same-source bar-resume rule to the preregistered
``first_authenticated_finalized_observation_or_absence_wins`` policy.

The tool is collection-only.  It permits authenticated loopback GET requests
to state, ticks, and direct M1 bars, writes immutable hash-chained chunks, and
has no command, signal, outcome, performance, issuer, runtime, or trade path.
"""

from __future__ import annotations

# AGENT: ROLE: restart-resilient GET-only producer of prospective MTVCLC inputs.
# AGENT: HANDSHAKE: exact integrity preregistration + same-source bridge observations -> immutable capture chain.
# AGENT: ISOLATION: collection only; all research, success, issuer, runtime, activation, and trade authority is false.
# AGENT: SIDE EFFECTS: authenticated loopback GET requests and append-only local capture writes only.

import argparse
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Literal
import sys
import threading
import time


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools import capture_ig_mt4_m1_activity as preserved  # noqa: E402


TOOL_PATH = Path(__file__).resolve()
PRESERVED_SUPPORT_PATH = preserved.TOOL_PATH.resolve()
PRESERVED_SUPPORT_SHA256 = (
    "87af2452ae3f0f3964c35b2904ad35fda28f3675e1f614e941ee46fd693bda5d"
)
PRESERVED_SUPPORT_SIZE_BYTES = 80_405
COLLECTOR_SCHEMA_VERSION = "fxstack.external_ig_mt4_m1_activity_resilient_collector.v2"
CAPTURE_INTEGRITY_SCHEMA_VERSION = "fxstack.scalp.mtvclc_capture_integrity_contract.v2"
CAPTURE_INTEGRITY_POLICY_ID = (
    "authenticated_finalized_m1_first_observation_or_absence_wins_across_"
    "same_source_restart.v2"
)

# The portable chunk/manifest encoding stays byte-contract compatible with the
# existing offline handoff parser.  The new collector identity and integrity
# policy are additionally bound in the preregistration and every new chunk.
CHUNK_SCHEMA_VERSION = preserved.CHUNK_SCHEMA_VERSION
MANIFEST_SCHEMA_VERSION = preserved.MANIFEST_SCHEMA_VERSION
PREREGISTRATION_SCHEMA_VERSION = preserved.PREREGISTRATION_SCHEMA_VERSION
SOURCE_CONTRACT_ID = preserved.SOURCE_CONTRACT_ID
ACTIVITY_METRIC_ID = preserved.ACTIVITY_METRIC_ID
MARKET_SOURCE_SCHEMA = preserved.MARKET_SOURCE_SCHEMA
BROKER_ACCOUNT_SCOPE_SCHEMA = preserved.BROKER_ACCOUNT_SCOPE_SCHEMA
BROKER_ACCOUNT_SCOPE_VERSION = preserved.BROKER_ACCOUNT_SCOPE_VERSION
BRIDGE_PROTOCOL_VERSION = preserved.BRIDGE_PROTOCOL_VERSION
SCOPE_VERSION = preserved.SCOPE_VERSION
VENUE_ID = preserved.VENUE_ID
PRICE_BASIS = preserved.PRICE_BASIS
VOLUME_SOURCE = preserved.VOLUME_SOURCE
TIMEFRAME = preserved.TIMEFRAME
SYMBOLS = preserved.SYMBOLS
MINIMUM_M1_BARS = preserved.MINIMUM_M1_BARS
DEFAULT_BAR_LIMIT = preserved.DEFAULT_BAR_LIMIT
DEFAULT_TICK_INTERVAL_SECS = preserved.DEFAULT_TICK_INTERVAL_SECS
DEFAULT_BAR_INTERVAL_SECS = preserved.DEFAULT_BAR_INTERVAL_SECS
DEFAULT_HTTP_TIMEOUT_SECS = preserved.DEFAULT_HTTP_TIMEOUT_SECS
MAXIMUM_TICK_INTERVAL_SECS = preserved.MAXIMUM_TICK_INTERVAL_SECS
MAXIMUM_API_KEY_BYTES = preserved.MAXIMUM_API_KEY_BYTES
MAXIMUM_PREREGISTRATION_BYTES = preserved.MAXIMUM_PREREGISTRATION_BYTES
PROSPECTIVE_WINDOW_DAYS = preserved.PROSPECTIVE_WINDOW_DAYS
MANIFEST_FILENAME = preserved.MANIFEST_FILENAME
CHUNK_DIRECTORY = preserved.CHUNK_DIRECTORY
_FIXED_FALSE_AUTHORITY = preserved._FIXED_FALSE_AUTHORITY
ACTIVE_JOURNAL_FILENAME = "active-hour.journal.sha256.jsonl"
DATA_WRITER_LOCK_FILENAME = "collector-data-writer.lock"
ACTIVE_JOURNAL_SCHEMA_VERSION = "fxstack.external_ig_mt4_m1_activity_hour_journal.v1"
MAXIMUM_JOURNAL_LINE_BYTES = 32 * 1024 * 1024
_ZERO_SHA256 = "0" * 64


def _source_stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    """Return the stable path identity frozen for this executing module."""

    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


try:
    _module_start_stat_before = TOOL_PATH.stat()
    _module_start_source_bytes = TOOL_PATH.read_bytes()
    _module_start_stat_after = TOOL_PATH.stat()
except OSError as exc:  # pragma: no cover - import cannot proceed safely
    raise RuntimeError("collector_source_unreadable_at_module_start") from exc
if (
    _source_stat_identity(_module_start_stat_before)
    != _source_stat_identity(_module_start_stat_after)
    or len(_module_start_source_bytes) != _module_start_stat_after.st_size
):  # pragma: no cover - requires a concurrent source replacement during import
    raise RuntimeError("collector_source_changed_during_module_start")
_MODULE_START_COLLECTOR_SOURCE_SHA256 = hashlib.sha256(
    _module_start_source_bytes
).hexdigest()
_MODULE_START_COLLECTOR_SOURCE_SIZE_BYTES = len(_module_start_source_bytes)
_MODULE_START_COLLECTOR_SOURCE_STAT_IDENTITY = _source_stat_identity(
    _module_start_stat_after
)
del _module_start_source_bytes
del _module_start_stat_before
del _module_start_stat_after

_PORTABLE_CHUNK_FIELDS = frozenset(
    {
        "schema_version",
        "collector_schema_version",
        "source_contract_id",
        "activity_metric_id",
        "scope_version",
        "symbol_scope",
        "timeframe",
        "minimum_m1_history_bars",
        "maximum_quote_gap_seconds",
        "requested_bar_limit",
        "configured_tick_interval_seconds",
        "utc_hour",
        "segment_index",
        "collector_cycle_started_at_epoch",
        "collector_cycle_completed_at_epoch",
        "observed_at_epoch",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "source",
        "bars",
        "quotes",
        "last_bar_epoch_by_symbol",
        "last_tick_sequence_by_symbol",
        "last_tick_transport_epoch_by_symbol",
        "last_tick_snapshot_sha256_by_symbol",
        "collection_only",
        "evaluation_performed",
        "success_claim_authorized",
        "authority_granted",
        "activation_authorized",
        "order_authorized",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "previous_entry_sha256",
        "chunk_path",
        "chunk_sha256",
        "chunk_size_bytes",
        "chunk_schema_version",
        "utc_hour",
        "segment_index",
        "market_source_id",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "bar_rows",
        "quote_rows",
        "last_bar_epoch_by_symbol",
        "last_tick_sequence_by_symbol",
        "last_tick_transport_epoch_by_symbol",
        "last_tick_snapshot_sha256_by_symbol",
        "manifest_entry_sha256",
    }
)
_BAR_FIELDS = frozenset(
    {
        "symbol",
        "minute_epoch",
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "tick_volume",
        "price_basis",
        "volume_source",
    }
)
_QUOTE_FIELDS = frozenset(
    {
        "symbol",
        "observation_sequence",
        "observation_epoch",
        "observed_at_epoch",
        "bid",
        "ask",
        "transport_received_at_epoch",
        "market_event_received_at_epoch",
        "market_event_sequence",
        "source_event_token_sha256",
        "snapshot_sha256",
    }
)
_JOURNAL_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "journal_sequence",
        "previous_journal_entry_sha256",
        "manifest_sequence_target",
        "previous_manifest_entry_sha256",
        "capture_integrity_contract_sha256",
        "collector_source_sha256",
        "collector_support_source_sha256",
        "cycle",
        "journal_entry_sha256",
    }
)

_DATA_LOCK_REGISTRY_GUARD = threading.Lock()
_HELD_DATA_LOCK_PATHS: set[str] = set()

CollectionRefusal = preserved.CollectionRefusal
ProspectiveBinding = preserved.ProspectiveBinding
BridgeReadClient = preserved.BridgeReadClient
SourceIdentity = preserved.SourceIdentity
TickObservation = preserved.TickObservation
M1ActivityBar = preserved.M1ActivityBar
canonical_json_bytes = preserved.canonical_json_bytes
canonical_sha256 = preserved.canonical_sha256
source_from_state = preserved.source_from_state
validate_tick_snapshot = preserved.validate_tick_snapshot
validate_m1_bar_response = preserved.validate_m1_bar_response
read_api_key_file = preserved.read_api_key_file
_positive_float = preserved._positive_float
_finite_float = preserved._finite_float
_strict_nonnegative_int = preserved._strict_nonnegative_int
_strict_positive_int = preserved._strict_positive_int
_strict_json_object = preserved._strict_json_object
_validated_binding_tuple = preserved._validated_binding_tuple
_validated_loopback_base_url = preserved._validated_loopback_base_url
_is_sha256 = preserved._is_sha256
_symbol_int_map = preserved._symbol_int_map
_symbol_float_map = preserved._symbol_float_map
_symbol_hash_map = preserved._symbol_hash_map
_atomic_write_new = preserved._atomic_write_new


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _verified_support_sha256() -> str:
    try:
        raw = PRESERVED_SUPPORT_PATH.read_bytes()
    except OSError as exc:
        raise CollectionRefusal("collector_support_source_unreadable") from exc
    if len(raw) != PRESERVED_SUPPORT_SIZE_BYTES:
        raise CollectionRefusal("collector_support_source_size_invalid")
    actual = _sha256_bytes(raw)
    if actual != PRESERVED_SUPPORT_SHA256:
        raise CollectionRefusal("collector_support_source_hash_invalid")
    return actual


def _assert_collector_source_unchanged() -> None:
    """Refuse if the file backing this already-loaded module has drifted."""

    try:
        before = TOOL_PATH.stat()
        raw = TOOL_PATH.read_bytes()
        after = TOOL_PATH.stat()
    except OSError as exc:
        raise CollectionRefusal("collector_source_path_drift") from exc
    if (
        _source_stat_identity(before) != _source_stat_identity(after)
        or _source_stat_identity(after) != _MODULE_START_COLLECTOR_SOURCE_STAT_IDENTITY
        or len(raw) != _MODULE_START_COLLECTOR_SOURCE_SIZE_BYTES
        or _sha256_bytes(raw) != _MODULE_START_COLLECTOR_SOURCE_SHA256
    ):
        raise CollectionRefusal("collector_source_path_drift")


def collector_source_sha256() -> str:
    """Return the exact currently executing replacement-collector identity."""

    _assert_collector_source_unchanged()
    return _MODULE_START_COLLECTOR_SOURCE_SHA256


def expected_capture_integrity_contract() -> dict[str, Any]:
    """Build the exact integrity object a future preregistration must seal."""

    return {
        "schema_version": CAPTURE_INTEGRITY_SCHEMA_VERSION,
        "contract_id": CAPTURE_INTEGRITY_POLICY_ID,
        "collector_source_sha256": collector_source_sha256(),
        "collector_support_source_sha256": _verified_support_sha256(),
        "first_authenticated_finalized_observation_is_immutable": True,
        "matching_later_overlap_is_ignored": True,
        "revised_later_overlap_is_ignored": True,
        "overlap_never_overwrites_or_duplicates_a_bar": True,
        "late_unseen_overlap_at_or_before_watermark_is_ignored": True,
        "durable_per_symbol_watermark_never_regresses": True,
        "market_source_id_rollover_refuses": True,
        "downtime_gaps_are_preserved": True,
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


def _parse_utc_second(value: Any, reason: str) -> datetime:
    return preserved._parse_utc_second(value, reason)


def load_preregistration(path: str | Path) -> ProspectiveBinding:
    """Load only a preregistration that exactly binds this collector policy."""

    _verified_support_sha256()
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
        or body.get("authority") != _FIXED_FALSE_AUTHORITY
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
    current_source_sha256 = collector_source_sha256()
    current_source_size = _MODULE_START_COLLECTOR_SOURCE_SIZE_BYTES
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
        or str(collector_identity.get("sha256") or "").lower() != current_source_sha256
        or collector_identity.get("size_bytes") != current_source_size
        or not isinstance(support_identity, Mapping)
        or support_identity.get("filename") != PRESERVED_SUPPORT_PATH.name
        or str(support_identity.get("sha256") or "").lower() != PRESERVED_SUPPORT_SHA256
        or support_identity.get("size_bytes") != PRESERVED_SUPPORT_SIZE_BYTES
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
        preregistration_artifact_sha256=_sha256_bytes(raw),
        t0_utc=t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_utc_exclusive=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        t0_epoch=t0.timestamp(),
        end_epoch_exclusive=end.timestamp(),
    )


class ExclusiveDataWriterLock:
    """Hold one non-blocking output-scoped OS lock for a collector run."""

    def __init__(self, output_root: str | Path) -> None:
        self.root = Path(output_root).expanduser().resolve(strict=False)
        self.path = self.root / DATA_WRITER_LOCK_FILENAME
        self._handle: Any = None
        self.acquired = False

    def acquire(self) -> ExclusiveDataWriterLock:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or self.path.is_symlink():
            raise CollectionRefusal("data_writer_lock_path_invalid")
        registry_key = str(self.path).casefold()
        with _DATA_LOCK_REGISTRY_GUARD:
            if registry_key in _HELD_DATA_LOCK_PATHS:
                raise CollectionRefusal("exclusive_data_writer_lock_unavailable")
            _HELD_DATA_LOCK_PATHS.add(registry_key)
        handle: Any = None
        try:
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
        except (OSError, ImportError) as exc:
            if handle is not None:
                handle.close()
            with _DATA_LOCK_REGISTRY_GUARD:
                _HELD_DATA_LOCK_PATHS.discard(registry_key)
            raise CollectionRefusal("exclusive_data_writer_lock_unavailable") from exc
        self._handle = handle
        self.acquired = True
        return self

    def release(self) -> None:
        if not self.acquired:
            return
        registry_key = str(self.path).casefold()
        handle = self._handle
        try:
            if handle is not None:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(  # type: ignore[attr-defined]
                        handle.fileno(),
                        fcntl.LOCK_UN,  # type: ignore[attr-defined]
                    )
                handle.close()
        finally:
            self._handle = None
            self.acquired = False
            with _DATA_LOCK_REGISTRY_GUARD:
                _HELD_DATA_LOCK_PATHS.discard(registry_key)

    def __enter__(self) -> ExclusiveDataWriterLock:
        return self.acquire()

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()

    def authorizes(self, output_root: str | Path) -> bool:
        return self.acquired and self.root == Path(output_root).expanduser().resolve(
            strict=False
        )


class _BarEpochCoverage:
    """Compact exact membership for monotonic observed M1 epoch intervals."""

    __slots__ = ("_ends", "_starts")

    def __init__(self) -> None:
        self._starts: list[int] = []
        self._ends: list[int] = []

    def clone(self) -> _BarEpochCoverage:
        result = _BarEpochCoverage()
        result._starts = list(self._starts)
        result._ends = list(self._ends)
        return result

    @property
    def last(self) -> int:
        return self._ends[-1] if self._ends else 0

    def __len__(self) -> int:
        return sum(
            ((end - start) // 60) + 1
            for start, end in zip(self._starts, self._ends, strict=True)
        )

    def contains(self, epoch: int) -> bool:
        position = bisect_right(self._starts, epoch) - 1
        return position >= 0 and epoch <= self._ends[position]

    def add_monotonic(self, epoch: int) -> None:
        if epoch <= self.last:
            raise CollectionRefusal("manifest_bar_not_monotonic")
        if self._ends and epoch == self._ends[-1] + 60:
            self._ends[-1] = epoch
            return
        self._starts.append(epoch)
        self._ends.append(epoch)


class ManifestLedger:
    """Streaming hourly manifest plus one crash-safe fsynced active journal."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        writer_lock: ExclusiveDataWriterLock,
    ) -> None:
        _verified_support_sha256()
        if not writer_lock.authorizes(output_root):
            raise CollectionRefusal("exclusive_data_writer_lock_required")
        self.writer_lock = writer_lock
        self.root = Path(output_root).expanduser().resolve(strict=False)
        if self.root.exists() and self.root.is_symlink():
            raise CollectionRefusal("output_root_symlink_forbidden")
        self.root.mkdir(parents=True, exist_ok=True)
        self.chunks_root = self.root / CHUNK_DIRECTORY
        self.chunks_root.mkdir(parents=True, exist_ok=True)
        if self.chunks_root.is_symlink():
            raise CollectionRefusal("chunk_directory_symlink_forbidden")
        self.manifest_path = self.root / MANIFEST_FILENAME
        self.manifest_staging_path = self.root / f".{MANIFEST_FILENAME}.next"
        self.journal_path = self.root / ACTIVE_JOURNAL_FILENAME
        if (
            self.manifest_path.is_symlink()
            or self.manifest_staging_path.is_symlink()
            or self.journal_path.is_symlink()
        ):
            raise CollectionRefusal("collector_ledger_symlink_forbidden")

        self.entries: list[dict[str, Any]] = []
        self.next_sequence = 1
        self.last_entry_sha256 = _ZERO_SHA256
        self.last_source_id = ""
        self.last_segment_index = 0
        self.binding_tuple: tuple[str, str, str, str] | None = None
        self.last_bar_epoch_by_symbol = {symbol: 0 for symbol in SYMBOLS}
        self.last_tick_sequence_by_symbol = {symbol: 0 for symbol in SYMBOLS}
        self.last_tick_transport_epoch_by_symbol = {symbol: 0.0 for symbol in SYMBOLS}
        self.last_tick_snapshot_sha256_by_symbol = {
            symbol: _ZERO_SHA256 for symbol in SYMBOLS
        }
        self.bar_epoch_coverage_by_symbol = {
            symbol: _BarEpochCoverage() for symbol in SYMBOLS
        }
        self.active_hour = ""
        self.active_journal_sequence = 0
        self.active_journal_hash = _ZERO_SHA256
        self.active_manifest_target = 0
        self._active_base_state: (
            tuple[
                dict[str, int],
                dict[str, int],
                dict[str, float],
                dict[str, str],
                dict[str, _BarEpochCoverage],
            ]
            | None
        ) = None
        self._referenced_chunks: set[str] = set()
        self._load_manifest_streaming()
        self._recover_active_journal()
        self._verify_chunk_tree()

    @property
    def hash_chained_bar_epochs_by_symbol(self) -> dict[str, _BarEpochCoverage]:
        return self.bar_epoch_coverage_by_symbol

    def _state_copies(
        self,
    ) -> tuple[
        dict[str, int],
        dict[str, int],
        dict[str, float],
        dict[str, str],
        dict[str, _BarEpochCoverage],
    ]:
        return (
            dict(self.last_bar_epoch_by_symbol),
            dict(self.last_tick_sequence_by_symbol),
            dict(self.last_tick_transport_epoch_by_symbol),
            dict(self.last_tick_snapshot_sha256_by_symbol),
            {
                symbol: self.bar_epoch_coverage_by_symbol[symbol].clone()
                for symbol in SYMBOLS
            },
        )

    def _install_state(
        self,
        state: tuple[
            dict[str, int],
            dict[str, int],
            dict[str, float],
            dict[str, str],
            dict[str, _BarEpochCoverage],
        ],
    ) -> None:
        (
            self.last_bar_epoch_by_symbol,
            self.last_tick_sequence_by_symbol,
            self.last_tick_transport_epoch_by_symbol,
            self.last_tick_snapshot_sha256_by_symbol,
            self.bar_epoch_coverage_by_symbol,
        ) = state

    @staticmethod
    def _hour_for_epoch(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y%m%dT%H")

    def _validate_source_and_binding(
        self,
        value: Mapping[str, Any],
    ) -> str:
        binding = _validated_binding_tuple(
            value,
            reason="manifest_preregistration_binding_invalid",
        )
        if self.binding_tuple is None:
            self.binding_tuple = binding
        elif self.binding_tuple != binding:
            raise CollectionRefusal("manifest_preregistration_binding_changed")
        source = value.get("source")
        if not isinstance(source, Mapping):
            raise CollectionRefusal("manifest_chunk_source_invalid")
        source_id = str(source.get("market_source_id") or "").strip().lower()
        if not _is_sha256(source_id):
            raise CollectionRefusal("manifest_chunk_source_invalid")
        if self.last_source_id and self.last_source_id != source_id:
            raise CollectionRefusal("market_source_rollover_refused")
        return source_id

    def _validated_rows_and_state(
        self,
        value: Mapping[str, Any],
        *,
        base_state: tuple[
            dict[str, int],
            dict[str, int],
            dict[str, float],
            dict[str, str],
            dict[str, _BarEpochCoverage],
        ],
    ) -> tuple[
        dict[str, int],
        dict[str, int],
        dict[str, float],
        dict[str, str],
        dict[str, _BarEpochCoverage],
    ]:
        last_bar, last_tick, last_transport, last_snapshot, coverage = base_state
        utc_hour = str(value.get("utc_hour") or "")
        cycle_started = _positive_float(
            value.get("collector_cycle_started_at_epoch"),
            "capture_cycle_time_invalid",
        )
        bars = value.get("bars")
        quotes = value.get("quotes")
        if not isinstance(bars, list) or not isinstance(quotes, list):
            raise CollectionRefusal("manifest_chunk_rows_invalid")
        for raw_bar in bars:
            if not isinstance(raw_bar, Mapping) or set(raw_bar) != _BAR_FIELDS:
                raise CollectionRefusal("manifest_bar_invalid")
            symbol = str(raw_bar.get("symbol") or "").strip().upper()
            epoch = raw_bar.get("minute_epoch")
            if (
                symbol not in coverage
                or isinstance(epoch, bool)
                or not isinstance(epoch, int)
                or epoch <= 0
                or epoch % 60 != 0
                or epoch + 60 > cycle_started + 1e-6
            ):
                raise CollectionRefusal("manifest_bar_invalid")
            for field in ("bid_open", "bid_high", "bid_low", "bid_close"):
                _positive_float(raw_bar.get(field), "manifest_bar_invalid")
            if (
                _strict_nonnegative_int(
                    raw_bar.get("tick_volume"), "manifest_bar_invalid"
                )
                < 0
                or raw_bar.get("price_basis") != PRICE_BASIS
                or raw_bar.get("volume_source") != VOLUME_SOURCE
            ):
                raise CollectionRefusal("manifest_bar_invalid")
            coverage[symbol].add_monotonic(epoch)
            last_bar[symbol] = epoch

        for raw_quote in quotes:
            if not isinstance(raw_quote, Mapping) or set(raw_quote) != _QUOTE_FIELDS:
                raise CollectionRefusal("manifest_quote_invalid")
            symbol = str(raw_quote.get("symbol") or "").strip().upper()
            if symbol not in last_tick:
                raise CollectionRefusal("manifest_quote_invalid")
            sequence = _strict_positive_int(
                raw_quote.get("observation_sequence"), "manifest_quote_invalid"
            )
            transport = _positive_float(
                raw_quote.get("transport_received_at_epoch"),
                "manifest_quote_invalid",
            )
            observed = _positive_float(
                raw_quote.get("observed_at_epoch"), "manifest_quote_invalid"
            )
            observation_epoch = _strict_positive_int(
                raw_quote.get("observation_epoch"), "manifest_quote_invalid"
            )
            event_sequence = _strict_nonnegative_int(
                raw_quote.get("market_event_sequence"),
                "manifest_quote_invalid",
            )
            event_received = raw_quote.get("market_event_received_at_epoch")
            if event_received is not None:
                event_received = _positive_float(
                    event_received, "manifest_quote_invalid"
                )
            snapshot = str(raw_quote.get("snapshot_sha256") or "").lower()
            token_hash = str(raw_quote.get("source_event_token_sha256") or "").lower()
            bid = _positive_float(raw_quote.get("bid"), "manifest_quote_invalid")
            ask = _positive_float(raw_quote.get("ask"), "manifest_quote_invalid")
            if (
                sequence != last_tick[symbol] + 1
                or transport <= last_transport[symbol]
                or observed != transport
                or observation_epoch != int(transport // 1)
                or self._hour_for_epoch(transport) != utc_hour
                or (event_sequence == 0) != (event_received is None)
                or (event_received is not None and event_received > transport)
                or ask < bid
                or not _is_sha256(snapshot)
                or not _is_sha256(token_hash)
            ):
                raise CollectionRefusal("manifest_quote_not_monotonic")
            last_tick[symbol] = sequence
            last_transport[symbol] = transport
            last_snapshot[symbol] = snapshot

        expected_maps = (
            _symbol_int_map(
                value.get("last_bar_epoch_by_symbol"),
                "manifest_last_bar_map_invalid",
            ),
            _symbol_int_map(
                value.get("last_tick_sequence_by_symbol"),
                "manifest_last_tick_map_invalid",
            ),
            _symbol_float_map(
                value.get("last_tick_transport_epoch_by_symbol"),
                "manifest_last_tick_transport_map_invalid",
            ),
            _symbol_hash_map(
                value.get("last_tick_snapshot_sha256_by_symbol"),
                "manifest_last_tick_snapshot_map_invalid",
            ),
        )
        if expected_maps != (last_bar, last_tick, last_transport, last_snapshot):
            raise CollectionRefusal("manifest_state_map_invalid")
        return last_bar, last_tick, last_transport, last_snapshot, coverage

    def _validate_portable_chunk(
        self,
        chunk: Mapping[str, Any],
        *,
        base_state: tuple[
            dict[str, int],
            dict[str, int],
            dict[str, float],
            dict[str, str],
            dict[str, _BarEpochCoverage],
        ],
    ) -> tuple[
        str,
        tuple[
            dict[str, int],
            dict[str, int],
            dict[str, float],
            dict[str, str],
            dict[str, _BarEpochCoverage],
        ],
    ]:
        if set(chunk) != _PORTABLE_CHUNK_FIELDS:
            raise CollectionRefusal("manifest_chunk_scope_invalid")
        cycle_started = _positive_float(
            chunk.get("collector_cycle_started_at_epoch"),
            "capture_cycle_time_invalid",
        )
        cycle_completed = _positive_float(
            chunk.get("collector_cycle_completed_at_epoch"),
            "capture_cycle_time_invalid",
        )
        observed = _positive_float(
            chunk.get("observed_at_epoch"), "capture_cycle_time_invalid"
        )
        if (
            chunk.get("schema_version") != CHUNK_SCHEMA_VERSION
            or chunk.get("collector_schema_version") != COLLECTOR_SCHEMA_VERSION
            or chunk.get("source_contract_id") != SOURCE_CONTRACT_ID
            or chunk.get("activity_metric_id") != ACTIVITY_METRIC_ID
            or chunk.get("scope_version") != SCOPE_VERSION
            or chunk.get("symbol_scope") != list(SYMBOLS)
            or chunk.get("timeframe") != TIMEFRAME
            or chunk.get("minimum_m1_history_bars") != MINIMUM_M1_BARS
            or _positive_float(
                chunk.get("maximum_quote_gap_seconds"),
                "manifest_chunk_policy_invalid",
            )
            != MAXIMUM_TICK_INTERVAL_SECS
            or not MINIMUM_M1_BARS
            <= _strict_positive_int(
                chunk.get("requested_bar_limit"),
                "manifest_chunk_policy_invalid",
            )
            <= 2_000
            or not 0.0
            < _positive_float(
                chunk.get("configured_tick_interval_seconds"),
                "manifest_chunk_policy_invalid",
            )
            <= MAXIMUM_TICK_INTERVAL_SECS
            or chunk.get("segment_index") != 1
            or cycle_completed < cycle_started
            or observed != cycle_completed
            or chunk.get("collection_only") is not True
            or any(
                chunk.get(flag) is not False
                for flag in (
                    "evaluation_performed",
                    "success_claim_authorized",
                    "authority_granted",
                    "activation_authorized",
                    "order_authorized",
                )
            )
        ):
            raise CollectionRefusal("manifest_chunk_contract_invalid")
        source_id = self._validate_source_and_binding(chunk)
        assert self.binding_tuple is not None
        try:
            parsed_hour = datetime.strptime(
                str(chunk.get("utc_hour") or ""), "%Y%m%dT%H"
            )
        except ValueError:
            raise CollectionRefusal("manifest_chunk_utc_hour_invalid") from None
        t0_epoch = _parse_utc_second(
            self.binding_tuple[2], "manifest_chunk_binding_invalid"
        ).timestamp()
        end_epoch = _parse_utc_second(
            self.binding_tuple[3], "manifest_chunk_binding_invalid"
        ).timestamp()
        if (
            parsed_hour.strftime("%Y%m%dT%H") != chunk.get("utc_hour")
            or cycle_started < t0_epoch
            or cycle_completed >= end_epoch
        ):
            raise CollectionRefusal("manifest_chunk_cycle_outside_window")
        state = self._validated_rows_and_state(chunk, base_state=base_state)
        return source_id, state

    def _load_manifest_streaming(self) -> None:
        if not self.manifest_path.exists():
            return
        if not self.manifest_path.is_file() or self.manifest_path.is_symlink():
            raise CollectionRefusal("manifest_invalid")
        prior_hash = _ZERO_SHA256
        try:
            handle = self.manifest_path.open("rb")
        except OSError as exc:
            raise CollectionRefusal("manifest_unreadable") from exc
        with handle:
            for expected_sequence, raw_line in enumerate(handle, start=1):
                if not raw_line.endswith(b"\n") or raw_line == b"\n":
                    raise CollectionRefusal("manifest_line_invalid")
                parsed = _strict_json_object(raw_line, reason="manifest_invalid")
                if set(parsed) != _MANIFEST_FIELDS:
                    raise CollectionRefusal("manifest_scope_invalid")
                if raw_line != canonical_json_bytes(parsed) + b"\n":
                    raise CollectionRefusal("manifest_not_canonical")
                entry = dict(parsed)
                claimed = str(entry.pop("manifest_entry_sha256", "")).lower()
                if not _is_sha256(claimed) or canonical_sha256(entry) != claimed:
                    raise CollectionRefusal("manifest_entry_hash_invalid")
                sequence = _strict_positive_int(
                    entry.get("sequence"), "manifest_sequence_invalid"
                )
                utc_hour = str(entry.get("utc_hour") or "")
                segment = _strict_positive_int(
                    entry.get("segment_index"), "manifest_segment_invalid"
                )
                if (
                    entry.get("schema_version") != MANIFEST_SCHEMA_VERSION
                    or sequence != expected_sequence
                    or entry.get("previous_entry_sha256") != prior_hash
                    or segment != 1
                ):
                    raise CollectionRefusal("manifest_chain_invalid")
                expected_relative = PurePosixPath(
                    CHUNK_DIRECTORY,
                    utc_hour,
                    f"ig-mt4-m1-activity-s{segment:04d}-q{sequence:010d}.json",
                ).as_posix()
                relative = str(entry.get("chunk_path") or "")
                if relative != expected_relative:
                    raise CollectionRefusal("manifest_chunk_path_invalid")
                chunk_path = self.root.joinpath(*PurePosixPath(relative).parts)
                try:
                    chunk_raw = chunk_path.read_bytes()
                except OSError as exc:
                    raise CollectionRefusal("manifest_chunk_missing") from exc
                if len(chunk_raw) != entry.get("chunk_size_bytes") or _sha256_bytes(
                    chunk_raw
                ) != entry.get("chunk_sha256"):
                    raise CollectionRefusal("manifest_chunk_hash_mismatch")
                chunk = _strict_json_object(chunk_raw, reason="manifest_chunk_invalid")
                if chunk_raw != canonical_json_bytes(chunk) + b"\n":
                    raise CollectionRefusal("manifest_chunk_not_canonical")
                source_id, state = self._validate_portable_chunk(
                    chunk, base_state=self._state_copies()
                )
                binding = _validated_binding_tuple(
                    entry,
                    reason="manifest_preregistration_binding_invalid",
                )
                chunk_binding = _validated_binding_tuple(
                    chunk,
                    reason="manifest_preregistration_binding_invalid",
                )
                bars = chunk.get("bars")
                quotes = chunk.get("quotes")
                assert isinstance(bars, list)
                assert isinstance(quotes, list)
                entry_maps = (
                    _symbol_int_map(
                        entry.get("last_bar_epoch_by_symbol"),
                        "manifest_state_map_invalid",
                    ),
                    _symbol_int_map(
                        entry.get("last_tick_sequence_by_symbol"),
                        "manifest_state_map_invalid",
                    ),
                    _symbol_float_map(
                        entry.get("last_tick_transport_epoch_by_symbol"),
                        "manifest_state_map_invalid",
                    ),
                    _symbol_hash_map(
                        entry.get("last_tick_snapshot_sha256_by_symbol"),
                        "manifest_state_map_invalid",
                    ),
                )
                if (
                    binding != chunk_binding
                    or str(entry.get("market_source_id") or "").lower() != source_id
                    or entry.get("chunk_schema_version") != CHUNK_SCHEMA_VERSION
                    or entry.get("bar_rows") != len(bars)
                    or entry.get("quote_rows") != len(quotes)
                    or entry_maps != state[:4]
                ):
                    raise CollectionRefusal("manifest_entry_chunk_mismatch")
                entry["manifest_entry_sha256"] = claimed
                self.entries.append(entry)
                self._referenced_chunks.add(relative)
                self._install_state(state)
                self.last_source_id = source_id
                self.last_segment_index = 1
                prior_hash = claimed
        if self.entries:
            latest = self.entries[-1]
            self.next_sequence = int(latest["sequence"]) + 1
            self.last_entry_sha256 = str(latest["manifest_entry_sha256"])

    def _journal_integrity_sha256(self) -> str:
        return canonical_sha256(expected_capture_integrity_contract())

    def _read_active_journal(
        self,
    ) -> tuple[int, str, str, list[dict[str, Any]]]:
        executing_source_sha256 = collector_source_sha256()
        records: list[dict[str, Any]] = []
        prior_hash = _ZERO_SHA256
        target = 0
        previous_manifest = ""
        active_hour = ""
        try:
            journal_raw = self.journal_path.read_bytes()
        except OSError as exc:
            raise CollectionRefusal("active_journal_unreadable") from exc
        if not journal_raw.endswith(b"\n"):
            # A torn record can already contain the first authenticated version
            # of a finalized bar.  Discarding it and accepting a later broker
            # revision would violate first-observation-wins, so recovery must
            # refuse rather than truncate or silently substitute an observation.
            raise CollectionRefusal("active_journal_partial_record_unverifiable")
        for expected_sequence, raw_line in enumerate(
            journal_raw.splitlines(keepends=True), start=1
        ):
            if (
                not raw_line.endswith(b"\n")
                or len(raw_line) > MAXIMUM_JOURNAL_LINE_BYTES
                or raw_line == b"\n"
            ):
                raise CollectionRefusal("active_journal_line_invalid")
            parsed = _strict_json_object(raw_line, reason="active_journal_invalid")
            if set(parsed) != _JOURNAL_RECORD_FIELDS:
                raise CollectionRefusal("active_journal_scope_invalid")
            if raw_line != canonical_json_bytes(parsed) + b"\n":
                raise CollectionRefusal("active_journal_not_canonical")
            wrapper = dict(parsed)
            claimed = str(wrapper.pop("journal_entry_sha256", "")).lower()
            if not _is_sha256(claimed) or canonical_sha256(wrapper) != claimed:
                raise CollectionRefusal("active_journal_hash_invalid")
            sequence = _strict_positive_int(
                wrapper.get("journal_sequence"),
                "active_journal_sequence_invalid",
            )
            cycle = wrapper.get("cycle")
            if not isinstance(cycle, Mapping):
                raise CollectionRefusal("active_journal_cycle_invalid")
            cycle_dict = dict(cycle)
            if (
                wrapper.get("schema_version") != ACTIVE_JOURNAL_SCHEMA_VERSION
                or sequence != expected_sequence
                or wrapper.get("previous_journal_entry_sha256") != prior_hash
                or wrapper.get("capture_integrity_contract_sha256")
                != self._journal_integrity_sha256()
                or wrapper.get("collector_source_sha256") != executing_source_sha256
                or wrapper.get("collector_support_source_sha256")
                != PRESERVED_SUPPORT_SHA256
                or set(cycle_dict) != _PORTABLE_CHUNK_FIELDS
            ):
                raise CollectionRefusal("active_journal_contract_invalid")
            observed_target = _strict_positive_int(
                wrapper.get("manifest_sequence_target"),
                "active_journal_target_invalid",
            )
            observed_previous = str(
                wrapper.get("previous_manifest_entry_sha256") or ""
            ).lower()
            observed_hour = str(cycle_dict.get("utc_hour") or "")
            if not _is_sha256(observed_previous):
                raise CollectionRefusal("active_journal_target_invalid")
            if not records:
                target = observed_target
                previous_manifest = observed_previous
                active_hour = observed_hour
            elif (
                observed_target != target
                or observed_previous != previous_manifest
                or observed_hour != active_hour
            ):
                raise CollectionRefusal("active_journal_identity_changed")
            records.append(cycle_dict)
            prior_hash = claimed
        if not records:
            raise CollectionRefusal("active_journal_empty")
        return target, previous_manifest, prior_hash, records

    def _aggregate_journal_cycles(
        self, records: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        if not records:
            raise CollectionRefusal("active_journal_empty")
        first = dict(records[0])
        last = dict(records[-1])
        bars: list[Any] = []
        quotes: list[Any] = []
        starts: list[float] = []
        completions: list[float] = []
        source = first.get("source")
        for raw in records:
            cycle = dict(raw)
            if (
                cycle.get("utc_hour") != first.get("utc_hour")
                or cycle.get("source") != source
                or _validated_binding_tuple(
                    cycle, reason="active_journal_binding_invalid"
                )
                != _validated_binding_tuple(
                    first, reason="active_journal_binding_invalid"
                )
            ):
                raise CollectionRefusal("active_journal_identity_changed")
            raw_bars = cycle.get("bars")
            raw_quotes = cycle.get("quotes")
            if not isinstance(raw_bars, list) or not isinstance(raw_quotes, list):
                raise CollectionRefusal("active_journal_cycle_invalid")
            bars.extend(raw_bars)
            quotes.extend(raw_quotes)
            starts.append(
                _positive_float(
                    cycle.get("collector_cycle_started_at_epoch"),
                    "active_journal_cycle_invalid",
                )
            )
            completions.append(
                _positive_float(
                    cycle.get("collector_cycle_completed_at_epoch"),
                    "active_journal_cycle_invalid",
                )
            )
        return {
            "schema_version": CHUNK_SCHEMA_VERSION,
            "collector_schema_version": COLLECTOR_SCHEMA_VERSION,
            "source_contract_id": SOURCE_CONTRACT_ID,
            "activity_metric_id": ACTIVITY_METRIC_ID,
            "scope_version": SCOPE_VERSION,
            "symbol_scope": list(SYMBOLS),
            "timeframe": TIMEFRAME,
            "minimum_m1_history_bars": MINIMUM_M1_BARS,
            "maximum_quote_gap_seconds": MAXIMUM_TICK_INTERVAL_SECS,
            "requested_bar_limit": first["requested_bar_limit"],
            "configured_tick_interval_seconds": first[
                "configured_tick_interval_seconds"
            ],
            "utc_hour": first["utc_hour"],
            "segment_index": 1,
            # The latest constituent start proves every included finalized bar.
            "collector_cycle_started_at_epoch": max(starts),
            "collector_cycle_completed_at_epoch": max(completions),
            "observed_at_epoch": max(completions),
            "preregistration_body_sha256": first["preregistration_body_sha256"],
            "preregistration_artifact_sha256": first["preregistration_artifact_sha256"],
            "prospective_t0_utc_inclusive": first["prospective_t0_utc_inclusive"],
            "prospective_end_utc_exclusive": first["prospective_end_utc_exclusive"],
            "source": source,
            "bars": bars,
            "quotes": quotes,
            "last_bar_epoch_by_symbol": last["last_bar_epoch_by_symbol"],
            "last_tick_sequence_by_symbol": last["last_tick_sequence_by_symbol"],
            "last_tick_transport_epoch_by_symbol": last[
                "last_tick_transport_epoch_by_symbol"
            ],
            "last_tick_snapshot_sha256_by_symbol": last[
                "last_tick_snapshot_sha256_by_symbol"
            ],
            "collection_only": True,
            "evaluation_performed": False,
            "success_claim_authorized": False,
            "authority_granted": False,
            "activation_authorized": False,
            "order_authorized": False,
        }

    def _expected_manifest_entry(
        self,
        chunk: Mapping[str, Any],
        chunk_bytes: bytes,
        *,
        sequence: int,
        previous_hash: str,
    ) -> dict[str, Any]:
        utc_hour = str(chunk.get("utc_hour") or "")
        relative = PurePosixPath(
            CHUNK_DIRECTORY,
            utc_hour,
            f"ig-mt4-m1-activity-s0001-q{sequence:010d}.json",
        ).as_posix()
        source = chunk.get("source")
        if not isinstance(source, Mapping):
            raise CollectionRefusal("chunk_source_invalid")
        body = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "sequence": sequence,
            "previous_entry_sha256": previous_hash,
            "chunk_path": relative,
            "chunk_sha256": _sha256_bytes(chunk_bytes),
            "chunk_size_bytes": len(chunk_bytes),
            "chunk_schema_version": CHUNK_SCHEMA_VERSION,
            "utc_hour": utc_hour,
            "segment_index": 1,
            "market_source_id": str(source.get("market_source_id") or ""),
            "preregistration_body_sha256": chunk["preregistration_body_sha256"],
            "preregistration_artifact_sha256": chunk["preregistration_artifact_sha256"],
            "prospective_t0_utc_inclusive": chunk["prospective_t0_utc_inclusive"],
            "prospective_end_utc_exclusive": chunk["prospective_end_utc_exclusive"],
            "bar_rows": len(list(chunk.get("bars") or [])),
            "quote_rows": len(list(chunk.get("quotes") or [])),
            "last_bar_epoch_by_symbol": chunk["last_bar_epoch_by_symbol"],
            "last_tick_sequence_by_symbol": chunk["last_tick_sequence_by_symbol"],
            "last_tick_transport_epoch_by_symbol": chunk[
                "last_tick_transport_epoch_by_symbol"
            ],
            "last_tick_snapshot_sha256_by_symbol": chunk[
                "last_tick_snapshot_sha256_by_symbol"
            ],
        }
        return {**body, "manifest_entry_sha256": canonical_sha256(body)}

    def _append_manifest_entry(self, entry: Mapping[str, Any]) -> None:
        _assert_collector_source_unchanged()
        line = canonical_json_bytes(dict(entry)) + b"\n"
        try:
            existing = (
                self.manifest_path.read_bytes() if self.manifest_path.exists() else b""
            )
            desired = existing + line
            if self.manifest_staging_path.exists():
                if self.manifest_staging_path.read_bytes() != desired:
                    raise CollectionRefusal("manifest_staging_mismatch")
            else:
                _atomic_write_new(self.manifest_staging_path, desired)
            os.replace(self.manifest_staging_path, self.manifest_path)
        except OSError as exc:
            raise CollectionRefusal("manifest_append_failed") from exc

    def _finalize_active_records(
        self,
        *,
        records: Sequence[Mapping[str, Any]],
        target: int,
        previous_manifest: str,
    ) -> dict[str, Any]:
        _assert_collector_source_unchanged()
        if target != self.next_sequence or previous_manifest != self.last_entry_sha256:
            raise CollectionRefusal("active_journal_target_invalid")
        chunk = self._aggregate_journal_cycles(records)
        chunk_bytes = canonical_json_bytes(chunk) + b"\n"
        entry = self._expected_manifest_entry(
            chunk,
            chunk_bytes,
            sequence=target,
            previous_hash=previous_manifest,
        )
        relative = str(entry["chunk_path"])
        chunk_path = self.root.joinpath(*PurePosixPath(relative).parts)
        if chunk_path.exists():
            try:
                existing = chunk_path.read_bytes()
            except OSError as exc:
                raise CollectionRefusal("recovery_chunk_unreadable") from exc
            if existing != chunk_bytes:
                raise CollectionRefusal("recovery_chunk_mismatch")
        else:
            _atomic_write_new(chunk_path, chunk_bytes)
        self._append_manifest_entry(entry)
        stored = dict(entry)
        self.entries.append(stored)
        self._referenced_chunks.add(relative)
        self.next_sequence += 1
        self.last_entry_sha256 = str(entry["manifest_entry_sha256"])
        self.last_source_id = str(entry["market_source_id"])
        self.last_segment_index = 1
        return stored

    def _clear_active_journal(self) -> None:
        try:
            self.journal_path.unlink()
        except OSError as exc:
            raise CollectionRefusal("active_journal_cleanup_failed") from exc
        self.active_hour = ""
        self.active_journal_sequence = 0
        self.active_journal_hash = _ZERO_SHA256
        self.active_manifest_target = 0
        self._active_base_state = None

    def _recover_active_journal(self) -> None:
        _assert_collector_source_unchanged()
        if not self.journal_path.exists():
            return
        if not self.journal_path.is_file() or self.journal_path.is_symlink():
            raise CollectionRefusal("active_journal_invalid")
        target, previous_manifest, tail_hash, records = self._read_active_journal()
        aggregate = self._aggregate_journal_cycles(records)
        chunk_bytes = canonical_json_bytes(aggregate) + b"\n"
        if target == self.next_sequence - 1:
            if not self.entries:
                raise CollectionRefusal("active_journal_target_invalid")
            expected_entry = self._expected_manifest_entry(
                aggregate,
                chunk_bytes,
                sequence=target,
                previous_hash=previous_manifest,
            )
            if self.entries[-1] != expected_entry:
                raise CollectionRefusal("finalized_journal_recovery_mismatch")
            self.active_journal_hash = tail_hash
            self._clear_active_journal()
            return
        if target != self.next_sequence or previous_manifest != self.last_entry_sha256:
            raise CollectionRefusal("active_journal_target_invalid")
        state = self._state_copies()
        self._active_base_state = self._state_copies()
        for cycle in records:
            source_id, state = self._validate_portable_chunk(cycle, base_state=state)
            if self.last_source_id and source_id != self.last_source_id:
                raise CollectionRefusal("market_source_rollover_refused")
        self._install_state(state)
        self.active_hour = str(records[0]["utc_hour"])
        self.active_journal_sequence = len(records)
        self.active_journal_hash = tail_hash
        self.active_manifest_target = target
        self._finalize_active_records(
            records=records,
            target=target,
            previous_manifest=previous_manifest,
        )
        self._clear_active_journal()

    def _verify_chunk_tree(self) -> None:
        if self.manifest_staging_path.exists():
            raise CollectionRefusal("manifest_staging_not_recovered")
        actual = {
            path.relative_to(self.root).as_posix()
            for path in self.chunks_root.rglob("*.json")
            if path.is_file()
        }
        if actual != self._referenced_chunks:
            raise CollectionRefusal("orphan_or_missing_chunk_detected")

    def _start_active_hour(self, utc_hour: str) -> None:
        if self.journal_path.exists():
            raise CollectionRefusal("active_journal_unexpected")
        self.active_hour = utc_hour
        self.active_journal_sequence = 0
        self.active_journal_hash = _ZERO_SHA256
        self.active_manifest_target = self.next_sequence
        self._active_base_state = self._state_copies()

    def _append_journal_wrapper(self, cycle: Mapping[str, Any]) -> None:
        _assert_collector_source_unchanged()
        sequence = self.active_journal_sequence + 1
        body = {
            "schema_version": ACTIVE_JOURNAL_SCHEMA_VERSION,
            "journal_sequence": sequence,
            "previous_journal_entry_sha256": self.active_journal_hash,
            "manifest_sequence_target": self.active_manifest_target,
            "previous_manifest_entry_sha256": self.last_entry_sha256,
            "capture_integrity_contract_sha256": (self._journal_integrity_sha256()),
            "collector_source_sha256": collector_source_sha256(),
            "collector_support_source_sha256": PRESERVED_SUPPORT_SHA256,
            "cycle": dict(cycle),
        }
        wrapper = {**body, "journal_entry_sha256": canonical_sha256(body)}
        line = canonical_json_bytes(wrapper) + b"\n"
        if len(line) > MAXIMUM_JOURNAL_LINE_BYTES:
            raise CollectionRefusal("active_journal_line_too_large")
        try:
            if sequence == 1:
                _atomic_write_new(self.journal_path, line)
            else:
                descriptor = os.open(
                    self.journal_path,
                    os.O_APPEND | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                try:
                    written = os.write(descriptor, line)
                    if written != len(line):
                        raise OSError("short journal append")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as exc:
            raise CollectionRefusal("active_journal_append_failed") from exc
        self.active_journal_sequence = sequence
        self.active_journal_hash = str(wrapper["journal_entry_sha256"])

    def append_cycle(self, cycle: Mapping[str, Any]) -> dict[str, Any] | None:
        _assert_collector_source_unchanged()
        payload = dict(cycle)
        if set(payload) != _PORTABLE_CHUNK_FIELDS:
            raise CollectionRefusal("cycle_scope_invalid")
        utc_hour = str(payload.get("utc_hour") or "")
        if self.active_hour and utc_hour != self.active_hour:
            if utc_hour < self.active_hour:
                raise CollectionRefusal("active_journal_hour_regressed")
            self.finalize_active()
        if not self.active_hour:
            self._start_active_hour(utc_hour)
        source_id, next_state = self._validate_portable_chunk(
            payload, base_state=self._state_copies()
        )
        self._append_journal_wrapper(payload)
        self._install_state(next_state)
        self.last_source_id = source_id
        self.last_segment_index = 1
        # Seal the first cycle immediately so the handoff retains a truthful
        # <=5-second start-edge witness. Later cycles compact by UTC hour.
        if self.next_sequence == 1:
            return self.finalize_active()
        return None

    def finalize_active(self) -> dict[str, Any] | None:
        _assert_collector_source_unchanged()
        if not self.active_hour:
            return None
        target, previous_manifest, _tail_hash, records = self._read_active_journal()
        if self._active_base_state is None:
            raise CollectionRefusal("active_journal_base_state_missing")
        verified_state = self._active_base_state
        for cycle in records:
            _source_id, verified_state = self._validate_portable_chunk(
                cycle, base_state=verified_state
            )
        if verified_state[:4] != (
            self.last_bar_epoch_by_symbol,
            self.last_tick_sequence_by_symbol,
            self.last_tick_transport_epoch_by_symbol,
            self.last_tick_snapshot_sha256_by_symbol,
        ):
            raise CollectionRefusal("active_journal_state_drift")
        entry = self._finalize_active_records(
            records=records,
            target=target,
            previous_manifest=previous_manifest,
        )
        self._clear_active_journal()
        return entry


@dataclass(frozen=True, slots=True)
class CollectionPolicy:
    bar_limit: int = DEFAULT_BAR_LIMIT
    tick_interval_secs: float = DEFAULT_TICK_INTERVAL_SECS
    bar_interval_secs: float = DEFAULT_BAR_INTERVAL_SECS
    rollover_mode: Literal["refuse"] = "refuse"

    def validate(self) -> None:
        if (
            self.rollover_mode != "refuse"
            or self.bar_limit != DEFAULT_BAR_LIMIT
            or self.tick_interval_secs != DEFAULT_TICK_INTERVAL_SECS
            or self.bar_interval_secs != DEFAULT_BAR_INTERVAL_SECS
        ):
            raise CollectionRefusal("collection_policy_must_match_preregistration")


class ProspectiveActivityCollector:
    """Emit only first authenticated finalized observations above each watermark."""

    ledger: ManifestLedger
    policy: CollectionPolicy

    def __init__(
        self,
        *,
        client: BridgeReadClient,
        ledger: ManifestLedger,
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
            {symbol: "0" * 64 for symbol in SYMBOLS}
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

        candidate_bars: dict[str, tuple[M1ActivityBar, ...]] = {}
        final_last_bar = dict(base_last_bar)
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
                durable_watermark = int(base_last_bar[symbol])
                candidate_watermark = durable_watermark
                fresh_rows: list[M1ActivityBar] = []
                for bar in validated:
                    if bar.minute_epoch <= durable_watermark:
                        # The durable watermark commits both observations and
                        # absences below it. Later matching, revised, or newly
                        # backfilled history is ignored and can never repair a
                        # gap in the original authenticated observation.
                        continue
                    if bar.minute_epoch <= candidate_watermark:
                        raise CollectionRefusal(
                            "completed_bar_not_monotonic_above_watermark"
                        )
                    fresh_rows.append(bar)
                    candidate_watermark = bar.minute_epoch
                fresh = tuple(fresh_rows)
                candidate_bars[symbol] = fresh
                if fresh:
                    final_last_bar[symbol] = candidate_watermark

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
    """Run inside the immutable sealed window without resetting its T0."""

    binding = collector.binding
    now = _positive_float(clock(), "collector_clock_invalid")
    while now < binding.t0_epoch:
        sleep(min(1.0, binding.t0_epoch - now))
        now = _positive_float(clock(), "collector_clock_invalid")
    if now >= binding.end_epoch_exclusive:
        raise CollectionRefusal("prospective_window_closed")
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
            "Collect restart-resilient prospective authenticated IG-MT4 M1 "
            "activity inputs."
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
            policy = CollectionPolicy(
                bar_limit=args.bar_limit,
                tick_interval_secs=args.tick_interval_secs,
                bar_interval_secs=args.bar_interval_secs,
                rollover_mode=args.rollover_mode,
            )
            collector = ProspectiveActivityCollector(
                client=client,
                ledger=ledger,
                binding=binding,
                policy=policy,
            )
            now = time.time()
            while now < binding.t0_epoch:
                time.sleep(min(1.0, binding.t0_epoch - now))
                now = time.time()
            if now >= binding.end_epoch_exclusive:
                raise CollectionRefusal("prospective_window_closed")
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
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
