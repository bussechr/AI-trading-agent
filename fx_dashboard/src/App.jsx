import { useState, useEffect } from 'react'
import SystemStatus from './components/SystemStatus'
import EquityCard from './components/EquityCard'
import ActiveDecisions from './components/ActiveDecisions'
import RecentSignals from './components/RecentSignals'
import ActivityLog from './components/ActivityLog'
import PerformanceChart from './components/PerformanceChart'

const API_BASE = 'http://127.0.0.1:5000'

function App() {
  const [state, setState] = useState(null)
  const [reports, setReports] = useState([])
  const [isConnected, setIsConnected] = useState(false)

  useEffect(() => {
    // Poll for state updates every 2 seconds
    const interval = setInterval(async () => {
      try {
        const [stateRes, reportsRes] = await Promise.all([
          fetch(`${API_BASE}/state`),
          fetch(`${API_BASE}/reports`)
        ])
        
        if (stateRes.ok) {
          const stateData = await stateRes.json()
          setState(stateData)
          setIsConnected(true)
        }
        
        if (reportsRes.ok) {
          const reportsData = await reportsRes.json()
          setReports(reportsData.reports || [])
        }
      } catch (error) {
        console.error('Failed to fetch data:', error)
        setIsConnected(false)
      }
    }, 2000)

    return () => clearInterval(interval)
  }, [])

  return (
    <div className="min-h-screen bg-slate-900 p-6">
      <div className="max-w-7xl mx-auto">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-4xl font-bold text-white mb-2">
            FX Trading Monitor
          </h1>
          <p className="text-slate-400">
            Real-time monitoring for IG MT4 EL momentum strategy
          </p>
        </div>

        {/* System Status */}
        <SystemStatus state={state} isConnected={isConnected} />

        {/* Main Grid */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6">
          {/* Equity Card */}
          <EquityCard state={state} />
          
          {/* Cycle Status */}
          <div className="lg:col-span-2">
            <div className="bg-slate-800 rounded-lg p-6 border border-slate-700">
              <h2 className="text-xl font-semibold text-white mb-4">
                Trading Cycle
              </h2>
              {state?.cycle_active ? (
                <div className="space-y-3">
                  <div className="flex justify-between items-center">
                    <span className="text-slate-400">Cycle Start Equity:</span>
                    <span className="text-white font-semibold">
                      ${state.cycle_start_equity?.toFixed(2) || '0.00'}
                    </span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-slate-400">Current Equity:</span>
                    <span className="text-white font-semibold">
                      ${state.equity?.toFixed(2) || '0.00'}
                    </span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-slate-400">P&L:</span>
                    <span className={`font-semibold ${
                      (state.equity - state.cycle_start_equity) >= 0 
                        ? 'text-green-400' 
                        : 'text-red-400'
                    }`}>
                      ${((state.equity - state.cycle_start_equity) || 0).toFixed(2)}
                    </span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-slate-400">Target (+1%):</span>
                    <span className="text-white font-semibold">
                      ${state.cycle_target?.toFixed(2) || '0.00'}
                    </span>
                  </div>
                  <div className="mt-4">
                    <div className="w-full bg-slate-700 rounded-full h-2">
                      <div 
                        className="bg-blue-500 h-2 rounded-full transition-all duration-300"
                        style={{
                          width: `${Math.min(100, Math.max(0, 
                            ((state.equity - state.cycle_start_equity) / state.cycle_target) * 100
                          ))}%`
                        }}
                      />
                    </div>
                  </div>
                </div>
              ) : (
                <div className="text-center py-8 text-slate-400">
                  No active trading cycle
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Performance Chart */}
        <PerformanceChart reports={reports} />

        {/* Active Decisions */}
        <ActiveDecisions decisions={state?.agent_decisions || []} />

        {/* Recent Signals */}
        <RecentSignals 
          signalsSent={state?.signals_sent || 0}
          tradesExecuted={state?.trades_executed || 0}
          lastSignal={state?.last_signal}
        />

        {/* Activity Log */}
        <ActivityLog reports={reports} />
      </div>
    </div>
  )
}

export default App
