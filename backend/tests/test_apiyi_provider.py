import asyncio
import base64
import random
from io import BytesIO

import pytest
import requests
from PIL import Image

from app import apiyi_provider as apiyi, image_provider, main
from app.generation_progress import ERROR_SUBMISSION_UNKNOWN, GenerationPhaseError
from conftest import insert_generation_task


def picture(fmt="PNG", size=(32, 24), noise=False):
    image = (Image.frombytes("RGB", size, random.Random(17).randbytes(size[0] * size[1] * 3))
             if noise else Image.new("RGB", size, "red"))
    buf = BytesIO()
    image.save(buf, fmt)
    return buf.getvalue()


@pytest.fixture
def configured(monkeypatch):
    settings = image_provider.get_settings()
    monkeypatch.setattr(settings, "image_provider", "apiyi")
    monkeypatch.setattr(settings, "apiyi_api_key", "test-key")
    monkeypatch.setattr(settings, "apiyi_base_url", "https://apiyi.example/v1")
    monkeypatch.setattr(settings, "apiyi_image_model", "gpt-image-2.5-sunburst-2026-09-08")
    monkeypatch.setattr(settings, "apiyi_image_size", "2880x2880")
    monkeypatch.setattr(settings, "apiyi_timeout_seconds", 360)
    monkeypatch.setattr(settings, "gpt_image2_output_format", "png")
    monkeypatch.setattr(image_provider, "render_reference_input_bytes", lambda item, deadline, **kwargs: (picture(), "image/png"))
    return settings


@pytest.mark.parametrize("size_bytes", [1_500_001, 10_000_000])
@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_under_recommendation_is_byte_identical_with_no_edge_cap(fmt, size_bytes):
    source = picture(fmt, (4000, 1000))
    original = source + b"\0" * (size_bytes - len(source))
    result, facts = apiyi.compress_references([original])
    assert result == [original]
    assert facts[0]["compressionMethod"] == "unchanged"
    assert facts[0]["uploadedDimensions"] == [4000, 1000]


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_oversized_reference_uses_lossless_encoding_at_original_dimensions(fmt):
    original = picture(fmt, (4000, 1000)) + b"\0" * apiyi.RECOMMENDED_IMAGE_BYTES
    result, facts = apiyi.compress_references([original])
    with Image.open(BytesIO(original)) as source, Image.open(BytesIO(result[0])) as uploaded:
        assert uploaded.size == source.size == (4000, 1000)
        assert uploaded.convert("RGBA").tobytes() == source.convert("RGBA").tobytes()
    assert facts[0]["compressionMethod"] == "lossless"
    assert len(result[0]) <= apiyi.RECOMMENDED_IMAGE_BYTES


def test_large_group_preserves_every_reference_and_order():
    contents = [picture(size=(700, 700), noise=True) + bytes([i]) * 2_000_000 for i in range(16)]
    assert sum(map(len, contents)) > 50_000_000
    result, facts = apiyi.compress_references(contents)
    assert result == contents
    assert all(fact["compressionMethod"] == "unchanged" for fact in facts)
    assert all("aggregateBudgetExceeded" not in fact for fact in facts)


def test_processing_failure_stops_before_paid_submission(configured, monkeypatch):
    monkeypatch.setattr(image_provider, "render_reference_input_bytes", lambda *args, **kwargs: (b"broken", "image/png"))
    monkeypatch.setattr(apiyi.requests, "post", lambda *a, **kw: pytest.fail("unexpected paid POST"))
    with pytest.raises(ValueError, match="第 1 张参考图准备失败"):
        asyncio.run(apiyi.GPTImage2ApiyiProvider().submit({"prompt": "edit", "imageInputs": [{}]}, "unused"))


@pytest.mark.parametrize("budget,method", [(200_000, "quality"), (10_000, "resize")])
def test_quality_before_resize_and_only_oversized_input_changes(monkeypatch, budget, method):
    monkeypatch.setattr(apiyi, "RECOMMENDED_IMAGE_BYTES", budget)
    source = picture(size=(500, 400), noise=True)
    healthy = picture()
    result, facts = apiyi.compress_references([source, healthy])
    assert result[1] == healthy
    assert len(result[0]) <= budget
    assert facts[0]["compressionMethod"] == method
    with Image.open(BytesIO(result[0])) as uploaded:
        if method == "quality":
            assert uploaded.size == (500, 400)
        else:
            assert uploaded.width < 500 and uploaded.height < 400
            assert abs(uploaded.width / uploaded.height - 1.25) < 0.02


