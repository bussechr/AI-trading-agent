"""Prepare and issue signed MTVCLC evidence on isolated offline hosts.

``prepare`` revalidates the sealed preregistration, authority-free handoff,
post-window report, all three report ledgers, and the exact IG-DEMO cost
inputs.  It writes only an unsigned, authority-free issuance request.

``issue`` repeats every public check and validates the request before opening
an explicitly supplied Ed25519 signing-key file.  It signs evidence only; it
cannot install a bundle, activate a runtime, contact MT4, or authorize a trade.
"""

from __future__ import annotations

# AGENT: ROLE: isolated two-phase prepare/issue ceremony for MTVCLC evidence.
# AGENT: HANDSHAKE: sealed public artifacts -> unsigned request -> signed public evidence bundle.
# AGENT: ISOLATION: local public files only until issue's final key-load step; no capture, network, runtime, broker, or activation access.

import argparse
import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.providers.ig_mt4_catalog import (  # noqa: E402
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.runtime.mtvclc_validation_evidence import (  # noqa: E402
    BUNDLE_SHA256_FIELD,
    CERTIFICATE_SHA256_FIELD,
    CERTIFICATE_SIGNATURE_FIELD,
    EXPECTED_ATTEMPT_ACCOUNTING,
    EXPECTED_EXECUTION_CONTRACT,
    EXPECTED_SEALED_GATES,
    MAX_CERTIFICATE_VALIDITY_SECS,
    MTVCLC_ACCOUNT_MODE,
    MTVCLC_ACTIVITY_METRIC_ID,
    MTVCLC_CONFIG_ID,
    MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA,
    MTVCLC_SOURCE_CONTRACT_ID,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
    MTVCLC_VALIDATION_CERTIFICATE_SCHEMA,
    MTVCLC_VALIDATION_EVIDENCE_SCHEMA,
    MTVCLCValidationExpectation,
    NO_RUNTIME_AUTHORITY,
    WILSON_ALPHA_ALLOCATION,
    WILSON_FAMILY_ATTEMPTED_CELLS,
    WILSON_INTERVAL_METHOD,
    bundle_body_sha256,
    canonical_json_bytes,
    canonical_sha256,
    certificate_body_sha256,
    ed25519_public_key_id,
    mtvclc_evidence_error,
    verify_mtvclc_validation_evidence,
    wilson_one_sided_lower,
)
from tools import verify_mt4_tick_volume_capture_handoff as handoff  # noqa: E402


TOOL_PATH = Path(__file__).resolve()
EVALUATOR_PATH = REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window.py"
ISSUANCE_REQUEST_SCHEMA = "fxstack.scalp.mtvclc_issuance_request.v1"
ISSUANCE_REQUEST_SHA256_FIELD = "request_body_sha256"
POST_WINDOW_REPORT_SCHEMA = "fxstack.scalp.mtvclc_post_window_report.v2"
POST_WINDOW_EVALUATOR_SCHEMA = "fxstack.scalp.mtvclc_post_window_evaluator.v1"
LEDGER_ROW_SCHEMA = "fxstack.scalp.mtvclc_research_ledger_row.v1"
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_LEDGER_BYTES = 512 * 1024 * 1024
MAX_KEY_BYTES = 64 * 1024
_FALSE_RESEARCH_AUTHORITY = dict(handoff.FALSE_AUTHORITY)
_HANDOFF_FIELDS = {
    "schema_version",
    "strategy_id",
    "strategy_version",
    "config_id",
    "venue_id",
    "scope_version",
    "symbol_scope",
    "source_contract_id",
    "activity_metric_id",
    "capture_inventory",
    "capture_inventory_sha256",
    "window_closed",
    "manifest_and_chunks_verified",
    "outcome_evaluation_performed",
    "performance_statistics_computed",
    "research_only",
    "authority",
    "handoff_body_sha256",
}
_CELL_FIELDS = {
    "config_id",
    "symbol",
    "side",
    "source_ready",
    "reservations",
    "wins",
    "independent_days",
    "full_target_rate",
    "win_probability_wilson_lower",
    "base_break_even_probability",
    "mean_net_bps",
    "passes_fixed_cell_screen",
}
_OUTCOME_FIELDS = {
    "config_id",
    "symbol",
    "side",
    "signal_epoch",
    "entry_day",
    "entry_epoch",
    "exit_epoch",
    "entry_price",
    "exit_price",
    "exit_reason",
    "full_target_hit_first",
    "gross_quote_bps",
    "recorded_cost_bps",
    "currency_conversion_debit_bps",
    "net_bps",
}
_REPORT_FIELDS = {
    "schema_version",
    "evaluator_schema_version",
    "strategy_id",
    "strategy_version",
    "config_id",
    "symbol_scope",
    "prospective_t0_utc_inclusive",
    "prospective_end_utc_exclusive",
    "window_closed_before_capture_access",
    "capture_snapshot_required_stopped_and_read_only",
    "capture_snapshot_active_hour_journal_absent",
    "capture_snapshot_data_writer_lock_required",
    "capture_snapshot_data_writer_lock_proven_free",
    "capture_snapshot_reverified_after_materialization",
    "capture_snapshot_fence_rechecked_before_publication",
    "bootstrap_context_bars_retained",
    "pre_t0_signal_bars_evaluated",
    "pre_t0_quotes_evaluated",
    "frozen_screen_semantics_executed",
    "frozen_screen_hash_is_exact_executed_bytes",
    "handoff_verifier_hash_is_exact_executed_bytes",
    "screen_result_valid",
    "screen_source_scope_ready",
    "screen_source_errors",
    "screen_all_cells_pass_fixed_screen",
    "global_success_gates",
    "preregistered_success_criteria_observed",
    "evidence_binding",
    "evidence_binding_sha256",
    "ledgers",
    "capture_materialization",
    "pbo_dsr_lineage_evaluated",
    "issuer_adapter_present",
    "evaluation_performed",
    "performance_statistics_computed",
    "research_only",
    "authority",
    "success_claim_authorized",
    "promotion_authorized",
    "activation_authorized",
    "registry_write_authorized",
    "runtime_authorized",
    "issuer_authorized",
    "signature_authorized",
    "broker_access_authorized",
    "order_authorized",
    "report_body_sha256",
}


class MTVCLCReleaseRefusal(RuntimeError):
    """Fail-closed public-validation or issuance refusal."""


@dataclass(frozen=True, slots=True)
class LoadedFile:
    path: Path
    raw: bytes
    sha256: str

    def identity(self) -> dict[str, Any]:
        return {
            "filename": self.path.name,
            "sha256": self.sha256,
            "size_bytes": len(self.raw),
        }


@dataclass(frozen=True, slots=True)
class ValidatedPublicInputs:
    evidence: dict[str, Any]
    input_manifest: dict[str, dict[str, Any]]
    prospective_end_epoch: float


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _finite(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MTVCLCReleaseRefusal(reason)
    result = float(value)
    if not math.isfinite(result):
        raise MTVCLCReleaseRefusal(reason)
    return result


def _strict_int(value: Any, reason: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MTVCLCReleaseRefusal(reason)
    return value


def _parse_utc(value: Any, reason: str) -> float:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MTVCLCReleaseRefusal(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MTVCLCReleaseRefusal(reason)
    return parsed.astimezone(UTC).timestamp()


def _read_regular(path: str | Path, *, label: str, limit: int) -> LoadedFile:
    target = Path(path).expanduser().resolve(strict=False)
    if target.is_symlink() or not target.is_file():
        raise MTVCLCReleaseRefusal(f"{label}_file_invalid")
    try:
        before = target.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > limit:
            raise MTVCLCReleaseRefusal(f"{label}_file_invalid")
        raw = target.read_bytes()
        after = target.stat()
    except OSError as exc:
        raise MTVCLCReleaseRefusal(f"{label}_file_unreadable") from exc
    if (
        len(raw) != before.st_size
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise MTVCLCReleaseRefusal(f"{label}_file_changed")
    return LoadedFile(target, raw, hashlib.sha256(raw).hexdigest())


def _json_object(loaded: LoadedFile, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(loaded.raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MTVCLCReleaseRefusal(f"{label}_json_invalid") from exc
    if not isinstance(value, dict):
        raise MTVCLCReleaseRefusal(f"{label}_json_invalid")
    try:
        canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise MTVCLCReleaseRefusal(f"{label}_json_invalid") from exc
    return value


def _file_identity_matches(expected: Any, actual: LoadedFile) -> bool:
    return isinstance(expected, Mapping) and dict(expected) == actual.identity()


def _load_preregistration(path: str | Path) -> tuple[LoadedFile, dict[str, Any]]:
    loaded = _read_regular(path, label="preregistration", limit=MAX_JSON_BYTES)
    payload = _json_object(loaded, label="preregistration")
    try:
        binding = handoff.load_preregistration(loaded.path)
    except handoff.HandoffRefusal as exc:
        raise MTVCLCReleaseRefusal("preregistration_contract_invalid") from exc
    body = dict(payload)
    claimed = str(body.pop("preregistration_body_sha256", "")).lower()
    if (
        not _is_sha256(claimed)
        or not hmac.compare_digest(claimed, canonical_sha256(body))
        or loaded.path.name != f"mtvclc_v1_preregistration_{claimed}.json"
        or binding.preregistration_body_sha256 != claimed
        or binding.preregistration_artifact_sha256 != loaded.sha256
    ):
        raise MTVCLCReleaseRefusal("preregistration_identity_invalid")
    strategy = payload.get("strategy")
    scope = payload.get("scope")
    execution = payload.get("execution_contract")
    attempt = payload.get("attempt_accounting")
    gates = payload.get("fixed_success_gates")
    if not all(
        isinstance(value, Mapping)
        for value in (strategy, scope, execution, attempt, gates)
    ):
        raise MTVCLCReleaseRefusal("preregistration_public_contract_invalid")
    manifest = strategy.get("attempt_manifest")
    if (
        strategy.get("strategy_id") != MTVCLC_STRATEGY_ID
        or strategy.get("strategy_version") != MTVCLC_STRATEGY_VERSION
        or strategy.get("config_id") != MTVCLC_CONFIG_ID
        or not _is_sha256(strategy.get("config_sha256"))
        or strategy.get("source_contract_id") != MTVCLC_SOURCE_CONTRACT_ID
        or strategy.get("activity_metric_id") != MTVCLC_ACTIVITY_METRIC_ID
        or not isinstance(manifest, Mapping)
        or strategy.get("attempt_manifest_sha256") != canonical_sha256(manifest)
        or manifest.get("win_probability_familywise_attempted_cells")
        != WILSON_FAMILY_ATTEMPTED_CELLS
        or manifest.get("win_probability_alpha_allocation")
        != WILSON_ALPHA_ALLOCATION
        or not isinstance(manifest.get("configuration"), Mapping)
        or manifest["configuration"].get("maximum_entry_delay_seconds") != 5
        or manifest["configuration"].get("outcome_horizon_m1_bars") != 30
        or manifest["configuration"].get("execution_type") != "market"
        or manifest["configuration"].get("pending_orders_forbidden") is not True
    ):
        raise MTVCLCReleaseRefusal("preregistration_strategy_contract_invalid")
    expected_cell_order = [
        {"config_id": MTVCLC_CONFIG_ID, "symbol": symbol, "side": side}
        for symbol in IG_MT4_SCALP_SYMBOLS
        for side in ("BUY", "SELL")
    ]
    if (
        scope.get("venue_id") != IG_MT4_VENUE_ID
        or scope.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or scope.get("ordered_symbols") != list(IG_MT4_SCALP_SYMBOLS)
        or scope.get("sides") != ["BUY", "SELL"]
        or scope.get("cell_order") != expected_cell_order
        or dict(attempt) != EXPECTED_ATTEMPT_ACCOUNTING
        or dict(gates) != EXPECTED_SEALED_GATES
        or execution.get("entry_type") != "immediate_market"
        or execution.get("pending_orders_forbidden") is not True
        or execution.get("outcome_horizon_m1_bars") != 30
        or execution.get("maximum_entries_per_symbol_utc_day") != 1
    ):
        raise MTVCLCReleaseRefusal("preregistration_sealed_contract_invalid")
    return loaded, payload


def _load_handoff(
    path: str | Path,
    *,
    preregistration: Mapping[str, Any],
    preregistration_file: LoadedFile,
) -> tuple[LoadedFile, dict[str, Any]]:
    loaded = _read_regular(path, label="handoff", limit=MAX_JSON_BYTES)
    payload = _json_object(loaded, label="handoff")
    body = dict(payload)
    claimed = str(body.pop("handoff_body_sha256", "")).lower()
    if (
        set(payload) != _HANDOFF_FIELDS
        or not _is_sha256(claimed)
        or not hmac.compare_digest(claimed, canonical_sha256(body))
        or loaded.path.name != f"mtvclc_capture_handoff_{claimed}.json"
        or loaded.raw != canonical_json_bytes(payload) + b"\n"
    ):
        raise MTVCLCReleaseRefusal("handoff_identity_invalid")
    inventory = payload.get("capture_inventory")
    if not isinstance(inventory, Mapping):
        raise MTVCLCReleaseRefusal("handoff_inventory_invalid")
    inventory_sha = str(payload.get("capture_inventory_sha256") or "").lower()
    if (
        not _is_sha256(inventory_sha)
        or not hmac.compare_digest(inventory_sha, canonical_sha256(inventory))
        or payload.get("schema_version") != handoff.HANDOFF_SCHEMA
        or payload.get("strategy_id") != MTVCLC_STRATEGY_ID
        or payload.get("strategy_version") != MTVCLC_STRATEGY_VERSION
        or payload.get("config_id") != MTVCLC_CONFIG_ID
        or payload.get("venue_id") != IG_MT4_VENUE_ID
        or payload.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or payload.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or payload.get("source_contract_id") != MTVCLC_SOURCE_CONTRACT_ID
        or payload.get("activity_metric_id") != MTVCLC_ACTIVITY_METRIC_ID
        or payload.get("window_closed") is not True
        or payload.get("manifest_and_chunks_verified") is not True
        or payload.get("outcome_evaluation_performed") is not False
        or payload.get("performance_statistics_computed") is not False
        or payload.get("research_only") is not True
        or payload.get("authority") != _FALSE_RESEARCH_AUTHORITY
        or inventory.get("preregistration_body_sha256")
        != preregistration.get("preregistration_body_sha256")
        or inventory.get("preregistration_artifact_sha256")
        != preregistration_file.sha256
    ):
        raise MTVCLCReleaseRefusal("handoff_contract_invalid")
    return loaded, payload


def _load_cost_inputs(
    *,
    preregistration: Mapping[str, Any],
    cost_capture_json_path: str | Path,
    cost_capture_npz_path: str | Path,
    fee_attestation_path: str | Path,
) -> tuple[dict[str, Any], dict[str, LoadedFile]]:
    identities = preregistration.get("source_identities")
    policy = preregistration.get("cost_policy")
    if not isinstance(identities, Mapping) or not isinstance(policy, Mapping):
        raise MTVCLCReleaseRefusal("sealed_cost_contract_invalid")
    capture_identity = identities.get("cost_capture")
    fee_identity = identities.get("fee_attestation")
    if not isinstance(capture_identity, Mapping) or not isinstance(
        fee_identity, Mapping
    ):
        raise MTVCLCReleaseRefusal("sealed_cost_identity_invalid")
    capture_file = _read_regular(
        cost_capture_json_path, label="cost_capture_json", limit=MAX_JSON_BYTES
    )
    npz_file = _read_regular(
        cost_capture_npz_path, label="cost_capture_npz", limit=MAX_LEDGER_BYTES
    )
    fee_file = _read_regular(
        fee_attestation_path, label="fee_attestation", limit=MAX_JSON_BYTES
    )
    if (
        not _file_identity_matches(capture_identity.get("capture_json"), capture_file)
        or not _file_identity_matches(capture_identity.get("capture_npz"), npz_file)
        or not _file_identity_matches(fee_identity.get("attestation"), fee_file)
    ):
        raise MTVCLCReleaseRefusal("sealed_cost_file_identity_mismatch")
    capture = _json_object(capture_file, label="cost_capture_json")
    fee = _json_object(fee_file, label="fee_attestation")
    capture_body = dict(capture)
    capture_payload_sha = str(
        capture_body.pop("capture_payload_sha256", "")
    ).lower()
    fee_body = dict(fee)
    operator_sha = str(fee_body.pop("operator_attestation_sha256", "")).lower()
    if (
        not _is_sha256(capture_payload_sha)
        or not hmac.compare_digest(capture_payload_sha, canonical_sha256(capture_body))
        or capture_identity.get("capture_payload_sha256") != capture_payload_sha
        or capture.get("capture_mode") != "authenticated_same_source_db_history"
        or capture.get("venue_id") != IG_MT4_VENUE_ID
        or capture.get("account_mode") != MTVCLC_ACCOUNT_MODE
        or capture.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or capture.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or capture.get("source_errors") != []
        or capture.get("npz_path") != npz_file.path.name
        or capture.get("npz_sha256") != npz_file.sha256
        or capture.get("npz_size_bytes") != len(npz_file.raw)
        or not _is_sha256(operator_sha)
        or not hmac.compare_digest(operator_sha, canonical_sha256(fee_body))
        or fee_identity.get("operator_attestation_sha256") != operator_sha
        or fee.get("venue_id") != IG_MT4_VENUE_ID
        or fee.get("account_mode") != MTVCLC_ACCOUNT_MODE
        or fee.get("account_currency") != "USD"
        or fee.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or fee.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or fee.get("source_errors") != []
    ):
        raise MTVCLCReleaseRefusal("sealed_cost_public_input_invalid")
    capture_symbols = capture.get("symbols")
    fee_symbols = fee.get("symbols")
    sealed_rows = policy.get("symbols")
    if (
        not isinstance(capture_symbols, Mapping)
        or not isinstance(fee_symbols, Mapping)
        or not isinstance(sealed_rows, Mapping)
        or set(capture_symbols) != set(IG_MT4_SCALP_SYMBOLS)
        or set(fee_symbols) != set(IG_MT4_SCALP_SYMBOLS)
        or set(sealed_rows) != set(IG_MT4_SCALP_SYMBOLS)
    ):
        raise MTVCLCReleaseRefusal("sealed_cost_symbol_scope_invalid")
    independently_rebuilt: dict[str, dict[str, Any]] = {}
    for symbol in IG_MT4_SCALP_SYMBOLS:
        raw_capture = capture_symbols[symbol]
        raw_fee = fee_symbols[symbol]
        if not isinstance(raw_capture, Mapping) or not isinstance(raw_fee, Mapping):
            raise MTVCLCReleaseRefusal(f"sealed_cost_row_invalid:{symbol}")
        p90 = _finite(
            raw_capture.get("p90_observed_spread_bps"),
            f"sealed_cost_row_invalid:{symbol}",
        )
        commission = _finite(
            raw_fee.get("commission_bps_per_round_trip"),
            f"sealed_cost_row_invalid:{symbol}",
        )
        financing = _finite(
            raw_fee.get("financing_bps_per_trade"),
            f"sealed_cost_row_invalid:{symbol}",
        )
        conversion_rate = _finite(
            raw_fee.get("conversion_rate_of_absolute_profit_or_loss"),
            f"sealed_cost_row_invalid:{symbol}",
        )
        if min(p90, commission, financing, conversion_rate) < 0.0 or p90 <= 0.0:
            raise MTVCLCReleaseRefusal(f"sealed_cost_row_invalid:{symbol}")
        pnl_currency = str(raw_fee.get("profit_loss_currency") or "")
        conversion_applies = pnl_currency != "USD"
        screen_rate = conversion_rate if conversion_applies else 0.0
        pre_conversion = p90 + commission + financing + 1.0
        target = 4.0 * pre_conversion
        stop = 8.0 * pre_conversion
        break_even = (stop * (1.0 + screen_rate) + pre_conversion) / (
            target * (1.0 - screen_rate) + stop * (1.0 + screen_rate)
        )
        independently_rebuilt[symbol] = {
            "p90_ig_spread_bps": p90,
            "commission_bps_per_round_trip": commission,
            "financing_bps_per_trade": financing,
            "fixed_adverse_execution_debit_bps": 1.0,
            "pre_conversion_geometry_cost_bps": pre_conversion,
            "profit_loss_currency": pnl_currency,
            "account_currency": "USD",
            "conversion_rate_of_absolute_profit_or_loss": conversion_rate,
            "convert_on_close_charge_fraction_for_screen": screen_rate,
            "conversion_applies": conversion_applies,
            "conversion_adjusted_break_even_win_probability": break_even,
            "commission_status": raw_fee.get("commission_status"),
            "financing_status": raw_fee.get("financing_status"),
            "conversion_status": raw_fee.get("conversion_status"),
        }
        if dict(sealed_rows[symbol]) != independently_rebuilt[symbol]:
            raise MTVCLCReleaseRefusal(f"sealed_cost_row_mismatch:{symbol}")
    cost_policy_without_rows = {
        key: value for key, value in policy.items() if key != "symbols"
    }
    costs = {
        "capture_bundle": dict(capture_identity),
        "fee_attestation": fee_file.identity(),
        "cost_policy_sha256": canonical_sha256(dict(policy)),
        "cost_rows_sha256": canonical_sha256(independently_rebuilt),
        "cost_row_sha256_by_symbol": {
            symbol: canonical_sha256(independently_rebuilt[symbol])
            for symbol in IG_MT4_SCALP_SYMBOLS
        },
        "rows": independently_rebuilt,
    }
    if not cost_policy_without_rows:
        raise MTVCLCReleaseRefusal("sealed_cost_policy_invalid")
    return costs, {
        "cost_capture_json": capture_file,
        "cost_capture_npz": npz_file,
        "fee_attestation": fee_file,
    }


def _load_report(path: str | Path) -> tuple[LoadedFile, dict[str, Any]]:
    loaded = _read_regular(path, label="report", limit=MAX_JSON_BYTES)
    report = _json_object(loaded, label="report")
    body = dict(report)
    claimed = str(body.pop("report_body_sha256", "")).lower()
    if (
        set(report) != _REPORT_FIELDS
        or not _is_sha256(claimed)
        or not hmac.compare_digest(claimed, canonical_sha256(body))
        or loaded.path.name != f"mtvclc_post_window_report_{loaded.sha256}.json"
        or loaded.raw != canonical_json_bytes(report) + b"\n"
    ):
        raise MTVCLCReleaseRefusal("report_identity_invalid")
    return loaded, report


def _load_ledger(
    path: str | Path,
    *,
    kind: str,
    expected_identity: Any,
    evidence_binding_sha256: str,
    handoff_body_sha256: str,
    capture_inventory_sha256: str,
) -> tuple[LoadedFile, list[dict[str, Any]]]:
    loaded = _read_regular(path, label=f"{kind}_ledger", limit=MAX_LEDGER_BYTES)
    if not isinstance(expected_identity, Mapping):
        raise MTVCLCReleaseRefusal(f"{kind}_ledger_identity_invalid")
    expected = dict(expected_identity)
    if (
        set(expected) != {"filename", "sha256", "size_bytes", "rows"}
        or expected.get("filename") != loaded.path.name
        or expected.get("sha256") != loaded.sha256
        or expected.get("size_bytes") != len(loaded.raw)
    ):
        raise MTVCLCReleaseRefusal(f"{kind}_ledger_identity_invalid")
    records: list[dict[str, Any]] = []
    for sequence, raw_line in enumerate(loaded.raw.splitlines(keepends=True), start=1):
        if not raw_line.endswith(b"\n"):
            raise MTVCLCReleaseRefusal(f"{kind}_ledger_line_invalid")
        try:
            wrapper = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MTVCLCReleaseRefusal(f"{kind}_ledger_line_invalid") from exc
        if not isinstance(wrapper, dict) or set(wrapper) != {
            "schema_version",
            "ledger_kind",
            "sequence",
            "evidence_binding_sha256",
            "handoff_body_sha256",
            "capture_inventory_sha256",
            "record",
            "research_only",
            "authority",
        }:
            raise MTVCLCReleaseRefusal(f"{kind}_ledger_scope_invalid")
        record = wrapper.get("record")
        if (
            raw_line != canonical_json_bytes(wrapper) + b"\n"
            or wrapper.get("schema_version") != LEDGER_ROW_SCHEMA
            or wrapper.get("ledger_kind") != kind
            or wrapper.get("sequence") != sequence
            or wrapper.get("evidence_binding_sha256") != evidence_binding_sha256
            or wrapper.get("handoff_body_sha256") != handoff_body_sha256
            or wrapper.get("capture_inventory_sha256")
            != capture_inventory_sha256
            or not isinstance(record, Mapping)
            or wrapper.get("research_only") is not True
            or wrapper.get("authority") != _FALSE_RESEARCH_AUTHORITY
        ):
            raise MTVCLCReleaseRefusal(f"{kind}_ledger_contract_invalid")
        records.append(dict(record))
    if expected.get("rows") != len(records):
        raise MTVCLCReleaseRefusal(f"{kind}_ledger_row_count_invalid")
    return loaded, records


def _derive_cell_evidence(
    *,
    cell_records: Sequence[Mapping[str, Any]],
    outcome_records: Sequence[Mapping[str, Any]],
    cost_rows: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {
        (symbol, side): []
        for symbol in IG_MT4_SCALP_SYMBOLS
        for side in ("BUY", "SELL")
    }
    all_days: set[str] = set()
    for row in outcome_records:
        symbol = str(row.get("symbol") or "")
        side = str(row.get("side") or "")
        day = str(row.get("entry_day") or "")
        if (
            set(row) != _OUTCOME_FIELDS
            or (symbol, side) not in grouped
            or row.get("config_id") != MTVCLC_CONFIG_ID
            or not day
            or not isinstance(row.get("full_target_hit_first"), bool)
        ):
            raise MTVCLCReleaseRefusal("outcome_ledger_record_invalid")
        _finite(row.get("net_bps"), "outcome_ledger_record_invalid")
        grouped[(symbol, side)].append(row)
        all_days.add(day)
    if len(cell_records) != 44:
        raise MTVCLCReleaseRefusal("cell_ledger_cardinality_invalid")
    normalized: list[dict[str, Any]] = []
    for index, (symbol, side) in enumerate(
        (
            (symbol, side)
            for symbol in IG_MT4_SCALP_SYMBOLS
            for side in ("BUY", "SELL")
        )
    ):
        raw_cell = cell_records[index]
        if set(raw_cell) != _CELL_FIELDS:
            raise MTVCLCReleaseRefusal("cell_ledger_record_scope_invalid")
        selected = grouped[(symbol, side)]
        reservations = len(selected)
        wins = sum(bool(row["full_target_hit_first"]) for row in selected)
        days = len({str(row["entry_day"]) for row in selected})
        rate = wins / reservations if reservations else 0.0
        lower = wilson_one_sided_lower(wins=wins, trials=reservations)
        mean_net = (
            sum(float(row["net_bps"]) for row in selected) / reservations
            if reservations
            else 0.0
        )
        break_even = float(
            cost_rows[symbol]["conversion_adjusted_break_even_win_probability"]
        )
        expected = {
            "config_id": MTVCLC_CONFIG_ID,
            "symbol": symbol,
            "side": side,
            "source_ready": True,
            "reservations": reservations,
            "wins": wins,
            "independent_days": days,
            "full_target_rate": rate,
            "win_probability_wilson_lower": lower,
            "base_break_even_probability": break_even,
            "mean_net_bps": mean_net,
            "passes_fixed_cell_screen": bool(
                reservations >= 30
                and days >= 10
                and lower > break_even
                and mean_net > 0.0
            ),
        }
        for field, expected_value in expected.items():
            actual = raw_cell.get(field)
            if isinstance(expected_value, float):
                if not math.isclose(
                    _finite(actual, "cell_ledger_record_invalid"),
                    expected_value,
                    rel_tol=0.0,
                    abs_tol=1e-15,
                ):
                    raise MTVCLCReleaseRefusal("cell_ledger_math_mismatch")
            elif actual != expected_value:
                raise MTVCLCReleaseRefusal("cell_ledger_math_mismatch")
        if expected["passes_fixed_cell_screen"] is not True:
            raise MTVCLCReleaseRefusal(f"sealed_cell_gate_failed:{symbol}:{side}")
        normalized.append(expected)
    return normalized, len(outcome_records), len(all_days)


def validate_public_inputs(
    *,
    preregistration_path: str | Path,
    handoff_path: str | Path,
    report_path: str | Path,
    reservation_ledger_path: str | Path,
    outcome_ledger_path: str | Path,
    cell_ledger_path: str | Path,
    cost_capture_json_path: str | Path,
    cost_capture_npz_path: str | Path,
    fee_attestation_path: str | Path,
    now_epoch: float,
) -> ValidatedPublicInputs:
    """Independently rebuild all signable claims from public files only."""

    now = _finite(now_epoch, "release_clock_invalid")
    if now <= 0.0:
        raise MTVCLCReleaseRefusal("release_clock_invalid")
    prereg_file, prereg = _load_preregistration(preregistration_path)
    handoff_file, handoff_payload = _load_handoff(
        handoff_path,
        preregistration=prereg,
        preregistration_file=prereg_file,
    )
    evaluator_file = _read_regular(
        EVALUATOR_PATH, label="evaluator_source", limit=MAX_JSON_BYTES
    )
    report_file, report = _load_report(report_path)
    prospective_end_epoch = _parse_utc(
        prereg["prospective_window"]["end_utc_exclusive"],
        "prospective_end_invalid",
    )
    if now < prospective_end_epoch:
        raise MTVCLCReleaseRefusal("prospective_window_not_closed")
    report_binding = report.get("evidence_binding")
    ledgers = report.get("ledgers")
    gates = report.get("global_success_gates")
    if not isinstance(report_binding, Mapping) or not isinstance(
        ledgers, Mapping
    ) or not isinstance(gates, Mapping):
        raise MTVCLCReleaseRefusal("report_contract_invalid")
    if (
        report.get("schema_version") != POST_WINDOW_REPORT_SCHEMA
        or report.get("evaluator_schema_version") != POST_WINDOW_EVALUATOR_SCHEMA
        or report.get("strategy_id") != MTVCLC_STRATEGY_ID
        or report.get("strategy_version") != MTVCLC_STRATEGY_VERSION
        or report.get("config_id") != MTVCLC_CONFIG_ID
        or report.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or report.get("prospective_t0_utc_inclusive")
        != prereg["prospective_window"]["t0_utc_inclusive"]
        or report.get("prospective_end_utc_exclusive")
        != prereg["prospective_window"]["end_utc_exclusive"]
        or report.get("window_closed_before_capture_access") is not True
        or report.get("screen_result_valid") is not True
        or report.get("screen_source_scope_ready") is not True
        or report.get("screen_source_errors") != []
        or report.get("screen_all_cells_pass_fixed_screen") is not True
        or report.get("preregistered_success_criteria_observed") is not True
        or report.get("pbo_dsr_lineage_evaluated") is not False
        or report.get("evaluation_performed") is not True
        or report.get("performance_statistics_computed") is not True
        or report.get("research_only") is not True
        or report.get("authority") != _FALSE_RESEARCH_AUTHORITY
        or any(
            report.get(field) is not False
            for field in (
                "success_claim_authorized",
                "promotion_authorized",
                "activation_authorized",
                "registry_write_authorized",
                "runtime_authorized",
                "issuer_authorized",
                "signature_authorized",
                "broker_access_authorized",
                "order_authorized",
            )
        )
    ):
        raise MTVCLCReleaseRefusal("report_sealed_gate_contract_invalid")
    if report.get("evidence_binding_sha256") != canonical_sha256(report_binding):
        raise MTVCLCReleaseRefusal("report_evidence_binding_invalid")
    inventory = handoff_payload["capture_inventory"]
    expected_binding = {
        "preregistration_body_sha256": prereg["preregistration_body_sha256"],
        "preregistration_artifact_sha256": prereg_file.sha256,
        "handoff_body_sha256": handoff_payload["handoff_body_sha256"],
        "handoff_artifact_sha256": handoff_file.sha256,
        "capture_inventory_sha256": handoff_payload["capture_inventory_sha256"],
        "manifest_sha256": inventory["manifest_sha256"],
        "manifest_head_sha256": inventory["manifest_head_sha256"],
        "market_source_id": inventory["market_source_id"],
        "screen_source_filename": prereg["source_identities"]["screen_source"][
            "filename"
        ],
        "frozen_screen_sha256": prereg["source_identities"]["screen_source"][
            "sha256"
        ],
        "frozen_screen_support_sha256": prereg["source_identities"][
            "screen_support_source"
        ]["sha256"],
        "handoff_verifier_sha256": prereg["source_identities"].get(
            "handoff_verifier_source", {}
        ).get("sha256", report_binding.get("handoff_verifier_sha256")),
        "evaluator_source_sha256": evaluator_file.sha256,
        "cost_source_sha256": prereg["source_identities"]["cost_capture"][
            "capture_json"
        ]["sha256"],
        "source_sha256_by_symbol": report_binding.get("source_sha256_by_symbol"),
    }
    if "guard_identity_sha256" in inventory:
        expected_binding["guard_identity_sha256"] = inventory[
            "guard_identity_sha256"
        ]
    if dict(report_binding) != expected_binding:
        raise MTVCLCReleaseRefusal("report_evidence_binding_mismatch")
    costs, cost_files = _load_cost_inputs(
        preregistration=prereg,
        cost_capture_json_path=cost_capture_json_path,
        cost_capture_npz_path=cost_capture_npz_path,
        fee_attestation_path=fee_attestation_path,
    )
    if set(ledgers) != {"reservation", "outcome", "cell"}:
        raise MTVCLCReleaseRefusal("report_ledger_scope_invalid")
    reservation_file, reservation_records = _load_ledger(
        reservation_ledger_path,
        kind="reservation",
        expected_identity=ledgers["reservation"],
        evidence_binding_sha256=report["evidence_binding_sha256"],
        handoff_body_sha256=handoff_payload["handoff_body_sha256"],
        capture_inventory_sha256=handoff_payload["capture_inventory_sha256"],
    )
    outcome_file, outcome_records = _load_ledger(
        outcome_ledger_path,
        kind="outcome",
        expected_identity=ledgers["outcome"],
        evidence_binding_sha256=report["evidence_binding_sha256"],
        handoff_body_sha256=handoff_payload["handoff_body_sha256"],
        capture_inventory_sha256=handoff_payload["capture_inventory_sha256"],
    )
    cell_file, cell_records = _load_ledger(
        cell_ledger_path,
        kind="cell",
        expected_identity=ledgers["cell"],
        evidence_binding_sha256=report["evidence_binding_sha256"],
        handoff_body_sha256=handoff_payload["handoff_body_sha256"],
        capture_inventory_sha256=handoff_payload["capture_inventory_sha256"],
    )
    if len(reservation_records) != len(outcome_records):
        raise MTVCLCReleaseRefusal("reservation_outcome_cardinality_invalid")
    cells, total_trades, total_days = _derive_cell_evidence(
        cell_records=cell_records,
        outcome_records=outcome_records,
        cost_rows=costs["rows"],
    )
    expected_global = {
        "all_44_cells_pass": True,
        "source_scope_ready": True,
        "total_trades": total_trades,
        "minimum_total_trades": 300,
        "total_independent_utc_days": total_days,
        "minimum_total_independent_utc_days": 60,
        "global_trade_gate_pass": total_trades >= 300,
        "global_day_gate_pass": total_days >= 60,
        "all_preregistered_success_gates_pass": total_trades >= 300
        and total_days >= 60,
    }
    if dict(gates) != expected_global or not expected_global[
        "all_preregistered_success_gates_pass"
    ]:
        raise MTVCLCReleaseRefusal("report_global_gates_invalid")
    artifact_hashes = {
        "preregistration_body_sha256": prereg["preregistration_body_sha256"],
        "preregistration_artifact_sha256": prereg_file.sha256,
        "handoff_body_sha256": handoff_payload["handoff_body_sha256"],
        "handoff_artifact_sha256": handoff_file.sha256,
        "capture_inventory_sha256": handoff_payload["capture_inventory_sha256"],
        "evaluator_source_sha256": evaluator_file.sha256,
        "report_body_sha256": report["report_body_sha256"],
        "report_artifact_sha256": report_file.sha256,
        "evidence_binding_sha256": report["evidence_binding_sha256"],
        "reservation_ledger_sha256": reservation_file.sha256,
        "outcome_ledger_sha256": outcome_file.sha256,
        "cell_ledger_sha256": cell_file.sha256,
    }
    evidence = {
        "schema_version": MTVCLC_VALIDATION_EVIDENCE_SCHEMA,
        "account_mode": MTVCLC_ACCOUNT_MODE,
        "strategy": {
            "strategy_id": MTVCLC_STRATEGY_ID,
            "strategy_version": MTVCLC_STRATEGY_VERSION,
            "config_id": MTVCLC_CONFIG_ID,
            "config_sha256": prereg["strategy"]["config_sha256"],
            "source_contract_id": MTVCLC_SOURCE_CONTRACT_ID,
            "activity_metric_id": MTVCLC_ACTIVITY_METRIC_ID,
            "attempt_manifest_sha256": prereg["strategy"][
                "attempt_manifest_sha256"
            ],
        },
        "scope": {
            "venue_id": IG_MT4_VENUE_ID,
            "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
            "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
            "cell_order": prereg["scope"]["cell_order"],
        },
        "execution_contract": dict(EXPECTED_EXECUTION_CONTRACT),
        "attempt_accounting": dict(EXPECTED_ATTEMPT_ACCOUNTING),
        "wilson_allocation": {
            "method": WILSON_INTERVAL_METHOD,
            "family_confidence": 0.95,
            "attempted_cells": WILSON_FAMILY_ATTEMPTED_CELLS,
            "alpha_allocation": WILSON_ALPHA_ALLOCATION,
        },
        "sealed_gates": dict(EXPECTED_SEALED_GATES),
        "artifacts": artifact_hashes,
        "costs": costs,
        "overall": {
            "source_scope_ready": True,
            "source_errors": [],
            "all_44_cells_pass": True,
            "total_trades": total_trades,
            "total_independent_utc_days": total_days,
            "all_preregistered_success_gates_pass": True,
        },
        "cells": cells,
        "authority": dict(NO_RUNTIME_AUTHORITY),
    }
    evidence_error = mtvclc_evidence_error(evidence)
    if evidence_error:
        raise MTVCLCReleaseRefusal(f"public_evidence_contract_failed:{evidence_error}")
    all_files = {
        "preregistration": prereg_file,
        "handoff": handoff_file,
        "evaluator_source": evaluator_file,
        "report": report_file,
        "reservation_ledger": reservation_file,
        "outcome_ledger": outcome_file,
        "cell_ledger": cell_file,
        **cost_files,
    }
    manifest = {role: loaded.identity() for role, loaded in all_files.items()}
    return ValidatedPublicInputs(evidence, manifest, prospective_end_epoch)


def _request_body_sha256(request: Mapping[str, Any]) -> str:
    body = {
        key: value
        for key, value in dict(request).items()
        if key != ISSUANCE_REQUEST_SHA256_FIELD
    }
    return canonical_sha256(body)


def _write_new_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    target = Path(path).expanduser().resolve(strict=False)
    if target.exists() or target.is_symlink():
        raise MTVCLCReleaseRefusal("release_output_exists")
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise MTVCLCReleaseRefusal("release_output_parent_invalid")
    encoded = canonical_json_bytes(payload) + b"\n"
    temporary = target.parent / f".{target.name}.{os.getpid()}.tmp"
    if temporary.exists():
        raise MTVCLCReleaseRefusal("release_output_temporary_exists")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
        temporary.unlink()
        target.chmod(stat.S_IREAD)
        if target.read_bytes() != encoded:
            raise MTVCLCReleaseRefusal("release_output_reopen_mismatch")
    except MTVCLCReleaseRefusal:
        raise
    except OSError as exc:
        raise MTVCLCReleaseRefusal("release_output_publish_failed") from exc
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return target


def _public_input_kwargs(paths: Mapping[str, str | Path]) -> dict[str, str | Path]:
    required = {
        "preregistration_path",
        "handoff_path",
        "report_path",
        "reservation_ledger_path",
        "outcome_ledger_path",
        "cell_ledger_path",
        "cost_capture_json_path",
        "cost_capture_npz_path",
        "fee_attestation_path",
    }
    if set(paths) != required:
        raise MTVCLCReleaseRefusal("public_input_path_scope_invalid")
    return {key: paths[key] for key in required}


def prepare_issuance_request(
    *,
    public_input_paths: Mapping[str, str | Path],
    generation_id: str,
    output_path: str | Path,
    validity_secs: float = 86_400.0,
    now_epoch: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    generation = str(generation_id or "").strip()
    if not generation:
        raise MTVCLCReleaseRefusal("issuance_generation_missing")
    now = _finite(time.time() if now_epoch is None else now_epoch, "release_clock_invalid")
    validity = _finite(validity_secs, "issuance_validity_invalid")
    if (
        now <= 0.0
        or validity <= 0.0
        or validity > MAX_CERTIFICATE_VALIDITY_SECS
    ):
        raise MTVCLCReleaseRefusal("issuance_validity_invalid")
    validated = validate_public_inputs(
        **_public_input_kwargs(public_input_paths), now_epoch=now
    )
    evidence = validated.evidence
    claims = {
        "schema_version": MTVCLC_VALIDATION_CERTIFICATE_SCHEMA,
        "generation_id": generation,
        "strategy_id": MTVCLC_STRATEGY_ID,
        "strategy_version": MTVCLC_STRATEGY_VERSION,
        "config_id": MTVCLC_CONFIG_ID,
        "config_sha256": evidence["strategy"]["config_sha256"],
        "evaluator_source_sha256": evidence["artifacts"][
            "evaluator_source_sha256"
        ],
        "venue_id": IG_MT4_VENUE_ID,
        "account_mode": MTVCLC_ACCOUNT_MODE,
        "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "issued_at_epoch": now,
        "expires_at_epoch": now + validity,
        "evidence": evidence,
        "authority": dict(NO_RUNTIME_AUTHORITY),
    }
    request: dict[str, Any] = {
        "schema_version": ISSUANCE_REQUEST_SCHEMA,
        "prepared_at_epoch": now,
        "certificate_claims": claims,
        "public_input_manifest": validated.input_manifest,
        "authority": dict(NO_RUNTIME_AUTHORITY),
    }
    request[ISSUANCE_REQUEST_SHA256_FIELD] = _request_body_sha256(request)
    output = _write_new_json(output_path, request)
    return output, request


def _load_request(path: str | Path) -> tuple[LoadedFile, dict[str, Any]]:
    loaded = _read_regular(path, label="issuance_request", limit=MAX_JSON_BYTES)
    request = _json_object(loaded, label="issuance_request")
    if set(request) != {
        "schema_version",
        "prepared_at_epoch",
        "certificate_claims",
        "public_input_manifest",
        "authority",
        ISSUANCE_REQUEST_SHA256_FIELD,
    }:
        raise MTVCLCReleaseRefusal("issuance_request_scope_invalid")
    claimed = str(request.get(ISSUANCE_REQUEST_SHA256_FIELD) or "").lower()
    if (
        request.get("schema_version") != ISSUANCE_REQUEST_SCHEMA
        or not _is_sha256(claimed)
        or not hmac.compare_digest(claimed, _request_body_sha256(request))
        or loaded.raw != canonical_json_bytes(request) + b"\n"
        or request.get("authority") != NO_RUNTIME_AUTHORITY
    ):
        raise MTVCLCReleaseRefusal("issuance_request_invalid")
    return loaded, request


def _revalidate_request(
    *,
    request: Mapping[str, Any],
    public_input_paths: Mapping[str, str | Path],
    now_epoch: float,
) -> dict[str, Any]:
    now = _finite(now_epoch, "release_clock_invalid")
    claims = request.get("certificate_claims")
    if not isinstance(claims, Mapping) or set(claims) != {
        "schema_version",
        "generation_id",
        "strategy_id",
        "strategy_version",
        "config_id",
        "config_sha256",
        "evaluator_source_sha256",
        "venue_id",
        "account_mode",
        "scope_version",
        "symbol_scope",
        "issued_at_epoch",
        "expires_at_epoch",
        "evidence",
        "authority",
    }:
        raise MTVCLCReleaseRefusal("issuance_request_claim_scope_invalid")
    validated = validate_public_inputs(
        **_public_input_kwargs(public_input_paths), now_epoch=now
    )
    if request.get("public_input_manifest") != validated.input_manifest:
        raise MTVCLCReleaseRefusal("issuance_request_public_inputs_changed")
    if claims.get("evidence") != validated.evidence:
        raise MTVCLCReleaseRefusal("issuance_request_evidence_changed")
    expected_static = {
        "schema_version": MTVCLC_VALIDATION_CERTIFICATE_SCHEMA,
        "strategy_id": MTVCLC_STRATEGY_ID,
        "strategy_version": MTVCLC_STRATEGY_VERSION,
        "config_id": MTVCLC_CONFIG_ID,
        "config_sha256": validated.evidence["strategy"]["config_sha256"],
        "evaluator_source_sha256": validated.evidence["artifacts"][
            "evaluator_source_sha256"
        ],
        "venue_id": IG_MT4_VENUE_ID,
        "account_mode": MTVCLC_ACCOUNT_MODE,
        "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "authority": NO_RUNTIME_AUTHORITY,
    }
    for field, expected in expected_static.items():
        if claims.get(field) != expected:
            raise MTVCLCReleaseRefusal(f"issuance_request_claim_invalid:{field}")
    if not str(claims.get("generation_id") or "").strip():
        raise MTVCLCReleaseRefusal("issuance_request_generation_invalid")
    prepared_at = _finite(request.get("prepared_at_epoch"), "issuance_request_time_invalid")
    issued_at = _finite(claims.get("issued_at_epoch"), "issuance_request_time_invalid")
    expires_at = _finite(claims.get("expires_at_epoch"), "issuance_request_time_invalid")
    if (
        not math.isclose(prepared_at, issued_at, rel_tol=0.0, abs_tol=1e-6)
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_CERTIFICATE_VALIDITY_SECS
        or issued_at > now + 5.0
        or expires_at <= now
    ):
        raise MTVCLCReleaseRefusal("issuance_request_time_window_invalid")
    return dict(claims)


def _load_public_key(path: str | Path) -> Any:
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    loaded = _read_regular(path, label="verification_key", limit=MAX_KEY_BYTES)
    candidates: list[Any] = []
    for loader in (serialization.load_pem_public_key, serialization.load_ssh_public_key):
        try:
            candidates.append(loader(loaded.raw))
        except (TypeError, ValueError, UnsupportedAlgorithm):
            continue
    if len(loaded.raw) == 32:
        try:
            candidates.append(Ed25519PublicKey.from_public_bytes(loaded.raw))
        except ValueError:
            pass
    for candidate in candidates:
        if isinstance(candidate, Ed25519PublicKey):
            return candidate
    raise MTVCLCReleaseRefusal("verification_key_invalid")


def _load_private_key(path: str | Path) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    loaded = _read_regular(path, label="signing_key", limit=MAX_KEY_BYTES)
    try:
        key = serialization.load_pem_private_key(loaded.raw, password=None)
    except (TypeError, ValueError) as exc:
        raise MTVCLCReleaseRefusal("signing_key_invalid") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise MTVCLCReleaseRefusal("signing_key_not_ed25519")
    return key


def _sign_certificate(certificate: dict[str, Any], signing_key: Any) -> None:
    certificate["signing_key_id"] = ed25519_public_key_id(
        signing_key.public_key()
    )
    certificate["evidence_sha256"] = canonical_sha256(certificate["evidence"])
    certificate[CERTIFICATE_SHA256_FIELD] = certificate_body_sha256(certificate)
    signature = signing_key.sign(canonical_json_bytes(certificate))
    certificate[CERTIFICATE_SIGNATURE_FIELD] = base64.b64encode(signature).decode(
        "ascii"
    )


def issue_validation_bundle(
    *,
    request_path: str | Path,
    public_input_paths: Mapping[str, str | Path],
    signing_key_path: str | Path,
    verification_key_path: str | Path,
    output_path: str | Path,
    now_epoch: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Revalidate public state before the first signing-key file access."""

    _request_file, request = _load_request(request_path)
    now = _finite(time.time() if now_epoch is None else now_epoch, "release_clock_invalid")
    claims = _revalidate_request(
        request=request,
        public_input_paths=public_input_paths,
        now_epoch=now,
    )
    public_key = _load_public_key(verification_key_path)

    # This is deliberately the first operation that receives or opens the
    # signing-key path. Every public byte, gate, request claim, and public key
    # has already been revalidated above.
    signing_key = _load_private_key(signing_key_path)
    if not hmac.compare_digest(
        ed25519_public_key_id(signing_key.public_key()),
        ed25519_public_key_id(public_key),
    ):
        raise MTVCLCReleaseRefusal("issuance_keypair_mismatch")
    certificate = dict(claims)
    _sign_certificate(certificate, signing_key)
    bundle: dict[str, Any] = {
        "schema_version": MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA,
        "certificate": certificate,
    }
    bundle[BUNDLE_SHA256_FIELD] = bundle_body_sha256(bundle)
    expectation = MTVCLCValidationExpectation(
        generation_id=str(certificate["generation_id"]),
        strategy_id=MTVCLC_STRATEGY_ID,
        strategy_version=MTVCLC_STRATEGY_VERSION,
        config_id=MTVCLC_CONFIG_ID,
        config_sha256=str(certificate["config_sha256"]),
        evaluator_source_sha256=str(certificate["evaluator_source_sha256"]),
    )
    parity = verify_mtvclc_validation_evidence(
        bundle=bundle,
        public_key=public_key,
        expectation=expectation,
        now_epoch=now,
    )
    if not parity.valid:
        raise MTVCLCReleaseRefusal(
            f"public_verifier_parity_failed:{parity.reason}"
        )
    output = _write_new_json(output_path, bundle)
    return output, bundle


def _public_args(args: argparse.Namespace) -> dict[str, str]:
    return {
        "preregistration_path": str(args.preregistration),
        "handoff_path": str(args.handoff),
        "report_path": str(args.report),
        "reservation_ledger_path": str(args.reservation_ledger),
        "outcome_ledger_path": str(args.outcome_ledger),
        "cell_ledger_path": str(args.cell_ledger),
        "cost_capture_json_path": str(args.cost_capture_json),
        "cost_capture_npz_path": str(args.cost_capture_npz),
        "fee_attestation_path": str(args.fee_attestation),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare or issue MTVCLC signed evidence on an isolated offline host."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def public_inputs(target: argparse.ArgumentParser) -> None:
        target.add_argument("--preregistration", required=True)
        target.add_argument("--handoff", required=True)
        target.add_argument("--report", required=True)
        target.add_argument("--reservation-ledger", required=True)
        target.add_argument("--outcome-ledger", required=True)
        target.add_argument("--cell-ledger", required=True)
        target.add_argument("--cost-capture-json", required=True)
        target.add_argument("--cost-capture-npz", required=True)
        target.add_argument("--fee-attestation", required=True)

    prepare = commands.add_parser("prepare")
    public_inputs(prepare)
    prepare.add_argument("--generation-id", required=True)
    prepare.add_argument("--validity-secs", type=float, default=86_400.0)
    prepare.add_argument("--output", required=True)

    issue = commands.add_parser("issue")
    public_inputs(issue)
    issue.add_argument("--request", required=True)
    issue.add_argument("--signing-key-file", required=True)
    issue.add_argument("--verification-key-file", required=True)
    issue.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            output, request = prepare_issuance_request(
                public_input_paths=_public_args(args),
                generation_id=args.generation_id,
                output_path=args.output,
                validity_secs=args.validity_secs,
            )
            print(
                json.dumps(
                    {
                        "status": "prepared",
                        "output": str(output),
                        "request_body_sha256": request[
                            ISSUANCE_REQUEST_SHA256_FIELD
                        ],
                        "authority": dict(NO_RUNTIME_AUTHORITY),
                    },
                    sort_keys=True,
                )
            )
            return 0
        output, bundle = issue_validation_bundle(
            request_path=args.request,
            public_input_paths=_public_args(args),
            signing_key_path=args.signing_key_file,
            verification_key_path=args.verification_key_file,
            output_path=args.output,
        )
        print(
            json.dumps(
                {
                    "status": "issued",
                    "output": str(output),
                    "certificate_body_sha256": bundle["certificate"][
                        CERTIFICATE_SHA256_FIELD
                    ],
                    "authority": dict(NO_RUNTIME_AUTHORITY),
                },
                sort_keys=True,
            )
        )
        return 0
    except (MTVCLCReleaseRefusal, OSError, RuntimeError, ValueError) as exc:
        print(f"MTVCLC validation release refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
