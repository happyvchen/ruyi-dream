"""文本/多模态调用通道，保留 Gemini 模块名与配置名兼容已有部署。

支持三种通道，通过 GEMINI_PROVIDER 切换：
- direct: google-genai SDK 直连 Google（需要能访问 googleapis.com）。
- toapis: 走 ToAPIs 的 OpenAI 兼容 chat completions 中转（国内可直连），
  复用 TOAPIS_API_KEY / TOAPIS_BASE_URL，模型 ID 可用 GEMINI_TOAPIS_MODEL 单独指定。
- deepseek: DeepSeek 官方 OpenAI 兼容接口，独立使用 DEEPSEEK_* 配置。

ToAPIs 当前使用 DeepSeek V4.1 Flash，也可通过原模型配置切回 Gemini。
本模块只负责“把 prompt + 图片输入送到模型并拿回文本”，不承担业务
schema 校验、attempt 记录和 JSON 解析，这些仍由调用方负责。
"""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
import contextvars
from functools import partial
import json
import logging
import re
import threading
import time
from typing import Any, Callable

from . import config
from . import deepseek_inputs
from .generation_progress import (
    ERROR_CONN_RESET,
    ERROR_INVALID_REQUEST,
    ERROR_MALFORMED_OUTPUT,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_PROVIDER_TRANSIENT_UNKNOWN,
    ERROR_RATE_LIMITED,
    ERROR_WAF_BLOCK,
    classify_provider_error,
    is_gemini_transport_retryable_error,
    normalize_toapis_error,
)
from .toapis_concurrency import toapis_concurrency_slot


logger = logging.getLogger(__name__)

# Blocking text requests (including HTTP-slot waits and retry backoff) must
# never occupy asyncio's default executor used by uploads and image delivery.
# Executor admission queues excess jobs without allocating waiting threads.
_text_executor: ThreadPoolExecutor | None = None


def _get_text_executor() -> ThreadPoolExecutor:
    global _text_executor
    if _text_executor is None:
        # Match the runtime's former default-executor capacity. HTTP admission
        # still uses the channel semaphore; using that smaller limit as
        # worker capacity would let sleeping retries block unrelated text work.
        _text_executor = ThreadPoolExecutor(
            thread_name_prefix="ruyi-text",
        )
    return _text_executor


VALID_GEMINI_PROVIDERS = {"direct", "toapis", "deepseek"}
# 两个 chat 通道沿用同一客户端预算，保持姿势拼图和提示词整理分批契约。
TOAPIS_MAX_MESSAGE_IMAGES = 16
DEEPSEEK_MAX_REQUEST_BYTES = 48 * 1024 * 1024
_deepseek_slot_lock = threading.Lock()
_deepseek_slot_limit: int | None = None
_deepseek_semaphore: threading.BoundedSemaphore | None = None


def _is_deepseek_channel() -> bool:
    return getattr(get_settings(), "gemini_provider", "toapis") == "deepseek"


def _chat_label() -> str:
    return "文本模型(DeepSeek 官方通道)" if _is_deepseek_channel() else "文本模型(ToAPIs 通道)"


@contextmanager
def _chat_concurrency_slot():
    if not _is_deepseek_channel():
        with toapis_concurrency_slot():
            yield
        return
    global _deepseek_slot_limit, _deepseek_semaphore
    limit = get_settings().deepseek_max_concurrency
    with _deepseek_slot_lock:
        if _deepseek_semaphore is None or _deepseek_slot_limit != limit:
            _deepseek_slot_limit = limit
            _deepseek_semaphore = threading.BoundedSemaphore(limit)
        semaphore = _deepseek_semaphore
    with semaphore:
        yield


