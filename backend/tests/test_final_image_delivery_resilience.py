"""成片生成链路健壮性测试：阶段进度、失败分类、回传分离与重新回传。

全部纯本地：临时数据库 + monkeypatch，不触发任何外部 provider 调用。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class TempEnvTestCase(unittest.TestCase):
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

    def _insert_attempt(self, owner_id: int = 1, **overrides: object) -> int:
        db_module = importlib.import_module("app.db")
        fields = {
            "owner_type": "generation_result_image",
            "owner_id": owner_id,
            "provider": "image-2",
            "status": "RUNNING",
            **overrides,
        }
        with db_module.get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO generation_attempts
                  (owner_type, owner_id, provider, provider_request_id, status, started_at,
                   request_snapshot_json, response_snapshot_json, error_code, created_at, updated_at)
                VALUES (:owner_type, :owner_id, :provider, :provider_request_id, :status, :started_at,
                        :request_snapshot_json, :response_snapshot_json, :error_code, :created_at, :updated_at)
                """,
                {
                    "provider_request_id": None,
                    "started_at": db_module.now_iso(),
                    "request_snapshot_json": "{}",
                    "response_snapshot_json": "{}",
                    "error_code": None,
                    "created_at": db_module.now_iso(),
                    "updated_at": db_module.now_iso(),
                    **fields,
                },
            )
            return int(cursor.lastrowid)


