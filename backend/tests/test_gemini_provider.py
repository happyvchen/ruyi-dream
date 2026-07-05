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
        # 先恢复原始环境,再按恢复后的环境重载模块,避免向同进程后续测试泄漏空值。
        os.environ.clear()
        os.environ.update(self._old_env)
        config = importlib.reload(importlib.import_module("app.config"))
        config.get_settings.cache_clear()
        importlib.reload(importlib.import_module("app.gemini_provider"))

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
        self.assertTrue(payload["stream"])  # 必须流式,避免 Cloudflare 100s 超时(HTTP 524)
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

    def test_build_toapis_chat_payload_requests_usage_in_stream(self) -> None:
        # include_usage 用于截断检测与失败诊断;max_tokens/response_format 实测被
        # ToAPIs 忽略,不应出现在 payload 中制造虚假安全感。
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        payload = gp.build_toapis_chat_payload("hi", [], None)
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("response_format", payload)

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

    # ---- SSE 流式组装 ----

    def test_assemble_stream_text_joins_deltas(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        lines = [
            'data: {"choices":[{"delta":{"role":"assistant"}}]}',
            'data: {"choices":[{"delta":{"content":"{\\"a\\":"}}]}',
            "",
            ": keep-alive comment",
            'data: {"choices":[{"delta":{"content":" 1}"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        self.assertEqual(gp.assemble_toapis_stream_text(lines), '{"a": 1}')

    def test_assemble_stream_text_raises_on_error_chunk(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        lines = ['data: {"error":{"message":"quota exceeded"}}']
        with self.assertRaises(RuntimeError) as ctx:
            gp.assemble_toapis_stream_text(lines)
        self.assertIn("quota exceeded", str(ctx.exception))

    def test_assemble_stream_text_raises_on_empty(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        with self.assertRaises(RuntimeError):
            gp.assemble_toapis_stream_text(["data: [DONE]"])

    def test_assemble_stream_text_tolerates_usage_only_chunk(self) -> None:
        # include_usage 时最后一个数据块 choices 为空、只带 usage,不能影响正文组装。
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        lines = [
            'data: {"choices":[{"delta":{"content":"{\\"a\\": 1}"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"completion_tokens":3,"prompt_tokens":7}}',
            "data: [DONE]",
        ]
        self.assertEqual(gp.assemble_toapis_stream_text(lines), '{"a": 1}')

    def test_assemble_stream_text_raises_explicit_error_on_length_truncation(self) -> None:
        # finish_reason=length 意味着正文被输出上限截断,必须显式失败,
        # 不能把不完整文本交给 JSON 解析产生费解的语法错误。
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"想一想"}}]}',
            'data: {"choices":[{"delta":{"content":"{"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
            'data: {"choices":[],"usage":{"completion_tokens":3698}}',
            "data: [DONE]",
        ]
        with self.assertRaises(RuntimeError) as ctx:
            gp.assemble_toapis_stream_text(lines)
        message = str(ctx.exception)
        self.assertIn("finish_reason=length", message)
        self.assertIn("3698", message)

    def test_iter_sse_lines_survives_utf8_bytes_on_unicode_line_boundaries(self) -> None:
        # 根因回归(2026-07-05)：中文 UTF-8 续字节命中 0x85(NEL) 等 Unicode 行界时,
        # iter_lines(decode_unicode=True)+ISO-8859-1 回退会把 data: 行打碎;
        # iter_sse_lines 必须按 b"\n" 切行并整行 UTF-8 解码,内容一字不差。
        import io
        import json as jsonlib

        import requests
        import urllib3

        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        # "展" 的 UTF-8 编码为 e5 b1 95,含 0x95;样本中多字覆盖 0x85 等行界字节。
        deltas = ['{"summary": "展现轻松高级', '感，强调腰线利落。"}']
        body = b""
        for delta in deltas:
            chunk = {"choices": [{"delta": {"content": delta}}]}
            body += b"data: " + jsonlib.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\r\n\r\n"
        body += b"data: [DONE]\n"
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "text/event-stream"  # 无 charset,复现 ISO-8859-1 回退条件
        response.raw = urllib3.HTTPResponse(body=io.BytesIO(body), preload_content=False)

        text = gp.assemble_toapis_stream_text(gp.iter_sse_lines(response))
        self.assertEqual(text, "".join(deltas))

    # ---- 上游错误摘要 ----

    def test_summarize_http_error_extracts_html_title(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        html = "<!DOCTYPE html><html><head><title>toapis.com | 524: A timeout occurred</title></head><body>" + "x" * 2000 + "</body></html>"
        message = gp.summarize_upstream_http_error(524, html)
        self.assertIn("HTTP 524", message)
        self.assertIn("上游网关错误或超时", message)
        self.assertIn("524: A timeout occurred", message)
        self.assertNotIn("<", message)
        self.assertLess(len(message), 400)

    def test_summarize_http_error_plain_body_truncated(self) -> None:
        gp = self._reload(GEMINI_PROVIDER="toapis", TOAPIS_API_KEY="tk")
        message = gp.summarize_upstream_http_error(429, "quota exceeded " * 50)
        self.assertIn("HTTP 429", message)
        self.assertIn("限流或额度不足", message)
        self.assertLess(len(message), 400)

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
            return '```json\n{"ok": 1}\n```'

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
