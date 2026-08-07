from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION
from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_CRYPTO_CFD_SYMBOLS,
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
    get_ig_mt4_instrument,
)
from fxstack.runtime import runner
from fxstack.runtime import scalp_live_loop as loop
from fxstack.runtime.broker_contract_state import (
    project_ig_mt4_selected_contract_universe,
)
from fxstack.runtime.market_source_identity import (
    MARKET_SOURCE_SCHEMA,
    build_authenticated_market_source,
)
from fxstack.runtime.mtvclc_entry_qualification import (
    MTVCLCEntryQualificationResult,
    QualifiedMTVCLCEntryCandidate,
)
from fxstack.runtime.mtvclc_entry_quote import (
    RefreshedMTVCLCEntryCandidate,
    refresh_mtvclc_entry_quote,
)
from fxstack.runtime.mtvclc_proposal_batch import (
    MTVCLC_RUNTIME_PROFILE_ID,
    MTVCLCProposalBatchDiagnostics,
    MTVCLCProposalBatchResult,
    MTVCLCSymbolProposalDiagnostic,
)
from fxstack.runtime.mtvclc_runtime_release import (
    RUNTIME_RELEASE_AUTHORITY,
    MTVCLCRuntimeReleaseVerification,
)
from fxstack.runtime.scalp_engine_identity import ProductionScalpEngineIdentity
from fxstack.runtime.scalp_execution_authority import (
    ScalpAuthorityExpectation,
    build_active_authority,
)
from fxstack.runtime.scalp_runtime_admission import ScalpRuntimeAdmission
from fxstack.strategy.mtvclc import (
    FROZEN_MTVCLC_POLICY,
    MAX_QUOTE_GAP_SECONDS,
    MT4_BID_PRICE_BASIS,
    MT4_IVOLUME_SOURCE,
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
    REQUIRED_COMPLETED_M1_BARS,
    STOP_COST_MULTIPLE,
    TARGET_COST_MULTIPLE,
    TIME_STOP_M1_BARS,
    MTVCLCCostCalibration,
    MTVCLCTradeCandidate,
)


NOW = 1_900_000_020.0
SIGNAL_EPOCH = 1_899_999_960
EXPECTED_ENTRY_EPOCH = 1_900_000_020
ENTRY_DEADLINE_EPOCH = EXPECTED_ENTRY_EPOCH + 5
PRODUCER_IDENTITY = "ig-mt4-production-ea"
PRODUCER_INSTANCE_ID = "ig-mt4-terminal-instance-1"
TERMINAL_LEASE_SCOPE = "ig-mt4-terminal-scope"
CREDENTIAL_GENERATION_ID = "ig-mt4-generation-1"
GENERATION_ID = "mtvclc-runtime-release-generation-1"
ENGINE_SHA256 = "1" * 64
RUNTIME_RELEASE_CERTIFICATE_SHA256 = "2" * 64
RUNTIME_RELEASE_SIGNING_KEY_ID = "3" * 64
EVIDENCE_SHA256 = "4" * 64
EVIDENCE_SIGNING_KEY_ID = "5" * 64
REGISTRY_SHA256 = "6" * 64
QUALIFICATION_SURFACE_SHA256 = "7" * 64
COST_MAPPING_SHA256 = "8" * 64
EXECUTION_CONTRACT_SHA256 = "9" * 64


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _market_source():
    source = build_authenticated_market_source(
        broker_account_scope="ig-demo-scope",
        broker_venue_id=IG_MT4_VENUE_ID,
        producer_identity=PRODUCER_IDENTITY,
        producer_instance_id=PRODUCER_INSTANCE_ID,
        terminal_lease_scope=TERMINAL_LEASE_SCOPE,
        credential_generation_id=CREDENTIAL_GENERATION_ID,
        bridge_protocol_version=BRIDGE_PROTOCOL_VERSION,
    )
    assert source is not None
    return source


def _prices(symbol: str) -> tuple[float, float]:
    if symbol in IG_MT4_CRYPTO_CFD_SYMBOLS:
        return 1_000.00, 1_000.05
    if symbol.endswith("JPY"):
        return 150.000, 150.010
    return 1.10000, 1.10010


def _broker_spec(symbol: str) -> dict[str, Any]:
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
        "margin_required": 50.0 if crypto else 1_000.0,
        "trade_allowed": True,
    }


def _cost(symbol: str) -> MTVCLCCostCalibration:
    instrument = get_ig_mt4_instrument(symbol)
    assert instrument is not None
    return MTVCLCCostCalibration(
        symbol=symbol,
        calibration_id="mtvclc-composition-costs-v1",
        source_sha256="a" * 64,
        p90_spread_bps=2.0,
        commission_bps_per_round_trip=0.25,
        financing_bps_per_trade=0.10,
        account_currency="USD",
        pnl_currency=instrument.quote_ccy,
        convert_on_close_charge_fraction=(
            0.0 if instrument.quote_ccy == "USD" else 0.005
        ),
    )


def _cost_row(cost: MTVCLCCostCalibration) -> dict[str, Any]:
    return {
        "symbol": cost.symbol,
        "calibration_id": cost.calibration_id,
        "source_sha256": cost.source_sha256,
        "p90_spread_bps": cost.p90_spread_bps,
        "commission_bps_per_round_trip": cost.commission_bps_per_round_trip,
        "financing_bps_per_trade": cost.financing_bps_per_trade,
        "account_currency": cost.account_currency,
        "pnl_currency": cost.pnl_currency,
        "convert_on_close_charge_fraction": (
            cost.convert_on_close_charge_fraction
        ),
        "adverse_execution_debit_bps": cost.adverse_execution_debit_bps,
    }


