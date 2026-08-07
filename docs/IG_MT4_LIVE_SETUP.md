# IG MT4 Live Setup

The former account-specific live setup has been retired. Use the credential-free [IG MT4 Setup](IG_MT4_SETUP.md) and the authoritative [Windows Ops Entrypoints](agents/ops-entrypoints.md).

There is no shortcut from a configured terminal to live execution. Every live launch must pass the current signed-release, explicit live-mode, arming, scope, expected-account-mode, broker-attestation, risk, and execution-egress gates. MTVCLC admission is IG-DEMO-only and cannot authorize a real account.

Never store an MT4 login, IG account reference, server name, password, bridge credential, or command token in repository files.
