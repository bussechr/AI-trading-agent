export interface AITrainingPromotion {
  status: string
  candidate_metric: number | null
  champion_metric: number | null
  delta: number | null
  report_ref_count: number
}

export interface AITrainingWorkflow {
  workflow_id: string
  workflow_type: string
  status: string
  updated_at_ms: number | null
  updated_at_age_sec: number | null
  has_training_refs: boolean
  has_failure_cluster: boolean
  promotion: AITrainingPromotion
}

export interface AITrainingOpsEvent {
  event_type: string
  status: string
  time_ms: number | null
  reason: string
}

export interface AITrainingSummary {
  workflows_total: number
  running_count: number
  failed_count: number
  last_update_age_sec: number | null
  pairs_with_full_lifecycle: number
  has_content: boolean
}

export interface AITrainingViewModel {
  summary: AITrainingSummary
  workflows: AITrainingWorkflow[]
  events: AITrainingOpsEvent[]
  latest_results: AITrainingWorkflow[]
  failure_cluster_summary: Record<string, unknown> | null
  drift_explainability: Record<string, unknown> | null
  lifecycle_capabilities: Record<string, Record<string, unknown>>
}

const RUNNING = new Set(["running", "scheduled", "queued", "active", "in_progress"])
const FAILED = new Set(["failed", "error"])

function asObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  return value as Record<string, unknown>
}

function asArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return []
  return value.filter((item) => item && typeof item === "object") as Array<Record<string, unknown>>
}

function maybeParseJsonObject(value: unknown): Record<string, unknown> {
  if (typeof value !== "string") return asObject(value)
  try {
    const parsed = JSON.parse(value)
    return asObject(parsed)
  } catch {
    return {}
  }
}

function asFiniteNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null
  const num = Number(value)
  return Number.isFinite(num) ? num : null
}

function parseTimestampMs(value: unknown): number | null {
  if (value === null || value === undefined) return null
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null
    return value > 10_000_000_000 ? value : value * 1000
  }
  const txt = String(value).trim()
  if (!txt) return null
  if (/^\d+(\.\d+)?$/.test(txt)) {
    const n = Number(txt)
    if (!Number.isFinite(n)) return null
    return n > 10_000_000_000 ? n : n * 1000
  }
  const parsed = Date.parse(txt)
  return Number.isFinite(parsed) ? parsed : null
}

function extractLifecycleCapabilities(payload: Record<string, unknown>): Record<string, Record<string, unknown>> {
  const direct =
    asObject(payload.lifecycle_capabilities).lifecycle_capabilities ||
    payload.lifecycle_capabilities ||
    payload.lifecycle_capability_snapshot ||
    payload.capabilities
  return asObject(direct) as Record<string, Record<string, unknown>>
}

function normalizeWorkflow(
  raw: Record<string, unknown>,
  nowMs: number,
): AITrainingWorkflow {
  const details = maybeParseJsonObject(raw.details_json ?? raw.details ?? raw.payload)
  const report = asObject(details.report)
  const promotionRaw = asObject(report.promotion || details.promotion || raw.promotion)

  const trainingRefsRaw = details.training_eval_reports ?? raw.training_eval_reports ?? details.training_refs
  const reportRefCount = Array.isArray(trainingRefsRaw)
    ? trainingRefsRaw.length
    : Object.keys(asObject(trainingRefsRaw)).length

  const updatedAtMs = parseTimestampMs(
    raw.updated_at ?? raw.updatedAt ?? raw.time ?? raw.ts ?? raw.created_at ?? raw.createdAt,
  )
  const status = String(raw.status ?? "unknown").toLowerCase()

  return {
    workflow_id: String(raw.workflow_id ?? raw.id ?? raw.name ?? "unknown"),
    workflow_type: String(raw.workflow_type ?? raw.type ?? raw.kind ?? "unknown"),
    status,
    updated_at_ms: updatedAtMs,
    updated_at_age_sec: updatedAtMs ? Math.max(0, Math.floor((nowMs - updatedAtMs) / 1000)) : null,
    has_training_refs: reportRefCount > 0,
    has_failure_cluster: Boolean(details.failure_cluster_summary || details.failure_clusters || raw.failure_cluster_summary),
    promotion: {
      status: String(promotionRaw.status ?? promotionRaw.eligibility ?? "unknown"),
      candidate_metric: asFiniteNumber(promotionRaw.candidate_metric ?? promotionRaw.candidate_score),
      champion_metric: asFiniteNumber(promotionRaw.champion_metric ?? promotionRaw.champion_score),
      delta: asFiniteNumber(promotionRaw.delta),
      report_ref_count: reportRefCount,
    },
  }
}

