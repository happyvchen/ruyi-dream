from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from .config import get_settings


MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("050", "finalize_direct_prompt_generation_workflow", "migration_finalize_direct_prompt_generation_workflow"),
)

# Prompt 维护页只保留当前运行链路会读取的配置槽。Prompt 正文不写死在代码里；
# 代码只登记槽位、拍摄图位族路由和动态事实占位，真正的黄金 Prompt 由页面维护。
PROMPT_CONFIG_CATALOG: tuple[tuple[str, str, str, str, int], ...] = (
    ("segment.brand_info", "segment", "品牌信息", "品牌信息分段 Prompt，可被组装 Prompt 引用。", 10),
    ("assembly.ai_tagging", "assembly", "AI 打标", "搭配服装入库后的图片理解和标签识别 Prompt。", 100),
    ("assembly.ai_styling", "assembly", "AI 选搭", "AI Stylist 选搭 Prompt，含主推款理解、搭配选择和质量判断。", 110),
    ("assembly.storyboard_preview_top", "assembly", "分镜预览 · 上装/针织/外套", "Image-2 上装类固定六镜头分镜 Prompt。", 120),
    ("assembly.storyboard_preview_bottom", "assembly", "分镜预览 · 裤装/半身裙", "Image-2 下装类固定六镜头分镜 Prompt。", 130),
    ("assembly.storyboard_preview_one_piece", "assembly", "分镜预览 · 连衣裙/连体/套装", "Image-2 连衣裙/连体/套装固定六镜头分镜 Prompt。", 140),
    ("assembly.final_image_top", "assembly", "生成成片 · 上装/针织/外套", "Image-2 上装类固定五镜头成片 Prompt。", 150),
    ("assembly.final_image_bottom", "assembly", "生成成片 · 裤装/半身裙", "Image-2 下装类固定五镜头成片 Prompt。", 160),
    ("assembly.final_image_one_piece", "assembly", "生成成片 · 连衣裙/连体/套装", "Image-2 连衣裙/连体/套装固定五镜头成片 Prompt。", 170),
)

