// BridgeUtils.mqh - Helper functions for IG MT4 Bridge EA
// Supports IG mini contracts (0.10 lot size)

double MinLot(string sym){
   double minlot=MarketInfo(sym,MODE_MINLOT);
   double step=MarketInfo(sym,MODE_LOTSTEP);
   if(!MathIsValidNumber(minlot) || !MathIsValidNumber(step) || minlot<=0.0 || step<=0.0) return(0.0);
   int d=(int)MathRound(MathLog10(1.0/step));
   return NormalizeDouble(minlot,d);
}

double RoundLot(string sym,double lots){
   // Kept for source compatibility with legacy callers. "Round" is now
   // deliberately fail-closed: an approved amount is returned unchanged only
   // when the live broker contract says it is exactly executable.
   double exactLots=0.0;
   string reason="";
   if(!ValidateExactBrokerLots(sym,lots,exactLots,reason)) return(0.0);
   return(exactLots);
}

int LotDigitsForStep(double step){
   if(!MathIsValidNumber(step) || step<=0.0) return(0);
   int digits=0;
   double scaled=step;
   while(digits<8 && MathAbs(scaled-MathRound(scaled))>1e-8){
      scaled*=10.0;
      digits++;
   }
   return digits;
}

double LotStepTolerance(double step){
   if(!MathIsValidNumber(step) || step<=0.0) return(1e-9);
   return MathMax(1e-9,MathAbs(step)*1e-7);
}

// Validate a risk-approved lot amount against the broker contract without
// changing its economic size. The caller may use exactLots only after this
// succeeds. Any amount that would require rounding, including an upward clamp
// to MODE_MINLOT, is rejected.
bool ValidateExactBrokerLots(string sym,double requestedLots,double &exactLots,string &reason){
   exactLots=0.0;
   reason="";
   if(!MathIsValidNumber(requestedLots) || requestedLots<=0.0){
      reason="non_positive_or_nonfinite";
      return(false);
   }

   double minlot=MarketInfo(sym,MODE_MINLOT);
   double maxlot=MarketInfo(sym,MODE_MAXLOT);
   double step=MarketInfo(sym,MODE_LOTSTEP);
   if(
      !MathIsValidNumber(minlot) || !MathIsValidNumber(maxlot) || !MathIsValidNumber(step) ||
      minlot<=0.0 || maxlot<=0.0 || step<=0.0 || minlot>maxlot
   ){
      reason="broker_lot_contract_invalid";
      return(false);
   }

   double tolerance=LotStepTolerance(step);
   if(requestedLots<minlot-tolerance){
      reason="below_broker_min";
      return(false);
   }
   if(requestedLots>maxlot+tolerance){
      reason="above_broker_max";
      return(false);
   }

   double units=requestedLots/step;
   double nearestUnits=MathRound(units);
   if(!MathIsValidNumber(units) || MathAbs(units-nearestUnits)>1e-7){
      reason="off_broker_step";
      return(false);
   }

   int digits=LotDigitsForStep(step);
   double normalized=NormalizeDouble(nearestUnits*step,digits);
   if(!MathIsValidNumber(normalized) || normalized<=0.0){
      reason="normalized_lots_invalid";
      return(false);
   }
   if(normalized>requestedLots){
      reason="would_round_up";
      return(false);
   }
   if(MathAbs(normalized-requestedLots)>tolerance){
      reason="not_exactly_executable";
      return(false);
   }
   if(normalized<minlot-tolerance || normalized>maxlot+tolerance){
      reason="outside_broker_bounds";
      return(false);
   }

   exactLots=normalized;
   return(true);
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
