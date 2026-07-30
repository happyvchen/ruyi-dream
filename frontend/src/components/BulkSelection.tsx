import { useEffect, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent } from 'react'

export type SelectableId = string | number
export type MarqueeBox = { left: number; top: number; width: number; height: number } | null

export function selectableIdsInDom<T extends SelectableId>(container: HTMLElement | null): T[] {
  if (!container) return []
  return Array.from(container.querySelectorAll<HTMLElement>('[data-select-id]'))
    .filter((item) => item.dataset.selectDisabled !== 'true')
    .map((item) => (item.dataset.selectType === 'number' ? Number(item.dataset.selectId) : item.dataset.selectId) as T)
}

export function updateSelectionWithRange<T extends SelectableId>({
  current,
  orderedIds,
  id,
  checked,
  extend,
  anchor,
}: {
  current: ReadonlySet<T>
  orderedIds: readonly T[]
  id: T
  checked: boolean
  extend: boolean
  anchor: T | null
}) {
  const next = new Set(current)
  if (extend && anchor !== null) {
    const start = orderedIds.indexOf(anchor)
    const end = orderedIds.indexOf(id)
    if (start >= 0 && end >= 0) {
      orderedIds.slice(Math.min(start, end), Math.max(start, end) + 1).forEach((rangeId) => {
        if (checked) next.add(rangeId)
        else next.delete(rangeId)
      })
      return next
    }
  }
  if (checked) next.add(id)
  else next.delete(id)
  return next
}

export function useMarqueeSelection<T extends SelectableId>({ enabled, selected, onChange, deferUntilPointerUp = false }: {
  enabled: boolean
  selected: ReadonlySet<T>
  onChange: (ids: Set<T>) => void
  deferUntilPointerUp?: boolean
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [box, setBox] = useState<MarqueeBox>(null)
  const [previewSelected, setPreviewSelected] = useState<Set<T> | null>(null)
  const selectedRef = useRef(selected)
  const suppressClickRef = useRef(false)
  const cleanupRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    selectedRef.current = selected
  }, [selected])

  useEffect(() => () => cleanupRef.current?.(), [])

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (!enabled || event.button !== 0 || event.pointerType === 'touch') return
    const target = event.target as HTMLElement
    if (target.closest('a, input, label, button:not(.record-open), [data-no-marquee]')) return
    const container = containerRef.current
    if (!container) return
    const start = { x: event.clientX, y: event.clientY }
    const additive = event.metaKey || event.ctrlKey || event.shiftKey
    const original = new Set(selectedRef.current)
    const base = additive ? new Set(selectedRef.current) : new Set<T>()
    let dragging = false
    let pending = new Set(base)

    const move = (pointerEvent: PointerEvent) => {
      const distance = Math.hypot(pointerEvent.clientX - start.x, pointerEvent.clientY - start.y)
      if (!dragging && distance < 5) return
      dragging = true
      suppressClickRef.current = true
      document.body.classList.add('is-marquee-selecting')
      const bounds = container.getBoundingClientRect()
      const left = Math.min(start.x, pointerEvent.clientX)
      const top = Math.min(start.y, pointerEvent.clientY)
      const right = Math.max(start.x, pointerEvent.clientX)
      const bottom = Math.max(start.y, pointerEvent.clientY)
      setBox({
        left: left - bounds.left + container.scrollLeft,
        top: top - bounds.top + container.scrollTop,
        width: right - left,
        height: bottom - top,
      })
      pending = new Set(base)
      container.querySelectorAll<HTMLElement>('[data-select-id]').forEach((item) => {
        if (item.dataset.selectDisabled === 'true') return
        const rect = item.getBoundingClientRect()
        const intersects = rect.left < right && rect.right > left && rect.top < bottom && rect.bottom > top
        const rawId = item.dataset.selectId
        if (!intersects || rawId === undefined) return
        const id = (item.dataset.selectType === 'number' ? Number(rawId) : rawId) as T
        pending.add(id)
      })
      setPreviewSelected(pending)
      if (!deferUntilPointerUp) onChange(pending)
    }
    const stop = (commit: boolean, restore: boolean) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', finish)
      window.removeEventListener('pointercancel', cancel)
      document.body.classList.remove('is-marquee-selecting')
      setBox(null)
      if (dragging && commit && deferUntilPointerUp) onChange(pending)
      if (dragging && restore && !deferUntilPointerUp) onChange(original)
      setPreviewSelected(null)
      cleanupRef.current = null
      if (dragging) window.setTimeout(() => { suppressClickRef.current = false }, 0)
    }
    const finish = () => stop(true, false)
    const cancel = () => stop(false, true)
    cleanupRef.current?.()
    cleanupRef.current = () => stop(false, false)
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', finish)
    window.addEventListener('pointercancel', cancel)
  }

  function onClickCapture(event: ReactMouseEvent<HTMLDivElement>) {
    if (!suppressClickRef.current) return
    event.preventDefault()
    event.stopPropagation()
  }

  return {
    containerRef,
    box,
    selectedForDisplay: previewSelected ?? selected,
    onPointerDown,
    onClickCapture,
  }
}

export function MarqueeSelectionBox({ box }: { box: MarqueeBox }) {
  if (!box) return null
  return <span className="marquee-selection-box" style={box} aria-hidden="true" />
}

export function BulkCheckbox({ checked, indeterminate = false, disabled = false, label, onChange }: {
  checked: boolean
  indeterminate?: boolean
  disabled?: boolean
  label: string
  onChange: (checked: boolean, extend: boolean) => void
}) {
  const ref = useRef<HTMLInputElement | null>(null)
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate
  }, [indeterminate])

  return (
    <input
      ref={ref}
      type="checkbox"
      checked={checked}
      disabled={disabled}
      aria-checked={indeterminate ? 'mixed' : checked}
      onChange={(event) => onChange(event.target.checked, event.nativeEvent instanceof MouseEvent && event.nativeEvent.shiftKey)}
      aria-label={label}
    />
  )
}
