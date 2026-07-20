from __future__ import annotations

from types import SimpleNamespace

import fxstack.runtime.runner as runtime_runner
import pandas as pd
import pytest
from fxstack.strategy.allocator_types import SleeveHealthSnapshot


class _RecordingService:
    def __init__(self, response: dict[str, object]) -> None:
        self._response = dict(response)
        self.payloads: list[dict[str, object]] = []

    def submit_command(self, payload, proto="v2"):
        self.payloads.append(dict(payload))
        out = dict(self._response)
        out.setdefault("action", payload.get("action"))
        out.setdefault("command_id", payload.get("command_id"))
        return out, None

    def record_governance_event(self, **kwargs):  # pragma: no cover - exercised only when fallback telemetry fires
        return None


def _live_settings(*, strategy_engine_mode: str = "supervised_legacy", adaptive_execution_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        agent_mode="live",
        agent_live_pair_allowlist=["EURUSD"],
        agent_live_sleeve_allowlist=["trend"],
        agent_live_intent_allowlist=["enter"],
        agent_decision_timeout_ms=250,
        adaptive_execution_enabled=adaptive_execution_enabled,
        adaptive_shadow_enabled=True,
        strategy_engine_mode=strategy_engine_mode,
        rl_supervised_fallback_required=True,
        min_order_lots=0.01,
        order_lot_step=0.01,
        max_order_lots=0.0,
    )


def _runtime_state(**live_overrides: object) -> dict[str, object]:
    live = {
        "runtime_enabled": True,
        "queue_kill_active": False,
    }
    live.update(live_overrides)
    return {"runtime_diag": {"orchestration_live": live}}


def _decision(
    *,
    execution_ready: bool = True,
    reasons: list[str] | None = None,
    strict_entry_ready: bool | None = None,
    adaptive_shadow_would_trade: bool | None = None,
    adaptive_shadow_rejection_reason: str = "none",
    side: str = "BUY",
) -> dict[str, object]:
    blocking_reasons = list(reasons or [])
    strict_ready = execution_ready if strict_entry_ready is None else bool(strict_entry_ready)
    adaptive_ready = execution_ready if adaptive_shadow_would_trade is None else bool(adaptive_shadow_would_trade)
    rejection_reason = str(blocking_reasons[0] if blocking_reasons else "none")
    return {
        "symbol": "EURUSD",
        "side": str(side),
        "execution_ready": bool(execution_ready),
        "reasons": list(blocking_reasons),
        "metadata": {
            "pair": "EURUSD",
            "strict_entry_ready": bool(strict_ready),
            "strict_entry_blocking_reasons": list(blocking_reasons),
            "strict_rejection_reason": rejection_reason,
            "entry_ready": bool(execution_ready),
            "entry_blocking_reasons": list(blocking_reasons),
            "rejection_reason": rejection_reason,
            "adaptive_shadow_would_trade": bool(adaptive_ready),
            "adaptive_shadow_rejection_reason": str(adaptive_shadow_rejection_reason),
            "lifecycle_action": "entry" if execution_ready else "hold",
            "lifecycle_reason": "entry_approved" if execution_ready else rejection_reason,
            "adaptive_sleeve": "trend",
            "rollout_active": True,
            "rollout_mode": "canary",
            "rollout_pair_allowlisted": True,
            "mt4_fresh": True,
            "ticks_fresh": True,
        },
    }


def _orchestration(command_preview: dict[str, object]) -> dict[str, object]:
    return {
        "enabled": True,
        "correlation_id": "EURUSD:live:test",
        "thread_id": "EURUSD:live:test",
        "run_id": "live-run-test",
        "trace_id": "live-trace-test",
        "latency_ms": 12,
        "fallback_used": False,
        "fault_classification": "",
        "governed_selected_action": "enter",
        "governed_allowed": True,
        "approval_state": "auto",
        "governed_decision": {
            "selected_action": "enter",
            "allowed": True,
            "approval_state": "auto",
            "blocking_reasons": [],
            "command_preview": dict(command_preview),
        },
    }


