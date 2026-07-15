from __future__ import annotations

import importlib
import asyncio
import os
from io import BytesIO
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class BackendCurrentFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = dict(os.environ)
        self._temp_dir = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._temp_dir.name) / "data"
        self._db_path = self._data_dir / "app.sqlite3"
        os.environ.update(
            {
                "APP_DATA_DIR": str(self._data_dir),
                "APP_DATABASE_PATH": str(self._db_path),
                "APP_LOG_DIR": str(self._data_dir / "logs"),
                "APP_USER_PASSWORD": "letmein-123456",
                "APP_ADMIN_PASSWORD": "admin-letmein-123456",
                "APP_SESSION_SECRET": "session-secret-123",
                "APP_S3_ACCESS_KEY_ID": "ci-test-access-key",
                "APP_S3_SECRET_ACCESS_KEY": "ci-test-secret-key",
            }
        )
        self._reload_settings()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        config = sys.modules.get("app.config")
        if config is not None:
            config.get_settings.cache_clear()
        self._temp_dir.cleanup()

    def _reload_settings(self) -> None:
        config = importlib.reload(importlib.import_module("app.config"))
        config.get_settings.cache_clear()
        db_module = sys.modules.get("app.db")
        if db_module is not None:
            db_module.get_settings = config.get_settings
        main = sys.modules.get("app.main")
        if main is not None:
            main.settings = config.get_settings()

    def test_legacy_planner_module_is_removed(self) -> None:
        self.assertFalse((BACKEND_DIR / "app" / "planner.py").exists())

    def test_parse_gemini_json_object_accepts_control_characters_in_strings(self) -> None:
        # ToAPIs OpenAI 兼容通道无原生 JSON 模式,模型输出的字符串值可能含未转义换行。
        main = importlib.import_module("app.main")

        text = '```json\n{\n  "镜头": "全身",\n  "备注": "第一行\n第二行"\n}\n```'
        parsed = main.parse_gemini_json_object(text)
        self.assertEqual(parsed["镜头"], "全身")
        self.assertEqual(parsed["备注"], "第一行\n第二行")

    def test_parse_gemini_json_object_accepts_single_quoted_python_dict(self) -> None:
        # 2026-07-03 线上实测形态:模型偶发输出 Python dict 风格(单引号),
        # json.loads 报 "Expecting property name ... (char 1)"。
        main = importlib.import_module("app.main")

        text = "```json\n{'镜头': '全身', 'shots': [{'shot_index': 1}]}\n```"
        parsed = main.parse_gemini_json_object(text)
        self.assertEqual(parsed["镜头"], "全身")
        self.assertEqual(parsed["shots"][0]["shot_index"], 1)

    def test_parse_gemini_json_object_unwraps_double_braces(self) -> None:
        main = importlib.import_module("app.main")

        text = '{{\n  "镜头": "全身"\n}}'
        parsed = main.parse_gemini_json_object(text)
        self.assertEqual(parsed["镜头"], "全身")

    def test_parse_gemini_json_object_failure_carries_raw_excerpt(self) -> None:
        # 解析彻底失败时,错误必须携带模型原文头尾,随 attempt.error_message 落库。
        main = importlib.import_module("app.main")

        text = "{千疮百孔 not json at all" + "x" * 500
        with self.assertRaises(RuntimeError) as ctx:
            main.parse_gemini_json_object(text)
        message = str(ctx.exception)
        self.assertIn("JSON 解析失败", message)
        self.assertIn("千疮百孔", message)
        self.assertIn(str(len(text)), message)

    def test_validate_styling_plan_rejects_empty_production_model_output(self) -> None:
        main = importlib.import_module("app.main")

        plan = main.normalize_styling_plan({}, {"candidate_material_ids": [], "library_tag_cards": []})
        plan["model_raw_output"] = {}

        self.assertEqual(plan["outfit_candidates"], [])
        with self.assertRaisesRegex(RuntimeError, "返回空 JSON"):
            main.validate_styling_plan(plan, {"candidate_material_ids": []})

    def test_normalize_styling_plan_repairs_mismatched_selected_outfit_key_from_material_codes(self) -> None:
        main = importlib.import_module("app.main")
        candidate_snapshot = {
            "candidate_material_ids": [24],
            "library_tag_cards": [{"id": 24, "code": "125309C7948510"}],
        }
        raw_plan = {
            "hero_understanding": {"shot_category_family": "top"},
            "selected_outfit_key": "missing_candidate",
            "selected_material_codes": ["125309C7948510"],
            "outfit_candidates": [
                {
                    "outfit_key": "candidate_a",
                    "selected_material_codes": ["125309C7948510"],
                }
            ],
        }

        plan = main.normalize_styling_plan(raw_plan, candidate_snapshot)
        plan["model_raw_output"] = raw_plan

        self.assertEqual(plan["selected_outfit_key"], "candidate_a")
        self.assertEqual(plan["selected_material_ids"], [24])
        self.assertIn("selected_outfit_key_auto_normalized", plan["warnings"])
        main.validate_styling_plan(plan, candidate_snapshot)

    def test_normalize_styling_plan_does_not_select_first_candidate_without_explicit_choice(self) -> None:
        main = importlib.import_module("app.main")
        candidate_snapshot = {
            "candidate_material_ids": [24],
            "library_tag_cards": [{"id": 24, "code": "125309C7948510"}],
        }
        raw_plan = {
            "outfit_candidates": [
                {
                    "outfit_key": "candidate_a",
                    "selected_material_ids": [24],
                }
            ],
        }

        plan = main.normalize_styling_plan(raw_plan, candidate_snapshot)
        plan["model_raw_output"] = raw_plan

        self.assertEqual(plan["selected_outfit_key"], "")
        self.assertEqual(plan["selected_material_ids"], [])
        self.assertNotIn("selected_outfit_key_auto_normalized", plan["warnings"])
        with self.assertRaisesRegex(RuntimeError, "selected_outfit_key"):
            main.validate_styling_plan(plan, candidate_snapshot)

    def test_prompt_replaced_config_tables_are_dropped_by_migration(self) -> None:
        db_module = importlib.import_module("app.db")
        legacy_tables = [
            "brand_profiles",
            "shoot_configs",
            "tag_dimensions",
            "tag_defs",
            "tag_relations",
            "tag_applicability_rules",
            "garment_tag_assignments",
        ]
        with db_module.get_db() as db:
            for table in legacy_tables:
                db.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")

        db_module.init_db()

        with db_module.get_db() as db:
            rows = db.execute(
                f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({','.join('?' for _ in legacy_tables)})",
                tuple(legacy_tables),
            ).fetchall()
        self.assertEqual([row["name"] for row in rows], [])

    def test_preview_grid_cell_input_crops_current_cell(self) -> None:
        main = importlib.import_module("app.main")
        from PIL import Image

        cell_colors = {
            1: (255, 0, 0),
            2: (0, 255, 0),
            3: (0, 0, 255),
            4: (255, 255, 0),
            5: (255, 0, 255),
            6: (0, 255, 255),
        }
        grid = Image.new("RGB", (300, 200), "white")
        for cell, color in cell_colors.items():
            col = (cell - 1) % 3
            row = (cell - 1) // 3
            block = Image.new("RGB", (100, 100), color)
            grid.paste(block, (col * 100, row * 100))
        buffer = BytesIO()
        grid.save(buffer, format="PNG")
        source_bytes = buffer.getvalue()

        stored: dict[str, bytes] = {}
        original_object_exists = main.object_exists
        original_get_bytes = main.get_bytes
        original_put_bytes = main.put_bytes
        try:
            main.object_exists = lambda key: key == "preview/grid.png" or key in stored
            main.get_bytes = lambda key: source_bytes if key == "preview/grid.png" else stored[key]
            main.put_bytes = lambda key, content, content_type: stored.setdefault(key, content)
            result = main.preview_grid_cell_image_input(
                {"id": 103, "objectKey": "preview/grid.png", "filename": "grid.png", "originalUrl": "/api/media?key=preview/grid.png"},
                5,
            )
        finally:
            main.object_exists = original_object_exists
            main.get_bytes = original_get_bytes
            main.put_bytes = original_put_bytes

        self.assertEqual(result["role"], "preview_grid")
        self.assertEqual(result["sourceObjectKey"], "preview/grid.png")
        self.assertEqual(result["objectKey"], "preview/_derived/final-cells/grid-shot5.png")
        self.assertIn(result["objectKey"], stored)
        with Image.open(BytesIO(stored[result["objectKey"]])) as cropped:
            self.assertEqual(cropped.size, (100, 100))
            self.assertEqual(cropped.getpixel((50, 50)), cell_colors[5])

    def test_storyboard_category_family_requires_explicit_shot_family(self) -> None:
        main = importlib.import_module("app.main")

        with self.assertRaisesRegex(RuntimeError, "shot_category_family"):
            main.storyboard_category_family({"hero_understanding": {"category": "西装马甲"}})

    def test_storyboard_category_family_prefers_explicit_shot_family(self) -> None:
        main = importlib.import_module("app.main")

        self.assertEqual(
            main.storyboard_category_family(
                {
                    "hero_understanding": {
                        "category": "西装马甲",
                        "sub_category": "suit_vest",
                        "shot_category_family": "top",
                    }
                }
            ),
            "top",
        )

    def test_storyboard_category_family_rejects_missing_sub_category_fallback(self) -> None:
        main = importlib.import_module("app.main")

        with self.assertRaisesRegex(RuntimeError, "shot_category_family"):
            main.storyboard_category_family(
                {"hero_understanding": {"category": "西装马甲", "sub_category": "suit_vest"}}
            )

    def test_ai_tagging_parser_repairs_missing_property_commas(self) -> None:
        main = importlib.import_module("app.main")

        parsed = main.parse_lily_tagging_json(
            """
            ```json
            {
              "一级品类": ["针织类"]
              "二级品类": ["针织开衫"],
              "整体视觉语义": "黑色针织开衫，正面菱格图案醒目，适合作为主推款外搭。"
              "结构/廓形语义": "短款开襟结构，袖身较修长，整体量感偏轻。",
              "置信度": 88,
            }
            ```
            """
        )

        self.assertEqual(parsed["一级品类"], ["针织类"])
        self.assertEqual(parsed["二级品类"], ["针织开衫"])
        self.assertEqual(parsed["置信度"], 88)
        self.assertIn("短款开襟结构", parsed["结构/廓形语义"])

    def test_ai_tagging_parser_drops_dangling_model_tail(self) -> None:
        main = importlib.import_module("app.main")

        parsed = main.parse_lily_tagging_json(
            """
            {
              "一级品类": ["外套类"],
              "整体视觉语义": "深色西装外套，线条利落。",
              "风险点": "弱光下颜色容易偏黑。"
              版型数据。"
            }
            """
        )

        self.assertEqual(parsed["一级品类"], ["外套类"])
        self.assertEqual(parsed["风险点"], "弱光下颜色容易偏黑。")

    def test_gemini_json_parser_uses_first_complete_object_before_extra_data(self) -> None:
        main = importlib.import_module("app.main")

        parsed = main.parse_gemini_json_object(
            """
            {
              "schema_version": "styling-plan-v1",
              "selected_outfit": {"title": "轻商务针织搭配"},
              "reason_summary": "已完成选搭。"
            }
            {
              "extra": "模型重复输出的第二段 JSON，不应导致 Extra data 失败。"
            }
            """
        )

        self.assertEqual(parsed["schema_version"], "styling-plan-v1")
        self.assertEqual(parsed["selected_outfit"]["title"], "轻商务针织搭配")
        self.assertNotIn("extra", parsed)

    def test_direct_upload_presign_returns_scoped_put_urls(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient

        db_module.init_db()
        original_presigned_put_url = main.presigned_put_url
        original_ensure_direct_upload_cors = main.ensure_direct_upload_cors
        main.presigned_put_url = lambda key, content_type=None: f"https://oss.example.test/{key}?content-type={content_type}"
        main.ensure_direct_upload_cors = lambda: None
        client = TestClient(main.app)
        try:
            login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-letmein-123456"})
            self.assertEqual(login.status_code, 200)
            response = client.post(
                "/api/uploads/presign",
                json={"scope": "generation_task", "files": [{"filename": "look.jpg", "mimeType": "image/jpeg", "sizeBytes": 1234}]},
            )
            self.assertEqual(response.status_code, 200)
            item = response.json()["items"][0]
            self.assertEqual(item["method"], "PUT")
            self.assertEqual(item["headers"]["Content-Type"], "image/jpeg")
            self.assertTrue(item["objectKey"].startswith("direct-uploads/generation-task/"))
            self.assertIn(item["objectKey"], item["uploadUrl"])
        finally:
            main.presigned_put_url = original_presigned_put_url
            main.ensure_direct_upload_cors = original_ensure_direct_upload_cors
            client.close()

    def test_put_fileobj_uses_plain_put_object_for_s3_compatibility(self) -> None:
        object_storage = importlib.import_module("app.object_storage")
        from io import BytesIO

        calls = []

        class FakeS3Client:
            def put_object(self, **kwargs):
                calls.append(("put_object", kwargs))

            def upload_fileobj(self, *args, **kwargs):
                raise AssertionError("upload_fileobj should not be used for S3-compatible uploads")

        original_s3_client = object_storage.s3_client
        object_storage.s3_client = lambda: FakeS3Client()
        try:
            object_storage.put_fileobj("uploads/example.jpg", BytesIO(b"image-bytes"), "image/jpeg")
        finally:
            object_storage.s3_client = original_s3_client

        self.assertEqual(calls[0][0], "put_object")
        self.assertEqual(calls[0][1]["Key"], "uploads/example.jpg")
        self.assertEqual(calls[0][1]["Body"], b"image-bytes")
        self.assertEqual(calls[0][1]["ContentType"], "image/jpeg")

    def test_direct_upload_cors_is_created_when_bucket_has_no_cors(self) -> None:
        object_storage = importlib.import_module("app.object_storage")
        from botocore.exceptions import ClientError

        calls = []

        class FakeS3Client:
            def get_bucket_cors(self, **kwargs):
                raise ClientError(
                    {
                        "Error": {"Code": "NoSuchCORSConfiguration"},
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                    },
                    "GetBucketCors",
                )

            def put_bucket_cors(self, **kwargs):
                calls.append(kwargs)

        original_s3_client = object_storage.s3_client
        object_storage.s3_client = lambda: FakeS3Client()
        try:
            object_storage.ensure_direct_upload_cors()
        finally:
            object_storage.s3_client = original_s3_client

        self.assertEqual(len(calls), 1)
        rules = calls[0]["CORSConfiguration"]["CORSRules"]
        self.assertEqual(rules[0]["ID"], object_storage.DIRECT_UPLOAD_CORS_RULE_ID)
        self.assertIn("http://127.0.0.1:5173", rules[0]["AllowedOrigins"])
        self.assertIn("PUT", rules[0]["AllowedMethods"])
        self.assertEqual(rules[0]["AllowedHeaders"], ["*"])

    def test_direct_upload_cors_preserves_existing_bucket_rules(self) -> None:
        object_storage = importlib.import_module("app.object_storage")

        existing_rule = {
            "ID": "existing-read-rule",
            "AllowedMethods": ["GET"],
            "AllowedOrigins": ["https://assets.example.test"],
            "AllowedHeaders": ["*"],
        }
        calls = []

        class FakeS3Client:
            def get_bucket_cors(self, **kwargs):
                return {"CORSRules": [existing_rule]}

            def put_bucket_cors(self, **kwargs):
                calls.append(kwargs)

        original_s3_client = object_storage.s3_client
        object_storage.s3_client = lambda: FakeS3Client()
        try:
            object_storage.ensure_direct_upload_cors()
        finally:
            object_storage.s3_client = original_s3_client

        rules = calls[0]["CORSConfiguration"]["CORSRules"]
        self.assertEqual(rules[0], existing_rule)
        self.assertEqual(rules[1]["ID"], object_storage.DIRECT_UPLOAD_CORS_RULE_ID)

    def test_direct_upload_cors_deduplicates_aliyun_rules_without_ids(self) -> None:
        object_storage = importlib.import_module("app.object_storage")
        allowed_origins = object_storage.get_settings().direct_upload_cors_origins

        aliyun_returned_rule = {
            "AllowedMethods": ["PUT", "GET", "HEAD"],
            "AllowedOrigins": allowed_origins,
            "AllowedHeaders": ["*"],
            "ExposeHeaders": ["ETag"],
            "MaxAgeSeconds": 3600,
        }
        calls = []

        class FakeS3Client:
            def get_bucket_cors(self, **kwargs):
                return {"CORSRules": [aliyun_returned_rule, dict(aliyun_returned_rule)]}

            def put_bucket_cors(self, **kwargs):
                calls.append(kwargs)

        original_s3_client = object_storage.s3_client
        object_storage.s3_client = lambda: FakeS3Client()
        try:
            object_storage.ensure_direct_upload_cors()
        finally:
            object_storage.s3_client = original_s3_client

        rules = calls[0]["CORSConfiguration"]["CORSRules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["ID"], object_storage.DIRECT_UPLOAD_CORS_RULE_ID)

    def test_create_generation_task_from_direct_uploads_registers_media_asset(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient
        from io import BytesIO
        from PIL import Image

        db_module.init_db()
        output = BytesIO()
        Image.new("RGB", (32, 48), color=(128, 160, 192)).save(output, format="JPEG")
        image_bytes = output.getvalue()
        object_key = "direct-uploads/generation-task/test-batch/001-look.jpg"
        original_object_head = main.object_head
        original_get_bytes = main.get_bytes
        original_put_bytes = main.put_bytes
        original_generate_image_derivatives = main.generate_image_derivatives
        main.object_head = lambda key: {"contentLength": len(image_bytes), "contentType": "image/jpeg"} if key == object_key else None
        main.get_bytes = lambda key: image_bytes if key == object_key else b""
        main.put_bytes = lambda key, content, content_type: None
        main.generate_image_derivatives = lambda key, content: {
            "thumb": f"{key}.thumb.webp",
            "preview": f"{key}.preview.webp",
            "thumbSize": 12,
            "previewSize": 34,
        }
        client = TestClient(main.app)
        try:
            login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-letmein-123456"})
            self.assertEqual(login.status_code, 200)
            response = client.post(
                "/api/generation-tasks/from-uploads",
                json={
                    "name": "Direct Upload Task",
                    "heroProductInfo": "格纹连衣裙",
                    "uploads": [{"objectKey": object_key, "filename": "look.jpg", "mimeType": "image/jpeg", "sizeBytes": len(image_bytes)}],
                },
            )
            self.assertEqual(response.status_code, 200)
            item = response.json()["item"]
            self.assertEqual(item["name"], "Direct Upload Task")
            self.assertEqual(item["images"][0]["asset"]["objectKey"], object_key)
            self.assertEqual(item["images"][0]["asset"]["width"], 32)
            self.assertEqual(item["images"][0]["asset"]["height"], 48)
            with db_module.get_db() as db:
                row = db.execute("SELECT object_key, thumb_key, preview_key FROM media_assets WHERE object_key = ?", (object_key,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["thumb_key"], f"{object_key}.thumb.webp")
            self.assertEqual(row["preview_key"], f"{object_key}.preview.webp")
        finally:
            main.object_head = original_object_head
            main.get_bytes = original_get_bytes
            main.put_bytes = original_put_bytes
            main.generate_image_derivatives = original_generate_image_derivatives
            client.close()

    def test_created_generation_task_rejects_adding_images(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient

        db_module.init_db()
        client = TestClient(main.app)
        try:
            login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-letmein-123456"})
            self.assertEqual(login.status_code, 200)
            response = client.post(
                "/api/generation-tasks/1/images",
                files={"files": ("look.jpg", b"not-used", "image/jpeg")},
            )
            self.assertEqual(response.status_code, 410)
            self.assertIn("创建后不支持继续添加主推款图片", response.json()["detail"])

            response = client.post(
                "/api/generation-tasks/1/images/from-uploads",
                json={"uploads": [{"objectKey": "direct-uploads/generation-task/a.jpg", "filename": "a.jpg"}]},
            )
            self.assertEqual(response.status_code, 410)
            self.assertIn("创建后不支持继续添加主推款图片", response.json()["detail"])
        finally:
            client.close()

    def test_created_styling_garment_rejects_adding_images(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient

        db_module.init_db()
        client = TestClient(main.app)
        try:
            login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-letmein-123456"})
            self.assertEqual(login.status_code, 200)
            response = client.post(
                "/api/styling-garments/1/images",
                files={"files": ("look.jpg", b"not-used", "image/jpeg")},
            )
            self.assertEqual(response.status_code, 410)
            self.assertIn("创建后不支持继续添加图片", response.json()["detail"])
        finally:
            client.close()

    def test_auto_run_final_sets_resume_marker_without_calling_provider(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient

        db_module.init_db()
        timestamp = "2026-06-03T04:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                "INSERT INTO generation_tasks (id, name, status, created_at, updated_at) VALUES (1, 'T', 'READY_FOR_STYLING', ?, ?)",
                (timestamp, timestamp),
            )

        scheduled: list[int] = []
        original_schedule = main.schedule_auto_run_for_task

        def capture_schedule(task_id: int) -> None:
            scheduled.append(task_id)

        main.schedule_auto_run_for_task = capture_schedule
        client = TestClient(main.app)
        try:
            login = client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"})
            self.assertEqual(login.status_code, 200)
            response = client.post("/api/generation-tasks/1/auto-run-final")
        finally:
            client.close()
            main.schedule_auto_run_for_task = original_schedule

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["autoRunTarget"], "FINAL")
        self.assertEqual(scheduled, [1])
        with db_module.get_db() as db:
            row = db.execute("SELECT auto_run_target FROM generation_tasks WHERE id = 1").fetchone()
        self.assertEqual(row["auto_run_target"], "FINAL")

    def test_cancelled_task_can_continue_one_click_final(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient

        db_module.init_db()
        timestamp = "2026-07-13T04:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                "INSERT INTO generation_tasks (id, name, status, created_at, updated_at) VALUES (1, 'Cancelled', 'CANCELLED', ?, ?)",
                (timestamp, timestamp),
            )

        scheduled: list[int] = []
        original_schedule = main.schedule_auto_run_for_task
        main.schedule_auto_run_for_task = scheduled.append
        client = TestClient(main.app)
        try:
            self.assertEqual(client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"}).status_code, 200)
            response = client.post("/api/generation-tasks/1/auto-run-final")
        finally:
            client.close()
            main.schedule_auto_run_for_task = original_schedule

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["status"], "READY_FOR_STYLING")
        self.assertEqual(response.json()["item"]["autoRunTarget"], "FINAL")
        self.assertEqual(scheduled, [1])

    def test_batch_one_click_final_includes_cancelled_and_skips_completed(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient

        db_module.init_db()
        timestamp = "2026-07-13T04:00:00Z"
        with db_module.get_db() as db:
            db.executemany(
                "INSERT INTO generation_tasks (id, name, batch_id, status, created_at, updated_at) VALUES (?, ?, 'batch-1', ?, ?, ?)",
                [
                    (1, "Cancelled", "CANCELLED", timestamp, timestamp),
                    (2, "Pending", "READY_FOR_STYLING", timestamp, timestamp),
                    (3, "Done", "COMPLETED", timestamp, timestamp),
                ],
            )

        scheduled: list[int] = []
        original_schedule = main.schedule_auto_run_for_task
        main.schedule_auto_run_for_task = scheduled.append
        client = TestClient(main.app)
        try:
            self.assertEqual(client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"}).status_code, 200)
            response = client.post("/api/generation-batches/batch-1/auto-run-final")
        finally:
            client.close()
            main.schedule_auto_run_for_task = original_schedule

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["startedTaskIds"], [1, 2])
        items = {item["id"]: item for item in response.json()["items"]}
        self.assertEqual(items[1]["status"], "READY_FOR_STYLING")
        self.assertEqual(items[1]["autoRunTarget"], "FINAL")
        self.assertIsNone(items[3]["autoRunTarget"])
        self.assertEqual(scheduled, [1, 2])

    def test_completed_batch_can_restart_all_tasks(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient

        db_module.init_db()
        timestamp = "2026-07-13T05:00:00Z"
        with db_module.get_db() as db:
            db.executemany(
                "INSERT INTO generation_tasks (id, name, batch_id, status, created_at, updated_at) VALUES (?, ?, 'batch-restart', 'COMPLETED', ?, ?)",
                [(1, "Done 1", timestamp, timestamp), (2, "Done 2", timestamp, timestamp)],
            )

        scheduled: list[int] = []
        original_schedule = main.schedule_auto_run_for_task
        main.schedule_auto_run_for_task = scheduled.append
        client = TestClient(main.app)
        try:
            self.assertEqual(client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"}).status_code, 200)
            response = client.post("/api/generation-batches/batch-restart/auto-run-final", json={"restart": True})
        finally:
            client.close()
            main.schedule_auto_run_for_task = original_schedule

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["startedTaskIds"], [1, 2])
        self.assertEqual(scheduled, [1, 2])
        self.assertTrue(all(item["status"] == "READY_FOR_STYLING" for item in response.json()["items"]))
        self.assertTrue(all(item["autoRunTarget"] == "FINAL" for item in response.json()["items"]))

    def test_batch_cancel_stops_running_and_dequeues_waiting_tasks(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient

        db_module.init_db()
        timestamp = "2026-07-13T05:00:00Z"
        with db_module.get_db() as db:
            db.executemany(
                """INSERT INTO generation_tasks
                   (id, name, batch_id, status, auto_run_target, auto_run_requested_at, created_at, updated_at)
                   VALUES (?, ?, 'batch-cancel', ?, 'FINAL', ?, ?, ?)""",
                [
                    (1, "Running", "STYLING_RUNNING", timestamp, timestamp, timestamp),
                    (2, "Queued", "READY_FOR_STYLING", timestamp, timestamp, timestamp),
                ],
            )

        client = TestClient(main.app)
        try:
            self.assertEqual(client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"}).status_code, 200)
            response = client.post("/api/generation-batches/batch-cancel/cancel")
        finally:
            client.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cancelledTaskIds"], [1, 2])
        items = {item["id"]: item for item in response.json()["items"]}
        self.assertEqual(items[1]["status"], "CANCELLED")
        self.assertIsNone(items[1]["autoRunTarget"])
        self.assertEqual(items[2]["status"], "READY_FOR_STYLING")
        self.assertIsNone(items[2]["autoRunTarget"])

    def test_cancelled_regeneration_resumes_the_cancelled_stage_not_old_active_artifacts(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        db_module.init_db()
        old_timestamp = "2026-07-13T03:00:00Z"
        cancelled_at = "2026-07-13T04:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                "INSERT INTO generation_tasks (id, name, status, active_styling_id, active_preview_id, active_storyboard_preview_id, created_at, updated_at) VALUES (1, 'Cancelled preview', 'CANCELLED', NULL, NULL, NULL, ?, ?)",
                (old_timestamp, cancelled_at),
            )
            input_version_id = int(db.execute(
                "INSERT INTO generation_task_input_versions (task_id, version_number, name_snapshot, image_snapshot_json, created_at) VALUES (1, 1, 'Cancelled preview', '[]', ?)",
                (old_timestamp,),
            ).lastrowid)
            styling_id = int(db.execute(
                """INSERT INTO generation_stylings
                   (task_id, input_version_id, source_material_ids_json, selected_material_ids_json,
                    selected_material_snapshot_json, status, created_at, updated_at)
                   VALUES (1, ?, '[]', '[]', '[]', 'SUCCEEDED', ?, ?)""",
                (input_version_id, old_timestamp, old_timestamp),
            ).lastrowid)
            preview_id = int(db.execute(
                "INSERT INTO generation_previews (task_id, styling_id, input_version_id, status, created_at, updated_at) VALUES (1, ?, ?, 'CANCELLED', ?, ?)",
                (styling_id, input_version_id, cancelled_at, cancelled_at),
            ).lastrowid)
            db.execute(
                "INSERT INTO generation_attempts (owner_type, owner_id, provider, status, cancelled_at, created_at, updated_at) VALUES ('generation_preview', ?, 'image-2', 'CANCELLED', ?, ?, ?)",
                (preview_id, cancelled_at, cancelled_at, cancelled_at),
            )
            resume_status = main.resume_cancelled_generation_task(db, 1, cancelled_at)

        self.assertEqual(resume_status, "STYLING_READY")
        with db_module.get_db() as db:
            row = db.execute("SELECT status FROM generation_tasks WHERE id = 1").fetchone()
        self.assertEqual(row["status"], "STYLING_READY")
        self.assertEqual(main.next_auto_final_step({"status": "STYLING_READY", "active_styling_id": 99}), "preview")

    def test_toapis_request_snapshot_keeps_reference_manifest_roles(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        provider_request = image_provider.build_toapis_request_snapshot(
            {
                "generation": {"stage": "final_image"},
                "prompt": "生成成片",
                "imageInputs": [
                    {"role": "model_identity", "assetId": 1, "objectKey": "model/front.png", "filename": "front.png", "angle": "front"},
                    {"role": "preview_grid", "assetId": 2, "objectKey": "preview/grid.png", "filename": "grid.png"},
                ],
            },
            ["https://example.com/model.png", "https://example.com/grid.png"],
        )

        self.assertEqual(provider_request["reference_images"], ["https://example.com/model.png", "https://example.com/grid.png"])
        self.assertEqual(
            [(item["reference_index"], item["role"]) for item in provider_request["reference_image_manifest"]],
            [(1, "model_identity"), (2, "preview_grid")],
        )

    def test_model_snapshot_missing_head_reference_falls_back_to_current_profile(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()
        with db_module.get_db() as db:
            db.execute(
                """
                UPDATE model_profiles
                SET profile_json = ?
                WHERE model_id = 'Face-01'
                """,
                (
                    main.dumps(main.normalize_model_profile({
                        "headReferences": {"front": "front-1.png", "left45": "left45-1.png"},
                        "headReferenceOrder": ["front", "left45"],
                    })),
                ),
            )

        original_object_exists = main.object_exists
        try:
            main.object_exists = lambda key: str(key).endswith(("front-1.png", "left45-1.png"))
            resolved = main.resolve_model_snapshot_image_inputs(
                {
                    "id": 1,
                    "modelId": "Face-01",
                    "imageInputs": [
                        {"role": "model_identity", "objectKey": "model-profiles/Face-01/front.png", "filename": "front.png", "angle": "front"},
                        {"role": "model_identity", "objectKey": "model-profiles/Face-01/left45.png", "filename": "left45.png", "angle": "left45"},
                    ],
                },
                limit=2,
            )
        finally:
            main.object_exists = original_object_exists

        self.assertEqual([item["objectKey"] for item in resolved], [
            "model-profiles/Face-01/front-1.png",
            "model-profiles/Face-01/left45-1.png",
        ])

    def test_seed_default_model_does_not_hardcode_head_reference_files(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()

        with db_module.get_db() as db:
            row = db.execute("SELECT profile_json FROM model_profiles WHERE model_id = 'Face-01'").fetchone()
        profile = main.loads(row["profile_json"], {})

        self.assertEqual(profile["headReferences"], {})
        self.assertEqual(profile["headReferenceOrder"], [])

    def test_explicit_model_snapshot_missing_profile_does_not_fall_back_to_default_model(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()

        original_object_exists = main.object_exists
        try:
            main.object_exists = lambda key: False
            with self.assertRaisesRegex(RuntimeError, "模特参考图缺失"):
                main.resolve_model_snapshot_image_inputs(
                    {
                        "id": 999,
                        "modelId": "Deleted-Model",
                        "imageInputs": [
                            {"role": "model_identity", "objectKey": "model-profiles/Deleted-Model/front.png", "filename": "front.png", "angle": "front"},
                        ],
                    },
                    limit=2,
                )
        finally:
            main.object_exists = original_object_exists

    def test_referenced_model_asset_file_is_not_removed(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()
        timestamp = "2026-07-13T08:00:00Z"
        old_key = "model-profiles/Face-01/front.png"
        with db_module.get_db() as db:
            db.execute(
                "INSERT INTO generation_tasks (id, name, status, created_at, updated_at) VALUES (1, 'Model snapshot', 'DRAFT', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_task_input_versions
                  (task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at)
                VALUES (1, 1, 'Model snapshot', '[]', ?, ?)
                """,
                (
                    main.dumps({"imageInputs": [{"role": "model_identity", "objectKey": old_key, "filename": "front.png"}]}),
                    timestamp,
                ),
            )

        removed: list[list[str]] = []
        original_cleanup = main.cleanup_objects
        try:
            main.cleanup_objects = lambda keys: removed.append(list(keys))
            main.remove_model_asset_file("model-profiles/Face-01", "front.png")
        finally:
            main.cleanup_objects = original_cleanup

        self.assertEqual(removed, [])

    def test_response_snapshot_model_asset_reference_is_not_removed(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()
        timestamp = "2026-07-13T08:10:00Z"
        old_key = "model-profiles/Face-01/left45.png"
        with db_module.get_db() as db:
            db.execute(
                """
                INSERT INTO generation_attempts
                  (owner_type, owner_id, provider, status, response_snapshot_json, created_at, updated_at)
                VALUES ('generation_task', 1, 'image-2', 'SUCCEEDED', ?, ?, ?)
                """,
                (
                    main.dumps({"imageInputs": [{"role": "model_identity", "objectKey": old_key}]}),
                    timestamp,
                    timestamp,
                ),
            )

        removed: list[list[str]] = []
        original_cleanup = main.cleanup_objects
        try:
            main.cleanup_objects = lambda keys: removed.append(list(keys))
            main.remove_model_asset_file("model-profiles/Face-01", "left45.png")
        finally:
            main.cleanup_objects = original_cleanup

        self.assertEqual(removed, [])

    def test_referenced_model_asset_dir_is_not_deleted(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()
        timestamp = "2026-07-13T08:20:00Z"
        with db_module.get_db() as db:
            db.execute(
                """
                INSERT INTO generation_attempts
                  (owner_type, owner_id, provider, status, request_snapshot_json, created_at, updated_at)
                VALUES ('generation_task', 1, 'image-2', 'SUCCEEDED', ?, ?, ?)
                """,
                (
                    main.dumps({"imageInputs": [{"role": "model_identity", "objectKey": "model-profiles/Face-01/front.png"}]}),
                    timestamp,
                    timestamp,
                ),
            )

        self.assertTrue(main.model_asset_dir_is_referenced("model-profiles/Face-01"))

    def test_final_schema_has_no_dynamic_shoot_script_runtime(self) -> None:
        db_module = importlib.import_module("app.db")
        db_module.init_db()
        with db_module.get_db() as db:
            tables = {
                row["name"]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            prompt_keys = {
                row["key"]
                for row in db.execute("SELECT key FROM prompt_configs").fetchall()
            }
            self.assertFalse(db_module.table_has_column(db, "generation_tasks", "active_shoot_script_id"))
            self.assertFalse(db_module.table_has_column(db, "generation_tasks", "shoot_script_user_instruction_text"))
            self.assertFalse(db_module.table_has_column(db, "generation_previews", "shoot_script_id"))
        self.assertNotIn("generation_shoot_scripts", tables)
        self.assertNotIn("generation_shot_plans", tables)
        self.assertNotIn("assembly.shoot_script", prompt_keys)
        self.assertNotIn("assembly.final_image", prompt_keys)
        self.assertIn("assembly.garment_image_selection", prompt_keys)
        self.assertIn("assembly.hero_image_selection", prompt_keys)
        self.assertIn("assembly.storyboard_preview_top", prompt_keys)
        self.assertIn("assembly.final_image_top", prompt_keys)

    def test_selected_files_helper_accepts_stable_refs_and_unique_filenames(self) -> None:
        main = importlib.import_module("app.main")
        images = [
            {"id": 11, "sortOrder": 0, "asset": {"filename": "front.jpg"}},
            {"id": 12, "sortOrder": 1, "asset": {"filename": "back.jpg"}},
            {"id": 13, "sortOrder": 2, "asset": {"filename": "detail.jpg"}},
        ]

        files = main.image_selection_prompt_files(images, 15)

        self.assertEqual(files[0]["file"], "image:11:front.jpg")
        self.assertEqual(
            main.selected_image_ids_from_files(["image:12:back.jpg", "front.jpg"], images, 15),
            [12, 11],
        )

    def test_selected_files_helper_rejects_bad_refs(self) -> None:
        main = importlib.import_module("app.main")
        images = [
            {"id": 11, "sortOrder": 0, "asset": {"filename": "same.jpg"}},
            {"id": 12, "sortOrder": 1, "asset": {"filename": "same.jpg"}},
        ]

        with self.assertRaisesRegex(ValueError, "不唯一"):
            main.selected_image_ids_from_files(["same.jpg"], images, 15)
        with self.assertRaisesRegex(ValueError, "不存在"):
            main.selected_image_ids_from_files(["missing.jpg"], images, 15)
        with self.assertRaisesRegex(ValueError, "重复"):
            main.selected_image_ids_from_files(["image:11:same.jpg", "11"], images, 15)

    def test_ai_tagging_normalizer_requires_selected_files_and_ai_tags(self) -> None:
        main = importlib.import_module("app.main")
        garment = {
            "images": [
                {"id": 11, "sortOrder": 0, "asset": {"filename": "front.jpg"}},
                {"id": 12, "sortOrder": 1, "asset": {"filename": "back.jpg"}},
            ]
        }

        result = main.normalize_prompt_tagging_result(
            {
                "selected_files": ["back.jpg", "front.jpg"],
                "ai_tags": {"一级品类": ["连衣裙"], "置信度": 92},
            },
            garment,
        )

        self.assertEqual(result["selected_image_ids"], [12, 11])
        self.assertEqual(result["ai_tags"]["一级品类"], ["连衣裙"])

        with self.assertRaisesRegex(ValueError, "selected_files"):
            main.normalize_prompt_tagging_result({"ai_tags": {}}, garment)
        with self.assertRaisesRegex(ValueError, "ai_tags"):
            main.normalize_prompt_tagging_result({"selected_files": ["front.jpg"]}, garment)

    def test_ai_stylist_normalizer_requires_selected_files_and_styling_plan(self) -> None:
        main = importlib.import_module("app.main")
        input_row = {
            "image_snapshot_json": main.dumps([
                {"id": 21, "sortOrder": 0, "asset": {"filename": "hero-front.jpg"}},
                {"id": 22, "sortOrder": 1, "asset": {"filename": "hero-back.jpg"}},
            ])
        }
        candidate_snapshot = {"candidate_material_ids": [], "library_tag_cards": []}

        result = main.normalize_ai_stylist_result(
            {
                "selected_files": ["hero-back.jpg", "hero-front.jpg"],
                "styling_plan": {
                    "hero_understanding": {"shot_category_family": "top"},
                    "outfit_candidates": [{"outfit_key": "solo", "selected_material_ids": []}],
                    "selected_outfit_key": "solo",
                },
            },
            input_row,
            candidate_snapshot,
        )

        self.assertEqual(result["selected_hero_image_ids"], [22, 21])
        self.assertEqual(result["hero_understanding"]["shot_category_family"], "top")

        with self.assertRaisesRegex(ValueError, "selected_files"):
            main.normalize_ai_stylist_result({"styling_plan": {}}, input_row, candidate_snapshot)
        with self.assertRaisesRegex(ValueError, "styling_plan"):
            main.normalize_ai_stylist_result({"selected_files": ["hero-front.jpg"]}, input_row, candidate_snapshot)

    def test_generation_input_selected_image_order_is_persisted(self) -> None:
        main = importlib.import_module("app.main")
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE generation_task_input_versions (id INTEGER PRIMARY KEY, image_snapshot_json TEXT)")
        db.execute(
            "INSERT INTO generation_task_input_versions (id, image_snapshot_json) VALUES (?, ?)",
            (
                1,
                main.dumps([
                    {"id": 21, "sortOrder": 0, "asset": {"filename": "hero-front.jpg"}},
                    {"id": 22, "sortOrder": 1, "asset": {"filename": "hero-back.jpg"}},
                    {"id": 23, "sortOrder": 2, "asset": {"filename": "hero-detail.jpg"}},
                ]),
            ),
        )

        main.apply_generation_input_selected_image_order(db, 1, [22, 21])

        row = db.execute("SELECT image_snapshot_json FROM generation_task_input_versions WHERE id = 1").fetchone()
        snapshot = main.loads(row["image_snapshot_json"], [])
        self.assertEqual([item["id"] for item in snapshot], [22, 21, 23])
        self.assertEqual([item["sortOrder"] for item in snapshot], [0, 1, 2])

    def test_garment_tagging_store_failure_marks_failed(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        db_module.init_db()
        timestamp = "2026-07-15T00:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                """
                INSERT INTO styling_garments
                  (id, code, status, manual_tags_json, ai_tags_json, tag_schema_version, created_at, updated_at)
                VALUES (1, 'LOOK-1', 'TAGGING_RUNNING', '{}', '{}', 'test', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_attempts
                  (id, owner_type, owner_id, provider, status, response_snapshot_json, created_at, updated_at)
                VALUES (41, 'styling_garment_tagging', 1, 'gemini', 'SUCCEEDED', '{}', ?, ?)
                """,
                (timestamp, timestamp),
            )

        original_tag = main.tag_styling_garment_with_gemini

        async def fake_tag(_garment_id: int) -> dict[str, object]:
            return {
                "selected_image_ids": [999],
                "ai_tags": {"一级品类": ["上装"]},
                "tag_meta": {"confidence": 95},
                "attempt_id": 41,
            }

        main.tag_styling_garment_with_gemini = fake_tag
        try:
            asyncio.run(main._run_styling_garment_tagging_guarded(1))
        finally:
            main.tag_styling_garment_with_gemini = original_tag

        with db_module.get_db() as db:
            row = db.execute("SELECT status, tag_error FROM styling_garments WHERE id = 1").fetchone()
            attempt = db.execute("SELECT status, error_message FROM generation_attempts WHERE id = 41").fetchone()
        self.assertEqual(row["status"], "TAGGING_FAILED")
        self.assertIn("selected_files", row["tag_error"])
        self.assertEqual(attempt["status"], "FAILED")
        self.assertIn("selected_files", attempt["error_message"])

    def test_ai_stylist_uses_single_gemini_json_call_without_reask(self) -> None:
        main = importlib.import_module("app.main")
        input_row = {
            "image_snapshot_json": main.dumps([
                {"id": 21, "sortOrder": 0, "asset": {"filename": "hero-front.jpg"}},
            ])
        }
        candidate_snapshot = {"candidate_material_ids": [], "library_tag_cards": []}
        request_snapshot = {"prompt": "Return JSON.", "systemInstruction": None}
        calls: list[str] = []

        original_configured = main.is_gemini_configured
        original_text = main.generate_gemini_json_text
        original_reask = main.generate_gemini_json_object_with_reask

        async def fake_text(*_args: object, **_kwargs: object) -> str:
            calls.append("text")
            return main.dumps({
                "selected_files": ["hero-front.jpg"],
                "styling_plan": {
                    "hero_understanding": {"shot_category_family": "top"},
                    "outfit_candidates": [{"outfit_key": "solo", "selected_material_ids": []}],
                    "selected_outfit_key": "solo",
                },
            })

        async def fake_reask(*_args: object, **_kwargs: object) -> dict[str, object]:
            calls.append("reask")
            raise AssertionError("AI Stylist should not use malformed-output reask")

        main.is_gemini_configured = lambda: True
        main.generate_gemini_json_text = fake_text
        main.generate_gemini_json_object_with_reask = fake_reask
        try:
            result = asyncio.run(main.select_styling_with_gemini(input_row, candidate_snapshot, request_snapshot))
        finally:
            main.is_gemini_configured = original_configured
            main.generate_gemini_json_text = original_text
            main.generate_gemini_json_object_with_reask = original_reask

        self.assertEqual(calls, ["text"])
        self.assertEqual(result["selected_hero_image_ids"], [21])

    def test_ai_stylist_malformed_json_fails_without_reask(self) -> None:
        main = importlib.import_module("app.main")
        input_row = {
            "image_snapshot_json": main.dumps([
                {"id": 21, "sortOrder": 0, "asset": {"filename": "hero-front.jpg"}},
            ])
        }
        candidate_snapshot = {"candidate_material_ids": [], "library_tag_cards": []}
        request_snapshot = {"prompt": "Return JSON.", "systemInstruction": None}
        calls: list[str] = []

        original_configured = main.is_gemini_configured
        original_text = main.generate_gemini_json_text
        original_reask = main.generate_gemini_json_object_with_reask

        async def fake_text(*_args: object, **_kwargs: object) -> str:
            calls.append("text")
            return "not json"

        async def fake_reask(*_args: object, **_kwargs: object) -> dict[str, object]:
            calls.append("reask")
            return {}

        main.is_gemini_configured = lambda: True
        main.generate_gemini_json_text = fake_text
        main.generate_gemini_json_object_with_reask = fake_reask
        try:
            with self.assertRaisesRegex(Exception, "Gemini AI Stylist 调用失败") as raised:
                asyncio.run(main.select_styling_with_gemini(input_row, candidate_snapshot, request_snapshot))
        finally:
            main.is_gemini_configured = original_configured
            main.generate_gemini_json_text = original_text
            main.generate_gemini_json_object_with_reask = original_reask

        self.assertEqual(calls, ["text"])
        self.assertEqual(main.classify_attempt_error(raised.exception), main.ERROR_MALFORMED_OUTPUT)


if __name__ == "__main__":
    unittest.main()
