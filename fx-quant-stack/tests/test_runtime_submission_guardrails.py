from __future__ import annotations

from types import SimpleNamespace

import fxstack.runtime.runner as runtime_runner
import pandas as pd
import pytest
from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.runtime import orchestration_bridge
from fxstack.strategy.allocator_types import SleeveHealthSnapshot


class _RecordingService:
    def __init__(self, response: dict[str, object]) -> None:
        self._response = dict(response)
        self.payloads: list[dict[str, object]] = []
        self.approvals: list[object] = []

    def submit_command(self, payload, proto="v2"):
        self.payloads.append(dict(payload))
        out = dict(self._response)
        out.setdefault("action", payload.get("action"))
        out.setdefault("command_id", payload.get("command_id"))
        return out, None

    def submit_approved_command(self, payload, *, approval, proto="v2"):
        self.approvals.append(approval)
        return self.submit_command(payload, proto=proto)

    def record_governance_event(self, **kwargs):  # pragma: no cover - exercised only when fallback telemetry fires
        return None


def _live_settings(*, strategy_engine_mode: str = "supervised_legacy", adaptive_execution_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        agent_mode="live",
        agent_live_pair_allowlist=["EURUSD"],
        agent_live_sleeve_allowlist=["trend"],
        agent_live_intent_allowlist=["enter"],
        agent_decision_timeout_ms=250,
        live_expected_account_mode="demo",
        adaptive_execution_enabled=adaptive_execution_enabled,
        strategy_engine_mode=strategy_engine_mode,
        rl_supervised_fallback_required=True,
        min_order_lots=0.01,
        order_lot_step=0.01,
        max_order_lots=0.0,
    )


def _runtime_state(**live_overrides: object) -> dict[str, object]:
    live = {
        "authority_revision": 1,
        "enabled": True,
        "mode": "live",
        "runtime_enabled": True,
        "queue_kill_active": False,
    }
    live.update(live_overrides)
    return {
        "broker_account_mode": "demo",
        "broker_account_scope": "demo-account-scope",
        "runtime_diag": {"orchestration_live": live},
    }


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
            "adaptive_selected": bool(adaptive_ready),
            "adaptive_rejection_reason": str(adaptive_shadow_rejection_reason),
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


def _final_entry_risk_result(*, cmd: str = "BUY") -> dict[str, object]:
    order = {
        "command_id": "canonical-entry",
        "action": "entry",
        "intent": "ENTRY",
        "symbol": "EURUSD",
        "cmd": str(cmd),
        "side": str(cmd),
        "lots": 0.10,
        "sl_price": 1.09,
        "tp_price": 1.12,
    }
    return {
        "approved_order": order,
        "verdict": "allow",
        "reason": "approved",
        "trace": [{"rule": "portfolio_cap", "verdict": "allow"}],
        "decision": {"verdict": "allow"},
        "rollout": {
            "mode": "canary",
            "active": True,
            "pair_allowlisted": True,
            "budget_scale": 0.25,
        },
        "portfolio_allocation": {"allowed": True},
        "portfolio_budget_scale": 1.0,
        "capital_budget_scale": 1.0,
        "governance": {},
    }


def test_post_adaptive_entry_reapproval_makes_recoverable_candidate_canonical_before_committee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = [
        _decision(
            execution_ready=False,
            reasons=["low_trade_prob"],
            strict_entry_ready=False,
            adaptive_shadow_would_trade=True,
        )
    ]
    decisions[0]["metadata"].update(
        {"trade_prob": 0.58, "allocator_rank": 1, "allocator_score": 0.82}
    )
    pending = {
        "index": 0,
        "pair": "EURUSD",
        "ts_value": "2026-04-09T10:00:00Z",
        "action_key": "entry:2026-04-09T10:00:00Z",
        "sl_price": 1.09,
        "tp_price": 1.12,
        "risk_reapproval_context": {"pair": "EURUSD"},
    }
    captured: dict[str, object] = {}

    def _risk(**kwargs):
        captured.update(kwargs)
        return _final_entry_risk_result()

    monkeypatch.setattr(runtime_runner, "_evaluate_runtime_risk_kernel", _risk)

    diag = runtime_runner._reapprove_final_entry_intents(
        decisions=decisions,
        pending_entries=[pending],
        settings=_live_settings(),
    )

    assert diag["adaptive_approved_count"] == 1
    assert captured["rejection_reasons"] == []
    assert captured["pending_entries"] == []
    assert pending["portfolio_slot_reserved"] is True
    assert decisions[0]["execution_ready"] is True
    assert decisions[0]["reasons"] == []
    assert decisions[0]["metadata"]["canonical_entry_ready"] is True
    baseline = orchestration_bridge.orchestration_baseline_action(
        decision=decisions[0],
        pending_entry=pending,
        pending_position_action=None,
    )
    assert baseline["action"] == "enter"
    assert baseline["command_preview"]["cmd"] == "BUY"


