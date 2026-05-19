from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import sqlite3
from typing import Any, Iterator

from .config import get_settings
from .object_storage import presigned_get_url


MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("001", "add_generation_image_attempt_columns", "migration_add_generation_image_attempt_columns"),
    ("002", "add_generation_run_archive_columns", "migration_add_generation_run_archive_columns"),
    ("003", "normalize_planning_schema", "migration_normalize_planning_schema"),
    ("004", "add_generation_task_control_columns", "migration_add_generation_task_control_columns"),
    ("005", "add_generation_review_delivery_columns", "migration_add_generation_review_delivery_columns"),
    ("006", "add_brand_template_versions", "migration_add_brand_template_versions"),
    ("007", "add_garment_sku_archive_columns", "migration_add_garment_sku_archive_columns"),
    ("008", "add_media_derivative_metadata", "migration_add_media_derivative_metadata"),
    ("009", "add_lily_tagging_columns", "migration_add_lily_tagging_columns"),
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    return json.loads(value)


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS garment_skus (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sku TEXT NOT NULL,
              title TEXT,
              source TEXT NOT NULL DEFAULT 'upload',
              storage_path TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'NEW',
              analysis_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS garment_images (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sku_id INTEGER NOT NULL REFERENCES garment_skus(id) ON DELETE CASCADE,
              filename TEXT NOT NULL,
	              storage_path TEXT NOT NULL,
	              thumb_path TEXT,
	              preview_path TEXT,
	              file_size INTEGER,
	              thumb_file_size INTEGER,
	              preview_file_size INTEGER,
	              mime_type TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT 'reference',
              sort_order INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS model_profiles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              model_id TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL,
              asset_dir TEXT NOT NULL,
              profile_json TEXT NOT NULL,
              is_default INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shot_plans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sku_id INTEGER NOT NULL REFERENCES garment_skus(id) ON DELETE CASCADE,
              model_profile_id INTEGER NOT NULL REFERENCES model_profiles(id),
              status TEXT NOT NULL DEFAULT 'DRAFT',
              plan_json TEXT NOT NULL,
              category TEXT,
              sales_info TEXT,
              lily_tone TEXT,
              styling TEXT,
              background TEXT,
              planner_provider TEXT NOT NULL,
              raw_response TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shot_plan_shots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              shot_plan_id INTEGER NOT NULL REFERENCES shot_plans(id) ON DELETE CASCADE,
              shot_index INTEGER NOT NULL,
              english_name TEXT,
              title TEXT NOT NULL,
              task TEXT,
              scene TEXT,
              crop_range TEXT,
              model_scale TEXT,
              head_treatment TEXT,
              body_position TEXT,
              pose_tone TEXT,
              image_purpose TEXT,
              shot_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(shot_plan_id, shot_index)
            );

            CREATE TABLE IF NOT EXISTS generation_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sku_id INTEGER NOT NULL REFERENCES garment_skus(id) ON DELETE CASCADE,
              shot_plan_id INTEGER NOT NULL REFERENCES shot_plans(id),
              model_profile_id INTEGER NOT NULL REFERENCES model_profiles(id),
              kind TEXT NOT NULL DEFAULT 'group',
              status TEXT NOT NULL DEFAULT 'CREATED',
              output_dir TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS generation_images (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER NOT NULL REFERENCES generation_runs(id) ON DELETE CASCADE,
              shot_index INTEGER NOT NULL,
              title TEXT NOT NULL,
	              status TEXT NOT NULL DEFAULT 'QUEUED',
	              output_path TEXT,
	              thumb_path TEXT,
	              preview_path TEXT,
	              file_size INTEGER,
	              thumb_file_size INTEGER,
	              preview_file_size INTEGER,
	              task_package_json TEXT NOT NULL,
              provider_request_json TEXT,
              provider_response_json TEXT,
              regenerate_from_image_id INTEGER REFERENCES generation_images(id),
              regenerate_reason TEXT,
              is_final INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS event_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              entity_type TEXT NOT NULL,
              entity_id INTEGER,
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS media_assets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              object_key TEXT NOT NULL,
              thumb_key TEXT,
              preview_key TEXT,
              original_filename TEXT NOT NULL,
              mime_type TEXT NOT NULL,
              width INTEGER,
              height INTEGER,
              size_bytes INTEGER,
              thumb_size_bytes INTEGER,
              preview_size_bytes INTEGER,
              hash TEXT,
              status TEXT NOT NULL DEFAULT 'READY',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS styling_garments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              code TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL DEFAULT 'DRAFT',
              manual_tags_json TEXT,
              ai_tags_json TEXT,
              official_title_tags_json TEXT,
              effective_tags_json TEXT,
              tag_meta_json TEXT,
              tag_schema_version TEXT,
              tag_completed_at TEXT,
              tag_error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS styling_garment_images (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              styling_garment_id INTEGER NOT NULL REFERENCES styling_garments(id) ON DELETE CASCADE,
              media_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
              sort_order INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'READY',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS generation_tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'DRAFT',
              current_input_version_id INTEGER,
              active_styling_id INTEGER,
              active_preview_id INTEGER,
              active_result_set_id INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS generation_task_input_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              version_number INTEGER NOT NULL,
              name_snapshot TEXT NOT NULL,
              image_snapshot_json TEXT NOT NULL,
              hero_visual_summary TEXT,
              created_at TEXT NOT NULL,
              UNIQUE(task_id, version_number)
            );

            CREATE TABLE IF NOT EXISTS generation_task_images (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              media_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
              sort_order INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'READY',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS generation_stylings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
              source_material_ids_json TEXT NOT NULL,
              selected_material_ids_json TEXT NOT NULL,
              selected_material_snapshot_json TEXT NOT NULL,
              reason_summary TEXT,
              warnings_json TEXT,
              status TEXT NOT NULL DEFAULT 'SUCCEEDED',
              attempt_id INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS generation_shot_plans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              styling_id INTEGER NOT NULL REFERENCES generation_stylings(id) ON DELETE CASCADE,
              sort_order INTEGER NOT NULL,
              shot_type TEXT,
              plan_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(styling_id, sort_order)
            );

            CREATE TABLE IF NOT EXISTS generation_previews (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              styling_id INTEGER NOT NULL REFERENCES generation_stylings(id),
              input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
              preview_asset_id INTEGER REFERENCES media_assets(id),
              status TEXT NOT NULL DEFAULT 'QUEUED',
              active_attempt_id INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS generation_result_sets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
              styling_id INTEGER NOT NULL REFERENCES generation_stylings(id),
              preview_id INTEGER NOT NULL REFERENCES generation_previews(id),
              status TEXT NOT NULL DEFAULT 'RUNNING',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS generation_result_images_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              result_set_id INTEGER NOT NULL REFERENCES generation_result_sets(id) ON DELETE CASCADE,
              shot_plan_id INTEGER NOT NULL REFERENCES generation_shot_plans(id),
              sort_order INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'QUEUED',
              current_asset_id INTEGER REFERENCES media_assets(id),
              active_attempt_id INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS generation_attempts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              owner_type TEXT NOT NULL,
              owner_id INTEGER NOT NULL,
              provider TEXT NOT NULL,
              provider_request_id TEXT,
              status TEXT NOT NULL DEFAULT 'RUNNING',
              started_at TEXT,
              completed_at TEXT,
              failed_at TEXT,
              cancelled_at TEXT,
              error_code TEXT,
              error_message TEXT,
              request_snapshot_json TEXT,
              response_snapshot_json TEXT,
              output_asset_id INTEGER REFERENCES media_assets(id),
              retry_of_attempt_id INTEGER REFERENCES generation_attempts(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        ensure_migration_table(db)
        run_migrations(db)
        seed_default_brand_template(db)
        seed_default_model(db)


def ensure_migration_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          applied_at TEXT NOT NULL
        )
        """
    )


def run_migrations(db: sqlite3.Connection) -> None:
    applied_versions = {
        row["version"]
        for row in db.execute("SELECT version FROM schema_migrations").fetchall()
    }
    migration_functions = {
        "migration_add_generation_image_attempt_columns": migration_add_generation_image_attempt_columns,
        "migration_add_generation_run_archive_columns": migration_add_generation_run_archive_columns,
        "migration_normalize_planning_schema": migration_normalize_planning_schema,
        "migration_add_generation_task_control_columns": migration_add_generation_task_control_columns,
        "migration_add_generation_review_delivery_columns": migration_add_generation_review_delivery_columns,
        "migration_add_brand_template_versions": migration_add_brand_template_versions,
        "migration_add_garment_sku_archive_columns": migration_add_garment_sku_archive_columns,
        "migration_add_media_derivative_metadata": migration_add_media_derivative_metadata,
        "migration_add_lily_tagging_columns": migration_add_lily_tagging_columns,
    }
    for version, name, function_name in MIGRATIONS:
        if version in applied_versions:
            continue
        migration_functions[function_name](db)
        db.execute(
            """
            INSERT INTO schema_migrations (version, name, applied_at)
            VALUES (?, ?, ?)
            """,
            (version, name, now_iso()),
        )


def migration_add_generation_image_attempt_columns(db: sqlite3.Connection) -> None:
    ensure_column(db, "generation_images", "prompt_text", "TEXT")
    ensure_column(db, "generation_images", "image_inputs_json", "TEXT")
    ensure_column(db, "generation_images", "template_snapshot_json", "TEXT")
    ensure_column(db, "generation_images", "model_snapshot_json", "TEXT")
    ensure_column(db, "generation_images", "garment_snapshot_json", "TEXT")
    ensure_column(db, "generation_images", "attempts_json", "TEXT")
    ensure_column(db, "generation_images", "current_attempt_index", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "generation_images", "provider_request_id", "TEXT")
    ensure_column(db, "generation_images", "provider_status", "TEXT")
    ensure_column(db, "generation_images", "provider_urls_json", "TEXT")
    ensure_column(db, "generation_images", "provider_status_json", "TEXT")
    ensure_column(db, "generation_images", "submitted_at", "TEXT")
    ensure_column(db, "generation_images", "completed_at", "TEXT")
    ensure_column(db, "generation_images", "last_status_checked_at", "TEXT")


def migration_add_generation_run_archive_columns(db: sqlite3.Connection) -> None:
    ensure_column(db, "generation_runs", "archived_at", "TEXT")
    ensure_column(db, "generation_runs", "archive_reason", "TEXT")


def migration_normalize_planning_schema(db: sqlite3.Connection) -> None:
    rebuild_planning_schema(db)


def migration_add_generation_task_control_columns(db: sqlite3.Connection) -> None:
    ensure_column(db, "generation_images", "locked_by", "TEXT")
    ensure_column(db, "generation_images", "locked_until", "TEXT")
    ensure_column(db, "generation_images", "submit_attempts", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "generation_images", "status_check_attempts", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "generation_images", "status_error_attempts", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "generation_images", "cancel_requested_at", "TEXT")
    ensure_column(db, "generation_images", "timeout_at", "TEXT")


def migration_add_generation_review_delivery_columns(db: sqlite3.Connection) -> None:
    ensure_column(db, "generation_runs", "delivery_status", "TEXT NOT NULL DEFAULT 'draft'")
    ensure_column(db, "generation_runs", "delivered_at", "TEXT")
    ensure_column(db, "generation_images", "review_status", "TEXT NOT NULL DEFAULT 'candidate'")
    ensure_column(db, "generation_images", "review_note", "TEXT")
    ensure_column(db, "generation_images", "reviewed_at", "TEXT")


def migration_add_brand_template_versions(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS brand_template_versions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          template_id TEXT NOT NULL,
          version TEXT NOT NULL,
          name TEXT NOT NULL,
          snapshot_json TEXT NOT NULL,
          is_active INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          UNIQUE(template_id, version)
        )
        """
    )


def migration_add_garment_sku_archive_columns(db: sqlite3.Connection) -> None:
    ensure_column(db, "garment_skus", "archived_at", "TEXT")
    ensure_column(db, "garment_skus", "archive_reason", "TEXT")


def migration_add_media_derivative_metadata(db: sqlite3.Connection) -> None:
    for table in ("garment_images", "generation_images"):
        ensure_column(db, table, "thumb_path", "TEXT")
        ensure_column(db, table, "preview_path", "TEXT")
        ensure_column(db, table, "file_size", "INTEGER")
        ensure_column(db, table, "thumb_file_size", "INTEGER")
        ensure_column(db, table, "preview_file_size", "INTEGER")


def migration_add_lily_tagging_columns(db: sqlite3.Connection) -> None:
    ensure_column(db, "styling_garments", "official_title_tags_json", "TEXT")
    ensure_column(db, "styling_garments", "effective_tags_json", "TEXT")
    ensure_column(db, "styling_garments", "tag_meta_json", "TEXT")


def ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def rebuild_planning_schema(db: sqlite3.Connection) -> None:
    plan_columns = table_columns(db, "shot_plans")
    shot_columns = table_columns(db, "shot_plan_shots")
    expected_plan_columns = {
        "id",
        "sku_id",
        "model_profile_id",
        "status",
        "plan_json",
        "category",
        "sales_info",
        "lily_tone",
        "styling",
        "background",
        "planner_provider",
        "raw_response",
        "created_at",
        "updated_at",
    }
    expected_shot_columns = {
        "id",
        "shot_plan_id",
        "shot_index",
        "english_name",
        "title",
        "task",
        "scene",
        "crop_range",
        "model_scale",
        "head_treatment",
        "body_position",
        "pose_tone",
        "image_purpose",
        "shot_json",
        "created_at",
        "updated_at",
    }
    if plan_columns == expected_plan_columns and shot_columns == expected_shot_columns:
        return

    plan_rows = db.execute("SELECT * FROM shot_plans ORDER BY id").fetchall()
    shot_rows = db.execute("SELECT * FROM shot_plan_shots ORDER BY id").fetchall()
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("BEGIN")
    db.execute("DROP TABLE IF EXISTS shot_plans_new")
    db.execute("DROP TABLE IF EXISTS shot_plan_shots_new")
    db.execute(
        """
        CREATE TABLE shot_plans_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sku_id INTEGER NOT NULL REFERENCES garment_skus(id) ON DELETE CASCADE,
          model_profile_id INTEGER NOT NULL REFERENCES model_profiles(id),
          status TEXT NOT NULL DEFAULT 'DRAFT',
          plan_json TEXT NOT NULL,
          category TEXT,
          sales_info TEXT,
          lily_tone TEXT,
          styling TEXT,
          background TEXT,
          planner_provider TEXT NOT NULL,
          raw_response TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE shot_plan_shots_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          shot_plan_id INTEGER NOT NULL REFERENCES shot_plans(id) ON DELETE CASCADE,
          shot_index INTEGER NOT NULL,
          english_name TEXT,
          title TEXT NOT NULL,
          task TEXT,
          scene TEXT,
          crop_range TEXT,
          model_scale TEXT,
          head_treatment TEXT,
          body_position TEXT,
          pose_tone TEXT,
          image_purpose TEXT,
          shot_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(shot_plan_id, shot_index)
        )
        """
    )
    for row in plan_rows:
        plan = loads(row["plan_json"], {}) or {}
        generation_plan = plan.get("generationPlan") if isinstance(plan.get("generationPlan"), dict) else {}
        styling = generation_plan.get("styling") or combine_text(
            row_value(row, "styling"),
            row_value(row, "styling_plan"),
            row_value(row, "color_palette"),
            row_value(row, "accessories"),
            plan.get("styling"),
            plan.get("stylingPlan"),
            plan.get("colorPalette"),
            plan.get("accessories"),
        )
        background = generation_plan.get("background") or plan.get("background")
        if not background:
            shots = plan.get("shots") if isinstance(plan.get("shots"), list) else []
            background = next((shot.get("background") for shot in shots if isinstance(shot, dict) and shot.get("background")), None)
        db.execute(
            """
            INSERT INTO shot_plans_new
              (id, sku_id, model_profile_id, status, plan_json, category, sales_info,
               lily_tone, styling, background, planner_provider, raw_response, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["sku_id"],
                row["model_profile_id"],
                row["status"],
                row["plan_json"],
                row_value(row, "category") or plan.get("category"),
                row_value(row, "sales_info") or generation_plan.get("salesInfo") or plan.get("salesInfo"),
                row_value(row, "lily_tone") or generation_plan.get("lilyTone") or plan.get("lilyTone"),
                styling,
                background,
                row["planner_provider"],
                row["raw_response"],
                row["created_at"],
                row["updated_at"],
            ),
        )
    for row in shot_rows:
        shot = loads(row["shot_json"], {}) or {}
        task = row_value(row, "task") or shot.get("task")
        unified = row_value(row, "unified_requirement") or shot.get("unifiedRequirement")
        if unified and unified not in str(task or ""):
            task = f"{task}；{unified}" if task else unified
        db.execute(
            """
            INSERT INTO shot_plan_shots_new
              (id, shot_plan_id, shot_index, english_name, title, task, scene, crop_range,
               model_scale, head_treatment, body_position, pose_tone, image_purpose,
               shot_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["shot_plan_id"],
                row["shot_index"],
                row_value(row, "english_name") or shot.get("englishName"),
                row_value(row, "title") or row_value(row, "slot_name") or shot.get("title") or shot.get("slotName"),
                task,
                row_value(row, "scene") or shot.get("scene") or shot.get("frame"),
                row_value(row, "crop_range") or shot.get("cropRange") or shot.get("crop"),
                row_value(row, "model_scale") or shot.get("modelScale"),
                row_value(row, "head_treatment") or shot.get("headTreatment"),
                row_value(row, "body_position") or shot.get("bodyPosition") or shot.get("angle"),
                row_value(row, "pose_tone") or shot.get("poseTone") or shot.get("pose"),
                row_value(row, "image_purpose") or shot.get("imagePurpose") or shot.get("focus"),
                row["shot_json"],
                row["created_at"],
                row["updated_at"],
            ),
        )
    db.execute("DROP TABLE shot_plan_shots")
    db.execute("ALTER TABLE shot_plan_shots_new RENAME TO shot_plan_shots")
    db.execute("DROP TABLE shot_plans")
    db.execute("ALTER TABLE shot_plans_new RENAME TO shot_plans")
    db.execute("COMMIT")
    db.execute("PRAGMA foreign_keys = ON")


def table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def row_value(row: sqlite3.Row, key: str) -> Any:
    return row[key] if key in row.keys() else None


def combine_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "；".join(parts)


def seed_default_model(db: sqlite3.Connection) -> None:
    existing = db.execute(
        "SELECT id FROM model_profiles WHERE model_id = ?", ("Face-01",)
    ).fetchone()
    if existing:
        return

    profile = {
        "height": "170cm",
        "weight": "",
        "bodyShape": "H型",
        "bust": "80cm",
        "waist": "59cm",
        "hips": "89cm",
        "notes": "都市、通勤、轻熟、自然、克制；短棕色 bob，轻薄刘海",
        "headReferences": {
            "front": "front.png",
            "left45": "left45.png",
            "right90": "right90.png",
            "back": "back.png",
            "backTurnLeft45": "back-turn-left45.png",
            "backTurnRight45": "back-turn-right45.png",
        },
        "forbidden": [
            "不改变发型长度",
            "不改变刘海形态",
            "不生成浓妆",
            "不生成夸张表情",
            "不生成明显非同一人的五官",
        ],
    }
    timestamp = now_iso()
    db.execute(
        """
        INSERT INTO model_profiles
          (model_id, name, asset_dir, profile_json, is_default, created_at, updated_at)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        """,
        (
            "Face-01",
            "Face-01 默认模特",
            "model-profiles/Face-01",
            dumps(profile),
            timestamp,
            timestamp,
        ),
    )


def seed_default_brand_template(db: sqlite3.Connection) -> None:
    existing = db.execute(
        "SELECT id FROM brand_template_versions WHERE template_id = ? AND version = ?",
        ("lily-studio", "2026-05-11"),
    ).fetchone()
    if existing:
        return

    timestamp = now_iso()
    snapshot = {
        "templateId": "lily-studio",
        "version": "2026-05-11",
        "name": "Lily 默认白/浅灰棚拍模板",
        "summary": "白/浅灰棚拍，柔和光线，克制通勤电商主图语言。",
        "background": "白色或浅灰棚拍背景",
        "lighting": "柔和均匀，保留服装材质和结构",
        "stylingBoundary": "简洁通勤配饰，避免外景大片和夸张造型",
    }
    db.execute("UPDATE brand_template_versions SET is_active = 0 WHERE template_id = ?", ("lily-studio",))
    db.execute(
        """
        INSERT INTO brand_template_versions
          (template_id, version, name, snapshot_json, is_active, created_at)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        ("lily-studio", "2026-05-11", snapshot["name"], dumps(snapshot), timestamp),
    )


def media_url(key: str | None) -> str | None:
    return presigned_get_url(key)


def log_event(
    db: sqlite3.Connection,
    entity_type: str,
    entity_id: int | None,
    event_type: str,
    payload: Any,
) -> None:
    timestamp = now_iso()
    db.execute(
        """
        INSERT INTO event_logs (entity_type, entity_id, event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (entity_type, entity_id, event_type, dumps(payload), timestamp),
    )

    settings = get_settings()
    log_path = settings.log_dir / f"{timestamp[:10]}.jsonl"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            dumps(
                {
                    "createdAt": timestamp,
                    "entityType": entity_type,
                    "entityId": entity_id,
                    "eventType": event_type,
                    "payload": payload,
                }
            )
            + "\n"
        )
