import { describe, expect, it } from 'vitest'
import { friendlyError, friendlyErrorText } from '../errorMessages'

describe('user-facing error messages', () => {
  it('does not claim automatic retry for balance, auth, input, or content policy failures', () => {
    expect(friendlyError('INSUFFICIENT_BALANCE').title).toBe('AI 服务额度不足')
    expect(friendlyErrorText('INSUFFICIENT_BALANCE')).not.toContain('自动重试')
    expect(friendlyErrorText('AUTH_FAILED')).not.toContain('自动重试')
    expect(friendlyErrorText('INVALID_REQUEST')).not.toContain('自动重试')
    expect(friendlyErrorText('CONTENT_POLICY_VIOLATION')).not.toContain('自动重试')
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
