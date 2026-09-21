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


def repair_media_assets(limit: int | None = 50, *, after_id: int = 0, active_only: bool = True, verify: bool = False) -> int:
    """Bounded, resumable scan. No object IO while holding a SQLite write lock."""
    limit = min(max(limit or 50, 1), 500)
    query = """SELECT ma.id, ma.object_key FROM media_assets ma
        WHERE ma.deleted_at IS NULL AND ma.object_key IS NOT NULL AND ma.id > ?"""
    if not verify:
        query += " AND (thumb_key IS NULL OR preview_key IS NULL OR size_bytes IS NULL OR thumb_size_bytes IS NULL OR preview_size_bytes IS NULL)"
    if active_only:
        query += """ AND (
            EXISTS (SELECT 1 FROM generation_task_images i JOIN generation_tasks t ON t.id = i.task_id WHERE i.media_asset_id = ma.id AND i.deleted_at IS NULL AND t.deleted_at IS NULL AND t.archived_at IS NULL)
            OR EXISTS (SELECT 1 FROM styling_garment_images i JOIN styling_garments g ON g.id = i.styling_garment_id WHERE i.media_asset_id = ma.id AND i.deleted_at IS NULL AND g.deleted_at IS NULL AND g.archived_at IS NULL)
            OR EXISTS (SELECT 1 FROM generation_result_images_new i JOIN generation_result_sets rs ON rs.id = i.result_set_id JOIN generation_tasks t ON t.id = rs.task_id WHERE i.current_asset_id = ma.id AND t.deleted_at IS NULL AND t.archived_at IS NULL)
            OR EXISTS (SELECT 1 FROM shot_preset_slots s WHERE s.background_asset_id = ma.id OR s.pose_asset_id = ma.id)
        )"""
    query += " ORDER BY ma.id LIMIT ?"
    with get_db() as db:
        rows = db.execute(query, (after_id, limit)).fetchall()
    repaired = 0
    for row in rows:
        try:
            metadata = media_metadata(row["object_key"])
            if not metadata["thumbKey"] or not metadata["previewKey"]:
                print(f"missing id={row['id']}")
                continue
            with get_db() as db:
                db.execute("""UPDATE media_assets SET thumb_key = ?, preview_key = ?, size_bytes = ?,
                    thumb_size_bytes = ?, preview_size_bytes = ?, updated_at = ?
                    WHERE id = ? AND object_key = ? AND deleted_at IS NULL""", (
                    metadata["thumbKey"], metadata["previewKey"], metadata["sizeBytes"],
                    metadata["thumbSizeBytes"], metadata["previewSizeBytes"], metadata["updatedAt"], row["id"], row["object_key"]))
            repaired += 1
        except Exception as error:
            # Error class only: storage exceptions can contain signed URLs.
            print(f"failed id={row['id']} error={type(error).__name__}")
    print(f"scanned={len(rows)} next_after_id={rows[-1]['id'] if rows else after_id} repaired={repaired}")
    return repaired


def media_metadata(original_key: str) -> dict[str, str | int | None]:
    derivative = ensure_image_derivatives(original_key)
    thumb_key = derivative.get("thumb")
    preview_key = derivative.get("preview")
    return {
        "thumbKey": thumb_key, "previewKey": preview_key, "sizeBytes": object_size(original_key),
        "thumbSizeBytes": object_size(thumb_key) if thumb_key else None,
        "previewSizeBytes": object_size(preview_key) if preview_key else None, "updatedAt": now_iso(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair media derivatives in bounded batches; use the reported cursor for the next batch.")
    parser.add_argument("--limit", type=int, default=50, help="Batch size, 1–500 (default 50).")
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--include-archived", action="store_true", help="Also scan archived/long-tail assets.")
    parser.add_argument("--verify", action="store_true", help="Verify object existence even when metadata is present.")
    args = parser.parse_args()
    init_db()
    repair_media_assets(args.limit, after_id=args.after_id, active_only=not args.include_archived, verify=args.verify)


if __name__ == "__main__":
    main()
