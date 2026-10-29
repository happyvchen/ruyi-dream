/* @vitest-environment jsdom */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { pollVisible, useSilentRefresh } from '../polling'
import { act, cleanup, renderHook } from '@testing-library/react'
import { queuedUpload } from '../uploadQueue'
import { cancelPageReads, request } from '../../api'

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve() }

describe('network budgets', () => {
  it('observes five unchanged minutes in fewer than 50 sequential polls, ignoring heartbeats', async () => {
    vi.useFakeTimers()
    let active = 0
    let peak = 0
    const operation = vi.fn(async () => {
      peak = Math.max(peak, ++active)
      await Promise.resolve()
      active -= 1
      return { status: 'FINAL_RUNNING', detailRevision: 1, updatedAt: Date.now() }
    })
    const stop = pollVisible(operation)
    await vi.advanceTimersByTimeAsync(300000)
    stop()
    expect(operation.mock.calls.length).toBeLessThanOrEqual(50)
    expect(operation.mock.calls.length).toBeGreaterThan(20)
    expect(peak).toBe(1)
  })

  it('pauses hidden tabs, aborts an old request and refreshes immediately on return', async () => {
    vi.useFakeTimers()
    let hidden = false
    vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
    const operation = vi.fn(async (_signal: AbortSignal) => 'same')
    const stop = pollVisible(operation)
    await vi.advanceTimersByTimeAsync(2000)
    hidden = true
    document.dispatchEvent(new Event('visibilitychange'))
    expect(operation.mock.calls[0][0].aborted).toBe(true)
    const count = operation.mock.calls.length
    await vi.advanceTimersByTimeAsync(60000)
    expect(operation).toHaveBeenCalledTimes(count)
    hidden = false
    document.dispatchEvent(new Event('visibilitychange'))
    await flush()
    expect(operation).toHaveBeenCalledTimes(count + 1)
    stop()
  })

  it('shares two PUT slots across independent callers and deduplicates the same object', async () => {
    const finish: (() => void)[] = []
    let active = 0
    let peak = 0
    const fetcher = vi.fn(() => new Promise<Response>((resolve) => {
      peak = Math.max(peak, ++active)
      finish.push(() => { active -= 1; resolve(new Response('', { status: 200 })) })
    }))
    vi.stubGlobal('fetch', fetcher)
    const a = queuedUpload('a', 'https://storage.test/a', { method: 'PUT' })
    const same = queuedUpload('a', 'https://storage.test/a', { method: 'PUT' })
    const rest = ['b', 'c', 'd', 'e'].map((key) => queuedUpload(key, `https://storage.test/${key}`, { method: 'PUT' }))
    expect(same).toBe(a)
    expect(fetcher).toHaveBeenCalledTimes(2)
    while (finish.length) { finish.shift()!(); await flush() }
    await Promise.all([a, same, ...rest])
    expect(fetcher).toHaveBeenCalledTimes(5)
    expect(peak).toBe(2)
  })

  it('backs off a failed PUT without starting duplicate retries', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('', { status: 503 })).mockResolvedValue(new Response('', { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    const upload = queuedUpload('retry', 'https://storage.test/retry', { method: 'PUT' })
    await flush()
    expect(fetcher).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1300)
    expect((await upload).ok).toBe(true)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('keeps application reads across navigation but rejects them after session cleanup', async () => {
    const completions: ((r: Response) => void)[] = []
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => completions.push(resolve))))
    const owner = new AbortController()
    const shared = request('/api/model-profiles', { signal: owner.signal }, 'application')
    cancelPageReads()
    window.history.pushState(null, '', '/models')
    completions.shift()!(Response.json({ items: ['model'] }))
    expect(await shared).toEqual({ items: ['model'] })
    const stale = request('/api/model-profiles', { signal: owner.signal }, 'application')
    const rejected = expect(stale).rejects.toMatchObject({ name: 'AbortError' })
    owner.abort()
    completions.shift()!(Response.json({ items: ['stale'] }))
    await rejected
  })

  it('rejects late GET bodies even if fetch ignores abort, while POST survives navigation', async () => {
    let resolveGet!: (r: Response) => void
    let resolvePost!: (r: Response) => void
    vi.stubGlobal('fetch', vi.fn((_url, options: RequestInit) => new Promise<Response>((resolve) => {
      if (options.method === 'POST') resolvePost = resolve
      else resolveGet = resolve
    })))
    const get = request('/api/generation-tasks/1')
    const rejected = expect(get).rejects.toMatchObject({ name: 'AbortError' })
    const post = request('/api/generation-tasks/1/cancel', { method: 'POST' })
    cancelPageReads()
    resolveGet(Response.json({ old: true }))
    resolvePost(Response.json({ ok: true }))
    await rejected
    expect(await post).toEqual({ ok: true })
  })
})


describe('silent collection refresh', () => {
  it('uses at most ten cheap checks and one list read in five idle minutes', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const revision = vi.fn(async () => ({ revision: 1 }))
    const refresh = vi.fn(async () => undefined)
    renderHook(() => useSilentRefresh({ enabled: true, resourceKey: 'poses', revision, refresh }))
    await act(async () => { await vi.advanceTimersByTimeAsync(300000) })
    expect(revision).toHaveBeenCalledTimes(10)
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('never overlaps slow reads, aborts on resource changes, and retries failed versions', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    let resolve!: () => void
    const revision = vi.fn(async () => 1)
    const refresh = vi.fn((_signal: AbortSignal) => new Promise<void>((done) => { resolve = done }))
    const { rerender, unmount } = renderHook(({ key }) => useSilentRefresh({ enabled: true, active: true, resourceKey: key, revision, refresh }), { initialProps: { key: 'page-1' } })
    await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(revision).toHaveBeenCalledTimes(1)
    rerender({ key: 'page-2' })
    expect(refresh.mock.calls[0][0].aborted).toBe(true)
    await act(async () => resolve())
    refresh.mockRejectedValueOnce(new Error('temporary'))
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    refresh.mockResolvedValue(undefined)
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(refresh).toHaveBeenCalledTimes(3)
    unmount()
    await vi.advanceTimersByTimeAsync(60000)
    expect(refresh).toHaveBeenCalledTimes(3)
  })

  it('pauses offline and backs off persistent failures to two minutes', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    let online = false
    vi.spyOn(navigator, 'onLine', 'get').mockImplementation(() => online)
    const revision = vi.fn(async () => { throw new Error('unreachable') })
    const refresh = vi.fn(async () => undefined)
    renderHook(() => useSilentRefresh({ enabled: true, active: true, resourceKey: 'poses', revision, refresh }))
    await act(async () => { await vi.advanceTimersByTimeAsync(120000) })
    expect(revision).not.toHaveBeenCalled()
    await act(async () => { online = true; window.dispatchEvent(new Event('online')) })
    expect(revision).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(300000) })
    expect(revision.mock.calls.length).toBeLessThanOrEqual(8)
    expect(refresh).not.toHaveBeenCalled()
  })
})