def test_intelligent_reapproval_recovers_strategy_reason_and_scales_lots_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = [
        _decision(
            execution_ready=False,
            reasons=["low_trade_prob"],
            strict_entry_ready=False,
            adaptive_shadow_would_trade=True,
        )
    ]
    decisions[0]["metadata"].update(
        {
            "trade_prob": 0.30,
            "allocator_rank": 1,
            "adaptive_entry_mode": "intelligent_utility",
            "adaptive_size_scale": 0.30,
            "adaptive_recovered_strict_reasons": ["low_trade_prob"],
        }
    )
    pending = {
        "index": 0,
        "pair": "EURUSD",
        "ts_value": "2026-04-09T10:00:00Z",
        "sl_price": 1.09,
        "tp_price": 1.12,
        "risk_reapproval_context": {
            "pair": "EURUSD",
            "planned_entry_lots": 0.10,
        },
    }
    captured: dict[str, object] = {}

    def _risk(**kwargs):
        captured.update(kwargs)
        return _final_entry_risk_result()

    monkeypatch.setattr(runtime_runner, "_evaluate_runtime_risk_kernel", _risk)
    settings = _live_settings()

    diag = runtime_runner._reapprove_final_entry_intents(
        decisions=decisions,
        pending_entries=[pending],
        settings=settings,
    )

    assert diag["adaptive_approved_count"] == 1
    assert captured["rejection_reasons"] == []
    assert captured["planned_entry_lots"] == pytest.approx(0.03)
    meta = decisions[0]["metadata"]
    assert meta["intelligent_size_scale"] == pytest.approx(0.30)
    assert meta["intelligent_planned_lots_before_scale"] == pytest.approx(0.10)
    assert meta["intelligent_planned_lots_after_scale"] == pytest.approx(0.03)


def test_intelligent_reapproval_can_override_weak_direction_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = [
        _decision(
            execution_ready=False,
            reasons=["low_swing_prob"],
            strict_entry_ready=False,
            adaptive_shadow_would_trade=True,
        )
    ]
    pending = {
        "index": 0,
        "pair": "EURUSD",
        "ts_value": "2026-04-09T10:00:00Z",
        "risk_reapproval_context": {"pair": "EURUSD", "planned_entry_lots": 0.10},
    }
    monkeypatch.setattr(runtime_runner, "_evaluate_runtime_risk_kernel", lambda **_kwargs: _final_entry_risk_result())

    diag = runtime_runner._reapprove_final_entry_intents(
        decisions=decisions,
        pending_entries=[pending],
        settings=_live_settings(),
    )

    assert diag["approved_count"] == 1
    assert decisions[0]["reasons"] == []
    assert decisions[0]["metadata"]["final_entry_source"] == "intelligent"


def test_post_adaptive_entry_reapproval_preserves_non_model_safety_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = [
        _decision(
            execution_ready=False,
            reasons=["low_trade_prob", "governance_paused"],
            strict_entry_ready=False,
            adaptive_shadow_would_trade=True,
        )
    ]
    pending = {
        "index": 0,
        "pair": "EURUSD",
        "ts_value": "2026-04-09T10:00:00Z",
        "risk_reapproval_context": {"pair": "EURUSD"},
    }
    monkeypatch.setattr(
        runtime_runner,
        "_evaluate_runtime_risk_kernel",
        lambda **_kwargs: pytest.fail("hard safety veto must not reach reapproval"),
    )

    diag = runtime_runner._reapprove_final_entry_intents(
        decisions=decisions,
        pending_entries=[pending],
        settings=_live_settings(),
    )

    assert diag["approved_count"] == 0
    assert decisions[0]["reasons"] == ["governance_paused"]
    assert pending["portfolio_slot_reserved"] is False


def test_strict_reapproval_ignores_observation_only_sleeve_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = [_decision(strict_entry_ready=True)]
    pending = {
        "index": 0,
        "pair": "EURUSD",
        "ts_value": "2026-04-09T10:00:00Z",
        "action_key": "entry:2026-04-09T10:00:00Z",
        "sl_price": 1.09,
        "tp_price": 1.12,
        "risk_reapproval_context": {"pair": "EURUSD"},
    }
    monkeypatch.setattr(
        runtime_runner,
        "_evaluate_runtime_risk_kernel",
        lambda **_kwargs: _final_entry_risk_result(),
    )

    diag = runtime_runner._reapprove_final_entry_intents(
        decisions=decisions,
        pending_entries=[pending],
        settings=_live_settings(adaptive_execution_enabled=False),
        sleeve_health_snapshots={
            "trend": SleeveHealthSnapshot(
                sleeve="trend",
                score=0.20,
                state="degraded",
                trades=5,
            )
        },
        enforce_sleeve_governance=True,
    )

    assert diag["approved_count"] == 1
    assert decisions[0]["metadata"]["sleeve_governance_enforced"] is False
    assert decisions[0]["metadata"]["canonical_entry_ready"] is True
    assert pending["portfolio_slot_reserved"] is True


def test_adaptive_allocator_ranked_out_strict_candidate_stays_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = [
        _decision(
            execution_ready=True,
            strict_entry_ready=True,
            adaptive_shadow_would_trade=True,
        ),
        _decision(
            execution_ready=True,
            strict_entry_ready=True,
            adaptive_shadow_would_trade=False,
            adaptive_shadow_rejection_reason="allocator_capacity",
        ),
    ]
    decisions[0]["metadata"].update({"allocator_rank": 1, "allocator_score": 0.9})
    decisions[1]["metadata"].update({"allocator_rank": 2, "allocator_score": 0.8})
    pending_entries = [
        {
            "index": 0,
            "pair": "EURUSD",
            "sl_price": 1.09,
            "tp_price": 1.12,
            "risk_reapproval_context": {"pair": "EURUSD"},
        },
        {
            "index": 1,
            "pair": "GBPUSD",
            "sl_price": 1.25,
            "tp_price": 1.28,
            "risk_reapproval_context": {"pair": "GBPUSD"},
        },
    ]
    risk_pairs: list[str] = []

    def _risk(**kwargs):
        risk_pairs.append(str(kwargs.get("pair") or ""))
        return _final_entry_risk_result()

    monkeypatch.setattr(runtime_runner, "_evaluate_runtime_risk_kernel", _risk)

    diag = runtime_runner._reapprove_final_entry_intents(
        decisions=decisions,
        pending_entries=pending_entries,
        settings=_live_settings(),
    )

    assert risk_pairs == ["EURUSD"]
    assert diag["approved_count"] == 1
    assert decisions[0]["execution_ready"] is True
    assert pending_entries[0]["portfolio_slot_reserved"] is True
    assert decisions[1]["execution_ready"] is False
    assert decisions[1]["reasons"] == ["allocator_capacity"]
    assert pending_entries[1]["portfolio_slot_reserved"] is False


