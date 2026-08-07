#property strict
#include <BridgeUtils.mqh>
#include <BridgeHttp.mqh> // Shared WinInet Logic

// MT4 Bridge EA - WinInet Version
// Configure the broker account in MT4 itself; do not record live identifiers here.

// Bridge wire-protocol version this EA was compiled against. Keep in sync with
// fx-quant-stack/src/fxstack/api/wire.py::BRIDGE_PROTOCOL_VERSION. A mismatch
// fences command polling and authenticated market-data production.
#define EA_EXPECTED_PROTOCOL_VERSION "v3.0.0"
#define ACK_OUTBOX_MAX_PENDING 512
#define ACK_OUTBOX_REPLAY_PER_TIMER 4
#define ACK_OUTBOX_REPLAY_ON_STARTUP 4
#define ACK_OUTBOX_RECOVER_PER_PASS 8
#define ACK_OUTBOX_UNPERSISTED_MAX 8
// MTVCLC consumes exactly 241 completed M1 bars. Keep the bridge proof and
// bootstrap payload on that exact contract instead of scanning a larger,
// strategy-irrelevant window.
#define SCALP_BOOTSTRAP_COMPLETED_DEPTH 241
// A healthy one-minute cadence creates exactly one new closed M1 bar. Send only
// that proven shift-1 row: including an overlap row would let an immutable
// conflict reject the whole atomic batch and suppress the new bar. Missed data,
// terminal restart, transport failure, or API restart uses the separate bounded
// full-recovery lane below.
#define SCALP_BAR_HISTORY_INCREMENTAL_DEPTH 1
#define SCALP_BAR_HISTORY_REMOTE_MIN_DIRECT_BARS SCALP_BOOTSTRAP_COMPLETED_DEPTH
#define SCALP_BAR_HISTORY_COVERAGE_SCHEMA "fxstack.direct_mt4_bar_coverage.v1"
#define SCALP_BAR_HISTORY_RESYNC_SECS 60
// Drive steady-state publication from the wall-clock minute edge rather than
// the EA's startup second. Off-chart MT4 series can lag a received quote, so
// start at +1s and prove each symbol's shift-1 minute before publication. Retry
// only unproved symbols through +4s; later retries are data reconciliation and
// cannot extend the strategy's separately enforced five-second entry window.
#define SCALP_BAR_HISTORY_MINUTE_EDGE_DELAY_SECS 1
#define SCALP_BAR_HISTORY_MINUTE_EDGE_DEADLINE_SECS 4
#define SCALP_BAR_HISTORY_LATE_RETRY_SECS 10
#define SCALP_BAR_HISTORY_DELAYED_RECOVERY_SECS 30
#define SCALP_BAR_HISTORY_STABILITY_SECS 5
#define SCALP_BAR_HISTORY_STABILITY_CHECK_SECS 5
#define SCALP_BAR_HISTORY_STABILITY_PINNED_RECHECKS_MAX 2
#define SCALP_BAR_HISTORY_DIAGNOSTIC_INTERVAL_SECS 60
#define SCALP_BAR_HISTORY_MAINTENANCE_START_SECS 7
#define SCALP_BAR_HISTORY_MAINTENANCE_CUTOFF_SECS 45
#define SCALP_BAR_HISTORY_FULL_UPLOADS_PER_PASS 1
#define SCALP_BAR_HISTORY_EDGE_PROBES_PER_PASS 4
// CopyRates starts asynchronous construction for an off-chart series and can
// leave shift 0/1 pinned well past the five-second scalp window.  Keep one M1
// chart materialized for every configured strategy symbol instead of rotating
// a single recovery chart.  Charts are opened gradually outside the edge guard
// and remain open until this EA deinitializes.
#define SCALP_BAR_HISTORY_RECOVERY_CHARTS_MAX 22
// MT4 loads off-chart history asynchronously.  The immediate OnInit bootstrap
// can therefore contain a temporary hole even though the requested history is
// available a few seconds later.  Perform exactly one delayed full refresh,
// then return to the single shift-1 steady-state cadence. Keeping this bounded
// avoids repeatedly uploading full history for legitimately sparse markets.
#define SCALP_BAR_HISTORY_DELAYED_RECOVERY_SWEEPS 1

enum BarHistoryObservationOutcome {
   BAR_HISTORY_OBSERVATION_INCOHERENT = 0,
   BAR_HISTORY_OBSERVATION_WAIT_STABLE = 1,
   BAR_HISTORY_OBSERVATION_STABLE = 2
};
enum RemoteBarHistoryCoverageOutcome {
   BAR_HISTORY_REMOTE_COVERAGE_UNAVAILABLE = 0,
   BAR_HISTORY_REMOTE_COVERAGE_MISSING = 1,
   BAR_HISTORY_REMOTE_COVERAGE_RETAINED_STALE = 2,
   BAR_HISTORY_REMOTE_COVERAGE_EXACT = 3
};
#define MARKET_DATA_SYMBOL_CACHE_REFRESH_SECS 900
#define MARKET_DATA_SYMBOL_CACHE_RETRY_SECS 5
#define BRIDGE_EDGE_TIMER_INTERVAL_MS 250
#define BRIDGE_MAIN_TIMER_INTERVAL_MS 1000
#define BRIDGE_HEARTBEAT_INTERVAL_SECS 5
#define BROKER_SPEC_REPORT_INTERVAL_SECS 15
#define PRODUCER_INSTANCE_ID_FILE "bridge_producer_instance_id.txt"
#define BROKER_ACCOUNT_SCOPE_SCHEMA "fxstack_mt4_account_scope_djb2_xor32_v1"
#define BROKER_ACCOUNT_SCOPE_VERSION 1
#define POSITIONS_SNAPSHOT_SCHEMA "fxstack_mt4_positions_snapshot_v2"
#define TICKET_OWNER_CONTRACT "ticket_owner_v1"
#define LEGACY_ELBRIDGE_CONTRACT "legacy_elbridge_v1"
#define LEGACY_ORDER_COMMENT "ELBridge"
#define OWNER_TOKEN_MAX_LENGTH 31
#define PRODUCTION_SCALPER_ENTRY_INTENT "PRODUCTION_SCALPER_ENTRY"
#define PRODUCTION_SCALP_AUTHORITY_SCHEMA "fxstack_production_scalp_authority_v3"
#define DIRECT_RUNTIME_SCALP_AUTHORITY_SCHEMA "fxstack_production_scalp_authority_v2"
#define DIRECT_RUNTIME_SCALP_STRATEGY_ID "scalp_dislocation"
#define PRODUCTION_SCALP_SCOPE_VERSION "fxstack.ig_mt4.scalp_scope.v3"
#define MTVCLC_STRATEGY_ID "ig_mt4_tick_volume_close_location_continuation"
#define MTVCLC_STRATEGY_VERSION "mtvclc.v1"
#define MTVCLC_CONFIG_ID "mtvclc_v1_vq90_cl80_b1k_h30_t4k_s8k"
#define SCALP_BROKER_ENTRY_PLAN_SCHEMA "fxstack.production_scalp_broker_entry_plan.v2"
#define BROKER_CONTRACT_STATE_SCHEMA "fxstack_ig_mt4_contract_state_v1"
#define BROKER_ORDER_ACTUALS_SCHEMA "fxstack.mt4_order_actuals.v1"
#define IG_MT4_VENUE_ID "ig_mt4"
#define PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS 20
#define PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS 5
#define ENTRY_SEND_ATTEMPTS 3
#define SIGNAL_OUTCOME_DIRECTORY "FXStack\\SignalOutcomes"

// AGENT HANDSHAKE: Python's production-scalper risk proof is serialized as a
// complete instant-market envelope.  Every field has a separate presence bit
// so an omitted value cannot silently become MQL's numeric/string zero.
struct MarketEntryEnvelope {
   bool has_any;
   bool execution_type_provided;
   string execution_type;
   bool pending_orders_forbidden_provided;
   bool pending_orders_forbidden;
   bool entry_deadline_epoch_provided;
   int entry_deadline_epoch;
   bool plan_schema_provided;
   string plan_schema;
   bool entry_quote_price_provided;
   double entry_quote_price;
   bool entry_price_provided;
   double entry_price;
   bool worst_fill_price_provided;
   double worst_fill_price;
   bool max_slippage_points_provided;
   int max_slippage_points;
   bool protection_cushion_points_provided;
   int protection_cushion_points;
   bool contract_schema_provided;
   string contract_schema;
   bool venue_id_provided;
   string venue_id;
   bool expected_symbol_provided;
   string expected_symbol;
   bool broker_symbol_provided;
   string broker_symbol;
   bool account_currency_provided;
   string account_currency;
   bool binding_sha256_provided;
   string binding_sha256;
   bool lot_size_provided;
   double lot_size;
   bool min_lot_provided;
   double min_lot;
   bool lot_step_provided;
   double lot_step;
   bool max_lot_provided;
   double max_lot;
   bool point_provided;
   double point;
   bool tick_size_provided;
   double tick_size;
   bool margin_required_provided;
   double margin_required;
   bool margin_utilization_cap_provided;
   double margin_utilization_cap;
   bool stop_level_points_provided;
   double stop_level_points;
   bool freeze_level_points_provided;
   double freeze_level_points;
   bool digits_provided;
   int digits;
   bool trade_allowed_provided;
   bool trade_allowed;
};

// The public runtime verifier and row-locked Python authority establish this
// signed-v3 identity. The EA cannot re-run Ed25519 verification, but it must
// still refuse an immediate BUY/SELL if the authenticated command loses or
// downgrades any immutable authority field before reaching MT4.
struct ScalpStrategyAuthorityEnvelope {
   string schema;
   string admission_mode;
   string account_mode;
   string generation_id;
   string strategy_id;
   string strategy_version;
   string engine_sha256;
   string config_id;
   string config_sha256;
   string validation_evidence_sha256;
   string runtime_release_certificate_sha256;
   string runtime_release_signing_key_id;
   string research_evidence_sha256;
   string research_evidence_signing_key_id;
   string registry_generation_id;
   bool registry_revision_provided;
   int registry_revision;
   string registry_sha256;
   string qualification_surface_sha256;
   string cost_mapping_sha256;
   string execution_contract_sha256;
   bool validation_expires_at_epoch_provided;
   int validation_expires_at_epoch;
   string venue_id;
   string scope_version;
   string binding_sha256;
   string runtime_boot_id;
   bool authority_revision_provided;
   int authority_revision;
};

// Broker facts selected before/after one strict ticket-management mutation.
// `lots` is the actually closed amount for CLOSE/CLOSE_PARTIAL and the current
// broker lots for MODIFY_SL.
struct StrictManagementActuals {
   string cmd;
   string logical_symbol;
   string broker_symbol;
   string side;
   string execution_type;
   int target_ticket;
   int magic;
   string owner_token;
   string order_comment;
   double lots;
   double remaining_lots;
   double open_price;
   double sl_price;
   double tp_price;
   datetime close_time;
};

input string ApiBase = "http://127.0.0.1:58710";
input string ApiKey = "";
input string CommandToken = "";
input string ConsumerIdentity = "";
input string TerminalLeaseScope = "";
input string CredentialGenerationId = "";
input int    PollMs  = 1000;
input int    SlipPts = 20;
input int    Magic   = 246810;
input string SymbolsCsv = "EURUSD,USDJPY,AUDUSD,GBPUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD,AUDJPY,CADJPY,CHFJPY,EURAUD,EURCAD,EURCHF,GBPCAD,GBPCHF,GBPJPY,BTCUSD,ETHUSD,AUDCAD,NZDJPY";
// Legacy compatibility toggle only. Approved command lots are never replaced
// with a mini/minimum lot; every entry must already be exactly executable.
input bool   UseIGMinis = true;
input bool   VerboseBridgeLog = false;
input bool   AllowCycleCloseAll = false;
input int    SignalDedupTTLSeconds = 3600;
input int    SignalDedupMax = 256;
input int    ClosedTradeReplayCount = 24;
input int    ClosedTradeReportIntervalSecs = 10;
// Retained for deployment-profile compatibility: <=0 disables publication;
// every positive value is constrained to the exact strategy depth above.
input int    BarHistoryDepth = SCALP_BOOTSTRAP_COMPLETED_DEPTH;
input int    BarHistoryBatchSize = 100;
input string BarHistorySymbolsCsv = "";

string DefaultSymbolsCsv() {
   return "EURUSD,USDJPY,AUDUSD,GBPUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD,AUDJPY,CADJPY,CHFJPY,EURAUD,EURCAD,EURCHF,GBPCAD,GBPCHF,GBPJPY,BTCUSD,ETHUSD,AUDCAD,NZDJPY";
}

double gCycleStartEq = 0.0;
double gCycleTargetCash = 0.0;
bool   gCycleActive = false;
// Basket-TP target as a fraction of cycle-start equity. Authoritative value
// lives in Python (settings.basket_tp_pct) and is pushed through the bridge's
// /v2/handshake response. -1.0 means "not yet known; fall back to hardcoded
// 0.01 at the next cycle start." Updated by VerifyBridgeHandshake().
double gBasketTpPctFromBridge = -1.0;
#define EA_FALLBACK_BASKET_TP_PCT 0.01
string gSeenSignalIds[];
datetime gSeenSignalTs[];
int gSeenCount = 0;
datetime gLastAuthWarnTs = 0;
datetime gLastClosedTradeTime = 0;
int      gLastClosedTradeTicket = -1;
bool     gClosedTradeReplayDone = false;
string   gAckUnpersistedPayloads[];
int      gAckUnpersistedCount = 0;
int      gAckOutboxSequence = 0;
int      gAckReplayCursor = 0;
bool     gAckOutboxBlocked = false;
bool     gSignalOutcomeJournalBlocked = false;
bool     gSignalOutcomeJournalReady = false;
datetime gLastAckOutboxWarnTs = 0;
bool     gAckScopePinned = false;
int      gAckScopeAccountNumber = 0;
string   gAckScopeAccountServer = "";
string   gAckScopeApiBase = "";
int      gAckScopeMagic = 0;
string   gAckScopeTerminalDataPath = "";
string   gAckScopeTerminalToken = "";
string   gAckScopeProducerInstanceId = "";
string   gAckScopeDirectory = "";
string   gBridgeApiKey = "";
string   gBridgeCommandToken = "";
string   gBridgeConsumerIdentity = "";
string   gBridgeProducerInstanceId = "";
string   gBridgeTerminalLeaseScope = "";
string   gBridgeCredentialGenerationId = "";
datetime gLastBarHistoryAttempt = 0;
int      gLastBarHistoryMinuteBucket = -1;
datetime gBarHistoryInitialBootstrapAt = 0;
string   gBarHistoryBootstrappedSymbols[];
string   gBarHistoryEmissionSymbols[];
datetime gBarHistoryLastEmittedClosedTimes[];
int      gBarHistoryPendingMinuteBucket = -1;
datetime gBarHistoryPendingNextLateRetryAt = 0;
bool     gBarHistoryPendingCompletedFlags[];
int      gBarHistoryPendingRemaining = 0;
int      gBarHistoryEdgeProbeCursor = 0;
bool     gBarHistoryEdgeStagedFlags[];
string   gBarHistoryEdgeStagedEntries[];
datetime gBarHistoryEdgeStagedClosedTimes[];
int      gBarHistoryEdgeStagedCount = 0;
int      gBarHistoryIncompleteNoticeBucket = -1;
bool     gBarHistoryRemoteProbePending = false;
bool     gBarHistoryFullRecoveryRequested = false;
bool     gBarHistoryRecoveryTransportAbort = false;
int      gBarHistoryRecoveryUploadCursor = 0;
string   gBarHistoryStabilitySymbols[];
string   gBarHistoryStabilityFingerprints[];
datetime gBarHistoryStabilityObservedAt[];
datetime gBarHistoryStabilityNewestClosedTimes[];
int      gBarHistoryStabilityRecheckCounts[];
datetime gBarHistoryStabilityNextCheckAt = 0;
string   gBarHistoryDiagnosticSymbols[];
datetime gBarHistoryDiagnosticAt[];
string   gBarHistoryRecoveryChartSymbols[];
long     gBarHistoryRecoveryChartIds[];
int      gBarHistoryDelayedRecoverySweepsRemaining = 0;
int      gBarHistoryMaterializationCursor = 0;
bool     gBarHistoryMaterializationReady = false;
string   gBarHistoryCacheRequestedSymbols[];
string   gBarHistoryCacheLogicalSymbols[];
string   gBarHistoryCacheBrokerSymbols[];
bool     gBarHistorySymbolCacheReady = false;
bool     gBarHistorySymbolCacheRefreshRequested = false;
string   gBarHistorySymbolCacheIdentity = "";
datetime gLastBarHistorySymbolCacheAttempt = 0;
datetime gLastBarHistorySymbolCacheWarn = 0;
bool     gBridgeHandshakeCompatible = false;
datetime gLastBridgeHandshakeAttempt = 0;
string   gMarketDataLogicalSymbols[];
string   gMarketDataBrokerSymbols[];
string   gMarketDataMappingReasons[];
string   gMarketDataMappingKinds[];
int      gMarketDataMappingCandidateCounts[];
bool     gMarketDataMappingAmbiguous[];
int      gMarketDataStrategySymbolCount = 0;
bool     gMarketDataSymbolCacheReady = false;
string   gMarketDataSymbolCacheIdentity = "";
string   gMarketDataSymbolCacheLastAttemptIdentity = "";
datetime gLastMarketDataSymbolCacheAttempt = 0;
datetime gLastMarketDataSymbolCacheRefresh = 0;
datetime gLastMarketDataSymbolCacheWarn = 0;
bool     gHeartbeatAttempted = false;
uint     gLastHeartbeatAttemptMs = 0;
bool     gMainTimerBodyAttempted = false;
uint     gLastMainTimerBodyMs = 0;
int      gAuxMaintenanceCursor = 0;
datetime gLastBridgeStatusReport = 0;
datetime gLastBrokerSpecsReport = 0;
datetime gLastDashboardRefresh = 0;
datetime gLastPositionsSnapshot = 0;
datetime gLastClosedTradeReport = 0;
string   gDashboardLastText = "";
long     gDashboardLastChartWidth = -1;
long     gDashboardLastChartHeight = -1;
int      gDashboardRowsShown = -1;

string LoadBridgeSecret(string configuredValue, string fileName, string label) {
   string configured = StringTrim(configuredValue);
   if(StringLen(configured) > 0) return configured;

   ResetLastError();
   int handle = FileOpen(fileName, FILE_READ|FILE_TXT|FILE_ANSI);
   if(handle == INVALID_HANDLE) {
      Print("[BRIDGE] ", fileName, " unavailable in MQL4/Files; ", label, " will fail closed (err=", GetLastError(), ")");
      return "";
   }
   string loaded = StringTrim(FileReadString(handle));
   FileClose(handle);
   if(StringLen(loaded) <= 0) {
      Print("[BRIDGE] ", fileName, " is empty; ", label, " will fail closed");
      return "";
   }
   return loaded;
}

string LoadBridgeApiKey() {
   return LoadBridgeSecret(ApiKey, "bridge_api_key.txt", "bridge auth");
}

void WarnAuthFailure(string op, int statusCode) {
   if(statusCode != 401) return;
   datetime now = TimeCurrent();
   if((now - gLastAuthWarnTs) < 5) return;
   gLastAuthWarnTs = now;
   string mode = LastBridgeTransportMode();
   Print("[BRIDGE] AUTH 401 on ", op, " (transport=", mode, "). Configure EA ApiKey to match FXSTACK_BRIDGE_API_KEY.");
   UpdateDashboard("AUTH ERROR 401|Set EA ApiKey to match bridge key");
}

double PipSizeForSymbol(string sym) {
   int dg = (int)MarketInfo(sym, MODE_DIGITS);
   if(dg == 2 || dg == 3) return 0.01;
   return 0.0001;
}

int SymbolsFromCsv(string csv, string &out[]) {
   string clean = csv;
   StringReplace(clean, ";", ",");
   string raw[];
   int n = StringSplit(clean, ',', raw);
   int count = 0;
   ArrayResize(out, 0);
   for(int i = 0; i < n; i++) {
      string sym = ToUpperSafe(StringTrim(raw[i]));
      if(StringLen(sym) <= 0) continue;
      ArrayResize(out, count + 1);
      out[count] = sym;
      count++;
   }
   return count;
}

bool ContainsSymbol(string &items[], string sym) {
   string target = ToUpperSafe(StringTrim(sym));
   if(StringLen(target) <= 0) return false;
   int n = ArraySize(items);
   for(int i = 0; i < n; i++) {
      if(ToUpperSafe(StringTrim(items[i])) == target) return true;
   }
   return false;
}

int EffectiveSymbols(string &out[]) {
   string defaults[];
   string configured[];
   int nDefaults = SymbolsFromCsv(DefaultSymbolsCsv(), defaults);
   int nConfigured = SymbolsFromCsv(SymbolsCsv, configured);
   int count = 0;
   ArrayResize(out, 0);
   for(int i = 0; i < nDefaults; i++) {
      string sym = ToUpperSafe(StringTrim(defaults[i]));
      if(StringLen(sym) <= 0 || ContainsSymbol(out, sym)) continue;
      ArrayResize(out, count + 1);
      out[count] = sym;
      count++;
   }
   for(int j = 0; j < nConfigured; j++) {
      string extra = ToUpperSafe(StringTrim(configured[j]));
      if(StringLen(extra) <= 0 || ContainsSymbol(out, extra)) continue;
      ArrayResize(out, count + 1);
      out[count] = extra;
      count++;
   }
   return count;
}

string NormalizePairToken(string sym) {
   string trimmed = ToUpperSafe(StringTrim(sym));
   if(StringLen(trimmed) <= 0) return trimmed;
   string configured[];
   int n = EffectiveSymbols(configured);
   for(int i = 0; i < n; i++) {
      string root = ToUpperSafe(StringTrim(configured[i]));
      if(StringLen(root) <= 0) continue;
      if(trimmed == root) return root;
      if(StringFind(trimmed, root, 0) >= 0) return root;
   }
   return trimmed;
}

bool SymbolsMatch(string left, string right) {
   string lhs = NormalizePairToken(left);
   string rhs = NormalizePairToken(right);
   if(StringLen(lhs) <= 0 || StringLen(rhs) <= 0) return false;
   return lhs == rhs;
}

bool AppendUniqueBrokerSymbol(string &items[], string candidate) {
   string clean = StringTrim(candidate);
   if(StringLen(clean) <= 0 || ContainsSymbol(items, clean)) return false;
   int count = ArraySize(items);
   ArrayResize(items, count + 1);
   items[count] = clean;
   return true;
}

void CollectBrokerSymbolCandidate(
   string candidate,
   bool isSelected,
   string requestedUpper,
   string logical,
   string &requestedExact[],
   string &logicalExact[],
   string &partialMatches[],
   string &selectedPartialMatches[]
) {
   string clean = StringTrim(candidate);
   string upper = ToUpperSafe(clean);
   if(StringLen(upper) <= 0) return;

   if(upper == requestedUpper) {
      AppendUniqueBrokerSymbol(requestedExact, clean);
      return;
   }
   if(upper == logical) {
      AppendUniqueBrokerSymbol(logicalExact, clean);
      return;
   }
   if(StringLen(logical) <= 0 || StringFind(upper, logical, 0) < 0) return;

   AppendUniqueBrokerSymbol(partialMatches, clean);
   if(isSelected) AppendUniqueBrokerSymbol(selectedPartialMatches, clean);
}

bool ConfirmBrokerSymbolCandidate(
   string candidate,
   string mappingKind,
   int candidateCount,
   string &resolved,
   string &reason,
   string &resolvedMappingKind,
   int &resolvedCandidateCount
) {
   resolved = candidate;
   resolvedMappingKind = mappingKind;
   resolvedCandidateCount = candidateCount;
   if(!SymbolSelect(candidate, true)) {
      reason = "symbol_select_failed";
      return false;
   }
   reason = "ok";
   return true;
}

bool ResolveBrokerSymbolStatus(
   string requested,
   string &resolved,
   string &reason,
   string &mappingKind,
   int &candidateCount,
   bool &ambiguous
) {
   string trimmed = StringTrim(requested);
   string requestedUpper = ToUpperSafe(trimmed);
   string logical = NormalizePairToken(trimmed);
   resolved = "";
   reason = "";
   mappingKind = "none";
   candidateCount = 0;
   ambiguous = false;
   if(StringLen(trimmed) <= 0 || StringLen(logical) <= 0) {
      reason = "empty_symbol";
      return false;
   }

   string requestedExact[];
   string logicalExact[];
   string partialMatches[];
   string selectedPartialMatches[];
   ArrayResize(requestedExact, 0);
   ArrayResize(logicalExact, 0);
   ArrayResize(partialMatches, 0);
   ArrayResize(selectedPartialMatches, 0);

   int total = SymbolsTotal(false);
   for(int i = 0; i < total; i++) {
      CollectBrokerSymbolCandidate(
         SymbolName(i, false), false, requestedUpper, logical,
         requestedExact, logicalExact, partialMatches, selectedPartialMatches
      );
   }

   total = SymbolsTotal(true);
   for(int j = 0; j < total; j++) {
      CollectBrokerSymbolCandidate(
         SymbolName(j, true), true, requestedUpper, logical,
         requestedExact, logicalExact, partialMatches, selectedPartialMatches
      );
   }

   int requestedExactCount = ArraySize(requestedExact);
   if(requestedExactCount == 1) {
      return ConfirmBrokerSymbolCandidate(
         requestedExact[0], "requested_exact", requestedExactCount,
         resolved, reason, mappingKind, candidateCount
      );
   }
   if(requestedExactCount > 1) {
      ambiguous = true;
      reason = "ambiguous_requested_exact_match";
      mappingKind = "requested_exact";
      candidateCount = requestedExactCount;
      return false;
   }

   int logicalExactCount = ArraySize(logicalExact);
   if(logicalExactCount == 1) {
      return ConfirmBrokerSymbolCandidate(
         logicalExact[0], "logical_exact", logicalExactCount,
         resolved, reason, mappingKind, candidateCount
      );
   }
   if(logicalExactCount > 1) {
      ambiguous = true;
      reason = "ambiguous_logical_exact_match";
      mappingKind = "logical_exact";
      candidateCount = logicalExactCount;
      return false;
   }

   int selectedPartialCount = ArraySize(selectedPartialMatches);
   if(selectedPartialCount == 1) {
      return ConfirmBrokerSymbolCandidate(
         selectedPartialMatches[0], "selected_partial", selectedPartialCount,
         resolved, reason, mappingKind, candidateCount
      );
   }
   if(selectedPartialCount > 1) {
      ambiguous = true;
      reason = "ambiguous_selected_partial_match";
      mappingKind = "selected_partial";
      candidateCount = selectedPartialCount;
      return false;
   }

   int partialCount = ArraySize(partialMatches);
   if(partialCount == 1) {
      return ConfirmBrokerSymbolCandidate(
         partialMatches[0], "unique_partial", partialCount,
         resolved, reason, mappingKind, candidateCount
      );
   }
   if(partialCount > 1) {
      ambiguous = true;
      reason = "ambiguous_partial_match";
      mappingKind = "partial";
      candidateCount = partialCount;
      return false;
   }

   reason = "symbol_not_found";
   return false;
}

bool ResolveBrokerSymbolEx(string requested, string &resolved) {
   string reason = "";
   string mappingKind = "";
   int candidateCount = 0;
   bool ambiguous = false;
   return ResolveBrokerSymbolStatus(
      requested, resolved, reason, mappingKind, candidateCount, ambiguous
   );
}

string ResolveBrokerSymbol(string requested) {
   string resolved = "";
   if(!ResolveBrokerSymbolEx(requested, resolved)) return "";
   return resolved;
}

int EffectiveMarketDataSymbols(string &out[]) {
   // Execution remains the exact configured strategy universe. Quotes may
   // additionally include one broker-resolvable direct/inverse conversion
   // cross per quote currency so Python can value stop risk in AccountCurrency.
   int count = EffectiveSymbols(out);
   string account = ToUpperSafe(StringTrim(AccountCurrency()));
   if(StringLen(account) != 3) return count;

   int strategyCount = count;
   for(int i = 0; i < strategyCount; i++) {
      string logical = NormalizePairToken(out[i]);
      if(StringLen(logical) < 6) continue;
      string quote = StringSubstr(logical, 3, 3);
      if(StringLen(quote) != 3 || quote == account) continue;

      string direct = account + quote;
      string inverse = quote + account;
      if(ContainsSymbol(out, direct) || ContainsSymbol(out, inverse)) continue;

      string resolved = "";
      string conversion = "";
      if(ResolveBrokerSymbolEx(direct, resolved)) conversion = direct;
      else if(ResolveBrokerSymbolEx(inverse, resolved)) conversion = inverse;
      if(StringLen(conversion) <= 0) continue;

      ArrayResize(out, count + 1);
      out[count] = conversion;
      count++;
   }
   return count;
}

void ClearMarketDataSymbolCache() {
   ArrayResize(gMarketDataLogicalSymbols, 0);
   ArrayResize(gMarketDataBrokerSymbols, 0);
   ArrayResize(gMarketDataMappingReasons, 0);
   ArrayResize(gMarketDataMappingKinds, 0);
   ArrayResize(gMarketDataMappingCandidateCounts, 0);
   ArrayResize(gMarketDataMappingAmbiguous, 0);
   gMarketDataStrategySymbolCount = 0;
   gMarketDataSymbolCacheReady = false;
   gMarketDataSymbolCacheIdentity = "";
   gLastMarketDataSymbolCacheRefresh = 0;
   gBarHistorySymbolCacheRefreshRequested = true;
}

bool MarketDataSymbolCacheShapeValid() {
   int count = ArraySize(gMarketDataLogicalSymbols);
   if(
      !gMarketDataSymbolCacheReady || count <= 0 ||
      gMarketDataStrategySymbolCount <= 0 ||
      gMarketDataStrategySymbolCount > count ||
      count != ArraySize(gMarketDataBrokerSymbols) ||
      count != ArraySize(gMarketDataMappingReasons) ||
      count != ArraySize(gMarketDataMappingKinds) ||
      count != ArraySize(gMarketDataMappingCandidateCounts) ||
      count != ArraySize(gMarketDataMappingAmbiguous)
   ) return false;
   for(int i = 0; i < count; i++) {
      if(
         StringLen(gMarketDataLogicalSymbols[i]) <= 0 ||
         StringLen(gMarketDataBrokerSymbols[i]) <= 0 ||
         StringLen(gMarketDataMappingReasons[i]) <= 0 ||
         StringLen(gMarketDataMappingKinds[i]) <= 0 ||
         gMarketDataMappingCandidateCounts[i] <= 0 ||
         gMarketDataMappingAmbiguous[i]
      ) return false;
   }
   return true;
}

void WarnMarketDataSymbolCache(string reason) {
   datetime now = TimeLocal();
   if(
      gLastMarketDataSymbolCacheWarn > 0 && now >= gLastMarketDataSymbolCacheWarn &&
      (now - gLastMarketDataSymbolCacheWarn) < 15
   ) return;
   gLastMarketDataSymbolCacheWarn = now;
   Print("[BRIDGE] market-data symbol cache unavailable; tick publication fenced reason=", reason);
}

string CurrentMarketDataSymbolCacheIdentity() {
   string accountScope = CurrentBrokerAccountScope(Magic);
   string server = StringTrim(AccountServer());
   string company = StringTrim(AccountCompany());
   string currency = ToUpperSafe(StringTrim(AccountCurrency()));
   if(
      AccountNumber() <= 0 || StringLen(accountScope) <= 0 ||
      StringLen(server) <= 0 || StringLen(company) <= 0 ||
      StringLen(currency) != 3
   ) return "";
   // Bind the cached broker mapping to the same non-reversible account scope
   // and broker facts carried by authenticated market-source reports. The
   // configured logical universe remains immutable for this EA instance.
   return accountScope + "|" + server + "|" + company + "|" + currency +
      "|" + IntegerToString(Magic) + "|" + SymbolsCsv;
}

bool RefreshMarketDataSymbolCache(bool force) {
   datetime now = TimeLocal();
   string identityBefore = CurrentMarketDataSymbolCacheIdentity();
   bool newAttemptIdentity = (
      identityBefore != gMarketDataSymbolCacheLastAttemptIdentity
   );
   if(
      !force && !newAttemptIdentity &&
      gLastMarketDataSymbolCacheAttempt > 0 &&
      now >= gLastMarketDataSymbolCacheAttempt &&
      (now - gLastMarketDataSymbolCacheAttempt) < MARKET_DATA_SYMBOL_CACHE_RETRY_SECS
   ) return false;
   gLastMarketDataSymbolCacheAttempt = now;
   gMarketDataSymbolCacheLastAttemptIdentity = identityBefore;
   if(StringLen(identityBefore) <= 0) {
      ClearMarketDataSymbolCache();
      WarnMarketDataSymbolCache("broker_identity_unavailable");
      return false;
   }

   string strategySymbols[];
   string logicalCandidates[];
   int strategyCount = EffectiveSymbols(strategySymbols);
   int candidateCount = EffectiveMarketDataSymbols(logicalCandidates);
   if(strategyCount <= 0 || candidateCount < strategyCount) {
      ClearMarketDataSymbolCache();
      WarnMarketDataSymbolCache("configured_scope_unavailable");
      return false;
   }
   // EffectiveMarketDataSymbols must retain the configured strategy universe
   // as its ordered prefix. Only named conversion-only extras may follow it.
   for(int scopeIndex = 0; scopeIndex < strategyCount; scopeIndex++) {
      if(
         ToUpperSafe(StringTrim(logicalCandidates[scopeIndex])) !=
         ToUpperSafe(StringTrim(strategySymbols[scopeIndex]))
      ) {
         ClearMarketDataSymbolCache();
         WarnMarketDataSymbolCache("configured_scope_drift");
         return false;
      }
   }

   string resolvedLogical[];
   string resolvedBroker[];
   string resolvedMappingReasons[];
   string resolvedMappingKinds[];
   int resolvedMappingCandidateCounts[];
   bool resolvedMappingAmbiguous[];
   ArrayResize(resolvedLogical, 0);
   ArrayResize(resolvedBroker, 0);
   ArrayResize(resolvedMappingReasons, 0);
   ArrayResize(resolvedMappingKinds, 0);
   ArrayResize(resolvedMappingCandidateCounts, 0);
   ArrayResize(resolvedMappingAmbiguous, 0);
   for(int i = 0; i < candidateCount; i++) {
      string logicalSym = ToUpperSafe(StringTrim(logicalCandidates[i]));
      string brokerSym = "";
      string mappingReason = "";
      string mappingKind = "";
      int mappingCandidateCount = 0;
      bool mappingAmbiguous = false;
      if(
         StringLen(logicalSym) <= 0 || ContainsSymbol(resolvedLogical, logicalSym) ||
         !ResolveBrokerSymbolStatus(
            logicalSym, brokerSym, mappingReason, mappingKind,
            mappingCandidateCount, mappingAmbiguous
         ) || StringLen(brokerSym) <= 0
      ) {
         ClearMarketDataSymbolCache();
         WarnMarketDataSymbolCache(
            "mapping_failed:" + logicalSym + ":" + mappingReason
         );
         return false;
      }
      int resolvedCount = ArraySize(resolvedLogical);
      ArrayResize(resolvedLogical, resolvedCount + 1);
      ArrayResize(resolvedBroker, resolvedCount + 1);
      ArrayResize(resolvedMappingReasons, resolvedCount + 1);
      ArrayResize(resolvedMappingKinds, resolvedCount + 1);
      ArrayResize(resolvedMappingCandidateCounts, resolvedCount + 1);
      ArrayResize(resolvedMappingAmbiguous, resolvedCount + 1);
      resolvedLogical[resolvedCount] = logicalSym;
      resolvedBroker[resolvedCount] = brokerSym;
      resolvedMappingReasons[resolvedCount] = mappingReason;
      resolvedMappingKinds[resolvedCount] = mappingKind;
      resolvedMappingCandidateCounts[resolvedCount] = mappingCandidateCount;
      resolvedMappingAmbiguous[resolvedCount] = mappingAmbiguous;
   }

   string identityAfter = CurrentMarketDataSymbolCacheIdentity();
   if(StringLen(identityAfter) <= 0 || identityAfter != identityBefore) {
      ClearMarketDataSymbolCache();
      WarnMarketDataSymbolCache("broker_identity_changed_during_refresh");
      return false;
   }
   if(
      ArraySize(resolvedLogical) != candidateCount ||
      ArraySize(resolvedBroker) != candidateCount ||
      ArraySize(resolvedMappingReasons) != candidateCount ||
      ArraySize(resolvedMappingKinds) != candidateCount ||
      ArraySize(resolvedMappingCandidateCounts) != candidateCount ||
      ArraySize(resolvedMappingAmbiguous) != candidateCount
   ) {
      ClearMarketDataSymbolCache();
      WarnMarketDataSymbolCache("mapping_incomplete");
      return false;
   }

   bool announceReady = (
      !gMarketDataSymbolCacheReady ||
      gMarketDataSymbolCacheIdentity != identityAfter
   );
   ArrayResize(gMarketDataLogicalSymbols, candidateCount);
   ArrayResize(gMarketDataBrokerSymbols, candidateCount);
   ArrayResize(gMarketDataMappingReasons, candidateCount);
   ArrayResize(gMarketDataMappingKinds, candidateCount);
   ArrayResize(gMarketDataMappingCandidateCounts, candidateCount);
   ArrayResize(gMarketDataMappingAmbiguous, candidateCount);
   for(int copyIndex = 0; copyIndex < candidateCount; copyIndex++) {
      gMarketDataLogicalSymbols[copyIndex] = resolvedLogical[copyIndex];
      gMarketDataBrokerSymbols[copyIndex] = resolvedBroker[copyIndex];
      gMarketDataMappingReasons[copyIndex] =
         resolvedMappingReasons[copyIndex];
      gMarketDataMappingKinds[copyIndex] = resolvedMappingKinds[copyIndex];
      gMarketDataMappingCandidateCounts[copyIndex] =
         resolvedMappingCandidateCounts[copyIndex];
      gMarketDataMappingAmbiguous[copyIndex] =
         resolvedMappingAmbiguous[copyIndex];
   }
   gMarketDataStrategySymbolCount = strategyCount;
   gMarketDataSymbolCacheIdentity = identityAfter;
   gMarketDataSymbolCacheReady = true;
   gLastMarketDataSymbolCacheRefresh = now;
   gBarHistorySymbolCacheRefreshRequested = true;
   if(announceReady)
      Print("[BRIDGE] market-data symbol cache ready symbols=", candidateCount);
   return true;
}

