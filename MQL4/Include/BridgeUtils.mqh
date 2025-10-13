// BridgeUtils.mqh - Helper functions for IG MT4 Bridge EA
// Supports IG mini contracts (0.10 lot size)

double MinLot(string sym){
   double minlot=MarketInfo(sym,MODE_MINLOT);
   double step=MarketInfo(sym,MODE_LOTSTEP);
   int d=(int)MathRound(MathLog10(1.0/step));
   return NormalizeDouble(minlot,d);
}

double RoundLot(string sym,double lots){
   double step=MarketInfo(sym,MODE_LOTSTEP);
   int d=(int)MathRound(MathLog10(1.0/step));
   double q=MathFloor(lots/step+1e-8)*step;
   double minlot=MarketInfo(sym,MODE_MINLOT);
   if(q<minlot) q=minlot;
   return NormalizeDouble(q,d);
}

// For IG mini contracts: enforce 0.10 lot size
double IGMiniLot(string sym){
   double mini = 0.10;  // IG mini contract size
   return RoundLot(sym, mini);
}

double TpFromCash(string sym,int type,double entry,double lots,double cash){
   double tv=MarketInfo(sym,MODE_TICKVALUE);
   double ts=MarketInfo(sym,MODE_TICKSIZE);
   if(tv<=0 || ts<=0) return(0);
   double ticks=cash/(tv*lots);
   double dpx=ticks*ts;
   return (type==OP_BUY)? entry+dpx : entry-dpx;
}