def test_non_adaptive_reapproval_preserves_original_pending_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = [
        _decision(adaptive_shadow_would_trade=True),
        _decision(adaptive_shadow_would_trade=False),
    ]
    decisions[0]["metadata"].update({"allocator_rank": 2, "allocator_score": 0.7})
    decisions[1]["symbol"] = "GBPUSD"
    decisions[1]["metadata"].update(
        {"pair": "GBPUSD", "allocator_rank": 1, "allocator_score": 0.9}
    )
    pending_entries = [
        {
            "index": 0,
            "pair": "EURUSD",
            "sl_price": 1.09,
            "tp_price": 1.12,
            "risk_reapproval_context": {"pair": "EURUSD"},
        },
        {
            "index": 1,
            "pair": "GBPUSD",
            "sl_price": 1.25,
            "tp_price": 1.28,
            "risk_reapproval_context": {"pair": "GBPUSD"},
        },
    ]
    risk_pairs: list[str] = []

    def _risk(**kwargs):
        pair = str(kwargs.get("pair") or "")
        risk_pairs.append(pair)
        result = _final_entry_risk_result()
        result["approved_order"] = {
            **dict(result["approved_order"]),
            "symbol": pair,
        }
        return result

    monkeypatch.setattr(runtime_runner, "_evaluate_runtime_risk_kernel", _risk)

    runtime_runner._reapprove_final_entry_intents(
        decisions=decisions,
        pending_entries=pending_entries,
        settings=_live_settings(adaptive_execution_enabled=False),
    )

    assert risk_pairs == ["EURUSD", "GBPUSD"]


def test_portfolio_slot_reservations_exclude_candidates_without_final_risk_approval() -> None:
    reserved = {
        "pair": "EURUSD",
        "portfolio_slot_reserved": True,
        "risk_approved_order": {"cmd": "BUY", "lots": 0.1},
    }
    assert runtime_runner._portfolio_slot_reservations(
        [
            {"pair": "GBPUSD", "payload": {}, "approved_order": {}},
            {
                "pair": "USDJPY",
                "portfolio_slot_reserved": False,
                "approved_order": {"cmd": "BUY", "lots": 0.1},
            },
            reserved,
        ]
    ) == [reserved]


def test_adaptive_entry_submission_order_preserves_allocator_priority() -> None:
    decisions = [
        {"metadata": {"canonical_entry_ready": True, "allocator_rank": 2, "allocator_score": 0.7}},
        {"metadata": {"canonical_entry_ready": True, "allocator_rank": 1, "allocator_score": 0.9}},
        {"metadata": {"canonical_entry_ready": False, "allocator_rank": 0, "allocator_score": 0.0}},
    ]
    pending_entries = [
        {"index": 0, "pair": "GBPUSD"},
        {"index": 2, "pair": "USDJPY"},
        {"index": 1, "pair": "EURUSD"},
    ]

    ordered = runtime_runner._ordered_pending_entries_for_submission(
        decisions=decisions,
        pending_entries=pending_entries,
        adaptive_mode=True,
    )

    assert [item["pair"] for item in ordered] == ["EURUSD", "GBPUSD", "USDJPY"]


def test_finalizer_cannot_resurrect_a_blocked_canonical_entry() -> None:
    svc = _RecordingService({"status": "queued"})
    decision = _decision(
        execution_ready=False,
        reasons=["final_entry_risk_blocked"],
        strict_entry_ready=False,
        adaptive_shadow_would_trade=True,
    )
    decision["metadata"].update(
        {
            "canonical_entry_ready": False,
            "canonical_entry_blocking_reasons": ["final_entry_risk_blocked"],
            "canonical_entry_rejection_reason": "final_entry_risk_blocked",
        }
    )

    diag = runtime_runner._finalize_entry_submissions(
        decisions=[decision],
        pending_entries=[
            _pending_entry(
                orchestration=_orchestration(
                    {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}
                )
            )
        ],
        svc=svc,
        last_action_key={},
        settings=_live_settings(),
        runtime_state=_runtime_state(),
    )

    assert diag["approved_entry_count"] == 0
    assert svc.payloads == []
    assert decision["reasons"] == ["final_entry_risk_blocked"]


def test_live_startup_admission_requires_explicit_active_canary_and_protective_intents() -> None:
    settings = SimpleNamespace(
        agent_mode="live",
        pairs=["EURUSD"],
        agent_live_pair_allowlist=["EURUSD"],
        agent_live_sleeve_allowlist=["trend", "range"],
        agent_live_intent_allowlist=["enter"],
        enable_lifecycle_actions=True,
        enable_adjust_actions=True,
    )
    blocked = runtime_runner._live_command_admission_diagnostics(
        settings=settings,
        model_sets={
            "EURUSD": SimpleNamespace(
                model_set_id="candidate",
                rollout_policy={
                    "configured": False,
                    "active": False,
                    "mode": "",
                    "pair_allowlisted": False,
                    "budget_scale": 1.0,
                },
            )
        },
    )

    assert blocked["allowed"] is False
    assert "missing_live_intents:exit,reduce,tighten_stop" in blocked["blockers"]
    assert "EURUSD:rollout_not_configured" in blocked["blockers"]
    assert "EURUSD:rollout_inactive" in blocked["blockers"]

    settings.agent_live_intent_allowlist = [
        "enter",
        "exit",
        "reduce",
        "tighten_stop",
    ]
    ready = runtime_runner._live_command_admission_diagnostics(
        settings=settings,
        model_sets={
            "EURUSD": SimpleNamespace(
                model_set_id="candidate",
                rollout_policy={
                    "configured": True,
                    "active": True,
                    "mode": "canary",
                    "pair_allowlisted": True,
                    "budget_scale": 0.25,
                    "source": "main_runtime_rollout",
                },
            )
        },
    )

    assert ready["allowed"] is True
    assert ready["status"] == "ready"


