from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from .config import get_settings


CATEGORY_SHOT_RULES: dict[str, list[dict[str, str]]] = {
    "连衣裙": [
        {"title": "首图", "frame": "正面全身", "angle": "正面", "focus": "整体裙型"},
        {"title": "3/4 转体", "frame": "全身", "angle": "微侧", "focus": "腰线、裙摆、身体轮廓"},
        {"title": "上半身图", "frame": "半身", "angle": "正面", "focus": "领口、袖型、腰部结构"},
        {"title": "背面/侧面", "frame": "全身", "angle": "背面或侧面", "focus": "后片、裙长、开衩"},
        {"title": "局部图", "frame": "特写", "angle": "局部", "focus": "面料、腰线、裙摆、领口"},
    ],
    "上衣": [
        {"title": "首图", "frame": "正面半身", "angle": "正面", "focus": "领口、肩线、衣身"},
        {"title": "搭配图", "frame": "全身", "angle": "正面或 3/4", "focus": "整体穿搭效果"},
        {"title": "版型图", "frame": "半身或 3/4 身", "angle": "微侧", "focus": "宽松度、下摆、袖型"},
        {"title": "背面图", "frame": "半身", "angle": "背面", "focus": "后片、肩背结构"},
        {"title": "局部图", "frame": "特写", "angle": "正面", "focus": "领口、袖口、面料"},
    ],
    "外套": [
        {"title": "首图", "frame": "正面全身", "angle": "正面", "focus": "外套整体轮廓"},
        {"title": "敞开穿法", "frame": "半身或 3/4 身", "angle": "正面", "focus": "领型、门襟、内搭关系"},
        {"title": "扣合穿法", "frame": "半身", "angle": "正面", "focus": "扣位、腰线、版型"},
        {"title": "侧身图", "frame": "全身或 3/4 身", "angle": "侧面", "focus": "厚薄、肩线、衣长"},
        {"title": "背面/局部", "frame": "背面或特写", "angle": "背面/局部", "focus": "后片、口袋、纽扣、面料"},
    ],
    "裤子": [
        {"title": "首图", "frame": "正面全身", "angle": "正面", "focus": "整体裤型、腿长比例"},
        {"title": "下半身图", "frame": "腰部到脚部", "angle": "正面", "focus": "腰头、裤腿、裤长"},
        {"title": "侧身图", "frame": "全身或下半身", "angle": "侧面", "focus": "侧缝、裤腿宽度、垂坠"},
        {"title": "背面图", "frame": "下半身或全身", "angle": "背面", "focus": "臀部、后袋、腰头"},
        {"title": "局部图", "frame": "特写", "angle": "局部", "focus": "裤脚、腰头、面料"},
    ],
}


def fallback_plan(sku: str, model_id: str, category: str = "连衣裙") -> dict[str, Any]:
    normalized = category if category in CATEGORY_SHOT_RULES else "连衣裙"
    shots = []
    for index, rule in enumerate(CATEGORY_SHOT_RULES[normalized], start=1):
        shots.append(
            {
                "shotIndex": index,
                "title": rule["title"],
                "frame": rule["frame"],
                "angle": rule["angle"],
                "focus": rule["focus"],
                "pose": default_pose(rule["angle"], normalized),
                "headReference": default_head_reference(rule["angle"]),
                "background": "白/浅灰棚拍",
                "styling": {
                    "shoes": "黑色平底凉鞋或黑色乐福鞋，简洁年轻",
                    "bag": "可选黑色小手提包",
                    "jewelry": "极简，不抢商品",
                    "forbidden": ["尖头职业鞋", "夸张高跟鞋", "运动鞋", "厚底鞋"],
                },
            }
        )
    return {
        "sku": sku,
        "modelId": model_id,
        "garment": {
            "category": normalized,
            "summary": f"系统将按 {normalized} 的 Lily 默认棚拍规则生成组图。",
            "color": "待从主图确认",
            "fabric": "待从主图确认",
            "keyStructures": [],
            "preserveStrictly": ["以服装参考图为商品事实源，不改变品类、颜色、结构和比例"],
        },
        "brandStyle": {
            "name": "Lily 默认棚拍风格",
            "summary": "白/浅灰背景，自然姿态，简洁搭配，商品优先。",
        },
        "shots": shots,
    }


def default_pose(angle: str, category: str) -> str:
    if "背面" in angle:
        return "背面自然站姿，肩颈放松，必要时轻微回头"
    if "侧" in angle or "3/4" in angle:
        return "身体微侧，重心轻微偏移，手臂自然下垂，带轻松走动感"
    if category in {"裤子"}:
        return "正面自然站姿，腿部错开，突出裤型比例"
    return "正面自然站姿，重心不僵硬，手臂放松，克制商业表情"


