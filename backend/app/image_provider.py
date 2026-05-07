from __future__ import annotations

from html import escape
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import get_settings


class ImageGenerationProvider:
    def output_suffix(self) -> str:
        return ".png"

    async def generate(self, task_package: dict[str, Any], output_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    async def submit(self, task_package: dict[str, Any], output_path: Path) -> dict[str, Any]:
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

    async def generate(self, task_package: dict[str, Any], output_path: Path) -> dict[str, Any]:
        settings = get_settings()
        if settings.gpt_image2_api_base_url and settings.fal_key:
            # The third-party GPT Image-2 API is intentionally not bound yet.
            # Keep the request shape stable so a real adapter can replace this class.
            provider_mode = "third-party-configured-placeholder"
        else:
            provider_mode = "local-placeholder"

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render_placeholder_svg(task_package), encoding="utf-8")
        return {
            "provider": "gpt-image-2-placeholder",
            "mode": provider_mode,
            "outputPath": str(output_path),
            "providerRequest": build_fal_request_snapshot(task_package, []),
            "providerResponse": None,
            "note": "真实三方平台未确定，当前写入占位图并完整保留任务包与日志。",
        }


class GPTImage2FalProvider(ImageGenerationProvider):
    def output_suffix(self) -> str:
        return f".{get_settings().gpt_image2_output_format}"

    async def generate(self, task_package: dict[str, Any], output_path: Path) -> dict[str, Any]:
        settings = get_settings()
        if not settings.fal_key:
            raise RuntimeError("FAL_KEY 未配置，无法调用 GPT Image-2 fal 接口")

        uploaded_inputs = upload_image_inputs(task_package.get("imageInputs", []))
        image_urls = [item["providerUrl"] for item in uploaded_inputs if item.get("providerUrl")]
        if not image_urls:
            raise RuntimeError("没有可传给 GPT Image-2 的 image_urls")

        provider_request = build_fal_request_snapshot(task_package, image_urls)
        raw_response = post_json(settings.gpt_image2_api_base_url, provider_request, settings.fal_key)
        images = raw_response.get("images") if isinstance(raw_response, dict) else None
        if not images:
            raise RuntimeError(f"GPT Image-2 fal 响应缺少 images: {raw_response}")

        output_url = images[0].get("url")
        if not output_url:
            raise RuntimeError(f"GPT Image-2 fal 响应缺少图片 URL: {raw_response}")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        download_file(output_url, output_path)
        return {
            "provider": "fal",
            "model": settings.gpt_image2_model_id,
            "outputPath": str(output_path),
            "imageInputs": uploaded_inputs,
            "providerRequest": provider_request,
            "providerResponse": raw_response,
        }

    async def submit(self, task_package: dict[str, Any], output_path: Path) -> dict[str, Any]:
        settings = get_settings()
        if not settings.fal_key:
            raise RuntimeError("FAL_KEY 未配置，无法调用 GPT Image-2 fal 接口")

        uploaded_inputs = upload_image_inputs(task_package.get("imageInputs", []))
        image_urls = [item["providerUrl"] for item in uploaded_inputs if item.get("providerUrl")]
        if not image_urls:
            raise RuntimeError("没有可传给 GPT Image-2 的 image_urls")

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
            output_path = Path(str(image_row["output_path"]))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            download_file(output_url, output_path)
            result.update({"providerResponse": response, "outputPath": str(output_path)})
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
    try:
        import fal_client
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(f"fal-client 未安装，无法上传本地图片: {exc}") from exc

    settings = get_settings()
    os.environ.setdefault("FAL_KEY", settings.fal_key)
    uploaded: list[dict[str, Any]] = []
    for item in image_inputs:
        local_path = item.get("localPath")
        source_url = item.get("providerUrl") or item.get("url")
        next_item = dict(item)
        if source_url and str(source_url).startswith(("http://", "https://")):
            next_item["providerUrl"] = source_url
            uploaded.append(next_item)
            continue
        if not local_path:
            uploaded.append(next_item)
            continue
        path = Path(str(local_path))
        if not path.exists():
            raise RuntimeError(f"图片输入不存在: {path}")
        next_item["providerUrl"] = fal_client.upload_file(str(path))
        uploaded.append(next_item)
    return uploaded


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


def download_file(url: str, output_path: Path) -> None:
    request = Request(url, method="GET")
    with urlopen(request, timeout=300) as response:
        output_path.write_bytes(response.read())


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
    generation = task_package.get("generation", {})
    constraints = generation.get("constraints", [])
    regeneration = task_package.get("regeneration")

    forbidden: list[str] = []
    styling = shot.get("styling") if isinstance(shot.get("styling"), dict) else {}
    for item in styling.get("forbidden", []):
        forbidden.append(str(item))
    model_profile = model.get("profile") if isinstance(model.get("profile"), dict) else {}
    for item in model_profile.get("forbidden", []):
        forbidden.append(str(item))

    lines = [
        "任务：",
        f"生成一张 Lily 电商模特图，当前图位为 {shot.get('shotIndex', '')}：{shot.get('title', '生成图')}。",
        "",
        "图片角色：",
        "- 服装参考图是商品事实来源，必须保持服装设计、颜色、面料、结构、长度和比例。",
        "- 模特头部图是身份事实来源，必须保持同一张脸、发型、刘海和克制表情。",
        "- Lily 真实成片参考图定义姿态、鞋包、配饰、光线、背景和构图边界。",
        "",
        "优先级：",
        f"1. 保持 {model.get('modelId', '当前模特')} 模特身份一致。",
        f"2. 保持货号 {task_package.get('sku', '')} 的服装事实：{garment.get('summary', '')}",
        f"3. 遵守品牌风格：{brand_style.get('summary', 'Lily 白/浅灰棚拍，商品优先。')}",
        f"4. 当前图要求：{shot.get('frame', '')}，{shot.get('angle', '')}，重点展示{shot.get('focus', '服装')}。",
        "",
        "姿态与造型：",
        f"- 姿态：{shot.get('pose', '自然、克制、符合 Lily 电商棚拍。')}",
        f"- 背景：{shot.get('background', '白/浅灰棚拍')}",
        f"- 鞋包配饰：{styling.get('shoes', '')}；{styling.get('bag', '')}；{styling.get('jewelry', '')}",
    ]
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
    summary = escape(str(garment.get("summary", "Lily 白/浅灰棚拍风格")))
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
