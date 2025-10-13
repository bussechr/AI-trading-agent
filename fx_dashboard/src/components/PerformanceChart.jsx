import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { TrendingUp } from 'lucide-react'

export default function PerformanceChart({ reports }) {
  // Extract equity data from HEARTBEAT reports
  const equityData = reports
    .filter(r => r.message && r.message.includes('HEARTBEAT'))
    .map(r => {
      const match = r.message.match(/eq=([\d.]+)/)
      if (match && r.time) {
        return {
          time: new Date(r.time).toLocaleTimeString(),
          equity: parseFloat(match[1])
        }
      }
      return null
    })
    .filter(Boolean)
    .slice(-50) // Last 50 data points

  if (equityData.length === 0) {
    return (
      <div className="bg-slate-800 rounded-lg p-6 mb-6 border border-slate-700">
        <div className="flex items-center space-x-2 mb-4">
          <TrendingUp className="w-5 h-5 text-blue-400" />
          <h2 className="text-xl font-semibold text-white">Equity Curve</h2>
        </div>
        <div className="text-center py-12 text-slate-400">
          Waiting for equity data...
        </div>
      </div>
    )
  }

  return (
    <div className="bg-slate-800 rounded-lg p-6 mb-6 border border-slate-700">
      <div className="flex items-center space-x-2 mb-4">
        <TrendingUp className="w-5 h-5 text-blue-400" />
        <h2 className="text-xl font-semibold text-white">Equity Curve</h2>
      </div>
      
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={equityData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis 
            dataKey="time" 
            stroke="#94a3b8"
            tick={{ fill: '#94a3b8', fontSize: 12 }}
          />
          <YAxis 
            stroke="#94a3b8"
            tick={{ fill: '#94a3b8', fontSize: 12 }}
            domain={['auto', 'auto']}
          />
          <Tooltip 
            contentStyle={{ 
              backgroundColor: '#1e293b', 
              border: '1px solid #475569',
              borderRadius: '8px'
            }}
            labelStyle={{ color: '#e2e8f0' }}
          />
          <Line 
            type="monotone" 
            dataKey="equity" 
            stroke="#3b82f6" 
            strokeWidth={2}
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
