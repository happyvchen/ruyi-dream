import { useEffect, useState } from 'react'
import { Loader2, X } from 'lucide-react'

// Each notification uses its event ID as the React key, including repeated messages.
export function TransientNotice({ message, tone }: { message: string; tone: 'warning' | 'info' }) {
  const [remaining, setRemaining] = useState(tone === 'warning' ? 8 : 5)
  const [dismissed, setDismissed] = useState(false)
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const paused = hovered || focused
  const expired = remaining === 0

  useEffect(() => {
    if (dismissed || expired || paused) return
    const timer = window.setInterval(() => setRemaining(value => Math.max(0, value - 1)), 1000)
    return () => window.clearInterval(timer)
  }, [dismissed, expired, paused])

  if (dismissed || expired) return null
  return (
    <div className={tone === 'warning' ? 'toast' : 'busy'}
      onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)} onBlur={event => {
        if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false)
      }}>
      {tone === 'info' && <Loader2 size={16} className="spin notice-icon" aria-hidden="true" />}
      <div className="notice-copy">
        <div role={tone === 'warning' ? 'alert' : 'status'}>{message.length > 300 ? `${message.slice(0, 300)}…` : message}</div>
        <span className="notice-countdown" aria-hidden="true">{paused ? '暂停自动关闭' : `${remaining} 秒后自动关闭`}</span>
      </div>
      <button type="button" className="notice-close" aria-label={tone === 'warning' ? '关闭告警' : '关闭提示'} onClick={() => setDismissed(true)}><X size={16} /></button>
    </div>
  )
}
