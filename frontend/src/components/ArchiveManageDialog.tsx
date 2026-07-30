import { useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

export type ArchiveDialogItem = {
  id: number | string
  name: string
}

export type ArchiveManageAction = 'archive' | 'restore' | 'purge'

export type ArchiveManageOption = {
  value: ArchiveManageAction
  label: string
}

export type ArchiveManageDialogProps = {
  title: string
  items: ArchiveDialogItem[]
  description: ReactNode | ((purge: boolean) => ReactNode)
  warning?: ReactNode
  impact?: ReactNode
  impactLoading?: boolean
  impactError?: ReactNode
  onRetryImpact?: () => void
  purgeReady?: boolean
  allowPurge?: boolean
  archiveAvailable?: boolean
  initialPurge?: boolean
  actions?: ArchiveManageOption[]
  initialAction?: ArchiveManageAction
  unavailablePurgeText?: ReactNode
  expectedText?: string
  onClose: () => void
  onConfirm?: (purge: boolean, confirmText: string) => void | Promise<void>
  onConfirmAction?: (action: ArchiveManageAction, confirmText: string) => void | Promise<void>
  onError?: (error: unknown) => void
}

export function ArchiveManageDialog({
  title,
  items,
  description,
  warning,
  impact,
  impactLoading = false,
  impactError,
  onRetryImpact,
  purgeReady = true,
  allowPurge = true,
  archiveAvailable = true,
  initialPurge = false,
  actions,
  initialAction,
  unavailablePurgeText,
  expectedText = items.length === 1 ? items[0]?.name ?? '' : '全部删除',
  onClose,
  onConfirm,
  onConfirmAction,
  onError,
}: ArchiveManageDialogProps) {
  const defaultAction = initialAction ?? (initialPurge || !archiveAvailable ? 'purge' : 'archive')
  const [action, setAction] = useState<ArchiveManageAction>(defaultAction)
  const purge = action === 'purge'
  const [confirmText, setConfirmText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const confirmInputRef = useRef<HTMLInputElement | null>(null)
  const dialogRef = useRef<HTMLFormElement | null>(null)
  const returnFocusRef = useRef<HTMLElement | null>(null)
  const itemResetKey = JSON.stringify(items.map((item) => [item.id, item.name]))
  const canConfirm = !submitting
    && (actions?.length ? !!onConfirmAction : !!onConfirm)
    && (actions?.length ? actions.some((option) => option.value === action) : archiveAvailable || purge)
    && (!purge || (allowPurge && purgeReady && !impactLoading && !impactError && confirmText === expectedText))

  useEffect(() => {
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    return () => returnFocusRef.current?.focus()
  }, [])

  useEffect(() => {
    setAction(initialAction ?? (initialPurge || !archiveAvailable ? 'purge' : 'archive'))
    setConfirmText('')
    setSubmitting(false)
  }, [archiveAvailable, initialAction, initialPurge, itemResetKey])

  useEffect(() => {
    if (purge) confirmInputRef.current?.focus()
    else dialogRef.current?.focus()
  }, [purge])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) onClose()
      if (event.key !== 'Tab') return
      const dialog = dialogRef.current
      if (!dialog) return
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'))
      if (!focusable.length) {
        event.preventDefault()
        dialog.focus()
        return
      }
      const first = focusable[0]
      const last = focusable.at(-1)!
      if (!dialog.contains(document.activeElement)) {
        event.preventDefault()
        first.focus()
      } else if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose, submitting])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!canConfirm) return
    setSubmitting(true)
    try {
      if (actions && onConfirmAction) await onConfirmAction(action, confirmText)
      else if (onConfirm) await onConfirm(purge, confirmText)
    } catch (error) {
      setSubmitting(false)
      onError?.(error)
    }
  }

  return createPortal(
    <div className="modal-backdrop" role="presentation" onClick={() => { if (!submitting) onClose() }}>
      <form ref={dialogRef} tabIndex={-1} className="confirm-modal" role="dialog" aria-modal="true" aria-label={title} onSubmit={submit} onClick={(event) => event.stopPropagation()}>
        <div className="confirm-head">
          <h2>{title}</h2>
          <button type="button" className="ghost-icon" disabled={submitting} onClick={onClose} title="关闭"><X size={18} /></button>
        </div>
        <div className="confirm-copy">{typeof description === 'function' ? description(purge) : description}</div>
        {warning && <div className="confirm-warning" role="alert">{warning}</div>}
        <div className="confirm-items">
          {items.slice(0, 4).map((item) => <span key={item.id}>{item.name}</span>)}
          {items.length > 4 && <span>另 {items.length - 4} 项</span>}
        </div>
        {actions?.length ? (
          <fieldset className="archive-action-options" disabled={submitting}>
            <legend>选择操作</legend>
            {actions.map((option) => (
              <label key={option.value} className={option.value === 'purge' ? 'is-danger' : ''}>
                <input
                  type="radio"
                  name="archive-action"
                  value={option.value}
                  checked={action === option.value}
                  onChange={() => {
                    setAction(option.value)
                    setConfirmText('')
                  }}
                />
                <span>{option.label}</span>
              </label>
            ))}
          </fieldset>
        ) : archiveAvailable && (
          <label className={`danger-check ${allowPurge ? '' : 'is-disabled'}`}>
            <input type="checkbox" checked={purge} disabled={!allowPurge || submitting} onChange={(event) => {
              setAction(event.target.checked ? 'purge' : 'archive')
              setConfirmText('')
            }} />
            同时彻底删除关联记录和文件
          </label>
        )}
        {!allowPurge && unavailablePurgeText && <div className="confirm-copy">{unavailablePurgeText}</div>}
        {purge && allowPurge && (
          <>
            <div className="confirm-copy">
              {impactLoading
                ? '正在计算影响范围...'
                : impactError
                  ? <>{impactError}{onRetryImpact && <> <button type="button" className="text-button" onClick={onRetryImpact}>重试</button></>}</>
                  : impact}
            </div>
            <label className="confirm-field">
              <span>{expectedText === '全部删除' ? '输入“全部删除”确认彻底删除' : '输入完整名称确认彻底删除'}</span>
              <input ref={confirmInputRef} value={confirmText} onChange={(event) => setConfirmText(event.target.value)} placeholder={expectedText} />
            </label>
          </>
        )}
        <div className="confirm-actions">
          <button type="button" className="management-action" disabled={submitting} onClick={onClose}>取消</button>
          <button className={purge ? 'danger-action' : 'management-action'} disabled={!canConfirm}>
            {submitting ? '处理中...' : purge ? '彻底删除' : action === 'restore' ? '确认取消归档' : '确认归档'}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  )
}
