//+------------------------------------------------------------------+
//|                                             BridgeVisualizer.mq4 |
//|                                  Copyright 2024, Trading Agent |
//|                                        https://www.google.com |
//+------------------------------------------------------------------+
#property copyright "Trading Agent"
#property link      "https://www.google.com"
#property version   "1.00"
#property strict
#property indicator_chart_window

#include <BridgeUtils.mqh>
#include <BridgeHttp.mqh>

input string ApiBase = "http://127.0.0.1:58710";
input int    PollMs  = 1000;      // Polling interval in ms
input string VisualPrefix = "BV_"; // Prefix for objects to avoid collisions

int OnInit() {
   EventSetTimer(1); // 1 second fixed for now, PollMs unused in timer logic but good for doc
   
   if(!InitBridgeHttp("MT4_Bridge_Vis")) {
      return(INIT_FAILED);
   }
   
   Print("Bridge Visualizer linked to ", Symbol());
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) {
   EventKillTimer();
   DeinitBridgeHttp();
   // Optional: Clean up objects on remove
   // ObjectsDeleteAll(0, VisualPrefix); 
   // Actually, maybe we want to keep them? Let's keep them for history.
   // Or provide an option? 
   // For now, let's NOT delete them so the user can see history even if they remove indicator.
}

int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[]) {
   return(rates_total);
}

void OnTimer() {
   string sym = Symbol();
   string url = ApiBase + "/v2/visuals?symbol=" + sym;
   string resp = HttpGET(url);
   
   if(StringLen(resp) > 2) {
      // Print("Visuals: ", resp);
      ParseAndDraw(resp);
   }
}

// Simple JSON Array Parser
// Expects: [{"type":...}, {"type":...}]
void ParseAndDraw(string json) {
   // 1. Remove outer brackets
   string content = json;
   StringReplace(content, "[", "");
   StringReplace(content, "]", "");
   
   // 2. Split by object delimiter "},{" 
   // Note: StringSplit doesn't handle multi-char well.
   // We'll iterate manually or replace "},{" with "}|{"
   StringReplace(content, "},{", "}|{");
   
   string items[];
   int n = StringSplit(content, '|', items);
   
   for(int i=0; i<n; i++) {
      string item = items[i];
      // Cleanup braces
      StringReplace(item, "{", "");
      StringReplace(item, "}", "");
      
      DrawItem(item);
   }
}

void DrawItem(string kv_str) {
   // Parse "key":"value", "key":123
   string parts[];
   int n = StringSplit(kv_str, ',', parts);
   
   string type="", side="", text="", color_str="";
   double price=0;
   datetime time=0;
   
   for(int i=0; i<n; i++) {
      string p = parts[i];
      string kv[];
      int idx = StringFind(p, ":");
      if(idx > 0) {
         string k = StringSubstr(p, 0, idx);
         string v = StringSubstr(p, idx+1);
         
         // Clean quotes
         StringReplace(k, "\"", "");
         StringReplace(v, "\"", "");
         
         // Trim
         k = StringTrim(k);
         v = StringTrim(v);
         
         if(k == "type") type = v;
         if(k == "side") side = v;
         if(k == "price") price = StringToDouble(v);
         if(k == "time") {
            // ISO string "2024-02-16T..." -> Datetime
            // MQL4 StringToTime handles "yyyy.mm.dd hh:mi"
            // We'll assume the python side sends something parseable or simple timestamp
            // Actually, best to use CurrentTime or parse properly.
            // Let's rely on Python sending a string that StringToTime supports or just use TimeCurrent if omitted
            time = StringToTime(v);
         }
         if(k == "text") text = v;
         if(k == "color") color_str = v;
      }
   }
   
   if(time == 0) time = TimeCurrent();
   
   if(type == "arrow") {
      string name = VisualPrefix + "Arrow_" + IntegerToString(time) + "_" + side;
      int objType = OBJ_ARROW;
      int arrowCode = (side == "BUY") ? 233 : 234; // Wingdings: 233=Up, 234=Down
      color c = (side == "BUY") ? clrLime : clrRed;
      
      if(color_str == "Green") c = clrLime;
      else if(color_str == "Red") c = clrRed;
      
      if(ObjectCreate(0, name, objType, 0, time, price)) {
         ObjectSetInteger(0, name, OBJPROP_ARROWCODE, arrowCode);
         ObjectSetInteger(0, name, OBJPROP_COLOR, c);
         ObjectSetInteger(0, name, OBJPROP_WIDTH, 2);
         ObjectSetString(0, name, OBJPROP_TEXT, side);
      }
   }
   else if(type == "label") {
      string name = VisualPrefix + "Label_" + IntegerToString(time);
      if(ObjectFind(0, name) >= 0) ObjectDelete(0, name); // Update existing if same timestamp?
      
      if(ObjectCreate(0, name, OBJ_TEXT, 0, time, price)) {
         ObjectSetString(0, name, OBJPROP_TEXT, text);
         ObjectSetInteger(0, name, OBJPROP_COLOR, clrWhite);
         ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
         ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LOWER);
      }
   }
}

string StringTrim(string str) {
   StringTrimLeft(str);
   StringTrimRight(str);
   return str;
}
