from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import json
import sqlite3
from typing import Any, Iterator

from .config import get_settings


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
              planner_provider TEXT NOT NULL,
              raw_response TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
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
            """
        )
        seed_default_model(db)


def seed_default_model(db: sqlite3.Connection) -> None:
    existing = db.execute(
        "SELECT id FROM model_profiles WHERE model_id = ?", ("Face-01",)
    ).fetchone()
    if existing:
        return

    settings = get_settings()
    profile = {
        "height": "170cm",
        "bodyType": "标准偏瘦",
        "shoulder": "标准",
        "legRatio": "标准偏长",
        "skinTone": "自然白皙",
        "hair": "短棕色 bob，轻薄刘海",
        "expression": "自然、克制",
        "temperament": "都市、通勤、轻熟",
        "headReferences": {
            "front": "front.png",
            "frontAlt": "front_2.png",
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
            str(settings.default_model_asset_dir),
            dumps(profile),
            timestamp,
            timestamp,
        ),
    )


def media_url(path: str | Path | None) -> str | None:
    if not path:
        return None
    settings = get_settings()
    resolved = Path(path).resolve()
    try:
        relative = resolved.relative_to(settings.storage_dir.resolve())
        return f"{settings.media_url_prefix}/{relative.as_posix()}"
    except ValueError:
        return None


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
    log_path = settings.storage_dir / "logs" / f"{timestamp[:10]}.jsonl"
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

