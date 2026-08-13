from __future__ import annotations

import sqlite3

import pytest

from app import image_provider
from app import main as app_main
from app.db import dumps, get_db, migration_direct_final_edits_workflow, now_iso, run_migrations
from conftest import insert_published_shot_presets, insert_ready_generation_task_with_preview


def test_admin_sees_all_fifteen_shot_preset_slots_before_assets_are_configured(client) -> None:
    response = client.get("/api/shot-presets")

    assert response.status_code == 200
    payload = response.json()
    assert payload["families"]["top"]["publishReady"] is False
    assert len(payload["slots"]) == 15
    assert {(slot["categoryFamily"], slot["shotIndex"]) for slot in payload["slots"]} == {
        (family, shot_index)
        for family in ("top", "bottom", "one_piece")
        for shot_index in range(1, 6)
    }


def test_incomplete_family_cannot_be_published(client) -> None:
    response = client.post("/api/shot-preset-families/top/publish")

    assert response.status_code == 409
    assert "上装" in response.json()["detail"]


def test_retired_storyboard_prompt_content_is_backed_up_before_removal() -> None:
    timestamp = now_iso()
    key = "assembly.storyboard_preview_top"
    with get_db() as db:
        db.execute(
            """
            INSERT INTO prompt_configs (key, group_type, title, description, content, sort_order, updated_at)
            VALUES (?, 'assembly', '旧分镜', '', '保留这段历史正文', 120, ?)
            """,
            (key, timestamp),
        )
        migration_direct_final_edits_workflow(db)
        active = db.execute("SELECT content FROM prompt_configs WHERE key = ?", (key,)).fetchone()
        retired = db.execute("SELECT content FROM retired_prompt_configs WHERE key = ?", (key,)).fetchone()

    assert active is None
    assert retired["content"] == "保留这段历史正文"


def test_migration_repairs_result_sets_when_066_was_recorded_before_column_was_added() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    db.executemany(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, 'already_applied', '2026-08-12T00:00:00Z')",
        [(version,) for version in [
            "050", "051", "052", "053", "054", "055", "056", "057", "058",
            "059", "060", "061", "062", "063", "064", "065", "066", "067",
        ]],
    )
    db.execute(
        """
        CREATE TABLE generation_result_sets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id INTEGER NOT NULL,
          input_version_id INTEGER NOT NULL,
          styling_id INTEGER NOT NULL,
          preview_id INTEGER NOT NULL,
          user_instruction_json TEXT,
          status TEXT NOT NULL DEFAULT 'RUNNING',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO generation_result_sets
          (task_id, input_version_id, styling_id, preview_id, status, created_at, updated_at)
        VALUES (1, 2, 3, 4, 'COMPLETED', 'before', 'before')
        """
    )

    run_migrations(db)

    columns = {row["name"]: row for row in db.execute("PRAGMA table_info(generation_result_sets)")}
    preserved = db.execute("SELECT * FROM generation_result_sets WHERE id = 1").fetchone()
    versions = {row["version"] for row in db.execute("SELECT version FROM schema_migrations")}
    assert "shot_preset_version_id" in columns
    assert columns["preview_id"]["notnull"] == 0
    assert preserved["status"] == "COMPLETED"
    assert "068" in versions


