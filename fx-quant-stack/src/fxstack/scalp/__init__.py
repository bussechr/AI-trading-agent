"""The scalper core -- the seed of the complete rebuild.

A deliberately small M1 scalping engine that talks to the existing bridge HTTP
API as an ordinary consumer and reuses only the verified-good parts of the old
stack (risk/sizing money math, the honest-cost doctrine, the spread-qualified
tier arithmetic from the judged design panel). It does NOT import the runner,
the orchestration layer, or any release ceremony -- those are scheduled for
deletion, and nothing here may grow a dependency on them.

Shape (conjunctive, deterministic, LLM-free hot path):

    ticks -> M1 bars (gap-invalidated)
          -> per-pair dislocation signal   (proposes, never sizes)
          -> spread sentinel               (absolute veto)
          -> session router                (pair-hour veto)
          -> risk sizing                   (sole sizing authority, fail-closed)
          -> ledger                        (every decision, every veto, every fill)

Modes: shadow (paper fills from live ticks, spread paid honestly) is the only
implemented mode. Live submission arrives only after the shadow ledger proves
the machinery against the pre-registered kill criteria -- aggression is not
unlocked by hope.
"""

from fxstack.scalp.config import ScalpConfig

__all__ = ["ScalpConfig"]
