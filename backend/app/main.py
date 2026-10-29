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
import tempfile
import threading
import time
import zipfile
from uuid import uuid4
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from .pose_selection import pack_pose_references, pose_plan_from_shortlist, validate_pose_plan
from .http_performance import HttpPerformanceMiddleware
from .config import get_settings
from .db import dumps, get_db, init_db, loads, log_event, now_iso
from .generation_records import capture_published_image, published_versions, replay_package, executable_package, json_object, adapt_replay_references
from .prompt_render import (
    render_image_reference_spans,
    upgrade_prompt_runtime_placeholders,
    render_pose_direction,
    render_user_instruction_line,
    render_user_requirement_line,
)
from .prompt_optimization import OPTIMIZER_KEY, optimizer_input, text_hash, parse_optimized_prompts
from .gemini_provider import (
    TOAPIS_MAX_MESSAGE_IMAGES,
    GeminiProviderError,
    GeminiRetryDeferred,
    effective_gemini_model,
    effective_text_provider,
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
    normalized_image_key,
)
from .generation_progress import (
    ERROR_AUTH_FAILED,
    ERROR_BUSINESS_REJECT,
    ERROR_DELIVERY_FAILED,
    ERROR_INSUFFICIENT_BALANCE,
    ERROR_MALFORMED_OUTPUT,
    ERROR_MEDIA_UNAVAILABLE,
    ERROR_POLL_TIMEOUT,
    ERROR_PROVIDER_FAILED,
    ERROR_PROVIDER_TIMEOUT,
    ERROR_SUBMISSION_UNKNOWN,
    ERROR_SYSTEM_ERROR,
    ERROR_TASK_NOT_FOUND,
    ERROR_UPLOAD_FAILED,
    ERROR_UPLOAD_REQUIREMENT_NOT_MET,
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
    is_status_check_retryable_error,
    normalize_toapis_error,
    provider_error_presentation,
)
from .image_provider import ImageProviderError, get_image_provider, build_reference_collage_bytes, reference_source_bytes
from .media_storage import (
    is_accepted_upload_key,
    media_original_key_for_owner,
    new_media_original_key,
    owned_media_object_keys,
)
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
    r"{{\s*([#^])\s*((?:segment|runtime)\.[a-zA-Z0-9_.-]+)\s*}}"
    r"((?:(?!{{\s*[#^]\s*(?:segment|runtime)\.).)*?){{\s*/\s*\2\s*}}",
    re.DOTALL,
)
AI_STYLING_PROMPT_CONFIG_KEY = "assembly.ai_styling"
GARMENT_IMAGE_SELECTION_PROMPT_CONFIG_KEY = "assembly.garment_image_selection"
HERO_IMAGE_SELECTION_PROMPT_CONFIG_KEY = "assembly.hero_image_selection"
MAX_GARMENT_IMAGE_INPUTS = 6
MAX_HERO_IMAGE_INPUTS = 8
MAX_GARMENT_SELECTED_IMAGES = 6
MAX_HERO_SELECTED_IMAGES = 8
IMAGE2_MAX_REFERENCE_IMAGES = 6
# 成片 6 张参考图预算按“少而大”分配：角色图不再拼版，单张事实图独占一个
# provider 输入位，避免多格拼版被上游缩放后损失身份、面料与图案细节。
IMAGE2_DIRECT_MODEL_SOURCE_LIMIT = 1
IMAGE2_DIRECT_HERO_SOURCE_LIMIT = 2
IMAGE2_DIRECT_STYLING_SOURCE_LIMIT = 1
# 姿势参考按镜头预裁切：provider 只收到与当前景别相符的动作参考，
# 避免全身姿势图把每个镜头都拉回全身构图。值为保留图片上部的比例。
IMAGE2_DIRECT_POSE_CROP: dict[tuple[str, int], float] = {
    ("top", 2): 0.45,
    ("top", 1): 0.78,
    ("top", 3): 0.40,
    ("bottom", 2): 0.45,
    ("one_piece", 2): 0.45,
}
IMAGE2_MODEL_SHEET_SOURCE_LIMIT = 4
IMAGE2_HERO_SHEET_SOURCE_LIMIT = 6
IMAGE2_HERO_SHEET_CHUNK_SIZE = 3
IMAGE2_STYLING_GARMENT_LIMIT = 2
IMAGE2_STYLING_SHEET_SOURCE_LIMIT = 4
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


class StylingGarmentBatchManualTagsRequest(BaseModel):
    ids: list[int]
    manualTags: dict[str, Any]


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
    userRequirement: str | None = None
    aiStylingUserInstruction: str | None = None
    poseSelectionUserInstruction: str | None = None
    storyboardPreviewUserInstruction: str | None = None
    finalImageUserInstruction: str | None = None


class GenerationBatchUpdateRequest(BaseModel):
    name: str | None = None


class DirectUploadFileRequest(BaseModel):
    filename: str
    mimeType: str | None = None
    sizeBytes: int | None = None
    relativePath: str | None = None
    lastModified: int | None = None
    checksumSha256: str | None = None


class DirectUploadRequest(BaseModel):
    scope: str = "generation_task"
    files: list[DirectUploadFileRequest] = Field(default_factory=list)


class UploadSessionCreateRequest(DirectUploadRequest):
    businessCode: str | None = None
    sourceTitle: str | None = None
    batchId: str | None = None
    executorId: str | None = None


class StylingImportSessionRequest(BaseModel):
    businessCode: str
    files: list[DirectUploadFileRequest] = Field(default_factory=list)


class StylingImportBatchCreateRequest(BaseModel):
    sourceTitle: str | None = None
    batchId: str | None = None
    items: list[StylingImportSessionRequest] = Field(default_factory=list)
    executorId: str | None = None


class UploadSessionResumeRequest(BaseModel):
    files: list[DirectUploadFileRequest] = Field(default_factory=list)
    executorId: str | None = None


class UploadSessionCompleteItemsRequest(BaseModel):
    objectKeys: list[str] = Field(default_factory=list)
    executorId: str | None = None


class UploadedMediaRequest(BaseModel):
    objectKey: str
    filename: str | None = None
    mimeType: str | None = None
    sizeBytes: int | None = None


class GenerationTaskFromUploadsRequest(BaseModel):
    name: str | None = None
    batchId: str | None = None
    batchName: str | None = None
    userRequirement: str | None = None
    uploads: list[UploadedMediaRequest] = Field(default_factory=list)


class UploadSessionFinalizeGenerationTaskRequest(BaseModel):
    name: str | None = None
    batchId: str | None = None
    batchName: str | None = None
    userRequirement: str | None = None


class UploadSessionFinalizeStylingGarmentRequest(BaseModel):
    code: str
    executorId: str | None = None


class UploadSessionLeaseRequest(BaseModel):
    executorId: str | None = None


class GenerationStylingRequest(BaseModel):
    userInstruction: str | None = None
    userRequirement: str | None = None


class GenerationStageInstructionRequest(BaseModel):
    userInstruction: str | None = None


class ResultImageRetryRequest(GenerationStageInstructionRequest):
    versionId: int | None = None


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


class UploadSessionMutationRequest(BaseModel):
    ids: list[str]
    confirmName: str | None = None
    confirmPhrase: str | None = None


class ModelProfileUpsertRequest(BaseModel):
    modelId: str | None = None
    name: str | None = None
    isDefault: bool | None = None
    profile: dict[str, Any] | None = None



class PromptConfigUpdateRequest(BaseModel):
    content: str
    enabled: bool | None = None


class PromptConfigPreviewRequest(BaseModel):
    content: str
    attemptId: int | None = None


class PromptOptimizationTestRequest(BaseModel):
    content: str
    attemptId: int


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
RUNNING_GENERATION_TASK_STATUSES = {"POSE_SELECTION_RUNNING", "STYLING_RUNNING", "PREVIEW_RUNNING", "FINAL_RUNNING"}
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
    app.state.tagging_recovery_poller = asyncio.create_task(tagging_recovery_loop())
    try:
        yield
    finally:
        for name in ("generation_poller", "tagging_recovery_poller"):
            task = getattr(app.state, name, None)
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


@app.exception_handler(GenerationPhaseError)
async def generation_phase_error_handler(request: Request, exc: GenerationPhaseError) -> JSONResponse:
    error_code = getattr(exc, "error_code", None)
    presentation = provider_error_presentation(error_code, str(exc))
    status_code = 400 if error_code == ERROR_MEDIA_UNAVAILABLE else (500 if error_code == ERROR_SYSTEM_ERROR else 502)
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": presentation["message"],
            "error": presentation,
        },
    )


def user_generation_response(value: Any) -> Any:
    """Project HTTP responses only; stored snapshots and internal execution stay intact."""
    if isinstance(value, list):
        return [user_generation_response(item) for item in value]
    if not isinstance(value, dict):
        return value
    private_keys = {
        "poseSelection", "poseSelectionId", "attempts", "requestFacts", "responseFacts",
        "technicalMessage", "provider", "providerRequestId", "candidateRetrievalSnapshot",
        "model_raw_output", "promptConfigSnapshot",
    }
    result = {}
    for key, item in value.items():
        if key in private_keys:
            continue
        if key in {"detail", "message", "error", "errorMessage"} and isinstance(item, str) and re.search(
            r"prompt|provider|gemini|toapis|fal\b|姿势|拍摄预设", item, re.IGNORECASE
        ):
            item = "拍摄暂不可用，请联系管理员处理后继续生成。"
        result[key] = user_generation_response(item)
    return result


@app.middleware("http")
async def require_api_session(request: Request, call_next: Any) -> Any:
    public_paths = {"/api/health", "/api/version", "/api/auth/login", "/api/auth/logout", "/api/auth/session"}
    if request.method == "OPTIONS" or request.url.path in public_paths:
        return await call_next(request)
    if request.url.path.startswith("/api/") and request.method in {"POST", "PATCH", "DELETE"} and not is_write_origin_allowed(request):
        return JSONResponse(status_code=403, content={"detail": "请求来源不被允许"})
    if request.url.path.startswith("/api/") and not is_request_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "请先登录"})
    response = await call_next(request)
    # Keep streaming downloads/media untouched. The outer performance middleware
    # compresses the final JSON response after this role-specific projection.
    if (get_request_role(request) == "user"
            and (request.url.path.startswith("/api/generation-") or response.status_code >= 400)
            and response.headers.get("content-type", "").startswith("application/json")):
        body = b"".join([chunk async for chunk in response.body_iterator])
        headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
        return JSONResponse(user_generation_response(json.loads(body)), status_code=response.status_code, headers=headers)
    return response


app.add_middleware(HttpPerformanceMiddleware)

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "storage": "configured" if settings.s3_access_key_id and settings.s3_secret_access_key else "missing_credentials",
        "geminiConfigured": is_gemini_configured(),
        "geminiProvider": gemini_channel(),
        "imageProviderConfigured": settings.is_image_provider_configured(),
    }


@app.get("/api/admin/storage-diagnostics")
def storage_diagnostics(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_age_minutes: int = Query(30, ge=0, le=1440, alias="minAgeMinutes"),
) -> dict[str, Any]:
    require_admin(request)
    stale_before = (datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        total_media_candidates = int(db.execute(
            "SELECT COUNT(*) AS count FROM media_assets WHERE deleted_at IS NULL"
        ).fetchone()["count"])
        total_upload_candidates = int(db.execute(
            """
            SELECT COUNT(*) AS count
            FROM upload_session_items usi
            JOIN upload_sessions us ON us.id = usi.session_id
            WHERE usi.status IN ('UPLOADED', 'READY')
              AND us.status <> 'FINALIZED'
              AND usi.updated_at <= ?
            """,
            (stale_before,),
        ).fetchone()["count"])
        media_rows = db.execute(
            """
            SELECT id, object_key, original_filename, created_at
            FROM media_assets
            WHERE deleted_at IS NULL
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        session_rows = db.execute(
            """
            SELECT usi.id, usi.session_id, usi.object_key, usi.filename, usi.status, us.created_at
            FROM upload_session_items usi
            JOIN upload_sessions us ON us.id = usi.session_id
            WHERE usi.status IN ('UPLOADED', 'READY')
              AND us.status <> 'FINALIZED'
              AND usi.updated_at <= ?
            ORDER BY usi.updated_at DESC, usi.sort_order, usi.id
            LIMIT ? OFFSET ?
            """,
            (stale_before, limit, offset),
        ).fetchall()
    missing_media = [
        {
            "mediaAssetId": int(row["id"]),
            "objectKey": row["object_key"],
            "filename": row["original_filename"],
            "createdAt": row["created_at"],
        }
        for row in media_rows
        if not object_exists(row["object_key"])
    ]
    unavailable_upload_items = [
        {
            "itemId": int(row["id"]),
            "sessionId": row["session_id"],
            "objectKey": row["object_key"],
            "filename": row["filename"],
            "status": row["status"],
            "createdAt": row["created_at"],
        }
        for row in session_rows
        if not object_exists(row["object_key"])
    ]
    return {
        "summary": {
            "checkedMediaAssets": len(media_rows),
            "totalMediaCandidates": total_media_candidates,
            "mediaAssetsTruncated": total_media_candidates > offset + len(media_rows),
            "missingMediaAssets": len(missing_media),
            "checkedUploadItems": len(session_rows),
            "totalUploadItemCandidates": total_upload_candidates,
            "uploadItemsTruncated": total_upload_candidates > offset + len(session_rows),
            "unavailableUploadItems": len(unavailable_upload_items),
            "minAgeMinutes": min_age_minutes,
            "staleBefore": stale_before,
            "limit": limit,
            "offset": offset,
            "nextOffset": offset + limit,
        },
        "missingMediaAssets": missing_media,
        "unavailableUploadItems": unavailable_upload_items,
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
    raise HTTPException(status_code=410, detail="独立直传已停用，请使用导入会话接口")


def build_direct_upload_items(scope: str, files: list[DirectUploadFileRequest]) -> list[dict[str, Any]]:
    if not files:
        raise HTTPException(status_code=400, detail="至少选择一张图片")
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="单次最多申请 20 张图片导入")
    ensure_direct_upload_cors()
    direct_upload_scope_prefix(scope)
    items: list[dict[str, Any]] = []
    for index, item in enumerate(files, start=1):
        filename = sanitize_filename(item.filename or f"image-{index}")
        if item.sizeBytes is not None and item.sizeBytes > settings.max_upload_image_bytes:
            max_mb = settings.max_upload_image_bytes / (1024 * 1024)
            raise HTTPException(status_code=400, detail=f"{filename} 超过 {max_mb:.0f}MB 大小限制")
        content_type = direct_upload_content_type(item.mimeType, filename)
        checksum_sha256 = normalized_upload_checksum(item.checksumSha256)
        object_key = new_media_original_key(filename)
        upload_url = presigned_put_url(object_key, content_type, {"sha256": checksum_sha256} if checksum_sha256 else None)
        if not upload_url:
            raise HTTPException(status_code=500, detail="无法创建导入地址")
        items.append(
            {
                "objectKey": object_key,
                "uploadUrl": upload_url,
                "method": "PUT",
                "headers": {
                    "Content-Type": content_type,
                    **({"x-amz-meta-sha256": checksum_sha256} if checksum_sha256 else {}),
                },
                "expiresIn": settings.s3_presign_expires_seconds,
                "filename": filename,
                "mimeType": content_type,
                "relativePath": normalized_upload_relative_path(item.relativePath, filename),
                "lastModified": item.lastModified,
                "checksumSha256": checksum_sha256,
                "maxSizeBytes": settings.max_upload_image_bytes,
            }
        )
    return items


def normalized_upload_relative_path(value: str | None, fallback: str) -> str:
    raw = (value or fallback).replace("\\", "/").strip().lstrip("/")
    parts = [part for part in raw.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(status_code=400, detail="图片相对路径不合法")
    normalized = "/".join(parts)
    if len(normalized) > 500:
        raise HTTPException(status_code=400, detail="图片相对路径过长")
    return normalized


def normalized_upload_executor_id(value: str | None) -> str | None:
    if not value:
        return None
    clean = re.sub(r"[^A-Za-z0-9._-]+", "", value.strip())[:100]
    if not clean:
        raise HTTPException(status_code=400, detail="导入执行端标识不合法")
    return clean


def normalized_upload_checksum(value: str | None) -> str | None:
    if not value:
        return None
    clean = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", clean):
        raise HTTPException(status_code=400, detail="图片 SHA-256 校验值不合法")
    return clean


def upload_lease_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat(timespec="seconds").replace("+00:00", "Z")


def claim_upload_session_lease(db: sqlite3.Connection, session: Any, executor_id: str | None) -> None:
    ensure_upload_session_not_deleting(session)
    owner = normalized_upload_executor_id(executor_id)
    batch_id = row_value(session, "batch_id")
    if not owner:
        if batch_id:
            raise HTTPException(status_code=409, detail="缺少导入执行凭证，请刷新页面后重试")
        return
    now = now_iso()
    expires = upload_lease_expiry()
    if batch_id:
        claimed = db.execute(
            """
            UPDATE upload_sessions
            SET lease_owner = ?, lease_expires_at = ?
            WHERE batch_id = ?
              AND NOT EXISTS (
                SELECT 1 FROM upload_sessions active
                WHERE active.batch_id = ?
                  AND active.lease_owner IS NOT NULL
                  AND active.lease_owner <> ?
                  AND active.lease_expires_at > ?
              )
            """,
            (owner, expires, batch_id, batch_id, owner, now),
        )
    else:
        claimed = db.execute(
            """
            UPDATE upload_sessions
            SET lease_owner = ?, lease_expires_at = ?
            WHERE id = ?
              AND (lease_owner IS NULL OR lease_owner = ? OR lease_expires_at <= ?)
            """,
            (owner, expires, session["id"], owner, now),
        )
    if claimed.rowcount == 0:
        raise HTTPException(status_code=409, detail="该导入任务正在另一页面或电脑继续，请稍后再试")


def insert_upload_session(
    db: sqlite3.Connection,
    *,
    scope: str,
    files: list[DirectUploadFileRequest],
    business_code: str | None = None,
    source_title: str | None = None,
    batch_id: str | None = None,
    executor_id: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    scope_prefix = direct_upload_scope_prefix(scope)
    if scope_prefix == "generation-task" and len(files) > MAX_HERO_IMAGE_INPUTS:
        raise HTTPException(status_code=400, detail=f"每个拍摄任务最多导入 {MAX_HERO_IMAGE_INPUTS} 张主推款图片")
    session_files = files[:MAX_GARMENT_IMAGE_INPUTS] if scope_prefix == "styling-garment" else files
    session_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
    items = build_direct_upload_items(scope, session_files)
    timestamp = now_iso()
    db.execute(
        """
        INSERT INTO upload_sessions
          (id, scope, status, business_code, source_title, batch_id, lease_owner, lease_expires_at, total_count, uploaded_count, created_at, updated_at)
        VALUES (?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (session_id, scope_prefix, business_code, source_title, batch_id, normalized_upload_executor_id(executor_id), upload_lease_expiry() if executor_id else None, len(items), timestamp, timestamp),
    )
    for index, item in enumerate(items):
        requested_file = session_files[index]
        db.execute(
            """
            INSERT INTO upload_session_items
              (session_id, sort_order, object_key, filename, mime_type, size_bytes, relative_path, last_modified, checksum_sha256, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
            """,
            (
                session_id,
                index,
                item["objectKey"],
                item["filename"],
                item["mimeType"],
                requested_file.sizeBytes,
                item["relativePath"],
                requested_file.lastModified,
                item["checksumSha256"],
                timestamp,
                timestamp,
            ),
        )
    return session_id, items


@app.post("/api/upload-sessions")
def create_upload_session(payload: UploadSessionCreateRequest) -> dict[str, Any]:
    business_code = sanitize_name(payload.businessCode) if payload.businessCode else None
    source_title = sanitize_name(payload.sourceTitle) if payload.sourceTitle else None
    batch_id = sanitize_name(payload.batchId) if payload.batchId else None
    with get_db() as db:
        session_id, items = insert_upload_session(
            db,
            scope=payload.scope,
            files=payload.files,
            business_code=business_code,
            source_title=source_title,
            batch_id=batch_id,
            executor_id=payload.executorId,
        )
    return {"session": get_upload_session(session_id), "items": items}


@app.post("/api/upload-sessions/styling-import-batch")
def create_styling_import_batch(payload: StylingImportBatchCreateRequest) -> dict[str, Any]:
    if not payload.items:
        raise HTTPException(status_code=400, detail="至少选择一个货号")
    if len(payload.items) > 500:
        raise HTTPException(status_code=400, detail="单次最多导入 500 个货号")
    source_title = sanitize_name(payload.sourceTitle) if payload.sourceTitle else None
    batch_id = sanitize_name(payload.batchId) if payload.batchId else f"import-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
    codes = [sanitize_name(item.businessCode) for item in payload.items]
    if any(not code for code in codes):
        raise HTTPException(status_code=400, detail="每个导入项都需要有效货号")
    if len(set(codes)) != len(codes):
        raise HTTPException(status_code=400, detail="同一导入任务中货号不能重复")
    created: list[tuple[str, list[dict[str, Any]]]] = []
    with get_db() as db:
        for item, clean_code in zip(payload.items, codes, strict=True):
            created.append(insert_upload_session(
                db,
                scope="styling_garment",
                files=item.files,
                business_code=clean_code,
                source_title=source_title or clean_code,
                batch_id=batch_id,
                executor_id=payload.executorId,
            ))
    return {
        "batchId": batch_id,
        "items": [
            {"session": get_upload_session(session_id), "items": upload_items}
            for session_id, upload_items in created
        ],
    }


def upload_session_object_matches(row: Any) -> bool:
    head = object_head(row["object_key"])
    if not head or int(head.get("contentLength") or 0) <= 0:
        return False
    expected_size = row["size_bytes"]
    if expected_size is not None and int(head.get("contentLength") or 0) != expected_size:
        return False
    expected_checksum = row_value(row, "checksum_sha256")
    if expected_checksum:
        metadata = head.get("metadata") if isinstance(head.get("metadata"), dict) else {}
        if str(metadata.get("sha256") or "").lower() != expected_checksum:
            return False
    return True


@app.get("/api/upload-sessions")
def list_upload_sessions(
    scope: str | None = None,
    status: str | None = None,
    batchId: str | None = None,
    limit: int = Query(20, ge=1, le=500),
    includeAbandoned: bool = False,
    includeArchived: bool = False,
) -> dict[str, Any]:
    filters: list[str] = []
    values: list[Any] = []
    if scope:
        filters.append("scope = ?")
        values.append(direct_upload_scope_prefix(scope))
    if batchId:
        filters.append("batch_id = ?")
        values.append(sanitize_name(batchId))
    if status:
        requested_statuses = [item.strip().upper() for item in status.split(",") if item.strip()]
        if requested_statuses:
            placeholders = ", ".join("?" for _ in requested_statuses)
            filters.append(f"status IN ({placeholders})")
            values.extend(requested_statuses)
    else:
        default_statuses = ["PENDING", "UPLOADING", "UPLOADED", "FINALIZING", "FAILED", "FINALIZED"]
        if includeAbandoned or includeArchived:
            default_statuses.append("ABANDONED")
        placeholders = ", ".join("?" for _ in default_statuses)
        filters.append(f"status IN ({placeholders})")
        values.extend(default_statuses)
    if not includeAbandoned and not includeArchived:
        filters.append("status <> 'ABANDONED'")
    if not includeArchived:
        filters.append("archived_at IS NULL")
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT * FROM upload_sessions
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        session_ids = [row["id"] for row in rows]
        items_by_session: dict[str, list[Any]] = {session_id: [] for session_id in session_ids}
        target_contexts: dict[str, dict[str, Any]] = {}
        if session_ids:
            placeholders = ", ".join("?" for _ in session_ids)
            item_rows = db.execute(
                f"""
                SELECT * FROM upload_session_items
                WHERE session_id IN ({placeholders})
                ORDER BY sort_order, id
                """,
                session_ids,
            ).fetchall()
            for item in item_rows:
                items_by_session.setdefault(item["session_id"], []).append(item)
            target_contexts = load_upload_target_contexts(db, rows)
    return {
        "items": [
            serialize_upload_session(
                row,
                items_by_session.get(row["id"], []),
                target_context=target_contexts.get(str(row["id"])),
            )
            for row in rows
        ]
    }


def upload_session_batch_key(row: Any) -> str:
    batch_id = str(row_value(row, "batch_id") or "").strip()
    return f"batch:{batch_id}" if batch_id else f"session:{row['id']}"


@app.get("/api/upload-session-batches")
def list_upload_session_batches(
    scope: str = Query("styling_garment"),
    include_archived: bool = Query(False, alias="includeArchived"),
    limit: int = Query(500, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    normalized_scope = direct_upload_scope_prefix(scope)
    filters = ["us.scope = ?", "COALESCE(us.archive_reason, '') <> 'deletion_pending'"]
    values: list[Any] = [normalized_scope]
    if not include_archived:
        filters.append("us.archived_at IS NULL")
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT us.*
            FROM upload_sessions us
            WHERE {' AND '.join(filters)}
            ORDER BY us.created_at DESC, us.id DESC
            """,
            values,
        ).fetchall()
        target_contexts = load_upload_target_contexts(db, rows)

    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(upload_session_batch_key(row), []).append(row)

    batches: list[dict[str, Any]] = []
    for key, members in grouped.items():
        members_by_created = sorted(members, key=lambda item: (str(item["created_at"]), str(item["id"])))
        first = members_by_created[0]
        title = str(row_value(first, "source_title") or row_value(first, "business_code") or "导入任务")
        status_counts: dict[str, int] = {}
        for member in members:
            status = str(member["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
        batches.append({
            "key": key,
            "batchId": row_value(first, "batch_id"),
            "name": title,
            "codeCount": len(members),
            "createdAt": min(str(member["created_at"]) for member in members),
            "updatedAt": max(str(member["updated_at"] or member["created_at"]) for member in members),
            "statusCounts": status_counts,
            "sessions": [
                serialize_upload_session(
                    member,
                    [],
                    target_context=target_contexts.get(str(member["id"])),
                )
                for member in members_by_created
            ],
        })
    batches.sort(key=lambda item: (str(item["createdAt"]), str(item["key"])), reverse=True)
    total = len(batches)
    page = batches[offset:offset + limit]
    return {
        "items": page,
        "limit": limit,
        "offset": offset,
        "nextOffset": offset + limit if offset + limit < total else None,
        "hasMore": offset + limit < total,
        "total": total,
        "totalPages": (total + limit - 1) // limit if total else 0,
    }


def normalized_upload_session_ids(ids: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(str(item).strip() for item in ids if str(item).strip()))
    if not cleaned:
        raise HTTPException(status_code=400, detail="至少选择一条导入记录")
    if len(cleaned) > 500:
        raise HTTPException(status_code=400, detail="单次最多处理 500 条导入记录")
    return cleaned


@app.post("/api/upload-sessions/reconcile")
def reconcile_upload_sessions(payload: UploadSessionMutationRequest) -> dict[str, list[str]]:
    """Authoritatively reconcile browser-persisted upload session references."""
    session_ids = normalized_upload_session_ids(payload.ids)
    placeholders = ", ".join("?" for _ in session_ids)
    with get_db() as db:
        rows = db.execute(
            f"SELECT id, archive_reason FROM upload_sessions WHERE id IN ({placeholders})",
            session_ids,
        ).fetchall()
    by_id = {str(row["id"]): row for row in rows}
    return {
        "existingIds": [session_id for session_id in session_ids if session_id in by_id],
        "deletionPendingIds": [
            session_id
            for session_id in session_ids
            if session_id in by_id and row_value(by_id[session_id], "archive_reason") == "deletion_pending"
        ],
    }


def upload_session_rows_for_mutation(db: Any, session_ids: list[str]) -> list[Any]:
    placeholders = ", ".join("?" for _ in session_ids)
    rows = db.execute(
        f"SELECT * FROM upload_sessions WHERE id IN ({placeholders})",
        session_ids,
    ).fetchall()
    if len(rows) != len(session_ids):
        raise HTTPException(status_code=404, detail="部分导入记录不存在或已删除")
    return rows


def upload_session_delete_impact(db: Any, session_ids: list[str]) -> dict[str, Any]:
    session_ids = normalized_upload_session_ids(session_ids)
    rows = upload_session_rows_for_mutation(db, session_ids)
    placeholders = ", ".join("?" for _ in session_ids)
    item_rows = db.execute(
        f"""
        SELECT item.size_bytes, item.media_asset_id,
               EXISTS (
                 SELECT 1 FROM media_assets active_media
                 WHERE active_media.object_key = item.object_key AND active_media.deleted_at IS NULL
               ) AS has_active_media
        FROM upload_session_items item
        WHERE item.session_id IN ({placeholders})
        """,
        session_ids,
    ).fetchall()
    temporary_rows = [row for row in item_rows if row["media_asset_id"] is None and not row["has_active_media"]]
    linked_business_count = sum(1 for row in rows if row_value(row, "target_id"))
    return {
        "uploadSessionCount": len(rows),
        "uploadItemCount": len(item_rows),
        "temporaryObjectCount": len(temporary_rows),
        "linkedBusinessCount": linked_business_count,
        "estimatedBytes": sum(int(row["size_bytes"] or 0) for row in temporary_rows),
    }


@app.post("/api/upload-sessions/delete-impact")
def get_upload_sessions_delete_impact(payload: UploadSessionMutationRequest) -> dict[str, Any]:
    with get_db() as db:
        return {"impact": upload_session_delete_impact(db, payload.ids)}


@app.post("/api/upload-sessions/archive")
def archive_upload_sessions(payload: UploadSessionMutationRequest) -> dict[str, Any]:
    session_ids = normalized_upload_session_ids(payload.ids)
    timestamp = now_iso()
    placeholders = ", ".join("?" for _ in session_ids)
    with get_db() as db:
        upload_session_rows_for_mutation(db, session_ids)
        db.execute(
            f"""
            UPDATE upload_sessions
            SET archived_at = COALESCE(archived_at, ?),
                archive_reason = COALESCE(archive_reason, 'user_archived'),
                updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (timestamp, timestamp, *session_ids),
        )
    return {"archivedIds": session_ids}


@app.post("/api/upload-sessions/restore")
def restore_upload_sessions(payload: UploadSessionMutationRequest) -> dict[str, Any]:
    session_ids = normalized_upload_session_ids(payload.ids)
    placeholders = ", ".join("?" for _ in session_ids)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = upload_session_rows_for_mutation(db, session_ids)
        if any(row_value(row, "archive_reason") == "deletion_pending" for row in rows):
            raise HTTPException(status_code=409, detail="导入记录正在彻底删除，无法恢复")
        db.execute(
            f"UPDATE upload_sessions SET archived_at = NULL, archive_reason = NULL, updated_at = ? WHERE id IN ({placeholders})",
            (now_iso(), *session_ids),
        )
    return {"restoredIds": session_ids}


def ensure_upload_session_not_deleting(session: Any) -> None:
    if row_value(session, "archive_reason") in {"deletion_pending", "deletion_failed"}:
        raise HTTPException(status_code=409, detail="导入记录正在彻底删除，不能继续导入或入库")


def enqueue_upload_session_cleanup_and_delete(db: Any, session_ids: list[str], *, include_protected_keys: bool = False) -> list[str]:
    if not session_ids:
        return []
    placeholders = ", ".join("?" for _ in session_ids)
    timestamp = now_iso()
    media_protection_filter = "" if include_protected_keys else """
          AND NOT EXISTS (
            SELECT 1 FROM media_assets active_media
            WHERE active_media.object_key = item.object_key AND active_media.deleted_at IS NULL
          )
    """
    safe_rows = db.execute(
        f"""
        SELECT item.session_id, item.object_key
        FROM upload_session_items item
        WHERE item.session_id IN ({placeholders})
          AND item.media_asset_id IS NULL
          {media_protection_filter}
        """,
        session_ids,
    ).fetchall()
    for row in safe_rows:
        db.execute(
            """
            INSERT INTO upload_object_cleanup_queue
              (object_key, source_session_id, error_message, attempt_count, created_at, updated_at)
            VALUES (?, ?, NULL, 0, ?, ?)
            ON CONFLICT(object_key) DO UPDATE SET
              source_session_id = excluded.source_session_id,
              updated_at = excluded.updated_at
            """,
            (row["object_key"], row["session_id"], timestamp, timestamp),
        )
    db.execute(f"DELETE FROM upload_sessions WHERE id IN ({placeholders})", session_ids)
    return [str(row["object_key"]) for row in safe_rows]


def process_upload_object_cleanup_queue(*, source_session_ids: list[str] | None = None, limit: int = 100) -> list[dict[str, str]]:
    if source_session_ids is not None and not source_session_ids:
        return []
    filters = ""
    values: list[Any] = []
    if source_session_ids:
        placeholders = ", ".join("?" for _ in source_session_ids)
        filters = f"WHERE source_session_id IN ({placeholders})"
        values.extend(source_session_ids)
    claim_stale_before = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
    claim_filter = "(claim_token IS NULL OR claimed_at <= ?)"
    filters = f"{filters} {'AND' if filters else 'WHERE'} {claim_filter}"
    values.append(claim_stale_before)
    with get_db() as db:
        rows = db.execute(
            f"SELECT id, object_key FROM upload_object_cleanup_queue {filters} ORDER BY attempt_count, updated_at, id LIMIT ?",
            (*values, limit),
        ).fetchall()
    errors: list[dict[str, str]] = []
    for row in rows:
        claim_token = uuid4().hex
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """
                SELECT id, object_key
                FROM upload_object_cleanup_queue
                WHERE id = ? AND (claim_token IS NULL OR claimed_at <= ?)
                """,
                (row["id"], claim_stale_before),
            ).fetchone()
            if not current:
                continue
            protected = db.execute(
                "SELECT 1 FROM media_assets WHERE object_key = ? AND deleted_at IS NULL LIMIT 1",
                (current["object_key"],),
            ).fetchone()
            if protected:
                db.execute(
                    """
                    UPDATE upload_object_cleanup_queue
                    SET error_message = '对象当前受正式媒体保护，已暂缓清理',
                        attempt_count = attempt_count + 1, claim_token = NULL, claimed_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now_iso(), row["id"]),
                )
                continue
            db.execute(
                "UPDATE upload_object_cleanup_queue SET claim_token = ?, claimed_at = ?, updated_at = ? WHERE id = ?",
                (claim_token, now_iso(), now_iso(), row["id"]),
            )
        try:
            delete_object(row["object_key"])
        except Exception as exc:
            message = str(exc)
            errors.append({"objectKey": row["object_key"], "error": message})
            with get_db() as db:
                db.execute(
                    """
                    UPDATE upload_object_cleanup_queue
                    SET error_message = ?, attempt_count = attempt_count + 1,
                        claim_token = NULL, claimed_at = NULL, updated_at = ?
                    WHERE id = ? AND claim_token = ?
                    """,
                    (message, now_iso(), row["id"], claim_token),
                )
        else:
            with get_db() as db:
                db.execute("DELETE FROM upload_object_cleanup_queue WHERE id = ? AND claim_token = ?", (row["id"], claim_token))
    return errors


@app.delete("/api/upload-sessions")
def delete_upload_sessions(payload: UploadSessionMutationRequest) -> dict[str, Any]:
    session_ids = normalized_upload_session_ids(payload.ids)
    timestamp = now_iso()
    with get_db() as db:
        rows = upload_session_rows_for_mutation(db, session_ids)
        expected_name = str(row_value(rows[0], "source_title") or row_value(rows[0], "business_code") or rows[0]["id"])
        if len(rows) == 1:
            if payload.confirmName != expected_name:
                raise HTTPException(status_code=400, detail=f"请输入完整名称“{expected_name}”确认彻底删除")
        elif payload.confirmPhrase != "全部删除":
            raise HTTPException(status_code=400, detail="批量彻底删除请输入“全部删除”")
        placeholders = ", ".join("?" for _ in session_ids)
        db.execute(
            f"""
            UPDATE upload_sessions
            SET archived_at = COALESCE(archived_at, ?), archive_reason = 'deletion_pending',
                lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (timestamp, timestamp, *session_ids),
        )
    deleted_ids: list[str] = []
    deferred_ids: list[str] = []
    try:
        with get_db() as db:
            rows = upload_session_rows_for_mutation(db, session_ids)
            deleted_ids = [str(row["id"]) for row in rows if row["status"] == "FINALIZED"]
            deferred_ids = [str(row["id"]) for row in rows if row["status"] != "FINALIZED"]
            enqueue_upload_session_cleanup_and_delete(db, deleted_ids)
    except Exception:
        with get_db() as db:
            placeholders = ", ".join("?" for _ in session_ids)
            db.execute(
                f"UPDATE upload_sessions SET archive_reason = 'deletion_failed', updated_at = ? WHERE id IN ({placeholders})",
                (now_iso(), *session_ids),
            )
        raise
    cleanup_errors = process_upload_object_cleanup_queue(source_session_ids=deleted_ids)
    return {
        "deletedIds": deleted_ids,
        "deferredIds": deferred_ids,
        "objectCleanupErrors": cleanup_errors,
    }


@app.get("/api/upload-sessions/{session_id}")
def get_upload_session_endpoint(session_id: str) -> dict[str, Any]:
    return {"session": get_upload_session(session_id.strip())}


@app.post("/api/upload-sessions/{session_id}/lease")
def renew_upload_session_lease(session_id: str, payload: UploadSessionLeaseRequest) -> dict[str, Any]:
    session_id = session_id.strip()
    with get_db() as db:
        session = db.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="导入批次不存在")
        if session["status"] in {"FINALIZED", "ABANDONED"}:
            return {"session": get_upload_session(session_id)}
        claim_upload_session_lease(db, session, payload.executorId)
    return {"session": get_upload_session(session_id)}


@app.post("/api/upload-sessions/{session_id}/resume")
def resume_upload_session(session_id: str, payload: UploadSessionResumeRequest) -> dict[str, Any]:
    session_id = session_id.strip()
    timestamp = now_iso()
    with get_db() as db:
        session = db.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="导入批次不存在")
        if session["status"] in {"FINALIZED", "ABANDONED"}:
            return {"session": get_upload_session(session_id), "items": []}
        claim_upload_session_lease(db, session, payload.executorId)
        if session["status"] == "FINALIZING":
            try:
                updated_at = datetime.fromisoformat(str(session["updated_at"]).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                updated_at = datetime.now(timezone.utc)
            if datetime.now(timezone.utc) - updated_at <= timedelta(minutes=5):
                return {"session": get_upload_session(session_id), "items": []}
            db.execute(
                """
                UPDATE upload_sessions
                SET status = 'UPLOADED', error_message = ?, updated_at = ?
                WHERE id = ? AND status = 'FINALIZING' AND target_id IS NULL
                """,
                ("上次保存中断，已恢复到可继续状态", timestamp, session_id),
            )
            session = db.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
        rows = db.execute(
            "SELECT * FROM upload_session_items WHERE session_id = ? ORDER BY sort_order, id",
            (session_id,),
        ).fetchall()
        supplied = {
            normalized_upload_relative_path(item.relativePath, item.filename): item
            for item in payload.files
        }
        expected_paths = [row_value(row, "relative_path") or row["filename"] for row in rows]
        if set(supplied) != set(expected_paths):
            missing = [path for path in expected_paths if path not in supplied]
            extra = [path for path in supplied if path not in expected_paths]
            detail = []
            if missing:
                detail.append(f"缺少 {len(missing)} 张")
            if extra:
                detail.append(f"多出 {len(extra)} 张")
            raise HTTPException(status_code=409, detail=f"所选目录与原导入任务不一致（{'，'.join(detail)}）")
        mismatched: list[str] = []
        checksum_bindings: list[tuple[str, int]] = []
        for row in rows:
            path = row_value(row, "relative_path") or row["filename"]
            item = supplied[path]
            expected_size = row["size_bytes"]
            expected_modified = row_value(row, "last_modified")
            expected_checksum = row_value(row, "checksum_sha256")
            if expected_size is not None and item.sizeBytes != expected_size:
                mismatched.append(path)
                continue
            if expected_modified is not None and item.lastModified != expected_modified:
                mismatched.append(path)
                continue
            supplied_checksum = normalized_upload_checksum(item.checksumSha256)
            if row_value(session, "batch_id") and not supplied_checksum:
                raise HTTPException(status_code=400, detail="断点续传需要图片 SHA-256 校验值")
            if expected_checksum and supplied_checksum != expected_checksum:
                mismatched.append(path)
            elif not expected_checksum and supplied_checksum:
                checksum_bindings.append((supplied_checksum, int(row["id"])))
        if mismatched:
            raise HTTPException(status_code=409, detail=f"所选目录中有 {len(mismatched)} 张图片已发生变化，已拒绝断点继续")
        if checksum_bindings:
            db.executemany(
                "UPDATE upload_session_items SET checksum_sha256 = ?, updated_at = ? WHERE id = ? AND checksum_sha256 IS NULL",
                [(checksum, timestamp, item_id) for checksum, item_id in checksum_bindings],
            )

        recovered_keys: list[str] = []
        for row in rows:
            if row["status"] in {"UPLOADED", "READY"}:
                continue
            if upload_session_object_matches(row):
                recovered_keys.append(row["object_key"])
        if recovered_keys:
            placeholders = ", ".join("?" for _ in recovered_keys)
            db.execute(
                f"UPDATE upload_session_items SET status = 'UPLOADED', error_message = NULL, updated_at = ? WHERE session_id = ? AND object_key IN ({placeholders})",
                (timestamp, session_id, *recovered_keys),
            )

        current_rows = db.execute(
            "SELECT * FROM upload_session_items WHERE session_id = ? ORDER BY sort_order, id",
            (session_id,),
        ).fetchall()
        uploaded_count = sum(1 for row in current_rows if row["status"] in {"UPLOADED", "READY"})
        next_status = "UPLOADED" if uploaded_count == len(current_rows) else "UPLOADING"
        db.execute(
            "UPDATE upload_sessions SET status = ?, uploaded_count = ?, error_message = NULL, updated_at = ? WHERE id = ?",
            (next_status, uploaded_count, timestamp, session_id),
        )

        pending_items: list[dict[str, Any]] = []
        for row in current_rows:
            if row["status"] in {"UPLOADED", "READY"}:
                continue
            checksum_sha256 = row_value(row, "checksum_sha256")
            upload_url = presigned_put_url(row["object_key"], row["mime_type"], {"sha256": checksum_sha256} if checksum_sha256 else None)
            if not upload_url:
                raise HTTPException(status_code=500, detail="无法刷新导入地址")
            pending_items.append({
                "objectKey": row["object_key"],
                "uploadUrl": upload_url,
                "method": "PUT",
                "headers": {
                    "Content-Type": row["mime_type"],
                    **({"x-amz-meta-sha256": checksum_sha256} if checksum_sha256 else {}),
                },
                "expiresIn": settings.s3_presign_expires_seconds,
                "filename": row["filename"],
                "mimeType": row["mime_type"],
                "relativePath": row_value(row, "relative_path") or row["filename"],
                "lastModified": row_value(row, "last_modified"),
                "checksumSha256": checksum_sha256,
                "maxSizeBytes": settings.max_upload_image_bytes,
            })
    return {"session": get_upload_session(session_id), "items": pending_items}


@app.post("/api/upload-sessions/{session_id}/complete-items")
def complete_upload_session_items(session_id: str, payload: UploadSessionCompleteItemsRequest) -> dict[str, Any]:
    session_id = session_id.strip()
    keys = list(dict.fromkeys(ensure_direct_upload_key(key) for key in payload.objectKeys if key.strip()))
    if not keys:
        raise HTTPException(status_code=400, detail="至少确认一张已导入图片")
    timestamp = now_iso()
    with get_db() as db:
        session = db.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="导入批次不存在")
        if session["status"] in {"FINALIZING", "FINALIZED", "ABANDONED"}:
            return {"session": get_upload_session(session_id)}
        claim_upload_session_lease(db, session, payload.executorId)
        rows = db.execute(
            "SELECT * FROM upload_session_items WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        known_keys = {row["object_key"] for row in rows}
        unknown_keys = [key for key in keys if key not in known_keys]
        if unknown_keys:
            raise HTTPException(status_code=400, detail="导入确认包含不属于该批次的图片")
        rows_by_key = {row["object_key"]: row for row in rows}
        invalid_keys = [key for key in keys if not upload_session_object_matches(rows_by_key[key])]
        if invalid_keys:
            raise HTTPException(status_code=409, detail="对象存储中的图片缺失或完整性校验失败")
        placeholders = ", ".join("?" for _ in keys)
        db.execute(
            f"""
            UPDATE upload_session_items
            SET status = 'UPLOADED', error_message = NULL, updated_at = ?
            WHERE session_id = ? AND object_key IN ({placeholders}) AND status IN ('PENDING', 'UPLOADING', 'FAILED')
            """,
            [timestamp, session_id, *keys],
        )
        uploaded_count = int(db.execute(
            """
            SELECT COUNT(*) AS count
            FROM upload_session_items
            WHERE session_id = ? AND status IN ('UPLOADED', 'READY')
            """,
            (session_id,),
        ).fetchone()["count"] or 0)
        total_count = int(session["total_count"] or 0)
        next_status = "UPLOADED" if total_count > 0 and uploaded_count >= total_count else "UPLOADING"
        db.execute(
            """
            UPDATE upload_sessions
            SET status = ?, uploaded_count = ?, error_message = NULL, updated_at = ?
            WHERE id = ? AND status IN ('PENDING', 'UPLOADING', 'UPLOADED', 'FAILED')
            """,
            (next_status, uploaded_count, timestamp, session_id),
        )
    return {"session": get_upload_session(session_id)}


@app.post("/api/upload-sessions/{session_id}/abandon")
def abandon_upload_session(session_id: str, payload: UploadSessionLeaseRequest | None = None) -> dict[str, Any]:
    session_id = session_id.strip()
    timestamp = now_iso()
    cleanup_keys: list[str] = []
    with get_db() as db:
        session = db.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="导入批次不存在")
        claim_upload_session_lease(db, session, payload.executorId if payload else None)
        if session["status"] == "FINALIZED" and session["target_id"]:
            raise HTTPException(status_code=409, detail="导入批次已保存，请在对应业务记录中处理")
        if session["status"] == "FINALIZING":
            raise HTTPException(status_code=409, detail="导入批次正在保存，请稍后")
        rows = db.execute(
            """
            SELECT object_key
            FROM upload_session_items
            WHERE session_id = ? AND media_asset_id IS NULL
            """,
            (session_id,),
        ).fetchall()
        cleanup_keys = [row["object_key"] for row in rows]
        db.execute(
            """
            UPDATE upload_session_items
            SET status = 'ABANDONED', error_message = NULL, updated_at = ?
            WHERE session_id = ? AND media_asset_id IS NULL
            """,
            (timestamp, session_id),
        )
        db.execute(
            """
            UPDATE upload_sessions
            SET status = 'ABANDONED', error_message = NULL, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, session_id),
        )
    cleanup_objects(cleanup_keys)
    return {"session": get_upload_session(session_id)}


def get_upload_session(session_id: str) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="导入批次不存在")
        items = db.execute(
            "SELECT * FROM upload_session_items WHERE session_id = ? ORDER BY sort_order, id",
            (session_id,),
        ).fetchall()
        target_contexts = load_upload_target_contexts(db, [row])
    return serialize_upload_session(row, items, target_context=target_contexts.get(str(row["id"])))


def load_upload_target_contexts(db: sqlite3.Connection, session_rows: list[Any]) -> dict[str, dict[str, Any]]:
    resolved_target_ids = {
        str(row["id"]): int(row["target_id"])
        for row in session_rows
        if row_value(row, "target_type") == "styling_garment" and row_value(row, "target_id")
    }
    legacy_duplicate_rows = [
        row
        for row in session_rows
        if str(row_value(row, "scope") or "") == "styling-garment"
        and not row_value(row, "target_id")
        and str(row_value(row, "error_message") or "") in {"搭配服装代码已存在", "搭配服装货号已存在"}
        and str(row_value(row, "business_code") or "").strip()
    ]
    if legacy_duplicate_rows:
        legacy_session_ids = [str(row["id"]) for row in legacy_duplicate_rows]
        placeholders = ", ".join("?" for _ in legacy_session_ids)
        failure_rows = db.execute(
            f"""
            SELECT session_id, MAX(updated_at) AS failed_at
            FROM upload_session_items
            WHERE session_id IN ({placeholders}) AND status = 'FAILED'
            GROUP BY session_id
            """,
            legacy_session_ids,
        ).fetchall()
        failure_times = {
            str(failure_row["session_id"]): str(failure_row["failed_at"])
            for failure_row in failure_rows
            if failure_row["failed_at"]
        }
        for row in legacy_duplicate_rows:
            duplicate_at = failure_times.get(str(row["id"]))
            if not duplicate_at:
                continue
            existing = db.execute(
                """SELECT id FROM styling_garments
                   WHERE code = ? AND deleted_at IS NULL AND created_at <= ?
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1""",
                (str(row["business_code"]).strip(), duplicate_at),
            ).fetchone()
            if existing:
                resolved_target_ids[str(row["id"])] = int(existing["id"])
    garment_ids = list(dict.fromkeys(resolved_target_ids.values()))
    if not garment_ids:
        return {}
    placeholders = ", ".join("?" for _ in garment_ids)
    garment_rows = db.execute(
        f"""
        SELECT sg.id, sg.code, sg.status, sg.updated_at, sg.archived_at,
               COUNT(sgi.id) AS image_count,
               (
                 SELECT ma.object_key
                 FROM styling_garment_images cover_link
                 JOIN media_assets ma ON ma.id = cover_link.media_asset_id
                 WHERE cover_link.styling_garment_id = sg.id
                   AND cover_link.deleted_at IS NULL
                   AND ma.deleted_at IS NULL
                 ORDER BY cover_link.sort_order, cover_link.id
                 LIMIT 1
               ) AS cover_key
        FROM styling_garments sg
        LEFT JOIN styling_garment_images sgi
          ON sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL
        WHERE sg.id IN ({placeholders}) AND sg.deleted_at IS NULL
        GROUP BY sg.id
        """,
        garment_ids,
    ).fetchall()
    attempt_rows = db.execute(
        f"""
        SELECT owner_id,
               MAX(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS had_failure,
               MAX(CASE WHEN status = 'FAILED' THEN COALESCE(failed_at, updated_at) END) AS latest_failure_at,
               MAX(CASE WHEN status = 'SUCCEEDED' THEN COALESCE(completed_at, updated_at) END) AS latest_success_at
        FROM generation_attempts
        WHERE owner_type = 'styling_garment_tagging' AND owner_id IN ({placeholders})
        GROUP BY owner_id
        """,
        garment_ids,
    ).fetchall()
    attempts_by_id = {int(item["owner_id"]): item for item in attempt_rows}
    contexts_by_garment_id: dict[int, dict[str, Any]] = {}
    for garment in garment_rows:
        garment_id = int(garment["id"])
        attempt = attempts_by_id.get(garment_id)
        had_failure = bool(attempt and int(attempt["had_failure"] or 0))
        status = str(garment["status"])
        contexts_by_garment_id[garment_id] = {
            "summary": {
                "id": garment_id,
                "code": str(garment["code"]),
                "exists": True,
                "status": status,
                "coverUrl": stable_media_url(garment["cover_key"], "thumb") if garment["cover_key"] else None,
                "imageCount": int(garment["image_count"] or 0),
                "updatedAt": garment["updated_at"],
                "archivedAt": garment["archived_at"],
            },
            "history": {
                "hadTaggingFailure": had_failure,
                "latestFailureAt": attempt["latest_failure_at"] if attempt else None,
                "recoveredAt": attempt["latest_success_at"] if had_failure and status == "READY" and attempt else None,
            },
        }
    return {
        session_id: contexts_by_garment_id[garment_id]
        for session_id, garment_id in resolved_target_ids.items()
        if garment_id in contexts_by_garment_id
    }


def import_outcome_for_upload_session(row: Any, target_context: dict[str, Any] | None) -> str:
    status = str(row["status"])
    if status == "ABANDONED":
        return "ABANDONED"
    if row_value(row, "target_type") == "styling_garment" and row_value(row, "target_id") and not target_context:
        return "TARGET_MISSING"
    if status == "FAILED":
        return "NEEDS_ACTION"
    if status in {"PENDING", "UPLOADING"}:
        return "IMPORTING"
    if status in {"UPLOADED", "FINALIZING"}:
        return "ORGANIZING"
    if status != "FINALIZED":
        return "NEEDS_ACTION"
    if row_value(row, "target_type") != "styling_garment":
        return "SUCCEEDED"
    if not target_context:
        return "TARGET_MISSING"
    target_status = str(target_context["summary"]["status"])
    had_failure = bool(target_context["history"]["hadTaggingFailure"])
    if target_status == "READY":
        return "RECOVERED" if had_failure else "SUCCEEDED"
    if target_status == "TAGGING_RUNNING":
        return "RECOVERING" if had_failure else "PROCESSING"
    if target_status == "TAGGING_FAILED":
        return "NEEDS_ACTION"
    if target_status == "DRAFT":
        return "PENDING_RECOGNITION"
    if target_status == "TAGGING_READY":
        return "REVIEW_REQUIRED"
    return "SUCCEEDED"


def serialize_upload_session(row: Any, items: list[Any], *, target_context: dict[str, Any] | None = None) -> dict[str, Any]:
    stored_target_id = int(row["target_id"]) if row_value(row, "target_id") else None
    target_summary = target_context["summary"] if target_context else ({
        "id": stored_target_id,
        "code": row_value(row, "business_code"),
        "exists": False,
        "status": None,
        "coverUrl": None,
        "imageCount": 0,
        "updatedAt": None,
        "archivedAt": None,
    } if stored_target_id and row_value(row, "target_type") == "styling_garment" else None)
    target_id = int(target_summary["id"]) if target_summary and target_summary.get("exists") else stored_target_id
    target_type = row_value(row, "target_type") or ("styling_garment" if target_context else None)
    history_summary = target_context["history"] if target_context else {
        "hadTaggingFailure": False,
        "latestFailureAt": None,
        "recoveredAt": None,
    }
    return {
        "id": row["id"],
        "scope": row["scope"],
        "status": row["status"],
        "businessCode": row_value(row, "business_code"),
        "sourceTitle": row_value(row, "source_title"),
        "batchId": row_value(row, "batch_id"),
        "totalCount": int(row["total_count"] or 0),
        "uploadedCount": int(row["uploaded_count"] or 0),
        "targetType": target_type,
        "targetId": target_id,
        "targetStatus": target_summary["status"] if target_summary else None,
        "targetSummary": target_summary,
        "importOutcome": import_outcome_for_upload_session(row, target_context),
        "historySummary": history_summary,
        "errorMessage": row_value(row, "error_message"),
        "archivedAt": row_value(row, "archived_at"),
        "archiveReason": row_value(row, "archive_reason"),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "items": [
            {
                "id": item["id"],
                "objectKey": item["object_key"],
                "filename": item["filename"],
                "mimeType": item["mime_type"],
                "sizeBytes": item["size_bytes"],
                "relativePath": row_value(item, "relative_path") or item["filename"],
                "lastModified": row_value(item, "last_modified"),
                "checksumSha256": row_value(item, "checksum_sha256"),
                "status": item["status"],
                "mediaAssetId": item["media_asset_id"],
                "errorMessage": item["error_message"],
            }
            for item in items
        ],
    }


def mark_upload_session_failed(
    session_id: str,
    message: str,
    *,
    uploaded_count: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    db: sqlite3.Connection | None = None,
) -> None:
    timestamp = now_iso()
    def apply(connection: sqlite3.Connection) -> None:
        nonlocal uploaded_count
        if uploaded_count is None:
            uploaded_count = int(connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM upload_session_items
                WHERE session_id = ? AND status IN ('UPLOADED', 'READY')
                """,
                (session_id,),
            ).fetchone()["count"] or 0)
        connection.execute(
            """
            UPDATE upload_sessions
            SET status = 'FAILED', uploaded_count = ?, error_message = ?, updated_at = ?
            WHERE id = ? AND status IN ('UPLOADED', 'FINALIZING', 'FAILED')
            """,
            (uploaded_count, message, timestamp, session_id),
        )
        connection.execute(
            """
            UPDATE upload_session_items
            SET status = 'FAILED', error_message = ?, updated_at = ?
            WHERE session_id = ?
              AND status <> 'READY'
              AND EXISTS (
                SELECT 1 FROM upload_sessions
                WHERE id = ? AND status IN ('UPLOADED', 'FINALIZING', 'FAILED')
              )
            """,
            (message, timestamp, session_id, session_id),
        )
        if target_type and target_id:
            connection.execute(
                """
                UPDATE upload_sessions
                SET target_type = ?, target_id = ?, updated_at = ?
                WHERE id = ? AND status = 'FAILED'
                """,
                (target_type, target_id, timestamp, session_id),
            )
    if db is not None:
        apply(db)
        return
    with get_db() as connection:
        apply(connection)


def ensure_upload_session_items_uploaded(rows: list[Any]) -> None:
    pending = [row["filename"] for row in rows if row["status"] not in {"UPLOADED", "READY"}]
    if pending:
        raise HTTPException(status_code=409, detail=f"还有 {len(pending)} 张图片未导入完成")


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
        raise HTTPException(status_code=400, detail="导入用途不合法")
    return normalized.replace("_", "-")


def ensure_direct_upload_key(object_key: str) -> str:
    key = clean_key(object_key)
    if not is_accepted_upload_key(key):
        raise HTTPException(status_code=400, detail="导入对象不属于本次直传路径")
    return key


def cleanup_objects(keys: list[str]) -> None:
    for key in keys:
        with suppress(Exception):
            delete_object(key)


def image_object_keys(original_key: str | Path | None) -> list[str]:
    if not original_key:
        return []
    return owned_media_object_keys(str(original_key))


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


def retained_snapshot_reference_count(db: Any, object_key: str) -> int:
    pattern = f'%{object_key}%'
    count = 0
    for table, columns in (
        ('generation_task_input_versions', ('model_snapshot_json',)),
        ('generation_image_versions', ('request_snapshot_json',)),
        ('generation_result_images_new', ('execution_snapshot_json',)),
        ('generation_result_sets', ('prompt_preparation_json',)),
        ('generation_pose_selections', ('plan_json',)),
        ('generation_pose_selection_shots', ('pose_snapshot_json',)),
    ):
        clause = ' OR '.join(f'{column} LIKE ?' for column in columns)
        count += db.execute(f'SELECT count(*) FROM {table} WHERE {clause}', (pattern,) * len(columns)).fetchone()[0]
    return count


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
    return int(row["count"] or 0) + retained_snapshot_reference_count(db, object_key)


def media_asset_snapshot_reference_count_after_styling_garment_purge(
    db: Any,
    object_key: str,
    garment_ids: list[int],
) -> int:
    """Count snapshot refs that survive removal of target garment tagging attempts."""
    garment_ids = list(dict.fromkeys(int(garment_id) for garment_id in garment_ids))
    if not garment_ids:
        return media_asset_snapshot_reference_count(db, object_key)
    pattern = f"%{object_key}%"
    placeholders = ", ".join("?" for _ in garment_ids)
    row = db.execute(
        f"""
        SELECT
          (SELECT COUNT(*) FROM generation_task_input_versions WHERE image_snapshot_json LIKE ?) +
          (SELECT COUNT(*) FROM generation_stylings WHERE selected_material_snapshot_json LIKE ?) +
          (SELECT COUNT(*) FROM generation_attempts
           WHERE (request_snapshot_json LIKE ? OR response_snapshot_json LIKE ?)
             AND NOT (owner_type = 'styling_garment_tagging' AND owner_id IN ({placeholders}))) AS count
        """,
        (pattern, pattern, pattern, pattern, *garment_ids),
    ).fetchone()
    return int(row["count"] or 0) + retained_snapshot_reference_count(db, object_key)


def media_asset_direct_reference_count(db: Any, asset_id: int) -> int:
    row = db.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM shoot_environment_references WHERE media_asset_id = ?) +
          (SELECT COUNT(*) FROM pose_images WHERE media_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_pose_selection_shots WHERE pose_asset_id = ? OR background_asset_id = ?) +
          (SELECT COUNT(*) FROM styling_garment_images WHERE media_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_task_images WHERE media_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_previews WHERE preview_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_previews WHERE environment_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_result_images_new WHERE current_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_attempts WHERE output_asset_id = ?) +
          (SELECT COUNT(*) FROM shot_presets WHERE background_asset_id = ?) +
          (SELECT COUNT(*) FROM shot_presets WHERE mask_asset_id = ?) +
          (SELECT COUNT(*) FROM shot_presets WHERE pose_asset_id = ?) +
          (SELECT COUNT(*) FROM shot_preset_slots WHERE background_asset_id = ?) +
          (SELECT COUNT(*) FROM shot_preset_slots WHERE pose_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_result_set_shot_presets WHERE background_asset_id = ?) +
          (SELECT COUNT(*) FROM generation_result_set_shot_presets WHERE pose_asset_id = ?) AS count
        """,
        (asset_id, asset_id, asset_id, asset_id, asset_id, asset_id, asset_id, asset_id, asset_id, asset_id,
         asset_id, asset_id, asset_id, asset_id, asset_id, asset_id, asset_id),
    ).fetchone()
    return int(row["count"] or 0) + db.execute("SELECT count(*) FROM generation_image_versions WHERE media_asset_id=?", (asset_id,)).fetchone()[0]


def detach_upload_session_media_assets(db: Any, asset_ids: list[int]) -> None:
    asset_ids = list(dict.fromkeys(int(asset_id) for asset_id in asset_ids if asset_id))
    if not asset_ids:
        return
    placeholders = ", ".join("?" for _ in asset_ids)
    db.execute(
        f"UPDATE upload_session_items SET media_asset_id = NULL WHERE media_asset_id IN ({placeholders})",
        asset_ids,
    )


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
    detach_upload_session_media_assets(db, [asset_id])
    db.execute("DELETE FROM media_assets WHERE id = ?", (asset_id,))
    cleanup_objects(image_object_keys(object_key))
    return True


def cleanup_created_media_assets(assets: list[dict[str, Any]], preserve_originals: bool = False) -> None:
    asset_ids = [int(asset["id"]) for asset in assets if asset.get("id")]
    object_keys = [] if preserve_originals else [str(asset["objectKey"]) for asset in assets if asset.get("objectKey")]
    object_keys.extend(str(asset[key]) for asset in assets for key in ("normalizedKey", "thumbKey", "previewKey") if asset.get(key))
    if asset_ids:
        with suppress(Exception):
            with get_db() as db:
                db.execute(
                    f"DELETE FROM media_assets WHERE id IN ({','.join('?' for _ in asset_ids)})",
                    asset_ids,
                )
    cleanup_objects(list(dict.fromkeys(object_keys)))


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
    upload_batch_key: str | None = Query(None, alias="uploadBatchKey"),
    sort_by: str = Query("createdAt", alias="sortBy"),
    sort_dir: str = Query("desc", alias="sortDir"),
) -> dict[str, Any]:
    expire_stale_styling_garment_tagging()
    status_values = split_filter_values(status)
    uploaded_from_dt = parse_filter_datetime(uploaded_from, end_of_day=False)
    uploaded_to_dt = parse_filter_datetime(uploaded_to, end_of_day=True)
    where_clauses = ["sg.deleted_at IS NULL", "(? OR sg.archived_at IS NULL)"]
    query_params: list[Any] = [include_archived]
    keyword_terms = split_keyword_terms(keyword)
    if keyword_terms:
        term_clauses = []
        for term in keyword_terms:
            term_clauses.append(
                """
                (
                    sg.code LIKE ? OR sg.status LIKE ? OR sg.ai_tags_json LIKE ? OR
                    sg.manual_tags_json LIKE ? OR sg.official_title_tags_json LIKE ? OR
                    sg.effective_tags_json LIKE ? OR sg.tag_meta_json LIKE ?
                )
                """
            )
            query_params.extend([f"%{term}%"] * 7)
        where_clauses.append(
            "(" + " OR ".join(term_clauses) + ")"
        )
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
              (SELECT CASE
                 WHEN NULLIF(TRIM(us.batch_id), '') IS NOT NULL THEN 'batch:' || us.batch_id
                 ELSE 'session:' || us.id
               END
               FROM upload_sessions us
               WHERE us.scope = 'styling-garment'
                 AND us.status = 'FINALIZED'
                 AND us.target_type = 'styling_garment'
                 AND us.target_id = sg.id
                 AND us.archived_at IS NULL
                 AND COALESCE(us.archive_reason, '') <> 'deletion_pending'
               ORDER BY us.created_at, us.id
               LIMIT 1) AS upload_batch_key,
              (SELECT ma.id FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_asset_id,
              (SELECT ma.object_key FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_key,
              (SELECT ma.thumb_key FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_thumb_key,
              (SELECT ma.preview_key FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_preview_key,
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
            "uploadBatchKey": upload_batch_key,
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


def split_keyword_terms(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    terms: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\s,，.。、]+", text):
        term = part.strip().casefold()
        if term and term not in seen:
            terms.append(term)
            seen.add(term)
    return terms


def matches_any_keyword(haystack: Any, keyword_terms: list[str]) -> bool:
    if not keyword_terms:
        return True
    text = str(haystack or "").casefold()
    return any(term in text for term in keyword_terms)


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
        "uploadBatchKey": row_value(row, "upload_batch_key"),
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
    if secondary_values:
        return [(primary, secondary) for primary in primary_values for secondary in secondary_values]
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
        counted_primary_keys: set[str] = set()
        counted_child_keys: set[tuple[str, str]] = set()
        for primary, secondary in context["facts"]["categoryPairs"]:
            primary_text = str(primary)
            primary_key = primary_text.casefold()
            node = tree.setdefault(primary_key, {"value": primary_text, "label": primary_text, "count": 0, "children": {}})
            if primary_key not in counted_primary_keys:
                node["count"] += 1
                counted_primary_keys.add(primary_key)
            if secondary:
                secondary_text = str(secondary)
                child_key = secondary_text.casefold()
                child = node["children"].setdefault(child_key, {"value": secondary_text, "label": secondary_text, "count": 0})
                child_count_key = (primary_key, child_key)
                if child_count_key not in counted_child_keys:
                    child["count"] += 1
                    counted_child_keys.add(child_count_key)
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
    keyword_terms = split_keyword_terms(filters.get("keyword"))
    if not matches_any_keyword(context["keywordBlob"], keyword_terms):
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
    upload_batch_key = str(filters.get("uploadBatchKey") or "").strip()
    if upload_batch_key and context.get("uploadBatchKey") != upload_batch_key:
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
        raise HTTPException(status_code=409, detail="正在识别，完成后才能修改或删除搭配服装")


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
            SELECT id, tag_active_attempt_id, tag_retry_state_json FROM styling_garments
            WHERE status = 'TAGGING_RUNNING'
              AND updated_at < ?
              AND tag_retry_at IS NULL
              AND deleted_at IS NULL
            """,
            (timeout_before,),
        ).fetchall()
    with _active_tagging_jobs_lock:
        active_ids = set(_active_styling_garment_tagging_ids)
    rows = [row for row in rows if int(row["id"]) not in active_ids]
    retry_limit = max(0, settings.gemini_retry_limit)
    for row in rows:
        garment_id = int(row["id"])
        state = loads(row["tag_retry_state_json"], {})
        if not isinstance(state, dict):
            state = {}
        process_retries = int(state.get("processRetries") or 0)
        if process_retries < retry_limit:
            process_retries += 1
            retry_delay = generation_retry_backoff_seconds(process_retries)
            schedule_styling_garment_tag_retry(
                garment_id,
                int(row["tag_active_attempt_id"]) if row["tag_active_attempt_id"] else None,
                state,
                {
                    "status": "TAGGING_PROCESS_RETRY_WAIT",
                    "processRetries": process_retries,
                    "retryLimit": retry_limit,
                    "retryDelaySeconds": retry_delay,
                    "errorCode": ERROR_POLL_TIMEOUT,
                    "error": "识别进程中断，系统将自动恢复。",
                },
                kind="process",
                require_stale_before=timeout_before,
            )
            continue
        message = f"识别进程连续中断，系统已自动恢复 {process_retries} 次。"
        terminal_state = {
            **state,
            "status": "TAGGING_RETRY_EXHAUSTED",
            "kind": "process",
            "retryAt": None,
            "retryLimit": retry_limit,
            "errorCode": ERROR_POLL_TIMEOUT,
            "lastError": message,
        }
        with get_db() as db:
            cursor = db.execute(
                """UPDATE styling_garments
                   SET status = 'TAGGING_FAILED', tag_error = ?, tag_error_code = ?,
                       tag_retry_at = NULL, tag_retry_state_json = ?, tag_active_attempt_id = NULL,
                       tag_retry_lease_until = NULL, updated_at = ?
                   WHERE id = ? AND status = 'TAGGING_RUNNING'
                     AND tag_retry_at IS NULL AND updated_at < ?""",
                (message, ERROR_POLL_TIMEOUT, dumps(terminal_state), timestamp, garment_id, timeout_before),
            )
            if cursor.rowcount:
                log_event(db, "styling_garment", garment_id, "tagging_retry_exhausted", terminal_state)
        if not cursor.rowcount:
            continue
        if row["tag_active_attempt_id"]:
            attempt_id = int(row["tag_active_attempt_id"])
            finish_attempt(
                attempt_id,
                "FAILED",
                response_snapshot={**get_attempt_response_snapshot(attempt_id), **terminal_state},
                error_message=message,
                error_code=ERROR_POLL_TIMEOUT,
            )


@app.post("/api/styling-garments")
async def create_styling_garment(
    code: str = Form(...),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    clean_code = sanitize_name(code)
    if not clean_code:
        raise HTTPException(status_code=400, detail="搭配服装货号不能为空")
    if not files:
        raise HTTPException(status_code=400, detail="至少导入一张图片")
    files = files[:MAX_GARMENT_IMAGE_INPUTS]
    with get_db() as db:
        duplicate = db.execute("SELECT id FROM styling_garments WHERE code = ? AND deleted_at IS NULL", (clean_code,)).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail="搭配服装货号已存在")
    timestamp = now_iso()
    assets = await save_uploads_as_media_assets(files)
    with get_db() as db:
        duplicate = db.execute("SELECT id FROM styling_garments WHERE code = ? AND deleted_at IS NULL", (clean_code,)).fetchone()
        if duplicate:
            cleanup_objects([key for asset in assets for key in image_object_keys(asset["objectKey"])])
            raise HTTPException(status_code=409, detail="搭配服装货号已存在")
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


@app.post("/api/upload-sessions/{session_id}/finalize-styling-garment")
async def finalize_styling_garment_upload_session(
    session_id: str,
    payload: UploadSessionFinalizeStylingGarmentRequest,
) -> dict[str, Any]:
    session_id = session_id.strip()
    clean_code = sanitize_name(payload.code)
    if not clean_code:
        raise HTTPException(status_code=400, detail="搭配服装货号不能为空")
    timestamp = now_iso()
    with get_db() as db:
        session = db.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="导入批次不存在")
        if session["scope"] != "styling-garment":
            raise HTTPException(status_code=400, detail="导入批次用途不匹配")
        if session["status"] == "FINALIZED" and session["target_id"]:
            return {"item": get_styling_garment_by_id(int(session["target_id"])), "session": get_upload_session(session_id)}
        claim_upload_session_lease(db, session, payload.executorId)
        db.execute(
            """
            UPDATE upload_sessions
            SET business_code = COALESCE(business_code, ?), source_title = COALESCE(source_title, ?), updated_at = ?
            WHERE id = ?
            """,
            (clean_code, clean_code, timestamp, session_id),
        )
        if session["status"] == "ABANDONED":
            raise HTTPException(status_code=409, detail="导入批次已放弃，请重新导入")
        if session["status"] == "FINALIZING":
            raise HTTPException(status_code=409, detail="导入批次正在保存，请稍后")
        rows = db.execute(
            "SELECT * FROM upload_session_items WHERE session_id = ? ORDER BY sort_order, id",
            (session_id,),
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=400, detail="导入批次没有图片")
        if len(rows) > MAX_GARMENT_IMAGE_INPUTS:
            raise HTTPException(status_code=400, detail=f"每个搭配服装最多导入 {MAX_GARMENT_IMAGE_INPUTS} 张图片")
        ensure_upload_session_items_uploaded(rows)
        duplicate = db.execute("SELECT id FROM styling_garments WHERE code = ? AND deleted_at IS NULL", (clean_code,)).fetchone()
        if duplicate:
            mark_upload_session_failed(
                session_id,
                "搭配服装货号已存在",
                target_type="styling_garment",
                target_id=int(duplicate["id"]),
                db=db,
            )
            db.commit()
            raise HTTPException(status_code=409, detail="搭配服装货号已存在")
        claimed = db.execute(
            """
            UPDATE upload_sessions
            SET status = 'FINALIZING', error_message = NULL, updated_at = ?
            WHERE id = ? AND status IN ('UPLOADED', 'FAILED')
            """,
            (timestamp, session_id),
        )
        if claimed.rowcount != 1:
            current = db.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
            if current and current["status"] == "FINALIZED" and current["target_id"]:
                return {"item": get_styling_garment_by_id(int(current["target_id"])), "session": get_upload_session(session_id)}
            raise HTTPException(status_code=409, detail="导入批次正在保存，请稍后")
    uploads = [
        UploadedMediaRequest(
            objectKey=row["object_key"],
            filename=row["filename"],
            mimeType=row["mime_type"],
            sizeBytes=row["size_bytes"],
        )
        for row in rows
    ]
    assets: list[dict[str, Any]] = []
    garment_id: int | None = None
    try:
        assets = await save_uploaded_objects_as_media_assets(uploads, preserve_originals_on_failure=True)
        with get_db() as db:
            current_session = db.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
            if not current_session:
                raise HTTPException(status_code=409, detail="导入记录已删除，已终止入库")
            ensure_upload_session_not_deleting(current_session)
            duplicate = db.execute("SELECT id FROM styling_garments WHERE code = ? AND deleted_at IS NULL", (clean_code,)).fetchone()
            if duplicate:
                raise HTTPException(status_code=409, detail="搭配服装货号已存在")
            try:
                cursor = db.execute(
                    """
                    INSERT INTO styling_garments (code, status, manual_tags_json, ai_tags_json, tag_schema_version, created_at, updated_at)
                    VALUES (?, 'DRAFT', ?, ?, ?, ?, ?)
                    """,
                    (clean_code, dumps({}), dumps({}), PROMPT_TAGGING_RESULT_VERSION, timestamp, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="搭配服装货号已存在") from exc
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
                db.execute(
                    """
                    UPDATE upload_session_items
                    SET status = 'READY', media_asset_id = ?, error_message = NULL, updated_at = ?
                    WHERE session_id = ? AND object_key = ?
                    """,
                    (asset["id"], timestamp, session_id, asset["objectKey"]),
                )
            db.execute(
                """
                UPDATE upload_sessions
                SET status = 'FINALIZED', uploaded_count = ?, target_type = 'styling_garment',
                    target_id = ?, error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (len(assets), garment_id, timestamp, session_id),
            )
            log_event(
                db,
                "styling_garment",
                garment_id,
                "created",
                {"code": clean_code, "fileCount": len(assets), "uploadMode": "upload_session", "uploadSessionId": session_id},
            )
    except Exception as exc:
        if assets:
            cleanup_created_media_assets(assets, preserve_originals=True)
        with suppress(Exception):
            error_message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            duplicate_target_id = None
            if str(error_message) == "搭配服装货号已存在":
                with get_db() as db:
                    duplicate = db.execute(
                        "SELECT id FROM styling_garments WHERE code = ? AND deleted_at IS NULL",
                        (clean_code,),
                    ).fetchone()
                    duplicate_target_id = int(duplicate["id"]) if duplicate else None
            mark_upload_session_failed(
                session_id,
                str(error_message),
                target_type="styling_garment" if duplicate_target_id else None,
                target_id=duplicate_target_id,
            )
        raise
    if garment_id is None:
        raise HTTPException(status_code=500, detail="导入批次保存失败")
    return {"item": get_styling_garment_by_id(garment_id), "session": get_upload_session(session_id)}


@app.patch("/api/styling-garments/{garment_id}")
def update_styling_garment(garment_id: int, payload: StylingGarmentUpdateRequest) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    manual_patch = normalize_manual_business_tags(payload.manualTags) if payload.manualTags is not None else None
    if payload.code is not None:
        clean_code = sanitize_name(payload.code)
        if not clean_code:
            raise HTTPException(status_code=400, detail="搭配服装货号不能为空")
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
                raise HTTPException(status_code=409, detail="搭配服装货号已存在")
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
        db.execute(
            """UPDATE styling_garments
                   SET status = 'TAGGING_RUNNING', tag_error = NULL, tag_error_code = NULL,
                   tag_retry_at = NULL, tag_retry_state_json = NULL, tag_active_attempt_id = NULL,
                   tag_retry_lease_until = NULL,
                   updated_at = ? WHERE id = ?""",
            (timestamp, garment_id),
        )
    background_tasks.add_task(run_styling_garment_tagging, garment_id)
    return {"item": get_styling_garment_by_id(garment_id)}


@app.post("/api/styling-garments/{garment_id}/stop-tagging")
def stop_styling_garment_tagging(garment_id: int) -> dict[str, Any]:
    """Stop a scheduled retry before the recovery worker claims it."""
    timestamp = now_iso()
    with get_db() as db:
        row = db.execute(
            "SELECT status, tag_retry_at, tag_active_attempt_id FROM styling_garments WHERE id = ? AND deleted_at IS NULL",
            (garment_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="搭配服装不存在")
        if row["status"] != "TAGGING_RUNNING" or row["tag_retry_at"] is None:
            raise HTTPException(status_code=409, detail="当前识别不在自动恢复等待中")
        cursor = db.execute(
            """UPDATE styling_garments
               SET status = 'TAGGING_FAILED', tag_retry_at = NULL, tag_retry_state_json = NULL,
                   tag_active_attempt_id = NULL, tag_retry_lease_until = NULL,
                   tag_error = '用户停止识别', tag_error_code = 'USER_CANCELLED', updated_at = ?
               WHERE id = ? AND status = 'TAGGING_RUNNING' AND tag_retry_at IS NOT NULL""",
            (timestamp, garment_id),
        )
        if not cursor.rowcount:
            raise HTTPException(status_code=409, detail="识别已恢复，请刷新状态")
        if row["tag_active_attempt_id"]:
            db.execute(
                """UPDATE generation_attempts
                   SET status = 'CANCELLED', cancelled_at = ?, updated_at = ?
                   WHERE id = ? AND status = 'RUNNING'""",
                (timestamp, timestamp, row["tag_active_attempt_id"]),
            )
        log_event(db, "styling_garment", garment_id, "tagging_stopped", {"reason": "user_requested"})
    return {"item": get_styling_garment_by_id(garment_id)}


@app.post("/api/styling-garments/batch-save-and-tag")
async def save_and_tag_styling_garments_batch(payload: StylingGarmentBatchTagRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    garment_ids = normalized_styling_garment_ids(payload.ids or [])
    timestamp = now_iso()
    taggable_statuses = {"DRAFT", "TAGGING_READY", "TAGGING_FAILED", "READY"}
    started_ids: list[int] = []
    skipped: list[dict[str, Any]] = []
    with get_db() as db:
        rows = get_styling_garment_rows_for_mutation(db, garment_ids)
        rows_by_id = {int(row["id"]): row for row in rows}
        for garment_id in garment_ids:
            row = rows_by_id[garment_id]
            status = row["status"]
            if row["archived_at"] is not None:
                skipped.append({"id": garment_id, "code": row["code"], "status": status, "reason": "已归档"})
                continue
            if status == "TAGGING_RUNNING":
                skipped.append({"id": garment_id, "code": row["code"], "status": status, "reason": "正在识别"})
                continue
            if status not in taggable_statuses:
                skipped.append({"id": garment_id, "code": row["code"], "status": status, "reason": "当前状态无需识别"})
                continue
            started_ids.append(garment_id)
        if started_ids:
            placeholders = ", ".join("?" for _ in started_ids)
            db.execute(
                f"""
                UPDATE styling_garments
                SET status = 'TAGGING_RUNNING', tag_error = NULL, tag_error_code = NULL,
                    tag_retry_at = NULL, tag_retry_state_json = NULL, tag_active_attempt_id = NULL,
                    tag_retry_lease_until = NULL,
                    updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (timestamp, *started_ids),
            )
            for garment_id in started_ids:
                log_event(db, "styling_garment", garment_id, "tagging_started", {"batch": True})
    if started_ids:
        background_tasks.add_task(run_styling_garment_tagging_batch, started_ids)
    return {
        "items": [get_styling_garment_by_id(garment_id) for garment_id in started_ids],
        "startedIds": started_ids,
        "skipped": skipped,
    }


_tagging_semaphore: asyncio.Semaphore | None = None
_active_tagging_jobs_lock = threading.Lock()
_active_styling_garment_tagging_ids: set[int] = set()


def _get_tagging_semaphore() -> asyncio.Semaphore:
    """打标并发闸门：限制同时调用 AI 打标的数量，超出的排队等待。

    懒初始化到当前事件循环，避免在导入期（无 loop）创建。
    """
    global _tagging_semaphore
    if _tagging_semaphore is None:
        _tagging_semaphore = asyncio.Semaphore(max(1, settings.tagging_max_concurrency))
    return _tagging_semaphore


async def run_styling_garment_tagging(garment_id: int) -> None:
    with _active_tagging_jobs_lock:
        _active_styling_garment_tagging_ids.add(garment_id)
    try:
        async with _get_tagging_semaphore():
            with get_db() as db:
                row = db.execute("SELECT status FROM styling_garments WHERE id = ?", (garment_id,)).fetchone()
                if not row or row["status"] != "TAGGING_RUNNING":
                    return
                db.execute("UPDATE styling_garments SET updated_at = ? WHERE id = ?", (now_iso(), garment_id))
            await _run_styling_garment_tagging_guarded(garment_id)
    finally:
        with _active_tagging_jobs_lock:
            _active_styling_garment_tagging_ids.discard(garment_id)


async def run_styling_garment_tagging_batch(garment_ids: list[int]) -> None:
    await asyncio.gather(*(run_styling_garment_tagging(garment_id) for garment_id in garment_ids))


async def _run_styling_garment_tagging_guarded(garment_id: int) -> None:
    # provider 传输错误与模型契约错误分别由唯一所有者累计预算并持久退避；
    # 此处不再叠加整条任务重试，避免同一种错误形成 10x10 放大。
    try:
        tagging_result = await tag_styling_garment_with_gemini(garment_id)
    except GeminiRetryDeferred:
        # 下一次执行时间和累计次数已经持久化；释放当前并发槽位，交给 poller 续跑。
        return
    except Exception as exc:
        error_code = classify_attempt_error(exc) or ERROR_SYSTEM_ERROR
        with get_db() as db:
            row = db.execute(
                "SELECT tag_active_attempt_id, tag_retry_state_json FROM styling_garments WHERE id = ?",
                (garment_id,),
            ).fetchone()
            attempt_id = int(row["tag_active_attempt_id"]) if row and row["tag_active_attempt_id"] else None
            retry_state = loads(row["tag_retry_state_json"], {}) if row else {}
            if not isinstance(retry_state, dict):
                retry_state = {}
            terminal_state = {
                **retry_state,
                "status": "TAGGING_FAILED",
                "retryAt": None,
                "retryLimit": int(retry_state.get("retryLimit") or settings.gemini_retry_limit),
                "errorCode": error_code,
                "lastError": str(exc)[:500],
            }
            db.execute(
                """UPDATE styling_garments
                   SET status = 'TAGGING_FAILED', tag_error = ?, tag_error_code = ?,
                       tag_retry_at = NULL, tag_retry_state_json = ?, tag_active_attempt_id = NULL,
                       tag_retry_lease_until = NULL,
                   updated_at = ? WHERE id = ?""",
                (str(exc), error_code, dumps(terminal_state), now_iso(), garment_id),
            )
        if attempt_id:
            finish_attempt(
                attempt_id,
                "FAILED",
                response_snapshot={
                    **get_attempt_response_snapshot(attempt_id),
                    **terminal_state,
                    **exception_process_facts(exc),
                },
                error_message=str(exc),
                error_code=error_code,
            )
        return
    try:
        persist_styling_garment_tagging_result(garment_id, tagging_result)
    except Exception as exc:
        error_code = classify_attempt_error(exc) or ERROR_SYSTEM_ERROR
        attempt_id = tagging_result.get("attempt_id")
        if attempt_id:
            finish_attempt(
                int(attempt_id),
                "FAILED",
                response_snapshot={**tagging_result, "error": str(exc)},
                error_message=str(exc),
                error_code=error_code,
            )
        with get_db() as db:
            db.execute(
                """UPDATE styling_garments
                   SET status = 'TAGGING_FAILED', tag_error = ?, tag_error_code = ?,
                       tag_retry_at = NULL, tag_active_attempt_id = NULL,
                       tag_retry_lease_until = NULL,
                       updated_at = ? WHERE id = ?""",
                (str(exc), error_code, now_iso(), garment_id),
            )


def persist_styling_garment_tagging_result(garment_id: int, tagging_result: dict[str, Any]) -> None:
    with get_db() as db:
        timestamp = now_iso()
        attempt_id = int(tagging_result["attempt_id"]) if tagging_result.get("attempt_id") else None
        attempt_response = tagging_result.get("_attempt_response_snapshot")
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
                tag_error_code = NULL,
                tag_retry_at = NULL,
                tag_retry_state_json = NULL,
                tag_active_attempt_id = NULL,
                tag_retry_lease_until = NULL,
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
        if attempt_id:
            db.execute(
                """UPDATE generation_attempts
                   SET status = 'SUCCEEDED', completed_at = ?, failed_at = NULL, cancelled_at = NULL,
                       response_snapshot_json = ?, error_code = NULL, error_message = NULL, updated_at = ?
                   WHERE id = ? AND status = 'RUNNING'""",
                (
                    timestamp,
                    dumps(attempt_response) if isinstance(attempt_response, dict) else None,
                    timestamp,
                    attempt_id,
                ),
            )
        event_payload = {key: value for key, value in tagging_result.items() if not key.startswith("_")}
        log_event(db, "styling_garment", garment_id, "tagged", event_payload)


@app.post("/api/styling-garments/{garment_id}/images")
async def add_styling_garment_images(
    garment_id: int,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="至少选择一张图片")
    with get_db() as db:
        row = db.execute("SELECT * FROM styling_garments WHERE id = ? AND deleted_at IS NULL", (garment_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="搭配服装不存在")
        ensure_styling_garment_not_tagging(row)
        count = int(db.execute(
            "SELECT COUNT(*) AS count FROM styling_garment_images WHERE styling_garment_id = ? AND deleted_at IS NULL",
            (garment_id,),
        ).fetchone()["count"])
    if count + len(files) > MAX_GARMENT_SELECTED_IMAGES:
        raise HTTPException(status_code=400, detail=f"搭配服装最多保留 {MAX_GARMENT_SELECTED_IMAGES} 张图片")
    assets = await save_uploads_as_media_assets(files)
    timestamp = now_iso()
    try:
        with get_db() as db:
            row = db.execute("SELECT * FROM styling_garments WHERE id = ? AND deleted_at IS NULL", (garment_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="搭配服装不存在")
            ensure_styling_garment_not_tagging(row)
            current_count = int(db.execute(
                "SELECT COUNT(*) AS count FROM styling_garment_images WHERE styling_garment_id = ? AND deleted_at IS NULL",
                (garment_id,),
            ).fetchone()["count"])
            if current_count + len(assets) > MAX_GARMENT_SELECTED_IMAGES:
                raise HTTPException(status_code=409, detail=f"搭配服装最多保留 {MAX_GARMENT_SELECTED_IMAGES} 张图片")
            next_order = int(db.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM styling_garment_images WHERE styling_garment_id = ? AND deleted_at IS NULL",
                (garment_id,),
            ).fetchone()["next_order"])
            for offset, asset in enumerate(assets):
                db.execute(
                    """
                    INSERT INTO styling_garment_images
                      (styling_garment_id, media_asset_id, sort_order, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'READY', ?, ?)
                    """,
                    (garment_id, asset["id"], next_order + offset, timestamp, timestamp),
                )
            db.execute("UPDATE styling_garments SET updated_at = ? WHERE id = ?", (timestamp, garment_id))
            log_event(db, "styling_garment", garment_id, "images_added", {"count": len(assets)})
    except Exception:
        cleanup_created_media_assets(assets)
        raise
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
        raise HTTPException(status_code=404, detail="搭配服装图片不存在")
    ensure_styling_garment_not_tagging({"status": row["garment_status"]})
    assets = await save_uploads_as_media_assets([file])
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
            mark_styling_garments_deletion_state(db, [garment_id], "deletion_pending")
    if purge:
        try:
            with get_db() as db:
                referencing_task_ids, object_keys = purge_styling_garment(db, garment_id, row["code"])
                log_event(db, "styling_garment", garment_id, "purged", {"code": row["code"], "referencingTaskIds": referencing_task_ids})
        except Exception as exc:
            with suppress(Exception):
                with get_db() as db:
                    mark_styling_garments_deletion_state(db, [garment_id], "deletion_failed")
            raise HTTPException(status_code=409, detail="彻底删除未完成，记录已保留在已归档，可恢复后重试") from exc
        cleanup_errors = cleanup_deleted_media_objects(object_keys)
        return {
            "ok": True,
            "purged": True,
            "deletedTaskIds": [],
            "referencingTaskIds": referencing_task_ids,
            "objectCleanupErrors": cleanup_errors,
        }
    with get_db() as db:
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
            mark_styling_garments_deletion_state(db, garment_ids, "deletion_pending")
    if payload.purge:
        try:
            referencing_task_ids: list[int] = []
            object_keys: list[str] = []
            with get_db() as db:
                for row in rows:
                    task_ids, keys = purge_styling_garment(db, int(row["id"]), row["code"])
                    referencing_task_ids.extend(task_ids)
                    object_keys.extend(keys)
                    log_event(db, "styling_garment", int(row["id"]), "purged", {"code": row["code"], "batch": True, "referencingTaskIds": task_ids})
        except Exception as exc:
            with suppress(Exception):
                with get_db() as db:
                    mark_styling_garments_deletion_state(db, garment_ids, "deletion_failed")
            raise HTTPException(status_code=409, detail="彻底删除未完成，记录已保留在已归档，可恢复后重试") from exc
        cleanup_errors = cleanup_deleted_media_objects(object_keys)
        return {
            "ok": True,
            "purged": True,
            "deletedGarmentIds": garment_ids,
            "deletedTaskIds": [],
            "referencingTaskIds": list(dict.fromkeys(referencing_task_ids)),
            "objectCleanupErrors": cleanup_errors,
        }
    with get_db() as db:
        archive_styling_garments(db, garment_ids, "user_batch_archived")
        for row in rows:
            log_event(db, "styling_garment", int(row["id"]), "archived", {"code": row["code"], "batch": True})
    return {"ok": True, "archived": True, "ids": garment_ids}


@app.post("/api/styling-garments/{garment_id}/restore")
def restore_styling_garment(garment_id: int) -> dict[str, Any]:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = get_styling_garment_rows_for_mutation(db, [garment_id])
        row = rows[0]
        ensure_rows_not_pending_deletion(rows)
        restore_styling_garments(db, [garment_id])
        if row["archived_at"] is not None:
            log_event(db, "styling_garment", garment_id, "restored", {"code": row["code"]})
    return {"ok": True, "restored": True, "item": get_styling_garment_by_id(garment_id)}


@app.post("/api/styling-garments/batch-restore")
def restore_styling_garment_batch(payload: StylingGarmentDeleteRequest) -> dict[str, Any]:
    garment_ids = normalized_styling_garment_ids(payload.ids or [])
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = get_styling_garment_rows_for_mutation(db, garment_ids)
        ensure_rows_not_pending_deletion(rows)
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


def ensure_rows_not_pending_deletion(rows: list[Any]) -> None:
    if any(row["archive_reason"] == "deletion_pending" for row in rows):
        raise HTTPException(status_code=409, detail="记录正在彻底删除，暂时不能取消归档")


def dependent_task_ids_for_styling_garments(db: Any, garment_ids: list[int]) -> list[int]:
    target_ids = {int(garment_id) for garment_id in garment_ids}
    styling_rows = db.execute(
        "SELECT task_id, selected_material_ids_json, selected_material_snapshot_json FROM generation_stylings"
    ).fetchall()
    referencing_task_ids: list[int] = []
    for styling in styling_rows:
        referenced_ids = {int(item) for item in loads(styling["selected_material_ids_json"], [])}
        referenced_ids.update(
            int(snapshot["id"])
            for snapshot in loads(styling["selected_material_snapshot_json"], [])
            if isinstance(snapshot, dict) and snapshot.get("id") is not None
        )
        if target_ids.intersection(referenced_ids):
            referencing_task_ids.append(int(styling["task_id"]))
    return list(dict.fromkeys(referencing_task_ids))


def styling_garment_owned_media_rows(db: Any, garment_ids: list[int]) -> list[Any]:
    """Return unique media candidates owned by the target garment aggregates."""
    garment_ids = list(dict.fromkeys(int(garment_id) for garment_id in garment_ids))
    if not garment_ids:
        return []
    placeholders = ", ".join("?" for _ in garment_ids)
    return db.execute(
        f"""
        SELECT DISTINCT ma.id, ma.object_key, ma.size_bytes
        FROM media_assets ma
        WHERE ma.id IN (
          SELECT media_asset_id
          FROM styling_garment_images
          WHERE styling_garment_id IN ({placeholders})
          UNION
          SELECT output_asset_id
          FROM generation_attempts
          WHERE owner_type = 'styling_garment_tagging'
            AND owner_id IN ({placeholders})
            AND output_asset_id IS NOT NULL
        )
        """,
        (*garment_ids, *garment_ids),
    ).fetchall()


def styling_garment_owned_media_reference_counts(db: Any, garment_ids: list[int]) -> dict[int, int]:
    """Count direct references removed when the target garments are purged."""
    garment_ids = list(dict.fromkeys(int(garment_id) for garment_id in garment_ids))
    if not garment_ids:
        return {}
    placeholders = ", ".join("?" for _ in garment_ids)
    rows = db.execute(
        f"""
        SELECT asset_id, COUNT(*) AS count
        FROM (
          SELECT media_asset_id AS asset_id
          FROM styling_garment_images
          WHERE styling_garment_id IN ({placeholders})
          UNION ALL
          SELECT output_asset_id AS asset_id
          FROM generation_attempts
          WHERE owner_type = 'styling_garment_tagging'
            AND owner_id IN ({placeholders})
            AND output_asset_id IS NOT NULL
        ) owned_references
        GROUP BY asset_id
        """,
        (*garment_ids, *garment_ids),
    ).fetchall()
    return {int(row["asset_id"]): int(row["count"] or 0) for row in rows}


def releasable_styling_garment_media_rows(db: Any, garment_ids: list[int]) -> list[Any]:
    """Preview assets that become fully unreferenced after a garment purge."""
    media_rows = styling_garment_owned_media_rows(db, garment_ids)
    owned_counts = styling_garment_owned_media_reference_counts(db, garment_ids)
    return [
        media
        for media in media_rows
        if media_asset_direct_reference_count(db, int(media["id"])) <= owned_counts.get(int(media["id"]), 0)
        and media_asset_snapshot_reference_count_after_styling_garment_purge(
            db,
            clean_key(media["object_key"]),
            garment_ids,
        ) == 0
    ]


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
        SELECT shots.pose_asset_id FROM generation_pose_selection_shots shots
        JOIN generation_pose_selections selections ON selections.id = shots.selection_id
        WHERE selections.task_id IN ({placeholders})
        UNION
        SELECT shots.background_asset_id FROM generation_pose_selection_shots shots
        JOIN generation_pose_selections selections ON selections.id = shots.selection_id
        WHERE selections.task_id IN ({placeholders})
        UNION
        SELECT ma.id FROM media_assets ma JOIN generation_attempts attempts
          ON attempts.request_snapshot_json LIKE '%' || ma.object_key || '%'
        WHERE attempts.owner_type = 'generation_pose_selection' AND attempts.owner_id IN ({placeholders})
        UNION
        SELECT gri.current_asset_id AS asset_id FROM generation_result_images_new gri
        JOIN generation_result_sets grs ON grs.id = gri.result_set_id
        WHERE grs.task_id IN ({placeholders}) AND gri.current_asset_id IS NOT NULL
        UNION
        SELECT output_asset_id AS asset_id FROM generation_attempts
        WHERE output_asset_id IS NOT NULL AND (
             (owner_type IN ('generation_styling', 'generation_pose_selection') AND owner_id IN ({placeholders}))
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
        (*task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids),
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
    releasable_media_rows = releasable_styling_garment_media_rows(db, garment_ids)
    referencing_task_ids = dependent_task_ids_for_styling_garments(db, garment_ids)
    total_bytes = sum(int(row["size_bytes"] or 0) for row in releasable_media_rows)
    return {
        "garmentCount": len(garment_ids),
        "garmentImageCount": len(image_rows),
        # Keep the old names during the 0.1.0 transition, but they now describe
        # historical references that are preserved rather than deleted.
        "dependentTaskIds": referencing_task_ids,
        "dependentTaskCount": len(referencing_task_ids),
        "referencingTaskIds": referencing_task_ids,
        "referencingTaskCount": len(referencing_task_ids),
        "taskCount": 0,
        "taskImageCount": 0,
        "inputVersionCount": 0,
        "stylingCount": 0,
        "previewCount": 0,
        "resultSetCount": 0,
        "resultImageCount": 0,
        "mediaAssetCount": len(releasable_media_rows),
        "estimatedBytes": total_bytes,
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


def mark_styling_garments_deletion_state(db: Any, garment_ids: list[int], reason: str) -> None:
    garment_ids = normalized_styling_garment_ids(garment_ids)
    timestamp = now_iso()
    placeholders = ", ".join("?" for _ in garment_ids)
    db.execute(
        f"""
        UPDATE styling_garments
        SET archived_at = COALESCE(archived_at, ?),
            archive_reason = ?,
            updated_at = ?
        WHERE id IN ({placeholders}) AND deleted_at IS NULL
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


def mark_generation_tasks_deletion_state(db: Any, task_ids: list[int], reason: str) -> None:
    task_ids = normalized_task_ids(task_ids)
    timestamp = now_iso()
    placeholders = ", ".join("?" for _ in task_ids)
    db.execute(
        f"""
        UPDATE generation_tasks
        SET archived_at = COALESCE(archived_at, ?),
            archive_reason = ?,
            updated_at = ?
        WHERE id IN ({placeholders}) AND deleted_at IS NULL
        """,
        (timestamp, reason, timestamp, *task_ids),
    )


def cleanup_deleted_media_objects(object_keys: list[str]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    exact_keys = dict.fromkeys(
        key
        for object_key in object_keys
        for key in image_object_keys(object_key)
    )
    for key in exact_keys:
        try:
            delete_object(key)
        except Exception as exc:
            errors.append({"objectKey": key, "error": str(exc)})
    return errors


def purge_styling_garment(db: Any, garment_id: int, code: str) -> tuple[list[int], list[str]]:
    image_rows = db.execute(
        """
        SELECT ma.id, ma.object_key FROM styling_garment_images sgi
        JOIN media_assets ma ON ma.id = sgi.media_asset_id
        WHERE sgi.styling_garment_id = ?
        """,
        (garment_id,),
    ).fetchall()
    referencing_task_ids = dependent_task_ids_for_styling_garments(db, [garment_id])
    tagging_attempt_assets = db.execute(
        """
        SELECT output_asset_id AS asset_id
        FROM generation_attempts
        WHERE owner_type = 'styling_garment_tagging' AND owner_id = ? AND output_asset_id IS NOT NULL
        """,
        (garment_id,),
    ).fetchall()
    tagging_asset_ids = [int(asset["asset_id"]) for asset in tagging_attempt_assets]
    db.execute(
        "DELETE FROM generation_attempts WHERE owner_type = 'styling_garment_tagging' AND owner_id = ?",
        (garment_id,),
    )
    candidate_asset_ids = list(dict.fromkeys([int(image["id"]) for image in image_rows] + tagging_asset_ids))
    media_rows = media_asset_rows_for_ids(db, candidate_asset_ids)
    db.execute("DELETE FROM styling_garments WHERE id = ?", (garment_id,))
    asset_ids = [
        int(media["id"])
        for media in media_rows
        if media_asset_direct_reference_count(db, int(media["id"])) == 0
        and media_asset_snapshot_reference_count(db, clean_key(media["object_key"])) == 0
    ]
    deletable_asset_ids = set(asset_ids)
    object_keys = [media["object_key"] for media in media_rows if int(media["id"]) in deletable_asset_ids]
    if asset_ids:
        placeholders = ", ".join("?" for _ in asset_ids)
        detach_upload_session_media_assets(db, asset_ids)
        db.execute(f"DELETE FROM media_assets WHERE id IN ({placeholders})", asset_ids)
    return referencing_task_ids, object_keys


@app.post("/api/generation-tasks")
async def create_generation_task(
    name: str | None = Form(None),
    user_requirement: str | None = Form(None, alias="userRequirement"),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="至少导入一张主推款图片")
    files = files[:MAX_HERO_IMAGE_INPUTS]
    task_name = (name or datetime.now().strftime("%Y%m%d%H%M%S")).strip()
    assets = await save_uploads_as_media_assets(files)
    timestamp = now_iso()
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO generation_tasks (name, user_requirement_text, pose_workflow_enabled, status, created_at, updated_at) VALUES (?, ?, 1, 'DRAFT', ?, ?)",
            (task_name, normalize_user_requirement_text(user_requirement), timestamp, timestamp),
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
    raise HTTPException(status_code=410, detail="独立直传已停用，请使用导入会话接口")


@app.post("/api/upload-sessions/{session_id}/finalize-generation-task")
async def finalize_generation_task_upload_session(
    session_id: str,
    payload: UploadSessionFinalizeGenerationTaskRequest,
) -> dict[str, Any]:
    session_id = session_id.strip()
    timestamp = now_iso()
    with get_db() as db:
        session = db.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="导入批次不存在")
        ensure_upload_session_not_deleting(session)
        if session["scope"] != "generation-task":
            raise HTTPException(status_code=400, detail="导入批次用途不匹配")
        if session["status"] == "FINALIZED" and session["target_id"]:
            return {"item": get_generation_task_by_id(int(session["target_id"])), "session": get_upload_session(session_id)}
        if session["status"] == "FINALIZING":
            raise HTTPException(status_code=409, detail="导入批次正在保存，请稍后")
        rows = db.execute(
            "SELECT * FROM upload_session_items WHERE session_id = ? ORDER BY sort_order, id",
            (session_id,),
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=400, detail="导入批次没有图片")
        ensure_upload_session_items_uploaded(rows)
        claimed = db.execute(
            """
            UPDATE upload_sessions
            SET status = 'FINALIZING', error_message = NULL, updated_at = ?
            WHERE id = ? AND status IN ('UPLOADED', 'FAILED')
            """,
            (timestamp, session_id),
        )
        if claimed.rowcount != 1:
            current = db.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
            if current and current["status"] == "FINALIZED" and current["target_id"]:
                return {"item": get_generation_task_by_id(int(current["target_id"])), "session": get_upload_session(session_id)}
            raise HTTPException(status_code=409, detail="导入批次正在保存，请稍后")
    uploads = [
        UploadedMediaRequest(
            objectKey=row["object_key"],
            filename=row["filename"],
            mimeType=row["mime_type"],
            sizeBytes=row["size_bytes"],
        )
        for row in rows
    ]
    assets: list[dict[str, Any]] = []
    task_id: int | None = None
    try:
        assets = await save_uploaded_objects_as_media_assets(uploads, preserve_originals_on_failure=True)
        task_name = (payload.name or datetime.now().strftime("%Y%m%d%H%M%S")).strip()
        with get_db() as db:
            current_session = db.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone()
            if not current_session:
                raise HTTPException(status_code=409, detail="导入记录已删除，已终止入库")
            ensure_upload_session_not_deleting(current_session)
            batch_id = (payload.batchId or "").strip()[:64] or None
            if batch_id:
                ensure_generation_batch(db, batch_id, payload.batchName, timestamp)
            cursor = db.execute(
                "INSERT INTO generation_tasks (name, user_requirement_text, batch_id, pose_workflow_enabled, status, created_at, updated_at) VALUES (?, ?, ?, 1, 'DRAFT', ?, ?)",
                (task_name, normalize_user_requirement_text(payload.userRequirement), batch_id, timestamp, timestamp),
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
                db.execute(
                    """
                    UPDATE upload_session_items
                    SET status = 'READY', media_asset_id = ?, error_message = NULL, updated_at = ?
                    WHERE session_id = ? AND object_key = ?
                    """,
                    (asset["id"], timestamp, session_id, asset["objectKey"]),
                )
            db.execute(
                """
                UPDATE upload_sessions
                SET status = 'FINALIZED', uploaded_count = ?, target_type = 'generation_task',
                    target_id = ?, error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (len(assets), task_id, timestamp, session_id),
            )
            log_event(
                db,
                "generation_task",
                task_id,
                "created",
                {"name": task_name, "fileCount": len(assets), "uploadMode": "upload_session", "uploadSessionId": session_id},
            )
    except Exception as exc:
        if assets:
            cleanup_created_media_assets(assets, preserve_originals=True)
        with suppress(Exception):
            with get_db() as db:
                db.execute(
                    """
                    UPDATE upload_sessions
                    SET status = 'FAILED', uploaded_count = 0, error_message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(exc), now_iso(), session_id),
                )
                db.execute(
                    """
                    UPDATE upload_session_items
                    SET status = 'FAILED', error_message = ?, updated_at = ?
                    WHERE session_id = ? AND status <> 'READY'
                    """,
                    (str(exc), now_iso(), session_id),
                )
        raise
    if task_id is None:
        raise HTTPException(status_code=500, detail="导入批次保存失败")
    return {"item": get_generation_task_by_id(task_id), "session": get_upload_session(session_id)}


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
            "items": generation_task_summaries(items),
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


@app.get("/api/generation-tasks/{task_id}/status")
def get_generation_task_status(task_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "拍摄任务不存在")
        images = db.execute("SELECT status, COUNT(*) AS n FROM generation_result_images_new WHERE result_set_id = ? GROUP BY status", (row["active_result_set_id"],)).fetchall()
        attempt = db.execute("""SELECT ga.status, ga.error_code FROM generation_attempts ga
            WHERE (ga.owner_type IN ('generation_styling', 'generation_pose_selection') AND ga.owner_id = ?)
            OR (ga.owner_type = 'generation_result_image' AND ga.owner_id IN
                (SELECT id FROM generation_result_images_new WHERE result_set_id = ?))
            ORDER BY ga.id DESC LIMIT 1""", (task_id, row["active_result_set_id"])).fetchone()
        operations = db.execute("""SELECT ga.id, ga.status,
            json_extract(ga.progress_json, '$.current') AS phase,
            (SELECT json_extract(value, '$.startedAt') FROM json_each(ga.progress_json, '$.phases')
             WHERE json_extract(value, '$.phase') = json_extract(ga.progress_json, '$.current') LIMIT 1) AS phase_started_at,
            (SELECT json_extract(value, '$.detail') FROM json_each(ga.progress_json, '$.phases')
             WHERE json_extract(value, '$.phase') = json_extract(ga.progress_json, '$.current') LIMIT 1) AS phase_detail
            FROM generation_attempts ga WHERE ga.id IN (
                SELECT active_attempt_id FROM generation_result_images_new WHERE result_set_id = ?
                UNION SELECT id FROM (SELECT id FROM generation_attempts WHERE owner_type IN ('generation_styling', 'generation_pose_selection') AND owner_id = ? ORDER BY id DESC LIMIT 1)
            ) ORDER BY ga.id DESC LIMIT 6""", (row["active_result_set_id"], task_id)).fetchall()
    counts = {item["status"]: item["n"] for item in images}
    stage = "POSE_SELECTION" if row["status"] in {"POSE_SELECTION_RUNNING", "POSE_SELECTION_READY"} else "FINAL_IMAGE" if row["active_result_set_id"] else "AI_STYLING" if row["status"] != "DRAFT" else "IMPORT"
    return {"taskId": task_id, "status": row["status"], "stage": stage,
        "operations": [{"id": op["id"], "status": op["status"], "phase": op["phase"], "startedAt": op["phase_started_at"],
            "detail": {key: value for key, value in (loads(op["phase_detail"], {}) or {}).items()
                if key in {"completed", "total", "retries", "statusCheckErrors", "contractRetry"} and isinstance(value, (int, float))}
        } for op in operations],
        "progress": {"completed": counts.get("SUCCEEDED", 0), "total": 5},
        "activeOperation": {"type": stage, "status": row["status"]} if row["status"] in RUNNING_GENERATION_TASK_STATUSES else None,
        "detailRevision": row["detail_revision"], "updatedAt": row["updated_at"],
        "autoRunTarget": row["auto_run_target"], "autoRunRetryAt": row["auto_run_retry_at"],
        "autoRunRetryCount": row["auto_run_retry_count"], "retryAt": row["auto_run_retry_at"],
        "error": provider_error_presentation(attempt["error_code"]) if attempt and attempt["status"] == "FAILED" else None}


@app.get("/api/generation-tasks/{task_id}/ai-invocations")
def get_generation_task_ai_invocations(task_id: int, request: Request) -> dict[str, Any]:
    require_admin(request)
    get_generation_task_row(task_id)
    return {"items": get_task_attempts(task_id)}


@app.get("/api/generation-tasks/{task_id}/attempts", dependencies=[Depends(require_admin)])
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
        db.execute("BEGIN IMMEDIATE")
        rows = get_generation_task_rows_for_mutation(db, [task_id])
        row = rows[0]
        ensure_rows_not_pending_deletion(rows)
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
        db.execute("BEGIN IMMEDIATE")
        rows = get_generation_task_rows_for_mutation(db, task_ids)
        ensure_rows_not_pending_deletion(rows)
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
                SET auto_run_target = NULL, auto_run_requested_at = NULL,
                    auto_run_retry_at = NULL, auto_run_retry_count = 0, updated_at = ?
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
                    auto_run_retry_at = NULL,
                    auto_run_retry_count = 0,
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
        mark_generation_tasks_deletion_state(db, [task_id], "deletion_pending")
    try:
        with get_db() as db:
            object_keys = purge_generation_tasks(db, [task_id], "user_purged")
    except Exception as exc:
        with suppress(Exception):
            with get_db() as db:
                mark_generation_tasks_deletion_state(db, [task_id], "deletion_failed")
        raise HTTPException(status_code=409, detail="彻底删除未完成，记录已保留在已归档，可恢复后重试") from exc
    cleanup_errors = cleanup_deleted_media_objects(object_keys)
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
        mark_generation_tasks_deletion_state(db, task_ids, "deletion_pending")
    try:
        with get_db() as db:
            object_keys = purge_generation_tasks(db, task_ids, "user_batch_purged")
    except Exception as exc:
        with suppress(Exception):
            with get_db() as db:
                mark_generation_tasks_deletion_state(db, task_ids, "deletion_failed")
        raise HTTPException(status_code=409, detail="彻底删除未完成，记录已保留在已归档，可恢复后重试") from exc
    cleanup_errors = cleanup_deleted_media_objects(object_keys)
    return {"ok": True, "purged": True, "deletedTaskIds": task_ids, "objectCleanupErrors": cleanup_errors}


@app.post("/api/generation-tasks/delete-impact")
def get_generation_tasks_delete_impact(payload: DeleteImpactRequest) -> dict[str, Any]:
    with get_db() as db:
        get_generation_task_rows_for_mutation(db, normalized_task_ids(payload.ids))
        return {"impact": generation_task_delete_impact(db, payload.ids)}


@app.patch("/api/generation-tasks/{task_id}")
def update_generation_task(task_id: int, payload: GenerationTaskUpdateRequest, request: Request) -> dict[str, Any]:
    if payload_has_field(payload, "poseSelectionUserInstruction"):
        require_admin(request)
    instruction_fields = {
        "aiStylingUserInstruction": "ai_styling_user_instruction_text",
        "poseSelectionUserInstruction": "pose_selection_user_instruction",
        "storyboardPreviewUserInstruction": "storyboard_preview_user_instruction_text",
        "finalImageUserInstruction": "final_image_user_instruction_text",
    }
    if (
        payload.name is None
        and not payload_has_field(payload, "userRequirement")
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
        user_requirement_changed = False
        if payload.name is not None:
            updates.append("name = ?")
            values.append(payload.name.strip() or "未命名任务")
        if payload_has_field(payload, "userRequirement"):
            next_user_requirement = normalize_user_requirement_text(payload.userRequirement)
            user_requirement_changed = next_user_requirement != normalize_user_requirement_text(row_value(row, "user_requirement_text", ""))
            updates.append("user_requirement_text = ?")
            values.append(next_user_requirement)
        for field, column in instruction_fields.items():
            if payload_has_field(payload, field):
                updates.append(f"{column} = ?")
                values.append(normalize_user_input_text(getattr(payload, field)))
        updates.append("updated_at = ?")
        timestamp = now_iso()
        values.append(timestamp)
        values.append(task_id)
        db.execute(f"UPDATE generation_tasks SET {', '.join(updates)} WHERE id = ?", values)
        if user_requirement_changed and row["status"] != "DRAFT":
            mark_generation_task_input_changed(db, task_id, timestamp, "hero_product_info_changed")
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
            active_pose_selection_id = NULL,
            active_preview_id = NULL,
            active_storyboard_preview_id = NULL,
            active_result_set_id = NULL,
            auto_run_target = NULL,
            auto_run_requested_at = NULL,
            auto_run_retry_at = NULL,
            auto_run_retry_count = 0,
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
        SET status = 'READY_FOR_STYLING', pose_workflow_enabled = 1,
            active_styling_id = NULL,
            active_pose_selection_id = NULL,
            active_preview_id = NULL,
            active_storyboard_preview_id = NULL,
            active_result_set_id = NULL,
            auto_run_target = NULL,
            auto_run_requested_at = NULL,
            auto_run_retry_at = NULL,
            auto_run_retry_count = 0,
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
            (owner_type IN ('generation_styling', 'generation_pose_selection') AND owner_id = ?)
            OR (owner_type = 'generation_result_set' AND owner_id IN (SELECT id FROM generation_result_sets WHERE task_id = ?))
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
        (terminal_status, task_id, task_id, task_id, task_id),
    ).fetchone()
    owner_type = attempt["owner_type"] if attempt else None
    if owner_type in {"generation_result_image", "generation_result_set"}:
        return "PREVIEW_READY"
    if owner_type == "generation_preview":
        return "STYLING_READY"
    if owner_type == "generation_pose_selection":
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
        "pose_selection": "pose_selection_user_instruction",
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
            (owner_type IN ('generation_styling', 'generation_pose_selection') AND owner_id = ?)
            OR (owner_type = 'generation_result_set' AND owner_id IN (SELECT id FROM generation_result_sets WHERE task_id = ?))
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
    db.execute("""UPDATE generation_attempts SET status = 'CANCELLED', cancelled_at = ?, updated_at = ?
        WHERE owner_type = 'generation_result_set' AND status = 'RUNNING'
        AND owner_id IN (SELECT id FROM generation_result_sets WHERE task_id = ? AND status = 'SUPERSEDED')""",
        (timestamp, timestamp, task_id))
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


def purge_generation_tasks(db: Any, task_ids: list[int], reason: str) -> list[str]:
    task_ids = normalized_task_ids(task_ids)
    placeholders = ", ".join("?" for _ in task_ids)
    asset_rows = db.execute(
        f"""
        SELECT media_asset_id AS asset_id FROM generation_task_images WHERE task_id IN ({placeholders})
        UNION
        SELECT preview_asset_id AS asset_id FROM generation_previews WHERE task_id IN ({placeholders}) AND preview_asset_id IS NOT NULL
        UNION
        SELECT shots.pose_asset_id FROM generation_pose_selection_shots shots
        JOIN generation_pose_selections selections ON selections.id = shots.selection_id
        WHERE selections.task_id IN ({placeholders})
        UNION
        SELECT shots.background_asset_id FROM generation_pose_selection_shots shots
        JOIN generation_pose_selections selections ON selections.id = shots.selection_id
        WHERE selections.task_id IN ({placeholders})
        UNION
        SELECT ma.id FROM media_assets ma JOIN generation_attempts attempts
          ON attempts.request_snapshot_json LIKE '%' || ma.object_key || '%'
        WHERE attempts.owner_type = 'generation_pose_selection' AND attempts.owner_id IN ({placeholders})
        UNION
        SELECT gri.current_asset_id AS asset_id FROM generation_result_images_new gri
        JOIN generation_result_sets grs ON grs.id = gri.result_set_id
        WHERE grs.task_id IN ({placeholders}) AND gri.current_asset_id IS NOT NULL
        UNION
        SELECT output_asset_id AS asset_id FROM generation_attempts
        WHERE output_asset_id IS NOT NULL AND (
             (owner_type IN ('generation_styling', 'generation_pose_selection') AND owner_id IN ({placeholders}))
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
        (*task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids, *task_ids),
    ).fetchall()
    asset_ids = list(dict.fromkeys(int(row["asset_id"]) for row in asset_rows if row["asset_id"] is not None))
    version_assets = db.execute(f'''SELECT DISTINCT v.media_asset_id FROM generation_image_versions v
        JOIN generation_result_images_new i ON i.id=v.image_id JOIN generation_result_sets s ON s.id=i.result_set_id
        WHERE s.task_id IN ({placeholders})''', task_ids).fetchall()
    asset_ids = list(dict.fromkeys(asset_ids + [row[0] for row in version_assets]))
    media_rows = media_asset_rows_for_ids(db, asset_ids)
    db.execute(f"UPDATE generation_pose_selections SET attempt_id = NULL WHERE task_id IN ({placeholders})", task_ids)
    db.execute(
        f"""
        DELETE FROM generation_attempts
        WHERE (owner_type IN ('generation_styling', 'generation_pose_selection') AND owner_id IN ({placeholders}))
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
    db.execute(f"DELETE FROM generation_attempts WHERE owner_type = 'generation_result_set' AND owner_id IN (SELECT id FROM generation_result_sets WHERE task_id IN ({placeholders}))", task_ids)
    rows = get_generation_task_rows_for_mutation(db, task_ids)
    db.execute(f"DELETE FROM generation_tasks WHERE id IN ({placeholders})", task_ids)
    media_rows = [media for media in media_rows
                  if not media_asset_direct_reference_count(db, media["id"])
                  and not media_asset_snapshot_reference_count(db, media["object_key"])]
    asset_ids = [media["id"] for media in media_rows]
    object_keys = [media["object_key"] for media in media_rows]
    if asset_ids:
        asset_placeholders = ", ".join("?" for _ in asset_ids)
        detach_upload_session_media_assets(db, asset_ids)
        db.execute(f"DELETE FROM media_assets WHERE id IN ({asset_placeholders})", asset_ids)
    for row in rows:
        log_event(db, "generation_task", int(row["id"]), "purged", {"name": row["name"], "reason": reason})
    return object_keys


@app.post("/api/generation-tasks/{task_id}/images")
async def add_generation_task_images(task_id: int, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="至少选择一张主推款图片")
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="生成任务不存在")
        ensure_generation_task_editable(row)
        count = int(db.execute(
            "SELECT COUNT(*) AS count FROM generation_task_images WHERE task_id = ? AND deleted_at IS NULL",
            (task_id,),
        ).fetchone()["count"])
    if count + len(files) > MAX_HERO_SELECTED_IMAGES:
        raise HTTPException(status_code=400, detail=f"主推款最多保留 {MAX_HERO_SELECTED_IMAGES} 张图片")
    assets = await save_uploads_as_media_assets(files)
    timestamp = now_iso()
    try:
        with get_db() as db:
            row = db.execute("SELECT * FROM generation_tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="生成任务不存在")
            ensure_generation_task_editable(row)
            current_count = int(db.execute(
                "SELECT COUNT(*) AS count FROM generation_task_images WHERE task_id = ? AND deleted_at IS NULL",
                (task_id,),
            ).fetchone()["count"])
            if current_count + len(assets) > MAX_HERO_SELECTED_IMAGES:
                raise HTTPException(status_code=409, detail=f"主推款最多保留 {MAX_HERO_SELECTED_IMAGES} 张图片")
            next_order = int(db.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM generation_task_images WHERE task_id = ? AND deleted_at IS NULL",
                (task_id,),
            ).fetchone()["next_order"])
            for offset, asset in enumerate(assets):
                db.execute(
                    """
                    INSERT INTO generation_task_images
                      (task_id, media_asset_id, sort_order, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'READY', ?, ?)
                    """,
                    (task_id, asset["id"], next_order + offset, timestamp, timestamp),
                )
            db.execute("UPDATE generation_tasks SET updated_at = ? WHERE id = ?", (timestamp, task_id))
            if row["status"] != "DRAFT":
                mark_generation_task_input_changed(db, task_id, timestamp, "hero_product_images_added")
            log_event(db, "generation_task", task_id, "images_added", {"count": len(assets)})
    except Exception:
        cleanup_created_media_assets(assets)
        raise
    return {"item": get_generation_task_by_id(task_id)}


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
    asset = (await save_uploads_as_media_assets([file]))[0]
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
            SELECT gti.task_id, gti.media_asset_id, gti.sort_order, gt.status FROM generation_task_images gti
            JOIN generation_tasks gt ON gt.id = gti.task_id
            WHERE gti.id = ? AND gti.deleted_at IS NULL AND gt.deleted_at IS NULL
            """,
            (image_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="主推款图片不存在")
        ensure_generation_task_editable(row)
        if payload.sortOrder is not None:
            if int(payload.sortOrder) == int(row["sort_order"]):
                return {"item": get_generation_task_by_id(int(row["task_id"]))}
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
            raise HTTPException(status_code=400, detail="请先导入主推款图片")
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
        # 导入即草稿；首次选搭时自动进入待选搭，用户无需单独“确认”步骤。
        ready_generation_task(task_id)
        task = get_generation_task_row(task_id)
    if task["status"] not in {
        "READY_FOR_STYLING",
        "STYLING_READY",
        "POSE_SELECTION_READY",
        "PREVIEW_READY",
        "COMPLETED",
        "FINAL_PARTIAL",
        "FAILED",
        "CANCELLED",
    }:
        raise HTTPException(status_code=409, detail="当前任务状态不能智能选搭")
    effective_user_instruction = user_instruction if user_instruction is not None else pending_generation_task_stage_instruction(task, "ai_styling")
    user_instruction_context = build_stage_user_instruction_context("ai_styling", effective_user_instruction)
    user_requirement_context = build_user_requirement_context(task)
    input_version_id = create_generation_input_version(task_id)
    with get_db() as db:
        timestamp = now_iso()
        if user_instruction is not None:
            persist_generation_task_stage_instruction(db, task_id, "ai_styling", effective_user_instruction, timestamp)
        db.execute("UPDATE generation_tasks SET status = 'STYLING_RUNNING', updated_at = ? WHERE id = ?", (timestamp, task_id))
    attempt_id = create_attempt("generation_styling", task_id, effective_text_provider())
    progress = AttemptProgressRecorder(attempt_id)
    styling_plan: dict[str, Any] | None = None

    def fail_styling_attempt(exc: Exception) -> dict[str, Any]:
        progress.finish()
        with get_db() as db:
            current = db.execute("SELECT status FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
            if not current or current["status"] == "CANCELLED" or generation_attempt_cancelled(attempt_id):
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
                SET status = 'FAILED', updated_at = ?
                WHERE id = ?
                """,
                (now_iso(), task_id),
            )
        if trigger != "auto_run" or not schedule_auto_run_retry(task_id, [error_code]):
            clear_task_auto_run(task_id)
        raise HTTPException(status_code=502, detail=user_facing_provider_error_detail("智能选搭失败", exc, error_code)) from exc

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
            if not current or current["status"] == "CANCELLED" or generation_attempt_cancelled(attempt_id):
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
                    active_pose_selection_id = NULL,
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
    raise HTTPException(status_code=410, detail="分镜预览已退出当前工作流，请直接生成最终成片")


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
            media_original_key_for_owner("generation_attempt", attempt_id, "preview.png"),
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
        final_error_code = classify_generation_attempt_error(exc)
        finish_attempt(
            attempt_id,
            "FAILED",
            response_snapshot=provider_response,
            error_message=str(exc),
            error_code=final_error_code,
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
        if trigger != "auto_run" or not schedule_auto_run_retry(task_id, [final_error_code]):
            clear_task_auto_run(task_id)
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
                auto_run_retry_at = NULL,
                auto_run_retry_count = 0,
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


def fallback_generation_batch_title(batch_id: str) -> str:
    return f"批次 {str(batch_id)[-8:]}" if len(str(batch_id)) > 8 else f"批次 {batch_id}"


def normalize_generation_batch_name(name: str | None, batch_id: str) -> str:
    value = str(name or "").strip()
    return value[:80] if value else fallback_generation_batch_title(batch_id)


def ensure_generation_batch(db: Any, batch_id: str, name: str | None, timestamp: str | None = None) -> None:
    clean_batch_id = str(batch_id or "").strip()[:64]
    if not clean_batch_id:
        return
    timestamp = timestamp or now_iso()
    batch_name = normalize_generation_batch_name(name, clean_batch_id)
    existing = db.execute("SELECT id FROM generation_batches WHERE id = ?", (clean_batch_id,)).fetchone()
    if existing:
        db.execute("UPDATE generation_batches SET updated_at = ? WHERE id = ?", (timestamp, clean_batch_id))
        return
    db.execute(
        "INSERT INTO generation_batches (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (clean_batch_id, batch_name, timestamp, timestamp),
    )


def generation_batch_metadata(batch_id: str) -> dict[str, str | None]:
    with get_db() as db:
        row = db.execute("SELECT name, updated_at FROM generation_batches WHERE id = ?", (batch_id,)).fetchone()
    return {
        "name": str(row["name"]) if row and str(row["name"]).strip() else fallback_generation_batch_title(batch_id),
        "updatedAt": str(row["updated_at"]) if row and row["updated_at"] else None,
    }


@app.get("/api/generation-batches/{batch_id}/tasks")
def list_generation_batch_tasks(batch_id: str) -> dict[str, Any]:
    rows = get_batch_task_rows(batch_id)
    return {"items": generation_task_summaries(rows)}


@app.patch("/api/generation-batches/{batch_id}")
def update_generation_batch(batch_id: str, payload: GenerationBatchUpdateRequest) -> dict[str, Any]:
    rows = get_batch_task_rows(batch_id)
    if not rows:
        raise HTTPException(status_code=404, detail="批次不存在或没有任务")
    timestamp = now_iso()
    with get_db() as db:
        ensure_generation_batch(db, batch_id, payload.name, timestamp)
        batch_name = normalize_generation_batch_name(payload.name, batch_id)
        db.execute(
            "UPDATE generation_batches SET name = ?, updated_at = ? WHERE id = ?",
            (batch_name, timestamp, batch_id),
        )
    refreshed = get_batch_task_rows(batch_id)
    return {"item": serialize_generation_batch_record(batch_id, refreshed)}


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
                SET auto_run_target = ?, auto_run_requested_at = ?,
                    auto_run_retry_at = NULL, auto_run_retry_count = 0, updated_at = ?
                WHERE id = ?
                """,
                (AUTO_RUN_TARGET_FINAL, timestamp, timestamp, int(row["id"])),
            )
            started_ids.append(int(row["id"]))
    pump_auto_run_queue()
    refreshed = get_batch_task_rows(batch_id)
    return {
        "startedTaskIds": started_ids,
        "items": generation_task_summaries(refreshed),
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
        "items": generation_task_summaries(refreshed),
    }


async def regenerate_result_group(result_set_id: int, user_instruction: str | None = None) -> dict[str, Any]:
    with get_db() as db:
        group = db.execute('SELECT * FROM generation_result_sets WHERE id=?', (result_set_id,)).fetchone()
        if not group:
            raise HTTPException(404, '成片组不存在')
        task = dict(assert_task_can_generate(db, group['task_id']))
        images = db.execute('SELECT * FROM generation_result_images_new WHERE result_set_id=? ORDER BY sort_order', (result_set_id,)).fetchall()
    if len(images) != 5 or {i['sort_order'] for i in images} != set(range(1, 6)):
        raise HTTPException(409, '这组拍摄记录不完整，请使用重新一键成片；已有图片仍然保留')
    packages = [shot_replay_package(group['task_id'], i['id'], user_instruction=user_instruction) for i in images]
    timestamp = now_iso()
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        current = assert_task_can_generate(db, group['task_id'])
        if current['detail_revision'] != task['detail_revision']:
            raise HTTPException(409, '任务已变化，请刷新后重试')
        new_id = int(db.execute("""INSERT INTO generation_result_sets
            (task_id, input_version_id, styling_id, preview_id, shot_preset_version_id, user_instruction_json,
             status, created_at, updated_at, pose_selection_id, prompt_preparation_json)
            VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?, ?, ?)""",
            (group['task_id'], group['input_version_id'], group['styling_id'], group['preview_id'],
             group['shot_preset_version_id'], group['user_instruction_json'], timestamp, timestamp,
             group['pose_selection_id'], dumps({'mode': 'direct', 'status': 'DIRECT', 'sourceResultSetId': result_set_id}))).lastrowid)
        db.execute("""INSERT INTO generation_result_set_shot_presets
            (result_set_id,category_family,shot_index,background_asset_id,pose_asset_id,created_at)
            SELECT ?,category_family,shot_index,background_asset_id,pose_asset_id,? FROM generation_result_set_shot_presets WHERE result_set_id=?""",
            (new_id, timestamp, result_set_id))
        ids = []
        for old, package in zip(images, packages):
            capture_published_image(db, old['id'])
            ids.append(int(db.execute("""INSERT INTO generation_result_images_new
                (result_set_id,shot_role,sort_order,status,execution_snapshot_json,user_instruction_json,created_at,updated_at)
                VALUES (?,?,?,'QUEUED',?,?,?,?)""", (new_id, old['shot_role'], old['sort_order'], dumps(package),
                dumps(package.get('finalImageUserInstruction', {})), timestamp, timestamp)).lastrowid))
        supersede_generation_result_sets(db, group['task_id'], timestamp, keep_result_set_id=new_id)
        activate_result_context(db, new_id)
        db.execute("UPDATE generation_tasks SET status='FINAL_RUNNING' WHERE id=?", (group['task_id'],))
    schedule_background_generation(run_final_generation(group['task_id'], new_id, ids, trigger='manual_step'), job_key=final_set_job_key(new_id))
    return {'item': get_generation_task_by_id(group['task_id'])}


@app.post('/api/generation-result-sets/{result_set_id}/regenerate')
async def regenerate_selected_result_set(result_set_id: int, payload: GenerationStageInstructionRequest | None = None) -> dict[str, Any]:
    return await regenerate_result_group(result_set_id, payload.userInstruction if payload else None)


@app.post('/api/generation-result-sets/{result_set_id}/continue')
async def continue_selected_result_set(result_set_id: int) -> dict[str, Any]:
    with get_db() as db:
        group = db.execute('SELECT * FROM generation_result_sets WHERE id=?', (result_set_id,)).fetchone()
        if not group:
            raise HTTPException(404, '成片组不存在')
    ids = continue_active_final_result_set(group['task_id'], result_set_id, group['styling_id'], group['preview_id'], activate=True)
    if ids:
        with get_db() as db:
            attempts = {r['id']: r['active_attempt_id'] for r in db.execute(
                'SELECT id,active_attempt_id FROM generation_result_images_new WHERE id IN (SELECT value FROM json_each(?))', (dumps(ids),)) if r['active_attempt_id']}
        schedule_background_generation(run_final_generation(group['task_id'], result_set_id, ids,
            resume_attempts_by_image_id=attempts, trigger='manual_step'), job_key=final_set_job_key(result_set_id))
    return {'item': get_generation_task_by_id(group['task_id'])}


async def run_generate_final_images(
    task_id: int,
    include_debug: bool = False,
    trigger: str = "manual_step",
    continue_existing: bool = False,
    user_instruction: str | None = None,
) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    if (
        task["status"] not in {"STYLING_READY", "POSE_SELECTION_READY", "PREVIEW_READY", "COMPLETED", "FINAL_PARTIAL", "CANCELLED"}
        or not task["active_styling_id"]
    ):
        raise HTTPException(status_code=409, detail="请先完成 AI 智能选搭")
    preview_id = int(task["active_storyboard_preview_id"]) if row_value(task, "active_storyboard_preview_id") else None
    styling_id = int(task["active_styling_id"])
    if not continue_existing and task['active_result_set_id']:
        with get_db() as db:
            existing = db.execute('SELECT styling_id FROM generation_result_sets WHERE id=?', (task['active_result_set_id'],)).fetchone()
        if existing and existing['styling_id'] == styling_id:
            return await regenerate_result_group(task['active_result_set_id'], user_instruction)
    category_family = '' if continue_existing and task['active_result_set_id'] else storyboard_category_family(get_styling_plan_by_id(styling_id))
    preset_rows = []
    if not row_value(task, "pose_workflow_enabled", 0):
        with get_db() as db:
            preset_rows = db.execute(
                """
                SELECT slots.*
                FROM shot_preset_slots slots
                JOIN media_assets background ON background.id = slots.background_asset_id AND background.deleted_at IS NULL
                JOIN media_assets pose ON pose.id = slots.pose_asset_id AND pose.deleted_at IS NULL
                WHERE slots.category_family = ?
                ORDER BY slots.shot_index
                """,
                (category_family,),
            ).fetchall()
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
                with get_db() as db:
                    resume_rows = db.execute(
                        f"SELECT id, active_attempt_id FROM generation_result_images_new WHERE id IN ({', '.join('?' for _ in image_ids)})",
                        image_ids,
                    ).fetchall()
                resume_attempts = {
                    int(row["id"]): int(row["active_attempt_id"])
                    for row in resume_rows
                    if row["active_attempt_id"]
                }
                schedule_background_generation(
                    run_final_generation(
                        task_id,
                        int(task["active_result_set_id"]),
                        image_ids,
                        resume_attempts_by_image_id=resume_attempts,
                        trigger=trigger,
                    ),
                    job_key=final_set_job_key(int(task["active_result_set_id"])),
                )
            return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}
    category_family = storyboard_category_family(get_styling_plan_by_id(styling_id))
    pose_selection_id = row_value(task, "active_pose_selection_id")
    selection = None
    if row_value(task, "pose_workflow_enabled", 0):
        with get_db() as db:
            selection = db.execute("SELECT * FROM generation_pose_selections WHERE id = ? AND task_id = ? AND styling_id = ? AND input_version_id = ?",
                (pose_selection_id, task_id, styling_id, task["current_input_version_id"])).fetchone()
            if not selection:
                raise HTTPException(409, "请先完成姿势选搭")
            preset_rows = db.execute("""SELECT shots.*, ? AS category_family FROM generation_pose_selection_shots shots
                JOIN media_assets pose ON pose.id = shots.pose_asset_id AND pose.deleted_at IS NULL
                JOIN media_assets background ON background.id = shots.background_asset_id AND background.deleted_at IS NULL
                WHERE selection_id = ? ORDER BY shot_index""", (category_family, pose_selection_id)).fetchall()
    if len(preset_rows) != 5:
        if row_value(task, "pose_workflow_enabled", 0):
            raise HTTPException(409, "冻结的背景或姿势图片不可用，请重新选择姿势")
        family_label = {"top": "上装", "bottom": "下装", "one_piece": "套装"}[category_family]
        raise HTTPException(status_code=409, detail=f"{family_label}拍摄预设尚未完整，请联系管理员补齐该类型的 5 个镜头图片")
    preparation = freeze_final_prompt_configuration()
    selection_plan = loads(selection["plan_json"], {}) if selection else {}
    if selection_plan.get("workflow") == "shared_background_v1":
        preparation["configRows"].update(selection_plan["promptConfigSnapshot"])
        input_version_id = int(selection["input_version_id"])
    else:
        input_version_id = create_generation_input_version(task_id)
    timestamp = now_iso()
    with get_db() as db:
        if user_instruction is not None:
            persist_generation_task_stage_instruction(db, task_id, "final_image", effective_user_instruction, timestamp)
        db.execute("UPDATE generation_tasks SET status = 'FINAL_RUNNING', updated_at = ? WHERE id = ?", (timestamp, task_id))
        cursor = db.execute(
            """
            INSERT INTO generation_result_sets
              (task_id, input_version_id, styling_id, preview_id, shot_preset_version_id, user_instruction_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)
            """,
            (task_id, input_version_id, styling_id, preview_id, None, dumps(user_instruction_context), timestamp, timestamp),
        )
        result_set_id = int(cursor.lastrowid)
        db.execute("UPDATE generation_result_sets SET prompt_preparation_json = ?, pose_selection_id = ? WHERE id = ?", (dumps(preparation), pose_selection_id, result_set_id))
        db.executemany(
            """
            INSERT INTO generation_result_set_shot_presets
              (result_set_id, category_family, shot_index, background_asset_id, pose_asset_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    result_set_id,
                    category_family,
                    int(preset["shot_index"]),
                    int(preset["background_asset_id"]),
                    int(preset["pose_asset_id"]),
                    timestamp,
                )
                for preset in preset_rows
            ],
        )
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
    preview_id: int | None,
    user_instruction_context: dict[str, Any] | None = None,
    activate: bool = False,
) -> list[int] | None:
    timestamp = now_iso()
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        if activate:
            assert_task_can_generate(db, task_id)
        retry_task = db.execute(
            "SELECT auto_run_retry_count FROM generation_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        automatic_retry = bool(retry_task and int(row_value(retry_task, "auto_run_retry_count", 0) or 0) > 0)
        result_set = db.execute(
            """
            SELECT * FROM generation_result_sets
            WHERE id = ? AND task_id = ?
            """,
            (result_set_id, task_id),
        ).fetchone()
        if not result_set or result_set["status"] == "RUNNING":
            raise HTTPException(status_code=409, detail="成片已在生成中，请勿重复提交")
        # 直接成片结果集只由当前选搭与结果集冻结的拍摄预设图片决定；历史结果集继续兼容 preview_id / 旧版本。
        if int(result_set["styling_id"]) != styling_id:
            return None
        if result_set["preview_id"] is not None and int(result_set["preview_id"]) != preview_id:
            return None
        existing_rows = db.execute(
            "SELECT * FROM generation_result_images_new WHERE result_set_id = ? ORDER BY sort_order",
            (result_set_id,),
        ).fetchall()
        existing_by_sort = {int(row["sort_order"]): row for row in existing_rows}
        if activate:
            activate_result_context(db, result_set_id)
            db.execute("UPDATE generation_result_sets SET status='PARTIAL' WHERE id=?", (result_set_id,))
        image_ids: list[int] = []
        roles = [existing_by_sort[n]['shot_role'] for n in range(1, 6)] if set(existing_by_sort) == set(range(1, 6)) else final_image_slot_roles(get_styling_plan_by_id(styling_id))
        for sort_order, shot_role in enumerate(roles, start=1):
            existing = existing_by_sort.get(sort_order)
            if existing and existing["status"] == "SUCCEEDED":
                continue
            if existing and existing["status"] in {"QUEUED", "RUNNING"}:
                raise HTTPException(status_code=409, detail="成片已在生成中，请勿重复提交")
            if existing:
                package = replay_package(db, existing)
                if package:
                    db.execute('UPDATE generation_result_images_new SET execution_snapshot_json=? WHERE id=?', (dumps(package), existing['id']))
                elif published_versions(db, existing):
                    raise HTTPException(409, '该镜头的拍摄输入记录不完整，请使用重新一键成片；原图仍可查看和下载')
                attempt = db.execute(
                    "SELECT error_code, provider_request_id FROM generation_attempts WHERE id = ?",
                    (row_value(existing, "active_attempt_id"),),
                ).fetchone()
                if automatic_retry and existing["status"] == "FAILED":
                    if not attempt or not is_auto_run_retryable_error(attempt["error_code"]):
                        continue
                preserve_provider_attempt = bool(
                    attempt
                    and attempt["provider_request_id"]
                    and (
                        attempt["error_code"] == ERROR_DELIVERY_FAILED
                        or is_status_check_retryable_error(attempt["error_code"])
                    )
                )
                db.execute(
                    """
                    UPDATE generation_result_images_new
                    SET status = 'QUEUED',
                        shot_role = ?,
                        active_attempt_id = ?,
                        locked_by = NULL,
                        locked_until = NULL,
                        user_instruction_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        shot_role,
                        row_value(existing, "active_attempt_id") if preserve_provider_attempt else None,
                        existing["user_instruction_json"],
                        timestamp,
                        int(existing["id"]),
                    ),
                )
                image_ids.append(int(existing["id"]))
                continue
            cursor = db.execute(
                """
                INSERT INTO generation_result_images_new
                  (result_set_id, shot_role, sort_order, user_instruction_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'QUEUED', ?, ?)
                """,
                (result_set_id, shot_role, sort_order, result_set["user_instruction_json"] or "{}", timestamp, timestamp),
            )
            image_ids.append(int(cursor.lastrowid))
        if image_ids:
            db.execute(
                "UPDATE generation_result_sets SET status = 'RUNNING', updated_at = ? WHERE id = ?",
                (timestamp, result_set_id),
            )
            db.execute(
                "UPDATE generation_tasks SET status = 'FINAL_RUNNING', active_result_set_id = ?, updated_at = ? WHERE id = ?",
                (result_set_id, timestamp, task_id),
            )
        else:
            reconcile_final_result_set(db, result_set_id, task_id)
    return image_ids


def freeze_final_prompt_configuration() -> dict[str, Any]:
    rows = get_prompt_config_rows()
    optimizer = rows.get(OPTIMIZER_KEY)
    enabled = bool(optimizer and row_value(optimizer, "enabled", 0))
    if enabled and not str(optimizer["content"] or "").strip():
        raise HTTPException(409, "请先在 Prompt 维护中保存成片提示词优化规则")
    return {
        "mode": "optimized" if enabled else "direct", "status": "PENDING" if enabled else "DIRECT",
        "configRows": {key: {"content": row["content"], "group_type": row["group_type"]}
                       for key, row in rows.items() if key.startswith("segment.") or key.startswith("assembly.final_")},
        "packages": {},
    }


def read_final_preparation(result_set_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT prompt_preparation_json FROM generation_result_sets WHERE id = ?", (result_set_id,)).fetchone()
    return loads(row["prompt_preparation_json"], {}) if row else {}


def save_final_preparation(result_set_id: int, preparation: dict[str, Any]) -> None:
    with get_db() as db:
        db.execute("UPDATE generation_result_sets SET prompt_preparation_json = ?, updated_at = ? WHERE id = ?",
                   (dumps(preparation), now_iso(), result_set_id))


def final_preparation_cancelled(task_id: int, result_set_id: int) -> bool:
    with get_db() as db:
        row = db.execute("SELECT status, active_result_set_id FROM generation_tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)).fetchone()
    return not row or row["status"] != "FINAL_RUNNING" or row["active_result_set_id"] != result_set_id


def vision_reference_input(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("collageItems"):
        content, _, mime = build_reference_collage_bytes(item)
    else:
        content, mime = reference_source_bytes(item)
    if not content:
        raise ValueError("参考图片内容为空")
    # Normalized originals may have a different format from their upload filename.
    from PIL import Image
    with Image.open(BytesIO(content)) as decoded:
        mime = Image.MIME.get(decoded.format, mime)
    return {"data": content, "mime_type": mime or "image/jpeg"}


def optimizer_visual_inputs(packages: dict[str, Any], inputs: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep each final prompt's local numbering while deduplicating vision attachments."""
    images, manifest, seen = [], [], {}
    for shot in inputs["shots"]:
        for reference, item in zip(shot["references"], packages[shot["shotId"]]["imageInputs"]):
            sources = item.get("collageItems") or [item]
            identity = dumps({"role": item["role"], "collageRule": item.get("collageRule"),
                              "sources": [source.get("objectKey") or source.get("url") for source in sources]})
            if identity not in seen:
                images.append(vision_reference_input(item))
                seen[identity] = len(images)
                manifest.append({"visionImageIndex": len(images), "role": item["role"], "sources": sources})
            reference["visionImageIndex"] = seen[identity]
    inputs["imageManifest"] = manifest
    return images


async def prepare_final_prompts(task_id: int, result_set_id: int, image_ids: list[int]) -> bool:
    preparation = read_final_preparation(result_set_id)
    if preparation.get("mode") != "optimized" or not image_ids:
        return True
    if final_preparation_cancelled(task_id, result_set_id):
        return False
    packages = preparation.setdefault("packages", {})
    pending: dict[str, Any] = {}
    for image_id in image_ids:
        with get_db() as db:
            image = db.execute("SELECT user_instruction_json, active_attempt_id, execution_snapshot_json FROM generation_result_images_new WHERE id = ?", (image_id,)).fetchone()
        if executable_package(json_object(image["execution_snapshot_json"])):
            continue
        if image["active_attempt_id"] and attempt_has_provider_request_id(image["active_attempt_id"]):
            continue
        cached = packages.get(str(image_id))
        instruction = loads(image["user_instruction_json"], {})
        if cached and cached.get("finalImageUserInstruction") == instruction:
            continue
        pending[str(image_id)] = None
    if not pending:
        return True
    # 重启时结束未完成的旧调用，不把它当作已完成结果，也不重置图像 provider 任务。
    old_attempt = preparation.get("attemptId")
    if preparation.get("status") == "RUNNING" and old_attempt:
        finish_attempt(old_attempt, "FAILED", error_message="拍摄准备执行中断", error_code="PROCESS_INTERRUPTED")
    attempt_id = create_attempt("generation_result_set", result_set_id, effective_text_provider())
    preparation.update(status="RUNNING", attemptId=attempt_id, error=None)
    save_final_preparation(result_set_id, preparation)
    raw_text = ""
    provider_call_started = False
    try:
        pending = {key: build_new_final_task_package(task_id, int(key)) for key in pending}
        system_prompt = preparation["configRows"][OPTIMIZER_KEY]["content"]
        request_snapshot = {"promptConfigKey": OPTIMIZER_KEY, "promptContentHash": text_hash(system_prompt),
                            "systemInstruction": system_prompt, "sourcePackages": pending,
                            **gemini_request_meta(),
                            "invocation": {"mode": "final_prompt_optimization", "providerCallStarted": False}}
        # 模型调用前保存源文本与系统提示词，供调用记录追溯。
        update_attempt_request_snapshot(attempt_id, request_snapshot)
        inputs = optimizer_input(pending)
        images = optimizer_visual_inputs(pending, inputs)
        if final_preparation_cancelled(task_id, result_set_id):
            finish_attempt(attempt_id, "CANCELLED")
            preparation["status"] = "CANCELLED"
            save_final_preparation(result_set_id, preparation)
            return False
        request_snapshot["prompt"] = dumps(inputs)
        request_snapshot["invocation"]["providerCallStarted"] = True
        update_attempt_request_snapshot(attempt_id, request_snapshot)
        provider_call_started = True
        raw_text = await generate_gemini_json_text(
            dumps(inputs), images=images, system_instruction=system_prompt,
            should_cancel=lambda: final_preparation_cancelled(task_id, result_set_id)
            or generation_task_or_attempt_cancelled(task_id, attempt_id),
        )
        optimized = parse_optimized_prompts(parse_gemini_json_object(raw_text), pending)
        for key, output in optimized.items():
            source = pending[key]
            packages[key] = {**source, "originalPrompt": source["prompt"], "prompt": output["prompt"],
                             "renderedPromptHash": text_hash(output["prompt"]),
                             "promptOptimization": {"attemptId": attempt_id, "systemPromptHash": text_hash(system_prompt), **output}}
        cancelled = final_preparation_cancelled(task_id, result_set_id) or generation_task_or_attempt_cancelled(task_id, attempt_id)
        preparation.update(status="SUCCEEDED", error=None)
        save_final_preparation(result_set_id, preparation)
        finish_attempt(attempt_id, "CANCELLED" if cancelled else "SUCCEEDED",
                       response_snapshot={"raw_text": raw_text, "shots": optimized})
        return not cancelled
    except Exception as exc:
        cancelled = final_preparation_cancelled(task_id, result_set_id) or generation_task_or_attempt_cancelled(task_id, attempt_id)
        preparation.update(status="CANCELLED" if cancelled else "FAILED",
                           error="拍摄准备未完成，本轮尚未开始生成图片。请重试或联系管理员检查拍摄要求。")
        save_final_preparation(result_set_id, preparation)
        error_code = ("PROMPT_PREPARATION_FAILED" if not provider_call_started else
                      ERROR_MALFORMED_OUTPUT if isinstance(exc, ValueError) else classify_generation_attempt_error(exc))
        finish_attempt(attempt_id, "CANCELLED" if cancelled else "FAILED", response_snapshot={"raw_text": raw_text},
                       error_message=str(exc), error_code=error_code)
        if not cancelled:
            with get_db() as db:
                for image_id in image_ids:
                    db.execute("""UPDATE generation_attempts SET status = 'FAILED', failed_at = ?, updated_at = ?,
                        error_code = 'PROMPT_PREPARATION_FAILED', error_message = '拍摄准备未完成，尚未提交图片生成'
                        WHERE id = (SELECT active_attempt_id FROM generation_result_images_new WHERE id = ?)
                        AND status = 'RUNNING' AND provider_request_id IS NULL""", (now_iso(), now_iso(), image_id))
                    db.execute("""UPDATE generation_result_images_new SET status = 'FAILED', updated_at = ?
                        WHERE id = ? AND status IN ('QUEUED', 'RUNNING') AND NOT EXISTS (
                        SELECT 1 FROM generation_attempts WHERE id = active_attempt_id AND provider_request_id IS NOT NULL)""", (now_iso(), image_id))
        return False


async def run_final_generation(
    task_id: int,
    result_set_id: int,
    image_ids: list[int],
    resume_attempts_by_image_id: dict[int, int] | None = None,
    trigger: str = "manual_step",
) -> None:
    resume_attempts_by_image_id = resume_attempts_by_image_id or {}
    prepared = await prepare_final_prompts(task_id, result_set_id, image_ids)
    # 余额硬停：任一并发成片检测到 INSUFFICIENT_BALANCE 时置位，尚未提交的兄弟图
    # 直接跳过 provider 提交（余额不足重试/继续提交只会继续报错、浪费轮询预算）。
    balance_stop = asyncio.Event()
    await asyncio.gather(
        *(
            generate_single_final_image(
                task_id,
                result_set_id,
                image_id,
                attempt_id=resume_attempts_by_image_id.get(image_id),
                trigger=trigger,
                balance_stop=balance_stop,
            )
            for image_id in (image_ids if prepared else [item for item in image_ids if attempt_has_provider_request_id(resume_attempts_by_image_id.get(item))])
        ),
        return_exceptions=True,
    )
    retry_error_codes: list[str | None] = []
    with get_db() as db:
        _, task_status = reconcile_final_result_set(db, result_set_id, task_id)
        if task_status == "FINAL_PARTIAL":
            retry_error_codes = [
                row["error_code"]
                for row in db.execute(
                    """
                    SELECT ga.error_code
                    FROM generation_result_images_new gri
                    LEFT JOIN generation_attempts ga ON ga.id = gri.active_attempt_id
                    WHERE gri.result_set_id = ? AND gri.status = 'FAILED'
                    """,
                    (result_set_id,),
                ).fetchall()
            ]
        if task_status in {"COMPLETED", "FAILED", "CANCELLED"}:
            db.execute(
                """
                UPDATE generation_tasks
                SET auto_run_target = NULL, auto_run_requested_at = NULL,
                    auto_run_retry_at = NULL, auto_run_retry_count = 0, updated_at = ?
                WHERE id = ?
                """,
                (now_iso(), task_id),
            )
    if task_status == "FINAL_PARTIAL":
        scheduled = trigger == "auto_run" and schedule_auto_run_retry(task_id, retry_error_codes)
        if not scheduled:
            clear_task_auto_run(task_id)
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


def assert_task_can_generate(db: Any, task_id: int) -> Any:
    task = db.execute('SELECT * FROM generation_tasks WHERE id=?', (task_id,)).fetchone()
    if not task or task['deleted_at']:
        raise HTTPException(404, "任务不存在")
    if task['archived_at'] or task['status'] == 'ABANDONED':
        raise HTTPException(409, "请先恢复已归档任务")
    if task['status'] in RUNNING_GENERATION_TASK_STATUSES or task['auto_run_target']:
        raise HTTPException(409, "当前任务正在执行，请等待完成或取消")
    return task


def activate_result_context(db: Any, result_set_id: int) -> None:
    row = db.execute('SELECT * FROM generation_result_sets WHERE id=?', (result_set_id,)).fetchone()
    if not row:
        raise HTTPException(404, "成片组不存在")
    db.execute("""UPDATE generation_tasks SET active_result_set_id=?, active_styling_id=?,
        current_input_version_id=?, active_pose_selection_id=?, active_preview_id=?,
        active_storyboard_preview_id=?, pose_workflow_enabled=?, updated_at=? WHERE id=?""",
        (row['id'], row['styling_id'], row['input_version_id'], row['pose_selection_id'], row['preview_id'],
         row['preview_id'], int(bool(row['pose_selection_id'])), now_iso(), row['task_id']))


def shot_replay_package(task_id: int, image_id: int, version_id: int | None = None,
                        user_instruction: str | None = None) -> dict[str, Any]:
    with get_db() as db:
        image = db.execute('SELECT * FROM generation_result_images_new WHERE id=?', (image_id,)).fetchone()
        if not image:
            raise HTTPException(404, "镜头不存在")
        try:
            package = replay_package(db, image, version_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        if package is None and (version_id is not None or published_versions(db, image)):
            raise HTTPException(409, "该图片的拍摄输入记录不完整，请使用重新一键成片；原图仍可查看和下载")
    if package is None:
        package = build_new_final_task_package(task_id, image_id)
    # JSON copy also detaches nested input lists from cached preparation packages.
    package = loads(dumps(package), {})
    if user_instruction is not None:
        if len(user_instruction) > MAX_USER_INPUT_CHARS:
            raise HTTPException(400, f"成片要求最多 {MAX_USER_INPUT_CHARS} 字")
        base = package.get('replayBasePrompt', package['prompt'])
        package['replayBasePrompt'] = base
        package['prompt'] = base + ("\n\n本次单张调整要求（更新原补充要求）：" + user_instruction if user_instruction else "")
        package['finalImageUserInstruction'] = build_stage_user_instruction_context('final_image', user_instruction)
        package['renderedPromptHash'] = text_hash(package['prompt'])
    return package


@app.post("/api/generation-result-images-new/{image_id}/retry")
async def retry_new_result_image(image_id: int, request: Request, payload: ResultImageRetryRequest | None = None) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("""SELECT i.*, s.task_id FROM generation_result_images_new i
            JOIN generation_result_sets s ON s.id=i.result_set_id WHERE i.id=?""", (image_id,)).fetchone()
        if not row:
            raise HTTPException(404, "结果图不存在")
        captured_task = dict(assert_task_can_generate(db, row['task_id']))
        if row['status'] in {'RUNNING', 'QUEUED'}:
            raise HTTPException(409, "当前结果图仍在生成中")
    package = shot_replay_package(row['task_id'], image_id, payload.versionId if payload else None,
                                  payload.userInstruction if payload else None)
    package['invocation'] = {'mode': 'manual_retry', 'target': 'final_image'}
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        task = assert_task_can_generate(db, row['task_id'])
        current = db.execute('SELECT * FROM generation_result_images_new WHERE id=?', (image_id,)).fetchone()
        if (not current or current['active_attempt_id'] != row['active_attempt_id']
                or task['detail_revision'] != captured_task['detail_revision']):
            raise HTTPException(409, "任务已变化，请刷新后重试")
        capture_published_image(db, image_id)
        attempt_id = create_attempt('generation_result_image', image_id, 'image-2', request_snapshot=package, db=db)
        db.execute('UPDATE generation_attempts SET retry_of_attempt_id=? WHERE id=?', (row['active_attempt_id'], attempt_id))
        db.execute("""UPDATE generation_result_images_new SET status='QUEUED', active_attempt_id=?,
            execution_snapshot_json=?, user_instruction_json=?, locked_by=NULL, locked_until=NULL, updated_at=? WHERE id=?""",
            (attempt_id, dumps(package), dumps(package.get('finalImageUserInstruction', {})), now_iso(), image_id))
        activate_result_context(db, row['result_set_id'])
        db.execute("UPDATE generation_result_sets SET status='RUNNING', updated_at=? WHERE id=?", (now_iso(), row['result_set_id']))
        db.execute("UPDATE generation_tasks SET status='FINAL_RUNNING', updated_at=? WHERE id=?", (now_iso(), row['task_id']))
    schedule_background_generation(run_final_generation(row['task_id'], row['result_set_id'], [image_id],
        resume_attempts_by_image_id={image_id: attempt_id}, trigger='manual_retry'), job_key=final_set_job_key(row['result_set_id']))
    return {'item': get_generation_task_by_id(row['task_id'], include_debug=admin_debug_for_request(request))}


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
    timestamp = now_iso()
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        assert_task_can_generate(db, task_id)
        current = db.execute('SELECT active_attempt_id,status FROM generation_result_images_new WHERE id=?', (image_id,)).fetchone()
        if not current or current['active_attempt_id'] != row['active_attempt_id'] or current['status'] != 'FAILED':
            raise HTTPException(409, '镜头已变化，请刷新后重试')
        attempt_id = create_attempt('generation_result_image', image_id, str(last_attempt['provider']),
                                    request_snapshot=loads(last_attempt['request_snapshot_json'], {}), db=db)
        activate_result_context(db, result_set_id)
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
    where_clauses = ["gt.deleted_at IS NULL", "(gt.batch_id IS NULL OR TRIM(gt.batch_id) = '')", "(? OR gt.archived_at IS NULL)"]
    query_params: list[Any] = [include_archived]
    keyword_terms = split_keyword_terms(keyword)
    if keyword_terms:
        term_clauses = []
        for term in keyword_terms:
            term_clauses.append("(gt.name LIKE ? OR gt.status LIKE ? OR gt.batch_id LIKE ?)")
            query_params.extend([f"%{term}%"] * 3)
        where_clauses.append("(" + " OR ".join(term_clauses) + ")")
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
            WHERE gt.deleted_at IS NULL
              AND (gt.batch_id IS NULL OR TRIM(gt.batch_id) = '')
              AND (? OR gt.archived_at IS NULL)
            """,
            (include_archived,),
        ).fetchall()
        items = rows[:limit]
        return {
            "items": generation_task_summaries(items),
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
    item = get_generation_task_by_id(task_id, include_debug=require_admin_debug(request, include_debug))
    with get_db() as db:
        ids = [r[0] for r in db.execute('SELECT id FROM generation_result_sets WHERE task_id=? ORDER BY id DESC', (task_id,))]
    item['resultSets'] = [get_generation_result_set(id) for id in ids]
    return {"item": item}


BATCH_RECORD_STATUS_ORDER = ["ARCHIVED", "RUNNING", "FAILED", "PARTIAL", "CANCELLED", "COMPLETED", "PENDING"]


def generation_batch_record_status(rows: list[Any]) -> str:
    if any(row_value(row, "archived_at") or row["status"] == "ABANDONED" for row in rows):
        return "ARCHIVED"
    statuses = [str(row["status"]) for row in rows]
    if any(status in RUNNING_GENERATION_TASK_STATUSES for status in statuses) or any(row_value(row, "auto_run_target") for row in rows):
        return "RUNNING"
    if any(status == "FAILED" for status in statuses):
        return "FAILED"
    if any(status == "FINAL_PARTIAL" for status in statuses) or (any(status == "COMPLETED" for status in statuses) and any(status != "COMPLETED" for status in statuses)):
        return "PARTIAL"
    if any(status == "CANCELLED" for status in statuses):
        return "CANCELLED"
    if statuses and all(status == "COMPLETED" for status in statuses):
        return "COMPLETED"
    return "PENDING"


def serialize_generation_batch_record(batch_id: str, rows: list[Any], context: tuple[dict[int, Any], dict[str, Any]] | None = None) -> dict[str, Any]:
    sorted_rows = sorted(rows, key=lambda row: (str(row["created_at"]), int(row["id"])))
    status_counts: dict[str, int] = {}
    archived_count = 0
    queued_count = 0
    running_count = 0
    completed_count = 0
    failed_count = 0
    cancelled_count = 0
    downloadable_count = 0
    for row in sorted_rows:
        status = str(row["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
        if row_value(row, "archived_at") or status == "ABANDONED":
            archived_count += 1
        if row_value(row, "auto_run_target"):
            queued_count += 1
        if status in RUNNING_GENERATION_TASK_STATUSES:
            running_count += 1
        if status == "COMPLETED":
            completed_count += 1
        if status == "FAILED":
            failed_count += 1
        if status == "CANCELLED":
            cancelled_count += 1
        if status in {"COMPLETED", "FINAL_PARTIAL"}:
            downloadable_count += 1
    created_values = [str(row["created_at"]) for row in sorted_rows]
    metadata = context[1].get(batch_id, {"name": fallback_generation_batch_title(batch_id), "updatedAt": None}) if context else generation_batch_metadata(batch_id)
    updated_values = [str(row["updated_at"]) for row in sorted_rows]
    if metadata["updatedAt"]:
        updated_values.append(str(metadata["updatedAt"]))
    first_row = sorted_rows[0]
    return {
        "id": batch_id,
        "batchId": batch_id,
        "name": metadata["name"],
        "status": generation_batch_record_status(sorted_rows),
        "taskCount": len(sorted_rows),
        "activeTaskCount": len(sorted_rows) - archived_count,
        "archivedTaskCount": archived_count,
        "completedTaskCount": completed_count,
        "failedTaskCount": failed_count,
        "cancelledTaskCount": cancelled_count,
        "runningTaskCount": running_count,
        "queuedTaskCount": queued_count,
        "downloadableTaskCount": downloadable_count,
        "statusCounts": status_counts,
        "createdAt": min(created_values),
        "updatedAt": max(updated_values),
        "coverTasks": [context[0][row["id"]] for row in sorted_rows[:4]] if context else generation_task_summaries(sorted_rows[:4]),
        "sampleTaskNames": [str(row["name"]) for row in sorted_rows[:3]],
        "firstTaskId": int(first_row["id"]),
    }


def generation_batch_matches_status(batch: dict[str, Any], status_values: set[str]) -> bool:
    if not status_values:
        return True
    return str(batch["status"]).casefold() in status_values


@app.get("/api/generation-record-batches")
def list_generation_record_batches(
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
    keyword_terms = split_keyword_terms(keyword)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM generation_tasks
            WHERE deleted_at IS NULL AND batch_id IS NOT NULL AND TRIM(batch_id) != ''
            ORDER BY batch_id, created_at, id
            """
        ).fetchall()
    context = generation_list_context(rows)
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(str(row["batch_id"]), []).append(row)
    batches: list[dict[str, Any]] = []
    for batch_id, batch_rows in grouped.items():
        batch = serialize_generation_batch_record(batch_id, batch_rows, context)
        if not include_archived and batch["status"] == "ARCHIVED":
            continue
        created_at = parse_filter_datetime(str(batch["createdAt"]), end_of_day=False)
        if created_from_dt and created_at and created_at < created_from_dt:
            continue
        if created_to_dt and created_at and created_at > created_to_dt:
            continue
        if keyword_terms:
            haystack = " ".join([batch_id, batch["name"], *(str(row["name"]) for row in batch_rows), *(batch.get("statusCounts") or {}).keys()]).casefold()
            if not matches_any_keyword(haystack, keyword_terms):
                continue
        if not generation_batch_matches_status(batch, status_values):
            continue
        batches.append(batch)
    sort_key = {
        "createdAt": lambda item: str(item["createdAt"]),
        "updatedAt": lambda item: str(item["updatedAt"]),
        "name": lambda item: str(item["name"]).casefold(),
        "status": lambda item: BATCH_RECORD_STATUS_ORDER.index(item["status"]) if item["status"] in BATCH_RECORD_STATUS_ORDER else len(BATCH_RECORD_STATUS_ORDER),
    }.get(sort_by, lambda item: str(item["createdAt"]))
    reverse = sort_dir != "asc"
    batches = sorted(batches, key=lambda item: (sort_key(item), str(item["batchId"])), reverse=reverse)
    total = len(batches)
    items = batches[offset:offset + limit]
    stable_batches = []
    for batch_id, batch_rows in grouped.items():
        batch = serialize_generation_batch_record(batch_id, batch_rows, context)
        if include_archived or batch["status"] != "ARCHIVED":
            stable_batches.append(batch)
    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "nextOffset": offset + limit if offset + limit < total else None,
        "hasMore": offset + limit < total,
        "total": total,
        "totalPages": (total + limit - 1) // limit if total else 0,
        "facets": {"status": facet_options(batch["status"] for batch in batches)},
        "stableFacets": {"status": facet_options(batch["status"] for batch in stable_batches)},
    }


@app.get("/api/generation-record-workspace")
def list_generation_record_workspace(
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
    keyword_terms = split_keyword_terms(keyword)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM generation_tasks
            WHERE deleted_at IS NULL
            ORDER BY created_at, id
            """
        ).fetchall()
    context = generation_list_context(rows)
    grouped: dict[str, list[Any]] = {}
    unbatched_rows = []
    for row in rows:
        batch_id = str(row_value(row, "batch_id") or "").strip()
        if batch_id:
            grouped.setdefault(batch_id, []).append(row)
        else:
            unbatched_rows.append(row)
    stable_statuses: list[str] = []
    for batch_id, batch_rows in grouped.items():
        batch = serialize_generation_batch_record(batch_id, batch_rows, context)
        if include_archived or batch["status"] != "ARCHIVED":
            stable_statuses.append(str(batch["status"]))
    for row in unbatched_rows:
        archived = bool(row_value(row, "archived_at") or row["status"] == "ABANDONED")
        if include_archived or not archived:
            stable_statuses.append(str(row["status"]))
    items: list[dict[str, Any]] = []
    for batch_id, batch_rows in grouped.items():
        batch = serialize_generation_batch_record(batch_id, batch_rows, context)
        if not include_archived and batch["status"] == "ARCHIVED":
            continue
        created_at = parse_filter_datetime(str(batch["createdAt"]), end_of_day=False)
        if created_from_dt and created_at and created_at < created_from_dt:
            continue
        if created_to_dt and created_at and created_at > created_to_dt:
            continue
        if keyword_terms:
            haystack = " ".join([batch_id, batch["name"], *(str(row["name"]) for row in batch_rows), *(batch.get("statusCounts") or {}).keys()]).casefold()
            if not matches_any_keyword(haystack, keyword_terms):
                continue
        if not generation_batch_matches_status(batch, status_values):
            continue
        items.append({
            "kind": "batch",
            "id": f"batch:{batch_id}",
            "status": batch["status"],
            "name": batch["name"],
            "createdAt": batch["createdAt"],
            "updatedAt": batch["updatedAt"],
            "batch": batch,
        })
    for row in unbatched_rows:
        archived = bool(row_value(row, "archived_at") or row["status"] == "ABANDONED")
        if not include_archived and archived:
            continue
        created_at = parse_filter_datetime(str(row["created_at"]), end_of_day=False)
        if created_from_dt and created_at and created_at < created_from_dt:
            continue
        if created_to_dt and created_at and created_at > created_to_dt:
            continue
        if keyword_terms:
            haystack = " ".join([str(row["name"]), str(row["status"]), str(row_value(row, "batch_id") or "")]).casefold()
            if not matches_any_keyword(haystack, keyword_terms):
                continue
        if status_values and str(row["status"]).casefold() not in status_values:
            continue
        task = context[0][row["id"]]
        items.append({
            "kind": "task",
            "id": f"task:{row['id']}",
            "status": str(row["status"]),
            "name": str(row["name"]),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
            "task": task,
        })
    sort_key = {
        "createdAt": lambda item: str(item["createdAt"]),
        "updatedAt": lambda item: str(item["updatedAt"]),
        "name": lambda item: str(item["name"]).casefold(),
        "status": lambda item: BATCH_RECORD_STATUS_ORDER.index(item["status"]) if item["status"] in BATCH_RECORD_STATUS_ORDER else len(BATCH_RECORD_STATUS_ORDER),
    }.get(sort_by, lambda item: str(item["createdAt"]))
    reverse = sort_dir != "asc"

    def tie_breaker(item: dict[str, Any]) -> str:
        if item["kind"] == "task":
            return f"task:{int(item['task']['id']):020d}"
        return f"batch:{item['batch']['batchId']}"

    items = sorted(items, key=lambda item: (sort_key(item), tie_breaker(item)), reverse=reverse)
    total = len(items)
    page_items = items[offset:offset + limit]
    return {
        "items": page_items,
        "limit": limit,
        "offset": offset,
        "nextOffset": offset + limit if offset + limit < total else None,
        "hasMore": offset + limit < total,
        "total": total,
        "totalPages": (total + limit - 1) // limit if total else 0,
        "facets": {"status": facet_options(item["status"] for item in items)},
        "stableFacets": {"status": facet_options(stable_statuses)},
    }


# ---------------------------------------------------------------------------
# 成片批量下载：预检 + 按任务名分文件夹流式打包
# ---------------------------------------------------------------------------

FINAL_IMAGE_EXPORT_MAX_TASKS = 50
FINAL_IMAGE_EXPORT_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
STYLING_GARMENT_EXPORT_MAX_ITEMS = 50
FINAL_IMAGE_EXPORT_STATUS_TEXT = {
    "FAILED": "生成失败",
    "RUNNING": "生成中",
    "QUEUED": "排队中",
    "CANCELLED": "已取消",
    "MISSING_ASSET": "缺少成片文件",
}


class FinalImageExportPrecheckRequest(BaseModel):
    resultSetId: int | None = None
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


def get_final_image_export_overview(db: Any, task_id: int, result_set_id: int | None = None) -> dict[str, Any] | None:
    task = db.execute('SELECT id, name, status FROM generation_tasks WHERE id=? AND deleted_at IS NULL', (task_id,)).fetchone()
    if not task:
        return None
    groups = db.execute('SELECT id FROM generation_result_sets WHERE task_id=? AND (? IS NULL OR id=?) ORDER BY id',
                        (task_id, result_set_id, result_set_id)).fetchall()
    if result_set_id is not None and not groups:
        raise HTTPException(404, '成片组不属于当前任务')
    downloadable, issues = [], []
    total_count = 0
    for group in groups:
        images = db.execute('SELECT * FROM generation_result_images_new WHERE result_set_id=? ORDER BY sort_order, id', (group['id'],)).fetchall()
        for image in images:
            versions = published_versions(db, image)
            count = 0
            for version in reversed(versions):
                asset = db.execute('SELECT * FROM media_assets WHERE id=? AND deleted_at IS NULL', (version['media_asset_id'],)).fetchone()
                if not asset:
                    continue
                count += 1
                downloadable.append({'resultSetId': group['id'], 'versionId': version['id'], 'mediaAssetId': asset['id'],
                    'sortOrder': image['sort_order'], 'shotLabel': final_image_export_shot_label(image['shot_role']),
                    'objectKey': asset['object_key'], 'mimeType': asset['mime_type'],
                    'originalFilename': asset['original_filename'], 'sizeBytes': int(asset['size_bytes'] or 0),
                    'nested': len(groups) > 1 or len(versions) > 1})
            total_count += max(1, count)
            if not count or image['status'] in {'FAILED', 'CANCELLED', 'RUNNING', 'QUEUED'}:
                issues.append({'resultSetId': group['id'], 'sortOrder': image['sort_order'],
                    'shotLabel': final_image_export_shot_label(image['shot_role']),
                    'status': image['status'] if image['status'] != 'SUCCEEDED' else 'MISSING_ASSET'})
    return {'taskId': task['id'], 'taskName': task['name'], 'taskStatus': task['status'],
            'hasResultSet': bool(groups), 'downloadable': downloadable, 'issues': issues, 'totalCount': total_count}


def collect_final_image_export_overviews(task_ids: list[int], result_set_id: int | None = None) -> list[dict[str, Any]]:
    overviews: list[dict[str, Any]] = []
    with get_db() as db:
        for task_id in task_ids:
            overview = get_final_image_export_overview(db, task_id, result_set_id)
            if not overview:
                raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
            overviews.append(overview)
    return overviews


@app.post("/api/generation-task-exports/precheck")
def precheck_final_image_export(payload: FinalImageExportPrecheckRequest) -> dict[str, Any]:
    task_ids = normalize_final_image_export_ids(payload.ids)
    overviews = collect_final_image_export_overviews(task_ids, payload.resultSetId)
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


def unique_export_filename(value: str | None, fallback: str, used: set[str]) -> str:
    """生成 ZIP 目录内安全且不重复的文件名，同时尽量保留用户导入时的原文件名。"""
    safe_name = final_image_export_safe_component(Path(str(value or "")).name) or fallback
    path = Path(safe_name)
    stem = path.stem or Path(fallback).stem or "image"
    suffix = path.suffix
    candidate = safe_name
    serial = 1
    while candidate.casefold() in used:
        serial += 1
        candidate = f"{stem}({serial}){suffix}"
    used.add(candidate.casefold())
    return candidate


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
                if entry.get('nested'):
                    filename = f"成片组-{entry['resultSetId']}/版本-{entry['versionId'] or entry['mediaAssetId']}/{filename}"
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
def download_final_image_archive(ids: str = Query(...), resultSetId: int | None = None) -> StreamingResponse:
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
    tasks = collect_final_image_export_overviews(task_ids, resultSetId)
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


def normalize_styling_garment_export_ids(raw_ids: list[int]) -> list[int]:
    garment_ids: list[int] = []
    for value in raw_ids:
        garment_id = int(value)
        if garment_id > 0 and garment_id not in garment_ids:
            garment_ids.append(garment_id)
    if not garment_ids:
        raise HTTPException(status_code=400, detail="请先选择要下载的搭配服装")
    if len(garment_ids) > STYLING_GARMENT_EXPORT_MAX_ITEMS:
        raise HTTPException(status_code=400, detail=f"单次最多下载 {STYLING_GARMENT_EXPORT_MAX_ITEMS} 个货号")
    return garment_ids


def collect_styling_garment_export_items(garment_ids: list[int]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with get_db() as db:
        for garment_id in garment_ids:
            garment = db.execute(
                "SELECT id, code FROM styling_garments WHERE id = ? AND deleted_at IS NULL",
                (garment_id,),
            ).fetchone()
            if not garment:
                raise HTTPException(status_code=404, detail=f"搭配服装 {garment_id} 不存在")
            images = db.execute(
                """
                SELECT sgi.sort_order, ma.object_key, ma.original_filename, ma.mime_type
                FROM styling_garment_images sgi
                JOIN media_assets ma ON ma.id = sgi.media_asset_id
                WHERE sgi.styling_garment_id = ?
                  AND sgi.deleted_at IS NULL
                  AND ma.deleted_at IS NULL
                ORDER BY sgi.sort_order, sgi.id
                """,
                (garment_id,),
            ).fetchall()
            if not images:
                raise HTTPException(status_code=400, detail=f"货号 {garment['code']} 没有可下载的图片")
            items.append(
                {
                    "garmentId": int(garment["id"]),
                    "code": str(garment["code"]),
                    "folder": final_image_export_safe_component(garment["code"]) or f"货号{garment_id}",
                    "images": [dict(image) for image in images],
                }
            )
    return items


def build_styling_garment_export_zip(items: list[dict[str, Any]]) -> Any:
    """在响应前完整构建 ZIP，避免对象读取失败被伪装成成功下载。"""
    handle = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024, mode="w+b")
    date_time = datetime.now().timetuple()[:6]
    try:
        with zipfile.ZipFile(handle, "w", zipfile.ZIP_STORED) as archive:
            for item in items:
                used_names: set[str] = set()
                for index, image in enumerate(item["images"], start=1):
                    content = get_bytes(image["object_key"])
                    fallback = f"image-{index}{final_image_export_extension(image['mime_type'], image['original_filename'])}"
                    filename = unique_export_filename(image["original_filename"], fallback, used_names)
                    info = zipfile.ZipInfo(f"{item['folder']}/{filename}", date_time=date_time)
                    archive.writestr(info, content)
        handle.seek(0)
        return handle
    except Exception as error:  # noqa: BLE001 - 转为用户可读的下载失败，并确保临时文件释放
        handle.close()
        logger.exception("构建搭配服装下载包失败")
        raise HTTPException(status_code=502, detail="读取搭配服装原图失败，请稍后重试") from error


def iter_temporary_export_file(handle: Any) -> Any:
    try:
        while chunk := handle.read(1024 * 1024):
            yield chunk
    finally:
        handle.close()


@app.get("/api/styling-garment-exports/archive")
def download_styling_garment_archive(ids: str = Query(...)) -> StreamingResponse:
    raw_ids: list[int] = []
    for part in ids.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            raw_ids.append(int(part))
        except ValueError as error:
            raise HTTPException(status_code=400, detail="搭配服装 ID 格式不正确") from error
    garment_ids = normalize_styling_garment_export_ids(raw_ids)
    items = collect_styling_garment_export_items(garment_ids)
    image_count = sum(len(item["images"]) for item in items)
    archive = build_styling_garment_export_zip(items)
    with get_db() as db:
        log_event(
            db,
            "styling_garment_export",
            None,
            "styling_garment_images_exported",
            {"garmentIds": garment_ids, "imageCount": image_count},
        )
    timestamp = now_iso()[:19].replace("-", "").replace(":", "").replace("T", "-")
    return StreamingResponse(
        iter_temporary_export_file(archive),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="styling-garments-{timestamp}.zip"'},
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
        optimizer_history = key == OPTIMIZER_KEY and facts.get("promptRuntime") and facts.get("generation", {}).get("stage") == "final_image"
        if facts.get("promptConfigKey") != key and key not in prompt_keys and not optimizer_history:
            raise HTTPException(status_code=400, detail="调用记录与 Prompt 配置不匹配")
        if isinstance(facts.get("promptRuntime"), dict):
            runtime = facts["promptRuntime"]
        historical_prompt = facts.get("systemInstruction") if key == OPTIMIZER_KEY else facts.get("prompt")
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
            """SELECT id, owner_type, provider, status, created_at FROM generation_attempts
            WHERE json_extract(request_snapshot_json, '$.promptConfigKey') = ?
            OR EXISTS (SELECT 1 FROM json_each(request_snapshot_json, '$.promptConfigKeys') WHERE value = ?)
            OR (? AND owner_type = 'generation_result_image' AND request_snapshot_json LIKE '%promptRuntime%')
            ORDER BY created_at DESC, id DESC LIMIT 30""",
            (key, key, key == OPTIMIZER_KEY),
        ).fetchall()
    return {"items": [{"id": row["id"], "ownerType": row["owner_type"], "provider": row["provider"], "status": row["status"], "createdAt": row["created_at"]} for row in rows]}


@app.patch("/api/prompt-configs/{config_key}")
def update_prompt_config(config_key: str, payload: PromptConfigUpdateRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    key = normalize_prompt_config_key(config_key)
    content = payload.content[:120000]
    if key == OPTIMIZER_KEY and payload.enabled and not content.strip():
        raise HTTPException(400, "启用前请保存成片提示词优化规则")
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
        if key == OPTIMIZER_KEY and payload.enabled is not None:
            db.execute("UPDATE prompt_configs SET enabled = ? WHERE key = ?", (int(payload.enabled), key))
        log_event(db, "prompt_config", None, "updated", {"key": key})
    return {"item": get_prompt_config(key), "preview": preview_prompt_config(key, request)}


@app.post("/api/prompt-configs/{config_key}/test-optimization")
async def test_prompt_optimization(config_key: str, payload: PromptOptimizationTestRequest, request: Request) -> dict[str, Any]:
    require_admin(request)
    if config_key not in {OPTIMIZER_KEY, *FINAL_IMAGE_PROMPT_CONFIG_KEYS.values()}:
        raise HTTPException(400, "仅成片 Prompt 支持优化测试")
    with get_db() as db:
        attempt = db.execute("SELECT * FROM generation_attempts WHERE id = ?", (payload.attemptId,)).fetchone()
        if not attempt:
            raise HTTPException(404, "调用记录不存在")
        facts = loads(attempt["request_snapshot_json"], {})
        if attempt["owner_type"] == "generation_result_image":
            image = db.execute("SELECT result_set_id FROM generation_result_images_new WHERE id = ?", (attempt["owner_id"],)).fetchone()
            result_set_id = image["result_set_id"] if image else None
            source = {**facts, "prompt": facts.get("originalPrompt") or facts.get("prompt", "")}
            packages = {str(attempt["owner_id"]): source}
        elif attempt["owner_type"] == "generation_result_set":
            result_set_id = attempt["owner_id"]
            packages = facts.get("sourcePackages", {})
        else:
            raise HTTPException(400, "请选择成片调用记录")
    if not result_set_id or not packages or any(not item.get("promptRuntime") for item in packages.values()):
        raise HTTPException(400, "该历史记录缺少成片上下文，请选择其他记录")
    system_prompt = payload.content[:120000] if config_key == OPTIMIZER_KEY else prompt_config_content(OPTIMIZER_KEY) or ""
    if not system_prompt.strip():
        raise HTTPException(400, "请先保存成片提示词优化规则")
    if config_key != OPTIMIZER_KEY:
        for package in packages.values():
            if package.get("promptConfigKey") != config_key:
                raise HTTPException(400, "调用记录与 Prompt 配置不匹配")
            rendered, diagnostics = render_prompt_config(config_key, package["promptRuntime"], content_overrides={config_key: payload.content[:120000]})
            if diagnostics["missingPlaceholders"]:
                raise HTTPException(400, "草稿缺少必要占位符")
            package["prompt"] = rendered
    inputs = optimizer_input(packages)
    snapshot = {"promptConfigKey": OPTIMIZER_KEY, "systemInstruction": system_prompt, "prompt": dumps(inputs),
                "promptContentHash": text_hash(system_prompt), "sourcePackages": packages,
                **gemini_request_meta(), "invocation": {"mode": "optimization_test", "sourceAttemptId": payload.attemptId}}
    attempt_id = create_attempt("generation_result_set", result_set_id, effective_text_provider(), request_snapshot=snapshot)
    raw_text = ""
    try:
        images = optimizer_visual_inputs(packages, inputs)
        snapshot["prompt"] = dumps(inputs)
        update_attempt_request_snapshot(attempt_id, snapshot)
        raw_text = await generate_gemini_json_text(dumps(inputs), images=images, system_instruction=system_prompt)
        results = parse_optimized_prompts(parse_gemini_json_object(raw_text), packages)
        finish_attempt(attempt_id, "SUCCEEDED", response_snapshot={"raw_text": raw_text, "shots": results})
    except Exception as exc:
        finish_attempt(attempt_id, "FAILED", response_snapshot={"raw_text": raw_text}, error_message=str(exc), error_code=ERROR_MALFORMED_OUTPUT if isinstance(exc, ValueError) else classify_generation_attempt_error(exc))
        raise HTTPException(422, f"优化测试失败，记录 #{attempt_id}：{str(exc)[:500]}") from exc
    return {"attemptId": attempt_id, "items": [{"shotId": key, "originalPrompt": packages[key]["prompt"], **value} for key, value in results.items()]}


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
        raise HTTPException(status_code=400, detail="至少导入一张头像")
    clean_angle = sanitize_reference_key(angle or "") if angle else None
    if clean_angle and clean_angle not in HEAD_REFERENCE_ANGLES:
        raise HTTPException(status_code=400, detail="头像角度不合法")
    if clean_angle and len(uploads) > 1:
        raise HTTPException(status_code=400, detail="指定角度时只能导入一张头像")
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


SHOT_PRESET_FAMILIES = ("top", "bottom", "one_piece")
SHOT_PRESET_INDICES = tuple(range(1, 6))


def shot_preset_validation_errors(db: Any, category_family: str | None = None) -> list[str]:
    rows = db.execute(
        """
        SELECT sp.*
        FROM shot_preset_slots sp
        JOIN media_assets bg ON bg.id = sp.background_asset_id AND bg.deleted_at IS NULL
        JOIN media_assets pose ON pose.id = sp.pose_asset_id AND pose.deleted_at IS NULL
        """
    ).fetchall()
    by_slot = {(str(row["category_family"]), int(row["shot_index"])): row for row in rows}
    errors: list[str] = []
    for family in (category_family,) if category_family else SHOT_PRESET_FAMILIES:
        for shot_index in SHOT_PRESET_INDICES:
            row = by_slot.get((family, shot_index))
            if not row:
                errors.append(f"{family} 镜头 {shot_index} 缺少拍摄母版或姿势图")
    return errors


def serialize_shot_presets() -> dict[str, Any]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM shot_preset_slots ORDER BY category_family, shot_index"
        ).fetchall()
        family_errors = {
            family: shot_preset_validation_errors(db, family)
            for family in SHOT_PRESET_FAMILIES
        }
        available_asset_ids = {
            int(row["id"])
            for row in db.execute("SELECT id FROM media_assets WHERE deleted_at IS NULL").fetchall()
        }
    by_slot = {(str(row["category_family"]), int(row["shot_index"])): row for row in rows}
    slots: list[dict[str, Any]] = []
    for family in SHOT_PRESET_FAMILIES:
        for shot_index in SHOT_PRESET_INDICES:
            row = by_slot.get((family, shot_index))
            background_asset_id = int(row["background_asset_id"]) if row and row["background_asset_id"] else None
            pose_asset_id = int(row["pose_asset_id"]) if row and row["pose_asset_id"] else None
            slots.append(
                {
                    "categoryFamily": family,
                    "shotIndex": shot_index,
                    "background": get_media_asset_by_id(background_asset_id) if background_asset_id else None,
                    "pose": get_media_asset_by_id(pose_asset_id) if pose_asset_id else None,
                    "complete": bool(
                        background_asset_id in available_asset_ids
                        and pose_asset_id in available_asset_ids
                    ),
                }
            )
    return {
        "families": {
            family: {
                "complete": not family_errors[family],
                "configuredCount": 5 - len(family_errors[family]),
                "validationErrors": family_errors[family],
            }
            for family in SHOT_PRESET_FAMILIES
        },
        "slots": slots,
    }


@app.get("/api/shot-presets", dependencies=[Depends(require_admin)])
def get_shot_presets(request: Request) -> dict[str, Any]:
    return serialize_shot_presets()


async def inspect_shot_preset_upload(upload: UploadFile) -> tuple[tuple[int, int], bool]:
    content = await upload.read()
    await upload.seek(0)
    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            return image.size, "A" in image.getbands()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{upload.filename or '图片'} 无法识别") from exc


@app.post("/api/shot-presets/{category_family}/{shot_index}")
async def set_shot_preset_slot(
    category_family: str,
    shot_index: int,
    request: Request,
    background: UploadFile = File(...),
    pose: UploadFile = File(...),
) -> dict[str, Any]:
    raise HTTPException(status_code=410, detail="拍摄预设改为单张图片更换后立即生效")


def normalize_shot_preset_asset_role(value: str) -> tuple[str, str]:
    normalized = value.strip().lower()
    if normalized in {"background", "master"}:
        return "background", "background_asset_id"
    if normalized == "pose":
        return "pose", "pose_asset_id"
    raise HTTPException(status_code=400, detail="拍摄预设图片类型不合法")


@app.post("/api/shot-presets/{category_family}/{shot_index}/{asset_role}")
async def replace_shot_preset_asset(
    category_family: str,
    shot_index: int,
    asset_role: str,
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    require_admin(request)
    family = normalize_shot_category_family(category_family)
    if family not in SHOT_PRESET_FAMILIES or shot_index not in SHOT_PRESET_INDICES:
        raise HTTPException(status_code=400, detail="拍摄预设槽位不合法")
    _, column = normalize_shot_preset_asset_role(asset_role)
    await inspect_shot_preset_upload(file)
    asset = (await save_uploads_as_media_assets([file]))[0]
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT * FROM shot_preset_slots WHERE category_family = ? AND shot_index = ?",
            (family, shot_index),
        ).fetchone()
        db.execute(
            f"""
            INSERT INTO shot_preset_slots
              (category_family, shot_index, {column}, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(category_family, shot_index) DO UPDATE SET
              {column} = excluded.{column}, updated_at = excluded.updated_at
            """,
            (family, shot_index, int(asset["id"]), timestamp, timestamp),
        )
        previous_asset_id = int(previous[column]) if previous and previous[column] else None
        if previous_asset_id:
            release_media_asset_if_unreferenced(db, previous_asset_id)
    return serialize_shot_presets()


@app.delete("/api/shot-presets/{category_family}/{shot_index}/{asset_role}")
def delete_shot_preset_asset(category_family: str, shot_index: int, asset_role: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    family = normalize_shot_category_family(category_family)
    if family not in SHOT_PRESET_FAMILIES or shot_index not in SHOT_PRESET_INDICES:
        raise HTTPException(status_code=400, detail="拍摄预设槽位不合法")
    _, column = normalize_shot_preset_asset_role(asset_role)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM shot_preset_slots WHERE category_family = ? AND shot_index = ?",
            (family, shot_index),
        ).fetchone()
        if not row or not row[column]:
            raise HTTPException(status_code=404, detail="拍摄预设图片不存在")
        asset_id = int(row[column])
        db.execute(
            f"UPDATE shot_preset_slots SET {column} = NULL, updated_at = ? WHERE id = ?",
            (now_iso(), int(row["id"])),
        )
        release_media_asset_if_unreferenced(db, asset_id)
    return serialize_shot_presets()


@app.post("/api/shot-preset-families/{category_family}/publish")
def publish_shot_presets(category_family: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    raise HTTPException(status_code=410, detail="拍摄预设图片更换后立即生效，无需发布")


def serialize_shoot_environment() -> dict[str, Any]:
    asset_id = get_current_shoot_environment_asset_id()
    return {"asset": get_media_asset_by_id(asset_id) if asset_id else None}


@app.get("/api/shoot-environment")
def get_shoot_environment() -> dict[str, Any]:
    return serialize_shoot_environment()


@app.post("/api/shoot-environment")
async def set_shoot_environment(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    require_admin(request)
    assets = await save_uploads_as_media_assets([file])
    if not assets:
        raise HTTPException(status_code=400, detail="拍摄环境参考图导入失败")
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
            raise HTTPException(status_code=404, detail="预览暂不可用", headers={"Cache-Control": "private, no-store"})
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


def build_user_requirement_context(task: Any) -> dict[str, Any]:
    upload_requirement = normalize_user_requirement_text(row_value(task, "user_requirement_text", ""))
    # 只保留任务级服装信息；各步骤的临时介入要求使用独立 runtime.*_user_instruction。
    return {
        "schema_version": "user-requirement-v1",
        "upload_requirement": upload_requirement,
    }




async def generation_poller_loop() -> None:
    last_upload_cleanup_at = 0.0
    while True:
        await asyncio.sleep(settings.generation_poll_interval_seconds)
        try:
            refresh_pending_new_generation_jobs()
            if time.monotonic() - last_upload_cleanup_at >= 3600:
                await asyncio.to_thread(cleanup_deleted_upload_sessions)
                await asyncio.to_thread(cleanup_stale_upload_sessions)
                last_upload_cleanup_at = time.monotonic()
        except Exception as exc:  # pragma: no cover - background safety net
            logger.exception("Generation poller failed")
            try:
                with get_db() as db:
                    log_event(db, "generation_poller", None, "failed", {"error": str(exc)})
            except Exception:
                logger.exception("Could not persist generation poller failure")


async def tagging_recovery_loop() -> None:
    """Keep tag retries independent of generation polling and its cleanup work."""
    while True:
        await asyncio.sleep(settings.generation_poll_interval_seconds)
        for operation in (expire_stale_styling_garment_tagging, resume_due_styling_garment_tagging_retries):
            try:
                operation()
            except Exception:
                logger.exception("Tagging recovery operation failed: %s", operation.__name__)


def resume_due_styling_garment_tagging_retries() -> None:
    """领取到期打标并切到执行态；再次崩溃后由 stale 检测累计进程恢复次数。"""
    timestamp = now_iso()
    lease_until = (
        datetime.now(timezone.utc) + timedelta(seconds=settings.generation_image_timeout_seconds)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        rows = db.execute(
            """SELECT id FROM styling_garments
               WHERE status = 'TAGGING_RUNNING' AND deleted_at IS NULL
                 AND archived_at IS NULL AND tag_retry_at IS NOT NULL AND tag_retry_at <= ?
                 AND (tag_retry_lease_until IS NULL OR tag_retry_lease_until <= ?)
               ORDER BY tag_retry_at, id
               LIMIT ?""",
            (timestamp, timestamp, max(1, settings.tagging_max_concurrency)),
        ).fetchall()
    for row in rows:
        garment_id = int(row["id"])
        with _active_tagging_jobs_lock:
            if garment_id in _active_styling_garment_tagging_ids:
                continue
        with get_db() as db:
            cursor = db.execute(
                """UPDATE styling_garments
                   SET tag_retry_at = NULL, tag_retry_lease_until = ?, updated_at = ?
                   WHERE id = ? AND status = 'TAGGING_RUNNING'
                     AND tag_retry_at IS NOT NULL AND tag_retry_at <= ?
                     AND (tag_retry_lease_until IS NULL OR tag_retry_lease_until <= ?)""",
                (lease_until, timestamp, garment_id, timestamp, timestamp),
            )
        if not cursor.rowcount:
            continue
        with _active_tagging_jobs_lock:
            _active_styling_garment_tagging_ids.add(garment_id)
        asyncio.create_task(run_styling_garment_tagging(garment_id))


def cleanup_deleted_upload_sessions() -> int:
    """Finalize user-requested deletion after every issued direct-upload URL is safely expired."""
    safe_before = (
        datetime.now(timezone.utc) - timedelta(seconds=settings.s3_presign_expires_seconds + 60)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            """
            SELECT id, status
            FROM upload_sessions
            WHERE archive_reason IN ('deletion_pending', 'deletion_failed')
              AND updated_at <= ?
            ORDER BY updated_at, id
            LIMIT ?
            """,
            (safe_before, settings.upload_session_cleanup_batch_size),
        ).fetchall()
        session_ids = [str(row["id"]) for row in rows]
        finalized_ids = [str(row["id"]) for row in rows if row["status"] == "FINALIZED"]
        deferred_ids = [str(row["id"]) for row in rows if row["status"] != "FINALIZED"]
        enqueue_upload_session_cleanup_and_delete(db, finalized_ids)
        enqueue_upload_session_cleanup_and_delete(db, deferred_ids, include_protected_keys=True)
    process_upload_object_cleanup_queue(limit=settings.upload_session_cleanup_batch_size * 20)
    return len(session_ids)


def cleanup_stale_upload_sessions() -> int:
    """Abandon expired, unfinalized upload sessions and delete only their exact objects."""
    timestamp = now_iso()
    retry_message = "导入批次已过期，文件清理待重试"
    stale_before = (
        datetime.now(timezone.utc) - timedelta(hours=settings.upload_session_ttl_hours)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        sessions = db.execute(
            """
            SELECT id
            FROM upload_sessions
            WHERE (
                (
                  status IN ('PENDING', 'UPLOADING', 'UPLOADED', 'FAILED')
                  AND updated_at <= ?
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                ) OR (status = 'ABANDONED' AND error_message = ?)
              )
              AND NOT EXISTS (
                SELECT 1 FROM upload_session_items linked
                WHERE linked.session_id = upload_sessions.id
                  AND linked.media_asset_id IS NOT NULL
              )
              AND NOT EXISTS (
                SELECT 1
                FROM upload_session_items linked
                JOIN media_assets ma ON ma.object_key = linked.object_key AND ma.deleted_at IS NULL
                WHERE linked.session_id = upload_sessions.id
              )
            ORDER BY CASE WHEN status = 'ABANDONED' THEN 0 ELSE 1 END, updated_at, id
            LIMIT ?
            """,
            (stale_before, timestamp, retry_message, settings.upload_session_cleanup_batch_size),
        ).fetchall()
        session_ids = [str(row["id"]) for row in sessions]
        if not session_ids:
            return 0
        placeholders = ", ".join("?" for _ in session_ids)
        object_rows = db.execute(
            f"SELECT session_id, object_key FROM upload_session_items WHERE session_id IN ({placeholders})",
            session_ids,
        ).fetchall()
        db.execute(
            f"""
            UPDATE upload_session_items
            SET status = 'ABANDONED', error_message = '导入批次已过期', updated_at = ?
            WHERE session_id IN ({placeholders})
            """,
            (timestamp, *session_ids),
        )
        db.execute(
            f"""
            UPDATE upload_sessions
            SET status = 'ABANDONED', error_message = '导入批次已过期',
                lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (timestamp, *session_ids),
        )
    keys_by_session: dict[str, list[str]] = {session_id: [] for session_id in session_ids}
    for row in object_rows:
        keys_by_session[str(row["session_id"])].extend(image_object_keys(str(row["object_key"])))
    cleaned_ids: list[str] = []
    failed_ids: list[str] = []
    for session_id in session_ids:
        failed = False
        for key in dict.fromkeys(keys_by_session[session_id]):
            try:
                delete_object(key)
            except Exception:
                failed = True
        (failed_ids if failed else cleaned_ids).append(session_id)
    with get_db() as db:
        if failed_ids:
            failed_placeholders = ", ".join("?" for _ in failed_ids)
            db.execute(
                f"UPDATE upload_sessions SET error_message = ?, updated_at = ? WHERE id IN ({failed_placeholders})",
                (retry_message, timestamp, *failed_ids),
            )
            log_event(db, "upload_session_cleanup", None, "cleanup_failed", {"sessionIds": failed_ids})
        if cleaned_ids:
            log_event(db, "upload_session_cleanup", None, "expired", {"sessionIds": cleaned_ids})
    return len(cleaned_ids)


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
        db.execute("BEGIN IMMEDIATE")
        pending_cleanup = db.execute(
            "SELECT 1 FROM upload_object_cleanup_queue WHERE object_key = ? LIMIT 1",
            (asset["objectKey"],),
        ).fetchone()
        if pending_cleanup:
            raise HTTPException(status_code=409, detail="导入记录正在彻底删除，不能登记为正式媒体")
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
        raise HTTPException(status_code=400, detail=f"{filename} 尚未导入完成")
    object_size_bytes = int(head.get("contentLength") or 0)
    if object_size_bytes <= 0:
        raise HTTPException(status_code=400, detail=f"{filename} 是空文件")
    if object_size_bytes > settings.max_upload_image_bytes:
        max_mb = settings.max_upload_image_bytes / (1024 * 1024)
        raise HTTPException(status_code=400, detail=f"{filename} 超过 {max_mb:.0f}MB 大小限制")
    content = get_bytes(object_key)
    content_type, _, _ = validate_image_content(content, filename, upload.mimeType or str(head.get("contentType") or ""))
    return process_uploaded_content_as_media_asset(content, content_type, filename, object_key, timestamp)


async def save_uploads_as_media_assets(files: list[UploadFile]) -> list[dict[str, Any]]:
    timestamp = now_iso()
    saved: list[dict[str, Any]] = []
    saved_object_keys: list[str] = []
    saved_asset_ids: list[int] = []
    try:
        for upload in files:
            content, content_type = await validate_upload_image(upload)
            filename = sanitize_filename(upload.filename or "image")
            key = new_media_original_key(filename)
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


async def save_uploaded_objects_as_media_assets(uploads: list[UploadedMediaRequest], preserve_originals_on_failure: bool = False) -> list[dict[str, Any]]:
    timestamp = now_iso()
    saved: list[dict[str, Any]] = []
    saved_object_keys: list[str] = []
    saved_asset_ids: list[int] = []
    try:
        for upload in uploads:
            object_key = ensure_direct_upload_key(upload.objectKey)
            if not preserve_originals_on_failure:
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
    content_hash = hashlib.sha256(content).hexdigest()
    width = height = None
    with suppress(Exception):
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            """
            SELECT id, hash
            FROM media_assets
            WHERE object_key = ? AND deleted_at IS NULL
            ORDER BY id
            LIMIT 1
            """,
            (object_key,),
        ).fetchone()
        if existing:
            existing_hash = str(existing["hash"] or "")
            if existing_hash and existing_hash != content_hash:
                raise RuntimeError(f"生成媒体对象键内容哈希冲突: {object_key}")
            if not existing_hash:
                db.execute(
                    "UPDATE media_assets SET hash = ?, updated_at = ? WHERE id = ?",
                    (content_hash, timestamp, int(existing["id"])),
                )
            return int(existing["id"])
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
                content_hash,
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
        "thumbStatus": "AVAILABLE" if row_value(row, "thumb_key") else "MISSING",
        "previewStatus": "AVAILABLE" if row_value(row, "preview_key") else "MISSING",
        "thumbUrl": stable_media_url(row["object_key"], "thumb") if row_value(row, "thumb_key") else None,
        "previewUrl": stable_media_url(row["object_key"], "preview") if row_value(row, "preview_key") else None,
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
    tag_retry_state = loads(row["tag_retry_state_json"], {}) if "tag_retry_state_json" in row.keys() else {}
    if not isinstance(tag_retry_state, dict):
        tag_retry_state = {}
    tag_retry_kind = str(tag_retry_state.get("kind") or "") or None
    retry_count_key = {
        "contract": "contractRetries",
        "process": "processRetries",
        "transport": "transportRetries",
    }.get(tag_retry_kind or "", "transportRetries")
    tag_retry_count = int(tag_retry_state.get(retry_count_key) or 0)
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
        "taggingErrorCode": row["tag_error_code"] if row["status"] == "TAGGING_FAILED" and "tag_error_code" in row.keys() else None,
        "tagRetryAt": row["tag_retry_at"] if "tag_retry_at" in row.keys() else None,
        "tagRetryCount": tag_retry_count,
        "tagRetryLimit": int(tag_retry_state.get("retryLimit") or settings.gemini_retry_limit),
        "tagRetryKind": tag_retry_kind,
        "tagSchemaVersion": row["tag_schema_version"],
        "tagCompletedAt": row["tag_completed_at"],
        "tagError": row["tag_error"] if include_debug else None,
        "archivedAt": row["archived_at"] if "archived_at" in row.keys() else None,
        "archiveReason": row["archive_reason"] if "archive_reason" in row.keys() else None,
        "imageCount": row["image_count"] if "image_count" in row.keys() else None,
        "coverUrl": stable_media_url(row["cover_key"], "thumb") if row_value(row, "cover_thumb_key") else None,
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
        "thumbStatus": "AVAILABLE" if row_value(row, "cover_thumb_key") else "MISSING",
        "previewStatus": "AVAILABLE" if row_value(row, "cover_preview_key") else "MISSING",
        "thumbUrl": stable_media_url(row["cover_key"], "thumb") if row_value(row, "cover_thumb_key") else None,
        "previewUrl": stable_media_url(row["cover_key"], "preview") if row_value(row, "cover_preview_key") else None,
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
              (SELECT ma.thumb_key FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_thumb_key,
              (SELECT ma.preview_key FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_preview_key,
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
        message = f"{gemini_not_configured_message()}AI 识别不能生成替代标签。"
        attempt_id = create_attempt("styling_garment_tagging", garment_id, effective_text_provider(), request_snapshot)
        finish_attempt(attempt_id, "FAILED", {"error": "gemini_not_configured"}, error_message=message)
        raise RuntimeError(message)
    attempt_id, retry_state = ensure_styling_garment_tagging_attempt(garment_id, request_snapshot)
    resume_snapshot = get_attempt_response_snapshot(attempt_id)
    cached_result = resume_snapshot.get("taggingResult")
    if resume_snapshot.get("providerSucceeded") is True and isinstance(cached_result, dict):
        return {
            **cached_result,
            "attempt_id": attempt_id,
            "_attempt_response_snapshot": resume_snapshot,
        }
    images: list[dict[str, Any]] = []
    for image in garment.get("images", [])[:MAX_GARMENT_IMAGE_INPUTS]:
        asset = image.get("asset") or {}
        image_input = gemini_vision_input(asset.get("objectKey"), asset.get("mimeType"))
        if image_input:
            images.append(image_input)
    retry_limit = max(0, settings.gemini_malformed_reask_limit)
    contract_retries = int(retry_state.get("contractRetries") or 0)
    transport_retries = int(retry_state.get("transportRetries") or 0)
    current_prompt = (
        build_gemini_contract_correction_prompt(prompt, str(retry_state.get("lastError") or "上一次契约校验失败"))
        if contract_retries > 0
        else prompt
    )
    while True:
        try:
            text = await generate_gemini_json_text(
                current_prompt,
                images,
                on_retry=lambda snapshot: schedule_styling_garment_tag_retry(
                    garment_id,
                    attempt_id,
                    retry_state,
                    snapshot,
                    kind="transport",
                ),
                initial_retry_count=transport_retries,
                defer_retry=True,
            )
        except GeminiRetryDeferred:
            raise
        except Exception as exc:
            if classify_attempt_error(exc) == ERROR_MALFORMED_OUTPUT and contract_retries < retry_limit:
                contract_retries += 1
                retry_delay = generation_retry_backoff_seconds(contract_retries)
                snapshot = {
                    **exception_process_facts(exc),
                    "status": "TAGGING_CONTRACT_RETRY_WAIT",
                    "contractRetries": contract_retries,
                    "retryLimit": retry_limit,
                    "retryDelaySeconds": retry_delay,
                    "contractError": str(exc)[:500],
                }
                schedule_styling_garment_tag_retry(
                    garment_id,
                    attempt_id,
                    retry_state,
                    snapshot,
                    kind="contract",
                )
                logger.warning(
                    "AI 打标输出截断/畸形，自动纠错重试第 %d 次，退避 %.1fs：%s",
                    contract_retries,
                    retry_delay,
                    str(exc)[:200],
                )
                raise GeminiRetryDeferred(snapshot) from exc
            finish_attempt(
                attempt_id,
                "FAILED",
                response_snapshot={
                    **get_attempt_response_snapshot(attempt_id),
                    **exception_process_facts(exc),
                    "transportRetries": transport_retries,
                    "retryLimit": settings.gemini_retry_limit,
                },
                error_message=str(exc),
                error_code=classify_attempt_error(exc),
            )
            raise
        raw: dict[str, Any] | None = None
        try:
            raw = parse_lily_tagging_json(text)
            tagging_result = normalize_prompt_tagging_result(raw, garment)
        except UploadRequirementNotMetError as exc:
            finish_attempt(
                attempt_id,
                "FAILED",
                {"raw_text": text, "raw_json": raw},
                error_message=f"AI 打标结果未通过导入要求：{exc}",
                error_code=ERROR_UPLOAD_REQUIREMENT_NOT_MET,
            )
            raise GeminiProviderError(
                f"AI 打标结果未通过导入要求：{exc}",
                ERROR_UPLOAD_REQUIREMENT_NOT_MET,
            ) from exc
        except Exception as exc:
            if contract_retries >= retry_limit:
                finish_attempt(
                    attempt_id,
                    "FAILED",
                    {
                        "raw_text": text,
                        "raw_json": raw,
                        "contractRetries": contract_retries,
                    },
                    error_message=f"AI 打标结果连续不符合契约：{exc}",
                    error_code=ERROR_MALFORMED_OUTPUT,
                )
                raise GeminiProviderError(
                    f"AI 打标结果连续不符合契约（已自动重试 {contract_retries} 次）：{exc}",
                    ERROR_MALFORMED_OUTPUT,
                ) from exc
            contract_retries += 1
            retry_delay = generation_retry_backoff_seconds(contract_retries)
            retry_snapshot = {
                "status": "TAGGING_CONTRACT_RETRY_WAIT",
                "raw_text": text,
                "raw_json": raw,
                "contractRetries": contract_retries,
                "retryDelaySeconds": retry_delay,
                "contractError": str(exc)[:500],
            }
            schedule_styling_garment_tag_retry(
                garment_id,
                attempt_id,
                retry_state,
                retry_snapshot,
                kind="contract",
            )
            logger.warning(
                "AI 打标契约输出异常，自动纠错重试第 %d 次，退避 %.1fs：%s",
                contract_retries,
                retry_delay,
                str(exc)[:200],
            )
            raise GeminiRetryDeferred(retry_snapshot) from exc
        success_snapshot = {
            "status": "TAGGING_PROVIDER_SUCCEEDED",
            "providerSucceeded": True,
            "raw_text": text,
            "raw_json": raw,
            "contractRetries": contract_retries,
            "transportRetries": transport_retries,
            "taggingResult": tagging_result,
        }
        persist_attempt_provider_progress(attempt_id, success_snapshot)
        return {
            **tagging_result,
            "attempt_id": attempt_id,
            "_attempt_response_snapshot": success_snapshot,
        }


def ensure_styling_garment_tagging_attempt(
    garment_id: int,
    request_snapshot: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    with get_db() as db:
        row = db.execute(
            """SELECT sg.tag_active_attempt_id, sg.tag_retry_state_json, ga.status AS attempt_status
               FROM styling_garments sg
               LEFT JOIN generation_attempts ga ON ga.id = sg.tag_active_attempt_id
               WHERE sg.id = ?""",
            (garment_id,),
        ).fetchone()
    retry_state = loads(row["tag_retry_state_json"], {}) if row else {}
    if not isinstance(retry_state, dict):
        retry_state = {}
    if row and row["tag_active_attempt_id"] and row["attempt_status"] == "RUNNING":
        return int(row["tag_active_attempt_id"]), retry_state
    attempt_id = create_attempt("styling_garment_tagging", garment_id, effective_text_provider(), request_snapshot)
    with get_db() as db:
        db.execute(
            "UPDATE styling_garments SET tag_active_attempt_id = ?, updated_at = ? WHERE id = ?",
            (attempt_id, now_iso(), garment_id),
        )
    return attempt_id, retry_state


def schedule_styling_garment_tag_retry(
    garment_id: int,
    attempt_id: int | None,
    previous_state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    kind: str,
    require_stale_before: str | None = None,
) -> None:
    delay = max(0.0, float(snapshot.get("retryDelaySeconds") or 0))
    retry_at = (
        datetime.now(timezone.utc) + timedelta(seconds=delay)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    # 服装行只保存调度所需的紧凑状态；原始模型输出继续只进入 attempt 快照。
    state = {
        "transportRetries": int(previous_state.get("transportRetries") or 0),
        "contractRetries": int(previous_state.get("contractRetries") or 0),
        "processRetries": int(previous_state.get("processRetries") or 0),
        "status": snapshot.get("status"),
        "kind": kind,
        "retryAt": retry_at,
        "retryLimit": int(snapshot.get("retryLimit") or settings.gemini_retry_limit),
        "retryDelaySeconds": delay,
        "errorCode": snapshot.get("errorCode"),
        "lastError": snapshot.get("contractError") or snapshot.get("error"),
    }
    if kind == "transport":
        state["transportRetries"] = int(snapshot.get("transportRetries") or 0)
    elif kind == "contract":
        state["contractRetries"] = int(snapshot.get("contractRetries") or 0)
    elif kind == "process":
        state["processRetries"] = int(snapshot.get("processRetries") or 0)
    with get_db() as db:
        cursor = db.execute(
            """UPDATE styling_garments
               SET status = 'TAGGING_RUNNING', tag_retry_at = ?, tag_retry_state_json = ?,
                   tag_active_attempt_id = ?, tag_retry_lease_until = NULL, updated_at = ?
               WHERE id = ? AND status = 'TAGGING_RUNNING'
                 AND (? IS NULL OR (tag_retry_at IS NULL AND updated_at < ?))""",
            (
                retry_at,
                dumps(state),
                attempt_id,
                now_iso(),
                garment_id,
                require_stale_before,
                require_stale_before,
            ),
        )
        if not cursor.rowcount:
            return
        log_event(
            db,
            "styling_garment",
            garment_id,
            "tagging_retry_scheduled",
            {"kind": kind, "retryAt": retry_at, "retryCount": state.get(f"{kind}Retries")},
        )
    persist_attempt_provider_progress(
        attempt_id,
        {
            **get_attempt_response_snapshot(attempt_id),
            **snapshot,
            "kind": kind,
            "retryAt": retry_at,
        },
    )


def build_gemini_contract_correction_prompt(prompt: str, error: str) -> str:
    """在不改变业务 Prompt 的前提下追加协议纠错事实。"""
    reason = re.sub(r"\s+", " ", str(error)).strip()[:500]
    return (
        f"{prompt.rstrip()}\n\n"
        "【上一次返回的协议纠错】\n"
        f"上一次返回未通过系统契约校验：{reason}\n"
        "请重新检查全部必需顶层字段及字段类型，只输出符合当前 Prompt 约定的严格 JSON；"
        "不要输出 Markdown、解释文字或额外包裹。\n"
    )


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


class AIStylistContractError(ValueError, RuntimeError):
    """AI Stylist returned JSON that does not match the production contract."""


class UploadRequirementNotMetError(AIStylistContractError):
    """打标结果没有选出可作为搭配服装入库的商品图。"""

    error_code = ERROR_UPLOAD_REQUIREMENT_NOT_MET


def is_ai_stylist_contract_retryable(exc: BaseException) -> bool:
    return classify_attempt_error(exc) == ERROR_MALFORMED_OUTPUT


def ai_stylist_response_diagnostic(
    *,
    raw_text: str | None = None,
    raw_json: Any = None,
    parse_error: BaseException | str | None = None,
    contract_error: BaseException | str | None = None,
    contract_retries: int = 0,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "stage": "ai_stylist_response",
        "contractRetries": contract_retries,
    }
    if raw_text is not None:
        snapshot["raw_text"] = raw_text
        snapshot["rawTextTruncated"] = len(raw_text) > 200_000
    if raw_json is not None:
        snapshot["raw_json"] = raw_json
    if parse_error is not None:
        snapshot["parse_error"] = str(parse_error)[:1000]
    if contract_error is not None:
        snapshot["contract_error"] = str(contract_error)[:1000]
    return snapshot


def selected_image_ids_from_files(selected_files: Any, images: list[dict[str, Any]], limit: int) -> list[int]:
    if not isinstance(selected_files, list) or not selected_files:
        raise AIStylistContractError("selected_files 必须是非空数组")
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
            raise AIStylistContractError("selected_files 只能包含非空字符串")
        ref = raw_ref.strip()
        image_id = file_to_id.get(ref) or id_to_id.get(ref)
        if image_id is None:
            matches = filename_to_ids.get(ref) or []
            if len(matches) == 1:
                image_id = matches[0]
            elif len(matches) > 1:
                raise AIStylistContractError(f"selected_files 引用文件名不唯一：{ref}")
        if image_id is None:
            raise AIStylistContractError(f"selected_files 引用了不存在的文件：{ref}")
        if image_id in selected_ids:
            raise AIStylistContractError(f"selected_files 重复引用文件：{ref}")
        selected_ids.append(image_id)
    return selected_ids


def apply_styling_garment_selected_image_order(db: Any, garment_id: int, selected_image_ids: list[int], timestamp: str) -> None:
    rows = db.execute(
        """
        SELECT id, media_asset_id, sort_order
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
        asset_ids_by_image_id = {int(row["id"]): int(row["media_asset_id"]) for row in rows if row["media_asset_id"] is not None}
        placeholders = ",".join("?" for _ in unselected_ids)
        db.execute(f"DELETE FROM styling_garment_images WHERE styling_garment_id = ? AND id IN ({placeholders})", (garment_id, *unselected_ids))
        for image_id in unselected_ids:
            release_media_asset_if_unreferenced(db, asset_ids_by_image_id.get(image_id))


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
    provider: str | None = None,
    warnings: list[str] | None = None,
    review_reasons: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("AI 打标结果必须是 JSON 对象")
    selected_files = raw.get("selected_files")
    if isinstance(selected_files, list) and not selected_files:
        raise UploadRequirementNotMetError("selected_files 必须是非空数组")
    selected_image_ids = selected_image_ids_from_files(selected_files, garment.get("images") or [], MAX_GARMENT_IMAGE_INPUTS)[:MAX_GARMENT_SELECTED_IMAGES]
    ai_tags = raw.get("ai_tags")
    if not isinstance(ai_tags, dict):
        raise ValueError("AI 打标结果缺少 ai_tags 对象")
    tag_meta = build_prompt_tag_meta(ai_tags, garment, provider=provider or effective_text_provider(), warnings=warnings, review_reasons=review_reasons)
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
        "tagger_provider": provider,
        "tagger_model": effective_gemini_model(),
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
    """调用 Gemini 取 JSON，解析失败时按统一 10 次预算纠错重问。

    分层的容错策略（避免重复扣费）：
    1. generate_gemini_json_text 内部已对"零产出瞬时错误"(502/连接错/429)做安全重发；
       此层不感知那些，只处理"已拿到文本但解析不出合法 JSON"和截断。
    2. parse_gemini_json_object 已尽力做容错解析（剥控制字符、提取首个完整 JSON、
       宽松解析、pythonish 兜底）。仍失败才走重问。
    3. 重问：重新发起 Gemini 调用并追加协议纠错事实，最多
       gemini_malformed_reask_limit 次，使用 30/60/120/240 秒封顶退避。截断
       (finish_reason=length) 同样归 MALFORMED_OUTPUT，一并走重问。
    4. 重问仍失败 → 抛 GeminiProviderError(MALFORMED_OUTPUT)，由上层判死。

    余额/鉴权/网关等 error_code 直接透传（不吞不重问），交由上层策略处理。
    """
    reask_limit = max(0, settings.gemini_malformed_reask_limit)
    attempt = 0
    transport_retries = 0
    reask_prompt = prompt

    def remember_transport_retry(snapshot: dict[str, Any]) -> None:
        nonlocal transport_retries
        transport_retries = max(transport_retries, int(snapshot.get("transportRetries") or 0))

    while True:
        parse_error: BaseException
        try:
            text = await generate_gemini_json_text(
                reask_prompt,
                images,
                system_instruction=system_instruction,
                on_retry=remember_transport_retry,
                initial_retry_count=transport_retries,
            )
            return parse_gemini_json_object(text)
        except GeminiProviderError as provider_error:
            if provider_error.error_code != ERROR_MALFORMED_OUTPUT:
                raise
            parse_error = provider_error
        except Exception as error:
            parse_error = error
        if attempt >= reask_limit:
            raise GeminiProviderError(
                f"模型返回内容不是合法 JSON（已重问 {attempt} 次）：{parse_error}",
                ERROR_MALFORMED_OUTPUT,
            ) from parse_error
        attempt += 1
        reask_prompt = build_gemini_contract_correction_prompt(prompt, str(parse_error))
        retry_delay = generation_retry_backoff_seconds(attempt)
        logger.warning(
            "模型返回 JSON 解析失败，自动纠错重问第 %d 次，退避 %.1fs：%s",
            attempt,
            retry_delay,
            str(parse_error)[:200],
        )
        if retry_delay > 0:
            await asyncio.sleep(retry_delay)


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


def user_facing_provider_error_detail(label: str, exc: BaseException, error_code: str | None) -> dict[str, Any]:
    """把技术报错翻译成结构化用户文案；技术码和 provider 原文只留在 attempt 详情。

    余额类必须明确提示充值，绝不表现成"生成失败请重试"（会让用户白等、继续烧额度）。
    """
    response = getattr(exc, "provider_response", None)
    provider_error = response.get("providerError") if isinstance(response, dict) else None
    reason = provider_error.get("upstreamMessage") if isinstance(provider_error, dict) else None
    presentation = provider_error_presentation(error_code, str(reason or ""))
    return {
        **presentation,
        "stage": label,
        "detail": f"{label}：{presentation['title']}。{presentation['message']}",
    }


def user_facing_provider_error_message(label: str, exc: BaseException, error_code: str | None) -> str:
    detail = user_facing_provider_error_detail(label, exc, error_code)
    return str(detail["detail"])


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
            raise HTTPException(status_code=400, detail="请先导入主推款图片")
        assert_media_assets_available(image_rows, label="主推款图片")
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


def assert_media_assets_available(rows: list[Any], label: str = "图片") -> None:
    broken: list[str] = []
    for index, row in enumerate(rows, start=1):
        object_key = row_value(row, "object_key")
        filename = row_value(row, "original_filename") or row_value(row, "filename") or f"第 {index} 张"
        if not object_key:
            broken.append(f"{filename} 缺少存储路径")
            continue
        head = object_head(str(object_key))
        if not head:
            broken.append(f"{filename} 文件不存在")
            continue
        if int(head.get("contentLength") or 0) <= 0:
            broken.append(f"{filename} 文件为空")
            continue
        try:
            content = get_bytes(str(object_key))
        except Exception:
            broken.append(f"{filename} 文件无法读取")
            continue
        if not content:
            broken.append(f"{filename} 文件为空")
    if broken:
        preview = "；".join(broken[:3])
        if len(broken) > 3:
            preview += f"；另有 {len(broken) - 3} 张"
        raise GenerationPhaseError(
            f"{label}不可用：{preview}。请重新导入这些图片后再生成。",
            ERROR_MEDIA_UNAVAILABLE,
            provider_response={"mediaHealth": {"label": label, "broken": broken}},
        )


def create_attempt(owner_type: str, owner_id: int, provider: str, request_snapshot: dict[str, Any] | None = None, *, db: Any = None) -> int:
    if db is None:
        with get_db() as connection:
            return create_attempt(owner_type, owner_id, provider, request_snapshot, db=connection)
    timestamp = now_iso()
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


def generation_attempt_cancelled(attempt_id: int | None) -> bool:
    if attempt_id is None:
        return False
    with get_db() as db:
        row = db.execute("SELECT status FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
    return not row or row["status"] == "CANCELLED"


def finish_attempt(
    attempt_id: int,
    status: str,
    response_snapshot: dict[str, Any] | None = None,
    output_asset_id: int | None = None,
    error_message: str | None = None,
    error_code: str | None = None,
    db: Any | None = None,
) -> None:
    timestamp = now_iso()
    if db is None:
        with get_db() as connection:
            finish_attempt(attempt_id, status, response_snapshot, output_asset_id, error_message, error_code, db=connection)
        return
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


def get_attempt_response_snapshot(attempt_id: int | None) -> dict[str, Any]:
    """读取 attempt 已持久化的事实，用于终态合并，避免覆盖重试计数。"""
    if not attempt_id:
        return {}
    with get_db() as db:
        row = db.execute(
            "SELECT response_snapshot_json FROM generation_attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()
    snapshot = loads(row["response_snapshot_json"], {}) if row else {}
    return snapshot if isinstance(snapshot, dict) else {}


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
    if current in {PHASE_PREPARE, PHASE_UPLOAD, None}:
        return ERROR_UPLOAD_FAILED, f"本地生成任务在 {current or 'prepare'} 阶段执行中断，可安全重新导入并继续。"
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
              (SELECT ma.thumb_key FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_thumb_key,
              (SELECT ma.preview_key FROM styling_garment_images sgi JOIN media_assets ma ON ma.id = sgi.media_asset_id
               WHERE sgi.styling_garment_id = sg.id AND sgi.deleted_at IS NULL ORDER BY sgi.sort_order, sgi.id LIMIT 1) AS cover_preview_key,
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
    def ensure_styling_active() -> None:
        if attempt_id and generation_task_or_attempt_cancelled(int(input_row["task_id"]), attempt_id):
            raise RuntimeError("智能选搭已取消，停止后续模型调用")

    ensure_styling_active()
    if not is_gemini_configured():
        raise RuntimeError(f"{gemini_not_configured_message()}AI Stylist 不能进入生产选搭。")
    if not request_snapshot.get("prompt"):
        request_snapshot = attach_ai_stylist_prompt_snapshot(request_snapshot, candidate_snapshot)
    if attempt_id:
        update_attempt_request_snapshot(attempt_id, request_snapshot)
    base_prompt = str(request_snapshot.get("prompt") or "")
    prompt = base_prompt
    images: list[dict[str, Any]] = []
    for item in loads(input_row["image_snapshot_json"], [])[:MAX_HERO_IMAGE_INPUTS]:
        asset = (item.get("asset") or {})
        image_input = gemini_vision_input(asset.get("objectKey"), asset.get("mimeType"))
        if image_input:
            images.append(image_input)
    retry_limit = max(0, settings.ai_stylist_contract_retry_limit)
    retry_delay = max(0.0, float(settings.ai_stylist_contract_retry_delay_seconds))
    contract_retries = 0
    transport_retries = 0
    latest_response: dict[str, Any] | None = None

    def record_transport_retry(snapshot: dict[str, Any]) -> None:
        nonlocal transport_retries
        ensure_styling_active()
        transport_retries = max(transport_retries, int(snapshot.get("transportRetries") or 0))
        persist_attempt_provider_progress(attempt_id, snapshot)

    while True:
        ensure_styling_active()
        try:
            if progress:
                progress.begin(
                    "styling_gemini",
                    imageCount=len(images),
                    candidateCount=len(candidate_snapshot.get("candidate_material_ids") or []),
                    contractRetry=contract_retries,
                )
            text = await generate_gemini_json_text(
                prompt,
                images,
                # 系统指令已并入 AI 选搭 Prompt 正文（prompt 内），不再单独下发 system_instruction。
                system_instruction=request_snapshot.get("systemInstruction"),
                on_retry=record_transport_retry,
                should_cancel=lambda: bool(attempt_id) and generation_task_or_attempt_cancelled(int(input_row["task_id"]), attempt_id),
                initial_retry_count=transport_retries,
            )
            ensure_styling_active()
            latest_response = ai_stylist_response_diagnostic(raw_text=text, contract_retries=contract_retries)
            persist_attempt_provider_progress(attempt_id, latest_response)
            try:
                raw = parse_gemini_json_object(text)
            except Exception as parse_error:
                latest_response = ai_stylist_response_diagnostic(
                    raw_text=text,
                    parse_error=parse_error,
                    contract_retries=contract_retries,
                )
                persist_attempt_provider_progress(attempt_id, latest_response)
                raise GeminiProviderError(
                    f"模型返回内容不是合法 JSON：{parse_error}",
                    ERROR_MALFORMED_OUTPUT,
                    provider_response=latest_response,
                ) from parse_error
            latest_response = ai_stylist_response_diagnostic(
                raw_text=text,
                raw_json=raw,
                contract_retries=contract_retries,
            )
            persist_attempt_provider_progress(attempt_id, latest_response)
            try:
                styling_plan = normalize_ai_stylist_result(raw, input_row, candidate_snapshot)
                validate_styling_plan(styling_plan, candidate_snapshot)
                return styling_plan
            except AIStylistContractError as contract_error:
                latest_response = ai_stylist_response_diagnostic(
                    raw_text=text,
                    raw_json=raw,
                    contract_error=contract_error,
                    contract_retries=contract_retries,
                )
                persist_attempt_provider_progress(attempt_id, latest_response)
                raise GeminiProviderError(
                    f"AI Stylist 结果不符合契约：{contract_error}",
                    ERROR_MALFORMED_OUTPUT,
                    provider_response=latest_response,
                ) from contract_error
        except Exception as exc:
            ensure_styling_active()
            if not is_ai_stylist_contract_retryable(exc):
                raise wrap_gemini_call_error("AI Stylist 调用失败", exc) from exc
            if contract_retries >= retry_limit:
                raise wrap_gemini_call_error("AI Stylist 调用失败", exc) from exc
            contract_retries += 1
            current_retry_delay = min(
                retry_delay * (2 ** max(0, contract_retries - 1)),
                float(settings.generation_retry_backoff_cap_seconds),
            )
            retry_snapshot = {
                **(latest_response or exception_process_facts(exc)),
                "status": "STYLING_CONTRACT_RETRY_WAIT",
                "contractRetries": contract_retries,
                "retryDelaySeconds": current_retry_delay,
                "error": str(exc)[:500],
            }
            if attempt_id:
                persist_attempt_provider_progress(attempt_id, retry_snapshot)
            if progress:
                progress.begin(
                    "styling_contract_retry_wait",
                    contractRetry=contract_retries,
                    retryDelaySeconds=current_retry_delay,
                )
            logger.warning(
                "AI Stylist 契约输出异常，自动重试第 %d 次，退避 %.1fs：%s",
                contract_retries,
                current_retry_delay,
                str(exc)[:200],
            )
            prompt = build_gemini_contract_correction_prompt(base_prompt, str(exc))
            if current_retry_delay > 0:
                await asyncio.sleep(current_retry_delay)


def normalize_ai_stylist_result(raw: Any, input_row: Any, candidate_snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AIStylistContractError("AI Stylist 结果必须是 JSON 对象")
    selected_image_ids = selected_image_ids_from_files(
        raw.get("selected_files"),
        loads(input_row["image_snapshot_json"], []),
        MAX_HERO_IMAGE_INPUTS,
    )[:MAX_HERO_SELECTED_IMAGES]
    plan_source = raw.get("styling_plan")
    if not isinstance(plan_source, dict):
        raise AIStylistContractError("AI Stylist 结果缺少 styling_plan 对象")
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
    # 当前模型只返回最终方案业务字段，不承担版本标记。旧多候选格式仅保留读取兼容。
    if "outfit_candidates" not in source or source.get("schema_version") == "styling-plan-v2":
        source = adapt_final_styling_plan(source, candidate_snapshot)
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
        "schema_version": source.get("schema_version") or "styling-plan-v1",
        "empty_reason": source.get("empty_reason", ""),
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


def adapt_final_styling_plan(source: dict[str, Any], candidate_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate the compact LLM contract, then adapt only its chosen outfit for legacy consumers."""
    required = {"hero_understanding", "selected_materials", "empty_reason",
                "generated_accessories", "wearing_notes", "reason_summary"}
    # 兼容旧 Prompt 已返回的版本元信息，但不要求模型生成它。
    if set(source) - {"schema_version"} != required:
        raise AIStylistContractError("最终造型方案业务字段缺失或包含额外字段")
    hero = source["hero_understanding"]
    hero_keys = {"category", "shot_category_family", "hero_visual_summary", "hero_selling_points"}
    if not isinstance(hero, dict) or set(hero) != hero_keys:
        raise AIStylistContractError("hero_understanding 商品理解字段缺失或包含额外字段")
    for key in ("category", "hero_visual_summary"):
        if not isinstance(hero[key], str) or not hero[key].strip():
            raise AIStylistContractError(f"hero_understanding.{key} 必须为非空字符串")
    if hero["shot_category_family"] not in ("top", "bottom", "one_piece"):
        raise AIStylistContractError("shot_category_family 只能为 top、bottom、one_piece")
    for key, value, maximum in (("hero_selling_points", hero["hero_selling_points"], 8),
                                 ("generated_accessories", source["generated_accessories"], 6)):
        if not isinstance(value, list) or len(value) > maximum or any(not isinstance(item, str) or not item.strip() for item in value):
            raise AIStylistContractError(f"{key} 必须为字符串数组，最多 {maximum} 项")
    for key, maximum in (("wearing_notes", 180), ("reason_summary", 180)):
        if not isinstance(source[key], str) or len(source[key]) > maximum:
            raise AIStylistContractError(f"{key} 必须为不超过 {maximum} 字的字符串")
    if not source["reason_summary"].strip():
        raise AIStylistContractError("reason_summary 不能为空")
    materials = source["selected_materials"]
    if not isinstance(materials, list) or len(materials) > 4:
        raise AIStylistContractError("selected_materials 必须为数组，最多 4 件")
    available = candidate_cards_by_code(candidate_snapshot)
    codes: set[str] = set()
    for item in materials:
        if not isinstance(item, dict) or set(item) != {"material_code", "wearing_method"}:
            raise AIStylistContractError("selected_materials 每项仅允许 material_code 与 wearing_method")
        code = item["material_code"]
        if not isinstance(code, str) or code not in available or code in codes:
            raise AIStylistContractError("selected_materials 包含不存在或重复的商品 code")
        if not isinstance(item["wearing_method"], str) or not item["wearing_method"].strip() or len(item["wearing_method"]) > 120:
            raise AIStylistContractError("wearing_method 必须为 1–120 字的穿法说明")
        codes.add(code)
    reason = source["empty_reason"]
    if (materials and reason != "") or (not materials and reason not in ("complete_look", "no_suitable_match", "user_requested")):
        raise AIStylistContractError("empty_reason 必须与是否选择库内服装一致")
    # Keep existing snapshot/UI/pose/final-image readers working without invented critiques or scores.
    return {
        **source,
        "schema_version": "styling-plan-v2",  # 内部存储兼容标记，不属于模型输出契约。
        "selected_outfit_key": "selected_final",
        "outfit_candidates": [{
            "outfit_key": "selected_final",
            "title": "推荐造型",
            "selected_materials": materials,
            "generated_accessories": source["generated_accessories"],
            "wearing_notes": source["wearing_notes"],
            "hero_focus": hero["hero_selling_points"],
        }],
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


def redact_deleted_material_codes(value: Any, deleted_codes: set[str]) -> Any:
    """Redact deleted product identifiers from public historical responses.

    Stored snapshots remain immutable for audit and generation recovery; only the
    serialized API view is redacted.
    """
    codes = {code.strip() for code in deleted_codes if code and code.strip()}
    if not codes:
        return value
    if isinstance(value, str):
        redacted = value
        for code in sorted(codes, key=len, reverse=True):
            redacted = re.sub(
                rf"(?<![A-Za-z0-9_-]){re.escape(code)}(?![A-Za-z0-9_-])",
                "已删除搭配服装",
                redacted,
            )
        return redacted
    if isinstance(value, list):
        return [redact_deleted_material_codes(item, codes) for item in value]
    if isinstance(value, dict):
        return {key: redact_deleted_material_codes(item, codes) for key, item in value.items()}
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
        raise AIStylistContractError("AI Stylist 返回空 JSON，未生成有效 StylingPlan")
    candidate_ids = {int(item) for item in candidate_snapshot.get("candidate_material_ids", [])}
    selected_ids = plan.get("selected_material_ids") if isinstance(plan.get("selected_material_ids"), list) else []
    selected_key = str(plan.get("selected_outfit_key") or plan.get("selected_outfit_id") or "")
    outfit_keys = {
        str(item.get("outfit_key") or item.get("id") or "")
        for item in plan.get("outfit_candidates", [])
        if isinstance(item, dict)
    }
    if not selected_key or selected_key not in outfit_keys:
        raise AIStylistContractError("AI Stylist 返回的 selected_outfit_key 不存在于 outfit_candidates")
    invalid = set(int(item) for item in selected_ids) - candidate_ids
    if invalid:
        raise AIStylistContractError(f"AI Stylist 返回了非法搭配服装 ID：{sorted(invalid)}")
    if len(selected_ids) != len(set(selected_ids)):
        raise AIStylistContractError("AI Stylist 返回了重复搭配服装 ID")
    if plan.get("plan_source_mode") == "fallback":
        raise AIStylistContractError("AI Stylist 工程 fallback 方案不能进入生产链路")
    try:
        storyboard_category_family(plan)
    except RuntimeError as exc:
        raise AIStylistContractError(str(exc)) from exc


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


def compact_prompt_runtime_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text.strip("；;。 ")


def build_shot_styling_line(styling_plan: dict[str, Any]) -> str:
    selected = selected_outfit_from_plan(styling_plan)
    selected_codes = ensure_string_list(styling_plan.get("selected_material_codes"))
    generated_accessories = ensure_string_list(selected.get("generated_accessories"))
    parts = [
        f"选中搭配服装：{'、'.join(selected_codes) if selected_codes else '无库内配角'}",
        *[f"{item.get('material_code')}穿法：{item.get('wearing_method')}"
          for item in styling_plan.get("selected_materials", [])
          if isinstance(item, dict) and item.get("material_code") and item.get("wearing_method")],
        selected.get("wearing_notes"),
        f"生成类配饰：{'、'.join(generated_accessories)}" if generated_accessories else "",
    ]
    return "；".join(part for part in (compact_prompt_runtime_text(item) for item in parts) if part)


def get_styling_source_and_selected_snapshots(selected_ids: list[int]) -> tuple[list[int], list[dict[str, Any]]]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM styling_garments WHERE deleted_at IS NULL AND status = 'READY' ORDER BY updated_at DESC, id DESC").fetchall()
    source_ids = [int(row["id"]) for row in rows]
    selected_set = set(selected_ids)
    invalid = selected_set - set(source_ids)
    if invalid:
        raise RuntimeError(f"模型返回了非法搭配服装 ID：{sorted(invalid)}")
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
    return [f"{family}_shot_{index}" for index in range(1, 6)]


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
    user_requirement = loads(row_value(input_row, "user_requirement_json"), build_user_requirement_context({}))
    prompt_runtime = build_preview_prompt_runtime(styling_plan, image_inputs, storyboard_preview_user_instruction, user_requirement)
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
    user_requirement: dict[str, Any] | None = None,
) -> str:
    # 渲染层只输出「可变数据」：参考图职能图号区间、用户要求和选中造型摘要。
    # 六镜头策略、职能描述与事实源约束由 Prompt 维护模板正文托管（配合条件段）。
    runtime = build_preview_prompt_runtime(styling_plan, image_inputs or [], storyboard_preview_user_instruction or {}, user_requirement or {})
    return render_prompt_template(storyboard_prompt_config_key(styling_plan), runtime)


def build_preview_prompt_runtime(
    styling_plan: dict[str, Any],
    image_inputs: list[dict[str, Any]],
    storyboard_preview_user_instruction: dict[str, Any],
    user_requirement: dict[str, Any],
) -> dict[str, Any]:
    spans = render_image_reference_spans(image_inputs or [])
    return {
        "category_family": storyboard_category_family(styling_plan),
        "user_requirement": render_user_requirement_line(user_requirement or {}),
        "shot_styling": build_shot_styling_line(styling_plan),
        "ref_model_identity": spans["model_identity"],
        "ref_hero_garment": spans["hero_garment"],
        "ref_styling_garment": spans["styling_garment"],
        "ref_shoot_environment": spans["shoot_environment"],
        "storyboard_preview_user_instruction": render_user_instruction_line(storyboard_preview_user_instruction or {}),
    }


def build_new_final_task_package(task_id: int, result_image_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gri.*, grs.styling_id, grs.input_version_id, grs.preview_id,
                   grs.shot_preset_version_id, grs.prompt_preparation_json, grs.pose_selection_id,
                   grs.user_instruction_json AS result_set_user_instruction_json,
                   gp.preview_asset_id, gp.environment_asset_id
            FROM generation_result_images_new gri
            JOIN generation_result_sets grs ON grs.id = gri.result_set_id
            LEFT JOIN generation_previews gp ON gp.id = grs.preview_id
            WHERE gri.id = ?
            """,
            (result_image_id,),
        ).fetchone()
    if not row:
        raise RuntimeError("成片槽位不存在")
    with get_db() as db:
        saved_package = replay_package(db, row)
    if saved_package:
        return saved_package
    styling_plan = get_styling_plan_by_id(int(row["styling_id"]))
    with get_db() as db:
        preset = db.execute(
            """
            SELECT * FROM generation_result_set_shot_presets
            WHERE result_set_id = ? AND category_family = ? AND shot_index = ?
            """,
            (
                int(row["result_set_id"]),
                storyboard_category_family(styling_plan),
                int(row["sort_order"]),
            ),
        ).fetchone()
    if not preset and row["shot_preset_version_id"]:
        with get_db() as db:
            preset = db.execute(
                """
                SELECT * FROM shot_presets
                WHERE version_id = ? AND category_family = ? AND shot_index = ?
                """,
                (
                    int(row["shot_preset_version_id"]),
                    storyboard_category_family(styling_plan),
                    int(row["sort_order"]),
                ),
            ).fetchone()
    if not preset:
        raise RuntimeError("当前成片冻结的拍摄预设不完整")
    image_inputs = build_direct_final_image_inputs(
        int(row["styling_id"]),
        int(row["input_version_id"]),
        preset,
    )
    final_image_user_instruction = loads(
        row_value(row, "user_instruction_json"),
        loads(row_value(row, "result_set_user_instruction_json"), build_stage_user_instruction_context("final_image")),
    )
    prompt_config_key = final_image_prompt_config_key(styling_plan)
    input_row = None
    with get_db() as db:
        input_row = db.execute(
            "SELECT user_requirement_json FROM generation_task_input_versions WHERE id = ?",
            (int(row["input_version_id"]),),
        ).fetchone()
    user_requirement = loads(row_value(input_row, "user_requirement_json"), build_user_requirement_context({}))
    prompt_runtime = build_final_prompt_runtime(int(row["sort_order"]), str(row["shot_role"]), styling_plan, image_inputs, final_image_user_instruction, user_requirement)
    selection = get_generation_pose_selection(row_value(row, "pose_selection_id"))
    if selection:
        shot = next((item for item in selection["shots"] if item["shotIndex"] == int(row["sort_order"])), None)
        if not shot:
            raise RuntimeError("冻结的姿势选搭缺少当前镜头")
        # 姿势动作只注入一次：模板已有独立的“已选姿势”行，重复拼接会把
        # 姿势标签 JSON 塞进配饰与造型描述，稀释商品与造型事实。
        prompt_runtime["shot_pose"] = render_pose_direction(shot["direction"])
    preparation = loads(row_value(row, "prompt_preparation_json"), {})
    frozen_rows = preparation.get("configRows")
    if frozen_rows:
        prompt, diagnostics = render_prompt_config(prompt_config_key, prompt_runtime, rows=frozen_rows)
        if diagnostics["missingPlaceholders"]:
            raise RuntimeError(f"冻结的成片配置缺失：{diagnostics['missingPlaceholders']}")
    else:
        prompt = render_prompt_template(prompt_config_key, prompt_runtime)
    config_content = prompt_config_content(prompt_config_key, frozen_rows) or ""
    return {
        "generation": {"stage": "final_image", "workflow": selection.get("workflow") if selection else "legacy_masters"},
        "prompt": prompt,
        "categoryFamily": storyboard_category_family(styling_plan),
        "promptConfigKey": prompt_config_key, "promptContentHash": text_hash(config_content),
        "promptConfigContent": config_content, "promptRuntime": prompt_runtime, "renderedPromptHash": text_hash(prompt),
        "promptConfigSnapshot": frozen_rows,
        "imageInputs": image_inputs,
        "shotPresetVersionId": row["shot_preset_version_id"],
        "shotPresetSnapshotId": int(preset["id"]),
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
    user_requirement: dict[str, Any] | None = None,
) -> str:
    # 渲染层只输出当前镜头、参考图职能区间、造型事实和用户要求。
    runtime = build_final_prompt_runtime(shot_index, shot_role, styling_plan, image_inputs or [], final_image_user_instruction or {}, user_requirement or {})
    return render_prompt_template(final_image_prompt_config_key(styling_plan), runtime)


def build_final_prompt_runtime(
    shot_index: int,
    shot_role: str,
    styling_plan: dict[str, Any],
    image_inputs: list[dict[str, Any]],
    final_image_user_instruction: dict[str, Any],
    user_requirement: dict[str, Any],
) -> dict[str, Any]:
    spans = render_image_reference_spans(image_inputs or [])
    runtime = {
        "shot_index": str(shot_index),
        "shot_pose": "",
        "user_requirement": render_user_requirement_line(user_requirement or {}),
        "shot_styling": build_shot_styling_line(styling_plan),
        "ref_model_identity": spans["model_identity"],
        "ref_hero_garment": spans["hero_garment"],
        "ref_styling_garment": spans["styling_garment"],
        "ref_shot_background": spans["shot_background"],
        "ref_shot_pose": spans["shot_pose"],
        "final_image_user_instruction": render_user_instruction_line(final_image_user_instruction or {}),
    }
    for index in range(1, 6):
        runtime[f"is_shot_{index}"] = "1" if int(shot_index) == index else ""
    return runtime


def build_direct_final_image_inputs(
    styling_id: int,
    input_version_id: int,
    preset: Any,
) -> list[dict[str, Any]]:
    category_family = str(preset["category_family"] or "")
    shot_index = int(preset["shot_index"] or 0)
    background = get_media_asset_by_id(int(preset["background_asset_id"]))
    pose = get_media_asset_by_id(int(preset["pose_asset_id"]))
    if not background or not pose:
        raise RuntimeError("冻结的拍摄背景或姿势图不可用")
    pose_input = image_input_from_asset(pose, "shot_pose")
    pose_crop_ratio = IMAGE2_DIRECT_POSE_CROP.get((category_family, shot_index))
    if pose_crop_ratio:
        pose_input["providerCrop"] = {"kind": "keep_top_ratio", "ratio": pose_crop_ratio}
    inputs: list[dict[str, Any]] = [
        image_input_from_asset(background, "shot_background"),
        pose_input,
    ]
    with get_db() as db:
        styling = db.execute(
            "SELECT selected_material_snapshot_json FROM generation_stylings WHERE id = ?",
            (styling_id,),
        ).fetchone()
        input_version = db.execute(
            "SELECT image_snapshot_json, model_snapshot_json FROM generation_task_input_versions WHERE id = ?",
            (input_version_id,),
        ).fetchone()
    if not input_version:
        raise RuntimeError("成片输入版本不存在")

    model_snapshot = loads(input_version["model_snapshot_json"], {})
    model_sources = resolve_model_snapshot_image_inputs(model_snapshot, limit=IMAGE2_DIRECT_MODEL_SOURCE_LIMIT)
    model_sheet = reference_sheet_image_input(
        "model_identity",
        model_sources,
        filename="model-identity-sheet.jpg",
        max_items=IMAGE2_DIRECT_MODEL_SOURCE_LIMIT,
    )
    if model_sheet:
        inputs.append(model_sheet)

    hero_snapshot = [item for item in loads(input_version["image_snapshot_json"], []) if isinstance(item, dict)]
    hero_sources = [image_input_from_asset(item.get("asset"), "hero_garment") for item in hero_snapshot]
    hero_sources = [item for item in hero_sources if reference_image_input_available(item)]
    # 正面镜头用封面图 + 细节图，背面镜头用封面图 + 背面图；每张独占一个输入位。
    hero_pick = (0, 2) if shot_index in {1, 2} else (0, 1)
    hero_selected = [hero_sources[index] for index in hero_pick if index < len(hero_sources)]
    if not hero_selected and hero_sources:
        hero_selected = hero_sources[:IMAGE2_DIRECT_HERO_SOURCE_LIMIT]
    for source in hero_selected[:IMAGE2_DIRECT_HERO_SOURCE_LIMIT]:
        hero_input = reference_sheet_image_input(
            "hero_garment",
            [source],
            filename="hero-garment.jpg",
            max_items=1,
        )
        if hero_input:
            inputs.append(hero_input)

    styling_sources: list[dict[str, Any]] = []
    for garment in loads(row_value(styling, "selected_material_snapshot_json"), []):
        if not isinstance(garment, dict):
            continue
        for image in garment.get("images", []):
            if len(styling_sources) >= IMAGE2_DIRECT_STYLING_SOURCE_LIMIT:
                break
            if isinstance(image, dict):
                source = image_input_from_asset(image.get("asset"), "styling_garment")
                source["materialCode"] = garment.get("code") or garment.get("sku")
                if reference_image_input_available(source):
                    styling_sources.append(source)
        if len(styling_sources) >= IMAGE2_DIRECT_STYLING_SOURCE_LIMIT:
            break
    styling_sheet = reference_sheet_image_input(
        "styling_garment",
        styling_sources,
        filename="styling-garments-sheet.jpg",
        max_items=1,
    )
    if styling_sheet:
        inputs.append(styling_sheet)

    inputs = [item for item in inputs if reference_image_input_available(item)]
    if len(inputs) > IMAGE2_MAX_REFERENCE_IMAGES:
        raise RuntimeError(f"Image-2 参考图组装超过 {IMAGE2_MAX_REFERENCE_IMAGES} 张：{len(inputs)}")
    if not inputs or inputs[0].get("role") != "shot_background":
        raise RuntimeError("编辑模式第一张参考图必须是当前镜头背景")
    return inputs


def reference_image_input_available(item: dict[str, Any]) -> bool:
    return bool(item.get("objectKey") or item.get("url") or item.get("providerUrl") or item.get("collageItems"))


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
    model_inputs = resolve_model_snapshot_image_inputs(model_snapshot, limit=IMAGE2_MODEL_SHEET_SOURCE_LIMIT)
    model_sheet = reference_sheet_image_input(
        "model_identity",
        model_inputs,
        filename="model-identity-sheet.jpg",
        max_items=IMAGE2_MODEL_SHEET_SOURCE_LIMIT,
    )
    if model_sheet:
        inputs.append(model_sheet)

    hero_snapshot = loads(input_version["image_snapshot_json"], []) if input_version else []
    hero_inputs = [
        image_input_from_asset(item.get("asset"), "hero_garment")
        for item in hero_snapshot[:IMAGE2_HERO_SHEET_SOURCE_LIMIT]
        if isinstance(item, dict)
    ]
    for index, chunk in enumerate(chunked(hero_inputs, IMAGE2_HERO_SHEET_CHUNK_SIZE), start=1):
        hero_sheet = reference_sheet_image_input(
            "hero_garment",
            chunk,
            filename=f"hero-garment-sheet-{index}.jpg",
            max_items=IMAGE2_HERO_SHEET_CHUNK_SIZE,
        )
        if hero_sheet:
            inputs.append(hero_sheet)

    selected_materials = loads(styling["selected_material_snapshot_json"], []) if styling else []
    for garment in selected_materials[:IMAGE2_STYLING_GARMENT_LIMIT]:
        images = garment.get("images", []) if isinstance(garment, dict) else []
        garment_inputs = [
            image_input_from_asset(image.get("asset"), "styling_garment")
            for image in images[:IMAGE2_STYLING_SHEET_SOURCE_LIMIT]
            if isinstance(image, dict)
        ]
        garment_code = str(garment.get("code") or garment.get("sku") or "styling-garment") if isinstance(garment, dict) else "styling-garment"
        styling_sheet = reference_sheet_image_input(
            "styling_garment",
            garment_inputs,
            filename=f"{safe_reference_filename(garment_code)}-sheet.jpg",
            max_items=IMAGE2_STYLING_SHEET_SOURCE_LIMIT,
        )
        if styling_sheet:
            inputs.append(styling_sheet)

    if include_preview and preview_asset_id:
        asset = get_media_asset_by_id(preview_asset_id)
        if asset:
            inputs.append(preview_grid_cell_image_input(asset, preview_grid_cell))
    elif environment_asset_id:
        env_asset = get_media_asset_by_id(environment_asset_id)
        if env_asset:
            inputs.append(image_input_from_asset(env_asset, "shoot_environment"))
    if len(inputs) > IMAGE2_MAX_REFERENCE_IMAGES:
        raise RuntimeError(f"Image-2 参考图组装超过 {IMAGE2_MAX_REFERENCE_IMAGES} 张：{len(inputs)}")
    return inputs


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index:index + size] for index in range(0, len(items), size) if items[index:index + size]]


def reference_sheet_image_input(
    role: str,
    source_inputs: list[dict[str, Any]],
    filename: str,
    max_items: int,
    board_kind: str | None = None,
) -> dict[str, Any] | None:
    sources = [
        item
        for item in source_inputs[:max_items]
        if isinstance(item, dict) and (item.get("objectKey") or item.get("url") or item.get("providerUrl"))
    ]
    if not sources:
        return None
    if len(sources) == 1:
        return {
            **sources[0],
            "role": role,
            "referenceBoardKind": board_kind,
        }
    return {
        "role": role,
        "filename": filename,
        "referenceBoardKind": board_kind,
        "collageItems": [{**item, "role": role} for item in sources],
        "collageRule": {
            "kind": "runtime_reference_sheet",
            "maxSourceImages": max_items,
            "sourceCount": len(sources),
            "notPersisted": True,
        },
    }


def safe_reference_filename(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return slug or "reference"


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
    try:
        with get_db() as db:
            rows = db.execute("SELECT key, content FROM prompt_configs").fetchall()
            timestamp = now_iso()
            for row in rows:
                content = str(row["content"] or "")
                repaired = (
                    content
                    .replace("{{runtime.shot_accessories}}{{runtime.shot_styling}}", "{{runtime.shot_styling}}")
                    .replace("{{runtime.shot_styling}}{{runtime.shot_accessories}}", "{{runtime.shot_styling}}")
                    .replace("{{runtime.shot_accessories}}", "")
                    .replace("{{runtime.styling_outfit}}", "")
                    .replace("{{runtime.resolved_accessories}}", "")
                )
                if repaired != content:
                    db.execute(
                        "UPDATE prompt_configs SET content = ?, updated_at = ? WHERE key = ?",
                        (repaired, timestamp, row["key"]),
                    )
    except sqlite3.OperationalError:
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
        "enabled": bool(row_value(row, "enabled", 0)),
        "suggestedContent": (Path(__file__).resolve().parents[1] / "prompt_templates" / "final_prompt_optimizer.txt").read_text(encoding="utf-8") if str(row["key"]) == OPTIMIZER_KEY else None,
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
    frozen_config = rows is not None
    rows = rows if rows is not None else get_prompt_config_rows()
    runtime = runtime or {}
    content_overrides = content_overrides or {}

    def content_for(name: str) -> str | None:
        if name in content_overrides:
            return content_overrides[name]
        content = prompt_config_content(name, rows)
        # 历史冻结正文只在读取时升级占位符，不回写历史快照。
        return upgrade_prompt_runtime_placeholders(content) if frozen_config and content is not None else content

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
        "pose_context": dumps({"code": "POSE-A", "images": [{"imageId": 101, "order": 1}]}),
        "pose_phase": "shortlist", "pose_candidates": "[]", "pose_shortlist": "[]",
        "pose_user_instruction": "动作自然，避免遮挡主推款细节。",
        "pose_shots": "[]", "pose_image_manifest": "[]", "shot_pose": "",
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
        "final_image_user_instruction": "成片整体更干净，减少配饰存在感。",
        # 固定品类分镜 Prompt 的少量动态事实（自然语言，不传中间剧本 JSON）
        "category_family": "bottom",
        # 当前成片各分段（成片用；无表头）
        "shot_index": "1",
        "shot_styling": "选中搭配服装：LOOK-A；穿法：单穿吊带裙；生成类配饰：极简金色小耳圈",
        "is_shot_1": "1",
        "is_shot_2": "",
        "is_shot_3": "",
        "is_shot_4": "",
        "is_shot_5": "",
        # 参考图各职能图号区间（只出会变的图号）
        "ref_shot_background": "图1",
        "ref_shot_pose": "图2",
        "ref_model_identity": "图3",
        "ref_hero_garment": "图4-5",
        "ref_styling_garment": "图6",
    }


def available_prompt_placeholders() -> list[dict[str, str]]:
    return [
        *[{"name": f"runtime.{name}", "scope": "runtime", "description": label} for name, label in (
            ("pose_context", "姿势识别条目与图片清单"), ("pose_phase", "姿势选搭阶段（固定 shortlist，初筛即定选）"),
            ("pose_candidates", "可选姿势标签和图片标识"), ("pose_shortlist", "旧配置兼容占位，固定为空列表"),
            ("pose_user_instruction", "本次姿势要求"), ("pose_shots", "从冻结成片 Prompt 渲染的五镜头要求"),
            ("pose_image_manifest", "初筛实际传入的商品和背景图片顺序与角色"), ("shot_pose", "当前镜头冻结的姿势动作说明"),
        )],
        {"name": "segment.brand_info", "scope": "segment", "description": "品牌信息分段 Prompt"},
        {"name": "runtime.garment_image_files", "scope": "runtime", "description": "搭配服装图片挑选/排序使用的文件标识与候选顺序"},
        {"name": "runtime.styling_garment", "scope": "runtime", "description": "AI 打标使用的当前搭配服装 code 和图片输入摘要"},
        {"name": "runtime.hero_image_files", "scope": "runtime", "description": "主推款图片挑选/排序使用的文件标识与候选顺序"},
        {"name": "runtime.candidate_retrieval", "scope": "runtime", "description": "搭配服装库 READY 服饰的全量 AI 标签事实卡"},
        {"name": "runtime.user_requirement", "scope": "runtime", "description": "任务级服装信息，来自 AI 摄影棚导入主推款时维护的服装信息"},
        {"name": "runtime.ai_styling_user_instruction", "scope": "runtime", "description": "AI 智能选搭本次执行的用户介入要求"},
        {"name": "runtime.final_image_user_instruction", "scope": "runtime", "description": "最终成片或单张重生本次执行的用户介入要求"},
        {"name": "runtime.styling_result", "scope": "runtime", "description": "AI 选搭导演上下文，使用搭配服装 code 标识"},
        {"name": "runtime.category_family", "scope": "runtime", "description": "代码判定的拍摄图位族：top / bottom / one_piece"},
        # 成片：当前镜头各分段
        {"name": "runtime.shot_index", "scope": "runtime", "description": "当前成片固定镜头号（1-5）"},
        {"name": "runtime.shot_styling", "scope": "runtime", "description": "配饰与造型正文，合并选中搭配、穿法和低存在感配饰边界，用中文分号连接为单行"},
        {"name": "runtime.is_shot_1", "scope": "runtime", "description": "当前成片为镜头 1 时有值，用于条件段"},
        {"name": "runtime.is_shot_2", "scope": "runtime", "description": "当前成片为镜头 2 时有值，用于条件段"},
        {"name": "runtime.is_shot_3", "scope": "runtime", "description": "当前成片为镜头 3 时有值，用于条件段"},
        {"name": "runtime.is_shot_4", "scope": "runtime", "description": "当前成片为镜头 4 时有值，用于条件段"},
        {"name": "runtime.is_shot_5", "scope": "runtime", "description": "当前成片为镜头 5 时有值，用于条件段"},
        # 参考图各职能图号区间：只出会变的图号，职能描述在模板正文托管
        {"name": "runtime.ref_model_identity", "scope": "runtime", "description": "模特身份参考板图号，直出链路通常为「图3」；无则空"},
        {"name": "runtime.ref_hero_garment", "scope": "runtime", "description": "主推款参考图图号，通常为「图4-5」；按实际输入编号，无则空"},
        {"name": "runtime.ref_styling_garment", "scope": "runtime", "description": "搭配款参考图图号，通常为「图6」；按实际输入编号，无搭配款则空"},
        {"name": "runtime.ref_shot_background", "scope": "runtime", "description": "统一背景参考图号，固定为图1；历史结果按原快照职责使用"},
        {"name": "runtime.ref_shot_pose", "scope": "runtime", "description": "当前镜头姿势参考图图号，固定为图2"},
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


def generation_retry_backoff_seconds(retry_number: int, base_seconds: float | None = None) -> float:
    """30/60/120/240 秒封顶的持久化 auto-run 退避。retry_number 从 1 开始。"""
    base = max(0.0, float(settings.generation_retry_backoff_base_seconds if base_seconds is None else base_seconds))
    cap = max(base, float(settings.generation_retry_backoff_cap_seconds))
    return min(base * (2 ** max(0, retry_number - 1)), cap)


def is_auto_run_retryable_error(error_code: str | None) -> bool:
    """跨 attempt 只接管没有内层重试的导入失败；其它阶段各自拥有唯一的 10 次预算。"""
    return error_code == ERROR_UPLOAD_FAILED


def schedule_auto_run_retry(task_id: int, error_codes: list[str | None], *, process_interrupted: bool = False) -> bool:
    """把一键成片任务放回现有 SQLite poller；达到 10 次或没有可重试错误时返回 False。"""
    retryable_codes = [
        str(code)
        for code in error_codes
        if process_interrupted or is_auto_run_retryable_error(code)
    ]
    if not retryable_codes:
        return False
    with get_db() as db:
        task = db.execute("SELECT * FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task or task["status"] in {"CANCELLED", "ABANDONED", "COMPLETED"}:
            return False
        retry_count = int(row_value(task, "auto_run_retry_count", 0) or 0)
        if retry_count >= settings.generation_auto_run_retry_limit:
            return False
        if task["status"] == "FAILED":
            resume_failed_generation_task(db, task_id, now_iso())
        retry_number = retry_count + 1
        delay = generation_retry_backoff_seconds(retry_number)
        retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        timestamp = now_iso()
        db.execute(
            """
            UPDATE generation_tasks
            SET auto_run_target = ?, auto_run_retry_at = ?, auto_run_retry_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (AUTO_RUN_TARGET_FINAL, retry_at, retry_number, timestamp, task_id),
        )
        log_event(
            db,
            "generation_task",
            task_id,
            "auto_run_retry_scheduled",
            {"retryNumber": retry_number, "retryAt": retry_at, "errorCodes": sorted(set(retryable_codes))},
        )
    return True


def task_auto_run_retry_waiting(task: Any) -> bool:
    retry_at = row_value(task, "auto_run_retry_at")
    return bool(retry_at and str(retry_at) > now_iso())


def consume_due_auto_run_retry(task_id: int) -> bool:
    """原子消费已到期的重试时间；保留累计次数，供本轮失败继续递增。"""
    timestamp = now_iso()
    with get_db() as db:
        cursor = db.execute(
            """
            UPDATE generation_tasks
            SET auto_run_retry_at = NULL, updated_at = ?
            WHERE id = ? AND auto_run_target = ?
              AND auto_run_retry_at IS NOT NULL AND auto_run_retry_at <= ?
            """,
            (timestamp, task_id, AUTO_RUN_TARGET_FINAL, timestamp),
        )
    return bool(cursor.rowcount)


def auto_run_active_task_ids() -> set[int]:
    """当前占用 auto-run 并发额度的任务：状态运行中，或后台正在推进其 auto-run 协程。"""
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id FROM generation_tasks
            WHERE deleted_at IS NULL
              AND auto_run_target = ?
              AND status IN ('POSE_SELECTION_RUNNING', 'STYLING_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING')
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
    slots = settings.toapis_max_concurrency - len(active_ids)
    if slots <= 0:
        return
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id FROM generation_tasks
            WHERE deleted_at IS NULL
              AND auto_run_target = ?
              AND status NOT IN ('POSE_SELECTION_RUNNING', 'STYLING_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING', 'ABANDONED')
              AND (auto_run_retry_at IS NULL OR auto_run_retry_at <= ?)
            ORDER BY COALESCE(auto_run_requested_at, updated_at), id
            """,
            (AUTO_RUN_TARGET_FINAL, now_iso()),
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
              AND status NOT IN ('POSE_SELECTION_RUNNING', 'STYLING_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING', 'ABANDONED')
              AND (auto_run_retry_at IS NULL OR auto_run_retry_at <= ?)
              AND id != ?
              AND (
                COALESCE(auto_run_requested_at, updated_at) < COALESCE(?, '')
                OR (COALESCE(auto_run_requested_at, updated_at) = COALESCE(?, '') AND id < ?)
              )
            """,
            (AUTO_RUN_TARGET_FINAL, now_iso(), task_id, requested_at, requested_at, task_id),
        ).fetchone()
    return int(row["ahead"]) + 1 if row else None


def maybe_schedule_auto_run_for_task(task_id: int) -> None:
    pump_auto_run_queue()


def clear_task_auto_run(task_id: int) -> None:
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_tasks
            SET auto_run_target = NULL, auto_run_requested_at = NULL,
                auto_run_retry_at = NULL, auto_run_retry_count = 0, updated_at = ?
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
            if task_auto_run_retry_waiting(task):
                return
            if row_value(task, "auto_run_retry_at"):
                consume_due_auto_run_retry(task_id)
                task = get_generation_task_row(task_id)
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
            if step == "pose":
                await run_select_generation_poses(task_id, trigger="auto_run")
                continue
            if step == "final":
                await run_generate_final_images(task_id, trigger="auto_run", continue_existing=True)
                return
            return
    except HTTPException:
        task = get_generation_task_row(task_id)
        if task_auto_run_retry_waiting(task):
            return
        clear_task_auto_run(task_id)
        raise
    except Exception:
        task = get_generation_task_row(task_id)
        if task_auto_run_retry_waiting(task):
            return
        clear_task_auto_run(task_id)
        raise


def next_auto_final_step(task: Any) -> str:
    if task["status"] in {"FAILED", "CANCELLED"}:
        operations = get_task_ai_operations(int(task["id"]))
        if operations and operations[0]["ownerType"] == "generation_pose_selection":
            return "pose"
    if task["status"] == "DRAFT":
        return "ready"
    if task["status"] == "READY_FOR_STYLING":
        return "styling"
    if task["status"] == "STYLING_READY":
        return "pose" if row_value(task, "pose_workflow_enabled", 0) else "final"
    if task["status"] == "POSE_SELECTION_READY":
        return "final"
    if task["status"] in {"PREVIEW_READY", "FINAL_PARTIAL"}:
        return "final"
    if task["status"] == "COMPLETED":
        return "complete"
    if not task["active_styling_id"]:
        return "styling"
    if row_value(task, "pose_workflow_enabled", 0) and not row_value(task, "active_pose_selection_id"):
        return "pose"
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
    recover_interrupted_pose_jobs()
    timestamp = now_iso()
    with get_db() as db:
        styling_rows = db.execute(
            """
            SELECT gt.id AS task_id,
                   (SELECT ga.id FROM generation_attempts ga
                    WHERE ga.owner_type IN ('generation_styling', 'generation_pose_selection') AND ga.owner_id = gt.id AND ga.status = 'RUNNING'
                    ORDER BY ga.id DESC LIMIT 1) AS attempt_id
            FROM generation_tasks gt
            WHERE gt.deleted_at IS NULL AND gt.status = 'STYLING_RUNNING' AND gt.auto_run_target = ?
            ORDER BY gt.updated_at, gt.id
            LIMIT 3
            """,
            (AUTO_RUN_TARGET_FINAL,),
        ).fetchall()
        preview_rows = db.execute(
            """
            SELECT gp.*, gt.auto_run_target AS task_auto_run_target
            FROM generation_previews gp
            JOIN generation_tasks gt ON gt.id = gp.task_id
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
            SELECT DISTINCT grs.id, grs.task_id, gt.auto_run_target AS task_auto_run_target
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
            SELECT grs.id, grs.task_id, gt.auto_run_target AS task_auto_run_target
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

    for row in styling_rows:
        task_id = int(row["task_id"])
        if auto_run_job_key(task_id) in active_background_generation_jobs:
            continue
        attempt_id = int(row["attempt_id"]) if row["attempt_id"] else None
        message = "AI 智能选搭执行进程已中断，系统将从选搭阶段自动恢复。"
        fail_stale_attempt(attempt_id, message, ERROR_PROVIDER_TIMEOUT)
        with get_db() as db:
            db.execute("UPDATE generation_tasks SET status = 'FAILED', updated_at = ? WHERE id = ?", (now_iso(), task_id))
        if not schedule_auto_run_retry(task_id, [ERROR_PROVIDER_TIMEOUT], process_interrupted=True):
            clear_task_auto_run(task_id)

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
                    "UPDATE generation_tasks SET status = 'FAILED', updated_at = ? WHERE id = ?",
                    (now_iso(), int(row["task_id"])),
                )
            if row["task_auto_run_target"] == AUTO_RUN_TARGET_FINAL and schedule_auto_run_retry(int(row["task_id"]), [error_code]):
                continue
            clear_task_auto_run(int(row["task_id"]))
            continue
        else:
            attempt_id = create_attempt("generation_preview", int(row["id"]), "image-2")
        lease_generation_preview(int(row["id"]), attempt_id)
        trigger = "auto_run" if row["task_auto_run_target"] == AUTO_RUN_TARGET_FINAL else "manual_step"
        schedule_background_generation(
            run_preview_generation(int(row["task_id"]), int(row["styling_id"]), int(row["id"]), attempt_id, trigger=trigger),
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
                trigger = "auto_run" if row["task_auto_run_target"] == AUTO_RUN_TARGET_FINAL else "manual_step"
                schedule_background_generation(
                    run_final_generation(
                        int(row["task_id"]), int(row["id"]), image_ids,
                        resume_attempts_by_image_id=resume_attempts_by_image_id,
                        trigger=trigger,
                    ),
                    job_key=key,
                )
            else:
                trigger = "auto_run" if row["task_auto_run_target"] == AUTO_RUN_TARGET_FINAL else "manual_step"
                schedule_background_generation(
                    run_final_generation(int(row["task_id"]), int(row["id"]), [], trigger=trigger),
                    job_key=key,
                )

    for row in terminal_result_sets:
        key = final_set_job_key(int(row["id"]))
        if key in active_background_generation_jobs:
            continue
        trigger = "auto_run" if row["task_auto_run_target"] == AUTO_RUN_TARGET_FINAL else "manual_step"
        schedule_background_generation(
            run_final_generation(int(row["task_id"]), int(row["id"]), [], trigger=trigger),
            job_key=key,
        )
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


async def generate_image_asset(
    task_package: dict[str, Any],
    output_key: str,
    attempt_id: int | None = None,
) -> tuple[int, dict[str, Any]]:
    return await _generate_image_asset_unlocked(task_package, output_key, attempt_id)


async def _generate_image_asset_unlocked(
    task_package: dict[str, Any],
    output_key: str,
    attempt_id: int | None = None,
) -> tuple[int, dict[str, Any]]:
    """完整生成一张图：导入参考图 → 提交 → 轮询 → 成片回传。

    健壮性契约（成功 > 慢 > 失败）：

    - 状态查询瞬时失败按配置退避重试；超过连续失败预算才判定本张图失败。
    - provider 成功后进入回传阶段；回传失败在回传预算内自动退避重试，
      耗尽后以 DELIVERY_FAILED 落败——providerRequestId 保留，可零成本重新回传。
    - 阶段进度（导入张数、排队、生成、回传重试次数）持久化在 attempt 上，
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

    def ensure_active() -> None:
        if generation_attempt_cancelled(attempt_id):
            raise RuntimeError("图片生成已取消，停止后续提交")

    def report_upload(completed: int, total: int) -> None:
        ensure_active()
        progress.update(PHASE_UPLOAD, completed=completed, total=total)
        if completed >= total:
            progress.begin(PHASE_SUBMIT, generationRetry=generation_retries)

    while True:
        if payload is None:
            ensure_active()
            client_business_id = f"ruyi_img_a{attempt_id or 0}_r{generation_retries}"
            submit_task_package = {**submit_task_package, "clientBusinessId": client_business_id}
            progress.begin(PHASE_PREPARE)
            progress.begin(PHASE_UPLOAD, completed=0, total=image_input_total, generationRetry=generation_retries)
            try:
                payload = await asyncio.to_thread(
                    run_image_provider_submit, submit_task_package, output_key, report_upload,
                    lambda: generation_attempt_cancelled(attempt_id),
                )
            except ImageProviderError as exc:
                if classify_attempt_error(exc) != ERROR_SUBMISSION_UNKNOWN:
                    raise
                unknown_snapshot = getattr(exc, "provider_response", None)
                if not isinstance(unknown_snapshot, dict):
                    unknown_snapshot = {}
                uploaded_inputs = unknown_snapshot.get("imageInputs")
                if isinstance(uploaded_inputs, list) and uploaded_inputs:
                    submit_task_package = {**submit_task_package, "imageInputs": uploaded_inputs}
                retry_allowed = unknown_submission_retries < settings.generation_unknown_submission_retry_limit
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
                retry_delay = generation_retry_backoff_seconds(
                    unknown_submission_retries,
                    settings.generation_unknown_submission_retry_delay_seconds,
                )
                snapshot.update(
                    {
                        "status": "SUBMISSION_RETRY_WAIT",
                        "unknownSubmissionRetries": unknown_submission_retries,
                    }
                )
                progress.begin(
                    PHASE_SUBMIT,
                    unknownSubmissionRetry=unknown_submission_retries,
                    retryDelaySeconds=retry_delay,
                )
                prepare_attempt_provider_resubmission(attempt_id, snapshot)
                await asyncio.sleep(retry_delay)
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
            and generation_retries < settings.generation_submit_retry_limit
        ):
            ensure_active()
            uploaded_inputs = payload.get("imageInputs")
            if isinstance(uploaded_inputs, list) and uploaded_inputs:
                submit_task_package = {**task_package, "imageInputs": uploaded_inputs}
            generation_retries += 1
            retry_delay = generation_retry_backoff_seconds(generation_retries)
            progress.begin(
                PHASE_SUBMIT,
                generationRetry=generation_retries,
                retryDelaySeconds=retry_delay,
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
            await asyncio.sleep(retry_delay)
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
        poll_interval = max(0.1, float(settings.generation_poll_interval_seconds))
        await asyncio.sleep(
            generation_retry_backoff_seconds(consecutive_errors)
            if consecutive_errors
            else poll_interval
        )
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
            if consecutive_errors > settings.generation_status_retry_limit:
                raise ImageProviderError(
                    f"provider 状态查询连续失败超过重试上限（{settings.generation_status_retry_limit}）：{exc}",
                    payload,
                    error_code=status_error_code,
                ) from exc
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
            next_retry = retries + 1
            if next_retry > settings.generation_delivery_retry_limit:
                break
            wait = generation_retry_backoff_seconds(next_retry)
            if time.time() + wait > deadline:
                break
            retries = next_retry
            progress.update(PHASE_DELIVER, retries=retries, lastError=str(exc)[:500])
            await asyncio.sleep(wait)
    raise ImageProviderError(
        f"成片生成已在 provider 侧完成，但回传失败（已重试 {retries} 次），"
        f"providerRequestId={payload.get('providerRequestId')}: {last_error}",
        payload,
        error_code=ERROR_DELIVERY_FAILED,
    )


def run_image_provider_submit(task_package: dict[str, Any], output_key: str, on_progress: Any = None,
                              should_cancel: Any = None) -> dict[str, Any]:
    return asyncio.run(get_image_provider().submit(task_package, output_key, on_progress, should_cancel))


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
) -> int:
    return await _generate_single_final_image_unlocked(
        task_id,
        result_set_id,
        image_id,
        attempt_id=attempt_id,
        trigger=trigger,
        balance_stop=balance_stop,
    )


async def _generate_single_final_image_unlocked(
    task_id: int,
    result_set_id: int,
    image_id: int,
    attempt_id: int | None = None,
    trigger: str = "manual_step",
    balance_stop: asyncio.Event | None = None,
) -> int:
    # Claim the current attempt atomically. A delayed worker must not replace a
    # newer retry's active_attempt_id before the publication guard can inspect it.
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = get_final_image_generation_state(db, image_id)
        image = db.execute('SELECT * FROM generation_result_images_new WHERE id=?', (image_id,)).fetchone()
        if (final_image_generation_cancelled(row, result_set_id) or not image
                or image['status'] not in {'QUEUED', 'RUNNING'}
                or (image['active_attempt_id'] is not None and image['active_attempt_id'] != attempt_id)):
            return 0
        if attempt_id is None:
            attempt_id = create_attempt('generation_result_image', image_id, 'image-2', db=db)
        attempt = db.execute('SELECT * FROM generation_attempts WHERE id=?', (attempt_id,)).fetchone()
        if (not attempt or attempt['status'] in {'CANCELLED', 'SUCCEEDED'}
                or attempt['owner_type'] != 'generation_result_image' or attempt['owner_id'] != image_id):
            return 0
        db.execute("""UPDATE generation_result_images_new SET status='RUNNING', active_attempt_id=?,
            locked_by=?, locked_until=?, run_attempts=COALESCE(run_attempts,0)+1, updated_at=? WHERE id=?""",
            (attempt_id, f'worker-{uuid4()}', job_lease_until(), now_iso(), image_id))
        if balance_stop is not None and balance_stop.is_set():
            finish_attempt(attempt_id, 'FAILED', error_message='ToAPIs 余额不足，已跳过本张成片提交（同批已检测到余额不足）。',
                           error_code=ERROR_INSUFFICIENT_BALANCE, db=db)
            db.execute("""UPDATE generation_result_images_new SET status='FAILED', locked_by=NULL,
                locked_until=NULL, updated_at=? WHERE id=?""", (now_iso(), image_id))
            return 0
    try:
        preparation = read_final_preparation(result_set_id)
        with get_db() as db:
            frozen_image = db.execute("SELECT execution_snapshot_json FROM generation_result_images_new WHERE id=?", (image_id,)).fetchone()
        frozen_package = json_object(frozen_image["execution_snapshot_json"]) if frozen_image else {}
        task_package = frozen_package if executable_package(frozen_package) else None
        if attempt_has_provider_request_id(attempt_id):
            with get_db() as db:
                saved = db.execute("SELECT request_snapshot_json FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
            previous = loads(saved["request_snapshot_json"], {}) if saved else {}
            if previous.get("prompt") and previous.get("imageInputs"):
                task_package = previous
        if task_package is None:
            task_package = preparation.get("packages", {}).get(str(image_id))
        if task_package is None:
            if preparation.get("mode") == "optimized":
                raise RuntimeError("拍摄要求尚未准备完成，不能提交成片生成")
            task_package = build_new_final_task_package(task_id, image_id)
        task_package = dict(task_package)
        if not attempt_has_provider_request_id(attempt_id):
            task_package = adapt_replay_references(task_package)
        trigger = preserved_attempt_invocation_mode(attempt_id) or trigger
        task_package["invocation"] = {"mode": trigger, "target": "final_image"}
        if not attempt_has_provider_request_id(attempt_id):
            update_attempt_request_snapshot(attempt_id, task_package)
        output_key = media_original_key_for_owner("generation_attempt", attempt_id, "final.png")
        asset_id, provider_response = await generate_image_asset(
            task_package,
            output_key,
            attempt_id=attempt_id,
        )
    except Exception as exc:
        provider_response = getattr(exc, "provider_response", None)
        with get_db() as db:
            current = db.execute(
                final_image_generation_state_sql(),
                (image_id,),
            ).fetchone()
            if final_image_generation_cancelled(current, result_set_id) or generation_attempt_cancelled(attempt_id):
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
                WHERE id = ? AND status IN ('QUEUED', 'RUNNING') AND active_attempt_id = ?
                """,
                (attempt_id, now_iso(), image_id, attempt_id),
            )
        raise
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        current = get_final_image_generation_state(db, image_id)
        active = db.execute('SELECT active_attempt_id, status FROM generation_result_images_new WHERE id=?', (image_id,)).fetchone()
        attempt = db.execute('SELECT status FROM generation_attempts WHERE id=?', (attempt_id,)).fetchone()
        if (final_image_generation_cancelled(current, result_set_id) or not active
                or active['active_attempt_id'] != attempt_id or active['status'] not in {'QUEUED', 'RUNNING'}
                or not attempt or attempt['status'] == 'CANCELLED'):
            finish_attempt(attempt_id, 'CANCELLED', response_snapshot=provider_response, output_asset_id=asset_id, db=db)
            return asset_id
        capture_published_image(db, image_id)
        finish_attempt(attempt_id, 'SUCCEEDED', response_snapshot=provider_response, output_asset_id=asset_id, db=db)
        db.execute("""UPDATE generation_result_images_new SET status='SUCCEEDED', current_asset_id=?,
            active_attempt_id=?, locked_by=NULL, locked_until=NULL, updated_at=? WHERE id=?""",
            (asset_id, attempt_id, now_iso(), image_id))
        capture_published_image(db, image_id)
    persist_attempt_provider_request_id(attempt_id, provider_response)
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
    if not include_detail:
        return generation_task_summaries([row])[0]
    item = {
        "detailRevision": row_value(row, "detail_revision", 0),
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "userRequirement": row_value(row, "user_requirement_text", ""),
        "stageUserInputs": {
            "aiStyling": row_value(row, "ai_styling_user_instruction_text", ""),
            "poseSelection": row_value(row, "pose_selection_user_instruction", ""),
            "storyboardPreview": row_value(row, "storyboard_preview_user_instruction_text", ""),
            "finalImage": row_value(row, "final_image_user_instruction_text", ""),
        },
        "currentInputVersionId": row["current_input_version_id"],
        "activeStylingId": row["active_styling_id"],
        "poseWorkflowEnabled": bool(row_value(row, "pose_workflow_enabled", 0)),
        "activePoseSelectionId": row_value(row, "active_pose_selection_id"),
        "activePreviewId": row["active_preview_id"],
        "activeStoryboardPreviewId": row["active_storyboard_preview_id"] if "active_storyboard_preview_id" in row.keys() else row["active_preview_id"],
        "activeResultSetId": row["active_result_set_id"],
        "batchId": row_value(row, "batch_id"),
        "autoRunTarget": row_value(row, "auto_run_target"),
        "autoRunRequestedAt": row_value(row, "auto_run_requested_at"),
        "autoRunRetryAt": row_value(row, "auto_run_retry_at"),
        "autoRunRetryCount": int(row_value(row, "auto_run_retry_count", 0) or 0),
        "autoRunRetryLimit": settings.generation_auto_run_retry_limit,
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
    item["poseSelection"] = get_generation_pose_selection(row_value(row, "active_pose_selection_id"))
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


def generation_list_context(rows: list[Any]) -> tuple[dict[int, Any], dict[str, Any]]:
    summaries = {item["id"]: item for item in generation_task_summaries(rows)}
    with get_db() as db:
        batches = db.execute("SELECT id, name, updated_at FROM generation_batches").fetchall()
    return summaries, {row["id"]: {"name": row["name"], "updatedAt": row["updated_at"]} for row in batches}


def generation_task_summaries(rows: list[Any]) -> list[dict[str, Any]]:
    if not rows:
        return []
    ids = [int(row["id"]) for row in rows]
    with get_db() as db:
        covers = db.execute("""
            SELECT gt.id AS task_id, ma.*,
              (SELECT COUNT(*) FROM generation_result_images_new ri WHERE ri.result_set_id = gt.active_result_set_id AND ri.status = 'SUCCEEDED') AS completed
            FROM generation_tasks gt LEFT JOIN media_assets ma ON ma.id = (
              SELECT media_asset_id FROM generation_task_images WHERE task_id = gt.id AND deleted_at IS NULL ORDER BY sort_order, id LIMIT 1
            ) WHERE gt.id IN (SELECT value FROM json_each(?))
        """, (dumps(ids),)).fetchall()
        queue = db.execute("SELECT id FROM generation_tasks WHERE auto_run_target IS NOT NULL AND deleted_at IS NULL AND archived_at IS NULL ORDER BY auto_run_requested_at, id").fetchall()
    by_id = {row["task_id"]: row for row in covers}
    active = auto_run_active_task_ids()
    positions = {row["id"]: i + 1 for i, row in enumerate(row for row in queue if row["id"] not in active)}
    result = []
    for row in rows:
        cover = by_id[row["id"]]
        queued = bool(row_value(row, "auto_run_target") and row["status"] not in RUNNING_GENERATION_TASK_STATUSES and row["status"] not in AUTO_RUN_TERMINAL_TASK_STATUSES and row["id"] not in active)
        result.append({
            "id": row["id"], "name": row["name"], "status": row["status"],
            "poseWorkflowEnabled": bool(row_value(row, "pose_workflow_enabled", 0)),
            "activeStylingId": row_value(row, "active_styling_id"),
            "activePoseSelectionId": row_value(row, "active_pose_selection_id"),
            "detailRevision": row_value(row, "detail_revision", 0),
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "batchId": row_value(row, "batch_id"), "archivedAt": row_value(row, "archived_at"), "archiveReason": row_value(row, "archive_reason"),
            "autoRunTarget": row_value(row, "auto_run_target"), "autoRunQueued": queued,
            "autoRunQueuePosition": positions.get(row["id"]) if queued else None,
            "autoRunRetryAt": row_value(row, "auto_run_retry_at"),
            "autoRunRetryCount": row_value(row, "auto_run_retry_count", 0), "autoRunRetryLimit": settings.generation_auto_run_retry_limit,
            "progress": {"completed": cover["completed"], "total": 5},
            "summary": {"cover": {"id": cover["id"], "filename": cover["original_filename"],
                "thumbUrl": stable_media_url(cover["object_key"], "thumb") if cover["thumb_key"] else None,
                "thumbStatus": "AVAILABLE" if cover["thumb_key"] else "MISSING"} if cover["id"] else None},
        })
    return result


def get_generation_task_summary(task_id: int) -> dict[str, Any]:
    return generation_task_summaries([get_generation_task_row(task_id)])[0]["summary"]


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
    selected_material_ids = [int(item) for item in loads(row["selected_material_ids_json"], [])]
    selected_material_snapshots = [
        item for item in loads(row["selected_material_snapshot_json"], [])
        if isinstance(item, dict)
    ]
    snapshot_material_ids = [
        int(snapshot["id"])
        for snapshot in selected_material_snapshots
        if snapshot.get("id") is not None
    ]
    referenced_material_ids = selected_material_ids or snapshot_material_ids
    live_material_ids: set[int] = set()
    if referenced_material_ids:
        placeholders = ", ".join("?" for _ in referenced_material_ids)
        with get_db() as db:
            live_material_ids = {
                int(material["id"])
                for material in db.execute(
                    f"SELECT id FROM styling_garments WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                    referenced_material_ids,
                ).fetchall()
            }
    snapshots_by_id = {
        int(snapshot["id"]): snapshot
        for snapshot in selected_material_snapshots
        if snapshot.get("id") is not None
    }
    deleted_material_codes = {
        str(snapshots_by_id.get(material_id, {}).get("code") or "")
        for material_id in referenced_material_ids
        if material_id not in live_material_ids
    }
    selected_materials: list[dict[str, Any]] = selected_material_snapshots if not referenced_material_ids else []
    for material_id in referenced_material_ids:
        snapshot = snapshots_by_id.get(material_id, {})
        if material_id in live_material_ids:
            selected_materials.append(snapshot or {"id": material_id})
            continue
        selected_materials.append({
            "id": material_id,
            "code": "",
            "status": "DELETED",
            "sourceAvailable": False,
        })
    item = {
        "id": row["id"],
        "taskId": row["task_id"],
        "inputVersionId": row["input_version_id"],
        "sourceMaterialIds": loads(row["source_material_ids_json"], []),
        "candidateMaterialIds": loads(row["candidate_material_ids_json"], []) if "candidate_material_ids_json" in row.keys() else [],
        "selectedMaterialIds": referenced_material_ids,
        "selectedMaterials": selected_materials,
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
    return redact_deleted_material_codes(item, deleted_material_codes)


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


def serialize_result_image(image: Any) -> dict[str, Any]:
    with get_db() as db:
        versions = published_versions(db, image)
    return {
        'id': image['id'], 'sortOrder': image['sort_order'], 'shotRole': image['shot_role'],
        'status': image['status'], 'userInstruction': loads(row_value(image, 'user_instruction_json'), {}),
        'asset': get_media_asset_by_id(image['current_asset_id']),
        'activeAttemptId': image['active_attempt_id'], 'activeAttempt': get_attempt_summary(image['active_attempt_id']),
        'createdAt': image['created_at'], 'updatedAt': image['updated_at'],
        'versions': [{'id': v['id'], 'asset': get_media_asset_by_id(v['media_asset_id']),
            'createdAt': v['created_at'], 'isCurrent': v['media_asset_id'] == image['current_asset_id'],
            'canRegenerate': executable_package(json_object(v['request_snapshot_json']))} for v in versions],
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
    preparation = loads(row_value(row, "prompt_preparation_json"), {})
    return {
        "id": row["id"],
        "status": row["status"],
        "previewId": row["preview_id"] if "preview_id" in row.keys() else None,
        "poseSelectionId": row_value(row, "pose_selection_id"),
        "userInstruction": loads(row_value(row, "user_instruction_json"), {}),
        "preparation": {"status": preparation.get("status", "DIRECT"), "error": preparation.get("error"),
                        "attemptId": preparation.get("attemptId")},
        "images": [serialize_result_image(image) for image in images],
        "stylingId": row['styling_id'],
        "selectedMaterials": (get_generation_styling(row['styling_id']) or {}).get('selectedMaterials', []),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def actual_model_input_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    result: dict[str, Any] = {}
    if snapshot.get("systemInstruction"):
        result["systemInstruction"] = snapshot["systemInstruction"]
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
    "generation_pose_selection",
    "generation_preview",
    "generation_result_image",
    "generation_result_set",
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
            WHERE (ga.owner_type IN ('generation_styling', 'generation_pose_selection') AND ga.owner_id = ?)
               OR (ga.owner_type = 'generation_result_set' AND ga.owner_id IN (SELECT id FROM generation_result_sets WHERE task_id = ?))
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
    if owner_type == "generation_result_set":
        return ("ga.owner_type = 'generation_result_set' AND ga.owner_id IN (SELECT id FROM generation_result_sets WHERE task_id = ?)", (task_id,))
    if owner_type in {"generation_styling", "generation_pose_selection"}:
        return "ga.owner_type = ? AND ga.owner_id = ?", (owner_type, task_id)
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
        if retained_snapshot_reference_count(db, clean):
            return True
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
        if retained_snapshot_reference_count(db, clean + '/'):
            return True
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


# Pose library: assets remain independent of photography masters and generation history.
class PoseUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class PoseManageRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=100)
    action: str
    confirmPhrase: str | None = None


def pose_row(db: Any, pose_id: int) -> Any:
    row = db.execute("SELECT * FROM poses WHERE id = ?", (pose_id,)).fetchone()
    if not row or row["archive_reason"] == "deletion_pending":
        raise HTTPException(404, "姿势不存在")
    return row


def ensure_pose_editable(row: Any) -> None:
    if row["status"] == "TAGGING_RUNNING":
        raise HTTPException(409, "姿势正在识别，请完成后再修改")
    if row["archived_at"]:
        raise HTTPException(409, "请先取消归档再修改姿势")


def serialize_pose(row: Any) -> dict[str, Any]:
    with get_db() as db:
        images = db.execute("""SELECT ma.*, pi.id AS link_id FROM pose_images pi
            JOIN media_assets ma ON ma.id = pi.media_asset_id WHERE pi.pose_id = ? AND ma.deleted_at IS NULL AND ma.status = 'READY'
            ORDER BY pi.sort_order, pi.id""", (row["id"],)).fetchall()
    return {"id": row["id"], "code": row["code"], "name": row["name"], "status": row["status"],
        "aiTags": loads(row["ai_tags_json"], {}), "archivedAt": row["archived_at"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "error": provider_error_presentation(row["tag_error_code"]) if row["tag_error_code"] else None,
        "images": [{"id": image["link_id"], "asset": serialize_media_asset(image)} for image in images]}


@app.get("/api/poses", dependencies=[Depends(require_admin)])
def list_poses(q: str = "", status: str = "", include_archived: bool = Query(False, alias="includeArchived"),
               limit: int = Query(24, ge=1, le=96), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    clauses, params = ["COALESCE(archive_reason, '') <> 'deletion_pending'"], []
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if status:
        clauses.append("status = ?")
        params.append(status)
    terms = list(dict.fromkeys(re.split(r"[\s,，.。、]+", q.strip())))[:100]
    terms = [term for term in terms if term]
    if terms:
        clauses.append("(" + " OR ".join("(instr(lower(name || ' ' || code || ' ' || COALESCE(ai_tags_json, '')), lower(?)) > 0)" for _ in terms) + ")")
        params.extend(terms)
    where = " AND ".join(clauses)
    with get_db() as db:
        total = db.execute(f"SELECT COUNT(*) FROM poses WHERE {where}", params).fetchone()[0]
        rows = db.execute(f"SELECT * FROM poses WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    return {"items": [serialize_pose(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@app.post("/api/poses", dependencies=[Depends(require_admin)])
async def create_pose(name: str = Form(...), files: list[UploadFile] = File(...)) -> dict[str, Any]:
    name = name.strip()
    if not name or len(name) > 80 or len(files) != 1:
        raise HTTPException(400, "请填写 1–80 字的姿势名称，每个姿势只能导入 1 张图片")
    assets = await save_uploads_as_media_assets(files)
    try:
        with get_db() as db:
            timestamp = now_iso()
            pose_id = int(db.execute("""INSERT INTO poses (code, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)""", (f"P-{uuid4().hex[:12]}", name, timestamp, timestamp)).lastrowid)
            db.executemany("INSERT INTO pose_images (pose_id, media_asset_id, sort_order) VALUES (?, ?, ?)",
                           [(pose_id, asset["id"], index) for index, asset in enumerate(assets)])
            row = pose_row(db, pose_id)
    except Exception:
        cleanup_created_media_assets(assets)
        raise
    return {"item": serialize_pose(row)}


@app.post("/api/poses/delete-impact", dependencies=[Depends(require_admin)])
def pose_delete_impact(payload: DeleteImpactRequest) -> dict[str, Any]:
    ids = normalized_task_ids(payload.ids)
    with get_db() as db:
        rows = [pose_row(db, pose_id) for pose_id in ids]
        for row in rows:
            if row["status"] == "TAGGING_RUNNING":
                raise HTTPException(409, "识别中的姿势暂不可归档或删除")
        return pose_purge_impact(db, ids)


def pose_purge_impact(db: Any, ids: list[int]) -> dict[str, Any]:
    placeholders = ','.join('?' for _ in ids)
    assets = db.execute(f"SELECT DISTINCT ma.* FROM media_assets ma JOIN pose_images pi ON pi.media_asset_id = ma.id WHERE pi.pose_id IN ({placeholders})", ids).fetchall()
    releasable = []
    for asset in assets:
        owned = db.execute(f"SELECT COUNT(*) FROM pose_images WHERE media_asset_id = ? AND pose_id IN ({placeholders})", (asset["id"], *ids)).fetchone()[0]
        # Ignore the recognition attempts that are part of the deleted aggregate.
        snapshot_refs = db.execute(f"""SELECT COUNT(*) FROM generation_attempts
            WHERE (request_snapshot_json LIKE ? OR response_snapshot_json LIKE ?)
            AND NOT (owner_type = 'pose_tagging' AND owner_id IN ({placeholders}))""",
            (f'%{asset["object_key"]}%', f'%{asset["object_key"]}%', *ids)).fetchone()[0]
        if media_asset_direct_reference_count(db, asset["id"]) == owned and not snapshot_refs and not db.execute(
            "SELECT 1 FROM generation_task_input_versions WHERE image_snapshot_json LIKE ? UNION SELECT 1 FROM generation_stylings WHERE selected_material_snapshot_json LIKE ? LIMIT 1",
            (f'%{asset["object_key"]}%', f'%{asset["object_key"]}%')).fetchone():
            releasable.append(asset)
    task_ids = [row[0] for row in db.execute(f"""SELECT DISTINCT ps.task_id FROM generation_pose_selections ps
        JOIN generation_pose_selection_shots shots ON shots.selection_id = ps.id WHERE shots.pose_id IN ({placeholders})""", ids)]
    return {"poseCount": len(ids), "mediaAssetCount": len(releasable),
            "estimatedBytes": sum((row["size_bytes"] or 0) + (row["thumb_size_bytes"] or 0) + (row["preview_size_bytes"] or 0) for row in releasable),
            "referencingTaskCount": len(task_ids), "referencingTaskIds": task_ids,
            "taskCount": 0, "resultImageCount": 0}


@app.post("/api/poses/manage", dependencies=[Depends(require_admin)])
def manage_poses(payload: PoseManageRequest) -> dict[str, Any]:
    ids = normalized_task_ids(payload.ids)
    if payload.action not in {"archive", "restore", "purge"}:
        raise HTTPException(400, "不支持的姿势管理操作")
    placeholders = ','.join('?' for _ in ids)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = [pose_row(db, pose_id) for pose_id in ids]
        if any(row["status"] == "TAGGING_RUNNING" for row in rows):
            raise HTTPException(409, "识别中的姿势暂不可归档或删除")
        if payload.action != "purge":
            db.execute(f"UPDATE poses SET archived_at = ?, archive_reason = NULL, updated_at = ? WHERE id IN ({placeholders})",
                       (now_iso() if payload.action == "archive" else None, now_iso(), *ids))
            return {"ok": True}
        phrase = rows[0]["name"] if len(ids) == 1 else "全部删除"
        if payload.confirmPhrase != phrase:
            raise HTTPException(400, f"请输入{phrase}以确认彻底删除")
        db.execute(f"UPDATE poses SET archive_reason = 'deletion_pending', archived_at = ?, updated_at = ? WHERE id IN ({placeholders})", (now_iso(), now_iso(), *ids))
    object_keys: list[str] = []
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            assets = db.execute(f"SELECT DISTINCT ma.* FROM media_assets ma JOIN pose_images pi ON pi.media_asset_id = ma.id WHERE pi.pose_id IN ({placeholders})", ids).fetchall()
            db.execute(f"DELETE FROM generation_attempts WHERE owner_type = 'pose_tagging' AND owner_id IN ({placeholders})", ids)
            db.execute(f"DELETE FROM poses WHERE id IN ({placeholders})", ids)
            for asset in assets:
                if media_asset_direct_reference_count(db, asset["id"]) == 0 and media_asset_snapshot_reference_count(db, asset["object_key"]) == 0:
                    detach_upload_session_media_assets(db, [asset["id"]])
                    db.execute("DELETE FROM media_assets WHERE id = ?", (asset["id"],))
                    object_keys.append(asset["object_key"])
    except Exception:
        with get_db() as db:
            db.execute(f"UPDATE poses SET archive_reason = 'deletion_failed' WHERE id IN ({placeholders})", ids)
        raise
    return {"ok": True, "objectCleanupErrors": cleanup_deleted_media_objects(object_keys)}


@app.get("/api/poses/{pose_id}", dependencies=[Depends(require_admin)])
def get_pose(pose_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = pose_row(db, pose_id)
    return {"item": serialize_pose(row)}


@app.patch("/api/poses/{pose_id}", dependencies=[Depends(require_admin)])
def update_pose(pose_id: int, payload: PoseUpdateRequest) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "姿势名称不能为空")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_pose_editable(pose_row(db, pose_id))
        db.execute("UPDATE poses SET name = ?, updated_at = ? WHERE id = ?", (name, now_iso(), pose_id))
    return get_pose(pose_id)


@app.post("/api/poses/{pose_id}/images", dependencies=[Depends(require_admin)])
async def add_pose_images(pose_id: int, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    # Compatibility endpoint: only repair a legacy entry with no image.
    return await save_pose_image(pose_id, files, replace=False)


@app.put("/api/poses/{pose_id}/image", dependencies=[Depends(require_admin)])
async def replace_pose_image(pose_id: int, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    return await save_pose_image(pose_id, files, replace=True)


async def save_pose_image(pose_id: int, files: list[UploadFile], *, replace: bool) -> dict[str, Any]:
    if len(files) != 1:
        raise HTTPException(400, "每个姿势只能保存 1 张图片")
    with get_db() as db:
        ensure_pose_editable(pose_row(db, pose_id))
        if not replace and db.execute("SELECT 1 FROM pose_images WHERE pose_id = ?", (pose_id,)).fetchone():
            raise HTTPException(409, "该姿势已有图片，请使用替换图片")
    assets = await save_uploads_as_media_assets(files)
    keys: list[str] = []
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            ensure_pose_editable(pose_row(db, pose_id))
            old = db.execute("""SELECT ma.* FROM media_assets ma JOIN pose_images pi ON pi.media_asset_id = ma.id
                WHERE pi.pose_id = ?""", (pose_id,)).fetchall()
            if old and not replace:
                raise HTTPException(409, "该姿势已有图片，请使用替换图片")
            db.execute("DELETE FROM pose_images WHERE pose_id = ?", (pose_id,))
            db.execute("INSERT INTO pose_images (pose_id, media_asset_id) VALUES (?, ?)", (pose_id, assets[0]["id"]))
            db.execute("UPDATE poses SET status = 'DRAFT', tag_error_code = NULL, updated_at = ? WHERE id = ?", (now_iso(), pose_id))
            for asset in old:
                if not media_asset_direct_reference_count(db, asset["id"]) and not media_asset_snapshot_reference_count(db, asset["object_key"]):
                    detach_upload_session_media_assets(db, [asset["id"]])
                    db.execute("DELETE FROM media_assets WHERE id = ?", (asset["id"],))
                    keys.append(asset["object_key"])
    except Exception:
        cleanup_created_media_assets(assets)
        raise
    return {**get_pose(pose_id), "objectCleanupErrors": cleanup_deleted_media_objects(keys)}


@app.delete("/api/poses/{pose_id}/images/{image_id}", dependencies=[Depends(require_admin)])
def remove_pose_image(pose_id: int, image_id: int) -> dict[str, Any]:
    with get_db() as db:
        ensure_pose_editable(pose_row(db, pose_id))
        if not db.execute("SELECT 1 FROM pose_images WHERE pose_id = ? AND id = ?", (pose_id, image_id)).fetchone():
            raise HTTPException(404, "姿势图片不存在")
    raise HTTPException(409, "每个姿势需保留 1 张图片，请替换图片或归档该姿势")


@app.post("/api/poses/{pose_id}/recognize", dependencies=[Depends(require_admin)])
async def recognize_pose(pose_id: int) -> dict[str, Any]:
    pose = get_pose(pose_id)["item"]
    if len(pose["images"]) != 1:
        raise HTTPException(409, "每个姿势需有且只有 1 张参考图")
    runtime = {"pose_context": dumps({"code": pose["code"], "images": [{"imageId": i["asset"]["id"], "order": n + 1} for n, i in enumerate(pose["images"]) ]})}
    try:
        prompt = render_prompt_template("assembly.pose_tagging", runtime)
    except RuntimeError as exc:
        raise HTTPException(409, "请联系管理员在 Prompt 维护中配置姿势识别规则") from exc
    snapshot = {**gemini_request_meta(), "prompt": prompt, **prompt_execution_facts("assembly.pose_tagging", prompt, runtime), "pose": pose}
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        current = pose_row(db, pose_id)
        ensure_pose_editable(current)
        if current["updated_at"] != pose["updatedAt"]:
            raise HTTPException(409, "姿势已变化，请刷新后重新识别")
        attempt_id = create_attempt("pose_tagging", pose_id, effective_text_provider(), snapshot, db=db)
        db.execute("UPDATE poses SET status = 'TAGGING_RUNNING', tag_error_code = NULL, tag_attempt_id = ?, updated_at = ? WHERE id = ?", (attempt_id, now_iso(), pose_id))
    schedule_background_generation(run_pose_recognition(pose_id, attempt_id, pose, prompt), job_key=f"pose_tagging:{pose_id}")
    return get_pose(pose_id)


async def run_pose_recognition(pose_id: int, attempt_id: int, pose: dict[str, Any], prompt: str) -> None:
    try:
        images = [gemini_vision_input(item["asset"]["objectKey"], item["asset"]["mimeType"]) for item in pose["images"]]
        if not all(images):
            raise ValueError("姿势参考图不可用")
        text = await generate_gemini_json_text(prompt, images)
        raw = parse_gemini_json_object(text)
        name = raw.get("name") if isinstance(raw, dict) else None
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("姿势识别未返回有效 name（需为 1–80 字的名称）")
        tags = raw.get("tags") if isinstance(raw, dict) else None
        if not isinstance(tags, dict) or not tags:
            raise ValueError("姿势识别未返回有效 tags（需为非空标签对象）")
        finish_attempt(attempt_id, "SUCCEEDED", response_snapshot=raw)
        with get_db() as db:
            db.execute("UPDATE poses SET name = ?, ai_tags_json = ?, status = 'READY', tag_error_code = NULL, updated_at = ? WHERE id = ? AND tag_attempt_id = ? AND status = 'TAGGING_RUNNING'", (name.strip(), dumps(tags), now_iso(), pose_id, attempt_id))
    except Exception as exc:
        error_code = classify_attempt_error(exc) or ERROR_MALFORMED_OUTPUT
        finish_attempt(attempt_id, "FAILED", response_snapshot=exception_process_facts(exc), error_message=str(exc), error_code=error_code)
        with get_db() as db:
            db.execute("UPDATE poses SET status = 'TAGGING_FAILED', tag_error_code = ?, updated_at = ? WHERE id = ? AND tag_attempt_id = ? AND status = 'TAGGING_RUNNING'", (error_code, now_iso(), pose_id, attempt_id))


def pose_selection_context(task: Any, user_instruction: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[Any], dict[str, Any]]:
    with get_db() as db:
        poses = db.execute("SELECT * FROM poses WHERE status = 'READY' AND archived_at IS NULL ORDER BY created_at DESC, id DESC").fetchall()
        styling = db.execute("SELECT * FROM generation_stylings WHERE id = ? AND task_id = ?", (task["active_styling_id"], task["id"])).fetchone()
        if not styling:
            raise HTTPException(409, "请先完成 AI 智能选搭")
        plan = loads(styling["styling_plan_json"], {})
        family = storyboard_category_family(plan)
    background = get_media_asset_by_id(get_current_shoot_environment_asset_id())
    if not background:
        raise HTTPException(409, "请联系管理员在模特与背景中配置统一拍摄背景")
    references = [{"shot_index": index, "background_asset_id": background["id"]} for index in range(1, 6)]
    library = [serialize_pose(row) for row in poses]
    library = [pose for pose in library if pose["images"] and all(image["asset"] for image in pose["images"])]
    if not library:
        raise HTTPException(409, "姿势库暂无可选姿势，请先导入姿势并完成识别")
    if len(library) < 5:
        raise HTTPException(409, f"五个镜头需要不同姿势，当前仅有 {len(library)} 个可用姿势，请至少补齐五个")
    cards = [{"code": p["code"], "name": p["name"], "ai_tags": p["aiTags"],
              "images": [{"imageId": item["asset"]["id"]} for item in p["images"]]} for p in library]
    runtime = {"pose_phase": "shortlist", "pose_candidates": dumps(cards), "pose_shortlist": "[]",
        "pose_user_instruction": user_instruction, "category_family": family,
        "styling_result": dumps(styling_result_for_prompt(plan, loads(styling["selected_material_snapshot_json"], []))),
        "pose_shots": dumps([{"shotIndex": row["shot_index"], "shotRole": role} for row, role in zip(references, final_image_slot_roles(plan))]),
        "pose_image_manifest": "[]"}
    return runtime, library, references, dict(styling)


def get_generation_pose_selection(selection_id: int | None) -> dict[str, Any] | None:
    if not selection_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_pose_selections WHERE id = ?", (selection_id,)).fetchone()
        if not row:
            return None
        shots = db.execute("SELECT * FROM generation_pose_selection_shots WHERE selection_id = ? ORDER BY shot_index", (selection_id,)).fetchall()
    plan = loads(row["plan_json"], {})
    return {"id": row["id"], "stylingId": row["styling_id"], "workflow": plan.get("workflow", "legacy_masters"), "summary": str(plan.get("summary") or ""),
        "createdAt": row["created_at"], "shots": [{"shotIndex": shot["shot_index"],
            **loads(shot["pose_snapshot_json"], {}), "sourceAvailable": shot["pose_id"] is not None,
            "pose": get_media_asset_by_id(shot["pose_asset_id"]), "background": get_media_asset_by_id(shot["background_asset_id"])} for shot in shots]}


@app.post("/api/generation-tasks/{task_id}/select-poses", dependencies=[Depends(require_admin)])
async def select_generation_poses(task_id: int, payload: GenerationStageInstructionRequest | None = None) -> dict[str, Any]:
    # Run independently of the HTTP connection; the task/attempt are persisted before dispatch.
    context = claim_pose_selection(task_id, payload.userInstruction if payload else None, "manual_step")
    schedule_background_generation(execute_pose_selection(task_id, *context, trigger="manual_step"), job_key=f"pose_selection:{task_id}")
    return {"item": get_generation_task_by_id(task_id)}


def claim_pose_selection(task_id: int, user_instruction: str | None, trigger: str) -> tuple[Any, ...]:
    task = get_generation_task_row(task_id)
    if task["status"] in RUNNING_GENERATION_TASK_STATUSES or (row_value(task, "auto_run_target") and trigger != "auto_run"):
        raise HTTPException(409, "当前任务正在执行，请等待完成或取消")
    if not task["active_styling_id"] or task["archived_at"]:
        raise HTTPException(409, "请先完成 AI 智能选搭，且任务不能已归档")
    instruction = (user_instruction if user_instruction is not None else row_value(task, "pose_selection_user_instruction", "")) or ""
    if len(instruction) > MAX_USER_INPUT_CHARS:
        raise HTTPException(400, f"姿势要求最多 {MAX_USER_INPUT_CHARS} 字")
    runtime, library, references, styling = pose_selection_context(task, instruction)
    config = {key: dict(row) for key, row in get_prompt_config_rows().items()}
    # Render the same frozen per-shot rules that will be used for final images.
    plan = loads(styling["styling_plan_json"], {})
    input_version = get_input_version(styling["input_version_id"]) or {}
    requirements = []
    for index, role in enumerate(final_image_slot_roles(plan), 1):
        shot_runtime = build_final_prompt_runtime(index, role, plan, [], {}, input_version.get("userRequirement", {}))
        shot_runtime.update(ref_shot_background="统一背景参考图", ref_shot_pose="当前镜头待选姿势图",
                            ref_model_identity="数字模特身份参考图", ref_hero_garment="主推款参考图",
                            ref_styling_garment="搭配款参考图" if loads(styling["selected_material_snapshot_json"], []) else "")
        requirement, check = render_prompt_config(final_image_prompt_config_key(plan), shot_runtime, rows=config)
        if check["missingPlaceholders"] or not requirement.strip():
            raise HTTPException(409, "请先配置当前服装类型的成片 Prompt 与镜头要求")
        requirements.append({"shotIndex": index, "shotRole": role, "requirements": requirement})
    runtime["pose_shots"] = dumps(requirements)
    prompt, diagnostics = render_prompt_config("assembly.pose_selection", runtime, rows=config)
    if diagnostics["missingPlaceholders"]:
        raise HTTPException(409, "请联系管理员在 Prompt 维护中配置姿势选搭规则与品牌信息")
    snapshot = {**gemini_request_meta(), "promptConfigKey": "assembly.pose_selection", "prompt": prompt, "promptConfigSnapshot": config,
        "workflow": "shared_background_v1", "runtime": runtime, "poses": library, "references": [{**dict(row), "backgroundAsset": get_media_asset_by_id(row["background_asset_id"])} for row in references],
        "capturedTaskInputVersionId": task["current_input_version_id"], "stylingId": styling["id"], "inputVersionId": styling["input_version_id"], "invocation": {"mode": trigger, "target": "pose_selection"}}
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT * FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
        if (not current or current["deleted_at"] or current["archived_at"] or current["status"] in RUNNING_GENERATION_TASK_STATUSES
                or current["active_styling_id"] != styling["id"] or current["current_input_version_id"] != task["current_input_version_id"]
                or (current["auto_run_target"] and trigger != "auto_run")):
            raise HTTPException(409, "任务已变化，请刷新后重新选姿势")
        attempt_id = create_attempt("generation_pose_selection", task_id, effective_text_provider(), snapshot, db=db)
        db.execute("UPDATE generation_tasks SET status = 'POSE_SELECTION_RUNNING', pose_workflow_enabled = 1, pose_selection_user_instruction = ?, updated_at = ? WHERE id = ?", (instruction, now_iso(), task_id))
    return attempt_id, runtime, library, [dict(row) for row in references], styling, config, snapshot


async def run_select_generation_poses(task_id: int, trigger: str = "auto_run") -> dict[str, Any]:
    before = get_generation_task_row(task_id)
    try:
        context = claim_pose_selection(task_id, None, trigger)
    except HTTPException as exc:
        if trigger != "auto_run":
            raise
        # A hidden preparation stage has no open HTTP request to display preflight
        # errors. Persist the failure so polling/reload can explain and resume it.
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
            if (current and current["auto_run_target"] == AUTO_RUN_TARGET_FINAL
                    and current["status"] == before["status"]
                    and current["status"] not in RUNNING_GENERATION_TASK_STATUSES
                    and not current["archived_at"] and not current["deleted_at"]
                    and current["active_styling_id"] == before["active_styling_id"]
                    and current["current_input_version_id"] == before["current_input_version_id"]):
                snapshot = {"stylingId": current["active_styling_id"], "inputVersionId": current["current_input_version_id"],
                            "invocation": {"mode": trigger, "target": "pose_selection"}, "providerCallStarted": False}
                attempt_id = create_attempt("generation_pose_selection", task_id, effective_text_provider(), snapshot, db=db)
                timestamp = now_iso()
                db.execute("""UPDATE generation_attempts SET status = 'FAILED', error_code = 'SHOOT_PREPARATION_FAILED',
                    error_message = ?, failed_at = ?, updated_at = ? WHERE id = ?""", (str(exc.detail), timestamp, timestamp, attempt_id))
                db.execute("""UPDATE generation_tasks SET status = 'FAILED', auto_run_target = NULL,
                    auto_run_requested_at = NULL, auto_run_retry_at = NULL, updated_at = ? WHERE id = ?""", (timestamp, task_id))
        raise
    await execute_pose_selection(task_id, *context, trigger=trigger)
    return {"item": get_generation_task_by_id(task_id)}


async def execute_pose_selection(task_id: int, attempt_id: int, runtime: dict[str, Any], library: list[dict[str, Any]],
                                 references: list[dict[str, Any]], styling: dict[str, Any], config: dict[str, Any],
                                 snapshot: dict[str, Any], trigger: str) -> None:
    try:
        if generation_task_or_attempt_cancelled(task_id, attempt_id):
            return
        image_inputs, manifest = [], []

        def add_image(asset: dict[str, Any] | None, role: str, **facts: Any) -> None:
            if not asset:
                raise ValueError("姿势选搭参考图片不可用")
            image_inputs.append(image_input_from_asset(asset, role))
            manifest.append({"order": len(image_inputs), "role": role, "imageId": asset["id"], "objectKey": asset["objectKey"], **facts})

        input_version = get_input_version(styling["input_version_id"]) or {}
        for image in input_version.get("imageSnapshot", []):
            add_image(image.get("asset") or image, "hero")
        for garment in loads(styling["selected_material_snapshot_json"], []):
            for image in garment.get("images", []):
                add_image(image.get("asset") or image, "garment", code=garment.get("code"))
        add_image(snapshot["references"][0]["backgroundAsset"], "shared_background")
        image_limit = TOAPIS_MAX_MESSAGE_IMAGES if gemini_channel() == "toapis" else None
        packed, provider_manifest = pack_pose_references(image_inputs, manifest, image_limit)
        runtime = {**runtime, "pose_image_manifest": dumps(provider_manifest)}
        prompt, diagnostics = render_prompt_config("assembly.pose_selection", runtime, rows=config)
        if diagnostics["missingPlaceholders"]:
            raise ValueError("姿势选搭配置缺少运行参数")
        snapshot = {**snapshot, "prompt": prompt, "runtime": runtime, "shortlistImageManifest": list(manifest),
                    "shortlistProviderImageManifest": provider_manifest, "selectionMode": "shortlist_only"}
        update_attempt_request_snapshot(attempt_id, snapshot)
        if generation_task_or_attempt_cancelled(task_id, attempt_id):
            return
        raw_text = await generate_gemini_json_text(
            prompt, [vision_reference_input(item) for item in packed],
            should_cancel=lambda: generation_task_or_attempt_cancelled(task_id, attempt_id),
        )
        raw = parse_gemini_json_object(raw_text)
        persist_attempt_provider_progress(attempt_id, {"shortlist": raw})
        if generation_task_or_attempt_cancelled(task_id, attempt_id):
            return
        # Old saved prompts may still return candidateCodes. Use each first choice
        # verbatim; never rerank, substitute duplicates, or call visual refinement.
        plan = pose_plan_from_shortlist(raw, library)
        shots = validate_pose_plan(plan, {p["code"]: {i["asset"]["id"] for i in p["images"]} for p in library})
        plan = {**plan, "shots": shots, "selectionMode": "shortlist_only"}
        plan = {**plan, "workflow": "shared_background_v1",
                "shotRequirements": loads(runtime["pose_shots"], []),
                "promptConfigSnapshot": {key: value for key, value in config.items()
                    if key.startswith("segment.") or key == final_image_prompt_config_key(loads(styling["styling_plan_json"], {}))}}
        if generation_task_or_attempt_cancelled(task_id, attempt_id):
            return
        by_code = {p["code"]: p for p in library}
        timestamp = now_iso()
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
            attempt = db.execute("SELECT status FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
            if not task or task["status"] != "POSE_SELECTION_RUNNING" or task["active_styling_id"] != styling["id"] or task["current_input_version_id"] != snapshot["capturedTaskInputVersionId"] or not attempt or attempt["status"] != "RUNNING":
                return
            # Archival/removal during the model call cannot silently select a retired source.
            for shot in shots:
                pose = by_code[shot["poseCode"]]
                row = pose_row(db, pose["id"])
                if row["status"] != "READY" or row["archived_at"] or not db.execute("SELECT 1 FROM pose_images WHERE pose_id = ? AND media_asset_id = ?", (pose["id"], shot["imageId"])).fetchone():
                    raise ValueError("候选姿势在选搭期间发生变化，请重新选姿势")
            selection_id = int(db.execute("""INSERT INTO generation_pose_selections
                (task_id, input_version_id, styling_id, category_family, plan_json, attempt_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (task_id, styling["input_version_id"], styling["id"], runtime["category_family"], dumps(plan), attempt_id, timestamp)).lastrowid)
            for shot in shots:
                pose = by_code[shot["poseCode"]]
                pose_snapshot = {"code": pose["code"], "name": pose["name"], "aiTags": pose["aiTags"], "reason": shot["reason"], "direction": shot["direction"]}
                db.execute("""INSERT INTO generation_pose_selection_shots
                    (selection_id, shot_index, pose_id, pose_snapshot_json, background_asset_id, pose_asset_id)
                    VALUES (?, ?, ?, ?, ?, ?)""", (selection_id, shot["shotIndex"], pose["id"], dumps(pose_snapshot), references[shot["shotIndex"] - 1]["background_asset_id"], shot["imageId"]))
            supersede_generation_result_sets(db, task_id, timestamp)
            db.execute("UPDATE generation_tasks SET status = 'POSE_SELECTION_READY', current_input_version_id = ?, active_pose_selection_id = ?, active_result_set_id = NULL, updated_at = ? WHERE id = ?", (styling["input_version_id"], selection_id, timestamp, task_id))
        finish_attempt(attempt_id, "SUCCEEDED", response_snapshot={"shortlist": raw, "plan": plan})
    except Exception as exc:
        if generation_task_or_attempt_cancelled(task_id, attempt_id):
            return
        code = classify_attempt_error(exc) or ERROR_MALFORMED_OUTPUT
        finish_attempt(attempt_id, "FAILED", response_snapshot=exception_process_facts(exc), error_message=str(exc), error_code=code)
        with get_db() as db:
            db.execute("UPDATE generation_tasks SET status = 'FAILED', updated_at = ? WHERE id = ? AND status = 'POSE_SELECTION_RUNNING'", (now_iso(), task_id))
        if trigger != "auto_run" or not schedule_auto_run_retry(task_id, [code]):
            clear_task_auto_run(task_id)


def recover_interrupted_pose_jobs() -> None:
    with get_db() as db:
        poses = db.execute("SELECT id, tag_attempt_id FROM poses WHERE status = 'TAGGING_RUNNING'").fetchall()
        tasks = db.execute("""SELECT gt.id, gt.auto_run_target, (SELECT id FROM generation_attempts WHERE owner_type = 'generation_pose_selection'
            AND owner_id = gt.id AND status = 'RUNNING' ORDER BY id DESC LIMIT 1) AS attempt_id
            FROM generation_tasks gt WHERE gt.status = 'POSE_SELECTION_RUNNING'""").fetchall()
    for row in poses:
        if f"pose_tagging:{row['id']}" in active_background_generation_jobs:
            continue
        fail_stale_attempt(row["tag_attempt_id"], "姿势识别进程已中断，请重新识别", ERROR_PROVIDER_TIMEOUT)
        with get_db() as db:
            db.execute("UPDATE poses SET status = 'TAGGING_FAILED', tag_error_code = ?, updated_at = ? WHERE id = ? AND status = 'TAGGING_RUNNING'", (ERROR_PROVIDER_TIMEOUT, now_iso(), row["id"]))
    for row in tasks:
        if f"pose_selection:{row['id']}" in active_background_generation_jobs or auto_run_job_key(row["id"]) in active_background_generation_jobs:
            continue
        fail_stale_attempt(row["attempt_id"], "姿势选搭进程已中断，可继续选搭", ERROR_PROVIDER_TIMEOUT)
        with get_db() as db:
            db.execute("UPDATE generation_tasks SET status = 'FAILED', updated_at = ? WHERE id = ? AND status = 'POSE_SELECTION_RUNNING'", (now_iso(), row["id"]))
        if row["auto_run_target"]:
            if not schedule_auto_run_retry(row["id"], [ERROR_PROVIDER_TIMEOUT], process_interrupted=True):
                clear_task_auto_run(row["id"])


@app.get("/api/pose-prompt-drafts/{kind}")
def get_pose_prompt_draft(kind: str, request: Request) -> dict[str, str]:
    require_admin(request)
    if kind not in {"tagging", "selection"}:
        raise HTTPException(404, "参考规则不存在")
    path = Path(__file__).resolve().parent.parent / "prompt_templates" / f"pose_{kind}.txt"
    return {"content": path.read_text(encoding="utf-8")}
