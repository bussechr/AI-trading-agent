#property strict
#include <BridgeUtils.mqh>
#include <BridgeHttp.mqh> // Shared WinInet Logic

// MT4 Bridge EA - WinInet Version
// Configure the broker account in MT4 itself; do not record live identifiers here.

// Bridge wire-protocol version this EA was compiled against. Keep in sync with
// fx-quant-stack/src/fxstack/api/wire.py::BRIDGE_PROTOCOL_VERSION. On mismatch
// the EA logs and posts a report but does not refuse to run (operator decides).
#define EA_EXPECTED_PROTOCOL_VERSION "v2.1.0"
#define ACK_OUTBOX_MAX_PENDING 512
#define ACK_OUTBOX_REPLAY_PER_TIMER 4
#define ACK_OUTBOX_REPLAY_ON_STARTUP 4
#define ACK_OUTBOX_RECOVER_PER_PASS 8
#define ACK_OUTBOX_UNPERSISTED_MAX 8

input string ApiBase = "http://127.0.0.1:58710";
input string ApiKey = "";
input int    PollMs  = 1000;
input int    SlipPts = 20;
input int    Magic   = 246810;
input string SymbolsCsv = "EURUSD,USDJPY,GBPUSD,AUDUSD,USDCHF,USDCAD,NZDUSD,EURJPY,EURGBP,GBPJPY,EURCHF,AUDJPY,EURAUD,CADJPY,CHFJPY,GBPCHF,EURCAD,GBPCAD";
// Legacy compatibility toggle only. Approved command lots are never replaced
// with a mini/minimum lot; every entry must already be exactly executable.
input bool   UseIGMinis = true;
input bool   VerboseBridgeLog = false;
input bool   AllowCycleCloseAll = false;
input int    SignalDedupTTLSeconds = 3600;
input int    SignalDedupMax = 256;
input int    ClosedTradeReplayCount = 24;
input int    ClosedTradeReportIntervalSecs = 10;
input int    BarHistoryDepth = 1200;
input int    BarHistoryBatchSize = 100;
input string BarHistorySymbolsCsv = "";

