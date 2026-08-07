"""Read-only production verifier for externally issued scalper evidence.

The external validation host owns evidence production and Ed25519 signing.
This module owns only authentication and fail-closed semantic verification for
the installed runtime.  It contains no issuer, private-key, activation, broker,
database, or strategy implementation dependency.
"""

from __future__ import annotations

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
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)


SCALP_VALIDATION_CERTIFICATE_SCHEMA = "fxstack_external_scalp_validation_certificate_v1"
SCALP_VALIDATION_EVIDENCE_SCHEMA = "fxstack_external_scalp_validation_evidence_v1"
SCALP_VALIDATION_REVOCATION_SCHEMA = "fxstack_external_scalp_validation_revocations_v1"
SCALP_ADMISSION_MODE_SIGNED = "signed_validation"
SCALP_ADMISSION_MODE_DIRECT_DEMO = "direct_demo"

CERTIFICATE_SHA256_FIELD = "cert_sha256"
CERTIFICATE_SIGNATURE_FIELD = "cert_signature_ed25519"
REVOCATION_SHA256_FIELD = "registry_sha256"
REVOCATION_SIGNATURE_FIELD = "revocation_signature_ed25519"

MAX_CERTIFICATE_VALIDITY_SECS = 7 * 86_400.0
MAX_ENTRIES_PER_SYMBOL_UTC_DAY = 1
MIN_TRADES = 300
MIN_INDEPENDENT_DAYS = 60
MAX_MCPT_P_VALUE = 0.05
MAX_PBO = 0.40
MIN_DSR = 0.95
MAX_DRAWDOWN_PCT = 25.0
MIN_CELL_TRADES = 30
MIN_CELL_INDEPENDENT_DAYS = 10
REQUIRED_COST_STRESS_MULTIPLE = 2.0

WIN_PROBABILITY_FAMILY_CONFIDENCE = 0.95
WIN_PROBABILITY_CI_METHOD = "wilson_one_sided_bonferroni_95pct_44_cells_v1"
_CELL_COUNT = len(IG_MT4_SCALP_SYMBOLS) * 2
_CELL_ALPHA = (1.0 - WIN_PROBABILITY_FAMILY_CONFIDENCE) / _CELL_COUNT
_CELL_Z = NormalDist().inv_cdf(1.0 - _CELL_ALPHA)
_FLOAT_TOLERANCE = 1e-12

REQUIRED_EVIDENCE_ARTIFACT_SHA256_FIELDS: tuple[str, ...] = (
    "trade_ledger",
    "cost_model",
    "statistical_report",
    "cell_evidence",
)


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


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
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
    """Return the strict canonical JSON SHA-256 used by external signers."""

    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


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


def _ed25519_backend() -> tuple[Any, Any, Any] | None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return None
    return Ed25519PublicKey, serialization, InvalidSignature


def ed25519_public_key_id(public_key: Any | None) -> str:
    """Content identity for an explicitly supplied Ed25519 verification key."""

    backend = _ed25519_backend()
    if backend is None:
        return ""
    public_key_type, serialization, _ = backend
    if not isinstance(public_key, public_key_type):
        return ""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()


def certificate_body_sha256(certificate: Mapping[str, Any]) -> str:
    """Hash certificate claims, excluding its hash and signature envelope."""

    body = {
        key: value
        for key, value in dict(certificate).items()
        if key not in {CERTIFICATE_SHA256_FIELD, CERTIFICATE_SIGNATURE_FIELD}
    }
    return canonical_sha256(body)


def revocation_body_sha256(registry: Mapping[str, Any]) -> str:
    """Hash revocation claims, excluding its hash and signature envelope."""

    body = {
        key: value
        for key, value in dict(registry).items()
        if key not in {REVOCATION_SHA256_FIELD, REVOCATION_SIGNATURE_FIELD}
    }
    return canonical_sha256(body)


