import test from "node:test"
import assert from "node:assert/strict"

import { shouldSuppressRuntimeStartupFailure } from "../lib/trading/runtime-startup.ts"

test("healthy recovered runtime suppresses stale startup failure banner", () => {
  assert.equal(
    shouldSuppressRuntimeStartupFailure(
      {
        bootId: "boot-new",
        lastFailureBootId: "boot-old",
        lastProgressAgeSecs: 0,
        phaseIndex: 8,
        failureReason: "",
        status: "ready",
        recovered: true,
      },
      "running",
    ),
    true,
  )
})

test("new boot progress suppresses a prior boot failure while starting", () => {
  assert.equal(
    shouldSuppressRuntimeStartupFailure(
      {
        bootId: "boot-new",
        lastFailureBootId: "boot-old",
        lastProgressAgeSecs: 0,
        phaseIndex: 1,
        failureReason: "",
        status: "starting",
        recovered: false,
      },
      "starting",
    ),
    true,
  )
})

test("active boot failure remains visible", () => {
  assert.equal(
    shouldSuppressRuntimeStartupFailure(
      {
        bootId: "boot-current",
        lastFailureBootId: "boot-current",
        lastProgressAgeSecs: 12,
        phaseIndex: 3,
        failureReason: "model load failed",
        status: "failed",
        recovered: false,
      },
      "failed",
    ),
    false,
  )
})
