from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import secrets
import sqlite3
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
from pydantic import BaseModel, Field

from .config import get_settings
from .db import dumps, get_db, init_db, loads, log_event, now_iso
from .image_derivatives import derivative_key, existing_derivative_or_original, generate_image_derivatives
from .image_provider import ImageProviderError, get_image_provider
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
logger = logging.getLogger(__name__)
Image.MAX_IMAGE_PIXELS = settings.max_upload_image_pixels
SESSION_ROLES = {"user", "admin"}
SHOOT_CONFIG_EXCLUDED_CATEGORY_CODES = {"未知", "不支持入库配饰", "unknown", "unsupported_accessory"}
SYSTEM_HARD_EXCLUDED_CANDIDATE_CATEGORIES = ("未知", "不支持入库配饰")
PROMPT_PLACEHOLDER_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_.-]+)\s*}}")
AI_STYLING_PROMPT_CONFIG_KEY = "assembly.ai_styling"
SHOOT_SCRIPT_PROMPT_CONFIG_KEY = "assembly.shoot_script"


def is_request_authenticated(request: Request) -> bool:
    return get_request_role(request) is not None


def require_admin(request: Request) -> None:
    if get_request_role(request) != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")


def get_request_role(request: Request) -> str | None:
    if settings.trust_reverse_proxy_auth or is_api_token_valid(request):
        return "admin"
    if not settings.is_gate_enabled():
        return "admin"
    return get_session_cookie_role(request.cookies.get("ruyi_session", ""))


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


def is_fixed_password_valid(password: str, expected_password: str, expected_hash: str) -> bool:
    if expected_password and secrets.compare_digest(password, expected_password):
        return True
    if expected_hash:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return secrets.compare_digest(digest, expected_hash)
    return False


def authenticate_fixed_account(username: str | None, password: str) -> str | None:
    account = (username or settings.user_username).strip().lower()
    user_username = settings.user_username.strip().lower()
    admin_username = settings.admin_username.strip().lower()
    if account == user_username and is_fixed_password_valid(password, settings.user_password, settings.user_password_hash):
        return "user"
    if (
        account == admin_username
        and settings.is_admin_account_enabled()
        and is_fixed_password_valid(password, settings.admin_password, settings.admin_password_hash)
    ):
        return "admin"
    return None


def sign_session_payload(payload: str) -> str:
    return hmac.new(settings.effective_session_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_cookie(role: str) -> str:
    if role not in SESSION_ROLES:
        raise ValueError("Unsupported session role.")
    expires_at = int(time.time() + settings.session_ttl_seconds)
    payload = f"{expires_at}.{role}.{secrets.token_urlsafe(18)}"
    return f"{payload}.{sign_session_payload(payload)}"


def get_session_cookie_role(cookie: str) -> str | None:
    parts = cookie.split(".")
    if len(parts) != 4:
        return None
    expires_at, role, nonce, signature = parts
    if not expires_at.isdigit() or not nonce:
        return None
    if role not in SESSION_ROLES:
        return None
    payload = f"{expires_at}.{role}.{nonce}"
    if not secrets.compare_digest(signature, sign_session_payload(payload)):
        return None
    return role if int(expires_at) > int(time.time()) else None


def is_session_cookie_valid(cookie: str) -> bool:
    return get_session_cookie_role(cookie) is not None


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


class StylingGarmentBatchTagRequest(BaseModel):
    ids: list[int]


class StylingGarmentDeleteRequest(BaseModel):
    ids: list[int] | None = None
    purge: bool = False
    confirmPhrase: str | None = None


class StylingGarmentImageUpdateRequest(BaseModel):
    sortOrder: int | None = None


class GenerationTaskUpdateRequest(BaseModel):
    name: str | None = None


class GenerationTaskImageUpdateRequest(BaseModel):
    sortOrder: int | None = None


class GenerationTaskDeleteRequest(BaseModel):
    confirmName: str | None = None
    confirmPhrase: str | None = None
    ids: list[int] | None = None
    purge: bool = False


class DeleteImpactRequest(BaseModel):
    ids: list[int]


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


class BrandInfoUpsertRequest(BaseModel):
    name: str | None = None
    tagline: str | None = None
    brandCore: str | None = None
    styleDna: str | None = None
    stylingGuidelines: str | None = None
    shootingGuidelines: str | None = None
    negativeConstraints: str | None = None


class ShootConfigUpdateRequest(BaseModel):
    config: dict[str, Any]


class ShootConfigImportRequest(BaseModel):
    config: dict[str, Any]


class TaxonomyDimensionUpdateRequest(BaseModel):
    labelZh: str | None = None
    descriptionZh: str | None = None
    sortOrder: int | None = None
    isActive: bool | None = None


class TaxonomyDimensionCreateRequest(TaxonomyDimensionUpdateRequest):
    code: str
    labelZh: str


class TaxonomyTagCreateRequest(BaseModel):
    dimensionCode: str
    code: str
    labelZh: str
    descriptionZh: str | None = None
    parentCode: str | None = None
    level: int | None = None
    valueKind: str | None = None
    isSelectable: bool | None = None
    isActive: bool | None = None
    sortOrder: int | None = None
    metadata: dict[str, Any] | None = None


class TaxonomyTagUpdateRequest(BaseModel):
    labelZh: str | None = None
    descriptionZh: str | None = None
    parentCode: str | None = None
    level: int | None = None
    valueKind: str | None = None
    isSelectable: bool | None = None
    isActive: bool | None = None
    sortOrder: int | None = None
    metadata: dict[str, Any] | None = None


class TaxonomyRelationCreateRequest(BaseModel):
    fromDimensionCode: str
    fromTagCode: str
    relationType: str
    toDimensionCode: str
    toTagCode: str
    weight: float | None = None
    descriptionZh: str | None = None
    isActive: bool | None = None


class TaxonomyRelationUpdateRequest(BaseModel):
    fromDimensionCode: str | None = None
    fromTagCode: str | None = None
    relationType: str | None = None
    toDimensionCode: str | None = None
    toTagCode: str | None = None
    weight: float | None = None
    descriptionZh: str | None = None
    isActive: bool | None = None


class TaxonomyApplicabilityCreateRequest(BaseModel):
    categoryDimensionCode: str = "category"
    categoryTagCode: str
    targetDimensionCode: str
    targetTagCode: str
    mode: str | None = None
    appliesToDescendants: bool | None = None
    note: str | None = None
    isActive: bool | None = None


class TaxonomyApplicabilityUpdateRequest(BaseModel):
    categoryDimensionCode: str | None = None
    categoryTagCode: str | None = None
    targetDimensionCode: str | None = None
    targetTagCode: str | None = None
    mode: str | None = None
    appliesToDescendants: bool | None = None
    note: str | None = None
    isActive: bool | None = None


class TaxonomyImportRequest(BaseModel):
    dimensions: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    applicabilityRules: list[dict[str, Any]] = Field(default_factory=list)


class PromptConfigUpdateRequest(BaseModel):
    content: str


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
RUNNING_GENERATION_TASK_STATUSES = {"STYLING_RUNNING", "SCRIPT_RUNNING", "PREVIEW_RUNNING", "FINAL_RUNNING"}
PLANNING_STATUSES = {"PLANNING"}
REVIEW_STATUSES = {"candidate", "approved", "needs_regen", "needs_retouch", "delivered"}
TAXONOMY_TAGGING_SCHEMA_VERSION = "ruyi-taxonomy-tagging-v1"
PROMPT_TAGGING_RESULT_VERSION = "prompt-configured-json-v1"
TAG_REVIEW_CONFIDENCE_THRESHOLD = 0.72
TAXONOMY_REVIEW_REASONS = {
    "low_confidence",
    "conflicting_images",
    "unclear_category",
    "title_image_conflict",
    "insufficient_images",
    "unsupported_accessory",
    "uncertain_material",
    "uncertain_color",
    "uncertain_fit",
    "fallback_tags",
    "unmapped_observation",
    "invalid_tag_reference",
    "missing_required_rule",
    "disallowed_tag",
    "empty_result",
}
LOGIN_FAILURE_WINDOW_SECONDS = 300
LOGIN_FAILURE_LIMIT = 8
login_failures_by_client: dict[str, list[float]] = {}
ALLOWED_UPLOAD_FORMATS = {"JPEG", "PNG", "WEBP"}
active_background_generation_jobs: set[str] = set()

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
        "imageProviderConfigured": settings.is_image_provider_configured(),
    }


@app.get("/api/auth/session")
def get_auth_session(request: Request) -> dict[str, bool | str | None]:
    role = get_request_role(request)
    return {
        "authRequired": settings.is_gate_enabled() and not settings.trust_reverse_proxy_auth,
        "authenticated": role is not None,
        "role": role,
    }


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request) -> JSONResponse:
    if not settings.is_gate_enabled():
        return JSONResponse({"authRequired": False, "authenticated": True, "role": "admin"})
    if is_login_rate_limited(request):
        return JSONResponse(status_code=429, content={"detail": "登录尝试过多，请稍后再试"})
    role = authenticate_fixed_account(payload.username, payload.password)
    if role is None:
        record_login_failure(request)
        return JSONResponse(status_code=401, content={"detail": "用户名或密码不正确"})
    clear_login_failures(request)
    response = JSONResponse({"authRequired": True, "authenticated": True, "role": role})
    response.set_cookie(
        "ruyi_session",
        create_session_cookie(role),
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


def media_cleanup_prefixes(object_keys: list[str]) -> list[str]:
    prefixes: list[str] = []
    for object_key in object_keys:
        parts = clean_key(object_key).split("/")
        if len(parts) >= 2 and parts[0] in {"generation-tasks", "styling-garments", "uploads", "outputs"}:
            prefixes.append("/".join(parts[:2]))
    return list(dict.fromkeys(prefixes))


def media_asset_snapshot_reference_count(db: Any, object_key: str) -> int:
    pattern = f"%{object_key}%"
    row = db.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM generation_task_input_versions WHERE image_snapshot_json LIKE ?) +
          (SELECT COUNT(*) FROM generation_stylings WHERE selected_material_snapshot_json LIKE ?) +
          (SELECT COUNT(*) FROM generation_attempts WHERE request_snapshot_json LIKE ? OR response_snapshot_json LIKE ?) AS count
        """,
        (pattern, pattern, pattern, pattern),
    ).fetchone()
    return int(row["count"] or 0)


def media_asset_direct_reference_count(db: Any, asset_id: int) -> int:
    row = db.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM styling_garment_images WHERE media_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_task_images WHERE media_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_previews WHERE preview_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_result_images_new WHERE current_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_attempts WHERE output_asset_id = ?) AS count
        """,
        (asset_id, asset_id, asset_id, asset_id, asset_id),
    ).fetchone()
    return int(row["count"] or 0)


def release_media_asset_if_unreferenced(db: Any, asset_id: int | None) -> bool:
    if not asset_id:
        return False
    row = db.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
    if not row:
        return False
    if media_asset_direct_reference_count(db, asset_id) > 0:
        return False
    object_key = clean_key(row["object_key"])
    if media_asset_snapshot_reference_count(db, object_key) > 0:
        return False
    cleanup_objects(image_object_keys(object_key))
    db.execute("DELETE FROM media_assets WHERE id = ?", (asset_id,))
    return True


def media_asset_rows_for_ids(db: Any, asset_ids: list[int]) -> list[Any]:
    asset_ids = list(dict.fromkeys(int(asset_id) for asset_id in asset_ids if asset_id))
    if not asset_ids:
        return []
    placeholders = ", ".join("?" for _ in asset_ids)
    return db.execute(f"SELECT id, object_key, size_bytes FROM media_assets WHERE id IN ({placeholders})", asset_ids).fetchall()


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
    include_archived: bool = Query(False, alias="includeArchived"),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    keyword: str | None = Query(None),
) -> dict[str, Any]:
    expire_stale_styling_garment_tagging()
    search = f"%{keyword.strip()}%" if keyword and keyword.strip() else None
    with get_db() as db:
        rows = db.execute(
            """
            SELECT sg.*,
              (SELECT ma.id FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_asset_id,
              (SELECT ma.object_key FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_key,
              (SELECT ma.original_filename FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_filename,
              (SELECT ma.mime_type FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_mime_type,
              (SELECT ma.width FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_width,
              (SELECT ma.height FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_height,
              (SELECT ma.size_bytes FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_size_bytes,
              (SELECT COUNT(*) FROM styling_garment_images sgi WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL) AS image_count
            FROM styling_garments sg
            WHERE sg.deleted_at IS NULL
              AND (? OR sg.archived_at IS NULL)
              AND (? IS NULL OR sg.code LIKE ? OR sg.status LIKE ? OR sg.ai_tags_json LIKE ?)
            ORDER BY sg.created_at DESC, sg.id DESC
            LIMIT ? OFFSET ?
            """,
            (include_archived, search, search, search, search, limit + 1, offset),
        ).fetchall()
        total = int(db.execute(
            """
            SELECT COUNT(*) AS total
            FROM styling_garments sg
            WHERE sg.deleted_at IS NULL
              AND (? OR sg.archived_at IS NULL)
              AND (? IS NULL OR sg.code LIKE ? OR sg.status LIKE ? OR sg.ai_tags_json LIKE ?)
            """,
            (include_archived, search, search, search, search),
        ).fetchone()["total"])
        items = rows[:limit]
        return {
            "items": [serialize_styling_garment(row, include_images=False) for row in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if offset + limit < total else None,
            "hasMore": offset + limit < total,
            "total": total,
            "totalPages": (total + limit - 1) // limit if total else 0,
        }


@app.get("/api/styling-garments/{garment_id}")
def get_styling_garment(garment_id: int) -> dict[str, Any]:
    expire_stale_styling_garment_tagging()
    return {"item": get_styling_garment_by_id(garment_id)}


def ensure_styling_garment_not_tagging(row: Any) -> None:
    if row["status"] == "TAGGING_RUNNING":
        raise HTTPException(status_code=409, detail="正在打标，完成后才能修改或删除搭配服饰")


def expire_stale_styling_garment_tagging() -> None:
    timeout_before = (
        datetime.fromtimestamp(time.time() - settings.generation_image_timeout_seconds, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    timestamp = now_iso()
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id FROM styling_garments
            WHERE status = 'TAGGING_RUNNING'
              AND updated_at < ?
              AND deleted_at IS NULL
            """,
            (timeout_before,),
        ).fetchall()
        if not rows:
            return
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        message = "打标任务长时间未完成，已自动停止轮询，可手动重新打标。"
        db.execute(
            f"""
            UPDATE styling_garments
            SET status = 'TAGGING_FAILED', tag_error = ?, updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (message, timestamp, *ids),
        )
        db.execute(
            f"""
            UPDATE generation_attempts
            SET status = 'FAILED', failed_at = ?, error_code = 'TIMEOUT', error_message = ?, updated_at = ?
            WHERE owner_type = 'styling_garment_tagging'
              AND owner_id IN ({placeholders})
              AND status = 'RUNNING'
            """,
            (timestamp, message, timestamp, *ids),
        )
        for garment_id in ids:
            log_event(db, "styling_garment", garment_id, "tagging_timed_out", {"timeoutAt": timestamp})


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
    with get_db() as db:
        duplicate = db.execute("SELECT id FROM styling_garments WHERE code = ? AND deleted_at IS NULL", (clean_code,)).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail="搭配服饰代码已存在")
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
            (clean_code, dumps({}), dumps({}), PROMPT_TAGGING_RESULT_VERSION, timestamp, timestamp),
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
        replace_garment_tag_assignments(
            db,
            garment_id,
            loads(row["ai_tags_json"], {}),
            loads(row["official_title_tags_json"], {}),
            manual_tags,
            loads(row["tag_meta_json"], {}) if "tag_meta_json" in row.keys() else {},
        )
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


@app.post("/api/styling-garments/batch-save-and-tag")
async def batch_save_and_tag_styling_garments(payload: StylingGarmentBatchTagRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    garment_ids = list(dict.fromkeys(payload.ids))
    if not garment_ids:
        raise HTTPException(status_code=400, detail="至少选择一套搭配服饰")
    timestamp = now_iso()
    with get_db() as db:
        placeholders = ",".join("?" for _ in garment_ids)
        rows = db.execute(
            f"SELECT * FROM styling_garments WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            tuple(garment_ids),
        ).fetchall()
        rows_by_id = {int(row["id"]): row for row in rows}
        missing = [garment_id for garment_id in garment_ids if garment_id not in rows_by_id]
        if missing:
            raise HTTPException(status_code=404, detail="搭配服饰不存在")
        for garment_id in garment_ids:
            ensure_styling_garment_not_tagging(rows_by_id[garment_id])
        db.execute(
            f"UPDATE styling_garments SET status = 'TAGGING_RUNNING', tag_error = NULL, updated_at = ? WHERE id IN ({placeholders})",
            (timestamp, *garment_ids),
        )
    for garment_id in garment_ids:
        background_tasks.add_task(run_styling_garment_tagging, garment_id)
    return {"items": [get_styling_garment_by_id(garment_id) for garment_id in garment_ids]}


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
        ai_tags = tagging_result.get("ai_tags") if isinstance(tagging_result.get("ai_tags"), dict) else tagging_result
        official_title_tags = tagging_result.get("official_title_tags", {}) if isinstance(tagging_result.get("official_title_tags"), dict) else {}
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
                tag_error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                dumps(ai_tags),
                dumps(official_title_tags),
                dumps(effective_tags),
                dumps(tag_meta),
                tagging_result.get("schema_version", PROMPT_TAGGING_RESULT_VERSION),
                timestamp,
                timestamp,
                garment_id,
            ),
        )
        replace_garment_tag_assignments(db, garment_id, ai_tags, official_title_tags, manual_tags, tag_meta)
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
                   ma.id AS previous_media_asset_id,
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
        release_media_asset_if_unreferenced(db, int(row["previous_media_asset_id"]))
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
            """
            SELECT sgi.*, sg.status AS garment_status
            FROM styling_garment_images sgi
            JOIN styling_garments sg ON sg.id = sgi.styling_garment_id
            WHERE sgi.id = ? AND sgi.deleted_at IS NULL AND sg.deleted_at IS NULL
            """,
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
        media_asset_id = int(row["media_asset_id"])
        db.execute("DELETE FROM styling_garment_images WHERE id = ?", (image_id,))
        release_media_asset_if_unreferenced(db, media_asset_id)
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
        if purge:
            deleted_task_ids, cleanup_errors = purge_styling_garment(db, garment_id, row["code"])
            log_event(db, "styling_garment", garment_id, "purged", {"code": row["code"], "deletedTaskIds": deleted_task_ids})
            return {"ok": True, "purged": True, "deletedTaskIds": deleted_task_ids, "objectCleanupErrors": cleanup_errors}
        archive_styling_garments(db, [garment_id], "user_archived")
        log_event(db, "styling_garment", garment_id, "archived", {"code": row["code"]})
    return {"ok": True, "archived": True}


@app.post("/api/styling-garments/batch-delete")
def delete_styling_garments(payload: StylingGarmentDeleteRequest) -> dict[str, Any]:
    garment_ids = normalized_styling_garment_ids(payload.ids or [])
    with get_db() as db:
        rows = get_styling_garment_rows_for_mutation(db, garment_ids)
        for row in rows:
            ensure_styling_garment_not_tagging(row)
        if payload.purge:
            if payload.confirmPhrase != "全部删除":
                raise HTTPException(status_code=400, detail="请输入“全部删除”以确认批量删除")
            deleted_task_ids: list[int] = []
            cleanup_errors: list[dict[str, str]] = []
            for row in rows:
                task_ids, errors = purge_styling_garment(db, int(row["id"]), row["code"])
                deleted_task_ids.extend(task_ids)
                cleanup_errors.extend(errors)
                log_event(db, "styling_garment", int(row["id"]), "purged", {"code": row["code"], "batch": True, "deletedTaskIds": task_ids})
            return {
                "ok": True,
                "purged": True,
                "deletedGarmentIds": garment_ids,
                "deletedTaskIds": list(dict.fromkeys(deleted_task_ids)),
                "objectCleanupErrors": cleanup_errors,
            }
        archive_styling_garments(db, garment_ids, "user_batch_archived")
        for row in rows:
            log_event(db, "styling_garment", int(row["id"]), "archived", {"code": row["code"], "batch": True})
    return {"ok": True, "archived": True, "ids": garment_ids}


def normalized_styling_garment_ids(ids: list[int]) -> list[int]:
    normalized = list(dict.fromkeys(int(garment_id) for garment_id in ids if int(garment_id) > 0))
    if not normalized:
        raise HTTPException(status_code=400, detail="请选择至少一套搭配服饰")
    if len(normalized) > 100:
        raise HTTPException(status_code=400, detail="单次最多处理 100 套搭配服饰")
    return normalized


def get_styling_garment_rows_for_mutation(db: Any, garment_ids: list[int]) -> list[Any]:
    placeholders = ", ".join("?" for _ in garment_ids)
    rows = db.execute(
        f"SELECT * FROM styling_garments WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        garment_ids,
    ).fetchall()
    if len(rows) != len(garment_ids):
        raise HTTPException(status_code=404, detail="部分搭配服饰不存在")
    return rows


def dependent_task_ids_for_styling_garments(db: Any, garment_ids: list[int]) -> list[int]:
    target_ids = {int(garment_id) for garment_id in garment_ids}
    styling_rows = db.execute("SELECT task_id, selected_material_ids_json FROM generation_stylings").fetchall()
    return list(dict.fromkeys(
        int(styling["task_id"])
        for styling in styling_rows
        if target_ids.intersection({int(item) for item in loads(styling["selected_material_ids_json"], [])})
    ))


def generation_task_delete_impact(db: Any, task_ids: list[int]) -> dict[str, Any]:
    task_ids = normalized_task_ids(task_ids)
    placeholders = ", ".join("?" for _ in task_ids)
    row = db.execute(
        f"""
        SELECT
          (SELECT COUNT(*) FROM generation_tasks WHERE id IN ({placeholders}) AND deleted_at IS NULL) AS task_count,
          (SELECT COUNT(*) FROM generation_task_images WHERE task_id IN ({placeholders})) AS task_image_count,
          (SELECT COUNT(*) FROM generation_task_input_versions WHERE task_id IN ({placeholders})) AS input_version_count,
          (SELECT COUNT(*) FROM generation_stylings WHERE task_id IN ({placeholders})) AS styling_count,
          (SELECT COUNT(*) FROM generation_shoot_scripts WHERE task_id IN ({placeholders})) AS shoot_script_count,
          (SELECT COUNT(*) FROM generation_shot_plans WHERE task_id IN ({placeholders})) AS shot_plan_count,
          (SELECT COUNT(*) FROM generation_previews WHERE task_id IN ({placeholders})) AS preview_count,
          (SELECT COUNT(*) FROM generation_result_sets WHERE task_id IN ({placeholders})) AS result_set_count,
          (SELECT COUNT(*) FROM generation_result_images_new gri JOIN generation_result_sets grs ON grs.id = gri.result_set_id WHERE grs.task_id IN ({placeholders})) AS result_image_count
        """,
        (*task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids),
    ).fetchone()
    asset_rows = db.execute(
        f"""
        SELECT media_asset_id AS asset_id FROM generation_task_images WHERE task_id IN ({placeholders})
        UNION
        SELECT preview_asset_id AS asset_id FROM generation_previews WHERE task_id IN ({placeholders}) AND preview_asset_id IS NOT NULL
        UNION
        SELECT gri.current_asset_id AS asset_id FROM generation_result_images_new gri
        JOIN generation_result_sets grs ON grs.id = gri.result_set_id
        WHERE grs.task_id IN ({placeholders}) AND gri.current_asset_id IS NOT NULL
        UNION
        SELECT output_asset_id AS asset_id FROM generation_attempts
        WHERE output_asset_id IS NOT NULL AND (
             (owner_type = 'generation_styling' AND owner_id IN ({placeholders}))
             OR (owner_type = 'generation_shoot_script' AND owner_id IN (
                  SELECT id FROM generation_shoot_scripts WHERE task_id IN ({placeholders})
                ))
             OR (owner_type = 'generation_preview' AND owner_id IN (
                  SELECT id FROM generation_previews WHERE task_id IN ({placeholders})
                ))
             OR (owner_type = 'generation_result_image' AND owner_id IN (
                  SELECT gri.id FROM generation_result_images_new gri
                  JOIN generation_result_sets grs ON grs.id = gri.result_set_id
                  WHERE grs.task_id IN ({placeholders})
                ))
        )
        """,
        (*task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids),
    ).fetchall()
    asset_ids = list(dict.fromkeys(int(asset["asset_id"]) for asset in asset_rows if asset["asset_id"] is not None))
    media_rows = media_asset_rows_for_ids(db, asset_ids)
    total_bytes = sum(int(media["size_bytes"] or 0) for media in media_rows if "size_bytes" in media.keys())
    return {
        "taskCount": int(row["task_count"] or 0),
        "taskImageCount": int(row["task_image_count"] or 0),
        "inputVersionCount": int(row["input_version_count"] or 0),
        "stylingCount": int(row["styling_count"] or 0),
        "shootScriptCount": int(row["shoot_script_count"] or 0),
        "shotPlanCount": int(row["shot_plan_count"] or 0),
        "previewCount": int(row["preview_count"] or 0),
        "resultSetCount": int(row["result_set_count"] or 0),
        "resultImageCount": int(row["result_image_count"] or 0),
        "mediaAssetCount": len(media_rows),
        "estimatedBytes": total_bytes,
    }


def styling_garment_delete_impact(db: Any, garment_ids: list[int]) -> dict[str, Any]:
    garment_ids = normalized_styling_garment_ids(garment_ids)
    get_styling_garment_rows_for_mutation(db, garment_ids)
    placeholders = ", ".join("?" for _ in garment_ids)
    image_rows = db.execute(
        f"""
        SELECT ma.id, ma.object_key, ma.size_bytes
        FROM styling_garment_images sgi
        JOIN media_assets ma ON ma.id = sgi.media_asset_id
        WHERE sgi.styling_garment_id IN ({placeholders})
        """,
        garment_ids,
    ).fetchall()
    dependent_task_ids = dependent_task_ids_for_styling_garments(db, garment_ids)
    task_impact = generation_task_delete_impact(db, dependent_task_ids) if dependent_task_ids else {
        "taskCount": 0,
        "taskImageCount": 0,
        "inputVersionCount": 0,
        "stylingCount": 0,
        "shotPlanCount": 0,
        "previewCount": 0,
        "resultSetCount": 0,
        "resultImageCount": 0,
        "mediaAssetCount": 0,
        "estimatedBytes": 0,
    }
    total_bytes = sum(int(row["size_bytes"] or 0) for row in image_rows) + int(task_impact["estimatedBytes"] or 0)
    return {
        "garmentCount": len(garment_ids),
        "garmentImageCount": len(image_rows),
        "dependentTaskIds": dependent_task_ids,
        "dependentTaskCount": len(dependent_task_ids),
        "mediaAssetCount": len(image_rows) + int(task_impact["mediaAssetCount"] or 0),
        "estimatedBytes": total_bytes,
        "dependentTaskImpact": task_impact,
    }


@app.post("/api/styling-garments/delete-impact")
def get_styling_garments_delete_impact(payload: DeleteImpactRequest) -> dict[str, Any]:
    with get_db() as db:
        return {"impact": styling_garment_delete_impact(db, payload.ids)}


def archive_styling_garments(db: Any, garment_ids: list[int], reason: str) -> None:
    garment_ids = normalized_styling_garment_ids(garment_ids)
    timestamp = now_iso()
    placeholders = ", ".join("?" for _ in garment_ids)
    db.execute(
        f"""
        UPDATE styling_garments
        SET archived_at = COALESCE(archived_at, ?),
            archive_reason = COALESCE(archive_reason, ?),
            updated_at = ?
        WHERE id IN ({placeholders})
        """,
        (timestamp, reason, timestamp, *garment_ids),
    )


def purge_styling_garment(db: Any, garment_id: int, code: str) -> tuple[list[int], list[dict[str, str]]]:
    image_rows = db.execute(
        """
        SELECT ma.id, ma.object_key FROM styling_garment_images sgi
        JOIN media_assets ma ON ma.id = sgi.media_asset_id
        WHERE sgi.styling_garment_id = ?
        """,
        (garment_id,),
    ).fetchall()
    dependent_task_ids = dependent_task_ids_for_styling_garments(db, [garment_id])
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
            WHERE (owner_type = 'generation_preview' AND owner_id IN (
                    SELECT id FROM generation_previews WHERE task_id IN ({placeholders})
                  ))
               OR (owner_type = 'generation_result_image' AND owner_id IN (
                    SELECT gri.id FROM generation_result_images_new gri
                    JOIN generation_result_sets grs ON grs.id = gri.result_set_id
                    WHERE grs.task_id IN ({placeholders})
                  ))
            """,
            (*dependent_task_ids, *dependent_task_ids),
        )
        db.execute(
            f"DELETE FROM generation_attempts WHERE owner_type = 'generation_styling' AND owner_id IN ({placeholders})",
            dependent_task_ids,
        )
        if dependent_asset_ids:
            asset_placeholders = ", ".join("?" for _ in dependent_asset_ids)
            db.execute(
                f"DELETE FROM generation_attempts WHERE output_asset_id IN ({asset_placeholders})",
                dependent_asset_ids,
            )
        db.execute(f"DELETE FROM generation_tasks WHERE id IN ({placeholders})", dependent_task_ids)
    asset_ids = [int(image["id"]) for image in image_rows] + dependent_asset_ids
    dependent_media_rows = media_asset_rows_for_ids(db, dependent_asset_ids)
    object_keys = [media["object_key"] for media in [*image_rows, *dependent_media_rows]]
    cleanup_objects([
        key
        for media in [*image_rows, *dependent_media_rows]
        for key in image_object_keys(media["object_key"])
    ])
    db.execute("DELETE FROM styling_garments WHERE id = ?", (garment_id,))
    if asset_ids:
        placeholders = ", ".join("?" for _ in asset_ids)
        db.execute(f"DELETE FROM media_assets WHERE id IN ({placeholders})", asset_ids)
    cleanup_errors = cleanup_prefixes(media_cleanup_prefixes(object_keys))
    return dependent_task_ids, cleanup_errors


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
def list_generation_tasks(
    include_archived: bool = Query(False, alias="includeArchived"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM generation_tasks
            WHERE deleted_at IS NULL
              AND (? OR archived_at IS NULL)
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (include_archived, limit + 1, offset),
        ).fetchall()
        total = int(db.execute(
            """
            SELECT COUNT(*) AS total FROM generation_tasks
            WHERE deleted_at IS NULL
              AND (? OR archived_at IS NULL)
            """,
            (include_archived,),
        ).fetchone()["total"])
        items = rows[:limit]
        return {
            "items": [serialize_generation_task(row, include_detail=False) for row in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if offset + limit < total else None,
            "hasMore": offset + limit < total,
            "total": total,
            "totalPages": (total + limit - 1) // limit if total else 0,
        }


@app.get("/api/generation-tasks/{task_id}")
def get_generation_task(task_id: int, include_debug: bool = Query(False, alias="includeDebug")) -> dict[str, Any]:
    return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}


@app.get("/api/generation-tasks/{task_id}/ai-invocations")
def get_generation_task_ai_invocations(task_id: int, request: Request) -> dict[str, Any]:
    require_admin(request)
    get_generation_task_row(task_id)
    return {"items": get_task_attempts(task_id)}


@app.post("/api/generation-tasks/{task_id}/archive")
def archive_generation_task(task_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成任务不存在")
        ensure_generation_task_not_running(row)
        timestamp = now_iso()
        if row["archived_at"] is None:
            db.execute(
                "UPDATE generation_tasks SET archived_at = ?, archive_reason = ?, updated_at = ? WHERE id = ?",
                (timestamp, "user_archived", timestamp, task_id),
            )
            log_event(db, "generation_task", task_id, "archived", {"name": row["name"]})
    return {"ok": True, "archived": True, "item": get_generation_task_by_id(task_id)}


@app.post("/api/generation-tasks/batch-archive")
def archive_generation_tasks(payload: GenerationTaskDeleteRequest) -> dict[str, Any]:
    task_ids = normalized_task_ids(payload.ids or [])
    with get_db() as db:
        rows = get_generation_task_rows_for_mutation(db, task_ids)
        for row in rows:
            ensure_generation_task_not_running(row)
        timestamp = now_iso()
        placeholders = ", ".join("?" for _ in task_ids)
        db.execute(
            f"""
            UPDATE generation_tasks
            SET archived_at = COALESCE(archived_at, ?),
                archive_reason = COALESCE(archive_reason, 'user_archived'),
                updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (timestamp, timestamp, *task_ids),
        )
        for row in rows:
            log_event(db, "generation_task", int(row["id"]), "archived", {"name": row["name"], "batch": True})
    return {"ok": True, "archived": True, "ids": task_ids}


