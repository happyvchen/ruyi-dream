from __future__ import annotations

import asyncio
from io import BytesIO
from types import SimpleNamespace

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


def test_toapis_pixel_policy_scales_7999_by_8000_below_safe_pixel_budget() -> None:
    assert image_provider.toapis_input_target_size(7999, 8000) == (5999, 6000)


def test_toapis_safe_policy_scales_before_official_limit() -> None:
    width, height = image_provider.toapis_input_target_size(8100, 4000)

    assert width == 8000
    assert height == 3950
    assert width * height <= image_provider.TOAPIS_TARGET_INPUT_PIXELS


def test_toapis_transport_copy_resizes_even_when_encoded_file_is_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PIL import Image

    source = BytesIO()
    Image.new("RGB", (80, 80), (220, 220, 220)).save(source, "JPEG", quality=70)
    original = source.getvalue()
    monkeypatch.setattr(image_provider, "TOAPIS_MAX_INPUT_PIXELS", 4_000)
    monkeypatch.setattr(image_provider, "TOAPIS_TARGET_INPUT_PIXELS", 3_600)

    normalized, filename, content_type = image_provider.fit_image_bytes_under_limit(
        original,
        "master.jpg",
        "image/jpeg",
    )

    with Image.open(BytesIO(normalized)) as result:
        assert result.size == (60, 60)
    assert len(original) < image_provider.TOAPIS_TARGET_UPLOAD_BYTES
    assert filename == "master.jpg"
    assert content_type == "image/jpeg"


def test_toapis_transport_copy_keeps_small_compliant_image_byte_identical() -> None:
    from PIL import Image

    source = BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(source, "JPEG", quality=85)
    original = source.getvalue()

    assert image_provider.fit_image_bytes_under_limit(original, "small.jpg", "image/jpeg") == (
        original,
        "small.jpg",
        "image/jpeg",
    )


def test_toapis_transport_copy_flattens_small_compliant_alpha_image() -> None:
    from PIL import Image

    source = BytesIO()
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(source, "PNG")

    normalized, filename, content_type = image_provider.fit_image_bytes_under_limit(
        source.getvalue(), "small.png", "image/png"
    )

    with Image.open(BytesIO(normalized)) as result:
        assert result.convert("RGB").getpixel((0, 0)) == (255, 255, 255)
    assert filename == "small.jpg"
    assert content_type == "image/jpeg"


def test_toapis_input_cache_namespace_invalidates_legacy_provider_url() -> None:
    assert image_provider.toapis_upload_cache_key("media/example/original.png") == (
        "input-v2:media/example/original.png"
    )


def test_toapis_content_hash_cache_namespace_does_not_reuse_legacy_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache: dict[tuple[str, str], str] = {}
    calls = 0

    def fake_upload_object_with_cache(provider: str, object_key: str, uploader):
        nonlocal calls
        key = (provider, object_key)
        if key not in cache:
            calls += 1
            cache[key] = uploader()
        return cache[key]

    monkeypatch.setattr(image_provider, "upload_object_with_cache", fake_upload_object_with_cache)

    legacy = image_provider.upload_bytes_with_cache(
        "toapis", b"same-image", lambda: "https://files.toapis.com/legacy.jpg"
    )
    current = image_provider.upload_bytes_with_cache(
        "toapis",
        b"same-image",
        lambda: "https://files.toapis.com/v2.jpg",
        cache_namespace="input-v2",
    )

    assert legacy.endswith("legacy.jpg")
    assert current.endswith("v2.jpg")
    assert calls == 2


def test_toapis_old_provider_url_without_policy_is_reprocessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        image_provider,
        "get_settings",
        lambda: SimpleNamespace(generation_upload_timeout_seconds=300),
    )
    monkeypatch.setattr(
        image_provider,
        "upload_url_to_toapis",
        lambda url, filename, deadline=None: calls.append(url) or "https://files.toapis.com/v2.jpg",
    )

    result = image_provider.upload_toapis_image_inputs(
        [{"providerUrl": "https://files.toapis.com/legacy.jpg", "filename": "master.jpg"}]
    )

    assert calls == ["https://files.toapis.com/legacy.jpg"]
    assert result[0]["providerUrl"] == "https://files.toapis.com/v2.jpg"
    assert result[0]["providerInputPolicyVersion"] == "toapis-input-v2"


def test_toapis_current_policy_provider_url_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        image_provider,
        "get_settings",
        lambda: SimpleNamespace(generation_upload_timeout_seconds=300),
    )
    monkeypatch.setattr(
        image_provider,
        "upload_url_to_toapis",
        lambda *args, **kwargs: pytest.fail("matching v2 provider URL should be reused"),
    )

    result = image_provider.upload_toapis_image_inputs(
        [
            {
                "providerUrl": "https://files.toapis.com/v2.jpg",
                "providerInputPolicyVersion": "toapis-input-v2",
            }
        ]
    )

    assert result[0]["providerUrl"] == "https://files.toapis.com/v2.jpg"


