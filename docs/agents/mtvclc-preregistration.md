# Historical MTVCLC V1 Prospective Preregistration (v3)

## Historical Status

This page records the frozen v3 predecessor and its gap-v3 experiment. The
active successor boundary is documented in
[mtvclc-preregistration-v5.md](mtvclc-preregistration-v5.md) and uses
`tools/seal_mt4_tick_volume_preregistration_resilient_v5.py`. The v3 sealer,
collector, handoff, evaluator, release, and public verifier remain pinned
historical templates for reproducibility only; neither they nor the
unversioned, resilient-v1, or resilient-v2 paths later on this page may start
a new attempt.

## Recorded v3 Boundary

`tools/seal_mt4_tick_volume_preregistration_resilient_v3.py` sealed the
corrected `ig_mt4_tick_volume_close_location_continuation` / `mtvclc.v1`
gap-v3 hypothesis before any eligible observation or outcome was inspected. It
read only local files and published one content-addressed, no-overwrite JSON
file. It had no network, credential, database, broker, issuer, registry,
runtime-control, activation, or order capability. Every authority bit was
false.

The artifact binds:

- the exact ordered scope-v3 22 symbols and 44 BUY/SELL cells;
- strategy, version, config, source-contract, and activity-metric identities
  from the pure screen module;
- every local executable source needed by the v3 sealer, corrected screen,
  GET-only collector, handoff, evaluator, release, public verifier, and their
  local support modules;
- the authenticated IG-DEMO p90-spread JSON/NPZ bytes;
- an operator fee attestation and local frozen copies of all three cited IG
  product-detail sources;
- the exact five-record abandoned lineage, 4,786 prior cells, 44 current
  cells, and the 4,830 cumulative lower bound.

The seal also carries the body/file hashes and refusal reason for abandoned
pre-T0 or zero-observation preregistrations. Those incidents add zero attempted
cells only when no eligible observation, manifest entry, signal, outcome, or
performance statistic was emitted; they remain visible rather than being
silently discarded.

Source files are read through stable no-follow descriptors and executed from
the same bytes without workspace pyc. Every active input is snapshot-bound,
including the three local IG product-detail documents cited by the fee
attestation, and is rechecked before and after namespace publication; detected
post-link drift removes the candidate artifact. The production runtime
identity is context only. MTVCLC is not integrated, activated, or authorized
by the preregistration.

The stopped d80e6cc9 attempt remains a failed operational attempt: it emitted
eligible observations, but no signals, outcomes, performance statistics, or
success claim were evaluated. Its 44 cells are therefore retained in lineage.
`tools/seal_mt4_tick_volume_preregistration_resilient.py` creates a new,
independent window and fixes the replacement family at 4,698 prior cells plus
44 current cells, or a 4,742-cell lower bound. It binds the replacement screen,
restart-resilient collector, preserved support sources, exact failure evidence,
and the stricter one-sided Wilson allocation over all 4,742 cells. It never
restarts or extends the stopped window and grants no authority.

The resulting `07b78ce6...` replacement also stopped before evaluation. After
an MT4 restart, the API exposed a previously absent finalized bar behind a
durable per-symbol watermark. The v1 integrity policy correctly refused to
insert or overwrite that late row, but made the harmless backfill permanently
fatal to collection. The three emitted manifest entries and the complete
failure identity remain immutable; no signal, outcome, or performance data was
read. Those 44 cells are counted as another attempted family.

The watermark-v2 successor was body `95306e016d8af312...`, artifact
SHA-256 `21d2f17464067a7e...`, sealed at `2026-08-03T16:48:02Z`, with the
half-open window `[2026-08-03T17:04:00Z, 2027-01-30T17:04:00Z)`. It fixes
4,742 prior cells plus 44 current cells, or a 4,786-cell lower bound, and uses
the one-sided Wilson allocation over all 4,786 attempts. Every authority bit
remained false. That attempt is now an abandoned crossed-T0 attempt because
eligible observations existed before the deployed BridgeEA source changed.
Its 44 cells remain in multiplicity accounting and its old window, paths, and
tasks must not be restarted, extended, or reused.

No active gap-v3 operator tuple is declared by this document. A future run
requires a newly published v3 preregistration with a distinct future T0 and a
separately authorized collector start. Sealing never launches MT4, the bridge,
the collector, the scalp runtime, or any Scheduled Task.

## Withheld auxiliary multiplicity proposal

