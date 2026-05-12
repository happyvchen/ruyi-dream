from __future__ import annotations

import asyncio
import hashlib
import hmac
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from io import BytesIO
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
import time
from uuid import uuid4
import zipfile
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

from .config import get_settings
from .db import dumps, get_db, init_db, loads, log_event, media_url, now_iso
from .image_derivatives import derivative_key, existing_derivative_or_original, generate_image_derivatives
from .image_provider import get_image_provider
from .object_storage import (
    clean_key,
    delete_object,
    delete_prefix,
    ensure_bucket,
    get_bytes,
    join_key,
    object_exists,
    object_size,
    presigned_get_url,
    put_fileobj,
    unique_key,
)
from .planner import PlannerError, SHOT_SLOTS, normalize_plan as normalize_planner_plan, plan_with_gemini


settings = get_settings()
Image.MAX_IMAGE_PIXELS = settings.max_upload_image_pixels


def is_request_authenticated(request: Request) -> bool:
    if settings.trust_reverse_proxy_auth:
        return True
    if is_api_token_valid(request):
        return True
    if not settings.is_gate_enabled():
        return True
    return is_session_cookie_valid(request.cookies.get("ruyi_session", ""))


def is_api_token_valid(request: Request) -> bool:
    if not settings.api_access_token:
        return False
    header_token = request.headers.get("x-ruyi-access-token", "")
    auth_header = request.headers.get("authorization", "")
    bearer_token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
    return secrets.compare_digest(header_token, settings.api_access_token) or secrets.compare_digest(
        bearer_token,
        settings.api_access_token,
    )


def is_write_origin_allowed(request: Request) -> bool:
    if settings.trust_reverse_proxy_auth or is_api_token_valid(request):
        return True
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    source = origin or referer
    if not source:
        return True
    return any(source == allowed or source.startswith(f"{allowed}/") for allowed in settings.cors_origins)


def is_gate_password_valid(password: str) -> bool:
    if settings.gate_password and secrets.compare_digest(password, settings.gate_password):
        return True
    if settings.gate_password_hash:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return secrets.compare_digest(digest, settings.gate_password_hash)
    return False


def sign_session_payload(payload: str) -> str:
    return hmac.new(settings.effective_session_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_cookie() -> str:
    expires_at = int(time.time() + settings.session_ttl_seconds)
    payload = f"{expires_at}.{secrets.token_urlsafe(18)}"
    return f"{payload}.{sign_session_payload(payload)}"


def is_session_cookie_valid(cookie: str) -> bool:
    parts = cookie.split(".")
    if len(parts) != 3:
        return False
    expires_at, nonce, signature = parts
    if not expires_at.isdigit() or not nonce:
        return False
    payload = f"{expires_at}.{nonce}"
    if not secrets.compare_digest(signature, sign_session_payload(payload)):
        return False
    return int(expires_at) > int(time.time())


def auth_client_key(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def is_login_rate_limited(request: Request) -> bool:
    now = time.time()
    cutoff = now - LOGIN_FAILURE_WINDOW_SECONDS
    client_key = auth_client_key(request)
    recent_failures = [timestamp for timestamp in login_failures_by_client.get(client_key, []) if timestamp >= cutoff]
    login_failures_by_client[client_key] = recent_failures
    return len(recent_failures) >= LOGIN_FAILURE_LIMIT


def record_login_failure(request: Request) -> None:
    client_key = auth_client_key(request)
    login_failures_by_client.setdefault(client_key, []).append(time.time())


def clear_login_failures(request: Request) -> None:
    login_failures_by_client.pop(auth_client_key(request), None)


class PlanCreateRequest(BaseModel):
    skuId: int
    modelProfileId: int | None = None


class GenerateGroupRequest(BaseModel):
    shotPlanId: int
    previewRunId: int | None = None


class ShotPlanUpdateRequest(BaseModel):
    plan: dict[str, Any]


class RegenerateRequest(BaseModel):
    reason: str
    note: str | None = None


class FinalImageRequest(BaseModel):
    isFinal: bool = True


class ReviewStatusRequest(BaseModel):
    status: str
    note: str | None = None


class LoginRequest(BaseModel):
    password: str


class GarmentSkuUpdateRequest(BaseModel):
    sku: str | None = None
    title: str | None = None
    status: str | None = None


class GarmentImageUpdateRequest(BaseModel):
    role: str | None = None
    sortOrder: int | None = None


class ModelProfileUpsertRequest(BaseModel):
    modelId: str | None = None
    name: str | None = None
    isDefault: bool | None = None
    profile: dict[str, Any] | None = None


class HeadReferenceUpdateRequest(BaseModel):
    angle: str | None = None
    orderIndex: int | None = None


HEAD_REFERENCE_ANGLES = [
    "front",
    "left45",
    "right45",
    "right90",
    "left90",
    "up45",
    "down45",
    "back",
    "backTurnLeft45",
    "backTurnRight45",
]
UNASSIGNED_HEAD_REFERENCE_PREFIX = "__unassigned__"


ACTIVE_IMAGE_STATUSES = {"QUEUED", "SUBMITTED", "IN_QUEUE", "IN_PROGRESS", "RUNNING"}
TERMINAL_IMAGE_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}
PLANNING_STATUSES = {"PLANNING"}
REVIEW_STATUSES = {"candidate", "approved", "needs_regen", "needs_retouch", "delivered"}
LOGIN_FAILURE_WINDOW_SECONDS = 300
LOGIN_FAILURE_LIMIT = 8
login_failures_by_client: dict[str, list[float]] = {}
ALLOWED_UPLOAD_MIME_TYPES = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}
ALLOWED_UPLOAD_FORMATS = set(ALLOWED_UPLOAD_MIME_TYPES.values())


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    ensure_bucket()
    init_db()
    backfill_shot_plan_script_data()
    app.state.generation_poller = asyncio.create_task(generation_poller_loop())
    app.state.planner_poller = asyncio.create_task(planner_poller_loop())
    try:
        yield
    finally:
        task = getattr(app.state, "generation_poller", None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        planner_task = getattr(app.state, "planner_poller", None)
        if planner_task:
            planner_task.cancel()
            with suppress(asyncio.CancelledError):
                await planner_task


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Ruyi-Access-Token"],
)


@app.middleware("http")
async def require_api_session(request: Request, call_next: Any) -> Any:
    public_paths = {"/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/session"}
    if request.method == "OPTIONS" or request.url.path in public_paths:
        return await call_next(request)
    if request.url.path.startswith("/api/") and request.method in {"POST", "PATCH", "DELETE"} and not is_write_origin_allowed(request):
        return JSONResponse(status_code=403, content={"detail": "请求来源不被允许"})
    if request.url.path.startswith("/api/") and not is_request_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "请先登录"})
    return await call_next(request)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "storage": "configured" if settings.s3_access_key_id and settings.s3_secret_access_key else "missing_credentials",
        "geminiConfigured": bool(settings.gemini_api_key),
        "imageProviderConfigured": bool(settings.gpt_image2_queue_url and settings.fal_key),
    }


