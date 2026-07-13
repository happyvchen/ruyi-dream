"""分镜预览画布比例单元测试（纯函数，不触碰 DB / 网络 / 对象存储 / 外部 AI）。

覆盖「分镜预览改 3:2 横版、6 个 1:1 方格」的验收：
- 预览阶段 provider 请求不再硬编码方形画布。
- toapis 路径预览用 GPT_IMAGE2_PREVIEW_SIZE（默认 3:2），成片仍用 GPT_IMAGE2_SIZE（1:1）。
- fal 路径预览用显式宽高（3:2），成片用 auto。
- 成片单镜头正文声明「第 N 格即 1:1 构图」。
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _ensure_stub(name: str, **attrs: object) -> None:
    """在缺少可选依赖（如 boto3）的最小环境下注入桩模块，

    使被测纯函数可导入。真实环境已装依赖时不生效。
    """
    import types

    try:
        importlib.import_module(name)
        return
    except Exception:  # noqa: BLE001
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module


_ensure_stub("boto3", client=lambda *a, **k: None)
_ensure_stub("botocore")
_ensure_stub("botocore.client", Config=object)
_ensure_stub("botocore.exceptions", ClientError=type("ClientError", (Exception,), {}))


PREVIEW_PACKAGE = {"generation": {"stage": "preview_grid"}, "prompt": "p", "imageInputs": []}
FINAL_PACKAGE = {"generation": {"stage": "final_image"}, "prompt": "p", "imageInputs": []}

PREVIEW_ENV = {
    "GPT_IMAGE2_SIZE": "1:1",
    "GPT_IMAGE2_PREVIEW_SIZE": "3:2",
    "GPT_IMAGE2_PREVIEW_FAL_IMAGE_SIZE": "1536x1024",
    # 被测的是纯画布尺寸逻辑，本身不碰对象存储；但它经由共享的 get_settings()，
    # 而 get_settings() 会校验整个应用配置（含 S3 凭证）。沿用 test_auth_and_migrations
    # 的约定注入哑元 CI 凭证，让校验通过而不真正触达 S3。
    "APP_S3_ACCESS_KEY_ID": "ci-test-access-key",
    "APP_S3_SECRET_ACCESS_KEY": "ci-test-secret-key",
}


class PreviewCanvasTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, PREVIEW_ENV)
        self._env.start()
        self.config = importlib.reload(importlib.import_module("app.config"))
        self.config.get_settings.cache_clear()
        self.image_provider = importlib.reload(importlib.import_module("app.image_provider"))

    def tearDown(self) -> None:
        self.config.get_settings.cache_clear()
        self._env.stop()
        importlib.reload(importlib.import_module("app.config"))
        importlib.reload(importlib.import_module("app.image_provider"))

    def test_settings_defaults(self) -> None:
        settings = self.config.get_settings()
        self.assertEqual(settings.gpt_image2_preview_size, "3:2")
        self.assertEqual(settings.gpt_image2_preview_fal_image_size, "1536x1024")

    def test_toapis_preview_is_3_2_final_is_1_1(self) -> None:
        preview = self.image_provider.build_toapis_request_snapshot(PREVIEW_PACKAGE, ["u"])
        final = self.image_provider.build_toapis_request_snapshot(FINAL_PACKAGE, ["u"])
        self.assertEqual(preview["size"], "3:2")
        self.assertEqual(final["size"], "1:1")

    def test_fal_preview_uses_explicit_landscape_size(self) -> None:
        preview = self.image_provider.build_fal_request_snapshot(PREVIEW_PACKAGE, ["u"])
        final = self.image_provider.build_fal_request_snapshot(FINAL_PACKAGE, ["u"])
        self.assertEqual(preview["image_size"], {"width": 1536, "height": 1024})
        self.assertEqual(final["image_size"], "auto")
        # 3:2 横版：宽 > 高，且每格（3 列 × 2 行）为 1:1。
        self.assertGreater(preview["image_size"]["width"], preview["image_size"]["height"])
        self.assertEqual(preview["image_size"]["width"] / 3, preview["image_size"]["height"] / 2)

    def test_fal_preview_size_parser_fallback(self) -> None:
        parse = self.image_provider._fal_preview_image_size
        self.assertEqual(parse(mock.Mock(gpt_image2_preview_fal_image_size="800x600")), {"width": 800, "height": 600})
        self.assertEqual(parse(mock.Mock(gpt_image2_preview_fal_image_size="")), "square_hd")

    def test_final_shot_context_cell_is_number_only(self) -> None:
        # 代码只输出「会变的格号」；“第 N 格即 1:1 构图/只借用姿势”等固定话术
        # 已移入三类 assembly.final_image_* 的 Prompt 维护模板正文。
        main = importlib.reload(importlib.import_module("app.main"))
        runtime = main.build_final_prompt_runtime(
            3,
            "bottom_grid_3",
            {
                "hero_understanding": {"category": "裤装"},
                "selected_material_codes": ["LOOK-A"],
                "outfit_candidates": [{"outfit_key": "A", "selected_material_ids": [], "wearing_notes": "轻量通勤"}],
                "selected_outfit_key": "A",
            },
            [],
            {},
            {},
        )
        self.assertEqual(runtime["preview_grid_cell"], "3")
        self.assertNotIn("1:1 构图", runtime["preview_grid_cell"])


if __name__ == "__main__":
    unittest.main()
