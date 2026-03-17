# Trade Smoothness Runbook

## Scope
- Strategy: FX EL Hawkes (EURUSD, H1)
- Objective: higher throughput with stable execution quality
- Risk envelope: max 12% drawdown, 3% daily loss breaker

## Staged Rollout
1. Stage A (24h): diagnostics only
- Keep execution behavior unchanged.
- Ensure `audit_trace_enabled` and `interop_audit_enabled` are both `true`.
- Promotion gate: complete 24h candidate/execution/ack traces.

2. Stage B (48h demo canary): hardening active at reduced risk
- Enable startup warmup + starvation relax + bridge safety degrade.
- Run at 25% risk scale externally or lower `risk_per_trade_pct`.
- Promotion gate: `ack_timeout_rate_5m < 1%`, no duplicate fills, no bridge queue backlog.

3. Stage C (72h demo full-risk): target profile
- Use configured risk envelope (`risk_per_trade_pct: 0.018`, gov hard DD 12%).
- Promotion gate: execution count >= 2x baseline, drawdown <= 12%.

4. Stage D (live canary 24h, then full)
- First 24h at half-size.
- Auto rollback to `close_only` on critical QoS or drawdown events.

## Live Monitoring

### Yellow Alerts
- `timeouts.ack_timeout_rate_5m > 0.02`
- Sustained `suppression_ratio_rolling > 0.85`
- `startup_warmup` active longer than expected window

Action:
- Verify MT4 polling health and bridge queue.
- Check dominant rejection reason and spread/cost conditions.

### Red Alerts
- `timeouts.ack_timeout_rate_5m > 0.05`
- `queue.pending_oldest_secs > 60` with `queue.pending_count > 50`
- Drawdown above `gov_soft_dd_pct`

Action:
- System auto-degrades to `close_only` for configured cycles.
- Confirm queue drains and ACK flow recovers before returning to full live.

### Critical Alerts
- Drawdown >= `gov_hard_dd_pct` (12%)
- Daily drawdown >= `daily_loss_breaker_pct` (3%)

Action:
- New entries paused.
- Keep exits/risk management live.
- Resume only after defined recovery criteria.

## Key Metrics
- `timeouts.ack_timeout_rate_5m`
- `queue.pending_oldest_secs`
- `execution.order_send_failed_rate`
- `dedup.duplicate_suppression_rate`
- `suppression_ratio_rolling`
- `dominant_rejection_reason`
- `warmup_mode`
- `model_age_secs`

## Operational Checks
1. Bridge health:
- `GET /v2/health`, `GET /v2/metrics`
2. Signal lifecycle:
- `queued -> delivered -> acked/failed` continuity
3. State synchronization:
- No stale pending entries beyond TTL
- Position snapshots update in `/v2/state`
4. Warmup behavior after major restart gap:
- Entries blocked until warmup exits
- Model reset + degraded flag observed in diagnostics
