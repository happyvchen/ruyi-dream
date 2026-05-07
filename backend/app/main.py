from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4
import shutil
import zipfile
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import get_settings
from .db import dumps, get_db, init_db, loads, log_event, media_url, now_iso
from .image_provider import get_image_provider
from .planner import plan_with_gemini


settings = get_settings()
app = FastAPI(title=settings.app_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount(settings.media_url_prefix, StaticFiles(directory=settings.storage_dir), name="media")


class PlanCreateRequest(BaseModel):
    skuId: int
    modelProfileId: int | None = None


class GenerateGroupRequest(BaseModel):
    shotPlanId: int


class RegenerateRequest(BaseModel):
    reason: str
    note: str | None = None


class FinalImageRequest(BaseModel):
    isFinal: bool = True


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "storageDir": str(settings.storage_dir),
        "databasePath": str(settings.database_path),
        "geminiConfigured": bool(settings.gemini_api_key),
        "imageProviderConfigured": bool(settings.gpt_image2_api_base_url and settings.gpt_image2_api_key),
    }


@app.get("/api/garment-skus")
def list_garment_skus() -> dict[str, Any]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT gs.*,
              (SELECT storage_path FROM garment_images gi WHERE gi.sku_id = gs.id ORDER BY sort_order, id LIMIT 1) AS cover_path,
              (SELECT COUNT(*) FROM garment_images gi WHERE gi.sku_id = gs.id) AS image_count
            FROM garment_skus gs
            ORDER BY gs.created_at DESC
            """
        ).fetchall()
        return {"items": [serialize_sku(row) for row in rows]}


@app.post("/api/garment-skus")
async def upload_garment_sku(
    sku: str = Form(...),
    title: str | None = Form(None),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    clean_sku = sanitize_name(sku)
    if not clean_sku:
        raise HTTPException(status_code=400, detail="SKU 不能为空")
    if not files:
        raise HTTPException(status_code=400, detail="至少上传一张图片")

    upload_id = f"{clean_sku}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    target_dir = settings.storage_dir / "uploads" / upload_id
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = now_iso()
    saved_files: list[tuple[UploadFile, Path]] = []
    for index, upload in enumerate(files):
        if not upload.content_type or not upload.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail=f"{upload.filename} 不是图片文件")
        safe_name = f"{index + 1:03d}-{sanitize_filename(upload.filename or 'image')}"
        destination = target_dir / safe_name
        with destination.open("wb") as handle:
            shutil.copyfileobj(upload.file, handle)
        saved_files.append((upload, destination))

    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO garment_skus (sku, title, source, storage_path, status, created_at, updated_at)
            VALUES (?, ?, 'upload', ?, 'NEW', ?, ?)
            """,
            (clean_sku, title or clean_sku, str(target_dir), timestamp, timestamp),
        )
        sku_id = int(cursor.lastrowid)
        for index, (upload, path) in enumerate(saved_files):
            db.execute(
                """
                INSERT INTO garment_images
                  (sku_id, filename, storage_path, mime_type, role, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sku_id,
                    path.name,
                    str(path),
                    upload.content_type or "application/octet-stream",
                    "main" if index == 0 else "reference",
                    index,
                    timestamp,
                ),
            )
        log_event(
            db,
            "garment_sku",
            sku_id,
            "uploaded",
            {"sku": clean_sku, "fileCount": len(saved_files), "storagePath": str(target_dir)},
        )
        row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (sku_id,)).fetchone()
    return {"item": get_sku_detail_by_id(sku_id), "summary": serialize_sku(row)}


@app.get("/api/garment-skus/{sku_id}")
def get_garment_sku(sku_id: int) -> dict[str, Any]:
    return {"item": get_sku_detail_by_id(sku_id)}


@app.get("/api/model-profiles")
def list_model_profiles() -> dict[str, Any]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM model_profiles ORDER BY is_default DESC, id").fetchall()
        return {"items": [serialize_model(row) for row in rows]}


@app.post("/api/shot-plans")
async def create_shot_plan(payload: PlanCreateRequest) -> dict[str, Any]:
    with get_db() as db:
        sku_row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (payload.skuId,)).fetchone()
        if not sku_row:
            raise HTTPException(status_code=404, detail="货号不存在")
        model_row = resolve_model_profile(db, payload.modelProfileId)
        image_rows = db.execute(
            "SELECT * FROM garment_images WHERE sku_id = ? ORDER BY role = 'main' DESC, sort_order, id",
            (payload.skuId,),
        ).fetchall()
        image_paths = [Path(row["storage_path"]) for row in image_rows]

    plan, provider, raw = await plan_with_gemini(sku_row["sku"], image_paths)
    timestamp = now_iso()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO shot_plans
              (sku_id, model_profile_id, status, plan_json, planner_provider, raw_response, created_at, updated_at)
            VALUES (?, ?, 'DRAFT', ?, ?, ?, ?, ?)
            """,
            (payload.skuId, model_row["id"], dumps(plan), provider, raw, timestamp, timestamp),
        )
        plan_id = int(cursor.lastrowid)
        db.execute(
            "UPDATE garment_skus SET status = 'ANALYZED', analysis_json = ?, updated_at = ? WHERE id = ?",
            (dumps(plan.get("garment", {})), timestamp, payload.skuId),
        )
        log_event(
            db,
            "shot_plan",
            plan_id,
            "created",
            {"skuId": payload.skuId, "modelProfileId": model_row["id"], "plannerProvider": provider, "plan": plan},
        )
        row = db.execute("SELECT * FROM shot_plans WHERE id = ?", (plan_id,)).fetchone()
    return {"item": serialize_shot_plan(row)}