def _verification(
    costs: tuple[MTVCLCCostCalibration, ...],
    *,
    valid: bool = True,
) -> MTVCLCRuntimeReleaseVerification:
    cost_by_symbol = {cost.symbol: cost for cost in costs}
    bounds = {
        symbol: {"BUY": 0.90, "SELL": 0.90}
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    cell_hashes = {
        symbol: {
            side: _digest(f"cell:{symbol}:{side}")
            for side in ("BUY", "SELL")
        }
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    evidence_cost_rows = {
        symbol: _digest(f"evidence-cost:{symbol}")
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    reason = "" if valid else "validation_revoked"
    return MTVCLCRuntimeReleaseVerification(
        valid=valid,
        reason=reason,
        errors=() if valid else (reason,),
        authenticated=valid,
        revocation_verified=valid,
        admission_mode="signed_validation",
        release_bundle_sha256="b" * 64,
        certificate_sha256=RUNTIME_RELEASE_CERTIFICATE_SHA256,
        runtime_release_certificate_sha256=(
            RUNTIME_RELEASE_CERTIFICATE_SHA256
        ),
        evidence_bundle_sha256="c" * 64,
        evidence_certificate_sha256="d" * 64,
        evidence_sha256=EVIDENCE_SHA256,
        signing_key_id=RUNTIME_RELEASE_SIGNING_KEY_ID,
        runtime_release_signing_key_id=RUNTIME_RELEASE_SIGNING_KEY_ID,
        evidence_signing_key_id=EVIDENCE_SIGNING_KEY_ID,
        registry_generation_id=GENERATION_ID,
        generation_id=GENERATION_ID,
        strategy_id=MTVCLC_STRATEGY_ID,
        strategy_version=MTVCLC_STRATEGY_VERSION,
        engine_sha256=ENGINE_SHA256,
        engine_component_sha256=(),
        config_id=MTVCLC_CONFIG_ID,
        config_sha256=MTVCLC_CONFIG_SHA256,
        evaluator_source_sha256="e" * 64,
        venue_id=IG_MT4_VENUE_ID,
        account_mode="demo",
        scope_version=IG_MT4_SCALP_SCOPE_VERSION,
        symbol_scope=IG_MT4_SCALP_SYMBOLS,
        max_entries_per_symbol_utc_day=1,
        maximum_account_currency_risk_per_trade=1.0,
        issued_at_epoch=NOW - 3_600.0,
        expires_at_epoch=NOW + 3_600.0,
        release_expires_at_epoch=NOW + 3_600.0,
        evidence_expires_at_epoch=NOW + 7_200.0,
        registry_expires_at_epoch=NOW + 3_600.0,
        registry_revision=11,
        registry_sha256=REGISTRY_SHA256,
        authority_purpose="mtvclc_ig_demo_runtime_release_eligibility.v1",
        authority=dict(RUNTIME_RELEASE_AUTHORITY),
        deployment_sha256="f" * 64,
        execution_contract_sha256=EXECUTION_CONTRACT_SHA256,
        qualification_surface_sha256=QUALIFICATION_SURFACE_SHA256,
        win_probability_lower_bounds=bounds,
        base_break_even_probabilities={
            symbol: {
                side: cost_by_symbol[symbol].break_even_win_probability
                for side in ("BUY", "SELL")
            }
            for symbol in IG_MT4_SCALP_SYMBOLS
        },
        evidence_cell_sha256=cell_hashes,
        evidence_cost_row_sha256=evidence_cost_rows,
        cost_mapping_sha256=COST_MAPPING_SHA256,
        cost_rows_sha256="0" * 64,
        cost_calibration_id="mtvclc-composition-costs-v1",
        cost_calibration_source_sha256="a" * 64,
        cost_calibration_source_sha256_by_symbol={
            symbol: cost_by_symbol[symbol].source_sha256
            for symbol in IG_MT4_SCALP_SYMBOLS
        },
        cost_calibration_row_sha256={
            symbol: cost_by_symbol[symbol].row_sha256()
            for symbol in IG_MT4_SCALP_SYMBOLS
        },
        cost_calibrations={
            symbol: _cost_row(cost_by_symbol[symbol])
            for symbol in IG_MT4_SCALP_SYMBOLS
        },
    )


def _admission() -> ScalpRuntimeAdmission:
    costs = tuple(_cost(symbol) for symbol in IG_MT4_SCALP_SYMBOLS)
    return ScalpRuntimeAdmission(
        valid=True,
        reason="",
        errors=(),
        verification=_verification(costs),
        engine_identity=ProductionScalpEngineIdentity(
            engine_sha256=ENGINE_SHA256,
            component_sha256=(),
        ),
        cost_calibrations=costs,
        bundle_file_sha256="b" * 64,
        evidence_public_key_file_sha256="c" * 64,
        release_public_key_file_sha256="d" * 64,
        bundle_path="test-only-runtime-release.json",
        evidence_public_key_path="test-only-evidence-public.pem",
        release_public_key_path="test-only-release-public.pem",
    )


def _active_authority() -> dict[str, Any]:
    return build_active_authority(
        ScalpAuthorityExpectation(
            generation_id=GENERATION_ID,
            strategy_id=MTVCLC_STRATEGY_ID,
            strategy_version=MTVCLC_STRATEGY_VERSION,
            engine_sha256=ENGINE_SHA256,
            config_id=MTVCLC_CONFIG_ID,
            config_sha256=MTVCLC_CONFIG_SHA256,
            runtime_release_certificate_sha256=(
                RUNTIME_RELEASE_CERTIFICATE_SHA256
            ),
            runtime_release_signing_key_id=RUNTIME_RELEASE_SIGNING_KEY_ID,
            research_evidence_sha256=EVIDENCE_SHA256,
            research_evidence_signing_key_id=EVIDENCE_SIGNING_KEY_ID,
            registry_generation_id=GENERATION_ID,
            registry_revision=11,
            registry_sha256=REGISTRY_SHA256,
            qualification_surface_sha256=QUALIFICATION_SURFACE_SHA256,
            cost_mapping_sha256=COST_MAPPING_SHA256,
            execution_contract_sha256=EXECUTION_CONTRACT_SHA256,
            validation_expires_at_epoch=NOW + 3_600.0,
            runtime_boot_id="scalp-runtime-boot",
            authority_revision=9,
        ),
        activated_at=NOW - 60.0,
    )


def _proposal(
    symbol: str = "EURUSD",
    *,
    side: str = "BUY",
) -> MTVCLCTradeCandidate:
    instrument = get_ig_mt4_instrument(symbol)
    assert instrument is not None
    source = _market_source()
    cost = _cost(symbol)
    bid, ask = _prices(symbol)
    entry = ask if side == "BUY" else bid
    target_bps = TARGET_COST_MULTIPLE * cost.recorded_cost_bps
    stop_bps = STOP_COST_MULTIPLE * cost.recorded_cost_bps
    stop = entry * (
        1.0 - stop_bps / 1e4 if side == "BUY" else 1.0 + stop_bps / 1e4
    )
    target = entry * (
        1.0 + target_bps / 1e4
        if side == "BUY"
        else 1.0 - target_bps / 1e4
    )
    mid = (bid + ask) / 2.0
    return MTVCLCTradeCandidate(
        symbol=symbol,
        instrument_id=instrument.instrument_id,
        venue_id=IG_MT4_VENUE_ID,
        allowed=True,
        reasons=(),
        bar_source_id=source.source_id,
        bar_source_version=MARKET_SOURCE_SCHEMA,
        quote_source_id=source.source_id,
        quote_source_version=MARKET_SOURCE_SCHEMA,
        market_source_identity_sha256=source.source_id,
        cost_calibration_id=cost.calibration_id,
        cost_calibration_source_sha256=cost.source_sha256,
        cost_calibration_row_sha256=cost.row_sha256(),
        side=side,  # type: ignore[arg-type]
        signal_epoch=SIGNAL_EPOCH,
        expected_entry_epoch=EXPECTED_ENTRY_EPOCH,
        entry_deadline_epoch=ENTRY_DEADLINE_EPOCH,
        entry_epoch=EXPECTED_ENTRY_EPOCH,
        entry_day="2030-03-17",
        entry_bid=bid,
        entry_ask=ask,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        live_spread_bps=(ask - bid) / mid * 1e4,
        p90_spread_bps=cost.p90_spread_bps,
        recorded_cost_bps=cost.recorded_cost_bps,
        conversion_charge_fraction=cost.convert_on_close_charge_fraction,
        p_star=cost.break_even_win_probability,
        target_bps=target_bps,
        stop_bps=stop_bps,
        time_stop_bars=TIME_STOP_M1_BARS,
        maximum_quote_gap_seconds=MAX_QUOTE_GAP_SECONDS,
        volume_v90=100.0,
        signal_tick_volume=150,
        bid_body_bps=4.0 if side == "BUY" else -4.0,
        bid_close_location=0.9 if side == "BUY" else 0.1,
    )


def _qualified(
    proposal: MTVCLCTradeCandidate,
    admission: ScalpRuntimeAdmission,
) -> QualifiedMTVCLCEntryCandidate:
    cost = admission.cost_calibration_for(proposal.symbol)
    assert cost is not None
    lower = 0.90
    target = float(proposal.target_bps or 0.0)
    stop = float(proposal.stop_bps or 0.0)
    conversion = cost.convert_on_close_charge_fraction
    edge = (
        lower * target * (1.0 - conversion)
        - (1.0 - lower) * stop * (1.0 + conversion)
        - cost.recorded_cost_bps
    )
    return QualifiedMTVCLCEntryCandidate(
        proposal=proposal,
        admitted_cost=cost,
        win_probability_lower_bound=lower,
        conservative_expected_edge_bps=edge,
        reward_risk_ratio=target / stop,
        release_generation_id=GENERATION_ID,
        release_certificate_sha256=RUNTIME_RELEASE_CERTIFICATE_SHA256,
        release_signing_key_id=RUNTIME_RELEASE_SIGNING_KEY_ID,
        evidence_sha256=EVIDENCE_SHA256,
        evidence_cell_sha256=_digest(
            f"cell:{proposal.symbol}:{proposal.side}"
        ),
        evidence_cost_row_sha256=_digest(
            f"evidence-cost:{proposal.symbol}"
        ),
        qualification_surface_sha256=QUALIFICATION_SURFACE_SHA256,
        release_expires_at_epoch=NOW + 3_600.0,
    )


def test_entry_contract_serializers_preserve_recursive_dataclass_shape() -> None:
    proposal = _proposal()
    admission = _admission()
    qualified = _qualified(proposal, admission)
    qualification = MTVCLCEntryQualificationResult(
        proposal=proposal,
        qualified_candidate=qualified,
        reasons=(),
    )
    source = _market_source()
    bid, ask = _prices(proposal.symbol)
    refresh = refresh_mtvclc_entry_quote(
        qualified,
        {
            "symbol": proposal.symbol,
            "provider": "mt4_bridge",
            "instrument": {
                "canonical_symbol": proposal.symbol,
                "venue": IG_MT4_VENUE_ID,
            },
            "bid": bid,
            "ask": ask,
            "received_at_epoch": NOW,
            "transport_fresh": True,
            "source_event_baseline_initialized": True,
            "source_event_token": "42",
            "market_event_sequence": 7,
            "market_event_received_at_epoch": NOW - 0.1,
            "market_event_fresh": True,
            "market_event_reason": "ok",
            "quality_flags": (),
            **source.to_fields(),
        },
        as_of_epoch=NOW + 1.0,
    )
    assert refresh.accepted
    assert refresh.refreshed_candidate is not None

    contracts = (
        proposal,
        qualified,
        qualification,
        refresh.refreshed_candidate,
        refresh.diagnostics,
        refresh,
    )
    for contract in contracts:
        assert contract.to_dict() == asdict(contract)

    payload = refresh.to_dict()
    payload["qualified_candidate"]["proposal"]["symbol"] = "MUTATED"
    payload["diagnostics"]["accepted"] = False
    assert refresh.qualified_candidate.proposal.symbol == "EURUSD"
    assert refresh.diagnostics.accepted is True
    assert refresh.to_dict() == asdict(refresh)


def _batch(
    proposals: tuple[MTVCLCTradeCandidate, ...],
    *,
    as_of_epoch: float = NOW,
    structural_ready_symbols: set[str] | None = None,
) -> MTVCLCProposalBatchResult:
    ready_symbols = (
        set(IG_MT4_SCALP_SYMBOLS)
        if structural_ready_symbols is None
        else set(structural_ready_symbols)
    )
    proposal_symbols = {proposal.symbol for proposal in proposals}
    common_closed = int(as_of_epoch // 60) * 60 - 60
    diagnostics = tuple(
        MTVCLCSymbolProposalDiagnostic(
            symbol=symbol,
            structural_ready=symbol in ready_symbols,
            structural_reasons=(
                () if symbol in ready_symbols else ("market_closed",)
            ),
            raw_bar_count=(
                REQUIRED_COMPLETED_M1_BARS + 1
                if symbol in ready_symbols
                else 0
            ),
            filtered_current_bar_count=(1 if symbol in ready_symbols else 0),
            finalized_bar_count=(
                REQUIRED_COMPLETED_M1_BARS if symbol in ready_symbols else 0
            ),
            selected_history_count=(
                REQUIRED_COMPLETED_M1_BARS if symbol in ready_symbols else 0
            ),
            latest_finalized_minute_epoch=(
                common_closed if symbol in ready_symbols else None
            ),
            raw_quote_count=1,
            selected_quote_count=1,
            quote_transport_received_at_epochs=(NOW,),
            cost_calibration_id=_cost(symbol).calibration_id,
            cost_calibration_source_sha256=_cost(symbol).source_sha256,
            cost_calibration_row_sha256=_cost(symbol).row_sha256(),
            evaluation_allowed=(
                symbol in proposal_symbols if symbol in ready_symbols else None
            ),
            evaluation_reasons=(
                ()
                if symbol in proposal_symbols or symbol not in ready_symbols
                else ("no_signal",)
            ),
        )
        for symbol in IG_MT4_SCALP_SYMBOLS
    )
    source = _market_source()
    return MTVCLCProposalBatchResult(
        proposals=proposals,
        diagnostics=MTVCLCProposalBatchDiagnostics(
            accepted=True,
            reasons=(),
            strategy_profile=MTVCLC_RUNTIME_PROFILE_ID,
            as_of_epoch=float(as_of_epoch),
            current_minute_epoch=int(as_of_epoch // 60) * 60,
            common_closed_minute_epoch=common_closed,
            expected_symbols=IG_MT4_SCALP_SYMBOLS,
            observed_bar_symbols=IG_MT4_SCALP_SYMBOLS,
            observed_quote_symbols=IG_MT4_SCALP_SYMBOLS,
            observed_cost_symbols=IG_MT4_SCALP_SYMBOLS,
            market_source_id=source.source_id,
            producer_instance_id=PRODUCER_INSTANCE_ID,
            symbol_diagnostics=diagnostics,
        ),
    )


class _Record(SimpleNamespace):
    def to_dict(self) -> dict[str, Any]:
        return deepcopy(vars(self))


class _CycleService:
    def __init__(self) -> None:
        market_source = _market_source()
        self.state: dict[str, Any] = {
            "broker_venue_id": IG_MT4_VENUE_ID,
            "broker_account_mode": "demo",
            "broker_account_scope": "ig-demo-scope",
            "broker_account_scope_schema": "ig_mt4_account_scope.v1",
            "broker_account_scope_version": 1,
            "broker_server": "IG-DEMO",
            "broker_company": "IG",
            "broker_account_currency": "USD",
            "broker_account_magic": 24_681,
            "bridge_producer_identity": PRODUCER_IDENTITY,
            "bridge_producer_instance_id": PRODUCER_INSTANCE_ID,
            "bridge_terminal_lease_scope": TERMINAL_LEASE_SCOPE,
            "bridge_credential_generation_id": CREDENTIAL_GENERATION_ID,
            "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
            "bridge_consumer_lease": {
                "schema_version": "fxstack_bridge_consumer_lease_v1",
                "consumer_identity": PRODUCER_IDENTITY,
                "producer_instance_id": PRODUCER_INSTANCE_ID,
                "terminal_lease_scope": TERMINAL_LEASE_SCOPE,
                "credential_generation_id": CREDENTIAL_GENERATION_ID,
                "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
                "expires_at": NOW + 120.0,
            },
            "bridge_market_source": market_source.to_fields(),
            "freemargin": 9_000.0,
            "symbol_specs_ts": NOW - 1.0,
            "symbol_specs": {
                symbol: _broker_spec(symbol) for symbol in IG_MT4_SCALP_SYMBOLS
            },
            "symbol_specs_market_source": market_source.to_fields(),
            "symbol_specs_market_source_id": market_source.source_id,
            "system_status": "connected",
            "last_heartbeat": datetime.fromtimestamp(NOW, UTC).isoformat(),
            "heartbeat_stale_after_secs": 30.0,
            "positions": [],
            "equity": 10_000.0,
            "runtime_last_cycle_ts": NOW - 10.0,
            "governance": {"schema_version": "predecessor"},
            "production_scalp_authority": _active_authority(),
            "runtime_diag": {
                "orchestration_live": {
                    "authority_revision": 9,
                    "bundle_run_id": "mtvclc-bundle",
                    "current_stage_index": 0,
                }
            },
        }
        self.approved_submissions: list[dict[str, Any]] = []
        self.raw_submissions: list[dict[str, Any]] = []
        self.patches: list[dict[str, Any]] = []
        self.decision_writes: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.governance_snapshot_reads = 0
        self.metrics_reads = 0
        self.state_reads = 0
        self.runtime_diag_patch_calls = 0
        self.cycle_commits = 0
        self.generic_command_reads = 0
        self.reconciliation_command_reads: list[bool] = []

    def get_state(self) -> dict[str, Any]:
        self.state_reads += 1
        return deepcopy(self.state)

    def get_commands(self, *, limit: int) -> list[dict[str, Any]]:
        self.generic_command_reads += 1
        assert limit > 0
        return deepcopy(self.commands)

    def get_scalp_reconciliation_commands(
        self,
        *,
        include_historical: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        assert limit > 0
        self.reconciliation_command_reads.append(bool(include_historical))
        return deepcopy(self.commands)

    def get_metrics(self) -> dict[str, Any]:
        self.metrics_reads += 1
        return {"feature_parity": {"breaches": 0}}

    def get_state_and_governance_metrics(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.governance_snapshot_reads += 1
        return deepcopy(self.state), {"feature_parity": {"breaches": 0}}

    def submit_approved_command(
        self,
        payload: dict[str, Any],
        *,
        approval: Any,
        proto: str,
    ) -> tuple[dict[str, Any], int]:
        self.approved_submissions.append(
            {
                "payload": deepcopy(payload),
                "approval": approval,
                "proto": proto,
            }
        )
        return {"status": "queued", "command_id": payload["command_id"]}, 200

    def submit_command(
        self,
        payload: dict[str, Any],
        *,
        proto: str,
    ) -> tuple[dict[str, Any], int]:
        self.raw_submissions.append({"payload": deepcopy(payload), "proto": proto})
        return {"status": "queued"}, 200

    def patch_state(
        self,
        patch: dict[str, Any],
        *,
        runtime_diag_patch: dict[str, Any] | None = None,
        runtime_diag_remove: tuple[str, ...] = (),
    ) -> None:
        materialized = deepcopy(patch)
        if runtime_diag_patch is not None or runtime_diag_remove:
            self.runtime_diag_patch_calls += 1
            runtime_diag = deepcopy(self.state.get("runtime_diag") or {})
            for key in runtime_diag_remove:
                runtime_diag.pop(key, None)
            runtime_diag.update(deepcopy(runtime_diag_patch or {}))
            materialized["runtime_diag"] = runtime_diag
        self.patches.append(materialized)
        self.state.update(deepcopy(materialized))

    def store_decisions(
        self,
        *,
        decisions: list[dict[str, Any]],
        vol: float,
        diagnostics: dict[str, Any],
    ) -> None:
        self.decision_writes.append(
            {
                "decisions": deepcopy(decisions),
                "vol": vol,
                "diagnostics": deepcopy(diagnostics),
            }
        )

    def commit_state_and_decisions(
        self,
        patch: dict[str, Any],
        *,
        runtime_diag_patch: dict[str, Any] | None = None,
        runtime_diag_remove: tuple[str, ...] = (),
        decisions: list[dict[str, Any]],
        vol: float,
        diagnostics: dict[str, Any],
    ) -> None:
        self.cycle_commits += 1
        self.patch_state(
            patch,
            runtime_diag_patch=runtime_diag_patch,
            runtime_diag_remove=runtime_diag_remove,
        )
        self.store_decisions(
            decisions=decisions,
            vol=vol,
            diagnostics=diagnostics,
        )


def _cycle_settings() -> SimpleNamespace:
    return SimpleNamespace(
        mt4_bridge_url="http://127.0.0.1:58710",
        entry_strategy_family="mtvclc",
        live_armed=True,
        live_expected_account_mode="demo",
        production_scalp_bar_history_limit=242,
        production_scalp_contract_max_age_secs=60.0,
        bridge_stale_heartbeat_secs=30.0,
        bridge_stale_tick_secs=5.0,
        max_total_positions=4,
        max_pair_positions=1,
        max_new_entries_per_cycle=1,
        policy_version="mtvclc-test",
    )


@dataclass
class _CycleContext:
    admission: ScalpRuntimeAdmission
    batch_holder: dict[str, MTVCLCProposalBatchResult]
    evaluation_calls: list[dict[str, Any]]
    risk_calls: list[dict[str, Any]]


def _install_cycle_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    service: _CycleService,
    live: bool,
    side: str = "BUY",
) -> _CycleContext:
    admission = _admission()
    proposal = _proposal(side=side)
    batch_holder = {"batch": _batch((proposal,))}
    evaluation_calls: list[dict[str, Any]] = []
    risk_calls: list[dict[str, Any]] = []
    governance = {
        "schema_version": "fxstack.capital_governance.v1",
        "computed_at": NOW,
        "source_cycle_ts": NOW - 10.0,
        "paused": False,
        "entries_only": False,
        "shadow_only": False,
        "budget_scale": 1.0,
        "reasons": [],
    }
    reconciliation = _Record(
        owned_positions=(),
        authoritative_open_symbols=(),
        active_queued_entry_symbols=(),
        broker_confirmed_exit_symbols=(),
        active_exit_tickets=(),
        entry_admission_ready=True,
        quarantine_reasons=(),
    )
    lifecycle = _Record(close_decisions=())

    monkeypatch.setattr(loop, "_is_live", lambda _settings: live)
    monkeypatch.setattr(
        loop,
        "verify_configured_scalp_runtime_admission",
        lambda *_args, **_kwargs: admission,
    )
    monkeypatch.setattr(
        loop,
        "scalp_live_command_admission",
        lambda **_kwargs: {"allowed": True, "blockers": []},
    )
    def _ticks(*_args: Any, **kwargs: Any) -> dict[str, dict[str, Any]]:
        assert kwargs["symbols"] == list(IG_MT4_SCALP_SYMBOLS)
        market_source = _market_source()
        rows: dict[str, dict[str, Any]] = {}
        for symbol in IG_MT4_SCALP_SYMBOLS:
            bid, ask = _prices(symbol)
            rows[symbol] = {
                "symbol": symbol,
                "provider": "mt4_bridge",
                "instrument": {
                    "canonical_symbol": symbol,
                    "venue": IG_MT4_VENUE_ID,
                },
                "bid": bid,
                "ask": ask,
                "received_at_epoch": NOW,
                "transport_fresh": True,
                "source_event_baseline_initialized": True,
                "source_event_token": "42",
                "market_event_sequence": 7,
                "market_event_received_at_epoch": NOW - 0.1,
                "market_event_fresh": True,
                "market_event_reason": "ok",
                "quality_flags": (),
                **market_source.to_fields(),
            }
        return rows

    monkeypatch.setattr(loop, "fetch_market_ticks", _ticks)
    monkeypatch.setattr(
        loop,
        "_fetch_exact_m1_bars",
        lambda **_kwargs: (
            {
                symbol: [
                    {"symbol": symbol, "index": index}
                    for index in range(REQUIRED_COMPLETED_M1_BARS + 1)
                ]
                for symbol in IG_MT4_SCALP_SYMBOLS
            },
            {},
        ),
    )

    def _evaluate(**kwargs: Any) -> MTVCLCProposalBatchResult:
        raw_bars = kwargs["raw_bars_by_symbol"]
        raw_quotes = kwargs["raw_quotes_by_symbol"]
        costs = kwargs["costs_by_symbol"]
        assert kwargs["strategy_profile"] == MTVCLC_RUNTIME_PROFILE_ID
        assert kwargs["policy"] == FROZEN_MTVCLC_POLICY
        assert list(raw_bars) == list(IG_MT4_SCALP_SYMBOLS)
        assert list(raw_quotes) in ([], list(IG_MT4_SCALP_SYMBOLS))
        assert list(costs) == list(IG_MT4_SCALP_SYMBOLS)
        assert all(
            isinstance(costs[symbol], MTVCLCCostCalibration)
            for symbol in IG_MT4_SCALP_SYMBOLS
        )
        evaluation_calls.append(dict(kwargs))
        if all(not raw_bars[symbol] for symbol in IG_MT4_SCALP_SYMBOLS):
            return _batch(
                (),
                as_of_epoch=float(kwargs["as_of_epoch"]),
                structural_ready_symbols=set(),
            )
        assert all(
            len(raw_bars[symbol]) == REQUIRED_COMPLETED_M1_BARS + 1
            for symbol in IG_MT4_SCALP_SYMBOLS
        )
        return batch_holder["batch"]

    monkeypatch.setattr(loop, "evaluate_mtvclc_profile_batch", _evaluate)
    monkeypatch.setattr(
        loop,
        "ensure_production_scalp_authority",
        lambda **_kwargs: loop.ScalpAuthorityActivationResult(
            active=True,
            reason="active",
            errors=(),
            authority=deepcopy(service.state["production_scalp_authority"]),
            live_command_admission={"allowed": True, "blockers": []},
            authority_revision=9,
        ),
    )
    monkeypatch.setattr(loop, "reconcile_scalp_restart", lambda **_kwargs: reconciliation)
    monkeypatch.setattr(
        loop,
        "evaluate_scalp_position_lifecycle",
        lambda **_kwargs: lifecycle,
    )

    def _qualify(
        observed: MTVCLCProposalBatchResult,
        *,
        admission: ScalpRuntimeAdmission,
        **_kwargs: Any,
    ) -> tuple[
        MTVCLCProposalBatchResult,
        dict[str, QualifiedMTVCLCEntryCandidate],
        dict[str, dict[str, Any]],
    ]:
        qualified = {
            item.symbol: _qualified(item, admission) for item in observed.proposals
        }
        diagnostics = {
            symbol: {
                "qualified": True,
                "reasons": [],
                "schema_version": candidate.schema_version,
            }
            for symbol, candidate in qualified.items()
        }
        return observed, qualified, diagnostics

    monkeypatch.setattr(loop, "_qualified_batch", _qualify)
    monkeypatch.setattr(
        loop,
        "_binding_governance_policy",
        lambda **_kwargs: deepcopy(governance),
    )

    def _risk_entry(**kwargs: Any) -> dict[str, Any]:
        candidate = kwargs["candidate"]
        assert isinstance(candidate, RefreshedMTVCLCEntryCandidate)
        assert kwargs["state"]["governance"] == governance
        assert tuple(kwargs["contract_universe"].contracts) == (candidate.symbol,)
        risk_calls.append(dict(kwargs))
        approved = {
            **kwargs["broker_entry_plan"].command_fields(),
            "command_id": f"mtvclc-entry-{candidate.symbol.lower()}-1",
            "cmd": candidate.side,
            "symbol": candidate.symbol,
            "lots": 0.1,
        }
        return {
            "verdict": "allow",
            "reason": "approved",
            "trace": ["canonical_risk_approved"],
            "approved_order": approved,
            "governance": deepcopy(governance),
            "rollout": {
                "active": True,
                "mode": "live",
                "pair_allowlisted": True,
            },
        }

    monkeypatch.setattr(loop, "_risk_entry", _risk_entry)
    monkeypatch.setattr(
        loop,
        "build_scalp_runtime_attestation",
        lambda **_kwargs: {"attested": True},
    )
    return _CycleContext(
        admission=admission,
        batch_holder=batch_holder,
        evaluation_calls=evaluation_calls,
        risk_calls=risk_calls,
    )


def _select_candidate(
    context: _CycleContext,
    *,
    symbol: str,
    side: str = "BUY",
    structural_ready_symbols: set[str] | None = None,
) -> MTVCLCTradeCandidate:
    proposal = _proposal(symbol, side=side)
    context.batch_holder["batch"] = _batch(
        (proposal,),
        structural_ready_symbols=structural_ready_symbols,
    )
    return proposal


def test_runner_routes_mtvclc_family_before_model_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(entry_strategy_family="mtvclc")
    preflight = {
        "settings_validated": True,
        "entry_strategy_family": "mtvclc",
    }
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    monkeypatch.setattr(
        runner,
        "validate_runtime_startup",
        lambda observed: preflight if observed is settings else {},
    )
    monkeypatch.setattr(runner, "unknown_fxstack_env_warnings", lambda: [])
    monkeypatch.setattr(
        runner,
        "_uncertified_entry_block_reason",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("model_stack_path_reached")
        ),
    )
    monkeypatch.setattr(
        runner,
        "_perform_startup_bridge_checks",
        lambda _settings: (_ for _ in ()).throw(
            AssertionError("model_stack_bridge_path_reached")
        ),
    )
    monkeypatch.setattr(
        loop,
        "run_production_scalp_loop",
        lambda **kwargs: calls.append(dict(kwargs)),
    )

    runner.run_loop(equity=12_345.0, sleep_secs=7, feature_root="features")

    assert calls == [
        {
            "settings": settings,
            "startup_preflight": preflight,
            "equity": 12_345.0,
            "sleep_secs": 7,
            "feature_root": "features",
        }
    ]


def test_cycle_fetches_exact_241_completed_m1_bars_at_inclusive_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CycleService()
    context = _install_cycle_fakes(monkeypatch, service=service, live=True)
    fetch_calls: list[dict[str, Any]] = []

    def _fetch(**kwargs: Any) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
        fetch_calls.append(dict(kwargs))
        return (
            {
                symbol: [
                    {"symbol": symbol, "index": index}
                    for index in range(REQUIRED_COMPLETED_M1_BARS + 1)
                ]
                for symbol in IG_MT4_SCALP_SYMBOLS
            },
            {},
        )

    monkeypatch.setattr(loop, "_fetch_exact_m1_bars", _fetch)
    monkeypatch.setattr(
        loop,
        "_qualified_batch",
        lambda observed, **_kwargs: (observed, {}, {}),
    )

    result, revoked_latch = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=1_900_000_025.0,
    )

    assert revoked_latch is False
    assert len(fetch_calls) == 1
    assert fetch_calls[0]["limit"] == REQUIRED_COMPLETED_M1_BARS + 1
    assert fetch_calls[0]["require_authenticated_source"] is True
    assert fetch_calls[0]["expected_market_source"] is not None
    assert len(context.evaluation_calls) == 1
    evaluated = context.evaluation_calls[0]["raw_bars_by_symbol"]
    assert list(evaluated) == list(IG_MT4_SCALP_SYMBOLS)
    assert all(
        len(evaluated[symbol]) == REQUIRED_COMPLETED_M1_BARS + 1
        for symbol in IG_MT4_SCALP_SYMBOLS
    )
    assert result.proposal_count == 1
    assert result.diagnostics["bar_fetch_window_open"] is True
    assert result.diagnostics["bar_signal_receipt_poll_attempts"] == 1
    proposal_summary = result.diagnostics["proposal_batch"]
    assert proposal_summary["symbol_diagnostic_count"] == 22
    assert "symbol_diagnostics" not in proposal_summary
    first = service.decision_writes[0]["decisions"][0]["metadata"][
        "proposal_batch_symbol_diagnostic"
    ]
    assert first["selected_history_count"] == REQUIRED_COMPLETED_M1_BARS


def test_live_cycle_reports_runtime_native_cost_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CycleService()
    _install_cycle_fakes(monkeypatch, service=service, live=True)

    result, _ = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=1_900_000_025.0,
    )

    snapshot = result.diagnostics["runtime_cost_snapshot"]
    assert snapshot["applicable"] is True
    assert snapshot["valid"] is True
    assert snapshot["qualification_eligible"] is True
    assert snapshot["reason"] == "runtime_native_costs_selected"
    assert service.governance_snapshot_reads == 1
    assert service.metrics_reads == 0


def test_cycle_fetches_bars_every_cycle_and_runs_protective_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CycleService()
    context = _install_cycle_fakes(monkeypatch, service=service, live=True)
    tick_fetch = loop.fetch_market_ticks
    tick_calls: list[None] = []
    lifecycle_calls: list[dict[str, Any]] = []
    exit_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        loop,
        "_fetch_exact_m1_bars",
        lambda **_kwargs: (
            {symbol: [] for symbol in IG_MT4_SCALP_SYMBOLS},
            {},
        ),
    )

    def _ticks(*args: Any, **kwargs: Any) -> dict[str, dict[str, Any]]:
        tick_calls.append(None)
        return tick_fetch(*args, **kwargs)

    def _lifecycle(**kwargs: Any) -> _Record:
        lifecycle_calls.append(dict(kwargs))
        return _Record(close_decisions=())

    def _protective_exits(**kwargs: Any) -> list[dict[str, Any]]:
        exit_calls.append(dict(kwargs))
        return []

    monkeypatch.setattr(loop, "fetch_market_ticks", _ticks)
    monkeypatch.setattr(loop, "evaluate_scalp_position_lifecycle", _lifecycle)
    monkeypatch.setattr(loop, "_submit_time_stop_exits", _protective_exits)

    result, revoked_latch = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=1_900_000_025.001,
    )

    assert revoked_latch is False
    assert tick_calls == [None]
    assert len(context.evaluation_calls) == 1
    evaluated = context.evaluation_calls[0]["raw_bars_by_symbol"]
    assert list(evaluated) == list(IG_MT4_SCALP_SYMBOLS)
    assert all(not evaluated[symbol] for symbol in IG_MT4_SCALP_SYMBOLS)
    assert len(lifecycle_calls) == 1
    assert lifecycle_calls[0]["finalized_common_minute_epoch"] == 1_899_999_960
    assert lifecycle_calls[0]["time_stop_bars"] == TIME_STOP_M1_BARS
    assert len(exit_calls) == 1
    assert exit_calls[0]["live"] is True
    assert result.proposal_count == 0
    assert result.qualified_count == 0
    assert result.entry_submit_count == 0
    assert result.diagnostics["bar_fetch_window_open"] is True
    assert result.diagnostics["bar_fetch_errors"] == {}
    assert result.diagnostics["bar_signal_receipt_poll_attempts"] == 1
    assert result.diagnostics["bar_signal_receipt_missing_symbols"] == list(
        IG_MT4_SCALP_SYMBOLS
    )


def test_signal_bar_receipt_scope_requires_direct_observable_exact_shift_one() -> None:
    close_epoch = SIGNAL_EPOCH + 60
    bars = {
        symbol: [
            {
                "time": SIGNAL_EPOCH,
                "received_at_epoch": close_epoch + 4.5,
                "volume_source": MT4_IVOLUME_SOURCE,
                "price_basis": MT4_BID_PRICE_BASIS,
            }
        ]
        for symbol in IG_MT4_SCALP_SYMBOLS
    }

    assert (
        loop._signal_bar_receipt_missing_symbols(
            bars,
            signal_minute_epoch=int(SIGNAL_EPOCH),
        )
        == ()
    )

    bars["EURUSD"][0]["volume_source"] = "bridge_market_event_count_v1"
    bars["USDJPY"][0]["received_at_epoch"] = close_epoch + 5.1

    assert loop._signal_bar_receipt_missing_symbols(
        bars,
        signal_minute_epoch=int(SIGNAL_EPOCH),
    ) == ("EURUSD",)


def test_signal_bar_receipt_scans_ordered_history_from_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse_calls = 0
    parse_bar_epoch = loop._parse_bar_epoch

    def counted_parse(value: Any) -> int | None:
        nonlocal parse_calls
        parse_calls += 1
        return parse_bar_epoch(value)

    monkeypatch.setattr(loop, "_parse_bar_epoch", counted_parse)
    bars = {
        symbol: [
            {"time": SIGNAL_EPOCH - offset * 60}
            for offset in range(REQUIRED_COMPLETED_M1_BARS, 0, -1)
        ]
        + [
            {
                "time": SIGNAL_EPOCH,
                "received_at_epoch": SIGNAL_EPOCH + 60,
                "volume_source": MT4_IVOLUME_SOURCE,
                "price_basis": MT4_BID_PRICE_BASIS,
            }
        ]
        for symbol in IG_MT4_SCALP_SYMBOLS
    }

    assert (
        loop._signal_bar_receipt_missing_symbols(
            bars,
            signal_minute_epoch=int(SIGNAL_EPOCH),
        )
        == ()
    )
    assert parse_calls == len(IG_MT4_SCALP_SYMBOLS)

    parse_calls = 0
    for rows in bars.values():
        rows.pop()
    assert loop._signal_bar_receipt_missing_symbols(
        bars,
        signal_minute_epoch=int(SIGNAL_EPOCH),
    ) == IG_MT4_SCALP_SYMBOLS
    assert parse_calls == len(IG_MT4_SCALP_SYMBOLS)


def test_successive_cycles_pick_up_a_delayed_direct_signal_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CycleService()
    _install_cycle_fakes(monkeypatch, service=service, live=True)
    service.state["symbol_specs_ts"] = NOW + 1.0
    fetch_count = 0

    def _fetch(**_kwargs: Any) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
        nonlocal fetch_count
        fetch_count += 1
        receipt = None if fetch_count == 1 else NOW + 6.0
        return (
            {
                symbol: [
                    {"index": index}
                    for index in range(REQUIRED_COMPLETED_M1_BARS)
                ]
                + [
                    {
                        "time": SIGNAL_EPOCH,
                        "received_at_epoch": receipt,
                        "volume_source": MT4_IVOLUME_SOURCE,
                        "price_basis": MT4_BID_PRICE_BASIS,
                    }
                ]
                for symbol in IG_MT4_SCALP_SYMBOLS
            },
            {},
        )

    monkeypatch.setattr(loop, "_fetch_exact_m1_bars", _fetch)

    first, _ = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=NOW + 6.0,
    )
    second, _ = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=NOW + 6.0,
    )

    assert fetch_count == 2
    assert first.diagnostics["bar_signal_receipt_missing_symbols"] == list(
        IG_MT4_SCALP_SYMBOLS
    )
    assert second.diagnostics["bar_signal_receipt_missing_symbols"] == []


