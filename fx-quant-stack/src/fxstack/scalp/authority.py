# AGENT: ROLE: External research prototype for scalp evidence/certificate semantics; not production entry authority.
# AGENT: ENTRYPOINT: `scalp_entry_error` (pure); `config_sha256`; `verify_certificate`.
# AGENT: PRIMARY INPUTS: command payload, bridge state snapshot, symbol_specs, arming certificate dict.
# AGENT: PRIMARY OUTPUTS: "" (offline semantic pass) or a refusal reason string; never an outbox approval.
# AGENT: CALLED BY: external scalp validation tooling and tests; excluded from the production runtime wheel.
# AGENT: STATE / SIDE EFFECTS: pure computation; certificate IO lives in `fxstack/scalp/validate.py`.
"""Research-only prototype of a future scalp live-entry authority.

The installed runtime does not import this module and the scalp compatibility
endpoint is fail-closed. These checks help define and adversarially test an
eventual production contract, but a locally signed certificate cannot enqueue
or deliver an order. A production implementation still needs immutable
engine/config/venue binding plus a DB-owned activation generation rechecked at
enqueue and broker poll.

The canonical lane guards entries with FinalEntryApproval bound to the
release ceremony (model identity, rollout allowlists). The scalper's lane
keeps the same INVARIANT -- no naked entries, server-side conjunctive
approval. This external prototype explores evidence suited to a scalp doctrine:

1. ARMING CERTIFICATE: issued exclusively by ``fxstack.scalp.validate`` when
   a signal family clears the pre-registered battery; sha-bound to the exact
   scalp config, symbol-scoped, expiring. No certificate, no live entry.
2. DEMO ATTESTATION: the broker heartbeat must attest a demo account. A real
   account is refused outright in this build -- there is no flag for it.
3. BROKER SPECS: no order without the symbol's published contract truth.
4. PROTECTION: server-side SL and TP prices are required and must sit on the
   correct side of each other for the side traded.
5. CAPS: lots within the broker's min/max, and within the margin-utilization
   ceiling at attested equity.

Every check returns a stable reason string; the first failure wins and is
recorded by the caller. Decision checks are pure; explicit key loaders are
the narrow file-backed trust-anchor boundary.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
from statistics import NormalDist
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

#: Certificates older than this are dead regardless of their expiry field --
#: a stale edge is no edge. Fourteen days forces re-validation cadence.
MAX_CERTIFICATE_AGE_SECS = 14 * 86_400.0

#: The 90% objective is one simultaneous claim over the broker's complete FX
#: universe, not 36 opportunities to choose a flattering subset after seeing
#: outcomes.  Keep this tuple independent of the configurable runtime
#: watchlist, which also contains crypto CFDs and may be operator-filtered.
CANONICAL_FX_SYMBOLS: tuple[str, ...] = (
    "EURUSD",
    "USDJPY",
    "AUDUSD",
    "GBPUSD",
    "USDCAD",
    "USDCHF",
    "EURGBP",
    "EURJPY",
    "NZDUSD",
    "AUDJPY",
    "CADJPY",
    "CHFJPY",
    "EURAUD",
    "EURCAD",
    "EURCHF",
    "GBPCAD",
    "GBPCHF",
    "GBPJPY",
)
CANONICAL_PAIR_DIRECTION_CELLS: tuple[str, ...] = tuple(
    f"{symbol}:{side}"
    for symbol in CANONICAL_FX_SYMBOLS
    for side in ("BUY", "SELL")
)

TRADE_EVIDENCE_SCHEMA = "fxstack.scalp.trade_evidence.v1"
WIN_DEFINITION = "full_predeclared_target_hit_first"
MIN_INITIAL_REWARD_RISK = 1.0
MIN_TARGET_STRESSED_COST_MULTIPLE = 4.0
MAX_TRADES_PER_CELL_UTC_DAY = 1

EVIDENCE_ARTIFACT_SHA256_FIELDS: tuple[str, ...] = (
    "trade_ledger",
    "venue_cost_artifact",
    "preregistration",
    "attempt_ledger",
)

VENUE_COST_PROVENANCE_SCHEMA = "fxstack.scalp.venue_cost_provenance.v1"
SUPPORTED_VENUE_ID = "ig_demo"
SUPPORTED_VENUE_COST_CONTRACT: dict[str, Any] = {
    "venue_id": SUPPORTED_VENUE_ID,
    "account_mode": "demo",
    "spread_source": "ig_live_executable_bid_ask_m1",
    "slippage_source": "ig_demo_executed_fill_ledger",
    "cost_basis": "stressed_round_trip_p90_spread_plus_slippage",
    "stress_quantile": 0.90,
    "includes_spread": True,
    "includes_slippage": True,
    "round_trip": True,
    "min_spread_samples_per_symbol": 200,
    "min_slippage_samples_per_symbol": 200,
}

PREREGISTRATION_SCHEMA = "fxstack.scalp.preregistration.v1"
ATTEMPT_LEDGER_SCHEMA = "fxstack.scalp.attempt_ledger.v1"

# Fixed live-arming floor for the 90% objective. Evidence may demand stricter
# thresholds, never weaker ones. The policy digest is sealed into every cert so
# config identity alone cannot silently omit or dilute the validation doctrine.
ARMING_POLICY: dict[str, Any] = {
    "version": "scalp_arming_90pct_v4",
    "min_trades": 300,
    "min_independent_days": 60,
    "min_dsr": 0.95,
    "min_positive_quarter_fraction": 0.60,
    "max_quarter_share": 0.40,
    "min_cell_trades": 30,
    "min_cell_independent_days": 10,
    "min_cell_win_rate": 0.90,
    "min_win_rate_family_confidence": 0.95,
    "win_rate_method": (
        "wilson_one_sided_bonferroni_full_target_hit_first_per_cell_utc_day"
    ),
    "canonical_fx_symbols": list(CANONICAL_FX_SYMBOLS),
    "canonical_pair_direction_cells": list(CANONICAL_PAIR_DIRECTION_CELLS),
    "trade_evidence_schema": TRADE_EVIDENCE_SCHEMA,
    "win_definition": WIN_DEFINITION,
    "min_initial_reward_risk": MIN_INITIAL_REWARD_RISK,
    "min_target_stressed_cost_multiple": MIN_TARGET_STRESSED_COST_MULTIPLE,
    "max_trades_per_cell_utc_day": MAX_TRADES_PER_CELL_UTC_DAY,
    "required_evidence_artifact_sha256": list(EVIDENCE_ARTIFACT_SHA256_FIELDS),
    "venue_cost_provenance_schema": VENUE_COST_PROVENANCE_SCHEMA,
    "supported_venue_cost_contract": SUPPORTED_VENUE_COST_CONTRACT,
    "preregistration_schema": PREREGISTRATION_SCHEMA,
    "attempt_ledger_schema": ATTEMPT_LEDGER_SCHEMA,
    "max_certificate_validity_secs": 7 * 86_400.0,
    "certificate_authentication": "ed25519_v1",
    "revocation_registry": "signed_append_only_certificate_ids_v1",
}
WIN_RATE_CI_METHOD = str(ARMING_POLICY["win_rate_method"])

ARMING_SIGNING_KEY_FILE_ENV = "FXSCALP_ARMING_SIGNING_KEY_FILE"
ARMING_VERIFY_KEY_FILE_ENV = "FXSCALP_ARMING_VERIFY_KEY_FILE"
CERTIFICATE_SIGNATURE_FIELD = "cert_signature_ed25519"
REVOCATION_SIGNATURE_FIELD = "revocation_signature_ed25519"
REVOCATION_SCHEMA = "fxstack.scalp.arming_revocations.v1"
LOADED_AUTHORITY_STATE_FIELD = "_arming_authority_state"

_REQUIRED_CERT_FIELDS = (
    "family",
    "config_sha256",
    "validation_policy_sha256",
    "symbols",
    "issued_at_epoch",
    "expires_at_epoch",
    "evidence",
    "signing_key_id",
    "cert_sha256",
    CERTIFICATE_SIGNATURE_FIELD,
)


def config_sha256(config_payload: dict[str, Any]) -> str:
    """Canonical sha over the scalp config dict (sorted keys, no floats drift)."""
    canonical = json.dumps(
        {str(k): config_payload[k] for k in sorted(config_payload)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    ).encode("utf-8")


def load_arming_signing_key(
    signing_key: Ed25519PrivateKey | None = None,
    *,
    signing_key_file: str | Path | None = None,
) -> Ed25519PrivateKey | None:
    """Resolve the operator-only Ed25519 private key without creating one.

    Key provisioning is an explicit operator action.  Missing, unreadable, or
    malformed keys return ``None`` so issuance fails closed and never silently
    creates a second trust root.
    """
    if isinstance(signing_key, Ed25519PrivateKey):
        return signing_key
    raw_path = signing_key_file or os.environ.get(ARMING_SIGNING_KEY_FILE_ENV)
    if not raw_path:
        return None
    try:
        loaded = serialization.load_pem_private_key(
            Path(raw_path).read_bytes(), password=None
        )
    except (OSError, TypeError, ValueError):
        return None
    return loaded if isinstance(loaded, Ed25519PrivateKey) else None


def load_arming_verify_key(
    verify_key: Ed25519PublicKey | None = None,
    *,
    verify_key_file: str | Path | None = None,
) -> Ed25519PublicKey | None:
    """Resolve the server-owned Ed25519 public trust anchor, fail closed."""
    if isinstance(verify_key, Ed25519PublicKey):
        return verify_key
    raw_path = verify_key_file or os.environ.get(ARMING_VERIFY_KEY_FILE_ENV)
    if not raw_path:
        return None
    try:
        loaded = serialization.load_pem_public_key(Path(raw_path).read_bytes())
    except (OSError, TypeError, ValueError):
        return None
    return loaded if isinstance(loaded, Ed25519PublicKey) else None


def arming_signing_key_id(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public_key = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()


def arming_policy_sha256() -> str:
    """Content identity of the fixed live-arming statistical floor."""
    return config_sha256(ARMING_POLICY)


def win_rate_confidence_metadata(
    *, family_cells: int, family_confidence: float
) -> dict[str, Any]:
    """Bonferroni allocation for simultaneous one-sided day-level bounds."""
    family_alpha = 1.0 - family_confidence
    if family_cells <= 0:
        return {
            "method": WIN_RATE_CI_METHOD,
            "sample_unit": "one_trade_per_pair_direction_utc_day",
            "win_definition": WIN_DEFINITION,
            "sidedness": "lower_one_sided",
            "correction": "bonferroni",
            "family_confidence": family_confidence,
            "family_alpha": family_alpha,
            "family_cells": 0,
            "cell_confidence": None,
            "cell_alpha": None,
            "z": None,
        }
    cell_alpha = family_alpha / family_cells
    cell_confidence = 1.0 - cell_alpha
    if not 0.5 < cell_confidence < 1.0:
        raise ValueError("win-rate confidence is numerically unusable")
    z = NormalDist().inv_cdf(cell_confidence)
    if not math.isfinite(z) or z <= 0.0:
        raise ValueError("win-rate confidence produced an invalid critical value")
    return {
        "method": WIN_RATE_CI_METHOD,
        "sample_unit": "one_trade_per_pair_direction_utc_day",
        "win_definition": WIN_DEFINITION,
        "sidedness": "lower_one_sided",
        "correction": "bonferroni",
        "family_confidence": family_confidence,
        "family_alpha": family_alpha,
        "family_cells": family_cells,
        "cell_confidence": cell_confidence,
        "cell_alpha": cell_alpha,
        "z": z,
    }


def wilson_lower_bound(*, wins: int, trials: int, z: float) -> float:
    """One-sided Wilson lower bound over independent UTC-day outcomes."""
    if trials <= 0 or wins < 0 or wins > trials or not math.isfinite(z) or z <= 0.0:
        return 0.0
    p_hat = wins / trials
    z_sq = z * z
    denominator = 1.0 + z_sq / trials
    center = p_hat + z_sq / (2.0 * trials)
    margin = z * math.sqrt(
        p_hat * (1.0 - p_hat) / trials + z_sq / (4.0 * trials * trials)
    )
    return max(0.0, min(1.0, (center - margin) / denominator))


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


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


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text)


def evidence_artifact_identity_error(payload: Any) -> str:
    """Validate the exact four byte identities required by the arming claim."""
    if not isinstance(payload, dict):
        return "artifact_sha256_malformed"
    if set(payload) != set(EVIDENCE_ARTIFACT_SHA256_FIELDS):
        return "artifact_sha256_scope_invalid"
    identities = [str(payload[name] or "").strip().lower() for name in payload]
    if any(not _is_sha256(value) for value in identities):
        return "artifact_sha256_invalid"
    if len(set(identities)) != len(identities):
        return "artifact_sha256_not_distinct"
    return ""


def venue_cost_provenance_error(
    provenance: Any,
    *,
    required_symbols: list[str] | tuple[str, ...],
) -> str:
    """Validate the one supported executable-venue cost evidence contract.

    A free-form venue label is not evidence.  Arming accepts only a measured
    IG demo executable bid/ask plus executed-fill artifact whose stress
    convention explicitly includes spread, slippage, and the complete round
    trip for every canonical FX pair.
    """
    if not isinstance(provenance, dict):
        return "venue_cost_provenance_malformed"
    if provenance.get("schema") != VENUE_COST_PROVENANCE_SCHEMA:
        return "venue_cost_provenance_schema_invalid"
    for field, expected in SUPPORTED_VENUE_COST_CONTRACT.items():
        actual = provenance.get(field)
        if isinstance(expected, float):
            numeric = _finite_float(actual)
            if numeric is None or not math.isclose(numeric, expected, abs_tol=1e-12):
                return f"venue_cost_provenance_unsupported:{field}"
        elif actual != expected:
            return f"venue_cost_provenance_unsupported:{field}"
    raw_symbols = provenance.get("symbols")
    if not isinstance(raw_symbols, dict):
        return "venue_cost_symbols_malformed"
    normalized_required = [str(value or "").strip().upper() for value in required_symbols]
    if normalized_required != list(CANONICAL_FX_SYMBOLS):
        return "venue_cost_symbol_scope_invalid"
    if set(raw_symbols) != set(CANONICAL_FX_SYMBOLS):
        return "venue_cost_symbol_scope_invalid"
    for symbol in CANONICAL_FX_SYMBOLS:
        try:
            row = dict(raw_symbols[symbol])
        except (KeyError, TypeError, ValueError):
            return f"venue_cost_symbol_malformed:{symbol}"
        cost = _finite_float(row.get("stressed_round_trip_cost_bps"))
        spread_samples = _strict_nonnegative_int(row.get("spread_samples"))
        slippage_samples = _strict_nonnegative_int(row.get("slippage_samples"))
        if (
            row.get("measured") is not True
            or cost is None
            or cost <= 0.0
            or spread_samples is None
            or spread_samples
            < int(SUPPORTED_VENUE_COST_CONTRACT["min_spread_samples_per_symbol"])
            or slippage_samples is None
            or slippage_samples
            < int(SUPPORTED_VENUE_COST_CONTRACT["min_slippage_samples_per_symbol"])
        ):
            return f"venue_cost_symbol_insufficient:{symbol}"
    return ""


def preregistration_contract_error(payload: Any) -> str:
    """Validate the content-bound, but not externally timestamped, precommitment."""
    if not isinstance(payload, dict):
        return "preregistration_malformed"
    if payload.get("schema") != PREREGISTRATION_SCHEMA:
        return "preregistration_schema_invalid"
    if payload.get("sealed_before_evaluation") is not True:
        return "preregistration_not_sealed"
    symbols = payload.get("canonical_fx_symbols")
    directions = payload.get("directions")
    if symbols != list(CANONICAL_FX_SYMBOLS) or directions != ["BUY", "SELL"]:
        return "preregistration_scope_invalid"
    if payload.get("win_definition") != WIN_DEFINITION:
        return "preregistration_win_definition_invalid"
    reward_risk = _finite_float(payload.get("fixed_initial_reward_risk"))
    target_cost_multiple = _finite_float(
        payload.get("min_target_stressed_cost_multiple")
    )
    max_per_day = _strict_nonnegative_int(
        payload.get("max_trades_per_cell_utc_day")
    )
    if reward_risk is None or reward_risk < MIN_INITIAL_REWARD_RISK:
        return "preregistration_reward_risk_weakened"
    if (
        target_cost_multiple is None
        or target_cost_multiple < MIN_TARGET_STRESSED_COST_MULTIPLE
    ):
        return "preregistration_target_cost_weakened"
    if max_per_day != MAX_TRADES_PER_CELL_UTC_DAY:
        return "preregistration_frequency_invalid"
    return ""


def certificate_body_sha256(cert: dict[str, Any]) -> str:
    body = {
        k: v
        for k, v in dict(cert or {}).items()
        if k
        not in {
            "cert_sha256",
            CERTIFICATE_SIGNATURE_FIELD,
            LOADED_AUTHORITY_STATE_FIELD,
        }
    }
    return config_sha256(body)


def sign_arming_certificate(
    cert: dict[str, Any], *, signing_key: Ed25519PrivateKey
) -> None:
    """Seal ``cert`` in place with an operator-held Ed25519 private key."""
    cert["signing_key_id"] = arming_signing_key_id(signing_key)
    cert["cert_sha256"] = certificate_body_sha256(cert)
    material = {
        key: value
        for key, value in cert.items()
        if key not in {CERTIFICATE_SIGNATURE_FIELD, LOADED_AUTHORITY_STATE_FIELD}
    }
    signature = signing_key.sign(_canonical_bytes(material))
    cert[CERTIFICATE_SIGNATURE_FIELD] = base64.b64encode(signature).decode("ascii")


def certificate_authentication_error(
    cert: dict[str, Any] | None,
    *,
    verify_key: Ed25519PublicKey | None,
) -> str:
    """Validate content identity and issuer authenticity before semantics."""
    if not isinstance(cert, dict) or not cert:
        return "certificate_missing"
    if verify_key is None:
        return "verify_key_unavailable"
    if str(cert.get("signing_key_id") or "") != arming_signing_key_id(verify_key):
        return "signing_key_mismatch"
    cert_sha = str(cert.get("cert_sha256") or "")
    try:
        body_sha = certificate_body_sha256(cert)
    except (TypeError, ValueError, OverflowError):
        return "body_malformed"
    if not _is_sha256(cert_sha) or not hmac.compare_digest(body_sha, cert_sha):
        return "body_tampered"
    encoded_signature = cert.get(CERTIFICATE_SIGNATURE_FIELD)
    if not isinstance(encoded_signature, str):
        return "signature_missing"
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
        material = {
            key: value
            for key, value in cert.items()
            if key not in {CERTIFICATE_SIGNATURE_FIELD, LOADED_AUTHORITY_STATE_FIELD}
        }
        verify_key.verify(signature, _canonical_bytes(material))
    except (InvalidSignature, TypeError, ValueError):
        return "signature_invalid"
    return ""


def sign_revocation_registry(
    registry: dict[str, Any], *, signing_key: Ed25519PrivateKey
) -> None:
    """Seal an append-only revoked-certificate identity registry in place."""
    registry["schema"] = REVOCATION_SCHEMA
    registry["signing_key_id"] = arming_signing_key_id(signing_key)
    material = {
        key: value
        for key, value in registry.items()
        if key != REVOCATION_SIGNATURE_FIELD
    }
    signature = signing_key.sign(_canonical_bytes(material))
    registry[REVOCATION_SIGNATURE_FIELD] = base64.b64encode(signature).decode("ascii")


def authenticated_revoked_certificate_ids(
    registry: dict[str, Any] | None,
    *,
    verify_key: Ed25519PublicKey | None,
) -> tuple[set[str], str]:
    """Return authenticated revoked IDs, or an error that callers fail on."""
    if registry is None:
        return set(), ""
    if not isinstance(registry, dict) or not registry:
        return set(), "revocation_registry_malformed"
    if verify_key is None:
        return set(), "verify_key_unavailable"
    if registry.get("schema") != REVOCATION_SCHEMA:
        return set(), "revocation_registry_schema_invalid"
    if str(registry.get("signing_key_id") or "") != arming_signing_key_id(verify_key):
        return set(), "revocation_registry_key_mismatch"
    raw_ids = registry.get("revoked_certificate_ids")
    if not isinstance(raw_ids, list):
        return set(), "revocation_registry_ids_invalid"
    ids = [str(value or "").strip().lower() for value in raw_ids]
    if any(not _is_sha256(value) for value in ids) or len(set(ids)) != len(ids):
        return set(), "revocation_registry_ids_invalid"
    active_id = str(registry.get("active_certificate_sha256") or "").strip().lower()
    if (active_id and not _is_sha256(active_id)) or active_id in ids:
        return set(), "revocation_registry_active_id_invalid"
    encoded_signature = registry.get(REVOCATION_SIGNATURE_FIELD)
    if not isinstance(encoded_signature, str):
        return set(), "revocation_registry_signature_missing"
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
        material = {
            key: value
            for key, value in registry.items()
            if key != REVOCATION_SIGNATURE_FIELD
        }
        verify_key.verify(signature, _canonical_bytes(material))
    except (InvalidSignature, TypeError, ValueError):
        return set(), "revocation_registry_signature_invalid"
    return set(ids), ""


def _hypothesis_audit_error(evidence: dict[str, Any], *, trials: int) -> str:
    try:
        audit = dict(evidence.get("hypothesis_audit") or {})
        artifact_sha256 = dict(evidence.get("artifact_sha256") or {})
    except (TypeError, ValueError):
        return "hypothesis_audit_malformed"
    attempt_ledger_sha256 = str(artifact_sha256.get("attempt_ledger") or "")
    if (
        audit.get("attempt_ledger_schema") != ATTEMPT_LEDGER_SCHEMA
        or not _is_sha256(attempt_ledger_sha256)
        or audit.get("attempt_ledger_sha256") != attempt_ledger_sha256
    ):
        return "attempt_ledger_unbound"
    mode = str(audit.get("mode") or "")
    if mode == "attempted_hypotheses":
        hypotheses = audit.get("hypotheses")
        if not isinstance(hypotheses, list):
            return "attempted_hypotheses_missing"
        normalized = [str(item or "").strip() for item in hypotheses]
        if (
            not normalized
            or any(not item for item in normalized)
            or len(set(normalized)) != len(normalized)
            or len(normalized) != trials
        ):
            return "attempted_hypotheses_incomplete"
        expected_sha = config_sha256({"hypotheses": normalized})
        if (
            _strict_nonnegative_int(audit.get("count")) != trials
            or str(audit.get("sha256") or "") != expected_sha
        ):
            return "attempted_hypotheses_unsealed"
        return ""
    if mode == "sealed_holdout":
        base = {
            "mode": mode,
            "trials": audit.get("trials"),
            "manifest_sha256": audit.get("manifest_sha256"),
            "dataset_sha256": audit.get("dataset_sha256"),
            "strategy_space_sha256": audit.get("strategy_space_sha256"),
            "sealed_before_evaluation": audit.get("sealed_before_evaluation"),
            "attempt_ledger_schema": audit.get("attempt_ledger_schema"),
            "attempt_ledger_sha256": audit.get("attempt_ledger_sha256"),
        }
        if (
            _strict_nonnegative_int(base["trials"]) != trials
            or base["sealed_before_evaluation"] is not True
            or not all(
                _is_sha256(base[key])
                for key in (
                    "manifest_sha256",
                    "dataset_sha256",
                    "strategy_space_sha256",
                )
            )
            or str(audit.get("sha256") or "") != config_sha256(base)
        ):
            return "sealed_holdout_invalid"
        return ""
    return "hypothesis_audit_required"


def arming_evidence_error(
    evidence: dict[str, Any] | None, *, covered_symbols: list[str]
) -> str:
    """Recompute the certificate's semantic gates without trusting ``passed``.

    The Ed25519 signature authenticates the issuer, while this routine still
    validates cross-field arithmetic and fixed policy minima so a signed but
    semantically invalid object cannot arm execution.
    """
    if not isinstance(evidence, dict) or not evidence:
        return "evidence_malformed"
    if evidence.get("source_errors") not in ([], ()):
        return "source_errors_present"
    reasons = evidence.get("reasons")
    if not isinstance(reasons, list) or reasons:
        return "recorded_failures_present"

    numeric_int_fields = (
        "trades",
        "independent_days",
        "trials",
        "min_trades_required",
        "min_independent_days_required",
        "min_cell_trades",
        "min_cell_independent_days",
    )
    ints = {name: _strict_nonnegative_int(evidence.get(name)) for name in numeric_int_fields}
    if any(value is None for value in ints.values()):
        return "integer_fields_invalid"
    trades = int(ints["trades"] or 0)
    independent_days = int(ints["independent_days"] or 0)
    trials = int(ints["trials"] or 0)
    min_trades = int(ints["min_trades_required"] or 0)
    min_days = int(ints["min_independent_days_required"] or 0)
    min_cell_trades = int(ints["min_cell_trades"] or 0)
    min_cell_days = int(ints["min_cell_independent_days"] or 0)
    if (
        min_trades < int(ARMING_POLICY["min_trades"])
        or min_days < int(ARMING_POLICY["min_independent_days"])
        or min_cell_trades < int(ARMING_POLICY["min_cell_trades"])
        or min_cell_days < int(ARMING_POLICY["min_cell_independent_days"])
    ):
        return "sample_threshold_weakened"
    if trades < min_trades or independent_days < min_days or trials < 1:
        return "overall_sample_insufficient"

    mean_r = _finite_float(evidence.get("mean_r"))
    ci_lo = _finite_float(evidence.get("ci_lo"))
    ci_hi = _finite_float(evidence.get("ci_hi"))
    dsr = _finite_float(evidence.get("deflated_sharpe"))
    dsr_threshold = _finite_float(evidence.get("dsr_threshold_required"))
    positive_quarter_fraction = _finite_float(
        evidence.get("min_positive_quarter_fraction_required")
    )
    max_quarter_share = _finite_float(evidence.get("max_quarter_share_required"))
    if (
        mean_r is None
        or mean_r <= 0.0
        or ci_lo is None
        or ci_lo <= 0.0
        or ci_hi is None
        or ci_hi < ci_lo
        or dsr is None
        or not 0.0 <= dsr <= 1.0
        or dsr_threshold is None
        or dsr_threshold < float(ARMING_POLICY["min_dsr"])
        or dsr < dsr_threshold
        or positive_quarter_fraction is None
        or positive_quarter_fraction
        < float(ARMING_POLICY["min_positive_quarter_fraction"])
        or not 0.0 < positive_quarter_fraction <= 1.0
        or max_quarter_share is None
        or not 0.0 < max_quarter_share <= float(ARMING_POLICY["max_quarter_share"])
    ):
        return "overall_statistics_invalid"
    required_raw = evidence.get("required_symbols")
    passing_raw = evidence.get("passing_symbols")
    if not isinstance(required_raw, list) or not isinstance(passing_raw, list):
        return "symbol_evidence_malformed"
    required = [str(item or "").strip().upper() for item in required_raw]
    passing = [str(item or "").strip().upper() for item in passing_raw]
    covered = [str(item or "").strip().upper() for item in covered_symbols]
    if (
        required != list(CANONICAL_FX_SYMBOLS)
        or passing != list(CANONICAL_FX_SYMBOLS)
        or covered != list(CANONICAL_FX_SYMBOLS)
    ):
        return "canonical_symbol_scope_invalid"

    artifact_error = evidence_artifact_identity_error(
        evidence.get("artifact_sha256")
    )
    if artifact_error:
        return artifact_error
    provenance_error = venue_cost_provenance_error(
        evidence.get("venue_cost_provenance"), required_symbols=required
    )
    if provenance_error:
        return provenance_error
    if str(evidence.get("venue") or "").strip().lower() != SUPPORTED_VENUE_ID:
        return "venue_cost_provenance_venue_mismatch"
    preregistration = evidence.get("preregistration")
    preregistration_error = preregistration_contract_error(preregistration)
    if preregistration_error:
        return preregistration_error
    hypothesis_error = _hypothesis_audit_error(evidence, trials=trials)
    if hypothesis_error:
        return hypothesis_error

    try:
        trade_contract = dict(evidence.get("trade_contract") or {})
    except (TypeError, ValueError):
        return "trade_contract_malformed"
    fixed_reward_risk = _finite_float(
        trade_contract.get("fixed_initial_reward_risk")
    )
    min_target_cost_multiple = _finite_float(
        trade_contract.get("min_target_stressed_cost_multiple")
    )
    contract_trades = _strict_nonnegative_int(trade_contract.get("trades"))
    contract_target_hits = _strict_nonnegative_int(
        trade_contract.get("full_target_hits")
    )
    contract_max_per_day = _strict_nonnegative_int(
        trade_contract.get("max_trades_per_cell_utc_day")
    )
    preregistered_reward_risk = _finite_float(
        preregistration.get("fixed_initial_reward_risk")
        if isinstance(preregistration, dict)
        else None
    )
    preregistered_cost_multiple = _finite_float(
        preregistration.get("min_target_stressed_cost_multiple")
        if isinstance(preregistration, dict)
        else None
    )
    if (
        trade_contract.get("schema") != TRADE_EVIDENCE_SCHEMA
        or trade_contract.get("win_definition") != WIN_DEFINITION
        or fixed_reward_risk is None
        or fixed_reward_risk < MIN_INITIAL_REWARD_RISK
        or preregistered_reward_risk is None
        or not math.isclose(fixed_reward_risk, preregistered_reward_risk, abs_tol=1e-12)
        or min_target_cost_multiple is None
        or min_target_cost_multiple < MIN_TARGET_STRESSED_COST_MULTIPLE
        or preregistered_cost_multiple is None
        or min_target_cost_multiple < preregistered_cost_multiple
        or contract_trades != trades
        or contract_target_hits is None
        or not 0 <= contract_target_hits <= trades
        or contract_max_per_day != MAX_TRADES_PER_CELL_UTC_DAY
    ):
        return "trade_contract_invalid"

    min_win_rate = _finite_float(evidence.get("min_cell_win_rate"))
    family_confidence = _finite_float(evidence.get("win_rate_family_confidence"))
    if (
        min_win_rate is None
        or not float(ARMING_POLICY["min_cell_win_rate"]) <= min_win_rate <= 1.0
        or family_confidence is None
        or not float(ARMING_POLICY["min_win_rate_family_confidence"])
        <= family_confidence
        < 1.0
    ):
        return "win_rate_policy_weakened"
    try:
        expected_confidence = win_rate_confidence_metadata(
            family_cells=2 * len(required), family_confidence=family_confidence
        )
        confidence = dict(evidence.get("win_rate_confidence") or {})
        cell_stats = dict(evidence.get("cell_stats") or {})
    except (TypeError, ValueError):
        return "win_rate_confidence_invalid"
    if confidence != expected_confidence or set(cell_stats) != set(required):
        return "win_rate_confidence_invalid"

    total_cell_trades = 0
    total_cell_wins = 0
    total_cell_r = 0.0
    all_days: set[str] = set()
    side_totals = {"BUY": [0, 0.0], "SELL": [0, 0.0]}
    for required_symbol in required:
        try:
            cells = dict(cell_stats[required_symbol])
        except (KeyError, TypeError, ValueError):
            return "cell_evidence_missing"
        if set(cells) != {"BUY", "SELL"}:
            return "cell_evidence_missing"
        for side in ("BUY", "SELL"):
            try:
                stats = dict(cells[side])
                cell_trades = _strict_nonnegative_int(stats.get("trades"))
                cell_days = _strict_nonnegative_int(stats.get("independent_days"))
                wins = _strict_nonnegative_int(stats.get("wins"))
                winning_days = _strict_nonnegative_int(stats.get("winning_days"))
                cell_mean = _finite_float(stats.get("mean_r"))
                cell_ci_lo = _finite_float(stats.get("ci_lo"))
                cell_ci_hi = _finite_float(stats.get("ci_hi"))
                win_rate = _finite_float(stats.get("win_rate"))
                day_win_rate = _finite_float(stats.get("day_win_rate"))
                win_rate_ci_lo = _finite_float(stats.get("win_rate_ci_lo"))
                day_stats = dict(stats.get("day_stats") or {})
                cell_confidence = dict(stats.get("win_rate_confidence") or {})
            except (TypeError, ValueError):
                return "cell_evidence_malformed"
            if None in (cell_trades, cell_days, wins, winning_days):
                return "cell_counts_invalid"
            cell_trades = int(cell_trades)
            cell_days = int(cell_days)
            wins = int(wins)
            winning_days = int(winning_days)
            if (
                cell_trades < min_cell_trades
                or cell_days < min_cell_days
                or not 0 <= wins <= cell_trades
                or not 0 <= winning_days <= cell_days
                or cell_mean is None
                or cell_mean <= 0.0
                or cell_ci_lo is None
                or cell_ci_lo <= 0.0
                or cell_ci_hi is None
                or cell_ci_hi < cell_ci_lo
                or win_rate is None
                or day_win_rate is None
                or win_rate_ci_lo is None
                or cell_confidence != confidence
                or stats.get("win_definition") != WIN_DEFINITION
                or len(day_stats) != cell_days
            ):
                return "cell_statistics_invalid"
            recomputed_trades = 0
            recomputed_wins = 0
            recomputed_winning_days = 0
            recomputed_total_r = 0.0
            for day, raw_day in day_stats.items():
                if not isinstance(day, str) or not day:
                    return "cell_day_evidence_invalid"
                try:
                    day_row = dict(raw_day)
                except (TypeError, ValueError):
                    return "cell_day_evidence_invalid"
                day_trades = _strict_nonnegative_int(day_row.get("trades"))
                day_wins = _strict_nonnegative_int(day_row.get("wins"))
                day_total_r = _finite_float(day_row.get("total_r"))
                if (
                    day_trades is None
                    or day_trades != MAX_TRADES_PER_CELL_UTC_DAY
                    or day_wins is None
                    or not 0 <= day_wins <= 1
                    or day_total_r is None
                ):
                    return "cell_day_evidence_invalid"
                recomputed_trades += day_trades
                recomputed_wins += day_wins
                recomputed_winning_days += int(day_wins == day_trades)
                recomputed_total_r += day_total_r
                all_days.add(day)
            expected_win_rate = recomputed_wins / recomputed_trades
            expected_day_rate = recomputed_winning_days / cell_days
            expected_win_ci = wilson_lower_bound(
                wins=recomputed_winning_days,
                trials=cell_days,
                z=float(expected_confidence["z"]),
            )
            if (
                recomputed_trades != cell_trades
                or recomputed_wins != wins
                or recomputed_winning_days != winning_days
                or not math.isclose(cell_mean, recomputed_total_r / cell_trades, abs_tol=1e-12)
                or not math.isclose(win_rate, expected_win_rate, abs_tol=1e-12)
                or not math.isclose(day_win_rate, expected_day_rate, abs_tol=1e-12)
                or not math.isclose(win_rate_ci_lo, expected_win_ci, abs_tol=1e-12)
                or win_rate_ci_lo < min_win_rate
            ):
                return "cell_arithmetic_invalid"
            total_cell_trades += cell_trades
            total_cell_wins += wins
            total_cell_r += recomputed_total_r
            side_totals[side][0] += cell_trades
            side_totals[side][1] += recomputed_total_r
    if (
        total_cell_trades != trades
        or total_cell_wins != contract_target_hits
        or len(all_days) != independent_days
        or not math.isclose(mean_r, total_cell_r / trades, abs_tol=1e-12)
    ):
        return "overall_arithmetic_invalid"

    try:
        side_means = dict(evidence.get("side_means") or {})
    except (TypeError, ValueError):
        return "side_evidence_invalid"
    for side, (side_trades, side_total_r) in side_totals.items():
        reported = _finite_float(side_means.get(side))
        if (
            side_trades < 1
            or reported is None
            or reported <= 0.0
            or not math.isclose(reported, side_total_r / side_trades, abs_tol=1e-12)
        ):
            return "side_evidence_invalid"

    try:
        quarter_stats = dict(evidence.get("quarter_stats") or {})
    except (TypeError, ValueError):
        return "quarter_evidence_invalid"
    scored: list[tuple[float, float]] = []
    quarter_trades_total = 0
    quarter_r_total = 0.0
    for raw_stats in quarter_stats.values():
        try:
            stats = dict(raw_stats)
        except (TypeError, ValueError):
            return "quarter_evidence_invalid"
        q_trades = _strict_nonnegative_int(stats.get("trades"))
        q_days = _strict_nonnegative_int(stats.get("days"))
        q_total_r = _finite_float(stats.get("total_r"))
        q_mean = _finite_float(stats.get("mean_r"))
        if (
            q_trades is None
            or q_trades < 1
            or q_days is None
            or q_days < 1
            or q_total_r is None
            or q_mean is None
            or not math.isclose(q_mean, q_total_r / q_trades, abs_tol=1e-12)
        ):
            return "quarter_evidence_invalid"
        quarter_trades_total += q_trades
        quarter_r_total += q_total_r
        if q_trades >= 10 and q_days >= 5:
            scored.append((q_mean, q_total_r))
    if (
        quarter_trades_total != trades
        or not math.isclose(quarter_r_total, total_cell_r, abs_tol=1e-12)
        or len(scored) < 4
    ):
        return "quarter_evidence_insufficient"
    positive_fraction = sum(mean > 0.0 for mean, _ in scored) / len(scored)
    scored_total_r = sum(total_r for _, total_r in scored)
    if not math.isfinite(scored_total_r) or scored_total_r <= 0.0:
        return "quarter_evidence_failed"
    top_share = max(total_r for _, total_r in scored) / scored_total_r
    if positive_fraction < positive_quarter_fraction or top_share > max_quarter_share:
        return "quarter_evidence_failed"
    return ""


def verify_certificate(
    cert: dict[str, Any] | None,
    *,
    now_epoch: float,
    expected_config_sha256: str,
    symbol: str,
    authority_verify_key: Ed25519PublicKey | None = None,
    authority_verify_key_file: str | Path | None = None,
    authority_state: dict[str, Any] | None = None,
    revoked_certificate_ids: set[str] | list[str] | tuple[str, ...] = (),
) -> str:
    """"" if the arming certificate authorizes this symbol now, else a reason."""
    if not isinstance(cert, dict) or not cert:
        return "scalp_arming_certificate_missing"
    checked_now = _finite_float(now_epoch)
    if checked_now is None or checked_now <= 0.0:
        return "scalp_now_epoch_invalid"
    for key in _REQUIRED_CERT_FIELDS:
        if key not in cert:
            return f"scalp_arming_certificate_malformed:{key}"
    verify_key = load_arming_verify_key(
        authority_verify_key, verify_key_file=authority_verify_key_file
    )
    authentication_error = certificate_authentication_error(
        cert, verify_key=verify_key
    )
    if authentication_error == "verify_key_unavailable":
        return "scalp_arming_verify_key_unavailable"
    if authentication_error == "body_malformed":
        return "scalp_arming_certificate_malformed:body"
    if authentication_error == "body_tampered":
        return "scalp_arming_certificate_tampered"
    if authentication_error:
        return f"scalp_arming_certificate_unauthenticated:{authentication_error}"
    cert_sha = str(cert.get("cert_sha256") or "")
    effective_authority_state = (
        authority_state
        if authority_state is not None
        else cert.get(LOADED_AUTHORITY_STATE_FIELD)
    )
    authenticated_revoked, revocation_error = authenticated_revoked_certificate_ids(
        effective_authority_state, verify_key=verify_key
    )
    if effective_authority_state is None or revocation_error:
        return "scalp_arming_revocation_state_invalid"
    active_id = str(
        effective_authority_state.get("active_certificate_sha256")
        if isinstance(effective_authority_state, dict)
        else ""
    ).strip().lower()
    if active_id != cert_sha.lower():
        return "scalp_arming_revocation_state_invalid"
    normalized_revoked = {
        str(value or "").strip().lower() for value in revoked_certificate_ids
    }
    if any(not _is_sha256(value) for value in normalized_revoked):
        return "scalp_arming_revocation_state_invalid"
    if cert_sha.lower() in normalized_revoked | authenticated_revoked:
        return "scalp_arming_certificate_revoked"
    if str(cert.get("validation_policy_sha256") or "") != arming_policy_sha256():
        return "scalp_arming_certificate_policy_mismatch"
    if not isinstance(cert.get("family"), str) or not str(cert["family"]).strip():
        return "scalp_arming_certificate_malformed:family"
    issued = _finite_float(cert.get("issued_at_epoch"))
    expires = _finite_float(cert.get("expires_at_epoch"))
    if (
        issued is None
        or expires is None
        or issued <= 0.0
        or expires <= 0.0
        or expires <= issued
        or expires - issued > float(ARMING_POLICY["max_certificate_validity_secs"])
    ):
        return "scalp_arming_certificate_malformed:timestamps"
    if checked_now >= expires:
        return "scalp_arming_certificate_expired"
    if checked_now - issued > MAX_CERTIFICATE_AGE_SECS:
        return "scalp_arming_certificate_stale"
    if issued > checked_now + 300.0:
        return "scalp_arming_certificate_future_dated"
    expected_config = str(expected_config_sha256 or "")
    if not _is_sha256(expected_config) or str(cert.get("config_sha256") or "") != expected_config:
        return "scalp_arming_certificate_config_mismatch"
    raw_symbols = cert.get("symbols")
    if not isinstance(raw_symbols, list):
        return "scalp_arming_certificate_malformed:symbols"
    symbols = [str(s or "").strip().upper() for s in raw_symbols]
    if symbols != list(CANONICAL_FX_SYMBOLS):
        return "scalp_arming_certificate_canonical_scope_mismatch"
    requested_symbol = str(symbol or "").strip().upper()
    if not requested_symbol or requested_symbol not in symbols:
        return "scalp_arming_certificate_symbol_not_covered"
    evidence_error = arming_evidence_error(cert.get("evidence"), covered_symbols=symbols)
    if evidence_error:
        return f"scalp_arming_certificate_evidence_failed:{evidence_error}"
    return ""


def scalp_entry_error(
    *,
    payload: dict[str, Any],
    state: dict[str, Any],
    specs: dict[str, dict[str, float]],
    certificate: dict[str, Any] | None,
    now_epoch: float,
    expected_config_sha256: str,
    authority_verify_key: Ed25519PublicKey | None = None,
    revoked_certificate_ids: set[str] | list[str] | tuple[str, ...] = (),
    margin_utilization_cap: float = 0.25,
) -> str:
    """First failing check of the scalp live-entry chain; "" approves."""
    if not isinstance(payload, dict) or not isinstance(state, dict):
        return "scalp_request_malformed"
    p = dict(payload)
    cmd = str(p.get("cmd") or "").strip().upper()
    if cmd not in {"BUY", "SELL"}:
        return "scalp_command_not_an_entry"
    symbol = str(p.get("symbol") or "").strip().upper()
    if not symbol:
        return "scalp_symbol_missing"

    # 2) Demo attestation -- refuse real accounts outright, no override knob.
    account_mode = str(state.get("broker_account_mode") or "").strip().lower()
    if account_mode != "demo":
        return "scalp_requires_demo_account_attestation"
    if not str(state.get("broker_account_scope") or "").strip():
        return "scalp_account_scope_unattested"

    # 1) Arming certificate (config-bound, symbol-scoped, expiring).
    cert_error = verify_certificate(
        certificate,
        now_epoch=now_epoch,
        expected_config_sha256=expected_config_sha256,
        symbol=symbol,
        authority_verify_key=authority_verify_key,
        revoked_certificate_ids=revoked_certificate_ids,
    )
    if cert_error:
        return cert_error

    # 3) Broker contract truth.
    if not isinstance(specs, dict):
        return "scalp_broker_spec_invalid"
    raw_spec = specs.get(symbol)
    if not isinstance(raw_spec, dict) or not raw_spec:
        return "scalp_broker_spec_missing"
    spec = dict(raw_spec)
    lot_size = _finite_float(spec.get("lot_size"))
    min_lot = _finite_float(spec.get("min_lot"))
    max_lot = _finite_float(spec.get("max_lot"))
    margin_required = _finite_float(spec.get("margin_required"))
    if (
        lot_size is None
        or lot_size <= 0.0
        or min_lot is None
        or min_lot <= 0.0
        or max_lot is None
        or max_lot < min_lot
        or margin_required is None
        or margin_required <= 0.0
    ):
        return "scalp_broker_spec_invalid"

    # 4) Server-side protection, side-consistent.
    lots = _finite_float(p.get("lots"))
    sl = _finite_float(p.get("sl_price"))
    tp = _finite_float(p.get("tp_price"))
    if lots is None or sl is None or tp is None:
        return "scalp_order_fields_invalid"
    if lots <= 0.0 or sl <= 0.0 or tp <= 0.0:
        return "scalp_protection_required"
    if cmd == "BUY" and not sl < tp:
        return "scalp_protection_sides_inverted"
    if cmd == "SELL" and not tp < sl:
        return "scalp_protection_sides_inverted"

    # 5) Caps: broker lot bounds, then margin ceiling at attested equity.
    if lots < min_lot:
        return "scalp_lots_below_broker_minimum"
    if lots > max_lot:
        return "scalp_lots_above_broker_maximum"
    equity = _finite_float(state.get("equity"))
    if equity is None or equity <= 0.0:
        return "scalp_equity_unattested"
    cap = _finite_float(margin_utilization_cap)
    if cap is None or not 0.0 < cap <= 1.0:
        return "scalp_margin_cap_invalid"
    if lots * margin_required > equity * cap:
        return "scalp_margin_cap_exceeded"
    return ""
