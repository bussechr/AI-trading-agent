from __future__ import annotations

from types import SimpleNamespace

import pytest

from fxstack.runtime import runner as runtime_runner
from fxstack.strategy.campaign_types import CampaignConfig, CampaignRegistryEntry
from fxstack.strategy.sleeve_governance import SleeveGovernanceTracker


def _management_settings() -> SimpleNamespace:
    return SimpleNamespace(
        min_order_lots=0.01,
        order_lot_step=0.01,
        max_order_lots=0.10,
        partial_close_fraction=0.50,
        partial_close_cooldown_secs=1800.0,
        max_partial_closes_per_position=2,
    )


class _CommandService:
    def __init__(self, command: dict | None = None) -> None:
        self.command = dict(command or {})
        self.payloads: list[dict] = []

    def submit_command(self, payload, proto="v2"):
        self.payloads.append(dict(payload))
        return {
            "status": "queued",
            "command_id": str(payload.get("command_id") or ""),
            "action": payload.get("action"),
        }, 200

    def get_command(self, command_id: str):
        if not self.command:
            return None
        return dict(self.command, command_id=command_id)


def _paper_management_settings() -> SimpleNamespace:
    settings = _management_settings()
    settings.agent_mode = "paper"
    settings.agent_paper_pair_allowlist = ["EURUSD"]
    settings.agent_paper_sleeve_allowlist = []
    settings.agent_paper_intent_allowlist = []
    return settings


def _position_action(*, lots_open: float, close_lots: float = 0.0) -> tuple[list[dict], list[dict]]:
    decisions = [
        {
            "symbol": "EURUSD",
            "metadata": {
                "pair": "EURUSD",
                "position_signature": "EURUSD|long|100|1.10000000|246810",
                "lifecycle_action": "partial_tp",
                "lifecycle_reason": "adaptive_campaign_harvest",
            },
        }
    ]
    actions = [
        {
            "index": 0,
            "pair": "EURUSD",
            "position_signature": "EURUSD|long|100|1.10000000|246810",
            "lifecycle_action": "partial_tp",
            "lifecycle_reason": "adaptive_campaign_harvest",
            "lifecycle_action_score": 0.6,
            "lots_open": float(lots_open),
            "close_lots": float(close_lots),
            "sl_price": 0.0,
        }
    ]
    return decisions, actions


def test_final_campaign_harvest_materializes_partial_close_lots() -> None:
    decisions, actions = _position_action(lots_open=0.06)

    diag = runtime_runner._materialize_final_position_actions(
        decisions=decisions,
        pending_position_actions=actions,
        partial_close_tracker={},
        loop_ts=10_000.0,
        settings=_management_settings(),
    )

    assert diag["partial_materialized_count"] == 1
    assert actions[0]["lifecycle_action"] == "partial_tp"
    assert actions[0]["close_lots"] == pytest.approx(0.03)
    assert decisions[0]["metadata"]["close_lots"] == pytest.approx(0.03)


def test_final_harvest_promotes_sub_minimum_residue_to_full_exit() -> None:
    decisions, actions = _position_action(lots_open=0.01)

    diag = runtime_runner._materialize_final_position_actions(
        decisions=decisions,
        pending_position_actions=actions,
        partial_close_tracker={},
        loop_ts=10_000.0,
        settings=_management_settings(),
    )

    assert diag["promoted_to_exit_count"] == 1
    assert actions[0]["lifecycle_action"] == "exit"
    assert actions[0]["close_lots"] == pytest.approx(0.01)


def test_final_harvest_respects_partial_close_cooldown() -> None:
    decisions, actions = _position_action(lots_open=0.06)
    tracker = {
        actions[0]["position_signature"]: {
            "count": 1,
            "last_partial_ts": 9_500.0,
        }
    }

    diag = runtime_runner._materialize_final_position_actions(
        decisions=decisions,
        pending_position_actions=actions,
        partial_close_tracker=tracker,
        loop_ts=10_000.0,
        settings=_management_settings(),
    )

    assert diag["blocked_count"] == 1
    assert actions[0]["lifecycle_action"] == "hold"
    assert actions[0]["close_lots"] == 0.0
    assert actions[0]["lifecycle_reason"] == "partial_tp_cooldown_active"


