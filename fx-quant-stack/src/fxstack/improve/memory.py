"""Append-only reflection memory for the self-improvement loop.

Every iteration's hypothesis, sanitized change-set, score, and accept/reject
verdict are recorded here. The proposer reads recent entries back so the loop is
*self-correcting*: it stops re-proposing changes that were already rejected and
biases toward directions that improved the objective. This also populates the
``reflection_memory.json`` artifact the Phase-7 experiment factory already expects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REFLECTION_SCHEMA_VERSION = "fxstack.improve.reflection_memory.v1"


@dataclass(slots=True)
class ReflectionEntry:
    iteration: int
    hypothesis: str
    change_set: dict[str, Any]
    sanitized: dict[str, Any]
    objective: float
    accepted: bool
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)
    proposer: str = ""
    model_id: str = ""
    prompt_hash: str = ""
    ts: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "iteration": int(self.iteration),
            "hypothesis": str(self.hypothesis),
            "change_set": dict(self.change_set),
            "sanitized": dict(self.sanitized),
            "objective": float(self.objective),
            "accepted": bool(self.accepted),
            "reason": str(self.reason),
            "metrics": dict(self.metrics),
            "proposer": str(self.proposer),
            "model_id": str(self.model_id),
            "prompt_hash": str(self.prompt_hash),
            "ts": str(self.ts),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReflectionEntry":
        data = dict(payload or {})
        return cls(
            iteration=int(data.get("iteration", 0) or 0),
            hypothesis=str(data.get("hypothesis", "")),
            change_set=dict(data.get("change_set") or {}),
            sanitized=dict(data.get("sanitized") or {}),
            objective=float(data.get("objective", 0.0) or 0.0),
            accepted=bool(data.get("accepted", False)),
            reason=str(data.get("reason", "")),
            metrics=dict(data.get("metrics") or {}),
            proposer=str(data.get("proposer", "")),
            model_id=str(data.get("model_id", "")),
            prompt_hash=str(data.get("prompt_hash", "")),
            ts=str(data.get("ts", "")),
        )


class ReflectionMemory:
    """JSONL-backed memory. In-memory only when ``path`` is None."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._entries: list[ReflectionEntry] = []
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        assert self._path is not None
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self._entries.append(ReflectionEntry.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

    def append(self, entry: ReflectionEntry) -> ReflectionEntry:
        self._entries.append(entry)
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.as_dict(), sort_keys=True) + "\n")
        return entry

    def entries(self) -> list[ReflectionEntry]:
        return list(self._entries)

    def recent(self, n: int = 6) -> list[ReflectionEntry]:
        return list(self._entries[-max(0, int(n)):])

    def accepted(self) -> list[ReflectionEntry]:
        return [e for e in self._entries if e.accepted]

    def best(self) -> ReflectionEntry | None:
        accepted = self.accepted()
        if not accepted:
            return None
        return max(accepted, key=lambda e: e.objective)

    def tried_signatures(self) -> set[str]:
        """Stable signatures of every change-set already evaluated."""

        return {change_set_signature(e.sanitized or e.change_set) for e in self._entries}

    def summary(self) -> dict[str, Any]:
        best = self.best()
        return {
            "iterations": len(self._entries),
            "accepted": len(self.accepted()),
            "best_objective": float(best.objective) if best else None,
            "best_change_set": dict(best.sanitized) if best else {},
        }

    def to_reflection_payload(self, *, experiment_id: str, updated_at: str) -> dict[str, Any]:
        """Render the Phase-7 ``reflection_memory.json`` envelope."""

        return {
            "schema_version": REFLECTION_SCHEMA_VERSION,
            "experiment_id": str(experiment_id),
            "entries": [e.as_dict() for e in self._entries],
            "summary": self.summary(),
            "updated_at": str(updated_at),
        }


def change_set_signature(change_set: dict[str, Any]) -> str:
    """Order-independent signature so equivalent change-sets dedupe."""

    items = sorted((str(k), round(float(v), 6) if isinstance(v, (int, float)) else v)
                   for k, v in dict(change_set or {}).items())
    return json.dumps(items, sort_keys=True, separators=(",", ":"))
