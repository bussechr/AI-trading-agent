function positiveFinite(value: unknown): number | null {
  const number = typeof value === "number" ? value : Number(value)
  return Number.isFinite(number) && number > 0 ? number : null
}

export function tickMidPrice(raw: unknown): number | null {
  const row = raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as Record<string, unknown>) : {}
  const bid = positiveFinite(row.bid)
  const ask = positiveFinite(row.ask)
  if (bid !== null && ask !== null) return (bid + ask) / 2

  for (const value of [row.mid, row.price, row.last, row.ask, row.bid]) {
    const candidate = positiveFinite(value)
    if (candidate !== null) return candidate
  }
  return null
}
