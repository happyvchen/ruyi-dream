from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import gemini_provider
from app.generation_progress import (
    ERROR_AUTH_FAILED,
    ERROR_CONN_RESET,
    ERROR_GATEWAY_5XX,
    ERROR_MALFORMED_OUTPUT,
    ERROR_CONTENT_POLICY,
    ERROR_INSUFFICIENT_BALANCE,
    ERROR_PROVIDER_TRANSIENT_UNKNOWN,
    ERROR_RATE_LIMITED,
    classify_provider_error,
)


def test_gemini_rate_limit_failure_waits_and_recovers_without_business_level_resubmit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        gemini_provider="toapis",
        gemini_toapis_model="gemini-test",
        gemini_model="gemini-test",
        toapis_api_key="configured",
        gemini_toapis_timeout_seconds=300,
        gemini_retry_limit=6,
        gemini_retry_backoff_base_seconds=5,
        gemini_retry_backoff_cap_seconds=60,
        gemini_retry_time_budget_seconds=300,
        gemini_waf_retry_limit=2,
    )
    provider_calls = 0
    waits: list[float] = []

    def post_toapis_chat(_payload: dict[str, object], *, read_timeout_seconds: float | None = None) -> str:
        assert read_timeout_seconds is not None and 299 <= read_timeout_seconds <= 300
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls < 3:
            raise gemini_provider.GeminiProviderError(
                "rate_limit_check_failed",
                ERROR_RATE_LIMITED,
            )
        return '{"selected_files": [], "ai_tags": {}}'

    monkeypatch.setattr(gemini_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini_provider, "_post_toapis_chat", post_toapis_chat)
    monkeypatch.setattr(gemini_provider, "_retry_backoff_seconds", lambda attempt, base, cap: float(attempt * 5))
    monkeypatch.setattr(gemini_provider.time, "sleep", waits.append)

    result = asyncio.run(gemini_provider.generate_gemini_json_text("tag this garment"))

    assert result == '{"selected_files": [], "ai_tags": {}}'
    assert provider_calls == 3
    assert waits == [5.0, 10.0]


def test_gemini_connection_failure_waits_and_recovers_with_capped_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        gemini_provider="toapis",
        gemini_toapis_model="gemini-test",
        gemini_model="gemini-test",
        toapis_api_key="configured",
        gemini_toapis_timeout_seconds=300,
        gemini_retry_limit=10,
        gemini_retry_backoff_base_seconds=30,
        gemini_retry_backoff_cap_seconds=240,
        gemini_retry_time_budget_seconds=5400,
        gemini_waf_retry_limit=10,
    )
    provider_calls = 0
    waits: list[float] = []

    def post_toapis_chat(_payload: dict[str, object], *, read_timeout_seconds: float | None = None) -> str:
        assert read_timeout_seconds == 300
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls < 3:
            raise gemini_provider.GeminiProviderError("connection reset", ERROR_CONN_RESET)
        return '{"selected_files": [], "styling_plan": {}}'

    monkeypatch.setattr(gemini_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini_provider, "_post_toapis_chat", post_toapis_chat)
    monkeypatch.setattr(gemini_provider.time, "sleep", waits.append)

    result = asyncio.run(gemini_provider.generate_gemini_json_text("style this garment"))

    assert result == '{"selected_files": [], "styling_plan": {}}'
    assert provider_calls == 3
    assert waits == [30.0, 60.0]


def test_gemini_retry_backoff_caps_at_four_minutes() -> None:
    assert [gemini_provider._retry_backoff_seconds(index, 30, 240) for index in range(1, 7)] == [
        30,
        60,
        120,
        240,
        240,
        240,
    ]


def test_gemini_gateway_failure_uses_unified_retry_and_reports_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        gemini_provider="toapis",
        gemini_toapis_model="gemini-test",
        gemini_model="gemini-test",
        toapis_api_key="configured",
        gemini_toapis_timeout_seconds=300,
        gemini_retry_limit=10,
        gemini_retry_backoff_base_seconds=30,
        gemini_retry_backoff_cap_seconds=240,
        gemini_retry_time_budget_seconds=5400,
        gemini_waf_retry_limit=10,
    )
    calls = 0
    snapshots: list[dict[str, object]] = []

    def post_toapis_chat(_payload: dict[str, object], *, read_timeout_seconds: float | None = None) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise gemini_provider.GeminiProviderError("HTTP 503", ERROR_GATEWAY_5XX)
        return '{}'

    monkeypatch.setattr(gemini_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini_provider, "_post_toapis_chat", post_toapis_chat)
    monkeypatch.setattr(gemini_provider.time, "sleep", lambda _seconds: None)

    result = asyncio.run(
        gemini_provider.generate_gemini_json_text("retry gateway", on_retry=snapshots.append)
    )

    assert result == '{}'
    assert calls == 2
    assert snapshots == [
        {
            "status": "GEMINI_TRANSPORT_RETRY_WAIT",
            "transportRetries": 1,
            "retryLimit": 10,
            "retryDelaySeconds": 30,
            "errorCode": ERROR_GATEWAY_5XX,
            "error": "HTTP 503",
        }
    ]


