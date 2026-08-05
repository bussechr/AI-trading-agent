"""Seal the research-only MTVCLC-v1 prospective experiment declaration.

This tool performs local, read-only validation of source and cost inputs and
publishes one content-addressed JSON preregistration.  It has no HTTP, broker,
database, credential, signing, issuer, activation, registry, runtime-control,
or order path.  The resulting artifact grants no authority and cannot be used
as outcome evidence.
"""

from __future__ import annotations

# AGENT: ROLE: Offline MTVCLC-v1 preregistration sealer.
# AGENT: HANDSHAKE: frozen screen/collector/cost/fee bytes -> immutable research declaration.
# AGENT ISOLATION: no outcomes, issuer, production mutation, or trading authority.
# AGENT: SIDE EFFECTS: one atomic, exclusive, content-addressed JSON write only.

import argparse
import ast
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.providers.ig_mt4_catalog import (  # noqa: E402
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.runtime.scalp_engine_identity import (  # noqa: E402
    production_scalp_engine_identity,
)
from fxstack.scalp import (  # noqa: E402
    screen_mt4_tick_volume_close_location_continuation as screen,
)
from fxstack.strategy.scalp_dislocation import (  # noqa: E402
    SCALP_DISLOCATION_STRATEGY_ID,
    SCALP_DISLOCATION_STRATEGY_VERSION,
    DislocationPolicy,
)


PREREGISTRATION_SCHEMA = "fxstack.scalp.mtvclc_preregistration.v1"
FEE_ATTESTATION_SCHEMA = "fxstack.scalp.mtvclc_fee_attestation.v1"
COST_CAPTURE_SCHEMA = "fxstack.external_ig_mt4_bid_ask_capture.v1"
COLLECTOR_PATH = REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity.py"
SCREEN_PATH = (
    FXSTACK_SRC
    / "fxstack"
    / "scalp"
    / "screen_mt4_tick_volume_close_location_continuation.py"
)
CATALOG_PATH = (
    FXSTACK_SRC / "fxstack" / "providers" / "ig_mt4_catalog.py"
)
TOOL_PATH = Path(__file__).resolve()

PROSPECTIVE_WINDOW_DAYS = 180
MINIMUM_START_DELAY_SECONDS = 60
DEFAULT_START_DELAY_SECONDS = 300
MAXIMUM_START_DELAY_SECONDS = 3_600
MINIMUM_TOTAL_TRADES = 300
MINIMUM_TOTAL_INDEPENDENT_DAYS = 60
ACCOUNT_CURRENCY_CONVERSION_RATE_FLOOR = 0.005
MAXIMUM_ATTESTED_FEE_BPS = 1_000.0
MAXIMUM_INPUT_BYTES = 1024 * 1024 * 1024
FIXED_AUTHORITY_FLAGS: dict[str, bool] = {
    "research_process_authorized": False,
    "outcome_access_authorized": False,
    "success_claim_authorized": False,
    "promotion_authorized": False,
    "activation_authorized": False,
    "registry_write_authorized": False,
    "runtime_authorized": False,
    "issuer_authorized": False,
    "signature_authorized": False,
    "broker_access_authorized": False,
    "order_authorized": False,
}
ABANDONED_PREREGISTRATION_AUDIT: tuple[dict[str, Any], ...] = (
    {
        "preregistration_body_sha256": (
            "5ff534bff7013d5836ab0034d2ef13d0739c56046d4d2637a5fd73df917c97d7"
        ),
        "artifact_file_sha256": (
            "fd32199fe13f6989743e7ad1b607ef39c2129bb3a7d20d24661eb13793347f9b"
        ),
        "reason": "atomic_publish_temp_hardlink_retained",
        "eligible_observations_emitted": False,
        "manifest_entries_emitted": 0,
        "attempted_cells_increment": 0,
    },
    {
        "preregistration_body_sha256": (
            "0511ee9c98204edc6dfd5166abade98eb47ec4f10c6364c36f504c7151853d96"
        ),
        "artifact_file_sha256": (
            "3cf1b36e4cd311425515ba8fe2268626f0c8e84ed750264e9793dd4e83c283a3"
        ),
        "reason": "first_cycle_refused_opaque_broker_token_misclassified_as_utc",
        "eligible_observations_emitted": False,
        "manifest_entries_emitted": 0,
        "attempted_cells_increment": 0,
    },
)
SOURCE_DOCUMENT_URLS: dict[str, str] = {
    "ig_mt4_forex_product_details": (
        "https://www.ig.com/uk/help-and-support/articles/"
        "681915-mt4-forex-product-details"
    ),
    "ig_mt4_crypto_product_details": (
        "https://www.ig.com/en/help-and-support/articles/"
        "681844-cryptocurrencies-mt4-product-details"
    ),
    "ig_spread_betting_cfd_product_details": (
        "https://www.ig.com/uk/help-and-support/articles/"
        "682149-what-are-ig-s-spread-betting-and-cfd-product-details-for-each-market"
    ),
}
_HEX = frozenset("0123456789abcdef")


class PreregistrationRefusal(RuntimeError):
    """Stable fail-closed refusal raised before publication."""


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise PreregistrationRefusal("payload_not_canonical") from exc
    return encoded.encode("utf-8")


def canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(character in _HEX for character in text)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def _require_regular_file(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or _is_reparse_point(candidate):
        raise PreregistrationRefusal(f"{label}_not_regular_file")
    try:
        resolved = candidate.resolve(strict=True)
        size = resolved.stat().st_size
    except OSError as exc:
        raise PreregistrationRefusal(f"{label}_missing_or_unreadable") from exc
    if (
        resolved.is_symlink()
        or _is_reparse_point(resolved)
        or not resolved.is_file()
        or size <= 0
        or size > MAXIMUM_INPUT_BYTES
    ):
        raise PreregistrationRefusal(f"{label}_not_regular_file")
    return resolved


def _file_identity(path: str | Path, *, label: str) -> dict[str, Any]:
    resolved = _require_regular_file(path, label=label)
    digest = hashlib.sha256()
    size = 0
    try:
        with resolved.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                if size > MAXIMUM_INPUT_BYTES:
                    raise PreregistrationRefusal(f"{label}_size_invalid")
                digest.update(chunk)
    except OSError as exc:
        raise PreregistrationRefusal(f"{label}_unreadable") from exc
    return {
        "filename": resolved.name,
        "sha256": digest.hexdigest(),
        "size_bytes": size,
    }


def _load_json_object(path: str | Path, *, label: str) -> dict[str, Any]:
    resolved = _require_regular_file(path, label=label)
    try:
        decoded = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreregistrationRefusal(f"{label}_json_invalid") from exc
    if not isinstance(decoded, Mapping):
        raise PreregistrationRefusal(f"{label}_malformed")
    return dict(decoded)


def _finite_nonnegative(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise PreregistrationRefusal(f"{label}_invalid")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise PreregistrationRefusal(f"{label}_invalid") from None
    if not math.isfinite(number) or number < 0.0:
        raise PreregistrationRefusal(f"{label}_invalid")
    return number


def _parse_utc_second(value: Any, *, label: str) -> datetime:
    text = str(value or "").strip()
    if not text.endswith("Z"):
        raise PreregistrationRefusal(f"{label}_invalid")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00").astimezone(UTC)
    except ValueError:
        raise PreregistrationRefusal(f"{label}_invalid") from None
    if parsed.microsecond or _format_utc(parsed) != text:
        raise PreregistrationRefusal(f"{label}_invalid")
    return parsed


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _literal_assignment(path: Path, name: str) -> Any:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise PreregistrationRefusal("collector_source_invalid") from exc
    matches: list[Any] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            if node.value is None:
                raise PreregistrationRefusal(
                    f"collector_{name.lower()}_not_literal"
                )
            try:
                matches.append(ast.literal_eval(node.value))
            except (TypeError, ValueError) as exc:
                raise PreregistrationRefusal(
                    f"collector_{name.lower()}_not_literal"
                ) from exc
    if len(matches) != 1:
        raise PreregistrationRefusal(f"collector_{name.lower()}_missing")
    return matches[0]


def _validate_collector_source() -> dict[str, Any]:
    collector = _require_regular_file(COLLECTOR_PATH, label="collector_source")
    expected = {
        "SOURCE_CONTRACT_ID": screen.SOURCE_CONTRACT_ID,
        "ACTIVITY_METRIC_ID": screen.ACTIVITY_METRIC_ID,
        "SCOPE_VERSION": IG_MT4_SCALP_SCOPE_VERSION,
        "VENUE_ID": IG_MT4_VENUE_ID,
        "PRICE_BASIS": "mt4_bid_ohlc_v1",
        "VOLUME_SOURCE": "mt4_ivolume_tick_count_v1",
        "TIMEFRAME": "M1",
        "SYMBOLS": tuple(IG_MT4_SCALP_SYMBOLS),
    }
    for name, expected_value in expected.items():
        if _literal_assignment(collector, name) != expected_value:
            raise PreregistrationRefusal(
                f"collector_{name.lower()}_contract_mismatch"
            )
    return _file_identity(collector, label="collector_source")


def _validate_screen_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    if tuple(screen.MTVCLC_SYMBOLS) != tuple(IG_MT4_SCALP_SYMBOLS):
        raise PreregistrationRefusal("screen_symbol_scope_mismatch")
    if (
        screen.IMMUTABLE_PRIOR_ATTEMPTED_CELLS != 4_654
        or screen.IMMUTABLE_CURRENT_ATTEMPTED_CELLS != 44
        or screen.IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS != 4_698
    ):
        raise PreregistrationRefusal("screen_attempt_accounting_mismatch")
    if not math.isclose(
        screen.IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION,
        ACCOUNT_CURRENCY_CONVERSION_RATE_FLOOR,
        rel_tol=0.0,
        abs_tol=1e-15,
    ):
        raise PreregistrationRefusal("screen_conversion_charge_mismatch")
    manifest = screen.attempt_manifest()
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise PreregistrationRefusal("screen_attempt_manifest_invalid")
    rollover = configuration.get("rollover_guard")
    if (
        manifest.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or manifest.get("source_contract_id") != screen.SOURCE_CONTRACT_ID
        or configuration.get("maximum_entries_per_symbol_utc_day") != 1
        or configuration.get("execution_type") != "market"
        or configuration.get("pending_orders_forbidden") is not True
        or not isinstance(rollover, Mapping)
        or rollover.get("entry_blackout_utc") != "[20:20:00,22:10:00)"
        or rollover.get("half_open") is not True
    ):
        raise PreregistrationRefusal("screen_attempt_manifest_invalid")
    for field in (
        "success_claim_authorized",
        "holdout_access_authorized",
        "activation_authorized",
        "order_authorized",
    ):
        if manifest.get(field) is not False:
            raise PreregistrationRefusal("screen_attempt_manifest_authority_invalid")
    return manifest, _file_identity(SCREEN_PATH, label="screen_source")


def _validate_cost_capture(
    *, json_path: str | Path, npz_path: str | Path, sealed_at: datetime
) -> tuple[dict[str, Any], dict[str, dict[str, float]]]:
    payload = _load_json_object(json_path, label="cost_capture_json")
    json_identity = _file_identity(json_path, label="cost_capture_json")
    npz_identity = _file_identity(npz_path, label="cost_capture_npz")
    expected_capture_hash = str(payload.get("capture_payload_sha256") or "").lower()
    capture_body = {
        key: value
        for key, value in payload.items()
        if key != "capture_payload_sha256"
    }
    symbol_rows = payload.get("symbols")
    market_source_audit = payload.get("market_source_audit")
    broker_contract_audit = payload.get("broker_contract_audit")
    point_in_time_audit = payload.get("point_in_time_audit")
    try:
        capture_end = float(str(payload.get("capture_end_epoch")))
    except (TypeError, ValueError, OverflowError):
        capture_end = math.nan
    if (
        payload.get("schema_version") != COST_CAPTURE_SCHEMA
        or payload.get("capture_definition")
        != "authenticated_ig_demo_live_quote_calibration.v1"
        or payload.get("source_errors") != []
        or payload.get("venue_id") != IG_MT4_VENUE_ID
        or payload.get("account_mode") != "demo"
        or payload.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or payload.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or payload.get("capture_mode")
        not in {"live_endpoint", "authenticated_same_source_db_history"}
        or not isinstance(symbol_rows, Mapping)
        or set(symbol_rows) != set(IG_MT4_SCALP_SYMBOLS)
        or not isinstance(market_source_audit, Mapping)
        or market_source_audit.get("authenticated") is not True
        or market_source_audit.get("venue_id") != IG_MT4_VENUE_ID
        or market_source_audit.get("account_mode") != "demo"
        or canonical_sha256(market_source_audit)
        != str(payload.get("market_source_audit_sha256") or "").lower()
        or not isinstance(broker_contract_audit, Mapping)
        or canonical_sha256(broker_contract_audit)
        != str(payload.get("broker_contract_snapshot_sha256") or "").lower()
        or not isinstance(point_in_time_audit, Mapping)
        or point_in_time_audit.get("passed") is not True
        or point_in_time_audit.get("errors") != []
        or canonical_sha256(point_in_time_audit)
        != str(payload.get("point_in_time_audit_sha256") or "").lower()
        or not _is_sha256(expected_capture_hash)
        or not hmac.compare_digest(
            expected_capture_hash, canonical_sha256(capture_body)
        )
        or str(payload.get("npz_sha256") or "").lower()
        != npz_identity["sha256"]
        or payload.get("npz_size_bytes") != npz_identity["size_bytes"]
        or not math.isfinite(capture_end)
        or capture_end > sealed_at.timestamp()
    ):
        raise PreregistrationRefusal("cost_capture_contract_invalid")
    for field in (
        "account_scope_sha256",
        "terminal_producer_instance_sha256",
        "market_source_audit_sha256",
        "broker_contract_snapshot_sha256",
        "point_in_time_audit_sha256",
    ):
        if not _is_sha256(payload.get(field)):
            raise PreregistrationRefusal("cost_capture_identity_invalid")
    p90_rows: dict[str, dict[str, float]] = {}
    for symbol in IG_MT4_SCALP_SYMBOLS:
        raw = symbol_rows.get(symbol)
        if not isinstance(raw, Mapping) or raw.get("trade_allowed") is not True:
            raise PreregistrationRefusal(
                f"cost_capture_symbol_invalid:{symbol}"
            )
        p90 = _finite_nonnegative(
            raw.get("p90_observed_spread_bps"),
            label=f"cost_capture_p90_spread:{symbol}",
        )
        if p90 <= 0.0:
            raise PreregistrationRefusal(
                f"cost_capture_p90_spread_invalid:{symbol}"
            )
        p90_rows[symbol] = {"p90_ig_spread_bps": p90}
    return (
        {
            "capture_json": json_identity,
            "capture_npz": npz_identity,
            "capture_payload_sha256": expected_capture_hash,
            "capture_mode": payload["capture_mode"],
            "scope_version": payload["scope_version"],
            "venue_id": payload["venue_id"],
        },
        p90_rows,
    )


def _validated_source_document(
    *, row: Mapping[str, Any], root: Path, role: str, attested_at: datetime
) -> dict[str, Any]:
    if set(row) != {"role", "url", "path", "sha256", "retrieved_at_utc"}:
        raise PreregistrationRefusal(f"fee_source_document_malformed:{role}")
    if row.get("role") != role or row.get("url") != SOURCE_DOCUMENT_URLS[role]:
        raise PreregistrationRefusal(f"fee_source_document_identity_invalid:{role}")
    parsed_url = urlsplit(str(row["url"]))
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "www.ig.com"
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise PreregistrationRefusal(f"fee_source_document_url_invalid:{role}")
    relative = Path(str(row.get("path") or ""))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise PreregistrationRefusal(f"fee_source_document_path_invalid:{role}")
    resolved = _require_regular_file(root / relative, label=f"fee_source:{role}")
    if not resolved.is_relative_to(root):
        raise PreregistrationRefusal(f"fee_source_document_path_escape:{role}")
    identity = _file_identity(resolved, label=f"fee_source:{role}")
    if not _is_sha256(row.get("sha256")) or not hmac.compare_digest(
        identity["sha256"], str(row["sha256"]).lower()
    ):
        raise PreregistrationRefusal(f"fee_source_document_sha256_invalid:{role}")
    retrieved_at = _parse_utc_second(
        row.get("retrieved_at_utc"), label=f"fee_source_retrieved_at:{role}"
    )
    if retrieved_at > attested_at:
        raise PreregistrationRefusal(f"fee_source_retrieved_after_attestation:{role}")
    return {
        "role": role,
        "url": row["url"],
        "retrieved_at_utc": _format_utc(retrieved_at),
        **identity,
    }


def _validate_fee_attestation(
    path: str | Path, *, sealed_at: datetime
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    resolved = _require_regular_file(path, label="fee_attestation")
    payload = _load_json_object(resolved, label="fee_attestation")
    required = {
        "schema_version",
        "venue_id",
        "account_mode",
        "account_currency",
        "scope_version",
        "symbol_scope",
        "effective_at_utc",
        "attested_at_utc",
        "source_errors",
        "source_documents",
        "symbols",
        "operator_attestation_sha256",
    }
    body = {
        key: value
        for key, value in payload.items()
        if key != "operator_attestation_sha256"
    }
    account_currency = str(payload.get("account_currency") or "")
    claimed = str(payload.get("operator_attestation_sha256") or "").lower()
    if (
        set(payload) != required
        or payload.get("schema_version") != FEE_ATTESTATION_SCHEMA
        or payload.get("venue_id") != IG_MT4_VENUE_ID
        or payload.get("account_mode") != "demo"
        or payload.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or payload.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or payload.get("source_errors") != []
        or len(account_currency) != 3
        or not account_currency.isalpha()
        or account_currency != account_currency.upper()
        or not _is_sha256(claimed)
        or not hmac.compare_digest(claimed, canonical_sha256(body))
    ):
        raise PreregistrationRefusal("fee_attestation_contract_invalid")
    effective_at = _parse_utc_second(
        payload.get("effective_at_utc"), label="fee_effective_at"
    )
    attested_at = _parse_utc_second(
        payload.get("attested_at_utc"), label="fee_attested_at"
    )
    if effective_at > attested_at or attested_at > sealed_at:
        raise PreregistrationRefusal("fee_attestation_time_invalid")

    raw_documents = payload.get("source_documents")
    if not isinstance(raw_documents, list) or len(raw_documents) != len(
        SOURCE_DOCUMENT_URLS
    ):
        raise PreregistrationRefusal("fee_source_document_scope_invalid")
    documents_by_role: dict[str, Mapping[str, Any]] = {}
    for row in raw_documents:
        if not isinstance(row, Mapping):
            raise PreregistrationRefusal("fee_source_document_malformed")
        role = str(row.get("role") or "")
        if role in documents_by_role:
            raise PreregistrationRefusal("fee_source_document_role_duplicate")
        documents_by_role[role] = row
    if set(documents_by_role) != set(SOURCE_DOCUMENT_URLS):
        raise PreregistrationRefusal("fee_source_document_scope_invalid")
    source_documents = [
        _validated_source_document(
            row=documents_by_role[role],
            root=resolved.parent,
            role=role,
            attested_at=attested_at,
        )
        for role in SOURCE_DOCUMENT_URLS
    ]

    raw_symbols = payload.get("symbols")
    if not isinstance(raw_symbols, Mapping) or set(raw_symbols) != set(
        IG_MT4_SCALP_SYMBOLS
    ):
        raise PreregistrationRefusal("fee_symbol_scope_invalid")
    fee_rows: dict[str, dict[str, Any]] = {}
    row_fields = {
        "commission_bps_per_round_trip",
        "commission_status",
        "commission_source_role",
        "financing_bps_per_trade",
        "financing_status",
        "financing_source_role",
        "profit_loss_currency",
        "conversion_rate_of_absolute_profit_or_loss",
        "conversion_status",
        "conversion_source_role",
    }
    for symbol in IG_MT4_SCALP_SYMBOLS:
        row = raw_symbols.get(symbol)
        if not isinstance(row, Mapping) or set(row) != row_fields:
            raise PreregistrationRefusal(f"fee_symbol_malformed:{symbol}")
        commission = _finite_nonnegative(
            row.get("commission_bps_per_round_trip"),
            label=f"commission:{symbol}",
        )
        financing = _finite_nonnegative(
            row.get("financing_bps_per_trade"),
            label=f"financing:{symbol}",
        )
        conversion_rate = _finite_nonnegative(
            row.get("conversion_rate_of_absolute_profit_or_loss"),
            label=f"conversion_rate:{symbol}",
        )
        product_role = (
            "ig_mt4_crypto_product_details"
            if symbol in {"BTCUSD", "ETHUSD"}
            else "ig_mt4_forex_product_details"
        )
        if (
            commission > MAXIMUM_ATTESTED_FEE_BPS
            or financing > MAXIMUM_ATTESTED_FEE_BPS
            or row.get("commission_status")
            not in {"explicit_source_attested", "conservative_upper_bound"}
            or row.get("commission_source_role") != product_role
            or row.get("financing_source_role") != product_role
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
            or row.get("profit_loss_currency") != symbol[3:]
            or not math.isclose(
                conversion_rate,
                screen.IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION,
                rel_tol=0.0,
                abs_tol=1e-15,
            )
            or row.get("conversion_status")
            != "debit_absolute_profit_or_loss_when_account_currency_differs"
            or row.get("conversion_source_role")
            != "ig_mt4_forex_product_details"
        ):
            raise PreregistrationRefusal(f"fee_symbol_invalid:{symbol}")
        fee_rows[symbol] = {
            "commission_bps_per_round_trip": commission,
            "financing_bps_per_trade": financing,
            "profit_loss_currency": row["profit_loss_currency"],
            "account_currency": account_currency,
            "conversion_rate_of_absolute_profit_or_loss": conversion_rate,
            "conversion_applies": row["profit_loss_currency"]
            != account_currency,
            "commission_status": row["commission_status"],
            "financing_status": row["financing_status"],
            "conversion_status": row["conversion_status"],
        }
    return (
        {
            "attestation": _file_identity(resolved, label="fee_attestation"),
            "operator_attestation_sha256": claimed,
            "effective_at_utc": _format_utc(effective_at),
            "attested_at_utc": _format_utc(attested_at),
            "account_currency": account_currency,
            "source_documents": source_documents,
        },
        fee_rows,
    )


def _round_up_utc_minute(value: datetime) -> datetime:
    rounded = value.astimezone(UTC).replace(second=0, microsecond=0)
    if rounded <= value:
        rounded += timedelta(minutes=1)
    return rounded


def build_preregistration(
    *,
    cost_capture_json: str | Path,
    cost_capture_npz: str | Path,
    fee_attestation: str | Path,
    sealed_at: datetime,
    start_delay_seconds: int = DEFAULT_START_DELAY_SECONDS,
) -> dict[str, Any]:
    """Build a fully validated authority-free declaration without writing it."""

    sealed_at = sealed_at.astimezone(UTC).replace(microsecond=0)
    if not MINIMUM_START_DELAY_SECONDS <= start_delay_seconds <= (
        MAXIMUM_START_DELAY_SECONDS
    ):
        raise PreregistrationRefusal("prospective_start_delay_invalid")
    if tuple(screen.MTVCLC_SYMBOLS) != tuple(IG_MT4_SCALP_SYMBOLS):
        raise PreregistrationRefusal("scope_identity_mismatch")
    attempt_manifest, screen_identity = _validate_screen_contract()
    collector_identity = _validate_collector_source()
    cost_identity, spread_rows = _validate_cost_capture(
        json_path=cost_capture_json,
        npz_path=cost_capture_npz,
        sealed_at=sealed_at,
    )
    fee_identity, fee_rows = _validate_fee_attestation(
        fee_attestation, sealed_at=sealed_at
    )
    production_engine = production_scalp_engine_identity(
        package_root=FXSTACK_SRC / "fxstack",
        repository_root=REPO_ROOT,
    )
    prospective_t0 = _round_up_utc_minute(
        sealed_at + timedelta(seconds=start_delay_seconds)
    )
    window_end = prospective_t0 + timedelta(days=PROSPECTIVE_WINDOW_DAYS)
    configuration = asdict(screen.GRID[0])
    cost_rows: dict[str, dict[str, Any]] = {}
    for symbol in IG_MT4_SCALP_SYMBOLS:
        p90 = spread_rows[symbol]["p90_ig_spread_bps"]
        fees = fee_rows[symbol]
        pre_conversion = (
            p90
            + fees["commission_bps_per_round_trip"]
            + fees["financing_bps_per_trade"]
            + screen.FIXED_ADVERSE_EXECUTION_DEBIT_BPS
        )
        screen_conversion_fraction = (
            fees["conversion_rate_of_absolute_profit_or_loss"]
            if fees["conversion_applies"]
            else 0.0
        )
        calibration = screen.MT4CostCalibration(
            symbol=symbol,
            p90_spread_bps=p90,
            commission_bps_per_round_trip=fees[
                "commission_bps_per_round_trip"
            ],
            financing_bps_per_trade=fees["financing_bps_per_trade"],
            account_currency=fees["account_currency"],
            pnl_currency=fees["profit_loss_currency"],
            convert_on_close_charge_fraction=screen_conversion_fraction,
            source_sha256=cost_identity["capture_json"]["sha256"],
        )
        if not screen.validate_cost_calibration(
            calibration, expected_symbol=symbol
        ):
            raise PreregistrationRefusal(
                f"screen_cost_calibration_parity_failed:{symbol}"
            )
        cost_rows[symbol] = {
            "p90_ig_spread_bps": p90,
            "commission_bps_per_round_trip": fees[
                "commission_bps_per_round_trip"
            ],
            "financing_bps_per_trade": fees["financing_bps_per_trade"],
            "fixed_adverse_execution_debit_bps": (
                screen.FIXED_ADVERSE_EXECUTION_DEBIT_BPS
            ),
            "pre_conversion_geometry_cost_bps": pre_conversion,
            "profit_loss_currency": fees["profit_loss_currency"],
            "account_currency": fees["account_currency"],
            "conversion_rate_of_absolute_profit_or_loss": fees[
                "conversion_rate_of_absolute_profit_or_loss"
            ],
            "convert_on_close_charge_fraction_for_screen": (
                screen_conversion_fraction
            ),
            "conversion_applies": fees["conversion_applies"],
            "conversion_adjusted_break_even_win_probability": (
                calibration.break_even_win_probability
            ),
            "commission_status": fees["commission_status"],
            "financing_status": fees["financing_status"],
            "conversion_status": fees["conversion_status"],
        }

    body: dict[str, Any] = {
        "schema_version": PREREGISTRATION_SCHEMA,
        "sealed_at_utc": _format_utc(sealed_at),
        "research_only": True,
        "strategy": {
            "strategy_id": screen.STRATEGY_ID,
            "strategy_version": screen.STRATEGY_VERSION,
            "config_id": screen.CONFIG_ID,
            "config_sha256": canonical_sha256(configuration),
            "source_contract_id": screen.SOURCE_CONTRACT_ID,
            "activity_metric_id": screen.ACTIVITY_METRIC_ID,
            "attempt_manifest": attempt_manifest,
            "attempt_manifest_sha256": canonical_sha256(attempt_manifest),
        },
        "scope": {
            "venue_id": IG_MT4_VENUE_ID,
            "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
            "ordered_symbols": list(IG_MT4_SCALP_SYMBOLS),
            "sides": ["BUY", "SELL"],
            "cell_order": [
                {
                    "config_id": screen.CONFIG_ID,
                    "symbol": symbol,
                    "side": side,
                }
                for symbol in IG_MT4_SCALP_SYMBOLS
                for side in ("BUY", "SELL")
            ],
        },
        "attempt_accounting": {
            "prior_attempted_cells_lower_bound": 4_654,
            "current_attempted_cells": 44,
            "cumulative_attempted_cells_lower_bound": 4_698,
        },
        "abandoned_preregistrations": [
            dict(row) for row in ABANDONED_PREREGISTRATION_AUDIT
        ],
        "prospective_window": {
            "t0_utc_inclusive": _format_utc(prospective_t0),
            "end_utc_exclusive": _format_utc(window_end),
            "consecutive_days": PROSPECTIVE_WINDOW_DAYS,
            "fixed_before_any_eligible_observation": True,
            "observations_before_t0_forbidden": True,
            "observations_at_or_after_end_forbidden": True,
            "interim_signal_or_outcome_evaluation_forbidden": True,
            "interim_performance_statistics_forbidden": True,
            "early_success_forbidden": True,
            "success_evaluation_not_before_utc": _format_utc(window_end),
            "no_optional_extension_or_restart_after_failure": True,
            "data_quality_monitoring_must_not_compute_performance": True,
        },
        "execution_contract": {
            "entry_type": "immediate_market",
            "pending_orders_forbidden": True,
            "maximum_entries_per_symbol_utc_day": 1,
            "outcome_horizon_m1_bars": 30,
            "rollover_entry_blackout_utc": "[20:20:00,22:10:00)",
            "rollover_entry_blackout_half_open": True,
            "signals_inside_blackout_reserve": False,
        },
        "cost_policy": {
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
            "symbols": cost_rows,
        },
        "fixed_success_gates": {
            "all_44_cells_must_pass": True,
            "minimum_trades_per_cell": screen.MIN_TRADES_PER_CELL,
            "minimum_independent_utc_days_per_cell": (
                screen.MIN_INDEPENDENT_DAYS_PER_CELL
            ),
            "cell_win_probability_interval": (
                "one_sided_wilson_family_adjusted_over_44_cells"
            ),
            "cell_win_probability_family_confidence": (
                screen.WIN_PROBABILITY_FAMILY_CONFIDENCE
            ),
            "cell_win_probability_lower_bound_strictly_greater_than": (
                "the_exact_per_symbol_conversion_adjusted_break_even_win_"
                "probability_in_cost_policy.symbols"
            ),
            "minimum_unconverted_break_even_win_probability": (
                screen.BASE_COST_BREAK_EVEN_WIN_PROBABILITY
            ),
            "cell_conversion_adjusted_mean_net_bps_strictly_greater_than": 0.0,
            "minimum_total_trades": MINIMUM_TOTAL_TRADES,
            "minimum_total_independent_utc_days": MINIMUM_TOTAL_INDEPENDENT_DAYS,
            "source_scope_ready_required": True,
            "source_errors_required": [],
            "descriptive_df99_bonferroni_abs_t_threshold": (
                screen.BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
            ),
        },
        "source_identities": {
            "screen_source": screen_identity,
            "collector_source": collector_identity,
            "sealer_source": _file_identity(TOOL_PATH, label="sealer_source"),
            "scope_catalog_source": _file_identity(
                CATALOG_PATH, label="scope_catalog_source"
            ),
            "cost_capture": cost_identity,
            "fee_attestation": fee_identity,
            "production_runtime_context": {
                "relationship": (
                    "context_only_successor_not_integrated_or_authorized"
                ),
                "engine_identity": json.loads(
                    canonical_json_bytes(production_engine.to_dict())
                ),
                "active_strategy_family_context": SCALP_DISLOCATION_STRATEGY_ID,
                "active_strategy_version_context": (
                    SCALP_DISLOCATION_STRATEGY_VERSION
                ),
                "active_policy_config_sha256_context": (
                    DislocationPolicy().config_sha256()
                ),
            },
        },
        "isolation_contract": {
            "artifact_grants_no_input_or_outcome_access": True,
            "prospective_collection_is_a_separate_get_only_operator_action": True,
            "outcome_evaluation_requires_a_physically_isolated_research_host": True,
            "production_database_bridge_broker_credentials_registry_and_issuer_"
            "must_not_be_mounted": True,
            "transfer_to_isolation_must_verify_preregistration_body_sha256": True,
        },
        "authority": dict(FIXED_AUTHORITY_FLAGS),
    }
    body["preregistration_body_sha256"] = canonical_sha256(body)
    return body


def validate_preregistration(payload: Mapping[str, Any]) -> bool:
    """Validate the immutable envelope without reading external artifacts."""

    body = dict(payload)
    claimed = str(body.pop("preregistration_body_sha256", "")).lower()
    if not _is_sha256(claimed) or not hmac.compare_digest(
        claimed, canonical_sha256(body)
    ):
        return False
    if (
        body.get("schema_version") != PREREGISTRATION_SCHEMA
        or body.get("research_only") is not True
        or body.get("authority") != FIXED_AUTHORITY_FLAGS
    ):
        return False
    scope = body.get("scope")
    window = body.get("prospective_window")
    execution = body.get("execution_contract")
    accounting = body.get("attempt_accounting")
    if (
        not isinstance(scope, Mapping)
        or scope.get("ordered_symbols") != list(IG_MT4_SCALP_SYMBOLS)
        or len(scope.get("cell_order", [])) != 44
        or accounting
        != {
            "prior_attempted_cells_lower_bound": 4_654,
            "current_attempted_cells": 44,
            "cumulative_attempted_cells_lower_bound": 4_698,
        }
        or body.get("abandoned_preregistrations")
        != [dict(row) for row in ABANDONED_PREREGISTRATION_AUDIT]
        or not isinstance(window, Mapping)
        or window.get("consecutive_days") != PROSPECTIVE_WINDOW_DAYS
        or window.get("early_success_forbidden") is not True
        or not isinstance(execution, Mapping)
        or execution.get("entry_type") != "immediate_market"
        or execution.get("pending_orders_forbidden") is not True
        or execution.get("maximum_entries_per_symbol_utc_day") != 1
        or execution.get("rollover_entry_blackout_utc")
        != "[20:20:00,22:10:00)"
    ):
        return False
    try:
        sealed = _parse_utc_second(body.get("sealed_at_utc"), label="sealed")
        t0 = _parse_utc_second(window.get("t0_utc_inclusive"), label="t0")
        end = _parse_utc_second(window.get("end_utc_exclusive"), label="end")
    except PreregistrationRefusal:
        return False
    return bool(t0 > sealed and end - t0 == timedelta(days=180))


def _validate_output_root(
    output_root: str | Path, *, input_paths: Sequence[str | Path]
) -> Path:
    candidate = Path(output_root).expanduser()
    if candidate.is_symlink() or _is_reparse_point(candidate):
        raise PreregistrationRefusal("output_root_invalid")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise PreregistrationRefusal("output_root_missing") from exc
    if not root.is_dir() or root.is_symlink() or _is_reparse_point(root):
        raise PreregistrationRefusal("output_root_invalid")
    if root.is_relative_to(REPO_ROOT):
        raise PreregistrationRefusal("output_root_inside_repository_forbidden")
    for raw in input_paths:
        resolved = _require_regular_file(raw, label="output_separation_input")
        if root == resolved.parent or root.is_relative_to(resolved.parent):
            raise PreregistrationRefusal("output_root_overlaps_input_root")
    return root


def atomic_publish(
    *, output_root: str | Path, payload: Mapping[str, Any], input_paths: Sequence[str | Path]
) -> Path:
    """Publish a complete file atomically without any overwrite primitive."""

    if not validate_preregistration(payload):
        raise PreregistrationRefusal("preregistration_envelope_invalid")
    root = _validate_output_root(output_root, input_paths=input_paths)
    digest = str(payload["preregistration_body_sha256"])
    target = root / f"mtvclc_v1_preregistration_{digest}.json"
    if target.exists() or target.is_symlink() or _is_reparse_point(target):
        raise PreregistrationRefusal("output_already_exists")
    encoded = (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temp = root / f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    published = False
    try:
        descriptor = os.open(temp, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, target)
        except FileExistsError as exc:
            raise PreregistrationRefusal("output_already_exists") from exc
        except OSError as exc:
            raise PreregistrationRefusal("atomic_no_overwrite_publish_failed") from exc
        published = True
        if target.read_bytes() != encoded:
            raise PreregistrationRefusal("output_verification_failed")
        # A Windows read-only attribute is shared by every hard link to the
        # same file.  Remove the private staging name before making the public
        # name read-only, otherwise the staging link cannot be unlinked and a
        # second apparent preregistration artifact is left behind.
        try:
            temp.unlink()
        except OSError as exc:
            raise PreregistrationRefusal("atomic_temp_cleanup_failed") from exc
        try:
            os.chmod(target, 0o400)
        except OSError as exc:
            raise PreregistrationRefusal("output_read_only_mark_failed") from exc
    except Exception:
        if published:
            try:
                os.chmod(target, 0o600)
                target.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        if temp.exists():
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seal the authority-free MTVCLC-v1 180-day prospective declaration."
        )
    )
    parser.add_argument("--cost-capture-json", required=True)
    parser.add_argument("--cost-capture-npz", required=True)
    parser.add_argument("--fee-attestation", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument(
        "--start-delay-seconds",
        type=int,
        default=DEFAULT_START_DELAY_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = build_preregistration(
            cost_capture_json=args.cost_capture_json,
            cost_capture_npz=args.cost_capture_npz,
            fee_attestation=args.fee_attestation,
            sealed_at=datetime.now(UTC),
            start_delay_seconds=args.start_delay_seconds,
        )
        output = atomic_publish(
            output_root=args.output_root,
            payload=payload,
            input_paths=(
                args.cost_capture_json,
                args.cost_capture_npz,
                args.fee_attestation,
            ),
        )
    except PreregistrationRefusal as exc:
        print(f"preregistration refused: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "preregistration_body_sha256": payload[
                    "preregistration_body_sha256"
                ],
                "artifact_file_sha256": hashlib.sha256(
                    output.read_bytes()
                ).hexdigest(),
                "authority": FIXED_AUTHORITY_FLAGS,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
