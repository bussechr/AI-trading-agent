"""Production-owned identity contract for the IG MT4 scalper.

This module deliberately contains no signal research, certificate issuer, or
filesystem activation logic.  It defines the immutable facts that a trusted
runtime must bind before a scalper entry can share the production command
queue: the exact strategy engine and configuration, the IG MT4 venue, the
complete broker universe, the broker-native bracket policy, the daily entry
frequency, and the current runtime boot/authority generation.

The command store consumes this contract at enqueue and broker poll.  Keeping
the verifier in ``fxstack.runtime`` means the installed runtime wheel never
has to import the excluded ``fxstack.scalp`` research package.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import time
from typing import Any, Iterable

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_CATALOG,
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.runtime.mtvclc_runtime_release import (
    MTVCLCRuntimeReleaseVerification,
)
from fxstack.runtime.scalp_validation_evidence import (
    SCALP_ADMISSION_MODE_DIRECT_DEMO,
    SCALP_ADMISSION_MODE_SIGNED,
)
from fxstack.strategy.mtvclc import (
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
)


SCALP_EXECUTION_AUTHORITY_SCHEMA = "fxstack_production_scalp_authority_v3"
SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA = "fxstack_production_scalp_authority_v2"
SCALP_EXECUTION_LANE = "production_scalper"
SCALP_ENTRY_INTENT = "production_scalper_entry"
SCALP_SLEEVE = "scalp"
BROKER_NATIVE_BRACKET_POLICY = "broker_native_sl_tp"
MAX_ENTRIES_PER_SYMBOL_UTC_DAY = 1

_AUTHORITY_BINDING_FIELDS_V3: tuple[str, ...] = (
    "schema_version",
    "source",
    "admission_mode",
    "account_mode",
    "generation_id",
    "strategy_id",
    "strategy_version",
    "engine_sha256",
    "config_id",
    "config_sha256",
    "runtime_release_certificate_sha256",
    "runtime_release_signing_key_id",
    "research_evidence_sha256",
    "research_evidence_signing_key_id",
    "registry_generation_id",
    "registry_revision",
    "registry_sha256",
    "qualification_surface_sha256",
    "cost_mapping_sha256",
    "execution_contract_sha256",
    "validation_expires_at_epoch",
    "venue_id",
    "scope_version",
    "symbol_scope",
    "bracket_policy",
    "max_entries_per_symbol_utc_day",
    "runtime_boot_id",
    "authority_revision",
)

_AUTHORITY_BINDING_FIELDS_V2: tuple[str, ...] = (
    "schema_version",
    "source",
    "admission_mode",
    "account_mode",
    "generation_id",
    "strategy_id",
    "engine_sha256",
    "config_sha256",
    "validation_evidence_sha256",
    "validation_expires_at_epoch",
    "venue_id",
    "symbol_scope",
    "bracket_policy",
    "max_entries_per_symbol_utc_day",
    "runtime_boot_id",
    "authority_revision",
)

_COMMAND_BINDING_FIELDS_V3: tuple[tuple[str, str], ...] = (
    ("expected_strategy_authority_schema", "schema_version"),
    ("expected_strategy_admission_mode", "admission_mode"),
    ("expected_strategy_account_mode", "account_mode"),
    ("expected_strategy_generation_id", "generation_id"),
    ("expected_strategy_id", "strategy_id"),
    ("expected_strategy_version", "strategy_version"),
    ("expected_strategy_engine_sha256", "engine_sha256"),
    ("expected_strategy_config_id", "config_id"),
    ("expected_strategy_config_sha256", "config_sha256"),
    (
        "expected_strategy_runtime_release_certificate_sha256",
        "runtime_release_certificate_sha256",
    ),
    (
        "expected_strategy_runtime_release_signing_key_id",
        "runtime_release_signing_key_id",
    ),
    (
        "expected_strategy_research_evidence_sha256",
        "research_evidence_sha256",
    ),
    (
        "expected_strategy_research_evidence_signing_key_id",
        "research_evidence_signing_key_id",
    ),
    (
        "expected_strategy_registry_generation_id",
        "registry_generation_id",
    ),
    ("expected_strategy_registry_revision", "registry_revision"),
    ("expected_strategy_registry_sha256", "registry_sha256"),
    (
        "expected_strategy_qualification_surface_sha256",
        "qualification_surface_sha256",
    ),
    ("expected_strategy_cost_mapping_sha256", "cost_mapping_sha256"),
    (
        "expected_strategy_execution_contract_sha256",
        "execution_contract_sha256",
    ),
    (
        "expected_strategy_validation_expires_at_epoch",
        "validation_expires_at_epoch",
    ),
    ("expected_strategy_venue_id", "venue_id"),
    ("expected_strategy_scope_version", "scope_version"),
    ("expected_strategy_binding_sha256", "binding_sha256"),
    ("expected_strategy_runtime_boot_id", "runtime_boot_id"),
    ("expected_strategy_authority_revision", "authority_revision"),
)

_COMMAND_BINDING_FIELDS_V2: tuple[tuple[str, str], ...] = (
    ("expected_strategy_authority_schema", "schema_version"),
    ("expected_strategy_admission_mode", "admission_mode"),
    ("expected_strategy_account_mode", "account_mode"),
    ("expected_strategy_generation_id", "generation_id"),
    ("expected_strategy_id", "strategy_id"),
    ("expected_strategy_engine_sha256", "engine_sha256"),
    ("expected_strategy_config_sha256", "config_sha256"),
    (
        "expected_strategy_validation_evidence_sha256",
        "validation_evidence_sha256",
    ),
    (
        "expected_strategy_validation_expires_at_epoch",
        "validation_expires_at_epoch",
    ),
    ("expected_strategy_venue_id", "venue_id"),
    ("expected_strategy_binding_sha256", "binding_sha256"),
    ("expected_strategy_runtime_boot_id", "runtime_boot_id"),
    ("expected_strategy_authority_revision", "authority_revision"),
)

# Active entry consumers use only the v3 field set. Legacy v2 fields are
# selected explicitly inside protective-history parsing below.
_COMMAND_BINDING_FIELDS = _COMMAND_BINDING_FIELDS_V3


def _canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text)


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _normalized_symbols(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(str(value or "").strip().upper() for value in values)


def symbol_scope_error(values: Iterable[Any]) -> str:
    """Return why ``values`` is not the complete canonical IG MT4 scope."""

    symbols = _normalized_symbols(values)
    if not symbols:
        return "scalp_authority_symbol_scope_missing"
    if any(not symbol for symbol in symbols):
        return "scalp_authority_symbol_scope_malformed"
    if len(set(symbols)) != len(symbols):
        return "scalp_authority_symbol_scope_duplicate"
    if set(symbols) != set(IG_MT4_SCALP_SYMBOLS):
        return "scalp_authority_symbol_scope_incomplete"
    if symbols != tuple(IG_MT4_SCALP_SYMBOLS):
        return "scalp_authority_symbol_scope_order_changed"
    return ""


def authority_binding_sha256(authority: dict[str, Any]) -> str:
    """Hash the execution-semantic authority fields only."""

    schema = str(authority.get("schema_version") or "")
    fields = (
        _AUTHORITY_BINDING_FIELDS_V2
        if schema == SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA
        else _AUTHORITY_BINDING_FIELDS_V3
    )
    payload = {
        field: authority.get(field)
        for field in fields
    }
    payload["symbol_scope"] = list(
        _normalized_symbols(payload.get("symbol_scope") or [])
    )
    return _sha256_json(payload)


@dataclass(frozen=True, slots=True)
class ScalpAuthorityExpectation:
    """Exact MTVCLC runtime-release identity expected by one runtime boot."""

    generation_id: str
    strategy_id: str
    engine_sha256: str
    config_sha256: str
    validation_expires_at_epoch: float
    runtime_boot_id: str
    authority_revision: int
    strategy_version: str = ""
    config_id: str = ""
    runtime_release_certificate_sha256: str = ""
    runtime_release_signing_key_id: str = ""
    research_evidence_sha256: str = ""
    research_evidence_signing_key_id: str = ""
    registry_generation_id: str = ""
    registry_revision: int = 0
    registry_sha256: str = ""
    qualification_surface_sha256: str = ""
    cost_mapping_sha256: str = ""
    execution_contract_sha256: str = ""
    # Parsed only to make old call sites fail closed with a named reason. It is
    # never admitted into v3 active authority.
    validation_evidence_sha256: str = ""
    admission_mode: str = SCALP_ADMISSION_MODE_SIGNED
    account_mode: str = "demo"
    venue_id: str = IG_MT4_VENUE_ID
    scope_version: str = IG_MT4_SCALP_SCOPE_VERSION
    bracket_policy: str = BROKER_NATIVE_BRACKET_POLICY
    max_entries_per_symbol_utc_day: int = MAX_ENTRIES_PER_SYMBOL_UTC_DAY
    symbol_scope: tuple[str, ...] = IG_MT4_SCALP_SYMBOLS

    def validation_error(self) -> str:
        admission_mode = str(self.admission_mode or "").strip().lower()
        account_mode = str(self.account_mode or "").strip().lower()
        if admission_mode != SCALP_ADMISSION_MODE_SIGNED:
            return "scalp_authority_admission_mode_invalid"
        if account_mode not in {"demo", "real"}:
            return "scalp_authority_account_mode_invalid"
        if not str(self.generation_id or "").strip():
            return "scalp_authority_generation_missing"
        if self.strategy_id != MTVCLC_STRATEGY_ID:
            return "scalp_authority_strategy_missing"
        if self.strategy_version != MTVCLC_STRATEGY_VERSION:
            return "scalp_authority_strategy_version_invalid"
        if not _is_sha256(self.engine_sha256):
            return "scalp_authority_engine_identity_invalid"
        if self.config_id != MTVCLC_CONFIG_ID:
            return "scalp_authority_config_id_invalid"
        if (
            not _is_sha256(self.config_sha256)
            or self.config_sha256 != MTVCLC_CONFIG_SHA256
        ):
            return "scalp_authority_config_identity_invalid"
        if self.validation_evidence_sha256:
            return "scalp_authority_legacy_validation_identity_forbidden"
        digest_fields = (
            (
                self.runtime_release_certificate_sha256,
                "scalp_authority_runtime_release_certificate_invalid",
            ),
            (
                self.runtime_release_signing_key_id,
                "scalp_authority_runtime_release_key_invalid",
            ),
            (
                self.research_evidence_sha256,
                "scalp_authority_research_evidence_invalid",
            ),
            (
                self.research_evidence_signing_key_id,
                "scalp_authority_research_evidence_key_invalid",
            ),
            (self.registry_sha256, "scalp_authority_registry_identity_invalid"),
            (
                self.qualification_surface_sha256,
                "scalp_authority_qualification_surface_invalid",
            ),
            (
                self.cost_mapping_sha256,
                "scalp_authority_cost_mapping_invalid",
            ),
            (
                self.execution_contract_sha256,
                "scalp_authority_execution_contract_invalid",
            ),
        )
        for value, reason in digest_fields:
            if not _is_sha256(value):
                return reason
        if (
            not str(self.registry_generation_id or "").strip()
            or self.registry_generation_id != self.generation_id
        ):
            return "scalp_authority_registry_generation_invalid"
        if _safe_int(self.registry_revision) <= 0:
            return "scalp_authority_registry_revision_invalid"
        try:
            validation_expires_at = float(self.validation_expires_at_epoch)
        except (TypeError, ValueError, OverflowError):
            validation_expires_at = 0.0
        if not math.isfinite(validation_expires_at) or validation_expires_at <= 0.0:
            return "scalp_authority_validation_expiry_invalid"
        if not str(self.runtime_boot_id or "").strip():
            return "scalp_authority_runtime_boot_missing"
        if _safe_int(self.authority_revision) <= 0:
            return "scalp_authority_revision_invalid"
        if str(self.venue_id or "").strip().lower() != IG_MT4_VENUE_ID:
            return "scalp_authority_venue_invalid"
        if str(self.scope_version or "") != IG_MT4_SCALP_SCOPE_VERSION:
            return "scalp_authority_scope_version_invalid"
        if str(self.bracket_policy or "").strip().lower() != BROKER_NATIVE_BRACKET_POLICY:
            return "scalp_authority_bracket_policy_invalid"
        if _safe_int(self.max_entries_per_symbol_utc_day) != MAX_ENTRIES_PER_SYMBOL_UTC_DAY:
            return "scalp_authority_daily_frequency_invalid"
        return symbol_scope_error(self.symbol_scope)


def expectation_from_mtvclc_runtime_release(
    verification: MTVCLCRuntimeReleaseVerification,
    *,
    runtime_boot_id: str,
    authority_revision: int,
) -> ScalpAuthorityExpectation:
    """Project the normalized public verifier result into the v3 contract."""

    if not isinstance(verification, MTVCLCRuntimeReleaseVerification):
        raise ValueError("scalp_authority_runtime_release_verification_required")
    expectation = ScalpAuthorityExpectation(
        admission_mode=str(verification.admission_mode),
        account_mode=str(verification.account_mode),
        generation_id=str(verification.generation_id),
        strategy_id=str(verification.strategy_id),
        strategy_version=str(verification.strategy_version),
        engine_sha256=str(verification.engine_sha256),
        config_id=str(verification.config_id),
        config_sha256=str(verification.config_sha256),
        runtime_release_certificate_sha256=str(
            verification.runtime_release_certificate_sha256
        ),
        runtime_release_signing_key_id=str(
            verification.runtime_release_signing_key_id
        ),
        research_evidence_sha256=str(verification.evidence_sha256),
        research_evidence_signing_key_id=str(
            verification.evidence_signing_key_id
        ),
        registry_generation_id=str(verification.registry_generation_id),
        registry_revision=_safe_int(verification.registry_revision),
        registry_sha256=str(verification.registry_sha256),
        qualification_surface_sha256=str(
            verification.qualification_surface_sha256
        ),
        cost_mapping_sha256=str(verification.cost_mapping_sha256),
        execution_contract_sha256=str(verification.execution_contract_sha256),
        validation_expires_at_epoch=_safe_float(verification.expires_at_epoch),
        runtime_boot_id=str(runtime_boot_id),
        authority_revision=_safe_int(authority_revision),
        venue_id=str(verification.venue_id),
        scope_version=str(verification.scope_version),
        max_entries_per_symbol_utc_day=_safe_int(
            verification.max_entries_per_symbol_utc_day
        ),
        symbol_scope=_normalized_symbols(verification.symbol_scope),
    )
    error = expectation.validation_error()
    if error:
        raise ValueError(error)
    return expectation


def _legacy_v2_authority_identity_error(
    authority: dict[str, Any],
) -> str:
    """Validate v2 identity only for exact-owner protective CLOSE history.

    This compatibility path is used only for revoked authority and exact-owner
    protective CLOSE history. It cannot build or validate active authority.
    """

    current = dict(authority or {})
    admission_mode = str(current.get("admission_mode") or "").strip().lower()
    account_mode = str(current.get("account_mode") or "").strip().lower()
    if admission_mode not in {
        SCALP_ADMISSION_MODE_SIGNED,
        SCALP_ADMISSION_MODE_DIRECT_DEMO,
    }:
        return "scalp_authority_admission_mode_invalid"
    if admission_mode == SCALP_ADMISSION_MODE_DIRECT_DEMO and account_mode != "demo":
        return "scalp_authority_direct_demo_account_mode_invalid"
    if admission_mode == SCALP_ADMISSION_MODE_SIGNED and account_mode not in {
        "demo",
        "real",
    }:
        return "scalp_authority_account_mode_invalid"
    for field_name, reason in (
        ("generation_id", "scalp_authority_generation_missing"),
        ("strategy_id", "scalp_authority_strategy_missing"),
        ("runtime_boot_id", "scalp_authority_runtime_boot_missing"),
    ):
        if not str(current.get(field_name) or "").strip():
            return reason
    for field_name, reason in (
        ("engine_sha256", "scalp_authority_engine_identity_invalid"),
        ("config_sha256", "scalp_authority_config_identity_invalid"),
        (
            "validation_evidence_sha256",
            "scalp_authority_validation_evidence_invalid",
        ),
    ):
        if not _is_sha256(current.get(field_name)):
            return reason
    if _safe_float(current.get("validation_expires_at_epoch")) <= 0.0:
        return "scalp_authority_validation_expiry_invalid"
    if _safe_int(current.get("authority_revision")) <= 0:
        return "scalp_authority_revision_invalid"
    if str(current.get("venue_id") or "").strip().lower() != IG_MT4_VENUE_ID:
        return "scalp_authority_venue_invalid"
    if (
        str(current.get("bracket_policy") or "").strip().lower()
        != BROKER_NATIVE_BRACKET_POLICY
    ):
        return "scalp_authority_bracket_policy_invalid"
    if (
        _safe_int(current.get("max_entries_per_symbol_utc_day"))
        != MAX_ENTRIES_PER_SYMBOL_UTC_DAY
    ):
        return "scalp_authority_daily_frequency_invalid"
    return symbol_scope_error(current.get("symbol_scope") or ())


def build_active_authority(
    expectation: ScalpAuthorityExpectation,
    *,
    activated_at: float,
) -> dict[str, Any]:
    """Build a canonical active authority document for DB-owned CAS storage."""

    error = expectation.validation_error()
    if error:
        raise ValueError(error)
    activated = float(activated_at)
    if not math.isfinite(activated) or activated <= 0.0:
        raise ValueError("scalp_authority_activation_time_invalid")
    authority: dict[str, Any] = {
        "schema_version": SCALP_EXECUTION_AUTHORITY_SCHEMA,
        "status": "active",
        "source": "production_runtime",
        "admission_mode": str(expectation.admission_mode).strip().lower(),
        "account_mode": str(expectation.account_mode).strip().lower(),
        "generation_id": str(expectation.generation_id).strip(),
        "strategy_id": str(expectation.strategy_id).strip(),
        "strategy_version": str(expectation.strategy_version).strip(),
        "engine_sha256": str(expectation.engine_sha256).strip().lower(),
        "config_id": str(expectation.config_id).strip(),
        "config_sha256": str(expectation.config_sha256).strip().lower(),
        "runtime_release_certificate_sha256": str(
            expectation.runtime_release_certificate_sha256
        ).strip().lower(),
        "runtime_release_signing_key_id": str(
            expectation.runtime_release_signing_key_id
        ).strip().lower(),
        "research_evidence_sha256": str(
            expectation.research_evidence_sha256
        ).strip().lower(),
        "research_evidence_signing_key_id": str(
            expectation.research_evidence_signing_key_id
        ).strip().lower(),
        "registry_generation_id": str(
            expectation.registry_generation_id
        ).strip(),
        "registry_revision": _safe_int(expectation.registry_revision),
        "registry_sha256": str(expectation.registry_sha256).strip().lower(),
        "qualification_surface_sha256": str(
            expectation.qualification_surface_sha256
        ).strip().lower(),
        "cost_mapping_sha256": str(
            expectation.cost_mapping_sha256
        ).strip().lower(),
        "execution_contract_sha256": str(
            expectation.execution_contract_sha256
        ).strip().lower(),
        "validation_expires_at_epoch": float(
            expectation.validation_expires_at_epoch
        ),
        "venue_id": IG_MT4_VENUE_ID,
        "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "bracket_policy": BROKER_NATIVE_BRACKET_POLICY,
        "max_entries_per_symbol_utc_day": MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
        "runtime_boot_id": str(expectation.runtime_boot_id).strip(),
        "authority_revision": _safe_int(expectation.authority_revision),
        "activated_at": activated,
        "updated_at": activated,
    }
    authority["binding_sha256"] = authority_binding_sha256(authority)
    if float(expectation.validation_expires_at_epoch) <= activated:
        raise ValueError("scalp_authority_validation_expired")
    return authority


def validation_witness_error(
    witness: dict[str, Any] | None,
    *,
    authority: dict[str, Any],
    now_epoch: float | None = None,
) -> str:
    """Bind an admission result to the authority being activated.

    This is intentionally rechecked by both the service facade and the
    transactional store. New entry authority always requires signed production
    evidence with signature, revocation, and probability-surface checks.
    """

    current = dict(witness or {})
    if not current:
        return "scalp_validation_witness_missing"
    if str(authority.get("schema_version") or "") != (
        SCALP_EXECUTION_AUTHORITY_SCHEMA
    ):
        return "scalp_validation_witness_authority_schema_invalid"
    admission_mode = str(
        current.get("admission_mode") or SCALP_ADMISSION_MODE_SIGNED
    ).strip().lower()
    if admission_mode != SCALP_ADMISSION_MODE_SIGNED:
        return "scalp_validation_witness_mode_invalid"
    if (
        current.get("valid") is not True
        or str(current.get("reason") or "").strip()
        or list(current.get("errors") or [])
    ):
        return "scalp_validation_witness_invalid"
    if (
        current.get("authenticated") is not True
        or current.get("revocation_verified") is not True
    ):
        return "scalp_validation_witness_invalid"
    comparisons: tuple[tuple[Any, Any, str], ...] = (
        (
            admission_mode,
            authority.get("admission_mode"),
            "scalp_validation_witness_admission_mode_changed",
        ),
        (
            str(current.get("account_mode") or "").lower(),
            authority.get("account_mode"),
            "scalp_validation_witness_account_mode_changed",
        ),
        (
            current.get("generation_id"),
            authority.get("generation_id"),
            "scalp_validation_witness_generation_changed",
        ),
        (
            current.get("strategy_id"),
            authority.get("strategy_id"),
            "scalp_validation_witness_strategy_changed",
        ),
        (
            current.get("strategy_version"),
            authority.get("strategy_version"),
            "scalp_validation_witness_strategy_version_changed",
        ),
        (
            str(current.get("engine_sha256") or "").lower(),
            str(authority.get("engine_sha256") or "").lower(),
            "scalp_validation_witness_engine_changed",
        ),
        (
            current.get("config_id"),
            authority.get("config_id"),
            "scalp_validation_witness_config_id_changed",
        ),
        (
            str(current.get("config_sha256") or "").lower(),
            str(authority.get("config_sha256") or "").lower(),
            "scalp_validation_witness_config_changed",
        ),
        (
            str(current.get("runtime_release_certificate_sha256") or "").lower(),
            authority.get("runtime_release_certificate_sha256"),
            "scalp_validation_witness_runtime_release_certificate_changed",
        ),
        (
            str(current.get("runtime_release_signing_key_id") or "").lower(),
            authority.get("runtime_release_signing_key_id"),
            "scalp_validation_witness_runtime_release_key_changed",
        ),
        (
            str(current.get("evidence_sha256") or "").lower(),
            authority.get("research_evidence_sha256"),
            "scalp_validation_witness_research_evidence_changed",
        ),
        (
            str(current.get("evidence_signing_key_id") or "").lower(),
            authority.get("research_evidence_signing_key_id"),
            "scalp_validation_witness_research_evidence_key_changed",
        ),
        (
            current.get("registry_generation_id"),
            authority.get("registry_generation_id"),
            "scalp_validation_witness_registry_generation_changed",
        ),
        (
            str(current.get("registry_sha256") or "").lower(),
            authority.get("registry_sha256"),
            "scalp_validation_witness_registry_changed",
        ),
        (
            str(current.get("qualification_surface_sha256") or "").lower(),
            authority.get("qualification_surface_sha256"),
            "scalp_validation_witness_qualification_surface_changed",
        ),
        (
            str(current.get("cost_mapping_sha256") or "").lower(),
            authority.get("cost_mapping_sha256"),
            "scalp_validation_witness_cost_mapping_changed",
        ),
        (
            str(current.get("execution_contract_sha256") or "").lower(),
            authority.get("execution_contract_sha256"),
            "scalp_validation_witness_execution_contract_changed",
        ),
        (
            str(current.get("venue_id") or "").lower(),
            str(authority.get("venue_id") or "").lower(),
            "scalp_validation_witness_venue_changed",
        ),
        (
            current.get("scope_version"),
            authority.get("scope_version"),
            "scalp_validation_witness_scope_version_changed",
        ),
    )
    for observed, expected, reason in comparisons:
        if str(observed or "").strip() != str(expected or "").strip():
            return reason
    if (
        str(current.get("certificate_sha256") or "").lower()
        != str(current.get("runtime_release_certificate_sha256") or "").lower()
    ):
        return "scalp_validation_witness_runtime_release_certificate_alias_changed"
    if (
        str(current.get("signing_key_id") or "").lower()
        != str(current.get("runtime_release_signing_key_id") or "").lower()
    ):
        return "scalp_validation_witness_runtime_release_key_alias_changed"
    for field_name in (
        "runtime_release_certificate_sha256",
        "runtime_release_signing_key_id",
        "evidence_sha256",
        "evidence_signing_key_id",
        "registry_sha256",
        "qualification_surface_sha256",
        "cost_mapping_sha256",
        "execution_contract_sha256",
    ):
        if not _is_sha256(current.get(field_name)):
            return f"scalp_validation_witness_{field_name}_invalid"
    if _safe_int(current.get("registry_revision")) != _safe_int(
        authority.get("registry_revision")
    ):
        return "scalp_validation_witness_registry_revision_changed"
    if _safe_int(current.get("registry_revision")) <= 0:
        return "scalp_validation_witness_registry_revision_invalid"
    if _normalized_symbols(current.get("symbol_scope") or []) != tuple(
        IG_MT4_SCALP_SYMBOLS
    ):
        return "scalp_validation_witness_symbol_scope_changed"
    if _safe_int(current.get("max_entries_per_symbol_utc_day")) != int(
        authority.get("max_entries_per_symbol_utc_day") or 0
    ):
        return "scalp_validation_witness_daily_frequency_changed"
    witness_expiry = _safe_float(current.get("expires_at_epoch"))
    authority_expiry = _safe_float(authority.get("validation_expires_at_epoch"))
    if witness_expiry <= 0.0 or witness_expiry != authority_expiry:
        return "scalp_validation_witness_expiry_changed"
    now = _safe_float(time.time() if now_epoch is None else now_epoch)
    if now <= 0.0 or now >= witness_expiry:
        return "scalp_validation_witness_expired"
    raw_bounds = current.get("win_probability_lower_bounds")
    if not isinstance(raw_bounds, dict) or set(raw_bounds) != set(
        IG_MT4_SCALP_SYMBOLS
    ):
        return "scalp_validation_witness_probability_scope_invalid"
    for symbol in IG_MT4_SCALP_SYMBOLS:
        sides = raw_bounds.get(symbol)
        if not isinstance(sides, dict) or set(sides) != {"BUY", "SELL"}:
            return "scalp_validation_witness_probability_scope_invalid"
        for side in ("BUY", "SELL"):
            lower = _safe_float(sides.get(side))
            if not 0.0 < lower <= 1.0:
                return "scalp_validation_witness_probability_invalid"
    return ""


def authority_error(
    authority: dict[str, Any] | None,
    *,
    expectation: ScalpAuthorityExpectation,
    now_epoch: float | None = None,
) -> str:
    """Validate durable authority against the currently executing runtime."""

    current = dict(authority or {})
    if not current:
        return "scalp_authority_missing"
    if str(current.get("schema_version") or "") != SCALP_EXECUTION_AUTHORITY_SCHEMA:
        return "scalp_authority_schema_invalid"
    expected_error = expectation.validation_error()
    if expected_error:
        return expected_error
    if str(current.get("status") or "").strip().lower() != "active":
        return "scalp_authority_inactive"
    if str(current.get("source") or "").strip().lower() != "production_runtime":
        return "scalp_authority_source_invalid"
    comparisons: tuple[tuple[str, Any, Any, str], ...] = (
        (
            "admission_mode",
            current.get("admission_mode"),
            expectation.admission_mode,
            "scalp_authority_admission_mode_changed",
        ),
        (
            "account_mode",
            current.get("account_mode"),
            expectation.account_mode,
            "scalp_authority_account_mode_changed",
        ),
        (
            "generation_id",
            current.get("generation_id"),
            expectation.generation_id,
            "scalp_authority_generation_changed",
        ),
        (
            "strategy_id",
            current.get("strategy_id"),
            expectation.strategy_id,
            "scalp_authority_strategy_changed",
        ),
        (
            "strategy_version",
            current.get("strategy_version"),
            expectation.strategy_version,
            "scalp_authority_strategy_version_changed",
        ),
        (
            "engine_sha256",
            str(current.get("engine_sha256") or "").lower(),
            str(expectation.engine_sha256 or "").lower(),
            "scalp_authority_engine_changed",
        ),
        (
            "config_id",
            current.get("config_id"),
            expectation.config_id,
            "scalp_authority_config_id_changed",
        ),
        (
            "config_sha256",
            str(current.get("config_sha256") or "").lower(),
            str(expectation.config_sha256 or "").lower(),
            "scalp_authority_config_changed",
        ),
        (
            "runtime_release_certificate_sha256",
            str(current.get("runtime_release_certificate_sha256") or "").lower(),
            str(expectation.runtime_release_certificate_sha256 or "").lower(),
            "scalp_authority_runtime_release_certificate_changed",
        ),
        (
            "runtime_release_signing_key_id",
            str(current.get("runtime_release_signing_key_id") or "").lower(),
            str(expectation.runtime_release_signing_key_id or "").lower(),
            "scalp_authority_runtime_release_key_changed",
        ),
        (
            "research_evidence_sha256",
            str(current.get("research_evidence_sha256") or "").lower(),
            str(expectation.research_evidence_sha256 or "").lower(),
            "scalp_authority_research_evidence_changed",
        ),
        (
            "research_evidence_signing_key_id",
            str(
                current.get("research_evidence_signing_key_id") or ""
            ).lower(),
            str(expectation.research_evidence_signing_key_id or "").lower(),
            "scalp_authority_research_evidence_key_changed",
        ),
        (
            "registry_generation_id",
            current.get("registry_generation_id"),
            expectation.registry_generation_id,
            "scalp_authority_registry_generation_changed",
        ),
        (
            "registry_sha256",
            str(current.get("registry_sha256") or "").lower(),
            str(expectation.registry_sha256 or "").lower(),
            "scalp_authority_registry_changed",
        ),
        (
            "qualification_surface_sha256",
            str(current.get("qualification_surface_sha256") or "").lower(),
            str(expectation.qualification_surface_sha256 or "").lower(),
            "scalp_authority_qualification_surface_changed",
        ),
        (
            "cost_mapping_sha256",
            str(current.get("cost_mapping_sha256") or "").lower(),
            str(expectation.cost_mapping_sha256 or "").lower(),
            "scalp_authority_cost_mapping_changed",
        ),
        (
            "execution_contract_sha256",
            str(current.get("execution_contract_sha256") or "").lower(),
            str(expectation.execution_contract_sha256 or "").lower(),
            "scalp_authority_execution_contract_changed",
        ),
        (
            "scope_version",
            current.get("scope_version"),
            expectation.scope_version,
            "scalp_authority_scope_version_changed",
        ),
        (
            "runtime_boot_id",
            current.get("runtime_boot_id"),
            expectation.runtime_boot_id,
            "scalp_authority_runtime_boot_changed",
        ),
    )
    for _, observed, expected, reason in comparisons:
        if str(observed or "").strip() != str(expected or "").strip():
            return reason
    if _safe_int(current.get("authority_revision")) != _safe_int(
        expectation.authority_revision
    ):
        return "scalp_authority_revision_changed"
    if _safe_int(current.get("registry_revision")) != _safe_int(
        expectation.registry_revision
    ):
        return "scalp_authority_registry_revision_changed"
    try:
        current_expiry = float(current.get("validation_expires_at_epoch"))
        expected_expiry = float(expectation.validation_expires_at_epoch)
        now = float(time.time() if now_epoch is None else now_epoch)
    except (TypeError, ValueError, OverflowError):
        return "scalp_authority_validation_expiry_invalid"
    if (
        not math.isfinite(current_expiry)
        or not math.isfinite(expected_expiry)
        or current_expiry != expected_expiry
    ):
        return "scalp_authority_validation_expiry_changed"
    if not math.isfinite(now) or now <= 0.0 or now >= current_expiry:
        return "scalp_authority_validation_expired"
    if str(current.get("venue_id") or "").strip().lower() != IG_MT4_VENUE_ID:
        return "scalp_authority_venue_changed"
    if (
        str(current.get("bracket_policy") or "").strip().lower()
        != BROKER_NATIVE_BRACKET_POLICY
    ):
        return "scalp_authority_bracket_policy_changed"
    if (
        _safe_int(current.get("max_entries_per_symbol_utc_day"))
        != MAX_ENTRIES_PER_SYMBOL_UTC_DAY
    ):
        return "scalp_authority_daily_frequency_changed"
    scope_error = symbol_scope_error(current.get("symbol_scope") or [])
    if scope_error:
        return scope_error
    binding = str(current.get("binding_sha256") or "").strip().lower()
    if not _is_sha256(binding) or binding != authority_binding_sha256(current):
        return "scalp_authority_binding_invalid"
    return ""


def protective_authority_structure_error(
    authority: dict[str, Any] | None,
) -> str:
    """Validate a revoked prior authority for protective management only.

    Expiry and the prior boot/revision deliberately remain part of the hashed
    identity but are not treated as entry leases.  This contract can identify
    positions opened by the prior generation; it can never reactivate it.
    """

    current = dict(authority or {})
    if not current:
        return "scalp_protective_authority_missing"
    schema = str(current.get("schema_version") or "")
    if schema not in {
        SCALP_EXECUTION_AUTHORITY_SCHEMA,
        SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA,
    }:
        return "scalp_protective_authority_schema_invalid"
    if str(current.get("status") or "").strip().lower() != "revoked":
        return "scalp_protective_authority_not_revoked"
    if str(current.get("source") or "").strip().lower() != "production_runtime":
        return "scalp_protective_authority_source_invalid"
    if schema == SCALP_EXECUTION_AUTHORITY_SCHEMA:
        expectation_error = expectation_from_authority(current).validation_error()
    else:
        expectation_error = _legacy_v2_authority_identity_error(current)
    if expectation_error:
        return str(expectation_error)
    binding = str(current.get("binding_sha256") or "").strip().lower()
    if not _is_sha256(binding) or binding != authority_binding_sha256(current):
        return "scalp_protective_authority_binding_invalid"
    return ""


def protective_history_binding_error(
    entry_payload: dict[str, Any] | None,
    *,
    close_payload: dict[str, Any] | None,
    symbol: str,
) -> str:
    """Bind a protective CLOSE to one durably admitted historical entry."""

    entry = dict(entry_payload or {})
    close = dict(close_payload or {})
    normalized_symbol = str(symbol or entry.get("symbol") or "").strip().upper()
    if normalized_symbol not in IG_MT4_SCALP_CATALOG:
        return "scalp_protective_history_symbol_invalid"
    if str(entry.get("strategy_lane") or "").strip().lower() != SCALP_EXECUTION_LANE:
        return "scalp_protective_history_lane_invalid"
    if str(entry.get("intent") or "").strip().lower() != SCALP_ENTRY_INTENT:
        return "scalp_protective_history_intent_invalid"

    historical_authority, binding_fields, history_error = (
        _historical_authority_from_entry_payload(entry)
    )
    if history_error:
        return history_error
    if (
        str(close.get("management_strategy") or "").strip()
        != str(historical_authority.get("strategy_id") or "").strip()
    ):
        return "scalp_protective_command_strategy_changed"
    for command_field, _authority_field in binding_fields:
        observed = str(close.get(command_field) or "").strip().lower()
        expected = str(entry.get(command_field) or "").strip().lower()
        if not observed:
            return f"{command_field}_missing"
        if observed != expected:
            return f"{command_field}_changed"
    return ""


def _historical_authority_from_entry_payload(
    entry_payload: dict[str, Any] | None,
) -> tuple[dict[str, Any], tuple[tuple[str, str], ...], str]:
    """Parse v3 or legacy-v2 authority solely for protective ownership."""

    entry = dict(entry_payload or {})
    schema = str(entry.get("expected_strategy_authority_schema") or "")
    common: dict[str, Any] = {
        "schema_version": schema,
        "status": "revoked",
        "source": "production_runtime",
        "admission_mode": entry.get("expected_strategy_admission_mode"),
        "account_mode": entry.get("expected_strategy_account_mode"),
        "generation_id": entry.get("expected_strategy_generation_id"),
        "strategy_id": entry.get("expected_strategy_id"),
        "engine_sha256": entry.get("expected_strategy_engine_sha256"),
        "config_sha256": entry.get("expected_strategy_config_sha256"),
        "validation_expires_at_epoch": entry.get(
            "expected_strategy_validation_expires_at_epoch"
        ),
        "venue_id": entry.get("expected_strategy_venue_id"),
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "bracket_policy": BROKER_NATIVE_BRACKET_POLICY,
        "max_entries_per_symbol_utc_day": MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
        "runtime_boot_id": entry.get("expected_strategy_runtime_boot_id"),
        "authority_revision": entry.get(
            "expected_strategy_authority_revision"
        ),
    }
    if schema == SCALP_EXECUTION_AUTHORITY_SCHEMA:
        historical_authority = {
            **common,
            "strategy_version": entry.get("expected_strategy_version"),
            "config_id": entry.get("expected_strategy_config_id"),
            "runtime_release_certificate_sha256": entry.get(
                "expected_strategy_runtime_release_certificate_sha256"
            ),
            "runtime_release_signing_key_id": entry.get(
                "expected_strategy_runtime_release_signing_key_id"
            ),
            "research_evidence_sha256": entry.get(
                "expected_strategy_research_evidence_sha256"
            ),
            "research_evidence_signing_key_id": entry.get(
                "expected_strategy_research_evidence_signing_key_id"
            ),
            "registry_generation_id": entry.get(
                "expected_strategy_registry_generation_id"
            ),
            "registry_revision": entry.get("expected_strategy_registry_revision"),
            "registry_sha256": entry.get("expected_strategy_registry_sha256"),
            "qualification_surface_sha256": entry.get(
                "expected_strategy_qualification_surface_sha256"
            ),
            "cost_mapping_sha256": entry.get(
                "expected_strategy_cost_mapping_sha256"
            ),
            "execution_contract_sha256": entry.get(
                "expected_strategy_execution_contract_sha256"
            ),
            "scope_version": entry.get("expected_strategy_scope_version"),
        }
        binding_fields = _COMMAND_BINDING_FIELDS_V3
        identity_error = expectation_from_authority(
            historical_authority
        ).validation_error()
    elif schema == SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA:
        historical_authority = {
            **common,
            "validation_evidence_sha256": entry.get(
                "expected_strategy_validation_evidence_sha256"
            ),
        }
        binding_fields = _COMMAND_BINDING_FIELDS_V2
        identity_error = _legacy_v2_authority_identity_error(
            historical_authority
        )
    else:
        return {}, (), "scalp_protective_history_schema_invalid"
    if identity_error:
        return {}, (), str(identity_error)
    historical_binding = str(
        entry.get("expected_strategy_binding_sha256") or ""
    ).strip().lower()
    historical_authority["binding_sha256"] = historical_binding
    if (
        not _is_sha256(historical_binding)
        or historical_binding
        != authority_binding_sha256(historical_authority)
    ):
        return {}, (), "scalp_protective_history_binding_invalid"
    return historical_authority, binding_fields, ""


def protective_command_binding_fields(
    entry_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return already-stamped history fields after protective-only parsing."""

    entry = dict(entry_payload or {})
    historical, binding_fields, error = _historical_authority_from_entry_payload(
        entry
    )
    if error:
        raise ValueError(error)
    del historical
    if str(entry.get("strategy_lane") or "").strip().lower() != (
        SCALP_EXECUTION_LANE
    ):
        raise ValueError("scalp_protective_history_lane_invalid")
    if str(entry.get("intent") or "").strip().lower() != SCALP_ENTRY_INTENT:
        raise ValueError("scalp_protective_history_intent_invalid")
    return {
        command_field: entry.get(command_field)
        for command_field, _authority_field in binding_fields
    }


