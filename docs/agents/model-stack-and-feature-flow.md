# Model Stack And Feature Flow

## Primary Files
- [scorer.py](../../fx-quant-stack/src/fxstack/live/scorer.py)
- [policy.py](../../fx-quant-stack/src/fxstack/live/policy.py)
- [fx_lifecycle.py](../../fx-quant-stack/src/fxstack/features/fx_lifecycle.py)
- [multi_tf_contract.py](../../fx-quant-stack/src/fxstack/features/multi_tf_contract.py)
- [session_contract.py](../../fx-quant-stack/src/fxstack/features/session_contract.py)
- [settings.py](../../fx-quant-stack/src/fxstack/settings.py)

## Upstream
- [ops-entrypoints.md](ops-entrypoints.md)

## Downstream
- [runtime-loop.md](runtime-loop.md)
- [twin-vs-prod-parity.md](twin-vs-prod-parity.md)

## Flow
- raw bars -> feature parquet via `ParquetStore`
- `fx_lifecycle.py` derives lifecycle, spread, regime, scenario, and trend features
- `session_contract.py` owns the UTC session cutovers and the current `fx_features_v2` / `utc_session_buckets_v2` / `hierarchical_v2` model-data contract
- `multi_tf_contract.py` aligns anchor M5 rows with M15/H1/H4/D context rows and emits `<tf>_available`, `<tf>_fresh`, and `<tf>_age_secs` for each requested context
- context values older than one source interval are masked and stale rows are rejected by the shared batch/latest finalizer before model inference
- cross-pair context uses backward as-of alignment, signed log returns, and explicit coverage/age diagnostics so missing peers cannot masquerade as neutral observations
- offline Feast retrieval is accepted only when requested non-key features contain usable values; empty or all-null services fall back explicitly to the point-in-time parquet builder instead of producing neutral-looking training rows
- directional-belief query rows are bounded before hypothesis expansion, preserving time-span coverage and outcome indices while preventing candidate-frame memory growth from scaling unchecked
- hierarchical rows carry a watermark and partition fingerprint covering every anchor, context, and cross-pair raw stream; training reuses a cache only when both still match
- lifecycle feature regeneration writes a complete staged pair/timeframe snapshot, then swaps it into place so rows omitted by the current contract cannot survive from an older schema
- `LiveScorer` selects model inputs, enriches meta inputs, and emits probabilities + diagnostics
- `policy.py` turns those probabilities + features into edge, uncertainty, structure timing, and gate decisions
- the final live policy gate rejects non-finite and out-of-domain numeric inputs before any threshold comparison
- settings provide thresholds, spread caps, blocked sessions, manifest paths, and execution toggles

## Handshakes
- scorer consumes model feature columns declared in artifacts
- Feast service hashes, sequence-dataset cache keys, lineage snapshots, registry schemas, and model sidecars all carry the v2 contract versions
- activation and runtime loading fail closed when a registry schema or artifact sidecar is unversioned or mismatched
- portfolio RL policy manifests publish an exact local-file SHA-256; activation preserves that full ref, runtime requires one canonical identity across all pairs, and any later missing/replaced checkpoint hard-blocks RL-mode entries until reactivation
- policy diagnostics feed runtime decisions, shadow policy, adaptive policy, and twin reports
- lifecycle models reuse the same feature family but different row construction
- numerical model artifacts persist their training-time fill statistics; inference reuses those values and rejects non-finite or zero-variance training inputs instead of silently fitting degenerate regimes
- adaptive percentile features use bounded causal rolling statistics; replay callers must retain the pre-start warm-up rows used by live history

## Migration

- the v2 UTC session cutovers change the meaning of rows around 07:00 and 12:00 UTC; existing feature caches and trained artifacts are not relabeled in place
- the first training run after this migration invalidates feature snapshots without raw-source markers and replaces the complete pair/timeframe scope; `--force-retrain` always bypasses feature-cache reuse
- retrain all affected model families, regenerate feature/sequence caches, and activate only artifacts whose root and nested model sidecars are present, valid JSON, non-empty, and stamped with the current contract
- new saves bind canonical semantic metadata plus payload/report bytes to a portable SHA-256 identity; registry refs pin that digest and an exact registered version while cooperative locks span save/load, so legacy or unbound artifacts must be retrained

## Related Docs
- [runtime-loop.md](runtime-loop.md)
- [../../docs/STRATEGY_DECISION_DAG.md](../../docs/STRATEGY_DECISION_DAG.md)
