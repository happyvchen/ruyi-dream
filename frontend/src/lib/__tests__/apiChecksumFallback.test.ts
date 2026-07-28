/* @vitest-environment jsdom */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api'

describe('upload session checksum fallback', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('submits a SHA-256 checksum when HTTP-like browsers do not expose crypto.subtle', async () => {
    vi.stubGlobal('crypto', {})
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      session: { id: 'session-1', status: 'PENDING', totalCount: 1, uploadedCount: 0 },
      items: [],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const onChecksumCompatibilityMode = vi.fn()
    const bytes = new TextEncoder().encode('abc')
    const file = {
      name: 'look.jpg',
      type: 'image/jpeg',
      size: bytes.byteLength,
      lastModified: 123,
      arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
    } as File

    await api.resumeUploadSession('session-1', [file], { onChecksumCompatibilityMode })

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const payload = JSON.parse(String(init.body))
    expect(payload.files[0].checksumSha256).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
    expect(onChecksumCompatibilityMode).toHaveBeenCalledOnce()
  })
})
