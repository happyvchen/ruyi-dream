from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import warnings

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - dependency is required in production
    pass


SUPPORTED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "HEIF", "AVIF"}
SUPPORTED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "image/avif",
}
SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif"}
FORMAT_MIME_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "HEIF": "image/heic",
    "AVIF": "image/avif",
}


class ImageIngestionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InspectedImage:
    image_format: str
    content_type: str
    width: int
    height: int


@dataclass(frozen=True)
class NormalizedImage:
    content: bytes
    content_type: str
    suffix: str
    width: int
    height: int


def declared_upload_content_type(mime_type: str | None, filename: str) -> str:
    declared = (mime_type or "").split(";", 1)[0].strip().lower()
    if declared in SUPPORTED_IMAGE_MIME_TYPES:
        return declared
    suffix = Path(filename).suffix.lower()
    suffix_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".avif": "image/avif",
    }
    if suffix in suffix_types:
        return suffix_types[suffix]
    raise ImageIngestionError("UNSUPPORTED_FORMAT", f"{filename} 仅支持 JPG、PNG、WebP、HEIC/HEIF 或 AVIF 图片")


def inspect_image(content: bytes, filename: str, max_pixels: int) -> InspectedImage:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                image_format = (image.format or "").upper()
                width, height = image.size
                frames = int(getattr(image, "n_frames", 1) or 1)
                if image_format not in SUPPORTED_IMAGE_FORMATS:
                    raise ImageIngestionError(
                        "UNSUPPORTED_FORMAT",
                        f"{filename} 仅支持 JPG、PNG、WebP、HEIC/HEIF 或 AVIF 图片",
                    )
                if frames > 1 or bool(getattr(image, "is_animated", False)):
                    raise ImageIngestionError("ANIMATED_IMAGE", f"{filename} 是动态图，当前仅支持静态图片")
                if width <= 0 or height <= 0:
                    raise ImageIngestionError("INVALID_DIMENSIONS", f"{filename} 的图片尺寸无效")
                if width * height > max_pixels:
                    max_mp = max_pixels / 1_000_000
                    raise ImageIngestionError("PIXEL_LIMIT", f"{filename} 超过 {max_mp:.0f}MP 像素限制")
                image.verify()
    except ImageIngestionError:
        raise
    except Image.DecompressionBombError as exc:
        max_mp = max_pixels / 1_000_000
        raise ImageIngestionError("PIXEL_LIMIT", f"{filename} 超过 {max_mp:.0f}MP 像素限制") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageIngestionError("DECODE_FAILED", f"{filename} 已损坏、不完整或不是有效图片") from exc
    return InspectedImage(image_format, FORMAT_MIME_TYPES[image_format], width, height)


def _to_srgb(image: Image.Image) -> Image.Image:
    icc_profile = image.info.get("icc_profile")
    if icc_profile:
        try:
            source = ImageCms.ImageCmsProfile(BytesIO(icc_profile))
            target = ImageCms.createProfile("sRGB")
            output_mode = "RGBA" if "A" in image.getbands() else "RGB"
            return ImageCms.profileToProfile(image, source, target, outputMode=output_mode)
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass
    return image.convert("RGBA" if "A" in image.getbands() else "RGB")


def normalize_image(content: bytes, max_edge: int) -> NormalizedImage:
    with Image.open(BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source)
        image.load()
        image = _to_srgb(image)
        if max(image.size) > max_edge:
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        has_alpha = "A" in image.getbands()
        output = BytesIO()
        if has_alpha:
            image.save(output, "PNG", optimize=True)
            content_type, suffix = "image/png", ".png"
        else:
            if image.mode != "RGB":
                image = image.convert("RGB")
            try:
                image.save(output, "JPEG", quality=95, subsampling=0, optimize=True)
            except OSError:
                # High-entropy images can exhaust the optimized JPEG encoder buffer.
                # A fresh buffer discards partial output without changing image quality.
                output = BytesIO()
                try:
                    image.save(output, "JPEG", quality=95, subsampling=0, optimize=False)
                except OSError as exc:
                    raise ImageIngestionError(
                        "ENCODE_FAILED", "图片转码失败，请重新导出图片后再试"
                    ) from exc
            content_type, suffix = "image/jpeg", ".jpg"
        return NormalizedImage(output.getvalue(), content_type, suffix, image.width, image.height)


def normalized_image_key(original_key: str | Path, suffix: str) -> str:
    path = Path(str(original_key))
    return str(path.parent / "_derived" / "normalized" / f"{path.stem}{suffix}")


def normalized_image_candidates(original_key: str | Path) -> list[str]:
    return [normalized_image_key(original_key, ".jpg"), normalized_image_key(original_key, ".png")]