REMOVED_PROMPT_CONFIG_KEYS: tuple[str, ...] = (
    "assembly.shoot_script",
    "assembly.shoot_script_system",
    "assembly.storyboard_preview",
    "assembly.final_image",
    "segment.json_output_discipline",
    "segment.json_only_reask_hint",
    "segment.image_reference_constraint",
    "segment.image_reference_preview_note",
    "assembly.ai_styling_system",
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

            CREATE TABLE IF NOT EXISTS shoot_environment_references (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              media_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
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
              storyboard_preview_user_instruction_text TEXT,
              final_image_user_instruction_text TEXT,
              current_input_version_id INTEGER,
              active_styling_id INTEGER,
              active_preview_id INTEGER,
              active_storyboard_preview_id INTEGER,
              active_result_set_id INTEGER,
              auto_run_target TEXT,
              auto_run_requested_at TEXT,
              batch_id TEXT,
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

            CREATE TABLE IF NOT EXISTS generation_previews (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              styling_id INTEGER NOT NULL REFERENCES generation_stylings(id),
              input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
              user_instruction_json TEXT,
              preview_asset_id INTEGER REFERENCES media_assets(id),
              environment_asset_id INTEGER REFERENCES media_assets(id),
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
              user_instruction_json TEXT,
              status TEXT NOT NULL DEFAULT 'RUNNING',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS generation_result_images_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              result_set_id INTEGER NOT NULL REFERENCES generation_result_sets(id) ON DELETE CASCADE,
              shot_role TEXT NOT NULL DEFAULT '',
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
              progress_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS provider_upload_cache (
              provider TEXT NOT NULL,
              object_key TEXT NOT NULL,
              provider_url TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (provider, object_key)
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
        "migration_finalize_direct_prompt_generation_workflow": migration_finalize_direct_prompt_generation_workflow,
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


def migration_finalize_direct_prompt_generation_workflow(db: sqlite3.Connection) -> None:
    """最终工作流清理：移除动态拍摄剧本 / ShotPlan 运行依赖。

    本迁移不兼容旧运行态：旧的拍摄剧本、镜头计划、相关 attempt 以及依赖旧结构的
    成片结果会被清理。当前权威链路是：
    上传主推款 -> AI 智能选搭 -> 三类固定 Prompt 生成分镜预览 -> 前 5 格生成成片。
    """
    if table_exists(db, "generation_attempts"):
        db.execute("DELETE FROM generation_attempts WHERE owner_type = 'generation_shoot_script'")
    if table_exists(db, "prompt_configs"):
        placeholders = ", ".join("?" for _ in REMOVED_PROMPT_CONFIG_KEYS)
        db.execute(f"DELETE FROM prompt_configs WHERE key IN ({placeholders})", REMOVED_PROMPT_CONFIG_KEYS)
    for table in (
        "brand_profiles",
        "shoot_configs",
        "tag_dimensions",
        "tag_defs",
        "tag_relations",
        "tag_applicability_rules",
        "garment_tag_assignments",
    ):
        db.execute(f"DROP TABLE IF EXISTS {table}")
    rebuild_generation_tasks_final(db)
    rebuild_generation_previews_final(db)
    rebuild_generation_result_sets_final(db)
    rebuild_generation_result_images_final(db)
    db.execute("DROP TABLE IF EXISTS generation_shot_plans")
    db.execute("DROP TABLE IF EXISTS generation_shoot_scripts")
    db.execute(
        """
        UPDATE generation_tasks
        SET status = CASE
              WHEN status = 'SCRIPT_RUNNING' THEN 'STYLING_READY'
              WHEN status = 'SCRIPT_READY' THEN 'STYLING_READY'
              ELSE status
            END,
            updated_at = ?
        WHERE status IN ('SCRIPT_RUNNING', 'SCRIPT_READY')
        """,
        (now_iso(),),
    )


def rebuild_generation_tasks_final(db: sqlite3.Connection) -> None:
    if not table_exists(db, "generation_tasks"):
        return
    columns = table_columns(db, "generation_tasks")
    removed = {"shoot_script_user_instruction_text", "active_shoot_script_id"}
    required = {"storyboard_preview_user_instruction_text", "final_image_user_instruction_text", "active_storyboard_preview_id", "batch_id"}
    if not removed.intersection(columns) and required.issubset(columns):
        return
    source_columns = [
        "id", "name", "status", "user_requirement_text", "hero_product_info_text",
        "ai_styling_user_instruction_text", "storyboard_preview_user_instruction_text",
        "final_image_user_instruction_text", "current_input_version_id", "active_styling_id",
        "active_preview_id", "active_storyboard_preview_id", "active_result_set_id",
        "auto_run_target", "auto_run_requested_at", "batch_id", "archived_at", "archive_reason",
        "created_at", "updated_at", "deleted_at",
    ]
    select_exprs = [column if column in columns else default_sql_for_generation_task_column(column) for column in source_columns]

    def rebuild() -> None:
        db.executescript(
            """
            CREATE TABLE generation_tasks_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'DRAFT',
              user_requirement_text TEXT,
              hero_product_info_text TEXT,
              ai_styling_user_instruction_text TEXT,
              storyboard_preview_user_instruction_text TEXT,
              final_image_user_instruction_text TEXT,
              current_input_version_id INTEGER,
              active_styling_id INTEGER,
              active_preview_id INTEGER,
              active_storyboard_preview_id INTEGER,
              active_result_set_id INTEGER,
              auto_run_target TEXT,
              auto_run_requested_at TEXT,
              batch_id TEXT,
              archived_at TEXT,
              archive_reason TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              deleted_at TEXT
            );
            """
        )
        db.execute(
            f"""
            INSERT INTO generation_tasks_new ({", ".join(source_columns)})
            SELECT {", ".join(select_exprs)}
            FROM generation_tasks
            """
        )
        db.executescript(
            """
            DROP TABLE generation_tasks;
            ALTER TABLE generation_tasks_new RENAME TO generation_tasks;
            CREATE INDEX IF NOT EXISTS idx_generation_tasks_batch_id ON generation_tasks(batch_id);
            """
        )

    run_schema_rebuild_with_foreign_keys_disabled(db, rebuild)


def default_sql_for_generation_task_column(column: str) -> str:
    if column in {"name"}:
        return "''"
    if column in {"status"}:
        return "'DRAFT'"
    if column in {"created_at", "updated_at"}:
        return f"'{now_iso()}'"
    return "NULL"


def rebuild_generation_previews_final(db: sqlite3.Connection) -> None:
    if not table_exists(db, "generation_previews"):
        return
    columns = table_columns(db, "generation_previews")
    if "shoot_script_id" not in columns and "environment_asset_id" in columns:
        return
    source_columns = [
        "id", "task_id", "styling_id", "input_version_id", "user_instruction_json",
        "preview_asset_id", "environment_asset_id", "status", "active_attempt_id",
        "locked_by", "locked_until", "run_attempts", "created_at", "updated_at",
    ]
    select_exprs = [column if column in columns else default_sql_for_preview_column(column) for column in source_columns]

    def rebuild() -> None:
        db.executescript(
            """
            CREATE TABLE generation_previews_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              styling_id INTEGER NOT NULL REFERENCES generation_stylings(id),
              input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
              user_instruction_json TEXT,
              preview_asset_id INTEGER REFERENCES media_assets(id),
              environment_asset_id INTEGER REFERENCES media_assets(id),
              status TEXT NOT NULL DEFAULT 'QUEUED',
              active_attempt_id INTEGER,
              locked_by TEXT,
              locked_until TEXT,
              run_attempts INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        db.execute(
            f"""
            INSERT INTO generation_previews_new ({", ".join(source_columns)})
            SELECT {", ".join(select_exprs)}
            FROM generation_previews
            """
        )
        db.executescript(
            """
            DROP TABLE generation_previews;
            ALTER TABLE generation_previews_new RENAME TO generation_previews;
            """
        )

    run_schema_rebuild_with_foreign_keys_disabled(db, rebuild)


def default_sql_for_preview_column(column: str) -> str:
    if column == "status":
        return "'QUEUED'"
    if column == "run_attempts":
        return "0"
    if column in {"created_at", "updated_at"}:
        return f"'{now_iso()}'"
    if column == "user_instruction_json":
        return "'{}'"
    return "NULL"


def rebuild_generation_result_sets_final(db: sqlite3.Connection) -> None:
    if not table_exists(db, "generation_result_sets"):
        return
    columns = table_columns(db, "generation_result_sets")
    if "shoot_script_id" not in columns and "preview_id" in columns:
        return
    def rebuild() -> None:
        db.executescript(
            """
            CREATE TABLE generation_result_sets_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
              styling_id INTEGER NOT NULL REFERENCES generation_stylings(id),
              preview_id INTEGER NOT NULL REFERENCES generation_previews(id),
              user_instruction_json TEXT,
              status TEXT NOT NULL DEFAULT 'RUNNING',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        if "preview_id" in columns:
            source_columns = [
                "id", "task_id", "input_version_id", "styling_id", "preview_id",
                "user_instruction_json", "status", "created_at", "updated_at",
            ]
            select_exprs = [column if column in columns else default_sql_for_result_set_column(column) for column in source_columns]
            db.execute(
                f"""
                INSERT INTO generation_result_sets_new ({", ".join(source_columns)})
                SELECT {", ".join(select_exprs)}
                FROM generation_result_sets
                WHERE preview_id IS NOT NULL
                """
            )
        db.executescript(
            """
            DROP TABLE generation_result_sets;
            ALTER TABLE generation_result_sets_new RENAME TO generation_result_sets;
            """
        )

    run_schema_rebuild_with_foreign_keys_disabled(db, rebuild)


def run_schema_rebuild_with_foreign_keys_disabled(db: sqlite3.Connection, rebuild: Any) -> None:
    """Run SQLite table rebuilds without triggering child-table cascade deletes.

    SQLite only allows PRAGMA foreign_keys to change outside an active transaction.
    The final-workflow migration rebuilds parent tables, so disabling foreign keys
    inside the already-open migration transaction is unsafe: the subsequent
    DROP TABLE can cascade-delete generation history rows. Commit first, rebuild
    with enforcement disabled, then re-enable enforcement for normal runtime.
    """
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        rebuild()
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")


def default_sql_for_result_set_column(column: str) -> str:
    if column == "status":
        return "'RUNNING'"
    if column in {"created_at", "updated_at"}:
        return f"'{now_iso()}'"
    if column == "user_instruction_json":
        return "'{}'"
    return "NULL"


def rebuild_generation_result_images_final(db: sqlite3.Connection) -> None:
    if not table_exists(db, "generation_result_images_new"):
        return
    columns = table_columns(db, "generation_result_images_new")
    removed = {"shot_plan_id"}
    required = {"shot_role", "user_instruction_json"}
    if not removed.intersection(columns) and required.issubset(columns):
        return
    source_columns = [
        "id", "result_set_id", "shot_role", "sort_order", "status", "current_asset_id",
        "user_instruction_json", "active_attempt_id", "locked_by", "locked_until",
        "run_attempts", "created_at", "updated_at",
    ]
    select_exprs = [column if column in columns else default_sql_for_result_image_column(column) for column in source_columns]

    def rebuild() -> None:
        db.executescript(
            """
            CREATE TABLE generation_result_images_new_final (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              result_set_id INTEGER NOT NULL REFERENCES generation_result_sets(id) ON DELETE CASCADE,
              shot_role TEXT NOT NULL DEFAULT '',
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
            """
        )
        db.execute(
            f"""
            INSERT INTO generation_result_images_new_final ({", ".join(source_columns)})
            SELECT {", ".join(select_exprs)}
            FROM generation_result_images_new
            """
        )
        db.executescript(
            """
            DROP TABLE generation_result_images_new;
            ALTER TABLE generation_result_images_new_final RENAME TO generation_result_images_new;
            """
        )

    run_schema_rebuild_with_foreign_keys_disabled(db, rebuild)


def default_sql_for_result_image_column(column: str) -> str:
    if column == "shot_role":
        return "('legacy_grid_' || sort_order)"
    if column == "status":
        return "'QUEUED'"
    if column == "run_attempts":
        return "0"
    if column in {"created_at", "updated_at"}:
        return f"'{now_iso()}'"
    if column == "user_instruction_json":
        return "'{}'"
    return "NULL"


def ensure_prompt_config_catalog(db: sqlite3.Connection) -> None:
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
    placeholders = ", ".join("?" for _ in REMOVED_PROMPT_CONFIG_KEYS)
    db.execute(f"DELETE FROM prompt_configs WHERE key IN ({placeholders})", REMOVED_PROMPT_CONFIG_KEYS)


def table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(db, table):
        return set()
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def table_has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    return column in table_columns(db, table)


def table_unique_column_sets(db: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    if not table_exists(db, table):
        return set()
    unique_columns: set[tuple[str, ...]] = set()
    for index in db.execute(f"PRAGMA index_list({table})").fetchall():
        if not index["unique"]:
            continue
        columns = tuple(row["name"] for row in db.execute(f"PRAGMA index_info({index['name']})").fetchall())
        unique_columns.add(columns)
    return unique_columns


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
        "headReferences": {},
        "headReferenceOrder": [],
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
    settings.log_dir.mkdir(parents=True, exist_ok=True)
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