def test_position_registry_preserves_state_for_same_broker_signature_and_reseeds_new_ticket() -> None:
    position = {
        "symbol": "EURUSD",
        "type": 0,
        "lots": 0.03,
        "open_time": 100,
        "open_price": 1.10,
        "sl": 1.09,
        "tp": 1.14,
        "magic": 246810,
    }
    signature = runtime_runner._position_signature(position)
    existing = SimpleNamespace(
        position_signature=signature,
        campaign_state="press",
        entry_trade_prob=0.30,
        partial_count=1,
        last_partial_bar_index=12,
        initial_risk_price=0.01,
        current_lots=0.04,
        open_price=1.10,
    )
    registry = {"EURUSD": existing}
    decisions = [
        {
            "symbol": "EURUSD",
            "metadata": {
                "pair": "EURUSD",
                "position_count_pair": 1,
                "position_side": "long",
            },
        }
    ]

    runtime_runner._sync_adaptive_position_registry(
        decisions=decisions,
        state={"positions": [position]},
        adaptive_rows_by_pair={"EURUSD": {}},
        adaptive_pending_entry_registry={},
        adaptive_position_registry=registry,
        current_equity=10_000.0,
    )

    assert registry["EURUSD"] is existing
    assert registry["EURUSD"].campaign_state == "press"
    assert registry["EURUSD"].entry_trade_prob == pytest.approx(0.30)
    assert registry["EURUSD"].partial_count == 1
    assert registry["EURUSD"].current_lots == pytest.approx(0.03)

    replacement = dict(position, open_time=200, open_price=1.11, lots=0.02)
    runtime_runner._sync_adaptive_position_registry(
        decisions=decisions,
        state={"positions": [replacement]},
        adaptive_rows_by_pair={"EURUSD": {}},
        adaptive_pending_entry_registry={},
        adaptive_position_registry=registry,
        current_equity=10_000.0,
    )

    assert registry["EURUSD"] is not existing
    assert registry["EURUSD"].campaign_state == "probe"
    assert registry["EURUSD"].partial_count == 0
    assert registry["EURUSD"].position_signature == runtime_runner._position_signature(replacement)


def test_partial_submission_stays_pending_until_broker_truth() -> None:
    decisions, actions = _position_action(lots_open=0.06, close_lots=0.03)
    actions[0]["ts_value"] = "2026-07-21T10:00:00Z"
    actions[0]["approved_order"] = {
        "cmd": "CLOSE_PARTIAL",
        "symbol": "EURUSD",
        "lots": 0.03,
        "close_lots": 0.03,
        "intent": "EXIT_MODEL",
        "action": "partial_tp",
        "side": "",
    }
    actions[0]["orchestration"] = {
        "enabled": True,
        "correlation_id": "EURUSD:paper:partial",
        "thread_id": "EURUSD:paper:partial",
    }
    signature = str(actions[0]["position_signature"])
    registry_state = SimpleNamespace(
        position_signature=signature,
        current_lots=0.06,
        partial_count=0,
        last_partial_bar_index=None,
    )
    tracker: dict[str, dict] = {}
    svc = _CommandService()

    diag = runtime_runner._submit_position_actions(
        decisions=decisions,
        pending_position_actions=actions,
        svc=svc,
        settings=_paper_management_settings(),
        last_action_key={},
        partial_close_tracker=tracker,
        adaptive_position_registry={"EURUSD": registry_state},
        adaptive_recent_exit_registry={},
        pair_bar_index={"EURUSD": 42},
        loop_ts=10_000.0,
    )

    assert diag["submitted_partial_close_count"] == 1
    assert diag["pending_partial_ack_count"] == 1
    assert tracker[signature]["count"] == 0
    assert tracker[signature]["pending_command_id"] == svc.payloads[0]["command_id"]
    assert tracker[signature]["pending_open_lots"] == pytest.approx(0.06)
    assert tracker[signature]["pending_close_lots"] == pytest.approx(0.03)
    assert registry_state.partial_count == 0
    assert registry_state.last_partial_bar_index is None


