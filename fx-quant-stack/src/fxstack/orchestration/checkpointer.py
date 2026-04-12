"""Durable checkpointer helpers for the Phase 1 no-op LangGraph runtime."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver


class DurableCheckpointAdapter:
    """Small wrapper around LangGraph's in-memory saver with JSON-safe exports."""

    def __init__(self) -> None:
        self._saver = InMemorySaver()

    @property
    def saver(self) -> InMemorySaver:
        return self._saver

    def serialize_checkpoint(self, *, thread_id: str, clear: bool = False) -> dict[str, Any]:
        config = {"configurable": {"thread_id": str(thread_id)}}
        checkpoint_tuple = self._saver.get_tuple(config)
        if checkpoint_tuple is None:
            payload = {"thread_id": str(thread_id), "checkpoint": None}
        else:
            payload = {
                "thread_id": str(thread_id),
                "config": dict(checkpoint_tuple.config or {}),
                "checkpoint": dict(checkpoint_tuple.checkpoint or {}),
                "metadata": dict(checkpoint_tuple.metadata or {}),
                "parent_config": dict(checkpoint_tuple.parent_config or {}) if checkpoint_tuple.parent_config else None,
                "pending_writes": list(checkpoint_tuple.pending_writes or []),
            }
        if clear:
            self._saver.delete_thread(str(thread_id))
        return payload
