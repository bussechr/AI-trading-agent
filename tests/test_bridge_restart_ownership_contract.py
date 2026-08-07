from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MQL4" / "Experts" / "BridgeEA.mq4"


def _source() -> str:
    return EA_PATH.read_text(encoding="utf-8")


def _function(source: str, name: str, next_name: str) -> str:
    return source.split(name, 1)[1].split(next_name, 1)[0]


def test_entry_uses_command_owner_comment_and_acks_full_broker_identity() -> None:
    source = _source()
    handle = _function(source, "void HandleCmd", "void UpdateDashboard")
    execute = _function(source, "void Execute", "void manageCycle")
    post_ack = _function(source, "void post_ack", "void CleanupSeenSignals")

    assert 'if(k=="magic") { magic=(int)StrToInteger(v); magic_provided=true; }' in handle
    assert 'if(k=="owner_token") { owner_token=v; owner_token_provided=true; }' in handle
    assert "ownership_contract!=TICKET_OWNER_CONTRACT" in handle
    assert "!magic_provided || magic<=0 || !IsValidOwnerToken(owner_token)" in handle
    assert handle.index("ownership_contract!=TICKET_OWNER_CONTRACT") < handle.index("Execute(")
    assert "OrderSend(brokerSym, type, lots2, px, usedSlip, slNorm, tp, owner_token, magic" in execute
    assert '"ELBridge", magic' not in execute
    assert "interop_mode, magic," in execute
    assert "owner_token,ackMutationState" in execute
    assert r',\"magic\":' + '" + IntegerToString(magic)' in post_ack
    assert r',\"owner_token\":\"' + '" + JsonEscape(owner_token)' in post_ack


def test_strict_management_selects_one_ticket_and_proves_symbol_magic_owner() -> None:
    source = _source()
    select_owned = _function(source, "bool SelectOwnedMarketOrder", "bool CloseOwnedTicket")
    close_owned = _function(
        source,
        "bool CloseOwnedTicket(",
        "bool SelectUniqueOwnedPartialRemainder",
    )
    partial_owned = _function(
        source,
        "bool CloseOwnedTicketPartial",
        "bool ModifyOwnedTicketStop",
    )
    modify_owned = _function(source, "bool ModifyOwnedTicketStop", "// Prove that")

    assert "OrderSelect(target_ticket,SELECT_BY_TICKET,MODE_TRADES)" in select_owned
    assert "!SymbolsMatch(OrderSymbol(),sym)" in select_owned
    assert "OrderMagicNumber()!=target_magic" in select_owned
    assert "!HasOwnerTokenPrefix(OrderComment(),owner_token)" in select_owned
    assert "target_ticket_identity_mismatch" in select_owned
    assert "for(" not in close_owned
    assert "OrderClose(target_ticket,expectedLots" in close_owned
    assert "for(" not in partial_owned
    assert "OrderClose(target_ticket,exactCloseLots" in partial_owned
    assert "for(" not in modify_owned
    assert "double expectedTp=OrderTakeProfit();" in modify_owned
    assert "OrderModify(target_ticket,expectedOpen,slNorm,expectedTp" in modify_owned