function normalizeEvent(raw: Record<string, unknown>): AITrainingOpsEvent {
  return {
    event_type: String(raw.event_type ?? raw.type ?? "unknown"),
    status: String(raw.status ?? "unknown").toLowerCase(),
    time_ms: parseTimestampMs(raw.time ?? raw.ts ?? raw.updated_at ?? raw.created_at),
    reason: String(raw.reason ?? raw.message ?? ""),
  }
}

export function normalizeAITrainingTelemetry(
  workflowsPayload: unknown,
  eventsPayload: unknown,
  nowMs = Date.now(),
): AITrainingViewModel {
  const workflowsBase = asObject(workflowsPayload)
  const eventsBase = asObject(eventsPayload)

  const workflowsRaw = asArray(workflowsBase.workflows ?? asObject(workflowsBase.data).workflows)
  const eventsRaw = asArray(eventsBase.events ?? asObject(eventsBase.data).events)

  const workflows = workflowsRaw.map((row) => normalizeWorkflow(row, nowMs)).sort((a, b) => {
    const ta = a.updated_at_ms ?? 0
    const tb = b.updated_at_ms ?? 0
    return tb - ta
  })
  const events = eventsRaw.map(normalizeEvent).sort((a, b) => (b.time_ms ?? 0) - (a.time_ms ?? 0))

  const lifecycleCapabilities = extractLifecycleCapabilities(workflowsBase)
  const pairKeys = Object.keys(lifecycleCapabilities)
  const pairsWithFullLifecycle = pairKeys.filter((pair) => {
    const row = asObject(lifecycleCapabilities[pair])
    return Boolean(row.has_exit_model) && Boolean(row.has_reversal_models)
  }).length

  const latestTs = [workflows[0]?.updated_at_ms ?? 0, events[0]?.time_ms ?? 0].reduce((acc, cur) => Math.max(acc, cur), 0)
  const lastUpdateAgeSec = latestTs > 0 ? Math.max(0, Math.floor((nowMs - latestTs) / 1000)) : null

  const latestResults = workflows.filter((wf) => wf.promotion.status !== "unknown").slice(0, 12)

  const failureClusterSummary =
    asObject(workflowsBase.failure_cluster_summary).failure_cluster_summary
      ? asObject(workflowsBase.failure_cluster_summary)
      : asObject(workflowsBase.failure_cluster_summary || asObject(workflowsBase.details_json).failure_cluster_summary) ||
          null
  const driftExplainability =
    asObject(workflowsBase.drift_explainability).drift_explainability
      ? asObject(workflowsBase.drift_explainability)
      : asObject(workflowsBase.drift_explainability || asObject(workflowsBase.details_json).drift_explainability) ||
          null
  const hasContent = Boolean(
    workflows.length > 0 ||
      events.length > 0 ||
      latestTs > 0 ||
      pairKeys.length > 0 ||
      latestResults.length > 0 ||
      Object.keys(asObject(failureClusterSummary)).length > 0 ||
      Object.keys(asObject(driftExplainability)).length > 0,
  )

  return {
    summary: {
      workflows_total: workflows.length,
      running_count: workflows.filter((w) => RUNNING.has(w.status)).length,
      failed_count: workflows.filter((w) => FAILED.has(w.status)).length,
      last_update_age_sec: lastUpdateAgeSec,
      pairs_with_full_lifecycle: pairsWithFullLifecycle,
      has_content: hasContent,
    },
    workflows,
    events,
    latest_results: latestResults,
    failure_cluster_summary: Object.keys(asObject(failureClusterSummary)).length > 0 ? asObject(failureClusterSummary) : null,
    drift_explainability: Object.keys(asObject(driftExplainability)).length > 0 ? asObject(driftExplainability) : null,
    lifecycle_capabilities: lifecycleCapabilities,
  }
}
