"""当前 Prompt 渲染层。

职责边界很窄：把代码侧动态事实渲染为 Prompt 维护模板中的占位符文本。
固定摄影规则、六镜头策略、成片兑现规则、事实源约束都写在 Prompt 维护页的模板正文里。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "render_hero_product_info_line",
    "render_user_instruction_line",
    "render_user_requirement_line",
    "render_image_reference_spans",
]


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    while text.endswith(("。。", "；；", "，，", "..")):
        text = text[:-1]
    return text


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def render_hero_product_info_line(context: dict[str, Any]) -> str:
    """渲染商品信息为一句话；无内容时返回空串，交由模板条件段省略整行。"""
    return _clean_text(_as_dict(context).get("info"))


def render_user_instruction_line(context: dict[str, Any]) -> str:
    """渲染本步骤用户要求为一句话；无内容时返回空串，交由模板条件段省略整行。"""
    return _clean_text(_as_dict(context).get("instruction"))


def render_user_requirement_line(context: dict[str, Any]) -> str:
    """渲染 AI Stylist 用的用户要求为一句话；无内容时返回空串。"""
    ctx = _as_dict(context)
    return _clean_text(ctx.get("combined_requirement")) or _clean_text(ctx.get("upload_requirement"))


def _span_text(start: int, end: int) -> str:
    return f"图{start}" if start == end else f"图{start}-{end}"


def render_image_reference_spans(image_inputs: list[dict[str, Any]] | None) -> dict[str, str]:
    """按输入顺序输出各职能图号区间；职能解释由 Prompt 模板正文托管。"""
    spans: dict[str, list[int]] = {
        "model_identity": [],
        "preview_grid": [],
        "garment": [],
        "shoot_environment": [],
    }
    for index, item in enumerate(image_inputs or [], start=1):
        role = str(_as_dict(item).get("role") or "")
        if role == "model_identity":
            spans["model_identity"].append(index)
        elif role == "preview_grid":
            spans["preview_grid"].append(index)
        elif role in {"hero_garment", "styling_garment", "garment"}:
            spans["garment"].append(index)
        elif role == "shoot_environment":
            spans["shoot_environment"].append(index)
    return {
        key: _span_text(values[0], values[-1]) if values else ""
        for key, values in spans.items()
    }
