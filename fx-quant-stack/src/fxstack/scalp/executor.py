# AGENT: ROLE: Client half of scalp live execution -- builds protected orders from sized intents and submits them to the server-side scalp authority.
# AGENT: ENTRYPOINT: `LiveExecutor.submit`.
# AGENT: PRIMARY INPUTS: SizedIntent, scalp config sha, bridge URL/key.
# AGENT: PRIMARY OUTPUTS: (accepted, reason, response) -- every refusal is ledger-recorded by the caller.
# AGENT: HANDSHAKES: POST /v2/scalp/commands -> fxstack/runtime/service.py submit_scalp_command -> fxstack/scalp/authority.py chain -> command outbox -> EA.
"""Scalp live executor (client side).

This module can only PROPOSE: the bridge's scalp authority chain disposes.
Every submission carries the exact scalp config sha so the server can bind it
to the arming certificate; a client with a drifted config is refused. The
shadow book keeps running in parallel in live mode -- live fills are
reconciled against shadow expectations, and the divergence (slippage, fill
quality) is itself ledger evidence.

Idempotency: command_id = scalp:<symbol>:<signal minute>. A crash-resubmit of
the same intent maps to the same outbox row; the outbox and the EA ACK fence
already guarantee at-most-once broker execution.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.sizing import SizedIntent


class LiveExecutor:
    def __init__(self, config: ScalpConfig, *, config_sha256: str) -> None:
        self.base = config.bridge_url.rstrip("/")
        self.key = config.api_key()
        self.config_sha256 = str(config_sha256)
        self.data_root = str(config.data_root)
        self.submitted = 0
        self.refused = 0
        self.errors = 0

    def submit(self, sized: SizedIntent) -> tuple[bool, str, dict[str, Any]]:
        """Submit one protected order; (accepted, reason, raw response)."""
        intent = sized.intent
        if not sized.sizeable or sized.lots <= 0.0:
            return False, f"unsizeable:{sized.reason or 'no_lots'}", {}
        payload = {
            "cmd": intent.side,
            "symbol": intent.symbol,
            "lots": float(sized.lots),
            "sl_price": float(intent.sl_price),
            "tp_price": float(intent.tp_price),
            "command_id": f"scalp:{intent.symbol}:{int(intent.minute_epoch)}",
            "intent": "scalp_live_entry",
            "scalp_config_sha256": self.config_sha256,
            "scalp_data_root": self.data_root,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/v2/scalp/commands",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"}
            | ({"X-API-Key": self.key} if self.key else {}),
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                out = json.loads(resp.read().decode("utf-8"))
                self.submitted += 1
                return True, "", dict(out)
        except urllib.error.HTTPError as exc:
            self.refused += 1
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001 -- refusal detail is best-effort
                detail = {}
            reason = str(detail.get("error") or f"http_{exc.code}")
            return False, reason, dict(detail)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            self.errors += 1
            return False, "bridge_unreachable", {}
