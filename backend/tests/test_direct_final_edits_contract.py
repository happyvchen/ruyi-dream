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
    assert payload["families"]["top"]["complete"] is False
    assert payload["families"]["top"]["configuredCount"] == 0
    assert len(payload["slots"]) == 15
    assert {(slot["categoryFamily"], slot["shotIndex"]) for slot in payload["slots"]} == {
        (family, shot_index)
        for family in ("top", "bottom", "one_piece")
        for shot_index in range(1, 6)
    }


def test_only_admin_can_read_or_change_shot_presets(client, monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "user_password", "test-user-password")

    anonymous_response = client.get("/api/shot-presets")

    client.cookies.set("ruyi_session", app_main.create_session_cookie("user"))

    read_response = client.get("/api/shot-presets")
    replace_response = client.post(
        "/api/shot-presets/top/1/background",
        files={"file": ("master.jpg", b"image", "image/jpeg")},
    )
    delete_response = client.delete("/api/shot-presets/top/1/background")

    assert anonymous_response.status_code == 401
    assert read_response.status_code == 403
    assert replace_response.status_code == 403
    assert delete_response.status_code == 403
    client.cookies.set("ruyi_session", app_main.create_session_cookie("admin"))
    assert len(client.get('/api/shot-presets').json()['slots']) == 15


def test_publish_endpoint_is_retired_because_each_image_is_live(client) -> None:
    response = client.post("/api/shot-preset-families/top/publish")

    assert response.status_code == 410
    assert "立即生效" in response.json()["detail"]


