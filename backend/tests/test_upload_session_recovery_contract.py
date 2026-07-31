from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import sqlite3

from app.db import get_db, now_iso, run_migrations
from conftest import insert_styling_garment


def create_session(client, *, scope: str = "generation_task", filenames: list[str] | None = None) -> dict:
    files = filenames or ["front.jpg", "back.jpg"]
    response = client.post(
        "/api/upload-sessions",
        json={
            "scope": scope,
            "files": [{"filename": filename, "mimeType": "image/jpeg", "sizeBytes": 128} for filename in files],
        },
    )
    assert response.status_code == 200
    return response.json()


def test_new_upload_objects_use_one_media_uuid_namespace_per_file(client) -> None:
    created = create_session(client, filenames=["front.jpg", "detail.png"])
    keys = [item["objectKey"] for item in created["items"]]

    assert len(set(keys)) == 2
    assert re.fullmatch(r"media/[0-9a-f]{32}/original\.jpg", keys[0])
    assert re.fullmatch(r"media/[0-9a-f]{32}/original\.png", keys[1])


def test_all_upload_session_states_can_be_archived_restored_and_deleted_without_deleting_business_objects(client, monkeypatch) -> None:
    from app import main as app_main
    from conftest import insert_media_asset

    state_names = ["PENDING", "UPLOADING", "UPLOADED", "FINALIZING", "FAILED", "ABANDONED"]
    unfinished_sessions = [
        create_session(client, scope="styling_garment", filenames=[f"{status.lower()}.jpg"])
        for status in state_names
    ]
    finalized = create_session(client, scope="styling_garment", filenames=["finalized.jpg"])
    unfinished_ids = [item["session"]["id"] for item in unfinished_sessions]
    unfinished_keys = [item["items"][0]["objectKey"] for item in unfinished_sessions]
    finalized_id = finalized["session"]["id"]
    finalized_key = finalized["items"][0]["objectKey"]
    garment_id = insert_styling_garment(code="ARCHIVE-UPLOAD-LOOK")
    media_asset_id = insert_media_asset("finalized.jpg", finalized_key)
    with get_db() as db:
        for session_id, status in zip(unfinished_ids, state_names, strict=True):
            db.execute("UPDATE upload_sessions SET status = ? WHERE id = ?", (status, session_id))
        db.execute(
            "UPDATE upload_sessions SET status = 'FINALIZED', target_type = 'styling_garment', target_id = ? WHERE id = ?",
            (garment_id, finalized_id),
        )
        # Simulate the finalize window where MediaAsset exists but the upload item weak link
        # has not been written yet. Delete protection must also check object_key.
        db.execute("UPDATE upload_session_items SET status = 'READY' WHERE session_id = ?", (finalized_id,))

    all_ids = [*unfinished_ids, finalized_id]
    archived = client.post("/api/upload-sessions/archive", json={"ids": all_ids})
    assert archived.status_code == 200
    assert set(archived.json()["archivedIds"]) == set(all_ids)
    assert client.get("/api/upload-sessions", params={"scope": "styling_garment"}).json()["items"] == []
    archived_items = client.get(
        "/api/upload-sessions",
        params={"scope": "styling_garment", "includeArchived": "true"},
    ).json()["items"]
    assert {item["id"] for item in archived_items} == set(all_ids)
    assert all(item["archivedAt"] for item in archived_items)

    restored = client.post("/api/upload-sessions/restore", json={"ids": all_ids})
    assert restored.status_code == 200
    assert len(client.get("/api/upload-sessions", params={"scope": "styling_garment", "includeAbandoned": "true"}).json()["items"]) == 7

    impact = client.post("/api/upload-sessions/delete-impact", json={"ids": all_ids}).json()["impact"]
    assert impact["uploadSessionCount"] == 7
    assert impact["temporaryObjectCount"] == 6
    assert impact["linkedBusinessCount"] == 1

    deleted_keys: list[str] = []

    def delete_without_holding_the_database_write_lock(key: str) -> None:
        with get_db() as db:
            db.execute(
                "UPDATE upload_object_cleanup_queue SET error_message = 'write-during-object-delete' WHERE object_key = ?",
                (key,),
            )
        deleted_keys.append(key)

    monkeypatch.setattr(app_main, "delete_object", delete_without_holding_the_database_write_lock)
    deleted = client.request(
        "DELETE",
        "/api/upload-sessions",
        json={"ids": all_ids, "confirmPhrase": "全部删除"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["objectCleanupErrors"] == []
    assert deleted.json()["deletedIds"] == [finalized_id]
    assert set(deleted.json()["deferredIds"]) == set(unfinished_ids)
    reconciled = client.post("/api/upload-sessions/reconcile", json={"ids": all_ids})
    assert reconciled.status_code == 200
    assert set(reconciled.json()["existingIds"]) == set(unfinished_ids)
    assert set(reconciled.json()["deletionPendingIds"]) == set(unfinished_ids)
    blocked_finalize = client.post(
        f"/api/upload-sessions/{unfinished_ids[2]}/finalize-styling-garment",
        json={"code": "DELETION-MUST-BLOCK-FINALIZE"},
    )
    assert blocked_finalize.status_code == 409
    assert "正在彻底删除" in blocked_finalize.json()["detail"]
    assert not set(unfinished_keys) & set(deleted_keys)
    assert finalized_key not in deleted_keys
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) AS count FROM upload_sessions").fetchone()["count"] == 6
        db.execute("UPDATE upload_sessions SET updated_at = '2000-01-01T00:00:00Z' WHERE archive_reason = 'deletion_pending'")
        assert db.execute("SELECT id FROM styling_garments WHERE id = ?", (garment_id,)).fetchone() is not None
        assert db.execute("SELECT id FROM media_assets WHERE id = ?", (media_asset_id,)).fetchone() is not None
    assert app_main.cleanup_deleted_upload_sessions() == 6
    assert set(unfinished_keys) <= set(deleted_keys)
    assert client.post("/api/upload-sessions/reconcile", json={"ids": all_ids}).json() == {
        "existingIds": [],
        "deletionPendingIds": [],
    }
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) AS count FROM upload_sessions").fetchone()["count"] == 0


