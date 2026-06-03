from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Iterator

from .config import get_settings


MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("027", "drop_legacy_generation_tables", "migration_drop_legacy_generation_tables"),
    ("028", "add_generation_task_auto_run", "migration_add_generation_task_auto_run"),
    ("029", "add_generation_user_requirements", "migration_add_generation_user_requirements"),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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

            CREATE TABLE IF NOT EXISTS brand_profiles (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              name TEXT NOT NULL DEFAULT '',
              tagline TEXT NOT NULL DEFAULT '',
              brand_core TEXT NOT NULL DEFAULT '',
              style_dna TEXT NOT NULL DEFAULT '',
              styling_guidelines TEXT NOT NULL DEFAULT '',
              shooting_guidelines TEXT NOT NULL DEFAULT '',
              negative_constraints TEXT NOT NULL DEFAULT '',
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
              archived_at TEXT,
              archive_reason TEXT,
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
              user_requirement_text TEXT,
              current_input_version_id INTEGER,
              active_styling_id INTEGER,
              active_shoot_script_id INTEGER,
              active_preview_id INTEGER,
              active_storyboard_preview_id INTEGER,
              active_result_set_id INTEGER,
              auto_run_target TEXT,
              auto_run_requested_at TEXT,
              archived_at TEXT,
              archive_reason TEXT,
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
              model_snapshot_json TEXT,
              user_requirement_json TEXT,
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
              candidate_material_ids_json TEXT,
              candidate_retrieval_snapshot_json TEXT,
              user_requirement_json TEXT,
              library_adequacy_snapshot_json TEXT,
              selected_material_ids_json TEXT NOT NULL,
              selected_material_snapshot_json TEXT NOT NULL,
              styling_plan_json TEXT,
              hero_understanding_json TEXT,
              outfit_candidates_json TEXT,
              critic_scores_json TEXT,
              rejected_candidates_json TEXT,
              reason_summary TEXT,
              warnings_json TEXT,
              status TEXT NOT NULL DEFAULT 'SUCCEEDED',
              attempt_id INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS generation_shoot_scripts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
              styling_id INTEGER NOT NULL REFERENCES generation_stylings(id) ON DELETE CASCADE,
              status TEXT NOT NULL DEFAULT 'SUCCEEDED',
              script_json TEXT NOT NULL,
              summary_json TEXT,
              styling_risks_json TEXT,
              accessory_adjustments_json TEXT,
              active_attempt_id INTEGER,
              locked_by TEXT,
              locked_until TEXT,
              run_attempts INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              superseded_at TEXT
            );

            CREATE TABLE IF NOT EXISTS shoot_configs (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              config_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS prompt_configs (
              key TEXT PRIMARY KEY,
              group_type TEXT NOT NULL,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              content TEXT NOT NULL DEFAULT '',
              sort_order INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generation_shot_plans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              styling_id INTEGER NOT NULL REFERENCES generation_stylings(id) ON DELETE CASCADE,
              shoot_script_id INTEGER REFERENCES generation_shoot_scripts(id) ON DELETE CASCADE,
              sort_order INTEGER NOT NULL,
              shot_type TEXT,
              plan_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(shoot_script_id, sort_order)
            );

            CREATE TABLE IF NOT EXISTS generation_previews (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              styling_id INTEGER NOT NULL REFERENCES generation_stylings(id),
              shoot_script_id INTEGER REFERENCES generation_shoot_scripts(id),
              input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
              preview_asset_id INTEGER REFERENCES media_assets(id),
              status TEXT NOT NULL DEFAULT 'QUEUED',
              active_attempt_id INTEGER,
              locked_by TEXT,
              locked_until TEXT,
              run_attempts INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS generation_result_sets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
              styling_id INTEGER NOT NULL REFERENCES generation_stylings(id),
              shoot_script_id INTEGER REFERENCES generation_shoot_scripts(id),
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
              locked_by TEXT,
              locked_until TEXT,
              run_attempts INTEGER NOT NULL DEFAULT 0,
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
        seed_default_model(db)
        create_taxonomy_tables(db)


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
        "migration_drop_legacy_generation_tables": migration_drop_legacy_generation_tables,
        "migration_add_generation_task_auto_run": migration_add_generation_task_auto_run,
        "migration_add_generation_user_requirements": migration_add_generation_user_requirements,
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


def migration_drop_legacy_generation_tables(db: sqlite3.Connection) -> None:
    for table in (
        "generation_images",
        "generation_runs",
        "shot_plan_shots",
        "shot_plans",
        "garment_images",
        "garment_skus",
        "brand_template_versions",
        "ai_policy_configs",
    ):
        db.execute(f"DROP TABLE IF EXISTS {table}")


def migration_add_generation_task_auto_run(db: sqlite3.Connection) -> None:
    if not table_has_column(db, "generation_tasks", "auto_run_target"):
        db.execute("ALTER TABLE generation_tasks ADD COLUMN auto_run_target TEXT")
    if not table_has_column(db, "generation_tasks", "auto_run_requested_at"):
        db.execute("ALTER TABLE generation_tasks ADD COLUMN auto_run_requested_at TEXT")


def migration_add_generation_user_requirements(db: sqlite3.Connection) -> None:
    if not table_has_column(db, "generation_tasks", "user_requirement_text"):
        db.execute("ALTER TABLE generation_tasks ADD COLUMN user_requirement_text TEXT")
    if not table_has_column(db, "generation_task_input_versions", "user_requirement_json"):
        db.execute("ALTER TABLE generation_task_input_versions ADD COLUMN user_requirement_json TEXT")
    if not table_has_column(db, "generation_stylings", "user_requirement_json"):
        db.execute("ALTER TABLE generation_stylings ADD COLUMN user_requirement_json TEXT")


def table_has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in db.execute(f"PRAGMA table_info({table})").fetchall())


def create_shoot_config_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS shoot_configs (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          config_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )


def create_prompt_config_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_configs (
          key TEXT PRIMARY KEY,
          group_type TEXT NOT NULL,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL DEFAULT '',
          sort_order INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL
        )
        """
    )


def create_generation_shoot_script_tables(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS generation_shoot_scripts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
          input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
          styling_id INTEGER NOT NULL REFERENCES generation_stylings(id) ON DELETE CASCADE,
          status TEXT NOT NULL DEFAULT 'SUCCEEDED',
          script_json TEXT NOT NULL,
          summary_json TEXT,
          styling_risks_json TEXT,
          accessory_adjustments_json TEXT,
          active_attempt_id INTEGER,
          locked_by TEXT,
          locked_until TEXT,
          run_attempts INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          superseded_at TEXT
        )
        """
    )


