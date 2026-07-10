from io import BytesIO

import pytest
from PIL import Image

from app.image_ingestion import (
    ImageIngestionError,
    declared_upload_content_type,
    inspect_image,
    normalize_image,
    normalized_image_candidates,
)


def encoded_image(image_format: str, mode: str = "RGB", size: tuple[int, int] = (32, 24)) -> bytes:
    output = BytesIO()
    Image.new(mode, size, (30, 80, 140, 128) if mode == "RGBA" else (30, 80, 140)).save(output, image_format)
    return output.getvalue()


@pytest.mark.parametrize(
    ("image_format", "filename", "content_type"),
    [
        ("JPEG", "sample.jpg", "image/jpeg"),
        ("PNG", "sample.png", "image/png"),
        ("WEBP", "sample.webp", "image/webp"),
        ("HEIF", "sample.heic", "image/heic"),
        ("AVIF", "sample.avif", "image/avif"),
    ],
)
def test_common_static_formats_are_accepted(image_format: str, filename: str, content_type: str) -> None:
    inspected = inspect_image(encoded_image(image_format), filename, 100_000_000)
    assert inspected.content_type == content_type
    assert (inspected.width, inspected.height) == (32, 24)


def test_image_over_business_pixel_limit_has_specific_error() -> None:
    content = encoded_image("PNG", size=(100, 100))
    with pytest.raises(ImageIngestionError, match="超过 0MP 像素限制") as caught:
        inspect_image(content, "large.png", 9_999)
    assert caught.value.code == "PIXEL_LIMIT"


def test_corrupt_image_has_decode_error() -> None:
    with pytest.raises(ImageIngestionError, match="已损坏、不完整") as caught:
        inspect_image(b"not an image", "broken.jpg", 100_000_000)
    assert caught.value.code == "DECODE_FAILED"


def test_animated_webp_is_rejected() -> None:
    output = BytesIO()
    frames = [Image.new("RGB", (16, 16), color) for color in ("red", "blue")]
    frames[0].save(output, "WEBP", save_all=True, append_images=frames[1:], duration=100, loop=0)
    with pytest.raises(ImageIngestionError, match="动态图") as caught:
        inspect_image(output.getvalue(), "animated.webp", 100_000_000)
    assert caught.value.code == "ANIMATED_IMAGE"


def test_normalization_preserves_alpha_and_caps_long_edge() -> None:
    normalized = normalize_image(encoded_image("PNG", mode="RGBA", size=(80, 40)), 64)
    assert normalized.content_type == "image/png"
    assert normalized.suffix == ".png"
    assert (normalized.width, normalized.height) == (64, 32)


def test_upload_type_and_normalized_candidates_are_centralized() -> None:
    assert declared_upload_content_type("", "phone.HEIC") == "image/heic"
    assert normalized_image_candidates("uploads/look.webp") == [
        "uploads/_derived/normalized/look.jpg",
        "uploads/_derived/normalized/look.png",
    ]
