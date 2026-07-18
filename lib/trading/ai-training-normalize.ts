export interface AITrainingPromotion {
  status: string
  candidate_metric: number | null
  champion_metric: number | null
  delta: number | null
  report_ref_count: number
}

export interface AITrainingLineageDrilldown {
  workflow_id: string
  pair: string
  run_id: string
  registry_path: string
  registry_source: string
  artifact_kind: string
  promotion_status: string
  feature_schema_id: string
  dataset_fingerprint: string
  training_ref_count: number
  report_ref_count: number
  updated_at_ms: number | null
  updated_at_age_sec: number | null
}

export interface AITrainingLineageSummary {
  workflows_with_lineage: number
  unique_pairs: number
  unique_run_ids: number
  unique_registry_paths: number
  latest_workflow_id: string | null
  latest_pair: string | null
  latest_run_id: string | null
  latest_registry_path: string | null
  latest_registry_source: string | null
  latest_artifact_kind: string | null
}

export interface AITrainingWorkflow {
  workflow_id: string
  workflow_type: string
  status: string
  updated_at_ms: number | null
  updated_at_age_sec: number | null
  has_primary_models: boolean
  has_exit_model: boolean
  has_reversal_models: boolean
  lifecycle_complete: boolean
  has_training_refs: boolean
  has_failure_cluster: boolean
  promotion: AITrainingPromotion
  lineage: AITrainingLineageDrilldown | null
}

export interface AITrainingOpsEvent {
  event_type: string
  status: string
  time_ms: number | null
  reason: string
  pair: string
  model: string
  run_name: string
  report_path: string
  shadow: boolean
}

export interface AITrainingShadowRun extends AITrainingOpsEvent {
  shadow: true
}

export interface AITrainingSummary {
  workflows_total: number
  activation_workflows_total: number
  running_count: number
  failed_count: number
  last_update_age_sec: number | null
  latest_activation_age_sec: number | null
  shadow_runs_total: number
  latest_shadow_run_age_sec: number | null
  latest_shadow_run_status: string | null
  latest_shadow_run_pair: string | null
  latest_shadow_run_model: string | null
  pairs_with_full_lifecycle: number
  has_content: boolean
}

export interface AITrainingViewModel {
  summary: AITrainingSummary
  workflows: AITrainingWorkflow[]
  events: AITrainingOpsEvent[]
  shadow_runs: AITrainingShadowRun[]
  latest_results: AITrainingWorkflow[]
  lineage_summary: AITrainingLineageSummary
  lineage_drilldowns: AITrainingLineageDrilldown[]
  failure_cluster_summary: Record<string, unknown> | null
  drift_explainability: Record<string, unknown> | null
  lifecycle_capabilities: Record<string, Record<string, unknown>>
}

export interface AITrainingSourcePayloads {
  workflows: unknown | null
  events: unknown | null
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
  const normalized = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(txt)
    ? `${txt}Z`
    : txt
  const parsed = Date.parse(normalized)
  return Number.isFinite(parsed) ? parsed : null
}

