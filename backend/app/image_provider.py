from __future__ import annotations

import json
import mimetypes
import os
import tempfile
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import config
from .image_derivatives import generate_image_derivatives
from .object_storage import get_bytes, object_exists, put_bytes


def get_settings() -> config.Settings:
    return config.get_settings()


class ImageGenerationProvider:
    def output_suffix(self) -> str:
        return ".png"

    async def generate(self, task_package: dict[str, Any], output_path: str) -> dict[str, Any]:
        raise NotImplementedError

    async def submit(self, task_package: dict[str, Any], output_path: str) -> dict[str, Any]:
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

    async def refresh(self, image_row: Any) -> dict[str, Any]:
        return {"status": image_row["status"], "providerStatus": image_row["provider_status"] if "provider_status" in image_row.keys() else None}


class ImageProviderError(RuntimeError):
    def __init__(self, message: str, provider_response: dict[str, Any] | None = None):
        super().__init__(message)
        self.provider_response = provider_response or {}


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
            latest = await self.refresh(image_row)
            if latest.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                break
        if latest.get("status") != "SUCCEEDED":
            raise RuntimeError(f"fal 生成未成功: {latest}")
        return {
            **submit_response,
            **latest,
            "submissionMode": "completed",
        }

    async def submit(self, task_package: dict[str, Any], output_path: str) -> dict[str, Any]:
        settings = get_settings()
        if not settings.fal_key:
            raise RuntimeError("FAL_KEY 未配置，无法调用 GPT Image-2 fal 接口")

        uploaded_inputs, image_urls = prepare_fal_image_inputs(task_package.get("imageInputs", []))

        provider_request = build_fal_request_snapshot(task_package, image_urls)
        submit_response = post_json(settings.gpt_image2_queue_url, provider_request, settings.fal_key, timeout=60)
        request_id = submit_response.get("request_id") or submit_response.get("requestId")
        provider_urls = {
            "statusUrl": submit_response.get("status_url"),
            "responseUrl": submit_response.get("response_url"),
            "cancelUrl": submit_response.get("cancel_url"),
        }
        return {
            "provider": "fal",
            "model": settings.gpt_image2_model_id,
            "submissionMode": "queued",
            "status": "SUBMITTED",
            "providerStatus": "SUBMITTED",
            "providerRequestId": request_id,
            "providerUrls": provider_urls,
            "outputPath": str(output_path),
            "imageInputs": uploaded_inputs,
            "providerRequest": provider_request,
            "providerResponse": submit_response,
            "providerStatusResponse": submit_response,
        }

    async def refresh(self, image_row: Any) -> dict[str, Any]:
        settings = get_settings()
        provider_urls = json.loads(image_row["provider_urls_json"] or "{}")
        status_url = provider_urls.get("statusUrl")
        response_url = provider_urls.get("responseUrl")
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
        if next_status == "SUCCEEDED":
            if not response_url:
                result.update({"status": "FAILED", "providerStatus": "MISSING_RESPONSE_URL", "error": "缺少 fal response_url"})
                return result
            response = get_json(response_url, settings.fal_key, timeout=60)
            images = response.get("images") if isinstance(response, dict) else None
            output_url = images[0].get("url") if images else None
            if not output_url:
                result.update({"status": "FAILED", "error": f"fal response 缺少图片 URL: {response}", "providerResponse": response})
                return result
            output_key = str(image_row["output_path"])
            content, content_type = download_bytes(output_url)
            put_bytes(output_key, content, content_type or f"image/{settings.gpt_image2_output_format}")
            derivative = generate_image_derivatives(output_key, content)
            result.update({
                "providerResponse": response,
                "outputPath": output_key,
                "fileSize": len(content),
                "thumbPath": derivative.get("thumb"),
                "previewPath": derivative.get("preview"),
                "thumbFileSize": derivative.get("thumbSize"),
                "previewFileSize": derivative.get("previewSize"),
            })
        elif next_status == "FAILED":
            result["error"] = status_response.get("error") or status_response.get("message") or "fal 生成失败"
        return result


