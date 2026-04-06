# AGENT: ROLE: Shared strategy-layer package for allocator, sleeve governance, and portfolio selection primitives.
# AGENT: ENTRYPOINT: imported by twin replay and live runtime adaptive paths.
# AGENT: PRIMARY INPUTS: adaptive candidate rows, open-position snapshots, rolling sleeve metrics.
# AGENT: PRIMARY OUTPUTS: allocator rankings, replacement plans, sleeve health snapshots.
# AGENT: DEPENDS ON: `fxstack/strategy/allocator.py`, `fxstack/strategy/allocator_types.py`, `fxstack/strategy/sleeve_governance.py`.
# AGENT: CALLED BY: `tools/fxstack_digital_twin_backtest.py`, `fxstack/runtime/runner.py`.
# AGENT: STATE / SIDE EFFECTS: pure package exports only.
# AGENT: HANDSHAKES: shared twin/prod portfolio-construction seam.
# AGENT: SEE: `docs/agents/twin-vs-prod-parity.md` -> `fxstack/strategy/allocator.py` -> `docs/agents/runtime-loop.md`