def conservative_win_probability_interval(
    *,
    wins: int,
    trades: int,
) -> tuple[float, float]:
    """Bonferroni-adjusted Wilson interval for one of the 44 required cells."""

    if (
        isinstance(wins, bool)
        or isinstance(trades, bool)
        or not isinstance(wins, int)
        or not isinstance(trades, int)
        or trades <= 0
        or wins < 0
        or wins > trades
    ):
        return 0.0, 0.0
    point = wins / trades
    z_sq = _CELL_Z * _CELL_Z
    denominator = 1.0 + z_sq / trades
    center = point + z_sq / (2.0 * trades)
    margin = _CELL_Z * math.sqrt(
        point * (1.0 - point) / trades + z_sq / (4.0 * trades * trades)
    )
    lower = max(0.0, min(1.0, (center - margin) / denominator))
    upper = max(0.0, min(1.0, (center + margin) / denominator))
    return float(lower), float(upper)


@dataclass(frozen=True, slots=True)
class ScalpValidationExpectation:
    """Exact strategy bytes and semantic version trusted by this runtime."""

    generation_id: str
    strategy_id: str
    strategy_version: str
    engine_sha256: str
    config_sha256: str

    def validation_error(self) -> str:
        if not str(self.generation_id or "").strip():
            return "validation_expected_generation_id_missing"
        if not str(self.strategy_id or "").strip():
            return "validation_expected_strategy_id_missing"
        if not str(self.strategy_version or "").strip():
            return "validation_expected_strategy_version_missing"
        if not _is_sha256(self.engine_sha256):
            return "validation_expected_engine_sha256_invalid"
        if not _is_sha256(self.config_sha256):
            return "validation_expected_config_sha256_invalid"
        return ""


@dataclass(frozen=True, slots=True)
class ScalpValidationVerification:
    """Admission output consumable by production authority.

    ``signed_validation`` retains the external evidence contract.  The
    explicit ``direct_demo`` mode is emitted only by the runtime's demo-only
    admission path and never claims signature or revocation verification.
    """

    valid: bool
    reason: str
    errors: tuple[str, ...]
    authenticated: bool = False
    revocation_verified: bool = False
    certificate_sha256: str = ""
    evidence_sha256: str = ""
    signing_key_id: str = ""
    generation_id: str = ""
    strategy_id: str = ""
    strategy_version: str = ""
    engine_sha256: str = ""
    config_sha256: str = ""
    venue_id: str = ""
    symbol_scope: tuple[str, ...] = ()
    max_entries_per_symbol_utc_day: int = 0
    issued_at_epoch: float = 0.0
    expires_at_epoch: float = 0.0
    win_probability_lower_bounds: dict[str, dict[str, float]] = field(
        default_factory=dict
    )
    win_probability_bounds_reason: str = ""
    admission_mode: str = SCALP_ADMISSION_MODE_SIGNED

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": bool(self.valid),
            "reason": str(self.reason),
            "errors": list(self.errors),
            "authenticated": bool(self.authenticated),
            "revocation_verified": bool(self.revocation_verified),
            "certificate_sha256": str(self.certificate_sha256),
            "evidence_sha256": str(self.evidence_sha256),
            "signing_key_id": str(self.signing_key_id),
            "generation_id": str(self.generation_id),
            "strategy_id": str(self.strategy_id),
            "strategy_version": str(self.strategy_version),
            "engine_sha256": str(self.engine_sha256),
            "config_sha256": str(self.config_sha256),
            "venue_id": str(self.venue_id),
            "symbol_scope": list(self.symbol_scope),
            "max_entries_per_symbol_utc_day": int(self.max_entries_per_symbol_utc_day),
            "issued_at_epoch": float(self.issued_at_epoch),
            "expires_at_epoch": float(self.expires_at_epoch),
            "win_probability_lower_bounds": {
                symbol: dict(sides)
                for symbol, sides in self.win_probability_lower_bounds.items()
            },
            "win_probability_bounds_reason": str(self.win_probability_bounds_reason),
            "admission_mode": str(self.admission_mode),
        }


def _verify_signature(
    *,
    payload: Mapping[str, Any],
    signature_field: str,
    public_key: Any,
) -> str:
    backend = _ed25519_backend()
    if backend is None:
        return "signature_backend_unavailable"
    _, _, invalid_signature = backend
    encoded = payload.get(signature_field)
    if not isinstance(encoded, str) or not encoded.strip():
        return "signature_missing"
    try:
        signature = base64.b64decode(encoded, validate=True)
        material = {
            key: value for key, value in payload.items() if key != signature_field
        }
        public_key.verify(signature, _canonical_bytes(material))
    except (invalid_signature, TypeError, ValueError):
        return "signature_invalid"
    return ""


