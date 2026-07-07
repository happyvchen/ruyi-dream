"""prompt_render 渲染层单元测试（纯函数，不触碰 DB / 网络 / 对象存储）。

覆盖设计文档 design/01_generation_flow/prompt_rendering_refactor.md 的验收第一道：
- 输出不含治理元数据（schema_version / priority_policy / cannot_override / priority_rules / conflict_handling）。
- 输出不含 JSON 语法噪音（大括号）。
- §4A「不丢」的导演字段（机位/取景范围/占比/构图等）全部出现在输出里。
- 空值优雅省略，用户信息为空时返回「（无）」。
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
    render_image_reference_line,
    render_model_identity_line,
    render_shoot_script_body,
    render_shot_context_body,
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
        # 治理噪音：应被丢弃
        "schema_version": "shot-vX",
    }


def _sample_shoot_script_context() -> dict:
    return {
        "schema_version": "shoot-script-storyboard-context-v1",
        "global_directing": {
            "brand_tone": "轻松高级、智性优雅",
            "background": "浅灰白纯色棚拍背景",
            "lighting": "柔光箱低反差",
            "model_direction": "从容自信",
            "hair_makeup_boundary": "以模特参考图为唯一身份源",
            "accessory_boundary": "克制精致不抢镜",
            "consistency_rules": ["面部一致", "印花一致"],
            "product_fidelity_rules": ["V领工艺清晰"],
            "negative_constraints": ["避免职业装沉闷"],
        },
        "styling_risks": [
            {
                "director_strategy": "放弃盘发，保留短发 Bob 塞耳后",
                "cannot_override": ["模特身份"],
            }
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


class RenderShootScriptBodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.out = render_shoot_script_body(_sample_shoot_script_context())

    def test_no_governance_metadata(self) -> None:
        for key in GOVERNANCE_KEYS:
            self.assertNotIn(key, self.out, f"治理元数据 {key} 泄漏进 prompt")

    def test_no_json_braces(self) -> None:
        self.assertNotIn("{", self.out)
        self.assertNotIn("}", self.out)

    def test_all_six_shots_present(self) -> None:
        for i in range(1, 7):
            self.assertIn(f"格{i}", self.out)

    def test_directing_fields_preserved(self) -> None:
        # §4A「不丢」字段：机位/取景范围/占比/构图 必须出现
        for token in ["平视", "头顶至脚底", "占比约85%", "居中留白", "核心焦点1"]:
            self.assertIn(token, self.out, f"导演字段 {token} 丢失")

    def test_risk_and_accessory_rendered(self) -> None:
        self.assertIn("短发 Bob", self.out)
        self.assertIn("燕麦色细带凉鞋", self.out)

    def test_static_movement_omitted(self) -> None:
        self.assertNotIn("动势", self.out)


class RenderShotContextBodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.out = render_shot_context_body(_sample_shot_context())

    def test_no_governance_metadata(self) -> None:
        for key in GOVERNANCE_KEYS:
            self.assertNotIn(key, self.out)

    def test_no_json_braces(self) -> None:
        self.assertNotIn("{", self.out)
        self.assertNotIn("}", self.out)

    def test_preview_cell_and_styling(self) -> None:
        self.assertIn("第 5 格", self.out)
        self.assertIn("吊带裙配同色飘带", self.out)
        self.assertIn("LOOK-A", self.out)

    def test_current_shot_directing_fields(self) -> None:
        for token in ["平视", "头顶至脚底", "居中留白"]:
            self.assertIn(token, self.out)


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
        # combined 为空时回退 upload_requirement
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


class RenderImageReferenceLineTests(unittest.TestCase):
    def _inputs(self, *roles: str) -> list[dict]:
        return [{"role": r, "objectKey": f"k/{i}"} for i, r in enumerate(roles)]

    def test_preview_stage_grouping(self) -> None:
        out = render_image_reference_line(
            self._inputs("model_identity", "model_identity", "hero_garment", "hero_garment", "hero_garment")
        )
        self.assertEqual(out, "图1-2=数字模特身份；图3-5=主推款")

    def test_final_stage_with_preview_grid(self) -> None:
        out = render_image_reference_line(
            self._inputs("model_identity", "model_identity", "preview_grid", "hero_garment")
        )
        self.assertEqual(out, "图1-2=数字模特身份；图3=分镜预览（构图参考）；图4=主推款")

    def test_styling_garment_and_unknown(self) -> None:
        out = render_image_reference_line(self._inputs("styling_garment", "mystery"))
        self.assertEqual(out, "图1=搭配服装；图2=辅助参考")

    def test_empty(self) -> None:
        self.assertEqual(render_image_reference_line([]), "")
        self.assertEqual(render_image_reference_line(None), "")

    def test_no_json_or_governance(self) -> None:
        out = render_image_reference_line(self._inputs("model_identity", "hero_garment"))
        self.assertNotIn("{", out)
        self.assertNotIn("reference_index", out)
        self.assertNotIn("schema_version", out)


class RenderModelIdentityLineTests(unittest.TestCase):
    def _context(self, **overrides) -> dict:
        contract = {
            "model_id": "Face-01",
            "name": "默认模特",
            "fact_source": "model_identity_reference_images",
            "must_preserve": ["same person identity", "face shape"],
            "must_not_change": ["do not invent a different face"],
            "profile_notes": "冷静克制气质",
            "forbidden": ["低马尾", "浓妆"],
            "reference_images": [
                {"role": "model_identity", "angle": "front", "objectKey": "k/1"},
                {"role": "model_identity", "angle": "left45", "objectKey": "k/2"},
            ],
        }
        contract.update(overrides)
        return {
            "schema_version": "model-identity-context-v1",
            "identity_contract": contract,
            "hair_makeup_boundary": "以数字模特参考图为唯一模特身份事实源；保持同一人的脸型、五官、头型、发长、发型轮廓、刘海/碎发形态、肤色和气质。",
        }

    def setUp(self) -> None:
        self.out = render_model_identity_line(self._context())

    def test_no_json_or_english_arrays(self) -> None:
        self.assertNotIn("{", self.out)
        self.assertNotIn("must_preserve", self.out)
        self.assertNotIn("schema_version", self.out)
        self.assertNotIn("objectKey", self.out)

    def test_core_content_present(self) -> None:
        self.assertIn("Face-01", self.out)
        self.assertIn("默认模特", self.out)
        self.assertIn("发型轮廓", self.out)  # from hair_makeup_boundary
        self.assertIn("冷静克制气质", self.out)  # profile_notes
        self.assertIn("低马尾", self.out)  # forbidden
        self.assertIn("front", self.out)  # reference angle

    def test_empty_context(self) -> None:
        self.assertEqual(render_model_identity_line({}), "")

    def test_partial_context_omits_missing(self) -> None:
        out = render_model_identity_line(
            {"identity_contract": {"model_id": "Face-02"}, "hair_makeup_boundary": ""}
        )
        self.assertIn("Face-02", out)
        self.assertNotIn("模特备注", out)
        self.assertNotIn("额外禁止", out)


if __name__ == "__main__":
    unittest.main()
