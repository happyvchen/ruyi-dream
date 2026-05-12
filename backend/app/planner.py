from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import get_settings

BRAND_TONE_PATH = Path(__file__).resolve().parents[1] / "ref" / "Lily拍摄与AI生图统一规则.md"
CATEGORIES = {"上衣", "裤装", "半裙", "短裤", "连衣裙", "外套", "套装"}

SHOT_SLOTS = [
    {
        "shotIndex": 1,
        "title": "全身主视觉",
        "englishName": "Full-length hero shot",
        "task": "先给完整穿着比例；全身或近全身，头脚完整，商品无遮挡",
    },
    {
        "shotIndex": 2,
        "title": "品类核心展示",
        "englishName": "Category key view",
        "task": "按品类突出销售主体；上衣看上身，裤裙看腰腿，外套看肩线门襟",
    },
    {
        "shotIndex": 3,
        "title": "设计细节近景",
        "englishName": "Detail close-up",
        "task": "解释面料、结构、工艺、设计点；可切头/不露脸，但设计点必须完整",
    },
    {
        "shotIndex": 4,
        "title": "身位/动态补充",
        "englishName": "Angle or movement view",
        "task": "用斜正、侧身、轻走动、背面补充版型；必须补充商品信息，不只是换动作",
    },
    {
        "shotIndex": 5,
        "title": "标准正面颜色图",
        "englishName": "Standard front color view",
        "task": "稳定、清楚、平台友好；正面或轻斜正，颜色和版型准确",
    },
]

class PlannerError(RuntimeError):
    def __init__(
        self,
        message: str,
        provider_request: dict[str, Any] | None = None,
        provider_response: Any = None,
    ) -> None:
        super().__init__(message)
        self.provider_request = provider_request
        self.provider_response = provider_response


class GarmentDraft(BaseModel):
    model_config = ConfigDict(extra="allow")

    category: str = ""
    summary: str = ""
    color: str = ""
    fabric: str = ""
    keyStructures: list[str] = Field(default_factory=list)
    preserveStrictly: list[str] = Field(default_factory=list)


