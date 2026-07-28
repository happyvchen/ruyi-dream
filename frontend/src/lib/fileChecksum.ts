export const CHECKSUM_COMPATIBILITY_NOTICE = '当前为 HTTP 内网访问，正在使用兼容方式校验图片，准备时间可能稍长。'

export type FileChecksumOptions = {
  onCompatibilityMode?: () => void
}

function toHex(bytes: Uint8Array) {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

export async function sha256File(file: File, options: FileChecksumOptions = {}) {
  let bytes: Uint8Array<ArrayBuffer>
  try {
    bytes = new Uint8Array(await file.arrayBuffer())
  } catch (error) {
    throw new Error(`无法读取图片“${file.name}”，请确认文件未被移动或占用后重新选择。`, { cause: error })
  }

  const subtle = globalThis.crypto?.subtle
  if (typeof subtle?.digest === 'function') {
    try {
      return toHex(new Uint8Array(await subtle.digest('SHA-256', bytes)))
    } catch {
      // Some embedded or restricted browsers expose Web Crypto but reject digest calls.
      // Fall through to the browser-independent implementation below.
    }
  }

  options.onCompatibilityMode?.()
  try {
    const { sha256 } = await import('@noble/hashes/sha2.js')
    return toHex(sha256(bytes))
  } catch (error) {
    throw new Error('当前浏览器无法完成图片校验，请升级 Chrome/Edge，或通过 HTTPS 访问后重试。', { cause: error })
  }
}