@app.post("/api/generation-tasks/{task_id}/cancel")
def cancel_generation_task(task_id: int) -> dict[str, Any]:
    timestamp = now_iso()
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成任务不存在")
        if row["status"] not in RUNNING_GENERATION_TASK_STATUSES:
            raise HTTPException(status_code=409, detail="只有生成中的任务可以取消")

        cancelled_preview_count = 0
        cancelled_image_count = 0
        if row["status"] == "SCRIPT_RUNNING":
            db.execute(
                """
                UPDATE generation_shoot_scripts
                SET status = 'CANCELLED', locked_by = NULL, locked_until = NULL, updated_at = ?
                WHERE task_id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                (timestamp, task_id),
            )
        elif row["status"] == "PREVIEW_RUNNING":
            preview_cursor = db.execute(
                """
                UPDATE generation_previews
                SET status = 'CANCELLED', locked_by = NULL, locked_until = NULL, updated_at = ?
                WHERE task_id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                (timestamp, task_id),
            )
            cancelled_preview_count = int(preview_cursor.rowcount or 0)
        elif row["status"] == "FINAL_RUNNING" and row["active_result_set_id"]:
            image_cursor = db.execute(
                """
                UPDATE generation_result_images_new
                SET status = 'CANCELLED', locked_by = NULL, locked_until = NULL, updated_at = ?
                WHERE result_set_id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                (timestamp, int(row["active_result_set_id"])),
            )
            cancelled_image_count = int(image_cursor.rowcount or 0)

        cancel_running_task_attempts(db, task_id, timestamp)
        if row["status"] == "FINAL_RUNNING" and row["active_result_set_id"]:
            reconcile_final_result_set(db, int(row["active_result_set_id"]), task_id)
        else:
            db.execute(
                "UPDATE generation_tasks SET status = 'CANCELLED', updated_at = ? WHERE id = ?",
                (timestamp, task_id),
            )
        log_event(
            db,
            "generation_task",
            task_id,
            "cancelled",
            {
                "previousStatus": row["status"],
                "cancelledPreviewCount": cancelled_preview_count,
                "cancelledImageCount": cancelled_image_count,
            },
        )
    return {"ok": True, "cancelled": True, "item": get_generation_task_by_id(task_id)}


@app.delete("/api/generation-tasks/{task_id}")
def delete_generation_task(task_id: int, payload: GenerationTaskDeleteRequest | None = None) -> dict[str, Any]:
    if not payload or not payload.purge:
        raise HTTPException(status_code=400, detail="删除任务必须勾选彻底删除")
    confirm_name = payload.confirmName or ""
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成任务不存在")
        ensure_generation_task_not_running(row)
        if confirm_name != row["name"]:
            raise HTTPException(status_code=400, detail="请输入完整任务名以确认彻底删除")
        cleanup_errors = purge_generation_tasks(db, [task_id], "user_purged")
    return {"ok": True, "purged": True, "deletedTaskIds": [task_id], "objectCleanupErrors": cleanup_errors}


@app.post("/api/generation-tasks/batch-purge")
def purge_generation_tasks_batch(payload: GenerationTaskDeleteRequest) -> dict[str, Any]:
    if not payload.purge:
        raise HTTPException(status_code=400, detail="删除任务必须勾选彻底删除")
    if payload.confirmPhrase != "全部删除":
        raise HTTPException(status_code=400, detail="请输入“全部删除”以确认批量删除")
    task_ids = normalized_task_ids(payload.ids or [])
    with get_db() as db:
        rows = get_generation_task_rows_for_mutation(db, task_ids)
        for row in rows:
            ensure_generation_task_not_running(row)
        cleanup_errors = purge_generation_tasks(db, task_ids, "user_batch_purged")
    return {"ok": True, "purged": True, "deletedTaskIds": task_ids, "objectCleanupErrors": cleanup_errors}


@app.post("/api/generation-tasks/delete-impact")
def get_generation_tasks_delete_impact(payload: DeleteImpactRequest) -> dict[str, Any]:
    with get_db() as db:
        get_generation_task_rows_for_mutation(db, normalized_task_ids(payload.ids))
        return {"impact": generation_task_delete_impact(db, payload.ids)}


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


def ensure_generation_task_not_running(task: Any) -> None:
    if task["status"] in RUNNING_GENERATION_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="任务正在生成中，请先取消生成")


def cancel_running_task_attempts(db: Any, task_id: int, timestamp: str) -> None:
    db.execute(
        """
        UPDATE generation_attempts
        SET status = 'CANCELLED', cancelled_at = COALESCE(cancelled_at, ?), updated_at = ?
        WHERE status = 'RUNNING'
          AND (
            (owner_type = 'generation_styling' AND owner_id = ?)
            OR (owner_type = 'generation_shoot_script' AND owner_id IN (
              SELECT id FROM generation_shoot_scripts WHERE task_id = ?
            ))
            OR (owner_type = 'generation_preview' AND owner_id IN (
              SELECT id FROM generation_previews WHERE task_id = ?
            ))
            OR (owner_type = 'generation_result_image' AND owner_id IN (
              SELECT gri.id
              FROM generation_result_images_new gri
              JOIN generation_result_sets grs ON grs.id = gri.result_set_id
              WHERE grs.task_id = ?
            ))
          )
        """,
        (timestamp, timestamp, task_id, task_id, task_id, task_id),
    )


def normalized_task_ids(ids: list[int]) -> list[int]:
    normalized = list(dict.fromkeys(int(task_id) for task_id in ids if int(task_id) > 0))
    if not normalized:
        raise HTTPException(status_code=400, detail="请选择至少一条任务")
    if len(normalized) > 100:
        raise HTTPException(status_code=400, detail="单次最多处理 100 条任务")
    return normalized


def get_generation_task_rows_for_mutation(db: Any, task_ids: list[int]) -> list[Any]:
    placeholders = ", ".join("?" for _ in task_ids)
    rows = db.execute(
        f"SELECT * FROM generation_tasks WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        task_ids,
    ).fetchall()
    if len(rows) != len(task_ids):
        raise HTTPException(status_code=404, detail="部分生成任务不存在")
    return rows


def purge_generation_tasks(db: Any, task_ids: list[int], reason: str) -> list[dict[str, str]]:
    task_ids = normalized_task_ids(task_ids)
    placeholders = ", ".join("?" for _ in task_ids)
    asset_rows = db.execute(
        f"""
        SELECT media_asset_id AS asset_id FROM generation_task_images WHERE task_id IN ({placeholders})
        UNION
        SELECT preview_asset_id AS asset_id FROM generation_previews WHERE task_id IN ({placeholders}) AND preview_asset_id IS NOT NULL
        UNION
        SELECT gri.current_asset_id AS asset_id FROM generation_result_images_new gri
        JOIN generation_result_sets grs ON grs.id = gri.result_set_id
        WHERE grs.task_id IN ({placeholders}) AND gri.current_asset_id IS NOT NULL
        UNION
        SELECT output_asset_id AS asset_id FROM generation_attempts
        WHERE output_asset_id IS NOT NULL AND (
             (owner_type = 'generation_styling' AND owner_id IN ({placeholders}))
             OR (owner_type = 'generation_shoot_script' AND owner_id IN (
                  SELECT id FROM generation_shoot_scripts WHERE task_id IN ({placeholders})
                ))
             OR (owner_type = 'generation_preview' AND owner_id IN (
                  SELECT id FROM generation_previews WHERE task_id IN ({placeholders})
                ))
             OR (owner_type = 'generation_result_image' AND owner_id IN (
                  SELECT gri.id FROM generation_result_images_new gri
                  JOIN generation_result_sets grs ON grs.id = gri.result_set_id
                  WHERE grs.task_id IN ({placeholders})
                ))
        )
        """,
        (*task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids),
    ).fetchall()
    asset_ids = list(dict.fromkeys(int(row["asset_id"]) for row in asset_rows if row["asset_id"] is not None))
    media_rows = media_asset_rows_for_ids(db, asset_ids)
    object_keys = [media["object_key"] for media in media_rows]
    cleanup_objects([key for media in media_rows for key in image_object_keys(media["object_key"])])
    db.execute(
        f"""
        DELETE FROM generation_attempts
        WHERE (owner_type = 'generation_styling' AND owner_id IN ({placeholders}))
           OR (owner_type = 'generation_shoot_script' AND owner_id IN (
                SELECT id FROM generation_shoot_scripts WHERE task_id IN ({placeholders})
              ))
           OR (owner_type = 'generation_preview' AND owner_id IN (
                SELECT id FROM generation_previews WHERE task_id IN ({placeholders})
              ))
           OR (owner_type = 'generation_result_image' AND owner_id IN (
                SELECT gri.id FROM generation_result_images_new gri
                JOIN generation_result_sets grs ON grs.id = gri.result_set_id
                WHERE grs.task_id IN ({placeholders})
              ))
        """,
        (*task_ids, *task_ids, *task_ids, *task_ids),
    )
    rows = get_generation_task_rows_for_mutation(db, task_ids)
    db.execute(f"DELETE FROM generation_tasks WHERE id IN ({placeholders})", task_ids)
    if asset_ids:
        asset_placeholders = ", ".join("?" for _ in asset_ids)
        db.execute(f"DELETE FROM media_assets WHERE id IN ({asset_placeholders})", asset_ids)
    for row in rows:
        log_event(db, "generation_task", int(row["id"]), "purged", {"name": row["name"], "reason": reason})
    return cleanup_prefixes(media_cleanup_prefixes(object_keys))


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
            SELECT gti.task_id, gti.media_asset_id AS previous_media_asset_id, gt.name, gt.status FROM generation_task_images gti
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
        release_media_asset_if_unreferenced(db, int(row["previous_media_asset_id"]))
        db.execute("UPDATE generation_tasks SET updated_at = ? WHERE id = ?", (now_iso(), row["task_id"]))
    return {"item": get_generation_task_by_id(int(row["task_id"]))}


@app.patch("/api/generation-task-images/{image_id}")
def update_generation_task_image(image_id: int, payload: GenerationTaskImageUpdateRequest) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gti.task_id, gti.media_asset_id, gt.status FROM generation_task_images gti
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
            SELECT gti.task_id, gti.media_asset_id, gt.status FROM generation_task_images gti
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
        media_asset_id = int(row["media_asset_id"])
        db.execute("DELETE FROM generation_task_images WHERE id = ?", (image_id,))
        release_media_asset_if_unreferenced(db, media_asset_id)
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
    if task["status"] not in {
        "READY_FOR_STYLING",
        "STYLING_READY",
        "SCRIPT_READY",
        "PREVIEW_READY",
        "COMPLETED",
        "FINAL_PARTIAL",
        "FAILED",
        "CANCELLED",
    }:
        raise HTTPException(status_code=409, detail="当前任务状态不能智能选搭")
    input_version_id = create_generation_input_version(task_id)
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET status = 'STYLING_RUNNING', updated_at = ? WHERE id = ?", (now_iso(), task_id))
    attempt_id = create_attempt("generation_styling", task_id, "gemini")
    try:
        input_row, candidate_snapshot, request_snapshot = build_ai_stylist_context(input_version_id)
        update_attempt_request_snapshot(attempt_id, request_snapshot)
        if generation_task_or_attempt_cancelled(task_id, attempt_id):
            finish_attempt(attempt_id, "CANCELLED")
            return {"item": get_generation_task_by_id(task_id)}
        styling_plan = await select_styling_with_gemini(input_row, candidate_snapshot, request_snapshot, attempt_id=attempt_id)
        validate_styling_plan(styling_plan, candidate_snapshot)
        selected_ids = [int(item) for item in styling_plan.get("selected_material_ids", [])]
        source_ids, selected_snapshot = get_styling_source_and_selected_snapshots(selected_ids)
    except Exception as exc:
        with get_db() as db:
            current = db.execute("SELECT status FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
            if not current or current["status"] == "CANCELLED":
                if attempt_id:
                    finish_attempt(attempt_id, "CANCELLED", error_message=str(exc))
                return {"item": get_generation_task_by_id(task_id)}
        if attempt_id:
            finish_attempt(attempt_id, "FAILED", error_message=str(exc))
        with get_db() as db:
            db.execute("UPDATE generation_tasks SET status = 'FAILED', updated_at = ? WHERE id = ?", (now_iso(), task_id))
        raise HTTPException(status_code=502, detail=f"智能选搭失败：{exc}") from exc
    with get_db() as db:
        current = db.execute("SELECT status FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
        if current and current["status"] == "CANCELLED":
            if attempt_id:
                finish_attempt(attempt_id, "CANCELLED", response_snapshot=styling_plan)
            return {"item": get_generation_task_by_id(task_id)}
    with get_db() as db:
        timestamp = now_iso()
        cursor = db.execute(
            """
            INSERT INTO generation_stylings
              (task_id, input_version_id, source_material_ids_json, candidate_material_ids_json,
               candidate_retrieval_snapshot_json, library_adequacy_snapshot_json,
               selected_material_ids_json, selected_material_snapshot_json,
               styling_plan_json, hero_understanding_json, outfit_candidates_json, critic_scores_json,
               rejected_candidates_json, reason_summary, warnings_json, status, attempt_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUCCEEDED', ?, ?, ?)
            """,
            (
                task_id,
                input_version_id,
                dumps(source_ids),
                dumps(candidate_snapshot.get("candidate_material_ids") or []),
                dumps(candidate_snapshot),
                dumps(candidate_snapshot.get("library_adequacy") or {}),
                dumps(selected_ids),
                dumps(selected_snapshot),
                dumps(styling_plan),
                dumps(styling_plan.get("hero_understanding") or {}),
                dumps(styling_plan.get("outfit_candidates") or []),
                dumps(get_selected_outfit_scores(styling_plan)),
                dumps(styling_plan.get("rejected_candidates") or []),
                str(styling_plan.get("reason_summary") or "已根据主推款和搭配服饰标签完成 AI Stylist 造型方案。"),
                dumps(styling_plan.get("warnings") or []),
                attempt_id,
                timestamp,
                timestamp,
            ),
        )
        styling_id = int(cursor.lastrowid)
        hero_summary = str((styling_plan.get("hero_understanding") or {}).get("hero_visual_summary") or "").strip()
        if hero_summary:
            db.execute("UPDATE generation_task_input_versions SET hero_visual_summary = ? WHERE id = ?", (hero_summary, input_version_id))
        db.execute(
            """
            UPDATE generation_shoot_scripts
            SET status = 'SUPERSEDED', superseded_at = ?, updated_at = ?
            WHERE task_id = ? AND status = 'SUCCEEDED'
            """,
            (timestamp, timestamp, task_id),
        )
        db.execute(
            """
            UPDATE generation_previews
            SET status = 'SUPERSEDED', updated_at = ?
            WHERE task_id = ? AND status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')
            """,
            (timestamp, task_id),
        )
        db.execute(
            """
            UPDATE generation_result_sets
            SET status = 'SUPERSEDED', updated_at = ?
            WHERE task_id = ? AND status IN ('RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED')
            """,
            (timestamp, task_id),
        )
        db.execute(
            """
            UPDATE generation_tasks
            SET status = 'STYLING_READY',
                current_input_version_id = ?,
                active_styling_id = ?,
                active_shoot_script_id = NULL,
                active_storyboard_preview_id = NULL,
                active_preview_id = NULL,
                active_result_set_id = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (input_version_id, styling_id, timestamp, task_id),
        )
    finish_attempt(attempt_id, "SUCCEEDED", response_snapshot=styling_plan)
    return {"item": get_generation_task_by_id(task_id, include_debug=True)}


@app.post("/api/generation-tasks/{task_id}/shoot-script")
async def create_generation_shoot_script(task_id: int) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    styling_id = task["active_styling_id"]
    input_version_id = task["current_input_version_id"]
    if not styling_id or not input_version_id or task["status"] not in {
        "STYLING_READY",
        "SCRIPT_READY",
        "PREVIEW_READY",
        "COMPLETED",
        "FINAL_PARTIAL",
        "FAILED",
        "CANCELLED",
    }:
        raise HTTPException(status_code=409, detail="请先完成 AI 智能选搭")
    timestamp = now_iso()
    with get_db() as db:
        transition = db.execute(
            """
            UPDATE generation_tasks
            SET status = 'SCRIPT_RUNNING', updated_at = ?
            WHERE id = ?
              AND current_input_version_id = ?
              AND active_styling_id = ?
              AND status IN ('STYLING_READY', 'SCRIPT_READY', 'PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'FAILED', 'CANCELLED')
            """,
            (timestamp, task_id, input_version_id, styling_id),
        )
        if int(transition.rowcount or 0) == 0:
            raise HTTPException(status_code=409, detail="拍摄剧本已在生成中，请勿重复提交")
        cursor = db.execute(
            """
            INSERT INTO generation_shoot_scripts
              (task_id, input_version_id, styling_id, status, script_json, summary_json,
               styling_risks_json, accessory_adjustments_json, created_at, updated_at)
            VALUES (?, ?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                input_version_id,
                styling_id,
                dumps({}),
                dumps({}),
                dumps([]),
                dumps([]),
                timestamp,
                timestamp,
            ),
        )
        shoot_script_id = int(cursor.lastrowid)
    attempt_id = create_attempt("generation_shoot_script", shoot_script_id, "gemini")
    with get_db() as db:
        db.execute("UPDATE generation_shoot_scripts SET active_attempt_id = ? WHERE id = ?", (attempt_id, shoot_script_id))
    try:
        request_snapshot = build_shoot_script_request_snapshot(task_id, int(styling_id), int(input_version_id))
        try:
            request_snapshot = attach_shoot_script_prompt_snapshot(request_snapshot)
        except RuntimeError:
            pass
        update_attempt_request_snapshot(attempt_id, request_snapshot)
        raw_shoot_script = await generate_shoot_script_with_gemini(request_snapshot)
        shoot_script = normalize_shoot_script(raw_shoot_script, request_snapshot)
        shoot_script["model_raw_output"] = raw_shoot_script
        validate_shoot_script(shoot_script)
        shot_plans = shoot_script_to_generation_shot_plans(shoot_script)
    except Exception as exc:
        if shoot_script_generation_cancelled(task_id, shoot_script_id, attempt_id):
            finish_attempt(attempt_id, "CANCELLED", error_message="拍摄剧本生成已取消")
            mark_shoot_script_generation_cancelled(shoot_script_id)
            return {"item": get_generation_task_by_id(task_id, include_debug=True)}
        finish_attempt(attempt_id, "FAILED", error_message=str(exc))
        with get_db() as db:
            previous_active = db.execute("SELECT active_shoot_script_id FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
            fallback_status = "SCRIPT_READY" if previous_active and previous_active["active_shoot_script_id"] else "FAILED"
            db.execute(
                """
                UPDATE generation_shoot_scripts
                SET status = 'FAILED', updated_at = ?
                WHERE id = ?
                """,
                (now_iso(), shoot_script_id),
            )
            db.execute("UPDATE generation_tasks SET status = ?, updated_at = ? WHERE id = ?", (fallback_status, now_iso(), task_id))
        raise HTTPException(status_code=502, detail=f"拍摄剧本生成失败：{exc}") from exc
    if shoot_script_generation_cancelled(task_id, shoot_script_id, attempt_id):
        finish_attempt(attempt_id, "CANCELLED", error_message="拍摄剧本生成已取消")
        mark_shoot_script_generation_cancelled(shoot_script_id)
        return {"item": get_generation_task_by_id(task_id, include_debug=True)}
    cancelled_after_provider_return = False
    status_changed_after_provider_return = False
    with get_db() as db:
        timestamp = now_iso()
        success_cursor = db.execute(
            """
            UPDATE generation_shoot_scripts
            SET status = 'SUCCEEDED',
                script_json = ?,
                summary_json = ?,
                styling_risks_json = ?,
                accessory_adjustments_json = ?,
                active_attempt_id = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'RUNNING'
              AND NOT EXISTS (
                SELECT 1 FROM generation_tasks
                WHERE id = ? AND status = 'CANCELLED'
              )
              AND NOT EXISTS (
                SELECT 1 FROM generation_attempts
                WHERE id = ? AND status = 'CANCELLED'
              )
            """,
            (
                dumps(shoot_script),
                dumps(shoot_script.get("user_visible_summary") or {}),
                dumps(shoot_script.get("styling_risks") or []),
                dumps(shoot_script.get("accessory_adjustments") or []),
                attempt_id,
                timestamp,
                shoot_script_id,
                task_id,
                attempt_id,
            ),
        )
        if int(success_cursor.rowcount or 0) == 0:
            current = db.execute(
                """
                SELECT gt.status AS task_status,
                       gss.status AS shoot_script_status,
                       ga.status AS attempt_status
                FROM generation_tasks gt
                LEFT JOIN generation_shoot_scripts gss ON gss.id = ?
                LEFT JOIN generation_attempts ga ON ga.id = ?
                WHERE gt.id = ?
                """,
                (shoot_script_id, attempt_id, task_id),
            ).fetchone()
            if (
                not current
                or current["task_status"] == "CANCELLED"
                or current["shoot_script_status"] == "CANCELLED"
                or current["attempt_status"] == "CANCELLED"
            ):
                cancelled_after_provider_return = True
                db.execute(
                    """
                    UPDATE generation_attempts
                    SET status = 'CANCELLED',
                        cancelled_at = COALESCE(cancelled_at, ?),
                        error_message = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, "拍摄剧本生成已取消", timestamp, attempt_id),
                )
                db.execute(
                    """
                    UPDATE generation_shoot_scripts
                    SET status = 'CANCELLED', locked_by = NULL, locked_until = NULL, updated_at = ?
                    WHERE id = ? AND status IN ('QUEUED', 'RUNNING', 'CANCELLED')
                    """,
                    (timestamp, shoot_script_id),
                )
            else:
                status_changed_after_provider_return = True
                db.execute(
                    """
                    UPDATE generation_attempts
                    SET status = 'FAILED',
                        failed_at = COALESCE(failed_at, ?),
                        error_message = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, "拍摄剧本生成状态已变更", timestamp, attempt_id),
                )
        else:
            db.execute(
                """
                UPDATE generation_shoot_scripts
                SET status = 'SUPERSEDED', superseded_at = ?, updated_at = ?
                WHERE task_id = ? AND id <> ? AND status = 'SUCCEEDED'
                """,
                (timestamp, timestamp, task_id, shoot_script_id),
            )
            db.execute(
                """
                UPDATE generation_previews
                SET status = 'SUPERSEDED', updated_at = ?
                WHERE task_id = ? AND status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')
                """,
                (timestamp, task_id),
            )
            db.execute(
                """
                UPDATE generation_result_sets
                SET status = 'SUPERSEDED', updated_at = ?
                WHERE task_id = ? AND status IN ('RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED')
                """,
                (timestamp, task_id),
            )
            for plan in shot_plans:
                db.execute(
                    """
                    INSERT INTO generation_shot_plans
                      (task_id, styling_id, shoot_script_id, sort_order, shot_type, plan_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        styling_id,
                        shoot_script_id,
                        plan["sortOrder"],
                        plan["shotType"],
                        dumps(plan),
                        timestamp,
                        timestamp,
                    ),
                )
            db.execute(
                """
                UPDATE generation_tasks
                SET status = 'SCRIPT_READY',
                    active_shoot_script_id = ?,
                    active_storyboard_preview_id = NULL,
                    active_preview_id = NULL,
                    active_result_set_id = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (shoot_script_id, timestamp, task_id),
            )
    if cancelled_after_provider_return:
        return {"item": get_generation_task_by_id(task_id, include_debug=True)}
    if status_changed_after_provider_return:
        raise HTTPException(status_code=409, detail="拍摄剧本生成状态已变更，请刷新后重试")
    finish_attempt(attempt_id, "SUCCEEDED", response_snapshot=shoot_script)
    return {"item": get_generation_task_by_id(task_id, include_debug=True)}


@app.post("/api/generation-tasks/{task_id}/preview")
async def create_generation_preview(task_id: int) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    styling_id = task["active_styling_id"]
    shoot_script_id = task["active_shoot_script_id"] if "active_shoot_script_id" in task.keys() else None
    input_version_id = task["current_input_version_id"]
    if not styling_id or not shoot_script_id or not input_version_id or (
        task["status"] not in {"SCRIPT_READY", "PREVIEW_READY", "COMPLETED", "FINAL_PARTIAL", "CANCELLED"}
        and not (task["status"] == "FAILED" and not task["active_storyboard_preview_id"])
    ):
        raise HTTPException(status_code=409, detail="请先生成拍摄剧本")
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET status = 'PREVIEW_RUNNING', updated_at = ? WHERE id = ?", (now_iso(), task_id))
    shot_plans = get_task_shot_plans_by_script(int(shoot_script_id))
    timestamp = now_iso()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO generation_previews (task_id, styling_id, shoot_script_id, input_version_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)
            """,
            (task_id, styling_id, shoot_script_id, input_version_id, timestamp, timestamp),
        )
        preview_id = int(cursor.lastrowid)
    attempt_id = create_attempt("generation_preview", preview_id, "image-2")
    lease_generation_preview(preview_id, attempt_id)
    schedule_background_generation(
        run_preview_generation(task_id, int(styling_id), int(shoot_script_id), shot_plans, preview_id, attempt_id),
        job_key=preview_job_key(preview_id),
    )
    return {"item": get_generation_task_by_id(task_id, include_debug=True)}


async def run_preview_generation(task_id: int, styling_id: int, shoot_script_id: int, shot_plans: list[Any], preview_id: int, attempt_id: int) -> None:
    try:
        task_package = build_new_preview_task_package(task_id, styling_id, shoot_script_id, shot_plans)
        if not attempt_has_provider_request_id(attempt_id):
            update_attempt_request_snapshot(attempt_id, task_package)
        asset_id, provider_response = await generate_image_asset(
            task_package,
            join_key("generation-tasks", str(task_id), "preview", f"preview-{preview_id}.png"),
            attempt_id=attempt_id,
        )
    except Exception as exc:
        provider_response = getattr(exc, "provider_response", None)
        with get_db() as db:
            current = db.execute(
                """
                SELECT gp.status AS preview_status, gt.status AS task_status
                FROM generation_previews gp
                JOIN generation_tasks gt ON gt.id = gp.task_id
                WHERE gp.id = ?
                """,
                (preview_id,),
            ).fetchone()
            if current and (current["preview_status"] == "CANCELLED" or current["task_status"] == "CANCELLED"):
                finish_attempt(attempt_id, "CANCELLED", response_snapshot=provider_response, error_message=str(exc))
                persist_attempt_provider_request_id(attempt_id, provider_response)
                return
        finish_attempt(attempt_id, "FAILED", response_snapshot=provider_response, error_message=str(exc))
        persist_attempt_provider_request_id(attempt_id, provider_response)
        with get_db() as db:
            timestamp = now_iso()
            db.execute(
                """
                UPDATE generation_previews
                SET status = 'FAILED', active_attempt_id = ?, locked_by = NULL, locked_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (attempt_id, timestamp, preview_id),
            )
            db.execute("UPDATE generation_tasks SET status = 'FAILED', updated_at = ? WHERE id = ?", (timestamp, task_id))
        return
    with get_db() as db:
        current = db.execute(
            """
            SELECT gp.status AS preview_status, gt.status AS task_status
            FROM generation_previews gp
            JOIN generation_tasks gt ON gt.id = gp.task_id
            WHERE gp.id = ?
            """,
            (preview_id,),
        ).fetchone()
        if current and (current["preview_status"] == "CANCELLED" or current["task_status"] == "CANCELLED"):
            finish_attempt(attempt_id, "CANCELLED", response_snapshot=provider_response, output_asset_id=asset_id)
            persist_attempt_provider_request_id(attempt_id, provider_response)
            return
    finish_attempt(attempt_id, "SUCCEEDED", response_snapshot=provider_response, output_asset_id=asset_id)
    persist_attempt_provider_request_id(attempt_id, provider_response)
    with get_db() as db:
        timestamp = now_iso()
        db.execute(
            """
            UPDATE generation_previews
            SET status = 'SUCCEEDED', preview_asset_id = ?, active_attempt_id = ?,
                locked_by = NULL, locked_until = NULL, updated_at = ?
            WHERE id = ?
            """,
            (asset_id, attempt_id, timestamp, preview_id),
        )
        db.execute(
            """
            UPDATE generation_previews
            SET status = 'SUPERSEDED', updated_at = ?
            WHERE task_id = ? AND id <> ? AND status = 'SUCCEEDED'
            """,
            (timestamp, task_id, preview_id),
        )
        db.execute(
            """
            UPDATE generation_result_sets
            SET status = 'SUPERSEDED', updated_at = ?
            WHERE task_id = ? AND status IN ('RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED')
            """,
            (timestamp, task_id),
        )
        db.execute(
            """
            UPDATE generation_tasks
            SET status = 'PREVIEW_READY',
                active_preview_id = ?,
                active_storyboard_preview_id = ?,
                active_result_set_id = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (preview_id, preview_id, timestamp, task_id),
        )


