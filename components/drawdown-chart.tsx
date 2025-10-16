"use client"

import { Card } from "@/components/ui/card"
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"

const data = Array.from({ length: 30 }, (_, i) => ({
  day: i + 1,
  drawdown: -Math.random() * 500,
}))

export function DrawdownChart() {
  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground mb-4">Drawdown Analysis</h3>

      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data}>
          <XAxis dataKey="day" stroke="hsl(var(--muted-foreground))" fontSize={12} tickLine={false} axisLine={false} />
          <YAxis
            stroke="hsl(var(--muted-foreground))"
            fontSize={12}
            tickLine={false}
            axisLine={false}
            tickFormatter={(value) => `$${value}`}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "hsl(var(--card))",
              border: "1px solid hsl(var(--border))",
              borderRadius: "8px",
            }}
          />
          <Bar dataKey="drawdown" fill="hsl(var(--destructive))" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>

      <div className="grid grid-cols-3 gap-4 mt-4 pt-4 border-t border-border">
        <div>
          <div className="text-xs text-muted-foreground">Max Drawdown</div>
          <div className="text-lg font-semibold text-destructive">-$487</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Avg Drawdown</div>
          <div className="text-lg font-semibold text-foreground">-$142</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Recovery Time</div>
          <div className="text-lg font-semibold text-foreground">3.2 days</div>
        </div>
      </div>
    </Card>
  )
}
