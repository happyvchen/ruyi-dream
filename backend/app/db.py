from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
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
    ("010", "add_generation_input_model_snapshot", "migration_add_generation_input_model_snapshot"),
    ("011", "add_generation_styling_plan_columns", "migration_add_generation_styling_plan_columns"),
    ("012", "add_new_generation_recovery_columns", "migration_add_new_generation_recovery_columns"),
    ("013", "add_generation_task_archive_columns", "migration_add_generation_task_archive_columns"),
    ("014", "add_styling_garment_archive_columns", "migration_add_styling_garment_archive_columns"),
    ("015", "add_library_adequacy_snapshot", "migration_add_library_adequacy_snapshot"),
    ("016", "clear_successful_tagging_timeout_errors", "migration_clear_successful_tagging_timeout_errors"),
    ("017", "add_brand_profiles", "migration_add_brand_profiles"),
    ("018", "normalize_brand_profile_text_fields", "migration_normalize_brand_profile_text_fields"),
    ("019", "reframe_brand_profile_guidance_fields", "migration_reframe_brand_profile_guidance_fields"),
    ("020", "rename_brand_profile_production_guidance_fields", "migration_rename_brand_profile_production_guidance_fields"),
    ("021", "add_taxonomy_skeleton", "migration_add_taxonomy_skeleton"),
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
              current_input_version_id INTEGER,
              active_styling_id INTEGER,
              active_preview_id INTEGER,
              active_result_set_id INTEGER,
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
        seed_default_brand_template(db)
        seed_default_model(db)
        seed_default_taxonomy(db)


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
        "migration_add_generation_input_model_snapshot": migration_add_generation_input_model_snapshot,
        "migration_add_generation_styling_plan_columns": migration_add_generation_styling_plan_columns,
        "migration_add_new_generation_recovery_columns": migration_add_new_generation_recovery_columns,
        "migration_add_generation_task_archive_columns": migration_add_generation_task_archive_columns,
        "migration_add_styling_garment_archive_columns": migration_add_styling_garment_archive_columns,
        "migration_add_library_adequacy_snapshot": migration_add_library_adequacy_snapshot,
        "migration_clear_successful_tagging_timeout_errors": migration_clear_successful_tagging_timeout_errors,
        "migration_add_brand_profiles": migration_add_brand_profiles,
        "migration_normalize_brand_profile_text_fields": migration_normalize_brand_profile_text_fields,
        "migration_reframe_brand_profile_guidance_fields": migration_reframe_brand_profile_guidance_fields,
        "migration_rename_brand_profile_production_guidance_fields": migration_rename_brand_profile_production_guidance_fields,
        "migration_add_taxonomy_skeleton": migration_add_taxonomy_skeleton,
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


def migration_add_generation_input_model_snapshot(db: sqlite3.Connection) -> None:
    ensure_column(db, "generation_task_input_versions", "model_snapshot_json", "TEXT")


def migration_add_generation_styling_plan_columns(db: sqlite3.Connection) -> None:
    ensure_column(db, "generation_stylings", "candidate_material_ids_json", "TEXT")
    ensure_column(db, "generation_stylings", "candidate_retrieval_snapshot_json", "TEXT")
    ensure_column(db, "generation_stylings", "library_adequacy_snapshot_json", "TEXT")
    ensure_column(db, "generation_stylings", "styling_plan_json", "TEXT")
    ensure_column(db, "generation_stylings", "hero_understanding_json", "TEXT")
    ensure_column(db, "generation_stylings", "outfit_candidates_json", "TEXT")
    ensure_column(db, "generation_stylings", "critic_scores_json", "TEXT")
    ensure_column(db, "generation_stylings", "rejected_candidates_json", "TEXT")


def migration_add_library_adequacy_snapshot(db: sqlite3.Connection) -> None:
    ensure_column(db, "generation_stylings", "library_adequacy_snapshot_json", "TEXT")


