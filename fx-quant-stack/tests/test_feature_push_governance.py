from __future__ import annotations

import os
import time
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import event

from fxstack.feast.push import (
    FeaturePushWorker,
    build_push_payload,
    claim_feature_push_batch,
    drain_feature_push_outbox,
    enqueue_feature_push,
    record_feature_parity,
    record_feature_push_failure,
    record_feature_push_success,
)
from fxstack.runtime.db_tools import migrate_database, verify_database
from fxstack.runtime.service import RuntimeService


def _fresh_service(tmp_path: Path) -> RuntimeService:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    os.environ["FXSTACK_DATABASE_URL"] = database_url
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    out = migrate_database(database_url=database_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    get_settings.cache_clear()
    return RuntimeService(database_url=database_url)


def test_database_verification_includes_feature_push_tables(tmp_path: Path):
    service = _fresh_service(tmp_path)
    out = verify_database(database_url=service.store.database_url)
    assert bool(out["ok"]), out
    assert "feature_push_outbox" in out["required"]
    assert "feature_push_audit" in out["required"]
    assert "feature_parity_audit" in out["required"]


def test_outbox_claim_success_failure_and_parity_roundtrip(tmp_path: Path):
    service = _fresh_service(tmp_path)
    store = service.store

    payload = build_push_payload(
        pair="eurusd",
        feature_service="fx.swing.v1",
        entity_key="EURUSD",
        event_timestamp=1775440000.0,
        feature_values={"feature_a": 1.25, "feature_b": 0.75},
        feature_version="v1",
        checksum="abc123",
    )
    row = enqueue_feature_push(store, payload)
    assert row["outbox_key"] == payload["outbox_key"]

    claimed = claim_feature_push_batch(store, worker_id="worker-1", limit=10)
    assert len(claimed) == 1
    assert claimed[0]["status"] == "claimed"
    assert claimed[0]["claimed_by"] == "worker-1"

    success = record_feature_push_success(store, outbox_key=payload["outbox_key"], worker_id="worker-1")
    assert success["status"] == "succeeded"

    audit = store.get_feature_push_audit(limit=10)
    assert len(audit) == 1
    assert audit[0]["status"] == "succeeded"

    parity = record_feature_parity(
        store,
        pair="EURUSD",
        feature_service="fx.swing.v1",
        entity_key="EURUSD",
        event_timestamp=1775440000.0,
        parity_ok=True,
        payload={"reference": {"feature_a": 1.25}, "observed": {"feature_a": 1.25}},
        source="online",
        drift_score=0.0,
    )
    assert parity["parity_ok"] == 1

    rollup = store.get_feature_push_rollup()
    assert rollup["outbox"]["by_status"]["succeeded"] == 1
    assert rollup["audit"]["by_status"]["succeeded"] == 1
    assert rollup["parity"]["ok"] == 1


def test_claim_feature_push_batch_reclaims_stale_claimed_row(tmp_path: Path):
    service = _fresh_service(tmp_path)
    store = service.store

    payload = build_push_payload(
        pair="EURUSD",
        feature_service="fx.swing.v1",
        entity_key="EURUSD",
        event_timestamp=1775440500.0,
        feature_values={"feature_a": 1.0},
        feature_version="v1",
    )
    enqueue_feature_push(store, payload)

    stale_claimed_at = 1.0
    with store.engine.begin() as conn:
        conn.execute(
            store.feature_push_outbox.update()
            .where(store.feature_push_outbox.c.outbox_key == payload["outbox_key"])
            .values(
                status="claimed",
                claimed_by="worker-old",
                claimed_at=stale_claimed_at,
                updated_at=stale_claimed_at,
                attempt_count=2,
            )
        )

    claimed = claim_feature_push_batch(store, worker_id="worker-new", limit=10)
    assert len(claimed) == 1
    assert claimed[0]["status"] == "claimed"
    assert claimed[0]["claimed_by"] == "worker-new"
    assert claimed[0]["attempt_count"] == 3

    outbox = store.get_feature_push_outbox(limit=10)
    assert outbox[0]["claimed_by"] == "worker-new"
    assert outbox[0]["attempt_count"] == 3
    assert float(outbox[0]["claimed_at"] or 0.0) > stale_claimed_at


def test_claim_feature_push_batch_does_not_direct_claim_fresh_claimed_rows(tmp_path: Path):
    service = _fresh_service(tmp_path)
    store = service.store

    payload = build_push_payload(
        pair="GBPUSD",
        feature_service="fx.swing.v1",
        entity_key="GBPUSD",
        event_timestamp=1775440550.0,
        feature_values={"feature_a": 1.5},
        feature_version="v1",
    )
    enqueue_feature_push(store, payload)

    fresh_claimed_at = time.time()
    with store.engine.begin() as conn:
        conn.execute(
            store.feature_push_outbox.update()
            .where(store.feature_push_outbox.c.outbox_key == payload["outbox_key"])
            .values(
                status="claimed",
                claimed_by="worker-old",
                claimed_at=fresh_claimed_at,
                updated_at=fresh_claimed_at,
            )
        )

    claimed = store.claim_feature_push_batch(worker_id="worker-new", limit=10, statuses={"claimed"})
    assert claimed == []

    outbox = store.get_feature_push_outbox(limit=10)
    assert outbox[0]["claimed_by"] == "worker-old"


def test_claim_feature_push_batch_uses_one_atomic_bounded_update(tmp_path: Path):
    service = _fresh_service(tmp_path)
    store = service.store
    now = time.time()
    statuses = ["queued"] * 5 + ["retry"] * 4 + ["succeeded", "claimed"]
    with store.engine.begin() as conn:
        conn.execute(
            store.feature_push_outbox.insert(),
            [
                {
                    "outbox_key": f"atomic-claim-{index}",
                    "pair": "EURUSD",
                    "feature_service": "fx.swing.v1",
                    "entity_key": "EURUSD",
                    "event_timestamp": now + index,
                    "payload_json": {},
                    "status": status,
                    "attempt_count": 0,
                    "claimed_by": "worker-old" if status == "claimed" else None,
                    "claimed_at": now if status == "claimed" else None,
                    "created_at": float(index),
                    "updated_at": now,
                }
                for index, status in enumerate(statuses)
            ],
        )

    statements: list[str] = []

    def _capture_claim(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(str(statement))

    event.listen(store.engine, "before_cursor_execute", _capture_claim)
    try:
        claimed = store.claim_feature_push_batch(worker_id="worker-new", limit=6)
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_claim)

    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("UPDATE")
    assert [row["outbox_key"] for row in claimed] == [
        f"atomic-claim-{index}" for index in range(6)
    ]
    assert all(row["status"] == "claimed" for row in claimed)
    assert all(row["claimed_by"] == "worker-new" for row in claimed)
    assert all(row["attempt_count"] == 1 for row in claimed)


def test_failure_moves_outbox_to_retry_and_records_audit(tmp_path: Path):
    service = _fresh_service(tmp_path)
    store = service.store

    payload = build_push_payload(
        pair="GBPUSD",
        feature_service="fx.intraday.v1",
        entity_key="GBPUSD",
        event_timestamp=1775441000.0,
        feature_values={"feature_a": 2.0},
        feature_version="v1",
    )
    enqueue_feature_push(store, payload)

    failed = record_feature_push_failure(
        store,
        outbox_key=payload["outbox_key"],
        worker_id="worker-2",
        message="temporary upstream error",
        retryable=True,
    )
    assert failed["status"] == "retry"

    outbox = store.get_feature_push_outbox(limit=10)
    assert outbox[0]["status"] == "retry"
    audit = store.get_feature_push_audit(limit=10)
    assert audit[0]["status"] == "retry"

    worker = FeaturePushWorker(store=store, worker_id="worker-3")
    claimed = worker.claim(limit=10)
    assert len(claimed) == 1
    assert claimed[0]["claimed_by"] == "worker-3"


def test_drain_feature_push_outbox_supports_dry_run(tmp_path: Path):
    service = _fresh_service(tmp_path)
    store = service.store

    payload = build_push_payload(
        pair="EURUSD",
        feature_service="fx.intraday.v1",
        entity_key="EURUSD",
        event_timestamp=1775442000.0,
        feature_values={"feature_a": 3.5},
        feature_version="v1",
    )
    enqueue_feature_push(store, payload)

    result = drain_feature_push_outbox(
        store,
        worker_id="worker-dry",
        limit=10,
        dry_run=True,
    )

    assert result["claimed"] == 1
    assert result["succeeded"] == 1
    outbox = store.get_feature_push_outbox(limit=10)
    assert outbox[0]["status"] == "succeeded"


def test_feature_push_worker_prepares_database_before_drain(monkeypatch, tmp_path: Path):
    """The installed worker migrates the DB before it ever drains the outbox.

    This used to live in the deleted ``ops/windows/feature_push_worker_loop.py``;
    the behaviour now belongs to the installed
    ``fxstack.runtime.feature_push_worker`` module launched by
    ``ops/windows/24_start_feature_push_worker.bat``.
    """
    from fxstack.runtime import feature_push_worker as worker

    calls: list[dict[str, object]] = []

    def _migrate_database(*, database_url: str, root: Path):  # noqa: ANN001
        calls.append({"database_url": database_url, "root": root})
        return {"ok": True, "return_code": 0}

    monkeypatch.setattr(worker, "migrate_database", _migrate_database)

    project_root = tmp_path / "repo"
    fxstack_root = project_root / "fx-quant-stack"
    fxstack_root.mkdir(parents=True)
    (fxstack_root / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")

    worker._prepare_worker_database(
        project_root=project_root,
        database_url="sqlite+pysqlite:///tmp/feature-push.db",
    )

    assert calls == [
        {
            "database_url": "sqlite+pysqlite:///tmp/feature-push.db",
            "root": fxstack_root,
        }
    ]


def test_feature_push_worker_fails_closed_when_migration_fails(monkeypatch, tmp_path: Path):
    """A failed migration must abort startup rather than drain against a stale schema."""
    from fxstack.runtime import feature_push_worker as worker

    monkeypatch.setattr(
        worker,
        "migrate_database",
        lambda **_: {"ok": False, "return_code": 1, "stderr": "boom"},
    )

    project_root = tmp_path / "repo"
    fxstack_root = project_root / "fx-quant-stack"
    fxstack_root.mkdir(parents=True)
    (fxstack_root / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="database migration failed"):
        worker._prepare_worker_database(
            project_root=project_root,
            database_url="sqlite+pysqlite:///tmp/feature-push.db",
        )


def test_publish_feature_payload_fills_missing_schema_columns_with_type_safe_defaults(monkeypatch):
    from fxstack.feast import push as push_mod

    captured: dict[str, pd.DataFrame] = {}

    class _Field:
        def __init__(self, name: str, value_type: str) -> None:
            self.name = name
            self.dtype = self
            self._value_type = value_type

        def to_value_type(self) -> str:
            return self._value_type

    class _Handle:
        def get_feature_view(self, name: str):  # noqa: ANN001
            return type(
                "_View",
                (),
                {
                    "schema": [
                        _Field("feature_a", "ValueType.DOUBLE"),
                        _Field("feature_b", "ValueType.DOUBLE"),
                        _Field("label_bucket", "ValueType.STRING"),
                    ]
                },
            )()

        def write_to_online_store(self, name: str, frame: pd.DataFrame):  # noqa: ANN001
            captured["frame"] = frame.copy()

    monkeypatch.setattr(push_mod, "_feature_store_handle", lambda repo_root: _Handle())
    ok, message = push_mod.publish_feature_payload(
        payload=build_push_payload(
            pair="EURUSD",
            feature_service="fx_test_service",
            entity_key="EURUSD",
            event_timestamp=1775442000.0,
            feature_values={"feature_a": 1.25},
        ),
        repo_root=str(Path.cwd()),
    )

    assert ok is True
    assert message == "online_store_written"
    frame = captured["frame"]
    assert frame.loc[0, "feature_a"] == 1.25
    assert pd.isna(frame.loc[0, "feature_b"])
    assert frame.loc[0, "label_bucket"] == ""
