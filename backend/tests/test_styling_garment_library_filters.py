"""搭配服装库查询测试：只使用临时数据库，不调用外部 provider。"""
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


class StylingGarmentLibraryFilterTests(unittest.TestCase):
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
        self._seed_garments()
        self.client = TestClient(main.app)
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "user", "password": "letmein-123456"}).status_code, 200)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        config = sys.modules.get("app.config")
        if config is not None:
            config.get_settings.cache_clear()
        self._temp_dir.cleanup()

    def _seed_garments(self) -> None:
        db_module = importlib.import_module("app.db")
        rows = [
            {
                "id": 1,
                "code": "LOOK-TOP-001",
                "status": "READY",
                "ai": {"category": "top", "colors": ["ivory"], "season": "spring", "style_summary": "通勤上装"},
                "manual": {"series": ["ART"], "retail_channel": ["online"]},
                "meta": {"overall_confidence": 0.95, "needs_review": False},
                "created": "2026-07-01T09:00:00",
                "updated": "2026-07-03T09:00:00",
                "tagged": "2026-07-02T09:00:00",
            },
            {
                "id": 2,
                "code": "LOOK-BOTTOM-002",
                "status": "TAGGING_READY",
                "ai": {"category": "bottom", "colors": ["black"], "season": "autumn"},
                "manual": {"series": ["CORE"], "retail_channel": ["offline"]},
                "meta": {"overall_confidence": 0.41, "needs_review": True},
                "created": "2026-07-10T09:00:00",
                "updated": "2026-07-11T09:00:00",
                "tagged": "2026-07-10T10:00:00",
            },
            {
                "id": 3,
                "code": "LOOK-DRESS-003",
                "status": "READY",
                "ai": {"category": "one_piece", "colors": ["red"], "season": "summer"},
                "manual": {"series": ["ART"], "retail_channel": ["online"]},
                "meta": {"overall_confidence": 0.9, "needs_review": False},
                "created": "2026-06-20T09:00:00",
                "updated": "2026-06-22T09:00:00",
                "tagged": "2026-06-20T10:00:00",
            },
            {
                "id": 4,
                "code": "LOOK-ARCHIVED-004",
                "status": "READY",
                "ai": {"category": "outerwear", "colors": ["navy"], "season": "winter"},
                "manual": {"series": ["ARCHIVE"], "retail_channel": ["online"]},
                "meta": {"overall_confidence": 0.88, "needs_review": False},
                "created": "2026-07-12T09:00:00",
                "updated": "2026-07-12T09:00:00",
                "tagged": "2026-07-12T10:00:00",
                "archived": "2026-07-13T09:00:00",
            },
        ]
        with db_module.get_db() as db:
            for item in rows:
                db.execute(
                    """
                    INSERT INTO styling_garments (
                      id, code, status, ai_tags_json, manual_tags_json, tag_meta_json,
                      tag_completed_at, archived_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        item["code"],
                        item["status"],
                        db_module.dumps(item["ai"]),
                        db_module.dumps(item["manual"]),
                        db_module.dumps(item["meta"]),
                        item["tagged"],
                        item.get("archived"),
                        item["created"],
                        item["updated"],
                    ),
                )

    def test_list_returns_dynamic_facets_from_fact_data(self) -> None:
        response = self.client.get("/api/styling-garments?limit=20")
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual(body["total"], 3)
        self.assertIn({"value": "top", "label": "top", "count": 1}, body["facets"]["category"])
        self.assertIn({"value": "ivory", "label": "ivory", "count": 1}, body["facets"]["color"])
        self.assertIn({"value": "ART", "label": "ART", "count": 2}, body["facets"]["series"])
        self.assertIn({"value": "needs_review", "label": "needs_review", "count": 1}, body["facets"]["reviewStatus"])
        self.assertNotIn("outerwear", {item["value"] for item in body["facets"]["category"]})

    def test_filters_by_status_multi_tag_values_and_uploaded_range(self) -> None:
        response = self.client.get(
            "/api/styling-garments",
            params={
                "status": "READY",
                "category": "top,one_piece",
                "series": "ART",
                "channel": "online",
                "uploadedFrom": "2026-06-30",
                "uploadedTo": "2026-07-02",
                "sortBy": "createdAt",
                "sortDir": "asc",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual([item["code"] for item in body["items"]], ["LOOK-TOP-001"])
        self.assertEqual(body["total"], 1)

    def test_accepts_repeated_multi_select_params_and_filters_facets_to_results(self) -> None:
        response = self.client.get(
            "/api/styling-garments?category=top&category=bottom&sortBy=code&sortDir=asc",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual([item["code"] for item in body["items"]], ["LOOK-BOTTOM-002", "LOOK-TOP-001"])
        self.assertEqual({item["value"] for item in body["facets"]["category"]}, {"bottom", "top"})

    def test_sorts_by_upload_time_descending_and_filters_review_status(self) -> None:
        sorted_response = self.client.get("/api/styling-garments?sortBy=createdAt&sortDir=desc")
        self.assertEqual(sorted_response.status_code, 200)
        self.assertEqual([item["code"] for item in sorted_response.json()["items"]], [
            "LOOK-BOTTOM-002",
            "LOOK-TOP-001",
            "LOOK-DRESS-003",
        ])

        review_response = self.client.get("/api/styling-garments?reviewStatus=needs_review")
        self.assertEqual(review_response.status_code, 200)
        self.assertEqual([item["code"] for item in review_response.json()["items"]], ["LOOK-BOTTOM-002"])

    def test_keyword_upload_to_end_of_day_and_archive_visibility(self) -> None:
        keyword_response = self.client.get("/api/styling-garments?keyword=通勤")
        self.assertEqual(keyword_response.status_code, 200)
        self.assertEqual([item["code"] for item in keyword_response.json()["items"]], ["LOOK-TOP-001"])

        end_of_day_response = self.client.get("/api/styling-garments?uploadedTo=2026-07-01")
        self.assertEqual(end_of_day_response.status_code, 200)
        self.assertIn("LOOK-TOP-001", [item["code"] for item in end_of_day_response.json()["items"]])
        self.assertNotIn("LOOK-BOTTOM-002", [item["code"] for item in end_of_day_response.json()["items"]])

        default_response = self.client.get("/api/styling-garments")
        self.assertNotIn("LOOK-ARCHIVED-004", [item["code"] for item in default_response.json()["items"]])

        archived_response = self.client.get("/api/styling-garments?includeArchived=true")
        self.assertIn("LOOK-ARCHIVED-004", [item["code"] for item in archived_response.json()["items"]])


if __name__ == "__main__":
    unittest.main()
