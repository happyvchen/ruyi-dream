from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from app.image_ingestion import (
    ImageIngestionError,
    declared_upload_content_type,
    inspect_image,
    normalize_image,
)


def image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (32, 24), *, animated: bool = False) -> bytes:
    output = BytesIO()
    image = Image.new("RGBA" if fmt == "PNG" else "RGB", size, (255, 0, 0, 255))
    if animated:
        frame = Image.new("RGB", size, (0, 255, 0))
        image.save(output, "GIF", save_all=True, append_images=[frame], duration=100, loop=0)
    else:
        image.save(output, fmt)
    return output.getvalue()


@pytest.mark.parametrize(
    ("mime_type", "filename", "expected"),
    [
        ("image/jpeg", "look.jpg", "image/jpeg"),
        ("", "look.webp", "image/webp"),
        (None, "look.heic", "image/heic"),
        ("application/octet-stream", "look.avif", "image/avif"),
    ],
)
def test_declared_content_type_accepts_static_supported_image_formats(
    mime_type: str | None,
    filename: str,
    expected: str,
) -> None:
    assert declared_upload_content_type(mime_type, filename) == expected


def test_declared_content_type_rejects_non_images_with_business_message() -> None:
    with pytest.raises(ImageIngestionError) as exc_info:
        declared_upload_content_type("text/plain", "readme.txt")

    assert exc_info.value.code == "UNSUPPORTED_FORMAT"
    assert "仅支持 JPG" in str(exc_info.value)


def test_inspect_image_rejects_animation_even_when_file_is_decodable() -> None:
    with pytest.raises(ImageIngestionError) as exc_info:
        inspect_image(image_bytes(animated=True), "moving.gif", max_pixels=10_000)

    assert exc_info.value.code in {"UNSUPPORTED_FORMAT", "ANIMATED_IMAGE"}


def test_inspect_image_rejects_pixel_extremes_before_storage_or_provider_use() -> None:
    with pytest.raises(ImageIngestionError) as exc_info:
        inspect_image(image_bytes(size=(100, 100)), "huge.jpg", max_pixels=9_999)

    assert exc_info.value.code == "PIXEL_LIMIT"


def test_normalize_image_preserves_alpha_as_png_and_downscales_large_edges() -> None:
    normalized = normalize_image(image_bytes("PNG", size=(128, 32)), max_edge=64)

    assert normalized.content_type == "image/png"
    assert normalized.suffix == ".png"
    assert normalized.width == 64
    assert normalized.height == 16
