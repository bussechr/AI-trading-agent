"use client"

import { useEffect, useState } from "react"

type SnapshotListener<T> = (snapshot: T) => void

interface Subscriber<T> {
  intervalMs: number
  listener: SnapshotListener<T>
}

interface SharedPollingHookOptions<T> {
  initialSnapshot: T
  poll: (current: T) => Promise<T>
  minIntervalMs?: number
}

export function createSharedPollingHook<T>({
  initialSnapshot,
  poll,
  minIntervalMs = 1000,
}: SharedPollingHookOptions<T>) {
  let snapshot = initialSnapshot
  let timer: ReturnType<typeof setInterval> | null = null
  let inFlight: Promise<void> | null = null
  let nextSubscriberId = 1
  const subscribers = new Map<number, Subscriber<T>>()

  const emit = () => {
    for (const { listener } of subscribers.values()) {
      listener(snapshot)
    }
  }

  const resolveIntervalMs = () => {
    if (subscribers.size === 0) return null
    let fastest = Number.POSITIVE_INFINITY
    for (const { intervalMs } of subscribers.values()) {
      fastest = Math.min(fastest, intervalMs)
    }
    if (!Number.isFinite(fastest)) return null
    return Math.max(minIntervalMs, Math.floor(fastest))
  }

  const resetTimer = () => {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
    const intervalMs = resolveIntervalMs()
    if (intervalMs === null) return
    timer = setInterval(() => {
      void refresh()
    }, intervalMs)
  }

  const refresh = async () => {
    if (inFlight) {
      return inFlight
    }
    inFlight = (async () => {
      snapshot = await poll(snapshot)
      emit()
    })().finally(() => {
      inFlight = null
    })
    return inFlight
  }

  return function useSharedPolling(intervalMs = minIntervalMs): T {
    const [localSnapshot, setLocalSnapshot] = useState<T>(snapshot)

    useEffect(() => {
      const subscriberId = nextSubscriberId++
      subscribers.set(subscriberId, {
        intervalMs: Math.max(minIntervalMs, Math.floor(intervalMs)),
        listener: setLocalSnapshot,
      })
      setLocalSnapshot(snapshot)
      void refresh()
      resetTimer()

      return () => {
        subscribers.delete(subscriberId)
        resetTimer()
      }
    }, [intervalMs])

    return localSnapshot
  }
}
