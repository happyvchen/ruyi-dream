"""prompt_render 渲染层单元测试（纯函数，不触碰 DB / 网络 / 对象存储）。

重构后边界（见 design/01_generation_flow/prompt_rendering_refactor.md）：
- 渲染层只输出「可变数据正文」，不带表头（【全局导演】等）、不含规则/事实源话术、不含治理元数据。
- 表头与固定话术一律由「Prompt 维护」页模板正文托管（配合条件段），代码不再拼接。
- 参考图只输出「会变的图号区间」，职能描述由模板正文托管。
- 空数据优雅省略（返回空串），模板据此用条件段省略整段。
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.prompt_render import (  # noqa: E402
    render_hero_product_info_line,
    render_image_reference_spans,
    render_preview_grid_cell,
    render_shoot_script_global_directing,
    render_shoot_script_resolved_accessories,
    render_shoot_script_shots,
    render_shoot_script_styling_risks,
    render_shot_accessories,
    render_shot_current,
    render_shot_global_directing,
    render_shot_styling,
    render_user_instruction_line,
    render_user_requirement_line,
)

GOVERNANCE_KEYS = [
    "schema_version",
    "schemaVersion",
    "priority_policy",
    "cannot_override",
    "priority_rules",
    "conflict_handling",
]

# 表头与规则话术不得再由渲染层输出（它们归 Prompt 维护模板正文）。
FORBIDDEN_TEMPLATE_TOKENS = [
    "【全局导演】",
    "【搭配风险处理】",
    "【本次配饰】",
    "【六格镜头】",
    "【分镜参考】",
    "【造型】",
    "【当前镜头】",
    "【本镜头配饰】",
    "分镜预览不是模特身份或商品事实来源",
    "只借用姿势",
]


def _sample_shot(idx: int) -> dict:
    return {
        "shot_index": idx,
        "shot_role": f"role_{idx}",
        "shot_role_label": f"标签{idx}",
        "framing": "全景",
        "crop_range": "头顶至脚底",
        "model_scale": "占比约85%",
        "camera_angle": "平视",
        "body_position": "3/4 斜朝镜头",
        "pose": "单腿承重",
        "movement": "静态",
        "main_focus": f"核心焦点{idx}",
        "visible_garments": ["hero"],
        "forbidden_occlusion_zones": ["前胸V领"],
        "composition_notes": "居中留白",
        "accessory_instructions": ["金色小耳圈"],
        "negative_constraints": ["禁止僵硬立正"],
        "schema_version": "shot-vX",  # 治理噪音：应被丢弃
    }


def _sample_shoot_script_context() -> dict:
    return {
        "schema_version": "shoot-script-storyboard-context-v1",
        "global_directing": {
            "accessory_boundary": "克制精致不抢镜",
            "consistency_rules": ["面部一致", "印花一致"],
            "negative_constraints": ["避免职业装沉闷"],
        },
        "styling_risks": [
            {"director_strategy": "放弃盘发，保留短发 Bob 塞耳后", "cannot_override": ["模特身份"]}
        ],
        "resolved_accessories": [
            {"item_type": "footwear", "final_direction": "燕麦色细带凉鞋", "applies_to_shots": [1, 4]},
        ],
        "shots": [_sample_shot(i) for i in range(1, 7)],
    }


def _sample_shot_context() -> dict:
    return {
        "schema_version": "final-image-shot-context-v1",
        "preview_grid_cell": 5,
        "styling": {
            "hero_understanding": {"category": "连衣裙"},
            "selected_outfit": {
                "title": "吊带裙配同色飘带",
                "wearing_notes": "单穿吊带裙",
                "selected_material_codes": ["LOOK-A", "LOOK-B"],
                "priority_policy": {"cannot_override": ["商品事实"]},
            },
        },
        "global_directing": _sample_shoot_script_context()["global_directing"],
        "current_shot": _sample_shot(5),
        "resolved_accessories_for_this_shot": [
            {"item_type": "jewelry", "final_direction": "极简金色小耳圈"},
        ],
    }


class ShootScriptSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = _sample_shoot_script_context()
        self.parts = {
            "gd": render_shoot_script_global_directing(self.ctx),
            "risks": render_shoot_script_styling_risks(self.ctx),
            "acc": render_shoot_script_resolved_accessories(self.ctx),
            "shots": render_shoot_script_shots(self.ctx),
        }

    def test_no_headers_or_rule_talk(self) -> None:
        for name, text in self.parts.items():
            for token in FORBIDDEN_TEMPLATE_TOKENS:
                self.assertNotIn(token, text, f"{name} 泄漏模板话术 {token}")

    def test_no_governance_or_braces(self) -> None:
        for text in self.parts.values():
            self.assertNotIn("{", text)
            for key in GOVERNANCE_KEYS:
                self.assertNotIn(key, text)

    def test_bodies_content(self) -> None:
        self.assertIn("配饰边界：克制精致不抢镜", self.parts["gd"])
        self.assertIn("短发 Bob", self.parts["risks"])
        self.assertIn("燕麦色细带凉鞋", self.parts["acc"])
        for i in range(1, 7):
            self.assertIn(f"格{i}", self.parts["shots"])
        for token in ["平视", "头顶至脚底", "占比约85%", "居中留白", "核心焦点1"]:
            self.assertIn(token, self.parts["shots"])

    def test_empty_sections_return_empty_string(self) -> None:
        empty = {"global_directing": {}, "shots": []}
        self.assertEqual(render_shoot_script_styling_risks(empty), "")
        self.assertEqual(render_shoot_script_resolved_accessories(empty), "")
        self.assertEqual(render_shoot_script_global_directing(empty), "")
        self.assertEqual(render_shoot_script_shots(empty), "")


class ShotContextSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = _sample_shot_context()

    def test_preview_cell_number_only(self) -> None:
        self.assertEqual(render_preview_grid_cell(self.ctx), "5")
        self.assertEqual(render_preview_grid_cell({}), "")
        # 关键：分镜参考的规则话术已移出代码，渲染层只出格号数字。
        self.assertNotIn("借用", render_preview_grid_cell(self.ctx))

    def test_styling_and_shot_bodies(self) -> None:
        styling = render_shot_styling(self.ctx)
        self.assertIn("吊带裙配同色飘带", styling)
        self.assertIn("LOOK-A", styling)
        current = render_shot_current(self.ctx)
        for token in ["平视", "头顶至脚底", "居中留白"]:
            self.assertIn(token, current)
        self.assertIn("极简金色小耳圈", render_shot_accessories(self.ctx))

    def test_no_headers_or_governance(self) -> None:
        for text in (
            render_shot_styling(self.ctx),
            render_shot_global_directing(self.ctx),
            render_shot_current(self.ctx),
            render_shot_accessories(self.ctx),
        ):
            self.assertNotIn("{", text)
            for token in FORBIDDEN_TEMPLATE_TOKENS:
                self.assertNotIn(token, text)
            for key in GOVERNANCE_KEYS:
                self.assertNotIn(key, text)


class RenderUserLineTests(unittest.TestCase):
    def test_hero_info_present(self) -> None:
        self.assertEqual(render_hero_product_info_line({"info": "夏季主推裙装"}), "夏季主推裙装")

    def test_hero_info_empty(self) -> None:
        self.assertEqual(render_hero_product_info_line({"info": ""}), "（无）")
        self.assertEqual(render_hero_product_info_line({}), "（无）")

    def test_instruction_present_and_empty(self) -> None:
        self.assertEqual(render_user_instruction_line({"instruction": "姿态更舒展"}), "姿态更舒展")
        self.assertEqual(render_user_instruction_line({"instruction": ""}), "（无）")

    def test_double_period_cleaned(self) -> None:
        self.assertEqual(render_hero_product_info_line({"info": "重点展示腰线。。"}), "重点展示腰线。")

    def test_user_requirement_combined_and_fallback(self) -> None:
        self.assertEqual(
            render_user_requirement_line({"combined_requirement": "重点腰线；避免厚外套"}),
            "重点腰线；避免厚外套",
        )
        self.assertEqual(
            render_user_requirement_line({"combined_requirement": "", "upload_requirement": "夏季主推"}),
            "夏季主推",
        )
        self.assertEqual(render_user_requirement_line({}), "（无）")

    def test_user_requirement_drops_governance(self) -> None:
        out = render_user_requirement_line(
            {"combined_requirement": "重点腰线", "priority_policy": {"cannot_override": ["商品事实"]}}
        )
        self.assertEqual(out, "重点腰线")
        self.assertNotIn("cannot_override", out)
        self.assertNotIn("{", out)


class RenderImageReferenceSpansTests(unittest.TestCase):
    def _inputs(self, *roles: str) -> list[dict]:
        return [{"role": r, "objectKey": f"k/{i}"} for i, r in enumerate(roles)]

    def test_preview_stage_spans(self) -> None:
        spans = render_image_reference_spans(
            self._inputs(
                "model_identity", "model_identity",
                "hero_garment", "hero_garment", "hero_garment",
                "styling_garment", "shoot_environment",
            )
        )
        self.assertEqual(spans["model_identity"], "图1-2")
        self.assertEqual(spans["hero_garment"], "图3-5")
        self.assertEqual(spans["styling_garment"], "图6")
        self.assertEqual(spans["shoot_environment"], "图7")
        self.assertEqual(spans["preview_grid"], "")
        # subject = 模特+主推款+搭配服装 的合并区间，供模板交叉引用（不含环境图）。
        self.assertEqual(spans["subject"], "图1-6")

    def test_final_stage_spans_with_preview_grid(self) -> None:
        spans = render_image_reference_spans(
            self._inputs(
                "model_identity", "model_identity",
                "preview_grid",
                "hero_garment", "hero_garment",
                "styling_garment",
            )
        )
        self.assertEqual(spans["model_identity"], "图1-2")
        self.assertEqual(spans["preview_grid"], "图3")
        self.assertEqual(spans["hero_garment"], "图4-5")
        self.assertEqual(spans["styling_garment"], "图6")
        self.assertEqual(spans["shoot_environment"], "")

    def test_single_image_role(self) -> None:
        spans = render_image_reference_spans(self._inputs("model_identity"))
        self.assertEqual(spans["model_identity"], "图1")

    def test_empty(self) -> None:
        spans = render_image_reference_spans([])
        self.assertEqual(spans["model_identity"], "")
        self.assertEqual(spans["subject"], "")
        self.assertEqual(render_image_reference_spans(None)["hero_garment"], "")

    def test_only_numbers_no_role_labels(self) -> None:
        # 关键：渲染层不再输出「数字模特身份/主推款」等职能标签（那些归模板正文）。
        spans = render_image_reference_spans(self._inputs("model_identity", "hero_garment"))
        blob = "".join(spans.values())
        self.assertNotIn("数字模特身份", blob)
        self.assertNotIn("主推款", blob)
        self.assertNotIn("{", blob)


if __name__ == "__main__":
    unittest.main()
