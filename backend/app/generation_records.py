"""Persistent published image versions and their actual per-image input evidence.

This module performs database work only. Backfills never read objects, invoke providers,
rewrite old attempts, or infer a shot's model from a mutable result-set input pointer.
"""
from __future__ import annotations

import json
from typing import Any


def json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def executable_package(value: dict[str, Any]) -> bool:
    return bool(isinstance(value.get("prompt"), str) and value["prompt"].strip()
                and isinstance(value.get("imageInputs"), list) and value["imageInputs"])


def adapt_replay_references(package: dict[str, Any], max_reference_images: int = 14) -> dict[str, Any]:
    """Keep requests within the current provider budget without dropping sources.

    All but the last available slot stay independent. The remainder form a labelled board;
    the original request is retained alongside an explicit source-to-tile mapping.
    This is a transport adaptation, not a new styling/pose decision.
    """
    inputs = package.get('imageInputs', [])
    if len(inputs) <= max_reference_images:
        return package
    result = json.loads(json.dumps(package))
    result['originalImageInputs'] = result['imageInputs']
    independent_count = max_reference_images - 1
    tail = result['imageInputs'][independent_count:]
    mapping = [{'originalIndex': index, 'referenceIndex': max_reference_images, 'tile': index - independent_count,
                'role': item.get('role')} for index, item in enumerate(tail, start=max_reference_images)]
    result['referenceTransportMapping'] = mapping
    result['imageInputs'] = result['imageInputs'][:independent_count] + [{
        'role': 'retained_reference_board', 'filename': 'retained-references.jpg',
        'referenceBoardKind': 'retained_sources', 'collageItems': tail,
        'tileLabels': [f'Original image {item["originalIndex"]}' for item in mapping],
    }]
    result['originalReferencePrompt'] = result['prompt']
    result['prompt'] += (f'\n\n【参考图编号映射】前 {independent_count} 张参考图编号不变。第 {max_reference_images} 张是来源参考板，'
                         '按从左到右、从上到下排列。上文的图号保留原含义：' +
                         '；'.join(f'原图 {item["originalIndex"]} 对应参考图 {max_reference_images} 的第 {item["tile"]} 格'
                                  for item in mapping) +
                         '。各格只承担原图规定的职责，成片仍为单张照片，不输出参考板或拼图。')
    return result


def capture_published_image(db: Any, image_id: int) -> None:
    image = db.execute("SELECT * FROM generation_result_images_new WHERE id = ?", (image_id,)).fetchone()
    if not image or not image["current_asset_id"]:
        return
    # A cancelled, late provider output is evidence, never a published version.
    attempt = db.execute("""SELECT * FROM generation_attempts WHERE owner_type = 'generation_result_image'
        AND owner_id = ? AND output_asset_id = ? AND status = 'SUCCEEDED'
        ORDER BY id DESC LIMIT 1""", (image_id, image["current_asset_id"])).fetchone()
    if not attempt and db.execute("""SELECT 1 FROM generation_attempts WHERE owner_type = 'generation_result_image'
        AND owner_id = ? AND output_asset_id = ? AND status = 'CANCELLED'""",
        (image_id, image["current_asset_id"])).fetchone():
        return
    db.execute("""INSERT OR IGNORE INTO generation_image_versions
        (image_id, attempt_id, media_asset_id, request_snapshot_json, evidence_source, created_at)
        VALUES (?, ?, ?, ?, ?, ?)""", (image_id, attempt["id"] if attempt else None,
        image["current_asset_id"], attempt["request_snapshot_json"] if attempt else None,
        "attempt" if attempt else "published_asset_only",
        (attempt["completed_at"] or attempt["created_at"]) if attempt else image["updated_at"]))


