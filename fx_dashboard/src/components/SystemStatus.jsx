import { Activity, Wifi, WifiOff } from 'lucide-react'

export default function SystemStatus({ state, isConnected }) {
  const getStatusColor = () => {
    if (!isConnected) return 'bg-red-500'
    if (state?.system_status === 'connected') return 'bg-green-500'
    return 'bg-yellow-500'
  }

  const getStatusText = () => {
    if (!isConnected) return 'Disconnected'
    if (state?.system_status === 'connected') return 'Connected'
    return 'Starting'
  }

  return (
    <div className="bg-slate-800 rounded-lg p-6 mb-6 border border-slate-700">
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-4">
          {isConnected ? (
            <Wifi className="w-6 h-6 text-green-400" />
          ) : (
            <WifiOff className="w-6 h-6 text-red-400" />
          )}
          <div>
            <h3 className="text-lg font-semibold text-white">System Status</h3>
            <p className="text-sm text-slate-400">
              Last heartbeat: {state?.last_heartbeat 
                ? new Date(state.last_heartbeat).toLocaleTimeString() 
                : 'N/A'}
            </p>
          </div>
        </div>
        
        <div className="flex items-center space-x-3">
          <div className={`w-3 h-3 rounded-full ${getStatusColor()} animate-pulse`} />
          <span className="text-white font-medium">{getStatusText()}</span>
        </div>
      </div>

      {state && (
        <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-4">
          <div className="bg-slate-700/50 rounded p-3">
            <p className="text-xs text-slate-400 mb-1">Signals Sent</p>
            <p className="text-2xl font-bold text-white">{state.signals_sent || 0}</p>
          </div>
          <div className="bg-slate-700/50 rounded p-3">
            <p className="text-xs text-slate-400 mb-1">Trades Executed</p>
            <p className="text-2xl font-bold text-white">{state.trades_executed || 0}</p>
          </div>
          <div className="bg-slate-700/50 rounded p-3">
            <p className="text-xs text-slate-400 mb-1">Active Decisions</p>
            <p className="text-2xl font-bold text-white">
              {state.agent_decisions?.length || 0}
            </p>
          </div>
          <div className="bg-slate-700/50 rounded p-3">
            <p className="text-xs text-slate-400 mb-1">Cycle Status</p>
            <p className="text-lg font-bold text-white">
              {state.cycle_active ? 
                <span className="text-green-400">Active</span> : 
                <span className="text-slate-400">Idle</span>
              }
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
