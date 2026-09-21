import { useEffect, useRef, useState } from 'react'
import type { ImgHTMLAttributes } from 'react'

// Reserve the image box without requesting a remote asset before it is visible.
const pendingImage = 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='
type MediaImageProps = ImgHTMLAttributes<HTMLImageElement> & {
  onOpen?: () => void
  openLabel?: string
  // Decorative thumbnails inside a navigation button must not add a nested button.
  retryable?: boolean
}

export function MediaImage(props: MediaImageProps) {
  return <SourceImage key={props.src} {...props} />
}

function SourceImage({ src, onLoad, onError, onOpen, openLabel = '放大查看', retryable = true, ...props }: MediaImageProps) {
  const ref = useRef<HTMLImageElement>(null)
  const [visible, setVisible] = useState(false)
  const [state, setState] = useState<'loading' | 'loaded' | 'failed'>('loading')
  const [attempt, setAttempt] = useState(0)
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
  const unavailable = !src || state === 'failed'
  const loading = !!src && state === 'loading'
  return <span className={`media-image ${loading ? 'is-loading' : ''}`} aria-busy={loading || undefined}>
    {src && <img {...props} key={attempt} ref={ref} src={visible ? src : pendingImage}
      alt={visible ? props.alt : ''} style={{ ...props.style, visibility: state === 'loaded' ? 'visible' : 'hidden' }}
      onLoad={visible ? (event) => {
        // Ignore a late load event from the local placeholder.
        if (event.currentTarget.currentSrc === pendingImage) return
        setState('loaded'); onLoad?.(event)
      } : undefined}
      onError={visible ? (event) => { setState('failed'); onError?.(event) } : undefined}
      loading="lazy" decoding="async" fetchPriority="low" />}
    {onOpen && <button className="media-image-open" type="button" onClick={onOpen} title={openLabel} aria-label={openLabel} />}
    {loading && <span className="media-image-feedback" aria-hidden="true">图片加载中</span>}
    {unavailable && <span className="media-image-feedback is-error">
      <span>预览暂不可用</span>
      {src && retryable && <button type="button" className="media-image-retry" aria-label={`重新加载图片${props.alt ? `：${props.alt}` : ''}`}
        onPointerDown={(event) => event.stopPropagation()}
        onClick={(event) => {
          event.stopPropagation()
          setState('loading')
          setAttempt((value) => value + 1)
        }}>重试</button>}
    </span>}
  </span>
}