def migration_add_new_generation_recovery_columns(db: sqlite3.Connection) -> None:
    for table in ("generation_previews", "generation_result_images_new"):
        ensure_column(db, table, "locked_by", "TEXT")
        ensure_column(db, table, "locked_until", "TEXT")
        ensure_column(db, table, "run_attempts", "INTEGER NOT NULL DEFAULT 0")


def migration_add_generation_task_archive_columns(db: sqlite3.Connection) -> None:
    ensure_column(db, "generation_tasks", "archived_at", "TEXT")
    ensure_column(db, "generation_tasks", "archive_reason", "TEXT")


def migration_add_styling_garment_archive_columns(db: sqlite3.Connection) -> None:
    ensure_column(db, "styling_garments", "archived_at", "TEXT")
    ensure_column(db, "styling_garments", "archive_reason", "TEXT")


def migration_clear_successful_tagging_timeout_errors(db: sqlite3.Connection) -> None:
    db.execute(
        """
        UPDATE styling_garments
        SET tag_error = NULL
        WHERE status IN ('READY', 'TAGGING_READY')
          AND tag_completed_at IS NOT NULL
          AND tag_error IS NOT NULL
        """
    )
    db.execute(
        """
        UPDATE generation_attempts
        SET error_code = NULL,
            error_message = NULL,
            failed_at = NULL
        WHERE owner_type = 'styling_garment_tagging'
          AND status = 'SUCCEEDED'
          AND error_code = 'TIMEOUT'
        """
    )


def migration_add_brand_profiles(db: sqlite3.Connection) -> None:
    db.execute(
        """
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
        )
        """
    )


def migration_normalize_brand_profile_text_fields(db: sqlite3.Connection) -> None:
    columns = table_columns(db, "brand_profiles")
    if {"brand_core", "style_dna", "styling_guidelines", "shooting_guidelines", "negative_constraints"}.issubset(columns):
        return
    if {"positioning", "visual_style", "styling_principles", "shooting_tone", "style_avoidance"}.issubset(columns):
        return
    if {"brand_values", "aesthetic", "tone"}.issubset(columns) and not {"values_json", "aesthetic_json", "tone_json"} & columns:
        return

    rows = db.execute("SELECT * FROM brand_profiles ORDER BY id").fetchall()
    db.execute("DROP TABLE IF EXISTS brand_profiles_new")
    db.execute(
        """
        CREATE TABLE brand_profiles_new (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          name TEXT NOT NULL DEFAULT '',
          tagline TEXT NOT NULL DEFAULT '',
          brand_values TEXT NOT NULL DEFAULT '',
          aesthetic TEXT NOT NULL DEFAULT '',
          tone TEXT NOT NULL DEFAULT '',
          business_overview TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    for row in rows:
        db.execute(
            """
            INSERT INTO brand_profiles_new
              (id, name, tagline, brand_values, aesthetic, tone, business_overview, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row_value(row, "name") or "",
                row_value(row, "tagline") or "",
                normalize_migrated_brand_text(row_value(row, "brand_values"), row_value(row, "values_json")),
                normalize_migrated_brand_text(row_value(row, "aesthetic"), row_value(row, "aesthetic_json")),
                normalize_migrated_brand_text(row_value(row, "tone"), row_value(row, "tone_json")),
                row_value(row, "business_overview") or "",
                row["created_at"],
                row["updated_at"],
            ),
        )
    db.execute("DROP TABLE brand_profiles")
    db.execute("ALTER TABLE brand_profiles_new RENAME TO brand_profiles")


def normalize_migrated_brand_text(current_value: Any, legacy_json_value: Any) -> str:
    current_text = str(current_value or "").strip()
    if current_text:
        return current_text
    legacy = loads(str(legacy_json_value), []) if legacy_json_value else []
    if isinstance(legacy, list):
        return "\n".join(str(item).strip() for item in legacy if str(item).strip())
    return str(legacy or "").strip()


