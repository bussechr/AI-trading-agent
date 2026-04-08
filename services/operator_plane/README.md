# Operator Plane

This directory contains the optional Phase 5 supervisory plane.

It is intentionally outside the live runtime path:
- no broker credentials
- no queue authority
- no execution adapter imports
- no `/v2/commands`

Subservices:
- `openclaw/`: supervisory workflow bindings and flow policy
- `mcp_runtime_state/`: read-only runtime inspection server
- `mcp_twin_artefacts/`: read-only replay artifact inspection server
- `mcp_release_registry/`: read-only registry and release inspection server
