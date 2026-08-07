from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MQL4" / "Experts" / "BridgeEA.mq4"
HTTP_PATH = ROOT / "MQL4" / "Include" / "BridgeHttp.mqh"
OPERATOR_EXECUTION_SURFACES = (
    ROOT / "docs" / "agents" / "dashboard-dataflow.md",
    ROOT / "docs" / "agents" / "runtime-loop.md",
    ROOT / "docs" / "STRATEGY_DECISION_DAG.md",
    ROOT / "components" / "command-lifecycle-timeline.tsx",
    ROOT / "components" / "live-signals.tsx",
)


def _source() -> str:
    return EA_PATH.read_text(encoding="utf-8")


def _http_source() -> str:
    return HTTP_PATH.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def test_production_scalper_requires_complete_instant_market_wire_envelope() -> None:
    source = _source()
    handler = _between(source, "void HandleCmd", "void UpdateDashboard")
    validator = _between(
        source,
        "bool ValidateExactMarketEntryEnvelope",
        "bool ValidateScalpMarketEntryEnvelope",
    )

    required_wire_keys = (
        "execution_type",
        "pending_orders_forbidden",
        "entry_deadline_epoch",
        "broker_entry_plan_schema",
        "entry_quote_price",
        "entry_price",
        "worst_fill_price",
        "max_slippage_points",
        "protection_cushion_points",
        "expected_strategy_authority_schema",
        "expected_strategy_admission_mode",
        "expected_strategy_account_mode",
        "expected_strategy_generation_id",
        "expected_strategy_id",
        "expected_strategy_version",
        "expected_strategy_engine_sha256",
        "expected_strategy_config_id",
        "expected_strategy_config_sha256",
        "expected_strategy_runtime_release_certificate_sha256",
        "expected_strategy_runtime_release_signing_key_id",
        "expected_strategy_research_evidence_sha256",
        "expected_strategy_research_evidence_signing_key_id",
        "expected_strategy_registry_generation_id",
        "expected_strategy_registry_revision",
        "expected_strategy_registry_sha256",
        "expected_strategy_qualification_surface_sha256",
        "expected_strategy_cost_mapping_sha256",
        "expected_strategy_execution_contract_sha256",
        "expected_strategy_validation_expires_at_epoch",
        "expected_strategy_venue_id",
        "expected_strategy_scope_version",
        "expected_strategy_binding_sha256",
        "expected_strategy_runtime_boot_id",
        "expected_strategy_authority_revision",
        "expected_broker_contract_state_schema",
        "expected_broker_contract_venue_id",
        "expected_broker_contract_symbol",
        "expected_broker_contract_broker_symbol",
        "expected_broker_contract_account_currency",
        "expected_broker_contract_binding_sha256",
        "expected_broker_contract_lot_size",
        "expected_broker_contract_min_lot",
        "expected_broker_contract_lot_step",
        "expected_broker_contract_max_lot",
        "expected_broker_contract_point",
        "expected_broker_contract_tick_size",
        "expected_broker_contract_margin_required",
        "expected_broker_contract_stop_level_points",
        "expected_broker_contract_freeze_level_points",
        "expected_broker_contract_digits",
        "expected_broker_contract_trade_allowed",
    )
    for key in required_wire_keys:
        assert f'k=="{key}"' in handler

    assert "intent==PRODUCTION_SCALPER_ENTRY_INTENT" in handler
    assert "intent = ToUpperSafe(StringTrim(intent))" in handler
    assert '#define PRODUCTION_SCALPER_ENTRY_INTENT "PRODUCTION_SCALPER_ENTRY"' in source
    assert "ValidateScalpMarketEntryEnvelope(" in handler
    assert 'ToUpperSafe(envelope.execution_type)!="MARKET"' in validator
    assert "!envelope.pending_orders_forbidden" in validator
    assert "ScalpEntryDeadlineActive(envelope,deadlineReason)" in _compact(
        validator
    )
    assert "SCALP_BROKER_ENTRY_PLAN_SCHEMA" in validator
    assert "BROKER_CONTRACT_STATE_SCHEMA" in validator
    assert "IsSha256Hex(envelope.binding_sha256)" in validator
    assert "scalp_market_envelope_incomplete" in validator
    assert "PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS" in validator


