const DEFAULT_MAX_FUTURE_SKEW_SECS = 5

export function timestampToMs(value: unknown): number {
  if (value === null || value === undefined) return 0

  if (typeof value === "number") {
    if (!Number.isFinite(value) || value <= 0) return 0
    return value > 10_000_000_000 ? value : value * 1000
  }

  const text = String(value).trim()
  if (!text) return 0
  if (/^\d+(?:\.\d+)?$/.test(text)) {
    const numeric = Number(text)
    if (!Number.isFinite(numeric) || numeric <= 0) return 0
    return numeric > 10_000_000_000 ? numeric : numeric * 1000
  }

  // Bridge/runtime timestamps without an explicit zone are defined as UTC.
  const normalized = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(text)
    ? `${text}Z`
    : text
  const parsed = Date.parse(normalized)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0
}

export function normalizeAgeSecs(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null
  const numeric = Number(value)
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : null
}

export function ageSecsFromTimestamp(
  value: unknown,
  nowMs = Date.now(),
  maxFutureSkewSecs = DEFAULT_MAX_FUTURE_SKEW_SECS,
): number | null {
  const timestampMs = timestampToMs(value)
  if (timestampMs <= 0 || !Number.isFinite(nowMs)) return null
  const ageSecs = (nowMs - timestampMs) / 1000
  if (!Number.isFinite(ageSecs) || ageSecs < -Math.max(0, maxFutureSkewSecs)) return null
  return Math.max(0, ageSecs)
}