def test_upload_record_object_cleanup_failure_keeps_a_retryable_queue_entry(client, monkeypatch) -> None:
    from app import main as app_main

    created = create_session(client, filenames=["retry.jpg"])
    session_id = created["session"]["id"]
    key = created["items"][0]["objectKey"]
    with get_db() as db:
        db.execute("UPDATE upload_sessions SET status = 'FINALIZED' WHERE id = ?", (session_id,))
    monkeypatch.setattr(app_main, "delete_object", lambda _key: (_ for _ in ()).throw(RuntimeError("rustfs unavailable")))

    response = client.request(
        "DELETE",
        "/api/upload-sessions",
        json={"ids": [session_id], "confirmName": created["session"]["sourceTitle"] or session_id},
    )

    assert response.status_code == 200
    assert response.json()["objectCleanupErrors"] == [{"objectKey": key, "error": "rustfs unavailable"}]
    with get_db() as db:
        queued = db.execute("SELECT object_key, error_message FROM upload_object_cleanup_queue").fetchone()
        assert queued["object_key"] == key
        assert queued["error_message"] == "rustfs unavailable"
    monkeypatch.setattr(app_main, "delete_object", lambda _key: None)
    assert app_main.process_upload_object_cleanup_queue() == []
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) AS count FROM upload_object_cleanup_queue").fetchone()["count"] == 0


