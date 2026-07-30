/* @vitest-environment jsdom */
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { MarqueeSelectionBox, useMarqueeSelection } from '../components/BulkSelection'

afterEach(cleanup)

function SelectionHarness({ deferred }: { deferred: boolean }) {
  const [selected, setSelected] = useState<Set<number>>(() => new Set([1]))
  const marquee = useMarqueeSelection<number>({ enabled: true, selected, onChange: setSelected, deferUntilPointerUp: deferred })
  return (
    <div>
      <output aria-label="selected">{Array.from(selected).sort().join(',')}</output>
      <div ref={marquee.containerRef} data-testid="surface" onPointerDown={marquee.onPointerDown} onClickCapture={marquee.onClickCapture}>
        <MarqueeSelectionBox box={marquee.box} />
        <div data-select-id="1" data-select-type="number">一</div>
        <div data-select-id="2" data-select-type="number">二</div>
      </div>
    </div>
  )
}

function setSelectionRects() {
  const surface = screen.getByTestId('surface')
  const items = Array.from(surface.querySelectorAll<HTMLElement>('[data-select-id]'))
  surface.getBoundingClientRect = () => ({ left: 0, top: 0, right: 300, bottom: 300, width: 300, height: 300, x: 0, y: 0, toJSON: () => ({}) })
  items[0]!.getBoundingClientRect = () => ({ left: 10, top: 10, right: 100, bottom: 70, width: 90, height: 60, x: 10, y: 10, toJSON: () => ({}) })
  items[1]!.getBoundingClientRect = () => ({ left: 10, top: 90, right: 100, bottom: 150, width: 90, height: 60, x: 10, y: 90, toJSON: () => ({}) })
  return surface
}

describe('shared bulk marquee selection', () => {
  it('does not commit a deferred marquee when the pointer is cancelled', () => {
    render(<SelectionHarness deferred />)
    const surface = setSelectionRects()

    fireEvent.pointerDown(surface, { button: 0, pointerType: 'mouse', clientX: 5, clientY: 80 })
    fireEvent.pointerMove(window, { pointerType: 'mouse', clientX: 105, clientY: 155 })
    fireEvent.pointerCancel(window, { pointerType: 'mouse', clientX: 105, clientY: 155 })

    expect(screen.getByLabelText('selected')).toHaveTextContent('1')
  })

  it('restores the previous live selection when the pointer is cancelled', () => {
    render(<SelectionHarness deferred={false} />)
    const surface = setSelectionRects()

    fireEvent.pointerDown(surface, { button: 0, pointerType: 'mouse', clientX: 5, clientY: 80 })
    fireEvent.pointerMove(window, { pointerType: 'mouse', clientX: 105, clientY: 155 })
    expect(screen.getByLabelText('selected')).toHaveTextContent('2')
    fireEvent.pointerCancel(window, { pointerType: 'mouse', clientX: 105, clientY: 155 })

    expect(screen.getByLabelText('selected')).toHaveTextContent('1')
  })
})
