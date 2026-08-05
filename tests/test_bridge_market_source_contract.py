from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_EA = ROOT / "MQL4" / "Experts" / "BridgeEA.mq4"
PYTHON_WIRE = ROOT / "fx-quant-stack" / "src" / "fxstack" / "api" / "wire.py"
DASHBOARD_BRIDGE = ROOT / "lib" / "server" / "bridge.ts"


def _source() -> str:
    return BRIDGE_EA.read_text(encoding="utf-8")


def test_bridge_protocol_version_is_exactly_v3_across_all_clients() -> None:
    patterns = (
        (PYTHON_WIRE, r'BRIDGE_PROTOCOL_VERSION:\s*str\s*=\s*"([^"]+)"'),
        (BRIDGE_EA, r'#define\s+EA_EXPECTED_PROTOCOL_VERSION\s+"([^"]+)"'),
        (DASHBOARD_BRIDGE, r'BRIDGE_EXPECTED_PROTOCOL_VERSION\s*=\s*"([^"]+)"'),
    )
    versions: list[str] = []
    for path, pattern in patterns:
        match = re.search(pattern, path.read_text(encoding="utf-8"))
        assert match is not None, path
        versions.append(match.group(1))
    assert versions == ["v3.0.0", "v3.0.0", "v3.0.0"]
    python_wire = PYTHON_WIRE.read_text(encoding="utf-8")
    minimum = re.search(
        r'BRIDGE_PROTOCOL_MIN_COMPATIBLE:\s*str\s*=\s*"([^"]+)"',
        python_wire,
    )
    assert minimum is not None
    assert minimum.group(1) == "v3.0.0"


def test_tick_bar_and_heartbeat_carry_the_same_pinned_market_source() -> None:
    source = _source()
    source_helper = source.split(
        "string CurrentBrokerMarketSourceJsonFields", 1
    )[1].split("void heartbeat", 1)[0]
    heartbeat = source.split("void heartbeat", 1)[1].split(
        "void reportBridgeStatus", 1
    )[0]
    bars = source.split("bool SendBarHistoryForSymbol", 1)[1].split(
        "void MaybeSyncBarHistory", 1
    )[0]
    ticks = source.split("void broadcastTick", 1)[1].split("void OnTimer", 1)[0]

    assert "TryPinAckOutboxScopeIdentity()" in source_helper
    assert "AckOutboxScopeIdentityMatches(identityReason)" in source_helper
    for field in (
        "broker_account_scope",
        "broker_account_scope_schema",
        "broker_account_scope_version",
        "broker_server",
        "broker_company",
        "consumer_identity",
        "producer_instance_id",
        "terminal_lease_scope",
        "credential_generation_id",
        "bridge_protocol_version",
    ):
        assert f'\\"{field}\\"' in source_helper or f'\\"{field}\\"' in heartbeat
    assert "EA_EXPECTED_PROTOCOL_VERSION" in source_helper
    assert "marketSourceFields" in bars
    assert "marketSourceFields" in ticks
    assert "marketSourceFields" in heartbeat
    assert "broker_venue_id" not in source_helper


def test_tick_batch_uses_one_authenticated_envelope_for_all_quotes() -> None:
    broadcaster = _source().split("void broadcastTick", 1)[1].split(
        "void OnTimer", 1
    )[0]

    assert 'return "/v2/market/ticks";' in _source()
    assert 'string ticksJson = "";' in broadcaster
    assert 'if(emitted > 0) ticksJson = ticksJson + ",";' in broadcaster
    assert '"{\\"ticks\\":[" + ticksJson + "]" + marketSourceFields + "}"' in broadcaster
    assert broadcaster.count("HttpPOST(ApiBase + TickPath(), payload, gBridgeApiKey)") == 1
    assert "HttpPOST(ApiBase + TickPath(), tick, gBridgeApiKey)" not in broadcaster

    market_source_fields = (
        ',"broker_account_scope":"ig-demo-account"'
        ',"bridge_protocol_version":"v3.0.0"'
    )
    body = (
        '{"ticks":[{"symbol":"EURUSD","broker_symbol":"EURUSD",'
        '"bid":1.10000,"ask":1.10020}]'
        + market_source_fields
        + "}"
    )
    decoded = json.loads(body)
    assert decoded["ticks"][0]["bid"] == 1.1
    assert decoded["bridge_protocol_version"] == "v3.0.0"


def test_terminal_instance_id_is_local_durable_and_binds_poll_ack_and_outbox() -> None:
    source = _source()
    identity = source.split("string LoadOrCreateProducerInstanceId", 1)[1].split(
        "// ACK files use FILE_COMMON", 1
    )[0]
    pin_scope = source.split("bool TryPinAckOutboxScopeIdentity", 1)[1].split(
        "bool AckOutboxScopeIdentityMatches", 1
    )[0]
    poll_path = source.split("string PollPath", 1)[1].split("string ReportPath", 1)[0]
    ack = source.split("void post_ack", 1)[1].split("void CleanupSeenSignals", 1)[0]
    init = source.split("int OnInit", 1)[1].split("void RemoveDashboard", 1)[0]

    assert "PRODUCER_INSTANCE_ID_FILE" in identity
    assert "FILE_WRITE|FILE_TXT|FILE_ANSI" in identity
    assert "FILE_COMMON" not in identity
    assert identity.index("FileWriteString(handle,candidate)") < identity.index(
        "FileFlush(handle)"
    )
    assert identity.index(
        "FileMove(tempPath,0,PRODUCER_INSTANCE_ID_FILE,0)"
    ) < identity.index("ReadProducerInstanceIdFile(verified)")
    assert r'"\\producer_"+producerToken' in pin_scope
    assert "gAckScopeProducerInstanceId=gBridgeProducerInstanceId" in pin_scope
    assert "producer_instance_id=" in poll_path
    assert "bridge_protocol_version=" in poll_path
    assert '\\"producer_instance_id\\"' in ack
    assert '\\"bridge_protocol_version\\"' in ack
    assert "LoadOrCreateProducerInstanceId()" in init
    assert "return(INIT_FAILED);" in init


def test_protocol_mismatch_fences_command_polling_and_retries() -> None:
    source = _source()
    handshake = source.split("bool VerifyBridgeHandshake", 1)[1].split(
        "int OnInit", 1
    )[0]
    timer = source.split("void OnTimer", 1)[1].split(
        "bool IsValidOwnerToken", 1
    )[0]

    assert "gBridgeHandshakeCompatible = false" in handshake
    assert "gBridgeHandshakeCompatible = true" in handshake
    assert "return(false);" in handshake
    assert "VerifyBridgeHandshake();" in timer
    assert "gLastBridgeHandshakeAttempt+14" in timer
    fence = timer.index("if(!gBridgeHandshakeCompatible) return;")
    poll = timer.index("HttpGET(pollUrl, gBridgeCommandToken)")
    assert fence < poll


