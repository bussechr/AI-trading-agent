# fx-quant-stack

`fx-quant-stack` is the active Python package for the FX trading system. It owns the `fxstack` runtime, bridge API, persistence, features, models, training, portfolio/risk logic, and offline research contracts.

The root `src/trader` package contains only a source-level allowlisted facade for advisory self-improvement research, offline security checks, weight verification, and Lean export. Its parser has no runtime, bridge, live-data, database, training, activation, deployment, or operator commands; the duplicate application, domain, persistence, DTO, and protocol implementations are removed. No `trader`/`fx-trader` console alias is published, and the facade is not a public runtime entrypoint.

## Environment

```bash
cd fx-quant-stack
uv sync --extra dev --extra security --extra market_data_download --extra external_mlops --extra deep_inference --frozen
cd ..
```

Use the repository root as the working directory for ops scripts and tools. Keep credentials, account identifiers, and machine-specific broker settings out of Git.

## Operator entrypoints

On Windows:

```bat
ops\windows\00_preflight.bat
launch_all.bat endpoints
launch_all.bat live 10000
launch_all.bat status
launch_all.bat stop
```

The production host runs one baseline stack. `launch_all.bat live` validates signed release authority before replacing or starting services; explicit live mode, arming, scopes, expected account mode, and broker attestation remain mandatory. The default staged-safe posture is shadow-only.

See [Windows Ops Entrypoints](../docs/agents/ops-entrypoints.md) and [IG MT4 Setup](../docs/IG_MT4_SETUP.md).

## Data and training

Dukascopy CSV input defaults to `fx-quant-stack/data/dukascopy/{PAIR}_{TIMEFRAME}.csv` with timestamp and OHLC columns; volume and bid/ask OHLC are optional. Check coverage with:

```bash
python tools/dukascopy_coverage_gate.py --source-root <ISOLATED_DUKASCOPY_ROOT>
```

Candidate ingestion, training, and activation are external pre-deployment operations. Use the numbered scripts only with physically separate candidate roots described in [Ops Entrypoints](../docs/agents/ops-entrypoints.md#isolated-training-and-activation):

```bat
ops\windows\10_ingest_all.bat
ops\windows\13_train_all.bat
ops\windows\14_activate_models.bat
```

Do not train or activate into production-owned artifact, registry, manifest, data, or state roots.

For a one-time provider-partition migration, preview `fx-quant-stack/scripts/migrate_provider_partitions.py --help` before applying it to an isolated copy.

## Validation boundary

Offline causal research uses `tools/run_causal_walk_forward.py` with immutable point-in-time inputs and delayed fills. Actual runtime validation uses the exact installed candidate on an external isolated host or VM with broker emission disabled and zero emitted entry commands. Neither path has production database, credentials, broker, registry-write, activation, or writable-mount authority.

See [Causal Research and Runtime Validation](../docs/agents/causal-research-and-runtime-validation.md) and the [External Full-Scale Validation Runbook](../docs/FULL_SCALE_E2E_RUNBOOK.md).

## Architecture

- Runtime and live policy: `src/fxstack/runtime`, `src/fxstack/live`
- Bridge API: `src/fxstack/api`
- Features, data, and model contracts: `src/fxstack/features`, `src/fxstack/io`, `src/fxstack/models`
- Training and release evidence: `src/fxstack/training`, `src/fxstack/mlops`
- Portfolio and risk: `src/fxstack/portfolio`, `src/fxstack/risk`
- Offline evaluation: `src/fxstack/backtest`, `src/fxstack/rl`

Start repository navigation at [AGENTS.md](../AGENTS.md) and the [agent docs index](../docs/agents/README.md).
