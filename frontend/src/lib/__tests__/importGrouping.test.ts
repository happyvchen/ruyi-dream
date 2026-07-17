import { describe, expect, it, vi } from 'vitest'
import {
  formalAssetFiles,
  groupImportFiles,
  importPath,
  isSupportedImageFile,
  sanitizeClientName,
} from '../importGrouping'

function file(name: string, type = 'image/jpeg', path?: string) {
  const item = new File(['x'], name, { type }) as File & { webkitRelativePath?: string }
  if (path) Object.defineProperty(item, 'webkitRelativePath', { value: path })
  return item
}

describe('import grouping business rules', () => {
  it('treats loose images as one new pending item', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 6, 17, 8, 9, 10))
    const result = groupImportFiles([
      file('front.jpg'),
      file('back.png', 'image/png'),
      file('notes.txt', 'text/plain'),
    ])

    expect(result.batches).toHaveLength(1)
    expect(result.batches[0].code).toBe('20260717080910')
    expect(result.batches[0].files.map((item) => item.name)).toEqual(['back.png', 'front.jpg'])
    expect(result.batches[0].issues?.[0]).toMatchObject({
      code: 'ignored_non_image_files',
      count: 1,
    })
    vi.useRealTimers()
  })

  it('recognizes each leaf image directory as one product set', () => {
    const result = groupImportFiles([
      file('a.jpg', 'image/jpeg', 'batch/LOOK-001/a.jpg'),
      file('b.jpg', 'image/jpeg', 'batch/LOOK-001/b.jpg'),
      file('c.jpg', 'image/jpeg', 'batch/LOOK-002/c.jpg'),
    ])

    expect(result.batches.map((batch) => batch.code)).toEqual(['LOOK-001', 'LOOK-002'])
    expect(result.batches.map((batch) => batch.files.length)).toEqual([2, 1])
  })

  it('flags mixed directory structures instead of silently merging product sets', () => {
    const result = groupImportFiles([
      file('cover.jpg', 'image/jpeg', 'batch/cover.jpg'),
      file('a.jpg', 'image/jpeg', 'batch/LOOK-001/a.jpg'),
    ])

    expect(result.batches).toHaveLength(2)
    expect(result.batches.flatMap((batch) => batch.issues ?? []).map((issue) => issue.code)).toContain('mixed_directory')
  })

  it('supports the same static image formats for studio upload and library import', () => {
    expect(isSupportedImageFile(file('look.jpg', ''))).toBe(true)
    expect(isSupportedImageFile(file('look.jpeg', ''))).toBe(true)
    expect(isSupportedImageFile(file('look.png', ''))).toBe(true)
    expect(isSupportedImageFile(file('look.webp', ''))).toBe(true)
    expect(isSupportedImageFile(file('look.heic', ''))).toBe(true)
    expect(isSupportedImageFile(file('look.heif', ''))).toBe(true)
    expect(isSupportedImageFile(file('look.avif', ''))).toBe(true)
    expect(isSupportedImageFile(file('moving.gif', 'image/gif'))).toBe(false)
  })

  it('formal asset input keeps only the first fifteen supported images after user ordering', () => {
    const images = Array.from({ length: 100 }, (_, index) => file(`look-${String(index + 1).padStart(3, '0')}.jpg`))

    const selected = formalAssetFiles(images, 15)

    expect(selected).toHaveLength(15)
    expect(selected[0].name).toBe('look-001.jpg')
    expect(selected.at(-1)?.name).toBe('look-015.jpg')
  })

  it('uses relative folder path when provided by folder picker or drag and drop', () => {
    expect(importPath(file('front.jpg', 'image/jpeg', '/batch/LOOK-001/front.jpg'))).toBe('batch/LOOK-001/front.jpg')
  })

  it('sanitizes business names without destroying Chinese product codes', () => {
    expect(sanitizeClientName('  Lily 夏季/LOOK 001  ')).toBe('Lily-夏季-LOOK-001')
  })
})