def test_authenticated_bootstrap_uses_completed_bounded_m1_for_default_scope() -> None:
    source = _source()
    symbols = source.split("int BarHistorySymbols", 1)[1].split(
        "bool SendBarHistoryForSymbol", 1
    )[0]
    bars = source.split("bool SendBarHistoryForSymbol", 1)[1].split(
        "void MaybeSyncBarHistory", 1
    )[0]

    assert "if(configured > 0) return configured;" in symbols
    assert "return EffectiveSymbols(out);" in symbols
    assert "SCALP_BOOTSTRAP_COMPLETED_DEPTH 241" in source
    assert "int fullDepth = SCALP_BOOTSTRAP_COMPLETED_DEPTH;" in bars
    assert bars.count("PERIOD_M1") >= 2
    assert "PERIOD_M5" not in bars
    assert '\\"timeframe\\":\\"M1\\"' in bars
    assert "CopyRates(brokerSym, PERIOD_M1, 1, depth, bars)" in bars
    assert "for(int index = copied - 1; index >= 0; index--)" in bars
    assert "bar.tick_volume" in bars
    for field in ("bid_open", "bid_high", "bid_low", "bid_close"):
        assert f'\\"{field}\\":' in bars
    assert (
        '\\"volume_source\\":\\"mt4_ivolume_tick_count_v1\\"' in bars
    )
    assert '\\"price_basis\\":\\"mt4_bid_ohlc_v1\\"' in bars


def test_completed_m1_history_is_periodically_reseeded_after_api_restart() -> None:
    source = _source()
    timer = source.split("void OnTimer", 1)[1].split(
        "bool IsValidOwnerToken", 1
    )[0]
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]

    assert "SCALP_BAR_HISTORY_RESYNC_SECS 60" in source
    assert "minuteBucket > gLastBarHistoryMinuteBucket" in sync
    assert "secondsIntoMinute >= SCALP_BAR_HISTORY_MINUTE_EDGE_DELAY_SECS" in sync
    assert "handshakeCompatibleAtStart && highResolutionBarPublication" in timer
    assert "handshakeCompatibleAtStart && !highResolutionBarPublication" in timer
    assert "ProbeRemoteBarHistoryCoverage(" in sync
    assert "probeSymbol," in sync
    assert "probeBrokerSymbol," in sync
    assert 'ResetBarHistoryBootstrap("remote_seed_missing")' in sync
    assert 'return "/v2/market/bars/coverage";' in source
    assert "SCALP_BAR_HISTORY_COVERAGE_SCHEMA" in source
    assert "SCALP_BAR_HISTORY_REMOTE_QUERY_BARS" not in source
    assert (
        "SCALP_BAR_HISTORY_REMOTE_MIN_DIRECT_BARS "
        "SCALP_BOOTSTRAP_COMPLETED_DEPTH"
    ) in source


def test_m1_steady_state_retries_from_utc_plus_one_through_plus_four() -> None:
    source = _source()
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]
    init = source.split("int OnInit", 1)[1].split("void RemoveDashboard", 1)[0]
    delay_match = re.search(
        r"#define\s+SCALP_BAR_HISTORY_MINUTE_EDGE_DELAY_SECS\s+(\d+)", source
    )
    cadence_match = re.search(
        r"#define\s+SCALP_BAR_HISTORY_RESYNC_SECS\s+(\d+)", source
    )
    deadline_match = re.search(
        r"#define\s+SCALP_BAR_HISTORY_MINUTE_EDGE_DEADLINE_SECS\s+(\d+)",
        source,
    )

    assert delay_match is not None
    assert cadence_match is not None
    assert deadline_match is not None
    delay_seconds = int(delay_match.group(1))
    cadence_seconds = int(cadence_match.group(1))
    deadline_seconds = int(deadline_match.group(1))
    assert cadence_seconds == 60
    assert delay_seconds == 1
    assert deadline_seconds == 4
    assert 0 < delay_seconds < 5
    assert delay_seconds <= deadline_seconds < 5
    assert "datetime now = TimeGMT();" in sync
    assert "int minuteBucket = (int)(now / SCALP_BAR_HISTORY_RESYNC_SECS);" in sync
    assert "int secondsIntoMinute = (int)(now % SCALP_BAR_HISTORY_RESYNC_SECS);" in sync
    assert "minuteBucket > gLastBarHistoryMinuteBucket" in sync
    assert "BeginBarHistoryMinuteEdge(minuteBucket);" in sync
    assert "withinMinuteEdgeDeadline || lateReconciliationDue" in sync
    assert "SCALP_BAR_HISTORY_LATE_RETRY_SECS 10" in source
    assert "gLastBarHistoryMinuteBucket=-1;" in init
    assert "EventSetMillisecondTimer(BRIDGE_EDGE_TIMER_INTERVAL_MS)" in init

    # Every possible EA startup second converges on the same next-minute +1s
    # publication point; cadence no longer inherits that startup offset.
    for startup_second in range(cadence_seconds):
        initial_bucket = startup_second // cadence_seconds
        due_times = [
            tick
            for tick in range(startup_second + 1, startup_second + 2 * cadence_seconds)
            if tick // cadence_seconds > initial_bucket
            and tick % cadence_seconds >= delay_seconds
        ]
        assert due_times[0] == cadence_seconds + delay_seconds


def test_high_resolution_edge_timer_does_not_accelerate_non_bar_work() -> None:
    source = _source()
    init = source.split("int OnInit", 1)[1].split("void RemoveDashboard", 1)[0]
    timer = source.split("void OnTimer", 1)[1].split(
        "bool IsValidOwnerToken", 1
    )[0]

    assert "BRIDGE_EDGE_TIMER_INTERVAL_MS 250" in source
    assert "BRIDGE_MAIN_TIMER_INTERVAL_MS 1000" in source
    assert "EventSetMillisecondTimer(BRIDGE_EDGE_TIMER_INTERVAL_MS)" in init
    assert "EventSetTimer(1)" not in init
    assert "gMainTimerBodyAttempted=false;" in init
    assert "gLastMainTimerBodyMs=0;" in init

    edge = timer.index("MaybeSyncBarHistory(false);")
    throttle_clock = timer.index("uint mainBodyNowMs = GetTickCount();")
    throttle_gate = timer.index(
        "(uint)(mainBodyNowMs - gLastMainTimerBodyMs) <"
    )
    ack = timer.index("ServiceAckOutbox(ACK_OUTBOX_REPLAY_PER_TIMER);")
    cycle = timer.index("manageCycle();")
    tick = timer.index("broadcastTick();")
    poll = timer.index("HttpGET(pollUrl, gBridgeCommandToken)")
    assert edge < throttle_clock < throttle_gate < ack < cycle < tick < poll
    assert "(uint)BRIDGE_MAIN_TIMER_INTERVAL_MS" in timer
    assert "gMainTimerBodyAttempted = true;" in timer
    assert "gLastMainTimerBodyMs = mainBodyNowMs;" in timer


