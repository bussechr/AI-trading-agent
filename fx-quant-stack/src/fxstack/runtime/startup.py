"""Runtime startup helpers extracted from ``fxstack.runtime.runner``.

Carved out of the 9k-line runner module to provide a clean, testable surface
for the startup phase. Re-exported back into ``runner`` so existing callers
that do ``runtime_runner._startup_log(...)`` continue to work.

What lives here:

* ``startup_log`` — terse stdout logger keyed with ``[runtime-startup]`` so
  ops can grep it out of the live log without pulling in structured logging.
* ``perform_startup_bridge_checks`` — probe the local bridge at boot:
  protocol-version handshake + DB-vs-EA position reconcile. Availability and
  reconcile failures are warnings, but an invalid or incompatible wire
  protocol is fatal so the runtime cannot submit commands across an unknown
  contract.

What does NOT live here yet:

* Model-set loading, activation reconciliation, dry-run inference. Those are
  also in ``runner.py`` (see ``_load_model_sets``, ``_seed_active_model_sets_from_manifest``,
  ``_startup_inference_dry_run``) and follow the same pattern; pull them
  here when there's a parity test ready to back the move.
"""

from __future__ import annotations

import json as _json
import urllib.error
import urllib.request
from typing import Any

from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION


class BridgeProtocolMismatchError(RuntimeError):
    """Raised when runtime and bridge wire protocols cannot interoperate."""


def _protocol_version_tuple(raw: Any) -> tuple[int, int, int] | None:
    text = str(raw or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    text = text.split("+", 1)[0].split("-", 1)[0]
    parts = text.split(".")
    if len(parts) != 3 or any(not item.isdigit() for item in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def startup_log(message: str) -> None:
    """Emit a startup-phase log line.

    Intentionally just ``print``: structured logging hasn't been initialized
    yet at the points this runs, and ops want a stable grep prefix.
    """
    print(f"[runtime-startup] {str(message)}", flush=True)


def perform_startup_bridge_checks(settings: Any) -> None:
    """Best-effort probe of the local bridge at runtime boot.

    Runs two non-blocking calls:

    1. ``GET /v2/handshake`` (public, no auth) to verify the bridge wire
       protocol version matches what the runtime was built against. Major
       drift, malformed compatibility metadata, or a server minimum newer than
       the runtime is fatal; compatible minor/patch drift logs a warning.
    2. ``GET /v2/positions/reconcile`` to surface any divergence between the
       broker's open positions and the bridge's DB view. Auth header is sent
       if a key is configured. Divergence is WARN, not fatal.

    Both calls have a 5-second timeout. If the handshake fails, the reconcile
    call is skipped — piling timeouts on an already-noisy launch is worse
    than missing one log line.
    """
    bridge_url = str(getattr(settings, "mt4_bridge_url", "") or "").rstrip("/")
    if not bridge_url:
        startup_log("bridge checks: skipping (mt4_bridge_url empty)")
        return

    api_key = str(getattr(settings, "bridge_api_key", "") or "").strip()
    timeout_secs = 5.0

    bridge_alive = False
    handshake: dict[str, Any] = {}
    try:
        req = urllib.request.Request(f"{bridge_url}/v2/handshake")
        with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
            decoded = _json.loads(resp.read().decode("utf-8") or "{}")
        if not isinstance(decoded, dict):
            raise ValueError("handshake response must be a JSON object")
        handshake = dict(decoded)
        bridge_alive = True
    except urllib.error.URLError as exc:
        startup_log(f"WARN bridge handshake unreachable: {exc!s}")
    except Exception as exc:
        startup_log(f"WARN bridge handshake call failed: {exc!s}")

    if not bridge_alive:
        return

    bridge_version = str(handshake.get("protocol_version") or "").strip()
    min_compatible = str(handshake.get("min_compatible") or "").strip()
    expected_parts = _protocol_version_tuple(BRIDGE_PROTOCOL_VERSION)
    bridge_parts = _protocol_version_tuple(bridge_version)
    minimum_parts = _protocol_version_tuple(min_compatible) if min_compatible else None
    if expected_parts is None:  # pragma: no cover - constant is covered by API tests
        raise RuntimeError(f"invalid runtime bridge protocol constant: {BRIDGE_PROTOCOL_VERSION}")
    if bridge_parts is None:
        message = (
            f"bridge handshake protocol missing or malformed: {bridge_version!r}; "
            f"runtime expects {BRIDGE_PROTOCOL_VERSION}"
        )
        startup_log(f"FATAL {message}")
        raise BridgeProtocolMismatchError(message)
    if min_compatible and minimum_parts is None:
        message = f"bridge minimum compatible version missing or malformed: {min_compatible!r}"
        startup_log(f"FATAL {message}")
        raise BridgeProtocolMismatchError(message)
    if bridge_parts[0] != expected_parts[0]:
        message = (
            f"bridge protocol major mismatch: runtime expects {BRIDGE_PROTOCOL_VERSION} "
            f"but bridge reports {bridge_version}"
        )
        startup_log(f"FATAL {message}")
        raise BridgeProtocolMismatchError(message)
    if minimum_parts is not None and expected_parts < minimum_parts:
        message = (
            f"runtime protocol {BRIDGE_PROTOCOL_VERSION} is older than bridge minimum "
            f"compatible version {min_compatible}"
        )
        startup_log(f"FATAL {message}")
        raise BridgeProtocolMismatchError(message)
    if bridge_version != BRIDGE_PROTOCOL_VERSION:
        startup_log(
            f"WARN compatible bridge handshake drift: runtime expects {BRIDGE_PROTOCOL_VERSION} "
            f"but bridge reports {bridge_version}"
        )
    else:
        startup_log(f"bridge handshake OK protocol={bridge_version}")

    try:
        req = urllib.request.Request(f"{bridge_url}/v2/positions/reconcile")
        if api_key:
            req.add_header("X-API-Key", api_key)
        with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
            recon = _json.loads(resp.read().decode("utf-8") or "{}")
        only_db = list(recon.get("only_in_db") or [])
        only_ea = list(recon.get("only_in_ea") or [])
        lot_mismatches = list(recon.get("lot_mismatches") or [])
        ea_available = bool(recon.get("ea_snapshot_available", False))
        if only_db or only_ea or lot_mismatches:
            startup_log(
                f"WARN position reconcile divergence: only_db={only_db} "
                f"only_ea={only_ea} lot_mismatches={lot_mismatches}"
            )
        elif not ea_available:
            startup_log(
                "position reconcile: ea_snapshot_available=false (EA may not yet "
                "be emitting positions_snapshot reports — recompile/redeploy EA)"
            )
        else:
            startup_log("position reconcile OK")
    except urllib.error.HTTPError as exc:
        startup_log(f"WARN position reconcile HTTP error: {exc.code} {exc.reason}")
    except Exception as exc:
        startup_log(f"WARN position reconcile call failed: {exc!s}")


__all__ = ["BridgeProtocolMismatchError", "startup_log", "perform_startup_bridge_checks"]
