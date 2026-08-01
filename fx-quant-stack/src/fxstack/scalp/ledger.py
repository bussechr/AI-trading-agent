"""Append-only decision ledger -- the scalper's falsification dataset.

One JSONL file per UTC day. Every finalized bar evaluation writes exactly one
record with the FULL reason chain (signal verdict, gate vetoes, sizing), and
every shadow fill writes a fill record. Nothing is sampled, nothing is
filtered: pre-registered kill criteria get evaluated against THIS file, so it
must contain the misses, not just the action.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any


class ScalpLedger:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, *, epoch: float) -> Path:
        day = dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc).strftime("%Y%m%d")
        return self.root / f"ledger_{day}.jsonl"

    @staticmethod
    def day_key(epoch: float) -> str:
        return dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc).strftime("%Y%m%d")

    def record(self, *, kind: str, epoch: float, payload: dict[str, Any]) -> None:
        row = {
            "kind": str(kind),
            "ts": dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc).isoformat(),
            **payload,
        }
        try:
            with self._path(epoch=epoch).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
        except OSError:
            # A ledger write failure must not kill the loop, but it MUST be
            # visible: the loop's heartbeat carries the error count.
            raise