def test_edge_requires_exact_broker_tick_minute_and_final_shift_recheck() -> None:
    source = _source()
    readiness = source.split("bool ExpectedFinalizedM1Ready", 1)[1].split(
        "void MixBarHistoryFingerprint", 1
    )[0]
    refresh = source.split("bool RefreshDirectM1Series", 1)[1].split(
        "bool ExpectedFinalizedM1Ready", 1
    )[0]
    bars = source.split("bool SendBarHistoryForSymbol", 1)[1].split(
        "void MaybeSyncBarHistory", 1
    )[0]
    serializer = source.split("bool BuildExactFinalizedM1BatchEntry", 1)[1].split(
        "bool PublishPendingBarHistoryMinuteEdgeBatch", 1
    )[0]

    assert "CopyRates(brokerSym, PERIOD_M1, 0, 3, probe)" in refresh
    assert "MarketInfo(brokerSym, MODE_TIME)" in readiness
    assert "expectedClosedTime <= lastEmittedClosedTime" in readiness
    assert "if(!RefreshDirectM1Series(brokerSym))" in readiness
    assert 'reason = "direct_m1_refresh_pending";' in readiness
    assert "iTime(brokerSym, PERIOD_M1, 0)" in readiness
    assert "iTime(brokerSym, PERIOD_M1, 1)" in readiness
    assert "SERIES_LASTBAR_DATE" not in readiness
    assert "currentBarTime != sourceMinute" in readiness
    assert "closedBarTime != expectedClosedTime" in readiness
    assert "CopyRates(brokerSym, PERIOD_M1, 1, 1, bars)" in serializer
    assert "finalShiftOneTime != requiredClosedTime" in serializer
    assert "newestClosedTime != requiredClosedTime" in bars
    assert "newestClosedIncluded" in bars
    assert "emittedClosedTime == requiredClosedTime" in bars


def test_edge_batch_completes_included_symbols_only_after_atomic_2xx() -> None:
    source = _source()
    publisher = source.split(
        "bool PublishPendingBarHistoryMinuteEdgeBatch", 1
    )[1].split("void MaybeSyncBarHistory", 1)[0]
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]
    edge_branch = sync.split("if(minuteEdgeDue) {", 1)[1].split(
        "// API bar history is intentionally process-local.", 1
    )[0]

    readiness = publisher.index("ExpectedFinalizedM1Ready(")
    serialize = publisher.index("BuildExactFinalizedM1BatchEntry(")
    post = publisher.index(
        "HttpPOST(ApiBase + BarHistoryBatchPath(), payload, gBridgeApiKey)"
    )
    status_gate = publisher.index("if(statusCode < 200 || statusCode >= 300)")
    record = publisher.index("RecordEmittedClosedBarTime(")
    complete_flag = publisher.index(
        "gBarHistoryPendingCompletedFlags[completeIndex] = true;"
    )
    complete_bucket = edge_branch.index(
        "gLastBarHistoryMinuteBucket = gBarHistoryPendingMinuteBucket;"
    )
    assert readiness < serialize < post < status_gate < record < complete_flag
    assert publisher.count(
        "HttpPOST(ApiBase + BarHistoryBatchPath(), payload, gBridgeApiKey)"
    ) == 1
    assert "SendBarHistoryForSymbol" not in publisher
    assert "gBarHistoryPendingRemaining -= includedCount;" in publisher
    assert "pendingCount = gBarHistoryPendingRemaining;" in publisher
    assert "failedCount += includedCount;" in publisher
    failure = publisher.split(
        "if(statusCode < 200 || statusCode >= 300) {", 1
    )[1].split("// This is deliberately the first mutation", 1)[0]
    assert "return false;" in failure
    assert "RecordEmittedClosedBarTime" not in failure
    assert "gBarHistoryPendingCompletedFlags[completeIndex] = true;" not in failure
    assert "gBarHistoryPendingCompletedSymbols" not in source
    assert "PublishPendingBarHistoryMinuteEdgeBatch(" in edge_branch
    assert "SendBarHistoryForSymbol" not in edge_branch
    assert edge_branch.index("PublishPendingBarHistoryMinuteEdgeBatch(") < complete_bucket
    assert "gBarHistoryPendingRemaining == 0" in edge_branch
    assert "ClearBarHistoryMinuteEdge();" in edge_branch
    assert edge_branch.rstrip().endswith("return;\n   }")


def test_edge_batch_has_one_top_level_authenticated_envelope_for_ready_rows() -> None:
    source = _source()
    builder = source.split("bool BuildExactFinalizedM1BatchEntry", 1)[1].split(
        "bool PublishPendingBarHistoryMinuteEdgeBatch", 1
    )[0]
    publisher = source.split(
        "bool PublishPendingBarHistoryMinuteEdgeBatch", 1
    )[1].split("void MaybeSyncBarHistory", 1)[0]
    scan = publisher.split(
        "// MT4 executes every EA timer callback on its terminal thread.", 1
    )[1].split("string marketSourceFields", 1)[0]

    assert 'return "/v2/market/bars/batch";' in source
    assert (
        '"{\\"batches\\":[" + batchesJson + "]" + marketSourceFields + "}"'
        in publisher
    )
    assert publisher.count("CurrentBrokerMarketSourceJsonFields()") == 1
    assert "CurrentBrokerMarketSourceJsonFields" not in builder
    assert '\\"symbol\\":\\"' in builder
    assert '\\"timeframe\\":\\"M1\\"' in builder
    assert '\\"bars\\":[' in builder
    assert "StageBarHistoryEdgeEntry(i, batchEntryJson, emittedClosedTime)" in scan
    assert "gBarHistoryEdgeStagedCount" in scan
    assert "HttpPOST" not in scan
    assert (
        "batchesJson = batchesJson + gBarHistoryEdgeStagedEntries[stagedIndex];"
        in publisher
    )
    assert publisher.count(
        "HttpPOST(ApiBase + BarHistoryBatchPath(), payload, gBridgeApiKey)"
    ) == 1
    assert "HttpPOST(ApiBase + BarHistoryPath()" not in publisher

    market_source_fields = {
        "broker_account_scope": "ig-demo-account",
        "producer_instance_id": "producer-1",
        "bridge_protocol_version": "v3.0.0",
    }
    frame = {
        "batches": [
            {
                "symbol": symbol,
                "timeframe": "M1",
                "bars": [{"time": 1_787_000_000, "volume": 17}],
            }
            for symbol in ("EURUSD", "USDJPY")
        ],
        **market_source_fields,
    }
    decoded = json.loads(json.dumps(frame))
    assert [batch["symbol"] for batch in decoded["batches"]] == [
        "EURUSD",
        "USDJPY",
    ]
    assert decoded["batches"][0]["timeframe"] == "M1"
    assert decoded["producer_instance_id"] == "producer-1"
    assert "producer_instance_id" not in decoded["batches"][0]


