"""Gap-v3-wire collector adapter for the independent v5 successor.

The reservation-before-GET WAL, first-finalized-observation rule, immutable
window, gap chains, writer lock, source pinning, and finalization behavior are
the exact v3 collector implementation.  This adapter advances only the sealed
attempt lineage and multiplicity family after the failed v4 first cycle.  It
remains GET-only and has no signal, outcome, performance, runtime, broker, or
trade authority.
"""

from __future__ import annotations

# AGENT: ROLE: gap-v3-wire collection-only adapter for the v5 successor.
# AGENT: HANDSHAKE: exact v3 collector + counted v4 failure -> v5 declaration.
# AGENT: ISOLATION: authenticated GET collection only; every authority false.

import hashlib
import os
from pathlib import Path
import stat
import sys
from types import ModuleType
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = Path(__file__).resolve()
V3_COLLECTOR_TEMPLATE_PATH = (
    REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v3.py"
)
COLLECTOR_ADAPTER_REVISION = (
    "fxstack.external_ig_mt4_m1_activity_resilient_collector_adapter.v4"
)
DERIVATION_MODE = "exact_v3_collector_template_plus_counted_v5_transforms"
_MAXIMUM_SOURCE_BYTES = 8 * 1024 * 1024
EXPECTED_V3_TEMPLATE_SHA256 = (
    "61b6a48b9eee7a3dc5fd615da23779b0fa876fb34049c912b791cf761e287501"
)
EXPECTED_V3_TEMPLATE_SIZE_BYTES = 298_993


