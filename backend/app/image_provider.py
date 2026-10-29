from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import tempfile
import threading
import time
from io import BytesIO
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import config
from .db import get_db, now_iso
from .generation_progress import (
    ERROR_DELIVERY_FAILED,
    ERROR_INVALID_REQUEST,
    ERROR_SUBMISSION_UNKNOWN,
    ERROR_SYSTEM_ERROR,
    ERROR_UPLOAD_FAILED,
    GenerationPhaseError,
    classify_provider_error,
    is_retryable_error,
    normalize_toapis_error,
)
from .toapis_concurrency import toapis_concurrency_slot
from .image_derivatives import generate_image_derivatives
from .image_ingestion import normalized_image_candidates
from .object_storage import get_bytes, object_exists, put_bytes

# 导入进度回调：on_progress(completed, total)
UploadProgressCallback = Callable[[int, int], None]


def normalized_or_original_key(object_key: str) -> str:
    return next((key for key in normalized_image_candidates(object_key) if object_exists(key)), object_key)


def get_settings() -> config.Settings:
    return config.get_settings()


class ImageGenerationProvider:
    """图片生成 provider 的步骤化接口。

    生命周期被拆成三个独立步骤，失败语义各不相同：

    - ``submit``：导入参考图并创建 provider 任务（失败=UPLOAD/PROVIDER 问题）。
    - ``check``：只查询任务状态，绝不下载结果（失败=瞬时网络问题，可无限容忍到超时）。
    - ``deliver``：把已成功的结果图下载回对象存储并生成缩略图
      （失败=回传问题，provider 已成功且已计费，必须可零成本重试）。

    重试/退避/超时策略统一由调用方（main.generate_image_asset）持有，
    provider 只做单次动作。
    """

    def output_suffix(self) -> str:
        return ".png"

    async def generate(self, task_package: dict[str, Any], output_path: str) -> dict[str, Any]:
        raise NotImplementedError

    async def submit(
        self,
        task_package: dict[str, Any],
        output_path: str,
        on_progress: UploadProgressCallback | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if should_cancel and should_cancel():
            raise RuntimeError("图片生成已取消")
        response = await self.generate(task_package, output_path)
        return {
            **response,
            "submissionMode": "completed",
            "status": "SUCCEEDED",
            "providerStatus": "COMPLETED",
            "providerRequestId": None,
            "providerUrls": {},
            "providerStatusResponse": response.get("providerResponse"),
        }

    async def check(self, image_row: Any) -> dict[str, Any]:
        return {"status": image_row["status"], "providerStatus": image_row["provider_status"] if "provider_status" in image_row.keys() else None}

    async def deliver(self, image_row: Any, status_response: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class ImageProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        provider_response: dict[str, Any] | None = None,
        error_code: str | None = None,
        *,
        response_received: bool | None = None,
    ):
        super().__init__(message)
        self.provider_response = provider_response or {}
        self.error_code = error_code
        self.response_received = response_received


class ProviderSubmissionUnknownError(ImageProviderError):
    """创建任务请求已发出，但连接中断导致无法确认 provider 是否已创建任务。"""

    def __init__(self, message: str, provider_response: dict[str, Any] | None = None):
        super().__init__(
            message,
            provider_response=provider_response,
            error_code=ERROR_SUBMISSION_UNKNOWN,
            response_received=False,
        )


def post_generation_task_with_confirmed_failure_retries(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    *,
    auth_scheme: str,
    error_label: str,
    timeout_seconds: int = 180,
    max_attempts: int = 11,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """创建 provider 任务。

    仅在 provider 明确返回可重试 HTTP 错误时重试；连接异常/读超时的执行
    结果未知，必须停止，避免创建重复计费任务。
    """
    for index in range(max_attempts):
        if should_cancel and should_cancel():
            raise RuntimeError("图片生成已取消")
        try:
            with toapis_concurrency_slot():
                if should_cancel and should_cancel():
                    raise RuntimeError("图片生成已取消")
                return post_json(
                    url,
                    payload,
                    api_key,
                    timeout=timeout_seconds,
                    auth_scheme=auth_scheme,
                    error_label=error_label,
                    attempts=1,
                )
        except ImageProviderError as exc:
            if exc.response_received is not True:
                raise ProviderSubmissionUnknownError(f"{error_label}，提交结果未知: {exc}") from exc
            if not is_retryable_error(exc.error_code) or index >= max_attempts - 1:
                raise
            time.sleep(min(30 * (2 ** index), 240))
    raise RuntimeError(f"{error_label}: 创建任务重试流程异常结束")


class GPTImage2FalProvider(ImageGenerationProvider):
    def output_suffix(self) -> str:
        return f".{get_settings().gpt_image2_output_format}"

    async def generate(self, task_package: dict[str, Any], output_path: str) -> dict[str, Any]:
        submit_response = await self.submit(task_package, output_path)
        request_id = submit_response.get("providerRequestId")
        if not request_id:
            raise RuntimeError(f"fal 创建任务响应缺少 request_id: {submit_response}")

        deadline = time.time() + get_settings().generation_image_timeout_seconds
        latest: dict[str, Any] = submit_response
        image_row = {
            "provider_urls_json": json.dumps(submit_response.get("providerUrls") or {}),
            "output_path": str(output_path),
        }
        while time.time() < deadline:
            time.sleep(3)
            latest = await self.check(image_row)
            if latest.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                break
        if latest.get("status") != "SUCCEEDED":
            raise RuntimeError(f"fal 生成未成功: {latest}")
        delivery = await self.deliver(image_row, latest.get("providerStatusResponse") or {})
        return {
            **submit_response,
            **latest,
            **delivery,
            "submissionMode": "completed",
        }

    async def submit(
        self,
        task_package: dict[str, Any],
        output_path: str,
        on_progress: UploadProgressCallback | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        if not settings.fal_key:
            raise RuntimeError("FAL_KEY 未配置，无法调用 GPT Image-2 fal 接口")

        uploaded_inputs, image_urls = prepare_fal_image_inputs(task_package.get("imageInputs", []), on_progress)

        provider_request = build_fal_request_snapshot(task_package, image_urls)
        if should_cancel and should_cancel():
            raise RuntimeError("图片生成已取消")
        submit_response = post_json(settings.gpt_image2_queue_url, provider_request, settings.fal_key, timeout=60)
        request_id = submit_response.get("request_id") or submit_response.get("requestId")
        provider_urls = {
            "statusUrl": submit_response.get("status_url"),
            "responseUrl": submit_response.get("response_url"),
            "cancelUrl": submit_response.get("cancel_url"),
        }
        # 注意：submit 响应不携带 outputPath。outputPath 的语义是"成片已回传到
        # 对象存储的实际位置"，只能由 store_delivered_image 在回传成功后写入；
        # 编排层依赖"payload 是否有 outputPath"判断是否还需要执行回传阶段。
        return {
            "provider": "fal",
            # fal 的实际模型由队列路由决定，不能沿用 ToAPIs 的 Sunburst 默认标识。
            "model": (
                "gpt-image-2"
                if urlparse(settings.gpt_image2_queue_url).path.rstrip("/") == "/openai/gpt-image-2/edit"
                else settings.gpt_image2_model_id
            ),
            "submissionMode": "queued",
            "status": "SUBMITTED",
            "providerStatus": "SUBMITTED",
            "providerRequestId": request_id,
            "providerUrls": provider_urls,
            "imageInputs": uploaded_inputs,
            "providerRequest": provider_request,
            "providerResponse": submit_response,
            "providerStatusResponse": submit_response,
        }

    async def check(self, image_row: Any) -> dict[str, Any]:
        settings = get_settings()
        provider_urls = json.loads(image_row["provider_urls_json"] or "{}")
        status_url = provider_urls.get("statusUrl")
        if not status_url:
            return {"status": "FAILED", "providerStatus": "MISSING_STATUS_URL", "error": "缺少 fal status_url"}

        status_response = get_json(add_query_param(status_url, "logs", "1"), settings.fal_key, timeout=60)
        provider_status = str(status_response.get("status") or status_response.get("state") or "").upper()
        next_status = map_fal_status(provider_status)
        result: dict[str, Any] = {
            "status": next_status,
            "providerStatus": provider_status,
            "providerStatusResponse": status_response,
        }
        if next_status == "FAILED":
            result["error"] = status_response.get("error") or status_response.get("message") or "fal 生成失败"
        return result

    async def deliver(self, image_row: Any, status_response: dict[str, Any]) -> dict[str, Any]:
        settings = get_settings()
        provider_urls = json.loads(image_row["provider_urls_json"] or "{}")
        response_url = provider_urls.get("responseUrl")
        if not response_url:
            raise GenerationPhaseError("缺少 fal response_url，无法回传成片", ERROR_DELIVERY_FAILED)
        try:
            response = get_json(response_url, settings.fal_key, timeout=60)
        except Exception as exc:
            raise GenerationPhaseError(f"fal 结果查询失败: {exc}", ERROR_DELIVERY_FAILED) from exc
        images = response.get("images") if isinstance(response, dict) else None
        output_url = images[0].get("url") if images else None
        if not output_url:
            raise GenerationPhaseError(f"fal response 缺少图片 URL: {response}", ERROR_DELIVERY_FAILED)
        return store_delivered_image(str(image_row["output_path"]), str(output_url), response)


class GPTImage2ToapisProvider(ImageGenerationProvider):
    def output_suffix(self) -> str:
        return f".{get_settings().gpt_image2_output_format}"

    async def generate(self, task_package: dict[str, Any], output_path: str) -> dict[str, Any]:
        submit_response = await self.submit(task_package, output_path)
        task_id = submit_response.get("providerRequestId")
        if not task_id:
            raise RuntimeError(f"toapis 创建任务响应缺少 id: {submit_response}")

        deadline = time.time() + get_settings().generation_image_timeout_seconds
        image_row = {"provider_request_id": task_id, "output_path": output_path}
        latest: dict[str, Any] = submit_response
        while time.time() < deadline:
            time.sleep(3)
            try:
                latest = await self.check(image_row)
            except Exception:
                continue
            if latest.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                break
        if latest.get("status") != "SUCCEEDED":
            raise ImageProviderError(f"toapis 生成未成功，providerRequestId={task_id}: {latest}", {**submit_response, **latest})
        delivery = await self.deliver(image_row, latest.get("providerStatusResponse") or {})
        return {
            **submit_response,
            **latest,
            **delivery,
            "submissionMode": "completed",
        }

    async def submit(
        self,
        task_package: dict[str, Any],
        output_path: str,
        on_progress: UploadProgressCallback | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        if not settings.toapis_api_key:
            raise RuntimeError("TOAPIS_API_KEY 未配置，无法调用 GPT Image-2 toapis 接口")

        uploaded_inputs, image_urls = prepare_toapis_image_inputs(task_package.get("imageInputs", []), on_progress)
        provider_request = build_toapis_request_snapshot(task_package, image_urls)
        provider_reference_manifest = build_provider_reference_image_manifest(uploaded_inputs, image_urls)
        try:
            submit_response = post_generation_task_with_confirmed_failure_retries(
                toapis_url("/v1/images/generations"),
                provider_request,
                settings.toapis_api_key,
                auth_scheme="Bearer",
                error_label="GPT Image-2 toapis 请求失败",
                timeout_seconds=settings.toapis_image_submit_timeout_seconds,
                max_attempts=settings.generation_submit_retry_limit + 1,
                should_cancel=should_cancel,
            )
        except ProviderSubmissionUnknownError as exc:
            exc.provider_response = {
                "provider": "toapis",
                "model": provider_request.get("model"),
                "submissionMode": "queued",
                "status": "SUBMISSION_UNKNOWN",
                "providerStatus": "submission_unknown",
                "providerRequestId": None,
                "imageInputs": uploaded_inputs,
                "providerReferenceImageManifest": provider_reference_manifest,
                "providerRequest": provider_request,
            }
            raise
        task_id = submit_response.get("id") or submit_response.get("task_id")
        # 注意：submit 响应不携带 outputPath。outputPath 的语义是"成片已回传到
        # 对象存储的实际位置"，只能由 store_delivered_image 在回传成功后写入；
        # 编排层依赖"payload 是否有 outputPath"判断是否还需要执行回传阶段。
        # 若在这里提前返回预期路径，轮询成功后会跳过回传、直接读一个不存在的
        # key，导致 NoSuchKey。
        return {
            "provider": "toapis",
            "model": provider_request.get("model"),
            "submissionMode": "queued",
            "status": "SUBMITTED",
            "providerStatus": submit_response.get("status", "submitted"),
            "providerRequestId": task_id,
            "providerUrls": {"statusUrl": toapis_url(f"/v1/images/generations/{task_id}") if task_id else None},
            "imageInputs": uploaded_inputs,
            "providerReferenceImageManifest": provider_reference_manifest,
            "providerRequest": provider_request,
            "providerResponse": submit_response,
            "providerStatusResponse": submit_response,
        }

    async def check(self, image_row: Any) -> dict[str, Any]:
        settings = get_settings()
        task_id = image_row["provider_request_id"]
        if not task_id:
            return {"status": "FAILED", "providerStatus": "MISSING_TASK_ID", "error": "缺少 toapis task id"}

        with toapis_concurrency_slot():
            status_response = get_json(
                toapis_url(f"/v1/images/generations/{task_id}"),
                settings.toapis_api_key,
                timeout=60,
                auth_scheme="Bearer",
                error_label="GPT Image-2 toapis 状态查询失败",
            )
        provider_status = str(status_response.get("status") or "").lower()
        resolved_task_id = status_response.get("id") or task_id
        next_status = map_toapis_status(provider_status)
        result: dict[str, Any] = {
            "status": next_status,
            "providerStatus": provider_status,
            "providerRequestId": resolved_task_id,
            "providerStatusResponse": status_response,
        }
        if next_status == "FAILED":
            error = status_response.get("error") if isinstance(status_response.get("error"), dict) else {}
            result["error"] = error.get("message") or status_response.get("fail_reason") or status_response.get("message") or "toapis 生成失败"
            result["providerError"] = normalize_toapis_error(
                status_response,
                fallback_message=str(result["error"]),
            )
        return result

    async def deliver(self, image_row: Any, status_response: dict[str, Any]) -> dict[str, Any]:
        output_url = extract_toapis_output_url(status_response)
        if not output_url:
            # 状态快照里没有 URL 时（如恢复现场），重新查询一次最新状态。
            refreshed = await self.check(image_row)
            status_response = refreshed.get("providerStatusResponse") or {}
            output_url = extract_toapis_output_url(status_response)
        if not output_url:
            raise GenerationPhaseError(f"toapis response 缺少图片 URL: {status_response}", ERROR_DELIVERY_FAILED)
        return store_delivered_image(str(image_row["output_path"]), str(output_url), status_response)


def store_delivered_image(output_key: str, output_url: str, provider_response: dict[str, Any]) -> dict[str, Any]:
    """单次成片回传动作：下载→写对象存储→生成缩略图。

    只做一次尝试（单次下载请求超时较短），退避重试由调用方统一控制；
    任何失败都抛 GenerationPhaseError(DELIVERY_FAILED)，provider 侧成功的事实不丢。
    """
    settings = get_settings()
    try:
        content, content_type = download_bytes(
            output_url,
            timeout=settings.generation_download_request_timeout_seconds,
            attempts=1,
        )
    except GenerationPhaseError:
        raise
    except Exception as exc:
        raise GenerationPhaseError(
            f"成片下载失败: {exc}",
            ERROR_DELIVERY_FAILED,
            provider_response=provider_response,
        ) from exc
    try:
        put_bytes(output_key, content, content_type or f"image/{settings.gpt_image2_output_format}")
        derivative = generate_image_derivatives(output_key, content)
    except GenerationPhaseError:
        raise
    except Exception as exc:
        raise GenerationPhaseError(
            f"本地对象存储或衍生图处理失败: {exc}",
            ERROR_SYSTEM_ERROR,
            provider_response=provider_response,
        ) from exc
    return {
        "providerResponse": provider_response,
        "outputPath": output_key,
        "fileSize": len(content),
        "thumbPath": derivative.get("thumb"),
        "previewPath": derivative.get("preview"),
        "thumbFileSize": derivative.get("thumbSize"),
        "previewFileSize": derivative.get("previewSize"),
    }


# -- provider 参考图导入缓存 -------------------------------------------------
#
# 同一 objectKey 的参考图（主推款、搭配服装、模特参考图、分镜预览）在 5 张成片
# 并发生成时会被重复导入 5 次。缓存 + 进程内按 key 加锁：第一个导入，其余复用。

_upload_key_locks: dict[tuple[str, str], threading.Lock] = {}
_upload_key_locks_guard = threading.Lock()


def _upload_lock_for(provider: str, object_key: str) -> threading.Lock:
    with _upload_key_locks_guard:
        return _upload_key_locks.setdefault((provider, object_key), threading.Lock())


def get_cached_provider_upload_url(provider: str, object_key: str) -> str | None:
    ttl = get_settings().provider_upload_cache_ttl_seconds
    with get_db() as db:
        row = db.execute(
            "SELECT provider_url, created_at FROM provider_upload_cache WHERE provider = ? AND object_key = ?",
            (provider, object_key),
        ).fetchone()
        if not row:
            return None
        try:
            from datetime import datetime, timezone

            created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - created).total_seconds()
        except ValueError:
            age = ttl + 1
        if age > ttl:
            db.execute(
                "DELETE FROM provider_upload_cache WHERE provider = ? AND object_key = ?",
                (provider, object_key),
            )
            return None
        return str(row["provider_url"])


def store_provider_upload_url(provider: str, object_key: str, provider_url: str) -> None:
    with get_db() as db:
        db.execute(
            """
            INSERT INTO provider_upload_cache (provider, object_key, provider_url, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(provider, object_key) DO UPDATE SET provider_url = excluded.provider_url, created_at = excluded.created_at
            """,
            (provider, object_key, provider_url, now_iso()),
        )


def upload_object_with_cache(provider: str, object_key: str, uploader: Callable[[], str]) -> str:
    with _upload_lock_for(provider, object_key):
        cached = get_cached_provider_upload_url(provider, object_key)
        if cached:
            return cached
        provider_url = uploader()
        store_provider_upload_url(provider, object_key, provider_url)
        return provider_url


def upload_bytes_with_cache(
    provider: str,
    content: bytes,
    uploader: Callable[[], str],
    *,
    cache_namespace: str = "collage",
) -> str:
    """运行时拼图使用内容哈希复用现有 provider_upload_cache。"""
    cache_key = f"{cache_namespace}:{hashlib.sha256(content).hexdigest()}"
    return upload_object_with_cache(provider, cache_key, uploader)


def _fal_preview_image_size(settings: Any) -> Any:
    """分镜预览在 fal 路径的画布尺寸。

    fal 的 image_size 枚举没有 3:2，用显式宽高对象表达 3:2 横版，
    让 3 列 × 2 行的 6 个分格各自为 1:1。配置形如 "1536x1024"。
    """
    raw = str(getattr(settings, "gpt_image2_preview_fal_image_size", "") or "").strip().lower()
    if "x" in raw:
        width_str, _, height_str = raw.partition("x")
        try:
            width = int(width_str)
            height = int(height_str)
        except ValueError:
            width = height = 0
        if width > 0 and height > 0:
            return {"width": width, "height": height}
    return raw or "square_hd"


def build_fal_request_snapshot(task_package: dict[str, Any], image_urls: list[str]) -> dict[str, Any]:
    settings = get_settings()
    generation = task_package.get("generation", {}) if isinstance(task_package.get("generation"), dict) else {}
    stage = generation.get("stage")
    prompt = str(task_package.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("图片生成缺少 Prompt 维护渲染出的 prompt。")
    return {
        "prompt": prompt,
        "image_urls": image_urls,
        "image_size": _fal_preview_image_size(settings) if stage == "preview_grid" else "auto",
        "quality": "high" if stage == "preview_grid" else settings.gpt_image2_quality,
        "num_images": 1,
        "output_format": settings.gpt_image2_output_format,
        "sync_mode": False,
    }


def build_toapis_request_snapshot(task_package: dict[str, Any], image_urls: list[str]) -> dict[str, Any]:
    settings = get_settings()
    generation = task_package.get("generation", {}) if isinstance(task_package.get("generation"), dict) else {}
    stage = generation.get("stage")
    prompt = str(task_package.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("图片生成缺少 Prompt 维护渲染出的 prompt。")
    if len(image_urls) > TOAPIS_MAX_REFERENCE_IMAGES:
        raise RuntimeError(f"参考图最多支持 {TOAPIS_MAX_REFERENCE_IMAGES} 张")
    image_inputs = task_package.get("imageInputs") if isinstance(task_package.get("imageInputs"), list) else []
    request = {
        "model": settings.gpt_image2_model_id,
        "prompt": prompt,
        "image_urls": image_urls,
        "n": 1,
        "size": settings.gpt_image2_preview_size if stage == "preview_grid" else "1:1",
        "resolution": str(settings.gpt_image2_resolution).lower(),
        "quality": "high" if stage == "final_image" else settings.gpt_image2_quality,
        "output_format": settings.gpt_image2_output_format,
        "response_format": "url",
    }
    client_business_id = str(task_package.get("clientBusinessId") or "").strip()
    if client_business_id:
        request["client_business_id"] = client_business_id
    return request


def build_provider_reference_image_manifest(image_inputs: list[dict[str, Any]], image_urls: list[str]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for index, url in enumerate(image_urls, start=1):
        item = image_inputs[index - 1] if index - 1 < len(image_inputs) and isinstance(image_inputs[index - 1], dict) else {}
        manifest.append(
            {
                "reference_index": index,
                "role": str(item.get("role") or ""),
                "assetId": item.get("assetId"),
                "objectKey": item.get("objectKey"),
                "filename": item.get("filename"),
                "angle": item.get("angle"),
                "referenceBoardKind": item.get("referenceBoardKind"),
                "collageRule": item.get("collageRule"),
                "providerCrop": item.get("providerCrop"),
                "providerInputPolicyVersion": item.get("providerInputPolicyVersion"),
                "collageSources": build_collage_source_manifest(item),
                "url": url,
            }
        )
    return manifest


def build_collage_source_manifest(item: dict[str, Any]) -> list[dict[str, Any]]:
    sources = item.get("collageItems") if isinstance(item.get("collageItems"), list) else []
    manifest: list[dict[str, Any]] = []
    for source_index, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            continue
        manifest.append(
            {
                "source_index": source_index,
                "role": str(source.get("role") or ""),
                "assetId": source.get("assetId"),
                "objectKey": source.get("objectKey"),
                "filename": source.get("filename"),
                "angle": source.get("angle"),
                "materialCode": source.get("materialCode"),
            }
        )
    return manifest


def upload_image_inputs(
    image_inputs: list[dict[str, Any]],
    on_progress: UploadProgressCallback | None = None,
) -> list[dict[str, Any]]:
    uploaded: list[dict[str, Any]] = []
    total = len(image_inputs)
    deadline = time.time() + get_settings().generation_upload_timeout_seconds
    for index, item in enumerate(image_inputs):
        if on_progress:
            on_progress(index, total)
        ensure_upload_deadline(deadline, item)
        object_key = item.get("objectKey")
        source_url = item.get("providerUrl") or item.get("url")
        next_item = dict(item)
        if source_url and is_fal_cdn_url(str(source_url)):
            next_item["providerUrl"] = source_url
            uploaded.append(next_item)
            continue
        if item.get("collageItems"):
            content, filename, content_type = build_reference_collage_bytes(item, deadline)
            next_item["providerUrl"] = upload_bytes_with_cache(
                "fal",
                content,
                lambda: upload_bytes_to_fal(content, filename, content_type),
            )
            uploaded.append(next_item)
            continue
        if object_key and object_exists(str(object_key)):
            source_key = normalized_or_original_key(str(object_key))
            next_item["sourceObjectKey"] = source_key
            next_item["providerUrl"] = upload_object_with_cache(
                "fal",
                source_key,
                lambda key=source_key: upload_object_to_fal(key, os.path.basename(key)),
            )
            uploaded.append(next_item)
            continue
        if source_url and str(source_url).startswith(("http://", "https://")):
            next_item["sourceUrl"] = source_url
            next_item["providerUrl"] = upload_url_to_fal(str(source_url), str(item.get("filename") or "reference-image"))
            uploaded.append(next_item)
            continue
        uploaded.append(next_item)
    if on_progress:
        on_progress(total, total)
    return uploaded


def ensure_upload_deadline(deadline: float, item: dict[str, Any]) -> None:
    if time.time() > deadline:
        label = str(item.get("role") or item.get("filename") or item.get("objectKey") or "reference-image")
        raise GenerationPhaseError(
            f"参考图导入超出总预算（{get_settings().generation_upload_timeout_seconds}s），未完成：{label}",
            ERROR_UPLOAD_FAILED,
        )


def remaining_upload_timeout(deadline: float | None) -> int:
    """把单次网络超时裁剪到参考图导入阶段的剩余总预算。"""
    if deadline is None:
        return 300
    remaining = deadline - time.time()
    if remaining <= 0:
        raise GenerationPhaseError("参考图导入超出总预算", ERROR_UPLOAD_FAILED)
    return max(1, min(300, int(remaining)))


def prepare_fal_image_inputs(
    image_inputs: list[dict[str, Any]],
    on_progress: UploadProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    uploaded_inputs = upload_image_inputs(image_inputs, on_progress)
    missing_roles = [
        str(item.get("role") or item.get("filename") or index)
        for index, item in enumerate(uploaded_inputs, start=1)
        if not item.get("providerUrl")
    ]
    if missing_roles:
        raise RuntimeError(f"参考图导入到 fal 失败，以下输入没有 fal Image URL：{', '.join(missing_roles)}")

    image_urls = [str(item["providerUrl"]) for item in uploaded_inputs]
    non_fal_urls = [url for url in image_urls if not is_fal_cdn_url(url)]
    if non_fal_urls:
        raise RuntimeError(f"拒绝将非 fal Image URL 传给 GPT Image-2：{', '.join(non_fal_urls)}")
    if not image_urls:
        raise RuntimeError("没有可传给 GPT Image-2 的 fal image_urls")
    return uploaded_inputs, image_urls


def prepare_toapis_image_inputs(
    image_inputs: list[dict[str, Any]],
    on_progress: UploadProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if len(image_inputs) > TOAPIS_MAX_REFERENCE_IMAGES:
        raise RuntimeError(f"参考图最多支持 {TOAPIS_MAX_REFERENCE_IMAGES} 张")
    uploaded_inputs = upload_toapis_image_inputs(image_inputs, on_progress)
    missing_roles = [
        str(item.get("role") or item.get("filename") or index)
        for index, item in enumerate(uploaded_inputs, start=1)
        if not item.get("providerUrl")
    ]
    if missing_roles:
        raise RuntimeError(f"参考图导入到 toapis 失败，以下输入没有 toapis Image URL：{', '.join(missing_roles)}")
    image_urls = [str(item["providerUrl"]) for item in uploaded_inputs]
    if not image_urls:
        raise RuntimeError("没有可传给 GPT Image-2 的 image_urls")
    return uploaded_inputs, image_urls


def upload_toapis_image_inputs(
    image_inputs: list[dict[str, Any]],
    on_progress: UploadProgressCallback | None = None,
) -> list[dict[str, Any]]:
    uploaded: list[dict[str, Any]] = []
    total = len(image_inputs)
    deadline = time.time() + get_settings().generation_upload_timeout_seconds
    for index, item in enumerate(image_inputs):
        if on_progress:
            on_progress(index, total)
        ensure_upload_deadline(deadline, item)
        object_key = item.get("objectKey")
        source_url = item.get("providerUrl") or item.get("url")
        next_item = dict(item)
        existing_policy_version = str(item.get("providerInputPolicyVersion") or "")
        next_item["providerInputPolicyVersion"] = f"toapis-input-{TOAPIS_INPUT_POLICY_VERSION}"
        if (
            source_url
            and is_toapis_file_url(str(source_url))
            and existing_policy_version == f"toapis-input-{TOAPIS_INPUT_POLICY_VERSION}"
        ):
            next_item["providerUrl"] = source_url
            uploaded.append(next_item)
            continue
        if item.get("collageItems"):
            content, filename, content_type = build_reference_collage_bytes(item, deadline)
            next_item["providerUrl"] = upload_bytes_with_cache(
                "toapis",
                content,
                lambda: upload_bytes_to_toapis(content, filename, content_type, deadline=deadline),
                cache_namespace=f"input-{TOAPIS_INPUT_POLICY_VERSION}",
            )
            uploaded.append(next_item)
            continue
        if object_key and object_exists(str(object_key)):
            source_key = normalized_or_original_key(str(object_key))
            next_item["sourceObjectKey"] = source_key
            crop = item.get("providerCrop") if isinstance(item.get("providerCrop"), dict) else {}
            if crop:
                # 按镜头预裁切的参考图（目前只用于姿势图）：不修改原素材，
                # 只生成一份运行时传输副本，并按内容哈希复用上传缓存。
                cropped = crop_provider_input_bytes(get_bytes(source_key), crop)
                next_item["providerCropApplied"] = {
                    "kind": str(crop.get("kind") or ""),
                    "ratio": crop.get("ratio"),
                }
                next_item["providerUrl"] = upload_bytes_with_cache(
                    "toapis",
                    cropped,
                    lambda: upload_bytes_to_toapis(cropped, "reference-crop.jpg", "image/jpeg", deadline=deadline),
                    cache_namespace=f"input-{TOAPIS_INPUT_POLICY_VERSION}",
                )
                uploaded.append(next_item)
                continue
            next_item["providerUrl"] = upload_object_with_cache(
                "toapis",
                toapis_upload_cache_key(source_key),
                lambda key=source_key: upload_object_to_toapis(key, os.path.basename(key), deadline=deadline),
            )
            uploaded.append(next_item)
            continue
        if source_url and str(source_url).startswith(("http://", "https://")):
            next_item["sourceUrl"] = source_url
            next_item["providerUrl"] = upload_url_to_toapis(
                str(source_url), str(item.get("filename") or "reference-image"), deadline=deadline
            )
            uploaded.append(next_item)
            continue
        uploaded.append(next_item)
    if on_progress:
        on_progress(total, total)
    return uploaded


def crop_provider_input_bytes(content: bytes, crop: dict[str, Any]) -> bytes:
    """按输入策略裁切参考图的运行时传输副本。

    当前只支持 ``keep_top_ratio``：保留图片上部，用于把全身姿势图裁成当前镜头的取景，
    让 provider 不会把每个镜头都拉回全身构图。原始素材不做任何写回。
    """
    kind = str(crop.get("kind") or "")
    if kind != "keep_top_ratio":
        raise RuntimeError(f"未知的参考图裁切策略：{kind or '(empty)'}")
    try:
        ratio = float(crop.get("ratio") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"参考图裁切比例无效：{crop.get('ratio')}") from exc
    if not 0 < ratio < 1:
        raise RuntimeError(f"参考图裁切比例必须在 0 和 1 之间：{ratio}")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise GenerationPhaseError("图片处理组件不可用，无法裁切参考图", ERROR_SYSTEM_ERROR) from exc
    try:
        with Image.open(BytesIO(content)) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode in {"RGBA", "LA", "P"}:
                base = image.convert("RGBA")
                flattened = Image.new("RGB", base.size, (255, 255, 255))
                flattened.paste(base, mask=base.getchannel("A"))
                image = flattened
            elif image.mode != "RGB":
                image = image.convert("RGB")
            height = max(1, int(round(image.height * ratio)))
            cropped = image.crop((0, 0, image.width, height))
            output = BytesIO()
            cropped.save(output, "JPEG", quality=95, optimize=True)
            return output.getvalue()
    except (OSError, ValueError) as exc:
        raise GenerationPhaseError("参考图无法解码或裁切", ERROR_INVALID_REQUEST) from exc


def build_reference_collage_bytes(item: dict[str, Any], deadline: float | None = None) -> tuple[bytes, str, str]:
    sources = item.get("collageItems") if isinstance(item.get("collageItems"), list) else []
    images = []
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("Pillow 未安装，无法拼接 Image-2 参考图") from exc
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get('collageItems'):
            content, _, _content_type = build_reference_collage_bytes(source, deadline)
        else:
            content, _content_type = reference_source_bytes(source, deadline)
            if source.get('providerCrop'):
                content = crop_provider_input_bytes(content, source['providerCrop'])
        try:
            with Image.open(BytesIO(content)) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode in {"RGBA", "LA", "P"}:
                    base = image.convert("RGBA")
                    flattened = Image.new("RGB", base.size, (255, 255, 255))
                    flattened.paste(base, mask=base.getchannel("A") if "A" in base.getbands() else None)
                    image = flattened
                elif image.mode != "RGB":
                    image = image.convert("RGB")
                images.append(image.copy())
        except OSError as exc:
            label = str(source.get("filename") or source.get("objectKey") or "reference-image")
            raise RuntimeError(f"参考图无法解码，不能拼接：{label}") from exc
    if not images:
        raise RuntimeError("参考图拼接缺少可用来源图片")

    if len(images) == 1:
        output = BytesIO()
        images[0].save(output, "JPEG", quality=95, optimize=True)
        stem = os.path.splitext(str(item.get("filename") or "reference-sheet.jpg"))[0] or "reference-sheet"
        return output.getvalue(), f"{stem}.jpg", "image/jpeg"

    columns, rows = reference_collage_grid(len(images))
    tile_size = REFERENCE_COLLAGE_TILE_SIZE
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow 未安装，无法拼接 Image-2 参考图") from exc
    labels = item.get("tileLabels") or []
    header = 36 if labels else 0
    canvas = Image.new("RGB", (columns * tile_size, rows * (tile_size + header)), (255, 255, 255))
    for index, image in enumerate(images[:columns * rows]):
        fitted = contain_on_white(image, tile_size, tile_size)
        col = index % columns
        row = index // columns
        canvas.paste(fitted, (col * tile_size, row * (tile_size + header) + header))
        if index < len(labels):
            from PIL import ImageDraw
            draw = ImageDraw.Draw(canvas)
            left, top = col * tile_size, row * (tile_size + header)
            draw.text((left + 8, top + 4), str(labels[index]), fill="black", font_size=24)
    output = BytesIO()
    canvas.save(output, "JPEG", quality=95, optimize=True)
    stem = os.path.splitext(str(item.get("filename") or "reference-sheet.jpg"))[0] or "reference-sheet"
    return output.getvalue(), f"{stem}.jpg", "image/jpeg"


def reference_collage_grid(count: int) -> tuple[int, int]:
    if count <= 2:
        return max(1, count), 1
    if count <= 4:
        return 2, 2
    if count <= 6:
        return 3, 2
    return 4, (count + 3) // 4


def contain_on_white(image: Any, width: int, height: int) -> Any:
    from PIL import Image

    copy = image.copy()
    copy.thumbnail((width, height), Image.Resampling.LANCZOS)
    tile = Image.new("RGB", (width, height), (255, 255, 255))
    left = (width - copy.width) // 2
    top = (height - copy.height) // 2
    tile.paste(copy, (left, top))
    return tile


def reference_source_bytes(source: dict[str, Any], deadline: float | None = None) -> tuple[bytes, str | None]:
    object_key = source.get("objectKey")
    source_url = source.get("providerUrl") or source.get("url")
    if object_key and object_exists(str(object_key)):
        source_key = normalized_or_original_key(str(object_key))
        return get_bytes(source_key), mimetypes.guess_type(str(source.get("filename") or source_key))[0]
    if source_url and str(source_url).startswith(("http://", "https://")):
        return download_bytes(str(source_url), timeout=remaining_upload_timeout(deadline), attempts=1)
    raise RuntimeError(f"参考图拼接来源不可用：{source.get('filename') or source.get('objectKey') or source.get('url')}")


def is_fal_cdn_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host in {"fal.media", "v3.fal.media"} or host.endswith(".fal.media")


def is_toapis_file_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "files.toapis.cn" or host.endswith(".toapis.cn")


def upload_object_to_fal(object_key: str, filename: str) -> str:
    content = get_bytes(object_key)
    return upload_bytes_to_fal(content, filename)


def upload_url_to_fal(url: str, filename: str) -> str:
    content, content_type = download_bytes(url)
    return upload_bytes_to_fal(content, filename, content_type)


def upload_object_to_toapis(object_key: str, filename: str, deadline: float | None = None) -> str:
    content = get_bytes(object_key)
    return upload_bytes_to_toapis(content, filename, deadline=deadline)


def upload_url_to_toapis(url: str, filename: str, deadline: float | None = None) -> str:
    content, content_type = download_bytes(url, timeout=remaining_upload_timeout(deadline), attempts=1)
    return upload_bytes_with_cache(
        "toapis",
        content,
        lambda: upload_bytes_to_toapis(content, filename, content_type, deadline=deadline),
        cache_namespace=f"input-{TOAPIS_INPUT_POLICY_VERSION}",
    )


def upload_bytes_to_fal(content: bytes, filename: str, content_type: str | None = None) -> str:
    settings = get_settings()
    if not settings.fal_key:
        raise RuntimeError("FAL_KEY 未配置，无法导入参考图到 fal")
    try:
        import fal_client
    except ImportError as exc:
        raise RuntimeError("fal-client 导入失败，无法导入参考图到 fal：请安装 fal-client") from exc

    os.environ["FAL_KEY"] = settings.fal_key
    suffix = filename_suffix(filename, content_type)
    with tempfile.NamedTemporaryFile(prefix="ruyi-dream-fal-input-", suffix=suffix) as handle:
        handle.write(content)
        handle.flush()
        return fal_client.upload_file(handle.name)


# toapis 单文件导入上限 10MB；留出安全余量，只压超限大图，小图原样导入。
TOAPIS_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
TOAPIS_TARGET_UPLOAD_BYTES = TOAPIS_MAX_UPLOAD_BYTES - 512 * 1024
TOAPIS_MAX_REFERENCE_IMAGES = 6
TOAPIS_MAX_INPUT_LONG_EDGE = 8192
TOAPIS_MAX_INPUT_PIXELS = 40_000_000
TOAPIS_TARGET_INPUT_LONG_EDGE = 8000
TOAPIS_TARGET_INPUT_PIXELS = 36_000_000
TOAPIS_INPUT_POLICY_VERSION = "v2"
FIT_QUALITY_STEPS = (95, 92, 88, 85)
FIT_SCALE_STEP = 0.85
REFERENCE_COLLAGE_TILE_SIZE = 1400


def toapis_input_target_size(width: int, height: int) -> tuple[int, int]:
    """返回 ToAPIs 传输副本尺寸；合规图片保持原尺寸，超限时等比缩小并留安全余量。"""
    width = max(1, int(width))
    height = max(1, int(height))
    scale = 1.0
    if max(width, height) > TOAPIS_TARGET_INPUT_LONG_EDGE:
        scale = min(scale, TOAPIS_TARGET_INPUT_LONG_EDGE / max(width, height))
    pixels = width * height
    if pixels > TOAPIS_TARGET_INPUT_PIXELS:
        scale = min(scale, math.sqrt(TOAPIS_TARGET_INPUT_PIXELS / pixels))
    if scale >= 1.0:
        return width, height
    return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))


def toapis_upload_cache_key(object_key: str) -> str:
    """预处理策略升级时自然失效旧 provider URL，不清空共享缓存表。"""
    return f"input-{TOAPIS_INPUT_POLICY_VERSION}:{object_key}"


def fit_image_bytes_under_limit(
    content: bytes,
    filename: str,
    content_type: str | None,
    target_bytes: int = TOAPIS_TARGET_UPLOAD_BYTES,
) -> tuple[bytes, str, str | None]:
    """把图片规范成 ToAPIs 支持的传输副本；原始媒体内容不做任何写回。

    同时约束长边、总像素和编码字节。尺寸合规时优先保留原分辨率、只降
    编码质量；仍超限才逐档缩小边长。
    目标是"压得越少越好"，保证参考图清晰度，避免把大图压得远小于其他输入图。
    """
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise GenerationPhaseError("图片处理组件不可用，无法校验参考图", ERROR_SYSTEM_ERROR) from exc
    try:
        with Image.open(BytesIO(content)) as image:
            image = ImageOps.exif_transpose(image)
            image.load()
            target_width, target_height = toapis_input_target_size(image.width, image.height)
            dimensions_compliant = (target_width, target_height) == image.size
            has_alpha = image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info)
            if dimensions_compliant and len(content) <= target_bytes and not has_alpha:
                return content, filename, content_type
            if image.mode in {"RGBA", "LA", "P"}:
                base = image.convert("RGBA")
                flattened = Image.new("RGB", base.size, (255, 255, 255))
                flattened.paste(base, mask=base.getchannel("A"))
                image = flattened
            elif image.mode != "RGB":
                image = image.convert("RGB")

            width, height = target_width, target_height
            while True:
                if (width, height) == image.size:
                    resized = image
                else:
                    resized = image.resize((width, height), Image.Resampling.LANCZOS)
                for quality in FIT_QUALITY_STEPS:
                    output = BytesIO()
                    resized.save(output, "JPEG", quality=quality, optimize=True)
                    candidate = output.getvalue()
                    if len(candidate) <= target_bytes:
                        stem = os.path.splitext(filename)[0] or "reference-image"
                        return candidate, f"{stem}.jpg", "image/jpeg"
                if width == 1 and height == 1:
                    break
                next_width = max(1, math.floor(width * FIT_SCALE_STEP))
                next_height = max(1, math.floor(height * FIT_SCALE_STEP))
                if next_width == width and width > 1:
                    next_width -= 1
                if next_height == height and height > 1:
                    next_height -= 1
                width, height = next_width, next_height
    except (OSError, ValueError) as exc:
        raise GenerationPhaseError(
            f"参考图无法解码或转换：{filename}",
            ERROR_INVALID_REQUEST,
        ) from exc
    raise GenerationPhaseError(
        f"参考图转换后仍超过导入大小限制：{filename}",
        ERROR_INVALID_REQUEST,
    )


def upload_bytes_to_toapis(
    content: bytes,
    filename: str,
    content_type: str | None = None,
    *,
    deadline: float | None = None,
) -> str:
    settings = get_settings()
    if not settings.toapis_api_key:
        raise RuntimeError("TOAPIS_API_KEY 未配置，无法导入参考图到 toapis")
    content, filename, content_type = fit_image_bytes_under_limit(content, filename, content_type)
    with toapis_concurrency_slot():
        upload_response = post_multipart_file(
            toapis_url("/v1/uploads/images"),
            "file",
            filename,
            content,
            content_type or mimetypes.guess_type(filename)[0] or "image/png",
            settings.toapis_api_key,
            timeout=remaining_upload_timeout(deadline),
            attempts=1,
        )
    data = upload_response.get("data") if isinstance(upload_response, dict) else None
    uploaded_url = data.get("url") if isinstance(data, dict) else None
    if not uploaded_url:
        raise RuntimeError(f"toapis 导入图片响应缺少 URL: {upload_response}")
    return str(uploaded_url)


def filename_suffix(filename: str, content_type: str | None = None) -> str:
    suffix = os.path.splitext(filename)[1]
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(content_type or "") if content_type else None
    return guessed or ".png"


def post_json(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    timeout: int | None = 300,
    auth_scheme: str = "Key",
    error_label: str = "GPT Image-2 fal 请求失败",
    attempts: int = 11,
) -> dict[str, Any]:
    if auth_scheme == "Bearer":
        return request_json("POST", url, api_key, timeout, auth_scheme, error_label, payload, attempts=attempts)

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "Authorization": f"{auth_scheme} {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{error_label}: HTTP {exc.code} {body}") from exc


def get_json(
    url: str,
    api_key: str,
    timeout: int | None = 60,
    auth_scheme: str = "Key",
    error_label: str = "GPT Image-2 fal 状态查询失败",
) -> dict[str, Any]:
    if auth_scheme == "Bearer":
        # 状态查询的连续错误预算由上层 poller 统一管理，避免 10×10 的嵌套重试。
        return request_json("GET", url, api_key, timeout, auth_scheme, error_label, attempts=1)

    request = Request(
        url,
        headers={"Authorization": f"{auth_scheme} {api_key}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{error_label}: HTTP {exc.code} {body}") from exc


def request_json(
    method: str,
    url: str,
    api_key: str,
    timeout: int | None,
    auth_scheme: str,
    error_label: str,
    payload: dict[str, Any] | None = None,
    attempts: int = 11,
) -> dict[str, Any]:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests 导入失败，无法按 toapis 官方方式调用接口：请安装 requests") from exc

    try:
        response = requests_call_with_retries(
            requests.request,
            method,
            url,
            headers={"Authorization": f"{auth_scheme} {api_key}"},
            json=payload,
            timeout=timeout,
            attempts=attempts,
        )
    except requests.RequestException as exc:
        # 网络异常（连接重置/写超时等）→ CONN_RESET，图片路径可幂等重试。
        raise ImageProviderError(
            f"{error_label}: {exc}",
            error_code=classify_provider_error(message=str(exc)),
            response_received=False,
        ) from exc
    try:
        response_payload = response.json()
    except ValueError:
        response_payload = {"message": response.text}
    if not response.ok:
        message = response_payload.get("message") or response_payload.get("error") or response_payload
        if isinstance(message, dict):
            message = message.get("message") or str(message)
        provider_error = normalize_toapis_error(
            response_payload,
            status_code=response.status_code,
            fallback_message=str(message),
        )
        raise ImageProviderError(
            f"{error_label}: HTTP {response.status_code} {message}",
            provider_response={
                "httpStatus": response.status_code,
                "providerError": provider_error,
                "providerResponse": response_payload,
            },
            error_code=str(provider_error["code"]),
            response_received=True,
        )
    return response_payload


def requests_call_with_retries(call: Any, *args: Any, attempts: int = 11, **kwargs: Any) -> Any:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests 导入失败，无法调用外部图片服务：请安装 requests") from exc

    last_error: requests.RequestException | None = None
    for index in range(attempts):
        try:
            return call(*args, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            if index < attempts - 1:
                time.sleep(min(30 * (2 ** index), 240))
    if last_error:
        raise last_error
    raise RuntimeError("requests 调用失败")


def post_multipart_file(
    url: str,
    field_name: str,
    filename: str,
    content: bytes,
    content_type: str,
    api_key: str,
    timeout: int | None = 300,
    attempts: int = 1,
) -> dict[str, Any]:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests 导入失败，无法按 toapis 官方方式导入参考图：请安装 requests") from exc

    upload_name = os.path.basename(filename) or "image.png"
    file_label = f"{upload_name}, {len(content) / 1048576:.2f}MB"
    files = {field_name: (upload_name, content, content_type)}
    try:
        response = requests_call_with_retries(
            requests.post,
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            timeout=timeout,
            attempts=attempts,
        )
    except requests.RequestException as exc:
        raise GenerationPhaseError(
            f"toapis 图片导入失败({file_label}): {exc}",
            ERROR_UPLOAD_FAILED,
        ) from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text}
    if not response.ok:
        message = payload.get("message") or payload.get("error") or payload
        if isinstance(message, dict):
            message = message.get("message") or str(message)
        provider_error = normalize_toapis_error(
            payload,
            status_code=response.status_code,
            fallback_message=str(message),
        )
        raise GenerationPhaseError(
            f"toapis 图片导入失败({file_label}): HTTP {response.status_code} {message}",
            str(provider_error["code"]),
            provider_response={
                "httpStatus": response.status_code,
                "providerError": provider_error,
                "providerResponse": payload,
            },
        )
    return payload


def download_bytes(url: str, timeout: int = 300, attempts: int = 11) -> tuple[bytes, str | None]:
    if url.startswith(("http://", "https://")):
        try:
            import requests
        except ImportError:
            pass
        else:
            last_error: Exception | None = None
            for index in range(max(1, attempts)):
                try:
                    response = requests.get(url, timeout=timeout)
                    response.raise_for_status()
                    return response.content, response.headers.get("content-type")
                except requests.RequestException as exc:
                    last_error = exc
                    if index < attempts - 1:
                        time.sleep(min(30 * (2 ** index), 240))
            if last_error:
                raise last_error
    request = Request(url, method="GET")
    with urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers.get_content_type()


def add_query_param(url: str, key: str, value: str) -> str:
    separator = "&" if "?" in url else "?"
    if f"{key}=" in url:
        return url
    return f"{url}{separator}{key}={value}"


def map_fal_status(provider_status: str) -> str:
    if provider_status == "COMPLETED":
        return "SUCCEEDED"
    if provider_status in {"IN_QUEUE", "IN_PROGRESS"}:
        return provider_status
    if provider_status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
        return "FAILED" if provider_status not in {"CANCELED", "CANCELLED"} else "CANCELLED"
    return "SUBMITTED"


def map_toapis_status(provider_status: str) -> str:
    if provider_status == "completed":
        return "SUCCEEDED"
    if provider_status in {"queued", "submitted"}:
        return "SUBMITTED"
    if provider_status in {"in_progress", "processing", "running"}:
        return "IN_PROGRESS"
    if provider_status in {"failed", "error"}:
        return "FAILED"
    if provider_status in {"canceled", "cancelled"}:
        return "CANCELLED"
    return "SUBMITTED"


def extract_toapis_output_url(response: dict[str, Any]) -> str | None:
    if response.get("url"):
        return str(response["url"])
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    data = result.get("data") if isinstance(result.get("data"), list) else []
    if data and isinstance(data[0], dict) and data[0].get("url"):
        return str(data[0]["url"])
    return None


def toapis_url(path: str) -> str:
    base = get_settings().toapis_base_url.rstrip("/")
    return f"{base}{path if path.startswith('/') else '/' + path}"


def get_image_provider() -> ImageGenerationProvider:
    settings = get_settings()
    if settings.image_provider == "toapis":
        return GPTImage2ToapisProvider()
    if settings.image_provider == "fal":
        return GPTImage2FalProvider()
    raise RuntimeError(f"不支持的 Image-2 provider：{settings.image_provider}。支持 toapis 或 fal。")
