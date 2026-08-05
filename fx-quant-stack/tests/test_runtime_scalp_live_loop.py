from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.risk.sizing import BrokerContractSpec, account_value_per_price_unit
from fxstack.runtime.broker_contract_state import BrokerContractUniverse
from fxstack.runtime.scalp_cycle_capacity import ScalpCycleSymbolDiagnostic
from fxstack.runtime.scalp_execution_authority import (
    ScalpAuthorityExpectation,
    build_active_authority,
    command_binding_fields,
)
from fxstack.runtime.scalp_live_loop import (
    _production_scalp_cash_risk_error,
    _account_conversion_tick_symbols,
    _cycle_account_conversion_projection,
    _exact_tick_scope,
    _exit_payload,
    _owner_token,
    _positions_with_current_contract_value,
    _submit_time_stop_exits,
    _symbol_decision_context,
)
from fxstack.runtime.scalp_position_lifecycle import (
    TICKET_OWNER_CONTRACT,
    ScalpTimeStopCloseDecision,
    evaluate_scalp_position_lifecycle,
)
from fxstack.runtime.mtvclc_proposal_batch import MTVCLCSymbolProposalDiagnostic
from fxstack.runtime.scalp_rollover_guard import (
    evaluate_production_scalp_rollover_guard,
)
from fxstack.runtime.scalp_proposal_batch import ScalpSymbolProposalDiagnostic
from fxstack.runtime.scalp_restart_reconciliation import (
    MT4_POSITIONS_SNAPSHOT_SCHEMA,
    reconcile_scalp_restart,
)
from fxstack.strategy.mtvclc import (
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
)


NOW = 1_800_001_200.0
MAGIC = 246_810
ACCOUNT_SCOPE = "ig-demo-account-scope"
OWNER_TOKEN = "fxs-owned-ticket-101"


@pytest.mark.parametrize(
    ("approved", "expected"),
    [
        ({}, "production_scalp_cash_risk_proof_missing"),
        (
            {"broker_contract_sizing": {"money_at_risk": 0.75}},
            "",
        ),
        (
            {"broker_contract_sizing": {"money_at_risk": 50.00000001}},
            "production_scalp_cash_risk_cap_invalid",
        ),
    ],
)
def test_production_scalp_cash_risk_seam_is_fail_closed(
    approved: dict[str, Any],
    expected: str,
) -> None:
    assert _production_scalp_cash_risk_error(approved, equity=10_000.0) == expected


def test_open_position_stop_risk_uses_each_current_broker_lot_size() -> None:
    universe = BrokerContractUniverse(
        contracts={
            "EURUSD": BrokerContractSpec(
                symbol="EURUSD",
                broker_symbol="EURUSD.IG",
                lot_size=100_000.0,
                min_lot=0.01,
                lot_step=0.01,
                max_lot=100.0,
                point=0.00001,
                stop_level_points=10.0,
                margin_required=1_000.0,
            ),
            "BTCUSD": BrokerContractSpec(
                symbol="BTCUSD",
                broker_symbol="BTCUSD.IG",
                lot_size=1.0,
                min_lot=0.01,
                lot_step=0.01,
                max_lot=100.0,
                point=0.01,
                stop_level_points=10.0,
                margin_required=50.0,
            ),
        },
        account_currency="USD",
        available_margin=9_000.0,
        observed_at=NOW,
        age_secs=0.0,
    )

    positions = _positions_with_current_contract_value(
        positions=[
            {"symbol": "EURUSD", "value_per_price_unit": 7.0},
            {"symbol": "BTCUSD", "value_per_price_unit": 100_000.0},
            {"symbol": "UNKNOWN", "value_per_price_unit": 42.0},
        ],
        contract_universe=universe,
        quote_rates={"EURUSD": 1.1, "BTCUSD": 50_000.0},
    )

    assert positions[0]["value_per_price_unit"] == pytest.approx(100_000.0)
    assert positions[1]["value_per_price_unit"] == pytest.approx(1.0)
    assert "value_per_price_unit" not in positions[2]


