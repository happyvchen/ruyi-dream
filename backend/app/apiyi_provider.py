"""API易 synchronous Images edits; never retry a potentially paid submission."""
from __future__ import annotations

import base64
import hashlib
import logging
import math
import time
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps

from . import image_provider as shared
from .generation_progress import ERROR_DELIVERY_FAILED, GenerationPhaseError

logger = logging.getLogger(__name__)
SINGLE_IMAGE_BYTES = 1_500_000
TOTAL_IMAGE_BYTES = 6_000_000


def recompress_reference(content: bytes, max_edge: int = 2048) -> bytes:
    """Preserve PNG/JPEG/WebP and alpha; quality 90 for lossy formats."""
    with Image.open(BytesIO(content)) as source:
        fmt = source.format
        if fmt not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError("API易参考图须为 PNG/JPEG/WebP")
        image = ImageOps.exif_transpose(source)
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, fmt, quality=90, optimize=True)
        return output.getvalue()


def compress_references(contents: list[bytes]) -> tuple[list[bytes], list[dict[str, Any]]]:
    result = list(contents)
    facts: list[dict[str, Any]] = [{"originalBytes": len(content)} for content in contents]
    failed: set[int] = set()
    for index, content in enumerate(contents):
        if len(content) <= SINGLE_IMAGE_BYTES:
            continue
        try:
            result[index] = recompress_reference(content)
        except Exception:
            failed.add(index)
            facts[index]["compressionFallback"] = True
            logger.warning("API易 reference %s compression failed; retaining original", index + 1)

    # The aggregate budget may require shrinking otherwise small individual images.
    # Never drop/reorder a reference; failed images remain byte-identical originals.
    edge = 2048
    while sum(map(len, result)) > TOTAL_IMAGE_BYTES and edge > 32:
        if sum(len(contents[index]) for index in failed) >= TOTAL_IMAGE_BYTES:
            break  # Failed originals already exhaust the budget; do not damage healthy inputs.
        edge = max(32, min(edge - 1, int(edge * math.sqrt(TOTAL_IMAGE_BYTES / sum(map(len, result))) * 0.9)))
        changed = False
        for index, content in enumerate(contents):
            if index in failed:
                continue
            try:
                candidate = recompress_reference(content, edge)
                if len(candidate) < len(result[index]):
                    result[index] = candidate
                    changed = True
            except Exception:
                result[index] = content
                failed.add(index)
                facts[index]["compressionFallback"] = True
                logger.warning("API易 reference %s compression failed; retaining original", index + 1)
        if not changed and len(failed) == len(contents):
            break
    over_budget = sum(map(len, result)) > TOTAL_IMAGE_BYTES
    if over_budget:
        logger.warning("API易 references exceed 6 MB after compression fallback; continuing with all references")
    for index, content in enumerate(result):
        facts[index].update({"uploadedBytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                             "aggregateBudgetExceeded": over_budget})
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
            contents.append(shared.render_reference_input_bytes(item, deadline)[0])
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
            item["providerInputPolicyVersion"] = "apiyi_multipart_v1"
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
