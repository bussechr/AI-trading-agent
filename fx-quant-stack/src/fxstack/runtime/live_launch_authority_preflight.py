"""Fail-closed signed-release gate for Windows live runtime launchers.

The gate is deliberately read-only.  It authenticates the configured public
release material with the same runtime verifier used by the production scalp
loop, then validates an already-active durable execution authority obtained
from the authenticated bridge state.  A launch continuation may carry only
the resulting content binding; the signed material is still reverified and
must reproduce that exact binding after a controlled stack replacement.
"""

from __future__ import annotations

# AGENT: ROLE: Read-only signed-release admission before any live stack/runtime mutation.
# AGENT: CALLED BY: `launch_all.bat live` and live `21_start_runtime.bat --run/--background`.
# AGENT: SIDE EFFECTS: None; reads public release files and, for a first admission, authenticated bridge state.
# AGENT: HANDSHAKE: selected strategy + signed release + active durable authority -> exact continuation binding.

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import sys
import time
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fxstack.runtime.scalp_execution_authority import (
    authority_error as scalp_authority_error,
    expectation_from_authority as scalp_expectation_from_authority,
    validation_witness_error as scalp_validation_witness_error,
)
from fxstack.runtime.scalp_runtime_admission import (
    SCALP_ADMISSION_MODE_SIGNED,
    ScalpRuntimeAdmission,
    verify_configured_scalp_runtime_admission,
)
from fxstack.settings import get_settings


LIVE_LAUNCH_RELEASE_BINDING_SCHEMA = "fxstack.live_launch_release_binding.v1"
SUPPORTED_LIVE_STRATEGY_FAMILY = "mtvclc"
MAX_BRIDGE_STATE_BYTES = 8 * 1024 * 1024


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalized_strategy_family(value: Any) -> str:
    return str(value or "").strip().lower()


def _bridge_state_url(settings: Any) -> str:
    base = str(getattr(settings, "mt4_bridge_url", "") or "").strip().rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or (parsed.hostname or "").strip().lower()
        not in {"127.0.0.1", "localhost", "::1"}
    ):
        raise RuntimeError("live_launch_bridge_url_not_loopback")
    return f"{base}/v2/state"


def load_authenticated_bridge_state(
    settings: Any,
    *,
    timeout_secs: float = 3.0,
) -> dict[str, Any]:
    """Read the current service-owned authority state without mutating it."""

    headers = {"Accept": "application/json"}
    api_key = str(getattr(settings, "bridge_api_key", "") or "").strip()
    if api_key:
        headers["X-API-Key"] = api_key
    request = Request(_bridge_state_url(settings), headers=headers, method="GET")
    with urlopen(request, timeout=max(0.1, float(timeout_secs))) as response:  # noqa: S310
        raw = response.read(MAX_BRIDGE_STATE_BYTES + 1)
    if not raw or len(raw) > MAX_BRIDGE_STATE_BYTES:
        raise RuntimeError("live_launch_bridge_state_size_invalid")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("live_launch_bridge_state_invalid") from exc
    if not isinstance(parsed, Mapping):
        raise RuntimeError("live_launch_bridge_state_invalid")
    return dict(parsed)


@dataclass(frozen=True, slots=True)
class LiveLaunchAuthorityResult:
    valid: bool
    strategy_family: str
    authority_kind: str
    binding_sha256: str
    generation_id: str
    strategy_id: str
    strategy_version: str
    expires_at_epoch: float
    errors: tuple[str, ...]

    @property
    def reason(self) -> str:
        return str(self.errors[0]) if self.errors else ""

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "errors": list(self.errors), "reason": self.reason}


