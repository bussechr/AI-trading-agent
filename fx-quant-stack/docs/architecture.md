# V2 Architecture

## Layers

1. Data layer: Dukascopy CSV historical ingestion, parquet storage.
2. Feature/label layer: PIT features, triple barrier labels, purged validation.
3. Model layer: HMM regime + XGB swing/intraday + XGB meta-label.
4. Runtime layer: Postgres-backed command/state store with MT4 bridge-compatible APIs.
5. Live scoring: deterministic score contract + execution gate.

Execution remains IG + MT4 via existing `/v2/*` bridge protocol; broker execution wiring is unchanged.

## Compatibility

The API surface is intentionally aligned with current `/v2/*` endpoints consumed by MT4 EA and Next.js UI.
