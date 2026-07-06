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
    ("030", "split_generation_user_inputs", "migration_split_generation_user_inputs"),
    ("031", "add_generation_pending_stage_inputs", "migration_add_generation_pending_stage_inputs"),
    ("032", "add_shoot_script_runtime_columns", "migration_add_shoot_script_runtime_columns"),
    ("033", "cleanup_legacy_tables_and_attempt_provider_request_ids", "migration_cleanup_legacy_tables_and_attempt_provider_request_ids"),
    ("034", "drop_prompt_replaced_config_tables", "migration_drop_prompt_replaced_config_tables"),
    ("035", "recover_legacy_shoot_scripts", "migration_recover_legacy_shoot_scripts"),
    ("036", "add_attempt_progress_and_upload_cache", "migration_add_attempt_progress_and_upload_cache"),
    ("037", "add_generation_task_batch_id", "migration_add_generation_task_batch_id"),
)

PROMPT_CONFIG_CATALOG: tuple[tuple[str, str, str, str, int], ...] = (
    ("segment.brand_info", "segment", "品牌信息", "品牌信息分段 Prompt，可被组装 Prompt 引用。", 10),
    ("assembly.ai_tagging", "assembly", "AI 打标", "搭配服装入库后的图片理解和标签识别 Prompt。", 100),
    ("assembly.ai_styling", "assembly", "AI 选搭", "AI Stylist 选搭 Prompt，包含主推款理解、搭配选择和质量判断。", 110),
    ("assembly.shoot_script", "assembly", "拍摄剧本", "AI Director 拍摄剧本 Prompt，生成 ShootScript 和 6 个镜头。", 120),
    ("assembly.storyboard_preview", "assembly", "分镜预览", "Image-2 分镜预览 Prompt，生成 3x2 分镜图。", 130),
    ("assembly.final_image", "assembly", "生成成片", "Image-2 单张成片 Prompt，按前 5 个镜头生成最终图。", 140),
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
              hero_product_info_text TEXT,
              ai_styling_user_instruction_text TEXT,
              shoot_script_user_instruction_text TEXT,
              storyboard_preview_user_instruction_text TEXT,
              final_image_user_instruction_text TEXT,
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
              hero_product_info_json TEXT,
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
              user_instruction_json TEXT,
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
              user_instruction_json TEXT,
              active_attempt_id INTEGER,
              locked_by TEXT,
              locked_until TEXT,
              run_attempts INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              superseded_at TEXT
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
              user_instruction_json TEXT,
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
              user_instruction_json TEXT,
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
              user_instruction_json TEXT,
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
        ensure_prompt_config_catalog(db)


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
        "migration_split_generation_user_inputs": migration_split_generation_user_inputs,
        "migration_add_generation_pending_stage_inputs": migration_add_generation_pending_stage_inputs,
        "migration_add_shoot_script_runtime_columns": migration_add_shoot_script_runtime_columns,
        "migration_cleanup_legacy_tables_and_attempt_provider_request_ids": migration_cleanup_legacy_tables_and_attempt_provider_request_ids,
        "migration_drop_prompt_replaced_config_tables": migration_drop_prompt_replaced_config_tables,
        "migration_recover_legacy_shoot_scripts": migration_recover_legacy_shoot_scripts,
        "migration_add_attempt_progress_and_upload_cache": migration_add_attempt_progress_and_upload_cache,
        "migration_add_generation_task_batch_id": migration_add_generation_task_batch_id,
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


def migration_add_attempt_progress_and_upload_cache(db: sqlite3.Connection) -> None:
    """attempt 阶段进度 + provider 参考图上传缓存。

    - generation_attempts.progress_json：阶段进度展示元数据（不是新的业务状态）。
    - provider_upload_cache：同一 objectKey 的参考图在 TTL 内复用 provider URL，
      避免 5 张成片并发时重复上传同一批参考图。
    """
    if not table_has_column(db, "generation_attempts", "progress_json"):
        db.execute("ALTER TABLE generation_attempts ADD COLUMN progress_json TEXT")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_upload_cache (
          provider TEXT NOT NULL,
          object_key TEXT NOT NULL,
          provider_url TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY (provider, object_key)
        )
        """
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


def migration_cleanup_legacy_tables_and_attempt_provider_request_ids(db: sqlite3.Connection) -> None:
    migration_drop_legacy_generation_tables(db)
    if not table_has_column(db, "generation_attempts", "provider_request_id"):
        db.execute("ALTER TABLE generation_attempts ADD COLUMN provider_request_id TEXT")


def migration_drop_prompt_replaced_config_tables(db: sqlite3.Connection) -> None:
    for table in (
        "garment_tag_assignments",
        "tag_applicability_rules",
        "tag_relations",
        "tag_defs",
        "tag_dimensions",
        "shoot_configs",
        "brand_profiles",
    ):
        db.execute(f"DROP TABLE IF EXISTS {table}")


def migration_add_generation_task_batch_id(db: sqlite3.Connection) -> None:
    """批量成片批次：同一批次创建的任务共享 batch_id（客户端生成的不透明字符串）。"""
    if not table_has_column(db, "generation_tasks", "batch_id"):
        db.execute("ALTER TABLE generation_tasks ADD COLUMN batch_id TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_generation_tasks_batch_id ON generation_tasks(batch_id)")


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


def migration_split_generation_user_inputs(db: sqlite3.Connection) -> None:
    if not table_has_column(db, "generation_tasks", "hero_product_info_text"):
        db.execute("ALTER TABLE generation_tasks ADD COLUMN hero_product_info_text TEXT")
        db.execute("UPDATE generation_tasks SET hero_product_info_text = user_requirement_text WHERE hero_product_info_text IS NULL AND user_requirement_text IS NOT NULL")
    if not table_has_column(db, "generation_task_input_versions", "hero_product_info_json"):
        db.execute("ALTER TABLE generation_task_input_versions ADD COLUMN hero_product_info_json TEXT")
        db.execute("UPDATE generation_task_input_versions SET hero_product_info_json = user_requirement_json WHERE hero_product_info_json IS NULL AND user_requirement_json IS NOT NULL")
    if not table_has_column(db, "generation_stylings", "user_instruction_json"):
        db.execute("ALTER TABLE generation_stylings ADD COLUMN user_instruction_json TEXT")
        db.execute("UPDATE generation_stylings SET user_instruction_json = user_requirement_json WHERE user_instruction_json IS NULL AND user_requirement_json IS NOT NULL")
    if not table_has_column(db, "generation_shoot_scripts", "user_instruction_json"):
        db.execute("ALTER TABLE generation_shoot_scripts ADD COLUMN user_instruction_json TEXT")
    if not table_has_column(db, "generation_previews", "user_instruction_json"):
        db.execute("ALTER TABLE generation_previews ADD COLUMN user_instruction_json TEXT")
    if not table_has_column(db, "generation_result_sets", "user_instruction_json"):
        db.execute("ALTER TABLE generation_result_sets ADD COLUMN user_instruction_json TEXT")
    if not table_has_column(db, "generation_result_images_new", "user_instruction_json"):
        db.execute("ALTER TABLE generation_result_images_new ADD COLUMN user_instruction_json TEXT")


def migration_add_generation_pending_stage_inputs(db: sqlite3.Connection) -> None:
    for column in (
        "ai_styling_user_instruction_text",
        "shoot_script_user_instruction_text",
        "storyboard_preview_user_instruction_text",
        "final_image_user_instruction_text",
    ):
        if not table_has_column(db, "generation_tasks", column):
            db.execute(f"ALTER TABLE generation_tasks ADD COLUMN {column} TEXT")


def migration_add_shoot_script_runtime_columns(db: sqlite3.Connection) -> None:
    for column in ("active_shoot_script_id", "active_storyboard_preview_id"):
        if not table_has_column(db, "generation_tasks", column):
            db.execute(f"ALTER TABLE generation_tasks ADD COLUMN {column} INTEGER")
    if not table_has_column(db, "generation_shot_plans", "shoot_script_id"):
        db.execute("ALTER TABLE generation_shot_plans ADD COLUMN shoot_script_id INTEGER REFERENCES generation_shoot_scripts(id) ON DELETE CASCADE")
    if generation_shot_plans_needs_constraint_rebuild(db):
        rebuild_generation_shot_plans_for_shoot_script_unique(db)
    if not table_has_column(db, "generation_previews", "shoot_script_id"):
        db.execute("ALTER TABLE generation_previews ADD COLUMN shoot_script_id INTEGER REFERENCES generation_shoot_scripts(id)")
    if not table_has_column(db, "generation_result_sets", "shoot_script_id"):
        db.execute("ALTER TABLE generation_result_sets ADD COLUMN shoot_script_id INTEGER REFERENCES generation_shoot_scripts(id)")


def migration_recover_legacy_shoot_scripts(db: sqlite3.Connection) -> None:
    ensure_legacy_shoot_script_recovery_columns(db)
    rows = db.execute(
        """
        SELECT gt.*,
               gp.id AS recover_preview_id,
               gp.input_version_id AS recover_input_version_id,
               gp.user_instruction_json AS recover_preview_user_instruction_json
        FROM generation_tasks gt
        JOIN generation_previews gp
          ON gp.id = COALESCE(gt.active_storyboard_preview_id, gt.active_preview_id)
        WHERE gt.deleted_at IS NULL
          AND gt.active_styling_id IS NOT NULL
          AND gt.active_shoot_script_id IS NULL
          AND gt.status NOT IN ('STYLING_RUNNING', 'SCRIPT_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING')
          AND gp.status = 'SUCCEEDED'
          AND gp.preview_asset_id IS NOT NULL
        ORDER BY gt.id
        """
    ).fetchall()
    for row in rows:
        recover_legacy_shoot_script_for_task(db, row)


def ensure_legacy_shoot_script_recovery_columns(db: sqlite3.Connection) -> None:
    if not table_has_column(db, "generation_tasks", "active_shoot_script_id"):
        db.execute("ALTER TABLE generation_tasks ADD COLUMN active_shoot_script_id INTEGER")
    if not table_has_column(db, "generation_tasks", "active_storyboard_preview_id"):
        db.execute("ALTER TABLE generation_tasks ADD COLUMN active_storyboard_preview_id INTEGER")
    if not table_has_column(db, "generation_shoot_scripts", "user_instruction_json"):
        db.execute("ALTER TABLE generation_shoot_scripts ADD COLUMN user_instruction_json TEXT")
    if not table_has_column(db, "generation_shot_plans", "shoot_script_id"):
        db.execute("ALTER TABLE generation_shot_plans ADD COLUMN shoot_script_id INTEGER REFERENCES generation_shoot_scripts(id) ON DELETE CASCADE")
    if generation_shot_plans_needs_constraint_rebuild(db):
        rebuild_generation_shot_plans_for_shoot_script_unique(db)
    if not table_has_column(db, "generation_previews", "shoot_script_id"):
        db.execute("ALTER TABLE generation_previews ADD COLUMN shoot_script_id INTEGER REFERENCES generation_shoot_scripts(id)")
    if not table_has_column(db, "generation_previews", "user_instruction_json"):
        db.execute("ALTER TABLE generation_previews ADD COLUMN user_instruction_json TEXT")
    if not table_has_column(db, "generation_result_sets", "shoot_script_id"):
        db.execute("ALTER TABLE generation_result_sets ADD COLUMN shoot_script_id INTEGER REFERENCES generation_shoot_scripts(id)")
    if not table_has_column(db, "generation_result_sets", "user_instruction_json"):
        db.execute("ALTER TABLE generation_result_sets ADD COLUMN user_instruction_json TEXT")
    if not table_has_column(db, "generation_result_images_new", "user_instruction_json"):
        db.execute("ALTER TABLE generation_result_images_new ADD COLUMN user_instruction_json TEXT")


def recover_legacy_shoot_script_for_task(db: sqlite3.Connection, task_row: sqlite3.Row) -> None:
    task_id = int(task_row["id"])
    styling_id = int(task_row["active_styling_id"])
    preview_id = int(task_row["recover_preview_id"])
    input_version_id = int(task_row["recover_input_version_id"] or task_row["current_input_version_id"] or 0)
    if input_version_id <= 0:
        styling_input = db.execute("SELECT input_version_id FROM generation_stylings WHERE id = ?", (styling_id,)).fetchone()
        if not styling_input or not styling_input["input_version_id"]:
            return
        input_version_id = int(styling_input["input_version_id"])

    existing_script = db.execute(
        """
        SELECT * FROM generation_shoot_scripts
        WHERE task_id = ? AND styling_id = ? AND status = 'SUCCEEDED'
        ORDER BY id DESC
        LIMIT 1
        """,
        (task_id, styling_id),
    ).fetchone()
    timestamp = now_iso()
    styling_row = db.execute("SELECT * FROM generation_stylings WHERE id = ?", (styling_id,)).fetchone()
    if not styling_row:
        return
    styling_plan = loads(styling_row["styling_plan_json"], {}) if "styling_plan_json" in styling_row.keys() else {}
    shot_rows = db.execute(
        """
        SELECT * FROM generation_shot_plans
        WHERE task_id = ? AND styling_id = ? AND shoot_script_id IS NULL
        ORDER BY sort_order
        """,
        (task_id, styling_id),
    ).fetchall()

    if existing_script:
        shoot_script_id = int(existing_script["id"])
        script_json = loads(existing_script["script_json"], {})
    else:
        script_json = build_legacy_recovered_shoot_script(task_id, input_version_id, styling_id, styling_plan, shot_rows)
        cursor = db.execute(
            """
            INSERT INTO generation_shoot_scripts
              (task_id, input_version_id, styling_id, status, script_json, summary_json,
               styling_risks_json, accessory_adjustments_json, user_instruction_json, created_at, updated_at)
            VALUES (?, ?, ?, 'SUCCEEDED', ?, ?, '[]', '[]', ?, ?, ?)
            """,
            (
                task_id,
                input_version_id,
                styling_id,
                dumps(script_json),
                dumps(script_json.get("user_visible_summary") or {}),
                task_row["recover_preview_user_instruction_json"] or "{}",
                timestamp,
                timestamp,
            ),
        )
        shoot_script_id = int(cursor.lastrowid)

    if shot_rows:
        db.execute(
            """
            UPDATE generation_shot_plans
            SET shoot_script_id = ?, updated_at = ?
            WHERE task_id = ? AND styling_id = ? AND shoot_script_id IS NULL
            """,
            (shoot_script_id, timestamp, task_id, styling_id),
        )
    else:
        for shot in script_json.get("shots", []) if isinstance(script_json.get("shots"), list) else []:
            if not isinstance(shot, dict):
                continue
            sort_order = int(shot.get("shot_index") or 0)
            if sort_order <= 0:
                continue
            db.execute(
                """
                INSERT OR IGNORE INTO generation_shot_plans
                  (task_id, styling_id, shoot_script_id, sort_order, shot_type, plan_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    styling_id,
                    shoot_script_id,
                    sort_order,
                    str(shot.get("shot_role") or "reference"),
                    dumps(shot),
                    timestamp,
                    timestamp,
                ),
            )

    db.execute(
        """
        UPDATE generation_previews
        SET shoot_script_id = ?, updated_at = ?
        WHERE task_id = ? AND styling_id = ? AND shoot_script_id IS NULL
        """,
        (shoot_script_id, timestamp, task_id, styling_id),
    )
    db.execute(
        """
        UPDATE generation_result_sets
        SET shoot_script_id = ?, updated_at = ?
        WHERE task_id = ? AND styling_id = ? AND shoot_script_id IS NULL
        """,
        (shoot_script_id, timestamp, task_id, styling_id),
    )
    db.execute(
        """
        UPDATE generation_tasks
        SET active_shoot_script_id = ?,
            active_storyboard_preview_id = COALESCE(active_storyboard_preview_id, active_preview_id),
            status = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (shoot_script_id, recovered_legacy_task_status(db, task_row, preview_id), timestamp, task_id),
    )


def build_legacy_recovered_shoot_script(
    task_id: int,
    input_version_id: int,
    styling_id: int,
    styling_plan: dict[str, Any],
    shot_rows: list[sqlite3.Row],
) -> dict[str, Any]:
    hero = styling_plan.get("hero_understanding") if isinstance(styling_plan.get("hero_understanding"), dict) else {}
    shots = [legacy_shot_from_row(row, hero) for row in shot_rows]
    if not shots:
        shots = [legacy_fallback_shot(index, hero) for index in range(1, 7)]
    shots = sorted(shots, key=lambda item: int(item.get("shot_index") or 0))[:6]
    while len(shots) < 6:
        shots.append(legacy_fallback_shot(len(shots) + 1, hero))
    return {
        "schema_version": "shoot-script-v1",
        "script_source_mode": "legacy_recovered",
        "input_refs": {
            "task_id": task_id,
            "input_version_id": input_version_id,
            "styling_id": styling_id,
            "styling_plan_version": str(styling_plan.get("schema_version") or "styling-plan-v1"),
        },
        "model_identity_contract": {},
        "global_directing": {
            "rules": [{"label": "历史恢复", "value": "从旧版分镜 ShotPlan 恢复 ShootScript，不重新调用 AI。"}],
            "brand_tone": "Lily 商务时装电商棚拍，商品优先，干净克制。",
            "background": "白/浅灰棚拍背景或旧 ShotPlan 指定背景。",
            "lighting": "柔和均匀光线，真实肤色，轻微落地阴影。",
            "model_direction": "自然、自信、从容，保持数字模特身份一致。",
            "hair_makeup_boundary": "沿用数字模特身份契约，不改变脸型、五官和发型轮廓。",
            "accessory_boundary": "生成配饰低存在感，可省略，不做商品级验收。",
            "consistency_rules": ["复用同一 StylingPlan、旧分镜和镜头计划。"],
            "product_fidelity_rules": ["主推款和选中搭配服装以参考图为商品事实源。"],
            "negative_constraints": ["不要文字水印", "不要改款换色", "不要让配饰成为视觉中心"],
        },
        "styling_risks": [],
        "accessory_adjustments": [],
        "resolved_accessories": legacy_resolved_accessories(styling_plan),
        "shots": shots,
        "user_visible_summary": {
            "title": "历史分镜恢复剧本",
            "summary": "系统从旧版分镜镜头计划恢复拍摄剧本，用于继续生成前 5 张成片。",
            "key_points": hero.get("shot_requirements") if isinstance(hero.get("shot_requirements"), list) else [],
            "risk_summary": "该剧本为历史数据恢复产物，未重新调用 AI Director。",
        },
        "warnings": ["legacy_recovered_shoot_script"],
    }


def legacy_shot_from_row(row: sqlite3.Row, hero: dict[str, Any]) -> dict[str, Any]:
    plan = loads(row["plan_json"], {})
    index = int(plan.get("shot_index") or plan.get("sortOrder") or row["sort_order"] or 1)
    shot_role = str(plan.get("shot_role") or plan.get("shotType") or row["shot_type"] or "reference")
    return {
        "shot_index": index,
        "shot_role": shot_role,
        "shot_role_label": str(plan.get("shot_role_label") or plan.get("shotType") or shot_role),
        "framing": str(plan.get("framing") or ""),
        "crop_range": str(plan.get("crop_range") or plan.get("cropRange") or plan.get("framing") or ""),
        "model_scale": str(plan.get("model_scale") or plan.get("modelScale") or ""),
        "camera_angle": str(plan.get("camera_angle") or plan.get("cameraAngle") or ""),
        "body_position": str(plan.get("body_position") or plan.get("bodyPosition") or plan.get("cameraAngle") or ""),
        "pose": str(plan.get("pose") or ""),
        "movement": str(plan.get("movement") or ""),
        "hand_position": str(plan.get("hand_position") or plan.get("handPosition") or ""),
        "main_focus": str(plan.get("main_focus") or plan.get("mainFocus") or ""),
        "visible_garments": ensure_list(plan.get("visible_garments") or plan.get("visibleGarments")),
        "hero_selling_points": ensure_list(plan.get("hero_selling_points") or plan.get("heroSellingPoints") or hero.get("hero_selling_points")),
        "forbidden_occlusion_zones": ensure_list(plan.get("forbidden_occlusion_zones") or plan.get("forbiddenOcclusionZones") or hero.get("hero_visibility_risks")),
        "composition_notes": str(plan.get("composition_notes") or plan.get("compositionNotes") or ""),
        "accessory_instructions": ensure_list(plan.get("accessory_instructions") or plan.get("accessoryInstructions")) or ["生成配饰低存在感，可省略，不遮挡商品。"],
        "acceptance_criteria": ensure_list(plan.get("acceptance_criteria") or plan.get("acceptanceCriteria")) or ["主推款商品事实清楚。"],
        "negative_constraints": ensure_list(plan.get("negative_constraints") or plan.get("negativeConstraints")),
    }


def legacy_fallback_shot(index: int, hero: dict[str, Any]) -> dict[str, Any]:
    roles = ["hero", "core", "detail", "side_motion", "front_fit", "reference"]
    focuses = ensure_list(hero.get("hero_selling_points")) or ["主推款整体轮廓", "主推款关键细节"]
    return {
        "shot_index": index,
        "shot_role": roles[index - 1],
        "shot_role_label": roles[index - 1],
        "framing": "全身或近全身" if index in {1, 4, 5, 6} else "中近景或细节图",
        "crop_range": "按历史恢复镜头执行",
        "model_scale": "",
        "camera_angle": "正面或轻斜正",
        "body_position": "自然站姿",
        "pose": "自然、自信、克制",
        "movement": "",
        "hand_position": "",
        "main_focus": focuses[min(index - 1, len(focuses) - 1)],
        "visible_garments": ["hero"],
        "hero_selling_points": focuses,
        "forbidden_occlusion_zones": ensure_list(hero.get("hero_visibility_risks")),
        "composition_notes": "历史任务恢复镜头，商品优先，构图干净。",
        "accessory_instructions": ["生成配饰低存在感，可省略，不遮挡商品。"],
        "acceptance_criteria": ["主推款商品事实清楚。"],
        "negative_constraints": ["不要文字水印", "不要改款换色"],
    }


def legacy_resolved_accessories(styling_plan: dict[str, Any]) -> list[dict[str, Any]]:
    selected_key = str(styling_plan.get("selected_outfit_key") or styling_plan.get("selected_outfit_id") or "")
    selected = {}
    for item in styling_plan.get("outfit_candidates", []) if isinstance(styling_plan.get("outfit_candidates"), list) else []:
        if isinstance(item, dict) and str(item.get("outfit_key") or item.get("id") or "") == selected_key:
            selected = item
            break
    accessories = ensure_list(selected.get("generated_accessories") if selected else [])
    return [
        {
            "item_type": "other",
            "final_direction": str(item)[:180],
            "source": "styling_plan",
            "visibility": "low",
            "applies_to_shots": [],
            "reason": "来自历史 StylingPlan，AI Director 未重新调整。",
        }
        for item in accessories[:8]
    ]


def recovered_legacy_task_status(db: sqlite3.Connection, task_row: sqlite3.Row, preview_id: int) -> str:
    current_status = str(task_row["status"])
    if current_status != "FAILED":
        return current_status
    result_set_id = task_row["active_result_set_id"]
    if result_set_id:
        counts = db.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'SUCCEEDED' THEN 1 ELSE 0 END) AS succeeded
            FROM generation_result_images_new
            WHERE result_set_id = ?
            """,
            (result_set_id,),
        ).fetchone()
        total = int(counts["total"] or 0)
        succeeded = int(counts["succeeded"] or 0)
        if total >= 5 and succeeded >= 5:
            return "COMPLETED"
        if succeeded > 0:
            return "FINAL_PARTIAL"
    preview = db.execute(
        "SELECT id FROM generation_previews WHERE id = ? AND status = 'SUCCEEDED' AND preview_asset_id IS NOT NULL",
        (preview_id,),
    ).fetchone()
    return "PREVIEW_READY" if preview else current_status