def _authority(
    *,
    revoked: bool = False,
    generation_id: str = "scalp-generation-1",
    runtime_boot_id: str = "scalp-boot-1",
    authority_revision: int = 7,
    engine_sha256: str = "1" * 64,
    now_epoch: float = NOW,
) -> dict[str, Any]:
    authority = build_active_authority(
        ScalpAuthorityExpectation(
            generation_id=generation_id,
            strategy_id=MTVCLC_STRATEGY_ID,
            strategy_version=MTVCLC_STRATEGY_VERSION,
            engine_sha256=engine_sha256,
            config_id=MTVCLC_CONFIG_ID,
            config_sha256=MTVCLC_CONFIG_SHA256,
            runtime_release_certificate_sha256="2" * 64,
            runtime_release_signing_key_id="3" * 64,
            research_evidence_sha256="4" * 64,
            research_evidence_signing_key_id="5" * 64,
            registry_generation_id=generation_id,
            registry_revision=11,
            registry_sha256="6" * 64,
            qualification_surface_sha256="7" * 64,
            cost_mapping_sha256="8" * 64,
            execution_contract_sha256="9" * 64,
            validation_expires_at_epoch=now_epoch + 3_600.0,
            runtime_boot_id=runtime_boot_id,
            authority_revision=authority_revision,
        ),
        activated_at=now_epoch - 120.0,
    )
    if revoked:
        authority["status"] = "revoked"
        authority["reason"] = "validation_revoked"
    return authority


def _position(*, now_epoch: float = NOW, open_age_secs: float = 1_200.0) -> dict[str, Any]:
    return {
        "symbol": "EURUSD",
        "broker_symbol": "EURUSD.IG",
        "side": "BUY",
        "ticket": 101,
        "lots": 0.12,
        "magic": MAGIC,
        "order_comment": OWNER_TOKEN,
        "open_price": 1.101,
        "open_time": now_epoch - open_age_secs,
        "sl": 1.099,
        "tp": 1.105,
        "profit": 2.0,
    }


def _state(*, now_epoch: float = NOW, open_age_secs: float = 1_200.0) -> dict[str, Any]:
    return {
        "positions_snapshot_authoritative": True,
        "positions_snapshot_source": "positions_snapshot",
        "positions_snapshot_schema": MT4_POSITIONS_SNAPSHOT_SCHEMA,
        "positions_snapshot_contract_current": True,
        "positions_snapshot_account_scope": ACCOUNT_SCOPE,
        "positions_snapshot_token": "snapshot-token-1",
        "positions_snapshot_received_at": now_epoch - 1.0,
        "positions": [
            _position(now_epoch=now_epoch, open_age_secs=open_age_secs)
        ],
    }


def _entry_command(authority: dict[str, Any]) -> dict[str, Any]:
    command_id = "entry-eurusd-101"
    payload = {
        **command_binding_fields(authority),
        "command_id": command_id,
        "cmd": "BUY",
        "symbol": "EURUSD",
        "magic": MAGIC,
        "owner_token": OWNER_TOKEN,
        "ownership_contract": TICKET_OWNER_CONTRACT,
        "lots": 0.12,
    }
    return {
        "command_id": command_id,
        "cmd": "BUY",
        "symbol": "EURUSD",
        "magic": MAGIC,
        "intent": payload["intent"],
        "status": "acked",
        "payload_json": payload,
        "ack_json": {
            "command_id": command_id,
            "status": "acked",
            "symbol": "EURUSD",
            "ticket": 101,
            "magic": MAGIC,
            "owner_token": OWNER_TOKEN,
        },
    }