The historical d80e-bound capture is not eligible for an auxiliary
multiplicity-control seal. A terminal restart was followed by revised
overlapping finalized bars, so exact continuation refused fail closed. The
existing capture, its rows, and any outcomes must not be used to seed, repair,
or justify an auxiliary experiment.

`tools/seal_mtvclc_auxiliary_multiplicity_preregistration.py` is therefore a
review-only replacement-design template despite its historical filename. It
can build an in-memory description with `build_template_payload`, but
`build_preregistration` always refuses with
`d80e_auxiliary_seal_withheld_replacement_primary_required`,
`validate_preregistration` always returns false, `atomic_publish` refuses
before resolving any path, and the CLI exits with status 2. No auxiliary
artifact should exist from this tool.

The template preserves the proposed statistical design for review: one exact
primary plus 4,697 deterministic causal negative-control portfolios, neutral
hash-derived column order, 150 declared 24-hour rows, exact immediate-market
trade/cost/missing-data rules, all-column uniqueness and matrix-rank gates,
252 balanced ten-block CSCV comparisons with PBO at most 0.40, and a
4,698-trial DSR of at least 0.95. Pending order instructions remain forbidden.
The proposed `2026-09-02T13:30:00Z` to `2027-01-30T13:30:00Z` dates and d80e
hashes are recorded only as a withheld proposal; they are not a live or future
preregistration.

A replacement requires a newly sealed primary with a future T0 and clean
capture start, explicit new auxiliary dates selected before observation,
regeneration of the master seed, all 4,698 policy identities and column order,
and rebinding of every screen, collector, scope, cost, fee, statistical, and
sealer source hash. The replacement must receive independent code and
statistical review. None of the 4,654 earlier attempted cells has an extant
complete return column, so this design does not reconstruct historical PBO,
DSR, policy lineage, or negative controls. Every authority bit remains false.

## Fixed prospective experiment

The v3 sealer chooses a minute-aligned `T0` after the configured delay
(15 minutes by default) and refuses publication unless durable visibility is
proved at least ten minutes before T0.
The window is the half-open interval `[T0, T0 + 180 days)`. Observations outside
that interval are forbidden. Signal/outcome evaluation, performance
statistics, early success, optional extension, and restart-after-failure are
forbidden until the full window closes. Operational data-quality monitoring
must not calculate signals or performance.

The GET-only collector treats MT4 `MODE_TIME` as an opaque broker-server event
identity and stores only its SHA-256. It is not a UTC clock and may reflect the
broker's timezone or DST step. Quote freshness and ordering use authenticated
bridge receipt timestamps plus the bridge-assigned monotonic market-event
metadata. That event sequence is API-process-local and may reset; the fixed
cross-restart continuity clock is the hash-chained transport-observation
sequence. Same-preregistration/same-source collector resume is allowed only as
continuation of the original experiment, with every observed gap retained; it
does not reset T0 or the experiment.

The v3 collector requires the sealed preregistration as an input, verifies its
body/artifact hashes, exact five-record lineage, collector contract, and
producer-source identities, records that binding in every chunk and manifest
entry, waits without bridge access before T0, and stops with a timeout-derived
safety margin before the exclusive end. Before every cycle's first GET it
fsyncs one authenticated reservation into the independent tail WAL. After the
window, its explicit network-free finalizer resolves any interrupted
reservation and emits the receipt required by the v3 handoff. No free duration
can extend the sealed interval.

All 44 cells must independently satisfy the screen's existing fixed gates:
at least 30 trades, at least 10 independent UTC days, a family-adjusted
one-sided Wilson lower bound strictly above that symbol's exact
conversion-adjusted break-even probability (0.75 only when conversion does not
apply), and positive conversion-adjusted mean net bps. The complete panel
additionally requires at least 300 trades and 60 independent UTC days. There
is at most one immediate market entry per symbol per UTC day; pending orders
are forbidden. The fixed rollover entry blackout is `[20:20,22:10)` UTC.

## Fee attestation

The sealer refuses missing, unknown, unhashed, future-dated, or silently zero
fees. The fee JSON uses schema
`fxstack.scalp.mtvclc_fee_attestation.v1`, exact scope order, `demo` account
mode, the explicitly attested three-letter account currency, no source errors,
and these exact top-level fields:

```text
schema_version, venue_id, account_mode, account_currency, scope_version,
symbol_scope, effective_at_utc, attested_at_utc, source_errors,
source_documents, symbols, operator_attestation_sha256
```

`source_documents` contains exactly one row for each role below. `path` is a
relative path beneath the fee JSON's directory and `sha256` covers those local
frozen bytes.

