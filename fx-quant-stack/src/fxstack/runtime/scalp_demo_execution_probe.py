# AGENT: ROLE: Fail-closed compatibility rejection for the retired IG demo execution-probe surface.
# AGENT: ENTRYPOINT: legacy imports may call the public helpers, but every supplied request is rejected.
# AGENT: PRIMARY INPUTS: legacy probe fields or an already constructed legacy request.
# AGENT: PRIMARY OUTPUTS: deterministic refusal only; no candidate, command identity, or payload fields.
# AGENT: STATE / SIDE EFFECTS: none; cannot authorize, qualify, size, enqueue, or execute a trade.
"""Non-executable compatibility boundary for the retired demo probe.

The former probe synthesized a BUY/SELL candidate after strategy qualification.
That lane is intentionally gone.  The names remain importable for stale callers
so an old deployment fails closed with a stable reason instead of regaining an
unsigned execution path through an import fallback.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


SCALP_DEMO_EXECUTION_PROBE_SCHEMA_VERSION = (
    "fxstack.runtime.scalp_demo_execution_probe.retired.v2"
)
SCALP_DEMO_EXECUTION_PROBE_REMOVED_REASON = "scalp_demo_execution_probe_removed_signed_release_and_strategy_qualification_required"
# Legacy live-loop imports retain this diagnostic label, but the retired
# compatibility module can never emit a qualified candidate using it.
SCALP_DEMO_EXECUTION_PROBE_PROBABILITY_SOURCE = "demo_execution_probe_removed"


@dataclass(frozen=True, slots=True)
class ScalpDemoExecutionProbeRequest:
    """Legacy request shape retained only for deterministic rejection."""

    probe_id: str
    symbol: str
    side: str
    schema_version: str = SCALP_DEMO_EXECUTION_PROBE_SCHEMA_VERSION

    def command_fields(self) -> dict[str, Any]:
        """Refuse construction of probe-specific executable payload fields."""

        raise RuntimeError(SCALP_DEMO_EXECUTION_PROBE_REMOVED_REASON)


@dataclass(frozen=True, slots=True)
class ScalpDemoExecutionProbeRequestResult:
    """Compatibility projection whose validity is unconditionally false."""

    enabled: bool
    request: None
    reasons: tuple[str, ...]
    schema_version: str = SCALP_DEMO_EXECUTION_PROBE_SCHEMA_VERSION

    @property
    def valid(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "enabled": bool(self.enabled),
            "valid": False,
            "request": None,
            "reasons": list(self.reasons),
        }


def validate_scalp_demo_execution_probe_request(
    *,
    probe_id: Any = "",
    symbol: Any = "",
    side: Any = "",
    live_mode: bool,
    expected_account_mode: Any,
    admission_mode: Any,
) -> ScalpDemoExecutionProbeRequestResult:
    """Reject every supplied legacy request regardless of runtime posture."""

    del live_mode, expected_account_mode, admission_mode
    supplied = any(str(value or "").strip() for value in (probe_id, symbol, side))
    return ScalpDemoExecutionProbeRequestResult(
        enabled=supplied,
        request=None,
        reasons=((SCALP_DEMO_EXECUTION_PROBE_REMOVED_REASON,) if supplied else ()),
    )


def scalp_demo_execution_probe_command_id(
    request: ScalpDemoExecutionProbeRequest,
    *,
    runtime_boot_id: Any,
) -> str:
    """Reject legacy attempts to mint a durable probe command identity."""

    del request, runtime_boot_id
    raise RuntimeError(SCALP_DEMO_EXECUTION_PROBE_REMOVED_REASON)


@dataclass(frozen=True, slots=True)
class ScalpDemoExecutionProbeCandidateResult:
    """Compatibility result that can never contain an executable candidate."""

    request: ScalpDemoExecutionProbeRequest
    qualified_candidate: None = None
    reasons: tuple[str, ...] = (SCALP_DEMO_EXECUTION_PROBE_REMOVED_REASON,)
    schema_version: str = SCALP_DEMO_EXECUTION_PROBE_SCHEMA_VERSION

    @property
    def accepted(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "accepted": False,
            "request": asdict(self.request),
            "reasons": list(self.reasons),
            "qualified_candidate": None,
        }


def build_scalp_demo_execution_probe_candidate(
    request: ScalpDemoExecutionProbeRequest,
    *,
    tick: Any,
    contract: Any,
    quote_rates: Any,
    account_currency: Any,
    broker_account_mode: Any,
    as_of_epoch: float,
) -> ScalpDemoExecutionProbeCandidateResult:
    """Return refusal only; no BUY/SELL candidate can be synthesized."""

    del tick, contract, quote_rates, account_currency, broker_account_mode, as_of_epoch
    return ScalpDemoExecutionProbeCandidateResult(request=request)
