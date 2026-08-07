from __future__ import annotations

from datetime import UTC, datetime
import math
from pathlib import Path

import pytest

from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION
from fxstack.providers.ig_mt4_catalog import IG_MT4_CRYPTO_CFD_SYMBOLS
from fxstack.risk.sizing import account_value_per_price_unit
from fxstack.runtime.db_tools import migrate_database
from fxstack.runtime.dto import ExecutionCommand, TICKET_OWNER_CONTRACT
from fxstack.runtime.execution_ack_attestation import MT4_ORDER_ACTUALS_SCHEMA
from fxstack.runtime.broker_contract_state import (
    broker_contract_sizing_metadata,
    project_ig_mt4_contract_universe,
)
from fxstack.runtime.market_source_identity import build_authenticated_market_source
from fxstack.runtime.mtvclc_runtime_release import (
    MTVCLCRuntimeReleaseVerification,
)
from fxstack.runtime.scalp_execution_boundary import (
    ScalpBrokerEntryCostModel,
    build_scalp_broker_entry_plan,
)
from fxstack.runtime.scalp_execution_authority import (
    IG_MT4_SCALP_SYMBOLS,
    SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA,
    ScalpAuthorityExpectation,
    authority_binding_sha256,
    build_active_authority,
    command_binding_fields,
)
from fxstack.runtime.scalp_rollover_guard import (
    DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY,
    PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION,
)
from fxstack.runtime.scalp_validation_evidence import (
    SCALP_ADMISSION_MODE_DIRECT_DEMO,
)
from fxstack.runtime.service import FinalEntryApproval, RuntimeService
from fxstack.runtime import postgres_store as postgres_store_module
from fxstack.settings import get_settings
from fxstack.strategy.mtvclc import (
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
)


BOOT_ID = "production-scalp-boot"
AUTHORITY_REVISION = 1
PRODUCER_IDENTITY = "ig-mt4-production-ea"
PRODUCER_INSTANCE_ID = "mt4-terminal-instance-a"
TERMINAL_LEASE_SCOPE = "ig-mt4-terminal-scope"
CREDENTIAL_GENERATION_ID = "ig-mt4-generation-1"


@pytest.fixture(autouse=True)
def _stable_non_blackout_store_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from fxstack.runtime.scalp_rollover_guard import (
        evaluate_production_scalp_rollover_guard,
    )

    safe = evaluate_production_scalp_rollover_guard(
        datetime(2026, 8, 3, 12, 0, tzinfo=UTC).timestamp()
    )
    monkeypatch.setattr(
        postgres_store_module,
        "evaluate_production_scalp_rollover_guard",
        lambda _now: safe,
    )


def _validation_verification(
    authority: dict,
) -> MTVCLCRuntimeReleaseVerification:
    return MTVCLCRuntimeReleaseVerification(
        valid=True,
        reason="",
        errors=(),
        authenticated=True,
        revocation_verified=True,
        certificate_sha256=str(
            authority["runtime_release_certificate_sha256"]
        ),
        runtime_release_certificate_sha256=str(
            authority["runtime_release_certificate_sha256"]
        ),
        evidence_sha256=str(authority["research_evidence_sha256"]),
        signing_key_id=str(authority["runtime_release_signing_key_id"]),
        runtime_release_signing_key_id=str(
            authority["runtime_release_signing_key_id"]
        ),
        evidence_signing_key_id=str(
            authority["research_evidence_signing_key_id"]
        ),
        registry_generation_id=str(authority["registry_generation_id"]),
        generation_id=str(authority["generation_id"]),
        strategy_id=str(authority["strategy_id"]),
        strategy_version=str(authority["strategy_version"]),
        engine_sha256=str(authority["engine_sha256"]),
        config_id=str(authority["config_id"]),
        config_sha256=str(authority["config_sha256"]),
        venue_id=str(authority["venue_id"]),
        account_mode=str(authority["account_mode"]),
        scope_version=str(authority["scope_version"]),
        symbol_scope=IG_MT4_SCALP_SYMBOLS,
        max_entries_per_symbol_utc_day=1,
        issued_at_epoch=datetime.now(UTC).timestamp() - 60.0,
        expires_at_epoch=float(authority["validation_expires_at_epoch"]),
        release_expires_at_epoch=float(
            authority["validation_expires_at_epoch"]
        ),
        evidence_expires_at_epoch=float(
            authority["validation_expires_at_epoch"]
        ),
        registry_expires_at_epoch=float(
            authority["validation_expires_at_epoch"]
        ),
        registry_revision=int(authority["registry_revision"]),
        registry_sha256=str(authority["registry_sha256"]),
        execution_contract_sha256=str(
            authority["execution_contract_sha256"]
        ),
        qualification_surface_sha256=str(
            authority["qualification_surface_sha256"]
        ),
        win_probability_lower_bounds={
            symbol: {"BUY": 0.60, "SELL": 0.60}
            for symbol in IG_MT4_SCALP_SYMBOLS
        },
        cost_mapping_sha256=str(authority["cost_mapping_sha256"]),
        admission_mode=str(authority.get("admission_mode") or ""),
    )


def _authority_expectation(
    *,
    generation_id: str,
    engine_sha256: str,
    authority_revision: int,
    account_mode: str = "demo",
) -> ScalpAuthorityExpectation:
    return ScalpAuthorityExpectation(
        admission_mode="signed_validation",
        account_mode=account_mode,
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
        validation_expires_at_epoch=datetime.now(UTC).timestamp() + 86_400.0,
        runtime_boot_id=BOOT_ID,
        authority_revision=authority_revision,
    )


def _broker_spec(symbol: str) -> dict[str, object]:
    crypto = symbol in IG_MT4_CRYPTO_CFD_SYMBOLS
    point = 0.01 if crypto else (0.001 if symbol.endswith("JPY") else 0.00001)
    return {
        "broker_symbol": f"{symbol}.IG",
        "lot_size": 1.0 if crypto else 100_000.0,
        "min_lot": 0.01,
        "lot_step": 0.01,
        "max_lot": 100.0,
        "point": point,
        "digits": 2 if crypto else (3 if symbol.endswith("JPY") else 5),
        "tick_size": point,
        "tick_value": 1.0,
        "stop_level_points": 10.0,
        "freeze_level_points": 0.0,
        "trade_allowed": True,
        "margin_required": 50.0 if crypto else 1_000.0,
    }


