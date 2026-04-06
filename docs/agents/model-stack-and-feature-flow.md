# Model Stack And Feature Flow

## Primary Files
- [scorer.py](../../fx-quant-stack/src/fxstack/live/scorer.py)
- [policy.py](../../fx-quant-stack/src/fxstack/live/policy.py)
- [fx_lifecycle.py](../../fx-quant-stack/src/fxstack/features/fx_lifecycle.py)
- [multi_tf_contract.py](../../fx-quant-stack/src/fxstack/features/multi_tf_contract.py)
- [settings.py](../../fx-quant-stack/src/fxstack/settings.py)

## Upstream
- [ops-entrypoints.md](ops-entrypoints.md)

## Downstream
- [runtime-loop.md](runtime-loop.md)
- [twin-vs-prod-parity.md](twin-vs-prod-parity.md)

## Flow
- raw bars -> feature parquet via `ParquetStore`
- `fx_lifecycle.py` derives lifecycle, spread, regime, scenario, and trend features
- `multi_tf_contract.py` aligns anchor M5 rows with M15/H1/H4/D context rows
- `LiveScorer` selects model inputs, enriches meta inputs, and emits probabilities + diagnostics
- `policy.py` turns those probabilities + features into edge, uncertainty, structure timing, and gate decisions
- settings provide thresholds, spread caps, blocked sessions, manifest paths, and execution toggles

## Handshakes
- scorer consumes model feature columns declared in artifacts
- policy diagnostics feed runtime decisions, shadow policy, adaptive policy, and twin reports
- lifecycle models reuse the same feature family but different row construction

## Related Docs
- [runtime-loop.md](runtime-loop.md)
- [../../docs/STRATEGY_DECISION_DAG.md](../../docs/STRATEGY_DECISION_DAG.md)