def _confirmed_close(authority: dict[str, Any]) -> dict[str, Any]:
    expected_bindings = {
        key: value
        for key, value in command_binding_fields(authority).items()
        if key.startswith("expected_strategy_")
    }
    command_id = "historical-confirmed-close"
    payload = {
        **expected_bindings,
        "command_id": command_id,
        "cmd": "CLOSE",
        "symbol": "EURUSD",
        "magic": MAGIC,
        "target_ticket": 101,
        "owner_token": OWNER_TOKEN,
        "ownership_contract": TICKET_OWNER_CONTRACT,
        "intent": "EXIT",
        "management_strategy": authority["strategy_id"],
        "managed_entry_command_id": "entry-eurusd-101",
    }
    return {
        "command_id": command_id,
        "cmd": "CLOSE",
        "symbol": "EURUSD",
        "magic": MAGIC,
        "intent": "EXIT",
        "status": "acked",
        "payload_json": payload,
        "ack_json": {
            "command_id": command_id,
            "status": "acked",
            "symbol": "EURUSD",
            "ticket": 101,
            "magic": MAGIC,
            "owner_token": OWNER_TOKEN,
        },
    }


def _restart_and_lifecycle(
    authority: dict[str, Any],
    *,
    confirmed_close: bool = False,
):
    commands = [_entry_command(authority)]
    if confirmed_close:
        commands.append(_confirmed_close(authority))
    reconciliation = reconcile_scalp_restart(
        state_snapshot=_state(),
        durable_command_rows=commands,
        production_scalp_authority=authority,
        now_epoch=NOW,
        max_snapshot_age_secs=30.0,
        expected_magic=MAGIC,
        expected_account_scope=ACCOUNT_SCOPE,
    )
    lifecycle = evaluate_scalp_position_lifecycle(
        authoritative_owned_positions=reconciliation.owned_positions,
        finalized_common_minute_epoch=int(NOW - 60.0),
        as_of_epoch=NOW,
        expected_magic=MAGIC,
        expected_ownership_contract=TICKET_OWNER_CONTRACT,
        time_stop_bars=20,
    )
    assert lifecycle.diagnostics.accepted is True
    assert tuple(item.target_ticket for item in lifecycle.close_decisions) == (101,)
    return reconciliation, lifecycle


class _RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], str]] = []

    def submit_command(
        self,
        payload: dict[str, Any],
        *,
        proto: str,
    ) -> tuple[dict[str, Any], int]:
        self.calls.append((dict(payload), proto))
        return {"status": "queued", "command_id": payload["command_id"]}, 200


def _ticks() -> dict[str, dict[str, Any]]:
    return {
        symbol: {
            "symbol": symbol,
            "bid": 1.0,
            "ask": 1.0001,
            "market_event_fresh": True,
            "market_event_reason": "ok",
        }
        for symbol in IG_MT4_SCALP_SYMBOLS
    }


def _proposal_diagnostic(
    *,
    structural_ready: bool,
    structural_reasons: tuple[str, ...] = (),
    evaluation_allowed: bool | None = None,
    evaluation_reasons: tuple[str, ...] = (),
) -> ScalpSymbolProposalDiagnostic:
    return ScalpSymbolProposalDiagnostic(
        symbol="EURUSD",
        structural_ready=structural_ready,
        structural_reasons=structural_reasons,
        raw_row_count=21 if structural_ready else 0,
        filtered_current_bar_count=1 if structural_ready else 0,
        finalized_row_count=20 if structural_ready else 0,
        selected_history_count=20 if structural_ready else 0,
        latest_finalized_minute_epoch=(
            int(NOW - 60.0) if structural_ready else None
        ),
        evaluation_allowed=evaluation_allowed,
        evaluation_reasons=evaluation_reasons,
    )


def _capacity_diagnostic(
    *reasons: str,
) -> ScalpCycleSymbolDiagnostic:
    return ScalpCycleSymbolDiagnostic(
        symbol="EURUSD",
        proposal_rank=None,
        had_allowed_proposal=False,
        selected=False,
        refusal_reasons=tuple(reasons),
        open_before_exits=False,
        confirmed_exit_applied=False,
        open_after_exits=False,
        queued_entry_active=False,
        occupies_projected_capacity=False,
    )