```text
ig_mt4_forex_product_details
ig_mt4_crypto_product_details
ig_spread_betting_cfd_product_details
```

Each of the 22 symbol rows contains:

```text
commission_bps_per_round_trip, commission_status, commission_source_role,
financing_bps_per_trade, financing_status, financing_source_role,
profit_loss_currency, conversion_rate_of_absolute_profit_or_loss,
conversion_status, conversion_source_role
```

Zero commission is accepted only as `explicit_source_attested`. Zero
financing is accepted only as
`structurally_avoided_by_fixed_rollover_guard`; otherwise a non-negative
conservative debit is required. The conversion rate must be at least 0.005.
When the instrument P/L currency differs from the attested account currency,
final net P/L debits that rate multiplied by the absolute gross quote-bps for
both wins and losses. Conversion is never credited. It is zero only when the
currencies are equal.

`operator_attestation_sha256` is canonical SHA-256 of the JSON object with
that field omitted. It is an integrity commitment, not a signature or trading
authority.

## Seal and isolate

Save the cited source pages and attestation outside the repository, then run:

```powershell
python tools/seal_mt4_tick_volume_preregistration_resilient_v3.py `
  --cost-capture-json D:\path\ig_mt4_bid_ask_capture.json `
  --cost-capture-npz D:\path\ig_mt4_bid_ask_samples.npz `
  --fee-attestation D:\path\mtvclc_fee_attestation.json `
  --bridge-ea-deployed-source D:\path\deployed\BridgeEA.mq4 `
  --bridge-ea-deployed-ex4 D:\path\deployed\BridgeEA.ex4 `
  --output-root D:\quarantine\mtvclc-gap-v3-prereg
```

This command only seals. It does not start the collector or any trading
process. A separately authorized collection must use the exact v3 collector
and its versioned-v2 continuity/guard/watchdog family:

- `tools/capture_ig_mt4_m1_activity_resilient_v3.py`
- `tools/check_mt4_tick_volume_collector_continuity_resilient_v2.py`
- `ops/windows/27_guard_mtvclc_collector_resilient_v2.ps1`
- `ops/windows/29_ensure_mtvclc_collector_resilient_v2.ps1`
- `ops/windows/29_register_mtvclc_collector_resilient_watchdog_v2.ps1`

Preview/Health is mutation-free. Any start, task installation, or task removal
is a separate operator-authorized action and must use the exact newly sealed
tuple; an old preregistration, capture root, or task identity is not reusable.

### Historical guard profiles (reference only)

The unversioned and resilient-v1 examples below describe preserved earlier
attempts. They are not the active gap-v3 entrypoints and must not be launched.

#### Guarded Windows continuity

On Windows, use `ops/windows/26_guard_mtvclc_collector.ps1` as the supported
supervision boundary around that exact command. The wrapper has three actions:

- `Health` performs no bridge request and no write. It validates the sealed
  preregistration/current collector-source identity, the first and last
  manifest records and their chunks, process ancestry, exact CLI policy, guard
  identity, exclusive supervisor lock, and manifest age. A Python launcher and
  its child interpreter count as one writer group. Omitted optional collector
  arguments are accepted only at their exact collector defaults.
- `AdoptRunning` requires exactly one matching writer group, publishes the
  immutable guard identity, acquires the output-scoped exclusive writer lock,
  and stays beside that process until it exits. It neither restarts nor signals
  the collector, so an already-running pre-guard collection can be adopted
  without a data interruption.
- `StartOrResume` refuses any existing writer for that output, acquires the
  same exclusive lock, validates or exclusively initializes the immutable guard
  identity, then runs the hash-bound collector with the fixed 2-second quote,
  60-second bar, 400-bar, five-second HTTP-timeout, and source-rollover-refuse
  policy. It passes only `--api-key-file`; the key value is never placed on the
  command line, stored in the guard identity, or read by the health helper.

Example read-only health check:

```powershell
ops/windows/26_guard_mtvclc_collector.ps1 `
  -Action Health `
  -PythonExe D:\path\python.exe `
  -Preregistration D:\quarantine\mtvclc-prereg\mtvclc_v1_preregistration_HASH.json `
  -OutputDir D:\quarantine\mtvclc-prospective-capture `
  -ApiKeyFile D:\path\bridge_api_key.txt
