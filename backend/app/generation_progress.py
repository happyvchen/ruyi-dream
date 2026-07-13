"""生成 attempt 的阶段进度与失败分类。

设计要点（对应当前四步生成工作流状态机约束）：

- 阶段进度只是 attempt 上的展示元数据，不是新的业务状态。
  `generation_result_images_new.status` / `generation_attempts.status`
  仍然只有 QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED。
- 失败必须分类。"provider 生成失败"（重试要重新消耗额度）、
  "成片回传失败"（provider 已成功，结果可零成本重新拉取）和
  "provider 超时" 是完全不同的业务事实，前端据此提供不同的重试动作。
- 所有写入走独立的 sqlite 连接，可以从 asyncio 线程池 worker 中安全调用。
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any

from .db import dumps, get_db, loads, now_iso

# 阶段常量：按业务顺序排列。
PHASE_PREPARE = "prepare"
PHASE_UPLOAD = "upload"
PHASE_SUBMIT = "submit"
PHASE_QUEUE = "queue"
PHASE_GENERATE = "generate"
PHASE_DELIVER = "deliver"
PHASE_STORE = "store"

PHASE_ORDER: tuple[str, ...] = (
    PHASE_PREPARE,
    PHASE_UPLOAD,
    PHASE_SUBMIT,
    PHASE_QUEUE,
    PHASE_GENERATE,
    PHASE_DELIVER,
    PHASE_STORE,
)

# 失败分类常量，落到 generation_attempts.error_code。
ERROR_UPLOAD_FAILED = "UPLOAD_FAILED"
ERROR_PROVIDER_FAILED = "PROVIDER_FAILED"
ERROR_PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
ERROR_DELIVERY_FAILED = "DELIVERY_FAILED"
ERROR_SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
ERROR_SYSTEM_ERROR = "SYSTEM_ERROR"

# ToAPIs 报错分类（方案 A–G）。这些 error_code 稳定落到 generation_attempts.error_code，
# 前端据此给不同文案与动作；重试策略由 is_retryable_error / is_zero_output_transient 决定。
ERROR_GATEWAY_5XX = "GATEWAY_5XX"                    # 502/503/504/520-524、上游网关错误 → 可安全重试
ERROR_CONN_RESET = "CONN_RESET"                      # 连接关闭/重置/SSL EOF/write timeout → 可安全重试
ERROR_RATE_LIMITED = "RATE_LIMITED"                  # 429 → 退避重试（尊重 Retry-After）
ERROR_INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"  # 余额不足/剩余额度负 → 不重试、硬停
ERROR_AUTH_FAILED = "AUTH_FAILED"                    # 401/403/不支持指定渠道 → 不重试
ERROR_WAF_BLOCK = "WAF_BLOCK"                        # 403 Cloudflare error code 1010 → 有限重试后熔断
ERROR_PROVIDER_GENERATION_FAILED = "PROVIDER_GENERATION_FAILED"  # provider 已建任务但生成阶段失败 → 有限重试
ERROR_INVALID_REQUEST = "INVALID_REQUEST"            # 请求参数/参考图不符合 provider 要求 → 修正输入后再试
ERROR_CONTENT_POLICY = "CONTENT_POLICY_VIOLATION"    # 内容安全策略拒绝 → 调整素材或要求
ERROR_TASK_NOT_FOUND = "TASK_NOT_FOUND"              # provider 任务不存在/已失效 → 重新创建任务
ERROR_PROVIDER_INTERNAL = "PROVIDER_INTERNAL_ERROR"  # provider 内部错误 → 有限重试
ERROR_MALFORMED_OUTPUT = "MALFORMED_OUTPUT"          # 非法 JSON/Extra data/控制字符/finish_reason=length 截断 → 上层修复+重问一次
ERROR_POLL_TIMEOUT = "POLL_TIMEOUT"                  # 打标/长任务轮询预算耗尽 → 自动排队重试
ERROR_BUSINESS_REJECT = "BUSINESS_REJECT"            # 我方业务/穿法校验否决（非 provider 错误）→ 不重试

# 连接类关键字（网络层异常文案）：命中即归 CONN_RESET，可安全重试。
_CONN_RESET_KEYWORDS = (
    "max retries exceeded",
    "response ended prematurely",
    "remote end closed",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection error",
    "connectionerror",
    "connectionreseterror",
    "unexpected_eof",
    "unexpected eof",
    "ssl eof",
    "eof occurred",
    "write operation timed out",
    "write timed out",
    "read timed out",
    "broken pipe",
    "server disconnected",
    "peer closed connection",
)

# 网关类关键字（应用/边缘网关错误文案）。
_GATEWAY_KEYWORDS = (
    "bad gateway",
    "do request failed",
    "upstream error",
    "gateway timeout",
    "service unavailable",
    "a timeout occurred",
)

# 余额/额度类关键字。命中即 INSUFFICIENT_BALANCE，绝不重试。
_BALANCE_KEYWORDS = (
    "余额不足",
    "额度不足",
    "剩余额度",
    "insufficient",
    "insufficient_quota",
    "quota exceeded",
    "no balance",
    "out of credit",
)

# 输出畸形关键字。
_MALFORMED_KEYWORDS = (
    "finish_reason=length",
    "被截断",
    "截断",
    "不是合法 json",
    "不是合法json",
    "not valid json",
    "invalid json",
    "extra data",
    "jsondecodeerror",
    "控制字符",
    "control character",
    "expecting value",
    "expecting property name",
    "未包含文本内容",
    "流腐蚀",
)


def _has_negative_balance(text: str) -> bool:
    """匹配 "剩余额度 $ -2.25" 之类的负额度文案。"""
    return bool(re.search(r"-\s*\d", text))


def _embedded_json_objects(text: str) -> list[dict[str, Any]]:
    """提取 ToAPIs error.message 中嵌套的上游 JSON 错误信封。

    ToAPIs 的异步失败有时顶层仅给 ``generation_failed``，实际原因形如
    ``W8X API returned 400: {\"error\": {...}}`` 嵌在 message 中。
    """
    decoder = json.JSONDecoder()
    items: list[dict[str, Any]] = []
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            items.append(value)
    return items


def _error_fields(value: Any) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    if not isinstance(value, dict):
        return fields
    error = value.get("error") if isinstance(value.get("error"), dict) else value
    if isinstance(error, dict):
        item = {
            "code": str(error.get("code") or "").strip(),
            "type": str(error.get("type") or "").strip(),
            "message": str(error.get("message") or "").strip(),
            "param": str(error.get("param") or "").strip(),
        }
        if any(item.values()):
            fields.append(item)
        for embedded in _embedded_json_objects(item["message"]):
            fields.extend(_error_fields(embedded))
    return fields


def normalize_toapis_error(
    payload: dict[str, Any] | None,
    *,
    status_code: int | None = None,
    fallback_message: str = "",
) -> dict[str, Any]:
    """把 ToAPIs HTTP/异步失败归一成稳定、可展示、可决策的错误事实。"""
    payload = payload if isinstance(payload, dict) else {}
    layers = _error_fields(payload)
    top = layers[0] if layers else {}
    actionable = next(
        (
            layer
            for layer in reversed(layers)
            if layer.get("code") or layer.get("type") or layer.get("message")
        ),
        top,
    )
    messages = [fallback_message, *(layer.get("message", "") for layer in layers)]
    combined = " ".join(item for item in messages if item)
    codes = {str(layer.get("code") or "").lower() for layer in layers}
    types = {str(layer.get("type") or "").lower() for layer in layers}

    if codes & {"insufficient_quota"} or status_code == 402:
        internal_code = ERROR_INSUFFICIENT_BALANCE
    elif codes & {"invalid_request"} or types & {"invalid_request", "invalid_request_error"} or status_code == 400:
        internal_code = ERROR_INVALID_REQUEST
    elif codes & {"content_policy_violation"} or status_code == 422:
        internal_code = ERROR_CONTENT_POLICY
    elif codes & {"task_not_found"} or status_code == 404:
        internal_code = ERROR_TASK_NOT_FOUND
    elif codes & {"unauthorized"} or status_code == 401:
        internal_code = ERROR_AUTH_FAILED
    elif codes & {"rate_limit_exceeded"} or status_code == 429:
        internal_code = ERROR_RATE_LIMITED
    elif codes & {"internal_error"} or status_code == 500:
        internal_code = ERROR_PROVIDER_INTERNAL
    elif codes & {"generation_failed"}:
        internal_code = ERROR_PROVIDER_GENERATION_FAILED
    else:
        internal_code = classify_provider_error(status_code=status_code, message=combined)

    return {
        "code": internal_code,
        "providerCode": top.get("code") or None,
        "providerType": top.get("type") or None,
        "providerMessage": top.get("message") or fallback_message or None,
        "upstreamCode": actionable.get("code") or None,
        "upstreamType": actionable.get("type") or None,
        "upstreamMessage": actionable.get("message") or top.get("message") or fallback_message or None,
        "param": actionable.get("param") or None,
        "httpStatus": status_code,
    }


def provider_error_presentation(error_code: str | None, reason: str | None = None) -> dict[str, Any]:
    """稳定的普通用户错误契约；原始 provider JSON 只留在技术详情。"""
    code = str(error_code or ERROR_SYSTEM_ERROR)
    reason = re.sub(r"\s+", " ", str(reason or "")).strip()[:240] or None
    catalog: dict[str, tuple[str, str, str, bool]] = {
        ERROR_INVALID_REQUEST: ("生成输入不符合服务要求", reason or "请检查参考图数量、格式和生成要求后重试。", "调整输入后重试", False),
        ERROR_CONTENT_POLICY: ("素材或生成要求未通过安全检查", "请调整相关图片或文字要求后重试。", "调整内容后重试", False),
        ERROR_INSUFFICIENT_BALANCE: ("AI 服务额度不足", "请联系管理员充值后重试。", "联系管理员", False),
        ERROR_AUTH_FAILED: ("AI 服务配置异常", "请联系管理员检查服务密钥或渠道权限。", "联系管理员", False),
        ERROR_RATE_LIMITED: ("AI 服务请求过于频繁", "系统已按策略退避；仍失败时请稍后重试。", "稍后重试", True),
        ERROR_GATEWAY_5XX: ("AI 服务暂时不可用", "系统已自动重试；仍失败时请稍后再试。", "稍后重试", True),
        ERROR_CONN_RESET: ("AI 服务连接中断", "系统已自动重试；仍失败时请稍后再试。", "稍后重试", True),
        ERROR_PROVIDER_INTERNAL: ("AI 服务内部异常", "系统已自动重试；仍失败时可再次生成。", "再次生成", True),
        ERROR_PROVIDER_GENERATION_FAILED: ("图片生成服务处理失败", "系统已自动重试；仍失败时可再次生成。", "再次生成", True),
        ERROR_TASK_NOT_FOUND: ("生成任务已失效", "系统无法继续查询原任务，请重新生成。", "重新生成", True),
        ERROR_PROVIDER_TIMEOUT: ("图片生成等待超时", "可稍后继续生成，系统会保留已有进度。", "继续生成", True),
        ERROR_POLL_TIMEOUT: ("AI 任务等待超时", "可稍后重试。", "重试", True),
        ERROR_SUBMISSION_UNKNOWN: ("未能确认生成任务是否提交成功", "系统已停止继续提交，避免重复计费。", "稍后重试", False),
        ERROR_DELIVERY_FAILED: ("图片已生成，但回传失败", "可重新回传，不会再次消耗生成额度。", "重新回传", True),
        ERROR_UPLOAD_FAILED: ("参考图上传失败", "请检查网络后重试。", "重试", True),
        ERROR_MALFORMED_OUTPUT: ("AI 返回结果不完整", "请重新生成。", "重新生成", False),
        ERROR_BUSINESS_REJECT: (reason or "当前方案不成立", "请调整要求后重试。", "调整后重试", False),
        ERROR_SYSTEM_ERROR: ("系统处理异常", "错误已记录，请稍后重试。", "稍后重试", False),
        ERROR_WAF_BLOCK: ("AI 服务访问受限", "请联系管理员检查服务访问配置。", "联系管理员", False),
        ERROR_PROVIDER_FAILED: ("图片生成未成功", "请稍后重试。", "重新生成", False),
    }
    title, message, action, retryable = catalog.get(code, catalog[ERROR_SYSTEM_ERROR])
    return {"code": code, "title": title, "message": message, "action": action, "retryable": retryable}


def classify_provider_error(
    *,
    status_code: int | None = None,
    message: str = "",
    body: str = "",
    provider_error_code: str = "",
) -> str:
    """把 provider（ToAPIs/Gemini/Image-2）报错归类成稳定的 error_code。

    分类优先级（先命中先返回，越"硬"越靠前，避免误判成可重试）：
      1. 余额/额度不足（含负额度）→ INSUFFICIENT_BALANCE（硬停）
      2. 403 + Cloudflare error code 1010 → WAF_BLOCK
      3. 401/403 或"不支持指定渠道"→ AUTH_FAILED
      4. 429 或 rate limit → RATE_LIMITED
      5. 502/503/504/520-524 或网关关键字 → GATEWAY_5XX
      6. 连接类关键字 → CONN_RESET
      7. 输出畸形/截断关键字 → MALFORMED_OUTPUT
      8. 兜底 → PROVIDER_FAILED
    """
    text = f"{message} {body}".lower()

    # 1. 余额优先：余额类文案即使伴随 429/403 也必须硬停，不能被限流/鉴权分支吞掉。
    if any(keyword in text for keyword in _BALANCE_KEYWORDS):
        return ERROR_INSUFFICIENT_BALANCE
    if "剩余额度" in text and _has_negative_balance(text):
        return ERROR_INSUFFICIENT_BALANCE

    # 2. Cloudflare WAF 指纹封禁（403 + error code 1010）。
    if "1010" in text and ("cloudflare" in text or "error code" in text or status_code == 403):
        return ERROR_WAF_BLOCK

    # 3. 鉴权/渠道：401/403、无效 key、无权限、指定渠道。
    if status_code in {401, 403} or "不支持指定渠道" in text or "指定渠道" in text \
            or "无权限" in text or "invalid api key" in text or "invalid key" in text \
            or "unauthorized" in text or "permission" in text:
        return ERROR_AUTH_FAILED

    # provider 已返回任务 ID，随后在生成阶段明确失败。此时不存在重复提交未知，
    # 可以由编排层按既有 PROVIDER_GENERATION_RETRY_LIMIT 有限重建任务。
    if provider_error_code.strip().lower() == "generation_failed":
        return ERROR_PROVIDER_GENERATION_FAILED

    # 4. 限流。
    if status_code == 429 or "rate limit" in text or "too many requests" in text or "限流" in text:
        return ERROR_RATE_LIMITED

    # 5. 网关 5xx。
    if status_code in {502, 503, 504, 520, 521, 522, 523, 524}:
        return ERROR_GATEWAY_5XX
    if any(keyword in text for keyword in _GATEWAY_KEYWORDS):
        return ERROR_GATEWAY_5XX

    # 6. 连接类。
    if any(keyword in text for keyword in _CONN_RESET_KEYWORDS):
        return ERROR_CONN_RESET

    # 7. 输出畸形/截断。
    if any(keyword in text for keyword in _MALFORMED_KEYWORDS):
        return ERROR_MALFORMED_OUTPUT

    return ERROR_PROVIDER_FAILED


# 可重试的 error_code：网关/连接/限流是瞬时问题；WAF 有限重试；provider/轮询超时可续跑。
_RETRYABLE_CODES = frozenset(
    {
        ERROR_GATEWAY_5XX,
        ERROR_CONN_RESET,
        ERROR_RATE_LIMITED,
        ERROR_WAF_BLOCK,
        ERROR_PROVIDER_GENERATION_FAILED,
        ERROR_PROVIDER_INTERNAL,
        ERROR_TASK_NOT_FOUND,
        ERROR_PROVIDER_TIMEOUT,
        ERROR_POLL_TIMEOUT,
    }
)

# 聊天类（Gemini）"零 token 产出"时可安全重发整个请求、绝不重复计费的 error_code。
# 关键：只有在请求"产生任何输出前"就失败（502/连接错/限流）才在这个集合里；
# MALFORMED_OUTPUT / 截断 一定已产出并计费，绝不放进来。
_ZERO_OUTPUT_TRANSIENT_CODES = frozenset({ERROR_GATEWAY_5XX, ERROR_CONN_RESET, ERROR_RATE_LIMITED})


def is_retryable_error(error_code: str | None) -> bool:
    """该 error_code 是否可重试（对幂等的图片路径）。

    AUTH_FAILED / INSUFFICIENT_BALANCE / BUSINESS_REJECT / MALFORMED_OUTPUT 不可重试。
    """
    return bool(error_code) and error_code in _RETRYABLE_CODES


def is_generation_retryable_error(error_code: str | None) -> bool:
    """已拿到 task ID 后，哪些失败允许重建一次生成任务。"""
    return error_code in {
        ERROR_PROVIDER_GENERATION_FAILED,
        ERROR_PROVIDER_INTERNAL,
        ERROR_GATEWAY_5XX,
        ERROR_CONN_RESET,
        ERROR_RATE_LIMITED,
        ERROR_PROVIDER_TIMEOUT,
    }


def is_status_check_retryable_error(error_code: str | None) -> bool:
    """状态查询本身的瞬时失败；参数/鉴权/任务不存在不能靠重复查询恢复。"""
    return error_code in {ERROR_GATEWAY_5XX, ERROR_CONN_RESET, ERROR_RATE_LIMITED, ERROR_WAF_BLOCK}


def is_zero_output_transient(error_code: str | None) -> bool:
    """该 error_code 是否属于"零产出瞬时错误"——聊天类可安全重发整个请求不重复计费。"""
    return bool(error_code) and error_code in _ZERO_OUTPUT_TRANSIENT_CODES


def is_balance_error(error_code: str | None) -> bool:
    return error_code == ERROR_INSUFFICIENT_BALANCE


class GenerationPhaseError(RuntimeError):
    """带失败分类和 provider 快照的生成错误。

    error_code 取上面的 ERROR_* 常量；provider_response 保留 providerRequestId
    等现场，保证回传失败后仍可零成本重新拉取结果。
    """

    def __init__(self, message: str, error_code: str, provider_response: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.provider_response = provider_response or {}


def classify_attempt_error(exc: BaseException) -> str | None:
    """从异常上提取失败分类；没有分类信息时返回 None（保持既有行为）。"""
    code = getattr(exc, "error_code", None)
    return str(code) if code else None


def classify_generation_attempt_error(exc: BaseException) -> str:
    """生成图片边界的最终分类：未携带 provider 分类的异常属于本地系统问题。"""
    return classify_attempt_error(exc) or ERROR_SYSTEM_ERROR


class AttemptProgressRecorder:
    """把阶段进度持久化到 generation_attempts.progress_json。

    progress_json 结构::

        {
          "current": "deliver",           # 当前阶段；结束后为 None
          "phases": [
            {"phase": "upload", "startedAt": "...", "endedAt": "...",
             "detail": {"completed": 2, "total": 4}},
            ...
          ]
        }

    attempt 被后台恢复（进程重启后 resume）时会先读回已有进度，
    已结束的阶段不会被覆盖。
    """

    def __init__(self, attempt_id: int | None):
        self._attempt_id = attempt_id
        self._lock = threading.Lock()
        self._progress: dict[str, Any] = {"current": None, "phases": []}
        if attempt_id:
            with get_db() as db:
                row = db.execute(
                    "SELECT progress_json FROM generation_attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
            existing = loads(row["progress_json"], None) if row else None
            if isinstance(existing, dict) and isinstance(existing.get("phases"), list):
                self._progress = existing

    @property
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"current": self._progress.get("current"), "phases": list(self._progress.get("phases") or [])}

    def begin(self, phase: str, **detail: Any) -> None:
        """进入一个阶段；自动结束上一个未结束阶段。幂等：重复 begin 同一阶段只更新 detail。"""
        timestamp = now_iso()
        with self._lock:
            entry = self._find(phase)
            if entry is not None and not entry.get("endedAt"):
                if detail:
                    entry["detail"] = {**(entry.get("detail") or {}), **detail}
            else:
                self._close_open_phases(timestamp)
                self._progress["phases"].append(
                    {"phase": phase, "startedAt": timestamp, "endedAt": None, "detail": detail or {}}
                )
            self._progress["current"] = phase
            self._persist()

    def update(self, phase: str, **detail: Any) -> None:
        """更新当前阶段 detail（上传张数、重试次数等）；阶段不存在时等价于 begin。"""
        with self._lock:
            entry = self._find(phase)
            if entry is None or entry.get("endedAt"):
                self._lock_free_begin(phase, detail)
            else:
                entry["detail"] = {**(entry.get("detail") or {}), **detail}
                self._progress["current"] = phase
            self._persist()

    def finish(self) -> None:
        """全部完成：关闭所有未结束阶段，current 置空。"""
        timestamp = now_iso()
        with self._lock:
            self._close_open_phases(timestamp)
            self._progress["current"] = None
            self._persist()

    # -- internal ----------------------------------------------------------

    def _lock_free_begin(self, phase: str, detail: dict[str, Any]) -> None:
        timestamp = now_iso()
        self._close_open_phases(timestamp)
        self._progress["phases"].append(
            {"phase": phase, "startedAt": timestamp, "endedAt": None, "detail": detail or {}}
        )
        self._progress["current"] = phase

    def _find(self, phase: str) -> dict[str, Any] | None:
        for entry in reversed(self._progress.get("phases") or []):
            if entry.get("phase") == phase:
                return entry
        return None

    def _close_open_phases(self, timestamp: str) -> None:
        for entry in self._progress.get("phases") or []:
            if not entry.get("endedAt"):
                entry["endedAt"] = timestamp

    def _persist(self) -> None:
        if not self._attempt_id:
            return
        with get_db() as db:
            db.execute(
                "UPDATE generation_attempts SET progress_json = ?, updated_at = ? WHERE id = ?",
                (dumps(self._progress), now_iso(), self._attempt_id),
            )
