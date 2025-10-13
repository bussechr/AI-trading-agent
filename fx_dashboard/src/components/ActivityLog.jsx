import { ScrollText } from 'lucide-react'

export default function ActivityLog({ reports }) {
  const recentReports = reports.slice(-20).reverse()

  const getMessageColor = (msg) => {
    if (msg.includes('ERR') || msg.includes('ERROR')) return 'text-red-400'
    if (msg.includes('OK')) return 'text-green-400'
    if (msg.includes('CYCLE_TARGET_HIT')) return 'text-blue-400'
    if (msg.includes('CYCLE_START')) return 'text-yellow-400'
    return 'text-slate-300'
  }

  return (
    <div className="bg-slate-800 rounded-lg p-6 border border-slate-700">
      <div className="flex items-center space-x-2 mb-4">
        <ScrollText className="w-5 h-5 text-slate-400" />
        <h2 className="text-xl font-semibold text-white">Activity Log</h2>
      </div>
      
      <div className="space-y-2 max-h-96 overflow-y-auto">
        {recentReports.length === 0 ? (
          <div className="text-center py-8 text-slate-400">
            No activity yet
          </div>
        ) : (
          recentReports.map((report, idx) => (
            <div 
              key={idx} 
              className="flex items-start space-x-3 p-2 hover:bg-slate-700/30 rounded text-sm"
            >
              <span className="text-xs text-slate-500 min-w-[80px]">
                {report.time ? new Date(report.time).toLocaleTimeString() : 'N/A'}
              </span>
              <span className={`font-mono ${getMessageColor(report.message)}`}>
                {report.message}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