def test_partial_ack_commits_limit_and_cooldown_state_once() -> None:
    position = {
        "symbol": "EURUSD",
        "type": 0,
        "lots": 0.03,
        "open_time": 100,
        "open_price": 1.10,
        "magic": 246810,
    }
    signature = runtime_runner._position_signature(position)
    tracker = {
        signature: {
            "count": 0,
            "pending_command_id": "partial-acked-1",
            "pending_pair": "EURUSD",
            "pending_position_signature": signature,
            "pending_open_lots": 0.06,
            "pending_close_lots": 0.03,
            "pending_submitted_ts": 9_900.0,
            "pending_bar_index": 42,
        }
    }
    registry_state = SimpleNamespace(
        position_signature=signature,
        partial_count=0,
        last_partial_bar_index=None,
    )
    svc = _CommandService({"status": "acked", "delivered_count": 1})

    diag = runtime_runner._reconcile_partial_close_tracker(
        partial_close_tracker=tracker,
        adaptive_position_registry={"EURUSD": registry_state},
        state={"positions": [position]},
        svc=svc,
        loop_ts=10_000.0,
        settings=_management_settings(),
        position_snapshot_advanced=True,
        position_snapshot_received_at=10_000.0,
    )

    assert diag["partial_ack_committed_count"] == 1
    assert diag["partial_ack_pending_count"] == 0
    assert tracker[signature]["count"] == 1
    assert tracker[signature]["last_partial_ts"] == pytest.approx(10_000.0)
    assert tracker[signature]["last_partial_cmd_id"] == "partial-acked-1"
    assert "pending_command_id" not in tracker[signature]
    assert registry_state.partial_count == 1
    assert registry_state.last_partial_bar_index == 42

    second = runtime_runner._reconcile_partial_close_tracker(
        partial_close_tracker=tracker,
        adaptive_position_registry={"EURUSD": registry_state},
        state={"positions": [position]},
        svc=svc,
        loop_ts=10_100.0,
        settings=_management_settings(),
        position_snapshot_advanced=False,
        position_snapshot_received_at=10_000.0,
    )
    assert second["partial_ack_committed_count"] == 0
    assert tracker[signature]["count"] == 1


@pytest.mark.parametrize(
    ("command", "expected_key"),
    [
        (
            {"status": "failed", "delivered_count": 1, "updated_at": 9_950.0},
            "partial_ack_failed_count",
        ),
        (
            {"status": "expired", "delivered_count": 0},
            "partial_ack_expired_undelivered_count",
        ),
    ],
)
def test_failed_or_undelivered_partial_does_not_consume_management_budget(
    command: dict,
    expected_key: str,
) -> None:
    position = {
        "symbol": "EURUSD",
        "type": 0,
        "lots": 0.06,
        "open_time": 100,
        "open_price": 1.10,
        "magic": 246810,
    }
    signature = runtime_runner._position_signature(position)
    tracker = {
        signature: {
            "count": 0,
            "pending_command_id": "partial-terminal-1",
            "pending_pair": "EURUSD",
            "pending_open_lots": 0.06,
            "pending_close_lots": 0.03,
        }
    }

    diag = runtime_runner._reconcile_partial_close_tracker(
        partial_close_tracker=tracker,
        adaptive_position_registry={},
        state={"positions": [position]},
        svc=_CommandService(command),
        loop_ts=10_000.0,
        settings=_management_settings(),
        position_snapshot_advanced=True,
        position_snapshot_received_at=10_000.0,
    )

    assert diag[expected_key] == 1
    assert tracker[signature]["count"] == 0
    assert "last_partial_ts" not in tracker[signature]
    assert "pending_command_id" not in tracker[signature]