def test_scalp_live_startup_admission_requires_only_implemented_lifecycle_intents() -> None:
    settings = SimpleNamespace(
        agent_mode="live",
        entry_strategy_family="mtvclc",
        pairs=list(IG_MT4_SCALP_SYMBOLS),
        agent_live_pair_allowlist=list(IG_MT4_SCALP_SYMBOLS),
        agent_live_sleeve_allowlist=["scalp"],
        agent_live_intent_allowlist=["enter", "exit"],
        enable_lifecycle_actions=True,
        enable_adjust_actions=True,
    )
    model_sets = {
        symbol: SimpleNamespace(
            model_set_id="scalp-generation-1",
            rollout_policy={
                "configured": True,
                "active": True,
                "mode": "live",
                "pair_allowlisted": True,
                "budget_scale": 1.0,
                "source": "production_operator_scope",
            },
        )
        for symbol in IG_MT4_SCALP_SYMBOLS
    }

    admission = runtime_runner._live_command_admission_diagnostics(
        settings=settings,
        model_sets=model_sets,
    )

    assert admission["allowed"] is True
    assert admission["required_intents"] == ["enter", "exit"]


def test_zero_over_zero_entry_ratio_is_explicitly_insufficient_evidence() -> None:
    diag = runtime_runner._build_orchestration_live_runtime_diag(
        state={},
        settings=_live_settings(),
        orchestration_diag={},
        entry_execution_diag={
            "approved_entry_count": 0,
            "submitted_entry_count": 0,
        },
        risk_cycle_diag={},
    )

    assert diag["entry_ratio_vs_baseline"] == 0.0
    assert diag["entry_ratio_evaluable"] is False
    assert diag["entry_ratio_status"] == "insufficient_evidence"
    assert diag["entry_ratio_approved_count"] == 0
    assert diag["entry_ratio_submitted_count"] == 0
    assert diag["entry_ratio_accepted_count"] == 0


def test_rejected_entry_submission_does_not_count_as_live_entry_evidence() -> None:
    diag = runtime_runner._build_orchestration_live_runtime_diag(
        state={},
        settings=_live_settings(),
        orchestration_diag={},
        entry_execution_diag={
            "approved_entry_count": 1,
            "submitted_entry_count": 1,
            "accepted_entry_count": 0,
        },
        risk_cycle_diag={},
    )

    assert diag["entry_ratio_evaluable"] is True
    assert diag["entry_ratio_vs_baseline"] == 0.0
    assert diag["entry_ratio_submitted_count"] == 1
    assert diag["entry_ratio_accepted_count"] == 0


def test_entry_evidence_is_pair_scoped_and_release_bound() -> None:
    state = {
        "runtime_diag": {
            "orchestration_live": {
                "bundle_run_id": "bundle-a",
                "current_stage_index": 0,
                "current_stage_pct": 1,
            }
        }
    }
    diag = runtime_runner._build_orchestration_live_runtime_diag(
        state=state,
        settings=_live_settings(),
        orchestration_diag={},
        entry_execution_diag={
            "entry_evidence_events": [
                {
                    "pair": "EURUSD",
                    "action_key": "eur-entry-1",
                    "approved": True,
                    "submitted": True,
                    "accepted": True,
                    "observed_at": 100.0,
                },
                {
                    "pair": "GBPUSD",
                    "action_key": "gbp-entry-1",
                    "approved": True,
                    "submitted": True,
                    "accepted": False,
                    "observed_at": 101.0,
                },
            ]
        },
        risk_cycle_diag={},
    )

    eur = diag["entry_evidence_by_pair"]["EURUSD"]
    gbp = diag["entry_evidence_by_pair"]["GBPUSD"]
    assert eur["bundle_run_id"] == "bundle-a"
    assert eur["stage_index"] == 0
    assert eur["entry_ratio_vs_baseline"] == 1.0
    assert gbp["entry_ratio_vs_baseline"] == 0.0