def _service(tmp_path: Path) -> RuntimeService:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    migrated = migrate_database(
        database_url=database_url,
        root=Path(__file__).resolve().parents[1],
    )
    assert migrated.get("ok") is True, migrated
    get_settings.cache_clear()
    service = RuntimeService(
        database_url=database_url,
        execution_provider="mt4",
    )
    now = datetime.now(UTC).timestamp()
    market_source = _market_source()
    assert market_source is not None
    market_source_fields = market_source.to_fields()
    service.patch_state(
        {
            "system_status": "connected",
            "last_heartbeat": now,
            "broker_account_mode": "demo",
            "broker_account_scope": "ig-demo-scope",
            "broker_account_currency": "USD",
            "broker_venue_id": "ig_mt4",
            "bridge_producer_identity": PRODUCER_IDENTITY,
            "bridge_producer_instance_id": PRODUCER_INSTANCE_ID,
            "bridge_terminal_lease_scope": TERMINAL_LEASE_SCOPE,
            "bridge_credential_generation_id": CREDENTIAL_GENERATION_ID,
            "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
            "bridge_consumer_lease": {
                "schema_version": "fxstack_bridge_consumer_lease_v2",
                "consumer_identity": PRODUCER_IDENTITY,
                "producer_instance_id": PRODUCER_INSTANCE_ID,
                "terminal_lease_scope": TERMINAL_LEASE_SCOPE,
                "credential_generation_id": CREDENTIAL_GENERATION_ID,
                "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
                "expires_at": now + 120.0,
            },
            "bridge_market_source": market_source_fields,
            "freemargin": 9_000.0,
            "equity": 10_000.0,
            "symbol_specs_ts": now,
            "symbol_specs": {
                symbol: _broker_spec(symbol) for symbol in IG_MT4_SCALP_SYMBOLS
            },
            "symbol_specs_market_source": market_source_fields,
            "symbol_specs_market_source_id": market_source.source_id,
            "runtime_status": "running",
            "runtime_last_cycle_ts": now,
            "runtime_startup": {"boot_id": BOOT_ID},
            "runtime_attestation": {"runtime_boot_id": BOOT_ID},
            "runtime_diag": {
                "orchestration_live": {
                    "authority_revision": AUTHORITY_REVISION,
                    "enabled": True,
                    "mode": "live",
                    "runtime_enabled": True,
                    "queue_kill_active": False,
                    "active_pair_scope": list(IG_MT4_SCALP_SYMBOLS),
                    "active_sleeve_scope": ["scalp"],
                    "active_intent_scope": ["enter"],
                },
                "live_command_admission": {
                    "allowed": True,
                    "pairs": {
                        symbol: {"allowed": True} for symbol in IG_MT4_SCALP_SYMBOLS
                    },
                },
            },
        }
    )
    service.enable_production_execution_egress(runtime_boot_id=BOOT_ID)
    return service


def _activate(
    service: RuntimeService,
    *,
    generation_id: str = "defined-strategy-generation-1",
    identity_digit: str = "1",
) -> dict:
    authority_revision = int(
        service.get_state()["runtime_diag"]["orchestration_live"]["authority_revision"]
    )
    authority = build_active_authority(
        _authority_expectation(
            generation_id=generation_id,
            engine_sha256=identity_digit * 64,
            authority_revision=authority_revision,
        ),
        activated_at=datetime.now(UTC).timestamp(),
    )
    result = service.compare_and_set_production_scalp_authority(
        next_authority=authority,
        validation_verification=_validation_verification(authority),
    )
    assert result["updated"] is True, result
    return authority


def _payload(
    service: RuntimeService,
    *,
    symbol: str,
    minute: int,
    authority_revision: int,
    quote_rates: dict[str, float] | None = None,
) -> dict:
    trace_id = f"trace-{symbol}-{minute}"
    now_epoch = datetime.now(UTC).timestamp()
    universe = project_ig_mt4_contract_universe(
        service.get_state(),
        now_ts=now_epoch,
        max_age_secs=120.0,
    )
    assert universe.ok, universe.errors
    rates = dict(quote_rates or {})
    contract_metadata = broker_contract_sizing_metadata(
        universe,
        symbol=symbol,
        margin_utilization_cap=0.25,
        quote_rates=rates,
    )
    contract = universe.contract_for(symbol)
    assert contract is not None
    bid, ask, _, _ = _market_geometry(symbol)
    lots = float(contract.min_lot)
    value_per_price_unit = account_value_per_price_unit(
        pair=symbol,
        rates=rates,
        account_currency=universe.account_currency,
        contract_units=contract.lot_size,
    )
    assert value_per_price_unit > 0.0
    fixture_cash_risk = 0.90
    stop_distance_price = fixture_cash_risk / (lots * value_per_price_unit)
    plan_result = build_scalp_broker_entry_plan(
        symbol=symbol,
        side="BUY",
        execution_type="market",
        pending_orders_forbidden=True,
        entry_deadline_epoch=math.ceil(now_epoch) + 5,
        as_of_epoch=now_epoch,
        quote_entry_price=ask,
        reference_mid=bid + (ask - bid) / 2.0,
        stop_distance_price=stop_distance_price,
        target_distance_price=stop_distance_price * 4.0,
        current_spread_bps=(ask - bid) / (bid + (ask - bid) / 2.0) * 1e4,
        win_probability_lower_bound=0.65,
        contract=contract,
        cost_model=ScalpBrokerEntryCostModel.mtvclc(
            p90_spread_bps=100.0,
            commission_bps_per_round_trip=0.0,
            financing_bps_per_trade=0.0,
            convert_on_close_charge_fraction=0.0,
        ),
    )
    assert plan_result.plan is not None, plan_result
    plan = plan_result.plan
    money_at_risk = (
        lots
        * abs(plan.worst_fill_price - plan.sl_price)
        * value_per_price_unit
    )
    assert 0.0 < money_at_risk <= 1.0
    risk_fraction = money_at_risk / 10_000.0
    return {
        **contract_metadata,
        "rollover_guard_schema_version": (
            PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION
        ),
        "rollover_guard_config_sha256": (
            DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY.config_sha256()
        ),
        "command_id": f"production-scalper:{symbol}:{minute}",
        "cmd": "BUY",
        "symbol": symbol,
        "lots": lots,
        **plan.command_fields(),
        "sl_price": plan.sl_price,
        "tp_price": plan.tp_price,
        "broker_contract_sizing": {
            "required": True,
            "status": "approved",
            "symbol": symbol,
            "broker_symbol": contract.broker_symbol,
            "account_currency": universe.account_currency,
            "lots": lots,
            "money_at_risk": money_at_risk,
            "value_per_price_unit": value_per_price_unit,
            "requested_risk_fraction": risk_fraction,
            "budgeted_risk_fraction": risk_fraction,
            "effective_risk_fraction": risk_fraction,
        },
        "correlation_id": f"{symbol}:defined-strategy:{minute}",
        "trace_id": trace_id,
        "orchestration_meta_json": {
            "trace_id": trace_id,
            "authority_revision": int(authority_revision),
            "adaptive_sleeve": "scalp",
        },
    }


