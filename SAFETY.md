# Safety Guide

## Public-Repo Hygiene

Never commit:

- `.env` files or copied environment variables
- MT4 account numbers, broker server names tied to an account, investor passwords, or screenshots showing account metadata
- Bridge API keys, API tokens, private keys, SSH keys, database passwords, or secret-manager material
- Runtime logs, trade tickets, order history exports, equity curves from live accounts, or account statements
- Local build outputs, databases, model artifacts, cache folders, or generated backtest reports

The repository ignores common local outputs, including `.env`, `.secrets/`, `artifacts/`, `fx-quant-stack/artifacts/`, logs, databases, and build folders.

## Running Safely

Use a demo account first. Confirm the EA, bridge, dashboard, and runtime behave as expected before changing lot sizes or enabling live execution.

Recommended first-run checks:

- Confirm `FXSTACK_BRIDGE_AUTH_REQUIRED=true` outside local development.
- Set `FXSTACK_BRIDGE_API_KEY` locally and keep it out of Git.
- Confirm MT4 WebRequest is limited to the local bridge URL.
- Confirm the broker's minimum lot, lot step, margin requirement, and contract size.
- Keep `FXSTACK_MAX_TOTAL_POSITIONS`, `FXSTACK_MAX_PAIR_POSITIONS`, and lot-size settings conservative.
- Verify every order path on demo before using a live account.

## Self-Improvement Loop Safety

The improvement loop should propose changes only inside the intended allowlist. Deterministic code should evaluate and accept or reject proposals. Do not let generated text, LLM output, or unreviewed artifacts directly control live credentials, broker settings, shell commands, or account-level risk limits.

## Publishing Forks

Before making a fork public, run your own scan for secrets and personal data. A clean latest commit does not automatically clean older Git history.