@app.get("/api/auth/session")
def get_auth_session(request: Request) -> dict[str, bool]:
    return {
        "authRequired": settings.is_gate_enabled() and not settings.trust_reverse_proxy_auth,
        "authenticated": is_request_authenticated(request),
    }


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request) -> JSONResponse:
    if not settings.is_gate_enabled():
        return JSONResponse({"authRequired": False, "authenticated": True})
    if is_login_rate_limited(request):
        return JSONResponse(status_code=429, content={"detail": "登录尝试过多，请稍后再试"})
    if not is_gate_password_valid(payload.password):
        record_login_failure(request)
        return JSONResponse(status_code=401, content={"detail": "访问密码不正确"})
    clear_login_failures(request)
    response = JSONResponse({"authRequired": True, "authenticated": True})
    response.set_cookie(
        "ruyi_session",
        create_session_cookie(),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie("ruyi_session", path="/")
    return response


async def validate_upload_image(upload: UploadFile) -> tuple[bytes, str]:
    filename = upload.filename or "image"
    declared_type = (upload.content_type or "").lower()
    if declared_type not in ALLOWED_UPLOAD_MIME_TYPES:
        raise HTTPException(status_code=400, detail=f"{filename} 仅支持 JPG、PNG 或 WebP 图片")

    content = await upload.read(settings.max_upload_image_bytes + 1)
    await upload.seek(0)
    if not content:
        raise HTTPException(status_code=400, detail=f"{filename} 是空文件")
    if len(content) > settings.max_upload_image_bytes:
        max_mb = settings.max_upload_image_bytes / (1024 * 1024)
        raise HTTPException(status_code=400, detail=f"{filename} 超过 {max_mb:.0f}MB 大小限制")

    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
        with Image.open(BytesIO(content)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=400, detail=f"{filename} 无法解码为有效图片") from exc

    if image_format not in ALLOWED_UPLOAD_FORMATS:
        raise HTTPException(status_code=400, detail=f"{filename} 仅支持 JPG、PNG 或 WebP 图片")
    expected_format = ALLOWED_UPLOAD_MIME_TYPES[declared_type]
    if image_format != expected_format:
        raise HTTPException(status_code=400, detail=f"{filename} 的文件类型与 Content-Type 不一致")
    if width <= 0 or height <= 0 or width * height > settings.max_upload_image_pixels:
        max_mp = settings.max_upload_image_pixels / 1_000_000
        raise HTTPException(status_code=400, detail=f"{filename} 超过 {max_mp:.0f}MP 像素限制")

    return content, declared_type


def cleanup_objects(keys: list[str]) -> None:
    for key in keys:
        with suppress(Exception):
            delete_object(key)


def image_object_keys(original_key: str | Path | None) -> list[str]:
    if not original_key:
        return []
    return [
        clean_key(original_key),
        *(key for key in [derivative_key(original_key, "thumb"), derivative_key(original_key, "preview")] if key),
    ]


def cleanup_prefixes(prefixes: list[str]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for prefix in dict.fromkeys(prefix for prefix in prefixes if prefix):
        try:
            delete_prefix(prefix)
        except Exception as exc:
            errors.append({"prefix": prefix, "error": str(exc)})
    return errors


@app.get("/api/garment-skus")
def list_garment_skus(
    include_archived: bool = Query(False, alias="includeArchived"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    keyword: str | None = Query(None),
) -> dict[str, Any]:
    search = f"%{keyword.strip()}%" if keyword and keyword.strip() else None
    with get_db() as db:
        rows = db.execute(
            """
            SELECT gs.*,
              (SELECT storage_path FROM garment_images gi WHERE gi.sku_id = gs.id ORDER BY sort_order, id LIMIT 1) AS cover_path,
              (SELECT COUNT(*) FROM garment_images gi WHERE gi.sku_id = gs.id) AS image_count,
              (SELECT COUNT(*) FROM shot_plans sp WHERE sp.sku_id = gs.id) AS shot_plan_count,
              (SELECT COUNT(*) FROM generation_runs gr WHERE gr.sku_id = gs.id) AS generation_run_count,
              (SELECT COUNT(*) FROM generation_runs gr WHERE gr.sku_id = gs.id AND gr.archived_at IS NOT NULL) AS archived_generation_run_count
            FROM garment_skus gs
            WHERE (? OR gs.archived_at IS NULL)
              AND (? IS NULL OR gs.sku LIKE ? OR gs.title LIKE ? OR gs.status LIKE ?)
            ORDER BY gs.created_at DESC, gs.id DESC
            LIMIT ? OFFSET ?
            """,
            (include_archived, search, search, search, search, limit + 1, offset),
        ).fetchall()
        items = rows[:limit]
        return {
            "items": [serialize_sku(row) for row in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if len(rows) > limit else None,
            "hasMore": len(rows) > limit,
        }


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
    if len(files) > settings.max_garment_images_per_sku:
        raise HTTPException(status_code=400, detail=f"单个货号最多上传 {settings.max_garment_images_per_sku} 张图片")

    upload_id = f"{clean_sku}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    target_prefix = join_key("uploads", upload_id)

    timestamp = now_iso()
    validated_files: list[tuple[UploadFile, bytes, str]] = []
    for upload in files:
        content, content_type = await validate_upload_image(upload)
        validated_files.append((upload, content, content_type))

    saved_files: list[tuple[str, str, str]] = []
    try:
        for index, (upload, content, content_type) in enumerate(validated_files):
            safe_name = f"{index + 1:03d}-{sanitize_filename(upload.filename or 'image')}"
            key = join_key(target_prefix, safe_name)
            put_fileobj(key, BytesIO(content), content_type)
            generate_image_derivatives(key, content)
            saved_files.append((safe_name, key, content_type))
    except Exception:
        cleanup_objects([key for _, original_key, _ in saved_files for key in image_object_keys(original_key)])
        raise

    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO garment_skus (sku, title, source, storage_path, status, created_at, updated_at)
                VALUES (?, ?, 'upload', ?, 'NEW', ?, ?)
                """,
                (clean_sku, title or clean_sku, target_prefix, timestamp, timestamp),
            )
            sku_id = int(cursor.lastrowid)
            for index, (filename, key, content_type) in enumerate(saved_files):
                db.execute(
                    """
                    INSERT INTO garment_images
                      (sku_id, filename, storage_path, mime_type, role, sort_order, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sku_id,
                        filename,
                        key,
                        content_type,
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
                {"sku": clean_sku, "fileCount": len(saved_files), "storagePath": target_prefix},
            )
            row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (sku_id,)).fetchone()
    except Exception:
        cleanup_objects([key for _, original_key, _ in saved_files for key in image_object_keys(original_key)])
        raise
    return {"item": get_sku_detail_by_id(sku_id), "summary": serialize_sku(row)}


@app.get("/api/garment-skus/{sku_id}")
def get_garment_sku(
    sku_id: int,
    image_limit: int = Query(24, alias="imageLimit", ge=1, le=120),
    image_offset: int = Query(0, alias="imageOffset", ge=0),
) -> dict[str, Any]:
    return {"item": get_sku_detail_by_id(sku_id, image_limit=image_limit, image_offset=image_offset)}


@app.get("/api/garment-skus/{sku_id}/images")
def list_garment_sku_images(
    sku_id: int,
    limit: int = Query(24, ge=1, le=120),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    with get_db() as db:
        sku_row = db.execute("SELECT id FROM garment_skus WHERE id = ?", (sku_id,)).fetchone()
        if not sku_row:
            raise HTTPException(status_code=404, detail="货号不存在")
        rows = db.execute(
            "SELECT * FROM garment_images WHERE sku_id = ? ORDER BY sort_order, id LIMIT ? OFFSET ?",
            (sku_id, limit + 1, offset),
        ).fetchall()
        items = rows[:limit]
        return {
            "items": [serialize_image(image) for image in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if len(rows) > limit else None,
            "hasMore": len(rows) > limit,
        }


@app.patch("/api/garment-skus/{sku_id}")
def update_garment_sku(sku_id: int, payload: GarmentSkuUpdateRequest) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if payload.sku is not None:
        clean_sku = sanitize_name(payload.sku)
        if not clean_sku:
            raise HTTPException(status_code=400, detail="SKU 不能为空")
        updates["sku"] = clean_sku
    if payload.title is not None:
        updates["title"] = payload.title.strip() or None
    if payload.status is not None:
        if payload.status not in {"NEW", "ANALYZED", "CONFIRMED"}:
            raise HTTPException(status_code=400, detail="状态不合法")
        updates["status"] = payload.status
    if not updates:
        return {"item": get_sku_detail_by_id(sku_id)}

    with get_db() as db:
        row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (sku_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="货号不存在")
        updates["updated_at"] = now_iso()
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        db.execute(
            f"UPDATE garment_skus SET {set_clause} WHERE id = ?",
            (*updates.values(), sku_id),
        )
        log_event(db, "garment_sku", sku_id, "updated", updates)
    return {"item": get_sku_detail_by_id(sku_id)}


@app.delete("/api/garment-skus/{sku_id}")
def delete_garment_sku(
    sku_id: int,
    purge: bool = Query(False),
    confirm_sku: str | None = Query(None, alias="confirmSku"),
) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (sku_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="货号不存在")
        impact = sku_delete_impact(db, sku_id)
        if purge and confirm_sku != row["sku"]:
            raise HTTPException(status_code=400, detail="请输入完整货号以确认删除关联记录和文件")
        timestamp = now_iso()
        if not purge:
            if "archived_at" in row.keys() and row["archived_at"]:
                return {"ok": True, "archived": True, "objectCleanupErrors": []}
            db.execute(
                "UPDATE garment_skus SET archived_at = ?, archive_reason = ?, updated_at = ? WHERE id = ?",
                (timestamp, "user_archived", timestamp, sku_id),
            )
            log_event(
                db,
                "garment_sku",
                sku_id,
                "archived",
                {"sku": row["sku"], "storagePath": row["storage_path"], "impact": impact},
            )
            return {"ok": True, "archived": True, "objectCleanupErrors": []}
        storage_path = row["storage_path"]
        output_dirs = [
            row["output_dir"]
            for row in db.execute(
                "SELECT DISTINCT output_dir FROM generation_runs WHERE sku_id = ? AND output_dir IS NOT NULL",
                (sku_id,),
            ).fetchall()
            if row["output_dir"]
        ]
        db.execute("DELETE FROM garment_skus WHERE id = ?", (sku_id,))
        log_event(
            db,
            "garment_sku",
            sku_id,
            "deleted_with_related_records",
            {"sku": row["sku"], "storagePath": storage_path, "impact": impact},
        )

    cleanup_errors = cleanup_prefixes([storage_path, *output_dirs])
    return {"ok": True, "purged": True, "objectCleanupErrors": cleanup_errors}


@app.post("/api/garment-skus/{sku_id}/images")
async def add_garment_images(sku_id: int, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="至少上传一张图片")
    timestamp = now_iso()
    validated_files: list[tuple[UploadFile, bytes, str]] = []
    for upload in files:
        content, content_type = await validate_upload_image(upload)
        validated_files.append((upload, content, content_type))
    saved: list[tuple[str, str, str, int]] = []

    with get_db() as db:
        sku_row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (sku_id,)).fetchone()
        if not sku_row:
            raise HTTPException(status_code=404, detail="货号不存在")
        target_prefix = clean_key(sku_row["storage_path"])
        current_max = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS max_sort FROM garment_images WHERE sku_id = ?",
            (sku_id,),
        ).fetchone()["max_sort"]
        current_count = db.execute(
            "SELECT COUNT(*) AS count FROM garment_images WHERE sku_id = ?",
            (sku_id,),
        ).fetchone()["count"]
        if int(current_count) + len(files) > settings.max_garment_images_per_sku:
            raise HTTPException(status_code=400, detail=f"单个货号最多保留 {settings.max_garment_images_per_sku} 张图片")

        try:
            for offset, (upload, content, content_type) in enumerate(validated_files, start=1):
                sort_order = int(current_max) + offset
                safe_name = f"{sort_order + 1:03d}-{sanitize_filename(upload.filename or 'image')}"
                key = unique_key(join_key(target_prefix, safe_name))
                put_fileobj(key, BytesIO(content), content_type)
                generate_image_derivatives(key, content)
                saved.append((Path(key).name, key, content_type, sort_order))
        except Exception:
            cleanup_objects([key for _, original_key, _, _ in saved for key in image_object_keys(original_key)])
            raise

        try:
            for filename, key, content_type, sort_order in saved:
                db.execute(
                    """
                    INSERT INTO garment_images
                      (sku_id, filename, storage_path, mime_type, role, sort_order, created_at)
                    VALUES (?, ?, ?, ?, 'reference', ?, ?)
                    """,
                    (
                        sku_id,
                        filename,
                        key,
                        content_type,
                        sort_order,
                        timestamp,
                    ),
                )
            db.execute(
                "UPDATE garment_skus SET updated_at = ? WHERE id = ?",
                (timestamp, sku_id),
            )
            log_event(
                db,
                "garment_sku",
                sku_id,
                "images_added",
                {"fileCount": len(saved), "files": [filename for filename, _, _, _ in saved]},
            )
        except Exception:
            cleanup_objects([key for _, original_key, _, _ in saved for key in image_object_keys(original_key)])
            raise
    return {"item": get_sku_detail_by_id(sku_id)}


@app.patch("/api/garment-images/{image_id}")
def update_garment_image(image_id: int, payload: GarmentImageUpdateRequest) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if payload.role is not None:
        allowed_roles = {"main", "reference", "front", "back", "detail", "fabric", "color", "3d"}
        if payload.role not in allowed_roles:
            raise HTTPException(status_code=400, detail="图片角色不合法")
        updates["role"] = payload.role
    if payload.sortOrder is not None:
        if payload.sortOrder < 0:
            raise HTTPException(status_code=400, detail="排序不能小于 0")
        updates["sort_order"] = payload.sortOrder
    if not updates:
        with get_db() as db:
            row = db.execute("SELECT * FROM garment_images WHERE id = ?", (image_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="图片不存在")
            return {"item": serialize_image(row)}

    with get_db() as db:
        row = db.execute("SELECT * FROM garment_images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="图片不存在")
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        db.execute(
            f"UPDATE garment_images SET {set_clause} WHERE id = ?",
            (*updates.values(), image_id),
        )
        db.execute(
            "UPDATE garment_skus SET updated_at = ? WHERE id = ?",
            (now_iso(), row["sku_id"]),
        )
        log_event(db, "garment_image", image_id, "updated", updates)
        updated = db.execute("SELECT * FROM garment_images WHERE id = ?", (image_id,)).fetchone()
    return {"item": serialize_image(updated)}


@app.delete("/api/garment-images/{image_id}")
def delete_garment_image(image_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM garment_images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="图片不存在")
        count = db.execute(
            "SELECT COUNT(*) AS count FROM garment_images WHERE sku_id = ?",
            (row["sku_id"],),
        ).fetchone()["count"]
        if count <= 1:
            raise HTTPException(status_code=409, detail="每个货号至少保留一张图片")
        storage_path = row["storage_path"]
        db.execute("DELETE FROM garment_images WHERE id = ?", (image_id,))
        db.execute(
            "UPDATE garment_skus SET updated_at = ? WHERE id = ?",
            (now_iso(), row["sku_id"]),
        )
        log_event(
            db,
            "garment_image",
            image_id,
            "deleted",
            {"skuId": row["sku_id"], "filename": row["filename"], "storagePath": storage_path},
        )

    cleanup_objects(image_object_keys(storage_path))
    return {"ok": True}


@app.get("/api/model-profiles")
def list_model_profiles(
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    keyword: str | None = Query(None),
) -> dict[str, Any]:
    search = f"%{keyword.strip()}%" if keyword and keyword.strip() else None
    with get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM model_profiles
            WHERE (? IS NULL OR model_id LIKE ? OR name LIKE ?)
            ORDER BY is_default DESC, id
            LIMIT ? OFFSET ?
            """,
            (search, search, search, limit + 1, offset),
        ).fetchall()
        items = rows[:limit]
        return {
            "items": [serialize_model(row) for row in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if len(rows) > limit else None,
            "hasMore": len(rows) > limit,
        }


@app.post("/api/model-profiles")
def create_model_profile(payload: ModelProfileUpsertRequest) -> dict[str, Any]:
    clean_model_id = sanitize_name(payload.modelId or "")
    if not clean_model_id:
        raise HTTPException(status_code=400, detail="模特 ID 不能为空")
    name = (payload.name or clean_model_id).strip()
    if not name:
        raise HTTPException(status_code=400, detail="模特名称不能为空")

    timestamp = now_iso()
    asset_dir = join_key("model-profiles", clean_model_id)
    profile = normalize_model_profile(payload.profile or {})

    with get_db() as db:
        existing = db.execute("SELECT id FROM model_profiles WHERE model_id = ?", (clean_model_id,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="模特 ID 已存在")
        if payload.isDefault:
            db.execute("UPDATE model_profiles SET is_default = 0")
        cursor = db.execute(
            """
            INSERT INTO model_profiles
              (model_id, name, asset_dir, profile_json, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_model_id,
                name,
                asset_dir,
                dumps(profile),
                1 if payload.isDefault else 0,
                timestamp,
                timestamp,
            ),
        )
        model_id = int(cursor.lastrowid)
        log_event(db, "model_profile", model_id, "created", {"modelId": clean_model_id, "name": name})
    return {"item": get_model_profile_by_id(model_id)}


@app.patch("/api/model-profiles/{model_profile_id}")
def update_model_profile(model_profile_id: int, payload: ModelProfileUpsertRequest) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (model_profile_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="数字模特不存在")

        current_profile = loads(row["profile_json"], {})
        if payload.modelId is not None:
            clean_model_id = sanitize_name(payload.modelId)
            if not clean_model_id:
                raise HTTPException(status_code=400, detail="模特 ID 不能为空")
            duplicate = db.execute(
                "SELECT id FROM model_profiles WHERE model_id = ? AND id <> ?",
                (clean_model_id, model_profile_id),
            ).fetchone()
            if duplicate:
                raise HTTPException(status_code=409, detail="模特 ID 已存在")
            updates["model_id"] = clean_model_id
        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="模特名称不能为空")
            updates["name"] = name
        if payload.profile is not None:
            updates["profile_json"] = dumps(normalize_model_profile({**current_profile, **payload.profile}))
        if payload.isDefault is not None:
            if payload.isDefault:
                db.execute("UPDATE model_profiles SET is_default = 0 WHERE id <> ?", (model_profile_id,))
            updates["is_default"] = 1 if payload.isDefault else 0

        if updates:
            updates["updated_at"] = now_iso()
            set_clause = ", ".join(f"{key} = ?" for key in updates)
            db.execute(
                f"UPDATE model_profiles SET {set_clause} WHERE id = ?",
                (*updates.values(), model_profile_id),
            )
            log_event(db, "model_profile", model_profile_id, "updated", updates)
    return {"item": get_model_profile_by_id(model_profile_id)}


@app.delete("/api/model-profiles/{model_profile_id}")
def delete_model_profile(model_profile_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (model_profile_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="数字模特不存在")
        model_count = db.execute("SELECT COUNT(*) AS count FROM model_profiles").fetchone()["count"]
        if model_count <= 1:
            raise HTTPException(status_code=409, detail="至少保留一个数字模特")
        usage_count = db.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM shot_plans WHERE model_profile_id = ?) +
              (SELECT COUNT(*) FROM generation_runs WHERE model_profile_id = ?) AS count
            """,
            (model_profile_id, model_profile_id),
        ).fetchone()["count"]
        if usage_count:
            raise HTTPException(status_code=409, detail="该模特已有组图或生成记录，为保留生产历史暂不允许删除")
        asset_dir = row["asset_dir"]
        was_default = bool(row["is_default"])
        db.execute("DELETE FROM model_profiles WHERE id = ?", (model_profile_id,))
        if was_default:
            fallback = db.execute("SELECT id FROM model_profiles ORDER BY id LIMIT 1").fetchone()
            if fallback:
                db.execute("UPDATE model_profiles SET is_default = 1 WHERE id = ?", (fallback["id"],))
        log_event(db, "model_profile", model_profile_id, "deleted", {"modelId": row["model_id"], "assetDir": asset_dir})

    delete_prefix(asset_dir)
    return {"ok": True}


@app.post("/api/model-profiles/{model_profile_id}/head-references")
async def add_model_head_reference(
    model_profile_id: int,
    angle: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    file: UploadFile | None = File(None),
) -> dict[str, Any]:
    uploads = files or ([file] if file else [])
    if not uploads:
        raise HTTPException(status_code=400, detail="至少上传一张头像")
    clean_angle = sanitize_reference_key(angle or "") if angle else None
    if clean_angle and clean_angle not in HEAD_REFERENCE_ANGLES:
        raise HTTPException(status_code=400, detail="头像角度不合法")
    if clean_angle and len(uploads) > 1:
        raise HTTPException(status_code=400, detail="指定角度时只能上传一张头像")
    validated_uploads: list[tuple[UploadFile, bytes, str]] = []
    for upload in uploads:
        content, content_type = await validate_upload_image(upload)
        validated_uploads.append((upload, content, content_type))

    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (model_profile_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="数字模特不存在")
        asset_dir = clean_key(row["asset_dir"])

        profile = loads(row["profile_json"], {})
        references = dict(profile.get("headReferences") or {})
        order = normalize_head_reference_order(profile, references)
        new_reference_count = 0 if clean_angle and clean_angle in references else len(validated_uploads)
        if len(order) + new_reference_count > settings.max_head_references_per_profile:
            raise HTTPException(
                status_code=400,
                detail=f"单个数字模特最多保留 {settings.max_head_references_per_profile} 张头像",
            )
        assigned_keys: list[str | None] = [clean_angle] if clean_angle else []
        reserved_references = dict(references)
        while len(assigned_keys) < len(validated_uploads):
            assigned_key = new_unassigned_head_reference_key(reserved_references)
            reserved_references[assigned_key] = ""
            assigned_keys.append(assigned_key)
        old_files: list[str] = []
        saved: list[dict[str, str]] = []
        saved_keys: list[str] = []
        try:
            for (upload, content, content_type), assigned_key in zip(validated_uploads, assigned_keys):
                if assigned_key is None:
                    raise HTTPException(status_code=400, detail="头像角度不合法")
                safe_name = sanitize_filename(upload.filename or "head-reference")
                key = unique_key(join_key(asset_dir, safe_name))
                put_fileobj(key, BytesIO(content), content_type)
                generate_image_derivatives(key, content)
                saved_keys.append(key)
                old_filename = references.get(assigned_key)
                if old_filename:
                    old_files.append(old_filename)
                filename = Path(key).name
                references[assigned_key] = filename
                if assigned_key not in order:
                    order.append(assigned_key)
                saved.append({"id": assigned_key, "filename": filename})
        except Exception:
            cleanup_objects([key for saved_key in saved_keys for key in image_object_keys(saved_key)])
            raise

        try:
            profile["headReferences"] = references
            profile["headReferenceOrder"] = order
            db.execute(
                "UPDATE model_profiles SET profile_json = ?, updated_at = ? WHERE id = ?",
                (dumps(normalize_model_profile(profile)), now_iso(), model_profile_id),
            )
            log_event(
                db,
                "model_profile",
                model_profile_id,
                "head_reference_added",
                {"items": saved},
            )
        except Exception:
            cleanup_objects([key for saved_key in saved_keys for key in image_object_keys(saved_key)])
            raise

    for old_filename in old_files:
        remove_model_asset_file(asset_dir, old_filename)
    return {"item": get_model_profile_by_id(model_profile_id)}


@app.patch("/api/model-profiles/{model_profile_id}/head-references/{reference_id}")
def update_model_head_reference(model_profile_id: int, reference_id: str, payload: HeadReferenceUpdateRequest) -> dict[str, Any]:
    clean_reference_id = sanitize_reference_id(reference_id)
    new_angle = sanitize_reference_key(payload.angle or "") if payload.angle is not None and payload.angle != "" else ""
    if new_angle and new_angle not in HEAD_REFERENCE_ANGLES:
        raise HTTPException(status_code=400, detail="头像角度不合法")

    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (model_profile_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="数字模特不存在")
        profile = loads(row["profile_json"], {})
        references = dict(profile.get("headReferences") or {})
        if clean_reference_id not in references:
            raise HTTPException(status_code=404, detail="头像角度不存在")
        order = normalize_head_reference_order(profile, references)

        next_reference_id = clean_reference_id
        if payload.angle is not None:
            if new_angle and new_angle != clean_reference_id:
                if new_angle in references:
                    raise HTTPException(status_code=409, detail="该角度已经有头像")
                references[new_angle] = references.pop(clean_reference_id)
                order = [new_angle if item == clean_reference_id else item for item in order]
                next_reference_id = new_angle
            elif new_angle == "" and not is_unassigned_head_reference_id(clean_reference_id):
                pending_id = new_unassigned_head_reference_key(references)
                references[pending_id] = references.pop(clean_reference_id)
                order = [pending_id if item == clean_reference_id else item for item in order]
                next_reference_id = pending_id
            elif new_angle == "" and is_unassigned_head_reference_id(clean_reference_id):
                next_reference_id = clean_reference_id
            elif new_angle == clean_reference_id:
                next_reference_id = clean_reference_id
            else:
                raise HTTPException(status_code=409, detail="该角度已经有头像")

        if payload.orderIndex is not None:
            order = [item for item in order if item in references and item != next_reference_id]
            order_index = max(0, min(payload.orderIndex, len(order)))
            order.insert(order_index, next_reference_id)

        profile["headReferences"] = references
        profile["headReferenceOrder"] = order
        db.execute(
            "UPDATE model_profiles SET profile_json = ?, updated_at = ? WHERE id = ?",
            (dumps(normalize_model_profile(profile)), now_iso(), model_profile_id),
        )
        log_event(
            db,
            "model_profile",
            model_profile_id,
            "head_reference_updated",
            {"referenceId": next_reference_id, "order": order},
        )
    return {"item": get_model_profile_by_id(model_profile_id)}


@app.delete("/api/model-profiles/{model_profile_id}/head-references/{reference_id}")
def delete_model_head_reference(model_profile_id: int, reference_id: str) -> dict[str, Any]:
    clean_reference_id = sanitize_reference_id(reference_id)
    if not clean_reference_id:
        raise HTTPException(status_code=400, detail="头像角度不能为空")
    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (model_profile_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="数字模特不存在")
        profile = loads(row["profile_json"], {})
        references = dict(profile.get("headReferences") or {})
        filename = references.pop(clean_reference_id, None)
        if filename is None:
            raise HTTPException(status_code=404, detail="头像角度不存在")
        order = [item for item in normalize_head_reference_order(profile, references) if item != clean_reference_id]
        profile["headReferences"] = references
        profile["headReferenceOrder"] = order
        db.execute(
            "UPDATE model_profiles SET profile_json = ?, updated_at = ? WHERE id = ?",
            (dumps(normalize_model_profile(profile)), now_iso(), model_profile_id),
        )
        log_event(db, "model_profile", model_profile_id, "head_reference_deleted", {"referenceId": clean_reference_id, "filename": filename})

    remove_model_asset_file(row["asset_dir"], filename)
    return {"item": get_model_profile_by_id(model_profile_id)}


@app.get("/api/model-profiles/{model_profile_id}/assets/{filename}")
def get_model_asset(model_profile_id: int, filename: str) -> RedirectResponse:
    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (model_profile_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="数字模特不存在")
    key = join_key(row["asset_dir"], sanitize_filename(filename))
    url = presigned_get_url(key) if object_exists(key) else None
    if not url:
        raise HTTPException(status_code=404, detail="头像文件不存在")
    return RedirectResponse(url)


@app.post("/api/shot-plans")
async def create_shot_plan(payload: PlanCreateRequest) -> dict[str, Any]:
    with get_db() as db:
        sku_row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (payload.skuId,)).fetchone()
        if not sku_row:
            raise HTTPException(status_code=404, detail="货号不存在")
        model_row = resolve_model_profile(db, payload.modelProfileId)
        timestamp = now_iso()
        plan = build_pending_shot_plan(sku_row["sku"], model_row["model_id"])
        cursor = db.execute(
            """
            INSERT INTO shot_plans
              (sku_id, model_profile_id, status, plan_json, category, sales_info,
               lily_tone, styling, background, planner_provider, raw_response, created_at, updated_at)
            VALUES (?, ?, 'PLANNING', ?, ?, ?, ?, ?, ?, 'gemini', NULL, ?, ?)
            """,
            (
                payload.skuId,
                model_row["id"],
                dumps(plan),
                plan.get("category"),
                plan_generation(plan).get("salesInfo"),
                plan_generation(plan).get("lilyTone"),
                plan_generation(plan).get("styling"),
                plan_generation(plan).get("background"),
                timestamp,
                timestamp,
            ),
        )
        plan_id = int(cursor.lastrowid)
        sync_shot_plan_shots(db, plan_id, plan, timestamp)
        log_event(
            db,
            "shot_plan",
            plan_id,
            "planner_queued",
            {"skuId": payload.skuId, "modelProfileId": model_row["id"], "plannerProvider": "gemini"},
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


@app.patch("/api/shot-plans/{shot_plan_id}")
def update_shot_plan(shot_plan_id: int, payload: ShotPlanUpdateRequest) -> dict[str, Any]:
    timestamp = now_iso()
    with get_db() as db:
        row = db.execute("SELECT * FROM shot_plans WHERE id = ?", (shot_plan_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="组图方案不存在")
        sku_row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (row["sku_id"],)).fetchone()
        plan = normalize_editable_plan(payload.plan, sku_row["sku"])
        db.execute(
            """
            UPDATE shot_plans
            SET plan_json = ?, category = ?, sales_info = ?,
                lily_tone = ?, styling = ?, background = ?,
                status = 'DRAFT', updated_at = ?
            WHERE id = ?
            """,
            (
                dumps(plan),
                plan.get("category") or plan.get("garment", {}).get("category"),
                plan_generation(plan).get("salesInfo"),
                plan_generation(plan).get("lilyTone"),
                plan_generation(plan).get("styling"),
                plan_generation(plan).get("background"),
                timestamp,
                shot_plan_id,
            ),
        )
        sync_shot_plan_shots(db, shot_plan_id, plan, timestamp)
        db.execute(
            "UPDATE garment_skus SET analysis_json = ?, status = 'ANALYZED', updated_at = ? WHERE id = ?",
            (dumps(plan.get("garment", {})), timestamp, row["sku_id"]),
        )
        log_event(db, "shot_plan", shot_plan_id, "updated", {"plan": plan})
        updated = db.execute("SELECT * FROM shot_plans WHERE id = ?", (shot_plan_id,)).fetchone()
    return {"item": serialize_shot_plan(updated)}


@app.get("/api/garment-skus/{sku_id}/default-shot-plan")
def get_default_shot_plan(sku_id: int) -> dict[str, Any]:
    with get_db() as db:
        sku_row = db.execute("SELECT id FROM garment_skus WHERE id = ?", (sku_id,)).fetchone()
        if not sku_row:
            raise HTTPException(status_code=404, detail="货号不存在")
        row = db.execute(
            """
            SELECT *
            FROM shot_plans
            WHERE sku_id = ? AND planner_provider = 'gemini' AND status <> 'FAILED'
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (sku_id,),
        ).fetchone()
        return {"item": serialize_shot_plan(row) if row else None}


@app.post("/api/preview-runs")
async def generate_preview_grid(payload: GenerateGroupRequest) -> dict[str, Any]:
    with get_db() as db:
        plan_row = db.execute("SELECT * FROM shot_plans WHERE id = ?", (payload.shotPlanId,)).fetchone()
        if not plan_row:
            raise HTTPException(status_code=404, detail="组图方案不存在")
        sku_row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (plan_row["sku_id"],)).fetchone()
        model_row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (plan_row["model_profile_id"],)).fetchone()

    plan = loads(plan_row["plan_json"], {})
    plan["_planner"] = {
        "provider": plan_row["planner_provider"],
        "rawResponse": plan_row["raw_response"],
    }
    output_dir = join_key("outputs", sku_row["sku"], f"preview-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    timestamp = now_iso()
    provider = get_image_provider()
    preview_shot = build_preview_shot()
    task_package = build_preview_task_package(sku_row, model_row, plan, preview_shot)

    with get_db() as db:
        run_cursor = db.execute(
            """
            INSERT INTO generation_runs
              (sku_id, shot_plan_id, model_profile_id, kind, status, output_dir, created_at, updated_at)
            VALUES (?, ?, ?, 'preview_grid', 'RUNNING', ?, ?, ?)
            """,
            (sku_row["id"], plan_row["id"], model_row["id"], str(output_dir), timestamp, timestamp),
        )
        run_id = int(run_cursor.lastrowid)
        log_event(db, "generation_run", run_id, "preview_started", {"shotPlanId": plan_row["id"], "outputDir": str(output_dir)})

    image_id = create_generation_image(
        run_id=run_id,
        plan=plan,
        shot=preview_shot,
        provider=provider,
        output_dir=output_dir,
        task_package_override=task_package,
    )
    update_generation_run_status(run_id)
    with get_db() as db:
        log_event(db, "generation_run", run_id, "preview_submitted", {"imageIds": [image_id]})
    return {"item": get_generation_run_by_id(run_id)}


@app.post("/api/generation-runs")
async def generate_group(payload: GenerateGroupRequest) -> dict[str, Any]:
    with get_db() as db:
        plan_row = db.execute("SELECT * FROM shot_plans WHERE id = ?", (payload.shotPlanId,)).fetchone()
        if not plan_row:
            raise HTTPException(status_code=404, detail="组图方案不存在")
        sku_row = db.execute("SELECT * FROM garment_skus WHERE id = ?", (plan_row["sku_id"],)).fetchone()
        model_row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (plan_row["model_profile_id"],)).fetchone()
        preview_run_row = None
        if payload.previewRunId:
            preview_run_row = db.execute(
                """
                SELECT *
                FROM generation_runs
                WHERE id = ? AND sku_id = ? AND shot_plan_id = ?
                """,
                (payload.previewRunId, sku_row["id"], plan_row["id"]),
            ).fetchone()
            if not preview_run_row:
                raise HTTPException(status_code=400, detail="分镜预览记录不存在或不属于当前方案")
            preview_image_row = db.execute(
                """
                SELECT *
                FROM generation_images
                WHERE run_id = ? AND shot_index = 0 AND status = 'SUCCEEDED' AND output_path IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (payload.previewRunId,),
            ).fetchone()
            if not preview_image_row:
                raise HTTPException(status_code=422, detail="分镜预览尚未生成成功，不能用于最终生成")
            existing_final_count = db.execute(
                "SELECT COUNT(*) AS count FROM generation_images WHERE run_id = ? AND shot_index > 0",
                (payload.previewRunId,),
            ).fetchone()["count"]
            if int(existing_final_count or 0) > 0:
                return {"item": get_generation_run_by_id(int(payload.previewRunId))}

    plan = loads(plan_row["plan_json"], {})
    plan["_planner"] = {
        "provider": plan_row["planner_provider"],
        "rawResponse": plan_row["raw_response"],
    }
    output_dir = clean_key(preview_run_row["output_dir"]) if preview_run_row else join_key("outputs", sku_row["sku"], datetime.now().strftime("%Y%m%d-%H%M%S"))
    timestamp = now_iso()
    provider = get_image_provider()

    if preview_run_row:
        run_id = int(preview_run_row["id"])
        with get_db() as db:
            db.execute(
                "UPDATE generation_runs SET kind = 'group', status = 'RUNNING', updated_at = ? WHERE id = ?",
                (timestamp, run_id),
            )
            log_event(db, "generation_run", run_id, "final_generation_started", {"shotPlanId": plan_row["id"], "outputDir": str(output_dir)})
    else:
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

    preview_inputs = get_preview_grid_inputs(payload.previewRunId, plan_row["id"]) if payload.previewRunId else []
    generated_ids: list[int] = []
    for shot in plan.get("shots", []):
        task_package = build_task_package(sku_row, model_row, plan, shot, preview_inputs=preview_inputs)
        image_id = create_generation_image(
            run_id=run_id,
            plan=plan,
            shot=shot,
            provider=provider,
            output_dir=output_dir,
            task_package_override=task_package,
        )
        generated_ids.append(image_id)
    update_generation_run_status(run_id)
    with get_db() as db:
        log_event(db, "generation_run", run_id, "submitted", {"imageIds": generated_ids, "previewRunId": payload.previewRunId})

    return {"item": get_generation_run_by_id(run_id)}


@app.get("/api/generation-runs")
def list_generation_runs(
    include_archived: bool = Query(False, alias="includeArchived"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    sku_id: int | None = Query(None, alias="skuId"),
    keyword: str | None = Query(None),
) -> dict[str, Any]:
    search = f"%{keyword.strip()}%" if keyword and keyword.strip() else None
    with get_db() as db:
        rows = db.execute(
            """
            SELECT gr.*, gs.sku, gs.title AS sku_title,
              (SELECT COUNT(*) FROM generation_images gi WHERE gi.run_id = gr.id) AS image_count,
              (SELECT COUNT(*) FROM generation_images gi WHERE gi.run_id = gr.id AND gi.status = 'SUCCEEDED') AS succeeded_count,
              (SELECT COUNT(*) FROM generation_images gi WHERE gi.run_id = gr.id AND gi.is_final = 1) AS final_count,
              (SELECT COALESCE(SUM(CASE WHEN gi.current_attempt_index > 1 THEN gi.current_attempt_index - 1 ELSE 0 END), 0)
                 FROM generation_images gi WHERE gi.run_id = gr.id) AS regeneration_count
            FROM generation_runs gr
            JOIN garment_skus gs ON gs.id = gr.sku_id
            WHERE (? OR gr.archived_at IS NULL)
              AND (? IS NULL OR gr.sku_id = ?)
              AND (? IS NULL OR gs.sku LIKE ? OR gs.title LIKE ? OR gr.status LIKE ? OR CAST(gr.id AS TEXT) LIKE ?)
            ORDER BY gr.created_at DESC, gr.id DESC
            LIMIT ? OFFSET ?
            """,
            (include_archived, sku_id, sku_id, search, search, search, search, search, limit + 1, offset),
        ).fetchall()
        items = rows[:limit]
        return {
            "items": [serialize_generation_run(row, include_images=False, include_summary=True) for row in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if len(rows) > limit else None,
            "hasMore": len(rows) > limit,
        }


@app.get("/api/generation-runs/{run_id}")
def get_generation_run(run_id: int) -> dict[str, Any]:
    return {"item": get_generation_run_by_id(run_id)}


@app.delete("/api/generation-runs/{run_id}")
def delete_generation_run(run_id: int) -> dict[str, bool]:
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成记录不存在")
        if "archived_at" in row.keys() and row["archived_at"]:
            return {"ok": True}
        timestamp = now_iso()
        db.execute(
            "UPDATE generation_runs SET archived_at = ?, archive_reason = ?, updated_at = ? WHERE id = ?",
            (timestamp, "user_archived", timestamp, run_id),
        )
        log_event(
            db,
            "generation_run",
            run_id,
            "archived",
            {"skuId": row["sku_id"], "shotPlanId": row["shot_plan_id"], "outputDir": row["output_dir"]},
        )

    return {"ok": True}


@app.post("/api/generation-runs/{run_id}/cancel")
def cancel_generation_run(run_id: int) -> dict[str, Any]:
    timestamp = now_iso()
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成记录不存在")
        active_count = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM generation_images
            WHERE run_id = ? AND status IN ('QUEUED', 'SUBMITTED', 'IN_QUEUE', 'IN_PROGRESS', 'RUNNING')
            """,
            (run_id,),
        ).fetchone()["count"]
        if not active_count:
            raise HTTPException(status_code=409, detail="生成记录已结束，不能取消")
        cursor = db.execute(
            """
            UPDATE generation_images
            SET status = 'CANCELLED', cancel_requested_at = COALESCE(cancel_requested_at, ?),
                completed_at = COALESCE(completed_at, ?), locked_by = NULL, locked_until = NULL,
                updated_at = ?
            WHERE run_id = ? AND status IN ('QUEUED', 'SUBMITTED', 'IN_QUEUE', 'IN_PROGRESS', 'RUNNING')
            """,
            (timestamp, timestamp, timestamp, run_id),
        )
        if cursor.rowcount <= 0:
            raise HTTPException(status_code=409, detail="生成记录已结束，不能取消")
        log_event(
            db,
            "generation_run",
            run_id,
            "cancelled",
            {"cancelledAt": timestamp, "cancelledImageCount": cursor.rowcount},
        )
    update_generation_run_status(run_id)
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
        db.execute("UPDATE generation_images SET status = 'QUEUED', updated_at = ? WHERE id = ?", (now_iso(), image_id))

    image_task_package = loads(image_row["task_package_json"], {})
    image_stage = image_task_package.get("generation", {}).get("stage") if isinstance(image_task_package.get("generation"), dict) else None
    if int(image_row["shot_index"]) == 0 or image_stage == "preview_grid":
        with get_db() as db:
            plan_row = get_latest_usable_shot_plan_row(db, sku_row["id"]) or db.execute(
                "SELECT * FROM shot_plans WHERE id = ?",
                (run_row["shot_plan_id"],),
            ).fetchone()
        if not plan_row:
            raise HTTPException(status_code=404, detail="组图方案不存在")
        plan = loads(plan_row["plan_json"], {})
        plan["_planner"] = {
            "provider": plan_row["planner_provider"],
            "rawResponse": plan_row["raw_response"],
        }
        task_package = build_preview_task_package(sku_row, model_row, plan, build_preview_shot())
        if plan_row["id"] != run_row["shot_plan_id"]:
            with get_db() as db:
                db.execute(
                    "UPDATE generation_runs SET shot_plan_id = ?, updated_at = ? WHERE id = ?",
                    (plan_row["id"], now_iso(), run_row["id"]),
                )
            run_row = {**dict(run_row), "shot_plan_id": plan_row["id"]}
    else:
        task_package = image_task_package
    task_package["regeneration"] = {"fromImageId": image_id, "reason": payload.reason, "note": payload.note}
    output_dir = clean_key(run_row["output_dir"])
    shot = task_package.get("shot", {})
    shot_index = int(shot.get("shotIndex", image_row["shot_index"]))
    title = str(shot.get("title", image_row["title"]))
    suffix = f"{shot_index:02d}-{sanitize_filename(title)}-regen-{datetime.now().strftime('%H%M%S')}"
    provider = get_image_provider()
    output_path = join_key(output_dir, f"{suffix}{provider.output_suffix()}")

    with get_db() as db:
        log_event(
            db,
            "generation_image",
            image_id,
            "regenerate_requested",
            {"reason": payload.reason, "note": payload.note},
        )
        db.execute(
            """
            UPDATE generation_images
            SET status = 'QUEUED', output_path = ?, task_package_json = ?,
                provider_request_json = NULL, provider_response_json = NULL, prompt_text = NULL,
                image_inputs_json = ?, current_attempt_index = current_attempt_index + 1,
                provider_request_id = NULL, provider_status = NULL, provider_urls_json = NULL,
                provider_status_json = NULL, submitted_at = NULL, completed_at = NULL,
                last_status_checked_at = NULL, locked_by = NULL, locked_until = NULL,
                submit_attempts = 0, status_check_attempts = 0, status_error_attempts = 0,
                cancel_requested_at = NULL,
                timeout_at = ?, regenerate_reason = ?, review_status = 'candidate',
                review_note = NULL, reviewed_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                str(output_path),
                dumps(task_package),
                dumps(task_package.get("imageInputs", [])),
                future_iso(settings.generation_image_timeout_seconds),
                payload.reason,
                now_iso(),
                image_id,
            ),
        )
    update_generation_run_status(run_row["id"])
    return {"item": get_generation_run_by_id(run_row["id"])}


@app.post("/api/generation-images/{image_id}/cancel")
def cancel_generation_image(image_id: int) -> dict[str, Any]:
    timestamp = now_iso()
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成图片不存在")
        if row["status"] not in ACTIVE_IMAGE_STATUSES:
            return {"item": serialize_generation_image(row)}
        db.execute(
            """
            UPDATE generation_images
            SET status = 'CANCELLED', cancel_requested_at = COALESCE(cancel_requested_at, ?),
                completed_at = COALESCE(completed_at, ?), locked_by = NULL, locked_until = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (timestamp, timestamp, timestamp, image_id),
        )
        log_event(db, "generation_image", image_id, "cancelled", {"cancelledAt": timestamp})
        updated = db.execute("SELECT * FROM generation_images WHERE id = ?", (image_id,)).fetchone()
    update_generation_run_status(row["run_id"])
    return {"item": serialize_generation_image(updated)}


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