class GeminiProviderError(RuntimeError):
    """带 error_code 的 Gemini 通道错误。

    error_code 取 generation_progress 的 ERROR_* 常量（由 classify_provider_error 判定），
    让上层 finish_attempt(error_code=...) 和重试策略据此决策：
    - 明确限流或连接中断/超时且当前尝试没有可用文本时，按配置重发整个请求；
    - MALFORMED_OUTPUT / 截断 一定已产出并计费，绝不在 provider 层重发。
    """

    def __init__(
        self,
        message: str,
        error_code: str,
        *,
        retry_after: float | None = None,
        provider_response: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after
        self.provider_response = provider_response or {}


class GeminiRetryDeferred(RuntimeError):
    """本次调用已把下一次恢复持久化，调用方应结束当前执行并等待 poller。"""

    def __init__(self, snapshot: dict[str, Any]):
        super().__init__("Gemini retry deferred to persistent scheduler")
        self.snapshot = snapshot


def get_settings() -> config.Settings:
    return config.get_settings()


def gemini_channel() -> str:
    return get_settings().gemini_provider


def effective_gemini_model() -> str:
    settings = get_settings()
    if settings.gemini_provider == "deepseek":
        return settings.deepseek_model
    if settings.gemini_provider == "toapis" and settings.gemini_toapis_model:
        return settings.gemini_toapis_model
    return settings.gemini_model


def effective_text_provider() -> str:
    """调用来源按实际模型记录；存量 Gemini 记录与 direct 通道保持兼容。"""
    return "deepseek" if _is_deepseek_channel() or effective_gemini_model().lower().startswith("deepseek-") else "gemini"


def is_gemini_configured() -> bool:
    settings = get_settings()
    if settings.gemini_provider == "deepseek":
        return bool(settings.deepseek_api_key)
    if settings.gemini_provider == "toapis":
        return bool(settings.toapis_api_key)
    return bool(settings.gemini_api_key)


def gemini_not_configured_message() -> str:
    if gemini_channel() == "deepseek":
        return "DEEPSEEK_API_KEY 未配置，文本模型(DeepSeek 官方通道) 不可用。"
    if gemini_channel() == "toapis":
        return "TOAPIS_API_KEY 未配置，文本模型(ToAPIs 通道) 不可用。"
    return "GEMINI_API_KEY 未配置，Gemini(直连通道) 不可用。"


def gemini_request_meta() -> dict[str, Any]:
    """写入 attempt request snapshot 的通道元信息，保证历史可追溯。"""
    return {
        "provider": effective_text_provider(),
        "channel": gemini_channel(),
        # 保留历史快照键，避免已有管理工具读取失败。
        "gemini_provider": gemini_channel(),
        "model": effective_gemini_model(),
    }


async def generate_gemini_json_text(
    prompt: str,
    images: list[dict[str, Any]] | None = None,
    system_instruction: str | None = None,
    on_retry: Callable[[dict[str, Any]], None] | None = None,
    initial_retry_count: int = 0,
    defer_retry: bool = False,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    """调用 Gemini 生成 JSON 文本（返回原始文本，由调用方解析）。

    images 元素形如 {"data": bytes, "mime_type": str}。
    """
    if not is_gemini_configured():
        raise RuntimeError(gemini_not_configured_message())
    if gemini_channel() in {"toapis", "deepseek"}:
        call = partial(
            _generate_via_toapis,
            prompt,
            images or [],
            system_instruction,
            on_retry,
            initial_retry_count,
            defer_retry,
            should_cancel,
        )
        return await asyncio.get_running_loop().run_in_executor(
            _get_text_executor(), contextvars.copy_context().run, call,
        )
    return await _generate_via_google_with_retry(
        prompt,
        images or [],
        system_instruction,
        on_retry,
        initial_retry_count,
        defer_retry,
        should_cancel,
    )


async def _generate_via_google_with_retry(
    prompt: str,
    images: list[dict[str, Any]],
    system_instruction: str | None,
    on_retry: Callable[[dict[str, Any]], None] | None = None,
    initial_retry_count: int = 0,
    defer_retry: bool = False,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    """直连 Gemini 与 ToAPIs 通道遵守相同的 10 次、30/60/120/240 恢复口径。"""
    settings = get_settings()
    retry_limit = max(0, settings.gemini_retry_limit)
    base = max(0.0, float(settings.gemini_retry_backoff_base_seconds))
    cap = max(0.0, float(settings.gemini_retry_backoff_cap_seconds))
    time_budget = max(0.1, float(getattr(settings, "gemini_retry_time_budget_seconds", 5400)))
    deadline = time.monotonic() + time_budget
    retries = max(0, int(initial_retry_count))
    while True:
        if should_cancel and should_cancel():
            raise RuntimeError("模型调用已取消")
        if time.monotonic() >= deadline:
            raise GeminiProviderError(
                "Gemini(直连通道)重试已超过总时间预算",
                ERROR_PROVIDER_TIMEOUT,
                provider_response={"channel": "direct", "transportRetries": retries},
            )
        try:
            return await _generate_via_google(prompt, images, system_instruction)
        except Exception as exc:
            if should_cancel and should_cancel():
                raise RuntimeError("模型调用已取消") from exc
            error_code = getattr(exc, "error_code", None) or classify_provider_error(
                status_code=_exception_status_code(exc),
                message=str(exc),
            )
            if not is_gemini_transport_retryable_error(error_code) or retries >= retry_limit:
                if isinstance(exc, GeminiProviderError):
                    raise
                raise GeminiProviderError(
                    str(exc),
                    error_code,
                    retry_after=getattr(exc, "retry_after", None),
                    provider_response={
                        "channel": "direct",
                        "httpStatus": _exception_status_code(exc),
                        "exceptionType": type(exc).__name__,
                    },
                ) from exc
            retries += 1
            wait = _retry_backoff_seconds(retries, base, cap)
            retry_after = getattr(exc, "retry_after", None)
            if error_code == ERROR_RATE_LIMITED and retry_after is not None:
                wait = max(wait, float(retry_after))
            if time.monotonic() + wait >= deadline:
                raise GeminiProviderError(
                    "Gemini(直连通道)重试已超过总时间预算",
                    ERROR_PROVIDER_TIMEOUT,
                    provider_response={"channel": "direct", "transportRetries": retries},
                ) from exc
            logger.warning(
                "Gemini(直连通道)瞬时错误(%s),第 %d 次重试,退避 %.1fs: %s",
                error_code,
                retries,
                wait,
                str(exc)[:200],
            )
            snapshot = {
                "status": "GEMINI_TRANSPORT_RETRY_WAIT",
                "transportRetries": retries,
                "retryLimit": retry_limit,
                "retryDelaySeconds": wait,
                "errorCode": error_code,
                "error": str(exc)[:500],
            }
            if on_retry:
                on_retry(snapshot)
            if defer_retry:
                raise GeminiRetryDeferred(snapshot) from exc
            if wait > 0:
                await asyncio.sleep(wait)


def _exception_status_code(exc: BaseException) -> int | None:
    """兼容 google SDK/httpx 常见异常上的 status_code/code 形态。"""
    for name in ("status_code", "code"):
        raw = getattr(exc, name, None)
        if callable(raw):
            with suppress(Exception):
                raw = raw()
        raw = getattr(raw, "value", raw)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if 100 <= value <= 599:
            return value
    return None


async def _generate_via_google(
    prompt: str,
    images: list[dict[str, Any]],
    system_instruction: str | None,
) -> str:
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:  # pragma: no cover - 环境缺 SDK 时
        raise RuntimeError("google-genai SDK 不可用，Gemini 直连通道无法调用。") from exc

    settings = get_settings()
    user_parts: list[Any] = [types.Part.from_text(text=prompt)]
    for image in images:
        user_parts.append(
            types.Part.from_bytes(
                data=image["data"], mime_type=image.get("mime_type") or "image/png"
            )
        )
    contents: list[Any] = [types.Content(role="user", parts=user_parts)]
    client = genai.Client(api_key=settings.gemini_api_key)
    config_kwargs: dict[str, Any] = {"response_mime_type": "application/json"}
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    try:
        generate_config = types.GenerateContentConfig(**config_kwargs)
        response = await client.aio.models.generate_content(
            model=settings.gemini_model, contents=contents, config=generate_config
        )
    except TypeError:
        # 旧版 SDK 不支持部分 config 字段时退化为无 config 调用。
        # 仅在没有 system_instruction 时允许降级(只丢 response_mime_type);
        # 带 system_instruction 的调用绝不能静默丢掉角色设定,必须显式失败。
        if system_instruction:
            raise
        response = await client.aio.models.generate_content(
            model=settings.gemini_model, contents=contents
        )
    return getattr(response, "text", "") or "{}"


def _generate_via_toapis(
    prompt: str,
    images: list[dict[str, Any]],
    system_instruction: str | None,
    on_retry: Callable[[dict[str, Any]], None] | None = None,
    initial_retry_count: int = 0,
    defer_retry: bool = False,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    # 保留历史函数名；DeepSeek 官方与 ToAPIs 共用 OpenAI chat/SSE 和恢复契约。
    if _is_deepseek_channel():
        if len(images) > TOAPIS_MAX_MESSAGE_IMAGES:
            raise GeminiProviderError("文本模型(DeepSeek 官方通道) 客户端单次请求最多 16 张图片", ERROR_INVALID_REQUEST)
        if should_cancel and should_cancel():
            raise RuntimeError("模型调用已取消")
        try:
            plan = deepseek_inputs.plan_request(prompt, images, system_instruction, effective_gemini_model())
        except ValueError as exc:
            raise GeminiProviderError(str(exc), ERROR_INVALID_REQUEST) from exc
        logger.info("DeepSeek input transport: %s", json.dumps(plan.facts, ensure_ascii=False))

        def prepare_request(deadline: float) -> None:
            plan.resolve_files(lambda item: _upload_deepseek_file(item, deadline), should_cancel)

        return _post_toapis_chat_with_retry(
            plan.payload, on_retry=on_retry, initial_retry_count=initial_retry_count,
            defer_retry=defer_retry, should_cancel=should_cancel, prepare_request=prepare_request,
        )
    payload = build_toapis_chat_payload(prompt, images, system_instruction)
    return _post_toapis_chat_with_retry(
        payload,
        on_retry=on_retry,
        initial_retry_count=initial_retry_count,
        defer_retry=defer_retry,
        should_cancel=should_cancel,
    )


def _post_toapis_chat_with_retry(
    payload: dict[str, Any],
    should_cancel: Callable[[], bool] | None = None,
    on_retry: Callable[[dict[str, Any]], None] | None = None,
    initial_retry_count: int = 0,
    defer_retry: bool = False,
    prepare_request: Callable[[float], None] | None = None,
) -> str:
    """对限流和连接类瞬时错误有限重发整个 chat completions 请求。

    ==== 安全边界（务必读懂再改）====
    聊天类按 token 计费且非幂等。产品要求连接中断/read timeout 也自动恢复，
    因此这里把 CONN_RESET 与明确的 RATE_LIMITED 一并纳入有限重发；每次重发都保留
    attempt 进度事实，并受次数和总时间预算约束。MALFORMED_OUTPUT 代表已经收到正文，
    仍由上层的定向重问处理，不在这里盲目重发。
    反例(绝不重试):MALFORMED_OUTPUT(含 finish_reason=length 截断、流腐蚀)——正文已
    产出并计费;AUTH_FAILED / INSUFFICIENT_BALANCE 重发无用（余额类还会继续烧钱）。
    WAF_BLOCK 保留独立的有限熔断预算，其它错误不在 zero-output 集合里，直接抛出。

    退避按 base * 2^(n-1) 增长并在 cap 封顶；默认 30/60/120/240 秒，
    之后保持 240 秒。429 若带 Retry-After 则尊重之。
    should_cancel 在每次请求前（包括取得并发槽位后）检查，取消后不再发送新请求。
    """
    settings = get_settings()
    retry_limit = max(0, settings.gemini_retry_limit)
    base = max(0.0, float(settings.gemini_retry_backoff_base_seconds))
    cap = max(0.0, float(settings.gemini_retry_backoff_cap_seconds))
    time_budget = max(0.1, float(settings.gemini_retry_time_budget_seconds))
    deadline: float | None = None
    waf_limit = max(0, settings.gemini_waf_retry_limit)

    attempt = max(0, int(initial_retry_count))
    while True:
        if should_cancel and should_cancel():
            raise RuntimeError("模型调用已取消")
        try:
            with _chat_concurrency_slot():
                if should_cancel and should_cancel():
                    raise RuntimeError("模型调用已取消")
                if deadline is None:
                    deadline = time.monotonic() + time_budget
                if prepare_request is not None:
                    prepare_request(deadline)
                    if should_cancel and should_cancel():
                        raise RuntimeError("模型调用已取消")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GeminiProviderError(f"{_chat_label()} 限流重试已超过总时间预算", ERROR_RATE_LIMITED)
                single_request_timeout = max(0.1, float(
                    settings.deepseek_timeout_seconds if _is_deepseek_channel() else settings.gemini_toapis_timeout_seconds
                ))
                return _post_toapis_chat(payload, read_timeout_seconds=min(remaining, single_request_timeout))
        except GeminiProviderError as exc:
            error_code = exc.error_code
            if not is_gemini_transport_retryable_error(error_code):
                # AUTH/BALANCE/MALFORMED 等:重发无用或会重复计费,直接抛出。
                raise
            current_limit = waf_limit if error_code == ERROR_WAF_BLOCK else retry_limit
            if attempt >= current_limit:
                raise
            attempt += 1

            if should_cancel is not None and should_cancel():
                raise
            wait = _retry_backoff_seconds(attempt, base, cap)
            if error_code == ERROR_RATE_LIMITED and exc.retry_after is not None:
                wait = max(wait, exc.retry_after)
            if deadline is not None and time.monotonic() + wait >= deadline:
                raise
            logger.warning(
                f"{_chat_label()} 瞬时错误(%s),第 %d 次重试,退避 %.1fs: %s",
                error_code,
                attempt,
                wait,
                str(exc)[:200],
            )
            snapshot = {
                "status": "GEMINI_TRANSPORT_RETRY_WAIT",
                "transportRetries": attempt,
                "retryLimit": current_limit,
                "retryDelaySeconds": wait,
                "errorCode": error_code,
                "error": str(exc)[:500],
            }
            if on_retry:
                on_retry(snapshot)
            if defer_retry:
                raise GeminiRetryDeferred(snapshot) from exc
            if wait > 0:
                time.sleep(wait)


def _retry_backoff_seconds(attempt: int, base: float, cap: float) -> float:
    """确定性指数退避并封顶。attempt 从 1 开始。"""
    if base <= 0:
        return 0.0
    raw = base * (2 ** max(0, attempt - 1))
    return min(raw, cap) if cap > 0 else raw


def build_toapis_chat_payload(
    prompt: str,
    images: list[dict[str, Any]],
    system_instruction: str | None,
) -> dict[str, Any]:
    if len(images) > TOAPIS_MAX_MESSAGE_IMAGES:
        raise GeminiProviderError(
            f"{_chat_label()} 客户端单次请求最多 {TOAPIS_MAX_MESSAGE_IMAGES} 张图片，当前 {len(images)} 张，请先合并参考图",
            ERROR_INVALID_REQUEST,
        )
    user_content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image in images:
        mime_type = image.get("mime_type") or "image/png"
        encoded = base64.b64encode(image["data"]).decode("ascii")
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
            }
        )
    messages: list[dict[str, Any]] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": user_content})
    # 必须流式:ToAPIs 在 Cloudflare 后面,非流式长请求会在
    # ~100s 被 CF 以 HTTP 524 掐断,而服务端仍完成生成并计费。
    # stream_options.include_usage: 让最后一个数据块携带 usage(含
    # reasoning_tokens 明细),用于截断检测和失败诊断(2026-07-03 实测可用;
    # 旧 Gemini 路由曾忽略 max_tokens/response_format，不能推广到其他模型。
    # 此处保留共同协议；未核实的模型专用参数不发送，使用模型默认值。
    return {
        "model": effective_gemini_model(),
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def _upload_deepseek_file(image: dict[str, Any], deadline: float) -> str:
    """Temporary official-provider reference; retries/slots belong to the chat loop.

    No local/S3 write and no permanent provider copy. Successful uploads are reused
    during this invocation; after process restart the original frozen media is read.
    """
    import requests

    settings = get_settings()
    remaining = min(600.0, deadline - time.monotonic(), float(settings.deepseek_timeout_seconds))
    if remaining <= 0:
        raise GeminiProviderError("DeepSeek 参考图上传已超过总时间预算", ERROR_RATE_LIMITED)
    try:
        response = requests.post(
            settings.deepseek_base_url.rstrip("/") + "/files",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            data={"purpose": "user_data", "expires_after[anchor]": "created_at",
                  "expires_after[seconds]": str(deepseek_inputs.FILE_TTL_SECONDS)},
            files={"file": (image["filename"], image["data"], image["mime_type"])},
            timeout=(min(float(settings.deepseek_connect_timeout_seconds), remaining), remaining),
        )
    except requests.RequestException as exc:
        raise GeminiProviderError(f"DeepSeek 参考图上传失败: {exc}", ERROR_CONN_RESET) from exc
    with response:
        try:
            result = response.json()
        except ValueError:
            result = {}
        if not isinstance(result, dict):
            result = {}
        if not response.ok:
            raw = response.text or ""
            error = normalize_toapis_error(result, status_code=response.status_code, fallback_message=raw)
            if response.status_code in {400, 413, 422}:
                error["code"] = ERROR_INVALID_REQUEST
            raise GeminiProviderError(
                f"DeepSeek 参考图上传失败: {summarize_upstream_http_error(response.status_code, raw)}",
                str(error["code"]), retry_after=_parse_retry_after(response.headers.get("Retry-After")),
                provider_response={"httpStatus": response.status_code, "providerError": error,
                                   "phase": "file_upload"},
            )
        file_id = result.get("id")
        if not isinstance(file_id, str) or not file_id.startswith("file-api-"):
            raise GeminiProviderError("DeepSeek 文件上传未返回有效 file_id", ERROR_INVALID_REQUEST)
        logger.info("DeepSeek temporary file: id=%s bytes=%s expires_at=%s", file_id, len(image["data"]), result.get("expires_at"))
        return file_id


def _post_toapis_chat(payload: dict[str, Any], *, read_timeout_seconds: float | None = None) -> str:
    """流式调用 ToAPIs 或 DeepSeek 官方 chat completions，保留历史函数名。

    单次调用不做重试;重试由上层 _post_toapis_chat_with_retry 在"零产出"前提下
    安全地重发整个请求(见其 docstring 的安全边界)。timeout 作用于相邻数据块之间的
    读等待,而不是整个响应时长。

    失败一律抛 GeminiProviderError(error_code=...)（由 classify_provider_error 判定）：
    - HTTP 非 ok → 按状态码 + 错误体分类（502→GATEWAY_5XX、429→RATE_LIMITED、
      401/403→AUTH_FAILED、余额→INSUFFICIENT_BALANCE 等）；错误体若带非零输出
      usage，则按已产出处理，绝不盲目重发。
    - 连接异常(requests.RequestException) → CONN_RESET；上层按产品配置有限自动重发。
    - 流截断/腐蚀 → MALFORMED_OUTPUT(由 assemble_toapis_stream_text 抛出,已产出并
      计费,绝不重发)。

    注意:timeout 元组第一个值(connect)在 urllib3 中同时是发送请求体阶段的
    socket 超时;请求体内嵌 base64 图片可达数 MB,部署环境上行慢时过小的值
    会触发 "The write operation timed out"(此时请求未到达 ToAPIs、不计费,
    ToAPIs 后台也查不到这笔调用)。故该值独立可配,默认 60s。
    """
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests 导入失败,无法调用文本接口:请安装 requests") from exc

    from .image_provider import toapis_url

    settings = get_settings()
    if _is_deepseek_channel():
        url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
        api_key = settings.deepseek_api_key
        configured_read_timeout = settings.deepseek_timeout_seconds
        configured_connect_timeout = settings.deepseek_connect_timeout_seconds
        if len(json.dumps(payload, allow_nan=False).encode("utf-8")) > DEEPSEEK_MAX_REQUEST_BYTES:
            raise GeminiProviderError("DeepSeek 官方完整请求体超过 48 MiB（图片、文本与 JSON 合计）；未截断提示词", ERROR_INVALID_REQUEST)
    else:
        url = toapis_url("/v1/chat/completions")
        api_key = settings.toapis_api_key
        configured_read_timeout = settings.gemini_toapis_timeout_seconds
        configured_connect_timeout = settings.gemini_toapis_connect_timeout_seconds
    read_timeout = max(
        0.1,
        min(
            float(configured_read_timeout),
            float(read_timeout_seconds) if read_timeout_seconds is not None else float(configured_read_timeout),
        ),
    )
    connect_timeout = min(float(configured_connect_timeout), read_timeout)
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            stream=True,
            timeout=(connect_timeout, read_timeout),
        )
    except requests.RequestException as exc:
        # 连接/写/读超时统一归 CONN_RESET，由上层按有限预算自动重发。
        message = f"{_chat_label()} 请求失败: {exc}"
        raise GeminiProviderError(message, classify_provider_error(message=str(exc))) from exc
    with response:
        if not response.ok:
            raw_body = response.text or ""
            # attempt 记录只存下方摘要;原始错误体在此落日志供排查。
            logger.warning("%s 上游错误 HTTP %s: %s", _chat_label(), response.status_code, redact_provider_text(raw_body)[:2000])
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            try:
                response_payload = response.json()
            except ValueError:
                response_payload = {"message": raw_body}
            provider_error = normalize_toapis_error(
                response_payload,
                status_code=response.status_code,
                fallback_message=raw_body,
            )
            # DeepSeek 的 400/422 是格式/参数错误，不使用中转的未知路由恢复策略。
            if _is_deepseek_channel() and response.status_code in {400, 422}:
                provider_error["code"] = ERROR_INVALID_REQUEST
            error_code = (
                ERROR_MALFORMED_OUTPUT
                if _usage_has_generated_output(response_payload.get("usage"))
                else str(provider_error["code"])
            )
            raise GeminiProviderError(
                f"{_chat_label()} 请求失败: {summarize_upstream_http_error(response.status_code, raw_body)}",
                error_code,
                retry_after=retry_after,
                provider_response={
                    "httpStatus": response.status_code,
                    "contentType": response.headers.get("Content-Type"),
                    "retryAfter": response.headers.get("Retry-After"),
                    "providerError": provider_error,
                    "rawBody": redact_provider_text(raw_body)[:16000],
                    "rawBodyTruncated": len(raw_body) > 16000,
                },
            )
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/event-stream" not in content_type:
            # 服务端未按流式返回时,退化按普通 JSON 响应解析。
            try:
                return extract_toapis_chat_text(response.json())
            except ValueError as exc:
                raise GeminiProviderError(
                    f"{_chat_label()} 响应不是合法 JSON: {(response.text or '')[:500]}",
                    ERROR_MALFORMED_OUTPUT,
                ) from exc
        return assemble_toapis_stream_text(iter_sse_lines(response))


def _parse_retry_after(value: str | None) -> float | None:
    """解析 429 响应的 Retry-After 头（仅支持秒数形式；HTTP-date 略过）。"""
    if not value:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def iter_sse_lines(response: Any) -> Any:
    """按 SSE 规范把响应体切成 UTF-8 文本行。

    根因说明(2026-07-05 线上事故)：SSE 规范(WHATWG)强制 UTF-8，但 ToAPIs 的
    Content-Type: text/event-stream 不带 charset，requests 按 RFC2616 遗留规则
    回退 ISO-8859-1；iter_lines(decode_unicode=True) 先把 UTF-8 字节流按单字节
    解成乱码，再用 str.splitlines() 切行——而中文 UTF-8 的续字节会频繁命中
    0x85(NEL)、0x0b、0x0c 等 Unicode 行界，把 "data: {...}" 行打碎成非法 JSON
    碎片被静默丢弃，导致组装文本结构性缺损、解析必败且形态随机。
    因此必须：只按 b"\\n" 切行(UTF-8 多字节序列不含 0x0A，行界安全)，
    整行显式 UTF-8 解码。
    """
    for raw in response.iter_lines(delimiter=b"\n"):
        if isinstance(raw, bytes):
            yield raw.decode("utf-8", errors="replace").rstrip("\r")
        elif raw is not None:
            yield str(raw).rstrip("\r")


def redact_provider_text(value: str) -> str:
    """保留上游原始错误事实，同时移除常见凭据形态。"""
    text = value or ""
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*[\"']?)[^\s\"',}]+", r"\1[REDACTED]", text)
    return text


def summarize_upstream_http_error(status_code: int, body: str) -> str:
    """把上游 HTTP 错误(可能是整页 HTML)摘要成用户可读的短消息。

    面向 UI 展示与 attempt 记录;原始错误体由调用方落应用日志供排查。
    """
    snippet = (body or "").strip()[:4000]
    lowered = snippet.lower()
    if lowered.startswith(("<!doctype", "<html")):
        title_match = re.search(r"<title[^>]*>(.*?)</title>", snippet, re.I | re.S)
        text = title_match.group(1) if title_match else re.sub(r"<[^>]+>", " ", snippet)
        snippet = re.sub(r"\s+", " ", text).strip()
    snippet = snippet[:200]
    if status_code in {502, 503, 504, 520, 521, 522, 523, 524}:
        hint = "上游网关错误或超时,可直接重试;反复出现时请检查当前文本通道服务状态。"
    elif status_code == 429:
        hint = "请求被限流或额度不足,请稍后重试或检查当前文本通道余额。"
    elif status_code in {401, 403}:
        key_name = "DEEPSEEK_API_KEY" if _is_deepseek_channel() else "TOAPIS_API_KEY"
        hint = f"API Key 无效或无权限,请检查 {key_name} 配置。"
    else:
        hint = ""
    parts = [f"HTTP {status_code}", hint]
    if snippet:
        parts.append(f"(响应摘要: {snippet})")
    return " ".join(part for part in parts if part)


def assemble_toapis_stream_text(lines: Any) -> str:
    """把 OpenAI 兼容 SSE 数据流组装为完整文本。

    同时捕获 finish_reason 和 usage：finish_reason=length 说明正文被输出上限
    截断,任何 JSON 修复都救不了,必须显式失败而不是让调用方看到一个费解的
    JSON 语法错误(2026-07-03 排障结论)。
    """
    parts: list[str] = []
    finish_reason: str | None = None
    usage: dict[str, Any] = {}
    reasoning_chars = 0
    dropped_data_lines = 0
    for line in lines:
        if not line or not str(line).startswith("data:"):
            continue
        data = str(line)[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            # data: 行不是合法 JSON 说明流被腐蚀(本次事故正因静默丢弃而难查),
            # 记录数量并告警,空文本错误里也会带上。
            dropped_data_lines += 1
            continue
        if not isinstance(chunk, dict):
            continue
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        if chunk.get("error"):
            error_body = json.dumps(chunk["error"], ensure_ascii=False)[:500]
            # 尚无正文/思考时，流内 error 是零输出 provider 错误，可按稳定分类
            # 有限恢复；一旦收到过任何输出，再重发整个请求可能重复计费，必须
            # 作为已产出但不完整处理。
            if parts or reasoning_chars or dropped_data_lines or finish_reason or _usage_has_generated_output(usage):
                raise GeminiProviderError(
                    f"{_chat_label()} 已收到部分输出后流式响应错误: {error_body}",
                    ERROR_MALFORMED_OUTPUT,
                )
            provider_error = normalize_toapis_error({"error": chunk["error"]})
            raise GeminiProviderError(
                f"{_chat_label()} 流式响应错误: {error_body}",
                str(provider_error["code"]),
                provider_response={"providerError": provider_error},
            )
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        first = choices[0] if isinstance(choices[0], dict) else {}
        if first.get("finish_reason"):
            finish_reason = str(first["finish_reason"])
        delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str):
            reasoning_chars += len(reasoning)
        content = delta.get("content")
        if isinstance(content, str):
            parts.append(content)
    text = "".join(parts)
    if dropped_data_lines:
        logger.warning(
            f"{_chat_label()} 流中有 %d 条 data 行不是合法 JSON 被丢弃(可能存在流腐蚀)",
            dropped_data_lines,
        )
    if finish_reason == "length":
        # 截断=正文已产出并计费,任何 JSON 修复都救不了。归 MALFORMED_OUTPUT:
        # 绝不在 provider 层重发(会重复计费),交由上层"修复+重问一次"处理。
        raise GeminiProviderError(
            f"{_chat_label()} 输出被截断(finish_reason=length): "
            f"completion_tokens={usage.get('completion_tokens', '?')}, "
            f"已收到正文 {len(text)} 字符(思考约 {reasoning_chars} 字符)。",
            ERROR_MALFORMED_OUTPUT,
        )
    if not text.strip():
        # 没有正文、思考和腐蚀事实时属于确定零输出，可有限恢复；已有思考 token
        # 或流腐蚀则可能已经产出并计费，不能盲目重发。
        error_code = (
            ERROR_MALFORMED_OUTPUT
            if reasoning_chars or dropped_data_lines or _usage_has_generated_output(usage) or finish_reason
            else ERROR_PROVIDER_TRANSIENT_UNKNOWN
        )
        raise GeminiProviderError(
            f"{_chat_label()} 流式响应未包含文本内容"
            f"(finish_reason={finish_reason or '?'}, 思考约 {reasoning_chars} 字符, "
            f"丢弃非法 data 行 {dropped_data_lines} 条)。",
            error_code,
        )
    return text


def extract_toapis_chat_text(response_payload: dict[str, Any]) -> str:
    if response_payload.get("error"):
        if _usage_has_generated_output(response_payload.get("usage")):
            raise GeminiProviderError(
                f"{_chat_label()} 已产生输出用量后返回错误信封: "
                f"{json.dumps(response_payload['error'], ensure_ascii=False)[:500]}",
                ERROR_MALFORMED_OUTPUT,
            )
        provider_error = normalize_toapis_error({"error": response_payload["error"]})
        raise GeminiProviderError(
            f"{_chat_label()} 响应返回错误信封: "
            f"{json.dumps(response_payload['error'], ensure_ascii=False)[:500]}",
            str(provider_error["code"]),
            provider_response={"providerError": provider_error},
        )
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        error_code = (
            ERROR_MALFORMED_OUTPUT
            if _usage_has_generated_output(response_payload.get("usage"))
            else ERROR_PROVIDER_TRANSIENT_UNKNOWN
        )
        raise GeminiProviderError(
            f"{_chat_label()} 响应缺少 choices: {json.dumps(response_payload, ensure_ascii=False)[:500]}",
            error_code,
        )
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        # 兼容 content 为内容块数组的形态，拼接其中的文本块。
        content = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if not isinstance(content, str) or not content.strip():
        first = choices[0] if isinstance(choices[0], dict) else {}
        reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
        has_output_facts = bool(
            _usage_has_generated_output(response_payload.get("usage"))
            or first.get("finish_reason")
            or (isinstance(reasoning, str) and reasoning.strip())
        )
        raise GeminiProviderError(
            f"{_chat_label()} 响应缺少文本内容: {json.dumps(response_payload, ensure_ascii=False)[:500]}",
            ERROR_MALFORMED_OUTPUT if has_output_facts else ERROR_PROVIDER_TRANSIENT_UNKNOWN,
        )
    return content


def _usage_has_generated_output(usage: Any) -> bool:
    """Provider usage 显示已有输出 token 时，禁止把整次请求当作零输出重发。"""
    if not isinstance(usage, dict):
        return False
    for key in ("completion_tokens", "output_tokens"):
        try:
            if int(usage.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False
