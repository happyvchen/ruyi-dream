"""Gemini 文本/多模态调用通道。

支持两种通道，通过 GEMINI_PROVIDER 切换：
- direct: google-genai SDK 直连 Google（需要能访问 googleapis.com）。
- toapis: 走 ToAPIs 的 OpenAI 兼容 chat completions 中转（国内可直连），
  复用 TOAPIS_API_KEY / TOAPIS_BASE_URL，模型 ID 可用 GEMINI_TOAPIS_MODEL 单独指定。

本模块只负责“把 prompt + 图片输入送到 Gemini 并拿回文本”，不承担业务
schema 校验、attempt 记录和 JSON 解析，这些仍由调用方负责。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import time
from typing import Any, Callable

from . import config
from .generation_progress import (
    ERROR_CONN_RESET,
    ERROR_MALFORMED_OUTPUT,
    ERROR_RATE_LIMITED,
    ERROR_WAF_BLOCK,
    classify_provider_error,
    is_zero_output_transient,
    normalize_toapis_error,
)
from .toapis_concurrency import toapis_concurrency_slot


logger = logging.getLogger(__name__)


VALID_GEMINI_PROVIDERS = {"direct", "toapis"}


class GeminiProviderError(RuntimeError):
    """带 error_code 的 Gemini 通道错误。

    error_code 取 generation_progress 的 ERROR_* 常量（由 classify_provider_error 判定），
    让上层 finish_attempt(error_code=...) 和重试策略据此决策：
    - 只有 is_zero_output_transient(error_code) 且当前尝试零文本产出时才重发整个请求；
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


def get_settings() -> config.Settings:
    return config.get_settings()


def gemini_channel() -> str:
    return get_settings().gemini_provider


def effective_gemini_model() -> str:
    settings = get_settings()
    if settings.gemini_provider == "toapis" and settings.gemini_toapis_model:
        return settings.gemini_toapis_model
    return settings.gemini_model


def is_gemini_configured() -> bool:
    settings = get_settings()
    if settings.gemini_provider == "toapis":
        return bool(settings.toapis_api_key)
    return bool(settings.gemini_api_key)


def gemini_not_configured_message() -> str:
    if gemini_channel() == "toapis":
        return "TOAPIS_API_KEY 未配置，Gemini(ToAPIs 通道) 不可用。"
    return "GEMINI_API_KEY 未配置，Gemini(直连通道) 不可用。"


def gemini_request_meta() -> dict[str, Any]:
    """写入 attempt request snapshot 的通道元信息，保证历史可追溯。"""
    return {
        "gemini_provider": gemini_channel(),
        "model": effective_gemini_model(),
    }


async def generate_gemini_json_text(
    prompt: str,
    images: list[dict[str, Any]] | None = None,
    system_instruction: str | None = None,
) -> str:
    """调用 Gemini 生成 JSON 文本（返回原始文本，由调用方解析）。

    images 元素形如 {"data": bytes, "mime_type": str}。
    """
    if not is_gemini_configured():
        raise RuntimeError(gemini_not_configured_message())
    if gemini_channel() == "toapis":
        return await asyncio.to_thread(
            _generate_via_toapis, prompt, images or [], system_instruction
        )
    return await _generate_via_google(prompt, images or [], system_instruction)


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
) -> str:
    payload = build_toapis_chat_payload(prompt, images, system_instruction)
    return _post_toapis_chat_with_retry(payload)


