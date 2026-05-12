from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .object_storage import clean_key, get_bytes, join_key, object_exists, put_bytes


THUMB_MAX_EDGE = 240
PREVIEW_MAX_EDGE = 1280
DERIVATIVE_FORMAT = "WEBP"
DERIVATIVE_CONTENT_TYPE = "image/webp"


def derivative_key(original_key: str | Path | None, kind: str) -> str | None:
    if not original_key:
        return None
    original_key = clean_key(original_key)
    path = Path(original_key)
    if kind not in {"thumb", "preview"}:
        raise ValueError(f"Unknown derivative kind: {kind}")
    return join_key(path.parent.as_posix(), "_derived", kind, f"{path.stem}.webp")


def existing_derivative_or_original(original_key: str | Path | None, kind: str) -> str | None:
    if not original_key:
        return None
    ensure_image_derivatives(original_key)
    key = derivative_key(original_key, kind)
    if key and object_exists(key):
        return key
    return clean_key(original_key)


def ensure_image_derivatives(original_key: str | Path | None) -> dict[str, Any]:
    if not original_key:
        return {}
    thumb_key = derivative_key(original_key, "thumb")
    preview_key = derivative_key(original_key, "preview")
    if thumb_key and preview_key and object_exists(thumb_key) and object_exists(preview_key):
        return {"thumb": thumb_key, "preview": preview_key}
    try:
        content = get_bytes(original_key)
        return generate_image_derivatives(original_key, content)
    except Exception:
        return {}


def generate_image_derivatives(original_key: str | Path, content: bytes) -> dict[str, Any]:
    thumb_key = derivative_key(original_key, "thumb")
    preview_key = derivative_key(original_key, "preview")
    if not thumb_key or not preview_key:
        return {}

    thumb = resize_to_webp(content, THUMB_MAX_EDGE)
    preview = resize_to_webp(content, PREVIEW_MAX_EDGE)
    put_bytes(thumb_key, thumb, DERIVATIVE_CONTENT_TYPE)
    put_bytes(preview_key, preview, DERIVATIVE_CONTENT_TYPE)
    return {
        "thumb": thumb_key,
        "preview": preview_key,
        "thumbSize": len(thumb),
        "previewSize": len(preview),
    }


def resize_to_webp(content: bytes, max_edge: int) -> bytes:
    try:
        with Image.open(BytesIO(content)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = BytesIO()
            image.save(output, DERIVATIVE_FORMAT, quality=82, method=6)
            return output.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("无法生成图片缩略图") from exc