def test_broker_lot_reduction_resolves_ambiguous_partial_status() -> None:
    position = {
        "symbol": "EURUSD",
        "type": 0,
        "lots": 0.03,
        "open_time": 100,
        "open_price": 1.10,
        "magic": 246810,
    }
    signature = runtime_runner._position_signature(position)
    tracker = {
        signature: {
            "count": 0,
            "pending_command_id": "partial-ambiguous-1",
            "pending_pair": "EURUSD",
            "pending_open_lots": 0.06,
            "pending_close_lots": 0.03,
        }
    }

    diag = runtime_runner._reconcile_partial_close_tracker(
        partial_close_tracker=tracker,
        adaptive_position_registry={},
        state={"positions": [position]},
        svc=_CommandService({"status": "reconcile_required", "delivered_count": 1}),
        loop_ts=10_000.0,
        settings=_management_settings(),
        position_snapshot_advanced=True,
        position_snapshot_received_at=10_000.0,
    )

    assert diag["partial_ack_committed_count"] == 1
    assert diag["partial_ack_observed_reduction_count"] == 1
    assert tracker[signature]["count"] == 1
    assert tracker[signature]["last_partial_confirmation"] == "broker_lot_reduction"


def test_failed_ack_waits_for_newer_position_snapshot_then_observed_reduction_wins() -> None:
    before_position = {
        "symbol": "EURUSD",
        "type": 0,
        "lots": 0.06,
        "open_time": 100,
        "open_price": 1.10,
        "magic": 246810,
    }
    signature = runtime_runner._position_signature(before_position)
    tracker = {
        signature: {
            "count": 0,
            "pending_command_id": "partial-failed-after-work-1",
            "pending_pair": "EURUSD",
            "pending_open_lots": 0.06,
            "pending_close_lots": 0.03,
            "pending_submitted_ts": 9_900.0,
        }
    }
    failed_command = {
        "status": "failed",
        "delivered_count": 1,
        "updated_at": 9_990.0,
    }

    old_snapshot = runtime_runner._reconcile_partial_close_tracker(
        partial_close_tracker=tracker,
        adaptive_position_registry={},
        state={"positions": [before_position]},
        svc=_CommandService(failed_command),
        loop_ts=10_000.0,
        settings=_management_settings(),
        position_snapshot_advanced=True,
        position_snapshot_received_at=9_980.0,
    )

    assert old_snapshot["partial_ack_failed_count"] == 0
    assert old_snapshot["partial_ack_pending_count"] == 1
    assert tracker[signature]["count"] == 0

    after_position = dict(before_position, lots=0.03)
    newer_snapshot = runtime_runner._reconcile_partial_close_tracker(
        partial_close_tracker=tracker,
        adaptive_position_registry={},
        state={"positions": [after_position]},
        svc=_CommandService(failed_command),
        loop_ts=10_010.0,
        settings=_management_settings(),
        position_snapshot_advanced=True,
        position_snapshot_received_at=10_005.0,
    )

    assert newer_snapshot["partial_ack_committed_count"] == 1
    assert newer_snapshot["partial_ack_failed_count"] == 0
    assert tracker[signature]["count"] == 1
    assert tracker[signature]["last_partial_confirmation"] == "broker_lot_reduction"


def test_duplicate_partial_resolves_without_count_on_newer_unchanged_snapshot() -> None:
    position = {
        "symbol": "EURUSD",
        "type": 0,
        "lots": 0.06,
        "open_time": 100,
        "open_price": 1.10,
        "magic": 246810,
    }
    signature = runtime_runner._position_signature(position)
    tracker = {
        signature: {
            "count": 0,
            "pending_command_id": "partial-duplicate-1",
            "pending_pair": "EURUSD",
            "pending_open_lots": 0.06,
            "pending_close_lots": 0.03,
            "pending_submitted_ts": 9_900.0,
        }
    }

    diag = runtime_runner._reconcile_partial_close_tracker(
        partial_close_tracker=tracker,
        adaptive_position_registry={},
        state={"positions": [position]},
        svc=_CommandService(
            {"status": "duplicate", "delivered_count": 1, "updated_at": 9_990.0}
        ),
        loop_ts=10_010.0,
        settings=_management_settings(),
        position_snapshot_advanced=True,
        position_snapshot_received_at=10_005.0,
    )

    assert diag["partial_ack_resolved_unchanged_count"] == 1
    assert diag["partial_ack_pending_count"] == 0
    assert tracker[signature]["count"] == 0
    assert "pending_command_id" not in tracker[signature]
    assert tracker[signature]["last_partial_resolution"] == (
        "broker_snapshot_unchanged:duplicate"
    )


