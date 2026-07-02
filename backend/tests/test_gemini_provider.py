from __future__ import annotations

import asyncio
import base64
import importlib
import os
from pathlib import Path
import sys
import unittest
from unittest import mock


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class GeminiProviderTests(unittest.TestCase):
    """gemini_provider 单元测试：不触碰真实网络、数据库和对象存储。"""

    def setUp(self) -> None:
        self._old_env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        self._reload()

    def _reload(self, **env: str):
        # 显式置空所有相关变量，防止真实 .env 通过 load_dotenv 泄漏进测试。
        for key in [
            "GEMINI_PROVIDER",
            "GEMINI_API_KEY",
            "GEMINI_MODEL",
            "GEMINI_TOAPIS_MODEL",
            "GEMINI_TOAPIS_TIMEOUT_SECONDS",
            "TOAPIS_API_KEY",
        ]:
            os.environ[key] = ""
        os.environ.update(env)
        config = importlib.reload(importlib.import_module("app.config"))
        config.get_settings.cache_clear()
        return importlib.reload(importlib.import_module("app.gemini_provider"))

    # ---- 配置与通道选择 ----

    def test_default_provider_is_direct(self) -> None:
        gp = self._reload()
        self.assertEqual(gp.gemini_channel(), "direct")

    def test_invalid_provider_rejected_by_validate(self) -> None:
        self._reload(
            GEMINI_PROVIDER="bogus",
            APP_ENV="development",
            APP_S3_ACCESS_KEY_ID="ci-test-access-key",
            APP_S3_SECRET_ACCESS_KEY="ci-test-secret-key",
        )
        config = sys.modules["app.config"]
        with self.assertRaises(RuntimeError) as ctx:
            config.Settings().validate()
        self.assertIn("GEMINI_PROVIDER", str(ctx.exception))

    def test_is_configured_direct_requires_gemini_key(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="direct", TOAPIS_API_KEY="tk")
        self.assertFalse(gp.is_gemini_configured())
        gp = self._reload(GEMINI_PROVIDER="direct", GEMINI_API_KEY="gk")
        self.assertTrue(gp.is_gemini_configured())

    def test_is_configured_toapis_requires_toapis_key(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", GEMINI_API_KEY="gk")
        self.assertFalse(gp.is_gemini_configured())
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        self.assertTrue(gp.is_gemini_configured())

    def test_effective_model_falls_back_to_gemini_model(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", GEMINI_MODEL="gemini-2.5-flash", TOAPIS_API_KEY="tk")
        self.assertEqual(gp.effective_gemini_model(), "gemini-2.5-flash")
        gp = self._reload(
            GEMINI_PROVIDER="toapis",
            GEMINI_MODEL="gemini-2.5-flash",
            GEMINI_TOAPIS_MODEL="gemini-3.5-flash",
            TOAPIS_API_KEY="tk",
        )
        self.assertEqual(gp.effective_gemini_model(), "gemini-3.5-flash")
        gp = self._reload(
            GEMINI_PROVIDER="direct",
            GEMINI_MODEL="gemini-2.5-flash",
            GEMINI_TOAPIS_MODEL="gemini-3.5-flash",
            GEMINI_API_KEY="gk",
        )
        self.assertEqual(gp.effective_gemini_model(), "gemini-2.5-flash")

    def test_request_meta_records_channel_and_model(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", GEMINI_MODEL="gemini-2.5-flash", TOAPIS_API_KEY="tk")
        self.assertEqual(
            gp.gemini_request_meta(),
            {"gemini_provider": "toapis", "model": "gemini-2.5-flash"},
        )

    # ---- ToAPIs OpenAI 兼容请求构造 ----

    def test_build_toapis_chat_payload_structure(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", GEMINI_MODEL="gemini-2.5-flash", TOAPIS_API_KEY="tk")
        payload = gp.build_toapis_chat_payload(
            "返回 JSON",
            [{"data": b"\x89PNG-fake", "mime_type": "image/png"}],
            system_instruction="你是造型师",
        )
        self.assertEqual(payload["model"], "gemini-2.5-flash")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "你是造型师"})
        user = payload["messages"][1]
        self.assertEqual(user["role"], "user")
        self.assertEqual(user["content"][0], {"type": "text", "text": "返回 JSON"})
        image_block = user["content"][1]
        self.assertEqual(image_block["type"], "image_url")
        expected_b64 = base64.b64encode(b"\x89PNG-fake").decode("ascii")
        self.assertEqual(image_block["image_url"]["url"], f"data:image/png;base64,{expected_b64}")

    def test_build_toapis_chat_payload_without_system_or_images(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        payload = gp.build_toapis_chat_payload("hi", [], None)
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["role"], "user")

    # ---- ToAPIs 响应解析 ----

    def test_extract_text_from_string_content(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        payload = {"choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}}]}
        self.assertEqual(gp.extract_toapis_chat_text(payload), '{"ok": true}')

    def test_extract_text_from_content_blocks(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        payload = {
            "choices": [
                {"message": {"content": [{"type": "text", "text": '{"a":'}, {"type": "text", "text": " 1}"}]}}
            ]
        }
        self.assertEqual(gp.extract_toapis_chat_text(payload), '{"a": 1}')

    def test_extract_text_rejects_missing_choices_or_empty_content(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        with self.assertRaises(RuntimeError):
            gp.extract_toapis_chat_text({"error": {"message": "boom"}})
        with self.assertRaises(RuntimeError):
            gp.extract_toapis_chat_text({"choices": [{"message": {"content": "   "}}]})

    # ---- 通道分流 ----

    def test_generate_raises_when_not_configured(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis")
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(gp.generate_gemini_json_text("hi"))
        self.assertIn("TOAPIS_API_KEY", str(ctx.exception))
        gp = self._reload(GEMINI_PROVIDER="direct")
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(gp.generate_gemini_json_text("hi"))
        self.assertIn("GEMINI_API_KEY", str(ctx.exception))

    def test_generate_routes_to_toapis_and_returns_text(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", GEMINI_MODEL="gemini-2.5-flash", TOAPIS_API_KEY="tk")
        captured: dict = {}

        def fake_post(payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": '```json\n{"ok": 1}\n```'}}]}

        with mock.patch.object(gp, "_post_toapis_chat", side_effect=fake_post):
            text = asyncio.run(
                gp.generate_gemini_json_text(
                    "给我 JSON",
                    [{"data": b"img", "mime_type": "image/jpeg"}],
                    system_instruction="sys",
                )
            )
        self.assertEqual(text, '```json\n{"ok": 1}\n```')
        self.assertEqual(captured["model"], "gemini-2.5-flash")
        self.assertEqual(captured["messages"][0]["role"], "system")
        self.assertEqual(captured["messages"][1]["content"][1]["type"], "image_url")

    def test_generate_direct_does_not_call_toapis(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="direct", GEMINI_API_KEY="gk")
        with mock.patch.object(gp, "_post_toapis_chat") as post, mock.patch.object(
            gp, "_generate_via_google", mock.AsyncMock(return_value="{}")
        ) as google_call:
            text = asyncio.run(gp.generate_gemini_json_text("hi"))
        self.assertEqual(text, "{}")
        post.assert_not_called()
        google_call.assert_awaited()


if __name__ == "__main__":
    unittest.main()
