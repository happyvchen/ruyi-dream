from __future__ import annotations

from app import main as app_main
from app.db import dumps, get_db, now_iso
from conftest import insert_generation_task, insert_media_asset, insert_ready_generation_task_with_preview, insert_styling_garment


REAL_CLEANUP_DELETED_MEDIA_OBJECTS = app_main.cleanup_deleted_media_objects


def test_batch_archive_handles_one_hundred_tasks_and_hides_them_by_default(client) -> None:
    task_ids = [insert_generation_task(name=f"LOOK-{index:03d}", status="DRAFT") for index in range(100)]

    response = client.post("/api/generation-tasks/batch-archive", json={"ids": task_ids})

    assert response.status_code == 200
    assert response.json()["ids"] == task_ids
    assert client.get("/api/generation-tasks?limit=100").json()["items"] == []

    archived = client.get("/api/generation-tasks?includeArchived=true&limit=100").json()["items"]
    assert len(archived) == 100
    assert {item["archiveReason"] for item in archived} == {"user_archived"}


def test_batch_purge_requires_explicit_phrase_before_deleting_one_hundred_tasks(client) -> None:
    task_ids = [insert_generation_task(name=f"PURGE-{index:03d}", status="DRAFT") for index in range(100)]

    rejected = client.post(
        "/api/generation-tasks/batch-purge",
        json={"ids": task_ids, "purge": True, "confirmPhrase": "删除"},
    )
    assert rejected.status_code == 400

    accepted = client.post(
        "/api/generation-tasks/batch-purge",
        json={"ids": task_ids, "purge": True, "confirmPhrase": "全部删除"},
    )

    assert accepted.status_code == 200
    assert accepted.json()["deletedTaskIds"] == task_ids
    with get_db() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS count FROM generation_tasks WHERE deleted_at IS NULL"
        ).fetchone()["count"]
    assert remaining == 0


def test_purge_failure_keeps_task_archived_for_refresh_recovery(client, monkeypatch) -> None:
    task_id = insert_generation_task(name="DELETE-REFRESH", status="DRAFT")

    def fail_purge(*args, **kwargs):
        raise RuntimeError("simulated storage interruption")

    from app import main as app_main

    monkeypatch.setattr(app_main, "purge_generation_tasks", fail_purge)
    response = client.request(
        "DELETE",
        f"/api/generation-tasks/{task_id}",
        json={"ids": [task_id], "purge": True, "confirmName": "DELETE-REFRESH"},
    )

    assert response.status_code == 409
    after_refresh = client.get(f"/api/generation-tasks/{task_id}").json()["item"]
    assert after_refresh["archiveReason"] == "deletion_failed"
    assert after_refresh["archivedAt"]


def test_delete_impact_counts_task_artifacts_before_dangerous_purge(client) -> None:
    ids = [insert_generation_task(name=f"IMPACT-{index}", status="DRAFT", image_count=2) for index in range(3)]

    response = client.post("/api/generation-tasks/delete-impact", json={"ids": ids})

    assert response.status_code == 200
    impact = response.json()["impact"]
    assert impact["taskCount"] == 3
    assert impact["mediaAssetCount"] == 6
    assert impact["previewCount"] == 0


def test_styling_garment_archive_restore_and_batch_purge_use_business_confirmations(client) -> None:
    garment_ids = [insert_styling_garment(code=f"STYLE-{index:03d}") for index in range(3)]

    archive = client.post("/api/styling-garments/batch-delete", json={"ids": garment_ids})
    assert archive.status_code == 200
    assert archive.json()["archived"] is True

    restore = client.post("/api/styling-garments/batch-restore", json={"ids": garment_ids})
    assert restore.status_code == 200
    assert restore.json()["restored"] is True

    rejected = client.post(
        "/api/styling-garments/batch-delete",
        json={"ids": garment_ids, "purge": True, "confirmPhrase": "确认"},
    )
    assert rejected.status_code == 400

    purged = client.post(
        "/api/styling-garments/batch-delete",
        json={"ids": garment_ids, "purge": True, "confirmPhrase": "全部删除"},
    )
    assert purged.status_code == 200
    assert purged.json()["deletedGarmentIds"] == garment_ids


