from __future__ import annotations

import asyncio
import ast
import hashlib
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import secrets
import sqlite3
import time
import zipfile
from uuid import uuid4
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from .config import get_settings
from .db import dumps, get_db, init_db, loads, log_event, now_iso
from .prompt_render import (
    render_image_reference_spans,
    render_user_instruction_line,
    render_user_requirement_line,
)
from .gemini_provider import (
    GeminiProviderError,
    effective_gemini_model,
    generate_gemini_json_text,
    gemini_channel,
    gemini_not_configured_message,
    gemini_request_meta,
    is_gemini_configured,
)
from .image_derivatives import (
    DERIVATIVE_CONTENT_TYPE,
    derivative_key,
    existing_derivative_or_original,
    generate_image_derivatives,
)
from .image_ingestion import (
    ImageIngestionError,
    declared_upload_content_type,
    inspect_image,
    normalize_image,
    normalized_image_candidates,
    normalized_image_key,
)
from .generation_progress import (
    ERROR_BUSINESS_REJECT,
    ERROR_DELIVERY_FAILED,
    ERROR_INSUFFICIENT_BALANCE,
    ERROR_MALFORMED_OUTPUT,
    ERROR_POLL_TIMEOUT,
    ERROR_PROVIDER_FAILED,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_SUBMISSION_UNKNOWN,
    ERROR_SYSTEM_ERROR,
    ERROR_TASK_NOT_FOUND,
    PHASE_DELIVER,
    PHASE_GENERATE,
    PHASE_PREPARE,
    PHASE_QUEUE,
    PHASE_STORE,
    PHASE_SUBMIT,
    PHASE_UPLOAD,
    AttemptProgressRecorder,
    GenerationPhaseError,
    classify_attempt_error,
    classify_generation_attempt_error,
    classify_provider_error,
    is_balance_error,
    is_generation_retryable_error,
    is_retryable_error,
    is_status_check_retryable_error,
    normalize_toapis_error,
    provider_error_presentation,
)
from .image_provider import ImageProviderError, get_image_provider
from .object_storage import (
    clean_key,
    delete_object,
    delete_prefix,
    ensure_bucket,
    ensure_direct_upload_cors,
    get_bytes,
    join_key,
    object_head,
    object_exists,
    presigned_get_url,
    presigned_put_url,
    put_bytes,
    put_fileobj,
    unique_key,
)


settings = get_settings()
logger = logging.getLogger(__name__)
Image.MAX_IMAGE_PIXELS = settings.image_decode_safety_pixels
SESSION_ROLES = {"user", "admin"}
SHOOT_CONFIG_EXCLUDED_CATEGORY_CODES = {"未知", "不支持入库配饰", "unknown", "unsupported_accessory"}
PROMPT_PLACEHOLDER_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_.-]+)\s*}}")
# 条件段：{{#runtime.x}}...{{/runtime.x}}（占位真值才保留）/ {{^runtime.x}}...{{/runtime.x}}（为空才保留）。
# 让模板在"有内容才显示表头/话术"的前提下完全托管固定文本，代码只提供可变占位值。
PROMPT_SECTION_PATTERN = re.compile(
    r"{{\s*([#^])\s*((?:segment|runtime)\.[a-zA-Z0-9_.-]+)\s*}}(.*?){{\s*/\s*\2\s*}}",
    re.DOTALL,
)
LEGACY_EMPTY_RUNTIME_PLACEHOLDERS = {"styling_outfit", "resolved_accessories"}
AI_STYLING_PROMPT_CONFIG_KEY = "assembly.ai_styling"
GARMENT_IMAGE_SELECTION_PROMPT_CONFIG_KEY = "assembly.garment_image_selection"
HERO_IMAGE_SELECTION_PROMPT_CONFIG_KEY = "assembly.hero_image_selection"
MAX_GARMENT_IMAGE_INPUTS = 15
MAX_HERO_IMAGE_INPUTS = 15
MAX_GARMENT_SELECTED_IMAGES = 6
MAX_HERO_SELECTED_IMAGES = 8
STORYBOARD_PROMPT_CONFIG_KEYS = {
    "top": "assembly.storyboard_preview_top",
    "bottom": "assembly.storyboard_preview_bottom",
    "one_piece": "assembly.storyboard_preview_one_piece",
}
FINAL_IMAGE_PROMPT_CONFIG_KEYS = {
    "top": "assembly.final_image_top",
    "bottom": "assembly.final_image_bottom",
    "one_piece": "assembly.final_image_one_piece",
}
AUTO_RUN_TARGET_FINAL = "FINAL"
MEDIA_CACHE_VARIANT_BASE_SECONDS = {
    "thumb": settings.media_thumb_cache_seconds,
    "preview": settings.media_preview_cache_seconds,
    "original": settings.media_original_cache_seconds,
}
MAX_USER_REQUIREMENT_CHARS = 800
MAX_USER_INPUT_CHARS = 800


# 所有 Prompt 正文均由「Prompt 维护」页维护，代码不写死。系统指令、JSON 输出纪律、
# 重问约束、参考图事实源约束等已分别合并进对应的组装 Prompt 正文：
#   - AI 选搭：正文开头含角色系统指令与 JSON 输出纪律，结尾含 JSON 重问约束，
#     整段作为 user prompt 下发（不再单独走 Gemini system_instruction）。
#   - 分镜预览 / 三类生成成片：正文内用按职能拆分的参考图图号占位承接参考图顺序，
#     并在正文写明参考图身份、商品、分镜单格和环境事实源约束。


def is_request_authenticated(request: Request) -> bool:
    return get_request_role(request) is not None


def require_admin(request: Request) -> None:
    if get_request_role(request) != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")


def require_admin_debug(request: Request, include_debug: bool) -> bool:
    if include_debug:
        require_admin(request)
    return include_debug


def admin_debug_for_request(request: Request) -> bool:
    return get_request_role(request) == "admin"


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


class LoginRequest(BaseModel):
    username: str | None = None
    password: str


class StylingGarmentUpdateRequest(BaseModel):
    code: str | None = None
    manualTags: dict[str, Any] | None = None


class StylingGarmentBatchTagRequest(BaseModel):
    ids: list[int]


class StylingGarmentBatchManualTagsRequest(BaseModel):
    ids: list[int]
    manualTags: dict[str, Any]


class StylingGarmentDeleteRequest(BaseModel):
    ids: list[int] | None = None
    purge: bool = False
    confirmPhrase: str | None = None


class StylingGarmentImageUpdateRequest(BaseModel):
    sortOrder: int | None = None


class GenerationTaskUpdateRequest(BaseModel):
    name: str | None = None
    aiStylingUserInstruction: str | None = None
    storyboardPreviewUserInstruction: str | None = None
    finalImageUserInstruction: str | None = None


class DirectUploadFileRequest(BaseModel):
    filename: str
    mimeType: str | None = None
    sizeBytes: int | None = None


class DirectUploadRequest(BaseModel):
    scope: str = "generation_task"
    files: list[DirectUploadFileRequest] = Field(default_factory=list)


class UploadedMediaRequest(BaseModel):
    objectKey: str
    filename: str | None = None
    mimeType: str | None = None
    sizeBytes: int | None = None


class GenerationTaskFromUploadsRequest(BaseModel):
    name: str | None = None
    batchId: str | None = None
    uploads: list[UploadedMediaRequest] = Field(default_factory=list)


class GenerationStylingRequest(BaseModel):
    userInstruction: str | None = None
    userRequirement: str | None = None


class GenerationStageInstructionRequest(BaseModel):
    userInstruction: str | None = None


class AutoRunFinalRequest(BaseModel):
    # restart=True：已完成任务「重新一键成片」，把当前方案整条置为历史后从头重跑。
    restart: bool = False


class GenerationTaskImageUpdateRequest(BaseModel):
    sortOrder: int | None = None


class GenerationTaskDeleteRequest(BaseModel):
    confirmName: str | None = None
    confirmPhrase: str | None = None
    ids: list[int] | None = None
    purge: bool = False


class DeleteImpactRequest(BaseModel):
    ids: list[int]


class ModelProfileUpsertRequest(BaseModel):
    modelId: str | None = None
    name: str | None = None
    isDefault: bool | None = None
    profile: dict[str, Any] | None = None



class PromptConfigUpdateRequest(BaseModel):
    content: str


class PromptConfigPreviewRequest(BaseModel):
    content: str
    attemptId: int | None = None


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
RUNNING_GENERATION_TASK_STATUSES = {"STYLING_RUNNING", "PREVIEW_RUNNING", "FINAL_RUNNING"}
PLANNING_STATUSES = {"PLANNING"}
REVIEW_STATUSES = {"candidate", "approved", "needs_regen", "needs_retouch", "delivered"}
PROMPT_TAGGING_RESULT_VERSION = "prompt-configured-json-v1"
TAG_REVIEW_CONFIDENCE_THRESHOLD = 0.72
LOGIN_FAILURE_WINDOW_SECONDS = 300
LOGIN_FAILURE_LIMIT = 8
login_failures_by_client: dict[str, list[float]] = {}
active_background_generation_jobs: set[str] = set()

@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    ensure_bucket()
    ensure_direct_upload_cors()
    init_db()
    app.state.generation_poller = asyncio.create_task(generation_poller_loop())
    try:
        yield
    finally:
        task = getattr(app.state, "generation_poller", None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


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
    public_paths = {"/api/health", "/api/version", "/api/auth/login", "/api/auth/logout", "/api/auth/session"}
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
        "geminiConfigured": is_gemini_configured(),
        "geminiProvider": gemini_channel(),
        "imageProviderConfigured": settings.is_image_provider_configured(),
    }


# 部署校验入口：确认线上容器实际运行的代码版本，避免"以为修了其实没上线"。
# commit 由镜像构建时的 GIT_COMMIT build-arg 注入；parserFeatures 是代码级能力
# 标记，即使 commit 为空也能判断解析链是否包含关键修复。
@app.get("/api/version")
def get_version() -> dict[str, Any]:
    return {
        "commit": os.getenv("APP_GIT_COMMIT", ""),
        "parserFeatures": [
            "json_strict_false",
            "jsonish_repair",
            "pythonish_fallback",
            "double_brace_unwrap",
            "parse_failure_excerpt",
            "utf8_sse_decode",
        ],
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


@app.post("/api/uploads/presign")
def create_direct_uploads(payload: DirectUploadRequest) -> dict[str, Any]:
    if not payload.files:
        raise HTTPException(status_code=400, detail="至少选择一张图片")
    if len(payload.files) > 20:
        raise HTTPException(status_code=400, detail="单次最多申请 20 张图片上传")
    ensure_direct_upload_cors()
    scope_prefix = direct_upload_scope_prefix(payload.scope)
    batch = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
    items: list[dict[str, Any]] = []
    for index, item in enumerate(payload.files, start=1):
        filename = sanitize_filename(item.filename or f"image-{index}")
        if item.sizeBytes is not None and item.sizeBytes > settings.max_upload_image_bytes:
            max_mb = settings.max_upload_image_bytes / (1024 * 1024)
            raise HTTPException(status_code=400, detail=f"{filename} 超过 {max_mb:.0f}MB 大小限制")
        content_type = direct_upload_content_type(item.mimeType, filename)
        object_key = join_key("direct-uploads", scope_prefix, batch, f"{index:03d}-{filename}")
        upload_url = presigned_put_url(object_key, content_type)
        if not upload_url:
            raise HTTPException(status_code=500, detail="无法创建上传地址")
        items.append(
            {
                "objectKey": object_key,
                "uploadUrl": upload_url,
                "method": "PUT",
                "headers": {"Content-Type": content_type},
                "expiresIn": settings.s3_presign_expires_seconds,
                "filename": filename,
                "mimeType": content_type,
                "maxSizeBytes": settings.max_upload_image_bytes,
            }
        )
    return {"items": items}


async def validate_upload_image(upload: UploadFile) -> tuple[bytes, str]:
    filename = upload.filename or "image"

    content = await upload.read(settings.max_upload_image_bytes + 1)
    await upload.seek(0)
    content_type, _, _ = validate_image_content(content, filename, upload.content_type)
    return content, content_type


def validate_image_content(content: bytes, filename: str, declared_type: str | None = None) -> tuple[str, int, int]:
    if not content:
        raise HTTPException(status_code=400, detail=f"{filename} 是空文件")
    if len(content) > settings.max_upload_image_bytes:
        max_mb = settings.max_upload_image_bytes / (1024 * 1024)
        raise HTTPException(status_code=400, detail=f"{filename} 超过 {max_mb:.0f}MB 大小限制")

    try:
        inspected = inspect_image(content, filename, settings.max_upload_image_pixels)
    except ImageIngestionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return inspected.content_type, inspected.width, inspected.height


def direct_upload_content_type(mime_type: str | None, filename: str) -> str:
    try:
        return declared_upload_content_type(mime_type, filename)
    except ImageIngestionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def direct_upload_scope_prefix(scope: str) -> str:
    normalized = scope.strip().replace("-", "_")
    if normalized not in {"generation_task", "styling_garment", "model_profile"}:
        raise HTTPException(status_code=400, detail="上传用途不合法")
    return normalized.replace("_", "-")


def ensure_direct_upload_key(object_key: str) -> str:
    key = clean_key(object_key)
    if not key.startswith("direct-uploads/"):
        raise HTTPException(status_code=400, detail="上传对象不属于本次直传路径")
    return key


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
        *normalized_image_candidates(original_key),
    ]


def create_normalized_image_derivatives(original_key: str, content: bytes) -> tuple[str, dict[str, Any]]:
    normalized = normalize_image(content, settings.normalized_image_max_edge)
    normalized_key = normalized_image_key(original_key, normalized.suffix)
    put_bytes(normalized_key, normalized.content, normalized.content_type)
    try:
        return normalized_key, generate_image_derivatives(original_key, normalized.content)
    except Exception:
        cleanup_objects([normalized_key, derivative_key(original_key, "thumb"), derivative_key(original_key, "preview")])
        raise


