"use client"

import { Card } from "@/components/ui/card"

export function OptionsChain() {
  const options = [
    { strike: 1.08, callIV: 12.8, putIV: 13.2, callDelta: 0.75, putDelta: -0.25 },
    { strike: 1.085, callIV: 12.5, putIV: 12.9, callDelta: 0.5, putDelta: -0.5 },
    { strike: 1.09, callIV: 12.3, putIV: 12.6, callDelta: 0.25, putDelta: -0.75 },
  ]

  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground mb-4">Options Chain (EUR/USD)</h3>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border">
              <th className="text-left py-2 text-xs font-medium text-muted-foreground">Strike</th>
              <th className="text-right py-2 text-xs font-medium text-muted-foreground">Call IV</th>
              <th className="text-right py-2 text-xs font-medium text-muted-foreground">Call Δ</th>
              <th className="text-right py-2 text-xs font-medium text-muted-foreground">Put IV</th>
              <th className="text-right py-2 text-xs font-medium text-muted-foreground">Put Δ</th>
            </tr>
          </thead>
          <tbody>
            {options.map((opt, i) => (
              <tr key={i} className="border-b border-border">
                <td className="py-2 font-medium text-foreground">{opt.strike}</td>
                <td className="py-2 text-right text-foreground">{opt.callIV}%</td>
                <td className="py-2 text-right text-foreground">{opt.callDelta}</td>
                <td className="py-2 text-right text-foreground">{opt.putIV}%</td>
                <td className="py-2 text-right text-foreground">{opt.putDelta}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