class V4CollectorBootstrapRefusal(RuntimeError):
    """Raised before the successor collector adapter is safely available."""


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _is_reparse(path: Path, value: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(
        int(getattr(value, "st_file_attributes", 0)) & marker
    )


def _read_exact_source(path: Path, *, reason: str) -> tuple[bytes, tuple[int, ...]]:
    candidate = path.expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = candidate.lstat()
        if (
            _is_reparse(candidate, before_path)
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_size <= 0
            or before_path.st_size > _MAXIMUM_SOURCE_BYTES
        ):
            raise OSError(reason)
        descriptor = os.open(candidate, flags)
        try:
            before_handle = os.fstat(descriptor)
            raw = bytearray()
            remaining = int(before_handle.st_size)
            while remaining:
                block = os.read(descriptor, min(1 << 20, remaining))
                if not block:
                    raise OSError(reason)
                raw.extend(block)
                remaining -= len(block)
            if os.read(descriptor, 1):
                raise OSError(reason)
            after_handle = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = candidate.lstat()
    except OSError as exc:
        raise V4CollectorBootstrapRefusal(reason) from exc
    identities = {
        _stat_identity(value)
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if len(identities) != 1 or len(raw) != before_handle.st_size:
        raise V4CollectorBootstrapRefusal(reason)
    return bytes(raw), identities.pop()


def _self_source() -> tuple[bytes, tuple[int, ...]]:
    bound_path = globals().get("__fxstack_exact_source_path__")
    bound_raw = globals().get("__fxstack_exact_source_raw__")
    bound_identity = globals().get("__fxstack_exact_source_stat_identity__")
    if (
        isinstance(bound_path, Path)
        and bound_path == TOOL_PATH
        and isinstance(bound_raw, bytes)
        and isinstance(bound_identity, tuple)
        and len(bound_identity) == 5
        and all(isinstance(value, int) for value in bound_identity)
    ):
        return bound_raw, bound_identity
    return _read_exact_source(TOOL_PATH, reason="v4_collector_adapter_source_invalid")


def _replace_once(source: str, old: str, new: str, *, label: str) -> str:
    if source.count(old) != 1:
        raise V4CollectorBootstrapRefusal(
            f"v3_collector_template_transform_invalid:{label}"
        )
    return source.replace(old, new, 1)


def _assert_pinned_v3_template(raw: bytes) -> None:
    if (
        len(raw) != EXPECTED_V3_TEMPLATE_SIZE_BYTES
        or hashlib.sha256(raw).hexdigest() != EXPECTED_V3_TEMPLATE_SHA256
    ):
        raise V4CollectorBootstrapRefusal(
            "v3_collector_template_identity_invalid"
        )


_FAILED_V4_LITERAL = '''FAILED_V4_ATTEMPT: dict[str, Any] = {
    "preregistration_body_sha256": (
        "45f6ca5ccb23195a8253dedfc4aef1b0e95f319d35c55558aa706bfa513952bd"
    ),
    "artifact_file_sha256": (
        "7755d0e5facb7b5f28d6f5a092355e7b33d1d8d790b64bd59d204e220a5c7e55"
    ),
    "attempt_failure_sha256": (
        "56f23f4298f010bf105246ed611ba9cdd47d81e8c55131324d8a0aa6a8e3ac78"
    ),
    "reason": "unresolved_first_cycle_reservation_after_process_interruption",
    "eligible_observations_emitted": False,
    "manifest_entries_emitted": 0,
    "tail_commitment_file_size_bytes": 15_303,
    "tail_commitment_file_sha256": (
        "d770d66bd8cb138b6eb142063aa106788cb2e8be9d4479660ed82c26569b9eb8"
    ),
    "tail_commitment_last_write_utc": "2026-08-04T00:59:20.687771Z",
    "immutable_guard_identity_sha256": (
        "f90fea1134d431feb3ab8a0021bf54159bc249a477490284534d9c444b8c5cf3"
    ),
    "attempted_cells_increment": 44,
    "signal_evaluation_performed": False,
    "outcome_evaluation_performed": False,
    "performance_statistics_computed": False,
    "success_claim_evaluated": False,
}

'''

_START_EDGE_CAPTURE_OLD = '''        before_get()
        state = self.client.get_state()
        state_observed_at = self._assert_observation_inside_window(self.clock())
        source = source_from_state(state, observed_at_epoch=state_observed_at)
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

        before_get()
        ticks_payload = self.client.get_ticks()
        ticks_observed_at = self._assert_observation_inside_window(self.clock())
        quotes = validate_tick_snapshot(
            ticks_payload,
            expected_source=source,
            observed_at_epoch=ticks_observed_at,
            prior_sequences=base_tick_sequence,
            prior_transport_epochs=base_tick_transport,
            prior_snapshot_sha256=base_snapshot_hash,
        )
        for quote in quotes:
            observation_times: tuple[Any, ...] = (
                quote.observation_epoch,
                quote.observed_at_epoch,
                quote.transport_received_at_epoch,
            )
            if quote.market_event_received_at_epoch is not None:
                observation_times = (
                    *observation_times,
                    quote.market_event_received_at_epoch,
                )
            for observed in observation_times:
                self._assert_observation_inside_window(observed)
        if pristine and (
            not quotes
            or {quote.symbol for quote in quotes} != SYMBOL_SET
            or max(quote.transport_received_at_epoch for quote in quotes)
            > self.binding.t0_epoch + MAXIMUM_START_EDGE_LAG_SECONDS
        ):
            raise CollectionRefusal("prospective_window_quote_start_edge_missed")
'''

_START_EDGE_CAPTURE_NEW = '''        readiness_source: SourceIdentity | None = None
        readiness_deadline = (
            self.binding.t0_epoch + MAXIMUM_START_EDGE_LAG_SECONDS
        )
        readiness_poll_count = 0
        readiness_discarded_count = 0
        readiness_first_poll_epoch = 0.0
        readiness_qualified_epoch = 0.0
        while True:
            readiness_now = _positive_float(
                self.clock(), "collector_clock_invalid"
            )
            if pristine and readiness_now >= readiness_deadline:
                raise CollectionRefusal(
                    "prospective_window_quote_start_edge_missed"
                )
            readiness_poll_count += 1
            if readiness_poll_count == 1:
                readiness_first_poll_epoch = readiness_now
            before_get()
            state = self.client.get_state()
            state_observed_at = self._assert_observation_inside_window(
                self.clock()
            )
            candidate_source = source_from_state(
                state, observed_at_epoch=state_observed_at
            )
            if readiness_source is None:
                readiness_source = candidate_source
            elif candidate_source != readiness_source:
                raise CollectionRefusal(
                    "market_source_rollover_during_start_edge_readiness"
                )
            source = readiness_source
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

            before_get()
            ticks_payload = self.client.get_ticks()
            ticks_observed_at = self._assert_observation_inside_window(
                self.clock()
            )
            quotes = validate_tick_snapshot(
                ticks_payload,
                expected_source=source,
                observed_at_epoch=ticks_observed_at,
                prior_sequences=base_tick_sequence,
                prior_transport_epochs=base_tick_transport,
                prior_snapshot_sha256=base_snapshot_hash,
            )
            pre_t0_timestamp = False
            for quote in quotes:
                observation_times: tuple[Any, ...] = (
                    quote.observation_epoch,
                    quote.observed_at_epoch,
                    quote.transport_received_at_epoch,
                )
                if quote.market_event_received_at_epoch is not None:
                    observation_times = (
                        *observation_times,
                        quote.market_event_received_at_epoch,
                    )
                for observed in observation_times:
                    parsed = _positive_float(
                        observed, "collector_observation_time_invalid"
                    )
                    if parsed < self.binding.t0_epoch:
                        pre_t0_timestamp = True
                    else:
                        self._assert_observation_inside_window(parsed)
            if not pristine or not pre_t0_timestamp:
                readiness_qualified_epoch = ticks_observed_at
                break
            readiness_discarded_count += 1
            self._producer_monitor().assert_unchanged()
            readiness_now = _positive_float(
                self.clock(), "collector_clock_invalid"
            )
            if readiness_now >= readiness_deadline:
                raise CollectionRefusal(
                    "prospective_window_quote_start_edge_missed"
                )
            _start_edge_readiness_sleep(START_EDGE_READINESS_RETRY_SECONDS)

        for quote in quotes:
            observation_times = (
                quote.observation_epoch,
                quote.observed_at_epoch,
                quote.transport_received_at_epoch,
            )
            if quote.market_event_received_at_epoch is not None:
                observation_times = (
                    *observation_times,
                    quote.market_event_received_at_epoch,
                )
            for observed in observation_times:
                self._assert_observation_inside_window(observed)
        if pristine and (
            not quotes
            or {quote.symbol for quote in quotes} != SYMBOL_SET
            or max(quote.transport_received_at_epoch for quote in quotes)
            > readiness_deadline
        ):
            raise CollectionRefusal("prospective_window_quote_start_edge_missed")
        start_edge_readiness_proof = {
            "schema_version": START_EDGE_READINESS_PROOF_SCHEMA_VERSION,
            "first_cycle": pristine,
            "poll_count": readiness_poll_count if pristine else 0,
            "discarded_pre_t0_snapshot_count": (
                readiness_discarded_count if pristine else 0
            ),
            "first_poll_started_at_epoch": (
                readiness_first_poll_epoch if pristine else 0.0
            ),
            "qualifying_poll_completed_at_epoch": (
                readiness_qualified_epoch if pristine else 0.0
            ),
            "retry_seconds": START_EDGE_READINESS_RETRY_SECONDS,
            "same_reservation_for_every_poll": True,
            "first_qualifying_snapshot_selected": True,
            "pre_t0_snapshots_persisted": False,
        }
'''

_READINESS_PROOF_VALIDATOR_LITERAL = '''_START_EDGE_READINESS_PROOF_FIELDS = frozenset(
    {
        "schema_version",
        "first_cycle",
        "poll_count",
        "discarded_pre_t0_snapshot_count",
        "first_poll_started_at_epoch",
        "qualifying_poll_completed_at_epoch",
        "retry_seconds",
        "same_reservation_for_every_poll",
        "first_qualifying_snapshot_selected",
        "pre_t0_snapshots_persisted",
    }
)


def _non_first_start_edge_readiness_proof() -> dict[str, Any]:
    return {
        "schema_version": START_EDGE_READINESS_PROOF_SCHEMA_VERSION,
        "first_cycle": False,
        "poll_count": 0,
        "discarded_pre_t0_snapshot_count": 0,
        "first_poll_started_at_epoch": 0.0,
        "qualifying_poll_completed_at_epoch": 0.0,
        "retry_seconds": START_EDGE_READINESS_RETRY_SECONDS,
        "same_reservation_for_every_poll": True,
        "first_qualifying_snapshot_selected": True,
        "pre_t0_snapshots_persisted": False,
    }


def _validated_start_edge_readiness_proof(
    value: Any,
    *,
    cycle: Mapping[str, Any],
    reservation_sequence: int,
) -> dict[str, Any]:
    reason = "start_edge_readiness_proof_invalid"
    if not isinstance(value, Mapping) or set(value) != _START_EDGE_READINESS_PROOF_FIELDS:
        raise CollectionRefusal(reason)
    proof = dict(value)
    first_cycle = reservation_sequence == 1
    if (
        proof.get("schema_version") != START_EDGE_READINESS_PROOF_SCHEMA_VERSION
        or proof.get("first_cycle") is not first_cycle
        or proof.get("retry_seconds") != START_EDGE_READINESS_RETRY_SECONDS
        or proof.get("same_reservation_for_every_poll") is not True
        or proof.get("first_qualifying_snapshot_selected") is not True
        or proof.get("pre_t0_snapshots_persisted") is not False
    ):
        raise CollectionRefusal(reason)
    if not first_cycle:
        if proof != _non_first_start_edge_readiness_proof():
            raise CollectionRefusal(reason)
        return proof
    poll_count = _strict_positive_int(proof.get("poll_count"), reason)
    discarded = _nonnegative_int(
        proof.get("discarded_pre_t0_snapshot_count"), reason
    )
    first_poll = _positive_float(proof.get("first_poll_started_at_epoch"), reason)
    qualified = _positive_float(
        proof.get("qualifying_poll_completed_at_epoch"), reason
    )
    cycle_started = _positive_float(
        cycle.get("collector_cycle_started_at_epoch"), reason
    )
    cycle_completed = _positive_float(
        cycle.get("collector_cycle_completed_at_epoch"), reason
    )
    t0_epoch = _parse_utc_second(
        cycle.get("prospective_t0_utc_inclusive"), reason
    ).timestamp()
    if (
        discarded != poll_count - 1
        or cycle_started > first_poll
        or first_poll < t0_epoch
        or qualified < first_poll
        or qualified > cycle_completed
        or cycle_completed > t0_epoch + MAXIMUM_START_EDGE_LAG_SECONDS
    ):
        raise CollectionRefusal(reason)
    return proof


'''


def _derive_v4_implementation(raw: bytes) -> bytes:
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise V4CollectorBootstrapRefusal(
            "v3_collector_template_encoding_invalid"
        ) from exc
    transforms = (
        (
            "MAXIMUM_START_EDGE_LAG_SECONDS = 30.0",
            "MAXIMUM_START_EDGE_LAG_SECONDS = 30.0\n"
            "START_EDGE_READINESS_RETRY_SECONDS = 1.0\n"
            "START_EDGE_READINESS_PROOF_SCHEMA_VERSION = (\n"
            "    \"fxstack.external_ig_mt4_m1_start_edge_readiness_proof.v1\"\n"
            ")\n\n"
            "def _start_edge_readiness_sleep(seconds: float) -> None:\n"
            "    time.sleep(seconds)",
            "readiness_policy",
        ),
        (
            'CAPTURE_PROFILE_ID = "gap_v3_source_pinned"',
            'CAPTURE_PROFILE_ID = "gap_v3_source_pinned"\n'
            'SUPERVISION_GUARD_IDENTITY_FILENAME = (\n'
            '    "collector-guard.identity.gap-v5.v1.json"\n'
            ')\n'
            'SUPERVISION_GUARD_IDENTITY_SCHEMA_VERSION = (\n'
            '    "fxstack.mtvclc_collector_guard_identity.gap_v5.v1"\n'
            ')',
            "guard_identity_constants",
        ),
        (
            "PRIOR_ATTEMPTED_CELLS_LOWER_BOUND = 4_786",
            "PRIOR_ATTEMPTED_CELLS_LOWER_BOUND = 4_830",
            "prior_cells",
        ),
        (
            "CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND = 4_830",
            "CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND = 4_874",
            "cumulative_cells",
        ),
        (
            "AUTHORITATIVE_ABANDONED_PREREGISTRATION_LINEAGE: tuple[",
            _FAILED_V4_LITERAL
            + "AUTHORITATIVE_ABANDONED_PREREGISTRATION_LINEAGE: tuple[",
            "failed_v4_record",
        ),
        (
            "    FAILED_CROSSED_T0_ATTEMPT,\n)",
            "    FAILED_CROSSED_T0_ATTEMPT,\n    FAILED_V4_ATTEMPT,\n)",
            "failed_v4_lineage",
        ),
        (
            '    """Return the exact five-record lineage emitted by the v3 sealer."""',
            '    """Return the exact six-record lineage required by the v5 sealer."""',
            "lineage_doc",
        ),
        (
            '    "replaces_preregistration_body_sha256": FAILED_CROSSED_T0_ATTEMPT[',
            '    "replaces_preregistration_body_sha256": FAILED_V4_ATTEMPT[',
            "replacement_target",
        ),
        (
            '        "new_source_pinned_upstream_producer_software_and_distinct_gap_v3_window"',
            '        "new_independent_window_after_failed_v4_first_cycle_reservation"',
            "replacement_reason",
        ),
        (
            '"gap_v3_successor_envelope_and_4830_cell_family_required": True,',
            '"gap_v3_successor_envelope_and_4874_cell_family_required": True,',
            "capture_contract_family",
        ),
        (
            '        "first_cycle_requires_complete_direct_m1_scope": True,',
            '        "first_cycle_requires_complete_direct_m1_scope": True,\n'
            '        "first_cycle_pre_t0_snapshot_retried_under_same_reservation": True,\n'
            '        "first_cycle_readiness_re_reservation_forbidden": True,\n'
            '        "first_cycle_readiness_source_drift_refuses": True,\n'
            '        "first_cycle_only_pre_t0_timestamp_condition_is_retryable": True,\n'
            '        "first_cycle_readiness_retry_seconds": (\n'
            '            START_EDGE_READINESS_RETRY_SECONDS\n'
            '        ),\n'
            '        "first_cycle_readiness_proof_schema_version": (\n'
            '            START_EDGE_READINESS_PROOF_SCHEMA_VERSION\n'
            '        ),\n'
            '        "first_cycle_readiness_proof_committed_in_every_cycle_part": True,',
            "readiness_contract",
        ),
        (
            '        "contract_id": CAPTURE_INTEGRITY_POLICY_ID,',
            '        "contract_id": CAPTURE_INTEGRITY_POLICY_ID,\n'
            '        "supervision_guard_identity_filename": (\n'
            '            SUPERVISION_GUARD_IDENTITY_FILENAME\n'
            '        ),\n'
            '        "supervision_guard_identity_schema_version": (\n'
            '            SUPERVISION_GUARD_IDENTITY_SCHEMA_VERSION\n'
            '        ),\n'
            '        "v5_supervision_guard_identity_required": True,',
            "guard_identity_capture_contract",
        ),
        (
            '        "schema_version": '
            '"fxstack.scalp.mtvclc_preservation_filenames.v3",\n'
            '        "capture_profile_id": CAPTURE_PROFILE_ID,',
            '        "schema_version": '
            '"fxstack.scalp.mtvclc_preservation_filenames.v5",\n'
            '        "capture_profile_id": CAPTURE_PROFILE_ID,\n'
            '        "guard_identity_filename": (\n'
            '            SUPERVISION_GUARD_IDENTITY_FILENAME\n'
            '        ),\n'
            '        "guard_identity_schema_version": (\n'
            '            SUPERVISION_GUARD_IDENTITY_SCHEMA_VERSION\n'
            '        ),',
            "guard_identity_preservation_contract",
        ),
        (
            '        == "one_sided_0.05_over_4830"',
            '        == "one_sided_0.05_over_4874"',
            "wilson_alpha",
        ),
        (
            '        != "one_sided_wilson_family_adjusted_over_4830_attempted_cells"',
            '        != "one_sided_wilson_family_adjusted_over_4874_attempted_cells"',
            "wilson_method",
        ),
        (
            _START_EDGE_CAPTURE_OLD,
            _START_EDGE_CAPTURE_NEW,
            "first_cycle_readiness_loop",
        ),
        (
            "_RESERVATION_CYCLE_FIELDS = frozenset(",
            _READINESS_PROOF_VALIDATOR_LITERAL
            + "_RESERVATION_CYCLE_FIELDS = frozenset(",
            "readiness_proof_validator",
        ),
        (
            '        "cycle_reservation_parts",\n'
            '        "interrupted_cycle_gap_evidence",\n'
            "    }\n)\n_RESERVATION_MANIFEST_FIELDS",
            '        "cycle_reservation_parts",\n'
            '        "interrupted_cycle_gap_evidence",\n'
            '        "start_edge_readiness_proof",\n'
            "    }\n)\n_RESERVATION_MANIFEST_FIELDS",
            "readiness_proof_cycle_field",
        ),
        (
            '        reservation_hash = str(\n'
            '            value.get("cycle_reservation_sha256") or ""\n'
            "        ).lower()",
            '        _validated_start_edge_readiness_proof(\n'
            '            value.get("start_edge_readiness_proof"),\n'
            "            cycle=value,\n"
            "            reservation_sequence=reservation_sequence,\n"
            "        )\n"
            '        reservation_hash = str(\n'
            '            value.get("cycle_reservation_sha256") or ""\n'
            "        ).lower()",
            "readiness_proof_cycle_validation",
        ),
        (
            "        last = records[-1]\n        aggregate.update(",
            "        last = records[-1]\n"
            "        readiness_proof = last.get(\"start_edge_readiness_proof\")\n"
            "        if any(\n"
            "            record.get(\"start_edge_readiness_proof\") != readiness_proof\n"
            "            for record in records\n"
            "        ):\n"
            "            raise CollectionRefusal(\"start_edge_readiness_proof_invalid\")\n"
            "        aggregate[\"start_edge_readiness_proof\"] = dict(readiness_proof)\n"
            "        aggregate.update(",
            "readiness_proof_journal_aggregation",
        ),
        (
            '                "interrupted_cycle_gap_evidence": evidence if final else [],\n'
            '                "collection_only": True,',
            '                "interrupted_cycle_gap_evidence": evidence if final else [],\n'
            '                "start_edge_readiness_proof": (\n'
            "                    _non_first_start_edge_readiness_proof()\n"
            "                ),\n"
            '                "collection_only": True,',
            "readiness_proof_recovery_chunk",
        ),
        (
            '                "interrupted_cycle_gap_evidence": [],\n'
            '                "collection_only": True,',
            '                "interrupted_cycle_gap_evidence": [],\n'
            '                "start_edge_readiness_proof": dict(\n'
            "                    start_edge_readiness_proof\n"
            "                ),\n"
            '                "collection_only": True,',
            "readiness_proof_live_chunk",
        ),
    )
    for old, new, label in transforms:
        source = _replace_once(source, old, new, label=label)
    return source.encode("utf-8")


_SELF_RAW, _SELF_STAT_IDENTITY = _self_source()
_V3_TEMPLATE_RAW, _V3_TEMPLATE_STAT_IDENTITY = _read_exact_source(
    V3_COLLECTOR_TEMPLATE_PATH,
    reason="v3_collector_template_source_invalid",
)
_assert_pinned_v3_template(_V3_TEMPLATE_RAW)
_DERIVED_SOURCE = _derive_v4_implementation(_V3_TEMPLATE_RAW)
_IMPLEMENTATION_NAME = "_fxstack_mtvclc_gap_v3_wire_collector_v4_impl"
_implementation = ModuleType(_IMPLEMENTATION_NAME)
_implementation.__file__ = str(TOOL_PATH)
_implementation.__package__ = ""
_implementation.__dict__["__fxstack_exact_source_path__"] = TOOL_PATH
_implementation.__dict__["__fxstack_exact_source_raw__"] = _SELF_RAW
_implementation.__dict__["__fxstack_exact_source_stat_identity__"] = (
    _SELF_STAT_IDENTITY
)
_previous = sys.modules.get(_IMPLEMENTATION_NAME)
sys.modules[_IMPLEMENTATION_NAME] = _implementation
try:
    exec(  # noqa: S102 - exact template bytes plus counted deterministic transforms
        compile(_DERIVED_SOURCE, str(TOOL_PATH), "exec", dont_inherit=True),
        _implementation.__dict__,
    )
except Exception as exc:
    raise V4CollectorBootstrapRefusal(
        "v4_collector_derived_source_import_invalid"
    ) from exc
finally:
    if _previous is None:
        sys.modules.pop(_IMPLEMENTATION_NAME, None)
    else:
        sys.modules[_IMPLEMENTATION_NAME] = _previous

for _name, _value in vars(_implementation).items():
    if _name.startswith("__") or _name in globals():
        continue
    globals()[_name] = _value


def derivation_identity() -> dict[str, Any]:
    return {
        "adapter_revision": COLLECTOR_ADAPTER_REVISION,
        "derivation_mode": DERIVATION_MODE,
        "v3_template_filename": V3_COLLECTOR_TEMPLATE_PATH.name,
        "v3_template_sha256": hashlib.sha256(_V3_TEMPLATE_RAW).hexdigest(),
        "v3_template_size_bytes": len(_V3_TEMPLATE_RAW),
        "derived_source_sha256": hashlib.sha256(_DERIVED_SOURCE).hexdigest(),
        "literal_transform_count": 22,
        "collector_wire_profile": "gap_v3_source_pinned",
        "prior_attempted_cells": 4_830,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells": 4_874,
        "first_cycle_readiness_retry_seconds": 1.0,
        "first_cycle_durable_deadline_seconds_after_t0": 30.0,
        "pre_t0_observations_accepted": False,
        "network_method": "authenticated_get_only",
        "evaluation_or_trade_authority_granted": False,
    }


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
