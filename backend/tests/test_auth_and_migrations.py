from __future__ import annotations

import importlib
import os
from pathlib import Path
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
            migration = db.execute(
                "SELECT version FROM schema_migrations WHERE version = '034' AND name = 'drop_prompt_replaced_config_tables'"
            ).fetchone()
        self.assertEqual([row["name"] for row in rows], [])
        self.assertIsNotNone(migration)

    def test_legacy_preview_tasks_recover_shoot_script_pointers(self) -> None:
        db_module = importlib.import_module("app.db")

        db_module.init_db()
        timestamp = "2026-06-04T08:00:00Z"
        styling_plan = {
            "schema_version": "styling-plan-v1",
            "hero_understanding": {
                "hero_visual_summary": "淡蓝色印花半裙",
                "category": "bottom",
                "hero_selling_points": ["高腰线", "印花裙摆"],
                "hero_visibility_risks": ["上衣不要遮挡腰头"],
            },
            "outfit_candidates": [
                {
                    "id": "candidate_13",
                    "selected_material_ids": [13],
                    "generated_accessories": ["低存在感米色鞋"],
                    "scores": {"aesthetic_score": 1},
                }
            ],
            "selected_outfit_id": "candidate_13",
            "selected_material_ids": [13],
            "reason_summary": "历史选搭成功。",
        }
        with db_module.get_db() as db:
            db.execute(
                """
                INSERT INTO media_assets
                  (id, object_key, original_filename, mime_type, status, created_at, updated_at)
                VALUES (296, 'generation-tasks/8/preview/preview-13.png', 'preview-13.png', 'image/png', 'READY', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_tasks
                  (id, name, status, current_input_version_id, active_styling_id, active_preview_id,
                   created_at, updated_at)
                VALUES (8, '20260527132852', 'FAILED', 11, 11, 13, ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_task_input_versions
                  (id, task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at)
                VALUES (11, 8, 1, '20260527132852', '[]', '{}', ?)
                """,
                (timestamp,),
            )
            db.execute(
                """
                INSERT INTO generation_stylings
                  (id, task_id, input_version_id, source_material_ids_json, selected_material_ids_json,
                   selected_material_snapshot_json, styling_plan_json, hero_understanding_json,
                   outfit_candidates_json, rejected_candidates_json, reason_summary, warnings_json,
                   status, created_at, updated_at)
                VALUES (11, 8, 11, '[]', '[13]', '[]', ?, '{}', '[]', '[]',
                        '历史选搭成功。', '[]', 'SUCCEEDED', ?, ?)
                """,
                (db_module.dumps(styling_plan), timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_previews
                  (id, task_id, styling_id, input_version_id, preview_asset_id, status, created_at, updated_at)
                VALUES (13, 8, 11, 11, 296, 'SUCCEEDED', ?, ?)
                """,
                (timestamp, timestamp),
            )
            for sort_order in range(1, 7):
                db.execute(
                    """
                    INSERT INTO generation_shot_plans
                      (task_id, styling_id, sort_order, shot_type, plan_json, created_at, updated_at)
                    VALUES (8, 11, ?, ?, ?, ?, ?)
                    """,
                    (
                        sort_order,
                        "reference" if sort_order == 6 else "legacy_shot",
                        db_module.dumps({
                            "sortOrder": sort_order,
                            "shotType": "reference" if sort_order == 6 else "legacy_shot",
                            "framing": "全身或近全身",
                            "pose": "自然站姿",
                            "cameraAngle": "正面",
                            "mainFocus": "主推款",
                            "visibleGarments": ["hero", "styling:13"],
                        }),
                        timestamp,
                        timestamp,
                    ),
                )

        with db_module.get_db() as db:
            db_module.migration_recover_legacy_shoot_scripts(db)
            db_module.migration_recover_legacy_shoot_scripts(db)

        with db_module.get_db() as db:
            task = db.execute("SELECT * FROM generation_tasks WHERE id = 8").fetchone()
            scripts = db.execute("SELECT * FROM generation_shoot_scripts WHERE task_id = 8").fetchall()
            preview = db.execute("SELECT * FROM generation_previews WHERE id = 13").fetchone()
            shot_plan_count = db.execute(
                "SELECT COUNT(*) AS count FROM generation_shot_plans WHERE task_id = 8 AND shoot_script_id = ?",
                (task["active_shoot_script_id"],),
            ).fetchone()["count"]

        self.assertEqual(task["status"], "PREVIEW_READY")
        self.assertEqual(task["active_storyboard_preview_id"], 13)
        self.assertIsNotNone(task["active_shoot_script_id"])
        self.assertEqual(len(scripts), 1)
        self.assertEqual(preview["shoot_script_id"], task["active_shoot_script_id"])
        self.assertEqual(shot_plan_count, 6)
        self.assertEqual(db_module.loads(scripts[0]["script_json"])["script_source_mode"], "legacy_recovered")

    def assert_no_model_raw_output(self, value: object) -> None:
        if isinstance(value, dict):
            self.assertNotIn("model_raw_output", value)
            for item in value.values():
                self.assert_no_model_raw_output(item)
        if isinstance(value, list):
            for item in value:
                self.assert_no_model_raw_output(item)

    def insert_prompt_config(self, db_module: object, key: str, group_type: str, content: str, sort_order: int) -> None:
        timestamp = "2026-06-03T04:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                """
                INSERT INTO prompt_configs
                  (key, group_type, title, description, content, sort_order, updated_at)
                VALUES (?, ?, ?, '', ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  group_type = excluded.group_type,
                  title = excluded.title,
                  content = excluded.content,
                  sort_order = excluded.sort_order,
                  updated_at = excluded.updated_at
                """,
                (key, group_type, key, content, sort_order, timestamp),
            )

    def test_prompt_config_slots_are_seeded_empty_without_default_prompt_text(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        db_module.init_db()

        rows = main.get_prompt_config_rows()
        self.assertEqual(
            set(rows),
            {
                "segment.brand_info",
                "assembly.ai_tagging",
                "assembly.ai_styling",
                "assembly.shoot_script",
                "assembly.storyboard_preview",
                "assembly.final_image",
            },
        )
        self.assertEqual(main.prompt_config_content("assembly.ai_styling"), "")
        self.assertEqual(main.prompt_config_content("segment.brand_info"), "")
        with self.assertRaisesRegex(RuntimeError, "Prompt 配置缺失或占位符未填充"):
            main.render_prompt_template("assembly.ai_styling", main.sample_prompt_runtime())

    def test_model_identity_prompt_runtime_migration_removes_placeholder_and_block(self) -> None:
        db_module = importlib.import_module("app.db")
        content = """【用户要求】
商品信息：{{runtime.hero_product_info}}
【强制模特身份契约】
{{runtime.model_identity_context}}
不得改变五官发型。
【AI 选搭结果】
{{runtime.styling_result}}
"""

        cleaned = db_module.remove_model_identity_prompt_runtime(content)

        self.assertNotIn("model_identity_context", cleaned)
        self.assertNotIn("强制模特身份契约", cleaned)
        self.assertNotIn("不得改变五官发型", cleaned)
        self.assertIn("runtime.hero_product_info", cleaned)
        self.assertIn("runtime.styling_result", cleaned)

    def test_subject_reference_prompt_runtime_migration_uses_explicit_fact_sources(self) -> None:
        db_module = importlib.import_module("app.db")
        db_module.init_db()
        self.insert_prompt_config(
            db_module,
            "assembly.storyboard_preview",
            "assembly",
            "环境只参考背景；模特与服装以 {{runtime.ref_subject}} 为准。",
            130,
        )
        with db_module.get_db() as db:
            db_module.migration_drop_subject_reference_prompt_runtime(db)
            row = db.execute(
                "SELECT content FROM prompt_configs WHERE key = 'assembly.storyboard_preview'"
            ).fetchone()

        self.assertNotIn("runtime.ref_subject", row["content"])
        self.assertIn("runtime.ref_model_identity", row["content"])
        self.assertIn("runtime.ref_garment", row["content"])

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

    def test_generation_debug_payloads_are_admin_only(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient

        db_module.init_db()
        timestamp = "2026-06-03T04:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                """
                INSERT INTO generation_tasks
                  (id, name, status, current_input_version_id, active_styling_id, active_shoot_script_id, created_at, updated_at)
                VALUES (1, 'Debug task', 'SCRIPT_READY', 2, 3, 5, ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_task_input_versions (id, task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at) VALUES (2, 1, 1, 'Debug task', '[]', '{}', ?)",
                (timestamp,),
            )
            db.execute(
                """
                INSERT INTO generation_stylings
                  (id, task_id, input_version_id, source_material_ids_json, selected_material_ids_json, selected_material_snapshot_json,
                   styling_plan_json, hero_understanding_json, outfit_candidates_json, rejected_candidates_json,
                   reason_summary, warnings_json, status, created_at, updated_at)
                VALUES (3, 1, 2, '[]', '[]', '[]', ?, ?, ?, ?, '已选搭', '[]', 'SUCCEEDED', ?, ?)
                """,
                (
                    db_module.dumps({
                        "schema_version": "styling-plan-v1",
                        "reason_summary": "已选搭",
                        "model_raw_output": {"secret": "styling raw"},
                    }),
                    db_module.dumps({"summary": "hero", "nested": {"model_raw_output": {"secret": "hero raw"}}}),
                    db_module.dumps([{"title": "candidate", "model_raw_output": {"secret": "candidate raw"}}]),
                    db_module.dumps([{"reason": "rejected", "nested": [{"model_raw_output": {"secret": "rejected raw"}}]}]),
                    timestamp,
                    timestamp,
                ),
            )
            db.execute(
                """
                INSERT INTO generation_shoot_scripts
                  (id, task_id, input_version_id, styling_id, status, script_json, summary_json,
                   styling_risks_json, accessory_adjustments_json, created_at, updated_at)
                VALUES (5, 1, 2, 3, 'SUCCEEDED', ?, ?, ?, ?, ?, ?)
                """,
                (
                    db_module.dumps({
                        "schema_version": "shoot-script-v1",
                        "user_visible_summary": {"title": "拍摄剧本"},
                        "model_raw_output": {"secret": "script raw"},
                    }),
                    db_module.dumps({"title": "拍摄剧本", "model_raw_output": {"secret": "summary raw"}}),
                    db_module.dumps([{"risk": "risk", "model_raw_output": {"secret": "risk raw"}}]),
                    db_module.dumps([{"item_type": "bag", "nested": {"model_raw_output": {"secret": "accessory raw"}}}]),
                    timestamp,
                    timestamp,
                ),
            )
            db.execute(
                """
                INSERT INTO generation_attempts
                  (id, owner_type, owner_id, provider, status, request_snapshot_json, response_snapshot_json, created_at, updated_at)
                VALUES (20, 'generation_styling', 1, 'gemini', 'SUCCEEDED', ?, ?, ?, ?)
                """,
                (
                    db_module.dumps({"prompt": "debug prompt"}),
                    db_module.dumps({"model_raw_output": {"secret": "attempt raw"}}),
                    timestamp,
                    timestamp,
                ),
            )

        client = TestClient(main.app)
        try:
            user_login = client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"})
            self.assertEqual(user_login.status_code, 200)

            public_detail = client.get("/api/generation-tasks/1")
            self.assertEqual(public_detail.status_code, 200)
            public_item = public_detail.json()["item"]
            self.assertNotIn("attempts", public_item)
            self.assert_no_model_raw_output(public_item["styling"])
            self.assert_no_model_raw_output(public_item["shootScript"])

            self.assertEqual(client.get("/api/generation-tasks/1?includeDebug=true").status_code, 403)
            self.assertEqual(client.get("/api/generation-records/1?includeDebug=true").status_code, 403)
            self.assertEqual(client.get("/api/generation-tasks/1/ai-invocations").status_code, 403)

            admin_login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-letmein-123456"})
            self.assertEqual(admin_login.status_code, 200)

            debug_detail = client.get("/api/generation-tasks/1?includeDebug=true")
            self.assertEqual(debug_detail.status_code, 200)
            debug_item = debug_detail.json()["item"]
            self.assertIn("attempts", debug_item)
            self.assertEqual(debug_item["styling"]["stylingPlan"]["model_raw_output"]["secret"], "styling raw")
            self.assertEqual(debug_item["styling"]["heroUnderstanding"]["nested"]["model_raw_output"]["secret"], "hero raw")
            self.assertEqual(debug_item["styling"]["outfitCandidates"][0]["model_raw_output"]["secret"], "candidate raw")
            self.assertEqual(debug_item["styling"]["rejectedCandidates"][0]["nested"][0]["model_raw_output"]["secret"], "rejected raw")
            self.assertEqual(debug_item["shootScript"]["script"]["model_raw_output"]["secret"], "script raw")
            self.assertEqual(debug_item["shootScript"]["summary"]["model_raw_output"]["secret"], "summary raw")
            self.assertEqual(debug_item["shootScript"]["stylingRisks"][0]["model_raw_output"]["secret"], "risk raw")
            self.assertEqual(debug_item["shootScript"]["accessoryAdjustments"][0]["nested"]["model_raw_output"]["secret"], "accessory raw")

            invocations = client.get("/api/generation-tasks/1/ai-invocations")
            self.assertEqual(invocations.status_code, 200)
            self.assertEqual(invocations.json()["items"][0]["modelOutput"]["secret"], "attempt raw")
        finally:
            client.close()

    def test_records_list_survives_old_task_schema_after_shoot_script_migration(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient

        timestamp = "2026-06-03T04:00:00Z"
        with db_module.get_db() as db:
            db.executescript(
                """
                CREATE TABLE schema_migrations (
                  version TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  applied_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations (version, name, applied_at) VALUES
                  ('027', 'drop_legacy_generation_tables', '2026-06-03T04:00:00Z'),
                  ('028', 'add_generation_task_auto_run', '2026-06-03T04:00:00Z'),
                  ('029', 'add_generation_user_requirements', '2026-06-03T04:00:00Z'),
                  ('030', 'split_generation_user_inputs', '2026-06-03T04:00:00Z'),
                  ('031', 'add_generation_pending_stage_inputs', '2026-06-03T04:00:00Z');

                CREATE TABLE generation_tasks (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'DRAFT',
                  current_input_version_id INTEGER,
                  active_styling_id INTEGER,
                  active_preview_id INTEGER,
                  active_result_set_id INTEGER,
                  archived_at TEXT,
                  archive_reason TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  deleted_at TEXT
                );
                CREATE TABLE generation_task_input_versions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
                  version_number INTEGER NOT NULL,
                  name_snapshot TEXT NOT NULL,
                  image_snapshot_json TEXT NOT NULL,
                  model_snapshot_json TEXT,
                  hero_visual_summary TEXT,
                  created_at TEXT NOT NULL,
                  UNIQUE(task_id, version_number)
                );
                CREATE TABLE generation_task_images (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
                  media_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
                  sort_order INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'READY',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  deleted_at TEXT
                );
                CREATE TABLE generation_stylings (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
                  input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
                  source_material_ids_json TEXT NOT NULL,
                  selected_material_ids_json TEXT NOT NULL,
                  selected_material_snapshot_json TEXT NOT NULL,
                  styling_plan_json TEXT,
                  reason_summary TEXT,
                  warnings_json TEXT,
                  status TEXT NOT NULL DEFAULT 'SUCCEEDED',
                  attempt_id INTEGER,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE generation_shot_plans (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
                  styling_id INTEGER NOT NULL REFERENCES generation_stylings(id) ON DELETE CASCADE,
                  sort_order INTEGER NOT NULL,
                  shot_type TEXT,
                  plan_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(styling_id, sort_order)
                );
                CREATE TABLE generation_previews (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
                  styling_id INTEGER NOT NULL REFERENCES generation_stylings(id),
                  input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
                  preview_asset_id INTEGER REFERENCES media_assets(id),
                  status TEXT NOT NULL DEFAULT 'QUEUED',
                  active_attempt_id INTEGER,
                  locked_by TEXT,
                  locked_until TEXT,
                  run_attempts INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE generation_result_sets (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
                  input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
                  styling_id INTEGER NOT NULL REFERENCES generation_stylings(id),
                  preview_id INTEGER NOT NULL REFERENCES generation_previews(id),
                  status TEXT NOT NULL DEFAULT 'RUNNING',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE generation_result_images_new (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  result_set_id INTEGER NOT NULL REFERENCES generation_result_sets(id) ON DELETE CASCADE,
                  shot_plan_id INTEGER NOT NULL REFERENCES generation_shot_plans(id),
                  sort_order INTEGER NOT NULL,
                  status TEXT NOT NULL DEFAULT 'QUEUED',
                  current_asset_id INTEGER REFERENCES media_assets(id),
                  active_attempt_id INTEGER,
                  locked_by TEXT,
                  locked_until TEXT,
                  run_attempts INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                """
            )

        db_module.init_db()
        with db_module.get_db() as db:
            self.assertTrue(db_module.table_has_column(db, "generation_tasks", "active_shoot_script_id"))
            self.assertTrue(db_module.table_has_column(db, "generation_tasks", "active_storyboard_preview_id"))
            self.assertTrue(db_module.table_has_column(db, "generation_previews", "shoot_script_id"))
            self.assertTrue(db_module.table_has_column(db, "generation_result_sets", "shoot_script_id"))
            self.assertTrue(db_module.table_has_column(db, "generation_shot_plans", "shoot_script_id"))
            unique_columns = db_module.table_unique_column_sets(db, "generation_shot_plans")
            self.assertIn(("shoot_script_id", "sort_order"), unique_columns)
            self.assertNotIn(("styling_id", "sort_order"), unique_columns)
            db.execute(
                """
                INSERT INTO media_assets
                  (id, object_key, original_filename, mime_type, created_at, updated_at)
                VALUES
                  (101, 'hero/front.jpg', 'front.jpg', 'image/jpeg', ?, ?),
                  (102, 'preview/grid.png', 'grid.png', 'image/png', ?, ?),
                  (103, 'final/1.png', '1.png', 'image/png', ?, ?)
                """,
                (timestamp, timestamp, timestamp, timestamp, timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_tasks (id, name, status, current_input_version_id, active_styling_id, active_preview_id, active_result_set_id, created_at, updated_at) VALUES (1, 'Old task', 'COMPLETED', 2, 3, 4, 10, ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_task_images (id, task_id, media_asset_id, sort_order, status, created_at, updated_at) VALUES (1, 1, 101, 1, 'READY', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_task_input_versions (id, task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, hero_visual_summary, created_at) VALUES (2, 1, 1, 'Old task', ?, '{}', '主推款', ?)",
                (db_module.dumps([{"asset": {"id": 101}}]), timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_stylings
                  (id, task_id, input_version_id, source_material_ids_json, selected_material_ids_json,
                   selected_material_snapshot_json, styling_plan_json, reason_summary, warnings_json, status, created_at, updated_at)
                VALUES (3, 1, 2, '[]', '[]', '[]', '{}', '已选搭', '[]', 'SUCCEEDED', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_shoot_scripts
                  (id, task_id, input_version_id, styling_id, status, script_json, created_at, updated_at)
                VALUES
                  (5, 1, 2, 3, 'SUCCEEDED', '{}', ?, ?),
                  (6, 1, 2, 3, 'SUCCEEDED', '{}', ?, ?)
                """,
                (timestamp, timestamp, timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shot_plans (id, task_id, styling_id, shoot_script_id, sort_order, shot_type, plan_json, created_at, updated_at) VALUES (6, 1, 3, 5, 1, 'hero', '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shot_plans (id, task_id, styling_id, shoot_script_id, sort_order, shot_type, plan_json, created_at, updated_at) VALUES (7, 1, 3, 6, 1, 'hero', '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_previews (id, task_id, styling_id, input_version_id, preview_asset_id, status, created_at, updated_at) VALUES (4, 1, 3, 2, 102, 'SUCCEEDED', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_result_sets (id, task_id, input_version_id, styling_id, preview_id, status, created_at, updated_at) VALUES (10, 1, 2, 3, 4, 'SUCCEEDED', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_result_images_new (id, result_set_id, shot_plan_id, sort_order, status, current_asset_id, created_at, updated_at) VALUES (30, 10, 6, 1, 'SUCCEEDED', 103, ?, ?)",
                (timestamp, timestamp),
            )

        client = TestClient(main.app)
        try:
            login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-letmein-123456"})
            self.assertEqual(login.status_code, 200)
            response = client.get("/api/generation-records?limit=5")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["items"][0]["id"], 1)
            self.assertEqual(len(payload["items"][0]["summary"]["finalImages"]), 1)
        finally:
            client.close()

    def test_poller_resumes_running_final_image_with_provider_request_before_lease_expires(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()
        timestamp = "2026-06-03T04:00:00Z"
        future_lock = "2099-01-01T00:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                "INSERT INTO generation_tasks (id, name, status, active_result_set_id, created_at, updated_at) VALUES (1, 'T', 'FINAL_RUNNING', 10, ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_task_input_versions (id, task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at) VALUES (2, 1, 1, 'T', '[]', '{}', ?)",
                (timestamp,),
            )
            db.execute(
                """
                INSERT INTO generation_stylings
                  (id, task_id, input_version_id, source_material_ids_json, selected_material_ids_json, selected_material_snapshot_json,
                   reason_summary, warnings_json, status, created_at, updated_at)
                VALUES (3, 1, 2, '[]', '[]', '[]', '', '[]', 'SUCCEEDED', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_previews (id, task_id, styling_id, input_version_id, status, created_at, updated_at) VALUES (4, 1, 3, 2, 'SUCCEEDED', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shoot_scripts (id, task_id, input_version_id, styling_id, status, script_json, created_at, updated_at) VALUES (5, 1, 2, 3, 'SUCCEEDED', '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shot_plans (id, task_id, styling_id, shoot_script_id, sort_order, plan_json, created_at, updated_at) VALUES (6, 1, 3, 5, 1, '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_result_sets (id, task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at) VALUES (10, 1, 2, 3, 5, 4, 'RUNNING', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_attempts
                  (id, owner_type, owner_id, provider, provider_request_id, status, started_at, request_snapshot_json, response_snapshot_json, created_at, updated_at)
                VALUES (20, 'generation_result_image', 30, 'image-2', 'provider-task-1', 'RUNNING', ?, '{}', '{}', ?, ?)
                """,
                (timestamp, timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_result_images_new
                  (id, result_set_id, shot_plan_id, sort_order, status, active_attempt_id, locked_by, locked_until, run_attempts, created_at, updated_at)
                VALUES (30, 10, 6, 1, 'RUNNING', 20, 'dead-worker', ?, 1, ?, ?)
                """,
                (future_lock, timestamp, timestamp),
            )

        scheduled: list[str | None] = []
        original_schedule = main.schedule_background_generation

        def capture_schedule(coro: object, job_key: str | None = None) -> None:
            scheduled.append(job_key)
            close = getattr(coro, "close", None)
            if callable(close):
                close()

        try:
            main.schedule_background_generation = capture_schedule
            main.refresh_pending_new_generation_jobs()
        finally:
            main.schedule_background_generation = original_schedule

        self.assertEqual(scheduled, ["generation_result_set:10"])

    def test_poller_does_not_resume_locked_final_image_without_provider_request(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()
        timestamp = "2026-06-03T04:00:00Z"
        future_lock = "2099-01-01T00:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                "INSERT INTO generation_tasks (id, name, status, active_result_set_id, created_at, updated_at) VALUES (1, 'T', 'FINAL_RUNNING', 10, ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_task_input_versions (id, task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at) VALUES (2, 1, 1, 'T', '[]', '{}', ?)",
                (timestamp,),
            )
            db.execute(
                """
                INSERT INTO generation_stylings
                  (id, task_id, input_version_id, source_material_ids_json, selected_material_ids_json, selected_material_snapshot_json,
                   reason_summary, warnings_json, status, created_at, updated_at)
                VALUES (3, 1, 2, '[]', '[]', '[]', '', '[]', 'SUCCEEDED', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_previews (id, task_id, styling_id, input_version_id, status, created_at, updated_at) VALUES (4, 1, 3, 2, 'SUCCEEDED', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shoot_scripts (id, task_id, input_version_id, styling_id, status, script_json, created_at, updated_at) VALUES (5, 1, 2, 3, 'SUCCEEDED', '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shot_plans (id, task_id, styling_id, shoot_script_id, sort_order, plan_json, created_at, updated_at) VALUES (6, 1, 3, 5, 1, '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_result_sets (id, task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at) VALUES (10, 1, 2, 3, 5, 4, 'RUNNING', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_result_images_new
                  (id, result_set_id, shot_plan_id, sort_order, status, active_attempt_id, locked_by, locked_until, run_attempts, created_at, updated_at)
                VALUES (30, 10, 6, 1, 'RUNNING', NULL, 'live-worker', ?, 1, ?, ?)
                """,
                (future_lock, timestamp, timestamp),
            )

        scheduled: list[str | None] = []
        original_schedule = main.schedule_background_generation

        def capture_schedule(coro: object, job_key: str | None = None) -> None:
            scheduled.append(job_key)
            close = getattr(coro, "close", None)
            if callable(close):
                close()

        try:
            main.schedule_background_generation = capture_schedule
            main.refresh_pending_new_generation_jobs()
        finally:
            main.schedule_background_generation = original_schedule

        self.assertEqual(scheduled, [])

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

    def test_auto_run_final_partial_only_requeues_failed_and_missing_images(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()
        timestamp = "2026-06-03T04:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                """
                INSERT INTO generation_tasks
                  (id, name, status, current_input_version_id, active_styling_id, active_shoot_script_id,
                   active_storyboard_preview_id, active_result_set_id, created_at, updated_at)
                VALUES (1, 'T', 'FINAL_PARTIAL', 2, 3, 5, 4, 10, ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_task_input_versions (id, task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at) VALUES (2, 1, 1, 'T', '[]', '{}', ?)",
                (timestamp,),
            )
            db.execute(
                """
                INSERT INTO generation_stylings
                  (id, task_id, input_version_id, source_material_ids_json, selected_material_ids_json, selected_material_snapshot_json,
                   reason_summary, warnings_json, status, created_at, updated_at)
                VALUES (3, 1, 2, '[]', '[]', '[]', '', '[]', 'SUCCEEDED', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shoot_scripts (id, task_id, input_version_id, styling_id, status, script_json, created_at, updated_at) VALUES (5, 1, 2, 3, 'SUCCEEDED', '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_previews (id, task_id, styling_id, shoot_script_id, input_version_id, status, created_at, updated_at) VALUES (4, 1, 3, 5, 2, 'SUCCEEDED', ?, ?)",
                (timestamp, timestamp),
            )
            for index in range(1, 6):
                db.execute(
                    "INSERT INTO generation_shot_plans (id, task_id, styling_id, shoot_script_id, sort_order, plan_json, created_at, updated_at) VALUES (?, 1, 3, 5, ?, '{}', ?, ?)",
                    (index + 5, index, timestamp, timestamp),
                )
            db.execute(
                "INSERT INTO generation_result_sets (id, task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at) VALUES (10, 1, 2, 3, 5, 4, 'PARTIAL', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_result_images_new (id, result_set_id, shot_plan_id, sort_order, status, created_at, updated_at) VALUES (30, 10, 6, 1, 'SUCCEEDED', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_result_images_new (id, result_set_id, shot_plan_id, sort_order, status, created_at, updated_at) VALUES (31, 10, 7, 2, 'FAILED', ?, ?)",
                (timestamp, timestamp),
            )

        image_ids = main.continue_active_final_result_set(1, 10, 2, 3, 5, 4)

        self.assertEqual(image_ids, [31, 32, 33, 34])
        with db_module.get_db() as db:
            rows = db.execute("SELECT sort_order, status, current_asset_id FROM generation_result_images_new WHERE result_set_id = 10 ORDER BY sort_order").fetchall()
            task = db.execute("SELECT status FROM generation_tasks WHERE id = 1").fetchone()
        self.assertEqual([(row["sort_order"], row["status"], row["current_asset_id"]) for row in rows], [
            (1, "SUCCEEDED", None),
            (2, "QUEUED", None),
            (3, "QUEUED", None),
            (4, "QUEUED", None),
            (5, "QUEUED", None),
        ])
        self.assertEqual(task["status"], "FINAL_RUNNING")

    def test_task_ai_operations_ignore_superseded_final_attempts(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()
        timestamp = "2026-06-03T04:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                """
                INSERT INTO generation_tasks
                  (id, name, status, active_result_set_id, created_at, updated_at)
                VALUES (1, 'T', 'COMPLETED', 20, ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_task_input_versions (id, task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at) VALUES (2, 1, 1, 'T', '[]', '{}', ?)",
                (timestamp,),
            )
            db.execute(
                """
                INSERT INTO generation_stylings
                  (id, task_id, input_version_id, source_material_ids_json, selected_material_ids_json, selected_material_snapshot_json,
                   reason_summary, warnings_json, status, created_at, updated_at)
                VALUES (3, 1, 2, '[]', '[]', '[]', '', '[]', 'SUCCEEDED', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shoot_scripts (id, task_id, input_version_id, styling_id, status, script_json, created_at, updated_at) VALUES (4, 1, 2, 3, 'SUCCEEDED', '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_previews (id, task_id, styling_id, shoot_script_id, input_version_id, status, created_at, updated_at) VALUES (5, 1, 3, 4, 2, 'SUCCEEDED', ?, ?)",
                (timestamp, timestamp),
            )
            for index in range(1, 6):
                db.execute(
                    "INSERT INTO generation_shot_plans (id, task_id, styling_id, shoot_script_id, sort_order, plan_json, created_at, updated_at) VALUES (?, 1, 3, 4, ?, '{}', ?, ?)",
                    (index, index, timestamp, timestamp),
                )
            db.execute(
                """
                INSERT INTO generation_result_sets
                  (id, task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at)
                VALUES (10, 1, 2, 3, 4, 5, 'SUPERSEDED', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_result_sets
                  (id, task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at)
                VALUES (20, 1, 2, 3, 4, 5, 'SUCCEEDED', ?, ?)
                """,
                (timestamp, timestamp),
            )
            for index in range(1, 6):
                db.execute(
                    """
                    INSERT INTO generation_result_images_new
                      (id, result_set_id, shot_plan_id, sort_order, status, active_attempt_id, created_at, updated_at)
                    VALUES (?, 10, ?, ?, 'RUNNING', ?, ?, ?)
                    """,
                    (index, index, index, index, timestamp, timestamp),
                )
                db.execute(
                    """
                    INSERT INTO generation_attempts
                      (id, owner_type, owner_id, provider, status, started_at, created_at, updated_at)
                    VALUES (?, 'generation_result_image', ?, 'image-2', 'RUNNING', ?, ?, ?)
                    """,
                    (index, index, timestamp, timestamp, timestamp),
                )
                current_id = index + 10
                db.execute(
                    """
                    INSERT INTO generation_result_images_new
                      (id, result_set_id, shot_plan_id, sort_order, status, active_attempt_id, created_at, updated_at)
                    VALUES (?, 20, ?, ?, 'SUCCEEDED', ?, ?, ?)
                    """,
                    (current_id, index, index, current_id, timestamp, timestamp),
                )
                db.execute(
                    """
                    INSERT INTO generation_attempts
                      (id, owner_type, owner_id, provider, status, started_at, completed_at, created_at, updated_at)
                    VALUES (?, 'generation_result_image', ?, 'image-2', 'SUCCEEDED', ?, ?, ?, ?)
                    """,
                    (current_id, current_id, timestamp, timestamp, timestamp, timestamp),
                )

        task = main.get_generation_task_by_id(1)

        self.assertEqual(task["status"], "COMPLETED")
        self.assertEqual(task["resultSet"]["status"], "SUCCEEDED")
        self.assertEqual({item["status"] for item in task["aiOperations"]}, {"SUCCEEDED"})
        self.assertEqual({item["ownerId"] for item in task["aiOperations"]}, {11, 12, 13, 14, 15})

    def test_superseding_result_set_cancels_running_images_and_attempts(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()
        timestamp = "2026-06-03T04:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                "INSERT INTO generation_tasks (id, name, status, active_result_set_id, created_at, updated_at) VALUES (1, 'T', 'FINAL_RUNNING', 20, ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_task_input_versions (id, task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at) VALUES (2, 1, 1, 'T', '[]', '{}', ?)",
                (timestamp,),
            )
            db.execute(
                """
                INSERT INTO generation_stylings
                  (id, task_id, input_version_id, source_material_ids_json, selected_material_ids_json, selected_material_snapshot_json,
                   reason_summary, warnings_json, status, created_at, updated_at)
                VALUES (3, 1, 2, '[]', '[]', '[]', '', '[]', 'SUCCEEDED', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shoot_scripts (id, task_id, input_version_id, styling_id, status, script_json, created_at, updated_at) VALUES (4, 1, 2, 3, 'SUCCEEDED', '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_previews (id, task_id, styling_id, shoot_script_id, input_version_id, status, created_at, updated_at) VALUES (5, 1, 3, 4, 2, 'SUCCEEDED', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shot_plans (id, task_id, styling_id, shoot_script_id, sort_order, plan_json, created_at, updated_at) VALUES (1, 1, 3, 4, 1, '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_result_sets
                  (id, task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at)
                VALUES (10, 1, 2, 3, 4, 5, 'RUNNING', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_result_sets
                  (id, task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at)
                VALUES (20, 1, 2, 3, 4, 5, 'RUNNING', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_result_images_new
                  (id, result_set_id, shot_plan_id, sort_order, status, active_attempt_id, created_at, updated_at)
                VALUES (1, 10, 1, 1, 'RUNNING', 1, ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_attempts
                  (id, owner_type, owner_id, provider, status, started_at, created_at, updated_at)
                VALUES (1, 'generation_result_image', 1, 'image-2', 'RUNNING', ?, ?, ?)
                """,
                (timestamp, timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_result_images_new
                  (id, result_set_id, shot_plan_id, sort_order, status, active_attempt_id, created_at, updated_at)
                VALUES (2, 20, 1, 1, 'RUNNING', 2, ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_attempts
                  (id, owner_type, owner_id, provider, status, started_at, created_at, updated_at)
                VALUES (2, 'generation_result_image', 2, 'image-2', 'RUNNING', ?, ?, ?)
                """,
                (timestamp, timestamp, timestamp),
            )

            main.supersede_generation_result_sets(db, 1, timestamp, keep_result_set_id=20)
            old_set = db.execute("SELECT status FROM generation_result_sets WHERE id = 10").fetchone()
            kept_set = db.execute("SELECT status FROM generation_result_sets WHERE id = 20").fetchone()
            old_image = db.execute("SELECT status FROM generation_result_images_new WHERE id = 1").fetchone()
            kept_image = db.execute("SELECT status FROM generation_result_images_new WHERE id = 2").fetchone()
            old_attempt = db.execute("SELECT status, cancelled_at FROM generation_attempts WHERE id = 1").fetchone()
            kept_attempt = db.execute("SELECT status FROM generation_attempts WHERE id = 2").fetchone()

        self.assertEqual(old_set["status"], "SUPERSEDED")
        self.assertEqual(kept_set["status"], "RUNNING")
        self.assertEqual(old_image["status"], "CANCELLED")
        self.assertEqual(kept_image["status"], "RUNNING")
        self.assertEqual(old_attempt["status"], "CANCELLED")
        self.assertEqual(old_attempt["cancelled_at"], timestamp)
        self.assertEqual(kept_attempt["status"], "RUNNING")

    def test_superseded_result_set_reconcile_does_not_restore_active_pointer(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()
        timestamp = "2026-06-03T04:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                """
                INSERT INTO generation_tasks
                  (id, name, status, current_input_version_id, active_styling_id, active_shoot_script_id,
                   active_storyboard_preview_id, active_result_set_id, created_at, updated_at)
                VALUES (1, 'T', 'FINAL_RUNNING', 2, 3, 4, 5, 20, ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_task_input_versions (id, task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at) VALUES (2, 1, 1, 'T', '[]', '{}', ?)",
                (timestamp,),
            )
            db.execute(
                """
                INSERT INTO generation_stylings
                  (id, task_id, input_version_id, source_material_ids_json, selected_material_ids_json, selected_material_snapshot_json,
                   reason_summary, warnings_json, status, created_at, updated_at)
                VALUES (3, 1, 2, '[]', '[]', '[]', '', '[]', 'SUCCEEDED', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shoot_scripts (id, task_id, input_version_id, styling_id, status, script_json, created_at, updated_at) VALUES (4, 1, 2, 3, 'SUCCEEDED', '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_previews (id, task_id, styling_id, shoot_script_id, input_version_id, status, created_at, updated_at) VALUES (5, 1, 3, 4, 2, 'SUCCEEDED', ?, ?)",
                (timestamp, timestamp),
            )
            for index in range(1, 6):
                db.execute(
                    "INSERT INTO generation_shot_plans (id, task_id, styling_id, shoot_script_id, sort_order, plan_json, created_at, updated_at) VALUES (?, 1, 3, 4, ?, '{}', ?, ?)",
                    (index, index, timestamp, timestamp),
                )
            db.execute(
                """
                INSERT INTO generation_result_sets
                  (id, task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at)
                VALUES (10, 1, 2, 3, 4, 5, 'SUPERSEDED', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_result_sets
                  (id, task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at)
                VALUES (20, 1, 2, 3, 4, 5, 'RUNNING', ?, ?)
                """,
                (timestamp, timestamp),
            )
            for index in range(1, 6):
                db.execute(
                    """
                    INSERT INTO generation_result_images_new
                      (id, result_set_id, shot_plan_id, sort_order, status, created_at, updated_at)
                    VALUES (?, 10, ?, ?, 'SUCCEEDED', ?, ?)
                    """,
                    (index, index, index, timestamp, timestamp),
                )
            result_status, task_status = main.reconcile_final_result_set(db, 10, 1)
            task = db.execute("SELECT status, active_result_set_id FROM generation_tasks WHERE id = 1").fetchone()
            old_set = db.execute("SELECT status FROM generation_result_sets WHERE id = 10").fetchone()
            new_set = db.execute("SELECT status FROM generation_result_sets WHERE id = 20").fetchone()

        self.assertEqual(result_status, "SUPERSEDED")
        self.assertEqual(task_status, "FINAL_RUNNING")
        self.assertEqual(task["active_result_set_id"], 20)
        self.assertEqual(task["status"], "FINAL_RUNNING")
        self.assertEqual(old_set["status"], "SUPERSEDED")
        self.assertEqual(new_set["status"], "RUNNING")

    def test_retry_final_image_rejects_when_task_is_running(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient

        db_module.init_db()
        timestamp = "2026-06-03T04:00:00Z"
        with db_module.get_db() as db:
            db.execute(
                "INSERT INTO generation_tasks (id, name, status, active_result_set_id, created_at, updated_at) VALUES (1, 'T', 'FINAL_RUNNING', 10, ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_task_input_versions (id, task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at) VALUES (2, 1, 1, 'T', '[]', '{}', ?)",
                (timestamp,),
            )
            db.execute(
                """
                INSERT INTO generation_stylings
                  (id, task_id, input_version_id, source_material_ids_json, selected_material_ids_json, selected_material_snapshot_json,
                   reason_summary, warnings_json, status, created_at, updated_at)
                VALUES (3, 1, 2, '[]', '[]', '[]', '', '[]', 'SUCCEEDED', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_previews (id, task_id, styling_id, input_version_id, status, created_at, updated_at) VALUES (4, 1, 3, 2, 'SUCCEEDED', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shoot_scripts (id, task_id, input_version_id, styling_id, status, script_json, created_at, updated_at) VALUES (5, 1, 2, 3, 'SUCCEEDED', '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shot_plans (id, task_id, styling_id, shoot_script_id, sort_order, plan_json, created_at, updated_at) VALUES (6, 1, 3, 5, 1, '{}', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_result_sets (id, task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at) VALUES (10, 1, 2, 3, 5, 4, 'RUNNING', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_result_images_new (id, result_set_id, shot_plan_id, sort_order, status, created_at, updated_at) VALUES (30, 10, 6, 1, 'FAILED', ?, ?)",
                (timestamp, timestamp),
            )

        client = TestClient(main.app)
        try:
            login = client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"})
            self.assertEqual(login.status_code, 200)
            response = client.post("/api/generation-result-images-new/30/retry")
        finally:
            client.close()

        self.assertEqual(response.status_code, 409)

    def test_image2_task_packages_pin_model_identity_references(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")
        db_module.init_db()
        timestamp = "2026-06-03T04:00:00Z"
        model_snapshot = {
            "modelId": "Face-01",
            "name": "Face-01 数字模特",
            "profile": {"notes": "短 bob，轻薄刘海", "forbidden": ["不改变发型长度", "不改变刘海形态"]},
            "imageInputs": [
                {"role": "model_identity", "objectKey": "model-profiles/Face-01/front.png", "filename": "front.png", "angle": "front"},
                {"role": "model_identity", "objectKey": "model-profiles/Face-01/left45.png", "filename": "left45.png", "angle": "left45"},
            ],
        }
        hero_snapshot = [
            {
                "sortOrder": 0,
                "asset": {
                    "id": 101,
                    "objectKey": "hero/front.jpg",
                    "filename": "front.jpg",
                    "originalUrl": "/api/media?key=hero/front.jpg&variant=original",
                },
            }
        ]
        selected_material_snapshot = [
            {
                "code": "LOOK-A",
                "images": [
                    {
                        "asset": {
                            "id": 102,
                            "objectKey": "styling/look-a.jpg",
                            "filename": "look-a.jpg",
                            "originalUrl": "/api/media?key=styling/look-a.jpg&variant=original",
                        }
                    }
                ],
            }
        ]
        shoot_script = {
            "schema_version": "shoot-script-v1",
            "global_directing": {
                "accessory_boundary": "生成配饰低存在感",
                "consistency_rules": ["同一模特同一造型"],
            },
            "shots": [{"shot_index": 1, "shot_role": "hero", "main_focus": "主推款"}],
        }
        self.insert_prompt_config(db_module, "segment.brand_info", "segment", "Lily 品牌信息", 10)
        self.insert_prompt_config(
            db_module,
            "assembly.storyboard_preview",
            "assembly",
            "生成分镜预览。\n\n{{segment.brand_info}}\n\n【输入参考图角色清单】\n"
            "{{#runtime.ref_model_identity}}{{runtime.ref_model_identity}} 为数字模特身份参考图。\n{{/runtime.ref_model_identity}}"
            "{{#runtime.ref_garment}}{{runtime.ref_garment}} 为主推款与搭配服装参考图。\n{{/runtime.ref_garment}}"
            "\n【拍摄剧本结果】\n【全局导演】\n{{runtime.shoot_script_global_directing}}\n【六格镜头】\n{{runtime.shoot_script_shots}}\n\n按拍摄剧本生成 3x2 分镜预览。",
            140,
        )
        self.insert_prompt_config(
            db_module,
            "assembly.final_image",
            "assembly",
            "生成一张电商棚拍成片。\n\n分镜预览不是模特或商品事实来源。模特参考图是模特身份事实来源。\n"
            "不得根据拍摄剧本、品牌妆发或分镜构图把模特改成另一张脸、低马尾、盘发、长发或参考图中不存在的发型。\n\n"
            "【输入参考图角色清单】\n"
            "{{#runtime.ref_model_identity}}{{runtime.ref_model_identity}} 为模特身份参考图。\n{{/runtime.ref_model_identity}}"
            "{{#runtime.ref_preview_grid}}{{runtime.ref_preview_grid}} 为分镜预览参考图，仅作构图参考。\n{{/runtime.ref_preview_grid}}"
            "\n{{segment.brand_info}}\n\n【当前单张成片上下文】\n"
            "{{#runtime.preview_grid_cell}}参考分镜预览第 {{runtime.preview_grid_cell}} 格。\n{{/runtime.preview_grid_cell}}"
            "{{#runtime.shot_current}}【当前镜头】\n{{runtime.shot_current}}\n{{/runtime.shot_current}}",
            150,
        )
        with db_module.get_db() as db:
            db.execute(
                "INSERT INTO generation_tasks (id, name, status, current_input_version_id, active_styling_id, active_shoot_script_id, active_storyboard_preview_id, active_result_set_id, created_at, updated_at) VALUES (1, 'T', 'PREVIEW_READY', 7, 3, 5, 4, 10, ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO media_assets (id, object_key, original_filename, mime_type, created_at, updated_at) VALUES (101, 'hero/front.jpg', 'front.jpg', 'image/jpeg', ?, ?), (102, 'styling/look-a.jpg', 'look-a.jpg', 'image/jpeg', ?, ?), (103, 'preview/grid.png', 'grid.png', 'image/png', ?, ?)",
                (timestamp, timestamp, timestamp, timestamp, timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_task_input_versions
                  (id, task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, hero_product_info_json, created_at)
                VALUES
                  (2, 1, 1, 'T', ?, ?, ?, ?),
                  (7, 1, 2, 'T', ?, ?, ?, ?)
                """,
                (
                    db_module.dumps(hero_snapshot),
                    db_module.dumps(model_snapshot),
                    db_module.dumps({"schema_version": "hero-product-info-v1", "info": "旧商品信息"}),
                    timestamp,
                    db_module.dumps(hero_snapshot),
                    db_module.dumps(model_snapshot),
                    db_module.dumps({"schema_version": "hero-product-info-v1", "info": "中短裙，裙摆刚过膝"}),
                    timestamp,
                ),
            )
            db.execute(
                """
                INSERT INTO generation_stylings
                  (id, task_id, input_version_id, source_material_ids_json, selected_material_ids_json,
                   selected_material_snapshot_json, styling_plan_json, reason_summary, warnings_json, status, created_at, updated_at)
                VALUES (3, 1, 2, '[]', '[]', ?, ?, '', '[]', 'SUCCEEDED', ?, ?)
                """,
                (
                    db_module.dumps(selected_material_snapshot),
                    db_module.dumps({"schema_version": "styling-plan-v1", "selected_material_codes": ["LOOK-A"]}),
                    timestamp,
                    timestamp,
                ),
            )
            db.execute(
                "INSERT INTO generation_shoot_scripts (id, task_id, input_version_id, styling_id, status, script_json, created_at, updated_at) VALUES (5, 1, 2, 3, 'SUCCEEDED', ?, ?, ?)",
                (db_module.dumps(shoot_script), timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_previews (id, task_id, styling_id, shoot_script_id, input_version_id, preview_asset_id, status, created_at, updated_at) VALUES (4, 1, 3, 5, 7, 103, 'SUCCEEDED', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shot_plans (id, task_id, styling_id, shoot_script_id, sort_order, plan_json, created_at, updated_at) VALUES (6, 1, 3, 5, 1, ?, ?, ?)",
                (db_module.dumps({"shot_index": 1, "shot_role": "hero", "main_focus": "主推款"}), timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_result_sets (id, task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at) VALUES (10, 1, 7, 3, 5, 4, 'RUNNING', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_result_images_new (id, result_set_id, shot_plan_id, sort_order, status, created_at, updated_at) VALUES (30, 10, 6, 1, 'QUEUED', ?, ?)",
                (timestamp, timestamp),
            )

        shoot_script_request = main.build_shoot_script_request_snapshot(
            1,
            3,
            7,
            main.build_stage_user_instruction_context("shoot_script", "突出裙摆长度"),
        )
        preview_package = main.build_new_preview_task_package(1, 3, 5, [{"sortOrder": 1, "plan": {"shot_index": 1}}])
        final_package = main.build_new_final_task_package(1, 30)

        self.assertEqual([item["role"] for item in preview_package["imageInputs"][:2]], ["model_identity", "model_identity"])
        self.assertEqual([item["role"] for item in final_package["imageInputs"][:3]], ["model_identity", "model_identity", "preview_grid"])
        self.assertEqual(shoot_script_request["input_version"]["id"], 7)
        self.assertEqual(shoot_script_request["hero_product_info"]["info"], "中短裙，裙摆刚过膝")
        self.assertEqual(shoot_script_request["shoot_script_user_instruction"]["instruction"], "突出裙摆长度")
        self.assertIn("输入参考图角色清单", preview_package["prompt"])
        # 参考图职能描述来自模板正文；会变的图号区间由代码填充（模特图占前两位 → 图1-2）。
        self.assertIn("数字模特身份参考图", preview_package["prompt"])
        self.assertIn("图1-2", preview_package["prompt"])
        # 旧的合成占位（image_reference_manifest / shoot_script_result / current_shot_context）已下线。
        self.assertNotIn("{{runtime.image_reference_manifest}}", preview_package["prompt"])
        self.assertNotIn("{{runtime.shoot_script_result}}", preview_package["prompt"])
        self.assertEqual(preview_package["inputVersionId"], 7)
        self.assertEqual(preview_package["heroProductInfo"]["info"], "中短裙，裙摆刚过膝")
        self.assertEqual(final_package["heroProductInfo"]["info"], "中短裙，裙摆刚过膝")
        self.assertIn("模特身份事实来源", final_package["prompt"])
        # 成片阶段参考图顺序为 模特×2 + 分镜预览 → 分镜预览参考图落在图3。
        self.assertIn("分镜预览参考图", final_package["prompt"])
        self.assertIn("图3", final_package["prompt"])
        self.assertNotIn("{{runtime.current_shot_context}}", final_package["prompt"])
        self.assertIn("低马尾", final_package["prompt"])
        self.assertIn("不得根据拍摄剧本", final_package["prompt"])

        normalized = main.normalize_shoot_script(
            {"global_directing": {"accessory_boundary": "生成配饰低存在感"}, "shots": []},
            {
                "task_id": 1,
                "styling_id": 3,
                "input_version": {"id": 2, "model_snapshot": model_snapshot},
                "styling_plan": {"schema_version": "styling-plan-v1", "hero_understanding": {}},
            },
        )
        self.assertIn("model_identity_contract", normalized)
        # 品牌调性/背景光线/模特导演/商品保真 字段已从拍摄剧本 global_directing 移除
        for removed in ("brand_tone", "background", "lighting", "model_direction", "hair_makeup_boundary", "product_fidelity_rules"):
            self.assertNotIn(removed, normalized["global_directing"])

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


if __name__ == "__main__":
    unittest.main()
