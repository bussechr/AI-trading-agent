"""The activation-time check that makes statistical evidence binding.

Everything else in this package computes numbers. This module is the part that
can say *no*, and it is written to be called from
``fxstack/training/activation.py`` beside the existing artifact-integrity check,
because a statistic that cannot block activation is decoration.

Design notes, learned from this codebase's failure mode:

  * ``gate_activation`` is fail-closed on ABSENCE as well as on failure. A model
    with no certificate is not "unknown", it is unvalidated, and unvalidated is
    a refusal when enforcement is on.
  * Enforcement is explicit per call (``enforce=``) rather than read from a
    global here, so the decision is visible at the call site instead of buried
    in env defaults. The caller passes the setting.
  * When ``enforce=False`` the gate still evaluates and still returns the full
    reason list -- it simply does not veto. That makes the "not yet enforced"
    state observable and countable rather than invisible, so switching it on is
    a known, measurable step instead of a leap.
  * The certificate must be bound to the artifact being activated. Passing the
    payload digest is mandatory; a certificate that verifies in isolation but
    was computed for a different model is worse than none, because it looks like
    diligence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from fxstack.validation.certificate import (
    AcceptanceThresholds,
    ValidationCertificate,
    load_certificate,
)

#: Filename convention for a certificate stored beside a model artifact.
CERTIFICATE_FILENAME = "validation_certificate.json"


@dataclass(frozen=True)
class GateResult:
    """Outcome of the activation-time validation check."""

    allowed: bool
    enforced: bool
    reasons: list[str] = field(default_factory=list)
    certificate: ValidationCertificate | None = None

    @property
    def validated(self) -> bool:
        """True when evidence exists and fully checks out, regardless of enforcement."""

        return not self.reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "enforced": bool(self.enforced),
            "validated": bool(self.validated),
            "reasons": list(self.reasons),
            "certificate_sha256": (self.certificate.certificate_sha256 if self.certificate else ""),
            "statistics": (dict(self.certificate.statistics) if self.certificate else {}),
        }


def certificate_path_for(artifact_path: str | Path) -> Path:
    """Where the certificate for a model artifact is expected to live."""

    path = Path(artifact_path)
    base = path.parent if path.suffix else path
    return base / CERTIFICATE_FILENAME


def read_certificate(path: str | Path) -> tuple[ValidationCertificate | None, list[str]]:
    """Load a certificate from disk, returning explicit reasons on failure."""

    target = Path(path)
    if not target.exists():
        return None, ["validation_certificate_absent"]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ["validation_certificate_unreadable"]
    if not isinstance(payload, dict):
        return None, ["validation_certificate_malformed"]
    try:
        return load_certificate(payload), []
    except (TypeError, ValueError):
        return None, ["validation_certificate_malformed"]


def gate_activation(
    *,
    artifact_path: str | Path,
    expected_payload_sha256: str,
    expected_dataset_fingerprint: str | None = None,
    enforce: bool = True,
    thresholds: AcceptanceThresholds | None = None,
    certificate_path: str | Path | None = None,
) -> GateResult:
    """Decide whether a model artifact may be activated.

    ``expected_payload_sha256`` must be the same digest the artifact-integrity
    check uses, so the certificate is provably about THIS payload.

    Returns a ``GateResult``; when ``enforce`` is true, ``allowed`` is false for
    any reason at all, including a missing certificate.
    """

    reasons: list[str] = []
    cert_path = Path(certificate_path) if certificate_path else certificate_path_for(artifact_path)
    certificate, load_reasons = read_certificate(cert_path)
    reasons.extend(load_reasons)

    if certificate is not None:
        ok, problems = certificate.verify(
            expected_model_payload_sha256=str(expected_payload_sha256),
            expected_dataset_fingerprint=(
                str(expected_dataset_fingerprint) if expected_dataset_fingerprint is not None else None
            ),
        )
        if not ok:
            reasons.extend(problems)
        # Re-apply thresholds at activation time. A certificate sealed under a
        # laxer bar must not pass a stricter current policy just because it says
        # ``passed: true``; the bar in force now is the bar that matters.
        if thresholds is not None:
            from fxstack.validation.certificate import evaluate_acceptance

            re_passed, re_reasons = evaluate_acceptance(certificate.statistics, thresholds=thresholds)
            if not re_passed:
                reasons.extend(f"recheck:{reason}" for reason in re_reasons)

    reasons = list(dict.fromkeys(reasons))
    allowed = (not reasons) if enforce else True
    return GateResult(allowed=allowed, enforced=bool(enforce), reasons=reasons, certificate=certificate)


def write_certificate(certificate: ValidationCertificate, *, artifact_path: str | Path) -> Path:
    """Persist a certificate next to its artifact and return the path."""

    target = certificate_path_for(artifact_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(certificate.to_dict(), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return target
