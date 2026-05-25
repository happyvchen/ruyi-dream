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
from urllib.parse import urlencode

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

from .config import get_settings
from .db import dumps, get_db, init_db, loads, log_event, now_iso
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
    presigned_get_url,
    put_bytes,
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
    username: str | None = None
    password: str


class StylingGarmentUpdateRequest(BaseModel):
    code: str | None = None
    manualTags: dict[str, Any] | None = None


class StylingGarmentImageUpdateRequest(BaseModel):
    sortOrder: int | None = None


class GenerationTaskUpdateRequest(BaseModel):
    name: str | None = None


class GenerationTaskImageUpdateRequest(BaseModel):
    sortOrder: int | None = None


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
LILY_TAG_SCHEMA_VERSION = "lily-garment-tags-v1"
TAG_REVIEW_CONFIDENCE_THRESHOLD = 0.72
TAG_CATEGORIES = {"top", "bottom", "dress", "jumpsuit", "outerwear", "knitwear", "suit_set", "other_garment", "unsupported_accessory", "unknown"}
TAG_SUB_CATEGORIES = {"shirt", "blouse", "t_shirt", "vest", "camisole", "cardigan", "blazer", "jacket", "trench", "coat", "down_jacket", "trousers", "jeans", "shorts", "skirt", "dress", "other"}
TAG_COLORS = {"black", "white", "ivory", "beige", "grey", "blue", "navy", "brown", "green", "red", "pink", "yellow", "purple", "metallic", "multi", "unknown"}
TAG_COLOR_TEMPERATURES = {"warm", "cool", "neutral", "mixed", "unknown"}
TAG_CONTRAST_LEVELS = {"low", "medium", "high"}
TAG_COLOR_DISTRIBUTIONS = {"solid", "color_block", "trim", "gradient", "print_based"}
TAG_MATERIALS_VISUAL = {"cotton_like", "linen_like", "silk_like", "satin_like", "acetate_like", "knit", "wool_like", "denim", "leather_like", "lace", "chiffon", "tweed_like", "down_padded", "technical_fabric", "unknown"}
TAG_SURFACE_TEXTURES = {"smooth", "ribbed", "openwork", "slub", "woven_texture", "sheer", "glossy", "matte", "structured", "soft_drape"}
TAG_FITS = {"slim", "regular", "relaxed", "loose", "oversized", "unknown"}
TAG_SILHOUETTES = {"straight", "a_line", "h_line", "x_line", "wide_leg", "flare", "tapered", "cropped", "waist_defined", "boxy"}
TAG_STRUCTURES = {"tailored", "soft", "draped", "layered", "structured_shoulder", "dropped_shoulder", "pleated", "gathered"}
TAG_TOP_LENGTHS = {"cropped", "hip", "long", "unknown"}
TAG_SLEEVE_LENGTHS = {"sleeveless", "short", "elbow", "three_quarter", "long", "unknown"}
TAG_BOTTOM_LENGTHS = {"short", "knee", "midi", "ankle", "full", "unknown"}
TAG_NECKLINES = {"crew", "v_neck", "polo", "shirt_collar", "lapel", "boat", "square", "halter", "cowl", "unknown"}
TAG_CLOSURES = {"button", "zipper", "pullover", "wrap", "tie", "open_front", "unknown"}
TAG_DETAILS = {"asymmetric", "lace", "contrast_trim", "embroidery", "pocket", "pleat", "cutout", "patchwork", "print", "button", "ruffle", "bow_tie", "belt", "slit"}
TAG_PATTERNS = {"solid", "stripe", "check", "polka_dot", "floral", "graphic", "abstract", "texture_pattern", "logo", "animal", "unknown"}
TAG_PATTERN_SCALES = {"none", "small", "medium", "large", "mixed", "unknown"}
TAG_WEARING_METHODS = {"buttoned", "open", "tucked", "untucked", "belted", "layered"}
TAG_COVERAGE_RISKS = {"none", "may_cover_hero", "likely_cover_hero", "unknown"}
TAG_FORMALITIES = {"casual", "smart_casual", "office", "formal", "unknown"}
TAG_OCCASIONS = {"daily", "commute", "office", "light_social", "date", "travel", "home"}
TAG_SEASONS = {"spring", "summer", "autumn", "winter"}
TAG_PAIRING_CATEGORIES = {"top", "bottom", "outerwear", "dress", "knitwear"}
TAG_SILHOUETTE_PAIRINGS = {"slim_top", "relaxed_top", "wide_leg_bottom", "straight_bottom", "a_line_skirt"}
TAG_STYLE_RISKS = {"too_formal", "too_casual", "too_sweet", "too_street", "too_party", "too_heavy", "too_busy", "none"}
TAG_REVIEW_REASONS = {"low_confidence", "conflicting_images", "unclear_category", "title_image_conflict", "insufficient_images", "unsupported_accessory", "uncertain_material", "uncertain_color", "uncertain_fit", "fallback_tags"}
LOGIN_FAILURE_WINDOW_SECONDS = 300
LOGIN_FAILURE_LIMIT = 8
login_failures_by_client: dict[str, list[float]] = {}
ALLOWED_UPLOAD_FORMATS = {"JPEG", "PNG", "WEBP"}

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
    if payload.username is not None and payload.username != "user":
        record_login_failure(request)
        return JSONResponse(status_code=401, content={"detail": "用户名或密码不正确"})
    if not is_gate_password_valid(payload.password):
        record_login_failure(request)
        return JSONResponse(status_code=401, content={"detail": "用户名或密码不正确"})
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
    if width <= 0 or height <= 0 or width * height > settings.max_upload_image_pixels:
        max_mp = settings.max_upload_image_pixels / 1_000_000
        raise HTTPException(status_code=400, detail=f"{filename} 超过 {max_mp:.0f}MP 像素限制")

    return content, detected_image_content_type(image_format, upload.content_type)