def test_cold_bootstrap_waits_for_two_stable_full_direct_history_observations() -> None:
    source = _source()
    init = source.split("int OnInit", 1)[1].split("void RemoveDashboard", 1)[0]
    prime = source.split("void PrimeBarHistoryColdLoad", 1)[1].split(
        "string BarHistoryUtcMinuteText", 1
    )[0]
    observe = source.split(
        "BarHistoryObservationOutcome ObserveStableDirectM1History", 1
    )[1].split(
        "void ForgetBarHistoryBootstrap", 1
    )[0]
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]

    assert "PrimeBarHistoryColdLoad();" in init
    assert "MaybeSyncBarHistory(true);" not in init
    assert "publishes no data" in prime
    assert "gLastBarHistoryMinuteBucket =\n      (int)(now / SCALP_BAR_HISTORY_RESYNC_SECS);" in prime
    assert "ClearBarHistoryMinuteEdge();" in prime
    assert "SendBarHistoryForSymbol" not in prime
    assert "CopyRates(" not in prime
    assert "SymbolSelect(" not in prime
    assert "EnsureBarHistoryRecoveryChart(" not in prime
    assert "cold M1 recovery scheduled" in prime
    assert "SCALP_BOOTSTRAP_COMPLETED_DEPTH 241" in source
    assert "int fullDepth = SCALP_BOOTSTRAP_COMPLETED_DEPTH;" in observe
    assert "SERIES_SYNCHRONIZED" in observe
    assert "if(seriesSynchronized" not in observe
    cheap_edge_refresh = observe.index(
        "bool edgeRefreshReady = RefreshDirectM1Series(brokerSym);"
    )
    refresh_refusal = observe.index("if(!edgeRefreshReady)")
    current_refusal = observe.index('"direct_m1_current_mismatch"')
    full_copy = observe.index(
        "CopyRates(\n      brokerSym, PERIOD_M1, 1, fullDepth, probe"
    )
    assert cheap_edge_refresh < refresh_refusal < current_refusal < full_copy
    assert observe.index(
        "EnsureBarHistoryRecoveryChart(logicalSym, brokerSym);",
        refresh_refusal,
    ) < observe.index(
        "return BAR_HISTORY_OBSERVATION_INCOHERENT;",
        refresh_refusal,
    )
    assert '"direct_m1_refresh_pending", 0, available' in observe
    assert "CopyRates(\n      brokerSym, PERIOD_M1, 1, fullDepth, probe" in observe
    assert "copied != fullDepth" in observe
    assert "probe[0].time != newestClosedTime" in observe
    for field in ("time", "open", "high", "low", "close", "tick_volume"):
        assert f"probe[index].{field}" in observe
    assert "barTime <= previousBarTime" in observe
    assert "barTime != previousBarTime + SCALP_BAR_HISTORY_RESYNC_SECS" not in observe
    assert "Preserve authentic no-tick minutes as gaps" in observe
    assert "newestClosedTime >= currentBarTime" in observe
    assert (
        "newestClosedTime != sourceMinute - SCALP_BAR_HISTORY_RESYNC_SECS"
        not in observe
    )
    assert "gBarHistoryStabilityFingerprints[index] != fingerprint" in observe
    assert "SCALP_BAR_HISTORY_STABILITY_SECS" in observe
    stability_gate = sync.index(
        "observationOutcome = ObserveStableDirectM1History(\n"
        "            logicalSym,\n"
        "            brokerSym,\n"
        "            now\n"
        "         )"
    )
    send = sync.index("bool historySent = SendBarHistoryForSymbol(")
    assert stability_gate < send


def test_cold_recovery_diagnostics_are_bounded_and_successes_retire() -> None:
    source = _source()
    diagnostics = source.split("void WarnBarHistoryRecoveryPending", 1)[1].split(
        "void ClearBarHistoryStability", 1
    )[0]
    retire = source.split("void RetireBarHistoryRecoveryState", 1)[1].split(
        "BarHistoryObservationOutcome ObserveStableDirectM1History", 1
    )[0]
    observe = source.split(
        "BarHistoryObservationOutcome ObserveStableDirectM1History", 1
    )[1].split(
        "void ForgetBarHistoryBootstrap", 1
    )[0]
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]

    assert "SCALP_BAR_HISTORY_DIAGNOSTIC_INTERVAL_SECS 60" in source
    assert "(now - lastWarnAt) < SCALP_BAR_HISTORY_DIAGNOSTIC_INTERVAL_SECS" in diagnostics
    for field in (
        '" reason="',
        '" copied="',
        '" iBars="',
        '" current="',
        '" source="',
        '" shift1="',
    ):
        assert field in diagnostics
    assert '"copy_incomplete"' in observe
    assert '"direct_m1_current_mismatch"' in observe
    assert '"direct_m1_shift1_incoherent"' in observe
    assert '" sync="' in diagnostics
    assert "if(seriesSynchronized == 0)" not in observe
    assert "ArrayResize(gBarHistoryStabilitySymbols, stabilityCount - 1);" in retire
    assert "ArrayResize(gBarHistoryDiagnosticSymbols, diagnosticCount - 1);" in retire
    assert "RetireBarHistoryRecoveryState(logicalSym);" in sync
    assert "!ContainsSymbol(gBarHistoryBootstrappedSymbols, logicalSym)" in sync


def test_all_scalp_m1_series_remain_materialized_until_ea_deinit() -> None:
    source = _source()
    observe = source.split(
        "BarHistoryObservationOutcome ObserveStableDirectM1History", 1
    )[1].split(
        "void ForgetBarHistoryBootstrap", 1
    )[0]
    ensure = source.split("bool EnsureBarHistoryRecoveryChart", 1)[1].split(
        "void CloseBarHistoryRecoveryChart", 1
    )[0]
    retire = source.split("void RetireBarHistoryRecoveryState", 1)[1].split(
        "BarHistoryObservationOutcome ObserveStableDirectM1History", 1
    )[0]
    deinit = source.split("void OnDeinit", 1)[1].split("void post_report", 1)[0]

    service = source.split("void ServiceOneBarHistoryMaterializationChart", 1)[1].split(
        "void RetireBarHistoryRecoveryState", 1
    )[0]
    timer = source.split("void OnTimer", 1)[1]

    assert "SCALP_BAR_HISTORY_RECOVERY_CHARTS_MAX 22" in source
    assert "ChartOpen(brokerSym, PERIOD_M1)" in ensure
    assert "HasOpenBarHistoryM1Chart(brokerSym)" in ensure
    assert "EnsureBarHistoryRecoveryChart(logicalSym, brokerSym);" in observe
    assert "CloseBarHistoryRecoveryChart(logicalSym);" not in retire
    assert "ServiceOneBarHistoryMaterializationChart();" in timer
    assert "BarHistoryMinuteEdgeGuardActive()" in service
    assert "gBarHistoryMaterializationCursor++" in service
    assert "CloseAllBarHistoryRecoveryCharts();" in deinit


def test_cold_edge_lane_bypasses_full_history_gate_and_batches_only_shift_one() -> None:
    source = _source()
    builder = source.split("bool BuildExactFinalizedM1BatchEntry", 1)[1].split(
        "bool PublishPendingBarHistoryMinuteEdgeBatch", 1
    )[0]
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]
    edge_branch = sync.split("if(minuteEdgeDue) {", 1)[1].split(
        "// API bar history is intentionally process-local.", 1
    )[0]

    assert "bool recoveryWorkActive =\n      !minuteEdgeDue &&" in sync
    assert "bool bootstrap =\n         recoveryWorkActive &&" in sync
    assert "if(!bootstrap) continue;" in sync
    assert "PublishPendingBarHistoryMinuteEdgeBatch(" in edge_branch
    assert "SendBarHistoryForSymbol" not in edge_branch
    assert edge_branch.rstrip().endswith("return;\n   }")
    assert "CopyRates(brokerSym, PERIOD_M1, 1, 1, bars)" in builder
    assert "finalShiftOneTime = iTime(brokerSym, PERIOD_M1, 1)" in builder
    assert '\\"timeframe\\":\\"M1\\",\\"bars\\":[' in builder
    assert "SCALP_BAR_HISTORY_INCREMENTAL_DEPTH 1" in source