function validatedTimestampMs(value: unknown, nowMs: number, maxFutureSkewMs = 60_000): number | null {
  const parsed = parseTimestampMs(value)
  if (parsed === null || parsed <= 0 || parsed > nowMs + maxFutureSkewMs) return null
  return parsed
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
  const registryMeta = asObject(details.registry_meta)
  const artifacts = asObject(registryMeta.artifacts)
  const lifecycleCapabilities = asObject(details.lifecycle_capabilities)

  const trainingRefsRaw = details.training_eval_reports ?? raw.training_eval_reports ?? details.training_refs
  const reportRefCount = Array.isArray(trainingRefsRaw)
    ? trainingRefsRaw.length
    : Object.keys(asObject(trainingRefsRaw)).length
  const pair = String(
    raw.pair ??
      registryMeta.pair ??
      details.pair ??
      String(raw.workflow_id ?? raw.id ?? "unknown").replace(/-training-eval$/i, ""),
  ).toUpperCase()
  const registryPath = String(
    registryMeta.registry_path ??
      registryMeta.registryPath ??
      details.registry_path ??
      details.registryPath ??
      raw.registry_path ??
      raw.registryPath ??
      "",
  ).trim()
  const registrySource = String(
    registryMeta.registry_source ??
      registryMeta.registrySource ??
      details.registry_source ??
      details.registrySource ??
      raw.registry_source ??
      raw.registrySource ??
      "",
  ).trim()
  const artifactKind = String(
    registryMeta.artifact_kind ??
      registryMeta.artifactKind ??
      details.artifact_kind ??
      details.artifactKind ??
      raw.artifact_kind ??
      raw.artifactKind ??
      "",
  ).trim()

  const updatedAtMs = validatedTimestampMs(
    raw.updated_at ?? raw.updatedAt ?? raw.time ?? raw.ts ?? raw.created_at ?? raw.createdAt,
    nowMs,
  )
  const status = String(raw.status ?? "unknown").toLowerCase()
  const hasExitModel = Boolean(lifecycleCapabilities.has_exit_model || artifacts.exit_policy)
  const hasReversalModels = Boolean(
    lifecycleCapabilities.has_reversal_models || (artifacts.reversal_failure && artifacts.reversal_opportunity),
  )
  const lifecycleComplete = Boolean(registryMeta.lifecycle_complete || (hasExitModel && hasReversalModels))
  const hasPrimaryModels = Boolean(
    (artifacts.regime || artifacts.regime_hmm) &&
      (artifacts.swing_xgb || artifacts.swing || artifacts.swing_transformer) &&
      (artifacts.intraday_xgb || artifacts.intraday || artifacts.intraday_tcn) &&
      artifacts.meta,
  )
  const lineage: AITrainingLineageDrilldown = {
    workflow_id: String(raw.workflow_id ?? raw.id ?? raw.name ?? "unknown"),
    pair,
    run_id: String(
      registryMeta.run_id ??
        registryMeta.runId ??
        registryMeta.bundle_run_id ??
        registryMeta.bundleRunId ??
        details.run_id ??
        raw.run_id ??
        raw.bundle_run_id ??
        "",
    ).trim(),
    registry_path: registryPath,
    registry_source: registrySource,
    artifact_kind: artifactKind,
    promotion_status: String(registryMeta.promotion_status ?? registryMeta.promotionStatus ?? raw.status ?? status),
    feature_schema_id: String(
      registryMeta.feature_schema_id ??
        registryMeta.featureSchemaId ??
        details.feature_schema_id ??
        details.featureSchemaId ??
        "",
    ).trim(),
    dataset_fingerprint: String(
      registryMeta.dataset_fingerprint ??
        registryMeta.datasetFingerprint ??
        details.dataset_fingerprint ??
        details.datasetFingerprint ??
        "",
    ).trim(),
    training_ref_count: reportRefCount,
    report_ref_count: reportRefCount,
    updated_at_ms: updatedAtMs,
    updated_at_age_sec: updatedAtMs ? Math.max(0, Math.floor((nowMs - updatedAtMs) / 1000)) : null,
  }
  const hasLineage = Boolean(
    lineage.run_id ||
      lineage.registry_path ||
      lineage.registry_source ||
      lineage.artifact_kind ||
      lineage.feature_schema_id ||
      lineage.dataset_fingerprint,
  )

  return {
    workflow_id: String(raw.workflow_id ?? raw.id ?? raw.name ?? "unknown"),
    workflow_type: String(raw.workflow_type ?? raw.type ?? raw.kind ?? "unknown"),
    status,
    updated_at_ms: updatedAtMs,
    updated_at_age_sec: updatedAtMs ? Math.max(0, Math.floor((nowMs - updatedAtMs) / 1000)) : null,
    has_primary_models: hasPrimaryModels,
    has_exit_model: hasExitModel,
    has_reversal_models: hasReversalModels,
    lifecycle_complete: lifecycleComplete,
    has_training_refs: reportRefCount > 0,
    has_failure_cluster: Boolean(details.failure_cluster_summary || details.failure_clusters || raw.failure_cluster_summary),
    promotion: {
      status: String(promotionRaw.status ?? promotionRaw.eligibility ?? "unknown"),
      candidate_metric: asFiniteNumber(promotionRaw.candidate_metric ?? promotionRaw.candidate_score),
      champion_metric: asFiniteNumber(promotionRaw.champion_metric ?? promotionRaw.champion_score),
      delta: asFiniteNumber(promotionRaw.delta),
      report_ref_count: reportRefCount,
    },
    lineage: hasLineage ? lineage : null,
  }
}

