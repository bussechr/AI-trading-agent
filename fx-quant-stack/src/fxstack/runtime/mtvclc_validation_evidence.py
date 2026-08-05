"""Public-only verifier for signed MTVCLC post-window evidence.

The isolated release host owns evidence reconstruction and signing.  This
module authenticates one strategy-specific bundle and recomputes the sealed
MTVCLC gates from its public claims.  It has no issuer, activation, database,
bridge, broker, or order surface and deliberately does not reuse the
dislocation strategy's unrelated PBO/DSR evidence contract.
"""

from __future__ import annotations

# AGENT: ROLE: public-only verifier for signed MTVCLC post-window evidence.
# AGENT: HANDSHAKE: signed evidence bundle + public key + installed expectation -> authenticated validation result.
# AGENT: ISOLATION: no private material, research input, persistence, activation, broker, or order access.

from collections.abc import Mapping
from dataclasses import dataclass, field
import base64
import hashlib
import hmac
import json
import math
from statistics import NormalDist
from typing import Any

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)


MTVCLC_STRATEGY_ID = "ig_mt4_tick_volume_close_location_continuation"
MTVCLC_STRATEGY_VERSION = "mtvclc.v1"
MTVCLC_CONFIG_ID = "mtvclc_v1_vq90_cl80_b1k_h30_t4k_s8k"
MTVCLC_SOURCE_CONTRACT_ID = (
    "authenticated_ig_mt4_bid_m1_ohlc_ivolume_plus_bid_ask_transport_snapshots.v1"
)
MTVCLC_ACTIVITY_METRIC_ID = "mt4_m1_ivolume_tick_volume.v1"
MTVCLC_ACCOUNT_MODE = "demo"

MTVCLC_VALIDATION_CERTIFICATE_SCHEMA = (
    "fxstack.scalp.mtvclc_validation_certificate.v1"
)
MTVCLC_VALIDATION_EVIDENCE_SCHEMA = "fxstack.scalp.mtvclc_validation_evidence.v1"
MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA = (
    "fxstack.scalp.mtvclc_signed_evidence_bundle.v1"
)
CERTIFICATE_SHA256_FIELD = "certificate_body_sha256"
CERTIFICATE_SIGNATURE_FIELD = "certificate_signature_ed25519"
BUNDLE_SHA256_FIELD = "bundle_body_sha256"

MAX_CERTIFICATE_VALIDITY_SECS = 7 * 86_400.0
MIN_TRADES_PER_CELL = 30
MIN_INDEPENDENT_DAYS_PER_CELL = 10
MIN_TOTAL_TRADES = 300
MIN_TOTAL_INDEPENDENT_DAYS = 60
WIN_PROBABILITY_FAMILY_CONFIDENCE = 0.95
WILSON_FAMILY_ATTEMPTED_CELLS = 4_786
WILSON_ALPHA_ALLOCATION = "one_sided_0.05_over_4786"
WILSON_INTERVAL_METHOD = (
    "one_sided_wilson_family_adjusted_over_4786_attempted_cells"
)
_CELL_ALPHA = (1.0 - WIN_PROBABILITY_FAMILY_CONFIDENCE) / (
    WILSON_FAMILY_ATTEMPTED_CELLS
)
_CELL_Z = NormalDist().inv_cdf(1.0 - _CELL_ALPHA)
_FLOAT_TOLERANCE = 1e-12