def test_png_alpha_survives_reencoding():
    source = BytesIO()
    Image.new("RGBA", (100, 50), (10, 20, 30, 123)).save(source, "PNG")
    result, _ = apiyi.compress_references([source.getvalue() + b"\0" * apiyi.RECOMMENDED_IMAGE_BYTES])
    with Image.open(BytesIO(result[0])) as image:
        assert image.size == (100, 50)
        assert image.getpixel((0, 0)) == (10, 20, 30, 123)


def test_lossy_webp_retains_alpha_and_color_profile(monkeypatch):
    monkeypatch.setattr(apiyi, "RECOMMENDED_IMAGE_BYTES", 200_000)
    with Image.open(BytesIO(picture(size=(500, 400), noise=True))) as source:
        rgba = source.convert("RGBA")
    rgba.putalpha(123)
    buf = BytesIO()
    rgba.save(buf, "PNG", icc_profile=b"test-profile")
    result, facts = apiyi.compress_references([buf.getvalue()])
    assert facts[0]["compressionMethod"] == "quality"
    with Image.open(BytesIO(result[0])) as uploaded:
        assert uploaded.size == rgba.size
        assert uploaded.getchannel("A").tobytes() == rgba.getchannel("A").tobytes()
        assert uploaded.info["icc_profile"] == b"test-profile"