def test_strict_management_acks_only_post_selected_confirmed_mutations() -> None:
    source = _source()
    handle = _function(source, "void HandleCmd", "void UpdateDashboard")
    management = _function(source, 'if(cmd=="CLOSE"){', 'if(cmd=="INFO"){')
    close_owned = _function(
        source,
        "bool CloseOwnedTicket(",
        "bool SelectUniqueOwnedPartialRemainder",
    )
    remainder = _function(
        source,
        "bool SelectUniqueOwnedPartialRemainder",
        "bool CloseOwnedTicketPartial",
    )
    partial_owned = _function(
        source,
        "bool CloseOwnedTicketPartial",
        "bool ModifyOwnedTicketStop",
    )
    modify_owned = _function(source, "bool ModifyOwnedTicketStop", "// Prove that")

    assert handle.count("post_strict_management_ack(") == 3
    assert '(okClose && closeConfirmed)?"acked":"failed"' in handle
    assert (
        '(okClosePartial && partialConfirmed)?"acked":"failed"' in handle
    )
    assert '(okModify && modifyConfirmed)?"acked":"failed"' in handle
    assert management.count(
        'signal_id, "failed", sym, -1, 403, "ownership_contract_invalid"'
    ) == 3

    assert close_owned.index("mutationAttempted=true;") < close_owned.index(
        "OrderClose(target_ticket,expectedLots"
    )
    assert close_owned.index("OrderClose(target_ticket,expectedLots") < close_owned.index(
        "OrderSelect(target_ticket,SELECT_BY_TICKET,MODE_HISTORY)"
    )
    assert "actual.remaining_lots>lotTolerance" in close_owned
    assert "actual.close_time<=0" in close_owned
    assert "mutationConfirmed=true;" in close_owned

    assert "candidateCount!=1" in remainder
    assert "partial_close_remainder_ambiguous" in remainder
    assert partial_owned.index("mutationAttempted=true;") < partial_owned.index(
        "OrderClose(target_ticket,exactCloseLots"
    )
    assert "SelectUniqueOwnedPartialRemainder(" in partial_owned
    assert "MathAbs(actual.remaining_lots-remainder)>tolerance" in partial_owned
    assert "actual.close_time<=0 || actual.remaining_lots>tolerance" in partial_owned
    assert "mutationConfirmed=true;" in partial_owned

    assert modify_owned.index("mutationAttempted=true;") < modify_owned.index(
        "OrderModify(target_ticket,expectedOpen,slNorm,expectedTp"
    )
    assert modify_owned.index(
        "OrderModify(target_ticket,expectedOpen,slNorm,expectedTp"
    ) < modify_owned.index("OrderSelect(target_ticket,SELECT_BY_TICKET,MODE_TRADES)")
    assert "modify_sl_post_attestation_mismatch" in modify_owned
    assert "MathAbs(actual.remaining_lots-expectedLots)>lotTolerance" in modify_owned
    assert "actual.close_time!=0" in modify_owned
    assert "mutationConfirmed=true;" in modify_owned


def test_strict_management_actuals_are_versioned_and_pre_refusal_is_ticketless() -> None:
    source = _source()
    wrapper = _function(
        source,
        "void post_strict_management_ack",
        "int ReplayDurableSignalOutcome",
    )
    post_ack = _function(source, "void post_ack", "void post_strict_management_ack")

    assert 'effectiveStatus="reconcile_required"' in wrapper
    assert "mutation_attempted && !mutation_confirmed" in wrapper
    assert "? target_ticket" in wrapper
    assert ": -1;" in wrapper
    assert "actual.execution_type,target_ticket,actual.magic" in wrapper
    assert "actual.order_comment,actual.lots,actual.sl_price,actual.tp_price" in wrapper
    assert "mutation_confirmed ? BROKER_ORDER_ACTUALS_SCHEMA : \"\"" in wrapper
    assert "actual.remaining_lots,actual.close_time" in wrapper
    assert '\\"actual_remaining_lots\\"' in post_ack
    assert '\\"actual_close_time\\"' in post_ack


def test_legacy_symbol_management_can_only_touch_historical_elbridge_comments() -> None:
    source = _source()
    handle = _function(source, "void HandleCmd", "void UpdateDashboard")
    close_all = _function(source, "bool CloseAll", "bool CloseSymbol")
    close_symbol = _function(source, "bool CloseSymbol", "bool SelectOwnedMarketOrder")
    partial_plan = _function(source, "bool ValidatePartialClosePlan", "bool CloseSymbolPartial")
    partial_symbol = _function(source, "bool CloseSymbolPartial", "bool IsStrictlyTighterStop")
    modify_symbol = _function(source, "bool ModifySymbolStop", "string ToUpperSafe")

    assert "OrderComment()!=owner_comment" in close_all
    assert "target_ticket_provided || owner_token_provided" in handle
    assert "!target_ticket_provided && !owner_token_provided" in handle
    assert "OrderComment() != LEGACY_ORDER_COMMENT" in close_symbol
    assert "OrderComment()!=LEGACY_ORDER_COMMENT" in partial_plan
    assert "OrderComment() != LEGACY_ORDER_COMMENT" in partial_symbol
    assert "OrderComment() != LEGACY_ORDER_COMMENT" in modify_symbol