@app.post("/api/generation-tasks/{task_id}/generate-final")
async def generate_final_images(task_id: int) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    if (
        task["status"] not in {"PREVIEW_READY", "COMPLETED", "FINAL_PARTIAL", "CANCELLED"}
        or not task["active_storyboard_preview_id"]
        or not task["active_styling_id"]
        or not task["active_shoot_script_id"]
        or not task["current_input_version_id"]
    ):
        raise HTTPException(status_code=409, detail="请先生成分镜预览")
    preview_id = int(task["active_storyboard_preview_id"])
    styling_id = int(task["active_styling_id"])
    shoot_script_id = int(task["active_shoot_script_id"])
    input_version_id = int(task["current_input_version_id"])
    timestamp = now_iso()
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET status = 'FINAL_RUNNING', updated_at = ? WHERE id = ?", (timestamp, task_id))
        cursor = db.execute(
            """
            INSERT INTO generation_result_sets (task_id, input_version_id, styling_id, shoot_script_id, preview_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?)
            """,
            (task_id, input_version_id, styling_id, shoot_script_id, preview_id, timestamp, timestamp),
        )
        result_set_id = int(cursor.lastrowid)
        db.execute(
            """
            UPDATE generation_result_sets
            SET status = 'SUPERSEDED', updated_at = ?
            WHERE task_id = ? AND id <> ? AND status IN ('SUCCEEDED', 'PARTIAL', 'FAILED')
            """,
            (timestamp, task_id, result_set_id),
        )
        db.execute("UPDATE generation_tasks SET active_result_set_id = ?, updated_at = ? WHERE id = ?", (result_set_id, timestamp, task_id))
        shot_rows = db.execute(
            "SELECT * FROM generation_shot_plans WHERE shoot_script_id = ? AND sort_order <= 5 ORDER BY sort_order",
            (shoot_script_id,),
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
    schedule_background_generation(
        run_final_generation(task_id, result_set_id, image_ids),
        job_key=final_set_job_key(result_set_id),
    )
    return {"item": get_generation_task_by_id(task_id, include_debug=True)}


async def run_final_generation(
    task_id: int,
    result_set_id: int,
    image_ids: list[int],
    resume_attempts_by_image_id: dict[int, int] | None = None,
) -> None:
    resume_attempts_by_image_id = resume_attempts_by_image_id or {}
    await asyncio.gather(
        *(
            generate_single_final_image(
                task_id,
                result_set_id,
                image_id,
                attempt_id=resume_attempts_by_image_id.get(image_id),
            )
            for image_id in image_ids
        ),
        return_exceptions=True,
    )
    with get_db() as db:
        reconcile_final_result_set(db, result_set_id, task_id)


def reconcile_final_result_set(db: Any, result_set_id: int, task_id: int | None = None) -> tuple[str, str]:
    if task_id is None:
        row = db.execute("SELECT task_id FROM generation_result_sets WHERE id = ?", (result_set_id,)).fetchone()
        if not row:
            return "PARTIAL", "FINAL_PARTIAL"
        task_id = int(row["task_id"])
    status_rows = db.execute("SELECT status FROM generation_result_images_new WHERE result_set_id = ?", (result_set_id,)).fetchall()
    statuses = [row["status"] for row in status_rows]
    timestamp = now_iso()
    if any(status in {"QUEUED", "RUNNING"} for status in statuses):
        db.execute("UPDATE generation_result_sets SET status = 'RUNNING', updated_at = ? WHERE id = ?", (timestamp, result_set_id))
        db.execute(
            "UPDATE generation_tasks SET status = 'FINAL_RUNNING', active_result_set_id = ?, updated_at = ? WHERE id = ?",
            (result_set_id, timestamp, task_id),
        )
        return "RUNNING", "FINAL_RUNNING"
    if statuses and all(status == "CANCELLED" for status in statuses):
        db.execute("UPDATE generation_result_sets SET status = 'CANCELLED', updated_at = ? WHERE id = ?", (timestamp, result_set_id))
        db.execute(
            "UPDATE generation_tasks SET status = 'CANCELLED', active_result_set_id = ?, updated_at = ? WHERE id = ?",
            (result_set_id, timestamp, task_id),
        )
        return "CANCELLED", "CANCELLED"
    completed = bool(statuses) and all(status == "SUCCEEDED" for status in statuses)
    result_set_status = "SUCCEEDED" if completed else "PARTIAL"
    task_status = "COMPLETED" if completed else "FINAL_PARTIAL"
    db.execute("UPDATE generation_result_sets SET status = ?, updated_at = ? WHERE id = ?", (result_set_status, timestamp, result_set_id))
    db.execute(
        "UPDATE generation_tasks SET status = ?, active_result_set_id = ?, updated_at = ? WHERE id = ?",
        (task_status, result_set_id, timestamp, task_id),
    )
    return result_set_status, task_status


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
        if row["status"] in {"QUEUED", "RUNNING"}:
            raise HTTPException(status_code=409, detail="当前结果图仍在生成中")
        timestamp = now_iso()
        db.execute(
            """
            UPDATE generation_result_images_new
            SET status = 'QUEUED', active_attempt_id = NULL,
                locked_by = NULL, locked_until = NULL, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, image_id),
        )
        db.execute("UPDATE generation_result_sets SET status = 'RUNNING', updated_at = ? WHERE id = ?", (timestamp, row["result_set_id"]))
        db.execute(
            "UPDATE generation_tasks SET status = 'FINAL_RUNNING', active_result_set_id = ?, updated_at = ? WHERE id = ?",
            (row["result_set_id"], timestamp, row["task_id"]),
        )
    schedule_background_generation(
        run_final_generation(int(row["task_id"]), int(row["result_set_id"]), [image_id]),
        job_key=final_set_job_key(int(row["result_set_id"])),
    )
    return {"item": get_generation_task_by_id(int(row["task_id"]), include_debug=True)}

@app.get("/api/generation-records")
def list_new_generation_records(
    include_archived: bool = Query(False, alias="includeArchived"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM generation_tasks gt
            WHERE gt.deleted_at IS NULL
              AND (? OR gt.archived_at IS NULL)
            ORDER BY gt.created_at DESC, gt.id DESC
            LIMIT ? OFFSET ?
            """,
            (include_archived, limit + 1, offset),
        ).fetchall()
        total = int(db.execute(
            """
            SELECT COUNT(*) AS total FROM generation_tasks gt
            WHERE gt.deleted_at IS NULL
              AND (? OR gt.archived_at IS NULL)
            """,
            (include_archived,),
        ).fetchone()["total"])
        items = rows[:limit]
        return {
            "items": [serialize_generation_task(row, include_detail=False) for row in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if offset + limit < total else None,
            "hasMore": offset + limit < total,
            "total": total,
            "totalPages": (total + limit - 1) // limit if total else 0,
        }


@app.get("/api/generation-records/{task_id}")
def get_new_generation_record(task_id: int, include_debug: bool = Query(False, alias="includeDebug")) -> dict[str, Any]:
    return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}


@app.get("/api/prompt-configs")
def list_prompt_configs(request: Request) -> dict[str, Any]:
    require_admin(request)
    items = get_all_prompt_configs()
    return {
        "items": items,
        "placeholders": available_prompt_placeholders(),
    }


@app.get("/api/prompt-configs/{config_key}/preview")
def preview_prompt_config(config_key: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    key = normalize_prompt_config_key(config_key)
    if key not in get_prompt_config_rows():
        raise HTTPException(status_code=404, detail="Prompt 配置不存在")
    rendered, diagnostics = render_prompt_config(key, sample_prompt_runtime())
    return {"rendered": rendered, "diagnostics": diagnostics}


@app.patch("/api/prompt-configs/{config_key}")
def update_prompt_config(config_key: str, payload: PromptConfigUpdateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    key = normalize_prompt_config_key(config_key)
    content = payload.content[:120000]
    timestamp = now_iso()
    with get_db() as db:
        existing = db.execute("SELECT key FROM prompt_configs WHERE key = ?", (key,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Prompt 配置不存在")
        db.execute(
            """
            UPDATE prompt_configs
            SET content = ?, updated_at = ?
            WHERE key = ?
            """,
            (content, timestamp, key),
        )
        log_event(db, "prompt_config", None, "updated", {"key": key})
    return {"item": get_prompt_config(key), "preview": preview_prompt_config(key, request)}


@app.get("/api/brand-info")
def get_brand_info(request: Request) -> dict[str, Any]:
    require_admin(request)
    return {"item": get_current_brand_info()}


@app.post("/api/brand-info")
def create_brand_info(payload: BrandInfoUpsertRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    timestamp = now_iso()
    profile = normalize_brand_info_payload(payload)
    with get_db() as db:
        existing = db.execute("SELECT id FROM brand_profiles WHERE id = 1").fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="品牌信息已存在")
        db.execute(
            """
            INSERT INTO brand_profiles
              (id, name, tagline, brand_core, style_dna, styling_guidelines, shooting_guidelines, negative_constraints, created_at, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile["name"],
                profile["tagline"],
                profile["brandCore"],
                profile["styleDna"],
                profile["stylingGuidelines"],
                profile["shootingGuidelines"],
                profile["negativeConstraints"],
                timestamp,
                timestamp,
            ),
        )
        log_event(db, "brand_profile", 1, "created", profile)
    return {"item": get_current_brand_info()}


@app.patch("/api/brand-info")
def update_brand_info(payload: BrandInfoUpsertRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    updates = normalize_brand_info_payload(payload, partial=True)
    with get_db() as db:
        row = db.execute("SELECT * FROM brand_profiles WHERE id = 1").fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="品牌信息不存在")
        if updates:
            db_updates: dict[str, Any] = {}
            key_map = {
                "name": "name",
                "tagline": "tagline",
                "brandCore": "brand_core",
                "styleDna": "style_dna",
                "stylingGuidelines": "styling_guidelines",
                "shootingGuidelines": "shooting_guidelines",
                "negativeConstraints": "negative_constraints",
            }
            for key, value in updates.items():
                column = key_map[key]
                db_updates[column] = value
            db_updates["updated_at"] = now_iso()
            set_clause = ", ".join(f"{key} = ?" for key in db_updates)
            db.execute(
                f"UPDATE brand_profiles SET {set_clause} WHERE id = 1",
                (*db_updates.values(),),
            )
            log_event(db, "brand_profile", 1, "updated", updates)
    return {"item": get_current_brand_info()}


@app.delete("/api/brand-info")
def delete_brand_info(request: Request) -> dict[str, Any]:
    require_admin(request)
    with get_db() as db:
        row = db.execute("SELECT * FROM brand_profiles WHERE id = 1").fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="品牌信息不存在")
        db.execute("DELETE FROM brand_profiles WHERE id = 1")
        log_event(db, "brand_profile", 1, "deleted", serialize_brand_info(row))
    return {"ok": True}


@app.get("/api/shoot-config")
def get_shoot_config(request: Request) -> dict[str, Any]:
    require_admin(request)
    return {"item": serialize_shoot_config(get_active_shoot_config())}


@app.get("/api/shoot-config/export")
def export_shoot_config(request: Request) -> dict[str, Any]:
    require_admin(request)
    config = normalize_shoot_config_payload(get_active_shoot_config())
    return {
        "schemaVersion": "ruyi-shoot-config-v1",
        "config": config,
        "categoryOptionsSnapshot": get_shoot_config_category_options(),
    }


@app.patch("/api/shoot-config")
def update_shoot_config(payload: ShootConfigUpdateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    config = normalize_shoot_config_payload(payload.config)
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO shoot_configs (id, config_json, created_at, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              config_json = excluded.config_json,
              updated_at = excluded.updated_at
            """,
            (dumps(config), timestamp, timestamp),
        )
        log_event(db, "shoot_config", 1, "updated", {"schemaVersion": config["schemaVersion"]})
    return {"item": serialize_shoot_config(config)}


@app.post("/api/shoot-config/import")
def import_shoot_config(payload: ShootConfigImportRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    config = normalize_shoot_config_payload(payload.config)
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO shoot_configs (id, config_json, created_at, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              config_json = excluded.config_json,
              updated_at = excluded.updated_at
            """,
            (dumps(config), timestamp, timestamp),
        )
        log_event(db, "shoot_config", 1, "imported", {"schemaVersion": config["schemaVersion"]})
    return {"item": serialize_shoot_config(config)}


@app.get("/api/taxonomy/rules")
def get_taxonomy_rules(request: Request) -> dict[str, Any]:
    require_admin(request)
    with get_db() as db:
        dimensions = db.execute(
            """
            SELECT * FROM tag_dimensions
            ORDER BY sort_order, id
            """
        ).fetchall()
        tags = db.execute(
            """
            SELECT td.*,
                   COUNT(gta.id) AS usage_count
            FROM tag_defs td
            LEFT JOIN garment_tag_assignments gta
              ON gta.dimension_code = td.dimension_code
             AND gta.tag_code = td.code
             AND gta.status IN ('pending', 'confirmed')
            GROUP BY td.id
            ORDER BY td.dimension_code, td.sort_order, td.id
            """
        ).fetchall()
        assignment_total = db.execute("SELECT COUNT(*) AS count FROM garment_tag_assignments").fetchone()["count"]
        tagged_garment_total = db.execute("SELECT COUNT(DISTINCT garment_id) AS count FROM garment_tag_assignments").fetchone()["count"]
        relations = db.execute(
            """
            SELECT * FROM tag_relations
            ORDER BY from_dimension_code, from_tag_code, relation_type, to_dimension_code, to_tag_code, id
            """
        ).fetchall()
        applicability_rules = db.execute(
            """
            SELECT * FROM tag_applicability_rules
            ORDER BY category_dimension_code, category_tag_code, target_dimension_code, target_tag_code, id
            """
        ).fetchall()
    tags_by_dimension: dict[str, list[dict[str, Any]]] = {}
    for row in tags:
        tags_by_dimension.setdefault(row["dimension_code"], []).append(serialize_tag_def(row))
    return {
        "dimensions": [
            {
                **serialize_tag_dimension(row),
                "tags": build_taxonomy_tree(tags_by_dimension.get(row["code"], [])),
            }
            for row in dimensions
        ],
        "summary": {
            "dimensionCount": len(dimensions),
            "tagCount": len(tags),
            "relationCount": len(relations),
            "applicabilityRuleCount": len(applicability_rules),
            "assignmentCount": int(assignment_total or 0),
            "taggedGarmentCount": int(tagged_garment_total or 0),
        },
        "relations": [serialize_tag_relation(row) for row in relations],
        "applicabilityRules": [serialize_tag_applicability_rule(row) for row in applicability_rules],
    }


@app.get("/api/taxonomy/export")
def export_taxonomy_rules(request: Request) -> dict[str, Any]:
    require_admin(request)
    with get_db() as db:
        dimensions = db.execute("SELECT * FROM tag_dimensions ORDER BY sort_order, id").fetchall()
        tags = db.execute(
            """
            SELECT * FROM tag_defs
            ORDER BY dimension_code, level, parent_code, sort_order, id
            """
        ).fetchall()
        relations = db.execute(
            """
            SELECT * FROM tag_relations
            ORDER BY from_dimension_code, from_tag_code, relation_type, to_dimension_code, to_tag_code, id
            """
        ).fetchall()
        applicability_rules = db.execute(
            """
            SELECT * FROM tag_applicability_rules
            ORDER BY category_dimension_code, category_tag_code, target_dimension_code, target_tag_code, id
            """
        ).fetchall()
    return {
        "schemaVersion": "ruyi-taxonomy-rules-v1",
        "dimensions": [serialize_tag_dimension(row) for row in dimensions],
        "tags": [serialize_tag_def(row) | {"children": []} for row in tags],
        "relations": [serialize_tag_relation(row) for row in relations],
        "applicabilityRules": [serialize_tag_applicability_rule(row) for row in applicability_rules],
    }


@app.post("/api/taxonomy/import")
def import_taxonomy_rules(payload: TaxonomyImportRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    normalized_dimensions = [normalize_taxonomy_dimension_create_payload(item) for item in payload.dimensions]
    normalized_tags = [normalize_taxonomy_tag_import_payload(item) for item in payload.tags]
    normalized_relations = [normalize_taxonomy_relation_import_payload(item) for item in payload.relations]
    normalized_applicability_rules = [normalize_taxonomy_applicability_import_payload(item) for item in payload.applicabilityRules]
    timestamp = now_iso()
    with get_db() as db:
        for dimension in normalized_dimensions:
            upsert_taxonomy_dimension(db, dimension, timestamp)
        for tag in normalized_tags:
            ensure_taxonomy_dimension_exists(db, tag["dimension_code"])
            upsert_taxonomy_tag(db, {**tag, "parent_code": None}, timestamp)
        for tag in normalized_tags:
            validate_taxonomy_parent(db, tag["dimension_code"], tag.get("parent_code"), current_code=tag["code"])
            update_taxonomy_tag_parent(db, tag, timestamp)
        for relation in normalized_relations:
            validate_taxonomy_relation_tags(db, relation)
            upsert_taxonomy_relation(db, relation, timestamp)
        for rule in normalized_applicability_rules:
            validate_taxonomy_applicability_tags(db, rule)
            upsert_taxonomy_applicability_rule(db, rule, timestamp)
        log_event(db, "taxonomy", None, "imported", {
            "dimensionCount": len(normalized_dimensions),
            "tagCount": len(normalized_tags),
            "relationCount": len(normalized_relations),
            "applicabilityRuleCount": len(normalized_applicability_rules),
        })
    return get_taxonomy_rules(request)


@app.post("/api/taxonomy/dimensions")
def create_taxonomy_dimension(payload: TaxonomyDimensionCreateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    normalized = normalize_taxonomy_dimension_create_payload(payload.model_dump() if hasattr(payload, "model_dump") else payload.dict())
    timestamp = now_iso()
    with get_db() as db:
        existing = db.execute("SELECT id FROM tag_dimensions WHERE code = ?", (normalized["code"],)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="标签维度 code 已存在")
        upsert_taxonomy_dimension(db, normalized, timestamp)
        log_event(db, "taxonomy_dimension", None, "created", normalized)
    return get_taxonomy_rules(request)


@app.patch("/api/taxonomy/dimensions/{dimension_code}")
def update_taxonomy_dimension(dimension_code: str, payload: TaxonomyDimensionUpdateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    code = sanitize_taxonomy_code(dimension_code)
    updates = normalize_taxonomy_dimension_payload(payload)
    if not updates:
        return get_taxonomy_rules(request)
    with get_db() as db:
        row = db.execute("SELECT * FROM tag_dimensions WHERE code = ?", (code,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="标签维度不存在")
        updates["updated_at"] = now_iso()
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        db.execute(f"UPDATE tag_dimensions SET {set_clause} WHERE code = ?", (*updates.values(), code))
        log_event(db, "taxonomy_dimension", int(row["id"]), "updated", {"code": code, **updates})
    return get_taxonomy_rules(request)


@app.delete("/api/taxonomy/dimensions/{dimension_code}")
def delete_taxonomy_dimension(dimension_code: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    code = sanitize_taxonomy_code(dimension_code)
    with get_db() as db:
        row = db.execute("SELECT * FROM tag_dimensions WHERE code = ?", (code,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="标签维度不存在")
        tag_count = db.execute("SELECT COUNT(*) AS count FROM tag_defs WHERE dimension_code = ?", (code,)).fetchone()["count"]
        if tag_count:
            raise HTTPException(status_code=409, detail="维度下仍有标签，请先删除或迁移标签")
        assignment_count = db.execute("SELECT COUNT(*) AS count FROM garment_tag_assignments WHERE dimension_code = ?", (code,)).fetchone()["count"]
        if assignment_count:
            raise HTTPException(status_code=409, detail="维度已有商品打标引用，不能删除")
        db.execute("DELETE FROM tag_dimensions WHERE code = ?", (code,))
        log_event(db, "taxonomy_dimension", int(row["id"]), "deleted", serialize_tag_dimension(row))
    return get_taxonomy_rules(request)


@app.post("/api/taxonomy/tags")
def create_taxonomy_tag(payload: TaxonomyTagCreateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    normalized = normalize_taxonomy_tag_create_payload(payload)
    timestamp = now_iso()
    with get_db() as db:
        ensure_taxonomy_dimension_exists(db, normalized["dimension_code"])
        validate_taxonomy_parent(db, normalized["dimension_code"], normalized.get("parent_code"))
        existing = db.execute(
            "SELECT id FROM tag_defs WHERE dimension_code = ? AND code = ?",
            (normalized["dimension_code"], normalized["code"]),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="标签 code 已存在")
        cursor = db.execute(
            """
            INSERT INTO tag_defs
              (dimension_code, code, label_zh, description_zh, parent_code, level, value_kind,
               is_selectable, is_active, sort_order, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized["dimension_code"],
                normalized["code"],
                normalized["label_zh"],
                normalized["description_zh"],
                normalized["parent_code"],
                normalized["level"],
                normalized["value_kind"],
                normalized["is_selectable"],
                normalized["is_active"],
                normalized["sort_order"],
                dumps(normalized["metadata"]),
                timestamp,
                timestamp,
            ),
        )
        log_event(db, "taxonomy_tag", int(cursor.lastrowid), "created", normalized)
    return get_taxonomy_rules(request)


@app.delete("/api/taxonomy/tags/{tag_id}")
def delete_taxonomy_tag(tag_id: int, request: Request) -> dict[str, Any]:
    require_admin(request)
    with get_db() as db:
        row = db.execute("SELECT * FROM tag_defs WHERE id = ?", (tag_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="标签不存在")
        child_count = db.execute(
            "SELECT COUNT(*) AS count FROM tag_defs WHERE dimension_code = ? AND parent_code = ?",
            (row["dimension_code"], row["code"]),
        ).fetchone()["count"]
        if child_count:
            raise HTTPException(status_code=409, detail="标签仍有子标签，请先删除或迁移子标签")
        usage_count = db.execute(
            "SELECT COUNT(*) AS count FROM garment_tag_assignments WHERE dimension_code = ? AND tag_code = ?",
            (row["dimension_code"], row["code"]),
        ).fetchone()["count"]
        if usage_count:
            raise HTTPException(status_code=409, detail="标签已有商品打标引用，不能删除；可先停用")
        relation_count = db.execute(
            """
            SELECT COUNT(*) AS count FROM tag_relations
            WHERE (from_dimension_code = ? AND from_tag_code = ?)
               OR (to_dimension_code = ? AND to_tag_code = ?)
            """,
            (row["dimension_code"], row["code"], row["dimension_code"], row["code"]),
        ).fetchone()["count"]
        applicability_count = db.execute(
            """
            SELECT COUNT(*) AS count FROM tag_applicability_rules
            WHERE (category_dimension_code = ? AND category_tag_code = ?)
               OR (target_dimension_code = ? AND target_tag_code = ?)
            """,
            (row["dimension_code"], row["code"], row["dimension_code"], row["code"]),
        ).fetchone()["count"]
        db.execute(
            """
            DELETE FROM tag_relations
            WHERE (from_dimension_code = ? AND from_tag_code = ?)
               OR (to_dimension_code = ? AND to_tag_code = ?)
            """,
            (row["dimension_code"], row["code"], row["dimension_code"], row["code"]),
        )
        db.execute(
            """
            DELETE FROM tag_applicability_rules
            WHERE (category_dimension_code = ? AND category_tag_code = ?)
               OR (target_dimension_code = ? AND target_tag_code = ?)
            """,
            (row["dimension_code"], row["code"], row["dimension_code"], row["code"]),
        )
        db.execute("DELETE FROM tag_defs WHERE id = ?", (tag_id,))
        log_event(db, "taxonomy_tag", tag_id, "deleted", {**serialize_tag_def(row), "deletedRelationCount": int(relation_count or 0), "deletedApplicabilityRuleCount": int(applicability_count or 0)})
    return get_taxonomy_rules(request)


@app.patch("/api/taxonomy/tags/{tag_id}")
def update_taxonomy_tag(tag_id: int, payload: TaxonomyTagUpdateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    updates = normalize_taxonomy_tag_update_payload(payload)
    if not updates:
        return get_taxonomy_rules(request)
    with get_db() as db:
        row = db.execute("SELECT * FROM tag_defs WHERE id = ?", (tag_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="标签不存在")
        if "parent_code" in updates:
            validate_taxonomy_parent(db, row["dimension_code"], updates["parent_code"], current_code=row["code"])
        updates["updated_at"] = now_iso()
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        db.execute(f"UPDATE tag_defs SET {set_clause} WHERE id = ?", (*updates.values(), tag_id))
        log_event(db, "taxonomy_tag", tag_id, "updated", updates)
    return get_taxonomy_rules(request)


@app.post("/api/taxonomy/relations")
def create_taxonomy_relation(payload: TaxonomyRelationCreateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    normalized = normalize_taxonomy_relation_create_payload(payload)
    timestamp = now_iso()
    with get_db() as db:
        validate_taxonomy_relation_tags(db, normalized)
        existing = db.execute(
            """
            SELECT id FROM tag_relations
            WHERE from_dimension_code = ? AND from_tag_code = ? AND relation_type = ?
              AND to_dimension_code = ? AND to_tag_code = ?
            """,
            (
                normalized["from_dimension_code"],
                normalized["from_tag_code"],
                normalized["relation_type"],
                normalized["to_dimension_code"],
                normalized["to_tag_code"],
            ),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="标签关系已存在")
        cursor = insert_taxonomy_relation(db, normalized, timestamp)
        log_event(db, "taxonomy_relation", int(cursor.lastrowid), "created", normalized)
    return get_taxonomy_rules(request)


@app.patch("/api/taxonomy/relations/{relation_id}")
def update_taxonomy_relation(relation_id: int, payload: TaxonomyRelationUpdateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    updates = normalize_taxonomy_relation_update_payload(payload)
    if not updates:
        return get_taxonomy_rules(request)
    with get_db() as db:
        row = db.execute("SELECT * FROM tag_relations WHERE id = ?", (relation_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="标签关系不存在")
        next_relation = {
            "from_dimension_code": updates.get("from_dimension_code", row["from_dimension_code"]),
            "from_tag_code": updates.get("from_tag_code", row["from_tag_code"]),
            "relation_type": updates.get("relation_type", row["relation_type"]),
            "to_dimension_code": updates.get("to_dimension_code", row["to_dimension_code"]),
            "to_tag_code": updates.get("to_tag_code", row["to_tag_code"]),
            "weight": updates.get("weight", row["weight"]),
            "description_zh": updates.get("description_zh", row["description_zh"]),
            "is_active": updates.get("is_active", row["is_active"]),
        }
        validate_taxonomy_relation_tags(db, next_relation)
        duplicate = db.execute(
            """
            SELECT id FROM tag_relations
            WHERE id <> ?
              AND from_dimension_code = ? AND from_tag_code = ? AND relation_type = ?
              AND to_dimension_code = ? AND to_tag_code = ?
            """,
            (
                relation_id,
                next_relation["from_dimension_code"],
                next_relation["from_tag_code"],
                next_relation["relation_type"],
                next_relation["to_dimension_code"],
                next_relation["to_tag_code"],
            ),
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail="标签关系已存在")
        updates["updated_at"] = now_iso()
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        db.execute(f"UPDATE tag_relations SET {set_clause} WHERE id = ?", (*updates.values(), relation_id))
        log_event(db, "taxonomy_relation", relation_id, "updated", updates)
    return get_taxonomy_rules(request)


@app.delete("/api/taxonomy/relations/{relation_id}")
def delete_taxonomy_relation(relation_id: int, request: Request) -> dict[str, Any]:
    require_admin(request)
    with get_db() as db:
        row = db.execute("SELECT * FROM tag_relations WHERE id = ?", (relation_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="标签关系不存在")
        db.execute("DELETE FROM tag_relations WHERE id = ?", (relation_id,))
        log_event(db, "taxonomy_relation", relation_id, "deleted", serialize_tag_relation(row))
    return get_taxonomy_rules(request)


@app.post("/api/taxonomy/applicability-rules")
def create_taxonomy_applicability_rule(payload: TaxonomyApplicabilityCreateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    normalized = normalize_taxonomy_applicability_create_payload(payload)
    timestamp = now_iso()
    with get_db() as db:
        validate_taxonomy_applicability_tags(db, normalized)
        existing = db.execute(
            """
            SELECT id FROM tag_applicability_rules
            WHERE category_dimension_code = ? AND category_tag_code = ?
              AND target_dimension_code = ? AND target_tag_code = ?
            """,
            (
                normalized["category_dimension_code"],
                normalized["category_tag_code"],
                normalized["target_dimension_code"],
                normalized["target_tag_code"],
            ),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="适用规则已存在")
        cursor = insert_taxonomy_applicability_rule(db, normalized, timestamp)
        log_event(db, "taxonomy_applicability_rule", int(cursor.lastrowid), "created", normalized)
    return get_taxonomy_rules(request)


@app.patch("/api/taxonomy/applicability-rules/{rule_id}")
def update_taxonomy_applicability_rule(rule_id: int, payload: TaxonomyApplicabilityUpdateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    updates = normalize_taxonomy_applicability_update_payload(payload)
    if not updates:
        return get_taxonomy_rules(request)
    with get_db() as db:
        row = db.execute("SELECT * FROM tag_applicability_rules WHERE id = ?", (rule_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="适用规则不存在")
        next_rule = {
            "category_dimension_code": updates.get("category_dimension_code", row["category_dimension_code"]),
            "category_tag_code": updates.get("category_tag_code", row["category_tag_code"]),
            "target_dimension_code": updates.get("target_dimension_code", row["target_dimension_code"]),
            "target_tag_code": updates.get("target_tag_code", row["target_tag_code"]),
            "mode": updates.get("mode", row["mode"]),
            "applies_to_descendants": updates.get("applies_to_descendants", row["applies_to_descendants"]),
            "note": updates.get("note", row["note"]),
            "is_active": updates.get("is_active", row["is_active"]),
        }
        validate_taxonomy_applicability_tags(db, next_rule)
        duplicate = db.execute(
            """
            SELECT id FROM tag_applicability_rules
            WHERE id <> ?
              AND category_dimension_code = ? AND category_tag_code = ?
              AND target_dimension_code = ? AND target_tag_code = ?
            """,
            (
                rule_id,
                next_rule["category_dimension_code"],
                next_rule["category_tag_code"],
                next_rule["target_dimension_code"],
                next_rule["target_tag_code"],
            ),
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail="适用规则已存在")
        updates["updated_at"] = now_iso()
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        db.execute(f"UPDATE tag_applicability_rules SET {set_clause} WHERE id = ?", (*updates.values(), rule_id))
        log_event(db, "taxonomy_applicability_rule", rule_id, "updated", updates)
    return get_taxonomy_rules(request)


@app.delete("/api/taxonomy/applicability-rules/{rule_id}")
def delete_taxonomy_applicability_rule(rule_id: int, request: Request) -> dict[str, Any]:
    require_admin(request)
    with get_db() as db:
        row = db.execute("SELECT * FROM tag_applicability_rules WHERE id = ?", (rule_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="适用规则不存在")
        db.execute("DELETE FROM tag_applicability_rules WHERE id = ?", (rule_id,))
        log_event(db, "taxonomy_applicability_rule", rule_id, "deleted", serialize_tag_applicability_rule(row))
    return get_taxonomy_rules(request)


@app.post("/api/taxonomy/backfill-assignments")
def backfill_taxonomy_assignments(request: Request) -> dict[str, Any]:
    require_admin(request)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, ai_tags_json, official_title_tags_json, manual_tags_json, tag_meta_json
            FROM styling_garments
            WHERE deleted_at IS NULL
            """
        ).fetchall()
        for row in rows:
            replace_garment_tag_assignments(
                db,
                int(row["id"]),
                loads(row["ai_tags_json"], {}),
                loads(row["official_title_tags_json"], {}),
                loads(row["manual_tags_json"], {}),
                loads(row["tag_meta_json"], {}),
            )
        log_event(db, "taxonomy", None, "assignments_backfilled", {"garmentCount": len(rows)})
    return {"ok": True, "garmentCount": len(rows)}


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
        total = int(db.execute(
            """
            SELECT COUNT(*) AS total FROM model_profiles
            WHERE (? IS NULL OR model_id LIKE ? OR name LIKE ?)
            """,
            (search, search, search),
        ).fetchone()["total"])
        items = rows[:limit]
        return {
            "items": [serialize_model(row) for row in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if offset + limit < total else None,
            "hasMore": offset + limit < total,
            "total": total,
            "totalPages": (total + limit - 1) // limit if total else 0,
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
    media_keys = registered_media_keys(object_key)
    if not media_keys:
        raise HTTPException(status_code=404, detail="图片不存在")
    object_key = media_keys["object_key"]
    target_key = object_key
    if variant in {"thumb", "preview"}:
        registered_variant_key = media_keys.get(f"{variant}_key")
        if registered_variant_key and object_exists(registered_variant_key):
            target_key = registered_variant_key
        else:
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


def registered_media_keys(object_key: str) -> dict[str, str | None] | None:
    with get_db() as db:
        asset = db.execute(
            """
            SELECT object_key, thumb_key, preview_key FROM media_assets
            WHERE object_key = ? OR thumb_key = ? OR preview_key = ?
            """,
            (object_key, object_key, object_key),
        ).fetchone()
        if asset:
            return {
                "object_key": clean_key(asset["object_key"]),
                "thumb_key": clean_key(asset["thumb_key"]) if asset["thumb_key"] else None,
                "preview_key": clean_key(asset["preview_key"]) if asset["preview_key"] else None,
            }
        model_reference = registered_model_reference_keys(db, object_key)
        if model_reference:
            return model_reference
        legacy_garment = db.execute(
            """
            SELECT storage_path, thumb_path, preview_path FROM garment_images
            WHERE storage_path = ? OR thumb_path = ? OR preview_path = ?
            """,
            (object_key, object_key, object_key),
        ).fetchone()
        if legacy_garment:
            return {
                "object_key": clean_key(legacy_garment["storage_path"]),
                "thumb_key": clean_key(legacy_garment["thumb_path"]) if legacy_garment["thumb_path"] else None,
                "preview_key": clean_key(legacy_garment["preview_path"]) if legacy_garment["preview_path"] else None,
            }
        legacy_generation = db.execute(
            """
            SELECT output_path, thumb_path, preview_path FROM generation_images
            WHERE output_path = ? OR thumb_path = ? OR preview_path = ?
            """,
            (object_key, object_key, object_key),
        ).fetchone()
        if legacy_generation:
            return {
                "object_key": clean_key(legacy_generation["output_path"]),
                "thumb_key": clean_key(legacy_generation["thumb_path"]) if legacy_generation["thumb_path"] else None,
                "preview_key": clean_key(legacy_generation["preview_path"]) if legacy_generation["preview_path"] else None,
            }
    return None


def registered_model_reference_keys(db: Any, object_key: str) -> dict[str, str | None] | None:
    rows = db.execute("SELECT asset_dir, profile_json FROM model_profiles").fetchall()
    for row in rows:
        profile = loads(row["profile_json"], {})
        references = profile.get("headReferences") if isinstance(profile.get("headReferences"), dict) else {}
        for filename in references.values():
            if not filename:
                continue
            original_key = join_key(row["asset_dir"], sanitize_filename(str(filename)))
            thumb_key = derivative_key(original_key, "thumb")
            preview_key = derivative_key(original_key, "preview")
            if object_key in {original_key, thumb_key, preview_key}:
                return {
                    "object_key": original_key,
                    "thumb_key": thumb_key,
                    "preview_key": preview_key,
                }
    return None


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
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        if key in row.keys():
            return row[key]
    except (AttributeError, KeyError, TypeError):
        pass
    return default


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
            refresh_pending_new_generation_jobs()
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
            "provider": settings.image_provider,
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


def get_current_brand_info() -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM brand_profiles WHERE id = 1").fetchone()
        return serialize_brand_info(row) if row else None


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
    saved_object_keys: list[str] = []
    saved_asset_ids: list[int] = []
    try:
        for index, upload in enumerate(files):
            content, content_type = await validate_upload_image(upload)
            filename = sanitize_filename(upload.filename or "image")
            key = unique_key(join_key(prefix, f"{index + 1:03d}-{filename}"))
            put_fileobj(key, BytesIO(content), content_type)
            saved_object_keys.append(key)
            derivative = generate_image_derivatives(key, content)
            saved_object_keys.extend([item for item in (derivative.get("thumb"), derivative.get("preview")) if item])
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
            saved_asset_ids.append(int(asset["id"]))
            saved.append(asset)
    except Exception:
        if saved_asset_ids:
            with suppress(Exception):
                with get_db() as db:
                    db.execute(
                        f"DELETE FROM media_assets WHERE id IN ({','.join('?' for _ in saved_asset_ids)})",
                        saved_asset_ids,
                    )
        cleanup_objects(saved_object_keys)
        raise
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
        "originalFilename": row["original_filename"],
        "originalMimeType": row["mime_type"],
        "originalWidth": row["width"],
        "originalHeight": row["height"],
        "originalSizeBytes": row["size_bytes"],
        "thumbSizeBytes": row["thumb_size_bytes"] if "thumb_size_bytes" in row.keys() else None,
        "previewSizeBytes": row["preview_size_bytes"] if "preview_size_bytes" in row.keys() else None,
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
        "archivedAt": row["archived_at"] if "archived_at" in row.keys() else None,
        "archiveReason": row["archive_reason"] if "archive_reason" in row.keys() else None,
        "imageCount": row["image_count"] if "image_count" in row.keys() else None,
        "coverUrl": stable_media_url(row["cover_key"], "thumb") if "cover_key" in row.keys() else None,
        "coverAsset": serialize_cover_asset(row),
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


def serialize_cover_asset(row: Any) -> dict[str, Any] | None:
    if "cover_key" not in row.keys() or not row["cover_key"]:
        return None
    return {
        "id": row["cover_asset_id"] if "cover_asset_id" in row.keys() else row["id"],
        "objectKey": row["cover_key"],
        "filename": row["cover_filename"] if "cover_filename" in row.keys() and row["cover_filename"] else row["code"],
        "mimeType": row["cover_mime_type"] if "cover_mime_type" in row.keys() and row["cover_mime_type"] else "",
        "width": row["cover_width"] if "cover_width" in row.keys() else None,
        "height": row["cover_height"] if "cover_height" in row.keys() else None,
        "sizeBytes": row["cover_size_bytes"] if "cover_size_bytes" in row.keys() else None,
        "originalFilename": row["cover_filename"] if "cover_filename" in row.keys() and row["cover_filename"] else row["code"],
        "originalMimeType": row["cover_mime_type"] if "cover_mime_type" in row.keys() and row["cover_mime_type"] else "",
        "originalWidth": row["cover_width"] if "cover_width" in row.keys() else None,
        "originalHeight": row["cover_height"] if "cover_height" in row.keys() else None,
        "originalSizeBytes": row["cover_size_bytes"] if "cover_size_bytes" in row.keys() else None,
        "url": stable_media_url(row["cover_key"], "preview"),
        "thumbUrl": stable_media_url(row["cover_key"], "thumb"),
        "previewUrl": stable_media_url(row["cover_key"], "preview"),
        "originalUrl": stable_media_url(row["cover_key"], "original"),
    }


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
            "errorCode": row["error_code"] if "error_code" in row.keys() else None,
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
    prompt = build_taxonomy_tagging_prompt(garment)
    request_snapshot = build_taxonomy_tagging_request_snapshot(garment, prompt)
    if not settings.gemini_api_key:
        attempt_id = create_attempt("styling_garment_tagging", garment_id, "gemini", request_snapshot)
        finish_attempt(attempt_id, "FAILED", {"error": "gemini_api_key_missing"}, error_message="Gemini API 未配置，AI 打标不能生成替代标签。")
        raise RuntimeError("Gemini API 未配置，AI 打标不能生成替代标签。")
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        attempt_id = create_attempt("styling_garment_tagging", garment_id, "gemini", request_snapshot)
        finish_attempt(attempt_id, "FAILED", {"error": "google_genai_import_failed"}, error_message="google-genai SDK 不可用，AI 打标不能生成替代标签。")
        raise RuntimeError("google-genai SDK 不可用，AI 打标不能生成替代标签。") from exc
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
        raw = parse_lily_tagging_json(text)
    except Exception as exc:
        finish_attempt(attempt_id, "FAILED", {"raw_text": text}, error_message=f"Gemini 返回内容不是合法 JSON：{exc}")
        raise
    tagging_result = normalize_prompt_tagging_result(raw, garment)
    finish_attempt(attempt_id, "SUCCEEDED", {"raw_text": text, "raw_json": raw})
    return tagging_result


def build_taxonomy_tagging_request_snapshot(garment: dict[str, Any], prompt: str, taxonomy_context: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = {
        "schema_version": PROMPT_TAGGING_RESULT_VERSION,
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
    if taxonomy_context:
        snapshot["taxonomy"] = {
            "dimension_count": len(taxonomy_context["dimensions"]),
            "tag_count": len(taxonomy_context["tags"]),
            "relation_count": len(taxonomy_context["relations"]),
            "applicability_rule_count": len(taxonomy_context["applicabilityRules"]),
            "content_hash": taxonomy_context.get("contentHash"),
        }
    return snapshot


def build_taxonomy_tagging_prompt(garment: dict[str, Any] | None = None, taxonomy_context: dict[str, Any] | None = None) -> str:
    return render_prompt_template("assembly.ai_tagging", {})


def fallback_taxonomy_tagging_result(garment: dict[str, Any], taxonomy_context: dict[str, Any] | None = None) -> dict[str, Any]:
    return normalize_prompt_tagging_result(
        {
            "一级品类": ["未知"],
            "整体视觉语义": f"{garment.get('code', '该服饰')} 待重新识别。",
            "置信度": 50,
            "风险点": "Gemini 未配置或不可用，无法完成图片判断。",
        },
        garment,
        provider="fallback",
        warnings=["Gemini 未配置或不可用，未生成完整标签"],
        review_reasons=["fallback_tags"],
    )


TAG_PROMPT_FIELD_ALIASES = {
    "category": ["一级品类", "品类", "主品类", "category"],
    "sub_category": ["二级品类", "细分品类", "子品类", "sub_category", "subcategory"],
    "colors": ["色彩", "颜色", "主色", "color", "colors"],
    "color_attributes": ["色调", "色彩属性", "颜色氛围", "color_attribute", "color_attributes"],
    "materials_visual": ["面料材质", "视觉材质", "材质", "material_visual", "materials_visual"],
    "surface_texture": ["表面肌理", "肌理", "surface_texture"],
    "fit": ["版型", "fit"],
    "silhouette": ["廓形", "silhouette"],
    "structure": ["结构", "structure"],
    "length": ["长度", "衣长", "裤长", "裙长", "length"],
    "neckline": ["领型", "neckline"],
    "sleeve": ["袖型与袖长", "袖长", "袖型", "sleeve"],
    "shoulder": ["肩部版型", "肩部", "shoulder"],
    "trouser_shape": ["裤型", "trouser_shape"],
    "skirt_shape": ["裙型", "skirt_shape"],
    "waist_height": ["腰高", "waist_height"],
    "waist_design": ["腰线设计", "waist_design"],
    "closure": ["闭合方式", "closure"],
    "details": ["设计细节", "细节", "detail", "details"],
    "patterns": ["图案", "pattern", "patterns"],
    "wearing_methods": ["穿法", "wearing_method", "wearing_methods"],
    "wearability": ["穿着能力", "wearability"],
    "occasion": ["适用场景", "场景", "occasion"],
    "season": ["季节", "season"],
    "overall_summary": ["整体视觉语义", "整体视觉描述", "整体描述", "overall_summary"],
    "structure_summary": ["结构/廓形语义", "结构廓形语义", "structure_summary"],
    "material_summary": ["材质/肌理语义", "材质肌理语义", "material_summary"],
    "color_summary": ["颜色/氛围语义", "颜色氛围语义", "color_summary"],
    "style_summary": ["风格语义", "风格描述", "style_summary"],
    "confidence": ["置信度", "confidence", "overall_confidence"],
    "risk": ["风险点", "风险", "risk", "risks"],
}

def normalize_prompt_tagging_result(
    raw: Any,
    garment: dict[str, Any],
    *,
    provider: str = "gemini",
    warnings: list[str] | None = None,
    review_reasons: list[str] | None = None,
) -> dict[str, Any]:
    ai_tags = raw if isinstance(raw, dict) else {"raw_result": raw}
    tag_meta = build_prompt_tag_meta(ai_tags, garment, provider=provider, warnings=warnings, review_reasons=review_reasons)
    return {
        "schema_version": str(ai_tags.get("schema_version") or ai_tags.get("schemaVersion") or PROMPT_TAGGING_RESULT_VERSION),
        "ai_tags": ai_tags,
        "official_title_tags": ai_tags.get("official_title_tags") if isinstance(ai_tags.get("official_title_tags"), dict) else {},
        "tag_meta": tag_meta,
    }


def build_prompt_tag_meta(
    ai_tags: dict[str, Any],
    garment: dict[str, Any],
    *,
    provider: str,
    warnings: list[str] | None = None,
    review_reasons: list[str] | None = None,
) -> dict[str, Any]:
    source_meta = ai_tags.get("tag_meta") if isinstance(ai_tags.get("tag_meta"), dict) else {}
    confidence = extract_prompt_confidence(ai_tags)
    reasons = ensure_string_list(review_reasons)
    if not has_meaningful_prompt_result(ai_tags):
        reasons.append("empty_result")
    if confidence is not None and confidence < TAG_REVIEW_CONFIDENCE_THRESHOLD:
        reasons.append("low_confidence")
    if provider == "fallback" and "fallback_tags" not in reasons:
        reasons.append("fallback_tags")
    reasons = list(dict.fromkeys(reasons))
    return {
        "tagger_provider": str(source_meta.get("tagger_provider") or provider),
        "tagger_model": str(source_meta.get("tagger_model") or ("fallback" if provider == "fallback" else settings.gemini_model)),
        "result_format": str(source_meta.get("result_format") or "prompt_configured_json"),
        "overall_confidence": confidence if confidence is not None else 0.5,
        "needs_review": bool(reasons),
        "review_reasons": reasons,
        "source_image_ids": [str(image.get("id")) for image in garment.get("images", [])],
        "warnings": ensure_string_list(warnings),
    }


def has_meaningful_prompt_result(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, list):
        return any(has_meaningful_prompt_result(item) for item in value)
    if isinstance(value, dict):
        return any(has_meaningful_prompt_result(item) for item in value.values())
    return True


def prompt_field_value(source: dict[str, Any], canonical_key: str) -> Any:
    aliases = set(TAG_PROMPT_FIELD_ALIASES.get(canonical_key, [canonical_key]))
    return prompt_field_value_recursive(source, aliases)


def prompt_field_value_recursive(value: Any, aliases: set[str]) -> Any:
    if isinstance(value, dict):
        for key in aliases:
            if key in value and value[key] not in (None, ""):
                return value[key]
        for nested in value.values():
            found = prompt_field_value_recursive(nested, aliases)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = prompt_field_value_recursive(item, aliases)
            if found not in (None, ""):
                return found
    return None


def extract_prompt_confidence(source: dict[str, Any]) -> float | None:
    value = prompt_field_value(source, "confidence")
    if value is None and isinstance(source.get("tag_meta"), dict):
        value = source["tag_meta"].get("overall_confidence")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1:
        number = number / 100
    return max(0.0, min(1.0, number))


def prompt_value_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip()[:120] for item in value if str(item).strip()]
    if isinstance(value, dict):
        values: list[str] = []
        for nested in value.values():
            values.extend(prompt_value_list(nested))
        return values
    text = str(value).strip()
    if not text:
        return []
    return [item.strip()[:120] for item in re.split(r"[,，/、;；]", text) if item.strip()]


def first_prompt_value(value: Any, fallback: str = "unknown") -> str:
    values = prompt_value_list(value)
    return values[0] if values else fallback


def prompt_code_values(value: Any) -> list[str]:
    return prompt_value_list(value)


def first_prompt_code(value: Any, fallback: str = "unknown") -> str:
    values = prompt_code_values(value)
    return values[0] if values else fallback


def prompt_text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "，".join(str(item).strip() for item in value if str(item).strip())[:240]
    if isinstance(value, dict):
        return dumps(value)[:240]
    return str(value).strip()[:240]


def normalize_taxonomy_tagging_result(raw: dict[str, Any], garment: dict[str, Any], taxonomy_context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    tags_value = raw.get("tags")
    if not isinstance(tags_value, list) and isinstance(raw.get("ai_tags"), dict):
        tags_value = raw["ai_tags"].get("tags")
    normalized_tags, validation = normalize_taxonomy_tag_entries(tags_value, taxonomy_context)
    visual_profile = normalize_visual_profile(raw.get("visual_profile"))
    official_title_tags = normalize_official_title_tags(raw.get("official_title_tags"))
    unmapped = normalize_unmapped_observations(raw.get("unmapped_observations"))
    suggestions = normalize_suggested_new_tags(raw.get("suggested_new_tags"))
    tag_meta = normalize_taxonomy_tag_meta(raw.get("tag_meta"), garment, validation, unmapped, suggestions, normalized_tags)
    return {
        "schema_version": TAXONOMY_TAGGING_SCHEMA_VERSION,
        "tags": normalized_tags,
        "tags_by_dimension": group_taxonomy_tags_by_dimension(normalized_tags),
        "visual_profile": visual_profile,
        "unmapped_observations": unmapped,
        "suggested_new_tags": suggestions,
        "validation": validation,
        "label_summary": taxonomy_label_summary(normalized_tags, taxonomy_context),
        "official_title_tags": official_title_tags,
        "tag_meta": tag_meta,
    }


def normalize_taxonomy_tag_entries(value: Any, taxonomy_context: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    active_tags = taxonomy_context["tagByKey"]
    result: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_item in value if isinstance(value, list) else []:
        if not isinstance(raw_item, dict):
            invalid.append({"reason": "标签项不是对象", "raw": raw_item})
            continue
        dimension_code = sanitize_name(str(raw_item.get("dimension_code") or raw_item.get("dimensionCode") or ""))
        tag_code = sanitize_name(str(raw_item.get("tag_code") or raw_item.get("tagCode") or ""))
        tag_def = active_tags.get((dimension_code, tag_code))
        if not tag_def or not tag_def.get("isSelectable"):
            invalid.append({"reason": "非法或不可直接打标的标签引用", "dimension_code": dimension_code, "tag_code": tag_code})
            continue
        value_type = str(raw_item.get("value_type") or raw_item.get("valueKind") or tag_def.get("valueKind") or "tag").strip()
        expected_type = str(tag_def.get("valueKind") or "tag")
        if value_type != expected_type:
            value_type = expected_type
        item: dict[str, Any] = {
            "dimension_code": dimension_code,
            "tag_code": tag_code,
            "value_type": value_type,
            "score": None,
            "value_text": None,
            "value_bool": None,
            "confidence": clamp_float(raw_item.get("confidence"), 0.0, 1.0, 0.0),
            "evidence": str(raw_item.get("evidence") or raw_item.get("note") or "")[:240],
        }
        if value_type == "score":
            item["score"] = clamp_float(raw_item.get("score"), 0.0, 1.0, 0.0)
        elif value_type == "boolean":
            item["value_bool"] = bool(raw_item.get("value_bool", raw_item.get("valueBool", True)))
        elif value_type == "text":
            item["value_text"] = str(raw_item.get("value_text") or raw_item.get("valueText") or "")[:240]
        key = (dimension_code, tag_code, value_type)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)

    validation = validate_taxonomy_tag_entries(result, taxonomy_context)
    if invalid:
        validation["invalidTagReferences"] = invalid[:20]
        validation["reviewReasons"].append("invalid_tag_reference")
    validation["reviewReasons"] = list(dict.fromkeys(validation["reviewReasons"]))
    return result, validation


def validate_taxonomy_tag_entries(tags: list[dict[str, Any]], taxonomy_context: dict[str, Any]) -> dict[str, Any]:
    tag_by_key = taxonomy_context["tagByKey"]
    selected_keys = {(item["dimension_code"], item["tag_code"]) for item in tags}
    category_keys = [key for key in selected_keys if key[0] == "category"]
    review_reasons: list[str] = []
    disallowed: list[dict[str, Any]] = []
    missing_required: list[dict[str, Any]] = []
    if not category_keys:
        review_reasons.append("unclear_category")
    for category_key in category_keys:
        for rule in taxonomy_context["applicabilityRules"]:
            if not taxonomy_rule_matches_category(rule, category_key, tag_by_key):
                continue
            target_key = (rule["targetDimensionCode"], rule["targetTagCode"])
            if rule["mode"] == "disallowed" and target_key in selected_keys:
                review_reasons.append("disallowed_tag")
                disallowed.append(rule)
            if rule["mode"] == "required" and target_key not in selected_keys:
                review_reasons.append("missing_required_rule")
                missing_required.append(rule)
    return {
        "reviewReasons": review_reasons,
        "disallowedRules": disallowed[:20],
        "missingRequiredRules": missing_required[:20],
    }


def taxonomy_rule_matches_category(rule: dict[str, Any], category_key: tuple[str, str], tag_by_key: dict[tuple[str, str], dict[str, Any]]) -> bool:
    rule_key = (rule["categoryDimensionCode"], rule["categoryTagCode"])
    if rule_key == category_key:
        return True
    if not rule.get("appliesToDescendants"):
        return False
    current = tag_by_key.get(category_key)
    while current and current.get("parentCode"):
        parent_key = (current["dimensionCode"], current["parentCode"])
        if parent_key == rule_key:
            return True
        current = tag_by_key.get(parent_key)
    return False


def normalize_visual_profile(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "overall_summary": str(source.get("overall_summary") or source.get("overallSummary") or "")[:240],
        "key_parts": ensure_string_list(source.get("key_parts") or source.get("keyParts"))[:12],
        "styling_interpretation": str(source.get("styling_interpretation") or source.get("stylingInterpretation") or "")[:240],
        "pairing_suggestion_text": str(source.get("pairing_suggestion_text") or source.get("pairingSuggestionText") or "")[:240],
        "avoid_pairing_text": str(source.get("avoid_pairing_text") or source.get("avoidPairingText") or "")[:240],
        "uncertain_points": ensure_string_list(source.get("uncertain_points") or source.get("uncertainPoints"))[:12],
    }


def normalize_unmapped_observations(value: Any) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("label_zh") or item.get("labelZh") or "").strip()
            if text:
                observations.append({
                    "text": text[:120],
                    "evidence": str(item.get("evidence") or "")[:240],
                    "confidence": clamp_float(item.get("confidence"), 0.0, 1.0, 0.0),
                })
        elif str(item).strip():
            observations.append({"text": str(item).strip()[:120], "evidence": "", "confidence": 0.0})
    return observations[:20]


def normalize_suggested_new_tags(value: Any) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label_zh") or item.get("labelZh") or item.get("label") or "").strip()
        if not label:
            continue
        suggestions.append({
            "dimension_code": sanitize_name(str(item.get("dimension_code") or item.get("dimensionCode") or ""))[:80],
            "label_zh": label[:80],
            "reason": str(item.get("reason") or "")[:240],
        })
    return suggestions[:20]


def normalize_taxonomy_tag_meta(
    value: Any,
    garment: dict[str, Any],
    validation: dict[str, Any],
    unmapped: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
    tags: list[dict[str, Any]],
) -> dict[str, Any]:
    meta = value if isinstance(value, dict) else {}
    confidence = clamp_float(meta.get("overall_confidence"), 0.0, 1.0, 0.5)
    reasons = [reason for reason in ensure_string_list(meta.get("review_reasons")) if reason in TAXONOMY_REVIEW_REASONS]
    reasons.extend(reason for reason in validation.get("reviewReasons", []) if reason in TAXONOMY_REVIEW_REASONS)
    if confidence < TAG_REVIEW_CONFIDENCE_THRESHOLD:
        reasons.append("low_confidence")
    if unmapped or suggestions:
        reasons.append("unmapped_observation")
    selected_category_codes = [item["tag_code"] for item in tags if item["dimension_code"] == "category"]
    if "未知" in selected_category_codes or "unknown" in selected_category_codes:
        reasons.append("unclear_category")
    if "不支持入库配饰" in selected_category_codes or "unsupported_accessory" in selected_category_codes:
        reasons.append("unsupported_accessory")
    reasons = list(dict.fromkeys(reasons))
    return {
        "tagger_provider": str(meta.get("tagger_provider") or "gemini")[:40],
        "tagger_model": str(meta.get("tagger_model") or settings.gemini_model)[:80],
        "schema_version": TAXONOMY_TAGGING_SCHEMA_VERSION,
        "overall_confidence": confidence,
        "field_confidence": meta.get("field_confidence") if isinstance(meta.get("field_confidence"), dict) else {},
        "needs_review": bool(meta.get("needs_review")) or bool(reasons),
        "review_reasons": reasons,
        "source_image_ids": ensure_string_list(meta.get("source_image_ids")) or [str(image.get("id")) for image in garment.get("images", [])],
        "warnings": ensure_string_list(meta.get("warnings")),
    }


def group_taxonomy_tags_by_dimension(tags: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in tags:
        grouped.setdefault(item["dimension_code"], []).append(item)
    return grouped


def taxonomy_label_summary(tags: list[dict[str, Any]], taxonomy_context: dict[str, Any]) -> dict[str, list[str]]:
    tag_by_key = taxonomy_context["tagByKey"]
    summary: dict[str, list[str]] = {}
    for item in tags:
        tag_def = tag_by_key.get((item["dimension_code"], item["tag_code"]))
        label = tag_def.get("labelZh") if tag_def else item["tag_code"]
        summary.setdefault(item["dimension_code"], []).append(str(label))
    return summary


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


def tagging_status_for_result(ai_tags: dict[str, Any], tag_meta: dict[str, Any]) -> str:
    if tag_meta.get("needs_review") or tag_meta.get("tagger_provider") == "fallback":
        return "TAGGING_READY"
    if not has_meaningful_prompt_result(ai_tags):
        return "TAGGING_READY"
    return "READY"


def apply_tag_review_policy(ai_tags: dict[str, Any], tag_meta: dict[str, Any]) -> dict[str, Any]:
    reasons = ensure_string_list(tag_meta.get("review_reasons")) if isinstance(tag_meta, dict) else []
    confidence = tag_meta.get("overall_confidence") if isinstance(tag_meta, dict) else None
    inferred_confidence = extract_prompt_confidence(ai_tags) if isinstance(ai_tags, dict) else None
    if inferred_confidence is not None:
        confidence = inferred_confidence
    if not has_meaningful_prompt_result(ai_tags) and "empty_result" not in reasons:
        reasons.append("empty_result")
    if confidence is not None and clamp_float(confidence, 0.0, 1.0, 1.0) < TAG_REVIEW_CONFIDENCE_THRESHOLD and "low_confidence" not in reasons:
        reasons.append("low_confidence")
    if tag_meta.get("tagger_provider") == "fallback" and "fallback_tags" not in reasons:
        reasons.append("fallback_tags")
    needs_review = bool(tag_meta.get("needs_review")) or bool(reasons)
    return {**tag_meta, "overall_confidence": confidence if confidence is not None else tag_meta.get("overall_confidence"), "needs_review": needs_review, "review_reasons": list(dict.fromkeys(reasons))}


def build_effective_tags(ai_tags: dict[str, Any], official_title_tags: dict[str, Any], manual_tags: dict[str, Any]) -> dict[str, Any]:
    return ai_tags if isinstance(ai_tags, dict) else {}


def taxonomy_codes_for_dimension(ai_tags: dict[str, Any], dimension_code: str) -> list[str]:
    return [
        item.get("tag_code")
        for item in ai_tags.get("tags", [])
        if isinstance(item, dict) and item.get("dimension_code") == dimension_code and item.get("tag_code")
    ]


def taxonomy_codes(entries: list[dict[str, Any]] | None) -> list[str]:
    return [str(item.get("tag_code")) for item in entries or [] if item.get("tag_code")]


def first_taxonomy_code(entries: list[dict[str, Any]] | None, fallback: str) -> str:
    values = taxonomy_codes(entries)
    return values[0] if values else fallback


def derive_effective_category(category_entries: list[dict[str, Any]], taxonomy_context: dict[str, Any]) -> tuple[str, str]:
    tag_by_key = taxonomy_context["tagByKey"]
    selected = [
        tag_by_key.get(("category", item["tag_code"]))
        for item in category_entries
        if item.get("tag_code")
    ]
    selected = [item for item in selected if item]
    if not selected:
        return "未知", "other"
    selected.sort(key=lambda item: int(item.get("level") or 1), reverse=True)
    leaf = selected[0]
    parent_code = leaf.get("parentCode")
    if parent_code:
        return str(parent_code), str(leaf["code"])
    return str(leaf["code"]), "other"


def derive_effective_lengths(entries: list[dict[str, Any]] | None) -> dict[str, str]:
    result = {"top_length": "unknown", "sleeve_length": "unknown", "bottom_length": "unknown"}
    for code in taxonomy_codes(entries):
        if code.startswith("top_"):
            result["top_length"] = code.removeprefix("top_")
        elif code.startswith("sleeve_"):
            result["sleeve_length"] = code.removeprefix("sleeve_")
        elif code.startswith("bottom_"):
            result["bottom_length"] = code.removeprefix("bottom_")
        elif code == "unknown":
            continue
    return result


def replace_garment_tag_assignments(
    db: Any,
    garment_id: int,
    ai_tags: dict[str, Any],
    official_title_tags: dict[str, Any],
    manual_tags: dict[str, Any],
    tag_meta: dict[str, Any],
) -> None:
    timestamp = now_iso()
    defs = taxonomy_def_keys(db)
    status = "pending" if tag_meta.get("needs_review") else "confirmed"
    confidence = clamp_float(tag_meta.get("overall_confidence"), 0.0, 1.0, 0.0) if tag_meta else None
    db.execute("DELETE FROM garment_tag_assignments WHERE garment_id = ?", (garment_id,))

    def add(
        dimension_code: str,
        tag_code: Any,
        *,
        source: str = "ai",
        value_kind: str = "tag",
        value_number: float | None = None,
        value_text: str | None = None,
        value_bool: bool | None = None,
        item_status: str = status,
        evidence: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> None:
        code = str(tag_code or "").strip()
        if not code or (dimension_code, code) not in defs:
            return
        db.execute(
            """
            INSERT INTO garment_tag_assignments
              (garment_id, dimension_code, tag_code, value_kind, value_number, value_text, value_bool,
               confidence, source, status, evidence_json, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(garment_id, dimension_code, tag_code, source) DO UPDATE SET
              value_kind = excluded.value_kind,
              value_number = excluded.value_number,
              value_text = excluded.value_text,
              value_bool = excluded.value_bool,
              confidence = excluded.confidence,
              status = excluded.status,
              evidence_json = excluded.evidence_json,
              note = excluded.note,
              updated_at = excluded.updated_at
            """,
            (
                garment_id,
                dimension_code,
                code,
                value_kind,
                value_number,
                value_text,
                1 if value_bool else 0 if value_bool is not None else None,
                confidence,
                source,
                item_status,
                dumps(evidence or {}),
                note,
                timestamp,
                timestamp,
            ),
        )

    for item in ai_tags.get("tags", []) if isinstance(ai_tags.get("tags"), list) else []:
        if not isinstance(item, dict):
            continue
        add(
            str(item.get("dimension_code") or ""),
            item.get("tag_code"),
            source="ai",
            value_kind=str(item.get("value_type") or "tag"),
            value_number=item.get("score") if item.get("value_type") == "score" else None,
            value_text=str(item.get("value_text") or "")[:240] if item.get("value_type") == "text" else None,
            value_bool=bool(item.get("value_bool")) if item.get("value_type") == "boolean" else None,
            evidence={"evidence": item.get("evidence")},
        )
    for item in ensure_string_list(tag_meta.get("review_reasons") if isinstance(tag_meta, dict) else []):
        add("review_reason", item, source="system", item_status="confirmed")
    if isinstance(manual_tags, dict):
        for key, value in manual_tags.items():
            if isinstance(value, list):
                text = "，".join(str(item) for item in value if str(item).strip())
            elif isinstance(value, bool):
                add("manual_business", key, source="manual", value_kind="boolean", value_bool=value, item_status="confirmed")
                continue
            else:
                text = str(value or "").strip()
            if text:
                add("manual_business", key, source="manual", value_kind="text", value_text=text[:240], item_status="confirmed")
    if official_title_tags:
        raw_title = str(official_title_tags.get("raw_title") or "").strip()
        if raw_title:
            add("manual_business", "notes", source="official", value_kind="text", value_text=raw_title[:240], item_status="confirmed", note="官方标题原文")


def taxonomy_def_keys(db: Any) -> set[tuple[str, str]]:
    rows = db.execute("SELECT dimension_code, code FROM tag_defs WHERE is_active = 1").fetchall()
    return {(str(row["dimension_code"]), str(row["code"])) for row in rows}


def compress_tags_for_styling(row: Any) -> dict[str, Any]:
    ai_tags = loads(row["ai_tags_json"], {}) if "ai_tags_json" in row.keys() else {}
    manual_tags = loads(row["manual_tags_json"], {}) if "manual_tags_json" in row.keys() else {}
    official_title_tags = loads(row["official_title_tags_json"], {}) if "official_title_tags_json" in row.keys() else {}
    tag_meta = loads(row["tag_meta_json"], {}) if "tag_meta_json" in row.keys() else {}
    effective = loads(row["effective_tags_json"], {}) if "effective_tags_json" in row.keys() else {}
    if not effective:
        effective = build_effective_tags(ai_tags, official_title_tags, manual_tags)
    cover_asset = serialize_cover_asset(row) if "cover_key" in row.keys() else None
    return {
        "id": row["id"],
        "code": row["code"],
        "manual": manual_tags,
        "tag_facts": {
            "ai_tags": ai_tags,
            "manual_tags": manual_tags,
            "official_title_tags": official_title_tags,
            "effective_tags": effective,
            "tag_meta": tag_meta,
        },
        "cover_asset": cover_asset,
    }


def library_tag_card_for_prompt(card: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in card.items() if key != "cover_asset"}


def build_phase_one_visual_caption(effective: dict[str, Any]) -> str:
    parts = [
        str(effective.get("category") or ""),
        "/".join(str(color) for color in (effective.get("colors") if isinstance(effective.get("colors"), list) else [])[:3]),
        str(effective.get("fit") or ""),
        "/".join(str(item) for item in (effective.get("materials_visual") if isinstance(effective.get("materials_visual"), list) else [])[:3]),
        str(effective.get("pairing_brief") or ""),
    ]
    return "，".join(part for part in parts if part)[:160]


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


def parse_lily_tagging_json(text: str) -> dict[str, Any]:
    json_text = extract_json_text(text)
    try:
        parsed = loads(json_text, {})
    except json.JSONDecodeError as strict_error:
        relaxed_json_text = remove_json_trailing_commas(json_text)
        if relaxed_json_text != json_text:
            try:
                parsed = loads(relaxed_json_text, {})
            except json.JSONDecodeError:
                parsed = None
            else:
                if isinstance(parsed, dict):
                    return parsed
        decoder = json.JSONDecoder()
        try:
            parsed, end_index = decoder.raw_decode(json_text)
        except json.JSONDecodeError:
            raise strict_error from None
        trailing = json_text[end_index:].strip()
        if trailing and not is_recoverable_json_trailing_noise(trailing):
            raise strict_error from None
    if not isinstance(parsed, dict):
        return {"raw_result": parsed}
    return parsed


def remove_json_trailing_commas(value: str) -> str:
    previous = value
    while True:
        next_value = re.sub(r",\s*([}\]])", r"\1", previous)
        if next_value == previous:
            return next_value
        previous = next_value


def is_recoverable_json_trailing_noise(value: str) -> bool:
    if not value:
        return True
    return set(value) <= {"}"}


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


def update_attempt_request_snapshot(attempt_id: int, request_snapshot: dict[str, Any]) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE generation_attempts SET request_snapshot_json = ?, updated_at = ? WHERE id = ?",
            (dumps(request_snapshot), now_iso(), attempt_id),
        )


def generation_task_or_attempt_cancelled(task_id: int, attempt_id: int) -> bool:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gt.status AS task_status, ga.status AS attempt_status
            FROM generation_tasks gt
            LEFT JOIN generation_attempts ga ON ga.id = ?
            WHERE gt.id = ?
            """,
            (attempt_id, task_id),
        ).fetchone()
    return not row or row["task_status"] == "CANCELLED" or row["attempt_status"] == "CANCELLED"


def shoot_script_generation_cancelled(task_id: int, shoot_script_id: int, attempt_id: int) -> bool:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gt.status AS task_status,
                   gss.status AS shoot_script_status,
                   ga.status AS attempt_status
            FROM generation_tasks gt
            LEFT JOIN generation_shoot_scripts gss ON gss.id = ?
            LEFT JOIN generation_attempts ga ON ga.id = ?
            WHERE gt.id = ?
            """,
            (shoot_script_id, attempt_id, task_id),
        ).fetchone()
    return (
        not row
        or row["task_status"] == "CANCELLED"
        or row["shoot_script_status"] == "CANCELLED"
        or row["attempt_status"] == "CANCELLED"
    )


def mark_shoot_script_generation_cancelled(shoot_script_id: int) -> None:
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_shoot_scripts
            SET status = 'CANCELLED', locked_by = NULL, locked_until = NULL, updated_at = ?
            WHERE id = ? AND status IN ('QUEUED', 'RUNNING', 'CANCELLED')
            """,
            (now_iso(), shoot_script_id),
        )


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
            SET status = ?, completed_at = ?, failed_at = ?, cancelled_at = ?, response_snapshot_json = ?, output_asset_id = ?,
                error_code = CASE WHEN ? = 'SUCCEEDED' THEN NULL ELSE error_code END,
                error_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                timestamp if status == "SUCCEEDED" else None,
                timestamp if status == "FAILED" else None,
                timestamp if status == "CANCELLED" else None,
                dumps(response_snapshot or {}),
                output_asset_id,
                status,
                error_message,
                timestamp,
                attempt_id,
            ),
        )


def persist_attempt_provider_request_id(attempt_id: int, provider_response: dict[str, Any] | None) -> None:
    persist_attempt_provider_progress(attempt_id, provider_response)


def persist_attempt_provider_progress(attempt_id: int | None, provider_response: dict[str, Any] | None) -> None:
    if not attempt_id or not isinstance(provider_response, dict):
        return
    provider_request_id = provider_response.get("providerRequestId")
    timestamp = now_iso()
    with get_db() as db:
        if provider_request_id:
            db.execute(
                """
                UPDATE generation_attempts
                SET provider_request_id = ?, response_snapshot_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(provider_request_id), dumps(provider_response), timestamp, attempt_id),
            )
            return
        db.execute(
            """
            UPDATE generation_attempts
            SET response_snapshot_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (dumps(provider_response), timestamp, attempt_id),
        )


def attempt_has_provider_request_id(attempt_id: int | None) -> bool:
    if not attempt_id:
        return False
    with get_db() as db:
        row = db.execute("SELECT provider_request_id FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
    return bool(row and row["provider_request_id"])


def get_attempt_provider_resume_snapshot(attempt_id: int | None) -> dict[str, Any] | None:
    if not attempt_id:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT provider_request_id, response_snapshot_json FROM generation_attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()
    if not row or not row["provider_request_id"]:
        return None
    snapshot = loads(row["response_snapshot_json"], {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    snapshot["providerRequestId"] = row["provider_request_id"]
    return snapshot


def fail_stale_attempt(attempt_id: int | None, message: str) -> None:
    if not attempt_id:
        return
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_attempts
            SET status = 'FAILED', failed_at = ?, error_code = 'STALE_JOB_RECOVERED',
                error_message = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING'
            """,
            (timestamp, message, timestamp, attempt_id),
        )


def build_ai_stylist_context(input_version_id: int) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    with get_db() as db:
        input_row = db.execute("SELECT * FROM generation_task_input_versions WHERE id = ?", (input_version_id,)).fetchone()
        material_rows = db.execute(
            """
            SELECT sg.id, sg.code, sg.ai_tags_json, sg.manual_tags_json, sg.official_title_tags_json,
              sg.effective_tags_json, sg.tag_meta_json,
              (SELECT ma.id FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_asset_id,
              (SELECT ma.object_key FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_key,
              (SELECT ma.original_filename FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_filename,
              (SELECT ma.mime_type FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_mime_type,
              (SELECT ma.width FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_width,
              (SELECT ma.height FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_height,
              (SELECT ma.size_bytes FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_size_bytes
            FROM styling_garments sg
            WHERE sg.deleted_at IS NULL AND sg.archived_at IS NULL AND sg.status = 'READY'
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
    material_cards = [compress_tags_for_styling(row) for row in material_rows]
    candidate_snapshot = build_candidate_retrieval_snapshot(input_row, material_cards)
    request_snapshot = build_ai_stylist_request_snapshot(input_row, candidate_snapshot)
    request_snapshot = attach_ai_stylist_prompt_snapshot(request_snapshot, candidate_snapshot)
    candidate_snapshot["ai_styling_prompt_config_key"] = request_snapshot["promptConfigKey"]
    candidate_snapshot["ai_styling_prompt"] = request_snapshot["prompt"]
    request_snapshot["candidate_retrieval"] = candidate_snapshot
    return input_row, candidate_snapshot, request_snapshot


async def select_styling_with_gemini(input_row: Any, candidate_snapshot: dict[str, Any], request_snapshot: dict[str, Any], attempt_id: int | None = None) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise RuntimeError("Gemini API 未配置，AI Stylist 不能进入生产选搭。")
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        raise RuntimeError("google-genai SDK 不可用，AI Stylist 不能进入生产选搭。") from exc
    if not request_snapshot.get("prompt"):
        request_snapshot = attach_ai_stylist_prompt_snapshot(request_snapshot, candidate_snapshot)
    if attempt_id:
        update_attempt_request_snapshot(attempt_id, request_snapshot)
    prompt = str(request_snapshot.get("prompt") or "")
    contents: list[Any] = [prompt]
    for item in loads(input_row["image_snapshot_json"], [])[:8]:
        asset = (item.get("asset") or {})
        object_key = asset.get("objectKey")
        if object_key and object_exists(object_key):
            contents.append(types.Part.from_bytes(data=get_bytes(object_key), mime_type=asset.get("mimeType") or "image/png"))
    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = await client.aio.models.generate_content(model=settings.gemini_model, contents=contents)
        raw = loads(extract_json_text(getattr(response, "text", "") or "{}"), {})
    except Exception as exc:
        raise RuntimeError(f"Gemini AI Stylist 调用失败：{exc}") from exc
    styling_plan = normalize_styling_plan(raw, candidate_snapshot)
    styling_plan["model_raw_output"] = raw
    return styling_plan


def attach_ai_stylist_prompt_snapshot(request_snapshot: dict[str, Any], candidate_snapshot: dict[str, Any]) -> dict[str, Any]:
    prompt = render_prompt_template(
        AI_STYLING_PROMPT_CONFIG_KEY,
        {
            "candidate_retrieval": dumps(candidate_retrieval_for_prompt(candidate_snapshot)),
        },
    )
    return {**request_snapshot, "promptConfigKey": AI_STYLING_PROMPT_CONFIG_KEY, "prompt": prompt}


def candidate_retrieval_for_prompt(candidate_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    cards = candidate_snapshot.get("library_tag_cards") if isinstance(candidate_snapshot.get("library_tag_cards"), list) else []
    return [styling_garment_card_for_prompt(card) for card in cards if isinstance(card, dict)]


def styling_garment_card_for_prompt(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": str(card.get("code") or ""),
        "manual": card.get("manual") or {},
        "tag_facts": card.get("tag_facts") or {},
    }


def build_candidate_retrieval_snapshot(input_row: Any, material_cards: list[dict[str, Any]], ai_policy_config: dict[str, Any] | None = None) -> dict[str, Any]:
    library_tag_cards = [library_tag_card_for_prompt(card) for card in material_cards]
    return {
        "schema_version": "candidate-retrieval-v2",
        "strategy": "prompt_config_raw_tag_facts_v1",
        "input_version_id": int(input_row["id"]),
        "library_scope": {
            "status": "READY",
            "archived_at": None,
            "deleted_at": None,
        },
        "source_material_ids": [int(card["id"]) for card in material_cards],
        "library_tag_card_count": len(library_tag_cards),
        "candidate_material_ids": [int(card["id"]) for card in material_cards],
        "candidate_pool_size": len(material_cards),
        "candidate_limit": len(material_cards),
        "candidate_image_limit": 0,
        "library_tag_cards": library_tag_cards,
        "material_cards": library_tag_cards,
        "outfit_drafts": [],
        "filtered_out": [],
        "prompt_rule_source": AI_STYLING_PROMPT_CONFIG_KEY,
    }


def build_library_context_report(material_cards: list[dict[str, Any]]) -> dict[str, Any]:
    manual_flags = {
        "do_not_use_for_styling": sum(1 for card in material_cards if (card.get("manual") or {}).get("do_not_use_for_styling")),
    }
    return {
        "schema_version": "library-context-v1",
        "library_ready_count": len(material_cards),
        "counts_by_status": {"ready": len(material_cards)},
        "manual_flags": manual_flags,
        "decision_owner": "AI Stylist Prompt",
        "notes": ["后端不判断库是否充足、不排序候选、不给出更优备选；这些结论由 Prompt 和大模型输出。"],
    }


def build_accessory_boundary(ai_policy_config: dict[str, Any] | None = None) -> dict[str, Any]:
    ai_policy_config = normalize_ai_policy_config_payload(ai_policy_config or default_ai_policy_config())
    accessory = ai_policy_config.get("accessoryPolicy") if isinstance(ai_policy_config.get("accessoryPolicy"), dict) else {}
    return {
        "allow_empty": bool(accessory.get("allowEmpty", True)),
        "default": ensure_string_list(accessory.get("default"))[:8],
        "avoid_default": ensure_string_list(accessory.get("avoidDefault"))[:12],
        "bag_rule": str(accessory.get("bagRule") or ""),
        "jewelry_rule": str(accessory.get("jewelryRule") or ""),
    }


def build_phase_one_brand_style_evidence(ai_policy_config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    ai_policy_config = normalize_ai_policy_config_payload(ai_policy_config or default_ai_policy_config())
    brand = ai_policy_config.get("brandStylePolicy") if isinstance(ai_policy_config.get("brandStylePolicy"), dict) else {}
    evidence: list[dict[str, Any]] = []
    for item in brand.get("evidence", []):
        if not isinstance(item, dict):
            continue
        evidence.append({
            "id": str(item.get("id") or "")[:80],
            "source_type": str(item.get("source_type") or "manual_policy")[:80],
            "visual_caption": str(item.get("visual_caption") or "")[:240],
            "brand_style_tags": ensure_string_list(item.get("brand_style_tags"))[:12],
            "accessory_boundary": str(item.get("accessory_boundary") or "")[:240],
        })
    return evidence[:10]


def build_ai_stylist_request_snapshot(input_row: Any, candidate_snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "ai-stylist-request-v1",
        "model": settings.gemini_model,
        "input_version": {
            "id": input_row["id"],
            "taskId": input_row["task_id"],
            "nameSnapshot": input_row["name_snapshot"],
            "imageSnapshot": loads(input_row["image_snapshot_json"], []),
        },
        "candidate_retrieval": candidate_snapshot,
        "prompt_rule_source": AI_STYLING_PROMPT_CONFIG_KEY,
    }


def fallback_styling_plan(input_row: Any, candidate_snapshot: dict[str, Any], reason: str) -> dict[str, Any]:
    plan = {
        "schema_version": "styling-plan-v1",
        "plan_source_mode": "fallback",
        "hero_understanding": {
            "hero_visual_summary": "主推款图片已作为商品事实源。",
            "hero_selling_points": ["主推款整体轮廓", "主推款关键细节"],
            "hero_visibility_risks": ["AI Stylist 未完成真实审美判断"],
        },
        "styling_intents": [
            {
                "id": "hero_first_single_piece",
                "title": "主推款优先",
                "reason": "模型不可用时采用最保守的商品优先方案。",
                "risk": "未进行真实智能选搭。",
            },
        ],
        "outfit_candidates": [
            {
                "id": "candidate_a",
                "mode": "hero_first_single_piece",
                "selected_material_ids": [],
                "generated_accessories": ["低存在感裸色或米色简洁鞋"],
                "wearing_notes": "主推款作为画面唯一商品级服饰。",
                "hero_focus": ["主推款整体轮廓", "主推款关键细节"],
                "aesthetic_diagnosis": {
                    "color_weight": "模型不可用，未完成真实色彩重量判断。",
                    "silhouette_balance": "模型不可用，采用主推款单穿以避免配角体量干扰。",
                    "waistline_proportion": "模型不可用，优先保留主推款比例。",
                    "season_material_fit": "模型不可用，未完成季节材质判断。",
                    "template_risk": "模型不可用，避免套用完整 outfit 模板。",
                    "brand_read": "模型不可用，无法确认 Lily 品牌采用可能性。",
                },
                "scores": {
                    "aesthetic_score": 0.45,
                    "brand_fit_score": 0.45,
                    "hero_visibility_score": 0.95,
                    "commercial_readiness_score": 0.55,
                },
                "risk_notes": [reason],
            },
        ],
        "selected_outfit_key": "candidate_a",
        "selected_outfit_id": "candidate_a",
        "selected_material_ids": [],
        "reason_summary": "AI Stylist 不可用，采用主推款优先的保守方案。",
        "rejected_candidates": [],
        "warnings": ["fallback_styling_plan", reason],
    }
    return normalize_styling_plan(plan, candidate_snapshot, plan_source_mode="fallback")


def normalize_styling_plan(raw: dict[str, Any], candidate_snapshot: dict[str, Any], plan_source_mode: str = "production") -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    hero = source.get("hero_understanding") if isinstance(source.get("hero_understanding"), dict) else {}
    intents = source.get("styling_intents") if isinstance(source.get("styling_intents"), list) else []
    candidates = source.get("outfit_candidates") if isinstance(source.get("outfit_candidates"), list) else []
    candidate_ids = {int(item) for item in candidate_snapshot.get("candidate_material_ids", [])}
    candidate_cards = candidate_cards_by_id(candidate_snapshot)
    candidate_cards_by_code_map = candidate_cards_by_code(candidate_snapshot)
    normalized_candidates = [
        normalize_outfit_candidate(item, candidate_ids, candidate_cards, candidate_cards_by_code_map, index)
        for index, item in enumerate(candidates, start=1)
        if isinstance(item, dict)
    ]
    if not normalized_candidates:
        normalized_candidates = [normalize_outfit_candidate({}, candidate_ids, candidate_cards, candidate_cards_by_code_map, 1)]
    selected_outfit_id = str(source.get("selected_outfit_key") or source.get("selected_outfit_id") or "")
    selected = next((item for item in normalized_candidates if item["id"] == selected_outfit_id), {})
    selected_codes = normalize_selected_material_codes(source.get("selected_material_codes") or source.get("selected_materials"), candidate_cards_by_code_map)
    selected_ids = selected_material_ids_for_codes(selected_codes, candidate_cards_by_code_map)
    selected_ids_source = source.get("selected_material_ids")
    if not isinstance(selected_ids_source, list):
        selected_ids_source = selected["selected_material_ids"] if isinstance(selected.get("selected_material_ids"), list) else []
    if not selected_ids:
        selected_ids = normalize_selected_material_ids(selected_ids_source, candidate_ids)
    if not selected_ids:
        selected_ids = selected.get("selected_material_ids") if isinstance(selected.get("selected_material_ids"), list) else []
    selected_refs = normalize_selected_material_refs(source.get("selected_materials"), selected_ids, candidate_cards, candidate_cards_by_code_map)
    if not selected_refs:
        selected_refs = normalize_selected_material_refs(selected.get("selected_materials"), selected_ids, candidate_cards, candidate_cards_by_code_map)
    selected_codes = [item["material_code"] for item in selected_refs if item.get("material_code")]
    warnings = ensure_string_list(source.get("warnings"))
    rejected_source = source.get("rejected_candidates") if isinstance(source.get("rejected_candidates"), list) else []
    return {
        "schema_version": "styling-plan-v1",
        "plan_source_mode": "fallback" if plan_source_mode == "fallback" else "production",
        "hero_understanding": {
            "hero_visual_summary": str(hero.get("hero_visual_summary") or source.get("hero_visual_summary") or "主推款图片已作为商品事实源。")[:120],
            "category": str(hero.get("category") or "")[:80],
            "shot_requirements": ensure_string_list(hero.get("shot_requirements"))[:8],
            "hero_selling_points": ensure_string_list(hero.get("hero_selling_points"))[:8],
            "hero_visibility_risks": ensure_string_list(hero.get("hero_visibility_risks"))[:8],
        },
        "styling_intents": [
            {
                "intent_key": str(item.get("intent_key") or item.get("id") or f"intent_{index}")[:40],
                "id": str(item.get("intent_key") or item.get("id") or f"intent_{index}")[:40],
                "title": str(item.get("title") or "造型方向")[:40],
                "reason": str(item.get("reason") or "")[:120],
                "risk": str(item.get("risk") or "")[:120],
            }
            for index, item in enumerate(intents, start=1)
            if isinstance(item, dict)
        ][:5],
        "outfit_candidates": normalized_candidates[:5],
        "selected_outfit_key": selected_outfit_id,
        "selected_outfit_id": selected_outfit_id,
        "selected_material_ids": selected_ids,
        "selected_material_codes": selected_codes,
        "selected_materials": selected_refs,
        "brand_evidence": normalize_brand_evidence(source.get("brand_evidence"), candidate_snapshot),
        "reason_summary": str(source.get("reason_summary") or "已根据主推款和搭配服饰标签完成 AI Stylist 造型方案。")[:180],
        "rejected_candidates": normalize_rejected_candidates(rejected_source, normalized_candidates, candidate_cards),
        "quality_signals": normalize_model_quality_signals(source.get("quality_signals")),
        "warnings": warnings[:8],
    }


def normalize_model_quality_signals(value: Any) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")[:80]
        message = str(item.get("message") or "")[:240]
        if not code and not message:
            continue
        signals.append({
            "code": code,
            "category": str(item.get("category") or "")[:80],
            "severity": str(item.get("severity") or "review")[:40],
            "message": message,
        })
    return signals[:12]


def normalize_brand_evidence(value: Any, candidate_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    source = value if isinstance(value, list) else candidate_snapshot.get("brand_style_evidence")
    evidence: list[dict[str, Any]] = []
    for item in (source if isinstance(source, list) else []):
        if not isinstance(item, dict):
            continue
        evidence.append(
            {
                "id": str(item.get("id") or "")[:80],
                "source_type": str(item.get("source_type") or "")[:80],
                "visual_caption": str(item.get("visual_caption") or "")[:180],
                "brand_style_tags": ensure_string_list(item.get("brand_style_tags"))[:8],
                "accessory_boundary": str(item.get("accessory_boundary") or "")[:180],
            }
        )
    return evidence[:5]


def candidate_cards_by_id(candidate_snapshot: dict[str, Any]) -> dict[int, dict[str, Any]]:
    cards: dict[int, dict[str, Any]] = {}
    for collection_key in ("library_tag_cards", "material_cards"):
        for card in candidate_snapshot.get(collection_key, []):
            if not isinstance(card, dict) or card.get("id") is None:
                continue
            try:
                cards[int(card["id"])] = card
            except (TypeError, ValueError):
                continue
    return cards


def candidate_cards_by_code(candidate_snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cards: dict[str, dict[str, Any]] = {}
    for collection_key in ("library_tag_cards", "material_cards"):
        for card in candidate_snapshot.get(collection_key, []):
            if not isinstance(card, dict):
                continue
            code = str(card.get("code") or "").strip()
            if code:
                cards[code] = card
    return cards


def normalize_selected_material_codes(value: Any, candidate_cards_by_code_map: dict[str, dict[str, Any]]) -> list[str]:
    source = value if isinstance(value, list) else []
    codes: list[str] = []
    for item in source:
        raw = item.get("material_code") or item.get("code") if isinstance(item, dict) else item
        code = str(raw or "").strip()
        if code and code in candidate_cards_by_code_map:
            codes.append(code)
    return list(dict.fromkeys(codes))[:4]


def selected_material_ids_for_codes(codes: list[str], candidate_cards_by_code_map: dict[str, dict[str, Any]]) -> list[int]:
    selected: list[int] = []
    for code in codes:
        card = candidate_cards_by_code_map.get(code) or {}
        try:
            selected.append(int(card.get("id")))
        except (TypeError, ValueError):
            continue
    return selected[:4]


def normalize_outfit_candidate(candidate: dict[str, Any], candidate_ids: set[int], candidate_cards: dict[int, dict[str, Any]], candidate_cards_by_code_map: dict[str, dict[str, Any]], index: int) -> dict[str, Any]:
    scores = candidate.get("scores") if isinstance(candidate.get("scores"), dict) else {}
    selected_codes = normalize_selected_material_codes(candidate.get("selected_material_codes") or candidate.get("selected_materials"), candidate_cards_by_code_map)
    selected_ids = selected_material_ids_for_codes(selected_codes, candidate_cards_by_code_map)
    if not selected_ids:
        selected_ids = normalize_selected_material_ids(candidate.get("selected_material_ids"), candidate_ids)
    if not selected_ids:
        selected_ids = normalize_selected_material_ids(candidate.get("selected_materials"), candidate_ids)
    selected_refs = normalize_selected_material_refs(candidate.get("selected_materials"), selected_ids, candidate_cards, candidate_cards_by_code_map)
    selected_codes = [item["material_code"] for item in selected_refs if item.get("material_code")]
    diagnosis = candidate.get("aesthetic_diagnosis") if isinstance(candidate.get("aesthetic_diagnosis"), dict) else {}
    outfit_key = str(candidate.get("outfit_key") or candidate.get("id") or f"candidate_{index}")[:40]
    return {
        "outfit_key": outfit_key,
        "id": outfit_key,
        "mode": str(candidate.get("mode") or "")[:40],
        "title": str(candidate.get("title") or "")[:60],
        "selected_material_ids": selected_ids,
        "selected_material_codes": selected_codes,
        "selected_materials": selected_refs,
        "generated_accessories": ensure_string_list(candidate.get("generated_accessories"))[:6],
        "wearing_notes": str(candidate.get("wearing_notes") or "")[:180],
        "hero_focus": ensure_string_list(candidate.get("hero_focus"))[:8],
        "aesthetic_diagnosis": normalize_string_dict(diagnosis, max_items=24, max_value_length=180),
        "scores": normalize_outfit_scores(scores),
        "risk_notes": ensure_string_list(candidate.get("risk_notes"))[:8],
    }


def normalize_string_dict(value: dict[str, Any], max_items: int = 24, max_value_length: int = 180) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, raw in list(value.items())[:max_items]:
        clean_key = str(key or "").strip()[:80]
        if clean_key:
            normalized[clean_key] = str(raw or "")[:max_value_length]
    return normalized


def normalize_outfit_scores(scores: dict[str, Any]) -> dict[str, float]:
    if not isinstance(scores, dict):
        return {}
    normalized: dict[str, float] = {}
    for key, value in scores.items():
        try:
            normalized[str(key)] = clamp_float(value, 0, 1, 0)
        except (TypeError, ValueError):
            continue
    return normalized


def normalize_rejected_candidates(
    rejected_source: list[Any],
    normalized_candidates: list[dict[str, Any]],
    candidate_cards: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates_by_key = {str(item.get("outfit_key") or item.get("id") or ""): item for item in normalized_candidates}
    rejected: list[dict[str, Any]] = []
    for item in rejected_source:
        if not isinstance(item, dict):
            continue
        outfit_key = str(item.get("outfit_key") or item.get("id") or "")[:40]
        material_codes = rejected_candidate_material_codes(item, candidates_by_key.get(outfit_key), candidate_cards)
        rejected.append({
            "outfit_key": outfit_key,
            "id": outfit_key,
            "reason": replace_material_ids_with_codes(str(item.get("reason") or ""), candidate_cards)[:160],
            "material_codes": material_codes[:4],
        })
    return rejected[:5]


def rejected_candidate_material_codes(
    item: dict[str, Any],
    matched_candidate: dict[str, Any] | None,
    candidate_cards: dict[int, dict[str, Any]],
) -> list[str]:
    codes = ensure_string_list(item.get("material_codes") or item.get("selected_material_codes"))
    if not codes and isinstance(item.get("selected_materials"), list):
        codes = [
            str(ref.get("material_code") or ref.get("code") or "")
            for ref in item["selected_materials"]
            if isinstance(ref, dict) and str(ref.get("material_code") or ref.get("code") or "")
        ]
    if not codes:
        selected_ids = normalize_selected_material_ids(
            item.get("selected_material_ids") if isinstance(item.get("selected_material_ids"), list) else item.get("selected_materials"),
            set(candidate_cards.keys()),
        )
        codes = [
            str(candidate_cards[item_id].get("code") or "")
            for item_id in selected_ids
            if item_id in candidate_cards and str(candidate_cards[item_id].get("code") or "")
        ]
    if not codes and matched_candidate:
        codes = ensure_string_list(matched_candidate.get("selected_material_codes"))
    return list(dict.fromkeys(code for code in codes if code))[:4]


def replace_material_ids_with_codes(text: str, candidate_cards: dict[int, dict[str, Any]]) -> str:
    result = text
    for material_id, card in candidate_cards.items():
        code = str(card.get("code") or "").strip()
        if not code:
            continue
        escaped_id = re.escape(str(material_id))
        result = re.sub(rf"\b(?:material[_\s-]*id|服饰ID|商品ID|ID)\s*[:：#-]?\s*{escaped_id}(?![A-Za-z0-9])", code, result, flags=re.IGNORECASE)
        result = re.sub(
            rf"(库内(?:针织衫|衬衫|外套|西装|上衣|下装|裤装|半裙|连衣裙|搭配服饰|服饰|单品)|皮革马甲)\s*{escaped_id}(?![A-Za-z0-9])",
            rf"\1 {code} ",
            result,
        )
    result = re.sub(r"\b(?:material[_\s-]*id|服饰ID|商品ID|ID)\s*[:：#-]?\s*\d+(?![A-Za-z0-9])", "该服饰", result, flags=re.IGNORECASE)
    result = re.sub(r"(库内(?:针织衫|衬衫|外套|西装|上衣|下装|裤装|半裙|连衣裙|搭配服饰|服饰|单品)|皮革马甲)\s*\d+(?![A-Za-z0-9])", r"\1", result)
    return result


def strip_internal_prompt_fields(value: Any) -> Any:
    internal_keys = {
        "id",
        "material_id",
        "selected_material_ids",
        "schema_version",
        "schemaVersion",
        "plan_source_mode",
        "script_source_mode",
        "input_refs",
        "task_id",
        "input_version_id",
        "styling_id",
        "model",
        "prompt",
        "promptConfigKey",
        "model_raw_output",
        "createdAt",
        "updatedAt",
        "created_at",
        "updated_at",
    }
    if isinstance(value, list):
        return [strip_internal_prompt_fields(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in internal_keys:
                continue
            result[key] = strip_internal_prompt_fields(item)
        return result
    return value


def styling_result_for_prompt(styling_plan: dict[str, Any], selected_material_snapshot: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return strip_internal_prompt_fields(styling_plan if isinstance(styling_plan, dict) else {})


def compress_selected_material_for_prompt(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": item.get("code"),
        "manual": item.get("manual") or {},
        "tag_facts": item.get("tagFacts") or item.get("tag_facts") or {},
    }


def shoot_script_result_for_prompt(shoot_script: dict[str, Any]) -> dict[str, Any]:
    return strip_internal_prompt_fields(shoot_script if isinstance(shoot_script, dict) else {})


def current_shot_context_for_prompt(shot_index: int, shot_plan: dict[str, Any], styling_plan: dict[str, Any], shoot_script: dict[str, Any]) -> dict[str, Any]:
    script = shoot_script if isinstance(shoot_script, dict) else {}
    shot = strip_internal_prompt_fields(shot_plan if isinstance(shot_plan, dict) else {})
    accessories = []
    for item in script.get("resolved_accessories", []) if isinstance(script.get("resolved_accessories"), list) else []:
        if not isinstance(item, dict):
            continue
        applies = item.get("applies_to_shots") if isinstance(item.get("applies_to_shots"), list) else []
        if not applies or shot_index in [int(value) for value in applies if str(value).isdigit()]:
            accessories.append(strip_internal_prompt_fields(item))
    return {
        "preview_grid_cell": shot_index,
        "styling": styling_result_for_prompt(styling_plan),
        "global_directing": strip_internal_prompt_fields(script.get("global_directing") if isinstance(script.get("global_directing"), dict) else {}),
        "current_shot": shot,
        "resolved_accessories_for_this_shot": accessories,
        "negative_constraints": strip_internal_prompt_fields((script.get("global_directing") or {}).get("negative_constraints") if isinstance(script.get("global_directing"), dict) else []),
    }


def normalize_selected_material_ids(value: Any, candidate_ids: set[int]) -> list[int]:
    if not isinstance(value, list):
        return []
    selected: list[int] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("material_id") or item.get("id")
        try:
            item_id = int(item)
        except (TypeError, ValueError):
            continue
        selected.append(item_id)
    return selected[:4]


def normalize_selected_material_refs(value: Any, selected_ids: list[int], candidate_cards: dict[int, dict[str, Any]], candidate_cards_by_code_map: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    candidate_cards_by_code_map = candidate_cards_by_code_map or {}
    refs_by_id: dict[int, dict[str, Any]] = {}
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            try:
                material_id = int(item.get("material_id") or item.get("id"))
            except (TypeError, ValueError):
                code = str(item.get("material_code") or item.get("code") or "").strip()
                card = candidate_cards_by_code_map.get(code) or {}
                try:
                    material_id = int(card.get("id"))
                except (TypeError, ValueError):
                    continue
            if material_id not in selected_ids:
                continue
            card = candidate_cards.get(material_id, {})
            refs_by_id[material_id] = {
                "material_id": material_id,
                "material_code": str(card.get("code") or item.get("material_code") or item.get("code") or "")[:80],
                "role": str(item.get("role") or card.get("category") or "")[:40],
                "styling_purpose": str(item.get("styling_purpose") or item.get("purpose") or "")[:120],
                "wearing_method": str(item.get("wearing_method") or "")[:120],
                "visibility_boundary": str(item.get("visibility_boundary") or "")[:160],
            }
    refs: list[dict[str, Any]] = []
    for material_id in selected_ids[:4]:
        card = candidate_cards.get(material_id, {})
        ref = refs_by_id.get(material_id) or {
            "material_id": material_id,
            "material_code": str(card.get("code") or "")[:80],
            "role": str(card.get("category") or "")[:40],
            "styling_purpose": "",
            "wearing_method": "",
            "visibility_boundary": "",
        }
        refs.append(ref)
    return refs


def validate_styling_plan(plan: dict[str, Any], candidate_snapshot: dict[str, Any]) -> None:
    candidate_ids = {int(item) for item in candidate_snapshot.get("candidate_material_ids", [])}
    selected_ids = plan.get("selected_material_ids") if isinstance(plan.get("selected_material_ids"), list) else []
    selected_key = str(plan.get("selected_outfit_key") or plan.get("selected_outfit_id") or "")
    outfit_keys = {
        str(item.get("outfit_key") or item.get("id") or "")
        for item in plan.get("outfit_candidates", [])
        if isinstance(item, dict)
    }
    if not selected_key or selected_key not in outfit_keys:
        raise RuntimeError("AI Stylist 返回的 selected_outfit_key 不存在于 outfit_candidates")
    invalid = set(int(item) for item in selected_ids) - candidate_ids
    if invalid:
        raise RuntimeError(f"AI Stylist 返回了非法搭配服饰 ID：{sorted(invalid)}")
    if len(selected_ids) != len(set(selected_ids)):
        raise RuntimeError("AI Stylist 返回了重复搭配服饰 ID")
    if plan.get("plan_source_mode") == "fallback":
        raise RuntimeError("AI Stylist 工程 fallback 方案不能进入生产链路")


def get_selected_outfit_scores(styling_plan: dict[str, Any]) -> dict[str, Any]:
    selected_outfit = selected_outfit_from_plan(styling_plan)
    scores = selected_outfit.get("scores") if isinstance(selected_outfit.get("scores"), dict) else {}
    return scores


def selected_outfit_from_plan(styling_plan: dict[str, Any]) -> dict[str, Any]:
    selected_key = str(styling_plan.get("selected_outfit_key") or styling_plan.get("selected_outfit_id") or "")
    return next(
        (
            item
            for item in styling_plan.get("outfit_candidates", [])
            if isinstance(item, dict) and str(item.get("outfit_key") or item.get("id") or "") == selected_key
        ),
        {},
    )


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
        styling_plan = loads(styling["styling_plan_json"], {}) if "styling_plan_json" in styling.keys() else {}
    return []


def default_ai_policy_config() -> dict[str, Any]:
    return {
        "schemaVersion": "ai-policy-config-v1",
        "candidateFunnel": {
            "candidateLimit": 20,
            "candidateImageLimit": 20,
            "hardExcludedCategories": ["未知", "不支持入库配饰"],
            "categoryCaps": {"default": 8},
            "categoryPolicies": [
                {
                    "heroCategory": "连衣裙",
                    "excludedCategories": ["裤装", "半身裙", "短裤", "连衣裙", "连体类", "套装"],
                    "categoryScoreAdjustments": {"上装": 0.16, "针织类": 0.12, "外套类": -0.45},
                    "draftCategoryPriority": ["上装", "针织类", "外套类"],
                    "draftModeByCandidateCategory": {"上装": "light_overlay_or_inner_layer", "针织类": "light_soft_layer", "外套类": "review_required_open_layer"},
                    "singlePieceMode": "hero_first_single_piece",
                    "singlePieceReason": "连衣裙本身是完整造型；除非候选轻量且不遮挡腰线、领口和裙摆，否则优先单穿。",
                    "supplyGapCategoryGroups": [
                        {"categories": ["上装", "针织类"], "minimum": 1, "note": "连衣裙主推缺少轻薄针织/上装类造型辅助候选，可优先单穿。"}
                    ],
                },
                {
                    "heroCategory": "裤装",
                    "excludedCategories": ["裤装", "半身裙", "短裤"],
                    "categoryScoreAdjustments": {"上装": 0.18, "针织类": 0.18, "外套类": -0.12},
                    "draftCategoryPriority": ["上装", "针织类", "外套类"],
                    "draftModeByCandidateCategory": {"上装": "waist_visible_light_top", "针织类": "waist_visible_light_top", "外套类": "open_light_layering"},
                    "supplyGapCategoryGroups": [
                        {"categories": ["上装", "针织类"], "minimum": 1, "note": "裤装/下装主推缺少轻量上装或针织候选。"}
                    ],
                },
                {
                    "heroCategory": "上装",
                    "categoryScoreAdjustments": {"裤装": 0.18, "半身裙": 0.18, "短裤": 0.12, "外套类": -0.08, "上装": -0.6, "针织类": -0.45},
                    "draftCategoryPriority": ["裤装", "半身裙", "短裤", "外套类"],
                    "draftModeByCandidateCategory": {"裤装": "simple_bottom_pairing", "半身裙": "simple_bottom_pairing", "短裤": "simple_bottom_pairing", "外套类": "review_required_open_layer"},
                    "supplyGapCategoryGroups": [
                        {"categories": ["裤装", "半身裙", "短裤"], "minimum": 1, "note": "上衣主推缺少简洁下装候选。"}
                    ],
                },
                {
                    "heroCategory": "针织类",
                    "categoryScoreAdjustments": {"裤装": 0.18, "半身裙": 0.18, "短裤": 0.12, "外套类": -0.08, "上装": -0.45, "针织类": -0.6},
                    "draftCategoryPriority": ["裤装", "半身裙", "短裤", "外套类"],
                    "draftModeByCandidateCategory": {"裤装": "simple_bottom_pairing", "半身裙": "simple_bottom_pairing", "短裤": "simple_bottom_pairing", "外套类": "review_required_open_layer"},
                    "supplyGapCategoryGroups": [
                        {"categories": ["裤装", "半身裙", "短裤"], "minimum": 1, "note": "针织主推缺少简洁下装候选。"}
                    ],
                },
                {
                    "heroCategory": "外套类",
                    "categoryScoreAdjustments": {"上装": 0.18, "针织类": 0.12, "裤装": 0.1, "半身裙": 0.1, "连衣裙": -0.08},
                    "draftCategoryPriority": ["上装", "针织类", "裤装", "半身裙", "连衣裙"],
                    "draftModeByCandidateCategory": {"上装": "outerwear_hero_outfit", "针织类": "outerwear_hero_outfit", "裤装": "outerwear_hero_outfit", "半身裙": "outerwear_hero_outfit", "连衣裙": "outerwear_hero_outfit"},
                    "supplyGapCategoryGroups": [
                        {"categories": ["上装", "针织类", "裤装", "半身裙", "短裤"], "minimum": 2, "note": "外套主推缺少内搭或下装候选。"}
                    ],
                },
            ],
            "riskAdjustments": {
                "coverageMayCoverHero": -0.35,
                "coverageLikelyCoverHero": -0.55,
                "styleRisk": -0.16,
                "longTopMayCoverWaist": -0.25,
                "waistFocusShortUpper": 0.12,
                "darkUpperAgainstLightHero": -0.12,
                "lightUpperForBottomHero": 0.1,
                "lightMaterialForBottomHero": 0.1,
                "heavyMaterialForBottomHero": -0.18,
                "seasonMismatchForLightBottom": -0.12,
                "structuredOuterwear": 0.08,
            },
            "coverageRiskAdjustments": {"可能遮挡主推款": -0.35, "较可能遮挡主推款": -0.55},
            "noStyleRiskValues": ["无明显风险", "无", "none"],
            "waistFocusCategoryAdjustments": {"外套类": -0.2},
            "longTopLengthValues": ["长上衣", "长款", "过臀"],
            "waistFocusShortUpperCategories": ["上装", "针织类"],
            "shortUpperLengthValues": ["短上衣", "及胯上衣", "短款"],
            "darkAgainstLightHeroCategories": ["上装", "针织类", "外套类"],
            "lightPairingByHeroCategory": {"裤装": ["上装", "针织类", "外套类"]},
            "structuredLayerBonusCategories": ["外套类"],
            "structuredLayerBonusSilhouettes": ["收腰", "收腰比例", "X型"],
            "semanticSets": {
                "darkColors": ["黑色", "棕色", "藏青色", "灰色"],
                "lightColors": ["白色", "象牙白", "米色"],
                "lightMaterials": ["亚麻感", "棉感", "真丝感", "雪纺"],
                "heavyMaterials": ["羊毛感", "粗花呢/香风感", "皮革感"],
                "springSummerSeasons": ["春季", "夏季"],
                "waistFocusDetails": ["腰带", "褶裥", "口袋"],
                "waistFocusSilhouettes": ["阔腿", "收腰", "收腰比例", "微喇", "喇叭"],
            },
            "heroProfileSellingPointLabels": {
                "腰带": "腰带/扣件细节",
                "褶裥": "褶位结构",
                "褶皱": "褶位结构",
                "口袋": "口袋结构",
                "纽扣": "纽扣/门襟",
                "阔腿": "阔腿裤型",
                "收腰": "收腰比例",
                "V领": "领口线条",
                "方领": "领口线条",
                "开衩": "开衩/裙摆动态",
            },
            "defaultDraftMode": "office_outfit",
        },
        "heroPolicy": {
            "taskNameCategoryKeywords": {
                "裤装": ["裤", "裙裤", "阔腿", "直筒裤", "pants", "trouser"],
                "半身裙": ["半裙", "半身裙", "skirt"],
                "连衣裙": ["连衣裙", "裙装", "dress"],
                "上装": ["衬衫", "上衣", "t恤", "T恤", "shirt", "blouse", "top"],
                "针织类": ["针织", "knit"],
            },
            "taskNameWaistFocusKeywords": ["高腰", "腰头", "裤腰", "腰带", "褶裥", "阔腿"],
            "taskNameLightColorKeywords": ["白", "米白", "象牙", "浅", "ivory", "white", "beige"],
            "taskNameSellingPointRules": [
                {"keywords": ["高腰", "腰头", "裤腰", "腰带", "褶裥", "阔腿"], "labels": ["腰线", "腰头细节"]},
                {"keywords": ["阔腿", "垂坠"], "labels": ["裤腿垂坠"]},
            ],
            "visibilityRisksByCategory": {
                "裤装": ["腰头或门襟遮挡", "裤褶和口袋被手包遮挡", "上衣抢走下装视觉中心"],
                "半身裙": ["腰线遮挡", "裙摆比例被外套压住", "上衣抢走裙装视觉中心"],
                "连衣裙": ["外套遮挡腰线", "包袋遮挡裙摆或腰部结构", "鞋包过重破坏连衣裙完整感"],
                "上装": ["外套遮挡领口/门襟/下摆", "下装或腰带抢走上衣主角", "项链遮挡领口织法"],
                "针织类": ["外套遮挡领口/门襟/下摆", "下装或腰带抢走上衣主角", "项链遮挡领口织法"],
                "外套类": ["内搭抢走领口和门襟", "下装体量压制外套", "配饰遮挡翻领或口袋"],
                "default": ["配角抢走视觉中心", "主推款关键细节被遮挡"],
            },
            "shotRequirementsByCategory": {
                "裤装": ["全身/近全身", "腰脚下装图", "腰膝/腰胯近景", "侧身/3/4 动态", "正面颜色/版型图"],
                "半身裙": ["全身比例", "腰膝裙型", "腰胯细节", "侧身/3/4 动态", "标准正面颜色图"],
                "上装": ["全身比例", "臀上/大腿中部核心展示", "胸腰近景", "侧身/3/4 补充", "标准正面颜色图"],
                "针织类": ["全身比例", "臀上/大腿中部核心展示", "胸腰近景", "侧身/3/4 补充", "标准正面颜色图"],
                "外套类": ["全身外套轮廓", "臀上/七分身门襟展示", "胸腰近景", "侧身/背面补充", "标准正面颜色图"],
                "连衣裙": ["全身裙型", "3/4 腰线裙摆", "胸腰或腰膝细节", "侧身/背面动态", "标准正面颜色图"],
                "default": ["完整比例", "核心结构", "关键细节", "侧身补充", "标准正面图"],
            },
        },
        "qualitySignalPolicy": {
            "principle": "商品可见性只是底线；审美、Lily 品牌感和商品主角表达都要形成质量信号，供系统改选和客户审核闭环使用。",
            "flagIf": ["沉闷", "笨重", "廉价通勤", "比例失衡", "色块压迫", "模板化搭配", "配角成为视觉故事"],
            "scoreThresholds": {
                "aesthetic_score": 0.65,
                "brand_fit_score": 0.65,
                "hero_visibility_score": 0.55,
                "commercial_readiness_score": 0.6,
            },
            "fullScoreThreshold": 0.98,
            "requiredDiagnosisKeys": [
                "color_weight",
                "silhouette_balance",
                "waistline_proportion",
                "season_material_fit",
                "template_risk",
                "wearing_method_validity",
                "tuck_quality",
                "brand_read",
            ],
            "forbiddenAccessoryKeywords": ["公文包", "商务手提", "手提公文", "大体量托特", "大号托特", "briefcase", "document bag", "large tote"],
            "layeringExplanationKeywords": ["肩搭", "手持", "不同时穿", "备用", "其中一件", "不穿上身", "仅作为道具"],
            "halfTuckKeywords": ["半塞", "轻微前塞", "前片塞", "前摆塞", "前侧塞", "half-tuck", "halftuck", "front-tuck", "fronttuck"],
            "fullTuckKeywords": ["全塞", "完全塞", "全部塞", "塞入裤腰", "塞进裤腰", "塞到裤腰", "塞在裤腰", "塞腰", "收进裤腰", "收入裤腰", "扎进裤腰", "扎入裤腰", "掖进裤腰", "掖入裤腰", "束进裤腰", "束入裤腰", "收进腰头", "扎进腰头", "掖进腰头", "束进腰头", "tucked-in", "tuckedin", "tuckinto", "tuckedinto", "fullytucked", "fulltuck"],
            "genericTuckKeywords": ["tuck", "塞"],
            "waistKeywords": ["裤腰", "腰头", "waist"],
            "fullTuckMethodValues": ["塞腰"],
            "halfTuckMethodValues": ["半塞", "轻微前塞"],
            "tuckCheckedHeroCategories": ["上装", "针织类", "外套类"],
            "tuckCheckedGarmentCategories": ["上装", "针织类", "外套类"],
            "structuredLayerCategories": ["外套类"],
            "structuredLayerSubCategories": ["针织开衫", "西装", "夹克", "大衣", "风衣"],
            "structuredLayerDetails": ["纽扣", "纽扣细节", "不对称", "不对称设计"],
            "structuredLayerMaterials": ["针织", "羊毛感", "粗花呢/香风感"],
            "highWaistHeroKeywords": ["高腰", "裤", "阔腿", "腰头", "裤腰", "褶裥", "垂坠"],
            "highWaistDarkStackCategories": ["上装", "外套类", "套装", "其他服装"],
            "signals": {
                "unexplainedOuterwearConflict": {
                    "enabled": True,
                    "conflictCategories": ["外套类"],
                    "message": "多个外套候选缺少真实穿着解释，建议客户审核层次关系。",
                },
                "lowCriticScores": {
                    "enabled": True,
                    "message": "AI Stylist 自评分偏低。",
                },
                "suspiciousFullScores": {
                    "enabled": True,
                    "message": "AI Stylist 给出接近满分但缺少完整诊断和风险说明。",
                },
                "missingCriticDiagnosis": {
                    "enabled": True,
                    "message": "AI Stylist 缺少部分审美诊断。",
                },
                "templateAccessoryRisk": {
                    "enabled": True,
                    "message": "方案使用了不符合 Lily 边界的模板化配饰。",
                },
                "unsupportedTuckMethod": {
                    "enabled": True,
                    "message": "方案使用了搭配服饰或主推款不支持的塞腰/半塞穿法，建议审核上衣下摆处理。",
                },
                "highWaistDarkOuterwearStack": {
                    "enabled": True,
                    "message": "高腰裤搭配深色厚重外套存在压低比例和色块过重风险。",
                },
                "dressOuterwearLayeringRisk": {
                    "enabled": True,
                    "heroCategory": "连衣裙",
                    "candidateCategory": "外套类",
                    "message": "连衣裙主推只选择外套时，存在腰线/裙型被遮挡、整套比例变笨重的风险，需证明外套确实服务主推款。",
                },
                "selectedBelowBetterAlternatives": {
                    "enabled": True,
                    "topAlternativeCount": 5,
                    "message": "选中候选不在候选漏斗高分备选内，且存在更轻量或更匹配的备选，需要复核选搭理由。",
                },
            },
        },
        "accessoryPolicy": {
            "allowEmpty": True,
            "default": ["低存在感裸色或米色简洁鞋"],
            "avoidDefault": ["项链", "手提公文包", "商务公文包", "大体量托特包"],
            "bagRule": "默认无包；确有必要时只能小体量侧拿或手持，不遮挡腰线、裤褶、口袋、门襟。",
            "jewelryRule": "默认无项链；只允许极简耳饰或极低存在感首饰辅助氛围。",
        },
        "libraryAdequacyPolicy": {
            "betterAlternativeLimit": 5,
            "emptyPairingWhenSupplyGap": True,
        },
        "brandStylePolicy": {
            "core": ["商务时装", "都市通勤", "年轻职业女性", "简约", "优雅", "自信", "商品优先", "现代", "利落", "干净"],
            "principle": "搭配服饰服务主推款，而不是主推款服务整套造型。",
            "evidence": [
                {
                    "id": "phase1_lily_business_fashion",
                    "source_type": "phase_one_brand_boundary",
                    "visual_caption": "Lily 商务时装强调现代、干净、女性化剪裁和轻松高级的通勤表达。",
                    "brand_style_tags": ["商务时装", "现代简洁", "轻松高级", "女性化剪裁"],
                    "accessory_boundary": "配饰低存在感，不用传统公文包模板抢画面。",
                },
                {
                    "id": "phase1_hero_first_product",
                    "visual_caption": "主推款必须是商品级视觉中心，配角只负责放大卖点。",
                    "brand_style_tags": ["商品主角", "比例清楚"],
                    "accessory_boundary": "包和首饰可省略。",
                },
            ],
        },
    }


def clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def normalize_number_map(value: Any, fallback: dict[str, float]) -> dict[str, float]:
    source = value if isinstance(value, dict) else {}
    merged = dict(fallback)
    for key, raw in source.items():
        try:
            merged[str(key)] = float(raw)
        except (TypeError, ValueError):
            continue
    return merged


def normalize_string_map(value: Any, fallback: dict[str, str]) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    merged = dict(fallback)
    for key, raw in source.items():
        merged[str(key)] = str(raw)[:120]
    return merged


def normalize_list_map(value: Any, fallback: dict[str, list[str]]) -> dict[str, list[str]]:
    source = value if isinstance(value, dict) else {}
    keys = set(fallback) | {str(key) for key in source.keys()}
    return {
        key: ensure_string_list(source.get(key) if key in source else fallback.get(key))[:30]
        for key in sorted(keys)
    }


def normalize_hero_policy_config(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    rules_source = source.get("taskNameSellingPointRules") if isinstance(source.get("taskNameSellingPointRules"), list) else fallback.get("taskNameSellingPointRules", [])
    selling_point_rules = []
    for item in rules_source:
        if not isinstance(item, dict):
            continue
        keywords = ensure_string_list(item.get("keywords"))[:20]
        labels = ensure_string_list(item.get("labels"))[:10]
        if keywords and labels:
            selling_point_rules.append({"keywords": keywords, "labels": labels})
    return {
        "taskNameCategoryKeywords": normalize_list_map(source.get("taskNameCategoryKeywords"), fallback.get("taskNameCategoryKeywords", {})),
        "taskNameWaistFocusKeywords": ensure_string_list(source.get("taskNameWaistFocusKeywords") or fallback.get("taskNameWaistFocusKeywords"))[:30],
        "taskNameLightColorKeywords": ensure_string_list(source.get("taskNameLightColorKeywords") or fallback.get("taskNameLightColorKeywords"))[:30],
        "taskNameSellingPointRules": selling_point_rules[:20],
        "visibilityRisksByCategory": normalize_list_map(source.get("visibilityRisksByCategory"), fallback.get("visibilityRisksByCategory", {})),
        "shotRequirementsByCategory": normalize_list_map(source.get("shotRequirementsByCategory"), fallback.get("shotRequirementsByCategory", {})),
    }


def normalize_ai_policy_config_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    fallback = default_ai_policy_config()
    source = payload if isinstance(payload, dict) else {}
    candidate_source = source.get("candidateFunnel") if isinstance(source.get("candidateFunnel"), dict) else {}
    fallback_candidate = fallback["candidateFunnel"]
    source_policies = candidate_source.get("categoryPolicies") if isinstance(candidate_source.get("categoryPolicies"), list) else fallback_candidate["categoryPolicies"]
    fallback_by_category = {str(item["heroCategory"]): item for item in fallback_candidate["categoryPolicies"]}
    normalized_category_policies: list[dict[str, Any]] = []
    for item in source_policies:
        if not isinstance(item, dict):
            continue
        hero_category = str(item.get("heroCategory") or "").strip()
        if not hero_category:
            continue
        fallback_item = fallback_by_category.get(hero_category, {})
        source_gap_rules = item.get("supplyGapCategoryGroups") if isinstance(item.get("supplyGapCategoryGroups"), list) else fallback_item.get("supplyGapCategoryGroups", [])
        normalized_gap_rules = []
        for rule in source_gap_rules:
            if not isinstance(rule, dict):
                continue
            categories = ensure_string_list(rule.get("categories"))[:8]
            if not categories:
                continue
            normalized_gap_rules.append({
                "categories": categories,
                "minimum": clamp_int(rule.get("minimum"), 0, 20, 1),
                "note": str(rule.get("note") or "")[:200],
            })
        normalized_category_policies.append({
            "heroCategory": hero_category[:60],
            "excludedCategories": ensure_string_list(item.get("excludedCategories") or fallback_item.get("excludedCategories"))[:12],
            "categoryScoreAdjustments": normalize_number_map(item.get("categoryScoreAdjustments"), fallback_item.get("categoryScoreAdjustments", {})),
            "draftCategoryPriority": ensure_string_list(item.get("draftCategoryPriority") or fallback_item.get("draftCategoryPriority"))[:12],
            "draftModeByCandidateCategory": normalize_string_map(item.get("draftModeByCandidateCategory"), fallback_item.get("draftModeByCandidateCategory", {})),
            "singlePieceMode": str(item.get("singlePieceMode") or fallback_item.get("singlePieceMode") or "hero_first_single_piece")[:80],
            "singlePieceReason": str(item.get("singlePieceReason") or fallback_item.get("singlePieceReason") or "")[:240],
            "supplyGapCategoryGroups": normalized_gap_rules,
        })
    if not normalized_category_policies:
        normalized_category_policies = fallback_candidate["categoryPolicies"]
    semantic_source = candidate_source.get("semanticSets") if isinstance(candidate_source.get("semanticSets"), dict) else {}
    fallback_semantics = fallback_candidate["semanticSets"]
    quality_source = source.get("qualitySignalPolicy") if isinstance(source.get("qualitySignalPolicy"), dict) else {}
    fallback_quality = fallback["qualitySignalPolicy"]
    signal_source = quality_source.get("signals") if isinstance(quality_source.get("signals"), dict) else {}
    fallback_signals = fallback_quality["signals"]
    normalized_signals: dict[str, Any] = {}
    for key, fallback_signal in fallback_signals.items():
        signal = signal_source.get(key) if isinstance(signal_source.get(key), dict) else {}
        normalized_signals[key] = {
            "enabled": bool(signal.get("enabled", fallback_signal.get("enabled", True))),
            "message": str(signal.get("message") or fallback_signal.get("message") or "")[:240],
        }
        if "topAlternativeCount" in fallback_signal:
            normalized_signals[key]["topAlternativeCount"] = clamp_int(signal.get("topAlternativeCount"), 1, 20, int(fallback_signal.get("topAlternativeCount") or 5))
        for extra_key, fallback_value in fallback_signal.items():
            if extra_key in {"enabled", "message", "topAlternativeCount"}:
                continue
            value = signal.get(extra_key, fallback_value)
            if isinstance(fallback_value, bool):
                normalized_signals[key][extra_key] = bool(value)
            elif isinstance(fallback_value, int):
                normalized_signals[key][extra_key] = clamp_int(value, 0, 100, fallback_value)
            elif isinstance(fallback_value, list):
                normalized_signals[key][extra_key] = ensure_string_list(value or fallback_value)[:20]
            else:
                normalized_signals[key][extra_key] = str(value or fallback_value)[:120]
    accessory_source = source.get("accessoryPolicy") if isinstance(source.get("accessoryPolicy"), dict) else {}
    fallback_accessory = fallback["accessoryPolicy"]
    library_source = source.get("libraryAdequacyPolicy") if isinstance(source.get("libraryAdequacyPolicy"), dict) else {}
    fallback_library = fallback["libraryAdequacyPolicy"]
    brand_source = source.get("brandStylePolicy") if isinstance(source.get("brandStylePolicy"), dict) else {}
    fallback_brand = fallback["brandStylePolicy"]
    evidence_source = brand_source.get("evidence") if isinstance(brand_source.get("evidence"), list) else fallback_brand["evidence"]
    evidence = []
    for item in evidence_source:
        if not isinstance(item, dict):
            continue
        evidence.append({
            "id": str(item.get("id") or "")[:80],
            "source_type": str(item.get("source_type") or "manual_policy")[:80],
            "visual_caption": str(item.get("visual_caption") or "")[:240],
            "brand_style_tags": ensure_string_list(item.get("brand_style_tags"))[:12],
            "accessory_boundary": str(item.get("accessory_boundary") or "")[:240],
        })
    return {
        "schemaVersion": "ai-policy-config-v1",
        "candidateFunnel": {
            "candidateLimit": clamp_int(candidate_source.get("candidateLimit"), 4, 40, fallback_candidate["candidateLimit"]),
            "candidateImageLimit": clamp_int(candidate_source.get("candidateImageLimit"), 4, 30, fallback_candidate["candidateImageLimit"]),
            "hardExcludedCategories": list(
                dict.fromkeys(
                    ensure_string_list(candidate_source.get("hardExcludedCategories") or fallback_candidate["hardExcludedCategories"])
                    + list(SYSTEM_HARD_EXCLUDED_CANDIDATE_CATEGORIES)
                )
            )[:20],
            "categoryCaps": {
                "default": 8,
                **{
                    str(key): clamp_int(value, 1, 30, 8)
                    for key, value in ((candidate_source.get("categoryCaps") if isinstance(candidate_source.get("categoryCaps"), dict) else fallback_candidate["categoryCaps"]).items())
                },
            },
            "categoryPolicies": normalized_category_policies,
            "riskAdjustments": normalize_number_map(candidate_source.get("riskAdjustments"), fallback_candidate["riskAdjustments"]),
            "coverageRiskAdjustments": normalize_number_map(candidate_source.get("coverageRiskAdjustments"), fallback_candidate["coverageRiskAdjustments"]),
            "noStyleRiskValues": ensure_string_list(candidate_source.get("noStyleRiskValues") or fallback_candidate["noStyleRiskValues"])[:20],
            "waistFocusCategoryAdjustments": normalize_number_map(candidate_source.get("waistFocusCategoryAdjustments"), fallback_candidate["waistFocusCategoryAdjustments"]),
            "longTopLengthValues": ensure_string_list(candidate_source.get("longTopLengthValues") or fallback_candidate["longTopLengthValues"])[:20],
            "waistFocusShortUpperCategories": ensure_string_list(candidate_source.get("waistFocusShortUpperCategories") or fallback_candidate["waistFocusShortUpperCategories"])[:20],
            "shortUpperLengthValues": ensure_string_list(candidate_source.get("shortUpperLengthValues") or fallback_candidate["shortUpperLengthValues"])[:20],
            "darkAgainstLightHeroCategories": ensure_string_list(candidate_source.get("darkAgainstLightHeroCategories") or fallback_candidate["darkAgainstLightHeroCategories"])[:20],
            "lightPairingByHeroCategory": normalize_list_map(candidate_source.get("lightPairingByHeroCategory"), fallback_candidate["lightPairingByHeroCategory"]),
            "structuredLayerBonusCategories": ensure_string_list(candidate_source.get("structuredLayerBonusCategories") or fallback_candidate["structuredLayerBonusCategories"])[:20],
            "structuredLayerBonusSilhouettes": ensure_string_list(candidate_source.get("structuredLayerBonusSilhouettes") or fallback_candidate["structuredLayerBonusSilhouettes"])[:20],
            "semanticSets": {
                key: ensure_string_list(semantic_source.get(key) or fallback_semantics.get(key))[:30]
                for key in fallback_semantics
            },
            "heroProfileSellingPointLabels": normalize_string_map(candidate_source.get("heroProfileSellingPointLabels"), fallback_candidate["heroProfileSellingPointLabels"]),
            "defaultDraftMode": str(candidate_source.get("defaultDraftMode") or fallback_candidate.get("defaultDraftMode") or "office_outfit")[:80],
        },
        "heroPolicy": normalize_hero_policy_config(source.get("heroPolicy"), fallback["heroPolicy"]),
        "qualitySignalPolicy": {
            "principle": str(quality_source.get("principle") or fallback_quality["principle"])[:300],
            "flagIf": ensure_string_list(quality_source.get("flagIf") or fallback_quality["flagIf"])[:30],
            "scoreThresholds": normalize_number_map(quality_source.get("scoreThresholds"), fallback_quality["scoreThresholds"]),
            "fullScoreThreshold": clamp_float(quality_source.get("fullScoreThreshold"), 0.5, 1.0, fallback_quality["fullScoreThreshold"]),
            "requiredDiagnosisKeys": ensure_string_list(quality_source.get("requiredDiagnosisKeys") or fallback_quality["requiredDiagnosisKeys"])[:20],
            "forbiddenAccessoryKeywords": ensure_string_list(quality_source.get("forbiddenAccessoryKeywords") or fallback_quality["forbiddenAccessoryKeywords"])[:40],
            "layeringExplanationKeywords": ensure_string_list(quality_source.get("layeringExplanationKeywords") or fallback_quality["layeringExplanationKeywords"])[:40],
            "halfTuckKeywords": ensure_string_list(quality_source.get("halfTuckKeywords") or fallback_quality["halfTuckKeywords"])[:40],
            "fullTuckKeywords": ensure_string_list(quality_source.get("fullTuckKeywords") or fallback_quality["fullTuckKeywords"])[:60],
            "genericTuckKeywords": ensure_string_list(quality_source.get("genericTuckKeywords") or fallback_quality["genericTuckKeywords"])[:20],
            "waistKeywords": ensure_string_list(quality_source.get("waistKeywords") or fallback_quality["waistKeywords"])[:20],
            "fullTuckMethodValues": ensure_string_list(quality_source.get("fullTuckMethodValues") or fallback_quality["fullTuckMethodValues"])[:20],
            "halfTuckMethodValues": ensure_string_list(quality_source.get("halfTuckMethodValues") or fallback_quality["halfTuckMethodValues"])[:20],
            "tuckCheckedHeroCategories": ensure_string_list(quality_source.get("tuckCheckedHeroCategories") or fallback_quality["tuckCheckedHeroCategories"])[:20],
            "tuckCheckedGarmentCategories": ensure_string_list(quality_source.get("tuckCheckedGarmentCategories") or fallback_quality["tuckCheckedGarmentCategories"])[:20],
            "structuredLayerCategories": ensure_string_list(quality_source.get("structuredLayerCategories") or fallback_quality["structuredLayerCategories"])[:20],
            "structuredLayerSubCategories": ensure_string_list(quality_source.get("structuredLayerSubCategories") or fallback_quality["structuredLayerSubCategories"])[:30],
            "structuredLayerDetails": ensure_string_list(quality_source.get("structuredLayerDetails") or fallback_quality["structuredLayerDetails"])[:30],
            "structuredLayerMaterials": ensure_string_list(quality_source.get("structuredLayerMaterials") or fallback_quality["structuredLayerMaterials"])[:30],
            "highWaistHeroKeywords": ensure_string_list(quality_source.get("highWaistHeroKeywords") or fallback_quality["highWaistHeroKeywords"])[:30],
            "highWaistDarkStackCategories": ensure_string_list(quality_source.get("highWaistDarkStackCategories") or fallback_quality["highWaistDarkStackCategories"])[:20],
            "signals": normalized_signals,
        },
        "accessoryPolicy": {
            "allowEmpty": bool(accessory_source.get("allowEmpty", fallback_accessory["allowEmpty"])),
            "default": ensure_string_list(accessory_source.get("default") or fallback_accessory["default"])[:8],
            "avoidDefault": ensure_string_list(accessory_source.get("avoidDefault") or fallback_accessory["avoidDefault"])[:12],
            "bagRule": str(accessory_source.get("bagRule") or fallback_accessory["bagRule"])[:300],
            "jewelryRule": str(accessory_source.get("jewelryRule") or fallback_accessory["jewelryRule"])[:300],
        },
        "libraryAdequacyPolicy": {
            "betterAlternativeLimit": clamp_int(library_source.get("betterAlternativeLimit"), 0, 20, fallback_library["betterAlternativeLimit"]),
            "emptyPairingWhenSupplyGap": bool(library_source.get("emptyPairingWhenSupplyGap", fallback_library["emptyPairingWhenSupplyGap"])),
        },
        "brandStylePolicy": {
            "core": ensure_string_list(brand_source.get("core") or fallback_brand["core"])[:20],
            "principle": str(brand_source.get("principle") or fallback_brand["principle"])[:300],
            "evidence": evidence[:10],
        },
    }


def get_active_ai_policy_config() -> dict[str, Any]:
    try:
        with get_db() as db:
            row = db.execute("SELECT config_json FROM ai_policy_configs WHERE id = 1").fetchone()
        if not row:
            return default_ai_policy_config()
        return normalize_ai_policy_config_payload(loads(row["config_json"], {}))
    except (sqlite3.OperationalError, json.JSONDecodeError, TypeError):
        return default_ai_policy_config()


def build_ai_policy_prompt_card(config: dict[str, Any]) -> str:
    policy = normalize_ai_policy_config_payload(config)
    quality = policy["qualitySignalPolicy"]
    accessory = policy["accessoryPolicy"]
    brand = policy["brandStylePolicy"]
    candidate = policy["candidateFunnel"]
    category_lines = []
    for item in candidate.get("categoryPolicies", []):
        if not isinstance(item, dict):
            continue
        category_lines.append(
            f"- 主推 {item.get('heroCategory')}：{item.get('singlePieceReason') or '由 AI Stylist 判断是否需要库内搭配服饰。'}"
        )
    lines = [
        "# Ruyi-Dream AI 选搭 Prompt 业务规则",
        "执行边界：后端不做候选漏斗、排序、评分、映射、质量判断或改选；以下规则仅作为 Prompt 输入，由 AI Stylist 自行理解和执行。",
        "搭配库输入：runtime.candidate_retrieval 是未归档 READY 搭配服饰的全量标签事实；所有 Prompt 输入、输出和引用服饰都使用 code，不暴露系统内部 ID。",
        "主推品类业务提醒：",
        *category_lines,
        "Aesthetic Critic 维度：" + "、".join(quality["requiredDiagnosisKeys"]) + "。",
        "质量信号提醒：" + str(quality.get("principle") or ""),
        "常见风险：" + "、".join(quality.get("flagIf") or []),
        "生成配饰：" + "；".join([f"默认 {', '.join(accessory['default'])}", f"避免 {', '.join(accessory['avoidDefault'])}", accessory["bagRule"], accessory["jewelryRule"]]),
        "品牌边界：" + "、".join(brand["core"]) + "；" + brand["principle"],
    ]
    return "\n".join(lines).rstrip() + "\n"


def default_shoot_config() -> dict[str, Any]:
    return {
        "schemaVersion": "shoot-config-v2",
        "globalRules": [
            {"id": "brand_tone", "label": "品牌调性", "value": "Lily 商务时装，现代、干净、轻熟、自信、克制，商品优先。", "enabled": True, "sortOrder": 10},
            {"id": "background", "label": "背景", "value": "白色或浅灰棚拍背景，背景不抢服装主体。", "enabled": True, "sortOrder": 20},
            {"id": "lighting", "label": "光线", "value": "柔和均匀、低反差、真实肤色，服装边缘和结构清楚。", "enabled": True, "sortOrder": 30},
            {"id": "model", "label": "模特导演", "value": "同一位数字模特，自然、自信、从容、克制。", "enabled": True, "sortOrder": 40},
            {"id": "hair_makeup", "label": "妆发边界", "value": "保持数字模特原有脸型、五官、头型、发型和气质。", "enabled": True, "sortOrder": 50},
            {"id": "accessory", "label": "配饰边界", "value": "鞋包首饰低存在感，可省略；不得遮挡主推款卖点。", "enabled": True, "sortOrder": 60},
            {"id": "consistency", "label": "一致性规则", "value": "6 个镜头必须是同一位模特、同一套造型、同一背景光线体系。", "enabled": True, "sortOrder": 70},
            {"id": "product_fidelity", "label": "商品保真规则", "value": "主推款图片是主角商品事实来源。选中搭配服饰图片是配角商品事实来源。", "enabled": True, "sortOrder": 80},
            {"id": "negative", "label": "负向约束", "value": "不要文字、水印、logo、编号和边框说明。不要改款换色。不要让生成配饰成为视觉中心。", "enabled": True, "sortOrder": 90},
        ],
        "defaultShotRule": {"shots": shot_template_to_config_rules(build_builtin_category_shot_template("other_garment"))},
        "categoryShotRules": [
            {"categoryDimensionCode": "category", "categoryTagCode": category, "shots": shot_template_to_config_rules(build_builtin_category_shot_template(category))}
            for category in ["裤装", "半身裙", "短裤", "上装", "针织类", "外套类", "连衣裙", "套装"]
        ],
    }


def shot_template_to_config_rules(template: list[tuple[str, str, str, str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "shotType": shot_type,
            "shotRoleLabel": f"镜头 {index}",
            "framing": framing,
            "pose": pose,
            "cameraAngle": angle,
            "mainFocus": focus,
        }
        for index, (shot_type, framing, pose, angle, focus) in enumerate(template[:6], start=1)
    ]


def get_active_shoot_config() -> dict[str, Any]:
    try:
        with get_db() as db:
            row = db.execute("SELECT config_json FROM shoot_configs WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return default_shoot_config()
    if not row:
        return default_shoot_config()
    return normalize_shoot_config_payload(loads(row["config_json"], {}))


def serialize_shoot_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_shoot_config_payload(config)
    normalized["categoryOptions"] = get_shoot_config_category_options()
    return normalized


def normalize_shoot_config_payload(config: dict[str, Any]) -> dict[str, Any]:
    source = config if isinstance(config, dict) else {}
    fallback = default_shoot_config()
    if "globalDirecting" in source or isinstance(source.get("categoryShotRules"), dict):
        source = migrate_shoot_config_v1_to_v2(source)
    normalized = {
        "schemaVersion": "shoot-config-v2",
        "globalRules": normalize_shoot_config_global_rules(source.get("globalRules"), fallback["globalRules"]),
        "defaultShotRule": {
            "shots": normalize_shoot_config_category_rules(
                ((source.get("defaultShotRule") or {}).get("shots") if isinstance(source.get("defaultShotRule"), dict) else None),
                fallback["defaultShotRule"]["shots"],
            )
        },
        "categoryShotRules": normalize_shoot_config_category_rule_overrides(source.get("categoryShotRules"), fallback["categoryShotRules"]),
    }
    return normalized


def migrate_shoot_config_v1_to_v2(source: dict[str, Any]) -> dict[str, Any]:
    global_source = source.get("globalDirecting") if isinstance(source.get("globalDirecting"), dict) else {}
    global_rules = [
        {"id": "brand_tone", "label": "品牌调性", "value": str(global_source.get("brandTone") or ""), "enabled": True, "sortOrder": 10},
        {"id": "background", "label": "背景", "value": str(global_source.get("background") or ""), "enabled": True, "sortOrder": 20},
        {"id": "lighting", "label": "光线", "value": str(global_source.get("lighting") or ""), "enabled": True, "sortOrder": 30},
        {"id": "model", "label": "模特导演", "value": str(global_source.get("modelDirection") or ""), "enabled": True, "sortOrder": 40},
        {"id": "hair_makeup", "label": "妆发边界", "value": str(global_source.get("hairMakeupBoundary") or ""), "enabled": True, "sortOrder": 50},
        {"id": "accessory", "label": "配饰边界", "value": str(global_source.get("accessoryBoundary") or ""), "enabled": True, "sortOrder": 60},
        {"id": "consistency", "label": "一致性规则", "value": "。".join(ensure_string_list(global_source.get("consistencyRules"))), "enabled": True, "sortOrder": 70},
        {"id": "product_fidelity", "label": "商品保真规则", "value": "。".join(ensure_string_list(global_source.get("productFidelityRules"))), "enabled": True, "sortOrder": 80},
        {"id": "negative", "label": "负向约束", "value": "。".join(ensure_string_list(global_source.get("negativeConstraints"))), "enabled": True, "sortOrder": 90},
    ]
    category_source = source.get("categoryShotRules") if isinstance(source.get("categoryShotRules"), dict) else {}
    return {
        "schemaVersion": "shoot-config-v2",
        "globalRules": global_rules,
        "defaultShotRule": {"shots": category_source.get("other_garment") or []},
        "categoryShotRules": [
            {"categoryDimensionCode": "category", "categoryTagCode": str(category), "shots": rules}
            for category, rules in category_source.items()
            if category != "other_garment" and isinstance(rules, list)
        ],
    }


def normalize_shoot_config_global_rules(value: Any, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_rules = value if isinstance(value, list) else fallback
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(source_rules[:40], start=1):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:80]
        rule_value = str(item.get("value") or "").strip()[:2000]
        if not label or not rule_value:
            continue
        rule_id = sanitize_config_id(str(item.get("id") or label))
        if rule_id in seen:
            rule_id = f"{rule_id}_{index}"
        seen.add(rule_id)
        normalized.append(
            {
                "id": rule_id,
                "label": label,
                "value": rule_value,
                "enabled": bool(item.get("enabled", True)),
                "sortOrder": int(item.get("sortOrder") or index * 10),
            }
        )
    return sorted(normalized or fallback, key=lambda item: (int(item.get("sortOrder") or 0), str(item.get("id") or "")))


def sanitize_config_id(value: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in value.strip())
    normalized = "_".join(part for part in normalized.split("_") if part)
    return normalized[:64] or f"rule_{uuid4().hex[:8]}"


def normalize_shoot_config_category_rule_overrides(value: Any, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_rules = value if isinstance(value, list) else fallback
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in source_rules[:80]:
        if not isinstance(item, dict):
            continue
        dimension_code = sanitize_taxonomy_code(str(item.get("categoryDimensionCode") or "category"))
        tag_code = sanitize_taxonomy_code(str(item.get("categoryTagCode") or ""))
        if not tag_code:
            continue
        if tag_code in SHOOT_CONFIG_EXCLUDED_CATEGORY_CODES:
            continue
        key = (dimension_code, tag_code)
        if key in seen:
            continue
        seen.add(key)
        fallback_rules = next(
            (rule.get("shots") for rule in fallback if rule.get("categoryDimensionCode") == dimension_code and rule.get("categoryTagCode") == tag_code),
            default_shoot_config()["defaultShotRule"]["shots"],
        )
        normalized.append(
            {
                "categoryDimensionCode": dimension_code,
                "categoryTagCode": tag_code,
                "shots": normalize_shoot_config_category_rules(item.get("shots"), fallback_rules),
            }
        )
    return normalized


def normalize_shoot_config_category_rules(rules: Any, fallback_rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_rules = rules if isinstance(rules, list) else []
    normalized: list[dict[str, Any]] = []
    for index in range(6):
        source = source_rules[index] if len(source_rules) > index and isinstance(source_rules[index], dict) else {}
        fallback = fallback_rules[index] if len(fallback_rules) > index else {}
        normalized.append(
            {
                "shotType": str(source.get("shotType") or fallback.get("shotType") or "reference")[:120],
                "shotRoleLabel": str(source.get("shotRoleLabel") or fallback.get("shotRoleLabel") or f"镜头 {index + 1}")[:120],
                "framing": str(source.get("framing") or fallback.get("framing") or "")[:500],
                "pose": str(source.get("pose") or fallback.get("pose") or "")[:500],
                "cameraAngle": str(source.get("cameraAngle") or fallback.get("cameraAngle") or "")[:300],
                "mainFocus": str(source.get("mainFocus") or fallback.get("mainFocus") or "")[:500],
            }
        )
    return normalized


def get_shoot_config_category_options() -> list[dict[str, Any]]:
    try:
        with get_db() as db:
            rows = db.execute(
                """
                SELECT * FROM tag_defs
                WHERE dimension_code = 'category'
                  AND (parent_code IS NULL OR parent_code = '')
                  AND level <= 1
                ORDER BY sort_order, id
                """
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "id": row["id"],
            "dimensionCode": row["dimension_code"],
            "code": row["code"],
            "labelZh": row["label_zh"],
            "isActive": bool(row["is_active"]),
            "sortOrder": row["sort_order"],
        }
        for row in rows
    ]


def build_shoot_script_request_snapshot(task_id: int, styling_id: int, input_version_id: int) -> dict[str, Any]:
    styling_plan = get_styling_plan_by_id(styling_id)
    shoot_config = get_active_shoot_config()
    with get_db() as db:
        input_row = db.execute("SELECT * FROM generation_task_input_versions WHERE id = ?", (input_version_id,)).fetchone()
        styling_row = db.execute("SELECT * FROM generation_stylings WHERE id = ?", (styling_id,)).fetchone()
    selected_snapshot = loads(styling_row["selected_material_snapshot_json"], []) if styling_row else []
    candidate_snapshot = loads(styling_row["candidate_retrieval_snapshot_json"], {}) if styling_row and "candidate_retrieval_snapshot_json" in styling_row.keys() else {}
    ai_styling_prompt = str(candidate_snapshot.get("ai_styling_prompt") or "") if isinstance(candidate_snapshot, dict) else ""
    ai_styling_prompt_config_key = str(candidate_snapshot.get("ai_styling_prompt_config_key") or AI_STYLING_PROMPT_CONFIG_KEY) if isinstance(candidate_snapshot, dict) else AI_STYLING_PROMPT_CONFIG_KEY
    return {
        "schema_version": "shoot-script-request-v1",
        "model": settings.gemini_model,
        "task_id": task_id,
        "input_version": {
            "id": input_version_id,
            "name_snapshot": input_row["name_snapshot"] if input_row else "",
            "image_snapshot": loads(input_row["image_snapshot_json"], []) if input_row else [],
            "model_snapshot": loads(input_row["model_snapshot_json"], {}) if input_row and "model_snapshot_json" in input_row.keys() else {},
        },
        "styling_id": styling_id,
        "styling_plan": styling_plan,
        "styling_result": styling_result_for_prompt(styling_plan, selected_snapshot),
        "selected_material_snapshot": selected_snapshot,
        "brand_info": get_current_brand_info() or {},
        "shoot_config": shoot_config,
        "ai_styling_prompt_config_key": ai_styling_prompt_config_key,
        "ai_styling_prompt": ai_styling_prompt,
        "base_shot_rules": [],
        "category_rule_summary": build_shoot_script_category_rule_summary(shoot_config),
        "accessory_boundary": candidate_snapshot.get("accessory_boundary") if isinstance(candidate_snapshot, dict) else build_accessory_boundary(),
        "output_schema": "shoot-script-v1",
    }


def build_shoot_script_category_rule_summary(shoot_config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = shoot_config or get_active_shoot_config()
    category_rules = config.get("categoryShotRules") if isinstance(config.get("categoryShotRules"), list) else []
    category_options = {
        (str(option["dimensionCode"]), str(option["code"])): option
        for option in get_shoot_config_category_options()
    }
    summary = {
        "universal_six_shots": [
            "1 全身主视觉：建立整体比例和第一视觉。",
            "2 品类核心展示：展示该品类最重要结构。",
            "3 设计细节近景：展示卖点、结构、面料或工艺。",
            "4 身位/动态补充：展示侧面、背面、走动或动态。",
            "5 标准正面颜色图：平台友好、颜色和版型稳定展示。",
            "6 备用姿态：只作预览参考，不进入最终成片。",
        ],
        "default": format_shoot_config_shot_rules_for_prompt((config.get("defaultShotRule") or {}).get("shots") if isinstance(config.get("defaultShotRule"), dict) else []),
    }
    for item in category_rules:
        if not isinstance(item, dict):
            continue
        dimension_code = str(item.get("categoryDimensionCode") or "category")
        tag_code = str(item.get("categoryTagCode") or "")
        if tag_code in SHOOT_CONFIG_EXCLUDED_CATEGORY_CODES:
            continue
        option = category_options.get((dimension_code, tag_code))
        label = str(option.get("labelZh") or tag_code) if option else tag_code
        key = f"{dimension_code}:{tag_code} {label}".strip()
        summary[key] = format_shoot_config_shot_rules_for_prompt(item.get("shots"))
    return summary


def format_shoot_config_shot_rules_for_prompt(rules: Any) -> str:
    if not isinstance(rules, list):
        return ""
    return "；".join(
        f"{index}. {rule.get('shotRoleLabel') or rule.get('shotType')}: {rule.get('framing')}，重点 {rule.get('mainFocus')}"
        for index, rule in enumerate(rules[:6], start=1)
        if isinstance(rule, dict)
    )


async def generate_shoot_script_with_gemini(request_snapshot: dict[str, Any]) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise RuntimeError("Gemini API 未配置，拍摄剧本不能进入生产生成。")
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        raise RuntimeError("google-genai SDK 不可用，拍摄剧本不能进入生产生成。") from exc
    if not request_snapshot.get("prompt"):
        request_snapshot = attach_shoot_script_prompt_snapshot(request_snapshot)
    prompt = str(request_snapshot.get("prompt") or "")
    contents: list[Any] = [prompt]
    for item in ((request_snapshot.get("input_version") or {}).get("image_snapshot") or [])[:6]:
        asset = item.get("asset") if isinstance(item, dict) else {}
        object_key = asset.get("objectKey") if isinstance(asset, dict) else None
        if object_key and object_exists(object_key):
            contents.append(types.Part.from_bytes(data=get_bytes(object_key), mime_type=asset.get("mimeType") or "image/png"))
    for material in request_snapshot.get("selected_material_snapshot", [])[:4]:
        images = material.get("images") if isinstance(material, dict) else []
        first = images[0] if images else {}
        asset = first.get("asset") if isinstance(first, dict) else {}
        object_key = asset.get("objectKey") if isinstance(asset, dict) else None
        if object_key and object_exists(object_key):
            contents.append(types.Part.from_bytes(data=get_bytes(object_key), mime_type=asset.get("mimeType") or "image/png"))
    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = await client.aio.models.generate_content(model=settings.gemini_model, contents=contents)
        return loads(extract_json_text(getattr(response, "text", "") or "{}"), {})
    except Exception as exc:
        raise RuntimeError(f"Gemini 拍摄剧本调用失败：{exc}") from exc


def attach_shoot_script_prompt_snapshot(request_snapshot: dict[str, Any]) -> dict[str, Any]:
    prompt = render_prompt_template(
        SHOOT_SCRIPT_PROMPT_CONFIG_KEY,
        {
            "styling_result": dumps(request_snapshot.get("styling_result") or styling_result_for_prompt(request_snapshot.get("styling_plan") if isinstance(request_snapshot.get("styling_plan"), dict) else {})),
        },
    )
    return {**request_snapshot, "promptConfigKey": SHOOT_SCRIPT_PROMPT_CONFIG_KEY, "prompt": prompt}


def normalize_shoot_script(raw: dict[str, Any], request_snapshot: dict[str, Any]) -> dict[str, Any]:
    styling_plan = request_snapshot.get("styling_plan") if isinstance(request_snapshot.get("styling_plan"), dict) else {}
    base_plans = request_snapshot.get("base_shot_rules") if isinstance(request_snapshot.get("base_shot_rules"), list) else []
    hero = styling_plan.get("hero_understanding") if isinstance(styling_plan.get("hero_understanding"), dict) else {}
    source = raw if isinstance(raw, dict) else {}
    input_version = request_snapshot.get("input_version") if isinstance(request_snapshot.get("input_version"), dict) else {}
    shoot_config = request_snapshot.get("shoot_config") if isinstance(request_snapshot.get("shoot_config"), dict) else default_shoot_config()
    config_global_rules = [
        {"label": str(item.get("label") or ""), "value": str(item.get("value") or "")}
        for item in (shoot_config.get("globalRules") if isinstance(shoot_config.get("globalRules"), list) else default_shoot_config()["globalRules"])
        if isinstance(item, dict) and item.get("enabled", True) and str(item.get("label") or "").strip() and str(item.get("value") or "").strip()
    ]
    global_directing = source.get("global_directing") if isinstance(source.get("global_directing"), dict) else {}
    fallback_consistency_rules = [value for value in [global_rule_value(config_global_rules, ["一致性规则"])] if value]
    fallback_product_fidelity_rules = [value for value in [global_rule_value(config_global_rules, ["商品保真规则", "商品保真"])] if value]
    fallback_negative_constraints = [value for value in [global_rule_value(config_global_rules, ["负向约束"])] if value]
    selected_outfit = selected_outfit_from_plan(styling_plan)
    accessory_adjustments = normalize_accessory_adjustments(source.get("accessory_adjustments"))
    resolved_accessories = normalize_resolved_accessories(source.get("resolved_accessories"), accessory_adjustments, selected_outfit)
    normalized = {
        "schema_version": "shoot-script-v1",
        "script_source_mode": str(source.get("script_source_mode") or "production"),
        "input_refs": {
            "task_id": int(request_snapshot.get("task_id") or 0),
            "input_version_id": int(input_version.get("id") or 0),
            "styling_id": int(request_snapshot.get("styling_id") or 0),
            "styling_plan_version": str((styling_plan.get("schema_version") if isinstance(styling_plan, dict) else "") or "styling-plan-v1"),
            "model_snapshot_id": str(((input_version.get("model_snapshot") or {}).get("modelId") if isinstance(input_version.get("model_snapshot"), dict) else "") or "Face-01"),
        },
        "global_directing": {
            "rules": normalize_shoot_script_global_rules(global_directing.get("rules"), config_global_rules),
            "brand_tone": str(global_directing.get("brand_tone") or global_rule_value(config_global_rules, ["品牌调性", "品牌风格"])),
            "background": str(global_directing.get("background") or global_rule_value(config_global_rules, ["背景"])),
            "lighting": str(global_directing.get("lighting") or global_rule_value(config_global_rules, ["光线"])),
            "model_direction": str(global_directing.get("model_direction") or global_rule_value(config_global_rules, ["模特导演", "模特"])),
            "hair_makeup_boundary": str(global_directing.get("hair_makeup_boundary") or global_rule_value(config_global_rules, ["妆发边界", "妆发"])),
            "accessory_boundary": str(global_directing.get("accessory_boundary") or global_rule_value(config_global_rules, ["配饰边界", "配饰"])),
            "consistency_rules": ensure_string_list(global_directing.get("consistency_rules")) or fallback_consistency_rules,
            "product_fidelity_rules": ensure_string_list(global_directing.get("product_fidelity_rules")) or fallback_product_fidelity_rules,
            "negative_constraints": ensure_string_list(global_directing.get("negative_constraints")) or fallback_negative_constraints,
        },
        "styling_risks": normalize_styling_risks(source.get("styling_risks")),
        "accessory_adjustments": accessory_adjustments,
        "resolved_accessories": resolved_accessories,
        "shots": normalize_shoot_script_shots(source.get("shots"), base_plans, hero, resolved_accessories),
        "user_visible_summary": normalize_shoot_script_summary(source.get("user_visible_summary"), hero),
        "warnings": ensure_string_list(source.get("warnings")),
    }
    return normalized


def normalize_styling_risks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    risks: list[dict[str, Any]] = []
    for index, item in enumerate(value[:12], start=1):
        if not isinstance(item, dict):
            continue
        risks.append(
            {
                "risk_id": str(item.get("risk_id") or f"risk_{index}"),
                "severity": str(item.get("severity") or "medium"),
                "material_id": item.get("material_id"),
                "material_code": str(item.get("material_code") or ""),
                "risk": str(item.get("risk") or ""),
                "affected_shots": [int(x) for x in ensure_string_list(item.get("affected_shots")) if str(x).isdigit()][:6],
                "director_strategy": str(item.get("director_strategy") or ""),
                "user_visible_recommendation": str(item.get("user_visible_recommendation") or ""),
            }
        )
    return risks


def normalize_shoot_script_global_rules(value: Any, fallback_rules: list[dict[str, str]]) -> list[dict[str, str]]:
    if isinstance(value, list):
        rules = [
            {"label": str(item.get("label") or ""), "value": str(item.get("value") or "")}
            for item in value
            if isinstance(item, dict) and str(item.get("label") or "").strip() and str(item.get("value") or "").strip()
        ]
        if rules:
            return rules[:40]
    return fallback_rules[:40]


def global_rule_value(rules: list[dict[str, str]], labels: list[str]) -> str:
    for label in labels:
        for item in rules:
            if str(item.get("label") or "") == label:
                return str(item.get("value") or "")
    return ""


def normalize_accessory_adjustments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    adjustments: list[dict[str, Any]] = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        adjustments.append(
            {
                "item_type": str(item.get("item_type") or "other"),
                "original_direction": str(item.get("original_direction") or ""),
                "adjusted_direction": str(item.get("adjusted_direction") or ""),
                "reason": str(item.get("reason") or ""),
                "affected_shots": [int(x) for x in ensure_string_list(item.get("affected_shots")) if str(x).isdigit()][:6],
            }
        )
    return adjustments


def normalize_resolved_accessories(value: Any, adjustments: list[dict[str, Any]], selected_outfit: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(value, list) and value:
        resolved = [
            normalize_resolved_accessory_item(item)
            for item in value[:12]
            if isinstance(item, dict)
        ]
        if resolved:
            return resolved
    initial_accessories = [
        normalize_initial_accessory_text(text)
        for text in ensure_string_list(selected_outfit.get("generated_accessories"))[:8]
    ]
    resolved_by_type = {item["item_type"]: item for item in initial_accessories}
    for index, adjustment in enumerate(adjustments, start=1):
        item_type = normalize_accessory_type(adjustment.get("item_type") or infer_accessory_type(adjustment.get("adjusted_direction") or adjustment.get("original_direction")))
        final_direction = str(adjustment.get("adjusted_direction") or adjustment.get("original_direction") or "按导演配饰边界低存在感处理").strip()
        visibility = "none" if accessory_direction_is_removed(final_direction) else "low"
        resolved_by_type[item_type or f"other_{index}"] = {
            "item_type": item_type,
            "final_direction": final_direction[:180],
            "source": "removed" if visibility == "none" else "director_adjusted",
            "visibility": visibility,
            "applies_to_shots": normalize_int_list(adjustment.get("affected_shots"), limit=6),
            "reason": str(adjustment.get("reason") or "")[:180],
        }
    return list(resolved_by_type.values())[:12]


def normalize_resolved_accessory_item(item: dict[str, Any]) -> dict[str, Any]:
    final_direction = str(item.get("final_direction") or item.get("direction") or item.get("adjusted_direction") or "").strip()
    visibility = str(item.get("visibility") or "").strip() or ("none" if accessory_direction_is_removed(final_direction) else "low")
    if visibility not in {"none", "low", "medium"}:
        visibility = "low"
    source = str(item.get("source") or "director_adjusted")
    if source not in {"styling_plan", "director_adjusted", "director_added", "removed"}:
        source = "director_adjusted"
    return {
        "item_type": normalize_accessory_type(item.get("item_type") or infer_accessory_type(final_direction)),
        "final_direction": final_direction[:180] or "按导演配饰边界低存在感处理",
        "source": source,
        "visibility": visibility,
        "applies_to_shots": normalize_int_list(item.get("applies_to_shots") or item.get("affected_shots"), limit=6),
        "reason": str(item.get("reason") or "")[:180],
    }


def normalize_initial_accessory_text(text: str) -> dict[str, Any]:
    return {
        "item_type": normalize_accessory_type(infer_accessory_type(text)),
        "final_direction": str(text)[:180],
        "source": "styling_plan",
        "visibility": "low",
        "applies_to_shots": [],
        "reason": "来自 AI Stylist 初始生成配饰建议，AI Director 未调整。",
    }


def normalize_accessory_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"footwear", "shoes", "shoe", "鞋", "鞋履"}:
        return "footwear"
    if text in {"bag", "包", "包袋"}:
        return "bag"
    if text in {"jewelry", "jewellery", "首饰", "饰品", "耳饰", "项链", "腕表"}:
        return "jewelry"
    if text in {"hat", "帽", "帽子"}:
        return "hat"
    return "other"


def infer_accessory_type(value: Any) -> str:
    text = str(value or "").lower()
    if any(keyword in text for keyword in ("鞋", "靴", "乐福", "高跟", "凉鞋", "loafer", "heel", "boot")):
        return "footwear"
    if any(keyword in text for keyword in ("包", "手袋", "托特", "肩背", "clutch", "bag", "tote")):
        return "bag"
    if any(keyword in text for keyword in ("耳", "项链", "戒指", "腕表", "手表", "首饰", "jewelry", "watch")):
        return "jewelry"
    if any(keyword in text for keyword in ("帽", "hat")):
        return "hat"
    return "other"


def accessory_direction_is_removed(value: str) -> bool:
    return any(keyword in str(value or "") for keyword in ("取消", "不使用", "省略", "不出现", "不要出现", "移除", "none", "remove"))


def normalize_int_list(value: Any, limit: int) -> list[int]:
    items = value if isinstance(value, list) else []
    normalized: list[int] = []
    for item in items:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number not in normalized:
            normalized.append(number)
    return normalized[:limit]


def accessory_instructions_for_shot(index: int, resolved_accessories: list[dict[str, Any]]) -> list[str]:
    instructions: list[str] = []
    for item in resolved_accessories:
        applies = item.get("applies_to_shots") if isinstance(item.get("applies_to_shots"), list) else []
        if applies and index not in [int(value) for value in applies if str(value).isdigit()]:
            continue
        direction = str(item.get("final_direction") or "").strip()
        if not direction:
            continue
        if item.get("visibility") == "none":
            instructions.append(f"{item.get('item_type')}: {direction}")
        else:
            instructions.append(f"{item.get('item_type')}: {direction}，低存在感，不做特写。")
    return instructions[:6] or ["生成配饰低存在感，可省略，不做特写，不遮挡主推款和搭配服饰。"]


def normalize_shoot_script_shots(value: Any, base_plans: list[Any], hero: dict[str, Any], resolved_accessories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_shots = value if isinstance(value, list) and value else []
    shots: list[dict[str, Any]] = []
    for index in range(1, 7):
        source = next((item for item in source_shots if isinstance(item, dict) and int(item.get("shot_index") or 0) == index), None)
        base = base_plans[index - 1] if len(base_plans) >= index and isinstance(base_plans[index - 1], dict) else {}
        shots.append(normalize_shoot_script_shot(source or {}, base, hero, index, resolved_accessories))
    return shots


def normalize_shoot_script_shot(source: dict[str, Any], base: dict[str, Any], hero: dict[str, Any], index: int, resolved_accessories: list[dict[str, Any]]) -> dict[str, Any]:
    shot_type = str(source.get("shot_role") or base.get("shotType") or "reference")
    return {
        "shot_index": index,
        "shot_role": shot_type,
        "shot_role_label": str(source.get("shot_role_label") or shot_type),
        "framing": str(source.get("framing") or base.get("framing") or ""),
        "crop_range": str(source.get("crop_range") or source.get("cropRange") or base.get("framing") or ""),
        "model_scale": str(source.get("model_scale") or source.get("modelScale") or ""),
        "camera_angle": str(source.get("camera_angle") or source.get("cameraAngle") or base.get("cameraAngle") or ""),
        "body_position": str(source.get("body_position") or source.get("bodyPosition") or base.get("cameraAngle") or ""),
        "pose": str(source.get("pose") or base.get("pose") or ""),
        "movement": str(source.get("movement") or ""),
        "hand_position": str(source.get("hand_position") or source.get("handPosition") or ""),
        "main_focus": str(source.get("main_focus") or source.get("mainFocus") or base.get("mainFocus") or ""),
        "visible_garments": ensure_string_list(source.get("visible_garments") or source.get("visibleGarments") or base.get("visibleGarments")),
        "hero_selling_points": ensure_string_list(source.get("hero_selling_points") or source.get("heroSellingPoints") or base.get("heroSellingPoints") or hero.get("hero_selling_points")),
        "forbidden_occlusion_zones": ensure_string_list(source.get("forbidden_occlusion_zones") or source.get("forbiddenOcclusionZones") or hero.get("forbidden_occlusion_zones") or hero.get("hero_visibility_risks")),
        "composition_notes": str(source.get("composition_notes") or source.get("compositionNotes") or base.get("compositionNotes") or ""),
        "accessory_instructions": ensure_string_list(source.get("accessory_instructions") or source.get("accessoryInstructions")) or accessory_instructions_for_shot(index, resolved_accessories),
        "acceptance_criteria": ensure_string_list(source.get("acceptance_criteria") or source.get("acceptanceCriteria")) or ["主推款品类、颜色、版型和长度清楚。", "搭配服饰服务主推款，不抢视觉中心。"],
        "negative_constraints": ensure_string_list(source.get("negative_constraints") or source.get("negativeConstraints") or base.get("negativeConstraints")),
    }


def normalize_shoot_script_summary(value: Any, hero: dict[str, Any]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "title": str(source.get("title") or "Lily 电商棚拍拍摄剧本"),
        "summary": str(source.get("summary") or "本组拍摄以主推款商品表达为核心，覆盖完整比例、品类核心、设计细节、动态补充和标准正面图。"),
        "key_points": ensure_string_list(source.get("key_points")) or ensure_string_list(hero.get("shot_requirements")),
        "risk_summary": str(source.get("risk_summary") or ""),
    }


def validate_shoot_script(script: dict[str, Any]) -> None:
    if script.get("schema_version") != "shoot-script-v1":
        raise RuntimeError("ShootScript schema_version 无效")
    shots = script.get("shots")
    if not isinstance(shots, list) or len(shots) != 6:
        raise RuntimeError("ShootScript 必须包含 6 个镜头")
    indexes = [int(item.get("shot_index") or 0) for item in shots if isinstance(item, dict)]
    if sorted(indexes) != [1, 2, 3, 4, 5, 6]:
        raise RuntimeError("ShootScript 镜头序号必须为 1-6 且不重复")


def shoot_script_to_generation_shot_plans(script: dict[str, Any]) -> list[dict[str, Any]]:
    plans = []
    for shot in script.get("shots", []):
        index = int(shot.get("shot_index") or len(plans) + 1)
        plans.append(
            {
                "sortOrder": index,
                "shotType": str(shot.get("shot_role") or "reference"),
                "framing": str(shot.get("framing") or ""),
                "pose": str(shot.get("pose") or ""),
                "cameraAngle": str(shot.get("camera_angle") or ""),
                "mainFocus": str(shot.get("main_focus") or ""),
                "plan": shot,
                **shot,
            }
        )
    return plans


def build_category_shot_template(hero_category: str) -> list[tuple[str, str, str, str, str]]:
    config = get_active_shoot_config()
    rules = find_shoot_config_category_shot_rules(config, hero_category)
    if rules is None and isinstance(config.get("defaultShotRule"), dict):
        rules = config.get("defaultShotRule", {}).get("shots")
    if isinstance(rules, list) and len(rules) >= 6:
        return shoot_config_rules_to_template(rules)
    return build_builtin_category_shot_template("other_garment")


def find_shoot_config_category_shot_rules(config: dict[str, Any], hero_category: str) -> list[dict[str, Any]] | None:
    category_code = str(hero_category or "").strip()
    if not category_code or category_code in SHOOT_CONFIG_EXCLUDED_CATEGORY_CODES:
        return None
    category_rules = config.get("categoryShotRules") if isinstance(config.get("categoryShotRules"), list) else []
    for item in category_rules:
        if not isinstance(item, dict):
            continue
        if str(item.get("categoryDimensionCode") or "category") == "category" and str(item.get("categoryTagCode") or "") == category_code:
            shots = item.get("shots")
            return shots if isinstance(shots, list) else None
    return None


def shoot_config_rules_to_template(rules: list[dict[str, Any]]) -> list[tuple[str, str, str, str, str]]:
    template = [
        (
            str(rule.get("shotType") or "reference"),
            str(rule.get("framing") or ""),
            str(rule.get("pose") or ""),
            str(rule.get("cameraAngle") or ""),
            str(rule.get("mainFocus") or ""),
        )
        for rule in rules[:6]
        if isinstance(rule, dict)
    ]
    if len(template) == 6:
        return template
    return build_builtin_category_shot_template("other_garment")


def build_builtin_category_shot_template(hero_category: str) -> list[tuple[str, str, str, str, str]]:
    if hero_category == "裤装":
        return [
            ("pants_hero", "全身或近全身主视觉，裤脚和鞋完整", "腿部自然分开，一腿微前，手自然垂放或单手插袋", "正面或轻斜正", "裤装完整裤型、腿长比例、整体 look"),
            ("pants_waist_to_feet", "腰脚下装图或近全身", "双手插袋或单手插袋，腰头完整露出", "正面", "腰头、门襟、裤褶、裤腿、裤长"),
            ("pants_waist_detail", "腰膝或腰胯近景", "手可插袋或轻扶腰头，不遮门襟", "正面或 3/4 斜正", "腰带扣、褶位、口袋、面料挺括度"),
            ("pants_side_motion", "全身、近全身或腰脚下装图", "3/4 斜正或侧身轻走动", "侧面或轻斜侧", "侧缝、垂坠、裤脚动态、裤腿宽度"),
            ("pants_front_fit", "腰脚下装图或近全身", "稳定克制，裤线清楚", "正面", "颜色、版型、裤长、落脚关系"),
            ("reference", "备用姿态", "轻斜正自然姿态", "自然", "参考，不进入最终生成"),
        ]
    if hero_category in {"上装", "针织类"}:
        return [
            ("top_hero", "全身图，上衣清楚，下装辅助比例", "肩颈打开，重心自然", "正面或轻斜正", "上衣整体穿着比例"),
            ("top_core", "臀上图或大腿中部图", "插袋、扶腰或自然垂手", "正面或 3/4 斜正", "领口、肩线、下摆、上衣版型"),
            ("top_detail", "胸腰近景", "手臂自然入画", "正面或轻斜正", "门襟、扣子、袖口、面料肌理"),
            ("top_side_motion", "臀上图、七分身或全身图", "轻走动、转肩或扶包", "3/4 斜正或轻侧身", "袖型、侧面衣身、下摆垂坠"),
            ("top_front_fit", "臀上图或大腿中部图", "稳定克制", "正面", "颜色、版型、正面结构"),
            ("reference", "备用姿态", "轻斜正替代姿态", "自然", "参考，不进入最终生成"),
        ]
    if hero_category == "外套类":
        return [
            ("outerwear_hero", "全身或近全身", "自然站立，手臂留出门襟", "正面或轻斜正", "外套整体轮廓"),
            ("outerwear_core", "臀上图、大腿中部图或七分身", "插袋、轻扶衣襟或自然垂手", "正面或 3/4 斜正", "领型、门襟、肩线、内搭关系"),
            ("outerwear_detail", "胸腰近景", "手轻触纽扣或门襟", "正面或轻侧", "扣位、口袋、面料、翻领"),
            ("outerwear_side_back", "全身或七分身", "轻走动或回身", "侧身、背面或 3/4", "厚薄、衣长、后片、开衩"),
            ("outerwear_front_fit", "臀上图或全身图", "稳定克制", "正面", "颜色、版型、平台友好"),
            ("reference", "备用姿态", "轻斜正替代姿态", "自然", "参考，不进入最终生成"),
        ]
    if hero_category == "连衣裙":
        return [
            ("dress_hero", "全身图，裙摆完整", "稳定自然", "正面或轻斜正", "连衣裙整体裙型"),
            ("dress_core", "全身、近全身或七分身", "轻转身、重心偏移", "3/4 斜正", "腰线、裙摆、身体轮廓"),
            ("dress_detail", "胸腰近景或腰膝近景", "手臂自然辅助", "正面或轻斜正", "领口、袖型、腰部结构"),
            ("dress_side_motion", "全身或近全身", "轻走动或回身", "背面、侧身或 3/4", "后片、裙长、开衩、摆量"),
            ("dress_front_fit", "全身或近全身", "稳定克制", "正面", "颜色、版型、平台友好"),
            ("reference", "备用姿态", "轻斜正替代姿态", "自然", "参考，不进入最终生成"),
        ]
    return [
        ("hero", "正面全身主视觉", "轻微重心变化，避免站军姿", "正面或轻斜正", "主推款完整轮廓，搭配服饰自然可见"),
        ("core", "品类核心展示", "自然克制姿态", "正面或 3/4", "主推款核心结构和比例"),
        ("detail", "设计细节近景", "局部近景，可不露脸", "局部", "主推款关键设计点"),
        ("side", "侧身或动态补充", "侧身轻走动感", "侧面或 3/4", "主推款侧面轮廓和动态"),
        ("front_fit", "标准正面颜色图", "稳定克制", "正面", "颜色、版型、平台友好"),
        ("reference", "参考格", "备用姿态", "自然", "参考，不进入最终生成"),
    ]


def serialize_new_shot_plan(row: Any) -> dict[str, Any]:
    plan = loads(row["plan_json"], {})
    return {"id": row["id"], "sortOrder": row["sort_order"], "shotType": row["shot_type"], "plan": plan}


def build_new_preview_task_package(task_id: int, styling_id: int, shoot_script_id: int, shot_plans: list[dict[str, Any]]) -> dict[str, Any]:
    styling_plan = get_styling_plan_by_id(styling_id)
    shoot_script = get_shoot_script_json_by_id(shoot_script_id)
    return {
        "generation": {"stage": "preview_grid"},
        "prompt": build_new_preview_prompt(shot_plans, styling_plan, shoot_script),
        "imageInputs": build_new_image_inputs(task_id, styling_id, include_preview=False),
        "stylingPlan": styling_plan,
        "shootScript": shoot_script,
        "shotPlans": shot_plans,
    }


def build_new_preview_prompt(shot_plans: list[dict[str, Any]], styling_plan: dict[str, Any], shoot_script: dict[str, Any]) -> str:
    return render_prompt_template(
        "assembly.storyboard_preview",
        {
            "shoot_script_result": dumps(shoot_script_result_for_prompt(shoot_script)),
        },
    )


def build_new_final_task_package(task_id: int, result_image_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gri.*, gsp.plan_json, gp.preview_asset_id, grs.styling_id, grs.shoot_script_id
            FROM generation_result_images_new gri
            JOIN generation_result_sets grs ON grs.id = gri.result_set_id
            JOIN generation_previews gp ON gp.id = grs.preview_id
            JOIN generation_shot_plans gsp ON gsp.id = gri.shot_plan_id
            WHERE gri.id = ?
            """,
            (result_image_id,),
        ).fetchone()
    shot_plan = loads(row["plan_json"], {})
    styling_plan = get_styling_plan_by_id(int(row["styling_id"]))
    shoot_script = get_shoot_script_json_by_id(int(row["shoot_script_id"])) if row["shoot_script_id"] else {}
    return {
        "generation": {"stage": "final_image"},
        "prompt": build_new_final_prompt(int(row["sort_order"]), shot_plan, styling_plan, shoot_script),
        "imageInputs": build_new_image_inputs(task_id, int(row["styling_id"]), include_preview=True, preview_asset_id=row["preview_asset_id"]),
        "stylingPlan": styling_plan,
        "shootScript": shoot_script,
        "shotPlan": shot_plan,
    }


def build_new_final_prompt(shot_index: int, shot_plan: dict[str, Any], styling_plan: dict[str, Any], shoot_script: dict[str, Any]) -> str:
    return render_prompt_template(
        "assembly.final_image",
        {
            "current_shot_context": dumps(current_shot_context_for_prompt(shot_index, shot_plan, styling_plan, shoot_script)),
        },
    )


def get_styling_plan_by_id(styling_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT styling_plan_json FROM generation_stylings WHERE id = ?", (styling_id,)).fetchone()
    if not row:
        return {}
    return loads(row["styling_plan_json"], {})


def get_shoot_script_json_by_id(shoot_script_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT script_json FROM generation_shoot_scripts WHERE id = ?", (shoot_script_id,)).fetchone()
    if not row:
        return {}
    return loads(row["script_json"], {})


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


def normalize_prompt_config_key(value: str) -> str:
    return str(value or "").strip().strip("/")


def repair_prompt_placeholder_text(value: str) -> str:
    return re.sub(r"(?<!{){((?:segment|runtime)\.[a-zA-Z0-9_.-]+)}(?!})", r"{{\1}}", value)


def merge_legacy_ai_policy_placeholders(value: str) -> str:
    if "{{runtime.ai_policy_card}}" not in value and "{{runtime.ai_policy}}" not in value:
        return value
    policy_text = (
        build_ai_policy_prompt_card(get_active_ai_policy_config())
        + "\n以上为原独立 AI 选搭策略配置迁移后的纯文字规则；后续请直接在 AI 选搭 Prompt 正文中维护。\n"
    )
    return (
        value
        .replace("{{runtime.ai_policy_card}}", policy_text)
        .replace("{{runtime.ai_policy}}", policy_text)
    )


DEPRECATED_PROMPT_CONFIG_KEYS = {"segment.tag_rules", "segment.director_rules", "segment.shot_rules"}


def migrate_prompt_runtime_placeholders(key: str, value: str) -> str:
    migrated = value
    migrated = migrated.replace("{{runtime.taxonomy_prompt_card}}", "")
    migrated = migrated.replace("{{runtime.garment}}", "")
    if key == AI_STYLING_PROMPT_CONFIG_KEY:
        migrated = migrate_ai_styling_prompt_to_code_only(migrated)
    if key == SHOOT_SCRIPT_PROMPT_CONFIG_KEY and (
        "{{runtime.request_snapshot}}" in migrated
        or "material_id" in migrated
        or '"task_id"' in migrated
        or '"input_version_id"' in migrated
    ):
        migrated = shoot_script_prompt_template_v2()
    if key == "assembly.storyboard_preview" and any(
        placeholder in migrated
        for placeholder in ("{{runtime.styling_plan}}", "{{runtime.shoot_script}}", "{{runtime.shot_plans}}")
    ):
        migrated = storyboard_preview_prompt_template_v2()
    if key == "assembly.final_image" and any(
        placeholder in migrated
        for placeholder in ("{{runtime.shot_index}}", "{{runtime.styling_plan}}", "{{runtime.shoot_script}}", "{{runtime.shot_plan}}")
    ):
        migrated = final_image_prompt_template_v2()
    return migrated


def migrate_ai_styling_prompt_to_code_only(value: str) -> str:
    migrated = value
    migrated = migrated.replace("candidate_retrieval.library_tag_cards 会展示搭配服饰库中全量标签信息", "candidate_retrieval 会展示搭配服饰库中所有可用服饰的 code 与全量标签事实")
    migrated = migrated.replace("允许 selected_material_ids 为空；候选库没有真正提升主推款的配角服饰时，应选择空库内搭配方案", "允许 selected_material_codes 为空；候选库没有真正提升主推款的配角服饰时，应选择空库内搭配方案")
    migrated = migrated.replace("material_id 必须来自 candidate_material_ids；material_code 必须原样复制对应标签卡 code；不得编造、替换或反查其它 ID", "material_code 必须原样复制对应标签卡 code；不得编造、替换或使用系统内部 ID")
    migrated = migrated.replace("系统 ID / material_id", "系统内部 ID")
    migrated = migrated.replace('"selected_material_ids": [123],\n', "")
    migrated = migrated.replace('"material_id": 123,\n      ', "")
    migrated = migrated.replace('"selected_material_ids": [123],\n  ', "")
    migrated = migrated.replace('"material_id": 123, ', "")
    migrated = migrated.replace('"selected_material_ids": [],\n', "")
    migrated = migrated.replace('"selected_material_ids": [123],', "")
    migrated = migrated.replace('"selected_material_codes": ["候选服饰 code，只用于展示和审计"]', '"selected_material_codes": ["候选服饰 code"]')
    migrated = migrated.replace('"selected_materials": [{"material_id": 123, "material_code": "候选服饰 code",', '"selected_materials": [{"material_code": "候选服饰 code",')
    migrated = migrated.replace('"material_code": "必须原样复制标签卡 code"', '"material_code": "必须原样复制标签卡 code"')
    return migrated


def shoot_script_prompt_template_v2() -> str:
    return """你是 Lily 品牌 AI 摄影棚的 AI Director。你的任务是基于已完成的 AI 选搭结果，一次性生成完整拍摄剧本 ShootScript。

职责边界：
1. 你负责导演判断：全局拍摄设定、镜头景别、取景范围、模特姿势、身姿走位、构图、背景光线、不可遮挡区域。
2. 你不能替换 AI 选搭已选择的库内搭配服饰；所有搭配服饰都用 code 表达，不要输出系统内部 ID。
3. 鞋、帽、包、首饰等生成类配饰不是商品事实源；你可以调整、弱化或取消，但必须在 accessory_adjustments 中披露调整原因。
4. 你必须输出 resolved_accessories 作为最终配饰方案；它是分镜预览和成片执行配饰的唯一准则，并覆盖 StylingPlan.generated_accessories。
5. 每个 shots[] 必须输出 accessory_instructions，说明该镜头是否出现鞋、包、首饰、帽子，以及如何保持低存在感。
6. 前 5 个镜头要形成完整商品说明，第 6 个镜头只作为备用姿态，不进入最终成片。
7. 分镜预览和最终成片都将读取此 ShootScript。不要输出长 prompt，只输出结构化 JSON。

{{segment.brand_info}}

【AI 选搭结果】
{{runtime.styling_result}}

请只返回 JSON，结构如下：
{
  "global_directing": {
    "brand_tone": "string",
    "background": "string",
    "lighting": "string",
    "model_direction": "string",
    "hair_makeup_boundary": "string",
    "accessory_boundary": "string",
    "consistency_rules": ["string"],
    "product_fidelity_rules": ["string"],
    "negative_constraints": ["string"]
  },
  "styling_risks": [{"risk_id": "string", "severity": "low | medium | high | blocking", "material_code": "string", "risk": "string", "affected_shots": [1], "director_strategy": "string", "user_visible_recommendation": "string"}],
  "accessory_adjustments": [{"item_type": "bag | footwear | jewelry | hat | other", "original_direction": "string", "adjusted_direction": "string", "reason": "string", "affected_shots": [1]}],
  "resolved_accessories": [{"item_type": "footwear | bag | jewelry | hat | other", "final_direction": "最终进入分镜和成片的配饰描述；取消则写取消", "source": "styling_plan | director_adjusted | director_added | removed", "visibility": "none | low | medium", "applies_to_shots": [1], "reason": "为什么这样执行"}],
  "shots": [{"shot_index": 1, "shot_role": "hero_full_body", "shot_role_label": "全身主视觉", "framing": "string", "crop_range": "string", "model_scale": "string", "camera_angle": "string", "body_position": "string", "pose": "string", "movement": "string", "hand_position": "string", "main_focus": "string", "visible_garments": ["hero", "搭配服饰 code"], "hero_selling_points": ["string"], "forbidden_occlusion_zones": ["string"], "composition_notes": "string", "accessory_instructions": ["本镜头配饰执行说明"], "acceptance_criteria": ["string"], "negative_constraints": ["string"]}],
  "user_visible_summary": {"title": "string", "summary": "string", "key_points": ["string"], "risk_summary": "string"},
  "warnings": []
}
"""


def storyboard_preview_prompt_template_v2() -> str:
    return """生成一张 3x2 电商棚拍分镜预览图。

{{segment.brand_info}}

主推款和搭配服饰参考图是真实商品事实来源，必须保持商品级保真。
搭配服饰和生成配饰必须服务主推款展示，不得让主推款沦为整套造型的背景元素。
数字模特参考图用于保持同一身份、发型和气质。

【拍摄剧本结果】
{{runtime.shoot_script_result}}

按拍摄剧本中的 6 个 shots 生成 3x2 分镜预览。第 6 格仅参考，最终不会生成大图。
配饰执行优先级：以 resolved_accessories 和 shots[].accessory_instructions 为准。
不要生成文字、水印、logo、编号或边框说明。
"""


def final_image_prompt_template_v2() -> str:
    return """生成一张电商棚拍成片，只生成单张图，不要生成拼图或分镜预览。

参考分镜预览图中 current_shot_context.preview_grid_cell 对应的格子，只参考姿势、身位、景别、构图、背景和光影方向。
分镜预览不是商品事实来源。主推款参考图是主角商品事实来源，搭配服饰参考图是配角商品事实来源。

{{segment.brand_info}}

【当前单张成片上下文】
{{runtime.current_shot_context}}

必须保持主推款和搭配服饰的品类、颜色、面料、版型、结构、比例和可见细节。
鞋、包、饰品只作为辅助搭配，不作为商品事实源，不做特写。
不要让搭配服饰或生成配饰抢主推款视觉中心；不要改款、换色、替换商品；不要添加文字、水印、logo。
"""


def repair_prompt_config_placeholders() -> None:
    return


def get_prompt_config_rows() -> dict[str, Any]:
    repair_prompt_config_placeholders()
    try:
        with get_db() as db:
            rows = db.execute("SELECT * FROM prompt_configs").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row["key"]): row for row in rows}


def serialize_prompt_config_row(row: Any, rows: dict[str, Any]) -> dict[str, Any]:
    rendered, diagnostics = render_prompt_config(str(row["key"]), sample_prompt_runtime(), rows=rows)
    return {
        "key": str(row["key"]),
        "groupType": str(row["group_type"] or "assembly"),
        "title": str(row["title"] or row["key"]),
        "description": str(row["description"] or ""),
        "content": str(row["content"] or ""),
        "sortOrder": int(row["sort_order"] or 0),
        "updatedAt": row["updated_at"],
        "usedSegments": diagnostics.get("usedSegments", []),
        "missingPlaceholders": diagnostics.get("missingPlaceholders", []),
        "referencedPlaceholders": diagnostics.get("referencedPlaceholders", []),
        "renderedPreview": rendered,
    }


def get_all_prompt_configs() -> list[dict[str, Any]]:
    rows = get_prompt_config_rows()
    items = [serialize_prompt_config_row(row, rows) for row in rows.values()]
    return sorted(items, key=lambda item: (str(item["groupType"]), int(item["sortOrder"])))


def get_prompt_config(key: str) -> dict[str, Any]:
    normalized = normalize_prompt_config_key(key)
    rows = get_prompt_config_rows()
    row = rows.get(normalized)
    if not row:
        raise HTTPException(status_code=404, detail="Prompt 配置不存在")
    return serialize_prompt_config_row(row, rows)


def prompt_config_content(key: str, rows: dict[str, Any] | None = None) -> str | None:
    normalized = normalize_prompt_config_key(key)
    rows = rows if rows is not None else get_prompt_config_rows()
    row = rows.get(normalized)
    if row:
        return str(row["content"] or "")
    return None


def render_prompt_config(key: str, runtime: dict[str, Any] | None = None, rows: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    diagnostics: dict[str, Any] = {
        "usedSegments": [],
        "missingPlaceholders": [],
        "referencedPlaceholders": [],
    }
    rows = rows if rows is not None else get_prompt_config_rows()
    runtime = runtime or {}

    def resolve_placeholder(name: str, stack: list[str]) -> str:
        diagnostics["referencedPlaceholders"].append(name)
        if name.startswith("segment."):
            if name in stack:
                diagnostics["missingPlaceholders"].append(f"{name} (循环引用)")
                return f"[[循环引用:{name}]]"
            if name not in rows or str(rows[name]["group_type"] or "") != "segment":
                diagnostics["missingPlaceholders"].append(name)
                return f"[[缺失分段:{name}]]"
            if name not in diagnostics["usedSegments"]:
                diagnostics["usedSegments"].append(name)
            return render_text(prompt_config_content(name, rows) or "", [*stack, name])
        if name.startswith("runtime."):
            value = runtime.get(name.removeprefix("runtime."))
            if value is None:
                diagnostics["missingPlaceholders"].append(name)
                return f"[[缺失运行时:{name}]]"
            if isinstance(value, str):
                return value
            return dumps(value)
        diagnostics["missingPlaceholders"].append(name)
        return f"[[未知占位符:{name}]]"

    def render_text(text: str, stack: list[str]) -> str:
        return PROMPT_PLACEHOLDER_PATTERN.sub(lambda match: resolve_placeholder(match.group(1), stack), text)

    source = prompt_config_content(key, rows)
    if source is None:
        diagnostics["missingPlaceholders"].append(key)
        source = ""
    rendered = render_text(source, [key]).strip() + "\n"
    for field in ("usedSegments", "missingPlaceholders", "referencedPlaceholders"):
        diagnostics[field] = sorted(set(diagnostics[field]))
    return rendered, diagnostics


def render_prompt_template(key: str, runtime: dict[str, Any] | None = None) -> str:
    rendered, diagnostics = render_prompt_config(key, runtime)
    if diagnostics.get("missingPlaceholders"):
        raise RuntimeError(f"Prompt 配置缺失或占位符未填充：{', '.join(diagnostics['missingPlaceholders'])}")
    return rendered


def sample_prompt_runtime() -> dict[str, Any]:
    return {
        "candidate_retrieval": dumps([{"code": "LOOK-A", "category": "上装", "tag_facts": {"effective_tags": {"colors": ["白色"]}}}]),
        "styling_result": dumps({"hero_understanding": {"category": "裤装"}, "selected_material_codes": ["LOOK-A"], "reason_summary": "轻量上装服务主推裤装。"}),
        "shoot_script_result": dumps({"global_directing": {"background": "浅灰棚拍"}, "shots": [{"shot_index": 1, "shot_role": "hero_full_body", "main_focus": "主推款整体比例"}]}),
        "current_shot_context": dumps({"preview_grid_cell": 1, "current_shot": {"shot_role": "hero_full_body", "main_focus": "主推款整体比例"}, "styling": {"selected_material_codes": ["LOOK-A"]}}),
    }


def available_prompt_placeholders() -> list[dict[str, str]]:
    return [
        {"name": "segment.brand_info", "scope": "segment", "description": "品牌信息分段 Prompt"},
        {"name": "runtime.candidate_retrieval", "scope": "runtime", "description": "搭配服饰库中可用服饰的 code 与全量标签事实"},
        {"name": "runtime.styling_result", "scope": "runtime", "description": "AI 选搭结果，使用搭配服饰 code 标识"},
        {"name": "runtime.shoot_script_result", "scope": "runtime", "description": "拍摄剧本结果，用于分镜预览"},
        {"name": "runtime.current_shot_context", "scope": "runtime", "description": "当前成片单镜头所需的选搭、剧本和分镜信息"},
    ]


def job_lease_until() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=settings.generation_image_timeout_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def preview_job_key(preview_id: int) -> str:
    return f"generation_preview:{preview_id}"


def final_set_job_key(result_set_id: int) -> str:
    return f"generation_result_set:{result_set_id}"


def lease_generation_preview(preview_id: int, attempt_id: int) -> None:
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_previews
            SET status = 'RUNNING', active_attempt_id = ?, locked_by = ?, locked_until = ?,
                run_attempts = COALESCE(run_attempts, 0) + 1, updated_at = ?
            WHERE id = ? AND status IN ('QUEUED', 'RUNNING')
            """,
            (attempt_id, f"worker-{uuid4()}", job_lease_until(), timestamp, preview_id),
        )


def lease_generation_result_image(image_id: int, attempt_id: int) -> None:
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_result_images_new
            SET status = 'RUNNING', active_attempt_id = ?, locked_by = ?, locked_until = ?,
                run_attempts = COALESCE(run_attempts, 0) + 1, updated_at = ?
            WHERE id = ? AND status IN ('QUEUED', 'RUNNING')
            """,
            (attempt_id, f"worker-{uuid4()}", job_lease_until(), timestamp, image_id),
        )


def refresh_pending_new_generation_jobs() -> None:
    timestamp = now_iso()
    with get_db() as db:
        preview_rows = db.execute(
            """
            SELECT * FROM generation_previews
            WHERE status IN ('QUEUED', 'RUNNING')
              AND (locked_until IS NULL OR locked_until < ?)
            ORDER BY updated_at, id
            LIMIT 3
            """,
            (timestamp,),
        ).fetchall()
        result_sets = db.execute(
            """
            SELECT DISTINCT grs.id, grs.task_id
            FROM generation_result_sets grs
            JOIN generation_tasks gt ON gt.active_result_set_id = grs.id
            JOIN generation_result_images_new gri ON gri.result_set_id = grs.id
            WHERE grs.status = 'RUNNING'
              AND gri.status IN ('QUEUED', 'RUNNING')
              AND (gri.locked_until IS NULL OR gri.locked_until < ?)
            ORDER BY grs.updated_at, grs.id
            LIMIT 3
            """,
            (timestamp,),
        ).fetchall()
        terminal_result_sets = db.execute(
            """
            SELECT grs.id, grs.task_id
            FROM generation_result_sets grs
            JOIN generation_tasks gt ON gt.active_result_set_id = grs.id
            WHERE (
                grs.status = 'RUNNING'
                AND NOT EXISTS (
                  SELECT 1 FROM generation_result_images_new gri
                  WHERE gri.result_set_id = grs.id
                    AND gri.status IN ('QUEUED', 'RUNNING')
                )
              )
              OR (gt.status = 'FINAL_RUNNING' AND grs.status IN ('SUCCEEDED', 'PARTIAL'))
            ORDER BY grs.updated_at, grs.id
            LIMIT 10
            """
        ).fetchall()

    for row in preview_rows:
        key = preview_job_key(int(row["id"]))
        if key in active_background_generation_jobs:
            continue
        active_attempt_id = int(row["active_attempt_id"]) if row["active_attempt_id"] else None
        if attempt_has_provider_request_id(active_attempt_id):
            attempt_id = int(active_attempt_id)
        else:
            fail_stale_attempt(active_attempt_id, "预览生成任务锁过期，已由后台恢复重试。")
            attempt_id = create_attempt("generation_preview", int(row["id"]), "image-2")
        lease_generation_preview(int(row["id"]), attempt_id)
        shoot_script_id = int(row["shoot_script_id"]) if "shoot_script_id" in row.keys() and row["shoot_script_id"] else 0
        shot_plans = get_task_shot_plans_by_script(shoot_script_id)
        schedule_background_generation(
            run_preview_generation(int(row["task_id"]), int(row["styling_id"]), shoot_script_id, shot_plans, int(row["id"]), attempt_id),
            job_key=key,
        )

    for row in result_sets:
        key = final_set_job_key(int(row["id"]))
        if key in active_background_generation_jobs:
            continue
        with get_db() as db:
            image_rows = db.execute(
                """
                SELECT id, active_attempt_id FROM generation_result_images_new
                WHERE result_set_id = ?
                  AND status IN ('QUEUED', 'RUNNING')
                  AND (locked_until IS NULL OR locked_until < ?)
                ORDER BY sort_order, id
                """,
                (row["id"], timestamp),
            ).fetchall()
        image_ids = [int(image["id"]) for image in image_rows]
        if image_ids:
            resume_attempts_by_image_id: dict[int, int] = {}
            for image in image_rows:
                active_attempt_id = int(image["active_attempt_id"]) if "active_attempt_id" in image.keys() and image["active_attempt_id"] else None
                if attempt_has_provider_request_id(active_attempt_id):
                    resume_attempts_by_image_id[int(image["id"])] = int(active_attempt_id)
                else:
                    fail_stale_attempt(active_attempt_id, "成片生成任务锁过期，已由后台恢复重试。")
            schedule_background_generation(
                run_final_generation(int(row["task_id"]), int(row["id"]), image_ids, resume_attempts_by_image_id=resume_attempts_by_image_id),
                job_key=key,
            )

    for row in terminal_result_sets:
        with get_db() as db:
            reconcile_final_result_set(db, int(row["id"]), int(row["task_id"]))


def schedule_background_generation(coro: Any, job_key: str | None = None) -> None:
    if job_key and job_key in active_background_generation_jobs:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return
    if job_key:
        active_background_generation_jobs.add(job_key)
    task = asyncio.create_task(coro)

    def report_failure(done: asyncio.Task[Any]) -> None:
        try:
            with suppress(asyncio.CancelledError):
                exc = done.exception()
                if exc:
                    logger.exception("Background generation task failed", exc_info=exc)
        finally:
            if job_key:
                active_background_generation_jobs.discard(job_key)

    task.add_done_callback(report_failure)


async def generate_image_asset(task_package: dict[str, Any], output_key: str, attempt_id: int | None = None) -> tuple[int, dict[str, Any]]:
    if not settings.is_image_provider_configured():
        raise RuntimeError(image_provider_missing_message())
    payload = get_attempt_provider_resume_snapshot(attempt_id)
    if payload is None:
        payload = await asyncio.to_thread(run_image_provider_submit, task_package, output_key)
        persist_attempt_provider_progress(attempt_id, payload)

    if payload.get("status") != "SUCCEEDED" or not payload.get("outputPath"):
        deadline = time.time() + settings.generation_image_timeout_seconds
        while time.time() < deadline:
            await asyncio.sleep(3)
            try:
                latest = await asyncio.to_thread(run_image_provider_refresh, payload, output_key)
            except Exception as exc:
                raise ImageProviderError(f"图片生成状态查询失败，providerRequestId={payload.get('providerRequestId')}: {exc}", payload) from exc
            payload = {**payload, **latest}
            persist_attempt_provider_progress(attempt_id, payload)
            if payload.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                break
        if payload.get("status") != "SUCCEEDED":
            raise ImageProviderError(f"图片生成未成功，providerRequestId={payload.get('providerRequestId')}: {payload}", payload)
        payload = {**payload, "submissionMode": "completed"}
        persist_attempt_provider_progress(attempt_id, payload)

    if payload.get("provider") == "placeholder":
        raise RuntimeError("Image-2 provider 返回占位图，拒绝将占位图记为生成成功。")
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


def run_image_provider_generate(task_package: dict[str, Any], output_key: str) -> dict[str, Any]:
    return asyncio.run(get_image_provider().generate(task_package, output_key))


def run_image_provider_submit(task_package: dict[str, Any], output_key: str) -> dict[str, Any]:
    return asyncio.run(get_image_provider().submit(task_package, output_key))


def run_image_provider_refresh(provider_response: dict[str, Any], output_key: str) -> dict[str, Any]:
    return asyncio.run(get_image_provider().refresh(provider_refresh_row(provider_response, output_key)))


def provider_refresh_row(provider_response: dict[str, Any], output_key: str) -> dict[str, Any]:
    provider_urls = provider_response.get("providerUrls") if isinstance(provider_response.get("providerUrls"), dict) else {}
    return {
        "provider_request_id": provider_response.get("providerRequestId"),
        "provider_urls_json": dumps(provider_urls),
        "output_path": str(provider_response.get("outputPath") or output_key),
        "status": provider_response.get("status"),
        "provider_status": provider_response.get("providerStatus"),
    }


def image_provider_missing_message() -> str:
    if settings.image_provider == "toapis":
        return "Image-2 provider 未配置：当前使用 toapis，请设置 TOAPIS_API_KEY，或将 GPT_IMAGE2_PROVIDER=fal 并设置 FAL_KEY。"
    if settings.image_provider == "fal":
        return "Image-2 provider 未配置：当前使用 fal，请设置 FAL_KEY。"
    return f"Image-2 provider 未配置：不支持的 provider {settings.image_provider}。"


async def generate_single_final_image(task_id: int, result_set_id: int, image_id: int, attempt_id: int | None = None) -> int:
    if attempt_id is None:
        attempt_id = create_attempt("generation_result_image", image_id, "image-2")
    lease_generation_result_image(image_id, attempt_id)
    with get_db() as db:
        row = db.execute("SELECT status FROM generation_result_images_new WHERE id = ?", (image_id,)).fetchone()
        if row and row["status"] == "CANCELLED":
            finish_attempt(attempt_id, "CANCELLED")
            return 0
        db.execute(
            """
            UPDATE generation_result_images_new
            SET status = 'RUNNING', active_attempt_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (attempt_id, now_iso(), image_id),
        )
    try:
        task_package = build_new_final_task_package(task_id, image_id)
        if not attempt_has_provider_request_id(attempt_id):
            update_attempt_request_snapshot(attempt_id, task_package)
        with get_db() as db:
            sort_order = db.execute("SELECT sort_order FROM generation_result_images_new WHERE id = ?", (image_id,)).fetchone()["sort_order"]
        output_key = join_key("generation-tasks", str(task_id), "final", f"{result_set_id}-{sort_order}.png")
        asset_id, provider_response = await generate_image_asset(task_package, output_key, attempt_id=attempt_id)
    except Exception as exc:
        provider_response = getattr(exc, "provider_response", None)
        with get_db() as db:
            current = db.execute(
                """
                SELECT gri.status AS image_status, gt.status AS task_status
                FROM generation_result_images_new gri
                JOIN generation_result_sets grs ON grs.id = gri.result_set_id
                JOIN generation_tasks gt ON gt.id = grs.task_id
                WHERE gri.id = ?
                """,
                (image_id,),
            ).fetchone()
            if current and (current["image_status"] == "CANCELLED" or current["task_status"] == "CANCELLED"):
                finish_attempt(attempt_id, "CANCELLED", response_snapshot=provider_response, error_message=str(exc))
                persist_attempt_provider_request_id(attempt_id, provider_response)
                return 0
        finish_attempt(attempt_id, "FAILED", response_snapshot=provider_response, error_message=str(exc))
        persist_attempt_provider_request_id(attempt_id, provider_response)
        with get_db() as db:
            db.execute(
                """
                UPDATE generation_result_images_new
                SET status = 'FAILED', active_attempt_id = ?, locked_by = NULL, locked_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (attempt_id, now_iso(), image_id),
            )
        raise
    with get_db() as db:
        current = db.execute(
            """
            SELECT gri.status AS image_status, gt.status AS task_status
            FROM generation_result_images_new gri
            JOIN generation_result_sets grs ON grs.id = gri.result_set_id
            JOIN generation_tasks gt ON gt.id = grs.task_id
            WHERE gri.id = ?
            """,
            (image_id,),
        ).fetchone()
        if current and (current["image_status"] == "CANCELLED" or current["task_status"] == "CANCELLED"):
            finish_attempt(attempt_id, "CANCELLED", response_snapshot=provider_response, output_asset_id=asset_id)
            persist_attempt_provider_request_id(attempt_id, provider_response)
            return asset_id
    finish_attempt(attempt_id, "SUCCEEDED", response_snapshot=provider_response, output_asset_id=asset_id)
    persist_attempt_provider_request_id(attempt_id, provider_response)
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_result_images_new
            SET status = 'SUCCEEDED', current_asset_id = ?, active_attempt_id = ?,
                locked_by = NULL, locked_until = NULL, updated_at = ?
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
        "activeShootScriptId": row["active_shoot_script_id"] if "active_shoot_script_id" in row.keys() else None,
        "activePreviewId": row["active_preview_id"],
        "activeStoryboardPreviewId": row["active_storyboard_preview_id"] if "active_storyboard_preview_id" in row.keys() else row["active_preview_id"],
        "activeResultSetId": row["active_result_set_id"],
        "archivedAt": row["archived_at"] if "archived_at" in row.keys() else None,
        "archiveReason": row["archive_reason"] if "archive_reason" in row.keys() else None,
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
    item["styling"] = get_generation_styling(row["active_styling_id"], include_debug=include_debug)
    item["shootScript"] = get_generation_shoot_script(row["active_shoot_script_id"] if "active_shoot_script_id" in row.keys() else None, include_debug=include_debug)
    item["shotPlans"] = get_task_shot_plans_by_script(row["active_shoot_script_id"] if "active_shoot_script_id" in row.keys() else None)
    preview_id = row["active_storyboard_preview_id"] if "active_storyboard_preview_id" in row.keys() and row["active_storyboard_preview_id"] else row["active_preview_id"]
    item["preview"] = get_generation_preview(preview_id)
    item["storyboardPreview"] = item["preview"]
    item["resultSet"] = get_generation_result_set(row["active_result_set_id"])
    item["aiOperations"] = get_task_ai_operations(row["id"])
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


def get_generation_styling(styling_id: int | None, include_debug: bool = False) -> dict[str, Any] | None:
    if not styling_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_stylings WHERE id = ?", (styling_id,)).fetchone()
    if not row:
        return None
    styling_plan = loads(row["styling_plan_json"], {}) if "styling_plan_json" in row.keys() else {}
    item = {
        "id": row["id"],
        "taskId": row["task_id"],
        "inputVersionId": row["input_version_id"],
        "sourceMaterialIds": loads(row["source_material_ids_json"], []),
        "candidateMaterialIds": loads(row["candidate_material_ids_json"], []) if "candidate_material_ids_json" in row.keys() else [],
        "selectedMaterialIds": loads(row["selected_material_ids_json"], []),
        "selectedMaterials": loads(row["selected_material_snapshot_json"], []),
        "stylingPlan": styling_plan,
        "heroUnderstanding": loads(row["hero_understanding_json"], {}) if "hero_understanding_json" in row.keys() else styling_plan.get("hero_understanding", {}),
        "libraryAdequacy": loads(row["library_adequacy_snapshot_json"], {}) if "library_adequacy_snapshot_json" in row.keys() else {},
        "outfitCandidates": loads(row["outfit_candidates_json"], []) if "outfit_candidates_json" in row.keys() else styling_plan.get("outfit_candidates", []),
        "criticScores": loads(row["critic_scores_json"], {}) if "critic_scores_json" in row.keys() else get_selected_outfit_scores(styling_plan),
        "rejectedCandidates": loads(row["rejected_candidates_json"], []) if "rejected_candidates_json" in row.keys() else styling_plan.get("rejected_candidates", []),
        "qualitySignals": styling_plan.get("quality_signals", []) if isinstance(styling_plan.get("quality_signals"), list) else [],
        "reasonSummary": row["reason_summary"],
        "warnings": loads(row["warnings_json"], []),
        "status": row["status"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_debug:
        item["candidateRetrievalSnapshot"] = loads(row["candidate_retrieval_snapshot_json"], {}) if "candidate_retrieval_snapshot_json" in row.keys() else {}
        item["libraryAdequacySnapshot"] = item["libraryAdequacy"]
    return item


def get_task_shot_plans(styling_id: int | None) -> list[dict[str, Any]]:
    if not styling_id:
        return []
    with get_db() as db:
        rows = db.execute("SELECT * FROM generation_shot_plans WHERE styling_id = ? ORDER BY sort_order", (styling_id,)).fetchall()
    return [serialize_new_shot_plan(row) for row in rows]


def get_task_shot_plans_by_script(shoot_script_id: int | None) -> list[dict[str, Any]]:
    if not shoot_script_id:
        return []
    with get_db() as db:
        rows = db.execute("SELECT * FROM generation_shot_plans WHERE shoot_script_id = ? ORDER BY sort_order", (shoot_script_id,)).fetchall()
    return [serialize_new_shot_plan(row) for row in rows]


def get_generation_shoot_script(shoot_script_id: int | None, include_debug: bool = False) -> dict[str, Any] | None:
    if not shoot_script_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_shoot_scripts WHERE id = ?", (shoot_script_id,)).fetchone()
    if not row:
        return None
    script = loads(row["script_json"], {})
    item = {
        "id": row["id"],
        "taskId": row["task_id"],
        "inputVersionId": row["input_version_id"],
        "stylingId": row["styling_id"],
        "status": row["status"],
        "script": script,
        "summary": loads(row["summary_json"], {}) if "summary_json" in row.keys() else script.get("user_visible_summary", {}),
        "stylingRisks": loads(row["styling_risks_json"], []) if "styling_risks_json" in row.keys() else script.get("styling_risks", []),
        "accessoryAdjustments": loads(row["accessory_adjustments_json"], []) if "accessory_adjustments_json" in row.keys() else script.get("accessory_adjustments", []),
        "resolvedAccessories": script.get("resolved_accessories", []),
        "activeAttemptId": row["active_attempt_id"],
        "activeAttempt": get_attempt_summary(row["active_attempt_id"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_debug:
        item["scriptJson"] = script
    return item


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
        "shootScriptId": row["shoot_script_id"] if "shoot_script_id" in row.keys() else None,
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
        "shootScriptId": row["shoot_script_id"] if "shoot_script_id" in row.keys() else None,
        "previewId": row["preview_id"] if "preview_id" in row.keys() else None,
        "images": [
            {
                "id": image["id"],
                "sortOrder": image["sort_order"],
                "status": image["status"],
                "asset": get_media_asset_by_id(image["current_asset_id"]),
                "activeAttemptId": image["active_attempt_id"],
                "activeAttempt": get_attempt_summary(image["active_attempt_id"]),
                "createdAt": image["created_at"],
                "updatedAt": image["updated_at"],
            }
            for image in images
        ],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def actual_model_input_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    result: dict[str, Any] = {}
    prompt = snapshot.get("prompt")
    if prompt is not None:
        result["prompt"] = prompt
    if isinstance(snapshot.get("imageInputs"), list):
        result["imageInputs"] = snapshot.get("imageInputs")
    elif isinstance(snapshot.get("images"), list):
        result["images"] = snapshot.get("images")
    else:
        input_version = snapshot.get("input_version") if isinstance(snapshot.get("input_version"), dict) else {}
        image_snapshot = input_version.get("imageSnapshot") or input_version.get("image_snapshot")
        if isinstance(image_snapshot, list):
            result["images"] = image_snapshot
        selected = snapshot.get("selected_material_snapshot")
        if isinstance(selected, list):
            result["selectedMaterialImages"] = [
                {
                    "code": item.get("code"),
                    "images": item.get("images"),
                }
                for item in selected
                if isinstance(item, dict)
            ]
    if not result:
        return snapshot
    return result


def actual_model_output_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    if "raw_json" in snapshot or "raw_text" in snapshot:
        return {
            key: snapshot[key]
            for key in ("raw_text", "raw_json")
            if key in snapshot
        }
    if isinstance(snapshot.get("model_raw_output"), dict):
        return snapshot["model_raw_output"]
    return snapshot


def get_task_attempts(task_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT ga.* FROM generation_attempts ga
            WHERE (ga.owner_type = 'generation_styling' AND ga.owner_id = ?)
               OR (ga.owner_type = 'generation_shoot_script' AND ga.owner_id IN (
                    SELECT id FROM generation_shoot_scripts WHERE task_id = ?
                  ))
               OR (ga.owner_type = 'generation_preview' AND ga.owner_id IN (
                    SELECT id FROM generation_previews WHERE task_id = ?
                  ))
               OR (ga.owner_type = 'generation_result_image' AND ga.owner_id IN (
                    SELECT gri.id FROM generation_result_images_new gri
                    JOIN generation_result_sets grs ON grs.id = gri.result_set_id
                    WHERE grs.task_id = ?
                  ))
            ORDER BY ga.created_at DESC
            """,
            (task_id, task_id, task_id, task_id),
        ).fetchall()
    records = []
    for row in rows:
        request_snapshot = loads(row["request_snapshot_json"], {})
        response_snapshot = loads(row["response_snapshot_json"], {})
        records.append(
            {
                "id": row["id"],
                "ownerType": row["owner_type"],
                "ownerId": row["owner_id"],
                "provider": row["provider"],
                "status": row["status"],
                "errorMessage": row["error_message"],
                "modelInput": actual_model_input_from_snapshot(request_snapshot),
                "modelOutput": actual_model_output_from_snapshot(response_snapshot),
            }
        )
    return records


def get_attempt_summary(attempt_id: int | None) -> dict[str, Any] | None:
    if not attempt_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if not row:
        return None
    return serialize_attempt_summary(row)


def serialize_attempt_summary(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ownerType": row["owner_type"],
        "ownerId": row["owner_id"],
        "provider": row["provider"],
        "providerRequestId": row["provider_request_id"],
        "status": row["status"],
        "startedAt": row["started_at"],
        "completedAt": row["completed_at"],
        "failedAt": row["failed_at"],
        "errorMessage": row["error_message"],
        "outputAssetId": row["output_asset_id"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def get_task_ai_operations(task_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT ga.* FROM generation_attempts ga
            WHERE (ga.owner_type = 'generation_styling' AND ga.owner_id = ?)
               OR (ga.owner_type = 'generation_shoot_script' AND ga.owner_id IN (
                    SELECT id FROM generation_shoot_scripts WHERE task_id = ?
                  ))
               OR (ga.owner_type = 'generation_preview' AND ga.owner_id IN (
                    SELECT id FROM generation_previews WHERE task_id = ?
                  ))
               OR (ga.owner_type = 'generation_result_image' AND ga.owner_id IN (
                    SELECT gri.id FROM generation_result_images_new gri
                    JOIN generation_result_sets grs ON grs.id = gri.result_set_id
                    WHERE grs.task_id = ?
                  ))
            ORDER BY ga.created_at DESC, ga.id DESC
            LIMIT 30
            """,
            (task_id, task_id, task_id, task_id),
        ).fetchall()
    return [serialize_attempt_summary(row) for row in rows]


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


def serialize_brand_info(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "tagline": row["tagline"],
        "brandCore": row["brand_core"],
        "styleDna": row["style_dna"],
        "stylingGuidelines": row["styling_guidelines"],
        "shootingGuidelines": row["shooting_guidelines"],
        "negativeConstraints": row["negative_constraints"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def serialize_tag_dimension(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "labelZh": row["label_zh"],
        "descriptionZh": row["description_zh"],
        "sortOrder": row["sort_order"],
        "isActive": bool(row["is_active"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def serialize_tag_def(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "dimensionCode": row["dimension_code"],
        "code": row["code"],
        "labelZh": row["label_zh"],
        "descriptionZh": row["description_zh"],
        "parentCode": row["parent_code"],
        "level": row["level"],
        "valueKind": row["value_kind"],
        "isSelectable": bool(row["is_selectable"]),
        "isActive": bool(row["is_active"]),
        "sortOrder": row["sort_order"],
        "metadata": loads(row["metadata_json"], {}) if "metadata_json" in row.keys() else {},
        "usageCount": int(row["usage_count"] or 0) if "usage_count" in row.keys() else 0,
        "children": [],
    }


def serialize_tag_relation(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "fromDimensionCode": row["from_dimension_code"],
        "fromTagCode": row["from_tag_code"],
        "relationType": row["relation_type"],
        "toDimensionCode": row["to_dimension_code"],
        "toTagCode": row["to_tag_code"],
        "weight": row["weight"],
        "descriptionZh": row["description_zh"],
        "isActive": bool(row["is_active"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def serialize_tag_applicability_rule(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "categoryDimensionCode": row["category_dimension_code"],
        "categoryTagCode": row["category_tag_code"],
        "targetDimensionCode": row["target_dimension_code"],
        "targetTagCode": row["target_tag_code"],
        "mode": row["mode"],
        "appliesToDescendants": bool(row["applies_to_descendants"]),
        "note": row["note"],
        "isActive": bool(row["is_active"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def build_taxonomy_tree(tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_code = {tag["code"]: {**tag, "children": []} for tag in tags}
    roots: list[dict[str, Any]] = []
    for tag in by_code.values():
        parent_code = tag.get("parentCode")
        parent = by_code.get(str(parent_code)) if parent_code else None
        if parent:
            parent["children"].append(tag)
        else:
            roots.append(tag)
    return roots


def build_active_taxonomy_context() -> dict[str, Any]:
    with get_db() as db:
        dimensions = [serialize_tag_dimension(row) for row in db.execute(
            """
            SELECT * FROM tag_dimensions
            WHERE is_active = 1
            ORDER BY sort_order, id
            """
        ).fetchall()]
        tags = [serialize_tag_def(row) for row in db.execute(
            """
            SELECT * FROM tag_defs
            WHERE is_active = 1
            ORDER BY dimension_code, level, parent_code, sort_order, id
            """
        ).fetchall()]
        relations = [serialize_tag_relation(row) for row in db.execute(
            """
            SELECT * FROM tag_relations
            WHERE is_active = 1
            ORDER BY from_dimension_code, from_tag_code, relation_type, to_dimension_code, to_tag_code, id
            """
        ).fetchall()]
        applicability_rules = [serialize_tag_applicability_rule(row) for row in db.execute(
            """
            SELECT * FROM tag_applicability_rules
            WHERE is_active = 1
            ORDER BY category_dimension_code, category_tag_code, mode, target_dimension_code, target_tag_code, id
            """
        ).fetchall()]

    active_dimension_codes = {dimension["code"] for dimension in dimensions}
    active_tag_keys = {(tag["dimensionCode"], tag["code"]) for tag in tags if tag["dimensionCode"] in active_dimension_codes}
    tags = [tag for tag in tags if (tag["dimensionCode"], tag["code"]) in active_tag_keys]
    relations = [
        relation for relation in relations
        if (
            (relation["fromDimensionCode"], relation["fromTagCode"]) in active_tag_keys
            and (relation["toDimensionCode"], relation["toTagCode"]) in active_tag_keys
        )
    ]
    applicability_rules = [
        rule for rule in applicability_rules
        if (
            (rule["categoryDimensionCode"], rule["categoryTagCode"]) in active_tag_keys
            and (rule["targetDimensionCode"], rule["targetTagCode"]) in active_tag_keys
        )
    ]
    content = dumps({
        "dimensions": dimensions,
        "tags": tags,
        "relations": relations,
        "applicabilityRules": applicability_rules,
    })
    return {
        "dimensions": dimensions,
        "tags": tags,
        "relations": relations,
        "applicabilityRules": applicability_rules,
        "dimensionByCode": {dimension["code"]: dimension for dimension in dimensions},
        "tagByKey": {(tag["dimensionCode"], tag["code"]): tag for tag in tags},
        "contentHash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
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


def normalize_brand_info_payload(payload: BrandInfoUpsertRequest, partial: bool = False) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    if not partial or payload.name is not None:
        normalized["name"] = (payload.name or "").strip()
    if not partial or payload.tagline is not None:
        normalized["tagline"] = (payload.tagline or "").strip()
    if not partial or payload.brandCore is not None:
        normalized["brandCore"] = (payload.brandCore or "").strip()
    if not partial or payload.styleDna is not None:
        normalized["styleDna"] = (payload.styleDna or "").strip()
    if not partial or payload.stylingGuidelines is not None:
        normalized["stylingGuidelines"] = (payload.stylingGuidelines or "").strip()
    if not partial or payload.shootingGuidelines is not None:
        normalized["shootingGuidelines"] = (payload.shootingGuidelines or "").strip()
    if not partial or payload.negativeConstraints is not None:
        normalized["negativeConstraints"] = (payload.negativeConstraints or "").strip()
    return normalized


TAXONOMY_VALUE_KINDS = {"tag", "score", "boolean", "text"}
TAXONOMY_APPLICABILITY_MODES = {"allowed", "recommended", "required", "disallowed"}


def payload_has_field(payload: BaseModel, field: str) -> bool:
    fields = getattr(payload, "model_fields_set", None)
    if fields is None:
        fields = getattr(payload, "__fields_set__", set())
    return field in fields


def sanitize_taxonomy_code(value: str) -> str:
    clean = sanitize_name(value)
    if not clean:
        raise HTTPException(status_code=400, detail="标签 code 不能为空")
    return clean


def normalize_taxonomy_dimension_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    code = sanitize_taxonomy_code(str(payload.get("code") or ""))
    label = str(payload.get("labelZh") or payload.get("label_zh") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="维度名称不能为空")
    return {
        "code": code,
        "label_zh": label[:80],
        "description_zh": str(payload.get("descriptionZh") or payload.get("description_zh") or "").strip()[:240],
        "sort_order": int(payload.get("sortOrder") if payload.get("sortOrder") is not None else payload.get("sort_order") or 0),
        "is_active": 1 if payload.get("isActive", payload.get("is_active", True)) is not False else 0,
    }


def normalize_taxonomy_dimension_payload(payload: TaxonomyDimensionUpdateRequest) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if payload.labelZh is not None:
        label = payload.labelZh.strip()
        if not label:
            raise HTTPException(status_code=400, detail="维度名称不能为空")
        updates["label_zh"] = label[:80]
    if payload.descriptionZh is not None:
        updates["description_zh"] = payload.descriptionZh.strip()[:240]
    if payload.sortOrder is not None:
        updates["sort_order"] = int(payload.sortOrder)
    if payload.isActive is not None:
        updates["is_active"] = 1 if payload.isActive else 0
    return updates


def normalize_taxonomy_tag_create_payload(payload: TaxonomyTagCreateRequest) -> dict[str, Any]:
    dimension_code = sanitize_taxonomy_code(payload.dimensionCode)
    code = sanitize_taxonomy_code(payload.code)
    label = payload.labelZh.strip()
    if not label:
        raise HTTPException(status_code=400, detail="标签名称不能为空")
    value_kind = str(payload.valueKind or "tag").strip()
    if value_kind not in TAXONOMY_VALUE_KINDS:
        raise HTTPException(status_code=400, detail="标签值类型不支持")
    parent_code = sanitize_taxonomy_code(payload.parentCode) if payload.parentCode else None
    return {
        "dimension_code": dimension_code,
        "code": code,
        "label_zh": label[:80],
        "description_zh": (payload.descriptionZh or "").strip()[:240],
        "parent_code": parent_code,
        "level": max(1, min(int(payload.level or (2 if parent_code else 1)), 8)),
        "value_kind": value_kind,
        "is_selectable": 1 if payload.isSelectable is not False else 0,
        "is_active": 1 if payload.isActive is not False else 0,
        "sort_order": int(payload.sortOrder or 0),
        "metadata": payload.metadata if isinstance(payload.metadata, dict) else {},
    }


def normalize_taxonomy_tag_import_payload(payload: dict[str, Any]) -> dict[str, Any]:
    dimension_code = sanitize_taxonomy_code(str(payload.get("dimensionCode") or payload.get("dimension_code") or ""))
    code = sanitize_taxonomy_code(str(payload.get("code") or ""))
    label = str(payload.get("labelZh") or payload.get("label_zh") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="标签名称不能为空")
    value_kind = str(payload.get("valueKind") or payload.get("value_kind") or "tag").strip()
    if value_kind not in TAXONOMY_VALUE_KINDS:
        raise HTTPException(status_code=400, detail="标签值类型不支持")
    parent_value = payload.get("parentCode") if "parentCode" in payload else payload.get("parent_code")
    parent_code = sanitize_taxonomy_code(str(parent_value)) if parent_value else None
    metadata = payload.get("metadata")
    return {
        "dimension_code": dimension_code,
        "code": code,
        "label_zh": label[:80],
        "description_zh": str(payload.get("descriptionZh") or payload.get("description_zh") or "").strip()[:240],
        "parent_code": parent_code,
        "level": max(1, min(int(payload.get("level") or (2 if parent_code else 1)), 8)),
        "value_kind": value_kind,
        "is_selectable": 1 if payload.get("isSelectable", payload.get("is_selectable", True)) is not False else 0,
        "is_active": 1 if payload.get("isActive", payload.get("is_active", True)) is not False else 0,
        "sort_order": int(payload.get("sortOrder") if payload.get("sortOrder") is not None else payload.get("sort_order") or 0),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def normalize_taxonomy_tag_update_payload(payload: TaxonomyTagUpdateRequest) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if payload.labelZh is not None:
        label = payload.labelZh.strip()
        if not label:
            raise HTTPException(status_code=400, detail="标签名称不能为空")
        updates["label_zh"] = label[:80]
    if payload.descriptionZh is not None:
        updates["description_zh"] = payload.descriptionZh.strip()[:240]
    if payload_has_field(payload, "parentCode"):
        updates["parent_code"] = sanitize_taxonomy_code(payload.parentCode) if payload.parentCode else None
    if payload.level is not None:
        updates["level"] = max(1, min(int(payload.level), 8))
    if payload.valueKind is not None:
        value_kind = payload.valueKind.strip()
        if value_kind not in TAXONOMY_VALUE_KINDS:
            raise HTTPException(status_code=400, detail="标签值类型不支持")
        updates["value_kind"] = value_kind
    if payload.isSelectable is not None:
        updates["is_selectable"] = 1 if payload.isSelectable else 0
    if payload.isActive is not None:
        updates["is_active"] = 1 if payload.isActive else 0
    if payload.sortOrder is not None:
        updates["sort_order"] = int(payload.sortOrder)
    if payload.metadata is not None:
        updates["metadata_json"] = dumps(payload.metadata if isinstance(payload.metadata, dict) else {})
    return updates


def normalize_taxonomy_relation_type(value: str) -> str:
    clean = sanitize_name(value)
    if not clean:
        raise HTTPException(status_code=400, detail="关系类型不能为空")
    return clean[:64]


def normalize_taxonomy_relation_create_payload(payload: TaxonomyRelationCreateRequest) -> dict[str, Any]:
    return {
        "from_dimension_code": sanitize_taxonomy_code(payload.fromDimensionCode),
        "from_tag_code": sanitize_taxonomy_code(payload.fromTagCode),
        "relation_type": normalize_taxonomy_relation_type(payload.relationType),
        "to_dimension_code": sanitize_taxonomy_code(payload.toDimensionCode),
        "to_tag_code": sanitize_taxonomy_code(payload.toTagCode),
        "weight": max(-10.0, min(float(payload.weight if payload.weight is not None else 1.0), 10.0)),
        "description_zh": (payload.descriptionZh or "").strip()[:240],
        "is_active": 1 if payload.isActive is not False else 0,
    }


def normalize_taxonomy_relation_import_payload(payload: dict[str, Any]) -> dict[str, Any]:
    weight = payload.get("weight")
    return {
        "from_dimension_code": sanitize_taxonomy_code(str(payload.get("fromDimensionCode") or payload.get("from_dimension_code") or "")),
        "from_tag_code": sanitize_taxonomy_code(str(payload.get("fromTagCode") or payload.get("from_tag_code") or "")),
        "relation_type": normalize_taxonomy_relation_type(str(payload.get("relationType") or payload.get("relation_type") or "")),
        "to_dimension_code": sanitize_taxonomy_code(str(payload.get("toDimensionCode") or payload.get("to_dimension_code") or "")),
        "to_tag_code": sanitize_taxonomy_code(str(payload.get("toTagCode") or payload.get("to_tag_code") or "")),
        "weight": max(-10.0, min(float(weight if weight is not None else 1.0), 10.0)),
        "description_zh": str(payload.get("descriptionZh") or payload.get("description_zh") or "").strip()[:240],
        "is_active": 1 if payload.get("isActive", payload.get("is_active", True)) is not False else 0,
    }


def normalize_taxonomy_relation_update_payload(payload: TaxonomyRelationUpdateRequest) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if payload.fromDimensionCode is not None:
        updates["from_dimension_code"] = sanitize_taxonomy_code(payload.fromDimensionCode)
    if payload.fromTagCode is not None:
        updates["from_tag_code"] = sanitize_taxonomy_code(payload.fromTagCode)
    if payload.relationType is not None:
        updates["relation_type"] = normalize_taxonomy_relation_type(payload.relationType)
    if payload.toDimensionCode is not None:
        updates["to_dimension_code"] = sanitize_taxonomy_code(payload.toDimensionCode)
    if payload.toTagCode is not None:
        updates["to_tag_code"] = sanitize_taxonomy_code(payload.toTagCode)
    if payload.weight is not None:
        updates["weight"] = max(-10.0, min(float(payload.weight), 10.0))
    if payload.descriptionZh is not None:
        updates["description_zh"] = payload.descriptionZh.strip()[:240]
    if payload.isActive is not None:
        updates["is_active"] = 1 if payload.isActive else 0
    return updates


def normalize_taxonomy_applicability_mode(value: str | None) -> str:
    mode = str(value or "allowed").strip()
    if mode not in TAXONOMY_APPLICABILITY_MODES:
        raise HTTPException(status_code=400, detail="适用规则模式不支持")
    return mode


def normalize_taxonomy_applicability_create_payload(payload: TaxonomyApplicabilityCreateRequest) -> dict[str, Any]:
    return {
        "category_dimension_code": sanitize_taxonomy_code(payload.categoryDimensionCode or "category"),
        "category_tag_code": sanitize_taxonomy_code(payload.categoryTagCode),
        "target_dimension_code": sanitize_taxonomy_code(payload.targetDimensionCode),
        "target_tag_code": sanitize_taxonomy_code(payload.targetTagCode),
        "mode": normalize_taxonomy_applicability_mode(payload.mode),
        "applies_to_descendants": 1 if payload.appliesToDescendants is not False else 0,
        "note": (payload.note or "").strip()[:240],
        "is_active": 1 if payload.isActive is not False else 0,
    }


def normalize_taxonomy_applicability_import_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "category_dimension_code": sanitize_taxonomy_code(str(payload.get("categoryDimensionCode") or payload.get("category_dimension_code") or "category")),
        "category_tag_code": sanitize_taxonomy_code(str(payload.get("categoryTagCode") or payload.get("category_tag_code") or "")),
        "target_dimension_code": sanitize_taxonomy_code(str(payload.get("targetDimensionCode") or payload.get("target_dimension_code") or "")),
        "target_tag_code": sanitize_taxonomy_code(str(payload.get("targetTagCode") or payload.get("target_tag_code") or "")),
        "mode": normalize_taxonomy_applicability_mode(str(payload.get("mode") or "allowed")),
        "applies_to_descendants": 1 if payload.get("appliesToDescendants", payload.get("applies_to_descendants", True)) is not False else 0,
        "note": str(payload.get("note") or "").strip()[:240],
        "is_active": 1 if payload.get("isActive", payload.get("is_active", True)) is not False else 0,
    }


def normalize_taxonomy_applicability_update_payload(payload: TaxonomyApplicabilityUpdateRequest) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if payload.categoryDimensionCode is not None:
        updates["category_dimension_code"] = sanitize_taxonomy_code(payload.categoryDimensionCode)
    if payload.categoryTagCode is not None:
        updates["category_tag_code"] = sanitize_taxonomy_code(payload.categoryTagCode)
    if payload.targetDimensionCode is not None:
        updates["target_dimension_code"] = sanitize_taxonomy_code(payload.targetDimensionCode)
    if payload.targetTagCode is not None:
        updates["target_tag_code"] = sanitize_taxonomy_code(payload.targetTagCode)
    if payload.mode is not None:
        updates["mode"] = normalize_taxonomy_applicability_mode(payload.mode)
    if payload.appliesToDescendants is not None:
        updates["applies_to_descendants"] = 1 if payload.appliesToDescendants else 0
    if payload.note is not None:
        updates["note"] = payload.note.strip()[:240]
    if payload.isActive is not None:
        updates["is_active"] = 1 if payload.isActive else 0
    return updates


def ensure_taxonomy_dimension_exists(db: Any, dimension_code: str) -> None:
    if not db.execute("SELECT id FROM tag_dimensions WHERE code = ?", (dimension_code,)).fetchone():
        raise HTTPException(status_code=404, detail="标签维度不存在")


def validate_taxonomy_parent(db: Any, dimension_code: str, parent_code: str | None, current_code: str | None = None) -> None:
    if not parent_code:
        return
    if current_code and parent_code == current_code:
        raise HTTPException(status_code=400, detail="父级标签不能是自己")
    parent = db.execute(
        "SELECT id FROM tag_defs WHERE dimension_code = ? AND code = ?",
        (dimension_code, parent_code),
    ).fetchone()
    if not parent:
        raise HTTPException(status_code=400, detail="父级标签不存在")
    cursor = parent_code
    seen = {current_code} if current_code else set()
    while cursor:
        if cursor in seen:
            raise HTTPException(status_code=400, detail="父级标签不能形成循环")
        seen.add(cursor)
        next_row = db.execute(
            "SELECT parent_code FROM tag_defs WHERE dimension_code = ? AND code = ?",
            (dimension_code, cursor),
        ).fetchone()
        cursor = next_row["parent_code"] if next_row else None


def validate_taxonomy_relation_tags(db: Any, relation: dict[str, Any]) -> None:
    for dimension_key, tag_key in (("from_dimension_code", "from_tag_code"), ("to_dimension_code", "to_tag_code")):
        row = db.execute(
            "SELECT id FROM tag_defs WHERE dimension_code = ? AND code = ?",
            (relation[dimension_key], relation[tag_key]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="标签关系引用的标签不存在")


def validate_taxonomy_applicability_tags(db: Any, rule: dict[str, Any]) -> None:
    for dimension_key, tag_key in (("category_dimension_code", "category_tag_code"), ("target_dimension_code", "target_tag_code")):
        row = db.execute(
            "SELECT id FROM tag_defs WHERE dimension_code = ? AND code = ?",
            (rule[dimension_key], rule[tag_key]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="适用规则引用的标签不存在")


def upsert_taxonomy_dimension(db: Any, dimension: dict[str, Any], timestamp: str) -> None:
    db.execute(
        """
        INSERT INTO tag_dimensions (code, label_zh, description_zh, sort_order, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
          label_zh = excluded.label_zh,
          description_zh = excluded.description_zh,
          sort_order = excluded.sort_order,
          is_active = excluded.is_active,
          updated_at = excluded.updated_at
        """,
        (
            dimension["code"],
            dimension["label_zh"],
            dimension["description_zh"],
            dimension["sort_order"],
            dimension["is_active"],
            timestamp,
            timestamp,
        ),
    )


def upsert_taxonomy_tag(db: Any, tag: dict[str, Any], timestamp: str) -> None:
    db.execute(
        """
        INSERT INTO tag_defs
          (dimension_code, code, label_zh, description_zh, parent_code, level, value_kind,
           is_selectable, is_active, sort_order, metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dimension_code, code) DO UPDATE SET
          label_zh = excluded.label_zh,
          description_zh = excluded.description_zh,
          parent_code = excluded.parent_code,
          level = excluded.level,
          value_kind = excluded.value_kind,
          is_selectable = excluded.is_selectable,
          is_active = excluded.is_active,
          sort_order = excluded.sort_order,
          metadata_json = excluded.metadata_json,
          updated_at = excluded.updated_at
        """,
        (
            tag["dimension_code"],
            tag["code"],
            tag["label_zh"],
            tag["description_zh"],
            tag.get("parent_code"),
            tag["level"],
            tag["value_kind"],
            tag["is_selectable"],
            tag["is_active"],
            tag["sort_order"],
            dumps(tag["metadata"]),
            timestamp,
            timestamp,
        ),
    )


def update_taxonomy_tag_parent(db: Any, tag: dict[str, Any], timestamp: str) -> None:
    db.execute(
        """
        UPDATE tag_defs
        SET parent_code = ?, level = ?, updated_at = ?
        WHERE dimension_code = ? AND code = ?
        """,
        (tag.get("parent_code"), tag["level"], timestamp, tag["dimension_code"], tag["code"]),
    )


def insert_taxonomy_relation(db: Any, relation: dict[str, Any], timestamp: str) -> Any:
    return db.execute(
        """
        INSERT INTO tag_relations
          (from_dimension_code, from_tag_code, relation_type, to_dimension_code, to_tag_code,
           weight, description_zh, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            relation["from_dimension_code"],
            relation["from_tag_code"],
            relation["relation_type"],
            relation["to_dimension_code"],
            relation["to_tag_code"],
            relation["weight"],
            relation["description_zh"],
            relation["is_active"],
            timestamp,
            timestamp,
        ),
    )


def upsert_taxonomy_relation(db: Any, relation: dict[str, Any], timestamp: str) -> None:
    db.execute(
        """
        INSERT INTO tag_relations
          (from_dimension_code, from_tag_code, relation_type, to_dimension_code, to_tag_code,
           weight, description_zh, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(from_dimension_code, from_tag_code, relation_type, to_dimension_code, to_tag_code) DO UPDATE SET
          weight = excluded.weight,
          description_zh = excluded.description_zh,
          is_active = excluded.is_active,
          updated_at = excluded.updated_at
        """,
        (
            relation["from_dimension_code"],
            relation["from_tag_code"],
            relation["relation_type"],
            relation["to_dimension_code"],
            relation["to_tag_code"],
            relation["weight"],
            relation["description_zh"],
            relation["is_active"],
            timestamp,
            timestamp,
        ),
    )


def insert_taxonomy_applicability_rule(db: Any, rule: dict[str, Any], timestamp: str) -> Any:
    return db.execute(
        """
        INSERT INTO tag_applicability_rules
          (category_dimension_code, category_tag_code, target_dimension_code, target_tag_code,
           mode, applies_to_descendants, note, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rule["category_dimension_code"],
            rule["category_tag_code"],
            rule["target_dimension_code"],
            rule["target_tag_code"],
            rule["mode"],
            rule["applies_to_descendants"],
            rule["note"],
            rule["is_active"],
            timestamp,
            timestamp,
        ),
    )


def upsert_taxonomy_applicability_rule(db: Any, rule: dict[str, Any], timestamp: str) -> None:
    db.execute(
        """
        INSERT INTO tag_applicability_rules
          (category_dimension_code, category_tag_code, target_dimension_code, target_tag_code,
           mode, applies_to_descendants, note, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(category_dimension_code, category_tag_code, target_dimension_code, target_tag_code) DO UPDATE SET
          mode = excluded.mode,
          applies_to_descendants = excluded.applies_to_descendants,
          note = excluded.note,
          is_active = excluded.is_active,
          updated_at = excluded.updated_at
        """,
        (
            rule["category_dimension_code"],
            rule["category_tag_code"],
            rule["target_dimension_code"],
            rule["target_tag_code"],
            rule["mode"],
            rule["applies_to_descendants"],
            rule["note"],
            rule["is_active"],
            timestamp,
            timestamp,
        ),
    )


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
