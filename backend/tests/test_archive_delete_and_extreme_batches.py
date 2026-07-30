from __future__ import annotations

from app import main as app_main
from app.db import get_db
from conftest import insert_generation_task, insert_styling_garment


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
