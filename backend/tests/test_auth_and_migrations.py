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


if __name__ == "__main__":
    unittest.main()
