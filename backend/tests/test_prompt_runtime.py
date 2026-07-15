"""Prompt runtime 组装测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.main import (  # noqa: E402
    build_final_prompt_runtime,
    build_preview_prompt_runtime,
    build_shot_styling_line,
    build_stage_user_instruction_context,
)


def sample_styling_plan() -> dict:
    return {
        "hero_understanding": {"shot_category_family": "top"},
        "selected_material_codes": ["LOOK-A"],
        "selected_outfit_key": "outfit-1",
        "outfit_candidates": [
            {
                "outfit_key": "outfit-1",
                "wearing_notes": "内搭衬衫\n半扎进裤腰。",
                "generated_accessories": ["极简耳圈", "低存在感手袋"],
            }
        ],
    }


class PromptRuntimeTests(unittest.TestCase):
    def test_shot_styling_is_single_line_with_chinese_separator(self) -> None:
        line = build_shot_styling_line(sample_styling_plan())

        self.assertNotIn("\n", line)
        self.assertIn("；", line)
        self.assertIn("选中搭配服装：LOOK-A", line)
        self.assertIn("内搭衬衫 半扎进裤腰", line)
        self.assertIn("生成类配饰：极简耳圈、低存在感手袋", line)

    def test_preview_runtime_contains_user_requirement_and_stage_instruction(self) -> None:
        runtime = build_preview_prompt_runtime(
            sample_styling_plan(),
            [{"role": "model_identity"}, {"role": "hero_garment"}],
            build_stage_user_instruction_context("storyboard_preview", "姿态更舒展"),
            {"upload_requirement": "重点展示腰线"},
        )

        self.assertEqual(runtime["user_requirement"], "重点展示腰线")
        self.assertEqual(runtime["storyboard_preview_user_instruction"], "姿态更舒展")
        self.assertIn("shot_styling", runtime)

    def test_final_runtime_contains_user_requirement_and_stage_instruction(self) -> None:
        runtime = build_final_prompt_runtime(
            1,
            "hero_full_body",
            sample_styling_plan(),
            [{"role": "preview_grid"}, {"role": "hero_garment"}],
            build_stage_user_instruction_context("final_image", "画面更干净"),
            {"upload_requirement": "重点展示腰线"},
        )

        self.assertEqual(runtime["user_requirement"], "重点展示腰线")
        self.assertEqual(runtime["final_image_user_instruction"], "画面更干净")
        self.assertIn("shot_styling", runtime)
        self.assertNotIn("shot_accessories", runtime)


if __name__ == "__main__":
    unittest.main()
