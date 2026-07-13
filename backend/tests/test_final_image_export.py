"""成片批量下载测试：导出预检与 zip 打包接口。

全部纯本地：临时数据库 + monkeypatch 对象存储读取，不触发任何外部 provider 调用。
"""
from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class FinalImageExportTestCase(unittest.TestCase):
    """与 test_auth_and_migrations 相同的隔离环境：临时目录 + 临时数据库。"""

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
                "TOAPIS_API_KEY": "test-toapis-key",
            }
        )
        self._reload_settings()
        db_module = importlib.import_module("app.db")
        db_module.init_db()

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
        for module_name in ("app.db", "app.image_provider", "app.generation_progress"):
            module = sys.modules.get(module_name)
            if module is not None and hasattr(module, "get_settings"):
                module.get_settings = config.get_settings
        main = sys.modules.get("app.main")
        if main is not None:
            main.settings = config.get_settings()

    def _seed_task_with_results(
        self,
        task_id: int,
        name: str,
        image_specs: list[tuple[int, str, str | None]],
    ) -> None:
        """image_specs: (sort_order, status, object_key 或 None)。"""
        db_module = importlib.import_module("app.db")
        now = db_module.now_iso()
        with db_module.get_db() as db:
            db.execute(
                "INSERT INTO generation_tasks (id, name, status, created_at, updated_at) VALUES (?, ?, 'COMPLETED', ?, ?)",
                (task_id, name, now, now),
            )
            db.execute(
                "INSERT INTO generation_task_input_versions (id, task_id, version_number, name_snapshot, image_snapshot_json, created_at) VALUES (?, ?, 1, ?, '[]', ?)",
                (task_id, task_id, name, now),
            )
            db.execute(
                "INSERT INTO generation_stylings (id, task_id, input_version_id, source_material_ids_json, selected_material_ids_json, selected_material_snapshot_json, created_at, updated_at) VALUES (?, ?, ?, '[]', '[]', '[]', ?, ?)",
                (task_id, task_id, task_id, now, now),
            )
            db.execute(
                "INSERT INTO generation_previews (id, task_id, styling_id, input_version_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'SUCCEEDED', ?, ?)",
                (task_id, task_id, task_id, task_id, now, now),
            )
            db.execute(
                "INSERT INTO generation_result_sets (id, task_id, input_version_id, styling_id, preview_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'SUCCEEDED', ?, ?)",
                (task_id, task_id, task_id, task_id, task_id, now, now),
            )
            db.execute(
                "UPDATE generation_tasks SET active_result_set_id = ? WHERE id = ?",
                (task_id, task_id),
            )
            labels = {1: "首图", 2: "全身", 3: "半身", 4: "背面", 5: "局部"}
            for sort_order, status, object_key in image_specs:
                asset_id = None
                if object_key:
                    cursor = db.execute(
                        "INSERT INTO media_assets (object_key, original_filename, mime_type, size_bytes, created_at, updated_at) VALUES (?, ?, 'image/png', 1000, ?, ?)",
                        (object_key, Path(object_key).name, now, now),
                    )
                    asset_id = cursor.lastrowid
                db.execute(
                    "INSERT INTO generation_result_images_new (result_set_id, shot_role, sort_order, status, current_asset_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (task_id, labels.get(sort_order, "镜头"), sort_order, status, asset_id, now, now),
                )

    def _client(self):
        main = importlib.import_module("app.main")
        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        login = client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"})
        self.assertEqual(login.status_code, 200)
        return client

    def test_precheck_reports_downloadable_and_issues(self) -> None:
        self._seed_task_with_results(
            1,
            "秋季风衣主推",
            [(1, "SUCCEEDED", "results/1-1.png"), (2, "SUCCEEDED", "results/1-2.png")],
        )
        self._seed_task_with_results(
            2,
            "连衣裙B款",
            [(1, "SUCCEEDED", "results/2-1.png"), (2, "FAILED", None), (3, "RUNNING", None)],
        )
        client = self._client()
        try:
            response = client.post("/api/generation-task-exports/precheck", json={"ids": [1, 2]})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["totalImages"], 3)
            self.assertEqual(payload["downloadableTaskIds"], [1, 2])
            self.assertEqual(payload["completeTaskIds"], [1])
            first, second = payload["items"]
            self.assertEqual(first["downloadableCount"], 2)
            self.assertEqual(first["issues"], [])
            self.assertEqual(second["downloadableCount"], 1)
            statuses = {issue["status"] for issue in second["issues"]}
            self.assertEqual(statuses, {"FAILED", "RUNNING"})
            labels = {issue["shotLabel"] for issue in second["issues"]}
            self.assertEqual(labels, {"全身", "半身"})
        finally:
            client.close()

    def test_archive_builds_zip_grouped_by_task_folder(self) -> None:
        self._seed_task_with_results(
            1,
            "秋季风衣主推",
            [(1, "SUCCEEDED", "results/1-1.png"), (2, "SUCCEEDED", "results/1-2.png")],
        )
        self._seed_task_with_results(
            2,
            "连衣裙B款",
            [(1, "SUCCEEDED", "results/2-1.png"), (2, "FAILED", None)],
        )
        main = importlib.import_module("app.main")
        original_get_bytes = main.get_bytes
        main.get_bytes = lambda key: f"binary:{key}".encode("utf-8")
        client = self._client()
        try:
            response = client.get("/api/generation-task-exports/archive", params={"ids": "1,2"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/zip")
            self.assertIn("attachment", response.headers["content-disposition"])
            archive = zipfile.ZipFile(io.BytesIO(response.content))
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            self.assertIn("秋季风衣主推/z(1).png", names)
            self.assertIn("秋季风衣主推/z(2).png", names)
            self.assertIn("连衣裙B款/z(1).png", names)
            self.assertIn("清单.txt", names)
            self.assertEqual(archive.read("秋季风衣主推/z(1).png"), b"binary:results/1-1.png")
            manifest = archive.read("清单.txt").decode("utf-8")
            self.assertIn("连衣裙B款", manifest)
            self.assertIn("生成失败", manifest)
        finally:
            main.get_bytes = original_get_bytes
            client.close()

    def test_archive_keeps_duplicate_task_names_and_illegal_chars(self) -> None:
        self._seed_task_with_results(1, "2026-05-27 18:10:22", [(1, "SUCCEEDED", "results/1-1.png")])
        self._seed_task_with_results(2, "2026-05-27 18:10:22", [(1, "SUCCEEDED", "results/2-1.png")])
        main = importlib.import_module("app.main")
        original_get_bytes = main.get_bytes
        main.get_bytes = lambda key: f"binary:{key}".encode("utf-8")
        client = self._client()
        try:
            response = client.get("/api/generation-task-exports/archive", params={"ids": "1,2"})
            self.assertEqual(response.status_code, 200)
            archive = zipfile.ZipFile(io.BytesIO(response.content))
            names = archive.namelist()
            # 任务名保持原貌，仅替换文件系统非法字符（冒号）；同名任务追加 (2) 序号，两个都保留
            self.assertIn("2026-05-27 18_10_22/z(1).png", names)
            self.assertIn("2026-05-27 18_10_22(2)/z(1).png", names)
            self.assertEqual(archive.read("2026-05-27 18_10_22/z(1).png"), b"binary:results/1-1.png")
            self.assertEqual(archive.read("2026-05-27 18_10_22(2)/z(1).png"), b"binary:results/2-1.png")
            manifest = archive.read("清单.txt").decode("utf-8")
            self.assertIn("任务 ID 1", manifest)
            self.assertIn("任务 ID 2", manifest)
        finally:
            main.get_bytes = original_get_bytes
            client.close()

    def test_archive_rejects_when_nothing_downloadable(self) -> None:
        self._seed_task_with_results(3, "尚未成片任务", [(1, "FAILED", None)])
        client = self._client()
        try:
            response = client.get("/api/generation-task-exports/archive", params={"ids": "3"})
            self.assertEqual(response.status_code, 400)
            missing = client.get("/api/generation-task-exports/archive", params={"ids": "999"})
            self.assertEqual(missing.status_code, 404)
            bad = client.get("/api/generation-task-exports/archive", params={"ids": "abc"})
            self.assertEqual(bad.status_code, 400)
        finally:
            client.close()

    def test_media_download_forces_original_and_attachment_response(self) -> None:
        self._seed_task_with_results(4, "下载测试", [(1, "SUCCEEDED", "results/原图.png")])
        main = importlib.import_module("app.main")
        original_exists = main.object_exists
        original_presign = main.presigned_get_url
        calls: list[tuple[str, dict[str, str | None]]] = []
        main.object_exists = lambda key: True

        def fake_presign(key, **kwargs):
            calls.append((str(key), kwargs))
            return "https://storage.example/download"

        main.presigned_get_url = fake_presign
        client = self._client()
        try:
            response = client.get(
                "/api/media",
                params={"key": "results/原图.png", "variant": "thumb", "download": "1"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["location"], "https://storage.example/download")
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self.assertEqual(calls[0][0], "results/原图.png")
            self.assertEqual(calls[0][1]["response_content_type"], "image/png")
            disposition = calls[0][1]["response_content_disposition"]
            self.assertIn("attachment", disposition)
            self.assertIn("filename*=UTF-8''", disposition)
        finally:
            main.object_exists = original_exists
            main.presigned_get_url = original_presign
            client.close()


if __name__ == "__main__":
    unittest.main()
