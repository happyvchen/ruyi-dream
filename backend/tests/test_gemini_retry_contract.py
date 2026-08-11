from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import gemini_provider
from app.generation_progress import ERROR_CONN_RESET, ERROR_RATE_LIMITED


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
