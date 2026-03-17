# Execution Blocker Baseline (2026-03-13)

## Scope
- Source trace: `data/state/audit/strategy_trace.jsonl`
- Interop trace: `data/state/audit/interop/compute_trace.jsonl`
- Snapshot generated: 2026-03-13 (UTC)

## Topline
- Total strategy rows: `9269`
- Candidate rows: `4634`
- Execution rows: `4634`
- State rows: `1`
- Executed execution rows (`outcome in {executed,sent}`): `0`

## Dominant Rejection Reasons
- Candidate rejections:
  - `low_score`: `4630`
  - `spread`: `4`
- Execution rejections:
  - `exec_low_score_ratio`: `4364`
  - `startup_warmup`: `267`
  - `exec_low_confidence`: `3`

## Score-Collapse Signal
- Candidate rows with `score_raw == 0.0`: `3202 / 4634` (`69.09%`)
- First candidate zero-score cycle: `317`
- First candidate zero-score timestamp (UTC): `2026-03-13T00:36:19.210114+00:00`

## Startup Timeline
- Initial state reset reason: `major_gap_361h`
- State reset cycle/timestamp (UTC): cycle `0` at `2026-03-12T18:04:47.990818+00:00`
- Warmup cleared first observed at candidate cycle `268`
- Warmup clear timestamp (UTC): `2026-03-13T00:27:58.223794+00:00`
- Starvation mode first observed at candidate cycle `286`
- Starvation activation timestamp (UTC): `2026-03-13T00:31:01.839776+00:00`

## Interop / Transport Evidence
- `data/state/audit/interop` contains only `compute_trace.jsonl` (line count: `4634`)
- `transport_trace.jsonl` does not exist
- Observed `py_signal_post` rows: `0` (no transport trace events present)

## Compute-Trace Aggregate Rejections
- `soft_low_score`: `4634`
- `exec_low_score_ratio`: `4364`
- `soft_low_predictive_sharpe`: `3235`
- `startup_warmup`: `267`
- `soft_spread`: `4`
- `exec_low_confidence`: `3`

## Baseline Conclusion
- Trade flow is blocked pre-send at execution quality gating (`exec_low_score_ratio`) after startup warmup.
- No evidence of MT4 post/send transport activity in the captured interop audit files.