@app.patch("/api/generation-images/{image_id}/review")
def set_generation_image_review_status(image_id: int, payload: ReviewStatusRequest) -> dict[str, Any]:
    normalized_status = payload.status.strip().lower()
    if normalized_status not in REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail="图片审核状态不合法")
    timestamp = now_iso()
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成图片不存在")
        db.execute(
            """
            UPDATE generation_images
            SET review_status = ?, review_note = ?, reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (normalized_status, payload.note, timestamp, timestamp, image_id),
        )
        if normalized_status == "delivered":
            db.execute(
                "UPDATE generation_runs SET delivery_status = 'delivered', delivered_at = COALESCE(delivered_at, ?), updated_at = ? WHERE id = ?",
                (timestamp, timestamp, row["run_id"]),
            )
        log_event(db, "generation_image", image_id, "review_updated", {"status": normalized_status, "note": payload.note})
        updated = db.execute("SELECT * FROM generation_images WHERE id = ?", (image_id,)).fetchone()
    return {"item": serialize_generation_image(updated)}


@app.get("/api/logs")
def list_logs(limit: int = 100) -> dict[str, Any]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM event_logs ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return {"items": [serialize_log(row) for row in rows]}


@app.post("/api/exports/runs/{run_id}")
def export_run(run_id: int) -> StreamingResponse:
    run = get_generation_run_by_id(run_id)
    images = run.get("images", [])
    group_images = [image for image in images if int(image.get("shotIndex") or 0) > 0]
    if not group_images:
        raise HTTPException(status_code=422, detail="没有可导出的最终组图")
    incomplete_images = [
        image
        for image in group_images
        if image.get("status") != "SUCCEEDED" or not image.get("objectKey")
    ]
    if incomplete_images or run.get("status") != "SUCCEEDED":
        raise HTTPException(status_code=409, detail="最终组图尚未全部生成成功，不能导出 ZIP")
    final_images = [image for image in group_images if image.get("isFinal")]
    selected = final_images or group_images
    if not selected:
        raise HTTPException(status_code=422, detail="没有可导出的生成图")

    zip_name = f"run-{run_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    zip_buffer = BytesIO()
    written = 0
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for image in selected:
            object_key = image.get("objectKey")
            if not object_key:
                continue
            try:
                archive.writestr(Path(object_key).name, get_bytes(object_key))
                written += 1
            except Exception:
                continue
    if written == 0:
        raise HTTPException(status_code=422, detail="没有可导出的生成图")
    zip_buffer.seek(0)
    export_key = join_key("exports", str(run["sku"] or run["skuId"]), zip_name)
    put_fileobj(export_key, zip_buffer, "application/zip")
    zip_buffer.seek(0)

    with get_db() as db:
        log_event(
            db,
            "generation_run",
            run_id,
            "exported",
            {"objectKey": export_key, "imageCount": written, "finalOnly": bool(final_images)},
        )
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


def create_generation_image(
    run_id: int,
    plan: dict[str, Any],
    shot: dict[str, Any],
    provider: Any,
    output_dir: str,
    regenerate_from_image_id: int | None = None,
    regenerate_reason: str | None = None,
    task_package_override: dict[str, Any] | None = None,
) -> int:
    shot_index = int(shot.get("shotIndex", 1))
    title = str(shot.get("title", f"图 {shot_index}"))
    suffix = f"{shot_index:02d}-{sanitize_filename(title)}"
    if regenerate_from_image_id:
        suffix = f"{suffix}-regen-{datetime.now().strftime('%H%M%S')}"
    output_path = join_key(output_dir, f"{suffix}{provider.output_suffix()}")
    if task_package_override is None:
        raise ValueError("create_generation_image requires task_package_override")
    task_package = task_package_override
    timestamp = now_iso()
    timeout_at = future_iso(settings.generation_image_timeout_seconds)

    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO generation_images
              (run_id, shot_index, title, status, output_path, task_package_json, regenerate_from_image_id,
               regenerate_reason, image_inputs_json, template_snapshot_json, model_snapshot_json,
               garment_snapshot_json, timeout_at, is_final, created_at, updated_at)
            VALUES (?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                shot_index,
                title,
                str(output_path),
                dumps(task_package),
                regenerate_from_image_id,
                regenerate_reason,
                dumps(task_package.get("imageInputs", [])),
                dumps({
                    "brandStyle": task_package.get("brandStyle", plan.get("brandStyle", {})),
                    "brandTemplate": task_package.get("brandTemplate", {}),
                    "shot": shot,
                    "plan": plan,
                }),
                dumps(task_package.get("model", {})),
                dumps(task_package.get("garment", {})),
                timeout_at,
                1 if shot_index > 0 else 0,
                timestamp,
                timestamp,
            ),
        )
        image_id = int(cursor.lastrowid)
        log_event(db, "generation_image", image_id, "queued", {"runId": run_id, "shot": shot, "outputPath": str(output_path)})
    return image_id


def future_iso(seconds: int) -> str:
    return datetime.fromtimestamp(time.time() + seconds).isoformat(timespec="seconds")


def lock_until_iso() -> str:
    return future_iso(settings.generation_lock_ttl_seconds)


def release_generation_image_lock(db: Any, image_id: int) -> None:
    db.execute("UPDATE generation_images SET locked_by = NULL, locked_until = NULL WHERE id = ?", (image_id,))


def claim_generation_image(image_id: int, worker_id: str, status: str) -> Any | None:
    timestamp = now_iso()
    lock_until = lock_until_iso()
    attempt_column = "submit_attempts" if status == "QUEUED" else "status_check_attempts"
    with get_db() as db:
        cursor = db.execute(
            f"""
            UPDATE generation_images
            SET locked_by = ?, locked_until = ?, {attempt_column} = {attempt_column} + 1, updated_at = ?
            WHERE id = ?
              AND status = ?
              AND (locked_until IS NULL OR locked_until < ?)
              AND cancel_requested_at IS NULL
            """,
            (worker_id, lock_until, timestamp, image_id, status, timestamp),
        )
        if cursor.rowcount != 1:
            return None
        return db.execute("SELECT * FROM generation_images WHERE id = ?", (image_id,)).fetchone()


def fail_generation_image(db: Any, image_id: int, status: str, payload: dict[str, Any]) -> None:
    timestamp = now_iso()
    db.execute(
        """
        UPDATE generation_images
        SET status = 'FAILED', provider_status = ?, provider_status_json = ?,
            completed_at = COALESCE(completed_at, ?), last_status_checked_at = ?,
            locked_by = NULL, locked_until = NULL, updated_at = ?
        WHERE id = ?
        """,
        (status, dumps(payload), timestamp, timestamp, timestamp, image_id),
    )


def submit_generation_image(provider: Any, image_row: Any) -> None:
    image_id = int(image_row["id"])
    task_package = loads(image_row["task_package_json"], {})
    output_path = image_row["output_path"]
    if not output_path:
        with get_db() as db:
            fail_generation_image(db, image_id, "MISSING_OUTPUT_PATH", {"error": "缺少输出路径，无法提交生成任务"})
            log_event(db, "generation_image", image_id, "failed", {"error": "缺少输出路径，无法提交生成任务"})
        update_generation_run_status(image_row["run_id"])
        return
    try:
        provider_response = asyncio.run(provider.submit(task_package, output_path))
    except Exception as exc:
        submit_attempts = int(row_value(image_row, "submit_attempts") or 0)
        with get_db() as db:
            if submit_attempts >= settings.generation_submit_retry_limit:
                fail_generation_image(db, image_id, "SUBMIT_FAILED", {"error": str(exc), "attempts": submit_attempts})
                log_event(db, "generation_image", image_id, "failed", {"error": str(exc), "attempts": submit_attempts})
            else:
                release_generation_image_lock(db, image_id)
                log_event(db, "generation_image", image_id, "submit_retry_scheduled", {"error": str(exc), "attempts": submit_attempts})
        update_generation_run_status(image_row["run_id"])
        return
    provider_request = provider_response.get("providerRequest") or {"taskPackage": task_package, "outputPath": str(output_path)}
    prompt_text = provider_request.get("prompt") if isinstance(provider_request, dict) else None
    next_status = provider_response.get("status", "SUBMITTED")
    provider_status = provider_response.get("providerStatus")
    provider_request_id = provider_response.get("providerRequestId")
    provider_urls = provider_response.get("providerUrls", {})
    provider_status_response = provider_response.get("providerStatusResponse")
    completed_at = now_iso() if next_status in TERMINAL_IMAGE_STATUSES else None
    attempt = {
        "attemptIndex": max(1, int(image_row["current_attempt_index"] or 0)),
        "kind": "regeneration" if task_package.get("regeneration") else "initial",
        "reason": task_package.get("regeneration", {}).get("reason") if isinstance(task_package.get("regeneration"), dict) else image_row["regenerate_reason"],
        "note": task_package.get("regeneration", {}).get("note") if isinstance(task_package.get("regeneration"), dict) else None,
        "prompt": prompt_text,
        "imageInputs": provider_response.get("imageInputs", task_package.get("imageInputs", [])),
        "providerRequest": provider_request,
        "providerResponse": provider_response.get("providerResponse", provider_response),
        "providerStatusResponse": provider_status_response,
        "providerRequestId": provider_request_id,
        "providerStatus": provider_status,
        "outputPath": str(output_path),
        "createdAt": now_iso(),
    }
    old_attempts = loads(image_row["attempts_json"], []) or []
    attempts = [*old_attempts, attempt]
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_images
            SET status = ?, output_path = ?, provider_request_json = ?,
                provider_response_json = ?, prompt_text = ?, image_inputs_json = ?,
                attempts_json = ?, current_attempt_index = ?, provider_request_id = ?,
                provider_status = ?, provider_urls_json = ?, provider_status_json = ?,
                submitted_at = ?, completed_at = ?, last_status_checked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                str(output_path),
                dumps(provider_request),
                dumps(provider_response.get("providerResponse", provider_response)),
                prompt_text,
                dumps(provider_response.get("imageInputs", task_package.get("imageInputs", []))),
                dumps(attempts),
                attempt["attemptIndex"],
                provider_request_id,
                provider_status,
                dumps(provider_urls),
                dumps(provider_status_response),
                now_iso(),
                completed_at,
                now_iso(),
                now_iso(),
                image_id,
            ),
        )
        release_generation_image_lock(db, image_id)
        log_event(db, "generation_image", image_id, "submitted", {"outputPath": str(output_path), "provider": provider_response})
    update_generation_run_status(image_row["run_id"])


async def generation_poller_loop() -> None:
    while True:
        await asyncio.sleep(settings.generation_poll_interval_seconds)
        try:
            await asyncio.to_thread(refresh_pending_generation_images)
        except Exception as exc:  # pragma: no cover - background safety net
            with get_db() as db:
                log_event(db, "generation_poller", None, "failed", {"error": str(exc)})


async def planner_poller_loop() -> None:
    while True:
        await asyncio.sleep(settings.generation_poll_interval_seconds)
        try:
            await asyncio.to_thread(refresh_pending_shot_plans)
        except Exception as exc:  # pragma: no cover - background safety net
            with get_db() as db:
                log_event(db, "planner_poller", None, "failed", {"error": str(exc)})


def refresh_pending_shot_plans() -> None:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT sp.*, gs.sku
            FROM shot_plans sp
            JOIN garment_skus gs ON gs.id = sp.sku_id
            WHERE sp.status = 'PLANNING'
            ORDER BY sp.created_at, sp.id
            LIMIT 2
            """
        ).fetchall()
    for row in rows:
        run_planner_for_shot_plan(row)


