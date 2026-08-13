import { describe, expect, it } from 'vitest'
import { friendlyAttemptErrorText, friendlyError, friendlyErrorText } from '../errorMessages'

describe('user-facing error messages', () => {
  it('does not claim automatic retry for balance, auth, input, or content policy failures', () => {
    expect(friendlyError('INSUFFICIENT_BALANCE').title).toBe('AI 服务额度不足')
    expect(friendlyErrorText('INSUFFICIENT_BALANCE')).not.toContain('自动重试')
    expect(friendlyErrorText('AUTH_FAILED')).not.toContain('自动重试')
    expect(friendlyErrorText('INVALID_REQUEST')).not.toContain('自动重试')
    expect(friendlyErrorText('CONTENT_POLICY_VIOLATION')).not.toContain('自动重试')
  })

  it('does not claim an unknown provider failure is still retrying after the attempt failed', () => {
    const message = friendlyErrorText('PROVIDER_TRANSIENT_UNKNOWN')

    expect(message).toContain('自动重试后仍未恢复')
    expect(message).not.toContain('正在')
    expect(friendlyError('PROVIDER_TRANSIENT_UNKNOWN').tone).toBe('warning')
  })

  it('does not present a deterministic oversized input as a network retry', () => {
    const message = friendlyErrorText('INVALID_REQUEST', '第 1 张参考图尺寸超限')

    expect(message).toContain('生成输入不符合服务要求')
    expect(message).not.toContain('自动重试')
  })

  it('explains an upload interruption as pending recovery while sibling shots finish', () => {
    expect(friendlyAttemptErrorText('UPLOAD_FAILED', 'socket closed', true)).toBe(
      '参考图导入中断，等待本轮其他镜头完成后自动恢复',
    )
    expect(friendlyAttemptErrorText('UPLOAD_FAILED', 'socket closed', false)).toContain('图片生成未成功')
  })

  it('keeps submission-unknown copy focused on avoiding duplicate paid work', () => {
    const message = friendlyErrorText('SUBMISSION_UNKNOWN')

    expect(message).toContain('未能确认生成任务是否提交成功')
    expect(message).toContain('避免重复计费')
  })

  it('preserves useful business rejection reasons instead of hiding them behind generic provider copy', () => {
    expect(friendlyError('BUSINESS_REJECT', '搭配服装压住主推款领口').title).toBe('搭配服装压住主推款领口')
  })

  it('falls back to backend detail when a future stable code is unknown', () => {
    expect(friendlyError('FUTURE_CODE', '后端已给出业务原因').title).toBe('后端已给出业务原因')
  })
})