def detected_image_content_type(image_format: str, declared_type: str | None) -> str:
    canonical_type = Image.MIME.get(image_format.upper())
    if canonical_type:
        return canonical_type
    return f"image/{image_format.lower() or 'unknown'}"


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

    saved_files: list[tuple[str, str, str, dict[str, Any]]] = []
    try:
        for index, (upload, content, content_type) in enumerate(validated_files):
            safe_name = f"{index + 1:03d}-{sanitize_filename(upload.filename or 'image')}"
            key = join_key(target_prefix, safe_name)
            put_fileobj(key, BytesIO(content), content_type)
            derivative = generate_image_derivatives(key, content)
            saved_files.append((safe_name, key, content_type, upload_media_metadata(key, content, derivative)))
    except Exception:
        cleanup_objects([key for _, original_key, _, _ in saved_files for key in image_object_keys(original_key)])
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
            for index, (filename, key, content_type, media) in enumerate(saved_files):
                db.execute(
                    """
                    INSERT INTO garment_images
                      (sku_id, filename, storage_path, thumb_path, preview_path, file_size,
                       thumb_file_size, preview_file_size, mime_type, role, sort_order, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sku_id,
                        filename,
                        key,
                        media.get("thumbPath"),
                        media.get("previewPath"),
                        media.get("fileSize"),
                        media.get("thumbFileSize"),
                        media.get("previewFileSize"),
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
        cleanup_objects([key for _, original_key, _, _ in saved_files for key in image_object_keys(original_key)])
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
    saved: list[tuple[str, str, str, int, dict[str, Any]]] = []

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
                derivative = generate_image_derivatives(key, content)
                saved.append((Path(key).name, key, content_type, sort_order, upload_media_metadata(key, content, derivative)))
        except Exception:
            cleanup_objects([key for _, original_key, _, _, _ in saved for key in image_object_keys(original_key)])
            raise

        try:
            for filename, key, content_type, sort_order, media in saved:
                db.execute(
                    """
                    INSERT INTO garment_images
                      (sku_id, filename, storage_path, thumb_path, preview_path, file_size,
                       thumb_file_size, preview_file_size, mime_type, role, sort_order, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reference', ?, ?)
                    """,
                    (
                        sku_id,
                        filename,
                        key,
                        media.get("thumbPath"),
                        media.get("previewPath"),
                        media.get("fileSize"),
                        media.get("thumbFileSize"),
                        media.get("previewFileSize"),
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
                {"fileCount": len(saved), "files": [filename for filename, _, _, _, _ in saved]},
            )
        except Exception:
            cleanup_objects([key for _, original_key, _, _, _ in saved for key in image_object_keys(original_key)])
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


@app.get("/api/styling-garments")
def list_styling_garments(
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    keyword: str | None = Query(None),
) -> dict[str, Any]:
    search = f"%{keyword.strip()}%" if keyword and keyword.strip() else None
    with get_db() as db:
        rows = db.execute(
            """
            SELECT sg.*,
              (SELECT ma.object_key FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_key,
              (SELECT COUNT(*) FROM styling_garment_images sgi WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL) AS image_count
            FROM styling_garments sg
            WHERE sg.deleted_at IS NULL
              AND (? IS NULL OR sg.code LIKE ? OR sg.status LIKE ? OR sg.ai_tags_json LIKE ?)
            ORDER BY sg.updated_at DESC, sg.id DESC
            LIMIT ? OFFSET ?
            """,
            (search, search, search, search, limit + 1, offset),
        ).fetchall()
        items = rows[:limit]
        return {
            "items": [serialize_styling_garment(row, include_images=False) for row in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if len(rows) > limit else None,
            "hasMore": len(rows) > limit,
        }


@app.get("/api/styling-garments/{garment_id}")
def get_styling_garment(garment_id: int) -> dict[str, Any]:
    return {"item": get_styling_garment_by_id(garment_id)}


def ensure_styling_garment_not_tagging(row: Any) -> None:
    if row["status"] == "TAGGING_RUNNING":
        raise HTTPException(status_code=409, detail="正在打标，完成后才能修改或归档搭配服饰")


@app.post("/api/styling-garments")
async def create_styling_garment(
    code: str = Form(...),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    clean_code = sanitize_name(code)
    if not clean_code:
        raise HTTPException(status_code=400, detail="搭配服饰代码不能为空")
    if not files:
        raise HTTPException(status_code=400, detail="至少上传一张图片")
    if len(files) > 6:
        raise HTTPException(status_code=400, detail="单套搭配服饰最多上传 6 张图片")
    timestamp = now_iso()
    prefix = join_key("styling-garments", f"{clean_code}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}")
    assets = await save_uploads_as_media_assets(files, prefix)
    with get_db() as db:
        duplicate = db.execute("SELECT id FROM styling_garments WHERE code = ? AND deleted_at IS NULL", (clean_code,)).fetchone()
        if duplicate:
            cleanup_objects([key for asset in assets for key in image_object_keys(asset["objectKey"])])
            raise HTTPException(status_code=409, detail="搭配服饰代码已存在")
        cursor = db.execute(
            """
            INSERT INTO styling_garments (code, status, manual_tags_json, ai_tags_json, tag_schema_version, created_at, updated_at)
            VALUES (?, 'DRAFT', ?, ?, ?, ?, ?)
            """,
            (clean_code, dumps({}), dumps({}), LILY_TAG_SCHEMA_VERSION, timestamp, timestamp),
        )
        garment_id = int(cursor.lastrowid)
        for index, asset in enumerate(assets):
            db.execute(
                """
                INSERT INTO styling_garment_images
                  (styling_garment_id, media_asset_id, sort_order, status, created_at, updated_at)
                VALUES (?, ?, ?, 'READY', ?, ?)
                """,
                (garment_id, asset["id"], index, timestamp, timestamp),
            )
        log_event(db, "styling_garment", garment_id, "created", {"code": clean_code, "fileCount": len(assets)})
    return {"item": get_styling_garment_by_id(garment_id)}


@app.patch("/api/styling-garments/{garment_id}")
def update_styling_garment(garment_id: int, payload: StylingGarmentUpdateRequest) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if payload.code is not None:
        clean_code = sanitize_name(payload.code)
        if not clean_code:
            raise HTTPException(status_code=400, detail="搭配服饰代码不能为空")
        updates["code"] = clean_code
    if payload.manualTags is not None:
        updates["manual_tags_json"] = dumps(normalize_manual_business_tags(payload.manualTags))
    if not updates:
        return {"item": get_styling_garment_by_id(garment_id)}
    with get_db() as db:
        row = db.execute("SELECT * FROM styling_garments WHERE id = ? AND deleted_at IS NULL", (garment_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="搭配服饰不存在")
        ensure_styling_garment_not_tagging(row)
        if "code" in updates:
            duplicate = db.execute(
                "SELECT id FROM styling_garments WHERE code = ? AND id <> ? AND deleted_at IS NULL",
                (updates["code"], garment_id),
            ).fetchone()
            if duplicate:
                raise HTTPException(status_code=409, detail="搭配服饰代码已存在")
        manual_tags = loads(updates.get("manual_tags_json") if "manual_tags_json" in updates else row["manual_tags_json"], {})
        effective_tags = build_effective_tags(
            loads(row["ai_tags_json"], {}),
            loads(row["official_title_tags_json"], {}),
            manual_tags,
        )
        updates["effective_tags_json"] = dumps(effective_tags)
        updates["updated_at"] = now_iso()
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        db.execute(f"UPDATE styling_garments SET {set_clause} WHERE id = ?", (*updates.values(), garment_id))
    return {"item": get_styling_garment_by_id(garment_id)}


@app.post("/api/styling-garments/{garment_id}/save-and-tag")
async def save_and_tag_styling_garment(garment_id: int, background_tasks: BackgroundTasks) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM styling_garments WHERE id = ? AND deleted_at IS NULL", (garment_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="搭配服饰不存在")
        ensure_styling_garment_not_tagging(row)
        timestamp = now_iso()
        db.execute("UPDATE styling_garments SET status = 'TAGGING_RUNNING', tag_error = NULL, updated_at = ? WHERE id = ?", (timestamp, garment_id))
    background_tasks.add_task(run_styling_garment_tagging, garment_id)
    return {"item": get_styling_garment_by_id(garment_id)}


async def run_styling_garment_tagging(garment_id: int) -> None:
    try:
        tagging_result = await tag_styling_garment_with_gemini(garment_id)
    except Exception as exc:
        with get_db() as db:
            db.execute(
                "UPDATE styling_garments SET status = 'TAGGING_FAILED', tag_error = ?, updated_at = ? WHERE id = ?",
                (str(exc), now_iso(), garment_id),
            )
        return
    with get_db() as db:
        timestamp = now_iso()
        row = db.execute("SELECT manual_tags_json FROM styling_garments WHERE id = ?", (garment_id,)).fetchone()
        manual_tags = loads(row["manual_tags_json"], {}) if row else {}
        ai_tags = tagging_result.get("ai_tags", {})
        official_title_tags = tagging_result.get("official_title_tags", {})
        tag_meta = apply_tag_review_policy(ai_tags, tagging_result.get("tag_meta", {}))
        effective_tags = build_effective_tags(ai_tags, official_title_tags, manual_tags)
        next_status = tagging_status_for_result(ai_tags, tag_meta)
        if next_status == "TAGGING_READY":
            tag_meta = {**tag_meta, "needs_review": True}
        db.execute(
            """
            UPDATE styling_garments
            SET status = ?,
                ai_tags_json = ?,
                official_title_tags_json = ?,
                effective_tags_json = ?,
                tag_meta_json = ?,
                tag_schema_version = ?,
                tag_completed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                dumps(ai_tags),
                dumps(official_title_tags),
                dumps(effective_tags),
                dumps(tag_meta),
                tagging_result.get("schema_version", LILY_TAG_SCHEMA_VERSION),
                timestamp,
                timestamp,
                garment_id,
            ),
        )
        log_event(db, "styling_garment", garment_id, "tagged", tagging_result)


@app.post("/api/styling-garments/{garment_id}/images")
async def add_styling_garment_images(
    garment_id: int,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="至少上传一张图片")
    with get_db() as db:
        row = db.execute("SELECT * FROM styling_garments WHERE id = ? AND deleted_at IS NULL", (garment_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="搭配服饰不存在")
        ensure_styling_garment_not_tagging(row)
        existing_count = db.execute(
            "SELECT COUNT(*) AS count FROM styling_garment_images WHERE styling_garment_id = ? AND deleted_at IS NULL",
            (garment_id,),
        ).fetchone()["count"]
        if int(existing_count) + len(files) > 6:
            raise HTTPException(status_code=400, detail="单套搭配服饰最多保存 6 张图片")
        max_sort = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS max_sort FROM styling_garment_images WHERE styling_garment_id = ? AND deleted_at IS NULL",
            (garment_id,),
        ).fetchone()["max_sort"]
    prefix = join_key("styling-garments", f"{sanitize_name(row['code'])}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}")
    assets = await save_uploads_as_media_assets(files, prefix)
    timestamp = now_iso()
    with get_db() as db:
        current = db.execute("SELECT * FROM styling_garments WHERE id = ? AND deleted_at IS NULL", (garment_id,)).fetchone()
        if not current:
            cleanup_objects([key for asset in assets for key in image_object_keys(asset["objectKey"])])
            raise HTTPException(status_code=404, detail="搭配服饰不存在")
        try:
            ensure_styling_garment_not_tagging(current)
        except HTTPException:
            cleanup_objects([key for asset in assets for key in image_object_keys(asset["objectKey"])])
            raise
        for offset, asset in enumerate(assets, start=1):
            db.execute(
                """
                INSERT INTO styling_garment_images
                  (styling_garment_id, media_asset_id, sort_order, status, created_at, updated_at)
                VALUES (?, ?, ?, 'READY', ?, ?)
                """,
                (garment_id, asset["id"], int(max_sort) + offset, timestamp, timestamp),
            )
        db.execute("UPDATE styling_garments SET updated_at = ? WHERE id = ?", (timestamp, garment_id))
        log_event(db, "styling_garment", garment_id, "images_added", {"fileCount": len(assets)})
    return {"item": get_styling_garment_by_id(garment_id)}


@app.post("/api/styling-garment-images/{image_id}/replace")
async def replace_styling_garment_image(image_id: int, file: UploadFile = File(...)) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT sgi.*, sg.code, sg.status AS garment_status, sg.deleted_at AS garment_deleted_at,
                   ma.object_key AS previous_object_key
            FROM styling_garment_images sgi
            JOIN styling_garments sg ON sg.id = sgi.styling_garment_id
            JOIN media_assets ma ON ma.id = sgi.media_asset_id
            WHERE sgi.id = ? AND sgi.deleted_at IS NULL
            """,
            (image_id,),
        ).fetchone()
    if not row or row["garment_deleted_at"] is not None:
        raise HTTPException(status_code=404, detail="搭配服饰图片不存在")
    ensure_styling_garment_not_tagging({"status": row["garment_status"]})
    prefix = join_key("styling-garments", f"{sanitize_name(row['code'])}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}")
    assets = await save_uploads_as_media_assets([file], prefix)
    replacement = assets[0]
    garment_id = int(row["styling_garment_id"])
    timestamp = now_iso()
    with get_db() as db:
        current = db.execute("SELECT * FROM styling_garments WHERE id = ? AND deleted_at IS NULL", (garment_id,)).fetchone()
        if not current:
            cleanup_objects(image_object_keys(replacement["objectKey"]))
            raise HTTPException(status_code=404, detail="搭配服饰不存在")
        try:
            ensure_styling_garment_not_tagging(current)
        except HTTPException:
            cleanup_objects(image_object_keys(replacement["objectKey"]))
            raise
        db.execute(
            "UPDATE styling_garment_images SET media_asset_id = ?, updated_at = ? WHERE id = ?",
            (replacement["id"], timestamp, image_id),
        )
        db.execute("UPDATE styling_garments SET updated_at = ? WHERE id = ?", (timestamp, garment_id))
        log_event(db, "styling_garment", garment_id, "image_replaced", {"imageId": image_id})
    return {"item": get_styling_garment_by_id(garment_id)}


@app.patch("/api/styling-garment-images/{image_id}")
def update_styling_garment_image(image_id: int, payload: StylingGarmentImageUpdateRequest) -> dict[str, Any]:
    if payload.sortOrder is None:
        with get_db() as db:
            row = db.execute(
                "SELECT sgi.styling_garment_id, sg.status FROM styling_garment_images sgi JOIN styling_garments sg ON sg.id = sgi.styling_garment_id WHERE sgi.id = ? AND sgi.deleted_at IS NULL AND sg.deleted_at IS NULL",
                (image_id,),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="搭配服饰图片不存在")
        ensure_styling_garment_not_tagging(row)
        return {"item": get_styling_garment_by_id(int(row["styling_garment_id"]))}
    if payload.sortOrder < 0:
        raise HTTPException(status_code=400, detail="图片排序不能小于 0")
    timestamp = now_iso()
    with get_db() as db:
        row = db.execute(
            "SELECT sgi.styling_garment_id, sg.status FROM styling_garment_images sgi JOIN styling_garments sg ON sg.id = sgi.styling_garment_id WHERE sgi.id = ? AND sgi.deleted_at IS NULL AND sg.deleted_at IS NULL",
            (image_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="搭配服饰图片不存在")
        ensure_styling_garment_not_tagging(row)
        garment_id = int(row["styling_garment_id"])
        db.execute(
            "UPDATE styling_garment_images SET sort_order = ?, updated_at = ? WHERE id = ?",
            (payload.sortOrder, timestamp, image_id),
        )
        db.execute("UPDATE styling_garments SET updated_at = ? WHERE id = ?", (timestamp, garment_id))
    return {"item": get_styling_garment_by_id(garment_id)}


@app.delete("/api/styling-garment-images/{image_id}")
def delete_styling_garment_image(image_id: int) -> dict[str, Any]:
    timestamp = now_iso()
    with get_db() as db:
        row = db.execute(
            "SELECT sgi.*, sg.status AS garment_status FROM styling_garment_images sgi JOIN styling_garments sg ON sg.id = sgi.styling_garment_id WHERE sgi.id = ? AND sgi.deleted_at IS NULL AND sg.deleted_at IS NULL",
            (image_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="搭配服饰图片不存在")
        ensure_styling_garment_not_tagging({"status": row["garment_status"]})
        garment_id = int(row["styling_garment_id"])
        remaining = db.execute(
            "SELECT COUNT(*) AS count FROM styling_garment_images WHERE styling_garment_id = ? AND deleted_at IS NULL AND id <> ?",
            (garment_id, image_id),
        ).fetchone()["count"]
        if int(remaining) < 1:
            raise HTTPException(status_code=409, detail="每套搭配服饰至少保留一张图片")
        db.execute("UPDATE styling_garment_images SET deleted_at = ?, updated_at = ? WHERE id = ?", (timestamp, timestamp, image_id))
        db.execute("UPDATE styling_garments SET updated_at = ? WHERE id = ?", (timestamp, garment_id))
        log_event(db, "styling_garment", garment_id, "image_deleted", {"imageId": image_id})
    return {"item": get_styling_garment_by_id(garment_id)}


@app.delete("/api/styling-garments/{garment_id}")
def delete_styling_garment(
    garment_id: int,
    purge: bool = Query(False),
    confirm_code: str | None = Query(None, alias="confirmCode"),
) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM styling_garments WHERE id = ? AND deleted_at IS NULL", (garment_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="搭配服饰不存在")
        ensure_styling_garment_not_tagging(row)
        if purge and confirm_code != row["code"]:
            raise HTTPException(status_code=400, detail="请输入完整 code 以确认删除关联记录和文件")
        timestamp = now_iso()
        if purge:
            image_rows = db.execute(
                """
                SELECT ma.id, ma.object_key FROM styling_garment_images sgi
                JOIN media_assets ma ON ma.id = sgi.media_asset_id
                WHERE sgi.styling_garment_id = ?
                """,
                (garment_id,),
            ).fetchall()
            styling_rows = db.execute(
                "SELECT task_id, selected_material_ids_json FROM generation_stylings"
            ).fetchall()
            dependent_task_ids = [
                int(styling["task_id"])
                for styling in styling_rows
                if garment_id in {int(item) for item in loads(styling["selected_material_ids_json"], [])}
            ]
            dependent_asset_ids: list[int] = []
            if dependent_task_ids:
                placeholders = ", ".join("?" for _ in dependent_task_ids)
                dependent_assets = db.execute(
                    f"""
                    SELECT media_asset_id AS asset_id FROM generation_task_images WHERE task_id IN ({placeholders})
                    UNION
                    SELECT preview_asset_id AS asset_id FROM generation_previews WHERE task_id IN ({placeholders}) AND preview_asset_id IS NOT NULL
                    UNION
                    SELECT gri.current_asset_id AS asset_id FROM generation_result_images_new gri
                    JOIN generation_result_sets grs ON grs.id = gri.result_set_id
                    WHERE grs.task_id IN ({placeholders}) AND gri.current_asset_id IS NOT NULL
                    UNION
                    SELECT ga.output_asset_id AS asset_id FROM generation_attempts ga
                    WHERE ga.output_asset_id IS NOT NULL AND (
                         (ga.owner_type = 'generation_styling' AND ga.owner_id IN ({placeholders}))
                         OR (ga.owner_type = 'generation_preview' AND ga.owner_id IN (
                              SELECT id FROM generation_previews WHERE task_id IN ({placeholders})
                            ))
                         OR (ga.owner_type = 'generation_result_image' AND ga.owner_id IN (
                              SELECT gri.id FROM generation_result_images_new gri
                              JOIN generation_result_sets grs ON grs.id = gri.result_set_id
                              WHERE grs.task_id IN ({placeholders})
                            ))
                    )
                    """,
                    (
                        *dependent_task_ids, *dependent_task_ids, *dependent_task_ids,
                        *dependent_task_ids, *dependent_task_ids, *dependent_task_ids,
                    ),
                ).fetchall()
                dependent_asset_ids = [int(asset["asset_id"]) for asset in dependent_assets]
                db.execute(
                    f"""
                    DELETE FROM generation_attempts
                    WHERE (owner_type = 'generation_styling' AND owner_id IN ({placeholders}))
                       OR (owner_type = 'generation_preview' AND owner_id IN (
                            SELECT id FROM generation_previews WHERE task_id IN ({placeholders})
                          ))
                       OR (owner_type = 'generation_result_image' AND owner_id IN (
                            SELECT gri.id FROM generation_result_images_new gri
                            JOIN generation_result_sets grs ON grs.id = gri.result_set_id
                            WHERE grs.task_id IN ({placeholders})
                          ))
                    """,
                    (*dependent_task_ids, *dependent_task_ids, *dependent_task_ids),
                )
                db.execute(f"DELETE FROM generation_tasks WHERE id IN ({placeholders})", dependent_task_ids)
            asset_ids = [int(image["id"]) for image in image_rows] + dependent_asset_ids
            dependent_media_rows: list[Any] = []
            if dependent_asset_ids:
                placeholders = ", ".join("?" for _ in dependent_asset_ids)
                dependent_media_rows = db.execute(
                    f"SELECT object_key FROM media_assets WHERE id IN ({placeholders})",
                    dependent_asset_ids,
                ).fetchall()
            cleanup_objects([
                key
                for media in [*image_rows, *dependent_media_rows]
                for key in image_object_keys(media["object_key"])
            ])
            db.execute("DELETE FROM styling_garments WHERE id = ?", (garment_id,))
            if asset_ids:
                placeholders = ", ".join("?" for _ in asset_ids)
                db.execute(f"DELETE FROM media_assets WHERE id IN ({placeholders})", asset_ids)
            cleanup_errors = cleanup_prefixes([join_key("generation-tasks", str(task_id)) for task_id in dependent_task_ids])
            log_event(db, "styling_garment", garment_id, "purged", {"code": row["code"], "deletedTaskIds": dependent_task_ids})
            return {"ok": True, "purged": True, "deletedTaskIds": dependent_task_ids, "objectCleanupErrors": cleanup_errors}
        db.execute("UPDATE styling_garments SET status = 'DELETED', deleted_at = ?, updated_at = ? WHERE id = ?", (timestamp, timestamp, garment_id))
        db.execute("UPDATE styling_garment_images SET deleted_at = ?, updated_at = ? WHERE styling_garment_id = ?", (timestamp, timestamp, garment_id))
        log_event(db, "styling_garment", garment_id, "archived", {"code": row["code"]})
    return {"ok": True, "archived": True}


@app.post("/api/generation-tasks")
async def create_generation_task(
    name: str | None = Form(None),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="至少上传一张主推款图片")
    if len(files) > 8:
        raise HTTPException(status_code=400, detail="主推款最多上传 8 张图片")
    task_name = (name or datetime.now().strftime("%Y%m%d%H%M%S")).strip()
    prefix = join_key("generation-tasks", f"{sanitize_name(task_name) or 'task'}-{uuid4().hex[:8]}")
    assets = await save_uploads_as_media_assets(files, prefix)
    timestamp = now_iso()
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO generation_tasks (name, status, created_at, updated_at) VALUES (?, 'DRAFT', ?, ?)",
            (task_name, timestamp, timestamp),
        )
        task_id = int(cursor.lastrowid)
        for index, asset in enumerate(assets):
            db.execute(
                """
                INSERT INTO generation_task_images (task_id, media_asset_id, sort_order, status, created_at, updated_at)
                VALUES (?, ?, ?, 'READY', ?, ?)
                """,
                (task_id, asset["id"], index, timestamp, timestamp),
            )
        log_event(db, "generation_task", task_id, "created", {"name": task_name, "fileCount": len(assets)})
    return {"item": get_generation_task_by_id(task_id)}


@app.get("/api/generation-tasks")
def list_generation_tasks(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM generation_tasks WHERE deleted_at IS NULL ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit + 1, offset),
        ).fetchall()
        items = rows[:limit]
        return {
            "items": [serialize_generation_task(row, include_detail=False) for row in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if len(rows) > limit else None,
            "hasMore": len(rows) > limit,
        }


@app.get("/api/generation-tasks/{task_id}")
def get_generation_task(task_id: int, include_debug: bool = Query(False, alias="includeDebug")) -> dict[str, Any]:
    return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}


@app.patch("/api/generation-tasks/{task_id}")
def update_generation_task(task_id: int, payload: GenerationTaskUpdateRequest) -> dict[str, Any]:
    if payload.name is None:
        return {"item": get_generation_task_by_id(task_id)}
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成任务不存在")
        ensure_generation_task_draft(row)
        db.execute("UPDATE generation_tasks SET name = ?, updated_at = ? WHERE id = ?", (payload.name.strip() or "未命名任务", now_iso(), task_id))
    return {"item": get_generation_task_by_id(task_id)}


def ensure_generation_task_draft(task: Any) -> None:
    if task["status"] != "DRAFT":
        raise HTTPException(status_code=409, detail="任务进入 AI 阶段后不可修改主推款输入")


@app.post("/api/generation-tasks/{task_id}/images")
async def add_generation_task_images(task_id: int, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    ensure_generation_task_draft(task)
    with get_db() as db:
        count = int(db.execute(
            "SELECT COUNT(*) AS count FROM generation_task_images WHERE task_id = ? AND deleted_at IS NULL",
            (task_id,),
        ).fetchone()["count"])
        max_sort = int(db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS max_sort FROM generation_task_images WHERE task_id = ? AND deleted_at IS NULL",
            (task_id,),
        ).fetchone()["max_sort"])
    if not files or count + len(files) > 8:
        raise HTTPException(status_code=400, detail="主推款最多上传 8 张图片")
    prefix = join_key("generation-tasks", f"{sanitize_name(task['name']) or 'task'}-{uuid4().hex[:8]}")
    assets = await save_uploads_as_media_assets(files, prefix)
    timestamp = now_iso()
    with get_db() as db:
        for offset, asset in enumerate(assets, start=1):
            db.execute(
                """
                INSERT INTO generation_task_images (task_id, media_asset_id, sort_order, status, created_at, updated_at)
                VALUES (?, ?, ?, 'READY', ?, ?)
                """,
                (task_id, asset["id"], max_sort + offset, timestamp, timestamp),
            )
        db.execute("UPDATE generation_tasks SET updated_at = ? WHERE id = ?", (timestamp, task_id))
    return {"item": get_generation_task_by_id(task_id)}


@app.post("/api/generation-task-images/{image_id}/replace")
async def replace_generation_task_image(image_id: int, file: UploadFile = File(...)) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gti.task_id, gt.name, gt.status FROM generation_task_images gti
            JOIN generation_tasks gt ON gt.id = gti.task_id
            WHERE gti.id = ? AND gti.deleted_at IS NULL AND gt.deleted_at IS NULL
            """,
            (image_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="主推款图片不存在")
    ensure_generation_task_draft(row)
    prefix = join_key("generation-tasks", f"{sanitize_name(row['name']) or 'task'}-{uuid4().hex[:8]}")
    asset = (await save_uploads_as_media_assets([file], prefix))[0]
    with get_db() as db:
        db.execute(
            "UPDATE generation_task_images SET media_asset_id = ?, updated_at = ? WHERE id = ?",
            (asset["id"], now_iso(), image_id),
        )
        db.execute("UPDATE generation_tasks SET updated_at = ? WHERE id = ?", (now_iso(), row["task_id"]))
    return {"item": get_generation_task_by_id(int(row["task_id"]))}


@app.patch("/api/generation-task-images/{image_id}")
def update_generation_task_image(image_id: int, payload: GenerationTaskImageUpdateRequest) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gti.task_id, gt.status FROM generation_task_images gti
            JOIN generation_tasks gt ON gt.id = gti.task_id
            WHERE gti.id = ? AND gti.deleted_at IS NULL AND gt.deleted_at IS NULL
            """,
            (image_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="主推款图片不存在")
        ensure_generation_task_draft(row)
        if payload.sortOrder is not None:
            db.execute(
                "UPDATE generation_task_images SET sort_order = ?, updated_at = ? WHERE id = ?",
                (payload.sortOrder, now_iso(), image_id),
            )
            db.execute("UPDATE generation_tasks SET updated_at = ? WHERE id = ?", (now_iso(), row["task_id"]))
    return {"item": get_generation_task_by_id(int(row["task_id"]))}


@app.delete("/api/generation-task-images/{image_id}")
def delete_generation_task_image(image_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gti.task_id, gt.status FROM generation_task_images gti
            JOIN generation_tasks gt ON gt.id = gti.task_id
            WHERE gti.id = ? AND gti.deleted_at IS NULL AND gt.deleted_at IS NULL
            """,
            (image_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="主推款图片不存在")
        ensure_generation_task_draft(row)
        count = int(db.execute(
            "SELECT COUNT(*) AS count FROM generation_task_images WHERE task_id = ? AND deleted_at IS NULL",
            (row["task_id"],),
        ).fetchone()["count"])
        if count <= 1:
            raise HTTPException(status_code=409, detail="主推款至少保留一张图片")
        timestamp = now_iso()
        db.execute("UPDATE generation_task_images SET deleted_at = ?, updated_at = ? WHERE id = ?", (timestamp, timestamp, image_id))
        db.execute("UPDATE generation_tasks SET updated_at = ? WHERE id = ?", (timestamp, row["task_id"]))
    return {"item": get_generation_task_by_id(int(row["task_id"]))}


@app.post("/api/generation-tasks/{task_id}/ready")
def ready_generation_task(task_id: int) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    ensure_generation_task_draft(task)
    with get_db() as db:
        count = int(db.execute(
            "SELECT COUNT(*) AS count FROM generation_task_images WHERE task_id = ? AND deleted_at IS NULL",
            (task_id,),
        ).fetchone()["count"])
        if count < 1:
            raise HTTPException(status_code=400, detail="请先上传主推款图片")
        db.execute("UPDATE generation_tasks SET status = 'READY_FOR_STYLING', updated_at = ? WHERE id = ?", (now_iso(), task_id))
    return {"item": get_generation_task_by_id(task_id)}


@app.post("/api/generation-tasks/{task_id}/select-styling")
async def select_generation_styling(task_id: int) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    if task["status"] != "READY_FOR_STYLING" and not (
        task["status"] == "FAILED" and not task["active_styling_id"]
    ):
        raise HTTPException(status_code=409, detail="当前任务状态不能智能选搭")
    input_version_id = create_generation_input_version(task_id)
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET status = 'STYLING_RUNNING', current_input_version_id = ?, updated_at = ? WHERE id = ?", (input_version_id, now_iso(), task_id))
    attempt_id = create_attempt("generation_styling", task_id, "gemini")
    try:
        selection = await select_styling_with_gemini(task_id, input_version_id)
        selected_ids = selection.get("selected_material_ids", [])
        if not isinstance(selected_ids, list):
            raise RuntimeError("Gemini 返回 selected_material_ids 格式不合法")
        selected_ids = [int(item) for item in selected_ids]
        source_ids, selected_snapshot = get_styling_source_and_selected_snapshots(selected_ids)
    except Exception as exc:
        finish_attempt(attempt_id, "FAILED", error_message=str(exc))
        with get_db() as db:
            db.execute("UPDATE generation_tasks SET status = 'FAILED', updated_at = ? WHERE id = ?", (now_iso(), task_id))
        raise HTTPException(status_code=502, detail=f"智能选搭失败：{exc}") from exc
    with get_db() as db:
        timestamp = now_iso()
        cursor = db.execute(
            """
            INSERT INTO generation_stylings
              (task_id, input_version_id, source_material_ids_json, selected_material_ids_json,
               selected_material_snapshot_json, reason_summary, warnings_json, status, attempt_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'SUCCEEDED', ?, ?, ?)
            """,
            (
                task_id,
                input_version_id,
                dumps(source_ids),
                dumps(selected_ids),
                dumps(selected_snapshot),
                str(selection.get("reason_summary") or "已根据主推款和搭配服饰标签完成智能选搭。"),
                dumps(selection.get("warnings") or []),
                attempt_id,
                timestamp,
                timestamp,
            ),
        )
        styling_id = int(cursor.lastrowid)
        hero_summary = str(selection.get("hero_visual_summary") or "").strip()
        if hero_summary:
            db.execute("UPDATE generation_task_input_versions SET hero_visual_summary = ? WHERE id = ?", (hero_summary, input_version_id))
        db.execute(
            "UPDATE generation_tasks SET status = 'STYLING_READY', active_styling_id = ?, updated_at = ? WHERE id = ?",
            (styling_id, timestamp, task_id),
        )
    finish_attempt(attempt_id, "SUCCEEDED", response_snapshot=selection)
    return {"item": get_generation_task_by_id(task_id, include_debug=True)}


@app.post("/api/generation-tasks/{task_id}/preview")
async def create_generation_preview(task_id: int) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    styling_id = task["active_styling_id"]
    input_version_id = task["current_input_version_id"]
    if not styling_id or not input_version_id or (
        task["status"] != "STYLING_READY"
        and not (task["status"] == "FAILED" and not task["active_preview_id"])
    ):
        raise HTTPException(status_code=409, detail="请先完成智能选搭")
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET status = 'PREVIEW_RUNNING', updated_at = ? WHERE id = ?", (now_iso(), task_id))
    shot_plans = ensure_generation_shot_plans(task_id, int(styling_id))
    timestamp = now_iso()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO generation_previews (task_id, styling_id, input_version_id, status, created_at, updated_at)
            VALUES (?, ?, ?, 'RUNNING', ?, ?)
            """,
            (task_id, styling_id, input_version_id, timestamp, timestamp),
        )
        preview_id = int(cursor.lastrowid)
    attempt_id = create_attempt("generation_preview", preview_id, "image-2")
    try:
        task_package = build_new_preview_task_package(task_id, int(styling_id), shot_plans)
        asset_id, provider_response = await generate_or_placeholder_asset(task_package, join_key("generation-tasks", str(task_id), "preview", f"preview-{preview_id}.png"), "六宫格预览")
    except Exception as exc:
        finish_attempt(attempt_id, "FAILED", error_message=str(exc))
        with get_db() as db:
            timestamp = now_iso()
            db.execute("UPDATE generation_previews SET status = 'FAILED', active_attempt_id = ?, updated_at = ? WHERE id = ?", (attempt_id, timestamp, preview_id))
            db.execute("UPDATE generation_tasks SET status = 'FAILED', updated_at = ? WHERE id = ?", (timestamp, task_id))
        raise HTTPException(status_code=502, detail=f"六宫格预览失败：{exc}") from exc
    finish_attempt(attempt_id, "SUCCEEDED", response_snapshot=provider_response, output_asset_id=asset_id)
    with get_db() as db:
        timestamp = now_iso()
        db.execute(
            "UPDATE generation_previews SET status = 'SUCCEEDED', preview_asset_id = ?, active_attempt_id = ?, updated_at = ? WHERE id = ?",
            (asset_id, attempt_id, timestamp, preview_id),
        )
        db.execute(
            "UPDATE generation_tasks SET status = 'PREVIEW_READY', active_preview_id = ?, updated_at = ? WHERE id = ?",
            (preview_id, timestamp, task_id),
        )
    return {"item": get_generation_task_by_id(task_id, include_debug=True)}


@app.post("/api/generation-tasks/{task_id}/generate-final")
async def generate_final_images(task_id: int) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    if task["status"] != "PREVIEW_READY" or not task["active_preview_id"]:
        raise HTTPException(status_code=409, detail="请先生成六宫格预览")
    preview_id = int(task["active_preview_id"])
    styling_id = int(task["active_styling_id"])
    input_version_id = int(task["current_input_version_id"])
    timestamp = now_iso()
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET status = 'FINAL_RUNNING', updated_at = ? WHERE id = ?", (timestamp, task_id))
        cursor = db.execute(
            """
            INSERT INTO generation_result_sets (task_id, input_version_id, styling_id, preview_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)
            """,
            (task_id, input_version_id, styling_id, preview_id, timestamp, timestamp),
        )
        result_set_id = int(cursor.lastrowid)
        shot_rows = db.execute(
            "SELECT * FROM generation_shot_plans WHERE styling_id = ? AND sort_order <= 5 ORDER BY sort_order",
            (styling_id,),
        ).fetchall()
        image_ids: list[int] = []
        for shot in shot_rows:
            image_cursor = db.execute(
                """
                INSERT INTO generation_result_images_new
                  (result_set_id, shot_plan_id, sort_order, status, created_at, updated_at)
                VALUES (?, ?, ?, 'QUEUED', ?, ?)
                """,
                (result_set_id, shot["id"], shot["sort_order"], timestamp, timestamp),
            )
            image_ids.append(int(image_cursor.lastrowid))
    results = await asyncio.gather(
        *(generate_single_final_image(task_id, result_set_id, image_id) for image_id in image_ids),
        return_exceptions=True,
    )
    succeeded = sum(1 for result in results if not isinstance(result, Exception))
    final_status = "COMPLETED" if succeeded == len(image_ids) else "FINAL_PARTIAL"
    with get_db() as db:
        timestamp = now_iso()
        db.execute("UPDATE generation_result_sets SET status = ?, updated_at = ? WHERE id = ?", ("SUCCEEDED" if final_status == "COMPLETED" else "PARTIAL", timestamp, result_set_id))
        db.execute("UPDATE generation_tasks SET status = ?, active_result_set_id = ?, updated_at = ? WHERE id = ?", (final_status, result_set_id, timestamp, task_id))
    return {"item": get_generation_task_by_id(task_id, include_debug=True)}


@app.post("/api/generation-result-images-new/{image_id}/retry")
async def retry_new_result_image(image_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gri.*, grs.task_id, grs.id AS result_set_id
            FROM generation_result_images_new gri
            JOIN generation_result_sets grs ON grs.id = gri.result_set_id
            WHERE gri.id = ?
            """,
            (image_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="结果图不存在")
    await generate_single_final_image(int(row["task_id"]), int(row["result_set_id"]), image_id)
    with get_db() as db:
        status_rows = db.execute(
            "SELECT status FROM generation_result_images_new WHERE result_set_id = ?",
            (row["result_set_id"],),
        ).fetchall()
        completed = bool(status_rows) and all(item["status"] == "SUCCEEDED" for item in status_rows)
        status = "COMPLETED" if completed else "FINAL_PARTIAL"
        db.execute(
            "UPDATE generation_result_sets SET status = ?, updated_at = ? WHERE id = ?",
            ("SUCCEEDED" if completed else "PARTIAL", now_iso(), row["result_set_id"]),
        )
        db.execute(
            "UPDATE generation_tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_iso(), row["task_id"]),
        )
    return {"item": get_generation_task_by_id(int(row["task_id"]), include_debug=True)}


@app.get("/api/generation-records")
def list_new_generation_records(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM generation_tasks
            WHERE deleted_at IS NULL AND status <> 'DRAFT' AND current_input_version_id IS NOT NULL
            ORDER BY updated_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit + 1, offset),
        ).fetchall()
        items = rows[:limit]
        return {
            "items": [serialize_generation_task(row, include_detail=False) for row in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if len(rows) > limit else None,
            "hasMore": len(rows) > limit,
        }


@app.get("/api/generation-records/{task_id}")
def get_new_generation_record(task_id: int, include_debug: bool = Query(False, alias="includeDebug")) -> dict[str, Any]:
    return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}


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