def run_planner_for_shot_plan(plan_row: Any) -> None:
    with get_db() as db:
        image_rows = db.execute(
            "SELECT * FROM garment_images WHERE sku_id = ? ORDER BY role = 'main' DESC, sort_order, id",
            (plan_row["sku_id"],),
        ).fetchall()
        image_keys = [row["storage_path"] for row in image_rows]

    with TemporaryDirectory(prefix="planner-inputs-") as temp_dir:
        image_paths: list[Path] = []
        for index, key in enumerate(image_keys[:6], start=1):
            try:
                path = Path(temp_dir) / f"{index:02d}-{Path(key).name}"
                path.write_bytes(get_bytes(key))
                image_paths.append(path)
            except Exception:
                continue
        try:
            plan, provider, raw = asyncio.run(plan_with_gemini(plan_row["sku"], image_paths))
        except PlannerError as exc:
            timestamp = now_iso()
            with get_db() as db:
                db.execute(
                    "UPDATE shot_plans SET status = 'FAILED', raw_response = ?, updated_at = ? WHERE id = ?",
                    (dumps({"error": str(exc), "providerRequest": exc.provider_request, "providerResponse": exc.provider_response}), timestamp, plan_row["id"]),
                )
                log_event(
                    db,
                    "shot_plan",
                    plan_row["id"],
                    "planner_failed",
                    {
                        "skuId": plan_row["sku_id"],
                        "sku": plan_row["sku"],
                        "modelProfileId": plan_row["model_profile_id"],
                        "error": str(exc),
                        "providerRequest": exc.provider_request,
                        "providerResponse": exc.provider_response,
                    },
                )
            return

    timestamp = now_iso()
    raw_snapshot = loads(raw, {})
    with get_db() as db:
        db.execute(
            """
            UPDATE shot_plans
            SET status = 'DRAFT', plan_json = ?, category = ?, sales_info = ?,
                lily_tone = ?, styling = ?, background = ?, planner_provider = ?,
                raw_response = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                dumps(plan),
                plan.get("category") or plan.get("garment", {}).get("category"),
                plan_generation(plan).get("salesInfo"),
                plan_generation(plan).get("lilyTone"),
                plan_generation(plan).get("styling"),
                plan_generation(plan).get("background"),
                provider,
                raw,
                timestamp,
                plan_row["id"],
            ),
        )
        sync_shot_plan_shots(db, plan_row["id"], plan, timestamp)
        db.execute(
            "UPDATE garment_skus SET status = 'ANALYZED', analysis_json = ?, updated_at = ? WHERE id = ?",
            (dumps(plan.get("garment", {})), timestamp, plan_row["sku_id"]),
        )
        parse_meta = raw_snapshot.get("parseMeta") if isinstance(raw_snapshot, dict) else None
        if isinstance(parse_meta, dict) and str(parse_meta.get("mode", "")).startswith("recovered"):
            log_event(
                db,
                "shot_plan",
                plan_row["id"],
                "planner_response_recovered",
                {
                    "skuId": plan_row["sku_id"],
                    "sku": plan_row["sku"],
                    "modelProfileId": plan_row["model_profile_id"],
                    "mode": parse_meta.get("mode"),
                    "parseError": parse_meta.get("parseError"),
                    "trailingText": parse_meta.get("trailingText"),
                },
            )
        log_event(
            db,
            "shot_plan",
            plan_row["id"],
            "created",
            {"skuId": plan_row["sku_id"], "modelProfileId": plan_row["model_profile_id"], "plannerProvider": provider, "plan": plan},
        )


def refresh_pending_generation_images() -> None:
    provider = get_image_provider()
    expire_stale_generation_images()
    worker_id = f"poller-{uuid4()}"
    timestamp = now_iso()
    with get_db() as db:
        queued_rows = db.execute(
            """
            SELECT gi.*
            FROM generation_images gi
            JOIN generation_runs gr ON gr.id = gi.run_id
            WHERE gi.status = 'QUEUED'
              AND gi.provider_request_id IS NULL
              AND gr.status <> 'CANCELLED'
              AND gi.cancel_requested_at IS NULL
              AND (gi.locked_until IS NULL OR gi.locked_until < ?)
              AND gi.submit_attempts < ?
            ORDER BY gi.updated_at, gi.id
            LIMIT 5
            """
            ,
            (timestamp, settings.generation_submit_retry_limit),
        ).fetchall()
        rows = db.execute(
            """
            SELECT *
            FROM generation_images
            WHERE status IN ('SUBMITTED', 'IN_QUEUE', 'IN_PROGRESS', 'RUNNING')
              AND provider_request_id IS NOT NULL
              AND cancel_requested_at IS NULL
              AND (locked_until IS NULL OR locked_until < ?)
            ORDER BY updated_at
            LIMIT 10
            """
            ,
            (timestamp,),
        ).fetchall()

    for row in queued_rows:
        claimed = claim_generation_image(int(row["id"]), worker_id, "QUEUED")
        if claimed:
            submit_generation_image(provider, claimed)
    for row in rows:
        claimed = claim_generation_image(int(row["id"]), worker_id, row["status"])
        if claimed:
            refresh_generation_image_status(provider, claimed)


def expire_stale_generation_images() -> None:
    timestamp = now_iso()
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, run_id
            FROM generation_images
            WHERE status IN ('QUEUED', 'SUBMITTED', 'IN_QUEUE', 'IN_PROGRESS', 'RUNNING')
              AND timeout_at IS NOT NULL
              AND timeout_at < ?
            """,
            (timestamp,),
        ).fetchall()
        for row in rows:
            fail_generation_image(db, int(row["id"]), "TIMEOUT", {"error": "生成任务超时", "timeoutAt": timestamp})
            log_event(db, "generation_image", row["id"], "timed_out", {"timeoutAt": timestamp})
    for run_id in {int(row["run_id"]) for row in rows}:
        update_generation_run_status(run_id)


