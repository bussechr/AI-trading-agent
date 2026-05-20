"""Bridge wire-protocol constants and metadata schemas.

This module is the single source of truth for the version that clients of the
bridge (the MT4 EA, the dashboard, the runtime, ops scripts) verify against.
Bump the version when the bridge wire format changes:

* **Patch** (``v2.1.0`` → ``v2.1.1``): backward-compatible internal change.
* **Minor** (``v2.1.x`` → ``v2.2.0``): backward-compatible additive change
  (new optional field, new endpoint).
* **Major** (``v2.x.x`` → ``v3.0.0``): breaking change. Clients must update.

Every client should call ``GET /v2/handshake`` on startup and compare
``protocol_version`` against the constant it was built with. A major mismatch
should be fatal; a minor or patch mismatch can be warning-only.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --- Version constants ------------------------------------------------------

#: Current bridge wire protocol version. Major.Minor.Patch.
BRIDGE_PROTOCOL_VERSION: str = "v2.1.0"

#: Minimum protocol version this server can interoperate with (clients older
#: than this should refuse to talk to the bridge).
BRIDGE_PROTOCOL_MIN_COMPATIBLE: str = "v2.0.0"


def _build_revision() -> str:
    """Best-effort identifier for the running build (commit sha or 'dev')."""
    return (
        os.environ.get("FXSTACK_BUILD_REVISION")
        or os.environ.get("GIT_SHA")
        or os.environ.get("VCS_REF")
        or "dev"
    ).strip() or "dev"


# --- Schemas ----------------------------------------------------------------


class HandshakeResponse(BaseModel):
    """Body of ``GET /v2/handshake``.

    Clients verify ``protocol_version`` against the version they were compiled /
    deployed against. ``min_compatible`` lets the server tell older clients
    they will be refused.
    """

    model_config = ConfigDict(extra="allow")

    protocol_version: str = Field(default=BRIDGE_PROTOCOL_VERSION)
    min_compatible: str = Field(default=BRIDGE_PROTOCOL_MIN_COMPATIBLE)
    server: str = Field(default="fxstack-bridge")
    build: str = Field(default_factory=_build_revision)
    auth_required: bool = Field(default=True)
    public_paths: list[str] = Field(default_factory=list)


class BridgeError(BaseModel):
    """A single error detail emitted by the bridge."""

    model_config = ConfigDict(extra="allow")

    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2048)
    request_id: str | None = Field(default=None, max_length=64)
    detail: dict[str, Any] | None = Field(default=None)


class BridgeErrorEnvelope(BaseModel):
    """Top-level error response shape returned by every error path."""

    model_config = ConfigDict(extra="forbid")

    error: BridgeError
