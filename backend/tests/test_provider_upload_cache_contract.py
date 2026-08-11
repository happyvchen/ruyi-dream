from __future__ import annotations

import pytest

from app import image_provider
from app.generation_progress import GenerationPhaseError


def test_runtime_collage_upload_is_reused_by_content_hash() -> None:
    calls = 0

    def upload() -> str:
        nonlocal calls
        calls += 1
        return "https://provider.example/collage.jpg"

    first = image_provider.upload_bytes_with_cache("toapis", b"same-collage", upload)
    second = image_provider.upload_bytes_with_cache("toapis", b"same-collage", upload)

    assert first == second == "https://provider.example/collage.jpg"
    assert calls == 1


def test_runtime_collage_cache_does_not_mix_different_content() -> None:
    calls = 0

    def upload() -> str:
        nonlocal calls
        calls += 1
        return f"https://provider.example/collage-{calls}.jpg"

    first = image_provider.upload_bytes_with_cache("toapis", b"collage-a", upload)
    second = image_provider.upload_bytes_with_cache("toapis", b"collage-b", upload)

    assert first != second
    assert calls == 2


def test_upload_request_timeout_is_clipped_to_stage_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_provider.time, "time", lambda: 100.0)

    assert image_provider.remaining_upload_timeout(220.9) == 120

    with pytest.raises(GenerationPhaseError, match="总预算"):
        image_provider.remaining_upload_timeout(99.0)
