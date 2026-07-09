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
    "render_shoot_script_body",
    "render_shot_context_body",
    "render_hero_product_info_line",
    "render_user_instruction_line",
    "render_user_requirement_line",
    "render_image_reference_line",
    "render_model_identity_line",
]

EMPTY_PLACEHOLDER = "（无）"

# 参考图角色 -> 面向模型的中文短标签（顺序说明用）。
_REFERENCE_ROLE_LABELS = {
    "model_identity": "数字模特身份",
    "hero_garment": "主推款",
    "styling_garment": "搭配服装",
    "preview_grid": "分镜预览（构图参考）",
    "shoot_environment": "拍摄环境参考",
}


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
        _kv_line(f"{indent}构图", shot.get("composition_notes")),
        _list_line(f"{indent}配饰", shot.get("accessory_instructions")),
        _list_line(f"{indent}不可遮挡", shot.get("forbidden_occlusion_zones")),
        _list_line(f"{indent}本格禁止", shot.get("negative_constraints")),
    ]
    return _block(lines)


def _styling_risk_lines(risks: Any) -> list[str]:
    lines: list[str] = []
    for risk in risks if isinstance(risks, list) else []:
        strategy = _clean_text(_as_dict(risk).get("director_strategy"))
        if strategy:
            lines.append(f"- {strategy}")
    return lines


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
def render_shoot_script_body(context: dict[str, Any]) -> str:
    """渲染分镜预览用的拍摄剧本正文。

    ``context`` 为 ``shoot_script_result_for_prompt`` 的输出（含 6 个 shots）。
    """
    context = _as_dict(context)
    gd = _as_dict(context.get("global_directing"))
    shots = [s for s in (context.get("shots") or []) if isinstance(s, dict)]

    sections: list[str] = []

    directing = _block(_global_directing_lines(gd))
    if directing:
        sections.append("【全局导演】\n" + directing)

    risk_lines = _styling_risk_lines(context.get("styling_risks"))
    if risk_lines:
        sections.append("【搭配风险处理】\n" + "\n".join(risk_lines))

    accessory_lines = _resolved_accessory_lines(context.get("resolved_accessories"))
    if accessory_lines:
        sections.append("【本次配饰】\n" + "\n".join(accessory_lines))

    if shots:
        shot_texts = [_shot_lines(shot) for shot in shots]
        sections.append("【六格镜头】\n" + "\n".join(shot_texts))

    return "\n\n".join(sections)


def render_shot_context_body(context: dict[str, Any]) -> str:
    """渲染最终单张成片用的当前镜头正文。

    ``context`` 为 ``current_shot_context_for_prompt`` 的输出。
    """
    context = _as_dict(context)
    sections: list[str] = []

    cell = context.get("preview_grid_cell")
    if cell is not None:
        sections.append(
            f"【分镜参考】\n参考分镜预览第 {cell} 格；该格本身即 1:1 构图，画幅与本张成片一致，"
            "只借用姿势、身位、景别、构图、背景与光影方向；分镜预览不是模特身份或商品事实来源。"
        )

    selected = _as_dict(_as_dict(context.get("styling")).get("selected_outfit"))
    styling_lines = [
        _kv_line("造型", selected.get("title")),
        _kv_line("穿法", selected.get("wearing_notes")),
        _list_line("选中搭配 code", selected.get("selected_material_codes"), sep="、"),
    ]
    styling_block = _block(styling_lines)
    if styling_block:
        sections.append("【造型】\n" + styling_block)

    directing = _block(_global_directing_lines(_as_dict(context.get("global_directing"))))
    if directing:
        sections.append("【全局导演】\n" + directing)

    current_shot = _as_dict(context.get("current_shot"))
    if current_shot:
        sections.append("【当前镜头】\n" + _shot_lines(current_shot))

    accessory_lines = _resolved_accessory_lines(context.get("resolved_accessories_for_this_shot"))
    if accessory_lines:
        sections.append("【本镜头配饰】\n" + "\n".join(accessory_lines))

    return "\n\n".join(sections)


def render_hero_product_info_line(context: dict[str, Any]) -> str:
    """渲染商品信息为一句话；无内容时返回「（无）」。"""
    text = _clean_text(_as_dict(context).get("info"))
    return text or EMPTY_PLACEHOLDER


def render_user_instruction_line(context: dict[str, Any]) -> str:
    """渲染本步骤用户要求为一句话；无内容时返回「（无）」。"""
    text = _clean_text(_as_dict(context).get("instruction"))
    return text or EMPTY_PLACEHOLDER


def render_user_requirement_line(context: dict[str, Any]) -> str:
    """渲染 AI Stylist 用的用户要求为一句话；无内容时返回「（无）」。

    优先取 ``combined_requirement``（上传要求 + 本次微调合并后），回退 ``upload_requirement``。
    优先级/冲突处理规则不在此渲染——它由 system instruction 与模板正文声明。
    """
    ctx = _as_dict(context)
    text = _clean_text(ctx.get("combined_requirement")) or _clean_text(ctx.get("upload_requirement"))
    return text or EMPTY_PLACEHOLDER


def render_model_identity_line(context: dict[str, Any]) -> str:
    """把模特身份契约渲染成中文自然语言块（替代英文 JSON dump）。

    ``context`` 为 ``model_identity_context_for_prompt`` 的输出。身份事实以模特参考图
    和 ``identity_contract`` 为准；具体身份边界话术由可编辑模板正文（Prompt 维护）声明。
    """
    ctx = _as_dict(context)
    contract = _as_dict(ctx.get("identity_contract"))
    lines: list[str] = []

    model_id = _clean_text(contract.get("model_id"))
    name = _clean_text(contract.get("name"))
    if model_id:
        lines.append(f"数字模特身份：{model_id}" + (f"（{name}）" if name else ""))

    notes = _clean_text(contract.get("profile_notes"))
    if notes:
        lines.append(f"模特备注：{notes}")

    forbidden = _clean_list(contract.get("forbidden"))
    if forbidden:
        lines.append("额外禁止：" + "；".join(forbidden))

    angles = _clean_list(
        [_as_dict(img).get("angle") for img in (contract.get("reference_images") or []) if isinstance(img, dict)]
    )
    if angles:
        lines.append("参考图角度：" + "、".join(angles))

    return "\n".join(lines)


def render_image_reference_line(image_inputs: list[dict[str, Any]] | None) -> str:
    """把参考图列表渲染成一句「图N=角色」的顺序说明（数据，不含规则/优先级话术）。

    顺序严格对应 provider ``reference_images`` 的传入顺序（由选图器决定），
    连续同角色合并为「图a-b」。角色 → 中文标签见 ``_REFERENCE_ROLE_LABELS``。
    列表为空返回空串，调用方据此省略整段。
    """
    groups: list[list[Any]] = []  # [role, start, end]
    for index, item in enumerate(image_inputs or [], start=1):
        role = str(_as_dict(item).get("role") or "")
        if groups and groups[-1][0] == role:
            groups[-1][2] = index
        else:
            groups.append([role, index, index])

    parts: list[str] = []
    for role, start, end in groups:
        label = _REFERENCE_ROLE_LABELS.get(role, "辅助参考")
        span = f"图{start}" if start == end else f"图{start}-{end}"
        parts.append(f"{span}={label}")
    return "；".join(parts)