def _approval(payload: dict, authority: dict) -> FinalEntryApproval:
    return FinalEntryApproval(
        pair=str(payload["symbol"]),
        side=str(payload["cmd"]),
        risk_approved_payload=dict(payload),
        canonical_ready=True,
        governed_allowed=True,
        rollout_active=True,
        rollout_mode="canary",
        rollout_pair_allowlisted=True,
        correlation_id=str(payload["correlation_id"]),
        trace_id=str(payload["trace_id"]),
        broker_account_mode="demo",
        broker_account_scope="ig-demo-scope",
        authority_revision=int(authority["authority_revision"]),
        runtime_boot_id=BOOT_ID,
        sleeve="scalp",
        strategy_authority=dict(authority),
    )


def _attested_market_entry_ack(
    service: RuntimeService,
    command_id: str,
    *,
    ticket: int,
) -> dict:
    row = service.get_command(command_id)
    assert row is not None
    payload = dict(row.get("payload_json") or {})
    plan = dict(payload.get("broker_entry_plan") or {})
    owner_token = str(payload.get("owner_token") or "")
    side = str(row.get("cmd") or "").strip().upper()
    return {
        "command_id": command_id,
        "actuals_schema": MT4_ORDER_ACTUALS_SCHEMA,
        "actual_command_id": command_id,
        "status": "acked",
        "mutation_state": "confirmed",
        "ticket": int(ticket),
        "magic": int(row.get("magic") or 0),
        "owner_token": owner_token,
        "symbol": str(row.get("symbol") or "").strip().upper(),
        "actual_cmd": side,
        "actual_side": side,
        "actual_symbol": str(row.get("symbol") or "").strip().upper(),
        "actual_broker_symbol": str(
            payload.get("expected_broker_contract_broker_symbol") or ""
        ),
        "actual_execution_type": "instant_market",
        "actual_ticket": int(ticket),
        "actual_magic": int(row.get("magic") or 0),
        "actual_owner_token": owner_token,
        "actual_order_comment": f"{owner_token}.ig",
        "actual_lots": float(row.get("lots") or 0.0),
        "actual_remaining_lots": float(row.get("lots") or 0.0),
        "actual_close_time": 0,
        "actual_open_price": float(plan.get("worst_fill_price") or 0.0),
        "actual_sl_price": float(row.get("sl_price") or 0.0),
        "actual_tp_price": float(row.get("tp_price") or 0.0),
    }


def _market_geometry(symbol: str) -> tuple[float, float, float, float]:
    if symbol in IG_MT4_CRYPTO_CFD_SYMBOLS:
        return 109.9, 110.1, 100.0, 120.0
    if symbol.endswith("JPY"):
        return 159.99, 160.01, 159.0, 162.0
    return 1.0999, 1.1001, 1.0991, 1.1020


def _record_tick(
    service: RuntimeService,
    symbol: str,
    *,
    bid: float | None = None,
    ask: float | None = None,
    observed_at: float | None = None,
) -> None:
    default_bid, default_ask, _, _ = _market_geometry(symbol)
    current_bid = default_bid if bid is None else bid
    current_ask = default_ask if ask is None else ask
    payload = {
        "symbol": symbol,
        "bid": current_bid,
        "ask": current_ask,
        "spread": current_ask - current_bid,
    }
    market_source = _market_source()
    assert market_source is not None
    payload.update(market_source.to_fields())
    if observed_at is not None:
        payload["ts"] = observed_at
    service.record_tick(payload)


def _market_source(
    *,
    broker_account_scope: str = "ig-demo-scope",
    broker_venue_id: str = "ig_mt4",
    producer_identity: str = PRODUCER_IDENTITY,
    producer_instance_id: str = PRODUCER_INSTANCE_ID,
    terminal_lease_scope: str = TERMINAL_LEASE_SCOPE,
    credential_generation_id: str = CREDENTIAL_GENERATION_ID,
    bridge_protocol_version: str = BRIDGE_PROTOCOL_VERSION,
):
    return build_authenticated_market_source(
        broker_account_scope=broker_account_scope,
        broker_venue_id=broker_venue_id,
        producer_identity=producer_identity,
        producer_instance_id=producer_instance_id,
        terminal_lease_scope=terminal_lease_scope,
        credential_generation_id=credential_generation_id,
        bridge_protocol_version=bridge_protocol_version,
    )


def test_production_scalper_uses_final_approval_enqueue_and_poll(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "BTCUSD")
    payload = _payload(
        service,
        symbol="BTCUSD",
        minute=1_800_000_000,
        authority_revision=authority["authority_revision"],
    )

    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )

    assert queued_code == 200, queued
    assert queued["status"] == "queued"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    stored_payload = dict(stored["payload_json"])
    assert stored_payload["strategy_lane"] == "production_scalper"
    assert (
        stored_payload["expected_strategy_generation_id"] == authority["generation_id"]
    )
    delivered, delivered_code = service.poll_command()
    assert delivered_code == 200, delivered
    assert delivered["status"] == "ok"
    assert delivered["command"]["command_id"] == payload["command_id"]


def test_direct_demo_cannot_enqueue_and_queued_legacy_row_expires_before_poll(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURUSD")
    payload = _payload(
        service,
        symbol="EURUSD",
        minute=1_800_000_000,
        authority_revision=authority["authority_revision"],
    )
    direct_payload = {
        **payload,
        **command_binding_fields(authority),
        "expected_strategy_admission_mode": SCALP_ADMISSION_MODE_DIRECT_DEMO,
        "expected_account_mode": "demo",
        "expected_account_scope": "ig-demo-scope",
        "expected_authority_revision": authority["authority_revision"],
    }
    direct_command = ExecutionCommand.from_payload(
        direct_payload,
        default_session_id="test-session",
        ttl_secs=30.0,
    )

    accepted, reason = service.store.enqueue_command(direct_command)

    assert accepted is False
    assert reason == "scalp_strategy_admission_mode_signed_validation_required"
    assert service.get_command(str(payload["command_id"])) is None

    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued
    assert queued["status"] == "queued"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    legacy_payload = dict(stored["payload_json"])
    legacy_payload["expected_strategy_admission_mode"] = (
        SCALP_ADMISSION_MODE_DIRECT_DEMO
    )
    with service.store.engine.begin() as conn:
        conn.execute(
            service.store.commands.update()
            .where(service.store.commands.c.command_id == payload["command_id"])
            .values(payload_json=legacy_payload)
        )

    polled, poll_code = service.poll_command()

    assert poll_code == 200
    assert polled["status"] == "empty"
    quarantined = service.get_command(str(payload["command_id"]))
    assert quarantined is not None
    assert quarantined["status"] == "expired"
    assert quarantined["reason"] == (
        "poll_authority_revoked:"
        "scalp_strategy_admission_mode_signed_validation_required"
    )


def test_rollover_diagnostic_does_not_refuse_transactional_enqueue_for_crypto(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fxstack.runtime.scalp_rollover_guard import (
        evaluate_production_scalp_rollover_guard,
    )

    blocked = evaluate_production_scalp_rollover_guard(
        datetime(2026, 8, 3, 20, 50, tzinfo=UTC).timestamp()
    )
    assert blocked.entry_allowed is True
    assert blocked.entry_blackout_active is False
    monkeypatch.setattr(
        postgres_store_module,
        "evaluate_production_scalp_rollover_guard",
        lambda _now: blocked,
    )
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "BTCUSD")
    payload = _payload(
        service,
        symbol="BTCUSD",
        minute=1_800_000_010,
        authority_revision=authority["authority_revision"],
    )

    queued, status_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )

    assert status_code == 200, queued
    assert queued["status"] == "queued"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    assert stored["status"] == "queued"