@app.get("/api/shot-plans/{shot_plan_id}")
def get_shot_plan(shot_plan_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM shot_plans WHERE id = ?", (shot_plan_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="组图方案不存在")
        return {"item": serialize_shot_plan(row)}


@app.post("/api/generation-runs")
async def generate_group(payload: GenerateGroupRequest) -> dict[str, Any]:
    with get_db() as db:
        plan_row = db.execute("SELECT * FROM shot_plans WHERE id = ?", (payload.shotPlanId,)).fetchone()
        if not plan_row:
            raise HTTPException(status_code=404, detail="组图方案不存在")
        sku_row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (plan_row["sku_id"],)).fetchone()
        model_row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (plan_row["model_profile_id"],)).fetchone()

    plan = loads(plan_row["plan_json"], {})
    output_dir = (
        settings.storage_dir
        / "outputs"
        / sku_row["sku"]
        / datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = now_iso()
    provider = get_image_provider()

    with get_db() as db:
        run_cursor = db.execute(
            """
            INSERT INTO generation_runs
              (sku_id, shot_plan_id, model_profile_id, kind, status, output_dir, created_at, updated_at)
            VALUES (?, ?, ?, 'group', 'RUNNING', ?, ?, ?)
            """,
            (sku_row["id"], plan_row["id"], model_row["id"], str(output_dir), timestamp, timestamp),
        )
        run_id = int(run_cursor.lastrowid)
        log_event(db, "generation_run", run_id, "started", {"shotPlanId": plan_row["id"], "outputDir": str(output_dir)})

    generated_ids: list[int] = []
    try:
        for shot in plan.get("shots", []):
            image_id = await generate_single_image(
                run_id=run_id,
                sku_row=sku_row,
                model_row=model_row,
                plan=plan,
                shot=shot,
                provider=provider,
                output_dir=output_dir,
            )
            generated_ids.append(image_id)
        with get_db() as db:
            db.execute(
                "UPDATE generation_runs SET status = 'SUCCEEDED', updated_at = ? WHERE id = ?",
                (now_iso(), run_id),
            )
            log_event(db, "generation_run", run_id, "succeeded", {"imageIds": generated_ids})
    except Exception as exc:
        with get_db() as db:
            db.execute(
                "UPDATE generation_runs SET status = 'FAILED', updated_at = ? WHERE id = ?",
                (now_iso(), run_id),
            )
            log_event(db, "generation_run", run_id, "failed", {"error": str(exc)})
        raise

    return {"item": get_generation_run_by_id(run_id)}


@app.get("/api/generation-runs")
def list_generation_runs() -> dict[str, Any]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT gr.*, gs.sku
            FROM generation_runs gr
            JOIN garment_skus gs ON gs.id = gr.sku_id
            ORDER BY gr.created_at DESC
            """
        ).fetchall()
        return {"items": [serialize_generation_run(row, include_images=False) for row in rows]}


@app.get("/api/generation-runs/{run_id}")
def get_generation_run(run_id: int) -> dict[str, Any]:
    return {"item": get_generation_run_by_id(run_id)}


@app.post("/api/generation-images/{image_id}/regenerate")
async def regenerate_image(image_id: int, payload: RegenerateRequest) -> dict[str, Any]:
    with get_db() as db:
        image_row = db.execute("SELECT * FROM generation_images WHERE id = ?", (image_id,)).fetchone()
        if not image_row:
            raise HTTPException(status_code=404, detail="生成图片不存在")
        run_row = db.execute("SELECT * FROM generation_runs WHERE id = ?", (image_row["run_id"],)).fetchone()
        sku_row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (run_row["sku_id"],)).fetchone()
        model_row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (run_row["model_profile_id"],)).fetchone()
        plan_row = db.execute("SELECT * FROM shot_plans WHERE id = ?", (run_row["shot_plan_id"],)).fetchone()

    plan = loads(plan_row["plan_json"], {})
    task_package = loads(image_row["task_package_json"], {})
    task_package["regeneration"] = {"fromImageId": image_id, "reason": payload.reason, "note": payload.note}
    output_dir = Path(run_row["output_dir"])
    timestamp = now_iso()

    with get_db() as db:
        run_cursor = db.execute(
            """
            INSERT INTO generation_runs
              (sku_id, shot_plan_id, model_profile_id, kind, status, output_dir, created_at, updated_at)
            VALUES (?, ?, ?, 'regeneration', 'RUNNING', ?, ?, ?)
            """,
            (sku_row["id"], plan_row["id"], model_row["id"], str(output_dir), timestamp, timestamp),
        )
        regen_run_id = int(run_cursor.lastrowid)
        log_event(
            db,
            "generation_image",
            image_id,
            "regenerate_requested",
            {"reason": payload.reason, "note": payload.note, "newRunId": regen_run_id},
        )

    shot = task_package.get("shot", {})
    new_image_id = await generate_single_image(
        run_id=regen_run_id,
        sku_row=sku_row,
        model_row=model_row,
        plan=plan,
        shot=shot,
        provider=get_image_provider(),
        output_dir=output_dir,
        regenerate_from_image_id=image_id,
        regenerate_reason=payload.reason,
        task_package_override=task_package,
    )
    with get_db() as db:
        db.execute("UPDATE generation_runs SET status = 'SUCCEEDED', updated_at = ? WHERE id = ?", (now_iso(), regen_run_id))
        log_event(db, "generation_run", regen_run_id, "succeeded", {"imageIds": [new_image_id]})
    return {"item": get_generation_run_by_id(regen_run_id)}


@app.patch("/api/generation-images/{image_id}/final")
def set_final_image(image_id: int, payload: FinalImageRequest) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成图片不存在")
        db.execute(
            "UPDATE generation_images SET is_final = ?, updated_at = ? WHERE id = ?",
            (1 if payload.isFinal else 0, now_iso(), image_id),
        )
        log_event(db, "generation_image", image_id, "final_updated", {"isFinal": payload.isFinal})
    return {"item": get_generation_image_by_id(image_id)}


@app.get("/api/logs")
def list_logs(limit: int = 100) -> dict[str, Any]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM event_logs ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return {"items": [serialize_log(row) for row in rows]}


@app.post("/api/exports/runs/{run_id}")
def export_run(run_id: int) -> FileResponse:
    run = get_generation_run_by_id(run_id)
    images = run.get("images", [])
    final_images = [image for image in images if image.get("isFinal")]
    selected = final_images or images
    if not selected:
        raise HTTPException(status_code=422, detail="没有可导出的生成图")

    export_dir = settings.storage_dir / "exports" / str(run["sku"] or run["skuId"])
    export_dir.mkdir(parents=True, exist_ok=True)
    zip_path = export_dir / f"run-{run_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for image in selected:
            output_url = image.get("url")
            if not output_url:
                continue
            relative = output_url.replace(f"{settings.media_url_prefix}/", "", 1)
            source_path = settings.storage_dir / relative
            if source_path.exists():
                archive.write(source_path, arcname=source_path.name)

    with get_db() as db:
        log_event(
            db,
            "generation_run",
            run_id,
            "exported",
            {"zipPath": str(zip_path), "imageCount": len(selected), "finalOnly": bool(final_images)},
        )
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")


async def generate_single_image(
    run_id: int,
    sku_row: Any,
    model_row: Any,
    plan: dict[str, Any],
    shot: dict[str, Any],
    provider: Any,
    output_dir: Path,
    regenerate_from_image_id: int | None = None,
    regenerate_reason: str | None = None,
    task_package_override: dict[str, Any] | None = None,
) -> int:
    shot_index = int(shot.get("shotIndex", 1))
    title = str(shot.get("title", f"图 {shot_index}"))
    suffix = f"{shot_index:02d}-{sanitize_filename(title)}"
    if regenerate_from_image_id:
        suffix = f"{suffix}-regen-{datetime.now().strftime('%H%M%S')}"
    output_path = output_dir / f"{suffix}.svg"
    task_package = task_package_override or build_task_package(sku_row, model_row, plan, shot)
    timestamp = now_iso()

    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO generation_images
              (run_id, shot_index, title, status, task_package_json, regenerate_from_image_id,
               regenerate_reason, created_at, updated_at)
            VALUES (?, ?, ?, 'RUNNING', ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                shot_index,
                title,
                dumps(task_package),
                regenerate_from_image_id,
                regenerate_reason,
                timestamp,
                timestamp,
            ),
        )
        image_id = int(cursor.lastrowid)
        log_event(db, "generation_image", image_id, "started", {"runId": run_id, "shot": shot})

    provider_request = {"taskPackage": task_package, "outputPath": str(output_path)}
    provider_response = await provider.generate(task_package, output_path)
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_images
            SET status = 'SUCCEEDED', output_path = ?, provider_request_json = ?,
                provider_response_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(output_path), dumps(provider_request), dumps(provider_response), now_iso(), image_id),
        )
        log_event(db, "generation_image", image_id, "succeeded", {"outputPath": str(output_path), "provider": provider_response})
    return image_id


def build_task_package(sku_row: Any, model_row: Any, plan: dict[str, Any], shot: dict[str, Any]) -> dict[str, Any]:
    return {
        "sku": sku_row["sku"],
        "garment": plan.get("garment", {}),
        "model": {
            "modelId": model_row["model_id"],
            "profile": loads(model_row["profile_json"], {}),
            "identityLock": "high",
        },
        "brandStyle": plan.get("brandStyle", {}),
        "shot": shot,
        "generation": {
            "targetModel": "GPT Image-2",
            "provider": "third-party-placeholder",
            "output": {"aspectRatio": "1:1", "size": "1200x1200"},
            "constraints": [
                "服装参考图是商品事实源",
                "保持同一模特脸、发型、气质",
                "保持 Lily 白/浅灰棚拍风格",
                "不生成无关文字、水印、logo",
            ],
        },
    }


def get_sku_detail_by_id(sku_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (sku_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="货号不存在")
        images = db.execute(
            "SELECT * FROM garment_images WHERE sku_id = ? ORDER BY sort_order, id",
            (sku_id,),
        ).fetchall()
        item = serialize_sku(row)
        item["images"] = [serialize_image(image) for image in images]
        return item


def get_generation_run_by_id(run_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gr.*, gs.sku
            FROM generation_runs gr
            JOIN garment_skus gs ON gs.id = gr.sku_id
            WHERE gr.id = ?
            """,
            (run_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成记录不存在")
        return serialize_generation_run(row, include_images=True)


def get_generation_image_by_id(image_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成图片不存在")
        return serialize_generation_image(row)


def resolve_model_profile(db: Any, model_profile_id: int | None) -> Any:
    if model_profile_id:
        row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (model_profile_id,)).fetchone()
    else:
        row = db.execute("SELECT * FROM model_profiles WHERE is_default = 1 ORDER BY id LIMIT 1").fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="数字模特不存在")
    return row


def serialize_sku(row: Any) -> dict[str, Any]:
    cover_path = row.keys() and row["cover_path"] if "cover_path" in row.keys() else None
    return {
        "id": row["id"],
        "sku": row["sku"],
        "title": row["title"],
        "source": row["source"],
        "status": row["status"],
        "analysis": loads(row["analysis_json"], None),
        "storagePath": row["storage_path"],
        "coverUrl": media_url(cover_path),
        "imageCount": row["image_count"] if "image_count" in row.keys() else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def serialize_image(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "role": row["role"],
        "mimeType": row["mime_type"],
        "url": media_url(row["storage_path"]),
        "createdAt": row["created_at"],
    }


def serialize_model(row: Any) -> dict[str, Any]:
    profile = loads(row["profile_json"], {})
    head = profile.get("headReferences", {}).get("front") or profile.get("headReferences", {}).get("frontAlt")
    preview_path = Path(row["asset_dir"]) / head if head else None
    return {
        "id": row["id"],
        "modelId": row["model_id"],
        "name": row["name"],
        "assetDir": row["asset_dir"],
        "profile": profile,
        "isDefault": bool(row["is_default"]),
        "previewPath": str(preview_path) if preview_path else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def serialize_shot_plan(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "skuId": row["sku_id"],
        "modelProfileId": row["model_profile_id"],
        "status": row["status"],
        "plan": loads(row["plan_json"], {}),
        "plannerProvider": row["planner_provider"],
        "rawResponse": row["raw_response"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def serialize_generation_run(row: Any, include_images: bool) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "skuId": row["sku_id"],
        "sku": row["sku"] if "sku" in row.keys() else None,
        "shotPlanId": row["shot_plan_id"],
        "modelProfileId": row["model_profile_id"],
        "kind": row["kind"],
        "status": row["status"],
        "outputDir": row["output_dir"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_images:
        with get_db() as db:
            images = db.execute(
                "SELECT * FROM generation_images WHERE run_id = ? ORDER BY shot_index, id",
                (row["id"],),
            ).fetchall()
            item["images"] = [serialize_generation_image(image) for image in images]
    return item


def serialize_generation_image(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "runId": row["run_id"],
        "shotIndex": row["shot_index"],
        "title": row["title"],
        "status": row["status"],
        "url": media_url(row["output_path"]),
        "taskPackage": loads(row["task_package_json"], {}),
        "providerRequest": loads(row["provider_request_json"], None),
        "providerResponse": loads(row["provider_response_json"], None),
        "regenerateFromImageId": row["regenerate_from_image_id"],
        "regenerateReason": row["regenerate_reason"],
        "isFinal": bool(row["is_final"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def serialize_log(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "entityType": row["entity_type"],
        "entityId": row["entity_id"],
        "eventType": row["event_type"],
        "payload": loads(row["payload_json"], {}),
        "createdAt": row["created_at"],
    }


def sanitize_name(value: str) -> str:
    return "".join(ch for ch in value.strip() if ch.isalnum() or ch in {"-", "_"}).strip("-_")


def sanitize_filename(value: str) -> str:
    stem = Path(value).stem or "image"
    suffix = Path(value).suffix.lower()
    clean = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in stem).strip("-")
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".svg"}:
        suffix = ""
    return f"{clean or 'image'}{suffix}"