def _artifact_identity_error(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return "validation_evidence_artifact_sha256_malformed"
    if set(payload) != set(REQUIRED_EVIDENCE_ARTIFACT_SHA256_FIELDS):
        return "validation_evidence_artifact_sha256_scope_invalid"
    identities = [
        str(payload[field] or "").strip().lower()
        for field in REQUIRED_EVIDENCE_ARTIFACT_SHA256_FIELDS
    ]
    if any(not _is_sha256(identity) for identity in identities):
        return "validation_evidence_artifact_sha256_invalid"
    if len(set(identities)) != len(identities):
        return "validation_evidence_artifact_sha256_not_distinct"
    return ""


def _evidence_error_and_bounds(
    evidence: Any,
) -> tuple[str, dict[str, dict[str, float]]]:
    if not isinstance(evidence, Mapping) or not evidence:
        return "validation_evidence_malformed", {}
    if str(evidence.get("schema_version") or "") != SCALP_VALIDATION_EVIDENCE_SCHEMA:
        return "validation_evidence_schema_invalid", {}

    source_errors = evidence.get("source_errors")
    if not isinstance(source_errors, list):
        return "validation_evidence_source_errors_malformed", {}
    if source_errors:
        return "validation_evidence_source_errors_present", {}
    recorded_errors = evidence.get("recorded_errors")
    if not isinstance(recorded_errors, list):
        return "validation_evidence_recorded_errors_malformed", {}
    if recorded_errors:
        return "validation_evidence_recorded_errors_present", {}

    if str(evidence.get("venue_id") or "").strip().lower() != IG_MT4_VENUE_ID:
        return "validation_evidence_venue_invalid", {}
    if evidence.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS):
        return "validation_evidence_symbol_scope_invalid", {}
    if (
        _strict_nonnegative_int(evidence.get("max_entries_per_symbol_utc_day"))
        != MAX_ENTRIES_PER_SYMBOL_UTC_DAY
    ):
        return "validation_evidence_daily_frequency_invalid", {}

    artifact_error = _artifact_identity_error(evidence.get("artifact_sha256"))
    if artifact_error:
        return artifact_error, {}

    overall = evidence.get("overall")
    if not isinstance(overall, Mapping):
        return "validation_evidence_overall_malformed", {}
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
        return "validation_evidence_cost_stressed_expectancy_invalid", {}

    mcpt = _finite_float(overall.get("mcpt_p_value"))
    if mcpt is None or not 0.0 <= mcpt <= MAX_MCPT_P_VALUE:
        return "validation_evidence_mcpt_failed", {}
    pbo = _finite_float(overall.get("pbo"))
    if pbo is None or not 0.0 <= pbo <= MAX_PBO:
        return "validation_evidence_pbo_failed", {}
    dsr = _finite_float(overall.get("dsr"))
    if dsr is None or not MIN_DSR <= dsr <= 1.0:
        return "validation_evidence_dsr_failed", {}

    trades = _strict_nonnegative_int(overall.get("trades"))
    independent_days = _strict_nonnegative_int(overall.get("independent_days"))
    if trades is None or trades < MIN_TRADES:
        return "validation_evidence_trade_sample_insufficient", {}
    if independent_days is None or independent_days < MIN_INDEPENDENT_DAYS:
        return "validation_evidence_day_sample_insufficient", {}
    if independent_days > trades:
        return "validation_evidence_sample_counts_inconsistent", {}

    max_drawdown_pct = _finite_float(overall.get("max_drawdown_pct"))
    if (
        max_drawdown_pct is None
        or max_drawdown_pct < 0.0
        or max_drawdown_pct > MAX_DRAWDOWN_PCT
    ):
        return "validation_evidence_max_drawdown_failed", {}

    doubled = evidence.get("two_x_cost_stress")
    if not isinstance(doubled, Mapping):
        return "validation_evidence_two_x_cost_stress_malformed", {}
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
        return "validation_evidence_two_x_cost_stress_failed", {}

    cells = evidence.get("cells")
    if not isinstance(cells, Mapping) or set(cells) != set(IG_MT4_SCALP_SYMBOLS):
        return "validation_evidence_cell_scope_invalid", {}

    bounds: dict[str, dict[str, float]] = {}
    total_cell_trades = 0
    max_cell_days = 0
    total_cell_days = 0
    for symbol in IG_MT4_SCALP_SYMBOLS:
        raw_sides = cells.get(symbol)
        if not isinstance(raw_sides, Mapping) or set(raw_sides) != {"BUY", "SELL"}:
            return f"validation_evidence_cell_side_missing:{symbol}", {}
        bounds[symbol] = {}
        for side in ("BUY", "SELL"):
            raw_cell = raw_sides.get(side)
            if not isinstance(raw_cell, Mapping):
                return f"validation_evidence_cell_malformed:{symbol}:{side}", {}
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
                return f"validation_evidence_cell_sample_invalid:{symbol}:{side}", {}

            point = _finite_float(raw_cell.get("win_probability"))
            stated_lower = _finite_float(raw_cell.get("win_probability_ci_lower"))
            stated_upper = _finite_float(raw_cell.get("win_probability_ci_upper"))
            recomputed_point = wins / cell_trades
            recomputed_lower, recomputed_upper = conservative_win_probability_interval(
                wins=wins,
                trades=cell_trades,
            )
            if (
                raw_cell.get("win_probability_ci_method") != WIN_PROBABILITY_CI_METHOD
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
                return f"validation_evidence_cell_win_ci_invalid:{symbol}:{side}", {}
            bounds[symbol][side] = float(recomputed_lower)
            total_cell_trades += cell_trades
            max_cell_days = max(max_cell_days, cell_days)
            total_cell_days += cell_days

    if total_cell_trades != trades:
        return "validation_evidence_trade_totals_inconsistent", {}
    if not max_cell_days <= independent_days <= total_cell_days:
        return "validation_evidence_day_totals_inconsistent", {}
    return "", bounds


def verify_scalp_validation_evidence(
    *,
    certificate: Mapping[str, Any] | None,
    revocation_registry: Mapping[str, Any] | None,
    public_key: Any | None,
    expectation: ScalpValidationExpectation,
    now_epoch: float,
) -> ScalpValidationVerification:
    """Authenticate and recompute every gate without granting authority itself."""

    key_id = ed25519_public_key_id(public_key)
    certificate_sha = ""
    evidence_sha = ""
    authenticated = False
    revocation_verified = False

    def _result(
        reason: str,
        *,
        bounds: dict[str, dict[str, float]] | None = None,
        issued_at: float = 0.0,
        expires_at: float = 0.0,
    ) -> ScalpValidationVerification:
        valid = not reason
        return ScalpValidationVerification(
            valid=valid,
            reason=str(reason),
            errors=() if valid else (str(reason),),
            authenticated=bool(authenticated),
            revocation_verified=bool(revocation_verified),
            certificate_sha256=str(certificate_sha),
            evidence_sha256=str(evidence_sha),
            signing_key_id=str(key_id),
            generation_id=str(expectation.generation_id),
            strategy_id=str(expectation.strategy_id),
            strategy_version=str(expectation.strategy_version),
            engine_sha256=str(expectation.engine_sha256).strip().lower(),
            config_sha256=str(expectation.config_sha256).strip().lower(),
            venue_id=IG_MT4_VENUE_ID if valid else "",
            symbol_scope=IG_MT4_SCALP_SYMBOLS if valid else (),
            max_entries_per_symbol_utc_day=(
                MAX_ENTRIES_PER_SYMBOL_UTC_DAY if valid else 0
            ),
            issued_at_epoch=float(issued_at),
            expires_at_epoch=float(expires_at),
            win_probability_lower_bounds=dict(bounds or {}) if valid else {},
            win_probability_bounds_reason="" if valid else str(reason),
        )

    expectation_error = expectation.validation_error()
    if expectation_error:
        return _result(expectation_error)
    backend = _ed25519_backend()
    if backend is None or not isinstance(public_key, backend[0]) or not key_id:
        return _result("validation_public_key_unavailable")
    if not isinstance(certificate, Mapping) or not certificate:
        return _result("validation_certificate_missing")
    cert = dict(certificate)
    if str(cert.get("signing_key_id") or "") != key_id:
        return _result("validation_certificate_signing_key_mismatch")
    claimed_certificate_sha = str(cert.get(CERTIFICATE_SHA256_FIELD) or "").lower()
    try:
        computed_certificate_sha = certificate_body_sha256(cert)
    except (TypeError, ValueError, OverflowError):
        return _result("validation_certificate_body_malformed")
    if not _is_sha256(claimed_certificate_sha) or not hmac.compare_digest(
        claimed_certificate_sha,
        computed_certificate_sha,
    ):
        return _result("validation_certificate_body_hash_invalid")
    signature_error = _verify_signature(
        payload=cert,
        signature_field=CERTIFICATE_SIGNATURE_FIELD,
        public_key=public_key,
    )
    if signature_error:
        return _result(f"validation_certificate_{signature_error}")
    authenticated = True
    certificate_sha = claimed_certificate_sha

    raw_evidence = cert.get("evidence")
    if not isinstance(raw_evidence, Mapping):
        return _result("validation_evidence_malformed")
    try:
        computed_evidence_sha = canonical_sha256(raw_evidence)
    except (TypeError, ValueError, OverflowError):
        return _result("validation_evidence_malformed")
    claimed_evidence_sha = str(cert.get("evidence_sha256") or "").lower()
    if not _is_sha256(claimed_evidence_sha) or not hmac.compare_digest(
        claimed_evidence_sha, computed_evidence_sha
    ):
        return _result("validation_evidence_sha256_mismatch")
    evidence_sha = computed_evidence_sha

    if not isinstance(revocation_registry, Mapping) or not revocation_registry:
        return _result("validation_revocation_registry_missing")
    registry = dict(revocation_registry)
    if str(registry.get("schema_version") or "") != SCALP_VALIDATION_REVOCATION_SCHEMA:
        return _result("validation_revocation_registry_schema_invalid")
    if str(registry.get("signing_key_id") or "") != key_id:
        return _result("validation_revocation_registry_signing_key_mismatch")
    claimed_registry_sha = str(registry.get(REVOCATION_SHA256_FIELD) or "").lower()
    try:
        computed_registry_sha = revocation_body_sha256(registry)
    except (TypeError, ValueError, OverflowError):
        return _result("validation_revocation_registry_body_malformed")
    if not _is_sha256(claimed_registry_sha) or not hmac.compare_digest(
        claimed_registry_sha, computed_registry_sha
    ):
        return _result("validation_revocation_registry_body_hash_invalid")
    revocation_signature_error = _verify_signature(
        payload=registry,
        signature_field=REVOCATION_SIGNATURE_FIELD,
        public_key=public_key,
    )
    if revocation_signature_error:
        return _result(f"validation_revocation_registry_{revocation_signature_error}")

    revision = _strict_nonnegative_int(registry.get("registry_revision"))
    updated_at = _finite_float(registry.get("updated_at_epoch"))
    if revision is None or revision <= 0 or updated_at is None or updated_at <= 0.0:
        return _result("validation_revocation_registry_metadata_invalid")
    raw_revoked = registry.get("revoked_certificate_sha256s")
    if not isinstance(raw_revoked, list):
        return _result("validation_revocation_registry_ids_invalid")
    revoked = [str(item or "").strip().lower() for item in raw_revoked]
    if any(not _is_sha256(item) for item in revoked) or len(set(revoked)) != len(
        revoked
    ):
        return _result("validation_revocation_registry_ids_invalid")
    active_id = str(registry.get("active_certificate_sha256") or "").lower()
    if active_id and not _is_sha256(active_id):
        return _result("validation_revocation_registry_active_id_invalid")
    revocation_verified = True
    if certificate_sha in revoked:
        return _result("validation_certificate_revoked")
    if active_id != certificate_sha:
        return _result("validation_certificate_not_active")

    now = _finite_float(now_epoch)
    issued_at = _finite_float(cert.get("issued_at_epoch"))
    expires_at = _finite_float(cert.get("expires_at_epoch"))
    if now is None or now <= 0.0:
        return _result("validation_clock_invalid")
    if (
        issued_at is None
        or expires_at is None
        or issued_at <= 0.0
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_CERTIFICATE_VALIDITY_SECS
    ):
        return _result("validation_certificate_time_window_invalid")
    if issued_at > now + 5.0:
        return _result(
            "validation_certificate_future_dated",
            issued_at=issued_at,
            expires_at=expires_at,
        )
    if expires_at <= now:
        return _result(
            "validation_certificate_expired",
            issued_at=issued_at,
            expires_at=expires_at,
        )

    if str(cert.get("schema_version") or "") != SCALP_VALIDATION_CERTIFICATE_SCHEMA:
        return _result("validation_certificate_schema_invalid")
    if str(cert.get("generation_id") or "") != expectation.generation_id:
        return _result("validation_certificate_generation_id_mismatch")
    if str(cert.get("strategy_id") or "") != expectation.strategy_id:
        return _result("validation_certificate_strategy_id_mismatch")
    if str(cert.get("strategy_version") or "") != expectation.strategy_version:
        return _result("validation_certificate_strategy_version_mismatch")
    if (
        str(cert.get("engine_sha256") or "").lower()
        != str(expectation.engine_sha256).lower()
    ):
        return _result("validation_certificate_engine_sha256_mismatch")
    if (
        str(cert.get("config_sha256") or "").lower()
        != str(expectation.config_sha256).lower()
    ):
        return _result("validation_certificate_config_sha256_mismatch")
    if str(cert.get("venue_id") or "").strip().lower() != IG_MT4_VENUE_ID:
        return _result("validation_certificate_venue_invalid")
    if cert.get("symbol_scope") != list(IG_MT4_SCALP_SYMBOLS):
        return _result("validation_certificate_symbol_scope_invalid")
    if (
        _strict_nonnegative_int(cert.get("max_entries_per_symbol_utc_day"))
        != MAX_ENTRIES_PER_SYMBOL_UTC_DAY
    ):
        return _result("validation_certificate_daily_frequency_invalid")

    evidence_error, bounds = _evidence_error_and_bounds(raw_evidence)
    if evidence_error:
        return _result(
            evidence_error,
            issued_at=issued_at,
            expires_at=expires_at,
        )
    return _result(
        "",
        bounds=bounds,
        issued_at=issued_at,
        expires_at=expires_at,
    )


__all__ = [
    "CERTIFICATE_SHA256_FIELD",
    "CERTIFICATE_SIGNATURE_FIELD",
    "MAX_CERTIFICATE_VALIDITY_SECS",
    "MAX_DRAWDOWN_PCT",
    "MAX_ENTRIES_PER_SYMBOL_UTC_DAY",
    "MAX_MCPT_P_VALUE",
    "MAX_PBO",
    "MIN_CELL_INDEPENDENT_DAYS",
    "MIN_CELL_TRADES",
    "MIN_DSR",
    "MIN_INDEPENDENT_DAYS",
    "MIN_TRADES",
    "REQUIRED_COST_STRESS_MULTIPLE",
    "REQUIRED_EVIDENCE_ARTIFACT_SHA256_FIELDS",
    "REVOCATION_SHA256_FIELD",
    "REVOCATION_SIGNATURE_FIELD",
    "SCALP_VALIDATION_CERTIFICATE_SCHEMA",
    "SCALP_VALIDATION_EVIDENCE_SCHEMA",
    "SCALP_VALIDATION_REVOCATION_SCHEMA",
    "ScalpValidationExpectation",
    "ScalpValidationVerification",
    "WIN_PROBABILITY_CI_METHOD",
    "WIN_PROBABILITY_FAMILY_CONFIDENCE",
    "canonical_sha256",
    "certificate_body_sha256",
    "conservative_win_probability_interval",
    "ed25519_public_key_id",
    "revocation_body_sha256",
    "verify_scalp_validation_evidence",
]