def test_complete_top_family_can_be_published_without_bottom_or_one_piece(client) -> None:
    timestamp = now_iso()
    with get_db() as db:
        version_id = int(
            db.execute(
                "INSERT INTO shot_preset_versions (version_number, status, created_at, updated_at) VALUES (1, 'DRAFT', ?, ?)",
                (timestamp, timestamp),
            ).lastrowid
        )
        family = "top"
        for shot_index in range(1, 6):
            asset_ids = []
            for role, mime_type in (("background", "image/png"), ("mask", "image/png"), ("pose", "image/jpeg")):
                asset_ids.append(
                    int(
                        db.execute(
                            """
                            INSERT INTO media_assets
                              (object_key, original_filename, mime_type, width, height, size_bytes, created_at, updated_at)
                            VALUES (?, ?, ?, 2048, 2048, 128, ?, ?)
                            """,
                            (f"tests/{family}-{shot_index}-{role}", f"{role}.png", mime_type, timestamp, timestamp),
                        ).lastrowid
                    )
                )
            db.execute(
                """
                INSERT INTO shot_presets
                  (version_id, category_family, shot_index, background_asset_id, mask_asset_id, pose_asset_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (version_id, family, shot_index, *asset_ids, timestamp, timestamp),
            )

    response = client.post("/api/shot-preset-families/top/publish")

    assert response.status_code == 200
    payload = response.json()
    assert payload["families"]["top"]["publishedVersion"]["status"] == "PUBLISHED"
    assert payload["families"]["top"]["publishReady"] is True
    assert payload["families"]["bottom"]["publishedVersion"] is None
    assert payload["families"]["one_piece"]["publishedVersion"] is None


def test_toapis_final_edits_request_uses_background_first_mask_and_production_quality(monkeypatch) -> None:
    monkeypatch.setattr(image_provider, "TOAPIS_MAX_REFERENCE_IMAGES", 6)
    task_package = {
        "generation": {"stage": "final_image"},
        "prompt": "在遮罩区域内生成当前镜头。",
        "imageInputs": [
            {"role": "shot_background", "filename": "background.png"},
            {"role": "shot_pose", "filename": "pose.jpg"},
            {"role": "model_identity", "filename": "model.jpg"},
            {"role": "hero_garment", "filename": "hero.jpg"},
        ],
        "maskProviderUrl": "https://files.toapis.com/mask.png",
    }

    request = image_provider.build_toapis_request_snapshot(
        task_package,
        [
            "https://files.toapis.com/background.png",
            "https://files.toapis.com/pose.jpg",
            "https://files.toapis.com/model.jpg",
            "https://files.toapis.com/hero.jpg",
        ],
    )

    assert request["image_urls"][0].endswith("background.png")
    assert request["mask_url"].endswith("mask.png")
    assert request["size"] == "1:1"
    assert request["resolution"] == "2k"
    assert request["quality"] == "high"
    assert "reference_images" not in request


def test_toapis_rejects_more_than_six_reference_urls_before_submission() -> None:
    task_package = {
        "generation": {"stage": "final_image"},
        "prompt": "在遮罩区域内生成当前镜头。",
    }

    with pytest.raises(RuntimeError, match="最多支持 6 张"):
        image_provider.build_toapis_request_snapshot(
            task_package,
            [f"https://files.toapis.com/reference-{index}.jpg" for index in range(7)],
        )


def test_direct_final_inputs_compose_six_provider_slots_without_losing_sources(monkeypatch) -> None:
    ids = insert_ready_generation_task_with_preview(family="top")
    monkeypatch.setattr(app_main, "object_exists", lambda _key: True)
    model_sources = [
        {"objectKey": f"models/model-{index}.jpg", "filename": f"model-{index}.jpg"}
        for index in range(2)
    ]
    hero_sources = [
        {"asset": {"id": 100 + index, "objectKey": f"hero/{index}.jpg", "filename": f"hero-{index}.jpg"}}
        for index in range(6)
    ]
    styling_sources = [
        {"asset": {"id": 200 + index, "objectKey": f"styling/{index}.jpg", "filename": f"styling-{index}.jpg"}}
        for index in range(6)
    ]
    with get_db() as db:
        input_version_id = int(
            db.execute("SELECT input_version_id FROM generation_stylings WHERE id = ?", (ids["styling_id"],)).fetchone()[0]
        )
        db.execute(
            "UPDATE generation_task_input_versions SET image_snapshot_json = ?, model_snapshot_json = ? WHERE id = ?",
            (dumps(hero_sources), dumps({"imageInputs": model_sources}), input_version_id),
        )
        db.execute(
            "UPDATE generation_stylings SET selected_material_snapshot_json = ? WHERE id = ?",
            (dumps([{"code": "PANTS-1", "images": styling_sources}]), ids["styling_id"]),
        )
        preset = db.execute(
            "SELECT * FROM shot_presets WHERE category_family = 'top' AND shot_index = 1 ORDER BY version_id DESC LIMIT 1"
        ).fetchone()

    inputs, mask = app_main.build_direct_final_image_inputs(ids["styling_id"], input_version_id, preset)

    assert [item["role"] for item in inputs] == [
        "shot_background",
        "shot_pose",
        "model_identity",
        "hero_garment",
        "hero_garment",
        "styling_garment",
    ]
    assert [item.get("referenceBoardKind") for item in inputs[2:]] == [
        "model_identity",
        "hero_overview",
        "hero_detail",
        "styling_garments",
    ]
    assert [len(item.get("collageItems", [])) for item in inputs[2:]] == [2, 3, 3, 6]
    assert sum(len(item.get("collageItems", [])) for item in inputs[2:]) == 14
    assert mask["role"] == "shot_mask"


def test_six_source_reference_board_uses_all_tiles() -> None:
    assert image_provider.reference_collage_grid(6) == (3, 2)


def test_styling_ready_task_creates_direct_final_set_without_preview(client) -> None:
    ids = insert_ready_generation_task_with_preview(family="one_piece")
    insert_published_shot_presets()
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_tasks
            SET status = 'STYLING_READY', active_preview_id = NULL, active_storyboard_preview_id = NULL
            WHERE id = ?
            """,
            (ids["task_id"],),
        )

    response = client.post(f"/api/generation-tasks/{ids['task_id']}/generate-final")

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["status"] == "FINAL_RUNNING"
    with get_db() as db:
        result_set = db.execute("SELECT * FROM generation_result_sets WHERE id = ?", (item["activeResultSetId"],)).fetchone()
    assert result_set["preview_id"] is None
    assert result_set["shot_preset_version_id"] is not None
    assert [image["shotRole"] for image in item["resultSet"]["images"]] == [
        "one_piece_shot_1",
        "one_piece_shot_2",
        "one_piece_shot_3",
        "one_piece_shot_4",
        "one_piece_shot_5",
    ]


def test_publishing_top_unblocks_top_but_not_bottom_generation(client) -> None:
    top_ids = insert_ready_generation_task_with_preview(family="top", ensure_presets=False)
    bottom_ids = insert_ready_generation_task_with_preview(family="bottom", ensure_presets=False)
    insert_published_shot_presets(families=("top",))
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_tasks
            SET status = 'STYLING_READY', active_preview_id = NULL, active_storyboard_preview_id = NULL
            WHERE id IN (?, ?)
            """,
            (top_ids["task_id"], bottom_ids["task_id"]),
        )

    top_response = client.post(f"/api/generation-tasks/{top_ids['task_id']}/generate-final")
    bottom_response = client.post(f"/api/generation-tasks/{bottom_ids['task_id']}/generate-final")

    assert top_response.status_code == 200
    assert top_response.json()["item"]["status"] == "FINAL_RUNNING"
    assert bottom_response.status_code == 409
    assert "下装拍摄预设尚未发布" in bottom_response.json()["detail"]


def test_preview_endpoint_is_retired_for_new_workflow(client) -> None:
    response = client.post("/api/generation-tasks/999/preview")

    assert response.status_code == 410
