from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import get_db, init_db
from app.image_derivatives import ensure_image_derivatives
from app.object_storage import object_size


def repair_garment_images(limit: int | None) -> int:
    query = """
        SELECT id, storage_path
        FROM garment_images
        WHERE storage_path IS NOT NULL
          AND (thumb_path IS NULL OR preview_path IS NULL OR file_size IS NULL
               OR thumb_file_size IS NULL OR preview_file_size IS NULL)
        ORDER BY id
    """
    params: tuple[int, ...] = ()
    if limit:
        query += " LIMIT ?"
        params = (limit,)
    with get_db() as db:
        rows = db.execute(query, params).fetchall()
        for row in rows:
            metadata = media_metadata(row["storage_path"])
            db.execute(
                """
                UPDATE garment_images
                SET thumb_path = ?, preview_path = ?, file_size = ?,
                    thumb_file_size = ?, preview_file_size = ?
                WHERE id = ?
                """,
                (
                    metadata["thumbPath"],
                    metadata["previewPath"],
                    metadata["fileSize"],
                    metadata["thumbFileSize"],
                    metadata["previewFileSize"],
                    row["id"],
                ),
            )
        return len(rows)


def repair_generation_images(limit: int | None) -> int:
    query = """
        SELECT id, output_path
        FROM generation_images
        WHERE status = 'SUCCEEDED'
          AND output_path IS NOT NULL
          AND (thumb_path IS NULL OR preview_path IS NULL OR file_size IS NULL
               OR thumb_file_size IS NULL OR preview_file_size IS NULL)
        ORDER BY id
    """
    params: tuple[int, ...] = ()
    if limit:
        query += " LIMIT ?"
        params = (limit,)
    with get_db() as db:
        rows = db.execute(query, params).fetchall()
        for row in rows:
            metadata = media_metadata(row["output_path"])
            db.execute(
                """
                UPDATE generation_images
                SET thumb_path = ?, preview_path = ?, file_size = ?,
                    thumb_file_size = ?, preview_file_size = ?
                WHERE id = ?
                """,
                (
                    metadata["thumbPath"],
                    metadata["previewPath"],
                    metadata["fileSize"],
                    metadata["thumbFileSize"],
                    metadata["previewFileSize"],
                    row["id"],
                ),
            )
        return len(rows)


def media_metadata(original_key: str) -> dict[str, str | int | None]:
    derivative = ensure_image_derivatives(original_key)
    thumb_path = derivative.get("thumb")
    preview_path = derivative.get("preview")
    return {
        "thumbPath": thumb_path,
        "previewPath": preview_path,
        "fileSize": object_size(original_key),
        "thumbFileSize": object_size(thumb_path) if thumb_path else None,
        "previewFileSize": object_size(preview_path) if preview_path else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill image derivative paths and file sizes into SQLite.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to repair per table.")
    args = parser.parse_args()
    init_db()
    garment_count = repair_garment_images(args.limit)
    generation_count = repair_generation_images(args.limit)
    print(f"repaired garment_images={garment_count} generation_images={generation_count}")


if __name__ == "__main__":
    main()
