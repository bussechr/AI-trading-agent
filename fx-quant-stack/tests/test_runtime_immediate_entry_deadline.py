from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from fxstack.runtime import postgres_store as postgres_store_module
from fxstack.runtime.db_tools import migrate_database
from fxstack.runtime.dto import ExecutionCommand
from fxstack.runtime.postgres_store import PostgresRuntimeStore
from fxstack.runtime.scalp_execution_boundary import (
    production_scalp_immediate_entry_contract_error,
)
from fxstack.schemas.entry import EntryProposal


def _immediate_payload(*, deadline: int, command_id: str) -> dict[str, object]:
    return {
        "command_id": command_id,
        "cmd": "BUY",
        "symbol": "EURUSD",
        "lots": 0.1,
        "strategy_lane": "production_scalper",
        "intent": "production_scalper_entry",
        "execution_type": "market",
        "pending_orders_forbidden": True,
        "entry_deadline_epoch": deadline,
    }


def _test_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    migrated = migrate_database(
        database_url=database_url,
        root=Path(__file__).resolve().parents[1],
    )
    assert migrated.get("ok") is True, migrated
    store = PostgresRuntimeStore(database_url)
    monkeypatch.setattr(
        store,
        "_execution_egress_authorization_failure",
        lambda conn, *, now_ts=None, command=None: "",
    )
    monkeypatch.setattr(
        store,
        "_poll_entry_authorization_failure",
        lambda conn, *, row, now_ts: production_scalp_immediate_entry_contract_error(
            dict(row.get("payload_json") or {}),
            now_epoch=now_ts,
        ),
    )
    return store


def test_server_owned_command_ttl_is_capped_at_t_plus_five() -> None:
    command = ExecutionCommand.from_payload(
        _immediate_payload(deadline=1_005, command_id="ttl-cap"),
        default_session_id="unit",
        ttl_secs=120.0,
        now_ts=1_000.25,
    )

    assert command.created_at == pytest.approx(1_000.25)
    assert command.expires_at == pytest.approx(1_005.0)
    assert command.expires_at - command.created_at < 5.0

    with pytest.raises(ValueError, match="entry_deadline_epoch has expired"):
        ExecutionCommand.from_payload(
            _immediate_payload(deadline=1_005, command_id="ttl-expired"),
            default_session_id="unit",
            ttl_secs=120.0,
            now_ts=1_005.0,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"execution_type": "limit"}, "execution_type=market"),
        ({"pending_orders_forbidden": False}, "forbid pending orders"),
        ({"entry_deadline_epoch": None}, "entry_deadline_epoch"),
    ),
)
def test_command_dto_refuses_pending_or_unbounded_entry_representations(
    mutation: dict[str, object],
    message: str,
) -> None:
    payload = {
        **_immediate_payload(deadline=1_005, command_id="invalid-contract"),
        **mutation,
    }
    with pytest.raises(ValueError, match=message):
        ExecutionCommand.from_payload(
            payload,
            default_session_id="unit",
            ttl_secs=120.0,
            now_ts=1_000.0,
        )


def test_proposal_type_cannot_represent_a_pending_entry() -> None:
    proposal_fields = {item.name: item for item in fields(EntryProposal)}

    assert proposal_fields["execution_type"].init is False
    assert proposal_fields["pending_orders_forbidden"].init is False
    assert proposal_fields["execution_type"].default == "market"
    assert proposal_fields["pending_orders_forbidden"].default is True


def test_store_atomically_caps_then_quarantines_t_plus_five_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1_000.0}
    monkeypatch.setattr(
        postgres_store_module,
        "_now",
        lambda: float(clock["now"]),
    )
    store = _test_store(tmp_path, monkeypatch)
    command = ExecutionCommand.from_payload(
        _immediate_payload(deadline=1_005, command_id="atomic-t-plus-five"),
        default_session_id="unit",
        ttl_secs=120.0,
        now_ts=1_000.0,
    )
    # Simulate a hand-built/stale caller trying to restore the configured TTL.
    command.expires_at = 1_120.0

    queued, state = store.enqueue_command(command)

    assert (queued, state) == (True, "queued")
    persisted = store.get_command(command.command_id)
    assert persisted is not None
    assert float(persisted["expires_at"]) == 1_005.0

    clock["now"] = 1_005.0
    assert store.poll_next_command() is None
    quarantined = store.get_command(command.command_id)
    assert quarantined is not None
    assert quarantined["status"] == "expired"
    assert quarantined["delivered_count"] == 0
    assert quarantined["reason"] == (
        "poll_authority_revoked:scalp_market_entry_deadline_expired"
    )


def test_store_resamples_deadline_after_poll_authority_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1_004.0}
    monkeypatch.setattr(
        postgres_store_module,
        "_now",
        lambda: float(clock["now"]),
    )
    store = _test_store(tmp_path, monkeypatch)
    command = ExecutionCommand.from_payload(
        _immediate_payload(deadline=1_005, command_id="poll-crosses-deadline"),
        default_session_id="unit",
        ttl_secs=120.0,
        now_ts=1_004.0,
    )
    assert store.enqueue_command(command) == (True, "queued")

    def _authorization_crosses_deadline(conn, *, row, now_ts):
        del conn
        if clock["now"] < 1_005.0:
            clock["now"] = 1_005.0
            return ""
        return production_scalp_immediate_entry_contract_error(
            dict(row.get("payload_json") or {}),
            now_epoch=now_ts,
        )

    monkeypatch.setattr(
        store,
        "_poll_entry_authorization_failure",
        _authorization_crosses_deadline,
    )

    assert store.poll_next_command() is None
    persisted = store.get_command(command.command_id)
    assert persisted is not None
    assert persisted["status"] == "expired"
    assert persisted["delivered_count"] == 0
    assert persisted["reason"] == (
        "poll_authority_revoked:scalp_market_entry_deadline_expired"
    )


def test_store_refuses_already_expired_entry_without_a_queue_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1_004.0}
    monkeypatch.setattr(
        postgres_store_module,
        "_now",
        lambda: float(clock["now"]),
    )
    store = _test_store(tmp_path, monkeypatch)
    command = ExecutionCommand.from_payload(
        _immediate_payload(deadline=1_005, command_id="atomic-expired-refusal"),
        default_session_id="unit",
        ttl_secs=120.0,
        now_ts=1_004.0,
    )

    clock["now"] = 1_005.0
    queued, state = store.enqueue_command(command)

    assert queued is False
    assert state == "scalp_market_entry_deadline_expired"
    assert store.get_command(command.command_id) is None
