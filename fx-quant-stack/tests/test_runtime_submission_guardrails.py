from __future__ import annotations

from types import SimpleNamespace

import fxstack.runtime.runner as runtime_runner


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


def test_finalize_entry_submissions_live_reconciles_missing_governed_preview_pair_and_side() -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [_decision()]

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            _pending_entry(
                orchestration=_orchestration(
                    {
                        "lots": 0.22,
                        "intent": "ENTRY_MODEL",
                        "action": "enter",
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
    assert float(svc.payloads[0]["lots"]) == 0.22


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
