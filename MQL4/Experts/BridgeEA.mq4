#property strict
#include <BridgeUtils.mqh>
#include <BridgeHttp.mqh> // Shared WinInet Logic

// MT4 Bridge EA - WinInet Version
// Account: 833602
// Server: Demo Forex USD (1:200)

input string ApiBase = "http://127.0.0.1:58710";
input int    PollMs  = 1000;
input int    SlipPts = 20;
input int    Magic   = 246810;
input bool   UseIGMinis = true;

double gCycleStartEq = 0.0;
double gCycleTargetCash = 0.0;
bool   gCycleActive = false;

string StringTrim(string str) {
   StringTrimLeft(str);
   StringTrimRight(str);
   return str;
}

int OnInit(){ 
   EventSetTimer(1); 
   Print("MT4 Bridge EA (WinInet) initialized");
   Print("ApiBase: ", ApiBase);
   
   // Initialize Shared WinInet Session
   if(!InitBridgeHttp("MT4_Bridge_EA")) {
       return(INIT_FAILED);
   }
   
   // Show initial status
   UpdateDashboard("WAITING FOR AGENT...|Starting Python Bridge...");
   ChartRedraw(0);
   
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
   HttpPOST(ApiBase + "/report", msg);
}

void heartbeat(){
   string out = "HEARTBEAT eq=" + DoubleToString(AccountEquity(), 2) + 
                " margin=" + DoubleToString(AccountMargin(), 2) + 
                " freemargin=" + DoubleToString(AccountFreeMargin(), 2);
   post_report(out);
}

void OnTick(){
   // Moved to OnTimer for consistent updates
}

void broadcastTick() {
   string sym = Symbol();
   // Use broker-reported spread (points) — avoids float precision issues
   int spread_pts = (int)MarketInfo(sym, MODE_SPREAD);
   double spread_pips = (double)spread_pts;
   
   // Adjust for 3/5 digit brokers (points to pips)
   if(Digits == 3 || Digits == 5) spread_pips /= 10.0;
   
   string tick = "{\"symbol\": \"" + sym + 
                 "\", \"bid\": " + DoubleToString(Bid, Digits) + 
                 ", \"ask\": " + DoubleToString(Ask, Digits) + 
                 ", \"spread\": " + DoubleToString(spread_pips, 1) + 
                 ", \"digits\": " + IntegerToString(Digits) + 
                 ", \"spread_pts\": " + IntegerToString(spread_pts) + "}";
   
   Print("Tick: ", tick); // DEBUG: Verify data is live
   HttpPOST(ApiBase + "/tick", tick);
}

void OnTimer(){
   manageCycle();
   heartbeat();
   broadcastTick(); // Send 1 tick per second
   
   static datetime lastPosReport = 0;
   if(TimeCurrent() > lastPosReport + 4) { // Every 5s
      SendPositions();
      lastPosReport = TimeCurrent();
   }

   string resp = HttpGET(ApiBase + "/poll");
   if(StringLen(resp) > 0) {
      Print("[BRIDGE] Command received: ", resp);
      HandleCmd(resp);
   }
}