def default_head_reference(angle: str) -> str:
    if "背面" in angle:
        return "back.png"
    if "侧" in angle or "3/4" in angle:
        return "left45.png"
    return "front.png"


def build_planner_prompt(sku: str) -> str:
    return f"""
你是 Lily 电商棚拍组图策划器。请根据用户上传的服装主图和参考图，识别服装品类，并决定默认 5 张组图的构图、模特姿势和服饰搭配。

要求：
- 用户不写 prompt，所以输出必须是结构化拍摄方案。
- 只支持 Lily 白/浅灰棚拍、电商主图风格。
- 优先服装保真：不改变颜色、结构、领型、袖长、裤长、裙长、口袋、纽扣等。
- 模特使用 Face-01，短发、自然淡妆、克制通勤气质。
- 根据品牌调性选择鞋履/包/配饰，避免老气尖头职业鞋、夸张高跟鞋、运动鞋、厚底鞋。
- 默认输出 5 张。

请只返回 JSON，不要 Markdown。JSON schema：
{{
  "sku": "{sku}",
  "modelId": "Face-01",
  "garment": {{
    "category": "连衣裙 | 上衣 | 外套 | 马甲 | 裤子 | 半裙/长裙 | 套装",
    "summary": "一句话服装摘要",
    "color": "主颜色",
    "fabric": "面料视觉特征",
    "keyStructures": ["关键结构"],
    "preserveStrictly": ["必须保真的点"]
  }},
  "brandStyle": {{
    "name": "Lily 默认棚拍风格",
    "summary": "白/浅灰背景，自然姿态，简洁搭配，商品优先"
  }},
  "shots": [
    {{
      "shotIndex": 1,
      "title": "首图",
      "frame": "正面全身/正面半身/全身/半身/特写",
      "angle": "正面/微侧/背面/局部",
      "focus": "商品展示重点",
      "pose": "模特姿势",
      "headReference": "front.png | left45.png | right90.png | back.png | back-turn-left45.png | back-turn-right45.png",
      "background": "白/浅灰棚拍",
      "styling": {{
        "shoes": "鞋履建议",
        "bag": "包建议",
        "jewelry": "配饰建议",
        "forbidden": ["禁用搭配"]
      }}
    }}
  ]
}}
"""


async def plan_with_gemini(sku: str, image_paths: list[Path]) -> tuple[dict[str, Any], str, str]:
    settings = get_settings()
    if not settings.gemini_api_key:
        plan = fallback_plan(sku, "Face-01")
        return plan, "fallback", "GEMINI_API_KEY is not configured; used fallback plan."

    try:
        from google import genai
        from google.genai import types
    except Exception as exc:  # pragma: no cover - dependency issue branch
        plan = fallback_plan(sku, "Face-01")
        return plan, "fallback", f"google-genai import failed: {exc}"

    prompt = build_planner_prompt(sku)
    contents: list[Any] = [prompt]
    for path in image_paths[:6]:
        contents.append(
            types.Part.from_bytes(data=path.read_bytes(), mime_type=guess_mime_type(path))
        )

    client = genai.Client(api_key=settings.gemini_api_key)
    try:
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
    except Exception as exc:
        plan = fallback_plan(sku, "Face-01")
        return plan, "gemini-error-fallback", f"Gemini request failed: {exc}"
    raw = response.text or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = fallback_plan(sku, "Face-01")
        raw = f"Failed to parse Gemini JSON. Raw response: {raw}"
        return parsed, "gemini-json-parse-fallback", raw
    return normalize_plan(parsed, sku), "gemini", raw


def normalize_plan(plan: dict[str, Any], sku: str) -> dict[str, Any]:
    if not isinstance(plan.get("shots"), list) or not plan["shots"]:
        return fallback_plan(sku, "Face-01", plan.get("garment", {}).get("category", "连衣裙"))
    plan.setdefault("sku", sku)
    plan.setdefault("modelId", "Face-01")
    plan.setdefault("brandStyle", {"name": "Lily 默认棚拍风格", "summary": "白/浅灰背景，自然姿态，简洁搭配，商品优先。"})
    for index, shot in enumerate(plan["shots"][:5], start=1):
        shot.setdefault("shotIndex", index)
        shot.setdefault("title", f"图 {index}")
        shot.setdefault("frame", "全身")
        shot.setdefault("angle", "正面")
        shot.setdefault("focus", "服装展示")
        shot.setdefault("pose", default_pose(shot["angle"], plan.get("garment", {}).get("category", "连衣裙")))
        shot.setdefault("headReference", default_head_reference(shot["angle"]))
        shot.setdefault("background", "白/浅灰棚拍")
        shot.setdefault("styling", {})
    plan["shots"] = plan["shots"][:5]
    return plan


def guess_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"