def test_purging_styling_garment_preserves_referencing_generation_history(client, monkeypatch) -> None:
    garment_id = insert_styling_garment(code="HISTORY-GARMENT")
    task = insert_ready_generation_task_with_preview(family="top", status="COMPLETED")
    final_asset_id = insert_media_asset("history-final.png", f"generation-tasks/{task['task_id']}/final.png")
    timestamp = now_iso()
    with get_db() as db:
        garment = client.get(f"/api/styling-garments/{garment_id}").json()["item"]
        garment_asset_id = int(garment["images"][0]["asset"]["id"])
        garment_object_key = str(garment["images"][0]["asset"]["objectKey"])
        db.execute(
            """
            UPDATE generation_stylings
            SET selected_material_ids_json = ?, selected_material_snapshot_json = ?
            WHERE id = ?
            """,
            (dumps([garment_id]), dumps([garment]), task["styling_id"]),
        )
        db.execute(
            "UPDATE generation_stylings SET reason_summary = ? WHERE id = ?",
            ("使用 HISTORY-GARMENT 完成历史造型", task["styling_id"]),
        )
        result_set = db.execute(
            """
            INSERT INTO generation_result_sets
              (task_id, input_version_id, styling_id, preview_id, user_instruction_json, status, created_at, updated_at)
            VALUES (?, (SELECT current_input_version_id FROM generation_tasks WHERE id = ?), ?, ?, '{}', 'COMPLETED', ?, ?)
            """,
            (task["task_id"], task["task_id"], task["styling_id"], task["preview_id"], timestamp, timestamp),
        )
        result_set_id = int(result_set.lastrowid)
        db.execute(
            """
            INSERT INTO generation_result_images_new
              (result_set_id, shot_role, sort_order, status, current_asset_id, created_at, updated_at)
            VALUES (?, 'hero', 1, 'SUCCEEDED', ?, ?, ?)
            """,
            (result_set_id, final_asset_id, timestamp, timestamp),
        )
        db.execute(
            "UPDATE generation_tasks SET active_result_set_id = ?, status = 'COMPLETED', updated_at = ? WHERE id = ?",
            (result_set_id, timestamp, task["task_id"]),
        )

    impact = client.post("/api/styling-garments/delete-impact", json={"ids": [garment_id]}).json()["impact"]
    assert impact["referencingTaskCount"] == 1
    assert impact["taskCount"] == 0
    assert impact["resultImageCount"] == 0
    assert impact["mediaAssetCount"] == 0
    assert impact["estimatedBytes"] == 0

    deleted_keys: list[str] = []
    monkeypatch.setattr(app_main, "cleanup_deleted_media_objects", REAL_CLEANUP_DELETED_MEDIA_OBJECTS)
    monkeypatch.setattr(app_main, "delete_object", deleted_keys.append)

    response = client.delete(
        f"/api/styling-garments/{garment_id}",
        params={"purge": "true", "confirmCode": "HISTORY-GARMENT"},
    )

    assert response.status_code == 200
    assert response.json()["deletedTaskIds"] == []
    assert response.json()["referencingTaskIds"] == [task["task_id"]]
    preserved = client.get(f"/api/generation-tasks/{task['task_id']}")
    assert preserved.status_code == 200
    item = preserved.json()["item"]
    assert item["images"]
    assert item["storyboardPreview"]["asset"]
    assert item["resultSet"]["images"][0]["asset"]["id"] == final_asset_id
    assert item["styling"]["selectedMaterials"] == [{
        "id": garment_id,
        "code": "",
        "status": "DELETED",
        "sourceAvailable": False,
    }]
    assert "HISTORY-GARMENT" not in str(item["styling"])
    with get_db() as db:
        assert db.execute("SELECT id FROM media_assets WHERE id = ?", (garment_asset_id,)).fetchone()
    assert garment_object_key not in deleted_keys