class GPTImage2ToapisProvider(ImageGenerationProvider):
    def output_suffix(self) -> str:
        return f".{get_settings().gpt_image2_output_format}"

    async def generate(self, task_package: dict[str, Any], output_path: str) -> dict[str, Any]:
        submit_response = await self.submit(task_package, output_path)
        task_id = submit_response.get("providerRequestId")
        if not task_id:
            raise RuntimeError(f"toapis 创建任务响应缺少 id: {submit_response}")

        deadline = time.time() + get_settings().generation_image_timeout_seconds
        latest: dict[str, Any] = submit_response
        while time.time() < deadline:
            time.sleep(3)
            try:
                latest = await self.refresh({"provider_request_id": task_id, "output_path": output_path})
            except Exception as exc:
                raise ImageProviderError(
                    f"toapis 状态查询失败，providerRequestId={task_id}: {exc}",
                    {**submit_response, "providerStatusResponse": latest},
                ) from exc
            if latest.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                break
        if latest.get("status") != "SUCCEEDED":
            raise ImageProviderError(f"toapis 生成未成功，providerRequestId={task_id}: {latest}", {**submit_response, **latest})
        return {
            **submit_response,
            **latest,
            "submissionMode": "completed",
        }

    async def submit(self, task_package: dict[str, Any], output_path: str) -> dict[str, Any]:
        settings = get_settings()
        if not settings.toapis_api_key:
            raise RuntimeError("TOAPIS_API_KEY 未配置，无法调用 GPT Image-2 toapis 接口")

        uploaded_inputs, image_urls = prepare_toapis_image_inputs(task_package.get("imageInputs", []))
        provider_request = build_toapis_request_snapshot(task_package, image_urls)
        submit_response = post_json(
            toapis_url("/v1/images/generations"),
            provider_request,
            settings.toapis_api_key,
            timeout=60,
            auth_scheme="Bearer",
            error_label="GPT Image-2 toapis 请求失败",
        )
        task_id = submit_response.get("id") or submit_response.get("task_id")
        return {
            "provider": "toapis",
            "model": provider_request.get("model"),
            "submissionMode": "queued",
            "status": "SUBMITTED",
            "providerStatus": submit_response.get("status", "submitted"),
            "providerRequestId": task_id,
            "providerUrls": {"statusUrl": toapis_url(f"/v1/images/generations/{task_id}") if task_id else None},
            "outputPath": str(output_path),
            "imageInputs": uploaded_inputs,
            "providerRequest": provider_request,
            "providerResponse": submit_response,
            "providerStatusResponse": submit_response,
        }

    async def refresh(self, image_row: Any) -> dict[str, Any]:
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
        if next_status == "SUCCEEDED":
            output_url = extract_toapis_output_url(status_response)
            if not output_url:
                result.update({"status": "FAILED", "error": f"toapis response 缺少图片 URL: {status_response}", "providerResponse": status_response})
                return result
            output_key = str(image_row["output_path"])
            try:
                content, content_type = download_bytes(output_url)
                put_bytes(output_key, content, content_type or f"image/{settings.gpt_image2_output_format}")
                derivative = generate_image_derivatives(output_key, content)
            except Exception as exc:
                result.update({"status": "FAILED", "error": f"toapis 结果图下载失败: {exc}", "providerResponse": status_response})
                return result
            result.update({
                "providerResponse": status_response,
                "outputPath": output_key,
                "fileSize": len(content),
                "thumbPath": derivative.get("thumb"),
                "previewPath": derivative.get("preview"),
                "thumbFileSize": derivative.get("thumbSize"),
                "previewFileSize": derivative.get("previewSize"),
            })
        elif next_status == "FAILED":
            error = status_response.get("error") if isinstance(status_response.get("error"), dict) else {}
            result["error"] = error.get("message") or status_response.get("fail_reason") or status_response.get("message") or "toapis 生成失败"
        return result


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
        "image_size": "square_hd" if stage == "preview_grid" else "auto",
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
        "size": "1:1" if stage == "preview_grid" else settings.gpt_image2_size,
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