@app.get("/api/media")
def get_media(key: str = Query(...), variant: str = Query("original")) -> RedirectResponse:
    object_key = clean_key(key)
    if variant not in {"thumb", "preview", "original"}:
        raise HTTPException(status_code=400, detail="图片规格不合法")
    target_key = object_key
    if variant in {"thumb", "preview"}:
        target_key = existing_derivative_or_original(object_key, variant) or object_key
    if not object_exists(target_key):
        raise HTTPException(status_code=404, detail="图片不存在")
    url = presigned_get_url(target_key)
    if not url:
        raise HTTPException(status_code=404, detail="图片不存在")
    return RedirectResponse(
        url,
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


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


@app.get("/api/garment-skus/{sku_id}/latest-preview-run")
def get_latest_preview_run(sku_id: int, shot_plan_id: int | None = Query(None, alias="shotPlanId")) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT id
            FROM generation_runs
            WHERE sku_id = ?
              AND kind = 'preview_grid'
              AND archived_at IS NULL
              AND (? IS NULL OR shot_plan_id = ?)
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (sku_id, shot_plan_id, shot_plan_id),
        ).fetchone()
    return {"item": get_generation_run_by_id(int(row["id"])) if row else None}


@app.get("/api/generation-runs/{run_id}")
def get_generation_run(run_id: int, include_debug: bool = Query(False, alias="includeDebug")) -> dict[str, Any]:
    return {"item": get_generation_run_by_id(run_id, include_debug=include_debug)}


