from __future__ import annotations

import pytest

from app import main as app_main
from app.db import get_db
from app.media_storage import media_original_key_for_owner


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
