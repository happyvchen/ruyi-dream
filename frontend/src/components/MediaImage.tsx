import { useEffect, useRef, useState } from 'react'
import type { ImgHTMLAttributes } from 'react'

// Keep the image box measurable without a broken-image icon or a remote request.
const pendingImage = 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='

export function MediaImage(props: ImgHTMLAttributes<HTMLImageElement>) {
  // Visibility and failures belong to a source, not to a reused card position.
  return <SourceImage key={props.src} {...props} />
}

function SourceImage({ src, onLoad, onError, ...props }: ImgHTMLAttributes<HTMLImageElement>) {
  const ref = useRef<HTMLImageElement>(null)
  const [visible, setVisible] = useState(false)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    const node = ref.current
    if (!node) return
    if (typeof IntersectionObserver === 'undefined') { setVisible(true); return }
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) return
      setVisible(true)
      observer.disconnect()
    }, { rootMargin: '0px' })
    observer.observe(node)
    return () => observer.disconnect()
  }, [])
  if (!src || failed) return <span className="image-card-placeholder" role="img" aria-label={props.alt || '预览暂不可用'}>预览暂不可用</span>
  return <img {...props} ref={ref} src={visible ? src : pendingImage}
    alt={visible ? props.alt : ''} aria-busy={!visible || undefined}
    onLoad={visible ? onLoad : undefined}
    onError={visible ? (event) => { setFailed(true); onError?.(event) } : undefined}
    loading="lazy" decoding="async" fetchPriority="low" />
}