def test_scalp_entry_is_market_only_and_never_builds_pending_order_types() -> None:
    source = _source()
    execute = _between(source, "void Execute", "void manageCycle")

    side_guard = 'if(cmd!="BUY" && cmd!="SELL")'
    assert side_guard in execute
    assert execute.index(side_guard) < execute.index(
        'int type=(cmd=="BUY")?OP_BUY:OP_SELL;'
    )
    assert "immediate_market_trade_side_invalid" in execute
    assert 'int type=(cmd=="BUY")?OP_BUY:OP_SELL;' in execute
    assert "OrderSend(brokerSym, type" in execute
    for pending_type in ("OP_BUYLIMIT", "OP_BUYSTOP", "OP_SELLLIMIT", "OP_SELLSTOP"):
        assert pending_type not in source
    assert "execution_type=market" not in execute  # parsed data, never a pending branch


def test_production_entry_is_signed_only_and_uses_immediate_trade_wording() -> None:
    source = _source()
    handler = _between(source, "void HandleCmd", "void UpdateDashboard")
    execute = _between(source, "void Execute", "void manageCycle")
    validator = _between(
        source,
        "bool ValidateScalpStrategyAuthorityEnvelope",
        "bool ContractNumberMatches",
    )

    assert "ValidateScalpStrategyAuthorityEnvelope(" in handler
    assert 'if(admissionMode!="SIGNED_VALIDATION")' in validator
    assert '"DIRECT_DEMO"' not in validator
    assert '"DIRECT_DEMO"' not in source
    assert "string strategyAdmissionMode," in execute
    signed_guard = (
        'ToUpperSafe(StringTrim(strategyAdmissionMode)) != "SIGNED_VALIDATION"'
    )
    assert signed_guard in execute
    assert execute.index(signed_guard) < execute.index("ResolveBrokerSymbolStatus(")
    assert execute.index(signed_guard) < execute.index("OrderSend(")
    assert "Executing immediate market " in execute
    assert " trade " in execute
    assert "Submitting " not in execute


def test_production_entry_requires_complete_signed_v3_authority_at_mt4() -> None:
    source = _source()
    handler = _between(source, "void HandleCmd", "void UpdateDashboard")
    validator = _between(
        source,
        "bool ValidateScalpStrategyAuthorityEnvelope",
        "bool ContractNumberMatches",
    )

    assert (
        '#define PRODUCTION_SCALP_AUTHORITY_SCHEMA '
        '"fxstack_production_scalp_authority_v3"'
    ) in source
    assert (
        '#define PRODUCTION_SCALP_SCOPE_VERSION '
        '"fxstack.ig_mt4.scalp_scope.v3"'
    ) in source
    assert '#define MTVCLC_STRATEGY_VERSION "mtvclc.v1"' in source
    assert "ValidateScalpStrategyAuthorityEnvelope(" in handler
    assert handler.index("ValidateScalpStrategyAuthorityEnvelope(") < handler.index(
        "ValidateScalpMarketEntryEnvelope("
    )
    assert "authority.schema!=PRODUCTION_SCALP_AUTHORITY_SCHEMA" in validator
    assert 'if(admissionMode!="SIGNED_VALIDATION")' in validator
    assert 'accountMode!="DEMO" && accountMode!="REAL"' in validator
    assert "strategy_account_mode_invalid" in validator
    assert "strategy_demo_account_required" not in validator
    assert "authority.strategy_id!=MTVCLC_STRATEGY_ID" in validator
    assert "authority.strategy_version!=MTVCLC_STRATEGY_VERSION" in validator
    assert "authority.config_id!=MTVCLC_CONFIG_ID" in validator
    assert "authority.registry_generation_id!=authority.generation_id" in validator
    assert "authority.registry_revision_provided" in validator
    assert "authority.authority_revision_provided" in validator
    assert "authority.validation_expires_at_epoch_provided" in validator
    assert "now>=authority.validation_expires_at_epoch" in validator
    for field in (
        "engine_sha256",
        "config_sha256",
        "runtime_release_certificate_sha256",
        "runtime_release_signing_key_id",
        "research_evidence_sha256",
        "research_evidence_signing_key_id",
        "registry_sha256",
        "qualification_surface_sha256",
        "cost_mapping_sha256",
        "execution_contract_sha256",
        "binding_sha256",
    ):
        assert f"IsSha256Hex(authority.{field})" in validator


