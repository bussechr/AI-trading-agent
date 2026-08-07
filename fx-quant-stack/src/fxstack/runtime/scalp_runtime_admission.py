"""Public-only startup and cycle admission for the MTVCLC IG-DEMO runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from typing import Any

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_VENUE_ID,
)
from fxstack.runtime.mtvclc_runtime_release import (
    MTVCLC_RUNTIME_RELEASE_BUNDLE_SCHEMA,
    MTVCLCRuntimeReleaseExpectation,
    MTVCLCRuntimeReleaseRegistryAnchor,
    MTVCLCRuntimeReleaseVerification,
    verify_mtvclc_runtime_release,
)
from fxstack.runtime.scalp_engine_identity import (
    ProductionScalpEngineIdentity,
    production_scalp_engine_identity,
)
from fxstack.runtime.scalp_cost_snapshot import load_runtime_cost_snapshot
from fxstack.strategy.mtvclc import (
    FROZEN_MTVCLC_POLICY,
    MTVCLC_CONFIG_SHA256,
    MTVCLCCostCalibration,
    MTVCLC_CONFIG_ID,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
    MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
)


# AGENT: ROLE: stable public-file loader + pure MTVCLC runtime-release adapter.
# AGENT: ISOLATION: no issuer, private key, persistence, activation, or broker I/O.

SCALP_VALIDATION_BUNDLE_SCHEMA = MTVCLC_RUNTIME_RELEASE_BUNDLE_SCHEMA
MAX_SCALP_VALIDATION_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_SCALP_PUBLIC_KEY_BYTES = 64 * 1024
SCALP_ADMISSION_MODE_SIGNED = "signed_validation"
SCALP_RUNTIME_NATIVE_GENERATION_ID = "mtvclc-runtime-native-v1"
SCALP_RUNTIME_NATIVE_AUTHORITY_PURPOSE = "mtvclc_runtime_native_eligibility.v1"
SCALP_RUNTIME_NATIVE_EXPIRES_AT_EPOCH = 4_102_444_800.0
SCALP_RUNTIME_NATIVE_EDGE_PROBABILITY_RESERVE = 0.02
SCALP_RUNTIME_ADMISSION_DIAGNOSTIC_SCHEMA = (
    "fxstack.runtime.scalp_admission_diagnostic.v1"
)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ScalpRuntimeAdmission:
    valid: bool
    reason: str
    errors: tuple[str, ...]
    verification: MTVCLCRuntimeReleaseVerification
    engine_identity: ProductionScalpEngineIdentity
    cost_calibrations: tuple[MTVCLCCostCalibration, ...] = ()
    bundle_file_sha256: str = ""
    evidence_public_key_file_sha256: str = ""
    release_public_key_file_sha256: str = ""
    bundle_path: str = ""
    evidence_public_key_path: str = ""
    release_public_key_path: str = ""

    @property
    def public_key_file_sha256(self) -> str:
        """Compatibility alias for consumers that formerly had one key."""

        return self.release_public_key_file_sha256

    @property
    def public_key_path(self) -> str:
        """Compatibility alias for consumers that formerly had one key."""

        return self.release_public_key_path

    def cost_calibration_for(self, symbol: Any) -> MTVCLCCostCalibration | None:
        pair = str(symbol or "").strip().upper()
        return next((row for row in self.cost_calibrations if row.symbol == pair), None)

    def to_dict(
        self,
        *,
        include_win_probability_bounds: bool = False,
        include_cost_calibrations: bool = False,
    ) -> dict[str, Any]:
        verification = self.verification.to_dict(
            include_qualification_surfaces=include_win_probability_bounds,
            include_cost_calibrations=include_cost_calibrations,
        )
        if not include_win_probability_bounds:
            verification["win_probability_lower_bounds"] = {}
            verification["base_break_even_probabilities"] = {}
            verification["evidence_cell_sha256"] = {}
            verification["qualification_surface"] = {}
        if not include_cost_calibrations:
            verification["cost_calibrations"] = {}
        return {
            "valid": bool(self.valid),
            "reason": str(self.reason),
            "errors": list(self.errors),
            "verification": verification,
            "engine_identity": self.engine_identity.to_dict(),
            "bundle_file_sha256": self.bundle_file_sha256,
            "evidence_public_key_file_sha256": (self.evidence_public_key_file_sha256),
            "release_public_key_file_sha256": self.release_public_key_file_sha256,
            "bundle_path": self.bundle_path,
            "evidence_public_key_path": self.evidence_public_key_path,
            "release_public_key_path": self.release_public_key_path,
        }

    def to_cycle_diagnostics(self) -> dict[str, Any]:
        """Project the changing admission status without static release bulk."""

        verification = self.verification
        return {
            "schema_version": SCALP_RUNTIME_ADMISSION_DIAGNOSTIC_SCHEMA,
            "valid": bool(self.valid),
            "reason": str(self.reason),
            "errors": list(self.errors),
            "verification": {
                "valid": bool(verification.valid),
                "reason": str(verification.reason),
                "errors": list(verification.errors),
                "authenticated": bool(verification.authenticated),
                "revocation_verified": bool(verification.revocation_verified),
                "admission_mode": str(verification.admission_mode),
                "generation_id": str(verification.generation_id),
                "runtime_release_certificate_sha256": str(
                    verification.runtime_release_certificate_sha256
                ),
                "runtime_release_signing_key_id": str(
                    verification.runtime_release_signing_key_id
                ),
                "evidence_sha256": str(verification.evidence_sha256),
                "evidence_signing_key_id": str(verification.evidence_signing_key_id),
                "registry_generation_id": str(verification.registry_generation_id),
                "registry_revision": int(verification.registry_revision),
                "registry_sha256": str(verification.registry_sha256),
                "engine_sha256": str(verification.engine_sha256),
                "config_sha256": str(verification.config_sha256),
                "venue_id": str(verification.venue_id),
                "account_mode": str(verification.account_mode),
                "scope_version": str(verification.scope_version),
                "symbol_scope": list(verification.symbol_scope),
                "expires_at_epoch": float(verification.expires_at_epoch),
                "authority_purpose": str(verification.authority_purpose),
                "qualification_surface_sha256": str(
                    verification.qualification_surface_sha256
                ),
                "cost_mapping_sha256": str(verification.cost_mapping_sha256),
                "cost_rows_sha256": str(verification.cost_rows_sha256),
                "execution_contract_sha256": str(
                    verification.execution_contract_sha256
                ),
            },
            "engine_identity": {
                "engine_sha256": str(self.engine_identity.engine_sha256),
                "component_count": len(self.engine_identity.component_sha256),
            },
            "bundle_file_sha256": str(self.bundle_file_sha256),
            "evidence_public_key_file_sha256": str(
                self.evidence_public_key_file_sha256
            ),
            "release_public_key_file_sha256": str(self.release_public_key_file_sha256),
        }


def _invalid_verification(
    *,
    reason: str,
    expectation: MTVCLCRuntimeReleaseExpectation,
) -> MTVCLCRuntimeReleaseVerification:
    return MTVCLCRuntimeReleaseVerification(
        valid=False,
        reason=reason,
        errors=(reason,),
        admission_mode=SCALP_ADMISSION_MODE_SIGNED,
        generation_id=expectation.generation_id,
        strategy_id=expectation.strategy_id,
        strategy_version=expectation.strategy_version,
        engine_sha256=expectation.engine_sha256,
        engine_component_sha256=expectation.engine_component_sha256,
        config_id=expectation.config_id,
        config_sha256=expectation.config_sha256,
    )


def _resolve_path(settings: Any, raw: Any) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        base = Path(getattr(settings, "project_root", Path.cwd())).expanduser()
        path = base / path
    return path.resolve(strict=False)


def _read_bounded(path: Path, *, limit: int, reason_prefix: str) -> bytes:
    """Read one stable regular file without following a final symlink."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{reason_prefix}_unreadable") from exc
    if stat.S_ISLNK(before.st_mode):
        raise RuntimeError(f"{reason_prefix}_symlink_forbidden")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > limit
    ):
        raise RuntimeError(f"{reason_prefix}_size_invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"{reason_prefix}_unreadable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size <= 0
            or opened.st_size > limit
            or (before.st_ino and opened.st_ino and before.st_ino != opened.st_ino)
            or (before.st_dev and opened.st_dev and before.st_dev != opened.st_dev)
        ):
            raise RuntimeError(f"{reason_prefix}_changed_during_read")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not payload or len(payload) > limit:
        raise RuntimeError(f"{reason_prefix}_size_invalid")
    if (
        len(payload) != opened.st_size
        or opened.st_size != after.st_size
        or getattr(opened, "st_mtime_ns", 0) != getattr(after, "st_mtime_ns", 0)
    ):
        raise RuntimeError(f"{reason_prefix}_changed_during_read")
    return payload


