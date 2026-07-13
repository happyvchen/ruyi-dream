"""prompt_render 单元测试。

当前渲染层只负责动态占位文本和参考图图号区间；固定摄影规则由 Prompt 维护模板正文托管。
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
    render_user_instruction_line,
    render_user_requirement_line,
)


class RenderUserLineTests(unittest.TestCase):
    def test_hero_info_present(self) -> None:
        self.assertEqual(render_hero_product_info_line({"info": "夏季主推裙装"}), "夏季主推裙装")

    def test_hero_info_empty(self) -> None:
        self.assertEqual(render_hero_product_info_line({"info": ""}), "")
        self.assertEqual(render_hero_product_info_line({}), "")

    def test_instruction_present_and_empty(self) -> None:
        self.assertEqual(render_user_instruction_line({"instruction": "姿态更舒展"}), "姿态更舒展")
        self.assertEqual(render_user_instruction_line({"instruction": ""}), "")

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
        self.assertEqual(render_user_requirement_line({}), "")

    def test_user_requirement_drops_structured_noise(self) -> None:
        out = render_user_requirement_line(
            {"combined_requirement": "重点腰线", "priority_policy": {"cannot_override": ["商品事实"]}}
        )
        self.assertEqual(out, "重点腰线")
        self.assertNotIn("cannot_override", out)
        self.assertNotIn("{", out)


class RenderImageReferenceSpansTests(unittest.TestCase):
    def _inputs(self, *roles: str) -> list[dict]:
        return [{"role": role, "objectKey": f"k/{index}"} for index, role in enumerate(roles)]

    def test_preview_stage_spans(self) -> None:
        spans = render_image_reference_spans(
            self._inputs(
                "model_identity",
                "model_identity",
                "hero_garment",
                "hero_garment",
                "hero_garment",
                "styling_garment",
                "shoot_environment",
            )
        )
        self.assertEqual(spans["model_identity"], "图1-2")
        self.assertEqual(spans["garment"], "图3-6")
        self.assertEqual(spans["shoot_environment"], "图7")
        self.assertEqual(spans["preview_grid"], "")

    def test_final_stage_spans_with_preview_grid(self) -> None:
        spans = render_image_reference_spans(
            self._inputs(
                "model_identity",
                "model_identity",
                "preview_grid",
                "hero_garment",
                "hero_garment",
                "styling_garment",
            )
        )
        self.assertEqual(spans["model_identity"], "图1-2")
        self.assertEqual(spans["preview_grid"], "图3")
        self.assertEqual(spans["garment"], "图4-6")
        self.assertEqual(spans["shoot_environment"], "")

    def test_single_image_role(self) -> None:
        spans = render_image_reference_spans(self._inputs("model_identity"))
        self.assertEqual(spans["model_identity"], "图1")

    def test_empty(self) -> None:
        spans = render_image_reference_spans([])
        self.assertEqual(spans["model_identity"], "")
        self.assertEqual(render_image_reference_spans(None)["garment"], "")


if __name__ == "__main__":
    unittest.main()
