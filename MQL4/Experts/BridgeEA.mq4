#property strict
#include <BridgeUtils.mqh>

// IG MT4 Bridge EA
// Account: 96940 (BXAWM)
// Server: IG-LIVE2 for live, IG-DEMO for demo
// Uses 0.10 lot size for mini contracts

input string ApiBase = "http://127.0.0.1:5000";
input int    PollMs  = 1000;
input int    SlipPts = 20;
input int    Magic   = 246810;
input bool   UseIGMinis = true;  // Force 0.10 lot for IG mini contracts

double gCycleStartEq = 0.0;
double gCycleTargetCash = 0.0;
bool   gCycleActive = false;

int OnInit(){ 
   EventSetTimer(1); 
   Print("IG MT4 Bridge EA initialized");
   Print("Account: ", AccountNumber());
   Print("Server: ", AccountServer());
   Print("Mini mode: ", UseIGMinis ? "ON (0.10 lot)" : "OFF");
   return(INIT_SUCCEEDED); 
}

void OnDeinit(const int){ EventKillTimer(); }

void heartbeat(){
   string headers="", out="HEARTBEAT eq="+DoubleToString(AccountEquity(),2);
   char data[]; ArrayResize(data,StringLen(out)); StringToCharArray(out,data);
   char res[]; WebRequest("POST", ApiBase+"/report", headers, 1000, data, ArraySize(data), res, headers);
}

void OnTimer(){
   manageCycle();
   heartbeat();
   string headers="", resp=""; char post[], result[];
   int code = WebRequest("GET", ApiBase+"/poll", headers, PollMs, post, 0, result, headers);
   if(code==200){
      resp = CharArrayToString(result);
      if(StringLen(resp)>0) HandleCmd(resp);
   }
}

void HandleCmd(string line){
   string items[]; int n = StringSplit(line,';',items);
   string cmd="", sym=""; double lots=0, tp_cash=0, sl=0; int magic=Magic;
   for(int i=0;i<n;i++){
      string kv[]; if(StringSplit(items[i],'=',kv)!=2) continue;
      string k=StringTrim(kv[0]), v=StringTrim(kv[1]);
      if(k=="cmd") cmd=v;
      if(k=="symbol") sym=v;
      if(k=="lots") lots=StrToDouble(v);
      if(k=="tp_cash") tp_cash=StrToDouble(v);
      if(k=="sl") sl=StrToDouble(v);
      if(k=="magic") magic=(int)StrToInteger(v);
   }
   if(cmd=="CLOSE_ALL"){ CloseAll(); resetCycle(); report("CLOSE_ALL"); return; }
   if(cmd=="BUY" || cmd=="SELL") Execute(cmd, sym, lots, tp_cash, sl, magic);
}

void Execute(string cmd,string sym,double lots,double tp_cash,double sl,int magic){
   if(SymbolSelect(sym,true)==false){ report("ERR symbol "+sym); return; }
   RefreshRates();
   int type=(cmd=="BUY")?OP_BUY:OP_SELL;
   double px=(type==OP_BUY)?Ask:Bid;
   
   // For IG minis: enforce 0.10 lot if UseIGMinis is true and lots is 0 or not specified
   double lots2;
   if(UseIGMinis && (lots<=0.0)){
      lots2 = IGMiniLot(sym);  // Force 0.10 for IG minis
   } else if(lots<=0.0){
      lots2 = MinLot(sym);  // Use minimum lot from broker
   } else {
      lots2 = RoundLot(sym,lots);  // Use specified lot
   }

   double tp=0;
   if(tp_cash>0) tp = TpFromCash(sym, type, px, lots2, tp_cash);

   int ticket = OrderSend(sym, type, lots2, px, SlipPts, sl, tp, "ELBridge", magic, 0,
                          (type==OP_BUY)?clrGreen:clrRed);
   if(ticket<0){ report("ERR order "+IntegerToString(GetLastError())); return; }

   if(!gCycleActive){
      gCycleStartEq = AccountEquity();
      gCycleTargetCash = gCycleStartEq * 0.01;  // basket 1% target
      gCycleActive = true;
      report("CYCLE_START eq="+DoubleToString(gCycleStartEq,2)+" target="+DoubleToString(gCycleTargetCash,2));
   }
   report("OK "+cmd+" "+sym+" ticket="+IntegerToString(ticket)+" lots="+DoubleToString(lots2,2)+" tp_cash="+DoubleToString(tp_cash,2));
}

void manageCycle(){
   if(!gCycleActive) return;
   double eq = AccountEquity();
   if(eq >= gCycleStartEq + gCycleTargetCash){
      CloseAll();
      report("CYCLE_TARGET_HIT eq="+DoubleToString(eq,2)+" profit="+DoubleToString(eq-gCycleStartEq,2));
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
            report("ERR close "+IntegerToString(GetLastError()));
   }
}

void report(string msg){
   Print(msg);
   string headers=""; char data[]; ArrayResize(data,StringLen(msg)); StringToCharArray(msg,data);
   char res[]; WebRequest("POST", ApiBase+"/report", headers, 1000, data, ArraySize(data), res, headers);
}
