import { Send } from 'lucide-react'

export default function RecentSignals({ signalsSent, tradesExecuted, lastSignal }) {
  return (
    <div className="bg-slate-800 rounded-lg p-6 mb-6 border border-slate-700">
      <div className="flex items-center space-x-2 mb-4">
        <Send className="w-5 h-5 text-blue-400" />
        <h2 className="text-xl font-semibold text-white">Signals</h2>
      </div>
      
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-slate-700/50 rounded p-4">
          <p className="text-sm text-slate-400 mb-1">Total Sent</p>
          <p className="text-2xl font-bold text-white">{signalsSent}</p>
        </div>
        
        <div className="bg-slate-700/50 rounded p-4">
          <p className="text-sm text-slate-400 mb-1">Executed</p>
          <p className="text-2xl font-bold text-green-400">{tradesExecuted}</p>
        </div>
        
        <div className="bg-slate-700/50 rounded p-4">
          <p className="text-sm text-slate-400 mb-1">Success Rate</p>
          <p className="text-2xl font-bold text-white">
            {signalsSent > 0 
              ? ((tradesExecuted / signalsSent) * 100).toFixed(0) 
              : 0}%
          </p>
        </div>
      </div>

      {lastSignal && (
        <div className="mt-4 p-4 bg-slate-700/30 rounded">
          <p className="text-xs text-slate-400 mb-2">Last Signal</p>
          <div className="flex justify-between items-center">
            <div>
              <span className="text-white font-semibold">
                {lastSignal.data?.cmd} {lastSignal.data?.symbol}
              </span>
              {lastSignal.data?.tp_cash && (
                <span className="text-slate-400 text-sm ml-2">
                  TP: ${lastSignal.data.tp_cash.toFixed(2)}
                </span>
              )}
            </div>
            <span className="text-xs text-slate-400">
              {new Date(lastSignal.time).toLocaleTimeString()}
            </span>
          </div>
        </div>
      )}
    </div>
  )
}
