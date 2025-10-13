#property strict
input string ApiBase = "http://127.0.0.1:5000";

// IG FX pairs that have mini contracts
string roots[] = {
   // Majors
   "AUDUSD", "EURCHF", "EURGBP", "EURJPY", "EURUSD", "GBPEUR", "GBPJPY", "GBPUSD", 
   "USDCAD", "USDCHF", "USDJPY", "USDHKD",
   // Minors
   "CADCHF", "CADJPY", "CHFJPY", "EURCAD", "EURSGD", "EURZAR", "GBPCAD", "GBPCHF", 
   "GBPSGD", "GBPZAR", "SGDJPY", "USDSGD", "USDZAR",
   // Australasian
   "AUDCAD", "AUDCHF", "AUDEUR", "AUDGBP", "AUDJPY", "AUDNZD", "AUDSGD", "EURAUD", 
   "EURNZD", "GBPAUD", "GBPNZD", "NZDAUD", "NZDCHF", "NZDEUR", "NZDGBP", "NZDJPY", 
   "NZDUSD", "NZDCAD",
   // Scandinavian
   "CADNOK", "CHFNOK", "EURDKK", "EURNOK", "EURSEK", "GBPDKK", "GBPNOK", "GBPSEK", 
   "NOKSEK", "USDDKK", "USDNOK", "USDSEK",
   // Exotics
   "CHFHUF", "EURCZK", "EURHUF", "EURILS", "EURMXN", "EURPLN", "EURTRY", "GBPCZK", 
   "GBPHUF", "GBPILS", "GBPMXN", "GBPPLN", "GBPTRY", "MXNJPY", "NOKJPY", "PLNJPY", 
   "SEKJPY", "TRYJPY", "USDCZK", "USDHUF", "USDILS", "USDMXN", "USDPLN", "USDTRY"
};

bool hasRoot(string s){
   string u=StringToUpper(s);
   for(int i=0;i<ArraySize(roots);i++) {
      if(StringFind(u, roots[i], 0)>=0) return true;
   }
   return false;
}

void post(string msg){
   string headers=""; char data[]; ArrayResize(data,StringLen(msg)); StringToCharArray(msg,data);
   char res[]; WebRequest("POST", ApiBase+"/report", headers, 1000, data, ArraySize(data), res, headers);
}

int OnInit(){
   Print("=== IG MT4 Symbol Scanner ===");
   Print("Account: ", AccountNumber());
   Print("Server: ", AccountServer());
   Print("Scanning for IG FX symbols...");
   
   int total = SymbolsTotal(true);
   int found = 0;
   
   for(int i=0;i<total;i++){
      string s = SymbolName(i, true);
      if(!hasRoot(s)) continue;
      
      double minlot=MarketInfo(s, MODE_MINLOT);
      double step=MarketInfo(s, MODE_LOTSTEP);
      double tv=MarketInfo(s, MODE_TICKVALUE);
      double spread=MarketInfo(s, MODE_SPREAD);
      
      string info = StringFormat("IG_FX %s minlot=%.2f step=%.2f tickvalue=%.2f spread=%.1f", 
                                 s, minlot, step, tv, spread);
      Print(info);
      post(info);
      found++;
   }
   
   Print("Found ", found, " IG FX symbols");
   post("SCAN_COMPLETE found="+IntegerToString(found));
   
   return(INIT_SUCCEEDED);
}

void OnTick(){}
