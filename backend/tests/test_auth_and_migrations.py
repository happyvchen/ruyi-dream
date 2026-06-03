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
                """,
                (key, group_type, key, content, sort_order, timestamp),
            )

    def test_prompt_configs_are_not_seeded_or_defaulted_by_backend(self) -> None:
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        db_module.init_db()

        self.assertEqual(main.get_prompt_config_rows(), {})
        self.assertIsNone(main.prompt_config_content("assembly.ai_styling"))
        with self.assertRaisesRegex(RuntimeError, "Prompt 配置缺失或占位符未填充"):
            main.render_prompt_template("assembly.ai_styling", main.sample_prompt_runtime())

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
                "hair_makeup_boundary": "错误示例：改成低马尾",
                "background": "浅灰棚拍",
            },
            "shots": [{"shot_index": 1, "shot_role": "hero", "main_focus": "主推款"}],
        }
        self.insert_prompt_config(db_module, "segment.brand_info", "segment", "Lily 品牌信息", 10)
        self.insert_prompt_config(
            db_module,
            "assembly.storyboard_preview",
            "assembly",
            "生成分镜预览。\n\n{{segment.brand_info}}\n\n【输入参考图角色清单】\n{{runtime.image_reference_manifest}}\n\n【拍摄剧本结果】\n{{runtime.shoot_script_result}}\n\n按拍摄剧本生成 3x2 分镜预览。",
            140,
        )
        self.insert_prompt_config(
            db_module,
            "assembly.final_image",
            "assembly",
            "生成一张电商棚拍成片。\n\n分镜预览不是模特或商品事实来源。模特参考图是模特身份事实来源。\n不得根据拍摄剧本、品牌妆发或分镜构图把模特改成另一张脸、低马尾、盘发、长发或参考图中不存在的发型。\n\n【输入参考图角色清单】\n{{runtime.image_reference_manifest}}\n\n{{segment.brand_info}}\n\n【当前单张成片上下文】\n{{runtime.current_shot_context}}",
            150,
        )
        with db_module.get_db() as db:
            db.execute(
                "INSERT INTO generation_tasks (id, name, status, current_input_version_id, active_styling_id, active_shoot_script_id, active_storyboard_preview_id, active_result_set_id, created_at, updated_at) VALUES (1, 'T', 'PREVIEW_READY', 2, 3, 5, 4, 10, ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO media_assets (id, object_key, original_filename, mime_type, created_at, updated_at) VALUES (101, 'hero/front.jpg', 'front.jpg', 'image/jpeg', ?, ?), (102, 'styling/look-a.jpg', 'look-a.jpg', 'image/jpeg', ?, ?), (103, 'preview/grid.png', 'grid.png', 'image/png', ?, ?)",
                (timestamp, timestamp, timestamp, timestamp, timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_task_input_versions (id, task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at) VALUES (2, 1, 1, 'T', ?, ?, ?)",
                (db_module.dumps(hero_snapshot), db_module.dumps(model_snapshot), timestamp),
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
                "INSERT INTO generation_previews (id, task_id, styling_id, shoot_script_id, input_version_id, preview_asset_id, status, created_at, updated_at) VALUES (4, 1, 3, 5, 2, 103, 'SUCCEEDED', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_shot_plans (id, task_id, styling_id, shoot_script_id, sort_order, plan_json, created_at, updated_at) VALUES (6, 1, 3, 5, 1, ?, ?, ?)",
                (db_module.dumps({"shot_index": 1, "shot_role": "hero", "main_focus": "主推款"}), timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_result_sets (id, task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at) VALUES (10, 1, 2, 3, 5, 4, 'RUNNING', ?, ?)",
                (timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO generation_result_images_new (id, result_set_id, shot_plan_id, sort_order, status, created_at, updated_at) VALUES (30, 10, 6, 1, 'QUEUED', ?, ?)",
                (timestamp, timestamp),
            )

        preview_package = main.build_new_preview_task_package(1, 3, 5, [{"sortOrder": 1, "plan": {"shot_index": 1}}])
        final_package = main.build_new_final_task_package(1, 30)

        self.assertEqual([item["role"] for item in preview_package["imageInputs"][:2]], ["model_identity", "model_identity"])
        self.assertEqual([item["role"] for item in final_package["imageInputs"][:3]], ["model_identity", "model_identity", "preview_grid"])
        self.assertIn("输入参考图角色清单", preview_package["prompt"])
        self.assertIn("model_identity", preview_package["prompt"])
        self.assertIn("模特身份事实来源", final_package["prompt"])
        self.assertIn("preview_grid is composition-only", final_package["prompt"])
        self.assertIn("低马尾", final_package["prompt"])
        self.assertIn("不得根据拍摄剧本", final_package["prompt"])

        normalized = main.normalize_shoot_script(
            {"global_directing": {"hair_makeup_boundary": "错误示例：改成低马尾"}, "shots": []},
            {
                "task_id": 1,
                "styling_id": 3,
                "input_version": {"id": 2, "model_snapshot": model_snapshot},
                "styling_plan": {"schema_version": "styling-plan-v1", "hero_understanding": {}},
            },
        )
        self.assertIn("model_identity_contract", normalized)
        self.assertIn("不得根据品牌妆发想象改成低马尾", normalized["global_directing"]["hair_makeup_boundary"])

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
