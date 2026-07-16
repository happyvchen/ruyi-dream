"""provider 报错分类纯函数单测（不 import fastapi、不联网、不碰 DB/对象存储）。

覆盖 generation_progress.classify_provider_error / is_retryable_error /
is_zero_output_transient / is_balance_error，用方案文档里的真实报错文案作为样本。
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.generation_progress import (  # noqa: E402
    ERROR_AUTH_FAILED,
    ERROR_BUSINESS_REJECT,
    ERROR_CONN_RESET,
    ERROR_GATEWAY_5XX,
    ERROR_INSUFFICIENT_BALANCE,
    ERROR_MALFORMED_OUTPUT,
    ERROR_POLL_TIMEOUT,
    ERROR_PROVIDER_FAILED,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_RATE_LIMITED,
    ERROR_WAF_BLOCK,
    classify_provider_error,
    is_balance_error,
    is_retryable_error,
    is_zero_output_transient,
    normalize_toapis_error,
)


class ClassifyProviderErrorTests(unittest.TestCase):
    def test_gateway_502_bad_gateway(self) -> None:
        self.assertEqual(
            classify_provider_error(status_code=502, body="toapis.com | 502: Bad gateway"),
            ERROR_GATEWAY_5XX,
        )

    def test_gateway_by_keyword_do_request_failed(self) -> None:
        # ToAPIs Error 日志里的 "upstream error: do request failed"（用量 0、未计费）。
        self.assertEqual(
            classify_provider_error(message="upstream error: do request failed"),
            ERROR_GATEWAY_5XX,
        )

    def test_gateway_524_timeout(self) -> None:
        self.assertEqual(
            classify_provider_error(status_code=524, body="524: A timeout occurred"),
            ERROR_GATEWAY_5XX,
        )

    def test_waf_block_1010(self) -> None:
        self.assertEqual(
            classify_provider_error(status_code=403, body="error code: 1010"),
            ERROR_WAF_BLOCK,
        )

    def test_auth_failed_unsupported_channel(self) -> None:
        # "普通用户不支持指定渠道" → 我方指定了无权渠道，改代码去掉即可，重试无用。
        self.assertEqual(
            classify_provider_error(status_code=403, body="普通用户不支持指定渠道"),
            ERROR_AUTH_FAILED,
        )

    def test_auth_failed_401(self) -> None:
        self.assertEqual(classify_provider_error(status_code=401, body="invalid api key"), ERROR_AUTH_FAILED)

    def test_insufficient_balance_negative_credit(self) -> None:
        # "用户额度不足，剩余额度 $ -2.25" → 硬停，不重试。
        self.assertEqual(
            classify_provider_error(body="用户额度不足，剩余额度 $ -2.25"),
            ERROR_INSUFFICIENT_BALANCE,
        )

    def test_insufficient_balance_english(self) -> None:
        self.assertEqual(
            classify_provider_error(status_code=402, body="insufficient_quota: you have run out of credit"),
            ERROR_INSUFFICIENT_BALANCE,
        )

    def test_quota_not_enough_403_is_balance_not_auth(self) -> None:
        self.assertEqual(
            classify_provider_error(status_code=403, body="user quota is not enough"),
            ERROR_INSUFFICIENT_BALANCE,
        )
        self.assertEqual(
            classify_provider_error(status_code=403, provider_error_code="quota_not_enough", body="user quota is not enough"),
            ERROR_INSUFFICIENT_BALANCE,
        )

    def test_normalize_quota_not_enough_403_is_balance(self) -> None:
        normalized = normalize_toapis_error(
            {
                "code": "quota_not_enough",
                "message": "user quota is not enough",
                "data": None,
            },
            status_code=403,
        )
        self.assertEqual(normalized["code"], ERROR_INSUFFICIENT_BALANCE)
        self.assertEqual(normalized["providerCode"], "quota_not_enough")

    def test_balance_beats_rate_limit(self) -> None:
        # 余额类文案即便伴随 429 也必须硬停，不能被限流分支吞成可重试。
        self.assertEqual(
            classify_provider_error(status_code=429, body="余额不足，请充值"),
            ERROR_INSUFFICIENT_BALANCE,
        )

    def test_rate_limited_429(self) -> None:
        self.assertEqual(classify_provider_error(status_code=429, body="rate limit exceeded"), ERROR_RATE_LIMITED)

    def test_conn_reset_max_retries(self) -> None:
        self.assertEqual(
            classify_provider_error(message="HTTPSConnectionPool: Max retries exceeded with url"),
            ERROR_CONN_RESET,
        )

    def test_conn_reset_response_ended_prematurely(self) -> None:
        self.assertEqual(classify_provider_error(message="Response ended prematurely"), ERROR_CONN_RESET)

    def test_conn_reset_ssl_eof(self) -> None:
        self.assertEqual(
            classify_provider_error(message="SSLError: UNEXPECTED_EOF_WHILE_READING"),
            ERROR_CONN_RESET,
        )

    def test_conn_reset_write_timeout(self) -> None:
        # write timeout：请求体未发完，请求未到达 ToAPIs、不计费。
        self.assertEqual(
            classify_provider_error(message="The write operation timed out"),
            ERROR_CONN_RESET,
        )

    def test_malformed_finish_reason_length(self) -> None:
        self.assertEqual(
            classify_provider_error(message="输出被截断(finish_reason=length): completion_tokens=3698"),
            ERROR_MALFORMED_OUTPUT,
        )

    def test_malformed_invalid_json(self) -> None:
        self.assertEqual(
            classify_provider_error(message="Gemini 返回内容不是合法 JSON：Extra data"),
            ERROR_MALFORMED_OUTPUT,
        )

    def test_fallback_provider_failed(self) -> None:
        self.assertEqual(classify_provider_error(message="something totally unexpected"), ERROR_PROVIDER_FAILED)


class RetryPolicyTests(unittest.TestCase):
    def test_is_retryable_true_cases(self) -> None:
        for code in (ERROR_GATEWAY_5XX, ERROR_CONN_RESET, ERROR_RATE_LIMITED, ERROR_WAF_BLOCK,
                     ERROR_PROVIDER_TIMEOUT, ERROR_POLL_TIMEOUT):
            self.assertTrue(is_retryable_error(code), code)

    def test_is_retryable_false_cases(self) -> None:
        for code in (ERROR_AUTH_FAILED, ERROR_INSUFFICIENT_BALANCE, ERROR_BUSINESS_REJECT,
                     ERROR_MALFORMED_OUTPUT, ERROR_PROVIDER_FAILED, None, ""):
            self.assertFalse(is_retryable_error(code), code)

    def test_zero_output_transient_only_gateway_conn_rate(self) -> None:
        self.assertTrue(is_zero_output_transient(ERROR_GATEWAY_5XX))
        self.assertTrue(is_zero_output_transient(ERROR_CONN_RESET))
        self.assertTrue(is_zero_output_transient(ERROR_RATE_LIMITED))

    def test_zero_output_transient_excludes_malformed_and_hard(self) -> None:
        # 关键安全断言：截断/畸形绝不可零产出重发（已产出并计费）；
        # WAF/余额/鉴权也不在零产出重发集合内。
        for code in (ERROR_MALFORMED_OUTPUT, ERROR_WAF_BLOCK, ERROR_INSUFFICIENT_BALANCE,
                     ERROR_AUTH_FAILED, ERROR_BUSINESS_REJECT, ERROR_PROVIDER_TIMEOUT, None):
            self.assertFalse(is_zero_output_transient(code), code)

    def test_is_balance_error(self) -> None:
        self.assertTrue(is_balance_error(ERROR_INSUFFICIENT_BALANCE))
        self.assertFalse(is_balance_error(ERROR_GATEWAY_5XX))
        self.assertFalse(is_balance_error(None))


if __name__ == "__main__":
    unittest.main()