def test_cold_recovery_is_per_symbol_bounded_and_remote_probe_is_post_edge() -> None:
    source = _source()
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]

    assert "SCALP_BAR_HISTORY_STABILITY_CHECK_SECS 5" in source
    assert "!ContainsSymbol(gBarHistoryBootstrappedSymbols, logicalSym)" in sync
    assert "if(!bootstrap) continue;" in sync
    assert "if(bootstrap && !stabilityCheckDue)" in sync
    assert (
        "observationOutcome = ObserveStableDirectM1History(\n"
        "            logicalSym,\n"
        "            brokerSym,\n"
        "            now\n"
        "         )"
        in sync
    )
    observer = source.split(
        "BarHistoryObservationOutcome ObserveStableDirectM1History", 1
    )[1].split("void PrimeBarHistoryColdLoad", 1)[0]
    assert "string brokerSym," in observer
    assert "ResolveBrokerSymbol(logicalSym)" not in observer
    assert "ArraySize(gBarHistoryBootstrappedSymbols) == count" in sync
    assert "gBarHistoryDelayedRecoverySweepsRemaining = 0;" in sync
    assert "!force && !minuteEdgeDue && fullWorkAllowed &&" in sync
    assert "gBarHistoryRemoteProbePending" in sync
    assert sync.index("if(remoteProbeWorkDue)") < sync.index(
        "ProbeRemoteBarHistoryCoverage("
    )
    assert "SCALP_BAR_HISTORY_MAINTENANCE_START_SECS 7" in source
    assert "secondsIntoMinute > SCALP_BAR_HISTORY_MAINTENANCE_START_SECS" in sync
    assert "SCALP_BAR_HISTORY_MAINTENANCE_CUTOFF_SECS 45" in source
    assert "secondsIntoMinute < SCALP_BAR_HISTORY_MAINTENANCE_CUTOFF_SECS" in sync
    assert "delayedRecoveryReady && !minuteEdgeDue && fullWorkAllowed" in sync
    assert "!minuteEdgeDue && fullWorkAllowed &&" in sync


def test_cold_recovery_pins_only_a_bounded_coherent_stability_candidate() -> None:
    source = _source()
    observe = source.split(
        "BarHistoryObservationOutcome ObserveStableDirectM1History", 1
    )[1].split("void ForgetBarHistoryBootstrap", 1)[0]
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]

    assert "BAR_HISTORY_OBSERVATION_INCOHERENT = 0" in source
    assert "BAR_HISTORY_OBSERVATION_WAIT_STABLE = 1" in source
    assert "BAR_HISTORY_OBSERVATION_STABLE = 2" in source
    assert "SCALP_BAR_HISTORY_STABILITY_PINNED_RECHECKS_MAX 2" in source
    assert "gBarHistoryStabilityRecheckCounts[index] = 0;" in observe
    assert (
        "nextRecheckCount >=\n"
        "            SCALP_BAR_HISTORY_STABILITY_PINNED_RECHECKS_MAX"
        in observe
    )
    bounded_release = observe.index(
        "nextRecheckCount >=\n"
        "            SCALP_BAR_HISTORY_STABILITY_PINNED_RECHECKS_MAX"
    )
    assert observe.index(
        "ClearBarHistoryStabilityObservation(logicalSym);", bounded_release
    ) < observe.index(
        "return BAR_HISTORY_OBSERVATION_INCOHERENT;", bounded_release
    )

    wait_branch = sync.split(
        "observationOutcome == BAR_HISTORY_OBSERVATION_WAIT_STABLE", 1
    )[1].split(
        "observationOutcome == BAR_HISTORY_OBSERVATION_INCOHERENT", 1
    )[0]
    incoherent_branch = sync.split(
        "observationOutcome == BAR_HISTORY_OBSERVATION_INCOHERENT", 1
    )[1].split("datetime emittedClosedTime", 1)[0]
    send_branch = sync.split(
        "bool historySent = SendBarHistoryForSymbol(", 1
    )[1].split("if(!historySent)", 1)[0]
    assert "gBarHistoryRecoveryUploadCursor = i;" in wait_branch
    assert "break;" in wait_branch
    assert "gBarHistoryRecoveryUploadCursor = (i + 1) % count;" in incoherent_branch
    assert "break;" in incoherent_branch
    assert "gBarHistoryRecoveryUploadCursor = (i + 1) % count;" in send_branch
    assert "bool stabilityWaitPending = false;" in sync
    assert "stabilityWaitPending = true;" in wait_branch
    assert "if(stabilityWaitPending && stabilityCheckDue)" in sync
    assert "if(recoveryPendingCount > 0 && stabilityCheckDue)" not in sync
    assert "else if(!stabilityWaitPending && stabilityCheckDue)" in sync
    assert "int recoveryScanStart = gBarHistoryRecoveryUploadCursor;" in sync
    assert "? (recoveryScanStart + scanOffset) % count" in sync
    assert sync.count("recoveryWorkAttempts++;") == 1


def test_full_recovery_is_one_symbol_per_pass_and_aborts_common_transport_failure() -> None:
    source = _source()
    bars = source.split("bool SendBarHistoryForSymbol", 1)[1].split(
        "void MaybeSyncBarHistory", 1
    )[0]
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]

    assert "SCALP_BAR_HISTORY_FULL_UPLOADS_PER_PASS 1" in source
    assert (
        "recoveryWorkAttempts >= SCALP_BAR_HISTORY_FULL_UPLOADS_PER_PASS"
        in sync
    )
    assert "gBarHistoryRecoveryUploadCursor = (i + 1) % count;" in sync
    assert sync.index(
        "recoveryWorkAttempts >= SCALP_BAR_HISTORY_FULL_UPLOADS_PER_PASS"
    ) < sync.index("ObserveStableDirectM1History(")
    coverage = source.split(
        "RemoteBarHistoryCoverageOutcome ProbeRemoteBarHistoryCoverage", 1
    )[1].split("bool SendBarHistoryForSymbol", 1)[0]
    assert "BarHistoryCoveragePath()" in coverage
    assert '"&timeframe=M1&minimum="' in coverage
    assert "SCALP_BAR_HISTORY_REMOTE_MIN_DIRECT_BARS" in coverage
    assert "SCALP_BAR_HISTORY_COVERAGE_SCHEMA" in coverage
    assert 'string readyNeedle = "\\\"ready\\\":"' in coverage
    assert "readyTrue == readyFalse" in coverage
    assert 'readyDelimiter != "," && readyDelimiter != "}"' in coverage
    assert "StringFind(response, readyNeedle, readyAt +" in coverage
    assert 'ParseJsonStringField(response, "latest_direct_time", "")' in coverage
    assert "BAR_HISTORY_REMOTE_COVERAGE_UNAVAILABLE" in coverage
    assert "BAR_HISTORY_REMOTE_COVERAGE_MISSING" in coverage
    assert "BAR_HISTORY_REMOTE_COVERAGE_RETAINED_STALE" in coverage
    assert "BAR_HISTORY_REMOTE_COVERAGE_EXACT" in coverage
    assert "CountJsonToken" not in source
    assert "RecordEmittedClosedBarTime(" in sync
    assert "if(bootstrap && statusCode != 409)" in bars
    assert "gBarHistoryRecoveryTransportAbort = true;" in bars
    assert "if(bootstrap && gBarHistoryRecoveryTransportAbort) break;" in sync
    assert "now + SCALP_BAR_HISTORY_STABILITY_CHECK_SECS" in sync
    assert "now + SCALP_BAR_HISTORY_RESYNC_SECS" in sync