def test_gemini_unknown_zero_output_provider_error_retries_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        gemini_provider="toapis",
        gemini_toapis_model="gemini-test",
        gemini_model="gemini-test",
        toapis_api_key="configured",
        gemini_toapis_timeout_seconds=300,
        gemini_retry_limit=10,
        gemini_retry_backoff_base_seconds=30,
        gemini_retry_backoff_cap_seconds=240,
        gemini_retry_time_budget_seconds=5400,
        gemini_waf_retry_limit=10,
    )
    calls = 0
    waits: list[float] = []

    def post(_payload: dict[str, object], *, read_timeout_seconds: float | None = None) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise gemini_provider.GeminiProviderError(
                "unknown provider for model gemini-test-low",
                ERROR_PROVIDER_TRANSIENT_UNKNOWN,
            )
        return '{}'

    monkeypatch.setattr(gemini_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini_provider, "_post_toapis_chat", post)
    monkeypatch.setattr(gemini_provider.time, "sleep", waits.append)

    assert asyncio.run(gemini_provider.generate_gemini_json_text("retry route")) == '{}'
    assert calls == 2
    assert waits == [30]


def test_toapis_stream_unknown_error_before_text_is_retryable() -> None:
    lines = [
        'data: {"error":{"code":"edge_route_unavailable","message":"temporary route failure"}}',
        "data: [DONE]",
    ]

    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        gemini_provider.assemble_toapis_stream_text(lines)

    assert caught.value.error_code == ERROR_PROVIDER_TRANSIENT_UNKNOWN


def test_toapis_stream_unknown_provider_route_without_http_status_is_retryable() -> None:
    lines = [
        'data: {"error":{"code":"model_not_found","message":"unknown provider for model gemini-test-low"}}',
        "data: [DONE]",
    ]

    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        gemini_provider.assemble_toapis_stream_text(lines)

    assert caught.value.error_code == ERROR_PROVIDER_TRANSIENT_UNKNOWN


def test_toapis_stream_error_after_text_is_never_blindly_retried() -> None:
    lines = [
        'data: {"choices":[{"delta":{"content":"{\\"partial\\":"}}]}',
        'data: {"error":{"code":"edge_route_unavailable","message":"temporary route failure"}}',
        "data: [DONE]",
    ]

    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        gemini_provider.assemble_toapis_stream_text(lines)

    assert caught.value.error_code == ERROR_MALFORMED_OUTPUT


def test_toapis_stream_error_with_nonzero_usage_in_same_chunk_is_never_blindly_retried() -> None:
    lines = [
        'data: {"error":{"code":"edge_route_unavailable","message":"temporary route failure"},"usage":{"completion_tokens":2}}',
        "data: [DONE]",
    ]

    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        gemini_provider.assemble_toapis_stream_text(lines)

    assert caught.value.error_code == ERROR_MALFORMED_OUTPUT