def test_symbol_decision_context_surfaces_structural_abstention_metadata() -> None:
    proposal_diagnostic = _proposal_diagnostic(
        structural_ready=False,
        structural_reasons=(
            "common_closed_minute_missing",
            "insufficient_finalized_history",
        ),
    )
    capacity_diagnostic = _capacity_diagnostic(
        "structural_unready:common_closed_minute_missing",
        "structural_unready:insufficient_finalized_history",
    )

    context = _symbol_decision_context(
        entry_global_reasons=[],
        proposal=None,
        proposal_diagnostic=proposal_diagnostic,
        qualification_diagnostic={},
        capacity_diagnostic=capacity_diagnostic,
    )

    assert context["reasons"] == [
        "structural_unready:common_closed_minute_missing",
        "structural_unready:insufficient_finalized_history",
    ]
    assert context["proposal_batch_symbol_diagnostic"]["structural_ready"] is False
    assert context["proposal_batch_symbol_diagnostic"]["evaluation_allowed"] is None
    assert context["capacity_symbol_diagnostic"]["refusal_reasons"] == (
        "structural_unready:common_closed_minute_missing",
        "structural_unready:insufficient_finalized_history",
    )


def test_symbol_decision_context_distinguishes_qualification_and_no_signal() -> None:
    qualified_refusal = _symbol_decision_context(
        entry_global_reasons=[],
        proposal=SimpleNamespace(reasons=()),
        proposal_diagnostic=_proposal_diagnostic(
            structural_ready=True,
            evaluation_allowed=True,
        ),
        qualification_diagnostic={"reasons": ["cell_not_validated"]},
        capacity_diagnostic=_capacity_diagnostic("no_allowed_proposal"),
    )
    no_signal = _symbol_decision_context(
        entry_global_reasons=[],
        proposal=None,
        proposal_diagnostic=_proposal_diagnostic(
            structural_ready=True,
            evaluation_allowed=False,
            evaluation_reasons=("no_dislocation",),
        ),
        qualification_diagnostic={},
        capacity_diagnostic=_capacity_diagnostic("no_allowed_proposal"),
    )

    assert qualified_refusal["reasons"] == ["cell_not_validated"]
    assert no_signal["reasons"] == ["no_dislocation"]


def test_symbol_decision_context_preserves_mtvclc_near_signal_telemetry() -> None:
    proposal_diagnostic = MTVCLCSymbolProposalDiagnostic(
        symbol="EURUSD",
        structural_ready=True,
        structural_reasons=(),
        raw_bar_count=242,
        filtered_current_bar_count=1,
        finalized_bar_count=241,
        selected_history_count=241,
        latest_finalized_minute_epoch=int(NOW - 60.0),
        raw_quote_count=1,
        selected_quote_count=1,
        quote_transport_received_at_epochs=(NOW,),
        cost_calibration_id="cost-v1",
        cost_calibration_source_sha256="a" * 64,
        cost_calibration_row_sha256="b" * 64,
        evaluation_allowed=False,
        evaluation_reasons=("tick_volume_not_strictly_above_v90",),
        evaluation_volume_v90=100.0,
        evaluation_signal_tick_volume=95,
        evaluation_activity_ratio=0.95,
    )

    context = _symbol_decision_context(
        entry_global_reasons=[],
        proposal=None,
        proposal_diagnostic=proposal_diagnostic,
        qualification_diagnostic={},
        capacity_diagnostic=_capacity_diagnostic("no_allowed_proposal"),
    )

    projected = context["proposal_batch_symbol_diagnostic"]
    assert context["reasons"] == ["tick_volume_not_strictly_above_v90"]
    assert projected["evaluation_volume_v90"] == pytest.approx(100.0)
    assert projected["evaluation_signal_tick_volume"] == 95
    assert projected["evaluation_activity_ratio"] == pytest.approx(0.95)