def refresh_generation_image_status(provider: Any, image_row: Any) -> None:
    try:
        result = asyncio.run(provider.refresh(image_row))
    except Exception as exc:
        failed_at = now_iso()
        attempts = loads(image_row["attempts_json"], []) or []
        status_error_attempts = int(row_value(image_row, "status_error_attempts") or 0) + 1
        if attempts:
            attempts[-1] = {
                **attempts[-1],
                "providerStatus": "STATUS_CHECK_FAILED",
                "providerStatusResponse": {"error": str(exc)},
                **({"completedAt": failed_at} if status_error_attempts >= settings.generation_status_retry_limit else {}),
            }
        with get_db() as db:
            if status_error_attempts >= settings.generation_status_retry_limit:
                db.execute(
                    """
                    UPDATE generation_images
                    SET status = 'FAILED', provider_status = ?, provider_status_json = ?,
                        attempts_json = ?, completed_at = COALESCE(completed_at, ?),
                        last_status_checked_at = ?, locked_by = NULL, locked_until = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        "STATUS_CHECK_FAILED",
                        dumps({"error": str(exc), "attempts": status_error_attempts}),
                        dumps(attempts),
                        failed_at,
                        failed_at,
                        failed_at,
                        image_row["id"],
                    ),
                )
                log_event(db, "generation_image", image_row["id"], "status_check_failed", {"error": str(exc), "attempts": status_error_attempts})
            else:
                db.execute(
                    """
                    UPDATE generation_images
                    SET provider_status = ?, provider_status_json = ?, attempts_json = ?,
                        status_error_attempts = ?,
                        last_status_checked_at = ?, locked_by = NULL, locked_until = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        "STATUS_CHECK_RETRY",
                        dumps({"error": str(exc), "attempts": status_error_attempts}),
                        dumps(attempts),
                        status_error_attempts,
                        failed_at,
                        failed_at,
                        image_row["id"],
                    ),
                )
                log_event(db, "generation_image", image_row["id"], "status_check_retry_scheduled", {"error": str(exc), "attempts": status_error_attempts})
        update_generation_run_status(image_row["run_id"])
        return

    next_status = result.get("status", image_row["status"])
    provider_status = result.get("providerStatus")
    status_response = result.get("providerStatusResponse")
    provider_response = result.get("providerResponse")
    completed_at = now_iso() if next_status in TERMINAL_IMAGE_STATUSES else None
    attempts = loads(image_row["attempts_json"], []) or []
    if attempts:
        attempts[-1] = {
            **attempts[-1],
            "providerStatus": provider_status,
            "providerStatusResponse": status_response,
            **({"providerResponse": provider_response} if provider_response is not None else {}),
            **({"completedAt": completed_at} if completed_at else {}),
        }

    with get_db() as db:
        db.execute(
            """
            UPDATE generation_images
            SET status = ?, provider_status = ?, provider_status_json = ?,
                provider_response_json = COALESCE(?, provider_response_json),
                attempts_json = ?, completed_at = COALESCE(?, completed_at),
                status_error_attempts = 0, last_status_checked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                provider_status,
                dumps(status_response),
                dumps(provider_response) if provider_response is not None else None,
                dumps(attempts),
                completed_at,
                now_iso(),
                now_iso(),
                image_row["id"],
            ),
        )
        release_generation_image_lock(db, int(image_row["id"]))
        log_event(
            db,
            "generation_image",
            image_row["id"],
            "status_updated",
            {"status": next_status, "providerStatus": provider_status},
        )
    update_generation_run_status(image_row["run_id"])


def update_generation_run_status(run_id: int) -> None:
    with get_db() as db:
        rows = db.execute("SELECT status FROM generation_images WHERE run_id = ?", (run_id,)).fetchall()
        statuses = [row["status"] for row in rows]
        if not statuses:
            next_status = "CREATED"
        elif any(status in ACTIVE_IMAGE_STATUSES for status in statuses):
            next_status = "RUNNING"
        elif all(status == "SUCCEEDED" for status in statuses):
            next_status = "SUCCEEDED"
        elif any(status == "SUCCEEDED" for status in statuses):
            next_status = "PARTIAL_SUCCEEDED"
        elif any(status == "CANCELLED" for status in statuses):
            next_status = "CANCELLED"
        else:
            next_status = "FAILED"
        db.execute(
            "UPDATE generation_runs SET status = ?, updated_at = ? WHERE id = ?",
            (next_status, now_iso(), run_id),
        )


def build_preview_task_package(sku_row: Any, model_row: Any, plan: dict[str, Any], shot: dict[str, Any]) -> dict[str, Any]:
    package = build_task_package(sku_row, model_row, plan, shot)
    base_constraints = [
        item
        for item in package["generation"]["constraints"]
        if "分镜预览图" not in item and "预览图" not in item and "九宫格" not in item and "3x3" not in item
    ]
    package["generation"] = {
        **package["generation"],
        "stage": "preview_grid",
        "output": {"aspectRatio": "1:1", "size": "1200x1200", "layout": "2x3 contact sheet"},
        "constraints": [
            *base_constraints,
            "生成一张 2x3 分镜预览图，前 5 格对应最终组图分镜，第 6 格为备用候选姿势",
            "分镜预览用于用户确认方向，不要输出单张最终成片",
            "每格保持服装和配饰一致，不能改变商品结构、颜色、长度和比例",
        ],
    }
    package["previewPlan"] = {
        "goal": "给用户预览 5 个最终分镜和 1 个备用候选姿势",
        "shots": plan.get("shots", []),
    }
    package["imageInputs"] = build_preview_image_inputs(sku_row, model_row, package["model"]["profile"])
    return package


def build_preview_shot() -> dict[str, Any]:
    return {
        "shotIndex": 0,
        "title": "2x3 分镜预览",
        "scene": "2x3 Contact Sheet",
        "cropRange": "前 5 格对应分镜剧本，第 6 格为备用候选姿势",
        "imagePurpose": "在服装、配饰、人物一致的前提下确认最终组图的分镜方向",
        "poseTone": "6 个商业棚拍候选画面，覆盖正面、轻斜正、3/4 斜正、侧身、轻走动和备用候选姿势",
        "bodyPosition": "多身位",
        "styling": {},
    }


def build_pending_shot_plan(sku: str, model_id: str) -> dict[str, Any]:
    shots = []
    for slot in SHOT_SLOTS:
        shots.append(
            {
                "shotIndex": slot["shotIndex"],
                "englishName": slot.get("englishName"),
                "title": slot.get("title", f"图 {slot['shotIndex']}"),
                "task": "Gemini 正在生成分镜任务",
                "scene": "",
                "cropRange": "",
                "modelScale": "",
                "headTreatment": "",
                "bodyPosition": "",
                "poseTone": "",
                "imagePurpose": "",
            }
        )
    return {
        "sku": sku,
        "modelId": model_id,
        "category": "策划中",
        "garment": {"category": "策划中", "summary": "Gemini 正在识别服装品类并生成组图方案。"},
        "brandStyle": {"name": "Lily 默认棚拍风格", "summary": "方案生成中。"},
        "generationPlan": {
            "salesInfo": "",
            "lilyTone": "",
            "styling": "",
            "background": "",
        },
        "shots": shots,
    }


def build_task_package(
    sku_row: Any,
    model_row: Any,
    plan: dict[str, Any],
    shot: dict[str, Any],
    preview_inputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    garment = plan.get("garment", {})
    model_profile = loads(model_row["profile_json"], {})
    if preview_inputs:
        image_inputs = build_final_image_inputs(sku_row, model_row, model_profile, preview_inputs)
    else:
        image_inputs = build_image_inputs(sku_row, model_row, model_profile, shot)
    brand_template = get_active_brand_template()
    brand_style = {
        **brand_template.get("snapshot", {}),
        **(plan.get("brandStyle", {}) if isinstance(plan.get("brandStyle"), dict) else {}),
        "templateId": brand_template.get("templateId"),
        "templateVersion": brand_template.get("version"),
    }
    if sku_row["sku"] == "125309C7948510":
        garment = {
            **garment,
            "proportionConstraints": (
                "裙长保持膝上中段；裙摆为克制轻 A 字，不要变成大伞摆；"
                "蓝白条纹只作为袖窿和下摆窄装饰层，不要扩大成大面积裙片。"
            ),
        }
    return {
        "sku": sku_row["sku"],
        "garment": garment,
        "model": {
            "modelId": model_row["model_id"],
            "profile": model_profile,
            "identityLock": "high",
        },
        "brandStyle": brand_style,
        "brandTemplate": brand_template,
        "planningScript": {
            "category": plan.get("category") or garment.get("category"),
            **plan_generation(plan),
        },
        "planner": plan.get("_planner", {}),
        "shot": shot,
        "imageInputs": image_inputs,
        "generation": {
            "targetModel": "GPT Image-2",
            "provider": "fal",
            "stage": "final_group",
            "output": {"aspectRatio": "1:1", "size": "1200x1200"},
            "constraints": [
                "服装素材参考图是唯一商品事实源",
                "保持同一模特脸、发型、气质",
                "保持白/浅灰棚拍风格",
                "不生成无关文字、水印、logo",
                "如果提供了分镜预览图，只把它作为姿势、身位、景别、构图、背景光线和搭配方向的参考",
            ],
        },
    }


def get_preview_grid_inputs(preview_run_id: int | None, shot_plan_id: int) -> list[dict[str, Any]]:
    if not preview_run_id:
        return []
    with get_db() as db:
        run_row = db.execute(
            "SELECT * FROM generation_runs WHERE id = ? AND shot_plan_id = ?",
            (preview_run_id, shot_plan_id),
        ).fetchone()
        if not run_row:
            raise HTTPException(status_code=400, detail="分镜预览记录不存在或不属于当前方案")
        image_rows = db.execute(
            """
            SELECT *
            FROM generation_images
            WHERE run_id = ? AND shot_index = 0 AND status = 'SUCCEEDED' AND output_path IS NOT NULL
            ORDER BY shot_index, id
            LIMIT 1
            """,
            (preview_run_id,),
        ).fetchall()
    inputs: list[dict[str, Any]] = []
    for row in image_rows:
        object_key = row["output_path"]
        inputs.append(
            {
                "role": "preview_grid",
                "filename": Path(object_key).name,
                "objectKey": object_key,
                "url": presigned_get_url(object_key),
            }
        )
    if not inputs:
        raise HTTPException(status_code=422, detail="分镜预览尚未生成成功，不能用于最终生成")
    return inputs


def normalize_editable_plan(plan: dict[str, Any], sku: str) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise HTTPException(status_code=400, detail="方案格式不合法")
    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        raise HTTPException(status_code=400, detail="方案至少需要一个图位")
    normalized = normalize_planner_plan(dict(plan), sku)
    if len(normalized.get("shots", [])) != 5:
        raise HTTPException(status_code=400, detail="方案必须包含 5 个固定图位")
    return normalized


def plan_generation(plan: dict[str, Any]) -> dict[str, Any]:
    generation_plan = plan.get("generationPlan") if isinstance(plan.get("generationPlan"), dict) else {}
    styling = generation_plan.get("styling")
    if not styling:
        legacy_parts = [
            plan.get("styling"),
            plan.get("stylingPlan"),
            plan.get("colorPalette"),
            plan.get("accessories"),
        ]
        styling = "；".join(str(part).strip() for part in legacy_parts if str(part or "").strip())
    return {
        "salesInfo": generation_plan.get("salesInfo") or plan.get("salesInfo"),
        "lilyTone": generation_plan.get("lilyTone") or plan.get("lilyTone"),
        "styling": styling,
        "background": generation_plan.get("background") or plan.get("background"),
    }


def get_active_brand_template() -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT *
            FROM brand_template_versions
            WHERE is_active = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        return {
            "templateId": "inline-default",
            "version": "unversioned",
            "name": "默认棚拍模板",
            "snapshot": {},
        }
    return {
        "id": row["id"],
        "templateId": row["template_id"],
        "version": row["version"],
        "name": row["name"],
        "snapshot": loads(row["snapshot_json"], {}),
    }


def get_latest_usable_shot_plan_row(db: Any, sku_id: int) -> Any:
    return db.execute(
        """
        SELECT *
        FROM shot_plans
        WHERE sku_id = ? AND planner_provider = 'gemini' AND status NOT IN ('FAILED', 'PLANNING')
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (sku_id,),
    ).fetchone()