class BrandStyleDraft(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = "默认棚拍风格"
    summary: str = "白/浅灰背景，自然姿态，简洁搭配，商品优先"


class GenerationPlanDraft(BaseModel):
    model_config = ConfigDict(extra="allow")

    salesInfo: str = ""
    lilyTone: str = ""
    styling: str = ""
    background: str = ""


class ShotDraft(BaseModel):
    model_config = ConfigDict(extra="allow")

    shotIndex: int | None = None
    englishName: str | None = None
    title: str | None = None
    task: str | None = None
    scene: str | None = None
    cropRange: str | None = None
    modelScale: str | None = None
    headTreatment: str | None = None
    bodyPosition: str | None = None
    poseTone: str | None = None
    imagePurpose: str | None = None


class ShotPlanDraft(BaseModel):
    model_config = ConfigDict(extra="allow")

    sku: str = ""
    modelId: str = "Face-01"
    category: str = ""
    garment: GarmentDraft = Field(default_factory=GarmentDraft)
    brandStyle: BrandStyleDraft = Field(default_factory=BrandStyleDraft)
    generationPlan: GenerationPlanDraft = Field(default_factory=GenerationPlanDraft)
    shots: list[ShotDraft] = Field(default_factory=list)


def default_pose(angle: str, category: str) -> str:
    if "背面" in angle:
        return "背面自然站姿，肩颈放松，必要时轻微回头"
    if "侧" in angle or "3/4" in angle:
        return "身体微侧，重心轻微偏移，手臂自然下垂，带轻松走动感"
    if category in {"裤子"}:
        return "正面自然站姿，腿部错开，突出裤型比例"
    return "正面自然站姿，重心不僵硬，手臂放松，克制商业表情"


def build_planner_prompt(sku: str) -> str:
    brand_tone = load_brand_tone()
    return f"""
你是 Lily 电商棚拍导演。请根据用户上传的服装主图和参考图，严格以《Lily 服装模特棚拍导演规范》为唯一规则来源，输出一份可执行的“生图方案”和“分镜剧本”。

【品牌调性】
{brand_tone}

要求：
- 用户不写 prompt，所以输出必须是结构化、可被人工修改的组图方案。
- 必须严格使用规范中的第 3 节固定 5 图顺序、第 4 节景别/取景/头部/身位、第 5 节品类导演规则、第 6 节搭配规范、第 8 节输出格式。
- 不允许自行新增不在规范中的图位、字段、风格方向或创意场景。
- 先判断品类，再确定“组图级搭配方案”，最后按 5 个固定图位分配景别、取景范围、占比、头部处理、身位、姿势调性和画面目的。
- 搭配是整组统一方案，5 张图不能每张换完全不同风格；将原“组图搭配方案、颜色基调、鞋包首饰”合并写入 generationPlan.styling。
- background 是整组统一生图方案，不要放入 shot。
- 不输出 evidenceLevel、slotName、unifiedRequirement、headReference、frame、crop、angle、pose、focus 等兼容或冗余字段。
- 优先服装保真：不改变颜色、结构、领型、袖长、裤长、裙长、口袋、纽扣等。
- 模特使用 Face-01，短发、自然淡妆、克制通勤气质。
- 根据品牌调性选择鞋履/包/配饰，避免老气尖头职业鞋、夸张高跟鞋、运动鞋、厚底鞋。
- 默认输出 5 张。

请只返回 JSON，不要 Markdown。JSON schema：
{{
  "sku": "{sku}",
  "modelId": "Face-01",
  "category": "上衣 | 裤装 | 半裙 | 短裤 | 连衣裙 | 外套 | 套装",
  "garment": {{
    "category": "上衣 | 裤装 | 半裙 | 短裤 | 连衣裙 | 外套 | 套装",
    "summary": "一句话服装摘要",
    "color": "主颜色",
    "fabric": "面料视觉特征",
    "keyStructures": ["关键结构"],
    "preserveStrictly": ["必须保真的点"]
  }},
  "brandStyle": {{
    "name": "默认棚拍风格",
    "summary": "白/浅灰背景，自然姿态，简洁搭配，商品优先"
  }},
  "generationPlan": {{
    "salesInfo": "主销售信息，说明该品类要卖什么",
    "lilyTone": "Lily 调性提炼",
    "styling": "服饰搭配。必须合并整组搭配、颜色基调、鞋履、包、首饰和禁忌，且 5 张统一",
    "background": "浅灰白棚拍背景、轻微落地阴影"
  }},
  "shots": [
    {{
      "shotIndex": 1,
      "englishName": "Full-length hero shot",
      "title": "全身主视觉",
      "task": "先给完整穿着比例；全身或近全身，头脚完整，商品无遮挡",
      "scene": "景别，必须来自规范第 4 节和对应品类原则",
      "cropRange": "取景范围，例如头顶到脚尖完整入画",
      "modelScale": "模特占画幅，例如 75%-86%",
      "headTreatment": "完整露头 | 切额头/半头 | 下巴以下裁切 | 不露脸裁切",
      "bodyPosition": "正面 | 轻斜正 | 3/4 斜正 | 侧身 | 背面",
      "poseTone": "姿势调性，必须符合对应品类姿势示例池",
      "imagePurpose": "画面目的，说明这一张补充什么商品信息"
    }}
  ]
}}
"""


def load_brand_tone() -> str:
    try:
        return BRAND_TONE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PlannerError(f"Gemini 引用文档不存在：{BRAND_TONE_PATH}") from None


async def plan_with_gemini(sku: str, image_paths: list[Path]) -> tuple[dict[str, Any], str, str]:
    settings = get_settings()
    prompt = build_planner_prompt(sku)
    request_snapshot = {
        "model": settings.gemini_model,
        "prompt": prompt,
        "inputs": {
            "sku": sku,
            "imagePaths": [str(path) for path in image_paths[:6]],
        },
        "responseMimeType": "application/json",
        "responseSchema": "ShotPlanDraft",
    }
    if not settings.gemini_api_key:
        raise PlannerError("GEMINI_API_KEY 未配置，无法生成组图方案。", request_snapshot)

    try:
        from google import genai
        from google.genai import types
    except Exception as exc:  # pragma: no cover - dependency issue branch
        raise PlannerError(f"google-genai 导入失败，无法调用 Gemini：{exc}", request_snapshot) from exc

    contents: list[Any] = [prompt]
    for path in image_paths[:6]:
        contents.append(
            types.Part.from_bytes(data=path.read_bytes(), mime_type=guess_mime_type(path))
        )

    client = genai.Client(api_key=settings.gemini_api_key)
    try:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ShotPlanDraft,
        )
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=config,
        )
    except TypeError:
        request_snapshot["responseSchemaFallback"] = "response_schema unsupported by installed google-genai"
        try:
            response = await client.aio.models.generate_content(
                model=settings.gemini_model,
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
        except Exception as exc:
            raise PlannerError(f"Gemini 请求失败：{exc}", request_snapshot) from exc
    except Exception as exc:
        raise PlannerError(f"Gemini 请求失败：{exc}", request_snapshot) from exc
    raw = response.text or "{}"
    sdk_parsed = parsed_planner_response_from_sdk(response)
    if sdk_parsed is None:
        parsed, parse_meta = parse_planner_response(raw, request_snapshot)
    else:
        parsed = sdk_parsed
        parse_meta = {"mode": "sdk_parsed"}
    validated = validate_planner_response(parsed, request_snapshot, raw)
    plan = normalize_plan(validated, sku)
    return plan, "gemini", json.dumps(
        {
            "providerRequest": request_snapshot,
            "providerResponse": validated,
            "parseMeta": parse_meta,
            "rawText": raw,
        },
        ensure_ascii=False,
    )


def parsed_planner_response_from_sdk(response: Any) -> dict[str, Any] | None:
    parsed = getattr(response, "parsed", None)
    if parsed is None:
        return None
    if isinstance(parsed, BaseModel):
        value = parsed.model_dump(mode="json")
    elif isinstance(parsed, dict):
        value = parsed
    else:
        return None
    return value if isinstance(value, dict) else None


def parse_planner_response(raw: str, provider_request: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    text = strip_json_fence(raw)
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise PlannerError("Gemini 返回 JSON 顶层不是对象。", provider_request, raw)
        return parsed, {"mode": "strict"}
    except json.JSONDecodeError as strict_error:
        decoder = json.JSONDecoder()
        try:
            parsed, end_index = decoder.raw_decode(text)
        except json.JSONDecodeError:
            raise PlannerError(
                "Gemini 返回内容不是合法 JSON。",
                provider_request,
                {
                    "rawText": raw,
                    "parseError": str(strict_error),
                },
            ) from None
        trailing = text[end_index:].strip()
        if not isinstance(parsed, dict) or not is_recoverable_json_trailing_noise(trailing):
            raise PlannerError(
                "Gemini 返回内容不是可恢复的拍摄方案 JSON。",
                provider_request,
                {
                    "rawText": raw,
                    "parseError": str(strict_error),
                    "trailingText": trailing[:500],
                },
            ) from None
        return parsed, {
            "mode": "recovered_trailing_noise",
            "parseError": str(strict_error),
            "trailingText": trailing,
        }


def strip_json_fence(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


def is_recoverable_json_trailing_noise(value: str) -> bool:
    if not value:
        return True
    return set(value) <= {"}"}


def validate_planner_response(parsed: dict[str, Any], provider_request: dict[str, Any] | None = None, raw: str | None = None) -> dict[str, Any]:
    try:
        draft = ShotPlanDraft.model_validate(parsed)
    except ValidationError as exc:
        raise PlannerError(
            "Gemini 返回的拍摄方案结构校验失败。",
            provider_request,
            {
                "rawText": raw,
                "validationErrors": exc.errors(),
                "providerResponse": parsed,
            },
        ) from None
    if len(draft.shots) < 5:
        raise PlannerError(
            "Gemini 返回的方案不足 5 个固定图位。",
            provider_request,
            {
                "rawText": raw,
                "providerResponse": parsed,
                "shotCount": len(draft.shots),
            },
        )
    return draft.model_dump(mode="json")


def normalize_plan(plan: dict[str, Any], sku: str) -> dict[str, Any]:
    if not isinstance(plan.get("shots"), list) or not plan["shots"]:
        raise PlannerError("Gemini 返回的方案缺少 shots。")
    plan.setdefault("sku", sku)
    plan.setdefault("modelId", "Face-01")
    garment = plan.get("garment", {}) if isinstance(plan.get("garment"), dict) else {}
    category = normalize_category_name(str(plan.get("category") or garment.get("category") or "连衣裙"))
    plan["category"] = category
    garment["category"] = category
    plan["garment"] = garment
    generation_plan = plan.get("generationPlan") if isinstance(plan.get("generationPlan"), dict) else {}
    sales_info = generation_plan.get("salesInfo") or plan.get("salesInfo") or garment.get("summary", "主销售信息待确认")
    lily_tone = generation_plan.get("lilyTone") or plan.get("lilyTone") or "浅灰白棚拍、干净通勤、轻熟克制、商品清楚。"
    styling = generation_plan.get("styling") or combine_text(
        plan.get("styling"),
        plan.get("stylingPlan"),
        plan.get("colorPalette"),
        plan.get("accessories"),
    ) or "整组统一搭配，低饱和 Lily 常用色，鞋包首饰低调通勤，不遮挡主商品。"
    background = generation_plan.get("background") or plan.get("background") or "白/浅灰棚拍，轻微落地阴影"
    plan["generationPlan"] = {
        "salesInfo": sales_info,
        "lilyTone": lily_tone,
        "styling": styling,
        "background": background,
    }
    for legacy_key in ["salesInfo", "evidenceLevel", "lilyTone", "styling", "stylingPlan", "colorPalette", "accessories", "background"]:
        plan.pop(legacy_key, None)
    plan.setdefault("brandStyle", {"name": "Lily 服装模特棚拍导演规范", "summary": lily_tone})
    normalized_shots: list[dict[str, Any]] = []
    for slot, raw_shot in zip(SHOT_SLOTS, plan["shots"][:5]):
        shot = raw_shot if isinstance(raw_shot, dict) else {}
        shot["shotIndex"] = slot["shotIndex"]
        shot.setdefault("englishName", slot["englishName"])
        raw_title = str(shot.get("title") or "").strip()
        raw_slot_name = str(shot.get("slotName") or "").strip()
        if raw_slot_name and (not raw_title or raw_title.startswith(f"图 {slot['shotIndex']}")):
            shot["title"] = raw_slot_name
        else:
            for prefix in (f"图 {slot['shotIndex']} ", f"图 {slot['shotIndex']:02d} "):
                if raw_title.startswith(prefix):
                    raw_title = raw_title[len(prefix):].strip()
            shot["title"] = raw_title or slot["title"]
        task = str(shot.get("task") or slot["task"])
        unified_requirement = str(shot.get("unifiedRequirement") or "")
        if unified_requirement and unified_requirement not in task:
            task = f"{task}；{unified_requirement}"
        shot["task"] = task
        shot.setdefault("scene", shot.get("frame", "全身图"))
        shot.setdefault("cropRange", shot.get("crop", shot.get("scene", "头顶到脚尖完整入画")))
        shot.setdefault("modelScale", "75%-88%")
        shot.setdefault("headTreatment", "完整露头" if slot["shotIndex"] in {1, 5} else "按设计点可切头/不露脸")
        shot.setdefault("bodyPosition", shot.get("angle", "正面"))
        shot.setdefault("poseTone", shot.get("pose", default_pose(shot["bodyPosition"], category)))
        shot.setdefault("imagePurpose", shot.get("focus", slot["task"]))
        normalized_shots.append(
            {
                "shotIndex": shot["shotIndex"],
                "englishName": shot.get("englishName"),
                "title": shot.get("title"),
                "task": shot.get("task"),
                "scene": shot.get("scene"),
                "cropRange": shot.get("cropRange"),
                "modelScale": shot.get("modelScale"),
                "headTreatment": shot.get("headTreatment"),
                "bodyPosition": shot.get("bodyPosition"),
                "poseTone": shot.get("poseTone"),
                "imagePurpose": shot.get("imagePurpose"),
            }
        )
    if len(normalized_shots) < 5:
        raise PlannerError("Gemini 返回的方案不足 5 个固定图位。")
    plan["shots"] = normalized_shots[:5]
    return plan


def normalize_category_name(value: str) -> str:
    if any(token in value for token in ["裤装", "裤子", "长裤", "阔腿裤", "西裤", "牛仔裤", "休闲裤"]):
        return "裤装"
    if any(token in value for token in ["半裙", "长裙", "半身裙", "A 字裙", "百褶裙"]):
        return "半裙"
    if "短裤" in value:
        return "短裤"
    if any(token in value for token in ["外套", "西装", "套装", "开衫", "夹克", "风衣"]):
        return "外套" if "套装" not in value else "套装"
    if "连衣裙" in value or "裙" in value:
        return "连衣裙"
    if any(token in value for token in ["上衣", "T 恤", "T恤", "针织", "衬衫", "背心", "马甲", "短袖", "无袖"]):
        return "上衣"
    return value if value in CATEGORIES else "连衣裙"


def combine_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "；".join(parts)


def guess_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"
