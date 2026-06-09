# Security Policy

## Reporting a Vulnerability

Do not open a public issue for secrets, account identifiers, broker details, exploitable runtime behavior, or anything that could expose a live trading account.

Report security concerns privately through GitHub Security Advisories if available on this repository. If advisories are not available, contact the repository owner privately and include only the minimum detail needed to reproduce the issue.

Please do not include:

- Live or demo account numbers
- Broker server names tied to a real account
- API keys, bridge keys, passwords, tokens, private keys, or `.env` contents
- Full runtime logs unless they have been scrubbed
- Screenshots showing account identifiers, balances, tickets, or broker account metadata

## Supported Use

This project is research software. It is not a hosted service and it is not financial advice. Users are responsible for securing their own machine, broker account, MT4 installation, environment variables, and network access.

## Secret Handling

Keep all credentials out of Git:

- Use a local `.env` file or environment variables.
- Keep `.env`, `.secrets/`, logs, databases, runtime artifacts, and compiled outputs ignored.
- Rotate any credential that was ever committed, posted in an issue, pasted into a chat, or shared in a log.

## Trading Safety

Run on a demo account first. Use conservative lot sizes, verify bridge authentication, verify MT4 AutoTrading behavior, and understand the risk controls before any live use.
