# MTVCLC Runtime-Bound Prospective Path v4

## Status

This page records the failed v4 predecessor and the design as it existed before
that attempt. A v4 preregistration was published and the first collection cycle
was interrupted with the exact recorded reason
`unresolved_first_cycle_reservation_after_process_interruption`. The attempt
emitted zero eligible observations and zero manifest entries. Its
preregistration body SHA-256 was
`45f6ca5ccb23195a8253dedfc4aef1b0e95f319d35c55558aa706bfa513952bd`, its
published artifact SHA-256 was
`7755d0e5facb7b5f28d6f5a092355e7b33d1d8d790b64bd59d204e220a5c7e55`, and
its failure-record SHA-256 was
`56f23f4298f010bf105246ed611ba9cdd47d81e8c55131324d8a0aa6a8e3ac78`.
Nothing stronger about the interruption cause is established. The active
successor boundary is [mtvclc-preregistration-v5.md](mtvclc-preregistration-v5.md);
its content-addressed artifact and owned task state, when present, are the
authoritative current status. The commands and future-tense workflow below are
retained only to reproduce the v4 design and must not be used to start or
resume collection.

## Primary Files

- [seal_mt4_tick_volume_preregistration_resilient_v4.py](../../tools/seal_mt4_tick_volume_preregistration_resilient_v4.py)
- [verify_mt4_tick_volume_capture_handoff_v4.py](../../tools/verify_mt4_tick_volume_capture_handoff_v4.py)
- [evaluate_mt4_tick_volume_post_window_v4.py](../../tools/evaluate_mt4_tick_volume_post_window_v4.py)
- [mtvclc_validation_release_v4.py](../../tools/mtvclc_validation_release_v4.py)
- [mtvclc.py](../../fx-quant-stack/src/fxstack/strategy/mtvclc.py)

## Why v4 Exists

The gap-v3 sealer's production-runtime context was built through the legacy
`fxstack.strategy.scalp_dislocation` API. The active runtime now evaluates the
pure MTVCLC policy in `fxstack.strategy.mtvclc`. A future experiment must bind
that active evaluator rather than inherit the stale family identity.

The v4 sealer descriptor-reads the complete v3 sealer template, applies five
counted literal transforms, and compiles the derived source without workspace
bytecode. The transforms select the v4 handoff, evaluator, and release files;
select `fxstack.strategy.mtvclc`; and provide only the three legacy names that
the old base-sealer API needs. That compatibility adapter takes its strategy
ID, version, and config hash from the exact MTVCLC source image. It does not
load `scalp_dislocation.py`. The raw v3 template, v4 wrapper, derived source,
and every downstream template are all SHA-256 bound in the declaration.
The inherited publication guard also snapshots all three local IG source
documents cited by the fee attestation, binds them to the nested declaration
identities, and rechecks their exact bytes before and after publication.

## Fixed Experiment Contract

The ordered symbol scope is exactly:

`EURUSD, USDJPY, AUDUSD, GBPUSD, USDCAD, USDCHF, EURGBP, EURJPY, NZDUSD, AUDJPY, CADJPY, CHFJPY, EURAUD, EURCAD, EURCHF, GBPCAD, GBPCHF, GBPJPY, BTCUSD, ETHUSD, AUDCAD, NZDJPY`.

There are exactly 44 ordered symbol/side cells, one BUY and one SELL cell for
each symbol. XRP is absent. Attempt accounting remains 4,786 prior cells plus
44 new cells, or a 4,830-cell family. Every fixed cell and panel gate must
pass; a failed cell is a failed experiment.

The window remains half-open and prospective: `[T0, T0 + 180 consecutive
days)`. The end cannot be shortened for apparent success, extended, restarted,
or moved after publication. Interim signal, outcome, or performance evaluation
is forbidden. The v4 handoff refuses before the sealed end and requires the
canonical post-window finalization receipt before it can emit an inventory.

The frozen execution model is an immediate market BUY at authenticated ask or
SELL at authenticated bid. Pending orders are forbidden. The preregistration,
collector, handoff, evaluator, and evidence ceremony grant no runtime, broker,
trade, activation, registry-write, issuer, signature, or success-claim
authority.

## Collector Compatibility And Restart Persistence

V4 intentionally retains the content-pinned `gap_v3_source_pinned` collector
wire profile and its content-addressed preregistration filename. This is not a
reuse of any old observation or T0. It preserves the already audited
reservation-before-GET tail WAL, first-authenticated-finalized-observation
wins rule, immutable T0, gap chain, source-drift refusal, writer lock,
restart-resume rules, and post-window finalization contract without editing the
collector or changing its sealed source hash.

On Windows, a durable operator start of this inherited gap-v3 wire uses
`ops/windows/29_register_mtvclc_collector_resilient_watchdog_v2.ps1 -Action
Start` only after the exact task has been separately previewed and installed.
That action refuses task/source/tuple drift, proves zero writers plus an
unlocked open window, delegates the restart adapter to Task Scheduler, and
returns success only after direct guard Health proves one exact locked writer
twice after the adapter task has finished successfully and the task definition
is unchanged. This fixes process ownership across an
interactive shell or tool timeout only. It does not alter, relax, or prove the
sealed T0 first-cycle timing contract; start-edge timing remains a separate
preregistration and collector admission requirement. The task has an unlimited
execution time, while the operator-facing ownership observation is bounded to
60 seconds by default (30 seconds minimum) and never kills a task or guard on
timeout. Start the exact installed task well before T0; do not use that bounded
ownership confirmation as evidence that the first post-T0 cycle is eligible.

The additional top-level `preregistration_tool_revision` and
`runtime_policy_binding` distinguish the v4 declaration. The collector accepts
those authority-free fields while still enforcing every gap-v3 collection
invariant. The v4 handoff revalidates the complete v4 envelope before invoking
the unchanged streaming collection audit.

## Offline Handoff, Evaluation, And Release

The v4 handoff is network-free and performs no outcome projection. It
revalidates the active MTVCLC binding, all executable source identities, the
complete manifest/chunk/gap chains, the reservation-aware tail WAL, the start
receipt, and the post-window finalization receipt.

The v4 evaluator is a source-bound adapter from that handoff to the unchanged
v3 screen and ledger arithmetic. It cannot run early and adds no live surface.
The v4 release adapter binds the key-last public ceremony to the v4 handoff and
evaluator. Importing it never reads a private key. A future issue operation can
reach an explicitly supplied key path only after the sealed window and only
after all public artifacts and fixed gates pass again.

## Isolation

Collection is GET-only and is a separate future operator action. Handoff,
evaluation, and release validation run on the physically isolated research
host with no production database, bridge URL, broker route, credential,
registry write, activation, runtime-control, or live filesystem mount. Only
immutable, content-addressed artifacts cross the boundary.

Creating these v4 tools is not permission to publish a declaration. Before any
future seal, the final source set, deployed BridgeEA source and EX4 identities,
authenticated IG-DEMO cost bytes, fee attestation, destination, free-space
policy, and operator timing must be rechecked. Publication must finish at least
ten minutes before a future minute-aligned T0.