def _post_toapis_chat_with_retry(
    payload: dict[str, Any],
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    """在"零产出瞬时错误"前提下安全重发整个 chat completions 请求。

    ==== 安全边界（务必读懂再改）====
    聊天类按 token 计费且非幂等,重发整个请求只有在"上一次尝试零 token 产出"时才安全。
    本函数只在 is_zero_output_transient(error_code) 为真时重试,该集合仅含:
      - GATEWAY_5XX:502/503/504/520-524。Cloudflare 边缘层错误,请求根本没到 ToAPIs
        应用层,不进 Consume(计费),零产出。
      - CONN_RESET:连接关闭/重置/SSL EOF/write timeout。请求未成功送达或响应未开始,
        零产出、不计费。
      - RATE_LIMITED:429。请求被前置限流拒绝,未进入生成,零产出。
    这三类都发生在"产生任何输出之前",_post_toapis_chat 在拿到流式正文前就抛出,
    因此重发不会重复计费。
    反例(绝不重试):MALFORMED_OUTPUT(含 finish_reason=length 截断、流腐蚀)——正文已
    产出并计费;AUTH_FAILED / INSUFFICIENT_BALANCE / WAF_BLOCK 由外层策略处理,重发无用
    (余额类还会继续烧钱)。这些 error_code 不在 zero-output 集合里,本函数直接抛出不重试。

    退避:指数退避 + 抖动,base/cap/次数由 settings 控制;429 若带 Retry-After 则尊重之。
    should_cancel 若提供,退避期间命中即中断重试并抛出最后一次错误。
    """
    settings = get_settings()
    retry_limit = max(0, settings.gemini_retry_limit)
    base = max(0.0, float(settings.gemini_retry_backoff_base_seconds))
    cap = max(0.0, float(settings.gemini_retry_backoff_cap_seconds))
    waf_limit = max(0, settings.gemini_waf_retry_limit)

    attempt = 0
    waf_attempts = 0
    while True:
        try:
            with toapis_concurrency_slot():
                return _post_toapis_chat(payload)
        except GeminiProviderError as exc:
            error_code = exc.error_code
            # WAF 熔断:有限重试后放弃(1010 是指纹封禁,盲目重发没用但短暂退避偶尔能过)。
            if error_code == ERROR_WAF_BLOCK:
                if waf_attempts >= waf_limit:
                    raise
                waf_attempts += 1
            elif is_zero_output_transient(error_code):
                if attempt >= retry_limit:
                    raise
                attempt += 1
            else:
                # AUTH/BALANCE/MALFORMED 等:重发无用或会重复计费,直接抛出。
                raise

            if should_cancel is not None and should_cancel():
                raise
            wait = _retry_backoff_seconds(attempt, base, cap)
            if error_code == ERROR_RATE_LIMITED and exc.retry_after is not None:
                wait = max(wait, exc.retry_after)
            logger.warning(
                "Gemini(ToAPIs 通道) 零产出瞬时错误(%s),第 %d 次重试,退避 %.1fs: %s",
                error_code,
                attempt + waf_attempts,
                wait,
                str(exc)[:200],
            )
            if wait > 0:
                time.sleep(wait)


def _retry_backoff_seconds(attempt: int, base: float, cap: float) -> float:
    """指数退避 + 满抖动(full jitter)。attempt 从 1 开始。"""
    if base <= 0:
        return 0.0
    raw = base * (2 ** max(0, attempt - 1))
    ceiling = min(raw, cap) if cap > 0 else raw
    return random.uniform(0, ceiling)


def build_toapis_chat_payload(
    prompt: str,
    images: list[dict[str, Any]],
    system_instruction: str | None,
) -> dict[str, Any]:
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
    # 注意 max_tokens/response_format 实测被 ToAPIs 忽略,不要依赖)。
    return {
        "model": effective_gemini_model(),
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def _post_toapis_chat(payload: dict[str, Any]) -> str:
    """流式调用 ToAPIs chat completions,返回组装后的完整文本。

    单次调用不做重试;重试由上层 _post_toapis_chat_with_retry 在"零产出"前提下
    安全地重发整个请求(见其 docstring 的安全边界)。timeout 作用于相邻数据块之间的
    读等待,而不是整个响应时长。

    失败一律抛 GeminiProviderError(error_code=...)（由 classify_provider_error 判定）：
    - HTTP 非 ok → 按状态码 + 错误体分类（502→GATEWAY_5XX、429→RATE_LIMITED、
      401/403→AUTH_FAILED、余额→INSUFFICIENT_BALANCE 等）。这些都是"请求未产出
      任何正文"的失败,天然零计费,可安全重发。
    - 连接异常(requests.RequestException) → CONN_RESET(含 write timeout,请求未到达
      ToAPIs、不计费)。
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
        raise RuntimeError("requests 导入失败,无法调用 ToAPIs 接口:请安装 requests") from exc

    from .image_provider import toapis_url

    settings = get_settings()
    try:
        response = requests.post(
            toapis_url("/v1/chat/completions"),
            headers={"Authorization": f"Bearer {settings.toapis_api_key}"},
            json=payload,
            stream=True,
            timeout=(settings.gemini_toapis_connect_timeout_seconds, settings.gemini_toapis_timeout_seconds),
        )
    except requests.RequestException as exc:
        # 连接/写超时:请求未到达 ToAPIs 应用层,零产出、不计费,归 CONN_RESET 可安全重发。
        message = f"Gemini(ToAPIs 通道) 请求失败: {exc}"
        raise GeminiProviderError(message, classify_provider_error(message=str(exc))) from exc
    with response:
        if not response.ok:
            raw_body = response.text or ""
            # attempt 记录只存下方摘要;原始错误体在此落日志供排查。
            logger.warning("ToAPIs 上游错误 HTTP %s: %s", response.status_code, raw_body[:2000])
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
            raise GeminiProviderError(
                f"Gemini(ToAPIs 通道) 请求失败: {summarize_upstream_http_error(response.status_code, raw_body)}",
                str(provider_error["code"]),
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
                    f"Gemini(ToAPIs 通道) 响应不是合法 JSON: {(response.text or '')[:500]}",
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
        hint = "上游网关错误或超时,可直接重试;反复出现时请检查 ToAPIs 服务状态。"
    elif status_code == 429:
        hint = "请求被限流或额度不足,请稍后重试或检查 ToAPIs 余额。"
    elif status_code in {401, 403}:
        hint = "API Key 无效或无权限,请检查 TOAPIS_API_KEY 配置。"
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
        if chunk.get("error"):
            error_body = json.dumps(chunk["error"], ensure_ascii=False)[:500]
            # 流内 error 块可能携带余额/限流/鉴权语义,按文案分类而不是一律当畸形。
            # 已收到过正文与否不影响这类"provider 明确报错"的定性。
            raise GeminiProviderError(
                f"Gemini(ToAPIs 通道) 流式响应错误: {error_body}",
                classify_provider_error(body=error_body),
            )
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
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
            "Gemini(ToAPIs 通道) 流中有 %d 条 data 行不是合法 JSON 被丢弃(可能存在流腐蚀)",
            dropped_data_lines,
        )
    if finish_reason == "length":
        # 截断=正文已产出并计费,任何 JSON 修复都救不了。归 MALFORMED_OUTPUT:
        # 绝不在 provider 层重发(会重复计费),交由上层"修复+重问一次"处理。
        raise GeminiProviderError(
            "Gemini(ToAPIs 通道) 输出被截断(finish_reason=length): "
            f"completion_tokens={usage.get('completion_tokens', '?')}, "
            f"已收到正文 {len(text)} 字符(思考约 {reasoning_chars} 字符)。",
            ERROR_MALFORMED_OUTPUT,
        )
    if not text.strip():
        # 流已建立但没组装出正文:可能被腐蚀(dropped_data_lines>0)或只有思考。
        # 归 MALFORMED_OUTPUT——流已开始产出、不能盲目重发。
        raise GeminiProviderError(
            "Gemini(ToAPIs 通道) 流式响应未包含文本内容"
            f"(finish_reason={finish_reason or '?'}, 思考约 {reasoning_chars} 字符, "
            f"丢弃非法 data 行 {dropped_data_lines} 条)。",
            ERROR_MALFORMED_OUTPUT,
        )
    return text


def extract_toapis_chat_text(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(
            f"Gemini(ToAPIs 通道) 响应缺少 choices: {json.dumps(response_payload, ensure_ascii=False)[:500]}"
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
        raise RuntimeError(
            f"Gemini(ToAPIs 通道) 响应缺少文本内容: {json.dumps(response_payload, ensure_ascii=False)[:500]}"
        )
    return content
