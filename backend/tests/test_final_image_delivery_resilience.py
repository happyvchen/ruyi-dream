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
import time
import unittest
from pathlib import Path
from contextlib import contextmanager
from unittest import mock

try:  # app.main 依赖 fastapi/pydantic；不可用时跳过相关用例（沙箱环境）。
    import fastapi  # noqa: F401

    HAS_MAIN_DEPS = True
except ImportError:  # pragma: no cover
    HAS_MAIN_DEPS = False


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
        self.assertEqual(
            progress_module.classify_generation_attempt_error(RuntimeError("local bug")),
            progress_module.ERROR_SYSTEM_ERROR,
        )

    def test_normalize_toapis_nested_invalid_request(self) -> None:
        progress_module = importlib.import_module("app.generation_progress")
        payload = {
            "error": {
                "code": "generation_failed",
                "message": (
                    'call upstream API failed: W8X API returned 400: '
                    '{"error":{"message":"参考图最多支持 6 张","type":"invalid_request_error",'
                    '"param":null,"code":null}}'
                ),
            }
        }
        normalized = progress_module.normalize_toapis_error(payload)
        self.assertEqual(normalized["code"], progress_module.ERROR_INVALID_REQUEST)
        self.assertEqual(normalized["providerCode"], "generation_failed")
        self.assertEqual(normalized["upstreamType"], "invalid_request_error")
        self.assertEqual(normalized["upstreamMessage"], "参考图最多支持 6 张")
        presentation = progress_module.provider_error_presentation(
            normalized["code"],
            normalized["upstreamMessage"],
        )
        self.assertEqual(presentation["title"], "生成输入不符合服务要求")
        self.assertEqual(presentation["message"], "参考图最多支持 6 张")
        self.assertFalse(presentation["retryable"])

    def test_normalize_toapis_official_http_error_matrix(self) -> None:
        progress_module = importlib.import_module("app.generation_progress")
        cases = [
            (400, "invalid_request", progress_module.ERROR_INVALID_REQUEST, False),
            (401, "unauthorized", progress_module.ERROR_AUTH_FAILED, False),
            (402, "insufficient_quota", progress_module.ERROR_INSUFFICIENT_BALANCE, False),
            (404, "task_not_found", progress_module.ERROR_TASK_NOT_FOUND, True),
            (422, "content_policy_violation", progress_module.ERROR_CONTENT_POLICY, False),
            (429, "rate_limit_exceeded", progress_module.ERROR_RATE_LIMITED, True),
            (500, "internal_error", progress_module.ERROR_PROVIDER_INTERNAL, True),
        ]
        for status, provider_code, expected, retryable in cases:
            with self.subTest(status=status, provider_code=provider_code):
                normalized = progress_module.normalize_toapis_error(
                    {"error": {"code": provider_code, "message": "provider message"}},
                    status_code=status,
                )
                self.assertEqual(normalized["code"], expected)
                self.assertEqual(progress_module.provider_error_presentation(expected)["retryable"], retryable)