bool EnsureMarketDataSymbolCache() {
   string currentIdentity = CurrentMarketDataSymbolCacheIdentity();
   if(StringLen(currentIdentity) <= 0) {
      ClearMarketDataSymbolCache();
      WarnMarketDataSymbolCache("broker_identity_unavailable");
      return false;
   }
   bool identityChanged =
      gMarketDataSymbolCacheReady &&
      currentIdentity != gMarketDataSymbolCacheIdentity;
   bool shapeInvalid =
      gMarketDataSymbolCacheReady && !MarketDataSymbolCacheShapeValid();
   if(identityChanged || shapeInvalid) ClearMarketDataSymbolCache();

   // A periodic broker-catalogue refresh is maintenance, not minute-edge work.
   // Reuse a still-valid identity-bound mapping during :58..+4; if identity or
   // shape changed, fail tick publication closed and rebuild after the guard.
   if(BarHistoryMinuteEdgeGuardActive())
      return(
         gMarketDataSymbolCacheReady &&
         currentIdentity == gMarketDataSymbolCacheIdentity
      );

   datetime now = TimeLocal();
   if(
      gMarketDataSymbolCacheReady &&
      currentIdentity == gMarketDataSymbolCacheIdentity &&
      gLastMarketDataSymbolCacheRefresh > 0 &&
      now >= gLastMarketDataSymbolCacheRefresh &&
      (now - gLastMarketDataSymbolCacheRefresh) < MARKET_DATA_SYMBOL_CACHE_REFRESH_SECS
   ) return true;

   bool force = (
      currentIdentity != gMarketDataSymbolCacheLastAttemptIdentity
   );
   return RefreshMarketDataSymbolCache(force);
}

string StringTrim(string str) {
   StringTrimLeft(str);
   StringTrimRight(str);
   return str;
}

string JsonEscape(string in) {
   string out = in;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   StringReplace(out, "\r", " ");
   StringReplace(out, "\n", " ");
   return out;
}

string JsonBool(bool flag) {
   return flag ? "true" : "false";
}

string AckPath() {
   return "/v2/commands/ack";
}

string PollPath() {
   return "/v2/commands/poll?format=line" +
      "&consumer_identity=" + gBridgeConsumerIdentity +
      "&producer_instance_id=" + gBridgeProducerInstanceId +
      "&terminal_lease_scope=" + gBridgeTerminalLeaseScope +
      "&credential_generation_id=" + gBridgeCredentialGenerationId +
      "&bridge_protocol_version=" + EA_EXPECTED_PROTOCOL_VERSION;
}

string ReportPath() {
   return "/v2/reports";
}

string TickPath() {
   return "/v2/market/ticks";
}

string BarHistoryPath() {
   return "/v2/market/bars";
}

string BarHistoryBatchPath() {
   return "/v2/market/bars/batch";
}

string BarHistoryCoveragePath() {
   return "/v2/market/bars/coverage";
}

int BarHistorySymbols(string &out[]) {
   int configured = SymbolsFromCsv(BarHistorySymbolsCsv, out);
   if(configured > 0) return configured;
   return EffectiveSymbols(out);
}

void ClearBarHistorySymbolCache() {
   ArrayResize(gBarHistoryCacheRequestedSymbols, 0);
   ArrayResize(gBarHistoryCacheLogicalSymbols, 0);
   ArrayResize(gBarHistoryCacheBrokerSymbols, 0);
   gBarHistorySymbolCacheReady = false;
   gBarHistorySymbolCacheIdentity = "";
   gBarHistoryMaterializationCursor = 0;
   gBarHistoryMaterializationReady = false;
}

void WarnBarHistorySymbolCache(string reason) {
   datetime now = TimeLocal();
   if(
      gLastBarHistorySymbolCacheWarn > 0 &&
      now >= gLastBarHistorySymbolCacheWarn &&
      (now - gLastBarHistorySymbolCacheWarn) < 15
   ) return;
   gLastBarHistorySymbolCacheWarn = now;
   Print(
      "[BRIDGE] bar-history symbol cache unavailable; publication fenced reason=",
      reason
   );
}

string CurrentBarHistorySymbolCacheIdentity() {
   string marketIdentity = CurrentMarketDataSymbolCacheIdentity();
   string terminalDataPath = StringTrim(
      TerminalInfoString(TERMINAL_DATA_PATH)
   );
   if(
      StringLen(marketIdentity) <= 0 ||
      StringLen(terminalDataPath) <= 0 ||
      !ProducerInstanceIdValid(gBridgeProducerInstanceId)
   ) return "";
   string configuredBars = StringTrim(BarHistorySymbolsCsv);
   return(
      marketIdentity +
      "|terminal=" + terminalDataPath +
      "|producer=" + gBridgeProducerInstanceId +
      "|bar_symbols_len=" + IntegerToString(StringLen(configuredBars)) +
      "|bar_symbols=" + configuredBars
   );
}

bool BarHistorySymbolCacheFastValid(string &reason) {
   reason = "";
   string currentIdentity = CurrentBarHistorySymbolCacheIdentity();
   if(StringLen(currentIdentity) <= 0) {
      reason = "identity_unavailable";
      return false;
   }
   if(!gBarHistorySymbolCacheReady) {
      reason = "not_ready";
      return false;
   }
   if(gBarHistorySymbolCacheRefreshRequested) {
      reason = "market_mapping_refresh_pending";
      return false;
   }
   if(gBarHistorySymbolCacheIdentity != currentIdentity) {
      reason = "identity_changed";
      return false;
   }

   int count = ArraySize(gBarHistoryCacheLogicalSymbols);
   if(
      count <= 0 ||
      count != ArraySize(gBarHistoryCacheRequestedSymbols) ||
      count != ArraySize(gBarHistoryCacheBrokerSymbols)
   ) {
      reason = "shape_changed";
      return false;
   }
   for(int i = 0; i < count; i++) {
      if(
         StringLen(gBarHistoryCacheRequestedSymbols[i]) <= 0 ||
         StringLen(gBarHistoryCacheLogicalSymbols[i]) <= 0 ||
         StringLen(gBarHistoryCacheBrokerSymbols[i]) <= 0
      ) {
         reason = "mapping_empty_index_" + IntegerToString(i);
         return false;
      }
   }
   reason = "ok";
   return true;
}

bool BarHistorySymbolCacheValid(string &reason) {
   // Full configured-scope and duplicate validation stays on the one-second
   // maintenance lane. The 250 ms minute-edge lane uses the identity/shape
   // proof above and never reparses CSV or performs quadratic string scans.
   if(!BarHistorySymbolCacheFastValid(reason)) return false;

   string requestedSymbols[];
   int count = BarHistorySymbols(requestedSymbols);
   if(
      count <= 0 ||
      count != ArraySize(gBarHistoryCacheRequestedSymbols) ||
      count != ArraySize(gBarHistoryCacheLogicalSymbols) ||
      count != ArraySize(gBarHistoryCacheBrokerSymbols)
   ) {
      reason = "shape_changed";
      return false;
   }
   for(int i = 0; i < count; i++) {
      string requested = ToUpperSafe(StringTrim(requestedSymbols[i]));
      string cachedRequested = ToUpperSafe(
         StringTrim(gBarHistoryCacheRequestedSymbols[i])
      );
      string logical = ToUpperSafe(
         StringTrim(gBarHistoryCacheLogicalSymbols[i])
      );
      string broker = StringTrim(gBarHistoryCacheBrokerSymbols[i]);
      if(
         StringLen(requested) <= 0 || requested != cachedRequested ||
         StringLen(logical) <= 0 || StringLen(broker) <= 0
      ) {
         reason = "logical_mapping_changed_index_" + IntegerToString(i);
         return false;
      }
      for(int previous = 0; previous < i; previous++) {
         if(
            logical == ToUpperSafe(
               StringTrim(gBarHistoryCacheLogicalSymbols[previous])
            ) ||
            ToUpperSafe(broker) == ToUpperSafe(
               StringTrim(gBarHistoryCacheBrokerSymbols[previous])
            )
         ) {
            reason = "duplicate_mapping_index_" + IntegerToString(i);
            return false;
         }
      }
   }
   reason = "ok";
   return true;
}

bool RefreshBarHistorySymbolCache() {
   // Reuse the identity-bound exact strategy prefix already resolved by the
   // market-data cache. A second all-catalogue discovery here doubled startup
   // work and could disagree with the quote path's broker mapping.
   if(BarHistoryMinuteEdgeGuardActive()) return false;
   datetime now = TimeLocal();
   gLastBarHistorySymbolCacheAttempt = now;
   string identityBefore = CurrentBarHistorySymbolCacheIdentity();
   if(StringLen(identityBefore) <= 0) {
      ClearBarHistorySymbolCache();
      WarnBarHistorySymbolCache("identity_unavailable");
      return false;
   }
   string currentMarketIdentity = CurrentMarketDataSymbolCacheIdentity();
   if(
      !MarketDataSymbolCacheShapeValid() ||
      StringLen(currentMarketIdentity) <= 0 ||
      gMarketDataSymbolCacheIdentity != currentMarketIdentity
   ) {
      ClearBarHistorySymbolCache();
      WarnBarHistorySymbolCache("market_mapping_unavailable_or_drifted");
      return false;
   }

   string requestedSymbols[];
   int count = BarHistorySymbols(requestedSymbols);
   if(count <= 0 || count != gMarketDataStrategySymbolCount) {
      ClearBarHistorySymbolCache();
      WarnBarHistorySymbolCache("configured_scope_not_exact_strategy_prefix");
      return false;
   }
   string candidateRequested[];
   string candidateLogical[];
   string candidateBroker[];
   ArrayResize(candidateRequested, count);
   ArrayResize(candidateLogical, count);
   ArrayResize(candidateBroker, count);
   for(int i = 0; i < count; i++) {
      string requested = ToUpperSafe(StringTrim(requestedSymbols[i]));
      string logical = NormalizePairToken(requested);
      string cachedLogical = ToUpperSafe(
         StringTrim(gMarketDataLogicalSymbols[i])
      );
      string broker = StringTrim(gMarketDataBrokerSymbols[i]);
      if(
         StringLen(requested) <= 0 || StringLen(logical) <= 0 ||
         logical != cachedLogical ||
         ContainsSymbol(candidateRequested, requested) ||
         ContainsSymbol(candidateLogical, logical) ||
         StringLen(broker) <= 0 ||
         ContainsSymbol(candidateBroker, broker)
      ) {
         ClearBarHistorySymbolCache();
         WarnBarHistorySymbolCache(
            "market_mapping_scope_or_uniqueness_failed:" + requested
         );
         return false;
      }
      candidateRequested[i] = requested;
      candidateLogical[i] = logical;
      candidateBroker[i] = broker;
   }

   string identityAfter = CurrentBarHistorySymbolCacheIdentity();
   if(StringLen(identityAfter) <= 0 || identityAfter != identityBefore) {
      ClearBarHistorySymbolCache();
      WarnBarHistorySymbolCache("identity_changed_during_refresh");
      return false;
   }
   ArrayResize(gBarHistoryCacheRequestedSymbols, count);
   ArrayResize(gBarHistoryCacheLogicalSymbols, count);
   ArrayResize(gBarHistoryCacheBrokerSymbols, count);
   for(int copyIndex = 0; copyIndex < count; copyIndex++) {
      gBarHistoryCacheRequestedSymbols[copyIndex] =
         candidateRequested[copyIndex];
      gBarHistoryCacheLogicalSymbols[copyIndex] = candidateLogical[copyIndex];
      gBarHistoryCacheBrokerSymbols[copyIndex] = candidateBroker[copyIndex];
   }
   gBarHistorySymbolCacheIdentity = identityAfter;
   gBarHistorySymbolCacheReady = true;
   gBarHistorySymbolCacheRefreshRequested = false;
   gBarHistoryMaterializationCursor = 0;
   gBarHistoryMaterializationReady = false;
   Print("[BRIDGE] bar-history symbol cache ready symbols=", count);
   return true;
}

bool EnsureBarHistorySymbolCache() {
   string reason = "";
   bool valid = BarHistorySymbolCacheValid(reason);
   if(valid && !gBarHistorySymbolCacheRefreshRequested) return true;
   if(BarHistoryMinuteEdgeGuardActive()) return false;
   datetime now = TimeLocal();
   if(
      gLastBarHistorySymbolCacheAttempt > 0 &&
      now >= gLastBarHistorySymbolCacheAttempt &&
      (now - gLastBarHistorySymbolCacheAttempt) <
         MARKET_DATA_SYMBOL_CACHE_RETRY_SECS
   ) return false;
   return RefreshBarHistorySymbolCache();
}

int BarHistoryEmissionIndex(string logicalSym) {
   string target = ToUpperSafe(StringTrim(logicalSym));
   int count = ArraySize(gBarHistoryEmissionSymbols);
   for(int i = 0; i < count; i++) {
      if(ToUpperSafe(gBarHistoryEmissionSymbols[i]) == target) return i;
   }
   return -1;
}

string CachedBarHistoryBrokerSymbol(string logicalSym) {
   string target = ToUpperSafe(StringTrim(logicalSym));
   int count = ArraySize(gBarHistoryCacheLogicalSymbols);
   if(count != ArraySize(gBarHistoryCacheBrokerSymbols)) return("");
   for(int i = 0; i < count; i++) {
      if(
         ToUpperSafe(StringTrim(gBarHistoryCacheLogicalSymbols[i])) == target
      ) return(StringTrim(gBarHistoryCacheBrokerSymbols[i]));
   }
   return("");
}

datetime LastEmittedClosedBarTime(string logicalSym) {
   int index = BarHistoryEmissionIndex(logicalSym);
   if(index < 0 || index >= ArraySize(gBarHistoryLastEmittedClosedTimes))
      return 0;
   return gBarHistoryLastEmittedClosedTimes[index];
}

void RecordEmittedClosedBarTime(string logicalSym, datetime brokerTime) {
   if(brokerTime <= 0) return;
   int index = BarHistoryEmissionIndex(logicalSym);
   if(index < 0) {
      index = ArraySize(gBarHistoryEmissionSymbols);
      ArrayResize(gBarHistoryEmissionSymbols, index + 1);
      ArrayResize(gBarHistoryLastEmittedClosedTimes, index + 1);
      gBarHistoryEmissionSymbols[index] = ToUpperSafe(StringTrim(logicalSym));
      gBarHistoryLastEmittedClosedTimes[index] = brokerTime;
      return;
   }
   if(brokerTime > gBarHistoryLastEmittedClosedTimes[index])
      gBarHistoryLastEmittedClosedTimes[index] = brokerTime;
}

void ClearBarHistoryEdgeStaging() {
   int count = ArraySize(gBarHistoryEdgeStagedFlags);
   for(int i = 0; i < count; i++) {
      gBarHistoryEdgeStagedFlags[i] = false;
      gBarHistoryEdgeStagedEntries[i] = "";
      gBarHistoryEdgeStagedClosedTimes[i] = 0;
   }
   gBarHistoryEdgeStagedCount = 0;
}

bool StageBarHistoryEdgeEntry(
   int index,
   string batchEntryJson,
   datetime emittedClosedTime
) {
   int count = ArraySize(gBarHistoryEdgeStagedFlags);
   if(
      index < 0 || index >= count ||
      count != ArraySize(gBarHistoryEdgeStagedEntries) ||
      count != ArraySize(gBarHistoryEdgeStagedClosedTimes) ||
      StringLen(batchEntryJson) <= 0 || emittedClosedTime <= 0
   ) return false;
   if(gBarHistoryEdgeStagedFlags[index])
      return(
         gBarHistoryEdgeStagedEntries[index] == batchEntryJson &&
         gBarHistoryEdgeStagedClosedTimes[index] == emittedClosedTime
      );
   gBarHistoryEdgeStagedFlags[index] = true;
   gBarHistoryEdgeStagedEntries[index] = batchEntryJson;
   gBarHistoryEdgeStagedClosedTimes[index] = emittedClosedTime;
   gBarHistoryEdgeStagedCount++;
   return true;
}

void BeginBarHistoryMinuteEdge(int minuteBucket) {
   int count = ArraySize(gBarHistoryCacheLogicalSymbols);
   gBarHistoryPendingMinuteBucket = minuteBucket;
   gBarHistoryPendingNextLateRetryAt =
      (datetime)(minuteBucket * SCALP_BAR_HISTORY_RESYNC_SECS) +
      SCALP_BAR_HISTORY_MINUTE_EDGE_DEADLINE_SECS + 1;
   ArrayResize(gBarHistoryPendingCompletedFlags, count);
   ArrayResize(gBarHistoryEdgeStagedFlags, count);
   ArrayResize(gBarHistoryEdgeStagedEntries, count);
   ArrayResize(gBarHistoryEdgeStagedClosedTimes, count);
   for(int i = 0; i < count; i++) {
      gBarHistoryPendingCompletedFlags[i] = false;
      gBarHistoryEdgeStagedFlags[i] = false;
      gBarHistoryEdgeStagedEntries[i] = "";
      gBarHistoryEdgeStagedClosedTimes[i] = 0;
   }
   gBarHistoryPendingRemaining = count;
   gBarHistoryEdgeStagedCount = 0;
   gBarHistoryEdgeProbeCursor = 0;
   gBarHistoryRemoteProbePending = true;
}

void ClearBarHistoryMinuteEdge() {
   gBarHistoryPendingMinuteBucket = -1;
   gBarHistoryPendingNextLateRetryAt = 0;
   ArrayResize(gBarHistoryPendingCompletedFlags, 0);
   ArrayResize(gBarHistoryEdgeStagedFlags, 0);
   ArrayResize(gBarHistoryEdgeStagedEntries, 0);
   ArrayResize(gBarHistoryEdgeStagedClosedTimes, 0);
   gBarHistoryPendingRemaining = 0;
   gBarHistoryEdgeStagedCount = 0;
   gBarHistoryEdgeProbeCursor = 0;
}

bool RefreshDirectM1Series(string brokerSym) {
   MqlRates probe[];
   ArraySetAsSeries(probe, true);
   ResetLastError();
   int copied = CopyRates(brokerSym, PERIOD_M1, 0, 3, probe);
   return copied >= 2;
}

bool ExpectedFinalizedM1Ready(
   string logicalSym,
   string brokerSym,
   datetime lastEmittedClosedTime,
   datetime &expectedClosedTime,
   string &reason
) {
   expectedClosedTime = 0;
   reason = "";
   if(StringLen(brokerSym) <= 0) {
      reason = "cached_broker_symbol_unavailable";
      return false;
   }
   if(!SymbolSelect(brokerSym, true)) {
      gBarHistorySymbolCacheRefreshRequested = true;
      reason = "cached_broker_symbol_select_failed";
      return false;
   }

   // MODE_TIME proves this symbol, not merely another chart/feed symbol,
   // received a post-boundary broker tick. Work entirely in broker-time here;
   // timezone offsets therefore cannot make a stale shift look finalized.
   datetime sourceEventTime = (datetime)MarketInfo(brokerSym, MODE_TIME);
   if(sourceEventTime <= 0) {
      reason = "source_tick_unavailable";
      return false;
   }
   datetime sourceMinute =
      sourceEventTime - (sourceEventTime % SCALP_BAR_HISTORY_RESYNC_SECS);
   expectedClosedTime = sourceMinute - SCALP_BAR_HISTORY_RESYNC_SECS;
   if(expectedClosedTime <= lastEmittedClosedTime) {
      reason = "post_boundary_tick_pending";
      return false;
   }

   // Off-chart iTime slots can remain pinned for tens of seconds even after
   // MODE_TIME proves a post-boundary broker tick. Refresh the bounded
   // three-row series inside the existing four-symbol-per-callback lane before
   // reading shift 0/1. Without this materialization the direct frame reached
   // the bridge around T+45 and every legitimate T+5 scalp window was missed.
   if(!RefreshDirectM1Series(brokerSym)) {
      reason = "direct_m1_refresh_pending";
      return false;
   }
   datetime currentBarTime = iTime(brokerSym, PERIOD_M1, 0);
   datetime closedBarTime = iTime(brokerSym, PERIOD_M1, 1);
   if(currentBarTime != sourceMinute) {
      reason = "direct_m1_current_bar_pending";
      return false;
   }
   if(closedBarTime != expectedClosedTime) {
      reason = "direct_m1_shift1_pending";
      return false;
   }
   reason = "ready";
   return true;
}

void MixBarHistoryFingerprint(string value, uint &hashA, uint &hashB) {
   int length = StringLen(value);
   for(int i = 0; i < length; i++) {
      uint code = (uint)StringGetCharacter(value, i);
      hashA = ((hashA << 5) + hashA) ^ code;
      hashB = (hashB * 65599) + code;
   }
}

int BarHistoryStabilityIndex(string logicalSym) {
   string target = ToUpperSafe(StringTrim(logicalSym));
   int count = ArraySize(gBarHistoryStabilitySymbols);
   for(int i = 0; i < count; i++) {
      if(ToUpperSafe(gBarHistoryStabilitySymbols[i]) == target) return i;
   }
   return -1;
}

int BarHistoryDiagnosticIndex(string logicalSym) {
   string target = ToUpperSafe(StringTrim(logicalSym));
   int count = ArraySize(gBarHistoryDiagnosticSymbols);
   for(int i = 0; i < count; i++) {
      if(ToUpperSafe(gBarHistoryDiagnosticSymbols[i]) == target) return i;
   }
   return -1;
}

void WarnBarHistoryRecoveryPending(
   string logicalSym,
   string brokerSym,
   string reason,
   int copied,
   int available,
   datetime currentBarTime,
   datetime sourceEventTime,
   datetime newestClosedTime,
   long seriesSynchronized,
   int copyError,
   datetime now
) {
   int index = BarHistoryDiagnosticIndex(logicalSym);
   if(index < 0) {
      index = ArraySize(gBarHistoryDiagnosticSymbols);
      ArrayResize(gBarHistoryDiagnosticSymbols, index + 1);
      ArrayResize(gBarHistoryDiagnosticAt, index + 1);
      gBarHistoryDiagnosticSymbols[index] =
         ToUpperSafe(StringTrim(logicalSym));
      gBarHistoryDiagnosticAt[index] = 0;
   }
   datetime lastWarnAt = gBarHistoryDiagnosticAt[index];
   if(
      lastWarnAt > 0 && now >= lastWarnAt &&
      (now - lastWarnAt) < SCALP_BAR_HISTORY_DIAGNOSTIC_INTERVAL_SECS
   ) return;
   gBarHistoryDiagnosticAt[index] = now;
   Print(
      "[BRIDGE] cold M1 recovery pending symbol=", logicalSym,
      " broker=", brokerSym,
      " reason=", reason,
      " copied=", copied,
      " iBars=", available,
      " current=", (int)currentBarTime,
      " source=", (int)sourceEventTime,
      " shift1=", (int)newestClosedTime,
      " sync=", seriesSynchronized,
      " copy_err=", copyError
   );
}

void ClearBarHistoryStability() {
   ArrayResize(gBarHistoryStabilitySymbols, 0);
   ArrayResize(gBarHistoryStabilityFingerprints, 0);
   ArrayResize(gBarHistoryStabilityObservedAt, 0);
   ArrayResize(gBarHistoryStabilityNewestClosedTimes, 0);
   ArrayResize(gBarHistoryStabilityRecheckCounts, 0);
   ArrayResize(gBarHistoryDiagnosticSymbols, 0);
   ArrayResize(gBarHistoryDiagnosticAt, 0);
   gBarHistoryStabilityNextCheckAt = 0;
}

int BarHistoryRecoveryChartIndex(string logicalSym) {
   string target = ToUpperSafe(StringTrim(logicalSym));
   for(int i = 0; i < ArraySize(gBarHistoryRecoveryChartSymbols); i++) {
      if(ToUpperSafe(gBarHistoryRecoveryChartSymbols[i]) == target) return i;
   }
   return -1;
}

bool HasOpenBarHistoryM1Chart(string brokerSym) {
   string target = StringTrim(brokerSym);
   if(StringLen(target) <= 0) return false;
   long chartId = ChartFirst();
   int visited = 0;
   while(chartId >= 0 && visited < 256) {
      if(
         ChartSymbol(chartId) == target &&
         ChartPeriod(chartId) == PERIOD_M1
      ) return true;
      chartId = ChartNext(chartId);
      visited++;
   }
   return false;
}

bool EnsureBarHistoryRecoveryChart(string logicalSym, string brokerSym) {
   if(BarHistoryRecoveryChartIndex(logicalSym) >= 0) return true;
   // Reuse an operator-opened chart without claiming ownership of it.  Only
   // chart IDs created below are stored and later closed by this EA.
   if(HasOpenBarHistoryM1Chart(brokerSym)) return true;
   if(
      StringLen(brokerSym) <= 0 ||
      ArraySize(gBarHistoryRecoveryChartIds) >=
         SCALP_BAR_HISTORY_RECOVERY_CHARTS_MAX
   ) return false;
   long chartId = ChartOpen(brokerSym, PERIOD_M1);
   if(chartId <= 0 || chartId == ChartID()) return false;
   int index = ArraySize(gBarHistoryRecoveryChartSymbols);
   ArrayResize(gBarHistoryRecoveryChartSymbols, index + 1);
   ArrayResize(gBarHistoryRecoveryChartIds, index + 1);
   gBarHistoryRecoveryChartSymbols[index] =
      ToUpperSafe(StringTrim(logicalSym));
   gBarHistoryRecoveryChartIds[index] = chartId;
   Print(
      "[BRIDGE] temporary M1 recovery chart opened symbol=", logicalSym,
      " broker=", brokerSym
   );
   return true;
}

void CloseBarHistoryRecoveryChart(string logicalSym) {
   int index = BarHistoryRecoveryChartIndex(logicalSym);
   int count = ArraySize(gBarHistoryRecoveryChartSymbols);
   if(index < 0 || index >= count) return;
   long chartId = gBarHistoryRecoveryChartIds[index];
   if(chartId > 0 && chartId != ChartID()) ChartClose(chartId);
   for(int i = index + 1; i < count; i++) {
      gBarHistoryRecoveryChartSymbols[i - 1] =
         gBarHistoryRecoveryChartSymbols[i];
      gBarHistoryRecoveryChartIds[i - 1] = gBarHistoryRecoveryChartIds[i];
   }
   ArrayResize(gBarHistoryRecoveryChartSymbols, count - 1);
   ArrayResize(gBarHistoryRecoveryChartIds, count - 1);
}

void CloseAllBarHistoryRecoveryCharts() {
   for(int i = ArraySize(gBarHistoryRecoveryChartIds) - 1; i >= 0; i--) {
      long chartId = gBarHistoryRecoveryChartIds[i];
      if(chartId > 0 && chartId != ChartID()) ChartClose(chartId);
   }
   ArrayResize(gBarHistoryRecoveryChartSymbols, 0);
   ArrayResize(gBarHistoryRecoveryChartIds, 0);
}

void ServiceOneBarHistoryMaterializationChart() {
   if(gBarHistoryMaterializationReady || BarHistoryMinuteEdgeGuardActive())
      return;
   string cacheReason = "";
   if(!BarHistorySymbolCacheFastValid(cacheReason)) return;
   int count = ArraySize(gBarHistoryCacheLogicalSymbols);
   if(count <= 0 || count != ArraySize(gBarHistoryCacheBrokerSymbols)) return;
   if(gBarHistoryMaterializationCursor < 0)
      gBarHistoryMaterializationCursor = 0;
   if(gBarHistoryMaterializationCursor >= count) {
      gBarHistoryMaterializationReady = true;
      Print("[BRIDGE] direct M1 materialization ready symbols=", count);
      return;
   }

   int index = gBarHistoryMaterializationCursor;
   string logicalSym = gBarHistoryCacheLogicalSymbols[index];
   string brokerSym = gBarHistoryCacheBrokerSymbols[index];
   if(EnsureBarHistoryRecoveryChart(logicalSym, brokerSym)) {
      gBarHistoryMaterializationCursor++;
      if(gBarHistoryMaterializationCursor >= count) {
         gBarHistoryMaterializationReady = true;
         Print("[BRIDGE] direct M1 materialization ready symbols=", count);
      }
   }
}

void ClearBarHistoryStabilityObservation(string logicalSym) {
   int index = BarHistoryStabilityIndex(logicalSym);
   if(index < 0 || index >= ArraySize(gBarHistoryStabilitySymbols)) return;
   gBarHistoryStabilityFingerprints[index] = "";
   gBarHistoryStabilityObservedAt[index] = 0;
   gBarHistoryStabilityNewestClosedTimes[index] = 0;
   gBarHistoryStabilityRecheckCounts[index] = 0;
}

void RetireBarHistoryRecoveryState(string logicalSym) {
   // Keep the symbol's M1 chart open after cold recovery. Closing it returned
   // the series to asynchronous off-chart updates and made the next finalized
   // bar arrive too late for the scalper.
   int stabilityIndex = BarHistoryStabilityIndex(logicalSym);
   int stabilityCount = ArraySize(gBarHistoryStabilitySymbols);
   if(stabilityIndex >= 0 && stabilityIndex < stabilityCount) {
      for(int i = stabilityIndex + 1; i < stabilityCount; i++) {
         gBarHistoryStabilitySymbols[i - 1] = gBarHistoryStabilitySymbols[i];
         gBarHistoryStabilityFingerprints[i - 1] =
            gBarHistoryStabilityFingerprints[i];
         gBarHistoryStabilityObservedAt[i - 1] =
            gBarHistoryStabilityObservedAt[i];
         gBarHistoryStabilityNewestClosedTimes[i - 1] =
            gBarHistoryStabilityNewestClosedTimes[i];
         gBarHistoryStabilityRecheckCounts[i - 1] =
            gBarHistoryStabilityRecheckCounts[i];
      }
      ArrayResize(gBarHistoryStabilitySymbols, stabilityCount - 1);
      ArrayResize(gBarHistoryStabilityFingerprints, stabilityCount - 1);
      ArrayResize(gBarHistoryStabilityObservedAt, stabilityCount - 1);
      ArrayResize(gBarHistoryStabilityNewestClosedTimes, stabilityCount - 1);
      ArrayResize(gBarHistoryStabilityRecheckCounts, stabilityCount - 1);
   }

   int diagnosticIndex = BarHistoryDiagnosticIndex(logicalSym);
   int diagnosticCount = ArraySize(gBarHistoryDiagnosticSymbols);
   if(diagnosticIndex >= 0 && diagnosticIndex < diagnosticCount) {
      for(int j = diagnosticIndex + 1; j < diagnosticCount; j++) {
         gBarHistoryDiagnosticSymbols[j - 1] =
            gBarHistoryDiagnosticSymbols[j];
         gBarHistoryDiagnosticAt[j - 1] = gBarHistoryDiagnosticAt[j];
      }
      ArrayResize(gBarHistoryDiagnosticSymbols, diagnosticCount - 1);
      ArrayResize(gBarHistoryDiagnosticAt, diagnosticCount - 1);
   }
}

BarHistoryObservationOutcome ObserveStableDirectM1History(
   string logicalSym,
   string brokerSym,
   datetime now
) {
   // The caller has already validated the identity-bound logical/broker cache.
   // Re-resolving every cold symbol scanned the entire MT4 catalogue again and
   // added visible UI latency to genuine bridge-recovery sweeps.
   if(StringLen(brokerSym) <= 0) {
      ClearBarHistoryStabilityObservation(logicalSym);
      WarnBarHistoryRecoveryPending(
         logicalSym, "", "broker_symbol_unavailable", -1, 0,
         0, 0, 0, 0, 0, now
      );
      return BAR_HISTORY_OBSERVATION_INCOHERENT;
   }
   if(!SymbolSelect(brokerSym, true)) {
      ClearBarHistoryStabilityObservation(logicalSym);
      WarnBarHistoryRecoveryPending(
         logicalSym, brokerSym, "broker_symbol_select_failed", -1, 0,
         0, 0, 0, 0, GetLastError(), now
      );
      return BAR_HISTORY_OBSERVATION_INCOHERENT;
   }

   // AGENT HOT PATH: most cold-recovery refusals are an off-chart series that
   // has not caught up with the symbol's latest broker tick yet. Refresh only
   // the three edge rows and prove their timestamps before copying and hashing
   // the full 241-bar window. The old ordering copied 241 rows every five
   // seconds even when the cheap edge proof already made admission impossible,
   // which caused visible MT4 UI stalls on the single EA/timer thread.
   bool edgeRefreshReady = RefreshDirectM1Series(brokerSym);
   datetime sourceEventTime = (datetime)MarketInfo(brokerSym, MODE_TIME);
   datetime currentBarTime = iTime(brokerSym, PERIOD_M1, 0);
   datetime newestClosedTime = iTime(brokerSym, PERIOD_M1, 1);
   int available = iBars(brokerSym, PERIOD_M1);
   long seriesSynchronized = SeriesInfoInteger(
      brokerSym, PERIOD_M1, SERIES_SYNCHRONIZED
   );
   if(!edgeRefreshReady) {
      // ChartOpen is bounded to one chart and idempotent for this symbol. It
      // asks MT4 to materialize an off-chart series without allowing a full
      // 241-row copy after the cheap three-row refresh failed.
      EnsureBarHistoryRecoveryChart(logicalSym, brokerSym);
      ClearBarHistoryStabilityObservation(logicalSym);
      WarnBarHistoryRecoveryPending(
         logicalSym, brokerSym, "direct_m1_refresh_pending", 0, available,
         currentBarTime, sourceEventTime, newestClosedTime,
         seriesSynchronized, GetLastError(), now
      );
      return BAR_HISTORY_OBSERVATION_INCOHERENT;
   }
   if(sourceEventTime <= 0) {
      EnsureBarHistoryRecoveryChart(logicalSym, brokerSym);
      ClearBarHistoryStabilityObservation(logicalSym);
      WarnBarHistoryRecoveryPending(
         logicalSym, brokerSym, "source_tick_unavailable", 0, available,
         currentBarTime, sourceEventTime, newestClosedTime,
         seriesSynchronized, 0, now
      );
      return BAR_HISTORY_OBSERVATION_INCOHERENT;
   }
   datetime sourceMinute =
      sourceEventTime - (sourceEventTime % SCALP_BAR_HISTORY_RESYNC_SECS);
   if(
      currentBarTime <= 0 ||
      ((int)currentBarTime % SCALP_BAR_HISTORY_RESYNC_SECS) != 0 ||
      currentBarTime != sourceMinute
   ) {
      EnsureBarHistoryRecoveryChart(logicalSym, brokerSym);
      ClearBarHistoryStabilityObservation(logicalSym);
      WarnBarHistoryRecoveryPending(
         logicalSym, brokerSym, "direct_m1_current_mismatch", 0, available,
         currentBarTime, sourceEventTime, newestClosedTime,
         seriesSynchronized, 0, now
      );
      return BAR_HISTORY_OBSERVATION_INCOHERENT;
   }
   // Cold recovery proves structural shift-1 coherence, not a synthetic dense
   // minute grid. A legitimate no-tick minute leaves the newest completed bar
   // older than current-60; steady-state ExpectedFinalizedM1Ready still keeps
   // the exact prior-minute T+5 requirement.
   if(
      newestClosedTime <= 0 ||
      ((int)newestClosedTime % SCALP_BAR_HISTORY_RESYNC_SECS) != 0 ||
      newestClosedTime >= currentBarTime
   ) {
      EnsureBarHistoryRecoveryChart(logicalSym, brokerSym);
      ClearBarHistoryStabilityObservation(logicalSym);
      WarnBarHistoryRecoveryPending(
         logicalSym, brokerSym, "direct_m1_shift1_incoherent", 0, available,
         currentBarTime, sourceEventTime, newestClosedTime,
         seriesSynchronized, 0, now
      );
      return BAR_HISTORY_OBSERVATION_INCOHERENT;
   }

   int fullDepth = SCALP_BOOTSTRAP_COMPLETED_DEPTH;
   MqlRates probe[];
   ArraySetAsSeries(probe, true);
   ResetLastError();
   int copied = CopyRates(
      brokerSym, PERIOD_M1, 1, fullDepth, probe
   );
   int copyError = GetLastError();
   if(copied != fullDepth) {
      // MT4 can expose only bars received since terminal startup for an
      // off-chart symbol even while its HST cache already contains the sealed
      // 241-row baseline. One bounded temporary M1 chart asks the terminal to
      // materialize that cache; it is closed immediately after recovery.
      EnsureBarHistoryRecoveryChart(logicalSym, brokerSym);
      ClearBarHistoryStabilityObservation(logicalSym);
      WarnBarHistoryRecoveryPending(
         logicalSym, brokerSym, "copy_incomplete", copied, available,
         currentBarTime, sourceEventTime, newestClosedTime,
         seriesSynchronized, copyError, now
      );
      return BAR_HISTORY_OBSERVATION_INCOHERENT;
   }
   if(probe[0].time != newestClosedTime) {
      EnsureBarHistoryRecoveryChart(logicalSym, brokerSym);
      ClearBarHistoryStabilityObservation(logicalSym);
      WarnBarHistoryRecoveryPending(
         logicalSym, brokerSym, "copy_shift1_mismatch", copied, available,
         currentBarTime, sourceEventTime, newestClosedTime,
         seriesSynchronized, copyError, now
      );
      return BAR_HISTORY_OBSERVATION_INCOHERENT;
   }

   int digits = (int)MarketInfo(brokerSym, MODE_DIGITS);
   uint hashA = 2166136261;
   uint hashB = 5381;
   datetime previousBarTime = 0;
   for(int index = fullDepth - 1; index >= 0; index--) {
      // Fingerprint the one coherent CopyRates snapshot. Repeating six i*()
      // lookups for every row across 22 symbols made a cold recovery sweep far
      // more expensive and could observe a series mutation mid-fingerprint.
      datetime barTime = probe[index].time;
      double bidOpen = probe[index].open;
      double bidHigh = probe[index].high;
      double bidLow = probe[index].low;
      double bidClose = probe[index].close;
      long tickVolume = probe[index].tick_volume;
      // Preserve authentic no-tick minutes as gaps.  Requiring every pair to
      // have a dense 241-minute series made a cold bridge restart impossible
      // for legitimately sparse IG instruments (observed on BTCUSD), even
      // though MT4 already held more than 241 finalized M1 observations.
      // Stability needs a strictly ordered, M1-aligned snapshot; downstream
      // MTVCLC admission separately refuses any gapped 241-bar baseline.
      if(
         barTime <= 0 ||
         ((int)barTime % SCALP_BAR_HISTORY_RESYNC_SECS) != 0 ||
         bidOpen <= 0 || bidHigh <= 0 || bidLow <= 0 || bidClose <= 0 ||
         tickVolume < 0 ||
         (previousBarTime > 0 && barTime <= previousBarTime)
      ) {
         ClearBarHistoryStabilityObservation(logicalSym);
         WarnBarHistoryRecoveryPending(
            logicalSym,
            brokerSym,
            "history_row_invalid_index_" + IntegerToString(index),
            copied,
            available,
            currentBarTime,
            sourceEventTime,
            newestClosedTime,
            seriesSynchronized,
            copyError,
            now
         );
         return BAR_HISTORY_OBSERVATION_INCOHERENT;
      }
      previousBarTime = barTime;
      string row =
         IntegerToString((int)barTime) + "|" +
         DoubleToString(bidOpen, digits) + "|" +
         DoubleToString(bidHigh, digits) + "|" +
         DoubleToString(bidLow, digits) + "|" +
         DoubleToString(bidClose, digits) + "|" +
         IntegerToString((int)MathMin(2147483647.0, (double)tickVolume));
      MixBarHistoryFingerprint(row, hashA, hashB);
   }
   string fingerprint =
      IntegerToString(fullDepth) + ":" +
      IntegerToString((int)hashA) + ":" + IntegerToString((int)hashB);

   int index = BarHistoryStabilityIndex(logicalSym);
   if(index < 0) {
      index = ArraySize(gBarHistoryStabilitySymbols);
      ArrayResize(gBarHistoryStabilitySymbols, index + 1);
      ArrayResize(gBarHistoryStabilityFingerprints, index + 1);
      ArrayResize(gBarHistoryStabilityObservedAt, index + 1);
      ArrayResize(gBarHistoryStabilityNewestClosedTimes, index + 1);
      ArrayResize(gBarHistoryStabilityRecheckCounts, index + 1);
      gBarHistoryStabilitySymbols[index] = ToUpperSafe(StringTrim(logicalSym));
      gBarHistoryStabilityFingerprints[index] = "";
      gBarHistoryStabilityObservedAt[index] = 0;
      gBarHistoryStabilityNewestClosedTimes[index] = 0;
      gBarHistoryStabilityRecheckCounts[index] = 0;
   }
   if(
      StringLen(gBarHistoryStabilityFingerprints[index]) <= 0 ||
      gBarHistoryStabilityObservedAt[index] <= 0 ||
      gBarHistoryStabilityNewestClosedTimes[index] <= 0
   ) {
      gBarHistoryStabilityFingerprints[index] = fingerprint;
      gBarHistoryStabilityObservedAt[index] = now;
      gBarHistoryStabilityNewestClosedTimes[index] = newestClosedTime;
      gBarHistoryStabilityRecheckCounts[index] = 0;
      return BAR_HISTORY_OBSERVATION_WAIT_STABLE;
   }
   if(now < gBarHistoryStabilityObservedAt[index]) {
      ClearBarHistoryStabilityObservation(logicalSym);
      return BAR_HISTORY_OBSERVATION_INCOHERENT;
   }
   if(
      (now - gBarHistoryStabilityObservedAt[index]) <
         SCALP_BAR_HISTORY_STABILITY_SECS
   ) return BAR_HISTORY_OBSERVATION_WAIT_STABLE;
   if(
      gBarHistoryStabilityFingerprints[index] != fingerprint ||
      gBarHistoryStabilityNewestClosedTimes[index] != newestClosedTime
   ) {
      int nextRecheckCount =
         gBarHistoryStabilityRecheckCounts[index] + 1;
      if(
         nextRecheckCount >=
            SCALP_BAR_HISTORY_STABILITY_PINNED_RECHECKS_MAX
      ) {
         // A continuously mutating full snapshot is not allowed to monopolize
         // the exact-scope cursor. Clear its candidate and let the next symbol
         // receive the following bounded recovery pass.
         ClearBarHistoryStabilityObservation(logicalSym);
         return BAR_HISTORY_OBSERVATION_INCOHERENT;
      }
      gBarHistoryStabilityFingerprints[index] = fingerprint;
      gBarHistoryStabilityObservedAt[index] = now;
      gBarHistoryStabilityNewestClosedTimes[index] = newestClosedTime;
      gBarHistoryStabilityRecheckCounts[index] = nextRecheckCount;
      return BAR_HISTORY_OBSERVATION_WAIT_STABLE;
   }
   return BAR_HISTORY_OBSERVATION_STABLE;
}

