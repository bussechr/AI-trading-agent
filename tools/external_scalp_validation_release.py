from __future__ import annotations

"""Prepare and issue production-scalp validation bundles off the runtime host.

This is an external validation/release-host tool, not a production runtime
entrypoint.  It deliberately has no key-generation capability and never
touches a runtime database, bridge, broker, environment file, or activation
state.

The ceremony has two explicit stages:

* ``prepare`` independently validates the exact evidence payload, verifies the
  bytes of all four required evidence artifacts, measures the exact candidate
  engine/config, and writes an immutable unsigned issuance request.
* ``issue`` revalidates that request and the same artifact bytes before loading
  an operator-provisioned Ed25519 key.  It then signs the certificate, rotates
  an append-only signed revocation registry, parity-checks the result with the
  production verifier, and writes an immutable runtime bundle.

The tool cannot invent evidence, weaken a threshold, bootstrap a private key,
arm execution, or install its output into production.
"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import random
from statistics import NormalDist
import sys
import time
from typing import Any, Mapping


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
    ProductionScalpEngineIdentity,
    production_scalp_engine_identity,
)
from fxstack.runtime.scalp_runtime_admission import (  # noqa: E402
    SCALP_VALIDATION_BUNDLE_SCHEMA,
)
from fxstack.runtime.scalp_validation_evidence import (  # noqa: E402
    CERTIFICATE_SHA256_FIELD,
    CERTIFICATE_SIGNATURE_FIELD,
    MAX_CERTIFICATE_VALIDITY_SECS,
    MAX_DRAWDOWN_PCT,
    MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
    MAX_MCPT_P_VALUE,
    MAX_PBO,
    MIN_CELL_INDEPENDENT_DAYS,
    MIN_CELL_TRADES,
    MIN_DSR,
    MIN_INDEPENDENT_DAYS,
    MIN_TRADES,
    REQUIRED_COST_STRESS_MULTIPLE,
    REQUIRED_EVIDENCE_ARTIFACT_SHA256_FIELDS,
    REVOCATION_SHA256_FIELD,
    REVOCATION_SIGNATURE_FIELD,
    SCALP_VALIDATION_CERTIFICATE_SCHEMA,
    SCALP_VALIDATION_EVIDENCE_SCHEMA,
    SCALP_VALIDATION_REVOCATION_SCHEMA,
    ScalpValidationExpectation,
    WIN_PROBABILITY_CI_METHOD,
    WIN_PROBABILITY_FAMILY_CONFIDENCE,
    canonical_sha256,
    certificate_body_sha256,
    ed25519_public_key_id,
    revocation_body_sha256,
    verify_scalp_validation_evidence,
)
from fxstack.strategy.scalp_dislocation import (  # noqa: E402
    SCALP_DISLOCATION_STRATEGY_ID,
    SCALP_DISLOCATION_STRATEGY_VERSION,
    DislocationPolicy,
)


ISSUANCE_REQUEST_SCHEMA = "fxstack.external_scalp_validation_issuance_request.v1"
ISSUANCE_REQUEST_SHA256_FIELD = "request_sha256"
TRADE_LEDGER_SCHEMA = "fxstack.external_scalp_trade_ledger.v1"
COST_MODEL_SCHEMA = "fxstack.external_scalp_cost_model.v2"
SOURCE_QUOTE_CAPTURE_SCHEMA = "fxstack.external_dukascopy_bid_ask_m1_source.v1"
IG_CALIBRATION_CAPTURE_SCHEMA = "fxstack.external_ig_mt4_bid_ask_capture.v1"
FEE_SCHEDULE_SCHEMA = "fxstack.external_ig_mt4_fee_schedule.v1"
IG_MARKET_SOURCE_AUDIT_SCHEMA = "fxstack.external_ig_mt4_market_source_audit.v1"
IG_BROKER_CONTRACT_AUDIT_SCHEMA = "fxstack.external_ig_mt4_contract_audit.v1"
IG_POINT_IN_TIME_AUDIT_SCHEMA = (
    "fxstack.external_ig_mt4_capture_point_in_time_audit.v1"
)
IG_EXECUTION_CONTRACT_SCHEMA = "fxstack.production_scalp_execution_tolerance.v1"
AUTHENTICATED_MARKET_SOURCE_SCHEMA = "fxstack_authenticated_broker_market_source_v2"
STATISTICAL_REPORT_SCHEMA = "fxstack.external_scalp_statistical_report.v1"
CELL_EVIDENCE_SCHEMA = "fxstack.external_scalp_cell_evidence.v1"
ATTEMPT_MANIFEST_SCHEMA = "fxstack.external_scalp_attempt_manifest.v1"
SOURCE_QUOTE_DEFINITION = "dukascopy_executable_bid_ask_m1.v1"
SOURCE_SAMPLING_CONTRACT = (
    "utc_epoch_mod_300_plus_all_decision_and_fill_minutes.v1"
)
IG_CALIBRATION_DEFINITION = "authenticated_ig_demo_live_quote_calibration.v1"
COST_DEFINITION = (
    "max_source_or_ig_p90_spread_plus_adverse_next_open_slippage_fees.v1"
)
RISK_SIZING_METHOD = "production_runtime_risk_kernel_replay.v1"
WIN_DEFINITION = "full_target_hit_first.v1"
MCPT_METHOD = "panel_common_circular_shift_sharpe.v1"
PBO_DSR_METHOD = "cscv_pbo_deflated_sharpe_complete_trials.v1"
CLUSTERED_CI_METHOD = "utc_day_clustered_bootstrap_mean_95pct.v1"
CLUSTERED_CI_SEED = 1337
CLUSTERED_CI_RESAMPLES = 5_000
MIN_MCPT_PERMUTATIONS = 999
PRODUCTION_MAX_SLIPPAGE_POINTS = 20
MIN_SOURCE_QUOTE_SAMPLES_PER_SYMBOL = 200
MIN_SOURCE_QUOTE_DAYS_PER_SYMBOL = 60
MIN_IG_CALIBRATION_SAMPLES_PER_SYMBOL = 300
MIN_IG_HISTORY_SAMPLES_PER_SYMBOL = 100
MIN_IG_CALIBRATION_DURATION_SECS = 300.0
MAX_IG_CALIBRATION_SAMPLE_GAP_SECS = 5.0
IG_CLOCK_TOLERANCE_SECS = 5.0
IG_EVENT_ORDER_TOLERANCE_SECS = 1e-6
MAX_EVIDENCE_JSON_BYTES = 4 * 1024 * 1024
MAX_REQUEST_JSON_BYTES = 8 * 1024 * 1024
MAX_KEY_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
_FLOAT_TOLERANCE = 1e-12
_CELL_ALPHA = (1.0 - WIN_PROBABILITY_FAMILY_CONFIDENCE) / (
    len(IG_MT4_SCALP_SYMBOLS) * 2
)
_CELL_Z = NormalDist().inv_cdf(1.0 - _CELL_ALPHA)


class ReleaseRefusal(RuntimeError):
    """Stable fail-closed refusal raised before any output is published."""


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _strict_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or numeric < 0.0 or not numeric.is_integer():
        return None
    return int(numeric)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def _require_regular_file(path: str | Path, *, label: str, limit: int) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or _is_reparse_point(candidate):
        raise ReleaseRefusal(f"{label}_not_regular_file")
    resolved = candidate.resolve(strict=True)
    if resolved.is_symlink() or _is_reparse_point(resolved) or not resolved.is_file():
        raise ReleaseRefusal(f"{label}_not_regular_file")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ReleaseRefusal(f"{label}_unreadable") from exc
    if size <= 0 or size > limit:
        raise ReleaseRefusal(f"{label}_size_invalid")
    return resolved


def _read_bounded(path: str | Path, *, label: str, limit: int) -> bytes:
    resolved = _require_regular_file(path, label=label, limit=limit)
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise ReleaseRefusal(f"{label}_unreadable") from exc
    if not payload or len(payload) > limit:
        raise ReleaseRefusal(f"{label}_size_invalid")
    return payload


def _resolve_source_file(
    *,
    source_root: str | Path,
    relative_path: Any,
    label: str,
) -> Path:
    root_candidate = Path(source_root).expanduser()
    if root_candidate.is_symlink() or _is_reparse_point(root_candidate):
        raise ReleaseRefusal("validation_source_root_invalid")
    root = root_candidate.resolve(strict=True)
    if not root.is_dir() or root.is_symlink() or _is_reparse_point(root):
        raise ReleaseRefusal("validation_source_root_invalid")
    raw = str(relative_path or "").strip()
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise ReleaseRefusal(f"{label}_path_invalid")
    resolved = _require_regular_file(
        root / relative,
        label=label,
        limit=MAX_ARTIFACT_BYTES,
    )
    if not resolved.is_relative_to(root):
        raise ReleaseRefusal(f"{label}_path_escape")
    return resolved


def _load_json_object(path: str | Path, *, label: str, limit: int) -> dict[str, Any]:
    payload = _read_bounded(path, label=label, limit=limit)
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ReleaseRefusal(f"{label}_encoding_invalid") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseRefusal(f"{label}_json_invalid") from exc
    if not isinstance(decoded, Mapping):
        raise ReleaseRefusal(f"{label}_malformed")
    return dict(decoded)


def _file_identity(path: str | Path, *, label: str) -> tuple[Path, str, int]:
    resolved = _require_regular_file(path, label=label, limit=MAX_ARTIFACT_BYTES)
    digest = hashlib.sha256()
    size = 0
    try:
        with resolved.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise ReleaseRefusal(f"{label}_size_invalid")
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseRefusal(f"{label}_unreadable") from exc
    if size <= 0:
        raise ReleaseRefusal(f"{label}_size_invalid")
    return resolved, digest.hexdigest(), size


def _request_body_sha256(request: Mapping[str, Any]) -> str:
    body = {
        key: value
        for key, value in dict(request).items()
        if key != ISSUANCE_REQUEST_SHA256_FIELD
    }
    return canonical_sha256(body)


def _write_new_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    target = Path(path).expanduser().resolve(strict=False)
    parent = target.parent.resolve(strict=True)
    if not parent.is_dir() or parent.is_symlink() or _is_reparse_point(parent):
        raise ReleaseRefusal("output_parent_invalid")
    if target.parent.resolve(strict=True) != parent:
        raise ReleaseRefusal("output_parent_changed")
    if target.exists() or target.is_symlink() or _is_reparse_point(target):
        raise ReleaseRefusal("output_already_exists")
    encoded = (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise ReleaseRefusal("output_already_exists") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if target.is_symlink() or _is_reparse_point(target) or target.parent != parent:
        raise ReleaseRefusal("output_identity_invalid")
    if target.read_bytes() != encoded:
        raise ReleaseRefusal("output_verification_failed")
    return target


def _independent_wilson_interval(*, wins: int, trades: int) -> tuple[float, float]:
    if trades <= 0 or wins < 0 or wins > trades:
        return 0.0, 0.0
    point = wins / trades
    z_sq = _CELL_Z * _CELL_Z
    denominator = 1.0 + z_sq / trades
    center = point + z_sq / (2.0 * trades)
    margin = _CELL_Z * math.sqrt(
        point * (1.0 - point) / trades + z_sq / (4.0 * trades * trades)
    )
    return (
        float(max(0.0, min(1.0, (center - margin) / denominator))),
        float(max(0.0, min(1.0, (center + margin) / denominator))),
    )


def _close(left: float, right: float, *, tolerance: float = 1e-9) -> bool:
    return math.isclose(
        float(left),
        float(right),
        rel_tol=tolerance,
        abs_tol=tolerance,
    )


def _utc_day(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError) as exc:
        raise ReleaseRefusal("validation_trade_epoch_invalid") from exc


def _load_primary_artifacts(
    artifact_paths: Mapping[str, str | Path],
) -> tuple[
    dict[str, Path],
    dict[str, dict[str, Any]],
    dict[str, str],
    dict[str, dict[str, Any]],
]:
    if set(artifact_paths) != set(REQUIRED_EVIDENCE_ARTIFACT_SHA256_FIELDS):
        raise ReleaseRefusal("validation_artifact_path_scope_invalid")
    resolved_paths: dict[str, Path] = {}
    payloads: dict[str, dict[str, Any]] = {}
    digests: dict[str, str] = {}
    manifest: dict[str, dict[str, Any]] = {}
    unique_paths: set[Path] = set()
    for role in REQUIRED_EVIDENCE_ARTIFACT_SHA256_FIELDS:
        resolved = _require_regular_file(
            artifact_paths[role],
            label=f"validation_artifact_{role}",
            limit=MAX_ARTIFACT_BYTES,
        )
        if resolved in unique_paths:
            raise ReleaseRefusal("validation_artifact_paths_not_distinct")
        unique_paths.add(resolved)
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            raise ReleaseRefusal(f"validation_artifact_{role}_unreadable") from exc
        if not raw or len(raw) > MAX_ARTIFACT_BYTES:
            raise ReleaseRefusal(f"validation_artifact_{role}_size_invalid")
        digest = hashlib.sha256(raw).hexdigest()
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ReleaseRefusal(
                f"validation_artifact_{role}_encoding_invalid"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ReleaseRefusal(f"validation_artifact_{role}_json_invalid") from exc
        if not isinstance(decoded, Mapping):
            raise ReleaseRefusal(f"validation_artifact_{role}_malformed")
        resolved_paths[role] = resolved
        payloads[role] = dict(decoded)
        digests[role] = digest
        manifest[role] = {"sha256": digest, "size_bytes": len(raw)}
    return resolved_paths, payloads, digests, manifest


def _validate_cost_model(
    payload: Any,
    *,
    model_path: Path,
    source_root: str | Path,
) -> dict[str, Any]:
    import numpy as np

    if not isinstance(payload, Mapping):
        raise ReleaseRefusal("validation_cost_model_malformed")
    expected_fields = {
        "schema_version",
        "source_errors",
        "venue_id",
        "symbol_scope",
        "source_quote_definition",
        "source_sampling_contract",
        "ig_calibration_definition",
        "cost_definition",
        "base_cost_multiplier",
        "two_x_cost_multiplier",
        "execution_max_slippage_points",
        "components",
        "source_quote_npz_path",
        "source_quote_npz_sha256",
        "source_snapshot_sha256",
        "ig_calibration_npz_path",
        "ig_calibration_npz_sha256",
        "fee_schedule_path",
        "fee_schedule_sha256",
        "source_capture",
        "ig_capture",
        "symbols",
    }
    if set(payload) != expected_fields:
        raise ReleaseRefusal("validation_cost_model_scope_invalid")
    if payload.get("schema_version") != COST_MODEL_SCHEMA:
        raise ReleaseRefusal("validation_cost_model_schema_invalid")
    if payload.get("source_errors") != []:
        raise ReleaseRefusal("validation_cost_model_source_errors_present")
    if payload.get("venue_id") != IG_MT4_VENUE_ID:
        raise ReleaseRefusal("validation_cost_model_venue_invalid")
    if payload.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS):
        raise ReleaseRefusal("validation_cost_model_symbol_scope_invalid")
    if payload.get("source_quote_definition") != SOURCE_QUOTE_DEFINITION:
        raise ReleaseRefusal("validation_cost_model_source_quote_definition_invalid")
    if payload.get("source_sampling_contract") != SOURCE_SAMPLING_CONTRACT:
        raise ReleaseRefusal("validation_cost_model_source_sampling_contract_invalid")
    if payload.get("ig_calibration_definition") != IG_CALIBRATION_DEFINITION:
        raise ReleaseRefusal("validation_cost_model_ig_calibration_definition_invalid")
    if payload.get("cost_definition") != COST_DEFINITION:
        raise ReleaseRefusal("validation_cost_model_definition_invalid")
    base_multiplier = _finite_float(payload.get("base_cost_multiplier"))
    doubled_multiplier = _finite_float(payload.get("two_x_cost_multiplier"))
    if (
        base_multiplier is None
        or not _close(base_multiplier, 1.0, tolerance=_FLOAT_TOLERANCE)
        or doubled_multiplier is None
        or not _close(
            doubled_multiplier,
            REQUIRED_COST_STRESS_MULTIPLE,
            tolerance=_FLOAT_TOLERANCE,
        )
    ):
        raise ReleaseRefusal("validation_cost_model_multiplier_invalid")
    if (
        _strict_nonnegative_int(payload.get("execution_max_slippage_points"))
        != PRODUCTION_MAX_SLIPPAGE_POINTS
    ):
        raise ReleaseRefusal("validation_cost_model_slippage_contract_invalid")
    components = payload.get("components")
    required_components = {
        "source_bid_ask",
        "ig_cost_pad",
        "slippage",
        "commission",
        "financing",
    }
    if (
        not isinstance(components, Mapping)
        or set(components) != required_components
        or any(components[name] is not True for name in required_components)
    ):
        raise ReleaseRefusal("validation_cost_model_components_invalid")

    fee_schedule_path = _resolve_statistical_sidecar(
        report_path=model_path,
        relative_path=payload.get("fee_schedule_path"),
        expected_sha256=payload.get("fee_schedule_sha256"),
        label="validation_cost_model_fee_schedule",
    )
    fee_schedule = _load_json_object(
        fee_schedule_path,
        label="validation_cost_model_fee_schedule",
        limit=MAX_REQUEST_JSON_BYTES,
    )
    expected_fee_fields = {
        "schema_version",
        "source_errors",
        "venue_id",
        "account_mode",
        "symbol_scope",
        "effective_at_epoch",
        "source_document_path",
        "source_document_sha256",
        "operator_attestation_sha256",
        "symbols",
    }
    if set(fee_schedule) != expected_fee_fields:
        raise ReleaseRefusal("validation_cost_model_fee_schedule_scope_invalid")
    fee_effective_at = _finite_float(fee_schedule.get("effective_at_epoch"))
    fee_attestation = str(
        fee_schedule.get("operator_attestation_sha256") or ""
    ).lower()
    fee_body = {
        key: value
        for key, value in fee_schedule.items()
        if key != "operator_attestation_sha256"
    }
    if (
        fee_schedule.get("schema_version") != FEE_SCHEDULE_SCHEMA
        or fee_schedule.get("source_errors") != []
        or fee_schedule.get("venue_id") != IG_MT4_VENUE_ID
        or fee_schedule.get("account_mode") != "demo"
        or fee_schedule.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or fee_effective_at is None
        or fee_effective_at <= 0.0
        or not _is_sha256(fee_schedule.get("source_document_sha256"))
        or not _is_sha256(fee_attestation)
        or not hmac.compare_digest(fee_attestation, canonical_sha256(fee_body))
    ):
        raise ReleaseRefusal("validation_cost_model_fee_schedule_invalid")
    fee_source_document = _resolve_statistical_sidecar(
        report_path=fee_schedule_path,
        relative_path=fee_schedule.get("source_document_path"),
        expected_sha256=fee_schedule.get("source_document_sha256"),
        label="validation_cost_model_fee_source_document",
    )
    if fee_source_document == fee_schedule_path:
        raise ReleaseRefusal("validation_cost_model_fee_inputs_not_distinct")
    raw_fee_rows = fee_schedule.get("symbols")
    if not isinstance(raw_fee_rows, Mapping) or set(raw_fee_rows) != set(
        IG_MT4_SCALP_SYMBOLS
    ):
        raise ReleaseRefusal("validation_cost_model_fee_symbol_scope_invalid")
    fee_rows: dict[str, dict[str, float]] = {}
    for symbol in IG_MT4_SCALP_SYMBOLS:
        raw_fee_row = raw_fee_rows.get(symbol)
        if not isinstance(raw_fee_row, Mapping) or set(raw_fee_row) != {
            "commission_bps_per_round_trip",
            "configured_financing_bps_per_trade",
        }:
            raise ReleaseRefusal(
                f"validation_cost_model_fee_symbol_malformed:{symbol}"
            )
        commission = _finite_float(
            raw_fee_row.get("commission_bps_per_round_trip")
        )
        financing = _finite_float(
            raw_fee_row.get("configured_financing_bps_per_trade")
        )
        if (
            commission is None
            or commission < 0.0
            or financing is None
            or financing < 0.0
        ):
            raise ReleaseRefusal(
                f"validation_cost_model_fee_symbol_invalid:{symbol}"
            )
        fee_rows[symbol] = {
            "commission_bps_per_round_trip": commission,
            "configured_financing_bps_per_trade": financing,
        }

    source_capture = payload.get("source_capture")
    if not isinstance(source_capture, Mapping) or set(source_capture) != {
        "schema_version",
        "source",
        "created_at_epoch",
        "source_snapshot_sha256",
        "source_snapshot_manifest",
        "source_file_paths",
        "point_in_time_audit_path",
        "point_in_time_audit_sha256",
        "source_errors",
    }:
        raise ReleaseRefusal("validation_cost_model_source_capture_malformed")
    source_created_at = _finite_float(source_capture.get("created_at_epoch"))
    if (
        source_capture.get("schema_version") != SOURCE_QUOTE_CAPTURE_SCHEMA
        or source_capture.get("source") != "dukascopy"
        or source_created_at is None
        or source_created_at <= 0.0
        or not _is_sha256(source_capture.get("source_snapshot_sha256"))
        or not _is_sha256(source_capture.get("point_in_time_audit_sha256"))
        or source_capture.get("source_errors") != []
    ):
        raise ReleaseRefusal("validation_cost_model_source_capture_invalid")
    source_snapshot = source_capture.get("source_snapshot_manifest")
    if not isinstance(source_snapshot, Mapping) or set(source_snapshot) != {
        "symbol_scope",
        "start_epoch",
        "end_epoch",
        "files",
    }:
        raise ReleaseRefusal("validation_cost_model_source_snapshot_malformed")
    snapshot_start = _strict_nonnegative_int(source_snapshot.get("start_epoch"))
    snapshot_end = _strict_nonnegative_int(source_snapshot.get("end_epoch"))
    snapshot_files = source_snapshot.get("files")
    if (
        source_snapshot.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or snapshot_start is None
        or snapshot_end is None
        or snapshot_start <= 0
        or snapshot_end <= snapshot_start
        or not isinstance(snapshot_files, list)
        or len(snapshot_files) != len(IG_MT4_SCALP_SYMBOLS)
    ):
        raise ReleaseRefusal("validation_cost_model_source_snapshot_invalid")
    composite_sha = canonical_sha256(dict(source_snapshot))
    if (
        str(source_capture.get("source_snapshot_sha256") or "").lower()
        != composite_sha
        or str(payload.get("source_snapshot_sha256") or "").lower()
        != composite_sha
    ):
        raise ReleaseRefusal("validation_cost_model_source_snapshot_sha256_invalid")
    source_file_paths = source_capture.get("source_file_paths")
    if not isinstance(source_file_paths, Mapping) or set(source_file_paths) != set(
        IG_MT4_SCALP_SYMBOLS
    ):
        raise ReleaseRefusal("validation_cost_model_source_file_paths_invalid")
    source_file_sha256: dict[str, str] = {}
    resolved_source_files: set[Path] = set()
    for index, symbol in enumerate(IG_MT4_SCALP_SYMBOLS):
        file_row = snapshot_files[index]
        if not isinstance(file_row, Mapping) or set(file_row) != {
            "symbol",
            "sha256",
            "size",
        }:
            raise ReleaseRefusal(
                f"validation_cost_model_source_snapshot_file_invalid:{symbol}"
            )
        digest = str(file_row.get("sha256") or "").lower()
        size = _strict_nonnegative_int(file_row.get("size"))
        if file_row.get("symbol") != symbol or not _is_sha256(digest) or not size:
            raise ReleaseRefusal(
                f"validation_cost_model_source_snapshot_file_invalid:{symbol}"
            )
        source_file = _resolve_source_file(
            source_root=source_root,
            relative_path=source_file_paths.get(symbol),
            label=f"validation_cost_model_source_file_{symbol}",
        )
        _, actual_digest, actual_size = _file_identity(
            source_file,
            label=f"validation_cost_model_source_file_{symbol}",
        )
        if (
            source_file in resolved_source_files
            or actual_size != size
            or not hmac.compare_digest(actual_digest, digest)
        ):
            raise ReleaseRefusal(
                f"validation_cost_model_source_snapshot_file_invalid:{symbol}"
            )
        resolved_source_files.add(source_file)
        source_file_sha256[symbol] = digest
    _resolve_statistical_sidecar(
        report_path=model_path,
        relative_path=source_capture.get("point_in_time_audit_path"),
        expected_sha256=source_capture.get("point_in_time_audit_sha256"),
        label="validation_cost_model_point_in_time_audit",
    )
    ig_capture = payload.get("ig_capture")
    expected_ig_capture_fields = {
        "schema_version",
        "capture_definition",
        "capture_mode",
        "source_errors",
        "venue_id",
        "account_mode",
        "source_id",
        "source_version",
        "scope_version",
        "symbol_scope",
        "capture_start_epoch",
        "capture_end_epoch",
        "created_at_epoch",
        "account_scope_sha256",
        "terminal_producer_instance_sha256",
        "market_source_audit",
        "market_source_audit_sha256",
        "broker_contract_audit",
        "broker_contract_snapshot_sha256",
        "point_in_time_audit",
        "point_in_time_audit_sha256",
        "execution_contract",
        "npz_path",
        "npz_sha256",
        "npz_size_bytes",
        "npz_arrays",
        "symbols",
        "capture_payload_sha256",
    }
    if not isinstance(ig_capture, Mapping) or set(ig_capture) != expected_ig_capture_fields:
        raise ReleaseRefusal("validation_cost_model_ig_capture_malformed")
    ig_start = _finite_float(ig_capture.get("capture_start_epoch"))
    ig_end = _finite_float(ig_capture.get("capture_end_epoch"))
    ig_created = _finite_float(ig_capture.get("created_at_epoch"))
    capture_mode = str(ig_capture.get("capture_mode") or "").strip()
    history_capture = capture_mode == "authenticated_same_source_db_history"
    live_capture = capture_mode == "live_endpoint"
    source_id = str(ig_capture.get("source_id") or "").strip()
    source_version = str(ig_capture.get("source_version") or "").strip()
    raw_capture_body = {
        key: value
        for key, value in dict(ig_capture).items()
        if key != "capture_payload_sha256"
    }
    capture_payload_sha = str(
        ig_capture.get("capture_payload_sha256") or ""
    ).lower()
    market_source_audit = ig_capture.get("market_source_audit")
    broker_contract_audit = ig_capture.get("broker_contract_audit")
    point_in_time_audit = ig_capture.get("point_in_time_audit")
    execution_contract = ig_capture.get("execution_contract")
    raw_npz_arrays = ig_capture.get("npz_arrays")
    declared_npz_array_names = (
        set(raw_npz_arrays)
        if isinstance(raw_npz_arrays, Mapping)
        else set(raw_npz_arrays)
        if isinstance(raw_npz_arrays, list)
        and all(isinstance(item, str) for item in raw_npz_arrays)
        else set()
    )
    required_ig_arrays = {
        "symbol_index",
        "sample_epoch",
        "broker_quote_epoch",
        "received_at_epoch",
        "market_event_received_at_epoch",
        "source_event_sequence",
        "source_event_token_sha256",
        "bid",
        "ask",
        "point",
        "price_tick_size",
        "digits",
        "trade_allowed",
    }
    expected_ig_dtype_declaration = {
        "symbol_index": "int64",
        "sample_epoch": "float64",
        "broker_quote_epoch": "float64",
        "received_at_epoch": "float64",
        "market_event_received_at_epoch": "float64",
        "source_event_sequence": "int64",
        "source_event_token_sha256": "S64",
        "bid": "float64",
        "ask": "float64",
        "point": "float64",
        "price_tick_size": "float64",
        "digits": "int64",
        "trade_allowed": "bool",
    }
    expected_market_source_audit_fields = {
        "schema_version",
        "authenticated",
        "venue_id",
        "account_mode",
        "market_source_schema",
        "market_source_id_sha256",
        "account_scope_sha256",
        "producer_identity_sha256",
        "terminal_producer_instance_sha256",
        "terminal_lease_scope_sha256",
        "credential_generation_id_sha256",
        "bridge_protocol_version",
        "identity_observation_count",
        "first_observed_epoch",
        "last_observed_epoch",
        "identity_observation_chain_sha256",
    }
    expected_broker_contract_audit_fields = {
        "schema_version",
        "market_source_id_sha256",
        "observation_count",
        "first_observed_epoch",
        "last_observed_epoch",
        "contract_observation_chain_sha256",
        "symbols",
    }
    expected_point_in_time_audit_fields = {
        "schema_version",
        "passed",
        "errors",
        "minimum_samples_per_symbol",
        "minimum_duration_secs",
        "maximum_sample_gap_secs",
        "maximum_sample_gap_enforced",
        "requires_fresh_authenticated_source_events",
        "latest_scope_market_event_fresh",
        "database_read_only",
        "sample_source",
        "symbols",
    }
    expected_execution_contract_fields = {
        "schema_version",
        "max_slippage_points",
        "semantics",
        "used_as_observed_cost",
    }
    market_first = (
        _finite_float(market_source_audit.get("first_observed_epoch"))
        if isinstance(market_source_audit, Mapping)
        else None
    )
    market_last = (
        _finite_float(market_source_audit.get("last_observed_epoch"))
        if isinstance(market_source_audit, Mapping)
        else None
    )
    broker_first = (
        _finite_float(broker_contract_audit.get("first_observed_epoch"))
        if isinstance(broker_contract_audit, Mapping)
        else None
    )
    broker_last = (
        _finite_float(broker_contract_audit.get("last_observed_epoch"))
        if isinstance(broker_contract_audit, Mapping)
        else None
    )
    audit_minimum_samples = (
        _strict_nonnegative_int(
            point_in_time_audit.get("minimum_samples_per_symbol")
        )
        if isinstance(point_in_time_audit, Mapping)
        else None
    )
    audit_minimum_duration = (
        _finite_float(point_in_time_audit.get("minimum_duration_secs"))
        if isinstance(point_in_time_audit, Mapping)
        else None
    )
    audit_maximum_gap = (
        _finite_float(point_in_time_audit.get("maximum_sample_gap_secs"))
        if isinstance(point_in_time_audit, Mapping)
        else None
    )
    if (
        ig_capture.get("schema_version") != IG_CALIBRATION_CAPTURE_SCHEMA
        or ig_capture.get("capture_definition") != IG_CALIBRATION_DEFINITION
        or not (live_capture or history_capture)
        or ig_capture.get("venue_id") != IG_MT4_VENUE_ID
        or ig_capture.get("account_mode") != "demo"
        or source_id != "authenticated_ig_demo_mt4_bridge"
        or not source_version
        or len(source_version) > 128
        or ig_capture.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or ig_capture.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS)
        or ig_start is None
        or ig_end is None
        or ig_created is None
        or ig_start <= 0.0
        or ig_end < ig_start
        or (
            live_capture
            and ig_end - ig_start < MIN_IG_CALIBRATION_DURATION_SECS
        )
        or ig_created < ig_end
        or fee_effective_at > ig_end
        or any(
            not _is_sha256(ig_capture.get(field))
            for field in (
                "account_scope_sha256",
                "terminal_producer_instance_sha256",
                "market_source_audit_sha256",
                "broker_contract_snapshot_sha256",
                "point_in_time_audit_sha256",
                "npz_sha256",
                "capture_payload_sha256",
            )
        )
        or ig_capture.get("source_errors") != []
        or not isinstance(market_source_audit, Mapping)
        or set(market_source_audit) != expected_market_source_audit_fields
        or market_source_audit.get("schema_version")
        != IG_MARKET_SOURCE_AUDIT_SCHEMA
        or market_source_audit.get("authenticated") is not True
        or market_source_audit.get("venue_id") != IG_MT4_VENUE_ID
        or market_source_audit.get("account_mode") != "demo"
        or market_source_audit.get("market_source_schema")
        != AUTHENTICATED_MARKET_SOURCE_SCHEMA
        or market_source_audit.get("bridge_protocol_version") != source_version
        or market_source_audit.get("account_scope_sha256")
        != ig_capture.get("account_scope_sha256")
        or market_source_audit.get("terminal_producer_instance_sha256")
        != ig_capture.get("terminal_producer_instance_sha256")
        or any(
            not _is_sha256(market_source_audit.get(field))
            for field in (
                "market_source_id_sha256",
                "account_scope_sha256",
                "producer_identity_sha256",
                "terminal_producer_instance_sha256",
                "terminal_lease_scope_sha256",
                "credential_generation_id_sha256",
                "identity_observation_chain_sha256",
            )
        )
        or (
            _strict_nonnegative_int(
                market_source_audit.get("identity_observation_count")
            )
            or 0
        )
        < (7 if history_capture else 4)
        or market_first is None
        or market_last is None
        or market_first < ig_start
        or market_last > ig_end
        or market_last < market_first
        or (
            history_capture
            and (
                abs(market_first - ig_start) > IG_CLOCK_TOLERANCE_SECS
                or abs(market_last - ig_end) > IG_CLOCK_TOLERANCE_SECS
            )
        )
        or canonical_sha256(dict(market_source_audit))
        != str(ig_capture.get("market_source_audit_sha256") or "").lower()
        or not isinstance(broker_contract_audit, Mapping)
        or set(broker_contract_audit) != expected_broker_contract_audit_fields
        or broker_contract_audit.get("schema_version")
        != IG_BROKER_CONTRACT_AUDIT_SCHEMA
        or broker_contract_audit.get("market_source_id_sha256")
        != market_source_audit.get("market_source_id_sha256")
        or not _is_sha256(
            broker_contract_audit.get("contract_observation_chain_sha256")
        )
        or (
            _strict_nonnegative_int(
                broker_contract_audit.get("observation_count")
            )
            or 0
        )
        < 2
        or broker_first is None
        or broker_last is None
        or broker_first < ig_start
        or broker_last > ig_end
        or broker_last < broker_first
        or (
            history_capture
            and (
                abs(broker_first - ig_start) > IG_CLOCK_TOLERANCE_SECS
                or abs(broker_last - ig_end) > IG_CLOCK_TOLERANCE_SECS
            )
        )
        or canonical_sha256(dict(broker_contract_audit))
        != str(ig_capture.get("broker_contract_snapshot_sha256") or "").lower()
        or not isinstance(point_in_time_audit, Mapping)
        or set(point_in_time_audit) != expected_point_in_time_audit_fields
        or point_in_time_audit.get("schema_version")
        != IG_POINT_IN_TIME_AUDIT_SCHEMA
        or point_in_time_audit.get("passed") is not True
        or point_in_time_audit.get("errors") != []
        or (audit_minimum_samples or 0)
        < (
            MIN_IG_HISTORY_SAMPLES_PER_SYMBOL
            if history_capture
            else MIN_IG_CALIBRATION_SAMPLES_PER_SYMBOL
        )
        or (audit_minimum_duration or -1.0) < MIN_IG_CALIBRATION_DURATION_SECS
        or (audit_maximum_gap or -1.0) <= 0.0
        or (
            live_capture
            and float(audit_maximum_gap or float("inf"))
            > MAX_IG_CALIBRATION_SAMPLE_GAP_SECS
        )
        or point_in_time_audit.get("maximum_sample_gap_enforced")
        is not live_capture
        or point_in_time_audit.get(
            "requires_fresh_authenticated_source_events"
        ) is not True
        or point_in_time_audit.get("latest_scope_market_event_fresh") is not True
        or point_in_time_audit.get("database_read_only") is not history_capture
        or point_in_time_audit.get("sample_source") != capture_mode
        or canonical_sha256(dict(point_in_time_audit))
        != str(ig_capture.get("point_in_time_audit_sha256") or "").lower()
        or not isinstance(execution_contract, Mapping)
        or set(execution_contract) != expected_execution_contract_fields
        or execution_contract.get("schema_version")
        != IG_EXECUTION_CONTRACT_SCHEMA
        or _strict_nonnegative_int(execution_contract.get("max_slippage_points"))
        != PRODUCTION_MAX_SLIPPAGE_POINTS
        or execution_contract.get("semantics")
        != "configured_broker_execution_tolerance_not_observed_slippage"
        or execution_contract.get("used_as_observed_cost") is not False
        or declared_npz_array_names != required_ig_arrays
        or raw_npz_arrays != expected_ig_dtype_declaration
        or not isinstance(ig_capture.get("symbols"), Mapping)
        or set(ig_capture.get("symbols") or {}) != set(IG_MT4_SCALP_SYMBOLS)
        or _strict_nonnegative_int(ig_capture.get("npz_size_bytes")) is None
        or not hmac.compare_digest(
            capture_payload_sha, canonical_sha256(raw_capture_body)
        )
        or str(ig_capture.get("npz_path") or "")
        != str(payload.get("ig_calibration_npz_path") or "")
        or str(ig_capture.get("npz_sha256") or "").lower()
        != str(payload.get("ig_calibration_npz_sha256") or "").lower()
    ):
        raise ReleaseRefusal("validation_cost_model_ig_capture_invalid")

    source_sidecar = _resolve_statistical_sidecar(
        report_path=model_path,
        relative_path=payload.get("source_quote_npz_path"),
        expected_sha256=payload.get("source_quote_npz_sha256"),
        label="validation_cost_model_source_quote_input",
    )
    ig_sidecar = _resolve_statistical_sidecar(
        report_path=model_path,
        relative_path=payload.get("ig_calibration_npz_path"),
        expected_sha256=payload.get("ig_calibration_npz_sha256"),
        label="validation_cost_model_ig_calibration_input",
    )
    if ig_sidecar.stat().st_size != _strict_nonnegative_int(
        ig_capture.get("npz_size_bytes")
    ):
        raise ReleaseRefusal("validation_cost_model_ig_calibration_size_mismatch")
    if source_sidecar == ig_sidecar:
        raise ReleaseRefusal("validation_cost_model_sidecars_not_distinct")
    try:
        with np.load(source_sidecar, allow_pickle=False) as arrays:
            if set(arrays.files) != {
                "symbol_index",
                "minute_epoch",
                "bid_open",
                "ask_open",
                "bid_close",
                "ask_close",
            }:
                raise ReleaseRefusal("validation_cost_model_source_quote_arrays_invalid")
            source_symbol_index = np.asarray(arrays["symbol_index"])
            source_epoch = np.asarray(arrays["minute_epoch"])
            source_bid_open = np.asarray(arrays["bid_open"], dtype=float)
            source_ask_open = np.asarray(arrays["ask_open"], dtype=float)
            source_bid_close = np.asarray(arrays["bid_close"], dtype=float)
            source_ask_close = np.asarray(arrays["ask_close"], dtype=float)
        with np.load(ig_sidecar, allow_pickle=False) as arrays:
            if set(arrays.files) != required_ig_arrays:
                raise ReleaseRefusal(
                    "validation_cost_model_ig_calibration_arrays_invalid"
                )
            raw_ig_arrays = {name: np.asarray(arrays[name]) for name in arrays.files}
            expected_dtypes = {
                "symbol_index": np.dtype("int64"),
                "sample_epoch": np.dtype("float64"),
                "broker_quote_epoch": np.dtype("float64"),
                "received_at_epoch": np.dtype("float64"),
                "market_event_received_at_epoch": np.dtype("float64"),
                "source_event_sequence": np.dtype("int64"),
                "source_event_token_sha256": np.dtype("S64"),
                "bid": np.dtype("float64"),
                "ask": np.dtype("float64"),
                "point": np.dtype("float64"),
                "price_tick_size": np.dtype("float64"),
                "digits": np.dtype("int64"),
                "trade_allowed": np.dtype("bool"),
            }
            if any(
                raw_ig_arrays[name].dtype != expected_dtypes[name]
                for name in required_ig_arrays
            ):
                raise ReleaseRefusal(
                    "validation_cost_model_ig_calibration_dtypes_invalid"
                )
            ig_symbol_index = raw_ig_arrays["symbol_index"]
            ig_epoch = raw_ig_arrays["sample_epoch"]
            ig_broker_quote_epoch = raw_ig_arrays["broker_quote_epoch"]
            ig_received_epoch = raw_ig_arrays["received_at_epoch"]
            ig_market_event_received_epoch = raw_ig_arrays[
                "market_event_received_at_epoch"
            ]
            ig_source_event_sequence = raw_ig_arrays["source_event_sequence"]
            ig_source_event_token_sha256 = raw_ig_arrays[
                "source_event_token_sha256"
            ]
            ig_bid = raw_ig_arrays["bid"]
            ig_ask = raw_ig_arrays["ask"]
            ig_point = raw_ig_arrays["point"]
            ig_tick = raw_ig_arrays["price_tick_size"]
            ig_digits = raw_ig_arrays["digits"]
            ig_trade_allowed = raw_ig_arrays["trade_allowed"]
    except (OSError, ValueError) as exc:
        raise ReleaseRefusal("validation_cost_model_sidecar_invalid") from exc
    source_count = int(source_symbol_index.size)
    if (
        source_count < len(IG_MT4_SCALP_SYMBOLS)
        or source_symbol_index.ndim != 1
        or source_epoch.shape != (source_count,)
        or any(
            array.shape != (source_count,)
            for array in (
                source_bid_open,
                source_ask_open,
                source_bid_close,
                source_ask_close,
            )
        )
        or not np.issubdtype(source_symbol_index.dtype, np.integer)
        or not np.issubdtype(source_epoch.dtype, np.integer)
        or not all(
            np.isfinite(array).all()
            for array in (
                source_bid_open,
                source_ask_open,
                source_bid_close,
                source_ask_close,
            )
        )
        or np.any(source_bid_open <= 0.0)
        or np.any(source_bid_close <= 0.0)
        or np.any(source_ask_open < source_bid_open)
        or np.any(source_ask_close < source_bid_close)
        or np.any(source_epoch.astype(np.int64) <= 0)
        or np.any(source_epoch.astype(np.int64) % 60 != 0)
        or np.any(source_symbol_index < 0)
        or np.any(source_symbol_index >= len(IG_MT4_SCALP_SYMBOLS))
    ):
        raise ReleaseRefusal("validation_cost_model_source_quote_panel_invalid")
    source_keys = list(
        zip(
            source_symbol_index.astype(int).tolist(),
            source_epoch.astype(np.int64).tolist(),
        )
    )
    if len(set(source_keys)) != len(source_keys):
        raise ReleaseRefusal("validation_cost_model_source_quote_duplicates")
    if source_created_at < float(np.max(source_epoch)):
        raise ReleaseRefusal("validation_cost_model_source_capture_time_invalid")
    if (
        int(np.min(source_epoch)) < int(snapshot_start)
        or int(np.max(source_epoch)) > int(snapshot_end)
    ):
        raise ReleaseRefusal("validation_cost_model_source_snapshot_window_invalid")

    ig_count = int(ig_symbol_index.size)
    ig_shapes = (
        ig_epoch.shape,
        ig_broker_quote_epoch.shape,
        ig_received_epoch.shape,
        ig_market_event_received_epoch.shape,
        ig_source_event_sequence.shape,
        ig_source_event_token_sha256.shape,
        ig_bid.shape,
        ig_ask.shape,
        ig_point.shape,
        ig_tick.shape,
        ig_digits.shape,
        ig_trade_allowed.shape,
    )
    if (
        ig_count < len(IG_MT4_SCALP_SYMBOLS)
        or ig_symbol_index.ndim != 1
        or any(shape != (ig_count,) for shape in ig_shapes)
        or not np.issubdtype(ig_symbol_index.dtype, np.integer)
        or not np.issubdtype(ig_digits.dtype, np.integer)
        or not np.issubdtype(ig_trade_allowed.dtype, np.bool_)
        or not all(
            np.isfinite(array).all()
            for array in (
                ig_epoch,
                ig_broker_quote_epoch,
                ig_received_epoch,
                ig_market_event_received_epoch,
                ig_bid,
                ig_ask,
                ig_point,
                ig_tick,
            )
        )
        or np.any(ig_broker_quote_epoch <= 0.0)
        or np.any(ig_market_event_received_epoch <= 0.0)
        or np.any(ig_received_epoch <= 0.0)
        or np.any(ig_epoch <= 0.0)
        or np.any(
            ig_broker_quote_epoch
            > ig_received_epoch + IG_CLOCK_TOLERANCE_SECS
        )
        or np.any(
            ig_market_event_received_epoch
            > ig_received_epoch + IG_EVENT_ORDER_TOLERANCE_SECS
        )
        or np.any(ig_received_epoch > ig_epoch + IG_CLOCK_TOLERANCE_SECS)
        or np.any(
            ig_epoch - ig_received_epoch
            > MAX_IG_CALIBRATION_SAMPLE_GAP_SECS
        )
        or np.any(
            ig_epoch - ig_market_event_received_epoch
            > MAX_IG_CALIBRATION_SAMPLE_GAP_SECS
        )
        or np.any(ig_epoch > float(ig_created))
        or np.any(ig_source_event_sequence <= 0)
        or np.any(ig_bid <= 0.0)
        or np.any(ig_ask < ig_bid)
        or np.any(ig_point <= 0.0)
        or np.any(ig_tick <= 0.0)
        or np.any(ig_digits < 0)
        or np.any(ig_symbol_index < 0)
        or np.any(ig_symbol_index >= len(IG_MT4_SCALP_SYMBOLS))
        or (
            live_capture
            and (
                np.any(ig_epoch < float(ig_start))
                or np.any(ig_epoch > float(ig_end))
            )
        )
        or (
            history_capture
            and np.any(ig_epoch > float(ig_start) + IG_CLOCK_TOLERANCE_SECS)
        )
    ):
        raise ReleaseRefusal("validation_cost_model_ig_calibration_panel_invalid")
    try:
        ig_source_event_tokens = [
            bytes(value).decode("ascii")
            for value in ig_source_event_token_sha256.tolist()
        ]
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReleaseRefusal(
            "validation_cost_model_ig_event_token_invalid"
        ) from exc
    if any(not _is_sha256(token) or token != token.lower() for token in ig_source_event_tokens):
        raise ReleaseRefusal("validation_cost_model_ig_event_token_invalid")

    symbols = payload.get("symbols")
    if not isinstance(symbols, Mapping) or set(symbols) != set(IG_MT4_SCALP_SYMBOLS):
        raise ReleaseRefusal("validation_cost_model_symbol_rows_invalid")
    raw_capture_symbols = ig_capture.get("symbols")
    broker_audit_symbols = broker_contract_audit.get("symbols")
    point_audit_symbols = point_in_time_audit.get("symbols")
    if any(
        not isinstance(container, Mapping)
        or set(container) != set(IG_MT4_SCALP_SYMBOLS)
        for container in (
            raw_capture_symbols,
            broker_audit_symbols,
            point_audit_symbols,
        )
    ):
        raise ReleaseRefusal("validation_cost_model_ig_symbol_scope_invalid")
    normalized: dict[str, dict[str, Any]] = {}
    source_lookup: dict[
        tuple[str, int], tuple[float, float, float, float]
    ] = {}
    expected_symbol_fields = {
        "source_observations",
        "source_file_sha256",
        "source_independent_days",
        "source_first_epoch",
        "source_last_epoch",
        "source_median_spread_bps",
        "source_p90_spread_bps",
        "ig_observations",
        "ig_duration_secs",
        "ig_median_spread_bps",
        "ig_p90_spread_bps",
        "ig_max_spread_bps",
        "point",
        "price_tick_size",
        "digits",
        "trade_allowed",
        "commission_bps_per_round_trip",
        "configured_financing_bps_per_trade",
    }
    for symbol_position, symbol in enumerate(IG_MT4_SCALP_SYMBOLS):
        row = symbols.get(symbol)
        if not isinstance(row, Mapping) or set(row) != expected_symbol_fields:
            raise ReleaseRefusal(f"validation_cost_model_symbol_malformed:{symbol}")
        source_mask = source_symbol_index.astype(int) == symbol_position
        source_epochs = source_epoch[source_mask].astype(np.int64)
        source_bids_open = source_bid_open[source_mask]
        source_asks_open = source_ask_open[source_mask]
        source_bids_close = source_bid_close[source_mask]
        source_asks_close = source_ask_close[source_mask]
        source_observations = int(source_epochs.size)
        source_days = len({_utc_day(float(epoch)) for epoch in source_epochs})
        if source_observations:
            if np.any(np.diff(source_epochs) <= 0):
                raise ReleaseRefusal(
                    f"validation_cost_model_source_quote_order_invalid:{symbol}"
                )
            source_spreads = (
                (source_asks_open - source_bids_open)
                / ((source_asks_open + source_bids_open) / 2.0)
                * 1e4
            )
            source_first = int(np.min(source_epochs))
            source_last = int(np.max(source_epochs))
            source_median = float(np.median(source_spreads))
            source_p90 = float(np.quantile(source_spreads, 0.90, method="linear"))
            for epoch, bid_open, ask_open, bid_close, ask_close in zip(
                source_epochs,
                source_bids_open,
                source_asks_open,
                source_bids_close,
                source_asks_close,
            ):
                source_lookup[(symbol, int(epoch))] = (
                    float(bid_open),
                    float(ask_open),
                    float(bid_close),
                    float(ask_close),
                )
        else:
            source_first = 0
            source_last = 0
            source_median = float("nan")
            source_p90 = float("nan")
        ig_mask = ig_symbol_index.astype(int) == symbol_position
        ig_epochs = ig_epoch[ig_mask]
        ig_sequences = ig_source_event_sequence[ig_mask]
        ig_tokens = [
            ig_source_event_tokens[index]
            for index in np.flatnonzero(ig_mask).tolist()
        ]
        ig_bids = ig_bid[ig_mask]
        ig_asks = ig_ask[ig_mask]
        ig_points = ig_point[ig_mask]
        ig_ticks = ig_tick[ig_mask]
        ig_digit_values = ig_digits[ig_mask].astype(int)
        ig_allowed = ig_trade_allowed[ig_mask]
        ig_observations = int(ig_epochs.size)
        if ig_observations:
            order = np.argsort(ig_epochs, kind="stable")
            ordered_epochs = ig_epochs[order]
            ordered_sequences = ig_sequences[order]
            if np.any(np.diff(ordered_epochs) <= 0.0):
                raise ReleaseRefusal(
                    f"validation_cost_model_ig_sample_order_invalid:{symbol}"
                )
            if (
                np.any(np.diff(ordered_sequences) <= 0)
                or len(set(ig_tokens)) != len(ig_tokens)
            ):
                raise ReleaseRefusal(
                    f"validation_cost_model_ig_event_identity_invalid:{symbol}"
                )
            ig_duration = float(ordered_epochs[-1] - ordered_epochs[0])
            gaps = np.diff(ordered_epochs)
            ig_spreads = (
                (ig_asks - ig_bids) / ((ig_asks + ig_bids) / 2.0) * 1e4
            )
            ig_spread_points = (ig_asks - ig_bids) / ig_points
            ig_median = float(np.median(ig_spreads))
            ig_p90 = float(np.quantile(ig_spreads, 0.90, method="linear"))
            ig_max = float(np.max(ig_spreads))
            ig_median_points = float(np.median(ig_spread_points))
            ig_p90_points = float(
                np.quantile(ig_spread_points, 0.90, method="linear")
            )
            ig_max_points = float(np.max(ig_spread_points))
        else:
            ig_duration = 0.0
            gaps = np.asarray([], dtype=float)
            ig_median = float("nan")
            ig_p90 = float("nan")
            ig_max = float("nan")
            ig_median_points = float("nan")
            ig_p90_points = float("nan")
            ig_max_points = float("nan")
        raw_capture_row = raw_capture_symbols.get(symbol)
        broker_audit_row = broker_audit_symbols.get(symbol)
        point_audit_row = point_audit_symbols.get(symbol)
        raw_capture_fields = {
            "observations",
            "duration_secs",
            "max_intersample_gap_secs",
            "median_observed_spread_bps",
            "p90_observed_spread_bps",
            "max_observed_spread_bps",
            "median_observed_spread_points",
            "p90_observed_spread_points",
            "max_observed_spread_points",
            "point",
            "price_tick_size",
            "digits",
            "trade_allowed",
        }
        broker_audit_fields = {
            "point",
            "price_tick_size",
            "digits",
            "trade_allowed",
        }
        point_audit_fields = {
            "observations",
            "duration_secs",
            "max_intersample_gap_secs",
            "first_source_event_sequence",
            "last_source_event_sequence",
            "unique_source_event_count",
            "passed",
        }
        if (
            not isinstance(raw_capture_row, Mapping)
            or set(raw_capture_row) != raw_capture_fields
            or not isinstance(broker_audit_row, Mapping)
            or set(broker_audit_row) != broker_audit_fields
            or not isinstance(point_audit_row, Mapping)
            or set(point_audit_row) != point_audit_fields
        ):
            raise ReleaseRefusal(
                f"validation_cost_model_ig_symbol_capture_malformed:{symbol}"
            )
        capture_expected_values = {
            "observations": ig_observations,
            "duration_secs": ig_duration,
            "max_intersample_gap_secs": float(np.max(gaps)) if gaps.size else 0.0,
            "median_observed_spread_bps": ig_median,
            "p90_observed_spread_bps": ig_p90,
            "max_observed_spread_bps": ig_max,
            "median_observed_spread_points": ig_median_points,
            "p90_observed_spread_points": ig_p90_points,
            "max_observed_spread_points": ig_max_points,
            "point": float(ig_points[0]) if ig_observations else float("nan"),
            "price_tick_size": float(ig_ticks[0]) if ig_observations else float("nan"),
            "digits": int(ig_digit_values[0]) if ig_observations else -1,
        }
        if (
            raw_capture_row.get("trade_allowed") is not True
            or broker_audit_row.get("trade_allowed") is not True
            or point_audit_row.get("passed") is not True
            or any(
                _finite_float(raw_capture_row.get(field)) is None
                or not _close(
                    float(raw_capture_row[field]), float(expected), tolerance=1e-12
                )
                for field, expected in capture_expected_values.items()
            )
            or any(
                _finite_float(broker_audit_row.get(field)) is None
                or not _close(
                    float(broker_audit_row[field]),
                    float(capture_expected_values[field]),
                    tolerance=1e-12,
                )
                for field in ("point", "price_tick_size", "digits")
            )
            or _strict_nonnegative_int(point_audit_row.get("observations"))
            != ig_observations
            or not _close(
                _finite_float(point_audit_row.get("duration_secs")) or -1.0,
                ig_duration,
                tolerance=1e-12,
            )
            or not _close(
                _finite_float(
                    point_audit_row.get("max_intersample_gap_secs")
                )
                or -1.0,
                float(np.max(gaps)) if gaps.size else 0.0,
                tolerance=1e-12,
            )
            or _strict_nonnegative_int(
                point_audit_row.get("first_source_event_sequence")
            )
            != (int(ordered_sequences[0]) if ig_observations else -1)
            or _strict_nonnegative_int(
                point_audit_row.get("last_source_event_sequence")
            )
            != (int(ordered_sequences[-1]) if ig_observations else -1)
            or _strict_nonnegative_int(
                point_audit_row.get("unique_source_event_count")
            )
            != len(set(ig_tokens))
        ):
            raise ReleaseRefusal(
                f"validation_cost_model_ig_symbol_capture_mismatch:{symbol}"
            )
        point = _finite_float(row.get("point"))
        tick = _finite_float(row.get("price_tick_size"))
        digits = _strict_nonnegative_int(row.get("digits"))
        commission = _finite_float(row.get("commission_bps_per_round_trip"))
        financing = _finite_float(row.get("configured_financing_bps_per_trade"))
        if (
            source_observations < MIN_SOURCE_QUOTE_SAMPLES_PER_SYMBOL
            or source_days < MIN_SOURCE_QUOTE_DAYS_PER_SYMBOL
            or ig_observations < int(audit_minimum_samples or 0)
            or ig_duration < float(audit_minimum_duration or float("inf"))
            or (
                live_capture
                and gaps.size
                and float(np.max(gaps)) > float(audit_maximum_gap or 0.0)
            )
            or not np.all(ig_allowed)
            or np.unique(ig_points).size != 1
            or np.unique(ig_ticks).size != 1
            or np.unique(ig_digit_values).size != 1
            or point is None
            or point <= 0.0
            or tick is None
            or tick <= 0.0
            or digits is None
            or commission is None
            or commission < 0.0
            or financing is None
            or financing < 0.0
            or not _close(point, float(ig_points[0]), tolerance=1e-12)
            or not _close(tick, float(ig_ticks[0]), tolerance=1e-12)
            or digits != int(ig_digit_values[0])
            or row.get("trade_allowed") is not True
            or str(row.get("source_file_sha256") or "").lower()
            != source_file_sha256[symbol]
        ):
            raise ReleaseRefusal(f"validation_cost_model_symbol_invalid:{symbol}")
        if not _close(
            commission,
            fee_rows[symbol]["commission_bps_per_round_trip"],
            tolerance=1e-12,
        ) or not _close(
            financing,
            fee_rows[symbol]["configured_financing_bps_per_trade"],
            tolerance=1e-12,
        ):
            raise ReleaseRefusal(
                f"validation_cost_model_fee_binding_mismatch:{symbol}"
            )
        expected_values = {
            "source_observations": source_observations,
            "source_independent_days": source_days,
            "source_first_epoch": source_first,
            "source_last_epoch": source_last,
            "source_median_spread_bps": source_median,
            "source_p90_spread_bps": source_p90,
            "ig_observations": ig_observations,
            "ig_duration_secs": ig_duration,
            "ig_median_spread_bps": ig_median,
            "ig_p90_spread_bps": ig_p90,
            "ig_max_spread_bps": ig_max,
        }
        for field, expected in expected_values.items():
            actual = _finite_float(row.get(field))
            if actual is None or not _close(actual, float(expected), tolerance=1e-12):
                raise ReleaseRefusal(
                    f"validation_cost_model_symbol_summary_mismatch:{symbol}:{field}"
                )
        normalized[symbol] = {
            **expected_values,
            "source_file_sha256": source_file_sha256[symbol],
            "point": point,
            "price_tick_size": tick,
            "digits": digits,
            "trade_allowed": True,
            "commission_bps_per_round_trip": commission,
            "configured_financing_bps_per_trade": financing,
        }
    return {
        "rows": normalized,
        "source_lookup": source_lookup,
        "source_snapshot_sha256": composite_sha,
    }


_TRADE_RECORD_FIELDS = {
    "trade_id",
    "symbol",
    "side",
    "strategy_id",
    "strategy_version",
    "engine_sha256",
    "config_sha256",
    "source_snapshot_sha256",
    "decision_bar_open_epoch",
    "fill_bar_open_epoch",
    "entry_epoch",
    "exit_epoch",
    "entry_utc_day",
    "decision_bid",
    "decision_ask",
    "next_open_bid",
    "next_open_ask",
    "entry_price",
    "initial_sl_price",
    "initial_tp_price",
    "exit_price",
    "exit_reason",
    "stop_bps",
    "target_bps",
    "p_star",
    "gross_mid_pnl_bps",
    "source_spread_cost_bps",
    "ig_cost_pad_bps",
    "slippage_bps",
    "commission_bps",
    "financing_bps",
    "base_total_cost_bps",
    "two_x_total_cost_bps",
    "net_pnl_bps_base",
    "net_pnl_bps_2x",
    "initial_risk_bps",
    "net_r_base",
    "net_r_2x",
    "full_target_hit_first",
    "notional_account_ccy",
    "equity_before",
    "realized_pnl_account_ccy",
    "equity_after",
    "peak_equity_after",
    "drawdown_pct_after",
}


def _validate_trade_ledger(
    payload: Any,
    *,
    expected_engine_sha256: str,
    expected_config_sha256: str,
    cost_model_sha256: str,
    cost_contract: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ReleaseRefusal("validation_trade_ledger_malformed")
    expected_fields = {
        "schema_version",
        "source_errors",
        "strategy_id",
        "strategy_version",
        "engine_sha256",
        "config_sha256",
        "venue_id",
        "symbol_scope",
        "cost_model_sha256",
        "portfolio_contract",
        "records",
    }
    if set(payload) != expected_fields:
        raise ReleaseRefusal("validation_trade_ledger_scope_invalid")
    if payload.get("schema_version") != TRADE_LEDGER_SCHEMA:
        raise ReleaseRefusal("validation_trade_ledger_schema_invalid")
    if payload.get("source_errors") != []:
        raise ReleaseRefusal("validation_trade_ledger_source_errors_present")
    if payload.get("strategy_id") != SCALP_DISLOCATION_STRATEGY_ID:
        raise ReleaseRefusal("validation_trade_ledger_strategy_id_invalid")
    if payload.get("strategy_version") != SCALP_DISLOCATION_STRATEGY_VERSION:
        raise ReleaseRefusal("validation_trade_ledger_strategy_version_invalid")
    if str(payload.get("engine_sha256") or "").lower() != expected_engine_sha256:
        raise ReleaseRefusal("validation_trade_ledger_engine_sha256_mismatch")
    if str(payload.get("config_sha256") or "").lower() != expected_config_sha256:
        raise ReleaseRefusal("validation_trade_ledger_config_sha256_mismatch")
    if payload.get("venue_id") != IG_MT4_VENUE_ID:
        raise ReleaseRefusal("validation_trade_ledger_venue_invalid")
    if payload.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS):
        raise ReleaseRefusal("validation_trade_ledger_symbol_scope_invalid")
    if str(payload.get("cost_model_sha256") or "").lower() != cost_model_sha256:
        raise ReleaseRefusal("validation_trade_ledger_cost_model_mismatch")
    portfolio = payload.get("portfolio_contract")
    if not isinstance(portfolio, Mapping) or set(portfolio) != {
        "initial_equity",
        "account_currency",
        "risk_sizing",
        "max_concurrent_positions",
    }:
        raise ReleaseRefusal("validation_trade_ledger_portfolio_malformed")
    initial_equity = _finite_float(portfolio.get("initial_equity"))
    account_currency = str(portfolio.get("account_currency") or "").strip().upper()
    max_concurrent = _strict_nonnegative_int(
        portfolio.get("max_concurrent_positions")
    )
    if (
        initial_equity is None
        or initial_equity <= 0.0
        or not account_currency
        or portfolio.get("risk_sizing") != RISK_SIZING_METHOD
        or max_concurrent is None
        or max_concurrent < 1
    ):
        raise ReleaseRefusal("validation_trade_ledger_portfolio_invalid")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ReleaseRefusal("validation_trade_ledger_records_missing")

    normalized: list[dict[str, Any]] = []
    trade_ids: set[str] = set()
    symbol_days: set[tuple[str, str]] = set()
    observed_by_symbol = {symbol: 0 for symbol in IG_MT4_SCALP_SYMBOLS}
    cost_rows = dict(cost_contract.get("rows") or {})
    source_lookup = dict(cost_contract.get("source_lookup") or {})
    previous_exit_key: tuple[float, str] | None = None
    previous_equity = float(initial_equity)
    peak_equity = float(initial_equity)
    maximum_drawdown_pct = 0.0
    intervals: list[tuple[float, int]] = []
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping) or set(raw) != _TRADE_RECORD_FIELDS:
            raise ReleaseRefusal(f"validation_trade_record_scope_invalid:{index}")
        row = dict(raw)
        trade_id = str(row.get("trade_id") or "").strip()
        symbol = str(row.get("symbol") or "").strip().upper()
        side = str(row.get("side") or "").strip().upper()
        if not trade_id or trade_id in trade_ids:
            raise ReleaseRefusal(f"validation_trade_id_invalid:{index}")
        trade_ids.add(trade_id)
        if symbol not in IG_MT4_SCALP_SYMBOLS or side not in {"BUY", "SELL"}:
            raise ReleaseRefusal(f"validation_trade_scope_invalid:{trade_id}")
        if (
            row.get("strategy_id") != SCALP_DISLOCATION_STRATEGY_ID
            or row.get("strategy_version") != SCALP_DISLOCATION_STRATEGY_VERSION
            or str(row.get("engine_sha256") or "").lower()
            != expected_engine_sha256
            or str(row.get("config_sha256") or "").lower()
            != expected_config_sha256
            or str(row.get("source_snapshot_sha256") or "").lower()
            != str(cost_rows[symbol]["source_file_sha256"]).lower()
        ):
            raise ReleaseRefusal(f"validation_trade_identity_invalid:{trade_id}")
        decision_epoch = _strict_nonnegative_int(row.get("decision_bar_open_epoch"))
        fill_epoch = _strict_nonnegative_int(row.get("fill_bar_open_epoch"))
        entry_epoch = _finite_float(row.get("entry_epoch"))
        exit_epoch = _finite_float(row.get("exit_epoch"))
        if (
            decision_epoch is None
            or fill_epoch is None
            or decision_epoch <= 0
            or decision_epoch % 60 != 0
            or fill_epoch != decision_epoch + 60
            or entry_epoch is None
            or exit_epoch is None
            or entry_epoch < fill_epoch
            or exit_epoch <= entry_epoch
        ):
            raise ReleaseRefusal(f"validation_trade_time_invalid:{trade_id}")
        day = _utc_day(entry_epoch)
        if row.get("entry_utc_day") != day:
            raise ReleaseRefusal(f"validation_trade_day_invalid:{trade_id}")
        symbol_day = (symbol, day)
        if symbol_day in symbol_days:
            raise ReleaseRefusal(
                f"validation_trade_daily_frequency_exceeded:{symbol}:{day}"
            )
        symbol_days.add(symbol_day)
        exit_key = (exit_epoch, trade_id)
        if previous_exit_key is not None and exit_key <= previous_exit_key:
            raise ReleaseRefusal("validation_trade_ledger_not_exit_ordered")
        previous_exit_key = exit_key

        numeric_names = (
            "decision_bid",
            "decision_ask",
            "next_open_bid",
            "next_open_ask",
            "entry_price",
            "initial_sl_price",
            "initial_tp_price",
            "exit_price",
            "stop_bps",
            "target_bps",
            "p_star",
            "gross_mid_pnl_bps",
            "source_spread_cost_bps",
            "ig_cost_pad_bps",
            "slippage_bps",
            "commission_bps",
            "financing_bps",
            "base_total_cost_bps",
            "two_x_total_cost_bps",
            "net_pnl_bps_base",
            "net_pnl_bps_2x",
            "initial_risk_bps",
            "net_r_base",
            "net_r_2x",
            "notional_account_ccy",
            "equity_before",
            "realized_pnl_account_ccy",
            "equity_after",
            "peak_equity_after",
            "drawdown_pct_after",
        )
        values = {name: _finite_float(row.get(name)) for name in numeric_names}
        if any(value is None for value in values.values()):
            raise ReleaseRefusal(f"validation_trade_numeric_invalid:{trade_id}")
        number = {name: float(value) for name, value in values.items() if value is not None}
        if (
            min(
                number["decision_bid"],
                number["decision_ask"],
                number["next_open_bid"],
                number["next_open_ask"],
                number["entry_price"],
                number["initial_sl_price"],
                number["initial_tp_price"],
                number["exit_price"],
                number["notional_account_ccy"],
                number["equity_before"],
                number["equity_after"],
                number["peak_equity_after"],
            )
            <= 0.0
            or number["decision_ask"] < number["decision_bid"]
            or number["next_open_ask"] < number["next_open_bid"]
            or number["stop_bps"] <= 0.0
            or number["target_bps"] <= 0.0
            or not 0.0 <= number["p_star"] < 1.0
            or any(
                number[name] < 0.0
                for name in (
                    "source_spread_cost_bps",
                    "ig_cost_pad_bps",
                    "slippage_bps",
                    "commission_bps",
                    "financing_bps",
                    "base_total_cost_bps",
                    "two_x_total_cost_bps",
                    "initial_risk_bps",
                    "drawdown_pct_after",
                )
            )
        ):
            raise ReleaseRefusal(f"validation_trade_numeric_domain_invalid:{trade_id}")
        if side == "BUY":
            bracket_valid = (
                number["initial_sl_price"]
                < number["entry_price"]
                < number["initial_tp_price"]
            )
            expected_slippage = (
                (number["entry_price"] - number["next_open_ask"])
                / number["next_open_ask"]
                * 1e4
            )
            expected_gross = (
                (number["exit_price"] - number["entry_price"])
                / number["entry_price"]
                * 1e4
            )
            expected_stop = (
                (number["entry_price"] - number["initial_sl_price"])
                / number["entry_price"]
                * 1e4
            )
            expected_target = (
                (number["initial_tp_price"] - number["entry_price"])
                / number["entry_price"]
                * 1e4
            )
        else:
            bracket_valid = (
                number["initial_tp_price"]
                < number["entry_price"]
                < number["initial_sl_price"]
            )
            expected_slippage = (
                (number["next_open_bid"] - number["entry_price"])
                / number["next_open_bid"]
                * 1e4
            )
            expected_gross = (
                (number["entry_price"] - number["exit_price"])
                / number["entry_price"]
                * 1e4
            )
            expected_stop = (
                (number["initial_sl_price"] - number["entry_price"])
                / number["entry_price"]
                * 1e4
            )
            expected_target = (
                (number["entry_price"] - number["initial_tp_price"])
                / number["entry_price"]
                * 1e4
            )
        if (
            not bracket_valid
            or expected_slippage < -1e-9
            or not _close(number["slippage_bps"], expected_slippage)
            or not _close(number["gross_mid_pnl_bps"], expected_gross)
            or not _close(number["stop_bps"], expected_stop)
            or not _close(number["target_bps"], expected_target)
        ):
            raise ReleaseRefusal(f"validation_trade_price_geometry_invalid:{trade_id}")
        decision_source_quote = source_lookup.get((symbol, int(decision_epoch)))
        fill_source_quote = source_lookup.get((symbol, int(fill_epoch)))
        if decision_source_quote is None or fill_source_quote is None:
            raise ReleaseRefusal(f"validation_trade_source_quote_missing:{trade_id}")
        source_bid, source_ask, _, _ = fill_source_quote
        _, _, decision_bid_close, decision_ask_close = decision_source_quote
        if (
            not _close(
                number["decision_bid"], decision_bid_close, tolerance=1e-12
            )
            or not _close(
                number["decision_ask"], decision_ask_close, tolerance=1e-12
            )
            or not _close(number["next_open_bid"], source_bid, tolerance=1e-12)
            or not _close(number["next_open_ask"], source_ask, tolerance=1e-12)
        ):
            raise ReleaseRefusal(f"validation_trade_source_quote_mismatch:{trade_id}")
        cost_row = cost_rows[symbol]
        source_mid = (source_bid + source_ask) / 2.0
        expected_source_spread = (source_ask - source_bid) / source_mid * 1e4
        expected_ig_pad = max(
            0.0,
            float(cost_row["ig_p90_spread_bps"]) - expected_source_spread,
        )
        if (
            not _close(
                number["source_spread_cost_bps"],
                expected_source_spread,
                tolerance=1e-12,
            )
            or not _close(
                number["ig_cost_pad_bps"], expected_ig_pad, tolerance=1e-12
            )
            or not _close(
                number["commission_bps"],
                float(cost_row["commission_bps_per_round_trip"]),
                tolerance=1e-12,
            )
            or not _close(
                number["financing_bps"],
                float(cost_row["configured_financing_bps_per_trade"]),
                tolerance=1e-12,
            )
        ):
            raise ReleaseRefusal(f"validation_trade_cost_provenance_invalid:{trade_id}")
        expected_base_cost = sum(
            number[name]
            for name in (
                "source_spread_cost_bps",
                "ig_cost_pad_bps",
                "slippage_bps",
                "commission_bps",
                "financing_bps",
            )
        )
        expected_two_x_cost = REQUIRED_COST_STRESS_MULTIPLE * expected_base_cost
        expected_net_base = number["gross_mid_pnl_bps"] - expected_base_cost
        expected_net_two_x = number["gross_mid_pnl_bps"] - expected_two_x_cost
        expected_initial_risk = number["stop_bps"] + expected_base_cost
        expected_p_star = expected_initial_risk / (
            number["target_bps"] + number["stop_bps"]
        )
        expected_r_base = expected_net_base / expected_initial_risk
        expected_r_two_x = expected_net_two_x / expected_initial_risk
        expected_realized = (
            expected_net_base / 1e4 * number["notional_account_ccy"]
        )
        if not all(
            (
                _close(number["base_total_cost_bps"], expected_base_cost),
                _close(number["two_x_total_cost_bps"], expected_two_x_cost),
                _close(number["net_pnl_bps_base"], expected_net_base),
                _close(number["net_pnl_bps_2x"], expected_net_two_x),
                _close(number["initial_risk_bps"], expected_initial_risk),
                _close(number["p_star"], expected_p_star),
                _close(number["net_r_base"], expected_r_base),
                _close(number["net_r_2x"], expected_r_two_x),
                _close(number["realized_pnl_account_ccy"], expected_realized),
            )
        ):
            raise ReleaseRefusal(f"validation_trade_cost_arithmetic_invalid:{trade_id}")
        exit_reason = str(row.get("exit_reason") or "").strip().upper()
        if exit_reason not in {"TAKE_PROFIT", "STOP_LOSS", "TIME_STOP"}:
            raise ReleaseRefusal(f"validation_trade_exit_reason_invalid:{trade_id}")
        target_hit = row.get("full_target_hit_first")
        if not isinstance(target_hit, bool) or target_hit != (
            exit_reason == "TAKE_PROFIT"
        ):
            raise ReleaseRefusal(f"validation_trade_win_definition_invalid:{trade_id}")
        if (
            (exit_reason == "TAKE_PROFIT" and side == "BUY" and number["exit_price"] < number["initial_tp_price"] - 1e-9)
            or (exit_reason == "TAKE_PROFIT" and side == "SELL" and number["exit_price"] > number["initial_tp_price"] + 1e-9)
            or (exit_reason == "STOP_LOSS" and side == "BUY" and number["exit_price"] > number["initial_sl_price"] + 1e-9)
            or (exit_reason == "STOP_LOSS" and side == "SELL" and number["exit_price"] < number["initial_sl_price"] - 1e-9)
        ):
            raise ReleaseRefusal(f"validation_trade_exit_price_invalid:{trade_id}")
        if not _close(number["equity_before"], previous_equity):
            raise ReleaseRefusal(f"validation_trade_equity_sequence_invalid:{trade_id}")
        expected_equity_after = previous_equity + expected_realized
        if expected_equity_after <= 0.0 or not _close(
            number["equity_after"], expected_equity_after
        ):
            raise ReleaseRefusal(f"validation_trade_equity_after_invalid:{trade_id}")
        peak_equity = max(peak_equity, expected_equity_after)
        drawdown_pct = (peak_equity - expected_equity_after) / peak_equity * 100.0
        if (
            not _close(number["peak_equity_after"], peak_equity)
            or not _close(number["drawdown_pct_after"], drawdown_pct)
        ):
            raise ReleaseRefusal(f"validation_trade_drawdown_invalid:{trade_id}")
        previous_equity = expected_equity_after
        maximum_drawdown_pct = max(maximum_drawdown_pct, drawdown_pct)
        observed_by_symbol[symbol] += 1
        intervals.extend(((entry_epoch, 1), (exit_epoch, -1)))
        normalized.append(
            {
                **row,
                "trade_id": trade_id,
                "symbol": symbol,
                "side": side,
                "entry_utc_day": day,
                "entry_epoch": entry_epoch,
                "exit_epoch": exit_epoch,
                "net_r_base": expected_r_base,
                "net_r_2x": expected_r_two_x,
                "full_target_hit_first": target_hit,
            }
        )
    concurrent = 0
    observed_max_concurrent = 0
    for _, delta in sorted(intervals, key=lambda item: (item[0], item[1])):
        concurrent += delta
        if concurrent < 0:
            raise ReleaseRefusal("validation_trade_concurrency_invalid")
        observed_max_concurrent = max(observed_max_concurrent, concurrent)
    if concurrent != 0 or observed_max_concurrent > int(max_concurrent):
        raise ReleaseRefusal("validation_trade_concurrency_invalid")
    return {
        "records": normalized,
        "maximum_drawdown_pct": maximum_drawdown_pct,
        "independent_days": len({row["entry_utc_day"] for row in normalized}),
    }


def _validate_cell_evidence(
    payload: Any,
    *,
    ledger_sha256: str,
    cost_model_sha256: str,
    records: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "source_errors",
        "trade_ledger_sha256",
        "cost_model_sha256",
        "win_definition",
        "max_entries_per_symbol_utc_day",
        "cells",
    }:
        raise ReleaseRefusal("validation_cell_evidence_scope_invalid")
    if payload.get("schema_version") != CELL_EVIDENCE_SCHEMA:
        raise ReleaseRefusal("validation_cell_evidence_schema_invalid")
    if payload.get("source_errors") != []:
        raise ReleaseRefusal("validation_cell_evidence_source_errors_present")
    if (
        str(payload.get("trade_ledger_sha256") or "").lower() != ledger_sha256
        or str(payload.get("cost_model_sha256") or "").lower()
        != cost_model_sha256
    ):
        raise ReleaseRefusal("validation_cell_evidence_binding_invalid")
    if payload.get("win_definition") != WIN_DEFINITION:
        raise ReleaseRefusal("validation_cell_evidence_win_definition_invalid")
    if (
        _strict_nonnegative_int(payload.get("max_entries_per_symbol_utc_day"))
        != MAX_ENTRIES_PER_SYMBOL_UTC_DAY
    ):
        raise ReleaseRefusal("validation_cell_evidence_frequency_invalid")
    raw_cells = payload.get("cells")
    if not isinstance(raw_cells, Mapping) or set(raw_cells) != set(
        IG_MT4_SCALP_SYMBOLS
    ):
        raise ReleaseRefusal("validation_cell_evidence_symbol_scope_invalid")
    by_cell: dict[tuple[str, str], list[dict[str, Any]]] = {
        (symbol, side): []
        for symbol in IG_MT4_SCALP_SYMBOLS
        for side in ("BUY", "SELL")
    }
    for record in records:
        by_cell[(record["symbol"], record["side"])].append(record)
    derived: dict[str, dict[str, dict[str, Any]]] = {}
    claimed_ids: list[str] = []
    for symbol in IG_MT4_SCALP_SYMBOLS:
        sides = raw_cells.get(symbol)
        if not isinstance(sides, Mapping) or set(sides) != {"BUY", "SELL"}:
            raise ReleaseRefusal(f"validation_cell_evidence_side_scope_invalid:{symbol}")
        derived[symbol] = {}
        for side in ("BUY", "SELL"):
            raw_cell = sides.get(side)
            if not isinstance(raw_cell, Mapping) or set(raw_cell) != {"trade_ids"}:
                raise ReleaseRefusal(
                    f"validation_cell_evidence_cell_malformed:{symbol}:{side}"
                )
            trade_ids = raw_cell.get("trade_ids")
            if not isinstance(trade_ids, list) or any(
                not isinstance(item, str) or not item for item in trade_ids
            ):
                raise ReleaseRefusal(
                    f"validation_cell_evidence_ids_invalid:{symbol}:{side}"
                )
            expected_records = by_cell[(symbol, side)]
            expected_ids = [record["trade_id"] for record in expected_records]
            if trade_ids != expected_ids:
                raise ReleaseRefusal(
                    f"validation_cell_evidence_membership_mismatch:{symbol}:{side}"
                )
            claimed_ids.extend(trade_ids)
            trades = len(expected_records)
            wins = sum(
                int(record["full_target_hit_first"] is True)
                for record in expected_records
            )
            days = len({record["entry_utc_day"] for record in expected_records})
            lower, upper = _independent_wilson_interval(wins=wins, trades=trades)
            derived[symbol][side] = {
                "trades": trades,
                "independent_days": days,
                "wins": wins,
                "win_probability": wins / trades if trades else 0.0,
                "win_probability_ci_lower": lower,
                "win_probability_ci_upper": upper,
                "win_probability_ci_method": WIN_PROBABILITY_CI_METHOD,
            }
    if len(claimed_ids) != len(records) or len(set(claimed_ids)) != len(records):
        raise ReleaseRefusal("validation_cell_evidence_coverage_invalid")
    return derived


def _resolve_statistical_sidecar(
    *,
    report_path: Path,
    relative_path: Any,
    expected_sha256: Any,
    label: str,
) -> Path:
    raw = str(relative_path or "").strip()
    if not raw:
        raise ReleaseRefusal(f"{label}_path_missing")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReleaseRefusal(f"{label}_path_invalid")
    candidate = report_path.parent / relative
    resolved = _require_regular_file(candidate, label=label, limit=MAX_ARTIFACT_BYTES)
    if not resolved.is_relative_to(report_path.parent.resolve(strict=True)):
        raise ReleaseRefusal(f"{label}_path_escape")
    _, digest, _ = _file_identity(resolved, label=label)
    if not _is_sha256(expected_sha256) or not hmac.compare_digest(
        digest, str(expected_sha256).lower()
    ):
        raise ReleaseRefusal(f"{label}_sha256_mismatch")
    return resolved


def _period_sharpe(values: Any) -> float:
    import numpy as np

    array = np.asarray(values, dtype=float).ravel()
    if array.size < 2 or not np.isfinite(array).all():
        return 0.0
    standard_deviation = float(np.std(array, ddof=1))
    if not math.isfinite(standard_deviation) or standard_deviation <= 0.0:
        return 0.0
    return float(np.mean(array) / standard_deviation)


def _recompute_mcpt(
    payload: Any,
    *,
    report_path: Path,
) -> tuple[float, int]:
    import numpy as np

    expected_fields = {
        "method",
        "seed",
        "n_permutations",
        "input_npz_path",
        "input_npz_sha256",
        "observed_statistic",
        "null_statistics",
        "p_value",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_fields:
        raise ReleaseRefusal("validation_statistical_mcpt_scope_invalid")
    if payload.get("method") != MCPT_METHOD:
        raise ReleaseRefusal("validation_statistical_mcpt_method_invalid")
    seed = _strict_nonnegative_int(payload.get("seed"))
    permutations = _strict_nonnegative_int(payload.get("n_permutations"))
    if seed is None or permutations is None or permutations < MIN_MCPT_PERMUTATIONS:
        raise ReleaseRefusal("validation_statistical_mcpt_parameters_invalid")
    sidecar = _resolve_statistical_sidecar(
        report_path=report_path,
        relative_path=payload.get("input_npz_path"),
        expected_sha256=payload.get("input_npz_sha256"),
        label="validation_statistical_mcpt_input",
    )
    try:
        with np.load(sidecar, allow_pickle=False) as arrays:
            if set(arrays.files) != {
                "timestamps",
                "lagged_signed_exposure",
                "bar_returns",
                "cost_per_turn",
            }:
                raise ReleaseRefusal("validation_statistical_mcpt_arrays_invalid")
            timestamps = np.asarray(arrays["timestamps"])
            exposure = np.asarray(arrays["lagged_signed_exposure"], dtype=float)
            bar_returns = np.asarray(arrays["bar_returns"], dtype=float)
            cost_per_turn = np.asarray(arrays["cost_per_turn"], dtype=float)
    except (OSError, ValueError) as exc:
        raise ReleaseRefusal("validation_statistical_mcpt_npz_invalid") from exc
    observations = int(timestamps.size)
    expected_shape = (observations, len(IG_MT4_SCALP_SYMBOLS))
    if (
        timestamps.ndim != 1
        or observations < 20
        or not np.issubdtype(timestamps.dtype, np.integer)
        or np.any(np.diff(timestamps.astype(np.int64)) <= 0)
        or exposure.shape != expected_shape
        or bar_returns.shape != expected_shape
        or cost_per_turn.shape != expected_shape
        or not np.isfinite(exposure).all()
        or not np.isfinite(bar_returns).all()
        or not np.isfinite(cost_per_turn).all()
        or np.any(cost_per_turn < 0.0)
    ):
        raise ReleaseRefusal("validation_statistical_mcpt_panel_invalid")

    def _statistic(selected_exposure: Any) -> float:
        turnover = np.abs(selected_exposure - np.roll(selected_exposure, 1, axis=0))
        net = np.sum(
            selected_exposure * bar_returns - turnover * cost_per_turn,
            axis=1,
        )
        return _period_sharpe(net)

    observed = _statistic(exposure)
    rng = random.Random(seed)
    null = [
        _statistic(np.roll(exposure, rng.randrange(1, observations), axis=0))
        for _ in range(permutations)
    ]
    reported_observed = _finite_float(payload.get("observed_statistic"))
    reported_null = payload.get("null_statistics")
    reported_p = _finite_float(payload.get("p_value"))
    if (
        reported_observed is None
        or not _close(reported_observed, observed, tolerance=1e-12)
        or not isinstance(reported_null, list)
        or len(reported_null) != permutations
        or any(_finite_float(value) is None for value in reported_null)
        or any(
            not _close(float(reported), recomputed, tolerance=1e-12)
            for reported, recomputed in zip(reported_null, null)
        )
    ):
        raise ReleaseRefusal("validation_statistical_mcpt_results_mismatch")
    p_value = (1.0 + sum(value >= observed for value in null)) / (
        permutations + 1.0
    )
    if reported_p is None or not _close(reported_p, p_value, tolerance=1e-12):
        raise ReleaseRefusal("validation_statistical_mcpt_p_value_mismatch")
    return p_value, observations


def _recompute_pbo_dsr(
    payload: Any,
    *,
    report_path: Path,
    expected_config_sha256: str,
    expected_source_snapshot_sha256: str,
) -> tuple[float, float, int, int, str]:
    import numpy as np

    from fxstack.validation.metrics import kurtosis, skewness
    from fxstack.validation.overfitting import (
        deflated_sharpe_ratio,
        probability_of_backtest_overfitting,
        sharpe_variance_across_trials,
    )

    expected_fields = {
        "method",
        "input_npz_path",
        "input_npz_sha256",
        "attempt_manifest_path",
        "attempt_manifest_sha256",
        "attempt_ids",
        "selected_attempt_id",
        "n_splits",
        "max_combinations",
        "pbo",
        "dsr",
        "selected_sharpe",
        "sharpe_variance_across_trials",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_fields:
        raise ReleaseRefusal("validation_statistical_pbo_dsr_scope_invalid")
    if payload.get("method") != PBO_DSR_METHOD:
        raise ReleaseRefusal("validation_statistical_pbo_dsr_method_invalid")
    manifest_path = _resolve_statistical_sidecar(
        report_path=report_path,
        relative_path=payload.get("attempt_manifest_path"),
        expected_sha256=payload.get("attempt_manifest_sha256"),
        label="validation_statistical_attempt_manifest",
    )
    manifest = _load_json_object(
        manifest_path,
        label="validation_statistical_attempt_manifest",
        limit=MAX_REQUEST_JSON_BYTES,
    )
    if set(manifest) != {
        "schema_version",
        "sealed_before_replay",
        "created_at_epoch",
        "replay_started_at_epoch",
        "source_snapshot_sha256",
        "selected_attempt_id",
        "attempts",
    } or manifest.get("schema_version") != ATTEMPT_MANIFEST_SCHEMA:
        raise ReleaseRefusal("validation_statistical_attempt_manifest_scope_invalid")
    manifest_created = _finite_float(manifest.get("created_at_epoch"))
    replay_started = _finite_float(manifest.get("replay_started_at_epoch"))
    manifest_attempts = manifest.get("attempts")
    if (
        manifest.get("sealed_before_replay") is not True
        or manifest_created is None
        or replay_started is None
        or manifest_created <= 0.0
        or replay_started - manifest_created < 1e-6
        or str(manifest.get("source_snapshot_sha256") or "").lower()
        != expected_source_snapshot_sha256
        or not isinstance(manifest_attempts, list)
        or len(manifest_attempts) < 2
    ):
        raise ReleaseRefusal("validation_statistical_attempt_manifest_invalid")
    manifest_ids: list[str] = []
    manifest_configs: list[str] = []
    for index, raw_attempt in enumerate(manifest_attempts):
        if not isinstance(raw_attempt, Mapping) or set(raw_attempt) != {
            "attempt_id",
            "policy",
            "config_sha256",
        }:
            raise ReleaseRefusal(
                f"validation_statistical_attempt_manifest_row_invalid:{index}"
            )
        attempt_id = str(raw_attempt.get("attempt_id") or "").strip()
        policy = raw_attempt.get("policy")
        config_sha = str(raw_attempt.get("config_sha256") or "").lower()
        if (
            not attempt_id
            or not isinstance(policy, Mapping)
            or not _is_sha256(config_sha)
            or canonical_sha256(dict(policy)) != config_sha
        ):
            raise ReleaseRefusal(
                f"validation_statistical_attempt_manifest_row_invalid:{index}"
            )
        manifest_ids.append(attempt_id)
        manifest_configs.append(config_sha)
    manifest_selected = str(manifest.get("selected_attempt_id") or "").strip()
    if (
        len(set(manifest_ids)) != len(manifest_ids)
        or len(set(manifest_configs)) != len(manifest_configs)
        or manifest_selected not in manifest_ids
        or manifest_configs[manifest_ids.index(manifest_selected)]
        != expected_config_sha256
    ):
        raise ReleaseRefusal("validation_statistical_attempt_manifest_identity_invalid")
    attempt_ids = payload.get("attempt_ids")
    selected_id = str(payload.get("selected_attempt_id") or "").strip()
    splits = _strict_nonnegative_int(payload.get("n_splits"))
    combinations = _strict_nonnegative_int(payload.get("max_combinations"))
    if (
        not isinstance(attempt_ids, list)
        or len(attempt_ids) < 2
        or any(not isinstance(item, str) or not item for item in attempt_ids)
        or len(set(attempt_ids)) != len(attempt_ids)
        or selected_id not in attempt_ids
        or attempt_ids != manifest_ids
        or selected_id != manifest_selected
        or splits is None
        or splits < 2
        or splits % 2 != 0
        or combinations is None
        or combinations < 1
    ):
        raise ReleaseRefusal("validation_statistical_pbo_dsr_parameters_invalid")
    sidecar = _resolve_statistical_sidecar(
        report_path=report_path,
        relative_path=payload.get("input_npz_path"),
        expected_sha256=payload.get("input_npz_sha256"),
        label="validation_statistical_pbo_dsr_input",
    )
    try:
        with np.load(sidecar, allow_pickle=False) as arrays:
            if set(arrays.files) != {"aligned_returns", "trial_sharpes"}:
                raise ReleaseRefusal(
                    "validation_statistical_pbo_dsr_arrays_invalid"
                )
            returns = np.asarray(arrays["aligned_returns"], dtype=float)
            trial_sharpes = np.asarray(arrays["trial_sharpes"], dtype=float).ravel()
    except (OSError, ValueError) as exc:
        raise ReleaseRefusal("validation_statistical_pbo_dsr_npz_invalid") from exc
    attempts = len(attempt_ids)
    if (
        returns.ndim != 2
        or returns.shape[1] != attempts
        or returns.shape[0] < 2 * splits
        or trial_sharpes.shape != (attempts,)
        or not np.isfinite(returns).all()
        or not np.isfinite(trial_sharpes).all()
    ):
        raise ReleaseRefusal("validation_statistical_pbo_dsr_matrix_invalid")
    recomputed_sharpes = np.asarray(
        [_period_sharpe(returns[:, index]) for index in range(attempts)],
        dtype=float,
    )
    if not np.allclose(
        trial_sharpes,
        recomputed_sharpes,
        rtol=1e-12,
        atol=1e-12,
    ):
        raise ReleaseRefusal("validation_statistical_trial_sharpes_mismatch")
    pbo_result = probability_of_backtest_overfitting(
        returns,
        n_splits=splits,
        max_combinations=combinations,
    )
    pbo = _finite_float(pbo_result.get("pbo"))
    if pbo is None or pbo_result.get("insufficient_data") != 0.0:
        raise ReleaseRefusal("validation_statistical_pbo_unavailable")
    selected_index = attempt_ids.index(selected_id)
    selected_returns = returns[:, selected_index]
    selected_sharpe = float(recomputed_sharpes[selected_index])
    sharpe_variance = sharpe_variance_across_trials(recomputed_sharpes)
    dsr_result = deflated_sharpe_ratio(
        sharpe_per_period=selected_sharpe,
        n_obs=int(returns.shape[0]),
        n_trials=attempts,
        sharpe_variance_across_trials=sharpe_variance,
        skew=skewness(selected_returns),
        kurtosis=kurtosis(selected_returns),
    )
    dsr = _finite_float(dsr_result.get("dsr"))
    reported_pbo = _finite_float(payload.get("pbo"))
    reported_dsr = _finite_float(payload.get("dsr"))
    reported_selected = _finite_float(payload.get("selected_sharpe"))
    reported_variance = _finite_float(
        payload.get("sharpe_variance_across_trials")
    )
    if (
        dsr is None
        or reported_pbo is None
        or reported_dsr is None
        or reported_selected is None
        or reported_variance is None
        or not _close(reported_pbo, pbo, tolerance=1e-12)
        or not _close(reported_dsr, dsr, tolerance=1e-12)
        or not _close(reported_selected, selected_sharpe, tolerance=1e-12)
        or not _close(reported_variance, sharpe_variance, tolerance=1e-12)
    ):
        raise ReleaseRefusal("validation_statistical_pbo_dsr_results_mismatch")
    return pbo, dsr, int(returns.shape[0]), attempts, selected_id


def _validate_statistical_report(
    payload: Any,
    *,
    report_path: Path,
    ledger_sha256: str,
    cost_model_sha256: str,
    cell_evidence_sha256: str,
    expected_config_sha256: str,
    expected_source_snapshot_sha256: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "source_errors",
        "trade_ledger_sha256",
        "cost_model_sha256",
        "cell_evidence_sha256",
        "mcpt",
        "pbo_dsr",
        "statistics",
    }:
        raise ReleaseRefusal("validation_statistical_report_scope_invalid")
    if payload.get("schema_version") != STATISTICAL_REPORT_SCHEMA:
        raise ReleaseRefusal("validation_statistical_report_schema_invalid")
    if payload.get("source_errors") != []:
        raise ReleaseRefusal("validation_statistical_report_source_errors_present")
    if (
        str(payload.get("trade_ledger_sha256") or "").lower() != ledger_sha256
        or str(payload.get("cost_model_sha256") or "").lower()
        != cost_model_sha256
        or str(payload.get("cell_evidence_sha256") or "").lower()
        != cell_evidence_sha256
    ):
        raise ReleaseRefusal("validation_statistical_report_binding_invalid")
    mcpt, mcpt_observations = _recompute_mcpt(
        payload.get("mcpt"), report_path=report_path
    )
    pbo, dsr, return_observations, attempts, selected_id = _recompute_pbo_dsr(
        payload.get("pbo_dsr"),
        report_path=report_path,
        expected_config_sha256=expected_config_sha256,
        expected_source_snapshot_sha256=expected_source_snapshot_sha256,
    )
    statistics = payload.get("statistics")
    expected_statistics = {
        "mcpt_p_value",
        "pbo",
        "dsr",
        "mcpt_observations",
        "return_observations",
        "attempts",
        "selected_attempt_id",
    }
    if not isinstance(statistics, Mapping) or set(statistics) != expected_statistics:
        raise ReleaseRefusal("validation_statistical_summary_scope_invalid")
    reported_mcpt = _finite_float(statistics.get("mcpt_p_value"))
    reported_pbo = _finite_float(statistics.get("pbo"))
    reported_dsr = _finite_float(statistics.get("dsr"))
    if (
        reported_mcpt is None
        or reported_pbo is None
        or reported_dsr is None
        or not _close(reported_mcpt, mcpt, tolerance=1e-12)
        or not _close(reported_pbo, pbo, tolerance=1e-12)
        or not _close(reported_dsr, dsr, tolerance=1e-12)
        or _strict_nonnegative_int(statistics.get("mcpt_observations"))
        != mcpt_observations
        or _strict_nonnegative_int(statistics.get("return_observations"))
        != return_observations
        or _strict_nonnegative_int(statistics.get("attempts")) != attempts
        or statistics.get("selected_attempt_id") != selected_id
    ):
        raise ReleaseRefusal("validation_statistical_summary_mismatch")
    return {"mcpt_p_value": mcpt, "pbo": pbo, "dsr": dsr}


def _clustered_mean_ci(
    records: list[dict[str, Any]],
    *,
    value_field: str,
) -> tuple[float, float, float]:
    if not records:
        return 0.0, 0.0, 0.0
    by_day: dict[str, list[float]] = {}
    for record in records:
        by_day.setdefault(record["entry_utc_day"], []).append(
            float(record[value_field])
        )
    day_keys = sorted(by_day)
    values = [value for day in day_keys for value in by_day[day]]
    expectancy = sum(values) / len(values)
    if len(day_keys) < 2:
        return expectancy, expectancy, expectancy
    rng = random.Random(CLUSTERED_CI_SEED)
    samples: list[float] = []
    for _ in range(CLUSTERED_CI_RESAMPLES):
        pooled: list[float] = []
        for _ in day_keys:
            pooled.extend(by_day[day_keys[rng.randrange(len(day_keys))]])
        samples.append(sum(pooled) / len(pooled))
    samples.sort()
    lower = samples[int(0.025 * len(samples))]
    upper = samples[int(0.975 * len(samples))]
    return float(expectancy), float(lower), float(upper)


def derive_evidence_from_artifacts(
    *,
    artifact_paths: Mapping[str, str | Path],
    source_root: str | Path,
    expected_engine_sha256: str,
    expected_config_sha256: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not _is_sha256(expected_engine_sha256) or not _is_sha256(
        expected_config_sha256
    ):
        raise ReleaseRefusal("validation_expected_candidate_identity_invalid")
    paths, payloads, digests, manifest = _load_primary_artifacts(artifact_paths)
    cost_contract = _validate_cost_model(
        payloads["cost_model"],
        model_path=paths["cost_model"],
        source_root=source_root,
    )
    ledger = _validate_trade_ledger(
        payloads["trade_ledger"],
        expected_engine_sha256=expected_engine_sha256,
        expected_config_sha256=expected_config_sha256,
        cost_model_sha256=digests["cost_model"],
        cost_contract=cost_contract,
    )
    records = list(ledger["records"])
    cells = _validate_cell_evidence(
        payloads["cell_evidence"],
        ledger_sha256=digests["trade_ledger"],
        cost_model_sha256=digests["cost_model"],
        records=records,
    )
    statistics = _validate_statistical_report(
        payloads["statistical_report"],
        report_path=paths["statistical_report"],
        ledger_sha256=digests["trade_ledger"],
        cost_model_sha256=digests["cost_model"],
        cell_evidence_sha256=digests["cell_evidence"],
        expected_config_sha256=expected_config_sha256,
        expected_source_snapshot_sha256=str(
            cost_contract["source_snapshot_sha256"]
        ),
    )
    base_expectancy, base_lower, base_upper = _clustered_mean_ci(
        records, value_field="net_r_base"
    )
    doubled_expectancy, doubled_lower, doubled_upper = _clustered_mean_ci(
        records, value_field="net_r_2x"
    )
    evidence = {
        "schema_version": SCALP_VALIDATION_EVIDENCE_SCHEMA,
        "source_errors": [],
        "recorded_errors": [],
        "venue_id": IG_MT4_VENUE_ID,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "max_entries_per_symbol_utc_day": MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
        "artifact_sha256": {
            role: digests[role]
            for role in REQUIRED_EVIDENCE_ARTIFACT_SHA256_FIELDS
        },
        "overall": {
            "cost_stressed_expectancy": base_expectancy,
            "cost_stressed_ci_lower": base_lower,
            "cost_stressed_ci_upper": base_upper,
            "mcpt_p_value": statistics["mcpt_p_value"],
            "pbo": statistics["pbo"],
            "dsr": statistics["dsr"],
            "trades": len(records),
            "independent_days": int(ledger["independent_days"]),
            "max_drawdown_pct": float(ledger["maximum_drawdown_pct"]),
        },
        "two_x_cost_stress": {
            "cost_multiplier": REQUIRED_COST_STRESS_MULTIPLE,
            "expectancy": doubled_expectancy,
            "ci_lower": doubled_lower,
            "ci_upper": doubled_upper,
        },
        "cells": cells,
    }
    evidence_error = independent_evidence_error(evidence)
    if evidence_error:
        raise ReleaseRefusal(_evidence_refusal_detail(evidence, evidence_error))
    return evidence, manifest


def independent_evidence_error(evidence: Any) -> str:
    """Recompute the production evidence contract without signing anything.

    This implementation intentionally lives outside the runtime package.  The
    issuer subsequently asks the production verifier to check the signed
    result as a parity test; disagreement between the two paths refuses output.
    """

    if not isinstance(evidence, Mapping) or not evidence:
        return "validation_evidence_malformed"
    expected_top_level = {
        "schema_version",
        "source_errors",
        "recorded_errors",
        "venue_id",
        "symbol_scope",
        "max_entries_per_symbol_utc_day",
        "artifact_sha256",
        "overall",
        "two_x_cost_stress",
        "cells",
    }
    if set(evidence) != expected_top_level:
        return "validation_evidence_scope_invalid"
    if str(evidence.get("schema_version") or "") != SCALP_VALIDATION_EVIDENCE_SCHEMA:
        return "validation_evidence_schema_invalid"
    if evidence.get("source_errors") != []:
        return "validation_evidence_source_errors_present"
    if evidence.get("recorded_errors") != []:
        return "validation_evidence_recorded_errors_present"
    if str(evidence.get("venue_id") or "").strip().lower() != IG_MT4_VENUE_ID:
        return "validation_evidence_venue_invalid"
    if evidence.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS):
        return "validation_evidence_symbol_scope_invalid"
    if (
        _strict_nonnegative_int(evidence.get("max_entries_per_symbol_utc_day"))
        != MAX_ENTRIES_PER_SYMBOL_UTC_DAY
    ):
        return "validation_evidence_daily_frequency_invalid"

    artifact_sha = evidence.get("artifact_sha256")
    if not isinstance(artifact_sha, Mapping):
        return "validation_evidence_artifact_sha256_malformed"
    if set(artifact_sha) != set(REQUIRED_EVIDENCE_ARTIFACT_SHA256_FIELDS):
        return "validation_evidence_artifact_sha256_scope_invalid"
    identities = [
        str(artifact_sha[field] or "").strip().lower()
        for field in REQUIRED_EVIDENCE_ARTIFACT_SHA256_FIELDS
    ]
    if any(not _is_sha256(identity) for identity in identities):
        return "validation_evidence_artifact_sha256_invalid"
    if len(set(identities)) != len(identities):
        return "validation_evidence_artifact_sha256_not_distinct"

    overall = evidence.get("overall")
    expected_overall = {
        "cost_stressed_expectancy",
        "cost_stressed_ci_lower",
        "cost_stressed_ci_upper",
        "mcpt_p_value",
        "pbo",
        "dsr",
        "trades",
        "independent_days",
        "max_drawdown_pct",
    }
    if not isinstance(overall, Mapping) or set(overall) != expected_overall:
        return "validation_evidence_overall_malformed"
    expectancy = _finite_float(overall.get("cost_stressed_expectancy"))
    ci_lower = _finite_float(overall.get("cost_stressed_ci_lower"))
    ci_upper = _finite_float(overall.get("cost_stressed_ci_upper"))
    if (
        expectancy is None
        or expectancy <= 0.0
        or ci_lower is None
        or ci_lower <= 0.0
        or ci_upper is None
        or ci_upper < ci_lower
        or not ci_lower <= expectancy <= ci_upper
    ):
        return "validation_evidence_cost_stressed_expectancy_invalid"
    mcpt = _finite_float(overall.get("mcpt_p_value"))
    if mcpt is None or not 0.0 <= mcpt <= MAX_MCPT_P_VALUE:
        return "validation_evidence_mcpt_failed"
    pbo = _finite_float(overall.get("pbo"))
    if pbo is None or not 0.0 <= pbo <= MAX_PBO:
        return "validation_evidence_pbo_failed"
    dsr = _finite_float(overall.get("dsr"))
    if dsr is None or not MIN_DSR <= dsr <= 1.0:
        return "validation_evidence_dsr_failed"
    trades = _strict_nonnegative_int(overall.get("trades"))
    independent_days = _strict_nonnegative_int(overall.get("independent_days"))
    if trades is None or trades < MIN_TRADES:
        return "validation_evidence_trade_sample_insufficient"
    if independent_days is None or independent_days < MIN_INDEPENDENT_DAYS:
        return "validation_evidence_day_sample_insufficient"
    if independent_days > trades:
        return "validation_evidence_sample_counts_inconsistent"
    max_drawdown_pct = _finite_float(overall.get("max_drawdown_pct"))
    if (
        max_drawdown_pct is None
        or max_drawdown_pct < 0.0
        or max_drawdown_pct > MAX_DRAWDOWN_PCT
    ):
        return "validation_evidence_max_drawdown_failed"

    doubled = evidence.get("two_x_cost_stress")
    expected_stress = {"cost_multiplier", "expectancy", "ci_lower", "ci_upper"}
    if not isinstance(doubled, Mapping) or set(doubled) != expected_stress:
        return "validation_evidence_two_x_cost_stress_malformed"
    multiplier = _finite_float(doubled.get("cost_multiplier"))
    doubled_expectancy = _finite_float(doubled.get("expectancy"))
    doubled_ci_lower = _finite_float(doubled.get("ci_lower"))
    doubled_ci_upper = _finite_float(doubled.get("ci_upper"))
    if (
        multiplier is None
        or not math.isclose(
            multiplier,
            REQUIRED_COST_STRESS_MULTIPLE,
            rel_tol=0.0,
            abs_tol=_FLOAT_TOLERANCE,
        )
        or doubled_expectancy is None
        or doubled_expectancy <= 0.0
        or doubled_ci_lower is None
        or doubled_ci_lower <= 0.0
        or doubled_ci_upper is None
        or doubled_ci_upper < doubled_ci_lower
        or not doubled_ci_lower <= doubled_expectancy <= doubled_ci_upper
    ):
        return "validation_evidence_two_x_cost_stress_failed"

    cells = evidence.get("cells")
    if not isinstance(cells, Mapping) or set(cells) != set(IG_MT4_SCALP_SYMBOLS):
        return "validation_evidence_cell_scope_invalid"
    total_cell_trades = 0
    max_cell_days = 0
    total_cell_days = 0
    expected_cell_fields = {
        "trades",
        "independent_days",
        "wins",
        "win_probability",
        "win_probability_ci_lower",
        "win_probability_ci_upper",
        "win_probability_ci_method",
    }
    for symbol in IG_MT4_SCALP_SYMBOLS:
        raw_sides = cells.get(symbol)
        if not isinstance(raw_sides, Mapping) or set(raw_sides) != {"BUY", "SELL"}:
            return f"validation_evidence_cell_side_missing:{symbol}"
        for side in ("BUY", "SELL"):
            raw_cell = raw_sides.get(side)
            if not isinstance(raw_cell, Mapping) or set(raw_cell) != expected_cell_fields:
                return f"validation_evidence_cell_malformed:{symbol}:{side}"
            cell_trades = _strict_nonnegative_int(raw_cell.get("trades"))
            cell_days = _strict_nonnegative_int(raw_cell.get("independent_days"))
            wins = _strict_nonnegative_int(raw_cell.get("wins"))
            if (
                cell_trades is None
                or cell_days is None
                or wins is None
                or cell_trades < MIN_CELL_TRADES
                or cell_days < MIN_CELL_INDEPENDENT_DAYS
                or cell_days > cell_trades
                or wins > cell_trades
            ):
                return f"validation_evidence_cell_sample_invalid:{symbol}:{side}"
            point = _finite_float(raw_cell.get("win_probability"))
            stated_lower = _finite_float(raw_cell.get("win_probability_ci_lower"))
            stated_upper = _finite_float(raw_cell.get("win_probability_ci_upper"))
            recomputed_point = wins / cell_trades
            recomputed_lower, recomputed_upper = _independent_wilson_interval(
                wins=wins,
                trades=cell_trades,
            )
            if (
                raw_cell.get("win_probability_ci_method")
                != WIN_PROBABILITY_CI_METHOD
                or point is None
                or not 0.0 <= point <= 1.0
                or not math.isclose(
                    point,
                    recomputed_point,
                    rel_tol=0.0,
                    abs_tol=_FLOAT_TOLERANCE,
                )
                or stated_lower is None
                or not 0.0 < stated_lower < 1.0
                or stated_upper is None
                or not 0.0 < stated_upper <= 1.0
                or stated_lower > point
                or stated_upper < point
                or not math.isclose(
                    stated_lower,
                    recomputed_lower,
                    rel_tol=0.0,
                    abs_tol=_FLOAT_TOLERANCE,
                )
                or not math.isclose(
                    stated_upper,
                    recomputed_upper,
                    rel_tol=0.0,
                    abs_tol=_FLOAT_TOLERANCE,
                )
            ):
                return f"validation_evidence_cell_win_ci_invalid:{symbol}:{side}"
            total_cell_trades += cell_trades
            max_cell_days = max(max_cell_days, cell_days)
            total_cell_days += cell_days
    if total_cell_trades != trades:
        return "validation_evidence_trade_totals_inconsistent"
    if not max_cell_days <= independent_days <= total_cell_days:
        return "validation_evidence_day_totals_inconsistent"
    return ""


def _refusal_number(value: Any) -> str:
    numeric = _finite_float(value)
    return "invalid" if numeric is None else format(numeric, ".17g")


def _evidence_refusal_detail(evidence: Any, error: str) -> str:
    """Attach bounded observed values to fixed-gate refusals.

    The stable refusal code remains the prefix.  Values are derived from the
    already-recomputed evidence and contain no paths, credentials, or input
    payloads, so a failed prepare run is useful for legitimate strategy
    revision without publishing an issuance request.
    """

    if not isinstance(evidence, Mapping):
        return error
    overall = evidence.get("overall")
    doubled = evidence.get("two_x_cost_stress")
    if not isinstance(overall, Mapping):
        overall = {}
    if not isinstance(doubled, Mapping):
        doubled = {}
    if error == "validation_evidence_cost_stressed_expectancy_invalid":
        return (
            f"{error}"
            f":observed_expectancy={_refusal_number(overall.get('cost_stressed_expectancy'))}"
            f":observed_ci_lower={_refusal_number(overall.get('cost_stressed_ci_lower'))}"
            ":required_expectancy_gt=0:required_ci_lower_gt=0"
        )
    if error == "validation_evidence_mcpt_failed":
        return (
            f"{error}:observed={_refusal_number(overall.get('mcpt_p_value'))}"
            f":required_lte={format(MAX_MCPT_P_VALUE, '.17g')}"
        )
    if error == "validation_evidence_pbo_failed":
        return (
            f"{error}:observed={_refusal_number(overall.get('pbo'))}"
            f":required_lte={format(MAX_PBO, '.17g')}"
        )
    if error == "validation_evidence_dsr_failed":
        return (
            f"{error}:observed={_refusal_number(overall.get('dsr'))}"
            f":required_gte={format(MIN_DSR, '.17g')}"
        )
    if error == "validation_evidence_trade_sample_insufficient":
        return (
            f"{error}:observed={overall.get('trades', 'invalid')}"
            f":required_gte={MIN_TRADES}"
        )
    if error == "validation_evidence_day_sample_insufficient":
        return (
            f"{error}:observed={overall.get('independent_days', 'invalid')}"
            f":required_gte={MIN_INDEPENDENT_DAYS}"
        )
    if error == "validation_evidence_max_drawdown_failed":
        return (
            f"{error}:observed_pct={_refusal_number(overall.get('max_drawdown_pct'))}"
            f":required_lte_pct={format(MAX_DRAWDOWN_PCT, '.17g')}"
        )
    if error == "validation_evidence_two_x_cost_stress_failed":
        return (
            f"{error}"
            f":observed_expectancy={_refusal_number(doubled.get('expectancy'))}"
            f":observed_ci_lower={_refusal_number(doubled.get('ci_lower'))}"
            ":required_expectancy_gt=0:required_ci_lower_gt=0"
        )
    sample_prefix = "validation_evidence_cell_sample_invalid:"
    if error.startswith(sample_prefix):
        parts = error[len(sample_prefix) :].split(":", 1)
        cells = evidence.get("cells")
        if len(parts) == 2 and isinstance(cells, Mapping):
            symbol, side = parts
            sides = cells.get(symbol)
            cell = sides.get(side) if isinstance(sides, Mapping) else None
            if isinstance(cell, Mapping):
                return (
                    f"{error}:observed_trades={cell.get('trades', 'invalid')}"
                    f":observed_days={cell.get('independent_days', 'invalid')}"
                    f":required_trades_gte={MIN_CELL_TRADES}"
                    f":required_days_gte={MIN_CELL_INDEPENDENT_DAYS}"
                )
    return error


def _validate_request(
    request: Mapping[str, Any],
    *,
    artifact_paths: Mapping[str, str | Path],
    source_root: str | Path,
    now_epoch: float,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if set(request) != {
        "schema_version",
        "prepared_at_epoch",
        "certificate_claims",
        "artifact_manifest",
        ISSUANCE_REQUEST_SHA256_FIELD,
    }:
        raise ReleaseRefusal("issuance_request_scope_invalid")
    if str(request.get("schema_version") or "") != ISSUANCE_REQUEST_SCHEMA:
        raise ReleaseRefusal("issuance_request_schema_invalid")
    claimed_request_sha = str(
        request.get(ISSUANCE_REQUEST_SHA256_FIELD) or ""
    ).strip().lower()
    computed_request_sha = _request_body_sha256(request)
    if not _is_sha256(claimed_request_sha) or not hmac.compare_digest(
        claimed_request_sha, computed_request_sha
    ):
        raise ReleaseRefusal("issuance_request_sha256_invalid")
    claims = request.get("certificate_claims")
    if not isinstance(claims, Mapping):
        raise ReleaseRefusal("issuance_request_claims_malformed")
    claims = dict(claims)
    required_claim_fields = {
        "schema_version",
        "generation_id",
        "strategy_id",
        "strategy_version",
        "engine_sha256",
        "config_sha256",
        "venue_id",
        "symbol_scope",
        "max_entries_per_symbol_utc_day",
        "issued_at_epoch",
        "expires_at_epoch",
        "evidence",
    }
    if set(claims) != required_claim_fields:
        raise ReleaseRefusal("issuance_request_claim_scope_invalid")
    if str(claims.get("schema_version") or "") != SCALP_VALIDATION_CERTIFICATE_SCHEMA:
        raise ReleaseRefusal("issuance_request_certificate_schema_invalid")
    if not str(claims.get("generation_id") or "").strip():
        raise ReleaseRefusal("issuance_request_generation_missing")
    if claims.get("strategy_id") != SCALP_DISLOCATION_STRATEGY_ID:
        raise ReleaseRefusal("issuance_request_strategy_id_invalid")
    if claims.get("strategy_version") != SCALP_DISLOCATION_STRATEGY_VERSION:
        raise ReleaseRefusal("issuance_request_strategy_version_invalid")
    if not _is_sha256(claims.get("engine_sha256")):
        raise ReleaseRefusal("issuance_request_engine_sha256_invalid")
    if not _is_sha256(claims.get("config_sha256")):
        raise ReleaseRefusal("issuance_request_config_sha256_invalid")
    if claims.get("venue_id") != IG_MT4_VENUE_ID:
        raise ReleaseRefusal("issuance_request_venue_invalid")
    if claims.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS):
        raise ReleaseRefusal("issuance_request_symbol_scope_invalid")
    if (
        _strict_nonnegative_int(claims.get("max_entries_per_symbol_utc_day"))
        != MAX_ENTRIES_PER_SYMBOL_UTC_DAY
    ):
        raise ReleaseRefusal("issuance_request_daily_frequency_invalid")
    now = _finite_float(now_epoch)
    prepared_at = _finite_float(request.get("prepared_at_epoch"))
    issued_at = _finite_float(claims.get("issued_at_epoch"))
    expires_at = _finite_float(claims.get("expires_at_epoch"))
    if now is None or now <= 0.0:
        raise ReleaseRefusal("issuance_clock_invalid")
    if prepared_at is None or issued_at is None or expires_at is None:
        raise ReleaseRefusal("issuance_request_time_invalid")
    if not math.isclose(prepared_at, issued_at, rel_tol=0.0, abs_tol=1e-6):
        raise ReleaseRefusal("issuance_request_time_binding_invalid")
    if (
        issued_at <= 0.0
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_CERTIFICATE_VALIDITY_SECS
        or issued_at > now + 5.0
        or expires_at <= now
    ):
        raise ReleaseRefusal("issuance_request_time_window_invalid")
    evidence = claims.get("evidence")
    evidence_error = independent_evidence_error(evidence)
    if evidence_error:
        raise ReleaseRefusal(evidence_error)
    derived_evidence, manifest = derive_evidence_from_artifacts(
        artifact_paths=artifact_paths,
        source_root=source_root,
        expected_engine_sha256=str(claims["engine_sha256"]),
        expected_config_sha256=str(claims["config_sha256"]),
    )
    if evidence != derived_evidence:
        raise ReleaseRefusal("issuance_request_derived_evidence_mismatch")
    if request.get("artifact_manifest") != manifest:
        raise ReleaseRefusal("issuance_request_artifact_manifest_mismatch")
    return claims, manifest


def prepare_issuance_request(
    *,
    artifact_paths: Mapping[str, str | Path],
    source_root: str | Path,
    generation_id: str,
    output_path: str | Path,
    package_root: str | Path | None = None,
    repository_root: str | Path | None = None,
    validity_secs: float = 86_400.0,
    now_epoch: float | None = None,
) -> tuple[Path, dict[str, Any], ProductionScalpEngineIdentity]:
    generation = str(generation_id or "").strip()
    if not generation:
        raise ReleaseRefusal("issuance_generation_missing")
    now = float(time.time() if now_epoch is None else now_epoch)
    validity = _finite_float(validity_secs)
    if (
        not math.isfinite(now)
        or now <= 0.0
        or validity is None
        or validity <= 0.0
        or validity > MAX_CERTIFICATE_VALIDITY_SECS
    ):
        raise ReleaseRefusal("issuance_validity_invalid")
    selected_package_root = Path(
        package_root or (FXSTACK_SRC / "fxstack")
    ).resolve(strict=True)
    selected_repository_root = Path(repository_root or REPO_ROOT).resolve(strict=True)
    engine_identity = production_scalp_engine_identity(
        package_root=selected_package_root,
        repository_root=selected_repository_root,
    )
    policy = DislocationPolicy()
    evidence, manifest = derive_evidence_from_artifacts(
        artifact_paths=artifact_paths,
        source_root=source_root,
        expected_engine_sha256=engine_identity.engine_sha256,
        expected_config_sha256=policy.config_sha256(),
    )
    claims = {
        "schema_version": SCALP_VALIDATION_CERTIFICATE_SCHEMA,
        "generation_id": generation,
        "strategy_id": SCALP_DISLOCATION_STRATEGY_ID,
        "strategy_version": SCALP_DISLOCATION_STRATEGY_VERSION,
        "engine_sha256": engine_identity.engine_sha256,
        "config_sha256": policy.config_sha256(),
        "venue_id": IG_MT4_VENUE_ID,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "max_entries_per_symbol_utc_day": MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
        "issued_at_epoch": now,
        "expires_at_epoch": now + validity,
        "evidence": evidence,
    }
    request: dict[str, Any] = {
        "schema_version": ISSUANCE_REQUEST_SCHEMA,
        "prepared_at_epoch": now,
        "certificate_claims": claims,
        "artifact_manifest": manifest,
    }
    request[ISSUANCE_REQUEST_SHA256_FIELD] = _request_body_sha256(request)
    _validate_request(
        request,
        artifact_paths=artifact_paths,
        source_root=source_root,
        now_epoch=now,
    )
    if Path(output_path).expanduser().resolve(strict=False).is_relative_to(
        Path(source_root).expanduser().resolve(strict=True)
    ):
        raise ReleaseRefusal("output_overlaps_authority_input")
    output = _write_new_json(output_path, request)
    return output, request, engine_identity


def _load_private_key(path: str | Path) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    payload = _read_bounded(path, label="issuance_signing_key", limit=MAX_KEY_BYTES)
    try:
        key = serialization.load_pem_private_key(payload, password=None)
    except (TypeError, ValueError) as exc:
        raise ReleaseRefusal("issuance_signing_key_invalid") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ReleaseRefusal("issuance_signing_key_not_ed25519")
    return key


def _load_public_key(path: str | Path) -> Any:
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    payload = _read_bounded(path, label="issuance_verify_key", limit=MAX_KEY_BYTES)
    candidates: list[Any] = []
    for loader in (serialization.load_pem_public_key, serialization.load_ssh_public_key):
        try:
            candidates.append(loader(payload))
        except (TypeError, ValueError, UnsupportedAlgorithm):
            continue
    if len(payload) == 32:
        try:
            candidates.append(Ed25519PublicKey.from_public_bytes(payload))
        except ValueError:
            pass
    for candidate in candidates:
        if isinstance(candidate, Ed25519PublicKey):
            return candidate
    raise ReleaseRefusal("issuance_verify_key_invalid")


def _sign_certificate(certificate: dict[str, Any], signing_key: Any) -> None:
    certificate.pop(CERTIFICATE_SHA256_FIELD, None)
    certificate.pop(CERTIFICATE_SIGNATURE_FIELD, None)
    certificate["signing_key_id"] = ed25519_public_key_id(signing_key.public_key())
    certificate["evidence_sha256"] = canonical_sha256(certificate["evidence"])
    certificate[CERTIFICATE_SHA256_FIELD] = certificate_body_sha256(certificate)
    signature = signing_key.sign(_canonical_bytes(certificate))
    certificate[CERTIFICATE_SIGNATURE_FIELD] = base64.b64encode(signature).decode(
        "ascii"
    )


def _sign_registry(registry: dict[str, Any], signing_key: Any) -> None:
    registry.pop(REVOCATION_SHA256_FIELD, None)
    registry.pop(REVOCATION_SIGNATURE_FIELD, None)
    registry["signing_key_id"] = ed25519_public_key_id(signing_key.public_key())
    registry[REVOCATION_SHA256_FIELD] = revocation_body_sha256(registry)
    signature = signing_key.sign(_canonical_bytes(registry))
    registry[REVOCATION_SIGNATURE_FIELD] = base64.b64encode(signature).decode(
        "ascii"
    )


def _verify_registry(registry: Any, public_key: Any) -> tuple[int, list[str], str]:
    from cryptography.exceptions import InvalidSignature

    if not isinstance(registry, Mapping):
        raise ReleaseRefusal("previous_revocation_registry_malformed")
    registry = dict(registry)
    expected_fields = {
        "schema_version",
        "registry_revision",
        "updated_at_epoch",
        "active_certificate_sha256",
        "revoked_certificate_sha256s",
        "signing_key_id",
        REVOCATION_SHA256_FIELD,
        REVOCATION_SIGNATURE_FIELD,
    }
    if set(registry) != expected_fields:
        raise ReleaseRefusal("previous_revocation_registry_scope_invalid")
    if registry.get("schema_version") != SCALP_VALIDATION_REVOCATION_SCHEMA:
        raise ReleaseRefusal("previous_revocation_registry_schema_invalid")
    key_id = ed25519_public_key_id(public_key)
    if not key_id or registry.get("signing_key_id") != key_id:
        raise ReleaseRefusal("previous_revocation_registry_key_mismatch")
    claimed_sha = str(registry.get(REVOCATION_SHA256_FIELD) or "").lower()
    computed_sha = revocation_body_sha256(registry)
    if not _is_sha256(claimed_sha) or not hmac.compare_digest(
        claimed_sha, computed_sha
    ):
        raise ReleaseRefusal("previous_revocation_registry_hash_invalid")
    encoded = registry.get(REVOCATION_SIGNATURE_FIELD)
    if not isinstance(encoded, str) or not encoded:
        raise ReleaseRefusal("previous_revocation_registry_signature_missing")
    try:
        signature = base64.b64decode(encoded, validate=True)
        material = {
            key: value
            for key, value in registry.items()
            if key != REVOCATION_SIGNATURE_FIELD
        }
        public_key.verify(signature, _canonical_bytes(material))
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise ReleaseRefusal("previous_revocation_registry_signature_invalid") from exc
    revision = _strict_nonnegative_int(registry.get("registry_revision"))
    updated_at = _finite_float(registry.get("updated_at_epoch"))
    if revision is None or revision <= 0 or updated_at is None or updated_at <= 0.0:
        raise ReleaseRefusal("previous_revocation_registry_metadata_invalid")
    revoked_raw = registry.get("revoked_certificate_sha256s")
    if not isinstance(revoked_raw, list):
        raise ReleaseRefusal("previous_revocation_registry_ids_invalid")
    revoked = [str(item or "").strip().lower() for item in revoked_raw]
    if any(not _is_sha256(item) for item in revoked) or len(set(revoked)) != len(
        revoked
    ):
        raise ReleaseRefusal("previous_revocation_registry_ids_invalid")
    active = str(registry.get("active_certificate_sha256") or "").strip().lower()
    if not _is_sha256(active) or active in revoked:
        raise ReleaseRefusal("previous_revocation_registry_active_invalid")
    return revision, revoked, active


def _load_previous_registry(path: str | Path) -> dict[str, Any]:
    payload = _load_json_object(
        path,
        label="previous_revocation_input",
        limit=MAX_REQUEST_JSON_BYTES,
    )
    if payload.get("schema_version") == SCALP_VALIDATION_BUNDLE_SCHEMA:
        registry = payload.get("revocation_registry")
        if not isinstance(registry, Mapping):
            raise ReleaseRefusal("previous_revocation_registry_malformed")
        return dict(registry)
    return payload


def issue_validation_bundle(
    *,
    request_path: str | Path,
    artifact_paths: Mapping[str, str | Path],
    source_root: str | Path,
    signing_key_path: str | Path,
    verify_key_path: str | Path,
    output_path: str | Path,
    previous_registry_path: str | Path | None = None,
    bootstrap_registry: bool = False,
    now_epoch: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Issue a bundle only after all unsigned checks have passed again."""

    if bootstrap_registry == bool(previous_registry_path):
        raise ReleaseRefusal(
            "issuance_requires_exactly_one_of_previous_registry_or_bootstrap"
        )
    request = _load_json_object(
        request_path,
        label="issuance_request",
        limit=MAX_REQUEST_JSON_BYTES,
    )
    now = float(time.time() if now_epoch is None else now_epoch)
    claims, _ = _validate_request(
        request,
        artifact_paths=artifact_paths,
        source_root=source_root,
        now_epoch=now,
    )

    # Public revocation state is checked before private material is loaded.
    public_key = _load_public_key(verify_key_path)
    if bootstrap_registry:
        revision = 1
        revoked: list[str] = []
        previous_active = ""
    else:
        previous = _load_previous_registry(str(previous_registry_path))
        previous_revision, revoked, previous_active = _verify_registry(
            previous, public_key
        )
        revision = previous_revision + 1

    # Private material is deliberately loaded only after every evidence,
    # identity, time-window, artifact-byte, and prior-registry check passed.
    signing_key = _load_private_key(signing_key_path)
    signing_key_id = ed25519_public_key_id(signing_key.public_key())
    public_key_id = ed25519_public_key_id(public_key)
    if not signing_key_id or not hmac.compare_digest(signing_key_id, public_key_id):
        raise ReleaseRefusal("issuance_keypair_mismatch")

    certificate = dict(claims)
    _sign_certificate(certificate, signing_key)
    certificate_sha = str(certificate[CERTIFICATE_SHA256_FIELD])
    if previous_active and previous_active != certificate_sha and previous_active not in revoked:
        revoked.append(previous_active)
    registry: dict[str, Any] = {
        "schema_version": SCALP_VALIDATION_REVOCATION_SCHEMA,
        "registry_revision": revision,
        "updated_at_epoch": now,
        "active_certificate_sha256": certificate_sha,
        "revoked_certificate_sha256s": revoked,
    }
    _sign_registry(registry, signing_key)

    expectation = ScalpValidationExpectation(
        generation_id=str(certificate["generation_id"]),
        strategy_id=str(certificate["strategy_id"]),
        strategy_version=str(certificate["strategy_version"]),
        engine_sha256=str(certificate["engine_sha256"]),
        config_sha256=str(certificate["config_sha256"]),
    )
    parity = verify_scalp_validation_evidence(
        certificate=certificate,
        revocation_registry=registry,
        public_key=public_key,
        expectation=expectation,
        now_epoch=now,
    )
    if not parity.valid:
        raise ReleaseRefusal(f"production_verifier_parity_failed:{parity.reason}")
    if (
        parity.certificate_sha256 != certificate_sha
        or parity.evidence_sha256 != certificate["evidence_sha256"]
        or parity.signing_key_id != public_key_id
        or parity.symbol_scope != IG_MT4_SCALP_SYMBOLS
        or parity.max_entries_per_symbol_utc_day
        != MAX_ENTRIES_PER_SYMBOL_UTC_DAY
    ):
        raise ReleaseRefusal("production_verifier_parity_identity_mismatch")

    bundle = {
        "schema_version": SCALP_VALIDATION_BUNDLE_SCHEMA,
        "certificate": certificate,
        "revocation_registry": registry,
    }
    protected_inputs = {
        Path(request_path).expanduser().resolve(strict=True),
        Path(signing_key_path).expanduser().resolve(strict=True),
        Path(verify_key_path).expanduser().resolve(strict=True),
        *(
            Path(value).expanduser().resolve(strict=True)
            for value in artifact_paths.values()
        ),
    }
    if previous_registry_path:
        protected_inputs.add(
            Path(previous_registry_path).expanduser().resolve(strict=True)
        )
    resolved_output = Path(output_path).expanduser().resolve(strict=False)
    resolved_source_root = Path(source_root).expanduser().resolve(strict=True)
    if (
        resolved_output in protected_inputs
        or resolved_output.is_relative_to(resolved_source_root)
    ):
        raise ReleaseRefusal("output_overlaps_authority_input")
    output = _write_new_json(output_path, bundle)
    return output, bundle


