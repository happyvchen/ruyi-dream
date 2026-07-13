from __future__ import annotations

import json
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
    ERROR_SUBMISSION_UNKNOWN,
    ERROR_SYSTEM_ERROR,
    ERROR_UPLOAD_FAILED,
    GenerationPhaseError,
    classify_provider_error,
    is_retryable_error,
)
from .image_derivatives import generate_image_derivatives
from .image_ingestion import normalized_image_candidates
from .object_storage import get_bytes, object_exists, put_bytes

# 上传进度回调：on_progress(completed, total)
UploadProgressCallback = Callable[[int, int], None]


def normalized_or_original_key(object_key: str) -> str:
    return next((key for key in normalized_image_candidates(object_key) if object_exists(key)), object_key)


def get_settings() -> config.Settings:
    return config.get_settings()


class ImageGenerationProvider:
    """图片生成 provider 的步骤化接口。

    生命周期被拆成三个独立步骤，失败语义各不相同：

    - ``submit``：上传参考图并创建 provider 任务（失败=UPLOAD/PROVIDER 问题）。
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
    ) -> dict[str, Any]:
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

    def __init__(self, message: str):
        super().__init__(message, error_code=ERROR_SUBMISSION_UNKNOWN, response_received=False)


def post_generation_task_with_confirmed_failure_retries(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    *,
    auth_scheme: str,
    error_label: str,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """创建 provider 任务。

    仅在 provider 明确返回可重试 HTTP 错误时重试；连接异常/读超时的执行
    结果未知，必须停止，避免创建重复计费任务。
    """
    for index in range(max_attempts):
        try:
            return post_json(
                url,
                payload,
                api_key,
                timeout=60,
                auth_scheme=auth_scheme,
                error_label=error_label,
                attempts=1,
            )
        except ImageProviderError as exc:
            if exc.response_received is not True:
                raise ProviderSubmissionUnknownError(f"{error_label}，提交结果未知: {exc}") from exc
            if not is_retryable_error(exc.error_code) or index >= max_attempts - 1:
                raise
            time.sleep(1 + index)
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
    ) -> dict[str, Any]:
        settings = get_settings()
        if not settings.fal_key:
            raise RuntimeError("FAL_KEY 未配置，无法调用 GPT Image-2 fal 接口")

        uploaded_inputs, image_urls = prepare_fal_image_inputs(task_package.get("imageInputs", []), on_progress)

        provider_request = build_fal_request_snapshot(task_package, image_urls)
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
            "model": settings.gpt_image2_model_id,
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
    ) -> dict[str, Any]:
        settings = get_settings()
        if not settings.toapis_api_key:
            raise RuntimeError("TOAPIS_API_KEY 未配置，无法调用 GPT Image-2 toapis 接口")

        uploaded_inputs, image_urls = prepare_toapis_image_inputs(task_package.get("imageInputs", []), on_progress)
        provider_request = build_toapis_request_snapshot(task_package, image_urls)
        submit_response = post_generation_task_with_confirmed_failure_retries(
            toapis_url("/v1/images/generations"),
            provider_request,
            settings.toapis_api_key,
            auth_scheme="Bearer",
            error_label="GPT Image-2 toapis 请求失败",
        )
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
            "providerRequest": provider_request,
            "providerResponse": submit_response,
            "providerStatusResponse": submit_response,
        }

    async def check(self, image_row: Any) -> dict[str, Any]:
        settings = get_settings()
        task_id = image_row["provider_request_id"]
        if not task_id:
            return {"status": "FAILED", "providerStatus": "MISSING_TASK_ID", "error": "缺少 toapis task id"}

        status_response = get_json(
            toapis_url(f"/v1/images/generations/{task_id}"),
            settings.toapis_api_key,
            timeout=60,
            auth_scheme="Bearer",
            error_label="GPT Image-2 toapis 状态查询失败",
        )
        provider_status = str(status_response.get("status") or "").lower()
        next_status = map_toapis_status(provider_status)
        result: dict[str, Any] = {
            "status": next_status,
            "providerStatus": provider_status,
            "providerRequestId": task_id,
            "providerStatusResponse": status_response,
        }
        if next_status == "FAILED":
            error = status_response.get("error") if isinstance(status_response.get("error"), dict) else {}
            result["error"] = error.get("message") or status_response.get("fail_reason") or status_response.get("message") or "toapis 生成失败"
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


# -- provider 参考图上传缓存 -------------------------------------------------
#
# 同一 objectKey 的参考图（主推款、搭配服装、模特参考图、分镜预览）在 5 张成片
# 并发生成时会被重复上传 5 次。缓存 + 进程内按 key 加锁：第一个上传，其余复用。

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
    image_inputs = task_package.get("imageInputs") if isinstance(task_package.get("imageInputs"), list) else []
    return {
        "model": settings.gpt_image2_model_id,
        "prompt": prompt,
        "reference_images": image_urls,
        "reference_image_manifest": build_provider_reference_image_manifest(image_inputs, image_urls),
        "n": 1,
        "size": settings.gpt_image2_preview_size if stage == "preview_grid" else settings.gpt_image2_size,
        "resolution": settings.gpt_image2_resolution,
        "response_format": "url",
    }


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
                "url": url,
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
            f"参考图上传超出总预算（{get_settings().generation_upload_timeout_seconds}s），未完成：{label}",
            ERROR_UPLOAD_FAILED,
        )


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
        raise RuntimeError(f"参考图上传到 fal 失败，以下输入没有 fal Image URL：{', '.join(missing_roles)}")

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
    uploaded_inputs = upload_toapis_image_inputs(image_inputs, on_progress)
    missing_roles = [
        str(item.get("role") or item.get("filename") or index)
        for index, item in enumerate(uploaded_inputs, start=1)
        if not item.get("providerUrl")
    ]
    if missing_roles:
        raise RuntimeError(f"参考图上传到 toapis 失败，以下输入没有 toapis Image URL：{', '.join(missing_roles)}")
    image_urls = [str(item["providerUrl"]) for item in uploaded_inputs]
    if not image_urls:
        raise RuntimeError("没有可传给 GPT Image-2 的 reference_images")
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
        if source_url and is_toapis_file_url(str(source_url)):
            next_item["providerUrl"] = source_url
            uploaded.append(next_item)
            continue
        if object_key and object_exists(str(object_key)):
            source_key = normalized_or_original_key(str(object_key))
            next_item["sourceObjectKey"] = source_key
            next_item["providerUrl"] = upload_object_with_cache(
                "toapis",
                source_key,
                lambda key=source_key: upload_object_to_toapis(key, os.path.basename(key)),
            )
            uploaded.append(next_item)
            continue
        if source_url and str(source_url).startswith(("http://", "https://")):
            next_item["sourceUrl"] = source_url
            next_item["providerUrl"] = upload_url_to_toapis(str(source_url), str(item.get("filename") or "reference-image"))
            uploaded.append(next_item)
            continue
        uploaded.append(next_item)
    if on_progress:
        on_progress(total, total)
    return uploaded


def is_fal_cdn_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host in {"fal.media", "v3.fal.media"} or host.endswith(".fal.media")


def is_toapis_file_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "files.toapis.com" or host.endswith(".toapis.com")


def upload_object_to_fal(object_key: str, filename: str) -> str:
    content = get_bytes(object_key)
    return upload_bytes_to_fal(content, filename)


def upload_url_to_fal(url: str, filename: str) -> str:
    content, content_type = download_bytes(url)
    return upload_bytes_to_fal(content, filename, content_type)


def upload_object_to_toapis(object_key: str, filename: str) -> str:
    content = get_bytes(object_key)
    return upload_bytes_to_toapis(content, filename)


def upload_url_to_toapis(url: str, filename: str) -> str:
    content, content_type = download_bytes(url)
    return upload_bytes_to_toapis(content, filename, content_type)


def upload_bytes_to_fal(content: bytes, filename: str, content_type: str | None = None) -> str:
    settings = get_settings()
    if not settings.fal_key:
        raise RuntimeError("FAL_KEY 未配置，无法上传参考图到 fal")
    try:
        import fal_client
    except ImportError as exc:
        raise RuntimeError("fal-client 导入失败，无法上传参考图到 fal：请安装 fal-client") from exc

    os.environ["FAL_KEY"] = settings.fal_key
    suffix = filename_suffix(filename, content_type)
    with tempfile.NamedTemporaryFile(prefix="ruyi-dream-fal-input-", suffix=suffix) as handle:
        handle.write(content)
        handle.flush()
        return fal_client.upload_file(handle.name)


# toapis 单文件上传上限 10MB；留出安全余量，只压超限大图，小图原样上传。
TOAPIS_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
TOAPIS_TARGET_UPLOAD_BYTES = TOAPIS_MAX_UPLOAD_BYTES - 512 * 1024
FIT_QUALITY_STEPS = (95, 92, 88, 85)
FIT_SCALE_STEP = 0.85
FIT_MIN_EDGE = 768


def fit_image_bytes_under_limit(
    content: bytes,
    filename: str,
    content_type: str | None,
    target_bytes: int = TOAPIS_TARGET_UPLOAD_BYTES,
) -> tuple[bytes, str, str | None]:
    """超过 target_bytes 的图片压到刚好过线为止；未超限的原样返回。

    压缩策略：优先保留原分辨率、只降编码质量；仍超限才逐档缩小边长。
    目标是"压得越少越好"，保证参考图清晰度，避免把大图压得远小于其他输入图。
    """
    if len(content) <= target_bytes:
        return content, filename, content_type
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return content, filename, content_type
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

            best: bytes | None = None
            scale = 1.0
            while True:
                if scale >= 1.0:
                    resized = image
                else:
                    width = max(1, round(image.width * scale))
                    height = max(1, round(image.height * scale))
                    resized = image.resize((width, height), Image.Resampling.LANCZOS)
                for quality in FIT_QUALITY_STEPS:
                    output = BytesIO()
                    resized.save(output, "JPEG", quality=quality, optimize=True)
                    best = output.getvalue()
                    if len(best) <= target_bytes:
                        stem = os.path.splitext(filename)[0] or "reference-image"
                        return best, f"{stem}.jpg", "image/jpeg"
                if min(resized.width, resized.height) <= FIT_MIN_EDGE:
                    break
                scale *= FIT_SCALE_STEP
    except OSError:
        return content, filename, content_type
    if best is not None:
        stem = os.path.splitext(filename)[0] or "reference-image"
        return best, f"{stem}.jpg", "image/jpeg"
    return content, filename, content_type


def upload_bytes_to_toapis(content: bytes, filename: str, content_type: str | None = None) -> str:
    settings = get_settings()
    if not settings.toapis_api_key:
        raise RuntimeError("TOAPIS_API_KEY 未配置，无法上传参考图到 toapis")
    content, filename, content_type = fit_image_bytes_under_limit(content, filename, content_type)
    upload_response = post_multipart_file(
        toapis_url("/v1/uploads/images"),
        "file",
        filename,
        content,
        content_type or mimetypes.guess_type(filename)[0] or "image/png",
        settings.toapis_api_key,
    )
    data = upload_response.get("data") if isinstance(upload_response, dict) else None
    uploaded_url = data.get("url") if isinstance(data, dict) else None
    if not uploaded_url:
        raise RuntimeError(f"toapis 上传图片响应缺少 URL: {upload_response}")
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
    attempts: int = 3,
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
        return request_json("GET", url, api_key, timeout, auth_scheme, error_label)

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
    attempts: int = 3,
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
        # HTTP 错误按状态码 + 文案分类：401/403→AUTH_FAILED、402/余额→INSUFFICIENT_BALANCE、
        # 1010→WAF_BLOCK、429→RATE_LIMITED、5xx→GATEWAY_5XX。上层据此早停或退避。
        error_code = classify_provider_error(status_code=response.status_code, body=str(message))
        raise ImageProviderError(
            f"{error_label}: HTTP {response.status_code} {message}",
            error_code=error_code,
            response_received=True,
        )
    return response_payload


def requests_call_with_retries(call: Any, *args: Any, attempts: int = 3, **kwargs: Any) -> Any:
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
                time.sleep(1 + index)
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
) -> dict[str, Any]:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests 导入失败，无法按 toapis 官方方式上传参考图：请安装 requests") from exc

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
        )
    except requests.RequestException as exc:
        raise GenerationPhaseError(
            f"toapis 图片上传失败({file_label}): {exc}",
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
        raise GenerationPhaseError(
            f"toapis 图片上传失败({file_label}): HTTP {response.status_code} {message}",
            ERROR_UPLOAD_FAILED,
        )
    return payload


def download_bytes(url: str, timeout: int = 300, attempts: int = 3) -> tuple[bytes, str | None]:
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
                        time.sleep(1 + index)
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
