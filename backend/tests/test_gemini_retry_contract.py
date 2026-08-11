from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import gemini_provider
from app.generation_progress import (
    ERROR_AUTH_FAILED,
    ERROR_CONN_RESET,
    ERROR_GATEWAY_5XX,
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
