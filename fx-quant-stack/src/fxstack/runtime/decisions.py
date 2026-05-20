"""Per-cycle decision plumbing — initial seam carved from ``runner.py``.

This module holds the **risk-envelope singleton** used by the live loop's
risk pre-check. Keeping it here (rather than as a private global inside
runner.py) gives tests and operator-side extensions a stable import path
for swapping the envelope without having to monkey-patch internals of the
9k-line runner module.

What lives here:

* :data:`_RUNTIME_RISK_ENVELOPE` — module-level singleton, lazily initialized
  to :func:`fxstack.risk.envelope.default_envelope`.
* :func:`runtime_risk_envelope` — accessor used by the runner per cycle.
* :func:`set_runtime_risk_envelope` — setter for tests / runtime extensions
  to attach post-rules. Pass ``None`` to reset to the default.

What does NOT live here yet:

* :func:`_risk_kernel_config_from_settings` and ``_evaluate_runtime_risk_kernel``
  remain in ``runner.py`` — they depend on runner-local utility helpers
  (``_safe_float``, ``_clip01``) and migrating them cleanly needs those
  utils in a shared module first. Pull them across once those moves are
  also made.
"""

from __future__ import annotations

from fxstack.risk.envelope import RiskEnvelope, default_envelope

_RUNTIME_RISK_ENVELOPE: RiskEnvelope | None = None


def runtime_risk_envelope() -> RiskEnvelope:
    """Return the module-level envelope used by the runtime risk pre-check.

    Memoized so post-rule lists stay stable across the process lifetime.
    The default is kernel-only — behavior identical to a direct
    ``evaluate_risk_decision`` call.
    """
    global _RUNTIME_RISK_ENVELOPE
    if _RUNTIME_RISK_ENVELOPE is None:
        _RUNTIME_RISK_ENVELOPE = default_envelope()
    return _RUNTIME_RISK_ENVELOPE


def set_runtime_risk_envelope(envelope: RiskEnvelope | None) -> None:
    """Swap the runtime envelope (tests, or to attach operator post-rules).

    Passing ``None`` resets to the default on the next access.
    """
    global _RUNTIME_RISK_ENVELOPE
    _RUNTIME_RISK_ENVELOPE = envelope


__all__ = ["runtime_risk_envelope", "set_runtime_risk_envelope"]
