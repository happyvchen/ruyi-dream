import { useEffect, useRef, useState } from 'react'
import type { ImgHTMLAttributes } from 'react'

// At most twelve unique remote sources before the user scrolls or opens another view.
let epoch = 0
let admitted = new Set<string>()
const listeners = new Set<() => void>()
export function resetMediaBudget() {
  epoch += 1
  admitted = new Set()
  listeners.forEach((notify) => notify())
}
if (typeof window !== 'undefined') {
  window.addEventListener('scroll', resetMediaBudget, { capture: true, passive: true })
  window.addEventListener('popstate', resetMediaBudget)
  window.addEventListener('click', resetMediaBudget, { capture: true, passive: true })
}
export function MediaImage({ src, ...props }: ImgHTMLAttributes<HTMLImageElement>) {
  const ref = useRef<HTMLImageElement>(null)
  const [visible, setVisible] = useState(false)
  const [budgetEpoch, setBudgetEpoch] = useState(epoch)
  const [loaded, setLoaded] = useState<string>()
  const [failedSrc, setFailedSrc] = useState<string>()
  useEffect(() => {
    const notify = () => setBudgetEpoch(epoch)
    listeners.add(notify)
    return () => { listeners.delete(notify) }
  }, [])
  useEffect(() => {
    setVisible(false)
    const node = ref.current
    if (!node) return
    if (typeof IntersectionObserver === 'undefined') { setVisible(true); return }
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { rootMargin: '0px' })
    observer.observe(node)
    return () => observer.disconnect()
  }, [src])
  useEffect(() => {
    if (!src || !visible || loaded === src) return
    const local = /^(blob:|data:)/.test(src)
    if (local || admitted.has(src) || admitted.size < 12) {
      if (!local) admitted.add(src)
      setLoaded(src)
    }
  }, [src, visible, budgetEpoch, loaded])
  if (!src || failedSrc === src) return <span className="image-card-placeholder" role="img" aria-label={props.alt || '预览暂不可用'}>预览暂不可用</span>
  return <img {...props} onError={(event) => { setFailedSrc(src); props.onError?.(event) }} ref={ref} src={loaded === src ? src : undefined} loading="lazy" decoding="async" fetchPriority="low" />
}