def test_m1_history_cache_merges_only_the_boundary_tail() -> None:
    history = {
        symbol: [
            {"time": 120, "close": 1.0},
            {"time": 180, "close": 2.0},
        ]
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    updates = {
        symbol: [
            {"time": 180, "close": 2.5},
            {"time": 240, "close": 3.0},
        ]
        for symbol in IG_MT4_SCALP_SYMBOLS
    }

    retained_row = history["EURUSD"][0]
    incoming_row = updates["EURUSD"][1]
    merged = loop._merge_m1_bar_history(history, updates, limit=3)

    assert tuple(merged) == IG_MT4_SCALP_SYMBOLS
    assert [row["time"] for row in merged["EURUSD"]] == [120, 180, 240]
    assert merged["EURUSD"][1]["close"] == 2.5
    assert merged["EURUSD"][0] is retained_row
    assert merged["EURUSD"][2] is not incoming_row
    incoming_row["close"] = 99.0
    assert merged["EURUSD"][2]["close"] == 3.0
    assert loop._m1_bar_history_is_warm(history, limit=3) is True


def test_runtime_owned_m1_history_reparses_only_the_incoming_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = loop._empty_exact_m1_bar_scope()
    baseline = {
        symbol: [
            {"time": 60 * index, "close": float(index)}
            for index in range(1, REQUIRED_COMPLETED_M1_BARS + 2)
        ]
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    loop._merge_m1_bar_history(
        history,
        baseline,
        limit=REQUIRED_COMPLETED_M1_BARS + 1,
    )
    replaced_row = history["EURUSD"][-1]
    updates = {
        symbol: [
            {"time": 60 * (REQUIRED_COMPLETED_M1_BARS + 1), "close": 500.0},
            {"time": 60 * (REQUIRED_COMPLETED_M1_BARS + 2), "close": 501.0},
        ]
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    parse_calls = 0
    original_parse = loop._parse_bar_epoch

    def _counted_parse(value: Any) -> int | None:
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(value)

    monkeypatch.setattr(loop, "_parse_bar_epoch", _counted_parse)
    merged = loop._merge_m1_bar_history(
        history,
        updates,
        limit=REQUIRED_COMPLETED_M1_BARS + 1,
    )

    assert parse_calls == len(IG_MT4_SCALP_SYMBOLS) * 2
    assert merged["EURUSD"][-2] is not replaced_row
    assert merged["EURUSD"][-2]["close"] == 500.0
    assert merged["EURUSD"][-1]["close"] == 501.0
    updates["EURUSD"][-1]["close"] = 999.0
    assert merged["EURUSD"][-1]["close"] == 501.0


def test_runtime_owned_m1_history_skips_copy_for_unchanged_exact_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = loop._empty_exact_m1_bar_scope()
    baseline = {
        symbol: [
            {"time": 60, "close": 1.0},
            {"time": 120, "close": 2.0},
        ]
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    loop._merge_m1_bar_history(history, baseline, limit=2)
    retained_rows = tuple(history[symbol] for symbol in IG_MT4_SCALP_SYMBOLS)
    updates = {
        symbol: [dict(row) for row in baseline[symbol]]
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    copy_calls = 0
    cache_row = loop.cache_mtvclc_bar_row

    def counted_cache(row: Any) -> dict[str, Any]:
        nonlocal copy_calls
        copy_calls += 1
        return cache_row(row)

    monkeypatch.setattr(loop, "cache_mtvclc_bar_row", counted_cache)
    merged = loop._merge_m1_bar_history(history, updates, limit=2)

    assert copy_calls == 0
    assert all(
        merged[symbol] is retained_rows[index]
        for index, symbol in enumerate(IG_MT4_SCALP_SYMBOLS)
    )
    updates["EURUSD"][-1]["close"] = 999.0
    assert merged["EURUSD"][-1]["close"] == 2.0


def test_runtime_owned_m1_history_duplicate_tail_keeps_last_changed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = loop._empty_exact_m1_bar_scope()
    baseline = {
        symbol: [{"time": 120, "close": 2.0}]
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    loop._merge_m1_bar_history(history, baseline, limit=2)
    replacement = {"time": 120, "close": 3.0}
    updates = {
        "EURUSD": [dict(baseline["EURUSD"][0]), replacement],
    }
    copy_calls = 0
    cache_row = loop.cache_mtvclc_bar_row

    def counted_cache(row: Any) -> dict[str, Any]:
        nonlocal copy_calls
        copy_calls += 1
        return cache_row(row)

    monkeypatch.setattr(loop, "cache_mtvclc_bar_row", counted_cache)
    merged = loop._merge_m1_bar_history(history, updates, limit=2)

    assert copy_calls == 1
    assert merged["EURUSD"][-1]["close"] == 3.0
    replacement["close"] = 999.0
    assert merged["EURUSD"][-1]["close"] == 3.0


def test_runtime_owned_m1_history_custom_tail_retains_copy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CustomRow(dict[str, Any]):
        pass

    history = loop._empty_exact_m1_bar_scope()
    baseline = {
        symbol: [{"time": 120, "close": 2.0}]
        for symbol in IG_MT4_SCALP_SYMBOLS
    }
    loop._merge_m1_bar_history(history, baseline, limit=2)
    retained_rows = history["EURUSD"]
    copy_calls = 0
    cache_row = loop.cache_mtvclc_bar_row

    def counted_cache(row: Any) -> dict[str, Any]:
        nonlocal copy_calls
        copy_calls += 1
        return cache_row(row)

    monkeypatch.setattr(loop, "cache_mtvclc_bar_row", counted_cache)
    merged = loop._merge_m1_bar_history(
        history,
        {"EURUSD": [CustomRow(baseline["EURUSD"][0])]},
        limit=2,
    )

    assert copy_calls == 1
    assert merged["EURUSD"] is retained_rows


@pytest.mark.parametrize("live", [True, False], ids=["live", "shadow"])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_exact_scope_demo_cycle_composes_immediate_trade_and_persistence(
    monkeypatch: pytest.MonkeyPatch,
    live: bool,
    side: str,
) -> None:
    service = _CycleService()
    context = _install_cycle_fakes(
        monkeypatch,
        service=service,
        live=live,
        side=side,
    )

    result, revoked_latch = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=NOW,
    )

    assert revoked_latch is False
    assert result.proposal_count == 1
    assert result.qualified_count == 1
    assert result.selected_count == 1
    assert result.diagnostics["entry_strategy_family"] == "mtvclc"
    assert result.diagnostics["configured_symbols"] == list(IG_MT4_SCALP_SYMBOLS)
    assert result.diagnostics["entry_global_reasons"] == []
    assert result.diagnostics["validation"]["schema_version"] == (
        "fxstack.runtime.scalp_admission_diagnostic.v1"
    )
    assert result.diagnostics["validation"]["verification"][
        "runtime_release_certificate_sha256"
    ] == RUNTIME_RELEASE_CERTIFICATE_SHA256
    assert "cost_calibration_row_sha256" not in (
        result.diagnostics["validation"]["verification"]
    )
    assert service.raw_submissions == []
    assert len(service.approved_submissions) == int(live)
    assert result.entry_submit_count == int(live)
    assert result.entry_accept_count == int(live)
    assert len(context.risk_calls) == 1
    refreshed = context.risk_calls[0]["candidate"]
    bid, ask = _prices("EURUSD")
    assert refreshed.refreshed_entry_price == pytest.approx(
        ask if side == "BUY" else bid
    )
    broker_plan = context.risk_calls[0]["broker_entry_plan"]
    assert broker_plan.execution_type == "market"
    assert broker_plan.pending_orders_forbidden is True
    assert broker_plan.side == side

    assert len(service.patches) == 1
    assert service.cycle_commits == 1
    assert service.runtime_diag_patch_calls == 1
    # One initial market-source state read is always required. Live additionally
    # refreshes after authority activation and immediately before submission;
    # the former final pre-write read is intentionally gone.
    assert service.state_reads == (3 if live else 1)
    assert service.generic_command_reads == 0
    assert service.reconciliation_command_reads == [False]
    patch = service.patches[0]
    assert patch["configured_pairs"] == list(IG_MT4_SCALP_SYMBOLS)
    assert patch["runtime_last_cycle_ts"] == pytest.approx(NOW)
    assert patch["governance"] == result.diagnostics["governance"]
    assert patch["scalp_account_conversion_ready"] is True
    assert patch["scalp_account_conversion_errors"] == []
    assert "production_scalp" not in patch["runtime_diag"]
    assert patch["agent_decisions"] == []
    assert patch["agent_diagnostics"] == {}
    assert patch["vol"] == 0.0

    assert len(service.decision_writes) == 1
    decision_write = service.decision_writes[0]
    assert len(decision_write["decisions"]) == len(IG_MT4_SCALP_SYMBOLS)
    assert [row["symbol"] for row in decision_write["decisions"]] == list(
        IG_MT4_SCALP_SYMBOLS
    )
    selected_metadata = decision_write["decisions"][0]["metadata"]
    assert {
        "proposal",
        "qualification",
        "quote_refresh",
        "broker_entry_plan",
        "risk",
    }.issubset(selected_metadata)
    assert ("enqueue" in selected_metadata) is (not live)
    abstention_metadata = decision_write["decisions"][1]["metadata"]
    assert not {
        "proposal",
        "qualification",
        "quote_refresh",
        "broker_entry_plan",
        "risk",
        "enqueue",
    }.intersection(abstention_metadata)
    assert "evaluation_side" not in abstention_metadata[
        "proposal_batch_symbol_diagnostic"
    ]
    assert decision_write["diagnostics"]["production_scalp"] == result.diagnostics
    if live:
        submission = service.approved_submissions[0]
        payload = submission["payload"]
        assert submission["proto"] == "v2"
        assert payload["symbol"] == "EURUSD"
        assert payload["cmd"] == side
        assert payload["execution_type"] == "market"
        assert payload["pending_orders_forbidden"] is True
        assert payload["entry_deadline_epoch"] == ENTRY_DEADLINE_EPOCH
        assert submission["approval"].broker_account_mode == "demo"
        assert submission["approval"].canonical_ready is True
    else:
        assert result.diagnostics["entry_outcomes"] == [
            {
                "symbol": "EURUSD",
                "status": "shadow_preview",
                "accepted": False,
                "command_id": "mtvclc-entry-eurusd-1",
            }
        ]


def test_retired_demo_probe_fields_cannot_synthesize_an_entry_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CycleService()
    _install_cycle_fakes(monkeypatch, service=service, live=True)
    settings = _cycle_settings()
    settings.production_scalp_demo_probe_id = "legacy-probe"
    settings.production_scalp_demo_probe_symbol = "EURUSD"
    settings.production_scalp_demo_probe_side = "SELL"
    monkeypatch.setattr(
        loop,
        "_qualified_batch",
        lambda observed, **_kwargs: (replace(observed, proposals=()), {}, {}),
    )

    result, entry_revoked_latch = loop.execute_production_scalp_cycle(
        service=service,
        settings=settings,
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="f" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=NOW,
    )

    assert entry_revoked_latch is False
    assert result.entry_accept_count == 0
    assert service.approved_submissions == []
    assert "demo_execution_probe" not in result.diagnostics


def test_cycle_uses_post_fetch_clock_for_snapshot_and_live_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CycleService()
    _install_cycle_fakes(monkeypatch, service=service, live=True)
    monkeypatch.setattr(
        loop,
        "_signal_bar_receipt_missing_symbols",
        lambda *_args, **_kwargs: (),
    )
    service.state["symbol_specs_ts"] = NOW + 1.0
    clock_samples = iter((NOW, NOW + 1.0, NOW + 2.0))
    monkeypatch.setattr(loop.time, "time", lambda: next(clock_samples))
    fetch_ticks = loop.fetch_market_ticks

    def _ticks_received_after_cycle_start(
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, dict[str, Any]]:
        ticks = fetch_ticks(*args, **kwargs)
        for tick in ticks.values():
            tick["received_at_epoch"] = NOW + 1.0
            tick["market_event_received_at_epoch"] = NOW + 1.0
        return ticks

    monkeypatch.setattr(loop, "fetch_market_ticks", _ticks_received_after_cycle_start)
    reconciliation_now: list[float] = []

    def _reconcile(**kwargs: Any) -> _Record:
        reconciliation_now.append(float(kwargs["now_epoch"]))
        return _Record(
            owned_positions=(),
            authoritative_open_symbols=(),
            active_queued_entry_symbols=(),
            broker_confirmed_exit_symbols=(),
            active_exit_tickets=(),
            entry_admission_ready=True,
            quarantine_reasons=(),
        )

    monkeypatch.setattr(loop, "reconcile_scalp_restart", _reconcile)

    result, revoked_latch = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
    )

    assert revoked_latch is False
    assert reconciliation_now == [NOW + 2.0]
    assert result.diagnostics["entry_global_reasons"] == []
    quote = result.diagnostics["quote_refresh"]["EURUSD"]
    assert quote["reasons"] == ()
    assert result.entry_accept_count == 1
    assert quote["accepted"] is True
    assert quote["as_of_epoch"] == pytest.approx(NOW + 2.0)
    assert quote["quote_timestamp_epoch"] == pytest.approx(NOW + 1.0)
    assert "scalp_entry_quote_tick_from_future" not in quote["reasons"]
    assert result.diagnostics["broker_contracts"]["ok"] is True
    assert "broker_contract_specs_timestamp_future" not in result.diagnostics[
        "broker_contracts"
    ]["errors"]
    assert service.patches[0]["runtime_last_cycle_ts"] == pytest.approx(NOW)


def test_stale_unrelated_conversion_does_not_block_ready_symbol_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CycleService()
    context = _install_cycle_fakes(monkeypatch, service=service, live=True)
    fetch_ticks = loop.fetch_market_ticks

    def _stale_conversion_ticks(*args: Any, **kwargs: Any) -> dict[str, dict[str, Any]]:
        ticks = fetch_ticks(*args, **kwargs)
        ticks["USDJPY"].update(
            market_event_fresh=False,
            market_event_reason="broker_market_event_stale",
        )
        return ticks

    monkeypatch.setattr(loop, "fetch_market_ticks", _stale_conversion_ticks)

    result, revoked_latch = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=NOW,
    )

    assert revoked_latch is False
    assert result.diagnostics["entry_global_reasons"] == []
    assert result.entry_accept_count == 1
    assert len(context.risk_calls) == 1
    assert result.diagnostics["account_conversion"]["ok"] is False
    assert "scalp_account_conversion_tick_not_fresh:JPY" in result.diagnostics[
        "account_conversion"
    ]["errors"]
    readiness = {
        item["symbol"]: item
        for item in result.diagnostics["symbol_execution_readiness"]
    }
    assert readiness["EURUSD"]["execution_ready"] is True
    assert readiness["EURUSD"]["account_conversion_ready"] is True
    assert readiness["EURJPY"]["execution_ready"] is False
    assert readiness["EURJPY"]["account_conversion_ready"] is False
    assert result.diagnostics["any_pair_execution_ready"] is True
    assert result.diagnostics["all_pairs_execution_ready"] is False
    eurjpy_readiness = readiness["EURJPY"]
    assert eurjpy_readiness["account_conversion_quote_currency"] == "JPY"
    assert "scalp_account_conversion_tick_not_fresh:JPY" in (
        eurjpy_readiness["account_conversion_errors"]
    )
    assert "account_conversion" not in eurjpy_readiness


def test_selected_stale_quote_is_a_symbol_scoped_entry_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CycleService()
    context = _install_cycle_fakes(monkeypatch, service=service, live=True)
    fetch_ticks = loop.fetch_market_ticks

    def _stale_selected_tick(*args: Any, **kwargs: Any) -> dict[str, dict[str, Any]]:
        ticks = fetch_ticks(*args, **kwargs)
        ticks["EURUSD"].update(
            market_event_fresh=False,
            market_event_reason="broker_market_event_stale",
        )
        return ticks

    monkeypatch.setattr(loop, "fetch_market_ticks", _stale_selected_tick)

    result, _ = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=NOW,
    )

    assert result.diagnostics["entry_global_reasons"] == []
    assert result.entry_accept_count == 0
    assert context.risk_calls == []
    readiness = {
        item["symbol"]: item
        for item in result.diagnostics["symbol_execution_readiness"]
    }
    assert readiness["EURUSD"]["market_tick_ready"] is False
    assert readiness["EURUSD"]["execution_ready"] is False
    assert readiness["AUDUSD"]["execution_ready"] is True


def test_fx_closed_crypto_ready_still_ranks_and_enters_crypto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CycleService()
    context = _install_cycle_fakes(monkeypatch, service=service, live=True)
    crypto_symbols = set(IG_MT4_CRYPTO_CFD_SYMBOLS)
    _select_candidate(
        context,
        symbol="BTCUSD",
        structural_ready_symbols=crypto_symbols,
    )
    fetch_ticks = loop.fetch_market_ticks

    def _closed_fx_ticks(*args: Any, **kwargs: Any) -> dict[str, dict[str, Any]]:
        ticks = fetch_ticks(*args, **kwargs)
        for symbol, tick in ticks.items():
            if symbol not in crypto_symbols:
                tick.update(
                    market_event_fresh=False,
                    market_event_reason="broker_market_event_stale",
                )
        return ticks

    monkeypatch.setattr(loop, "fetch_market_ticks", _closed_fx_ticks)

    result, revoked_latch = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=NOW,
    )

    assert revoked_latch is False
    assert result.diagnostics["entry_global_reasons"] == []
    assert result.entry_accept_count == 1
    assert service.approved_submissions[0]["payload"]["symbol"] == "BTCUSD"
    assert len(context.risk_calls) == 1
    assert tuple(context.risk_calls[0]["contract_universe"].contracts) == (
        "BTCUSD",
    )
    readiness_rows = result.diagnostics["symbol_execution_readiness"]
    assert [item["symbol"] for item in readiness_rows] == list(IG_MT4_SCALP_SYMBOLS)
    readiness = {item["symbol"]: item for item in readiness_rows}
    assert {
        symbol for symbol, item in readiness.items() if item["execution_ready"]
    } == crypto_symbols
    assert result.diagnostics["account_conversion"]["ok"] is False
    assert result.diagnostics["any_pair_account_conversion_ready"] is True
    assert result.diagnostics["all_pairs_account_conversion_ready"] is False
    assert result.diagnostics["any_pair_execution_ready"] is True
    assert result.diagnostics["all_pairs_execution_ready"] is False


def test_one_malformed_unselected_contract_does_not_block_ready_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CycleService()
    service.state["symbol_specs"]["NZDJPY"]["lot_size"] = 0.0
    _install_cycle_fakes(monkeypatch, service=service, live=True)

    result, _ = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=NOW,
    )

    assert result.diagnostics["entry_global_reasons"] == []
    assert result.entry_accept_count == 1
    assert result.diagnostics["broker_contracts"]["ok"] is False
    assert "broker_contract_lot_size_invalid:NZDJPY" in result.diagnostics[
        "broker_contracts"
    ]["errors"]
    readiness = {
        item["symbol"]: item
        for item in result.diagnostics["symbol_execution_readiness"]
    }
    assert readiness["EURUSD"]["broker_contract_ready"] is True
    assert readiness["EURUSD"]["execution_ready"] is True
    assert readiness["NZDJPY"]["broker_contract_ready"] is False
    assert readiness["NZDJPY"]["execution_ready"] is False
    assert result.diagnostics["any_pair_broker_contract_ready"] is True
    assert result.diagnostics["all_pairs_broker_contract_ready"] is False