def gemini_vision_input(object_key: str | None, fallback_mime: str | None = None) -> dict[str, Any] | None:
    """取 Gemini 理解类调用（打标/选搭）的图片输入，优先 preview 派生图。

    原图 base64 后单张可达数 MB,多张叠加会让 ToAPIs 请求体在上行带宽有限的
    部署环境发送超时("The write operation timed out")。文本理解不需要原图
    分辨率,preview(webp,长边 ≤1280)足够且体积小一个数量级。
    派生图缺失且无法生成时回退原图。最终成片生成不走此函数,仍用原图保真。
    """
    if not object_key or not object_exists(object_key):
        return None
    key = existing_derivative_or_original(object_key, "preview")
    if key and key != clean_key(object_key):
        return {"data": get_bytes(key), "mime_type": DERIVATIVE_CONTENT_TYPE}
    return {"data": get_bytes(object_key), "mime_type": fallback_mime or "image/png"}


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
        if len(parts) >= 2 and parts[0] in {"generation-tasks", "styling-garments", "uploads", "direct-uploads", "outputs"}:
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
          (SELECT COUNT(*) FROM generation_previews WHERE environment_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_result_images_new WHERE current_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_attempts WHERE output_asset_id = ?) AS count
        """,
        (asset_id, asset_id, asset_id, asset_id, asset_id, asset_id),
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


def cleanup_created_media_assets(assets: list[dict[str, Any]]) -> None:
    asset_ids = [int(asset["id"]) for asset in assets if asset.get("id")]
    object_keys = [str(asset["objectKey"]) for asset in assets if asset.get("objectKey")]
    if asset_ids:
        with suppress(Exception):
            with get_db() as db:
                db.execute(
                    f"DELETE FROM media_assets WHERE id IN ({','.join('?' for _ in asset_ids)})",
                    asset_ids,
                )
    cleanup_objects([key for object_key in object_keys for key in image_object_keys(object_key)])


def media_asset_rows_for_ids(db: Any, asset_ids: list[int]) -> list[Any]:
    asset_ids = list(dict.fromkeys(int(asset_id) for asset_id in asset_ids if asset_id))
    if not asset_ids:
        return []
    placeholders = ", ".join("?" for _ in asset_ids)
    return db.execute(f"SELECT id, object_key, size_bytes FROM media_assets WHERE id IN ({placeholders})", asset_ids).fetchall()


@app.get("/api/styling-garments")
def list_styling_garments(
    include_archived: bool = Query(False, alias="includeArchived"),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    keyword: str | None = Query(None),
    status: list[str] | None = Query(None),
    category: list[str] | None = Query(None),
    primary_category: list[str] | None = Query(None, alias="primaryCategory"),
    secondary_category: list[str] | None = Query(None, alias="secondaryCategory"),
    color: list[str] | None = Query(None),
    season: list[str] | None = Query(None),
    series: list[str] | None = Query(None),
    channel: list[str] | None = Query(None),
    review_status: list[str] | None = Query(None, alias="reviewStatus"),
    uploaded_from: str | None = Query(None, alias="uploadedFrom"),
    uploaded_to: str | None = Query(None, alias="uploadedTo"),
    sort_by: str = Query("createdAt", alias="sortBy"),
    sort_dir: str = Query("desc", alias="sortDir"),
) -> dict[str, Any]:
    expire_stale_styling_garment_tagging()
    status_values = split_filter_values(status)
    uploaded_from_dt = parse_filter_datetime(uploaded_from, end_of_day=False)
    uploaded_to_dt = parse_filter_datetime(uploaded_to, end_of_day=True)
    where_clauses = ["sg.deleted_at IS NULL", "(? OR sg.archived_at IS NULL)"]
    query_params: list[Any] = [include_archived]
    keyword_search = f"%{keyword.strip()}%" if keyword and keyword.strip() else None
    if keyword_search:
        where_clauses.append(
            """
            (
                sg.code LIKE ? OR sg.status LIKE ? OR sg.ai_tags_json LIKE ? OR
                sg.manual_tags_json LIKE ? OR sg.official_title_tags_json LIKE ? OR
                sg.effective_tags_json LIKE ? OR sg.tag_meta_json LIKE ?
            )
            """
        )
        query_params.extend([keyword_search] * 7)
    if status_values:
        placeholders = ", ".join("?" for _ in status_values)
        where_clauses.append(f"LOWER(sg.status) IN ({placeholders})")
        query_params.extend(sorted(status_values))
    if uploaded_from_dt:
        where_clauses.append("sg.created_at >= ?")
        query_params.append(uploaded_from_dt.isoformat())
    if uploaded_to_dt:
        where_clauses.append("sg.created_at <= ?")
        query_params.append(uploaded_to_dt.isoformat())
    with get_db() as db:
        rows = db.execute(
            f"""
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
            WHERE {" AND ".join(where_clauses)}
            ORDER BY sg.created_at DESC, sg.id DESC
            """,
            query_params,
        ).fetchall()
        contexts = [styling_garment_filter_context(row) for row in rows]
        facet_rows = db.execute(
            """
            SELECT sg.*
            FROM styling_garments sg
            WHERE sg.deleted_at IS NULL AND (? OR sg.archived_at IS NULL)
            ORDER BY sg.created_at DESC, sg.id DESC
            """,
            (include_archived,),
        ).fetchall()
        stable_facets = styling_garment_facets([styling_garment_filter_context(row) for row in facet_rows])
        filters = {
            "keyword": keyword,
            "status": status_values,
            "category": split_filter_values(category),
            "primaryCategory": split_filter_values(primary_category),
            "secondaryCategory": split_filter_values(secondary_category),
            "color": split_filter_values(color),
            "season": split_filter_values(season),
            "series": split_filter_values(series),
            "channel": split_filter_values(channel),
            "reviewStatus": split_filter_values(review_status),
            "uploadedFrom": uploaded_from,
            "uploadedTo": uploaded_to,
        }
        filtered = [context for context in contexts if styling_garment_matches_filters(context, filters)]
        facets = styling_garment_facets(filtered)
        sorted_contexts = sorted_styling_garment_contexts(filtered, sort_by, sort_dir)
        total = len(sorted_contexts)
        items = [context["row"] for context in sorted_contexts[offset:offset + limit]]
        return {
            "items": [serialize_styling_garment(row, include_images=False) for row in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if offset + limit < total else None,
            "hasMore": offset + limit < total,
            "total": total,
            "totalPages": (total + limit - 1) // limit if total else 0,
            "facets": facets,
            "stableFacets": stable_facets,
        }


def split_filter_values(value: str | list[str] | None) -> set[str]:
    if not value:
        return set()
    items = value if isinstance(value, list) else [value]
    return {
        part.strip().casefold()
        for item in items
        for part in re.split(r"[,，]", item)
        if part.strip()
    }


def styling_garment_filter_context(row: Any) -> dict[str, Any]:
    ai_tags = loads(row["ai_tags_json"], {}) if "ai_tags_json" in row.keys() else {}
    manual_tags = loads(row["manual_tags_json"], {}) if "manual_tags_json" in row.keys() else {}
    official_title_tags = loads(row["official_title_tags_json"], {}) if "official_title_tags_json" in row.keys() else {}
    effective_tags = loads(row["effective_tags_json"], {}) if "effective_tags_json" in row.keys() else {}
    if not effective_tags:
        effective_tags = build_effective_tags(ai_tags, official_title_tags, manual_tags)
    tag_meta = loads(row["tag_meta_json"], {}) if "tag_meta_json" in row.keys() else {}
    primary_category = fact_values(ai_tags, effective_tags, official_title_tags, "category", "category_label", "一级品类", "品类")
    secondary_category = fact_values(ai_tags, effective_tags, official_title_tags, "sub_category", "二级品类", "official_category_terms")
    facts = {
        "primaryCategory": primary_category,
        "secondaryCategory": secondary_category,
        "category": list(dict.fromkeys([*primary_category, *secondary_category])),
        "categoryPairs": category_value_pairs(primary_category, secondary_category),
        "color": fact_values(ai_tags, effective_tags, official_title_tags, "colors", "color_labels", "色彩", "颜色"),
        "season": fact_values(ai_tags, effective_tags, official_title_tags, manual_tags, "season", "季节", "season_text", "season_year", "season_drop"),
        "series": fact_values(ai_tags, effective_tags, official_title_tags, manual_tags, "series", "official_series", "internal_collection", "collaboration"),
        "channel": fact_values(manual_tags, "retail_channel"),
    }
    review_status = styling_garment_review_status(row["status"], tag_meta)
    keyword_blob = " ".join(
        str(part)
        for part in (
            row["code"],
            row["status"],
            dumps(ai_tags),
            dumps(effective_tags),
            dumps(manual_tags),
            dumps(official_title_tags),
            dumps(tag_meta),
        )
    ).casefold()
    return {
        "row": row,
        "facts": facts,
        "reviewStatus": review_status,
        "keywordBlob": keyword_blob,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "tagCompletedAt": row["tag_completed_at"],
    }


def fact_values(*sources_and_keys: Any) -> list[str]:
    sources: list[Any] = []
    keys: list[str] = []
    for item in sources_and_keys:
        if isinstance(item, dict):
            sources.append(item)
        elif isinstance(item, str):
            keys.append(item)
    values: list[str] = []
    for source in sources:
        for key in keys:
            values.extend(prompt_value_list(source.get(key)))
    return list(dict.fromkeys(value for value in values if value))


def category_value_pairs(primary_values: list[str], secondary_values: list[str]) -> list[tuple[str, str | None]]:
    if not primary_values:
        return [("未分类", secondary) for secondary in secondary_values]
    if len(primary_values) == 1:
        return [(primary_values[0], secondary) for secondary in secondary_values] or [(primary_values[0], None)]
    if len(primary_values) == len(secondary_values):
        return list(zip(primary_values, secondary_values))
    return [(primary, None) for primary in primary_values]


def styling_garment_review_status(status: str, tag_meta: dict[str, Any]) -> str:
    if status == "TAGGING_READY" or bool(tag_meta.get("needs_review")):
        return "needs_review"
    if status == "TAGGING_FAILED":
        return "failed"
    if status == "TAGGING_RUNNING":
        return "running"
    if status == "READY":
        return "ready"
    return "untagged"


def styling_garment_facets(contexts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": facet_options(context["row"]["status"] for context in contexts),
        "category": facet_options(value for context in contexts for value in context["facts"]["category"]),
        "primaryCategory": facet_options(value for context in contexts for value in context["facts"]["primaryCategory"]),
        "secondaryCategory": facet_options(value for context in contexts for value in context["facts"]["secondaryCategory"]),
        "categoryTree": category_tree_options(contexts),
        "color": facet_options(value for context in contexts for value in context["facts"]["color"]),
        "season": facet_options(value for context in contexts for value in context["facts"]["season"]),
        "series": facet_options(value for context in contexts for value in context["facts"]["series"]),
        "channel": facet_options(value for context in contexts for value in context["facts"]["channel"]),
        "reviewStatus": facet_options(context["reviewStatus"] for context in contexts),
    }


def category_tree_options(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tree: dict[str, dict[str, Any]] = {}
    for context in contexts:
        for primary, secondary in context["facts"]["categoryPairs"]:
            primary_text = str(primary)
            primary_key = primary_text.casefold()
            node = tree.setdefault(primary_key, {"value": primary_text, "label": primary_text, "count": 0, "children": {}})
            node["count"] += 1
            if secondary:
                secondary_text = str(secondary)
                child_key = secondary_text.casefold()
                child = node["children"].setdefault(child_key, {"value": secondary_text, "label": secondary_text, "count": 0})
                child["count"] += 1
    result: list[dict[str, Any]] = []
    for node in tree.values():
        children = sorted(node["children"].values(), key=lambda item: (-int(item["count"]), str(item["label"]).casefold()))
        result.append({**node, "children": children})
    return sorted(result, key=lambda item: (-int(item["count"]), str(item["label"]).casefold()))


def facet_options(values: Any) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key not in counts:
            counts[key] = {"value": text, "label": text, "count": 0}
        counts[key]["count"] += 1
    return sorted(counts.values(), key=lambda item: (-int(item["count"]), str(item["label"]).casefold()))


def styling_garment_matches_filters(context: dict[str, Any], filters: dict[str, Any]) -> bool:
    keyword = str(filters.get("keyword") or "").strip().casefold()
    if keyword and keyword not in context["keywordBlob"]:
        return False
    category_terms = set(filters.get("category") or set())
    category_terms.update(filters.get("primaryCategory") or set())
    category_terms.update(filters.get("secondaryCategory") or set())
    if category_terms and not category_terms.intersection(value.casefold() for value in context["facts"]["category"]):
        return False
    for name in ("color", "season", "series", "channel"):
        selected = filters.get(name) or set()
        if selected and not selected.intersection(value.casefold() for value in context["facts"][name]):
            return False
    statuses = filters.get("status") or set()
    if statuses and context["row"]["status"].casefold() not in statuses:
        return False
    review_statuses = filters.get("reviewStatus") or set()
    if review_statuses and context["reviewStatus"].casefold() not in review_statuses:
        return False
    uploaded_from = parse_filter_datetime(filters.get("uploadedFrom"), end_of_day=False)
    uploaded_to = parse_filter_datetime(filters.get("uploadedTo"), end_of_day=True)
    created_at = parse_filter_datetime(context["createdAt"], end_of_day=False)
    if uploaded_from and created_at and created_at < uploaded_from:
        return False
    if uploaded_to and created_at and created_at > uploaded_to:
        return False
    return True


def parse_filter_datetime(value: Any, *, end_of_day: bool) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text = f"{text}T23:59:59" if end_of_day else f"{text}T00:00:00"
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def sorted_styling_garment_contexts(contexts: list[dict[str, Any]], sort_by: str, sort_dir: str) -> list[dict[str, Any]]:
    key_name = {
        "createdAt": "createdAt",
        "uploadedAt": "createdAt",
        "updatedAt": "updatedAt",
        "tagCompletedAt": "tagCompletedAt",
        "code": "code",
    }.get(sort_by, "createdAt")
    reverse = sort_dir != "asc"

    def sort_key(context: dict[str, Any]) -> tuple[Any, ...]:
        if key_name == "code":
            return (str(context["row"]["code"]).casefold(), int(context["row"]["id"]))
        value = context.get(key_name) or ""
        return (str(value), int(context["row"]["id"]))

    return sorted(contexts, key=sort_key, reverse=reverse)


@app.get("/api/styling-garments/{garment_id}")
def get_styling_garment(garment_id: int, request: Request) -> dict[str, Any]:
    expire_stale_styling_garment_tagging()
    return {"item": get_styling_garment_by_id(garment_id, include_debug=get_request_role(request) == "admin")}


def ensure_styling_garment_not_tagging(row: Any) -> None:
    if row["status"] == "TAGGING_RUNNING":
        raise HTTPException(status_code=409, detail="正在打标，完成后才能修改或删除搭配服装")


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
            SET status = 'FAILED', failed_at = ?, error_code = ?, error_message = ?, updated_at = ?
            WHERE owner_type = 'styling_garment_tagging'
              AND owner_id IN ({placeholders})
              AND status = 'RUNNING'
            """,
            (timestamp, ERROR_POLL_TIMEOUT, message, timestamp, *ids),
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
        raise HTTPException(status_code=400, detail="搭配服装代码不能为空")
    if not files:
        raise HTTPException(status_code=400, detail="至少上传一张图片")
    files = files[:MAX_GARMENT_IMAGE_INPUTS]
    with get_db() as db:
        duplicate = db.execute("SELECT id FROM styling_garments WHERE code = ? AND deleted_at IS NULL", (clean_code,)).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail="搭配服装代码已存在")
    timestamp = now_iso()
    prefix = join_key("styling-garments", f"{clean_code}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}")
    assets = await save_uploads_as_media_assets(files, prefix)
    with get_db() as db:
        duplicate = db.execute("SELECT id FROM styling_garments WHERE code = ? AND deleted_at IS NULL", (clean_code,)).fetchone()
        if duplicate:
            cleanup_objects([key for asset in assets for key in image_object_keys(asset["objectKey"])])
            raise HTTPException(status_code=409, detail="搭配服装代码已存在")
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
    manual_patch = normalize_manual_business_tags(payload.manualTags) if payload.manualTags is not None else None
    if payload.code is not None:
        clean_code = sanitize_name(payload.code)
        if not clean_code:
            raise HTTPException(status_code=400, detail="搭配服装代码不能为空")
        updates["code"] = clean_code
    if manual_patch is None and not updates:
        return {"item": get_styling_garment_by_id(garment_id)}
    with get_db() as db:
        row = db.execute("SELECT * FROM styling_garments WHERE id = ? AND deleted_at IS NULL", (garment_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="搭配服装不存在")
        ensure_styling_garment_not_tagging(row)
        if "code" in updates:
            duplicate = db.execute(
                "SELECT id FROM styling_garments WHERE code = ? AND id <> ? AND deleted_at IS NULL",
                (updates["code"], garment_id),
            ).fetchone()
            if duplicate:
                raise HTTPException(status_code=409, detail="搭配服装代码已存在")
        if manual_patch is not None:
            current_manual = loads(row["manual_tags_json"], {})
            updates["manual_tags_json"] = dumps(normalize_manual_business_tags({**current_manual, **manual_patch}))
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


@app.post("/api/styling-garments/batch-manual-tags")
def update_styling_garment_manual_tags_batch(payload: StylingGarmentBatchManualTagsRequest) -> dict[str, Any]:
    garment_ids = normalized_styling_garment_ids(payload.ids or [])
    manual_patch = normalize_manual_business_tags(payload.manualTags)
    if not manual_patch:
        raise HTTPException(status_code=400, detail="人工业务标签不能为空")
    timestamp = now_iso()
    with get_db() as db:
        rows = get_styling_garment_rows_for_mutation(db, garment_ids)
        for row in rows:
            ensure_styling_garment_not_tagging(row)
        for row in rows:
            current_manual = loads(row["manual_tags_json"], {})
            manual_tags = normalize_manual_business_tags({**current_manual, **manual_patch})
            effective_tags = build_effective_tags(
                loads(row["ai_tags_json"], {}),
                loads(row["official_title_tags_json"], {}),
                manual_tags,
            )
            db.execute(
                """
                UPDATE styling_garments
                SET manual_tags_json = ?, effective_tags_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (dumps(manual_tags), dumps(effective_tags), timestamp, row["id"]),
            )
            log_event(db, "styling_garment", int(row["id"]), "manual_tags_updated", {"batch": True, "keys": sorted(manual_patch.keys())})
    return {"items": [get_styling_garment_by_id(garment_id) for garment_id in garment_ids]}


@app.post("/api/styling-garments/{garment_id}/save-and-tag")
async def save_and_tag_styling_garment(garment_id: int, background_tasks: BackgroundTasks) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM styling_garments WHERE id = ? AND deleted_at IS NULL", (garment_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="搭配服装不存在")
        ensure_styling_garment_not_tagging(row)
        timestamp = now_iso()
        db.execute("UPDATE styling_garments SET status = 'TAGGING_RUNNING', tag_error = NULL, updated_at = ? WHERE id = ?", (timestamp, garment_id))
    background_tasks.add_task(run_styling_garment_tagging, garment_id)
    return {"item": get_styling_garment_by_id(garment_id)}


@app.post("/api/styling-garments/batch-save-and-tag")
async def batch_save_and_tag_styling_garments(payload: StylingGarmentBatchTagRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    garment_ids = list(dict.fromkeys(payload.ids))
    if not garment_ids:
        raise HTTPException(status_code=400, detail="至少选择一套搭配服装")
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
            raise HTTPException(status_code=404, detail="搭配服装不存在")
        for garment_id in garment_ids:
            ensure_styling_garment_not_tagging(rows_by_id[garment_id])
        db.execute(
            f"UPDATE styling_garments SET status = 'TAGGING_RUNNING', tag_error = NULL, updated_at = ? WHERE id IN ({placeholders})",
            (timestamp, *garment_ids),
        )
    for garment_id in garment_ids:
        background_tasks.add_task(run_styling_garment_tagging, garment_id)
    return {"items": [get_styling_garment_by_id(garment_id) for garment_id in garment_ids]}


_tagging_semaphore: asyncio.Semaphore | None = None


def _get_tagging_semaphore() -> asyncio.Semaphore:
    """打标并发闸门：限制同时调用 Gemini 打标的数量，超出的排队等待。

    懒初始化到当前事件循环，避免在导入期（无 loop）创建。
    """
    global _tagging_semaphore
    if _tagging_semaphore is None:
        _tagging_semaphore = asyncio.Semaphore(max(1, settings.tagging_max_concurrency))
    return _tagging_semaphore


async def run_styling_garment_tagging(garment_id: int) -> None:
    async with _get_tagging_semaphore():
        await _run_styling_garment_tagging_guarded(garment_id)


async def _run_styling_garment_tagging_guarded(garment_id: int) -> None:
    # 瞬时错误（网关/连接/限流）自动整条重试；重复打标幂等覆盖同一 garment。
    retry_limit = max(0, settings.tagging_auto_retry_limit)
    attempt = 0
    while True:
        try:
            tagging_result = await tag_styling_garment_with_gemini(garment_id)
            break
        except Exception as exc:
            error_code = classify_attempt_error(exc)
            if is_retryable_error(error_code) and attempt < retry_limit:
                attempt += 1
                logger.warning("打标瞬时错误(%s)，自动整条重试第 %d 次：garment=%s", error_code, attempt, garment_id)
                continue
            with get_db() as db:
                db.execute(
                    "UPDATE styling_garments SET status = 'TAGGING_FAILED', tag_error = ?, updated_at = ? WHERE id = ?",
                    (str(exc), now_iso(), garment_id),
                )
            return
    try:
        persist_styling_garment_tagging_result(garment_id, tagging_result)
    except Exception as exc:
        attempt_id = tagging_result.get("attempt_id")
        if attempt_id:
            finish_attempt(
                int(attempt_id),
                "FAILED",
                response_snapshot={**tagging_result, "error": str(exc)},
                error_message=str(exc),
                error_code=classify_attempt_error(exc) or ERROR_SYSTEM_ERROR,
            )
        with get_db() as db:
            db.execute(
                "UPDATE styling_garments SET status = 'TAGGING_FAILED', tag_error = ?, updated_at = ? WHERE id = ?",
                (str(exc), now_iso(), garment_id),
            )


def persist_styling_garment_tagging_result(garment_id: int, tagging_result: dict[str, Any]) -> None:
    with get_db() as db:
        timestamp = now_iso()
        row = db.execute("SELECT manual_tags_json FROM styling_garments WHERE id = ?", (garment_id,)).fetchone()
        manual_tags = loads(row["manual_tags_json"], {}) if row else {}
        apply_styling_garment_selected_image_order(
            db,
            garment_id,
            [int(image_id) for image_id in tagging_result.get("selected_image_ids", [])],
            timestamp,
        )
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
        log_event(db, "styling_garment", garment_id, "tagged", tagging_result)


@app.post("/api/styling-garments/{garment_id}/images")
async def add_styling_garment_images(
    garment_id: int,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    raise HTTPException(status_code=410, detail="创建后不支持继续添加图片，请重新导入一套素材")


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
        raise HTTPException(status_code=404, detail="搭配服装图片不存在")
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
            raise HTTPException(status_code=404, detail="搭配服装不存在")
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
            raise HTTPException(status_code=404, detail="搭配服装图片不存在")
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
            raise HTTPException(status_code=404, detail="搭配服装图片不存在")
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
            raise HTTPException(status_code=404, detail="搭配服装图片不存在")
        ensure_styling_garment_not_tagging({"status": row["garment_status"]})
        garment_id = int(row["styling_garment_id"])
        remaining = db.execute(
            "SELECT COUNT(*) AS count FROM styling_garment_images WHERE styling_garment_id = ? AND deleted_at IS NULL AND id <> ?",
            (garment_id, image_id),
        ).fetchone()["count"]
        if int(remaining) < 1:
            raise HTTPException(status_code=409, detail="每套搭配服装至少保留一张图片")
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
            raise HTTPException(status_code=404, detail="搭配服装不存在")
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


@app.post("/api/styling-garments/{garment_id}/restore")
def restore_styling_garment(garment_id: int) -> dict[str, Any]:
    with get_db() as db:
        rows = get_styling_garment_rows_for_mutation(db, [garment_id])
        row = rows[0]
        restore_styling_garments(db, [garment_id])
        if row["archived_at"] is not None:
            log_event(db, "styling_garment", garment_id, "restored", {"code": row["code"]})
    return {"ok": True, "restored": True, "item": get_styling_garment_by_id(garment_id)}


@app.post("/api/styling-garments/batch-restore")
def restore_styling_garment_batch(payload: StylingGarmentDeleteRequest) -> dict[str, Any]:
    garment_ids = normalized_styling_garment_ids(payload.ids or [])
    with get_db() as db:
        rows = get_styling_garment_rows_for_mutation(db, garment_ids)
        restore_styling_garments(db, garment_ids)
        for row in rows:
            if row["archived_at"] is not None:
                log_event(db, "styling_garment", int(row["id"]), "restored", {"code": row["code"], "batch": True})
    return {"ok": True, "restored": True, "ids": garment_ids}


def normalized_styling_garment_ids(ids: list[int]) -> list[int]:
    normalized = list(dict.fromkeys(int(garment_id) for garment_id in ids if int(garment_id) > 0))
    if not normalized:
        raise HTTPException(status_code=400, detail="请选择至少一套搭配服装")
    if len(normalized) > 100:
        raise HTTPException(status_code=400, detail="单次最多处理 100 套搭配服装")
    return normalized


def get_styling_garment_rows_for_mutation(db: Any, garment_ids: list[int]) -> list[Any]:
    placeholders = ", ".join("?" for _ in garment_ids)
    rows = db.execute(
        f"SELECT * FROM styling_garments WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        garment_ids,
    ).fetchall()
    if len(rows) != len(garment_ids):
        raise HTTPException(status_code=404, detail="部分搭配服装不存在")
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
          (SELECT COUNT(*) FROM generation_previews WHERE task_id IN ({placeholders})) AS preview_count,
          (SELECT COUNT(*) FROM generation_result_sets WHERE task_id IN ({placeholders})) AS result_set_count,
          (SELECT COUNT(*) FROM generation_result_images_new gri JOIN generation_result_sets grs ON grs.id = gri.result_set_id WHERE grs.task_id IN ({placeholders})) AS result_image_count
        """,
        (*task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids),
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
        (*task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids),
    ).fetchall()
    asset_ids = list(dict.fromkeys(int(asset["asset_id"]) for asset in asset_rows if asset["asset_id"] is not None))
    media_rows = media_asset_rows_for_ids(db, asset_ids)
    total_bytes = sum(int(media["size_bytes"] or 0) for media in media_rows if "size_bytes" in media.keys())
    return {
        "taskCount": int(row["task_count"] or 0),
        "taskImageCount": int(row["task_image_count"] or 0),
        "inputVersionCount": int(row["input_version_count"] or 0),
        "stylingCount": int(row["styling_count"] or 0),
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


def restore_styling_garments(db: Any, garment_ids: list[int]) -> None:
    garment_ids = normalized_styling_garment_ids(garment_ids)
    placeholders = ", ".join("?" for _ in garment_ids)
    db.execute(
        f"UPDATE styling_garments SET archived_at = NULL, archive_reason = NULL, updated_at = ? WHERE id IN ({placeholders})",
        (now_iso(), *garment_ids),
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
    files = files[:MAX_HERO_IMAGE_INPUTS]
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


@app.post("/api/generation-tasks/from-uploads")
async def create_generation_task_from_uploads(payload: GenerationTaskFromUploadsRequest) -> dict[str, Any]:
    if not payload.uploads:
        raise HTTPException(status_code=400, detail="至少上传一张主推款图片")
    uploads = payload.uploads[:MAX_HERO_IMAGE_INPUTS]
    task_name = (payload.name or datetime.now().strftime("%Y%m%d%H%M%S")).strip()
    assets = await save_uploaded_objects_as_media_assets(uploads)
    timestamp = now_iso()
    try:
        with get_db() as db:
            batch_id = (payload.batchId or "").strip()[:64] or None
            cursor = db.execute(
                "INSERT INTO generation_tasks (name, batch_id, status, created_at, updated_at) VALUES (?, ?, 'DRAFT', ?, ?)",
                (task_name, batch_id, timestamp, timestamp),
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
            log_event(db, "generation_task", task_id, "created", {"name": task_name, "fileCount": len(assets), "uploadMode": "direct"})
    except Exception:
        cleanup_created_media_assets(assets)
        raise
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
def get_generation_task(task_id: int, request: Request, include_debug: bool = Query(False, alias="includeDebug")) -> dict[str, Any]:
    return {"item": get_generation_task_by_id(task_id, include_debug=require_admin_debug(request, include_debug))}


@app.get("/api/generation-tasks/{task_id}/ai-invocations")
def get_generation_task_ai_invocations(task_id: int, request: Request) -> dict[str, Any]:
    require_admin(request)
    get_generation_task_row(task_id)
    return {"items": get_task_attempts(task_id)}


@app.get("/api/generation-tasks/{task_id}/attempts")
def get_generation_task_attempt_summaries(
    task_id: int,
    owner_type: str = Query(..., alias="ownerType"),
    limit: int = Query(20, ge=1, le=100),
    before_created_at: str | None = Query(None, alias="beforeCreatedAt"),
    before_id: int | None = Query(None, ge=1, alias="beforeId"),
) -> dict[str, Any]:
    get_generation_task_row(task_id)
    if owner_type not in GENERATION_ATTEMPT_OWNER_TYPES:
        raise HTTPException(status_code=400, detail="不支持的调用步骤")
    if (before_created_at is None) != (before_id is None):
        raise HTTPException(status_code=400, detail="分页游标不完整")
    return get_task_attempt_summary_page(task_id, owner_type, limit, before_created_at, before_id)


@app.get("/api/generation-tasks/{task_id}/ai-invocations/{attempt_id}")
def get_generation_task_ai_invocation(task_id: int, attempt_id: int, request: Request) -> dict[str, Any]:
    require_admin(request)
    get_generation_task_row(task_id)
    record = get_task_attempt_detail(task_id, attempt_id)
    if not record:
        raise HTTPException(status_code=404, detail="调用记录不存在")
    return {"item": record}


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


@app.post("/api/generation-tasks/{task_id}/restore")
def restore_generation_task(task_id: int) -> dict[str, Any]:
    with get_db() as db:
        rows = get_generation_task_rows_for_mutation(db, [task_id])
        row = rows[0]
        timestamp = now_iso()
        db.execute(
            "UPDATE generation_tasks SET archived_at = NULL, archive_reason = NULL, updated_at = ? WHERE id = ?",
            (timestamp, task_id),
        )
        if row["archived_at"] is not None:
            log_event(db, "generation_task", task_id, "restored", {"name": row["name"]})
    return {"ok": True, "restored": True, "item": get_generation_task_by_id(task_id)}


@app.post("/api/generation-tasks/batch-restore")
def restore_generation_tasks(payload: GenerationTaskDeleteRequest) -> dict[str, Any]:
    task_ids = normalized_task_ids(payload.ids or [])
    with get_db() as db:
        rows = get_generation_task_rows_for_mutation(db, task_ids)
        timestamp = now_iso()
        placeholders = ", ".join("?" for _ in task_ids)
        db.execute(
            f"UPDATE generation_tasks SET archived_at = NULL, archive_reason = NULL, updated_at = ? WHERE id IN ({placeholders})",
            (timestamp, *task_ids),
        )
        for row in rows:
            if row["archived_at"] is not None:
                log_event(db, "generation_task", int(row["id"]), "restored", {"name": row["name"], "batch": True})
    return {"ok": True, "restored": True, "ids": task_ids}


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
        if row["status"] == "PREVIEW_RUNNING":
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
            db.execute(
                """
                UPDATE generation_tasks
                SET auto_run_target = NULL, auto_run_requested_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, task_id),
            )
        else:
            db.execute(
                """
                UPDATE generation_tasks
                SET status = 'CANCELLED',
                    auto_run_target = NULL,
                    auto_run_requested_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
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
    instruction_fields = {
        "aiStylingUserInstruction": "ai_styling_user_instruction_text",
        "storyboardPreviewUserInstruction": "storyboard_preview_user_instruction_text",
        "finalImageUserInstruction": "final_image_user_instruction_text",
    }
    if (
        payload.name is None
        and not any(payload_has_field(payload, field) for field in instruction_fields)
    ):
        return {"item": get_generation_task_by_id(task_id)}
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成任务不存在")
        ensure_generation_task_editable(row)
        updates: list[str] = []
        values: list[Any] = []
        if payload.name is not None:
            updates.append("name = ?")
            values.append(payload.name.strip() or "未命名任务")
        for field, column in instruction_fields.items():
            if payload_has_field(payload, field):
                updates.append(f"{column} = ?")
                values.append(normalize_user_input_text(getattr(payload, field)))
        updates.append("updated_at = ?")
        timestamp = now_iso()
        values.append(timestamp)
        values.append(task_id)
        db.execute(f"UPDATE generation_tasks SET {', '.join(updates)} WHERE id = ?", values)
    return {"item": get_generation_task_by_id(task_id)}


def ensure_generation_task_draft(task: Any) -> None:
    if task["status"] != "DRAFT":
        raise HTTPException(status_code=409, detail="任务进入 AI 阶段后不可修改主推款输入")


def ensure_generation_task_editable(task: Any) -> None:
    ensure_generation_task_not_running(task)


def ensure_generation_task_not_running(task: Any) -> None:
    if task["status"] in RUNNING_GENERATION_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="任务正在生成中，请先取消生成")


def mark_generation_task_input_changed(db: Any, task_id: int, timestamp: str, reason: str) -> None:
    db.execute(
        """
        UPDATE generation_stylings
        SET status = 'SUPERSEDED', updated_at = ?
        WHERE task_id = ? AND status = 'SUCCEEDED'
        """,
        (timestamp, task_id),
    )
    db.execute(
        """
        UPDATE generation_previews
        SET status = 'SUPERSEDED', updated_at = ?
        WHERE task_id = ? AND status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')
        """,
        (timestamp, task_id),
    )
    supersede_generation_result_sets(db, task_id, timestamp)
    db.execute(
        """
        UPDATE generation_tasks
        SET status = 'READY_FOR_STYLING',
            current_input_version_id = NULL,
            active_styling_id = NULL,
            active_preview_id = NULL,
            active_storyboard_preview_id = NULL,
            active_result_set_id = NULL,
            auto_run_target = NULL,
            auto_run_requested_at = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (timestamp, task_id),
    )
    log_event(db, "generation_task", task_id, "input_changed", {"reason": reason})


def reset_generation_task_for_rerun(db: Any, task_id: int, timestamp: str) -> None:
    """「重新一键成片」：主推款输入不变，把当前 active 的选搭/预览/成片整条置为 SUPERSEDED 历史，
    任务回到 READY_FOR_STYLING，使自动推进从智能选搭重新开始。旧结果保留可追溯，不删除。"""
    db.execute(
        """
        UPDATE generation_stylings
        SET status = 'SUPERSEDED', updated_at = ?
        WHERE task_id = ? AND status = 'SUCCEEDED'
        """,
        (timestamp, task_id),
    )
    db.execute(
        """
        UPDATE generation_previews
        SET status = 'SUPERSEDED', updated_at = ?
        WHERE task_id = ? AND status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')
        """,
        (timestamp, task_id),
    )
    supersede_generation_result_sets(db, task_id, timestamp)
    db.execute(
        """
        UPDATE generation_tasks
        SET status = 'READY_FOR_STYLING',
            active_styling_id = NULL,
            active_preview_id = NULL,
            active_storyboard_preview_id = NULL,
            active_result_set_id = NULL,
            auto_run_target = NULL,
            auto_run_requested_at = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (timestamp, task_id),
    )
    log_event(db, "generation_task", task_id, "restarted", {"reason": "rerun_from_scratch"})


def interrupted_generation_resume_status(db: Any, task_id: int, terminal_status: str) -> str:
    """Map the most recent failed/cancelled attempt back to its resumable task state.

    Active pointers deliberately keep the last successful artifacts until a replacement succeeds,
    so they cannot identify which in-flight regeneration the user cancelled.
    """
    if terminal_status not in {"FAILED", "CANCELLED"}:
        raise ValueError(f"不支持的中断状态：{terminal_status}")
    attempt = db.execute(
        """
        SELECT owner_type
        FROM generation_attempts
        WHERE status = ?
          AND (
            (owner_type = 'generation_styling' AND owner_id = ?)
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
        ORDER BY COALESCE(failed_at, cancelled_at, updated_at, created_at) DESC, id DESC
        LIMIT 1
        """,
        (terminal_status, task_id, task_id, task_id),
    ).fetchone()
    owner_type = attempt["owner_type"] if attempt else None
    if owner_type == "generation_result_image":
        return "PREVIEW_READY"
    if owner_type == "generation_preview":
        return "STYLING_READY"
    if owner_type == "generation_styling":
        return "READY_FOR_STYLING"

    # A queued preview/final-image operation may end before its attempt is created.
    # Compare both artifact types by time instead of letting an older final interruption win.
    interrupted_artifact = db.execute(
        """
        SELECT stage
        FROM (
          SELECT 'final' AS stage, gri.updated_at AS interrupted_at, gri.id AS artifact_id
          FROM generation_result_images_new gri
          JOIN generation_result_sets grs ON grs.id = gri.result_set_id
          WHERE grs.task_id = ? AND gri.status = ?
          UNION ALL
          SELECT 'preview' AS stage, gp.updated_at AS interrupted_at, gp.id AS artifact_id
          FROM generation_previews gp
          WHERE gp.task_id = ? AND gp.status = ?
        ) interrupted_artifacts
        ORDER BY interrupted_at DESC, artifact_id DESC
        LIMIT 1
        """,
        (task_id, terminal_status, task_id, terminal_status),
    ).fetchone()
    if interrupted_artifact and interrupted_artifact["stage"] == "final":
        return "PREVIEW_READY"
    if interrupted_artifact and interrupted_artifact["stage"] == "preview":
        return "STYLING_READY"
    return "READY_FOR_STYLING"


def cancelled_generation_resume_status(db: Any, task_id: int) -> str:
    return interrupted_generation_resume_status(db, task_id, "CANCELLED")


def resume_cancelled_generation_task(db: Any, task_id: int, timestamp: str) -> str:
    resume_status = cancelled_generation_resume_status(db, task_id)
    db.execute(
        "UPDATE generation_tasks SET status = ?, updated_at = ? WHERE id = ? AND status = 'CANCELLED'",
        (resume_status, timestamp, task_id),
    )
    log_event(db, "generation_task", task_id, "resumed", {"resumeStatus": resume_status})
    return resume_status


def resume_failed_generation_task(db: Any, task_id: int, timestamp: str) -> str:
    resume_status = interrupted_generation_resume_status(db, task_id, "FAILED")
    db.execute(
        "UPDATE generation_tasks SET status = ?, updated_at = ? WHERE id = ? AND status = 'FAILED'",
        (resume_status, timestamp, task_id),
    )
    log_event(db, "generation_task", task_id, "resumed", {"resumeStatus": resume_status, "previousStatus": "FAILED"})
    return resume_status


def generation_task_stage_instruction_column(stage: str) -> str | None:
    return {
        "ai_styling": "ai_styling_user_instruction_text",
        "storyboard_preview": "storyboard_preview_user_instruction_text",
        "final_image": "final_image_user_instruction_text",
    }.get(stage)


def pending_generation_task_stage_instruction(task: Any, stage: str) -> str:
    column = generation_task_stage_instruction_column(stage)
    return normalize_user_input_text(row_value(task, column, "")) if column else ""


def persist_generation_task_stage_instruction(db: Any, task_id: int, stage: str, instruction: str | None, timestamp: str) -> None:
    column = generation_task_stage_instruction_column(stage)
    if not column:
        return
    db.execute(
        f"UPDATE generation_tasks SET {column} = ?, updated_at = ? WHERE id = ?",
        (normalize_user_input_text(instruction), timestamp, task_id),
    )


def cancel_running_task_attempts(db: Any, task_id: int, timestamp: str) -> None:
    db.execute(
        """
        UPDATE generation_attempts
        SET status = 'CANCELLED', cancelled_at = COALESCE(cancelled_at, ?), updated_at = ?
        WHERE status = 'RUNNING'
          AND (
            (owner_type = 'generation_styling' AND owner_id = ?)
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
        (timestamp, timestamp, task_id, task_id, task_id),
    )


def supersede_generation_result_sets(db: Any, task_id: int, timestamp: str, keep_result_set_id: int | None = None) -> None:
    params: list[Any] = [timestamp, task_id]
    keep_clause = ""
    aliased_keep_clause = ""
    if keep_result_set_id is not None:
        keep_clause = "AND id <> ?"
        aliased_keep_clause = "AND grs.id <> ?"
        params.append(keep_result_set_id)
    db.execute(
        f"""
        UPDATE generation_result_sets
        SET status = 'SUPERSEDED', updated_at = ?
        WHERE task_id = ?
          {keep_clause}
          AND status IN ('RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED')
        """,
        params,
    )
    image_params: list[Any] = [timestamp, task_id]
    if keep_result_set_id is not None:
        image_params.append(keep_result_set_id)
    db.execute(
        f"""
        UPDATE generation_result_images_new
        SET status = 'CANCELLED', locked_by = NULL, locked_until = NULL, updated_at = ?
        WHERE result_set_id IN (
          SELECT id FROM generation_result_sets
          WHERE task_id = ?
            {keep_clause}
            AND status = 'SUPERSEDED'
        )
          AND status IN ('QUEUED', 'RUNNING')
        """,
        image_params,
    )
    attempt_params: list[Any] = [timestamp, timestamp, task_id]
    if keep_result_set_id is not None:
        attempt_params.append(keep_result_set_id)
    db.execute(
        f"""
        UPDATE generation_attempts
        SET status = 'CANCELLED', cancelled_at = COALESCE(cancelled_at, ?), updated_at = ?
        WHERE status IN ('RUNNING', 'SUBMITTED', 'IN_QUEUE', 'IN_PROGRESS')
          AND owner_type = 'generation_result_image'
          AND owner_id IN (
            SELECT gri.id
            FROM generation_result_images_new gri
            JOIN generation_result_sets grs ON grs.id = gri.result_set_id
            WHERE grs.task_id = ?
              {aliased_keep_clause}
              AND grs.status = 'SUPERSEDED'
          )
        """,
        attempt_params,
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
        (*task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids),
    ).fetchall()
    asset_ids = list(dict.fromkeys(int(row["asset_id"]) for row in asset_rows if row["asset_id"] is not None))
    media_rows = media_asset_rows_for_ids(db, asset_ids)
    object_keys = [media["object_key"] for media in media_rows]
    cleanup_objects([key for media in media_rows for key in image_object_keys(media["object_key"])])
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
        (*task_ids, *task_ids, *task_ids),
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
    raise HTTPException(status_code=410, detail="创建后不支持继续添加主推款图片，请重新创建拍摄任务")


@app.post("/api/generation-tasks/{task_id}/images/from-uploads")
async def add_generation_task_images_from_uploads(task_id: int, payload: GenerationTaskFromUploadsRequest) -> dict[str, Any]:
    raise HTTPException(status_code=410, detail="创建后不支持继续添加主推款图片，请重新创建拍摄任务")


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
    ensure_generation_task_editable(row)
    prefix = join_key("generation-tasks", f"{sanitize_name(row['name']) or 'task'}-{uuid4().hex[:8]}")
    asset = (await save_uploads_as_media_assets([file], prefix))[0]
    with get_db() as db:
        timestamp = now_iso()
        db.execute(
            "UPDATE generation_task_images SET media_asset_id = ?, updated_at = ? WHERE id = ?",
            (asset["id"], timestamp, image_id),
        )
        release_media_asset_if_unreferenced(db, int(row["previous_media_asset_id"]))
        db.execute("UPDATE generation_tasks SET updated_at = ? WHERE id = ?", (timestamp, row["task_id"]))
        if row["status"] != "DRAFT":
            mark_generation_task_input_changed(db, int(row["task_id"]), timestamp, "hero_product_image_replaced")
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
        ensure_generation_task_editable(row)
        if payload.sortOrder is not None:
            timestamp = now_iso()
            db.execute(
                "UPDATE generation_task_images SET sort_order = ?, updated_at = ? WHERE id = ?",
                (payload.sortOrder, timestamp, image_id),
            )
            db.execute("UPDATE generation_tasks SET updated_at = ? WHERE id = ?", (timestamp, row["task_id"]))
            if row["status"] != "DRAFT":
                mark_generation_task_input_changed(db, int(row["task_id"]), timestamp, "hero_product_image_reordered")
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
        ensure_generation_task_editable(row)
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
        if row["status"] != "DRAFT":
            mark_generation_task_input_changed(db, int(row["task_id"]), timestamp, "hero_product_image_deleted")
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
async def select_generation_styling(task_id: int, request: Request, payload: GenerationStylingRequest | None = None) -> dict[str, Any]:
    return await run_select_generation_styling(
        task_id,
        include_debug=admin_debug_for_request(request),
        trigger="manual_step",
        user_instruction=payload.userInstruction if payload and payload_has_field(payload, "userInstruction") else (payload.userRequirement if payload else None),
    )


async def run_select_generation_styling(
    task_id: int,
    include_debug: bool = False,
    trigger: str = "manual_step",
    user_instruction: str | None = None,
) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    if task["status"] == "DRAFT":
        # 上传即草稿；首次选搭时自动进入待选搭，用户无需单独“确认”步骤。
        ready_generation_task(task_id)
        task = get_generation_task_row(task_id)
    if task["status"] not in {
        "READY_FOR_STYLING",
        "STYLING_READY",
        "PREVIEW_READY",
        "COMPLETED",
        "FINAL_PARTIAL",
        "FAILED",
        "CANCELLED",
    }:
        raise HTTPException(status_code=409, detail="当前任务状态不能智能选搭")
    effective_user_instruction = user_instruction if user_instruction is not None else pending_generation_task_stage_instruction(task, "ai_styling")
    user_instruction_context = build_stage_user_instruction_context("ai_styling", effective_user_instruction)
    user_requirement_context = build_user_requirement_context(task, effective_user_instruction)
    input_version_id = create_generation_input_version(task_id)
    with get_db() as db:
        timestamp = now_iso()
        if user_instruction is not None:
            persist_generation_task_stage_instruction(db, task_id, "ai_styling", effective_user_instruction, timestamp)
        db.execute("UPDATE generation_tasks SET status = 'STYLING_RUNNING', updated_at = ? WHERE id = ?", (timestamp, task_id))
    attempt_id = create_attempt("generation_styling", task_id, "gemini")
    progress = AttemptProgressRecorder(attempt_id)
    styling_plan: dict[str, Any] | None = None

    def fail_styling_attempt(exc: Exception) -> dict[str, Any]:
        progress.finish()
        with get_db() as db:
            current = db.execute("SELECT status FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
            if not current or current["status"] == "CANCELLED":
                if attempt_id:
                    finish_attempt(attempt_id, "CANCELLED", error_message=str(exc))
                return {"item": get_generation_task_by_id(task_id)}
        error_code = classify_attempt_error(exc) or classify_styling_business_reject(exc)
        if attempt_id:
            finish_attempt(attempt_id, "FAILED", response_snapshot=styling_plan or exception_process_facts(exc), error_message=str(exc), error_code=error_code)
        with get_db() as db:
            db.execute(
                """
                UPDATE generation_tasks
                SET status = 'FAILED',
                    auto_run_target = NULL,
                    auto_run_requested_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_iso(), task_id),
            )
        raise HTTPException(status_code=502, detail=user_facing_provider_error_message("智能选搭失败", exc, error_code)) from exc

    try:
        progress.begin("styling_context")
        input_row, candidate_snapshot, request_snapshot = build_ai_stylist_context(input_version_id)
        request_snapshot["invocation"] = {"mode": trigger, "target": "styling"}
        request_snapshot["ai_styling_user_instruction"] = user_instruction_context
        request_snapshot["user_requirement"] = user_requirement_context
        request_snapshot = attach_ai_stylist_prompt_snapshot(request_snapshot, candidate_snapshot)
        update_attempt_request_snapshot(attempt_id, request_snapshot)
        if generation_task_or_attempt_cancelled(task_id, attempt_id):
            progress.finish()
            finish_attempt(attempt_id, "CANCELLED")
            return {"item": get_generation_task_by_id(task_id)}
        styling_plan = await select_styling_with_gemini(input_row, candidate_snapshot, request_snapshot, attempt_id=attempt_id, progress=progress)
        persist_attempt_provider_progress(attempt_id, styling_plan)
        progress.begin("styling_validate")
        validate_styling_plan(styling_plan, candidate_snapshot)
        selected_ids = [int(item) for item in styling_plan.get("selected_material_ids", [])]
        source_ids, selected_snapshot = get_styling_source_and_selected_snapshots(selected_ids)
    except Exception as exc:
        return fail_styling_attempt(exc)
    try:
        with get_db() as db:
            current = db.execute("SELECT status FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
            if current and current["status"] == "CANCELLED":
                if attempt_id:
                    progress.finish()
                    finish_attempt(attempt_id, "CANCELLED", response_snapshot=styling_plan)
                return {"item": get_generation_task_by_id(task_id)}
        progress.begin("styling_store")
        with get_db() as db:
            timestamp = now_iso()
            apply_generation_input_selected_image_order(
                db,
                input_version_id,
                [int(image_id) for image_id in styling_plan.get("selected_hero_image_ids", [])],
            )
            cursor = db.execute(
                """
                INSERT INTO generation_stylings
                  (task_id, input_version_id, source_material_ids_json, candidate_material_ids_json,
                   candidate_retrieval_snapshot_json, user_requirement_json, user_instruction_json,
                   selected_material_ids_json, selected_material_snapshot_json,
                   styling_plan_json, hero_understanding_json, outfit_candidates_json, critic_scores_json,
                   rejected_candidates_json, reason_summary, warnings_json, status, attempt_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUCCEEDED', ?, ?, ?)
                """,
                (
                    task_id,
                    input_version_id,
                    dumps(source_ids),
                    dumps(candidate_snapshot.get("candidate_material_ids") or []),
                    dumps(candidate_snapshot),
                    dumps(user_requirement_context),
                    dumps(user_instruction_context),
                    dumps(selected_ids),
                    dumps(selected_snapshot),
                    dumps(styling_plan),
                    dumps(styling_plan.get("hero_understanding") or {}),
                    dumps(styling_plan.get("outfit_candidates") or []),
                    dumps(get_selected_outfit_scores(styling_plan)),
                    dumps(styling_plan.get("rejected_candidates") or []),
                    str(styling_plan.get("reason_summary") or "已根据主推款和搭配服装标签完成 AI Stylist 造型方案。"),
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
                UPDATE generation_previews
                SET status = 'SUPERSEDED', updated_at = ?
                WHERE task_id = ? AND status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')
                """,
                (timestamp, task_id),
            )
            supersede_generation_result_sets(db, task_id, timestamp)
            db.execute(
                """
                UPDATE generation_tasks
                SET status = 'STYLING_READY',
                    current_input_version_id = ?,
                    active_styling_id = ?,
                    active_storyboard_preview_id = NULL,
                    active_preview_id = NULL,
                    active_result_set_id = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (input_version_id, styling_id, timestamp, task_id),
            )
    except Exception as exc:
        return fail_styling_attempt(exc)
    progress.finish()
    finish_attempt(attempt_id, "SUCCEEDED", response_snapshot=styling_plan)
    return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}


@app.post("/api/generation-tasks/{task_id}/preview")
async def create_generation_preview(task_id: int, request: Request, payload: GenerationStageInstructionRequest | None = None) -> dict[str, Any]:
    return await run_create_generation_preview(
        task_id,
        include_debug=admin_debug_for_request(request),
        trigger="manual_step",
        user_instruction=payload.userInstruction if payload else None,
    )


async def run_create_generation_preview(task_id: int, include_debug: bool = False, trigger: str = "manual_step", user_instruction: str | None = None) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    styling_id = task["active_styling_id"]
    if not styling_id or (
        task["status"] not in {"STYLING_READY", "PREVIEW_READY", "COMPLETED", "FINAL_PARTIAL", "CANCELLED"}
        and not (task["status"] == "FAILED" and not task["active_storyboard_preview_id"])
    ):
        raise HTTPException(status_code=409, detail="请先完成 AI 智能选搭")
    input_version_id = create_generation_input_version(task_id)
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET status = 'PREVIEW_RUNNING', updated_at = ? WHERE id = ?", (now_iso(), task_id))
    timestamp = now_iso()
    effective_user_instruction = user_instruction if user_instruction is not None else pending_generation_task_stage_instruction(task, "storyboard_preview")
    user_instruction_context = build_stage_user_instruction_context("storyboard_preview", effective_user_instruction)
    # 环境信息只在分镜预览这一步进入并锁定：自动引用当前维护好的「拍摄环境参考图」（0..1 张）。
    environment_asset_id = get_current_shoot_environment_asset_id()
    with get_db() as db:
        if user_instruction is not None:
            persist_generation_task_stage_instruction(db, task_id, "storyboard_preview", effective_user_instruction, timestamp)
        cursor = db.execute(
            """
            INSERT INTO generation_previews (task_id, styling_id, input_version_id, user_instruction_json, environment_asset_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?)
            """,
            (task_id, styling_id, input_version_id, dumps(user_instruction_context), environment_asset_id, timestamp, timestamp),
        )
        preview_id = int(cursor.lastrowid)
    attempt_id = create_attempt("generation_preview", preview_id, "image-2")
    lease_generation_preview(preview_id, attempt_id)
    schedule_background_generation(
        run_preview_generation(task_id, int(styling_id), preview_id, attempt_id, trigger=trigger),
        job_key=preview_job_key(preview_id),
    )
    return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}


async def run_preview_generation(
    task_id: int,
    styling_id: int,
    preview_id: int,
    attempt_id: int,
    trigger: str = "manual_step",
) -> None:
    try:
        task_package = build_new_preview_task_package(task_id, styling_id, preview_id=preview_id)
        if not task_package.get("inputVersionId"):
            raise RuntimeError("分镜预览缺少输入版本快照")
        task_package["invocation"] = {"mode": trigger, "target": "storyboard_preview"}
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
        finish_attempt(
            attempt_id,
            "FAILED",
            response_snapshot=provider_response,
            error_message=str(exc),
            error_code=classify_generation_attempt_error(exc),
        )
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
            db.execute(
                """
                UPDATE generation_tasks
                SET auto_run_target = NULL, auto_run_requested_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, task_id),
            )
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
        supersede_generation_result_sets(db, task_id, timestamp)
        db.execute(
            """
            UPDATE generation_tasks
            SET status = 'PREVIEW_READY',
                active_preview_id = ?,
                active_storyboard_preview_id = ?,
                current_input_version_id = ?,
                active_result_set_id = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (preview_id, preview_id, task_package.get("inputVersionId"), timestamp, task_id),
        )
    maybe_schedule_auto_run_for_task(task_id)


@app.post("/api/generation-tasks/{task_id}/generate-final")
async def generate_final_images(task_id: int, request: Request, payload: GenerationStageInstructionRequest | None = None) -> dict[str, Any]:
    return await run_generate_final_images(
        task_id,
        include_debug=admin_debug_for_request(request),
        trigger="manual_step",
        continue_existing=False,
        user_instruction=payload.userInstruction if payload else None,
    )


@app.post("/api/generation-tasks/{task_id}/auto-run-final")
async def auto_run_final_images(task_id: int, request: Request, payload: AutoRunFinalRequest | None = None) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    if task["status"] in RUNNING_GENERATION_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="当前任务正在生成中，请先取消或等待完成")
    if task["status"] == "ABANDONED":
        raise HTTPException(status_code=409, detail="当前任务已归档，不能一键成片")
    restart = bool(payload and payload.restart)
    if restart and task["status"] != "COMPLETED":
        raise HTTPException(status_code=409, detail="仅已完成的任务可以重新一键成片")
    timestamp = now_iso()
    with get_db() as db:
        if restart:
            reset_generation_task_for_rerun(db, task_id, timestamp)
        elif task["status"] == "CANCELLED":
            resume_cancelled_generation_task(db, task_id, timestamp)
        elif task["status"] == "FAILED":
            resume_failed_generation_task(db, task_id, timestamp)
        db.execute(
            """
            UPDATE generation_tasks
            SET auto_run_target = ?,
                auto_run_requested_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (AUTO_RUN_TARGET_FINAL, timestamp, timestamp, task_id),
        )
    pump_auto_run_queue()
    return {"item": get_generation_task_by_id(task_id, include_debug=admin_debug_for_request(request))}


@app.post("/api/generation-tasks/{task_id}/cancel-auto-run")
async def cancel_auto_run_final_images(task_id: int, request: Request) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    if row_value(task, "auto_run_target") != AUTO_RUN_TARGET_FINAL:
        raise HTTPException(status_code=409, detail="当前任务不在一键成片队列中")
    if task["status"] in RUNNING_GENERATION_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="当前任务正在生成中，请使用取消生成")
    clear_task_auto_run(task_id)
    pump_auto_run_queue()
    return {"item": get_generation_task_by_id(task_id, include_debug=admin_debug_for_request(request))}


def get_batch_task_rows(batch_id: str) -> list[Any]:
    with get_db() as db:
        return db.execute(
            """
            SELECT * FROM generation_tasks
            WHERE deleted_at IS NULL AND batch_id = ?
            ORDER BY id
            """,
            (batch_id,),
        ).fetchall()


@app.get("/api/generation-batches/{batch_id}/tasks")
def list_generation_batch_tasks(batch_id: str) -> dict[str, Any]:
    rows = get_batch_task_rows(batch_id)
    return {"items": [serialize_generation_task(row, include_detail=False) for row in rows]}


@app.post("/api/generation-batches/{batch_id}/auto-run-final")
async def auto_run_generation_batch(batch_id: str, payload: AutoRunFinalRequest | None = None) -> dict[str, Any]:
    rows = get_batch_task_rows(batch_id)
    if not rows:
        raise HTTPException(status_code=404, detail="批次不存在或没有任务")
    active_rows = [row for row in rows if not row_value(row, "archived_at") and row["status"] != "ABANDONED"]
    restart = bool(payload and payload.restart)
    if restart and (not active_rows or any(row["status"] != "COMPLETED" for row in active_rows)):
        raise HTTPException(status_code=409, detail="仅整批任务全部完成后可以重新一键成片")
    timestamp = now_iso()
    started_ids: list[int] = []
    with get_db() as db:
        for row in active_rows:
            if row["status"] in RUNNING_GENERATION_TASK_STATUSES:
                continue
            if not restart and row["status"] == "COMPLETED":
                continue
            if row_value(row, "auto_run_target") == AUTO_RUN_TARGET_FINAL:
                continue
            if restart:
                reset_generation_task_for_rerun(db, int(row["id"]), timestamp)
            elif row["status"] == "CANCELLED":
                resume_cancelled_generation_task(db, int(row["id"]), timestamp)
            elif row["status"] == "FAILED":
                resume_failed_generation_task(db, int(row["id"]), timestamp)
            db.execute(
                """
                UPDATE generation_tasks
                SET auto_run_target = ?, auto_run_requested_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (AUTO_RUN_TARGET_FINAL, timestamp, timestamp, int(row["id"])),
            )
            started_ids.append(int(row["id"]))
    pump_auto_run_queue()
    refreshed = get_batch_task_rows(batch_id)
    return {
        "startedTaskIds": started_ids,
        "items": [serialize_generation_task(row, include_detail=False) for row in refreshed],
    }


@app.post("/api/generation-batches/{batch_id}/cancel")
def cancel_generation_batch(batch_id: str) -> dict[str, Any]:
    rows = get_batch_task_rows(batch_id)
    if not rows:
        raise HTTPException(status_code=404, detail="批次不存在或没有任务")
    cancelled_ids: list[int] = []
    for row in rows:
        task_id = int(row["id"])
        current = get_generation_task_row(task_id)
        if current["status"] in RUNNING_GENERATION_TASK_STATUSES:
            cancel_generation_task(task_id)
            cancelled_ids.append(task_id)
        elif row_value(current, "auto_run_target") == AUTO_RUN_TARGET_FINAL:
            clear_task_auto_run(task_id)
            cancelled_ids.append(task_id)
    pump_auto_run_queue()
    refreshed = get_batch_task_rows(batch_id)
    return {
        "cancelledTaskIds": cancelled_ids,
        "items": [serialize_generation_task(row, include_detail=False) for row in refreshed],
    }


async def run_generate_final_images(
    task_id: int,
    include_debug: bool = False,
    trigger: str = "manual_step",
    continue_existing: bool = False,
    user_instruction: str | None = None,
) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    if (
        task["status"] not in {"PREVIEW_READY", "COMPLETED", "FINAL_PARTIAL", "CANCELLED"}
        or not task["active_storyboard_preview_id"]
        or not task["active_styling_id"]
    ):
        raise HTTPException(status_code=409, detail="请先生成分镜预览")
    preview_id = int(task["active_storyboard_preview_id"])
    styling_id = int(task["active_styling_id"])
    with get_db() as db:
        preview_row = db.execute(
            "SELECT id FROM generation_previews WHERE id = ? AND task_id = ?",
            (preview_id, task_id),
        ).fetchone()
    if not preview_row:
        raise HTTPException(status_code=409, detail="请先生成分镜预览")
    effective_user_instruction = user_instruction if user_instruction is not None else pending_generation_task_stage_instruction(task, "final_image")
    user_instruction_context = build_stage_user_instruction_context("final_image", effective_user_instruction)
    if continue_existing and task["active_result_set_id"]:
        image_ids = continue_active_final_result_set(
            task_id,
            int(task["active_result_set_id"]),
            styling_id,
            preview_id,
            user_instruction_context,
        )
        if image_ids is not None:
            if image_ids:
                schedule_background_generation(
                    run_final_generation(task_id, int(task["active_result_set_id"]), image_ids, trigger=trigger),
                    job_key=final_set_job_key(int(task["active_result_set_id"])),
                )
            return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}
    input_version_id = create_generation_input_version(task_id)
    timestamp = now_iso()
    with get_db() as db:
        if user_instruction is not None:
            persist_generation_task_stage_instruction(db, task_id, "final_image", effective_user_instruction, timestamp)
        db.execute("UPDATE generation_tasks SET status = 'FINAL_RUNNING', updated_at = ? WHERE id = ?", (timestamp, task_id))
        cursor = db.execute(
            """
            INSERT INTO generation_result_sets (task_id, input_version_id, styling_id, preview_id, user_instruction_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?)
            """,
            (task_id, input_version_id, styling_id, preview_id, dumps(user_instruction_context), timestamp, timestamp),
        )
        result_set_id = int(cursor.lastrowid)
        supersede_generation_result_sets(db, task_id, timestamp, keep_result_set_id=result_set_id)
        db.execute(
            "UPDATE generation_tasks SET current_input_version_id = ?, active_result_set_id = ?, updated_at = ? WHERE id = ?",
            (input_version_id, result_set_id, timestamp, task_id),
        )
        image_ids: list[int] = []
        for sort_order, shot_role in enumerate(final_image_slot_roles(get_styling_plan_by_id(styling_id)), start=1):
            image_cursor = db.execute(
                """
                INSERT INTO generation_result_images_new
                  (result_set_id, shot_role, sort_order, user_instruction_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'QUEUED', ?, ?)
                """,
                (result_set_id, shot_role, sort_order, dumps(user_instruction_context), timestamp, timestamp),
            )
            image_ids.append(int(image_cursor.lastrowid))
    schedule_background_generation(
        run_final_generation(task_id, result_set_id, image_ids, trigger=trigger),
        job_key=final_set_job_key(result_set_id),
    )
    return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}