def test_toapis_non_ok_error_with_nonzero_usage_is_never_blindly_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    payload = {
        "error": {"code": "edge_route_unavailable", "message": "temporary route failure"},
        "usage": {"completion_tokens": 2},
    }

    class FakeResponse:
        ok = False
        status_code = 503
        text = json.dumps(payload)
        headers = {"Content-Type": "application/json"}

        def json(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(
        gemini_provider,
        "get_settings",
        lambda: SimpleNamespace(
            gemini_toapis_timeout_seconds=300,
            gemini_toapis_connect_timeout_seconds=60,
            toapis_api_key="test-key",
            toapis_base_url="https://toapis.example",
        ),
    )
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        gemini_provider._post_toapis_chat({"model": "gemini-test"})

    assert caught.value.error_code == ERROR_MALFORMED_OUTPUT


def test_toapis_stream_error_after_finish_reason_is_never_blindly_retried() -> None:
    lines = [
        'data: {"choices":[{"finish_reason":"stop","delta":{}}]}',
        'data: {"error":{"code":"edge_route_unavailable","message":"temporary route failure"}}',
        "data: [DONE]",
    ]

    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        gemini_provider.assemble_toapis_stream_text(lines)

    assert caught.value.error_code == ERROR_MALFORMED_OUTPUT


def test_toapis_http_200_without_text_is_zero_output_transient() -> None:
    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        gemini_provider.extract_toapis_chat_text({"object": "chat.completion"})

    assert caught.value.error_code == ERROR_PROVIDER_TRANSIENT_UNKNOWN


@pytest.mark.parametrize(
    ("provider_code", "expected"),
    [
        ("unauthorized", ERROR_AUTH_FAILED),
        ("insufficient_quota", ERROR_INSUFFICIENT_BALANCE),
        ("content_policy_violation", ERROR_CONTENT_POLICY),
    ],
)
def test_toapis_http_200_error_envelope_preserves_hard_stop(provider_code: str, expected: str) -> None:
    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        gemini_provider.extract_toapis_chat_text(
            {"error": {"code": provider_code, "message": "provider rejected request"}}
        )

    assert caught.value.error_code == expected


def test_toapis_http_200_unknown_provider_route_is_retryable() -> None:
    payload = {
        "error": {
            "code": "model_not_found",
            "message": "unknown provider for model gemini-test-low",
        }
    }

    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        gemini_provider.extract_toapis_chat_text(payload)

    assert caught.value.error_code == ERROR_PROVIDER_TRANSIENT_UNKNOWN


def test_toapis_http_200_error_with_nonzero_usage_is_never_blindly_retried() -> None:
    payload = {
        "error": {"code": "edge_route_unavailable", "message": "temporary route failure"},
        "usage": {"completion_tokens": 3},
    }

    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        gemini_provider.extract_toapis_chat_text(payload)

    assert caught.value.error_code == ERROR_MALFORMED_OUTPUT


def test_toapis_stream_nonzero_usage_without_text_is_not_blindly_retried() -> None:
    lines = [
        'data: {"usage":{"completion_tokens":12},"choices":[]}',
        "data: [DONE]",
    ]

    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        gemini_provider.assemble_toapis_stream_text(lines)

    assert caught.value.error_code == ERROR_MALFORMED_OUTPUT


def test_toapis_non_stream_nonzero_usage_without_text_is_not_blindly_retried() -> None:
    payload = {
        "choices": [{"finish_reason": "stop", "message": {"content": ""}}],
        "usage": {"completion_tokens": 8},
    }

    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        gemini_provider.extract_toapis_chat_text(payload)

    assert caught.value.error_code == ERROR_MALFORMED_OUTPUT


def test_gemini_deferred_retry_returns_control_without_sleeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        gemini_provider="toapis",
        gemini_toapis_model="gemini-test",
        gemini_model="gemini-test",
        toapis_api_key="configured",
        gemini_toapis_timeout_seconds=300,
        gemini_retry_limit=10,
        gemini_retry_backoff_base_seconds=30,
        gemini_retry_backoff_cap_seconds=240,
        gemini_retry_time_budget_seconds=5400,
        gemini_waf_retry_limit=10,
    )
    snapshots: list[dict[str, object]] = []

    def fail(_payload: dict[str, object], *, read_timeout_seconds: float | None = None) -> str:
        raise gemini_provider.GeminiProviderError("read timed out", ERROR_CONN_RESET)

    monkeypatch.setattr(gemini_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini_provider, "_post_toapis_chat", fail)
    monkeypatch.setattr(gemini_provider.time, "sleep", lambda _seconds: pytest.fail("deferred retry must release the worker"))

    with pytest.raises(gemini_provider.GeminiRetryDeferred):
        asyncio.run(
            gemini_provider.generate_gemini_json_text(
                "defer retry",
                on_retry=snapshots.append,
                defer_retry=True,
            )
        )

    assert snapshots[0]["transportRetries"] == 1
    assert snapshots[0]["retryDelaySeconds"] == 30


def test_direct_gemini_uses_same_gateway_retry_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        gemini_provider="direct",
        gemini_api_key="configured",
        gemini_model="gemini-test",
        gemini_retry_limit=10,
        gemini_retry_backoff_base_seconds=30,
        gemini_retry_backoff_cap_seconds=240,
    )
    calls = 0
    waits: list[float] = []

    class ServiceUnavailable(RuntimeError):
        status_code = 503

    async def direct(_prompt: str, _images: list[dict], _system: str | None) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ServiceUnavailable("UNAVAILABLE")
        return '{}'

    async def record_wait(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(gemini_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini_provider, "_generate_via_google", direct)
    monkeypatch.setattr(gemini_provider.asyncio, "sleep", record_wait)

    assert asyncio.run(gemini_provider.generate_gemini_json_text("direct retry")) == '{}'
    assert calls == 2
    assert waits == [30]


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(401, ERROR_AUTH_FAILED), (408, ERROR_GATEWAY_5XX)],
)
def test_direct_gemini_terminal_error_preserves_stable_classification(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected: str,
) -> None:
    settings = SimpleNamespace(
        gemini_provider="direct",
        gemini_api_key="configured",
        gemini_model="gemini-test",
        gemini_retry_limit=0,
        gemini_retry_backoff_base_seconds=30,
        gemini_retry_backoff_cap_seconds=240,
    )

    class DirectError(RuntimeError):
        pass

    error = DirectError(f"HTTP {status_code}")
    error.status_code = status_code

    async def direct(_prompt: str, _images: list[dict], _system: str | None) -> str:
        raise error

    monkeypatch.setattr(gemini_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini_provider, "_generate_via_google", direct)

    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        asyncio.run(gemini_provider.generate_gemini_json_text("direct terminal error"))

    assert caught.value.error_code == expected
    assert caught.value.provider_response["httpStatus"] == status_code


