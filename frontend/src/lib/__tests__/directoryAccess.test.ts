/* @vitest-environment jsdom */
import { describe, expect, it } from 'vitest'
import { filesFromDirectory } from '../directoryAccess'
import type { RuyiFileSystemDirectoryHandle } from '../directoryAccess'

function directory(name: string, entries: unknown[]): RuyiFileSystemDirectoryHandle {
  return {
    kind: 'directory',
    name,
    async *values() {
      for (const entry of entries) yield entry as never
    },
    queryPermission: async () => 'granted',
    requestPermission: async () => 'granted',
  }
}

describe('directory upload recovery', () => {
  it('rebuilds stable root-relative paths after a directory handle is restored', async () => {
    const front = new File(['front'], 'front.jpg', { type: 'image/jpeg', lastModified: 1000 })
    const back = new File(['back'], 'back.jpg', { type: 'image/jpeg', lastModified: 2000 })
    const root = directory('7.20回图', [
      directory('LOOK-001', [
        { kind: 'file', name: 'front.jpg', getFile: async () => front },
        { kind: 'file', name: 'back.jpg', getFile: async () => back },
      ]),
    ])

    const files = await filesFromDirectory(root)

    expect(files.map((file) => (file as File & { relativePath: string }).relativePath)).toEqual([
      '7.20回图/LOOK-001/front.jpg',
      '7.20回图/LOOK-001/back.jpg',
    ])
  })
})
