from __future__ import annotations

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
