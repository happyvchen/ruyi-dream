from __future__ import annotations

import importlib
import os
import tempfile
import unittest


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


if __name__ == "__main__":
    unittest.main()
