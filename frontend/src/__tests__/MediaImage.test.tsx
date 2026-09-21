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