def test_purging_styling_garment_does_not_delete_media_used_by_another_garment(client, monkeypatch) -> None:
    first_id = insert_styling_garment(code="SHARED-MEDIA-A")
    second_id = insert_styling_garment(code="SHARED-MEDIA-B")
    with get_db() as db:
        first_asset = db.execute(
            "SELECT media_asset_id FROM styling_garment_images WHERE styling_garment_id = ?",
            (first_id,),
        ).fetchone()["media_asset_id"]
        second_asset = db.execute(
            "SELECT media_asset_id FROM styling_garment_images WHERE styling_garment_id = ?",
            (second_id,),
        ).fetchone()["media_asset_id"]
        db.execute(
            "UPDATE styling_garment_images SET media_asset_id = ? WHERE styling_garment_id = ?",
            (first_asset, second_id),
        )
        db.execute("DELETE FROM media_assets WHERE id = ?", (second_asset,))

    deleted_keys: list[str] = []
    monkeypatch.setattr(app_main, "cleanup_deleted_media_objects", REAL_CLEANUP_DELETED_MEDIA_OBJECTS)
    monkeypatch.setattr(app_main, "delete_object", deleted_keys.append)

    impact = client.post("/api/styling-garments/delete-impact", json={"ids": [first_id]}).json()["impact"]
    assert impact["garmentImageCount"] == 1
    assert impact["mediaAssetCount"] == 0
    assert impact["estimatedBytes"] == 0

    response = client.delete(
        f"/api/styling-garments/{first_id}",
        params={"purge": "true", "confirmCode": "SHARED-MEDIA-A"},
    )

    assert response.status_code == 200
    remaining = client.get(f"/api/styling-garments/{second_id}").json()["item"]
    assert remaining["images"][0]["asset"]["id"] == first_asset
    assert deleted_keys == []