def test_dashboard_is_a_noop_until_text_or_chart_dimensions_change() -> None:
    source = _source()
    dashboard = _between(source, "void UpdateDashboard", "bool ValidateDirectional")
    on_init = _between(source, "int OnInit", "void RemoveDashboard")

    cache_guard = "text == gDashboardLastText"
    assert cache_guard in dashboard
    assert "chartWidth == gDashboardLastChartWidth" in dashboard
    assert "chartHeight == gDashboardLastChartHeight" in dashboard
    assert dashboard.index(cache_guard) < dashboard.index("StringSplit(")
    assert "gDashboardRowsShown < 0 ? 60 : gDashboardRowsShown" in dashboard
    assert dashboard.count("ChartRedraw(0);") == 1
    assert "ChartRedraw(0);" not in on_init


def test_bridge_http_has_bounded_timeouts_response_size_and_handle_cleanup() -> None:
    source = _http_source()

    for option in (
        "INTERNET_OPTION_CONNECT_TIMEOUT",
        "INTERNET_OPTION_SEND_TIMEOUT",
        "INTERNET_OPTION_RECEIVE_TIMEOUT",
    ):
        assert option in source
    for timeout in (
        "BRIDGE_HTTP_CONNECT_TIMEOUT_MS 250",
        "BRIDGE_HTTP_SEND_TIMEOUT_MS 750",
        "BRIDGE_HTTP_RECEIVE_TIMEOUT_MS 750",
        "BRIDGE_HTTP_WEBREQUEST_TIMEOUT_MS 1500",
    ):
        assert timeout in source
    assert "InternetSetOptionW(" in source
    assert "ConfigureBridgeHttpTimeouts(gSession)" in source
    assert "BRIDGE_HTTP_MAX_RESPONSE_BYTES 4194304" in source
    assert source.count("BRIDGE_HTTP_MAX_RESPONSE_BYTES") >= 4
    deinit = _between(source, "void DeinitBridgeHttp", "void HttpPOST")
    assert "InternetCloseHandle(gSession)" in deinit
    assert "InternetCloseHandle(hRequest);" in source
    assert "InternetCloseHandle(hConnect);" in source


def test_live_contract_and_execution_permissions_gate_every_scalp_send() -> None:
    source = _source()
    live_contract = _between(
        source,
        "bool ValidateLiveBrokerContract",
        "bool IsRetryableEntryError",
    )
    pre_send = _between(
        source,
        "bool ValidateMarketEntryPreSend",
        "void AppendAttestationReason",
    )
    execute = _between(source, "void Execute", "void manageCycle")
    retry_loop = _between(
        execute,
        "for(int attempt=0; attempt<ENTRY_SEND_ATTEMPTS; attempt++)",
        "if(ticket<0)",
    )

    assert "brokerSym!=envelope.broker_symbol" in live_contract
    for broker_field in (
        "MODE_LOTSIZE",
        "MODE_MINLOT",
        "MODE_LOTSTEP",
        "MODE_MAXLOT",
        "MODE_POINT",
        "MODE_DIGITS",
        "SYMBOL_TRADE_TICK_SIZE",
        "MODE_STOPLEVEL",
        "MODE_FREEZELEVEL",
        "MODE_MARGINREQUIRED",
        "MODE_TRADEALLOWED",
    ):
        assert broker_field in live_contract
    assert "ValidateLiveBrokerContract(" in pre_send
    assert "IsTradeAllowed()" in pre_send
    assert "IsTradeContextBusy()" in pre_send
    assert "!gSignalOutcomeJournalReady || gSignalOutcomeJournalBlocked" in pre_send
    assert "signal_outcome_journal_unavailable" in pre_send
    assert "AccountFreeMarginCheck(" in pre_send
    assert "QuantizeAndValidateBrokerPrice(" in pre_send
    assert "buy_quote_beyond_worst_fill" in pre_send
    assert "sell_quote_beyond_worst_fill" in pre_send
    assert retry_loop.index("ValidateMarketEntryPreSend(") < retry_loop.index(
        "OrderSend("
    )


def test_entry_deadline_is_rechecked_on_every_retry_immediately_before_send() -> None:
    source = _source()
    deadline_guard = _between(
        source,
        "bool ScalpEntryDeadlineActive",
        "bool ValidateScalpMarketEntryEnvelope",
    )
    execute = _between(source, "void Execute", "void manageCycle")
    retry_loop = _between(
        execute,
        "for(int attempt=0; attempt<ENTRY_SEND_ATTEMPTS; attempt++)",
        "if(ticket<0)",
    )

    assert "TimeGMT()" in deadline_guard
    assert "TimeLocal()" in deadline_guard
    assert ">=envelope.entry_deadline_epoch" in _compact(deadline_guard)
    assert retry_loop.count("ScalpEntryDeadlineActive(") == 1
    assert retry_loop.index("ScalpEntryDeadlineActive(") < retry_loop.index(
        "OrderSend("
    )
    between_guard_and_send = retry_loop.split(
        "ScalpEntryDeadlineActive(", 1
    )[1].split("OrderSend(", 1)[0]
    assert "Sleep(" not in between_guard_and_send
    assert "RefreshRates(" not in between_guard_and_send