void ForgetBarHistoryBootstrap(string logicalSym) {
   string target = ToUpperSafe(StringTrim(logicalSym));
   int count = ArraySize(gBarHistoryBootstrappedSymbols);
   int writeIndex = 0;
   for(int i = 0; i < count; i++) {
      if(ToUpperSafe(gBarHistoryBootstrappedSymbols[i]) == target) continue;
      gBarHistoryBootstrappedSymbols[writeIndex] = gBarHistoryBootstrappedSymbols[i];
      writeIndex++;
   }
   ArrayResize(gBarHistoryBootstrappedSymbols, writeIndex);
}

void ResetBarHistoryBootstrap(string reason) {
   ClearBarHistoryStability();
   gBarHistoryFullRecoveryRequested = true;
   gBarHistoryRecoveryTransportAbort = false;
   gBarHistoryRecoveryUploadCursor = 0;
   if(ArraySize(gBarHistoryBootstrappedSymbols) <= 0) return;
   ArrayResize(gBarHistoryBootstrappedSymbols, 0);
   Print("[BRIDGE] full bar-history recovery requested reason=", reason);
}

void RetireSupersededBarHistoryMinuteEdge(int nextMinuteBucket) {
   int pendingBucket = gBarHistoryPendingMinuteBucket;
   int count = ArraySize(gBarHistoryCacheLogicalSymbols);
   if(
      pendingBucket <= gLastBarHistoryMinuteBucket ||
      pendingBucket >= nextMinuteBucket
   ) return;
   if(
      count <= 0 ||
      count != ArraySize(gBarHistoryPendingCompletedFlags) ||
      count != ArraySize(gBarHistoryEdgeStagedFlags)
   ) {
      ResetBarHistoryBootstrap("superseded_edge_shape_invalid");
      gLastBarHistoryMinuteBucket = pendingBucket;
      ClearBarHistoryMinuteEdge();
      return;
   }

   int targetedRecoveryCount = 0;
   for(int i = 0; i < count; i++) {
      // A completed flag is set only after the atomic batch receives 2xx, and
      // that path has already advanced this symbol's actual-data watermark.
      if(gBarHistoryPendingCompletedFlags[i]) continue;
      string logicalSym = gBarHistoryCacheLogicalSymbols[i];
      if(StringLen(logicalSym) <= 0) continue;
      // Target only unresolved symbols. The existing recovery probe cheaply
      // reuses an exact retained row for an authentic no-tick gap; a staged but
      // unacknowledged row or genuinely newer terminal row receives bounded
      // catch-up. Never clear the other symbols' successful bootstrap state.
      ForgetBarHistoryBootstrap(logicalSym);
      ClearBarHistoryStabilityObservation(logicalSym);
      targetedRecoveryCount++;
   }
   if(targetedRecoveryCount > 0) {
      gBarHistoryFullRecoveryRequested = true;
      if(VerboseBridgeLog)
         Print("[BRIDGE] targeted M1 recovery requested symbols=",
               targetedRecoveryCount,
               " retired_bucket=", pendingBucket);
   }
   // Scheduler watermark only. Per-symbol emitted timestamps are deliberately
   // unchanged, so no missing M1 observation is synthesized or treated as sent.
   gLastBarHistoryMinuteBucket = pendingBucket;
   ClearBarHistoryMinuteEdge();
}

void PrimeBarHistoryColdLoad() {
   if(BarHistoryDepth <= 0) return;
   string cacheReason = "";
   if(!BarHistorySymbolCacheValid(cacheReason)) {
      WarnBarHistorySymbolCache("cold_prime:" + cacheReason);
      return;
   }
   datetime now = TimeGMT();
   if(now <= 0) now = TimeLocal();
   if(now <= 0) return;
   gLastBarHistoryAttempt = now;
   gBarHistoryInitialBootstrapAt = now;
   // Baseline only the scheduler generation. This publishes no data, but lets
   // the next UTC minute open the bounded shift-1 lane while the independent
   // full-history stability proof is still pending.
   gLastBarHistoryMinuteBucket =
      (int)(now / SCALP_BAR_HISTORY_RESYNC_SECS);
   ClearBarHistoryMinuteEdge();
   ClearBarHistoryStability();
   gBarHistoryRecoveryTransportAbort = false;
   gBarHistoryRecoveryUploadCursor = 0;
   gBarHistoryDelayedRecoverySweepsRemaining =
      SCALP_BAR_HISTORY_DELAYED_RECOVERY_SWEEPS;
   // Initial cold recovery is owned by the 30-second delayed lane below. The
   // explicit request flag is reserved for API/missed-window repair.
   gBarHistoryFullRecoveryRequested = false;

   // Off-chart history materialization is deliberately left to the existing
   // recovery lane, which owns one symbol per pass. Priming all 22 series here
   // made OnInit monopolize MT4's single EA thread with 22 synchronous CopyRates
   // calls before command polling could settle.
   Print("[BRIDGE] cold M1 recovery scheduled; publication awaits stable proof");
}

string BarHistoryUtcMinuteText(datetime brokerTime) {
   if(brokerTime <= 0) return("");
   int serverOffset = (int)(TimeCurrent() - TimeGMT());
   datetime utcTime = brokerTime - serverOffset;
   MqlDateTime parts;
   TimeToStruct(utcTime, parts);
   return(
      StringFormat(
         "%04d-%02d-%02dT%02d:%02d:00+00:00",
         parts.year,
         parts.mon,
         parts.day,
         parts.hour,
         parts.min
      )
   );
}

int BrokerTimeUtcEpoch(datetime brokerTime) {
   if(brokerTime<=0) return(0);
   int serverOffset=(int)(TimeCurrent()-TimeGMT());
   return((int)brokerTime-serverOffset);
}

RemoteBarHistoryCoverageOutcome ProbeRemoteBarHistoryCoverage(
   string logicalSym,
   string brokerSym,
   datetime &coveredClosedTime
) {
   coveredClosedTime = 0;
   datetime newestClosedTime = 0;
   string newestClosedText = "";
   if(StringLen(brokerSym) > 0) {
      newestClosedTime = iTime(brokerSym, PERIOD_M1, 1);
      newestClosedText = BarHistoryUtcMinuteText(newestClosedTime);
   }
   string url = ApiBase + BarHistoryCoveragePath() +
      "?symbol=" + logicalSym + "&timeframe=M1&minimum=" +
      IntegerToString(SCALP_BAR_HISTORY_REMOTE_MIN_DIRECT_BARS);
   string response = HttpGET(url, gBridgeApiKey);
   int statusCode = LastBridgeHttpStatus();
   WarnAuthFailure("bar_history_probe", statusCode);
   if(statusCode != 200)
      return BAR_HISTORY_REMOTE_COVERAGE_UNAVAILABLE;
   if(
      ParseJsonStringField(response, "schema", "") !=
         SCALP_BAR_HISTORY_COVERAGE_SCHEMA
   ) return BAR_HISTORY_REMOTE_COVERAGE_UNAVAILABLE;
   string readyNeedle = "\"ready\":";
   int readyAt = StringFind(response, readyNeedle);
   if(
      readyAt < 0 ||
      StringFind(response, readyNeedle, readyAt + StringLen(readyNeedle)) >= 0
   ) return BAR_HISTORY_REMOTE_COVERAGE_UNAVAILABLE;
   int readyValueAt = readyAt + StringLen(readyNeedle);
   bool readyTrue =
      StringSubstr(response, readyValueAt, 4) == "true";
   bool readyFalse =
      StringSubstr(response, readyValueAt, 5) == "false";
   if(readyTrue == readyFalse)
      return BAR_HISTORY_REMOTE_COVERAGE_UNAVAILABLE;
   int readyEnd = readyValueAt + (readyTrue ? 4 : 5);
   int responseLength = StringLen(response);
   while(readyEnd < responseLength) {
      string readySeparator = StringSubstr(response, readyEnd, 1);
      if(
         readySeparator != " " && readySeparator != "\t" &&
         readySeparator != "\r" && readySeparator != "\n"
      ) break;
      readyEnd++;
   }
   if(readyEnd >= responseLength)
      return BAR_HISTORY_REMOTE_COVERAGE_UNAVAILABLE;
   string readyDelimiter = StringSubstr(response, readyEnd, 1);
   if(readyDelimiter != "," && readyDelimiter != "}")
      return BAR_HISTORY_REMOTE_COVERAGE_UNAVAILABLE;
   if(readyFalse)
      return BAR_HISTORY_REMOTE_COVERAGE_MISSING;
   // An API-restart probe needs only to prove that the process-local direct-M1
   // seed still exists. Requiring its latest row to equal MT4 shift 1 here made
   // a normal minute-edge/no-tick lag look like total seed loss and repeatedly
   // rebuilt 22 * 241 bars. Terminal-only bootstrap reuse remains stricter: it
   // still requires the exact current shift-1 timestamp before trusting bytes
   // retained by a bridge that outlived the terminal.
   if(
      newestClosedTime <= 0 || StringLen(newestClosedText) <= 0 ||
      ParseJsonStringField(response, "latest_direct_time", "") !=
         newestClosedText
   ) return BAR_HISTORY_REMOTE_COVERAGE_RETAINED_STALE;
   coveredClosedTime = newestClosedTime;
   return BAR_HISTORY_REMOTE_COVERAGE_EXACT;
}

bool SendBarHistoryForSymbol(
   string logicalSym,
   string brokerSym,
   bool bootstrap,
   datetime requiredClosedTime,
   datetime &emittedClosedTime
) {
   emittedClosedTime = 0;
   string marketSourceFields = CurrentBrokerMarketSourceJsonFields();
   if(StringLen(marketSourceFields) <= 0) return false;
   if(StringLen(brokerSym) <= 0) return false;
   if(!SymbolSelect(brokerSym, true)) {
      gBarHistorySymbolCacheRefreshRequested = true;
      return false;
   }
   // Cold recovery benefits from explicitly priming the off-chart series. The
   // incremental edge lane has already done that in ExpectedFinalizedM1Ready
   // and uses its own final CopyRates+iTime shift-1 recheck before batching.
   if(bootstrap) RefreshDirectM1Series(brokerSym);

   int fullDepth = SCALP_BOOTSTRAP_COMPLETED_DEPTH;
   int depth = bootstrap
      ? fullDepth
      : MathMin(SCALP_BAR_HISTORY_INCREMENTAL_DEPTH, fullDepth);
   MqlRates bars[];
   ArraySetAsSeries(bars, true);
   ResetLastError();
   int copied = CopyRates(brokerSym, PERIOD_M1, 1, depth, bars);
   if(copied != depth) {
      if(bootstrap || VerboseBridgeLog)
         Print("[BRIDGE] bar history copy incomplete symbol=", logicalSym,
               " broker=", brokerSym, " copied=", copied,
               " required=", depth, " err=", GetLastError());
      return false;
   }

   datetime newestClosedTime = bars[0].time;
   if(
      newestClosedTime <= 0 ||
      newestClosedTime != iTime(brokerSym, PERIOD_M1, 1) ||
      (requiredClosedTime > 0 && newestClosedTime != requiredClosedTime)
   ) return false;

   datetime previousBarTime = 0;
   for(int validateIndex = copied - 1; validateIndex >= 0; validateIndex--) {
      MqlRates validateBar = bars[validateIndex];
      // Upload the exact finalized observations MT4 has and leave missing
      // minutes missing.  Dense-history enforcement belongs to the strategy
      // baseline gate, not the transport bootstrap; otherwise one sparse pair
      // prevents collection for the entire fixed 22-symbol scope after every
      // API restart.
      if(
         validateBar.time <= 0 ||
         ((int)validateBar.time % SCALP_BAR_HISTORY_RESYNC_SECS) != 0 ||
         validateBar.open <= 0 || validateBar.high <= 0 ||
         validateBar.low <= 0 || validateBar.close <= 0 ||
         validateBar.tick_volume < 0 ||
         (previousBarTime > 0 && validateBar.time <= previousBarTime)
      ) return false;
      previousBarTime = validateBar.time;
   }

   // A cold recovery is one terminal snapshot, not three unrelated chunks.
   // Keeping all 241 rows in one bounded request lets the API attest an
   // authentic sparse snapshot and commits or rejects the symbol atomically.
   // The compatibility input still bounds non-bootstrap publication.
   int batchLimit = bootstrap
      ? fullDepth
      : MathMax(1, MathMin(500, BarHistoryBatchSize));
   int serverOffset = (int)(TimeCurrent() - TimeGMT());
   int digits = (int)MarketInfo(brokerSym, MODE_DIGITS);
   double point = MarketInfo(brokerSym, MODE_POINT);
   double spreadPx = MathMax(0.0, MarketInfo(brokerSym, MODE_SPREAD) * point);
   string barsJson = "";
   int batchCount = 0;
   int sentCount = 0;
   bool newestClosedIncluded = false;

   for(int index = copied - 1; index >= 0; index--) {
      MqlRates bar = bars[index];
      datetime brokerTime = bar.time;
      double bidOpen = bar.open;
      double bidHigh = bar.high;
      double bidLow = bar.low;
      double bidClose = bar.close;
      if(brokerTime == newestClosedTime) newestClosedIncluded = true;

      int utcEpoch = (int)brokerTime - serverOffset;
      double halfSpread = spreadPx / 2.0;
      double midOpen = bidOpen + halfSpread;
      double midHigh = bidHigh + halfSpread;
      double midLow = bidLow + halfSpread;
      double midClose = bidClose + halfSpread;
      int volume = (int)MathMax(
         0, MathMin(2147483647.0, (double)bar.tick_volume)
      );
      string row =
         "{\"time\":" + IntegerToString(utcEpoch) +
         ",\"open\":" + DoubleToString(midOpen, digits) +
         ",\"high\":" + DoubleToString(midHigh, digits) +
         ",\"low\":" + DoubleToString(midLow, digits) +
         ",\"close\":" + DoubleToString(midClose, digits) +
         ",\"bid_open\":" + DoubleToString(bidOpen, digits) +
         ",\"bid_high\":" + DoubleToString(bidHigh, digits) +
         ",\"bid_low\":" + DoubleToString(bidLow, digits) +
         ",\"bid_close\":" + DoubleToString(bidClose, digits) +
         ",\"spread\":" + DoubleToString(spreadPx, digits) +
         ",\"volume\":" + IntegerToString(volume) +
         ",\"volume_source\":\"mt4_ivolume_tick_count_v1\"" +
         ",\"price_basis\":\"mt4_bid_ohlc_v1\"}";
      if(batchCount > 0) barsJson = barsJson + ",";
      barsJson = barsJson + row;
      batchCount++;

      if(batchCount >= batchLimit || index == 0) {
         string payload =
            "{\"symbol\":\"" + JsonEscape(logicalSym) +
            "\"" + marketSourceFields +
            ",\"timeframe\":\"M1\",\"bars\":[" + barsJson + "]}";
         HttpPOST(ApiBase + BarHistoryPath(), payload, gBridgeApiKey);
         int statusCode = LastBridgeHttpStatus();
         WarnAuthFailure("bar_history", statusCode);
         if(statusCode < 200 || statusCode >= 300) {
            // A synchronous common failure must stop this maintenance pass.
            // Continuing through 22 symbols can otherwise pin OnTimer for
            // tens of seconds. HTTP 409 remains symbol/data-specific.
            if(bootstrap && statusCode != 409)
               gBarHistoryRecoveryTransportAbort = true;
            Print("[BRIDGE] bar history batch failed symbol=", logicalSym,
                  " status=", statusCode, " sent=", sentCount);
            return false;
         }
         sentCount += batchCount;
         barsJson = "";
         batchCount = 0;
      }
   }

   if(bootstrap && sentCount == depth)
      AppendUniqueBrokerSymbol(gBarHistoryBootstrappedSymbols, logicalSym);
   if(sentCount > 0 && newestClosedIncluded)
      emittedClosedTime = newestClosedTime;
   if(bootstrap || VerboseBridgeLog)
      Print("[BRIDGE] bar history synced symbol=", logicalSym,
            " bars=", sentCount, " mode=", (bootstrap ? "bootstrap" : "incremental"));
   return(
      sentCount == depth && newestClosedIncluded &&
      (requiredClosedTime <= 0 || emittedClosedTime == requiredClosedTime)
   );
}

bool BuildExactFinalizedM1BatchEntry(
   string logicalSym,
   string brokerSym,
   datetime requiredClosedTime,
   int serverOffset,
   string &batchEntryJson,
   datetime &emittedClosedTime
) {
   batchEntryJson = "";
   emittedClosedTime = 0;
   if(
      StringLen(logicalSym) <= 0 || StringLen(brokerSym) <= 0 ||
      requiredClosedTime <= 0
   ) return false;

   // ExpectedFinalizedM1Ready selected the cached broker symbol and proved
   // shift 0/1 against this symbol's post-boundary broker tick. Do not force a
   // second asynchronous refresh here. Instead, copy the one immutable row and
   // immediately re-read iTime(shift 1); either disagreement fails closed.
   MqlRates bars[];
   ArraySetAsSeries(bars, true);
   ResetLastError();
   int copied = CopyRates(brokerSym, PERIOD_M1, 1, 1, bars);
   if(copied != 1) return false;
   MqlRates bar = bars[0];
   datetime finalShiftOneTime = iTime(brokerSym, PERIOD_M1, 1);
   if(
      bar.time <= 0 || bar.time != requiredClosedTime ||
      finalShiftOneTime != requiredClosedTime ||
      bar.open <= 0 || bar.high <= 0 || bar.low <= 0 || bar.close <= 0 ||
      bar.tick_volume < 0
   ) return false;

   int digits = (int)MarketInfo(brokerSym, MODE_DIGITS);
   double point = MarketInfo(brokerSym, MODE_POINT);
   if(digits < 0 || point <= 0.0) return false;
   double spreadPx = MathMax(
      0.0, MarketInfo(brokerSym, MODE_SPREAD) * point
   );
   double halfSpread = spreadPx / 2.0;
   int utcEpoch = (int)bar.time - serverOffset;
   int volume = (int)MathMax(
      0, MathMin(2147483647.0, (double)bar.tick_volume)
   );
   string row =
      "{\"time\":" + IntegerToString(utcEpoch) +
      ",\"open\":" + DoubleToString(bar.open + halfSpread, digits) +
      ",\"high\":" + DoubleToString(bar.high + halfSpread, digits) +
      ",\"low\":" + DoubleToString(bar.low + halfSpread, digits) +
      ",\"close\":" + DoubleToString(bar.close + halfSpread, digits) +
      ",\"bid_open\":" + DoubleToString(bar.open, digits) +
      ",\"bid_high\":" + DoubleToString(bar.high, digits) +
      ",\"bid_low\":" + DoubleToString(bar.low, digits) +
      ",\"bid_close\":" + DoubleToString(bar.close, digits) +
      ",\"spread\":" + DoubleToString(spreadPx, digits) +
      ",\"volume\":" + IntegerToString(volume) +
      ",\"volume_source\":\"mt4_ivolume_tick_count_v1\"" +
      ",\"price_basis\":\"mt4_bid_ohlc_v1\"}";
   batchEntryJson =
      "{\"symbol\":\"" + JsonEscape(logicalSym) +
      "\",\"timeframe\":\"M1\",\"bars\":[" + row + "]}";
   emittedClosedTime = bar.time;
   return true;
}

bool PublishPendingBarHistoryMinuteEdgeBatch(
   int &pendingCount,
   int &failedCount
) {
   pendingCount = gBarHistoryPendingRemaining;
   failedCount = 0;
   int count = ArraySize(gBarHistoryCacheLogicalSymbols);
   if(pendingCount <= 0) return true;
   if(
      count <= 0 ||
      count != ArraySize(gBarHistoryCacheBrokerSymbols) ||
      count != ArraySize(gBarHistoryPendingCompletedFlags) ||
      count != ArraySize(gBarHistoryEdgeStagedFlags) ||
      count != ArraySize(gBarHistoryEdgeStagedEntries) ||
      count != ArraySize(gBarHistoryEdgeStagedClosedTimes)
   ) {
      failedCount = pendingCount;
      return false;
   }

   int serverOffset = (int)(TimeCurrent() - TimeGMT());

   // MT4 executes every EA timer callback on its terminal thread. Probe only a
   // bounded slice on each 250 ms callback. Ready rows are retained by exact
   // cache index until the cursor reaches the end of the 22-symbol sweep, then
   // one atomic request admits the sweep. This removes five redundant network
   // transactions without marking any symbol complete before a 2xx response.
   int startIndex = gBarHistoryEdgeProbeCursor;
   if(startIndex < 0 || startIndex >= count) startIndex = 0;
   int indicesUntilSweepEnd = count - startIndex;
   int visitedCount = 0;
   int probedCount = 0;
   while(
      visitedCount < indicesUntilSweepEnd &&
      probedCount < SCALP_BAR_HISTORY_EDGE_PROBES_PER_PASS
   ) {
      int i = startIndex + visitedCount;
      visitedCount++;
      string logicalSym = gBarHistoryCacheLogicalSymbols[i];
      string brokerSym = gBarHistoryCacheBrokerSymbols[i];
      if(
         gBarHistoryPendingCompletedFlags[i] ||
         gBarHistoryEdgeStagedFlags[i]
      ) continue;
      probedCount++;
      if(StringLen(logicalSym) <= 0 || StringLen(brokerSym) <= 0) {
         failedCount++;
         continue;
      }

      datetime lastEmittedClosedTime = LastEmittedClosedBarTime(logicalSym);
      datetime expectedClosedTime = 0;
      string readinessReason = "not_pending";
      if(!ExpectedFinalizedM1Ready(
         logicalSym,
         brokerSym,
         lastEmittedClosedTime,
         expectedClosedTime,
         readinessReason
      )) {
         if(VerboseBridgeLog)
            Print("[BRIDGE] finalized M1 pending symbol=", logicalSym,
                  " reason=", readinessReason);
         continue;
      }

      string batchEntryJson = "";
      datetime emittedClosedTime = 0;
      if(!BuildExactFinalizedM1BatchEntry(
         logicalSym,
         brokerSym,
         expectedClosedTime,
         serverOffset,
         batchEntryJson,
         emittedClosedTime
       ) || emittedClosedTime != expectedClosedTime) {
         failedCount++;
         continue;
      }
      if(!StageBarHistoryEdgeEntry(i, batchEntryJson, emittedClosedTime)) {
         failedCount++;
         continue;
      }
   }
   gBarHistoryEdgeProbeCursor = (startIndex + visitedCount) % count;

   bool sweepComplete = visitedCount >= indicesUntilSweepEnd;
   bool everyPendingRowStaged =
      gBarHistoryEdgeStagedCount >= gBarHistoryPendingRemaining;
   if(
      gBarHistoryEdgeStagedCount <= 0 ||
      (!sweepComplete && !everyPendingRowStaged)
   ) return failedCount <= 0;

   string marketSourceFields = CurrentBrokerMarketSourceJsonFields();
   if(StringLen(marketSourceFields) <= 0) {
      failedCount += gBarHistoryEdgeStagedCount;
      return false;
   }
   string batchesJson = "";
   int includedCount = 0;
   for(int stagedIndex = 0; stagedIndex < count; stagedIndex++) {
      if(!gBarHistoryEdgeStagedFlags[stagedIndex]) continue;
      if(includedCount > 0) batchesJson = batchesJson + ",";
      batchesJson = batchesJson + gBarHistoryEdgeStagedEntries[stagedIndex];
      includedCount++;
   }
   if(
      includedCount <= 0 || includedCount != gBarHistoryEdgeStagedCount
   ) {
      failedCount += MathMax(1, gBarHistoryEdgeStagedCount);
      return false;
   }
   string payload =
      "{\"batches\":[" + batchesJson + "]" + marketSourceFields + "}";
   HttpPOST(ApiBase + BarHistoryBatchPath(), payload, gBridgeApiKey);
   int statusCode = LastBridgeHttpStatus();
   WarnAuthFailure("bar_history_edge_batch", statusCode);
   if(statusCode < 200 || statusCode >= 300) {
      // Atomic transport: none of the included symbols may advance locally
      // after any failed response. Retain the immutable staged rows so the next
      // callback retries the same one-frame proof without rebuilding 22 series.
      failedCount += includedCount;
      Print("[BRIDGE] finalized M1 edge batch failed status=", statusCode,
            " symbols=", includedCount);
      return false;
   }

   // This is deliberately the first mutation of per-symbol edge completion.
   // A 2xx response atomically admits every row in the one batch frame.
   for(int completeIndex = 0; completeIndex < count; completeIndex++) {
      if(!gBarHistoryEdgeStagedFlags[completeIndex]) continue;
      RecordEmittedClosedBarTime(
         gBarHistoryCacheLogicalSymbols[completeIndex],
         gBarHistoryEdgeStagedClosedTimes[completeIndex]
      );
      gBarHistoryPendingCompletedFlags[completeIndex] = true;
   }
   gBarHistoryPendingRemaining -= includedCount;
   if(gBarHistoryPendingRemaining < 0) gBarHistoryPendingRemaining = 0;
   pendingCount = gBarHistoryPendingRemaining;
   ClearBarHistoryEdgeStaging();
   if(VerboseBridgeLog)
      Print("[BRIDGE] finalized M1 edge batch synced symbols=", includedCount);
   return true;
}

void MaybeSyncBarHistory(bool force) {
   if(BarHistoryDepth <= 0) return;
   string cacheReason = "";
   // AGENT HOT PATH: preserve the 250 ms T+5 edge cadence without rebuilding
   // and quadratically validating the immutable configured symbol list.
   if(!BarHistorySymbolCacheFastValid(cacheReason)) {
      WarnBarHistorySymbolCache("edge_or_recovery:" + cacheReason);
      return;
   }
   int count = ArraySize(gBarHistoryCacheLogicalSymbols);
   // TimeCurrent can remain pinned to the last broker tick while OnTimer keeps
   // running. TimeGMT is the continuously advancing UTC wall clock, so the EA
   // no longer inherits whatever second within the minute it happened to start.
   datetime now = TimeGMT();
   if(now <= 0) now = TimeLocal();
   if(now <= 0) return;
   int minuteBucket = (int)(now / SCALP_BAR_HISTORY_RESYNC_SECS);
   int secondsIntoMinute = (int)(now % SCALP_BAR_HISTORY_RESYNC_SECS);
   bool fullWorkAllowed =
      secondsIntoMinute > SCALP_BAR_HISTORY_MAINTENANCE_START_SECS &&
      secondsIntoMinute < SCALP_BAR_HISTORY_MAINTENANCE_CUTOFF_SECS;
   int elapsedSinceLastAttempt = gLastBarHistoryAttempt > 0
      ? (int)(now - gLastBarHistoryAttempt)
      : 0;

   // A backwards workstation-clock correction must not suppress publication
   // until the old bucket is reached again. Treat the corrected bucket as the
   // new baseline; the following UTC minute edge will publish normally.
   if(gLastBarHistoryMinuteBucket > minuteBucket) {
      gLastBarHistoryMinuteBucket = minuteBucket;
      ClearBarHistoryMinuteEdge();
   }

   bool newMinuteEdge =
      !force && gLastBarHistoryMinuteBucket >= 0 &&
      minuteBucket > gLastBarHistoryMinuteBucket &&
      secondsIntoMinute >= SCALP_BAR_HISTORY_MINUTE_EDGE_DELAY_SECS;
   bool supersededPendingEdge =
      newMinuteEdge &&
      gBarHistoryPendingMinuteBucket > gLastBarHistoryMinuteBucket &&
      gBarHistoryPendingMinuteBucket < minuteBucket;
   int schedulerReferenceBucket = supersededPendingEdge
      ? gBarHistoryPendingMinuteBucket
      : gLastBarHistoryMinuteBucket;
   bool schedulerWallClockGap =
      newMinuteEdge && schedulerReferenceBucket >= 0 &&
      (minuteBucket - schedulerReferenceBucket) >
         SCALP_BAR_HISTORY_INCREMENTAL_DEPTH;
   if(schedulerWallClockGap) {
      // A real multi-minute timer/wall-clock jump cannot be covered by the
      // one-row incremental contract. Preserve the existing all-scope bounded
      // recovery for that case only, then baseline the current edge scheduler.
      ResetBarHistoryBootstrap("scheduler_wall_clock_gap");
      ClearBarHistoryMinuteEdge();
      gLastBarHistoryMinuteBucket = minuteBucket - 1;
   } else if(supersededPendingEdge)
      RetireSupersededBarHistoryMinuteEdge(minuteBucket);
   if(newMinuteEdge && gBarHistoryPendingMinuteBucket != minuteBucket)
      BeginBarHistoryMinuteEdge(minuteBucket);

   bool pendingMinuteEdge =
      !force && gBarHistoryPendingMinuteBucket > gLastBarHistoryMinuteBucket;
   datetime pendingMinuteStart = pendingMinuteEdge
      ? (datetime)(
         gBarHistoryPendingMinuteBucket * SCALP_BAR_HISTORY_RESYNC_SECS
      )
      : 0;
   bool withinMinuteEdgeDeadline =
      pendingMinuteEdge && now >= pendingMinuteStart +
         SCALP_BAR_HISTORY_MINUTE_EDGE_DELAY_SECS &&
      now <= pendingMinuteStart +
         SCALP_BAR_HISTORY_MINUTE_EDGE_DEADLINE_SECS;
   bool lateReconciliationDue =
      pendingMinuteEdge && now > pendingMinuteStart +
         SCALP_BAR_HISTORY_MINUTE_EDGE_DEADLINE_SECS &&
      now >= gBarHistoryPendingNextLateRetryAt;
   bool minuteEdgeDue =
      withinMinuteEdgeDeadline || lateReconciliationDue;
   if(lateReconciliationDue)
      gBarHistoryPendingNextLateRetryAt =
         now + SCALP_BAR_HISTORY_LATE_RETRY_SECS;
   // The API seed probe is deliberately a post-edge maintenance operation; a
   // synchronous GET can never consume the +1..+4 publication window.
   bool remoteProbeWorkDue =
      !force && !minuteEdgeDue && fullWorkAllowed &&
      gBarHistoryRemoteProbePending;
   bool fullRecoveryDue =
      !force && !minuteEdgeDue && fullWorkAllowed &&
      gBarHistoryFullRecoveryRequested;

   int elapsedSinceInitialBootstrap = gBarHistoryInitialBootstrapAt > 0
      ? (int)(now - gBarHistoryInitialBootstrapAt)
      : 0;
   bool delayedRecoveryReady =
      !force && gBarHistoryDelayedRecoverySweepsRemaining > 0 &&
      gBarHistoryInitialBootstrapAt > 0 &&
      elapsedSinceInitialBootstrap >= SCALP_BAR_HISTORY_DELAYED_RECOVERY_SECS;
   // Never let the expensive one-time cold-history sweep take a minute-edge
   // attempt. Prove and publish the exact shift-1 row first; recovery runs
   // on a later timer and can only reconcile data, never authorize a late trade.
   bool delayedRecoverySweep =
      delayedRecoveryReady && !minuteEdgeDue && fullWorkAllowed;
   bool coldBootstrapDue =
      !force && fullWorkAllowed && gLastBarHistoryAttempt <= 0;
   bool coldRetryDue =
      !force && fullWorkAllowed &&
      ArraySize(gBarHistoryBootstrappedSymbols) < count &&
      gLastBarHistoryAttempt > 0 &&
      elapsedSinceLastAttempt >= SCALP_BAR_HISTORY_RESYNC_SECS;
   if(
      !force && !minuteEdgeDue && !delayedRecoverySweep &&
      !coldBootstrapDue && !coldRetryDue &&
      !remoteProbeWorkDue && !fullRecoveryDue
   ) return;
   if(
      !force && !minuteEdgeDue && !remoteProbeWorkDue &&
      gBarHistoryStabilityNextCheckAt > 0 &&
      now < gBarHistoryStabilityNextCheckAt
   ) return;

   gLastBarHistoryAttempt = now;
   if(gBarHistoryInitialBootstrapAt <= 0)
      gBarHistoryInitialBootstrapAt = now;

   if(minuteEdgeDue) {
      int edgePendingCount = 0;
      int edgeFailedCount = 0;
      PublishPendingBarHistoryMinuteEdgeBatch(
         edgePendingCount, edgeFailedCount
      );
      bool pendingComplete =
         pendingMinuteEdge &&
         gBarHistoryPendingRemaining == 0;
      if(pendingComplete) {
         gLastBarHistoryMinuteBucket = gBarHistoryPendingMinuteBucket;
         ClearBarHistoryMinuteEdge();
      }
      if(edgeFailedCount > 0 && VerboseBridgeLog)
         Print("[BRIDGE] bar history retry pending symbols=", edgeFailedCount);
      if(
         edgePendingCount > 0 &&
         gBarHistoryIncompleteNoticeBucket != gBarHistoryPendingMinuteBucket &&
         (secondsIntoMinute >= SCALP_BAR_HISTORY_MINUTE_EDGE_DEADLINE_SECS ||
           lateReconciliationDue)
       ) {
          gBarHistoryIncompleteNoticeBucket = gBarHistoryPendingMinuteBucket;
          Print("[BRIDGE] finalized M1 edge incomplete pending_symbols=",
                edgePendingCount,
                " bucket=", gBarHistoryPendingMinuteBucket,
                " late_reconciliation=",
                (lateReconciliationDue ? "true" : "false"));
       }
      return;
   }

   // API bar history is intentionally process-local. Probe one already seeded
   // pair before the cheap incremental pass so an API restart triggers one
   // complete exact-scope reseed even if its outage fell between EA timers.
   if(remoteProbeWorkDue) {
      gBarHistoryRemoteProbePending = false;
      if(ArraySize(gBarHistoryBootstrappedSymbols) > 0) {
         string probeSymbol = gBarHistoryBootstrappedSymbols[0];
         string probeBrokerSymbol = CachedBarHistoryBrokerSymbol(probeSymbol);
         datetime probeCoveredClosedTime = 0;
         RemoteBarHistoryCoverageOutcome remoteProbeOutcome =
            ProbeRemoteBarHistoryCoverage(
               probeSymbol,
               probeBrokerSymbol,
               probeCoveredClosedTime
            );
         // Only an authenticated valid-schema ready=false response proves that
         // the API's process-local seed disappeared. Transport/auth failures or
         // a retained-but-lagging latest row retry next minute without throwing
         // away all 22 symbols' completed bootstrap state.
         if(
            remoteProbeOutcome == BAR_HISTORY_REMOTE_COVERAGE_MISSING
         )
            ResetBarHistoryBootstrap("remote_seed_missing");
      }
      return;
   }

   bool stabilityCheckDue =
      gBarHistoryStabilityNextCheckAt <= 0 ||
      now >= gBarHistoryStabilityNextCheckAt;

   int failedCount = 0;
   int recoveryPendingCount = 0;
   bool stabilityWaitPending = false;
   bool recoveryWorkActive =
      !minuteEdgeDue && fullWorkAllowed &&
      (
         force || delayedRecoverySweep || coldBootstrapDue || coldRetryDue ||
         fullRecoveryDue
   );
   int recoveryWorkAttempts = 0;
   int recoveryScanStart = gBarHistoryRecoveryUploadCursor;
   if(recoveryWorkActive) gBarHistoryRecoveryTransportAbort = false;
   for(int scanOffset = 0; scanOffset < count; scanOffset++) {
      int i = recoveryWorkActive
         ? (recoveryScanStart + scanOffset) % count
         : scanOffset;
      string logicalSym = gBarHistoryCacheLogicalSymbols[i];
      string brokerSym = gBarHistoryCacheBrokerSymbols[i];
      if(StringLen(logicalSym) <= 0 || StringLen(brokerSym) <= 0) {
         failedCount++;
         if(recoveryWorkActive) {
            recoveryPendingCount++;
            gBarHistoryRecoveryUploadCursor = (i + 1) % count;
         }
         continue;
      }

      // Edge publication returned above. This loop is only the cold/full
      // per-symbol recovery lane and never falls back to per-symbol edge POSTs.
      bool bootstrap =
         recoveryWorkActive &&
         !ContainsSymbol(gBarHistoryBootstrappedSymbols, logicalSym);
      if(!bootstrap) continue;
      if(bootstrap && !stabilityCheckDue) {
         recoveryPendingCount++;
         continue;
      }
      // A cold pass used to fingerprint all 22 * 241 rows before applying the
      // one-upload limit below. That monopolized MT4's UI/timer thread after
      // every terminal restart. Bound the complete remote-check/CopyRates/
      // upload unit instead, and rotate fairly across the exact scope.
      if(
         bootstrap &&
         recoveryWorkAttempts >= SCALP_BAR_HISTORY_FULL_UPLOADS_PER_PASS
      ) {
         recoveryPendingCount++;
         continue;
      }
      if(bootstrap) {
         recoveryWorkAttempts++;
         // A terminal-only restart must reuse the authenticated direct-M1
         // seed already held by the still-running bridge. Reposting 241 rows
         // can collide with their immutable first-observation bytes and keep
         // MT4 in an endless 409/rebuild loop. Require the full direct depth
         // and the exact current shift-1 timestamp before trusting that seed.
         datetime remoteCoveredClosedTime = 0;
         RemoteBarHistoryCoverageOutcome remoteCoverageOutcome =
            ProbeRemoteBarHistoryCoverage(
               logicalSym,
               brokerSym,
               remoteCoveredClosedTime
            );
         if(
            remoteCoverageOutcome == BAR_HISTORY_REMOTE_COVERAGE_EXACT
         ) {
            AppendUniqueBrokerSymbol(
               gBarHistoryBootstrappedSymbols,
               logicalSym
            );
            RecordEmittedClosedBarTime(
               logicalSym,
               remoteCoveredClosedTime
            );
            RetireBarHistoryRecoveryState(logicalSym);
            gBarHistoryRecoveryUploadCursor = (i + 1) % count;
            break;
         }
      }
      BarHistoryObservationOutcome observationOutcome =
         BAR_HISTORY_OBSERVATION_INCOHERENT;
      if(bootstrap)
         observationOutcome = ObserveStableDirectM1History(
            logicalSym,
            brokerSym,
            now
         );
      if(
         bootstrap &&
         observationOutcome == BAR_HISTORY_OBSERVATION_WAIT_STABLE
      ) {
         // Pin one coherent first fingerprint so the same symbol receives its
         // >=5-second stability recheck. The bounded outcome above releases a
         // repeatedly changing candidate after its second pinned recheck.
         gBarHistoryRecoveryUploadCursor = i;
         stabilityWaitPending = true;
         recoveryPendingCount++;
         break;
      }
      if(
         bootstrap &&
         observationOutcome == BAR_HISTORY_OBSERVATION_INCOHERENT
      ) {
         // Hard/off-chart symbols stay eligible but never starve the remaining
         // exact scope: the next bounded pass starts at the following symbol.
         gBarHistoryRecoveryUploadCursor = (i + 1) % count;
         recoveryPendingCount++;
         break;
      }
      datetime emittedClosedTime = 0;
      // A cold or unavailable symbol must not starve every later pair.  Failed
      // symbols remain eligible on the next bounded resync cadence.
      bool historySent = SendBarHistoryForSymbol(
          logicalSym,
          brokerSym,
          true,
         0,
         emittedClosedTime
      );
      if(bootstrap) gBarHistoryRecoveryUploadCursor = (i + 1) % count;
      if(!historySent) {
         // A shift-1 transport retry must stay one row. Only a failed full
         // bootstrap invalidates that symbol's seeded-history state.
         if(bootstrap) {
            ForgetBarHistoryBootstrap(logicalSym);
            recoveryPendingCount++;
         }
         failedCount++;
         if(bootstrap && gBarHistoryRecoveryTransportAbort) break;
         break;
      }
      RetireBarHistoryRecoveryState(logicalSym);
      // A background full recovery may overlap an incomplete edge. Do not let
      // that maintenance emission advance edge-dedup state: the next bounded
      // or late edge attempt must still prove and repost the exact shift-1 row.
      if(!pendingMinuteEdge)
         RecordEmittedClosedBarTime(logicalSym, emittedClosedTime);
      break;
   }
   // Only a coherent first fingerprint needs the five-second clock. Hard or
   // incoherent symbols advance on the next maintenance callback instead of
   // multiplying that delay across the exact 22-symbol scope.
   if(stabilityWaitPending && stabilityCheckDue)
      gBarHistoryStabilityNextCheckAt =
          now + SCALP_BAR_HISTORY_STABILITY_CHECK_SECS;
   else if(!stabilityWaitPending && stabilityCheckDue)
      gBarHistoryStabilityNextCheckAt = 0;
   if(recoveryWorkActive && gBarHistoryRecoveryTransportAbort) {
      gBarHistoryStabilityNextCheckAt =
         now + SCALP_BAR_HISTORY_RESYNC_SECS;
      Print("[BRIDGE] cold M1 recovery pass aborted after common transport failure");
   }
   bool recoveryComplete =
      ArraySize(gBarHistoryBootstrappedSymbols) == count;
   if(recoveryComplete) {
      gBarHistoryFullRecoveryRequested = false;
      gBarHistoryDelayedRecoverySweepsRemaining = 0;
      gBarHistoryStabilityNextCheckAt = 0;
   }
   if(
      !pendingMinuteEdge && recoveryComplete &&
      (force || coldBootstrapDue || coldRetryDue ||
       (delayedRecoverySweep && gLastBarHistoryMinuteBucket < 0))
   ) gLastBarHistoryMinuteBucket = minuteBucket;
   if(failedCount > 0)
      Print("[BRIDGE] bar history retry pending symbols=", failedCount);
   if(VerboseBridgeLog && recoveryPendingCount > 0)
      Print("[BRIDGE] stable cold M1 recovery pending symbols=",
            recoveryPendingCount);
}