def test_symbol_decision_context_allows_missing_diagnostic_on_global_refusal() -> None:
    context = _symbol_decision_context(
        entry_global_reasons=["missing_universe_symbol:NZDJPY"],
        proposal=None,
        proposal_diagnostic=None,
        qualification_diagnostic={},
        capacity_diagnostic=_capacity_diagnostic("planner_input_invalid"),
    )

    assert context["reasons"] == ["missing_universe_symbol:NZDJPY"]
    assert context["proposal_batch_symbol_diagnostic"] == {}
    assert context["capacity_symbol_diagnostic"]["refusal_reasons"] == (
        "planner_input_invalid",
    )


def test_owner_token_is_deterministic_bounded_and_identity_sensitive() -> None:
    first = _owner_token(
        generation_id="generation-a",
        command_id="command-a",
        magic=MAGIC,
    )
    second = _owner_token(
        generation_id="generation-a",
        command_id="command-a",
        magic=MAGIC,
    )

    assert first == second
    assert first.startswith("fxs-s-")
    assert len(first) == 22
    assert len(first) < 31
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,31}", first)
    assert len(
        {
            first,
            _owner_token(
                generation_id="generation-b",
                command_id="command-a",
                magic=MAGIC,
            ),
            _owner_token(
                generation_id="generation-a",
                command_id="command-b",
                magic=MAGIC,
            ),
            _owner_token(
                generation_id="generation-a",
                command_id="command-a",
                magic=MAGIC + 1,
            ),
        }
    ) == 4


@pytest.mark.parametrize("revoked", (False, True))
def test_time_stop_close_payload_is_strict_management_not_entry(
    revoked: bool,
) -> None:
    authority = _authority(revoked=revoked)
    decision = ScalpTimeStopCloseDecision(
        symbol="EURUSD",
        target_ticket=101,
        lots=0.12,
        magic=MAGIC,
        owner_token=OWNER_TOKEN,
        ownership_contract=TICKET_OWNER_CONTRACT,
        reason="time_stop",
        bars_held=20,
    )
    reconciliation, _ = _restart_and_lifecycle(authority)
    owned_position = reconciliation.owned_positions[0]

    payload = _exit_payload(
        decision=decision,
        owned_position=owned_position,
    )
    expected = {
        key: value
        for key, value in command_binding_fields(authority).items()
        if key.startswith("expected_strategy_")
    }

    assert payload["cmd"] == "CLOSE"
    assert payload["symbol"] == "EURUSD"
    assert payload["target_ticket"] == 101
    assert payload["expected_target_lots"] == pytest.approx(0.12)
    assert payload["expected_broker_contract_broker_symbol"] == "EURUSD.IG"
    assert payload["expected_open_price"] == pytest.approx(1.101)
    assert payload["expected_tp_price"] == pytest.approx(1.105)
    assert payload["magic"] == MAGIC
    assert payload["owner_token"] == OWNER_TOKEN
    assert payload["ownership_contract"] == TICKET_OWNER_CONTRACT
    assert payload["intent"] == "EXIT"
    assert payload["management_strategy"] == authority["strategy_id"]
    assert payload["managed_entry_command_id"] == "entry-eurusd-101"
    assert "strategy_lane" not in payload
    assert all(payload[key] == value for key, value in expected.items())


