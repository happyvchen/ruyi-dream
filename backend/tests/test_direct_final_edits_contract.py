from __future__ import annotations

import hashlib
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


def test_no_mask_migration_keeps_historical_mask_but_allows_new_presets_without_one() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("CREATE TABLE media_assets (id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE shot_preset_versions (id INTEGER PRIMARY KEY)")
    db.execute(
        """
        CREATE TABLE shot_presets (
          id INTEGER PRIMARY KEY,
          version_id INTEGER NOT NULL REFERENCES shot_preset_versions(id),
          category_family TEXT NOT NULL,
          shot_index INTEGER NOT NULL,
          background_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
          mask_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
          pose_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    db.executemany("INSERT INTO media_assets (id) VALUES (?)", [(1,), (2,), (3,), (4,)])
    db.execute("INSERT INTO shot_preset_versions (id) VALUES (1)")
    db.execute(
        """
        INSERT INTO shot_presets
          (id, version_id, category_family, shot_index, background_asset_id, mask_asset_id, pose_asset_id, created_at, updated_at)
        VALUES (1, 1, 'top', 1, 1, 2, 3, 'before', 'before')
        """
    )

    from app.db import migration_no_mask_master_shot_presets
    migration_no_mask_master_shot_presets(db)

    historical = db.execute("SELECT mask_asset_id FROM shot_presets WHERE id = 1").fetchone()
    db.execute(
        """
        INSERT INTO shot_presets
          (id, version_id, category_family, shot_index, background_asset_id, pose_asset_id, created_at, updated_at)
        VALUES (2, 1, 'top', 2, 1, 4, 'after', 'after')
        """
    )
    current = db.execute("SELECT mask_asset_id FROM shot_presets WHERE id = 2").fetchone()

    assert historical["mask_asset_id"] == 2
    assert current["mask_asset_id"] is None


def test_final_prompt_migration_preserves_admin_content_and_removes_old_preview_references() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE prompt_configs (key TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at TEXT)")
    db.execute(
        """
        INSERT INTO prompt_configs (key, content) VALUES
        ('assembly.final_image_bottom', '{{runtime.ref_preview_grid}} 是分镜参考图，参考其背景、光线、景别、取景、裁切、人物姿势、人物站位、姿势等\n{{runtime.ref_shoot_environment}} 是拍摄环境参考图\n仅在透明区域生成主体；保留管理员自定义下装规则\n沿人物轮廓编辑；保留纽扣细节\n背景其余区域保持原像素不变；裙摆长度必须准确'),
        ('assembly.final_image_top', '保留管理员自定义上装规则')
        """
    )

    from app.db import migration_final_prompt_master_shot_contract
    migration_final_prompt_master_shot_contract(db)

    bottom = db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_bottom'").fetchone()["content"]
    top = db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_top'").fetchone()["content"]
    assert "拍摄母版替换规则" in bottom
    assert "保留管理员自定义下装规则" in bottom
    assert "ref_preview_grid" not in bottom
    assert "ref_shoot_environment" not in bottom
    assert "透明区域生成主体" not in bottom
    assert "沿人物轮廓编辑" not in bottom
    assert "背景其余区域保持原像素不变" not in bottom
    assert "保留管理员自定义下装规则" in bottom
    assert "保留纽扣细节" in bottom
    assert "裙摆长度必须准确" in bottom
    assert "拍摄母版替换规则" in top
    assert "保留管理员自定义上装规则" in top


def test_known_legacy_top_prompt_is_replaced_but_custom_prompt_is_not(monkeypatch) -> None:
    from app import db as app_db

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE prompt_configs (key TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at TEXT)")
    legacy = "可精确识别的旧上装系统 Prompt"
    db.execute("INSERT INTO prompt_configs (key, content) VALUES ('assembly.final_image_top', ?)", (legacy,))
    monkeypatch.setattr(app_db, "LEGACY_TOP_FINAL_PROMPT_SHA256", hashlib.sha256(legacy.encode("utf-8")).hexdigest())

    app_db.migration_refresh_legacy_top_final_prompt(db)
    refreshed = db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_top'").fetchone()["content"]
    assert refreshed == app_db.canonical_top_final_prompt()
    assert "中央 3:4 竖向裁切安全区" in refreshed

    db.execute("UPDATE prompt_configs SET content = '管理员自定义上装 Prompt' WHERE key = 'assembly.final_image_top'")
    app_db.migration_refresh_legacy_top_final_prompt(db)
    preserved = db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_top'").fetchone()["content"]
    assert preserved == "管理员自定义上装 Prompt"


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


def test_preset_serialization_does_not_mark_deleted_assets_complete(client) -> None:
    timestamp = now_iso()
    with get_db() as db:
        version_id = int(db.execute(
            "INSERT INTO shot_preset_versions (version_number, status, created_at, updated_at) VALUES (1, 'DRAFT', ?, ?)",
            (timestamp, timestamp),
        ).lastrowid)
        background_id = int(db.execute(
            "INSERT INTO media_assets (object_key, original_filename, mime_type, width, height, size_bytes, deleted_at, created_at, updated_at) VALUES ('tests/deleted-master.jpg', 'master.jpg', 'image/jpeg', 2048, 2048, 1, ?, ?, ?)",
            (timestamp, timestamp, timestamp),
        ).lastrowid)
        pose_id = int(db.execute(
            "INSERT INTO media_assets (object_key, original_filename, mime_type, width, height, size_bytes, created_at, updated_at) VALUES ('tests/pose.jpg', 'pose.jpg', 'image/jpeg', 2048, 2048, 1, ?, ?)",
            (timestamp, timestamp),
        ).lastrowid)
        db.execute(
            "INSERT INTO shot_presets (version_id, category_family, shot_index, background_asset_id, pose_asset_id, created_at, updated_at) VALUES (?, 'top', 1, ?, ?, ?, ?)",
            (version_id, background_id, pose_id, timestamp, timestamp),
        )

    payload = app_main.serialize_shot_presets()
    slot = next(item for item in payload["slots"] if item["categoryFamily"] == "top" and item["shotIndex"] == 1)
    assert slot["complete"] is False


def test_toapis_final_edits_request_uses_master_shot_first_without_mask_and_production_quality(monkeypatch) -> None:
    monkeypatch.setattr(image_provider, "TOAPIS_MAX_REFERENCE_IMAGES", 6)
    task_package = {
        "generation": {"stage": "final_image"},
        "prompt": "将拍摄母版中的原人物和原服装替换为当前商品与模特。",
        "imageInputs": [
            {"role": "shot_background", "filename": "background.png"},
            {"role": "shot_pose", "filename": "pose.jpg"},
            {"role": "model_identity", "filename": "model.jpg"},
            {"role": "hero_garment", "filename": "hero.jpg"},
        ],
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
    assert "mask_url" not in request
    assert request["size"] == "1:1"
    assert request["resolution"] == "2k"
    assert request["quality"] == "high"
    assert "reference_images" not in request


def test_toapis_rejects_more_than_six_reference_urls_before_submission() -> None:
    task_package = {
        "generation": {"stage": "final_image"},
        "prompt": "将拍摄母版中的原人物和原服装替换为当前商品与模特。",
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

    inputs = app_main.build_direct_final_image_inputs(ids["styling_id"], input_version_id, preset)

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
    assert inputs[0]["role"] == "shot_background"


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