def test_http_408_is_a_retryable_timeout_class() -> None:
    assert classify_provider_error(status_code=408, message="Request Timeout") == ERROR_GATEWAY_5XX


def test_direct_gemini_stops_before_backoff_exceeds_total_time_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        gemini_provider="direct",
        gemini_api_key="configured",
        gemini_model="gemini-test",
        gemini_retry_limit=10,
        gemini_retry_backoff_base_seconds=30,
        gemini_retry_backoff_cap_seconds=240,
        gemini_retry_time_budget_seconds=1,
    )

    class ServiceUnavailable(RuntimeError):
        status_code = 503

    async def direct(_prompt: str, _images: list[dict], _system: str | None) -> str:
        raise ServiceUnavailable("UNAVAILABLE")

    monkeypatch.setattr(gemini_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini_provider, "_generate_via_google", direct)
    monkeypatch.setattr(gemini_provider.time, "monotonic", lambda: 0.0)

    with pytest.raises(gemini_provider.GeminiProviderError) as caught:
        asyncio.run(gemini_provider.generate_gemini_json_text("direct budget"))

    assert caught.value.error_code == "PROVIDER_TIMEOUT"


def test_gemini_rate_limit_retries_stop_before_exceeding_time_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        gemini_provider="toapis",
        gemini_toapis_model="gemini-test",
        gemini_model="gemini-test",
        toapis_api_key="configured",
        gemini_toapis_timeout_seconds=300,
        gemini_retry_limit=6,
        gemini_retry_backoff_base_seconds=5,
        gemini_retry_backoff_cap_seconds=60,
        gemini_retry_time_budget_seconds=1,
        gemini_waf_retry_limit=2,
    )
    provider_calls = 0

    def post_toapis_chat(_payload: dict[str, object], *, read_timeout_seconds: float | None = None) -> str:
        nonlocal provider_calls
        provider_calls += 1
        raise gemini_provider.GeminiProviderError("rate limited", ERROR_RATE_LIMITED)

    monkeypatch.setattr(gemini_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini_provider, "_post_toapis_chat", post_toapis_chat)
    monkeypatch.setattr(gemini_provider, "_retry_backoff_seconds", lambda attempt, base, cap: 2.0)
    monkeypatch.setattr(gemini_provider.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(gemini_provider.time, "sleep", lambda _seconds: pytest.fail("must not sleep past retry budget"))

    with pytest.raises(gemini_provider.GeminiProviderError, match="rate limited"):
        asyncio.run(gemini_provider.generate_gemini_json_text("tag this garment"))

    assert provider_calls == 1


@pytest.mark.parametrize('count', [16, 17])
def test_toapis_image_budget_checked_before_network(monkeypatch, count):
    from app.generation_progress import ERROR_INVALID_REQUEST
    calls = []
    monkeypatch.setattr(gemini_provider, 'effective_gemini_model', lambda: 'test')
    monkeypatch.setattr(gemini_provider, '_post_toapis_chat_with_retry', lambda *args, **kwargs: calls.append(args) or '{}')
    images = [{'data': b'image', 'mime_type': 'image/png'}] * count
    if count <= 16:
        assert gemini_provider._generate_via_toapis('test', images, None) == '{}'
        assert len(calls) == 1
    else:
        with pytest.raises(gemini_provider.GeminiProviderError) as error:
            gemini_provider._generate_via_toapis('test', images, None)
        assert error.value.error_code == ERROR_INVALID_REQUEST
        assert not calls


@pytest.mark.parametrize('status', [None, 400, 500])
def test_image_count_error_is_not_retried_even_when_wrapped_as_internal_error(status):
    from app.generation_progress import ERROR_INVALID_REQUEST, normalize_toapis_error, is_gemini_transport_retryable_error
    error = normalize_toapis_error({'error': {'code': 'internal_error',
        'message': 'too many images in the message, max allowed is 16'}}, status_code=status)
    assert error['code'] == ERROR_INVALID_REQUEST
    assert not is_gemini_transport_retryable_error(error['code'])
