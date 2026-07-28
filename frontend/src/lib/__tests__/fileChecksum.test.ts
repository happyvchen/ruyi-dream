import { afterEach, describe, expect, it, vi } from 'vitest'
import { CHECKSUM_COMPATIBILITY_NOTICE, sha256File } from '../fileChecksum'

function testFile(contents: string, name = 'test.jpg') {
  const bytes = new TextEncoder().encode(contents)
  return {
    name,
    arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
  } as File
}

describe('file SHA-256 checksum', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('uses native Web Crypto when digest is available', async () => {
    const digest = vi.fn(async () => new Uint8Array([0, 15, 255]).buffer)
    const onCompatibilityMode = vi.fn()
    vi.stubGlobal('crypto', { subtle: { digest } })

    await expect(sha256File(testFile('abc'), { onCompatibilityMode })).resolves.toBe('000fff')
    expect(digest).toHaveBeenCalledWith('SHA-256', expect.any(Uint8Array))
    expect(onCompatibilityMode).not.toHaveBeenCalled()
  })

  it('uses the compatible implementation when crypto.subtle is unavailable', async () => {
    const onCompatibilityMode = vi.fn()
    vi.stubGlobal('crypto', {})

    await expect(sha256File(testFile('abc'), { onCompatibilityMode })).resolves.toBe(
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    )
    expect(onCompatibilityMode).toHaveBeenCalledOnce()
    expect(CHECKSUM_COMPATIBILITY_NOTICE).toContain('HTTP 内网访问')
  })

  it('falls back when an exposed native digest rejects the operation', async () => {
    const onCompatibilityMode = vi.fn()
    vi.stubGlobal('crypto', { subtle: { digest: vi.fn().mockRejectedValue(new DOMException('blocked')) } })

    await expect(sha256File(testFile('abc'), { onCompatibilityMode })).resolves.toBe(
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    )
    expect(onCompatibilityMode).toHaveBeenCalledOnce()
  })

  it('turns file read failures into a user-facing message', async () => {
    const file = { name: 'look-01.jpg', arrayBuffer: vi.fn().mockRejectedValue(new Error('raw read failure')) } as unknown as File

    await expect(sha256File(file)).rejects.toThrow('无法读取图片“look-01.jpg”，请确认文件未被移动或占用后重新选择。')
  })
})
