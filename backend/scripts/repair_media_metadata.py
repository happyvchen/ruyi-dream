from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import get_db, init_db, now_iso
from app.image_derivatives import ensure_image_derivatives
from app.object_storage import object_size


def repair_media_assets(limit: int | None) -> int:
    query = """
        SELECT id, object_key
        FROM media_assets
        WHERE object_key IS NOT NULL
          AND deleted_at IS NULL
          AND (thumb_key IS NULL OR preview_key IS NULL OR size_bytes IS NULL
               OR thumb_size_bytes IS NULL OR preview_size_bytes IS NULL)
        ORDER BY id
    """
    params: tuple[int, ...] = ()
    if limit:
        query += " LIMIT ?"
        params = (limit,)
    with get_db() as db:
        rows = db.execute(query, params).fetchall()
        for row in rows:
            metadata = media_metadata(row["object_key"])
            db.execute(
                """
                UPDATE media_assets
                SET thumb_key = ?, preview_key = ?, size_bytes = ?,
                    thumb_size_bytes = ?, preview_size_bytes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    metadata["thumbKey"],
                    metadata["previewKey"],
                    metadata["sizeBytes"],
                    metadata["thumbSizeBytes"],
                    metadata["previewSizeBytes"],
                    metadata["updatedAt"],
                    row["id"],
                ),
            )
        return len(rows)


def media_metadata(original_key: str) -> dict[str, str | int | None]:
    derivative = ensure_image_derivatives(original_key)
    thumb_key = derivative.get("thumb")
    preview_key = derivative.get("preview")
    return {
        "thumbKey": thumb_key,
        "previewKey": preview_key,
        "sizeBytes": object_size(original_key),
        "thumbSizeBytes": object_size(thumb_key) if thumb_key else None,
        "previewSizeBytes": object_size(preview_key) if preview_key else None,
        "updatedAt": now_iso(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill media asset derivative keys and file sizes into SQLite.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to repair per table.")
    args = parser.parse_args()
    init_db()
    media_count = repair_media_assets(args.limit)
    print(f"repaired media_assets={media_count}")


if __name__ == "__main__":
    main()