def _scalp_release_identity(
    *,
    settings: Any,
    admission: ScalpRuntimeAdmission,
) -> dict[str, Any]:
    verification = admission.verification
    return {
        "schema_version": LIVE_LAUNCH_RELEASE_BINDING_SCHEMA,
        "authority_kind": "production_mtvclc_runtime_release",
        "strategy_family": SUPPORTED_LIVE_STRATEGY_FAMILY,
        "admission_mode": str(verification.admission_mode).strip().lower(),
        "account_mode": str(verification.account_mode).strip().lower(),
        "generation_id": str(verification.generation_id),
        "strategy_id": str(verification.strategy_id),
        "strategy_version": str(verification.strategy_version),
        "certificate_sha256": str(verification.certificate_sha256).lower(),
        "release_bundle_sha256": str(verification.release_bundle_sha256).lower(),
        "runtime_release_signing_key_id": str(
            verification.runtime_release_signing_key_id
        ).lower(),
        "evidence_bundle_sha256": str(
            verification.evidence_bundle_sha256
        ).lower(),
        "evidence_certificate_sha256": str(
            verification.evidence_certificate_sha256
        ).lower(),
        "evidence_sha256": str(verification.evidence_sha256).lower(),
        "evidence_signing_key_id": str(
            verification.evidence_signing_key_id
        ).lower(),
        "registry_generation_id": str(verification.registry_generation_id),
        "registry_revision": int(verification.registry_revision),
        "registry_sha256": str(verification.registry_sha256).lower(),
        "engine_sha256": str(verification.engine_sha256).lower(),
        "config_sha256": str(verification.config_sha256).lower(),
        "deployment_sha256": str(verification.deployment_sha256).lower(),
        "execution_contract_sha256": str(
            verification.execution_contract_sha256
        ).lower(),
        "qualification_surface_sha256": str(
            verification.qualification_surface_sha256
        ).lower(),
        "cost_mapping_sha256": str(verification.cost_mapping_sha256).lower(),
        "cost_rows_sha256": str(verification.cost_rows_sha256).lower(),
        "venue_id": str(verification.venue_id).lower(),
        "symbol_scope": [str(item).upper() for item in verification.symbol_scope],
        "max_entries_per_symbol_utc_day": int(
            verification.max_entries_per_symbol_utc_day
        ),
        "expires_at_epoch": float(verification.expires_at_epoch),
        "bundle_file_sha256": str(admission.bundle_file_sha256).lower(),
        "release_public_key_file_sha256": str(
            admission.release_public_key_file_sha256
        ).lower(),
        "evidence_public_key_file_sha256": str(
            admission.evidence_public_key_file_sha256
        ).lower(),
    }