class MigrationAndProgressTests(TempEnvTestCase):
    def test_migration_adds_progress_column_and_upload_cache_table(self) -> None:
        db_module = importlib.import_module("app.db")
        with db_module.get_db() as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(generation_attempts)")]
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertIn("progress_json", columns)
        self.assertIn("provider_upload_cache", tables)

    def test_progress_recorder_persists_and_resumes_phases(self) -> None:
        db_module = importlib.import_module("app.db")
        progress_module = importlib.import_module("app.generation_progress")
        attempt_id = self._insert_attempt()

        recorder = progress_module.AttemptProgressRecorder(attempt_id)
        recorder.begin(progress_module.PHASE_UPLOAD, completed=0, total=4)
        recorder.update(progress_module.PHASE_UPLOAD, completed=3, total=4)
        recorder.begin(progress_module.PHASE_DELIVER, retries=0)
        recorder.update(progress_module.PHASE_DELIVER, retries=2, lastError="boom")
        recorder.finish()

        with db_module.get_db() as db:
            row = db.execute("SELECT progress_json FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
        progress = json.loads(row["progress_json"])
        phases = {entry["phase"]: entry for entry in progress["phases"]}
        self.assertIsNone(progress["current"])
        self.assertEqual(phases["upload"]["detail"], {"completed": 3, "total": 4})
        self.assertIsNotNone(phases["upload"]["endedAt"])
        self.assertEqual(phases["deliver"]["detail"]["retries"], 2)
        self.assertIsNotNone(phases["deliver"]["endedAt"])

        # 进程重启后 resume：新 recorder 读回既有阶段，不覆盖历史。
        resumed = progress_module.AttemptProgressRecorder(attempt_id)
        self.assertEqual(len(resumed.snapshot["phases"]), 2)

    def test_classify_attempt_error(self) -> None:
        progress_module = importlib.import_module("app.generation_progress")
        error = progress_module.GenerationPhaseError("x", progress_module.ERROR_DELIVERY_FAILED)
        self.assertEqual(progress_module.classify_attempt_error(error), "DELIVERY_FAILED")
        self.assertIsNone(progress_module.classify_attempt_error(RuntimeError("x")))


class UploadCacheTests(TempEnvTestCase):
    def test_upload_cache_dedupes_same_object_key(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        calls: list[int] = []

        def uploader() -> str:
            calls.append(1)
            return "https://files.toapis.com/u/first.jpg"

        first = image_provider.upload_object_with_cache("toapis", "garments/a.jpg", uploader)
        second = image_provider.upload_object_with_cache("toapis", "garments/a.jpg", uploader)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_upload_cache_dedupes_concurrent_uploads(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        calls: list[int] = []

        def uploader() -> str:
            calls.append(1)
            return "https://files.toapis.com/u/concurrent.jpg"

        threads = [
            threading.Thread(target=lambda: image_provider.upload_object_with_cache("toapis", "garments/b.jpg", uploader))
            for _ in range(5)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(calls), 1)

    def test_upload_cache_isolated_by_provider(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        image_provider.store_provider_upload_url("toapis", "garments/c.jpg", "https://files.toapis.com/u/c.jpg")
        self.assertIsNone(image_provider.get_cached_provider_upload_url("fal", "garments/c.jpg"))
        self.assertEqual(
            image_provider.get_cached_provider_upload_url("toapis", "garments/c.jpg"),
            "https://files.toapis.com/u/c.jpg",
        )


class CheckDeliverSplitTests(TempEnvTestCase):
    def test_toapis_check_never_downloads(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        provider = image_provider.GPTImage2ToapisProvider()
        completed_response = {"status": "completed", "result": {"data": [{"url": "https://files.toapis.com/out.png"}]}}
        with mock.patch.object(image_provider, "get_json", return_value=completed_response):
            with mock.patch.object(image_provider, "download_bytes", side_effect=AssertionError("check 不允许下载")):
                result = asyncio.run(provider.check({"provider_request_id": "tsk_1", "output_path": "k/out.png"}))
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["providerRequestId"], "tsk_1")

    def test_toapis_deliver_download_failure_raises_delivery_failed(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        progress_module = importlib.import_module("app.generation_progress")
        provider = image_provider.GPTImage2ToapisProvider()
        status_response = {"status": "completed", "result": {"data": [{"url": "https://files.toapis.com/out.png"}]}}
        with mock.patch.object(image_provider, "download_bytes", side_effect=RuntimeError("connection reset")):
            with self.assertRaises(progress_module.GenerationPhaseError) as ctx:
                asyncio.run(provider.deliver({"provider_request_id": "tsk_1", "output_path": "k/out.png"}, status_response))
        self.assertEqual(ctx.exception.error_code, progress_module.ERROR_DELIVERY_FAILED)

    def test_store_delivered_image_success(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        stored: dict[str, bytes] = {}
        with mock.patch.object(image_provider, "download_bytes", return_value=(b"png-bytes", "image/png")):
            with mock.patch.object(image_provider, "put_bytes", side_effect=lambda key, content, _ct: stored.update({key: content})):
                with mock.patch.object(image_provider, "generate_image_derivatives", return_value={"thumb": "k/t.png", "preview": "k/p.png", "thumbSize": 10, "previewSize": 20}):
                    result = image_provider.store_delivered_image("k/out.png", "https://files.toapis.com/out.png", {"status": "completed"})
        self.assertEqual(stored["k/out.png"], b"png-bytes")
        self.assertEqual(result["outputPath"], "k/out.png")
        self.assertEqual(result["fileSize"], len(b"png-bytes"))
        self.assertEqual(result["thumbPath"], "k/t.png")


try:  # app.main 依赖 fastapi/pydantic；不可用时跳过相关用例（沙箱环境）。
    import fastapi  # noqa: F401

    HAS_MAIN_DEPS = True
except ImportError:  # pragma: no cover
    HAS_MAIN_DEPS = False


@unittest.skipIf(not HAS_MAIN_DEPS, "app.main 依赖不可用")
class GenerateImageAssetPipelineTests(TempEnvTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.main = importlib.import_module("app.main")
        self.progress_module = importlib.import_module("app.generation_progress")

    def test_poll_tolerates_transient_status_check_errors(self) -> None:
        """状态查询抛错不再判死整张图：出错后退避继续轮询，直到拿到终态。"""
        attempt_id = self._insert_attempt()
        recorder = self.progress_module.AttemptProgressRecorder(attempt_id)
        responses: list[object] = [
            RuntimeError("HTTP 502 bad gateway"),
            RuntimeError("timed out"),
            {"status": "SUCCEEDED", "providerStatus": "completed", "providerStatusResponse": {"status": "completed"}},
        ]

        def fake_check(payload: dict, output_key: str) -> dict:
            step = responses.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        async def instant_sleep(_seconds: float) -> None:
            return None

        payload = {"providerRequestId": "tsk_1", "status": "SUBMITTED", "providerStatus": "submitted", "providerUrls": {}}
        with mock.patch.object(self.main, "run_image_provider_check", side_effect=fake_check):
            with mock.patch("asyncio.sleep", new=instant_sleep):
                result = asyncio.run(self.main.poll_provider_until_terminal(payload, "k/out.png", attempt_id, recorder))
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(responses, [])

    def test_deliver_with_retries_exhausts_to_delivery_failed(self) -> None:
        """回传预算耗尽后以 DELIVERY_FAILED 落败，providerRequestId 保留。"""
        os.environ["GENERATION_DELIVERY_TIMEOUT_SECONDS"] = "1"
        self._reload_settings()
        attempt_id = self._insert_attempt()
        recorder = self.progress_module.AttemptProgressRecorder(attempt_id)
        payload = {"providerRequestId": "tsk_keep", "status": "SUCCEEDED", "providerStatus": "completed", "providerUrls": {}}

        async def instant_sleep(_seconds: float) -> None:
            return None

        with mock.patch.object(self.main, "run_image_provider_deliver", side_effect=RuntimeError("download stalled")):
            with mock.patch("asyncio.sleep", new=instant_sleep):
                with self.assertRaises(self.main.ImageProviderError) as ctx:
                    asyncio.run(self.main.deliver_with_retries(payload, "k/out.png", attempt_id, recorder))
        self.assertEqual(ctx.exception.error_code, "DELIVERY_FAILED")
        self.assertEqual(ctx.exception.provider_response.get("providerRequestId"), "tsk_keep")

    def test_deliver_with_retries_recovers_after_transient_failure(self) -> None:
        attempt_id = self._insert_attempt()
        recorder = self.progress_module.AttemptProgressRecorder(attempt_id)
        payload = {"providerRequestId": "tsk_2", "status": "SUCCEEDED", "providerStatus": "completed", "providerUrls": {}}
        responses: list[object] = [RuntimeError("reset"), {"outputPath": "k/out.png", "fileSize": 9}]

        def fake_deliver(_payload: dict, _output_key: str) -> dict:
            step = responses.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        async def instant_sleep(_seconds: float) -> None:
            return None

        with mock.patch.object(self.main, "run_image_provider_deliver", side_effect=fake_deliver):
            with mock.patch("asyncio.sleep", new=instant_sleep):
                result = asyncio.run(self.main.deliver_with_retries(payload, "k/out.png", attempt_id, recorder))
        self.assertEqual(result["outputPath"], "k/out.png")


@unittest.skipIf(not HAS_MAIN_DEPS, "app.main 依赖不可用")
class RedeliverEndpointTests(TempEnvTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.main = importlib.import_module("app.main")
        self.db_module = importlib.import_module("app.db")

    def _seed_failed_delivery_image(self) -> tuple[int, int, int, int]:
        """直接铺底最小行集（关闭 FK 校验，避免整条 styling/preview 链）。"""
        timestamp = self.db_module.now_iso()
        conn = sqlite3.connect(os.environ["APP_DATABASE_PATH"])
        conn.row_factory = sqlite3.Row
        try:
            task_id = conn.execute(
                "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('t', 'FINAL_PARTIAL', ?, ?)",
                (timestamp, timestamp),
            ).lastrowid
            result_set_id = conn.execute(
                """
                INSERT INTO generation_result_sets (task_id, input_version_id, styling_id, preview_id, status, created_at, updated_at)
                VALUES (?, 1, 1, 1, 'PARTIAL', ?, ?)
                """,
                (task_id, timestamp, timestamp),
            ).lastrowid
            conn.execute("UPDATE generation_tasks SET active_result_set_id = ? WHERE id = ?", (result_set_id, task_id))
            failed_attempt_id = conn.execute(
                """
                INSERT INTO generation_attempts
                  (owner_type, owner_id, provider, provider_request_id, status, started_at, failed_at,
                   error_code, error_message, request_snapshot_json, response_snapshot_json, created_at, updated_at)
                VALUES ('generation_result_image', 0, 'image-2', 'tsk_prev', 'FAILED', ?, ?, 'DELIVERY_FAILED', '回传失败',
                        '{"prompt": "p"}', ?, ?, ?)
                """,
                (
                    timestamp,
                    timestamp,
                    json.dumps({"providerRequestId": "tsk_prev", "status": "SUCCEEDED", "providerStatusResponse": {"status": "completed"}}),
                    timestamp,
                    timestamp,
                ),
            ).lastrowid
            image_id = conn.execute(
                """
                INSERT INTO generation_result_images_new (result_set_id, shot_plan_id, sort_order, status, active_attempt_id, created_at, updated_at)
                VALUES (?, 1, 1, 'FAILED', ?, ?, ?)
                """,
                (result_set_id, failed_attempt_id, timestamp, timestamp),
            ).lastrowid
            conn.execute("UPDATE generation_attempts SET owner_id = ? WHERE id = ?", (image_id, failed_attempt_id))
            conn.commit()
        finally:
            conn.close()
        return int(task_id), int(result_set_id), int(image_id), int(failed_attempt_id)

    def test_queue_result_image_redelivery_copies_provider_request(self) -> None:
        task_id, result_set_id, image_id, failed_attempt_id = self._seed_failed_delivery_image()
        queued_task_id, queued_result_set_id, attempt_id = self.main.queue_result_image_redelivery(image_id)
        self.assertEqual((queued_task_id, queued_result_set_id), (task_id, result_set_id))
        self.assertNotEqual(attempt_id, failed_attempt_id)
        with self.db_module.get_db() as db:
            attempt = db.execute("SELECT * FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
            image = db.execute("SELECT * FROM generation_result_images_new WHERE id = ?", (image_id,)).fetchone()
            task = db.execute("SELECT * FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
        # 复制 provider 现场：零成本回传的关键。
        self.assertEqual(attempt["provider_request_id"], "tsk_prev")
        self.assertEqual(attempt["retry_of_attempt_id"], failed_attempt_id)
        snapshot = json.loads(attempt["response_snapshot_json"])
        self.assertEqual(snapshot["providerRequestId"], "tsk_prev")
        self.assertEqual(image["status"], "QUEUED")
        self.assertEqual(image["active_attempt_id"], attempt_id)
        self.assertEqual(task["status"], "FINAL_RUNNING")

    def test_redelivery_rejected_without_provider_request_id(self) -> None:
        task_id, result_set_id, image_id, failed_attempt_id = self._seed_failed_delivery_image()
        with self.db_module.get_db() as db:
            db.execute("UPDATE generation_attempts SET provider_request_id = NULL WHERE id = ?", (failed_attempt_id,))
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.main.queue_result_image_redelivery(image_id)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_redelivery_rejected_for_succeeded_image(self) -> None:
        _task_id, _result_set_id, image_id, _failed_attempt_id = self._seed_failed_delivery_image()
        with self.db_module.get_db() as db:
            db.execute("UPDATE generation_result_images_new SET status = 'SUCCEEDED' WHERE id = ?", (image_id,))
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.main.queue_result_image_redelivery(image_id)
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