def sync_shot_plan_shots(db: Any, shot_plan_id: int, plan: dict[str, Any], timestamp: str) -> None:
    db.execute("DELETE FROM shot_plan_shots WHERE shot_plan_id = ?", (shot_plan_id,))
    for raw_shot in plan.get("shots", []):
        shot = dict(raw_shot)
        shot_index = int(shot.get("shotIndex", 1))
        slot = next((item for item in SHOT_SLOTS if item["shotIndex"] == shot_index), SHOT_SLOTS[0])
        db.execute(
            """
            INSERT INTO shot_plan_shots
              (shot_plan_id, shot_index, english_name, title, task, scene, crop_range,
               model_scale, head_treatment, body_position, pose_tone, image_purpose,
               shot_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                shot_plan_id,
                shot_index,
                str(shot.get("englishName") or slot["englishName"]),
                str(shot.get("title") or slot["title"]),
                str(shot.get("task") or slot["task"]),
                str(shot.get("scene") or ""),
                str(shot.get("cropRange") or ""),
                str(shot.get("modelScale") or ""),
                str(shot.get("headTreatment") or ""),
                str(shot.get("bodyPosition") or ""),
                str(shot.get("poseTone") or ""),
                str(shot.get("imagePurpose") or ""),
                dumps(shot),
                timestamp,
                timestamp,
            ),
        )


def backfill_shot_plan_script_data() -> None:
    timestamp = now_iso()
    with get_db() as db:
        rows = db.execute(
            """
            SELECT sp.*, gs.sku
            FROM shot_plans sp
            JOIN garment_skus gs ON gs.id = sp.sku_id
            WHERE sp.planner_provider = 'gemini'
              AND sp.status NOT IN ('FAILED', 'PLANNING')
              AND (
                sp.category IS NULL OR sp.sales_info IS NULL OR sp.lily_tone IS NULL
                OR sp.styling IS NULL OR sp.background IS NULL
                OR NOT EXISTS (
                  SELECT 1 FROM shot_plan_shots s WHERE s.shot_plan_id = sp.id
                )
              )
            ORDER BY sp.id
            """
        ).fetchall()
        for row in rows:
            raw_plan = loads(row["plan_json"], {})
            if not is_backfillable_shot_plan(raw_plan):
                continue
            plan = normalize_planner_plan(raw_plan, row["sku"])
            generation_plan = plan_generation(plan)
            db.execute(
                """
                UPDATE shot_plans
                SET plan_json = ?, category = ?, sales_info = ?,
                    lily_tone = ?, styling = ?, background = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    dumps(plan),
                    plan.get("category") or plan.get("garment", {}).get("category"),
                    generation_plan.get("salesInfo"),
                    generation_plan.get("lilyTone"),
                    generation_plan.get("styling"),
                    generation_plan.get("background"),
                    row["updated_at"] or timestamp,
                    row["id"],
                ),
            )
            sync_shot_plan_shots(db, row["id"], plan, row["updated_at"] or timestamp)


