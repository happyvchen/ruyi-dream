from __future__ import annotations

import os
import inspect
import tempfile
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient


TEST_ROOT = Path(tempfile.mkdtemp(prefix="ruyi-dream-tests-"))
os.environ["APP_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["APP_DATABASE_PATH"] = str(TEST_ROOT / "app.sqlite3")
os.environ["APP_S3_ACCESS_KEY_ID"] = "test-access-key"
os.environ["APP_S3_SECRET_ACCESS_KEY"] = "test-secret-key"
os.environ["APP_USER_PASSWORD"] = ""
os.environ["APP_ADMIN_PASSWORD"] = ""
os.environ["APP_SESSION_SECRET"] = "test-session-secret"
os.environ["BACKEND_CORS_ORIGINS"] = "http://testserver"
os.environ["TOAPIS_MAX_CONCURRENCY"] = "5"

from app import main as app_main  # noqa: E402
from app.db import dumps, get_db, init_db, now_iso  # noqa: E402


def _discard_background_job(job: Any, *args: Any, **kwargs: Any) -> None:
    if inspect.iscoroutine(job):
        job.close()


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    db_path = app_main.settings.database_path
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db()
    monkeypatch.setattr(app_main, "ensure_bucket", lambda: None)
    monkeypatch.setattr(app_main, "ensure_direct_upload_cors", lambda: None)
    monkeypatch.setattr(app_main, "object_head", lambda key: {"contentLength": 128})
    monkeypatch.setattr(app_main, "get_bytes", lambda key: b"test-image-bytes")
    monkeypatch.setattr(app_main, "delete_object", lambda key: None)
    monkeypatch.setattr(app_main, "delete_prefix", lambda prefix: None)
    monkeypatch.setattr(app_main, "pump_auto_run_queue", lambda: None)
    monkeypatch.setattr(app_main, "schedule_background_generation", _discard_background_job)
    yield


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app_main.app)


def insert_media_asset(filename: str = "image.jpg", object_key: str | None = None) -> int:
    timestamp = now_iso()
    with get_db() as db:
        cursor = _insert_media_asset_row(db, filename, object_key, timestamp)
        return int(cursor.lastrowid)


def _insert_media_asset_row(db: Any, filename: str, object_key: str | None, timestamp: str) -> Any:
    return db.execute(
        """
        INSERT INTO media_assets
          (object_key, original_filename, mime_type, width, height, size_bytes, created_at, updated_at)
        VALUES (?, ?, 'image/jpeg', 1200, 1600, 128, ?, ?)
        """,
        (object_key or f"tests/{filename}", filename, timestamp, timestamp),
    )


def insert_generation_task(
    *,
    name: str = "LOOK-001",
    status: str = "DRAFT",
    image_count: int = 1,
    batch_id: str | None = None,
) -> int:
    timestamp = now_iso()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO generation_tasks (name, status, batch_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, status, batch_id, timestamp, timestamp),
        )
        task_id = int(cursor.lastrowid)
        for index in range(image_count):
            asset_id = int(_insert_media_asset_row(db, f"{name}-{index + 1}.jpg", f"generation-tasks/{task_id}/{index + 1}.jpg", timestamp).lastrowid)
            db.execute(
                """
                INSERT INTO generation_task_images
                  (task_id, media_asset_id, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, asset_id, index, timestamp, timestamp),
            )
    return task_id


def insert_ready_generation_task_with_preview(*, family: str = "top", status: str = "PREVIEW_READY") -> dict[str, int]:
    task_id = insert_generation_task(name="READY-LOOK", status=status, image_count=2)
    preview_asset_id = insert_media_asset("preview.png", f"generation-tasks/{task_id}/preview.png")
    timestamp = now_iso()
    styling_plan: dict[str, Any] = {
        "hero_understanding": {
            "category": "西装马甲",
            "sub_category": "suit_vest",
            "shot_category_family": family,
        },
        "selected_material_codes": [],
        "selected_material_ids": [],
        "selected_outfit": {"wearing_logic": "主推款单品清晰呈现"},
    }
    with get_db() as db:
        input_cursor = db.execute(
            """
            INSERT INTO generation_task_input_versions
              (task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, user_requirement_json, created_at)
            VALUES (?, 1, 'READY-LOOK', '[]', '{}', '{}', ?)
            """,
            (task_id, timestamp),
        )
        input_version_id = int(input_cursor.lastrowid)
        styling_cursor = db.execute(
            """
            INSERT INTO generation_stylings
              (task_id, input_version_id, source_material_ids_json, selected_material_ids_json,
               selected_material_snapshot_json, styling_plan_json, hero_understanding_json,
               status, created_at, updated_at)
            VALUES (?, ?, '[]', '[]', '[]', ?, ?, 'SUCCEEDED', ?, ?)
            """,
            (task_id, input_version_id, dumps(styling_plan), dumps(styling_plan["hero_understanding"]), timestamp, timestamp),
        )
        styling_id = int(styling_cursor.lastrowid)
        preview_cursor = db.execute(
            """
            INSERT INTO generation_previews
              (task_id, styling_id, input_version_id, preview_asset_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'SUCCEEDED', ?, ?)
            """,
            (task_id, styling_id, input_version_id, preview_asset_id, timestamp, timestamp),
        )
        preview_id = int(preview_cursor.lastrowid)
        db.execute(
            """
            UPDATE generation_tasks
            SET status = ?, current_input_version_id = ?, active_styling_id = ?,
                active_preview_id = ?, active_storyboard_preview_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, input_version_id, styling_id, preview_id, preview_id, timestamp, task_id),
        )
    return {"task_id": task_id, "styling_id": styling_id, "preview_id": preview_id}


def insert_styling_garment(*, code: str, status: str = "READY", image_count: int = 1) -> int:
    timestamp = now_iso()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO styling_garments (code, status, ai_tags_json, effective_tags_json, created_at, updated_at)
            VALUES (?, ?, '{}', '{}', ?, ?)
            """,
            (code, status, timestamp, timestamp),
        )
        garment_id = int(cursor.lastrowid)
        for index in range(image_count):
            asset_id = int(_insert_media_asset_row(db, f"{code}-{index + 1}.jpg", f"styling-garments/{garment_id}/{index + 1}.jpg", timestamp).lastrowid)
            db.execute(
                """
                INSERT INTO styling_garment_images
                  (styling_garment_id, media_asset_id, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (garment_id, asset_id, index, timestamp, timestamp),
            )
    return garment_id
