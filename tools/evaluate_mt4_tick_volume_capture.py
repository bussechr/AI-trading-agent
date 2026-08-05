"""Evaluate one completed MTVCLC-v1 capture on an isolated research host.

The existing handoff verifier is the mandatory first boundary.  Until it has
authenticated the sealed preregistration, the complete manifest, every chunk
and row, source continuity, and both window edges, this module does not open a
capture file or construct a strategy input.  After that pass, a second
hash-checked stream projects only the fields consumed by the frozen screen into
deterministic per-symbol binary spools.  The screen is invoked exactly once.

This is research evidence, not trading authority.  It has no network,
credential, database, signer, registry, deployment, production-control, or
broker surface.  The frozen execution model is an immediate market BUY at ask
or SELL at bid; pending orders are forbidden.
"""

from __future__ import annotations

# AGENT: ROLE: Post-window isolated MTVCLC-v1 outcome evaluator.
# AGENT FLOW: verified capture handoff -> deterministic spools -> one frozen screen call.
# AGENT HANDSHAKE: authority-free capture inventory -> authority-free research evidence.
# AGENT ISOLATION: local immutable inputs only; no external-system access or trade authority.
# AGENT: SIDE EFFECTS: one content-addressed immutable research bundle under output-root.
import argparse
import hashlib
import hmac
import json
import math
import os
import shutil
import stat
import struct
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from statistics import NormalDist
from typing import Any, Generic, TypeVar, cast, overload

REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.scalp import (
    screen_mt4_tick_volume_close_location_continuation as screen,
)

from tools import verify_mt4_tick_volume_capture_handoff as handoff

EVALUATION_SCHEMA = "fxstack.scalp.mtvclc_isolated_evaluation.v1"
GATE_SUMMARY_SCHEMA = "fxstack.scalp.mtvclc_gate_summary.v1"
SPOOL_INVENTORY_SCHEMA = "fxstack.scalp.mtvclc_screen_input_spools.v1"
BAR_SPOOL_SCHEMA = "fxstack.scalp.mtvclc_bar_spool.v1"
QUOTE_SPOOL_SCHEMA = "fxstack.scalp.mtvclc_quote_spool.v1"

BAR_STRUCT = struct.Struct(">qddddq")
QUOTE_STRUCT = struct.Struct(">qdd")
BAR_FIELDS = (
    "epoch_int64",
    "bid_open_float64",
    "bid_high_float64",
    "bid_low_float64",
    "bid_close_float64",
    "tick_volume_int64",
)
QUOTE_FIELDS = ("epoch_int64", "bid_float64", "ask_float64")

MINIMUM_TOTAL_TRADES = 300
MINIMUM_TOTAL_INDEPENDENT_DAYS = 60
OUTPUT_HEADROOM_BYTES = 512 * 1024 * 1024
MAXIMUM_PREREGISTRATION_BYTES = 1024 * 1024
MAXIMUM_SPOOL_HEADER_BYTES = 4096
SIDES = ("BUY", "SELL")
HEX = frozenset("0123456789abcdef")

FALSE_AUTHORITY: dict[str, bool] = dict(handoff.FALSE_AUTHORITY)

EXPECTED_COST_POLICY = {
    "formula": (
        "net_bps=gross_quote_bps-(p90_ig_spread_bps+"
        "commission_bps_per_round_trip+financing_bps_per_trade+"
        "1.0bps_adverse_execution_debit)-"
        "conversion_rate*abs(gross_quote_bps)_when_profit_loss_currency_"
        "differs_from_account_currency"
    ),
    "conversion_treatment": (
        "debit_the_attested_rate_on_absolute_profit_or_loss_for_both_"
        "wins_and_losses;never_credit_conversion;zero_only_when_the_"
        "profit_loss_currency_equals_the_attested_account_currency"
    ),
    "geometry_uses_pre_conversion_cost": True,
    "final_cell_mean_uses_conversion_adjusted_net": True,
    "unknown_commission_financing_or_conversion_refuses_evaluation": True,
    "fee_schedule_change_or_source_uncertainty_refuses_evaluation": True,
}

COST_ROW_FIELDS = frozenset(
    {
        "p90_ig_spread_bps",
        "commission_bps_per_round_trip",
        "financing_bps_per_trade",
        "fixed_adverse_execution_debit_bps",
        "pre_conversion_geometry_cost_bps",
        "profit_loss_currency",
        "account_currency",
        "conversion_rate_of_absolute_profit_or_loss",
        "convert_on_close_charge_fraction_for_screen",
        "conversion_applies",
        "conversion_adjusted_break_even_win_probability",
        "commission_status",
        "financing_status",
        "conversion_status",
    }
)
RESERVATION_FIELDS = frozenset(
    {field.name for field in dataclass_fields(screen.MTVCLCClosedSignal)}
    | {"entry_status"}
)
OUTCOME_FIELDS = frozenset(
    field.name for field in dataclass_fields(screen.MTVCLCOutcome)
)


class EvaluationRefusal(RuntimeError):
    """Stable fail-closed refusal emitted without publishing evidence."""


@dataclass(frozen=True, slots=True)
class FileIdentity:
    filename: str
    sha256: str
    size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SpoolFile:
    path: Path
    identity: FileIdentity
    records: int
    record_size_bytes: int
    schema_version: str
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SymbolSpools:
    symbol: str
    bars: SpoolFile
    quotes: SpoolFile
    screen_input_sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedPreregistration:
    payload: dict[str, Any]
    calibrations: dict[str, screen.MT4CostCalibration]
    identities: dict[str, FileIdentity]
    screen_source: FileIdentity


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvaluationRefusal("noncanonical_json_value") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and set(text) <= HEX