def test_replacing_only_one_side_of_an_empty_preset_slot_returns_partial_slot(client, monkeypatch) -> None:
    async def fake_inspect(_upload):
        return (1200, 1600), False

    async def fake_save(_uploads):
        timestamp = now_iso()
        with get_db() as db:
            asset_id = int(
                db.execute(
                    """
                    INSERT INTO media_assets
                      (object_key, original_filename, mime_type, width, height, size_bytes, created_at, updated_at)
                    VALUES ('tests/partial-master.jpg', 'partial-master.jpg', 'image/jpeg', 1200, 1600, 128, ?, ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
            )
        return [{"id": asset_id}]

    monkeypatch.setattr(app_main, "inspect_shot_preset_upload", fake_inspect)
    monkeypatch.setattr(app_main, "save_uploads_as_media_assets", fake_save)

    response = client.post(
        "/api/shot-presets/bottom/1/background",
        files={"file": ("partial-master.jpg", b"image", "image/jpeg")},
    )

    assert response.status_code == 200
    slot = next(
        item
        for item in response.json()["slots"]
        if item["categoryFamily"] == "bottom" and item["shotIndex"] == 1
    )
    assert slot["background"]["originalFilename"] == "partial-master.jpg"
    assert slot["pose"] is None
    assert slot["complete"] is False


def test_deleting_one_side_of_a_complete_preset_slot_returns_partial_slot(client) -> None:
    timestamp = now_iso()
    with get_db() as db:
        asset_ids = []
        for role in ("background", "pose"):
            asset_ids.append(
                int(
                    db.execute(
                        """
                        INSERT INTO media_assets
                          (object_key, original_filename, mime_type, width, height, size_bytes, created_at, updated_at)
                        VALUES (?, ?, 'image/jpeg', 1200, 1600, 128, ?, ?)
                        """,
                        (f"tests/delete-{role}.jpg", f"delete-{role}.jpg", timestamp, timestamp),
                    ).lastrowid
                )
            )
        db.execute(
            """
            INSERT INTO shot_preset_slots
              (category_family, shot_index, background_asset_id, pose_asset_id, created_at, updated_at)
            VALUES ('bottom', 1, ?, ?, ?, ?)
            """,
            (*asset_ids, timestamp, timestamp),
        )

    response = client.delete("/api/shot-presets/bottom/1/background")

    assert response.status_code == 200
    slot = next(
        item
        for item in response.json()["slots"]
        if item["categoryFamily"] == "bottom" and item["shotIndex"] == 1
    )
    assert slot["background"] is None
    assert slot["pose"]["originalFilename"] == "delete-pose.jpg"
    assert slot["complete"] is False


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
    assert "中央全高 3:4 裁切窗口" in refreshed

    db.execute("UPDATE prompt_configs SET content = '管理员自定义上装 Prompt' WHERE key = 'assembly.final_image_top'")
    app_db.migration_refresh_legacy_top_final_prompt(db)
    preserved = db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_top'").fetchone()["content"]
    assert preserved == "管理员自定义上装 Prompt"


def test_known_legacy_bottom_and_one_piece_prompts_are_replaced_but_custom_prompt_is_not(monkeypatch) -> None:
    from app import db as app_db

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE prompt_configs (key TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at TEXT)")
    legacy_by_key = {
        "assembly.final_image_bottom": "可精确识别的旧下装系统 Prompt",
        "assembly.final_image_one_piece": "可精确识别的旧套装系统 Prompt",
    }
    for key, legacy in legacy_by_key.items():
        db.execute("INSERT INTO prompt_configs (key, content) VALUES (?, ?)", (key, legacy))
    monkeypatch.setattr(
        app_db,
        "LEGACY_BOTTOM_AND_ONE_PIECE_FINAL_PROMPT_SHA256",
        {key: hashlib.sha256(legacy.encode("utf-8")).hexdigest() for key, legacy in legacy_by_key.items()},
    )

    app_db.migration_refresh_legacy_bottom_and_one_piece_final_prompts(db)

    for key in legacy_by_key:
        refreshed = db.execute("SELECT content FROM prompt_configs WHERE key = ?", (key,)).fetchone()["content"]
        assert refreshed == app_db.canonical_bottom_or_one_piece_final_prompt(key)
        assert "中央全高 3:4 裁切窗口" in refreshed

    db.execute("UPDATE prompt_configs SET content = '管理员自定义下装 Prompt' WHERE key = 'assembly.final_image_bottom'")
    app_db.migration_refresh_legacy_bottom_and_one_piece_final_prompts(db)
    preserved = db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_bottom'").fetchone()["content"]
    assert preserved == "管理员自定义下装 Prompt"


def test_complete_top_family_is_live_without_publishing(client) -> None:
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
                INSERT INTO shot_preset_slots
                  (category_family, shot_index, background_asset_id, pose_asset_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (family, shot_index, asset_ids[0], asset_ids[2], timestamp, timestamp),
            )

    response = client.get("/api/shot-presets")

    assert response.status_code == 200
    payload = response.json()
    assert payload["families"]["top"]["complete"] is True
    assert payload["families"]["top"]["configuredCount"] == 5
    assert payload["families"]["bottom"]["complete"] is False
    assert payload["families"]["one_piece"]["complete"] is False


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
            "INSERT INTO shot_preset_slots (category_family, shot_index, background_asset_id, pose_asset_id, created_at, updated_at) VALUES ('top', 1, ?, ?, ?, ?)",
            (background_id, pose_id, timestamp, timestamp),
        )

    payload = app_main.serialize_shot_presets()
    slot = next(item for item in payload["slots"] if item["categoryFamily"] == "top" and item["shotIndex"] == 1)
    assert slot["complete"] is False


@pytest.mark.parametrize("resolution, expected_resolution", [("2k", "2k"), ("4K", "4k")])
def test_toapis_final_edits_request_uses_master_shot_first_without_mask_and_production_quality(monkeypatch, resolution, expected_resolution) -> None:
    monkeypatch.setattr(image_provider, "TOAPIS_MAX_REFERENCE_IMAGES", 6)
    monkeypatch.setattr(image_provider.get_settings(), "gpt_image2_model_id", "gpt-image-2.5-sunburst")
    monkeypatch.setattr(image_provider.get_settings(), "gpt_image2_resolution", resolution)
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
            "https://files.toapis.cn/background.png",
            "https://files.toapis.cn/pose.jpg",
            "https://files.toapis.cn/model.jpg",
            "https://files.toapis.cn/hero.jpg",
        ],
    )

    assert request["model"] == "gpt-image-2.5-sunburst"
    assert request["image_urls"][0].endswith("background.png")
    assert "mask_url" not in request
    assert request["size"] == "1:1"
    assert request["resolution"] == expected_resolution
    assert request["quality"] == "high"
    assert request["n"] == 1
    assert request["response_format"] == "url"
    assert "reference_images" not in request


def test_toapis_accepts_fourteen_and_rejects_fifteen_reference_urls_before_submission() -> None:
    task_package = {
        "generation": {"stage": "final_image"},
        "prompt": "将拍摄母版中的原人物和原服装替换为当前商品与模特。",
    }

    assert len(image_provider.build_toapis_request_snapshot(
        task_package, [f"https://files.toapis.cn/reference-{index}.jpg" for index in range(14)],
    )["image_urls"]) == 14
    with pytest.raises(RuntimeError, match="最多支持 14 张"):
        image_provider.build_toapis_request_snapshot(
            task_package,
            [f"https://files.toapis.cn/reference-{index}.jpg" for index in range(15)],
        )


@pytest.mark.parametrize("shot_index", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("model_count", [1, 2, 3, 4, 5])
def test_direct_final_inputs_keep_order_and_fourteen_slot_budget(monkeypatch, shot_index, model_count) -> None:
    ids = insert_ready_generation_task_with_preview(family="top")
    monkeypatch.setattr(app_main, "object_exists", lambda _key: True)
    model_sources = [
        {"objectKey": f"models/model-{index}.jpg", "filename": f"model-{index}.jpg"}
        for index in range(model_count)
    ]
    hero_sources = [
        {"asset": {"id": 100 + index, "objectKey": f"hero/{index}.jpg", "filename": f"hero-{index}.jpg"}}
        for index in range(8)
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
            "SELECT * FROM shot_presets WHERE category_family = 'top' AND shot_index = ? ORDER BY version_id DESC LIMIT 1",
            (shot_index,),
        ).fetchone()

    inputs = app_main.build_direct_final_image_inputs(ids["styling_id"], input_version_id, preset)

    assert len(inputs) == 14
    assert [item["role"] for item in inputs] == [
        "shot_background", "shot_pose", "model_identity", "model_identity",
        *(["hero_garment"] * 6), *(["styling_garment"] * 4),
    ]
    assert inputs[2]["objectKey"] == "models/model-0.jpg"
    assert inputs[3]["collageRule"]["kind"] == "model_identity_grid_2x2"
    assert [item["objectKey"] for item in inputs[3]["collageItems"]] == [
        f"models/model-{index}.jpg" for index in range(min(4, model_count))
    ]
    assert all(not item.get("collageItems") for item in inputs[4:])
    assert [item["objectKey"] for item in inputs[4:]] == [
        *[f"hero/{index}.jpg" for index in range(6)],
        *[f"styling/{index}.jpg" for index in range(4)],
    ]
    assert all(item["materialCode"] == "PANTS-1" for item in inputs[10:])
    crop = app_main.IMAGE2_DIRECT_POSE_CROP.get(("top", shot_index))
    assert inputs[1].get("providerCrop") == ({"kind": "keep_top_ratio", "ratio": crop} if crop else None)


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
    assert result_set["shot_preset_version_id"] is None
    with get_db() as db:
        frozen = db.execute(
            "SELECT * FROM generation_result_set_shot_presets WHERE result_set_id = ? ORDER BY shot_index",
            (item["activeResultSetId"],),
        ).fetchall()
    assert len(frozen) == 5
    assert [image["shotRole"] for image in item["resultSet"]["images"]] == [
        "one_piece_shot_1",
        "one_piece_shot_2",
        "one_piece_shot_3",
        "one_piece_shot_4",
        "one_piece_shot_5",
    ]


def test_complete_live_top_presets_unblock_top_but_not_bottom_generation(client) -> None:
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
    assert "下装拍摄预设尚未完整" in bottom_response.json()["detail"]


def test_preview_endpoint_is_retired_for_new_workflow(client) -> None:
    response = client.post("/api/generation-tasks/999/preview")

    assert response.status_code == 410


def test_photographic_final_prompt_migration_only_replaces_known_system_content(monkeypatch) -> None:
    from app import db as app_db

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE prompt_configs (key TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at TEXT)")
    db.execute("CREATE TABLE event_logs (id INTEGER PRIMARY KEY, entity_type TEXT, entity_id INTEGER, event_type TEXT, payload_json TEXT, created_at TEXT)")
    legacy_top = "旧版反伪影上装 Prompt"
    custom_bottom = "管理员自定义下装 Prompt"
    db.execute("INSERT INTO prompt_configs VALUES ('assembly.final_image_top', ?, 'now')", (legacy_top,))
    db.execute("INSERT INTO prompt_configs VALUES ('assembly.final_image_bottom', ?, 'now')", (custom_bottom,))
    monkeypatch.setattr(
        app_db,
        "LEGACY_PHOTOGRAPHIC_FINAL_PROMPT_SHA256",
        {"assembly.final_image_top": hashlib.sha256(legacy_top.encode("utf-8")).hexdigest()},
    )

    app_db.migration_photographic_final_prompts(db)

    refreshed = db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_top'").fetchone()["content"]
    canonical = (app_db.Path(__file__).resolve().parents[1] / "prompt_templates" / "final_image_top.txt").read_text(encoding="utf-8").strip() + "\n"
    assert refreshed == canonical
    assert "生成一张专业摄影机拍摄的时尚女装电商棚拍成片" in refreshed
    assert db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_bottom'").fetchone()["content"] == custom_bottom


def test_final_prompt_optimizer_is_disabled_only_for_known_system_rules(monkeypatch) -> None:
    from app import db as app_db

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE prompt_configs (key TEXT PRIMARY KEY, content TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, updated_at TEXT)")
    db.execute("CREATE TABLE event_logs (id INTEGER PRIMARY KEY, entity_type TEXT, entity_id INTEGER, event_type TEXT, payload_json TEXT, created_at TEXT)")
    system_rules = "系统 imagegen 摘录"
    db.execute("INSERT INTO prompt_configs VALUES ('assembly.final_prompt_optimizer', ?, 1, 'now')", (system_rules,))
    monkeypatch.setattr(
        app_db,
        "LEGACY_FINAL_PROMPT_OPTIMIZER_SHA256",
        hashlib.sha256(system_rules.encode("utf-8")).hexdigest(),
    )

    app_db.migration_disable_final_prompt_optimizer(db)

    assert db.execute("SELECT enabled FROM prompt_configs WHERE key = 'assembly.final_prompt_optimizer'").fetchone()["enabled"] == 0
    events = db.execute("SELECT event_type FROM event_logs").fetchall()
    assert [row["event_type"] for row in events] == ["final_prompt_optimizer_disabled"]

    db.execute("UPDATE prompt_configs SET content = '管理员自定义优化规则', enabled = 1 WHERE key = 'assembly.final_prompt_optimizer'")
    app_db.migration_disable_final_prompt_optimizer(db)
    row = db.execute("SELECT enabled FROM prompt_configs WHERE key = 'assembly.final_prompt_optimizer'").fetchone()
    assert row["enabled"] == 1


def test_pose_crop_builds_cropped_runtime_copy() -> None:
    from io import BytesIO

    from PIL import Image

    source = BytesIO()
    Image.new("RGB", (120, 200), (12, 24, 36)).save(source, "PNG")
    raw = source.getvalue()

    cropped = image_provider.crop_provider_input_bytes(raw, {"kind": "keep_top_ratio", "ratio": 0.5})

    assert Image.open(BytesIO(cropped)).size == (120, 100)
    with pytest.raises(RuntimeError, match="未知的参考图裁切策略"):
        image_provider.crop_provider_input_bytes(raw, {"kind": "nope"})
    with pytest.raises(RuntimeError, match="必须在 0 和 1 之间"):
        image_provider.crop_provider_input_bytes(raw, {"kind": "keep_top_ratio", "ratio": 1.5})


def test_pose_crop_uploads_cropped_copy_and_records_applied_policy(monkeypatch) -> None:
    from io import BytesIO

    from PIL import Image

    source = BytesIO()
    Image.new("RGB", (100, 200), (30, 40, 50)).save(source, "PNG")
    raw = source.getvalue()
    uploaded: list[tuple[int, str | None]] = []

    monkeypatch.setattr(image_provider, "object_exists", lambda _key: True)
    monkeypatch.setattr(image_provider, "normalized_or_original_key", lambda key: key)
    monkeypatch.setattr(image_provider, "get_bytes", lambda _key: raw)
    monkeypatch.setattr(image_provider, "get_cached_provider_upload_url", lambda _p, _k: None)
    monkeypatch.setattr(image_provider, "store_provider_upload_url", lambda _p, _k, _u: None)

    def fake_upload(content: bytes, filename: str, content_type: str | None = None, deadline: float | None = None) -> str:
        uploaded.append((len(content), content_type))
        return "https://files.toapis.cn/cropped.jpg"

    monkeypatch.setattr(image_provider, "upload_bytes_to_toapis", fake_upload)

    inputs = image_provider.upload_toapis_image_inputs(
        [{"role": "shot_pose", "objectKey": "pose.jpg", "providerCrop": {"kind": "keep_top_ratio", "ratio": 0.5}}]
    )

    assert uploaded and uploaded[0][1] == "image/jpeg"
    assert inputs[0]["providerUrl"] == "https://files.toapis.cn/cropped.jpg"
    assert inputs[0]["providerCropApplied"] == {"kind": "keep_top_ratio", "ratio": 0.5}
    assert inputs[0]["providerInputPolicyVersion"].startswith("toapis-input-")


@pytest.mark.parametrize('count', [1, 2, 3, 4])
def test_model_grid_is_two_by_two_without_cropping_or_repeating_sources(monkeypatch, count):
    from io import BytesIO
    from PIL import Image
    colors = [(220, 10, 10), (10, 220, 10), (10, 10, 220), (220, 220, 10)]
    monkeypatch.setattr(image_provider, 'REFERENCE_COLLAGE_TILE_SIZE', 80)
    def read(source, deadline=None):
        data = BytesIO()
        Image.new('RGB', (40, 80), colors[source['index']]).save(data, 'PNG')
        return data.getvalue(), 'image/png'
    monkeypatch.setattr(image_provider, 'reference_source_bytes', read)
    content, _, _ = image_provider.build_reference_collage_bytes({
        'collageItems': [{'index': i} for i in range(count)],
        'collageRule': {'kind': 'model_identity_grid_2x2'},
    })
    im = Image.open(BytesIO(content))
    assert im.size == (160, 160)
    for i in range(4):
        pixel = im.getpixel(((i % 2) * 80 + 40, (i // 2) * 80 + 40))
        expected = colors[i] if i < count else (255, 255, 255)
        assert max(abs(a-b) for a, b in zip(pixel, expected)) < 5
    # Portrait images are contained: the unused sides stay white, not cropped/stretched.
    assert min(im.getpixel((5, 40))) > 250


def test_new_model_snapshot_freezes_first_four_in_user_order(monkeypatch):
    from app.db import loads
    monkeypatch.setattr(app_main, 'object_exists', lambda key: True)
    with get_db() as db:
        row = db.execute('SELECT * FROM model_profiles ORDER BY is_default DESC, id LIMIT 1').fetchone()
        refs = {f'angle{i}': f'{i}.png' for i in range(5)}
        db.execute('UPDATE model_profiles SET profile_json=? WHERE id=?',
                   (dumps({'headReferences': refs, 'headReferenceOrder': ['angle3', 'angle1', 'angle4', 'angle0', 'angle2']}), row['id']))
    snapshot = app_main.snapshot_default_model()
    assert [x['filename'] for x in snapshot['imageInputs']] == ['3.png', '1.png', '4.png', '0.png']
    with get_db() as db:
        db.execute('UPDATE model_profiles SET profile_json=? WHERE id=?', (dumps({'headReferences': {'front': 'new.png'}}), row['id']))
    assert app_main.resolve_model_snapshot_image_inputs(snapshot, limit=4) == snapshot['imageInputs']


@pytest.mark.parametrize('size', [(30, 40), (40, 30), (40, 40)])
def test_square_canvas_preserves_pixels_and_adds_only_white(size):
    from io import BytesIO
    from PIL import Image
    image = Image.new('RGB', size, (17, 90, 160))
    raw = BytesIO()
    image.save(raw, 'PNG')
    result = Image.open(BytesIO(image_provider.square_white_reference_bytes(raw.getvalue())))
    side = max(size)
    left, top = (side-size[0])//2, (side-size[1])//2
    assert result.size == (side, side)
    assert result.crop((left, top, left+size[0], top+size[1])).tobytes() == image.tobytes()
    if size[0] != size[1]:
        assert result.getpixel((0, 0)) == (255, 255, 255)


def test_square_canvas_applies_orientation_and_transparency():
    from io import BytesIO
    from PIL import Image
    raw = BytesIO()
    image = Image.new('RGBA', (20, 40), (10, 20, 30, 0))
    image.putpixel((0, 0), (200, 100, 50, 255))
    exif = Image.Exif()
    exif[274] = 6
    image.save(raw, 'PNG', exif=exif)
    result = Image.open(BytesIO(image_provider.square_white_reference_bytes(raw.getvalue())))
    assert result.size == (40, 40)
    assert result.getpixel((39, 10)) == (200, 100, 50)
    assert result.getpixel((0, 10)) == (255, 255, 255)


def test_square_canvas_crop_precedes_padding_and_optimizer_matches_upload(monkeypatch):
    from io import BytesIO
    from PIL import Image
    raw = BytesIO()
    original = Image.new('RGB', (30, 80), 'red')
    original.paste('blue', (0, 40, 30, 80))
    original.save(raw, 'PNG')
    monkeypatch.setattr(image_provider, 'reference_source_bytes', lambda *args: (raw.getvalue(), 'image/png'))
    item = {'role': 'shot_pose', 'objectKey': 'pose.png', 'providerCanvas': image_provider.SQUARE_WHITE_CANVAS,
            'providerCrop': {'kind': 'keep_top_ratio', 'ratio': .5},
            'providerUrl': 'https://files.toapis.cn/old.png', 'providerInputPolicyVersion': 'toapis-input-v2'}
    sent = []
    monkeypatch.setattr(image_provider, 'upload_bytes_with_cache', lambda provider, data, fn, **kw: fn())
    monkeypatch.setattr(image_provider, 'upload_bytes_to_toapis', lambda data, *args, **kw: sent.append(data) or 'https://files.toapis.cn/new.png')
    uploaded = image_provider.upload_toapis_image_inputs([item])
    assert len(sent) == 1
    assert app_main.vision_reference_input(item)['data'] == sent[0]
    result = Image.open(BytesIO(sent[0]))
    assert result.size == (40, 40)
    assert result.getpixel((0, 20)) == (255, 255, 255)
    assert result.getpixel((20, 39))[0] > 240
    assert image_provider.upload_toapis_image_inputs(uploaded) == uploaded
    assert len(sent) == 1
    manifest = image_provider.build_provider_reference_image_manifest(uploaded, [uploaded[0]['providerUrl']])
    assert manifest[0]['providerCanvas'] == image_provider.SQUARE_WHITE_CANVAS


def test_square_canvas_before_collage_and_url_input_on_fal(monkeypatch):
    from io import BytesIO
    from PIL import Image
    raw = BytesIO()
    Image.new('RGB', (20, 40), 'red').save(raw, 'PNG')
    monkeypatch.setattr(image_provider, 'download_bytes', lambda *args, **kwargs: (raw.getvalue(), 'image/png'))
    monkeypatch.setattr(image_provider, 'REFERENCE_COLLAGE_TILE_SIZE', 40)
    source = {'url': 'https://example.test/portrait.png', 'providerCanvas': image_provider.SQUARE_WHITE_CANVAS}
    board = {'collageItems': [source, source], 'providerCanvas': image_provider.SQUARE_WHITE_CANVAS}
    content, mime = image_provider.render_reference_input_bytes(board)
    result = Image.open(BytesIO(content))
    assert result.size == (80, 80) and mime == 'image/png'
    assert result.getpixel((20, 40))[0] > 240
    assert result.getpixel((0, 0)) == (255, 255, 255)
    sent = []
    monkeypatch.setattr(image_provider, 'upload_bytes_with_cache', lambda provider, data, fn, **kw: fn())
    monkeypatch.setattr(image_provider, 'upload_bytes_to_fal', lambda data, *args: sent.append(data) or 'https://fal.media/new.png')
    uploaded = image_provider.upload_image_inputs([board])
    assert sent == [content]
    assert image_provider.upload_image_inputs(uploaded) == uploaded
    assert sent == [content]


def test_legacy_reference_without_canvas_keeps_original_bytes(monkeypatch):
    monkeypatch.setattr(image_provider, 'reference_source_bytes', lambda *args: (b'original', 'image/png'))
    assert image_provider.render_reference_input_bytes({'objectKey': 'old'}) == (b'original', 'image/png')
    with pytest.raises(RuntimeError, match='未知的参考图画布策略'):
        image_provider.render_reference_input_bytes({'objectKey': 'old', 'providerCanvas': 'unknown'})