def test_scalp_slippage_is_command_bounded_and_never_widens_on_retry() -> None:
    source = _source()
    pre_send = _between(
        source,
        "bool ValidateMarketEntryPreSend",
        "void AppendAttestationReason",
    )
    execute = _between(source, "void Execute", "void manageCycle")
    retry_loop = _between(
        execute,
        "for(int attempt=0; attempt<ENTRY_SEND_ATTEMPTS; attempt++)",
        "if(ticket<0)",
    )

    assert "(worstExact-ask)/envelope.point" in pre_send
    assert "(bid-worstExact)/envelope.point" in pre_send
    assert "allowedSlippagePoints=envelope.max_slippage_points" in _compact(pre_send)
    assert '#define PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS 20' in source
    assert (
        "envelope.max_slippage_points!=PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS"
        in _compact(source)
    )
    assert "attempt * 10" not in source
    assert "SlipPts +" not in source
    assert "usedSlip = SlipPts" not in retry_loop
    assert "OrderSend(brokerSym, type, lots2, px, usedSlip" in retry_loop
    retryable = _between(
        source,
        "bool IsRetryableEntryError",
        "bool ValidateMarketEntryPreSend",
    )
    assert "errorCode==128" not in retryable
    assert "!IsRetryableEntryError(err)" in retry_loop


def test_positive_ticket_requires_orderselect_and_actual_fill_attestation() -> None:
    source = _source()
    selector = _between(
        source,
        "bool SelectSubmittedTicketForAttestation",
        "bool AttestSubmittedMarketTicket",
    )
    attestor = _between(
        source,
        "bool AttestSubmittedMarketTicket",
        "void Execute",
    )
    execute = _between(source, "void Execute", "void manageCycle")
    post_ack = _between(source, "void post_ack", "int ReplayDurableSignalOutcome")

    assert "OrderSelect(ticket,SELECT_BY_TICKET,MODE_TRADES)" in selector
    for actual in (
        "OrderSymbol()",
        "OrderType()",
        "OrderLots()",
        "OrderOpenPrice()",
        "OrderStopLoss()",
        "OrderTakeProfit()",
        "OrderMagicNumber()",
        "OrderComment()",
    ):
        assert actual in attestor
    assert "buy_fill_exceeds_worst_fill" in attestor
    assert "sell_fill_exceeds_worst_fill" in attestor
    assert "HasOwnerTokenPrefix(actualOrderComment,expectedOwnerToken)" in attestor
    assert 'string ackStatus=attested ? "acked" : "reconcile_required";' in execute
    assert 'string ackMutationState=attested ? "confirmed" : "attempted";' in execute
    assert '\\"actual_broker_symbol\\"' in post_ack
    assert '\\"actual_open_price\\"' in post_ack
    assert '\\"actual_order_comment\\"' in post_ack


def test_every_confirmed_entry_ack_stamps_versioned_broker_actuals_envelope() -> None:
    source = _source()
    execute = _between(source, "void Execute", "void manageCycle")
    post_ack = _between(source, "void post_ack", "int ReplayDurableSignalOutcome")

    assert (
        '#define BROKER_ORDER_ACTUALS_SCHEMA "fxstack.mt4_order_actuals.v1"'
        in source
    )
    assert '\\"actuals_schema\\"' in post_ack
    assert 'attested ? BROKER_ORDER_ACTUALS_SCHEMA : ""' in execute
    assert "(productionScalperEntry && attested)" not in execute
    for actual_name in (
        "actual_ticket",
        "actual_cmd",
        "actual_symbol",
        "actual_broker_symbol",
        "actual_side",
        "actual_execution_type",
        "actual_magic",
        "actual_order_comment",
        "actual_lots",
        "actual_remaining_lots",
        "actual_sl_price",
        "actual_tp_price",
        "actual_open_price",
        "actual_close_time",
    ):
        assert f'\\"{actual_name}\\"' in post_ack