uint AckOutboxHash(string value) {
   uint hash=5381;
   int length=StringLen(value);
   for(int i=0; i<length; i++) {
      hash=((hash<<5)+hash)^(uint)StringGetCharacter(value,i);
   }
   return(hash);
}

bool EnsureSignalOutcomeJournalReady() {
   gSignalOutcomeJournalReady=false;
   if(!ProducerInstanceIdValid(gBridgeProducerInstanceId)) return(false);
   // FileOpen(FILE_WRITE) creates missing subfolders, while this explicit call
   // documents that the journal is terminal-local (common_flag=0).
   ResetLastError();
   FolderCreate(SIGNAL_OUTCOME_DIRECTORY,0);

   string token=
      IntegerToString((int)AckOutboxHash(gBridgeProducerInstanceId))+"_"+
      IntegerToString((int)GetTickCount())+"_"+
      IntegerToString((int)ChartID());
   string tempPath=SIGNAL_OUTCOME_DIRECTORY+"\\probe_"+token+".tmp";
   string finalPath=SIGNAL_OUTCOME_DIRECTORY+"\\probe_"+token+".ack";
   string expected="fxstack-signal-outcome-journal-v1";
   ResetLastError();
   int handle=FileOpen(tempPath,FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(handle==INVALID_HANDLE) return(false);
   uint written=FileWriteString(handle,expected);
   FileFlush(handle);
   FileClose(handle);
   if(written!=(uint)StringLen(expected)) {
      FileDelete(tempPath);
      return(false);
   }
   if(!FileMove(tempPath,0,finalPath,0)) {
      FileDelete(tempPath);
      return(false);
   }
   string observed="";
   handle=FileOpen(finalPath,FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ);
   if(handle==INVALID_HANDLE) {
      FileDelete(finalPath);
      return(false);
   }
   while(!FileIsEnding(handle)) observed+=FileReadString(handle);
   FileClose(handle);
   bool removed=FileDelete(finalPath);
   if(observed!=expected || !removed) return(false);
   gSignalOutcomeJournalReady=true;
   return(true);
}

string SignalOutcomePath(string signalId) {
   if(
      StringLen(signalId)<=0 || AccountNumber()<=0 ||
      StringLen(StringTrim(AccountServer()))<=0 ||
      !ProducerInstanceIdValid(gBridgeProducerInstanceId) ||
      !gSignalOutcomeJournalReady || gSignalOutcomeJournalBlocked
   ) return("");
   string scope=
      IntegerToString(AccountNumber())+"|"+StringTrim(AccountServer())+"|"+
      IntegerToString(Magic)+"|"+gBridgeProducerInstanceId;
   return(
      SIGNAL_OUTCOME_DIRECTORY+"\\outcome_"+
      IntegerToString((int)AckOutboxHash(scope))+"_"+
      IntegerToString((int)AckOutboxHash(signalId))+"_"+
      IntegerToString(StringLen(signalId))+".ack"
   );
}

bool ReadSignalOutcomePayload(string signalId,string &payload,bool &exists) {
   payload="";
   exists=false;
   string path=SignalOutcomePath(signalId);
   if(StringLen(path)<=0) return(false);
   exists=FileIsExist(path);
   if(!exists) return(true);

   ResetLastError();
   int handle=FileOpen(path,FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ);
   if(handle==INVALID_HANDLE) return(false);
   while(!FileIsEnding(handle)) payload+=FileReadString(handle);
   FileClose(handle);
   string identity="\"command_id\":\""+JsonEscape(signalId)+"\"";
   return(
      StringLen(payload)>=2 && StringSubstr(payload,0,1)=="{" &&
      StringSubstr(payload,StringLen(payload)-1,1)=="}" &&
      StringFind(payload,identity,0)>=0
   );
}

// Preserve the first effective outcome in terminal-local MQL Files.  This is
// separate from the FILE_COMMON HTTP outbox: the outbox is deleted after 2xx,
// while this record prevents a post-restart duplicate from reaching OrderSend.
bool PersistSignalOutcomePayload(string signalId,string payload) {
   string path=SignalOutcomePath(signalId);
   if(StringLen(path)<=0 || StringLen(payload)<=0) return(false);

   string existing="";
   bool exists=false;
   if(!ReadSignalOutcomePayload(signalId,existing,exists)) return(false);
   if(exists) return(existing==payload);

   string tempPath=path+"."+IntegerToString((int)GetTickCount())+".tmp";
   ResetLastError();
   int handle=FileOpen(tempPath,FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(handle==INVALID_HANDLE) return(false);
   uint written=FileWriteString(handle,payload);
   FileFlush(handle);
   FileClose(handle);
   if(written!=(uint)StringLen(payload)) {
      FileDelete(tempPath);
      return(false);
   }
   if(!FileMove(tempPath,0,path,0)) {
      FileDelete(tempPath);
      // Another chart may have won the first-outcome race.  Only byte-for-byte
      // equality is safe; any divergent outcome is execution uncertainty.
      existing="";
      exists=false;
      return(
         ReadSignalOutcomePayload(signalId,existing,exists) &&
         exists && existing==payload
      );
   }
   existing="";
   exists=false;
   return(
      ReadSignalOutcomePayload(signalId,existing,exists) &&
      exists && existing==payload
   );
}

bool ProducerInstanceIdValid(string value) {
   string token=StringTrim(value);
   int length=StringLen(token);
   if(length<16 || length>128) return(false);
   for(int i=0; i<length; i++) {
      int ch=StringGetCharacter(token,i);
      bool valid=(ch>=48 && ch<=57) || (ch>=65 && ch<=90) ||
                 (ch>=97 && ch<=122) || ch==45 || ch==46 || ch==95;
      if(!valid) return(false);
   }
   return(true);
}

bool ReadProducerInstanceIdFile(string &value) {
   value="";
   ResetLastError();
   int handle=FileOpen(
      PRODUCER_INSTANCE_ID_FILE,
      FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ
   );
   if(handle==INVALID_HANDLE) return(false);
   value=StringTrim(FileReadString(handle));
   FileClose(handle);
   return(ProducerInstanceIdValid(value));
}

string GenerateProducerInstanceId() {
   string terminalDataPath=StringTrim(TerminalInfoString(TERMINAL_DATA_PATH));
   if(StringLen(terminalDataPath)<=0) return("");
   string entropy=terminalDataPath+"|"+StringTrim(AccountServer())+"|"+
                  IntegerToString((int)TimeLocal())+"|"+
                  IntegerToString((int)GetTickCount())+"|"+
                  IntegerToString((int)ChartID());
   return(
      "mt4-"+IntegerToString((int)AckOutboxHash(terminalDataPath))+"-"+
      IntegerToString((int)AckOutboxHash(entropy))+"-"+
      IntegerToString((int)AckOutboxHash(entropy+"|producer-instance-v1"))
   );
}

string LoadOrCreateProducerInstanceId() {
   string existing="";
   if(FileIsExist(PRODUCER_INSTANCE_ID_FILE)) {
      if(ReadProducerInstanceIdFile(existing)) return(existing);
      Print(
         "[BRIDGE] producer instance ID exists but is unreadable or invalid; "
         "authenticated bridge channels remain fenced"
      );
      return("");
   }

   string candidate=GenerateProducerInstanceId();
   if(!ProducerInstanceIdValid(candidate)) {
      Print("[BRIDGE] unable to generate producer instance ID");
      return("");
   }
   string tempPath=PRODUCER_INSTANCE_ID_FILE+"."+
                   IntegerToString((int)AckOutboxHash((string)ChartID()))+".tmp";
   ResetLastError();
   int handle=FileOpen(tempPath,FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(handle==INVALID_HANDLE) {
      Print("[BRIDGE] producer instance ID create failed err=",GetLastError());
      return("");
   }
   uint written=FileWriteString(handle,candidate);
   FileFlush(handle);
   FileClose(handle);
   if(written!=(uint)StringLen(candidate)) {
      FileDelete(tempPath);
      Print("[BRIDGE] producer instance ID write incomplete");
      return("");
   }
   if(!FileMove(tempPath,0,PRODUCER_INSTANCE_ID_FILE,0)) {
      FileDelete(tempPath);
      // Two charts in one terminal may race during first creation. The first
      // durable local value wins and all charts converge on it.
      if(ReadProducerInstanceIdFile(existing)) return(existing);
      Print("[BRIDGE] producer instance ID promote failed err=",GetLastError());
      return("");
   }
   string verified="";
   if(!ReadProducerInstanceIdFile(verified) || verified!=candidate) {
      Print("[BRIDGE] producer instance ID readback verification failed");
      return("");
   }
   return(verified);
}

// ACK files use FILE_COMMON for durable replay but are isolated by a pinned
// broker account/server, exact bridge endpoint, Magic, and the terminal-local
// producer instance ID. ApiKey is deliberately excluded. A disconnected
// account is never allowed to create an account_0 scope that could orphan an
// outcome after login becomes available.
bool TryPinAckOutboxScopeIdentity() {
   if(gAckScopePinned) return(true);
   int accountNumber=AccountNumber();
   string accountServer=StringTrim(AccountServer());
   string endpoint=StringTrim(ApiBase);
   string terminalDataPath=StringTrim(TerminalInfoString(TERMINAL_DATA_PATH));
   if(
      accountNumber<=0 || StringLen(accountServer)<=0 ||
      StringLen(endpoint)<=0 || StringLen(terminalDataPath)<=0 ||
      !ProducerInstanceIdValid(gBridgeProducerInstanceId)
   ) {
      BlockAckOutbox("scope_identity_unavailable_account_or_server_or_endpoint_or_terminal_or_producer");
      return(false);
   }

   gAckScopeAccountNumber=accountNumber;
   gAckScopeAccountServer=accountServer;
   gAckScopeApiBase=endpoint;
   gAckScopeMagic=Magic;
   gAckScopeTerminalDataPath=terminalDataPath;
   gAckScopeTerminalToken=IntegerToString((int)AckOutboxHash(terminalDataPath));
   gAckScopeProducerInstanceId=gBridgeProducerInstanceId;
   string endpointToken=IntegerToString((int)AckOutboxHash(endpoint));
   string serverToken=IntegerToString((int)AckOutboxHash(accountServer));
   string producerToken=IntegerToString((int)AckOutboxHash(gBridgeProducerInstanceId));
   gAckScopeDirectory=
      "FXStack\\AckOutbox\\account_"+IntegerToString(accountNumber)+
      "_server_"+serverToken+
      "\\endpoint_"+endpointToken+"_"+IntegerToString(StringLen(endpoint))+
      "_magic_"+IntegerToString(Magic)+
      "\\producer_"+producerToken;
   gAckScopePinned=true;
   Print(
      "[ACK_OUTBOX] scope pinned account=",gAckScopeAccountNumber,
      " server=",gAckScopeAccountServer,
      " endpoint=",gAckScopeApiBase,
      " magic=",gAckScopeMagic,
      " terminal=",gAckScopeTerminalToken,
      " producer=",gAckScopeProducerInstanceId
   );
   return(true);
}

bool AckOutboxScopeIdentityMatches(string &reason) {
   reason="";
   if(!gAckScopePinned) {
      reason="scope_identity_not_pinned";
      return(false);
   }
   if(AccountNumber()!=gAckScopeAccountNumber) {
      reason="scope_account_changed";
      return(false);
   }
   if(StringTrim(AccountServer())!=gAckScopeAccountServer) {
      reason="scope_server_changed";
      return(false);
   }
   if(StringTrim(ApiBase)!=gAckScopeApiBase) {
      reason="scope_endpoint_changed";
      return(false);
   }
   if(Magic!=gAckScopeMagic) {
      reason="scope_magic_changed";
      return(false);
   }
   if(StringTrim(TerminalInfoString(TERMINAL_DATA_PATH))!=gAckScopeTerminalDataPath) {
      reason="scope_terminal_instance_changed";
      return(false);
   }
   if(
      !ProducerInstanceIdValid(gBridgeProducerInstanceId) ||
      gBridgeProducerInstanceId!=gAckScopeProducerInstanceId
   ) {
      reason="scope_producer_instance_changed";
      return(false);
   }
   return(true);
}

string AckOutboxScopeDirectory() {
   return(gAckScopePinned ? gAckScopeDirectory : "");
}

int AckOutboxCountPattern(string pattern,int stopAfter) {
   string found="";
   string directory=AckOutboxScopeDirectory();
   if(StringLen(directory)<=0) return(0);
   string filter=directory+"\\"+pattern;
   long handle=FileFindFirst(filter,found,FILE_COMMON);
   if(handle==INVALID_HANDLE) return(0);
   int count=0;
   do {
      count++;
      if(stopAfter>0 && count>=stopAfter) break;
   } while(FileFindNext(handle,found));
   FileFindClose(handle);
   return(count);
}

int AckOutboxPendingCount() {
   return(AckOutboxCountPattern("*.ack",ACK_OUTBOX_MAX_PENDING+1));
}

int AckOutboxStagedCount() {
   return(AckOutboxCountPattern("*.tmp",ACK_OUTBOX_MAX_PENDING+1));
}

int AckOutboxStoredCount() {
   return(AckOutboxPendingCount()+AckOutboxStagedCount());
}

void BlockAckOutbox(string reason) {
   gAckOutboxBlocked=true;
   datetime now=TimeLocal();
   if((now-gLastAckOutboxWarnTs)<5) return;
   gLastAckOutboxWarnTs=now;
   string concise=reason;
   if(StringLen(concise)>180) concise=StringSubstr(concise,0,180);
   Print(
      "[ACK_OUTBOX] BLOCKED reason=",concise,
      " pending=",AckOutboxPendingCount(),
      " staged=",AckOutboxStagedCount(),
      " unpersisted=",gAckUnpersistedCount,
      " scope=",(gAckScopePinned ? AckOutboxScopeDirectory() : "<unavailable>")
   );
   UpdateDashboard(
      "ACK OUTBOX BLOCKED|"+concise+
      "|Pending ACKs must reach HTTP 2xx before command polling resumes"
   );
}

int AckOutboxListPattern(string pattern,string &paths[]) {
   ArrayResize(paths,0);
   string found="";
   string directory=AckOutboxScopeDirectory();
   if(StringLen(directory)<=0) return(0);
   long handle=FileFindFirst(directory+"\\"+pattern,found,FILE_COMMON);
   if(handle==INVALID_HANDLE) return(0);
   int count=0;
   do {
      if(count>=ACK_OUTBOX_MAX_PENDING+ACK_OUTBOX_UNPERSISTED_MAX+1) break;
      ArrayResize(paths,count+1);
      paths[count]=directory+"\\"+found;
      count++;
   } while(FileFindNext(handle,found));
   FileFindClose(handle);
   return(count);
}

bool AllocateAckOutboxPaths(string payload,string &tmpPath,string &finalPath) {
   tmpPath="";
   finalPath="";
   string directory=AckOutboxScopeDirectory();
   if(StringLen(directory)<=0) {
      BlockAckOutbox("scope_identity_not_pinned");
      return(false);
   }
   string payloadToken=IntegerToString((int)AckOutboxHash(payload));
   string chartToken=IntegerToString((int)AckOutboxHash((string)ChartID()));
   for(int attempt=0; attempt<64; attempt++) {
      gAckOutboxSequence++;
      string token=
         IntegerToString((int)TimeLocal())+"_"+
         IntegerToString((int)GetTickCount())+"_"+
         IntegerToString(gAckOutboxSequence)+"_"+
         gAckScopeTerminalToken+"_"+chartToken+"_"+payloadToken;
      string candidateBase=directory+"\\ack_"+token;
      string candidateTmp=candidateBase+".tmp";
      string candidateFinal=candidateBase+".ack";
      if(
         !FileIsExist(candidateTmp,FILE_COMMON) &&
         !FileIsExist(candidateFinal,FILE_COMMON)
      ) {
         tmpPath=candidateTmp;
         finalPath=candidateFinal;
         return(true);
      }
   }
   BlockAckOutbox("unique_filename_exhausted");
   return(false);
}

bool ReadAckOutboxPayload(string path,string &payload) {
   payload="";
   ResetLastError();
   int handle=FileOpen(
      path,
      FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ
   );
   if(handle==INVALID_HANDLE) {
      BlockAckOutbox("read_failed err="+IntegerToString(GetLastError()));
      return(false);
   }
   while(!FileIsEnding(handle)) payload+=FileReadString(handle);
   FileClose(handle);
   if(
      StringLen(payload)<2 ||
      StringSubstr(payload,0,1)!="{" ||
      StringSubstr(payload,StringLen(payload)-1,1)!="}"
   ) {
      BlockAckOutbox("queued_payload_invalid file="+path);
      return(false);
   }
   return(true);
}

// Returns true only when the flushed temporary file was atomically promoted
// to a replayable .ack file. `durable` is also true when a flushed .tmp remains
// after a rename failure, so the caller must not replace it with a direct POST.
bool PersistAckPayloadBeforePost(string payload,string &finalPath,bool &durable) {
   finalPath="";
   durable=false;
   if(!TryPinAckOutboxScopeIdentity()) return(false);
   if(StringLen(payload)<=0) {
      BlockAckOutbox("empty_ack_payload");
      return(false);
   }
   if(AckOutboxStoredCount()>=ACK_OUTBOX_MAX_PENDING) {
      BlockAckOutbox("capacity_exhausted max="+IntegerToString(ACK_OUTBOX_MAX_PENDING));
      return(false);
   }

   string tmpPath="";
   if(!AllocateAckOutboxPaths(payload,tmpPath,finalPath)) return(false);
   ResetLastError();
   int handle=FileOpen(tmpPath,FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(handle==INVALID_HANDLE) {
      BlockAckOutbox("persist_open_failed err="+IntegerToString(GetLastError()));
      finalPath="";
      return(false);
   }
   uint written=FileWriteString(handle,payload);
   FileFlush(handle);
   FileClose(handle);
   if(written!=(uint)StringLen(payload)) {
      BlockAckOutbox(
         "persist_write_incomplete written="+IntegerToString((int)written)+
         " expected="+IntegerToString(StringLen(payload))+
         " err="+IntegerToString(GetLastError())
      );
      finalPath="";
      return(false);
   }
   durable=true;
   if(!FileMove(tmpPath,FILE_COMMON,finalPath,FILE_COMMON)) {
      BlockAckOutbox("persist_promote_failed err="+IntegerToString(GetLastError()));
      finalPath="";
      return(false);
   }
   return(true);
}

bool RetainUnpersistedAckPayload(string payload) {
   if(gAckUnpersistedCount>=ACK_OUTBOX_UNPERSISTED_MAX) {
      BlockAckOutbox(
         "unpersisted_memory_capacity_exhausted max="+
         IntegerToString(ACK_OUTBOX_UNPERSISTED_MAX)
      );
      return(false);
   }
   ArrayResize(gAckUnpersistedPayloads,gAckUnpersistedCount+1);
   gAckUnpersistedPayloads[gAckUnpersistedCount]=payload;
   gAckUnpersistedCount++;
   BlockAckOutbox("ack_not_yet_durable");
   return(true);
}

void RemoveUnpersistedAckPayload(int index) {
   if(index<0 || index>=gAckUnpersistedCount) return;
   for(int i=index+1; i<gAckUnpersistedCount; i++) {
      gAckUnpersistedPayloads[i-1]=gAckUnpersistedPayloads[i];
   }
   gAckUnpersistedCount--;
   ArrayResize(gAckUnpersistedPayloads,gAckUnpersistedCount);
}

int FlushUnpersistedAckPayloads(int limit) {
   int processed=0;
   while(gAckUnpersistedCount>0 && processed<limit) {
      string finalPath="";
      bool durable=false;
      bool promoted=PersistAckPayloadBeforePost(
         gAckUnpersistedPayloads[0],finalPath,durable
      );
      if(promoted || durable) {
         RemoveUnpersistedAckPayload(0);
         processed++;
         continue;
      }
      break;
   }
   return(processed);
}

int RecoverAckOutboxTemps(int limit) {
   string paths[];
   int count=AckOutboxListPattern("*.tmp",paths);
   int recovered=0;
   for(int i=0; i<count && recovered<limit; i++) {
      string payload="";
      if(!ReadAckOutboxPayload(paths[i],payload)) continue;
      string finalPath=StringSubstr(paths[i],0,StringLen(paths[i])-4)+".ack";
      if(FileIsExist(finalPath,FILE_COMMON)) {
         string unusedTmp="";
         if(!AllocateAckOutboxPaths(payload,unusedTmp,finalPath)) continue;
      }
      if(FileMove(paths[i],FILE_COMMON,finalPath,FILE_COMMON)) {
         recovered++;
      } else {
         BlockAckOutbox("temp_recovery_failed err="+IntegerToString(GetLastError()));
      }
   }
   return(recovered);
}

bool AckHttpStatusIsSuccess(int statusCode) {
   return(statusCode>=200 && statusCode<300);
}

bool ReplayAckOutboxFile(string path) {
   if(!TryPinAckOutboxScopeIdentity()) return(false);
   string payload="";
   if(!ReadAckOutboxPayload(path,payload)) return(false);
   HttpPOST(gAckScopeApiBase+AckPath(),payload,gBridgeCommandToken);
   int statusCode=LastBridgeHttpStatus();
   WarnAuthFailure("ack_replay",statusCode);
   if(!AckHttpStatusIsSuccess(statusCode)) {
      BlockAckOutbox("replay_http_status="+IntegerToString(statusCode));
      return(false);
   }

   // Dequeue only after HTTP 2xx. If another EA instance already removed the
   // shared file after the same idempotent replay, absence is also success.
   if(FileDelete(path,FILE_COMMON) || !FileIsExist(path,FILE_COMMON)) return(true);
   BlockAckOutbox("dequeue_after_2xx_failed err="+IntegerToString(GetLastError()));
   return(false);
}

int ReplayAckOutbox(int limit) {
   if(limit<=0) return(0);
   string paths[];
   int count=AckOutboxListPattern("*.ack",paths);
   if(count<=0) return(0);
   int attempts=limit;
   if(attempts>count) attempts=count;
   int start=gAckReplayCursor%count;
   for(int i=0; i<attempts; i++) {
      int index=(start+i)%count;
      ReplayAckOutboxFile(paths[index]);
   }
   gAckReplayCursor=(start+attempts)%count;
   return(attempts);
}

void ServiceAckOutbox(int replayLimit) {
   if(!TryPinAckOutboxScopeIdentity()) return;
   string identityReason="";
   bool identityMatches=AckOutboxScopeIdentityMatches(identityReason);
   RecoverAckOutboxTemps(ACK_OUTBOX_RECOVER_PER_PASS);
   FlushUnpersistedAckPayloads(ACK_OUTBOX_RECOVER_PER_PASS);
   ReplayAckOutbox(replayLimit);

   int pending=AckOutboxPendingCount();
   int staged=AckOutboxStagedCount();
   int stored=pending+staged;
   if(!gSignalOutcomeJournalReady || gSignalOutcomeJournalBlocked) {
      BlockAckOutbox("signal_outcome_journal_blocked");
   } else if(!identityMatches) {
      BlockAckOutbox(identityReason);
   } else if(stored>=ACK_OUTBOX_MAX_PENDING) {
      BlockAckOutbox("capacity_exhausted max="+IntegerToString(ACK_OUTBOX_MAX_PENDING));
   } else if(gAckUnpersistedCount>0) {
      BlockAckOutbox("unpersisted_ack_waiting count="+IntegerToString(gAckUnpersistedCount));
   } else if(staged>0) {
      BlockAckOutbox("staged_ack_waiting count="+IntegerToString(staged));
   } else if(pending>0) {
      BlockAckOutbox("pending_ack_replay count="+IntegerToString(pending));
   } else {
      gAckOutboxBlocked=false;
   }
}

bool AckOutboxAllowsCommandPolling() {
   if(!TryPinAckOutboxScopeIdentity()) return(false);
   if(!gSignalOutcomeJournalReady || gSignalOutcomeJournalBlocked) {
      BlockAckOutbox("signal_outcome_journal_blocked");
      return(false);
   }
   string identityReason="";
   if(!AckOutboxScopeIdentityMatches(identityReason)) {
      BlockAckOutbox(identityReason);
      return(false);
   }
   int pending=AckOutboxPendingCount();
   int staged=AckOutboxStagedCount();
   int stored=pending+staged;
   if(
      gAckUnpersistedCount>0 || pending>0 || staged>0 ||
      stored>=ACK_OUTBOX_MAX_PENDING-1
   ) {
      BlockAckOutbox("command_poll_fenced_until_ack_replay");
      return(false);
   }
   gAckOutboxBlocked=false;
   return(true);
}

void FlushAckOutboxOnDeinit() {
   if(!TryPinAckOutboxScopeIdentity()) {
      BlockAckOutbox("deinit_scope_identity_unavailable");
      return;
   }
   FlushUnpersistedAckPayloads(ACK_OUTBOX_UNPERSISTED_MAX);
   RecoverAckOutboxTemps(ACK_OUTBOX_RECOVER_PER_PASS);
   int pending=AckOutboxPendingCount();
   int staged=AckOutboxStagedCount();
   Print(
      "[ACK_OUTBOX] deinit persisted pending=",pending,
      " staged=",staged,
      " unpersisted=",gAckUnpersistedCount,
      " scope=",AckOutboxScopeDirectory()
   );
   if(gAckUnpersistedCount>0 || staged>0) {
      BlockAckOutbox("deinit_persistence_incomplete");
   }
}

bool QueueAckPayloadBeforePost(string payload,string &finalPath) {
   bool durable=false;
   if(PersistAckPayloadBeforePost(payload,finalPath,durable)) return(true);
   if(!durable) RetainUnpersistedAckPayload(payload);
   return(false);
}

void post_ack(
   string signal_id,
   string status,
   string symbol = "",
   int ticket = -1,
   int error_code = 0,
   string message = "",
   string trace_id = "",
   double t_py_signal_post_start = 0.0,
   double t_bridge_queued = 0.0,
   double t_bridge_delivered = 0.0,
   double t_ea_received = 0.0,
   double t_ea_exec_start = 0.0,
   double t_ea_exec_end = 0.0,
   double ea_handle_to_ack_ms = 0.0,
   string interop_mode = "",
   int magic = -1,
   string owner_token = "",
   string mutation_state = "",
   string actual_cmd = "",
   string actual_symbol = "",
   string actual_broker_symbol = "",
   string actual_side = "",
   string actual_execution_type = "",
   int actual_target_ticket = -1,
   int actual_magic = -1,
   string actual_owner_token = "",
   string actual_order_comment = "",
   double actual_lots = 0.0,
   double actual_sl_price = 0.0,
   double actual_tp_price = 0.0,
   double actual_open_price = 0.0,
   string actuals_schema = "",
   double actual_remaining_lots = 0.0,
   datetime actual_close_time = 0
) {
   if(StringLen(signal_id) <= 0) return;
   string effectiveStatus=StringTrim(status);
   string effectiveMutationState=StringTrim(mutation_state);
   // A positive broker ticket can never be represented as an ordinary failed
   // or duplicate outcome.  The mutation must be reconciled against broker
   // truth, even when the caller supplied an unsafe status by mistake.
   if(
      ticket>0 &&
      (effectiveStatus=="failed" || effectiveStatus=="duplicate")
   ) {
      effectiveStatus="reconcile_required";
      effectiveMutationState="attempted";
      if(StringLen(message)<=0) message="positive_ticket_requires_reconciliation";
   }
   if(StringLen(effectiveMutationState)<=0) {
      if(effectiveStatus=="acked") effectiveMutationState="confirmed";
      else if(effectiveStatus=="failed" || effectiveStatus=="duplicate") {
         effectiveMutationState="not_attempted";
      } else if(effectiveStatus=="reconcile_required") {
         effectiveMutationState="attempted";
      } else {
         effectiveMutationState="unknown";
      }
   }
   bool mutationAttempted=(
      effectiveMutationState=="attempted" ||
      effectiveMutationState=="confirmed" ||
      effectiveMutationState=="unknown"
   );
   bool mutationConfirmed=(effectiveMutationState=="confirmed");
   bool brokerOutcomeKnown=(
      effectiveMutationState=="confirmed" ||
      effectiveMutationState=="not_attempted"
   );
   double t_ea_ack_post = (double)TimeCurrent();
   string payload = "{\"signal_id\":\"" + JsonEscape(signal_id) +
                    "\",\"command_id\":\"" + JsonEscape(signal_id) +
                    "\",\"status\":\"" + JsonEscape(effectiveStatus) +
                    "\",\"symbol\":\"" + JsonEscape(symbol) +
                    "\",\"ticket\":" + IntegerToString(ticket) +
                    ",\"magic\":" + IntegerToString(magic) +
                    ",\"owner_token\":\"" + JsonEscape(owner_token) + "\"" +
                    ",\"mutation_state\":\"" + JsonEscape(effectiveMutationState) + "\"" +
                    ",\"broker_mutation_attempted\":" + JsonBool(mutationAttempted) +
                    ",\"broker_mutation_confirmed\":" + JsonBool(mutationConfirmed) +
                    ",\"broker_outcome_known\":" + JsonBool(brokerOutcomeKnown) +
                    ",\"execution_uncertain\":" + JsonBool(effectiveStatus=="reconcile_required") +
                    ",\"actuals_schema\":\"" + JsonEscape(actuals_schema) + "\"" +
                    ",\"actual_command_id\":\"" + JsonEscape(signal_id) + "\"" +
                    ",\"actual_cmd\":\"" + JsonEscape(actual_cmd) + "\"" +
                    ",\"actual_symbol\":\"" + JsonEscape(actual_symbol) + "\"" +
                    ",\"actual_broker_symbol\":\"" + JsonEscape(actual_broker_symbol) + "\"" +
                    ",\"actual_side\":\"" + JsonEscape(actual_side) + "\"" +
                    ",\"actual_execution_type\":\"" + JsonEscape(actual_execution_type) + "\"" +
                    ",\"actual_ticket\":" + IntegerToString(ticket) +
                    ",\"actual_target_ticket\":" + IntegerToString(actual_target_ticket) +
                    ",\"actual_magic\":" + IntegerToString(actual_magic) +
                    ",\"actual_owner_token\":\"" + JsonEscape(actual_owner_token) + "\"" +
                    ",\"actual_order_comment\":\"" + JsonEscape(actual_order_comment) + "\"" +
                    ",\"actual_lots\":" + DoubleToString(actual_lots, 8) +
                    ",\"actual_remaining_lots\":" + DoubleToString(actual_remaining_lots, 8) +
                    ",\"actual_sl_price\":" + DoubleToString(actual_sl_price, 12) +
                    ",\"actual_tp_price\":" + DoubleToString(actual_tp_price, 12) +
                    ",\"actual_open_price\":" + DoubleToString(actual_open_price, 12) +
                    ",\"actual_close_time\":" + DoubleToString((double)actual_close_time, 0) +
                    ",\"error_code\":" + IntegerToString(error_code) +
                    ",\"message\":\"" + JsonEscape(message) +
                    "\",\"status_reason\":\"" + JsonEscape(message) +
                    "\",\"trace_id\":\"" + JsonEscape(trace_id) +
                    "\",\"consumer_identity\":\"" + JsonEscape(gBridgeConsumerIdentity) +
                    "\",\"producer_instance_id\":\"" + JsonEscape(gBridgeProducerInstanceId) +
                    "\",\"terminal_lease_scope\":\"" + JsonEscape(gBridgeTerminalLeaseScope) +
                    "\",\"credential_generation_id\":\"" + JsonEscape(gBridgeCredentialGenerationId) +
                    "\",\"bridge_protocol_version\":\"" + EA_EXPECTED_PROTOCOL_VERSION +
                    "\",\"interop_mode\":\"" + JsonEscape(interop_mode) +
                    "\",\"t_py_signal_post_start\":" + DoubleToString(t_py_signal_post_start, 6) +
                    ",\"t_bridge_queued\":" + DoubleToString(t_bridge_queued, 6) +
                    ",\"t_bridge_delivered\":" + DoubleToString(t_bridge_delivered, 6) +
                    ",\"t_ea_received\":" + DoubleToString(t_ea_received, 6) +
                    ",\"t_ea_exec_start\":" + DoubleToString(t_ea_exec_start, 6) +
                    ",\"t_ea_exec_end\":" + DoubleToString(t_ea_exec_end, 6) +
                    ",\"t_ea_ack_post\":" + DoubleToString(t_ea_ack_post, 6) +
                    ",\"ea_handle_to_ack_ms\":" + DoubleToString(ea_handle_to_ack_ms, 3) +
                    ",\"executed_at\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) +
                    "\"}";
   if(!PersistSignalOutcomePayload(signal_id,payload)) {
      gSignalOutcomeJournalBlocked=true;
      BlockAckOutbox("signal_outcome_persist_failed command_id="+signal_id);
   }
   string queuedPath="";
   if(!QueueAckPayloadBeforePost(payload,queuedPath)) return;
   ReplayAckOutboxFile(queuedPath);
}

void post_strict_management_ack(
   string signal_id,
   string status,
   string expected_symbol,
   int target_ticket,
   int error_code,
   string message,
   bool mutation_attempted,
   bool mutation_confirmed,
   StrictManagementActuals &actual,
   string trace_id,
   double t_py_signal_post_start,
   double t_bridge_queued,
   double t_bridge_delivered,
   double t_ea_received,
   double ea_handle_to_ack_ms,
   string interop_mode,
   int expected_magic,
   string expected_owner_token
) {
   string mutationState=mutation_confirmed
      ? "confirmed"
      : (mutation_attempted ? "attempted" : "not_attempted");
   string effectiveStatus=status;
   if(mutation_attempted && !mutation_confirmed) {
      effectiveStatus="reconcile_required";
   }
   int resultTicket=(mutation_attempted || mutation_confirmed)
      ? target_ticket
      : -1;
   post_ack(
      signal_id,effectiveStatus,expected_symbol,resultTicket,error_code,message,
      trace_id,t_py_signal_post_start,t_bridge_queued,t_bridge_delivered,
      t_ea_received,0.0,(double)TimeCurrent(),ea_handle_to_ack_ms,
      interop_mode,expected_magic,expected_owner_token,mutationState,
      actual.cmd,actual.logical_symbol,actual.broker_symbol,actual.side,
      actual.execution_type,target_ticket,actual.magic,actual.owner_token,
      actual.order_comment,actual.lots,actual.sl_price,actual.tp_price,
      actual.open_price,
      mutation_confirmed ? BROKER_ORDER_ACTUALS_SCHEMA : "",
      actual.remaining_lots,actual.close_time
   );
}

// Return 1 when an exact durable prior outcome was queued for idempotent
// replay, 0 when this command has no prior outcome, and -1 on journal
// ambiguity.  A caller must never execute the command when -1 is returned.
int ReplayDurableSignalOutcome(string signalId) {
   string payload="";
   bool exists=false;
   if(!ReadSignalOutcomePayload(signalId,payload,exists)) return(-1);
   if(!exists) return(0);
   string queuedPath="";
   if(!QueueAckPayloadBeforePost(payload,queuedPath)) return(-1);
   ReplayAckOutboxFile(queuedPath);
   return(1);
}

void CleanupSeenSignals() {
   datetime now = TimeCurrent();
   int writeIdx = 0;
   for(int i = 0; i < gSeenCount; i++) {
      if((now - gSeenSignalTs[i]) <= SignalDedupTTLSeconds) {
         if(writeIdx != i) {
            gSeenSignalIds[writeIdx] = gSeenSignalIds[i];
            gSeenSignalTs[writeIdx] = gSeenSignalTs[i];
         }
         writeIdx++;
      }
   }
   gSeenCount = writeIdx;
   ArrayResize(gSeenSignalIds, gSeenCount);
   ArrayResize(gSeenSignalTs, gSeenCount);
}

bool SeenSignalRecently(string signal_id) {
   if(StringLen(signal_id) <= 0) return false;
   CleanupSeenSignals();
   for(int i = 0; i < gSeenCount; i++) {
      if(gSeenSignalIds[i] == signal_id) return true;
   }
   return false;
}

void RememberSignalId(string signal_id) {
   if(StringLen(signal_id) <= 0) return;
   CleanupSeenSignals();
   if(gSeenCount >= SignalDedupMax && gSeenCount > 0) {
      for(int i = 1; i < gSeenCount; i++) {
         gSeenSignalIds[i - 1] = gSeenSignalIds[i];
         gSeenSignalTs[i - 1] = gSeenSignalTs[i];
      }
      gSeenCount--;
   }
   ArrayResize(gSeenSignalIds, gSeenCount + 1);
   ArrayResize(gSeenSignalTs, gSeenCount + 1);
   gSeenSignalIds[gSeenCount] = signal_id;
   gSeenSignalTs[gSeenCount] = TimeCurrent();
   gSeenCount++;
}

// Parse a JSON-encoded numeric field by key (no JSON lib in MQL4). Returns
// -1.0 if the key is absent or the value is outside (0, 1]. Tolerant of
// whitespace; expects the format ``"key":<number>`` with optional spaces.
double ParseHandshakeFraction(string json, string key) {
   string needle = "\"" + key + "\":";
   int idx = StringFind(json, needle);
   if(idx < 0) return -1.0;
   int start = idx + StringLen(needle);
   int len = StringLen(json);
   while(start < len) {
      string c = StringSubstr(json, start, 1);
      if(c != " " && c != "\t") break;
      start++;
   }
   int end = start;
   while(end < len) {
      string c = StringSubstr(json, end, 1);
      if(c == "," || c == "}" || c == " " || c == "\r" || c == "\n" || c == "\t") break;
      end++;
   }
   if(end <= start) return -1.0;
   double val = StrToDouble(StringSubstr(json, start, end - start));
   if(val <= 0.0 || val > 1.0) return -1.0;
   return val;
}

string ParseJsonStringField(string json, string key, string fallback) {
   string needle = "\"" + key + "\":\"";
   int p = StringFind(json, needle);
   if(p < 0) return fallback;
   int start = p + StringLen(needle);
   int end = StringFind(json, "\"", start);
   if(end < start) return fallback;
   return StringSubstr(json, start, end - start);
}

bool ParseJsonBoolField(string json, string key, bool fallback) {
   string needle = "\"" + key + "\":";
   int p = StringFind(json, needle);
   if(p < 0) return fallback;
   int start = p + StringLen(needle);
   string tail = StringSubstr(json, start, 5);
   if(StringFind(tail, "true") == 0) return true;
   tail = StringSubstr(json, start, 6);
   if(StringFind(tail, "false") == 0) return false;
   return fallback;
}

void RefreshDashboardFromBridgeReady() {
   string resp = HttpGET(ApiBase + "/v2/ready", gBridgeApiKey);
   int statusCode = LastBridgeHttpStatus();
   WarnAuthFailure("ready", statusCode);
   if(statusCode != 200 || StringLen(resp) <= 0) {
      UpdateDashboard("BRIDGE DISCONNECTED|ready_http=" + IntegerToString(statusCode) + "|api=" + ApiBase);
      return;
   }

   string status = ParseJsonStringField(resp, "status", "unknown");
   string reason = ParseJsonStringField(resp, "reason", "");
   string runtime = ParseJsonStringField(resp, "runtime_status", "unknown");
   string phase = ParseJsonStringField(resp, "runtime_phase", "");
   bool runtimeReady = ParseJsonBoolField(resp, "runtime_ready", false);
   bool mt4Fresh = ParseJsonBoolField(resp, "mt4_fresh", false);
   bool ticksFresh = ParseJsonBoolField(resp, "ticks_fresh", false);
   string tickStatus = ParseJsonStringField(resp, "tick_status", "unknown");
   bool featureFresh = ParseJsonBoolField(resp, "feature_data_fresh", false);
   string featureReason = ParseJsonStringField(resp, "feature_blocker_reason", "");
   if(!runtimeReady) MaybeSyncBarHistory(false);

   string line1 = "Scanning live stack";
   if(status != "ok") line1 = "BRIDGE DEGRADED";
   string line2 = "Bridge " + status;
   if(StringLen(reason) > 0 && reason != "ok") line2 = line2 + " " + reason;
   string line3 = "Runtime " + runtime + " ready=" + (runtimeReady ? "yes" : "no");
   if(StringLen(phase) > 0) line3 = line3 + " phase=" + phase;
   string line4 = "MT4 " + (mt4Fresh ? "fresh" : "stale") + " ticks=" + (ticksFresh ? "fresh" : tickStatus);
   string line5 = "Features " + (featureFresh ? "fresh" : "stale");
   if(StringLen(featureReason) > 0) line5 = line5 + " " + featureReason;
   UpdateDashboard(line1 + "|" + line2 + "|" + line3 + "|" + line4 + "|" + line5);
}

// Fail-closed capability check for the bridge protocol this EA was built for.
// Durable ACK replay remains available, but command polling stays fenced until
// a later retry proves exact compatibility.
bool VerifyBridgeHandshake() {
   gBridgeHandshakeCompatible = false;
   gLastBridgeHandshakeAttempt = TimeCurrent();
   string url = ApiBase + "/v2/handshake";
   string resp = HttpGET(url, gBridgeApiKey);
   if(StringLen(resp) == 0) {
      ResetBarHistoryBootstrap("handshake_unavailable");
      Print("[BRIDGE] handshake: no response from ", url, " (bridge offline?)");
      post_report("BRIDGE_HANDSHAKE_FAIL reason=no_response url=" + url);
      return(false);
   }

   // Pull the basket-TP override before the fail-closed version check. A
   // mismatch still leaves command polling and broker-truth production fenced.
   double pct = ParseHandshakeFraction(resp, "basket_tp_pct");
   if(pct > 0.0) {
      gBasketTpPctFromBridge = pct;
      Print("[BRIDGE] handshake: basket_tp_pct=", DoubleToString(pct, 6));
   } else {
      Print("[BRIDGE] handshake: basket_tp_pct missing or invalid; falling back to ",
            DoubleToString(EA_FALLBACK_BASKET_TP_PCT, 6));
   }

   string expected = "\"protocol_version\":\"" + EA_EXPECTED_PROTOCOL_VERSION + "\"";
   if(StringFind(resp, expected) >= 0) {
      gBridgeHandshakeCompatible = true;
      Print("[BRIDGE] handshake OK: protocol=", EA_EXPECTED_PROTOCOL_VERSION);
      return(true);
   }
   Print("[BRIDGE] handshake MISMATCH: expected ", EA_EXPECTED_PROTOCOL_VERSION, " in ", resp);
   post_report("BRIDGE_HANDSHAKE_MISMATCH expected=" + EA_EXPECTED_PROTOCOL_VERSION + " resp=" + resp);
   UpdateDashboard("PROTOCOL MISMATCH|Command polling fenced|Recompile and redeploy BridgeEA");
   return(false);
}

int OnInit(){
   if(!EventSetMillisecondTimer(BRIDGE_EDGE_TIMER_INTERVAL_MS)) {
      Print("[BRIDGE] high-resolution edge timer unavailable err=", GetLastError());
      return(INIT_FAILED);
   }
   Print("MT4 Bridge EA (WinInet) initialized");
   Print("ApiBase: ", ApiBase);
   gBridgeApiKey = LoadBridgeApiKey();
   gBridgeCommandToken = LoadBridgeSecret(CommandToken, "bridge_command_token.txt", "command auth");
   gBridgeConsumerIdentity = LoadBridgeSecret(ConsumerIdentity, "bridge_consumer_identity.txt", "consumer identity");
   gBridgeTerminalLeaseScope = LoadBridgeSecret(TerminalLeaseScope, "bridge_terminal_lease_scope.txt", "terminal lease");
   gBridgeCredentialGenerationId = LoadBridgeSecret(CredentialGenerationId, "bridge_credential_generation_id.txt", "credential generation");
   gBridgeProducerInstanceId = LoadOrCreateProducerInstanceId();
   if(!ProducerInstanceIdValid(gBridgeProducerInstanceId)) {
      Print(
         "[BRIDGE] durable producer instance identity unavailable; "
         "EA initialization fails closed"
      );
      EventKillTimer();
      return(INIT_FAILED);
   }
   gBridgeHandshakeCompatible=false;
   gLastBridgeHandshakeAttempt=0;
   gLastBarHistoryAttempt=0;
   gLastBarHistoryMinuteBucket=-1;
   gBarHistoryInitialBootstrapAt=0;
   ArrayResize(gBarHistoryBootstrappedSymbols,0);
   ArrayResize(gBarHistoryEmissionSymbols,0);
   ArrayResize(gBarHistoryLastEmittedClosedTimes,0);
   ClearBarHistoryMinuteEdge();
   ClearBarHistoryStability();
   gBarHistoryRecoveryTransportAbort=false;
   gBarHistoryRecoveryUploadCursor=0;
   gBarHistoryDelayedRecoverySweepsRemaining=
      SCALP_BAR_HISTORY_DELAYED_RECOVERY_SWEEPS;
   ArrayResize(gBarHistoryRecoveryChartSymbols,0);
   ArrayResize(gBarHistoryRecoveryChartIds,0);
   ClearBarHistorySymbolCache();
   gBarHistorySymbolCacheRefreshRequested=false;
   gLastBarHistorySymbolCacheAttempt=0;
   gLastBarHistorySymbolCacheWarn=0;
    ClearMarketDataSymbolCache();
   gMarketDataSymbolCacheLastAttemptIdentity="";
   gLastMarketDataSymbolCacheAttempt=0;
   gLastMarketDataSymbolCacheWarn=0;
   gHeartbeatAttempted=false;
   gLastHeartbeatAttemptMs=0;
   gMainTimerBodyAttempted=false;
   gLastMainTimerBodyMs=0;
   gAuxMaintenanceCursor=0;
   gLastBridgeStatusReport=0;
   gLastBrokerSpecsReport=0;
   gLastDashboardRefresh=0;
   gLastPositionsSnapshot=0;
   gLastClosedTradeReport=0;
   ArrayResize(gSeenSignalIds, 0);
   ArrayResize(gSeenSignalTs, 0);
   gSeenCount = 0;
   ArrayResize(gAckUnpersistedPayloads,0);
   gAckUnpersistedCount=0;
   gAckReplayCursor=0;
   gAckOutboxBlocked=false;
   gSignalOutcomeJournalBlocked=false;
   gSignalOutcomeJournalReady=false;
   gLastAckOutboxWarnTs=0;
   gAckScopePinned=false;
   gAckScopeAccountNumber=0;
   gAckScopeAccountServer="";
   gAckScopeApiBase="";
   gAckScopeMagic=0;
   gAckScopeTerminalDataPath="";
   gAckScopeTerminalToken="";
   gAckScopeProducerInstanceId="";
   gAckScopeDirectory="";

   // A command must never reach a broker mutation unless terminal-local
   // write/flush/promote/read/delete durability has been proved this boot.
   if(!EnsureSignalOutcomeJournalReady()) {
      gSignalOutcomeJournalBlocked=true;
      Print(
         "[BRIDGE] terminal-local signal outcome journal unavailable; "
         "EA initialization fails closed err=",GetLastError()
      );
      EventKillTimer();
      return(INIT_FAILED);
   }

   // Initialize Shared WinInet Session
   if(!InitBridgeHttp("MT4_Bridge_EA")) {
       return(INIT_FAILED);
   }
   // Resolve the complete market-data universe once. An unavailable or
   // ambiguous mapping fences tick publication until a bounded retry succeeds;
    // command polling and protective execution retain their existing behavior.
    RefreshMarketDataSymbolCache(true);
    // Broker-catalogue scans are also prepared once for the completed-bar lane.
    // The cache helper refuses to discover symbols inside :58..+4; if startup
    // lands there, the first post-edge timer prepares it while publication
    // remains fail closed.
   EnsureBarHistorySymbolCache();

   // Load and replay durable broker outcomes before this EA is allowed to poll
   // another command. Replaying an ACK never re-enters HandleCmd.
   UpdateDashboard("WAITING FOR AGENT...|Replaying durable ACK outbox...");
   ServiceAckOutbox(ACK_OUTBOX_REPLAY_ON_STARTUP);

   // Verify exact protocol compatibility before authenticated history or polling.
   VerifyBridgeHandshake();
   MaybeSendHeartbeat(true);
   // A restarted terminal can report asynchronously mutating off-chart bars.
   // Prime downloads now, but do not overwrite API history until two complete
   // synchronized observations prove the exact full window is stable.
   if(gBridgeHandshakeCompatible) PrimeBarHistoryColdLoad();

   // Show initial status
   UpdateDashboard("WAITING FOR AGENT...|Starting Python Bridge...");
   reportBridgeStatus();
   PrimeClosedTradeCursor();
   ReplayRecentClosedTrades(ClosedTradeReplayCount);
   AckOutboxAllowsCommandPolling();

   return(INIT_SUCCEEDED);
}

// Cleanup Dashboard
void RemoveDashboard() {
   // Delete Background Panel
   ObjectDelete(0, "BridgeHUD_BG");
   
   // Delete Header Bar
   ObjectDelete(0, "BridgeHUD_HdrBG");

   // Delete Header Text & Status
   ObjectDelete(0, "BridgeHUD_Title");
   ObjectDelete(0, "BridgeHUD_Dot");

   // Delete Content Lines
   int i = 0;
   while(true) {
      string objName = "BridgeHUD_Txt_" + IntegerToString(i);
      if(ObjectFind(0, objName) >= 0) ObjectDelete(0, objName);
      else if(i > 0) break; // Break if not found (assuming ordered)
      i++;
      if(i > 100) break; // Safety
   }

   gDashboardLastText = "";
   gDashboardLastChartWidth = -1;
   gDashboardLastChartHeight = -1;
   gDashboardRowsShown = -1;
   ChartRedraw(0);
}

void OnDeinit(const int reason){ 
   EventKillTimer();
   CloseAllBarHistoryRecoveryCharts();
   // Every ACK writer flushes and closes before returning. This final pass
   // promotes any staged files and retries persistence of bounded memory fallbacks.
   FlushAckOutboxOnDeinit();
   // Give pending operations a moment to settle
   Sleep(250);
   
   RemoveDashboard();
   
   DeinitBridgeHttp();
}

void post_report(string msg) {
   // Print("Sending Report: ", msg); // DEBUG REMOVED
   HttpPOST(ApiBase + ReportPath(), msg, gBridgeApiKey);
   WarnAuthFailure("report", LastBridgeHttpStatus());
}

string CurrentBrokerAccountMode() {
   long accountTradeMode = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(accountTradeMode == ACCOUNT_TRADE_MODE_DEMO) return("demo");
   if(accountTradeMode == ACCOUNT_TRADE_MODE_CONTEST) return("contest");
   if(accountTradeMode == ACCOUNT_TRADE_MODE_REAL) return("real");
   return("unknown");
}

string CurrentBrokerAccountScope(int magic) {
   string material = IntegerToString(AccountNumber()) + "|" +
                     StringTrim(AccountServer()) + "|" + IntegerToString(magic);
   return(IntegerToString((int)AckOutboxHash(material)));
}

string CurrentBrokerMarketSourceJsonFields() {
   if(!TryPinAckOutboxScopeIdentity()) return("");
   string identityReason="";
   if(!AckOutboxScopeIdentityMatches(identityReason)) {
      BlockAckOutbox(identityReason);
      return("");
   }
   string accountScope=CurrentBrokerAccountScope(Magic);
   if(
      StringLen(accountScope)<=0 || StringLen(gBridgeConsumerIdentity)<=0 ||
      !ProducerInstanceIdValid(gBridgeProducerInstanceId) ||
      StringLen(gBridgeTerminalLeaseScope)<=0 ||
      StringLen(gBridgeCredentialGenerationId)<=0
   ) return("");
   return(
      ",\"broker_account_scope\":\"" + JsonEscape(accountScope) + "\"" +
      ",\"broker_account_scope_schema\":\"" + BROKER_ACCOUNT_SCOPE_SCHEMA + "\"" +
      ",\"broker_account_scope_version\":" + IntegerToString(BROKER_ACCOUNT_SCOPE_VERSION) +
      ",\"broker_server\":\"" + JsonEscape(StringTrim(AccountServer())) + "\"" +
      ",\"broker_company\":\"" + JsonEscape(StringTrim(AccountCompany())) + "\"" +
      ",\"consumer_identity\":\"" + JsonEscape(gBridgeConsumerIdentity) + "\"" +
      ",\"producer_instance_id\":\"" + JsonEscape(gBridgeProducerInstanceId) + "\"" +
      ",\"terminal_lease_scope\":\"" + JsonEscape(gBridgeTerminalLeaseScope) + "\"" +
      ",\"credential_generation_id\":\"" + JsonEscape(gBridgeCredentialGenerationId) + "\"" +
      ",\"bridge_protocol_version\":\"" + EA_EXPECTED_PROTOCOL_VERSION + "\""
   );
}

void heartbeat(){
   string marketSourceFields=CurrentBrokerMarketSourceJsonFields();
   if(StringLen(marketSourceFields)<=0) return;
   string transport = gUseWebRequest ? "webrequest" : "wininet";
   string accountMode = CurrentBrokerAccountMode();
   string accountScope = CurrentBrokerAccountScope(Magic);
   // Account number remains only inside the existing non-reversible scope.
   // Server and company are non-secret broker evidence used by Python's
   // explicit fail-closed venue matcher; the EA never self-asserts a venue.
   string out = "{\"report_type\":\"heartbeat\"" +
                ",\"equity\":" + DoubleToString(AccountEquity(), 2) +
                ",\"margin\":" + DoubleToString(AccountMargin(), 2) +
                ",\"freemargin\":" + DoubleToString(AccountFreeMargin(), 2) +
                ",\"leverage\":" + IntegerToString(AccountLeverage()) +
                ",\"transport_mode\":\"" + JsonEscape(transport) + "\"" +
                ",\"broker_account_mode\":\"" + JsonEscape(accountMode) + "\"" +
                ",\"broker_account_scope\":\"" + JsonEscape(accountScope) + "\"" +
                ",\"broker_account_scope_schema\":\"" + BROKER_ACCOUNT_SCOPE_SCHEMA + "\"" +
                ",\"broker_account_scope_version\":" + IntegerToString(BROKER_ACCOUNT_SCOPE_VERSION) +
                ",\"broker_account_magic\":" + IntegerToString(Magic) +
                ",\"broker_account_currency\":\"" + JsonEscape(StringTrim(AccountCurrency())) + "\"" +
                ",\"broker_server\":\"" + JsonEscape(StringTrim(AccountServer())) + "\"" +
                ",\"broker_company\":\"" + JsonEscape(StringTrim(AccountCompany())) + "\"" +
                ",\"consumer_identity\":\"" + JsonEscape(gBridgeConsumerIdentity) + "\"" +
                ",\"producer_instance_id\":\"" + JsonEscape(gBridgeProducerInstanceId) + "\"" +
                ",\"terminal_lease_scope\":\"" + JsonEscape(gBridgeTerminalLeaseScope) + "\"" +
                ",\"credential_generation_id\":\"" + JsonEscape(gBridgeCredentialGenerationId) + "\"" +
                ",\"bridge_protocol_version\":\"" + EA_EXPECTED_PROTOCOL_VERSION + "\"" +
                ",\"ack_outbox_blocked\":" + JsonBool(gAckOutboxBlocked) +
                ",\"ack_outbox_pending\":" + IntegerToString(AckOutboxPendingCount()) +
                "}";
   post_report(out);
}

void MaybeSendHeartbeat(bool force) {
   uint nowMs = GetTickCount();
   uint intervalMs = (uint)(BRIDGE_HEARTBEAT_INTERVAL_SECS * 1000);
   if(
      !force && gHeartbeatAttempted &&
      (uint)(nowMs - gLastHeartbeatAttemptMs) < intervalMs
   ) return;
   gHeartbeatAttempted = true;
   gLastHeartbeatAttemptMs = nowMs;
   heartbeat();
}

bool BridgeHeartbeatDue() {
   if(!gHeartbeatAttempted) return true;
   return(
      (uint)(GetTickCount() - gLastHeartbeatAttemptMs) >=
      (uint)(BRIDGE_HEARTBEAT_INTERVAL_SECS * 1000)
   );
}

bool ServiceOneAuxiliaryMaintenance() {
   datetime now = TimeGMT();
   if(now <= 0) now = TimeLocal();
   if(now <= 0) return false;
   int closedTradeInterval = ClosedTradeReportIntervalSecs;
   if(closedTradeInterval < 1) closedTradeInterval = 1;

   // Each timer second already carries the latency-critical all-symbol quote
   // frame and command poll. Rotate at most one slower report/read so the work
   // deferred by the minute-edge guard cannot all land on the first +5 callback.
   int taskCount = 6;
   for(int offset = 0; offset < taskCount; offset++) {
      int task = (gAuxMaintenanceCursor + offset) % taskCount;
      bool due = false;
      if(task == 0) due = BridgeHeartbeatDue();
      else if(task == 1)
         due = now > gLastDashboardRefresh + 4;
      else if(task == 2)
         due = now > gLastPositionsSnapshot + 4;
      else if(task == 3)
         due = now >=
            gLastBrokerSpecsReport + BROKER_SPEC_REPORT_INTERVAL_SECS;
      else if(task == 4)
         due = now > gLastBridgeStatusReport + 14;
      else if(task == 5)
         due = now >
            gLastClosedTradeReport + closedTradeInterval;
      if(!due) continue;

      gAuxMaintenanceCursor = (task + 1) % taskCount;
      if(task == 0) {
         MaybeSendHeartbeat(false);
      } else if(task == 1) {
         RefreshDashboardFromBridgeReady();
         gLastDashboardRefresh = now;
      } else if(task == 2) {
         EmitPositionsSnapshot();
         gLastPositionsSnapshot = now;
      } else if(task == 3) {
         reportSymbolSpecs();
         gLastBrokerSpecsReport = now;
      } else if(task == 4) {
         reportBridgeStatus();
         gLastBridgeStatusReport = now;
      } else {
         SendClosedTradeUpdates();
         gLastClosedTradeReport = now;
      }
      return true;
   }
   return false;
}

void reportBridgeStatus() {
   if(!gBridgeHandshakeCompatible) return;
   // AGENT HOT PATH: the broker catalogue was already resolved atomically into
   // this identity-bound cache. Re-running two complete catalogue scans for
   // every configured pair on each 15-second status report stalls MT4's single
   // EA/UI thread and cannot add fresher broker truth.
   if(!EnsureMarketDataSymbolCache()) return;
   string marketSourceFields=CurrentBrokerMarketSourceJsonFields();
   if(StringLen(marketSourceFields)<=0) return;
   string transport = gUseWebRequest ? "webrequest" : "wininet";
   int n = gMarketDataStrategySymbolCount;
   if(n <= 0 || n > ArraySize(gMarketDataLogicalSymbols)) return;
   string pairsJson = "[";
   string readinessJson = "{";
   int readyCount = 0;

   for(int i = 0; i < n; i++) {
      string logicalSym = gMarketDataLogicalSymbols[i];
      string brokerSym = gMarketDataBrokerSymbols[i];
      string mappingReason = gMarketDataMappingReasons[i];
      string mappingKind = gMarketDataMappingKinds[i];
      int mappingCandidateCount = gMarketDataMappingCandidateCounts[i];
      bool mappingAmbiguous = gMarketDataMappingAmbiguous[i];
      bool supported =
         StringLen(logicalSym) > 0 && StringLen(brokerSym) > 0 &&
         !mappingAmbiguous && mappingCandidateCount > 0;
      bool selected = supported;
      if(i > 0) {
         pairsJson = pairsJson + ",";
         readinessJson = readinessJson + ",";
      }
      pairsJson = pairsJson + "\"" + JsonEscape(logicalSym) + "\"";
      readinessJson = readinessJson +
         "\"" + JsonEscape(logicalSym) + "\":{" +
         "\"broker_symbol\":\"" + JsonEscape(brokerSym) + "\"," +
         "\"supported\":" + JsonBool(supported) + "," +
         "\"selected\":" + JsonBool(selected) + "," +
         "\"mapping_ambiguous\":" + JsonBool(mappingAmbiguous) + "," +
         "\"mapping_reason\":\"" + JsonEscape(mappingReason) + "\"," +
         "\"mapping_kind\":\"" + JsonEscape(mappingKind) + "\"," +
         "\"mapping_candidate_count\":" + IntegerToString(mappingCandidateCount) +
         "}";
      if(supported) readyCount++;
   }
   pairsJson = pairsJson + "]";
   readinessJson = readinessJson + "}";

   string payload =
      "{\"report_type\":\"bridge_status\"" +
      marketSourceFields +
      ",\"equity\":" + DoubleToString(AccountEquity(), 2) +
      ",\"margin\":" + DoubleToString(AccountMargin(), 2) +
      ",\"freemargin\":" + DoubleToString(AccountFreeMargin(), 2) +
      ",\"transport_mode\":\"" + JsonEscape(transport) + "\"" +
      ",\"ack_outbox_blocked\":" + JsonBool(gAckOutboxBlocked) +
      ",\"ack_outbox_pending\":" + IntegerToString(AckOutboxPendingCount()) +
      ",\"ack_outbox_staged\":" + IntegerToString(AckOutboxStagedCount()) +
      ",\"configured_pairs\":" + pairsJson +
      ",\"symbol_ready_count\":" + IntegerToString(readyCount) +
      ",\"symbol_readiness\":" + readinessJson +
      "}";
   post_report(payload);
}

void reportSymbolSpecs() {
   // Broker contract truth per symbol. Sizing downstream must use these --
   // FX-contract assumptions over-size IG crypto CFDs by orders of magnitude,
   // and stop floors must come from MODE_STOPLEVEL, not config guesses.
   if(!gBridgeHandshakeCompatible) return;
   // Mapping discovery is a separately validated cache refresh. Dynamic
   // MarketInfo/SymbolInfo values remain sampled every 15 seconds, while the
   // expensive full broker-catalogue search is not repeated here.
   if(!EnsureMarketDataSymbolCache()) return;
   string marketSourceFields=CurrentBrokerMarketSourceJsonFields();
   if(StringLen(marketSourceFields)<=0) return;
   int n = gMarketDataStrategySymbolCount;
   if(n <= 0) return;
   string specsJson = "{";
   int emitted = 0;
   for(int i = 0; i < n; i++) {
      string logicalSym = gMarketDataLogicalSymbols[i];
      string brokerSym = gMarketDataBrokerSymbols[i];
      double lotSize = MarketInfo(brokerSym, MODE_LOTSIZE);
      double point = MarketInfo(brokerSym, MODE_POINT);
      double tickSize = SymbolInfoDouble(brokerSym, SYMBOL_TRADE_TICK_SIZE);
      if(!MathIsValidNumber(lotSize) || !MathIsValidNumber(point) ||
         !MathIsValidNumber(tickSize) || lotSize <= 0.0 || point <= 0.0 ||
         tickSize <= 0.0) continue;
      if(emitted > 0) specsJson = specsJson + ",";
      specsJson = specsJson +
         "\"" + JsonEscape(logicalSym) + "\":{" +
         "\"broker_symbol\":\"" + JsonEscape(brokerSym) + "\"," +
         "\"trade_allowed\":" + JsonBool(MarketInfo(brokerSym, MODE_TRADEALLOWED)>0.5) + "," +
         "\"lot_size\":" + DoubleToString(lotSize, 2) + "," +
         "\"stop_level_points\":" + DoubleToString(MarketInfo(brokerSym, MODE_STOPLEVEL), 1) + "," +
         "\"freeze_level_points\":" + DoubleToString(MarketInfo(brokerSym, MODE_FREEZELEVEL), 1) + "," +
         "\"min_lot\":" + DoubleToString(MarketInfo(brokerSym, MODE_MINLOT), 4) + "," +
         "\"lot_step\":" + DoubleToString(MarketInfo(brokerSym, MODE_LOTSTEP), 4) + "," +
         "\"max_lot\":" + DoubleToString(MarketInfo(brokerSym, MODE_MAXLOT), 2) + "," +
         "\"tick_value\":" + DoubleToString(MarketInfo(brokerSym, MODE_TICKVALUE), 6) + "," +
         "\"tick_size\":" + DoubleToString(tickSize, 8) + "," +
         "\"margin_required\":" + DoubleToString(MarketInfo(brokerSym, MODE_MARGINREQUIRED), 2) + "," +
         "\"point\":" + DoubleToString(point, 8) + "," +
         "\"digits\":" + IntegerToString((int)MarketInfo(brokerSym, MODE_DIGITS)) +
         "}";
      emitted++;
   }
   specsJson = specsJson + "}";
   if(emitted <= 0) return;
   string payload =
      "{\"report_type\":\"symbol_specs\"" +
      marketSourceFields +
      ",\"account_leverage\":" + IntegerToString(AccountLeverage()) +
      ",\"specs\":" + specsJson +
      "}";
   post_report(payload);
}

void OnTick(){
   // Moved to OnTimer for consistent updates
}

bool BarHistoryMinuteEdgeGuardActive() {
   datetime now = TimeGMT();
   if(now <= 0) now = TimeLocal();
   if(now <= 0) return true;
   int secondsIntoMinute = (int)(now % SCALP_BAR_HISTORY_RESYNC_SECS);
   return(
      secondsIntoMinute >= 58 ||
      secondsIntoMinute <= SCALP_BAR_HISTORY_MINUTE_EDGE_DEADLINE_SECS
   );
}

void broadcastTick() {
   if(!gBridgeHandshakeCompatible) return;
   if(!EnsureMarketDataSymbolCache()) return;
   string marketSourceFields = CurrentBrokerMarketSourceJsonFields();
   if(StringLen(marketSourceFields) <= 0) return;
   int n = ArraySize(gMarketDataLogicalSymbols);
   if(
      n <= 0 || n != ArraySize(gMarketDataBrokerSymbols) ||
      CurrentMarketDataSymbolCacheIdentity() != gMarketDataSymbolCacheIdentity
   ) {
      ClearMarketDataSymbolCache();
      WarnMarketDataSymbolCache("cache_identity_or_shape_invalid");
      return;
   }

   string ticksJson = "";
   int emitted = 0;
   for(int i = 0; i < n; i++){
      string logicalSym = gMarketDataLogicalSymbols[i];
      string brokerSym = gMarketDataBrokerSymbols[i];
      if(StringLen(logicalSym) <= 0 || StringLen(brokerSym) <= 0) {
         ClearMarketDataSymbolCache();
         WarnMarketDataSymbolCache("cached_mapping_invalid");
         return;
      }
      double bid = MarketInfo(brokerSym, MODE_BID);
      double ask = MarketInfo(brokerSym, MODE_ASK);
      datetime sourceEventTime = (datetime)MarketInfo(brokerSym, MODE_TIME);
      string sourceEventToken = sourceEventTime > 0
         ? IntegerToString((int)sourceEventTime)
         : "";
      int digits = (int)MarketInfo(brokerSym, MODE_DIGITS);
      int spread_points = (int)MarketInfo(brokerSym, MODE_SPREAD);
      if(bid <= 0 || ask <= 0) continue;
      if(digits < 0) digits = Digits;

      double points_per_pip = (digits == 3 || digits == 5) ? 10.0 : 1.0;
      double spread_pips = ((double)spread_points) / points_per_pip;
      double mid = (bid + ask) / 2.0;
      double pip_size = PipSizeForSymbol(brokerSym);
      double spread_bps = 0.0;
      if(mid > 0){
         spread_bps = ((spread_pips * pip_size) / mid) * 10000.0;
      }

      string tick = "{\"symbol\":\"" + JsonEscape(logicalSym) +
                    "\",\"broker_symbol\":\"" + JsonEscape(brokerSym) +
                    "\",\"bid\":" + DoubleToString(bid, digits) +
                    ",\"ask\":" + DoubleToString(ask, digits) +
                    ",\"mid\":" + DoubleToString(mid, digits) +
                    ",\"spread\":" + DoubleToString(spread_pips, 3) +
                    ",\"spread_points\":" + IntegerToString(spread_points) +
                    ",\"spread_pips\":" + DoubleToString(spread_pips, 3) +
                    ",\"spread_bps\":" + DoubleToString(spread_bps, 6) +
                    ",\"source_event_token\":\"" + JsonEscape(sourceEventToken) + "\"" +
                    ",\"digits\":" + IntegerToString(digits) + "}";
      if(emitted > 0) ticksJson = ticksJson + ",";
      ticksJson = ticksJson + tick;
      emitted++;
    }
   if(emitted <= 0) return;

   // One authenticated frame preserves every per-symbol quote/event token but
   // pays the synchronous WinInet and producer-lease cost once, not 22 times.
   string payload = "{\"ticks\":[" + ticksJson + "]" + marketSourceFields + "}";
   HttpPOST(ApiBase + TickPath(), payload, gBridgeApiKey);
   int tickStatusCode = LastBridgeHttpStatus();
   WarnAuthFailure("tick", tickStatusCode);
   if(tickStatusCode <= 0 || tickStatusCode >= 500)
      ResetBarHistoryBootstrap("tick_transport_unavailable");
}

void OnTimer(){
   // AGENT HOT PATH: finalized direct M1 publication runs before ACK replay,
   // reports, dashboard reads, or command polling. A :59 maintenance request
   // therefore cannot be started during the guarded edge window, and the +1s
   // attempt gets first use of this timer event.
   bool handshakeCompatibleAtStart = gBridgeHandshakeCompatible;
   bool edgeGuardActive = BarHistoryMinuteEdgeGuardActive();
   datetime edgeNow = TimeGMT();
   if(edgeNow <= 0) edgeNow = TimeLocal();
   int edgeSecond = edgeNow > 0
      ? (int)(edgeNow % SCALP_BAR_HISTORY_RESYNC_SECS)
      : -1;
   bool highResolutionBarPublication =
      edgeSecond >= SCALP_BAR_HISTORY_MINUTE_EDGE_DELAY_SECS &&
      edgeSecond <= SCALP_BAR_HISTORY_MINUTE_EDGE_DEADLINE_SECS;
   if(handshakeCompatibleAtStart && highResolutionBarPublication)
      MaybeSyncBarHistory(false);

   // Only the finalized-bar scheduler runs at 250 ms. Keep command polling,
   // cycle management, tick transport, heartbeat, reports, and snapshots at
   // their historical approximately-one-second cadence. Unsigned subtraction
   // is intentionally wrap-safe across GetTickCount's rollover.
   uint mainBodyNowMs = GetTickCount();
   if(
      gMainTimerBodyAttempted &&
      (uint)(mainBodyNowMs - gLastMainTimerBodyMs) <
         (uint)BRIDGE_MAIN_TIMER_INTERVAL_MS
   ) return;
   gMainTimerBodyAttempted = true;
   gLastMainTimerBodyMs = mainBodyNowMs;

   // Recovery, coverage checks, and late reconciliation need at most one pass
   // per second. Running their identity/array scans on all four timer callbacks
   // added idle CPU without improving the exact +1..+4 publication cadence.
   if(handshakeCompatibleAtStart && !highResolutionBarPublication)
      MaybeSyncBarHistory(false);

   // ACK replay remains ahead of command polling, but is deferred for the
   // seven-second edge guard. Pending ACK state still fences polling, so this
   // cannot cause a broker command to execute twice.
   if(!edgeGuardActive)
      ServiceAckOutbox(ACK_OUTBOX_REPLAY_PER_TIMER);
   if(
      !gBridgeHandshakeCompatible ||
      (
         !edgeGuardActive &&
         (
            gLastBridgeHandshakeAttempt<=0 ||
            TimeCurrent()>gLastBridgeHandshakeAttempt+14
         )
      )
   ) VerifyBridgeHandshake();
   if(!edgeGuardActive) {
      EnsureBarHistorySymbolCache();
      ServiceOneBarHistoryMaterializationChart();
   }
   if(!handshakeCompatibleAtStart && gBridgeHandshakeCompatible)
      PrimeBarHistoryColdLoad();
   if(
      !edgeGuardActive && gBridgeHandshakeCompatible &&
      gBarHistoryInitialBootstrapAt <= 0
   ) PrimeBarHistoryColdLoad();
   manageCycle();
   broadcastTick(); // Send one authenticated all-symbol tick frame per second.
   if(!edgeGuardActive) ServiceOneAuxiliaryMaintenance();

   if(!gBridgeHandshakeCompatible) return;
   if(!AckOutboxAllowsCommandPolling()) return;

   string pollUrl = ApiBase + PollPath();
   string resp = HttpGET(pollUrl, gBridgeCommandToken);
   int pollStatus = LastBridgeHttpStatus();
   WarnAuthFailure("poll", pollStatus);
   if(pollStatus != 200 && pollStatus != 0) {
      return;
   }
   string respTrim = StringTrim(resp);
   if(StringLen(respTrim) > 0 && StringSubstr(respTrim, 0, 1) == "{") {
      // Avoid mis-parsing JSON error payloads as MT4 command lines.
      return;
   }
   if(StringLen(resp) > 0) {
      if(VerboseBridgeLog) {
         string preview = resp;
         if(StringLen(preview) > 220) preview = StringSubstr(preview, 0, 220) + "...";
         Print("[BRIDGE] Command received: ", preview);
      }
      HandleCmd(resp);
   }
}

bool IsValidOwnerToken(string ownerToken) {
   int length=StringLen(ownerToken);
   if(length<=0 || length>OWNER_TOKEN_MAX_LENGTH) return(false);
   for(int i=0; i<length; i++) {
      int code=(int)StringGetCharacter(ownerToken,i);
      bool alpha=(code>='A' && code<='Z') || (code>='a' && code<='z');
      bool digit=(code>='0' && code<='9');
      bool punctuation=code=='.' || code=='_' || code==':' || code=='-';
      if(!alpha && !digit && !punctuation) return(false);
   }
   return(true);
}

bool HasOwnerTokenPrefix(string brokerComment,string ownerToken) {
   return(
      IsValidOwnerToken(ownerToken) &&
      StringLen(brokerComment)>=StringLen(ownerToken) &&
      StringSubstr(brokerComment,0,StringLen(ownerToken))==ownerToken
   );
}

void ResetStrictManagementActuals(
   string cmd,
   int targetTicket,
   StrictManagementActuals &actual
) {
   actual.cmd=cmd;
   actual.logical_symbol="";
   actual.broker_symbol="";
   actual.side="";
   actual.execution_type="";
   actual.target_ticket=targetTicket;
   actual.magic=-1;
   actual.owner_token="";
   actual.order_comment="";
   actual.lots=0.0;
   actual.remaining_lots=0.0;
   actual.open_price=0.0;
   actual.sl_price=0.0;
   actual.tp_price=0.0;
   actual.close_time=0;
}

void CaptureSelectedManagementActuals(
   string cmd,
   int targetTicket,
   string expectedOwnerToken,
   StrictManagementActuals &actual
) {
   actual.cmd=cmd;
   actual.target_ticket=targetTicket;
   actual.broker_symbol=OrderSymbol();
   actual.logical_symbol=NormalizePairToken(actual.broker_symbol);
   int orderType=OrderType();
   actual.side=(orderType==OP_BUY)?"BUY":((orderType==OP_SELL)?"SELL":"");
   actual.execution_type=(orderType==OP_BUY || orderType==OP_SELL)
      ? "market"
      : "pending";
   actual.magic=OrderMagicNumber();
   actual.order_comment=OrderComment();
   actual.owner_token=HasOwnerTokenPrefix(
      actual.order_comment,expectedOwnerToken
   ) ? expectedOwnerToken : "";
   actual.lots=OrderLots();
   actual.remaining_lots=(OrderCloseTime()==0) ? OrderLots() : 0.0;
   actual.open_price=OrderOpenPrice();
   actual.sl_price=OrderStopLoss();
   actual.tp_price=OrderTakeProfit();
   actual.close_time=OrderCloseTime();
}

void ResetMarketEntryEnvelope(MarketEntryEnvelope &envelope) {
   envelope.has_any=false;
   envelope.execution_type_provided=false;
   envelope.pending_orders_forbidden_provided=false;
   envelope.pending_orders_forbidden=false;
   envelope.entry_deadline_epoch_provided=false;
   envelope.entry_deadline_epoch=0;
   envelope.plan_schema_provided=false;
   envelope.entry_quote_price_provided=false;
   envelope.entry_price_provided=false;
   envelope.worst_fill_price_provided=false;
   envelope.max_slippage_points_provided=false;
   envelope.protection_cushion_points_provided=false;
   envelope.contract_schema_provided=false;
   envelope.venue_id_provided=false;
   envelope.expected_symbol_provided=false;
   envelope.broker_symbol_provided=false;
   envelope.account_currency_provided=false;
   envelope.binding_sha256_provided=false;
   envelope.lot_size_provided=false;
   envelope.min_lot_provided=false;
   envelope.lot_step_provided=false;
   envelope.max_lot_provided=false;
   envelope.point_provided=false;
   envelope.tick_size_provided=false;
   envelope.margin_required_provided=false;
   envelope.margin_utilization_cap_provided=false;
   envelope.stop_level_points_provided=false;
   envelope.freeze_level_points_provided=false;
   envelope.digits_provided=false;
   envelope.trade_allowed_provided=false;
}

void ResetScalpStrategyAuthorityEnvelope(
   ScalpStrategyAuthorityEnvelope &authority
) {
   authority.schema="";
   authority.admission_mode="";
   authority.account_mode="";
   authority.generation_id="";
   authority.strategy_id="";
   authority.strategy_version="";
   authority.engine_sha256="";
   authority.config_id="";
   authority.config_sha256="";
   authority.validation_evidence_sha256="";
   authority.runtime_release_certificate_sha256="";
   authority.runtime_release_signing_key_id="";
   authority.research_evidence_sha256="";
   authority.research_evidence_signing_key_id="";
   authority.registry_generation_id="";
   authority.registry_revision_provided=false;
   authority.registry_revision=0;
   authority.registry_sha256="";
   authority.qualification_surface_sha256="";
   authority.cost_mapping_sha256="";
   authority.execution_contract_sha256="";
   authority.validation_expires_at_epoch_provided=false;
   authority.validation_expires_at_epoch=0;
   authority.venue_id="";
   authority.scope_version="";
   authority.binding_sha256="";
   authority.runtime_boot_id="";
   authority.authority_revision_provided=false;
   authority.authority_revision=0;
}

bool WireNumberTokenValid(string value) {
   string token=StringTrim(value);
   int length=StringLen(token);
   if(length<=0) return(false);
   int index=0;
   string ch=StringSubstr(token,index,1);
   if(ch=="+" || ch=="-") index++;
   int mantissaDigits=0;
   while(index<length) {
      int code=(int)StringGetCharacter(token,index);
      if(code<'0' || code>'9') break;
      mantissaDigits++;
      index++;
   }
   if(index<length && StringSubstr(token,index,1)==".") {
      index++;
      while(index<length) {
         int fractionCode=(int)StringGetCharacter(token,index);
         if(fractionCode<'0' || fractionCode>'9') break;
         mantissaDigits++;
         index++;
      }
   }
   if(mantissaDigits<=0) return(false);
   if(index<length) {
      ch=StringSubstr(token,index,1);
      if(ch!="e" && ch!="E") return(false);
      index++;
      if(index<length) {
         ch=StringSubstr(token,index,1);
         if(ch=="+" || ch=="-") index++;
      }
      int exponentDigits=0;
      while(index<length) {
         int exponentCode=(int)StringGetCharacter(token,index);
         if(exponentCode<'0' || exponentCode>'9') return(false);
         exponentDigits++;
         index++;
      }
      if(exponentDigits<=0) return(false);
   }
   return(index==length);
}

bool ParseFiniteWireNumber(string value,double &out) {
   out=0.0;
   if(!WireNumberTokenValid(value)) return(false);
   out=StrToDouble(StringTrim(value));
   return(MathIsValidNumber(out));
}

bool ParseNonNegativeWireInteger(string value,int &out) {
   out=0;
   string token=StringTrim(value);
   int length=StringLen(token);
   if(length<=0) return(false);
   for(int i=0; i<length; i++) {
      int code=(int)StringGetCharacter(token,i);
      if(code<'0' || code>'9') return(false);
   }
   long parsed=StringToInteger(token);
   if(parsed<0 || parsed>2147483647) return(false);
   out=(int)parsed;
   return(true);
}

bool IsSha256Hex(string value) {
   string token=StringTrim(value);
   if(StringLen(token)!=64) return(false);
   for(int i=0; i<64; i++) {
      int ch=(int)StringGetCharacter(token,i);
      bool digit=(ch>='0' && ch<='9');
      bool lower=(ch>='a' && ch<='f');
      bool upper=(ch>='A' && ch<='F');
      if(!digit && !lower && !upper) return(false);
   }
   return(true);
}

bool ValidateScalpStrategyAuthorityEnvelope(
   ScalpStrategyAuthorityEnvelope &authority,
   string &reason
) {
   reason="";
   string admissionMode=ToUpperSafe(authority.admission_mode);
   if(authority.schema!=PRODUCTION_SCALP_AUTHORITY_SCHEMA) {
      reason="strategy_authority_schema_invalid";
      return(false);
   }
   if(admissionMode!="SIGNED_VALIDATION") {
      reason="strategy_signed_validation_required";
      return(false);
   }
   string accountMode=ToUpperSafe(authority.account_mode);
   if(accountMode!="DEMO" && accountMode!="REAL") {
      reason="strategy_account_mode_invalid";
      return(false);
   }
   if(
      authority.strategy_id!=MTVCLC_STRATEGY_ID ||
      authority.strategy_version!=MTVCLC_STRATEGY_VERSION ||
      authority.config_id!=MTVCLC_CONFIG_ID
   ) {
      reason="strategy_mtvclc_identity_invalid";
      return(false);
   }
   if(ToUpperSafe(authority.venue_id)!=ToUpperSafe(IG_MT4_VENUE_ID)) {
      reason="strategy_venue_invalid";
      return(false);
   }
   if(authority.scope_version!=PRODUCTION_SCALP_SCOPE_VERSION) {
      reason="strategy_scope_version_invalid";
      return(false);
   }
   if(
      StringLen(StringTrim(authority.generation_id))<=0 ||
      StringLen(authority.generation_id)>128 ||
      authority.registry_generation_id!=authority.generation_id ||
      StringLen(StringTrim(authority.runtime_boot_id))<=0 ||
      StringLen(authority.runtime_boot_id)>128
   ) {
      reason="strategy_runtime_generation_invalid";
      return(false);
   }
   if(
      !authority.registry_revision_provided ||
      authority.registry_revision<=0 ||
      !authority.authority_revision_provided ||
      authority.authority_revision<=0
   ) {
      reason="strategy_authority_revision_invalid";
      return(false);
   }
   if(
      !IsSha256Hex(authority.engine_sha256) ||
      !IsSha256Hex(authority.config_sha256) ||
      !IsSha256Hex(authority.runtime_release_certificate_sha256) ||
      !IsSha256Hex(authority.runtime_release_signing_key_id) ||
      !IsSha256Hex(authority.research_evidence_sha256) ||
      !IsSha256Hex(authority.research_evidence_signing_key_id) ||
      !IsSha256Hex(authority.registry_sha256) ||
      !IsSha256Hex(authority.qualification_surface_sha256) ||
      !IsSha256Hex(authority.cost_mapping_sha256) ||
      !IsSha256Hex(authority.execution_contract_sha256) ||
      !IsSha256Hex(authority.binding_sha256)
   ) {
      reason="strategy_authority_digest_invalid";
      return(false);
   }
   datetime now=TimeGMT();
   if(now<=0) now=TimeCurrent();
   if(
      !authority.validation_expires_at_epoch_provided ||
      authority.validation_expires_at_epoch<=0 ||
      now<=0 ||
      now>=authority.validation_expires_at_epoch
   ) {
      reason="strategy_validation_expired";
      return(false);
   }
   reason="ok";
   return(true);
}

bool ContractNumberMatches(double actual,double expected,double absoluteTolerance) {
   if(!MathIsValidNumber(actual) || !MathIsValidNumber(expected)) return(false);
   double relativeTolerance=MathMax(MathAbs(actual),MathAbs(expected))*1e-9;
   return(MathAbs(actual-expected)<=MathMax(absoluteTolerance,relativeTolerance));
}

bool ScalpEntryDeadlineActive(
   MarketEntryEnvelope &envelope,
   string &reason
) {
   reason="";
   if(!envelope.entry_deadline_epoch_provided || envelope.entry_deadline_epoch<=0) {
      reason="scalp_entry_deadline_invalid";
      return(false);
   }
   // TimeCurrent can remain pinned when no broker tick arrives. Use the
   // continuously advancing UTC wall clock for this final no-late-entry
   // fence, with TimeLocal only as MT4's documented fallback.
   datetime now=TimeGMT();
   if(now<=0) now=TimeLocal();
   if(now<=0) {
      reason="scalp_entry_deadline_clock_unavailable";
      return(false);
   }
   if((int)now>=envelope.entry_deadline_epoch) {
      reason="scalp_entry_deadline_expired";
      return(false);
   }
   return(true);
}

bool ValidateExactMarketEntryEnvelope(
   MarketEntryEnvelope &envelope,
   string logicalSym,
   string side,
   bool requireScalpFields,
   string &reason
) {
   reason="";
   if(
      !envelope.execution_type_provided ||
      !envelope.pending_orders_forbidden_provided ||
      !envelope.entry_quote_price_provided || !envelope.entry_price_provided ||
      !envelope.worst_fill_price_provided || !envelope.max_slippage_points_provided ||
      !envelope.contract_schema_provided || !envelope.venue_id_provided ||
      !envelope.expected_symbol_provided || !envelope.broker_symbol_provided ||
      !envelope.account_currency_provided || !envelope.binding_sha256_provided ||
      !envelope.lot_size_provided || !envelope.min_lot_provided ||
      !envelope.lot_step_provided || !envelope.max_lot_provided ||
      !envelope.point_provided || !envelope.tick_size_provided ||
      !envelope.margin_required_provided ||
      !envelope.margin_utilization_cap_provided ||
      !envelope.stop_level_points_provided ||
      !envelope.freeze_level_points_provided || !envelope.digits_provided ||
      !envelope.trade_allowed_provided
   ) {
      reason=requireScalpFields
         ? "scalp_market_envelope_incomplete"
         : "market_entry_envelope_incomplete";
      return(false);
   }
   if(
      requireScalpFields &&
      (
         !envelope.plan_schema_provided ||
         !envelope.entry_deadline_epoch_provided ||
         !envelope.protection_cushion_points_provided
      )
   ) {
      reason="scalp_market_envelope_incomplete";
      return(false);
   }
   if(ToUpperSafe(envelope.execution_type)!="MARKET") {
      reason="scalp_execution_type_not_market";
      return(false);
   }
   if(!envelope.pending_orders_forbidden) {
      reason="scalp_pending_orders_not_forbidden";
      return(false);
   }
   if(requireScalpFields) {
      string deadlineReason="";
      if(!ScalpEntryDeadlineActive(envelope,deadlineReason)) {
         reason=deadlineReason;
         return(false);
      }
      if(envelope.plan_schema!=SCALP_BROKER_ENTRY_PLAN_SCHEMA) {
         reason="scalp_broker_entry_plan_schema_mismatch";
         return(false);
      }
   }
   if(envelope.contract_schema!=BROKER_CONTRACT_STATE_SCHEMA) {
      reason="scalp_broker_contract_schema_mismatch";
      return(false);
   }
   if(ToUpperSafe(envelope.venue_id)!=ToUpperSafe(IG_MT4_VENUE_ID)) {
      reason="scalp_broker_venue_mismatch";
      return(false);
   }
   if(ToUpperSafe(envelope.expected_symbol)!=ToUpperSafe(logicalSym)) {
      reason="scalp_expected_symbol_mismatch";
      return(false);
   }
   if(StringLen(StringTrim(envelope.broker_symbol))<=0) {
      reason="scalp_expected_broker_symbol_missing";
      return(false);
   }
   if(ToUpperSafe(envelope.account_currency)!=ToUpperSafe(AccountCurrency())) {
      reason="scalp_account_currency_mismatch";
      return(false);
   }
   if(!IsSha256Hex(envelope.binding_sha256)) {
      reason="scalp_broker_contract_binding_invalid";
      return(false);
   }
   if(
      !MathIsValidNumber(envelope.entry_quote_price) || envelope.entry_quote_price<=0.0 ||
      !MathIsValidNumber(envelope.entry_price) || envelope.entry_price<=0.0 ||
      !MathIsValidNumber(envelope.worst_fill_price) || envelope.worst_fill_price<=0.0 ||
      !MathIsValidNumber(envelope.lot_size) || envelope.lot_size<=0.0 ||
      !MathIsValidNumber(envelope.min_lot) || envelope.min_lot<=0.0 ||
      !MathIsValidNumber(envelope.lot_step) || envelope.lot_step<=0.0 ||
      !MathIsValidNumber(envelope.max_lot) || envelope.max_lot<=0.0 ||
      !MathIsValidNumber(envelope.point) || envelope.point<=0.0 ||
      !MathIsValidNumber(envelope.tick_size) || envelope.tick_size<=0.0 ||
      !MathIsValidNumber(envelope.margin_required) || envelope.margin_required<=0.0 ||
      !MathIsValidNumber(envelope.margin_utilization_cap) ||
      envelope.margin_utilization_cap<=0.0 ||
      envelope.margin_utilization_cap>1.0 ||
      !MathIsValidNumber(envelope.stop_level_points) || envelope.stop_level_points<0.0 ||
      !MathIsValidNumber(envelope.freeze_level_points) || envelope.freeze_level_points<0.0
   ) {
      reason="scalp_broker_contract_geometry_invalid";
      return(false);
   }
   if(
      envelope.max_slippage_points!=PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS ||
      envelope.digits<1 || envelope.digits>8 || !envelope.trade_allowed
   ) {
      reason="scalp_broker_contract_discrete_geometry_invalid";
      return(false);
   }
   if(
      requireScalpFields &&
      envelope.protection_cushion_points!=PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS
   ) {
      reason="scalp_broker_contract_discrete_geometry_invalid";
      return(false);
   }
   double tolerance=MathMax(envelope.point*1e-7,1e-12);
   if(MathAbs(envelope.entry_price-envelope.worst_fill_price)>tolerance) {
      reason="scalp_entry_worst_fill_mismatch";
      return(false);
   }
   double allowed=(double)envelope.max_slippage_points*envelope.point;
   if(side=="BUY") {
      if(
         envelope.worst_fill_price<envelope.entry_quote_price-tolerance ||
         envelope.worst_fill_price>envelope.entry_quote_price+allowed+tolerance
      ) {
         reason="scalp_buy_worst_fill_geometry_invalid";
         return(false);
      }
   } else if(side=="SELL") {
      if(
         envelope.worst_fill_price>envelope.entry_quote_price+tolerance ||
         envelope.worst_fill_price<envelope.entry_quote_price-allowed-tolerance
      ) {
         reason="scalp_sell_worst_fill_geometry_invalid";
         return(false);
      }
   } else {
      reason="scalp_entry_side_invalid";
      return(false);
   }
   return(true);
}

bool ValidateScalpMarketEntryEnvelope(
   MarketEntryEnvelope &envelope,
   string logicalSym,
   string side,
   string &reason
) {
   return ValidateExactMarketEntryEnvelope(
      envelope,logicalSym,side,true,reason
   );
}

bool ValidateModelStackMarketEntryEnvelope(
   MarketEntryEnvelope &envelope,
   string logicalSym,
   string side,
   string &reason
) {
   return ValidateExactMarketEntryEnvelope(
      envelope,logicalSym,side,false,reason
   );
}

bool IsStrictTicketOwnerContract(
   string ownershipContract,
   int targetTicket,
   bool magicProvided,
   int targetMagic,
   string ownerToken
) {
   return(
      ownershipContract==TICKET_OWNER_CONTRACT &&
      targetTicket>0 &&
      magicProvided && targetMagic>0 &&
      IsValidOwnerToken(ownerToken)
   );
}

void HandleCmd(string line){
   string items[]; int n = StringSplit(line,';',items);
   uint t_handle_start_ms = GetTickCount();
   double t_ea_received = (double)TimeCurrent();
   string cmd="", sym="", signal_id="", command_id="", intent="", trace_id="", interop_mode="";
   string expected_account_mode="", expected_account_scope="";
   string expected_strategy_admission_mode="", expected_strategy_account_mode="";
   string owner_token="", ownership_contract="";
   MarketEntryEnvelope marketEntryEnvelope;
   ResetMarketEntryEnvelope(marketEntryEnvelope);
   ScalpStrategyAuthorityEnvelope strategyAuthority;
   ResetScalpStrategyAuthorityEnvelope(strategyAuthority);
   double lots=0, close_lots=0, tp_cash=0, tp_price=0, sl=0, action_score=0, t_py_signal_post_start=0, t_bridge_queued=0, t_bridge_delivered=0;
   string action="", reversal_token="";
   int magic=Magic, target_ticket=-1;
   bool magic_provided=false, target_ticket_provided=false, owner_token_provided=false;
   for(int i=0;i<n;i++){
      if(StringFind(items[i],"target_ticket=",0)==0) target_ticket_provided=true;
      if(StringFind(items[i],"owner_token=",0)==0) owner_token_provided=true;
      string kv[]; if(StringSplit(items[i],'=',kv)!=2) continue;
      string k=StringTrim(kv[0]), v=StringTrim(kv[1]);
      if(k=="cmd") cmd=v;
      if(k=="symbol") sym=v;
      if(k=="lots") lots=StrToDouble(v);
      if(k=="tp_cash") tp_cash=StrToDouble(v);
      if(k=="tp_price") tp_price=StrToDouble(v);
      if(k=="sl") sl=StrToDouble(v);
      if(k=="close_lots") close_lots=StrToDouble(v);
      if(k=="magic") { magic=(int)StrToInteger(v); magic_provided=true; }
      if(k=="target_ticket") {
         target_ticket=(int)StrToInteger(v);
         target_ticket_provided=true;
      }
      if(k=="owner_token") { owner_token=v; owner_token_provided=true; }
      if(k=="ownership_contract") ownership_contract=v;
      if(k=="signal_id") signal_id=v;
      if(k=="command_id") command_id=v;
      if(k=="intent") intent=v;
      if(k=="trace_id") trace_id=v;
      if(k=="interop_mode") interop_mode=v;
      if(k=="expected_account_mode") expected_account_mode=ToUpperSafe(v);
      if(k=="expected_account_scope") expected_account_scope=v;
      if(k=="expected_strategy_admission_mode") {
         expected_strategy_admission_mode=ToUpperSafe(v);
         strategyAuthority.admission_mode=v;
      }
      if(k=="expected_strategy_account_mode") {
         expected_strategy_account_mode=ToUpperSafe(v);
         strategyAuthority.account_mode=v;
      }
      if(k=="expected_strategy_authority_schema") strategyAuthority.schema=v;
      if(k=="expected_strategy_generation_id") strategyAuthority.generation_id=v;
      if(k=="expected_strategy_id") strategyAuthority.strategy_id=v;
      if(k=="expected_strategy_version") strategyAuthority.strategy_version=v;
      if(k=="expected_strategy_engine_sha256") strategyAuthority.engine_sha256=v;
      if(k=="expected_strategy_config_id") strategyAuthority.config_id=v;
      if(k=="expected_strategy_config_sha256") strategyAuthority.config_sha256=v;
      if(k=="expected_strategy_validation_evidence_sha256")
         strategyAuthority.validation_evidence_sha256=v;
      if(k=="expected_strategy_runtime_release_certificate_sha256")
         strategyAuthority.runtime_release_certificate_sha256=v;
      if(k=="expected_strategy_runtime_release_signing_key_id")
         strategyAuthority.runtime_release_signing_key_id=v;
      if(k=="expected_strategy_research_evidence_sha256")
         strategyAuthority.research_evidence_sha256=v;
      if(k=="expected_strategy_research_evidence_signing_key_id")
         strategyAuthority.research_evidence_signing_key_id=v;
      if(k=="expected_strategy_registry_generation_id")
         strategyAuthority.registry_generation_id=v;
      if(k=="expected_strategy_registry_revision") {
         strategyAuthority.registry_revision_provided=ParseNonNegativeWireInteger(
            v,strategyAuthority.registry_revision
         );
      }
      if(k=="expected_strategy_registry_sha256")
         strategyAuthority.registry_sha256=v;
      if(k=="expected_strategy_qualification_surface_sha256")
         strategyAuthority.qualification_surface_sha256=v;
      if(k=="expected_strategy_cost_mapping_sha256")
         strategyAuthority.cost_mapping_sha256=v;
      if(k=="expected_strategy_execution_contract_sha256")
         strategyAuthority.execution_contract_sha256=v;
      if(k=="expected_strategy_validation_expires_at_epoch") {
         strategyAuthority.validation_expires_at_epoch_provided=
            ParseNonNegativeWireInteger(
               v,strategyAuthority.validation_expires_at_epoch
            );
      }
      if(k=="expected_strategy_venue_id") strategyAuthority.venue_id=v;
      if(k=="expected_strategy_scope_version") strategyAuthority.scope_version=v;
      if(k=="expected_strategy_binding_sha256") strategyAuthority.binding_sha256=v;
      if(k=="expected_strategy_runtime_boot_id") strategyAuthority.runtime_boot_id=v;
      if(k=="expected_strategy_authority_revision") {
         strategyAuthority.authority_revision_provided=ParseNonNegativeWireInteger(
            v,strategyAuthority.authority_revision
         );
      }
      if(k=="execution_type") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.execution_type=v;
         marketEntryEnvelope.execution_type_provided=(StringLen(v)>0);
      }
      if(k=="pending_orders_forbidden") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.pending_orders_forbidden_provided=(v=="true" || v=="false");
         marketEntryEnvelope.pending_orders_forbidden=(v=="true");
      }
      if(k=="entry_deadline_epoch") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.entry_deadline_epoch_provided=ParseNonNegativeWireInteger(
            v,marketEntryEnvelope.entry_deadline_epoch
         ) && marketEntryEnvelope.entry_deadline_epoch>0;
      }
      if(k=="broker_entry_plan_schema") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.plan_schema=v;
         marketEntryEnvelope.plan_schema_provided=(StringLen(v)>0);
      }
      if(k=="entry_quote_price") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.entry_quote_price_provided=ParseFiniteWireNumber(
            v,marketEntryEnvelope.entry_quote_price
         );
      }
      if(k=="entry_price") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.entry_price_provided=ParseFiniteWireNumber(
            v,marketEntryEnvelope.entry_price
         );
      }
      if(k=="worst_fill_price") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.worst_fill_price_provided=ParseFiniteWireNumber(
            v,marketEntryEnvelope.worst_fill_price
         );
      }
      if(k=="max_slippage_points") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.max_slippage_points_provided=ParseNonNegativeWireInteger(
            v,marketEntryEnvelope.max_slippage_points
         );
      }
      if(k=="protection_cushion_points") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.protection_cushion_points_provided=ParseNonNegativeWireInteger(
            v,marketEntryEnvelope.protection_cushion_points
         );
      }
      if(k=="expected_broker_contract_state_schema") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.contract_schema=v;
         marketEntryEnvelope.contract_schema_provided=(StringLen(v)>0);
      }
      if(k=="expected_broker_contract_venue_id") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.venue_id=v;
         marketEntryEnvelope.venue_id_provided=(StringLen(v)>0);
      }
      if(k=="expected_broker_contract_symbol") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.expected_symbol=v;
         marketEntryEnvelope.expected_symbol_provided=(StringLen(v)>0);
      }
      if(k=="expected_broker_contract_broker_symbol") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.broker_symbol=v;
         marketEntryEnvelope.broker_symbol_provided=(StringLen(v)>0);
      }
      if(k=="expected_broker_contract_account_currency") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.account_currency=v;
         marketEntryEnvelope.account_currency_provided=(StringLen(v)>0);
      }
      if(k=="expected_broker_contract_binding_sha256") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.binding_sha256=v;
         marketEntryEnvelope.binding_sha256_provided=(StringLen(v)>0);
      }
      if(k=="expected_broker_contract_lot_size") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.lot_size_provided=ParseFiniteWireNumber(v,marketEntryEnvelope.lot_size);
      }
      if(k=="expected_broker_contract_min_lot") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.min_lot_provided=ParseFiniteWireNumber(v,marketEntryEnvelope.min_lot);
      }
      if(k=="expected_broker_contract_lot_step") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.lot_step_provided=ParseFiniteWireNumber(v,marketEntryEnvelope.lot_step);
      }
      if(k=="expected_broker_contract_max_lot") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.max_lot_provided=ParseFiniteWireNumber(v,marketEntryEnvelope.max_lot);
      }
      if(k=="expected_broker_contract_point") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.point_provided=ParseFiniteWireNumber(v,marketEntryEnvelope.point);
      }
      if(k=="expected_broker_contract_tick_size") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.tick_size_provided=ParseFiniteWireNumber(v,marketEntryEnvelope.tick_size);
      }
      if(k=="expected_broker_contract_margin_required") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.margin_required_provided=ParseFiniteWireNumber(
            v,marketEntryEnvelope.margin_required
         );
      }
      if(k=="broker_contract_margin_utilization_cap") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.margin_utilization_cap_provided=ParseFiniteWireNumber(
            v,marketEntryEnvelope.margin_utilization_cap
         );
      }
      if(k=="expected_broker_contract_stop_level_points") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.stop_level_points_provided=ParseFiniteWireNumber(
            v,marketEntryEnvelope.stop_level_points
         );
      }
      if(k=="expected_broker_contract_freeze_level_points") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.freeze_level_points_provided=ParseFiniteWireNumber(
            v,marketEntryEnvelope.freeze_level_points
         );
      }
      if(k=="expected_broker_contract_digits") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.digits_provided=ParseNonNegativeWireInteger(v,marketEntryEnvelope.digits);
      }
      if(k=="expected_broker_contract_trade_allowed") {
         marketEntryEnvelope.has_any=true;
         marketEntryEnvelope.trade_allowed_provided=(v=="0" || v=="1");
         marketEntryEnvelope.trade_allowed=(v=="1");
      }
      if(k=="action") action=v;
      if(k=="action_score") action_score=StrToDouble(v);
      if(k=="reversal_token") reversal_token=v;
      if(k=="t_py_signal_post_start") t_py_signal_post_start=StrToDouble(v);
      if(k=="t_bridge_queued") t_bridge_queued=StrToDouble(v);
      if(k=="t_bridge_delivered") t_bridge_delivered=StrToDouble(v);
   }
   cmd = ToUpperSafe(StringTrim(cmd));
   sym = StringTrim(sym);
   signal_id = StringTrim(signal_id);
   command_id = StringTrim(command_id);
   owner_token = StringTrim(owner_token);
   ownership_contract = StringTrim(ownership_contract);
   intent = ToUpperSafe(StringTrim(intent));
   if(StringLen(signal_id) <= 0 && StringLen(command_id) > 0) signal_id = command_id;

   if(StringLen(cmd) <= 0){
      post_report("ERR malformed_cmd");
      post_ack(
         signal_id, "failed", sym, -1, 400, "malformed_cmd",
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
      );
      return;
   }
   if(cmd!="INFO" && StringLen(signal_id) > 0){
      int durableOutcomeState=ReplayDurableSignalOutcome(signal_id);
      if(durableOutcomeState==1) {
         post_report(
            "REPLAY durable_outcome signal_id="+signal_id+
            " cmd="+cmd+" sym="+sym
         );
         return;
      }
      if(durableOutcomeState<0) {
         post_report(
            "ERR durable_outcome_ambiguous signal_id="+signal_id+
            " cmd="+cmd+" sym="+sym
         );
         post_ack(
            signal_id,"reconcile_required",sym,-1,409,
            "durable_outcome_ambiguous",
            trace_id,t_py_signal_post_start,t_bridge_queued,t_bridge_delivered,
            t_ea_received,0.0,0.0,(double)(GetTickCount()-t_handle_start_ms),
            interop_mode,magic,owner_token,"unknown",cmd
         );
         return;
      }
      if(SeenSignalRecently(signal_id)){
         // A memory-only duplicate without a durable prior outcome may have
         // crossed an execution boundary.  It is never safe to manufacture a
         // ticketless duplicate ACK or execute the command again.
         post_report("ERR memory_duplicate_without_outcome signal_id=" + signal_id + " cmd=" + cmd + " sym=" + sym);
         post_ack(
            signal_id, "reconcile_required", sym, -1, 409,
            "memory_duplicate_without_durable_outcome",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token, "unknown", cmd
         );
         return;
      }
      RememberSignalId(signal_id);
   }
   if(cmd=="CLOSE_ALL"){
      if(
         ownership_contract!=LEGACY_ELBRIDGE_CONTRACT ||
         !magic_provided || magic<=0 ||
         target_ticket_provided || owner_token_provided
      ){
         post_report("ERR close_all ownership_contract_invalid");
         post_ack(
            signal_id, "failed", "", -1, 403, "ownership_contract_invalid",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token
         );
         return;
      }
      int closeErr = 0;
      bool okAll = CloseAll(magic, LEGACY_ORDER_COMMENT, closeErr);
      resetCycle();
      if(okAll){
         post_report("CLOSE_ALL_OK");
         post_ack(
            signal_id, "acked", "", -1, 0, "close_all_ok",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token
         );
      } else {
         post_report("ERR close_all " + IntegerToString(closeErr));
         post_ack(
            signal_id, "failed", "", -1, closeErr, "close_all_failed",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token
         );
      }
      return;
   }
   if(cmd=="CLOSE"){
      if(StringLen(sym) <= 0){
         post_report("ERR close missing_symbol");
         post_ack(
            signal_id, "failed", sym, -1, 400, "missing_symbol",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
         return;
      }
      bool strictClose=IsStrictTicketOwnerContract(
         ownership_contract,target_ticket,magic_provided,magic,owner_token
      );
      bool legacyClose=(
         ownership_contract==LEGACY_ELBRIDGE_CONTRACT &&
         !target_ticket_provided && !owner_token_provided &&
         magic_provided && magic>0
      );
      if(!strictClose && !legacyClose){
         post_report("ERR close ownership_contract_invalid");
         post_ack(
            signal_id, "failed", sym, -1, 403, "ownership_contract_invalid",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token
         );
         return;
      }
      int closeErr2 = 0;
      string closeReason="";
      if(strictClose) {
         StrictManagementActuals closeActual;
         bool closeAttempted=false;
         bool closeConfirmed=false;
         bool okClose=CloseOwnedTicket(
            target_ticket,sym,magic,owner_token,closeActual,
            closeAttempted,closeConfirmed,closeErr2,closeReason
         );
         post_strict_management_ack(
            signal_id,(okClose && closeConfirmed)?"acked":"failed",sym,
            target_ticket,(okClose && closeConfirmed)?0:closeErr2,
            (okClose && closeConfirmed)
               ? "close_ok"
               : (StringLen(closeReason)>0 ? closeReason : "close_failed"),
            closeAttempted,closeConfirmed,closeActual,
            trace_id,t_py_signal_post_start,t_bridge_queued,t_bridge_delivered,
            t_ea_received,(double)(GetTickCount()-t_handle_start_ms),
            interop_mode,magic,owner_token
         );
      } else {
         bool okClose=CloseSymbol(sym,magic,closeErr2);
         post_ack(
            signal_id,okClose?"acked":"failed",sym,-1,
            okClose?0:closeErr2,okClose?"close_ok":"close_failed",
            trace_id,t_py_signal_post_start,t_bridge_queued,t_bridge_delivered,
            t_ea_received,0.0,0.0,(double)(GetTickCount()-t_handle_start_ms),
            interop_mode,magic,owner_token
         );
      }
      return;
   }
   if(cmd=="CLOSE_PARTIAL"){
      if(StringLen(sym) <= 0){
         post_report("ERR close_partial missing_symbol");
         post_ack(
            signal_id, "failed", sym, -1, 400, "missing_symbol",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
         return;
      }
      if(!MathIsValidNumber(close_lots) || close_lots <= 0.0){
         post_report("ERR close_partial invalid_lots");
         post_ack(
            signal_id, "failed", sym, -1, 400, "invalid_lots",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
         return;
      }
      bool strictPartial=IsStrictTicketOwnerContract(
         ownership_contract,target_ticket,magic_provided,magic,owner_token
      );
      bool legacyPartial=(
         ownership_contract==LEGACY_ELBRIDGE_CONTRACT &&
         !target_ticket_provided && !owner_token_provided &&
         magic_provided && magic>0
      );
      if(!strictPartial && !legacyPartial){
         post_report("ERR close_partial ownership_contract_invalid");
         post_ack(
            signal_id, "failed", sym, -1, 403, "ownership_contract_invalid",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token
         );
         return;
      }
      int closeErr3 = 0;
      string partialReason="";
      if(strictPartial) {
         StrictManagementActuals partialActual;
         bool partialAttempted=false;
         bool partialConfirmed=false;
         bool okClosePartial=CloseOwnedTicketPartial(
            target_ticket,sym,magic,owner_token,close_lots,partialActual,
            partialAttempted,partialConfirmed,closeErr3,partialReason
         );
         post_strict_management_ack(
            signal_id,(okClosePartial && partialConfirmed)?"acked":"failed",sym,
            target_ticket,(okClosePartial && partialConfirmed)?0:closeErr3,
            (okClosePartial && partialConfirmed)
               ? "close_partial_ok"
               : (StringLen(partialReason)>0
                    ? partialReason
                    : "close_partial_failed"),
            partialAttempted,partialConfirmed,partialActual,
            trace_id,t_py_signal_post_start,t_bridge_queued,t_bridge_delivered,
            t_ea_received,(double)(GetTickCount()-t_handle_start_ms),
            interop_mode,magic,owner_token
         );
      } else {
         bool okClosePartial=CloseSymbolPartial(
            sym,magic,close_lots,closeErr3
         );
         post_ack(
            signal_id,okClosePartial?"acked":"failed",sym,-1,
            okClosePartial?0:closeErr3,
            okClosePartial?"close_partial_ok":"close_partial_failed",
            trace_id,t_py_signal_post_start,t_bridge_queued,t_bridge_delivered,
            t_ea_received,0.0,0.0,(double)(GetTickCount()-t_handle_start_ms),
            interop_mode,magic,owner_token
         );
      }
      return;
   }
   if(cmd=="MODIFY_SL"){
      if(StringLen(sym) <= 0){
         post_report("ERR modify_sl missing_symbol");
         post_ack(
            signal_id, "failed", sym, -1, 400, "missing_symbol",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
         return;
      }
      if(sl <= 0){
         post_report("ERR modify_sl invalid_sl");
         post_ack(
            signal_id, "failed", sym, -1, 400, "invalid_sl",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
         return;
      }
      bool strictModify=IsStrictTicketOwnerContract(
         ownership_contract,target_ticket,magic_provided,magic,owner_token
      );
      bool legacyModify=(
         ownership_contract==LEGACY_ELBRIDGE_CONTRACT &&
         !target_ticket_provided && !owner_token_provided &&
         magic_provided && magic>0
      );
      if(!strictModify && !legacyModify){
         post_report("ERR modify_sl ownership_contract_invalid");
         post_ack(
            signal_id, "failed", sym, -1, 403, "ownership_contract_invalid",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token
         );
         return;
      }
      int modErr = 0;
      string modifyReason="";
      if(strictModify) {
         StrictManagementActuals modifyActual;
         bool modifyAttempted=false;
         bool modifyConfirmed=false;
         bool okModify=ModifyOwnedTicketStop(
            target_ticket,sym,magic,owner_token,sl,modifyActual,
            modifyAttempted,modifyConfirmed,modErr,modifyReason
         );
         post_strict_management_ack(
            signal_id,(okModify && modifyConfirmed)?"acked":"failed",sym,
            target_ticket,(okModify && modifyConfirmed)?0:modErr,
            (okModify && modifyConfirmed)
               ? "modify_sl_ok"
               : (StringLen(modifyReason)>0
                    ? modifyReason
                    : "modify_sl_failed"),
            modifyAttempted,modifyConfirmed,modifyActual,
            trace_id,t_py_signal_post_start,t_bridge_queued,t_bridge_delivered,
            t_ea_received,(double)(GetTickCount()-t_handle_start_ms),
            interop_mode,magic,owner_token
         );
      } else {
         bool okModify=ModifySymbolStop(sym,magic,sl,modErr);
         post_ack(
            signal_id,okModify?"acked":"failed",sym,-1,
            okModify?0:modErr,okModify?"modify_sl_ok":"modify_sl_failed",
            trace_id,t_py_signal_post_start,t_bridge_queued,t_bridge_delivered,
            t_ea_received,0.0,0.0,(double)(GetTickCount()-t_handle_start_ms),
            interop_mode,magic,owner_token
         );
      }
      return;
   }
   if(cmd=="INFO"){ 
      string thought="";
      int p = StringFind(line, "thought=");
      if(p >= 0) {
         thought = StringSubstr(line, p + 8);
      }
      if(StringLen(thought) > 1400) thought = StringSubstr(thought, 0, 1400);
      UpdateDashboard(thought);
      if(StringLen(signal_id) > 0){
         post_ack(
            signal_id, "acked", sym, -1, 0, "info_consumed",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
      }
      return; 
   }
   if(cmd=="BUY" || cmd=="SELL"){
      bool productionScalperEntry=(intent==PRODUCTION_SCALPER_ENTRY_INTENT);
      string current_account_mode = ToUpperSafe(CurrentBrokerAccountMode());
      string current_account_scope = CurrentBrokerAccountScope(magic);
      string strategyAuthorityReason="";
      if(
         productionScalperEntry &&
         !ValidateScalpStrategyAuthorityEnvelope(
            strategyAuthority,strategyAuthorityReason
         )
      ){
         post_report("ERR trade " + strategyAuthorityReason);
         post_ack(
            signal_id, "failed", sym, -1, 403, strategyAuthorityReason,
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token
         );
         return;
      }
      if(
         productionScalperEntry &&
         (
            StringLen(expected_strategy_account_mode)<=0 ||
            expected_strategy_account_mode!=current_account_mode
         )
      ){
         post_report("ERR trade strategy_account_mode_mismatch");
         post_ack(
            signal_id, "failed", sym, -1, 403, "strategy_account_mode_mismatch",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token
         );
         return;
      }
      if(
         StringLen(expected_account_mode) <= 0 || StringLen(expected_account_scope) <= 0 ||
         expected_account_mode != current_account_mode ||
         expected_account_scope != current_account_scope
      ){
         post_report("ERR trade broker_account_mismatch");
         post_ack(
            signal_id, "failed", sym, -1, 403, "broker_account_mismatch",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token
         );
         return;
      }
      string marketEntryEnvelopeReason="";
      if(
         productionScalperEntry &&
         !ValidateScalpMarketEntryEnvelope(
            marketEntryEnvelope,NormalizePairToken(sym),cmd,marketEntryEnvelopeReason
         )
      ) {
         post_report("ERR trade "+marketEntryEnvelopeReason);
         post_ack(
            signal_id,"failed",sym,-1,400,marketEntryEnvelopeReason,
            trace_id,t_py_signal_post_start,t_bridge_queued,t_bridge_delivered,
            t_ea_received,0.0,0.0,(double)(GetTickCount()-t_handle_start_ms),
            interop_mode,magic,owner_token,"not_attempted",cmd
         );
         return;
      }
      if(
         !productionScalperEntry &&
         !ValidateModelStackMarketEntryEnvelope(
            marketEntryEnvelope,NormalizePairToken(sym),cmd,marketEntryEnvelopeReason
         )
      ) {
         post_report("ERR trade "+marketEntryEnvelopeReason);
         post_ack(
            signal_id,"failed",sym,-1,400,marketEntryEnvelopeReason,
            trace_id,t_py_signal_post_start,t_bridge_queued,t_bridge_delivered,
            t_ea_received,0.0,0.0,(double)(GetTickCount()-t_handle_start_ms),
            interop_mode,magic,owner_token,"not_attempted",cmd
         );
         return;
      }
      if(
         ownership_contract!=TICKET_OWNER_CONTRACT ||
         !magic_provided || magic<=0 || !IsValidOwnerToken(owner_token)
      ){
         post_report("ERR trade ownership_contract_invalid");
         post_ack(
            signal_id, "failed", sym, -1, 403, "ownership_contract_invalid",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token
         );
         return;
      }
      if(StringLen(sym) <= 0){
         post_report("ERR trade missing_symbol");
         post_ack(
            signal_id, "failed", sym, -1, 400, "missing_symbol",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token
         );
         return;
      }
      if(!MathIsValidNumber(lots) || lots <= 0.0){
         post_report("ERR trade invalid_lots");
         post_ack(
            signal_id, "failed", sym, -1, 400, "invalid_lots",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms),
            interop_mode, magic, owner_token
         );
         return;
      }
      Execute(
         cmd, sym, lots, tp_cash, tp_price, sl, magic, signal_id, intent,
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_handle_start_ms, interop_mode, owner_token,
         productionScalperEntry, expected_strategy_admission_mode, marketEntryEnvelope
      );
      return;
   }
   post_report("ERR unknown_cmd " + cmd);
   post_ack(
      signal_id, "failed", sym, -1, 400, "unknown_cmd",
      trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
      t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
   );
}

void UpdateDashboard(string text) {
   // --- Modern Dark Theme ---
   color  BG_COLOR       = C'33,37,43'; // VSCode-like Dark
   color  HEADER_COLOR   = C'40,44,52'; // Slightly lighter header
   color  ACCENT_COLOR   = C'97,175,239'; // Blue highlight
   color  TEXT_MAIN      = C'220,223,228';
   color  TEXT_DIM       = C'150,150,150';
   
   int    WIDTH          = 280; 
   int    ROW_HEIGHT     = 20;
   int    PADDING        = 12;
   int    HDR_HEIGHT     = 30;
   int    FONT_SIZE      = 9;
   string FONT_NAME      = "Consolas"; // Monospace for alignment
   
   StringReplace(text, "\r", " ");
   StringReplace(text, "\n", " | ");
   if(StringLen(text) > 1400) text = StringSubstr(text, 0, 1400);

   long chartWidth = ChartGetInteger(0, CHART_WIDTH_IN_PIXELS);
   long chartHeight = ChartGetInteger(0, CHART_HEIGHT_IN_PIXELS);
   if(
      text == gDashboardLastText &&
      chartWidth == gDashboardLastChartWidth &&
      chartHeight == gDashboardLastChartHeight
   ) return;

   // --- PARSING ---
   string lines[]; 
   int n = StringSplit(text, '|', lines);
   // StringSplit returns zero elements for an empty INFO/dashboard payload.
   // Allocate the single fallback row before any lines[0] access; otherwise an
   // INFO command without `thought` raises an array-out-of-range fault and
   // terminates the EA timer before its durable ACK can be written.
   if(n < 1) {
      ArrayResize(lines, 1);
      lines[0] = "";
      n = 1;
   }
   int MAX_ROWS = 14;
   int shown = n;
   if(shown > MAX_ROWS) shown = MAX_ROWS;
   int totalHeight = (shown * ROW_HEIGHT) + HDR_HEIGHT + (PADDING * 2);
   
   // --- POSITIONING ---
   int xPos = (int)(chartWidth - WIDTH - 20); // Top-Right corner
   int yPos = 40; 

   // --- 1. Main Background Panel ---
   string bgName = "BridgeHUD_BG";
   if(ObjectFind(0, bgName) < 0) {
      ObjectCreate(0, bgName, OBJ_RECTANGLE_LABEL, 0, 0, 0);
      ObjectSetInteger(0, bgName, OBJPROP_BORDER_TYPE, BORDER_FLAT);
      ObjectSetInteger(0, bgName, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, bgName, OBJPROP_BGCOLOR, BG_COLOR);
      ObjectSetInteger(0, bgName, OBJPROP_BACK, false);
      ObjectSetInteger(0, bgName, OBJPROP_SELECTABLE, false); // NO CRASH
      ObjectSetInteger(0, bgName, OBJPROP_SELECTED, false);
      ObjectSetInteger(0, bgName, OBJPROP_HIDDEN, true);
   }
   ObjectSetInteger(0, bgName, OBJPROP_XDISTANCE, xPos);
   ObjectSetInteger(0, bgName, OBJPROP_YDISTANCE, yPos);
   ObjectSetInteger(0, bgName, OBJPROP_XSIZE, WIDTH);
   ObjectSetInteger(0, bgName, OBJPROP_YSIZE, totalHeight);

   // --- 2. Header Bar ---
   string hdrBgName = "BridgeHUD_HdrBG";
   if(ObjectFind(0, hdrBgName) < 0) {
      ObjectCreate(0, hdrBgName, OBJ_RECTANGLE_LABEL, 0, 0, 0);
      ObjectSetInteger(0, hdrBgName, OBJPROP_BORDER_TYPE, BORDER_FLAT);
      ObjectSetInteger(0, hdrBgName, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, hdrBgName, OBJPROP_BGCOLOR, HEADER_COLOR);
      ObjectSetInteger(0, hdrBgName, OBJPROP_SELECTABLE, false); // NO CRASH
      ObjectSetInteger(0, hdrBgName, OBJPROP_HIDDEN, true);
   }
   ObjectSetInteger(0, hdrBgName, OBJPROP_XDISTANCE, xPos);
   ObjectSetInteger(0, hdrBgName, OBJPROP_YDISTANCE, yPos);
   ObjectSetInteger(0, hdrBgName, OBJPROP_XSIZE, WIDTH);
   ObjectSetInteger(0, hdrBgName, OBJPROP_YSIZE, HDR_HEIGHT);

   // --- 3. Header Text & Live Status ---
   string titleName = "BridgeHUD_Title";
   if(ObjectFind(0, titleName) < 0) {
      ObjectCreate(0, titleName, OBJ_LABEL, 0, 0, 0);
      ObjectSetString(0, titleName, OBJPROP_FONT, "Segoe UI Bold");
      ObjectSetInteger(0, titleName, OBJPROP_FONTSIZE, 10);
      ObjectSetString(0, titleName, OBJPROP_TEXT, "AI AGENT :: LIVE");
      ObjectSetInteger(0, titleName, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, titleName, OBJPROP_COLOR, ACCENT_COLOR);
      ObjectSetInteger(0, titleName, OBJPROP_SELECTABLE, false); // NO CRASH
      ObjectSetInteger(0, titleName, OBJPROP_HIDDEN, true);
   }
   ObjectSetInteger(0, titleName, OBJPROP_XDISTANCE, xPos + PADDING);
   ObjectSetInteger(0, titleName, OBJPROP_YDISTANCE, yPos + 8);

   // Live Pulse Dot (Wingdings)
   string dotName = "BridgeHUD_Dot";
   if(ObjectFind(0, dotName) < 0) {
      ObjectCreate(0, dotName, OBJ_LABEL, 0, 0, 0);
      ObjectSetString(0, dotName, OBJPROP_FONT, "Wingdings"); // Circle
      ObjectSetInteger(0, dotName, OBJPROP_FONTSIZE, 10);
      ObjectSetString(0, dotName, OBJPROP_TEXT, "l"); // Filled circle char
      ObjectSetInteger(0, dotName, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, dotName, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, dotName, OBJPROP_HIDDEN, true);
   }
   ObjectSetInteger(0, dotName, OBJPROP_XDISTANCE, xPos + WIDTH - 25);
   ObjectSetInteger(0, dotName, OBJPROP_YDISTANCE, yPos + 8);
   
   // --- DYNAMIC STATUS LOGIC ---
   color dotColor = clrSilver; 
   color titleColor = ACCENT_COLOR; // Default Blue
   string titleText = "AI AGENT :: LIVE";
   
   if(shown > 0) {
      if(StringFind(lines[0], "Scanning") >= 0) {
         dotColor = C'50,200,50'; // Pulse Green
         titleText = "AI AGENT :: SCANNING";
      } else if(StringFind(lines[0], "BUY") >= 0 || StringFind(lines[0], "SELL") >= 0) {
         dotColor = clrGold; // Action Warning
         titleColor = C'152,195,121'; // Green Header for Action
         titleText = "AI AGENT :: TRADING";
      }
   } else {
      titleText = "AI AGENT :: WAITING";
   }
   
   ObjectSetInteger(0, dotName, OBJPROP_COLOR, dotColor);
   ObjectSetInteger(0, titleName, OBJPROP_COLOR, titleColor);
   ObjectSetString(0, titleName, OBJPROP_TEXT, titleText);

   // --- 4. Content Content ---
   int startY = yPos + HDR_HEIGHT + (PADDING/2);

   for(int i=0; i<shown; i++) {
      string objName = "BridgeHUD_Txt_" + IntegerToString(i);
      if(ObjectFind(0, objName) < 0) {
         ObjectCreate(0, objName, OBJ_LABEL, 0, 0, 0);
         ObjectSetString(0, objName, OBJPROP_FONT, FONT_NAME);
         ObjectSetInteger(0, objName, OBJPROP_FONTSIZE, FONT_SIZE);
         ObjectSetInteger(0, objName, OBJPROP_CORNER, CORNER_LEFT_UPPER);
         ObjectSetInteger(0, objName, OBJPROP_SELECTABLE, false); // NO CRASH
         ObjectSetInteger(0, objName, OBJPROP_HIDDEN, true);
      }
      
      // Color Logic
      color rowColor = TEXT_MAIN;
      string s = StringTrim(lines[i]);
      if(StringLen(s) > 72) s = StringSubstr(s, 0, 72) + "...";
      if(StringFind(s, "Signal") >= 0) rowColor = C'152,195,121'; // Green
      if(StringFind(s, "CRITICAL") >= 0) rowColor = C'224,108,117'; // Red
      if(StringFind(s, "High") >= 0) rowColor = C'229,192,123'; // Tech Gold
      if(StringFind(s, "Scanning") >= 0) rowColor = TEXT_DIM;
      
      ObjectSetInteger(0, objName, OBJPROP_XDISTANCE, xPos + PADDING);
      ObjectSetInteger(0, objName, OBJPROP_YDISTANCE, startY + (i * ROW_HEIGHT));
      ObjectSetInteger(0, objName, OBJPROP_COLOR, rowColor);
      ObjectSetString(0, objName, OBJPROP_TEXT, s);
   }
   
   // Scan the legacy maximum once after attach, then only remove rows that were
   // visible on the preceding changed render.
   int cleanupLimit = gDashboardRowsShown < 0 ? 60 : gDashboardRowsShown;
   for(int k=shown; k<cleanupLimit; k++) {
      string delName = "BridgeHUD_Txt_" + IntegerToString(k);
      if(ObjectFind(0, delName) >= 0) ObjectDelete(0, delName);
   }
   
   gDashboardLastText = text;
   gDashboardLastChartWidth = chartWidth;
   gDashboardLastChartHeight = chartHeight;
   gDashboardRowsShown = shown;
   ChartRedraw(0);
}

bool ValidateDirectionalEntryProtection(
   string brokerSym,
   int orderType,
   double bid,
   double ask,
   double slPrice,
   double tpPrice,
   int digits,
   double &slExact,
   double &tpExact,
   string &reason
) {
   slExact=0.0;
   tpExact=0.0;
   reason="";
   if(
      !MathIsValidNumber(bid) || !MathIsValidNumber(ask) || bid<=0.0 || ask<=0.0 || ask<bid
   ){
      reason="quote_invalid";
      return(false);
   }
   if(!MathIsValidNumber(slPrice) || slPrice<=0.0){
      reason="sl_missing_or_invalid";
      return(false);
   }
   if(!MathIsValidNumber(tpPrice) || tpPrice<=0.0){
      reason="tp_missing_or_invalid";
      return(false);
   }

   slExact=NormalizeDouble(slPrice,digits);
   tpExact=NormalizeDouble(tpPrice,digits);
   if(slExact<=0.0 || tpExact<=0.0){
      reason="protection_normalized_invalid";
      return(false);
   }
   if(orderType==OP_BUY){
      if(slExact>=bid){
         reason="buy_sl_not_below_bid";
         return(false);
      }
      if(tpExact<=ask){
         reason="buy_tp_not_above_ask";
         return(false);
      }
   } else if(orderType==OP_SELL){
      if(slExact<=ask){
         reason="sell_sl_not_above_ask";
         return(false);
      }
      if(tpExact>=bid){
         reason="sell_tp_not_below_bid";
         return(false);
      }
   } else {
      reason="entry_order_type_invalid";
      return(false);
   }

   double point=MarketInfo(brokerSym,MODE_POINT);
   double stopLevelPoints=MarketInfo(brokerSym,MODE_STOPLEVEL);
   if(
      !MathIsValidNumber(point) || !MathIsValidNumber(stopLevelPoints) ||
      point<=0.0 || stopLevelPoints<0.0
   ){
      reason="broker_protection_contract_invalid";
      return(false);
   }
   if(stopLevelPoints>0.0){
      double minDistance=point*stopLevelPoints;
      double tolerance=MathMax(point*0.1,1e-12);
      if(orderType==OP_BUY && ((bid-slExact)+tolerance<minDistance || (tpExact-ask)+tolerance<minDistance)){
         reason="buy_protection_inside_stop_level";
         return(false);
      }
      if(orderType==OP_SELL && ((slExact-ask)+tolerance<minDistance || (bid-tpExact)+tolerance<minDistance)){
         reason="sell_protection_inside_stop_level";
         return(false);
      }
   }
   return(true);
}

bool QuantizeAndValidateBrokerPrice(
   double rawPrice,
   double tickSize,
   int digits,
   double &exactPrice
) {
   exactPrice=0.0;
   if(
      !MathIsValidNumber(rawPrice) || rawPrice<=0.0 ||
      !MathIsValidNumber(tickSize) || tickSize<=0.0 ||
      digits<1 || digits>8
   ) return(false);
   double units=rawPrice/tickSize;
   if(!MathIsValidNumber(units)) return(false);
   exactPrice=NormalizeDouble(MathRound(units)*tickSize,digits);
   double tolerance=MathMax(tickSize*1e-7,1e-12);
   return(
      MathIsValidNumber(exactPrice) && exactPrice>0.0 &&
      MathAbs(rawPrice-exactPrice)<=tolerance
   );
}

bool ValidateLiveBrokerContract(
   string brokerSym,
   MarketEntryEnvelope &envelope,
   string &reason
) {
   reason="";
   // Resolver output and risk-bound broker symbol must be byte-for-byte equal.
   // Normalized/suffix-equivalent symbols are deliberately not accepted.
   if(brokerSym!=envelope.broker_symbol) {
      reason="scalp_expected_broker_symbol_drift";
      return(false);
   }
   if(ToUpperSafe(AccountCurrency())!=ToUpperSafe(envelope.account_currency)) {
      reason="scalp_account_currency_drift";
      return(false);
   }

   double liveLotSize=MarketInfo(brokerSym,MODE_LOTSIZE);
   double liveMinLot=MarketInfo(brokerSym,MODE_MINLOT);
   double liveLotStep=MarketInfo(brokerSym,MODE_LOTSTEP);
   double liveMaxLot=MarketInfo(brokerSym,MODE_MAXLOT);
   double livePoint=MarketInfo(brokerSym,MODE_POINT);
   int liveDigits=(int)MarketInfo(brokerSym,MODE_DIGITS);
   double liveTickSize=SymbolInfoDouble(brokerSym,SYMBOL_TRADE_TICK_SIZE);
   double liveStopLevel=MarketInfo(brokerSym,MODE_STOPLEVEL);
   double liveFreezeLevel=MarketInfo(brokerSym,MODE_FREEZELEVEL);
   double liveMarginRequired=MarketInfo(brokerSym,MODE_MARGINREQUIRED);
   bool liveTradeAllowed=(MarketInfo(brokerSym,MODE_TRADEALLOWED)>0.5);

   // Tolerances match the decimal precision of reportSymbolSpecs(), the
   // authenticated producer of the Python-side contract snapshot.
   if(!ContractNumberMatches(liveLotSize,envelope.lot_size,0.0050001)) {
      reason="scalp_broker_contract_lot_size_drift";
      return(false);
   }
   if(!ContractNumberMatches(liveMinLot,envelope.min_lot,0.0000501)) {
      reason="scalp_broker_contract_min_lot_drift";
      return(false);
   }
   if(!ContractNumberMatches(liveLotStep,envelope.lot_step,0.0000501)) {
      reason="scalp_broker_contract_lot_step_drift";
      return(false);
   }
   if(!ContractNumberMatches(liveMaxLot,envelope.max_lot,0.0050001)) {
      reason="scalp_broker_contract_max_lot_drift";
      return(false);
   }
   if(!ContractNumberMatches(livePoint,envelope.point,0.0000000051)) {
      reason="scalp_broker_contract_point_drift";
      return(false);
   }
   if(liveDigits!=envelope.digits) {
      reason="scalp_broker_contract_digits_drift";
      return(false);
   }
   if(!ContractNumberMatches(liveTickSize,envelope.tick_size,0.0000000051)) {
      reason="scalp_broker_contract_tick_size_drift";
      return(false);
   }
   if(!ContractNumberMatches(liveStopLevel,envelope.stop_level_points,0.0500001)) {
      reason="scalp_broker_contract_stop_level_drift";
      return(false);
   }
   if(!ContractNumberMatches(liveFreezeLevel,envelope.freeze_level_points,0.0500001)) {
      reason="scalp_broker_contract_freeze_level_drift";
      return(false);
   }
   // MODE_MARGINREQUIRED is quote/conversion dependent, not immutable broker
   // geometry. Its exact value normally moves between the authenticated spec
   // report and OrderSend. Validate that MT4 still exposes a usable value here;
   // the live utilization limit is enforced below with AccountFreeMarginCheck.
   if(!MathIsValidNumber(liveMarginRequired) || liveMarginRequired<=0.0) {
      reason="scalp_broker_contract_margin_required_invalid";
      return(false);
   }
   if(!envelope.trade_allowed || !liveTradeAllowed) {
      reason="scalp_broker_contract_trade_not_allowed";
      return(false);
   }
   return(true);
}

bool IsRetryableEntryError(int errorCode) {
   // Documented transient MT4 trade-server/quote/context errors only.  Trade
   // timeout (128) is intentionally absent: its broker outcome is ambiguous
   // and retrying could create a second position.
   return(
      errorCode==4 ||   // ERR_SERVER_BUSY
      errorCode==6 ||   // ERR_NO_CONNECTION
      errorCode==8 ||   // ERR_TOO_FREQUENT_REQUESTS
      errorCode==129 || // ERR_INVALID_PRICE
      errorCode==135 || // ERR_PRICE_CHANGED
      errorCode==136 || // ERR_OFF_QUOTES
      errorCode==137 || // ERR_BROKER_BUSY
      errorCode==138 || // ERR_REQUOTE
      errorCode==146    // ERR_TRADE_CONTEXT_BUSY
   );
}

bool ValidateMarketEntryPreSend(
   string brokerSym,
   int orderType,
   double lots,
   double slPrice,
   double tpPrice,
   MarketEntryEnvelope &envelope,
   double &sendPrice,
   int &allowedSlippagePoints,
   double &slExact,
   double &tpExact,
   string &reason
) {
   sendPrice=0.0;
   allowedSlippagePoints=0;
   slExact=0.0;
   tpExact=0.0;
   reason="";
   if(!gSignalOutcomeJournalReady || gSignalOutcomeJournalBlocked) {
      reason="signal_outcome_journal_unavailable";
      return(false);
   }
   if(!ValidateLiveBrokerContract(brokerSym,envelope,reason)) return(false);
   if(!IsTradeAllowed()) {
      reason="terminal_trade_not_allowed";
      return(false);
   }
   if(IsTradeContextBusy()) {
      reason="trade_context_busy";
      return(false);
   }

   double exactLots=0.0;
   string lotReason="";
   if(!ValidateExactBrokerLots(brokerSym,lots,exactLots,lotReason)) {
      reason="entry_lots_not_exactly_executable:"+lotReason;
      return(false);
   }
   if(!ContractNumberMatches(exactLots,lots,MathMax(envelope.lot_step*1e-7,1e-9))) {
      reason="entry_lots_changed_during_pre_send";
      return(false);
   }

   ResetLastError();
   double freeMarginAfter=AccountFreeMarginCheck(brokerSym,orderType,lots);
   int marginError=GetLastError();
   if(
      !MathIsValidNumber(freeMarginAfter) || freeMarginAfter<=0.0 ||
      marginError!=0
   ) {
      reason="entry_free_margin_check_failed:"+IntegerToString(marginError);
      return(false);
   }
   double currentFreeMargin=AccountFreeMargin();
   if(!MathIsValidNumber(currentFreeMargin) || currentFreeMargin<=0.0) {
      reason="entry_current_free_margin_invalid";
      return(false);
   }
   double liveMarginConsumed=MathMax(0.0,currentFreeMargin-freeMarginAfter);
   double liveMarginBudget=currentFreeMargin*envelope.margin_utilization_cap;
   double marginTolerance=MathMax(0.01,currentFreeMargin*1e-9);
   if(
      !MathIsValidNumber(liveMarginConsumed) ||
      !MathIsValidNumber(liveMarginBudget) || liveMarginBudget<=0.0 ||
      liveMarginConsumed>liveMarginBudget+marginTolerance
   ) {
      reason="entry_live_margin_utilization_exceeded";
      return(false);
   }

   double bid=MarketInfo(brokerSym,MODE_BID);
   double ask=MarketInfo(brokerSym,MODE_ASK);
   if(
      !MathIsValidNumber(bid) || !MathIsValidNumber(ask) ||
      bid<=0.0 || ask<=0.0 || ask<bid
   ) {
      reason="entry_quote_unavailable";
      return(false);
   }

   double quoteExact=0.0;
   double entryExact=0.0;
   double worstExact=0.0;
   if(
      !QuantizeAndValidateBrokerPrice(
         envelope.entry_quote_price,envelope.tick_size,envelope.digits,quoteExact
      ) ||
      !QuantizeAndValidateBrokerPrice(
         envelope.entry_price,envelope.tick_size,envelope.digits,entryExact
      ) ||
      !QuantizeAndValidateBrokerPrice(
         envelope.worst_fill_price,envelope.tick_size,envelope.digits,worstExact
      ) ||
      !QuantizeAndValidateBrokerPrice(
         slPrice,envelope.tick_size,envelope.digits,slExact
      ) ||
      !QuantizeAndValidateBrokerPrice(
         tpPrice,envelope.tick_size,envelope.digits,tpExact
      )
   ) {
      reason="scalp_entry_price_off_tick_grid";
      return(false);
   }
   double tolerance=MathMax(envelope.point*1e-7,1e-12);
   if(MathAbs(entryExact-worstExact)>tolerance) {
      reason="scalp_entry_worst_fill_mismatch";
      return(false);
   }

   double remainingPoints=0.0;
   if(orderType==OP_BUY) {
      if(ask>worstExact+tolerance) {
         reason="buy_quote_beyond_worst_fill";
         return(false);
      }
      if(!(slExact<worstExact && worstExact<tpExact)) {
         reason="buy_bracket_not_bound_to_worst_fill";
         return(false);
      }
      sendPrice=ask;
      remainingPoints=(worstExact-ask)/envelope.point;
   } else if(orderType==OP_SELL) {
      if(bid<worstExact-tolerance) {
         reason="sell_quote_beyond_worst_fill";
         return(false);
      }
      if(!(tpExact<worstExact && worstExact<slExact)) {
         reason="sell_bracket_not_bound_to_worst_fill";
         return(false);
      }
      sendPrice=bid;
      remainingPoints=(bid-worstExact)/envelope.point;
   } else {
      reason="entry_order_type_not_market";
      return(false);
   }
   if(!MathIsValidNumber(remainingPoints) || remainingPoints<-1e-7) {
      reason="remaining_slippage_invalid";
      return(false);
   }
   allowedSlippagePoints=(int)MathFloor(MathMax(0.0,remainingPoints)+1e-7);
   if(allowedSlippagePoints>envelope.max_slippage_points) {
      allowedSlippagePoints=envelope.max_slippage_points;
   }
   if(allowedSlippagePoints<0) {
      reason="remaining_slippage_negative";
      return(false);
   }

   double sendPriceExact=0.0;
   if(!QuantizeAndValidateBrokerPrice(
      sendPrice,envelope.tick_size,envelope.digits,sendPriceExact
   )) {
      reason="current_market_price_off_tick_grid";
      return(false);
   }
   sendPrice=sendPriceExact;
   string protectionReason="";
   if(!ValidateDirectionalEntryProtection(
      brokerSym,orderType,bid,ask,slExact,tpExact,envelope.digits,
      slExact,tpExact,protectionReason
   )) {
      reason="entry_protection_invalid:"+protectionReason;
      return(false);
   }
   return(true);
}

void AppendAttestationReason(string nextReason,string &reasons) {
   if(StringLen(nextReason)<=0) return;
   if(StringLen(reasons)>0) reasons=reasons+",";
   reasons=reasons+nextReason;
}

bool SelectSubmittedTicketForAttestation(int ticket) {
   for(int attempt=0; attempt<3; attempt++) {
      ResetLastError();
      if(OrderSelect(ticket,SELECT_BY_TICKET,MODE_TRADES)) return(true);
      if(attempt<2) Sleep(40);
   }
   return(false);
}

bool AttestSubmittedMarketTicket(
   int ticket,
   string logicalSym,
   string expectedBrokerSym,
   int expectedType,
   double expectedLots,
   double expectedSl,
   double expectedTp,
   int expectedMagic,
   string expectedOwnerToken,
   MarketEntryEnvelope &envelope,
   string &actualCmd,
   string &actualLogicalSym,
   string &actualBrokerSym,
   string &actualSide,
   string &actualExecutionType,
   int &actualMagic,
   string &actualOwnerToken,
   string &actualOrderComment,
   double &actualLots,
   double &actualSl,
   double &actualTp,
   double &actualOpenPrice,
   string &reasons
) {
   actualCmd="";
   actualLogicalSym="";
   actualBrokerSym="";
   actualSide="";
   actualExecutionType="";
   actualMagic=-1;
   actualOwnerToken="";
   actualOrderComment="";
   actualLots=0.0;
   actualSl=0.0;
   actualTp=0.0;
   actualOpenPrice=0.0;
   reasons="";
   if(!SelectSubmittedTicketForAttestation(ticket)) {
      AppendAttestationReason("post_send_order_select_failed",reasons);
      return(false);
   }

   int actualType=OrderType();
   actualBrokerSym=OrderSymbol();
   actualLogicalSym=NormalizePairToken(actualBrokerSym);
   actualSide=(actualType==OP_BUY)?"BUY":((actualType==OP_SELL)?"SELL":"");
   actualCmd=actualSide;
   actualExecutionType=(actualType==OP_BUY || actualType==OP_SELL)?"market":"pending";
   actualMagic=OrderMagicNumber();
   actualOrderComment=OrderComment();
   actualLots=OrderLots();
   actualSl=OrderStopLoss();
   actualTp=OrderTakeProfit();
   actualOpenPrice=OrderOpenPrice();
   if(HasOwnerTokenPrefix(actualOrderComment,expectedOwnerToken)) {
      actualOwnerToken=expectedOwnerToken;
   }

   if(OrderTicket()!=ticket) AppendAttestationReason("ticket_mismatch",reasons);
   if(actualBrokerSym!=expectedBrokerSym) {
      AppendAttestationReason("broker_symbol_mismatch",reasons);
   }
   if(actualLogicalSym!=logicalSym) AppendAttestationReason("logical_symbol_mismatch",reasons);
   if(actualType!=expectedType) AppendAttestationReason("market_side_mismatch",reasons);
   if(actualExecutionType!="market") AppendAttestationReason("execution_type_not_market",reasons);
   if(!ContractNumberMatches(
      actualLots,expectedLots,MathMax(envelope.lot_step*1e-7,1e-9)
   )) AppendAttestationReason("lots_mismatch",reasons);

   double actualOpenExact=0.0;
   double actualSlExact=0.0;
   double actualTpExact=0.0;
   if(!QuantizeAndValidateBrokerPrice(
      actualOpenPrice,envelope.tick_size,envelope.digits,actualOpenExact
   )) AppendAttestationReason("open_price_off_tick_grid",reasons);
   if(!QuantizeAndValidateBrokerPrice(
      actualSl,envelope.tick_size,envelope.digits,actualSlExact
   )) AppendAttestationReason("sl_off_tick_grid",reasons);
   if(!QuantizeAndValidateBrokerPrice(
      actualTp,envelope.tick_size,envelope.digits,actualTpExact
   )) AppendAttestationReason("tp_off_tick_grid",reasons);

   double priceTolerance=MathMax(envelope.tick_size*1e-6,1e-12);
   if(expectedType==OP_BUY && actualOpenPrice>envelope.worst_fill_price+priceTolerance) {
      AppendAttestationReason("buy_fill_exceeds_worst_fill",reasons);
   }
   if(expectedType==OP_SELL && actualOpenPrice<envelope.worst_fill_price-priceTolerance) {
      AppendAttestationReason("sell_fill_exceeds_worst_fill",reasons);
   }
   if(MathAbs(actualSl-expectedSl)>priceTolerance) {
      AppendAttestationReason("sl_mismatch",reasons);
   }
   if(MathAbs(actualTp-expectedTp)>priceTolerance) {
      AppendAttestationReason("tp_mismatch",reasons);
   }
   if(actualMagic!=expectedMagic) AppendAttestationReason("magic_mismatch",reasons);
   if(!HasOwnerTokenPrefix(actualOrderComment,expectedOwnerToken)) {
      AppendAttestationReason("owner_comment_prefix_mismatch",reasons);
   }
   return(StringLen(reasons)<=0);
}

void Execute(
   string cmd,
   string sym,
   double lots,
   double tp_cash,
   double tp_price_in,
   double sl,
   int magic,
   string signal_id,
   string intent,
   string trace_id,
   double t_py_signal_post_start,
   double t_bridge_queued,
   double t_bridge_delivered,
   double t_ea_received,
   int t_handle_start_ms,
   string interop_mode,
   string owner_token,
   bool productionScalperEntry,
   string strategyAdmissionMode,
   MarketEntryEnvelope &marketEntryEnvelope
){
   string logicalSym = NormalizePairToken(sym);
   double t_ea_exec_start = (double)TimeCurrent();
   // Defense in depth: even a future caller that bypasses HandleCmd cannot
   // turn an unknown or pending action into the SELL arm of the ternary below.
   // This function accepts immediate market BUY/SELL trades only.
   if(cmd!="BUY" && cmd!="SELL") {
      UpdateDashboard("Trade refused " + logicalSym + "|immediate market side invalid");
      post_report("ERR immediate_market_trade_side_invalid");
      post_ack(
         signal_id, "failed", logicalSym, -1, 400,
         "immediate_market_trade_side_invalid",
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
         (double)(GetTickCount() - t_handle_start_ms), interop_mode, magic,
         owner_token,"not_attempted",cmd
      );
      return;
   }
   if(
      productionScalperEntry &&
      ToUpperSafe(StringTrim(strategyAdmissionMode)) != "SIGNED_VALIDATION"
   ) {
      UpdateDashboard("Trade refused " + logicalSym + "|signed validation required");
      post_report("ERR strategy_signed_validation_required");
      post_ack(
         signal_id, "failed", logicalSym, -1, 403,
         "strategy_signed_validation_required",
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
         (double)(GetTickCount() - t_handle_start_ms), interop_mode, magic,
         owner_token,"not_attempted",cmd
      );
      return;
   }
   string brokerSym="";
   string mappingReason="";
   string mappingKind="";
   int mappingCandidateCount=0;
   bool mappingAmbiguous=false;
   bool symbolResolved=ResolveBrokerSymbolStatus(
      sym,brokerSym,mappingReason,mappingKind,mappingCandidateCount,mappingAmbiguous
   );
   UpdateDashboard(
      "Executing immediate market " + cmd + " trade " + logicalSym +
      "...|Awaiting broker confirmation"
   );
   if(!symbolResolved || StringLen(brokerSym)<=0 || !SymbolSelect(brokerSym,true)){
      string symbolFailure="symbol_select_failed:"+mappingReason;
      UpdateDashboard("Trade failed " + cmd + " " + logicalSym + "|"+symbolFailure);
      post_report("ERR symbol " + sym + " reason=" + mappingReason);
      post_ack(
         signal_id, "failed", logicalSym, -1, 410, symbolFailure,
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
         (double)(GetTickCount() - t_handle_start_ms), interop_mode, magic,
         owner_token,"not_attempted",cmd
      );
      return;
   }
   if(brokerSym!=marketEntryEnvelope.broker_symbol) {
      UpdateDashboard("Trade failed " + cmd + " " + logicalSym + "|broker_symbol_drift");
      post_report(
         "ERR broker_symbol_drift resolved="+brokerSym+
         " expected="+marketEntryEnvelope.broker_symbol
      );
      post_ack(
         signal_id,"failed",logicalSym,-1,409,"scalp_expected_broker_symbol_drift",
         trace_id,t_py_signal_post_start,t_bridge_queued,t_bridge_delivered,
         t_ea_received,t_ea_exec_start,(double)TimeCurrent(),
         (double)(GetTickCount()-t_handle_start_ms),interop_mode,magic,
         owner_token,"not_attempted",cmd
      );
      return;
   }

   // MT4 names both market and pending mutations OrderSend.  This branch is
   // instant market execution only: pending OP_*LIMIT/OP_*STOP types are never
   // constructed or reachable.
   int type=(cmd=="BUY")?OP_BUY:OP_SELL;
   int symDigits = (int)MarketInfo(brokerSym, MODE_DIGITS);
   if(symDigits < 0) symDigits = Digits;

   // The command amount has already passed portfolio and risk approval. The EA
   // may reject it against the live broker contract, but it must never increase
   // or otherwise rewrite that approved economic size.
   double lots2=0.0;
   string lotReason="";
   if(!ValidateExactBrokerLots(brokerSym,lots,lots2,lotReason)){
      string lotFailure="entry_lots_not_exactly_executable:"+lotReason;
      UpdateDashboard("Trade failed " + cmd + " " + logicalSym + "|" + lotFailure);
      post_report("ERR " + lotFailure + " sym=" + logicalSym);
      post_ack(
         signal_id, "failed", logicalSym, -1, 409, lotFailure,
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
         (double)(GetTickCount() - t_handle_start_ms), interop_mode, magic,
         owner_token,"not_attempted",cmd
      );
      return;
   }

   int ticket = -1;
   int err = 0;
   int usedSlip = 0;
   int retriesUsed = 0;
   string terminalFailure="";
   bool mutationAttempted=false;
   double px=0.0;
   double slNorm=0.0;
   double tp=0.0;
   for(int attempt=0; attempt<ENTRY_SEND_ATTEMPTS; attempt++){
      if(attempt > 0){
         Sleep(80);
      }
      RefreshRates();
      string preSendReason="";
      // AGENT HOT PATH: every live entry, not only MTVCLC, rechecks the exact
      // broker contract, quote bound, lot geometry, protection, margin, and
      // remaining command-owned slippage immediately before OrderSend.
      if(!ValidateMarketEntryPreSend(
         brokerSym,type,lots2,sl,tp_price_in,marketEntryEnvelope,
         px,usedSlip,slNorm,tp,preSendReason
      )) {
         err=412;
         terminalFailure="entry_pre_send_refused:"+preSendReason;
         bool transientPreSend=(
            preSendReason=="trade_context_busy" ||
            preSendReason=="entry_quote_unavailable"
         );
         if(transientPreSend && attempt+1<ENTRY_SEND_ATTEMPTS) continue;
         break;
      }

      post_report(
         "EXEC cmd="+cmd+
         " sym="+logicalSym+
         " broker="+brokerSym+
         " intent="+intent+
         " px="+DoubleToString(px,symDigits)+
         " lots="+DoubleToString(lots2,2)+
         " max_slip="+IntegerToString(usedSlip)
      );
      mutationAttempted=true;
      ResetLastError();
      if(productionScalperEntry) {
         // AGENT HOT PATH: this is the last statement that can refuse the
         // mutation. It runs on every retry immediately before OrderSend.
         string finalDeadlineReason="";
         if(!ScalpEntryDeadlineActive(marketEntryEnvelope,finalDeadlineReason)) {
            mutationAttempted=false;
            err=408;
            terminalFailure="entry_pre_send_refused:"+finalDeadlineReason;
            break;
         }
      }
      ticket = OrderSend(brokerSym, type, lots2, px, usedSlip, slNorm, tp, owner_token, magic, 0, (type==OP_BUY)?clrGreen:clrRed);
      if(ticket >= 0){
         retriesUsed = attempt;
         break;
      }
      err = GetLastError();
      retriesUsed = attempt;
      if(err==128) {
         terminalFailure="order_send_trade_timeout_outcome_unknown";
         break;
      }
      if(!IsRetryableEntryError(err)) {
         terminalFailure="order_send_non_retryable_error";
         break;
      }
      post_report(
         "WARN trade_retry sym=" + logicalSym +
         " broker=" + brokerSym +
         " attempt=" + IntegerToString(attempt + 1) +
         " err=" + IntegerToString(err) +
         " slip=" + IntegerToString(usedSlip)
      );
   }
   if(ticket<0){ 
      UpdateDashboard("Trade failed " + cmd + " " + logicalSym + "|err=" + IntegerToString(err));
      if(StringLen(terminalFailure)<=0) {
         terminalFailure=mutationAttempted
            ? "order_send_outcome_unknown"
            : "entry_pre_send_refused";
      }
      post_report("ERR trade "+IntegerToString(err)+" "+terminalFailure);
      Print("Immediate market trade error (OrderSend): ", err);
      string failureStatus=mutationAttempted ? "reconcile_required" : "failed";
      string mutationState=mutationAttempted ? "attempted" : "not_attempted";
      post_ack(
         signal_id, failureStatus, logicalSym, -1, err, terminalFailure,
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
         (double)(GetTickCount() - t_handle_start_ms), interop_mode, magic,
         owner_token,mutationState,cmd
      );
      return; 
   }

   string actualCmd="";
   string actualLogicalSym="";
   string actualBrokerSym="";
   string actualSide="";
   string actualExecutionType="";
   int actualMagic=-1;
   string actualOwnerToken="";
   string actualOrderComment="";
   double actualLots=0.0;
   double actualSl=0.0;
   double actualTp=0.0;
   double actualOpenPrice=0.0;
   string attestationReasons="";
   bool attested=false;
   attested=AttestSubmittedMarketTicket(
      ticket,logicalSym,marketEntryEnvelope.broker_symbol,type,lots2,slNorm,tp,
      magic,owner_token,marketEntryEnvelope,
      actualCmd,actualLogicalSym,actualBrokerSym,actualSide,
      actualExecutionType,actualMagic,actualOwnerToken,actualOrderComment,
      actualLots,actualSl,actualTp,actualOpenPrice,attestationReasons
   );

   if(!gCycleActive){
      gCycleStartEq = AccountEquity();
      // Basket-TP fraction comes from the bridge's /v2/handshake (authoritative
      // source: Python settings.basket_tp_pct). Fall back to a hardcoded value
      // only when the bridge has not yet supplied one, so an offline bridge
      // doesn't silently change cycle behavior.
      double pct = (gBasketTpPctFromBridge > 0.0) ? gBasketTpPctFromBridge : EA_FALLBACK_BASKET_TP_PCT;
      gCycleTargetCash = gCycleStartEq * pct;
      gCycleActive = true;
      post_report("CYCLE_START eq="+DoubleToString(gCycleStartEq,2)+" target="+DoubleToString(gCycleTargetCash,2)+" pct="+DoubleToString(pct,6));
   }
   string ackStatus=attested ? "acked" : "reconcile_required";
   string ackMutationState=attested ? "confirmed" : "attempted";
   string ackMessage=attested
      ? "market_order_attested"
      : "post_send_attestation_failed:"+attestationReasons;
   UpdateDashboard(
      attested
         ? cmd + " opened " + logicalSym + "|ticket=" + IntegerToString(ticket) + " lots=" + DoubleToString(lots2, 2)
         : "Execution uncertainty " + cmd + " " + logicalSym + "|ticket=" + IntegerToString(ticket)
   );
   post_report(
      (attested ? "OK " : "RECONCILE ")+cmd+" "+logicalSym+
      " broker="+brokerSym+
      " ticket="+IntegerToString(ticket)+
      " lots="+DoubleToString(lots2,2)+
      " retries="+IntegerToString(retriesUsed)+
      " slip="+IntegerToString(usedSlip)
   );
   post_ack(
      signal_id,ackStatus,logicalSym,ticket,attested ? 0 : 409,ackMessage,
      trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
      t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
      (double)(GetTickCount() - t_handle_start_ms), interop_mode, magic,
      owner_token,ackMutationState,actualCmd,actualLogicalSym,actualBrokerSym,
      actualSide,actualExecutionType,-1,actualMagic,actualOwnerToken,
      actualOrderComment,actualLots,actualSl,actualTp,actualOpenPrice,
      attested ? BROKER_ORDER_ACTUALS_SCHEMA : "",
      actualLots,0
   );
}

void manageCycle(){
   if(!gCycleActive) return;
   double eq = AccountEquity();
   if(eq >= gCycleStartEq + gCycleTargetCash){
      post_report("CYCLE_TARGET_HIT eq="+DoubleToString(eq,2)+" profit="+DoubleToString(eq-gCycleStartEq,2));
      if(AllowCycleCloseAll){
         int cycleErr = 0;
         bool cycleCloseOk = CloseAll(Magic, LEGACY_ORDER_COMMENT, cycleErr);
         if(!cycleCloseOk){
            post_report("ERR cycle_close_all " + IntegerToString(cycleErr));
         }
      } else {
         post_report("CYCLE_TARGET_HIT_NO_AUTO_CLOSE");
      }
      resetCycle();
   }
}

void resetCycle(){ gCycleActive=false; gCycleStartEq=0; gCycleTargetCash=0; }

bool CloseAll(int target_magic, string owner_comment, int &lastErr){
   bool ok = true;
   lastErr = 0;
   for(int i=OrdersTotal()-1;i>=0;i--){
      if(!OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(OrderMagicNumber()!=target_magic) continue;
      if(OrderComment()!=owner_comment) continue;
      int ty=OrderType();
      if(ty!=OP_BUY && ty!=OP_SELL) continue;
      string osym = OrderSymbol();
      double ask = MarketInfo(osym, MODE_ASK);
      double bid = MarketInfo(osym, MODE_BID);
      int dg = (int)MarketInfo(osym, MODE_DIGITS);
      if(dg < 0) dg = Digits;
      double px=(ty==OP_BUY)?bid:ask;
      px = NormalizeDouble(px, dg);
      if(px <= 0){
         ok = false;
         lastErr = 411;
         post_report("ERR close_quote " + osym);
         continue;
      }
      if(!OrderClose(OrderTicket(), OrderLots(), px, SlipPts)){
         ok = false;
         lastErr = GetLastError();
         post_report("ERR close "+IntegerToString(lastErr));
      }
   }
   return ok;
}

bool CloseSymbol(string sym, int target_magic, int &lastErr) {
   bool ok = true;
   bool closedAny = false;
   lastErr = 0;
   for(int i=OrdersTotal()-1; i>=0; i--) {
      if(!OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(OrderMagicNumber() != target_magic) continue;
      if(OrderComment() != LEGACY_ORDER_COMMENT) continue;
      // Case-insensitive match or exact
      if(!SymbolsMatch(OrderSymbol(), sym)) continue;
      
      int ty = OrderType();
      if(ty == OP_BUY || ty == OP_SELL) {
         string osym = OrderSymbol();
         double ask = MarketInfo(osym, MODE_ASK);
         double bid = MarketInfo(osym, MODE_BID);
         int dg = (int)MarketInfo(osym, MODE_DIGITS);
         if(dg < 0) dg = Digits;
         double px = (ty == OP_BUY) ? bid : ask;
         px = NormalizeDouble(px, dg);
         if(px <= 0){
            ok = false;
            lastErr = 411;
            post_report("ERR close_quote " + osym);
            continue;
         }
         if(!OrderClose(OrderTicket(), OrderLots(), px, SlipPts)) {
            ok = false;
            lastErr = GetLastError();
            post_report("ERR close " + sym + " " + IntegerToString(lastErr));
         } else {
            closedAny = true;
            post_report("OK CLOSE " + sym);
         }
      }
   }
   if(!closedAny){
      ok = false;
      if(lastErr == 0) lastErr = 404;
      post_report("ERR close " + sym + " not_found");
   }
   return ok;
}

// AGENT HANDSHAKE: New-owner management is ticket-addressed. Selection must
// prove the same open immediate market trade matches all three immutable command
// identities before any OrderClose/OrderModify call is reachable.
bool SelectOwnedMarketOrder(
   int target_ticket,
   string sym,
   int target_magic,
   string owner_token,
   int &lastErr,
   string &reason
) {
   lastErr=0;
   reason="";
   if(target_ticket<=0 || target_magic<=0 || !IsValidOwnerToken(owner_token)){
      lastErr=400;
      reason="ownership_contract_invalid";
      return(false);
   }
   if(!OrderSelect(target_ticket,SELECT_BY_TICKET,MODE_TRADES)){
      lastErr=404;
      reason="target_ticket_not_open";
      return(false);
   }
   int ty=OrderType();
   if(OrderCloseTime()!=0 || (ty!=OP_BUY && ty!=OP_SELL)){
      lastErr=404;
      reason="target_ticket_not_open_market_order";
      return(false);
   }
   if(
      !SymbolsMatch(OrderSymbol(),sym) ||
      OrderMagicNumber()!=target_magic ||
      !HasOwnerTokenPrefix(OrderComment(),owner_token)
   ){
      lastErr=403;
      reason="target_ticket_identity_mismatch";
      return(false);
   }
   return(true);
}

bool CloseOwnedTicket(
   int target_ticket,
   string sym,
   int target_magic,
   string owner_token,
   StrictManagementActuals &actual,
   bool &mutationAttempted,
   bool &mutationConfirmed,
   int &lastErr,
   string &reason
) {
   ResetStrictManagementActuals("CLOSE",target_ticket,actual);
   mutationAttempted=false;
   mutationConfirmed=false;
   if(!SelectOwnedMarketOrder(
      target_ticket,sym,target_magic,owner_token,lastErr,reason
   )) return(false);
   CaptureSelectedManagementActuals("CLOSE",target_ticket,owner_token,actual);

   string osym=OrderSymbol();
   int ty=OrderType();
   double expectedLots=OrderLots();
   double expectedOpen=OrderOpenPrice();
   double expectedSl=OrderStopLoss();
   double expectedTp=OrderTakeProfit();
   double ask=MarketInfo(osym,MODE_ASK);
   double bid=MarketInfo(osym,MODE_BID);
   int dg=(int)MarketInfo(osym,MODE_DIGITS);
   if(dg<0) dg=Digits;
   double px=NormalizeDouble((ty==OP_BUY)?bid:ask,dg);
   if(px<=0.0){
      lastErr=411;
      reason="close_quote_unavailable";
      return(false);
   }
   if(!IsTradeAllowed() || IsTradeContextBusy()) {
      lastErr=410;
      reason="close_trade_context_unavailable";
      return(false);
   }
   mutationAttempted=true;
   ResetLastError();
   if(!OrderClose(target_ticket,expectedLots,px,SlipPts)){
      lastErr=GetLastError();
      reason="close_ticket_outcome_unknown";
      return(false);
   }
   if(!OrderSelect(target_ticket,SELECT_BY_TICKET,MODE_HISTORY)) {
      lastErr=409;
      reason="close_post_select_failed";
      return(false);
   }
   CaptureSelectedManagementActuals("CLOSE",target_ticket,owner_token,actual);
   double lotTolerance=LotStepTolerance(MarketInfo(osym,MODE_LOTSTEP));
   double priceTolerance=MathMax(MarketInfo(osym,MODE_POINT)*1e-6,1e-12);
   if(
      OrderCloseTime()<=0 || OrderType()!=ty || OrderSymbol()!=osym ||
      OrderMagicNumber()!=target_magic ||
      !HasOwnerTokenPrefix(OrderComment(),owner_token) ||
      MathAbs(OrderLots()-expectedLots)>lotTolerance ||
      MathAbs(OrderOpenPrice()-expectedOpen)>priceTolerance ||
      MathAbs(OrderStopLoss()-expectedSl)>priceTolerance ||
      MathAbs(OrderTakeProfit()-expectedTp)>priceTolerance ||
      actual.remaining_lots>lotTolerance || actual.close_time<=0
   ) {
      lastErr=409;
      reason="close_post_attestation_mismatch";
      return(false);
   }
   actual.lots=expectedLots;
   mutationConfirmed=true;
   post_report("OK CLOSE ticket="+IntegerToString(target_ticket));
   return(true);
}

bool SelectUniqueOwnedPartialRemainder(
   string expectedBrokerSymbol,
   int expectedType,
   int expectedMagic,
   string expectedOwnerToken,
   double expectedLots,
   double expectedOpenPrice,
   double expectedSl,
   double expectedTp,
   string &reason
) {
   reason="";
   double step=MarketInfo(expectedBrokerSymbol,MODE_LOTSTEP);
   double lotTolerance=LotStepTolerance(step);
   double point=MarketInfo(expectedBrokerSymbol,MODE_POINT);
   double priceTolerance=MathMax(point*1e-6,1e-12);
   int candidateTicket=-1;
   int candidateCount=0;
   for(int i=OrdersTotal()-1; i>=0; i--) {
      if(!OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(OrderCloseTime()!=0 || OrderType()!=expectedType) continue;
      if(OrderSymbol()!=expectedBrokerSymbol) continue;
      if(OrderMagicNumber()!=expectedMagic) continue;
      if(!HasOwnerTokenPrefix(OrderComment(),expectedOwnerToken)) continue;
      if(MathAbs(OrderLots()-expectedLots)>lotTolerance) continue;
      if(MathAbs(OrderOpenPrice()-expectedOpenPrice)>priceTolerance) continue;
      if(MathAbs(OrderStopLoss()-expectedSl)>priceTolerance) continue;
      if(MathAbs(OrderTakeProfit()-expectedTp)>priceTolerance) continue;
      candidateTicket=OrderTicket();
      candidateCount++;
   }
   if(candidateCount!=1 || candidateTicket<=0) {
      reason=(candidateCount<=0)
         ? "partial_close_remainder_missing"
         : "partial_close_remainder_ambiguous";
      return(false);
   }
   if(!OrderSelect(candidateTicket,SELECT_BY_TICKET,MODE_TRADES)) {
      reason="partial_close_remainder_select_failed";
      return(false);
   }
   // A broker may retain the original ticket or issue one replacement ticket.
   // Either way the immutable owner prefix and complete economic remainder
   // must be unique before the partial close is confirmed.
   return(true);
}

bool CloseOwnedTicketPartial(
   int target_ticket,
   string sym,
   int target_magic,
   string owner_token,
   double closeLots,
   StrictManagementActuals &actual,
   bool &mutationAttempted,
   bool &mutationConfirmed,
   int &lastErr,
   string &reason
) {
   ResetStrictManagementActuals("CLOSE_PARTIAL",target_ticket,actual);
   mutationAttempted=false;
   mutationConfirmed=false;
   if(!SelectOwnedMarketOrder(
      target_ticket,sym,target_magic,owner_token,lastErr,reason
   )) return(false);
   CaptureSelectedManagementActuals(
      "CLOSE_PARTIAL",target_ticket,owner_token,actual
   );
   if(!MathIsValidNumber(closeLots) || closeLots<=0.0){
      lastErr=400;
      reason="invalid_close_lots";
      return(false);
   }

   string osym=OrderSymbol();
   int ty=OrderType();
   double orderLots=OrderLots();
   double expectedOpen=OrderOpenPrice();
   double expectedSl=OrderStopLoss();
   double expectedTp=OrderTakeProfit();
   double step=MarketInfo(osym,MODE_LOTSTEP);
   double tolerance=LotStepTolerance(step);
   double priceTolerance=MathMax(MarketInfo(osym,MODE_POINT)*1e-6,1e-12);
   double exactCloseLots=0.0;
   string lotReason="";
   if(!ValidateExactBrokerLots(osym,closeLots,exactCloseLots,lotReason)){
      lastErr=409;
      reason="close_lots_not_exactly_executable:"+lotReason;
      return(false);
   }
   if(exactCloseLots>orderLots+tolerance){
      lastErr=409;
      reason="close_lots_exceed_target_ticket";
      return(false);
   }
   double remainder=orderLots-exactCloseLots;
   if(remainder>tolerance){
      double exactRemainder=0.0;
      string remainderReason="";
      if(!ValidateExactBrokerLots(osym,remainder,exactRemainder,remainderReason)){
         lastErr=409;
         reason="ticket_remainder_not_executable:"+remainderReason;
         return(false);
      }
   }

   double ask=MarketInfo(osym,MODE_ASK);
   double bid=MarketInfo(osym,MODE_BID);
   int dg=(int)MarketInfo(osym,MODE_DIGITS);
   if(dg<0) dg=Digits;
   double px=NormalizeDouble((ty==OP_BUY)?bid:ask,dg);
   if(px<=0.0){
      lastErr=411;
      reason="close_partial_quote_unavailable";
      return(false);
   }
   if(!IsTradeAllowed() || IsTradeContextBusy()) {
      lastErr=410;
      reason="close_partial_trade_context_unavailable";
      return(false);
   }
   mutationAttempted=true;
   ResetLastError();
   if(!OrderClose(target_ticket,exactCloseLots,px,SlipPts)){
      lastErr=GetLastError();
      reason="close_partial_ticket_outcome_unknown";
      return(false);
   }
   if(remainder>tolerance) {
      if(!SelectUniqueOwnedPartialRemainder(
         osym,ty,target_magic,owner_token,remainder,
         expectedOpen,expectedSl,expectedTp,reason
      )) {
         lastErr=409;
         return(false);
      }
      CaptureSelectedManagementActuals(
         "CLOSE_PARTIAL",target_ticket,owner_token,actual
      );
   } else {
      if(!OrderSelect(target_ticket,SELECT_BY_TICKET,MODE_HISTORY)) {
         lastErr=409;
         reason="partial_close_full_post_select_failed";
         return(false);
      }
      CaptureSelectedManagementActuals(
         "CLOSE_PARTIAL",target_ticket,owner_token,actual
      );
      if(OrderCloseTime()<=0) {
         lastErr=409;
         reason="partial_close_full_not_closed";
         return(false);
      }
   }
   if(remainder>tolerance) {
      if(
         actual.close_time!=0 ||
         MathAbs(actual.lots-remainder)>tolerance ||
         MathAbs(actual.remaining_lots-remainder)>tolerance
      ) {
         lastErr=409;
         reason="partial_close_remainder_attestation_mismatch";
         return(false);
      }
   } else if(
      actual.close_time<=0 || actual.remaining_lots>tolerance ||
      MathAbs(actual.lots-orderLots)>tolerance
   ) {
      lastErr=409;
      reason="partial_close_full_attestation_mismatch";
      return(false);
   }
   if(
      actual.broker_symbol!=osym || actual.side!=((ty==OP_BUY)?"BUY":"SELL") ||
      actual.magic!=target_magic ||
      !HasOwnerTokenPrefix(actual.order_comment,owner_token) ||
      MathAbs(actual.open_price-expectedOpen)>priceTolerance ||
      MathAbs(actual.sl_price-expectedSl)>priceTolerance ||
      MathAbs(actual.tp_price-expectedTp)>priceTolerance
   ) {
      lastErr=409;
      reason="partial_close_post_attestation_mismatch";
      return(false);
   }
   // The ACK reports the broker-confirmed mutation quantity, not remaining
   // position size; remainder evidence was independently proved above.
   actual.lots=exactCloseLots;
   mutationConfirmed=true;
   return(true);
}

bool ModifyOwnedTicketStop(
   int target_ticket,
   string sym,
   int target_magic,
   string owner_token,
   double sl_price,
   StrictManagementActuals &actual,
   bool &mutationAttempted,
   bool &mutationConfirmed,
   int &lastErr,
   string &reason
) {
   ResetStrictManagementActuals("MODIFY_SL",target_ticket,actual);
   mutationAttempted=false;
   mutationConfirmed=false;
   if(!SelectOwnedMarketOrder(
      target_ticket,sym,target_magic,owner_token,lastErr,reason
   )) return(false);
   CaptureSelectedManagementActuals("MODIFY_SL",target_ticket,owner_token,actual);

   int ty=OrderType();
   string osym=OrderSymbol();
   double expectedLots=OrderLots();
   double expectedOpen=OrderOpenPrice();
   double expectedTp=OrderTakeProfit();
   int dg=(int)MarketInfo(osym,MODE_DIGITS);
   if(dg<0) dg=Digits;
   double slNorm=NormalizeDouble(sl_price,dg);
   double currentSl=NormalizeDouble(OrderStopLoss(),dg);
   if(slNorm<=0.0){
      lastErr=400;
      reason="invalid_sl";
      return(false);
   }
   if(!IsStrictlyTighterStop(ty,currentSl,slNorm)){
      lastErr=409;
      reason="modify_sl_non_tightening";
      return(false);
   }
   if(!IsTradeAllowed() || IsTradeContextBusy()) {
      lastErr=410;
      reason="modify_sl_trade_context_unavailable";
      return(false);
   }
   // Preserve the broker-native take profit when tightening the stop.
   mutationAttempted=true;
   ResetLastError();
   if(!OrderModify(target_ticket,expectedOpen,slNorm,expectedTp,0,clrNONE)){
      lastErr=GetLastError();
      reason="modify_sl_ticket_outcome_unknown";
      return(false);
   }
   if(!OrderSelect(target_ticket,SELECT_BY_TICKET,MODE_TRADES)) {
      lastErr=409;
      reason="modify_sl_post_select_failed";
      return(false);
   }
   CaptureSelectedManagementActuals("MODIFY_SL",target_ticket,owner_token,actual);
   double lotTolerance=LotStepTolerance(MarketInfo(osym,MODE_LOTSTEP));
   double priceTolerance=MathMax(MarketInfo(osym,MODE_POINT)*1e-6,1e-12);
   if(
      OrderCloseTime()!=0 || OrderType()!=ty || OrderSymbol()!=osym ||
      OrderMagicNumber()!=target_magic ||
      !HasOwnerTokenPrefix(OrderComment(),owner_token) ||
      MathAbs(OrderLots()-expectedLots)>lotTolerance ||
      MathAbs(actual.remaining_lots-expectedLots)>lotTolerance ||
      actual.close_time!=0 ||
      MathAbs(OrderOpenPrice()-expectedOpen)>priceTolerance ||
      MathAbs(OrderStopLoss()-slNorm)>priceTolerance ||
      MathAbs(OrderTakeProfit()-expectedTp)>priceTolerance
   ) {
      lastErr=409;
      reason="modify_sl_post_attestation_mismatch";
      return(false);
   }
   mutationConfirmed=true;
   return(true);
}

// Prove that the complete approved partial-close amount can be distributed
// across the current tickets without rounding a chunk upward or leaving an
// unexecutable broker remainder. This pass must succeed before any ticket is
// touched, because MT4 cannot roll back a partially executed multi-ticket plan.
bool ValidatePartialClosePlan(
   string sym,
   int target_magic,
   double approvedCloseLots,
   int &lastErr,
   string &reason
) {
   lastErr=0;
   reason="";
   if(!MathIsValidNumber(approvedCloseLots) || approvedCloseLots<=0.0){
      lastErr=400;
      reason="non_positive_or_nonfinite";
      return(false);
   }

   bool found=false;
   double remaining=approvedCloseLots;
   double planTolerance=0.0;
   for(int i=OrdersTotal()-1; i>=0; i--){
      if(!OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(OrderMagicNumber()!=target_magic) continue;
      if(OrderComment()!=LEGACY_ORDER_COMMENT) continue;
      if(!SymbolsMatch(OrderSymbol(),sym)) continue;
      int ty=OrderType();
      if(ty!=OP_BUY && ty!=OP_SELL) continue;
      found=true;
      if(remaining<=0.0) break;

      string osym=OrderSymbol();
      double orderLots=OrderLots();
      if(!MathIsValidNumber(orderLots) || orderLots<=0.0){
         lastErr=409;
         reason="open_ticket_lots_invalid";
         return(false);
      }
      double step=MarketInfo(osym,MODE_LOTSTEP);
      double tolerance=LotStepTolerance(step);
      if(tolerance>planTolerance) planTolerance=tolerance;

      double requestedChunk=MathMin(orderLots,remaining);
      double exactChunk=0.0;
      string lotReason="";
      if(!ValidateExactBrokerLots(osym,requestedChunk,exactChunk,lotReason)){
         lastErr=409;
         reason="close_chunk_not_exactly_executable:"+lotReason;
         return(false);
      }
      if(exactChunk>requestedChunk || exactChunk>remaining){
         lastErr=409;
         reason="close_chunk_exceeds_approved_amount";
         return(false);
      }

      // A valid partial close must also leave either zero lots or another
      // exactly executable amount on the broker ticket.
      double ticketRemainder=orderLots-exactChunk;
      if(ticketRemainder < -tolerance){
         lastErr=409;
         reason="close_chunk_exceeds_ticket";
         return(false);
      }
      if(ticketRemainder>tolerance){
         double exactRemainder=0.0;
         string remainderReason="";
         if(!ValidateExactBrokerLots(osym,ticketRemainder,exactRemainder,remainderReason)){
            lastErr=409;
            reason="ticket_remainder_not_executable:"+remainderReason;
            return(false);
         }
      }

      remaining-=exactChunk;
      if(MathAbs(remaining)<=tolerance) remaining=0.0;
   }

   if(!found){
      lastErr=404;
      reason="position_not_found";
      return(false);
   }
   if(remaining>planTolerance){
      lastErr=409;
      reason="approved_close_lots_not_fully_executable";
      return(false);
   }
   return(true);
}

bool CloseSymbolPartial(string sym, int target_magic, double closeLots, int &lastErr) {
   string planReason="";
   if(!ValidatePartialClosePlan(sym,target_magic,closeLots,lastErr,planReason)){
      post_report("ERR close_partial_preflight " + sym + " " + planReason);
      return(false);
   }

   bool closedAny=false;
   double closedTotal=0.0;
   double remaining=closeLots;
   double approvedTolerance=0.0;
   for(int i=OrdersTotal()-1; i>=0; i--) {
      if(!OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(OrderMagicNumber() != target_magic) continue;
      if(OrderComment() != LEGACY_ORDER_COMMENT) continue;
      if(!SymbolsMatch(OrderSymbol(), sym)) continue;
      int ty = OrderType();
      if(ty != OP_BUY && ty != OP_SELL) continue;
      if(remaining <= 0) break;

      string osym = OrderSymbol();
      double orderLots = OrderLots();
      double step = MarketInfo(osym, MODE_LOTSTEP);
      double tolerance=LotStepTolerance(step);
      if(tolerance>approvedTolerance) approvedTolerance=tolerance;
      double requestedChunk=MathMin(orderLots,remaining);
      double exactChunk=0.0;
      string lotReason="";
      if(!ValidateExactBrokerLots(osym,requestedChunk,exactChunk,lotReason)){
         lastErr=409;
         post_report("ERR close_partial_lots " + osym + " " + lotReason);
         return(false);
      }
      if(
         exactChunk>remaining ||
         closedTotal+exactChunk>closeLots
      ){
         lastErr=409;
         post_report("ERR close_partial_exceeds_approved " + sym);
         return(false);
      }

      double ask = MarketInfo(osym, MODE_ASK);
      double bid = MarketInfo(osym, MODE_BID);
      int dg = (int)MarketInfo(osym, MODE_DIGITS);
      if(dg < 0) dg = Digits;
      double px = (ty == OP_BUY) ? bid : ask;
      px = NormalizeDouble(px, dg);
      if(px <= 0){
         lastErr = 411;
         post_report("ERR close_partial_quote " + osym);
         return(false);
      }

      if(!OrderClose(OrderTicket(), exactChunk, px, SlipPts)) {
         lastErr = GetLastError();
         post_report("ERR close_partial " + osym + " " + IntegerToString(lastErr));
         return(false);
      } else {
         closedAny = true;
         closedTotal += exactChunk;
         if(closedTotal>closeLots){
            // Defensive assertion: this branch must be unreachable because the
            // same bound is checked before OrderClose.
            lastErr=409;
            post_report("ERR close_partial_postcondition " + sym);
            return(false);
         }
         remaining=closeLots-closedTotal;
         if(MathAbs(remaining)<=tolerance) remaining=0.0;
      }
   }

   if(!closedAny){
      if(lastErr == 0) lastErr = 404;
      return(false);
   }
   if(remaining>approvedTolerance){
      if(lastErr == 0) lastErr = 409;
      post_report("ERR close_partial_incomplete " + sym);
      return(false);
   }
   return(true);
}

bool IsStrictlyTighterStop(int order_type, double current_sl, double proposed_sl) {
   if(proposed_sl <= 0.0) return false;
   if(current_sl <= 0.0) return true;
   if(order_type == OP_BUY) return proposed_sl > current_sl;
   if(order_type == OP_SELL) return proposed_sl < current_sl;
   return false;
}

bool ModifySymbolStop(string sym, int target_magic, double sl_price, int &lastErr) {
   bool ok = true;
   bool modifiedAny = false;
   lastErr = 0;
   for(int i=OrdersTotal()-1; i>=0; i--) {
      if(!OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(OrderMagicNumber() != target_magic) continue;
      if(OrderComment() != LEGACY_ORDER_COMMENT) continue;
      if(!SymbolsMatch(OrderSymbol(), sym)) continue;
      int ty = OrderType();
      if(ty != OP_BUY && ty != OP_SELL) continue;

      int dg = (int)MarketInfo(OrderSymbol(), MODE_DIGITS);
      if(dg < 0) dg = Digits;
      double slNorm = NormalizeDouble(sl_price, dg);
      if(slNorm <= 0){
         ok = false;
         if(lastErr == 0) lastErr = 400;
         continue;
      }

      double currentSl = NormalizeDouble(OrderStopLoss(), dg);
      if(!IsStrictlyTighterStop(ty, currentSl, slNorm)){
         ok = false;
         if(lastErr == 0) lastErr = 409;
         post_report(
            "ERR modify_sl non_tightening symbol=" + OrderSymbol() +
            " ticket=" + IntegerToString(OrderTicket()) +
            " current=" + DoubleToString(currentSl, dg) +
            " proposed=" + DoubleToString(slNorm, dg)
         );
         continue;
      }

      double tp = OrderTakeProfit();
      if(!OrderModify(OrderTicket(), OrderOpenPrice(), slNorm, tp, 0, clrNONE)){
         ok = false;
         lastErr = GetLastError();
      } else {
         modifiedAny = true;
      }
   }
   if(!modifiedAny){
      ok = false;
      if(lastErr == 0) lastErr = 404;
   }
   return ok;
}

string ToUpperSafe(string str) {
   int len = StringLen(str);
   for(int i=0; i<len; i++) {
      int ch = StringGetCharacter(str, i);
      if(ch >= 97 && ch <= 122) {
         StringSetCharacter(str, i, (ushort)(ch - 32));
      }
   }
   return str;
}

// AGENT HANDSHAKE: Emits a structured JSON snapshot of all EA-managed open
// positions to the bridge's /v2/reports endpoint. The bridge's
// /v2/positions/reconcile endpoint uses the most-recent such snapshot to
// compute the diff against DB-known positions. This is the only scheduled
// positions producer; legacy text must never overwrite its ownership fields.
void EmitPositionsSnapshot() {
   if(!gBridgeHandshakeCompatible) return;
   string marketSourceFields=CurrentBrokerMarketSourceJsonFields();
   if(StringLen(marketSourceFields)<=0) return;
   string body = "{\"report_type\":\"positions_snapshot\""
                 + marketSourceFields
                 + ",\"schema_version\":\"" + POSITIONS_SNAPSHOT_SCHEMA + "\""
                 + ",\"ts\":" + IntegerToString((int)TimeGMT())
                 + ",\"positions\":[";
   int cnt = 0;
   for(int i = 0; i < OrdersTotal(); i++) {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderMagicNumber() != Magic) continue;
      int ty = OrderType();
      if(ty != OP_BUY && ty != OP_SELL) continue;
      string side = (ty == OP_BUY) ? "BUY" : "SELL";
      int odg = (int)MarketInfo(OrderSymbol(), MODE_DIGITS);
      if(odg < 0) odg = Digits;
      if(cnt > 0) body = body + ",";
      body = body + "{"
             + "\"symbol\":\"" + JsonEscape(NormalizePairToken(OrderSymbol())) + "\","
             + "\"broker_symbol\":\"" + JsonEscape(OrderSymbol()) + "\","
             + "\"side\":\"" + side + "\","
             + "\"ticket\":" + IntegerToString(OrderTicket()) + ","
             + "\"magic\":" + IntegerToString(OrderMagicNumber()) + ","
             + "\"order_comment\":\"" + JsonEscape(OrderComment()) + "\","
             + "\"type\":" + IntegerToString(ty) + ","
             + "\"lots\":" + DoubleToString(OrderLots(), 2) + ","
             + "\"open_price\":" + DoubleToString(OrderOpenPrice(), odg) + ","
             + "\"open_time\":" + IntegerToString(BrokerTimeUtcEpoch(OrderOpenTime())) + ","
             + "\"sl\":" + DoubleToString(OrderStopLoss(), odg) + ","
             + "\"tp\":" + DoubleToString(OrderTakeProfit(), odg) + ","
             + "\"profit\":" + DoubleToString(OrderProfit(), 2)
             + "}";
      cnt++;
   }
   body = body + "],\"count\":" + IntegerToString(cnt) + "}";
   post_report(body);
}

void SendPositions() {
   string list = "POSITIONS";
   int cnt = 0;
   for(int i=0; i<OrdersTotal(); i++) {
       if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) {
           if(OrderMagicNumber() == Magic && (OrderType()==OP_BUY || OrderType()==OP_SELL)) {
               int odg = (int)MarketInfo(OrderSymbol(), MODE_DIGITS);
               if(odg < 0) odg = Digits;
               // Format: symbol=EURUSD,lots=0.1,profit=10.5;...
               // Using simpler format for parsing: symbol=EURUSD:lots=0.1
               // Or comma separated list of dicts?
               // Let's use semi-colon separated items, comma separated fields
               // POSITIONS item1;item2
               string item = "symbol=" + NormalizePairToken(OrderSymbol()) +
                             ",broker_symbol=" + OrderSymbol() +
                             ",type=" + IntegerToString(OrderType()) +
                             ",open_price=" + DoubleToString(OrderOpenPrice(), odg) +
                             ",open_time=" + IntegerToString(BrokerTimeUtcEpoch(OrderOpenTime())) +
                             ",sl=" + DoubleToString(OrderStopLoss(), odg) +
                             ",lots=" + DoubleToString(OrderLots(), 2) + 
                             ",profit=" + DoubleToString(OrderProfit(), 2);
               list += " " + item;
               cnt++;
           }
       }
   }
   if(cnt == 0) list += " NONE";
   post_report(list);
}

bool IsManagedHistoryTradeSelected() {
   if(OrderMagicNumber() != Magic) return false;
   int ty = OrderType();
   if(ty != OP_BUY && ty != OP_SELL) return false;
   if(OrderCloseTime() <= 0) return false;
   return true;
}

void EmitClosedTradeReportFromSelection() {
   if(!IsManagedHistoryTradeSelected()) return;
   if(!gBridgeHandshakeCompatible) return;
   string marketSourceFields=CurrentBrokerMarketSourceJsonFields();
   if(StringLen(marketSourceFields)<=0) return;
   string brokerSym = OrderSymbol();
   string logicalSym = NormalizePairToken(brokerSym);
   int ty = OrderType();
   int dg = (int)MarketInfo(brokerSym, MODE_DIGITS);
   if(dg < 0) dg = Digits;
   double profit = OrderProfit();
   double swap = OrderSwap();
   double commission = OrderCommission();
   double netProfit = profit + swap + commission;
   string side = (ty == OP_BUY) ? "BUY" : "SELL";
   string payload =
      "{\"report_type\":\"closed_trade\"" +
      marketSourceFields +
      ",\"ticket\":" + IntegerToString(OrderTicket()) +
      ",\"symbol\":\"" + logicalSym + "\"" +
      ",\"broker_symbol\":\"" + brokerSym + "\"" +
      ",\"side\":\"" + side + "\"" +
      ",\"type\":" + IntegerToString(ty) +
      ",\"lots\":" + DoubleToString(OrderLots(), 2) +
      ",\"open_price\":" + DoubleToString(OrderOpenPrice(), dg) +
      ",\"close_price\":" + DoubleToString(OrderClosePrice(), dg) +
      ",\"open_time\":" + IntegerToString(BrokerTimeUtcEpoch(OrderOpenTime())) +
      ",\"close_time\":" + IntegerToString(BrokerTimeUtcEpoch(OrderCloseTime())) +
      ",\"profit\":" + DoubleToString(profit, 2) +
      ",\"swap\":" + DoubleToString(swap, 2) +
      ",\"commission\":" + DoubleToString(commission, 2) +
      ",\"net_profit\":" + DoubleToString(netProfit, 2) +
      "}";
   post_report(payload);
}

void PrimeClosedTradeCursor() {
   datetime latestClose = 0;
   int latestTicket = -1;
   int total = OrdersHistoryTotal();
   for(int i = 0; i < total; i++) {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_HISTORY)) continue;
      if(!IsManagedHistoryTradeSelected()) continue;
      datetime closeTime = OrderCloseTime();
      int ticket = OrderTicket();
      if(closeTime > latestClose || (closeTime == latestClose && ticket > latestTicket)) {
         latestClose = closeTime;
         latestTicket = ticket;
      }
   }
   gLastClosedTradeTime = latestClose;
   gLastClosedTradeTicket = latestTicket;
}

void ReplayRecentClosedTrades(int maxCount) {
   if(gClosedTradeReplayDone) return;
   gClosedTradeReplayDone = true;
   if(maxCount <= 0) return;
   int total = OrdersHistoryTotal();
   int emitted = 0;
   for(int i = total - 1; i >= 0 && emitted < maxCount; i--) {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_HISTORY)) continue;
      if(!IsManagedHistoryTradeSelected()) continue;
      EmitClosedTradeReportFromSelection();
      emitted++;
   }
}

void SendClosedTradeUpdates() {
   int total = OrdersHistoryTotal();
   datetime latestClose = gLastClosedTradeTime;
   int latestTicket = gLastClosedTradeTicket;
   for(int i = 0; i < total; i++) {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_HISTORY)) continue;
      if(!IsManagedHistoryTradeSelected()) continue;
      datetime closeTime = OrderCloseTime();
      int ticket = OrderTicket();
      if(closeTime < gLastClosedTradeTime) continue;
      if(closeTime == gLastClosedTradeTime && ticket <= gLastClosedTradeTicket) continue;
      EmitClosedTradeReportFromSelection();
      if(closeTime > latestClose || (closeTime == latestClose && ticket > latestTicket)) {
         latestClose = closeTime;
         latestTicket = ticket;
      }
   }
   gLastClosedTradeTime = latestClose;
   gLastClosedTradeTicket = latestTicket;
}

void report(string msg){ post_report(msg); }

void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam) {
   // Prevent event bubbling/crashes for clicks
   if(id == CHARTEVENT_OBJECT_CLICK) {
      if(StringFind(sparam, "BridgeHUD") >= 0) return;
   }
}
