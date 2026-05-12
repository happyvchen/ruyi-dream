from __future__ import annotations

import importlib
import os
import tempfile
import unittest
import zipfile
from io import BytesIO
from unittest.mock import patch
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
                sku_columns = {row["name"] for row in db.execute("PRAGMA table_info(garment_skus)").fetchall()}
                template_count = db.execute("SELECT COUNT(*) AS count FROM brand_template_versions").fetchone()["count"]

        self.assertEqual(versions, ["001", "002", "003", "004", "005", "006", "007"])
        self.assertIn("review_status", image_columns)
        self.assertIn("locked_by", image_columns)
        self.assertIn("archived_at", sku_columns)
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
                patch.object(main, "put_fileobj", return_value=None),
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