def continue_active_final_result_set(
    task_id: int,
    result_set_id: int,
    styling_id: int,
    preview_id: int,
    user_instruction_context: dict[str, Any] | None = None,
) -> list[int] | None:
    timestamp = now_iso()
    with get_db() as db:
        result_set = db.execute(
            """
            SELECT * FROM generation_result_sets
            WHERE id = ? AND task_id = ?
            """,
            (result_set_id, task_id),
        ).fetchone()
        if not result_set or result_set["status"] == "RUNNING":
            raise HTTPException(status_code=409, detail="成片已在生成中，请勿重复提交")
        # 结果集自己的 input_version_id 是成片阶段快照，本来就不会等于分镜阶段快照。
        # 是否可续跑只由当前选搭和分镜身份决定；匹配后复用原结果集并跳过成功槽位。
        if int(result_set["styling_id"]) != styling_id or int(result_set["preview_id"]) != preview_id:
            return None
        existing_rows = db.execute(
            "SELECT * FROM generation_result_images_new WHERE result_set_id = ? ORDER BY sort_order",
            (result_set_id,),
        ).fetchall()
        existing_by_sort = {int(row["sort_order"]): row for row in existing_rows}
        image_ids: list[int] = []
        for sort_order, shot_role in enumerate(final_image_slot_roles(get_styling_plan_by_id(styling_id)), start=1):
            existing = existing_by_sort.get(sort_order)
            if existing and existing["status"] == "SUCCEEDED":
                continue
            if existing and existing["status"] in {"QUEUED", "RUNNING"}:
                raise HTTPException(status_code=409, detail="成片已在生成中，请勿重复提交")
            if existing:
                db.execute(
                    """
                    UPDATE generation_result_images_new
                    SET status = 'QUEUED',
                        shot_role = ?,
                        active_attempt_id = NULL,
                        locked_by = NULL,
                        locked_until = NULL,
                        user_instruction_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (shot_role, dumps(user_instruction_context or {}), timestamp, int(existing["id"])),
                )
                image_ids.append(int(existing["id"]))
                continue
            cursor = db.execute(
                """
                INSERT INTO generation_result_images_new
                  (result_set_id, shot_role, sort_order, user_instruction_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'QUEUED', ?, ?)
                """,
                (result_set_id, shot_role, sort_order, dumps(user_instruction_context or {}), timestamp, timestamp),
            )
            image_ids.append(int(cursor.lastrowid))
        if image_ids:
            db.execute(
                "UPDATE generation_result_sets SET status = 'RUNNING', user_instruction_json = ?, updated_at = ? WHERE id = ?",
                (dumps(user_instruction_context or {}), timestamp, result_set_id),
            )
            db.execute(
                "UPDATE generation_tasks SET status = 'FINAL_RUNNING', active_result_set_id = ?, updated_at = ? WHERE id = ?",
                (result_set_id, timestamp, task_id),
            )
        else:
            reconcile_final_result_set(db, result_set_id, task_id)
    return image_ids


async def run_final_generation(
    task_id: int,
    result_set_id: int,
    image_ids: list[int],
    resume_attempts_by_image_id: dict[int, int] | None = None,
    trigger: str = "manual_step",
) -> None:
    resume_attempts_by_image_id = resume_attempts_by_image_id or {}
    # 余额硬停：任一并发成片检测到 INSUFFICIENT_BALANCE 时置位，尚未提交的兄弟图
    # 直接跳过 provider 提交（余额不足重试/继续提交只会继续报错、浪费轮询预算）。
    balance_stop = asyncio.Event()
    unknown_retry_guard = SubmissionUnknownRetryGuard(
        settings.generation_unknown_submission_batch_circuit_breaker
    )
    await asyncio.gather(
        *(
            generate_single_final_image(
                task_id,
                result_set_id,
                image_id,
                attempt_id=resume_attempts_by_image_id.get(image_id),
                trigger=trigger,
                balance_stop=balance_stop,
                unknown_retry_guard=unknown_retry_guard,
            )
            for image_id in image_ids
        ),
        return_exceptions=True,
    )
    with get_db() as db:
        _, task_status = reconcile_final_result_set(db, result_set_id, task_id)
        if task_status in {"COMPLETED", "FINAL_PARTIAL", "FAILED", "CANCELLED"}:
            db.execute(
                """
                UPDATE generation_tasks
                SET auto_run_target = NULL, auto_run_requested_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now_iso(), task_id),
            )
    maybe_schedule_auto_run_for_task(task_id)


def reconcile_final_result_set(db: Any, result_set_id: int, task_id: int | None = None) -> tuple[str, str]:
    row = db.execute(
        """
        SELECT grs.*, gt.status AS task_status, gt.active_result_set_id
        FROM generation_result_sets grs
        JOIN generation_tasks gt ON gt.id = grs.task_id
        WHERE grs.id = ?
        """,
        (result_set_id,),
    ).fetchone()
    if not row:
        return "PARTIAL", "FINAL_PARTIAL"
    if task_id is None:
        task_id = int(row["task_id"])
    if row["status"] == "SUPERSEDED" or row["active_result_set_id"] != result_set_id:
        return row["status"], row["task_status"]
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
async def retry_new_result_image(image_id: int, request: Request, payload: GenerationStageInstructionRequest | None = None) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gri.*, grs.task_id, grs.id AS result_set_id, gt.status AS task_status,
                   gt.final_image_user_instruction_text
            FROM generation_result_images_new gri
            JOIN generation_result_sets grs ON grs.id = gri.result_set_id
            JOIN generation_tasks gt ON gt.id = grs.task_id
            WHERE gri.id = ?
            """,
            (image_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="结果图不存在")
        if row["task_status"] in RUNNING_GENERATION_TASK_STATUSES:
            raise HTTPException(status_code=409, detail="当前任务正在生成中，请先取消或等待完成")
        if row["status"] in {"QUEUED", "RUNNING"}:
            raise HTTPException(status_code=409, detail="当前结果图仍在生成中")
        effective_user_instruction = payload.userInstruction if payload and payload.userInstruction is not None else pending_generation_task_stage_instruction(row, "final_image")
        previous_attempt_id = int(row["active_attempt_id"]) if row["active_attempt_id"] else None
    task_id = int(row["task_id"])
    result_set_id = int(row["result_set_id"])
    input_version_id = create_generation_input_version(task_id)
    timestamp = now_iso()
    user_instruction_context = build_stage_user_instruction_context("final_image", effective_user_instruction)
    attempt_id = create_attempt(
        "generation_result_image",
        image_id,
        "image-2",
        request_snapshot={"invocation": {"mode": "manual_retry", "target": "final_image"}},
    )
    with get_db() as db:
        if previous_attempt_id:
            db.execute(
                "UPDATE generation_attempts SET retry_of_attempt_id = ?, updated_at = ? WHERE id = ?",
                (previous_attempt_id, timestamp, attempt_id),
            )
        if payload and payload.userInstruction is not None:
            persist_generation_task_stage_instruction(db, task_id, "final_image", effective_user_instruction, timestamp)
        db.execute(
            """
            UPDATE generation_result_images_new
            SET status = 'QUEUED', active_attempt_id = ?,
                locked_by = NULL, locked_until = NULL,
                user_instruction_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (attempt_id, dumps(user_instruction_context), timestamp, image_id),
        )
        db.execute(
            "UPDATE generation_result_sets SET status = 'RUNNING', input_version_id = ?, user_instruction_json = ?, updated_at = ? WHERE id = ?",
            (input_version_id, dumps(user_instruction_context), timestamp, result_set_id),
        )
        db.execute(
            "UPDATE generation_tasks SET status = 'FINAL_RUNNING', current_input_version_id = ?, active_result_set_id = ?, updated_at = ? WHERE id = ?",
            (input_version_id, result_set_id, timestamp, task_id),
        )
    schedule_background_generation(
        run_final_generation(
            task_id,
            result_set_id,
            [image_id],
            resume_attempts_by_image_id={image_id: attempt_id},
            trigger="manual_retry",
        ),
        job_key=final_set_job_key(result_set_id),
    )
    return {"item": get_generation_task_by_id(task_id, include_debug=admin_debug_for_request(request))}


@app.post("/api/generation-result-images-new/{image_id}/redeliver")
async def redeliver_new_result_image(image_id: int, request: Request) -> dict[str, Any]:
    """重新回传：provider 已生成成功、仅回传失败的成片，零成本重新拉取结果。

    与 /retry（重新生成）的区别：不创建新的 provider 任务、不消耗生成额度。
    新 attempt 复制上一次的 providerRequestId 和响应快照，直接走恢复路径——
    先确认 provider 状态，再执行回传。
    """
    task_id, result_set_id, attempt_id = queue_result_image_redelivery(image_id)
    schedule_background_generation(
        run_final_generation(
            task_id,
            result_set_id,
            [image_id],
            resume_attempts_by_image_id={image_id: attempt_id},
            trigger="manual_redeliver",
        ),
        job_key=final_set_job_key(result_set_id),
    )
    return {"item": get_generation_task_by_id(task_id, include_debug=admin_debug_for_request(request))}


