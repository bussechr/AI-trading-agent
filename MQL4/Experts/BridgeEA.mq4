#property strict
#include <BridgeUtils.mqh>
#include <BridgeHttp.mqh> // Shared WinInet Logic

// MT4 Bridge EA - WinInet Version
// Configure the broker account in MT4 itself; do not record live identifiers here.

// Bridge wire-protocol version this EA was compiled against. Keep in sync with
// fx-quant-stack/src/fxstack/api/wire.py::BRIDGE_PROTOCOL_VERSION. On mismatch
// the EA logs and posts a report but does not refuse to run (operator decides).
#define EA_EXPECTED_PROTOCOL_VERSION "v2.1.0"

input string ApiBase = "http://127.0.0.1:58710";
input string ApiKey = "";
input int    PollMs  = 1000;
input int    SlipPts = 20;
input int    Magic   = 246810;
input string SymbolsCsv = "EURUSD,USDJPY,GBPUSD,AUDUSD,USDCHF,USDCAD,NZDUSD,EURJPY,EURGBP,GBPJPY,EURCHF,AUDJPY,EURAUD,CADJPY,CHFJPY,GBPCHF,EURCAD,GBPCAD";
input bool   UseIGMinis = true;
input bool   VerboseBridgeLog = false;
input bool   AllowCycleCloseAll = false;
input int    SignalDedupTTLSeconds = 3600;
input int    SignalDedupMax = 256;
input int    ClosedTradeReplayCount = 24;
input int    ClosedTradeReportIntervalSecs = 10;

string DefaultSymbolsCsv() {
   return "EURUSD,USDJPY,GBPUSD,AUDUSD,USDCHF,USDCAD,NZDUSD,EURJPY,EURGBP,GBPJPY,EURCHF,AUDJPY,EURAUD,CADJPY,CHFJPY,GBPCHF,EURCAD,GBPCAD";
}

double gCycleStartEq = 0.0;
double gCycleTargetCash = 0.0;
bool   gCycleActive = false;
string gSeenSignalIds[];
datetime gSeenSignalTs[];
int gSeenCount = 0;
datetime gLastAuthWarnTs = 0;
datetime gLastClosedTradeTime = 0;
int      gLastClosedTradeTicket = -1;
bool     gClosedTradeReplayDone = false;

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

bool ResolveBrokerSymbolEx(string requested, string &resolved) {
   string trimmed = StringTrim(requested);
   string logical = NormalizePairToken(trimmed);
   resolved = "";
   if(StringLen(trimmed) <= 0) return false;

   int total = SymbolsTotal(false);
   string partial = "";
   for(int i = 0; i < total; i++) {
      string candidate = SymbolName(i, false);
      string upper = ToUpperSafe(candidate);
      if(StringLen(upper) <= 0) continue;
      if(upper == ToUpperSafe(trimmed) || upper == logical) {
         SymbolSelect(candidate, true);
         resolved = candidate;
         return true;
      }
      if(StringFind(upper, logical, 0) >= 0 && StringLen(partial) <= 0) {
         partial = candidate;
      }
   }

   total = SymbolsTotal(true);
   for(int j = 0; j < total; j++) {
      string selected = SymbolName(j, true);
      string upperSelected = ToUpperSafe(selected);
      if(StringLen(upperSelected) <= 0) continue;
      if(upperSelected == ToUpperSafe(trimmed) || upperSelected == logical) {
         SymbolSelect(selected, true);
         resolved = selected;
         return true;
      }
      if(StringFind(upperSelected, logical, 0) >= 0 && StringLen(partial) <= 0) {
         partial = selected;
      }
   }

   if(StringLen(partial) > 0) {
      SymbolSelect(partial, true);
      resolved = partial;
      return true;
   }

   return false;
}

string ResolveBrokerSymbol(string requested) {
   string resolved = "";
   if(!ResolveBrokerSymbolEx(requested, resolved)) return "";
   return resolved;
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
   return "/v2/commands/poll?format=line";
}

string ReportPath() {
   return "/v2/reports";
}