@app.get("/api/generation-runs/{run_id}/status")
def get_generation_run_status(run_id: int) -> dict[str, Any]:
    return {"item": get_generation_run_by_id(run_id, status_only=True)}


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
    with get_db() as db:
        run = db.execute(
            """
            SELECT gr.*, gs.sku
            FROM generation_runs gr
            JOIN garment_skus gs ON gs.id = gr.sku_id
            WHERE gr.id = ?
            """,
            (run_id,),
        ).fetchone()
        if not run:
            raise HTTPException(status_code=404, detail="生成任务不存在")
        images = db.execute(
            """
            SELECT id, shot_index, status, output_path, is_final
            FROM generation_images
            WHERE run_id = ?
            ORDER BY shot_index, id
            """,
            (run_id,),
        ).fetchall()

    group_images = [image for image in images if int(image["shot_index"] or 0) > 0]
    if not group_images:
        raise HTTPException(status_code=422, detail="没有可导出的最终组图")
    incomplete_images = [
        image
        for image in group_images
        if image["status"] != "SUCCEEDED" or not image["output_path"]
    ]
    if incomplete_images or run["status"] != "SUCCEEDED":
        raise HTTPException(status_code=409, detail="最终组图尚未全部生成成功，不能导出 ZIP")
    final_images = [image for image in group_images if image["is_final"]]
    selected = final_images or group_images
    if not selected:
        raise HTTPException(status_code=422, detail="没有可导出的生成图")

    zip_name = f"run-{run_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    zip_buffer = BytesIO()
    written = 0
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for image in selected:
            object_key = image["output_path"]
            if not object_key:
                continue
            try:
                archive.writestr(Path(object_key).name, get_bytes(object_key))
                written += 1
            except Exception:
                continue
    if written == 0:
        raise HTTPException(status_code=422, detail="没有可导出的生成图")
    zip_bytes = zip_buffer.getvalue()
    export_key = join_key("exports", str(run["sku"] or run["sku_id"]), zip_name)
    put_bytes(export_key, zip_bytes, "application/zip")

    with get_db() as db:
        log_event(
            db,
            "generation_run",
            run_id,
            "exported",
            {"objectKey": export_key, "imageCount": written, "finalOnly": bool(final_images)},
        )
    return StreamingResponse(
        BytesIO(zip_bytes),
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
    media = provider_media_metadata(provider_response, output_path)
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
            SET status = ?, output_path = ?, thumb_path = ?, preview_path = ?,
                file_size = COALESCE(?, file_size),
                thumb_file_size = COALESCE(?, thumb_file_size),
                preview_file_size = COALESCE(?, preview_file_size),
                provider_request_json = ?,
                provider_response_json = ?, prompt_text = ?, image_inputs_json = ?,
                attempts_json = ?, current_attempt_index = ?, provider_request_id = ?,
                provider_status = ?, provider_urls_json = ?, provider_status_json = ?,
                submitted_at = ?, completed_at = ?, last_status_checked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                media.get("outputPath") or str(output_path),
                media.get("thumbPath"),
                media.get("previewPath"),
                media.get("fileSize"),
                media.get("thumbFileSize"),
                media.get("previewFileSize"),
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
    media = provider_media_metadata(result, image_row["output_path"])
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
            SET status = ?, output_path = COALESCE(?, output_path),
                thumb_path = COALESCE(?, thumb_path),
                preview_path = COALESCE(?, preview_path),
                file_size = COALESCE(?, file_size),
                thumb_file_size = COALESCE(?, thumb_file_size),
                preview_file_size = COALESCE(?, preview_file_size),
                provider_status = ?, provider_status_json = ?,
                provider_response_json = COALESCE(?, provider_response_json),
                attempts_json = ?, completed_at = COALESCE(?, completed_at),
                status_error_attempts = 0, last_status_checked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                media.get("outputPath"),
                media.get("thumbPath"),
                media.get("previewPath"),
                media.get("fileSize"),
                media.get("thumbFileSize"),
                media.get("previewFileSize"),
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


def get_generation_run_by_id(run_id: int, include_debug: bool = False, status_only: bool = False) -> dict[str, Any]:
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
        return serialize_generation_run(row, include_images=True, include_debug=include_debug, status_only=status_only)


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


def stable_media_url(key: str | Path | None, variant: str = "original") -> str | None:
    if not key:
        return None
    return f"/api/media?{urlencode({'key': clean_key(key), 'variant': variant})}"


async def save_uploads_as_media_assets(files: list[UploadFile], prefix: str) -> list[dict[str, Any]]:
    timestamp = now_iso()
    saved: list[dict[str, Any]] = []
    for index, upload in enumerate(files):
        content, content_type = await validate_upload_image(upload)
        filename = sanitize_filename(upload.filename or "image")
        key = unique_key(join_key(prefix, f"{index + 1:03d}-{filename}"))
        put_fileobj(key, BytesIO(content), content_type)
        derivative = generate_image_derivatives(key, content)
        width = height = None
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
        asset = {
            "objectKey": key,
            "thumbKey": derivative.get("thumb"),
            "previewKey": derivative.get("preview"),
            "originalFilename": upload.filename or filename,
            "mimeType": content_type,
            "width": width,
            "height": height,
            "sizeBytes": len(content),
            "thumbSizeBytes": derivative.get("thumbSize"),
            "previewSizeBytes": derivative.get("previewSize"),
            "hash": hashlib.sha256(content).hexdigest(),
        }
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO media_assets
                  (object_key, thumb_key, preview_key, original_filename, mime_type, width, height,
                   size_bytes, thumb_size_bytes, preview_size_bytes, hash, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?)
                """,
                (
                    asset["objectKey"],
                    asset["thumbKey"],
                    asset["previewKey"],
                    asset["originalFilename"],
                    asset["mimeType"],
                    asset["width"],
                    asset["height"],
                    asset["sizeBytes"],
                    asset["thumbSizeBytes"],
                    asset["previewSizeBytes"],
                    asset["hash"],
                    timestamp,
                    timestamp,
                ),
            )
            asset["id"] = int(cursor.lastrowid)
        saved.append(asset)
    return saved


def insert_generated_media_asset(object_key: str, filename: str, content: bytes, content_type: str, derivative: dict[str, Any]) -> int:
    timestamp = now_iso()
    width = height = None
    with suppress(Exception):
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO media_assets
              (object_key, thumb_key, preview_key, original_filename, mime_type, width, height,
               size_bytes, thumb_size_bytes, preview_size_bytes, hash, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?)
            """,
            (
                object_key,
                derivative.get("thumb"),
                derivative.get("preview"),
                filename,
                content_type,
                width,
                height,
                len(content),
                derivative.get("thumbSize"),
                derivative.get("previewSize"),
                hashlib.sha256(content).hexdigest(),
                timestamp,
                timestamp,
            ),
        )
        return int(cursor.lastrowid)


def serialize_media_asset(row: Any | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "objectKey": row["object_key"],
        "filename": row["original_filename"],
        "mimeType": row["mime_type"],
        "width": row["width"],
        "height": row["height"],
        "sizeBytes": row["size_bytes"],
        "url": stable_media_url(row["object_key"], "preview"),
        "thumbUrl": stable_media_url(row["object_key"], "thumb"),
        "previewUrl": stable_media_url(row["object_key"], "preview"),
        "originalUrl": stable_media_url(row["object_key"], "original"),
        "createdAt": row["created_at"],
    }


def get_media_asset_by_id(asset_id: int | None) -> dict[str, Any] | None:
    if not asset_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
    return serialize_media_asset(row)


