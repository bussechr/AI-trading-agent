export interface RuntimeStartupSuppressionState {
  bootId?: string
  lastFailureBootId?: string
  lastProgressAgeSecs?: number | null
  phaseIndex?: number
  failureReason?: string
  status?: string
  recovered?: boolean
}

export function shouldSuppressRuntimeStartupFailure(
  runtimeStartup: RuntimeStartupSuppressionState,
  runtimeStatus: string,
): boolean {
  if (runtimeStatus === "running" && Boolean(runtimeStartup.recovered)) return true

  const activeBootId = String(runtimeStartup.bootId || "").trim()
  const failedBootId = String(runtimeStartup.lastFailureBootId || "").trim()
  const hasProgress =
    (runtimeStartup.lastProgressAgeSecs !== null && runtimeStartup.lastProgressAgeSecs !== undefined) ||
    Number(runtimeStartup.phaseIndex || 0) > 0

  return Boolean(
    activeBootId &&
      failedBootId &&
      activeBootId !== failedBootId &&
      !runtimeStartup.failureReason &&
      hasProgress &&
      (runtimeStatus === "starting" ||
        runtimeStartup.status === "ready" ||
        runtimeStartup.status === "recovered_with_warnings"),
  )
}
