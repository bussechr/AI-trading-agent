# Twin Vs Prod Parity

## Primary Files
- [adaptive_policy.py](../../fx-quant-stack/src/fxstack/backtest/adaptive_policy.py)
- [fxstack_digital_twin_backtest.py](../../tools/fxstack_digital_twin_backtest.py)
- [runner.py](../../fx-quant-stack/src/fxstack/runtime/runner.py)

## Upstream
- [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md)
- [runtime-loop.md](runtime-loop.md)

## Downstream
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)

## Shared Logic
- environment classification
- playbook routing
- location scoring
- trigger scoring
- adaptive entry quality
- re-entry cooldown
- tempo-gap logic
- replacement keep score
- adaptive lifecycle decision

## Twin-Only Surface
- full portfolio replay
- artifact emission and guardrail reports
- same-window strict baseline comparisons
- validation against `/v2/decision-snapshots`

## Prod-Only Surface
- live bridge freshness gates
- broker command submission and ACK handling
- runtime state patching for dashboard and ops
- MT4 tick / bar refresh and heartbeat integration

## Validation Path
- use strict twin for replay baseline
- compare adaptive twin against strict twin for quality and aggressiveness
- compare prod adaptive diagnostics against twin on overlapping windows

## Handshakes
- shared import boundary: [adaptive_policy.py](../../fx-quant-stack/src/fxstack/backtest/adaptive_policy.py) -> [runner.py](../../fx-quant-stack/src/fxstack/runtime/runner.py)
- live validation source: [app.py](../../fx-quant-stack/src/fxstack/api/app.py) `/v2/decision-snapshots`
- promotion yardstick: twin strict/adaptive comparison artifacts from [fxstack_digital_twin_backtest.py](../../tools/fxstack_digital_twin_backtest.py)

## Related Docs
- [runtime-loop.md](runtime-loop.md)
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [../../fx-quant-stack/docs/promotion_gate.md](../../fx-quant-stack/docs/promotion_gate.md)
