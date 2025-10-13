import { TrendingUp, TrendingDown } from 'lucide-react'

export default function ActiveDecisions({ decisions }) {
  if (!decisions || decisions.length === 0) {
    return (
      <div className="bg-slate-800 rounded-lg p-6 mb-6 border border-slate-700">
        <h2 className="text-xl font-semibold text-white mb-4">Active Decisions</h2>
        <div className="text-center py-8 text-slate-400">
          No active trading decisions
        </div>
      </div>
    )
  }

  return (
    <div className="bg-slate-800 rounded-lg p-6 mb-6 border border-slate-700">
      <h2 className="text-xl font-semibold text-white mb-4">
        Active Decisions ({decisions.length})
      </h2>
      
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {decisions.map((decision, idx) => (
          <div 
            key={idx}
            className={`rounded-lg p-4 border-2 ${
              decision.side === 'BUY' 
                ? 'bg-green-900/20 border-green-500/50' 
                : 'bg-red-900/20 border-red-500/50'
            }`}
          >
            <div className="flex items-center justify-between mb-3">
              <span className="text-lg font-bold text-white">
                {decision.symbol}
              </span>
              <div className={`flex items-center space-x-1 px-2 py-1 rounded ${
                decision.side === 'BUY' 
                  ? 'bg-green-500/20 text-green-300' 
                  : 'bg-red-500/20 text-red-300'
              }`}>
                {decision.side === 'BUY' ? (
                  <TrendingUp className="w-4 h-4" />
                ) : (
                  <TrendingDown className="w-4 h-4" />
                )}
                <span className="text-sm font-semibold">{decision.side}</span>
              </div>
            </div>
            
            <div className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span className="text-slate-400">Score:</span>
                <span className="text-white font-semibold">
                  {decision.score?.toFixed(3)}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Price:</span>
                <span className="text-white font-semibold">
                  {decision.price?.toFixed(5)}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Target:</span>
                <span className="text-white font-semibold">
                  {(decision.target_pct * 100)?.toFixed(2)}%
                </span>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