def test_legacy_snapshot_without_selected_ids_still_becomes_a_deleted_source_tombstone(client) -> None:
    garment_id = insert_styling_garment(code="LEGACY-SNAPSHOT")
    task = insert_ready_generation_task_with_preview(family="top", status="STYLING_READY")
    garment = client.get(f"/api/styling-garments/{garment_id}").json()["item"]
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_stylings
            SET selected_material_ids_json = '[]', selected_material_snapshot_json = ?
            WHERE id = ?
            """,
            (dumps([garment]), task["styling_id"]),
        )

    impact = client.post("/api/styling-garments/delete-impact", json={"ids": [garment_id]}).json()["impact"]
    assert impact["referencingTaskIds"] == [task["task_id"]]

    response = client.delete(
        f"/api/styling-garments/{garment_id}",
        params={"purge": "true", "confirmCode": "LEGACY-SNAPSHOT"},
    )

    assert response.status_code == 200
    styling = client.get(f"/api/generation-tasks/{task['task_id']}").json()["item"]["styling"]
    assert styling["selectedMaterialIds"] == [garment_id]
    assert styling["selectedMaterials"] == [{
        "id": garment_id,
        "code": "",
        "status": "DELETED",
        "sourceAvailable": False,
    }]


def test_restore_rejects_records_that_are_pending_permanent_deletion(client) -> None:
    garment_ids = [insert_styling_garment(code=f"RESTORE-GUARD-{index}") for index in range(2)]
    task_ids = [insert_generation_task(name=f"RESTORE-GUARD-{index}", status="DRAFT") for index in range(2)]
    with get_db() as db:
        db.execute(
            "UPDATE styling_garments SET archived_at = '2026-07-30T00:00:00Z', archive_reason = 'user_archived' WHERE id = ?",
            (garment_ids[0],),
        )
        db.execute(
            "UPDATE styling_garments SET archived_at = '2026-07-30T00:00:00Z', archive_reason = 'deletion_pending' WHERE id = ?",
            (garment_ids[1],),
        )
        db.execute(
            "UPDATE generation_tasks SET archived_at = '2026-07-30T00:00:00Z', archive_reason = 'user_archived' WHERE id = ?",
            (task_ids[0],),
        )
        db.execute(
            "UPDATE generation_tasks SET archived_at = '2026-07-30T00:00:00Z', archive_reason = 'deletion_pending' WHERE id = ?",
            (task_ids[1],),
        )

    assert client.post(f"/api/styling-garments/{garment_ids[1]}/restore").status_code == 409
    assert client.post("/api/styling-garments/batch-restore", json={"ids": garment_ids}).status_code == 409
    assert client.post(f"/api/generation-tasks/{task_ids[1]}/restore").status_code == 409
    assert client.post("/api/generation-tasks/batch-restore", json={"ids": task_ids}).status_code == 409

    with get_db() as db:
        garment_rows = db.execute(
            "SELECT archive_reason FROM styling_garments WHERE id IN (?, ?) ORDER BY id",
            garment_ids,
        ).fetchall()
        task_rows = db.execute(
            "SELECT archive_reason FROM generation_tasks WHERE id IN (?, ?) ORDER BY id",
            task_ids,
        ).fetchall()
    assert [row["archive_reason"] for row in garment_rows] == ["user_archived", "deletion_pending"]
    assert [row["archive_reason"] for row in task_rows] == ["user_archived", "deletion_pending"]


def test_purging_one_direct_uploaded_garment_never_deletes_a_shared_upload_prefix(client, monkeypatch) -> None:
    first_id = insert_styling_garment(code="SESSION-A")
    second_id = insert_styling_garment(code="SESSION-B")
    first_key = "direct-uploads/styling-garment/session-a/001-first.jpg"
    second_key = "direct-uploads/styling-garment/session-b/001-second.jpg"
    with get_db() as db:
        db.execute(
            """
            UPDATE media_assets SET object_key = ?
            WHERE id = (SELECT media_asset_id FROM styling_garment_images WHERE styling_garment_id = ?)
            """,
            (first_key, first_id),
        )
        db.execute(
            """
            UPDATE media_assets SET object_key = ?
            WHERE id = (SELECT media_asset_id FROM styling_garment_images WHERE styling_garment_id = ?)
            """,
            (second_key, second_id),
        )

    deleted_keys: list[str] = []
    deleted_prefixes: list[str] = []
    monkeypatch.setattr(app_main, "cleanup_deleted_media_objects", REAL_CLEANUP_DELETED_MEDIA_OBJECTS)
    monkeypatch.setattr(app_main, "delete_object", deleted_keys.append)
    monkeypatch.setattr(app_main, "delete_prefix", deleted_prefixes.append)

    response = client.delete(
        f"/api/styling-garments/{first_id}",
        params={"purge": "true", "confirmCode": "SESSION-A"},
    )

    assert response.status_code == 200
    assert first_key in deleted_keys
    assert "direct-uploads/styling-garment/session-a/_derived/final-cells/001-first-shot1.png" in deleted_keys
    assert all("session-b" not in key for key in deleted_keys)
    assert deleted_prefixes == []


def test_purge_reports_exact_object_cleanup_failures(client, monkeypatch) -> None:
    garment_id = insert_styling_garment(code="CLEANUP-FAIL")
    monkeypatch.setattr(app_main, "cleanup_deleted_media_objects", REAL_CLEANUP_DELETED_MEDIA_OBJECTS)
    monkeypatch.setattr(app_main, "delete_object", lambda key: (_ for _ in ()).throw(RuntimeError("rustfs unavailable")))

    response = client.delete(
        f"/api/styling-garments/{garment_id}",
        params={"purge": "true", "confirmCode": "CLEANUP-FAIL"},
    )

    assert response.status_code == 200
    errors = response.json()["objectCleanupErrors"]
    assert errors
    assert errors[0]["objectKey"].startswith("styling-garments/")
    assert errors[0]["error"] == "rustfs unavailable"


def test_running_tasks_cannot_be_archived_or_purged_during_refresh_sensitive_operations(client) -> None:
    task_id = insert_generation_task(name="RUNNING", status="FINAL_RUNNING")

    archive = client.post(f"/api/generation-tasks/{task_id}/archive")
    purge = client.request(
        "DELETE",
        f"/api/generation-tasks/{task_id}",
        json={"ids": [task_id], "purge": True, "confirmName": "RUNNING"},
    )

    assert archive.status_code == 409
    assert purge.status_code == 409
