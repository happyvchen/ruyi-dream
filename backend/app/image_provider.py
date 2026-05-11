from __future__ import annotations

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
    generation = task_package.get("generation", {}) if isinstance(task_package.get("generation"), dict) else {}
    stage = generation.get("stage")
    return {
        "prompt": task_package.get("prompt") or build_chinese_prompt(task_package),
        "image_urls": image_urls,
        "image_size": "square_hd" if stage == "preview_grid" else "auto",
        "quality": "high" if stage == "preview_grid" else settings.gpt_image2_quality,
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
        lines = build_preview_prompt_lines(task_package, garment, brand_style, planning_script)
    else:
        lines = build_final_prompt_lines(task_package, shot, model, brand_style, planning_script)
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


def build_preview_prompt_lines(
    task_package: dict[str, Any],
    garment: dict[str, Any],
    brand_style: dict[str, Any],
    planning_script: dict[str, Any],
) -> list[str]:
    image_inputs = task_package.get("imageInputs", [])
    model_count = sum(1 for item in image_inputs if item.get("role") == "model_identity")
    garment_count = sum(1 for item in image_inputs if item.get("role") == "garment_reference")
    styling_text = str(planning_script.get("styling") or "").strip()
    if not styling_text:
        raise RuntimeError("分镜预览缺少服饰搭配：请先在服装素材页生成或保存完整的生图方案")
    return [
        "【任务】：",
        "根据给定的模特身份参考图和服装素材参考图，生成一张 2x3 商业摄影分镜预览图（Contact Sheet）。",
        "",
        "这是一张视觉预览图，不是文字脚本。画面共 6 个格子：前 5 格严格对应最终组图的 5 个分镜，第 6 格为备用候选姿势。每个格子都应像真实电商棚拍成片候选画面。",
        "",
        "服装素材参考图是唯一商品事实来源。请直接依据服装素材图还原服装，不要根据文字自行改造、补全或简化服装。",
        "必须严格保持模特身份一致，包括面部特征、头型、发型、刘海、气质、自然表情和身体比例。",
        "",
        "不要在画面中生成任何文字、编号、标签、水印、logo、脚本说明或边框说明。",
        "",
        "【图片角色】：",
        build_image_role_line(model_count, garment_count),
        "如果模特参考图少于 4 张，以实际提供数量为准；服装素材图数量以实际提供数量为准。无论参考图数量如何，最终必须保持同一位模特和同一件服装。",
        "",
        "【灯光背景】：",
        f"{planning_script.get('background', '浅灰白棚拍背景、轻微落地阴影')}。整体呈现{planning_script.get('lilyTone') or brand_style.get('summary', '干净、理性、通勤、轻熟、克制的都市通勤风格')}。光线柔和均匀，商品结构清楚，背景不抢夺服装主体。",
        "",
        "【服饰搭配】：",
        styling_text,
        "",
        "【分镜要求】：",
        *build_storyboard_lines(task_package.get("previewPlan", {}).get("shots", [])),
        "",
        "【一致性要求】：",
        "6 个格子必须是同一位模特、同一发型、同一服装、同一服饰搭配、同一背景灯光。",
        "只允许改变镜头构图、取景范围、身位和姿势。",
        "不得改变服装结构、长度、比例和细节质感；服装具体事实以服装素材图为准。",
        "不得生成夸张写真、街拍大片、复杂布景、过度妆容、夸张表情或不符合电商棚拍的姿势。",
        "",
        "【技术参数】：",
        "商业级电商棚拍质感，哈苏中画幅相机效果，85mm 镜头，f/8 光圈。超清画质，皮肤纹理自然清晰，面料质感真实，商品边缘清楚，商业精修效果。",
    ]


def build_final_prompt_lines(
    task_package: dict[str, Any],
    shot: dict[str, Any],
    model: dict[str, Any],
    brand_style: dict[str, Any],
    planning_script: dict[str, Any],
) -> list[str]:
    image_inputs = task_package.get("imageInputs", [])
    role_lines = build_final_image_role_lines(image_inputs, int(shot.get("shotIndex") or 1))
    return [
        "任务：",
        f"生成一张商业级电商棚拍单图，当前图位为 {shot.get('shotIndex', '')}：{shot.get('title', '生成图')}。",
        "本次只生成这一张单图，不要生成九宫格、拼图、文字说明、水印、logo 或边框。",
        "",
        "图片角色：",
        *role_lines,
        "",
        "优先级：",
        "1. 服装素材参考图是最高优先级商品事实来源。",
        "2. 模特身份参考图是最高优先级人物身份来源。",
        "3. 九宫格预览图只决定导演分镜，不覆盖服装事实。",
        "4. 当前图位脚本决定景别、取景范围、姿势和身位。",
        "",
        "品牌与搭配：",
        f"- 背景光线：{planning_script.get('background', '白/浅灰棚拍，轻微落地阴影')}",
        f"- Lily 调性：{planning_script.get('lilyTone') or brand_style.get('summary', '干净、理性、通勤、轻熟、克制的都市通勤风格')}",
        f"- 服饰搭配：{planning_script.get('styling', '')}",
        "",
        "当前图要求：",
        f"- 任务：{shot.get('task', '')}",
        f"- 画面目的：{shot.get('imagePurpose') or shot.get('focus', '')}",
        f"- 景别：{shot.get('scene') or shot.get('frame', '')}",
        f"- 取景范围：{shot.get('cropRange') or shot.get('crop', '')}",
        f"- 模特占画幅：{shot.get('modelScale', '')}",
        f"- 头部处理：{shot.get('headTreatment', '')}",
        f"- 姿势身位：{shot.get('poseTone') or shot.get('pose', '自然、克制、符合电商棚拍。')}，身位为{shot.get('bodyPosition') or shot.get('angle', '')}",
        "",
        "服装保真：",
        "请直接依据服装素材参考图还原服装的品类、颜色、面料、版型、结构、比例、长度、门襟、扣位、口袋、刺绣、领型、袖口和细节质感。不要根据文字自行改造、补全或简化服装。",
        "",
        "技术参数：",
        "商业级电商棚拍质感，超清画质，皮肤纹理自然清晰，面料质感真实，商品边缘清楚，商业精修效果。",
    ]


def build_final_image_role_lines(image_inputs: list[dict[str, Any]], shot_index: int) -> list[str]:
    lines: list[str] = []
    preview_indexes = [index for index, item in enumerate(image_inputs, start=1) if item.get("role") == "preview_grid"]
    model_indexes = [index for index, item in enumerate(image_inputs, start=1) if item.get("role") == "model_identity"]
    garment_indexes = [index for index, item in enumerate(image_inputs, start=1) if item.get("role") == "garment_reference"]
    if preview_indexes:
        lines.append(
            f"- 图{format_index_range(preview_indexes)}是已确认的 2x3 分镜预览图，只用于参考第 {shot_index} 格的姿势、身位、景别、构图、背景光线和搭配方向；它不是服装商品事实来源。"
        )
    if model_indexes:
        lines.append(
            f"- 图{format_index_range(model_indexes)}是同一位数字模特身份参考图，用于锁定脸型、五官、头型、发型、刘海、气质和身体比例。"
        )
    if garment_indexes:
        lines.append(
            f"- 图{format_index_range(garment_indexes)}是同一服装货号的素材参考图，是唯一商品事实来源。"
        )
    return lines or ["- 未提供参考图。"]


def format_index_range(indexes: list[int]) -> str:
    if not indexes:
        return ""
    if len(indexes) == 1:
        return str(indexes[0])
    return f"{indexes[0]}~{indexes[-1]}"


def build_image_role_line(model_count: int, garment_count: int) -> str:
    if model_count > 0 and garment_count > 0:
        garment_start = model_count + 1
        garment_end = model_count + garment_count
        return f"图1~{model_count}是模特身份参考图，用于锁定同一位模特的脸型、五官、头型、发型、刘海、气质和自然表情。图{garment_start}~{garment_end}是同一服装货号的素材参考图，是商品事实来源，用于锁定服装原貌、颜色、面料、结构、比例和细节。"
    if model_count > 0:
        return f"图1~{model_count}是模特身份参考图，用于锁定同一位模特的脸型、五官、头型、发型、刘海、气质和自然表情。"
    if garment_count > 0:
        return f"图1~{garment_count}是同一服装货号的素材参考图，是商品事实来源，用于锁定服装原貌、颜色、面料、结构、比例和细节。"
    return "未提供参考图。"


def build_garment_fact_lines(garment: dict[str, Any], planning_script: dict[str, Any]) -> list[str]:
    lines = [
        f"- 品类：{garment.get('category') or planning_script.get('category') or ''}",
        f"- 摘要：{garment.get('summary', '')}",
        f"- 颜色：{garment.get('color', '')}",
        f"- 面料：{garment.get('fabric', '')}",
    ]
    key_structures = garment.get("keyStructures")
    if isinstance(key_structures, list) and key_structures:
        lines.append(f"- 关键结构：{'、'.join(str(item) for item in key_structures)}")
    preserve = garment.get("preserveStrictly")
    if isinstance(preserve, list) and preserve:
        lines.append(f"- 必须严格保留：{'、'.join(str(item) for item in preserve)}")
    sales_info = planning_script.get("salesInfo")
    if sales_info:
        lines.append(f"- 主销售信息：{sales_info}")
    lines.append("- 特别注意：扣子数量、扣位、扣眼、门襟开合、口袋/袋盖、绣标位置和形状、领型、肩线、后中开衩、衣长比例必须与服装素材图一致。")
    return [line for line in lines if line.strip() and not line.endswith("：")]


def build_storyboard_lines(shots: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for index, shot in enumerate(shots[:5], start=1):
        lines.extend(
            [
                f"镜头{chinese_ordinal(index)}：",
                f"任务：{shot.get('task', '')}",
                f"画面目的：{shot.get('imagePurpose', '')}",
                f"景别：{shot.get('scene', '')}",
                f"取景范围：{shot.get('cropRange', '')}",
                f"模特占画幅：{shot.get('modelScale', '')}",
                f"头部处理：{shot.get('headTreatment', '')}",
                f"姿势身位：{shot.get('poseTone', '')}，身位为{shot.get('bodyPosition', '')}",
                "",
            ]
        )
    lines.extend(
        [
            "镜头六：",
            "任务：备用候选姿势",
            "画面目的：在不改变服装、搭配、背景和模特身份的前提下，提供一个可替代的商业棚拍姿势",
            "景别：全身或近全身图",
            "取景范围：头顶到脚尖或小腿下方，商品主体完整",
            "模特占画幅：78%-88%",
            "头部处理：完整露头",
            "姿势身位：自然轻走动或轻斜正站姿，动作比前 5 个镜头略有变化，但仍保持 Lily 克制通勤调性",
        ]
    )
    return lines


def chinese_ordinal(value: int) -> str:
    return {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}.get(value, str(value))


def get_image_provider() -> ImageGenerationProvider:
    settings = get_settings()
    if settings.image_provider != "fal":
        raise RuntimeError(f"不支持的 Image-2 provider：{settings.image_provider}。生产实现只支持 fal。")
    return GPTImage2FalProvider()
