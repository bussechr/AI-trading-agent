# AGENT: ROLE: Pure typed contracts at the production strategy-proposal boundary.
# AGENT: ENTRYPOINT: `EntryEvaluationRequest` in; `EntryProposal` out.
# AGENT: PRIMARY INPUTS: versioned, closed, quality-labelled bars and live spread.
# AGENT: PRIMARY OUTPUTS: an explicitly unqualified entry candidate or ordered refusal reasons.
# AGENT: STATE / SIDE EFFECTS: immutable data only; no sizing, persistence, queueing, or execution.
"""Production-owned contracts for pure entry-strategy evaluation.

``EntryProposal.allowed`` means only that the named strategy produced a
mathematical candidate.  Every proposal from this layer remains
``candidate_unqualified``: a later production authority must apply portfolio,
risk, sizing, frequency, and execution checks before any command can exist.
In particular, ``win_probability`` is intentionally ``None`` because the
dislocation control has no calibrated probability contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


ENTRY_BAR_SCHEMA_VERSION = "fxstack.entry.closed_bar.v1"
ENTRY_PROPOSAL_SCHEMA_VERSION = "fxstack.entry.proposal.v1"
ENTRY_PROPOSAL_QUALIFICATION: Literal["candidate_unqualified"] = (
    "candidate_unqualified"
)
IMMEDIATE_MARKET_EXECUTION_TYPE: Literal["market"] = "market"
IMMEDIATE_ENTRY_MAX_DELAY_SECONDS = 5

EntrySide = Literal["BUY", "SELL"]
EntryQualification = Literal["candidate_unqualified"]


@dataclass(frozen=True, slots=True)
class EntryBar:
    """One finalized strategy-timeframe bar with explicit source identity.

    ``minute_epoch`` is the UTC epoch at the start of the bar.  The evaluator
    validates that every supplied bar is closed, quality-clean, from one
    versioned source, and exactly ``bar_seconds`` after its predecessor.
    """

    symbol: str
    venue_id: str
    source_id: str
    source_version: str
    minute_epoch: int
    bar_seconds: int
    open: float
    high: float
    low: float
    close: float
    bid_close: float
    ask_close: float
    closed: bool
    quality_flags: tuple[str, ...] = ()
    schema_version: str = field(
        init=False,
        default=ENTRY_BAR_SCHEMA_VERSION,
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EntryEvaluationRequest:
    """Pure input to one strategy evaluation for one canonical symbol."""

    symbol: str
    bars: tuple[EntryBar, ...]
    spread_bps: float


@dataclass(frozen=True, slots=True)
class EntryProposal:
    """A strategy result that is never, by itself, authority to trade.

    Refusals use ``allowed=False`` and one or more deterministic ``reasons``.
    A mathematical candidate uses ``allowed=True`` and an empty reason tuple,
    but remains execution-unqualified by construction.
    """

    strategy_id: str
    strategy_version: str
    config_sha256: str
    symbol: str
    instrument_id: str
    venue_id: str
    source_id: str
    source_version: str
    allowed: bool
    reasons: tuple[str, ...]
    side: EntrySide | None = None
    minute_epoch: int | None = None
    ref_mid: float | None = None
    entry_price: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None
    atr_bps: float | None = None
    stop_bps: float | None = None
    target_bps: float | None = None
    disp_z: float | None = None
    spread_bps: float | None = None
    p_star: float | None = None
    time_stop_bars: int | None = None
    entry_deadline_epoch: int | None = None
    execution_type: Literal["market"] = field(
        init=False,
        default=IMMEDIATE_MARKET_EXECUTION_TYPE,
    )
    pending_orders_forbidden: bool = field(init=False, default=True)
    win_probability: float | None = field(init=False, default=None)
    qualification: EntryQualification = field(
        init=False,
        default=ENTRY_PROPOSAL_QUALIFICATION,
    )
    execution_qualified: bool = field(init=False, default=False)
    schema_version: str = field(
        init=False,
        default=ENTRY_PROPOSAL_SCHEMA_VERSION,
    )

    def __post_init__(self) -> None:
        if self.allowed and self.reasons:
            raise ValueError("allowed entry proposal cannot carry refusal reasons")
        if not self.allowed and not self.reasons:
            raise ValueError("refused entry proposal requires at least one reason")
        if self.allowed and (
            type(self.entry_deadline_epoch) is not int
            or int(self.entry_deadline_epoch) <= 0
        ):
            raise ValueError(
                "allowed entry proposal requires a positive integer entry deadline"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "ENTRY_BAR_SCHEMA_VERSION",
    "ENTRY_PROPOSAL_QUALIFICATION",
    "ENTRY_PROPOSAL_SCHEMA_VERSION",
    "IMMEDIATE_ENTRY_MAX_DELAY_SECONDS",
    "IMMEDIATE_MARKET_EXECUTION_TYPE",
    "EntryBar",
    "EntryEvaluationRequest",
    "EntryProposal",
    "EntryQualification",
    "EntrySide",
]
