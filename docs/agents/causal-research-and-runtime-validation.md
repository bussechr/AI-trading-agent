# Causal Research And Runtime Validation

## Primary Files
- [run_causal_walk_forward.py](../../tools/run_causal_walk_forward.py)
- [build_walk_forward_snapshot.py](../../tools/build_walk_forward_snapshot.py)
- [fxstack_causal_research_backtest.py](../../tools/fxstack_causal_research_backtest.py)
- [loop.py](../../fx-quant-stack/src/fxstack/improve/loop.py)
- [runner.py](../../fx-quant-stack/src/fxstack/runtime/runner.py)
- [24_start_candidate_stack.bat](../../ops/windows/24_start_candidate_stack.bat) (production-host quarantine stub)

## Separation Of Responsibilities
- causal research studies strategy economics against immutable point-in-time inputs
- the production runtime owns live decision, lifecycle, risk, command, and persistence behavior
- an isolated candidate/shadow deployment of the actual runtime validates software behavior
- research output is advisory evidence and cannot activate models, write the runtime registry, submit broker commands, or claim runtime parity

## Physical Isolation Contract
- run causal research on a separate host or VM; a separate locked-down OS account and unmounted production roots is the minimum fallback
- copy an immutable input bundle into the research environment; never mount production data, artifact, registry, database, credential, log, or broker paths writable
- do not provide bridge URLs, API keys, database credentials, MT4 access, execution-provider access, active manifests, or registry-write authority
- deny network routes to production and apply CPU, memory, disk, and GPU limits so research cannot contend with the live stack
- write outputs only beneath a disposable research root; transfer hashed evidence bundles into a quarantined review location through an explicit operator action
- build research manifests with an explicit bundle root; manifest, registry, artifact, and symlink-resolved paths must remain beneath that root, and manifest artifact references contain no alternate external path or URI
- production startup, readiness, monitoring, shutdown, and command processing must remain independent of all research processes and artifacts

## Self-Improvement Evidence

- `fxstack.improve` is offline research and emits best-config, summary, reflection-memory, and proposal evidence files only
- production Windows operations contain no self-correction launcher, and the operator plane cannot start the loop
- the former continuous supervisor and experiment-factory bridge are removed; improve code cannot import `RuntimeService` or upsert the runtime database
- proposal evidence is marked research-only, cannot authorize activation or runtime registration, and requires independent candidate-runtime validation
- any transfer from a disposable research root into a quarantined review location is an explicit, integrity-checked operator action

## Causal Research Path
- build physically truncated training inputs at `TRAIN_END`, including label knowledge-time truncation
- train against isolated raw, feature, label, artifact, and registry roots with ingestion disabled
- build a separate replay raw snapshot truncated at `TEST_END`
- execute delayed fills with `fill_delay_bars>=1` and `future_data_access=forbidden`
- lifecycle economic replay must use the production-equivalent partial-close cap/cooldown and mandatory closed-bar ATR SL/TP geometry. It evaluates bid OHLC for long exits and ask OHLC for short exits, chooses stop loss on an ambiguous bar, and records broker stop/target counts; an unlimited-partial or forced-final-close approximation is not promotion evidence
- require each `point_in_time_audit.json` and the run-level `causal_walk_forward_summary.json` to pass before interpreting economic results
- never treat a causal-integrity pass as an economic or promotion pass

## Software Validation Path
- validate candidate artifacts only on the external causal-research host; the installed production package carries neither a simulated/paper adapter nor the candidate-stack launcher
- prove startup, feature freshness, model identity, position lifecycle, portfolio/risk gates, command state transitions, persistence, and restart behavior there
- follow with the actual runtime in live-data shadow mode with broker emission disabled
- require explicit activation and canary controls before any live-capital change

## Release Evidence Boundary
- causal-walk-forward and self-improvement outputs remain advisory even when their economics pass; they cannot satisfy the activation economic gate
- binding economic evidence must come from a completed, executed independent Lean or Nautilus harness and is normalized only after its manifest, report bytes, pair, bundle ID, model-set ID, active-manifest SHA-256, and artifact-set SHA-256 all match. The offline Nautilus adapter consumes a self-contained content-inventoried bundle, denies outbound sockets and external paths, calls the production scorer for causal OOS rows, and preserves hashed engine/order/fill/position ledgers for every differentiated stress scenario. Planned manifests and zero-fill engine runs remain advisory failures
- economic sufficiency requires finite metrics, positive realized PnL, at least one executed trade, positive turnover, base and worst-stress drawdown below 25%, at least one deterministic stress scenario, and worst-stress PnL above the Phase 5 floor
- binding runtime evidence comes from the actual candidate runtime on the external isolated validation host or VM in shadow posture, proves manifest/DB/loaded-runtime consistency, feature/runtime readiness, and loaded exit/reversal lifecycle models, and requires broker emission to remain disabled with zero emitted entry commands
- the fast and 24-hour observations require at least 900 and 86,400 seconds respectively, healthy runtime samples rather than manufactured BUY/SELL traffic, and distinct files and run windows; a short, duplicated, or entry-emitting shadow artifact fails the boundary
- Phase 5 stores hashes for the candidate manifest and every declared support, economic, and runtime artifact. Release-package reloads recompute gates from those bytes, so a legacy gate boolean or a later file edit cannot authorize canary start
- evidence binding never signs, activates, or starts a canary; those remain distinct operator-controlled transitions after the exact-candidate gates pass

## Ownership Direction
- [adaptive_policy.py](../../fx-quant-stack/src/fxstack/strategy/adaptive_policy.py) is production-owned strategy code
- production packages do not import `fxstack.backtest`, research scripts, or research artifact types
- the research backtest may consume a copied immutable build of production-owned strategy/model code inside the isolated research environment
- no research component calls `/v2/*`, opens a runtime database, reads a live registry, or participates in operator-plane control

## Related Docs
- [runtime-loop.md](runtime-loop.md)
- [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md)
- [ops-entrypoints.md](ops-entrypoints.md)
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
