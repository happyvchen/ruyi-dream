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
from typing import Any

from . import config


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
        response = await client.aio.models.generate_content(
            model=settings.gemini_model, contents=contents
        )
    return getattr(response, "text", "") or "{}"


def _generate_via_toapis(
    prompt: str,
    images: list[dict[str, Any]],
    system_instruction: str | None,
) -> str:
    settings = get_settings()
    payload = build_toapis_chat_payload(prompt, images, system_instruction)
    response_payload = _post_toapis_chat(payload)
    return extract_toapis_chat_text(response_payload)


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
    return {
        "model": effective_gemini_model(),
        "messages": messages,
        "stream": False,
    }


def _post_toapis_chat(payload: dict[str, Any]) -> dict[str, Any]:
    from .image_provider import post_json, toapis_url

    settings = get_settings()
    return post_json(
        toapis_url("/v1/chat/completions"),
        payload,
        settings.toapis_api_key,
        timeout=settings.gemini_toapis_timeout_seconds,
        auth_scheme="Bearer",
        error_label="Gemini(ToAPIs 通道) 请求失败",
    )


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
