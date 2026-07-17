from __future__ import annotations

from app import main as app_main
from app.db import get_db, now_iso
from conftest import insert_styling_garment


def test_garment_tagging_selection_deletes_unselected_images_and_reorders_selected(monkeypatch) -> None:
    deleted_keys: list[str] = []
    monkeypatch.setattr(app_main, "delete_object", lambda key: deleted_keys.append(str(key)))
    garment_id = insert_styling_garment(code="SELECT-CLEANUP", image_count=4)

    with get_db() as db:
        before = db.execute(
            """
            SELECT sgi.id, sgi.media_asset_id, ma.object_key
            FROM styling_garment_images sgi
            JOIN media_assets ma ON ma.id = sgi.media_asset_id
            WHERE sgi.styling_garment_id = ?
            ORDER BY sgi.sort_order
            """,
            (garment_id,),
        ).fetchall()
        selected_ids = [int(before[2]["id"]), int(before[0]["id"])]
        unselected_asset_ids = {int(before[1]["media_asset_id"]), int(before[3]["media_asset_id"])}
        unselected_object_keys = {before[1]["object_key"], before[3]["object_key"]}

        app_main.apply_styling_garment_selected_image_order(db, garment_id, selected_ids, now_iso())

        remaining = db.execute(
            """
            SELECT id, sort_order
            FROM styling_garment_images
            WHERE styling_garment_id = ?
            ORDER BY sort_order
            """,
            (garment_id,),
        ).fetchall()
        remaining_asset_ids = {
            int(row["id"])
            for row in db.execute("SELECT id FROM media_assets").fetchall()
        }

    assert [(int(row["id"]), int(row["sort_order"])) for row in remaining] == [(selected_ids[0], 0), (selected_ids[1], 1)]
    assert unselected_asset_ids.isdisjoint(remaining_asset_ids)
    assert unselected_object_keys.issubset(set(deleted_keys))