def migration_reframe_brand_profile_guidance_fields(db: sqlite3.Connection) -> None:
    columns = table_columns(db, "brand_profiles")
    final_columns = {"brand_core", "style_dna", "styling_guidelines", "shooting_guidelines", "negative_constraints"}
    if final_columns.issubset(columns):
        return
    target_columns = {"positioning", "visual_style", "styling_principles", "shooting_tone", "style_avoidance"}
    legacy_columns = {"brand_values", "aesthetic", "tone", "business_overview", "values_json", "aesthetic_json", "tone_json"}
    if target_columns.issubset(columns) and not legacy_columns & columns:
        return

    rows = db.execute("SELECT * FROM brand_profiles ORDER BY id").fetchall()
    db.execute("DROP TABLE IF EXISTS brand_profiles_new")
    db.execute(
        """
        CREATE TABLE brand_profiles_new (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          name TEXT NOT NULL DEFAULT '',
          tagline TEXT NOT NULL DEFAULT '',
          positioning TEXT NOT NULL DEFAULT '',
          visual_style TEXT NOT NULL DEFAULT '',
          styling_principles TEXT NOT NULL DEFAULT '',
          shooting_tone TEXT NOT NULL DEFAULT '',
          style_avoidance TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    for row in rows:
        db.execute(
            """
            INSERT INTO brand_profiles_new
              (id, name, tagline, positioning, visual_style, styling_principles, shooting_tone, style_avoidance, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row_value(row, "name") or "",
                row_value(row, "tagline") or "",
                row_value(row, "positioning") or row_value(row, "business_overview") or "",
                row_value(row, "visual_style") or normalize_migrated_brand_text(row_value(row, "aesthetic"), row_value(row, "aesthetic_json")),
                row_value(row, "styling_principles") or normalize_migrated_brand_text(row_value(row, "brand_values"), row_value(row, "values_json")),
                row_value(row, "shooting_tone") or normalize_migrated_brand_text(row_value(row, "tone"), row_value(row, "tone_json")),
                row_value(row, "style_avoidance") or "",
                row["created_at"],
                row["updated_at"],
            ),
        )
    db.execute("DROP TABLE brand_profiles")
    db.execute("ALTER TABLE brand_profiles_new RENAME TO brand_profiles")


def migration_rename_brand_profile_production_guidance_fields(db: sqlite3.Connection) -> None:
    columns = table_columns(db, "brand_profiles")
    final_columns = {"brand_core", "style_dna", "styling_guidelines", "shooting_guidelines", "negative_constraints"}
    legacy_columns = {
        "positioning",
        "visual_style",
        "styling_principles",
        "shooting_tone",
        "style_avoidance",
        "brand_values",
        "aesthetic",
        "tone",
        "business_overview",
        "values_json",
        "aesthetic_json",
        "tone_json",
    }
    if final_columns.issubset(columns) and not legacy_columns & columns:
        return

    rows = db.execute("SELECT * FROM brand_profiles ORDER BY id").fetchall()
    db.execute("DROP TABLE IF EXISTS brand_profiles_new")
    db.execute(
        """
        CREATE TABLE brand_profiles_new (
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
        )
        """
    )
    for row in rows:
        db.execute(
            """
            INSERT INTO brand_profiles_new
              (id, name, tagline, brand_core, style_dna, styling_guidelines, shooting_guidelines, negative_constraints, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row_value(row, "name") or "",
                row_value(row, "tagline") or "",
                row_value(row, "brand_core") or row_value(row, "positioning") or row_value(row, "business_overview") or "",
                row_value(row, "style_dna") or row_value(row, "visual_style") or normalize_migrated_brand_text(row_value(row, "aesthetic"), row_value(row, "aesthetic_json")),
                row_value(row, "styling_guidelines") or row_value(row, "styling_principles") or normalize_migrated_brand_text(row_value(row, "brand_values"), row_value(row, "values_json")),
                row_value(row, "shooting_guidelines") or row_value(row, "shooting_tone") or normalize_migrated_brand_text(row_value(row, "tone"), row_value(row, "tone_json")),
                row_value(row, "negative_constraints") or row_value(row, "style_avoidance") or "",
                row["created_at"],
                row["updated_at"],
            ),
        )
    db.execute("DROP TABLE brand_profiles")
    db.execute("ALTER TABLE brand_profiles_new RENAME TO brand_profiles")


def migration_add_taxonomy_skeleton(db: sqlite3.Connection) -> None:
    create_taxonomy_tables(db)
    seed_default_taxonomy(db)


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
        CREATE INDEX IF NOT EXISTS idx_garment_tag_assignments_garment ON garment_tag_assignments(garment_id);
        CREATE INDEX IF NOT EXISTS idx_garment_tag_assignments_tag ON garment_tag_assignments(dimension_code, tag_code, status);
        """
    )