def _pending_entry(*, orchestration: dict[str, object]) -> dict[str, object]:
    return {
        "index": 0,
        "pair": "EURUSD",
        "ts_value": "2026-04-09T10:00:00Z",
        "action_key": "entry:2026-04-09T10:00:00Z",
        "payload": {"command_id": "baseline-entry", "action": "entry", "symbol": "EURUSD", "lots": 0.10},
        "approved_order": {
            "command_id": "baseline-entry",
            "action": "entry",
            "symbol": "EURUSD",
            "cmd": "BUY",
            "side": "BUY",
            "lots": 0.10,
        },
        "orchestration": dict(orchestration),
    }


def test_finalize_entry_submissions_live_does_not_resurrect_blocked_entry_from_governed_preview() -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [
        _decision(
            execution_ready=False,
            reasons=["low_edge"],
            strict_entry_ready=False,
            adaptive_shadow_would_trade=False,
            adaptive_shadow_rejection_reason="low_edge",
        )
    ]

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            _pending_entry(
                orchestration=_orchestration(
                    {
                        "cmd": "BUY",
                        "side": "BUY",
                        "symbol": "EURUSD",
                        "lots": 0.22,
                        "intent": "ENTRY_MODEL",
                        "action": "enter",
                    }
                )
            )
        ],
        svc=svc,
        last_action_key={},
        settings=_live_settings(adaptive_execution_enabled=False),
        runtime_state=_runtime_state(),
    )

    meta = decisions[0]["metadata"]
    assert svc.payloads == []
    assert diag["approved_entry_count"] == 0
    assert diag["submitted_entry_count"] == 0
    assert diag["live_governed_submitted_count"] == 0
    assert decisions[0]["execution_ready"] is False
    assert decisions[0]["reasons"] == ["low_edge"]
    assert meta["entry_ready"] is False
    assert meta["entry_blocking_reasons"] == ["low_edge"]
    assert meta["enqueue"]["status"] == "skipped"
    assert meta["enqueue"]["reason"] == "low_edge"


def test_finalize_entry_submissions_live_does_not_resurrect_committee_hold() -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [_decision()]
    orchestration = _orchestration({})
    orchestration["governed_selected_action"] = "hold"
    orchestration["governed_allowed"] = False
    governed = orchestration["governed_decision"]
    assert isinstance(governed, dict)
    governed.update(
        {
            "selected_action": "hold",
            "allowed": False,
            "blocking_reasons": ["committee_veto"],
            "command_preview": {},
        }
    )

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[_pending_entry(orchestration=orchestration)],
        svc=svc,
        last_action_key={},
        settings=_live_settings(),
        runtime_state=_runtime_state(),
    )

    meta = decisions[0]["metadata"]
    assert svc.payloads == []
    assert diag["approved_entry_count"] == 0
    assert diag["submitted_entry_count"] == 0
    assert diag["live_baseline_fallback_count"] == 0
    assert diag["live_governed_blocked_count"] == 1
    assert meta["orchestration_live_command_source"] == "governed_live_blocked"
    assert meta["enqueue"]["status"] == "skipped"
    assert meta["enqueue"]["reason"] == "committee_veto"


