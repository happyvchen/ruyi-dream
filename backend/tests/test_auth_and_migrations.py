from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit


class BackendSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = dict(os.environ)
        os.environ.update(
            {
                "APP_S3_ACCESS_KEY_ID": "test-access-key",
                "APP_S3_SECRET_ACCESS_KEY": "test-secret-key",
                "APP_GATE_PASSWORD": "letmein-123456",
                "APP_SESSION_SECRET": "session-secret-123",
            }
        )

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def _create_new_final_result_set(
        self,
        db_module: object,
        image_statuses: list[str],
        task_status: str = "FINAL_RUNNING",
        result_set_status: str = "RUNNING",
    ) -> tuple[int, int, list[int]]:
        timestamp = db_module.now_iso()
        with db_module.get_db() as db:
            task_id = db.execute(
                "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('final recovery', ?, ?, ?)",
                (task_status, timestamp, timestamp),
            ).lastrowid
            input_id = db.execute(
                """
                INSERT INTO generation_task_input_versions (task_id, version_number, name_snapshot, image_snapshot_json, created_at)
                VALUES (?, 1, 'final recovery', '[]', ?)
                """,
                (task_id, timestamp),
            ).lastrowid
            styling_id = db.execute(
                """
                INSERT INTO generation_stylings
                  (task_id, input_version_id, source_material_ids_json, selected_material_ids_json,
                   selected_material_snapshot_json, status, created_at, updated_at)
                VALUES (?, ?, '[]', '[]', '[]', 'SUCCEEDED', ?, ?)
                """,
                (task_id, input_id, timestamp, timestamp),
            ).lastrowid
            preview_id = db.execute(
                """
                INSERT INTO generation_previews (task_id, styling_id, input_version_id, status, created_at, updated_at)
                VALUES (?, ?, ?, 'SUCCEEDED', ?, ?)
                """,
                (task_id, styling_id, input_id, timestamp, timestamp),
            ).lastrowid
            shot_ids: list[int] = []
            for index, _status in enumerate(image_statuses, start=1):
                shot_ids.append(
                    db.execute(
                        """
                        INSERT INTO generation_shot_plans (task_id, styling_id, sort_order, shot_type, plan_json, created_at, updated_at)
                        VALUES (?, ?, ?, 'test', '{}', ?, ?)
                        """,
                        (task_id, styling_id, index, timestamp, timestamp),
                    ).lastrowid
                )
            result_set_id = db.execute(
                """
                INSERT INTO generation_result_sets (task_id, input_version_id, styling_id, preview_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, input_id, styling_id, preview_id, result_set_status, timestamp, timestamp),
            ).lastrowid
            image_ids: list[int] = []
            for index, status in enumerate(image_statuses, start=1):
                image_ids.append(
                    db.execute(
                        """
                        INSERT INTO generation_result_images_new
                          (result_set_id, shot_plan_id, sort_order, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (result_set_id, shot_ids[index - 1], index, status, timestamp, timestamp),
                    ).lastrowid
                )
            db.execute(
                """
                UPDATE generation_tasks
                SET current_input_version_id = ?, active_styling_id = ?, active_preview_id = ?,
                    active_result_set_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (input_id, styling_id, preview_id, result_set_id, timestamp, task_id),
            )
        return int(task_id), int(result_set_id), [int(image_id) for image_id in image_ids]

    def test_app_gate_session_flow(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        config.get_settings.cache_clear()
        main = importlib.import_module("app.main")
        client = TestClient(main.app)

        self.assertEqual(client.get("/api/auth/session").json(), {"authRequired": True, "authenticated": False})
        self.assertEqual(client.get("/api/garment-skus").status_code, 401)
        self.assertEqual(client.post("/api/auth/login", json={"password": "bad"}).status_code, 401)
        login = client.post("/api/auth/login", json={"password": "letmein-123456"})
        self.assertEqual(login.status_code, 200)
        self.assertIn("set-cookie", login.headers)
        self.assertEqual(client.get("/api/auth/session").json(), {"authRequired": True, "authenticated": True})

    def test_presigned_urls_are_signed_with_public_s3_endpoint(self) -> None:
        object_storage = importlib.import_module("app.object_storage")
        object_storage.s3_client.cache_clear()
        object_storage.presign_s3_client.cache_clear()
        self.addCleanup(object_storage.s3_client.cache_clear)
        self.addCleanup(object_storage.presign_s3_client.cache_clear)

        class FakeSettings:
            s3_endpoint_url = "http://rustfs:9000"
            s3_public_endpoint_url = "http://203.0.113.10:9000"
            s3_region = "auto"
            s3_access_key_id = "test-access-key"
            s3_secret_access_key = "test-secret-key"
            s3_force_path_style = True
            s3_bucket = "ruyi-dream"
            s3_presign_expires_seconds = 900

        with patch.object(object_storage, "get_settings", return_value=FakeSettings):
            internal_url = object_storage.s3_client().generate_presigned_url(
                "get_object",
                Params={"Bucket": "ruyi-dream", "Key": "uploads/sample.jpg"},
                ExpiresIn=900,
            )
            public_url = object_storage.presigned_get_url("uploads/sample.jpg")

        self.assertIsNotNone(public_url)
        self.assertEqual(urlsplit(public_url).netloc, "203.0.113.10:9000")
        self.assertEqual(urlsplit(internal_url).netloc, "rustfs:9000")
        self.assertNotEqual(
            parse_qs(urlsplit(public_url).query)["X-Amz-Signature"],
            parse_qs(urlsplit(internal_url).query)["X-Amz-Signature"],
        )

    def test_virtual_hosted_s3_presigned_urls_are_supported(self) -> None:
        object_storage = importlib.import_module("app.object_storage")
        object_storage.s3_client.cache_clear()
        object_storage.presign_s3_client.cache_clear()
        self.addCleanup(object_storage.s3_client.cache_clear)
        self.addCleanup(object_storage.presign_s3_client.cache_clear)

        class FakeSettings:
            s3_endpoint_url = "https://oss-cn-hangzhou-internal.aliyuncs.com"
            s3_public_endpoint_url = "https://oss-cn-hangzhou.aliyuncs.com"
            s3_region = "cn-hangzhou"
            s3_access_key_id = "test-access-key"
            s3_secret_access_key = "test-secret-key"
            s3_force_path_style = False
            s3_bucket = "ruyi-dream-prod"
            s3_presign_expires_seconds = 900

        with patch.object(object_storage, "get_settings", return_value=FakeSettings):
            public_url = object_storage.presigned_get_url("uploads/sample.jpg")

        self.assertIsNotNone(public_url)
        split_url = urlsplit(public_url)
        self.assertEqual(split_url.scheme, "https")
        self.assertEqual(split_url.netloc, "ruyi-dream-prod.oss-cn-hangzhou.aliyuncs.com")
        self.assertEqual(split_url.path, "/uploads/sample.jpg")

    def test_media_redirect_is_not_cached_past_presigned_url_ttl(self) -> None:
        from fastapi.testclient import TestClient

        main = importlib.import_module("app.main")
        db_module = importlib.import_module("app.db")
        client = TestClient(main.app)
        login = client.post("/api/auth/login", json={"password": "letmein-123456"})
        self.assertEqual(login.status_code, 200)
        db_module.init_db()
        timestamp = db_module.now_iso()
        with db_module.get_db() as db:
            db.execute(
                """
                INSERT INTO media_assets (object_key, thumb_key, original_filename, mime_type, status, created_at, updated_at)
                VALUES ('uploads/sample.jpg', 'uploads/_derived/thumb/sample.webp', 'sample.jpg', 'image/jpeg', 'READY', ?, ?)
                """,
                (timestamp, timestamp),
            )

        signed_url = "http://203.0.113.10:9000/ruyi-dream/uploads/sample.webp?X-Amz-Expires=900&X-Amz-Signature=test"
        with (
            patch.object(main, "existing_derivative_or_original", return_value="uploads/_derived/thumb/sample.webp"),
            patch.object(main, "object_exists", return_value=True),
            patch.object(main, "presigned_get_url", return_value=signed_url),
        ):
            response = client.get(
                "/api/media?key=uploads/sample.jpg&variant=thumb",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], signed_url)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertNotIn("max-age=3600", response.headers["cache-control"])
        self.assertEqual(response.headers["pragma"], "no-cache")

    def test_media_redirect_allows_registered_legacy_media_keys(self) -> None:
        from fastapi.testclient import TestClient

        main = importlib.import_module("app.main")
        db_module = importlib.import_module("app.db")
        client = TestClient(main.app)
        self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)
        db_module.init_db()
        timestamp = db_module.now_iso()
        with db_module.get_db() as db:
            sku_id = db.execute(
                "INSERT INTO garment_skus (sku, storage_path, status, created_at, updated_at) VALUES ('LEGACY', 'uploads/legacy', 'NEW', ?, ?)",
                (timestamp, timestamp),
            ).lastrowid
            db.execute(
                """
                INSERT INTO garment_images
                  (sku_id, filename, storage_path, thumb_path, preview_path, mime_type, created_at)
                VALUES (?, 'legacy.jpg', 'uploads/legacy/original.jpg', 'uploads/legacy/thumb.webp',
                        'uploads/legacy/preview.webp', 'image/jpeg', ?)
                """,
                (sku_id, timestamp),
            )
            model_id = db.execute("SELECT id FROM model_profiles ORDER BY id LIMIT 1").fetchone()["id"]
            shot_plan_id = db.execute(
                """
                INSERT INTO shot_plans
                  (sku_id, model_profile_id, status, plan_json, planner_provider, created_at, updated_at)
                VALUES (?, ?, 'DRAFT', '{}', 'test', ?, ?)
                """,
                (sku_id, model_id, timestamp, timestamp),
            ).lastrowid
            run_id = db.execute(
                """
                INSERT INTO generation_runs
                  (sku_id, shot_plan_id, model_profile_id, status, kind, output_dir, created_at, updated_at)
                VALUES (?, ?, ?, 'SUCCEEDED', 'group', 'outputs/legacy', ?, ?)
                """,
                (sku_id, shot_plan_id, model_id, timestamp, timestamp),
            ).lastrowid
            db.execute(
                """
                INSERT INTO generation_images
                  (run_id, shot_index, title, status, output_path, thumb_path, preview_path, task_package_json, created_at, updated_at)
                VALUES (?, 1, 'legacy final', 'SUCCEEDED', 'outputs/legacy/final.png',
                        'outputs/legacy/final-thumb.webp', 'outputs/legacy/final-preview.webp', '{}', ?, ?)
                """,
                (run_id, timestamp, timestamp),
            )

        signed_url = "http://203.0.113.10:9000/ruyi-dream/registered.webp?X-Amz-Signature=test"
        with (
            patch.object(main, "existing_derivative_or_original", return_value=None),
            patch.object(main, "object_exists", return_value=True),
            patch.object(main, "presigned_get_url", return_value=signed_url) as presigned_get_url,
        ):
            garment_response = client.get("/api/media?key=uploads/legacy/preview.webp&variant=preview", follow_redirects=False)
            generation_response = client.get("/api/media?key=outputs/legacy/final-thumb.webp&variant=thumb", follow_redirects=False)
            unknown_response = client.get("/api/media?key=uploads/not-registered.jpg&variant=original", follow_redirects=False)

        self.assertEqual(garment_response.status_code, 307)
        self.assertEqual(generation_response.status_code, 307)
        self.assertEqual(unknown_response.status_code, 404)
        presigned_get_url.assert_any_call("uploads/legacy/preview.webp")
        presigned_get_url.assert_any_call("outputs/legacy/final-thumb.webp")

    def test_media_redirect_allows_model_profile_head_references(self) -> None:
        from fastapi.testclient import TestClient

        main = importlib.import_module("app.main")
        db_module = importlib.import_module("app.db")
        client = TestClient(main.app)
        self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)
        db_module.init_db()

        signed_url = "http://203.0.113.10:9000/ruyi-dream/model-preview.webp?X-Amz-Signature=test"
        with (
            patch.object(main, "object_exists", return_value=True),
            patch.object(main, "presigned_get_url", return_value=signed_url) as presigned_get_url,
        ):
            response = client.get(
                "/api/media?key=model-profiles/Face-01/front.png&variant=preview",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], signed_url)
        presigned_get_url.assert_called_with("model-profiles/Face-01/_derived/preview/front.webp")

    def test_upload_validation_accepts_decodable_images_despite_content_type_mismatch(self) -> None:
        from fastapi import UploadFile
        from PIL import Image
        from starlette.datastructures import Headers

        main = importlib.import_module("app.main")
        image_bytes = BytesIO()
        Image.new("RGB", (12, 12), "white").save(image_bytes, format="JPEG")

        mismatched_upload = UploadFile(
            BytesIO(image_bytes.getvalue()),
            filename="sample.jpg",
            headers=Headers({"content-type": "image/png"}),
        )
        _, content_type = asyncio.run(main.validate_upload_image(mismatched_upload))
        self.assertEqual(content_type, "image/jpeg")

        generic_upload = UploadFile(
            BytesIO(image_bytes.getvalue()),
            filename="sample.bin",
            headers=Headers({"content-type": "application/octet-stream"}),
        )
        _, generic_content_type = asyncio.run(main.validate_upload_image(generic_upload))
        self.assertEqual(generic_content_type, "image/jpeg")

    def test_toapis_image_provider_can_be_selected_by_env(self) -> None:
        os.environ["GPT_IMAGE2_PROVIDER"] = "toapis"
        os.environ["GPT_IMAGE2_MODEL_ID"] = "gpt-image-2"
        config = importlib.import_module("app.config")
        config = importlib.reload(config)
        config.get_settings.cache_clear()
        self.addCleanup(config.get_settings.cache_clear)

        settings = config.get_settings()

        self.assertEqual(settings.image_provider, "toapis")
        self.assertEqual(settings.gpt_image2_model_id, "gpt-image-2")

    def test_toapis_request_uses_reference_images(self) -> None:
        os.environ["GPT_IMAGE2_PROVIDER"] = "toapis"
        os.environ["GPT_IMAGE2_MODEL_ID"] = "gpt-image-2"
        config = importlib.import_module("app.config")
        config = importlib.reload(config)
        config.get_settings.cache_clear()
        self.addCleanup(config.get_settings.cache_clear)
        image_provider = importlib.import_module("app.image_provider")

        request = image_provider.build_toapis_request_snapshot(
            {
                "prompt": "生成棚拍图",
                "generation": {"stage": "final_group"},
            },
            ["https://files.toapis.com/uploads/reference.png"],
        )

        self.assertEqual(request["model"], "gpt-image-2")
        self.assertEqual(request["reference_images"], ["https://files.toapis.com/uploads/reference.png"])
        self.assertNotIn("image_urls", request)
        self.assertEqual(request["response_format"], "url")

    def test_toapis_status_response_extracts_result_url(self) -> None:
        image_provider = importlib.import_module("app.image_provider")

        self.assertEqual(image_provider.map_toapis_status("completed"), "SUCCEEDED")
        self.assertEqual(
            image_provider.extract_toapis_output_url(
                {"result": {"data": [{"url": "https://files.toapis.com/generated.png"}]}}
            ),
            "https://files.toapis.com/generated.png",
        )

    def test_toapis_upload_uses_requests_file_upload(self) -> None:
        image_provider = importlib.import_module("app.image_provider")

        class FakeResponse:
            ok = True
            status_code = 200
            text = ""

            def json(self) -> dict[str, object]:
                return {"data": {"url": "https://files.toapis.com/uploads/reference.png"}}

        with patch("requests.post", return_value=FakeResponse()) as post:
            response = image_provider.post_multipart_file(
                "https://toapis.com/v1/uploads/images",
                "file",
                "reference.png",
                b"image-bytes",
                "image/png",
                "test-toapis-key",
            )

        self.assertEqual(response["data"]["url"], "https://files.toapis.com/uploads/reference.png")
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer test-toapis-key"})
        self.assertIn("files", post.call_args.kwargs)
        self.assertEqual(post.call_args.kwargs["files"]["file"], ("reference.png", b"image-bytes", "image/png"))

    def test_toapis_json_calls_use_requests(self) -> None:
        image_provider = importlib.import_module("app.image_provider")

        class FakeResponse:
            ok = True
            status_code = 200
            text = ""

            def json(self) -> dict[str, object]:
                return {"id": "task-1", "status": "submitted"}

        with patch("requests.request", return_value=FakeResponse()) as request:
            response = image_provider.post_json(
                "https://toapis.com/v1/images/generations",
                {"model": "gpt-image-2"},
                "test-toapis-key",
                auth_scheme="Bearer",
                error_label="GPT Image-2 toapis 请求失败",
            )

        self.assertEqual(response["id"], "task-1")
        self.assertEqual(request.call_args.args[0], "POST")
        self.assertEqual(request.call_args.kwargs["headers"], {"Authorization": "Bearer test-toapis-key"})
        self.assertEqual(request.call_args.kwargs["json"], {"model": "gpt-image-2"})

    def test_fal_generate_uses_queue_polling_instead_of_sync_endpoint(self) -> None:
        os.environ["GPT_IMAGE2_PROVIDER"] = "fal"
        os.environ["GPT_IMAGE2_MODEL_ID"] = "openai/gpt-image-2/edit"
        os.environ["FAL_KEY"] = "test-fal-key"
        config = importlib.import_module("app.config")
        config = importlib.reload(config)
        config.get_settings.cache_clear()
        self.addCleanup(config.get_settings.cache_clear)
        image_provider = importlib.import_module("app.image_provider")

        submit_response = {
            "request_id": "fal-request-1",
            "status_url": "https://queue.fal.run/status",
            "response_url": "https://queue.fal.run/response",
            "cancel_url": "https://queue.fal.run/cancel",
        }
        status_response = {"status": "COMPLETED"}
        final_response = {"images": [{"url": "https://fal.media/output.png"}]}

        with (
            patch.object(image_provider, "prepare_fal_image_inputs", return_value=([{"providerUrl": "https://v3.fal.media/input.png"}], ["https://v3.fal.media/input.png"])),
            patch.object(image_provider, "post_json", return_value=submit_response) as post_json,
            patch.object(image_provider, "get_json", side_effect=[status_response, final_response]),
            patch.object(image_provider, "download_bytes", return_value=(b"png-bytes", "image/png")),
            patch.object(image_provider, "put_bytes"),
            patch.object(image_provider, "generate_image_derivatives", return_value={"thumb": "thumb.webp", "preview": "preview.webp"}),
            patch.object(image_provider.time, "sleep", return_value=None),
        ):
            result = asyncio.run(image_provider.GPTImage2FalProvider().generate({"prompt": "test", "imageInputs": []}, "outputs/test.png"))

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["providerRequestId"], "fal-request-1")
        self.assertEqual(post_json.call_args.args[0], "https://queue.fal.run/openai/gpt-image-2/edit")

    def test_upload_validation_rejects_decodable_but_unsupported_image_formats(self) -> None:
        from fastapi import HTTPException, UploadFile
        from PIL import Image
        from starlette.datastructures import Headers

        main = importlib.import_module("app.main")
        image_bytes = BytesIO()
        Image.new("RGB", (12, 12), "white").save(image_bytes, format="GIF")
        gif_upload = UploadFile(
            BytesIO(image_bytes.getvalue()),
            filename="sample.gif",
            headers=Headers({"content-type": "image/gif"}),
        )

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(main.validate_upload_image(gif_upload))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("仅支持 JPG、PNG 或 WebP 图片", raised.exception.detail)

    def test_bucket_permission_error_is_not_treated_as_missing_bucket(self) -> None:
        from botocore.exceptions import ClientError

        object_storage = importlib.import_module("app.object_storage")
        object_storage.s3_client.cache_clear()
        self.addCleanup(object_storage.s3_client.cache_clear)

        class FakeSettings:
            s3_endpoint_url = "http://127.0.0.1:9000"
            s3_bucket = "ruyi-dream"

        class FakeClient:
            create_called = False

            def head_bucket(self, Bucket: str) -> None:
                raise ClientError(
                    {
                        "Error": {"Code": "403", "Message": "Forbidden"},
                        "ResponseMetadata": {"HTTPStatusCode": 403},
                    },
                    "HeadBucket",
                )

            def create_bucket(self, Bucket: str) -> None:
                self.create_called = True

        fake_client = FakeClient()
        with (
            patch.object(object_storage, "get_settings", return_value=FakeSettings),
            patch.object(object_storage, "s3_client", return_value=fake_client),
        ):
            with self.assertRaisesRegex(RuntimeError, "Object storage bucket check failed"):
                object_storage.ensure_bucket()

        self.assertFalse(fake_client.create_called)

    def test_styling_plan_records_low_score_quality_signal(self) -> None:
        main = importlib.import_module("app.main")
        candidate_snapshot = {
            "candidate_material_ids": [9],
            "material_cards": [
                {"id": 9, "category": "outerwear", "colors": ["brown"]},
            ],
        }
        plan = {
            "selected_material_ids": [9],
            "selected_outfit_id": "candidate_a",
            "outfit_candidates": [
                {
                    "id": "candidate_a",
                    "selected_material_ids": [9],
                    "wearing_notes": "外套完全敞开穿着。",
                    "aesthetic_diagnosis": {
                        "color_weight": "上半身深色面积过大。",
                        "silhouette_balance": "外套体量偏重。",
                    },
                    "scores": {
                        "aesthetic_score": 0.4,
                        "brand_fit_score": 0.9,
                        "hero_visibility_score": 0.9,
                        "commercial_readiness_score": 0.9,
                    },
                }
            ],
        }

        main.validate_styling_plan(plan, candidate_snapshot)
        signals = main.collect_styling_quality_signals(plan, candidate_snapshot)

        self.assertTrue(any(signal["code"] == "low_critic_scores" and "aesthetic_score" in signal["fields"] for signal in signals))

    def test_styling_plan_records_full_scores_without_diagnosis_signal(self) -> None:
        main = importlib.import_module("app.main")
        candidate_snapshot = {
            "candidate_material_ids": [],
            "material_cards": [],
        }
        plan = {
            "selected_material_ids": [],
            "selected_outfit_id": "candidate_a",
            "outfit_candidates": [
                {
                    "id": "candidate_a",
                    "selected_material_ids": [],
                    "wearing_notes": "主推款单穿。",
                    "scores": {
                        "aesthetic_score": 1,
                        "brand_fit_score": 1,
                        "hero_visibility_score": 1,
                        "commercial_readiness_score": 1,
                    },
                    "risk_notes": [],
                }
            ],
        }

        main.validate_styling_plan(plan, candidate_snapshot)
        signals = main.collect_styling_quality_signals(plan, candidate_snapshot)

        self.assertTrue(any(signal["code"] == "suspicious_full_scores" for signal in signals))

    def test_styling_plan_allows_full_scores_with_complete_diagnosis(self) -> None:
        main = importlib.import_module("app.main")
        candidate_snapshot = {
            "candidate_material_ids": [],
            "material_cards": [],
        }
        plan = {
            "selected_material_ids": [],
            "selected_outfit_id": "candidate_a",
            "outfit_candidates": [
                {
                    "id": "candidate_a",
                    "selected_material_ids": [],
                    "wearing_notes": "主推款单穿。",
                    "aesthetic_diagnosis": {
                        "color_weight": "色彩干净轻量。",
                        "silhouette_balance": "主推款比例完整。",
                        "waistline_proportion": "腰线清楚。",
                        "season_material_fit": "材质季节匹配。",
                        "template_risk": "没有套用完整通勤模板。",
                        "wearing_method_validity": "主推款单穿，无额外穿法风险。",
                        "tuck_quality": "无塞腰穿法。",
                        "brand_read": "符合 Lily 干净通勤调性。",
                    },
                    "scores": {
                        "aesthetic_score": 1,
                        "brand_fit_score": 1,
                        "hero_visibility_score": 1,
                        "commercial_readiness_score": 1,
                    },
                    "risk_notes": [],
                }
            ],
        }

        main.validate_styling_plan(plan, candidate_snapshot)

    def test_fallback_styling_plan_is_rejected_by_production_source_check(self) -> None:
        main = importlib.import_module("app.main")
        candidate_snapshot = {
            "candidate_material_ids": [],
            "material_cards": [],
        }
        input_row = {
            "id": 1,
            "task_id": 1,
            "name_snapshot": "fallback",
            "image_snapshot_json": "[]",
        }
        plan = main.fallback_styling_plan(input_row, candidate_snapshot, "gemini_api_key_missing")

        self.assertEqual(plan["plan_source_mode"], "fallback")
        with self.assertRaisesRegex(RuntimeError, "fallback"):
            main.validate_styling_plan(plan, candidate_snapshot)

    def test_normalize_styling_plan_does_not_trust_model_plan_source_mode(self) -> None:
        main = importlib.import_module("app.main")
        candidate_snapshot = {
            "candidate_material_ids": [9],
            "material_cards": [
                {"id": 9, "category": "outerwear", "colors": ["brown"]},
            ],
        }
        raw_plan = {
            "plan_source_mode": "fallback",
            "selected_material_ids": [9],
            "selected_outfit_id": "candidate_a",
            "outfit_candidates": [
                {
                    "id": "candidate_a",
                    "selected_material_ids": [9],
                    "wearing_notes": "外套完全敞开穿着。",
                    "aesthetic_diagnosis": {
                        "color_weight": "上半身深色面积过大。",
                        "silhouette_balance": "外套体量偏重。",
                    },
                    "scores": {
                        "aesthetic_score": 0.4,
                        "brand_fit_score": 0.9,
                        "hero_visibility_score": 0.9,
                        "commercial_readiness_score": 0.9,
                    },
                }
            ],
        }

        plan = main.normalize_styling_plan(raw_plan, candidate_snapshot)

        self.assertEqual(plan["plan_source_mode"], "production")
        main.validate_styling_plan(plan, candidate_snapshot)
        signals = main.collect_styling_quality_signals(plan, candidate_snapshot)

        self.assertTrue(any(signal["code"] == "low_critic_scores" for signal in signals))

    def test_styling_plan_records_high_waist_dark_outerwear_stack_signal(self) -> None:
        main = importlib.import_module("app.main")
        candidate_snapshot = {
            "candidate_material_ids": [9, 10],
            "material_cards": [
                {"id": 9, "category": "outerwear", "colors": ["brown"]},
                {"id": 10, "category": "top", "colors": ["brown"], "wearability": {"recommended_wearing_methods": ["tucked"]}},
            ],
        }
        plan = {
            "hero_understanding": {
                "hero_visual_summary": "白色高腰阔腿西装裤，腰部有环扣和褶裥。",
            },
            "selected_material_ids": [10, 9],
            "selected_outfit_id": "candidate_a",
            "outfit_candidates": [
                {
                    "id": "candidate_a",
                    "selected_material_ids": [10, 9],
                    "wearing_notes": "短款针织衫自然落在裤腰上方，西装外套敞开穿着。",
                    "hero_focus": ["腰部环扣", "裤管垂坠"],
                    "aesthetic_diagnosis": {
                        "color_weight": "深棕和白色形成对比。",
                        "silhouette_balance": "外套敞开。",
                        "waistline_proportion": "短款上衣露出裤腰。",
                        "season_material_fit": "秋冬通勤。",
                        "template_risk": "常见通勤三件套。",
                        "wearing_method_validity": "短款上衣和敞开外套穿法成立。",
                        "tuck_quality": "无塞腰穿法。",
                        "brand_read": "商务通勤。",
                    },
                    "scores": {
                        "aesthetic_score": 0.9,
                        "brand_fit_score": 0.9,
                        "hero_visibility_score": 0.9,
                        "commercial_readiness_score": 0.9,
                    },
                    "risk_notes": ["外套可能遮挡腰部。"],
                }
            ],
        }

        main.validate_styling_plan(plan, candidate_snapshot)
        signals = main.collect_styling_quality_signals(plan, candidate_snapshot)

        self.assertTrue(any(signal["code"] == "high_waist_dark_outerwear_stack" for signal in signals))

    def test_candidate_funnel_caps_candidates_and_filters_conflicting_bottoms(self) -> None:
        main = importlib.import_module("app.main")
        input_row = {
            "id": 1,
            "task_id": 1,
            "name_snapshot": "白色高腰阔腿裤",
            "image_snapshot_json": "[]",
        }
        material_cards = [
            {"id": 1, "code": "BOTTOM", "category": "bottom", "colors": ["white"], "wearability": {}, "risk": []},
            {
                "id": 2,
                "code": "OUTER",
                "category": "outerwear",
                "colors": ["brown"],
                "wearability": {"coverage_risk": "likely_cover_hero"},
                "risk": ["too_heavy"],
            },
            {"id": 3, "code": "TOP", "category": "top", "colors": ["ivory"], "wearability": {}, "risk": []},
            *[
                {"id": index, "code": f"TOP-{index}", "category": "top", "colors": ["beige"], "wearability": {}, "risk": []}
                for index in range(4, 30)
            ],
        ]

        snapshot = main.build_candidate_retrieval_snapshot(input_row, material_cards)

        self.assertEqual(snapshot["schema_version"], "candidate-retrieval-v2")
        self.assertLessEqual(len(snapshot["material_cards"]), 20)
        self.assertNotIn(1, snapshot["candidate_material_ids"])
        self.assertIn(1, snapshot["source_material_ids"])
        self.assertTrue(any(item["id"] == 1 and "same_bottom_category_excluded" in item["reasons"] for item in snapshot["filtered_out"]))
        self.assertEqual(snapshot["outfit_drafts"][0]["selected_material_ids"], [])
        self.assertIn("library_adequacy", snapshot)

    def test_candidate_funnel_uses_matching_material_as_hero_and_excludes_it(self) -> None:
        main = importlib.import_module("app.main")
        input_row = {
            "id": 1,
            "task_id": 1,
            "name_snapshot": "PANTS-001",
            "image_snapshot_json": "[]",
        }
        material_cards = [
            {
                "id": 10,
                "code": "PANTS-001",
                "category": "bottom",
                "colors": ["white"],
                "details": ["belt", "pleat"],
                "silhouette": ["wide_leg"],
                "wearability": {"recommended_wearing_methods": ["belted", "tucked"]},
                "risk": [],
            },
            {"id": 11, "code": "VEST-001", "category": "top", "colors": ["white"], "wearability": {}, "risk": []},
        ]

        snapshot = main.build_candidate_retrieval_snapshot(input_row, material_cards)

        self.assertEqual(snapshot["hero_profile"]["source"], "styling_garment_code_match")
        self.assertEqual(snapshot["hero_profile"]["category"], "bottom")
        self.assertNotIn(10, snapshot["candidate_material_ids"])
        self.assertIn(11, snapshot["candidate_material_ids"])
        self.assertTrue(any(item["id"] == 10 and "current_hero_same_code_excluded" in item["reasons"] for item in snapshot["filtered_out"]))
        self.assertEqual(snapshot["library_adequacy"]["excluded_current_hero_count"], 1)

    def test_styling_plan_records_unsupported_tuck_method_signal(self) -> None:
        main = importlib.import_module("app.main")
        candidate_snapshot = {
            "candidate_material_ids": [9],
            "hero_profile": {"category": "bottom"},
            "material_cards": [
                {
                    "id": 9,
                    "category": "knitwear",
                    "sub_category": "cardigan",
                    "materials_visual": ["knit"],
                    "details": ["button"],
                    "wearability": {"recommended_wearing_methods": ["buttoned", "open"]},
                    "colors": ["ivory"],
                },
            ],
        }
        plan = {
            "selected_material_ids": [9],
            "selected_outfit_id": "candidate_a",
            "outfit_candidates": [
                {
                    "id": "candidate_a",
                    "selected_material_ids": [9],
                    "wearing_notes": "针织开衫全部扣起并全塞入裤腰。",
                    "aesthetic_diagnosis": {
                        "color_weight": "浅色同色系。",
                        "silhouette_balance": "上短下长。",
                        "waistline_proportion": "露出裤腰。",
                        "season_material_fit": "春夏匹配。",
                        "template_risk": "通勤公式。",
                        "wearing_method_validity": "缺少全塞依据。",
                        "tuck_quality": "全塞会挤压门襟。",
                        "brand_read": "偏 Lily。",
                    },
                    "scores": {
                        "aesthetic_score": 0.9,
                        "brand_fit_score": 0.9,
                        "hero_visibility_score": 0.9,
                        "commercial_readiness_score": 0.9,
                    },
                    "risk_notes": ["穿法风险。"],
                }
            ],
        }

        main.validate_styling_plan(plan, candidate_snapshot)
        signals = main.collect_styling_quality_signals(plan, candidate_snapshot)

        self.assertTrue(any(signal["code"] == "unsupported_tuck_method" for signal in signals))

    def test_forbidden_tuck_method_detects_equivalent_waist_tuck_phrases(self) -> None:
        main = importlib.import_module("app.main")
        candidate_snapshot = {
            "candidate_material_ids": [9],
            "hero_profile": {"category": "bottom"},
            "material_cards": [
                {
                    "id": 9,
                    "category": "knitwear",
                    "sub_category": "cardigan",
                    "materials_visual": ["knit"],
                    "details": ["button"],
                    "wearability": {"recommended_wearing_methods": ["buttoned", "open"]},
                },
            ],
        }

        phrases = [
            "针织开衫下摆收进裤腰。",
            "针织开衫扎进裤腰。",
            "针织开衫掖进裤腰。",
            "针织开衫束进裤腰。",
            "针织开衫 tucked-in 到裤腰。",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertTrue(main.has_forbidden_tuck_method([9], candidate_snapshot, {"wearing_notes": phrase}))

    def test_bottom_shot_plan_uses_category_specific_frames(self) -> None:
        main = importlib.import_module("app.main")
        plans = main.build_default_new_shot_plans(
            [],
            {
                "hero_understanding": {
                    "category": "bottom",
                    "hero_selling_points": ["腰头", "裤褶", "裤腿垂坠"],
                },
                "selected_outfit_id": "candidate_a",
                "outfit_candidates": [
                    {
                        "id": "candidate_a",
                        "selected_material_ids": [],
                        "wearing_notes": "主推裤装单穿展示。",
                    }
                ],
            },
        )

        self.assertEqual([item["shotType"] for item in plans[:5]], ["pants_hero", "pants_waist_to_feet", "pants_waist_detail", "pants_side_motion", "pants_front_fit"])
        self.assertIn("腰头", plans[1]["mainFocus"])

    def test_styling_plan_records_forbidden_briefcase_accessory_signal(self) -> None:
        main = importlib.import_module("app.main")
        candidate_snapshot = {
            "candidate_material_ids": [],
            "material_cards": [],
        }
        plan = {
            "selected_material_ids": [],
            "selected_outfit_id": "candidate_a",
            "outfit_candidates": [
                {
                    "id": "candidate_a",
                    "selected_material_ids": [],
                    "generated_accessories": ["棕色尖头高跟皮鞋", "米白色手提公文包"],
                    "wearing_notes": "主推款单穿。",
                    "aesthetic_diagnosis": {
                        "color_weight": "色彩干净轻量。",
                        "silhouette_balance": "主推款比例完整。",
                        "waistline_proportion": "腰线清楚。",
                        "season_material_fit": "材质季节匹配。",
                        "template_risk": "没有套用完整通勤模板。",
                        "wearing_method_validity": "主推款单穿，无额外穿法风险。",
                        "tuck_quality": "无塞腰穿法。",
                        "brand_read": "符合 Lily 干净通勤调性。",
                    },
                    "scores": {
                        "aesthetic_score": 0.9,
                        "brand_fit_score": 0.9,
                        "hero_visibility_score": 0.9,
                        "commercial_readiness_score": 0.9,
                    },
                    "risk_notes": [],
                }
            ],
        }

        main.validate_styling_plan(plan, candidate_snapshot)
        signals = main.collect_styling_quality_signals(plan, candidate_snapshot)

        self.assertTrue(any(signal["code"] == "template_accessory_risk" for signal in signals))

    def test_schema_migrations_are_recorded(self) -> None:
        config = importlib.import_module("app.config")
        config.get_settings.cache_clear()
        db_module = importlib.import_module("app.db")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            db_module.init_db()
            with db_module.get_db() as db:
                versions = [
                    row["version"]
                    for row in db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
                ]
                image_columns = {row["name"] for row in db.execute("PRAGMA table_info(generation_images)").fetchall()}
                garment_image_columns = {row["name"] for row in db.execute("PRAGMA table_info(garment_images)").fetchall()}
                sku_columns = {row["name"] for row in db.execute("PRAGMA table_info(garment_skus)").fetchall()}
                styling_garment_columns = {row["name"] for row in db.execute("PRAGMA table_info(styling_garments)").fetchall()}
                input_version_columns = {row["name"] for row in db.execute("PRAGMA table_info(generation_task_input_versions)").fetchall()}
                task_columns = {row["name"] for row in db.execute("PRAGMA table_info(generation_tasks)").fetchall()}
                styling_columns = {row["name"] for row in db.execute("PRAGMA table_info(generation_stylings)").fetchall()}
                preview_columns = {row["name"] for row in db.execute("PRAGMA table_info(generation_previews)").fetchall()}
                result_image_columns = {row["name"] for row in db.execute("PRAGMA table_info(generation_result_images_new)").fetchall()}
                template_count = db.execute("SELECT COUNT(*) AS count FROM brand_template_versions").fetchone()["count"]

        self.assertEqual(versions, ["001", "002", "003", "004", "005", "006", "007", "008", "009", "010", "011", "012", "013", "014", "015", "016"])
        self.assertIn("review_status", image_columns)
        self.assertIn("locked_by", image_columns)
        self.assertIn("thumb_path", image_columns)
        self.assertIn("preview_path", garment_image_columns)
        self.assertIn("archived_at", sku_columns)
        self.assertIn("official_title_tags_json", styling_garment_columns)
        self.assertIn("effective_tags_json", styling_garment_columns)
        self.assertIn("styling_plan_json", styling_columns)
        self.assertIn("candidate_retrieval_snapshot_json", styling_columns)
        self.assertIn("library_adequacy_snapshot_json", styling_columns)
        self.assertIn("outfit_candidates_json", styling_columns)
        self.assertIn("locked_until", preview_columns)
        self.assertIn("run_attempts", result_image_columns)
        self.assertIn("tag_meta_json", styling_garment_columns)
        self.assertIn("archived_at", styling_garment_columns)
        self.assertIn("archive_reason", styling_garment_columns)
        self.assertIn("model_snapshot_json", input_version_columns)
        self.assertIn("archived_at", task_columns)
        self.assertIn("archive_reason", task_columns)
        self.assertGreaterEqual(template_count, 1)

    def test_terminal_generation_run_cancel_returns_conflict(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            timestamp = db_module.now_iso()
            with db_module.get_db() as db:
                model_id = db.execute("SELECT id FROM model_profiles ORDER BY id LIMIT 1").fetchone()["id"]
                sku_id = db.execute(
                    """
                    INSERT INTO garment_skus (sku, title, storage_path, created_at, updated_at)
                    VALUES ('SKU-TEST', 'Test SKU', 'garments/sku-test', ?, ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                shot_plan_id = db.execute(
                    """
                    INSERT INTO shot_plans
                      (sku_id, model_profile_id, status, plan_json, planner_provider, created_at, updated_at)
                    VALUES (?, ?, 'CONFIRMED', '{}', 'test', ?, ?)
                    """,
                    (sku_id, model_id, timestamp, timestamp),
                ).lastrowid
                run_id = db.execute(
                    """
                    INSERT INTO generation_runs
                      (sku_id, shot_plan_id, model_profile_id, kind, status, output_dir, created_at, updated_at)
                    VALUES (?, ?, ?, 'group', 'SUCCEEDED', 'outputs/test-run', ?, ?)
                    """,
                    (sku_id, shot_plan_id, model_id, timestamp, timestamp),
                ).lastrowid
                db.execute(
                    """
                    INSERT INTO generation_images
                      (run_id, shot_index, title, status, output_path, task_package_json, is_final, created_at, updated_at)
                    VALUES (?, 1, 'Front', 'SUCCEEDED', 'outputs/test-run/front.png', '{}', 1, ?, ?)
                    """,
                    (run_id, timestamp, timestamp),
                )

            client = TestClient(main.app)
            login = client.post("/api/auth/login", json={"password": "letmein-123456"})
            self.assertEqual(login.status_code, 200)
            response = client.post(f"/api/generation-runs/{run_id}/cancel")
            self.assertEqual(response.status_code, 409)
            with db_module.get_db() as db:
                status = db.execute("SELECT status FROM generation_runs WHERE id = ?", (run_id,)).fetchone()["status"]

        self.assertEqual(status, "SUCCEEDED")

    def test_running_generation_task_cancel_marks_preview_and_attempt_cancelled(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            timestamp = db_module.now_iso()
            with db_module.get_db() as db:
                task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('preview cancel', 'PREVIEW_RUNNING', ?, ?)",
                    (timestamp, timestamp),
                ).lastrowid
                input_id = db.execute(
                    """
                    INSERT INTO generation_task_input_versions (task_id, version_number, name_snapshot, image_snapshot_json, created_at)
                    VALUES (?, 1, 'preview cancel', '[]', ?)
                    """,
                    (task_id, timestamp),
                ).lastrowid
                styling_id = db.execute(
                    """
                    INSERT INTO generation_stylings
                      (task_id, input_version_id, source_material_ids_json, selected_material_ids_json,
                       selected_material_snapshot_json, status, created_at, updated_at)
                    VALUES (?, ?, '[]', '[]', '[]', 'SUCCEEDED', ?, ?)
                    """,
                    (task_id, input_id, timestamp, timestamp),
                ).lastrowid
                preview_id = db.execute(
                    """
                    INSERT INTO generation_previews (task_id, styling_id, input_version_id, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'RUNNING', ?, ?)
                    """,
                    (task_id, styling_id, input_id, timestamp, timestamp),
                ).lastrowid
                attempt_id = db.execute(
                    """
                    INSERT INTO generation_attempts (owner_type, owner_id, provider, status, started_at, created_at, updated_at)
                    VALUES ('generation_preview', ?, 'image-2', 'RUNNING', ?, ?, ?)
                    """,
                    (preview_id, timestamp, timestamp, timestamp),
                ).lastrowid
                db.execute(
                    """
                    UPDATE generation_tasks
                    SET current_input_version_id = ?, active_styling_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (input_id, styling_id, timestamp, task_id),
                )

            client = TestClient(main.app)
            self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)
            response = client.post(f"/api/generation-tasks/{task_id}/cancel")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["item"]["status"], "CANCELLED")

            with db_module.get_db() as db:
                task_status = db.execute("SELECT status FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()["status"]
                preview_status = db.execute("SELECT status FROM generation_previews WHERE id = ?", (preview_id,)).fetchone()["status"]
                attempt = db.execute("SELECT status, cancelled_at FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()

        self.assertEqual(task_status, "CANCELLED")
        self.assertEqual(preview_status, "CANCELLED")
        self.assertEqual(attempt["status"], "CANCELLED")
        self.assertIsNotNone(attempt["cancelled_at"])

    def test_select_styling_cancel_before_provider_call_prevents_gemini_spend(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            timestamp = db_module.now_iso()
            with db_module.get_db() as db:
                task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('styling race', 'READY_FOR_STYLING', ?, ?)",
                    (timestamp, timestamp),
                ).lastrowid
                media_id = db.execute(
                    """
                    INSERT INTO media_assets (object_key, original_filename, mime_type, status, created_at, updated_at)
                    VALUES ('generation-tasks/styling-race/hero.jpg', 'hero.jpg', 'image/jpeg', 'READY', ?, ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                db.execute(
                    """
                    INSERT INTO generation_task_images (task_id, media_asset_id, sort_order, status, created_at, updated_at)
                    VALUES (?, ?, 0, 'READY', ?, ?)
                    """,
                    (task_id, media_id, timestamp, timestamp),
                )

            client = TestClient(main.app)
            self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)

            def cancel_during_context(input_version_id: int) -> tuple[object, dict[str, object], dict[str, object]]:
                main.cancel_generation_task(int(task_id))
                return object(), {}, {"inputVersionId": input_version_id}

            with (
                patch.object(main, "snapshot_default_model", return_value={}),
                patch.object(main, "build_ai_stylist_context", side_effect=cancel_during_context),
                patch.object(main, "select_styling_with_gemini", new_callable=AsyncMock) as gemini,
            ):
                response = client.post(f"/api/generation-tasks/{task_id}/select-styling")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["item"]["status"], "CANCELLED")
            gemini.assert_not_awaited()
            with db_module.get_db() as db:
                attempt = db.execute(
                    """
                    SELECT status, cancelled_at, request_snapshot_json
                    FROM generation_attempts
                    WHERE owner_type = 'generation_styling' AND owner_id = ?
                    """,
                    (task_id,),
                ).fetchone()

        self.assertEqual(attempt["status"], "CANCELLED")
        self.assertIsNotNone(attempt["cancelled_at"])
        self.assertIn("inputVersionId", attempt["request_snapshot_json"])

    def test_running_generation_task_cancel_preserves_partial_final_results(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            task_id, result_set_id, image_ids = self._create_new_final_result_set(
                db_module,
                ["SUCCEEDED", "RUNNING", "QUEUED"],
            )
            timestamp = db_module.now_iso()
            with db_module.get_db() as db:
                attempt_id = db.execute(
                    """
                    INSERT INTO generation_attempts (owner_type, owner_id, provider, status, started_at, created_at, updated_at)
                    VALUES ('generation_result_image', ?, 'image-2', 'RUNNING', ?, ?, ?)
                    """,
                    (image_ids[1], timestamp, timestamp, timestamp),
                ).lastrowid

            client = TestClient(main.app)
            self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)
            response = client.post(f"/api/generation-tasks/{task_id}/cancel")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["item"]["status"], "FINAL_PARTIAL")

            with db_module.get_db() as db:
                task_status = db.execute("SELECT status FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()["status"]
                result_set_status = db.execute("SELECT status FROM generation_result_sets WHERE id = ?", (result_set_id,)).fetchone()["status"]
                image_statuses = [
                    row["status"]
                    for row in db.execute(
                        "SELECT status FROM generation_result_images_new WHERE result_set_id = ? ORDER BY sort_order",
                        (result_set_id,),
                    ).fetchall()
                ]
                attempt = db.execute("SELECT status, cancelled_at FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()

        self.assertEqual(task_status, "FINAL_PARTIAL")
        self.assertEqual(result_set_status, "PARTIAL")
        self.assertEqual(image_statuses, ["SUCCEEDED", "CANCELLED", "CANCELLED"])
        self.assertEqual(attempt["status"], "CANCELLED")
        self.assertIsNotNone(attempt["cancelled_at"])

    def test_tagging_running_styling_garment_rejects_mutation_and_archive(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            with patch.object(config.Settings, "database_path", config.Path(db_file.name)):
                config.get_settings.cache_clear()
                db_module.init_db()
                timestamp = db_module.now_iso()
                code = f"STYLING-LOCK-{os.urandom(5).hex()}"
                with db_module.get_db() as db:
                    garment_id = db.execute(
                        """
                        INSERT INTO styling_garments (code, status, manual_tags_json, ai_tags_json, created_at, updated_at)
                        VALUES (?, 'TAGGING_RUNNING', '{}', '{}', ?, ?)
                        """,
                        (code, timestamp, timestamp),
                    ).lastrowid

                client = TestClient(main.app)
                self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)
                edit = client.patch(f"/api/styling-garments/{garment_id}", json={"code": "CHANGED"})
                archive = client.delete(f"/api/styling-garments/{garment_id}")
            config.get_settings.cache_clear()

        self.assertEqual(edit.status_code, 409)
        self.assertEqual(archive.status_code, 409)

    def test_stale_tagging_garment_is_expired_on_read(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            with patch.object(config.Settings, "database_path", config.Path(db_file.name)):
                config.get_settings.cache_clear()
                db_module.init_db()
                stale_timestamp = "2000-01-01T00:00:00"
                code = f"STALE-TAGGING-{os.urandom(5).hex()}"
                with db_module.get_db() as db:
                    garment_id = db.execute(
                        """
                        INSERT INTO styling_garments (code, status, manual_tags_json, ai_tags_json, created_at, updated_at)
                        VALUES (?, 'TAGGING_RUNNING', '{}', '{}', ?, ?)
                        """,
                        (code, stale_timestamp, stale_timestamp),
                    ).lastrowid
                    attempt_id = db.execute(
                        """
                        INSERT INTO generation_attempts
                          (owner_type, owner_id, provider, status, started_at, created_at, updated_at)
                        VALUES ('styling_garment_tagging', ?, 'gemini', 'RUNNING', ?, ?, ?)
                        """,
                        (garment_id, stale_timestamp, stale_timestamp, stale_timestamp),
                    ).lastrowid

                client = TestClient(main.app)
                self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)
                response = client.get(f"/api/styling-garments/{garment_id}")

                with db_module.get_db() as db:
                    garment_status = db.execute("SELECT status FROM styling_garments WHERE id = ?", (garment_id,)).fetchone()["status"]
                    attempt = db.execute("SELECT status, error_code FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
            config.get_settings.cache_clear()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["status"], "TAGGING_FAILED")
        self.assertEqual(garment_status, "TAGGING_FAILED")
        self.assertEqual(attempt["status"], "FAILED")
        self.assertEqual(attempt["error_code"], "TIMEOUT")

    def test_successful_tagging_clears_timeout_error_state(self) -> None:
        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        timeout_message = "打标任务长时间未完成，已自动停止轮询，可手动重新打标。"
        tagging_result = {
            "schema_version": main.LILY_TAG_SCHEMA_VERSION,
            "ai_tags": {"category": "top"},
            "official_title_tags": {},
            "tag_meta": {
                "tagger_provider": "gemini",
                "overall_confidence": 0.95,
                "needs_review": False,
                "review_reasons": [],
            },
        }

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            with patch.object(config.Settings, "database_path", config.Path(db_file.name)):
                config.get_settings.cache_clear()
                db_module.init_db()
                timestamp = db_module.now_iso()
                code = f"STALE-SUCCESS-{os.urandom(5).hex()}"
                with db_module.get_db() as db:
                    garment_id = db.execute(
                        """
                        INSERT INTO styling_garments
                          (code, status, manual_tags_json, ai_tags_json, tag_error, created_at, updated_at)
                        VALUES (?, 'TAGGING_FAILED', '{}', '{}', ?, ?, ?)
                        """,
                        (code, timeout_message, timestamp, timestamp),
                    ).lastrowid
                    attempt_id = db.execute(
                        """
                        INSERT INTO generation_attempts
                          (owner_type, owner_id, provider, status, started_at, failed_at,
                           error_code, error_message, created_at, updated_at)
                        VALUES ('styling_garment_tagging', ?, 'gemini', 'FAILED', ?, ?,
                                'TIMEOUT', ?, ?, ?)
                        """,
                        (garment_id, timestamp, timestamp, timeout_message, timestamp, timestamp),
                    ).lastrowid

                with patch.object(main, "tag_styling_garment_with_gemini", new=AsyncMock(return_value=tagging_result)):
                    asyncio.run(main.run_styling_garment_tagging(int(garment_id)))
                main.finish_attempt(int(attempt_id), "SUCCEEDED", {"normalized": tagging_result})

                with db_module.get_db() as db:
                    garment = db.execute(
                        "SELECT status, tag_error, tag_completed_at FROM styling_garments WHERE id = ?",
                        (garment_id,),
                    ).fetchone()
                    attempt = db.execute(
                        "SELECT status, error_code, error_message, failed_at FROM generation_attempts WHERE id = ?",
                        (attempt_id,),
                    ).fetchone()
            config.get_settings.cache_clear()

        self.assertEqual(garment["status"], "READY")
        self.assertIsNone(garment["tag_error"])
        self.assertIsNotNone(garment["tag_completed_at"])
        self.assertEqual(attempt["status"], "SUCCEEDED")
        self.assertIsNone(attempt["error_code"])
        self.assertIsNone(attempt["error_message"])
        self.assertIsNone(attempt["failed_at"])

    def test_migration_clears_existing_successful_tagging_timeout_errors(self) -> None:
        db_module = importlib.import_module("app.db")

        timeout_message = "打标任务长时间未完成，已自动停止轮询，可手动重新打标。"
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            conn = db_module.sqlite3.connect(db_file.name)
            conn.row_factory = db_module.sqlite3.Row
            conn.executescript(
                """
                CREATE TABLE styling_garments (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  code TEXT NOT NULL,
                  status TEXT NOT NULL,
                  tag_completed_at TEXT,
                  tag_error TEXT
                );
                CREATE TABLE generation_attempts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  owner_type TEXT NOT NULL,
                  owner_id INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  error_code TEXT,
                  error_message TEXT,
                  failed_at TEXT
                );
                """
            )
            conn.execute(
                """
                INSERT INTO styling_garments (code, status, tag_completed_at, tag_error)
                VALUES ('CLEAN-ME', 'READY', '2026-05-26T09:53:42Z', ?)
                """,
                (timeout_message,),
            )
            conn.execute(
                """
                INSERT INTO generation_attempts
                  (owner_type, owner_id, status, error_code, error_message, failed_at)
                VALUES ('styling_garment_tagging', 1, 'SUCCEEDED', 'TIMEOUT', ?, '2026-05-26T09:52:11Z')
                """,
                (timeout_message,),
            )
            db_module.migration_clear_successful_tagging_timeout_errors(conn)
            garment = conn.execute("SELECT tag_error FROM styling_garments WHERE code = 'CLEAN-ME'").fetchone()
            attempt = conn.execute("SELECT error_code, error_message, failed_at FROM generation_attempts WHERE owner_id = 1").fetchone()
            conn.close()

        self.assertIsNone(garment["tag_error"])
        self.assertIsNone(attempt["error_code"])
        self.assertIsNone(attempt["error_message"])
        self.assertIsNone(attempt["failed_at"])

    def test_lily_tagging_normalization_filters_invalid_enums(self) -> None:
        main = importlib.import_module("app.main")
        garment = {"code": "TEST", "images": [{"id": 1}]}

        normalized = main.normalize_styling_tags(
            {
                "category": "shoe",
                "sub_category": "bag",
                "colors": {"primary": "cyan", "secondary": ["white", "neon"], "temperature": "hot"},
                "materials_visual": ["denim", "plastic"],
                "fit": "giant",
                "patterns": ["solid", "paisley"],
                "styling_compatibility": {
                    "style_risk": ["none", "too_gothic"],
                    "pairing_brief": "适合搭配浅色直筒裤，保持清爽通勤感，不要过长",
                },
            },
            garment,
        )

        self.assertEqual(normalized["category"], "unknown")
        self.assertEqual(normalized["sub_category"], "other")
        self.assertEqual(normalized["colors"]["primary"], "unknown")
        self.assertEqual(normalized["colors"]["secondary"], ["white"])
        self.assertEqual(normalized["materials_visual"], ["denim"])
        self.assertEqual(normalized["fit"], "unknown")
        self.assertEqual(normalized["patterns"], ["solid"])
        self.assertEqual(normalized["styling_compatibility"]["style_risk"], ["none"])
        self.assertLessEqual(len(normalized["styling_compatibility"]["pairing_brief"]), 40)

    def test_lily_tagging_json_parser_recovers_trailing_brace(self) -> None:
        main = importlib.import_module("app.main")

        parsed = main.parse_lily_tagging_json('{"schema_version":"lily-garment-tags-v1","ai_tags":{"category":"outerwear"}}\n\n}')

        self.assertEqual(parsed["schema_version"], "lily-garment-tags-v1")
        self.assertEqual(parsed["ai_tags"]["category"], "outerwear")

    def test_lily_tagging_json_parser_rejects_non_json_trailing_text(self) -> None:
        main = importlib.import_module("app.main")

        with self.assertRaises(Exception):
            main.parse_lily_tagging_json('{"schema_version":"lily-garment-tags-v1"}\nexplanation}')

    def test_lily_tagging_review_results_do_not_enter_ready(self) -> None:
        main = importlib.import_module("app.main")

        ready_meta = main.apply_tag_review_policy(
            {"category": "top"},
            {"tagger_provider": "gemini", "overall_confidence": 0.9, "needs_review": False, "review_reasons": []},
        )
        review_meta = main.apply_tag_review_policy(
            {"category": "unsupported_accessory"},
            {"tagger_provider": "gemini", "overall_confidence": 0.9, "needs_review": False, "review_reasons": []},
        )
        fallback_meta = main.apply_tag_review_policy(
            {"category": "top"},
            {"tagger_provider": "fallback", "overall_confidence": 0.5, "needs_review": True, "review_reasons": ["fallback_tags"]},
        )

        self.assertEqual(main.tagging_status_for_result({"category": "top"}, ready_meta), "READY")
        self.assertEqual(main.tagging_status_for_result({"category": "unsupported_accessory"}, review_meta), "TAGGING_READY")
        self.assertIn("unsupported_accessory", review_meta["review_reasons"])
        self.assertEqual(main.tagging_status_for_result({"category": "top"}, fallback_meta), "TAGGING_READY")

    def test_generation_inputs_use_selected_material_snapshot_after_archive(self) -> None:
        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            timestamp = db_module.now_iso()
            snapshot = [{
                "id": 99,
                "code": "ARCHIVED-STYLING",
                "images": [{"asset": {"id": 901, "objectKey": "styling/snapshot.png", "filename": "snapshot.png", "originalUrl": "/api/media?key=styling/snapshot.png&variant=original"}}],
            }]
            hero_snapshot = [{
                "sortOrder": 0,
                "asset": {"id": 902, "objectKey": "hero/frozen.png", "filename": "frozen.png", "originalUrl": "/api/media?key=hero/frozen.png&variant=original"},
            }]
            model_snapshot = {
                "imageInputs": [{"role": "model_identity", "objectKey": "models/frozen.png", "filename": "frozen-model.png", "angle": "front"}],
            }
            with db_module.get_db() as db:
                task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('snapshot', 'STYLING_READY', ?, ?)",
                    (timestamp, timestamp),
                ).lastrowid
                input_id = db.execute(
                    """
                    INSERT INTO generation_task_input_versions
                      (task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at)
                    VALUES (?, 1, 'snapshot', ?, ?, ?)
                    """,
                    (task_id, db_module.dumps(hero_snapshot), db_module.dumps(model_snapshot), timestamp),
                ).lastrowid
                styling_id = db.execute(
                    """
                    INSERT INTO generation_stylings
                      (task_id, input_version_id, source_material_ids_json, selected_material_ids_json,
                       selected_material_snapshot_json, status, created_at, updated_at)
                    VALUES (?, ?, '[99]', '[99]', ?, 'SUCCEEDED', ?, ?)
                    """,
                    (task_id, input_id, db_module.dumps(snapshot), timestamp, timestamp),
                ).lastrowid

            inputs = main.build_new_image_inputs(int(task_id), int(styling_id), include_preview=False)

        hero_inputs = [item for item in inputs if item["role"] == "hero_garment"]
        styling_inputs = [item for item in inputs if item["role"] == "styling_garment"]
        model_inputs = [item for item in inputs if item["role"] == "model_identity"]
        self.assertEqual(hero_inputs[0]["objectKey"], "hero/frozen.png")
        self.assertEqual(styling_inputs[0]["objectKey"], "styling/snapshot.png")
        self.assertEqual(model_inputs[0]["objectKey"], "models/frozen.png")

    def test_generation_task_freezes_draft_before_ai_and_rejects_running_reruns(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            timestamp = db_module.now_iso()
            with db_module.get_db() as db:
                task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('draft', 'DRAFT', ?, ?)",
                    (timestamp, timestamp),
                ).lastrowid
                asset_id = db.execute(
                    """
                    INSERT INTO media_assets (object_key, original_filename, mime_type, status, created_at, updated_at)
                    VALUES ('hero/draft.png', 'draft.png', 'image/png', 'READY', ?, ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                db.execute(
                    """
                    INSERT INTO generation_task_images (task_id, media_asset_id, sort_order, status, created_at, updated_at)
                    VALUES (?, ?, 0, 'READY', ?, ?)
                    """,
                    (task_id, asset_id, timestamp, timestamp),
                )

            client = TestClient(main.app)
            self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)
            draft_record_ids = [item["id"] for item in client.get("/api/generation-records").json()["items"]]
            self.assertIn(task_id, draft_record_ids)
            ready = client.post(f"/api/generation-tasks/{task_id}/ready")
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json()["item"]["status"], "READY_FOR_STYLING")
            self.assertEqual(client.patch(f"/api/generation-tasks/{task_id}", json={"name": "changed"}).status_code, 409)

            with db_module.get_db() as db:
                db.execute("UPDATE generation_tasks SET status = 'STYLING_RUNNING' WHERE id = ?", (task_id,))
            self.assertEqual(client.post(f"/api/generation-tasks/{task_id}/select-styling").status_code, 409)
            with db_module.get_db() as db:
                db.execute("UPDATE generation_tasks SET status = 'PREVIEW_RUNNING' WHERE id = ?", (task_id,))
            self.assertEqual(client.post(f"/api/generation-tasks/{task_id}/preview").status_code, 409)
            with db_module.get_db() as db:
                db.execute("UPDATE generation_tasks SET status = 'FINAL_RUNNING', active_preview_id = 1 WHERE id = ?", (task_id,))
            self.assertEqual(client.post(f"/api/generation-tasks/{task_id}/generate-final").status_code, 409)

    def test_user_lists_default_to_created_at_desc_even_after_updates(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.Settings.database_path = Path(db_file.name)
            config.get_settings.cache_clear()
            db_module.get_settings = config.get_settings
            main.settings = config.get_settings()
            db_module.init_db()
            older_created = "2026-01-01T00:00:00"
            newer_created = "2026-01-02T00:00:00"
            older_updated = "2026-01-01T01:00:00"
            newer_updated = "2026-01-03T00:00:00"
            with db_module.get_db() as db:
                older_task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('older task', 'DRAFT', ?, ?)",
                    (older_created, newer_updated),
                ).lastrowid
                newer_task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('newer task', 'DRAFT', ?, ?)",
                    (newer_created, older_updated),
                ).lastrowid
                older_garment_id = db.execute(
                    """
                    INSERT INTO styling_garments (code, status, manual_tags_json, ai_tags_json, created_at, updated_at)
                    VALUES ('OLDER-GARMENT', 'READY', '{}', '{}', ?, ?)
                    """,
                    (older_created, newer_updated),
                ).lastrowid
                newer_garment_id = db.execute(
                    """
                    INSERT INTO styling_garments (code, status, manual_tags_json, ai_tags_json, created_at, updated_at)
                    VALUES ('NEWER-GARMENT', 'READY', '{}', '{}', ?, ?)
                    """,
                    (newer_created, older_updated),
                ).lastrowid

            client = TestClient(main.app)
            self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)

            task_ids = [item["id"] for item in client.get("/api/generation-tasks").json()["items"]]
            record_ids = [item["id"] for item in client.get("/api/generation-records").json()["items"]]
            garment_ids = [item["id"] for item in client.get("/api/styling-garments").json()["items"]]
            self.assertLess(task_ids.index(newer_task_id), task_ids.index(older_task_id))
            self.assertLess(record_ids.index(newer_task_id), record_ids.index(older_task_id))
            self.assertLess(garment_ids.index(newer_garment_id), garment_ids.index(older_garment_id))

    def test_generation_task_archive_and_delete_hide_default_lists(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            timestamp = db_module.now_iso()
            with db_module.get_db() as db:
                archived_task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('archive me', 'STYLING_READY', ?, ?)",
                    (timestamp, timestamp),
                ).lastrowid
                batch_archived_task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('batch archive me', 'STYLING_READY', ?, ?)",
                    (timestamp, timestamp),
                ).lastrowid
                deleted_task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('delete me', 'STYLING_READY', ?, ?)",
                    (timestamp, timestamp),
                ).lastrowid
                batch_deleted_task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('batch delete me', 'STYLING_READY', ?, ?)",
                    (timestamp, timestamp),
                ).lastrowid
                for task_id, name in (
                    (archived_task_id, "archive me"),
                    (batch_archived_task_id, "batch archive me"),
                    (deleted_task_id, "delete me"),
                    (batch_deleted_task_id, "batch delete me"),
                ):
                    db.execute(
                        """
                        INSERT INTO generation_task_input_versions (task_id, version_number, name_snapshot, image_snapshot_json, created_at)
                        VALUES (?, 1, ?, '[]', ?)
                        """,
                        (task_id, name, timestamp),
                    )
                deleted_media_ids: list[int] = []
                for task_id, key in (
                    (deleted_task_id, "generation-tasks/delete-me/source.jpg"),
                    (batch_deleted_task_id, "generation-tasks/batch-delete-me/source.jpg"),
                ):
                    media_id = db.execute(
                        """
                        INSERT INTO media_assets (object_key, original_filename, mime_type, status, created_at, updated_at)
                        VALUES (?, 'source.jpg', 'image/jpeg', 'READY', ?, ?)
                        """,
                        (key, timestamp, timestamp),
                    ).lastrowid
                    deleted_media_ids.append(media_id)
                    db.execute(
                        """
                        INSERT INTO generation_task_images (task_id, media_asset_id, sort_order, status, created_at, updated_at)
                        VALUES (?, ?, 0, 'READY', ?, ?)
                        """,
                        (task_id, media_id, timestamp, timestamp),
                    )

            client = TestClient(main.app)
            self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)

            archived = client.post(f"/api/generation-tasks/{archived_task_id}/archive")
            self.assertEqual(archived.status_code, 200)
            self.assertIsNotNone(archived.json()["item"]["archivedAt"])
            batch_archived = client.post("/api/generation-tasks/batch-archive", json={"ids": [batch_archived_task_id]})
            self.assertEqual(batch_archived.status_code, 200)
            default_record_ids = [item["id"] for item in client.get("/api/generation-records").json()["items"]]
            self.assertNotIn(archived_task_id, default_record_ids)
            self.assertNotIn(batch_archived_task_id, default_record_ids)
            archived_record_ids = [item["id"] for item in client.get("/api/generation-records?includeArchived=true").json()["items"]]
            self.assertIn(archived_task_id, archived_record_ids)
            self.assertIn(batch_archived_task_id, archived_record_ids)

            missing_purge = client.request("DELETE", f"/api/generation-tasks/{deleted_task_id}", json={"confirmName": "delete me"})
            self.assertEqual(missing_purge.status_code, 400)
            bad_delete = client.request("DELETE", f"/api/generation-tasks/{deleted_task_id}", json={"purge": True, "confirmName": "wrong"})
            self.assertEqual(bad_delete.status_code, 400)
            bad_batch_delete = client.post("/api/generation-tasks/batch-purge", json={"purge": True, "ids": [batch_deleted_task_id], "confirmPhrase": "wrong"})
            self.assertEqual(bad_batch_delete.status_code, 400)
            impact = client.post("/api/generation-tasks/delete-impact", json={"ids": [deleted_task_id, batch_deleted_task_id]})
            self.assertEqual(impact.status_code, 200)
            self.assertEqual(impact.json()["impact"]["taskCount"], 2)
            self.assertEqual(impact.json()["impact"]["mediaAssetCount"], 2)
            with (
                patch.object(main, "delete_object", return_value=None) as delete_object,
                patch.object(main, "delete_prefix", return_value=None),
            ):
                deleted = client.request("DELETE", f"/api/generation-tasks/{deleted_task_id}", json={"purge": True, "confirmName": "delete me"})
                self.assertEqual(deleted.status_code, 200)
                self.assertTrue(deleted.json()["purged"])
                batch_deleted = client.post("/api/generation-tasks/batch-purge", json={"purge": True, "ids": [batch_deleted_task_id], "confirmPhrase": "全部删除"})
                self.assertEqual(batch_deleted.status_code, 200)
                self.assertEqual(batch_deleted.json()["deletedTaskIds"], [batch_deleted_task_id])
                self.assertGreaterEqual(delete_object.call_count, 2)
            default_task_ids = [item["id"] for item in client.get("/api/generation-tasks").json()["items"]]
            self.assertNotIn(deleted_task_id, default_task_ids)
            self.assertNotIn(batch_deleted_task_id, default_task_ids)
            self.assertEqual(client.get(f"/api/generation-tasks/{deleted_task_id}").status_code, 404)
            self.assertEqual(client.get(f"/api/generation-tasks/{batch_deleted_task_id}").status_code, 404)
            with db_module.get_db() as db:
                media_count = db.execute(
                    f"SELECT COUNT(*) AS count FROM media_assets WHERE id IN ({', '.join('?' for _ in deleted_media_ids)})",
                    deleted_media_ids,
                ).fetchone()["count"]
            self.assertEqual(media_count, 0)

    def test_retry_failed_new_result_image_marks_parent_running_before_background_job(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            task_id, result_set_id, image_ids = self._create_new_final_result_set(
                db_module,
                ["SUCCEEDED", "FAILED"],
                task_status="FINAL_PARTIAL",
                result_set_status="PARTIAL",
            )

            client = TestClient(main.app)
            self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)

            def close_background_coroutine(coro: object, job_key: str | None = None) -> None:
                del job_key
                coro.close()

            with patch.object(main, "schedule_background_generation", side_effect=close_background_coroutine) as schedule:
                response = client.post(f"/api/generation-result-images-new/{image_ids[1]}/retry")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["item"]["status"], "FINAL_RUNNING")
            self.assertEqual(schedule.call_args.kwargs["job_key"], main.final_set_job_key(result_set_id))
            with db_module.get_db() as db:
                task = db.execute("SELECT status, active_result_set_id FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
                result_set = db.execute("SELECT status FROM generation_result_sets WHERE id = ?", (result_set_id,)).fetchone()
                image = db.execute("SELECT status, active_attempt_id, locked_until FROM generation_result_images_new WHERE id = ?", (image_ids[1],)).fetchone()
            self.assertEqual(task["status"], "FINAL_RUNNING")
            self.assertEqual(task["active_result_set_id"], result_set_id)
            self.assertEqual(result_set["status"], "RUNNING")
            self.assertEqual(image["status"], "QUEUED")
            self.assertIsNone(image["active_attempt_id"])
            self.assertIsNone(image["locked_until"])

    def test_new_generation_recovery_reconciles_running_result_sets_with_terminal_children(self) -> None:
        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            completed_task_id, completed_result_set_id, _completed_images = self._create_new_final_result_set(
                db_module,
                ["SUCCEEDED", "SUCCEEDED"],
            )
            partial_task_id, partial_result_set_id, _partial_images = self._create_new_final_result_set(
                db_module,
                ["SUCCEEDED", "FAILED"],
            )
            half_reconciled_task_id, half_reconciled_result_set_id, _half_reconciled_images = self._create_new_final_result_set(
                db_module,
                ["SUCCEEDED", "SUCCEEDED"],
                task_status="FINAL_RUNNING",
                result_set_status="SUCCEEDED",
            )

            def close_background_coroutine(coro: object, job_key: str | None = None) -> None:
                del job_key
                coro.close()

            with patch.object(main, "schedule_background_generation", side_effect=close_background_coroutine):
                main.refresh_pending_new_generation_jobs()

            with db_module.get_db() as db:
                completed_task = db.execute("SELECT status FROM generation_tasks WHERE id = ?", (completed_task_id,)).fetchone()
                completed_set = db.execute("SELECT status FROM generation_result_sets WHERE id = ?", (completed_result_set_id,)).fetchone()
                partial_task = db.execute("SELECT status FROM generation_tasks WHERE id = ?", (partial_task_id,)).fetchone()
                partial_set = db.execute("SELECT status FROM generation_result_sets WHERE id = ?", (partial_result_set_id,)).fetchone()
                half_reconciled_task = db.execute("SELECT status FROM generation_tasks WHERE id = ?", (half_reconciled_task_id,)).fetchone()
                half_reconciled_set = db.execute("SELECT status FROM generation_result_sets WHERE id = ?", (half_reconciled_result_set_id,)).fetchone()

            self.assertEqual(completed_task["status"], "COMPLETED")
            self.assertEqual(completed_set["status"], "SUCCEEDED")
            self.assertEqual(partial_task["status"], "FINAL_PARTIAL")
            self.assertEqual(partial_set["status"], "PARTIAL")
            self.assertEqual(half_reconciled_task["status"], "COMPLETED")
            self.assertEqual(half_reconciled_set["status"], "SUCCEEDED")

    def test_purge_styling_garment_removes_tasks_that_selected_it(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            timestamp = db_module.now_iso()
            code = f"STYLING-PURGE-{os.urandom(5).hex()}"
            archive_code = f"STYLING-ARCHIVE-{os.urandom(5).hex()}"
            batch_archive_code = f"STYLING-BATCH-ARCHIVE-{os.urandom(5).hex()}"
            batch_purge_code = f"STYLING-BATCH-PURGE-{os.urandom(5).hex()}"
            with db_module.get_db() as db:
                garment_id = db.execute(
                    """
                    INSERT INTO styling_garments (code, status, manual_tags_json, ai_tags_json, created_at, updated_at)
                    VALUES (?, 'READY', '{}', '{}', ?, ?)
                    """,
                    (code, timestamp, timestamp),
                ).lastrowid
                archive_garment_id = db.execute(
                    """
                    INSERT INTO styling_garments (code, status, manual_tags_json, ai_tags_json, created_at, updated_at)
                    VALUES (?, 'READY', '{}', '{}', ?, ?)
                    """,
                    (archive_code, timestamp, timestamp),
                ).lastrowid
                batch_archive_garment_id = db.execute(
                    """
                    INSERT INTO styling_garments (code, status, manual_tags_json, ai_tags_json, created_at, updated_at)
                    VALUES (?, 'READY', '{}', '{}', ?, ?)
                    """,
                    (batch_archive_code, timestamp, timestamp),
                ).lastrowid
                batch_purge_garment_id = db.execute(
                    """
                    INSERT INTO styling_garments (code, status, manual_tags_json, ai_tags_json, created_at, updated_at)
                    VALUES (?, 'READY', '{}', '{}', ?, ?)
                    """,
                    (batch_purge_code, timestamp, timestamp),
                ).lastrowid
                task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('dependent', 'STYLING_READY', ?, ?)",
                    (timestamp, timestamp),
                ).lastrowid
                hero_asset_id = db.execute(
                    """
                    INSERT INTO media_assets (object_key, original_filename, mime_type, status, created_at, updated_at)
                    VALUES ('generation-tasks/dependent-abc123/hero.png', 'hero.png', 'image/png', 'READY', ?, ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                db.execute(
                    """
                    INSERT INTO generation_task_images (task_id, media_asset_id, sort_order, status, created_at, updated_at)
                    VALUES (?, ?, 0, 'READY', ?, ?)
                    """,
                    (task_id, hero_asset_id, timestamp, timestamp),
                )
                input_id = db.execute(
                    """
                    INSERT INTO generation_task_input_versions (task_id, version_number, name_snapshot, image_snapshot_json, created_at)
                    VALUES (?, 1, 'dependent', '[]', ?)
                    """,
                    (task_id, timestamp),
                ).lastrowid
                db.execute(
                    """
                    INSERT INTO generation_stylings
                      (task_id, input_version_id, source_material_ids_json, selected_material_ids_json,
                       selected_material_snapshot_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '[]', 'SUCCEEDED', ?, ?)
                    """,
                    (task_id, input_id, db_module.dumps([garment_id]), db_module.dumps([garment_id]), timestamp, timestamp),
                )
                output_asset_id = db.execute(
                    """
                    INSERT INTO media_assets (object_key, original_filename, mime_type, status, created_at, updated_at)
                    VALUES ('generation-tasks/dependent/output.png', 'output.png', 'image/png', 'READY', ?, ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                db.execute(
                    """
                    INSERT INTO generation_attempts
                      (owner_type, owner_id, provider, status, output_asset_id, created_at, updated_at)
                    VALUES ('generation_styling', ?, 'test', 'SUCCEEDED', ?, ?, ?)
                    """,
                    (task_id, output_asset_id, timestamp, timestamp),
                )
                db.execute(
                    """
                    INSERT INTO generation_attempts
                      (owner_type, owner_id, provider, status, created_at, updated_at)
                    VALUES ('generation_styling', ?, 'test', 'FAILED', ?, ?)
                    """,
                    (task_id, timestamp, timestamp),
                )

            client = TestClient(main.app)
            self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)
            archived = client.delete(f"/api/styling-garments/{archive_garment_id}")
            self.assertEqual(archived.status_code, 200)
            batch_archived = client.post("/api/styling-garments/batch-delete", json={"ids": [batch_archive_garment_id]})
            self.assertEqual(batch_archived.status_code, 200)
            default_garment_ids = [item["id"] for item in client.get("/api/styling-garments").json()["items"]]
            self.assertNotIn(archive_garment_id, default_garment_ids)
            self.assertNotIn(batch_archive_garment_id, default_garment_ids)
            archived_garment_ids = [item["id"] for item in client.get("/api/styling-garments?includeArchived=true").json()["items"]]
            self.assertIn(archive_garment_id, archived_garment_ids)
            self.assertIn(batch_archive_garment_id, archived_garment_ids)
            bad_batch_purge = client.post("/api/styling-garments/batch-delete", json={"ids": [batch_purge_garment_id], "purge": True, "confirmPhrase": "wrong"})
            self.assertEqual(bad_batch_purge.status_code, 400)
            impact = client.post("/api/styling-garments/delete-impact", json={"ids": [garment_id]})
            self.assertEqual(impact.status_code, 200)
            self.assertEqual(impact.json()["impact"]["dependentTaskCount"], 1)
            self.assertEqual(impact.json()["impact"]["mediaAssetCount"], 2)
            batch_purged = client.post("/api/styling-garments/batch-delete", json={"ids": [batch_purge_garment_id], "purge": True, "confirmPhrase": "全部删除"})
            self.assertEqual(batch_purged.status_code, 200)
            self.assertEqual(batch_purged.json()["deletedGarmentIds"], [batch_purge_garment_id])
            with (
                patch.object(main, "cleanup_objects") as cleanup_objects,
                patch.object(main, "cleanup_prefixes", return_value=[]),
            ):
                purged = client.delete(f"/api/styling-garments/{garment_id}?purge=true&confirmCode={code}")

            self.assertEqual(purged.status_code, 200)
            self.assertEqual(purged.json()["deletedTaskIds"], [task_id])
            removed_keys = cleanup_objects.call_args.args[0]
            self.assertIn("generation-tasks/dependent-abc123/hero.png", removed_keys)
            with db_module.get_db() as db:
                task_count = db.execute("SELECT COUNT(*) AS count FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()["count"]
                attempt_count = db.execute("SELECT COUNT(*) AS count FROM generation_attempts WHERE owner_id = ?", (task_id,)).fetchone()["count"]
                output_asset_count = db.execute("SELECT COUNT(*) AS count FROM media_assets WHERE id = ?", (output_asset_id,)).fetchone()["count"]
                hero_asset_count = db.execute("SELECT COUNT(*) AS count FROM media_assets WHERE id = ?", (hero_asset_id,)).fetchone()["count"]
                batch_purge_garment_count = db.execute("SELECT COUNT(*) AS count FROM styling_garments WHERE id = ?", (batch_purge_garment_id,)).fetchone()["count"]

        self.assertEqual(task_count, 0)
        self.assertEqual(attempt_count, 0)
        self.assertEqual(output_asset_count, 0)
        self.assertEqual(hero_asset_count, 0)
        self.assertEqual(batch_purge_garment_count, 0)

    def test_styling_garment_image_delete_releases_only_unreferenced_media(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            timestamp = db_module.now_iso()
            code = f"IMG-CLEANUP-{os.urandom(5).hex()}"
            with db_module.get_db() as db:
                garment_id = db.execute(
                    """
                    INSERT INTO styling_garments (code, status, manual_tags_json, ai_tags_json, created_at, updated_at)
                    VALUES (?, 'READY', '{}', '{}', ?, ?)
                    """,
                    (code, timestamp, timestamp),
                ).lastrowid
                asset_ids: list[int] = []
                link_ids: list[int] = []
                for index, key in enumerate(("styling-garments/img-cleanup/a.jpg", "styling-garments/img-cleanup/b.jpg", "styling-garments/img-cleanup/c.jpg")):
                    asset_id = db.execute(
                        """
                        INSERT INTO media_assets (object_key, original_filename, mime_type, status, created_at, updated_at)
                        VALUES (?, ?, 'image/jpeg', 'READY', ?, ?)
                        """,
                        (key, Path(key).name, timestamp, timestamp),
                    ).lastrowid
                    asset_ids.append(asset_id)
                    link_ids.append(db.execute(
                        """
                        INSERT INTO styling_garment_images
                          (styling_garment_id, media_asset_id, sort_order, status, created_at, updated_at)
                        VALUES (?, ?, ?, 'READY', ?, ?)
                        """,
                        (garment_id, asset_id, index, timestamp, timestamp),
                    ).lastrowid)
                task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('snapshot holder', 'STYLING_READY', ?, ?)",
                    (timestamp, timestamp),
                ).lastrowid
                input_id = db.execute(
                    """
                    INSERT INTO generation_task_input_versions (task_id, version_number, name_snapshot, image_snapshot_json, created_at)
                    VALUES (?, 1, 'snapshot holder', '[]', ?)
                    """,
                    (task_id, timestamp),
                ).lastrowid
                db.execute(
                    """
                    INSERT INTO generation_stylings
                      (task_id, input_version_id, source_material_ids_json, selected_material_ids_json,
                       selected_material_snapshot_json, status, created_at, updated_at)
                    VALUES (?, ?, '[]', '[]', ?, 'SUCCEEDED', ?, ?)
                    """,
                    (task_id, input_id, db_module.dumps([{"asset": {"objectKey": "styling-garments/img-cleanup/b.jpg"}}]), timestamp, timestamp),
                )

            client = TestClient(main.app)
            self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)
            with patch.object(main, "delete_object", return_value=None) as delete_object:
                unreferenced = client.delete(f"/api/styling-garment-images/{link_ids[0]}")
                referenced = client.delete(f"/api/styling-garment-images/{link_ids[1]}")

            self.assertEqual(unreferenced.status_code, 200)
            self.assertEqual(referenced.status_code, 200)
            removed_keys = [call.args[0] for call in delete_object.call_args_list]
            self.assertIn("styling-garments/img-cleanup/a.jpg", removed_keys)
            self.assertNotIn("styling-garments/img-cleanup/b.jpg", removed_keys)
            with db_module.get_db() as db:
                removed_asset_count = db.execute("SELECT COUNT(*) AS count FROM media_assets WHERE id = ?", (asset_ids[0],)).fetchone()["count"]
                preserved_asset_count = db.execute("SELECT COUNT(*) AS count FROM media_assets WHERE id = ?", (asset_ids[1],)).fetchone()["count"]
                remaining_link_count = db.execute("SELECT COUNT(*) AS count FROM styling_garment_images WHERE styling_garment_id = ?", (garment_id,)).fetchone()["count"]
            self.assertEqual(removed_asset_count, 0)
            self.assertEqual(preserved_asset_count, 1)
            self.assertEqual(remaining_link_count, 1)

    def test_generation_task_draft_image_delete_and_replace_release_old_media(self) -> None:
        from fastapi.testclient import TestClient
        from PIL import Image

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            timestamp = db_module.now_iso()
            with db_module.get_db() as db:
                task_id = db.execute(
                    "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES ('draft cleanup', 'DRAFT', ?, ?)",
                    (timestamp, timestamp),
                ).lastrowid
                asset_ids: list[int] = []
                link_ids: list[int] = []
                for index, key in enumerate(("generation-tasks/draft-cleanup/a.jpg", "generation-tasks/draft-cleanup/b.jpg")):
                    asset_id = db.execute(
                        """
                        INSERT INTO media_assets (object_key, original_filename, mime_type, status, created_at, updated_at)
                        VALUES (?, ?, 'image/jpeg', 'READY', ?, ?)
                        """,
                        (key, Path(key).name, timestamp, timestamp),
                    ).lastrowid
                    asset_ids.append(asset_id)
                    link_ids.append(db.execute(
                        """
                        INSERT INTO generation_task_images (task_id, media_asset_id, sort_order, status, created_at, updated_at)
                        VALUES (?, ?, ?, 'READY', ?, ?)
                        """,
                        (task_id, asset_id, index, timestamp, timestamp),
                    ).lastrowid)

            image_bytes = BytesIO()
            Image.new("RGB", (16, 16), "white").save(image_bytes, format="JPEG")
            upload = {"file": ("replacement.jpg", image_bytes.getvalue(), "image/jpeg")}

            client = TestClient(main.app)
            self.assertEqual(client.post("/api/auth/login", json={"password": "letmein-123456"}).status_code, 200)
            with (
                patch.object(main, "delete_object", return_value=None) as delete_object,
                patch.object(main, "put_fileobj", return_value="generation-tasks/draft-cleanup/replacement.jpg"),
                patch.object(main, "generate_image_derivatives", return_value={"thumb": "generation-tasks/draft-cleanup/_derived/thumb/replacement.webp", "preview": "generation-tasks/draft-cleanup/_derived/preview/replacement.webp"}),
                patch.object(main, "unique_key", side_effect=lambda key: main.clean_key(key)),
            ):
                removed = client.delete(f"/api/generation-task-images/{link_ids[0]}")
                replaced = client.post(f"/api/generation-task-images/{link_ids[1]}/replace", files=upload)

            self.assertEqual(removed.status_code, 200)
            self.assertEqual(replaced.status_code, 200)
            removed_keys = [call.args[0] for call in delete_object.call_args_list]
            self.assertIn("generation-tasks/draft-cleanup/a.jpg", removed_keys)
            self.assertIn("generation-tasks/draft-cleanup/b.jpg", removed_keys)
            with db_module.get_db() as db:
                old_asset_count = db.execute(
                    f"SELECT COUNT(*) AS count FROM media_assets WHERE id IN ({', '.join('?' for _ in asset_ids)})",
                    asset_ids,
                ).fetchone()["count"]
                current_image_count = db.execute("SELECT COUNT(*) AS count FROM generation_task_images WHERE task_id = ?", (task_id,)).fetchone()["count"]
            self.assertEqual(old_asset_count, 0)
            self.assertEqual(current_image_count, 1)

    def test_sku_delete_archives_unless_related_records_are_confirmed_for_deletion(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            timestamp = db_module.now_iso()
            with db_module.get_db() as db:
                model_id = db.execute("SELECT id FROM model_profiles ORDER BY id LIMIT 1").fetchone()["id"]
                sku_id = db.execute(
                    """
                    INSERT INTO garment_skus (sku, title, storage_path, created_at, updated_at)
                    VALUES ('SKU-TEST', 'Test SKU', 'uploads/sku-test', ?, ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                db.execute(
                    """
                    INSERT INTO garment_images (sku_id, filename, storage_path, mime_type, created_at)
                    VALUES (?, 'front.png', 'uploads/sku-test/front.png', 'image/png', ?)
                    """,
                    (sku_id, timestamp),
                )
                shot_plan_id = db.execute(
                    """
                    INSERT INTO shot_plans
                      (sku_id, model_profile_id, status, plan_json, planner_provider, created_at, updated_at)
                    VALUES (?, ?, 'CONFIRMED', '{}', 'test', ?, ?)
                    """,
                    (sku_id, model_id, timestamp, timestamp),
                ).lastrowid
                db.execute(
                    """
                    INSERT INTO generation_runs
                      (sku_id, shot_plan_id, model_profile_id, kind, status, output_dir, archived_at, created_at, updated_at)
                    VALUES (?, ?, ?, 'group', 'SUCCEEDED', 'outputs/sku-test', ?, ?, ?)
                    """,
                    (sku_id, shot_plan_id, model_id, timestamp, timestamp, timestamp),
                )

            client = TestClient(main.app)
            login = client.post("/api/auth/login", json={"password": "letmein-123456"})
            self.assertEqual(login.status_code, 200)
            archived = client.delete(f"/api/garment-skus/{sku_id}")
            self.assertEqual(archived.status_code, 200)
            with db_module.get_db() as db:
                archived_at = db.execute("SELECT archived_at FROM garment_skus WHERE id = ?", (sku_id,)).fetchone()["archived_at"]
            self.assertIsNotNone(archived_at)
            wrong_confirmation = client.delete(f"/api/garment-skus/{sku_id}?purge=true&confirmSku=WRONG")
            self.assertEqual(wrong_confirmation.status_code, 400)
            purged = client.delete(f"/api/garment-skus/{sku_id}?purge=true&confirmSku=SKU-TEST")
            self.assertEqual(purged.status_code, 200)
            with db_module.get_db() as db:
                sku_count = db.execute("SELECT COUNT(*) AS count FROM garment_skus WHERE id = ?", (sku_id,)).fetchone()["count"]
                run_count = db.execute("SELECT COUNT(*) AS count FROM generation_runs WHERE sku_id = ?", (sku_id,)).fetchone()["count"]
                plan_count = db.execute("SELECT COUNT(*) AS count FROM shot_plans WHERE sku_id = ?", (sku_id,)).fetchone()["count"]

        self.assertEqual(sku_count, 0)
        self.assertEqual(run_count, 0)
        self.assertEqual(plan_count, 0)

    def test_planner_parser_recovers_only_complete_json_with_trailing_noise(self) -> None:
        planner = importlib.import_module("app.planner")

        raw = planner_fixture_json() + "}"
        parsed, meta = planner.parse_planner_response(raw, {"model": "test"})
        validated = planner.validate_planner_response(parsed, {"model": "test"}, raw)
        normalized = planner.normalize_plan(validated, "SKU-PLAN")

        self.assertEqual(meta["mode"], "recovered_trailing_noise")
        self.assertEqual(normalized["sku"], "SKU-PLAN")
        self.assertEqual(normalized["category"], "上衣")
        self.assertEqual(len(normalized["shots"]), 5)

    def test_planner_parser_rejects_non_contract_output(self) -> None:
        planner = importlib.import_module("app.planner")

        with self.assertRaisesRegex(planner.PlannerError, "不是可恢复"):
            planner.parse_planner_response(planner_fixture_json() + "not-json", {"model": "test"})

        parsed, _ = planner.parse_planner_response('{"sku":"SKU","shots":[]}', {"model": "test"})
        with self.assertRaisesRegex(planner.PlannerError, "不足 5 个固定图位"):
            planner.validate_planner_response(parsed, {"model": "test"}, '{"sku":"SKU","shots":[]}')

    def test_planner_response_schema_omits_gemini_unsupported_additional_properties(self) -> None:
        planner = importlib.import_module("app.planner")

        def has_additional_properties(value: object) -> bool:
            if isinstance(value, dict):
                return "additionalProperties" in value or any(has_additional_properties(item) for item in value.values())
            if isinstance(value, list):
                return any(has_additional_properties(item) for item in value)
            return False

        self.assertFalse(has_additional_properties(planner.ShotPlanDraft.model_json_schema()))

    def test_final_generation_images_are_final_by_default(self) -> None:
        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        class FakeProvider:
            def output_suffix(self) -> str:
                return ".png"

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            timestamp = db_module.now_iso()
            with db_module.get_db() as db:
                model_id = db.execute("SELECT id FROM model_profiles ORDER BY id LIMIT 1").fetchone()["id"]
                sku_id = db.execute(
                    """
                    INSERT INTO garment_skus (sku, title, storage_path, created_at, updated_at)
                    VALUES ('SKU-FINAL', 'Final SKU', 'uploads/sku-final', ?, ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                shot_plan_id = db.execute(
                    """
                    INSERT INTO shot_plans
                      (sku_id, model_profile_id, status, plan_json, planner_provider, created_at, updated_at)
                    VALUES (?, ?, 'CONFIRMED', '{}', 'test', ?, ?)
                    """,
                    (sku_id, model_id, timestamp, timestamp),
                ).lastrowid
                run_id = db.execute(
                    """
                    INSERT INTO generation_runs
                      (sku_id, shot_plan_id, model_profile_id, kind, status, output_dir, created_at, updated_at)
                    VALUES (?, ?, ?, 'group', 'RUNNING', 'outputs/final', ?, ?)
                    """,
                    (sku_id, shot_plan_id, model_id, timestamp, timestamp),
                ).lastrowid

            preview_id = main.create_generation_image(
                run_id,
                {},
                {"shotIndex": 0, "title": "分镜预览"},
                FakeProvider(),
                "outputs/final",
                task_package_override={"imageInputs": []},
            )
            final_id = main.create_generation_image(
                run_id,
                {},
                {"shotIndex": 1, "title": "全身主视觉"},
                FakeProvider(),
                "outputs/final",
                task_package_override={"imageInputs": []},
            )
            with db_module.get_db() as db:
                preview_final = db.execute("SELECT is_final FROM generation_images WHERE id = ?", (preview_id,)).fetchone()["is_final"]
                final_final = db.execute("SELECT is_final FROM generation_images WHERE id = ?", (final_id,)).fetchone()["is_final"]

        self.assertEqual(preview_final, 0)
        self.assertEqual(final_final, 1)

    def test_export_requires_all_final_group_images_to_succeed(self) -> None:
        from fastapi.testclient import TestClient

        config = importlib.import_module("app.config")
        db_module = importlib.import_module("app.db")
        main = importlib.import_module("app.main")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_file:
            os.environ["APP_DATABASE_PATH"] = db_file.name
            config.get_settings.cache_clear()
            db_module.init_db()
            timestamp = db_module.now_iso()
            with db_module.get_db() as db:
                model_id = db.execute("SELECT id FROM model_profiles ORDER BY id LIMIT 1").fetchone()["id"]
                sku_id = db.execute(
                    """
                    INSERT INTO garment_skus (sku, title, storage_path, created_at, updated_at)
                    VALUES ('SKU-EXPORT', 'Export SKU', 'uploads/sku-export', ?, ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                shot_plan_id = db.execute(
                    """
                    INSERT INTO shot_plans
                      (sku_id, model_profile_id, status, plan_json, planner_provider, created_at, updated_at)
                    VALUES (?, ?, 'CONFIRMED', '{}', 'test', ?, ?)
                    """,
                    (sku_id, model_id, timestamp, timestamp),
                ).lastrowid
                run_id = db.execute(
                    """
                    INSERT INTO generation_runs
                      (sku_id, shot_plan_id, model_profile_id, kind, status, output_dir, created_at, updated_at)
                    VALUES (?, ?, ?, 'group', 'RUNNING', 'outputs/export', ?, ?)
                    """,
                    (sku_id, shot_plan_id, model_id, timestamp, timestamp),
                ).lastrowid
                db.execute(
                    """
                    INSERT INTO generation_images
                      (run_id, shot_index, title, status, output_path, task_package_json, is_final, created_at, updated_at)
                    VALUES
                      (?, 0, '分镜预览', 'SUCCEEDED', 'outputs/export/preview.png', '{}', 0, ?, ?),
                      (?, 1, '全身主视觉', 'SUCCEEDED', 'outputs/export/front.png', '{}', 1, ?, ?),
                      (?, 2, '品类核心展示', 'QUEUED', 'outputs/export/category.png', '{}', 1, ?, ?)
                    """,
                    (run_id, timestamp, timestamp, run_id, timestamp, timestamp, run_id, timestamp, timestamp),
                )

            client = TestClient(main.app)
            login = client.post("/api/auth/login", json={"password": "letmein-123456"})
            self.assertEqual(login.status_code, 200)

            response = client.post(f"/api/exports/runs/{run_id}")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"], "最终组图尚未全部生成成功，不能导出 ZIP")

            with db_module.get_db() as db:
                db.execute(
                    "UPDATE generation_runs SET status = 'SUCCEEDED', updated_at = ? WHERE id = ?",
                    (timestamp, run_id),
                )
                db.execute(
                    "UPDATE generation_images SET status = 'SUCCEEDED', updated_at = ? WHERE run_id = ? AND shot_index = 2",
                    (timestamp, run_id),
                )

            with (
                patch.object(main, "get_bytes", side_effect=lambda key: key.encode("utf-8")),
                patch.object(main, "put_bytes", return_value=None),
            ):
                response = client.post(f"/api/exports/runs/{run_id}")
            self.assertEqual(response.status_code, 200)
            with zipfile.ZipFile(BytesIO(response.content)) as archive:
                self.assertEqual(sorted(archive.namelist()), ["category.png", "front.png"])

def planner_fixture_json() -> str:
    return """
{
  "sku": "SKU-PLAN",
  "modelId": "Face-01",
  "category": "上衣",
  "garment": {
    "category": "上衣",
    "summary": "灰色人字纹马甲",
    "color": "灰色",
    "fabric": "人字纹梭织面料",
    "keyStructures": ["V领", "纽扣门襟"],
    "preserveStrictly": ["人字纹", "纽扣"]
  },
  "brandStyle": {
    "name": "默认棚拍风格",
    "summary": "白/浅灰背景，自然姿态，简洁搭配，商品优先"
  },
  "generationPlan": {
    "salesInfo": "展示马甲结构",
    "lilyTone": "干净通勤",
    "styling": "白裤黑鞋，低调配饰",
    "background": "浅灰白棚拍背景、轻微落地阴影"
  },
  "shots": [
    {
      "shotIndex": 1,
      "englishName": "Full-length hero shot",
      "title": "全身主视觉",
      "task": "先给完整穿着比例",
      "scene": "全身图",
      "cropRange": "头顶到脚尖完整入画",
      "modelScale": "75%-86%",
      "headTreatment": "完整露头",
      "bodyPosition": "正面",
      "poseTone": "稳定站姿",
      "imagePurpose": "展示整体比例"
    },
    {
      "shotIndex": 2,
      "englishName": "Category key view",
      "title": "品类核心展示",
      "task": "突出销售主体",
      "scene": "大腿中部图",
      "cropRange": "头部到大腿中部",
      "modelScale": "80%-90%",
      "headTreatment": "完整露头",
      "bodyPosition": "3/4 斜正",
      "poseTone": "轻扶门襟",
      "imagePurpose": "展示剪裁结构"
    },
    {
      "shotIndex": 3,
      "englishName": "Detail close-up",
      "title": "设计细节近景",
      "task": "解释面料结构",
      "scene": "胸腰近景",
      "cropRange": "下巴以下到腰部",
      "modelScale": "85%-95%",
      "headTreatment": "下巴以下裁切",
      "bodyPosition": "正面",
      "poseTone": "手臂自然入画",
      "imagePurpose": "展示面料和扣子"
    },
    {
      "shotIndex": 4,
      "englishName": "Angle or movement view",
      "title": "身位/动态补充",
      "task": "补充版型",
      "scene": "七分身",
      "cropRange": "头部到膝盖",
      "modelScale": "78%-88%",
      "headTreatment": "完整露头",
      "bodyPosition": "侧身",
      "poseTone": "轻走动",
      "imagePurpose": "展示侧面厚度"
    },
    {
      "shotIndex": 5,
      "englishName": "Standard front color view",
      "title": "标准正面颜色图",
      "task": "稳定清楚平台友好",
      "scene": "臀上图",
      "cropRange": "头部到臀部下方",
      "modelScale": "80%-90%",
      "headTreatment": "完整露头",
      "bodyPosition": "正面",
      "poseTone": "双手自然下垂",
      "imagePurpose": "提供标准正面样貌"
    }
  ]
}
""".strip()


if __name__ == "__main__":
    unittest.main()