def test_periodic_seed_probe_does_not_turn_normal_latest_bar_lag_into_full_recovery() -> None:
    source = _source()
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]
    periodic_probe = sync.split("if(remoteProbeWorkDue)", 1)[1].split(
        "bool stabilityCheckDue", 1
    )[0]
    bootstrap_probe = sync.split(
        "datetime remoteCoveredClosedTime = 0;", 1
    )[1].split("BarHistoryObservationOutcome", 1)[0]

    assert "ProbeRemoteBarHistoryCoverage(" in periodic_probe
    assert "probeBrokerSymbol," in periodic_probe
    assert "probeCoveredClosedTime" in periodic_probe
    assert (
        "remoteProbeOutcome == BAR_HISTORY_REMOTE_COVERAGE_MISSING"
        in periodic_probe
    )
    reset_guard = periodic_probe.split(
        'ResetBarHistoryBootstrap("remote_seed_missing")', 1
    )[0]
    assert "BAR_HISTORY_REMOTE_COVERAGE_UNAVAILABLE" not in reset_guard
    assert "BAR_HISTORY_REMOTE_COVERAGE_RETAINED_STALE" not in reset_guard
    assert 'ResetBarHistoryBootstrap("remote_seed_missing")' in periodic_probe
    assert (
        "remoteCoverageOutcome == BAR_HISTORY_REMOTE_COVERAGE_EXACT"
        in bootstrap_probe
    )


def test_full_recovery_preserves_sparse_m1_history_instead_of_blocking_scope() -> None:
    source = _source()
    bars = source.split("bool SendBarHistoryForSymbol", 1)[1].split(
        "bool BuildExactFinalizedM1BatchEntry", 1
    )[0]

    assert "validateBar.time <= previousBarTime" in bars
    assert (
        "validateBar.time !=\n"
        "             previousBarTime + SCALP_BAR_HISTORY_RESYNC_SECS"
        not in bars
    )
    assert "leave missing\n      // minutes missing" in bars


def test_minute_edge_precedes_network_maintenance_and_defers_noncritical_work() -> None:
    source = _source()
    timer = source.split("void OnTimer", 1)[1].split(
        "bool IsValidOwnerToken", 1
    )[0]
    guard = source.split("bool BarHistoryMinuteEdgeGuardActive", 1)[1].split(
        "void OnTimer", 1
    )[0]

    edge = timer.index("MaybeSyncBarHistory(false);")
    ack = timer.index("ServiceAckOutbox(ACK_OUTBOX_REPLAY_PER_TIMER);")
    tick = timer.index("broadcastTick();")
    maintenance = timer.index("ServiceOneAuxiliaryMaintenance();")
    assert edge < ack < tick < maintenance
    assert "secondsIntoMinute >= 58" in guard
    assert "SCALP_BAR_HISTORY_MINUTE_EDGE_DEADLINE_SECS" in guard
    assert "if(!edgeGuardActive)\n      ServiceAckOutbox" in timer
    assert "if(!edgeGuardActive) ServiceOneAuxiliaryMaintenance();" in timer
    assert "int taskCount = 6;" in source


def test_periodic_history_sync_sends_only_new_bars_and_recovers_missed_windows() -> None:
    source = _source()
    bars = source.split("bool SendBarHistoryForSymbol", 1)[1].split(
        "bool BuildExactFinalizedM1BatchEntry", 1
    )[0]
    builder = source.split("bool BuildExactFinalizedM1BatchEntry", 1)[1].split(
        "bool PublishPendingBarHistoryMinuteEdgeBatch", 1
    )[0]
    publisher = source.split(
        "bool PublishPendingBarHistoryMinuteEdgeBatch", 1
    )[1].split("void MaybeSyncBarHistory", 1)[0]
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]

    incremental_match = re.search(
        r"#define\s+SCALP_BAR_HISTORY_INCREMENTAL_DEPTH\s+(\d+)", source
    )
    batch_match = re.search(r"input int\s+BarHistoryBatchSize\s*=\s*(\d+);", source)
    assert incremental_match is not None
    assert batch_match is not None
    incremental_depth = int(incremental_match.group(1))
    default_batch_size = int(batch_match.group(1))
    assert incremental_depth == 1
    assert incremental_depth <= default_batch_size
    assert "bootstrap\n      ? fullDepth\n      : MathMin(SCALP_BAR_HISTORY_INCREMENTAL_DEPTH, fullDepth);" in bars
    assert "int batchLimit = bootstrap\n      ? fullDepth" in bars
    assert "bool delayedRecoverySweep" in sync
    assert "bool supersededPendingEdge" in sync
    assert "bool schedulerWallClockGap" in sync
    assert "(minuteBucket - schedulerReferenceBucket) >" in sync
    assert 'ResetBarHistoryBootstrap("scheduler_wall_clock_gap")' in sync
    assert 'ResetBarHistoryBootstrap("missed_incremental_window")' not in source
    edge_start = sync.split("bool newMinuteEdge", 1)[1].split(
        "if(newMinuteEdge && gBarHistoryPendingMinuteBucket != minuteBucket)", 1
    )[0]
    assert edge_start.index(
        "RetireSupersededBarHistoryMinuteEdge(minuteBucket);"
    ) < len(edge_start)
    retire = source.split(
        "void RetireSupersededBarHistoryMinuteEdge", 1
    )[1].split("void PrimeBarHistoryColdLoad", 1)[0]
    assert "if(gBarHistoryPendingCompletedFlags[i]) continue;" in retire
    assert "ForgetBarHistoryBootstrap(logicalSym);" in retire
    assert "gBarHistoryFullRecoveryRequested = true;" in retire
    assert "RecordEmittedClosedBarTime" not in retire
    assert retire.index(
        "gLastBarHistoryMinuteBucket = pendingBucket;"
    ) < retire.rindex("ClearBarHistoryMinuteEdge();")
    assert "requiredClosedTime" in builder
    assert "emittedClosedTime" in builder
    assert "gBarHistoryEdgeStagedClosedTimes" in publisher
    assert "AppendUniqueBrokerSymbol(gBarHistoryBootstrappedSymbols, logicalSym)" in bars
    assert "for(int index = copied - 1; index >= 0; index--)" in bars
    assert "sentCount == depth" in bars
    assert "CopyRates(brokerSym, PERIOD_M1, 1, 1, bars)" in builder
    assert "BarHistoryBatchPath()" in publisher


