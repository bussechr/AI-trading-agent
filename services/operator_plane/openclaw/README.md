# OpenClaw Operator Plane

This service binds OpenClaw-style supervisory flows to a staging-only drafting and release-preparation workspace.

Session classes:
- `operator-write-staging`: staging-only drafting, release-prep, and PR creation

Flows:
- `draft_experiment`
- `collect_approval_pack`
- `open_pr`
- `prepare_paper_pack`

Hard boundaries:
- no broker credentials
- no bridge write surface
- no queue authority
- no production workspace write scope
- no production-repository mount
- no offline orchestration replay or causal-research flow
- no research artifact, capture, configuration, database, credential, or output-root input

Runtime and release inspection remain available through the separate read-only MCP services. OpenClaw accepts operator-authored payloads, reads only its own staged proposal/approval files, and never invokes research tooling.