def ensure_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def generation_shot_plans_needs_constraint_rebuild(db: sqlite3.Connection) -> bool:
    unique_columns = table_unique_column_sets(db, "generation_shot_plans")
    return ("shoot_script_id", "sort_order") not in unique_columns or ("styling_id", "sort_order") in unique_columns


def rebuild_generation_shot_plans_for_shoot_script_unique(db: sqlite3.Connection) -> None:
    foreign_keys_enabled = db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    db.commit()
    if foreign_keys_enabled:
        db.execute("PRAGMA foreign_keys = OFF")
    try:
        db.executescript(
            """
            CREATE TABLE generation_shot_plans_new (
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

            INSERT INTO generation_shot_plans_new
              (id, task_id, styling_id, shoot_script_id, sort_order, shot_type, plan_json, created_at, updated_at)
            SELECT
              id, task_id, styling_id, shoot_script_id, sort_order, shot_type, plan_json, created_at, updated_at
            FROM generation_shot_plans;

            DROP TABLE generation_shot_plans;
            ALTER TABLE generation_shot_plans_new RENAME TO generation_shot_plans;
            """
        )
        db.execute("DELETE FROM sqlite_sequence WHERE name = 'generation_shot_plans'")
        db.execute(
            """
            INSERT INTO sqlite_sequence (name, seq)
            SELECT 'generation_shot_plans', COALESCE(MAX(id), 0)
            FROM generation_shot_plans
            """
        )
        db.commit()
    finally:
        if foreign_keys_enabled:
            db.execute("PRAGMA foreign_keys = ON")


def table_has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in db.execute(f"PRAGMA table_info({table})").fetchall())


def table_unique_column_sets(db: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    unique_columns: set[tuple[str, ...]] = set()
    for index in db.execute(f"PRAGMA index_list({table})").fetchall():
        if not index["unique"]:
            continue
        columns = tuple(row["name"] for row in db.execute(f"PRAGMA index_info({index['name']})").fetchall())
        unique_columns.add(columns)
    return unique_columns



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


def ensure_prompt_config_catalog(db: sqlite3.Connection) -> None:
    create_prompt_config_table(db)
    timestamp = now_iso()
    for key, group_type, title, description, sort_order in PROMPT_CONFIG_CATALOG:
        db.execute(
            """
            INSERT INTO prompt_configs
              (key, group_type, title, description, content, sort_order, updated_at)
            VALUES (?, ?, ?, ?, '', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              group_type = excluded.group_type,
              title = excluded.title,
              description = excluded.description,
              sort_order = excluded.sort_order
            """,
            (key, group_type, title, description, sort_order, timestamp),
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