def serialize_styling_garment(row: Any, include_images: bool = True) -> dict[str, Any]:
    ai_tags = loads(row["ai_tags_json"], {})
    manual_tags = loads(row["manual_tags_json"], {})
    official_title_tags = loads(row["official_title_tags_json"], {}) if "official_title_tags_json" in row.keys() else {}
    effective_tags = loads(row["effective_tags_json"], {}) if "effective_tags_json" in row.keys() else {}
    if not effective_tags:
        effective_tags = build_effective_tags(ai_tags, official_title_tags, manual_tags)
    item = {
        "id": row["id"],
        "code": row["code"],
        "status": row["status"],
        "manualTags": manual_tags,
        "aiTags": ai_tags,
        "officialTitleTags": official_title_tags,
        "effectiveTags": effective_tags,
        "tagMeta": loads(row["tag_meta_json"], {}) if "tag_meta_json" in row.keys() else {},
        "tagAttempts": get_styling_garment_tag_attempts(row["id"]) if include_images else [],
        "tagSchemaVersion": row["tag_schema_version"],
        "tagCompletedAt": row["tag_completed_at"],
        "tagError": row["tag_error"],
        "imageCount": row["image_count"] if "image_count" in row.keys() else None,
        "coverUrl": stable_media_url(row["cover_key"], "thumb") if "cover_key" in row.keys() else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_images:
        with get_db() as db:
            images = db.execute(
                """
                SELECT ma.*, sgi.id AS link_id, sgi.sort_order AS link_sort_order FROM styling_garment_images sgi
                JOIN media_assets ma ON ma.id = sgi.media_asset_id
                WHERE sgi.styling_garment_id = ? AND sgi.deleted_at IS NULL
                ORDER BY sgi.sort_order, sgi.id
                """,
                (row["id"],),
            ).fetchall()
        item["images"] = [
            {
                "id": image["link_id"],
                "sortOrder": image["link_sort_order"],
                "asset": serialize_media_asset(image),
            }
            for image in images
        ]
    return item


def get_styling_garment_tag_attempts(garment_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM generation_attempts
            WHERE owner_type = 'styling_garment_tagging' AND owner_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 10
            """,
            (garment_id,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "provider": row["provider"],
            "status": row["status"],
            "startedAt": row["started_at"],
            "completedAt": row["completed_at"],
            "failedAt": row["failed_at"],
            "errorMessage": row["error_message"],
            "requestSnapshot": loads(row["request_snapshot_json"], {}),
            "responseSnapshot": loads(row["response_snapshot_json"], {}),
        }
        for row in rows
    ]


def get_styling_garment_by_id(garment_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT sg.*,
              (SELECT ma.object_key FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_key,
              (SELECT COUNT(*) FROM styling_garment_images sgi WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL) AS image_count
            FROM styling_garments sg
            WHERE sg.id = ? AND sg.deleted_at IS NULL
            """,
            (garment_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="搭配服饰不存在")
    return serialize_styling_garment(row, include_images=True)


async def tag_styling_garment_with_gemini(garment_id: int) -> dict[str, Any]:
    garment = get_styling_garment_by_id(garment_id)
    prompt = build_lily_tagging_prompt(garment)
    request_snapshot = build_lily_tagging_request_snapshot(garment, prompt)
    if not settings.gemini_api_key:
        tagging_result = fallback_lily_tagging_result(garment)
        attempt_id = create_attempt("styling_garment_tagging", garment_id, "fallback", request_snapshot)
        finish_attempt(attempt_id, "SUCCEEDED", {"reason": "gemini_api_key_missing", "normalized": tagging_result})
        return tagging_result
    try:
        from google import genai
        from google.genai import types
    except Exception:
        tagging_result = fallback_lily_tagging_result(garment)
        attempt_id = create_attempt("styling_garment_tagging", garment_id, "fallback", request_snapshot)
        finish_attempt(attempt_id, "SUCCEEDED", {"reason": "google_genai_import_failed", "normalized": tagging_result})
        return tagging_result
    attempt_id = create_attempt("styling_garment_tagging", garment_id, "gemini", request_snapshot)
    contents: list[Any] = [prompt]
    for image in garment.get("images", [])[:6]:
        asset = image.get("asset") or {}
        object_key = asset.get("objectKey")
        if object_key and object_exists(object_key):
            contents.append(types.Part.from_bytes(data=get_bytes(object_key), mime_type=asset.get("mimeType") or "image/png"))
    client = genai.Client(api_key=settings.gemini_api_key)
    try:
        config = types.GenerateContentConfig(response_mime_type="application/json")
        response = await client.aio.models.generate_content(model=settings.gemini_model, contents=contents, config=config)
    except TypeError:
        response = await client.aio.models.generate_content(model=settings.gemini_model, contents=contents)
    except Exception as exc:
        finish_attempt(attempt_id, "FAILED", error_message=str(exc))
        raise
    text = getattr(response, "text", "") or "{}"
    try:
        raw = loads(extract_json_text(text), {})
    except Exception as exc:
        finish_attempt(attempt_id, "FAILED", {"raw_text": text}, error_message=f"Gemini 返回内容不是合法 JSON：{exc}")
        raise
    normalized = normalize_lily_tagging_result(raw, garment)
    finish_attempt(attempt_id, "SUCCEEDED", {"raw_text": text, "raw_json": raw, "normalized": normalized})
    return normalized


def build_lily_tagging_request_snapshot(garment: dict[str, Any], prompt: str) -> dict[str, Any]:
    return {
        "schema_version": LILY_TAG_SCHEMA_VERSION,
        "model": settings.gemini_model,
        "prompt": prompt,
        "images": [
            {
                "image_id": image.get("id"),
                "sort_order": image.get("sortOrder"),
                "role_hint": "primary" if index == 0 else "reference",
                "asset": {
                    "id": ((image.get("asset") or {}).get("id")),
                    "objectKey": ((image.get("asset") or {}).get("objectKey")),
                    "filename": ((image.get("asset") or {}).get("filename")),
                    "mimeType": ((image.get("asset") or {}).get("mimeType")),
                    "width": ((image.get("asset") or {}).get("width")),
                    "height": ((image.get("asset") or {}).get("height")),
                },
            }
            for index, image in enumerate(garment.get("images", [])[:6])
        ],
    }


def build_lily_tagging_prompt(garment: dict[str, Any]) -> str:
    image_hints = [
        {
            "image_id": image.get("id"),
            "role_hint": "primary" if index == 0 else "reference",
            "sort_order": index,
            "filename": ((image.get("asset") or {}).get("filename") or ""),
            "mime_type": ((image.get("asset") or {}).get("mimeType") or ""),
            "width": ((image.get("asset") or {}).get("width")),
            "height": ((image.get("asset") or {}).get("height")),
        }
        for index, image in enumerate(garment.get("images", [])[:6])
    ]
    tagging_input = {
        "asset_id": f"styling_garment_{garment.get('id')}",
        "code": garment.get("code"),
        "images": image_hints,
        "official_title": "",
        "brand_boundary": {
            "core": ["商务时装", "都市通勤", "年轻职业女性", "简约", "优雅", "自信", "清新明快", "轻松但得体", "时尚但不夸张", "商品优先", "现代", "利落", "干净"],
            "avoid": ["传统严肃职业制服感", "夸张时装大片感", "网红辣妹风", "森系/甜美少女风", "街头潮牌风", "宴会礼服感", "鞋包饰品抢服装主体", "复杂外景或戏剧化强光"],
        },
        "schema_version": LILY_TAG_SCHEMA_VERSION,
    }
    return f"""
你是 Lily 商务时装品牌的服装视觉生产资产打标专家。你的任务是把一组真实服装图片转成可追溯、可校验、可用于智能选搭的结构化标签。

你不是文案助手，也不是生图助手。不要写 prompt，不要写营销文案，不要决定商品是否最终可用。

【本次输入】
{dumps(tagging_input)}

【图片与文字的优先级】
1. 图片事实最高：服装品类、颜色、版型、结构、细节、可见性必须以图片为准。
2. 多图一致性第二：如果多张图疑似不同商品，标记 needs_review=true，并在 review_reasons 加 conflicting_images。
3. 官方标题只是证据层：只能进入 official_title_tags；不得覆盖图片事实。
4. Lily 品牌边界只用于风格分数和风险判断，不要把品牌词机械复制到每件商品。

【先做这些判断】
1. 判断图片组是否展示同一件可售服装资产。
2. 排除模特、鞋、包、首饰、背景、道具；它们不是本资产。
3. 判断主商品大类、子类、是否套装。
4. 判断颜色、图案、视觉材质、表面纹理、版型、轮廓、长度、领型、闭合方式、细节。
5. 判断穿着能力：可单穿、可内搭、可外搭、能否作为下装搭配、是否可能遮挡主推款。
6. 判断 Lily 场景适配和搭配约束。
7. 标注每个低置信度原因。

【枚举与评判规则】
- category:
  - top: 衬衫、T 恤、背心、针织上衣等上装。
  - bottom: 裤装、半裙、短裤等下装。
  - dress: 连衣裙。
  - jumpsuit: 连体裤/连体衣。
  - outerwear: 西装、夹克、风衣、大衣、羽绒外套等外穿外套。
  - knitwear: 以针织视觉为主且更适合作为针织单品管理的服装。
  - suit_set: 明确成套售卖或图片主体为套装。
  - other_garment: 是服装但不能稳定归入以上品类。
  - unsupported_accessory: 鞋、包、首饰、围巾、帽子等非服装配饰。
  - unknown: 图片不足或无法判断。
- sub_category 可选值: shirt, blouse, t_shirt, vest, camisole, cardigan, blazer, jacket, trench, coat, down_jacket, trousers, jeans, shorts, skirt, dress, other。
- colors.primary 可选值: black, white, ivory, beige, grey, blue, navy, brown, green, red, pink, yellow, purple, metallic, multi, unknown。
  - primary 是视觉面积最大或商品识别最重要的颜色。
  - secondary 只写明显辅色；不要写光影造成的颜色。
  - temperature: warm, cool, neutral, mixed, unknown。
  - contrast_level: low, medium, high。
  - color_distribution: solid, color_block, trim, gradient, print_based。
- materials_visual 可选值: cotton_like, linen_like, silk_like, satin_like, acetate_like, knit, wool_like, denim, leather_like, lace, chiffon, tweed_like, down_padded, technical_fabric, unknown。
  - 只判断视觉材质；不要把标题中的“桑蚕丝/莱赛尔/醋酸”等成分当作图片事实。
- surface_texture 可选值: smooth, ribbed, openwork, slub, woven_texture, sheer, glossy, matte, structured, soft_drape。
- fit 可选值: slim, regular, relaxed, loose, oversized, unknown。
  - slim=贴身修身；regular=常规合体；relaxed=略宽松；loose=明显宽松；oversized=刻意大廓形。
- silhouette 可选值: straight, a_line, h_line, x_line, wide_leg, flare, tapered, cropped, waist_defined, boxy。
- structure 可选值: tailored, soft, draped, layered, structured_shoulder, dropped_shoulder, pleated, gathered。
- length:
  - top_length: cropped, hip, long, unknown。
  - sleeve_length: sleeveless, short, elbow, three_quarter, long, unknown。
  - bottom_length: short, knee, midi, ankle, full, unknown。
- neckline 可选值: crew, v_neck, polo, shirt_collar, lapel, boat, square, halter, cowl, unknown。
- closure 可选值: button, zipper, pullover, wrap, tie, open_front, unknown。
- details 可选值: asymmetric, lace, contrast_trim, embroidery, pocket, pleat, cutout, patchwork, print, button, ruffle, bow_tie, belt, slit。
- patterns 可选值: solid, stripe, check, polka_dot, floral, graphic, abstract, texture_pattern, logo, animal, unknown。
- pattern_scale 可选值: none, small, medium, large, mixed。
- wearability:
  - can_be_worn_alone: 单独穿着是否成立。
  - can_be_inner_layer: 作为内搭是否自然。
  - can_be_outer_layer: 作为外搭是否自然。
  - can_be_bottom_pairing: 是否是下装/可与上装搭配。
  - can_be_set_anchor: 是否可作为套装核心。
  - recommended_wearing_methods 可选值: buttoned, open, tucked, untucked, belted, layered。
  - coverage_risk 可选值: none, may_cover_hero, likely_cover_hero, unknown。
- lily_style 分数均为 0-1:
  - business_fashion_score: 是否符合商务时装，而非制服或纯休闲。
  - commute_score: 是否适合都市通勤。
  - clean_score: 是否清爽、利落、不过度复杂。
  - relaxed_score: 是否轻松但得体。
  - feminine_score: 是否有女性化柔和/优雅特征。
  - fashion_detail_score: 是否有设计点但不夸张。
  - formality 可选值: casual, smart_casual, office, formal, unknown。
  - style_notes 最多 3 条，每条不超过 30 个中文字符。
- occasion 可选值: daily, commute, office, light_social, date, travel, home。
- season 可选值: spring, summer, autumn, winter。
- styling_compatibility:
  - best_pairing_categories 可选值: top, bottom, outerwear, dress, knitwear。
  - color_pairing.works_with 使用颜色枚举。
  - silhouette_pairing.works_with 可选值: slim_top, relaxed_top, wide_leg_bottom, straight_bottom, a_line_skirt。
  - style_risk 可选值: too_formal, too_casual, too_sweet, too_street, too_party, too_heavy, too_busy, none。
  - pairing_brief 必须是短句，用于全库选搭，最多 40 个中文字符。

【复核规则】
以下任一情况必须 needs_review=true:
- category 为 unknown 或 unsupported_accessory。
- overall_confidence < 0.72。
- 多图疑似不同商品。
- 图片只有局部细节，无法判断主商品。
- 标题与图片冲突。

review_reasons 可选值: low_confidence, conflicting_images, unclear_category, title_image_conflict, insufficient_images, unsupported_accessory, uncertain_material, uncertain_color, uncertain_fit。

【输出要求】
- 只输出严格 JSON，不输出 Markdown，不输出解释。
- 所有枚举字段必须从可选值中选择；不确定用 unknown 或空数组。
- 自由文本字段要短，不写散文。
- 置信度必须是 0 到 1 的数字。
- tag_meta.source_image_ids 使用输入中的 image_id。

【输出 JSON 模板】
{{
  "schema_version": "{LILY_TAG_SCHEMA_VERSION}",
  "ai_tags": {{
    "category": "unknown",
    "sub_category": "other",
    "official_category_candidates": [],
    "is_set": false,
    "colors": {{"primary": "unknown", "secondary": [], "temperature": "unknown", "contrast_level": "medium", "color_distribution": "solid"}},
    "materials_visual": [],
    "surface_texture": [],
    "fit": "unknown",
    "silhouette": [],
    "structure": [],
    "length": {{"top_length": "unknown", "sleeve_length": "unknown", "bottom_length": "unknown"}},
    "neckline": [],
    "closure": [],
    "details": [],
    "patterns": [],
    "pattern_scale": "none",
    "wearability": {{"can_be_worn_alone": true, "can_be_inner_layer": false, "can_be_outer_layer": false, "can_be_bottom_pairing": false, "can_be_set_anchor": false, "recommended_wearing_methods": [], "coverage_risk": "unknown"}},
    "lily_style": {{"business_fashion_score": 0.0, "commute_score": 0.0, "clean_score": 0.0, "relaxed_score": 0.0, "feminine_score": 0.0, "fashion_detail_score": 0.0, "formality": "unknown", "style_notes": []}},
    "occasion": [],
    "season": [],
    "season_confidence": 0.0,
    "styling_compatibility": {{"best_pairing_categories": [], "avoid_pairing_categories": [], "color_pairing": {{"works_with": [], "avoid_with": [], "notes": ""}}, "silhouette_pairing": {{"works_with": [], "avoid_with": [], "notes": ""}}, "style_risk": [], "pairing_brief": ""}}
  }},
  "official_title_tags": {{"raw_title": "", "year": "", "season_text": "", "official_series": [], "official_material_terms": [], "official_style_terms": [], "official_category_terms": [], "official_selling_points": []}},
  "tag_meta": {{"overall_confidence": 0.0, "field_confidence": {{}}, "needs_review": false, "review_reasons": [], "source_image_ids": [], "warnings": []}}
}}
"""


def fallback_lily_tagging_result(garment: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": LILY_TAG_SCHEMA_VERSION,
        "ai_tags": fallback_styling_tags(garment),
        "official_title_tags": {},
        "tag_meta": {
            "tagger_provider": "fallback",
            "tagger_model": "fallback",
            "schema_version": LILY_TAG_SCHEMA_VERSION,
            "overall_confidence": 0.5,
            "field_confidence": {},
            "needs_review": True,
            "review_reasons": ["fallback_tags"],
            "source_image_ids": [str(image.get("id")) for image in garment.get("images", [])],
            "warnings": ["Gemini 未配置或不可用，已生成保守兜底标签"],
        },
    }


def fallback_styling_tags(garment: dict[str, Any]) -> dict[str, Any]:
    code = garment.get("code", "GARMENT")
    return {
        "category": "other_garment",
        "sub_category": "other",
        "official_category_candidates": [],
        "is_set": False,
        "colors": {"primary": "unknown", "secondary": [], "temperature": "unknown", "contrast_level": "medium", "color_distribution": "solid"},
        "materials_visual": [],
        "surface_texture": [],
        "fit": "unknown",
        "silhouette": [],
        "structure": [],
        "length": {"top_length": "unknown", "sleeve_length": "unknown", "bottom_length": "unknown"},
        "neckline": [],
        "closure": [],
        "details": [],
        "patterns": ["unknown"],
        "pattern_scale": "unknown",
        "wearability": {
            "can_be_worn_alone": True,
            "can_be_inner_layer": False,
            "can_be_outer_layer": False,
            "can_be_bottom_pairing": False,
            "can_be_set_anchor": False,
            "recommended_wearing_methods": [],
            "coverage_risk": "unknown",
        },
        "lily_style": {
            "business_fashion_score": 0.5,
            "commute_score": 0.5,
            "clean_score": 0.5,
            "relaxed_score": 0.5,
            "feminine_score": 0.5,
            "fashion_detail_score": 0.5,
            "formality": "unknown",
            "style_notes": [],
        },
        "occasion": ["commute"],
        "season": ["spring", "autumn"],
        "season_confidence": 0.5,
        "styling_compatibility": {
            "best_pairing_categories": [],
            "avoid_pairing_categories": [],
            "color_pairing": {"works_with": [], "avoid_with": [], "notes": ""},
            "silhouette_pairing": {"works_with": [], "avoid_with": [], "notes": ""},
            "style_risk": ["none"],
            "pairing_brief": f"{code} 搭配服饰，通勤简洁风格",
        },
    }


def normalize_lily_tagging_result(raw: dict[str, Any], garment: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    if "ai_tags" in raw:
        ai_tags = normalize_styling_tags(raw.get("ai_tags"), garment)
        official_title_tags = normalize_official_title_tags(raw.get("official_title_tags"))
        tag_meta = normalize_tag_meta(raw.get("tag_meta"), garment)
    else:
        ai_tags = normalize_styling_tags(raw, garment)
        official_title_tags = {}
        tag_meta = normalize_tag_meta({}, garment)
    return {
        "schema_version": LILY_TAG_SCHEMA_VERSION,
        "ai_tags": ai_tags,
        "official_title_tags": official_title_tags,
        "tag_meta": tag_meta,
    }


def normalize_styling_tags(tags: Any, garment: dict[str, Any]) -> dict[str, Any]:
    fallback = fallback_styling_tags(garment)
    merged = deep_merge(fallback, tags if isinstance(tags, dict) else {})
    merged["category"] = normalize_enum(merged.get("category"), TAG_CATEGORIES, "unknown")
    merged["sub_category"] = normalize_enum(merged.get("sub_category"), TAG_SUB_CATEGORIES, "other")
    merged["is_set"] = bool(merged.get("is_set"))
    merged["colors"] = normalize_color_tags(merged.get("colors"), fallback["colors"])
    merged["materials_visual"] = normalize_enum_list(merged.get("materials_visual"), TAG_MATERIALS_VISUAL)
    merged["surface_texture"] = normalize_enum_list(merged.get("surface_texture"), TAG_SURFACE_TEXTURES)
    merged["fit"] = normalize_enum(merged.get("fit"), TAG_FITS, "unknown")
    merged["silhouette"] = normalize_enum_list(merged.get("silhouette"), TAG_SILHOUETTES)
    merged["structure"] = normalize_enum_list(merged.get("structure"), TAG_STRUCTURES)
    merged["length"] = normalize_length_tags(merged.get("length"), fallback["length"])
    merged["neckline"] = normalize_enum_list(merged.get("neckline"), TAG_NECKLINES)
    merged["closure"] = normalize_enum_list(merged.get("closure"), TAG_CLOSURES)
    merged["details"] = normalize_enum_list(merged.get("details"), TAG_DETAILS)
    merged["patterns"] = normalize_enum_list(merged.get("patterns"), TAG_PATTERNS)
    merged["pattern_scale"] = normalize_enum(merged.get("pattern_scale"), TAG_PATTERN_SCALES, "unknown")
    merged["wearability"] = normalize_wearability_tags(merged.get("wearability"), fallback["wearability"])
    merged["lily_style"] = normalize_score_object(merged.get("lily_style"), fallback["lily_style"])
    merged["occasion"] = normalize_enum_list(merged.get("occasion"), TAG_OCCASIONS)
    merged["season"] = normalize_enum_list(merged.get("season"), TAG_SEASONS)
    merged["season_confidence"] = clamp_float(merged.get("season_confidence"), 0.0, 1.0, 0.0)
    merged["styling_compatibility"] = normalize_styling_compatibility(merged.get("styling_compatibility"), fallback["styling_compatibility"])
    return merged


def normalize_official_title_tags(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    normalized = {
        "raw_title": str(value.get("raw_title") or "")[:160],
        "year": str(value.get("year") or "")[:16],
        "season_text": str(value.get("season_text") or "")[:40],
        "official_series": ensure_string_list(value.get("official_series")),
        "official_material_terms": ensure_string_list(value.get("official_material_terms")),
        "official_style_terms": ensure_string_list(value.get("official_style_terms")),
        "official_category_terms": ensure_string_list(value.get("official_category_terms")),
        "official_selling_points": ensure_string_list(value.get("official_selling_points")),
    }
    return normalized


def normalize_manual_business_tags(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "season_year",
        "season_drop",
        "launch_batch",
        "retail_channel",
        "series",
        "collaboration",
        "internal_collection",
        "styling_priority",
        "do_not_use_for_styling",
        "notes",
    }
    normalized = {key: value[key] for key in allowed if key in value}
    for key in ("retail_channel", "series", "collaboration", "internal_collection"):
        if key in normalized:
            normalized[key] = ensure_string_list(normalized[key])
    if "do_not_use_for_styling" in normalized:
        normalized["do_not_use_for_styling"] = bool(normalized["do_not_use_for_styling"])
    return normalized


def normalize_tag_meta(value: Any, garment: dict[str, Any]) -> dict[str, Any]:
    meta = value if isinstance(value, dict) else {}
    confidence = clamp_float(meta.get("overall_confidence"), 0.0, 1.0, 0.5)
    review_reasons = normalize_enum_list(meta.get("review_reasons"), TAG_REVIEW_REASONS)
    if confidence < TAG_REVIEW_CONFIDENCE_THRESHOLD and "low_confidence" not in review_reasons:
        review_reasons.append("low_confidence")
    return {
        "tagger_provider": str(meta.get("tagger_provider") or "gemini")[:40],
        "tagger_model": str(meta.get("tagger_model") or settings.gemini_model)[:80],
        "schema_version": LILY_TAG_SCHEMA_VERSION,
        "overall_confidence": confidence,
        "field_confidence": meta.get("field_confidence") if isinstance(meta.get("field_confidence"), dict) else {},
        "needs_review": bool(meta.get("needs_review")) or confidence < TAG_REVIEW_CONFIDENCE_THRESHOLD,
        "review_reasons": review_reasons,
        "source_image_ids": ensure_string_list(meta.get("source_image_ids")) or [str(image.get("id")) for image in garment.get("images", [])],
        "warnings": ensure_string_list(meta.get("warnings")),
    }


def tagging_status_for_result(ai_tags: dict[str, Any], tag_meta: dict[str, Any]) -> str:
    if tag_meta.get("needs_review") or tag_meta.get("tagger_provider") == "fallback":
        return "TAGGING_READY"
    if ai_tags.get("category") in {"unknown", "unsupported_accessory"}:
        return "TAGGING_READY"
    return "READY"


def apply_tag_review_policy(ai_tags: dict[str, Any], tag_meta: dict[str, Any]) -> dict[str, Any]:
    reasons = normalize_enum_list(tag_meta.get("review_reasons"), TAG_REVIEW_REASONS)
    category = ai_tags.get("category")
    if category == "unknown" and "unclear_category" not in reasons:
        reasons.append("unclear_category")
    if category == "unsupported_accessory" and "unsupported_accessory" not in reasons:
        reasons.append("unsupported_accessory")
    if tag_meta.get("tagger_provider") == "fallback" and "fallback_tags" not in reasons:
        reasons.append("fallback_tags")
    needs_review = bool(tag_meta.get("needs_review")) or bool(reasons)
    return {**tag_meta, "needs_review": needs_review, "review_reasons": reasons}


def build_effective_tags(ai_tags: dict[str, Any], official_title_tags: dict[str, Any], manual_tags: dict[str, Any]) -> dict[str, Any]:
    compatibility = ai_tags.get("styling_compatibility") if isinstance(ai_tags.get("styling_compatibility"), dict) else {}
    colors = ai_tags.get("colors") if isinstance(ai_tags.get("colors"), dict) else {}
    return {
        "schema_version": LILY_TAG_SCHEMA_VERSION,
        "category": ai_tags.get("category", "unknown"),
        "sub_category": ai_tags.get("sub_category", "other"),
        "colors": [value for value in [colors.get("primary"), *(colors.get("secondary") or [])] if value],
        "fit": ai_tags.get("fit", "unknown"),
        "silhouette": ai_tags.get("silhouette", []),
        "materials_visual": ai_tags.get("materials_visual", []),
        "details": ai_tags.get("details", []),
        "patterns": ai_tags.get("patterns", []),
        "occasion": ai_tags.get("occasion", []),
        "season": ai_tags.get("season", []),
        "wearability": ai_tags.get("wearability", {}),
        "lily_style": ai_tags.get("lily_style", {}),
        "styling_compatibility": compatibility,
        "official": official_title_tags,
        "manual": manual_tags,
        "pairing_brief": compatibility.get("pairing_brief", ""),
        "risk": compatibility.get("style_risk", []),
    }


def compress_tags_for_styling(row: Any) -> dict[str, Any]:
    effective = loads(row["effective_tags_json"], {}) if "effective_tags_json" in row.keys() else {}
    if not effective:
        effective = build_effective_tags(loads(row["ai_tags_json"], {}), {}, loads(row["manual_tags_json"], {}))
    manual = effective.get("manual") if isinstance(effective.get("manual"), dict) else {}
    return {
        "id": row["id"],
        "code": row["code"],
        "category": effective.get("category", "unknown"),
        "sub_category": effective.get("sub_category", "other"),
        "colors": effective.get("colors", []),
        "fit": effective.get("fit", "unknown"),
        "silhouette": effective.get("silhouette", []),
        "materials_visual": effective.get("materials_visual", []),
        "details": effective.get("details", []),
        "occasion": effective.get("occasion", []),
        "season": effective.get("season", []),
        "wearability": effective.get("wearability", {}),
        "manual": {
            "season_year": manual.get("season_year"),
            "series": manual.get("series", []),
            "do_not_use_for_styling": bool(manual.get("do_not_use_for_styling")),
        },
        "pairing_brief": effective.get("pairing_brief", ""),
        "risk": effective.get("risk", []),
    }


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_score_object(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = {**fallback, **source}
    for key in ("business_fashion_score", "commute_score", "clean_score", "relaxed_score", "feminine_score", "fashion_detail_score"):
        result[key] = clamp_float(result.get(key), 0.0, 1.0, 0.0)
    result["formality"] = normalize_enum(result.get("formality"), TAG_FORMALITIES, "unknown")
    result["style_notes"] = [str(item)[:30] for item in ensure_string_list(result.get("style_notes"))[:3]]
    return result


def normalize_enum(value: Any, allowed: set[str], fallback: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else fallback


def normalize_enum_list(value: Any, allowed: set[str], limit: int = 12) -> list[str]:
    values = ensure_string_list(value)
    result: list[str] = []
    for item in values:
        if item in allowed and item not in result:
            result.append(item)
    return result[:limit]


def normalize_color_tags(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "primary": normalize_enum(source.get("primary"), TAG_COLORS, fallback.get("primary", "unknown")),
        "secondary": normalize_enum_list(source.get("secondary"), TAG_COLORS, limit=4),
        "temperature": normalize_enum(source.get("temperature"), TAG_COLOR_TEMPERATURES, fallback.get("temperature", "unknown")),
        "contrast_level": normalize_enum(source.get("contrast_level"), TAG_CONTRAST_LEVELS, fallback.get("contrast_level", "medium")),
        "color_distribution": normalize_enum(source.get("color_distribution"), TAG_COLOR_DISTRIBUTIONS, fallback.get("color_distribution", "solid")),
    }


def normalize_length_tags(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "top_length": normalize_enum(source.get("top_length"), TAG_TOP_LENGTHS, fallback.get("top_length", "unknown")),
        "sleeve_length": normalize_enum(source.get("sleeve_length"), TAG_SLEEVE_LENGTHS, fallback.get("sleeve_length", "unknown")),
        "bottom_length": normalize_enum(source.get("bottom_length"), TAG_BOTTOM_LENGTHS, fallback.get("bottom_length", "unknown")),
    }


def normalize_wearability_tags(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "can_be_worn_alone": bool(source.get("can_be_worn_alone", fallback.get("can_be_worn_alone", True))),
        "can_be_inner_layer": bool(source.get("can_be_inner_layer", fallback.get("can_be_inner_layer", False))),
        "can_be_outer_layer": bool(source.get("can_be_outer_layer", fallback.get("can_be_outer_layer", False))),
        "can_be_bottom_pairing": bool(source.get("can_be_bottom_pairing", fallback.get("can_be_bottom_pairing", False))),
        "can_be_set_anchor": bool(source.get("can_be_set_anchor", fallback.get("can_be_set_anchor", False))),
        "recommended_wearing_methods": normalize_enum_list(source.get("recommended_wearing_methods"), TAG_WEARING_METHODS),
        "coverage_risk": normalize_enum(source.get("coverage_risk"), TAG_COVERAGE_RISKS, fallback.get("coverage_risk", "unknown")),
    }


def normalize_pairing_block(value: Any, allowed_values: set[str]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "works_with": normalize_enum_list(source.get("works_with"), allowed_values),
        "avoid_with": normalize_enum_list(source.get("avoid_with"), allowed_values),
        "notes": str(source.get("notes") or "")[:80],
    }


def normalize_styling_compatibility(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    fallback_color = fallback.get("color_pairing") if isinstance(fallback.get("color_pairing"), dict) else {}
    fallback_silhouette = fallback.get("silhouette_pairing") if isinstance(fallback.get("silhouette_pairing"), dict) else {}
    color_pairing = source.get("color_pairing") if isinstance(source.get("color_pairing"), dict) else fallback_color
    silhouette_pairing = source.get("silhouette_pairing") if isinstance(source.get("silhouette_pairing"), dict) else fallback_silhouette
    return {
        "best_pairing_categories": normalize_enum_list(source.get("best_pairing_categories"), TAG_PAIRING_CATEGORIES),
        "avoid_pairing_categories": normalize_enum_list(source.get("avoid_pairing_categories"), TAG_PAIRING_CATEGORIES),
        "color_pairing": normalize_pairing_block(color_pairing, TAG_COLORS),
        "silhouette_pairing": normalize_pairing_block(silhouette_pairing, TAG_SILHOUETTE_PAIRINGS),
        "style_risk": normalize_enum_list(source.get("style_risk"), TAG_STYLE_RISKS) or ["none"],
        "pairing_brief": str(source.get("pairing_brief") or fallback.get("pairing_brief") or "")[:40],
    }


def ensure_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item)[:80] for item in value if item is not None and str(item)]
    if isinstance(value, str) and value:
        return [value[:80]]
    return []


def clamp_float(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def extract_json_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end >= start:
        return stripped[start : end + 1]
    return stripped or "{}"


def get_generation_task_row(task_id: int) -> Any:
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    return row


def create_generation_input_version(task_id: int) -> int:
    task = get_generation_task_row(task_id)
    with get_db() as db:
        image_rows = db.execute(
            """
            SELECT gti.sort_order, ma.* FROM generation_task_images gti
            JOIN media_assets ma ON ma.id = gti.media_asset_id
            WHERE gti.task_id = ? AND gti.deleted_at IS NULL
            ORDER BY gti.sort_order, gti.id
            """,
            (task_id,),
        ).fetchall()
        if not image_rows:
            raise HTTPException(status_code=400, detail="请先上传主推款图片")
        version_number = int(db.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version FROM generation_task_input_versions WHERE task_id = ?",
            (task_id,),
        ).fetchone()["next_version"])
        timestamp = now_iso()
        snapshot = [
            {"sortOrder": row["sort_order"], "asset": serialize_media_asset(row)}
            for row in image_rows
        ]
        model_snapshot = snapshot_default_model()
        cursor = db.execute(
            """
            INSERT INTO generation_task_input_versions
              (task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, version_number, task["name"], dumps(snapshot), dumps(model_snapshot), timestamp),
        )
        return int(cursor.lastrowid)


def create_attempt(owner_type: str, owner_id: int, provider: str, request_snapshot: dict[str, Any] | None = None) -> int:
    timestamp = now_iso()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO generation_attempts
              (owner_type, owner_id, provider, status, started_at, request_snapshot_json, created_at, updated_at)
            VALUES (?, ?, ?, 'RUNNING', ?, ?, ?, ?)
            """,
            (owner_type, owner_id, provider, timestamp, dumps(request_snapshot or {}), timestamp, timestamp),
        )
        return int(cursor.lastrowid)


def finish_attempt(
    attempt_id: int,
    status: str,
    response_snapshot: dict[str, Any] | None = None,
    output_asset_id: int | None = None,
    error_message: str | None = None,
) -> None:
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_attempts
            SET status = ?, completed_at = ?, failed_at = ?, response_snapshot_json = ?, output_asset_id = ?,
                error_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                timestamp if status == "SUCCEEDED" else None,
                timestamp if status == "FAILED" else None,
                dumps(response_snapshot or {}),
                output_asset_id,
                error_message,
                timestamp,
                attempt_id,
            ),
        )


async def select_styling_with_gemini(task_id: int, input_version_id: int) -> dict[str, Any]:
    with get_db() as db:
        input_row = db.execute("SELECT * FROM generation_task_input_versions WHERE id = ?", (input_version_id,)).fetchone()
        material_rows = db.execute(
            """
            SELECT id, code, ai_tags_json, manual_tags_json, effective_tags_json
            FROM styling_garments
            WHERE deleted_at IS NULL AND status = 'READY'
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
    material_cards = [compress_tags_for_styling(row) for row in material_rows]
    material_cards = [card for card in material_cards if not (card.get("manual") or {}).get("do_not_use_for_styling")]
    if not settings.gemini_api_key:
        return fallback_styling_selection(input_row, material_cards)
    try:
        from google import genai
        from google.genai import types
    except Exception:
        return fallback_styling_selection(input_row, material_cards)
    prompt = f"""
你是服饰品牌 AI 摄影棚的智能选搭助手。
任务：根据主推款图片，从搭配服饰库中选择适合一起穿到模特身上的配角服饰。
你只负责选择服饰 ID，不生成穿着关系和拍摄方案。
允许返回空数组。只能从 material_cards 中选择 ID。

material_cards:
{dumps(material_cards)}

请只返回 JSON：
{{
  "selected_material_ids": [1, 2],
  "hero_visual_summary": "极短主推款视觉摘要",
  "reason_summary": "极短选择理由",
  "warnings": []
}}
"""
    contents: list[Any] = [prompt]
    for item in loads(input_row["image_snapshot_json"], [])[:8]:
        asset = (item.get("asset") or {})
        object_key = asset.get("objectKey")
        if object_key and object_exists(object_key):
            contents.append(types.Part.from_bytes(data=get_bytes(object_key), mime_type=asset.get("mimeType") or "image/png"))
    client = genai.Client(api_key=settings.gemini_api_key)
    response = await client.aio.models.generate_content(model=settings.gemini_model, contents=contents)
    return loads(extract_json_text(getattr(response, "text", "") or "{}"), {})


def fallback_styling_selection(input_row: Any, material_cards: list[dict[str, Any]]) -> dict[str, Any]:
    selected = [int(card["id"]) for card in material_cards[:2]]
    return {
        "selected_material_ids": selected,
        "hero_visual_summary": "主推款图片已作为商品事实源。",
        "reason_summary": "开发环境未配置 Gemini，已按最近可用搭配服饰生成默认选搭。",
        "warnings": ["fallback_selection"],
    }


def get_styling_source_and_selected_snapshots(selected_ids: list[int]) -> tuple[list[int], list[dict[str, Any]]]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM styling_garments WHERE deleted_at IS NULL AND status = 'READY' ORDER BY updated_at DESC, id DESC").fetchall()
    source_ids = [int(row["id"]) for row in rows]
    selected_set = set(selected_ids)
    invalid = selected_set - set(source_ids)
    if invalid:
        raise RuntimeError(f"Gemini 返回了非法搭配服饰 ID：{sorted(invalid)}")
    return source_ids, [get_styling_garment_by_id(item_id) for item_id in selected_ids]


def ensure_generation_shot_plans(task_id: int, styling_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        existing = db.execute(
            "SELECT * FROM generation_shot_plans WHERE styling_id = ? ORDER BY sort_order",
            (styling_id,),
        ).fetchall()
        if existing:
            return [serialize_new_shot_plan(row) for row in existing]
        styling = db.execute("SELECT * FROM generation_stylings WHERE id = ?", (styling_id,)).fetchone()
        selected_snapshot = loads(styling["selected_material_snapshot_json"], [])
    plans = build_default_new_shot_plans(selected_snapshot)
    with get_db() as db:
        timestamp = now_iso()
        for plan in plans:
            db.execute(
                """
                INSERT INTO generation_shot_plans
                  (task_id, styling_id, sort_order, shot_type, plan_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, styling_id, plan["sortOrder"], plan["shotType"], dumps(plan), timestamp, timestamp),
            )
    with get_db() as db:
        rows = db.execute("SELECT * FROM generation_shot_plans WHERE styling_id = ? ORDER BY sort_order", (styling_id,)).fetchall()
    return [serialize_new_shot_plan(row) for row in rows]


def build_default_new_shot_plans(selected_snapshot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    garment_refs = ["hero", *[f"styling:{item.get('id')}" for item in selected_snapshot]]
    base = [
        ("hero", "正面全身主视觉", "正面自然站姿", "正面", "主推款完整轮廓，搭配服饰自然可见"),
        ("full_body", "全身搭配展示", "轻微 3/4 身位，重心自然偏移", "轻斜正", "主推款与搭配服饰的整体比例"),
        ("half_body", "半身结构展示", "上半身克制姿态，手臂自然", "正面或轻侧", "领口、肩线、门襟、内搭关系"),
        ("side", "侧身版型展示", "侧身轻走动感", "侧面", "厚薄、衣长、垂坠和侧面轮廓"),
        ("detail", "设计细节近景", "局部近景，可不露脸", "局部", "面料、袖口、腰线或关键设计点"),
        ("reference", "参考格", "备用姿态", "自然", "参考，不进入最终生成"),
    ]
    return [
        {
            "sortOrder": index,
            "shotType": shot_type,
            "framing": framing,
            "pose": pose,
            "cameraAngle": angle,
            "mainFocus": focus,
            "visibleGarments": garment_refs,
            "generatedAccessoryNotes": "可生成低存在感鞋、包、首饰作为辅助，不做特写，不抢主推款。",
            "backgroundLighting": "白/浅灰棚拍背景，柔和均匀光线，轻微落地阴影。",
            "compositionNotes": "构图克制、商品优先、留白干净，避免报刊大片和外景感。",
            "negativeConstraints": ["不要文字水印", "不要改款换色", "不要让生成配饰成为视觉中心"],
        }
        for index, (shot_type, framing, pose, angle, focus) in enumerate(base, start=1)
    ]


def serialize_new_shot_plan(row: Any) -> dict[str, Any]:
    plan = loads(row["plan_json"], {})
    return {"id": row["id"], "sortOrder": row["sort_order"], "shotType": row["shot_type"], "plan": plan}


def build_new_preview_task_package(task_id: int, styling_id: int, shot_plans: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generation": {"stage": "preview_grid"},
        "prompt": build_new_preview_prompt(shot_plans),
        "imageInputs": build_new_image_inputs(task_id, styling_id, include_preview=False),
        "shotPlans": shot_plans,
    }


def build_new_preview_prompt(shot_plans: list[dict[str, Any]]) -> str:
    return "\n".join([
        "生成一张 3x2 六宫格电商棚拍分镜预览图。",
        "主推款和搭配服饰参考图是真实商品事实来源，必须保持商品级保真。",
        "数字模特参考图用于保持同一身份、发型和气质。",
        "鞋、包、饰品只作为辅助搭配，不作为画面核心，不做特写。",
        "六个格子按以下 ShotPlan 执行，第 6 格仅参考，最终不会生成大图。",
        dumps([plan["plan"] for plan in shot_plans]),
        "不要生成文字、水印、logo、编号或边框说明。",
    ])


def build_new_final_task_package(task_id: int, result_image_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gri.*, gsp.plan_json, gp.preview_asset_id, grs.styling_id
            FROM generation_result_images_new gri
            JOIN generation_result_sets grs ON grs.id = gri.result_set_id
            JOIN generation_previews gp ON gp.id = grs.preview_id
            JOIN generation_shot_plans gsp ON gsp.id = gri.shot_plan_id
            WHERE gri.id = ?
            """,
            (result_image_id,),
        ).fetchone()
    shot_plan = loads(row["plan_json"], {})
    return {
        "generation": {"stage": "final_image"},
        "prompt": build_new_final_prompt(int(row["sort_order"]), shot_plan),
        "imageInputs": build_new_image_inputs(task_id, int(row["styling_id"]), include_preview=True, preview_asset_id=row["preview_asset_id"]),
        "shotPlan": shot_plan,
    }


def build_new_final_prompt(shot_index: int, shot_plan: dict[str, Any]) -> str:
    return "\n".join([
        f"生成一张电商棚拍成片。当前生成第 {shot_index} 张，只生成单张图，不要生成拼图或六宫格。",
        f"参考六宫格预览图中的第 {shot_index} 格，只参考姿势、身位、景别、构图、背景和光影方向。",
        "六宫格不是商品事实来源。主推款参考图是主角商品事实来源，搭配服饰参考图是配角商品事实来源。",
        "必须保持主推款和搭配服饰的品类、颜色、面料、版型、结构、比例和可见细节。",
        f"当前 ShotPlan：{dumps(shot_plan)}",
        "鞋、包、饰品只作为辅助搭配，不作为画面核心，不做特写。",
        "不要改款、换色、替换商品；不要添加文字、水印、logo。",
    ])


def build_new_image_inputs(task_id: int, styling_id: int, include_preview: bool, preview_asset_id: int | None = None) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    if include_preview and preview_asset_id:
        asset = get_media_asset_by_id(preview_asset_id)
        if asset:
            inputs.append(image_input_from_asset(asset, "preview_grid"))
    with get_db() as db:
        styling = db.execute(
            "SELECT selected_material_snapshot_json, input_version_id FROM generation_stylings WHERE id = ?",
            (styling_id,),
        ).fetchone()
        input_version = db.execute(
            "SELECT image_snapshot_json, model_snapshot_json FROM generation_task_input_versions WHERE id = ?",
            (styling["input_version_id"],),
        ).fetchone() if styling else None
    hero_snapshot = loads(input_version["image_snapshot_json"], []) if input_version else []
    inputs.extend(image_input_from_asset(item.get("asset"), "hero_garment") for item in hero_snapshot[:3])
    selected_materials = loads(styling["selected_material_snapshot_json"], []) if styling else []
    remaining = max(0, 10 - len(inputs) - 2)
    for garment in selected_materials:
        if remaining <= 0:
            break
        images = garment.get("images", [])
        if images:
            inputs.append(image_input_from_asset(images[0]["asset"], "styling_garment"))
            remaining -= 1
    model_snapshot = loads(input_version["model_snapshot_json"], {}) if input_version else {}
    model_inputs = model_snapshot.get("imageInputs", []) if isinstance(model_snapshot, dict) else []
    if not model_inputs:
        model_inputs = get_default_model_image_inputs(limit=2)
    inputs.extend(model_inputs[:max(0, 10 - len(inputs))][:2])
    return inputs[:10]


def image_input_from_asset(asset: dict[str, Any] | None, role: str) -> dict[str, Any]:
    asset = asset or {}
    return {
        "role": role,
        "assetId": asset.get("id"),
        "objectKey": asset.get("objectKey"),
        "filename": asset.get("filename"),
        "url": asset.get("originalUrl"),
    }


def get_default_model_image_inputs(limit: int = 2) -> list[dict[str, Any]]:
    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles ORDER BY is_default DESC, id LIMIT 1").fetchone()
    if not row:
        return []
    profile = loads(row["profile_json"], {})
    refs = ordered_head_references(profile, profile.get("headReferences") or {})
    inputs: list[dict[str, Any]] = []
    for angle, filename in refs.items():
        key = join_key(row["asset_dir"], filename)
        if filename and object_exists(key):
            inputs.append({"role": "model_identity", "objectKey": key, "filename": filename, "angle": angle})
        if len(inputs) >= limit:
            break
    return inputs


def snapshot_default_model() -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles ORDER BY is_default DESC, id LIMIT 1").fetchone()
    if not row:
        return {}
    return {
        "id": row["id"],
        "modelId": row["model_id"],
        "name": row["name"],
        "profile": loads(row["profile_json"], {}),
        "imageInputs": get_default_model_image_inputs(limit=2),
    }


async def generate_or_placeholder_asset(task_package: dict[str, Any], output_key: str, label: str) -> tuple[int, dict[str, Any]]:
    if settings.fal_key:
        payload = await get_image_provider().generate(task_package, output_key)
        content = get_bytes(payload["outputPath"])
        asset_id = insert_generated_media_asset(
            payload["outputPath"],
            Path(payload["outputPath"]).name,
            content,
            f"image/{settings.gpt_image2_output_format}",
            {
                "thumb": payload.get("thumbPath"),
                "preview": payload.get("previewPath"),
                "thumbSize": payload.get("thumbFileSize"),
                "previewSize": payload.get("previewFileSize"),
            },
        )
        return asset_id, payload
    content = create_placeholder_png(label)
    put_bytes(output_key, content, "image/png")
    derivative = generate_image_derivatives(output_key, content)
    asset_id = insert_generated_media_asset(output_key, Path(output_key).name, content, "image/png", derivative)
    return asset_id, {"provider": "placeholder", "outputPath": output_key, "message": "FAL_KEY 未配置，生成占位图"}


def create_placeholder_png(label: str) -> bytes:
    image = Image.new("RGB", (1200, 1200), (246, 248, 250))
    try:
        from PIL import ImageDraw
        draw = ImageDraw.Draw(image)
        for y in range(0, 1200, 24):
            shade = 246 - min(18, y // 80)
            draw.line((0, y, 1200, y), fill=(shade, shade + 2, 252))
        draw.rounded_rectangle((120, 120, 1080, 1080), radius=36, outline=(150, 170, 210), width=3)
        draw.text((170, 170), "Ruyi-Dream", fill=(20, 30, 45))
        draw.text((170, 220), label, fill=(40, 70, 110))
        draw.text((170, 1020), "placeholder image - configure FAL_KEY for Image-2", fill=(90, 105, 130))
    except Exception:
        pass
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def generate_single_final_image(task_id: int, result_set_id: int, image_id: int) -> int:
    with get_db() as db:
        db.execute("UPDATE generation_result_images_new SET status = 'RUNNING', updated_at = ? WHERE id = ?", (now_iso(), image_id))
    attempt_id = create_attempt("generation_result_image", image_id, "image-2")
    try:
        task_package = build_new_final_task_package(task_id, image_id)
        with get_db() as db:
            sort_order = db.execute("SELECT sort_order FROM generation_result_images_new WHERE id = ?", (image_id,)).fetchone()["sort_order"]
        output_key = join_key("generation-tasks", str(task_id), "final", f"{result_set_id}-{sort_order}.png")
        asset_id, provider_response = await generate_or_placeholder_asset(task_package, output_key, f"最终成片 {sort_order}")
    except Exception as exc:
        finish_attempt(attempt_id, "FAILED", error_message=str(exc))
        with get_db() as db:
            db.execute(
                "UPDATE generation_result_images_new SET status = 'FAILED', active_attempt_id = ?, updated_at = ? WHERE id = ?",
                (attempt_id, now_iso(), image_id),
            )
        raise
    finish_attempt(attempt_id, "SUCCEEDED", response_snapshot=provider_response, output_asset_id=asset_id)
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_result_images_new
            SET status = 'SUCCEEDED', current_asset_id = ?, active_attempt_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (asset_id, attempt_id, now_iso(), image_id),
        )
    return asset_id


def serialize_generation_task(row: Any, include_detail: bool = True, include_debug: bool = False) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "currentInputVersionId": row["current_input_version_id"],
        "activeStylingId": row["active_styling_id"],
        "activePreviewId": row["active_preview_id"],
        "activeResultSetId": row["active_result_set_id"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if not include_detail:
        item["summary"] = get_generation_task_summary(row["id"])
        return item
    with get_db() as db:
        image_rows = db.execute(
            """
            SELECT ma.*, gti.id AS link_id, gti.sort_order AS link_sort_order FROM generation_task_images gti
            JOIN media_assets ma ON ma.id = gti.media_asset_id
            WHERE gti.task_id = ? AND gti.deleted_at IS NULL
            ORDER BY gti.sort_order, gti.id
            """,
            (row["id"],),
        ).fetchall()
    item["images"] = [{"id": image["link_id"], "sortOrder": image["link_sort_order"], "asset": serialize_media_asset(image)} for image in image_rows]
    item["inputVersion"] = get_input_version(row["current_input_version_id"])
    item["styling"] = get_generation_styling(row["active_styling_id"])
    item["shotPlans"] = get_task_shot_plans(row["active_styling_id"])
    item["preview"] = get_generation_preview(row["active_preview_id"])
    item["resultSet"] = get_generation_result_set(row["active_result_set_id"])
    if include_debug:
        item["attempts"] = get_task_attempts(row["id"])
    return item


def get_generation_task_by_id(task_id: int, include_debug: bool = False) -> dict[str, Any]:
    row = get_generation_task_row(task_id)
    return serialize_generation_task(row, include_detail=True, include_debug=include_debug)


def get_generation_task_summary(task_id: int) -> dict[str, Any]:
    task = get_generation_task_by_id(task_id, include_debug=False)
    images = task.get("images", [])
    result_images = ((task.get("resultSet") or {}).get("images") or [])
    return {
        "cover": images[0]["asset"] if images else None,
        "preview": (task.get("preview") or {}).get("asset"),
        "finalImages": [image.get("asset") for image in result_images if image.get("asset")],
        "selectedGarments": ((task.get("styling") or {}).get("selectedMaterials") or []),
    }


def get_input_version(input_version_id: int | None) -> dict[str, Any] | None:
    if not input_version_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_task_input_versions WHERE id = ?", (input_version_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "taskId": row["task_id"],
        "versionNumber": row["version_number"],
        "nameSnapshot": row["name_snapshot"],
        "imageSnapshot": loads(row["image_snapshot_json"], []),
        "modelSnapshot": loads(row["model_snapshot_json"], {}) if "model_snapshot_json" in row.keys() else {},
        "heroVisualSummary": row["hero_visual_summary"],
        "createdAt": row["created_at"],
    }


def get_generation_styling(styling_id: int | None) -> dict[str, Any] | None:
    if not styling_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_stylings WHERE id = ?", (styling_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "taskId": row["task_id"],
        "inputVersionId": row["input_version_id"],
        "sourceMaterialIds": loads(row["source_material_ids_json"], []),
        "selectedMaterialIds": loads(row["selected_material_ids_json"], []),
        "selectedMaterials": loads(row["selected_material_snapshot_json"], []),
        "reasonSummary": row["reason_summary"],
        "warnings": loads(row["warnings_json"], []),
        "status": row["status"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def get_task_shot_plans(styling_id: int | None) -> list[dict[str, Any]]:
    if not styling_id:
        return []
    with get_db() as db:
        rows = db.execute("SELECT * FROM generation_shot_plans WHERE styling_id = ? ORDER BY sort_order", (styling_id,)).fetchall()
    return [serialize_new_shot_plan(row) for row in rows]


def get_generation_preview(preview_id: int | None) -> dict[str, Any] | None:
    if not preview_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_previews WHERE id = ?", (preview_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "status": row["status"],
        "asset": get_media_asset_by_id(row["preview_asset_id"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def get_generation_result_set(result_set_id: int | None) -> dict[str, Any] | None:
    if not result_set_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_result_sets WHERE id = ?", (result_set_id,)).fetchone()
        images = db.execute(
            "SELECT * FROM generation_result_images_new WHERE result_set_id = ? ORDER BY sort_order",
            (result_set_id,),
        ).fetchall()
    if not row:
        return None
    return {
        "id": row["id"],
        "status": row["status"],
        "images": [
            {
                "id": image["id"],
                "sortOrder": image["sort_order"],
                "status": image["status"],
                "asset": get_media_asset_by_id(image["current_asset_id"]),
                "activeAttemptId": image["active_attempt_id"],
            }
            for image in images
        ],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def get_task_attempts(task_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT ga.* FROM generation_attempts ga
            WHERE (ga.owner_type = 'generation_styling' AND ga.owner_id = ?)
               OR ga.owner_id IN (SELECT id FROM generation_previews WHERE task_id = ?)
               OR ga.owner_id IN (
                    SELECT gri.id FROM generation_result_images_new gri
                    JOIN generation_result_sets grs ON grs.id = gri.result_set_id
                    WHERE grs.task_id = ?
                  )
            ORDER BY ga.created_at DESC
            """,
            (task_id, task_id, task_id),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "ownerType": row["owner_type"],
            "ownerId": row["owner_id"],
            "provider": row["provider"],
            "status": row["status"],
            "startedAt": row["started_at"],
            "completedAt": row["completed_at"],
            "failedAt": row["failed_at"],
            "errorMessage": row["error_message"],
            "requestSnapshot": loads(row["request_snapshot_json"], {}),
            "responseSnapshot": loads(row["response_snapshot_json"], {}),
        }
        for row in rows
    ]


def upload_media_metadata(key: str | Path, content: bytes, derivative: dict[str, Any]) -> dict[str, Any]:
    return {
        "fileSize": len(content),
        "thumbPath": derivative.get("thumb"),
        "previewPath": derivative.get("preview"),
        "thumbFileSize": derivative.get("thumbSize"),
        "previewFileSize": derivative.get("previewSize"),
    }


def provider_media_metadata(payload: dict[str, Any], fallback_key: str | Path | None) -> dict[str, Any]:
    output_path = payload.get("outputPath") or fallback_key
    return {
        "outputPath": clean_key(output_path) if output_path else None,
        "thumbPath": payload.get("thumbPath"),
        "previewPath": payload.get("previewPath"),
        "fileSize": payload.get("fileSize"),
        "thumbFileSize": payload.get("thumbFileSize"),
        "previewFileSize": payload.get("previewFileSize"),
    }


def serialize_sku(row: Any) -> dict[str, Any]:
    cover_path = row.keys() and row["cover_path"] if "cover_path" in row.keys() else None
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
        "coverUrl": stable_media_url(cover_path, "thumb"),
        "coverThumbUrl": stable_media_url(cover_path, "thumb"),
        "coverPreviewUrl": stable_media_url(cover_path, "preview"),
        "coverOriginalUrl": stable_media_url(cover_path, "original"),
        "coverFileSize": None,
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
    return {
        "id": row["id"],
        "filename": row["filename"],
        "role": row["role"],
        "sortOrder": row["sort_order"],
        "mimeType": row["mime_type"],
        "url": stable_media_url(original_path, "preview"),
        "thumbUrl": stable_media_url(original_path, "thumb"),
        "previewUrl": stable_media_url(original_path, "preview"),
        "originalUrl": stable_media_url(original_path, "original"),
        "fileSize": row["file_size"] if "file_size" in row.keys() else None,
        "thumbFileSize": row["thumb_file_size"] if "thumb_file_size" in row.keys() else None,
        "previewFileSize": row["preview_file_size"] if "preview_file_size" in row.keys() else None,
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
    reference_meta = {angle: {"fileSize": None} for angle, filename in references.items() if filename}
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


def serialize_generation_run(
    row: Any,
    include_images: bool,
    include_summary: bool = False,
    include_debug: bool = False,
    status_only: bool = False,
) -> dict[str, Any]:
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
                        "url": stable_media_url(preview["output_path"], "thumb"),
                        "thumbUrl": stable_media_url(preview["output_path"], "thumb"),
                        "previewUrl": stable_media_url(preview["output_path"], "preview"),
                        "originalUrl": stable_media_url(preview["output_path"], "original"),
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
            serializer = serialize_generation_image_status if status_only else serialize_generation_image
            item["images"] = [serializer(image) if status_only else serializer(image, include_debug=include_debug) for image in images]
    return item


def serialize_generation_image_status(row: Any) -> dict[str, Any]:
    displayable = has_displayable_generation_output(row)
    original_path = row["output_path"] if displayable else None
    return {
        "id": row["id"],
        "runId": row["run_id"],
        "shotIndex": row["shot_index"],
        "title": row["title"],
        "status": row["status"],
        "objectKey": row["output_path"],
        "url": stable_media_url(original_path, "preview") if displayable else None,
        "thumbUrl": stable_media_url(original_path, "thumb") if displayable else None,
        "previewUrl": stable_media_url(original_path, "preview") if displayable else None,
        "originalUrl": stable_media_url(original_path, "original") if displayable else None,
        "fileSize": row["file_size"] if "file_size" in row.keys() else None,
        "thumbFileSize": row["thumb_file_size"] if "thumb_file_size" in row.keys() else None,
        "previewFileSize": row["preview_file_size"] if "preview_file_size" in row.keys() else None,
        "currentAttemptIndex": row["current_attempt_index"] if "current_attempt_index" in row.keys() else 0,
        "providerStatus": row["provider_status"] if "provider_status" in row.keys() else None,
        "submittedAt": row["submitted_at"] if "submitted_at" in row.keys() else None,
        "completedAt": row["completed_at"] if "completed_at" in row.keys() else None,
        "lastStatusCheckedAt": row["last_status_checked_at"] if "last_status_checked_at" in row.keys() else None,
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


def serialize_generation_image(row: Any, include_debug: bool = False) -> dict[str, Any]:
    displayable = has_displayable_generation_output(row)
    original_path = row["output_path"] if displayable else None
    item = {
        "id": row["id"],
        "runId": row["run_id"],
        "shotIndex": row["shot_index"],
        "title": row["title"],
        "status": row["status"],
        "objectKey": row["output_path"],
        "url": stable_media_url(original_path, "preview") if displayable else None,
        "thumbUrl": stable_media_url(original_path, "thumb") if displayable else None,
        "previewUrl": stable_media_url(original_path, "preview") if displayable else None,
        "originalUrl": stable_media_url(original_path, "original") if displayable else None,
        "fileSize": row["file_size"] if "file_size" in row.keys() else None,
        "thumbFileSize": row["thumb_file_size"] if "thumb_file_size" in row.keys() else None,
        "previewFileSize": row["preview_file_size"] if "preview_file_size" in row.keys() else None,
        "prompt": row["prompt_text"] if "prompt_text" in row.keys() else None,
        "currentAttemptIndex": row["current_attempt_index"] if "current_attempt_index" in row.keys() else 0,
        "providerRequestId": row["provider_request_id"] if "provider_request_id" in row.keys() else None,
        "providerStatus": row["provider_status"] if "provider_status" in row.keys() else None,
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
    if include_debug:
        item.update({
            "taskPackage": loads(row["task_package_json"], {}),
            "providerRequest": loads(row["provider_request_json"], None),
            "providerResponse": loads(row["provider_response_json"], None),
            "imageInputs": loads(row["image_inputs_json"], []) if "image_inputs_json" in row.keys() else [],
            "templateSnapshot": loads(row["template_snapshot_json"], None) if "template_snapshot_json" in row.keys() else None,
            "modelSnapshot": loads(row["model_snapshot_json"], None) if "model_snapshot_json" in row.keys() else None,
            "garmentSnapshot": loads(row["garment_snapshot_json"], None) if "garment_snapshot_json" in row.keys() else None,
            "attempts": loads(row["attempts_json"], []) if "attempts_json" in row.keys() else [],
            "providerUrls": loads(row["provider_urls_json"], {}) if "provider_urls_json" in row.keys() else {},
            "providerStatusResponse": loads(row["provider_status_json"], None) if "provider_status_json" in row.keys() else None,
        })
    return item


def has_displayable_generation_output(row: Any) -> bool:
    output_path = row["output_path"] if "output_path" in row.keys() else None
    return row["status"] == "SUCCEEDED" and bool(output_path)


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
    return stable_media_url(key, variant)


def remove_model_asset_file(asset_dir: str, filename: str | None) -> None:
    if not filename:
        return
    cleanup_objects(image_object_keys(join_key(asset_dir, sanitize_filename(filename))))
