"""DeepSeek-only vision transport. No business prompts, storage writes or AI calls.

Official limits checked 2026-10-30:
https://api-docs.deepseek.com/guides/vision/
https://api-docs.deepseek.com/guides/files_api/
All images use file references, without lossy compression or removing text/images.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
from typing import Any, Callable

from PIL import Image, ImageOps

MAX_BODY_BYTES = 48 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_IMAGES = 600
MAX_EDGE = 8192
MANY_IMAGE_MAX_EDGE = 4096
FILE_TTL_SECONDS = 24 * 60 * 60
VISION_MODELS = {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}
FORMATS = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"),
           "GIF": ("image/gif", "gif"), "WEBP": ("image/webp", "webp")}


def body_size(payload: dict[str, Any]) -> int:
    # Match requests' json= serialization, including file IDs, text and JSON syntax.
    return len(json.dumps(payload, allow_nan=False).encode("utf-8"))


def prepare_image(image: dict[str, Any], index: int, count: int) -> tuple[dict[str, Any], dict[str, Any]]:
    content = image["data"]
    edge = MANY_IMAGE_MAX_EDGE if count >= 15 else MAX_EDGE
    try:
        with Image.open(BytesIO(content)) as decoded:
            fmt, original_size = decoded.format, decoded.size
            if fmt not in FORMATS:
                raise ValueError(f"DeepSeek 参考图 {index} 格式 {fmt} 不受支持，仅支持 JPEG/PNG/GIF/WebP")
            resized = max(decoded.size) > edge
            if resized:
                if getattr(decoded, "is_animated", False):
                    raise ValueError(f"DeepSeek 动画参考图 {index} 超过单边 {edge}px，不能静默丢弃动画帧")
                normalized = ImageOps.exif_transpose(decoded)
                normalized.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                # Resize only for an actual dimension violation; never add JPEG loss.
                output = BytesIO()
                normalized.save(output, "PNG")
                content, fmt, size = output.getvalue(), "PNG", normalized.size
            else:
                decoded.verify()
                size = original_size
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"DeepSeek 参考图 {index} 无法解码") from exc
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"DeepSeek 参考图 {index} 为 {len(content) / 2**20:.2f} MiB，超过 Files API 单图 64 MiB；未自动降低画质")
    mime, suffix = FORMATS[fmt]
    prepared = {"data": content, "mime_type": mime, "filename": f"reference-{index}.{suffix}"}
    facts = {"index": index, "sourceBytes": len(image["data"]), "bytes": len(content),
             "sourceDimensions": original_size, "dimensions": size, "resized": resized}
    return prepared, facts


@dataclass
class DeepSeekInputPlan:
    payload: dict[str, Any]
    uploads: dict[int, dict[str, Any]]
    facts: list[dict[str, Any]]

    def resolve_files(self, upload: Callable[[dict[str, Any]], str],
                      should_cancel: Callable[[], bool] | None = None) -> None:
        content = self.payload["messages"][-1]["content"]
        for index in list(self.uploads):
            if should_cancel and should_cancel():
                raise RuntimeError("模型调用已取消")
            file_id = upload(self.uploads[index])
            content[index + 1] = {"type": "file", "file_id": file_id}
            # Completed uploads survive chat retries and later upload failures.
            del self.uploads[index]


def plan_request(prompt: str, images: list[dict[str, Any]], system: str | None,
                 model: str) -> DeepSeekInputPlan:
    if len(images) > MAX_IMAGES:
        raise ValueError("DeepSeek 官方单次请求最多 600 张图片")
    if images and model not in VISION_MODELS:
        raise ValueError(f"DeepSeek 模型 {model} 未声明支持图片，请使用 deepseek-flash")
    prepared, facts = [], []
    for index, image in enumerate(images, 1):
        item, fact = prepare_image(image, index, len(images))
        prepared.append(item)
        facts.append(fact)
    total = sum(len(item["data"]) for item in prepared)
    if total > MAX_TOTAL_BYTES:
        raise ValueError(f"DeepSeek 图片合计 {total / 2**20:.2f} MiB，超过含 file_id 的 200 MiB 上限；未自动删图或降低画质")
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    # Reserve room for returned IDs; actual body is checked again before POST.
    content.extend({"type": "file", "file_id": "file-api-" + "0" * 512} for _ in prepared)
    messages = ([{"role": "system", "content": system}] if system is not None else [])
    messages.append({"role": "user", "content": content})
    payload = {"model": model, "messages": messages, "stream": True,
               "stream_options": {"include_usage": True}}
    uploads = dict(enumerate(prepared))
    if body_size(payload) > MAX_BODY_BYTES:
        raise ValueError("DeepSeek 请求文本及 JSON 结构超过 48 MiB；图片改为 file_id 后仍超限，未截断提示词")
    for fact in facts:
        fact["transport"] = "file_id"
    return DeepSeekInputPlan(payload, uploads, facts)
