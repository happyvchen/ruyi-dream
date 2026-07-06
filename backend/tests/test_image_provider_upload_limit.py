"""toapis 上传体积护栏测试：只压超限大图，小图原样返回。纯本地，不触发任何外部 provider。"""

from io import BytesIO

from PIL import Image

from app.image_provider import (
    TOAPIS_TARGET_UPLOAD_BYTES,
    fit_image_bytes_under_limit,
)


def png_bytes(width: int, height: int, noisy: bool = False) -> bytes:
    image = Image.new("RGB", (width, height), (200, 200, 200))
    if noisy:
        import random

        random.seed(42)
        pixels = image.load()
        for x in range(width):
            for y in range(height):
                pixels[x, y] = (
                    random.randrange(256),
                    random.randrange(256),
                    random.randrange(256),
                )
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_small_image_untouched() -> None:
    content = png_bytes(512, 512)
    result, filename, content_type = fit_image_bytes_under_limit(content, "hero.png", "image/png")
    assert result == content
    assert filename == "hero.png"
    assert content_type == "image/png"


def test_oversized_image_compressed_under_limit() -> None:
    # 随机噪声 PNG 压缩率极低，可稳定超过 10MB
    content = png_bytes(2048, 2048, noisy=True)
    assert len(content) > TOAPIS_TARGET_UPLOAD_BYTES
    result, filename, content_type = fit_image_bytes_under_limit(content, "preview-123.png", "image/png")
    assert len(result) <= TOAPIS_TARGET_UPLOAD_BYTES
    assert filename == "preview-123.jpg"
    assert content_type == "image/jpeg"
    # 结果仍是可解码图片，且优先保留原分辨率
    with Image.open(BytesIO(result)) as image:
        assert image.size == (2048, 2048)


def test_non_image_bytes_passthrough() -> None:
    content = b"x" * (TOAPIS_TARGET_UPLOAD_BYTES + 1)
    result, filename, content_type = fit_image_bytes_under_limit(content, "blob.bin", None)
    assert result == content
    assert filename == "blob.bin"