```

For a process that was started before the guard existed, replace `Health` with
`AdoptRunning` in a durable operator session. For a stopped process or after a
Windows restart, use `StartOrResume` with the same paths. The collector
reconstructs its sequence from the existing hash chain; the original sealed T0
is never reset, and downtime remains an observable gap. Neither action computes
signals, outcomes, or performance.

This script deliberately does not create a Scheduled Task, service, or startup
entry and does not automatically relaunch a failed child. An operator-owned
process supervisor may invoke the same `StartOrResume` command after reboot,
but installing that external policy is a separate authorized operation.

The health helper performs a bounded first/last-record check so routine
monitoring does not reread the growing capture. The hash-bound collector still
performs its authoritative full manifest-and-chunk audit on every actual
restart. Because the current two-second layout can grow to millions of chunks,
late-window recovery can be slow and memory-intensive; the wrapper cannot
remove that cost without changing the preregistered collector source.

#### Versioned restart-resilient replacement collector

New replacement captures use
`tools/capture_ig_mt4_m1_activity_resilient.py`; the preserved v3 collector
and its existing capture are not rewritten or resumed under this profile. The
replacement remains GET-only and collection-only over the exact ordered 22
symbols. Its sealed integrity contract fixes the two-second quote cadence,
60-second finalized-bar cadence, 400-bar request, five-second HTTP timeout,
same-source-only continuation, original T0, and all-false research, runtime,
activation, and trade authority.

The first authenticated finalized observation—or absence—at a durable
per-symbol watermark wins across a same-source restart. Every later bar at or
behind that watermark is skipped without overwrite or insertion, whether it
matches, is revised, or fills an earlier gap. Only strictly newer monotonic
rows advance the watermark. Any source-ID rollover, torn active-journal record,
or collector-source drift refuses. Downtime and late-history gaps remain
observable and never reset or extend the sealed window.

The first cycle is finalized immediately. Later cycles are fsynced into one
hash-chained `active-hour.journal.sha256.jsonl` and compacted to one immutable
portable chunk per UTC-hour segment. The manifest is replaced atomically, and
the collector holds `collector-data-writer.lock` for its full run. This avoids
the old per-cycle file explosion while retaining the existing portable
chunk/manifest field contracts.

On Windows, `ops/windows/27_guard_mtvclc_collector_resilient.ps1` and
`tools/check_mt4_tick_volume_collector_continuity_resilient.py` are the paired
supervision boundary. Health is bounded and read-only; adoption/start binds an
immutable guard identity to the replacement collector and support hashes,
integrity contract, exact config, preregistration, external output root, API-key
file path, original window, and exclusive data-writer lock. Neither component
reads the key value, contacts the bridge during health, evaluates results, or
grants trading authority.

Start or inspect the replacement collector only through that guard. It can be
launched before T0; the collector performs no bridge GET before T0:

```powershell
ops/windows/27_guard_mtvclc_collector_resilient.ps1 `
  -Action StartOrResume `
  -PythonExe D:\path\python.exe `
  -Preregistration D:\quarantine\mtvclc-prereg-resilient\mtvclc_v1_preregistration_HASH.json `
  -OutputDir D:\quarantine\mtvclc-capture-resilient `
  -ApiKeyFile D:\path\bridge_api_key.txt
```

Use the same arguments with `-Action Health` for a bounded read-only check.

For restart persistence, keep the guard as the only collector supervisor and
place the non-pinned fail-closed adapter around it. The adapter performs a fresh
`Health` first and invokes the same `StartOrResume` tuple only when that report
proves zero writers, `collector_writer_absent`, an absent or available
supervisor lock, an exact stopped-before-T0 or stopped-during-window state, and
an open sealed window. It refuses stale activity, a live or mismatched writer,
duplicate writers, a held/unknown lock, malformed health output, and a closed
window:

```powershell
ops/windows/29_ensure_mtvclc_collector_resilient.ps1 `
  -PythonExe D:\path\python.exe `
  -Preregistration D:\quarantine\mtvclc-prereg-resilient\mtvclc_v1_preregistration_HASH.json `
  -OutputDir D:\quarantine\mtvclc-capture-resilient `
  -ApiKeyFile D:\path\bridge_api_key.txt
```

After the absent-writer proof, the adapter launches the guard hidden/detached,
requires a bounded follow-up `Health` to prove one locked writer, then exits.
This lets later task triggers continue to monitor health. A stale-but-existing
writer is reported nonzero and is never killed or restarted automatically.

