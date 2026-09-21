/* @vitest-environment jsdom */
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { MediaImage, resetMediaBudget } from '../components/MediaImage'

afterEach(() => { cleanup(); vi.unstubAllGlobals(); resetMediaBudget() })

it('admits no more than twelve cold images and keeps offscreen images unloaded', () => {
  const observers: ((entries: { isIntersecting: boolean }[]) => void)[] = []
  vi.stubGlobal('IntersectionObserver', class {
    constructor(callback: (entries: { isIntersecting: boolean }[]) => void) { observers.push(callback) }
    observe() {}
    disconnect() {}
  })
  resetMediaBudget()
  const { container } = render(<div>{Array.from({ length: 30 }, (_, id) => <MediaImage key={id} src={`/thumb-${id}.webp`} />)}</div>)
  expect(container.querySelectorAll('img[src]')).toHaveLength(0)
  act(() => observers.slice(0, 15).forEach((callback) => callback([{ isIntersecting: true }])))
  expect(container.querySelectorAll('img[src]')).toHaveLength(12)
  expect(container.querySelector('img[src="/thumb-29.webp"]')).toBeNull()
  act(() => window.dispatchEvent(new Event('scroll')))
  expect(container.querySelectorAll('img[src]')).toHaveLength(15)
  act(() => {
    observers[29]([{ isIntersecting: true }])
    window.dispatchEvent(new Event('scroll'))
  })
  expect(container.querySelector('img[src="/thumb-29.webp"]')).not.toBeNull()
})
