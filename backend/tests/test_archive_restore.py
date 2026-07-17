"""归档恢复测试：仅使用临时数据库，不调用外部 provider。"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class ArchiveRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = dict(os.environ)
        self._temp_dir = tempfile.TemporaryDirectory()
        data_dir = Path(self._temp_dir.name) / "data"
        os.environ.update({
            "APP_DATA_DIR": str(data_dir),
            "APP_DATABASE_PATH": str(data_dir / "app.sqlite3"),
            "APP_LOG_DIR": str(data_dir / "logs"),
            "APP_USER_PASSWORD": "letmein-123456",
            "APP_ADMIN_PASSWORD": "admin-letmein-123456",
            "APP_SESSION_SECRET": "session-secret-123",
            "APP_S3_ACCESS_KEY_ID": "ci-test-access-key",
            "APP_S3_SECRET_ACCESS_KEY": "ci-test-secret-key",
        })
        config = importlib.reload(importlib.import_module("app.config"))
        config.get_settings.cache_clear()
        db_module = importlib.reload(importlib.import_module("app.db"))
        db_module.init_db()
        main = importlib.import_module("app.main")
        main.settings = config.get_settings()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        config = sys.modules.get("app.config")
        if config is not None:
            config.get_settings.cache_clear()
        self._temp_dir.cleanup()

    def _seed_archived_objects(self) -> None:
        db_module = importlib.import_module("app.db")
        timestamp = db_module.now_iso()
        with db_module.get_db() as db:
            for object_id in (1, 2):
                db.execute(
                    "INSERT INTO styling_garments (id, code, status, archived_at, archive_reason, created_at, updated_at) VALUES (?, ?, 'READY', ?, 'user_archived', ?, ?)",
                    (object_id, f"LOOK-{object_id}", timestamp, timestamp, timestamp),
                )
                db.execute(
                    "INSERT INTO generation_tasks (id, name, status, archived_at, archive_reason, created_at, updated_at) VALUES (?, ?, 'COMPLETED', ?, 'user_archived', ?, ?)",
                    (object_id, f"TASK-{object_id}", timestamp, timestamp, timestamp),
                )

    def test_single_restore_clears_archive_fields_and_logs_event(self) -> None:
        self._seed_archived_objects()
        main = importlib.import_module("app.main")
        db_module = importlib.import_module("app.db")
        client = TestClient(main.app)
        self.assertEqual(client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"}).status_code, 200)

        garment = client.post("/api/styling-garments/1/restore")
        task = client.post("/api/generation-tasks/1/restore")

        self.assertEqual(garment.status_code, 200)
        self.assertEqual(task.status_code, 200)
        self.assertIsNone(garment.json()["item"]["archivedAt"])
        self.assertIsNone(task.json()["item"]["archivedAt"])
        with db_module.get_db() as db:
            rows = db.execute("SELECT entity_type, event_type FROM event_logs WHERE entity_id = 1 ORDER BY id").fetchall()
        self.assertEqual([(row["entity_type"], row["event_type"]) for row in rows], [
            ("styling_garment", "restored"),
            ("generation_task", "restored"),
        ])

    def test_batch_restore_is_idempotent_and_only_logs_archived_rows(self) -> None:
        self._seed_archived_objects()
        main = importlib.import_module("app.main")
        db_module = importlib.import_module("app.db")
        client = TestClient(main.app)
        self.assertEqual(client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"}).status_code, 200)

        for _ in range(2):
            garment = client.post("/api/styling-garments/batch-restore", json={"ids": [1, 2]})
            task = client.post("/api/generation-tasks/batch-restore", json={"ids": [1, 2]})
            self.assertEqual(garment.status_code, 200)
            self.assertEqual(task.status_code, 200)

        with db_module.get_db() as db:
            garments = db.execute("SELECT archived_at, archive_reason FROM styling_garments").fetchall()
            tasks = db.execute("SELECT archived_at, archive_reason FROM generation_tasks").fetchall()
            restored_count = db.execute("SELECT COUNT(*) AS count FROM event_logs WHERE event_type = 'restored'").fetchone()["count"]
        self.assertTrue(all(row["archived_at"] is None and row["archive_reason"] is None for row in garments + tasks))
        self.assertEqual(restored_count, 4)

    def test_restore_routes_reject_missing_objects_and_empty_batches(self) -> None:
        main = importlib.import_module("app.main")
        client = TestClient(main.app)
        self.assertEqual(client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"}).status_code, 200)

        self.assertEqual(client.post("/api/styling-garments/999/restore").status_code, 404)
        self.assertEqual(client.post("/api/generation-tasks/999/restore").status_code, 404)
        self.assertEqual(client.post("/api/styling-garments/batch-restore", json={"ids": []}).status_code, 400)
        self.assertEqual(client.post("/api/generation-tasks/batch-restore", json={"ids": []}).status_code, 400)

    def test_purge_generation_task_detaches_upload_session_media_reference(self) -> None:
        main = importlib.import_module("app.main")
        db_module = importlib.import_module("app.db")
        timestamp = db_module.now_iso()
        with db_module.get_db() as db:
            db.execute(
                """
                INSERT INTO media_assets
                  (id, object_key, original_filename, mime_type, status, created_at, updated_at)
                VALUES (1, 'direct-uploads/generation-task/session/001.png', '001.png', 'image/png', 'READY', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO upload_sessions
                  (id, scope, status, total_count, uploaded_count, target_type, target_id, created_at, updated_at)
                VALUES ('session', 'generation-task', 'FINALIZED', 1, 1, 'generation_task', 1, ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO upload_session_items
                  (session_id, sort_order, object_key, filename, mime_type, status, media_asset_id, created_at, updated_at)
                VALUES ('session', 0, 'direct-uploads/generation-task/session/001.png', '001.png', 'image/png', 'READY', 1, ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_tasks (id, name, status, created_at, updated_at)
                VALUES (1, 'TASK-1', 'DRAFT', ?, ?)
                """,
                (timestamp, timestamp),
            )
            db.execute(
                """
                INSERT INTO generation_task_images
                  (task_id, media_asset_id, sort_order, status, created_at, updated_at)
                VALUES (1, 1, 0, 'READY', ?, ?)
                """,
                (timestamp, timestamp),
            )

        client = TestClient(main.app)
        self.assertEqual(client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"}).status_code, 200)

        response = client.request("DELETE", "/api/generation-tasks/1", json={"purge": True, "confirmName": "TASK-1"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["purged"])
        with db_module.get_db() as db:
            task_count = db.execute("SELECT COUNT(*) AS count FROM generation_tasks").fetchone()["count"]
            media_count = db.execute("SELECT COUNT(*) AS count FROM media_assets").fetchone()["count"]
            upload_item = db.execute("SELECT media_asset_id FROM upload_session_items WHERE session_id = 'session'").fetchone()
        self.assertEqual(task_count, 0)
        self.assertEqual(media_count, 0)
        self.assertIsNone(upload_item["media_asset_id"])


if __name__ == "__main__":
    unittest.main()