def test_cold_terminal_performs_one_bounded_delayed_full_history_refresh() -> None:
    source = _source()
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]
    init = source.split("int OnInit", 1)[1].split("void RemoveDashboard", 1)[0]
    prime = source.split("void PrimeBarHistoryColdLoad", 1)[1].split(
        "string BarHistoryUtcMinuteText", 1
    )[0]

    assert "SCALP_BAR_HISTORY_DELAYED_RECOVERY_SWEEPS 1" in source
    assert (
        "gBarHistoryDelayedRecoverySweepsRemaining=\n"
        "      SCALP_BAR_HISTORY_DELAYED_RECOVERY_SWEEPS;"
    ) in init
    assert (
        "gBarHistoryDelayedRecoverySweepsRemaining =\n"
        "      SCALP_BAR_HISTORY_DELAYED_RECOVERY_SWEEPS;"
    ) in prime
    assert (
        "!force && gBarHistoryDelayedRecoverySweepsRemaining > 0"
    ) in sync
    assert "SCALP_BAR_HISTORY_DELAYED_RECOVERY_SECS 30" in source
    assert "delayedRecoveryReady && !minuteEdgeDue" in sync
    assert sync.index("bool minuteEdgeDue") < sync.index("bool delayedRecoverySweep")
    assert "delayedRecoverySweep || coldBootstrapDue || coldRetryDue" in sync
    assert "if(recoveryComplete)" in sync
    assert "gBarHistoryDelayedRecoverySweepsRemaining = 0;" in sync


def test_cold_history_recovery_is_cursor_bounded_without_all_scope_prefetch() -> None:
    source = _source()
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]

    assert "prefetchBroker" not in sync
    assert "SymbolSelect(" not in sync
    assert "gBarHistoryRecoveryUploadCursor" in sync
    assert "SCALP_BAR_HISTORY_FULL_UPLOADS_PER_PASS" in sync
    assert "bool historySent = SendBarHistoryForSymbol(" in sync
    assert "ForgetBarHistoryBootstrap(logicalSym);" in sync
    assert "if(bootstrap) {\n            ForgetBarHistoryBootstrap(logicalSym);" in sync
    assert "bar history retry pending symbols=" in sync


def test_terminal_and_bridge_restart_invalidate_only_the_bootstrap_cache() -> None:
    source = _source()
    init = source.split("int OnInit", 1)[1].split("void RemoveDashboard", 1)[0]
    handshake = source.split("bool VerifyBridgeHandshake", 1)[1].split(
        "int OnInit", 1
    )[0]
    ticks = source.split("void broadcastTick", 1)[1].split("void OnTimer", 1)[0]

    assert "ArrayResize(gBarHistoryBootstrappedSymbols,0);" in init
    assert 'ResetBarHistoryBootstrap("handshake_unavailable")' in handshake
    assert 'ResetBarHistoryBootstrap("tick_transport_unavailable")' in ticks
    assert "tickStatusCode <= 0 || tickStatusCode >= 500" in ticks


def test_broker_specs_refresh_inside_production_contract_freshness_window() -> None:
    source = _source()
    maintenance = source.split("bool ServiceOneAuxiliaryMaintenance", 1)[1].split(
        "void reportBridgeStatus", 1
    )[0]

    assert "BROKER_SPEC_REPORT_INTERVAL_SECS 15" in source
    assert (
        "gLastBrokerSpecsReport + BROKER_SPEC_REPORT_INTERVAL_SECS"
        in maintenance
    )
    assert "reportSymbolSpecs();" in maintenance
    assert "gLastBrokerSpecsReport = now;" in maintenance
    assert "gLastBrokerSpecsReport + 59" not in maintenance


def test_heartbeat_is_throttled_without_throttling_command_polling() -> None:
    source = _source()
    init = source.split("int OnInit", 1)[1].split("void RemoveDashboard", 1)[0]
    scheduler = source.split("void MaybeSendHeartbeat", 1)[1].split(
        "void reportBridgeStatus", 1
    )[0]
    timer = source.split("void OnTimer", 1)[1].split(
        "bool IsValidOwnerToken", 1
    )[0]

    assert "BRIDGE_HEARTBEAT_INTERVAL_SECS 5" in source
    assert "MaybeSendHeartbeat(true);" in init
    assert "heartbeat();" not in init
    assert "GetTickCount()" in scheduler
    assert "BRIDGE_HEARTBEAT_INTERVAL_SECS * 1000" in scheduler
    assert "MaybeSendHeartbeat(false);" in scheduler
    assert "heartbeat();" not in timer
    assert "EventSetMillisecondTimer(BRIDGE_EDGE_TIMER_INTERVAL_MS)" in init
    assert "HttpGET(pollUrl, gBridgeCommandToken)" in timer
    assert "BROKER_SPEC_REPORT_INTERVAL_SECS 15" in source
    assert "gLastBrokerSpecsReport + BROKER_SPEC_REPORT_INTERVAL_SECS" in scheduler


def test_bar_history_symbol_cache_is_identity_bound_and_prepared_off_edge() -> None:
    source = _source()
    cache = source.split("void ClearBarHistorySymbolCache", 1)[1].split(
        "int BarHistoryEmissionIndex", 1
    )[0]
    refresh = cache.split("bool RefreshBarHistorySymbolCache", 1)[1].split(
        "bool EnsureBarHistorySymbolCache", 1
    )[0]
    ensure = cache.split("bool EnsureBarHistorySymbolCache", 1)[1]
    fast_validity = cache.split("bool BarHistorySymbolCacheFastValid", 1)[1].split(
        "bool BarHistorySymbolCacheValid", 1
    )[0]
    validity = cache.split("bool BarHistorySymbolCacheValid", 1)[1].split(
        "bool RefreshBarHistorySymbolCache", 1
    )[0]
    init = source.split("int OnInit", 1)[1].split("void RemoveDashboard", 1)[0]
    timer = source.split("void OnTimer", 1)[1].split(
        "bool IsValidOwnerToken", 1
    )[0]

    for identity_component in (
        "CurrentMarketDataSymbolCacheIdentity()",
        "TERMINAL_DATA_PATH",
        "gBridgeProducerInstanceId",
        "BarHistorySymbolsCsv",
    ):
        assert identity_component in cache
    for cached_array in (
        "gBarHistoryCacheRequestedSymbols",
        "gBarHistoryCacheLogicalSymbols",
        "gBarHistoryCacheBrokerSymbols",
    ):
        assert f"count != ArraySize({cached_array})" in validity
    assert "CurrentBarHistorySymbolCacheIdentity()" in fast_validity
    assert "gBarHistorySymbolCacheIdentity != currentIdentity" in fast_validity
    assert "BarHistorySymbols(" not in fast_validity
    assert "duplicate_mapping_index_" not in fast_validity
    assert "BarHistorySymbolCacheFastValid(reason)" in validity
    assert "requested != cachedRequested" in validity
    assert "duplicate_mapping_index_" in validity
    assert "if(BarHistoryMinuteEdgeGuardActive()) return false;" in refresh
    assert "ResolveBrokerSymbolStatus(" not in refresh
    assert "MarketDataSymbolCacheShapeValid()" in refresh
    assert "count != gMarketDataStrategySymbolCount" in refresh
    assert "logical != cachedLogical" in refresh
    assert "gMarketDataBrokerSymbols[i]" in refresh
    assert "gBarHistorySymbolCacheRefreshRequested = true;" in source
    assert "identityAfter != identityBefore" in refresh
    assert "BarHistorySymbolCacheValid(reason)" in ensure

    init_market_cache = init.index("RefreshMarketDataSymbolCache(true);")
    init_bar_cache = init.index("EnsureBarHistorySymbolCache();")
    assert init_market_cache < init_bar_cache < init.index("PrimeBarHistoryColdLoad();")
    edge_guard = timer.index("bool edgeGuardActive = BarHistoryMinuteEdgeGuardActive();")
    timer_cache = timer.index("if(!edgeGuardActive) {")
    assert "EnsureBarHistorySymbolCache();" in timer[timer_cache:]
    assert edge_guard < timer_cache