void HandleCmd(string line){
   string items[]; int n = StringSplit(line,';',items);
   string cmd="", sym=""; double lots=0, tp_cash=0, tp_price=0, sl=0; int magic=Magic;
   for(int i=0;i<n;i++){
      string kv[]; if(StringSplit(items[i],'=',kv)!=2) continue;
      string k=StringTrim(kv[0]), v=StringTrim(kv[1]);
      if(k=="cmd") cmd=v;
      if(k=="symbol") sym=v;
      if(k=="lots") lots=StrToDouble(v);
      if(k=="tp_cash") tp_cash=StrToDouble(v);
      if(k=="tp_price") tp_price=StrToDouble(v);
      if(k=="sl") sl=StrToDouble(v);
      if(k=="magic") magic=(int)StrToInteger(v);
   }
   if(cmd=="CLOSE_ALL"){ CloseAll(); resetCycle(); post_report("CLOSE_ALL_OK"); return; }
   if(cmd=="INFO"){ 
      string thought="";
      // Parse thought
      for(int i=0;i<n;i++){
         string item = items[i];
         int idx = StringFind(item, "=");
         if(idx < 0) continue;
         string k = StringTrim(StringSubstr(item, 0, idx));
         string v = StringSubstr(item, idx + 1);
         if(k=="thought") thought=v;
      }
      UpdateDashboard(thought);
      return; 
   }
   if(cmd=="BUY" || cmd=="SELL") Execute(cmd, sym, lots, tp_cash, tp_price, sl, magic);
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
   
   // --- PARSING ---
   string lines[]; 
   int n = StringSplit(text, '|', lines);
   int totalHeight = (n * ROW_HEIGHT) + HDR_HEIGHT + (PADDING * 2);
   
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
   
   if(n > 0) {
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

   for(int i=0; i<n; i++) {
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
      string s = lines[i];
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
   for(int k=n; k<20; k++) {
      string delName = "BridgeHUD_Txt_" + IntegerToString(k);
      if(ObjectFind(0, delName) >= 0) ObjectDelete(0, delName);
   }
   
   ChartRedraw(0);
}

void Execute(string cmd,string sym,double lots,double tp_cash,double tp_price_in,double sl,int magic){
   UpdateDashboard("Executing " + cmd + "...");
   if(SymbolSelect(sym,true)==false){ post_report("ERR symbol "+sym); return; }
   RefreshRates();
   int type=(cmd=="BUY")?OP_BUY:OP_SELL;
   double px=(type==OP_BUY)?Ask:Bid;
   
   double lots2;
   if(UseIGMinis && (lots<=0.0)){
      lots2 = IGMiniLot(sym);
   } else if(lots<=0.0){
      lots2 = MinLot(sym);
   } else {
      lots2 = RoundLot(sym,lots);
   }

   // TP: Use absolute price if provided, otherwise convert from cash
   double tp=0;
   if(tp_price_in > 0) {
      tp = tp_price_in; // Absolute price from Python agent
   } else if(tp_cash > 0) {
      tp = TpFromCash(sym, type, px, lots2, tp_cash); // Legacy cash conversion
   }

   int ticket = OrderSend(sym, type, lots2, px, SlipPts, sl, tp, "ELBridge", magic, 0, (type==OP_BUY)?clrGreen:clrRed);
   if(ticket<0){ 
      int err = GetLastError();
      post_report("ERR order "+IntegerToString(err)); 
      Print("OrderSend error: ", err);
      return; 
   }

   if(!gCycleActive){
      gCycleStartEq = AccountEquity();
      gCycleTargetCash = gCycleStartEq * 0.01;
      gCycleActive = true;
      post_report("CYCLE_START eq="+DoubleToString(gCycleStartEq,2)+" target="+DoubleToString(gCycleTargetCash,2));
   }
   post_report("OK "+cmd+" "+sym+" ticket="+IntegerToString(ticket)+" lots="+DoubleToString(lots2,2));
}

void manageCycle(){
   if(!gCycleActive) return;
   double eq = AccountEquity();
   if(eq >= gCycleStartEq + gCycleTargetCash){
      CloseAll();
      post_report("CYCLE_TARGET_HIT eq="+DoubleToString(eq,2)+" profit="+DoubleToString(eq-gCycleStartEq,2));
      resetCycle();
   }
}

void resetCycle(){ gCycleActive=false; gCycleStartEq=0; gCycleTargetCash=0; }

void CloseAll(){
   for(int i=OrdersTotal()-1;i>=0;i--){
      if(!OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(OrderMagicNumber()!=Magic) continue;
      int ty=OrderType(); double px=(ty==OP_BUY)?Bid:Ask;
      if(ty==OP_BUY || ty==OP_SELL)
         if(!OrderClose(OrderTicket(), OrderLots(), px, SlipPts))
            post_report("ERR close "+IntegerToString(GetLastError()));
   }
}

void SendPositions() {
   string list = "POSITIONS";
   int cnt = 0;
   for(int i=0; i<OrdersTotal(); i++) {
       if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) {
           if(OrderMagicNumber() == Magic && (OrderType()==OP_BUY || OrderType()==OP_SELL)) {
               // Format: symbol=EURUSD,lots=0.1,profit=10.5;...
               // Using simpler format for parsing: symbol=EURUSD:lots=0.1
               // Or comma separated list of dicts?
               // Let's use semi-colon separated items, comma separated fields
               // POSITIONS item1;item2
               string item = "symbol=" + OrderSymbol() + ",lots=" + DoubleToString(OrderLots(), 2) + ",profit=" + DoubleToString(OrderProfit(), 2);
               list += " " + item;
               cnt++;
           }
       }
   }
   if(cnt == 0) list += " NONE";
   post_report(list);
}

void report(string msg){ post_report(msg); }

void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam) {
   // Prevent event bubbling/crashes for clicks
   if(id == CHARTEVENT_OBJECT_CLICK) {
      if(StringFind(sparam, "BridgeHUD") >= 0) return;
   }
}
