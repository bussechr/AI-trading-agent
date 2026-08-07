"""Runtime-native MTVCLC costs from an authenticated spread capture."""

from __future__ import annotations

# AGENT: ROLE: Project a hash-pinned IG spread capture into exact-22 runtime-native cost rows.
# AGENT: CALLED BY: `scalp_runtime_admission.py` for the common demo/real runtime contract.
# AGENT: SIDE EFFECTS: read one bounded local JSON file; never reads keys, writes state, or submits trades.
# AGENT HANDSHAKE: exact authenticated capture + explicit cost assumptions -> typed proposal geometry.

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.strategy.mtvclc import (
    FIXED_ADVERSE_EXECUTION_DEBIT_BPS,
    IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION,
    MTVCLCCostCalibration,
)


RUNTIME_COST_CAPTURE_SCHEMA = "fxstack.external_ig_mt4_bid_ask_capture.v1"
RUNTIME_COST_SNAPSHOT_SCHEMA = "fxstack.runtime.scalp_cost_snapshot.v1"
RUNTIME_COST_CAPTURE_DEFINITION = "authenticated_ig_demo_live_quote_calibration.v1"
RUNTIME_COST_SAMPLE_SOURCE = "authenticated_same_source_db_history"
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MIN_OBSERVATIONS_PER_SYMBOL = 100
MIN_DURATION_SECONDS = 300.0
RUNTIME_COMMISSION_BPS_PER_ROUND_TRIP = 0.0
RUNTIME_FINANCING_BPS_PER_TRADE = 0.0


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _mapping_keys(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(str(item).strip().upper() for item in value)


@dataclass(frozen=True, slots=True)
class RuntimeCostSnapshot:
    valid: bool
    reason: str
    errors: tuple[str, ...]
    costs_by_symbol: Mapping[str, MTVCLCCostCalibration]
    capture_file_sha256: str
    capture_payload_sha256: str
    calibration_id: str
    source_id: str
    source_version: str
    commission_assumption_bps_per_round_trip: float
    financing_assumption_bps_per_trade: float
    authority: bool = False
    execution_eligible: bool = False
    qualification_eligible: bool = False
    schema_version: str = RUNTIME_COST_SNAPSHOT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "costs_by_symbol": {
                symbol: {
                    **asdict(cost),
                    "row_sha256": cost.row_sha256(),
                }
                for symbol, cost in self.costs_by_symbol.items()
            },
        }


def _refusal(*errors: str, file_sha256: str = "") -> RuntimeCostSnapshot:
    unique = tuple(dict.fromkeys(str(item) for item in errors if str(item)))
    return RuntimeCostSnapshot(
        valid=False,
        reason=unique[0] if unique else "runtime_cost_snapshot_invalid",
        errors=unique or ("runtime_cost_snapshot_invalid",),
        costs_by_symbol={},
        capture_file_sha256=file_sha256,
        capture_payload_sha256="",
        calibration_id="",
        source_id="",
        source_version="",
        commission_assumption_bps_per_round_trip=(
            RUNTIME_COMMISSION_BPS_PER_ROUND_TRIP
        ),
        financing_assumption_bps_per_trade=RUNTIME_FINANCING_BPS_PER_TRADE,
    )