def test_one_closed_unselected_contract_remains_symbol_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CycleService()
    service.state["symbol_specs"]["BTCUSD"]["trade_allowed"] = False
    _install_cycle_fakes(monkeypatch, service=service, live=True)

    result, _ = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=NOW,
    )

    assert result.diagnostics["entry_global_reasons"] == []
    assert result.entry_accept_count == 1
    assert result.diagnostics["any_pair_execution_ready"] is True
    assert result.diagnostics["all_pairs_execution_ready"] is False
    readiness = {
        item["symbol"]: item
        for item in result.diagnostics["symbol_execution_readiness"]
    }
    assert readiness["EURUSD"]["execution_ready"] is True
    assert readiness["BTCUSD"]["broker_contract_ready"] is False
    assert readiness["BTCUSD"]["execution_ready"] is False
    assert readiness["BTCUSD"]["broker_contract_errors"] == [
        "broker_contract_trade_not_allowed:BTCUSD"
    ]


def test_symbol_contract_split_matches_strict_selected_projections() -> None:
    state = deepcopy(_CycleService().state)
    state["broker_account_currency"] = ""
    state["symbol_specs"]["BTCUSD"]["trade_allowed"] = False
    state["symbol_specs"]["NZDJPY"]["lot_size"] = 0.0
    aggregate = loop.project_ig_mt4_contract_universe(
        state,
        now_ts=NOW,
        max_age_secs=120.0,
    )

    projections = loop._symbol_broker_contract_projections(aggregate)

    assert tuple(projections) == IG_MT4_SCALP_SYMBOLS
    for symbol in IG_MT4_SCALP_SYMBOLS:
        assert projections[symbol] == project_ig_mt4_selected_contract_universe(
            state,
            selected_symbols=(symbol,),
            now_ts=NOW,
            max_age_secs=120.0,
        )


