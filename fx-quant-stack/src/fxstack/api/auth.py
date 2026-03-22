"""Optional API-key authentication middleware for the fxstack bridge."""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

# Paths that bypass authentication (health checks, ping).
_PUBLIC_PATHS: frozenset[str] = frozenset({"/v2/ping", "/v2/health"})


def add_api_key_middleware(app: FastAPI, api_key: str) -> None:
    """Register middleware that enforces ``X-API-Key`` on non-public routes.

    If *api_key* is empty the middleware is **not** registered, leaving the
    API open (opt-in authentication).
    """
    key = str(api_key or "").strip()
    if not key:
        return  # Auth disabled — no middleware added.

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