def test_entry_evidence_survives_quiet_cycles_and_dedupes_action_keys() -> None:
    base_state = {
        "runtime_diag": {
            "orchestration_live": {
                "bundle_run_id": "bundle-a",
                "current_stage_index": 0,
                "current_stage_pct": 1,
            }
        }
    }
    first = runtime_runner._build_orchestration_live_runtime_diag(
        state=base_state,
        settings=_live_settings(),
        orchestration_diag={},
        entry_execution_diag={
            "entry_evidence_events": [
                {
                    "pair": "EURUSD",
                    "action_key": "entry-1",
                    "approved": True,
                    "submitted": True,
                    "accepted": True,
                    "observed_at": 100.0,
                }
            ]
        },
        risk_cycle_diag={},
    )
    carried_state = {"runtime_diag": {"orchestration_live": first}}
    quiet = runtime_runner._build_orchestration_live_runtime_diag(
        state=carried_state,
        settings=_live_settings(),
        orchestration_diag={},
        entry_execution_diag={},
        risk_cycle_diag={},
    )
    duplicate = runtime_runner._build_orchestration_live_runtime_diag(
        state={"runtime_diag": {"orchestration_live": quiet}},
        settings=_live_settings(),
        orchestration_diag={},
        entry_execution_diag={
            "entry_evidence_events": [
                {
                    "pair": "EURUSD",
                    "action_key": "entry-1",
                    "approved": True,
                    "submitted": True,
                    "accepted": False,
                    "observed_at": 102.0,
                }
            ]
        },
        risk_cycle_diag={},
    )

    quiet_evidence = quiet["entry_evidence_by_pair"]["EURUSD"]
    duplicate_evidence = duplicate["entry_evidence_by_pair"]["EURUSD"]
    assert quiet_evidence["approved_count"] == 1
    assert quiet_evidence["accepted_count"] == 1
    assert duplicate_evidence["approved_count"] == 1
    assert duplicate_evidence["accepted_count"] == 1
    assert duplicate_evidence["entry_ratio_vs_baseline"] == 1.0


@pytest.mark.parametrize(
    "enqueue_out",
    [
        {},
        {"status": "forbidden"},
        {"status": "reconciliation_required"},
        {"status": "reconciliation_check_failed"},
        {"status": "draining"},
        {"status": "unknown_future_status"},
        {"status": "duplicate", "state": "acked"},
    ],
)
def test_submission_acceptance_fails_closed_without_an_active_queue_record(
    enqueue_out: dict[str, object],
) -> None:
    assert runtime_runner._submission_is_accepted(enqueue_out) is False


@pytest.mark.parametrize(
    "enqueue_out",
    [
        {"status": "queued"},
        {"status": "duplicate", "state": "queued"},
        {"status": "duplicate", "state": "delivered"},
    ],
)
def test_submission_acceptance_requires_an_active_queue_record(
    enqueue_out: dict[str, object],
) -> None:
    assert runtime_runner._submission_is_accepted(enqueue_out) is True


def test_duplicate_action_is_not_counted_as_new_canary_approval() -> None:
    svc = _RecordingService({"status": "queued"})
    last_action_key: dict[str, str] = {}
    first = runtime_runner._finalize_entry_submissions(
        decisions=[_decision()],
        pending_entries=[
            _pending_entry(
                orchestration=_orchestration(
                    {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}
                )
            )
        ],
        svc=svc,
        last_action_key=last_action_key,
        settings=_live_settings(),
        runtime_state=_runtime_state(),
    )
    duplicate = runtime_runner._finalize_entry_submissions(
        decisions=[_decision()],
        pending_entries=[
            _pending_entry(
                orchestration=_orchestration(
                    {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}
                )
            )
        ],
        svc=svc,
        last_action_key=last_action_key,
        settings=_live_settings(),
        runtime_state=_runtime_state(),
    )

    assert first["approved_entry_count"] == 1
    assert len(first["entry_evidence_events"]) == 1
    assert duplicate["approved_entry_count"] == 0
    assert duplicate["duplicate_entry_count"] == 1
    assert duplicate["entry_evidence_events"] == []


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
def test_finalize_entry_submissions_sleeve_state_is_advisory(
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

    assert len(svc.payloads) == 1
    assert diag["sleeve_governance_enforced"] is True
    assert diag["sleeve_governance_blocked_count"] == 0
    assert diag["sleeve_governance_advisory_count"] == 1
    assert decisions[0]["execution_ready"] is True
    assert decisions[0]["reasons"] == []
    assert decisions[0]["metadata"]["sleeve_governance_entry_block_reason"] == expected_reason
    assert decisions[0]["metadata"]["sleeve_governance_advisory"] is True


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
    "advisory_reason",
    [
        "cross_pair_hard_gate",
        "overlay_low_conviction",
        "overlay_stand_down",
        # NOTE: "adaptive_reentry_cooldown" and "campaign_abandon_cooldown" were
        # removed from this list. They are risk controls about REPEATING a failed
        # bet, not opinions about setup quality, and they are now binding -- see
        # test_finalize_entry_submissions_churn_cooldowns_do_veto below and
        # runner._ADAPTIVE_HARD_ENTRY_BLOCK_REASONS.
    ],
)
def test_finalize_entry_submissions_strategy_advisory_does_not_veto(advisory_reason: str) -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [
        _decision(
            strict_entry_ready=True,
            adaptive_shadow_would_trade=False,
            adaptive_shadow_rejection_reason=advisory_reason,
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

    assert len(svc.payloads) == 1
    assert diag["approved_entry_count"] == 1
    assert decisions[0]["execution_ready"] is True
    assert decisions[0]["reasons"] == []


@pytest.mark.parametrize("hard_reason", ["overlay_low_conviction", "overlay_stand_down"])
def test_observation_only_overlay_cannot_veto_when_adaptive_execution_is_disabled(
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

    assert len(svc.payloads) == 1
    assert diag["approved_entry_count"] == 1
    assert decisions[0]["reasons"] == []


def test_observation_only_sleeve_state_cannot_veto_when_adaptive_execution_is_disabled() -> None:
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

    assert len(svc.payloads) == 1
    assert diag["sleeve_governance_enforced"] is False
    assert diag["sleeve_governance_blocked_count"] == 0
    assert decisions[0]["reasons"] == []


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


def test_live_entry_refuses_when_approved_submission_boundary_is_missing() -> None:
    class _DirectOnlyService:
        direct_calls = 0

        def submit_command(self, payload, proto="v2"):
            self.direct_calls += 1
            return {"status": "queued"}, None

    svc = _DirectOnlyService()
    decisions = [_decision()]

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            _pending_entry(
                orchestration=_orchestration(
                    {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}
                )
            )
        ],
        svc=svc,
        last_action_key={},
        settings=_live_settings(),
        runtime_state=_runtime_state(),
    )

    assert svc.direct_calls == 0
    assert diag["submitted_entry_count"] == 0
    assert decisions[0]["execution_ready"] is False
    assert decisions[0]["metadata"]["enqueue"]["status"] == (
        "approved_entry_submission_unavailable"
    )


@pytest.mark.parametrize("previous_mode", ["shadow", "off", "paper"])
def test_live_restart_requires_persisted_live_rotation_before_using_allowlists(
    previous_mode: str,
) -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [_decision()]
    shadow_state = _runtime_state(
        mode=previous_mode,
        active_pair_scope=[],
        active_sleeve_scope=[],
        active_intent_scope=[],
    )

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            _pending_entry(
                orchestration=_orchestration(
                    {"cmd": "BUY", "side": "BUY", "symbol": "EURUSD", "lots": 0.10}
                )
            )
        ],
        svc=svc,
        last_action_key={},
        settings=_live_settings(),
        runtime_state=shadow_state,
    )

    assert diag["live_governed_submitted_count"] == 0
    assert svc.payloads == []
    assert decisions[0]["metadata"]["enqueue"]["reason"] == "live_mode_disabled"

    live_diag = runtime_runner._build_orchestration_live_runtime_diag(
        state=shadow_state,
        settings=_live_settings(),
        orchestration_diag={},
        entry_execution_diag={},
        risk_cycle_diag={},
    )
    assert live_diag["active_pair_scope"] == ["EURUSD"]
    assert live_diag["active_sleeve_scope"] == ["trend"]
    assert live_diag["active_intent_scope"] == ["enter"]
    assert live_diag["enabled"] is True
    assert live_diag["mode"] == "live"


