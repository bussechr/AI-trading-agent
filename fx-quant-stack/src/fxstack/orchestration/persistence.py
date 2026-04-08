"""High-level orchestration persistence helpers."""

from __future__ import annotations

from typing import Any

from fxstack.orchestration.contracts import AgentTrace, DecisionContext, DecisionPacket


def persist_orchestration_artifacts(
    *,
    service: Any,
    context: DecisionContext,
    packet: DecisionPacket,
    trace: AgentTrace,
    checkpoint_json: dict[str, Any],
    runtime_mode: str,
    fallback_used: bool,
) -> None:
    trace_payload = dict(trace.model_dump(mode="json"))
    trace_payload.pop("checkpoint", None)
    service.store_orchestration_bundle(
        context=context.model_dump(mode="json"),
        packet=packet.model_dump(mode="json"),
        trace=trace_payload,
        runtime_mode=str(runtime_mode),
        fallback_used=bool(fallback_used),
    )