@pytest.mark.parametrize(
    ("mutation", "expected_global_reason"),
    [
        (
            lambda state: state.update(broker_account_currency=""),
            "broker_contract_account_currency_unattested",
        ),
        (
            lambda state: state.update(broker_account_scope=""),
            "scalp_broker_account_scope_unattested",
        ),
        (
            lambda state: state.update(broker_venue_id=""),
            "scalp_broker_venue_unattested",
        ),
        (
            lambda state: state.update(bridge_producer_instance_id=""),
            "scalp_market_source_heartbeat_identity_missing",
        ),
        (
            lambda state: state.update(broker_account_mode="real"),
            "scalp_broker_account_mode_mismatch",
        ),
    ],
)
def test_missing_account_venue_source_or_configured_mode_is_a_global_entry_gate(
    monkeypatch: pytest.MonkeyPatch,
    mutation,
    expected_global_reason: str,
) -> None:
    service = _CycleService()
    mutation(service.state)
    _install_cycle_fakes(monkeypatch, service=service, live=True)
    monkeypatch.setattr(
        loop,
        "_risk_entry",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("risk_reached_despite_global_identity_failure")
        ),
    )

    result, _ = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=NOW,
    )

    assert expected_global_reason in result.diagnostics["entry_global_reasons"]
    assert result.diagnostics["any_pair_execution_ready"] is False
    assert result.entry_accept_count == 0


