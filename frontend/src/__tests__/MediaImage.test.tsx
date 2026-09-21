/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { MediaImage } from '../components/MediaImage'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function observeImages() {
  const callbacks: ((entries: { isIntersecting: boolean }[]) => void)[] = []
  vi.stubGlobal('IntersectionObserver', class {
    constructor(callback: (entries: { isIntersecting: boolean }[]) => void) { callbacks.push(callback) }
    observe() {}
    disconnect() {}
  })
  return callbacks
}

it('loads every visible image without clicks or scrolling and defers offscreen images', () => {
  const observers = observeImages()
  const { container } = render(<div>{Array.from({ length: 30 }, (_, id) => <MediaImage key={id} src={`/thumb-${id}.webp`} alt={`Photo ${id}`} />)}</div>)
  expect(container.querySelectorAll('img[src^="/thumb-"]')).toHaveLength(0)
  act(() => observers.slice(0, 24).forEach(callback => callback([{ isIntersecting: true }])))
  container.querySelectorAll('img[src^="/thumb-"]').forEach(img => fireEvent.load(img))
  expect(container.querySelectorAll('img[src^="/thumb-"]')).toHaveLength(24)
  expect(container.querySelector('img[src="/thumb-29.webp"]')).toBeNull()
  act(() => observers[29]([{ isIntersecting: true }]))
  expect(container.querySelector('img[src="/thumb-29.webp"]')).not.toBeNull()
})

it('newly mounted views do not inherit another view’s exhausted allowance', () => {
  vi.stubGlobal('IntersectionObserver', undefined)
  const first = render(<div>{Array.from({ length: 12 }, (_, id) => <MediaImage key={id} src={`/old-${id}`} />)}</div>)
  first.unmount()
  const next = render(<MediaImage src="/new" />)
  expect(next.container.querySelector('img')?.getAttribute('src')).toBe('/new')
})

it('recovers after a failed source is replaced and preserves load/error callbacks', () => {
  vi.stubGlobal('IntersectionObserver', undefined)
  const onLoad = vi.fn()
  const onError = vi.fn()
  const { container, rerender } = render(<MediaImage src="/bad" onLoad={onLoad} onError={onError} />)
  fireEvent.error(container.querySelector('img')!)
  expect(onError).toHaveBeenCalledTimes(1)
  expect(container.textContent).toContain('预览暂不可用')
  rerender(<MediaImage src="/good" onLoad={onLoad} onError={onError} />)
  expect(container.querySelector('img')?.getAttribute('src')).toBe('/good')
  fireEvent.load(container.querySelector('img')!)
  expect(onLoad).toHaveBeenCalledTimes(1)
})

it('uses a valid pending image and does not report its load as an asset load', () => {
  observeImages()
  const onLoad = vi.fn()
  const { container } = render(<MediaImage src="/pending" alt="Product" onLoad={onLoad} />)
  const img = container.querySelector('img')!
  expect(img.getAttribute('src')).toMatch(/^data:image\//)
  fireEvent.load(img)
  expect(onLoad).not.toHaveBeenCalled()
})

it('keeps loading feedback until the actual image loads and retries only that source', () => {
  vi.stubGlobal('IntersectionObserver', undefined)
  const onOpen = vi.fn()
  const { container, getByRole } = render(<MediaImage src="/thumb" alt="服装" onOpen={onOpen} />)
  const first = container.querySelector('img')!
  expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
  fireEvent.error(first)
  fireEvent.click(getByRole('button', { name: '重新加载图片：服装' }))
  expect(onOpen).not.toHaveBeenCalled()
  const retry = container.querySelector('img')!
  expect(retry).not.toBe(first)
  expect(retry.getAttribute('src')).toBe('/thumb')
  fireEvent.load(retry)
  expect(container.querySelector('[aria-busy="true"]')).toBeNull()
  expect(retry.style.visibility).toBe('visible')
  fireEvent.click(getByRole('button', { name: '放大查看' }))
  expect(onOpen).toHaveBeenCalledTimes(1)
})

it('does not add a retry button inside a decorative navigation thumbnail', () => {
  vi.stubGlobal('IntersectionObserver', undefined)
  const { container } = render(<button><MediaImage src="/thumb" retryable={false} /></button>)
  fireEvent.error(container.querySelector('img')!)
  expect(container.querySelector('button button')).toBeNull()
})

it('resets loaded feedback on source change and never substitutes an original for missing thumbnails', () => {
  vi.stubGlobal('IntersectionObserver', undefined)
  const { container, rerender } = render(<MediaImage src="/one" />)
  fireEvent.load(container.querySelector('img')!)
  rerender(<MediaImage src="/two" />)
  expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
  expect(container.querySelector('img')!.style.visibility).toBe('hidden')
  rerender(<MediaImage />)
  expect(container.querySelector('img')).toBeNull()
  expect(container.querySelector('button')).toBeNull()
})
