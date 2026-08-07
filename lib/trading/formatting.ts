export function formatCurrency(
  value: number | null | undefined,
  fallback = "N/A",
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return fallback
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

export function formatPercent(
  value: number | null | undefined,
  fallback = "—",
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return fallback
  return `${value.toFixed(2)}%`
}