def test_already_live_empty_scope_remains_an_authoritative_kill_scope() -> None:
    svc = _RecordingService({"status": "queued"})
    decisions = [_decision()]

    diag = runtime_runner._finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            _pending_entry(
                orchestration=_orchestration(
                    {"cmd": "BUY", "side": "BUY", "symbol": "EURUSD", "lots": 0.10}
                )
            )
        ],
        svc=svc,
        last_action_key={},
        settings=_live_settings(),
        runtime_state=_runtime_state(
            mode="live",
            active_pair_scope=[],
            active_sleeve_scope=[],
            active_intent_scope=[],
        ),
    )

    assert svc.payloads == []
    assert diag["live_governed_submitted_count"] == 0
    assert decisions[0]["metadata"]["enqueue"]["reason"] == "live_pair_not_allowlisted"


def test_finalize_entry_submissions_duplicate_queue_response_does_not_mutate_live_submission_state() -> None:
    svc = _RecordingService({"status": "duplicate", "state": "acked"})
    decisions = [_decision()]
    last_action_key: dict[str, str] = {}
    live_entry_registry: dict[str, dict[str, object]] = {}

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
        current_equity=25_000.0,
    )

    assert decisions[0]["metadata"]["enqueue"]["status"] == "duplicate"
    assert last_action_key == {}
    assert live_entry_registry == {}
    assert diag["submitted_entry_count"] == 1
    assert diag["accepted_entry_count"] == 0
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
        adaptive_execution_enabled=True,
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
    assert pending_actions[0]["hard_lifecycle_action"] == "exit"
    assert pending_actions[0]["hard_lifecycle_reason"] == "hard_time_stop"
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
                    "authority_revision": 1,
                    "enabled": True,
                    "mode": "live",
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


def test_legacy_exit_model_still_runs_when_adaptive_execution_is_off(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_runner,
        "_score_exit_policy_model",
        lambda *args, **kwargs: {"selected": "exit", "score": 0.91, "probs": {"exit": 0.91}},
    )
    action = runtime_runner._legacy_lifecycle_failure_action(
        positions=[{"symbol": "EURUSD", "side": "long", "lots": 0.1, "open_time": 1_799_999_900.0}],
        loop_ts=1_800_000_000.0,
        settings=SimpleNamespace(
            enable_lifecycle_actions=True,
            lifecycle_model_action_min_prob=0.5,
        ),
        loaded=SimpleNamespace(exit_model=object(), exit_action_labels={0: "hold", 1: "partial_tp", 2: "exit"}),
        intraday_row=pd.DataFrame([{"ts": "2027-01-15T08:00:00Z", "mid_close": 1.1011, "atr_14": 0.0008}]),
        intraday_timeframe="M5",
        total_position_count=1,
    )

    assert action["lifecycle_action"] == "exit"
    assert action["lifecycle_reason"] == "exit_model_exit_after_entry_inference_error"
    assert action["lifecycle_action_score"] == 0.91