def test_restart_lifecycle_shadow_exit_retains_exact_owner_binding() -> None:
    authority = _authority(revoked=True)
    reconciliation, lifecycle = _restart_and_lifecycle(authority)
    service = _RecordingService()

    outcomes = _submit_time_stop_exits(
        service=service,
        live=False,
        reconciliation=reconciliation,
        lifecycle=lifecycle,
    )

    assert reconciliation.entry_admission_ready is False
    assert tuple(item.ticket for item in reconciliation.owned_positions) == (101,)
    assert service.calls == []
    assert outcomes[0]["status"] == "shadow_preview"
    payload = outcomes[0]["payload"]
    assert payload["target_ticket"] == 101
    assert payload["magic"] == MAGIC
    assert payload["owner_token"] == OWNER_TOKEN
    assert payload["intent"] == "EXIT"
    assert "strategy_lane" not in payload


def test_restart_inside_funding_window_immediately_projects_exact_owner_close() -> None:
    guard_now = datetime(2026, 8, 3, 20, 50, tzinfo=UTC).timestamp()
    authority = _authority(now_epoch=guard_now)
    reconciliation = reconcile_scalp_restart(
        state_snapshot=_state(now_epoch=guard_now, open_age_secs=30.0),
        durable_command_rows=[_entry_command(authority)],
        production_scalp_authority=authority,
        now_epoch=guard_now,
        max_snapshot_age_secs=30.0,
        expected_magic=MAGIC,
        expected_account_scope=ACCOUNT_SCOPE,
    )
    lifecycle = evaluate_scalp_position_lifecycle(
        authoritative_owned_positions=reconciliation.owned_positions,
        finalized_common_minute_epoch=int(guard_now - 60.0),
        as_of_epoch=guard_now,
        expected_magic=MAGIC,
        expected_ownership_contract=TICKET_OWNER_CONTRACT,
        time_stop_bars=20,
        rollover_guard_decision=(
            evaluate_production_scalp_rollover_guard(guard_now)
        ),
    )

    outcomes = _submit_time_stop_exits(
        service=_RecordingService(),
        live=False,
        reconciliation=reconciliation,
        lifecycle=lifecycle,
    )

    assert reconciliation.owned_positions[0].entry_command_id == (
        "entry-eurusd-101"
    )
    assert lifecycle.close_decisions[0].reason == "rollover_funding_guard"
    payload = outcomes[0]["payload"]
    assert payload["command_id"] == "fxs-exit-101-rollover-funding-guard"
    assert payload["cmd"] == "CLOSE"
    assert payload["action"] == "rollover_funding_guard"
    assert payload["managed_entry_command_id"] == "entry-eurusd-101"
    assert payload["target_ticket"] == 101
    assert "strategy_lane" not in payload


def test_restart_exit_retains_full_entry_generation_after_authority_rollover() -> None:
    entry_authority = _authority()
    current_authority = _authority(
        generation_id="scalp-generation-2",
        runtime_boot_id="scalp-boot-2",
        authority_revision=8,
        engine_sha256="4" * 64,
    )
    reconciliation = reconcile_scalp_restart(
        state_snapshot=_state(),
        durable_command_rows=[_entry_command(entry_authority)],
        production_scalp_authority=current_authority,
        now_epoch=NOW,
        max_snapshot_age_secs=30.0,
        expected_magic=MAGIC,
        expected_account_scope=ACCOUNT_SCOPE,
    )
    lifecycle = evaluate_scalp_position_lifecycle(
        authoritative_owned_positions=reconciliation.owned_positions,
        finalized_common_minute_epoch=int(NOW - 60.0),
        as_of_epoch=NOW,
        expected_magic=MAGIC,
        expected_ownership_contract=TICKET_OWNER_CONTRACT,
        time_stop_bars=20,
    )

    outcomes = _submit_time_stop_exits(
        service=_RecordingService(),
        live=False,
        reconciliation=reconciliation,
        lifecycle=lifecycle,
    )

    payload = outcomes[0]["payload"]
    historical = {
        key: value
        for key, value in command_binding_fields(entry_authority).items()
        if key.startswith("expected_strategy_")
    }
    current = {
        key: value
        for key, value in command_binding_fields(current_authority).items()
        if key.startswith("expected_strategy_")
    }
    assert all(payload[key] == value for key, value in historical.items())
    assert payload["expected_strategy_binding_sha256"] != current[
        "expected_strategy_binding_sha256"
    ]
    assert payload["managed_entry_command_id"] == "entry-eurusd-101"


