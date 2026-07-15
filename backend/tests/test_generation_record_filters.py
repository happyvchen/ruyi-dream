"""生成记录查询测试：只使用临时数据库，不调用外部 provider。"""
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


class GenerationRecordFilterTests(unittest.TestCase):
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
        self._seed_records()
        self.client = TestClient(main.app)
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"}).status_code, 200)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        config = sys.modules.get("app.config")
        if config is not None:
            config.get_settings.cache_clear()
        self._temp_dir.cleanup()

    def _seed_records(self) -> None:
        db_module = importlib.import_module("app.db")
        rows = [
            (1, "夏季连衣裙", "COMPLETED", "batch-a", None, "2026-07-01T09:00:00", "2026-07-03T09:00:00"),
            (2, "通勤衬衫", "FAILED", "batch-a", None, "2026-07-10T09:00:00", "2026-07-11T09:00:00"),
            (3, "西装外套", "FINAL_RUNNING", "batch-b", None, "2026-07-12T09:00:00", "2026-07-12T10:00:00"),
            (4, "归档旧任务", "COMPLETED", "batch-old", "2026-07-13T09:00:00", "2026-06-20T09:00:00", "2026-06-21T09:00:00"),
        ]
        with db_module.get_db() as db:
            for row in rows:
                db.execute(
                    """
                    INSERT INTO generation_tasks (
                      id, name, status, batch_id, archived_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )

    def test_filters_keyword_status_created_range_and_sort(self) -> None:
        response = self.client.get(
            "/api/generation-records",
            params={
                "keyword": "batch-a",
                "status": "FAILED,COMPLETED",
                "createdFrom": "2026-07-01",
                "createdTo": "2026-07-10",
                "sortBy": "createdAt",
                "sortDir": "asc",
                "limit": "20",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual([item["name"] for item in body["items"]], ["夏季连衣裙", "通勤衬衫"])
        self.assertEqual(body["total"], 2)
        self.assertEqual(
            {item["value"]: item["count"] for item in body["facets"]["status"]},
            {"COMPLETED": 1, "FAILED": 1},
        )
        self.assertEqual(
            {item["value"]: item["count"] for item in body["stableFacets"]["status"]},
            {"COMPLETED": 1, "FAILED": 1, "FINAL_RUNNING": 1},
        )

    def test_archive_visibility_and_name_sort(self) -> None:
        default_response = self.client.get("/api/generation-records?limit=20")
        self.assertEqual(default_response.status_code, 200)
        self.assertNotIn("归档旧任务", [item["name"] for item in default_response.json()["items"]])

        archived_response = self.client.get("/api/generation-records?includeArchived=true&sortBy=name&sortDir=asc&limit=20")
        self.assertEqual(archived_response.status_code, 200)
        self.assertIn("归档旧任务", [item["name"] for item in archived_response.json()["items"]])


if __name__ == "__main__":
    unittest.main()
