// Sequential, visibility-aware polling; operation returns a stable change fingerprint.
export function pollVisible(operation: (signal: AbortSignal) => Promise<unknown>, initialDelay = 2000) {
  let stopped = false
  let inFlight = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let controller: AbortController | undefined
  let fingerprint: string | undefined
  let changedAt = Date.now()
  let refreshPending = false
  async function tick() {
    if (stopped || document.hidden) return
    if (inFlight) { refreshPending = true; return }
    inFlight = true
    controller = new AbortController()
    try {
      const next = JSON.stringify(await operation(controller.signal), (key, value) => ['updatedAt', 'leaseExpiresAt', 'leaseUntil'].includes(key) ? undefined : value)
      if (next !== fingerprint) { fingerprint = next; changedAt = Date.now() }
    } catch { /* Retain last usable state; back off even during a network outage. */ }
    finally {
      inFlight = false
      const unchanged = Date.now() - changedAt
      const delay = unchanged < 30000 ? 2000 : unchanged < 60000 ? 5000 : unchanged < 90000 ? 10000 : 15000
      if (!stopped && !document.hidden) {
        timer = setTimeout(() => void tick(), refreshPending ? 0 : delay * (1 + Math.random() * .2))
        refreshPending = false
      }
    }
  }
  function visibility() {
    clearTimeout(timer)
    if (document.hidden) controller?.abort()
    else void tick()
  }
  document.addEventListener('visibilitychange', visibility)
  timer = setTimeout(() => void tick(), initialDelay)
  return () => {
    stopped = true
    clearTimeout(timer)
    controller?.abort()
    document.removeEventListener('visibilitychange', visibility)
  }
}