def backfill_generation_records(db: Any) -> None:
    """Idempotent projection; the original IDs, rows and raw evidence stay intact."""
    db.execute("""INSERT OR IGNORE INTO generation_image_versions
        (image_id, attempt_id, media_asset_id, request_snapshot_json, evidence_source, created_at)
        SELECT i.id, a.id, a.output_asset_id, a.request_snapshot_json, 'attempt',
               COALESCE(a.completed_at, a.created_at)
        FROM generation_attempts a JOIN generation_result_images_new i ON i.id = a.owner_id
        JOIN media_assets m ON m.id = a.output_asset_id
        WHERE a.owner_type = 'generation_result_image' AND a.status = 'SUCCEEDED'
        ORDER BY a.id""")
    for row in db.execute("SELECT id, execution_snapshot_json FROM generation_result_images_new").fetchall():
        capture_published_image(db, row["id"])
        if row["execution_snapshot_json"]:
            continue
        attempts = db.execute("""SELECT request_snapshot_json FROM generation_attempts
            WHERE owner_type = 'generation_result_image' AND owner_id = ? AND status <> 'CANCELLED'
            ORDER BY id DESC""", (row["id"],)).fetchall()
        for attempt in attempts:
            if executable_package(json_object(attempt["request_snapshot_json"])):
                db.execute("UPDATE generation_result_images_new SET execution_snapshot_json = ? WHERE id = ?",
                           (attempt["request_snapshot_json"], row["id"]))
                break
    # Existing per-slot masters are facts. Copy only known references, never invent
    # a shared background, distinct poses, recognition tags or selection reasons.
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"generation_result_set_shot_presets", "shot_presets", "generation_stylings"} <= names:
        return
    db.execute("""INSERT OR IGNORE INTO generation_result_set_shot_presets
        (result_set_id, category_family, shot_index, background_asset_id, pose_asset_id, created_at)
        SELECT r.id, p.category_family, p.shot_index, p.background_asset_id, p.pose_asset_id, r.created_at
        FROM generation_result_sets r JOIN shot_presets p ON p.version_id = r.shot_preset_version_id
        JOIN generation_stylings s ON s.id = r.styling_id
        WHERE json_valid(s.styling_plan_json)
          AND p.category_family = json_extract(s.styling_plan_json, '$.hero_understanding.shot_category_family')
          AND p.background_asset_id IS NOT NULL AND p.pose_asset_id IS NOT NULL""")


def image_version_rows(db: Any, image_id: int) -> list[Any]:
    return db.execute("SELECT * FROM generation_image_versions WHERE image_id = ? ORDER BY id DESC", (image_id,)).fetchall()


def published_versions(db: Any, image: Any) -> list[dict[str, Any]]:
    """Read projection, including pre-version-table imports; never writes on GET."""
    versions = [dict(r) for r in image_version_rows(db, image['id'])]
    asset_id = image['current_asset_id']
    if asset_id and not any(v['media_asset_id'] == asset_id for v in versions):
        attempt = db.execute("""SELECT * FROM generation_attempts WHERE owner_type='generation_result_image'
            AND owner_id=? AND output_asset_id=? ORDER BY CASE WHEN status='SUCCEEDED' THEN 0 ELSE 1 END, id DESC LIMIT 1""",
            (image['id'], asset_id)).fetchone()
        if not attempt or attempt['status'] != 'CANCELLED':
            versions.insert(0, {'id': None, 'image_id': image['id'], 'attempt_id': attempt['id'] if attempt else None,
                'media_asset_id': asset_id, 'request_snapshot_json': attempt['request_snapshot_json'] if attempt else None,
                'created_at': image['updated_at'], 'evidence_source': 'attempt' if attempt else 'published_asset_only'})
    return versions


def replay_package(db: Any, image: Any, version_id: int | None = None) -> dict[str, Any] | None:
    """Actual shot evidence takes precedence over a mutable result-set input pointer."""
    if version_id is not None:
        version = db.execute('SELECT * FROM generation_image_versions WHERE id=? AND image_id=?',
                             (version_id, image['id'])).fetchone()
        if not version:
            raise LookupError('图片版本不存在或不属于当前镜头')
        package = json_object(version['request_snapshot_json'])
        return package if executable_package(package) else None
    frozen = json_object(image['execution_snapshot_json'])
    if executable_package(frozen):
        return frozen
    # Prefer the displayed output's own request, never another shot's/group's facts.
    versions = published_versions(db, image)
    ordered = sorted(versions, key=lambda v: v['media_asset_id'] != image['current_asset_id'])
    if ordered:
        package = json_object(ordered[0]['request_snapshot_json'])
        return package if executable_package(package) else None
    for attempt in db.execute("""SELECT request_snapshot_json FROM generation_attempts
            WHERE owner_type='generation_result_image' AND owner_id=? AND status<>'CANCELLED' ORDER BY id DESC""", (image['id'],)):
        package = json_object(attempt['request_snapshot_json'])
        if executable_package(package):
            return package
    return None
