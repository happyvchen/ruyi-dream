// 统一把后端稳定的 error_code（序列化字段 errorCode，来自 generation_attempts.error_code）
// 归类成面向普通业务用户的友好文案。技术码/原始 message 仍保留在“技术详情/调用记录”里，
// 这里只负责给用户看的标题、提示和语气。

export type FriendlyTone = 'retrying' | 'warning' | 'error' | 'info'

export type FriendlyError = {
  title: string
  hint?: string
  tone: FriendlyTone
}

// 网关瞬时 / 连接中断 / 限流 / provider 超时 / 回传失败：后端会自动重试，
// 对用户统一表述为“暂时繁忙、已自动重试、可稍后再试”。
const BUSY_RETRY: FriendlyError = {
  title: 'AI 服务暂时繁忙，系统已自动重试',
  hint: '若持续失败请稍后再试。',
  tone: 'retrying',
}

const ERROR_MESSAGE_MAP: Record<string, FriendlyError> = {
  // —— 网关 / 连接 / 限流 / 超时类：后端自动重试 ——
  GATEWAY_5XX: BUSY_RETRY,
  CONN_RESET: BUSY_RETRY,
  RATE_LIMITED: BUSY_RETRY,
  PROVIDER_TIMEOUT: BUSY_RETRY,
  PROVIDER_INTERNAL_ERROR: BUSY_RETRY,
  PROVIDER_GENERATION_FAILED: {
    title: '图片生成服务处理失败',
    hint: '系统已自动重试，仍失败时可再次生成。',
    tone: 'error',
  },
  DELIVERY_FAILED: BUSY_RETRY,

  // —— 额度不足 ——
  INSUFFICIENT_BALANCE: {
    title: 'AI 服务额度不足',
    hint: '请联系管理员充值后重试。',
    tone: 'warning',
  },

  // —— 鉴权 / 渠道配置 / WAF 拦截：属于配置类异常，用户无法自助解决 ——
  AUTH_FAILED: {
    title: 'AI 服务配置异常，已记录',
    hint: '请联系管理员处理。',
    tone: 'error',
  },
  WAF_BLOCK: {
    title: 'AI 服务配置异常，已记录',
    hint: '请联系管理员处理。',
    tone: 'error',
  },

  // —— AI 返回内容异常 / 截断 ——
  MALFORMED_OUTPUT: {
    title: 'AI 返回结果异常，请重试',
    hint: '多次失败可稍后再试。',
    tone: 'error',
  },
  INVALID_REQUEST: {
    title: '生成输入不符合服务要求',
    hint: '请调整输入后重试。',
    tone: 'warning',
  },
  CONTENT_POLICY_VIOLATION: {
    title: '素材或生成要求未通过安全检查',
    hint: '请调整相关图片或文字要求后重试。',
    tone: 'warning',
  },
  TASK_NOT_FOUND: {
    title: '生成任务已失效',
    hint: '请重新生成。',
    tone: 'warning',
  },
  SUBMISSION_UNKNOWN: {
    title: '未能确认生成任务是否提交成功',
    hint: '系统已停止继续提交，避免重复计费。',
    tone: 'warning',
  },

  // —— 任务耗时过长超时（新码 POLL_TIMEOUT 与旧打标超时码 TIMEOUT 走同一文案）——
  POLL_TIMEOUT: {
    title: 'AI 任务耗时较长已超时',
    hint: '可点击重试。',
    tone: 'warning',
  },
  TIMEOUT: {
    title: 'AI 任务耗时较长已超时',
    hint: '可点击重试。',
    tone: 'warning',
  },

  // —— 业务/穿法校验否决：非系统错误，原始业务原因文案更有用，交由调用方透传 message ——
  // 见 friendlyError() 里的特殊处理；此处仅作为无 message 时的兜底。
  BUSINESS_REJECT: {
    title: '当前方案不成立，请重新选搭',
    tone: 'info',
  },

  // —— 成片阶段既有码 ——
  UPLOAD_FAILED: {
    title: '图片生成未成功，可重试',
    tone: 'error',
  },
  PROVIDER_FAILED: {
    title: '图片生成未成功，可重试',
    tone: 'error',
  },
  STALE_JOB_RECOVERED: BUSY_RETRY,
}

/**
 * 把后端 error_code 映射成用户友好文案。
 *
 * @param errorCode 后端稳定错误码（可空）。
 * @param fallback  拿不到已知 error_code 时展示的兜底文案，一般是后端原始 message / detail。
 */
export function friendlyError(errorCode?: string | null, fallback?: string | null): FriendlyError {
  const code = errorCode?.trim()

  // 业务否决：优先透传后端给的业务原因（这是有用的业务反馈，不要盖成通用话术）。
  if (code === 'BUSINESS_REJECT') {
    const reason = fallback?.trim()
    if (reason) return { title: reason, tone: 'info' }
    return ERROR_MESSAGE_MAP.BUSINESS_REJECT
  }

  if (code && ERROR_MESSAGE_MAP[code]) return ERROR_MESSAGE_MAP[code]

  // 未知 / 无 code：回退到后端原始文案（如 HTTPException 的 detail）。
  const message = fallback?.trim()
  if (message) return { title: message, tone: 'error' }
  return { title: '操作失败，请稍后再试', tone: 'error' }
}

/** 拼成单行文案，适合 toast 等只能展示一行的位置。 */
export function friendlyErrorText(errorCode?: string | null, fallback?: string | null): string {
  const { title, hint } = friendlyError(errorCode, fallback)
  return hint ? `${title}，${hint}` : title
}