def test_command_queued_before_rollover_diagnostic_remains_pollable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fxstack.runtime.scalp_rollover_guard import (
        evaluate_production_scalp_rollover_guard,
    )

    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "ETHUSD")
    payload = _payload(
        service,
        symbol="ETHUSD",
        minute=1_800_000_011,
        authority_revision=authority["authority_revision"],
    )
    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued

    blocked = evaluate_production_scalp_rollover_guard(
        datetime(2026, 1, 15, 21, 55, tzinfo=UTC).timestamp()
    )
    assert blocked.entry_allowed is True
    assert blocked.entry_blackout_active is False
    monkeypatch.setattr(
        postgres_store_module,
        "evaluate_production_scalp_rollover_guard",
        lambda _now: blocked,
    )

    polled, poll_code = service.poll_command()

    assert poll_code == 200
    assert polled["status"] == "ok"
    assert polled["command"]["command_id"] == payload["command_id"]
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    assert stored["status"] == "delivered"


def test_legacy_tick_cannot_satisfy_production_scalp_enqueue(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    bid, ask, _, _ = _market_geometry("BTCUSD")
    service.record_tick(
        {
            "symbol": "BTCUSD",
            "bid": bid,
            "ask": ask,
            "spread": ask - bid,
        }
    )
    payload = _payload(
        service,
        symbol="BTCUSD",
        minute=1_800_000_100,
        authority_revision=authority["authority_revision"],
    )

    refused, status_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )

    assert status_code == 503, refused
    assert refused["error"] == "market_tick_missing"
    assert service.get_command(str(payload["command_id"])) is None


def test_newer_foreign_source_tick_cannot_replace_pinned_tick_at_poll(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "BTCUSD")
    payload = _payload(
        service,
        symbol="BTCUSD",
        minute=1_800_000_101,
        authority_revision=authority["authority_revision"],
    )
    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued

    foreign_source = _market_source(
        broker_account_scope="foreign-account",
        broker_venue_id="foreign-venue",
        producer_identity="second-authenticated-ea",
        terminal_lease_scope="second-terminal",
        credential_generation_id="generation-2",
    )
    assert foreign_source is not None
    service.record_tick(
        {
            "symbol": "BTCUSD",
            "bid": 1.0,
            "ask": 2.0,
            "spread": 1.0,
            **foreign_source.to_fields(),
        }
    )

    delivered, delivered_code = service.poll_command()

    assert delivered_code == 200, delivered
    assert delivered["status"] == "ok"
    assert delivered["command"]["command_id"] == payload["command_id"]


def test_legacy_tick_cannot_satisfy_production_scalp_poll(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "BTCUSD")
    payload = _payload(
        service,
        symbol="BTCUSD",
        minute=1_800_000_102,
        authority_revision=authority["authority_revision"],
    )
    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued

    with service.store.engine.begin() as conn:
        conn.execute(service.store.market_ticks.delete())
    bid, ask, _, _ = _market_geometry("BTCUSD")
    service.record_tick(
        {
            "symbol": "BTCUSD",
            "bid": bid,
            "ask": ask,
            "spread": ask - bid,
        }
    )

    polled, poll_code = service.poll_command()

    assert poll_code == 200
    assert polled["status"] == "empty"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    assert stored["status"] == "expired"
    assert stored["reason"] == "poll_authority_revoked:market_tick_missing"


