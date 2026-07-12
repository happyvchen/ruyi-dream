"""生图阶段 prompt 渲染层（P1）。

职责：把上游已组装好的结构化上下文（``shoot_script_result_for_prompt`` /
``current_shot_context_for_prompt`` / hero 信息 / 用户要求）渲染成**干净的自然语言文本**，
供 ``assembly.storyboard_preview`` / ``assembly.final_image`` 模板占位符填充。

设计边界（见 design/01_generation_flow/prompt_rendering_refactor.md §3.1）：

- 只做「数据 -> 文本」，不碰 DB、不发请求、不改状态；纯函数，可独立单测。
- 不输出治理元数据（``schema_version`` / ``priority_policy`` / ``cannot_override`` /
  ``priority_rules`` / ``conflict_handling``），这些规则由可编辑模板正文声明。
- 不硬编码规则话术、不重新判断事实优先级——那是模板/上游的职责。
- 空值优雅省略：字段为空则不输出该行，杜绝 ``""`` / ``[]`` / ``{}`` 空壳。
- 字段取舍严格遵循设计文档 §4A 的字段映射表：保留全部导演字段，仅丢弃机器码重复字段。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "render_shoot_script_resolved_accessories",
    "render_shoot_script_shots",
    "render_preview_grid_cell",
    "render_shot_styling",
    "render_shot_global_directing",
    "render_shot_current",
    "render_shot_accessories",
    "render_hero_product_info_line",
    "render_user_instruction_line",
    "render_user_requirement_line",
    "render_image_reference_spans",
]

EMPTY_PLACEHOLDER = "（无）"


# --------------------------------------------------------------------------- #
# 基础工具（纯文本，无副作用）
# --------------------------------------------------------------------------- #
def _clean_text(value: Any) -> str:
    """转成去首尾空白、去重复句末标点的字符串；非字符串安全降级。"""
    if value is None:
        return ""
    text = str(value).strip()
    while text.endswith(("。。", "；；", "，，", "..")):
        text = text[:-1]
    return text


def _clean_list(value: Any) -> list[str]:
    """把任意列表规整成去空、去重（保序）的字符串列表。"""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        text = _clean_text(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _join(value: Any, sep: str = "；") -> str:
    return sep.join(_clean_list(value))


def _kv_line(label: str, value: Any) -> str | None:
    """``label：value``；value 为空则返回 None（调用方过滤掉）。"""
    text = _clean_text(value)
    return f"{label}：{text}" if text else None


def _list_line(label: str, value: Any, sep: str = "；") -> str | None:
    joined = _join(value, sep)
    return f"{label}：{joined}" if joined else None


def _block(lines: list[str | None]) -> str:
    """拼接非空行为一个段落。"""
    return "\n".join(line for line in lines if line)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------- #
# 全局导演 / 镜头（storyboard 与 final 共用）
# --------------------------------------------------------------------------- #
def _global_directing_lines(gd: dict[str, Any]) -> list[str | None]:
    return [
        _kv_line("配饰边界", gd.get("accessory_boundary")),
        _list_line("一致性", gd.get("consistency_rules")),
        _list_line("全局禁止", gd.get("negative_constraints")),
    ]


def _shot_lines(shot: dict[str, Any], *, indent: str = "  ") -> str:
    """单个镜头渲染为多行文本。字段取舍见设计文档 §4A。"""
    label = _clean_text(shot.get("shot_role_label")) or _clean_text(shot.get("shot_role"))
    framing_parts = [
        p
        for p in (_clean_text(shot.get("framing")), _clean_text(shot.get("crop_range")))
        if p
    ]
    header_meta = "·".join(framing_parts)
    idx = shot.get("shot_index")
    header = f"格{idx}" if idx is not None else "镜头"
    if label:
        header += f"（{label}{'·' + header_meta if header_meta else ''}）"
    elif header_meta:
        header += f"（{header_meta}）"

    # 机位/占比/身位/姿态/动势 合并为一行
    pose_bits: list[str] = []
    for lbl, key in (("机位", "camera_angle"), ("占比", "model_scale"), ("身位", "body_position"), ("姿态", "pose")):
        text = _clean_text(shot.get(key))
        if text:
            pose_bits.append(f"{lbl}：{text}")
    movement = _clean_text(shot.get("movement"))
    if movement and movement not in {"静态", "无", "静止"}:
        pose_bits.append(f"动势：{movement}")

    lines = [
        header + "：",
        _kv_line(f"{indent}核心", shot.get("main_focus")),
        (indent + "｜".join(pose_bits)) if pose_bits else None,
        _kv_line(f"{indent}手部", shot.get("hand_position")),
        _list_line(f"{indent}商品重点", shot.get("hero_selling_points")),
        _kv_line(f"{indent}构图", shot.get("composition_notes")),
        _list_line(f"{indent}配饰", shot.get("accessory_instructions")),
        _list_line(f"{indent}不可遮挡", shot.get("forbidden_occlusion_zones")),
        _list_line(f"{indent}本格禁止", shot.get("negative_constraints")),
        _list_line(f"{indent}验收", shot.get("acceptance_criteria")),
    ]
    return _block(lines)


def _resolved_accessory_lines(items: Any) -> list[str]:
    lines: list[str] = []
    for item in items if isinstance(items, list) else []:
        item = _as_dict(item)
        direction = _clean_text(item.get("final_direction"))
        if not direction:
            continue
        item_type = _clean_text(item.get("item_type"))
        applies = _clean_list(item.get("applies_to_shots"))
        suffix = f"（适用格 {'、'.join(applies)}）" if applies else ""
        prefix = f"{item_type}：" if item_type else ""
        lines.append(f"- {prefix}{direction}{suffix}")
    return lines


# --------------------------------------------------------------------------- #
# 对外渲染函数
# --------------------------------------------------------------------------- #
# 说明：以下渲染函数只输出「可变数据正文」，不带任何表头（【全局导演】等）、规则话术或
# 事实源约束——那些固定文本一律由「Prompt 维护」页的模板正文托管，配合条件段
# {{#runtime.x}}...{{/runtime.x}} 决定"有内容才显示表头"。空数据一律返回空串，模板据此省略整段。
#
# 边界：单条镜头/配饰内部的字段标签（核心/构图/机位…）属于「变长记录的序列化格式」，
# 简单占位引擎无法用循环表达，故保留在代码里；它们是数据格式，不是可调话术。
def render_shoot_script_resolved_accessories(context: dict[str, Any]) -> str:
    """拍摄剧本本次配饰正文（无表头）。"""
    return "\n".join(_resolved_accessory_lines(_as_dict(context).get("resolved_accessories")))


def render_shoot_script_shots(context: dict[str, Any]) -> str:
    """拍摄剧本六格镜头正文（无表头）。"""
    shots = [s for s in (_as_dict(context).get("shots") or []) if isinstance(s, dict)]
    return "\n".join(_shot_lines(shot) for shot in shots)


def render_preview_grid_cell(context: dict[str, Any]) -> str:
    """当前成片对应分镜预览格号（纯数字文本，如 ``5``）；无则空串。"""
    cell = _as_dict(context).get("preview_grid_cell")
    return "" if cell is None else _clean_text(cell)


def render_shot_styling(context: dict[str, Any]) -> str:
    """当前成片造型正文（无表头）。"""
    selected = _as_dict(_as_dict(_as_dict(context).get("styling")).get("selected_outfit"))
    return _block([
        _kv_line("造型", selected.get("title")),
        _kv_line("穿法", selected.get("wearing_notes")),
        _list_line("选中搭配 code", selected.get("selected_material_codes"), sep="、"),
    ])


def render_shot_global_directing(context: dict[str, Any]) -> str:
    """当前成片全局导演正文（无表头）。"""
    return _block(_global_directing_lines(_as_dict(_as_dict(context).get("global_directing"))))


def render_shot_current(context: dict[str, Any]) -> str:
    """当前成片单镜头正文（无表头）。"""
    current_shot = _as_dict(_as_dict(context).get("current_shot"))
    return _shot_lines(current_shot) if current_shot else ""


def render_shot_accessories(context: dict[str, Any]) -> str:
    """当前成片本镜头配饰正文（无表头）。"""
    return "\n".join(_resolved_accessory_lines(_as_dict(context).get("resolved_accessories_for_this_shot")))


def render_hero_product_info_line(context: dict[str, Any]) -> str:
    """渲染商品信息为一句话；无内容时返回空串，交由模板条件段省略整行。"""
    return _clean_text(_as_dict(context).get("info"))


def render_user_instruction_line(context: dict[str, Any]) -> str:
    """渲染本步骤用户要求为一句话；无内容时返回空串，交由模板条件段省略整行。"""
    return _clean_text(_as_dict(context).get("instruction"))


def render_user_requirement_line(context: dict[str, Any]) -> str:
    """渲染 AI Stylist 用的用户要求为一句话；无内容时返回空串。

    优先取 ``combined_requirement``（上传要求 + 本次微调合并后），回退 ``upload_requirement``。
    优先级/冲突处理规则不在此渲染——它由 system instruction 与模板正文声明。
    """
    ctx = _as_dict(context)
    return _clean_text(ctx.get("combined_requirement")) or _clean_text(ctx.get("upload_requirement"))


def _span_text(start: int, end: int) -> str:
    return f"图{start}" if start == end else f"图{start}-{end}"


def render_image_reference_spans(image_inputs: list[dict[str, Any]] | None) -> dict[str, str]:
    """按「职能」输出参考图图号区间（**只出会变的图号**，职能描述由模板正文托管）。

    图号严格对应 provider ``reference_images`` 传入顺序（由选图器决定）。每个职能取该角色
    出现的最小到最大图号，合并成「图a」或「图a-b」；不存在的职能返回空串，模板据此省略整段。

    返回键：``model_identity`` / ``preview_grid`` / ``garment`` / ``shoot_environment``。
    """
    ranges: dict[str, list[int]] = {}
    for index, item in enumerate(image_inputs or [], start=1):
        role = str(_as_dict(item).get("role") or "")
        if not role:
            continue
        if role in ranges:
            ranges[role][1] = index
        else:
            ranges[role] = [index, index]

    result: dict[str, str] = {}
    for role in ("model_identity", "preview_grid", "shoot_environment"):
        result[role] = _span_text(*ranges[role]) if role in ranges else ""

    garments = [ranges[r] for r in ("hero_garment", "styling_garment") if r in ranges]
    result["garment"] = (
        _span_text(min(s for s, _ in garments), max(e for _, e in garments)) if garments else ""
    )

    return result
