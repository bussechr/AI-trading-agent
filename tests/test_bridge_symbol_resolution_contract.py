from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_EA = ROOT / "MQL4" / "Experts" / "BridgeEA.mq4"


def _source() -> str:
    return BRIDGE_EA.read_text(encoding="utf-8")


def test_bridge_symbol_resolution_uses_strict_deterministic_precedence() -> None:
    source = _source()
    resolver = source.split("bool ResolveBrokerSymbolStatus", 1)[1].split(
        "bool ResolveBrokerSymbolEx", 1
    )[0]

    precedence_markers = (
        "if(requestedExactCount == 1)",
        "if(logicalExactCount == 1)",
        "if(selectedPartialCount == 1)",
        "if(partialCount == 1)",
    )
    positions = [resolver.index(marker) for marker in precedence_markers]

    assert positions == sorted(positions)
    assert "SymbolsTotal(false)" in resolver
    assert "SymbolsTotal(true)" in resolver
    assert '"requested_exact"' in resolver
    assert '"logical_exact"' in resolver
    assert '"selected_partial"' in resolver
    assert '"unique_partial"' in resolver


def test_bridge_symbol_resolution_fails_closed_on_partial_ambiguity() -> None:
    source = _source()
    resolver = source.split("bool ResolveBrokerSymbolStatus", 1)[1].split(
        "bool ResolveBrokerSymbolEx", 1
    )[0]
    wrapper = source.split("bool ResolveBrokerSymbolEx", 1)[1].split(
        "string ResolveBrokerSymbol", 1
    )[0]
    public_resolver = source.split("string ResolveBrokerSymbol", 1)[1].split(
        "string StringTrim", 1
    )[0]

    assert "if(selectedPartialCount > 1)" in resolver
    assert 'reason = "ambiguous_selected_partial_match"' in resolver
    assert "if(partialCount > 1)" in resolver
    assert 'reason = "ambiguous_partial_match"' in resolver
    assert resolver.count("ambiguous = true") >= 4
    assert "ResolveBrokerSymbolStatus(" in wrapper
    assert 'if(!ResolveBrokerSymbolEx(requested, resolved)) return "";' in public_resolver


def test_bridge_status_reuses_cached_symbol_mapping_diagnostics() -> None:
    source = _source()
    status_report = source.split("void reportBridgeStatus", 1)[1].split(
        "void reportSymbolSpecs", 1
    )[0]
    specs_report = source.split("void reportSymbolSpecs", 1)[1].split(
        "void OnTick", 1
    )[0]

    assert "EnsureMarketDataSymbolCache()" in status_report
    assert "gMarketDataStrategySymbolCount" in status_report
    assert "gMarketDataLogicalSymbols[i]" in status_report
    assert "gMarketDataBrokerSymbols[i]" in status_report
    assert "gMarketDataMappingReasons[i]" in status_report
    assert "gMarketDataMappingKinds[i]" in status_report
    assert "gMarketDataMappingCandidateCounts[i]" in status_report
    assert "gMarketDataMappingAmbiguous[i]" in status_report
    assert '\\"mapping_ambiguous\\"' in status_report
    assert '\\"mapping_reason\\"' in status_report
    assert '\\"mapping_kind\\"' in status_report
    assert '\\"mapping_candidate_count\\"' in status_report
    assert "ResolveBrokerSymbolStatus(" not in status_report
    assert "SymbolsTotal(" not in status_report

    assert "EnsureMarketDataSymbolCache()" in specs_report
    assert "gMarketDataLogicalSymbols[i]" in specs_report
    assert "gMarketDataBrokerSymbols[i]" in specs_report
    assert "ResolveBrokerSymbolEx(" not in specs_report
    assert "SymbolsTotal(" not in specs_report


def test_tick_cache_adds_only_resolvable_account_conversion_crosses() -> None:
    source = _source()
    conversion_scope = source.split(
        "int EffectiveMarketDataSymbols", 1
    )[1].split("string StringTrim", 1)[0]
    refresh = source.split(
        "bool RefreshMarketDataSymbolCache", 1
    )[1].split("bool EnsureMarketDataSymbolCache", 1)[0]
    broadcaster = source.split("void broadcastTick", 1)[1].split(
        "void OnTimer", 1
    )[0]

    assert "EffectiveSymbols(out)" in conversion_scope
    assert "AccountCurrency()" in conversion_scope
    assert "ResolveBrokerSymbolEx(direct, resolved)" in conversion_scope
    assert "ResolveBrokerSymbolEx(inverse, resolved)" in conversion_scope
    assert "EffectiveMarketDataSymbols(logicalCandidates)" in refresh
    assert "candidateCount < strategyCount" in refresh
    assert "configured_scope_drift" in refresh
    assert "gMarketDataStrategySymbolCount = strategyCount" in refresh
    for cached_diagnostic in (
        "gMarketDataMappingReasons",
        "gMarketDataMappingKinds",
        "gMarketDataMappingCandidateCounts",
        "gMarketDataMappingAmbiguous",
    ):
        assert cached_diagnostic in refresh
    assert "gMarketDataLogicalSymbols[i]" in broadcaster
    assert "gMarketDataBrokerSymbols[i]" in broadcaster
    assert "EffectiveMarketDataSymbols(" not in broadcaster
    assert "ResolveBrokerSymbol(" not in broadcaster
    assert "SymbolSelect(" not in broadcaster


def test_tick_cache_is_all_or_nothing_identity_bound_and_periodically_refreshed() -> None:
    source = _source()
    identity = source.split(
        "string CurrentMarketDataSymbolCacheIdentity", 1
    )[1].split("bool RefreshMarketDataSymbolCache", 1)[0]
    refresh = source.split(
        "bool RefreshMarketDataSymbolCache", 1
    )[1].split("bool EnsureMarketDataSymbolCache", 1)[0]
    ensure = source.split(
        "bool EnsureMarketDataSymbolCache", 1
    )[1].split("string StringTrim", 1)[0]
    init = source.split("int OnInit", 1)[1].split("void RemoveDashboard", 1)[0]
    broadcaster = source.split("void broadcastTick", 1)[1].split(
        "void OnTimer", 1
    )[0]

    for marker in (
        "CurrentBrokerAccountScope(Magic)",
        "AccountServer()",
        "AccountCompany()",
        "AccountCurrency()",
        "SymbolsCsv",
    ):
        assert marker in identity
    assert "identityAfter != identityBefore" in refresh
    assert "ClearMarketDataSymbolCache();" in refresh
    assert "mapping_failed:" in refresh
    assert "ArraySize(resolvedLogical) != candidateCount" in refresh
    assert "MARKET_DATA_SYMBOL_CACHE_REFRESH_SECS 900" in source
    assert "MARKET_DATA_SYMBOL_CACHE_RETRY_SECS 5" in source
    assert "gLastMarketDataSymbolCacheRefresh" in ensure
    assert "if(BarHistoryMinuteEdgeGuardActive())" in ensure
    assert ensure.index("BarHistoryMinuteEdgeGuardActive()") < ensure.index(
        "RefreshMarketDataSymbolCache(force)"
    )
    assert "RefreshMarketDataSymbolCache(force)" in ensure
    assert "RefreshMarketDataSymbolCache(true);" in init
    assert "EnsureMarketDataSymbolCache()" in broadcaster
    assert "syms[0] = Symbol()" not in broadcaster