def load_runtime_cost_snapshot(
    *,
    capture_path: str | Path,
    expected_file_sha256: str,
    account_currency: str = "USD",
) -> RuntimeCostSnapshot:
    """Load the hash-pinned cost snapshot used by the common demo/real runtime."""

    expected_sha = str(expected_file_sha256 or "").strip().lower()
    if not _is_sha256(expected_sha):
        return _refusal("runtime_cost_capture_expected_sha256_invalid")
    path_text = str(capture_path or "").strip()
    if not path_text:
        return _refusal("runtime_cost_capture_path_missing")
    try:
        path = Path(path_text).expanduser().resolve(strict=True)
        if not path.is_file():
            return _refusal("runtime_cost_capture_file_invalid")
        raw = path.read_bytes()
    except (OSError, RuntimeError):
        return _refusal("runtime_cost_capture_read_failed")
    if not raw or len(raw) > MAX_CAPTURE_BYTES:
        return _refusal("runtime_cost_capture_size_invalid")
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha:
        return _refusal(
            "runtime_cost_capture_sha256_mismatch",
            file_sha256=actual_sha,
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _refusal("runtime_cost_capture_json_invalid", file_sha256=actual_sha)
    if not isinstance(parsed, Mapping):
        return _refusal("runtime_cost_capture_shape_invalid", file_sha256=actual_sha)

    errors: list[str] = []
    if str(parsed.get("schema_version") or "") != RUNTIME_COST_CAPTURE_SCHEMA:
        errors.append("runtime_cost_capture_schema_invalid")
    if str(parsed.get("capture_definition") or "") != RUNTIME_COST_CAPTURE_DEFINITION:
        errors.append("runtime_cost_capture_definition_invalid")
    if str(parsed.get("venue_id") or "").strip().lower() != IG_MT4_VENUE_ID:
        errors.append("runtime_cost_capture_venue_invalid")
    if str(parsed.get("account_mode") or "").strip().lower() != "demo":
        errors.append("runtime_cost_capture_account_mode_invalid")
    if str(parsed.get("scope_version") or "") != IG_MT4_SCALP_SCOPE_VERSION:
        errors.append("runtime_cost_capture_scope_version_invalid")
    symbol_scope = tuple(
        str(item).strip().upper() for item in list(parsed.get("symbol_scope") or [])
    )
    if symbol_scope != tuple(IG_MT4_SCALP_SYMBOLS):
        errors.append("runtime_cost_capture_symbol_scope_invalid")
    if list(parsed.get("source_errors") or []):
        errors.append("runtime_cost_capture_source_errors_present")
    payload_sha = str(parsed.get("capture_payload_sha256") or "").strip().lower()
    if not _is_sha256(payload_sha):
        errors.append("runtime_cost_capture_payload_sha256_invalid")
    source_id = str(parsed.get("source_id") or "").strip()
    source_version = str(parsed.get("source_version") or "").strip()
    if not source_id or not source_version:
        errors.append("runtime_cost_capture_source_identity_invalid")

    point_in_time = parsed.get("point_in_time_audit")
    if not isinstance(point_in_time, Mapping):
        errors.append("runtime_cost_capture_point_in_time_audit_invalid")
        point_in_time = {}
    if point_in_time.get("passed") is not True or list(
        point_in_time.get("errors") or []
    ):
        errors.append("runtime_cost_capture_point_in_time_failed")
    if point_in_time.get("database_read_only") is not True:
        errors.append("runtime_cost_capture_not_read_only")
    if str(point_in_time.get("sample_source") or "") != RUNTIME_COST_SAMPLE_SOURCE:
        errors.append("runtime_cost_capture_sample_source_invalid")
    if int(point_in_time.get("minimum_samples_per_symbol") or 0) < (
        MIN_OBSERVATIONS_PER_SYMBOL
    ):
        errors.append("runtime_cost_capture_minimum_samples_invalid")
    minimum_duration = _finite(point_in_time.get("minimum_duration_secs"))
    if minimum_duration is None or minimum_duration < MIN_DURATION_SECONDS:
        errors.append("runtime_cost_capture_minimum_duration_invalid")

    raw_symbols = parsed.get("symbols")
    if _mapping_keys(raw_symbols) != tuple(sorted(IG_MT4_SCALP_SYMBOLS)):
        errors.append("runtime_cost_capture_symbol_rows_invalid")
    audit_symbols = point_in_time.get("symbols")
    if _mapping_keys(audit_symbols) != tuple(sorted(IG_MT4_SCALP_SYMBOLS)):
        errors.append("runtime_cost_capture_audit_symbol_rows_invalid")

    currency = str(account_currency or "").strip().upper()
    if currency != "USD":
        errors.append("runtime_cost_snapshot_account_currency_unsupported")
    prepared: dict[str, tuple[float, str]] = {}
    if isinstance(raw_symbols, Mapping) and isinstance(audit_symbols, Mapping):
        for symbol in IG_MT4_SCALP_SYMBOLS:
            row = raw_symbols.get(symbol)
            audit = audit_symbols.get(symbol)
            if not isinstance(row, Mapping) or not isinstance(audit, Mapping):
                errors.append(f"runtime_cost_capture_symbol_missing:{symbol}")
                continue
            spread = _finite(row.get("p90_observed_spread_bps"))
            observations = int(row.get("observations") or 0)
            duration = _finite(row.get("duration_secs"))
            if spread is None or spread <= 0.0:
                errors.append(f"runtime_cost_capture_spread_invalid:{symbol}")
            if observations < MIN_OBSERVATIONS_PER_SYMBOL:
                errors.append(f"runtime_cost_capture_observations_invalid:{symbol}")
            if duration is None or duration < MIN_DURATION_SECONDS:
                errors.append(f"runtime_cost_capture_duration_invalid:{symbol}")
            if audit.get("passed") is not True:
                errors.append(f"runtime_cost_capture_symbol_audit_failed:{symbol}")
            if int(audit.get("observations") or 0) != observations:
                errors.append(f"runtime_cost_capture_observation_mismatch:{symbol}")
            audit_duration = _finite(audit.get("duration_secs"))
            if (
                duration is not None
                and audit_duration is not None
                and not math.isclose(duration, audit_duration, rel_tol=0.0, abs_tol=1e-9)
            ):
                errors.append(f"runtime_cost_capture_duration_mismatch:{symbol}")
            if spread is not None and spread > 0.0:
                prepared[symbol] = (spread, symbol[-3:])

    unique_errors = tuple(dict.fromkeys(errors))
    if unique_errors:
        return _refusal(*unique_errors, file_sha256=actual_sha)

    calibration_id = f"runtime-native-{actual_sha[:24]}"
    costs = {
        symbol: MTVCLCCostCalibration(
            symbol=symbol,
            calibration_id=calibration_id,
            source_sha256=actual_sha,
            p90_spread_bps=prepared[symbol][0],
            commission_bps_per_round_trip=RUNTIME_COMMISSION_BPS_PER_ROUND_TRIP,
            financing_bps_per_trade=RUNTIME_FINANCING_BPS_PER_TRADE,
            account_currency=currency,
            pnl_currency=prepared[symbol][1],
            convert_on_close_charge_fraction=(
                0.0
                if prepared[symbol][1] == currency
                else IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION
            ),
            adverse_execution_debit_bps=FIXED_ADVERSE_EXECUTION_DEBIT_BPS,
        )
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    return RuntimeCostSnapshot(
        valid=True,
        reason="",
        errors=(),
        costs_by_symbol=costs,
        capture_file_sha256=actual_sha,
        capture_payload_sha256=payload_sha,
        calibration_id=calibration_id,
        source_id=source_id,
        source_version=source_version,
        commission_assumption_bps_per_round_trip=(
            RUNTIME_COMMISSION_BPS_PER_ROUND_TRIP
        ),
        financing_assumption_bps_per_trade=RUNTIME_FINANCING_BPS_PER_TRADE,
    )


__all__ = [
    "RUNTIME_COST_SNAPSHOT_SCHEMA",
    "RuntimeCostSnapshot",
    "load_runtime_cost_snapshot",
]