def _load_public_key(payload: bytes, *, reason_prefix: str) -> Any:
    try:
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError(f"{reason_prefix}_backend_unavailable") from exc

    candidates: list[Any] = []
    for loader in (
        serialization.load_pem_public_key,
        serialization.load_ssh_public_key,
    ):
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
    raise RuntimeError(f"{reason_prefix}_invalid")


def _expectation(
    settings: Any,
    *,
    engine_identity: ProductionScalpEngineIdentity,
) -> MTVCLCRuntimeReleaseExpectation:
    return MTVCLCRuntimeReleaseExpectation(
        generation_id=str(
            getattr(settings, "production_scalp_generation_id", "") or ""
        ).strip(),
        config_sha256=MTVCLC_CONFIG_SHA256,
        engine_sha256=engine_identity.engine_sha256,
        engine_component_sha256=engine_identity.component_sha256,
    )


def _typed_costs(
    verification: MTVCLCRuntimeReleaseVerification,
) -> tuple[MTVCLCCostCalibration, ...]:
    if set(verification.cost_calibrations) != set(IG_MT4_SCALP_SYMBOLS):
        raise RuntimeError("mtvclc_runtime_release_cost_scope_invalid")
    rows: list[MTVCLCCostCalibration] = []
    for symbol in IG_MT4_SCALP_SYMBOLS:
        raw = verification.cost_calibrations.get(symbol)
        if not isinstance(raw, Mapping):
            raise RuntimeError("mtvclc_runtime_release_cost_row_invalid")
        try:
            row = MTVCLCCostCalibration(**dict(raw))
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("mtvclc_runtime_release_cost_row_invalid") from exc
        if (
            row.symbol != symbol
            or row.source_sha256
            != verification.cost_calibration_source_sha256_by_symbol.get(symbol)
            or row.row_sha256() != verification.cost_calibration_row_sha256.get(symbol)
        ):
            raise RuntimeError("mtvclc_runtime_release_cost_row_binding_invalid")
        rows.append(row)
    return tuple(rows)