def test_position_memory_survives_transient_empty_snapshots_then_prunes() -> None:
    registry = {
        "EURUSD": SimpleNamespace(
            pair="EURUSD",
            position_signature="EURUSD|long|100|1.10000000|246810",
            campaign_state="press",
            partial_count=1,
            _missing_position_cycles=0,
        )
    }

    runtime_runner._sync_adaptive_position_registry(
        decisions=[],
        state={"positions": []},
        adaptive_rows_by_pair={},
        adaptive_pending_entry_registry={},
        adaptive_position_registry=registry,
        current_equity=10_000.0,
        position_snapshot_authoritative=False,
    )
    assert registry["EURUSD"]._missing_position_cycles == 0

    for expected_missing in (1, 2):
        runtime_runner._sync_adaptive_position_registry(
            decisions=[],
            state={"positions": []},
            adaptive_rows_by_pair={},
            adaptive_pending_entry_registry={},
            adaptive_position_registry=registry,
            current_equity=10_000.0,
            position_snapshot_authoritative=True,
        )
        assert registry["EURUSD"]._missing_position_cycles == expected_missing

    runtime_runner._sync_adaptive_position_registry(
        decisions=[],
        state={"positions": []},
        adaptive_rows_by_pair={},
        adaptive_pending_entry_registry={},
        adaptive_position_registry=registry,
        current_equity=10_000.0,
        position_snapshot_authoritative=True,
    )
    assert "EURUSD" not in registry


def test_managed_position_state_round_trips_across_runtime_restart() -> None:
    signature = "EURUSD|long|100|1.10000000|246810"
    adaptive = {
        "EURUSD": SimpleNamespace(
            pair="EURUSD",
            side="long",
            position_signature=signature,
            campaign_state="press",
            partial_count=1,
            last_partial_bar_index=42,
            _missing_position_cycles=0,
        )
    }
    partials = {
        signature: {
            "count": 1,
            "last_partial_ts": 9_000.0,
            "pending_command_id": "partial-restart-1",
            "pending_pair": "EURUSD",
            "pending_open_lots": 0.03,
        }
    }
    campaigns = {
        "thesis-1": CampaignRegistryEntry(
            thesis_id="thesis-1",
            pair="EURUSD",
            side="long",
            sleeve="trend",
            campaign_active=True,
            state="press",
            active_position=True,
            harvest_count=1,
        )
    }

    payload = runtime_runner._serialize_managed_position_state(
        adaptive_position_registry=adaptive,
        partial_close_tracker=partials,
        campaign_registry=campaigns,
        saved_at=10_000.0,
        adaptive_pending_entry_registry={
            "EURUSD": {"approved_lots": 0.03, "initial_sl_price": 1.09}
        },
        adaptive_recent_exit_registry={
            "EURUSD": {"bar_idx": 40, "side": "long", "reason": "managed_exit"}
        },
        exit_command_ledger={
            "exit-restart-1": {
                "command_id": "exit-restart-1",
                "pair": "EURUSD",
                "position_signature": signature,
                "submitted_ts": 9_500.0,
                "resolved": False,
            }
        },
    )
    restored_adaptive: dict[str, SimpleNamespace] = {}
    restored_partials: dict[str, dict] = {}
    restored_campaigns: dict[str, CampaignRegistryEntry] = {}
    restored_pending_entries: dict[str, dict] = {}
    restored_recent_exits: dict[str, dict] = {}
    restored_exit_commands: dict[str, dict] = {}

    diag = runtime_runner._restore_managed_position_state(
        payload=payload,
        adaptive_position_registry=restored_adaptive,
        partial_close_tracker=restored_partials,
        campaign_registry=restored_campaigns,
        allowed_pairs={"EURUSD"},
        adaptive_pending_entry_registry=restored_pending_entries,
        adaptive_recent_exit_registry=restored_recent_exits,
        exit_command_ledger=restored_exit_commands,
    )

    assert diag["status"] == "restored"
    assert diag["adaptive_position_count"] == 1
    assert diag["partial_tracker_count"] == 1
    assert diag["campaign_count"] == 1
    assert diag["pending_entry_count"] == 1
    assert diag["recent_exit_count"] == 1
    assert diag["exit_command_count"] == 1
    assert restored_adaptive["EURUSD"].campaign_state == "press"
    assert restored_adaptive["EURUSD"].partial_count == 1
    assert restored_partials[signature]["pending_command_id"] == "partial-restart-1"
    assert restored_campaigns["thesis-1"].state == "press"
    assert restored_campaigns["thesis-1"].harvest_count == 1
    assert restored_pending_entries["EURUSD"]["approved_lots"] == pytest.approx(0.03)
    assert restored_recent_exits["EURUSD"]["reason"] == "managed_exit"
    assert restored_exit_commands["exit-restart-1"]["resolved"] is False


