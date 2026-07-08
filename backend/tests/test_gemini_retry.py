"""Gemini(ToAPIs 通道) 零产出安全重试单测（不联网、不碰 DB/对象存储）。

用桩替换底层 _post_toapis_chat，断言：
- 零产出瞬时错误(GATEWAY_5XX/CONN_RESET/RATE_LIMITED)才重发整个请求，且最终成功；
- 达到重试上限后抛出；
- MALFORMED_OUTPUT(截断/畸形)绝不重试（避免重复计费）；
- AUTH/BALANCE 绝不重试；
- 退避 sleep 被调用（patch time.sleep 断言，测试不真实等待）；
- 429 尊重 Retry-After。
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class GeminiRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = dict(os.environ)
        self.gp = self._reload(
            GEMINI_PROVIDER="toapis",
            TOAPIS_API_KEY="tk",
            GEMINI_RETRY_LIMIT="3",
            GEMINI_RETRY_BACKOFF_BASE_SECONDS="2",
            GEMINI_RETRY_BACKOFF_CAP_SECONDS="30",
            GEMINI_WAF_RETRY_LIMIT="2",
        )

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        config = importlib.reload(importlib.import_module("app.config"))
        config.get_settings.cache_clear()
        importlib.reload(importlib.import_module("app.gemini_provider"))

    def _reload(self, **env: str):
        for key in [
            "GEMINI_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_TOAPIS_MODEL",
            "GEMINI_TOAPIS_TIMEOUT_SECONDS", "GEMINI_TOAPIS_CONNECT_TIMEOUT_SECONDS",
            "TOAPIS_API_KEY", "GEMINI_RETRY_LIMIT", "GEMINI_RETRY_BACKOFF_BASE_SECONDS",
            "GEMINI_RETRY_BACKOFF_CAP_SECONDS", "GEMINI_WAF_RETRY_LIMIT",
        ]:
            os.environ[key] = ""
        os.environ["APP_S3_ACCESS_KEY_ID"] = "unit-dummy-access-key"
        os.environ["APP_S3_SECRET_ACCESS_KEY"] = "unit-dummy-secret-key"
        os.environ["APP_DATA_DIR"] = os.path.join(tempfile.gettempdir(), "ruyi-gemini-retry-unittest")
        os.environ.update(env)
        config = importlib.reload(importlib.import_module("app.config"))
        config.get_settings.cache_clear()
        return importlib.reload(importlib.import_module("app.gemini_provider"))

    def _err(self, code: str, message: str = "boom", retry_after: float | None = None):
        return self.gp.GeminiProviderError(message, code, retry_after=retry_after)

    def test_retries_zero_output_transient_then_succeeds(self) -> None:
        gp = self.gp
        from app.generation_progress import ERROR_GATEWAY_5XX

        calls = {"n": 0}

        def stub(payload):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise self._err(ERROR_GATEWAY_5XX, "502 bad gateway")
            return '{"ok": 1}'

        with mock.patch.object(gp, "_post_toapis_chat", side_effect=stub), \
                mock.patch.object(gp.time, "sleep") as sleep:
            result = gp._post_toapis_chat_with_retry({"m": 1})
        self.assertEqual(result, '{"ok": 1}')
        self.assertEqual(calls["n"], 3)  # 1 次原始 + 2 次重试
        self.assertEqual(sleep.call_count, 2)  # 每次重试前退避一次

    def test_gives_up_after_retry_limit(self) -> None:
        gp = self.gp
        from app.generation_progress import ERROR_CONN_RESET

        def stub(payload):
            raise self._err(ERROR_CONN_RESET, "Max retries exceeded")

        with mock.patch.object(gp, "_post_toapis_chat", side_effect=stub), \
                mock.patch.object(gp.time, "sleep") as sleep:
            with self.assertRaises(gp.GeminiProviderError) as ctx:
                gp._post_toapis_chat_with_retry({"m": 1})
        self.assertEqual(ctx.exception.error_code, ERROR_CONN_RESET)
        # retry_limit=3 → 退避 3 次后第 4 次仍失败即抛出。
        self.assertEqual(sleep.call_count, 3)

    def test_malformed_never_retried(self) -> None:
        gp = self.gp
        from app.generation_progress import ERROR_MALFORMED_OUTPUT

        calls = {"n": 0}

        def stub(payload):
            calls["n"] += 1
            raise self._err(ERROR_MALFORMED_OUTPUT, "finish_reason=length 截断")

        with mock.patch.object(gp, "_post_toapis_chat", side_effect=stub), \
                mock.patch.object(gp.time, "sleep") as sleep:
            with self.assertRaises(gp.GeminiProviderError):
                gp._post_toapis_chat_with_retry({"m": 1})
        self.assertEqual(calls["n"], 1)  # 只调用一次，绝不重发（已产出并计费）
        sleep.assert_not_called()

    def test_auth_and_balance_never_retried(self) -> None:
        gp = self.gp
        from app.generation_progress import ERROR_AUTH_FAILED, ERROR_INSUFFICIENT_BALANCE

        for code in (ERROR_AUTH_FAILED, ERROR_INSUFFICIENT_BALANCE):
            calls = {"n": 0}

            def stub(payload, _code=code):
                calls["n"] += 1
                raise self._err(_code)

            with mock.patch.object(gp, "_post_toapis_chat", side_effect=stub), \
                    mock.patch.object(gp.time, "sleep") as sleep:
                with self.assertRaises(gp.GeminiProviderError):
                    gp._post_toapis_chat_with_retry({"m": 1})
            self.assertEqual(calls["n"], 1, code)
            sleep.assert_not_called()

    def test_waf_limited_retry_then_gives_up(self) -> None:
        gp = self.gp
        from app.generation_progress import ERROR_WAF_BLOCK

        calls = {"n": 0}

        def stub(payload):
            calls["n"] += 1
            raise self._err(ERROR_WAF_BLOCK, "error code: 1010")

        with mock.patch.object(gp, "_post_toapis_chat", side_effect=stub), \
                mock.patch.object(gp.time, "sleep") as sleep:
            with self.assertRaises(gp.GeminiProviderError):
                gp._post_toapis_chat_with_retry({"m": 1})
        # waf_retry_limit=2 → 原始 1 + 重试 2 = 3 次调用。
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleep.call_count, 2)

    def test_rate_limit_respects_retry_after(self) -> None:
        gp = self.gp
        from app.generation_progress import ERROR_RATE_LIMITED

        calls = {"n": 0}

        def stub(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._err(ERROR_RATE_LIMITED, "429 rate limit", retry_after=99.0)
            return "{}"

        with mock.patch.object(gp, "_post_toapis_chat", side_effect=stub), \
                mock.patch.object(gp.time, "sleep") as sleep:
            result = gp._post_toapis_chat_with_retry({"m": 1})
        self.assertEqual(result, "{}")
        # 尊重 Retry-After：等待时间至少 99s（退避取 max(backoff, retry_after)）。
        self.assertEqual(sleep.call_count, 1)
        self.assertGreaterEqual(sleep.call_args[0][0], 99.0)

    def test_cancel_callback_stops_retry(self) -> None:
        gp = self.gp
        from app.generation_progress import ERROR_GATEWAY_5XX

        def stub(payload):
            raise self._err(ERROR_GATEWAY_5XX, "502")

        with mock.patch.object(gp, "_post_toapis_chat", side_effect=stub), \
                mock.patch.object(gp.time, "sleep") as sleep:
            with self.assertRaises(gp.GeminiProviderError):
                gp._post_toapis_chat_with_retry({"m": 1}, should_cancel=lambda: True)
        # 取消回调命中：不进入退避 sleep 即抛出。
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