def is_backfillable_shot_plan(plan: Any) -> bool:
    shots = plan.get("shots") if isinstance(plan, dict) else None
    return isinstance(shots, list) and len(shots) > 0 and all(isinstance(shot, dict) for shot in shots)


def build_image_inputs(sku_row: Any, model_row: Any, model_profile: dict[str, Any], shot: dict[str, Any]) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    with get_db() as db:
        image_rows = db.execute(
            "SELECT * FROM garment_images WHERE sku_id = ? ORDER BY role = 'main' DESC, sort_order, id LIMIT 4",
            (sku_row["id"],),
        ).fetchall()
    for row in image_rows:
        object_key = row["storage_path"]
        inputs.append(
            {
                "role": f"garment_{row['role']}",
                "filename": row["filename"],
                "objectKey": object_key,
                "url": presigned_get_url(object_key),
            }
        )

    head_reference = infer_head_reference_key(str(shot.get("bodyPosition") or "正面"))
    head_references = model_profile.get("headReferences", {}) if isinstance(model_profile.get("headReferences"), dict) else {}
    head_filename = head_references.get(head_reference) or f"{head_reference}.png"
    head_key = join_key(model_row["asset_dir"], str(head_filename))
    if object_exists(head_key):
        inputs.append(
            {
                "role": "model_head",
                "angle": head_reference,
                "objectKey": head_key,
                "url": presigned_get_url(head_key),
            }
        )
    return inputs


MAX_PREVIEW_REFERENCE_IMAGES = 10
MAX_PREVIEW_MODEL_IMAGES = 4
MAX_FINAL_REFERENCE_IMAGES = 10
MAX_FINAL_MODEL_IMAGES = 4