function normalizeEvent(raw: Record<string, unknown>, nowMs: number): AITrainingOpsEvent {
  const payload = asObject(raw.payload)
  const eventType = String(raw.event_type ?? raw.type ?? "unknown")
  const shadow = Boolean(payload.shadow ?? raw.shadow ?? eventType === "training_shadow_update")
  return {
    event_type: eventType,
    status: String(raw.status ?? "unknown").toLowerCase(),
    time_ms: validatedTimestampMs(raw.time ?? raw.ts ?? raw.updated_at ?? raw.created_at, nowMs),
    reason: String(raw.reason ?? raw.message ?? ""),
    pair: String(payload.pair ?? raw.pair ?? "").toUpperCase(),
    model: String(payload.model ?? raw.model ?? "").trim(),
    run_name: String(payload.run_name ?? raw.run_name ?? "").trim(),
    report_path: String(payload.report_path ?? raw.report_path ?? "").trim(),
    shadow,
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
  const events = eventsRaw.map((row) => normalizeEvent(row, nowMs)).sort((a, b) => (b.time_ms ?? 0) - (a.time_ms ?? 0))
  const shadowRuns = events.filter((event): event is AITrainingShadowRun => event.shadow)
  const lineageDrilldowns = workflows
    .map((workflow) => workflow.lineage)
    .filter((item): item is AITrainingLineageDrilldown => Boolean(item))

  const lifecycleCapabilities = extractLifecycleCapabilities(workflowsBase)
  const pairKeys = Object.keys(lifecycleCapabilities)
  const pairsWithFullLifecycle = pairKeys.filter((pair) => {
    const row = asObject(lifecycleCapabilities[pair])
    return Boolean(row.has_exit_model) && Boolean(row.has_reversal_models)
  }).length

  const latestTs = [workflows[0]?.updated_at_ms ?? 0, events[0]?.time_ms ?? 0].reduce((acc, cur) => Math.max(acc, cur), 0)
  const lastUpdateAgeSec = latestTs > 0 ? Math.max(0, Math.floor((nowMs - latestTs) / 1000)) : null
  const latestWorkflow = workflows[0] || null
  const latestShadowRun = shadowRuns[0] || null

  const latestResults = workflows.filter((wf) => wf.promotion.status !== "unknown").slice(0, 12)
  const lineageSummary: AITrainingLineageSummary = {
    workflows_with_lineage: lineageDrilldowns.length,
    unique_pairs: new Set(lineageDrilldowns.map((item) => item.pair).filter(Boolean)).size,
    unique_run_ids: new Set(lineageDrilldowns.map((item) => item.run_id).filter(Boolean)).size,
    unique_registry_paths: new Set(lineageDrilldowns.map((item) => item.registry_path).filter(Boolean)).size,
    latest_workflow_id: lineageDrilldowns[0]?.workflow_id ?? null,
    latest_pair: lineageDrilldowns[0]?.pair ?? null,
    latest_run_id: lineageDrilldowns[0]?.run_id ?? null,
    latest_registry_path: lineageDrilldowns[0]?.registry_path ?? null,
    latest_registry_source: lineageDrilldowns[0]?.registry_source ?? null,
    latest_artifact_kind: lineageDrilldowns[0]?.artifact_kind ?? null,
  }

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
      lineageDrilldowns.length > 0 ||
      Object.keys(asObject(failureClusterSummary)).length > 0 ||
      Object.keys(asObject(driftExplainability)).length > 0,
  )

  return {
    summary: {
      workflows_total: workflows.length,
      activation_workflows_total: workflows.length,
      running_count: workflows.filter((w) => RUNNING.has(w.status)).length,
      failed_count: workflows.filter((w) => FAILED.has(w.status)).length,
      last_update_age_sec: lastUpdateAgeSec,
      latest_activation_age_sec: latestWorkflow?.updated_at_age_sec ?? null,
      shadow_runs_total: shadowRuns.length,
      latest_shadow_run_age_sec: latestShadowRun?.time_ms ? Math.max(0, Math.floor((nowMs - latestShadowRun.time_ms) / 1000)) : null,
      latest_shadow_run_status: latestShadowRun?.status ?? null,
      latest_shadow_run_pair: latestShadowRun?.pair ?? null,
      latest_shadow_run_model: latestShadowRun?.model ?? null,
      pairs_with_full_lifecycle: pairsWithFullLifecycle,
      has_content: hasContent,
    },
    workflows,
    events,
    shadow_runs: shadowRuns,
    latest_results: latestResults,
    lineage_summary: lineageSummary,
    lineage_drilldowns: lineageDrilldowns,
    failure_cluster_summary: Object.keys(asObject(failureClusterSummary)).length > 0 ? asObject(failureClusterSummary) : null,
    drift_explainability: Object.keys(asObject(driftExplainability)).length > 0 ? asObject(driftExplainability) : null,
    lifecycle_capabilities: lifecycleCapabilities,
  }
}

export function normalizeAITrainingTelemetryWithLastGood(
  current: AITrainingSourcePayloads,
  incoming: Partial<AITrainingSourcePayloads>,
  nowMs = Date.now(),
): { data: AITrainingViewModel; sources: AITrainingSourcePayloads } {
  const sources: AITrainingSourcePayloads = {
    workflows: Object.prototype.hasOwnProperty.call(incoming, "workflows")
      ? incoming.workflows ?? null
      : current.workflows,
    events: Object.prototype.hasOwnProperty.call(incoming, "events")
      ? incoming.events ?? null
      : current.events,
  }
  return {
    data: normalizeAITrainingTelemetry(sources.workflows || {}, sources.events || {}, nowMs),
    sources,
  }
}
