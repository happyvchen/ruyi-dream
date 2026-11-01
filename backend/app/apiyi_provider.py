"""API易 synchronous Images edits; never retry a potentially paid submission."""
from __future__ import annotations

import base64
import hashlib
import math
import time
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps

from . import image_provider as shared
from .generation_progress import ERROR_DELIVERY_FAILED, GenerationPhaseError

# API易 recommends <=10 MB per image; this is not a documented hard maximum.
# No documented aggregate byte limit: never shrink healthy inputs for a group budget.
RECOMMENDED_IMAGE_BYTES = 10_000_000
REFERENCE_QUALITIES = (95, 90, 85)


def _encode_reference(image: Image.Image, fmt: str, *, quality: int | None = None) -> bytes:
    output = BytesIO()
    metadata = {key: image.info[key] for key in ("icc_profile", "exif") if image.info.get(key)}
    if fmt == "PNG":
        image.save(output, fmt, optimize=True, **metadata)
    else:
        image.save(output, "WEBP", lossless=quality is None, quality=quality or 100,
                   method=6, exact=True, **metadata)
    return output.getvalue()


def recompress_reference(content: bytes) -> tuple[bytes, dict[str, Any]]:
    """Only oversized inputs: lossless -> bounded quality -> proportional resize.

    Every candidate is encoded from decoded source pixels, never from a previous
    lossy candidate. Original stored media is never modified.
    """
    with Image.open(BytesIO(content)) as source:
        if source.format not in {"PNG", "JPEG", "WEBP"} or getattr(source, "is_animated", False):
            raise ValueError("API易参考图须为静态 PNG/JPEG/WebP")
        image = ImageOps.exif_transpose(source)
        # WebP supports RGB/RGBA; retain transparency, including palette alpha.
        mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
        image = image.convert(mode)
        facts = {"originalDimensions": list(source.size)}
        for fmt in ("PNG", "WEBP"):
            candidate = _encode_reference(image, fmt)
            if len(candidate) <= RECOMMENDED_IMAGE_BYTES:
                return candidate, {**facts, "uploadedDimensions": list(image.size),
                                   "compressionMethod": "lossless"}
        for quality in REFERENCE_QUALITIES:
            candidate = _encode_reference(image, "WEBP", quality=quality)
            if len(candidate) <= RECOMMENDED_IMAGE_BYTES:
                return candidate, {**facts, "uploadedDimensions": list(image.size),
                                   "compressionMethod": "quality", "encodingQuality": quality}
        # No fixed edge cap. Reduce only this input and only as far as needed.
        width, height = image.size
        while (width, height) != (1, 1):
            ratio = min(0.95, math.sqrt(RECOMMENDED_IMAGE_BYTES / len(candidate)) * 0.95)
            width, height = max(1, int(width * ratio)), max(1, int(height * ratio))
            resized = image.resize((width, height), Image.Resampling.LANCZOS)
            candidate = _encode_reference(resized, "WEBP", quality=REFERENCE_QUALITIES[-1])
            if len(candidate) <= RECOMMENDED_IMAGE_BYTES:
                return candidate, {**facts, "uploadedDimensions": [width, height],
                                   "compressionMethod": "resize", "encodingQuality": REFERENCE_QUALITIES[-1]}
        raise ValueError("无法在单图建议体积内准备参考图")


def compress_references(contents: list[bytes]) -> tuple[list[bytes], list[dict[str, Any]]]:
    result, facts = [], []
    for index, content in enumerate(contents):
        fact: dict[str, Any] = {"originalBytes": len(content),
                                "recommendedMaxBytes": RECOMMENDED_IMAGE_BYTES,
                                "compressionMethod": "unchanged"}
        try:
            if len(content) > RECOMMENDED_IMAGE_BYTES:
                uploaded, processing = recompress_reference(content)
                fact.update(processing)
            else:
                uploaded = content  # Preserve bytes, dimensions, encoding and metadata.
                with Image.open(BytesIO(content)) as source:
                    fact.update(originalDimensions=list(source.size), uploadedDimensions=list(source.size))
        except Exception as exc:
            # Stop before the paid POST; do not silently upload a broken preparation.
            raise ValueError(f"API易第 {index + 1} 张参考图准备失败：{exc}") from exc
        fact.update(uploadedBytes=len(uploaded), sha256=hashlib.sha256(uploaded).hexdigest())
        result.append(uploaded)
        facts.append(fact)
    return result, facts


def build_request(task_package: dict[str, Any]) -> dict[str, Any]:
    settings = shared.get_settings()
    prompt = str(task_package.get("prompt") or "")
    if not prompt.strip():
        raise ValueError("图片生成缺少 Prompt 维护渲染出的 prompt。")
    return {"model": settings.apiyi_image_model, "prompt": prompt, "n": 1,
            "size": settings.apiyi_image_size, "quality": "max",
            "output_format": settings.gpt_image2_output_format}


def response_metadata(response: dict[str, Any]) -> dict[str, Any]:
    return {**response, "data": [{k: v for k, v in item.items() if k != "b64_json"}
                                 for item in response.get("data", [])]}


