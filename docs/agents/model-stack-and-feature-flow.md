# Model Stack And Feature Flow

## Primary Files
- [model_manifest_preflight.py](../../fx-quant-stack/src/fxstack/runtime/model_manifest_preflight.py)
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
- [causal-research-and-runtime-validation.md](causal-research-and-runtime-validation.md)

## Flow
- raw bars -> feature parquet via `ParquetStore`
- `fx_lifecycle.py` derives lifecycle, spread, regime, scenario, and trend features
- `session_contract.py` owns the UTC session cutovers and the current `fx_features_v2` / `utc_session_buckets_v2` / `hierarchical_v2` model-data contract
- `multi_tf_contract.py` aligns anchor M5 rows with M15/H1/H4/D context rows and emits `<tf>_available`, `<tf>_fresh`, and `<tf>_age_secs` for each requested context
- partial M15/H1 provider histories are filled only at missing timestamps by causal aggregation from closed M5 bars; stored provider bars remain authoritative and market-closure rows still fail freshness checks
- context values older than one source interval are masked and stale rows are rejected by the shared batch/latest finalizer before model inference
- cross-pair context uses backward as-of alignment, signed log returns, and explicit coverage/age diagnostics so missing peers cannot masquerade as neutral observations
- offline Feast retrieval is accepted only when requested non-key features contain usable values; empty or all-null services fall back explicitly to the point-in-time parquet builder instead of producing neutral-looking training rows
- directional-belief query rows are bounded before hypothesis expansion, preserving time-span coverage and outcome indices while preventing candidate-frame memory growth from scaling unchecked
- hierarchical rows carry a watermark and partition fingerprint covering every anchor, context, and cross-pair raw stream; training reuses a cache only when both still match
- lifecycle feature regeneration writes a complete staged pair/timeframe snapshot, then swaps it into place so rows omitted by the current contract cannot survive from an older schema
- `LiveScorer` selects model inputs, enriches meta inputs, and emits probabilities + diagnostics
- intraday artifacts retain the trained raw `P(up)` contract for meta-model features, while entry policy consumes side-conditional confidence (`P(up)` for long and `1-P(up)` for short); the two values are persisted separately and must never be substituted for one another
- `policy.py` turns those probabilities + features into edge, uncertainty, structure timing, and gate decisions
- the final live policy gate rejects non-finite and out-of-domain numeric inputs before any threshold comparison
- settings provide thresholds, spread caps, blocked sessions, manifest paths, and execution toggles

## Handshakes
- scorer consumes model feature columns declared in artifacts
- Feast service hashes, sequence-dataset cache keys, lineage snapshots, registry schemas, and model sidecars all carry the v2 contract versions
- lifecycle promotion preserves explicit zero-valued calibration metrics, and diagnostic challenger seeds never become the binding incumbent unless the configured portfolio champion names them
- every binding XGBoost probability calibrator uses an embargoed chronological tail rather than the rows used to fit its preliminary learner; small calibration samples use smooth sigmoid calibration, and the final artifact records the split and method
- Tier-1 bundle eligibility requires `eligible` promotion reports for swing, intraday, meta, exit, reversal-failure, and reversal-opportunity models. Tier-2 still requires the complete swing/intraday/meta entry stack; file presence or a strong meta report cannot mask a failed directional specialist
- activation and runtime loading fail closed when a registry schema or artifact sidecar is unversioned or mismatched, or when the registry promotion status is anything other than `eligible`
- a cross-pair directional-belief bundle may declare `pair=GLOBAL`; that scope exception applies only to the directional-belief component and does not relax its feature-contract or payload-integrity checks
- Windows launch runs the contract before process reset/spawn, and every Python runtime entrypoint repeats it before bridge/service access. The read-only preflight SHA-256 anchors the active manifest through DB seeding and loaded-runtime comparison, so required-pair presence, model-set ID, registry path, or available artifact-identity drift fails startup.
- xgb-only registries omit policy-disabled deep artifacts, and belief-disabled runs omit the belief artifact; registries never advertise placeholder paths, while enabled policies still require their real sidecars at activation
- portfolio RL policy manifests publish an exact local-file SHA-256; activation preserves that full ref, runtime requires one canonical identity across all pairs, and any later missing/replaced checkpoint hard-blocks RL-mode entries until reactivation
- policy diagnostics feed runtime decisions, the single direct adaptive policy, and physically isolated causal-research reports; the production runtime does not compute a baseline shadow policy
- lifecycle models reuse the same feature family but different row construction
- numerical model artifacts persist their training-time fill statistics; inference reuses those values and rejects non-finite or zero-variance training inputs instead of silently fitting degenerate regimes
- supervised label builders omit the incomplete trailing horizon, and point-in-time snapshots additionally gate labels by outcome knowledge time rather than row timestamp
- adaptive percentile features use bounded causal rolling statistics; replay callers must retain the pre-start warm-up rows used by live history

## Migration

- the v2 UTC session cutovers change the meaning of rows around 07:00 and 12:00 UTC; existing feature caches and trained artifacts are not relabeled in place
- the first training run after this migration invalidates feature snapshots without raw-source markers and replaces the complete pair/timeframe scope; `--force-retrain` always bypasses feature-cache reuse
- retrain all affected model families, regenerate feature/sequence caches, and activate only artifacts whose root and nested model sidecars are present, valid JSON, non-empty, and stamped with the current contract
- artifacts trained before chronological calibration and binding swing/intraday promotion reports are research-only under the current contract and must be retrained; do not rewrite their metadata in place
- new saves bind canonical semantic metadata plus payload/report bytes to a portable SHA-256 identity; registry refs pin that digest and an exact registered version while cooperative locks span save/load, so legacy or unbound artifacts must be retrained

## Related Docs
- [runtime-loop.md](runtime-loop.md)
- [../../docs/STRATEGY_DECISION_DAG.md](../../docs/STRATEGY_DECISION_DAG.md)