def test_broker_confirmed_close_is_not_resubmitted_from_stale_snapshot() -> None:
    authority = _authority()
    reconciliation, lifecycle = _restart_and_lifecycle(
        authority,
        confirmed_close=True,
    )
    service = _RecordingService()

    outcomes = _submit_time_stop_exits(
        service=service,
        live=True,
        reconciliation=reconciliation,
        lifecycle=lifecycle,
    )

    assert reconciliation.broker_confirmed_exit_symbols == ("EURUSD",)
    assert service.calls == []
    assert all(item["submitted"] is False for item in outcomes)


def test_exact_tick_scope_accepts_only_the_complete_canonical_universe() -> None:
    normalized, reasons = _exact_tick_scope(_ticks())

    assert tuple(symbol for symbol in IG_MT4_SCALP_SYMBOLS if symbol in normalized) == (
        IG_MT4_SCALP_SYMBOLS
    )
    assert reasons == ()


def test_account_conversion_ticks_are_market_data_only_not_strategy_scope() -> None:
    conversion_symbols = _account_conversion_tick_symbols("GBP")
    assert "GBPAUD" in conversion_symbols
    assert "AUDGBP" in conversion_symbols
    assert not set(conversion_symbols) & set(IG_MT4_SCALP_SYMBOLS)

    raw_ticks = _ticks()
    raw_ticks["GBPAUD"] = {"bid": 1.91, "ask": 1.9102}
    normalized, reasons = _exact_tick_scope(
        raw_ticks,
        allowed_market_data_symbols=conversion_symbols,
    )

    assert tuple(normalized) == IG_MT4_SCALP_SYMBOLS
    assert reasons == ()


def test_account_conversion_scope_does_not_allow_redundant_inverse_extras() -> None:
    assert _account_conversion_tick_symbols("USD") == ()
    assert _account_conversion_tick_symbols("EUR") == ()
    assert _account_conversion_tick_symbols("JPY") == ()
    assert _account_conversion_tick_symbols("GBP") == ("AUDGBP", "GBPAUD")


def test_cycle_conversion_uses_direct_ask_and_inverse_bid_only() -> None:
    raw_ticks = _ticks()
    raw_ticks["USDJPY"].update(bid=100.0, ask=110.0)
    raw_ticks["GBPUSD"].update(bid=1.20, ask=1.30)
    raw_ticks["XAUUSD"] = {
        "bid": 2_000.0,
        "ask": 2_100.0,
        "market_event_fresh": True,
    }

    projection = _cycle_account_conversion_projection(
        raw_ticks,
        account_currency="USD",
        allowed_market_data_symbols=(),
    )

    assert projection.ok, projection.errors
    assert projection.rates["USDJPY"] == pytest.approx(110.0)
    assert projection.rates["GBPUSD"] == pytest.approx(1.20)
    assert "XAUUSD" not in projection.rates
    assert account_value_per_price_unit(
        pair="EURJPY",
        rates=projection.rates,
        account_currency="USD",
    ) == pytest.approx(100_000.0 / 110.0)
    assert account_value_per_price_unit(
        pair="EURGBP",
        rates=projection.rates,
        account_currency="USD",
    ) == pytest.approx(120_000.0)


def test_cycle_conversion_triangulates_through_fresh_usd_legs() -> None:
    raw_ticks = _ticks()
    raw_ticks["AUDUSD"].update(bid=0.60, ask=0.70)
    raw_ticks["GBPUSD"].update(bid=1.20, ask=1.25)

    projection = _cycle_account_conversion_projection(
        raw_ticks,
        account_currency="GBP",
        allowed_market_data_symbols=_account_conversion_tick_symbols("GBP"),
    )

    assert projection.ok, projection.errors
    assert projection.coverage["AUD"]["method"] == "usd_triangulated"
    assert projection.rates["AUDGBP"] == pytest.approx(0.60 / 1.25)
    assert account_value_per_price_unit(
        pair="EURAUD",
        rates=projection.rates,
        account_currency="GBP",
    ) == pytest.approx(48_000.0)