def test_durable_command_hydration_closes_enqueue_state_patch_crash_window() -> None:
    signature = "EURUSD|long|100|1.10000000|246810"
    context = {
        "schema": "fxstack_lifecycle_command_context_v1",
        "lifecycle_action": "partial_tp",
        "position_signature": signature,
        "pair": "EURUSD",
        "lots_open": 0.06,
        "close_lots": 0.03,
        "bar_index": 42,
        "submitted_ts": 9_900.0,
    }
    queued = {
        "command_id": "partial-durable-queued",
        "cmd": "CLOSE_PARTIAL",
        "symbol": "EURUSD",
        "status": "delivered",
        "delivered_count": 1,
        "created_at": 9_900.0,
        "updated_at": 9_950.0,
        "payload_json": {"management_context": context},
    }
    acked = {
        **queued,
        "command_id": "partial-durable-acked",
        "status": "acked",
        "created_at": 9_800.0,
        "updated_at": 9_850.0,
    }
    tracker: dict[str, dict] = {}

    first = runtime_runner._hydrate_partial_close_tracker_from_commands(
        commands=[queued, acked],
        partial_close_tracker=tracker,
        adaptive_position_registry={},
        allowed_pairs={"EURUSD"},
    )

    assert first["durable_partial_ack_recovered_count"] == 1
    assert first["durable_partial_pending_recovered_count"] == 1
    assert tracker[signature]["count"] == 1
    assert tracker[signature]["pending_command_id"] == "partial-durable-queued"
    assert tracker[signature]["pending_open_lots"] == pytest.approx(0.06)

    second = runtime_runner._hydrate_partial_close_tracker_from_commands(
        commands=[queued, acked],
        partial_close_tracker=tracker,
        adaptive_position_registry={},
        allowed_pairs={"EURUSD"},
    )

    assert second["durable_partial_ack_recovered_count"] == 0
    assert second["durable_partial_pending_recovered_count"] == 0
    assert tracker[signature]["count"] == 1


def test_old_durable_management_commands_before_state_watermark_are_not_replayed() -> None:
    signature = "EURUSD|long|100|1.10000000|246810"
    context = {
        "schema": "fxstack_lifecycle_command_context_v1",
        "lifecycle_action": "exit",
        "position_signature": signature,
        "pair": "EURUSD",
        "submitted_ts": 9_000.0,
    }
    old_acked = {
        "command_id": "exit-before-watermark",
        "cmd": "CLOSE",
        "symbol": "EURUSD",
        "status": "acked",
        "created_at": 9_000.0,
        "updated_at": 9_100.0,
        "payload_json": {"management_context": context},
    }
    ledger: dict[str, dict] = {}

    diag = runtime_runner._hydrate_exit_command_ledger_from_commands(
        commands=[old_acked],
        exit_command_ledger=ledger,
        allowed_pairs={"EURUSD"},
        after_ts=10_000.0,
    )

    assert diag["durable_exit_recovered_count"] == 0
    assert diag["durable_exit_watermark_skip_count"] == 1
    assert ledger == {}


