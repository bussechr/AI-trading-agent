from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_EA = ROOT / "MQL4" / "Experts" / "BridgeEA.mq4"
IG_CATALOG = (
    ROOT
    / "fx-quant-stack"
    / "src"
    / "fxstack"
    / "providers"
    / "ig_mt4_catalog.py"
)


def _source() -> str:
    return BRIDGE_EA.read_text(encoding="utf-8")


def test_bridge_default_universe_exactly_matches_production_ig_catalog() -> None:
    source = _source()
    catalog_source = IG_CATALOG.read_text(encoding="utf-8")
    catalog_symbols = tuple(
        re.findall(r'canonical_symbol="([A-Z]{6})"', catalog_source)
    )
    input_match = re.search(r'input string SymbolsCsv = "([^"]+)";', source)
    default_match = re.search(
        r'string DefaultSymbolsCsv\(\) \{\s*return "([^"]+)";', source
    )

    assert len(catalog_symbols) == len(set(catalog_symbols)) == 22
    assert input_match is not None
    assert default_match is not None
    assert tuple(input_match.group(1).split(",")) == catalog_symbols
    assert tuple(default_match.group(1).split(",")) == catalog_symbols


def test_heartbeat_reports_raw_broker_evidence_without_account_number() -> None:
    source = _source()
    heartbeat = source.split("void heartbeat", 1)[1].split(
        "void reportBridgeStatus", 1
    )[0]

    assert '\\"report_type\\":\\"heartbeat\\"' in heartbeat
    assert "AccountServer()" in heartbeat
    assert "AccountCompany()" in heartbeat
    assert "AccountCurrency()" in heartbeat
    assert "AccountNumber()" not in heartbeat
    assert '\\"broker_server\\"' in heartbeat
    assert '\\"broker_company\\"' in heartbeat
    assert '\\"broker_account_currency\\"' in heartbeat
    assert '\\"broker_account_scope_schema\\"' in heartbeat
    assert '\\"broker_account_scope_version\\"' in heartbeat
    assert "BROKER_ACCOUNT_SCOPE_SCHEMA" in heartbeat
    assert "BROKER_ACCOUNT_SCOPE_VERSION" in heartbeat
    assert "broker_venue_id" not in heartbeat


def test_authoritative_snapshot_carries_restart_identity_and_is_only_cadence() -> None:
    source = _source()
    snapshot = source.split("void EmitPositionsSnapshot", 1)[1].split(
        "void SendPositions", 1
    )[0]
    market_source = source.split(
        "string CurrentBrokerMarketSourceJsonFields", 1
    )[1].split("void heartbeat", 1)[0]
    timer = source.split("void OnTimer", 1)[1].split("void HandleCmd", 1)[0]
    auxiliary = source.split(
        "bool ServiceOneAuxiliaryMaintenance", 1
    )[1].split("void reportBridgeStatus", 1)[0]

    for field in (
        "schema_version",
        "broker_symbol",
        "ticket",
        "magic",
        "order_comment",
        "tp",
    ):
        assert f'\\"{field}\\"' in snapshot
    assert "CurrentBrokerMarketSourceJsonFields()" in snapshot
    assert "StringLen(marketSourceFields)<=0" in snapshot
    for field in (
        "broker_account_scope",
        "broker_account_scope_schema",
        "broker_account_scope_version",
        "consumer_identity",
        "producer_instance_id",
        "terminal_lease_scope",
        "credential_generation_id",
        "bridge_protocol_version",
    ):
        assert f'\\"{field}\\"' in market_source
    assert "JsonEscape(OrderComment())" in snapshot
    assert "ServiceOneAuxiliaryMaintenance();" in timer
    assert "EmitPositionsSnapshot();" in auxiliary
    assert source.count("EmitPositionsSnapshot();") == 1
    assert "SendPositions();" not in timer