def _strict_int(
    value: Any,
    reason: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise EvaluationRefusal(reason)
    return value


def _finite(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationRefusal(reason)
    result = float(value)
    if not math.isfinite(result):
        raise EvaluationRefusal(reason)
    return result


def _identity(path: Path) -> FileIdentity:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EvaluationRefusal("source_identity_unreadable") from exc
    return FileIdentity(
        filename=path.name,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )


def _identity_row(value: Any, reason: str) -> FileIdentity:
    if not isinstance(value, Mapping) or set(value) != {
        "filename",
        "sha256",
        "size_bytes",
    }:
        raise EvaluationRefusal(reason)
    filename = str(value.get("filename") or "")
    sha256 = str(value.get("sha256") or "").lower()
    size = value.get("size_bytes")
    if (
        not filename
        or Path(filename).name != filename
        or not _is_sha256(sha256)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
    ):
        raise EvaluationRefusal(reason)
    return FileIdentity(filename=filename, sha256=sha256, size_bytes=size)


def _same_identity(left: FileIdentity, right: FileIdentity) -> bool:
    return (
        left.filename == right.filename
        and left.size_bytes == right.size_bytes
        and hmac.compare_digest(left.sha256, right.sha256)
    )


def _validate_handoff_result(value: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(value)
    claimed = str(body.pop("handoff_body_sha256", "")).lower()
    inventory = body.get("capture_inventory")
    if (
        not _is_sha256(claimed)
        or not hmac.compare_digest(claimed, canonical_sha256(body))
        or body.get("schema_version") != handoff.HANDOFF_SCHEMA
        or body.get("strategy_id") != screen.STRATEGY_ID
        or body.get("strategy_version") != screen.STRATEGY_VERSION
        or body.get("config_id") != screen.CONFIG_ID
        or body.get("symbol_scope") != list(screen.MTVCLC_SYMBOLS)
        or body.get("source_contract_id") != screen.SOURCE_CONTRACT_ID
        or body.get("activity_metric_id") != screen.ACTIVITY_METRIC_ID
        or body.get("window_closed") is not True
        or body.get("manifest_and_chunks_verified") is not True
        or body.get("outcome_evaluation_performed") is not False
        or body.get("performance_statistics_computed") is not False
        or body.get("research_only") is not True
        or body.get("authority") != FALSE_AUTHORITY
        or not isinstance(inventory, Mapping)
        or body.get("capture_inventory_sha256") != canonical_sha256(inventory)
    ):
        raise EvaluationRefusal("verified_handoff_contract_invalid")
    return cast(dict[str, Any], inventory)


def _load_preregistration_payload(
    path: str | Path,
    *,
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
        if (
            not resolved.is_file()
            or resolved.is_symlink()
            or resolved.stat().st_size <= 0
            or resolved.stat().st_size > MAXIMUM_PREREGISTRATION_BYTES
        ):
            raise OSError("invalid preregistration")
        raw = resolved.read_bytes()
    except OSError as exc:
        raise EvaluationRefusal("preregistration_file_invalid_after_handoff") from exc
    try:
        payload = handoff._strict_json_object(
            raw, reason="preregistration_json_invalid_after_handoff"
        )
    except handoff.HandoffRefusal as exc:
        raise EvaluationRefusal(str(exc)) from exc
    body = dict(payload)
    claimed = str(body.pop("preregistration_body_sha256", "")).lower()
    if (
        not _is_sha256(claimed)
        or not hmac.compare_digest(claimed, canonical_sha256(body))
        or not hmac.compare_digest(
            claimed,
            str(inventory.get("preregistration_body_sha256") or "").lower(),
        )
        or not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(),
            str(inventory.get("preregistration_artifact_sha256") or "").lower(),
        )
    ):
        raise EvaluationRefusal("preregistration_changed_after_handoff")
    return cast(dict[str, Any], payload)


def _validate_sealed_source_identities(
    payload: Mapping[str, Any],
) -> tuple[dict[str, FileIdentity], FileIdentity, str]:
    raw = payload.get("source_identities")
    expected_fields = {
        "screen_source",
        "collector_source",
        "sealer_source",
        "scope_catalog_source",
        "cost_capture",
        "fee_attestation",
        "production_runtime_context",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected_fields:
        raise EvaluationRefusal("preregistration_source_identity_scope_invalid")

    named: dict[str, FileIdentity] = {}
    for field, filename in (
        ("screen_source", "screen_mt4_tick_volume_close_location_continuation.py"),
        ("collector_source", "capture_ig_mt4_m1_activity.py"),
        ("sealer_source", "seal_mt4_tick_volume_preregistration.py"),
        ("scope_catalog_source", "ig_mt4_catalog.py"),
    ):
        identity = _identity_row(raw.get(field), f"{field}_identity_invalid")
        if identity.filename != filename:
            raise EvaluationRefusal(f"{field}_identity_invalid")
        named[field] = identity

    cost = raw.get("cost_capture")
    if not isinstance(cost, Mapping) or set(cost) != {
        "capture_json",
        "capture_npz",
        "capture_payload_sha256",
        "capture_mode",
        "scope_version",
        "venue_id",
    }:
        raise EvaluationRefusal("sealed_cost_source_identity_invalid")
    capture_json = _identity_row(
        cost.get("capture_json"), "sealed_cost_source_identity_invalid"
    )
    capture_npz = _identity_row(
        cost.get("capture_npz"), "sealed_cost_source_identity_invalid"
    )
    if (
        not _is_sha256(cost.get("capture_payload_sha256"))
        or cost.get("capture_mode")
        not in {"live_endpoint", "authenticated_same_source_db_history"}
        or cost.get("scope_version") != handoff.SCOPE_VERSION
        or cost.get("venue_id") != handoff.VENUE_ID
    ):
        raise EvaluationRefusal("sealed_cost_source_identity_invalid")
    named["cost_capture_json"] = capture_json
    named["cost_capture_npz"] = capture_npz

    fee = raw.get("fee_attestation")
    if not isinstance(fee, Mapping) or set(fee) != {
        "attestation",
        "operator_attestation_sha256",
        "effective_at_utc",
        "attested_at_utc",
        "account_currency",
        "source_documents",
    }:
        raise EvaluationRefusal("sealed_fee_source_identity_invalid")
    attestation = _identity_row(
        fee.get("attestation"), "sealed_fee_source_identity_invalid"
    )
    account_currency = str(fee.get("account_currency") or "")
    documents = fee.get("source_documents")
    if (
        not _is_sha256(fee.get("operator_attestation_sha256"))
        or len(account_currency) != 3
        or not account_currency.isalpha()
        or account_currency != account_currency.upper()
        or not isinstance(documents, list)
        or len(documents) != 3
    ):
        raise EvaluationRefusal("sealed_fee_source_identity_invalid")
    document_roles = {
        "ig_mt4_forex_product_details",
        "ig_mt4_crypto_product_details",
        "ig_spread_betting_cfd_product_details",
    }
    observed_roles: set[str] = set()
    for item in documents:
        if not isinstance(item, Mapping) or set(item) != {
            "role",
            "url",
            "retrieved_at_utc",
            "filename",
            "sha256",
            "size_bytes",
        }:
            raise EvaluationRefusal("sealed_fee_source_identity_invalid")
        role = str(item.get("role") or "")
        _identity_row(
            {
                "filename": item.get("filename"),
                "sha256": item.get("sha256"),
                "size_bytes": item.get("size_bytes"),
            },
            "sealed_fee_source_identity_invalid",
        )
        if role not in document_roles or not str(item.get("url") or "").startswith(
            "https://www.ig.com/"
        ):
            raise EvaluationRefusal("sealed_fee_source_identity_invalid")
        observed_roles.add(role)
    if observed_roles != document_roles:
        raise EvaluationRefusal("sealed_fee_source_identity_invalid")
    named["fee_attestation"] = attestation

    runtime_context = raw.get("production_runtime_context")
    if (
        not isinstance(runtime_context, Mapping)
        or runtime_context.get("relationship")
        != "context_only_successor_not_integrated_or_authorized"
    ):
        raise EvaluationRefusal("production_context_not_advisory")
    return named, named["screen_source"], account_currency


def _validate_cost_policy(
    payload: Mapping[str, Any],
    *,
    cost_source_sha256: str,
    sealed_account_currency: str,
) -> dict[str, screen.MT4CostCalibration]:
    raw_policy = payload.get("cost_policy")
    if not isinstance(raw_policy, Mapping) or set(raw_policy) != {
        *EXPECTED_COST_POLICY,
        "symbols",
    }:
        raise EvaluationRefusal("sealed_cost_policy_scope_invalid")
    for field, expected in EXPECTED_COST_POLICY.items():
        if raw_policy.get(field) != expected:
            raise EvaluationRefusal("sealed_cost_policy_drift")
    rows = raw_policy.get("symbols")
    if not isinstance(rows, Mapping) or set(rows) != set(screen.MTVCLC_SYMBOLS):
        raise EvaluationRefusal("sealed_cost_symbol_scope_invalid")

    calibrations: dict[str, screen.MT4CostCalibration] = {}
    for symbol in screen.MTVCLC_SYMBOLS:
        row = rows.get(symbol)
        if not isinstance(row, Mapping) or set(row) != COST_ROW_FIELDS:
            raise EvaluationRefusal(f"sealed_cost_row_invalid:{symbol}")
        p90 = _finite(row.get("p90_ig_spread_bps"), "sealed_cost_numeric_invalid")
        commission = _finite(
            row.get("commission_bps_per_round_trip"),
            "sealed_cost_numeric_invalid",
        )
        financing = _finite(
            row.get("financing_bps_per_trade"), "sealed_cost_numeric_invalid"
        )
        adverse = _finite(
            row.get("fixed_adverse_execution_debit_bps"),
            "sealed_cost_numeric_invalid",
        )
        geometry = _finite(
            row.get("pre_conversion_geometry_cost_bps"),
            "sealed_cost_numeric_invalid",
        )
        conversion_rate = _finite(
            row.get("conversion_rate_of_absolute_profit_or_loss"),
            "sealed_cost_numeric_invalid",
        )
        screen_conversion = _finite(
            row.get("convert_on_close_charge_fraction_for_screen"),
            "sealed_cost_numeric_invalid",
        )
        p_star = _finite(
            row.get("conversion_adjusted_break_even_win_probability"),
            "sealed_cost_numeric_invalid",
        )
        account_currency = str(row.get("account_currency") or "")
        pnl_currency = str(row.get("profit_loss_currency") or "")
        conversion_applies = row.get("conversion_applies")
        expected_applies = pnl_currency != account_currency
        expected_screen_conversion = conversion_rate if expected_applies else 0.0
        if (
            p90 <= 0.0
            or commission < 0.0
            or financing < 0.0
            or adverse != screen.FIXED_ADVERSE_EXECUTION_DEBIT_BPS
            or not math.isclose(
                geometry,
                p90 + commission + financing + adverse,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or account_currency != sealed_account_currency
            or pnl_currency != symbol[3:]
            or conversion_rate
            != screen.IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION
            or conversion_applies is not expected_applies
            or not math.isclose(
                screen_conversion,
                expected_screen_conversion,
                rel_tol=0.0,
                abs_tol=1e-15,
            )
            or row.get("commission_status")
            not in {"explicit_source_attested", "conservative_upper_bound"}
            or row.get("financing_status")
            not in {
                "structurally_avoided_by_fixed_rollover_guard",
                "conservative_upper_bound",
            }
            or (
                financing == 0.0
                and row.get("financing_status")
                != "structurally_avoided_by_fixed_rollover_guard"
            )
            or row.get("conversion_status")
            != "debit_absolute_profit_or_loss_when_account_currency_differs"
        ):
            raise EvaluationRefusal(f"sealed_cost_row_drift:{symbol}")
        calibration = screen.MT4CostCalibration(
            symbol=symbol,
            p90_spread_bps=p90,
            commission_bps_per_round_trip=commission,
            financing_bps_per_trade=financing,
            account_currency=account_currency,
            pnl_currency=pnl_currency,
            convert_on_close_charge_fraction=screen_conversion,
            source_sha256=cost_source_sha256,
            adverse_execution_debit_bps=adverse,
        )
        if (
            not screen.validate_cost_calibration(
                calibration, expected_symbol=symbol
            )
            or not math.isclose(
                calibration.break_even_win_probability,
                p_star,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise EvaluationRefusal(f"sealed_cost_screen_parity_failed:{symbol}")
        calibrations[symbol] = calibration
    return calibrations


def _validate_preregistration_for_screen(
    payload: dict[str, Any],
) -> VerifiedPreregistration:
    named, sealed_screen_source, account_currency = (
        _validate_sealed_source_identities(payload)
    )
    actual_screen_path = Path(screen.__file__).resolve(strict=True)
    actual_screen_source = _identity(actual_screen_path)
    if not _same_identity(sealed_screen_source, actual_screen_source):
        raise EvaluationRefusal("frozen_screen_source_identity_drift")

    strategy = payload.get("strategy")
    gates = payload.get("fixed_success_gates")
    execution = payload.get("execution_contract")
    if not all(isinstance(item, Mapping) for item in (strategy, gates, execution)):
        raise EvaluationRefusal("preregistration_evaluation_contract_invalid")
    assert isinstance(strategy, Mapping)
    assert isinstance(gates, Mapping)
    assert isinstance(execution, Mapping)
    frozen_config = asdict(screen.GRID[0])
    attempt = screen.attempt_manifest()
    if (
        strategy.get("strategy_id") != screen.STRATEGY_ID
        or strategy.get("strategy_version") != screen.STRATEGY_VERSION
        or strategy.get("config_id") != screen.CONFIG_ID
        or strategy.get("config_sha256") != canonical_sha256(frozen_config)
        or strategy.get("source_contract_id") != screen.SOURCE_CONTRACT_ID
        or strategy.get("activity_metric_id") != screen.ACTIVITY_METRIC_ID
        or strategy.get("attempt_manifest") != attempt
        or strategy.get("attempt_manifest_sha256") != canonical_sha256(attempt)
        or execution.get("entry_type") != "immediate_market"
        or execution.get("pending_orders_forbidden") is not True
        or execution.get("maximum_entries_per_symbol_utc_day") != 1
        or gates.get("all_44_cells_must_pass") is not True
        or gates.get("minimum_trades_per_cell") != screen.MIN_TRADES_PER_CELL
        or gates.get("minimum_independent_utc_days_per_cell")
        != screen.MIN_INDEPENDENT_DAYS_PER_CELL
        or gates.get("minimum_total_trades") != MINIMUM_TOTAL_TRADES
        or gates.get("minimum_total_independent_utc_days")
        != MINIMUM_TOTAL_INDEPENDENT_DAYS
        or gates.get("source_scope_ready_required") is not True
        or gates.get("source_errors_required") != []
    ):
        raise EvaluationRefusal("preregistration_evaluation_contract_drift")
    calibrations = _validate_cost_policy(
        payload,
        cost_source_sha256=named["cost_capture_json"].sha256,
        sealed_account_currency=account_currency,
    )
    return VerifiedPreregistration(
        payload=payload,
        calibrations=calibrations,
        identities=named,
        screen_source=actual_screen_source,
    )


def _spool_header(*, schema: str, symbol: str, fields: tuple[str, ...]) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": schema,
            "symbol": symbol,
            "encoding": "network_byte_order_ieee754_binary64",
            "fields": list(fields),
            "audit_only_projection": (
                "source-event token and process-local event sequence remain in the "
                "fully authenticated capture; the frozen screen consumes only "
                "integer observation epoch and executable bid/ask"
            ),
        }
    ) + b"\n"


def _prepare_output_root(
    *, output_root: str | Path, capture_root: str | Path, preregistration: str | Path
) -> Path:
    root_candidate = Path(output_root).expanduser()
    if root_candidate.is_symlink() or handoff._is_reparse_point(root_candidate):
        raise EvaluationRefusal("evaluation_output_root_unsafe")
    root = root_candidate.resolve(strict=False)
    capture = Path(capture_root).expanduser().resolve(strict=True)
    prereg = Path(preregistration).expanduser().resolve(strict=True)
    if root == Path(root.anchor) or root in {capture, prereg, prereg.parent}:
        raise EvaluationRefusal("evaluation_output_root_unsafe")
    if root.is_relative_to(capture) or capture.is_relative_to(root):
        raise EvaluationRefusal("evaluation_output_overlaps_capture")
    if prereg.parent.is_relative_to(root) or root.is_relative_to(prereg.parent):
        raise EvaluationRefusal("evaluation_output_overlaps_preregistration")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EvaluationRefusal("evaluation_output_root_unavailable") from exc
    if not root.is_dir() or root.is_symlink():
        raise EvaluationRefusal("evaluation_output_root_invalid")
    return root


def _preflight_spool_capacity(root: Path, inventory: Mapping[str, Any]) -> int:
    bar_rows = _strict_int(inventory.get("bar_rows"), "handoff_row_count_invalid")
    quote_rows = _strict_int(
        inventory.get("quote_rows"), "handoff_row_count_invalid"
    )
    projected = bar_rows * BAR_STRUCT.size + quote_rows * QUOTE_STRUCT.size
    projected += len(screen.MTVCLC_SYMBOLS) * 2 * MAXIMUM_SPOOL_HEADER_BYTES
    try:
        free = shutil.disk_usage(root).free
    except OSError as exc:
        raise EvaluationRefusal("evaluation_disk_capacity_unknown") from exc
    required = projected + max(OUTPUT_HEADROOM_BYTES, projected // 10)
    if free < required:
        raise EvaluationRefusal("evaluation_disk_capacity_insufficient")
    return projected


def _chunk_path(
    *, capture_root: Path, relative: str, sequence: int, utc_hour: str
) -> Path:
    expected = PurePosixPath(
        handoff.CHUNK_DIRECTORY,
        utc_hour,
        f"ig-mt4-m1-activity-s0001-q{sequence:010d}.json",
    ).as_posix()
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or relative != expected:
        raise EvaluationRefusal("capture_changed_during_projection")
    candidate = capture_root.joinpath(*pure.parts)
    if candidate.is_symlink() or handoff._is_reparse_point(candidate):
        raise EvaluationRefusal("capture_changed_during_projection")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise EvaluationRefusal("capture_changed_during_projection") from exc
    if (
        not resolved.is_relative_to(capture_root)
        or not resolved.is_file()
        or resolved.is_symlink()
    ):
        raise EvaluationRefusal("capture_changed_during_projection")
    return resolved


def _spool_verified_capture(
    *,
    capture_root: str | Path,
    staging_root: Path,
    inventory: Mapping[str, Any],
) -> dict[str, SymbolSpools]:
    """Project only after the full handoff pass, rechecking every content hash."""

    try:
        root = Path(capture_root).expanduser().resolve(strict=True)
        manifest = (root / handoff.MANIFEST_FILENAME).resolve(strict=True)
    except OSError as exc:
        raise EvaluationRefusal("capture_changed_after_handoff") from exc
    if not root.is_dir() or not manifest.is_file() or manifest.is_symlink():
        raise EvaluationRefusal("capture_changed_after_handoff")

    handles: dict[tuple[str, str], Any] = {}
    paths: dict[tuple[str, str], Path] = {}
    digests: dict[tuple[str, str], Any] = {}
    counts = {
        (symbol, kind): 0
        for symbol in screen.MTVCLC_SYMBOLS
        for kind in ("bars", "quotes")
    }
    try:
        for symbol in screen.MTVCLC_SYMBOLS:
            for kind, schema, fields in (
                ("bars", BAR_SPOOL_SCHEMA, BAR_FIELDS),
                ("quotes", QUOTE_SPOOL_SCHEMA, QUOTE_FIELDS),
            ):
                path = staging_root / f".{symbol.lower()}-{kind}.spool"
                handle = path.open("xb")
                header = _spool_header(schema=schema, symbol=symbol, fields=fields)
                handle.write(header)
                handles[(symbol, kind)] = handle
                paths[(symbol, kind)] = path
                digest = hashlib.sha256()
                digest.update(header)
                digests[(symbol, kind)] = digest

        manifest_digest = hashlib.sha256()
        entries = 0
        previous_hash = handoff.ZERO_SHA256
        try:
            manifest_handle = manifest.open("rb")
        except OSError as exc:
            raise EvaluationRefusal("capture_changed_after_handoff") from exc
        with manifest_handle:
            for raw_line in manifest_handle:
                if (
                    not raw_line.endswith(b"\n")
                    or len(raw_line) > handoff.MAXIMUM_MANIFEST_LINE_BYTES
                ):
                    raise EvaluationRefusal("capture_changed_during_projection")
                manifest_digest.update(raw_line)
                try:
                    entry = handoff._strict_json_object(
                        raw_line[:-1], reason="capture_changed_during_projection"
                    )
                except handoff.HandoffRefusal as exc:
                    raise EvaluationRefusal(str(exc)) from exc
                entries += 1
                sequence = _strict_int(
                    entry.get("sequence"),
                    "capture_changed_during_projection",
                    minimum=1,
                )
                entry_body = dict(entry)
                claimed_entry_hash = str(
                    entry_body.pop("manifest_entry_sha256", "")
                ).lower()
                utc_hour = str(entry.get("utc_hour") or "")
                if (
                    set(entry) != handoff.MANIFEST_FIELDS
                    or raw_line != canonical_json_bytes(entry) + b"\n"
                    or sequence != entries
                    or entry.get("previous_entry_sha256") != previous_hash
                    or not _is_sha256(claimed_entry_hash)
                    or not hmac.compare_digest(
                        claimed_entry_hash, canonical_sha256(entry_body)
                    )
                    or entry.get("segment_index") != 1
                ):
                    raise EvaluationRefusal("capture_changed_during_projection")
                chunk_path = _chunk_path(
                    capture_root=root,
                    relative=str(entry.get("chunk_path") or ""),
                    sequence=sequence,
                    utc_hour=utc_hour,
                )
                try:
                    chunk_raw = chunk_path.read_bytes()
                except OSError as exc:
                    raise EvaluationRefusal(
                        "capture_changed_during_projection"
                    ) from exc
                if (
                    len(chunk_raw) != entry.get("chunk_size_bytes")
                    or not hmac.compare_digest(
                        hashlib.sha256(chunk_raw).hexdigest(),
                        str(entry.get("chunk_sha256") or "").lower(),
                    )
                ):
                    raise EvaluationRefusal("capture_changed_during_projection")
                try:
                    chunk = handoff._strict_json_object(
                        chunk_raw, reason="capture_changed_during_projection"
                    )
                except handoff.HandoffRefusal as exc:
                    raise EvaluationRefusal(str(exc)) from exc
                if (
                    set(chunk) != handoff.CHUNK_FIELDS
                    or chunk_raw != canonical_json_bytes(chunk) + b"\n"
                    or chunk.get("symbol_scope") != list(screen.MTVCLC_SYMBOLS)
                    or chunk.get("source_contract_id") != screen.SOURCE_CONTRACT_ID
                    or chunk.get("activity_metric_id") != screen.ACTIVITY_METRIC_ID
                    or chunk.get("collection_only") is not True
                    or chunk.get("evaluation_performed") is not False
                    or chunk.get("order_authorized") is not False
                ):
                    raise EvaluationRefusal("capture_changed_during_projection")
                bars = chunk.get("bars")
                quotes = chunk.get("quotes")
                if not isinstance(bars, list) or not isinstance(quotes, list):
                    raise EvaluationRefusal("capture_changed_during_projection")
                for raw in bars:
                    if not isinstance(raw, Mapping):
                        raise EvaluationRefusal("capture_changed_during_projection")
                    symbol = str(raw.get("symbol") or "")
                    if symbol not in screen.MTVCLC_SYMBOLS:
                        raise EvaluationRefusal("capture_changed_during_projection")
                    record = BAR_STRUCT.pack(
                        _strict_int(
                            raw.get("minute_epoch"),
                            "capture_changed_during_projection",
                            minimum=1,
                            maximum=2**63 - 1,
                        ),
                        _finite(raw.get("bid_open"), "capture_changed_during_projection"),
                        _finite(raw.get("bid_high"), "capture_changed_during_projection"),
                        _finite(raw.get("bid_low"), "capture_changed_during_projection"),
                        _finite(raw.get("bid_close"), "capture_changed_during_projection"),
                        _strict_int(
                            raw.get("tick_volume"),
                            "capture_changed_during_projection",
                            minimum=0,
                            maximum=2**63 - 1,
                        ),
                    )
                    handles[(symbol, "bars")].write(record)
                    digests[(symbol, "bars")].update(record)
                    counts[(symbol, "bars")] += 1
                for raw in quotes:
                    if not isinstance(raw, Mapping):
                        raise EvaluationRefusal("capture_changed_during_projection")
                    symbol = str(raw.get("symbol") or "")
                    if symbol not in screen.MTVCLC_SYMBOLS:
                        raise EvaluationRefusal("capture_changed_during_projection")
                    record = QUOTE_STRUCT.pack(
                        _strict_int(
                            raw.get("observation_epoch"),
                            "capture_changed_during_projection",
                            minimum=1,
                            maximum=2**63 - 1,
                        ),
                        _finite(raw.get("bid"), "capture_changed_during_projection"),
                        _finite(raw.get("ask"), "capture_changed_during_projection"),
                    )
                    handles[(symbol, "quotes")].write(record)
                    digests[(symbol, "quotes")].update(record)
                    counts[(symbol, "quotes")] += 1
                previous_hash = claimed_entry_hash

        if (
            manifest_digest.hexdigest() != inventory.get("manifest_sha256")
            or previous_hash != inventory.get("manifest_head_sha256")
            or entries != inventory.get("manifest_entries")
        ):
            raise EvaluationRefusal("capture_changed_during_projection")
        expected_bars = inventory.get("bar_rows_by_symbol")
        expected_quotes = inventory.get("quote_rows_by_symbol")
        if not isinstance(expected_bars, Mapping) or not isinstance(
            expected_quotes, Mapping
        ):
            raise EvaluationRefusal("verified_handoff_row_inventory_invalid")
        for symbol in screen.MTVCLC_SYMBOLS:
            if (
                counts[(symbol, "bars")] != expected_bars.get(symbol)
                or counts[(symbol, "quotes")] != expected_quotes.get(symbol)
            ):
                raise EvaluationRefusal("capture_projection_row_count_mismatch")
        for handle in handles.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        handles.clear()

        result: dict[str, SymbolSpools] = {}
        for symbol in screen.MTVCLC_SYMBOLS:
            files: dict[str, SpoolFile] = {}
            for kind, record_struct, schema, fields in (
                ("bars", BAR_STRUCT, BAR_SPOOL_SCHEMA, BAR_FIELDS),
                ("quotes", QUOTE_STRUCT, QUOTE_SPOOL_SCHEMA, QUOTE_FIELDS),
            ):
                path = paths[(symbol, kind)]
                digest = digests[(symbol, kind)].hexdigest()
                target = staging_root / f"{symbol.lower()}_{kind}_{digest}.bin"
                if target.exists():
                    raise EvaluationRefusal("spool_content_address_collision")
                path.rename(target)
                files[kind] = SpoolFile(
                    path=target,
                    identity=FileIdentity(
                        filename=target.name,
                        sha256=digest,
                        size_bytes=target.stat().st_size,
                    ),
                    records=counts[(symbol, kind)],
                    record_size_bytes=record_struct.size,
                    schema_version=schema,
                    fields=fields,
                )
            source_identity = canonical_sha256(
                {
                    "schema_version": SPOOL_INVENTORY_SCHEMA,
                    "symbol": symbol,
                    "market_source_id": inventory.get("market_source_id"),
                    "capture_manifest_sha256": inventory.get("manifest_sha256"),
                    "bar_spool_sha256": files["bars"].identity.sha256,
                    "quote_spool_sha256": files["quotes"].identity.sha256,
                    "projection": "epoch_bid_ask_and_direct_bid_m1_ivolume_only",
                }
            )
            result[symbol] = SymbolSpools(
                symbol=symbol,
                bars=files["bars"],
                quotes=files["quotes"],
                screen_input_sha256=source_identity,
            )
        return result
    finally:
        for handle in handles.values():
            try:
                handle.close()
            except OSError:
                pass


RecordT = TypeVar("RecordT")


class _BinarySequence(Sequence[RecordT], Generic[RecordT]):
    def __init__(self, spool: SpoolFile, symbol: str) -> None:
        self._spool = spool
        self._symbol = symbol

    def __len__(self) -> int:
        return self._spool.records

    def _iter_records(self) -> Iterator[RecordT]:
        expected_header = _spool_header(
            schema=self._spool.schema_version,
            symbol=self._symbol,
            fields=self._spool.fields,
        )
        digest = hashlib.sha256()
        count = 0
        try:
            handle = self._spool.path.open("rb")
        except OSError as exc:
            raise EvaluationRefusal("screen_input_spool_unreadable") from exc
        with handle:
            header = handle.readline(MAXIMUM_SPOOL_HEADER_BYTES + 1)
            digest.update(header)
            if header != expected_header:
                raise EvaluationRefusal("screen_input_spool_header_invalid")
            while True:
                raw = handle.read(self._spool.record_size_bytes)
                if not raw:
                    break
                if len(raw) != self._spool.record_size_bytes:
                    raise EvaluationRefusal("screen_input_spool_truncated")
                digest.update(raw)
                count += 1
                yield self._decode(raw)
        if (
            count != self._spool.records
            or not hmac.compare_digest(
                digest.hexdigest(), self._spool.identity.sha256
            )
        ):
            raise EvaluationRefusal("screen_input_spool_identity_invalid")

    def __iter__(self) -> Iterator[RecordT]:
        return self._iter_records()

    def _decode(self, raw: bytes) -> RecordT:
        raise NotImplementedError

    @overload
    def __getitem__(self, index: int) -> RecordT: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[RecordT]: ...

    def __getitem__(self, index: int | slice) -> RecordT | Sequence[RecordT]:
        if isinstance(index, slice):
            return tuple(self)[index]
        normalized = index if index >= 0 else len(self) + index
        if normalized < 0 or normalized >= len(self):
            raise IndexError(index)
        for offset, value in enumerate(self):
            if offset == normalized:
                return value
        raise IndexError(index)


class _BarSequence(_BinarySequence[screen.MT4BidBar]):
    def _decode(self, raw: bytes) -> screen.MT4BidBar:
        epoch, open_px, high, low, close, volume = BAR_STRUCT.unpack(raw)
        return screen.MT4BidBar(
            epoch=epoch,
            bid_open=open_px,
            bid_high=high,
            bid_low=low,
            bid_close=close,
            tick_volume=volume,
        )


class _QuoteSequence(_BinarySequence[screen.MT4Quote]):
    def _decode(self, raw: bytes) -> screen.MT4Quote:
        epoch, bid, ask = QUOTE_STRUCT.unpack(raw)
        # Audit-only event-token fields remain authenticated in the capture and
        # are bound through the manifest and spool inventory.  The frozen screen
        # consumes only this exact integer observation epoch and bid/ask pair.
        return screen.MT4Quote(epoch=epoch, bid=bid, ask=ask)


ValueT = TypeVar("ValueT")


class _LazySpoolMapping(Mapping[str, Sequence[ValueT]], Generic[ValueT]):
    def __init__(
        self,
        spools: Mapping[str, SymbolSpools],
        *,
        kind: str,
    ) -> None:
        self._spools = spools
        self._kind = kind

    def __iter__(self) -> Iterator[str]:
        return iter(screen.MTVCLC_SYMBOLS)

    def __len__(self) -> int:
        return len(screen.MTVCLC_SYMBOLS)

    def __getitem__(self, symbol: str) -> Sequence[ValueT]:
        spool = self._spools[symbol]
        if self._kind == "bars":
            return cast(Sequence[ValueT], _BarSequence(spool.bars, symbol))
        return cast(Sequence[ValueT], _QuoteSequence(spool.quotes, symbol))


def _float_close(left: Any, right: float) -> bool:
    try:
        value = float(left)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(value) and math.isclose(
        value, right, rel_tol=0.0, abs_tol=1e-12
    )


def _wilson_lower(wins: int, trials: int) -> float:
    if trials <= 0:
        return 0.0
    alpha = (1.0 - screen.WIN_PROBABILITY_FAMILY_CONFIDENCE) / 44.0
    z = NormalDist().inv_cdf(1.0 - alpha)
    point = wins / trials
    z_sq = z * z
    denominator = 1.0 + z_sq / trials
    center = point + z_sq / (2.0 * trials)
    radius = z * math.sqrt(
        point * (1.0 - point) / trials + z_sq / (4.0 * trials * trials)
    )
    return max(0.0, (center - radius) / denominator)


def _validate_and_summarize_result(
    *,
    result: Mapping[str, Any],
    calibrations: Mapping[str, screen.MT4CostCalibration],
    source_hashes: Mapping[str, str],
) -> dict[str, Any]:
    if not screen.validate_result_bundle(result):
        raise EvaluationRefusal("screen_result_bundle_invalid")
    if result.get("source_sha256_by_symbol") != dict(source_hashes):
        raise EvaluationRefusal("screen_result_source_identity_mismatch")
    expected_costs = {
        symbol: asdict(calibrations[symbol]) for symbol in screen.MTVCLC_SYMBOLS
    }
    if result.get("costs") != expected_costs:
        raise EvaluationRefusal("screen_result_cost_identity_mismatch")

    reservations = result.get("reservation_ledger")
    outcomes = result.get("outcome_ledger")
    cells = result.get("cells")
    if not isinstance(reservations, list) or not isinstance(outcomes, list):
        raise EvaluationRefusal("screen_ledgers_invalid")
    if not isinstance(cells, list) or len(cells) != 44:
        raise EvaluationRefusal("screen_cell_scope_invalid")
    if len(reservations) != len(outcomes):
        raise EvaluationRefusal("screen_ledger_cardinality_mismatch")

    reservation_keys: list[tuple[str, str, int, str]] = []
    for row in reservations:
        if (
            not isinstance(row, Mapping)
            or set(row) != RESERVATION_FIELDS
            or row.get("config_id") != screen.CONFIG_ID
            or row.get("entry_status")
            not in {"admitted", "contemporaneous_entry_quote_missing"}
        ):
            raise EvaluationRefusal("screen_reservation_ledger_invalid")
        signal_epoch = _strict_int(
            row.get("signal_epoch"),
            "screen_reservation_ledger_invalid",
            minimum=1,
        )
        expected_entry_epoch = _strict_int(
            row.get("expected_entry_epoch"),
            "screen_reservation_ledger_invalid",
            minimum=1,
        )
        entry_day = str(row.get("entry_day") or "")
        if (
            expected_entry_epoch != signal_epoch + 60
            or entry_day
            != datetime.fromtimestamp(expected_entry_epoch, tz=UTC)
            .date()
            .isoformat()
        ):
            raise EvaluationRefusal("screen_reservation_ledger_invalid")
        key = (
            str(row.get("symbol") or ""),
            str(row.get("side") or ""),
            signal_epoch,
            entry_day,
        )
        reservation_keys.append(key)
    outcome_keys: list[tuple[str, str, int, str]] = []
    for row in outcomes:
        if (
            not isinstance(row, Mapping)
            or set(row) != OUTCOME_FIELDS
            or row.get("config_id") != screen.CONFIG_ID
        ):
            raise EvaluationRefusal("screen_outcome_ledger_invalid")
        key = (
            str(row.get("symbol") or ""),
            str(row.get("side") or ""),
            _strict_int(
                row.get("signal_epoch"),
                "screen_outcome_ledger_invalid",
                minimum=1,
            ),
            str(row.get("entry_day") or ""),
        )
        if key[0] not in screen.MTVCLC_SYMBOLS or key[1] not in SIDES or not key[3]:
            raise EvaluationRefusal("screen_outcome_ledger_invalid")
        if not isinstance(row.get("full_target_hit_first"), bool):
            raise EvaluationRefusal("screen_outcome_ledger_invalid")
        calibration = calibrations.get(key[0])
        if calibration is None:
            raise EvaluationRefusal("screen_outcome_cost_math_invalid")
        gross = _finite(row.get("gross_quote_bps"), "screen_outcome_ledger_invalid")
        recorded_cost = _finite(
            row.get("recorded_cost_bps"), "screen_outcome_ledger_invalid"
        )
        conversion_debit = _finite(
            row.get("currency_conversion_debit_bps"),
            "screen_outcome_ledger_invalid",
        )
        net = _finite(row.get("net_bps"), "screen_outcome_ledger_invalid")
        expected_conversion_debit = (
            abs(gross) * calibration.convert_on_close_charge_fraction
        )
        if (
            not _float_close(recorded_cost, calibration.recorded_cost_bps)
            or not _float_close(conversion_debit, expected_conversion_debit)
            or not _float_close(net, gross - recorded_cost - conversion_debit)
            or (
                row.get("full_target_hit_first") is True
                and (
                    row.get("exit_reason") != "TAKE_PROFIT"
                    or not _float_close(
                        gross,
                        screen.TARGET_COST_MULTIPLE
                        * calibration.recorded_cost_bps,
                    )
                )
            )
        ):
            raise EvaluationRefusal("screen_outcome_cost_math_invalid")
        outcome_keys.append(key)
    if (
        reservation_keys != outcome_keys
        or len(set(outcome_keys)) != len(outcome_keys)
        or len({(symbol, day) for symbol, _side, _epoch, day in outcome_keys})
        != len(outcome_keys)
    ):
        raise EvaluationRefusal("screen_ledgers_incomplete_or_duplicate")
    for reservation, outcome in zip(reservations, outcomes, strict=True):
        assert isinstance(reservation, Mapping)
        assert isinstance(outcome, Mapping)
        missing_entry = (
            reservation.get("entry_status")
            == "contemporaneous_entry_quote_missing"
        )
        if missing_entry and not (
            outcome.get("entry_epoch") is None
            and outcome.get("exit_reason") == "ENTRY_QUOTE_MISSING"
        ):
            raise EvaluationRefusal("screen_entry_outcome_ledger_mismatch")
        if not missing_entry and (
            outcome.get("entry_epoch") is None
            or outcome.get("exit_reason") == "ENTRY_QUOTE_MISSING"
        ):
            raise EvaluationRefusal("screen_entry_outcome_ledger_mismatch")
        if not missing_entry:
            expected_entry = int(reservation["expected_entry_epoch"])
            actual_entry = _strict_int(
                outcome.get("entry_epoch"),
                "screen_entry_outcome_ledger_mismatch",
                minimum=1,
            )
            if not expected_entry <= actual_entry <= (
                expected_entry + screen.MAX_ENTRY_DELAY_SECONDS
            ):
                raise EvaluationRefusal("screen_entry_outcome_ledger_mismatch")

    expected_order = [
        (screen.CONFIG_ID, symbol, side)
        for symbol in screen.MTVCLC_SYMBOLS
        for side in SIDES
    ]
    observed_order: list[tuple[str, str, str]] = []
    cell_summaries: list[dict[str, Any]] = []
    cell_passes: list[bool] = []
    for cell in cells:
        if not isinstance(cell, Mapping):
            raise EvaluationRefusal("screen_cell_invalid")
        symbol = str(cell.get("symbol") or "")
        side = str(cell.get("side") or "")
        observed_order.append((str(cell.get("config_id") or ""), symbol, side))
        selected = [
            row
            for row in outcomes
            if isinstance(row, Mapping)
            and row.get("symbol") == symbol
            and row.get("side") == side
        ]
        trials = len(selected)
        wins = sum(bool(row["full_target_hit_first"]) for row in selected)
        days = len({str(row["entry_day"]) for row in selected})
        mean_net = (
            sum(float(row["net_bps"]) for row in selected) / trials
            if trials
            else 0.0
        )
        wilson = _wilson_lower(wins, trials)
        calibration = calibrations.get(symbol)
        if calibration is None:
            raise EvaluationRefusal("screen_cell_cost_missing")
        p_star = calibration.break_even_win_probability
        source_ready = cell.get("source_ready") is True
        expected_pass = bool(
            source_ready
            and trials >= screen.MIN_TRADES_PER_CELL
            and days >= screen.MIN_INDEPENDENT_DAYS_PER_CELL
            and wilson > p_star
            and mean_net > 0.0
        )
        if (
            cell.get("reservations") != trials
            or cell.get("wins") != wins
            or cell.get("independent_days") != days
            or not _float_close(
                cell.get("full_target_rate"), wins / trials if trials else 0.0
            )
            or not _float_close(cell.get("win_probability_wilson_lower"), wilson)
            or not _float_close(cell.get("base_break_even_probability"), p_star)
            or not _float_close(cell.get("mean_net_bps"), mean_net)
            or cell.get("passes_fixed_cell_screen") is not expected_pass
        ):
            raise EvaluationRefusal("screen_cell_ledger_or_gate_mismatch")
        cell_passes.append(expected_pass)
        cell_summaries.append(
            {
                "config_id": screen.CONFIG_ID,
                "symbol": symbol,
                "side": side,
                "trades": trials,
                "independent_utc_days": days,
                "wins": wins,
                "win_probability_wilson_lower": wilson,
                "conversion_adjusted_break_even_probability": p_star,
                "conversion_adjusted_mean_net_bps": mean_net,
                "passes_fixed_cell_screen": expected_pass,
            }
        )
    if observed_order != expected_order:
        raise EvaluationRefusal("screen_cell_order_invalid")

    all_cells_pass = all(cell_passes)
    if result.get("all_cells_pass_fixed_screen") is not all_cells_pass:
        raise EvaluationRefusal("screen_aggregate_cell_gate_mismatch")
    total_trades = len(outcomes)
    total_days = len({key[3] for key in outcome_keys})
    source_scope_ready = result.get("source_scope_ready") is True
    source_errors = result.get("source_errors")
    if not isinstance(source_errors, list):
        raise EvaluationRefusal("screen_source_errors_invalid")
    panel_trade_gate = total_trades >= MINIMUM_TOTAL_TRADES
    panel_day_gate = total_days >= MINIMUM_TOTAL_INDEPENDENT_DAYS
    all_gates_pass = bool(
        source_scope_ready
        and source_errors == []
        and all_cells_pass
        and panel_trade_gate
        and panel_day_gate
    )
    return {
        "schema_version": GATE_SUMMARY_SCHEMA,
        "strategy_id": screen.STRATEGY_ID,
        "strategy_version": screen.STRATEGY_VERSION,
        "config_id": screen.CONFIG_ID,
        "execution_contract": {
            "entry_type": "immediate_market",
            "sides": ["BUY", "SELL"],
            "pending_orders_forbidden": True,
        },
        "fixed_cell_gates": {
            "required_cells": 44,
            "minimum_trades_per_cell": screen.MIN_TRADES_PER_CELL,
            "minimum_independent_utc_days_per_cell": (
                screen.MIN_INDEPENDENT_DAYS_PER_CELL
            ),
            "win_probability_interval": (
                "one_sided_wilson_family_adjusted_over_44_cells"
            ),
            "lower_bound_must_strictly_exceed_conversion_adjusted_p_star": True,
            "mean_conversion_adjusted_net_bps_must_be_strictly_positive": True,
            "all_44_cells_pass": all_cells_pass,
        },
        "panel_gates": {
            "total_trades": total_trades,
            "minimum_total_trades": MINIMUM_TOTAL_TRADES,
            "minimum_total_trades_pass": panel_trade_gate,
            "independent_utc_days": total_days,
            "minimum_independent_utc_days": MINIMUM_TOTAL_INDEPENDENT_DAYS,
            "minimum_independent_utc_days_pass": panel_day_gate,
        },
        "source_gates": {
            "source_scope_ready": source_scope_ready,
            "source_errors": source_errors,
            "source_gate_pass": source_scope_ready and source_errors == [],
        },
        "cells": cell_summaries,
        "all_fixed_research_gates_pass": all_gates_pass,
        "research_only": True,
        "authority": dict(FALSE_AUTHORITY),
    }


def _spool_inventory_payload(
    *, spools: Mapping[str, SymbolSpools], handoff_result: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": SPOOL_INVENTORY_SCHEMA,
        "projection_semantics": {
            "bars": "exact MT4 bid M1 OHLC and integer iVolume",
            "quotes": "exact integer observation epoch and executable bid/ask",
            "audit_only_quote_fields_omitted_from_binary_spool": [
                "source_event_token_sha256",
                "market_event_sequence",
            ],
            "omitted_fields_remain_bound_by": [
                "verified_capture_manifest_sha256",
                "verified_chunk_sha256_values",
                "handoff_body_sha256",
            ],
        },
        "handoff_body_sha256": handoff_result["handoff_body_sha256"],
        "symbols": {
            symbol: {
                "screen_input_sha256": spools[symbol].screen_input_sha256,
                "bars": {
                    **spools[symbol].bars.identity.as_dict(),
                    "records": spools[symbol].bars.records,
                    "record_size_bytes": spools[symbol].bars.record_size_bytes,
                    "schema_version": spools[symbol].bars.schema_version,
                    "fields": list(spools[symbol].bars.fields),
                },
                "quotes": {
                    **spools[symbol].quotes.identity.as_dict(),
                    "records": spools[symbol].quotes.records,
                    "record_size_bytes": spools[symbol].quotes.record_size_bytes,
                    "schema_version": spools[symbol].quotes.schema_version,
                    "fields": list(spools[symbol].quotes.fields),
                },
            }
            for symbol in screen.MTVCLC_SYMBOLS
        },
        "research_only": True,
        "authority": dict(FALSE_AUTHORITY),
    }


def _write_content_addressed_json(
    staging: Path, *, prefix: str, value: Mapping[str, Any]
) -> FileIdentity:
    raw = canonical_json_bytes(value) + b"\n"
    digest = hashlib.sha256(raw).hexdigest()
    target = staging / f"{prefix}_{digest}.json"
    if target.exists():
        raise EvaluationRefusal("evaluation_artifact_collision")
    try:
        with target.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise EvaluationRefusal("evaluation_artifact_write_failed") from exc
    return FileIdentity(target.name, digest, len(raw))


def _verify_staged_content_address(path: Path) -> None:
    if path.is_symlink() or handoff._is_reparse_point(path) or not path.is_file():
        raise EvaluationRefusal("evaluation_staged_artifact_invalid")
    claimed = path.stem.rsplit("_", 1)[-1].lower()
    if not _is_sha256(claimed):
        raise EvaluationRefusal("evaluation_staged_artifact_name_invalid")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(8 * 1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise EvaluationRefusal("evaluation_staged_artifact_unreadable") from exc
    if not hmac.compare_digest(digest.hexdigest(), claimed):
        raise EvaluationRefusal("evaluation_staged_artifact_identity_invalid")


def _publish_bundle(staging: Path, output_root: Path, manifest: FileIdentity) -> Path:
    target = output_root / f"mtvclc_evaluation_{manifest.sha256}"
    if target.exists():
        raise EvaluationRefusal("evaluation_output_exists")
    try:
        paths = list(staging.iterdir())
        if not paths or staging / manifest.filename not in paths:
            raise EvaluationRefusal("evaluation_manifest_missing_before_publish")
        for path in paths:
            _verify_staged_content_address(path)
        for path in paths:
            path.chmod(stat.S_IREAD)
        staging.rename(target)
    except EvaluationRefusal:
        raise
    except OSError as exc:
        raise EvaluationRefusal("evaluation_atomic_publish_failed") from exc
    return target


def evaluate_capture(
    *,
    preregistration_path: str | Path,
    capture_root: str | Path,
    output_root: str | Path,
) -> dict[str, Any]:
    """Authenticate, evaluate once, and publish immutable research evidence."""

    # This call must remain first: it refuses before end without touching the
    # capture root, and it authenticates every capture byte before outcome use.
    try:
        handoff_result = handoff.verify_capture_handoff(
            preregistration_path=preregistration_path,
            capture_root=capture_root,
        )
    except handoff.HandoffRefusal as exc:
        raise EvaluationRefusal(str(exc)) from exc
    inventory = _validate_handoff_result(handoff_result)
    payload = _load_preregistration_payload(
        preregistration_path, inventory=inventory
    )
    prereg = _validate_preregistration_for_screen(payload)
    root = _prepare_output_root(
        output_root=output_root,
        capture_root=capture_root,
        preregistration=preregistration_path,
    )
    projected_spool_bytes = _preflight_spool_capacity(root, inventory)
    evaluator_source = _identity(Path(__file__).resolve(strict=True))
    verifier_source = _identity(Path(handoff.__file__).resolve(strict=True))

    try:
        staging = Path(tempfile.mkdtemp(prefix=".mtvclc-eval-", dir=root))
    except OSError as exc:
        raise EvaluationRefusal("evaluation_staging_create_failed") from exc
    published = False
    try:
        spools = _spool_verified_capture(
            capture_root=capture_root,
            staging_root=staging,
            inventory=inventory,
        )
        source_hashes = {
            symbol: spools[symbol].screen_input_sha256
            for symbol in screen.MTVCLC_SYMBOLS
        }
        bars = _LazySpoolMapping[screen.MT4BidBar](spools, kind="bars")
        quotes = _LazySpoolMapping[screen.MT4Quote](spools, kind="quotes")

        # Exactly one frozen-screen invocation.  A refusal or exception is not
        # retried, and the sealed window is never extended or restarted.
        screen_result = screen.screen_universe(
            bars_by_symbol=bars,
            quotes_by_symbol=quotes,
            costs_by_symbol=prereg.calibrations,
            source_sha256_by_symbol=source_hashes,
            source_contract_id=screen.SOURCE_CONTRACT_ID,
        )
        gate_summary = _validate_and_summarize_result(
            result=screen_result,
            calibrations=prereg.calibrations,
            source_hashes=source_hashes,
        )
        spool_inventory = _spool_inventory_payload(
            spools=spools, handoff_result=handoff_result
        )
        screen_identity = _write_content_addressed_json(
            staging, prefix="mtvclc_screen_result", value=screen_result
        )
        summary_with_bindings = {
            **gate_summary,
            "bindings": {
                "preregistration_body_sha256": inventory[
                    "preregistration_body_sha256"
                ],
                "preregistration_artifact_sha256": inventory[
                    "preregistration_artifact_sha256"
                ],
                "handoff_body_sha256": handoff_result["handoff_body_sha256"],
                "capture_inventory_sha256": handoff_result[
                    "capture_inventory_sha256"
                ],
                "capture_manifest_sha256": inventory["manifest_sha256"],
                "capture_manifest_head_sha256": inventory[
                    "manifest_head_sha256"
                ],
                "screen_source_sha256": prereg.screen_source.sha256,
                "evaluator_source_sha256": evaluator_source.sha256,
                "handoff_verifier_source_sha256": verifier_source.sha256,
                "screen_result_sha256": screen_identity.sha256,
                "spool_inventory_sha256": canonical_sha256(spool_inventory),
            },
        }
        summary_identity = _write_content_addressed_json(
            staging, prefix="mtvclc_gate_summary", value=summary_with_bindings
        )
        evaluation_manifest: dict[str, Any] = {
            "schema_version": EVALUATION_SCHEMA,
            "strategy_id": screen.STRATEGY_ID,
            "strategy_version": screen.STRATEGY_VERSION,
            "config_id": screen.CONFIG_ID,
            "symbol_scope": list(screen.MTVCLC_SYMBOLS),
            "execution_contract": {
                "entry_type": "immediate_market",
                "pending_orders_forbidden": True,
            },
            "window": {
                "t0_utc_inclusive": inventory[
                    "prospective_t0_utc_inclusive"
                ],
                "end_utc_exclusive": inventory[
                    "prospective_end_utc_exclusive"
                ],
                "extended_or_restarted": False,
            },
            "bindings": summary_with_bindings["bindings"],
            "source_files": {
                "screen": prereg.screen_source.as_dict(),
                "evaluator": evaluator_source.as_dict(),
                "handoff_verifier": verifier_source.as_dict(),
                "sealed_collector": prereg.identities[
                    "collector_source"
                ].as_dict(),
                "sealed_cost_capture_json": prereg.identities[
                    "cost_capture_json"
                ].as_dict(),
                "sealed_cost_capture_npz": prereg.identities[
                    "cost_capture_npz"
                ].as_dict(),
                "sealed_fee_attestation": prereg.identities[
                    "fee_attestation"
                ].as_dict(),
            },
            "artifacts": {
                "screen_result": screen_identity.as_dict(),
                "gate_summary": summary_identity.as_dict(),
                "spools": spool_inventory,
            },
            "resource_posture": {
                "projected_binary_spool_bytes": projected_spool_bytes,
                "total_authenticated_quote_rows": inventory["quote_rows"],
                "maximum_quotes_materialized_for_one_symbol": max(
                    int(value)
                    for value in inventory["quote_rows_by_symbol"].values()
                ),
                "all_symbols_materialized_simultaneously": False,
                "screen_invocations": 1,
            },
            "all_fixed_research_gates_pass": summary_with_bindings[
                "all_fixed_research_gates_pass"
            ],
            "research_only": True,
            "authority": dict(FALSE_AUTHORITY),
        }
        manifest_identity = _write_content_addressed_json(
            staging,
            prefix="mtvclc_evaluation_manifest",
            value=evaluation_manifest,
        )
        bundle_path = _publish_bundle(staging, root, manifest_identity)
        published = True
        return {
            "status": "evaluated",
            "bundle_path": str(bundle_path),
            "evaluation_manifest": manifest_identity.as_dict(),
            "screen_result": screen_identity.as_dict(),
            "gate_summary": summary_identity.as_dict(),
            "all_fixed_research_gates_pass": evaluation_manifest[
                "all_fixed_research_gates_pass"
            ],
            "research_only": True,
            "authority": dict(FALSE_AUTHORITY),
        }
    finally:
        if not published and staging.exists():
            try:
                for path in staging.iterdir():
                    if path.is_file() and not path.is_symlink():
                        path.chmod(stat.S_IREAD | stat.S_IWRITE)
                shutil.rmtree(staging)
            except OSError:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Post-window isolated evaluation of one sealed MTVCLC-v1 capture; "
            "publishes research evidence only and never trading authority."
        )
    )
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        outcome = evaluate_capture(
            preregistration_path=args.preregistration,
            capture_root=args.capture_root,
            output_root=args.output_root,
        )
    except (EvaluationRefusal, OSError, ValueError) as exc:
        print(f"MTVCLC isolated evaluation refused: {exc}")
        return 2
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
