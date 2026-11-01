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
    monkeypatch.setattr(image_provider, "render_reference_input_bytes", lambda item, deadline: (picture(), "image/png"))
    return settings


def test_small_reference_is_byte_identical():
    original = picture()
    result, _ = apiyi.compress_references([original])
    assert result == [original]


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_large_reference_preserves_format_and_never_upscales(fmt):
    original = picture(fmt, (4000, 1000)) + b"\0" * 1_500_001
    result, facts = apiyi.compress_references([original])
    with Image.open(BytesIO(result[0])) as image:
        assert image.format == fmt
        assert image.size == (2048, 512)
    assert facts[0]["uploadedBytes"] < facts[0]["originalBytes"]
    small = picture(fmt, (100, 50)) + b"\0" * 1_500_001
    with Image.open(BytesIO(apiyi.compress_references([small])[0][0])) as image:
        assert image.size == (100, 50)


def test_total_budget_also_applies_to_many_small_images():
    source = picture(size=(700, 700), noise=True)
    assert len(source) < apiyi.SINGLE_IMAGE_BYTES
    result, _ = apiyi.compress_references([source] * 5)
    assert len(result) == 5
    assert sum(map(len, result)) <= apiyi.TOTAL_IMAGE_BYTES


def test_compression_failure_retains_original_and_continues():
    broken = b"not an image" * 150_000
    good = picture()
    result, facts = apiyi.compress_references([broken, good])
    assert result == [broken, good]
    assert facts[0]["compressionFallback"]


def test_impossible_budget_records_fallback_without_shrinking_healthy_inputs():
    broken = b"x" * (apiyi.TOTAL_IMAGE_BYTES + 1)
    good = picture()
    result, facts = apiyi.compress_references([broken, good])
    assert result == [broken, good]
    assert all(item["aggregateBudgetExceeded"] for item in facts)


def test_png_alpha_survives_reencoding():
    source = BytesIO()
    Image.new("RGBA", (100, 50), (10, 20, 30, 123)).save(source, "PNG")
    result, _ = apiyi.compress_references([source.getvalue() + b"\0" * 1_500_001])
    with Image.open(BytesIO(result[0])) as image:
        assert image.getpixel((0, 0)) == (10, 20, 30, 123)


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
