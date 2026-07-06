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
import re
from typing import Any

from . import config


logger = logging.getLogger(__name__)


VALID_GEMINI_PROVIDERS = {"direct", "toapis"}


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
    return _post_toapis_chat(payload)


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
    # 必须流式:ToAPIs 在 Cloudflare 后面,非流式长请求(如拍摄剧本)会在
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

    chat completions 非幂等且按 token 计费,不做重试;timeout 作用于
    相邻数据块之间的读等待,而不是整个响应时长。

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
        raise RuntimeError(f"Gemini(ToAPIs 通道) 请求失败: {exc}") from exc
    with response:
        if not response.ok:
            raw_body = response.text or ""
            # attempt 记录只存下方摘要;原始错误体在此落日志供排查。
            logger.warning("ToAPIs 上游错误 HTTP %s: %s", response.status_code, raw_body[:2000])
            raise RuntimeError(
                f"Gemini(ToAPIs 通道) 请求失败: {summarize_upstream_http_error(response.status_code, raw_body)}"
            )
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/event-stream" not in content_type:
            # 服务端未按流式返回时,退化按普通 JSON 响应解析。
            try:
                return extract_toapis_chat_text(response.json())
            except ValueError as exc:
                raise RuntimeError(f"Gemini(ToAPIs 通道) 响应不是合法 JSON: {(response.text or '')[:500]}") from exc
        return assemble_toapis_stream_text(iter_sse_lines(response))


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
            raise RuntimeError(f"Gemini(ToAPIs 通道) 流式响应错误: {json.dumps(chunk['error'], ensure_ascii=False)[:500]}")
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
        raise RuntimeError(
            "Gemini(ToAPIs 通道) 输出被截断(finish_reason=length): "
            f"completion_tokens={usage.get('completion_tokens', '?')}, "
            f"已收到正文 {len(text)} 字符(思考约 {reasoning_chars} 字符)。请直接重试。"
        )
    if not text.strip():
        raise RuntimeError(
            "Gemini(ToAPIs 通道) 流式响应未包含文本内容"
            f"(finish_reason={finish_reason or '?'}, 思考约 {reasoning_chars} 字符, "
            f"丢弃非法 data 行 {dropped_data_lines} 条)。"
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