def test_midrun_runtime_release_revocation_narrows_once_to_protective_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CycleService()
    context = _install_cycle_fakes(monkeypatch, service=service, live=True)
    invalid_admission = replace(
        context.admission,
        valid=False,
        reason="validation_revoked",
        errors=("validation_revoked",),
        verification=_verification(
            context.admission.cost_calibrations,
            valid=False,
        ),
    )
    monkeypatch.setattr(
        loop,
        "verify_configured_scalp_runtime_admission",
        lambda *_args, **_kwargs: invalid_admission,
    )
    monkeypatch.setattr(
        loop,
        "scalp_live_command_admission",
        lambda **_kwargs: {
            "allowed": False,
            "blockers": ["validation_revoked"],
        },
    )
    activations: list[str] = []

    def _protective(**kwargs: Any) -> _Record:
        activations.append(str(kwargs["runtime_boot_id"]))
        service.state["production_scalp_authority"]["status"] = "revoked"
        service.state["execution_egress_enabled"] = True
        service.state["execution_egress_authority"] = {
            "enabled": True,
            "protective_management_only": True,
            "runtime_boot_id": str(kwargs["runtime_boot_id"]),
            "pair_scope": list(IG_MT4_SCALP_SYMBOLS),
            "sleeve_scope": ["scalp"],
            "intent_scope": ["exit"],
        }
        service.state["runtime_diag"]["orchestration_live"].update(
            {
                "enabled": True,
                "mode": "live",
                "runtime_enabled": True,
                "queue_kill_active": False,
            }
        )
        return _Record(
            active=True,
            reason="protective_management_only",
            errors=(),
        )

    monkeypatch.setattr(
        loop,
        "ensure_production_scalp_protective_management_egress",
        _protective,
    )

    first, latch = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=False,
        now_epoch=NOW,
    )
    second, latch = loop.execute_production_scalp_cycle(
        service=service,
        settings=_cycle_settings(),
        runtime_boot_id="scalp-runtime-boot",
        runtime_config_hash="e" * 64,
        equity_seed=10_000.0,
        entry_revoked_latch=latch,
        now_epoch=NOW + 1.0,
    )

    assert latch is True
    assert activations == ["scalp-runtime-boot"]
    assert service.approved_submissions == []
    assert first.diagnostics["protective_management_activation"]["active"] is True
    assert second.diagnostics["protective_management_activation"]["reason"] == (
        "already_active"
    )
    assert "validation_revoked" in first.diagnostics["entry_global_reasons"]
