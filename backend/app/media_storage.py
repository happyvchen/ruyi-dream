from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4, uuid5

from .object_storage import clean_key, join_key


MEDIA_ROOT = "media"
GENERATED_MEDIA_NAMESPACE = UUID("1d573fc2-e00b-48c3-b7a4-5936ed09f9b6")


def new_media_original_key(filename: str, *, media_uuid: UUID | None = None) -> str:
    """Return the durable object key for one independently owned media asset."""
    suffix = Path(filename).suffix.lower()
    if not suffix:
        raise ValueError("媒体文件必须包含扩展名")
    return join_key(MEDIA_ROOT, (media_uuid or uuid4()).hex, f"original{suffix}")


def media_original_key_for_owner(owner_type: str, owner_id: int, filename: str) -> str:
    """Return a stable opaque key so a recovered generation writes the same object."""
    owner_uuid = uuid5(GENERATED_MEDIA_NAMESPACE, f"{owner_type}:{owner_id}")
    return new_media_original_key(filename, media_uuid=owner_uuid)


def is_accepted_upload_key(object_key: str) -> bool:
    """Accept current asset-scoped keys and historical direct-upload keys."""
    key = clean_key(object_key)
    parts = key.split("/")
    if key.startswith("direct-uploads/"):
        return True
    return (
        len(parts) == 3
        and parts[0] == MEDIA_ROOT
        and len(parts[1]) == 32
        and all(character in "0123456789abcdef" for character in parts[1].lower())
        and parts[2].startswith("original.")
    )


def owned_media_object_keys(original_key: str) -> list[str]:
    """Enumerate every deterministic object owned by one media record.

    Cleanup deliberately returns exact keys instead of a prefix so historical
    layouts whose parent folders are shared remain safe.
    """
    key = clean_key(original_key)
    path = Path(key)
    derived_root = path.parent / "_derived"
    return [
        key,
        str(derived_root / "thumb" / f"{path.stem}.webp"),
        str(derived_root / "preview" / f"{path.stem}.webp"),
        str(derived_root / "normalized" / f"{path.stem}.jpg"),
        str(derived_root / "normalized" / f"{path.stem}.png"),
        *[
            str(derived_root / "final-cells" / f"{path.stem}-shot{cell}.png")
            for cell in range(1, 7)
        ],
    ]