@pytest.mark.parametrize(
    "url",
    [
        "https://files.toapis.com/uploads/reference.jpg",
        "https://files.toapis.xyz/uploads/reference.jpg",
        "https://cdn.toapis.com/uploads/reference.jpg",
        "https://cdn.toapis.xyz/uploads/reference.jpg",
    ],
)
def test_toapis_file_url_accepts_official_primary_and_mainland_domains(url: str) -> None:
    assert image_provider.is_toapis_file_url(url)


def test_toapis_file_url_rejects_lookalike_domain() -> None:
    assert not image_provider.is_toapis_file_url("https://files.toapis.xyz.example/reference.jpg")


def test_toapis_url_appends_versioned_path_to_configured_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        image_provider,
        "get_settings",
        lambda: SimpleNamespace(toapis_base_url="https://toapis.xyz/"),
    )

    assert image_provider.toapis_url("/v1/images/generations") == (
        "https://toapis.xyz/v1/images/generations"
    )


def test_provider_reference_manifest_records_input_policy_version() -> None:
    manifest = image_provider.build_provider_reference_image_manifest(
        [{"role": "shooting_master", "providerInputPolicyVersion": "toapis-input-v2"}],
        ["https://files.toapis.com/v2.jpg"],
    )

    assert manifest[0]["providerInputPolicyVersion"] == "toapis-input-v2"


def test_toapis_transport_copy_preserves_aspect_ratio_and_flattens_alpha_to_white(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PIL import Image

    source = BytesIO()
    image = Image.new("RGBA", (100, 50), (255, 0, 0, 0))
    image.putpixel((50, 25), (255, 0, 0, 255))
    image.save(source, "PNG")
    monkeypatch.setattr(image_provider, "TOAPIS_MAX_INPUT_LONG_EDGE", 90)
    monkeypatch.setattr(image_provider, "TOAPIS_TARGET_INPUT_LONG_EDGE", 80)

    normalized, filename, content_type = image_provider.fit_image_bytes_under_limit(
        source.getvalue(), "transparent.png", "image/png"
    )

    with Image.open(BytesIO(normalized)) as result:
        assert result.size == (80, 40)
        assert result.convert("RGB").getpixel((0, 0))[0] > 245
        assert result.convert("RGB").getpixel((0, 0))[1] > 245
        assert result.convert("RGB").getpixel((0, 0))[2] > 245
    assert filename == "transparent.jpg"
    assert content_type == "image/jpeg"


def test_toapis_transport_copy_rejects_undecodable_image() -> None:
    with pytest.raises(GenerationPhaseError) as raised:
        image_provider.fit_image_bytes_under_limit(b"not-an-image", "broken.jpg", "image/jpeg")

    assert raised.value.error_code == "INVALID_REQUEST"


def test_toapis_transport_copy_never_returns_bytes_over_target() -> None:
    from PIL import Image

    source = BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(source, "JPEG", quality=95)

    with pytest.raises(GenerationPhaseError) as raised:
        image_provider.fit_image_bytes_under_limit(source.getvalue(), "tiny.jpg", "image/jpeg", target_bytes=10)

    assert raised.value.error_code == "INVALID_REQUEST"


def test_toapis_submit_snapshot_contains_live_reference_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded = [
        {
            "role": "shooting_master",
            "objectKey": "media/master.jpg",
            "providerUrl": "https://files.toapis.com/v2.jpg",
            "providerInputPolicyVersion": "toapis-input-v2",
        }
    ]
    monkeypatch.setattr(
        image_provider,
        "get_settings",
        lambda: SimpleNamespace(
            toapis_api_key="test-key",
            toapis_base_url="https://toapis.example",
            toapis_image_submit_timeout_seconds=30,
            generation_submit_retry_limit=10,
        ),
    )
    monkeypatch.setattr(image_provider, "prepare_toapis_image_inputs", lambda inputs, progress=None: (uploaded, [uploaded[0]["providerUrl"]]))
    monkeypatch.setattr(image_provider, "build_toapis_request_snapshot", lambda task, urls: {"model": "gpt-image-2", "image_urls": urls})
    monkeypatch.setattr(image_provider, "post_generation_task_with_confirmed_failure_retries", lambda *args, **kwargs: {"id": "task-1", "status": "submitted"})

    result = asyncio.run(image_provider.GPTImage2ToapisProvider().submit({"imageInputs": [{}]}, "output.png"))

    assert result["providerReferenceImageManifest"][0]["providerInputPolicyVersion"] == "toapis-input-v2"
    assert result["providerReferenceImageManifest"][0]["url"] == "https://files.toapis.com/v2.jpg"


def test_upload_request_timeout_is_clipped_to_stage_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_provider.time, "time", lambda: 100.0)

    assert image_provider.remaining_upload_timeout(220.9) == 120

    with pytest.raises(GenerationPhaseError, match="总预算"):
        image_provider.remaining_upload_timeout(99.0)
