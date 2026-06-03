from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class BackendCurrentFlowTests(unittest.TestCase):
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
            }
        )
        self._reload_settings()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        config = sys.modules.get("app.config")
        if config is not None:
            config.get_settings.cache_clear()
        self._temp_dir.cleanup()

    def _reload_settings(self) -> None:
        config = importlib.import_module("app.config")
        config.get_settings.cache_clear()
        db_module = sys.modules.get("app.db")
        if db_module is not None:
            db_module.get_settings = config.get_settings
        main = sys.modules.get("app.main")
        if main is not None:
            main.settings = config.get_settings()

    def test_init_db_creates_current_schema_and_drops_legacy_tables(self) -> None:
        db_module = importlib.import_module("app.db")
        db_module.init_db()

        with db_module.get_db() as db:
            tables = {
                row["name"]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            applied = {
                row["version"]
                for row in db.execute("SELECT version FROM schema_migrations").fetchall()
            }

        self.assertIn("generation_tasks", tables)
        self.assertIn("generation_stylings", tables)
        self.assertIn("generation_shoot_scripts", tables)
        self.assertIn("generation_result_images_new", tables)
        self.assertIn("prompt_configs", tables)
        self.assertIn("027", applied)
        self.assertFalse(
            {
                "garment_skus",
                "garment_images",
                "shot_plans",
                "shot_plan_shots",
                "generation_runs",
                "generation_images",
                "brand_template_versions",
                "ai_policy_configs",
            }
            & tables
        )

    def test_candidate_retrieval_prompt_uses_compact_cards(self) -> None:
        main = importlib.import_module("app.main")
        material_cards = [
            {
                "id": 42,
                "code": "LOOK-A",
                "manual": {"styling_note": "适合轻通勤", "internal_note": "不应进入 prompt"},
                "tag_facts": {
                    "ai_tags": {"category": "上装", "colors": ["白色"], "materials_visual": ["梭织"]},
                    "effective_tags": {
                        "category": "上装",
                        "colors": ["白色", "米色"],
                        "fit": "合体",
                        "pairing_brief": "清爽短上衣，能服务高腰主推款。",
                    },
                    "manual_tags": {"very_long": "x" * 2000},
                    "official_title_tags": {"title": "不应整包进入 prompt"},
                    "tag_meta": {"confidence": 0.91, "review_reasons": ["人工确认"]},
                },
            }
        ]

        snapshot = main.build_candidate_retrieval_snapshot({"id": 7}, material_cards)
        prompt_cards = main.candidate_retrieval_for_prompt(snapshot)
        prompt_text = json.dumps(prompt_cards, ensure_ascii=False)

        self.assertEqual(prompt_cards[0]["code"], "LOOK-A")
        self.assertEqual(prompt_cards[0]["category"], "上装")
        self.assertEqual(prompt_cards[0]["colors"], ["白色", "米色"])
        self.assertNotIn("id", prompt_cards[0])
        self.assertNotIn("tag_facts", prompt_cards[0])
        self.assertNotIn("ai_tags", prompt_text)
        self.assertNotIn("official_title_tags", prompt_text)
        self.assertLess(len(prompt_text), 600)

    def test_candidate_retrieval_prompt_reads_current_chinese_tag_schema(self) -> None:
        main = importlib.import_module("app.main")
        material_cards = [
            {
                "id": 24,
                "code": "125309C7948510",
                "manual": {},
                "tag_facts": {
                    "ai_tags": {},
                    "effective_tags": {
                        "一级品类": ["连衣裙"],
                        "色彩": ["黑色", "蓝色", "白色"],
                        "面料材质": ["棉感", "挺括面料"],
                        "版型": ["合体", "微宽松"],
                        "廓形": ["A字型"],
                        "长度": ["及膝"],
                        "穿着能力": ["可单穿", "可作内搭"],
                        "风格语义": "摩登通勤，克制利落，适合职场场景。",
                        "风险点": "外搭可能遮挡袖口和裙摆拼接。",
                    },
                    "tag_meta": {"overall_confidence": 0.95, "needs_review": False},
                },
            }
        ]

        snapshot = main.build_candidate_retrieval_snapshot({"id": 7}, material_cards)
        prompt_card = main.candidate_retrieval_for_prompt(snapshot)[0]

        self.assertEqual(prompt_card["category"], "连衣裙")
        self.assertEqual(prompt_card["colors"], ["黑色", "蓝色", "白色"])
        self.assertEqual(prompt_card["materials"], ["棉感", "挺括面料"])
        self.assertEqual(prompt_card["silhouette"], "A字型")
        self.assertEqual(prompt_card["length"], "及膝")
        self.assertEqual(prompt_card["wearing_capabilities"], ["可单穿", "可作内搭"])
        self.assertIn("摩登通勤", prompt_card["style_summary"])
        self.assertEqual(prompt_card["review"]["confidence"], 0.95)

    def test_downstream_prompt_dtos_do_not_carry_full_history(self) -> None:
        main = importlib.import_module("app.main")
        styling_plan = {
            "hero_understanding": {"category": "裤装", "hero_selling_points": ["高腰比例"]},
            "styling_intents": [{"id": "intent_1", "title": "轻通勤"}],
            "selected_outfit_key": "candidate_a",
            "outfit_candidates": [
                {
                    "id": "candidate_a",
                    "selected_material_codes": ["LOOK-A"],
                    "selected_materials": [{"material_id": 42, "material_code": "LOOK-A"}],
                    "generated_accessories": ["低存在感鞋"],
                    "scores": {"aesthetic_score": 0.82},
                },
                {"id": "candidate_b", "selected_material_codes": ["LOOK-B"]},
            ],
            "rejected_candidates": [{"id": "candidate_b", "reason": "太重"}],
            "reason_summary": "LOOK-A 更轻，服务主推裤装。",
        }

        dto = main.styling_result_for_prompt(styling_plan)
        text = json.dumps(dto, ensure_ascii=False)

        self.assertIn("selected_outfit", dto)
        self.assertNotIn("outfit_candidates", dto)
        self.assertNotIn("rejected_candidates", dto)
        self.assertNotIn("candidate_b", text)
        self.assertNotIn("material_id", text)

    def test_legacy_planner_module_is_removed(self) -> None:
        self.assertFalse((BACKEND_DIR / "app" / "planner.py").exists())


if __name__ == "__main__":
    unittest.main()