def test_model_stack_entry_uses_exact_envelope_pre_send_and_attestation() -> None:
    source = _source()
    handle = _between(source, "void HandleCmd", "bool ResolveTicketOwnerIdentity")
    execute = _between(source, "void Execute", "void manageCycle")

    assert "bool ValidateExactMarketEntryEnvelope" in source
    assert "bool ValidateModelStackMarketEntryEnvelope" in source
    assert "ValidateModelStackMarketEntryEnvelope(" in handle
    assert "ValidateMarketEntryPreSend(" in execute
    assert "AttestSubmittedMarketTicket(" in execute
    assert "legacy_post_send_attestation_mismatch" not in execute
    assert "usedSlip=(int)MathMax(0,SlipPts)" not in execute


def test_duplicate_replays_durable_outcome_and_ambiguous_replay_fails_closed() -> None:
    source = _source()
    handler = _between(source, "void HandleCmd", "void UpdateDashboard")
    journal = _between(
        source,
        "string SignalOutcomePath",
        "bool ProducerInstanceIdValid",
    )
    post_ack = _between(source, "void post_ack", "int ReplayDurableSignalOutcome")
    replay = _between(
        source,
        "int ReplayDurableSignalOutcome",
        "void CleanupSeenSignals",
    )

    assert "PersistSignalOutcomePayload(signal_id,payload)" in post_ack
    journal_code = "\n".join(
        line for line in journal.splitlines() if not line.lstrip().startswith("//")
    )
    assert "FILE_COMMON" not in journal_code
    assert "FileFlush(handle)" in journal
    assert "FileMove(tempPath,0,path,0)" in journal
    assert "QueueAckPayloadBeforePost(payload,queuedPath)" in replay
    assert "ReplayDurableSignalOutcome(signal_id)" in handler
    assert "durable_outcome_ambiguous" in handler
    assert "memory_duplicate_without_durable_outcome" in handler
    assert 'signal_id,"reconcile_required"' in _compact(handler)


def test_terminal_local_outcome_journal_is_proved_before_runtime_startup() -> None:
    source = _source()
    readiness = _between(
        source,
        "bool EnsureSignalOutcomeJournalReady",
        "string SignalOutcomePath",
    )
    signal_path = _between(
        source,
        "string SignalOutcomePath",
        "bool ReadSignalOutcomePayload",
    )
    on_init = _between(source, "int OnInit", "void OnDeinit")

    assert "FolderCreate(SIGNAL_OUTCOME_DIRECTORY,0)" in readiness
    assert "FileOpen(tempPath,FILE_WRITE|FILE_TXT|FILE_ANSI)" in readiness
    assert "FileFlush(handle)" in readiness
    assert "FileMove(tempPath,0,finalPath,0)" in readiness
    assert "FileOpen(finalPath,FILE_READ" in readiness
    assert "FileDelete(finalPath)" in readiness
    assert "gSignalOutcomeJournalReady=true" in readiness
    assert "!gSignalOutcomeJournalReady" in signal_path
    assert "if(!EnsureSignalOutcomeJournalReady())" in on_init
    assert "return(INIT_FAILED)" in on_init
    assert on_init.index("EnsureSignalOutcomeJournalReady()") < on_init.index(
        'InitBridgeHttp("MT4_Bridge_EA")'
    )


def test_ack_guard_never_leaves_failed_or_duplicate_with_positive_ticket() -> None:
    source = _source()
    post_ack = _between(source, "void post_ack", "int ReplayDurableSignalOutcome")
    compact = _compact(post_ack)

    assert 'ticket>0&&(effectiveStatus=="failed"||effectiveStatus=="duplicate")' in compact
    assert 'effectiveStatus="reconcile_required"' in compact
    assert 'effectiveMutationState="attempted"' in compact
    assert '\\"mutation_state\\"' in post_ack
    assert '\\"broker_mutation_attempted\\"' in post_ack


def test_operator_surfaces_describe_immediate_trades_not_order_placement() -> None:
    banned_phrases = (
        "place an order",
        "places orders",
        "placing an order",
        "send order",
        "why no order",
        "market orders are submitted",
    )
    combined = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in OPERATOR_EXECUTION_SURFACES
    )
    for phrase in banned_phrases:
        assert phrase not in combined
    assert "immediate market buy/sell trade" in combined
    assert "buy at ask or sell at bid" in combined

    timeline = (ROOT / "components" / "command-lifecycle-timeline.tsx").read_text(
        encoding="utf-8"
    )
    signals = (ROOT / "components" / "live-signals.tsx").read_text(
        encoding="utf-8"
    )
    for source in (timeline, signals):
        assert '"market_order_attested", "immediate_market_trade_confirmed"' in source
        assert '"order_send", "immediate_trade_execution"' in source