def test_revocation_after_enqueue_expires_before_broker_poll(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURUSD")
    payload = _payload(
        service,
        symbol="EURUSD",
        minute=1_800_000_001,
        authority_revision=authority["authority_revision"],
    )
    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued

    revoked = service.compare_and_set_production_scalp_authority(
        next_authority={
            "status": "revoked",
            "generation_id": authority["generation_id"],
            "reason": "operator_revoked_defined_strategy",
        },
        safety_dominant=True,
    )
    assert revoked["updated"] is True, revoked

    polled, poll_code = service.poll_command()
    assert poll_code == 200
    assert polled["status"] == "empty"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    assert stored["status"] == "expired"
    assert stored["reason"] == "poll_authority_revoked:scalp_authority_inactive"


def _arm_protective_management_store(
    service: RuntimeService,
) -> dict:
    disabled = service.disable_execution_egress(
        reason="validation_certificate_revoked",
        preserve_queued_exposure_reducing=True,
    )
    assert disabled["execution_egress_enabled"] is False
    state = service.get_state()
    authority = dict(state["production_scalp_authority"])
    assert authority["status"] == "revoked"
    runtime_diag = dict(state["runtime_diag"])
    runtime_diag["live_command_admission"] = {
        "allowed": False,
        "blockers": ["validation_certificate_revoked"],
        "pairs": {
            symbol: {"allowed": False} for symbol in IG_MT4_SCALP_SYMBOLS
        },
    }
    service.patch_state({"runtime_diag": runtime_diag})
    current_live = dict(
        service.get_state()["runtime_diag"]["orchestration_live"]
    )
    service.patch_orchestration_live_state(
        updates={
            "enabled": True,
            "mode": "live",
            "runtime_enabled": True,
            "queue_kill_active": False,
            "queue_kill_reason": "",
            "active_pair_scope": list(IG_MT4_SCALP_SYMBOLS),
            "active_sleeve_scope": ["scalp"],
            "active_intent_scope": ["exit"],
            "budget_scale": 0.0,
            "release_status": "protective_management_only",
        },
        expected_live_authority=current_live,
        allow_reenable=True,
    )
    enabled = service.enable_production_execution_egress(runtime_boot_id=BOOT_ID)
    assert enabled["execution_egress_enabled"] is True
    state = service.get_state()
    assert state["execution_egress_authority"][
        "protective_management_only"
    ] is True
    assert state["runtime_diag"]["live_command_admission"]["allowed"] is False
    return authority


def _protective_close_payload(
    authority: dict,
    *,
    managed_entry_command_id: str,
    owner_token: str,
) -> dict:
    if (
        authority.get("schema_version")
        == SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA
    ):
        management_binding = _legacy_v2_binding_fields(authority)
    else:
        management_binding = {
            key: value
            for key, value in command_binding_fields(authority).items()
            if key.startswith("expected_strategy_")
        }
    return {
        **management_binding,
        "command_id": "protective-management-close-101",
        "cmd": "CLOSE",
        "symbol": "EURUSD",
        "lots": 0.0,
        "target_ticket": 101,
        "magic": 246_810,
        "owner_token": owner_token,
        "ownership_contract": TICKET_OWNER_CONTRACT,
        "intent": "EXIT",
        "management_strategy": authority["strategy_id"],
        "managed_entry_command_id": managed_entry_command_id,
    }


def _legacy_v2_binding_fields(authority: dict) -> dict:
    return {
        "expected_strategy_authority_schema": authority["schema_version"],
        "expected_strategy_admission_mode": authority["admission_mode"],
        "expected_strategy_account_mode": authority["account_mode"],
        "expected_strategy_generation_id": authority["generation_id"],
        "expected_strategy_id": authority["strategy_id"],
        "expected_strategy_engine_sha256": authority["engine_sha256"],
        "expected_strategy_config_sha256": authority["config_sha256"],
        "expected_strategy_validation_evidence_sha256": authority[
            "validation_evidence_sha256"
        ],
        "expected_strategy_validation_expires_at_epoch": authority[
            "validation_expires_at_epoch"
        ],
        "expected_strategy_venue_id": authority["venue_id"],
        "expected_strategy_binding_sha256": authority["binding_sha256"],
        "expected_strategy_runtime_boot_id": authority["runtime_boot_id"],
        "expected_strategy_authority_revision": authority[
            "authority_revision"
        ],
    }


def _rearm_entry_store(service: RuntimeService) -> None:
    state = service.get_state()
    runtime_diag = dict(state["runtime_diag"])
    runtime_diag["live_command_admission"] = {
        "allowed": True,
        "pairs": {
            symbol: {"allowed": True} for symbol in IG_MT4_SCALP_SYMBOLS
        },
    }
    service.patch_state({"runtime_diag": runtime_diag})
    current_live = dict(
        service.get_state()["runtime_diag"]["orchestration_live"]
    )
    service.patch_orchestration_live_state(
        updates={
            "enabled": True,
            "mode": "live",
            "runtime_enabled": True,
            "queue_kill_active": False,
            "queue_kill_reason": "",
            "active_pair_scope": list(IG_MT4_SCALP_SYMBOLS),
            "active_sleeve_scope": ["scalp"],
            "active_intent_scope": ["enter"],
            "budget_scale": 1.0,
        },
        expected_live_authority=current_live,
        allow_reenable=True,
    )
    service.enable_production_execution_egress(runtime_boot_id=BOOT_ID)


def test_protective_management_store_delivers_only_exact_owner_close(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    active_authority = _activate(service)
    _record_tick(service, "EURUSD")
    historical_entry = _payload(
        service,
        symbol="EURUSD",
        minute=1_800_000_011,
        authority_revision=active_authority["authority_revision"],
    )
    queued_entry, queued_entry_code = service.submit_approved_command(
        historical_entry,
        approval=_approval(historical_entry, active_authority),
    )
    assert queued_entry_code == 200, queued_entry
    delivered_entry, delivered_entry_code = service.poll_command()
    assert delivered_entry_code == 200, delivered_entry
    stored_entry = service.get_command(historical_entry["command_id"])
    assert stored_entry is not None
    entry_owner_token = str(stored_entry["payload_json"]["owner_token"])
    acked_entry, acked_entry_code = service.ack_command(
        _attested_market_entry_ack(
            service,
            str(historical_entry["command_id"]),
            ticket=101,
        )
    )
    assert acked_entry_code == 200, acked_entry

    # Simulate an exact historical position opened by the retired demo lane.
    # New direct-demo rows cannot be built or enqueued, but its immutable owner
    # identity remains parseable for this one protective CLOSE.
    legacy_entry_authority = {
        "schema_version": SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA,
        "status": "revoked",
        "source": "production_runtime",
        "admission_mode": SCALP_ADMISSION_MODE_DIRECT_DEMO,
        "account_mode": "demo",
        "generation_id": active_authority["generation_id"],
        "strategy_id": active_authority["strategy_id"],
        "engine_sha256": active_authority["engine_sha256"],
        "config_sha256": active_authority["config_sha256"],
        "validation_evidence_sha256": active_authority[
            "research_evidence_sha256"
        ],
        "validation_expires_at_epoch": active_authority[
            "validation_expires_at_epoch"
        ],
        "venue_id": active_authority["venue_id"],
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "bracket_policy": "broker_native_sl_tp",
        "max_entries_per_symbol_utc_day": 1,
        "runtime_boot_id": active_authority["runtime_boot_id"],
        "authority_revision": active_authority["authority_revision"],
    }
    legacy_entry_authority["binding_sha256"] = authority_binding_sha256(
        legacy_entry_authority
    )
    legacy_stored_payload = dict(stored_entry["payload_json"])
    legacy_stored_payload.update(_legacy_v2_binding_fields(legacy_entry_authority))
    with service.store.engine.begin() as conn:
        conn.execute(
            service.store.commands.update()
            .where(
                service.store.commands.c.command_id
                == historical_entry["command_id"]
            )
            .values(payload_json=legacy_stored_payload)
        )

    service.disable_execution_egress(
        reason="generation_rotation",
        preserve_queued_exposure_reducing=True,
    )
    _rearm_entry_store(service)
    latest_authority = _activate(
        service,
        generation_id="defined-strategy-generation-2",
        identity_digit="6",
    )
    revoked_authority = _arm_protective_management_store(service)
    assert revoked_authority["generation_id"] == latest_authority["generation_id"]
    assert revoked_authority["generation_id"] != active_authority["generation_id"]

    entry_payload = _payload(
        service,
        symbol="EURUSD",
        minute=1_800_000_012,
        authority_revision=active_authority["authority_revision"],
    )
    blocked_entry, blocked_entry_code = service.submit_approved_command(
        entry_payload,
        approval=_approval(entry_payload, active_authority),
    )
    assert blocked_entry_code == 403, blocked_entry
    assert blocked_entry["status"] == "forbidden"

    missing_binding = _protective_close_payload(
        legacy_entry_authority,
        managed_entry_command_id=historical_entry["command_id"],
        owner_token=entry_owner_token,
    )
    missing_binding.pop("expected_strategy_binding_sha256")
    refused_close, refused_code = service.submit_command(missing_binding)
    assert refused_code == 403, refused_close
    assert refused_close["error"] == (
        "execution_egress_protective_"
        "expected_strategy_binding_sha256_missing"
    )

    close_payload = _protective_close_payload(
        legacy_entry_authority,
        managed_entry_command_id=historical_entry["command_id"],
        owner_token=entry_owner_token,
    )
    queued, queued_code = service.submit_command(close_payload)
    assert queued_code == 200, queued
    assert queued["status"] == "queued"

    close_all, close_all_code = service.submit_command(
        {
            "command_id": "protective-management-close-all",
            "cmd": "CLOSE_ALL",
            "magic": 246_810,
            "intent": "EXIT",
        }
    )
    assert close_all_code == 403, close_all
    assert close_all["error"] == (
        "execution_egress_protective_command_blocked"
    )

    delivered, delivered_code = service.poll_command()
    assert delivered_code == 200, delivered
    assert delivered["status"] == "ok"
    assert delivered["command"]["command_id"] == close_payload["command_id"]
    assert delivered["command"]["target_ticket"] == 101


def test_protective_management_accepts_fresh_position_join_after_ack_loss(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURUSD")
    entry_payload = _payload(
        service,
        symbol="EURUSD",
        minute=1_800_000_013,
        authority_revision=authority["authority_revision"],
    )
    queued_entry, queued_entry_code = service.submit_approved_command(
        entry_payload,
        approval=_approval(entry_payload, authority),
    )
    assert queued_entry_code == 200, queued_entry
    delivered_entry, delivered_entry_code = service.poll_command()
    assert delivered_entry_code == 200, delivered_entry
    stored_entry = service.get_command(entry_payload["command_id"])
    assert stored_entry is not None
    owner_token = str(stored_entry["payload_json"]["owner_token"])
    now = datetime.now(UTC).timestamp()
    service.patch_state(
        {
            "positions_snapshot_authoritative": True,
            "positions_snapshot_source": "positions_snapshot",
            "positions_snapshot_schema": "fxstack_mt4_positions_snapshot_v2",
            "positions_snapshot_contract_current": True,
            "positions_snapshot_account_scope": "ig-demo-scope",
            "positions_snapshot_token": "fresh-position-snapshot",
            "positions_snapshot_received_at": now - 1_000.0,
            "positions": [
                {
                    "symbol": "EURUSD",
                    "ticket": 202,
                    "magic": 246_810,
                    "order_comment": owner_token,
                    "lots": entry_payload["lots"],
                }
            ],
        }
    )

    revoked_authority = _arm_protective_management_store(service)
    close_payload = _protective_close_payload(
        authority,
        managed_entry_command_id=entry_payload["command_id"],
        owner_token=owner_token,
    )
    close_payload["command_id"] = "protective-management-close-202"
    close_payload["target_ticket"] = 202
    assert revoked_authority["generation_id"] == authority["generation_id"]

    stale_close, stale_close_code = service.submit_command(close_payload)
    assert stale_close_code == 403, stale_close
    assert stale_close["error"] == (
        "execution_egress_protective_entry_ownership_unconfirmed"
    )

    service.patch_state({"positions_snapshot_received_at": now})
    queued_close, queued_close_code = service.submit_command(close_payload)
    assert queued_close_code == 200, queued_close
    delivered_close, delivered_close_code = service.poll_command()
    assert delivered_close_code == 200, delivered_close
    assert delivered_close["status"] == "ok"
    assert delivered_close["command"]["target_ticket"] == 202


def test_stale_broker_contract_after_enqueue_expires_before_poll(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "NZDJPY")
    _record_tick(service, "USDJPY")
    payload = _payload(
        service,
        symbol="NZDJPY",
        minute=1_800_000_002,
        authority_revision=authority["authority_revision"],
        quote_rates={"USDJPY": 160.0},
    )
    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued

    service.patch_state({"symbol_specs_ts": (datetime.now(UTC).timestamp() - 121.0)})
    polled, poll_code = service.poll_command()
    assert poll_code == 200
    assert polled["status"] == "empty"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    assert stored["status"] == "expired"
    assert stored["reason"] == ("poll_authority_revoked:broker_contract_specs_stale")


def test_broker_contract_drift_after_enqueue_expires_before_poll(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "BTCUSD")
    payload = _payload(
        service,
        symbol="BTCUSD",
        minute=1_800_000_003,
        authority_revision=authority["authority_revision"],
    )
    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued

    state = service.get_state()
    specs = dict(state["symbol_specs"])
    specs["BTCUSD"] = {
        **dict(specs["BTCUSD"]),
        "broker_symbol": "BTCUSD.CHANGED",
    }
    service.patch_state(
        {
            "symbol_specs": specs,
            "symbol_specs_ts": datetime.now(UTC).timestamp(),
        }
    )

    polled, poll_code = service.poll_command()
    assert poll_code == 200
    assert polled["status"] == "empty"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    assert stored["status"] == "expired"
    assert stored["reason"] == (
        "poll_authority_revoked:expected_broker_contract_broker_symbol_changed"
    )


def test_unrelated_malformed_contract_does_not_block_selected_queue_entry(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURUSD")
    payload = _payload(
        service,
        symbol="EURUSD",
        minute=1_800_000_019,
        authority_revision=authority["authority_revision"],
    )
    state = service.get_state()
    specs = dict(state["symbol_specs"])
    specs["NZDJPY"] = {**dict(specs["NZDJPY"]), "lot_size": 0.0}
    service.patch_state(
        {
            "symbol_specs": specs,
            "symbol_specs_ts": datetime.now(UTC).timestamp(),
        }
    )

    queued, status_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )

    assert status_code == 200, queued
    assert queued["status"] == "queued"


def test_closed_symbol_does_not_revoke_authority_or_block_unrelated_entry(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    eurusd_payload = _payload(
        service,
        symbol="EURUSD",
        minute=1_800_000_030,
        authority_revision=authority["authority_revision"],
    )
    btcusd_payload = _payload(
        service,
        symbol="BTCUSD",
        minute=1_800_000_030,
        authority_revision=authority["authority_revision"],
    )
    state = service.get_state()
    specs = dict(state["symbol_specs"])
    specs["BTCUSD"] = {**dict(specs["BTCUSD"]), "trade_allowed": False}
    service.patch_state(
        {
            "symbol_specs": specs,
            "symbol_specs_ts": datetime.now(UTC).timestamp(),
        }
    )

    refreshed = service.compare_and_set_production_scalp_authority(
        next_authority=authority,
        validation_verification=_validation_verification(authority),
    )
    assert refreshed["updated"] is True, refreshed
    assert service.get_state()["production_scalp_authority"]["status"] == "active"

    _record_tick(service, "EURUSD")
    eurusd, eurusd_code = service.submit_approved_command(
        eurusd_payload,
        approval=_approval(eurusd_payload, authority),
    )
    assert eurusd_code == 200, eurusd
    assert eurusd["status"] == "queued"

    _record_tick(service, "BTCUSD")
    btcusd, btcusd_code = service.submit_approved_command(
        btcusd_payload,
        approval=_approval(btcusd_payload, authority),
    )
    assert btcusd_code == 403, btcusd
    assert btcusd["status"] == "forbidden"
    assert btcusd["error"] == "broker_contract_trade_not_allowed:BTCUSD"
    assert service.get_command(str(btcusd_payload["command_id"])) is None


def test_adverse_quote_drift_is_refused_at_enqueue(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURUSD")
    payload = _payload(
        service,
        symbol="EURUSD",
        minute=1_800_000_020,
        authority_revision=authority["authority_revision"],
    )
    _record_tick(service, "EURUSD", bid=1.1003, ask=1.1004)

    refused, status_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )

    assert status_code == 403, refused
    assert refused["error"] == "scalp_market_entry_price_beyond_worst_fill"
    assert service.get_command(str(payload["command_id"])) is None


def test_adverse_quote_drift_after_enqueue_expires_before_poll(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURUSD")
    payload = _payload(
        service,
        symbol="EURUSD",
        minute=1_800_000_021,
        authority_revision=authority["authority_revision"],
    )
    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued
    _record_tick(service, "EURUSD", bid=1.1003, ask=1.1004)

    polled, poll_code = service.poll_command()

    assert poll_code == 200
    assert polled["status"] == "empty"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    assert stored["status"] == "expired"
    assert stored["reason"] == (
        "poll_authority_revoked:scalp_market_entry_price_beyond_worst_fill"
    )


def test_fresh_current_conversion_rate_is_required_at_enqueue(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURJPY")
    _record_tick(
        service,
        "USDJPY",
        bid=99.9,
        ask=100.1,
        observed_at=datetime.now(UTC).timestamp() - 3_600.0,
    )
    # JPYUSD is not an allowed market-data extra when USDJPY is already in the
    # exact strategy scope, so it must not rescue the stale exact conversion.
    _record_tick(service, "JPYUSD", bid=0.0099, ask=0.0101)
    payload = _payload(
        service,
        symbol="EURJPY",
        minute=1_800_000_022,
        authority_revision=authority["authority_revision"],
        quote_rates={"USDJPY": 100.0},
    )

    refused, status_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )

    assert status_code == 403, refused
    assert refused["error"] == (
        "broker_contract_order_cash_risk_conversion_unresolvable"
    )
    assert service.get_command(str(payload["command_id"])) is None


def test_fresh_inverse_conversion_rate_is_resolved_at_enqueue(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURGBP")
    _record_tick(service, "GBPUSD", bid=1.2499, ask=1.2501)
    payload = _payload(
        service,
        symbol="EURGBP",
        minute=1_800_000_024,
        authority_revision=authority["authority_revision"],
        quote_rates={"GBPUSD": 1.25},
    )

    queued, status_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )

    assert status_code == 200, queued
    assert queued["status"] == "queued"


def test_wide_direct_conversion_uses_ask_at_enqueue_and_poll(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURJPY")
    _record_tick(service, "USDJPY", bid=100.0, ask=110.0)
    payload = _payload(
        service,
        symbol="EURJPY",
        minute=1_800_000_025,
        authority_revision=authority["authority_revision"],
        quote_rates={"USDJPY": 110.0},
    )

    queued, status_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    delivered, delivered_code = service.poll_command()

    assert status_code == 200, queued
    assert queued["status"] == "queued"
    assert delivered_code == 200, delivered
    assert delivered["status"] == "ok"


def test_wide_inverse_conversion_uses_bid_at_enqueue_and_poll(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURGBP")
    _record_tick(service, "GBPUSD", bid=1.20, ask=1.30)
    payload = _payload(
        service,
        symbol="EURGBP",
        minute=1_800_000_026,
        authority_revision=authority["authority_revision"],
        quote_rates={"GBPUSD": 1.20},
    )

    queued, status_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    delivered, delivered_code = service.poll_command()

    assert status_code == 200, queued
    assert queued["status"] == "queued"
    assert delivered_code == 200, delivered
    assert delivered["status"] == "ok"


def test_wide_direct_conversion_adverse_move_expires_at_poll(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURJPY")
    _record_tick(service, "USDJPY", bid=100.0, ask=110.0)
    payload = _payload(
        service,
        symbol="EURJPY",
        minute=1_800_000_027,
        authority_revision=authority["authority_revision"],
        quote_rates={"USDJPY": 110.0},
    )
    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued
    _record_tick(service, "USDJPY", bid=70.0, ask=80.0)

    polled, poll_code = service.poll_command()

    assert poll_code == 200
    assert polled["status"] == "empty"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    assert stored["status"] == "expired"
    assert stored["reason"] == (
        "poll_authority_revoked:broker_contract_order_cash_risk_exceeded"
    )


def test_conversion_event_staleness_expires_before_poll_risk_approval(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURJPY")
    _record_tick(service, "USDJPY", bid=100.0, ask=110.0)
    payload = _payload(
        service,
        symbol="EURJPY",
        minute=1_800_000_030,
        authority_revision=authority["authority_revision"],
        quote_rates={"USDJPY": 110.0},
    )
    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued
    with service.store.engine.begin() as conn:
        conn.execute(
            service.store.market_ticks.update()
            .where(service.store.market_ticks.c.symbol == "USDJPY")
            .values(ts=datetime.now(UTC).timestamp() - 120.0)
        )

    polled, poll_code = service.poll_command()

    assert poll_code == 200
    assert polled["status"] == "empty"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    assert stored["status"] == "expired"
    assert stored["reason"] == (
        "poll_authority_revoked:broker_contract_order_cash_risk_conversion_unresolvable"
    )


def test_wide_inverse_conversion_adverse_move_expires_at_poll(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURGBP")
    _record_tick(service, "GBPUSD", bid=1.20, ask=1.30)
    payload = _payload(
        service,
        symbol="EURGBP",
        minute=1_800_000_028,
        authority_revision=authority["authority_revision"],
        quote_rates={"GBPUSD": 1.20},
    )
    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued
    # The market-entry proof is sized from its adverse fill bound, so the
    # unchanged EURGBP quote carries some risk slack. Move conversion far
    # enough to exceed that bound as well.
    _record_tick(service, "GBPUSD", bid=1.80, ask=1.90)

    polled, poll_code = service.poll_command()

    assert poll_code == 200
    assert polled["status"] == "empty"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    assert stored["status"] == "expired"
    assert stored["reason"] == (
        "poll_authority_revoked:broker_contract_order_cash_risk_exceeded"
    )


def test_non_usd_account_uses_fresh_usd_triangulation_at_enqueue_and_poll(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    service.patch_state({"broker_account_currency": "GBP"})
    authority = _activate(service)
    _record_tick(service, "EURAUD")
    _record_tick(service, "AUDUSD", bid=0.60, ask=0.70)
    _record_tick(service, "GBPUSD", bid=1.20, ask=1.25)
    payload = _payload(
        service,
        symbol="EURAUD",
        minute=1_800_000_029,
        authority_revision=authority["authority_revision"],
        quote_rates={"AUDGBP": 0.60 / 1.25},
    )

    queued, status_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    delivered, delivered_code = service.poll_command()

    assert status_code == 200, queued
    assert queued["status"] == "queued"
    assert delivered_code == 200, delivered
    assert delivered["status"] == "ok"


def test_conversion_rate_drift_after_enqueue_expires_before_poll(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURJPY")
    _record_tick(service, "USDJPY", bid=99.9, ask=100.1)
    payload = _payload(
        service,
        symbol="EURJPY",
        minute=1_800_000_023,
        authority_revision=authority["authority_revision"],
        quote_rates={"USDJPY": 100.0},
    )
    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued
    _record_tick(service, "USDJPY", bid=49.9, ask=50.1)

    polled, poll_code = service.poll_command()

    assert poll_code == 200
    assert polled["status"] == "empty"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    assert stored["status"] == "expired"
    assert stored["reason"] == (
        "poll_authority_revoked:broker_contract_order_cash_risk_exceeded"
    )


def test_favorable_quote_drift_cannot_consume_protection_cushion_at_poll(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "EURUSD")
    payload = _payload(
        service,
        symbol="EURUSD",
        minute=1_800_000_024,
        authority_revision=authority["authority_revision"],
    )
    queued, queued_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )
    assert queued_code == 200, queued

    # A lower BUY quote remains comfortably inside the approved worst fill,
    # but moves bid close enough to the fixed SL to consume the five-point
    # transit cushion. Poll must expire it before the EA can see the command.
    _record_tick(service, "EURUSD", bid=1.09952, ask=1.09954)
    polled, poll_code = service.poll_command()

    assert poll_code == 200
    assert polled["status"] == "empty"
    stored = service.get_command(str(payload["command_id"]))
    assert stored is not None
    assert stored["status"] == "expired"
    assert stored["reason"] == (
        "poll_authority_revoked:scalp_market_entry_stop_below_broker_minimum"
    )


def test_off_quantum_scalper_lots_are_refused_at_enqueue(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "BTCUSD")
    payload = _payload(
        service,
        symbol="BTCUSD",
        minute=1_800_000_004,
        authority_revision=authority["authority_revision"],
    )
    payload["lots"] = 0.015
    refused, status_code = service.submit_approved_command(
        payload,
        approval=_approval(payload, authority),
    )

    assert status_code == 403, refused
    assert refused["error"] == "broker_contract_order_lot_step_mismatch"
    assert service.get_command(str(payload["command_id"])) is None


def test_daily_frequency_is_rechecked_atomically(tmp_path) -> None:
    service = _service(tmp_path)
    authority = _activate(service)
    _record_tick(service, "ETHUSD")
    first = _payload(
        service,
        symbol="ETHUSD",
        minute=1_800_000_010,
        authority_revision=authority["authority_revision"],
    )
    queued, queued_code = service.submit_approved_command(
        first,
        approval=_approval(first, authority),
    )
    assert queued_code == 200, queued
    delivered, delivered_code = service.poll_command()
    assert delivered_code == 200 and delivered["status"] == "ok"
    acked, acked_code = service.ack_command(
        _attested_market_entry_ack(
            service,
            str(first["command_id"]),
            ticket=101,
        )
    )
    assert acked_code == 200, acked

    second = _payload(
        service,
        symbol="ETHUSD",
        minute=1_800_000_011,
        authority_revision=authority["authority_revision"],
    )
    refused, refused_code = service.submit_approved_command(
        second,
        approval=_approval(second, authority),
    )
    assert refused_code == 403, refused
    assert refused["error"] == "scalp_daily_entry_frequency_exhausted"
    assert service.get_command(str(second["command_id"])) is None


def test_generic_state_patch_cannot_mint_or_replace_scalp_authority(tmp_path) -> None:
    service = _service(tmp_path)
    service.patch_state(
        {
            "production_scalp_authority": {
                "status": "active",
                "generation_id": "forged",
            }
        }
    )
    assert not service.get_state().get("production_scalp_authority")

    authority = _activate(service)
    service.patch_state(
        {
            "production_scalp_authority": {
                **authority,
                "engine_sha256": "9" * 64,
            }
        }
    )
    assert (
        service.get_state()["production_scalp_authority"]["engine_sha256"]
        == authority["engine_sha256"]
    )


def test_signed_authority_account_mode_must_match_attested_broker_mode(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    revision = int(
        service.get_state()["runtime_diag"]["orchestration_live"][
            "authority_revision"
        ]
    )
    authority = build_active_authority(
        _authority_expectation(
            account_mode="real",
            generation_id="signed-real-on-demo",
            engine_sha256="1" * 64,
            authority_revision=revision,
        ),
        activated_at=datetime.now(UTC).timestamp(),
    )
    refused = service.compare_and_set_production_scalp_authority(
        next_authority=authority,
        validation_verification=_validation_verification(authority),
    )

    assert refused["updated"] is False
    assert refused["reason"] == "scalp_authority_broker_account_mode_changed"
    assert not service.get_state().get("production_scalp_authority")

    real_market_source = _market_source(broker_account_scope="ig-real-scope")
    assert real_market_source is not None
    real_market_source_fields = real_market_source.to_fields()
    service.patch_state(
        {
            "broker_account_mode": "real",
            "broker_account_scope": "ig-real-scope",
            "bridge_market_source": real_market_source_fields,
            "symbol_specs_market_source": real_market_source_fields,
            "symbol_specs_market_source_id": real_market_source.source_id,
        }
    )
    accepted = service.compare_and_set_production_scalp_authority(
        next_authority=authority,
        validation_verification=_validation_verification(authority),
    )

    assert accepted["updated"] is True, accepted
    assert accepted["authority"]["account_mode"] == "real"


@pytest.mark.parametrize("missing_symbol", ["EURUSD", "NZDJPY"])
def test_activation_requires_live_admission_for_every_pair(
    tmp_path, missing_symbol: str
) -> None:
    service = _service(tmp_path)
    state = service.get_state()
    runtime_diag = dict(state["runtime_diag"])
    admission = dict(runtime_diag["live_command_admission"])
    pairs = dict(admission["pairs"])
    pairs[missing_symbol] = {"allowed": False}
    service.patch_state(
        {
            "runtime_diag": {
                **runtime_diag,
                "live_command_admission": {**admission, "pairs": pairs},
            }
        }
    )
    current_revision = int(
        service.get_state()["runtime_diag"]["orchestration_live"]["authority_revision"]
    )
    authority = build_active_authority(
        _authority_expectation(
            generation_id="defined-strategy-generation-2",
            engine_sha256="1" * 64,
            authority_revision=current_revision,
        ),
        activated_at=datetime.now(UTC).timestamp(),
    )
    refused = service.compare_and_set_production_scalp_authority(
        next_authority=authority,
        validation_verification=_validation_verification(authority),
    )
    assert refused["updated"] is False
    assert refused["reason"] in {
        "scalp_authority_pair_admission_incomplete",
        "scalp_authority_revision_changed",
    }
