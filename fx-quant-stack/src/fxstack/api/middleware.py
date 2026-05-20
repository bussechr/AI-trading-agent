"""HTTP middleware shared across bridge endpoints.

Provides:

* :func:`add_request_id_middleware` — stamps every request with a correlation
  ID, propagates it onto ``request.state.request_id`` and into a contextvar so
  loggers can pick it up, and echoes it on the response.
* :func:`configure_structured_logging` — installs a stream handler on the
  ``fxstack`` package logger that includes the current request id (via
  :class:`RequestIdFilter`) on every log line. Idempotent.
"""

from __future__ import annotations

import contextvars
import logging
import uuid

from fastapi import FastAPI, Request, Response

logger = logging.getLogger(__name__)

#: Header name used by clients to pass a correlation ID. If absent, the
#: middleware generates one.
REQUEST_ID_HEADER = "X-Request-ID"

#: Contextvar that downstream code (loggers, services) reads to attach the
#: current request ID to log lines and stored records.
current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "fxstack_request_id", default=None
)


class RequestIdFilter(logging.Filter):
    """Logging filter that injects the current request id onto every record.

    The format string ``%(request_id)s`` then renders the id (or ``-`` when no
    request is in flight). This is what makes one trade traceable across every
    log line emitted by the bridge.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        rid = current_request_id.get()
        record.request_id = rid or "-"
        return True


def configure_structured_logging(level: int = logging.INFO) -> None:
    """Install a request-id-aware stream handler on the ``fxstack`` logger.

    Idempotent: re-invoking is a no-op once the handler is in place. We attach
    to the ``fxstack`` package logger rather than the root logger so we do not
    clobber any handlers the operator may have configured elsewhere
    (e.g. dictConfig in a service supervisor). ``propagate=False`` prevents
    duplicate lines via the root logger.
    """
    pkg_logger = logging.getLogger("fxstack")
    for h in pkg_logger.handlers:
        if getattr(h, "_fxstack_structured", False):
            return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [%(name)s] [rid=%(request_id)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.addFilter(RequestIdFilter())
    handler._fxstack_structured = True  # type: ignore[attr-defined]
    pkg_logger.addHandler(handler)
    pkg_logger.setLevel(level)
    pkg_logger.propagate = False


def _generate_request_id() -> str:
    return uuid.uuid4().hex


def add_request_id_middleware(app: FastAPI) -> None:
    """Register middleware that ensures every request carries an ``X-Request-ID``.

    Behavior
    --------
    * If the inbound request already has ``X-Request-ID``, it is preserved
      (trimmed to 64 chars to bound storage).
    * Otherwise a fresh uuid4 hex is generated.
    * The ID is set on ``request.state.request_id`` and exposed via
      ``current_request_id`` for downstream code.
    * The response always includes ``X-Request-ID``.
    """

    @app.middleware("http")
    async def _request_id(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        rid = incoming[:64] if incoming else _generate_request_id()
        request.state.request_id = rid
        token = current_request_id.set(rid)
        try:
            response = await call_next(request)
        finally:
            current_request_id.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        return response