def queue_result_image_redelivery(image_id: int) -> tuple[int, int, int]:
    """校验并把回传失败的成片重新排队；返回 (task_id, result_set_id, attempt_id)。"""
    with get_db() as db:
        row = db.execute(
            """
            SELECT gri.*, grs.task_id, grs.id AS result_set_id, gt.status AS task_status
            FROM generation_result_images_new gri
            JOIN generation_result_sets grs ON grs.id = gri.result_set_id
            JOIN generation_tasks gt ON gt.id = grs.task_id
            WHERE gri.id = ?
            """,
            (image_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="结果图不存在")
        if row["task_status"] in RUNNING_GENERATION_TASK_STATUSES:
            raise HTTPException(status_code=409, detail="当前任务正在生成中，请先取消或等待完成")
        if row["status"] in {"QUEUED", "RUNNING"}:
            raise HTTPException(status_code=409, detail="当前结果图仍在生成中")
        if row["status"] != "FAILED":
            raise HTTPException(status_code=409, detail="只有失败的成片才能重新回传")
        last_attempt = (
            db.execute("SELECT * FROM generation_attempts WHERE id = ?", (row["active_attempt_id"],)).fetchone()
            if row["active_attempt_id"]
            else None
        )
    if not last_attempt or not last_attempt["provider_request_id"]:
        raise HTTPException(status_code=409, detail="该成片没有可回传的 provider 任务，请使用重新生成")
    task_id = int(row["task_id"])
    result_set_id = int(row["result_set_id"])
    attempt_id = create_attempt(
        "generation_result_image",
        image_id,
        str(last_attempt["provider"]),
        request_snapshot=loads(last_attempt["request_snapshot_json"], {}),
    )
    timestamp = now_iso()
    with get_db() as db:
        request_snapshot = loads(last_attempt["request_snapshot_json"], {})
        if isinstance(request_snapshot, dict):
            request_snapshot = {**request_snapshot, "invocation": {"mode": "manual_redeliver", "target": "final_image"}}
        db.execute(
            """
            UPDATE generation_attempts
            SET provider_request_id = ?, response_snapshot_json = ?, retry_of_attempt_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                last_attempt["provider_request_id"],
                last_attempt["response_snapshot_json"],
                int(last_attempt["id"]),
                timestamp,
                attempt_id,
            ),
        )
        db.execute(
            "UPDATE generation_attempts SET request_snapshot_json = ? WHERE id = ?",
            (dumps(request_snapshot), attempt_id),
        )
        db.execute(
            """
            UPDATE generation_result_images_new
            SET status = 'QUEUED', active_attempt_id = ?, locked_by = NULL, locked_until = NULL, updated_at = ?
            WHERE id = ?
            """,
            (attempt_id, timestamp, image_id),
        )
        db.execute("UPDATE generation_result_sets SET status = 'RUNNING', updated_at = ? WHERE id = ?", (timestamp, result_set_id))
        db.execute(
            "UPDATE generation_tasks SET status = 'FINAL_RUNNING', active_result_set_id = ?, updated_at = ? WHERE id = ?",
            (result_set_id, timestamp, task_id),
        )
    return task_id, result_set_id, attempt_id


@app.get("/api/generation-records")
def list_new_generation_records(
    include_archived: bool = Query(False, alias="includeArchived"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    keyword: str | None = Query(None),
    status: list[str] | None = Query(None),
    created_from: str | None = Query(None, alias="createdFrom"),
    created_to: str | None = Query(None, alias="createdTo"),
    sort_by: str = Query("createdAt", alias="sortBy"),
    sort_dir: str = Query("desc", alias="sortDir"),
) -> dict[str, Any]:
    status_values = split_filter_values(status)
    created_from_dt = parse_filter_datetime(created_from, end_of_day=False)
    created_to_dt = parse_filter_datetime(created_to, end_of_day=True)
    where_clauses = ["gt.deleted_at IS NULL", "(? OR gt.archived_at IS NULL)"]
    query_params: list[Any] = [include_archived]
    keyword_search = f"%{keyword.strip()}%" if keyword and keyword.strip() else None
    if keyword_search:
        where_clauses.append("(gt.name LIKE ? OR gt.status LIKE ? OR gt.batch_id LIKE ?)")
        query_params.extend([keyword_search] * 3)
    if status_values:
        placeholders = ", ".join("?" for _ in status_values)
        where_clauses.append(f"LOWER(gt.status) IN ({placeholders})")
        query_params.extend(sorted(status_values))
    if created_from_dt:
        where_clauses.append("gt.created_at >= ?")
        query_params.append(created_from_dt.isoformat())
    if created_to_dt:
        where_clauses.append("gt.created_at <= ?")
        query_params.append(created_to_dt.isoformat())
    sort_column = {
        "createdAt": "gt.created_at",
        "updatedAt": "gt.updated_at",
        "name": "gt.name",
        "status": "gt.status",
    }.get(sort_by, "gt.created_at")
    direction = "ASC" if sort_dir == "asc" else "DESC"
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT * FROM generation_tasks gt
            WHERE {" AND ".join(where_clauses)}
            ORDER BY {sort_column} {direction}, gt.id {direction}
            LIMIT ? OFFSET ?
            """,
            [*query_params, limit + 1, offset],
        ).fetchall()
        total = int(db.execute(
            f"""
            SELECT COUNT(*) AS total FROM generation_tasks gt
            WHERE {" AND ".join(where_clauses)}
            """,
            query_params,
        ).fetchone()["total"])
        status_rows = db.execute(
            f"""
            SELECT gt.status
            FROM generation_tasks gt
            WHERE {" AND ".join(where_clauses)}
            """,
            query_params,
        ).fetchall()
        stable_status_rows = db.execute(
            """
            SELECT gt.status
            FROM generation_tasks gt
            WHERE gt.deleted_at IS NULL AND (? OR gt.archived_at IS NULL)
            """,
            (include_archived,),
        ).fetchall()
        items = rows[:limit]
        return {
            "items": [serialize_generation_task(row, include_detail=False) for row in items],
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit if offset + limit < total else None,
            "hasMore": offset + limit < total,
            "total": total,
            "totalPages": (total + limit - 1) // limit if total else 0,
            "facets": {
                "status": facet_options(row["status"] for row in status_rows),
            },
            "stableFacets": {
                "status": facet_options(row["status"] for row in stable_status_rows),
            },
        }


@app.get("/api/generation-records/{task_id}")
def get_new_generation_record(task_id: int, request: Request, include_debug: bool = Query(False, alias="includeDebug")) -> dict[str, Any]:
    return {"item": get_generation_task_by_id(task_id, include_debug=require_admin_debug(request, include_debug))}


# ---------------------------------------------------------------------------
# 成片批量下载：预检 + 按任务名分文件夹流式打包
# ---------------------------------------------------------------------------

FINAL_IMAGE_EXPORT_MAX_TASKS = 50
FINAL_IMAGE_EXPORT_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
FINAL_IMAGE_EXPORT_STATUS_TEXT = {
    "FAILED": "生成失败",
    "RUNNING": "生成中",
    "QUEUED": "排队中",
    "CANCELLED": "已取消",
    "MISSING_ASSET": "缺少成片文件",
}


class FinalImageExportPrecheckRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


def normalize_final_image_export_ids(raw_ids: list[int]) -> list[int]:
    ids: list[int] = []
    for value in raw_ids:
        task_id = int(value)
        if task_id > 0 and task_id not in ids:
            ids.append(task_id)
    if not ids:
        raise HTTPException(status_code=400, detail="请先选择要下载的任务")
    if len(ids) > FINAL_IMAGE_EXPORT_MAX_TASKS:
        raise HTTPException(status_code=400, detail=f"单次最多下载 {FINAL_IMAGE_EXPORT_MAX_TASKS} 个任务")
    return ids


def final_image_export_shot_label(shot_role: str | None) -> str:
    return str(shot_role or "").strip()


def get_final_image_export_overview(db: Any, task_id: int) -> dict[str, Any] | None:
    """收集单个任务当前 active 结果集的可下载成片和缺失/失败明细。"""
    task = db.execute(
        "SELECT id, name, status, active_result_set_id FROM generation_tasks WHERE id = ? AND deleted_at IS NULL",
        (task_id,),
    ).fetchone()
    if not task:
        return None
    downloadable: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    total_count = 0
    if task["active_result_set_id"]:
        rows = db.execute(
            """
            SELECT gri.sort_order, gri.status, gri.shot_role,
                   ma.object_key, ma.mime_type, ma.original_filename, ma.size_bytes
            FROM generation_result_images_new gri
            LEFT JOIN media_assets ma ON ma.id = gri.current_asset_id AND ma.deleted_at IS NULL
            WHERE gri.result_set_id = ?
            ORDER BY gri.sort_order
            """,
            (int(task["active_result_set_id"]),),
        ).fetchall()
        total_count = len(rows)
        for row in rows:
            label = final_image_export_shot_label(row["shot_role"])
            if row["status"] == "SUCCEEDED" and row["object_key"]:
                downloadable.append(
                    {
                        "sortOrder": int(row["sort_order"]),
                        "shotLabel": label,
                        "objectKey": row["object_key"],
                        "mimeType": row["mime_type"],
                        "originalFilename": row["original_filename"],
                        "sizeBytes": int(row["size_bytes"] or 0),
                    }
                )
            else:
                issues.append(
                    {
                        "sortOrder": int(row["sort_order"]),
                        "shotLabel": label,
                        "status": "MISSING_ASSET" if row["status"] == "SUCCEEDED" else str(row["status"]),
                    }
                )
    return {
        "taskId": int(task["id"]),
        "taskName": task["name"],
        "taskStatus": task["status"],
        "hasResultSet": bool(task["active_result_set_id"]),
        "downloadable": downloadable,
        "issues": issues,
        "totalCount": total_count,
    }


def collect_final_image_export_overviews(task_ids: list[int]) -> list[dict[str, Any]]:
    overviews: list[dict[str, Any]] = []
    with get_db() as db:
        for task_id in task_ids:
            overview = get_final_image_export_overview(db, task_id)
            if not overview:
                raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
            overviews.append(overview)
    return overviews


@app.post("/api/generation-task-exports/precheck")
def precheck_final_image_export(payload: FinalImageExportPrecheckRequest) -> dict[str, Any]:
    task_ids = normalize_final_image_export_ids(payload.ids)
    overviews = collect_final_image_export_overviews(task_ids)
    items = [
        {
            "taskId": item["taskId"],
            "taskName": item["taskName"],
            "taskStatus": item["taskStatus"],
            "hasResultSet": item["hasResultSet"],
            "downloadableCount": len(item["downloadable"]),
            "totalCount": item["totalCount"],
            "totalBytes": sum(entry["sizeBytes"] for entry in item["downloadable"]),
            "issues": item["issues"],
        }
        for item in overviews
    ]
    return {
        "items": items,
        "downloadableTaskIds": [item["taskId"] for item in items if item["downloadableCount"] > 0],
        "completeTaskIds": [item["taskId"] for item in items if item["downloadableCount"] > 0 and not item["issues"]],
        "totalImages": sum(item["downloadableCount"] for item in items),
        "totalBytes": sum(item["totalBytes"] for item in items),
    }


def final_image_export_extension(mime_type: str | None, original_filename: str | None) -> str:
    if mime_type and mime_type in FINAL_IMAGE_EXPORT_EXTENSIONS:
        return FINAL_IMAGE_EXPORT_EXTENSIONS[mime_type]
    suffix = Path(original_filename or "").suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return suffix
    return ".jpg"


FINAL_IMAGE_EXPORT_INVALID_CHARS = set('\\/:*?"<>|')


def final_image_export_safe_component(value: str | None) -> str:
    """保留任务名原貌，只把文件系统非法字符替换为下划线；去掉尾部空格和点（Windows 限制）。"""
    cleaned = "".join(
        "_" if ch in FINAL_IMAGE_EXPORT_INVALID_CHARS or ord(ch) < 32 else ch
        for ch in str(value or "").strip()
    )
    return cleaned.rstrip(" .")


def assign_final_image_export_folders(tasks: list[dict[str, Any]]) -> None:
    """文件夹名与任务名保持一致；同名任务全部保留，从第二个起追加 (2)、(3) 序号。"""
    used: set[str] = {"清单.txt"}
    for task in tasks:
        base = final_image_export_safe_component(task["taskName"]) or f"任务{task['taskId']}"
        folder = base
        serial = 1
        while folder in used:
            serial += 1
            folder = f"{base}({serial})"
        used.add(folder)
        task["folder"] = folder


class _ZipStreamBuffer:
    """给 zipfile 的只写缓冲：writestr 写入的字节被 drain 成 HTTP chunk 流出。"""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        if data:
            self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def drain(self) -> list[bytes]:
        chunks, self._chunks = self._chunks, []
        return chunks


def iter_final_image_export_zip(tasks: list[dict[str, Any]], exported_at: str) -> Any:
    """流式生成 zip：图片用 STORE 不再压缩，单文件读取失败跳过并写入清单。"""
    buffer = _ZipStreamBuffer()
    date_time = datetime.now().timetuple()[:6]
    manifest: list[str] = [
        "Ruyi-Dream 成片批量下载清单",
        f"导出时间：{exported_at}",
        f"任务数：{len(tasks)}",
        "",
    ]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for task in tasks:
            folder = task["folder"]
            included = 0
            notes: list[str] = []
            for entry in task["downloadable"]:
                try:
                    content = get_bytes(entry["objectKey"])
                except Exception:  # noqa: BLE001 - 单张读取失败不应中断整个压缩包
                    logger.exception("批量下载读取成片失败 object_key=%s", entry["objectKey"])
                    notes.append(f"  - #{entry['sortOrder']} {entry['shotLabel']}：读取成片文件失败，未包含在压缩包中")
                    continue
                filename = f"z({entry['sortOrder']}){final_image_export_extension(entry['mimeType'], entry['originalFilename'])}"
                info = zipfile.ZipInfo(f"{folder}/{filename}", date_time=date_time)
                archive.writestr(info, content)
                included += 1
                yield from buffer.drain()
            manifest.append(f"{task['taskName']}（任务 ID {task['taskId']}）")
            manifest.append(f"  已包含成片：{included} 张，文件夹：{folder}/")
            if not task["hasResultSet"]:
                manifest.append("  - 该任务尚未生成成片")
            for issue in task["issues"]:
                status_text = FINAL_IMAGE_EXPORT_STATUS_TEXT.get(str(issue["status"]), str(issue["status"]))
                manifest.append(f"  - #{issue['sortOrder']} {issue['shotLabel']}：{status_text}，未包含在压缩包中")
            manifest.extend(notes)
            manifest.append("")
        info = zipfile.ZipInfo("清单.txt", date_time=date_time)
        archive.writestr(info, "\n".join(manifest).encode("utf-8"))
    yield from buffer.drain()


@app.get("/api/generation-task-exports/archive")
def download_final_image_archive(ids: str = Query(...)) -> StreamingResponse:
    raw_ids: list[int] = []
    for part in ids.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            raw_ids.append(int(part))
        except ValueError as error:
            raise HTTPException(status_code=400, detail="任务 ID 格式不正确") from error
    task_ids = normalize_final_image_export_ids(raw_ids)
    tasks = collect_final_image_export_overviews(task_ids)
    total_images = sum(len(task["downloadable"]) for task in tasks)
    if not total_images:
        raise HTTPException(status_code=400, detail="所选任务没有可下载的成片")
    assign_final_image_export_folders(tasks)
    with get_db() as db:
        log_event(
            db,
            "generation_task_export",
            None,
            "final_images_exported",
            {"taskIds": task_ids, "imageCount": total_images},
        )
    exported_at = now_iso()
    timestamp = exported_at[:19].replace("-", "").replace(":", "").replace("T", "-")
    filename = f"final-images-{timestamp}.zip"
    return StreamingResponse(
        iter_final_image_export_zip(tasks, exported_at),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


@app.post("/api/prompt-configs/{config_key}/preview")
def preview_prompt_config_draft(config_key: str, payload: PromptConfigPreviewRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    key = normalize_prompt_config_key(config_key)
    if key not in get_prompt_config_rows():
        raise HTTPException(status_code=404, detail="Prompt 配置不存在")
    runtime = sample_prompt_runtime()
    historical_prompt = None
    if payload.attemptId:
        with get_db() as db:
            attempt = db.execute("SELECT request_snapshot_json FROM generation_attempts WHERE id = ?", (payload.attemptId,)).fetchone()
        if not attempt:
            raise HTTPException(status_code=404, detail="调用记录不存在")
        facts = loads(attempt["request_snapshot_json"], {})
        prompt_keys = facts.get("promptConfigKeys") if isinstance(facts.get("promptConfigKeys"), list) else []
        if facts.get("promptConfigKey") != key and key not in prompt_keys:
            raise HTTPException(status_code=400, detail="调用记录与 Prompt 配置不匹配")
        if isinstance(facts.get("promptRuntime"), dict):
            runtime = facts["promptRuntime"]
        historical_prompt = facts.get("prompt")
    rendered, diagnostics = render_prompt_config(
        key,
        runtime,
        content_overrides={key: payload.content[:120000]},
    )
    return {"rendered": rendered, "historicalPrompt": historical_prompt, "diagnostics": diagnostics}


@app.get("/api/prompt-configs/{config_key}/invocations")
def list_prompt_config_invocations(config_key: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    key = normalize_prompt_config_key(config_key)
    with get_db() as db:
        rows = db.execute(
            "SELECT id, owner_type, provider, status, created_at FROM generation_attempts WHERE request_snapshot_json LIKE ? ORDER BY created_at DESC LIMIT 30",
            (f'%"{key}"%',),
        ).fetchall()
    return {"items": [{"id": row["id"], "ownerType": row["owner_type"], "provider": row["provider"], "status": row["status"], "createdAt": row["created_at"]} for row in rows]}


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
def create_model_profile(request: Request, payload: ModelProfileUpsertRequest) -> dict[str, Any]:
    require_admin(request)
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
def update_model_profile(model_profile_id: int, request: Request, payload: ModelProfileUpsertRequest) -> dict[str, Any]:
    require_admin(request)
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
def delete_model_profile(model_profile_id: int, request: Request) -> dict[str, Any]:
    require_admin(request)
    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (model_profile_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="数字模特不存在")
        model_count = db.execute("SELECT COUNT(*) AS count FROM model_profiles").fetchone()["count"]
        if model_count <= 1:
            raise HTTPException(status_code=409, detail="至少保留一个数字模特")
        usage_count = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM generation_task_input_versions
            WHERE model_snapshot_json LIKE ? OR model_snapshot_json LIKE ?
            """,
            (f'%"id":{model_profile_id}%', f'%"id": {model_profile_id}%'),
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

    if not model_asset_dir_is_referenced(asset_dir):
        delete_prefix(asset_dir)
    return {"ok": True}


@app.post("/api/model-profiles/{model_profile_id}/head-references")
async def add_model_head_reference(
    model_profile_id: int,
    request: Request,
    angle: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    file: UploadFile | None = File(None),
) -> dict[str, Any]:
    require_admin(request)
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
                create_normalized_image_derivatives(key, content)
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
def update_model_head_reference(model_profile_id: int, reference_id: str, request: Request, payload: HeadReferenceUpdateRequest) -> dict[str, Any]:
    require_admin(request)
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
def delete_model_head_reference(model_profile_id: int, reference_id: str, request: Request) -> dict[str, Any]:
    require_admin(request)
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


@app.post("/api/model-profiles/{model_profile_id}/head-references/{reference_id}/replace")
async def replace_model_head_reference(
    model_profile_id: int,
    reference_id: str,
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    require_admin(request)
    clean_reference_id = sanitize_reference_id(reference_id)
    if not clean_reference_id:
        raise HTTPException(status_code=400, detail="头像标识不能为空")
    content, content_type = await validate_upload_image(file)
    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (model_profile_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="数字模特不存在")
        asset_dir = clean_key(row["asset_dir"])
        profile = loads(row["profile_json"], {})
        references = dict(profile.get("headReferences") or {})
        if clean_reference_id not in references:
            raise HTTPException(status_code=404, detail="头像不存在")
        old_filename = references.get(clean_reference_id)
        safe_name = sanitize_filename(file.filename or "head-reference")
        key = unique_key(join_key(asset_dir, safe_name))
        try:
            put_fileobj(key, BytesIO(content), content_type)
            create_normalized_image_derivatives(key, content)
            references[clean_reference_id] = Path(key).name
            profile["headReferences"] = references
            db.execute(
                "UPDATE model_profiles SET profile_json = ?, updated_at = ? WHERE id = ?",
                (dumps(normalize_model_profile(profile)), now_iso(), model_profile_id),
            )
            log_event(db, "model_profile", model_profile_id, "head_reference_replaced", {"referenceId": clean_reference_id, "filename": Path(key).name})
        except Exception:
            cleanup_objects(image_object_keys(key))
            raise
    remove_model_asset_file(asset_dir, old_filename)
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


def get_current_shoot_environment_asset_id() -> int | None:
    with get_db() as db:
        row = db.execute(
            "SELECT media_asset_id FROM shoot_environment_references ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return int(row["media_asset_id"]) if row else None


def serialize_shoot_environment() -> dict[str, Any]:
    asset_id = get_current_shoot_environment_asset_id()
    return {"asset": get_media_asset_by_id(asset_id) if asset_id else None}


@app.get("/api/shoot-environment")
def get_shoot_environment() -> dict[str, Any]:
    return serialize_shoot_environment()


@app.post("/api/shoot-environment")
async def set_shoot_environment(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    require_admin(request)
    assets = await save_uploads_as_media_assets([file], join_key("shoot-environment", uuid4().hex[:8]))
    if not assets:
        raise HTTPException(status_code=400, detail="拍摄环境参考图上传失败")
    new_asset_id = int(assets[0]["id"])
    timestamp = now_iso()
    with get_db() as db:
        old_ids = [int(r["media_asset_id"]) for r in db.execute("SELECT media_asset_id FROM shoot_environment_references").fetchall()]
        db.execute("DELETE FROM shoot_environment_references")
        db.execute(
            "INSERT INTO shoot_environment_references (media_asset_id, created_at, updated_at) VALUES (?, ?, ?)",
            (new_asset_id, timestamp, timestamp),
        )
        for old_id in old_ids:
            if old_id != new_asset_id:
                release_media_asset_if_unreferenced(db, old_id)
    return serialize_shoot_environment()


@app.delete("/api/shoot-environment")
def clear_shoot_environment(request: Request) -> dict[str, Any]:
    require_admin(request)
    with get_db() as db:
        old_ids = [int(r["media_asset_id"]) for r in db.execute("SELECT media_asset_id FROM shoot_environment_references").fetchall()]
        db.execute("DELETE FROM shoot_environment_references")
        for old_id in old_ids:
            release_media_asset_if_unreferenced(db, old_id)
    return serialize_shoot_environment()


@app.get("/api/media")
def get_media(key: str = Query(...), variant: str = Query("original"), download: bool = Query(False)) -> RedirectResponse:
    object_key = clean_key(key)
    if variant not in {"thumb", "preview", "original"}:
        raise HTTPException(status_code=400, detail="图片规格不合法")
    media_keys = registered_media_keys(object_key)
    if not media_keys:
        raise HTTPException(status_code=404, detail="图片不存在")
    object_key = media_keys["object_key"]
    target_key = object_key
    if not download and variant in {"thumb", "preview"}:
        registered_variant_key = media_keys.get(f"{variant}_key")
        if registered_variant_key and object_exists(registered_variant_key):
            target_key = registered_variant_key
        else:
            target_key = existing_derivative_or_original(object_key, variant) or object_key
    if not object_exists(target_key):
        raise HTTPException(status_code=404, detail="图片不存在")
    cache_control = media_cache_control(object_key, variant, download)
    disposition = media_attachment_disposition(media_keys.get("original_filename"), object_key) if download else None
    url = presigned_get_url(
        target_key,
        response_content_disposition=disposition,
        response_content_type=media_keys.get("mime_type") if download else None,
        response_cache_control=cache_control,
        expires_seconds=settings.media_presign_expires_seconds,
    )
    if not url:
        raise HTTPException(status_code=404, detail="图片不存在")
    return RedirectResponse(
        url,
        status_code=302,
        headers={"Cache-Control": cache_control},
    )


def media_cache_control(object_key: str, variant: str, download: bool) -> str:
    if download:
        return "private, no-store"
    return f"private, max-age={media_cache_max_age(object_key, variant)}"


def media_cache_max_age(object_key: str, variant: str) -> int:
    base_ttl = max(0, int(MEDIA_CACHE_VARIANT_BASE_SECONDS.get(variant, settings.media_original_cache_seconds)))
    jitter_seconds = max(0, int(settings.media_cache_jitter_seconds))
    safe_ttl = max(0, int(settings.media_presign_expires_seconds) - max(0, int(settings.media_presign_safety_margin_seconds)))
    if base_ttl <= 0 or safe_ttl <= 0:
        return 0
    jitter = stable_media_cache_jitter(object_key, variant, jitter_seconds)
    return min(base_ttl + jitter, safe_ttl)


def stable_media_cache_jitter(object_key: str, variant: str, jitter_seconds: int) -> int:
    if jitter_seconds <= 0:
        return 0
    digest = hashlib.sha256(f"{variant}:{clean_key(object_key)}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % (jitter_seconds + 1)


def media_attachment_disposition(original_filename: str | None, object_key: str) -> str:
    filename = sanitize_filename(original_filename or Path(object_key).name or "image")
    suffix = Path(filename).suffix
    ascii_stem = Path(filename).stem.encode("ascii", "ignore").decode().strip("-_") or "image"
    ascii_filename = f"{ascii_stem}{suffix}"
    return f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


def registered_media_keys(object_key: str) -> dict[str, str | None] | None:
    with get_db() as db:
        asset = db.execute(
            """
            SELECT object_key, thumb_key, preview_key, original_filename, mime_type FROM media_assets
            WHERE object_key = ? OR thumb_key = ? OR preview_key = ?
            """,
            (object_key, object_key, object_key),
        ).fetchone()
        if asset:
            return {
                "object_key": clean_key(asset["object_key"]),
                "thumb_key": clean_key(asset["thumb_key"]) if asset["thumb_key"] else None,
                "preview_key": clean_key(asset["preview_key"]) if asset["preview_key"] else None,
                "original_filename": asset["original_filename"],
                "mime_type": asset["mime_type"],
            }
        model_reference = registered_model_reference_keys(db, object_key)
        if model_reference:
            return model_reference
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
                    "original_filename": str(filename),
                    "mime_type": None,
                }
    return None


@app.get("/api/logs")
def list_logs(limit: int = 100) -> dict[str, Any]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM event_logs ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return {"items": [serialize_log(row) for row in rows]}


def future_iso(seconds: int) -> str:
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def lock_until_iso() -> str:
    return future_iso(settings.generation_lock_ttl_seconds)


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


def normalize_user_input_text(value: str | None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:MAX_USER_INPUT_CHARS]


def normalize_user_requirement_text(value: str | None) -> str:
    return normalize_user_input_text(value)


def build_stage_user_instruction_context(stage: str, instruction: str | None = None) -> dict[str, Any]:
    text = normalize_user_input_text(instruction)
    # 只保留可变数据（instruction）。优先级/不可覆盖边界由 Prompt 维护模板正文声明。
    return {
        "schema_version": "stage-user-instruction-v1",
        "stage": stage,
        "instruction": text,
    }


def build_user_requirement_context(task: Any, styling_adjustment: str | None = None) -> dict[str, Any]:
    adjustment = normalize_user_input_text(styling_adjustment)
    combined_parts = [part for part in (adjustment,) if part]
    # 只保留可变数据（合并后的用户要求）。优先级/不可覆盖边界由 Prompt 维护模板正文声明。
    return {
        "schema_version": "user-requirement-v1",
        "styling_adjustment": adjustment,
        "combined_requirement": "；".join(combined_parts),
    }




async def generation_poller_loop() -> None:
    while True:
        await asyncio.sleep(settings.generation_poll_interval_seconds)
        try:
            refresh_pending_new_generation_jobs()
        except Exception as exc:  # pragma: no cover - background safety net
            with get_db() as db:
                log_event(db, "generation_poller", None, "failed", {"error": str(exc)})


def get_model_profile_by_id(model_profile_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (model_profile_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="数字模特不存在")
        return serialize_model(row)


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


def insert_media_asset_record(asset: dict[str, Any], timestamp: str | None = None) -> int:
    timestamp = timestamp or now_iso()
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
        return int(cursor.lastrowid)


def process_uploaded_content_as_media_asset(content: bytes, content_type: str, filename: str, key: str, timestamp: str) -> dict[str, Any]:
    inspected = inspect_image(content, filename, settings.max_upload_image_pixels)
    try:
        normalized_key, derivative = create_normalized_image_derivatives(key, content)
        width, height = inspected.width, inspected.height
        asset = {
            "objectKey": key,
            "normalizedKey": normalized_key,
            "thumbKey": derivative.get("thumb"),
            "previewKey": derivative.get("preview"),
            "originalFilename": filename,
            "mimeType": content_type,
            "width": width,
            "height": height,
            "sizeBytes": len(content),
            "thumbSizeBytes": derivative.get("thumbSize"),
            "previewSizeBytes": derivative.get("previewSize"),
            "hash": hashlib.sha256(content).hexdigest(),
        }
        asset["id"] = insert_media_asset_record(asset, timestamp)
        return asset
    except Exception:
        cleanup_objects(image_object_keys(key))
        raise


def process_uploaded_object_as_media_asset(upload: UploadedMediaRequest, timestamp: str) -> dict[str, Any]:
    object_key = ensure_direct_upload_key(upload.objectKey)
    filename = sanitize_filename(upload.filename or Path(object_key).name or "image")
    head = object_head(object_key)
    if not head:
        raise HTTPException(status_code=400, detail=f"{filename} 尚未上传完成")
    object_size_bytes = int(head.get("contentLength") or 0)
    if object_size_bytes <= 0:
        raise HTTPException(status_code=400, detail=f"{filename} 是空文件")
    if object_size_bytes > settings.max_upload_image_bytes:
        max_mb = settings.max_upload_image_bytes / (1024 * 1024)
        raise HTTPException(status_code=400, detail=f"{filename} 超过 {max_mb:.0f}MB 大小限制")
    content = get_bytes(object_key)
    content_type, _, _ = validate_image_content(content, filename, upload.mimeType or str(head.get("contentType") or ""))
    return process_uploaded_content_as_media_asset(content, content_type, filename, object_key, timestamp)


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
            await asyncio.to_thread(put_fileobj, key, BytesIO(content), content_type)
            saved_object_keys.append(key)
            asset = await asyncio.to_thread(process_uploaded_content_as_media_asset, content, content_type, upload.filename or filename, key, timestamp)
            saved_object_keys.extend([item for item in (asset.get("thumbKey"), asset.get("previewKey")) if item])
            saved_object_keys.extend([item for item in (asset.get("normalizedKey"),) if item])
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


async def save_uploaded_objects_as_media_assets(uploads: list[UploadedMediaRequest]) -> list[dict[str, Any]]:
    timestamp = now_iso()
    saved: list[dict[str, Any]] = []
    saved_object_keys: list[str] = []
    saved_asset_ids: list[int] = []
    try:
        for upload in uploads:
            object_key = ensure_direct_upload_key(upload.objectKey)
            saved_object_keys.append(object_key)
            asset = await asyncio.to_thread(process_uploaded_object_as_media_asset, upload, timestamp)
            saved_object_keys.extend([item for item in (asset.get("thumbKey"), asset.get("previewKey")) if item])
            saved_object_keys.extend([item for item in (asset.get("normalizedKey"),) if item])
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


def serialize_styling_garment(row: Any, include_images: bool = True, include_debug: bool = False) -> dict[str, Any]:
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
        "tagAttempts": get_styling_garment_tag_attempts(row["id"]) if include_images and include_debug else [],
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


def get_styling_garment_by_id(garment_id: int, include_debug: bool = False) -> dict[str, Any]:
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
        raise HTTPException(status_code=404, detail="搭配服装不存在")
    return serialize_styling_garment(row, include_images=True, include_debug=include_debug)


async def tag_styling_garment_with_gemini(garment_id: int) -> dict[str, Any]:
    garment = get_styling_garment_by_id(garment_id)
    prompt = build_prompt_tagging_prompt(garment)
    request_snapshot = build_prompt_tagging_request_snapshot(garment, prompt)
    if not is_gemini_configured():
        message = f"{gemini_not_configured_message()}AI 打标不能生成替代标签。"
        attempt_id = create_attempt("styling_garment_tagging", garment_id, "gemini", request_snapshot)
        finish_attempt(attempt_id, "FAILED", {"error": "gemini_not_configured"}, error_message=message)
        raise RuntimeError(message)
    attempt_id = create_attempt("styling_garment_tagging", garment_id, "gemini", request_snapshot)
    images: list[dict[str, Any]] = []
    for image in garment.get("images", [])[:MAX_GARMENT_IMAGE_INPUTS]:
        asset = image.get("asset") or {}
        image_input = gemini_vision_input(asset.get("objectKey"), asset.get("mimeType"))
        if image_input:
            images.append(image_input)
    try:
        text = await generate_gemini_json_text(prompt, images)
    except Exception as exc:
        finish_attempt(attempt_id, "FAILED", response_snapshot=exception_process_facts(exc), error_message=str(exc), error_code=classify_attempt_error(exc))
        raise
    try:
        raw = parse_lily_tagging_json(text)
    except Exception as exc:
        finish_attempt(
            attempt_id,
            "FAILED",
            {"raw_text": text},
            error_message=f"Gemini 返回内容不是合法 JSON：{exc}",
            error_code=ERROR_MALFORMED_OUTPUT,
        )
        raise GeminiProviderError(f"Gemini 返回内容不是合法 JSON：{exc}", ERROR_MALFORMED_OUTPUT) from exc
    try:
        tagging_result = normalize_prompt_tagging_result(raw, garment)
    except Exception as exc:
        finish_attempt(
            attempt_id,
            "FAILED",
            {"raw_text": text, "raw_json": raw},
            error_message=f"Gemini 打标结果不符合契约：{exc}",
            error_code=ERROR_MALFORMED_OUTPUT,
        )
        raise GeminiProviderError(f"Gemini 打标结果不符合契约：{exc}", ERROR_MALFORMED_OUTPUT) from exc
    finish_attempt(attempt_id, "SUCCEEDED", {"raw_text": text, "raw_json": raw})
    return {**tagging_result, "attempt_id": attempt_id}


def build_prompt_tagging_request_snapshot(garment: dict[str, Any], prompt: str) -> dict[str, Any]:
    prompt_runtime = prompt_tagging_runtime(garment)
    return {
        "schema_version": PROMPT_TAGGING_RESULT_VERSION,
        **gemini_request_meta(),
        "prompt": prompt,
        "promptConfigKey": "assembly.ai_tagging",
        "promptConfigKeys": [GARMENT_IMAGE_SELECTION_PROMPT_CONFIG_KEY, "assembly.ai_tagging"],
        "promptRuntime": prompt_runtime,
        "renderedPromptHash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
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
            for index, image in enumerate(garment.get("images", [])[:MAX_GARMENT_IMAGE_INPUTS])
        ],
    }


def build_prompt_tagging_prompt(garment: dict[str, Any] | None = None) -> str:
    runtime = prompt_tagging_runtime(garment or {})
    selection_prompt = render_prompt_template(GARMENT_IMAGE_SELECTION_PROMPT_CONFIG_KEY, runtime)
    tagging_prompt = render_prompt_template("assembly.ai_tagging", runtime)
    return f"{selection_prompt.rstrip()}\n\n{tagging_prompt.rstrip()}\n"


def prompt_tagging_runtime(garment: dict[str, Any]) -> dict[str, str]:
    return {
        "styling_garment": dumps(styling_garment_prompt_context(garment)),
        "garment_image_files": dumps(image_selection_prompt_files(garment.get("images") or [], MAX_GARMENT_IMAGE_INPUTS)),
    }


def styling_garment_prompt_context(garment: dict[str, Any]) -> dict[str, Any]:
    images = garment.get("images") if isinstance(garment.get("images"), list) else []
    return {
        "code": garment.get("code"),
        "image_count": len(images),
        "image_inputs": [
            {
                "sort_order": image.get("sortOrder"),
                "role_hint": "primary" if index == 0 else "reference",
                "filename": ((image.get("asset") or {}).get("filename")) if isinstance(image, dict) else None,
                "mime_type": ((image.get("asset") or {}).get("mimeType")) if isinstance(image, dict) else None,
            }
            for index, image in enumerate(images[:MAX_GARMENT_IMAGE_INPUTS])
            if isinstance(image, dict)
        ],
    }


def image_selection_file_ref(image: dict[str, Any]) -> str:
    asset = image.get("asset") if isinstance(image.get("asset"), dict) else {}
    link_id = image.get("id")
    filename = asset.get("filename") or asset.get("originalFilename") or f"image-{link_id}"
    return f"image:{link_id}:{filename}"


def image_selection_prompt_files(images: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for index, image in enumerate(images[:limit]):
        if not isinstance(image, dict):
            continue
        files.append(
            {
                "file": image_selection_file_ref(image),
                "order": index + 1,
            }
        )
    return files


def selected_image_ids_from_files(selected_files: Any, images: list[dict[str, Any]], limit: int) -> list[int]:
    if not isinstance(selected_files, list) or not selected_files:
        raise ValueError("selected_files 必须是非空数组")
    candidates = images[:limit]
    file_to_id: dict[str, int] = {}
    filename_to_ids: dict[str, list[int]] = {}
    id_to_id: dict[str, int] = {}
    for image in candidates:
        if not isinstance(image, dict):
            continue
        link_id = image.get("id")
        if link_id is None:
            continue
        image_id = int(link_id)
        file_to_id[image_selection_file_ref(image)] = image_id
        id_to_id[str(image_id)] = image_id
        asset = image.get("asset") if isinstance(image.get("asset"), dict) else {}
        filename = asset.get("filename") or asset.get("originalFilename")
        if filename:
            filename_to_ids.setdefault(str(filename), []).append(image_id)
    selected_ids: list[int] = []
    for raw_ref in selected_files:
        if not isinstance(raw_ref, str) or not raw_ref.strip():
            raise ValueError("selected_files 只能包含非空字符串")
        ref = raw_ref.strip()
        image_id = file_to_id.get(ref) or id_to_id.get(ref)
        if image_id is None:
            matches = filename_to_ids.get(ref) or []
            if len(matches) == 1:
                image_id = matches[0]
            elif len(matches) > 1:
                raise ValueError(f"selected_files 引用文件名不唯一：{ref}")
        if image_id is None:
            raise ValueError(f"selected_files 引用了不存在的文件：{ref}")
        if image_id in selected_ids:
            raise ValueError(f"selected_files 重复引用文件：{ref}")
        selected_ids.append(image_id)
    return selected_ids


def apply_styling_garment_selected_image_order(db: Any, garment_id: int, selected_image_ids: list[int], timestamp: str) -> None:
    rows = db.execute(
        """
        SELECT id, sort_order
        FROM styling_garment_images
        WHERE styling_garment_id = ? AND deleted_at IS NULL
        ORDER BY sort_order, id
        """,
        (garment_id,),
    ).fetchall()
    existing_ids = [int(row["id"]) for row in rows]
    selected_set = set(selected_image_ids)
    if not selected_image_ids or not selected_set.issubset(set(existing_ids)):
        raise ValueError("selected_files 对应的图片不属于当前搭配服装")
    ordered_ids = [image_id for image_id in selected_image_ids if image_id in existing_ids]
    for sort_order, image_id in enumerate(ordered_ids):
        db.execute(
            "UPDATE styling_garment_images SET sort_order = ?, updated_at = ? WHERE id = ?",
            (sort_order, timestamp, image_id),
        )
    unselected_ids = [image_id for image_id in existing_ids if image_id not in selected_set]
    if unselected_ids:
        placeholders = ",".join("?" for _ in unselected_ids)
        db.execute(
            f"UPDATE styling_garment_images SET deleted_at = ?, updated_at = ? WHERE styling_garment_id = ? AND id IN ({placeholders})",
            (timestamp, timestamp, garment_id, *unselected_ids),
        )


def apply_generation_input_selected_image_order(db: Any, input_version_id: int, selected_image_ids: list[int]) -> None:
    row = db.execute("SELECT task_id, image_snapshot_json FROM generation_task_input_versions WHERE id = ?", (input_version_id,)).fetchone()
    if not row:
        raise ValueError("输入版本不存在")
    snapshot = loads(row["image_snapshot_json"], [])
    if not isinstance(snapshot, list):
        raise ValueError("输入版本图片快照无效")
    existing_ids = [int(item["id"]) for item in snapshot if isinstance(item, dict) and item.get("id") is not None]
    selected_set = set(selected_image_ids)
    if not selected_image_ids or not selected_set.issubset(set(existing_ids)):
        raise ValueError("selected_files 对应的主推款图片不属于当前输入版本")
    by_id = {int(item["id"]): item for item in snapshot if isinstance(item, dict) and item.get("id") is not None}
    ordered_ids = [image_id for image_id in selected_image_ids if image_id in by_id]
    ordered_snapshot: list[dict[str, Any]] = []
    for sort_order, image_id in enumerate(ordered_ids):
        next_item = {**by_id[image_id], "sortOrder": sort_order}
        ordered_snapshot.append(next_item)
    db.execute(
        "UPDATE generation_task_input_versions SET image_snapshot_json = ? WHERE id = ?",
        (dumps(ordered_snapshot), input_version_id),
    )
    timestamp = now_iso()
    for sort_order, image_id in enumerate(ordered_ids):
        db.execute(
            "UPDATE generation_task_images SET sort_order = ?, updated_at = ? WHERE id = ?",
            (sort_order, timestamp, image_id),
        )
    unselected_ids = [image_id for image_id in existing_ids if image_id not in selected_set]
    if unselected_ids:
        placeholders = ",".join("?" for _ in unselected_ids)
        db.execute(
            f"UPDATE generation_task_images SET deleted_at = ?, updated_at = ? WHERE task_id = ? AND id IN ({placeholders})",
            (timestamp, timestamp, int(row["task_id"]), *unselected_ids),
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
    if not isinstance(raw, dict):
        raise ValueError("Gemini 打标结果必须是 JSON 对象")
    selected_files = raw.get("selected_files")
    selected_image_ids = selected_image_ids_from_files(selected_files, garment.get("images") or [], MAX_GARMENT_IMAGE_INPUTS)[:MAX_GARMENT_SELECTED_IMAGES]
    ai_tags = raw.get("ai_tags")
    if not isinstance(ai_tags, dict):
        raise ValueError("Gemini 打标结果缺少 ai_tags 对象")
    tag_meta = build_prompt_tag_meta(ai_tags, garment, provider=provider, warnings=warnings, review_reasons=review_reasons)
    return {
        "schema_version": str(ai_tags.get("schema_version") or ai_tags.get("schemaVersion") or PROMPT_TAGGING_RESULT_VERSION),
        "selected_files": selected_files,
        "selected_image_ids": selected_image_ids,
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
    reasons = list(dict.fromkeys(reasons))
    return {
        "tagger_provider": str(source_meta.get("tagger_provider") or provider),
        "tagger_model": str(source_meta.get("tagger_model") or effective_gemini_model()),
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
    return styling_library_tag_card_for_prompt(card, include_internal=True)


def styling_library_tag_card_for_prompt(card: dict[str, Any], include_internal: bool = False) -> dict[str, Any]:
    tag_facts = first_dict(card.get("tag_facts"), card.get("tagFacts"))
    ai_tags = first_dict(tag_facts.get("ai_tags"), tag_facts.get("aiTags"), card.get("ai_tags"), card.get("aiTags"))
    manual = first_dict(card.get("manual"), card.get("manualTags"), tag_facts.get("manual_tags"), tag_facts.get("manualTags"))
    tag_meta = first_dict(tag_facts.get("tag_meta"), tag_facts.get("tagMeta"), card.get("tagMeta"), card.get("tag_meta"))

    result: dict[str, Any] = {
        "code": str(card.get("code") or ""),
        "ai_tags": ai_tags,
        "manual_flags": compact_manual_prompt_flags(manual),
        "review": compact_tag_review_meta(tag_meta),
    }
    if include_internal and card.get("id") is not None:
        result["id"] = card.get("id")
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


def first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def compact_manual_prompt_flags(manual: dict[str, Any]) -> dict[str, Any]:
    allowed = ("do_not_use_for_styling", "preferred_pairing", "avoid_pairing", "wearing_note", "styling_note")
    return {key: manual[key] for key in allowed if key in manual and manual[key] not in (None, "", [], {})}


def compact_tag_review_meta(tag_meta: dict[str, Any]) -> dict[str, Any]:
    confidence = tag_meta.get("confidence", tag_meta.get("overall_confidence"))
    reasons = ensure_string_list(tag_meta.get("review_reasons") or tag_meta.get("reasons"))[:4]
    result: dict[str, Any] = {}
    if confidence is not None:
        result["confidence"] = clamp_float(confidence, 0, 1, 0)
    if reasons:
        result["reasons"] = reasons
    if tag_meta.get("needs_review") is not None:
        result["needs_review"] = bool(tag_meta.get("needs_review"))
    return result


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
    extracted = extract_first_complete_json_value(stripped)
    if extracted:
        return extracted
    return stripped or "{}"


def extract_first_complete_json_value(text: str) -> str:
    start = next((index for index, char in enumerate(text) if char in "{["), -1)
    if start < 0:
        return ""
    stack: list[str] = []
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in "{[":
            stack.append("}" if char == "{" else "]")
            continue
        if char in "}]":
            if not stack or char != stack[-1]:
                break
            stack.pop()
            if not stack:
                return text[start : index + 1]
    end = text.rfind("}")
    if text[start] == "[":
        end = text.rfind("]")
    if end >= start:
        return text[start : end + 1]
    return text[start:]


async def generate_gemini_json_object_with_reask(
    prompt: str,
    images: list[dict[str, Any]] | None = None,
    system_instruction: str | None = None,
) -> dict[str, Any]:
    """调用 Gemini 取 JSON，解析失败(MALFORMED_OUTPUT)时自动重问一次。

    分层的容错策略（避免重复扣费）：
    1. generate_gemini_json_text 内部已对"零产出瞬时错误"(502/连接错/429)做安全重发；
       此层不感知那些，只处理"已拿到文本但解析不出合法 JSON"和截断。
    2. parse_gemini_json_object 已尽力做容错解析（剥控制字符、提取首个完整 JSON、
       宽松解析、pythonish 兜底）。仍失败才走重问。
    3. 重问：重新发起一次 Gemini 调用（追加"仅输出合法 JSON"约束），最多
       gemini_malformed_reask_limit 次。模型随机性使二次多半成功。截断
       (finish_reason=length) 同样归 MALFORMED_OUTPUT，一并走重问。
    4. 重问仍失败 → 抛 GeminiProviderError(MALFORMED_OUTPUT)，由上层判死。

    余额/鉴权/网关等 error_code 直接透传（不吞不重问），交由上层策略处理。
    """
    reask_limit = max(0, settings.gemini_malformed_reask_limit)
    attempt = 0
    reask_prompt = prompt
    while True:
        text = await generate_gemini_json_text(reask_prompt, images, system_instruction=system_instruction)
        try:
            return parse_gemini_json_object(text)
        except GeminiProviderError:
            # 已带 error_code（如流式截断 MALFORMED_OUTPUT）——按下方统一重问逻辑处理。
            raise
        except Exception as parse_error:
            if attempt >= reask_limit:
                raise GeminiProviderError(
                    f"Gemini 返回内容不是合法 JSON（已重问 {attempt} 次）：{parse_error}",
                    ERROR_MALFORMED_OUTPUT,
                ) from parse_error
            attempt += 1
            # JSON 输出纪律与重问约束已写入各 stage 的 Prompt 维护正文（每次都随 prompt 下发），
            # 故重问直接重发原 prompt；模型随机性使二次多半成功。
            reask_prompt = prompt
            logger.warning("Gemini 返回 JSON 解析失败，自动重问第 %d 次：%s", attempt, str(parse_error)[:200])


# 我方业务/穿法校验否决的关键词。命中 → BUSINESS_REJECT（非 provider 错误，不重试）。
_STYLING_BUSINESS_REJECT_KEYWORDS = ("塞腰", "半塞", "穿法", "审美", "品牌不成立", "商品不成立")


def classify_styling_business_reject(exc: BaseException) -> str | None:
    """选搭失败若源自我方审美/穿法业务校验，则归 BUSINESS_REJECT。

    这类是造型质量结果、不是基础设施失败，不能当 provider 错误无脑重试。
    仅在没有 provider error_code 时作为兜底判断。
    """
    message = str(exc)
    if any(keyword in message for keyword in _STYLING_BUSINESS_REJECT_KEYWORDS):
        return ERROR_BUSINESS_REJECT
    return None


def wrap_gemini_call_error(label: str, exc: BaseException) -> RuntimeError:
    """把 Gemini 调用异常包一层可读前缀，但保留 error_code，方便上层 finish_attempt 分类。"""
    error_code = classify_attempt_error(exc)
    if error_code:
        return GeminiProviderError(
            f"{label}：{exc}",
            error_code,
            retry_after=getattr(exc, "retry_after", None),
            provider_response=getattr(exc, "provider_response", None),
        )
    return RuntimeError(f"{label}：{exc}")


def exception_process_facts(exc: BaseException) -> dict[str, Any]:
    facts = getattr(exc, "provider_response", None)
    return facts if isinstance(facts, dict) else {}


def user_facing_provider_error_message(label: str, exc: BaseException, error_code: str | None) -> str:
    """把技术报错翻译成面向普通用户的文案；技术码只在 detail 里保留一份。

    余额类必须明确提示充值，绝不表现成"生成失败请重试"（会让用户白等、继续烧额度）。
    """
    response = getattr(exc, "provider_response", None)
    provider_error = response.get("providerError") if isinstance(response, dict) else None
    reason = provider_error.get("upstreamMessage") if isinstance(provider_error, dict) else None
    presentation = provider_error_presentation(error_code, str(reason or ""))
    return f"{label}：{presentation['title']}。{presentation['message']}"


def parse_gemini_json_object(text: str) -> dict[str, Any]:
    json_text = extract_json_text(text)
    try:
        parsed = loads(json_text, {})
    except json.JSONDecodeError as strict_error:
        # OpenAI 兼容通道(ToAPIs)没有原生 JSON 模式,模型手写 JSON 的字符串值内
        # 可能包含未转义的换行等控制字符;strict=False 允许这类字符。
        try:
            parsed = json.loads(json_text, strict=False)
        except json.JSONDecodeError:
            parsed = None
        else:
            if isinstance(parsed, dict):
                return parsed
        relaxed_json_text = repair_jsonish_model_text(json_text)
        if relaxed_json_text != json_text:
            try:
                parsed = json.loads(relaxed_json_text, strict=False)
            except json.JSONDecodeError:
                parsed = None
            else:
                if isinstance(parsed, dict):
                    return parsed
        decoder = json.JSONDecoder(strict=False)
        try:
            parsed, end_index = decoder.raw_decode(relaxed_json_text if relaxed_json_text != json_text else json_text)
        except json.JSONDecodeError:
            fallback = parse_pythonish_model_object(json_text)
            if fallback is not None:
                return fallback
            raise json_parse_failure_with_excerpt(strict_error, text) from strict_error
        trailing = (relaxed_json_text if relaxed_json_text != json_text else json_text)[end_index:].strip()
        if trailing and not is_recoverable_json_trailing_noise(trailing):
            fallback = parse_pythonish_model_object(json_text)
            if fallback is not None:
                return fallback
            raise json_parse_failure_with_excerpt(strict_error, text) from strict_error
    if not isinstance(parsed, dict):
        return {"raw_result": parsed}
    return parsed


def parse_pythonish_model_object(json_text: str) -> dict[str, Any] | None:
    """兜底解析模型手写 JSON 的两种实测异形（2026-07-03 线上复现）：

    1. 单引号 Python dict 风格（json.loads 报 Expecting property name ... char 1）；
    2. 双花括号 {{...}} 包裹（同上报错）。
    仅在标准链路（strict / strict=False / repair / raw_decode）全部失败后调用。
    """
    # TypeError: {{...}} 会被当作含 dict 的 set 字面量（dict 不可哈希）。
    try:
        value = ast.literal_eval(json_text)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        value = None
    if isinstance(value, dict):
        return value
    stripped = json_text.strip()
    if stripped.startswith("{{") and stripped.endswith("}}"):
        inner = stripped[1:-1]
        try:
            parsed = json.loads(inner, strict=False)
        except json.JSONDecodeError:
            try:
                value = ast.literal_eval(inner)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                value = None
            return value if isinstance(value, dict) else None
        if isinstance(parsed, dict):
            return parsed
    return None


def json_parse_failure_with_excerpt(strict_error: json.JSONDecodeError, raw_text: str) -> RuntimeError:
    """解析失败时把模型原文头尾带进异常，随 attempt.error_message 落库，杜绝盲修。"""
    head = raw_text[:400]
    # max(400, ...) 保证 401-800 字符时头尾不重叠重复。
    tail = raw_text[max(400, len(raw_text) - 400):] if len(raw_text) > 400 else ""
    message = f"JSON 解析失败（{strict_error}）；模型原文 {len(raw_text)} 字符，头部片段: {head!r}"
    if tail:
        message += f" …… 尾部片段: {tail!r}"
    return RuntimeError(message)


def parse_lily_tagging_json(text: str) -> dict[str, Any]:
    return parse_gemini_json_object(text)


def remove_json_trailing_commas(value: str) -> str:
    previous = value
    while True:
        next_value = re.sub(r",\s*([}\]])", r"\1", previous)
        if next_value == previous:
            return next_value
        previous = next_value


def repair_jsonish_model_text(value: str) -> str:
    return remove_dangling_json_object_tail(
        insert_missing_json_property_commas(remove_json_trailing_commas(value))
    )


def insert_missing_json_property_commas(value: str) -> str:
    value_token = r'"(?:\\.|[^"\\])*"|[\]}]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null'
    key_token = r'"[^"\r\n]+?"\s*:'
    pattern = re.compile(rf"({value_token})(\s+)({key_token})")
    previous = value
    while True:
        next_value = pattern.sub(r"\1,\2\3", previous)
        if next_value == previous:
            return next_value
        previous = next_value


def remove_dangling_json_object_tail(value: str) -> str:
    value_token = r'"(?:\\.|[^"\\])*"|[\]}]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null'
    return re.sub(rf"({value_token})(\s+)([^{{}}\[\],:]+?)(\s*)\}}$", r"\1\4}", value)


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
            SELECT gti.id AS link_id, gti.sort_order, ma.* FROM generation_task_images gti
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
            {"id": row["link_id"], "sortOrder": row["sort_order"], "asset": serialize_media_asset(row)}
            for row in image_rows
        ]
        model_snapshot = snapshot_default_model()
        cursor = db.execute(
            """
            INSERT INTO generation_task_input_versions
              (task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, user_requirement_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                version_number,
                task["name"],
                dumps(snapshot),
                dumps(model_snapshot),
                dumps(build_user_requirement_context(task)),
                timestamp,
            ),
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


def finish_attempt(
    attempt_id: int,
    status: str,
    response_snapshot: dict[str, Any] | None = None,
    output_asset_id: int | None = None,
    error_message: str | None = None,
    error_code: str | None = None,
) -> None:
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_attempts
            SET status = ?, completed_at = ?, failed_at = ?, cancelled_at = ?,
                response_snapshot_json = COALESCE(?, response_snapshot_json), output_asset_id = ?,
                error_code = CASE WHEN ? = 'SUCCEEDED' THEN NULL ELSE COALESCE(?, error_code) END,
                error_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                timestamp if status == "SUCCEEDED" else None,
                timestamp if status == "FAILED" else None,
                timestamp if status == "CANCELLED" else None,
                dumps(response_snapshot) if response_snapshot is not None else None,
                output_asset_id,
                status,
                error_code,
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


def prepare_attempt_provider_resubmission(
    attempt_id: int | None,
    snapshot: dict[str, Any],
) -> None:
    """重新创建 provider 任务前原子切换当前代次。

    旧 provider id 只保留在 history/snapshot；当前 id 必须清空。这样从清空到新
    id 落库之间若进程退出，恢复逻辑会安全地标记 SUBMISSION_UNKNOWN，而不会把
    上一代已经失败的 provider 任务误当作当前任务继续轮询。
    """
    if not attempt_id:
        return
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_attempts
            SET provider_request_id = NULL, response_snapshot_json = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING'
            """,
            (dumps({**snapshot, "status": "SUBMITTING", "providerRequestId": None}), timestamp, attempt_id),
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


def fail_stale_attempt(
    attempt_id: int | None,
    message: str,
    error_code: str = ERROR_SYSTEM_ERROR,
) -> None:
    if not attempt_id:
        return
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_attempts
            SET status = 'FAILED', failed_at = ?, error_code = ?,
                error_message = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING'
            """,
            (timestamp, error_code, message, timestamp, attempt_id),
        )


def stale_attempt_error(attempt_id: int | None) -> tuple[str, str]:
    """锁过期且没有 provider id 时，区分提交未知与本地执行中断。"""
    if not attempt_id:
        return ERROR_SYSTEM_ERROR, "生成任务未建立有效执行记录，需要人工检查。"
    with get_db() as db:
        row = db.execute("SELECT progress_json FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
    progress = loads(row["progress_json"], {}) if row else {}
    current = progress.get("current") if isinstance(progress, dict) else None
    if current == PHASE_SUBMIT:
        return ERROR_SUBMISSION_UNKNOWN, "创建 provider 任务时执行中断，无法确认是否已经创建；已停止自动重提。"
    return ERROR_SYSTEM_ERROR, f"本地生成任务在 {current or 'unknown'} 阶段执行中断；已停止自动重试。"


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
    request_snapshot["candidate_retrieval"] = candidate_snapshot
    return input_row, candidate_snapshot, request_snapshot


async def select_styling_with_gemini(
    input_row: Any,
    candidate_snapshot: dict[str, Any],
    request_snapshot: dict[str, Any],
    attempt_id: int | None = None,
    progress: AttemptProgressRecorder | None = None,
) -> dict[str, Any]:
    if not is_gemini_configured():
        raise RuntimeError(f"{gemini_not_configured_message()}AI Stylist 不能进入生产选搭。")
    if not request_snapshot.get("prompt"):
        request_snapshot = attach_ai_stylist_prompt_snapshot(request_snapshot, candidate_snapshot)
    if attempt_id:
        update_attempt_request_snapshot(attempt_id, request_snapshot)
    prompt = str(request_snapshot.get("prompt") or "")
    images: list[dict[str, Any]] = []
    for item in loads(input_row["image_snapshot_json"], [])[:MAX_HERO_IMAGE_INPUTS]:
        asset = (item.get("asset") or {})
        image_input = gemini_vision_input(asset.get("objectKey"), asset.get("mimeType"))
        if image_input:
            images.append(image_input)
    try:
        if progress:
            progress.begin("styling_gemini", imageCount=len(images), candidateCount=len(candidate_snapshot.get("candidate_material_ids") or []))
        text = await generate_gemini_json_text(
            prompt,
            images,
            # 系统指令已并入 AI 选搭 Prompt 正文（prompt 内），不再单独下发 system_instruction。
            system_instruction=request_snapshot.get("systemInstruction"),
        )
        try:
            raw = parse_gemini_json_object(text)
        except Exception as parse_error:
            raise GeminiProviderError(
                f"Gemini 返回内容不是合法 JSON：{parse_error}",
                ERROR_MALFORMED_OUTPUT,
            ) from parse_error
    except Exception as exc:
        raise wrap_gemini_call_error("Gemini AI Stylist 调用失败", exc) from exc
    return normalize_ai_stylist_result(raw, input_row, candidate_snapshot)


def normalize_ai_stylist_result(raw: Any, input_row: Any, candidate_snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Gemini AI Stylist 结果必须是 JSON 对象")
    selected_image_ids = selected_image_ids_from_files(
        raw.get("selected_files"),
        loads(input_row["image_snapshot_json"], []),
        MAX_HERO_IMAGE_INPUTS,
    )[:MAX_HERO_SELECTED_IMAGES]
    plan_source = raw.get("styling_plan")
    if not isinstance(plan_source, dict):
        raise ValueError("Gemini AI Stylist 结果缺少 styling_plan 对象")
    styling_plan = normalize_styling_plan(plan_source, candidate_snapshot)
    styling_plan["selected_hero_image_ids"] = selected_image_ids
    styling_plan["model_raw_output"] = raw
    return styling_plan


def attach_ai_stylist_prompt_snapshot(request_snapshot: dict[str, Any], candidate_snapshot: dict[str, Any]) -> dict[str, Any]:
    # P3：用户要求/本步骤介入要求以自然语言文本填充占位符，去掉重复的
    # priority_policy / cannot_override 治理元数据——优先级与冲突处理已由
    # AI 选搭 Prompt 正文（含开头系统指令段）声明一次。candidate_retrieval 是
    # 结构化标签事实业务输入，保持 JSON。
    input_version = request_snapshot.get("input_version") if isinstance(request_snapshot.get("input_version"), dict) else {}
    hero_images = input_version.get("imageSnapshot") if isinstance(input_version.get("imageSnapshot"), list) else []
    prompt_runtime = {
        "hero_image_files": dumps(image_selection_prompt_files(hero_images, MAX_HERO_IMAGE_INPUTS)),
        "candidate_retrieval": dumps(candidate_retrieval_for_prompt(candidate_snapshot)),
        "ai_styling_user_instruction": render_user_instruction_line(request_snapshot.get("ai_styling_user_instruction") or {}),
        "user_requirement": render_user_requirement_line(request_snapshot.get("user_requirement") or {}),
    }
    selection_prompt = render_prompt_template(HERO_IMAGE_SELECTION_PROMPT_CONFIG_KEY, prompt_runtime)
    styling_prompt = render_prompt_template(AI_STYLING_PROMPT_CONFIG_KEY, prompt_runtime)
    prompt = f"{selection_prompt.rstrip()}\n\n{styling_prompt.rstrip()}\n"
    return {
        **request_snapshot,
        "prompt": prompt,
        "promptConfigKey": AI_STYLING_PROMPT_CONFIG_KEY,
        "promptConfigKeys": [HERO_IMAGE_SELECTION_PROMPT_CONFIG_KEY, AI_STYLING_PROMPT_CONFIG_KEY],
        "promptRuntime": prompt_runtime,
        "renderedPromptHash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }


def candidate_retrieval_for_prompt(candidate_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    cards = candidate_snapshot.get("library_tag_cards") if isinstance(candidate_snapshot.get("library_tag_cards"), list) else []
    return [styling_garment_card_for_prompt(card) for card in cards if isinstance(card, dict)]


def styling_garment_card_for_prompt(card: dict[str, Any]) -> dict[str, Any]:
    source = styling_library_tag_card_for_prompt(card) if "tag_facts" in card else card
    return {key: value for key, value in source.items() if key != "id" and value not in (None, "", [], {})}


def build_candidate_retrieval_snapshot(input_row: Any, material_cards: list[dict[str, Any]]) -> dict[str, Any]:
    library_tag_cards = [library_tag_card_for_prompt(card) for card in material_cards]
    return {
        "schema_version": "candidate-retrieval-v3",
        "strategy": "direct_ai_tags_json_v1",
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
        "prompt_rule_source": AI_STYLING_PROMPT_CONFIG_KEY,
    }


def build_phase_one_brand_style_evidence() -> list[dict[str, Any]]:
    return []


def build_ai_stylist_request_snapshot(input_row: Any, candidate_snapshot: dict[str, Any]) -> dict[str, Any]:
    user_requirement = loads(row_value(input_row, "user_requirement_json"), build_user_requirement_context({}))
    return {
        "schema_version": "ai-stylist-request-v1",
        **gemini_request_meta(),
        # 系统指令已并入 AI 选搭 Prompt 正文，不再单独快照 systemInstruction。
        "systemInstruction": None,
        "input_version": {
            "id": input_row["id"],
            "taskId": input_row["task_id"],
            "nameSnapshot": input_row["name_snapshot"],
            "imageSnapshot": loads(input_row["image_snapshot_json"], []),
            "userRequirement": user_requirement,
        },
        "candidate_retrieval": candidate_snapshot,
        "ai_styling_user_instruction": {},
        "user_requirement": user_requirement,
        "prompt_rule_source": AI_STYLING_PROMPT_CONFIG_KEY,
    }


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
    if not normalized_candidates and plan_source_mode == "fallback":
        normalized_candidates = [normalize_outfit_candidate({}, candidate_ids, candidate_cards, candidate_cards_by_code_map, 1)]
    requested_selected_outfit_id = str(source.get("selected_outfit_key") or source.get("selected_outfit_id") or "")
    selected_codes = normalize_selected_material_codes(source.get("selected_material_codes") or source.get("selected_materials"), candidate_cards_by_code_map)
    selected_ids = selected_material_ids_for_codes(selected_codes, candidate_cards_by_code_map)
    selected_ids_source = source.get("selected_material_ids")
    if not isinstance(selected_ids_source, list):
        selected_ids_source = []
    if not selected_ids:
        selected_ids = normalize_selected_material_ids(selected_ids_source, candidate_ids)
    selected = resolve_selected_outfit_candidate(normalized_candidates, requested_selected_outfit_id, selected_ids, selected_codes)
    selected_outfit_id = str(selected.get("outfit_key") or selected.get("id") or "") if selected else requested_selected_outfit_id
    selected_key_normalized = bool(normalized_candidates) and selected_outfit_id != requested_selected_outfit_id
    if not selected_ids:
        selected_ids = selected.get("selected_material_ids") if isinstance(selected.get("selected_material_ids"), list) else []
    selected_refs = normalize_selected_material_refs(source.get("selected_materials"), selected_ids, candidate_cards, candidate_cards_by_code_map)
    if not selected_refs:
        selected_refs = normalize_selected_material_refs(selected.get("selected_materials"), selected_ids, candidate_cards, candidate_cards_by_code_map)
    selected_codes = [item["material_code"] for item in selected_refs if item.get("material_code")]
    warnings = ensure_string_list(source.get("warnings"))
    if selected_key_normalized:
        warnings.append("selected_outfit_key_auto_normalized")
    rejected_source = source.get("rejected_candidates") if isinstance(source.get("rejected_candidates"), list) else []
    return {
        "schema_version": "styling-plan-v1",
        "plan_source_mode": "fallback" if plan_source_mode == "fallback" else "production",
        "hero_understanding": {
            "hero_visual_summary": str(hero.get("hero_visual_summary") or source.get("hero_visual_summary") or "主推款图片已作为商品事实源。")[:120],
            "category": str(hero.get("category") or "")[:80],
            "sub_category": str(hero.get("sub_category") or hero.get("subcategory") or "")[:80],
            "shot_category_family": normalize_shot_category_family(hero.get("shot_category_family") or hero.get("category_family")) or "",
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
        "reason_summary": str(source.get("reason_summary") or "已根据主推款和搭配服装标签完成 AI Stylist 造型方案。")[:180],
        "user_requirement_resolution": normalize_user_requirement_resolution(source.get("user_requirement_resolution")),
        "rejected_candidates": normalize_rejected_candidates(rejected_source, normalized_candidates, candidate_cards),
        "quality_signals": normalize_model_quality_signals(source.get("quality_signals")),
        "warnings": warnings[:8],
    }


def resolve_selected_outfit_candidate(
    normalized_candidates: list[dict[str, Any]],
    requested_selected_outfit_id: str,
    selected_ids: list[int],
    selected_codes: list[str],
) -> dict[str, Any]:
    if not normalized_candidates:
        return {}
    if requested_selected_outfit_id:
        selected = next(
            (
                item
                for item in normalized_candidates
                if str(item.get("outfit_key") or item.get("id") or "") == requested_selected_outfit_id
            ),
            {},
        )
        if selected:
            return selected
    selected_id_set = set(selected_ids)
    if selected_id_set:
        selected = next(
            (
                item
                for item in normalized_candidates
                if set(item.get("selected_material_ids") if isinstance(item.get("selected_material_ids"), list) else []) == selected_id_set
            ),
            {},
        )
        if selected:
            return selected
    selected_code_set = set(selected_codes)
    if selected_code_set:
        selected = next(
            (
                item
                for item in normalized_candidates
                if set(item.get("selected_material_codes") if isinstance(item.get("selected_material_codes"), list) else []) == selected_code_set
            ),
            {},
        )
        if selected:
            return selected
    return {}


def normalize_user_requirement_resolution(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "user_requirement_summary": str(source.get("user_requirement_summary") or source.get("summary") or "")[:180],
        "satisfied": ensure_string_list(source.get("satisfied"))[:8],
        "not_satisfied": ensure_string_list(source.get("not_satisfied"))[:8],
        "conflicts": ensure_string_list(source.get("conflicts"))[:8],
        "alternative_handling": ensure_string_list(source.get("alternative_handling"))[:8],
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
            rf"(库内(?:针织衫|衬衫|外套|西装|上衣|下装|裤装|半裙|连衣裙|搭配服装|服饰|单品)|皮革马甲)\s*{escaped_id}(?![A-Za-z0-9])",
            rf"\1 {code} ",
            result,
        )
    result = re.sub(r"\b(?:material[_\s-]*id|服饰ID|商品ID|ID)\s*[:：#-]?\s*\d+(?![A-Za-z0-9])", "该服饰", result, flags=re.IGNORECASE)
    result = re.sub(r"(库内(?:针织衫|衬衫|外套|西装|上衣|下装|裤装|半裙|连衣裙|搭配服装|服饰|单品)|皮革马甲)\s*\d+(?![A-Za-z0-9])", r"\1", result)
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


def strip_debug_response_fields(value: Any) -> Any:
    if isinstance(value, list):
        return [strip_debug_response_fields(item) for item in value]
    if isinstance(value, dict):
        return {
            key: strip_debug_response_fields(item)
            for key, item in value.items()
            if key != "model_raw_output"
        }
    return value


def styling_result_for_prompt(styling_plan: dict[str, Any], selected_material_snapshot: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    plan = styling_plan if isinstance(styling_plan, dict) else {}
    selected = selected_outfit_from_plan(plan)
    selected_materials = selected_material_snapshot or []
    selected_refs = selected.get("selected_materials") if isinstance(selected.get("selected_materials"), list) else []
    return {
        "schema_version": "styling-plan-director-context-v1",
        "hero_understanding": strip_internal_prompt_fields(plan.get("hero_understanding") if isinstance(plan.get("hero_understanding"), dict) else {}),
        "styling_intents": strip_internal_prompt_fields(plan.get("styling_intents") if isinstance(plan.get("styling_intents"), list) else []),
        "selected_outfit": strip_internal_prompt_fields({
            "outfit_key": selected.get("outfit_key") or selected.get("id") or plan.get("selected_outfit_key"),
            "mode": selected.get("mode"),
            "title": selected.get("title"),
            "selected_material_codes": selected.get("selected_material_codes") or plan.get("selected_material_codes") or [],
            "selected_materials": selected_refs,
            "generated_accessories": selected.get("generated_accessories") or [],
            "wearing_notes": selected.get("wearing_notes") or "",
            "hero_focus": selected.get("hero_focus") or [],
            "aesthetic_diagnosis": selected.get("aesthetic_diagnosis") or {},
            "scores": selected.get("scores") or {},
            "risk_notes": selected.get("risk_notes") or [],
        }),
        "selected_material_tag_cards": [compress_selected_material_for_prompt(item) for item in selected_materials[:4] if isinstance(item, dict)],
        "reason_summary": str(plan.get("reason_summary") or "")[:180],
        "user_requirement_resolution": strip_internal_prompt_fields(plan.get("user_requirement_resolution") if isinstance(plan.get("user_requirement_resolution"), dict) else {}),
        "quality_signals": strip_internal_prompt_fields(plan.get("quality_signals") if isinstance(plan.get("quality_signals"), list) else []),
        "warnings": ensure_string_list(plan.get("warnings"))[:6],
    }


def compress_selected_material_for_prompt(item: dict[str, Any]) -> dict[str, Any]:
    return styling_library_tag_card_for_prompt(item)


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
    if plan.get("plan_source_mode") != "fallback" and not plan.get("model_raw_output"):
        raise RuntimeError("AI Stylist 返回空 JSON，未生成有效 StylingPlan")
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
        raise RuntimeError(f"AI Stylist 返回了非法搭配服装 ID：{sorted(invalid)}")
    if len(selected_ids) != len(set(selected_ids)):
        raise RuntimeError("AI Stylist 返回了重复搭配服装 ID")
    if plan.get("plan_source_mode") == "fallback":
        raise RuntimeError("AI Stylist 工程 fallback 方案不能进入生产链路")
    storyboard_category_family(plan)


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
        raise RuntimeError(f"Gemini 返回了非法搭配服装 ID：{sorted(invalid)}")
    return source_ids, [get_styling_garment_by_id(item_id) for item_id in selected_ids]


def selected_material_image_refs_for_request(selected_snapshot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in selected_snapshot[:4]:
        if not isinstance(item, dict):
            continue
        images = item.get("images") if isinstance(item.get("images"), list) else []
        refs.append({
            "code": item.get("code"),
            "images": [
                {"asset": image.get("asset")}
                for image in images[:6]
                if isinstance(image, dict) and isinstance(image.get("asset"), dict)
            ],
        })
    return refs


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
    return instructions[:6] or ["生成配饰低存在感，可省略，不做特写，不遮挡主推款和搭配服装。"]


def build_category_shot_template(hero_category: str) -> list[tuple[str, str, str, str, str]]:
    return build_builtin_category_shot_template(hero_category)


def storyboard_category_family(styling_plan: dict[str, Any]) -> str:
    hero = styling_plan.get("hero_understanding") if isinstance(styling_plan.get("hero_understanding"), dict) else {}
    return resolve_storyboard_category_family(hero, strict=True) or ""


def resolve_storyboard_category_family(hero: dict[str, Any], strict: bool = True) -> str | None:
    explicit = normalize_shot_category_family(hero.get("shot_category_family") or hero.get("category_family"))
    if explicit:
        return explicit
    if strict:
        raise RuntimeError("AI 选搭结果缺少或非法 shot_category_family，无法选择分镜 Prompt")
    return None


def normalize_shot_category_family(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return None
    aliases = {
        "top": "top",
        "tops": "top",
        "upper": "top",
        "upper_body": "top",
        "上装": "top",
        "上衣": "top",
        "外套": "top",
        "针织": "top",
        "bottom": "bottom",
        "bottoms": "bottom",
        "lower": "bottom",
        "lower_body": "bottom",
        "下装": "bottom",
        "裤装": "bottom",
        "半身裙": "bottom",
        "one_piece": "one_piece",
        "onepiece": "one_piece",
        "dress": "one_piece",
        "jumpsuit": "one_piece",
        "set": "one_piece",
        "suit_set": "one_piece",
        "连衣裙": "one_piece",
        "连体": "one_piece",
        "套装": "one_piece",
    }
    return aliases.get(text)


def category_route_terms(hero: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for key in ("category", "sub_category", "subcategory"):
        value = hero.get(key)
        if value:
            terms.append(str(value))
    for key in ("official_category_candidates", "official_category_terms", "category_candidates"):
        value = hero.get(key)
        if isinstance(value, list):
            terms.extend(str(item) for item in value if item)
        elif value:
            terms.append(str(value))
    return terms


def storyboard_prompt_config_key(styling_plan: dict[str, Any]) -> str:
    return STORYBOARD_PROMPT_CONFIG_KEYS[storyboard_category_family(styling_plan)]


def final_image_prompt_config_key(styling_plan: dict[str, Any]) -> str:
    return FINAL_IMAGE_PROMPT_CONFIG_KEYS[storyboard_category_family(styling_plan)]


def final_image_slot_roles(styling_plan: dict[str, Any]) -> list[str]:
    family = storyboard_category_family(styling_plan)
    return [f"{family}_grid_{index}" for index in range(1, 6)]


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
        ("hero", "正面全身主视觉", "轻微重心变化，避免站军姿", "正面或轻斜正", "主推款完整轮廓，搭配服装自然可见"),
        ("core", "品类核心展示", "自然克制姿态", "正面或 3/4", "主推款核心结构和比例"),
        ("detail", "设计细节近景", "局部近景，可不露脸", "局部", "主推款关键设计点"),
        ("side", "侧身或动态补充", "侧身轻走动感", "侧面或 3/4", "主推款侧面轮廓和动态"),
        ("front_fit", "标准正面颜色图", "稳定克制", "正面", "颜色、版型、平台友好"),
        ("reference", "参考格", "备用姿态", "自然", "参考，不进入最终生成"),
    ]


def model_identity_contract_from_snapshot(model_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    snapshot = model_snapshot if isinstance(model_snapshot, dict) else {}
    profile = snapshot.get("profile") if isinstance(snapshot.get("profile"), dict) else {}
    image_inputs = snapshot.get("imageInputs") if isinstance(snapshot.get("imageInputs"), list) else []
    return {
        "model_id": str(snapshot.get("modelId") or snapshot.get("model_id") or "Face-01"),
        "name": str(snapshot.get("name") or ""),
        "fact_source": "model_identity_reference_images",
        "must_preserve": [
            "same person identity",
            "face shape",
            "facial features",
            "head shape",
            "hair length",
            "hair outline",
            "bangs/fringe if visible",
            "skin tone",
            "temperament",
        ],
        "must_not_change": [
            "do not invent a different face",
            "do not change hair length or hair outline",
            "do not change bangs/fringe",
            "do not switch to a ponytail, updo, long hair, or any hairstyle not visible in the model reference images",
            "do not generate heavy makeup or a different facial expression style",
        ],
        "profile_notes": str(profile.get("notes") or ""),
        "forbidden": ensure_string_list(profile.get("forbidden"))[:12],
        "reference_images": [
            {
                "role": "model_identity",
                "angle": str(item.get("angle") or ""),
                "filename": str(item.get("filename") or ""),
                "objectKey": str(item.get("objectKey") or ""),
            }
            for item in image_inputs[:6]
            if isinstance(item, dict)
        ],
    }


def build_new_preview_task_package(
    task_id: int,
    styling_id: int,
    preview_id: int | None = None,
) -> dict[str, Any]:
    styling_plan = get_styling_plan_by_id(styling_id)
    category_family = storyboard_category_family(styling_plan)
    prompt_config_key = STORYBOARD_PROMPT_CONFIG_KEYS[category_family]
    with get_db() as db:
        if preview_id is not None:
            preview_row = db.execute(
                """
                SELECT input_version_id, user_instruction_json, environment_asset_id
                FROM generation_previews
                WHERE id = ? AND task_id = ? AND styling_id = ?
                """,
                (preview_id, task_id, styling_id),
            ).fetchone()
        else:
            preview_row = db.execute(
                """
                SELECT input_version_id, user_instruction_json, environment_asset_id
                FROM generation_previews
                WHERE task_id = ? AND styling_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (task_id, styling_id),
            ).fetchone()
        input_version_id = int(preview_row["input_version_id"]) if preview_row and preview_row["input_version_id"] else None
        if input_version_id is None:
            styling_row = db.execute("SELECT input_version_id FROM generation_stylings WHERE id = ?", (styling_id,)).fetchone()
            input_version_id = int(styling_row["input_version_id"]) if styling_row and styling_row["input_version_id"] else None
        input_row = db.execute(
            "SELECT * FROM generation_task_input_versions WHERE id = ?",
            (input_version_id,),
        ).fetchone() if input_version_id is not None else None
    environment_asset_id = row_value(preview_row, "environment_asset_id")
    image_inputs = build_new_image_inputs(task_id, styling_id, include_preview=False, input_version_id=input_version_id, environment_asset_id=environment_asset_id)
    storyboard_preview_user_instruction = loads(row_value(preview_row, "user_instruction_json"), build_stage_user_instruction_context("storyboard_preview"))
    prompt_runtime = build_preview_prompt_runtime(styling_plan, image_inputs, storyboard_preview_user_instruction)
    prompt = render_prompt_template(prompt_config_key, prompt_runtime)
    return {
        "generation": {"stage": "preview_grid"},
        "prompt": prompt,
        "categoryFamily": category_family,
        **prompt_execution_facts(prompt_config_key, prompt, prompt_runtime),
        "imageInputs": image_inputs,
        "inputVersionId": input_version_id,
        "storyboardPreviewUserInstruction": storyboard_preview_user_instruction,
        "stylingPlan": styling_plan,
    }


def build_new_preview_prompt(
    styling_plan: dict[str, Any],
    image_inputs: list[dict[str, Any]] | None = None,
    storyboard_preview_user_instruction: dict[str, Any] | None = None,
) -> str:
    # 渲染层只输出「可变数据」：参考图职能图号区间、用户要求和选中造型摘要。
    # 六镜头策略、职能描述与事实源约束由 Prompt 维护模板正文托管（配合条件段）。
    runtime = build_preview_prompt_runtime(styling_plan, image_inputs or [], storyboard_preview_user_instruction or {})
    return render_prompt_template(storyboard_prompt_config_key(styling_plan), runtime)


def build_preview_prompt_runtime(
    styling_plan: dict[str, Any],
    image_inputs: list[dict[str, Any]],
    storyboard_preview_user_instruction: dict[str, Any],
) -> dict[str, Any]:
    spans = render_image_reference_spans(image_inputs or [])
    return {
        "category_family": storyboard_category_family(styling_plan),
        "ref_model_identity": spans["model_identity"],
        "ref_garment": spans["garment"],
        "ref_shoot_environment": spans["shoot_environment"],
        "storyboard_preview_user_instruction": render_user_instruction_line(storyboard_preview_user_instruction or {}),
    }


def build_new_final_task_package(task_id: int, result_image_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gri.*, gp.preview_asset_id, gp.environment_asset_id, grs.styling_id,
                   grs.input_version_id, grs.user_instruction_json AS result_set_user_instruction_json
            FROM generation_result_images_new gri
            JOIN generation_result_sets grs ON grs.id = gri.result_set_id
            JOIN generation_previews gp ON gp.id = grs.preview_id
            WHERE gri.id = ?
            """,
            (result_image_id,),
        ).fetchone()
    styling_plan = get_styling_plan_by_id(int(row["styling_id"]))
    image_inputs = build_new_image_inputs(
        task_id,
        int(row["styling_id"]),
        include_preview=True,
        preview_asset_id=row["preview_asset_id"],
        preview_grid_cell=int(row["sort_order"]),
        input_version_id=int(row["input_version_id"]),
        environment_asset_id=row["environment_asset_id"],
    )
    final_image_user_instruction = loads(
        row_value(row, "user_instruction_json"),
        loads(row_value(row, "result_set_user_instruction_json"), build_stage_user_instruction_context("final_image")),
    )
    prompt_config_key = final_image_prompt_config_key(styling_plan)
    prompt_runtime = build_final_prompt_runtime(int(row["sort_order"]), str(row["shot_role"]), styling_plan, image_inputs, final_image_user_instruction)
    prompt = render_prompt_template(prompt_config_key, prompt_runtime)
    return {
        "generation": {"stage": "final_image"},
        "prompt": prompt,
        "categoryFamily": storyboard_category_family(styling_plan),
        **prompt_execution_facts(prompt_config_key, prompt, prompt_runtime),
        "imageInputs": image_inputs,
        "finalImageUserInstruction": final_image_user_instruction,
        "stylingPlan": styling_plan,
        "shotRole": row["shot_role"],
    }


def build_new_final_prompt(
    shot_index: int,
    shot_role: str,
    styling_plan: dict[str, Any],
    image_inputs: list[dict[str, Any]] | None = None,
    final_image_user_instruction: dict[str, Any] | None = None,
) -> str:
    # 渲染层只输出「可变数据」：当前镜头各分段正文、分镜预览格号、参考图各职能图号区间、
    # 用户要求。表头、分镜参考话术与事实源约束由 Prompt 维护模板正文托管。
    runtime = build_final_prompt_runtime(shot_index, shot_role, styling_plan, image_inputs or [], final_image_user_instruction or {})
    return render_prompt_template(final_image_prompt_config_key(styling_plan), runtime)


def build_final_prompt_runtime(
    shot_index: int,
    shot_role: str,
    styling_plan: dict[str, Any],
    image_inputs: list[dict[str, Any]],
    final_image_user_instruction: dict[str, Any],
) -> dict[str, Any]:
    spans = render_image_reference_spans(image_inputs or [])
    selected = selected_outfit_from_plan(styling_plan)
    selected_codes = ensure_string_list(styling_plan.get("selected_material_codes"))
    runtime = {
        "preview_grid_cell": str(shot_index),
        "shot_styling": f"选中搭配服装：{'、'.join(selected_codes) if selected_codes else '无库内配角'}。{str(selected.get('wearing_notes') or '')}",
        "shot_global_directing": "严格复现对应分镜格的构图、角度、裁切与姿势，并保持商品和人物一致。",
        "shot_current": f"分镜第 {shot_index} 格，镜头角色 {shot_role}。",
        "shot_accessories": "仅保留分镜中已有的低存在感配饰，不新增遮挡商品的配饰。",
        "ref_model_identity": spans["model_identity"],
        "ref_preview_grid": spans["preview_grid"],
        "ref_garment": spans["garment"],
        "ref_shoot_environment": spans["shoot_environment"],
        "final_image_user_instruction": render_user_instruction_line(final_image_user_instruction or {}),
    }
    for index in range(1, 6):
        runtime[f"is_shot_{index}"] = "1" if int(shot_index) == index else ""
    return runtime


def get_styling_plan_by_id(styling_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT styling_plan_json FROM generation_stylings WHERE id = ?", (styling_id,)).fetchone()
    if not row:
        return {}
    return loads(row["styling_plan_json"], {})


def build_new_image_inputs(
    task_id: int,
    styling_id: int,
    include_preview: bool,
    preview_asset_id: int | None = None,
    preview_grid_cell: int | None = None,
    input_version_id: int | None = None,
    environment_asset_id: int | None = None,
) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    # 环境参考图（验证阶段，仅分镜预览）固定放在参考图最后一格，需预留 1 个额度。
    reserve_env = 1 if environment_asset_id else 0
    with get_db() as db:
        styling = db.execute(
            "SELECT selected_material_snapshot_json, input_version_id FROM generation_stylings WHERE id = ?",
            (styling_id,),
        ).fetchone()
        effective_input_version_id = input_version_id or (int(styling["input_version_id"]) if styling and styling["input_version_id"] else None)
        input_version = db.execute(
            "SELECT image_snapshot_json, model_snapshot_json FROM generation_task_input_versions WHERE id = ?",
            (effective_input_version_id,),
        ).fetchone() if effective_input_version_id is not None else None
    model_snapshot = loads(input_version["model_snapshot_json"], {}) if input_version else {}
    model_inputs = resolve_model_snapshot_image_inputs(model_snapshot, limit=2)
    inputs.extend({**item, "role": "model_identity"} for item in model_inputs[:2] if isinstance(item, dict))
    if include_preview and preview_asset_id:
        asset = get_media_asset_by_id(preview_asset_id)
        if asset:
            inputs.append(preview_grid_cell_image_input(asset, preview_grid_cell))
    hero_snapshot = loads(input_version["image_snapshot_json"], []) if input_version else []
    inputs.extend(image_input_from_asset(item.get("asset"), "hero_garment") for item in hero_snapshot[:3])
    selected_materials = loads(styling["selected_material_snapshot_json"], []) if styling else []
    remaining = max(0, 10 - len(inputs) - reserve_env)
    for garment in selected_materials:
        if remaining <= 0:
            break
        images = garment.get("images", [])
        if images:
            inputs.append(image_input_from_asset(images[0]["asset"], "styling_garment"))
            remaining -= 1
    if environment_asset_id:
        env_asset = get_media_asset_by_id(environment_asset_id)
        if env_asset:
            inputs.append(image_input_from_asset(env_asset, "shoot_environment"))
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


def preview_grid_cell_image_input(asset: dict[str, Any], cell: int | None) -> dict[str, Any]:
    if cell is None:
        return image_input_from_asset(asset, "preview_grid")
    source_key = str(asset.get("objectKey") or "")
    if not source_key or not object_exists(source_key):
        return image_input_from_asset(asset, "preview_grid")
    cell_key = preview_grid_cell_object_key(source_key, int(cell))
    if not object_exists(cell_key):
        content = crop_preview_grid_cell(get_bytes(source_key), int(cell))
        put_bytes(cell_key, content, "image/png")
    return {
        "role": "preview_grid",
        "assetId": asset.get("id"),
        "objectKey": cell_key,
        "sourceObjectKey": source_key,
        "filename": f"{Path(source_key).stem}-shot{int(cell)}.png",
        "url": asset.get("originalUrl"),
    }


def preview_grid_cell_object_key(source_key: str, cell: int) -> str:
    if cell < 1 or cell > 6:
        raise ValueError(f"Invalid preview grid cell: {cell}")
    path = Path(clean_key(source_key))
    return join_key(path.parent.as_posix(), "_derived", "final-cells", f"{path.stem}-shot{cell}.png")


def crop_preview_grid_cell(content: bytes, cell: int) -> bytes:
    if cell < 1 or cell > 6:
        raise ValueError(f"Invalid preview grid cell: {cell}")
    with Image.open(BytesIO(content)) as image:
        image = ImageOps.exif_transpose(image)
        width, height = image.size
        col = (cell - 1) % 3
        row = (cell - 1) // 3
        left = round(width * col / 3)
        right = round(width * (col + 1) / 3)
        top = round(height * row / 2)
        bottom = round(height * (row + 1) / 2)
        cropped = image.crop((left, top, right, bottom))
        if cropped.mode not in {"RGB", "RGBA"}:
            cropped = cropped.convert("RGBA" if "A" in cropped.getbands() else "RGB")
        output = BytesIO()
        cropped.save(output, format="PNG")
        return output.getvalue()


def get_default_model_image_inputs(limit: int = 2) -> list[dict[str, Any]]:
    with get_db() as db:
        row = db.execute("SELECT * FROM model_profiles ORDER BY is_default DESC, id LIMIT 1").fetchone()
    return model_image_inputs_from_row(row, limit=limit)


def model_image_inputs_from_row(row: Any, limit: int = 2) -> list[dict[str, Any]]:
    if not row:
        return []
    profile = loads(row["profile_json"], {})
    refs = ordered_head_references(profile, profile.get("headReferences") or {})
    inputs: list[dict[str, Any]] = []
    for angle, filename in refs.items():
        key = join_key(row["asset_dir"], filename)
        if filename and object_exists(key):
            # 未指定角度的图不要把内部键（__unassigned__xxx）当作 angle 传给生图模型。
            clean_angle = None if is_unassigned_head_reference_id(angle) else angle
            inputs.append({"role": "model_identity", "objectKey": key, "filename": filename, "angle": clean_angle})
        if len(inputs) >= limit:
            break
    return inputs


def resolve_model_snapshot_image_inputs(model_snapshot: dict[str, Any] | None, limit: int = 2) -> list[dict[str, Any]]:
    snapshot = model_snapshot if isinstance(model_snapshot, dict) else {}
    raw_inputs = snapshot.get("imageInputs") if isinstance(snapshot.get("imageInputs"), list) else []
    expected = [item for item in raw_inputs[:limit] if isinstance(item, dict)]
    existing = [
        {**item, "role": "model_identity"}
        for item in expected
        if item.get("objectKey") and object_exists(str(item.get("objectKey")))
    ]
    if expected and len(existing) == len(expected):
        return existing[:limit]

    with get_db() as db:
        row = None
        snapshot_id = snapshot.get("id")
        if snapshot_id is not None:
            row = db.execute("SELECT * FROM model_profiles WHERE id = ?", (snapshot_id,)).fetchone()
        if row is None:
            model_id = str(snapshot.get("modelId") or snapshot.get("model_id") or "").strip()
            if model_id:
                row = db.execute("SELECT * FROM model_profiles WHERE model_id = ?", (model_id,)).fetchone()
    fallback = model_image_inputs_from_row(row, limit=limit)
    if fallback:
        return fallback
    has_explicit_snapshot = bool(expected or snapshot.get("id") is not None or snapshot.get("modelId") or snapshot.get("model_id"))
    if has_explicit_snapshot:
        raise RuntimeError("模特参考图缺失或已被删除，请重新确认数字模特后再生成。")
    return get_default_model_image_inputs(limit=limit)


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
    content = str(row["content"] or "")
    return {
        "key": str(row["key"]),
        "groupType": str(row["group_type"] or "assembly"),
        "title": str(row["title"] or row["key"]),
        "description": str(row["description"] or ""),
        "content": content,
        "contentHash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
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


def render_prompt_config(
    key: str,
    runtime: dict[str, Any] | None = None,
    rows: dict[str, Any] | None = None,
    content_overrides: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    diagnostics: dict[str, Any] = {
        "usedSegments": [],
        "missingPlaceholders": [],
        "referencedPlaceholders": [],
    }
    rows = rows if rows is not None else get_prompt_config_rows()
    runtime = runtime or {}
    content_overrides = content_overrides or {}

    def content_for(name: str) -> str | None:
        if name in content_overrides:
            return content_overrides[name]
        return prompt_config_content(name, rows)

    def resolve_placeholder(name: str, stack: list[str]) -> str:
        diagnostics["referencedPlaceholders"].append(name)
        if name.startswith("segment."):
            if name in stack:
                diagnostics["missingPlaceholders"].append(f"{name} (循环引用)")
                return f"[[循环引用:{name}]]"
            if name not in rows:
                diagnostics["missingPlaceholders"].append(name)
                return f"[[缺失分段:{name}]]"
            if name in rows and str(rows[name]["group_type"] or "") != "segment":
                diagnostics["missingPlaceholders"].append(name)
                return f"[[缺失分段:{name}]]"
            if name not in diagnostics["usedSegments"]:
                diagnostics["usedSegments"].append(name)
            segment_content = content_for(name) or ""
            if not segment_content.strip():
                diagnostics["missingPlaceholders"].append(f"{name} (空内容)")
            return render_text(segment_content, [*stack, name])
        if name.startswith("runtime."):
            runtime_name = name.removeprefix("runtime.")
            if runtime_name in LEGACY_EMPTY_RUNTIME_PLACEHOLDERS and runtime_name not in runtime:
                return ""
            value = runtime.get(runtime_name)
            if value is None:
                diagnostics["missingPlaceholders"].append(name)
                return f"[[缺失运行时:{name}]]"
            if isinstance(value, str):
                return value
            return dumps(value)
        diagnostics["missingPlaceholders"].append(name)
        return f"[[未知占位符:{name}]]"

    def placeholder_truthy(name: str) -> bool:
        # 条件段真值判定：不写诊断（缺失/空不是错误，只表示"不显示这段"）。
        if name.startswith("segment."):
            return bool((content_for(name) or "").strip())
        if name.startswith("runtime."):
            value = runtime.get(name.removeprefix("runtime."))
            if value is None:
                return False
            if isinstance(value, str):
                return bool(value.strip())
            return bool(value)
        return False

    def render_conditionals(text: str) -> str:
        def repl(match: "re.Match[str]") -> str:
            marker, name, inner = match.group(1), match.group(2), match.group(3)
            keep = placeholder_truthy(name) if marker == "#" else not placeholder_truthy(name)
            return inner if keep else ""

        previous = None
        while previous != text:  # 反复展开以支持嵌套条件段。
            previous = text
            text = PROMPT_SECTION_PATTERN.sub(repl, text)
        return text

    def render_text(text: str, stack: list[str]) -> str:
        text = render_conditionals(text)
        return PROMPT_PLACEHOLDER_PATTERN.sub(lambda match: resolve_placeholder(match.group(1), stack), text)

    source = content_for(key)
    if source is None:
        diagnostics["missingPlaceholders"].append(key)
        source = ""
    elif not source.strip():
        diagnostics["missingPlaceholders"].append(f"{key} (空内容)")
    rendered = re.sub(r"\n{3,}", "\n\n", render_text(source, [key])).strip() + "\n"
    for field in ("usedSegments", "missingPlaceholders", "referencedPlaceholders"):
        diagnostics[field] = sorted(set(diagnostics[field]))
    return rendered, diagnostics


def render_prompt_template(key: str, runtime: dict[str, Any] | None = None) -> str:
    rendered, diagnostics = render_prompt_config(key, runtime)
    if diagnostics.get("missingPlaceholders"):
        raise RuntimeError(f"Prompt 配置缺失或占位符未填充：{', '.join(diagnostics['missingPlaceholders'])}")
    return rendered


def prompt_execution_facts(key: str, prompt: str, runtime: dict[str, Any]) -> dict[str, Any]:
    """记录本次渲染真实使用的版本标识与动态事实，不复制 Prompt 正文。"""
    content = prompt_config_content(key) or ""
    return {
        "promptConfigKey": key,
        "promptContentHash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "renderedPromptHash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "promptRuntime": runtime,
    }


def sample_prompt_runtime() -> dict[str, Any]:
    # 结构化业务输入（选搭/标签）保持 JSON；面向模型的文本占位给出「已渲染」的样例文本，
    # 与运行时各 render_* 函数产物同形，便于「Prompt 维护」页预览真实效果。
    return {
        # AI 打标 / AI 选搭
        "styling_garment": dumps({"code": "LOOK-A", "image_count": 3, "image_roles": ["primary", "reference", "reference"]}),
        "garment_image_files": dumps([
            {"file": "image:101:front.jpg", "order": 1},
            {"file": "image:102:back.jpg", "order": 2},
            {"file": "image:103:fabric.jpg", "order": 3},
        ]),
        "hero_image_files": dumps([
            {"file": "image:201:hero-front.jpg", "order": 1},
            {"file": "image:202:hero-detail.jpg", "order": 2},
        ]),
        "candidate_retrieval": dumps([{"code": "LOOK-A", "ai_tags": {"一级品类": ["上装"], "色彩": ["白色"], "版型": ["合体"], "风格语义": "轻量上装服务主推裤装。"}}]),
        "styling_result": dumps({"hero_understanding": {"category": "裤装", "sub_category": "trousers", "shot_category_family": "bottom"}, "selected_material_codes": ["LOOK-A"], "reason_summary": "轻量上装服务主推裤装。"}),
        # 各阶段用户要求（已渲染为一句话）
        "user_requirement": "重点展示腰线，避免厚重外套。",
        "ai_styling_user_instruction": "不要厚重外套，优先轻盈通勤。",
        "storyboard_preview_user_instruction": "分镜姿态更舒展。",
        "final_image_user_instruction": "成片整体更干净，减少配饰存在感。",
        # 固定品类分镜 Prompt 的少量动态事实（自然语言，不传中间剧本 JSON）
        "category_family": "bottom",
        # 当前成片各分段（成片用；无表头）
        "preview_grid_cell": "1",
        "shot_styling": "造型：吊带裙配同色飘带\n穿法：单穿吊带裙",
        "shot_global_directing": "配饰边界：生成配饰低存在感",
        "shot_current": "格1（全身主视觉·全景）：\n  核心：主推款整体比例",
        "shot_accessories": "- jewelry：极简金色小耳圈",
        "is_shot_1": "1",
        "is_shot_2": "",
        "is_shot_3": "",
        "is_shot_4": "",
        "is_shot_5": "",
        # 参考图各职能图号区间（只出会变的图号）
        "ref_model_identity": "图1-2",
        "ref_preview_grid": "图3",
        "ref_garment": "图4-6",
        "ref_shoot_environment": "图7",
    }


def available_prompt_placeholders() -> list[dict[str, str]]:
    return [
        {"name": "segment.brand_info", "scope": "segment", "description": "品牌信息分段 Prompt"},
        {"name": "runtime.garment_image_files", "scope": "runtime", "description": "搭配服装图片挑选/排序使用的文件标识与候选顺序"},
        {"name": "runtime.styling_garment", "scope": "runtime", "description": "AI 打标使用的当前搭配服装 code 和图片输入摘要"},
        {"name": "runtime.hero_image_files", "scope": "runtime", "description": "主推款图片挑选/排序使用的文件标识与候选顺序"},
        {"name": "runtime.candidate_retrieval", "scope": "runtime", "description": "搭配服装库 READY 服饰的全量 AI 标签事实卡"},
        {"name": "runtime.ai_styling_user_instruction", "scope": "runtime", "description": "AI 智能选搭本次执行的用户介入要求"},
        {"name": "runtime.storyboard_preview_user_instruction", "scope": "runtime", "description": "分镜预览本次执行的用户介入要求"},
        {"name": "runtime.final_image_user_instruction", "scope": "runtime", "description": "最终成片或单张重生本次执行的用户介入要求"},
        {"name": "runtime.styling_result", "scope": "runtime", "description": "AI 选搭导演上下文，使用搭配服装 code 标识"},
        {"name": "runtime.category_family", "scope": "runtime", "description": "代码判定的拍摄图位族：top / bottom / one_piece"},
        # 成片：当前镜头各分段
        {"name": "runtime.preview_grid_cell", "scope": "runtime", "description": "当前成片对应分镜预览的格号（纯数字）"},
        {"name": "runtime.shot_styling", "scope": "runtime", "description": "当前成片造型正文（造型/穿法/选中搭配 code），无表头"},
        {"name": "runtime.shot_global_directing", "scope": "runtime", "description": "当前成片全局导演正文，无表头"},
        {"name": "runtime.shot_current", "scope": "runtime", "description": "当前成片单镜头正文，无表头"},
        {"name": "runtime.shot_accessories", "scope": "runtime", "description": "当前成片本镜头配饰正文，无表头；为空则条件段省略"},
        {"name": "runtime.is_shot_1", "scope": "runtime", "description": "当前成片为镜头 1 时有值，用于条件段"},
        {"name": "runtime.is_shot_2", "scope": "runtime", "description": "当前成片为镜头 2 时有值，用于条件段"},
        {"name": "runtime.is_shot_3", "scope": "runtime", "description": "当前成片为镜头 3 时有值，用于条件段"},
        {"name": "runtime.is_shot_4", "scope": "runtime", "description": "当前成片为镜头 4 时有值，用于条件段"},
        {"name": "runtime.is_shot_5", "scope": "runtime", "description": "当前成片为镜头 5 时有值，用于条件段"},
        # 参考图各职能图号区间：只出会变的图号，职能描述在模板正文托管
        {"name": "runtime.ref_model_identity", "scope": "runtime", "description": "模特身份参考图图号区间，如「图1-2」；无则空"},
        {"name": "runtime.ref_preview_grid", "scope": "runtime", "description": "分镜预览参考图图号（仅成片阶段），如「图3」；无则空"},
        {"name": "runtime.ref_garment", "scope": "runtime", "description": "主推款与搭配服装参考图的总图号区间，如「图4-6」；无则空"},
        {"name": "runtime.ref_shoot_environment", "scope": "runtime", "description": "拍摄环境参考图图号；无则空"},
    ]


def job_lease_until() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=settings.generation_image_timeout_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def preview_job_key(preview_id: int) -> str:
    return f"generation_preview:{preview_id}"


def final_set_job_key(result_set_id: int) -> str:
    return f"generation_result_set:{result_set_id}"


def auto_run_job_key(task_id: int) -> str:
    return f"generation_auto_run:{task_id}"


def schedule_auto_run_for_task(task_id: int) -> None:
    schedule_background_generation(run_auto_final_generation(task_id), job_key=auto_run_job_key(task_id))


AUTO_RUN_JOB_KEY_PREFIX = "generation_auto_run:"
AUTO_RUN_TERMINAL_TASK_STATUSES = {"ABANDONED"}


def auto_run_active_task_ids() -> set[int]:
    """当前占用 auto-run 并发额度的任务：状态运行中，或后台正在推进其 auto-run 协程。"""
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id FROM generation_tasks
            WHERE deleted_at IS NULL
              AND auto_run_target = ?
              AND status IN ('STYLING_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING')
            """,
            (AUTO_RUN_TARGET_FINAL,),
        ).fetchall()
    ids = {int(row["id"]) for row in rows}
    for key in list(active_background_generation_jobs):
        if key.startswith(AUTO_RUN_JOB_KEY_PREFIX):
            with suppress(ValueError):
                ids.add(int(key[len(AUTO_RUN_JOB_KEY_PREFIX):]))
    return ids


def pump_auto_run_queue() -> None:
    """并发闸门：按 auto_run_requested_at 顺序推进排队中的一键成片任务，超出上限的继续等待。"""
    active_ids = auto_run_active_task_ids()
    slots = settings.generation_auto_run_max_concurrency - len(active_ids)
    if slots <= 0:
        return
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id FROM generation_tasks
            WHERE deleted_at IS NULL
              AND auto_run_target = ?
              AND status NOT IN ('STYLING_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING', 'ABANDONED')
            ORDER BY COALESCE(auto_run_requested_at, updated_at), id
            """,
            (AUTO_RUN_TARGET_FINAL,),
        ).fetchall()
    for row in rows:
        if slots <= 0:
            return
        task_id = int(row["id"])
        if task_id in active_ids:
            continue
        schedule_auto_run_for_task(task_id)
        slots -= 1


def auto_run_queue_position(task_id: int, requested_at: str | None) -> int | None:
    """任务在一键成片队列中的近似位置（1 起）。"""
    with get_db() as db:
        row = db.execute(
            """
            SELECT COUNT(*) AS ahead FROM generation_tasks
            WHERE deleted_at IS NULL
              AND auto_run_target = ?
              AND status NOT IN ('STYLING_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING', 'ABANDONED')
              AND id != ?
              AND (
                COALESCE(auto_run_requested_at, updated_at) < COALESCE(?, '')
                OR (COALESCE(auto_run_requested_at, updated_at) = COALESCE(?, '') AND id < ?)
              )
            """,
            (AUTO_RUN_TARGET_FINAL, task_id, requested_at, requested_at, task_id),
        ).fetchone()
    return int(row["ahead"]) + 1 if row else None


def maybe_schedule_auto_run_for_task(task_id: int) -> None:
    pump_auto_run_queue()


def clear_task_auto_run(task_id: int) -> None:
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_tasks
            SET auto_run_target = NULL, auto_run_requested_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (now_iso(), task_id),
        )


async def run_auto_final_generation(task_id: int) -> None:
    try:
        while True:
            task = get_generation_task_row(task_id)
            if row_value(task, "auto_run_target") != AUTO_RUN_TARGET_FINAL:
                return
            if task["status"] in RUNNING_GENERATION_TASK_STATUSES:
                return
            if task["status"] == "ABANDONED":
                clear_task_auto_run(task_id)
                return
            step = next_auto_final_step(task)
            if step == "complete":
                clear_task_auto_run(task_id)
                return
            if step == "ready":
                ready_generation_task(task_id)
                continue
            if step == "styling":
                await run_select_generation_styling(task_id, trigger="auto_run")
                continue
            if step == "preview":
                await run_create_generation_preview(task_id, trigger="auto_run")
                return
            if step == "final":
                await run_generate_final_images(task_id, trigger="auto_run", continue_existing=True)
                return
            return
    except HTTPException:
        clear_task_auto_run(task_id)
        raise
    except Exception:
        clear_task_auto_run(task_id)
        raise


def next_auto_final_step(task: Any) -> str:
    if task["status"] == "DRAFT":
        return "ready"
    if task["status"] == "READY_FOR_STYLING":
        return "styling"
    if task["status"] == "STYLING_READY":
        return "preview"
    if task["status"] in {"PREVIEW_READY", "FINAL_PARTIAL"}:
        return "final"
    if task["status"] == "COMPLETED":
        return "complete"
    if not task["active_styling_id"]:
        return "styling"
    preview_id = row_value(task, "active_storyboard_preview_id") or row_value(task, "active_preview_id")
    if not preview_id or not generation_preview_has_asset(int(preview_id)):
        return "preview"
    result_set_id = row_value(task, "active_result_set_id")
    if result_set_id and generation_result_set_is_complete(int(result_set_id)):
        return "complete"
    return "final"


def generation_preview_has_asset(preview_id: int) -> bool:
    with get_db() as db:
        row = db.execute("SELECT preview_asset_id, status FROM generation_previews WHERE id = ?", (preview_id,)).fetchone()
    return bool(row and row["preview_asset_id"] and row["status"] == "SUCCEEDED")


def generation_result_set_is_complete(result_set_id: int) -> bool:
    with get_db() as db:
        rows = db.execute(
            "SELECT status FROM generation_result_images_new WHERE result_set_id = ? ORDER BY sort_order",
            (result_set_id,),
        ).fetchall()
    statuses = [row["status"] for row in rows]
    return len(statuses) >= 5 and all(status == "SUCCEEDED" for status in statuses[:5])


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
            SELECT gp.* FROM generation_previews gp
            LEFT JOIN generation_attempts ga ON ga.id = gp.active_attempt_id
            WHERE gp.status IN ('QUEUED', 'RUNNING')
              AND (
                gp.locked_until IS NULL OR gp.locked_until < ?
                OR (gp.status = 'RUNNING' AND ga.provider_request_id IS NOT NULL)
              )
            ORDER BY gp.updated_at, gp.id
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
            LEFT JOIN generation_attempts ga ON ga.id = gri.active_attempt_id
            WHERE grs.status = 'RUNNING'
              AND gri.status IN ('QUEUED', 'RUNNING')
              AND (
                gri.locked_until IS NULL OR gri.locked_until < ?
                OR (gri.status = 'RUNNING' AND ga.provider_request_id IS NOT NULL)
              )
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
        elif active_attempt_id:
            error_code, message = stale_attempt_error(active_attempt_id)
            fail_stale_attempt(active_attempt_id, message, error_code)
            with get_db() as db:
                db.execute(
                    """
                    UPDATE generation_previews
                    SET status = 'FAILED', locked_by = NULL, locked_until = NULL, updated_at = ?
                    WHERE id = ? AND status IN ('QUEUED', 'RUNNING')
                    """,
                    (now_iso(), int(row["id"])),
                )
                db.execute(
                    "UPDATE generation_tasks SET status = 'FAILED', auto_run_target = NULL, updated_at = ? WHERE id = ?",
                    (now_iso(), int(row["task_id"])),
                )
            continue
        else:
            attempt_id = create_attempt("generation_preview", int(row["id"]), "image-2")
        lease_generation_preview(int(row["id"]), attempt_id)
        schedule_background_generation(
            run_preview_generation(int(row["task_id"]), int(row["styling_id"]), int(row["id"]), attempt_id),
            job_key=key,
        )

    for row in result_sets:
        key = final_set_job_key(int(row["id"]))
        if key in active_background_generation_jobs:
            continue
        with get_db() as db:
            image_rows = db.execute(
                """
                SELECT gri.id, gri.status, gri.active_attempt_id FROM generation_result_images_new gri
                LEFT JOIN generation_attempts ga ON ga.id = gri.active_attempt_id
                WHERE gri.result_set_id = ?
                  AND gri.status IN ('QUEUED', 'RUNNING')
                  AND (
                    gri.locked_until IS NULL OR gri.locked_until < ?
                    OR (gri.status = 'RUNNING' AND ga.provider_request_id IS NOT NULL)
                  )
                ORDER BY gri.sort_order, gri.id
                """,
                (row["id"], timestamp),
            ).fetchall()
        image_ids: list[int] = []
        if image_rows:
            resume_attempts_by_image_id: dict[int, int] = {}
            for image in image_rows:
                active_attempt_id = int(image["active_attempt_id"]) if "active_attempt_id" in image.keys() and image["active_attempt_id"] else None
                if attempt_has_provider_request_id(active_attempt_id):
                    resume_attempts_by_image_id[int(image["id"])] = int(active_attempt_id)
                    image_ids.append(int(image["id"]))
                elif active_attempt_id and image["status"] == "QUEUED":
                    resume_attempts_by_image_id[int(image["id"])] = int(active_attempt_id)
                    image_ids.append(int(image["id"]))
                elif active_attempt_id:
                    error_code, message = stale_attempt_error(active_attempt_id)
                    fail_stale_attempt(active_attempt_id, message, error_code)
                    with get_db() as db:
                        db.execute(
                            """
                            UPDATE generation_result_images_new
                            SET status = 'FAILED', locked_by = NULL, locked_until = NULL, updated_at = ?
                            WHERE id = ? AND status IN ('QUEUED', 'RUNNING')
                            """,
                            (now_iso(), int(image["id"])),
                        )
                else:
                    image_ids.append(int(image["id"]))
            if image_ids:
                schedule_background_generation(
                    run_final_generation(int(row["task_id"]), int(row["id"]), image_ids, resume_attempts_by_image_id=resume_attempts_by_image_id),
                    job_key=key,
                )
            else:
                with get_db() as db:
                    reconcile_final_result_set(db, int(row["id"]), int(row["task_id"]))

    for row in terminal_result_sets:
        with get_db() as db:
            reconcile_final_result_set(db, int(row["id"]), int(row["task_id"]))
    pump_auto_run_queue()


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


# 成片回传的退避重试间隔（秒）；总时长由 GENERATION_DELIVERY_TIMEOUT_SECONDS 限定。
DELIVERY_RETRY_BACKOFF_SECONDS: tuple[int, ...] = (5, 15, 30, 60, 120, 300)

# 状态查询连续失败时的退避上限（秒）：查询失败不判死，只放慢节奏。
STATUS_CHECK_ERROR_BACKOFF_CAP_SECONDS = 30

# provider 明确返回瞬时生成失败时，允许重新创建一次生成任务。提交结果未知不在此列。
PROVIDER_GENERATION_RETRY_LIMIT = 1


class SubmissionUnknownRetryGuard:
    """同批提交未知熔断器：达到阈值后，剩余镜头不再放大自动重提。"""

    def __init__(self, threshold: int):
        self.threshold = max(1, int(threshold))
        self.unknown_count = 0

    def allow_retry(self) -> bool:
        self.unknown_count += 1
        return self.unknown_count < self.threshold


async def generate_image_asset(
    task_package: dict[str, Any],
    output_key: str,
    attempt_id: int | None = None,
    unknown_retry_guard: SubmissionUnknownRetryGuard | None = None,
) -> tuple[int, dict[str, Any]]:
    """完整生成一张图：上传参考图 → 提交 → 轮询 → 成片回传。

    健壮性契约（成功 > 慢 > 失败）：

    - 状态查询失败绝不判死本张图，退避后继续轮询，直到生成阶段预算耗尽。
    - provider 成功后进入回传阶段；回传失败在回传预算内自动退避重试，
      耗尽后以 DELIVERY_FAILED 落败——providerRequestId 保留，可零成本重新回传。
    - 阶段进度（上传张数、排队、生成、回传重试次数）持久化在 attempt 上，
      前端轮询任务详情即可看到实时阶段和分段耗时。
    """
    if not settings.is_image_provider_configured():
        raise RuntimeError(image_provider_missing_message())
    progress = AttemptProgressRecorder(attempt_id)
    payload = get_attempt_provider_resume_snapshot(attempt_id)
    image_input_total = len(task_package.get("imageInputs") or [])
    generation_retries = int(payload.get("generationRetries") or 0) if payload else 0
    unknown_submission_retries = int(payload.get("unknownSubmissionRetries") or 0) if payload else 0
    existing_history = payload.get("providerRequestHistory") if payload else None
    provider_request_history: list[str] = (
        [str(item) for item in existing_history if item]
        if isinstance(existing_history, list)
        else []
    )
    if payload and payload.get("providerRequestId") and str(payload["providerRequestId"]) not in provider_request_history:
        provider_request_history.append(str(payload["providerRequestId"]))
    submit_task_package = dict(task_package)

    def report_upload(completed: int, total: int) -> None:
        progress.update(PHASE_UPLOAD, completed=completed, total=total)
        if completed >= total:
            progress.begin(PHASE_SUBMIT, generationRetry=generation_retries)

    while True:
        if payload is None:
            client_business_id = f"ruyi_img_a{attempt_id or 0}_r{generation_retries}"
            submit_task_package = {**submit_task_package, "clientBusinessId": client_business_id}
            progress.begin(PHASE_PREPARE)
            progress.begin(PHASE_UPLOAD, completed=0, total=image_input_total, generationRetry=generation_retries)
            try:
                payload = await asyncio.to_thread(run_image_provider_submit, submit_task_package, output_key, report_upload)
            except ImageProviderError as exc:
                if classify_attempt_error(exc) != ERROR_SUBMISSION_UNKNOWN:
                    raise
                unknown_snapshot = getattr(exc, "provider_response", None)
                if not isinstance(unknown_snapshot, dict):
                    unknown_snapshot = {}
                uploaded_inputs = unknown_snapshot.get("imageInputs")
                if isinstance(uploaded_inputs, list) and uploaded_inputs:
                    submit_task_package = {**submit_task_package, "imageInputs": uploaded_inputs}
                batch_retry_allowed = (
                    unknown_retry_guard.allow_retry()
                    if unknown_retry_guard is not None
                    else True
                )
                retry_allowed = (
                    unknown_submission_retries < settings.generation_unknown_submission_retry_limit
                    and batch_retry_allowed
                )
                snapshot = {
                    **unknown_snapshot,
                    "providerRequestHistory": provider_request_history,
                    "generationRetries": generation_retries,
                    "unknownSubmissionRetries": unknown_submission_retries,
                }
                if not retry_allowed:
                    snapshot["status"] = "SUBMISSION_UNKNOWN"
                    persist_attempt_provider_progress(attempt_id, snapshot)
                    exc.provider_response = snapshot
                    raise
                unknown_submission_retries += 1
                snapshot.update(
                    {
                        "status": "SUBMISSION_RETRY_WAIT",
                        "unknownSubmissionRetries": unknown_submission_retries,
                    }
                )
                progress.begin(
                    PHASE_SUBMIT,
                    unknownSubmissionRetry=unknown_submission_retries,
                    retryDelaySeconds=settings.generation_unknown_submission_retry_delay_seconds,
                )
                prepare_attempt_provider_resubmission(attempt_id, snapshot)
                await asyncio.sleep(settings.generation_unknown_submission_retry_delay_seconds)
                # 官方支持用 client_business_id 走同一个状态接口查询。先确认原任务，
                # 只有明确 404 才补提；查询本身仍不确定时停止，避免重复任务/重复计费。
                lookup_snapshot = {
                    **snapshot,
                    "providerRequestId": client_business_id,
                    "clientBusinessId": client_business_id,
                }
                try:
                    recovered = await asyncio.to_thread(run_image_provider_check, lookup_snapshot, output_key)
                except ImageProviderError as lookup_exc:
                    if classify_attempt_error(lookup_exc) == ERROR_TASK_NOT_FOUND:
                        payload = None
                        continue
                    snapshot["status"] = "SUBMISSION_UNKNOWN"
                    snapshot["reconciliationError"] = str(lookup_exc)[:500]
                    persist_attempt_provider_progress(attempt_id, snapshot)
                    exc.provider_response = snapshot
                    raise exc from lookup_exc
                payload = {**snapshot, **recovered, "clientBusinessId": client_business_id}
            if payload.get("providerRequestId"):
                provider_request_history.append(str(payload["providerRequestId"]))
            payload = {
                **payload,
                "providerRequestHistory": provider_request_history,
                "generationRetries": generation_retries,
                "unknownSubmissionRetries": unknown_submission_retries,
            }
            persist_attempt_provider_progress(attempt_id, payload)

        if payload.get("status") not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            payload = await poll_provider_until_terminal(payload, output_key, attempt_id, progress)
        if payload.get("status") == "SUCCEEDED":
            break

        if payload.get("status") in {"FAILED", "CANCELLED"}:
            terminal_error = str(payload.get("error") or payload.get("message") or payload.get("providerStatus") or "")
            provider_error = payload.get("providerError")
            if not isinstance(provider_error, dict):
                provider_error = normalize_toapis_error(
                    payload.get("providerStatusResponse") if isinstance(payload.get("providerStatusResponse"), dict) else {},
                    fallback_message=terminal_error,
                )
                payload["providerError"] = provider_error
            error_code = str(provider_error.get("code") or ERROR_PROVIDER_FAILED)
        else:
            error_code = ERROR_PROVIDER_TIMEOUT
        if (
            payload.get("status") == "FAILED"
            and is_generation_retryable_error(error_code)
            and generation_retries < PROVIDER_GENERATION_RETRY_LIMIT
        ):
            uploaded_inputs = payload.get("imageInputs")
            if isinstance(uploaded_inputs, list) and uploaded_inputs:
                submit_task_package = {**task_package, "imageInputs": uploaded_inputs}
            generation_retries += 1
            progress.begin(
                PHASE_SUBMIT,
                generationRetry=generation_retries,
                previousProviderRequestIds=provider_request_history,
                previousError=terminal_error[:500],
            )
            prepare_attempt_provider_resubmission(
                attempt_id,
                {
                    **payload,
                    "providerRequestHistory": provider_request_history,
                    "generationRetries": generation_retries,
                },
            )
            payload = None
            continue
        raise ImageProviderError(
            f"图片生成未成功，providerRequestId={payload.get('providerRequestId')}: {payload}",
            payload,
            error_code=error_code,
        )

    if not payload.get("outputPath"):
        payload = await deliver_with_retries(payload, output_key, attempt_id, progress)
    payload = {
        **payload,
        "submissionMode": "completed",
        "providerRequestHistory": provider_request_history,
        "generationRetries": generation_retries,
        "unknownSubmissionRetries": unknown_submission_retries,
    }
    persist_attempt_provider_progress(attempt_id, payload)

    if payload.get("provider") == "placeholder":
        raise RuntimeError("Image-2 provider 返回占位图，拒绝将占位图记为生成成功。")
    progress.begin(PHASE_STORE)
    try:
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
    except Exception as exc:
        raise GenerationPhaseError(
            f"provider 已完成，但本地成片登记失败: {exc}",
            ERROR_SYSTEM_ERROR,
            provider_response=payload,
        ) from exc
    progress.finish()
    return asset_id, payload


async def poll_provider_until_terminal(
    payload: dict[str, Any],
    output_key: str,
    attempt_id: int | None,
    progress: AttemptProgressRecorder,
) -> dict[str, Any]:
    """轮询 provider 直到终态或生成预算耗尽；单次查询失败只退避、不判死。"""
    if progress.snapshot.get("current") != PHASE_GENERATE:
        progress.begin(PHASE_QUEUE)
    deadline = time.time() + settings.generation_image_timeout_seconds
    consecutive_errors = 0
    while time.time() < deadline:
        await asyncio.sleep(min(3 * (2 ** min(consecutive_errors, 4)), STATUS_CHECK_ERROR_BACKOFF_CAP_SECONDS) if consecutive_errors else 3)
        try:
            latest = await asyncio.to_thread(run_image_provider_check, payload, output_key)
        except Exception as exc:
            # 鉴权/余额类错误重试无用（余额还会继续烧），立即抛出停止轮询；
            # 网关/连接/限流等瞬时错误只退避、不判死，续查到生成预算耗尽。
            status_error_code = classify_attempt_error(exc)
            if not status_error_code:
                raise ImageProviderError(
                    f"provider 状态查询发生本地系统错误: {exc}",
                    payload,
                    error_code=ERROR_SYSTEM_ERROR,
                ) from exc
            if not is_status_check_retryable_error(status_error_code):
                raise ImageProviderError(str(exc), payload, error_code=status_error_code) from exc
            consecutive_errors += 1
            current_phase = PHASE_GENERATE if progress.snapshot.get("current") == PHASE_GENERATE else PHASE_QUEUE
            progress.update(current_phase, statusCheckErrors=consecutive_errors, lastStatusCheckError=str(exc)[:500])
            continue
        consecutive_errors = 0
        payload = {**payload, **latest}
        persist_attempt_provider_progress(attempt_id, payload)
        provider_status = str(payload.get("providerStatus") or "").upper()
        if provider_status in {"IN_PROGRESS", "PROCESSING", "RUNNING"}:
            provider_progress = provider_progress_percent(payload)
            if provider_progress is None or provider_progress > 0:
                progress.begin(PHASE_GENERATE, providerProgress=provider_progress)
            else:
                progress.update(PHASE_QUEUE, providerStatus=provider_status, providerProgress=provider_progress)
        if payload.get("status") == "SUCCEEDED" and progress.snapshot.get("current") == PHASE_QUEUE:
            duration_seconds = provider_completed_duration_seconds(payload)
            detail = {"providerProgress": 100}
            if duration_seconds is not None:
                detail["durationSeconds"] = duration_seconds
            progress.begin(PHASE_GENERATE, **detail)
        if payload.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
    return payload


def provider_progress_percent(payload: dict[str, Any]) -> float | None:
    """Return provider-reported progress when available.

    ToAPIs can report ``status=in_progress`` while its dashboard still treats the
    task as waiting/preparing. In that state ``progress`` is usually 0, so the UI
    should keep charging that time to the queue phase rather than to AI
    generation. Providers that do not expose progress keep the old behavior.
    """
    status_response = payload.get("providerStatusResponse")
    raw = status_response.get("progress") if isinstance(status_response, dict) else payload.get("progress")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def provider_completed_duration_seconds(payload: dict[str, Any]) -> int | None:
    status_response = payload.get("providerStatusResponse")
    if not isinstance(status_response, dict):
        return None
    try:
        created_at = int(status_response["created_at"])
        completed_at = int(status_response["completed_at"])
    except (KeyError, TypeError, ValueError):
        return None
    duration = completed_at - created_at
    return duration if duration >= 0 else None


async def deliver_with_retries(
    payload: dict[str, Any],
    output_key: str,
    attempt_id: int | None,
    progress: AttemptProgressRecorder,
) -> dict[str, Any]:
    """provider 已成功，把成片回传到对象存储；回传预算内自动退避重试。"""
    progress.begin(PHASE_DELIVER, retries=0)
    deadline = time.time() + settings.generation_delivery_timeout_seconds
    retries = 0
    last_error: Exception | None = None
    while True:
        try:
            delivery = await asyncio.to_thread(run_image_provider_deliver, payload, output_key)
            payload = {**payload, **delivery}
            persist_attempt_provider_progress(attempt_id, payload)
            return payload
        except Exception as exc:
            last_error = exc
            delivery_error_code = classify_generation_attempt_error(exc)
            if delivery_error_code != ERROR_DELIVERY_FAILED:
                raise ImageProviderError(
                    f"provider 已完成，但本地交付阶段失败: {exc}",
                    getattr(exc, "provider_response", None) or payload,
                    error_code=delivery_error_code,
                ) from exc
            wait = DELIVERY_RETRY_BACKOFF_SECONDS[min(retries, len(DELIVERY_RETRY_BACKOFF_SECONDS) - 1)]
            retries += 1
            progress.update(PHASE_DELIVER, retries=retries, lastError=str(exc)[:500])
            if time.time() + wait > deadline:
                break
            await asyncio.sleep(wait)
    raise ImageProviderError(
        f"成片生成已在 provider 侧完成，但回传失败（已重试 {retries} 次），"
        f"providerRequestId={payload.get('providerRequestId')}: {last_error}",
        payload,
        error_code=ERROR_DELIVERY_FAILED,
    )


def run_image_provider_submit(task_package: dict[str, Any], output_key: str, on_progress: Any = None) -> dict[str, Any]:
    return asyncio.run(get_image_provider().submit(task_package, output_key, on_progress))


def run_image_provider_check(provider_response: dict[str, Any], output_key: str) -> dict[str, Any]:
    return asyncio.run(get_image_provider().check(provider_refresh_row(provider_response, output_key)))


def run_image_provider_deliver(provider_response: dict[str, Any], output_key: str) -> dict[str, Any]:
    status_response = provider_response.get("providerStatusResponse")
    return asyncio.run(
        get_image_provider().deliver(
            provider_refresh_row(provider_response, output_key),
            status_response if isinstance(status_response, dict) else {},
        )
    )


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


async def generate_single_final_image(
    task_id: int,
    result_set_id: int,
    image_id: int,
    attempt_id: int | None = None,
    trigger: str = "manual_step",
    balance_stop: asyncio.Event | None = None,
    unknown_retry_guard: SubmissionUnknownRetryGuard | None = None,
) -> int:
    if balance_stop is not None and balance_stop.is_set():
        # 同批已有成片撞到余额不足，本图直接落 FAILED(INSUFFICIENT_BALANCE)，不再提交 provider。
        stop_attempt_id = attempt_id or create_attempt("generation_result_image", image_id, "image-2")
        finish_attempt(
            stop_attempt_id,
            "FAILED",
            error_message="ToAPIs 余额不足，已跳过本张成片提交（同批已检测到余额不足）。",
            error_code=ERROR_INSUFFICIENT_BALANCE,
        )
        with get_db() as db:
            db.execute(
                """
                UPDATE generation_result_images_new
                SET status = 'FAILED', active_attempt_id = ?, locked_by = NULL, locked_until = NULL, updated_at = ?
                WHERE id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                (stop_attempt_id, now_iso(), image_id),
            )
        return 0
    if attempt_id is None:
        attempt_id = create_attempt("generation_result_image", image_id, "image-2")
    lease_generation_result_image(image_id, attempt_id)
    with get_db() as db:
        row = get_final_image_generation_state(db, image_id)
        if final_image_generation_cancelled(row, result_set_id):
            finish_attempt(attempt_id, "CANCELLED")
            return 0
        db.execute(
            """
            UPDATE generation_result_images_new
            SET status = 'RUNNING', active_attempt_id = ?, updated_at = ?
            WHERE id = ? AND status IN ('QUEUED', 'RUNNING')
            """,
            (attempt_id, now_iso(), image_id),
        )
    try:
        task_package = build_new_final_task_package(task_id, image_id)
        trigger = preserved_attempt_invocation_mode(attempt_id) or trigger
        task_package["invocation"] = {"mode": trigger, "target": "final_image"}
        if not attempt_has_provider_request_id(attempt_id):
            update_attempt_request_snapshot(attempt_id, task_package)
        with get_db() as db:
            sort_order = db.execute("SELECT sort_order FROM generation_result_images_new WHERE id = ?", (image_id,)).fetchone()["sort_order"]
        output_key = join_key("generation-tasks", str(task_id), "final", f"{result_set_id}-{sort_order}.png")
        asset_id, provider_response = await generate_image_asset(
            task_package,
            output_key,
            attempt_id=attempt_id,
            unknown_retry_guard=unknown_retry_guard,
        )
    except Exception as exc:
        provider_response = getattr(exc, "provider_response", None)
        with get_db() as db:
            current = db.execute(
                final_image_generation_state_sql(),
                (image_id,),
            ).fetchone()
            if final_image_generation_cancelled(current, result_set_id):
                finish_attempt(attempt_id, "CANCELLED", response_snapshot=provider_response, error_message=str(exc))
                persist_attempt_provider_request_id(attempt_id, provider_response)
                return 0
        final_error_code = classify_generation_attempt_error(exc)
        if balance_stop is not None and is_balance_error(final_error_code):
            balance_stop.set()
        finish_attempt(attempt_id, "FAILED", response_snapshot=provider_response, error_message=str(exc), error_code=final_error_code)
        persist_attempt_provider_request_id(attempt_id, provider_response)
        with get_db() as db:
            db.execute(
                """
                UPDATE generation_result_images_new
                SET status = 'FAILED', active_attempt_id = ?, locked_by = NULL, locked_until = NULL, updated_at = ?
                WHERE id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                (attempt_id, now_iso(), image_id),
            )
        raise
    with get_db() as db:
        current = db.execute(
            final_image_generation_state_sql(),
            (image_id,),
        ).fetchone()
        if final_image_generation_cancelled(current, result_set_id):
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
            WHERE id = ? AND status IN ('QUEUED', 'RUNNING')
            """,
            (asset_id, attempt_id, now_iso(), image_id),
        )
    return asset_id


def preserved_attempt_invocation_mode(attempt_id: int | None) -> str | None:
    if not attempt_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT request_snapshot_json FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
    snapshot = loads(row["request_snapshot_json"], {}) if row else {}
    invocation = snapshot.get("invocation") if isinstance(snapshot, dict) else None
    mode = invocation.get("mode") if isinstance(invocation, dict) else None
    return str(mode) if mode in {"manual_retry"} else None


def final_image_generation_state_sql() -> str:
    return """
        SELECT gri.status AS image_status,
               grs.id AS result_set_id,
               grs.status AS result_set_status,
               gt.status AS task_status,
               gt.active_result_set_id
        FROM generation_result_images_new gri
        JOIN generation_result_sets grs ON grs.id = gri.result_set_id
        JOIN generation_tasks gt ON gt.id = grs.task_id
        WHERE gri.id = ?
        """


def get_final_image_generation_state(db: Any, image_id: int) -> Any:
    return db.execute(final_image_generation_state_sql(), (image_id,)).fetchone()


def final_image_generation_cancelled(row: Any, result_set_id: int) -> bool:
    if not row:
        return True
    return (
        row["image_status"] == "CANCELLED"
        or row["result_set_status"] in {"CANCELLED", "SUPERSEDED"}
        or row["task_status"] == "CANCELLED"
        or row["active_result_set_id"] != result_set_id
    )


def serialize_generation_task(row: Any, include_detail: bool = True, include_debug: bool = False) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "userRequirement": row_value(row, "user_requirement_text", ""),
        "stageUserInputs": {
            "aiStyling": row_value(row, "ai_styling_user_instruction_text", ""),
            "storyboardPreview": row_value(row, "storyboard_preview_user_instruction_text", ""),
            "finalImage": row_value(row, "final_image_user_instruction_text", ""),
        },
        "currentInputVersionId": row["current_input_version_id"],
        "activeStylingId": row["active_styling_id"],
        "activePreviewId": row["active_preview_id"],
        "activeStoryboardPreviewId": row["active_storyboard_preview_id"] if "active_storyboard_preview_id" in row.keys() else row["active_preview_id"],
        "activeResultSetId": row["active_result_set_id"],
        "batchId": row_value(row, "batch_id"),
        "autoRunTarget": row_value(row, "auto_run_target"),
        "autoRunRequestedAt": row_value(row, "auto_run_requested_at"),
        "autoRunQueued": False,
        "autoRunQueuePosition": None,
        "archivedAt": row["archived_at"] if "archived_at" in row.keys() else None,
        "archiveReason": row["archive_reason"] if "archive_reason" in row.keys() else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if (
        row_value(row, "auto_run_target") == AUTO_RUN_TARGET_FINAL
        and row["status"] not in RUNNING_GENERATION_TASK_STATUSES
        and row["status"] not in AUTO_RUN_TERMINAL_TASK_STATUSES
        and int(row["id"]) not in auto_run_active_task_ids()
    ):
        item["autoRunQueued"] = True
        item["autoRunQueuePosition"] = auto_run_queue_position(int(row["id"]), row_value(row, "auto_run_requested_at"))
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
        "userRequirement": loads(row_value(row, "user_requirement_json"), build_user_requirement_context({})),
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
    if not include_debug:
        styling_plan = strip_debug_response_fields(styling_plan)
    item = {
        "id": row["id"],
        "taskId": row["task_id"],
        "inputVersionId": row["input_version_id"],
        "sourceMaterialIds": loads(row["source_material_ids_json"], []),
        "candidateMaterialIds": loads(row["candidate_material_ids_json"], []) if "candidate_material_ids_json" in row.keys() else [],
        "selectedMaterialIds": loads(row["selected_material_ids_json"], []),
        "selectedMaterials": loads(row["selected_material_snapshot_json"], []),
        "userInstruction": loads(row_value(row, "user_instruction_json"), {}),
        "userRequirement": loads(row_value(row, "user_requirement_json"), {}),
        "stylingPlan": styling_plan,
        "heroUnderstanding": loads(row["hero_understanding_json"], {}) if "hero_understanding_json" in row.keys() else styling_plan.get("hero_understanding", {}),
        "outfitCandidates": loads(row["outfit_candidates_json"], []) if "outfit_candidates_json" in row.keys() else styling_plan.get("outfit_candidates", []),
        "criticScores": loads(row["critic_scores_json"], {}) if "critic_scores_json" in row.keys() else get_selected_outfit_scores(styling_plan),
        "rejectedCandidates": loads(row["rejected_candidates_json"], []) if "rejected_candidates_json" in row.keys() else styling_plan.get("rejected_candidates", []),
        "qualitySignals": styling_plan.get("quality_signals", []) if isinstance(styling_plan.get("quality_signals"), list) else [],
        "userRequirementResolution": styling_plan.get("user_requirement_resolution", {}) if isinstance(styling_plan.get("user_requirement_resolution"), dict) else {},
        "reasonSummary": row["reason_summary"],
        "warnings": loads(row["warnings_json"], []),
        "status": row["status"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if include_debug:
        item["candidateRetrievalSnapshot"] = loads(row["candidate_retrieval_snapshot_json"], {}) if "candidate_retrieval_snapshot_json" in row.keys() else {}
    else:
        item = strip_debug_response_fields(item)
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
        "userInstruction": loads(row_value(row, "user_instruction_json"), {}),
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
        "previewId": row["preview_id"] if "preview_id" in row.keys() else None,
        "userInstruction": loads(row_value(row, "user_instruction_json"), {}),
        "images": [
            {
                "id": image["id"],
                "sortOrder": image["sort_order"],
                "shotRole": image["shot_role"],
                "status": image["status"],
                "userInstruction": loads(row_value(image, "user_instruction_json"), {}),
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


GENERATION_ATTEMPT_OWNER_TYPES = (
    "generation_styling",
    "generation_preview",
    "generation_result_image",
)


def serialize_attempt_detail(row: Any) -> dict[str, Any]:
    request_snapshot = loads(row["request_snapshot_json"], {})
    response_snapshot = loads(row["response_snapshot_json"], {})
    provider_error = response_snapshot.get("providerError") if isinstance(response_snapshot, dict) else None
    if not isinstance(provider_error, dict) and isinstance(response_snapshot, dict):
        provider_error = normalize_toapis_error(
            response_snapshot.get("providerStatusResponse")
            if isinstance(response_snapshot.get("providerStatusResponse"), dict)
            else {},
            fallback_message=str(row["error_message"] or ""),
        )
    reason = provider_error.get("upstreamMessage") if isinstance(provider_error, dict) else None
    error = provider_error_presentation(row_value(row, "error_code"), str(reason or "")) if row["status"] == "FAILED" else None
    return {
        "id": row["id"],
        "ownerType": row["owner_type"],
        "ownerId": row["owner_id"],
        "provider": row["provider"],
        "status": row["status"],
        "providerRequestId": row_value(row, "provider_request_id"),
        "startedAt": row["started_at"],
        "completedAt": row["completed_at"],
        "failedAt": row["failed_at"],
        "errorCode": row_value(row, "error_code"),
        "errorMessage": error["message"] if error else row["error_message"],
        "technicalMessage": row["error_message"],
        "error": error,
        "requestFacts": request_snapshot,
        "responseFacts": response_snapshot,
        "modelInput": actual_model_input_from_snapshot(request_snapshot),
        "modelOutput": actual_model_output_from_snapshot(response_snapshot),
        "retryOfAttemptId": row_value(row, "retry_of_attempt_id"),
        "retryMode": attempt_retry_mode(row, request_snapshot),
        "retryIndex": attempt_retry_index(row),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def get_task_attempts(task_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT ga.* FROM generation_attempts ga
            WHERE (ga.owner_type = 'generation_styling' AND ga.owner_id = ?)
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
            (task_id, task_id, task_id),
        ).fetchall()
    return [serialize_attempt_detail(row) for row in rows]


def get_attempt_summary(attempt_id: int | None) -> dict[str, Any] | None:
    if not attempt_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if not row:
        return None
    return serialize_attempt_summary(row)


def serialize_attempt_summary(row: Any) -> dict[str, Any]:
    request_snapshot = loads(row_value(row, "request_snapshot_json"), {})
    response_snapshot = loads(row_value(row, "response_snapshot_json"), {})
    provider_error = response_snapshot.get("providerError") if isinstance(response_snapshot, dict) else None
    if not isinstance(provider_error, dict) and isinstance(response_snapshot, dict):
        provider_error = normalize_toapis_error(
            response_snapshot.get("providerStatusResponse")
            if isinstance(response_snapshot.get("providerStatusResponse"), dict)
            else {},
            fallback_message=str(row["error_message"] or ""),
        )
    reason = provider_error.get("upstreamMessage") if isinstance(provider_error, dict) else None
    error = provider_error_presentation(row_value(row, "error_code"), str(reason or "")) if row["status"] == "FAILED" else None
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
        "errorCode": row_value(row, "error_code"),
        "errorMessage": error["message"] if error else row["error_message"],
        "error": error,
        "progress": loads(row_value(row, "progress_json"), None),
        "outputAssetId": row["output_asset_id"],
        "retryOfAttemptId": row_value(row, "retry_of_attempt_id"),
        "retryMode": attempt_retry_mode(row, request_snapshot),
        "retryIndex": attempt_retry_index(row),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def attempt_retry_mode(row: Any, request_snapshot: dict[str, Any]) -> str | None:
    if not row_value(row, "retry_of_attempt_id"):
        return None
    invocation = request_snapshot.get("invocation") if isinstance(request_snapshot, dict) else None
    mode = invocation.get("mode") if isinstance(invocation, dict) else None
    if mode == "manual_redeliver":
        return "redeliver"
    if mode == "manual_retry":
        return "regenerate"
    return "retry"


def attempt_retry_index(row: Any) -> int | None:
    if not row_value(row, "retry_of_attempt_id"):
        return None
    with get_db() as db:
        previous = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM generation_attempts
            WHERE owner_type = ? AND owner_id = ? AND retry_of_attempt_id IS NOT NULL AND id <= ?
            """,
            (row["owner_type"], row["owner_id"], row["id"]),
        ).fetchone()
    return int(previous["count"] or 1)


def task_attempt_scope(task: Any, owner_type: str) -> tuple[str, tuple[Any, ...]]:
    active_statuses = "('QUEUED', 'RUNNING', 'SUBMITTED', 'IN_QUEUE', 'IN_PROGRESS')"
    task_id = int(task["id"])
    if owner_type == "generation_styling":
        return "ga.owner_type = 'generation_styling' AND ga.owner_id = ?", (task_id,)
    if owner_type == "generation_preview":
        return (
            f"ga.owner_type = 'generation_preview' AND ga.owner_id IN (SELECT id FROM generation_previews WHERE task_id = ?) "
            f"AND (ga.status NOT IN {active_statuses} OR ga.owner_id = ?)",
            (task_id, row_value(task, "active_storyboard_preview_id") or task["active_preview_id"]),
        )
    if owner_type == "generation_result_image":
        return (
            f"ga.owner_type = 'generation_result_image' AND ga.owner_id IN ("
            "SELECT gri.id FROM generation_result_images_new gri "
            "JOIN generation_result_sets grs ON grs.id = gri.result_set_id "
            f"WHERE grs.task_id = ? AND (ga.status NOT IN {active_statuses} OR grs.id = ?))",
            (task_id, task["active_result_set_id"]),
        )
    raise ValueError(f"Unsupported owner type: {owner_type}")


def get_task_attempt_summary_page(task_id: int, owner_type: str, limit: int, before_created_at: str | None = None, before_id: int | None = None) -> dict[str, Any]:
    with get_db() as db:
        task = db.execute("SELECT * FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return {"items": [], "total": 0, "limit": limit, "hasMore": False, "nextCursor": None}
        scope, params = task_attempt_scope(task, owner_type)
        cursor_scope = f"({scope}) AND (ga.created_at < ? OR (ga.created_at = ? AND ga.id < ?))" if before_created_at is not None and before_id is not None else scope
        cursor_params = (*params, before_created_at, before_created_at, before_id) if before_created_at is not None and before_id is not None else params
        total = int(db.execute(f"SELECT COUNT(*) AS total FROM generation_attempts ga WHERE {scope}", params).fetchone()["total"])
        rows = db.execute(
            f"""
            SELECT ga.*,
                   CASE WHEN ga.owner_type = 'generation_result_image'
                        THEN (SELECT sort_order FROM generation_result_images_new WHERE id = ga.owner_id)
                        ELSE NULL END AS shot_sort_order
            FROM generation_attempts ga
            WHERE {cursor_scope}
            ORDER BY ga.created_at DESC, ga.id DESC
            LIMIT ?
            """,
            (*cursor_params, limit + 1),
        ).fetchall()
    page_rows = rows[:limit]
    items = []
    for row in page_rows:
        item = serialize_attempt_summary(row)
        item["shotSortOrder"] = row_value(row, "shot_sort_order")
        items.append(item)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "hasMore": len(rows) > limit,
        "nextCursor": {
            "beforeCreatedAt": page_rows[-1]["created_at"],
            "beforeId": page_rows[-1]["id"],
        } if len(rows) > limit and page_rows else None,
    }


def get_task_attempt_detail(task_id: int, attempt_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        task = db.execute("SELECT * FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return None
        clauses = []
        params: list[Any] = []
        for owner_type in GENERATION_ATTEMPT_OWNER_TYPES:
            scope, scope_params = task_attempt_scope(task, owner_type)
            clauses.append(f"({scope})")
            params.extend(scope_params)
        row = db.execute(
            f"SELECT ga.* FROM generation_attempts ga WHERE ga.id = ? AND ({' OR '.join(clauses)})",
            (attempt_id, *params),
        ).fetchone()
    return serialize_attempt_detail(row) if row else None


def get_task_ai_operations(task_id: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for owner_type in GENERATION_ATTEMPT_OWNER_TYPES:
        page = get_task_attempt_summary_page(task_id, owner_type, 1)
        items.extend(page["items"])
    return sorted(items, key=lambda item: (item.get("createdAt") or "", item["id"]), reverse=True)


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


def payload_has_field(payload: BaseModel, field: str) -> bool:
    fields = getattr(payload, "model_fields_set", None)
    if fields is None:
        fields = getattr(payload, "__fields_set__", set())
    return field in fields


def normalize_model_profile(profile: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(profile)
    for derived_key in ("headReferenceUrls", "headReferenceMeta", "headReferenceEntries"):
        normalized.pop(derived_key, None)
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
    key = join_key(asset_dir, sanitize_filename(filename))
    if model_asset_file_is_referenced(key):
        return
    cleanup_objects(image_object_keys(key))


def model_asset_file_is_referenced(object_key: str) -> bool:
    clean = clean_key(object_key)
    if not clean:
        return False
    pattern = f"%{clean}%"
    with get_db() as db:
        input_version = db.execute(
            """
            SELECT 1 FROM generation_task_input_versions
            WHERE model_snapshot_json LIKE ?
            LIMIT 1
            """,
            (pattern,),
        ).fetchone()
        if input_version:
            return True
        attempt = db.execute(
            """
            SELECT 1 FROM generation_attempts
            WHERE request_snapshot_json LIKE ? OR response_snapshot_json LIKE ?
            LIMIT 1
            """,
            (pattern, pattern),
        ).fetchone()
    return bool(attempt)


def model_asset_dir_is_referenced(asset_dir: str) -> bool:
    clean = clean_key(asset_dir)
    if not clean:
        return False
    pattern = f"%{clean}/%"
    with get_db() as db:
        input_version = db.execute(
            """
            SELECT 1 FROM generation_task_input_versions
            WHERE model_snapshot_json LIKE ?
            LIMIT 1
            """,
            (pattern,),
        ).fetchone()
        if input_version:
            return True
        attempt = db.execute(
            """
            SELECT 1 FROM generation_attempts
            WHERE request_snapshot_json LIKE ? OR response_snapshot_json LIKE ?
            LIMIT 1
            """,
            (pattern, pattern),
        ).fetchone()
    return bool(attempt)