def test_queued_exit_has_no_strategy_side_effect_until_broker_confirmation() -> None:
    decisions = [
        {
            "symbol": "EURUSD",
            "metadata": {
                "pair": "EURUSD",
                "position_signature": "EURUSD|long|100|1.10000000|246810",
                "position_side": "long",
                "lifecycle_action": "exit",
                "lifecycle_reason": "managed_exit",
                "adaptive_playbook": "trend_pullback",
                "adaptive_sleeve": "trend",
            },
        }
    ]
    actions = [
        {
            "index": 0,
            "pair": "EURUSD",
            "ts_value": "2026-07-21T10:00:00Z",
            "position_signature": "EURUSD|long|100|1.10000000|246810",
            "position_side": "long",
            "lifecycle_action": "exit",
            "lifecycle_reason": "managed_exit",
            "lifecycle_action_score": 0.8,
            "lots_open": 0.06,
            "close_lots": 0.0,
            "approved_order": {
                "cmd": "CLOSE",
                "symbol": "EURUSD",
                "lots": 0.0,
                "close_lots": 0.0,
                "intent": "EXIT_MODEL",
                "action": "exit",
                "side": "",
            },
            "orchestration": {
                "enabled": True,
                "correlation_id": "EURUSD:paper:exit",
                "thread_id": "EURUSD:paper:exit",
            },
        }
    ]
    recent_exits: dict[str, dict] = {}
    campaigns: dict[str, CampaignRegistryEntry] = {}
    ledger: dict[str, dict] = {}
    svc = _CommandService()

    diag = runtime_runner._submit_position_actions(
        decisions=decisions,
        pending_position_actions=actions,
        svc=svc,
        settings=_paper_management_settings(),
        last_action_key={},
        partial_close_tracker={},
        adaptive_position_registry={},
        adaptive_recent_exit_registry=recent_exits,
        pair_bar_index={"EURUSD": 42},
        loop_ts=10_000.0,
        campaign_registry=campaigns,
        campaign_transition_counts={},
        campaign_config=CampaignConfig(enabled=True),
        exit_command_ledger=ledger,
    )

    assert diag["submitted_exit_count"] == 1
    assert diag["pending_exit_ack_count"] == 1
    assert recent_exits == {}
    assert campaigns == {}
    assert len(ledger) == 1

    actions[0]["ts_value"] = "2026-07-21T10:05:00Z"
    duplicate_attempt = runtime_runner._submit_position_actions(
        decisions=decisions,
        pending_position_actions=actions,
        svc=svc,
        settings=_paper_management_settings(),
        last_action_key={},
        partial_close_tracker={},
        adaptive_position_registry={},
        adaptive_recent_exit_registry=recent_exits,
        pair_bar_index={"EURUSD": 43},
        loop_ts=10_300.0,
        exit_command_ledger=ledger,
    )
    assert duplicate_attempt["submitted_position_action_count"] == 0
    assert len(svc.payloads) == 1
    assert decisions[0]["metadata"]["enqueue"]["reason"] == "exit_ack_pending"