def validate_live_launch_authority(
    settings: Any,
    *,
    selected_strategy_family: str,
    now_epoch: float | None = None,
    expected_binding_sha256: str = "",
    current_state: Mapping[str, Any] | None = None,
    admission_verifier: Callable[..., ScalpRuntimeAdmission] | None = None,
) -> LiveLaunchAuthorityResult:
    """Validate a signed release and, initially, its active service authority.

    Supplying ``expected_binding_sha256`` is the controlled-replacement path:
    the public bundle is fully reverified after sync/stop and must reproduce
    the exact pre-mutation binding.  An environment marker alone can never
    pass because no boolean or caller-supplied identity is trusted.
    """

    family = _normalized_strategy_family(selected_strategy_family)
    configured_family = _normalized_strategy_family(
        getattr(settings, "entry_strategy_family", "")
    )
    errors: list[str] = []
    expected_binding = str(expected_binding_sha256 or "").strip().lower()
    if family != configured_family:
        errors.append("live_launch_strategy_family_changed")
    if family != SUPPORTED_LIVE_STRATEGY_FAMILY:
        errors.append("live_launch_signed_authority_strategy_unsupported")
    if expected_binding and not _is_sha256(expected_binding):
        errors.append("live_launch_expected_binding_invalid")

    now = float(time.time() if now_epoch is None else now_epoch)
    if not math.isfinite(now) or now <= 0.0:
        errors.append("live_launch_clock_invalid")

    admission: ScalpRuntimeAdmission | None = None
    if not errors:
        try:
            verifier = admission_verifier or verify_configured_scalp_runtime_admission
            admission = verifier(settings, now_epoch=now)
        except Exception as exc:
            errors.append(
                "live_launch_signed_release_verification_failed:"
                + type(exc).__name__
            )
    if admission is not None:
        verification = admission.verification
        if not admission.valid or not verification.valid:
            errors.append(
                "live_launch_signed_release_invalid:"
                + str(admission.reason or verification.reason or "unknown")
            )
        if (
            str(verification.admission_mode).strip().lower()
            != SCALP_ADMISSION_MODE_SIGNED
        ):
            errors.append("live_launch_signed_release_required")
        if (
            verification.authenticated is not True
            or verification.revocation_verified is not True
        ):
            errors.append("live_launch_signed_release_not_authenticated")
        if not _is_sha256(verification.certificate_sha256):
            errors.append("live_launch_signed_release_certificate_invalid")
        if not _is_sha256(verification.runtime_release_signing_key_id):
            errors.append("live_launch_signed_release_key_identity_invalid")
        if not _is_sha256(verification.evidence_signing_key_id):
            errors.append("live_launch_signed_evidence_key_identity_invalid")
        release_hashes = (
            verification.evidence_bundle_sha256,
            verification.evidence_certificate_sha256,
            verification.evidence_sha256,
            verification.registry_sha256,
            verification.deployment_sha256,
            verification.execution_contract_sha256,
            verification.qualification_surface_sha256,
            verification.cost_mapping_sha256,
            verification.cost_rows_sha256,
        )
        if any(not _is_sha256(value) for value in release_hashes):
            errors.append("live_launch_signed_release_binding_invalid")
        if (
            not _is_sha256(admission.bundle_file_sha256)
            or not _is_sha256(admission.release_public_key_file_sha256)
            or not _is_sha256(admission.evidence_public_key_file_sha256)
        ):
            errors.append("live_launch_signed_release_file_identity_invalid")
        if (
            not math.isfinite(float(verification.expires_at_epoch))
            or float(verification.expires_at_epoch) <= now
        ):
            errors.append("live_launch_signed_release_expired")
        expected_account_mode = str(
            getattr(settings, "live_expected_account_mode", "") or ""
        ).strip().lower()
        if expected_account_mode not in {"demo", "real"}:
            errors.append("live_launch_expected_account_mode_invalid")
        elif str(verification.account_mode or "").strip().lower() != (
            expected_account_mode
        ):
            errors.append("live_launch_signed_release_account_mode_changed")

    identity: dict[str, Any] = {}
    binding = ""
    if admission is not None and not errors:
        identity = _scalp_release_identity(settings=settings, admission=admission)
        binding = _canonical_sha256(identity)
        if expected_binding and binding != expected_binding:
            errors.append("live_launch_release_binding_changed")

    # A first admission must prove that this exact signed witness is already
    # represented by the service-owned active authority.  A continuation is
    # allowed only after the exact signed identity has been reverified above;
    # stop_all intentionally revokes the boot-bound execution lease.
    if admission is not None and not errors and not expected_binding:
        state = dict(current_state or {})
        authority = dict(state.get("production_scalp_authority") or {})
        expectation = scalp_expectation_from_authority(authority)
        authority_failure = scalp_authority_error(
            authority,
            expectation=expectation,
            now_epoch=now,
        )
        if authority_failure:
            errors.append(str(authority_failure))
        witness_failure = scalp_validation_witness_error(
            admission.verification.to_dict(),
            authority=authority,
            now_epoch=now,
        )
        if witness_failure:
            errors.append(str(witness_failure))
        expected_account_mode = str(identity.get("account_mode") or "")
        if str(authority.get("account_mode") or "").strip().lower() != (
            expected_account_mode
        ):
            errors.append("live_launch_authority_account_mode_changed")
        state_boot_id = str(state.get("runtime_boot_id") or "").strip()
        if not state_boot_id or state_boot_id != str(
            authority.get("runtime_boot_id") or ""
        ).strip():
            errors.append("live_launch_authority_runtime_boot_changed")

    unique_errors = tuple(dict.fromkeys(str(item) for item in errors if str(item)))
    verification = admission.verification if admission is not None else None
    return LiveLaunchAuthorityResult(
        valid=not unique_errors,
        strategy_family=family,
        authority_kind=str(identity.get("authority_kind") or ""),
        binding_sha256=binding if not unique_errors else "",
        generation_id=str(getattr(verification, "generation_id", "") or ""),
        strategy_id=str(getattr(verification, "strategy_id", "") or ""),
        strategy_version=str(getattr(verification, "strategy_version", "") or ""),
        expires_at_epoch=float(
            getattr(verification, "expires_at_epoch", 0.0) or 0.0
        ),
        errors=unique_errors,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the active signed release before live launch mutation."
    )
    parser.add_argument("--strategy-family", required=True)
    parser.add_argument("--expected-binding", default="")
    parser.add_argument("--binding-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    settings = get_settings()
    state: dict[str, Any] | None = None
    if not str(args.expected_binding or "").strip():
        try:
            state = load_authenticated_bridge_state(settings)
        except Exception as exc:
            print(
                "[live-authority] ERROR: active service authority unavailable:"
                + type(exc).__name__,
                file=sys.stderr,
            )
            return 2
    result = validate_live_launch_authority(
        settings,
        selected_strategy_family=str(args.strategy_family),
        expected_binding_sha256=str(args.expected_binding or ""),
        current_state=state,
    )
    if not result.valid:
        print(
            "[live-authority] ERROR: " + " | ".join(result.errors),
            file=sys.stderr,
        )
        return 2
    if args.binding_only:
        print(result.binding_sha256)
    else:
        print(json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the launcher
    raise SystemExit(main())


__all__ = [
    "LIVE_LAUNCH_RELEASE_BINDING_SCHEMA",
    "LiveLaunchAuthorityResult",
    "load_authenticated_bridge_state",
    "main",
    "validate_live_launch_authority",
]