def test_upload_cleanup_queue_rechecks_media_protection_and_rotates_deferred_keys(client, monkeypatch) -> None:
    from app import main as app_main
    from conftest import insert_media_asset

    protected_key = "media/11111111111111111111111111111111/original.jpg"
    ordinary_key = "media/22222222222222222222222222222222/original.jpg"
    media_asset_id = insert_media_asset("protected.jpg", protected_key)
    with get_db() as db:
        for source_id, key in (("protected-session", protected_key), ("ordinary-session", ordinary_key)):
            db.execute(
                """
                INSERT INTO upload_object_cleanup_queue
                  (object_key, source_session_id, error_message, attempt_count, created_at, updated_at)
                VALUES (?, ?, NULL, 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """,
                (key, source_id),
            )
    deleted_keys: list[str] = []
    monkeypatch.setattr(app_main, "delete_object", deleted_keys.append)

    assert app_main.process_upload_object_cleanup_queue(limit=1) == []
    assert deleted_keys == []
    assert app_main.process_upload_object_cleanup_queue(limit=1) == []
    assert deleted_keys == [ordinary_key]
    with get_db() as db:
        db.execute("DELETE FROM media_assets WHERE id = ?", (media_asset_id,))
    assert app_main.process_upload_object_cleanup_queue(limit=1) == []
    assert deleted_keys == [ordinary_key, protected_key]


def test_deletion_failed_still_waits_for_presigned_upload_urls_to_expire(client) -> None:
    from app import main as app_main

    created = create_session(client, filenames=["still-signed.jpg"])
    session_id = created["session"]["id"]
    with get_db() as db:
        db.execute(
            "UPDATE upload_sessions SET archived_at = ?, archive_reason = 'deletion_failed', updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), session_id),
        )
    assert app_main.cleanup_deleted_upload_sessions() == 0
    with get_db() as db:
        assert db.execute("SELECT id FROM upload_sessions WHERE id = ?", (session_id,)).fetchone() is not None
        db.execute("UPDATE upload_sessions SET updated_at = '2000-01-01T00:00:00Z' WHERE id = ?", (session_id,))
    assert app_main.cleanup_deleted_upload_sessions() == 1


def test_legacy_standalone_presign_and_finalize_routes_are_retired(client) -> None:
    presign = client.post(
        "/api/uploads/presign",
        json={"scope": "generation_task", "files": [{"filename": "front.jpg", "mimeType": "image/jpeg"}]},
    )
    finalize = client.post(
        "/api/generation-tasks/from-uploads",
        json={"name": "LEGACY", "uploads": [{"objectKey": "media/deadbeefdeadbeefdeadbeefdeadbeef/original.jpg"}]},
    )

    assert presign.status_code == 410
    assert finalize.status_code == 410


def test_storage_diagnostics_only_reports_confirmed_upload_objects_as_unavailable(client, monkeypatch) -> None:
    from app import main as app_main

    created = create_session(client, filenames=["front.jpg"])
    key = created["items"][0]["objectKey"]
    monkeypatch.setattr(app_main, "object_exists", lambda _key: False)

    pending = client.get("/api/admin/storage-diagnostics", params={"minAgeMinutes": 0})

    assert pending.status_code == 200
    assert pending.json()["summary"]["checkedUploadItems"] == 0

    completed = client.post(
        f"/api/upload-sessions/{created['session']['id']}/complete-items",
        json={"objectKeys": [key]},
    )
    assert completed.status_code == 200
    confirmed = client.get("/api/admin/storage-diagnostics", params={"minAgeMinutes": 0})

    assert confirmed.json()["summary"]["checkedUploadItems"] == 1
    assert confirmed.json()["unavailableUploadItems"][0]["objectKey"] == key


def test_stale_unfinalized_upload_sessions_are_abandoned_and_cleaned_exactly(client, monkeypatch) -> None:
    from app import main as app_main

    created = create_session(client, filenames=["front.jpg"])
    session_id = created["session"]["id"]
    key = created["items"][0]["objectKey"]
    stale_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        db.execute("UPDATE upload_sessions SET updated_at = ? WHERE id = ?", (stale_at, session_id))
        db.execute("UPDATE upload_session_items SET updated_at = ? WHERE session_id = ?", (stale_at, session_id))
    deleted_keys: list[str] = []
    monkeypatch.setattr(app_main, "delete_object", deleted_keys.append)

    cleaned = app_main.cleanup_stale_upload_sessions()

    assert cleaned == 1
    assert key in deleted_keys
    assert all(item.startswith(key.rsplit("/", 1)[0] + "/") for item in deleted_keys)
    recovered = client.get(
        "/api/upload-sessions",
        params={"includeAbandoned": "true", "status": "ABANDONED"},
    ).json()["items"]
    assert recovered[0]["id"] == session_id