def upload_image_inputs(image_inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    uploaded: list[dict[str, Any]] = []
    for item in image_inputs:
        object_key = item.get("objectKey")
        source_url = item.get("providerUrl") or item.get("url")
        next_item = dict(item)
        if source_url and is_fal_cdn_url(str(source_url)):
            next_item["providerUrl"] = source_url
            uploaded.append(next_item)
            continue
        if object_key and object_exists(str(object_key)):
            next_item["sourceObjectKey"] = str(object_key)
            next_item["providerUrl"] = upload_object_to_fal(str(object_key), str(item.get("filename") or object_key))
            uploaded.append(next_item)
            continue
        if source_url and str(source_url).startswith(("http://", "https://")):
            next_item["sourceUrl"] = source_url
            next_item["providerUrl"] = upload_url_to_fal(str(source_url), str(item.get("filename") or "reference-image"))
            uploaded.append(next_item)
            continue
        uploaded.append(next_item)
    return uploaded


def prepare_fal_image_inputs(image_inputs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    uploaded_inputs = upload_image_inputs(image_inputs)
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


def prepare_toapis_image_inputs(image_inputs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    uploaded_inputs = upload_toapis_image_inputs(image_inputs)
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


def upload_toapis_image_inputs(image_inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    uploaded: list[dict[str, Any]] = []
    for item in image_inputs:
        object_key = item.get("objectKey")
        source_url = item.get("providerUrl") or item.get("url")
        next_item = dict(item)
        if source_url and is_toapis_file_url(str(source_url)):
            next_item["providerUrl"] = source_url
            uploaded.append(next_item)
            continue
        if object_key and object_exists(str(object_key)):
            next_item["sourceObjectKey"] = str(object_key)
            next_item["providerUrl"] = upload_object_to_toapis(str(object_key), str(item.get("filename") or object_key))
            uploaded.append(next_item)
            continue
        if source_url and str(source_url).startswith(("http://", "https://")):
            next_item["sourceUrl"] = source_url
            next_item["providerUrl"] = upload_url_to_toapis(str(source_url), str(item.get("filename") or "reference-image"))
            uploaded.append(next_item)
            continue
        uploaded.append(next_item)
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


def upload_bytes_to_toapis(content: bytes, filename: str, content_type: str | None = None) -> str:
    settings = get_settings()
    if not settings.toapis_api_key:
        raise RuntimeError("TOAPIS_API_KEY 未配置，无法上传参考图到 toapis")
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
        raise RuntimeError(f"{error_label}: {exc}") from exc
    try:
        response_payload = response.json()
    except ValueError:
        response_payload = {"message": response.text}
    if not response.ok:
        message = response_payload.get("message") or response_payload.get("error") or response_payload
        if isinstance(message, dict):
            message = message.get("message") or str(message)
        raise RuntimeError(f"{error_label}: HTTP {response.status_code} {message}")
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

    files = {field_name: (os.path.basename(filename) or "image.png", content, content_type)}
    try:
        response = requests_call_with_retries(
            requests.post,
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"toapis 图片上传失败: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text}
    if not response.ok:
        message = payload.get("message") or payload.get("error") or payload
        if isinstance(message, dict):
            message = message.get("message") or str(message)
        raise RuntimeError(f"toapis 图片上传失败: HTTP {response.status_code} {message}")
    return payload


def download_bytes(url: str) -> tuple[bytes, str | None]:
    if url.startswith(("http://", "https://")):
        try:
            import requests
        except ImportError:
            pass
        else:
            last_error: Exception | None = None
            for _ in range(3):
                try:
                    response = requests_call_with_retries(requests.get, url, timeout=300)
                    response.raise_for_status()
                    return response.content, response.headers.get("content-type")
                except requests.RequestException as exc:
                    last_error = exc
                    time.sleep(1)
            if last_error:
                raise last_error
    request = Request(url, method="GET")
    with urlopen(request, timeout=300) as response:
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
