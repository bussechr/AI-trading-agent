import { DollarSign, TrendingUp, TrendingDown } from 'lucide-react'

export default function EquityCard({ state }) {
  const equity = state?.equity || 0
  const cycleStartEquity = state?.cycle_start_equity || equity
  const pnl = equity - cycleStartEquity
  const pnlPercent = cycleStartEquity > 0 ? (pnl / cycleStartEquity) * 100 : 0

  return (
    <div className="bg-gradient-to-br from-blue-600 to-blue-800 rounded-lg p-6 border border-blue-500/50">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-white">Account Equity</h2>
        <DollarSign className="w-6 h-6 text-blue-200" />
      </div>
      
      <div className="space-y-3">
        <div>
          <p className="text-3xl font-bold text-white">
            ${equity.toFixed(2)}
          </p>
        </div>
        
        {state?.cycle_active && (
          <div className="flex items-center space-x-2 text-sm">
            {pnl >= 0 ? (
              <TrendingUp className="w-4 h-4 text-green-300" />
            ) : (
              <TrendingDown className="w-4 h-4 text-red-300" />
            )}
            <span className={`font-semibold ${pnl >= 0 ? 'text-green-300' : 'text-red-300'}`}>
              {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)} ({pnlPercent >= 0 ? '+' : ''}{pnlPercent.toFixed(2)}%)
            </span>
          </div>
        )}
        
        <div className="pt-2 border-t border-blue-400/30">
          <p className="text-xs text-blue-200">
            {state?.cycle_active ? 'Cycle in progress' : 'Waiting for signals'}
          </p>
        </div>
      </div>
    </div>
  )
}