def build_final_image_inputs(
    sku_row: Any,
    model_row: Any,
    model_profile: dict[str, Any],
    preview_inputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    for item in preview_inputs[:1]:
        inputs.append({**item, "role": "preview_grid"})

    remaining = MAX_FINAL_REFERENCE_IMAGES - len(inputs)
    if remaining <= 0:
        return inputs

    model_limit = min(MAX_FINAL_MODEL_IMAGES, remaining)
    model_inputs = build_model_identity_inputs(model_row, model_profile, model_limit)
    inputs.extend(model_inputs)

    remaining = MAX_FINAL_REFERENCE_IMAGES - len(inputs)
    if remaining <= 0:
        return inputs
    inputs.extend(build_garment_reference_inputs(sku_row, remaining))
    return inputs


def build_preview_image_inputs(sku_row: Any, model_row: Any, model_profile: dict[str, Any]) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    inputs.extend(build_model_identity_inputs(model_row, model_profile, MAX_PREVIEW_MODEL_IMAGES))

    garment_limit = max(MAX_PREVIEW_REFERENCE_IMAGES - len(inputs), 0)
    if garment_limit <= 0:
        return inputs
    inputs.extend(build_garment_reference_inputs(sku_row, garment_limit))
    return inputs


def build_model_identity_inputs(model_row: Any, model_profile: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    head_references = model_profile.get("headReferences", {}) if isinstance(model_profile.get("headReferences"), dict) else {}
    ordered_keys = get_ordered_head_reference_keys(model_profile, head_references)
    for key in ordered_keys:
        if len(inputs) >= limit:
            break
        filename = str(head_references.get(key) or "")
        if not filename:
            continue
        object_key = join_key(model_row["asset_dir"], filename)
        if object_exists(object_key):
            inputs.append(
                {
                    "role": "model_identity",
                    "angle": key,
                    "filename": filename,
                    "objectKey": object_key,
                    "url": presigned_get_url(object_key),
                }
            )
    return inputs


def build_garment_reference_inputs(sku_row: Any, limit: int) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    with get_db() as db:
        image_rows = db.execute(
            "SELECT * FROM garment_images WHERE sku_id = ? ORDER BY sort_order, id LIMIT ?",
            (sku_row["id"], limit),
        ).fetchall()
    for row in image_rows:
        object_key = row["storage_path"]
        inputs.append(
            {
                "role": "garment_reference",
                "filename": row["filename"],
                "objectKey": object_key,
                "url": presigned_get_url(object_key),
            }
        )
    return inputs


def get_ordered_head_reference_keys(model_profile: dict[str, Any], head_references: dict[str, Any]) -> list[str]:
    ordered_keys: list[str] = []
    entries = model_profile.get("headReferenceEntries")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                key = str(entry.get("id") or entry.get("angle") or "").strip()
                if key and key in head_references and key not in ordered_keys:
                    ordered_keys.append(key)
    order = model_profile.get("headReferenceOrder")
    if isinstance(order, list):
        for key in order:
            key = str(key).strip()
            if key and key in head_references and key not in ordered_keys:
                ordered_keys.append(key)
    for key in head_references:
        if key not in ordered_keys:
            ordered_keys.append(key)
    return ordered_keys


def infer_head_reference_key(body_position: str) -> str:
    if "背" in body_position:
        return "back"
    if "右" in body_position:
        return "right90"
    if "侧" in body_position or "3/4" in body_position or "斜" in body_position:
        return "left45"
    return "front"


def get_sku_detail_by_id(sku_id: int, image_limit: int = 24, image_offset: int = 0) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gs.*,
              (SELECT COUNT(*) FROM garment_images gi WHERE gi.sku_id = gs.id) AS image_count,
              (SELECT COUNT(*) FROM shot_plans sp WHERE sp.sku_id = gs.id) AS shot_plan_count,
              (SELECT COUNT(*) FROM generation_runs gr WHERE gr.sku_id = gs.id) AS generation_run_count,
              (SELECT COUNT(*) FROM generation_runs gr WHERE gr.sku_id = gs.id AND gr.archived_at IS NOT NULL) AS archived_generation_run_count
            FROM garment_skus gs
            WHERE gs.id = ?
            """,
            (sku_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="货号不存在")
        images = db.execute(
            "SELECT * FROM garment_images WHERE sku_id = ? ORDER BY sort_order, id LIMIT ? OFFSET ?",
            (sku_id, image_limit + 1, image_offset),
        ).fetchall()
        visible_images = images[:image_limit]
        item = serialize_sku(row)
        item["images"] = [serialize_image(image) for image in visible_images]
        item["imagePage"] = {
            "limit": image_limit,
            "offset": image_offset,
            "nextOffset": image_offset + image_limit if len(images) > image_limit else None,
            "hasMore": len(images) > image_limit,
        }
        return item


def sku_delete_impact(db: Any, sku_id: int) -> dict[str, int]:
    row = db.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM garment_images WHERE sku_id = ?) AS image_count,
          (SELECT COUNT(*) FROM shot_plans WHERE sku_id = ?) AS shot_plan_count,
          (SELECT COUNT(*) FROM generation_runs WHERE sku_id = ?) AS generation_run_count,
          (SELECT COUNT(*) FROM generation_runs WHERE sku_id = ? AND archived_at IS NOT NULL) AS archived_generation_run_count,
          (SELECT COUNT(*)
             FROM generation_images gi
             JOIN generation_runs gr ON gr.id = gi.run_id
            WHERE gr.sku_id = ?) AS generation_image_count
        """,
        (sku_id, sku_id, sku_id, sku_id, sku_id),
    ).fetchone()
    return {
        "imageCount": int(row["image_count"] or 0),
        "shotPlanCount": int(row["shot_plan_count"] or 0),
        "generationRunCount": int(row["generation_run_count"] or 0),
        "archivedGenerationRunCount": int(row["archived_generation_run_count"] or 0),
        "generationImageCount": int(row["generation_image_count"] or 0),
    }


def get_model_profile_by_id(model_profile_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (model_profile_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="数字模特不存在")
        return serialize_model(row)


def get_generation_run_by_id(run_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gr.*, gs.sku, gs.title AS sku_title
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
    cover_thumb_path = existing_derivative_or_original(cover_path, "thumb")
    cover_preview_path = existing_derivative_or_original(cover_path, "preview")
    generation_run_count = row["generation_run_count"] if "generation_run_count" in row.keys() else 0
    archived_generation_run_count = row["archived_generation_run_count"] if "archived_generation_run_count" in row.keys() else 0
    shot_plan_count = row["shot_plan_count"] if "shot_plan_count" in row.keys() else 0
    return {
        "id": row["id"],
        "sku": row["sku"],
        "title": row["title"],
        "source": row["source"],
        "status": row["status"],
        "analysis": loads(row["analysis_json"], None),
        "storagePath": row["storage_path"],
        "coverUrl": media_url(cover_thumb_path),
        "coverThumbUrl": media_url(cover_thumb_path),
        "coverPreviewUrl": media_url(cover_preview_path),
        "coverOriginalUrl": media_url(cover_path),
        "coverFileSize": file_size(cover_path),
        "imageCount": row["image_count"] if "image_count" in row.keys() else None,
        "deleteImpact": {
            "generationRunCount": int(generation_run_count or 0),
            "archivedGenerationRunCount": int(archived_generation_run_count or 0),
            "shotPlanCount": int(shot_plan_count or 0),
        },
        "archivedAt": row["archived_at"] if "archived_at" in row.keys() else None,
        "archiveReason": row["archive_reason"] if "archive_reason" in row.keys() else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def serialize_image(row: Any) -> dict[str, Any]:
    original_path = row["storage_path"]
    thumb_path = existing_derivative_or_original(original_path, "thumb")
    preview_path = existing_derivative_or_original(original_path, "preview")
    return {
        "id": row["id"],
        "filename": row["filename"],
        "role": row["role"],
        "sortOrder": row["sort_order"],
        "mimeType": row["mime_type"],
        "url": media_url(preview_path),
        "thumbUrl": media_url(thumb_path),
        "previewUrl": media_url(preview_path),
        "originalUrl": media_url(original_path),
        "fileSize": file_size(original_path),
        "thumbFileSize": file_size(thumb_path) if thumb_path != original_path else None,
        "previewFileSize": file_size(preview_path) if preview_path != original_path else None,
        "createdAt": row["created_at"],
    }


def serialize_model(row: Any) -> dict[str, Any]:
    profile = loads(row["profile_json"], {})
    raw_references = profile.get("headReferences", {}) or {}
    references = ordered_head_references(profile, raw_references)
    assigned_references = {key: value for key, value in references.items() if not is_unassigned_head_reference_id(key)}
    head = assigned_references.get("front")
    preview_path = join_key(row["asset_dir"], head) if head else None
    reference_urls = {
        angle: model_asset_url(row["id"], row["asset_dir"], filename, "preview")
        for angle, filename in references.items()
        if filename
    }
    reference_thumb_urls = {
        angle: model_asset_url(row["id"], row["asset_dir"], filename, "thumb")
        for angle, filename in references.items()
        if filename
    }
    reference_original_urls = {
        angle: model_asset_url(row["id"], row["asset_dir"], filename, "original")
        for angle, filename in references.items()
        if filename
    }
    reference_meta = {
        angle: {"fileSize": file_size(join_key(row["asset_dir"], str(filename)))}
        for angle, filename in references.items()
        if filename
    }
    reference_entries = [
        {
            "id": reference_id,
            "angle": None if is_unassigned_head_reference_id(reference_id) else reference_id,
            "filename": str(filename),
            "url": reference_urls.get(reference_id),
            "thumbUrl": reference_thumb_urls.get(reference_id),
            "previewUrl": reference_urls.get(reference_id),
            "originalUrl": reference_original_urls.get(reference_id),
            "fileSize": reference_meta.get(reference_id, {}).get("fileSize"),
        }
        for reference_id, filename in references.items()
    ]
    return {
        "id": row["id"],
        "modelId": row["model_id"],
        "name": row["name"],
        "assetDir": row["asset_dir"],
        "profile": {
            **profile,
            "headReferences": assigned_references,
            "headReferenceOrder": list(references.keys()),
            "headReferenceUrls": reference_urls,
            "headReferenceMeta": reference_meta,
            "headReferenceEntries": reference_entries,
        },
        "isDefault": bool(row["is_default"]),
        "previewPath": str(preview_path) if preview_path else None,
        "thumbUrl": model_asset_url(row["id"], row["asset_dir"], head, "thumb") if head else None,
        "previewUrl": model_asset_url(row["id"], row["asset_dir"], head, "preview") if head else None,
        "originalUrl": model_asset_url(row["id"], row["asset_dir"], head, "original") if head else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def serialize_shot_plan(row: Any) -> dict[str, Any]:
    plan = loads(row["plan_json"], {})
    with get_db() as db:
        shot_rows = db.execute(
            "SELECT * FROM shot_plan_shots WHERE shot_plan_id = ? ORDER BY shot_index",
            (row["id"],),
        ).fetchall()
    if shot_rows:
        plan["shots"] = [loads(shot["shot_json"], {}) for shot in shot_rows]
    plan.setdefault("category", row["category"] if "category" in row.keys() else plan.get("garment", {}).get("category"))
    generation_plan = plan.get("generationPlan") if isinstance(plan.get("generationPlan"), dict) else {}
    plan["generationPlan"] = {
        "salesInfo": generation_plan.get("salesInfo") or (row["sales_info"] if "sales_info" in row.keys() else None),
        "lilyTone": generation_plan.get("lilyTone") or (row["lily_tone"] if "lily_tone" in row.keys() else None),
        "styling": generation_plan.get("styling") or (row["styling"] if "styling" in row.keys() else None),
        "background": generation_plan.get("background") or (row["background"] if "background" in row.keys() else None),
    }
    return {
        "id": row["id"],
        "skuId": row["sku_id"],
        "modelProfileId": row["model_profile_id"],
        "status": row["status"],
        "plan": plan,
        "plannerProvider": row["planner_provider"],
        "rawResponse": row["raw_response"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def serialize_generation_run(row: Any, include_images: bool, include_summary: bool = False) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "skuId": row["sku_id"],
        "sku": row["sku"] if "sku" in row.keys() else None,
        "skuTitle": row["sku_title"] if "sku_title" in row.keys() else None,
        "shotPlanId": row["shot_plan_id"],
        "modelProfileId": row["model_profile_id"],
        "kind": row["kind"],
        "status": row["status"],
        "outputDir": row["output_dir"],
        "deliveryStatus": row["delivery_status"] if "delivery_status" in row.keys() else "draft",
        "deliveredAt": row["delivered_at"] if "delivered_at" in row.keys() else None,
        "archivedAt": row["archived_at"] if "archived_at" in row.keys() else None,
        "archiveReason": row["archive_reason"] if "archive_reason" in row.keys() else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_summary:
        with get_db() as db:
            preview_rows = db.execute(
                """
                SELECT id, shot_index, title, output_path, status, is_final, current_attempt_index
                FROM generation_images
                WHERE run_id = ? AND status = 'SUCCEEDED' AND output_path IS NOT NULL
                ORDER BY is_final DESC, shot_index, id
                LIMIT 10
                """,
                (row["id"],),
            ).fetchall()
            previews = [preview for preview in preview_rows if has_displayable_generation_output(preview)][:5]
            item["summary"] = {
                "imageCount": row["image_count"] if "image_count" in row.keys() else 0,
                "succeededCount": row["succeeded_count"] if "succeeded_count" in row.keys() else 0,
                "finalCount": row["final_count"] if "final_count" in row.keys() else 0,
                "regenerationCount": row["regeneration_count"] if "regeneration_count" in row.keys() else 0,
                "previews": [
                    {
                        "id": preview["id"],
                        "shotIndex": preview["shot_index"],
                        "title": preview["title"],
                        "url": media_url(existing_derivative_or_original(preview["output_path"], "thumb")),
                        "thumbUrl": media_url(existing_derivative_or_original(preview["output_path"], "thumb")),
                        "previewUrl": media_url(existing_derivative_or_original(preview["output_path"], "preview")),
                        "originalUrl": media_url(preview["output_path"]),
                        "status": preview["status"],
                        "isFinal": bool(preview["is_final"]),
                        "attemptCount": preview["current_attempt_index"] or 1,
                    }
                    for preview in previews
                ],
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
    displayable = has_displayable_generation_output(row)
    original_path = row["output_path"] if displayable else None
    thumb_path = existing_derivative_or_original(original_path, "thumb") if original_path else None
    preview_path = existing_derivative_or_original(original_path, "preview") if original_path else None
    return {
        "id": row["id"],
        "runId": row["run_id"],
        "shotIndex": row["shot_index"],
        "title": row["title"],
        "status": row["status"],
        "objectKey": row["output_path"],
        "url": media_url(preview_path) if displayable else None,
        "thumbUrl": media_url(thumb_path) if displayable else None,
        "previewUrl": media_url(preview_path) if displayable else None,
        "originalUrl": media_url(original_path) if displayable else None,
        "fileSize": file_size(original_path) if displayable else None,
        "thumbFileSize": file_size(thumb_path) if displayable and thumb_path != original_path else None,
        "previewFileSize": file_size(preview_path) if displayable and preview_path != original_path else None,
        "taskPackage": loads(row["task_package_json"], {}),
        "providerRequest": loads(row["provider_request_json"], None),
        "providerResponse": loads(row["provider_response_json"], None),
        "prompt": row["prompt_text"] if "prompt_text" in row.keys() else None,
        "imageInputs": loads(row["image_inputs_json"], []) if "image_inputs_json" in row.keys() else [],
        "templateSnapshot": loads(row["template_snapshot_json"], None) if "template_snapshot_json" in row.keys() else None,
        "modelSnapshot": loads(row["model_snapshot_json"], None) if "model_snapshot_json" in row.keys() else None,
        "garmentSnapshot": loads(row["garment_snapshot_json"], None) if "garment_snapshot_json" in row.keys() else None,
        "attempts": loads(row["attempts_json"], []) if "attempts_json" in row.keys() else [],
        "currentAttemptIndex": row["current_attempt_index"] if "current_attempt_index" in row.keys() else 0,
        "providerRequestId": row["provider_request_id"] if "provider_request_id" in row.keys() else None,
        "providerStatus": row["provider_status"] if "provider_status" in row.keys() else None,
        "providerUrls": loads(row["provider_urls_json"], {}) if "provider_urls_json" in row.keys() else {},
        "providerStatusResponse": loads(row["provider_status_json"], None) if "provider_status_json" in row.keys() else None,
        "submittedAt": row["submitted_at"] if "submitted_at" in row.keys() else None,
        "completedAt": row["completed_at"] if "completed_at" in row.keys() else None,
        "lastStatusCheckedAt": row["last_status_checked_at"] if "last_status_checked_at" in row.keys() else None,
        "lockedBy": row["locked_by"] if "locked_by" in row.keys() else None,
        "lockedUntil": row["locked_until"] if "locked_until" in row.keys() else None,
        "submitAttempts": row["submit_attempts"] if "submit_attempts" in row.keys() else 0,
        "statusCheckAttempts": row["status_check_attempts"] if "status_check_attempts" in row.keys() else 0,
        "statusErrorAttempts": row["status_error_attempts"] if "status_error_attempts" in row.keys() else 0,
        "cancelRequestedAt": row["cancel_requested_at"] if "cancel_requested_at" in row.keys() else None,
        "timeoutAt": row["timeout_at"] if "timeout_at" in row.keys() else None,
        "regenerateFromImageId": row["regenerate_from_image_id"],
        "regenerateReason": row["regenerate_reason"],
        "isFinal": bool(row["is_final"]),
        "reviewStatus": row["review_status"] if "review_status" in row.keys() else "candidate",
        "reviewNote": row["review_note"] if "review_note" in row.keys() else None,
        "reviewedAt": row["reviewed_at"] if "reviewed_at" in row.keys() else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def has_displayable_generation_output(row: Any) -> bool:
    output_path = row["output_path"] if "output_path" in row.keys() else None
    return row["status"] == "SUCCEEDED" and bool(output_path) and object_exists(output_path)


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


def sanitize_reference_key(value: str) -> str:
    return "".join(ch for ch in value.strip() if ch.isalnum() or ch in {"-", "_"}).strip("-_")


def sanitize_reference_id(value: str) -> str:
    return "".join(ch for ch in value.strip() if ch.isalnum() or ch in {"-", "_"})


def normalize_model_profile(profile: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(profile)
    normalized["headReferences"] = dict(normalized.get("headReferences") or {})
    normalized["headReferenceOrder"] = normalize_head_reference_order(normalized, normalized["headReferences"])
    forbidden = normalized.get("forbidden")
    if isinstance(forbidden, str):
        normalized["forbidden"] = [item.strip() for item in forbidden.split("\n") if item.strip()]
    elif not isinstance(forbidden, list):
        normalized["forbidden"] = []
    return normalized


def normalize_head_reference_order(profile: dict[str, Any], references: dict[str, Any]) -> list[str]:
    existing_order = profile.get("headReferenceOrder")
    ordered: list[str] = []
    if isinstance(existing_order, list):
        ordered.extend(str(item) for item in existing_order if str(item) in references)
    for angle in references:
        if angle not in ordered:
            ordered.append(angle)
    return ordered


def ordered_head_references(profile: dict[str, Any], references: dict[str, Any]) -> dict[str, Any]:
    order = normalize_head_reference_order(profile, references)
    return {angle: references[angle] for angle in order if angle in references}


def is_unassigned_head_reference_id(reference_id: str) -> bool:
    return reference_id.startswith(UNASSIGNED_HEAD_REFERENCE_PREFIX)


def new_unassigned_head_reference_key(references: dict[str, Any]) -> str:
    while True:
        candidate = f"{UNASSIGNED_HEAD_REFERENCE_PREFIX}{uuid4().hex[:10]}"
        if candidate not in references:
            return candidate


def model_asset_url(model_profile_id: int, asset_dir: str, filename: str | None, variant: str = "preview") -> str | None:
    if not filename:
        return None
    key = join_key(asset_dir, sanitize_filename(filename))
    if variant in {"thumb", "preview"}:
        key = existing_derivative_or_original(key, variant) or key
    if not object_exists(key):
        return None
    return presigned_get_url(key)


def file_size(path: str | Path | None) -> int | None:
    return object_size(path)


def remove_model_asset_file(asset_dir: str, filename: str | None) -> None:
    if not filename:
        return
    cleanup_objects(image_object_keys(join_key(asset_dir, sanitize_filename(filename))))