string DefaultSymbolsCsv() {
   return "EURUSD,USDJPY,GBPUSD,AUDUSD,USDCHF,USDCAD,NZDUSD,EURJPY,EURGBP,GBPJPY,EURCHF,AUDJPY,EURAUD,CADJPY,CHFJPY,GBPCHF,EURCAD,GBPCAD";
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
datetime gLastAckOutboxWarnTs = 0;
bool     gAckScopePinned = false;
int      gAckScopeAccountNumber = 0;
string   gAckScopeAccountServer = "";
string   gAckScopeApiBase = "";
int      gAckScopeMagic = 0;
string   gAckScopeTerminalDataPath = "";
string   gAckScopeTerminalToken = "";
string   gAckScopeDirectory = "";
string   gBridgeApiKey = "";
datetime gLastBarHistoryAttempt = 0;

string LoadBridgeApiKey() {
   string configured = StringTrim(ApiKey);
   if(StringLen(configured) > 0) return configured;

   ResetLastError();
   int handle = FileOpen("bridge_api_key.txt", FILE_READ|FILE_TXT|FILE_ANSI);
   if(handle == INVALID_HANDLE) {
      Print("[BRIDGE] bridge_api_key.txt unavailable in MQL4/Files; auth will fail closed (err=", GetLastError(), ")");
      return "";
   }
   string loaded = StringTrim(FileReadString(handle));
   FileClose(handle);
   if(StringLen(loaded) <= 0) {
      Print("[BRIDGE] bridge_api_key.txt is empty; auth will fail closed");
      return "";
   }
   return loaded;
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

string BarHistoryPath() {
   return "/v2/market/bars";
}

int BarHistorySymbols(string &out[]) {
   int configured = SymbolsFromCsv(BarHistorySymbolsCsv, out);
   if(configured > 0) return configured;
   ArrayResize(out, 1);
   out[0] = NormalizePairToken(Symbol());
   return StringLen(out[0]) > 0 ? 1 : 0;
}

bool SendBarHistoryForSymbol(string logicalSym) {
   string brokerSym = ResolveBrokerSymbol(logicalSym);
   if(StringLen(brokerSym) <= 0) return false;
   SymbolSelect(brokerSym, true);

   int available = iBars(brokerSym, PERIOD_M5);
   int depth = MathMax(1, MathMin(2000, BarHistoryDepth));
   int oldestShift = MathMin(depth, available - 1);
   if(oldestShift < 1) {
      Print("[BRIDGE] bar history unavailable for ", logicalSym, " broker=", brokerSym);
      return false;
   }

   int batchLimit = MathMax(1, MathMin(500, BarHistoryBatchSize));
   int serverOffset = (int)(TimeCurrent() - TimeGMT());
   int digits = (int)MarketInfo(brokerSym, MODE_DIGITS);
   double point = MarketInfo(brokerSym, MODE_POINT);
   double spreadPx = MathMax(0.0, MarketInfo(brokerSym, MODE_SPREAD) * point);
   string barsJson = "";
   int batchCount = 0;
   int sentCount = 0;

   for(int shift = oldestShift; shift >= 1; shift--) {
      datetime brokerTime = iTime(brokerSym, PERIOD_M5, shift);
      double bidOpen = iOpen(brokerSym, PERIOD_M5, shift);
      double bidHigh = iHigh(brokerSym, PERIOD_M5, shift);
      double bidLow = iLow(brokerSym, PERIOD_M5, shift);
      double bidClose = iClose(brokerSym, PERIOD_M5, shift);
      if(brokerTime <= 0 || bidOpen <= 0 || bidHigh <= 0 || bidLow <= 0 || bidClose <= 0) continue;

      int utcEpoch = (int)brokerTime - serverOffset;
      double halfSpread = spreadPx / 2.0;
      double midOpen = bidOpen + halfSpread;
      double midHigh = bidHigh + halfSpread;
      double midLow = bidLow + halfSpread;
      double midClose = bidClose + halfSpread;
      int volume = (int)MathMax(0, MathMin(2147483647.0, (double)iVolume(brokerSym, PERIOD_M5, shift)));
      string row =
         "{\"time\":" + IntegerToString(utcEpoch) +
         ",\"open\":" + DoubleToString(midOpen, digits) +
         ",\"high\":" + DoubleToString(midHigh, digits) +
         ",\"low\":" + DoubleToString(midLow, digits) +
         ",\"close\":" + DoubleToString(midClose, digits) +
         ",\"spread\":" + DoubleToString(spreadPx, digits) +
         ",\"volume\":" + IntegerToString(volume) + "}";
      if(batchCount > 0) barsJson = barsJson + ",";
      barsJson = barsJson + row;
      batchCount++;

      if(batchCount >= batchLimit || shift == 1) {
         string payload =
            "{\"symbol\":\"" + JsonEscape(logicalSym) +
            "\",\"timeframe\":\"M5\",\"bars\":[" + barsJson + "]}";
         HttpPOST(ApiBase + BarHistoryPath(), payload, gBridgeApiKey);
         int statusCode = LastBridgeHttpStatus();
         WarnAuthFailure("bar_history", statusCode);
         if(statusCode < 200 || statusCode >= 300) {
            Print("[BRIDGE] bar history batch failed symbol=", logicalSym,
                  " status=", statusCode, " sent=", sentCount);
            return false;
         }
         sentCount += batchCount;
         barsJson = "";
         batchCount = 0;
      }
   }

   Print("[BRIDGE] bar history synced symbol=", logicalSym, " bars=", sentCount);
   return sentCount > 0;
}

void MaybeSyncBarHistory(bool force) {
   if(BarHistoryDepth <= 0) return;
   datetime now = TimeCurrent();
   if(now <= 0) now = TimeLocal();
   if(!force && gLastBarHistoryAttempt > 0 && (now - gLastBarHistoryAttempt) < 10) return;
   gLastBarHistoryAttempt = now;

   string symbols[];
   int count = BarHistorySymbols(symbols);
   for(int i = 0; i < count; i++) {
      string logicalSym = NormalizePairToken(symbols[i]);
      if(StringLen(logicalSym) > 0 && !SendBarHistoryForSymbol(logicalSym)) return;
   }
}

uint AckOutboxHash(string value) {
   uint hash=5381;
   int length=StringLen(value);
   for(int i=0; i<length; i++) {
      hash=((hash<<5)+hash)^(uint)StringGetCharacter(value,i);
   }
   return(hash);
}

// ACK files are shared across terminals but isolated by a pinned broker
// account/server, exact bridge endpoint, and Magic. ApiKey is deliberately
// excluded. A disconnected account is never allowed to create an account_0
// scope that could orphan an outcome after login becomes available.
bool TryPinAckOutboxScopeIdentity() {
   if(gAckScopePinned) return(true);
   int accountNumber=AccountNumber();
   string accountServer=StringTrim(AccountServer());
   string endpoint=StringTrim(ApiBase);
   string terminalDataPath=StringTrim(TerminalInfoString(TERMINAL_DATA_PATH));
   if(
      accountNumber<=0 || StringLen(accountServer)<=0 ||
      StringLen(endpoint)<=0 || StringLen(terminalDataPath)<=0
   ) {
      BlockAckOutbox("scope_identity_unavailable_account_or_server_or_endpoint_or_terminal");
      return(false);
   }

   gAckScopeAccountNumber=accountNumber;
   gAckScopeAccountServer=accountServer;
   gAckScopeApiBase=endpoint;
   gAckScopeMagic=Magic;
   gAckScopeTerminalDataPath=terminalDataPath;
   gAckScopeTerminalToken=IntegerToString((int)AckOutboxHash(terminalDataPath));
   string endpointToken=IntegerToString((int)AckOutboxHash(endpoint));
   string serverToken=IntegerToString((int)AckOutboxHash(accountServer));
   gAckScopeDirectory=
      "FXStack\\AckOutbox\\account_"+IntegerToString(accountNumber)+
      "_server_"+serverToken+
      "\\endpoint_"+endpointToken+"_"+IntegerToString(StringLen(endpoint))+
      "_magic_"+IntegerToString(Magic);
   gAckScopePinned=true;
   Print(
      "[ACK_OUTBOX] scope pinned account=",gAckScopeAccountNumber,
      " server=",gAckScopeAccountServer,
      " endpoint=",gAckScopeApiBase,
      " magic=",gAckScopeMagic,
      " terminal=",gAckScopeTerminalToken
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
   HttpPOST(gAckScopeApiBase+AckPath(),payload,gBridgeApiKey);
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
   if(!identityMatches) {
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
   string queuedPath="";
   if(!QueueAckPayloadBeforePost(payload,queuedPath)) return;
   ReplayAckOutboxFile(queuedPath);
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

// Soft check that the bridge speaks the protocol version this EA was built
// for, AND read configuration the bridge pushes through the handshake
// (currently: basket_tp_pct). Logs and posts a report on mismatch but does
// not refuse to run, so an operator can still see traffic and decide whether
// to recompile/redeploy.
void VerifyBridgeHandshake() {
   string url = ApiBase + "/v2/handshake";
   string resp = HttpGET(url, gBridgeApiKey);
   if(StringLen(resp) == 0) {
      Print("[BRIDGE] handshake: no response from ", url, " (bridge offline?)");
      post_report("BRIDGE_HANDSHAKE_FAIL reason=no_response url=" + url);
      return;
   }

   // Pull the basket-TP override before checking version, so even on a
   // soft-warned version mismatch the EA still respects the operator's
   // configured target.
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
   gBridgeApiKey = LoadBridgeApiKey();
   ArrayResize(gSeenSignalIds, 0);
   ArrayResize(gSeenSignalTs, 0);
   gSeenCount = 0;
   ArrayResize(gAckUnpersistedPayloads,0);
   gAckUnpersistedCount=0;
   gAckReplayCursor=0;
   gAckOutboxBlocked=false;
   gLastAckOutboxWarnTs=0;
   gAckScopePinned=false;
   gAckScopeAccountNumber=0;
   gAckScopeAccountServer="";
   gAckScopeApiBase="";
   gAckScopeMagic=0;
   gAckScopeTerminalDataPath="";
   gAckScopeTerminalToken="";
   gAckScopeDirectory="";

   // Initialize Shared WinInet Session
   if(!InitBridgeHttp("MT4_Bridge_EA")) {
       return(INIT_FAILED);
   }

   // Load and replay durable broker outcomes before this EA is allowed to poll
   // another command. Replaying an ACK never re-enters HandleCmd.
   UpdateDashboard("WAITING FOR AGENT...|Replaying durable ACK outbox...");
   ServiceAckOutbox(ACK_OUTBOX_REPLAY_ON_STARTUP);

   // Verify protocol version compatibility with the bridge (soft check).
   VerifyBridgeHandshake();
   MaybeSyncBarHistory(true);

   // Show initial status
   UpdateDashboard("WAITING FOR AGENT...|Starting Python Bridge...");
   ChartRedraw(0);
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

   ChartRedraw(0);
}

void OnDeinit(const int reason){ 
   EventKillTimer();
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

void heartbeat(){
   string transport = gUseWebRequest ? "webrequest" : "wininet";
   string out = "HEARTBEAT eq=" + DoubleToString(AccountEquity(), 2) + 
                " margin=" + DoubleToString(AccountMargin(), 2) + 
                " freemargin=" + DoubleToString(AccountFreeMargin(), 2) +
                " transport=" + transport +
                " ack_outbox_blocked=" + JsonBool(gAckOutboxBlocked) +
                " ack_outbox_pending=" + IntegerToString(AckOutboxPendingCount());
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
      ",\"ack_outbox_blocked\":" + JsonBool(gAckOutboxBlocked) +
      ",\"ack_outbox_pending\":" + IntegerToString(AckOutboxPendingCount()) +
      ",\"ack_outbox_staged\":" + IntegerToString(AckOutboxStagedCount()) +
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
      HttpPOST(ApiBase + TickPath(), tick, gBridgeApiKey);
      WarnAuthFailure("tick", LastBridgeHttpStatus());
   }
}

void OnTimer(){
   // Drain durable outcomes first. A remaining ACK fences command polling, so a
   // bridge/auth outage cannot cause the broker command to execute twice.
   ServiceAckOutbox(ACK_OUTBOX_REPLAY_PER_TIMER);
   manageCycle();
   heartbeat();
   static datetime lastStatusReport = 0;
   if(TimeCurrent() > lastStatusReport + 14) {
      reportBridgeStatus();
      lastStatusReport = TimeCurrent();
   }
   static datetime lastDashboardRefresh = 0;
   if(TimeCurrent() > lastDashboardRefresh + 4) {
      RefreshDashboardFromBridgeReady();
      lastDashboardRefresh = TimeCurrent();
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

   if(!AckOutboxAllowsCommandPolling()) return;

   string pollUrl = ApiBase + PollPath();
   string resp = HttpGET(pollUrl, gBridgeApiKey);
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
      if(!MathIsValidNumber(close_lots) || close_lots <= 0.0){
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
      if(!MathIsValidNumber(lots) || lots <= 0.0){
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
   
   // The command amount has already passed portfolio and risk approval. The EA
   // may reject it against the live broker contract, but it must never increase
   // or otherwise rewrite that approved economic size.
   double lots2=0.0;
   string lotReason="";
   if(!ValidateExactBrokerLots(brokerSym,lots,lots2,lotReason)){
      string lotFailure="entry_lots_not_exactly_executable:"+lotReason;
      UpdateDashboard("Order failed " + cmd + " " + logicalSym + "|" + lotFailure);
      post_report("ERR " + lotFailure + " sym=" + logicalSym);
      post_ack(
         signal_id, "failed", logicalSym, -1, 409, lotFailure,
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
         (double)(GetTickCount() - t_handle_start_ms), interop_mode
      );
      return;
   }

   // BUY/SELL commands must carry absolute, directionally safe protection.
   // Legacy tp_cash is intentionally not a substitute: no OrderSend is allowed
   // when either approved SL or TP is missing or invalid.
   double tp=0.0;
   double slNorm=0.0;
   string protectionReason="";
   if(!ValidateDirectionalEntryProtection(
      brokerSym,type,bid,ask,sl,tp_price_in,symDigits,
      slNorm,tp,protectionReason
   )){
      string protectionFailure="entry_protection_invalid:"+protectionReason;
      UpdateDashboard("Order failed " + cmd + " " + logicalSym + "|" + protectionFailure);
      post_report("ERR " + protectionFailure + " sym=" + logicalSym);
      post_ack(
         signal_id, "failed", logicalSym, -1, 412, protectionFailure,
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
         (double)(GetTickCount() - t_handle_start_ms), interop_mode
      );
      return;
   }
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
   string terminalFailure="";
   for(int attempt=0; attempt<3; attempt++){
      if(attempt > 0){
         Sleep(80);
         RefreshRates();
         ask = MarketInfo(brokerSym, MODE_ASK);
         bid = MarketInfo(brokerSym, MODE_BID);
      }
      // Prices can move between retries. Revalidate both protective orders
      // against the current quote immediately before every OrderSend.
      if(!ValidateDirectionalEntryProtection(
         brokerSym,type,bid,ask,sl,tp_price_in,symDigits,
         slNorm,tp,protectionReason
      )){
         err=412;
         terminalFailure="entry_protection_invalid:"+protectionReason;
         break;
      }
      px = (type==OP_BUY)?ask:bid;
      px = NormalizeDouble(px, symDigits);
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
      if(StringLen(terminalFailure)<=0) terminalFailure="order_send_failed";
      post_report("ERR order "+IntegerToString(err)+" "+terminalFailure);
      Print("OrderSend error: ", err);
      post_ack(
         signal_id, "failed", logicalSym, -1, err, terminalFailure,
         trace_id, t_py_signal_post_start, t_bridge_queued, t_bridge_delivered,
         t_ea_received, t_ea_exec_start, (double)TimeCurrent(),
         (double)(GetTickCount() - t_handle_start_ms), interop_mode
      );
      return; 
   }

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
             + "\"sl\":" + DoubleToString(OrderStopLoss(), odg) + ","
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
