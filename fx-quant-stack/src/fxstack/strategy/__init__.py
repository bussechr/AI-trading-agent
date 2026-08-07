# AGENT: ROLE: Shared strategy-layer package for allocator, sleeve governance, and portfolio selection primitives.
# AGENT: ENTRYPOINT: imported by live runtime adaptive paths and isolated research.
# AGENT: PRIMARY INPUTS: adaptive candidate rows, open-position snapshots, rolling sleeve metrics.
# AGENT: PRIMARY OUTPUTS: allocator rankings, replacement plans, sleeve health snapshots.
# AGENT: DEPENDS ON: `fxstack/strategy/allocator.py`, `fxstack/strategy/allocator_types.py`, `fxstack/strategy/sleeve_governance.py`.
# AGENT: CALLED BY: `fxstack/runtime/runner.py` and isolated research tooling.
# AGENT: STATE / SIDE EFFECTS: pure package exports only.
# AGENT: HANDSHAKES: production portfolio-construction seam exposed to immutable research snapshots.
# AGENT: SEE: `docs/agents/causal-research-and-runtime-validation.md` -> `fxstack/strategy/allocator.py` -> `docs/agents/runtime-loop.md`