def create_taxonomy_tables(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS tag_dimensions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          code TEXT NOT NULL UNIQUE,
          label_zh TEXT NOT NULL,
          description_zh TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tag_defs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          dimension_code TEXT NOT NULL REFERENCES tag_dimensions(code),
          code TEXT NOT NULL,
          label_zh TEXT NOT NULL,
          description_zh TEXT,
          parent_code TEXT,
          level INTEGER NOT NULL DEFAULT 1,
          value_kind TEXT NOT NULL DEFAULT 'tag',
          is_selectable INTEGER NOT NULL DEFAULT 1,
          is_active INTEGER NOT NULL DEFAULT 1,
          sort_order INTEGER NOT NULL DEFAULT 0,
          metadata_json TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(dimension_code, code),
          FOREIGN KEY (dimension_code, parent_code) REFERENCES tag_defs(dimension_code, code)
        );

        CREATE TABLE IF NOT EXISTS tag_relations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          from_dimension_code TEXT NOT NULL,
          from_tag_code TEXT NOT NULL,
          relation_type TEXT NOT NULL,
          to_dimension_code TEXT NOT NULL,
          to_tag_code TEXT NOT NULL,
          weight REAL NOT NULL DEFAULT 1.0,
          description_zh TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(from_dimension_code, from_tag_code, relation_type, to_dimension_code, to_tag_code),
          FOREIGN KEY (from_dimension_code, from_tag_code) REFERENCES tag_defs(dimension_code, code),
          FOREIGN KEY (to_dimension_code, to_tag_code) REFERENCES tag_defs(dimension_code, code)
        );

        CREATE TABLE IF NOT EXISTS tag_applicability_rules (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          category_dimension_code TEXT NOT NULL DEFAULT 'category',
          category_tag_code TEXT NOT NULL,
          target_dimension_code TEXT NOT NULL,
          target_tag_code TEXT NOT NULL,
          mode TEXT NOT NULL DEFAULT 'allowed',
          applies_to_descendants INTEGER NOT NULL DEFAULT 1,
          note TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(category_dimension_code, category_tag_code, target_dimension_code, target_tag_code),
          FOREIGN KEY (category_dimension_code, category_tag_code) REFERENCES tag_defs(dimension_code, code),
          FOREIGN KEY (target_dimension_code, target_tag_code) REFERENCES tag_defs(dimension_code, code)
        );

        CREATE TABLE IF NOT EXISTS garment_tag_assignments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          garment_id INTEGER NOT NULL REFERENCES styling_garments(id) ON DELETE CASCADE,
          dimension_code TEXT NOT NULL,
          tag_code TEXT NOT NULL,
          value_kind TEXT NOT NULL DEFAULT 'tag',
          value_number REAL,
          value_text TEXT,
          value_bool INTEGER,
          confidence REAL,
          source TEXT NOT NULL DEFAULT 'system',
          status TEXT NOT NULL DEFAULT 'confirmed',
          evidence_json TEXT,
          note TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(garment_id, dimension_code, tag_code, source),
          FOREIGN KEY (dimension_code, tag_code) REFERENCES tag_defs(dimension_code, code)
        );

        CREATE INDEX IF NOT EXISTS idx_tag_defs_dimension ON tag_defs(dimension_code, sort_order, id);
        CREATE INDEX IF NOT EXISTS idx_tag_applicability_category ON tag_applicability_rules(category_dimension_code, category_tag_code, is_active);
        CREATE INDEX IF NOT EXISTS idx_tag_applicability_target ON tag_applicability_rules(target_dimension_code, target_tag_code, is_active);
        CREATE INDEX IF NOT EXISTS idx_garment_tag_assignments_garment ON garment_tag_assignments(garment_id);
        CREATE INDEX IF NOT EXISTS idx_garment_tag_assignments_tag ON garment_tag_assignments(dimension_code, tag_code, status);
        """
    )


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