def _artifact_args(args: argparse.Namespace) -> dict[str, str]:
    return {
        "trade_ledger": str(args.trade_ledger),
        "cost_model": str(args.cost_model),
        "statistical_report": str(args.statistical_report),
        "cell_evidence": str(args.cell_evidence),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare or issue an external production-scalp validation bundle; "
            "run only on the physically isolated validation/release host."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def _add_artifacts(target: argparse.ArgumentParser) -> None:
        target.add_argument("--trade-ledger", required=True)
        target.add_argument("--cost-model", required=True)
        target.add_argument("--statistical-report", required=True)
        target.add_argument("--cell-evidence", required=True)
        target.add_argument("--source-root", required=True)

    prepare = subparsers.add_parser(
        "prepare", help="validate evidence and create an unsigned issuance request"
    )
    _add_artifacts(prepare)
    prepare.add_argument("--generation-id", required=True)
    prepare.add_argument(
        "--package-root", default=str(FXSTACK_SRC / "fxstack")
    )
    prepare.add_argument("--repository-root", default=str(REPO_ROOT))
    prepare.add_argument("--validity-secs", type=float, default=86_400.0)
    prepare.add_argument("--output", required=True)

    issue = subparsers.add_parser(
        "issue", help="revalidate and sign a prepared issuance request"
    )
    issue.add_argument("--request", required=True)
    _add_artifacts(issue)
    issue.add_argument("--signing-key-file", required=True)
    issue.add_argument("--verify-key-file", required=True)
    registry = issue.add_mutually_exclusive_group(required=True)
    registry.add_argument("--previous-registry")
    registry.add_argument("--bootstrap-registry", action="store_true")
    issue.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            output, request, engine = prepare_issuance_request(
                artifact_paths=_artifact_args(args),
                source_root=args.source_root,
                generation_id=args.generation_id,
                output_path=args.output,
                package_root=args.package_root,
                repository_root=args.repository_root,
                validity_secs=args.validity_secs,
            )
            print(
                json.dumps(
                    {
                        "status": "prepared",
                        "output": str(output),
                        "request_sha256": request[ISSUANCE_REQUEST_SHA256_FIELD],
                        "engine_sha256": engine.engine_sha256,
                        "config_sha256": request["certificate_claims"][
                            "config_sha256"
                        ],
                    },
                    sort_keys=True,
                )
            )
            return 0
        output, bundle = issue_validation_bundle(
            request_path=args.request,
            artifact_paths=_artifact_args(args),
            source_root=args.source_root,
            signing_key_path=args.signing_key_file,
            verify_key_path=args.verify_key_file,
            previous_registry_path=args.previous_registry,
            bootstrap_registry=bool(args.bootstrap_registry),
            output_path=args.output,
        )
        certificate = dict(bundle["certificate"])
        registry_payload = dict(bundle["revocation_registry"])
        print(
            json.dumps(
                {
                    "status": "issued",
                    "output": str(output),
                    "certificate_sha256": certificate[CERTIFICATE_SHA256_FIELD],
                    "evidence_sha256": certificate["evidence_sha256"],
                    "signing_key_id": certificate["signing_key_id"],
                    "registry_revision": registry_payload["registry_revision"],
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ReleaseRefusal, RuntimeError, ValueError) as exc:
        print(f"external scalp validation release refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