def protective_authority_from_entry_payload(
    entry_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Recover a revoked historical authority for protective use only."""

    entry = dict(entry_payload or {})
    if str(entry.get("strategy_lane") or "").strip().lower() != (
        SCALP_EXECUTION_LANE
    ):
        raise ValueError("scalp_protective_history_lane_invalid")
    if str(entry.get("intent") or "").strip().lower() != SCALP_ENTRY_INTENT:
        raise ValueError("scalp_protective_history_intent_invalid")
    historical, _binding_fields, error = _historical_authority_from_entry_payload(
        entry
    )
    if error:
        raise ValueError(error)
    return historical


def expectation_from_authority(
    authority: dict[str, Any] | None,
) -> ScalpAuthorityExpectation:
    """Project the immutable expectation from a durable authority document."""

    current = dict(authority or {})
    return ScalpAuthorityExpectation(
        admission_mode=str(current.get("admission_mode") or ""),
        account_mode=str(current.get("account_mode") or ""),
        generation_id=str(current.get("generation_id") or ""),
        strategy_id=str(current.get("strategy_id") or ""),
        strategy_version=str(current.get("strategy_version") or ""),
        engine_sha256=str(current.get("engine_sha256") or ""),
        config_id=str(current.get("config_id") or ""),
        config_sha256=str(current.get("config_sha256") or ""),
        runtime_release_certificate_sha256=str(
            current.get("runtime_release_certificate_sha256") or ""
        ),
        runtime_release_signing_key_id=str(
            current.get("runtime_release_signing_key_id") or ""
        ),
        research_evidence_sha256=str(
            current.get("research_evidence_sha256") or ""
        ),
        research_evidence_signing_key_id=str(
            current.get("research_evidence_signing_key_id") or ""
        ),
        registry_generation_id=str(
            current.get("registry_generation_id") or ""
        ),
        registry_revision=_safe_int(current.get("registry_revision")),
        registry_sha256=str(current.get("registry_sha256") or ""),
        qualification_surface_sha256=str(
            current.get("qualification_surface_sha256") or ""
        ),
        cost_mapping_sha256=str(current.get("cost_mapping_sha256") or ""),
        execution_contract_sha256=str(
            current.get("execution_contract_sha256") or ""
        ),
        validation_expires_at_epoch=_safe_float(
            current.get("validation_expires_at_epoch")
        ),
        runtime_boot_id=str(current.get("runtime_boot_id") or ""),
        authority_revision=_safe_int(current.get("authority_revision")),
        venue_id=str(current.get("venue_id") or ""),
        scope_version=str(current.get("scope_version") or ""),
        bracket_policy=str(current.get("bracket_policy") or ""),
        max_entries_per_symbol_utc_day=_safe_int(
            current.get("max_entries_per_symbol_utc_day")
        ),
        symbol_scope=_normalized_symbols(current.get("symbol_scope") or []),
    )


def expectation_from_command(
    payload: dict[str, Any] | None,
) -> ScalpAuthorityExpectation:
    """Recover the enqueue-time identity stamped in a durable command."""

    raw = dict(payload or {})
    return ScalpAuthorityExpectation(
        admission_mode=str(raw.get("expected_strategy_admission_mode") or ""),
        account_mode=str(raw.get("expected_strategy_account_mode") or ""),
        generation_id=str(raw.get("expected_strategy_generation_id") or ""),
        strategy_id=str(raw.get("expected_strategy_id") or ""),
        strategy_version=str(raw.get("expected_strategy_version") or ""),
        engine_sha256=str(raw.get("expected_strategy_engine_sha256") or ""),
        config_id=str(raw.get("expected_strategy_config_id") or ""),
        config_sha256=str(raw.get("expected_strategy_config_sha256") or ""),
        runtime_release_certificate_sha256=str(
            raw.get("expected_strategy_runtime_release_certificate_sha256") or ""
        ),
        runtime_release_signing_key_id=str(
            raw.get("expected_strategy_runtime_release_signing_key_id") or ""
        ),
        research_evidence_sha256=str(
            raw.get("expected_strategy_research_evidence_sha256") or ""
        ),
        research_evidence_signing_key_id=str(
            raw.get("expected_strategy_research_evidence_signing_key_id") or ""
        ),
        registry_generation_id=str(
            raw.get("expected_strategy_registry_generation_id") or ""
        ),
        registry_revision=_safe_int(
            raw.get("expected_strategy_registry_revision")
        ),
        registry_sha256=str(
            raw.get("expected_strategy_registry_sha256") or ""
        ),
        qualification_surface_sha256=str(
            raw.get("expected_strategy_qualification_surface_sha256") or ""
        ),
        cost_mapping_sha256=str(
            raw.get("expected_strategy_cost_mapping_sha256") or ""
        ),
        execution_contract_sha256=str(
            raw.get("expected_strategy_execution_contract_sha256") or ""
        ),
        validation_expires_at_epoch=_safe_float(
            raw.get("expected_strategy_validation_expires_at_epoch")
        ),
        runtime_boot_id=str(raw.get("expected_strategy_runtime_boot_id") or ""),
        authority_revision=_safe_int(
            raw.get("expected_strategy_authority_revision")
        ),
        venue_id=str(raw.get("expected_strategy_venue_id") or ""),
        scope_version=str(raw.get("expected_strategy_scope_version") or ""),
        symbol_scope=IG_MT4_SCALP_SYMBOLS,
    )


def command_binding_fields(authority: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable authority identity stamped into one command."""

    if str(authority.get("schema_version") or "") != (
        SCALP_EXECUTION_AUTHORITY_SCHEMA
    ):
        raise ValueError("scalp_command_authority_schema_invalid")
    binding = str(authority.get("binding_sha256") or "").strip().lower()
    if not _is_sha256(binding) or binding != authority_binding_sha256(authority):
        raise ValueError("scalp_command_authority_binding_invalid")
    return {
        command_field: authority.get(authority_field)
        for command_field, authority_field in _COMMAND_BINDING_FIELDS_V3
    } | {
        "strategy_lane": SCALP_EXECUTION_LANE,
        "intent": SCALP_ENTRY_INTENT,
    }


def command_binding_error(
    payload: dict[str, Any] | None,
    *,
    authority: dict[str, Any],
    symbol: str,
) -> str:
    """Recheck one queued command against the currently active authority."""

    raw = dict(payload or {})
    normalized_symbol = str(symbol or raw.get("symbol") or "").strip().upper()
    if str(authority.get("schema_version") or "") != (
        SCALP_EXECUTION_AUTHORITY_SCHEMA
    ):
        return "scalp_command_authority_schema_invalid"
    if str(raw.get("expected_strategy_authority_schema") or "") != (
        SCALP_EXECUTION_AUTHORITY_SCHEMA
    ):
        return "expected_strategy_authority_schema_changed"
    authority_binding = str(authority.get("binding_sha256") or "").strip().lower()
    if (
        not _is_sha256(authority_binding)
        or authority_binding != authority_binding_sha256(authority)
    ):
        return "scalp_command_authority_binding_invalid"
    if str(raw.get("strategy_lane") or "").strip().lower() != SCALP_EXECUTION_LANE:
        return "scalp_command_lane_invalid"
    if str(raw.get("intent") or "").strip().lower() != SCALP_ENTRY_INTENT:
        return "scalp_command_intent_invalid"
    if normalized_symbol not in IG_MT4_SCALP_CATALOG:
        return "scalp_command_symbol_invalid"
    if (
        str(authority.get("admission_mode") or "").strip().lower()
        != SCALP_ADMISSION_MODE_SIGNED
        or str(raw.get("expected_strategy_admission_mode") or "")
        .strip()
        .lower()
        != SCALP_ADMISSION_MODE_SIGNED
    ):
        return "scalp_command_admission_mode_invalid"
    authority_account_mode = str(
        authority.get("account_mode") or ""
    ).strip().lower()
    command_account_mode = str(
        raw.get("expected_strategy_account_mode") or ""
    ).strip().lower()
    if (
        authority_account_mode not in {"demo", "real"}
        or command_account_mode != authority_account_mode
    ):
        return "scalp_command_account_mode_invalid"
    for command_field, authority_field in _COMMAND_BINDING_FIELDS_V3:
        observed = str(raw.get(command_field) or "").strip().lower()
        expected = str(authority.get(authority_field) or "").strip().lower()
        if not observed:
            return f"{command_field}_missing"
        if observed != expected:
            return f"{command_field}_changed"
    return ""


def is_production_scalper_entry(*, intent: Any, payload: Any) -> bool:
    raw = dict(payload) if isinstance(payload, dict) else {}
    return bool(
        str(intent or raw.get("intent") or "").strip().lower()
        == SCALP_ENTRY_INTENT
        and str(raw.get("strategy_lane") or "").strip().lower()
        == SCALP_EXECUTION_LANE
    )
