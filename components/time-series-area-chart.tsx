"use client"

import { useMemo, useState, type KeyboardEvent, type PointerEvent } from "react"

export type TimeSeriesChartPoint = {
  ts: number
  value: number
}

type TimeSeriesAreaChartProps = {
  accessibleLabel: string
  color: string
  data: readonly TimeSeriesChartPoint[]
  formatTimestamp: (value: number) => string
  formatValue: (value: number) => string
  formatYAxis: (value: number) => string
  gradientId: string
  height: number
  seriesLabel: string
}

const WIDTH = 900
const MARGIN = { top: 16, right: 18, bottom: 42, left: 72 }
const Y_TICK_COUNT = 4
const X_TICK_COUNT = 4

function nearestPointIndex(points: readonly TimeSeriesChartPoint[], targetTs: number): number {
  let low = 0
  let high = points.length - 1

  while (low < high) {
    const mid = Math.floor((low + high) / 2)
    if (points[mid]!.ts < targetTs) low = mid + 1
    else high = mid
  }

  if (low === 0) return 0
  const previous = points[low - 1]!
  const current = points[low]!
  return targetTs - previous.ts <= current.ts - targetTs ? low - 1 : low
}

export function TimeSeriesAreaChart({
  accessibleLabel,
  color,
  data,
  formatTimestamp,
  formatValue,
  formatYAxis,
  gradientId,
  height,
  seriesLabel,
}: TimeSeriesAreaChartProps) {
  const [activeIndex, setActiveIndex] = useState<number | null>(null)

  const geometry = useMemo(() => {
    const points = data.filter((point) => Number.isFinite(point.ts) && Number.isFinite(point.value))
    if (points.length < 2) return null

    const xMin = points[0]!.ts
    const xMax = points[points.length - 1]!.ts
    const xSpan = Math.max(1, xMax - xMin)
    const values = points.map((point) => point.value)
    const rawMin = Math.min(...values)
    const rawMax = Math.max(...values)
    const rawSpan = rawMax - rawMin
    const padding = rawSpan > 0 ? rawSpan * 0.08 : Math.max(Math.abs(rawMin) * 0.01, 1)
    const yMin = rawMin - padding
    const yMax = rawMax + padding
    const ySpan = Math.max(Number.EPSILON, yMax - yMin)
    const plotWidth = WIDTH - MARGIN.left - MARGIN.right
    const plotHeight = height - MARGIN.top - MARGIN.bottom

    const projected = points.map((point) => ({
      ...point,
      x: MARGIN.left + ((point.ts - xMin) / xSpan) * plotWidth,
      y: MARGIN.top + (1 - (point.value - yMin) / ySpan) * plotHeight,
    }))
    const linePath = projected
      .map((point, index) => `${index === 0 ? "M" : "L"}${point.x.toFixed(2)},${point.y.toFixed(2)}`)
      .join(" ")
    const bottom = MARGIN.top + plotHeight
    const areaPath = `${linePath} L${projected[projected.length - 1]!.x.toFixed(2)},${bottom.toFixed(2)} L${projected[0]!.x.toFixed(2)},${bottom.toFixed(2)} Z`
    const yTicks = Array.from({ length: Y_TICK_COUNT }, (_, index) => {
      const ratio = index / (Y_TICK_COUNT - 1)
      return {
        value: yMax - ratio * ySpan,
        y: MARGIN.top + ratio * plotHeight,
      }
    })
    const xTicks = Array.from({ length: X_TICK_COUNT }, (_, index) => {
      const ratio = index / (X_TICK_COUNT - 1)
      return {
        value: xMin + ratio * xSpan,
        x: MARGIN.left + ratio * plotWidth,
      }
    })

    return { areaPath, bottom, linePath, plotWidth, points, projected, xMin, xSpan, xTicks, yTicks }
  }, [data, height])

  if (!geometry) return null

  const selectedIndex = activeIndex === null ? null : Math.min(activeIndex, geometry.projected.length - 1)
  const selected = selectedIndex === null ? null : geometry.projected[selectedIndex]!

  function updatePointer(event: PointerEvent<SVGSVGElement>) {
    const matrix = event.currentTarget.getScreenCTM()
    if (!matrix) return
    const point = event.currentTarget.createSVGPoint()
    point.x = event.clientX
    point.y = event.clientY
    const local = point.matrixTransform(matrix.inverse())
    const ratio = Math.max(0, Math.min(1, (local.x - MARGIN.left) / geometry.plotWidth))
    const targetTs = geometry.xMin + ratio * geometry.xSpan
    setActiveIndex(nearestPointIndex(geometry.points, targetTs))
  }

  function moveSelection(event: KeyboardEvent<SVGSVGElement>) {
    if (event.key === "Escape") {
      setActiveIndex(null)
      return
    }
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return
    event.preventDefault()
    if (event.key === "Home") setActiveIndex(0)
    else if (event.key === "End") setActiveIndex(geometry.points.length - 1)
    else if (event.key === "ArrowLeft") setActiveIndex((current) => Math.max(0, (current ?? geometry.points.length) - 1))
    else setActiveIndex((current) => Math.min(geometry.points.length - 1, (current ?? -1) + 1))
  }

  return (
    <div className="relative" style={{ height }}>
      <svg
        aria-label={`${accessibleLabel}. ${geometry.points.length} points. Use the left and right arrow keys to inspect values.`}
        className="h-full w-full rounded-lg focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 focus:ring-offset-card"
        onBlur={() => setActiveIndex(null)}
        onFocus={() => setActiveIndex((current) => current ?? geometry.points.length - 1)}
        onKeyDown={moveSelection}
        onPointerLeave={() => setActiveIndex(null)}
        onPointerMove={updatePointer}
        preserveAspectRatio="none"
        role="img"
        tabIndex={0}
        viewBox={`0 0 ${WIDTH} ${height}`}
      >
        <defs>
          <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
            <stop offset="5%" stopColor={color} stopOpacity={0.28} />
            <stop offset="95%" stopColor={color} stopOpacity={0.02} />
          </linearGradient>
        </defs>

        {geometry.yTicks.map((tick) => (
          <g key={tick.y}>
            <line
              stroke="var(--color-border)"
              strokeDasharray="3 3"
              vectorEffect="non-scaling-stroke"
              x1={MARGIN.left}
              x2={WIDTH - MARGIN.right}
              y1={tick.y}
              y2={tick.y}
            />
            <text
              fill="var(--color-muted-foreground)"
              fontSize="12"
              textAnchor="end"
              x={MARGIN.left - 10}
              y={tick.y + 4}
            >
              {formatYAxis(tick.value)}
            </text>
          </g>
        ))}

        {geometry.xTicks.map((tick, index) => (
          <text
            fill="var(--color-muted-foreground)"
            fontSize="12"
            key={tick.x}
            textAnchor={index === 0 ? "start" : index === geometry.xTicks.length - 1 ? "end" : "middle"}
            x={tick.x}
            y={geometry.bottom + 28}
          >
            {formatTimestamp(tick.value)}
          </text>
        ))}

        <path d={geometry.areaPath} fill={`url(#${gradientId})`} />
        <path
          d={geometry.linePath}
          fill="none"
          stroke={color}
          strokeWidth="2"
          vectorEffect="non-scaling-stroke"
        />

        {selected ? (
          <g aria-hidden="true">
            <line
              stroke="var(--color-muted-foreground)"
              strokeDasharray="4 4"
              vectorEffect="non-scaling-stroke"
              x1={selected.x}
              x2={selected.x}
              y1={MARGIN.top}
              y2={geometry.bottom}
            />
            <circle cx={selected.x} cy={selected.y} fill="var(--color-card)" r="5" stroke={color} strokeWidth="3" vectorEffect="non-scaling-stroke" />
          </g>
        ) : null}
      </svg>

      {selected ? (
        <div
          aria-live="polite"
          className="pointer-events-none absolute top-2 z-10 min-w-36 -translate-x-1/2 rounded-xl border border-border bg-card px-3 py-2 text-xs shadow-lg"
          style={{ left: `${(selected.x / WIDTH) * 100}%` }}
        >
          <div className="text-muted-foreground">{formatTimestamp(selected.ts)}</div>
          <div className="mt-1 font-semibold text-foreground">
            {seriesLabel}: {formatValue(selected.value)}
          </div>
        </div>
      ) : null}
    </div>
  )
}