EXPECTED_ATTEMPT_ACCOUNTING: dict[str, int] = {
    "prior_attempted_cells_lower_bound": 4_742,
    "current_attempted_cells": 44,
    "cumulative_attempted_cells_lower_bound": 4_786,
}
EXPECTED_EXECUTION_CONTRACT: dict[str, Any] = {
    "entry_type": "immediate_market",
    "execution_type": "market",
    "pending_orders_forbidden": True,
    "maximum_entry_delay_seconds": 5,
    "outcome_horizon_m1_bars": 30,
    "maximum_entries_per_symbol_utc_day": 1,
    "rollover_entry_blackout_utc": "[20:20:00,22:10:00)",
    "rollover_entry_blackout_half_open": True,
    "signals_inside_blackout_reserve": False,
}
EXPECTED_SEALED_GATES: dict[str, Any] = {
    "all_44_cells_must_pass": True,
    "minimum_trades_per_cell": MIN_TRADES_PER_CELL,
    "minimum_independent_utc_days_per_cell": MIN_INDEPENDENT_DAYS_PER_CELL,
    "cell_win_probability_interval": WILSON_INTERVAL_METHOD,
    "cell_win_probability_family_confidence": (
        WIN_PROBABILITY_FAMILY_CONFIDENCE
    ),
    "cell_win_probability_lower_bound_strictly_greater_than": (
        "the_exact_per_symbol_conversion_adjusted_break_even_win_"
        "probability_in_cost_policy.symbols"
    ),
    "minimum_unconverted_break_even_win_probability": 0.75,
    "cell_conversion_adjusted_mean_net_bps_strictly_greater_than": 0.0,
    "minimum_total_trades": MIN_TOTAL_TRADES,
    "minimum_total_independent_utc_days": MIN_TOTAL_INDEPENDENT_DAYS,
    "source_scope_ready_required": True,
    "source_errors_required": [],
    "descriptive_df99_bonferroni_abs_t_threshold": 4.645748208417252,
}
NO_RUNTIME_AUTHORITY: dict[str, bool] = {
    "activation_authorized": False,
    "registry_write_authorized": False,
    "runtime_authorized": False,
    "broker_access_authorized": False,
    "order_authorized": False,
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
_COST_ROW_FIELDS = {
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
_ARTIFACT_FIELDS = {
    "preregistration_body_sha256",
    "preregistration_artifact_sha256",
    "handoff_body_sha256",
    "handoff_artifact_sha256",
    "capture_inventory_sha256",
    "evaluator_source_sha256",
    "report_body_sha256",
    "report_artifact_sha256",
    "evidence_binding_sha256",
    "reservation_ledger_sha256",
    "outcome_ledger_sha256",
    "cell_ledger_sha256",
}


def _json_tree_valid(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_tree_valid(item) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _json_tree_valid(item)
            for key, item in value.items()
        )
    return False


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    material = dict(value)
    if not _json_tree_valid(material):
        raise ValueError("non-canonical JSON value")
    return json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _strict_int(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def ed25519_public_key_id(public_key: Any | None) -> str:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return ""
    if not isinstance(public_key, Ed25519PublicKey):
        return ""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()


def certificate_body_sha256(certificate: Mapping[str, Any]) -> str:
    body = {
        key: value
        for key, value in dict(certificate).items()
        if key not in {CERTIFICATE_SHA256_FIELD, CERTIFICATE_SIGNATURE_FIELD}
    }
    return canonical_sha256(body)


def bundle_body_sha256(bundle: Mapping[str, Any]) -> str:
    body = {
        key: value
        for key, value in dict(bundle).items()
        if key != BUNDLE_SHA256_FIELD
    }
    return canonical_sha256(body)


def wilson_one_sided_lower(*, wins: int, trials: int) -> float:
    """Exact one-sided Wilson lower bound with the sealed 0.05/4,786 alpha."""

    if (
        isinstance(wins, bool)
        or isinstance(trials, bool)
        or not isinstance(wins, int)
        or not isinstance(trials, int)
        or trials <= 0
        or wins < 0
        or wins > trials
    ):
        return 0.0
    point = wins / trials
    z_sq = _CELL_Z * _CELL_Z
    denominator = 1.0 + z_sq / trials
    center = point + z_sq / (2.0 * trials)
    radius = _CELL_Z * math.sqrt(
        point * (1.0 - point) / trials + z_sq / (4.0 * trials * trials)
    )
    return max(0.0, min(1.0, (center - radius) / denominator))


def _expected_cell_order() -> list[dict[str, str]]:
    return [
        {"config_id": MTVCLC_CONFIG_ID, "symbol": symbol, "side": side}
        for symbol in IG_MT4_SCALP_SYMBOLS
        for side in ("BUY", "SELL")
    ]


def _file_identity_valid(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "filename",
        "sha256",
        "size_bytes",
    }:
        return False
    size = _strict_int(value.get("size_bytes"), minimum=1)
    return bool(str(value.get("filename") or "").strip()) and _is_sha256(
        value.get("sha256")
    ) and size is not None


def _cost_row_error(symbol: str, value: Any) -> str:
    if not isinstance(value, Mapping) or set(value) != _COST_ROW_FIELDS:
        return f"mtvclc_evidence_cost_row_scope_invalid:{symbol}"
    numeric_fields = (
        "p90_ig_spread_bps",
        "commission_bps_per_round_trip",
        "financing_bps_per_trade",
        "fixed_adverse_execution_debit_bps",
        "pre_conversion_geometry_cost_bps",
        "conversion_rate_of_absolute_profit_or_loss",
        "convert_on_close_charge_fraction_for_screen",
        "conversion_adjusted_break_even_win_probability",
    )
    numbers = {field: _finite(value.get(field)) for field in numeric_fields}
    if any(number is None or number < 0.0 for number in numbers.values()):
        return f"mtvclc_evidence_cost_row_numeric_invalid:{symbol}"
    if numbers["p90_ig_spread_bps"] <= 0.0:
        return f"mtvclc_evidence_cost_row_spread_invalid:{symbol}"
    expected_pre_conversion = (
        numbers["p90_ig_spread_bps"]
        + numbers["commission_bps_per_round_trip"]
        + numbers["financing_bps_per_trade"]
        + numbers["fixed_adverse_execution_debit_bps"]
    )
    if not math.isclose(
        numbers["pre_conversion_geometry_cost_bps"],
        expected_pre_conversion,
        rel_tol=0.0,
        abs_tol=_FLOAT_TOLERANCE,
    ):
        return f"mtvclc_evidence_cost_row_geometry_invalid:{symbol}"
    account_currency = str(value.get("account_currency") or "")
    pnl_currency = str(value.get("profit_loss_currency") or "")
    applies = value.get("conversion_applies")
    if (
        account_currency != "USD"
        or len(pnl_currency) != 3
        or applies is not (pnl_currency != account_currency)
    ):
        return f"mtvclc_evidence_cost_row_currency_invalid:{symbol}"
    conversion_rate = numbers["conversion_rate_of_absolute_profit_or_loss"]
    screen_rate = numbers["convert_on_close_charge_fraction_for_screen"]
    expected_screen_rate = conversion_rate if applies else 0.0
    if not math.isclose(
        screen_rate,
        expected_screen_rate,
        rel_tol=0.0,
        abs_tol=_FLOAT_TOLERANCE,
    ):
        return f"mtvclc_evidence_cost_row_conversion_invalid:{symbol}"
    cost = numbers["pre_conversion_geometry_cost_bps"]
    target = 4.0 * cost
    stop = 8.0 * cost
    expected_break_even = (stop * (1.0 + screen_rate) + cost) / (
        target * (1.0 - screen_rate) + stop * (1.0 + screen_rate)
    )
    if not math.isclose(
        numbers["conversion_adjusted_break_even_win_probability"],
        expected_break_even,
        rel_tol=0.0,
        abs_tol=_FLOAT_TOLERANCE,
    ):
        return f"mtvclc_evidence_cost_row_break_even_invalid:{symbol}"
    if (
        value.get("commission_status")
        not in {"explicit_source_attested", "conservative_upper_bound"}
        or value.get("financing_status")
        not in {
            "structurally_avoided_by_fixed_rollover_guard",
            "conservative_upper_bound",
        }
        or value.get("conversion_status")
        != "debit_absolute_profit_or_loss_when_account_currency_differs"
    ):
        return f"mtvclc_evidence_cost_row_status_invalid:{symbol}"
    return ""


def mtvclc_evidence_error(value: Any) -> str:
    """Return the first fail-closed semantic error in public evidence claims."""

    if not isinstance(value, Mapping):
        return "mtvclc_evidence_missing"
    expected_top = {
        "schema_version",
        "account_mode",
        "strategy",
        "scope",
        "execution_contract",
        "attempt_accounting",
        "wilson_allocation",
        "sealed_gates",
        "artifacts",
        "costs",
        "overall",
        "cells",
        "authority",
    }
    if set(value) != expected_top:
        return "mtvclc_evidence_scope_invalid"
    if value.get("schema_version") != MTVCLC_VALIDATION_EVIDENCE_SCHEMA:
        return "mtvclc_evidence_schema_invalid"
    if value.get("account_mode") != MTVCLC_ACCOUNT_MODE:
        return "mtvclc_evidence_account_mode_invalid"
    strategy = value.get("strategy")
    if not isinstance(strategy, Mapping) or set(strategy) != {
        "strategy_id",
        "strategy_version",
        "config_id",
        "config_sha256",
        "source_contract_id",
        "activity_metric_id",
        "attempt_manifest_sha256",
    }:
        return "mtvclc_evidence_strategy_scope_invalid"
    if (
        strategy.get("strategy_id") != MTVCLC_STRATEGY_ID
        or strategy.get("strategy_version") != MTVCLC_STRATEGY_VERSION
        or strategy.get("config_id") != MTVCLC_CONFIG_ID
        or not _is_sha256(strategy.get("config_sha256"))
        or strategy.get("source_contract_id") != MTVCLC_SOURCE_CONTRACT_ID
        or strategy.get("activity_metric_id") != MTVCLC_ACTIVITY_METRIC_ID
        or not _is_sha256(strategy.get("attempt_manifest_sha256"))
    ):
        return "mtvclc_evidence_strategy_invalid"
    scope = value.get("scope")
    if not isinstance(scope, Mapping) or set(scope) != {
        "venue_id",
        "scope_version",
        "symbol_scope",
        "cell_order",
    }:
        return "mtvclc_evidence_scope_contract_invalid"
    if (
        scope.get("venue_id") != IG_MT4_VENUE_ID
        or scope.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or scope.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or scope.get("cell_order") != _expected_cell_order()
    ):
        return "mtvclc_evidence_symbol_scope_invalid"
    if value.get("execution_contract") != EXPECTED_EXECUTION_CONTRACT:
        return "mtvclc_evidence_execution_contract_invalid"
    if value.get("attempt_accounting") != EXPECTED_ATTEMPT_ACCOUNTING:
        return "mtvclc_evidence_attempt_accounting_invalid"
    if value.get("wilson_allocation") != {
        "method": WILSON_INTERVAL_METHOD,
        "family_confidence": WIN_PROBABILITY_FAMILY_CONFIDENCE,
        "attempted_cells": WILSON_FAMILY_ATTEMPTED_CELLS,
        "alpha_allocation": WILSON_ALPHA_ALLOCATION,
    }:
        return "mtvclc_evidence_wilson_allocation_invalid"
    if value.get("sealed_gates") != EXPECTED_SEALED_GATES:
        return "mtvclc_evidence_sealed_gates_invalid"
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != _ARTIFACT_FIELDS:
        return "mtvclc_evidence_artifact_scope_invalid"
    if any(not _is_sha256(artifacts.get(field)) for field in _ARTIFACT_FIELDS):
        return "mtvclc_evidence_artifact_hash_invalid"

    costs = value.get("costs")
    if not isinstance(costs, Mapping) or set(costs) != {
        "capture_bundle",
        "fee_attestation",
        "cost_policy_sha256",
        "cost_rows_sha256",
        "cost_row_sha256_by_symbol",
        "rows",
    }:
        return "mtvclc_evidence_cost_scope_invalid"
    bundle = costs.get("capture_bundle")
    if not isinstance(bundle, Mapping) or set(bundle) != {
        "capture_json",
        "capture_npz",
        "capture_payload_sha256",
        "capture_mode",
        "scope_version",
        "venue_id",
    }:
        return "mtvclc_evidence_cost_bundle_invalid"
    if (
        not _file_identity_valid(bundle.get("capture_json"))
        or not _file_identity_valid(bundle.get("capture_npz"))
        or not _is_sha256(bundle.get("capture_payload_sha256"))
        or bundle.get("capture_mode") != "authenticated_same_source_db_history"
        or bundle.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or bundle.get("venue_id") != IG_MT4_VENUE_ID
        or not _file_identity_valid(costs.get("fee_attestation"))
        or not _is_sha256(costs.get("cost_policy_sha256"))
        or not _is_sha256(costs.get("cost_rows_sha256"))
    ):
        return "mtvclc_evidence_cost_bundle_invalid"
    rows = costs.get("rows")
    row_hashes = costs.get("cost_row_sha256_by_symbol")
    if (
        not isinstance(rows, Mapping)
        or not isinstance(row_hashes, Mapping)
        or set(rows) != set(IG_MT4_SCALP_SYMBOLS)
        or set(row_hashes) != set(IG_MT4_SCALP_SYMBOLS)
    ):
        return "mtvclc_evidence_cost_rows_invalid"
    for symbol in IG_MT4_SCALP_SYMBOLS:
        error = _cost_row_error(symbol, rows[symbol])
        if error:
            return error
        expected_hash = canonical_sha256(dict(rows[symbol]))
        if not hmac.compare_digest(str(row_hashes[symbol]), expected_hash):
            return f"mtvclc_evidence_cost_row_hash_invalid:{symbol}"
    if not hmac.compare_digest(
        str(costs.get("cost_rows_sha256")), canonical_sha256(dict(rows))
    ):
        return "mtvclc_evidence_cost_rows_hash_invalid"

    overall = value.get("overall")
    if not isinstance(overall, Mapping) or set(overall) != {
        "source_scope_ready",
        "source_errors",
        "all_44_cells_pass",
        "total_trades",
        "total_independent_utc_days",
        "all_preregistered_success_gates_pass",
    }:
        return "mtvclc_evidence_overall_scope_invalid"
    total_trades = _strict_int(overall.get("total_trades"), minimum=0)
    total_days = _strict_int(
        overall.get("total_independent_utc_days"), minimum=0
    )
    if (
        overall.get("source_scope_ready") is not True
        or overall.get("source_errors") != []
        or overall.get("all_44_cells_pass") is not True
        or overall.get("all_preregistered_success_gates_pass") is not True
        or total_trades is None
        or total_trades < MIN_TOTAL_TRADES
        or total_days is None
        or total_days < MIN_TOTAL_INDEPENDENT_DAYS
    ):
        return "mtvclc_evidence_overall_gates_failed"

    cells = value.get("cells")
    if not isinstance(cells, list) or len(cells) != 44:
        return "mtvclc_evidence_cells_invalid"
    expected_order = _expected_cell_order()
    summed_reservations = 0
    max_days = 0
    summed_days = 0
    for expected, cell in zip(expected_order, cells, strict=True):
        if not isinstance(cell, Mapping) or set(cell) != _CELL_FIELDS:
            return "mtvclc_evidence_cell_scope_invalid"
        if any(cell.get(key) != expected[key] for key in expected):
            return "mtvclc_evidence_cell_order_invalid"
        reservations = _strict_int(cell.get("reservations"), minimum=0)
        wins = _strict_int(cell.get("wins"), minimum=0)
        days = _strict_int(cell.get("independent_days"), minimum=0)
        rate = _finite(cell.get("full_target_rate"))
        recorded_lower = _finite(cell.get("win_probability_wilson_lower"))
        break_even = _finite(cell.get("base_break_even_probability"))
        mean_net = _finite(cell.get("mean_net_bps"))
        if (
            cell.get("source_ready") is not True
            or cell.get("passes_fixed_cell_screen") is not True
            or reservations is None
            or reservations < MIN_TRADES_PER_CELL
            or wins is None
            or wins > reservations
            or days is None
            or days < MIN_INDEPENDENT_DAYS_PER_CELL
            or rate is None
            or recorded_lower is None
            or break_even is None
            or mean_net is None
            or mean_net <= 0.0
        ):
            return "mtvclc_evidence_cell_gate_failed"
        expected_rate = wins / reservations
        expected_lower = wilson_one_sided_lower(wins=wins, trials=reservations)
        expected_break_even = float(
            rows[str(cell["symbol"])][
                "conversion_adjusted_break_even_win_probability"
            ]
        )
        if (
            not math.isclose(rate, expected_rate, rel_tol=0.0, abs_tol=1e-15)
            or not math.isclose(
                recorded_lower,
                expected_lower,
                rel_tol=0.0,
                abs_tol=1e-15,
            )
            or not math.isclose(
                break_even,
                expected_break_even,
                rel_tol=0.0,
                abs_tol=1e-15,
            )
            or recorded_lower <= break_even
        ):
            return "mtvclc_evidence_cell_math_invalid"
        summed_reservations += reservations
        max_days = max(max_days, days)
        summed_days += days
    if summed_reservations != total_trades:
        return "mtvclc_evidence_trade_total_mismatch"
    if not max_days <= total_days <= summed_days:
        return "mtvclc_evidence_day_total_mismatch"
    if value.get("authority") != NO_RUNTIME_AUTHORITY:
        return "mtvclc_evidence_authority_invalid"
    return ""


@dataclass(frozen=True, slots=True)
class MTVCLCValidationExpectation:
    generation_id: str
    strategy_id: str
    strategy_version: str
    config_id: str
    config_sha256: str
    evaluator_source_sha256: str

    def validation_error(self) -> str:
        if not str(self.generation_id or "").strip():
            return "mtvclc_expected_generation_id_missing"
        if self.strategy_id != MTVCLC_STRATEGY_ID:
            return "mtvclc_expected_strategy_id_invalid"
        if self.strategy_version != MTVCLC_STRATEGY_VERSION:
            return "mtvclc_expected_strategy_version_invalid"
        if self.config_id != MTVCLC_CONFIG_ID:
            return "mtvclc_expected_config_id_invalid"
        if not _is_sha256(self.config_sha256):
            return "mtvclc_expected_config_sha256_invalid"
        if not _is_sha256(self.evaluator_source_sha256):
            return "mtvclc_expected_evaluator_sha256_invalid"
        return ""


@dataclass(frozen=True, slots=True)
class MTVCLCValidationVerification:
    valid: bool
    reason: str
    errors: tuple[str, ...]
    authenticated: bool = False
    certificate_sha256: str = ""
    evidence_sha256: str = ""
    signing_key_id: str = ""
    generation_id: str = ""
    strategy_id: str = ""
    strategy_version: str = ""
    config_id: str = ""
    config_sha256: str = ""
    evaluator_source_sha256: str = ""
    venue_id: str = ""
    account_mode: str = ""
    scope_version: str = ""
    symbol_scope: tuple[str, ...] = ()
    issued_at_epoch: float = 0.0
    expires_at_epoch: float = 0.0
    artifact_sha256: dict[str, str] = field(default_factory=dict)


def _verify_signature(certificate: Mapping[str, Any], public_key: Any) -> str:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return "signature_backend_unavailable"
    if not isinstance(public_key, Ed25519PublicKey):
        return "public_key_invalid"
    encoded = certificate.get(CERTIFICATE_SIGNATURE_FIELD)
    if not isinstance(encoded, str) or not encoded:
        return "signature_missing"
    try:
        signature = base64.b64decode(encoded, validate=True)
        material = {
            key: value
            for key, value in certificate.items()
            if key != CERTIFICATE_SIGNATURE_FIELD
        }
        public_key.verify(signature, canonical_json_bytes(material))
    except (InvalidSignature, TypeError, ValueError):
        return "signature_invalid"
    return ""


def verify_mtvclc_validation_evidence(
    *,
    bundle: Mapping[str, Any] | None,
    public_key: Any | None,
    expectation: MTVCLCValidationExpectation,
    now_epoch: float,
) -> MTVCLCValidationVerification:
    """Authenticate one evidence-only bundle and recompute sealed gates."""

    key_id = ed25519_public_key_id(public_key)
    authenticated = False
    cert_sha = ""
    evidence_sha = ""

    def result(
        reason: str,
        *,
        issued_at: float = 0.0,
        expires_at: float = 0.0,
        artifacts: Mapping[str, Any] | None = None,
    ) -> MTVCLCValidationVerification:
        valid = not reason
        return MTVCLCValidationVerification(
            valid=valid,
            reason=reason,
            errors=() if valid else (reason,),
            authenticated=authenticated,
            certificate_sha256=cert_sha,
            evidence_sha256=evidence_sha,
            signing_key_id=key_id,
            generation_id=expectation.generation_id,
            strategy_id=expectation.strategy_id,
            strategy_version=expectation.strategy_version,
            config_id=expectation.config_id,
            config_sha256=expectation.config_sha256,
            evaluator_source_sha256=expectation.evaluator_source_sha256,
            venue_id=IG_MT4_VENUE_ID if valid else "",
            account_mode=MTVCLC_ACCOUNT_MODE if valid else "",
            scope_version=IG_MT4_SCALP_SCOPE_VERSION if valid else "",
            symbol_scope=IG_MT4_SCALP_SYMBOLS if valid else (),
            issued_at_epoch=issued_at,
            expires_at_epoch=expires_at,
            artifact_sha256={
                str(key): str(value) for key, value in dict(artifacts or {}).items()
            }
            if valid
            else {},
        )

    expectation_error = expectation.validation_error()
    if expectation_error:
        return result(expectation_error)
    if not key_id:
        return result("mtvclc_public_key_unavailable")
    if not isinstance(bundle, Mapping) or set(bundle) != {
        "schema_version",
        "certificate",
        BUNDLE_SHA256_FIELD,
    }:
        return result("mtvclc_bundle_scope_invalid")
    if bundle.get("schema_version") != MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA:
        return result("mtvclc_bundle_schema_invalid")
    claimed_bundle_sha = str(bundle.get(BUNDLE_SHA256_FIELD) or "").lower()
    try:
        computed_bundle_sha = bundle_body_sha256(bundle)
    except (TypeError, ValueError):
        return result("mtvclc_bundle_body_invalid")
    if not _is_sha256(claimed_bundle_sha) or not hmac.compare_digest(
        claimed_bundle_sha, computed_bundle_sha
    ):
        return result("mtvclc_bundle_hash_invalid")
    raw_certificate = bundle.get("certificate")
    if not isinstance(raw_certificate, Mapping):
        return result("mtvclc_certificate_missing")
    certificate = dict(raw_certificate)
    expected_certificate_fields = {
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
        "signing_key_id",
        "evidence",
        "evidence_sha256",
        "authority",
        CERTIFICATE_SHA256_FIELD,
        CERTIFICATE_SIGNATURE_FIELD,
    }
    if set(certificate) != expected_certificate_fields:
        return result("mtvclc_certificate_scope_invalid")
    if certificate.get("signing_key_id") != key_id:
        return result("mtvclc_certificate_signing_key_mismatch")
    claimed_cert_sha = str(certificate.get(CERTIFICATE_SHA256_FIELD) or "").lower()
    try:
        computed_cert_sha = certificate_body_sha256(certificate)
    except (TypeError, ValueError):
        return result("mtvclc_certificate_body_invalid")
    if not _is_sha256(claimed_cert_sha) or not hmac.compare_digest(
        claimed_cert_sha, computed_cert_sha
    ):
        return result("mtvclc_certificate_hash_invalid")
    signature_error = _verify_signature(certificate, public_key)
    if signature_error:
        return result(f"mtvclc_certificate_{signature_error}")
    authenticated = True
    cert_sha = claimed_cert_sha
    if certificate.get("schema_version") != MTVCLC_VALIDATION_CERTIFICATE_SCHEMA:
        return result("mtvclc_certificate_schema_invalid")
    expected_claims = {
        "generation_id": expectation.generation_id,
        "strategy_id": expectation.strategy_id,
        "strategy_version": expectation.strategy_version,
        "config_id": expectation.config_id,
        "config_sha256": expectation.config_sha256,
        "evaluator_source_sha256": expectation.evaluator_source_sha256,
        "venue_id": IG_MT4_VENUE_ID,
        "account_mode": MTVCLC_ACCOUNT_MODE,
        "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "authority": NO_RUNTIME_AUTHORITY,
    }
    for field_name, expected_value in expected_claims.items():
        if certificate.get(field_name) != expected_value:
            return result(f"mtvclc_certificate_{field_name}_mismatch")
    now = _finite(now_epoch)
    issued_at = _finite(certificate.get("issued_at_epoch"))
    expires_at = _finite(certificate.get("expires_at_epoch"))
    if now is None or now <= 0.0:
        return result("mtvclc_clock_invalid")
    if (
        issued_at is None
        or expires_at is None
        or issued_at <= 0.0
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_CERTIFICATE_VALIDITY_SECS
    ):
        return result("mtvclc_certificate_time_window_invalid")
    if issued_at > now + 5.0:
        return result(
            "mtvclc_certificate_future_dated",
            issued_at=issued_at,
            expires_at=expires_at,
        )
    if expires_at <= now:
        return result(
            "mtvclc_certificate_expired",
            issued_at=issued_at,
            expires_at=expires_at,
        )
    evidence = certificate.get("evidence")
    if not isinstance(evidence, Mapping):
        return result("mtvclc_evidence_missing")
    try:
        computed_evidence_sha = canonical_sha256(evidence)
    except (TypeError, ValueError):
        return result("mtvclc_evidence_invalid")
    claimed_evidence_sha = str(certificate.get("evidence_sha256") or "").lower()
    if not _is_sha256(claimed_evidence_sha) or not hmac.compare_digest(
        claimed_evidence_sha, computed_evidence_sha
    ):
        return result("mtvclc_evidence_hash_invalid")
    evidence_sha = computed_evidence_sha
    error = mtvclc_evidence_error(evidence)
    if error:
        return result(error, issued_at=issued_at, expires_at=expires_at)
    strategy = evidence["strategy"]
    artifacts = evidence["artifacts"]
    if (
        strategy["config_sha256"] != expectation.config_sha256
        or artifacts["evaluator_source_sha256"]
        != expectation.evaluator_source_sha256
    ):
        return result(
            "mtvclc_evidence_expectation_mismatch",
            issued_at=issued_at,
            expires_at=expires_at,
        )
    return result(
        "",
        issued_at=issued_at,
        expires_at=expires_at,
        artifacts=artifacts,
    )


__all__ = [
    "BUNDLE_SHA256_FIELD",
    "CERTIFICATE_SHA256_FIELD",
    "CERTIFICATE_SIGNATURE_FIELD",
    "EXPECTED_ATTEMPT_ACCOUNTING",
    "EXPECTED_EXECUTION_CONTRACT",
    "EXPECTED_SEALED_GATES",
    "MAX_CERTIFICATE_VALIDITY_SECS",
    "MTVCLC_ACCOUNT_MODE",
    "MTVCLC_ACTIVITY_METRIC_ID",
    "MTVCLC_CONFIG_ID",
    "MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA",
    "MTVCLC_SOURCE_CONTRACT_ID",
    "MTVCLC_STRATEGY_ID",
    "MTVCLC_STRATEGY_VERSION",
    "MTVCLC_VALIDATION_CERTIFICATE_SCHEMA",
    "MTVCLC_VALIDATION_EVIDENCE_SCHEMA",
    "MTVCLCValidationExpectation",
    "MTVCLCValidationVerification",
    "NO_RUNTIME_AUTHORITY",
    "WILSON_ALPHA_ALLOCATION",
    "WILSON_FAMILY_ATTEMPTED_CELLS",
    "WILSON_INTERVAL_METHOD",
    "bundle_body_sha256",
    "canonical_json_bytes",
    "canonical_sha256",
    "certificate_body_sha256",
    "ed25519_public_key_id",
    "mtvclc_evidence_error",
    "verify_mtvclc_validation_evidence",
    "wilson_one_sided_lower",
]
