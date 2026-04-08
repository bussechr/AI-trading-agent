"""Best-effort OpenTelemetry helpers for the Phase 1 orchestration bus."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode


_METER = metrics.get_meter("fxstack.orchestration")
_TRACER = trace.get_tracer("fxstack.orchestration")
_RUNS_TOTAL = _METER.create_counter("fxstack_orchestration_runs_total")
_PERSISTENCE_FAILURES_TOTAL = _METER.create_counter("fxstack_orchestration_persistence_failures_total")
_FALLBACK_TOTAL = _METER.create_counter("fxstack_orchestration_fallback_total")
_RUN_LATENCY_MS = _METER.create_histogram("fxstack_orchestration_run_latency_ms")


def _otel_attributes(attrs: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = dict(attrs or {})
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if isinstance(value, (bool, int, float, str)):
            clean[str(key)] = value
        else:
            clean[str(key)] = str(value)
    return clean


@contextmanager
def start_span(name: str, *, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    with _TRACER.start_as_current_span(name) as span:
        for key, value in _otel_attributes(attributes).items():
            span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def record_run(*, latency_ms: int, attributes: dict[str, Any] | None = None, fallback_used: bool = False) -> None:
    attrs = _otel_attributes(attributes)
    _RUNS_TOTAL.add(1, attrs)
    _RUN_LATENCY_MS.record(float(latency_ms), attrs)
    if fallback_used:
        _FALLBACK_TOTAL.add(1, attrs)


def record_persistence_failure(*, attributes: dict[str, Any] | None = None) -> None:
    _PERSISTENCE_FAILURES_TOTAL.add(1, _otel_attributes(attributes))
