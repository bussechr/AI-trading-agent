# OpenClaw Operator Plane

This service binds OpenClaw-style supervisory flows to the existing FXStack replay and release tooling.

Session classes:
- `operator-read`: read-only runtime, artefact, and registry inspection
- `operator-write-staging`: staging-only drafting, release-prep, and PR creation

Flows:
- `replay_window`
- `analyse_divergence`
- `draft_experiment`
- `collect_approval_pack`
- `open_pr`
- `prepare_paper_pack`

Hard boundaries:
- no broker credentials
- no bridge write surface
- no queue authority
- no production workspace write scope