def test_minute_edge_uses_cached_mapping_without_broker_catalogue_scans() -> None:
    source = _source()
    readiness = source.split("bool ExpectedFinalizedM1Ready", 1)[1].split(
        "void MixBarHistoryFingerprint", 1
    )[0]
    bootstrap_send = source.split("bool SendBarHistoryForSymbol", 1)[1].split(
        "bool BuildExactFinalizedM1BatchEntry", 1
    )[0]
    builder = source.split("bool BuildExactFinalizedM1BatchEntry", 1)[1].split(
        "bool PublishPendingBarHistoryMinuteEdgeBatch", 1
    )[0]
    publisher = source.split(
        "bool PublishPendingBarHistoryMinuteEdgeBatch", 1
    )[1].split("void MaybeSyncBarHistory", 1)[0]
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]
    prime = source.split("void PrimeBarHistoryColdLoad", 1)[1].split(
        "string BarHistoryUtcMinuteText", 1
    )[0]

    for edge_body in (readiness, bootstrap_send, builder, publisher, sync, prime):
        assert "ResolveBrokerSymbol" not in edge_body
    assert "string brokerSym," in readiness
    assert "string brokerSym," in bootstrap_send
    assert "string brokerSym," in builder
    assert "BarHistorySymbolCacheFastValid(cacheReason)" in sync
    assert "BarHistorySymbolCacheValid(cacheReason)" not in sync
    assert sync.index("BarHistorySymbolCacheFastValid(cacheReason)") < sync.index(
        "ArraySize(gBarHistoryCacheLogicalSymbols)"
    )
    assert "string logicalSym = gBarHistoryCacheLogicalSymbols[i];" in publisher
    assert "string brokerSym = gBarHistoryCacheBrokerSymbols[i];" in publisher
    assert "ExpectedFinalizedM1Ready(\n         logicalSym,\n         brokerSym," in publisher
    assert "BuildExactFinalizedM1BatchEntry(\n         logicalSym,\n         brokerSym," in publisher

    # Catalogue discovery and full-series loading remain off-edge. Readiness
    # refreshes only three cached rows in the bounded probe lane, then
    # serialization proves the exact shift-1 CopyRates row before one batch POST.
    assert "if(!RefreshDirectM1Series(brokerSym))" in readiness
    assert "CopyRates(brokerSym, PERIOD_M1, 0, 3, probe)" in source
    assert "closedBarTime != expectedClosedTime" in readiness
    assert "RefreshDirectM1Series" not in builder
    assert "CopyRates(brokerSym, PERIOD_M1, 1, 1, bars)" in builder
    assert "finalShiftOneTime != requiredClosedTime" in builder
    assert publisher.count(
        "HttpPOST(ApiBase + BarHistoryBatchPath(), payload, gBridgeApiKey)"
    ) == 1
    assert "SendBarHistoryForSymbol" not in publisher
    assert "if(bootstrap) RefreshDirectM1Series(brokerSym);" in bootstrap_send
    assert bootstrap_send.count(
        "HttpPOST(ApiBase + BarHistoryPath(), payload, gBridgeApiKey)"
    ) == 1


def test_minute_edge_round_robin_bounds_terminal_work_and_retires_exact_cycle() -> None:
    source = _source()
    publisher = source.split(
        "bool PublishPendingBarHistoryMinuteEdgeBatch", 1
    )[1].split("void MaybeSyncBarHistory", 1)[0]
    sync = source.split("void MaybeSyncBarHistory", 1)[1].split(
        "uint AckOutboxHash", 1
    )[0]

    exact_scope = 22
    probes_per_pass = 4
    passes_for_scope = (exact_scope + probes_per_pass - 1) // probes_per_pass
    nominal_edge_callbacks = ((4 - 1) + 1) * 1000 // 250

    assert passes_for_scope == 6
    assert passes_for_scope * 250 <= 1_500
    assert nominal_edge_callbacks * probes_per_pass == 64
    assert nominal_edge_callbacks * probes_per_pass >= exact_scope * 2
    assert "SCALP_BAR_HISTORY_EDGE_PROBES_PER_PASS 4" in source
    assert "int probedCount = 0;" in publisher
    assert "probedCount < SCALP_BAR_HISTORY_EDGE_PROBES_PER_PASS" in publisher
    assert "int indicesUntilSweepEnd = count - startIndex;" in publisher
    assert "int i = startIndex + visitedCount;" in publisher
    assert "gBarHistoryEdgeProbeCursor = (startIndex + visitedCount) % count;" in publisher
    assert "gBarHistoryPendingCompletedFlags[i]" in publisher
    assert "gBarHistoryEdgeStagedFlags[i]" in publisher
    assert "StageBarHistoryEdgeEntry(i, batchEntryJson, emittedClosedTime)" in publisher
    assert "gBarHistoryPendingRemaining -= includedCount;" in publisher
    assert "gBarHistoryPendingRemaining == 0" in sync
    assert "ContainsSymbol(gBarHistoryPendingCompletedSymbols" not in publisher
    assert "gBarHistoryIncompleteNoticeBucket != gBarHistoryPendingMinuteBucket" in sync
    assert "gBarHistoryIncompleteNoticeBucket = gBarHistoryPendingMinuteBucket" in sync
    assert "prefetchBroker" not in sync
    assert "SymbolSelect(" not in sync


def test_structured_broker_truth_reports_share_authenticated_source_and_trade_flag() -> None:
    source = _source()
    for start, end in (
        ("void reportBridgeStatus", "void reportSymbolSpecs"),
        ("void reportSymbolSpecs", "void OnTick"),
        ("void EmitPositionsSnapshot", "void SendPositions"),
        ("void EmitClosedTradeReportFromSelection", "void PrimeClosedTradeCursor"),
    ):
        body = source.split(start, 1)[1].split(end, 1)[0]
        assert "CurrentBrokerMarketSourceJsonFields()" in body
        assert "marketSourceFields" in body
        assert "gBridgeHandshakeCompatible" in body
    specs = source.split("void reportSymbolSpecs", 1)[1].split("void OnTick", 1)[0]
    assert "MODE_TRADEALLOWED" in specs
    assert '\\"trade_allowed\\"' in specs
    assert "SymbolInfoDouble(brokerSym, SYMBOL_TRADE_TICK_SIZE)" in specs
    assert "tickSize <= 0.0" in specs
    assert "MarketInfo(brokerSym, MODE_TICKSIZE)" not in specs
    assert '\\"tick_size\\":' in specs
