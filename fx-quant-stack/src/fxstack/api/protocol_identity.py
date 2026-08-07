"""Dependency-free bridge protocol identity shared by runtime and API code."""

from __future__ import annotations

import os


# Keep wire-format identity separate from Pydantic request/response schemas so
# runtime clients can validate a handshake without importing the API model stack.
BRIDGE_PROTOCOL_VERSION: str = "v3.0.0"
BRIDGE_PROTOCOL_MIN_COMPATIBLE: str = "v3.0.0"


def build_revision() -> str:
    """Return the best available build identifier, or ``dev``."""

    return (
        os.environ.get("FXSTACK_BUILD_REVISION")
        or os.environ.get("GIT_SHA")
        or os.environ.get("VCS_REF")
        or "dev"
    ).strip() or "dev"
