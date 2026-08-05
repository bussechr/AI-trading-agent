# MTVCLC v5 Successor and Runtime Binding

This page records the pre-launch v5 boundary: before the first v5 publication,
the successor was code-ready but unstarted, no v5 `T0` or 180-day window had
begun, no eligible observation or outcome had been collected, and no evidence
or runtime-release key had been accessed. Once a v5 artifact exists, its exact
content-addressed bytes, capture receipt, immutable guard, and owned Scheduled
Task state are authoritative for operational status; this pre-launch paragraph
must not be used to infer that the attempt is still unstarted.

The failed v4 attempt is counted exactly once. Its preregistration body is `45f6ca5ccb23195a8253dedfc4aef1b0e95f319d35c55558aa706bfa513952bd`, artifact is `7755d0e5facb7b5f28d6f5a092355e7b33d1d8d790b64bd59d204e220a5c7e55`, and failure record is `56f23f4298f010bf105246ed611ba9cdd47d81e8c55131324d8a0aa6a8e3ac78`. The recorded reason is exactly `unresolved_first_cycle_reservation_after_process_interruption`; it does not prove a timeout or any stronger cause. It emitted zero eligible observations and zero manifest entries. The successor accounting is `4830 + 44 = 4874` attempted cells.

The active chain is:

- `tools/seal_mt4_tick_volume_preregistration_resilient_v5.py`
- `tools/capture_ig_mt4_m1_activity_resilient_v4.py`
- `tools/verify_mt4_tick_volume_capture_handoff_v5.py`
- `tools/evaluate_mt4_tick_volume_post_window_v5.py`
- `tools/mtvclc_validation_release_v5.py`
- `fxstack.runtime.mtvclc_validation_evidence_v3`
- `tools/mtvclc_runtime_release.py`
- `fxstack.runtime.mtvclc_runtime_release`

The collector retains one fsynced first-cycle reservation before every GET. Only an otherwise valid quote snapshot with a required timestamp before `T0` may retry at a fixed one-second cadence under that same reservation. Source drift and non-time-invalid data fail immediately. The qualifying snapshot and bar response must finish within the sealed 30-second start-edge deadline, and every committed cycle part carries the readiness proof.

The trade contract is immediate market execution only: BUY uses the authenticated ask and SELL uses the authenticated bid. Pending orders and pending trades are both forbidden. The preregistration grants no runtime or broker authority.

Gap-v3 wire filenames remain for compatibility, but v5 supervision and preservation require only `collector-guard.identity.gap-v5.v1.json`, schema `fxstack.mtvclc_collector_guard_identity.gap_v5.v1`, and preservation schema `fxstack.scalp.mtvclc_preservation_filenames.v5`. Parent `mtvclc_prereg_sealed_runtime_bound_v5` cannot be mixed with a gap-v3 guard leaf.

After evidence-v3 is signed, `tools/mtvclc_runtime_release.py prepare` and `issue` both require the exact content-addressed v5 preregistration. The outer-v2 certificate signs the full preregistration inside `validated_preregistration_binding`, matches its body and pretty-published artifact hashes to signed evidence, matches the entire sealed engine identity to the installed engine, and independently checks the exact immediate-market, scope, accounting, window, and failed-v4 lineage semantics before private-key access or runtime admission.