def test_adaptive_pipeline_failure_does_not_run_legacy_exit_model(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_runner,
        "_score_exit_policy_model",
        lambda *args, **kwargs: pytest.fail("legacy exit model must not run in adaptive mode"),
    )
    decisions: list[dict] = []
    pending_actions: list[dict] = []

    runtime_runner._append_failed_pair_decision_with_fail_safe(
        decisions=decisions,
        pending_position_actions=pending_actions,
        pair="EURUSD",
        failure_reason="model_inference_error:RuntimeError",
        state={
            "positions": [
                {
                    "symbol": "EURUSD",
                    "side": "long",
                    "lots": 0.1,
                    "open_time": 1_799_999_900.0,
                }
            ]
        },
        tick={"bid": 1.1010, "ask": 1.1012, "digits": 5},
        loop_ts=1_800_000_000.0,
        settings=SimpleNamespace(
            adaptive_execution_enabled=True,
            hard_time_stop_secs=0.0,
            enable_adjust_actions=False,
            adjust_stop_buffer_pips=0.0,
        ),
        loaded=SimpleNamespace(exit_model=object(), exit_action_labels={0: "hold", 1: "exit"}),
        intraday_row=pd.DataFrame([{"ts": "2027-01-15T08:00:00Z", "mid_close": 1.1011}]),
    )

    assert pending_actions == []
    assert decisions[0]["metadata"]["lifecycle_action"] == "hold"
    assert decisions[0]["metadata"]["lifecycle_reason"] == "adaptive_lifecycle_unavailable"
    assert decisions[0]["metadata"]["hard_lifecycle_action"] == "hold"


