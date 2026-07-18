import test from "node:test"
import assert from "node:assert/strict"

import {
  normalizeAITrainingTelemetry,
  normalizeAITrainingTelemetryWithLastGood,
} from "../lib/trading/ai-training-normalize.ts"

test("normalizeAITrainingTelemetry preserves lineage summaries and drilldowns", () => {
  const workflowsPayload = {
    lifecycle_capabilities: {
      lifecycle_capabilities: {
        EURUSD: { has_exit_model: true, has_reversal_models: true },
      },
    },
    workflows: [
      {
        workflow_id: "eurusd-training-eval",
        pair: "EURUSD",
        workflow_type: "training_eval",
        status: "eligible",
        updated_at: "2026-04-01T12:00:00Z",
        details_json: {
          registry_meta: {
            run_id: "run-2",
            registry_path: "/tmp/registry/run-2.json",
            registry_source: "shadow",
            artifact_kind: "paper_pack",
            promotion_status: "eligible",
            feature_schema_id: "fx.features.v1",
            dataset_fingerprint: "dfp-2",
          },
          training_eval_reports: [{ report_id: "report-2" }],
          lifecycle_capabilities: { has_exit_model: true, has_reversal_models: true },
        },
        promotion: {
          status: "eligible",
          candidate_metric: 1.1,
          champion_metric: 1.0,
          delta: 0.1,
        },
      },
      {
        workflow_id: "eurusd-shadow-eval",
        pair: "EURUSD",
        workflow_type: "shadow_eval",
        status: "running",
        updated_at: "2026-04-01T11:00:00Z",
        details_json: {
          registry_meta: {
            run_id: "run-1",
            registry_path: "/tmp/registry/run-1.json",
            registry_source: "paper",
            artifact_kind: "canary_pack",
            promotion_status: "running",
            feature_schema_id: "fx.features.v1",
            dataset_fingerprint: "dfp-1",
          },
          training_eval_reports: [{ report_id: "report-1" }],
          lifecycle_capabilities: { has_exit_model: true, has_reversal_models: false },
        },
        promotion: { status: "running", candidate_metric: null, champion_metric: null, delta: null },
      },
    ],
  }

  const eventsPayload = {
    events: [
      {
        event_type: "training_shadow_update",
        status: "info",
        time: "2026-04-01T12:00:01Z",
        payload: {
          shadow: true,
          pair: "EURUSD",
          model: "shadow-xgb",
          run_name: "run-2",
          report_path: "/tmp/report-2",
        },
      },
    ],
  }

  const view = normalizeAITrainingTelemetry(workflowsPayload, eventsPayload, Date.parse("2026-04-01T12:05:00Z"))

  assert.equal(view.summary.workflows_total, 2)
  assert.equal(view.summary.has_content, true)
  assert.equal(view.lineage_summary.workflows_with_lineage, 2)
  assert.equal(view.lineage_summary.unique_pairs, 1)
  assert.equal(view.lineage_summary.unique_run_ids, 2)
  assert.equal(view.lineage_summary.latest_run_id, "run-2")
  assert.equal(view.lineage_summary.latest_registry_path, "/tmp/registry/run-2.json")
  assert.equal(view.lineage_drilldowns[0].workflow_id, "eurusd-training-eval")
  assert.equal(view.lineage_drilldowns[0].pair, "EURUSD")
  assert.equal(view.lineage_drilldowns[0].registry_source, "shadow")
  assert.equal(view.workflows[0].lineage?.run_id, "run-2")
})

test("partial AI ops updates preserve the failed source's last good payload", () => {
  const now = Date.parse("2026-04-01T12:05:00Z")
  const current = {
    workflows: {
      workflows: [
        {
          workflow_id: "eurusd-training-eval",
          status: "running",
          updated_at: "2026-04-01T12:00:00Z",
        },
      ],
    },
    events: {
      events: [
        {
          event_type: "training_shadow_update",
          status: "info",
          time: "2026-04-01T12:00:01Z",
          payload: { shadow: true, pair: "EURUSD" },
        },
      ],
    },
  }

  const next = normalizeAITrainingTelemetryWithLastGood(
    current,
    {
      events: {
        events: [
          {
            event_type: "training_shadow_update",
            status: "info",
            time: "2026-04-01T12:04:30Z",
            payload: { shadow: true, pair: "GBPUSD" },
          },
        ],
      },
    },
    now,
  )

  assert.equal(next.data.workflows.length, 1)
  assert.equal(next.data.workflows[0].workflow_id, "eurusd-training-eval")
  assert.equal(next.data.events.length, 1)
  assert.equal(next.data.events[0].pair, "GBPUSD")
  assert.equal(next.data.summary.last_update_age_sec, 30)
  assert.equal(next.sources.workflows, current.workflows)
})

test("future-dated AI ops telemetry cannot masquerade as fresh", () => {
  const now = Date.parse("2026-04-01T12:00:00Z")
  const view = normalizeAITrainingTelemetry(
    {
      workflows: [
        {
          workflow_id: "future-run",
          status: "running",
          updated_at: "2026-04-01T12:05:00Z",
        },
      ],
    },
    {},
    now,
  )

  assert.equal(view.workflows[0].updated_at_ms, null)
  assert.equal(view.summary.last_update_age_sec, null)
})

test("timezone-less AI ops timestamps use the UTC wire convention", () => {
  const now = Date.parse("2026-04-01T12:00:10Z")
  const view = normalizeAITrainingTelemetry(
    {
      workflows: [
        {
          workflow_id: "utc-run",
          status: "running",
          updated_at: "2026-04-01T12:00:00",
        },
      ],
    },
    {},
    now,
  )

  assert.equal(view.workflows[0].updated_at_ms, Date.parse("2026-04-01T12:00:00Z"))
  assert.equal(view.summary.last_update_age_sec, 10)
})
