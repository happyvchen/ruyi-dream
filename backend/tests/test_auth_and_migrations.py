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
                template_count = db.execute("SELECT COUNT(*) AS count FROM brand_template_versions").fetchone()["count"]

        self.assertEqual(versions, ["001", "002", "003", "004", "005", "006"])
        self.assertIn("review_status", image_columns)
        self.assertIn("locked_by", image_columns)
        self.assertGreaterEqual(template_count, 1)


if __name__ == "__main__":
    unittest.main()
