"""API-key authentication middleware for the fxstack bridge.

When ``required=True`` (the default and the recommended production setting), a
missing/empty key causes a fail-secure middleware to be registered that 503s
every non-public request and emits a critical startup log. Operators set
``FXSTACK_BRIDGE_API_KEY=<secret>`` to enable real auth.

When ``required=False`` (explicit opt-out via ``FXSTACK_BRIDGE_AUTH_REQUIRED=false``,
intended for dev / tests), a missing key leaves the API open with a warning log.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Paths that bypass authentication. Kept minimal: health checks, ping, and
# the protocol handshake (clients need to verify version before they can
# meaningfully authenticate — otherwise a stale key would mask a version
# mismatch behind a 401).
_PUBLIC_PATHS: frozenset[str] = frozenset({"/v2/ping", "/v2/health", "/v2/handshake"})


def add_api_key_middleware(app: FastAPI, api_key: str, required: bool = True) -> None:
    """Register middleware that enforces ``X-API-Key`` on non-public routes.

    Behavior matrix
    ---------------

    +-----------+---------+----------------------------------------------+
    | required  | key set | result                                       |
    +===========+=========+==============================================+
    | True      | yes     | Normal auth: 401 on bad/missing X-API-Key    |
    | True      | no      | Fail-secure: 503 on every non-public path    |
    | False     | yes     | Normal auth (explicit dev/test with key)     |
    | False     | no      | No middleware registered (open API)          |
    +-----------+---------+----------------------------------------------+
    """
    key = (api_key or "").strip()

    if not key:
        if required:
            logger.critical(
                "fxstack bridge auth required but FXSTACK_BRIDGE_API_KEY is empty; "
                "registering fail-secure middleware (all non-public requests will 503). "
                "Set FXSTACK_BRIDGE_API_KEY=<secret> or explicitly disable with "
                "FXSTACK_BRIDGE_AUTH_REQUIRED=false for dev/test."
            )

            @app.middleware("http")
            async def _fail_secure(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
                if request.url.path in _PUBLIC_PATHS:
                    return await call_next(request)
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": (
                            "Bridge auth required but no API key configured. "
                            "Set FXSTACK_BRIDGE_API_KEY or FXSTACK_BRIDGE_AUTH_REQUIRED=false."
                        )
                    },
                )

            return

        logger.warning(
            "fxstack bridge auth disabled (FXSTACK_BRIDGE_AUTH_REQUIRED=false and no key); "
            "bridge is unauthenticated."
        )
        return

    @app.middleware("http")
    async def _check_api_key(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        provided = str(request.headers.get("X-API-Key", "")).strip()
        if provided != key:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )
        return await call_next(request)
