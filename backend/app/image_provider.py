from __future__ import annotations

from html import escape
import json
import mimetypes
import os
import tempfile
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from .config import get_settings
from .object_storage import get_bytes, object_exists, put_bytes


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


class GPTImage2PlaceholderProvider(ImageGenerationProvider):
    def output_suffix(self) -> str:
        return ".svg"

    async def generate(self, task_package: dict[str, Any], output_path: str) -> dict[str, Any]:
        settings = get_settings()
        if settings.gpt_image2_api_base_url and settings.fal_key:
            # The third-party GPT Image-2 API is intentionally not bound yet.
            # Keep the request shape stable so a real adapter can replace this class.
            provider_mode = "third-party-configured-placeholder"
        else:
            provider_mode = "placeholder"

        output_key = str(output_path)
        put_bytes(output_key, render_placeholder_svg(task_package).encode("utf-8"), "image/svg+xml")
        return {
            "provider": "gpt-image-2-placeholder",
            "mode": provider_mode,
            "outputPath": output_key,
            "providerRequest": build_fal_request_snapshot(task_package, []),
            "providerResponse": None,
            "note": "真实三方平台未确定，当前写入占位图并完整保留任务包与日志。",
        }


class GPTImage2FalProvider(ImageGenerationProvider):
    def output_suffix(self) -> str:
        return f".{get_settings().gpt_image2_output_format}"

    async def generate(self, task_package: dict[str, Any], output_path: str) -> dict[str, Any]:
        settings = get_settings()
        if not settings.fal_key:
            raise RuntimeError("FAL_KEY 未配置，无法调用 GPT Image-2 fal 接口")

        uploaded_inputs, image_urls = prepare_fal_image_inputs(task_package.get("imageInputs", []))

        provider_request = build_fal_request_snapshot(task_package, image_urls)
        raw_response = post_json(settings.gpt_image2_api_base_url, provider_request, settings.fal_key)
        images = raw_response.get("images") if isinstance(raw_response, dict) else None
        if not images:
            raise RuntimeError(f"GPT Image-2 fal 响应缺少 images: {raw_response}")

        output_url = images[0].get("url")
        if not output_url:
            raise RuntimeError(f"GPT Image-2 fal 响应缺少图片 URL: {raw_response}")

        output_key = str(output_path)
        content, content_type = download_bytes(output_url)
        put_bytes(output_key, content, content_type or f"image/{settings.gpt_image2_output_format}")
        return {
            "provider": "fal",
            "model": settings.gpt_image2_model_id,
            "outputPath": output_key,
            "imageInputs": uploaded_inputs,
            "providerRequest": provider_request,
            "providerResponse": raw_response,
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
            result.update({"providerResponse": response, "outputPath": output_key})
        elif next_status == "FAILED":
            result["error"] = status_response.get("error") or status_response.get("message") or "fal 生成失败"
        return result


def build_fal_request_snapshot(task_package: dict[str, Any], image_urls: list[str]) -> dict[str, Any]:
    settings = get_settings()
    return {
        "prompt": task_package.get("prompt") or build_chinese_prompt(task_package),
        "image_urls": image_urls,
        "image_size": "auto",
        "quality": settings.gpt_image2_quality,
        "num_images": 1,
        "output_format": settings.gpt_image2_output_format,
        "sync_mode": False,
    }


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


def is_fal_cdn_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host in {"fal.media", "v3.fal.media"} or host.endswith(".fal.media")


def upload_object_to_fal(object_key: str, filename: str) -> str:
    content = get_bytes(object_key)
    return upload_bytes_to_fal(content, filename)


def upload_url_to_fal(url: str, filename: str) -> str:
    content, content_type = download_bytes(url)
    return upload_bytes_to_fal(content, filename, content_type)


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
    with tempfile.NamedTemporaryFile(prefix="lily-fal-input-", suffix=suffix) as handle:
        handle.write(content)
        handle.flush()
        return fal_client.upload_file(handle.name)


def filename_suffix(filename: str, content_type: str | None = None) -> str:
    suffix = os.path.splitext(filename)[1]
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(content_type or "") if content_type else None
    return guessed or ".png"


def post_json(url: str, payload: dict[str, Any], api_key: str, timeout: int | None = 300) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GPT Image-2 fal 请求失败: HTTP {exc.code} {body}") from exc


def get_json(url: str, api_key: str, timeout: int | None = 60) -> dict[str, Any]:
    request = Request(
        url,
        headers={"Authorization": f"Key {api_key}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GPT Image-2 fal 状态查询失败: HTTP {exc.code} {body}") from exc


def download_bytes(url: str) -> tuple[bytes, str | None]:
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


def build_chinese_prompt(task_package: dict[str, Any]) -> str:
    shot = task_package.get("shot", {})
    garment = task_package.get("garment", {})
    model = task_package.get("model", {})
    brand_style = task_package.get("brandStyle", {})
    planning_script = task_package.get("planningScript", {}) if isinstance(task_package.get("planningScript"), dict) else {}
    generation = task_package.get("generation", {})
    stage = generation.get("stage", "final_group")
    constraints = generation.get("constraints", [])
    regeneration = task_package.get("regeneration")

    forbidden: list[str] = []
    styling = shot.get("styling") if isinstance(shot.get("styling"), dict) else {}
    for item in styling.get("forbidden", []):
        forbidden.append(str(item))
    model_profile = model.get("profile") if isinstance(model.get("profile"), dict) else {}
    for item in model_profile.get("forbidden", []):
        forbidden.append(str(item))

    if stage == "preview_grid":
        lines = [
            "任务：",
            "生成一张 Lily AI 模特九宫格姿势预览图。画面为 3x3 九宫格，一张图内包含 9 个小格。",
            "",
            "九宫格要求：",
            "- 9 个小格必须是同一位模特、同一件服装、同一套鞋包配饰、同一浅灰白棚拍背景。",
            "- 只改变姿势、身位、轻微动态和取景，不改变服装品类、颜色、结构、长度和比例。",
            "- 姿势需覆盖正面、轻斜正、3/4 斜正、侧身、轻走动、扶腰/插袋/自然垂手等 Lily 品牌常用方向。",
            "- 每格看起来像可用于最终成片的候选姿态，不要生成夸张写真、街拍大片或复杂布景。",
            "- 不要在图片中添加文字、编号、水印、logo 或说明。",
            "",
            "图片角色：",
            "- 服装参考图是商品事实来源，必须保持服装设计、颜色、面料、结构、长度和比例。",
            "- 模特头部图是身份事实来源，必须保持同一张脸、发型、刘海和克制表情。",
            "- 品牌调性定义姿态、鞋包、配饰、光线、背景和构图边界。",
            "",
            "服装与品牌：",
            f"- 货号 {task_package.get('sku', '')}：{garment.get('summary', '')}",
            f"- Lily 调性：{planning_script.get('lilyTone') or brand_style.get('summary', '浅灰白棚拍、干净通勤、轻熟克制、商品优先。')}",
            f"- 组图级搭配方案：{planning_script.get('stylingPlan', '')}",
            f"- 颜色基调：{planning_script.get('colorPalette', '')}",
            f"- 鞋包首饰：{planning_script.get('accessories', '')}",
        ]
    else:
        lines = [
        "任务：",
        f"生成一张电商 AI 模特图，当前图位为 {shot.get('shotIndex', '')}：{shot.get('title', '生成图')}。",
        "",
        "图片角色：",
        "- 服装参考图是商品事实来源，必须保持服装设计、颜色、面料、结构、长度和比例。",
        "- 模特头部图是身份事实来源，必须保持同一张脸、发型、刘海和克制表情。",
        "- 品牌真实成片参考图定义姿态、鞋包、配饰、光线、背景和构图边界。",
        "",
        "优先级：",
        f"1. 保持 {model.get('modelId', '当前模特')} 模特身份一致。",
        f"2. 保持货号 {task_package.get('sku', '')} 的服装事实：{garment.get('summary', '')}",
        f"3. 遵守 Lily 调性：{planning_script.get('lilyTone') or brand_style.get('summary', '白/浅灰棚拍，商品优先。')}",
        f"4. 服从组图级搭配方案：{planning_script.get('stylingPlan', '')}",
        f"5. 当前图位任务：{shot.get('slotName', shot.get('title', ''))}，{shot.get('task', '')}；统一要求：{shot.get('unifiedRequirement', '')}。",
        "",
        "拍摄剧本：",
        f"- 景别：{shot.get('scene') or shot.get('frame', '')}",
        f"- 取景范围：{shot.get('cropRange') or shot.get('crop', '')}",
        f"- 模特占画幅：{shot.get('modelScale', '')}",
        f"- 头部处理：{shot.get('headTreatment', '')}",
        f"- 身位：{shot.get('bodyPosition') or shot.get('angle', '')}",
        f"- 姿势调性：{shot.get('poseTone') or shot.get('pose', '自然、克制、符合电商棚拍。')}",
        f"- 画面目的：{shot.get('imagePurpose') or shot.get('focus', '')}",
        f"- 背景：{shot.get('background', '白/浅灰棚拍')}",
        f"- 颜色基调：{planning_script.get('colorPalette', '')}",
        f"- 鞋包首饰：{planning_script.get('accessories', '')}",
        ]
        if any(item.get("role") == "preview_grid" for item in task_package.get("imageInputs", [])):
            lines.extend(
                [
                    "",
                    "九宫格预览参考：",
                    "- 已提供一张九宫格预览图，请参考其中的姿势、身位、服装一致性和整体气质。",
                    "- 最终输出仍然只生成当前单张图位，不要输出九宫格。",
                ]
            )
    if constraints:
        lines.extend(["", "约束：", *[f"- {item}" for item in constraints]])
    if garment.get("proportionConstraints"):
        lines.extend(["", "商品比例约束：", f"- {garment.get('proportionConstraints')}"])
    if regeneration:
        lines.extend(
            [
                "",
                "重生成要求：",
                f"- 原因：{regeneration.get('reason', '')}",
                f"- 补充：{regeneration.get('note', '')}",
            ]
        )
    lines.extend(
        [
            "",
            "禁止：",
            "- 不要换脸、不要改变发型、不要改变服装结构、不要改变服装长度和比例、不要生成文字、水印或 logo。",
        ]
    )
    if forbidden:
        lines.append(f"- 不要出现：{'、'.join(dict.fromkeys(forbidden))}。")
    return "\n".join(str(line) for line in lines if line is not None)


def render_placeholder_svg(task_package: dict[str, Any]) -> str:
    shot = task_package.get("shot", {})
    garment = task_package.get("garment", {})
    title = escape(str(shot.get("title", "生成图")))
    frame = escape(str(shot.get("frame", "")))
    focus = escape(str(shot.get("focus", "")))
    category = escape(str(garment.get("category", "待识别品类")))
    summary = escape(str(garment.get("summary", "白/浅灰棚拍风格")))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1200" viewBox="0 0 1200 1200">
  <rect width="1200" height="1200" fill="#f7f7f5"/>
  <rect x="96" y="96" width="1008" height="1008" rx="8" fill="#ffffff" stroke="#d8d8d2" stroke-width="2"/>
  <rect x="300" y="180" width="600" height="760" rx="300" fill="#eeeeea"/>
  <path d="M600 250 C520 250 470 310 470 390 L470 850 C470 920 530 970 600 970 C670 970 730 920 730 850 L730 390 C730 310 680 250 600 250Z" fill="#d9d9d2"/>
  <circle cx="600" cy="240" r="82" fill="#cfcfc8"/>
  <text x="600" y="1020" text-anchor="middle" font-family="Arial, sans-serif" font-size="42" fill="#2c2c2c">{title}</text>
  <text x="600" y="1074" text-anchor="middle" font-family="Arial, sans-serif" font-size="28" fill="#666">{category} · {frame}</text>
  <text x="600" y="1120" text-anchor="middle" font-family="Arial, sans-serif" font-size="24" fill="#777">{focus}</text>
  <text x="600" y="1160" text-anchor="middle" font-family="Arial, sans-serif" font-size="20" fill="#999">{summary}</text>
</svg>
"""


def get_image_provider() -> ImageGenerationProvider:
    settings = get_settings()
    if settings.image_provider == "fal":
        return GPTImage2FalProvider()
    return GPTImage2PlaceholderProvider()
