// One queue per tab, shared by every uploader and resume call.
const configured = Number(import.meta.env.VITE_UPLOAD_CONCURRENCY ?? 2)
export const uploadConcurrency = Number.isInteger(configured) && configured >= 1 && configured <= 3 ? configured : 2
let active = 0
const waiting: (() => void)[] = []
const transfers = new Map<string, Promise<Response>>()
function drain() {
  while (active < uploadConcurrency && waiting.length) waiting.shift()!()
}
export function queuedUpload(key: string, url: string, init: RequestInit, onStart?: () => void): Promise<Response> {
  const existing = transfers.get(key)
  if (existing) return existing
  const promise = new Promise<Response>((resolve, reject) => {
    waiting.push(() => {
      active += 1
      onStart?.()
      void (async () => {
        for (let attempt = 0; ; attempt += 1) {
          init.signal?.throwIfAborted()
          try {
            const response = await fetch(url, init)
            if (response.ok || attempt >= 2 || (response.status !== 429 && response.status < 500)) return response
          } catch (error) {
            if (init.signal?.aborted || attempt >= 2) throw error
          }
          await new Promise((done) => setTimeout(done, 1000 * 2 ** attempt * (1 + Math.random() * .2)))
        }
      })().then(resolve, reject).finally(() => {
        active -= 1
        transfers.delete(key)
        drain()
      })
    })
  })
  transfers.set(key, promise)
  drain()
  return promise
}