@pytest.mark.parametrize("account_currency", ["USD", "EUR", "JPY", "GBP", "AUD", "CAD", "CHF"])
def test_exact_ticks_cover_every_quote_for_common_account_currencies(
    account_currency: str,
) -> None:
    projection = _cycle_account_conversion_projection(
        _ticks(),
        account_currency=account_currency,
        allowed_market_data_symbols=_account_conversion_tick_symbols(account_currency),
    )

    assert projection.ok, projection.to_dict()
    assert all(
        item["covered"] is True for item in projection.coverage.values()
    )


def test_cycle_conversion_rejects_stale_broker_event_before_risk() -> None:
    raw_ticks = _ticks()
    raw_ticks["USDJPY"].update(
        market_event_fresh=False,
        market_event_reason="broker_market_event_stale",
    )
    raw_ticks["JPYUSD"] = {
        "bid": 0.0090,
        "ask": 0.0100,
        "market_event_fresh": True,
        "market_event_reason": "ok",
    }

    projection = _cycle_account_conversion_projection(
        raw_ticks,
        account_currency="USD",
        allowed_market_data_symbols=(),
    )

    assert projection.ok is False
    assert "scalp_account_conversion_tick_not_fresh:JPY" in projection.errors
    assert projection.coverage["JPY"]["candidate_event_reasons"]["USDJPY"] == (
        "broker_market_event_stale"
    )
    assert projection.coverage["JPY"]["candidate_status"]["JPYUSD"] == "missing"


def test_tick_scope_refuses_missing_extra_collision_and_invalid_rows() -> None:
    missing = _ticks()
    missing.pop("NZDJPY")
    _, missing_reasons = _exact_tick_scope(missing)
    assert "scalp_tick_symbol_missing:NZDJPY" in missing_reasons

    extra = _ticks()
    extra["XAUUSD"] = {"bid": 2_000.0, "ask": 2_000.1}
    _, extra_reasons = _exact_tick_scope(extra)
    assert "scalp_tick_symbol_extra:XAUUSD" in extra_reasons

    collision = _ticks()
    collision[" eurusd "] = {"bid": 1.0, "ask": 1.0001}
    _, collision_reasons = _exact_tick_scope(collision)
    assert "scalp_tick_symbol_duplicate:EURUSD" in collision_reasons

    invalid = _ticks()
    invalid["EURUSD"] = "not-a-tick"  # type: ignore[assignment]
    _, invalid_reasons = _exact_tick_scope(invalid)
    assert "scalp_tick_row_invalid" in invalid_reasons
    assert "scalp_tick_symbol_missing:EURUSD" in invalid_reasons

    assert _exact_tick_scope([]) == ({}, ("scalp_tick_universe_invalid",))


def test_active_exact_exit_suppresses_duplicate_time_stop_submission() -> None:
    authority = _authority()
    reconciliation, lifecycle = _restart_and_lifecycle(authority)
    active_reconciliation = SimpleNamespace(
        active_exit_tickets=(101,),
        broker_confirmed_exit_symbols=(),
    )
    service = _RecordingService()

    outcomes = _submit_time_stop_exits(
        service=service,
        live=True,
        reconciliation=active_reconciliation,
        lifecycle=lifecycle,
    )

    assert reconciliation.owned_positions[0].ticket == 101
    assert service.calls == []
    assert outcomes == [
        {
            "ticket": 101,
            "symbol": "EURUSD",
            "status": "already_active",
            "submitted": False,
        }
    ]