@pytest.mark.parametrize(
    ("snapshots", "expected_reason"),
    [
        ({}, "sleeve_governance_unavailable"),
        (
            {"trend": SleeveHealthSnapshot(sleeve="trend", score=0.30, state="degraded", trades=5)},
            "sleeve_governance_degraded",
        ),
        (
            {"trend": SleeveHealthSnapshot(sleeve="other", score=0.60, state="healthy")},
            "sleeve_governance_mismatch",
        ),
        (
            {"trend": SleeveHealthSnapshot(sleeve="trend", score=float("nan"), state="healthy")},
            "sleeve_governance_invalid",
        ),
        (
            {"trend": SleeveHealthSnapshot(sleeve="trend", score=0.60, state="unknown")},
            "sleeve_governance_invalid",
        ),
    ],
)
def test_finalize_entry_submissions_sleeve_hard_block_dominates_both_ready_paths(
    snapshots: dict[str, SleeveHealthSnapshot],
    expected_reason: str,
) -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [_decision(strict_entry_ready=True, adaptive_shadow_would_trade=True)]

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[_pending_entry(orchestration=_orchestration({"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}))],
        svc=svc,
        last_action_key={},
        settings=_live_settings(),
        runtime_state=_runtime_state(),
        sleeve_health_snapshots=snapshots,
        enforce_sleeve_governance=True,
    )

    assert svc.payloads == []
    assert diag["sleeve_governance_enforced"] is True
    assert diag["sleeve_governance_blocked_count"] == 1
    assert decisions[0]["execution_ready"] is False
    assert decisions[0]["reasons"] == [expected_reason]
    assert decisions[0]["metadata"]["enqueue"]["reason"] == expected_reason


def test_finalize_entry_submissions_sleeve_watch_keeps_soft_strict_fallback() -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [
        _decision(
            strict_entry_ready=True,
            adaptive_shadow_would_trade=False,
            adaptive_shadow_rejection_reason="allocator_ranked_out",
        )
    ]

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[_pending_entry(orchestration=_orchestration({"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}))],
        svc=svc,
        last_action_key={},
        settings=_live_settings(),
        runtime_state=_runtime_state(),
        sleeve_health_snapshots={
            "trend": SleeveHealthSnapshot(sleeve="trend", score=0.48, state="watch", trades=5)
        },
        enforce_sleeve_governance=True,
    )

    assert diag["sleeve_governance_blocked_count"] == 0
    assert diag["live_governed_submitted_count"] == 1
    assert len(svc.payloads) == 1


@pytest.mark.parametrize(
    "hard_reason",
    [
        "cross_pair_hard_gate",
        "adaptive_reentry_cooldown",
        "campaign_abandon_cooldown",
        "overlay_low_conviction",
        "overlay_stand_down",
    ],
)
def test_finalize_entry_submissions_adaptive_hard_veto_dominates_strict_fallback(hard_reason: str) -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [
        _decision(
            strict_entry_ready=True,
            adaptive_shadow_would_trade=False,
            adaptive_shadow_rejection_reason=hard_reason,
        )
    ]

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[_pending_entry(orchestration=_orchestration({"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}))],
        svc=svc,
        last_action_key={},
        settings=_live_settings(),
        runtime_state=_runtime_state(),
    )

    assert svc.payloads == []
    assert diag["approved_entry_count"] == 0
    assert decisions[0]["execution_ready"] is False
    assert decisions[0]["reasons"] == [hard_reason]


@pytest.mark.parametrize("hard_reason", ["overlay_low_conviction", "overlay_stand_down"])
def test_finalize_entry_submissions_hard_overlay_veto_binds_when_adaptive_execution_is_disabled(
    hard_reason: str,
) -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [
        _decision(
            strict_entry_ready=True,
            adaptive_shadow_would_trade=False,
            adaptive_shadow_rejection_reason=hard_reason,
        )
    ]

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[_pending_entry(orchestration=_orchestration({"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}))],
        svc=svc,
        last_action_key={},
        settings=_live_settings(adaptive_execution_enabled=False),
        runtime_state=_runtime_state(),
    )

    assert svc.payloads == []
    assert diag["approved_entry_count"] == 0
    assert decisions[0]["reasons"] == [hard_reason]


def test_finalize_entry_submissions_sleeve_veto_binds_when_adaptive_execution_is_disabled() -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [_decision(strict_entry_ready=True)]

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[_pending_entry(orchestration=_orchestration({"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}))],
        svc=svc,
        last_action_key={},
        settings=_live_settings(adaptive_execution_enabled=False),
        runtime_state=_runtime_state(),
        sleeve_health_snapshots={
            "trend": SleeveHealthSnapshot(sleeve="trend", score=0.20, state="degraded", trades=5)
        },
        enforce_sleeve_governance=True,
    )

    assert svc.payloads == []
    assert diag["sleeve_governance_enforced"] is True
    assert diag["sleeve_governance_blocked_count"] == 1
    assert decisions[0]["reasons"] == ["sleeve_governance_degraded"]


def test_finalize_entry_submissions_live_uses_risk_approved_payload_not_governed_preview_fields() -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [_decision()]

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            _pending_entry(
                orchestration=_orchestration(
                    {
                        "lots": 0.22,
                        "intent": "UNTRUSTED_PREVIEW_INTENT",
                        "action": "untrusted_preview_action",
                        "command_id": "untrusted-preview-command",
                        "magic": 999999,
                        "tp_cash": 999999.0,
                    }
                )
            )
        ],
        svc=svc,
        last_action_key={},
        settings=_live_settings(),
        runtime_state=_runtime_state(),
    )

    assert diag["live_governed_submitted_count"] == 1
    assert svc.payloads[0]["symbol"] == "EURUSD"
    assert svc.payloads[0]["cmd"] == "BUY"
    assert svc.payloads[0]["side"] == "BUY"
    assert float(svc.payloads[0]["lots"]) == 0.10
    assert svc.payloads[0]["command_id"] == "baseline-entry"
    assert "magic" not in svc.payloads[0]
    assert "tp_cash" not in svc.payloads[0]


def test_finalize_entry_submissions_duplicate_queue_response_does_not_mutate_live_submission_state() -> None:
    svc = _RecordingService({"status": "duplicate", "state": "acked"})
    decisions = [_decision()]
    last_action_key: dict[str, str] = {}
    live_entry_registry: dict[str, dict[str, object]] = {}
    seen_live_entry_keys: set[tuple[str, str]] = set()

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            _pending_entry(
                orchestration=_orchestration(
                    {
                        "cmd": "BUY",
                        "side": "BUY",
                        "symbol": "EURUSD",
                        "lots": 0.22,
                        "intent": "ENTRY_MODEL",
                        "action": "enter",
                    }
                )
            )
        ],
        svc=svc,
        last_action_key=last_action_key,
        settings=_live_settings(),
        runtime_state=_runtime_state(),
        adaptive_pending_entry_registry=live_entry_registry,
        adaptive_seen_live_entry_keys=seen_live_entry_keys,
        current_equity=25_000.0,
    )

    assert decisions[0]["metadata"]["enqueue"]["status"] == "duplicate"
    assert last_action_key == {}
    assert live_entry_registry == {}
    assert seen_live_entry_keys == set()
    assert diag["submitted_entry_count"] == 1
    assert diag["submitted_live_entry_count"] == 0
    assert diag["submitted_live_entry_pairs"] == []
    assert diag["live_governed_submitted_count"] == 0


def test_finalize_entry_submissions_rl_blocked_entry_count_reflects_final_blocked_outcome() -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [_decision()]

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            _pending_entry(
                orchestration=_orchestration(
                    {
                        "cmd": "BUY",
                        "side": "BUY",
                        "symbol": "EURUSD",
                        "lots": 0.22,
                        "intent": "ENTRY_MODEL",
                        "action": "enter",
                    }
                )
            )
        ],
        svc=svc,
        last_action_key={},
        settings=_live_settings(strategy_engine_mode="rl_primary"),
        runtime_state=_runtime_state(queue_kill_active=True),
        rl_portfolio_proposal={
            "source": "rl_checkpoint",
            "checkpoint_loaded": True,
            "proposals_by_pair": {
                "EURUSD": {
                    "source": "rl_checkpoint",
                    "supervised_fallback_used": False,
                    "action": {
                        "target_position": 0.75,
                        "close_position": False,
                        "metadata": {"entry_supported": True},
                    },
                }
            },
        },
    )

    assert svc.payloads == []
    assert diag["approved_entry_count"] == 0
    assert diag["blocked_entry_count"] == 1
    assert diag["rl_routed_entry_count"] == 1
    assert diag["rl_blocked_entry_count"] == 1
    assert decisions[0]["metadata"]["enqueue"]["status"] == "skipped"
    assert decisions[0]["metadata"]["enqueue"]["reason"] == "live_queue_killed"


@pytest.mark.parametrize("failure_reason", ["model_inference_error:RuntimeError", "no_features:H4,D,M5"])
def test_open_position_hard_stop_survives_entry_pipeline_failure(failure_reason: str) -> None:
    loop_ts = 1_800_000_000.0
    settings = SimpleNamespace(
        agent_mode="live",
        hard_time_stop_secs=60.0,
        enable_lifecycle_actions=True,
        enable_adjust_actions=False,
        adjust_stop_buffer_pips=0.0,
        lifecycle_model_action_min_prob=0.5,
        max_allowed_spread_bps=3.0,
        max_total_positions=6,
        max_pair_positions=1,
        min_order_lots=0.01,
        order_lot_step=0.01,
        max_order_lots=0.5,
        agent_live_pair_allowlist=["EURUSD"],
        agent_live_sleeve_allowlist=[],
        agent_live_intent_allowlist=["exit"],
        agent_decision_timeout_ms=250,
    )
    state = {
        "equity": 10_000.0,
        "balance": 10_000.0,
        "positions": [
            {
                "symbol": "EURUSD",
                "side": "long",
                "lots": 0.1,
                "open_time": loop_ts - 3600.0,
                "ticket": 12345,
            }
        ],
        "governance": {},
    }
    decisions: list[dict[str, object]] = []
    pending_actions: list[dict[str, object]] = []

    runtime_runner._append_failed_pair_decision_with_fail_safe(
        decisions=decisions,
        pending_position_actions=pending_actions,
        pair="EURUSD",
        failure_reason=failure_reason,
        state=state,
        tick={"bid": 1.1010, "ask": 1.1012, "digits": 5},
        loop_ts=loop_ts,
        settings=settings,
    )

    assert len(decisions) == 1
    assert decisions[0]["execution_ready"] is False
    assert decisions[0]["reasons"] == [failure_reason]
    assert len(pending_actions) == 1
    assert pending_actions[0]["lifecycle_action"] == "exit"
    assert pending_actions[0]["lifecycle_reason"] == "hard_time_stop"
    assert pending_actions[0]["approved_order"]["cmd"] == "CLOSE"
    final_risk = runtime_runner._reapprove_final_position_actions(
        decisions=decisions,
        pending_position_actions=pending_actions,
        settings=settings,
    )
    assert final_risk["approved_count"] == 1
    decisions[0]["metadata"].update(
        {
            "rollout_active": True,
            "rollout_mode": "canary",
            "rollout_pair_allowlisted": True,
            "mt4_fresh": True,
            "ticks_fresh": True,
        }
    )
    pending_actions[0]["orchestration"] = {
        "enabled": True,
        "correlation_id": "EURUSD:live:fail-safe-exit",
        "thread_id": "EURUSD:live:fail-safe-exit",
        "run_id": "live-fail-safe-run",
        "trace_id": "live-fail-safe-trace",
        "latency_ms": 12,
        "fallback_used": False,
        "fault_classification": "",
        "governed_selected_action": "exit",
        "governed_allowed": True,
        "approval_state": "auto",
        "governed_decision": {
            "selected_action": "exit",
            "allowed": True,
            "approval_state": "auto",
            "blocking_reasons": [],
            "command_preview": {"cmd": "CLOSE", "symbol": "EURUSD", "action": "exit"},
        },
    }

    svc = _RecordingService({"status": "queued", "command_id": "fail-safe-close"})
    diag = runtime_runner._submit_position_actions(
        decisions=decisions,
        pending_position_actions=pending_actions,
        svc=svc,
        settings=settings,
        runtime_state={
            "runtime_diag": {
                "orchestration_live": {
                    "runtime_enabled": True,
                    "queue_kill_active": False,
                    "active_pair_scope": ["EURUSD"],
                    "active_sleeve_scope": [],
                    "active_intent_scope": ["exit"],
                }
            }
        },
        last_action_key={},
        partial_close_tracker={},
        adaptive_position_registry={},
        adaptive_recent_exit_registry={},
        pair_bar_index={"EURUSD": 1},
        loop_ts=loop_ts,
    )

    assert diag["submitted_position_action_count"] == 1
    assert diag["submitted_exit_count"] == 1
    assert len(svc.payloads) == 1
    assert svc.payloads[0]["cmd"] == "CLOSE"


@pytest.mark.parametrize("failure_mode", ["committee_hold", "committee_fault"])
def test_live_lifecycle_submission_is_blocked_by_committee_hold_or_fault(failure_mode: str) -> None:
    settings = _live_settings()
    settings.agent_live_intent_allowlist = ["exit"]
    decisions = [_decision()]
    decisions[0]["metadata"]["lifecycle_action"] = "exit"
    decisions[0]["metadata"]["lifecycle_reason"] = "close_signal"
    orchestration = _orchestration({"cmd": "CLOSE", "symbol": "EURUSD", "action": "exit"})
    orchestration["governed_selected_action"] = "exit"
    governed = orchestration["governed_decision"]
    assert isinstance(governed, dict)
    governed["selected_action"] = "exit"
    if failure_mode == "committee_hold":
        orchestration["governed_selected_action"] = "hold"
        orchestration["governed_allowed"] = False
        governed.update({"selected_action": "hold", "allowed": False, "blocking_reasons": ["committee_veto"]})
    else:
        orchestration["fault_classification"] = "committee_timeout"

    pending_actions = [
        {
            "index": 0,
            "pair": "EURUSD",
            "ts_value": "2026-04-09T10:00:00Z",
            "position_side": "long",
            "lifecycle_action": "exit",
            "lifecycle_reason": "close_signal",
            "lifecycle_action_score": 0.9,
            "position_signature": "ticket:123",
            "approved_order": {
                "cmd": "CLOSE",
                "symbol": "EURUSD",
                "lots": 0.0,
                "close_lots": 0.0,
                "intent": "EXIT_MODEL",
                "action": "exit",
            },
            "final_risk_approved": True,
            "orchestration": orchestration,
        }
    ]
    svc = _RecordingService({"status": "queued"})

    diag = runtime_runner._submit_position_actions(
        decisions=decisions,
        pending_position_actions=pending_actions,
        svc=svc,
        settings=settings,
        runtime_state=_runtime_state(),
        last_action_key={},
        partial_close_tracker={},
        adaptive_position_registry={},
        adaptive_recent_exit_registry={},
        pair_bar_index={"EURUSD": 1},
        loop_ts=1_800_000_000.0,
    )

    assert svc.payloads == []
    assert diag["submitted_position_action_count"] == 0
    expected_reason = "committee_veto" if failure_mode == "committee_hold" else "live_shadow_fault"
    assert decisions[0]["metadata"]["enqueue"]["reason"] == expected_reason


def test_live_lifecycle_post_risk_action_mutation_cannot_reuse_stale_approval() -> None:
    settings = _live_settings()
    settings.agent_live_intent_allowlist = ["reduce"]
    decisions = [_decision()]
    pending_actions = [
        {
            "index": 0,
            "pair": "EURUSD",
            "ts_value": "2026-04-09T10:00:00Z",
            "position_side": "long",
            # The final intent was mutated after an EXIT approval.
            "lifecycle_action": "partial_tp",
            "lifecycle_reason": "rl_resize_down",
            "lifecycle_action_score": 0.8,
            "close_lots": 0.05,
            "position_signature": "ticket:123",
            "approved_order": {
                "cmd": "CLOSE",
                "symbol": "EURUSD",
                "lots": 0.0,
                "close_lots": 0.0,
                "intent": "EXIT_MODEL",
                "action": "exit",
            },
            "final_risk_approved": True,
            "orchestration": _orchestration(
                {"cmd": "CLOSE_PARTIAL", "symbol": "EURUSD", "close_lots": 0.05, "action": "partial_tp"}
            ),
        }
    ]
    svc = _RecordingService({"status": "queued"})

    diag = runtime_runner._submit_position_actions(
        decisions=decisions,
        pending_position_actions=pending_actions,
        svc=svc,
        settings=settings,
        runtime_state=_runtime_state(),
        last_action_key={},
        partial_close_tracker={},
        adaptive_position_registry={},
        adaptive_recent_exit_registry={},
        pair_bar_index={"EURUSD": 1},
        loop_ts=1_800_000_000.0,
    )

    assert svc.payloads == []
    assert diag["submitted_position_action_count"] == 0
    assert decisions[0]["metadata"]["enqueue"]["reason"] == "final_lifecycle_risk_payload_mismatch"


def test_exit_model_still_runs_when_entry_scorer_failed(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_runner,
        "_score_exit_policy_model",
        lambda *args, **kwargs: {"selected": "exit", "score": 0.91, "probs": {"exit": 0.91}},
    )
    action = runtime_runner._independent_position_fail_safe_action(
        positions=[{"symbol": "EURUSD", "side": "long", "lots": 0.1, "open_time": 1_799_999_900.0}],
        loop_ts=1_800_000_000.0,
        tick={"bid": 1.1010, "ask": 1.1012, "digits": 5},
        settings=SimpleNamespace(
            hard_time_stop_secs=0.0,
            enable_lifecycle_actions=True,
            lifecycle_model_action_min_prob=0.5,
            enable_adjust_actions=False,
        ),
        loaded=SimpleNamespace(exit_model=object(), exit_action_labels={0: "hold", 1: "partial_tp", 2: "exit"}),
        intraday_row=pd.DataFrame([{"ts": "2027-01-15T08:00:00Z", "mid_close": 1.1011, "atr_14": 0.0008}]),
        intraday_timeframe="M5",
        total_position_count=1,
    )

    assert action["lifecycle_action"] == "exit"
    assert action["lifecycle_reason"] == "exit_model_exit_after_entry_inference_error"
    assert action["lifecycle_action_score"] == 0.91


@pytest.mark.parametrize("side", ["long", "short"])
def test_independent_fail_safe_adjust_stop_is_strictly_monotonic(side: str) -> None:
    settings = SimpleNamespace(
        hard_time_stop_secs=0.0,
        enable_lifecycle_actions=False,
        enable_adjust_actions=True,
        adjust_stop_buffer_pips=5.0,
    )
    tick = {"bid": 1.1010, "ask": 1.1012, "digits": 5}
    position = {"symbol": "EURUSD", "side": side, "lots": 0.1, "open_time": 1_799_999_900.0}

    without_current_stop = runtime_runner._independent_position_fail_safe_action(
        positions=[position],
        loop_ts=1_800_000_000.0,
        tick=tick,
        settings=settings,
    )

    assert without_current_stop["lifecycle_action"] == "tighten_stop"
    proposed_sl = float(without_current_stop["sl_price"])
    tighter_current_sl = proposed_sl - 0.0001 if side == "long" else proposed_sl + 0.0001
    widening_current_sl = proposed_sl + 0.0001 if side == "long" else proposed_sl - 0.0001

    for current_sl, expected_action in (
        (tighter_current_sl, "tighten_stop"),
        (proposed_sl, "hold"),
        (widening_current_sl, "hold"),
    ):
        action = runtime_runner._independent_position_fail_safe_action(
            positions=[{**position, "sl": current_sl}],
            loop_ts=1_800_000_000.0,
            tick=tick,
            settings=settings,
        )
        assert action["lifecycle_action"] == expected_action