def test_confirmed_exit_updates_strategy_state_exactly_once() -> None:
    position = {
        "symbol": "EURUSD",
        "type": 0,
        "lots": 0.06,
        "open_time": 100,
        "open_price": 1.10,
        "magic": 246810,
    }
    signature = runtime_runner._position_signature(position)
    command_id = "exit-acked-1"
    ledger = {
        command_id: {
            "schema": "fxstack_lifecycle_command_context_v1",
            "command_id": command_id,
            "lifecycle_action": "exit",
            "lifecycle_reason": "managed_exit",
            "position_signature": signature,
            "pair": "EURUSD",
            "position_side": "long",
            "playbook": "trend_pullback",
            "sleeve": "trend",
            "campaign_state": "press",
            "thesis_id": "thesis-1",
            "bar_index": 42,
            "decision_ts": "2026-07-21T10:00:00Z",
            "submitted_ts": 9_900.0,
            "unrealized_pnl_usd": 12.0,
            "age_bars": 8.0,
            "session_bucket": "london",
            "partial_count": 1,
            "resolved": False,
        }
    }
    recent_exits: dict[str, dict] = {}
    campaigns: dict[str, CampaignRegistryEntry] = {}
    transitions: dict[str, int] = {}
    sleeve_tracker = SleeveGovernanceTracker(sleeves=["trend"])
    svc = _CommandService(
        {"status": "acked", "delivered_count": 1, "updated_at": 9_990.0}
    )

    first = runtime_runner._reconcile_exit_command_ledger(
        exit_command_ledger=ledger,
        state={"positions": [position]},
        svc=svc,
        loop_ts=10_000.0,
        position_snapshot_advanced=False,
        position_snapshot_received_at=0.0,
        adaptive_recent_exit_registry=recent_exits,
        campaign_registry=campaigns,
        campaign_transition_counts=transitions,
        campaign_config=CampaignConfig(enabled=True),
        sleeve_tracker=sleeve_tracker,
    )

    assert first["exit_ack_confirmed_count"] == 1
    assert recent_exits["EURUSD"]["command_id"] == command_id
    assert recent_exits["EURUSD"]["confirmation"] == "broker_ack"
    assert sleeve_tracker.snapshot()["trend"].trades == 1
    assert ledger[command_id]["resolved"] is True

    second = runtime_runner._reconcile_exit_command_ledger(
        exit_command_ledger=ledger,
        state={"positions": []},
        svc=svc,
        loop_ts=10_100.0,
        position_snapshot_advanced=True,
        position_snapshot_received_at=10_050.0,
        adaptive_recent_exit_registry=recent_exits,
        campaign_registry=campaigns,
        campaign_transition_counts=transitions,
        campaign_config=CampaignConfig(enabled=True),
        sleeve_tracker=sleeve_tracker,
    )
    assert second["exit_ack_confirmed_count"] == 0
    assert sleeve_tracker.snapshot()["trend"].trades == 1


def test_failed_exit_with_newer_still_open_snapshot_has_no_close_side_effect() -> None:
    position = {
        "symbol": "EURUSD",
        "type": 0,
        "lots": 0.06,
        "open_time": 100,
        "open_price": 1.10,
        "magic": 246810,
    }
    signature = runtime_runner._position_signature(position)
    remaining_position = dict(position, open_time=200, open_price=1.11, lots=0.03)
    remaining_signature = runtime_runner._position_signature(remaining_position)
    ledger = {
        "exit-failed-1": {
            "command_id": "exit-failed-1",
            "position_signature": signature,
            "position_signatures": [signature, remaining_signature],
            "pair": "EURUSD",
            "position_side": "long",
            "submitted_ts": 9_900.0,
            "resolved": False,
        }
    }
    recent_exits: dict[str, dict] = {}
    sleeve_tracker = SleeveGovernanceTracker(sleeves=["trend"])

    diag = runtime_runner._reconcile_exit_command_ledger(
        exit_command_ledger=ledger,
        # The first ticket closed before the EA reported a later-ticket
        # failure.  The campaign must remain open while any intended ticket
        # survives.
        state={"positions": [remaining_position]},
        svc=_CommandService(
            {"status": "failed", "delivered_count": 1, "updated_at": 9_990.0}
        ),
        loop_ts=10_010.0,
        position_snapshot_advanced=True,
        position_snapshot_received_at=10_005.0,
        adaptive_recent_exit_registry=recent_exits,
        campaign_registry={},
        campaign_transition_counts={},
        campaign_config=CampaignConfig(enabled=True),
        sleeve_tracker=sleeve_tracker,
    )

    assert diag["exit_ack_confirmed_count"] == 0
    assert diag["exit_ack_resolved_without_close_count"] == 1
    assert recent_exits == {}
    assert sleeve_tracker.snapshot()["trend"].trades == 0