def test_stale_upload_cleanup_retries_when_rustfs_is_unavailable(client, monkeypatch) -> None:
    from app import main as app_main

    created = create_session(client, filenames=["front.jpg"])
    session_id = created["session"]["id"]
    stale_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        db.execute("UPDATE upload_sessions SET updated_at = ? WHERE id = ?", (stale_at, session_id))
    monkeypatch.setattr(app_main, "delete_object", lambda key: (_ for _ in ()).throw(RuntimeError("rustfs unavailable")))

    cleaned = app_main.cleanup_stale_upload_sessions()

    assert cleaned == 0
    with get_db() as db:
        row = db.execute("SELECT status, error_message FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
    assert row["status"] == "ABANDONED"
    assert row["error_message"] == "上传批次已过期，文件清理待重试"

    monkeypatch.setattr(app_main, "delete_object", lambda key: None)
    assert app_main.cleanup_stale_upload_sessions() == 1


def test_stale_upload_cleanup_never_deletes_an_item_bound_to_media_asset(client, monkeypatch) -> None:
    from app import main as app_main

    created = create_session(client, filenames=["front.jpg"])
    session_id = created["session"]["id"]
    key = created["items"][0]["objectKey"]
    stale_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO media_assets
              (object_key, original_filename, mime_type, size_bytes, created_at, updated_at)
            VALUES (?, 'front.jpg', 'image/jpeg', 128, ?, ?)
            """,
            (key, stale_at, stale_at),
        )
        db.execute(
            "UPDATE upload_session_items SET media_asset_id = ?, updated_at = ? WHERE session_id = ?",
            (int(cursor.lastrowid), stale_at, session_id),
        )
        db.execute("UPDATE upload_sessions SET updated_at = ? WHERE id = ?", (stale_at, session_id))
    deleted_keys: list[str] = []
    monkeypatch.setattr(app_main, "delete_object", deleted_keys.append)

    assert app_main.cleanup_stale_upload_sessions() == 0
    assert deleted_keys == []


def test_stale_upload_cleanup_is_bounded_per_poller_run(client, monkeypatch) -> None:
    from app import main as app_main

    session_ids = [create_session(client, filenames=[f"{index}.jpg"])["session"]["id"] for index in range(21)]
    stale_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        placeholders = ", ".join("?" for _ in session_ids)
        db.execute(
            f"UPDATE upload_sessions SET updated_at = ? WHERE id IN ({placeholders})",
            (stale_at, *session_ids),
        )
    monkeypatch.setattr(app_main, "delete_object", lambda key: None)

    assert app_main.cleanup_stale_upload_sessions() == 20
    with get_db() as db:
        remaining = db.execute(
            f"SELECT COUNT(*) AS count FROM upload_sessions WHERE id IN ({placeholders}) AND status = 'PENDING'",
            session_ids,
        ).fetchone()["count"]
    assert remaining == 1


def test_upload_session_tracks_completed_items_and_is_recoverable(client) -> None:
    created = create_session(client)
    session = created["session"]
    keys = [item["objectKey"] for item in created["items"]]

    partial = client.post(
        f"/api/upload-sessions/{session['id']}/complete-items",
        json={"objectKeys": [keys[0]]},
    )
    assert partial.status_code == 200
    assert partial.json()["session"]["status"] == "UPLOADING"
    assert partial.json()["session"]["uploadedCount"] == 1

    recovered = client.get("/api/upload-sessions", params={"scope": "generation_task"})
    assert recovered.status_code == 200
    assert recovered.json()["items"][0]["id"] == session["id"]
    assert recovered.json()["items"][0]["uploadedCount"] == 1

    completed = client.post(
        f"/api/upload-sessions/{session['id']}/complete-items",
        json={"objectKeys": keys},
    )
    assert completed.status_code == 200
    assert completed.json()["session"]["status"] == "UPLOADED"
    assert completed.json()["session"]["uploadedCount"] == 2

    repeated = client.post(
        f"/api/upload-sessions/{session['id']}/complete-items",
        json={"objectKeys": keys},
    )
    assert repeated.status_code == 200
    assert repeated.json()["session"]["status"] == "UPLOADED"
    assert repeated.json()["session"]["uploadedCount"] == 2


def test_finalize_requires_all_items_confirmed_uploaded(client) -> None:
    created = create_session(client)
    session_id = created["session"]["id"]
    key = created["items"][0]["objectKey"]

    response = client.post(
        f"/api/upload-sessions/{session_id}/complete-items",
        json={"objectKeys": [key]},
    )
    assert response.status_code == 200

    finalize = client.post(
        f"/api/upload-sessions/{session_id}/finalize-generation-task",
        json={"name": "LOOK-001"},
    )
    assert finalize.status_code == 409
    assert "未上传完成" in finalize.json()["detail"]


def test_duplicate_styling_garment_finalize_marks_session_failed_for_recovery(client) -> None:
    insert_styling_garment(code="DUPLICATE-LOOK")
    created = create_session(client, scope="styling_garment", filenames=["front.jpg", "detail.jpg"])
    session_id = created["session"]["id"]
    keys = [item["objectKey"] for item in created["items"]]

    completed = client.post(
        f"/api/upload-sessions/{session_id}/complete-items",
        json={"objectKeys": keys},
    )
    assert completed.status_code == 200
    assert completed.json()["session"]["status"] == "UPLOADED"
    assert completed.json()["session"]["uploadedCount"] == 2

    finalize = client.post(
        f"/api/upload-sessions/{session_id}/finalize-styling-garment",
        json={"code": "DUPLICATE-LOOK"},
    )
    assert finalize.status_code == 409

    recovered = client.get("/api/upload-sessions", params={"scope": "styling_garment"})
    assert recovered.status_code == 200
    failed = recovered.json()["items"][0]
    assert failed["id"] == session_id
    assert failed["status"] == "FAILED"
    assert failed["uploadedCount"] == 2
    assert failed["errorMessage"] == "搭配服装货号已存在"
    assert failed["businessCode"] == "DUPLICATE-LOOK"


def test_unfinished_duplicate_styling_finalize_does_not_mark_session_failed(client) -> None:
    insert_styling_garment(code="PARTIAL-DUPLICATE")
    created = create_session(client, scope="styling_garment", filenames=["front.jpg", "detail.jpg"])
    session_id = created["session"]["id"]

    completed = client.post(
        f"/api/upload-sessions/{session_id}/complete-items",
        json={"objectKeys": [created["items"][0]["objectKey"]]},
    )
    assert completed.status_code == 200
    assert completed.json()["session"]["status"] == "UPLOADING"

    finalize = client.post(
        f"/api/upload-sessions/{session_id}/finalize-styling-garment",
        json={"code": "PARTIAL-DUPLICATE"},
    )
    assert finalize.status_code == 409
    assert "未上传完成" in finalize.json()["detail"]

    recovered = client.get("/api/upload-sessions", params={"scope": "styling_garment"})
    assert recovered.status_code == 200
    current = recovered.json()["items"][0]
    assert current["id"] == session_id
    assert current["status"] == "UPLOADING"
    assert current["uploadedCount"] == 1
    assert current["errorMessage"] is None
    assert [item["status"] for item in current["items"]] == ["UPLOADED", "PENDING"]


def test_abandoned_styling_session_finalize_does_not_mark_session_failed(client) -> None:
    insert_styling_garment(code="ABANDONED-DUPLICATE")
    created = create_session(client, scope="styling_garment", filenames=["front.jpg"])
    session_id = created["session"]["id"]
    completed = client.post(
        f"/api/upload-sessions/{session_id}/complete-items",
        json={"objectKeys": [created["items"][0]["objectKey"]]},
    )
    assert completed.status_code == 200

    abandoned = client.post(f"/api/upload-sessions/{session_id}/abandon")
    assert abandoned.status_code == 200
    assert abandoned.json()["session"]["status"] == "ABANDONED"

    finalize = client.post(
        f"/api/upload-sessions/{session_id}/finalize-styling-garment",
        json={"code": "ABANDONED-DUPLICATE"},
    )
    assert finalize.status_code == 409
    assert "已放弃" in finalize.json()["detail"]

    debug = client.get(
        "/api/upload-sessions",
        params={"scope": "styling_garment", "includeAbandoned": "true", "status": "ABANDONED"},
    )
    assert debug.status_code == 200
    assert debug.json()["items"][0]["id"] == session_id
    assert debug.json()["items"][0]["status"] == "ABANDONED"
    assert debug.json()["items"][0]["items"][0]["status"] == "ABANDONED"


def test_abandon_upload_session_excludes_default_recovery_and_cleans_unfinalized_objects(client, monkeypatch) -> None:
    deleted_keys: list[str] = []
    from app import main as app_main

    monkeypatch.setattr(app_main, "delete_object", lambda key: deleted_keys.append(str(key)))
    created = create_session(client, filenames=["front.jpg"])
    session_id = created["session"]["id"]
    key = created["items"][0]["objectKey"]
    complete = client.post(
        f"/api/upload-sessions/{session_id}/complete-items",
        json={"objectKeys": [key]},
    )
    assert complete.status_code == 200

    abandoned = client.post(f"/api/upload-sessions/{session_id}/abandon")
    assert abandoned.status_code == 200
    assert abandoned.json()["session"]["status"] == "ABANDONED"
    assert deleted_keys == [key]

    recovered = client.get("/api/upload-sessions")
    assert recovered.status_code == 200
    assert recovered.json()["items"] == []

    debug = client.get("/api/upload-sessions", params={"includeAbandoned": "true", "status": "ABANDONED"})
    assert debug.status_code == 200
    assert debug.json()["items"][0]["id"] == session_id


def test_styling_import_batch_persists_every_item_before_upload(client) -> None:
    response = client.post(
        "/api/upload-sessions/styling-import-batch",
        json={
            "batchId": "import-task-001",
            "sourceTitle": "7.20回图",
            "items": [
                {
                    "businessCode": "LOOK-001",
                    "files": [{"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128, "relativePath": "7.20回图/LOOK-001/front.jpg", "lastModified": 1000}],
                },
                {
                    "businessCode": "LOOK-002",
                    "files": [{"filename": "back.jpg", "mimeType": "image/jpeg", "sizeBytes": 256, "relativePath": "7.20回图/LOOK-002/back.jpg", "lastModified": 2000}],
                },
            ],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["batchId"] == "import-task-001"
    assert len(payload["items"]) == 2
    assert {entry["session"]["businessCode"] for entry in payload["items"]} == {"LOOK-001", "LOOK-002"}
    assert all(entry["session"]["batchId"] == "import-task-001" for entry in payload["items"])
    assert payload["items"][0]["session"]["items"][0]["relativePath"] == "7.20回图/LOOK-001/front.jpg"


def test_resume_rejects_a_different_directory_before_upload(client) -> None:
    created = client.post(
        "/api/upload-sessions",
        json={
            "scope": "styling_garment",
            "businessCode": "LOOK-RESUME",
            "files": [{"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128, "relativePath": "original/LOOK-RESUME/front.jpg", "lastModified": 1000}],
        },
    ).json()
    response = client.post(
        f"/api/upload-sessions/{created['session']['id']}/resume",
        json={"files": [{"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128, "relativePath": "wrong/LOOK-RESUME/front.jpg", "lastModified": 1000}]},
    )
    assert response.status_code == 409
    assert "原上传任务不一致" in response.json()["detail"]

    changed = client.post(
        f"/api/upload-sessions/{created['session']['id']}/resume",
        json={"files": [{"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128, "relativePath": "original/LOOK-RESUME/front.jpg", "lastModified": 2000}]},
    )
    assert changed.status_code == 409
    assert "图片已发生变化" in changed.json()["detail"]


def test_resume_skips_confirmed_files_and_refreshes_only_pending_uploads(client) -> None:
    created = client.post(
        "/api/upload-sessions",
        json={
            "scope": "styling_garment",
            "businessCode": "LOOK-PARTIAL",
            "files": [
                {"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128, "relativePath": "root/LOOK-PARTIAL/front.jpg", "lastModified": 1000},
                {"filename": "back.jpg", "mimeType": "image/jpeg", "sizeBytes": 256, "relativePath": "root/LOOK-PARTIAL/back.jpg", "lastModified": 2000},
            ],
        },
    ).json()
    first_key = created["items"][0]["objectKey"]
    client.post(f"/api/upload-sessions/{created['session']['id']}/complete-items", json={"objectKeys": [first_key]})
    response = client.post(
        f"/api/upload-sessions/{created['session']['id']}/resume",
        json={"files": [
            {"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128, "relativePath": "root/LOOK-PARTIAL/front.jpg", "lastModified": 1000},
            {"filename": "back.jpg", "mimeType": "image/jpeg", "sizeBytes": 256, "relativePath": "root/LOOK-PARTIAL/back.jpg", "lastModified": 2000},
        ]},
    )
    assert response.status_code == 200
    assert response.json()["session"]["uploadedCount"] == 1
    assert [item["relativePath"] for item in response.json()["items"]] == ["root/LOOK-PARTIAL/back.jpg"]


def test_active_upload_lease_prevents_a_second_computer_from_taking_over(client) -> None:
    created = client.post(
        "/api/upload-sessions",
        json={
            "scope": "styling_garment",
            "businessCode": "LOOK-LEASE",
            "batchId": "lease-batch-1",
            "executorId": "computer-a-tab-1",
            "files": [{"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128, "relativePath": "root/LOOK-LEASE/front.jpg", "lastModified": 1000}],
        },
    ).json()
    response = client.post(
        f"/api/upload-sessions/{created['session']['id']}/resume",
        json={
            "executorId": "computer-b-tab-1",
            "files": [{"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128, "relativePath": "root/LOOK-LEASE/front.jpg", "lastModified": 1000}],
        },
    )
    assert response.status_code == 409
    assert "另一页面或电脑" in response.json()["detail"]


def test_batch_resume_requires_an_executor_credential(client) -> None:
    created = client.post(
        "/api/upload-sessions/styling-import-batch",
        json={
            "batchId": "credential-batch-1",
            "items": [{
                "businessCode": "LOOK-CREDENTIAL",
                "files": [{"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128}],
            }],
        },
    ).json()["items"][0]
    response = client.post(
        f"/api/upload-sessions/{created['session']['id']}/resume",
        json={"files": [{"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128}]},
    )
    assert response.status_code == 409
    assert "执行凭证" in response.json()["detail"]


def test_batch_is_persisted_before_hashing_and_binds_checksum_on_first_resume(client, monkeypatch) -> None:
    from app import main as app_main

    monkeypatch.setattr(app_main, "object_head", lambda key: None)
    checksum = "c" * 64
    created = client.post(
        "/api/upload-sessions/styling-import-batch",
        json={
            "batchId": "checksum-bind-batch",
            "items": [{
                "businessCode": "LOOK-BIND",
                "files": [{"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128, "lastModified": 1000}],
            }],
        },
    ).json()["items"][0]
    assert created["session"]["items"][0]["checksumSha256"] is None

    resumed = client.post(
        f"/api/upload-sessions/{created['session']['id']}/resume",
        json={
            "executorId": "computer-a-tab-1",
            "files": [{
                "filename": "front.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 128,
                "lastModified": 1000,
                "checksumSha256": checksum,
            }],
        },
    )
    assert resumed.status_code == 200
    assert resumed.json()["session"]["items"][0]["checksumSha256"] == checksum
    assert resumed.json()["items"][0]["headers"]["x-amz-meta-sha256"] == checksum


def test_resume_rejects_same_path_and_metadata_when_checksum_changed(client) -> None:
    checksum_a = "a" * 64
    checksum_b = "b" * 64
    created = client.post(
        "/api/upload-sessions",
        json={
            "scope": "styling_garment",
            "businessCode": "LOOK-CHECKSUM",
            "files": [{
                "filename": "front.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 128,
                "relativePath": "root/LOOK-CHECKSUM/front.jpg",
                "lastModified": 1000,
                "checksumSha256": checksum_a,
            }],
        },
    ).json()
    response = client.post(
        f"/api/upload-sessions/{created['session']['id']}/resume",
        json={"files": [{
            "filename": "front.jpg",
            "mimeType": "image/jpeg",
            "sizeBytes": 128,
            "relativePath": "root/LOOK-CHECKSUM/front.jpg",
            "lastModified": 1000,
            "checksumSha256": checksum_b,
        }]},
    )
    assert response.status_code == 409
    assert "图片已发生变化" in response.json()["detail"]


def test_complete_items_rejects_an_object_missing_from_storage(client, monkeypatch) -> None:
    from app import main as app_main

    created = create_session(client, filenames=["front.jpg"])
    monkeypatch.setattr(app_main, "object_head", lambda key: None)
    response = client.post(
        f"/api/upload-sessions/{created['session']['id']}/complete-items",
        json={"objectKeys": [created["items"][0]["objectKey"]]},
    )
    assert response.status_code == 409
    assert "完整性校验失败" in response.json()["detail"]


def test_resume_recovers_an_object_uploaded_before_browser_refresh(client, monkeypatch) -> None:
    from app import main as app_main

    created = client.post(
        "/api/upload-sessions",
        json={
            "scope": "styling_garment",
            "businessCode": "LOOK-ORPHAN-PUT",
            "files": [{"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128, "relativePath": "root/LOOK-ORPHAN-PUT/front.jpg", "lastModified": 1000}],
        },
    ).json()
    monkeypatch.setattr(app_main, "object_head", lambda key: {"contentLength": 128, "contentType": "image/jpeg", "eTag": "etag"})
    response = client.post(
        f"/api/upload-sessions/{created['session']['id']}/resume",
        json={"files": [{"filename": "front.jpg", "mimeType": "image/jpeg", "sizeBytes": 128, "relativePath": "root/LOOK-ORPHAN-PUT/front.jpg", "lastModified": 1000}]},
    )
    assert response.status_code == 200
    assert response.json()["session"]["status"] == "UPLOADED"
    assert response.json()["session"]["uploadedCount"] == 1
    assert response.json()["items"] == []


def test_upgrade_after_054_adds_lease_and_checksum_columns() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    db.executemany(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, '2026-07-20T00:00:00Z')",
        [(version, name) for version, name in [
            ("050", "old"), ("051", "old"), ("052", "old"), ("053", "old"), ("054", "old"),
        ]],
    )
    db.execute("""
        CREATE TABLE upload_sessions (
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, status TEXT NOT NULL, batch_id TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE upload_session_items (
          id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, relative_path TEXT, last_modified INTEGER
        )
    """)

    run_migrations(db)

    session_columns = {row["name"] for row in db.execute("PRAGMA table_info(upload_sessions)")}
    item_columns = {row["name"] for row in db.execute("PRAGMA table_info(upload_session_items)")}
    versions = {row["version"] for row in db.execute("SELECT version FROM schema_migrations")}
    assert {"lease_owner", "lease_expires_at"} <= session_columns
    assert "checksum_sha256" in item_columns
    assert {"055", "056"} <= versions
