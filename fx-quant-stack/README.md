# fx-quant-stack

Next-generation FX strategy stack (nested v2 rebuild) with:

- Dukascopy CSV-first research data ingestion
- HMM regime model + XGBoost swing/intraday/meta stack
- Custom triple-barrier labels + purged temporal validation
- FastAPI runtime preserving MT4 bridge `/v2/*` compatibility
- Postgres runtime persistence

## Quick Start

```bash
cd fx-quant-stack
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

## CLI

```bash
python -m src.trader.cli bridge serve
python -m src.trader.cli runtime run --equity 10000 --sleep 10

python -m src.trader.cli data ingest --pair EURUSD --granularity M5 --source-root fx-quant-stack/data/dukascopy
python -m src.trader.cli features build --pair EURUSD --timeframe M5
python -m src.trader.cli labels build --pair EURUSD --timeframe M5
python -m src.trader.cli train regime --pair EURUSD --timeframe H4
python -m src.trader.cli train swing --pair EURUSD --timeframe D
python -m src.trader.cli train intraday --pair EURUSD --timeframe M5
python -m src.trader.cli train swing-transformer --pair EURUSD --timeframe D
python -m src.trader.cli train intraday-tcn --pair EURUSD --timeframe M5
python -m src.trader.cli train deep-stale
python -m src.trader.cli train meta --pair EURUSD --timeframe M5
python -m src.trader.cli train all --pair EURUSD
python -m src.trader.cli live score --pair EURUSD --timeframe M5
python -m src.trader.cli db migrate
python -m src.trader.cli db verify
python -m src.trader.cli models activate --require-all
python -m src.trader.cli stack preflight
python -m src.trader.cli stack gpu-check
```

## Dukascopy CSV Layout

- Default source root: `fx-quant-stack/data/dukascopy`
- Default filename pattern: `{pair}_{granularity}.csv`
- Example file: `fx-quant-stack/data/dukascopy/EURUSD_M5.csv`
- Required columns: timestamp + OHLC (+ optional volume); bid/ask OHLC is optional.

## One-time Provider Migration

If you already have parquet data under `provider=oanda`, migrate it once:

```bash
python -m src.trader.cli data migrate-provider --store-root fx-quant-stack/data/raw --apply
python -m src.trader.cli data migrate-provider --store-root fx-quant-stack/data/features --apply
python -m src.trader.cli data migrate-provider --store-root fx-quant-stack/data/labels --apply
```

## Status

This project is the active v2 strategy/runtime stack for bridge + execution.
