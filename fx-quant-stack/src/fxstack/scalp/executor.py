# AGENT: ROLE: Disabled compatibility client retained for protocol tests; the server always refuses scalp live ingress.
# AGENT: ENTRYPOINT: `LiveExecutor.submit`.
# AGENT: PRIMARY INPUTS: SizedIntent, scalp config sha, bridge URL/key.
# AGENT: PRIMARY OUTPUTS: (accepted, reason, response) -- every refusal is ledger-recorded by the caller.
# AGENT: HANDSHAKES: POST /v2/scalp/commands -> constant fail-closed refusal; no command outbox access.
"""Disabled scalp live-executor compatibility client.

The shadow loop no longer imports or constructs this client. Calls retained by
older tooling receive ``scalp_live_ingress_disabled_unvalidated_authority``
from the bridge and cannot reach the command outbox.

Idempotency: command_id = scalp:<symbol>:<signal minute>. A crash-resubmit of
the same intent maps to the same outbox row; the outbox and the EA ACK fence
already guarantee at-most-once broker execution.
"""

from __future__ import annotations

from typing import Any

from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.sizing import SizedIntent


class LiveExecutor:
    def __init__(self, config: ScalpConfig, *, config_sha256: str) -> None:
        del config, config_sha256
        self.submitted = 0
        self.refused = 0
        self.errors = 0

    def submit(self, sized: SizedIntent) -> tuple[bool, str, dict[str, Any]]:
        """Refuse locally; no compatibility caller may touch the bridge."""
        del sized
        self.refused += 1
        return False, "scalp_live_ingress_disabled_unvalidated_authority", {}