class Response:
    status_code = 200
    headers = {"x-request-id": "req-log-only"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def json(self):
        return {"data": [{"b64_json": base64.b64encode(picture()).decode()}],
                "usage": {"input_tokens": 1050, "output_tokens": 7024}}


def test_multipart_and_base64_delivery(configured, monkeypatch):
    calls = []
    stored = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(apiyi.requests, "post", post)
    monkeypatch.setattr(image_provider, "put_bytes", lambda *args: stored.append(args))
    monkeypatch.setattr(image_provider, "generate_image_derivatives", lambda *args: {"thumb": "thumb.webp"})
    provider = image_provider.get_image_provider()
    result = asyncio.run(provider.generate({"prompt": "keep product", "imageInputs": [
        {"objectKey": "first", "role": "background"}, {"objectKey": "second", "role": "hero"},
    ]}, "result.png"))
    url, kwargs = calls[0]
    assert url == "https://apiyi.example/v1/images/edits"
    assert kwargs["timeout"] == 360
    assert kwargs["allow_redirects"] is False
    assert kwargs["data"]["quality"] == "max"
    assert kwargs["data"]["size"] == "2880x2880"
    assert kwargs["data"]["model"] == "gpt-image-2.5-sunburst-2026-09-08"
    assert not {"response_format", "input_fidelity", "image_urls", "client_business_id"} & kwargs["data"].keys()
    assert [name for name, _ in kwargs["files"]] == ["image[]", "image[]"]
    assert [item["objectKey"] for item in result["providerReferenceImageManifest"]] == ["first", "second"]
    assert all(item["providerInputPolicyVersion"] == "apiyi_multipart_v2"
               for item in result["providerReferenceImageManifest"])
    assert result["providerRequestId"] is None
    assert result["httpRequestId"] == "req-log-only"
    assert result["outputPath"] == "result.png"
    assert stored == [("result.png", picture(), "image/png")]
    assert result["thumbPath"] == "thumb.webp"
    assert "b64_json" not in result["providerStatusResponse"]["data"][0]
    assert result["providerResponse"]["usage"]["output_tokens"] == 7024


@pytest.mark.parametrize("count", [0, 17])
def test_reference_limit_checked_before_network(configured, monkeypatch, count):
    monkeypatch.setattr(apiyi.requests, "post", lambda *a, **kw: pytest.fail("unexpected network"))
    with pytest.raises(ValueError, match="1–16"):
        asyncio.run(apiyi.GPTImage2ApiyiProvider().submit({"prompt": "edit", "imageInputs": [{}] * count}, "unused"))


def test_explicit_http_failure_preserves_request_id(configured, monkeypatch):
    response = Response()
    response.status_code = 401
    response.json = lambda: {"error": {"message": "invalid key", "code": "unauthorized"}}
    monkeypatch.setattr(apiyi.requests, "post", lambda *a, **kw: response)
    with pytest.raises(image_provider.ImageProviderError) as error:
        asyncio.run(apiyi.GPTImage2ApiyiProvider().submit({"prompt": "edit", "imageInputs": [{}]}, "unused"))
    assert error.value.error_code == "AUTH_FAILED"
    assert error.value.provider_response["httpRequestId"] == "req-log-only"



def test_explicit_size_rejection_does_not_recompress_or_retry(configured, monkeypatch):
    calls = []
    response = Response()
    response.status_code = 413
    response.json = lambda: {"error": {"message": "request body too large"}}
    monkeypatch.setattr(apiyi.requests, "post", lambda *a, **kw: calls.append(kw) or response)
    with pytest.raises(image_provider.ImageProviderError) as error:
        asyncio.run(apiyi.GPTImage2ApiyiProvider().submit({"prompt": "edit", "imageInputs": [{}]}, "unused"))
    assert len(calls) == 1
    assert "too large" in str(error.value)
    assert error.value.provider_response["providerResponse"] == response.json()


@pytest.mark.parametrize("failure", ["timeout", "http504", "malformed"])
def test_unknown_submission_never_reconciles_or_resubmits(configured, monkeypatch, failure):
    calls = []
    task_id = insert_generation_task()
    attempt_id = main.create_attempt("generation_styling", task_id, "image-2")

    def post(*args, **kwargs):
        calls.append(1)
        if failure == "timeout":
            raise requests.Timeout("read timeout")
        response = Response()
        if failure == "http504":
            response.status_code = 504
        else:
            response.json = lambda: {"data": []}
        return response

    monkeypatch.setattr(apiyi.requests, "post", post)
    monkeypatch.setattr(main, "run_image_provider_check", lambda *a: pytest.fail("no async task exists"))
    with pytest.raises(image_provider.ProviderSubmissionUnknownError) as error:
        asyncio.run(main.generate_image_asset({"prompt": "edit", "imageInputs": [{}]}, "unused", attempt_id))
    assert error.value.error_code == ERROR_SUBMISSION_UNKNOWN
    assert len(calls) == 1
    snapshot = main.get_attempt_response_snapshot(attempt_id)
    assert snapshot["provider"] == "apiyi"
    assert snapshot["status"] == "SUBMISSION_UNKNOWN"
    assert snapshot["unknownSubmissionRetries"] == 0


def test_delivery_failure_resumes_without_task_id_or_api_key(configured, monkeypatch):
    task_id = insert_generation_task()
    attempt_id = main.create_attempt("generation_styling", task_id, "image-2")
    monkeypatch.setattr(apiyi.requests, "post", lambda *a, **kw: Response())
    provider = apiyi.GPTImage2ApiyiProvider()
    submitted = asyncio.run(provider.submit({"prompt": "edit", "imageInputs": [{}]}, "result.png"))
    main.persist_attempt_provider_progress(attempt_id, submitted)
    monkeypatch.setattr(image_provider, "put_bytes", lambda *a: (_ for _ in ()).throw(OSError("storage offline")))
    with pytest.raises(GenerationPhaseError):
        asyncio.run(provider.deliver({"output_path": "result.png"}, submitted["providerStatusResponse"]))
    assert main.attempt_has_provider_request_id(attempt_id)
    snapshot = main.get_attempt_provider_resume_snapshot(attempt_id)
    assert snapshot["providerRequestId"] is None
    monkeypatch.setattr(configured, "apiyi_api_key", "")
    monkeypatch.setattr(configured, "image_provider", "fal")
    monkeypatch.setattr(apiyi.requests, "post", lambda *a, **kw: pytest.fail("must not regenerate"))
    monkeypatch.setattr(image_provider, "put_bytes", lambda *a: None)
    monkeypatch.setattr(image_provider, "generate_image_derivatives", lambda *a: {})
    result = main.run_image_provider_deliver(snapshot, "result.png")
    assert result["outputPath"] == "result.png"
    assert "b64_json" not in result["providerStatusResponse"]["data"][0]


def test_cancellation_after_preparation_does_not_submit(configured, monkeypatch):
    cancelled = False

    def progress(done, total):
        nonlocal cancelled
        cancelled = done == total

    monkeypatch.setattr(apiyi.requests, "post", lambda *a, **kw: pytest.fail("cancelled request"))
    with pytest.raises(RuntimeError, match="取消"):
        asyncio.run(apiyi.GPTImage2ApiyiProvider().submit(
            {"prompt": "edit", "imageInputs": [{}]}, "unused", progress, lambda: cancelled))


def test_timeout_floor_and_production_snapshot(configured, monkeypatch):
    monkeypatch.setattr(configured, "apiyi_timeout_seconds", 359)
    with pytest.raises(RuntimeError, match="APIYI_TIMEOUT"):
        configured.validate()
    monkeypatch.setattr(configured, "apiyi_timeout_seconds", 360)
    monkeypatch.setattr(configured, "app_env", "production")
    monkeypatch.setattr(configured, "apiyi_image_model", "gpt-image-2.5-sunburst")
    with pytest.raises(RuntimeError, match="snapshot"):
        configured.validate()


def test_original_source_bypasses_normalized_edge_cap_and_retains_frozen_geometry(monkeypatch):
    original = picture(size=(9000, 100))
    normalized = picture(size=(8000, 89))
    monkeypatch.setattr(image_provider, "object_exists", lambda key: True)
    monkeypatch.setattr(image_provider, "normalized_or_original_key", lambda key: "normalized.jpg")
    monkeypatch.setattr(image_provider, "get_bytes", lambda key: original if key == "source.png" else normalized)
    source = {"objectKey": "source.png"}
    assert image_provider.render_reference_input_bytes(source)[0] == normalized
    assert image_provider.render_reference_input_bytes(source, prefer_original=True)[0] == original
    cropped, _ = image_provider.render_reference_input_bytes(
        {**source, "providerCrop": {"kind": "keep_top_ratio", "ratio": 0.5}}, prefer_original=True)
    with Image.open(BytesIO(cropped)) as image:
        assert image.size == (9000, 50)


def test_original_preference_propagates_into_collage(monkeypatch):
    calls = []
    def read(source, deadline, *, prefer_original=False):
        calls.append(prefer_original)
        return picture(), "image/png"
    monkeypatch.setattr(image_provider, "reference_source_bytes", read)
    image_provider.render_reference_input_bytes(
        {"collageItems": [{"objectKey": "one"}, {"objectKey": "two"}]}, prefer_original=True)
    assert calls == [True, True]


def test_unsupported_original_format_uses_normalized_copy(monkeypatch):
    original, normalized = picture("TIFF"), picture()
    monkeypatch.setattr(image_provider, "object_exists", lambda key: True)
    monkeypatch.setattr(image_provider, "normalized_or_original_key", lambda key: "normalized.png")
    monkeypatch.setattr(image_provider, "get_bytes", lambda key: original if key == "original.tiff" else normalized)
    content, _ = image_provider.render_reference_input_bytes({"objectKey": "original.tiff"}, prefer_original=True)
    assert content == normalized


def test_reencoded_webp_uses_matching_multipart_mime(configured, monkeypatch):
    monkeypatch.setattr(apiyi, "RECOMMENDED_IMAGE_BYTES", 200_000)
    source = picture(size=(500, 400), noise=True)
    def render(item, deadline, *, prefer_original=False):
        assert prefer_original
        return source, "image/png"
    monkeypatch.setattr(image_provider, "render_reference_input_bytes", render)
    calls = []
    monkeypatch.setattr(apiyi.requests, "post", lambda *args, **kwargs: calls.append(kwargs) or Response())
    result = asyncio.run(apiyi.GPTImage2ApiyiProvider().submit({"prompt": "edit", "imageInputs": [{}]}, "unused"))
    filename, content, mime = calls[0]["files"][0][1]
    assert filename == "reference-1.webp" and mime == "image/webp"
    with Image.open(BytesIO(content)) as uploaded:
        assert uploaded.size == (500, 400)
    assert result["providerReferenceImageManifest"][0]["compressionMethod"] == "quality"


def test_reencoding_applies_exif_orientation_once():
    source = Image.new("RGB", (100, 50), "red")
    exif = Image.Exif()
    exif[274] = 6
    buf = BytesIO()
    source.save(buf, "JPEG", exif=exif)
    result, facts = apiyi.compress_references([buf.getvalue() + b"\0" * apiyi.RECOMMENDED_IMAGE_BYTES])
    with Image.open(BytesIO(result[0])) as uploaded:
        assert uploaded.size == (50, 100)
        assert uploaded.getexif().get(274, 1) == 1
    assert facts[0]["originalDimensions"] == [100, 50]
    assert facts[0]["uploadedDimensions"] == [50, 100]
