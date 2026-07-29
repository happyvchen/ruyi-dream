from __future__ import annotations

import pytest

from app import main as app_main
from app.db import get_db
from app.media_storage import media_original_key_for_owner, new_media_original_key
from conftest import insert_media_asset


def test_generated_media_attempt_key_is_stable_for_recovery_but_changes_on_retry() -> None:
    first = media_original_key_for_owner("generation_attempt", 42, "preview.png")

    assert first == media_original_key_for_owner("generation_attempt", 42, "preview.png")
    assert first != media_original_key_for_owner("generation_attempt", 43, "preview.png")
    assert first.startswith("media/")
    assert first.endswith("/original.png")


def test_generated_media_registration_is_idempotent_for_one_attempt_object() -> None:
    key = media_original_key_for_owner("generation_attempt", 42, "preview.png")

    first_id = app_main.insert_generated_media_asset(key, "preview.png", b"same-image", "image/png", {})
    second_id = app_main.insert_generated_media_asset(key, "preview.png", b"same-image", "image/png", {})

    assert second_id == first_id
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) AS count FROM media_assets WHERE object_key = ?", (key,)).fetchone()["count"] == 1


def test_generated_media_registration_rejects_changed_content_for_same_attempt_object() -> None:
    key = media_original_key_for_owner("generation_attempt", 43, "final.png")
    app_main.insert_generated_media_asset(key, "final.png", b"first-image", "image/png", {})

    with pytest.raises(RuntimeError, match="内容哈希冲突"):
        app_main.insert_generated_media_asset(key, "final.png", b"changed-image", "image/png", {})


def test_media_endpoint_serves_legacy_and_asset_scoped_keys_during_migration(client, monkeypatch) -> None:
    legacy_key = "direct-uploads/styling-garment/session-a/001-front.jpg"
    current_key = new_media_original_key("front.jpg")
    insert_media_asset("legacy.jpg", legacy_key)
    insert_media_asset("current.jpg", current_key)
    monkeypatch.setattr(app_main, "object_exists", lambda key: True)
    monkeypatch.setattr(app_main, "presigned_get_url", lambda key, **kwargs: f"https://storage.test/{key}")

    legacy = client.get("/api/media", params={"key": legacy_key}, follow_redirects=False)
    current = client.get("/api/media", params={"key": current_key}, follow_redirects=False)

    assert legacy.status_code == 302
    assert legacy.headers["location"] == f"https://storage.test/{legacy_key}"
    assert current.status_code == 302
    assert current.headers["location"] == f"https://storage.test/{current_key}"
