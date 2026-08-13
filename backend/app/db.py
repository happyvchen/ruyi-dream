from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator

from .config import get_settings


MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("050", "finalize_direct_prompt_generation_workflow", "migration_finalize_direct_prompt_generation_workflow"),
    ("051", "remove_hero_product_info", "migration_remove_hero_product_info"),
    ("052", "upload_sessions", "migration_upload_sessions"),
    ("053", "upload_session_business_metadata", "migration_upload_session_business_metadata"),
    ("054", "resumable_styling_imports", "migration_resumable_styling_imports"),
    ("055", "upload_session_execution_lease", "migration_upload_session_execution_lease"),
    ("056", "upload_session_content_checksum", "migration_upload_session_content_checksum"),
    ("057", "styling_garment_tag_error_code", "migration_styling_garment_tag_error_code"),
    ("058", "upload_session_archiving", "migration_upload_session_archiving"),
    ("059", "upload_object_cleanup_retry", "migration_upload_object_cleanup_retry"),
    ("060", "upload_object_cleanup_claim", "migration_upload_object_cleanup_claim"),
    ("061", "upload_session_batch_queries", "migration_upload_session_batch_queries"),
    ("062", "generation_auto_run_retry", "migration_generation_auto_run_retry"),
    ("063", "styling_garment_tag_retry", "migration_styling_garment_tag_retry"),
    ("064", "generation_attempt_owner_status_index", "migration_generation_attempt_owner_status_index"),
    ("065", "repair_styling_garment_tag_retry_lease", "migration_repair_styling_garment_tag_retry_lease"),
    ("066", "direct_final_edits_workflow", "migration_direct_final_edits_workflow"),
    ("067", "shot_preset_family_publication", "migration_shot_preset_family_publication"),
    ("068", "repair_direct_final_result_set_schema", "migration_repair_direct_final_result_set_schema"),
    ("069", "no_mask_master_shot_presets", "migration_no_mask_master_shot_presets"),
    ("070", "final_prompt_master_shot_contract", "migration_final_prompt_master_shot_contract"),
    ("071", "refresh_legacy_top_final_prompt", "migration_refresh_legacy_top_final_prompt"),
    ("072", "live_shot_preset_slots", "migration_live_shot_preset_slots"),
    ("073", "refresh_legacy_bottom_and_one_piece_final_prompts", "migration_refresh_legacy_bottom_and_one_piece_final_prompts"),
)

