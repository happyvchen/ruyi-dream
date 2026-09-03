import { useEffect, useState } from 'react'
import type { DragEvent, ReactNode } from 'react'
import { FileImage, ImagePlus, RefreshCw, Trash2 } from 'lucide-react'

export type ImageAssetCardProps = {
  thumbnailSrc?: string | null
  title: string
  meta?: string
  alt?: string
  badge?: string
  footer?: ReactNode
  accept?: string
  disabled?: boolean
  draggable?: boolean
  canDelete?: boolean
  onOpen?: () => void
  onImageLoad?: (dimensions: { width: number; height: number }) => void
  onReplace?: (file: File) => void
  onDelete?: () => void
  onDragStart?: () => void
  onDragOver?: (event: DragEvent<HTMLDivElement>) => void
  onDrop?: () => void
}

export function ImageAssetCard({
  thumbnailSrc,
  title,
  meta,
  alt,
  badge,
  footer,
  accept,
  disabled = false,
  draggable = false,
  canDelete = true,
  onOpen,
  onImageLoad,
  onReplace,
  onDelete,
  onDragStart,
  onDragOver,
  onDrop,
}: ImageAssetCardProps) {
  const [failed, setFailed] = useState(false)
  useEffect(() => setFailed(false), [thumbnailSrc])

  return (
    <div
      className="saved-image-card image-asset-card"
      draggable={draggable && !disabled}
      onDragStart={draggable && !disabled ? onDragStart : undefined}
      onDragOver={draggable && !disabled ? onDragOver : undefined}
      onDrop={draggable && !disabled ? onDrop : undefined}
    >
      {onOpen ? (
        <button className="image-open" type="button" onClick={onOpen} title="放大查看" disabled={!thumbnailSrc || failed}>
          {thumbnailSrc && !failed
            ? <img src={thumbnailSrc} alt={alt ?? title} loading="lazy" onError={() => setFailed(true)} onLoad={(event) => onImageLoad?.({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })} />
            : <span className="image-card-placeholder"><FileImage size={24} />图片不可用</span>}
        </button>
      ) : thumbnailSrc && !failed ? (
        <img src={thumbnailSrc} alt={alt ?? title} loading="lazy" onError={() => setFailed(true)} onLoad={(event) => onImageLoad?.({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })} />
      ) : (
        <span className="image-card-placeholder"><FileImage size={24} />图片不可用</span>
      )}
      {badge && <em className="thumb-pending-badge">{badge}</em>}
      <span title={title}>{title}{meta && <small title={meta}>{meta}</small>}</span>
      {footer}
      {(onReplace || onDelete) && (
        <div className="thumb-actions">
          {onReplace && (
            <label className={disabled ? 'is-disabled' : ''} title="替换图片" aria-label={`替换 ${title}`}>
              <RefreshCw size={14} />
              <input
                disabled={disabled}
                type="file"
                accept={accept}
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  event.currentTarget.value = ''
                  if (file) onReplace(file)
                }}
              />
            </label>
          )}
          {onDelete && (
            <button type="button" disabled={disabled || !canDelete} onClick={onDelete} title="删除图片" aria-label={`删除 ${title}`}>
              <Trash2 size={14} />
            </button>
          )}
        </div>
      )}
    </div>
  )
}

export function ImageUploadPlaceholder({
  accept,
  disabled = false,
  multiple = true,
  remaining,
  label = '添加图片',
  onFiles,
}: {
  accept?: string
  disabled?: boolean
  multiple?: boolean
  remaining?: number
  label?: string
  onFiles: (files: File[]) => void
}) {
  return (
    <label className={`saved-image-card image-upload-placeholder ${disabled ? 'is-disabled' : ''}`} aria-label={label} title={label}>
      <span className="image-upload-placeholder-content">
        <ImagePlus size={28} />
        <strong>{label}</strong>
        {typeof remaining === 'number' && <small>还可添加 {remaining} 张</small>}
      </span>
      <input
        type="file"
        accept={accept}
        multiple={multiple}
        disabled={disabled}
        onChange={(event) => {
          const selected = Array.from(event.currentTarget.files ?? [])
          event.currentTarget.value = ''
          const files = typeof remaining === 'number' ? selected.slice(0, Math.max(0, remaining)) : selected
          if (files.length) onFiles(files)
        }}
      />
    </label>
  )
}
