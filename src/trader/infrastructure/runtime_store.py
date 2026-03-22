"""Persistent command/state repository backed by sqlite.

This module is a thin coordinator that composes the focused mixin modules:

* ``schema.py``         – DDL, migration, legacy backfill
* ``command_store.py``  – enqueue / poll / ack / expire
* ``tick_store.py``     – tick, report, decision persistence
* ``metrics_store.py``  – metrics, health, state queries

All public API remains on :class:`RuntimeStore` so existing imports are
unaffected.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from src.trader.domain.decision_pipeline import DecisionPipeline

from .command_store import CommandStoreMixin
from .metrics_store import MetricsStoreMixin
from .schema import SchemaMixin
from .tick_store import TickStoreMixin


class RuntimeStore(SchemaMixin, CommandStoreMixin, TickStoreMixin, MetricsStoreMixin):
    """Persistent command/state repository backed by sqlite."""

    def __init__(
        self,
        db_path: str,
        *,
        soft_band: tuple[float, float] = (0.06, 0.09),
        hard_band: tuple[float, float] = (0.10, 0.12),
        daily_band: tuple[float, float] = (0.02, 0.03),
        sizing_band: tuple[float, float, float] = (0.03, 0.01, 2.00),
    ) -> None:
        self.db_path = str(db_path)
        self.soft_band = (float(soft_band[0]), float(soft_band[1]))
        self.hard_band = (float(hard_band[0]), float(hard_band[1]))
        self.daily_band = (float(daily_band[0]), float(daily_band[1]))
        self.sizing_band = (float(sizing_band[0]), float(sizing_band[1]), float(sizing_band[2]))
        self._decision_pipeline = DecisionPipeline()

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._init_schema_locked()
            self._recover_pending_on_startup_locked()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