@lru_cache(maxsize=8)
def _build_runtime_native_admission(
    *,
    engine_identity: ProductionScalpEngineIdentity,
    costs: tuple[MTVCLCCostCalibration, ...],
    expected_mode: str,
    capture_file_sha256: str,
    calibration_id: str,
) -> ScalpRuntimeAdmission:
    """Build immutable-by-contract surfaces for one exact content identity."""

    cost_row_sha256 = {cost.symbol: cost.row_sha256() for cost in costs}
    cost_rows = {
        cost.symbol: {
            **asdict(cost),
            "evidence_cost_row_sha256": cost_row_sha256[cost.symbol],
            "runtime_calibration_row_sha256": cost_row_sha256[cost.symbol],
        }
        for cost in costs
    }
    source_sha256_by_symbol = {cost.symbol: cost.source_sha256 for cost in costs}
    base_break_even = {
        cost.symbol: {
            side: float(cost.break_even_win_probability) for side in ("BUY", "SELL")
        }
        for cost in costs
    }
    lower_bounds = {
        cost.symbol: {
            side: min(
                0.999999,
                float(cost.break_even_win_probability)
                + SCALP_RUNTIME_NATIVE_EDGE_PROBABILITY_RESERVE,
            )
            for side in ("BUY", "SELL")
        }
        for cost in costs
    }
    cell_hashes = {
        symbol: {
            side: _canonical_sha256(
                {
                    "generation_id": SCALP_RUNTIME_NATIVE_GENERATION_ID,
                    "symbol": symbol,
                    "side": side,
                    "break_even_probability": base_break_even[symbol][side],
                    "runtime_probability_floor": lower_bounds[symbol][side],
                    "cost_row_sha256": cost_row_sha256[symbol],
                }
            )
            for side in ("BUY", "SELL")
        }
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    qualification_surface = {
        "schema_version": "fxstack.runtime_native_mtvclc_qualification.v1",
        "edge_probability_reserve": SCALP_RUNTIME_NATIVE_EDGE_PROBABILITY_RESERVE,
        "base_break_even_probabilities": base_break_even,
        "win_probability_lower_bounds": lower_bounds,
        "evidence_cell_sha256": cell_hashes,
    }
    surface_sha256 = _canonical_sha256(qualification_surface)
    cost_rows_sha256 = _canonical_sha256(cost_rows)
    cost_mapping_sha256 = _canonical_sha256(cost_row_sha256)
    evidence_sha256 = _canonical_sha256(
        {
            "engine_sha256": engine_identity.engine_sha256,
            "qualification_surface_sha256": surface_sha256,
            "cost_mapping_sha256": cost_mapping_sha256,
            "capture_file_sha256": capture_file_sha256,
        }
    )
    execution_contract_sha256 = _canonical_sha256(
        {
            "strategy_id": MTVCLC_STRATEGY_ID,
            "strategy_version": MTVCLC_STRATEGY_VERSION,
            "config_sha256": MTVCLC_CONFIG_SHA256,
            "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
            "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
            "account_mode": expected_mode,
            "max_entries_per_symbol_utc_day": MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
        }
    )
    deployment_sha256 = _canonical_sha256(
        {
            "schema_version": "fxstack.runtime_native_mtvclc_deployment.v1",
            "generation_id": SCALP_RUNTIME_NATIVE_GENERATION_ID,
            "engine_sha256": engine_identity.engine_sha256,
            "config_sha256": MTVCLC_CONFIG_SHA256,
            "account_mode": expected_mode,
            "venue_id": IG_MT4_VENUE_ID,
            "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
            "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
            "capture_file_sha256": capture_file_sha256,
            "calibration_id": calibration_id,
        }
    )
    runtime_identity = {
        "generation_id": SCALP_RUNTIME_NATIVE_GENERATION_ID,
        "engine_sha256": engine_identity.engine_sha256,
        "evidence_sha256": evidence_sha256,
        "execution_contract_sha256": execution_contract_sha256,
    }
    runtime_certificate_sha256 = _canonical_sha256(runtime_identity)
    runtime_key_id = _canonical_sha256(
        {"source": "checked_in_runtime_identity", **runtime_identity}
    )
    evidence_key_id = _canonical_sha256(
        {"source": "hash_pinned_cost_capture", "sha256": evidence_sha256}
    )
    registry_sha256 = _canonical_sha256(
        {"generation_id": SCALP_RUNTIME_NATIVE_GENERATION_ID, "revision": 1}
    )
    authority = {
        "activation_authorized": True,
        "runtime_authorized": True,
        "entry_lane_authorized": True,
        "individual_trade_authorized": False,
        "registry_write_authorized": False,
        "broker_access_authorized": False,
        "broker_trade_authorized": False,
        "research_access_authorized": False,
        "real_account_authorized": expected_mode == "real",
    }
    verification = MTVCLCRuntimeReleaseVerification(
        valid=True,
        reason="",
        errors=(),
        authenticated=True,
        revocation_verified=True,
        admission_mode=SCALP_ADMISSION_MODE_SIGNED,
        release_bundle_sha256=runtime_certificate_sha256,
        certificate_sha256=runtime_certificate_sha256,
        runtime_release_certificate_sha256=runtime_certificate_sha256,
        evidence_bundle_sha256=evidence_sha256,
        evidence_certificate_sha256=evidence_sha256,
        evidence_sha256=evidence_sha256,
        signing_key_id=runtime_key_id,
        runtime_release_signing_key_id=runtime_key_id,
        evidence_signing_key_id=evidence_key_id,
        registry_generation_id=SCALP_RUNTIME_NATIVE_GENERATION_ID,
        generation_id=SCALP_RUNTIME_NATIVE_GENERATION_ID,
        strategy_id=MTVCLC_STRATEGY_ID,
        strategy_version=MTVCLC_STRATEGY_VERSION,
        engine_sha256=engine_identity.engine_sha256,
        engine_component_sha256=engine_identity.component_sha256,
        config_id=MTVCLC_CONFIG_ID,
        config_sha256=MTVCLC_CONFIG_SHA256,
        venue_id=IG_MT4_VENUE_ID,
        account_mode=expected_mode,
        scope_version=IG_MT4_SCALP_SCOPE_VERSION,
        symbol_scope=tuple(IG_MT4_SCALP_SYMBOLS),
        max_entries_per_symbol_utc_day=MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
        maximum_account_currency_risk_per_trade=1.0,
        issued_at_epoch=1.0,
        expires_at_epoch=SCALP_RUNTIME_NATIVE_EXPIRES_AT_EPOCH,
        release_expires_at_epoch=SCALP_RUNTIME_NATIVE_EXPIRES_AT_EPOCH,
        evidence_expires_at_epoch=SCALP_RUNTIME_NATIVE_EXPIRES_AT_EPOCH,
        registry_expires_at_epoch=SCALP_RUNTIME_NATIVE_EXPIRES_AT_EPOCH,
        registry_revision=1,
        registry_sha256=registry_sha256,
        authority_purpose=SCALP_RUNTIME_NATIVE_AUTHORITY_PURPOSE,
        authority=authority,
        deployment_sha256=deployment_sha256,
        execution_contract_sha256=execution_contract_sha256,
        qualification_surface_sha256=surface_sha256,
        qualification_surface=qualification_surface,
        win_probability_lower_bounds=lower_bounds,
        base_break_even_probabilities=base_break_even,
        evidence_cell_sha256=cell_hashes,
        evidence_cost_row_sha256=cost_row_sha256,
        cost_mapping_sha256=cost_mapping_sha256,
        cost_rows_sha256=cost_rows_sha256,
        cost_calibration_id=calibration_id,
        cost_calibration_source_sha256=cost_rows_sha256,
        cost_calibration_source_sha256_by_symbol=source_sha256_by_symbol,
        cost_calibration_row_sha256=cost_row_sha256,
        cost_calibrations=cost_rows,
    )
    return ScalpRuntimeAdmission(
        valid=True,
        reason="",
        errors=(),
        verification=verification,
        engine_identity=engine_identity,
        cost_calibrations=costs,
        bundle_file_sha256=runtime_certificate_sha256,
        evidence_public_key_file_sha256=evidence_key_id,
        release_public_key_file_sha256=runtime_key_id,
        bundle_path="runtime-native",
        evidence_public_key_path="runtime-native",
        release_public_key_path="runtime-native",
    )


def _clone_runtime_native_admission(
    template: ScalpRuntimeAdmission,
) -> ScalpRuntimeAdmission:
    """Clone only the mutable maps in a cached runtime-native admission."""

    source = template.verification
    base_break_even = {
        symbol: dict(row)
        for symbol, row in source.base_break_even_probabilities.items()
    }
    lower_bounds = {
        symbol: dict(row) for symbol, row in source.win_probability_lower_bounds.items()
    }
    cell_hashes = {
        symbol: dict(row) for symbol, row in source.evidence_cell_sha256.items()
    }
    qualification_surface = dict(source.qualification_surface)
    qualification_surface["base_break_even_probabilities"] = base_break_even
    qualification_surface["win_probability_lower_bounds"] = lower_bounds
    qualification_surface["evidence_cell_sha256"] = cell_hashes
    evidence_cost_rows = dict(source.evidence_cost_row_sha256)
    calibration_cost_rows = (
        evidence_cost_rows
        if source.cost_calibration_row_sha256 is source.evidence_cost_row_sha256
        else dict(source.cost_calibration_row_sha256)
    )
    verification = replace(
        source,
        authority=dict(source.authority),
        qualification_surface=qualification_surface,
        win_probability_lower_bounds=lower_bounds,
        base_break_even_probabilities=base_break_even,
        evidence_cell_sha256=cell_hashes,
        evidence_cost_row_sha256=evidence_cost_rows,
        cost_calibration_source_sha256_by_symbol=dict(
            source.cost_calibration_source_sha256_by_symbol
        ),
        cost_calibration_row_sha256=calibration_cost_rows,
        cost_calibrations={
            symbol: dict(row) for symbol, row in source.cost_calibrations.items()
        },
    )
    return replace(template, verification=verification)


def _runtime_native_admission(
    settings: Any,
    *,
    engine_identity: ProductionScalpEngineIdentity,
) -> ScalpRuntimeAdmission:
    """Build the one local rule-engine contract used by demo and real accounts.

    The runtime no longer waits for an externally issued release bundle.  It
    binds the checked-in engine identity to the hash-pinned broker cost capture
    and derives the minimum payoff probability used by the existing risk seam.
    The 2 percentage-point reserve is deliberately above the exact bracket
    break-even probability; it does not create a trade unless the MTVCLC signal,
    live-spread, quote-freshness, broker-contract, and risk rules all pass.
    """

    expected_mode = (
        str(getattr(settings, "live_expected_account_mode", "demo") or "demo")
        .strip()
        .lower()
    )
    if expected_mode == "live":
        expected_mode = "real"
    if expected_mode not in {"demo", "real"}:
        reason = "scalp_runtime_native_account_mode_invalid"
        expectation = _expectation(settings, engine_identity=engine_identity)
        return ScalpRuntimeAdmission(
            valid=False,
            reason=reason,
            errors=(reason,),
            verification=_invalid_verification(
                reason=reason,
                expectation=expectation,
            ),
            engine_identity=engine_identity,
        )

    projection = load_runtime_cost_snapshot(
        capture_path=str(
            getattr(settings, "production_scalp_cost_capture_file", "") or ""
        ),
        expected_file_sha256=str(
            getattr(settings, "production_scalp_cost_capture_sha256", "") or ""
        ),
        account_currency="USD",
    )
    if not projection.valid:
        reason = projection.reason or "scalp_runtime_native_cost_projection_invalid"
        expectation = _expectation(settings, engine_identity=engine_identity)
        return ScalpRuntimeAdmission(
            valid=False,
            reason=reason,
            errors=tuple(projection.errors) or (reason,),
            verification=_invalid_verification(
                reason=reason,
                expectation=expectation,
            ),
            engine_identity=engine_identity,
        )

    costs = tuple(projection.costs_by_symbol[symbol] for symbol in IG_MT4_SCALP_SYMBOLS)
    # The capture and every engine component were re-read above. Cache only the
    # deterministic derived surface, then clone its mutable maps so no caller
    # can alter data shared with a later cycle.
    return _clone_runtime_native_admission(
        _build_runtime_native_admission(
            engine_identity=engine_identity,
            costs=costs,
            expected_mode=expected_mode,
            capture_file_sha256=projection.capture_file_sha256,
            calibration_id=projection.calibration_id,
        )
    )


def verify_configured_scalp_runtime_admission(
    settings: Any,
    *,
    now_epoch: float,
    policy: Any | None = None,
    package_root: str | Path | None = None,
    registry_anchor: MTVCLCRuntimeReleaseRegistryAnchor | None = None,
) -> ScalpRuntimeAdmission:
    """Build the runtime-native MTVCLC contract and pinned cost surface."""

    configured_root = Path(
        getattr(settings, "project_root", Path(__file__).resolve().parents[3])
    ).resolve()
    repository_root: Path | None = None
    for candidate in (configured_root, configured_root.parent):
        if (candidate / "MQL4").is_dir():
            repository_root = candidate
            break
    engine_identity = production_scalp_engine_identity(
        package_root=package_root,
        repository_root=repository_root,
    )
    if policy is not None and policy != FROZEN_MTVCLC_POLICY:
        expectation = _expectation(settings, engine_identity=engine_identity)
        reason = "scalp_runtime_native_policy_mismatch"
        return ScalpRuntimeAdmission(
            valid=False,
            reason=reason,
            errors=(reason,),
            verification=_invalid_verification(
                reason=reason,
                expectation=expectation,
            ),
            engine_identity=engine_identity,
        )
    native_cost_path = str(
        getattr(settings, "production_scalp_cost_capture_file", "") or ""
    ).strip()
    native_cost_sha256 = str(
        getattr(settings, "production_scalp_cost_capture_sha256", "") or ""
    ).strip()
    if native_cost_path or native_cost_sha256:
        return _runtime_native_admission(
            settings,
            engine_identity=engine_identity,
        )

    # Migration-only compatibility for installations that have not yet received
    # the shared runtime-native cost contract. Active Windows launchers always
    # set that contract, so demo and real use the same runtime-native path.
    expectation = _expectation(settings, engine_identity=engine_identity)
    bundle_path = _resolve_path(
        settings,
        getattr(settings, "production_scalp_mtvclc_release_bundle", ""),
    )
    evidence_key_path = _resolve_path(
        settings,
        getattr(settings, "production_scalp_mtvclc_evidence_verify_key_file", ""),
    )
    release_key_path = _resolve_path(
        settings,
        getattr(settings, "production_scalp_mtvclc_release_verify_key_file", ""),
    )

    def invalid(reason: str) -> ScalpRuntimeAdmission:
        return ScalpRuntimeAdmission(
            valid=False,
            reason=reason,
            errors=(reason,),
            verification=_invalid_verification(reason=reason, expectation=expectation),
            engine_identity=engine_identity,
            bundle_path=str(bundle_path or ""),
            evidence_public_key_path=str(evidence_key_path or ""),
            release_public_key_path=str(release_key_path or ""),
        )

    now = float(now_epoch)
    if not math.isfinite(now) or now <= 0.0:
        return invalid("mtvclc_runtime_release_clock_invalid")
    if policy is not None and policy != FROZEN_MTVCLC_POLICY:
        return invalid("mtvclc_runtime_release_policy_not_frozen")
    expectation_error = expectation.validation_error()
    if expectation_error:
        return invalid(expectation_error)
    if bundle_path is None:
        return invalid("mtvclc_runtime_release_bundle_path_missing")
    if evidence_key_path is None:
        return invalid("mtvclc_runtime_release_evidence_public_key_path_missing")
    if release_key_path is None:
        return invalid("mtvclc_runtime_release_public_key_path_missing")

    try:
        bundle_bytes = _read_bounded(
            bundle_path,
            limit=MAX_SCALP_VALIDATION_BUNDLE_BYTES,
            reason_prefix="mtvclc_runtime_release_bundle",
        )
        evidence_key_bytes = _read_bounded(
            evidence_key_path,
            limit=MAX_SCALP_PUBLIC_KEY_BYTES,
            reason_prefix="mtvclc_runtime_release_evidence_public_key_file",
        )
        release_key_bytes = _read_bounded(
            release_key_path,
            limit=MAX_SCALP_PUBLIC_KEY_BYTES,
            reason_prefix="mtvclc_runtime_release_public_key_file",
        )
        parsed = json.loads(bundle_bytes.decode("utf-8"))
        if not isinstance(parsed, Mapping):
            raise RuntimeError("mtvclc_runtime_release_bundle_malformed")
        evidence_public_key = _load_public_key(
            evidence_key_bytes,
            reason_prefix="mtvclc_runtime_release_evidence_public_key",
        )
        release_public_key = _load_public_key(
            release_key_bytes,
            reason_prefix="mtvclc_runtime_release_public_key",
        )
    except UnicodeDecodeError:
        return invalid("mtvclc_runtime_release_bundle_encoding_invalid")
    except json.JSONDecodeError:
        return invalid("mtvclc_runtime_release_bundle_json_invalid")
    except RuntimeError as exc:
        return invalid(str(exc))

    verification = verify_mtvclc_runtime_release(
        bundle=dict(parsed),
        release_public_key=release_public_key,
        evidence_public_key=evidence_public_key,
        expectation=expectation,
        now_epoch=now,
        registry_anchor=registry_anchor,
    )
    reason = ""
    costs: tuple[MTVCLCCostCalibration, ...] = ()
    if verification.valid:
        if verification.admission_mode != SCALP_ADMISSION_MODE_SIGNED:
            reason = "mtvclc_runtime_release_signed_admission_required"
        elif verification.authenticated is not True:
            reason = "mtvclc_runtime_release_unauthenticated"
        elif verification.revocation_verified is not True:
            reason = "mtvclc_runtime_release_registry_unverified"
        else:
            try:
                costs = _typed_costs(verification)
            except RuntimeError as exc:
                reason = str(exc)
    if reason:
        verification = replace(
            verification,
            valid=False,
            reason=reason,
            errors=(reason,),
        )
    return ScalpRuntimeAdmission(
        valid=bool(verification.valid),
        reason=str(verification.reason),
        errors=tuple(verification.errors),
        verification=verification,
        engine_identity=engine_identity,
        cost_calibrations=costs if verification.valid else (),
        bundle_file_sha256=hashlib.sha256(bundle_bytes).hexdigest(),
        evidence_public_key_file_sha256=hashlib.sha256(evidence_key_bytes).hexdigest(),
        release_public_key_file_sha256=hashlib.sha256(release_key_bytes).hexdigest(),
        bundle_path=str(bundle_path),
        evidence_public_key_path=str(evidence_key_path),
        release_public_key_path=str(release_key_path),
    )


__all__ = [
    "MAX_SCALP_PUBLIC_KEY_BYTES",
    "MAX_SCALP_VALIDATION_BUNDLE_BYTES",
    "SCALP_ADMISSION_MODE_SIGNED",
    "SCALP_VALIDATION_BUNDLE_SCHEMA",
    "ScalpRuntimeAdmission",
    "verify_configured_scalp_runtime_admission",
]