`ops/windows/29_register_mtvclc_collector_resilient_watchdog.ps1` can preview
or install the exact command. The default is a non-elevated current-user
`AtLogOn` trigger (matching interactive MT4) plus a five-minute repeating
trigger; `-TriggerMode AtStartup` is an explicit administrator-only option.
`IgnoreNew` prevents overlapping task instances. The task action pins the
watchdog and guard source hashes, refuses an existing task rather than
overwriting it, verifies ownership before `-Action Remove`, and does not start
or stop the collector during task mutation. Registration is a separate
operator-authorized system mutation; `-Action Preview` and tests do not install
the task.

The output root must already exist, remain outside the repository and all
input roots, and must not be an issuer or production-runtime path. Publication
uses a complete temporary file plus an atomic hard-link create, refuses an
existing content-addressed name, verifies the bytes, and marks the result
read-only.

## Post-window isolated evaluation

After the exclusive sealed end, stop the collector and explicitly finalize the
capture on the collection host. This mode constructs no client and performs no
network request:

```powershell
python tools/capture_ig_mt4_m1_activity_resilient_v3.py `
  --finalize-after-window `
  --preregistration D:\quarantine\mtvclc-gap-v3-prereg\mtvclc_gap_v3_preregistration_HASH.json `
  --output-dir D:\quarantine\mtvclc-gap-v3-capture `
  --bridge-ea-repository-source D:\repo\MQL4\Experts\BridgeEA.mq4 `
  --bridge-ea-deployed-source D:\path\deployed\BridgeEA.mq4 `
  --bridge-ea-deployed-ex4 D:\path\deployed\BridgeEA.ex4
```

Transfer the sealed preregistration and complete finalized capture tree to the
physically isolated research host. Run the handoff verifier first against the
stopped capture:

```powershell
python tools/verify_mt4_tick_volume_capture_handoff_v3.py `
  --preregistration D:\isolated\mtvclc-prereg\mtvclc_gap_v3_preregistration_HASH.json `
  --capture-root D:\isolated\mtvclc-capture `
  --output-root D:\isolated\mtvclc-handoff
```

Make the stopped capture snapshot read-only, keep staging and output physically
separate from it, then run the exact post-window evaluator:

```powershell
python tools/evaluate_mt4_tick_volume_post_window_v3.py `
  --preregistration D:\isolated\mtvclc-prereg\mtvclc_gap_v3_preregistration_HASH.json `
  --handoff D:\isolated\mtvclc-handoff\mtvclc_capture_handoff_v2_HASH.json `
  --capture-root D:\isolated\mtvclc-capture `
  --staging-root D:\isolated\mtvclc-staging `
  --output-root D:\isolated\mtvclc-evaluation
```

The handoff requires `post-window-finalization-receipt.v1.json`; the collector
creates it only through explicit `--finalize-after-window` operation after
end-exclusive, without constructing a client or performing a network request.
The evaluator refuses before the end without opening the capture. After the
end, it executes the exact screen, handoff, and legacy-support bytes that it
hashes, re-verifies the complete handoff, proves the writer locks are free,
rejects an active journal or unresolved cycle reservation, and fences the
read-only manifest, chunks, finalization receipt, and replacement guard
identity against mutation. It streams a deterministic projection into
per-symbol binary spools, rebuilds costs only from sealed rows, and rechecks the
entire handoff and snapshot before outcomes and again before publication. It
verifies all 44 fixed cells, at least 300 total trades, and at least 60
independent UTC days. The immediate trade model is BUY at ask or SELL at bid;
pending orders are forbidden.

The corrected replacement-v3 screen allocates its family-wise interval over
4,830 attempted cells. The content-addressed bundle contains the complete
screen result, a concise gate summary, the spool inventory/files, and hashes
binding the preregistration, handoff, manifest, screen source,
handoff-verifier source, and evaluator source. Passing research gates still
grants no signature, activation, runtime, broker, or trading authority.
Failure is published as
failure and is never retried, converted to success, or used to extend/restart
the sealed experiment.

At the two-second sealed cadence, plan for about 171 million quote rows. The
current deterministic quote record is 72 bytes, roughly 12.3 GB total before
bar spools, headers, and working headroom. Projection and evaluation are
streamed through bounded per-symbol files; the isolated host does not load the
complete 22-symbol capture into RAM.

Transfer the sealed artifact to the physically isolated research host only by
an explicit operator action. Recompute and compare the printed artifact-file
SHA-256 and the embedded preregistration-body SHA-256, then make the isolated
copy read-only. Do not mount production data, bridge/API access, database,
credentials, registry, issuer key, or activation paths. The collector run and
all later outcome evaluation are separate actions; this sealer performs
neither.