def seed_default_taxonomy(db: sqlite3.Connection) -> None:
    create_taxonomy_tables(db)
    timestamp = now_iso()
    dimensions = [
        ("category", "品类", "服装系统品类和规范子类。", 10),
        ("color", "色彩", "主色、辅色等可见颜色。", 20),
        ("color_attribute", "色彩属性", "冷暖、对比和色彩分布。", 30),
        ("material_visual", "视觉材质", "AI 可从图片判断的视觉材质。", 40),
        ("surface_texture", "表面肌理", "面料表面纹理、光泽和垂坠。", 50),
        ("fit", "版型", "修身、常规、宽松等合体程度。", 60),
        ("silhouette", "廓形", "轮廓、比例和视觉结构。", 70),
        ("structure", "结构", "肩线、褶皱、垂坠等结构语言。", 80),
        ("length", "长度", "上衣、袖长、下装长度。", 90),
        ("neckline", "领型", "领口和领型。", 100),
        ("closure", "闭合方式", "纽扣、拉链、套头等穿脱结构。", 110),
        ("design_detail", "设计细节", "口袋、撞色、褶皱、开衩等设计点。", 120),
        ("pattern", "图案", "纯色、条纹、格纹、印花等。", 130),
        ("pattern_scale", "图案大小", "图案视觉尺度。", 140),
        ("wearing_method", "穿法", "扣合、敞开、塞腰等真实穿法。", 150),
        ("wearability", "穿着能力", "可单穿、内搭、外搭、下装搭配等能力。", 160),
        ("coverage_risk", "遮挡风险", "作为搭配服饰时遮挡主推款的风险。", 170),
        ("lily_style_score", "Lily 风格分值", "商务时装、通勤、干净度等 0-1 分值项。", 180),
        ("formality", "正式度", "休闲到正式的分档。", 190),
        ("occasion", "适用场景", "通勤、办公室、日常等场景。", 200),
        ("season", "季节", "春夏秋冬。", 210),
        ("pairing_logic", "搭配逻辑", "适合搭配、避免搭配、色彩和廓形匹配。", 220),
        ("style_risk", "风格风险", "过甜、过街头、过重等搭配风险。", 230),
        ("manual_business", "人工业务标签", "运营维护的非视觉业务信息。", 240),
        ("review_reason", "复核原因", "低置信度、品类不清等复核原因。", 250),
    ]
    for code, label, description, sort_order in dimensions:
        db.execute(
            """
            INSERT INTO tag_dimensions (code, label_zh, description_zh, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
              label_zh = excluded.label_zh,
              description_zh = excluded.description_zh,
              sort_order = excluded.sort_order,
              updated_at = excluded.updated_at
            """,
            (code, label, description, sort_order, timestamp, timestamp),
        )

    def add_tags(dimension: str, values: list[tuple[str, str] | tuple[str, str, str | None, int, str]]) -> None:
        for index, item in enumerate(values, start=1):
            if len(item) == 2:
                code, label = item
                parent_code = None
                level = 1
                value_kind = "tag"
            else:
                code, label, parent_code, level, value_kind = item
            db.execute(
                """
                INSERT INTO tag_defs
                  (dimension_code, code, label_zh, parent_code, level, value_kind, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dimension_code, code) DO UPDATE SET
                  label_zh = excluded.label_zh,
                  parent_code = excluded.parent_code,
                  level = excluded.level,
                  value_kind = excluded.value_kind,
                  sort_order = excluded.sort_order,
                  updated_at = excluded.updated_at
                """,
                (dimension, code, label, parent_code, level, value_kind, index * 10, timestamp, timestamp),
            )

    add_tags("category", [
        ("top", "上装"), ("shirt", "衬衫", "top", 2, "tag"), ("blouse", "罩衫", "top", 2, "tag"),
        ("t_shirt", "T恤", "top", 2, "tag"), ("vest", "马甲/背心", "top", 2, "tag"), ("camisole", "吊带", "top", 2, "tag"),
        ("knitwear", "针织类"), ("cardigan", "针织开衫", "knitwear", 2, "tag"),
        ("bottom", "下装"), ("trousers", "裤装", "bottom", 2, "tag"), ("jeans", "牛仔裤", "bottom", 2, "tag"),
        ("shorts", "短裤", "bottom", 2, "tag"), ("skirt", "半身裙", "bottom", 2, "tag"),
        ("dress", "连衣裙"), ("jumpsuit", "连体裤/连体衣"), ("suit_set", "套装"),
        ("outerwear", "外套类"), ("blazer", "西装", "outerwear", 2, "tag"), ("jacket", "夹克", "outerwear", 2, "tag"),
        ("trench", "风衣", "outerwear", 2, "tag"), ("coat", "大衣", "outerwear", 2, "tag"),
        ("down_jacket", "羽绒服", "outerwear", 2, "tag"), ("other_garment", "其他服装"),
        ("unsupported_accessory", "不支持入库配饰"), ("unknown", "未知"),
    ])
    add_tags("color", [("black", "黑色"), ("white", "白色"), ("ivory", "象牙白"), ("beige", "米色"), ("grey", "灰色"), ("blue", "蓝色"), ("navy", "藏青色"), ("brown", "棕色"), ("green", "绿色"), ("red", "红色"), ("pink", "粉色"), ("yellow", "黄色"), ("purple", "紫色"), ("metallic", "金属色"), ("multi", "多色"), ("unknown", "未知")])
    add_tags("color_attribute", [("warm", "暖调"), ("cool", "冷调"), ("neutral", "中性"), ("mixed", "混合冷暖"), ("low", "低对比"), ("medium", "中对比"), ("high", "高对比"), ("solid", "纯色"), ("color_block", "色块"), ("trim", "边饰/镶边"), ("gradient", "渐变"), ("print_based", "基于图案")])
    add_tags("material_visual", [("cotton_like", "棉感"), ("linen_like", "亚麻感"), ("silk_like", "真丝感"), ("satin_like", "缎面感"), ("acetate_like", "醋酸感"), ("knit", "针织"), ("wool_like", "羊毛感"), ("denim", "丹宁"), ("leather_like", "皮革感"), ("lace", "蕾丝"), ("chiffon", "雪纺"), ("tweed_like", "粗花呢/香风感"), ("down_padded", "羽绒填充感"), ("technical_fabric", "科技面料感"), ("unknown", "未知")])
    add_tags("surface_texture", [("smooth", "平滑"), ("ribbed", "坑条"), ("openwork", "镂空织法"), ("slub", "竹节肌理"), ("woven_texture", "编织肌理"), ("sheer", "半透"), ("glossy", "有光泽"), ("matte", "哑光"), ("structured", "挺括"), ("soft_drape", "柔软垂坠")])
    add_tags("fit", [("slim", "修身"), ("regular", "常规"), ("relaxed", "微宽松"), ("loose", "宽松"), ("oversized", "廓形宽大"), ("unknown", "未知")])
    add_tags("silhouette", [("straight", "直线型"), ("a_line", "A字型"), ("h_line", "H型"), ("x_line", "X型"), ("wide_leg", "阔腿"), ("flare", "微喇/喇叭"), ("tapered", "锥形"), ("cropped", "短款"), ("waist_defined", "收腰/腰线明确"), ("boxy", "方正廓形")])
    add_tags("structure", [("tailored", "剪裁感"), ("soft", "柔软结构"), ("draped", "垂坠"), ("layered", "层次"), ("structured_shoulder", "结构肩"), ("dropped_shoulder", "落肩"), ("pleated", "褶裥"), ("gathered", "抽褶")])
    add_tags("length", [("top_cropped", "短上衣"), ("top_hip", "及胯上衣"), ("top_long", "长上衣"), ("sleeve_sleeveless", "无袖"), ("sleeve_short", "短袖"), ("sleeve_elbow", "肘袖"), ("sleeve_three_quarter", "七分袖"), ("sleeve_long", "长袖"), ("bottom_short", "短款下装"), ("bottom_knee", "及膝"), ("bottom_midi", "中长"), ("bottom_ankle", "九分/及踝"), ("bottom_full", "全长"), ("unknown", "未知")])
    add_tags("neckline", [("crew", "圆领"), ("v_neck", "V领"), ("polo", "Polo领"), ("shirt_collar", "衬衫领"), ("lapel", "翻领"), ("boat", "船领"), ("square", "方领"), ("halter", "挂脖"), ("cowl", "垂褶领"), ("unknown", "未知")])
    add_tags("closure", [("button", "纽扣"), ("zipper", "拉链"), ("pullover", "套头"), ("wrap", "裹身"), ("tie", "系带"), ("open_front", "开襟"), ("unknown", "未知")])
    add_tags("design_detail", [("asymmetric", "不对称"), ("lace", "蕾丝"), ("contrast_trim", "撞色边饰"), ("embroidery", "绣花"), ("pocket", "口袋"), ("pleat", "褶皱"), ("cutout", "镂空"), ("patchwork", "拼接"), ("print", "印花"), ("button", "纽扣细节"), ("ruffle", "荷叶边"), ("bow_tie", "蝴蝶结/飘带"), ("belt", "腰带"), ("slit", "开衩")])
    add_tags("pattern", [("solid", "纯色"), ("stripe", "条纹"), ("check", "格纹"), ("polka_dot", "波点"), ("floral", "花卉"), ("graphic", "图形"), ("abstract", "抽象"), ("texture_pattern", "肌理图案"), ("logo", "Logo"), ("animal", "动物纹"), ("unknown", "未知")])
    add_tags("pattern_scale", [("none", "无图案"), ("small", "小图案"), ("medium", "中图案"), ("large", "大图案"), ("mixed", "混合尺度"), ("unknown", "未知")])
    add_tags("wearing_method", [("buttoned", "扣合"), ("open", "敞开"), ("tucked", "塞腰"), ("untucked", "自然下摆"), ("belted", "系腰带"), ("layered", "叠穿")])
    add_tags("wearability", [("can_be_worn_alone", "可单穿", None, 1, "boolean"), ("can_be_inner_layer", "可作内搭", None, 1, "boolean"), ("can_be_outer_layer", "可作外搭", None, 1, "boolean"), ("can_be_bottom_pairing", "可作下装搭配", None, 1, "boolean"), ("can_be_set_anchor", "可作套装核心", None, 1, "boolean")])
    add_tags("coverage_risk", [("none", "无明显遮挡风险"), ("may_cover_hero", "可能遮挡主推款"), ("likely_cover_hero", "较可能遮挡主推款"), ("unknown", "未知")])
    add_tags("lily_style_score", [("business_fashion_score", "商务时装感", None, 1, "score"), ("commute_score", "通勤适配度", None, 1, "score"), ("clean_score", "干净利落度", None, 1, "score"), ("relaxed_score", "松弛得体度", None, 1, "score"), ("feminine_score", "女性化柔和度", None, 1, "score"), ("fashion_detail_score", "时装细节度", None, 1, "score")])
    add_tags("formality", [("casual", "休闲"), ("smart_casual", "精致休闲"), ("office", "办公室"), ("formal", "正式"), ("unknown", "未知")])
    add_tags("occasion", [("daily", "日常"), ("commute", "通勤"), ("office", "办公室"), ("light_social", "轻社交"), ("date", "约会"), ("travel", "旅行"), ("home", "居家")])
    add_tags("season", [("spring", "春"), ("summer", "夏"), ("autumn", "秋"), ("winter", "冬")])
    add_tags("pairing_logic", [("best_pairing_top", "适合搭配上装"), ("best_pairing_bottom", "适合搭配下装"), ("best_pairing_outerwear", "适合搭配外套"), ("best_pairing_dress", "适合搭配连衣裙"), ("best_pairing_knitwear", "适合搭配针织"), ("avoid_pairing_top", "避免搭配上装"), ("avoid_pairing_bottom", "避免搭配下装"), ("avoid_pairing_outerwear", "避免搭配外套"), ("avoid_pairing_dress", "避免搭配连衣裙"), ("avoid_pairing_knitwear", "避免搭配针织")])
    add_tags("style_risk", [("too_formal", "过正式"), ("too_casual", "过休闲"), ("too_sweet", "过甜美"), ("too_street", "过街头"), ("too_party", "过宴会"), ("too_heavy", "过厚重"), ("too_busy", "过繁复"), ("none", "无明显风险")])
    add_tags("manual_business", [("styling_priority", "选搭优先级", None, 1, "text"), ("do_not_use_for_styling", "不进入智能选搭", None, 1, "boolean"), ("season_year", "年份", None, 1, "text"), ("season_drop", "上新季节", None, 1, "text"), ("launch_batch", "上新批次", None, 1, "text"), ("retail_channel", "零售渠道", None, 1, "text"), ("series", "系列", None, 1, "text"), ("collaboration", "联名", None, 1, "text"), ("internal_collection", "内部企划系列", None, 1, "text"), ("notes", "人工备注", None, 1, "text")])
    add_tags("review_reason", [("low_confidence", "低置信度"), ("conflicting_images", "多图疑似不同商品"), ("unclear_category", "品类不清"), ("title_image_conflict", "标题和图片冲突"), ("insufficient_images", "图片不足"), ("unsupported_accessory", "不支持的配饰"), ("uncertain_material", "材质不确定"), ("uncertain_color", "颜色不确定"), ("uncertain_fit", "版型不确定"), ("fallback_tags", "兜底标签")])

    relations = [
        ("category", "blazer", "parent_of", "category", "outerwear", 1.0, "西装归属外套类。"),
        ("category", "jacket", "parent_of", "category", "outerwear", 1.0, "夹克归属外套类。"),
        ("category", "trench", "parent_of", "category", "outerwear", 1.0, "风衣归属外套类。"),
        ("category", "dress", "slot_mapping", "wearability", "can_be_worn_alone", 1.0, "连衣裙通常可单穿。"),
        ("category", "outerwear", "implies", "wearability", "can_be_outer_layer", 1.0, "外套类通常可作外搭。"),
        ("category", "down_jacket", "implies", "style_risk", "too_heavy", 0.7, "羽绒服在轻量通勤搭配里可能偏厚重。"),
    ]
    for relation in relations:
        db.execute(
            """
            INSERT OR IGNORE INTO tag_relations
              (from_dimension_code, from_tag_code, relation_type, to_dimension_code, to_tag_code, weight, description_zh, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*relation, timestamp, timestamp),
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
