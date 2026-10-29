import { useEffect, useRef } from 'react'

// Sequential, visibility-aware polling; operation returns a stable change fingerprint.
export function pollVisible(operation: (signal: AbortSignal) => Promise<unknown>, initialDelay = 2000, minimumDelay: () => number = () => 2000) {
  let stopped = false
  let inFlight = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let controller: AbortController | undefined
  let fingerprint: string | undefined
  let changedAt = Date.now()
  let refreshPending = false
  let failures = 0
  const paused = () => document.hidden || navigator.onLine === false
  async function tick() {
    if (stopped || paused()) return
    if (inFlight) { refreshPending = true; return }
    inFlight = true
    controller = new AbortController()
    try {
      const next = JSON.stringify(await operation(controller.signal), (key, value) => ['updatedAt', 'leaseExpiresAt', 'leaseUntil'].includes(key) ? undefined : value)
      failures = 0
      if (next !== fingerprint) { fingerprint = next; changedAt = Date.now() }
    } catch { if (!controller.signal.aborted) failures += 1 }
    finally {
      inFlight = false
      const unchanged = Date.now() - changedAt
      const adaptiveDelay = unchanged < 30000 ? 2000 : unchanged < 60000 ? 5000 : unchanged < 90000 ? 10000 : 15000
      const delay = Math.max(minimumDelay(), adaptiveDelay, failures ? Math.min(120000, 5000 * 2 ** Math.min(failures - 1, 5)) : 0)
      if (!stopped && !paused()) {
        timer = setTimeout(() => void tick(), refreshPending ? 0 : delay * (1 + Math.random() * .2))
        refreshPending = false
      }
    }
  }
  function visibility() {
    clearTimeout(timer)
    if (paused()) controller?.abort()
    else void tick()
  }
  document.addEventListener('visibilitychange', visibility)
  window.addEventListener('online', visibility)
  window.addEventListener('offline', visibility)
  timer = setTimeout(() => void tick(), initialDelay)
  return () => {
    stopped = true
    clearTimeout(timer)
    controller?.abort()
    document.removeEventListener('visibilitychange', visibility)
    window.removeEventListener('online', visibility)
    window.removeEventListener('offline', visibility)
  }
}

// JSON API values only. Keep object identity (and image DOM) when nothing changed.
export function retainEqual<T>(current: T, next: T): T {
  return JSON.stringify(current) === JSON.stringify(next) ? current : next
}

// One coordinator per visible resource. A cheap version read gates the expensive list read.
// Commit the version only after success so a failed list read is retried.
export function useSilentRefresh(options: {
  enabled: boolean
  resourceKey: string
  active?: boolean
  revision: (signal: AbortSignal) => Promise<unknown>
  refresh: (signal: AbortSignal) => Promise<void>
}) {
  const latest = useRef(options)
  latest.current = options
  useEffect(() => {
    if (!options.enabled) return
    let revision: string | undefined
    const interval = () => latest.current.active ? 5000 : 30000
    return pollVisible(async (signal) => {
      const next = JSON.stringify(await latest.current.revision(signal))
      if (signal.aborted) return
      if (next !== revision) {
        await latest.current.refresh(signal)
        if (!signal.aborted) revision = next
      }
      return next
    }, interval(), interval)
  }, [options.enabled, options.resourceKey])
}