# Prompt 维护页只保留当前运行链路会读取的配置槽。Prompt 正文不写死在代码里；
# 代码只登记槽位、拍摄图位族路由和动态事实占位，真正的黄金 Prompt 由页面维护。
PROMPT_CONFIG_CATALOG: tuple[tuple[str, str, str, str, int], ...] = (
    ("segment.brand_info", "segment", "品牌信息", "品牌信息分段 Prompt，可被组装 Prompt 引用。", 10),
    ("assembly.garment_image_selection", "assembly", "搭配服装图片挑选/排序", "搭配服装入库时挑选真实商品参考图并排序的 Prompt；运行时与 AI 打标合并调用。", 90),
    ("assembly.ai_tagging", "assembly", "AI 打标", "搭配服装入库后的图片理解和标签识别 Prompt；运行时与图片挑选/排序合并调用。", 100),
    ("assembly.hero_image_selection", "assembly", "主推款图片挑选/排序", "AI 摄影棚主推款图片挑选和排序 Prompt；运行时与 AI 选搭合并调用。", 105),
    ("assembly.ai_styling", "assembly", "AI 选搭", "AI Stylist 选搭 Prompt，含主推款理解、搭配选择和质量判断；运行时与主推款图片挑选/排序合并调用。", 110),
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
    "assembly.storyboard_preview_top",
    "assembly.storyboard_preview_bottom",
    "assembly.storyboard_preview_one_piece",
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

            CREATE TABLE IF NOT EXISTS shot_preset_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              version_number INTEGER NOT NULL UNIQUE,
              status TEXT NOT NULL DEFAULT 'DRAFT',
              published_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shot_presets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              version_id INTEGER NOT NULL REFERENCES shot_preset_versions(id) ON DELETE CASCADE,
              category_family TEXT NOT NULL,
              shot_index INTEGER NOT NULL,
              background_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
              mask_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
              pose_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(version_id, category_family, shot_index),
              CHECK(category_family IN ('top', 'bottom', 'one_piece')),
              CHECK(shot_index BETWEEN 1 AND 5)
            );

            CREATE TABLE IF NOT EXISTS retired_prompt_configs (
              key TEXT PRIMARY KEY,
              content TEXT NOT NULL,
              retired_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shot_preset_family_publications (
              category_family TEXT PRIMARY KEY,
              version_id INTEGER NOT NULL REFERENCES shot_preset_versions(id),
              published_at TEXT NOT NULL,
              CHECK(category_family IN ('top', 'bottom', 'one_piece'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_shot_preset_versions_single_draft
              ON shot_preset_versions(status) WHERE status = 'DRAFT';

            CREATE TABLE IF NOT EXISTS shot_preset_slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              category_family TEXT NOT NULL,
              shot_index INTEGER NOT NULL,
              background_asset_id INTEGER REFERENCES media_assets(id),
              pose_asset_id INTEGER REFERENCES media_assets(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(category_family, shot_index),
              CHECK(category_family IN ('top', 'bottom', 'one_piece')),
              CHECK(shot_index BETWEEN 1 AND 5)
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
              tag_error_code TEXT,
              tag_retry_at TEXT,
              tag_retry_state_json TEXT,
              tag_active_attempt_id INTEGER REFERENCES generation_attempts(id),
              tag_retry_lease_until TEXT,
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
              auto_run_retry_at TEXT,
              auto_run_retry_count INTEGER NOT NULL DEFAULT 0,
              batch_id TEXT,
              lease_owner TEXT,
              lease_expires_at TEXT,
              archived_at TEXT,
              archive_reason TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS generation_batches (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
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
              preview_id INTEGER REFERENCES generation_previews(id),
              shot_preset_version_id INTEGER REFERENCES shot_preset_versions(id),
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

            CREATE TABLE IF NOT EXISTS generation_result_set_shot_presets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              result_set_id INTEGER NOT NULL REFERENCES generation_result_sets(id) ON DELETE CASCADE,
              category_family TEXT NOT NULL,
              shot_index INTEGER NOT NULL,
              background_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
              pose_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
              created_at TEXT NOT NULL,
              UNIQUE(result_set_id, shot_index),
              CHECK(category_family IN ('top', 'bottom', 'one_piece')),
              CHECK(shot_index BETWEEN 1 AND 5)
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

            CREATE TABLE IF NOT EXISTS upload_sessions (
              id TEXT PRIMARY KEY,
              scope TEXT NOT NULL,
              status TEXT NOT NULL,
              business_code TEXT,
              source_title TEXT,
              batch_id TEXT,
              lease_owner TEXT,
              lease_expires_at TEXT,
              total_count INTEGER NOT NULL DEFAULT 0,
              uploaded_count INTEGER NOT NULL DEFAULT 0,
              target_type TEXT,
              target_id INTEGER,
              error_message TEXT,
              archived_at TEXT,
              archive_reason TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS upload_session_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL REFERENCES upload_sessions(id) ON DELETE CASCADE,
              sort_order INTEGER NOT NULL DEFAULT 0,
              object_key TEXT NOT NULL UNIQUE,
              filename TEXT NOT NULL,
              mime_type TEXT NOT NULL,
              size_bytes INTEGER,
              relative_path TEXT,
              last_modified INTEGER,
              checksum_sha256 TEXT,
              status TEXT NOT NULL DEFAULT 'PENDING',
              media_asset_id INTEGER REFERENCES media_assets(id),
              error_message TEXT,
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
        "migration_remove_hero_product_info": migration_remove_hero_product_info,
        "migration_upload_sessions": migration_upload_sessions,
        "migration_upload_session_business_metadata": migration_upload_session_business_metadata,
        "migration_resumable_styling_imports": migration_resumable_styling_imports,
        "migration_upload_session_execution_lease": migration_upload_session_execution_lease,
        "migration_upload_session_content_checksum": migration_upload_session_content_checksum,
        "migration_styling_garment_tag_error_code": migration_styling_garment_tag_error_code,
        "migration_upload_session_archiving": migration_upload_session_archiving,
        "migration_upload_object_cleanup_retry": migration_upload_object_cleanup_retry,
        "migration_upload_object_cleanup_claim": migration_upload_object_cleanup_claim,
        "migration_upload_session_batch_queries": migration_upload_session_batch_queries,
        "migration_generation_auto_run_retry": migration_generation_auto_run_retry,
        "migration_styling_garment_tag_retry": migration_styling_garment_tag_retry,
        "migration_generation_attempt_owner_status_index": migration_generation_attempt_owner_status_index,
        "migration_repair_styling_garment_tag_retry_lease": migration_repair_styling_garment_tag_retry_lease,
        "migration_direct_final_edits_workflow": migration_direct_final_edits_workflow,
        "migration_shot_preset_family_publication": migration_shot_preset_family_publication,
        "migration_repair_direct_final_result_set_schema": migration_repair_direct_final_result_set_schema,
        "migration_no_mask_master_shot_presets": migration_no_mask_master_shot_presets,
        "migration_final_prompt_master_shot_contract": migration_final_prompt_master_shot_contract,
        "migration_refresh_legacy_top_final_prompt": migration_refresh_legacy_top_final_prompt,
        "migration_live_shot_preset_slots": migration_live_shot_preset_slots,
        "migration_refresh_legacy_bottom_and_one_piece_final_prompts": migration_refresh_legacy_bottom_and_one_piece_final_prompts,
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
    导入主推款 -> AI 智能选搭 -> 三类固定 Prompt 生成分镜预览 -> 前 5 格生成成片。
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


def migration_remove_hero_product_info(db: sqlite3.Connection) -> None:
    """移除导入阶段历史补充文本字段和输入版本冻结字段。"""
    rebuild_generation_tasks_final(db)
    rebuild_generation_task_input_versions_without_hero_info(db)
    scrub_prompt_config_hero_product_info(db)


def migration_upload_sessions(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS upload_sessions (
          id TEXT PRIMARY KEY,
          scope TEXT NOT NULL,
          status TEXT NOT NULL,
          business_code TEXT,
          source_title TEXT,
          total_count INTEGER NOT NULL DEFAULT 0,
          uploaded_count INTEGER NOT NULL DEFAULT 0,
          target_type TEXT,
          target_id INTEGER,
          error_message TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS upload_session_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL REFERENCES upload_sessions(id) ON DELETE CASCADE,
          sort_order INTEGER NOT NULL DEFAULT 0,
          object_key TEXT NOT NULL UNIQUE,
          filename TEXT NOT NULL,
          mime_type TEXT NOT NULL,
          size_bytes INTEGER,
          status TEXT NOT NULL DEFAULT 'PENDING',
          media_asset_id INTEGER REFERENCES media_assets(id),
          error_message TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )


def migration_upload_session_business_metadata(db: sqlite3.Connection) -> None:
    if not table_has_column(db, "upload_sessions", "business_code"):
        db.execute("ALTER TABLE upload_sessions ADD COLUMN business_code TEXT")
    if not table_has_column(db, "upload_sessions", "source_title"):
        db.execute("ALTER TABLE upload_sessions ADD COLUMN source_title TEXT")


def migration_resumable_styling_imports(db: sqlite3.Connection) -> None:
    if not table_has_column(db, "upload_sessions", "batch_id"):
        db.execute("ALTER TABLE upload_sessions ADD COLUMN batch_id TEXT")
    if not table_has_column(db, "upload_session_items", "relative_path"):
        db.execute("ALTER TABLE upload_session_items ADD COLUMN relative_path TEXT")
    if not table_has_column(db, "upload_session_items", "last_modified"):
        db.execute("ALTER TABLE upload_session_items ADD COLUMN last_modified INTEGER")
    db.execute("CREATE INDEX IF NOT EXISTS idx_upload_sessions_batch_id ON upload_sessions(batch_id)")


def migration_upload_session_execution_lease(db: sqlite3.Connection) -> None:
    if not table_has_column(db, "upload_sessions", "lease_owner"):
        db.execute("ALTER TABLE upload_sessions ADD COLUMN lease_owner TEXT")
    if not table_has_column(db, "upload_sessions", "lease_expires_at"):
        db.execute("ALTER TABLE upload_sessions ADD COLUMN lease_expires_at TEXT")


def migration_upload_session_content_checksum(db: sqlite3.Connection) -> None:
    if not table_has_column(db, "upload_session_items", "checksum_sha256"):
        db.execute("ALTER TABLE upload_session_items ADD COLUMN checksum_sha256 TEXT")


def migration_styling_garment_tag_error_code(db: sqlite3.Connection) -> None:
    if table_exists(db, "styling_garments") and not table_has_column(db, "styling_garments", "tag_error_code"):
        db.execute("ALTER TABLE styling_garments ADD COLUMN tag_error_code TEXT")


def migration_upload_session_archiving(db: sqlite3.Connection) -> None:
    if not table_has_column(db, "upload_sessions", "archived_at"):
        db.execute("ALTER TABLE upload_sessions ADD COLUMN archived_at TEXT")
    if not table_has_column(db, "upload_sessions", "archive_reason"):
        db.execute("ALTER TABLE upload_sessions ADD COLUMN archive_reason TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_upload_sessions_archived_at ON upload_sessions(archived_at)")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_object_cleanup_queue (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          object_key TEXT NOT NULL UNIQUE,
          source_session_id TEXT,
          error_message TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          claim_token TEXT,
          claimed_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_upload_object_cleanup_queue_updated_at ON upload_object_cleanup_queue(updated_at)")


def migration_upload_session_batch_queries(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_upload_sessions_batch_visibility
        ON upload_sessions(scope, archived_at, archive_reason, created_at)
        """
    )
    if {"scope", "target_type", "target_id", "archived_at", "created_at"}.issubset(table_columns(db, "upload_sessions")):
        db.execute("DROP INDEX IF EXISTS idx_upload_sessions_styling_target")
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_upload_sessions_styling_target
            ON upload_sessions(scope, target_type, target_id, archived_at, created_at)
            """
        )


def migration_upload_object_cleanup_retry(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_object_cleanup_queue (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          object_key TEXT NOT NULL UNIQUE,
          source_session_id TEXT,
          error_message TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          claim_token TEXT,
          claimed_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    if not table_has_column(db, "upload_object_cleanup_queue", "attempt_count"):
        db.execute("ALTER TABLE upload_object_cleanup_queue ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
    db.execute("CREATE INDEX IF NOT EXISTS idx_upload_object_cleanup_queue_updated_at ON upload_object_cleanup_queue(updated_at)")


def migration_upload_object_cleanup_claim(db: sqlite3.Connection) -> None:
    if not table_has_column(db, "upload_object_cleanup_queue", "claim_token"):
        db.execute("ALTER TABLE upload_object_cleanup_queue ADD COLUMN claim_token TEXT")
    if not table_has_column(db, "upload_object_cleanup_queue", "claimed_at"):
        db.execute("ALTER TABLE upload_object_cleanup_queue ADD COLUMN claimed_at TEXT")


def migration_generation_auto_run_retry(db: sqlite3.Connection) -> None:
    if not table_exists(db, "generation_tasks"):
        return
    if not table_has_column(db, "generation_tasks", "auto_run_retry_at"):
        db.execute("ALTER TABLE generation_tasks ADD COLUMN auto_run_retry_at TEXT")
    if not table_has_column(db, "generation_tasks", "auto_run_retry_count"):
        db.execute("ALTER TABLE generation_tasks ADD COLUMN auto_run_retry_count INTEGER NOT NULL DEFAULT 0")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_generation_tasks_auto_run_retry_at "
        "ON generation_tasks(auto_run_target, auto_run_retry_at)"
    )


def migration_styling_garment_tag_retry(db: sqlite3.Connection) -> None:
    if not table_exists(db, "styling_garments"):
        return
    if not table_has_column(db, "styling_garments", "tag_retry_at"):
        db.execute("ALTER TABLE styling_garments ADD COLUMN tag_retry_at TEXT")
    if not table_has_column(db, "styling_garments", "tag_retry_state_json"):
        db.execute("ALTER TABLE styling_garments ADD COLUMN tag_retry_state_json TEXT")
    if not table_has_column(db, "styling_garments", "tag_active_attempt_id"):
        db.execute("ALTER TABLE styling_garments ADD COLUMN tag_active_attempt_id INTEGER REFERENCES generation_attempts(id)")
    if not table_has_column(db, "styling_garments", "tag_retry_lease_until"):
        db.execute("ALTER TABLE styling_garments ADD COLUMN tag_retry_lease_until TEXT")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_styling_garments_tag_retry_at "
        "ON styling_garments(status, tag_retry_at)"
    )


def migration_generation_attempt_owner_status_index(db: sqlite3.Connection) -> None:
    if not table_exists(db, "generation_attempts"):
        return
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_generation_attempts_owner_status "
        "ON generation_attempts(owner_type, owner_id, status)"
    )


def migration_repair_styling_garment_tag_retry_lease(db: sqlite3.Connection) -> None:
    """修复开发期 063 已登记、但租约字段尚未落库的历史数据库。"""
    if not table_exists(db, "styling_garments"):
        return
    if not table_has_column(db, "styling_garments", "tag_retry_lease_until"):
        db.execute("ALTER TABLE styling_garments ADD COLUMN tag_retry_lease_until TEXT")


def migration_direct_final_edits_workflow(db: sqlite3.Connection) -> None:
    """Add versioned 3×5 shot presets and retire preview Prompt slots."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS shot_preset_versions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version_number INTEGER NOT NULL UNIQUE,
          status TEXT NOT NULL DEFAULT 'DRAFT',
          published_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shot_presets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version_id INTEGER NOT NULL REFERENCES shot_preset_versions(id) ON DELETE CASCADE,
          category_family TEXT NOT NULL,
          shot_index INTEGER NOT NULL,
          background_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
          mask_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
          pose_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(version_id, category_family, shot_index),
          CHECK(category_family IN ('top', 'bottom', 'one_piece')),
          CHECK(shot_index BETWEEN 1 AND 5)
        );
        CREATE INDEX IF NOT EXISTS idx_shot_preset_versions_status
          ON shot_preset_versions(status, version_number DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shot_preset_versions_single_draft
          ON shot_preset_versions(status) WHERE status = 'DRAFT';
        CREATE INDEX IF NOT EXISTS idx_shot_presets_version_slot
          ON shot_presets(version_id, category_family, shot_index);
        CREATE TABLE IF NOT EXISTS retired_prompt_configs (
          key TEXT PRIMARY KEY,
          content TEXT NOT NULL,
          retired_at TEXT NOT NULL
        );
        """
    )
    if table_exists(db, "prompt_configs"):
        keys = (
            "assembly.storyboard_preview_top",
            "assembly.storyboard_preview_bottom",
            "assembly.storyboard_preview_one_piece",
        )
        timestamp = now_iso()
        db.execute(
            f"""
            INSERT INTO retired_prompt_configs (key, content, retired_at)
            SELECT key, content, ? FROM prompt_configs WHERE key IN ({','.join('?' for _ in keys)})
            ON CONFLICT(key) DO UPDATE SET content = excluded.content, retired_at = excluded.retired_at
            """,
            (timestamp, *keys),
        )
        db.execute(
            f"DELETE FROM prompt_configs WHERE key IN ({','.join('?' for _ in keys)})",
            keys,
        )
    rebuild_generation_result_sets_direct_edits(db)


def migration_shot_preset_family_publication(db: sqlite3.Connection) -> None:
    """Make published shot-preset snapshots independently addressable per family."""
    db.execute("DROP INDEX IF EXISTS idx_shot_preset_versions_single_published")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS shot_preset_family_publications (
          category_family TEXT PRIMARY KEY,
          version_id INTEGER NOT NULL REFERENCES shot_preset_versions(id),
          published_at TEXT NOT NULL,
          CHECK(category_family IN ('top', 'bottom', 'one_piece'))
        );
        """
    )
    published_rows = db.execute(
        "SELECT id, published_at, updated_at FROM shot_preset_versions WHERE status = 'PUBLISHED' ORDER BY version_number"
    ).fetchall()
    for version in published_rows:
        families = db.execute(
            "SELECT DISTINCT category_family FROM shot_presets WHERE version_id = ?",
            (int(version["id"]),),
        ).fetchall()
        for family in families:
            db.execute(
                """
                INSERT INTO shot_preset_family_publications (category_family, version_id, published_at)
                VALUES (?, ?, ?)
                ON CONFLICT(category_family) DO UPDATE SET
                  version_id = excluded.version_id,
                  published_at = excluded.published_at
                """,
                (str(family["category_family"]), int(version["id"]), version["published_at"] or version["updated_at"]),
            )


def migration_repair_direct_final_result_set_schema(db: sqlite3.Connection) -> None:
    """Repair databases that recorded 066 before its result-set rebuild was added."""
    rebuild_generation_result_sets_direct_edits(db)


def migration_no_mask_master_shot_presets(db: sqlite3.Connection) -> None:
    """Allow new master-shot presets without a mask while preserving historical rows."""
    if not table_exists(db, "shot_presets") or "mask_asset_id" not in table_columns(db, "shot_presets"):
        return
    db.executescript(
        """
        CREATE TABLE shot_presets_no_mask (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version_id INTEGER NOT NULL REFERENCES shot_preset_versions(id) ON DELETE CASCADE,
          category_family TEXT NOT NULL,
          shot_index INTEGER NOT NULL,
          background_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
          mask_asset_id INTEGER REFERENCES media_assets(id),
          pose_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(version_id, category_family, shot_index),
          CHECK(category_family IN ('top', 'bottom', 'one_piece')),
          CHECK(shot_index BETWEEN 1 AND 5)
        );
        INSERT INTO shot_presets_no_mask
          (id, version_id, category_family, shot_index, background_asset_id, mask_asset_id, pose_asset_id, created_at, updated_at)
        SELECT id, version_id, category_family, shot_index, background_asset_id, mask_asset_id, pose_asset_id, created_at, updated_at
        FROM shot_presets;
        DROP TABLE shot_presets;
        ALTER TABLE shot_presets_no_mask RENAME TO shot_presets;
        CREATE INDEX IF NOT EXISTS idx_shot_presets_version_slot
          ON shot_presets(version_id, category_family, shot_index);
        """
    )


MASTER_SHOT_PROMPT_CONTRACT = """【拍摄母版替换规则】
- {{runtime.ref_shot_background}} 是当前镜头的拍摄母版。完整移除并替换母版中的原人物、原服装及其身份特征；不得混入原人物的脸、发型、肤色、服装、配饰或 logo。
- 继承拍摄母版的场景、机位、景别、裁切、人物尺度、站位与光线；{{runtime.ref_shot_pose}} 仅补充身体朝向、重心、肩线、手位、视线与头部朝向，不提供人物身份或服装事实，也不得改变脸、发型或母版构图。
- {{runtime.ref_model_identity}} 是唯一模特身份事实源；{{runtime.ref_garment}} 是唯一商品事实源。优先级：商品和人物身份 > 当前镜头构图契约 > 母版场景与光线 > 姿势参考 > 商业画质提升。
- 使用真实、干净、连续、均匀的商业棚拍成像；不要使用电影化调色、胶片颗粒、做旧、纹理化、磨砂或画布效果，避免噪点、云斑、色斑、波纹、摩尔纹、局部色漂与不真实纹理覆盖。

"""

LEGACY_FINAL_PROMPT_PATTERNS = (
    re.compile(r"(?:-\s*)?\{\{runtime\.ref_preview_grid\}\}[^\n]*(?:\n|$)"),
    re.compile(r"(?:-\s*)?\{\{#runtime\.ref_shoot_environment\}\}[^\n]*(?:\n|$)"),
    re.compile(r"(?:-\s*)?\{\{runtime\.ref_shoot_environment\}\}[^\n]*(?:\n|$)"),
    re.compile(r"(?:仅|只)?在(?:遮罩)?(?:透明|可编辑)区域(?:内)?(?:生成|编辑)[^。；\n]*[。；]?"),
    re.compile(r"(?:遮罩(?:非透明)?区(?:域)?|背景(?:其余|遮罩外)?区域)[^。；\n]*(?:原样|原像素|保持不变|不变)[^。；\n]*[。；]?"),
    re.compile(r"沿人物轮廓(?:进行)?编辑[^。；\n]*[。；]?"),
)


def migration_final_prompt_master_shot_contract(db: sqlite3.Connection) -> None:
    """Prepend the no-mask master-shot rule without replacing admin Prompt content."""
    if not table_exists(db, "prompt_configs"):
        return
    keys = ("assembly.final_image_top", "assembly.final_image_bottom", "assembly.final_image_one_piece")
    rows = db.execute(
        f"SELECT key, content FROM prompt_configs WHERE key IN ({','.join('?' for _ in keys)})",
        keys,
    ).fetchall()
    for row in rows:
        content = str(row["content"] or "")
        if MASTER_SHOT_PROMPT_CONTRACT not in content:
            content = MASTER_SHOT_PROMPT_CONTRACT + content
        for pattern in LEGACY_FINAL_PROMPT_PATTERNS:
            content = pattern.sub("", content)
        content = re.sub(r"[ \t]{2,}", " ", content)
        content = re.sub(r"\n{3,}", "\n\n", content).strip() + "\n"
        db.execute("UPDATE prompt_configs SET content = ?, updated_at = ? WHERE key = ?", (content, now_iso(), row["key"]))


# 该摘要只匹配已由 070 写入、且从未被管理员修改过的旧上装系统 Prompt。
# 不匹配时一律保留原文，避免把管理员的局部规则误当作可覆盖的系统默认值。
LEGACY_TOP_FINAL_PROMPT_SHA256 = "71426fe538930755006687cf96a4f8b955294a1a812beec80254fd0412e3b855"


def canonical_top_final_prompt() -> str:
    return (Path(__file__).resolve().parents[1] / "prompt_templates" / "final_image_top.txt").read_text(encoding="utf-8").strip() + "\n"


def migration_refresh_legacy_top_final_prompt(db: sqlite3.Connection) -> None:
    """将可精确识别的旧上装系统 Prompt 升级为当前母版直出版本。"""
    if not table_exists(db, "prompt_configs"):
        return
    row = db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_top'").fetchone()
    if not row:
        return
    content = str(row["content"] or "")
    digest = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()
    if digest != LEGACY_TOP_FINAL_PROMPT_SHA256:
        return
    db.execute(
        "UPDATE prompt_configs SET content = ?, updated_at = ? WHERE key = 'assembly.final_image_top'",
        (canonical_top_final_prompt(), now_iso()),
    )


# 仅升级由 070 写入、且从未被管理员修改过的下装/套装系统 Prompt。
# 摘要不匹配时保留原文，避免覆盖任何管理员自定义内容。
LEGACY_BOTTOM_AND_ONE_PIECE_FINAL_PROMPT_SHA256: dict[str, str] = {
    "assembly.final_image_bottom": "2ecde92070900fd3d86f5378b05d523c0dbe530854aa630eebf45a95f6f37100",
    "assembly.final_image_one_piece": "586e32581a3a4758335659617fb4c61d3ffdcf43ffebe3b403d9a4b00b937a64",
}


def canonical_bottom_or_one_piece_final_prompt(key: str) -> str:
    filenames = {
        "assembly.final_image_bottom": "final_image_bottom.txt",
        "assembly.final_image_one_piece": "final_image_one_piece.txt",
    }
    filename = filenames[key]
    return (Path(__file__).resolve().parents[1] / "prompt_templates" / filename).read_text(encoding="utf-8").strip() + "\n"


def migration_refresh_legacy_bottom_and_one_piece_final_prompts(db: sqlite3.Connection) -> None:
    """将可精确识别的旧下装、套装系统 Prompt 升级为当前母版直出版本。"""
    if not table_exists(db, "prompt_configs"):
        return
    for key, expected_digest in LEGACY_BOTTOM_AND_ONE_PIECE_FINAL_PROMPT_SHA256.items():
        row = db.execute("SELECT content FROM prompt_configs WHERE key = ?", (key,)).fetchone()
        if not row:
            continue
        content = str(row["content"] or "")
        digest = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()
        if digest != expected_digest:
            continue
        db.execute(
            "UPDATE prompt_configs SET content = ?, updated_at = ? WHERE key = ?",
            (canonical_bottom_or_one_piece_final_prompt(key), now_iso(), key),
        )


def migration_live_shot_preset_slots(db: sqlite3.Connection) -> None:
    """Replace admin draft/publish maintenance with live slots and per-result-set snapshots.

    Legacy version tables remain read-only so historical result sets can still be replayed.
    """
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS shot_preset_slots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          category_family TEXT NOT NULL,
          shot_index INTEGER NOT NULL,
          background_asset_id INTEGER REFERENCES media_assets(id),
          pose_asset_id INTEGER REFERENCES media_assets(id),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(category_family, shot_index),
          CHECK(category_family IN ('top', 'bottom', 'one_piece')),
          CHECK(shot_index BETWEEN 1 AND 5)
        );
        CREATE TABLE IF NOT EXISTS generation_result_set_shot_presets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          result_set_id INTEGER NOT NULL REFERENCES generation_result_sets(id) ON DELETE CASCADE,
          category_family TEXT NOT NULL,
          shot_index INTEGER NOT NULL,
          background_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
          pose_asset_id INTEGER NOT NULL REFERENCES media_assets(id),
          created_at TEXT NOT NULL,
          UNIQUE(result_set_id, shot_index),
          CHECK(category_family IN ('top', 'bottom', 'one_piece')),
          CHECK(shot_index BETWEEN 1 AND 5)
        );
        CREATE INDEX IF NOT EXISTS idx_result_set_shot_presets_result
          ON generation_result_set_shot_presets(result_set_id, shot_index);
        """
    )
    if not table_exists(db, "shot_presets") or not table_exists(db, "shot_preset_versions"):
        return
    draft = db.execute(
        "SELECT id FROM shot_preset_versions WHERE status = 'DRAFT' ORDER BY version_number DESC LIMIT 1"
    ).fetchone()
    timestamp = now_iso()
    for family in ("top", "bottom", "one_piece"):
        published = db.execute(
            "SELECT version_id FROM shot_preset_family_publications WHERE category_family = ?",
            (family,),
        ).fetchone() if table_exists(db, "shot_preset_family_publications") else None
        for shot_index in range(1, 6):
            row = None
            if draft:
                row = db.execute(
                    "SELECT background_asset_id, pose_asset_id FROM shot_presets WHERE version_id = ? AND category_family = ? AND shot_index = ?",
                    (int(draft["id"]), family, shot_index),
                ).fetchone()
            if not row and published:
                row = db.execute(
                    "SELECT background_asset_id, pose_asset_id FROM shot_presets WHERE version_id = ? AND category_family = ? AND shot_index = ?",
                    (int(published["version_id"]), family, shot_index),
                ).fetchone()
            if row:
                db.execute(
                    """
                    INSERT INTO shot_preset_slots
                      (category_family, shot_index, background_asset_id, pose_asset_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(category_family, shot_index) DO NOTHING
                    """,
                    (family, shot_index, row["background_asset_id"], row["pose_asset_id"], timestamp, timestamp),
                )


def rebuild_generation_result_sets_direct_edits(db: sqlite3.Connection) -> None:
    if not table_exists(db, "generation_result_sets"):
        return
    columns = table_columns(db, "generation_result_sets")
    info = {str(row["name"]): row for row in db.execute("PRAGMA table_info(generation_result_sets)").fetchall()}
    if "shot_preset_version_id" in columns and not int(info.get("preview_id", {"notnull": 0})["notnull"] or 0):
        return

    def rebuild() -> None:
        db.executescript(
            """
            CREATE TABLE generation_result_sets_direct (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
              input_version_id INTEGER NOT NULL REFERENCES generation_task_input_versions(id),
              styling_id INTEGER NOT NULL REFERENCES generation_stylings(id),
              preview_id INTEGER REFERENCES generation_previews(id),
              shot_preset_version_id INTEGER REFERENCES shot_preset_versions(id),
              user_instruction_json TEXT,
              status TEXT NOT NULL DEFAULT 'RUNNING',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        source_columns = [
            "id", "task_id", "input_version_id", "styling_id", "preview_id",
            "shot_preset_version_id", "user_instruction_json", "status", "created_at", "updated_at",
        ]
        select_exprs = [column if column in columns else "NULL" if column == "shot_preset_version_id" else default_sql_for_result_set_column(column) for column in source_columns]
        db.execute(
            f"INSERT INTO generation_result_sets_direct ({', '.join(source_columns)}) SELECT {', '.join(select_exprs)} FROM generation_result_sets"
        )
        db.executescript(
            """
            DROP TABLE generation_result_sets;
            ALTER TABLE generation_result_sets_direct RENAME TO generation_result_sets;
            """
        )

    run_schema_rebuild_with_foreign_keys_disabled(db, rebuild)


def scrub_prompt_config_hero_product_info(db: sqlite3.Connection) -> None:
    if not table_exists(db, "prompt_configs"):
        return
    rows = db.execute("SELECT key, content FROM prompt_configs WHERE content LIKE '%hero_product_info%'").fetchall()
    replacements = (
        ("{{#runtime.hero_product_info}}服装信息：{{runtime.hero_product_info}}\n{{/runtime.hero_product_info}}", ""),
        ("{{#runtime.hero_product_info}}服装信息：{{runtime.hero_product_info}}\r\n{{/runtime.hero_product_info}}", ""),
        ("{{#runtime.hero_product_info}}商品信息：{{runtime.hero_product_info}}\n{{/runtime.hero_product_info}}", ""),
        ("{{#runtime.hero_product_info}}商品信息：{{runtime.hero_product_info}}\r\n{{/runtime.hero_product_info}}", ""),
        ("{{#runtime.hero_product_info}}- 服装信息：{{runtime.hero_product_info}}\n{{/runtime.hero_product_info}}", ""),
        ("{{#runtime.hero_product_info}}- 服装信息：{{runtime.hero_product_info}}\r\n{{/runtime.hero_product_info}}", ""),
        ("{{#runtime.hero_product_info}}{{runtime.hero_product_info}}{{/runtime.hero_product_info}}", ""),
        ("商品信息：{{runtime.hero_product_info}} 补充要求：", "补充要求："),
        ("商品与造型：{{runtime.hero_product_info}}\n", ""),
        ("商品与造型：{{runtime.hero_product_info}}\r\n", ""),
        ("商品信息：{{runtime.hero_product_info}}\n", ""),
        ("商品信息：{{runtime.hero_product_info}}\r\n", ""),
        ("商品信息", "商品事实"),
    )
    for row in rows:
        content = row["content"] or ""
        next_content = content
        for old, new in replacements:
            next_content = next_content.replace(old, new)
        if next_content != content:
            db.execute("UPDATE prompt_configs SET content = ?, updated_at = ? WHERE key = ?", (next_content, now_iso(), row["key"]))


def rebuild_generation_tasks_final(db: sqlite3.Connection) -> None:
    if not table_exists(db, "generation_tasks"):
        return
    columns = table_columns(db, "generation_tasks")
    removed = {"shoot_script_user_instruction_text", "active_shoot_script_id", "hero_product_info_text"}
    required = {"storyboard_preview_user_instruction_text", "final_image_user_instruction_text", "active_storyboard_preview_id", "batch_id"}
    if not removed.intersection(columns) and required.issubset(columns):
        return
    source_columns = [
        "id", "name", "status", "user_requirement_text",
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


def rebuild_generation_task_input_versions_without_hero_info(db: sqlite3.Connection) -> None:
    if not table_exists(db, "generation_task_input_versions"):
        return
    columns = table_columns(db, "generation_task_input_versions")
    if "hero_product_info_json" not in columns:
        return
    source_columns = [
        "id", "task_id", "version_number", "name_snapshot", "image_snapshot_json",
        "model_snapshot_json", "user_requirement_json", "hero_visual_summary", "created_at",
    ]
    select_exprs = [column if column in columns else default_sql_for_input_version_column(column) for column in source_columns]

    def rebuild() -> None:
        db.executescript(
            """
            CREATE TABLE generation_task_input_versions_new (
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
            """
        )
        db.execute(
            f"""
            INSERT INTO generation_task_input_versions_new ({", ".join(source_columns)})
            SELECT {", ".join(select_exprs)}
            FROM generation_task_input_versions
            """
        )
        db.executescript(
            """
            DROP TABLE generation_task_input_versions;
            ALTER TABLE generation_task_input_versions_new RENAME TO generation_task_input_versions;
            """
        )

    run_schema_rebuild_with_foreign_keys_disabled(db, rebuild)


def default_sql_for_input_version_column(column: str) -> str:
    if column in {"name_snapshot"}:
        return "''"
    if column in {"image_snapshot_json"}:
        return "'[]'"
    if column in {"created_at"}:
        return f"'{now_iso()}'"
    return "NULL"


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
