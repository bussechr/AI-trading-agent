# IG MT4 Setup

This guide wires an operator-managed IG MetaTrader 4 terminal to the active `fxstack` runtime on Windows. Keep MT4 login numbers, account references, broker server names, passwords, API keys, and command tokens out of this repository.

## 1. Install and sign in

1. Install IG MetaTrader 4 from IG's official download.
2. Sign in interactively with the intended demo or live account.
3. Confirm the account and server in the visible terminal before enabling AutoTrading.
4. If MT4 is installed outside the normal IG location, set `FXSTACK_MT4_TERMINAL_EXE` to the absolute path of `terminal.exe` in the operator environment.

Use placeholders such as `<YOUR_MT4_LOGIN>` and `<YOUR_IG_SERVER>` in private notes. Never commit real identifiers.

## 2. Resolve the bridge endpoint

From the repository root:

```bat
launch_all.bat endpoints
```

This performs bind checks, persists the selected loopback endpoints in the ignored runtime state, and starts no service. The preferred bridge URL is `http://127.0.0.1:58710`, but the resolver may select another free port. Use the URL printed by this command rather than assuming the default.

In MT4, open **Tools > Options > Expert Advisors**, enable WebRequest for the resolved bridge origin, and keep the allowlist limited to loopback.

## 3. Deploy the bridge EA

Stop MT4 before deployment, then run:

```bat
ops\windows\24_deploy_bridge_ea.bat
```

The deployer copies the BridgeEA source and the generated bridge credentials into the terminal's MQL4 data area. The API key and command token are distinct and remain in ignored files; the EA inputs stay empty so secrets are not written to the MT4 journal.

Restart MT4, compile `BridgeEA.mq4` if needed, attach it to the intended chart, and enable AutoTrading. Confirm the EA and Journal tabs show the expected endpoint and no authentication, WebRequest, or DLL errors.

## 4. Start and observe

The canonical launcher reuses an already-running configured terminal or starts it visibly:

```bat
launch_all.bat live 10000
```

`launch_all.bat live` is not an authority bypass. It refuses before starting or replacing the stack unless the selected strategy has valid signed release authority and the configured live posture, scopes, account-mode expectation, and fresh broker attestation all agree. The default staged-safe posture is shadow-only.

Use the authenticated dashboard or the packaged monitor instead of unauthenticated `curl` commands:

```bat
ops\windows\23_start_monitor.bat --run
```

## 5. Stop the stack

```bat
launch_all.bat stop
```

Shutdown disables execution egress, quarantines queued commands, and stops only repository-owned services. It intentionally leaves MT4 open so the operator can inspect account, chart, and EA state.

## Safety checks

- Start with an IG demo account and verify the visible account/server before every run.
- Do not put credentials or account identifiers in `.env`, Markdown, batch files, screenshots, or committed logs.
- Do not disable bridge authentication outside an isolated local development session.
- Do not treat offline research or a passing backtest as runtime or release authority.
- Full candidate validation runs on an external isolated host or VM with broker emission disabled; the production host runs one baseline stack only.

For the authoritative startup and shutdown contracts, see [Windows Ops Entrypoints](agents/ops-entrypoints.md). For the isolation boundary, see [Causal Research and Runtime Validation](agents/causal-research-and-runtime-validation.md).