@pytest.mark.parametrize("side", ["long", "short"])
def test_hard_lifecycle_fail_safe_adjust_stop_is_strictly_monotonic(side: str) -> None:
    settings = SimpleNamespace(
        hard_time_stop_secs=0.0,
        enable_lifecycle_actions=False,
        enable_adjust_actions=True,
        adjust_stop_buffer_pips=5.0,
    )
    tick = {"bid": 1.1010, "ask": 1.1012, "digits": 5}
    position = {"symbol": "EURUSD", "side": side, "lots": 0.1, "open_time": 1_799_999_900.0}

    without_current_stop = runtime_runner._hard_lifecycle_fail_safe_action(
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
        action = runtime_runner._hard_lifecycle_fail_safe_action(
            positions=[{**position, "sl": current_sl}],
            loop_ts=1_800_000_000.0,
            tick=tick,
            settings=settings,
        )
        assert action["lifecycle_action"] == expected_action


@pytest.mark.parametrize("adaptive_action", ["hold", "partial_tp", "tighten_stop"])
def test_hard_time_stop_monotonically_upgrades_adaptive_lifecycle(adaptive_action: str) -> None:
    resolved = runtime_runner._resolve_hard_lifecycle_floor(
        lifecycle_action=adaptive_action,
        lifecycle_reason=f"adaptive_{adaptive_action}",
        lifecycle_action_score=0.7,
        close_lots=0.03,
        sl_price=1.1000,
        hard_lifecycle_action="exit",
        hard_lifecycle_reason="hard_time_stop",
        hard_lifecycle_action_score=1.0,
    )

    assert resolved["lifecycle_action"] == "exit"
    assert resolved["lifecycle_reason"] == "hard_time_stop"
    assert resolved["hard_lifecycle_applied"] is True


@pytest.mark.parametrize("adaptive_action", ["partial_tp", "exit"])
def test_hard_tighten_stop_never_downgrades_adaptive_reduce_or_exit(adaptive_action: str) -> None:
    resolved = runtime_runner._resolve_hard_lifecycle_floor(
        lifecycle_action=adaptive_action,
        lifecycle_reason=f"adaptive_{adaptive_action}",
        lifecycle_action_score=0.8,
        close_lots=0.03,
        sl_price=0.0,
        hard_lifecycle_action="tighten_stop",
        hard_lifecycle_reason="adjust_stop_after_entry_inference_error",
        hard_lifecycle_action_score=1.0,
        hard_lifecycle_sl_price=1.1000,
    )

    assert resolved["lifecycle_action"] == adaptive_action
    assert resolved["lifecycle_reason"] == f"adaptive_{adaptive_action}"
    assert resolved["hard_lifecycle_applied"] is False


@pytest.mark.parametrize(
    "cooldown_reason",
    ["adaptive_reentry_cooldown", "campaign_abandon_cooldown"],
)
def test_finalize_entry_submissions_churn_cooldowns_do_veto(cooldown_reason: str) -> None:
    """Churn cooldowns are binding risk controls, not advisory opinions.

    Previously both were appended to ``adaptive_advisories`` -- a bucket with no
    non-telemetry consumers -- so the runtime could re-enter the same pair in the
    same direction on the bar after a stop-out. With transaction cost the
    dominant term in this system's P&L, that was the most expensive available
    behaviour.
    """

    svc = _RecordingService({"status": "queued"})
    decisions = [
        _decision(
            strict_entry_ready=True,
            adaptive_shadow_would_trade=False,
            adaptive_shadow_rejection_reason=cooldown_reason,
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

    assert svc.payloads == [], "a churn cooldown must not reach the broker"
    assert diag["approved_entry_count"] == 0
    assert decisions[0]["execution_ready"] is False
    assert cooldown_reason in decisions[0]["reasons"]


def _redundant_complementarity(*, demoted: str, winner: str):
    """A snapshot in which ``demoted`` was measured redundant against ``winner``."""
    from fxstack.strategy.complementarity import (
        VERDICT_ADMITTED,
        VERDICT_DEMOTED,
        ComplementaritySnapshot,
        SleeveVerdict,
    )

    return ComplementaritySnapshot(
        verdicts={
            winner: SleeveVerdict(sleeve=winner, verdict=VERDICT_ADMITTED),
            demoted: SleeveVerdict(
                sleeve=demoted,
                verdict=VERDICT_DEMOTED,
                reason="redundant_positive_correlation",
                redundant_with=winner,
                correlation=0.93,
            ),
        },
        evaluated_sleeves=[demoted, winner],
    )


def test_redundant_sleeve_entry_is_blocked_before_the_risk_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sleeve measured redundant against a stronger one must not open a position.

    Two sleeves that win and lose in the same conditions are one bet taken twice.
    Admitting both doubles position risk for no extra edge, so the redundant side
    is refused -- and refused early, before the risk kernel spends work on it.
    """

    def _unexpected_risk(**_kwargs):
        raise AssertionError("a redundant sleeve must not reach the risk kernel")

    monkeypatch.setattr(runtime_runner, "_evaluate_runtime_risk_kernel", _unexpected_risk)

    decisions = [_decision()]
    pending = {
        "index": 0,
        "pair": "EURUSD",
        "ts_value": "2026-04-09T10:00:00Z",
        "action_key": "entry:2026-04-09T10:00:00Z",
        "sl_price": 1.09,
        "tp_price": 1.12,
        "risk_reapproval_context": {"pair": "EURUSD"},
    }

    diag = runtime_runner._reapprove_final_entry_intents(
        decisions=decisions,
        pending_entries=[pending],
        settings=_live_settings(),
        complementarity=_redundant_complementarity(demoted="trend", winner="range_mean_reversion"),
    )

    assert diag["approved_count"] == 0
    assert decisions[0]["execution_ready"] is False
    assert "sleeve_redundant_with:range_mean_reversion" in decisions[0]["reasons"]
    assert (
        decisions[0]["metadata"]["sleeve_redundancy_block_reason"]
        == "sleeve_redundant_with:range_mean_reversion"
    )


def test_surviving_sleeve_still_trades_when_its_pair_was_demoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate withholds the redundant side only -- it never empties the book."""
    captured: dict[str, object] = {}

    def _risk(**kwargs):
        captured.update(kwargs)
        return _final_entry_risk_result()

    monkeypatch.setattr(runtime_runner, "_evaluate_runtime_risk_kernel", _risk)

    decisions = [_decision()]
    pending = {
        "index": 0,
        "pair": "EURUSD",
        "ts_value": "2026-04-09T10:00:00Z",
        "action_key": "entry:2026-04-09T10:00:00Z",
        "sl_price": 1.09,
        "tp_price": 1.12,
        "risk_reapproval_context": {"pair": "EURUSD"},
    }

    diag = runtime_runner._reapprove_final_entry_intents(
        decisions=decisions,
        pending_entries=[pending],
        settings=_live_settings(),
        # "trend" is the SURVIVOR here; the demoted sleeve is a different one.
        complementarity=_redundant_complementarity(demoted="range_mean_reversion", winner="trend"),
    )

    assert diag["approved_count"] == 1
    assert decisions[0]["execution_ready"] is True
    assert decisions[0]["metadata"]["sleeve_redundancy_block_reason"] == ""
    assert captured  # the risk kernel was reached


def test_absent_complementarity_snapshot_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No measurement -> no opinion. The gate is inert without history."""
    monkeypatch.setattr(
        runtime_runner,
        "_evaluate_runtime_risk_kernel",
        lambda **_kwargs: _final_entry_risk_result(),
    )

    decisions = [_decision()]
    pending = {
        "index": 0,
        "pair": "EURUSD",
        "ts_value": "2026-04-09T10:00:00Z",
        "action_key": "entry:2026-04-09T10:00:00Z",
        "sl_price": 1.09,
        "tp_price": 1.12,
        "risk_reapproval_context": {"pair": "EURUSD"},
    }

    diag = runtime_runner._reapprove_final_entry_intents(
        decisions=decisions,
        pending_entries=[pending],
        settings=_live_settings(),
        complementarity=None,
    )

    assert diag["approved_count"] == 1
    assert decisions[0]["execution_ready"] is True


def test_losing_sleeve_gets_a_smaller_position_through_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Realized expectancy must reach the lots the risk kernel is asked to approve.

    A sleeve that has been paying keeps shrinking; one that has been paid keeps
    its size. This pins the wiring, not just the pure function.
    """
    captured: list[dict[str, object]] = []

    def _risk(**kwargs):
        # The reapproval context is spread into kwargs, not passed as one object.
        captured.append(dict(kwargs))
        return _final_entry_risk_result()

    monkeypatch.setattr(runtime_runner, "_evaluate_runtime_risk_kernel", _risk)

    def _run(expectancy: float) -> float:
        captured.clear()
        decisions = [_decision()]
        decisions[0]["metadata"]["adaptive_size_scale"] = 1.0
        pending = {
            "index": 0,
            "pair": "EURUSD",
            "ts_value": "2026-04-09T10:00:00Z",
            "action_key": "entry:2026-04-09T10:00:00Z",
            "sl_price": 1.09,
            "tp_price": 1.12,
            "risk_reapproval_context": {"pair": "EURUSD", "planned_entry_lots": 1.0},
        }
        runtime_runner._reapprove_final_entry_intents(
            decisions=decisions,
            pending_entries=[pending],
            settings=_live_settings(),
            sleeve_health_snapshots={
                "trend": SleeveHealthSnapshot(
                    sleeve="trend",
                    score=0.6,
                    state="healthy",
                    trades=40,
                    win_rate=0.5,
                    expectancy_usd=float(expectancy),
                    profit_factor=1.0,
                    avg_holding_bars=10.0,
                    partial_frequency=0.0,
                    replacement_exit_share=0.0,
                    drawdown_contribution_usd=0.0,
                    session_pnl_mix={},
                    pair_contribution={},
                )
            },
        )
        assert captured, "the risk kernel should have been reached"
        return float(captured[0].get("planned_entry_lots", 0.0))

    paying = _run(25.0)
    losing = _run(-25.0)

    assert paying > losing, "a losing sleeve must be funded less than a paying one"
    assert losing == pytest.approx(paying * 0.25), "starved to the floor, not switched off"
    assert losing > 0.0