@unittest.skipIf(not HAS_MAIN_DEPS, "app.main 依赖不可用")
class ContinueFinalResultSetTests(TempEnvTestCase):
    def test_failed_preview_regeneration_resumes_preview_even_with_old_active_artifacts(self) -> None:
        main = importlib.import_module("app.main")
        db_module = importlib.import_module("app.db")
        timestamp = "2026-07-13T07:00:00Z"
        conn = sqlite3.connect(os.environ["APP_DATABASE_PATH"])
        conn.row_factory = sqlite3.Row
        try:
            task_id = int(conn.execute(
                """INSERT INTO generation_tasks
                   (name, status, active_styling_id, active_storyboard_preview_id, created_at, updated_at)
                   VALUES ('failed preview', 'FAILED', 22, 88, ?, ?)""",
                (timestamp, timestamp),
            ).lastrowid)
            preview_id = int(conn.execute(
                """INSERT INTO generation_previews
                   (task_id, styling_id, input_version_id, status, created_at, updated_at)
                   VALUES (?, 22, 77, 'FAILED', ?, ?)""",
                (task_id, timestamp, timestamp),
            ).lastrowid)
            conn.execute(
                """INSERT INTO generation_attempts
                   (owner_type, owner_id, provider, status, failed_at, created_at, updated_at)
                   VALUES ('generation_preview', ?, 'image-2', 'FAILED', ?, ?, ?)""",
                (preview_id, timestamp, timestamp, timestamp),
            )
            conn.commit()
        finally:
            conn.close()

        with db_module.get_db() as db:
            resume_status = main.resume_failed_generation_task(db, task_id, timestamp)
        self.assertEqual(resume_status, "STYLING_READY")
        with db_module.get_db() as db:
            task = db.execute("SELECT * FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "STYLING_READY")
        self.assertEqual(main.next_auto_final_step(task), "preview")

    def test_continue_keeps_succeeded_image_and_queues_only_incomplete_slots(self) -> None:
        main = importlib.import_module("app.main")
        db_module = importlib.import_module("app.db")
        timestamp = "2026-07-13T08:00:00Z"
        conn = sqlite3.connect(os.environ["APP_DATABASE_PATH"])
        conn.row_factory = sqlite3.Row
        try:
            task_id = int(conn.execute(
                "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('continue', 'FAILED', ?, ?)",
                (timestamp, timestamp),
            ).lastrowid)
            result_set_id = int(conn.execute(
                """INSERT INTO generation_result_sets
                   (task_id, input_version_id, styling_id, preview_id, status, created_at, updated_at)
                   VALUES (?, 77, 22, 33, 'PARTIAL', ?, ?)""",
                (task_id, timestamp, timestamp),
            ).lastrowid)
            conn.execute("UPDATE generation_tasks SET active_result_set_id = ? WHERE id = ?", (result_set_id, task_id))
            succeeded_id = int(conn.execute(
                """INSERT INTO generation_result_images_new
                   (result_set_id, shot_role, sort_order, status, current_asset_id, active_attempt_id, user_instruction_json, created_at, updated_at)
                   VALUES (?, 'top_grid_1', 1, 'SUCCEEDED', 501, 101, '{\"kept\": true}', ?, ?)""",
                (result_set_id, timestamp, timestamp),
            ).lastrowid)
            failed_id = int(conn.execute(
                """INSERT INTO generation_result_images_new
                   (result_set_id, shot_role, sort_order, status, active_attempt_id, created_at, updated_at)
                   VALUES (?, 'top_grid_2', 2, 'FAILED', 102, ?, ?)""",
                (result_set_id, timestamp, timestamp),
            ).lastrowid)
            cancelled_id = int(conn.execute(
                """INSERT INTO generation_result_images_new
                   (result_set_id, shot_role, sort_order, status, active_attempt_id, created_at, updated_at)
                   VALUES (?, 'top_grid_3', 3, 'CANCELLED', 103, ?, ?)""",
                (result_set_id, timestamp, timestamp),
            ).lastrowid)
            conn.commit()
        finally:
            conn.close()

        roles = [f"top_grid_{index}" for index in range(1, 6)]
        with mock.patch.object(main, "get_styling_plan_by_id", return_value={}):
            with mock.patch.object(main, "final_image_slot_roles", return_value=roles):
                queued_ids = main.continue_active_final_result_set(
                    task_id,
                    result_set_id,
                    22,
                    33,
                    {"final_image_user_instruction": "继续未完成槽位"},
                )
                from fastapi import HTTPException

                with self.assertRaises(HTTPException) as duplicate:
                    main.continue_active_final_result_set(
                        task_id,
                        result_set_id,
                        22,
                        33,
                        {"final_image_user_instruction": "重复继续"},
                    )

        self.assertIsNotNone(queued_ids)
        self.assertEqual(duplicate.exception.status_code, 409)
        self.assertEqual(set(queued_ids or []) & {succeeded_id}, set())
        self.assertTrue({failed_id, cancelled_id}.issubset(set(queued_ids or [])))
        with db_module.get_db() as db:
            result_sets = db.execute("SELECT id, status, input_version_id FROM generation_result_sets WHERE task_id = ?", (task_id,)).fetchall()
            images = db.execute(
                "SELECT sort_order, status, current_asset_id, active_attempt_id, user_instruction_json, updated_at FROM generation_result_images_new WHERE result_set_id = ? ORDER BY sort_order",
                (result_set_id,),
            ).fetchall()
            task = db.execute("SELECT status, active_result_set_id FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()

        self.assertEqual([(row["id"], row["input_version_id"]) for row in result_sets], [(result_set_id, 77)])
        self.assertEqual(len(images), 5)
        self.assertEqual(images[0]["status"], "SUCCEEDED")
        self.assertEqual(images[0]["current_asset_id"], 501)
        self.assertEqual(images[0]["active_attempt_id"], 101)
        self.assertEqual(images[0]["user_instruction_json"], '{"kept": true}')
        self.assertEqual(images[0]["updated_at"], timestamp)
        self.assertEqual([row["status"] for row in images[1:]], ["QUEUED", "QUEUED", "QUEUED", "QUEUED"])
        self.assertEqual(task["status"], "FINAL_RUNNING")
        self.assertEqual(task["active_result_set_id"], result_set_id)


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

    def test_toapis_check_uses_shared_concurrency_slot(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        provider = image_provider.GPTImage2ToapisProvider()
        completed_response = {"status": "completed", "result": {"data": [{"url": "https://files.toapis.com/out.png"}]}}
        events: list[str] = []

        @contextmanager
        def slot():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        with mock.patch.object(image_provider, "toapis_concurrency_slot", slot):
            with mock.patch.object(image_provider, "get_json", return_value=completed_response):
                asyncio.run(provider.check({"provider_request_id": "tsk_1", "output_path": "k/out.png"}))
        self.assertEqual(events, ["enter", "exit"])

    def test_toapis_upload_uses_shared_concurrency_slot(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        events: list[str] = []

        @contextmanager
        def slot():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        with mock.patch.object(image_provider, "toapis_concurrency_slot", slot):
            with mock.patch.object(image_provider, "fit_image_bytes_under_limit", side_effect=lambda content, filename, content_type: (content, filename, content_type)):
                with mock.patch.object(image_provider, "post_multipart_file", return_value={"data": {"url": "https://files.toapis.com/u/ref.png"}}):
                    result = image_provider.upload_bytes_to_toapis(b"png", "ref.png", "image/png")
        self.assertEqual(result, "https://files.toapis.com/u/ref.png")
        self.assertEqual(events, ["enter", "exit"])

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


class ProviderSubmitContractTests(TempEnvTestCase):
    """submit 响应契约：不得携带 outputPath。

    outputPath 只能由回传成功（store_delivered_image）产生；编排层用
    "payload 是否有 outputPath" 判断是否还需要执行回传阶段。submit 提前
    返回预期路径会导致轮询成功后跳过回传，读取不存在的 key 报 NoSuchKey。
    """

    def test_toapis_submit_response_has_no_output_path(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        provider = image_provider.GPTImage2ToapisProvider()
        with mock.patch.object(image_provider, "prepare_toapis_image_inputs", return_value=([], [])):
            with mock.patch.object(image_provider, "build_toapis_request_snapshot", return_value={"model": "gpt-image-2"}):
                with mock.patch.object(image_provider, "post_json", return_value={"id": "tsk_1", "status": "submitted"}):
                    result = asyncio.run(provider.submit({"prompt": "p", "imageInputs": []}, "k/out.png"))
        self.assertNotIn("outputPath", result)
        self.assertEqual(result["providerRequestId"], "tsk_1")
        self.assertEqual(result["status"], "SUBMITTED")

    def test_create_task_connection_failure_is_unknown_and_never_retried(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        progress_module = importlib.import_module("app.generation_progress")
        uncertain = image_provider.ImageProviderError(
            "read timed out",
            error_code=progress_module.ERROR_CONN_RESET,
            response_received=False,
        )
        with mock.patch.object(image_provider, "post_json", side_effect=uncertain) as post:
            with self.assertRaises(image_provider.ProviderSubmissionUnknownError) as ctx:
                image_provider.post_generation_task_with_confirmed_failure_retries(
                    "https://toapis.test/v1/images/generations",
                    {"model": "gpt-image-2"},
                    "key",
                    auth_scheme="Bearer",
                    error_label="submit failed",
                )
        self.assertEqual(ctx.exception.error_code, progress_module.ERROR_SUBMISSION_UNKNOWN)
        self.assertEqual(post.call_count, 1)

    def test_create_task_retries_only_confirmed_transient_http_failure(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        progress_module = importlib.import_module("app.generation_progress")
        confirmed = image_provider.ImageProviderError(
            "HTTP 502",
            error_code=progress_module.ERROR_GATEWAY_5XX,
            response_received=True,
        )
        with mock.patch.object(image_provider, "post_json", side_effect=[confirmed, {"id": "tsk_ok"}]) as post:
            with mock.patch.object(image_provider.time, "sleep"):
                result = image_provider.post_generation_task_with_confirmed_failure_retries(
                    "https://toapis.test/v1/images/generations",
                    {"model": "gpt-image-2"},
                    "key",
                    auth_scheme="Bearer",
                    error_label="submit failed",
                )
        self.assertEqual(result["id"], "tsk_ok")
        self.assertEqual(post.call_count, 2)
        self.assertTrue(all(call.kwargs["attempts"] == 1 for call in post.call_args_list))
        self.assertTrue(all(call.kwargs["timeout"] == 180 for call in post.call_args_list))

    def test_toapis_unknown_submission_preserves_uploaded_inputs_for_retry(self) -> None:
        image_provider = importlib.import_module("app.image_provider")
        uploaded = [{"role": "hero", "providerUrl": "https://files.toapis.com/ref.jpg"}]
        unknown = image_provider.ProviderSubmissionUnknownError("tls eof")
        provider = image_provider.GPTImage2ToapisProvider()
        with mock.patch.object(image_provider, "prepare_toapis_image_inputs", return_value=(uploaded, [uploaded[0]["providerUrl"]])):
            with mock.patch.object(image_provider, "build_toapis_request_snapshot", return_value={"model": "gpt-image-2"}):
                with mock.patch.object(image_provider, "post_generation_task_with_confirmed_failure_retries", side_effect=unknown):
                    with self.assertRaises(image_provider.ProviderSubmissionUnknownError) as ctx:
                        asyncio.run(provider.submit({"imageInputs": [{"role": "hero"}]}, "k/out.png"))
        self.assertEqual(ctx.exception.provider_response["imageInputs"], uploaded)
        self.assertEqual(ctx.exception.provider_response["status"], "SUBMISSION_UNKNOWN")

    def test_toapis_submit_uses_configured_submit_retry_limit(self) -> None:
        os.environ["GENERATION_SUBMIT_RETRY_LIMIT"] = "3"
        self._reload_settings()
        image_provider = importlib.import_module("app.image_provider")
        provider = image_provider.GPTImage2ToapisProvider()
        with mock.patch.object(image_provider, "prepare_toapis_image_inputs", return_value=([], [])):
            with mock.patch.object(image_provider, "build_toapis_request_snapshot", return_value={"model": "gpt-image-2"}):
                with mock.patch.object(
                    image_provider,
                    "post_generation_task_with_confirmed_failure_retries",
                    return_value={"id": "tsk_1", "status": "submitted"},
                ) as submit:
                    asyncio.run(provider.submit({"imageInputs": []}, "k/out.png"))
        self.assertEqual(submit.call_args.kwargs["max_attempts"], 4)

    def test_fal_submit_response_has_no_output_path(self) -> None:
        os.environ["FAL_KEY"] = "test-fal-key"
        self._reload_settings()
        image_provider = importlib.import_module("app.image_provider")
        provider = image_provider.GPTImage2FalProvider()
        submit_response = {"request_id": "req_1", "status_url": "s", "response_url": "r", "cancel_url": "c"}
        with mock.patch.object(image_provider, "prepare_fal_image_inputs", return_value=([], [])):
            with mock.patch.object(image_provider, "build_fal_request_snapshot", return_value={}):
                with mock.patch.object(image_provider, "post_json", return_value=submit_response):
                    result = asyncio.run(provider.submit({"prompt": "p", "imageInputs": []}, "k/out.png"))
        self.assertNotIn("outputPath", result)
        self.assertEqual(result["providerRequestId"], "req_1")


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
            self.main.ImageProviderError("HTTP 502 bad gateway", error_code=self.progress_module.ERROR_GATEWAY_5XX),
            self.main.ImageProviderError("timed out", error_code=self.progress_module.ERROR_CONN_RESET),
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

    def test_poll_stops_after_configured_status_retry_limit(self) -> None:
        os.environ["GENERATION_STATUS_RETRY_LIMIT"] = "1"
        self._reload_settings()
        attempt_id = self._insert_attempt()
        recorder = self.progress_module.AttemptProgressRecorder(attempt_id)
        transient = self.main.ImageProviderError(
            "HTTP 502 bad gateway",
            error_code=self.progress_module.ERROR_GATEWAY_5XX,
        )

        async def instant_sleep(_seconds: float) -> None:
            return None

        payload = {"providerRequestId": "tsk_1", "status": "SUBMITTED", "providerStatus": "submitted", "providerUrls": {}}
        with mock.patch.object(self.main, "run_image_provider_check", side_effect=[transient, transient]) as check:
            with mock.patch("asyncio.sleep", new=instant_sleep):
                with self.assertRaises(self.main.ImageProviderError) as ctx:
                    asyncio.run(self.main.poll_provider_until_terminal(payload, "k/out.png", attempt_id, recorder))
        self.assertEqual(ctx.exception.error_code, self.progress_module.ERROR_GATEWAY_5XX)
        self.assertEqual(check.call_count, 2)

    def test_poll_keeps_zero_progress_toapis_waiting_in_queue_phase(self) -> None:
        """ToAPIs in_progress 但 progress=0 时仍算排队，避免把后台等待误记为 AI 生成。"""
        db_module = importlib.import_module("app.db")
        attempt_id = self._insert_attempt()
        recorder = self.progress_module.AttemptProgressRecorder(attempt_id)
        responses: list[dict[str, object]] = [
            {
                "status": "IN_PROGRESS",
                "providerStatus": "in_progress",
                "providerStatusResponse": {"status": "in_progress", "progress": 0},
            },
            {
                "status": "IN_PROGRESS",
                "providerStatus": "in_progress",
                "providerStatusResponse": {"status": "in_progress", "progress": 12},
            },
            {
                "status": "SUCCEEDED",
                "providerStatus": "completed",
                "providerStatusResponse": {"status": "completed", "progress": 100},
            },
        ]

        def fake_check(_payload: dict, _output_key: str) -> dict:
            return responses.pop(0)

        async def instant_sleep(_seconds: float) -> None:
            return None

        payload = {"providerRequestId": "tsk_1", "status": "SUBMITTED", "providerStatus": "submitted", "providerUrls": {}}
        with mock.patch.object(self.main, "run_image_provider_check", side_effect=fake_check):
            with mock.patch("asyncio.sleep", new=instant_sleep):
                result = asyncio.run(self.main.poll_provider_until_terminal(payload, "k/out.png", attempt_id, recorder))

        self.assertEqual(result["status"], "SUCCEEDED")
        with db_module.get_db() as db:
            row = db.execute("SELECT progress_json FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
        progress = json.loads(row["progress_json"])
        phases = progress["phases"]
        self.assertEqual([entry["phase"] for entry in phases], ["queue", "generate"])
        self.assertEqual(phases[0]["detail"]["providerProgress"], 0.0)
        self.assertEqual(phases[1]["detail"]["providerProgress"], 12.0)

    def test_poll_direct_completion_adds_generate_phase_with_provider_duration(self) -> None:
        """ToAPIs 0% 后直接 completed 时，仍补齐生成阶段并采用 provider duration。"""
        db_module = importlib.import_module("app.db")
        attempt_id = self._insert_attempt()
        recorder = self.progress_module.AttemptProgressRecorder(attempt_id)
        responses: list[dict[str, object]] = [
            {
                "status": "IN_PROGRESS",
                "providerStatus": "in_progress",
                "providerStatusResponse": {"status": "in_progress", "progress": 0},
            },
            {
                "status": "SUCCEEDED",
                "providerStatus": "completed",
                "providerStatusResponse": {
                    "status": "completed",
                    "progress": 100,
                    "created_at": 1000,
                    "completed_at": 1118,
                },
            },
        ]

        def fake_check(_payload: dict, _output_key: str) -> dict:
            return responses.pop(0)

        async def instant_sleep(_seconds: float) -> None:
            return None

        payload = {"providerRequestId": "tsk_1", "status": "SUBMITTED", "providerStatus": "submitted", "providerUrls": {}}
        with mock.patch.object(self.main, "run_image_provider_check", side_effect=fake_check):
            with mock.patch("asyncio.sleep", new=instant_sleep):
                result = asyncio.run(self.main.poll_provider_until_terminal(payload, "k/out.png", attempt_id, recorder))

        self.assertEqual(result["status"], "SUCCEEDED")
        with db_module.get_db() as db:
            row = db.execute("SELECT progress_json FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
        progress = json.loads(row["progress_json"])
        phases = progress["phases"]
        self.assertEqual([entry["phase"] for entry in phases], ["queue", "generate"])
        self.assertEqual(phases[1]["detail"]["durationSeconds"], 118)

    def test_attempt_summary_exposes_manual_retry_mode_and_index(self) -> None:
        db_module = importlib.import_module("app.db")
        first_attempt_id = self._insert_attempt(owner_id=42)
        retry_attempt_id = self._insert_attempt(
            owner_id=42,
            request_snapshot_json=json.dumps({"invocation": {"mode": "manual_retry", "target": "final_image"}}),
        )
        with db_module.get_db() as db:
            db.execute(
                "UPDATE generation_attempts SET retry_of_attempt_id = ? WHERE id = ?",
                (first_attempt_id, retry_attempt_id),
            )

        summary = self.main.get_attempt_summary(retry_attempt_id)

        self.assertEqual(summary["retryOfAttemptId"], first_attempt_id)
        self.assertEqual(summary["retryMode"], "regenerate")
        self.assertEqual(summary["retryIndex"], 1)

    def test_preserves_manual_retry_invocation_for_precreated_attempt_resume(self) -> None:
        attempt_id = self._insert_attempt(
            request_snapshot_json=json.dumps({"invocation": {"mode": "manual_retry", "target": "final_image"}}),
        )

        mode = self.main.preserved_attempt_invocation_mode(attempt_id)

        self.assertEqual(mode, "manual_retry")

    def test_deliver_with_retries_exhausts_to_delivery_failed(self) -> None:
        """回传预算耗尽后以 DELIVERY_FAILED 落败，providerRequestId 保留。"""
        os.environ["GENERATION_DELIVERY_TIMEOUT_SECONDS"] = "1"
        self._reload_settings()
        attempt_id = self._insert_attempt()
        recorder = self.progress_module.AttemptProgressRecorder(attempt_id)
        payload = {"providerRequestId": "tsk_keep", "status": "SUCCEEDED", "providerStatus": "completed", "providerUrls": {}}

        async def instant_sleep(_seconds: float) -> None:
            return None

        delivery_error = self.progress_module.GenerationPhaseError(
            "download stalled",
            self.progress_module.ERROR_DELIVERY_FAILED,
        )
        with mock.patch.object(self.main, "run_image_provider_deliver", side_effect=delivery_error):
            with mock.patch("asyncio.sleep", new=instant_sleep):
                with self.assertRaises(self.main.ImageProviderError) as ctx:
                    asyncio.run(self.main.deliver_with_retries(payload, "k/out.png", attempt_id, recorder))
        self.assertEqual(ctx.exception.error_code, "DELIVERY_FAILED")
        self.assertEqual(ctx.exception.provider_response.get("providerRequestId"), "tsk_keep")

    def test_deliver_with_retries_recovers_after_transient_failure(self) -> None:
        attempt_id = self._insert_attempt()
        recorder = self.progress_module.AttemptProgressRecorder(attempt_id)
        payload = {"providerRequestId": "tsk_2", "status": "SUCCEEDED", "providerStatus": "completed", "providerUrls": {}}
        responses: list[object] = [
            self.progress_module.GenerationPhaseError("reset", self.progress_module.ERROR_DELIVERY_FAILED),
            {"outputPath": "k/out.png", "fileSize": 9},
        ]

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

    def test_pipeline_runs_delivery_after_provider_success(self) -> None:
        """NoSuchKey 回归用例：提交→轮询成功后必须执行回传，再读取回传后的 key。

        此前 submit 响应携带预期 outputPath，导致编排层误判"已回传"而跳过
        deliver，直接 get_bytes 一个从未写入的 key 报 NoSuchKey。
        """
        attempt_id = self._insert_attempt()
        submit_payload = {
            "provider": "toapis",
            "providerRequestId": "tsk_x",
            "status": "SUBMITTED",
            "providerStatus": "submitted",
            "providerUrls": {},
            "providerStatusResponse": {"status": "submitted"},
        }
        check_payload = {
            "status": "SUCCEEDED",
            "providerStatus": "completed",
            "providerRequestId": "tsk_x",
            "providerStatusResponse": {"status": "completed"},
        }
        deliver_payload = {"outputPath": "k/out.png", "fileSize": 3, "thumbPath": None, "previewPath": None}
        deliver_calls: list[str] = []

        def fake_deliver(_payload: dict, output_key: str) -> dict:
            deliver_calls.append(output_key)
            return deliver_payload

        async def instant_sleep(_seconds: float) -> None:
            return None

        with mock.patch.object(self.main, "run_image_provider_submit", return_value=submit_payload):
            with mock.patch.object(self.main, "run_image_provider_check", return_value=check_payload):
                with mock.patch.object(self.main, "run_image_provider_deliver", side_effect=fake_deliver):
                    with mock.patch.object(self.main, "get_bytes", return_value=b"img") as get_bytes_mock:
                        with mock.patch.object(self.main, "insert_generated_media_asset", return_value=4242):
                            with mock.patch("asyncio.sleep", new=instant_sleep):
                                asset_id, payload = asyncio.run(
                                    self.main.generate_image_asset({"imageInputs": []}, "k/out.png", attempt_id)
                                )
        self.assertEqual(asset_id, 4242)
        self.assertEqual(deliver_calls, ["k/out.png"])
        get_bytes_mock.assert_called_once_with("k/out.png")
        self.assertEqual(payload["outputPath"], "k/out.png")

    def test_toapis_image_asset_lifecycle_respects_global_concurrency_limit(self) -> None:
        os.environ["TOAPIS_MAX_CONCURRENCY"] = "2"
        os.environ["GPT_IMAGE2_PROVIDER"] = "toapis"
        self._reload_settings()
        attempt_ids = [self._insert_attempt(owner_id=index + 1) for index in range(4)]
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_submit(_task_package: dict, output_key: str, _on_progress: object = None) -> dict:
            nonlocal active, max_active

            with lock:
                active += 1
                max_active = max(max_active, active)
            return {
                "provider": "toapis",
                "providerRequestId": f"tsk_{output_key}",
                "status": "SUBMITTED",
                "providerStatus": "submitted",
            }

        def fake_check(payload: dict, _output_key: str) -> dict:
            time.sleep(0.05)
            return {**payload, "status": "SUCCEEDED", "providerStatus": "completed"}

        def fake_deliver(_payload: dict, _output_key: str) -> dict:
            nonlocal active

            with lock:
                active -= 1
            return {"outputPath": "k/out.png"}

        async def run_all() -> None:
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        self.main.generate_image_asset({"imageInputs": []}, f"k/{index}.png", attempt_id)
                        for index, attempt_id in enumerate(attempt_ids)
                    )
                ),
                timeout=5,
            )

        async def instant_sleep(_seconds: float) -> None:
            return None

        with mock.patch.object(self.main, "run_image_provider_submit", side_effect=fake_submit):
            with mock.patch.object(self.main, "run_image_provider_check", side_effect=fake_check):
                with mock.patch.object(self.main, "run_image_provider_deliver", side_effect=fake_deliver):
                    with mock.patch.object(self.main, "get_bytes", return_value=b"img"):
                        with mock.patch.object(self.main, "insert_generated_media_asset", return_value=4242):
                            with mock.patch("asyncio.sleep", new=instant_sleep):
                                asyncio.run(run_all())

        self.assertEqual(max_active, 2)

    def test_explicit_transient_provider_failure_retries_generation_only(self) -> None:
        attempt_id = self._insert_attempt()
        submits = [
            {"provider": "toapis", "providerRequestId": "tsk_1", "status": "SUBMITTED", "providerStatus": "submitted", "providerUrls": {}, "imageInputs": [{"role": "hero", "providerUrl": "https://files.toapis.com/ref.jpg"}]},
            {"provider": "toapis", "providerRequestId": "tsk_2", "status": "SUBMITTED", "providerStatus": "submitted", "providerUrls": {}, "imageInputs": [{"role": "hero", "providerUrl": "https://files.toapis.com/ref.jpg"}]},
        ]
        checks = [
            {"status": "FAILED", "providerStatus": "failed", "providerRequestId": "tsk_1", "error": "502 bad gateway"},
            {"status": "SUCCEEDED", "providerStatus": "completed", "providerRequestId": "tsk_2", "providerStatusResponse": {"status": "completed"}},
        ]

        async def instant_sleep(_seconds: float) -> None:
            return None

        with mock.patch.object(self.main, "run_image_provider_submit", side_effect=submits) as submit:
            with mock.patch.object(self.main, "run_image_provider_check", side_effect=checks):
                with mock.patch.object(self.main, "run_image_provider_deliver", return_value={"outputPath": "k/out.png"}):
                    with mock.patch.object(self.main, "get_bytes", return_value=b"img"):
                        with mock.patch.object(self.main, "insert_generated_media_asset", return_value=99):
                            with mock.patch("asyncio.sleep", new=instant_sleep):
                                asset_id, payload = asyncio.run(
                                    self.main.generate_image_asset(
                                        {"imageInputs": [{"role": "hero", "url": "https://source.example/ref.jpg"}]},
                                        "k/out.png",
                                        attempt_id,
                                    )
                                )
        self.assertEqual(asset_id, 99)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(payload["providerRequestHistory"], ["tsk_1", "tsk_2"])
        self.assertEqual(payload["generationRetries"], 1)
        second_task_package = submit.call_args_list[1].args[0]
        self.assertEqual(
            second_task_package["imageInputs"],
            [{"role": "hero", "providerUrl": "https://files.toapis.com/ref.jpg"}],
        )

    def test_explicit_generation_failed_retries_generation_to_configured_limit(self) -> None:
        os.environ["GENERATION_SUBMIT_RETRY_LIMIT"] = "3"
        self._reload_settings()
        attempt_id = self._insert_attempt()
        submits = [
            {
                "provider": "toapis",
                "providerRequestId": "tsk_failed_0",
                "status": "SUBMITTED",
                "providerStatus": "submitted",
                "imageInputs": [{"role": "hero", "providerUrl": "https://files.toapis.com/ref.jpg"}],
            },
            {
                "provider": "toapis",
                "providerRequestId": "tsk_failed_1",
                "status": "SUBMITTED",
                "providerStatus": "submitted",
                "imageInputs": [{"role": "hero", "providerUrl": "https://files.toapis.com/ref.jpg"}],
            },
            {
                "provider": "toapis",
                "providerRequestId": "tsk_failed_2",
                "status": "SUBMITTED",
                "providerStatus": "submitted",
                "imageInputs": [{"role": "hero", "providerUrl": "https://files.toapis.com/ref.jpg"}],
            },
            {
                "provider": "toapis",
                "providerRequestId": "tsk_retry_3",
                "status": "SUBMITTED",
                "providerStatus": "submitted",
                "imageInputs": [{"role": "hero", "providerUrl": "https://files.toapis.com/ref.jpg"}],
            },
        ]
        checks = [
            {
                "status": "FAILED",
                "providerStatus": "failed",
                "providerRequestId": "tsk_failed_0",
                "error": "生成失败：任务处理失败",
                "providerStatusResponse": {
                    "status": "failed",
                    "error": {"code": "generation_failed", "message": "生成失败：任务处理失败"},
                },
            },
            {
                "status": "FAILED",
                "providerStatus": "failed",
                "providerRequestId": "tsk_failed_1",
                "error": "生成失败：任务处理失败",
                "providerStatusResponse": {
                    "status": "failed",
                    "error": {"code": "generation_failed", "message": "生成失败：任务处理失败"},
                },
            },
            {
                "status": "FAILED",
                "providerStatus": "failed",
                "providerRequestId": "tsk_failed_2",
                "error": "生成失败：任务处理失败",
                "providerStatusResponse": {
                    "status": "failed",
                    "error": {"code": "generation_failed", "message": "生成失败：任务处理失败"},
                },
            },
            {
                "status": "SUCCEEDED",
                "providerStatus": "completed",
                "providerRequestId": "tsk_retry_3",
                "providerStatusResponse": {"status": "completed"},
            },
        ]

        async def instant_sleep(_seconds: float) -> None:
            return None

        with mock.patch.object(self.main, "run_image_provider_submit", side_effect=submits) as submit:
            with mock.patch.object(self.main, "run_image_provider_check", side_effect=checks):
                with mock.patch.object(self.main, "run_image_provider_deliver", return_value={"outputPath": "k/out.png"}):
                    with mock.patch.object(self.main, "get_bytes", return_value=b"img"):
                        with mock.patch.object(self.main, "insert_generated_media_asset", return_value=101):
                            with mock.patch("asyncio.sleep", new=instant_sleep):
                                asset_id, payload = asyncio.run(
                                    self.main.generate_image_asset(
                                        {"imageInputs": [{"role": "hero", "url": "https://source.example/ref.jpg"}]},
                                        "k/out.png",
                                        attempt_id,
                                    )
                                )

        self.assertEqual(asset_id, 101)
        self.assertEqual(submit.call_count, 4)
        self.assertEqual(payload["providerRequestHistory"], ["tsk_failed_0", "tsk_failed_1", "tsk_failed_2", "tsk_retry_3"])
        self.assertEqual(payload["generationRetries"], 3)

    def test_nested_invalid_request_does_not_retry_generation(self) -> None:
        attempt_id = self._insert_attempt()
        submitted = {
            "provider": "toapis",
            "providerRequestId": "tsk_invalid",
            "status": "SUBMITTED",
            "providerStatus": "submitted",
        }
        failed = {
            "status": "FAILED",
            "providerStatus": "failed",
            "providerRequestId": "tsk_invalid",
            "error": "所有渠道都失败",
            "providerStatusResponse": {
                "status": "failed",
                "error": {
                    "code": "generation_failed",
                    "message": (
                        'W8X API returned 400: {"error":{"message":"参考图最多支持 6 张",'
                        '"type":"invalid_request_error","param":null,"code":null}}'
                    ),
                },
            },
        }
        with mock.patch.object(self.main, "run_image_provider_submit", return_value=submitted) as submit:
            with mock.patch.object(self.main, "run_image_provider_check", return_value=failed):
                with self.assertRaises(self.main.ImageProviderError) as ctx:
                    asyncio.run(self.main.generate_image_asset({"imageInputs": []}, "k/out.png", attempt_id))
        self.assertEqual(ctx.exception.error_code, self.progress_module.ERROR_INVALID_REQUEST)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(ctx.exception.provider_response["providerError"]["upstreamMessage"], "参考图最多支持 6 张")

    def test_poll_local_error_is_system_error_without_retry(self) -> None:
        attempt_id = self._insert_attempt()
        recorder = self.progress_module.AttemptProgressRecorder(attempt_id)
        payload = {"providerRequestId": "tsk_1", "status": "SUBMITTED", "providerStatus": "submitted"}

        async def instant_sleep(_seconds: float) -> None:
            return None

        with mock.patch.object(self.main, "run_image_provider_check", side_effect=KeyError("bad local shape")) as check:
            with mock.patch("asyncio.sleep", new=instant_sleep):
                with self.assertRaises(self.main.ImageProviderError) as ctx:
                    asyncio.run(self.main.poll_provider_until_terminal(payload, "k/out.png", attempt_id, recorder))
        self.assertEqual(ctx.exception.error_code, self.progress_module.ERROR_SYSTEM_ERROR)
        self.assertEqual(check.call_count, 1)

    def test_delivery_local_error_is_system_error_without_retry(self) -> None:
        attempt_id = self._insert_attempt()
        recorder = self.progress_module.AttemptProgressRecorder(attempt_id)
        payload = {"providerRequestId": "tsk_1", "status": "SUCCEEDED", "providerStatus": "completed"}
        with mock.patch.object(self.main, "run_image_provider_deliver", side_effect=TypeError("local bug")) as deliver:
            with self.assertRaises(self.main.ImageProviderError) as ctx:
                asyncio.run(self.main.deliver_with_retries(payload, "k/out.png", attempt_id, recorder))
        self.assertEqual(ctx.exception.error_code, self.progress_module.ERROR_SYSTEM_ERROR)
        self.assertEqual(deliver.call_count, 1)

    def test_resumed_generation_keeps_retry_budget(self) -> None:
        attempt_id = self._insert_attempt(provider_request_id="tsk_2")
        resume = {
            "provider": "toapis",
            "providerRequestId": "tsk_4",
            "providerRequestHistory": ["tsk_1", "tsk_2", "tsk_3", "tsk_4"],
            "generationRetries": 3,
            "status": "SUBMITTED",
            "providerStatus": "submitted",
        }
        db_module = importlib.import_module("app.db")
        with db_module.get_db() as db:
            db.execute(
                "UPDATE generation_attempts SET response_snapshot_json = ? WHERE id = ?",
                (json.dumps(resume), attempt_id),
            )

        async def instant_sleep(_seconds: float) -> None:
            return None

        failed = {"status": "FAILED", "providerStatus": "failed", "providerRequestId": "tsk_4", "error": "502 bad gateway"}
        with mock.patch.object(self.main, "run_image_provider_submit") as submit:
            with mock.patch.object(self.main, "run_image_provider_check", return_value=failed):
                with mock.patch("asyncio.sleep", new=instant_sleep):
                    with self.assertRaises(self.main.ImageProviderError):
                        asyncio.run(self.main.generate_image_asset({"imageInputs": []}, "k/out.png", attempt_id))
        submit.assert_not_called()

    def test_unknown_submission_recovers_by_business_id_without_second_post(self) -> None:
        os.environ["GENERATION_UNKNOWN_SUBMISSION_RETRY_DELAY_SECONDS"] = "0"
        self._reload_settings()
        attempt_id = self._insert_attempt()
        uploaded = [{"role": "hero", "providerUrl": "https://files.toapis.com/ref.jpg"}]
        unknown = self.main.ImageProviderError(
            "tls eof",
            provider_response={"provider": "toapis", "status": "SUBMISSION_UNKNOWN", "imageInputs": uploaded},
            error_code=self.progress_module.ERROR_SUBMISSION_UNKNOWN,
        )
        submitted = {
            "provider": "toapis",
            "providerRequestId": "tsk_retry",
            "status": "SUBMITTED",
            "providerStatus": "submitted",
            "imageInputs": uploaded,
        }
        succeeded = {
            "status": "SUCCEEDED",
            "providerStatus": "completed",
            "providerRequestId": "tsk_retry",
            "providerStatusResponse": {"status": "completed"},
        }
        with mock.patch.object(self.main, "run_image_provider_submit", side_effect=[unknown, submitted]) as submit:
            with mock.patch.object(self.main, "run_image_provider_check", return_value=succeeded):
                with mock.patch.object(self.main, "run_image_provider_deliver", return_value={"outputPath": "k/out.png"}):
                    with mock.patch.object(self.main, "get_bytes", return_value=b"img"):
                        with mock.patch.object(self.main, "insert_generated_media_asset", return_value=77):
                            asset_id, payload = asyncio.run(
                                self.main.generate_image_asset(
                                    {"imageInputs": [{"role": "hero", "url": "https://source.example/ref.jpg"}]},
                                    "k/out.png",
                                    attempt_id,
                                )
                            )
        self.assertEqual(asset_id, 77)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(submit.call_args.args[0]["clientBusinessId"], f"ruyi_img_a{attempt_id}_r0")
        self.assertEqual(payload["unknownSubmissionRetries"], 1)
        self.assertEqual(payload["providerRequestId"], "tsk_retry")

    def test_unknown_submission_retry_budget_exhaustion_stops_after_two_posts(self) -> None:
        os.environ["GENERATION_UNKNOWN_SUBMISSION_RETRY_LIMIT"] = "1"
        os.environ["GENERATION_UNKNOWN_SUBMISSION_RETRY_DELAY_SECONDS"] = "0"
        self._reload_settings()
        attempt_id = self._insert_attempt()
        uploaded = [{"role": "hero", "providerUrl": "https://files.toapis.com/ref.jpg"}]

        def unknown() -> Exception:
            return self.main.ImageProviderError(
                "read timeout",
                provider_response={"provider": "toapis", "status": "SUBMISSION_UNKNOWN", "imageInputs": uploaded},
                error_code=self.progress_module.ERROR_SUBMISSION_UNKNOWN,
            )

        task_not_found = self.main.ImageProviderError(
            "HTTP 404 task not found",
            error_code=self.progress_module.ERROR_TASK_NOT_FOUND,
            response_received=True,
        )
        with mock.patch.object(self.main, "run_image_provider_submit", side_effect=[unknown(), unknown()]) as submit:
            with mock.patch.object(self.main, "run_image_provider_check", side_effect=task_not_found):
                with self.assertRaises(self.main.ImageProviderError) as ctx:
                    asyncio.run(self.main.generate_image_asset({"imageInputs": [{"role": "hero"}]}, "k/out.png", attempt_id))
        self.assertEqual(ctx.exception.error_code, self.progress_module.ERROR_SUBMISSION_UNKNOWN)
        self.assertEqual(ctx.exception.provider_response["unknownSubmissionRetries"], 1)
        self.assertEqual(submit.call_count, 2)

    def test_resubmission_clears_old_provider_id_before_new_post(self) -> None:
        attempt_id = self._insert_attempt(provider_request_id="tsk_old")
        recorder = self.progress_module.AttemptProgressRecorder(attempt_id)
        recorder.begin(self.progress_module.PHASE_SUBMIT, generationRetry=1)
        self.main.prepare_attempt_provider_resubmission(
            attempt_id,
            {
                "providerRequestId": "tsk_old",
                "providerRequestHistory": ["tsk_old"],
                "generationRetries": 1,
                "status": "FAILED",
            },
        )
        db_module = importlib.import_module("app.db")
        with db_module.get_db() as db:
            row = db.execute(
                "SELECT provider_request_id, response_snapshot_json FROM generation_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        snapshot = json.loads(row["response_snapshot_json"])
        self.assertIsNone(row["provider_request_id"])
        self.assertIsNone(snapshot["providerRequestId"])
        self.assertEqual(snapshot["providerRequestHistory"], ["tsk_old"])
        self.assertEqual(snapshot["status"], "SUBMITTING")
        code, _message = self.main.stale_attempt_error(attempt_id)
        self.assertEqual(code, self.progress_module.ERROR_SUBMISSION_UNKNOWN)
        self.assertFalse(self.main.attempt_has_provider_request_id(attempt_id))

        # 外层收到无 provider_response 的未知提交异常时，失败落库不能清空审计现场。
        self.main.finish_attempt(
            attempt_id,
            "FAILED",
            response_snapshot=None,
            error_code=self.progress_module.ERROR_SUBMISSION_UNKNOWN,
            error_message="submission result unknown",
        )
        with db_module.get_db() as db:
            failed = db.execute(
                "SELECT status, error_code, response_snapshot_json FROM generation_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        failed_snapshot = json.loads(failed["response_snapshot_json"])
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error_code"], self.progress_module.ERROR_SUBMISSION_UNKNOWN)
        self.assertEqual(failed_snapshot["providerRequestHistory"], ["tsk_old"])
        self.assertEqual(failed_snapshot["generationRetries"], 1)

    def test_local_store_failure_is_system_error_and_preserves_provider_snapshot(self) -> None:
        attempt_id = self._insert_attempt()
        submit_payload = {
            "provider": "toapis",
            "providerRequestId": "tsk_paid",
            "status": "SUCCEEDED",
            "providerStatus": "completed",
            "outputPath": "k/out.png",
        }
        with mock.patch.object(self.main, "run_image_provider_submit", return_value=submit_payload):
            with mock.patch.object(self.main, "get_bytes", side_effect=RuntimeError("RustFS unavailable")):
                with self.assertRaises(self.progress_module.GenerationPhaseError) as ctx:
                    asyncio.run(self.main.generate_image_asset({"imageInputs": []}, "k/out.png", attempt_id))
        self.assertEqual(ctx.exception.error_code, self.progress_module.ERROR_SYSTEM_ERROR)
        self.assertEqual(ctx.exception.provider_response["providerRequestId"], "tsk_paid")

    def test_stale_submit_without_provider_id_is_submission_unknown(self) -> None:
        attempt_id = self._insert_attempt()
        recorder = self.progress_module.AttemptProgressRecorder(attempt_id)
        recorder.begin(self.progress_module.PHASE_SUBMIT)
        code, message = self.main.stale_attempt_error(attempt_id)
        self.assertEqual(code, self.progress_module.ERROR_SUBMISSION_UNKNOWN)
        self.assertIn("停止自动重提", message)


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
                INSERT INTO generation_result_images_new (result_set_id, shot_role, sort_order, status, active_attempt_id, created_at, updated_at)
                VALUES (?, 'top_grid_1', 1, 'FAILED', ?, ?, ?)
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
