from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import pytest

from app import main as app_main
from app.db import dumps, get_db, now_iso
from conftest import insert_ready_generation_task_with_preview, insert_styling_garment


def create_result_set(task_id: int, styling_id: int, preview_id: int, statuses: list[str]) -> tuple[int, list[int]]:
    timestamp = now_iso()
    with get_db() as db:
        input_version = db.execute(
            "SELECT current_input_version_id FROM generation_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()["current_input_version_id"]
        result_set = db.execute(
            """
            INSERT INTO generation_result_sets
              (task_id, input_version_id, styling_id, preview_id, user_instruction_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, '{}', 'PARTIAL', ?, ?)
            """,
            (task_id, input_version, styling_id, preview_id, timestamp, timestamp),
        )
        result_set_id = int(result_set.lastrowid)
        image_ids: list[int] = []
        for index, status in enumerate(statuses, start=1):
            asset_id = None
            if status == "SUCCEEDED":
                asset = db.execute(
                    """
                    INSERT INTO media_assets
                      (object_key, original_filename, mime_type, width, height, size_bytes, created_at, updated_at)
                    VALUES (?, ?, 'image/jpeg', 1200, 1600, 128, ?, ?)
                    """,
                    (f"outputs/{task_id}/{index}.png", f"final-{index}.png", timestamp, timestamp),
                )
                asset_id = int(asset.lastrowid)
            cursor = db.execute(
                """
                INSERT INTO generation_result_images_new
                  (result_set_id, shot_role, sort_order, status, current_asset_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (result_set_id, f"top_grid_{index}", index, status, asset_id, timestamp, timestamp),
            )
            image_ids.append(int(cursor.lastrowid))
        db.execute(
            "UPDATE generation_tasks SET active_result_set_id = ?, status = 'FINAL_PARTIAL', updated_at = ? WHERE id = ?",
            (result_set_id, timestamp, task_id),
        )
    return result_set_id, image_ids


def test_reordering_hero_image_after_ai_stage_invalidates_current_chain_but_keeps_history(client) -> None:
    ids = insert_ready_generation_task_with_preview(family="top")
    create_result_set(ids["task_id"], ids["styling_id"], ids["preview_id"], ["SUCCEEDED", "FAILED", "FAILED", "FAILED", "FAILED"])

    task = client.get(f"/api/generation-tasks/{ids['task_id']}").json()["item"]
    image_id = task["images"][0]["id"]
    response = client.patch(f"/api/generation-task-images/{image_id}", json={"sortOrder": 99})

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["status"] == "READY_FOR_STYLING"
    assert item["activeStylingId"] is None
    assert item["activeStoryboardPreviewId"] is None
    assert item["activeResultSetId"] is None

    with get_db() as db:
        styling_status = db.execute("SELECT status FROM generation_stylings WHERE id = ?", (ids["styling_id"],)).fetchone()["status"]
        preview_status = db.execute("SELECT status FROM generation_previews WHERE id = ?", (ids["preview_id"],)).fetchone()["status"]
        result_status = db.execute("SELECT status FROM generation_result_sets WHERE task_id = ?", (ids["task_id"],)).fetchone()["status"]
    assert styling_status == "SUPERSEDED"
    assert preview_status == "SUPERSEDED"
    assert result_status == "SUPERSEDED"


def test_created_generation_task_rejects_append_images_so_top_entry_starts_new_product(client) -> None:
    ids = insert_ready_generation_task_with_preview(family="top")

    response = client.post(f"/api/generation-tasks/{ids['task_id']}/images/from-uploads", json={"uploads": []})

    assert response.status_code == 410
    assert "不支持继续添加主推款图片" in response.json()["detail"]


def test_created_generation_task_accepts_inline_append_up_to_eight_images(client, monkeypatch) -> None:
    ids = insert_ready_generation_task_with_preview(family="top")

    async def fake_save(_uploads):
        timestamp = now_iso()
        with get_db() as db:
            asset_id = int(db.execute(
                """
                INSERT INTO media_assets
                  (object_key, original_filename, mime_type, width, height, size_bytes, created_at, updated_at)
                VALUES ('tests/hero-added.jpg', 'hero-added.jpg', 'image/jpeg', 1200, 1600, 128, ?, ?)
                """,
                (timestamp, timestamp),
            ).lastrowid)
        return [{"id": asset_id, "objectKey": "tests/hero-added.jpg"}]

    monkeypatch.setattr(app_main, "save_uploads_as_media_assets", fake_save)
    response = client.post(
        f"/api/generation-tasks/{ids['task_id']}/images",
        files={"files": ("hero-added.jpg", b"image", "image/jpeg")},
    )

    assert response.status_code == 200
    item = response.json()["item"]
    assert len(item["images"]) == 3
    assert item["images"][-1]["asset"]["originalFilename"] == "hero-added.jpg"
    assert item["status"] == "READY_FOR_STYLING"


def test_styling_garment_accepts_inline_append_up_to_six_images(client, monkeypatch) -> None:
    garment_id = insert_styling_garment(code="ADD-IMAGE", image_count=1)

    async def fake_save(_uploads):
        timestamp = now_iso()
        with get_db() as db:
            asset_id = int(db.execute(
                """
                INSERT INTO media_assets
                  (object_key, original_filename, mime_type, width, height, size_bytes, created_at, updated_at)
                VALUES ('tests/garment-added.jpg', 'garment-added.jpg', 'image/jpeg', 1200, 1600, 128, ?, ?)
                """,
                (timestamp, timestamp),
            ).lastrowid)
        return [{"id": asset_id, "objectKey": "tests/garment-added.jpg"}]

    monkeypatch.setattr(app_main, "save_uploads_as_media_assets", fake_save)
    response = client.post(
        f"/api/styling-garments/{garment_id}/images",
        files={"files": ("garment-added.jpg", b"image", "image/jpeg")},
    )

    assert response.status_code == 200
    assert len(response.json()["item"]["images"]) == 2


def test_export_precheck_reports_complete_partial_and_missing_final_images(client) -> None:
    complete = insert_ready_generation_task_with_preview(family="top")
    partial = insert_ready_generation_task_with_preview(family="top")
    missing = insert_ready_generation_task_with_preview(family="top")
    create_result_set(complete["task_id"], complete["styling_id"], complete["preview_id"], ["SUCCEEDED"] * 5)
    create_result_set(partial["task_id"], partial["styling_id"], partial["preview_id"], ["SUCCEEDED", "FAILED", "QUEUED", "CANCELLED", "SUCCEEDED"])

    response = client.post(
        "/api/generation-task-exports/precheck",
        json={"ids": [complete["task_id"], partial["task_id"], missing["task_id"]]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["totalImages"] == 7
    assert payload["completeTaskIds"] == [complete["task_id"]]
    assert set(payload["downloadableTaskIds"]) == {complete["task_id"], partial["task_id"]}
    by_id = {item["taskId"]: item for item in payload["items"]}
    assert by_id[complete["task_id"]]["downloadableCount"] == 5
    assert by_id[partial["task_id"]]["downloadableCount"] == 2
    assert [issue["status"] for issue in by_id[partial["task_id"]]["issues"]] == ["FAILED", "QUEUED", "CANCELLED"]
    assert by_id[missing["task_id"]]["hasResultSet"] is False


def test_export_precheck_deduplicates_ids_and_enforces_selection(client) -> None:
    ids = insert_ready_generation_task_with_preview(family="top")
    create_result_set(ids["task_id"], ids["styling_id"], ids["preview_id"], ["SUCCEEDED"] * 5)

    deduped = client.post("/api/generation-task-exports/precheck", json={"ids": [ids["task_id"], ids["task_id"]]})
    empty = client.post("/api/generation-task-exports/precheck", json={"ids": []})

    assert deduped.status_code == 200
    assert len(deduped.json()["items"]) == 1
    assert empty.status_code == 400


def test_styling_garment_export_downloads_only_original_images_in_code_folders(client) -> None:
    first = insert_styling_garment(code="LOOK-001", status="TAGGING_RUNNING", image_count=2)
    second = insert_styling_garment(code="LOOK-002", status="READY", image_count=1)
    with get_db() as db:
        first_images = db.execute(
            """
            SELECT ma.id FROM styling_garment_images sgi
            JOIN media_assets ma ON ma.id = sgi.media_asset_id
            WHERE sgi.styling_garment_id = ? ORDER BY sgi.sort_order
            """,
            (first,),
        ).fetchall()
        db.execute("UPDATE media_assets SET original_filename = 'front.jpg' WHERE id IN (?, ?)", (first_images[0]["id"], first_images[1]["id"]))

    response = client.get("/api/styling-garment-exports/archive", params={"ids": f"{first},{second}"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(response.content)) as archive:
        assert archive.namelist() == [
            "LOOK-001/front.jpg",
            "LOOK-001/front(2).jpg",
            "LOOK-002/LOOK-002-1.jpg",
        ]
        assert all(archive.read(name) == b"test-image-bytes" for name in archive.namelist())
        assert all(not name.endswith((".json", ".txt")) for name in archive.namelist())


def test_styling_garment_export_validates_selection_and_formal_images(client, monkeypatch) -> None:
    garment_id = insert_styling_garment(code="EMPTY-LOOK", image_count=0)
    broken_id = insert_styling_garment(code="BROKEN-LOOK", image_count=1)

    empty_selection = client.get("/api/styling-garment-exports/archive", params={"ids": ""})
    empty_garment = client.get("/api/styling-garment-exports/archive", params={"ids": str(garment_id)})
    missing = client.get("/api/styling-garment-exports/archive", params={"ids": "999999"})
    monkeypatch.setattr("app.main.get_bytes", lambda _key: (_ for _ in ()).throw(OSError("object unavailable")))
    unreadable = client.get("/api/styling-garment-exports/archive", params={"ids": str(broken_id)})

    assert empty_selection.status_code == 400
    assert empty_garment.status_code == 400
    assert "没有可下载的图片" in empty_garment.json()["detail"]
    assert missing.status_code == 404
    assert unreadable.status_code == 502
    assert "读取搭配服装原图失败" in unreadable.json()["detail"]


@pytest.mark.parametrize("provider", ["toapis", "apiyi"])
def test_redeliver_failed_image_reuses_provider_request_without_new_generation_submission(client, provider) -> None:
    ids = insert_ready_generation_task_with_preview(family="top")
    _, image_ids = create_result_set(ids["task_id"], ids["styling_id"], ids["preview_id"], ["FAILED", "SUCCEEDED", "SUCCEEDED", "SUCCEEDED", "SUCCEEDED"])
    timestamp = now_iso()
    with get_db() as db:
        attempt = db.execute(
            """
            INSERT INTO generation_attempts
              (owner_type, owner_id, provider, provider_request_id, status, request_snapshot_json,
               response_snapshot_json, created_at, updated_at)
            VALUES ('generation_result_image', ?, 'image-2', 'provider-task-1', 'FAILED', ?, ?, ?, ?)
            """,
            (
                image_ids[0],
                dumps({"invocation": {"mode": "original", "target": "final_image"}}),
                dumps({"ok": True}),
                timestamp,
                timestamp,
            ),
        )
        db.execute(
            "UPDATE generation_result_images_new SET active_attempt_id = ? WHERE id = ?",
            (int(attempt.lastrowid), image_ids[0]),
        )
        if provider == "apiyi":
            db.execute("UPDATE generation_attempts SET provider_request_id = NULL, response_snapshot_json = ? WHERE id = ?", (
                dumps({"provider": "apiyi", "status": "SUCCEEDED", "providerStatusResponse": {"data": [{"b64_json": "cGFpZA=="}]}}),
                int(attempt.lastrowid),
            ))

    response = client.post(f"/api/generation-result-images-new/{image_ids[0]}/redeliver")

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["status"] == "FINAL_RUNNING"
    with get_db() as db:
        latest = db.execute(
            "SELECT * FROM generation_attempts WHERE owner_type = 'generation_result_image' AND owner_id = ? ORDER BY id DESC LIMIT 1",
            (image_ids[0],),
        ).fetchone()
        queued = db.execute("SELECT status, active_attempt_id FROM generation_result_images_new WHERE id = ?", (image_ids[0],)).fetchone()
    assert latest["provider_request_id"] == ("provider-task-1" if provider == "toapis" else None)
    assert app_main.attempt_has_provider_request_id(latest["id"])
    assert latest["retry_of_attempt_id"] is not None
    assert loads_request(latest)["invocation"]["mode"] == "manual_redeliver"
    assert queued["status"] == "QUEUED"
    assert queued["active_attempt_id"] == latest["id"]


def loads_request(row) -> dict:
    import json

    return json.loads(row["request_snapshot_json"])


def test_styling_garment_filters_cover_keywords_status_tags_and_archive_visibility(client) -> None:
    first = insert_styling_garment(code="CARDIGAN-RED", status="READY")
    second = insert_styling_garment(code="PANTS-BLUE", status="TAGGING_READY")
    archived = insert_styling_garment(code="ARCHIVED-SKIRT", status="READY")
    with get_db() as db:
        db.execute(
            "UPDATE styling_garments SET ai_tags_json = ?, effective_tags_json = ? WHERE id = ?",
            (dumps({"category": ["上装"], "colors": ["红色"], "season": ["春夏"]}), dumps({"category": ["上装"], "colors": ["红色"], "season": ["春夏"]}), first),
        )
        db.execute(
            "UPDATE styling_garments SET ai_tags_json = ?, effective_tags_json = ? WHERE id = ?",
            (dumps({"category": ["下装"], "colors": ["蓝色"], "season": ["秋冬"]}), dumps({"category": ["下装"], "colors": ["蓝色"], "season": ["秋冬"]}), second),
        )
        db.execute("UPDATE styling_garments SET archived_at = ?, archive_reason = 'user_archived' WHERE id = ?", (now_iso(), archived))

    keyword = client.get("/api/styling-garments", params={"keyword": "red，blue"})
    ready_only = client.get("/api/styling-garments", params={"status": "READY"})
    red = client.get("/api/styling-garments", params={"color": "红色"})
    with_archived = client.get("/api/styling-garments", params={"includeArchived": "true", "keyword": "ARCHIVED"})

    assert {item["code"] for item in keyword.json()["items"]} == {"CARDIGAN-RED", "PANTS-BLUE"}
    assert {item["code"] for item in ready_only.json()["items"]} == {"CARDIGAN-RED"}
    assert {item["code"] for item in red.json()["items"]} == {"CARDIGAN-RED"}
    assert {item["code"] for item in with_archived.json()["items"]} == {"ARCHIVED-SKIRT"}