string TickPath() {
   return "/v2/market/tick";
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
   string interop_mode = ""
) {
   if(StringLen(signal_id) <= 0) return;
   double t_ea_ack_post = (double)TimeCurrent();
   string payload = "{\"signal_id\":\"" + JsonEscape(signal_id) +
                    "\",\"command_id\":\"" + JsonEscape(signal_id) +
                    "\",\"status\":\"" + JsonEscape(status) +
                    "\",\"symbol\":\"" + JsonEscape(symbol) +
                    "\",\"ticket\":" + IntegerToString(ticket) +
                    ",\"error_code\":" + IntegerToString(error_code) +
                    ",\"message\":\"" + JsonEscape(message) +
                    "\",\"status_reason\":\"" + JsonEscape(message) +
                    "\",\"trace_id\":\"" + JsonEscape(trace_id) +
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
   HttpPOST(ApiBase + AckPath(), payload, ApiKey);
   WarnAuthFailure("ack", LastBridgeHttpStatus());
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

// Soft check that the bridge speaks the protocol version this EA was built for.
// Logs and posts a report on mismatch but does not refuse to run, so an
// operator can still see traffic and decide whether to recompile/redeploy.
void VerifyBridgeHandshake() {
   string url = ApiBase + "/v2/handshake";
   string resp = HttpGET(url, ApiKey);
   if(StringLen(resp) == 0) {
      Print("[BRIDGE] handshake: no response from ", url, " (bridge offline?)");
      post_report("BRIDGE_HANDSHAKE_FAIL reason=no_response url=" + url);
      return;
   }
   string expected = "\"protocol_version\":\"" + EA_EXPECTED_PROTOCOL_VERSION + "\"";
   if(StringFind(resp, expected) >= 0) {
      Print("[BRIDGE] handshake OK: protocol=", EA_EXPECTED_PROTOCOL_VERSION);
      return;
   }
   Print("[BRIDGE] handshake MISMATCH: expected ", EA_EXPECTED_PROTOCOL_VERSION, " in ", resp);
   post_report("BRIDGE_HANDSHAKE_MISMATCH expected=" + EA_EXPECTED_PROTOCOL_VERSION + " resp=" + resp);
}

int OnInit(){
   EventSetTimer(1);
   Print("MT4 Bridge EA (WinInet) initialized");
   Print("ApiBase: ", ApiBase);
   ArrayResize(gSeenSignalIds, 0);
   ArrayResize(gSeenSignalTs, 0);
   gSeenCount = 0;

   // Initialize Shared WinInet Session
   if(!InitBridgeHttp("MT4_Bridge_EA")) {
       return(INIT_FAILED);
   }

   // Verify protocol version compatibility with the bridge (soft check).
   VerifyBridgeHandshake();

   // Show initial status
   UpdateDashboard("WAITING FOR AGENT...|Starting Python Bridge...");
   ChartRedraw(0);
   reportBridgeStatus();
   PrimeClosedTradeCursor();
   ReplayRecentClosedTrades(ClosedTradeReplayCount);

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

   ChartRedraw(0);
}

void OnDeinit(const int reason){ 
   EventKillTimer(); 
   // Give pending operations a moment to settle
   Sleep(250);
   
   RemoveDashboard();
   
   DeinitBridgeHttp();
}

void post_report(string msg) {
   // Print("Sending Report: ", msg); // DEBUG REMOVED
   HttpPOST(ApiBase + ReportPath(), msg, ApiKey);
   WarnAuthFailure("report", LastBridgeHttpStatus());
}

void heartbeat(){
   string transport = gUseWebRequest ? "webrequest" : "wininet";
   string out = "HEARTBEAT eq=" + DoubleToString(AccountEquity(), 2) + 
                " margin=" + DoubleToString(AccountMargin(), 2) + 
                " freemargin=" + DoubleToString(AccountFreeMargin(), 2) +
                " transport=" + transport;
   post_report(out);
}

void reportBridgeStatus() {
   string transport = gUseWebRequest ? "webrequest" : "wininet";
   string syms[];
   int n = EffectiveSymbols(syms);
   string pairsJson = "[";
   string readinessJson = "{";
   int readyCount = 0;

   for(int i = 0; i < n; i++) {
      string logicalSym = NormalizePairToken(syms[i]);
      string brokerSym = "";
      bool supported = ResolveBrokerSymbolEx(logicalSym, brokerSym);
      bool selected = false;
      if(supported) {
         selected = SymbolSelect(brokerSym, true);
         if(!selected) supported = false;
      }
      if(i > 0) {
         pairsJson = pairsJson + ",";
         readinessJson = readinessJson + ",";
      }
      pairsJson = pairsJson + "\"" + JsonEscape(logicalSym) + "\"";
      readinessJson = readinessJson +
         "\"" + JsonEscape(logicalSym) + "\":{" +
         "\"broker_symbol\":\"" + JsonEscape(brokerSym) + "\"," +
         "\"supported\":" + JsonBool(supported) + "," +
         "\"selected\":" + JsonBool(selected) +
         "}";
      if(supported) readyCount++;
   }
   pairsJson = pairsJson + "]";
   readinessJson = readinessJson + "}";

   string payload =
      "{\"report_type\":\"bridge_status\"" +
      ",\"equity\":" + DoubleToString(AccountEquity(), 2) +
      ",\"margin\":" + DoubleToString(AccountMargin(), 2) +
      ",\"freemargin\":" + DoubleToString(AccountFreeMargin(), 2) +
      ",\"transport_mode\":\"" + JsonEscape(transport) + "\"" +
      ",\"configured_pairs\":" + pairsJson +
      ",\"symbol_ready_count\":" + IntegerToString(readyCount) +
      ",\"symbol_readiness\":" + readinessJson +
      "}";
   post_report(payload);
}

void OnTick(){
   // Moved to OnTimer for consistent updates
}

void broadcastTick() {
   string syms[];
   int n = EffectiveSymbols(syms);
   if(n <= 0){
      ArrayResize(syms, 1);
      syms[0] = Symbol();
      n = 1;
   }

   for(int i = 0; i < n; i++){
      string logicalSym = NormalizePairToken(syms[i]);
      string brokerSym = ResolveBrokerSymbol(logicalSym);
      if(StringLen(logicalSym) <= 0 || StringLen(brokerSym) <= 0) continue;
      SymbolSelect(brokerSym, true);
      double bid = MarketInfo(brokerSym, MODE_BID);
      double ask = MarketInfo(brokerSym, MODE_ASK);
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

      string tick = "{\"symbol\":\"" + logicalSym +
                    "\",\"broker_symbol\":\"" + brokerSym +
                    "\",\"bid\":" + DoubleToString(bid, digits) +
                    ",\"ask\":" + DoubleToString(ask, digits) +
                    ",\"mid\":" + DoubleToString(mid, digits) +
                    ",\"spread\":" + DoubleToString(spread_pips, 3) +
                    ",\"spread_points\":" + IntegerToString(spread_points) +
                    ",\"spread_pips\":" + DoubleToString(spread_pips, 3) +
                    ",\"spread_bps\":" + DoubleToString(spread_bps, 6) +
                    ",\"digits\":" + IntegerToString(digits) + "}";
      HttpPOST(ApiBase + TickPath(), tick, ApiKey);
      WarnAuthFailure("tick", LastBridgeHttpStatus());
   }
}

void OnTimer(){
   manageCycle();
   heartbeat();
   static datetime lastStatusReport = 0;
   if(TimeCurrent() > lastStatusReport + 14) {
      reportBridgeStatus();
      lastStatusReport = TimeCurrent();
   }
   broadcastTick(); // Send 1 tick per second
   
   static datetime lastPosReport = 0;
   if(TimeCurrent() > lastPosReport + 4) { // Every 5s
      SendPositions();
      lastPosReport = TimeCurrent();
   }
   // JSON-structured snapshot for /v2/positions/reconcile (slower cadence).
   static datetime lastPosSnapshot = 0;
   if(TimeCurrent() > lastPosSnapshot + 9) { // Every 10s
      EmitPositionsSnapshot();
      lastPosSnapshot = TimeCurrent();
   }
   static datetime lastClosedTradeReport = 0;
   if(TimeCurrent() > lastClosedTradeReport + ClosedTradeReportIntervalSecs) {
      SendClosedTradeUpdates();
      lastClosedTradeReport = TimeCurrent();
   }

   string pollUrl = ApiBase + PollPath();
   string resp = HttpGET(pollUrl, ApiKey);
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

void HandleCmd(string line){
   string items[]; int n = StringSplit(line,';',items);
   uint t_handle_start_ms = GetTickCount();
   double t_ea_received = (double)TimeCurrent();
   string cmd="", sym="", signal_id="", command_id="", intent="", trace_id="", interop_mode="";
   double lots=0, close_lots=0, tp_cash=0, tp_price=0, sl=0, action_score=0, t_py_signal_post_start=0, t_bridge_queued=0, t_bridge_delivered=0;
   string action="", reversal_token="";
   int magic=Magic;
   for(int i=0;i<n;i++){
      string kv[]; if(StringSplit(items[i],'=',kv)!=2) continue;
      string k=StringTrim(kv[0]), v=StringTrim(kv[1]);
      if(k=="cmd") cmd=v;
      if(k=="symbol") sym=v;
      if(k=="lots") lots=StrToDouble(v);
      if(k=="tp_cash") tp_cash=StrToDouble(v);
      if(k=="tp_price") tp_price=StrToDouble(v);
      if(k=="sl") sl=StrToDouble(v);
      if(k=="close_lots") close_lots=StrToDouble(v);
      if(k=="magic") magic=(int)StrToInteger(v);
      if(k=="signal_id") signal_id=v;
      if(k=="command_id") command_id=v;
      if(k=="intent") intent=v;
      if(k=="trace_id") trace_id=v;
      if(k=="interop_mode") interop_mode=v;
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
      if(SeenSignalRecently(signal_id)){
         post_report("DUPLICATE signal_id=" + signal_id + " cmd=" + cmd + " sym=" + sym);
         post_ack(
            signal_id, "duplicate", sym, -1, 0, "duplicate_suppressed",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
         return;
      }
      RememberSignalId(signal_id);
   }
   if(cmd=="CLOSE_ALL"){
      int closeErr = 0;
      bool okAll = CloseAll(closeErr);
      resetCycle();
      if(okAll){
         post_report("CLOSE_ALL_OK");
         post_ack(
            signal_id, "acked", "", -1, 0, "close_all_ok",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
      } else {
         post_report("ERR close_all " + IntegerToString(closeErr));
         post_ack(
            signal_id, "failed", "", -1, closeErr, "close_all_failed",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
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
      int closeErr2 = 0;
      bool okClose = CloseSymbol(sym, magic, closeErr2);
      if(okClose){
         post_ack(
            signal_id, "acked", sym, -1, 0, "close_ok",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
      } else {
         post_ack(
            signal_id, "failed", sym, -1, closeErr2, "close_failed",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
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
      if(close_lots <= 0) close_lots = lots;
      if(close_lots <= 0){
         post_report("ERR close_partial invalid_lots");
         post_ack(
            signal_id, "failed", sym, -1, 400, "invalid_lots",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
         return;
      }
      int closeErr3 = 0;
      bool okClosePartial = CloseSymbolPartial(sym, magic, close_lots, closeErr3);
      if(okClosePartial){
         post_ack(
            signal_id, "acked", sym, -1, 0, "close_partial_ok",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
      } else {
         post_ack(
            signal_id, "failed", sym, -1, closeErr3, "close_partial_failed",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
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
      int modErr = 0;
      bool okModify = ModifySymbolStop(sym, magic, sl, modErr);
      if(okModify){
         post_ack(
            signal_id, "acked", sym, -1, 0, "modify_sl_ok",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
      } else {
         post_ack(
            signal_id, "failed", sym, -1, modErr, "modify_sl_failed",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
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
      if(StringLen(sym) <= 0){
         post_report("ERR order missing_symbol");
         post_ack(
            signal_id, "failed", sym, -1, 400, "missing_symbol",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
         return;
      }
      if(lots < 0){
         post_report("ERR order invalid_lots");
         post_ack(
            signal_id, "failed", sym, -1, 400, "invalid_lots",
            trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
            t_ea_received, 0.0, 0.0, (double)(GetTickCount() - t_handle_start_ms), interop_mode
         );
         return;
      }
      Execute(
         cmd, sym, lots, tp_cash, tp_price, sl, magic, signal_id, intent,
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_handle_start_ms, interop_mode
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

   // --- PARSING ---
   string lines[]; 
   int n = StringSplit(text, '|', lines);
   int MAX_ROWS = 14;
   int shown = n;
   if(shown > MAX_ROWS) shown = MAX_ROWS;
   if(shown < 1) shown = 1;
   int totalHeight = (shown * ROW_HEIGHT) + HDR_HEIGHT + (PADDING * 2);
   
   // --- POSITIONING ---
   long chartWidth = ChartGetInteger(0, CHART_WIDTH_IN_PIXELS);
   long chartHeight = ChartGetInteger(0, CHART_HEIGHT_IN_PIXELS);
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
   
   // Cleanup excess lines
   for(int k=shown; k<60; k++) {
      string delName = "BridgeHUD_Txt_" + IntegerToString(k);
      if(ObjectFind(0, delName) >= 0) ObjectDelete(0, delName);
   }
   
   ChartRedraw(0);
}

void Execute(
   string cmd,
   string sym,
   double lots,
   double tp_cash,
   double tp_price_in,
   double sl,
   int magic,
   string signal_id = "",
   string intent = "",
   string trace_id = "",
   double t_py_signal_post_start = 0.0,
   double t_bridge_queued = 0.0,
   double t_bridge_delivered = 0.0,
   double t_ea_received = 0.0,
   int t_handle_start_ms = 0,
   string interop_mode = ""
){
   string logicalSym = NormalizePairToken(sym);
   string brokerSym = ResolveBrokerSymbol(sym);
   UpdateDashboard("Submitting " + cmd + " " + logicalSym + "...|Awaiting broker confirmation");
   double t_ea_exec_start = (double)TimeCurrent();
   if(StringLen(brokerSym) <= 0 || SymbolSelect(brokerSym,true)==false){
      UpdateDashboard("Order failed " + cmd + " " + logicalSym + "|symbol_select_failed");
      post_report("ERR symbol " + sym);
      post_ack(
         signal_id, "failed", logicalSym, -1, 410, "symbol_select_failed",
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
         (double)(GetTickCount() - t_handle_start_ms), interop_mode
      );
      return;
   }
   RefreshRates();
   int type=(cmd=="BUY")?OP_BUY:OP_SELL;
   double ask = MarketInfo(brokerSym, MODE_ASK);
   double bid = MarketInfo(brokerSym, MODE_BID);
   int symDigits = (int)MarketInfo(brokerSym, MODE_DIGITS);
   if(symDigits < 0) symDigits = Digits;
   if(ask <= 0 || bid <= 0){
      UpdateDashboard("Order failed " + cmd + " " + logicalSym + "|quote_unavailable");
      post_report("ERR quote " + sym);
      post_ack(
         signal_id, "failed", logicalSym, -1, 411, "quote_unavailable",
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
         (double)(GetTickCount() - t_handle_start_ms), interop_mode
      );
      return;
   }
   double px=(type==OP_BUY)?ask:bid;
   px = NormalizeDouble(px, symDigits);
   
   double lots2;
   if(UseIGMinis && (lots<=0.0)){
      lots2 = IGMiniLot(brokerSym);
   } else if(lots<=0.0){
      lots2 = MinLot(brokerSym);
   } else {
      lots2 = RoundLot(brokerSym,lots);
   }

   // TP: Use absolute price if provided, otherwise convert from cash
   double tp=0;
   if(tp_price_in > 0) {
      tp = NormalizeDouble(tp_price_in, symDigits); // Absolute price from Python agent
   } else if(tp_cash > 0) {
      tp = TpFromCash(brokerSym, type, px, lots2, tp_cash); // Legacy cash conversion
      tp = NormalizeDouble(tp, symDigits);
   }
   double slNorm = 0;
   if(sl > 0) slNorm = NormalizeDouble(sl, symDigits);
   post_report(
      "EXEC cmd=" + cmd +
      " sym=" + logicalSym +
      " broker=" + brokerSym +
      " intent=" + intent +
      " px=" + DoubleToString(px, symDigits) +
      " bid=" + DoubleToString(bid, symDigits) +
      " ask=" + DoubleToString(ask, symDigits) +
      " lots=" + DoubleToString(lots2, 2)
   );

   int ticket = -1;
   int err = 0;
   int usedSlip = SlipPts;
   int retriesUsed = 0;
   for(int attempt=0; attempt<3; attempt++){
      if(attempt > 0){
         Sleep(80);
         RefreshRates();
         ask = MarketInfo(brokerSym, MODE_ASK);
         bid = MarketInfo(brokerSym, MODE_BID);
         if(ask > 0 && bid > 0){
            px = (type==OP_BUY)?ask:bid;
            px = NormalizeDouble(px, symDigits);
         }
      }
      usedSlip = SlipPts + (attempt * 10); // 20 -> 30 -> 40 with default inputs.
      ticket = OrderSend(brokerSym, type, lots2, px, usedSlip, slNorm, tp, "ELBridge", magic, 0, (type==OP_BUY)?clrGreen:clrRed);
      if(ticket >= 0){
         retriesUsed = attempt;
         break;
      }
      err = GetLastError();
      retriesUsed = attempt;
      post_report(
         "WARN order_retry sym=" + logicalSym +
         " broker=" + brokerSym +
         " attempt=" + IntegerToString(attempt + 1) +
         " err=" + IntegerToString(err) +
         " slip=" + IntegerToString(usedSlip)
      );
   }
   if(ticket<0){ 
      UpdateDashboard("Order failed " + cmd + " " + logicalSym + "|err=" + IntegerToString(err));
      post_report("ERR order "+IntegerToString(err)); 
      Print("OrderSend error: ", err);
      post_ack(
         signal_id, "failed", logicalSym, -1, err, "order_send_failed",
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
         (double)(GetTickCount() - t_handle_start_ms), interop_mode
      );
      return; 
   }

   if(!gCycleActive){
      gCycleStartEq = AccountEquity();
      gCycleTargetCash = gCycleStartEq * 0.01;
      gCycleActive = true;
      post_report("CYCLE_START eq="+DoubleToString(gCycleStartEq,2)+" target="+DoubleToString(gCycleTargetCash,2));
   }
   UpdateDashboard(cmd + " opened " + logicalSym + "|ticket=" + IntegerToString(ticket) + " lots=" + DoubleToString(lots2, 2));
   post_report(
      "OK "+cmd+" "+logicalSym+
      " broker="+brokerSym+
      " ticket="+IntegerToString(ticket)+
      " lots="+DoubleToString(lots2,2)+
      " retries="+IntegerToString(retriesUsed)+
      " slip="+IntegerToString(usedSlip)
   );
   post_ack(
      signal_id, "acked", logicalSym, ticket, 0, "order_send_ok",
      trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
      t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
      (double)(GetTickCount() - t_handle_start_ms), interop_mode
   );
}

void manageCycle(){
   if(!gCycleActive) return;
   double eq = AccountEquity();
   if(eq >= gCycleStartEq + gCycleTargetCash){
      post_report("CYCLE_TARGET_HIT eq="+DoubleToString(eq,2)+" profit="+DoubleToString(eq-gCycleStartEq,2));
      if(AllowCycleCloseAll){
         int cycleErr = 0;
         bool cycleCloseOk = CloseAll(cycleErr);
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

bool CloseAll(int &lastErr){
   bool ok = true;
   lastErr = 0;
   for(int i=OrdersTotal()-1;i>=0;i--){
      if(!OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(OrderMagicNumber()!=Magic) continue;
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

bool CloseSymbolPartial(string sym, int target_magic, double closeLots, int &lastErr) {
   bool ok = true;
   bool closedAny = false;
   lastErr = 0;
   double remaining = closeLots;
   double minExecutable = 0.0;
   if(remaining <= 0){
      lastErr = 400;
      return false;
   }

   for(int i=OrdersTotal()-1; i>=0; i--) {
      if(!OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(OrderMagicNumber() != target_magic) continue;
      if(!SymbolsMatch(OrderSymbol(), sym)) continue;
      int ty = OrderType();
      if(ty != OP_BUY && ty != OP_SELL) continue;
      if(remaining <= 0) break;

      string osym = OrderSymbol();
      double orderLots = OrderLots();
      double toClose = MathMin(orderLots, remaining);
      toClose = RoundLot(osym, toClose);
      double step = MarketInfo(osym, MODE_LOTSTEP);
      double minlot = MarketInfo(osym, MODE_MINLOT);
      if(step > minExecutable) minExecutable = step;
      if(minlot > minExecutable) minExecutable = minlot;
      if(toClose <= 0) continue;

      double ask = MarketInfo(osym, MODE_ASK);
      double bid = MarketInfo(osym, MODE_BID);
      int dg = (int)MarketInfo(osym, MODE_DIGITS);
      if(dg < 0) dg = Digits;
      double px = (ty == OP_BUY) ? bid : ask;
      px = NormalizeDouble(px, dg);
      if(px <= 0){
         ok = false;
         lastErr = 411;
         continue;
      }

      if(!OrderClose(OrderTicket(), toClose, px, SlipPts)) {
         ok = false;
         lastErr = GetLastError();
      } else {
         closedAny = true;
         remaining -= toClose;
      }
   }

   if(!closedAny){
      ok = false;
      if(lastErr == 0) lastErr = 404;
      return false;
   }
   if(minExecutable <= 0.0) minExecutable = 0.01;
   if(remaining > (minExecutable + 0.000001)){
      ok = false;
      if(lastErr == 0) lastErr = 409;
   }
   return ok;
}

bool ModifySymbolStop(string sym, int target_magic, double sl_price, int &lastErr) {
   bool ok = true;
   bool modifiedAny = false;
   lastErr = 0;
   for(int i=OrdersTotal()-1; i>=0; i--) {
      if(!OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(OrderMagicNumber() != target_magic) continue;
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
// compute the diff against DB-known positions. Sent alongside the legacy
// text-format SendPositions() to keep backward compatibility.
void EmitPositionsSnapshot() {
   string body = "{\"report_type\":\"positions_snapshot\",\"ts\":"
                 + IntegerToString((int)TimeCurrent())
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
             + "\"symbol\":\"" + NormalizePairToken(OrderSymbol()) + "\","
             + "\"broker_symbol\":\"" + OrderSymbol() + "\","
             + "\"side\":\"" + side + "\","
             + "\"ticket\":" + IntegerToString(OrderTicket()) + ","
             + "\"type\":" + IntegerToString(ty) + ","
             + "\"lots\":" + DoubleToString(OrderLots(), 2) + ","
             + "\"open_price\":" + DoubleToString(OrderOpenPrice(), odg) + ","
             + "\"open_time\":" + IntegerToString((int)OrderOpenTime()) + ","
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
                             ",open_time=" + IntegerToString((int)OrderOpenTime()) +
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
      ",\"ticket\":" + IntegerToString(OrderTicket()) +
      ",\"symbol\":\"" + logicalSym + "\"" +
      ",\"broker_symbol\":\"" + brokerSym + "\"" +
      ",\"side\":\"" + side + "\"" +
      ",\"type\":" + IntegerToString(ty) +
      ",\"lots\":" + DoubleToString(OrderLots(), 2) +
      ",\"open_price\":" + DoubleToString(OrderOpenPrice(), dg) +
      ",\"close_price\":" + DoubleToString(OrderClosePrice(), dg) +
      ",\"open_time\":" + IntegerToString((int)OrderOpenTime()) +
      ",\"close_time\":" + IntegerToString((int)OrderCloseTime()) +
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