class GPTImage2ApiyiProvider(shared.ImageGenerationProvider):
    def output_suffix(self) -> str:
        return f".{shared.get_settings().gpt_image2_output_format}"

    async def generate(self, task_package: dict[str, Any], output_path: str) -> dict[str, Any]:
        result = await self.submit(task_package, output_path)
        delivered = await self.deliver({"output_path": output_path}, result["providerStatusResponse"])
        return {**result, **delivered}

    async def submit(self, task_package, output_path, on_progress=None, should_cancel=None):
        settings = shared.get_settings()
        if not settings.apiyi_api_key or not settings.apiyi_base_url:
            raise ValueError("请配置 APIYI_API_KEY 和 APIYI_BASE_URL")
        parsed = urlparse(settings.apiyi_base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query:
            raise ValueError("APIYI_BASE_URL 必须是有效的 HTTPS API 地址")
        inputs = task_package.get("imageInputs") or []
        if not 1 <= len(inputs) <= 16:
            raise ValueError("API易图片编辑需要 1–16 张参考图")
        request = build_request(task_package)
        deadline = time.time() + settings.generation_upload_timeout_seconds
        contents = []
        for index, item in enumerate(inputs):
            if should_cancel and should_cancel():
                raise RuntimeError("图片生成已取消")
            if on_progress:
                on_progress(index, len(inputs))
            shared.ensure_upload_deadline(deadline, item)
            contents.append(shared.render_reference_input_bytes(item, deadline, prefer_original=True)[0])
        contents, facts = compress_references(contents)
        files = []
        for index, content in enumerate(contents):
            # Sniff the rendered format: crops/boards may differ from source suffixes.
            try:
                with Image.open(BytesIO(content)) as image:
                    fmt = str(image.format).lower()
            except Exception:
                fmt = str(inputs[index].get("filename") or "reference.png").rsplit(".", 1)[-1].lower()
            fmt = "jpeg" if fmt == "jpg" else fmt
            files.append(("image[]", (f"reference-{index + 1}.{fmt}", content, f"image/{fmt}")))
        if on_progress:
            on_progress(len(inputs), len(inputs))
        if should_cancel and should_cancel():
            raise RuntimeError("图片生成已取消")
        manifest = shared.build_provider_reference_image_manifest(inputs, [None] * len(inputs))
        for item, fact in zip(manifest, facts):
            item.update(fact)
            item["providerInputPolicyVersion"] = "apiyi_multipart_v2"
        snapshot = {"provider": "apiyi", "model": request["model"], "submissionMode": "completed",
                    "providerRequestId": None, "providerUrls": {}, "imageInputs": inputs,
                    "providerRequest": request, "providerReferenceImageManifest": manifest}
        started = time.monotonic()
        try:
            # requests has no automatic retries; redirects must not replay the paid POST.
            with requests.post(settings.apiyi_base_url.rstrip("/") + "/images/edits",
                               headers={"Authorization": f"Bearer {settings.apiyi_api_key}"},
                               data=request, files=files, timeout=max(360, settings.apiyi_timeout_seconds),
                               allow_redirects=False) as response:
                snapshot["httpRequestId"] = response.headers.get("x-request-id")
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if response.status_code >= 500 or response.status_code in {408, 409}:
                    raise requests.RequestException(f"HTTP {response.status_code}; 上游执行结果未知")
                if not 200 <= response.status_code < 300:
                    error = shared.normalize_toapis_error(body if isinstance(body, dict) else {},
                                                         status_code=response.status_code,
                                                         fallback_message=f"API易 HTTP {response.status_code}")
                    raise shared.ImageProviderError(error["upstreamMessage"], {**snapshot, "providerResponse": body},
                                                    error_code=error["code"], response_received=True)
                if (not isinstance(body, dict) or not isinstance(body.get("data"), list)
                        or not body["data"] or not isinstance(body["data"][0], dict)
                        or not body["data"][0].get("b64_json")):
                    raise requests.RequestException("API易成功响应缺少 b64_json，无法确认出图结果")
        except requests.RequestException as exc:
            raise shared.ProviderSubmissionUnknownError(
                "API易同步图片请求结果未知，可能已经计费，已停止自动重提。",
                {**snapshot, "status": "SUBMISSION_UNKNOWN", "providerStatus": "submission_unknown"},
            ) from exc
        body["httpRequestId"] = snapshot.get("httpRequestId")
        body["elapsedSeconds"] = round(time.monotonic() - started, 3)
        # Persist base64 once until delivery succeeds so storage retries never regenerate.
        return {**snapshot, "status": "SUCCEEDED", "providerStatus": "COMPLETED",
                "providerResponse": response_metadata(body), "providerStatusResponse": body}

    async def deliver(self, image_row, status_response):
        try:
            content = base64.b64decode(status_response["data"][0]["b64_json"], validate=True)
            with Image.open(BytesIO(content)) as image:
                mime = Image.MIME[image.format]
                image.verify()
            output_key = str(image_row["output_path"])
            shared.put_bytes(output_key, content, mime)
            derivative = shared.generate_image_derivatives(output_key, content)
        except Exception as exc:
            raise GenerationPhaseError("API易成片写入或解码失败，可重试交付而不重新生成。",
                                       ERROR_DELIVERY_FAILED, provider_response=status_response) from exc
        metadata = response_metadata(status_response)
        return {"outputPath": output_key, "fileSize": len(content),
                "thumbPath": derivative.get("thumb"), "previewPath": derivative.get("preview"),
                "thumbFileSize": derivative.get("thumbSize"), "previewFileSize": derivative.get("previewSize"),
                "providerResponse": metadata, "providerStatusResponse": metadata}
