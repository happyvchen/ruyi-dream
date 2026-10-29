"""当前 Prompt 渲染层。

职责边界很窄：把代码侧动态事实渲染为 Prompt 维护模板中的占位符文本。
固定摄影规则、五镜头策略、成片规则和事实源约束都写在 Prompt 维护页的模板正文里。
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = [
    "render_user_instruction_line",
    "render_user_requirement_line",
    "render_image_reference_spans",
    "render_pose_direction",
    "render_model_body_facts",
    "upgrade_prompt_runtime_placeholders",
]

# 姿势事实里真正描述"怎么摆"的字段。选搭元信息（展示优势、遮挡风险、
# 适配加分/排除规则）属于姿势选搭阶段的判断依据，不是拍摄事实，
# 注入成片 Prompt 只会稀释商品与构图要求。
POSE_DIRECTION_FACT_KEYS: tuple[str, ...] = (
    "姿势描述",
    "姿势类别",
    "身体朝向",
    "头部视线",
    "面部可见性",
    "表情神态",
    "模特右手",
    "模特左手",
    "双腿与重心",
    "核心动作",
    "可调整动作",
    "存疑信息",
)


def render_model_body_facts(snapshot: dict[str, Any] | None) -> str:
    """Only render explicitly frozen measurements; never infer from portraits."""
    profile = (snapshot or {}).get("profile") or {}
    if not isinstance(profile, dict):
        return ""
    fields = (("height", "身高"), ("weight", "体重"), ("bodyShape", "体型"),
              ("bust", "胸围"), ("waist", "腰围"), ("hips", "臀围"))
    return "；".join(f"{label}：{_clean_text(profile[key])}" for key, label in fields
                    if key in profile and _clean_text(profile[key]))


def _join_pose_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "、".join(_clean_text(item) for item in value if _clean_text(item))
    return _clean_text(value)


def render_pose_direction(direction: Any) -> str:
    """把冻结的姿势动作渲染为可执行的拍摄动作说明。

    非 JSON 的历史快照原样返回；JSON 快照只保留动作事实字段。
    """
    text = str(direction or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(parsed, dict):
        return text
    parts = []
    for key in POSE_DIRECTION_FACT_KEYS:
        if key not in parsed:
            continue
        value = _join_pose_value(parsed.get(key))
        if value:
            parts.append(value if key == "姿势描述" else f"{key}：{value}")
    return "；".join(parts) if parts else text


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    while text.endswith(("。。", "；；", "，，", "..")):
        text = text[:-1]
    return text


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def render_user_instruction_line(context: dict[str, Any]) -> str:
    """渲染本步骤用户要求为一句话；无内容时返回空串，交由模板条件段省略整行。"""
    return _clean_text(_as_dict(context).get("instruction"))


def render_user_requirement_line(context: dict[str, Any]) -> str:
    """渲染任务级服装信息为一句话；无内容时返回空串。"""
    ctx = _as_dict(context)
    return _clean_text(ctx.get("upload_requirement"))


def _span_text(start: int, end: int) -> str:
    return f"图{start}" if start == end else f"图{start}-{end}"


def render_image_reference_spans(image_inputs: list[dict[str, Any]] | None) -> dict[str, str]:
    """按输入顺序输出各职能图号区间；职能解释由 Prompt 模板正文托管。"""
    spans: dict[str, list[int]] = {
        "model_identity": [],
        "preview_grid": [],
        "hero_garment": [],
        "styling_garment": [],
        "shoot_environment": [],
        "shot_background": [],
        "shot_pose": [],
    }
    for index, item in enumerate(image_inputs or [], start=1):
        role = str(_as_dict(item).get("role") or "")
        if role == "model_identity":
            spans["model_identity"].append(index)
        elif role == "preview_grid":
            spans["preview_grid"].append(index)
        elif role in {"hero_garment", "styling_garment"}:
            spans[role].append(index)
        elif role == "shoot_environment":
            spans["shoot_environment"].append(index)
        elif role == "shot_background":
            spans["shot_background"].append(index)
        elif role == "shot_pose":
            spans["shot_pose"].append(index)
    return {
        key: _reference_ranges(values)
        for key, values in spans.items()
    }


def _reference_ranges(values: list[int]) -> str:
    """只合并相邻图号，避免将其他角色的图片纳入当前事实源。"""
    if not values:
        return ""
    ranges: list[str] = []
    start = end = values[0]
    for value in values[1:]:
        if value == end + 1:
            end = value
        else:
            ranges.append(_span_text(start, end))
            start = end = value
    ranges.append(_span_text(start, end))
    return "、".join(ranges)


def upgrade_prompt_runtime_placeholders(content: str) -> str:
    """迁移存量配置和读取冻结配置；不再提供已删除变量的运行时值。"""
    for name in ("shot_global_directing", "shot_current"):
        # 原字段始终非空：保留正条件内的自定义正文，移除反条件分支。
        section = re.compile(r"\{\{\s*([#^])runtime\." + name + r"\s*\}\}(.*?)\{\{\s*/runtime\." + name + r"\s*\}\}", re.S)
        content = section.sub(lambda m: m[2] if m[1] == "#" else "", content)
        content = re.sub(r"\{\{\s*runtime\." + name + r"\s*\}\}", "", content)
    # 合并引用的条件等价于主推款或搭配款任一存在。
    section = re.compile(r"\{\{\s*([#^])runtime\.ref_garment\s*\}\}(.*?)\{\{\s*/runtime\.ref_garment\s*\}\}", re.S)
    def split_section(match: re.Match[str]) -> str:
        inner = match[2]
        if match[1] == "^":
            return "{{^runtime.ref_hero_garment}}{{^runtime.ref_styling_garment}}" + inner + "{{/runtime.ref_styling_garment}}{{/runtime.ref_hero_garment}}"
        return ("{{#runtime.ref_hero_garment}}" + inner + "{{/runtime.ref_hero_garment}}"
                "{{^runtime.ref_hero_garment}}{{#runtime.ref_styling_garment}}" + inner +
                "{{/runtime.ref_styling_garment}}{{/runtime.ref_hero_garment}}")
    content = section.sub(split_section, content)
    return re.sub(r"\{\{\s*runtime\.ref_garment\s*\}\}",
        "{{#runtime.ref_hero_garment}}主推款 {{runtime.ref_hero_garment}}{{/runtime.ref_hero_garment}}"
        "{{#runtime.ref_styling_garment}}；搭配款 {{runtime.ref_styling_garment}}{{/runtime.ref_styling_garment}}", content)
