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
from uuid import uuid4
from typing import Any
from urllib.parse import urlencode

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from .config import get_settings
from .db import dumps, get_db, init_db, loads, log_event, now_iso
from .gemini_provider import (
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
from .generation_progress import (
    ERROR_DELIVERY_FAILED,
    ERROR_PROVIDER_FAILED,
    ERROR_PROVIDER_TIMEOUT,
    PHASE_DELIVER,
    PHASE_GENERATE,
    PHASE_PREPARE,
    PHASE_QUEUE,
    PHASE_UPLOAD,
    AttemptProgressRecorder,
    GenerationPhaseError,
    classify_attempt_error,
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
Image.MAX_IMAGE_PIXELS = settings.max_upload_image_pixels
SESSION_ROLES = {"user", "admin"}
SHOOT_CONFIG_EXCLUDED_CATEGORY_CODES = {"未知", "不支持入库配饰", "unknown", "unsupported_accessory"}
PROMPT_PLACEHOLDER_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_.-]+)\s*}}")
AI_STYLING_PROMPT_CONFIG_KEY = "assembly.ai_styling"
SHOOT_SCRIPT_PROMPT_CONFIG_KEY = "assembly.shoot_script"
AUTO_RUN_TARGET_FINAL = "FINAL"
# /api/media 重定向的浏览器缓存时长:必须显著小于 presign 有效期,避免缓存过期签名。
MEDIA_REDIRECT_CACHE_SECONDS = max(0, min(300, settings.s3_presign_expires_seconds - 120))
MAX_USER_REQUIREMENT_CHARS = 800
MAX_USER_INPUT_CHARS = 800


# ToAPIs OpenAI 兼容通道没有原生 JSON 模式，模型手写 JSON 的输出形态存在方差
# （markdown 围栏 / 单引号 dict / 字符串内裸换行）。此纪律用于压低方差；
# 解析端的 parse_gemini_json_object 仍保留对应兜底，两层缺一不可。
GEMINI_JSON_OUTPUT_DISCIPLINE = (
    "输出纪律（必须遵守）：直接输出 JSON 本体，第一个字符必须是 {；"
    "不要 markdown 代码围栏，不要任何解释文字；"
    "只允许双引号，禁止单引号和 Python 字典风格；"
    "字符串值内的换行必须写成 \\n 转义，不得输出裸换行。"
)


AI_STYLIST_SYSTEM_INSTRUCTION = """你是 Lily 品牌 AI 摄影棚的 AI Stylist。
你必须遵守不可覆盖的生产边界：商品事实以主推款图片和搭配服装库事实为准；搭配服装只能来自 READY 候选 code；不得编造商品级服饰；必须返回合法 JSON。
用户要求是本次任务的高优先级业务输入，高于默认搭配偏好；但不得覆盖商品图片事实、搭配库事实、Lily 品牌硬边界、输出 schema 和安全规则。
当用户要求与硬边界冲突时，不要强行满足；请在 user_requirement_resolution 中说明冲突原因、未满足项和替代处理。
""" + GEMINI_JSON_OUTPUT_DISCIPLINE


AI_DIRECTOR_SYSTEM_INSTRUCTION = """你是 Lily 品牌 AI 摄影棚的 AI Director。
你必须承接已完成的 StylingPlan 和其中的 user_requirement_resolution，不得重新选择库内搭配服装。
用户要求高于默认导演偏好；但不得覆盖商品事实、数字模特身份契约、已选搭配服装、输出 schema 和安全规则。
请只返回合法 JSON。
""" + GEMINI_JSON_OUTPUT_DISCIPLINE


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


class StylingGarmentDeleteRequest(BaseModel):
    ids: list[int] | None = None
    purge: bool = False
    confirmPhrase: str | None = None


class StylingGarmentImageUpdateRequest(BaseModel):
    sortOrder: int | None = None


class GenerationTaskUpdateRequest(BaseModel):
    name: str | None = None
    heroProductInfo: str | None = None
    aiStylingUserInstruction: str | None = None
    shootScriptUserInstruction: str | None = None
    storyboardPreviewUserInstruction: str | None = None
    finalImageUserInstruction: str | None = None
    userRequirement: str | None = None


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
    heroProductInfo: str | None = None
    userRequirement: str | None = None
    uploads: list[UploadedMediaRequest] = Field(default_factory=list)


class GenerationStylingRequest(BaseModel):
    userInstruction: str | None = None
    userRequirement: str | None = None


class GenerationStageInstructionRequest(BaseModel):
    userInstruction: str | None = None


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
PROMPT_TAGGING_RESULT_VERSION = "prompt-configured-json-v1"
TAG_REVIEW_CONFIDENCE_THRESHOLD = 0.72
LOGIN_FAILURE_WINDOW_SECONDS = 300
LOGIN_FAILURE_LIMIT = 8
login_failures_by_client: dict[str, list[float]] = {}
ALLOWED_UPLOAD_FORMATS = {"JPEG", "PNG", "WEBP"}
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


def detected_image_content_type(image_format: str, declared_type: str | None) -> str:
    canonical_type = Image.MIME.get(image_format.upper())
    if canonical_type:
        return canonical_type
    return f"image/{image_format.lower() or 'unknown'}"


def validate_image_content(content: bytes, filename: str, declared_type: str | None = None) -> tuple[str, int, int]:
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
    return detected_image_content_type(image_format, declared_type), width, height


def direct_upload_content_type(mime_type: str | None, filename: str) -> str:
    declared = (mime_type or "").split(";", 1)[0].strip().lower()
    if declared in {"image/jpeg", "image/png", "image/webp"}:
        return declared
    suffix = Path(filename).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    raise HTTPException(status_code=400, detail=f"{filename} 仅支持 JPG、PNG 或 WebP 图片")


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
    ]


def gemini_vision_input(object_key: str | None, fallback_mime: str | None = None) -> dict[str, Any] | None:
    """取 Gemini 理解类调用(打标/选搭/拍摄剧本)的图片输入,优先 preview 派生图。

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
        raise HTTPException(status_code=400, detail="搭配服装代码不能为空")
    if not files:
        raise HTTPException(status_code=400, detail="至少上传一张图片")
    if len(files) > 6:
        raise HTTPException(status_code=400, detail="单套搭配服装最多上传 6 张图片")
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
    if payload.code is not None:
        clean_code = sanitize_name(payload.code)
        if not clean_code:
            raise HTTPException(status_code=400, detail="搭配服装代码不能为空")
        updates["code"] = clean_code
    if payload.manualTags is not None:
        updates["manual_tags_json"] = dumps(normalize_manual_business_tags(payload.manualTags))
    if not updates:
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
            raise HTTPException(status_code=404, detail="搭配服装不存在")
        ensure_styling_garment_not_tagging(row)
        existing_count = db.execute(
            "SELECT COUNT(*) AS count FROM styling_garment_images WHERE styling_garment_id = ? AND deleted_at IS NULL",
            (garment_id,),
        ).fetchone()["count"]
        if int(existing_count) + len(files) > 6:
            raise HTTPException(status_code=400, detail="单套搭配服装最多保存 6 张图片")
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
            raise HTTPException(status_code=404, detail="搭配服装不存在")
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
    heroProductInfo: str | None = Form(None),
    userRequirement: str | None = Form(None),
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
    hero_product_info_text = normalize_user_input_text(heroProductInfo if heroProductInfo is not None else userRequirement)
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO generation_tasks (name, hero_product_info_text, user_requirement_text, status, created_at, updated_at) VALUES (?, ?, ?, 'DRAFT', ?, ?)",
            (task_name, hero_product_info_text, hero_product_info_text, timestamp, timestamp),
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
    if len(payload.uploads) > 8:
        raise HTTPException(status_code=400, detail="主推款最多上传 8 张图片")
    task_name = (payload.name or datetime.now().strftime("%Y%m%d%H%M%S")).strip()
    assets = await save_uploaded_objects_as_media_assets(payload.uploads)
    timestamp = now_iso()
    hero_product_info_text = normalize_user_input_text(
        payload.heroProductInfo if payload.heroProductInfo is not None else payload.userRequirement
    )
    try:
        with get_db() as db:
            cursor = db.execute(
                "INSERT INTO generation_tasks (name, hero_product_info_text, user_requirement_text, status, created_at, updated_at) VALUES (?, ?, ?, 'DRAFT', ?, ?)",
                (task_name, hero_product_info_text, hero_product_info_text, timestamp, timestamp),
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
        "shootScriptUserInstruction": "shoot_script_user_instruction_text",
        "storyboardPreviewUserInstruction": "storyboard_preview_user_instruction_text",
        "finalImageUserInstruction": "final_image_user_instruction_text",
    }
    if (
        payload.name is None
        and not payload_has_field(payload, "heroProductInfo")
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
        if payload.name is not None:
            updates.append("name = ?")
            values.append(payload.name.strip() or "未命名任务")
        if payload_has_field(payload, "heroProductInfo") or payload_has_field(payload, "userRequirement"):
            hero_info = payload.heroProductInfo if payload_has_field(payload, "heroProductInfo") else payload.userRequirement
            normalized = normalize_user_input_text(hero_info)
            updates.append("hero_product_info_text = ?")
            values.append(normalized)
            updates.append("user_requirement_text = ?")
            values.append(normalized)
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
        UPDATE generation_shoot_scripts
        SET status = 'SUPERSEDED', superseded_at = COALESCE(superseded_at, ?), updated_at = ?
        WHERE task_id = ? AND status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')
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
    supersede_generation_result_sets(db, task_id, timestamp)
    db.execute(
        """
        UPDATE generation_tasks
        SET status = 'READY_FOR_STYLING',
            current_input_version_id = NULL,
            active_styling_id = NULL,
            active_shoot_script_id = NULL,
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


def generation_task_stage_instruction_column(stage: str) -> str | None:
    return {
        "ai_styling": "ai_styling_user_instruction_text",
        "shoot_script": "shoot_script_user_instruction_text",
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
    ensure_generation_task_editable(task)
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
        if task["status"] != "DRAFT":
            mark_generation_task_input_changed(db, task_id, timestamp, "hero_product_images_added")
    return {"item": get_generation_task_by_id(task_id)}


@app.post("/api/generation-tasks/{task_id}/images/from-uploads")
async def add_generation_task_images_from_uploads(task_id: int, payload: GenerationTaskFromUploadsRequest) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    ensure_generation_task_editable(task)
    with get_db() as db:
        count = int(db.execute(
            "SELECT COUNT(*) AS count FROM generation_task_images WHERE task_id = ? AND deleted_at IS NULL",
            (task_id,),
        ).fetchone()["count"])
        max_sort = int(db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS max_sort FROM generation_task_images WHERE task_id = ? AND deleted_at IS NULL",
            (task_id,),
        ).fetchone()["max_sort"])
    if not payload.uploads or count + len(payload.uploads) > 8:
        raise HTTPException(status_code=400, detail="主推款最多上传 8 张图片")
    assets = await save_uploaded_objects_as_media_assets(payload.uploads)
    timestamp = now_iso()
    try:
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
            if task["status"] != "DRAFT":
                mark_generation_task_input_changed(db, task_id, timestamp, "hero_product_images_added")
    except Exception:
        cleanup_created_media_assets(assets)
        raise
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
    effective_user_instruction = user_instruction if user_instruction is not None else pending_generation_task_stage_instruction(task, "ai_styling")
    hero_product_info_context = build_hero_product_info_context(task)
    user_instruction_context = build_stage_user_instruction_context("ai_styling", effective_user_instruction)
    user_requirement_context = build_user_requirement_context(task, effective_user_instruction)
    input_version_id = create_generation_input_version(task_id, hero_product_info_context)
    with get_db() as db:
        timestamp = now_iso()
        if user_instruction is not None:
            persist_generation_task_stage_instruction(db, task_id, "ai_styling", effective_user_instruction, timestamp)
        db.execute("UPDATE generation_tasks SET status = 'STYLING_RUNNING', updated_at = ? WHERE id = ?", (timestamp, task_id))
    attempt_id = create_attempt("generation_styling", task_id, "gemini")
    styling_plan: dict[str, Any] | None = None
    try:
        input_row, candidate_snapshot, request_snapshot = build_ai_stylist_context(input_version_id)
        request_snapshot["invocation"] = {"mode": trigger, "target": "styling"}
        request_snapshot["hero_product_info"] = hero_product_info_context
        request_snapshot["ai_styling_user_instruction"] = user_instruction_context
        request_snapshot["user_requirement"] = user_requirement_context
        update_attempt_request_snapshot(attempt_id, request_snapshot)
        if generation_task_or_attempt_cancelled(task_id, attempt_id):
            finish_attempt(attempt_id, "CANCELLED")
            return {"item": get_generation_task_by_id(task_id)}
        styling_plan = await select_styling_with_gemini(input_row, candidate_snapshot, request_snapshot, attempt_id=attempt_id)
        persist_attempt_provider_progress(attempt_id, styling_plan)
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
            finish_attempt(attempt_id, "FAILED", response_snapshot=styling_plan, error_message=str(exc))
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
        supersede_generation_result_sets(db, task_id, timestamp)
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
    return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}


@app.post("/api/generation-tasks/{task_id}/shoot-script")
async def create_generation_shoot_script(task_id: int, request: Request, payload: GenerationStageInstructionRequest | None = None) -> dict[str, Any]:
    return await run_create_generation_shoot_script(
        task_id,
        include_debug=admin_debug_for_request(request),
        trigger="manual_step",
        user_instruction=payload.userInstruction if payload else None,
    )


async def run_create_generation_shoot_script(task_id: int, include_debug: bool = False, trigger: str = "manual_step", user_instruction: str | None = None) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    styling_id = task["active_styling_id"]
    if not styling_id or task["status"] not in {
        "STYLING_READY",
        "SCRIPT_READY",
        "PREVIEW_READY",
        "COMPLETED",
        "FINAL_PARTIAL",
        "FAILED",
        "CANCELLED",
    }:
        raise HTTPException(status_code=409, detail="请先完成 AI 智能选搭")
    input_version_id = create_generation_input_version(task_id, build_hero_product_info_context(task))
    timestamp = now_iso()
    effective_user_instruction = user_instruction if user_instruction is not None else pending_generation_task_stage_instruction(task, "shoot_script")
    user_instruction_context = build_stage_user_instruction_context("shoot_script", effective_user_instruction)
    with get_db() as db:
        if user_instruction is not None:
            persist_generation_task_stage_instruction(db, task_id, "shoot_script", effective_user_instruction, timestamp)
        transition = db.execute(
            """
            UPDATE generation_tasks
            SET status = 'SCRIPT_RUNNING', updated_at = ?
            WHERE id = ?
              AND active_styling_id = ?
              AND status IN ('STYLING_READY', 'SCRIPT_READY', 'PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'FAILED', 'CANCELLED')
            """,
            (timestamp, task_id, styling_id),
        )
        if int(transition.rowcount or 0) == 0:
            raise HTTPException(status_code=409, detail="拍摄剧本已在生成中，请勿重复提交")
        cursor = db.execute(
            """
            INSERT INTO generation_shoot_scripts
              (task_id, input_version_id, styling_id, status, script_json, summary_json,
               styling_risks_json, accessory_adjustments_json, user_instruction_json, created_at, updated_at)
            VALUES (?, ?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                input_version_id,
                styling_id,
                dumps({}),
                dumps({}),
                dumps([]),
                dumps([]),
                dumps(user_instruction_context),
                timestamp,
                timestamp,
            ),
        )
        shoot_script_id = int(cursor.lastrowid)
    attempt_id = create_attempt("generation_shoot_script", shoot_script_id, "gemini")
    with get_db() as db:
        db.execute("UPDATE generation_shoot_scripts SET active_attempt_id = ? WHERE id = ?", (attempt_id, shoot_script_id))
    try:
        request_snapshot = build_shoot_script_request_snapshot(task_id, int(styling_id), int(input_version_id), user_instruction_context)
        try:
            request_snapshot = attach_shoot_script_prompt_snapshot(request_snapshot)
        except RuntimeError:
            pass
        request_snapshot["invocation"] = {"mode": trigger, "target": "shoot_script"}
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
            return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}
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
            db.execute(
                """
                UPDATE generation_tasks
                SET auto_run_target = NULL, auto_run_requested_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now_iso(), task_id),
            )
        raise HTTPException(status_code=502, detail=f"拍摄剧本生成失败：{exc}") from exc
    if shoot_script_generation_cancelled(task_id, shoot_script_id, attempt_id):
        finish_attempt(attempt_id, "CANCELLED", error_message="拍摄剧本生成已取消")
        mark_shoot_script_generation_cancelled(shoot_script_id)
        return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}
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
            supersede_generation_result_sets(db, task_id, timestamp)
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
                    current_input_version_id = ?,
                    active_shoot_script_id = ?,
                    active_storyboard_preview_id = NULL,
                    active_preview_id = NULL,
                    active_result_set_id = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (input_version_id, shoot_script_id, timestamp, task_id),
            )
    if cancelled_after_provider_return:
        return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}
    if status_changed_after_provider_return:
        raise HTTPException(status_code=409, detail="拍摄剧本生成状态已变更，请刷新后重试")
    finish_attempt(attempt_id, "SUCCEEDED", response_snapshot=shoot_script)
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
    shoot_script_id = task["active_shoot_script_id"] if "active_shoot_script_id" in task.keys() else None
    if not styling_id or not shoot_script_id or (
        task["status"] not in {"SCRIPT_READY", "PREVIEW_READY", "COMPLETED", "FINAL_PARTIAL", "CANCELLED"}
        and not (task["status"] == "FAILED" and not task["active_storyboard_preview_id"])
    ):
        raise HTTPException(status_code=409, detail="请先生成拍摄剧本")
    input_version_id = create_generation_input_version(task_id, build_hero_product_info_context(task))
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET status = 'PREVIEW_RUNNING', updated_at = ? WHERE id = ?", (now_iso(), task_id))
    shot_plans = get_task_shot_plans_by_script(int(shoot_script_id))
    timestamp = now_iso()
    effective_user_instruction = user_instruction if user_instruction is not None else pending_generation_task_stage_instruction(task, "storyboard_preview")
    user_instruction_context = build_stage_user_instruction_context("storyboard_preview", effective_user_instruction)
    with get_db() as db:
        if user_instruction is not None:
            persist_generation_task_stage_instruction(db, task_id, "storyboard_preview", effective_user_instruction, timestamp)
        cursor = db.execute(
            """
            INSERT INTO generation_previews (task_id, styling_id, shoot_script_id, input_version_id, user_instruction_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?)
            """,
            (task_id, styling_id, shoot_script_id, input_version_id, dumps(user_instruction_context), timestamp, timestamp),
        )
        preview_id = int(cursor.lastrowid)
    attempt_id = create_attempt("generation_preview", preview_id, "image-2")
    lease_generation_preview(preview_id, attempt_id)
    schedule_background_generation(
        run_preview_generation(task_id, int(styling_id), int(shoot_script_id), shot_plans, preview_id, attempt_id, trigger=trigger),
        job_key=preview_job_key(preview_id),
    )
    return {"item": get_generation_task_by_id(task_id, include_debug=include_debug)}


async def run_preview_generation(
    task_id: int,
    styling_id: int,
    shoot_script_id: int,
    shot_plans: list[Any],
    preview_id: int,
    attempt_id: int,
    trigger: str = "manual_step",
) -> None:
    try:
        task_package = build_new_preview_task_package(task_id, styling_id, shoot_script_id, shot_plans, preview_id=preview_id)
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
        finish_attempt(attempt_id, "FAILED", response_snapshot=provider_response, error_message=str(exc), error_code=classify_attempt_error(exc))
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
async def auto_run_final_images(task_id: int, request: Request) -> dict[str, Any]:
    task = get_generation_task_row(task_id)
    if task["status"] in RUNNING_GENERATION_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="当前任务正在生成中，请先取消或等待完成")
    if task["status"] in {"CANCELLED", "ABANDONED"}:
        raise HTTPException(status_code=409, detail="当前任务已取消，不能一键成片")
    timestamp = now_iso()
    with get_db() as db:
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
    schedule_auto_run_for_task(task_id)
    return {"item": get_generation_task_by_id(task_id, include_debug=admin_debug_for_request(request))}


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
        or not task["active_shoot_script_id"]
    ):
        raise HTTPException(status_code=409, detail="请先生成分镜预览")
    preview_id = int(task["active_storyboard_preview_id"])
    styling_id = int(task["active_styling_id"])
    shoot_script_id = int(task["active_shoot_script_id"])
    with get_db() as db:
        preview_row = db.execute(
            "SELECT input_version_id FROM generation_previews WHERE id = ? AND task_id = ?",
            (preview_id, task_id),
        ).fetchone()
    if not preview_row or not preview_row["input_version_id"]:
        raise HTTPException(status_code=409, detail="请先生成分镜预览")
    preview_input_version_id = int(preview_row["input_version_id"])
    effective_user_instruction = user_instruction if user_instruction is not None else pending_generation_task_stage_instruction(task, "final_image")
    user_instruction_context = build_stage_user_instruction_context("final_image", effective_user_instruction)
    if continue_existing and task["active_result_set_id"]:
        image_ids = continue_active_final_result_set(
            task_id,
            int(task["active_result_set_id"]),
            preview_input_version_id,
            styling_id,
            shoot_script_id,
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
    input_version_id = create_generation_input_version(task_id, build_hero_product_info_context(task))
    timestamp = now_iso()
    with get_db() as db:
        if user_instruction is not None:
            persist_generation_task_stage_instruction(db, task_id, "final_image", effective_user_instruction, timestamp)
        db.execute("UPDATE generation_tasks SET status = 'FINAL_RUNNING', updated_at = ? WHERE id = ?", (timestamp, task_id))
        cursor = db.execute(
            """
            INSERT INTO generation_result_sets (task_id, input_version_id, styling_id, shoot_script_id, preview_id, user_instruction_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)
            """,
            (task_id, input_version_id, styling_id, shoot_script_id, preview_id, dumps(user_instruction_context), timestamp, timestamp),
        )
        result_set_id = int(cursor.lastrowid)
        supersede_generation_result_sets(db, task_id, timestamp, keep_result_set_id=result_set_id)
        db.execute(
            "UPDATE generation_tasks SET current_input_version_id = ?, active_result_set_id = ?, updated_at = ? WHERE id = ?",
            (input_version_id, result_set_id, timestamp, task_id),
        )
        shot_rows = db.execute(
            "SELECT * FROM generation_shot_plans WHERE shoot_script_id = ? AND sort_order <= 5 ORDER BY sort_order",
            (shoot_script_id,),
        ).fetchall()
        image_ids: list[int] = []
        for shot in shot_rows:
            image_cursor = db.execute(
                """
                INSERT INTO generation_result_images_new
                  (result_set_id, shot_plan_id, sort_order, user_instruction_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'QUEUED', ?, ?)
                """,
                (result_set_id, shot["id"], shot["sort_order"], dumps(user_instruction_context), timestamp, timestamp),
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
    input_version_id: int,
    styling_id: int,
    shoot_script_id: int,
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
        if (
            int(result_set["input_version_id"]) != input_version_id
            or int(result_set["styling_id"]) != styling_id
            or int(result_set["shoot_script_id"]) != shoot_script_id
            or int(result_set["preview_id"]) != preview_id
        ):
            return None
        existing_rows = db.execute(
            "SELECT * FROM generation_result_images_new WHERE result_set_id = ? ORDER BY sort_order",
            (result_set_id,),
        ).fetchall()
        existing_by_sort = {int(row["sort_order"]): row for row in existing_rows}
        image_ids: list[int] = []
        shot_rows = db.execute(
            "SELECT * FROM generation_shot_plans WHERE shoot_script_id = ? AND sort_order <= 5 ORDER BY sort_order",
            (shoot_script_id,),
        ).fetchall()
        for shot in shot_rows:
            sort_order = int(shot["sort_order"])
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
                        active_attempt_id = NULL,
                        locked_by = NULL,
                        locked_until = NULL,
                        user_instruction_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (dumps(user_instruction_context or {}), timestamp, int(existing["id"])),
                )
                image_ids.append(int(existing["id"]))
                continue
            cursor = db.execute(
                """
                INSERT INTO generation_result_images_new
                  (result_set_id, shot_plan_id, sort_order, user_instruction_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'QUEUED', ?, ?)
                """,
                (result_set_id, int(shot["id"]), sort_order, dumps(user_instruction_context or {}), timestamp, timestamp),
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
    await asyncio.gather(
        *(
            generate_single_final_image(
                task_id,
                result_set_id,
                image_id,
                attempt_id=resume_attempts_by_image_id.get(image_id),
                trigger=trigger,
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
    task_id = int(row["task_id"])
    result_set_id = int(row["result_set_id"])
    input_version_id = create_generation_input_version(task_id, build_hero_product_info_context(get_generation_task_row(task_id)))
    timestamp = now_iso()
    user_instruction_context = build_stage_user_instruction_context("final_image", effective_user_instruction)
    with get_db() as db:
        if payload and payload.userInstruction is not None:
            persist_generation_task_stage_instruction(db, task_id, "final_image", effective_user_instruction, timestamp)
        db.execute(
            """
            UPDATE generation_result_images_new
            SET status = 'QUEUED', active_attempt_id = NULL,
                locked_by = NULL, locked_until = NULL,
                user_instruction_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (dumps(user_instruction_context), timestamp, image_id),
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
        run_final_generation(task_id, result_set_id, [image_id], trigger="manual_step"),
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
def get_new_generation_record(task_id: int, request: Request, include_debug: bool = Query(False, alias="includeDebug")) -> dict[str, Any]:
    return {"item": get_generation_task_by_id(task_id, include_debug=require_admin_debug(request, include_debug))}


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
    # 302 + 短时私有缓存:5 分钟内重定向目标(presigned URL)保持稳定,
    # 浏览器才能对最终图片做缓存,避免每次进页面全量回源。
    # max-age 必须小于 presign 有效期(APP_S3_PRESIGN_EXPIRES_SECONDS,默认 900s)。
    # 部分浏览器不缓存 307,故使用 302。对象按 objectKey 不可变,无脏缓存风险。
    return RedirectResponse(
        url,
        status_code=302,
        headers={
            "Cache-Control": f"private, max-age={MEDIA_REDIRECT_CACHE_SECONDS}",
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


def build_hero_product_info_context(task: Any) -> dict[str, Any]:
    text = normalize_user_input_text(
        row_value(task, "hero_product_info_text", None)
        if row_value(task, "hero_product_info_text", None) is not None
        else row_value(task, "user_requirement_text", "")
    )
    return {
        "schema_version": "hero-product-info-v1",
        "info": text,
        "priority_policy": {
            "fact_role": "用户补充的商品事实和业务要求，可贯穿后续工作流。",
            "cannot_override": [
                "主推款图片可见商品事实",
                "搭配服装库 READY 候选事实",
                "数字模特身份契约",
                "Lily 品牌硬边界",
                "输出 schema",
                "安全规则",
            ],
            "conflict_handling": "与不可覆盖边界冲突时，以边界为准并说明处理。",
        },
    }


def build_stage_user_instruction_context(stage: str, instruction: str | None = None) -> dict[str, Any]:
    text = normalize_user_input_text(instruction)
    return {
        "schema_version": "stage-user-instruction-v1",
        "stage": stage,
        "instruction": text,
        "priority_policy": {
            "user_instruction_priority": "本步骤用户介入要求高于默认阶段偏好。",
            "cannot_override": [
                "主推款图片商品事实",
                "商品信息事实",
                "搭配服装库 READY 候选事实",
                "数字模特身份契约",
                "Lily 品牌硬边界",
                "输出 schema",
                "安全规则",
            ],
            "conflict_handling": "无法满足时必须说明冲突原因、未满足项和替代处理。",
        },
    }


def build_user_requirement_context(task: Any, styling_adjustment: str | None = None) -> dict[str, Any]:
    upload_requirement = normalize_user_input_text(row_value(task, "hero_product_info_text", None) or row_value(task, "user_requirement_text", ""))
    adjustment = normalize_user_input_text(styling_adjustment)
    combined_parts = [part for part in (upload_requirement, adjustment) if part]
    return {
        "schema_version": "user-requirement-v1",
        "upload_requirement": upload_requirement,
        "styling_adjustment": adjustment,
        "combined_requirement": "；".join(combined_parts),
        "priority_policy": {
            "user_requirement_priority": "用户要求高于默认搭配偏好和默认导演偏好。",
            "cannot_override": [
                "主推款图片商品事实",
                "搭配服装库 READY 候选事实",
                "数字模特身份契约",
                "Lily 品牌硬边界",
                "输出 schema",
                "安全规则",
            ],
            "conflict_handling": "无法满足时必须说明冲突原因、未满足项和替代处理。",
        },
    }


def has_user_requirement(context: dict[str, Any] | None) -> bool:
    return bool((context or {}).get("combined_requirement"))


def append_user_requirement_block(prompt: str, context: dict[str, Any] | None) -> str:
    if not has_user_requirement(context) or "【本次用户要求】" in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n【本次用户要求】\n{dumps(context)}\n"


def has_user_input(context: dict[str, Any] | None) -> bool:
    if not context:
        return False
    return bool(context.get("info") or context.get("instruction") or context.get("combined_requirement"))


def append_runtime_input_block(prompt: str, title: str, context: dict[str, Any] | None) -> str:
    if not has_user_input(context) or title in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n【{title}】\n{dumps(context)}\n"


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
    derivative = generate_image_derivatives(key, content)
    width = height = None
    with Image.open(BytesIO(content)) as image:
        width, height = image.size
    asset = {
        "objectKey": key,
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
        raise HTTPException(status_code=404, detail="搭配服装不存在")
    return serialize_styling_garment(row, include_images=True)


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
    for image in garment.get("images", [])[:6]:
        asset = image.get("asset") or {}
        image_input = gemini_vision_input(asset.get("objectKey"), asset.get("mimeType"))
        if image_input:
            images.append(image_input)
    try:
        text = await generate_gemini_json_text(prompt, images)
    except Exception as exc:
        finish_attempt(attempt_id, "FAILED", error_message=str(exc))
        raise
    try:
        raw = parse_lily_tagging_json(text)
    except Exception as exc:
        finish_attempt(attempt_id, "FAILED", {"raw_text": text}, error_message=f"Gemini 返回内容不是合法 JSON：{exc}")
        raise
    tagging_result = normalize_prompt_tagging_result(raw, garment)
    finish_attempt(attempt_id, "SUCCEEDED", {"raw_text": text, "raw_json": raw})
    return tagging_result


def build_prompt_tagging_request_snapshot(garment: dict[str, Any], prompt: str) -> dict[str, Any]:
    return {
        "schema_version": PROMPT_TAGGING_RESULT_VERSION,
        **gemini_request_meta(),
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


def build_prompt_tagging_prompt(garment: dict[str, Any] | None = None) -> str:
    return render_prompt_template("assembly.ai_tagging", {
        "styling_garment": dumps(styling_garment_prompt_context(garment or {})),
    })


def styling_garment_prompt_context(garment: dict[str, Any]) -> dict[str, Any]:
    images = garment.get("images") if isinstance(garment.get("images"), list) else []
    return {
        "schema_version": "styling-garment-tagging-context-v1",
        "code": garment.get("code"),
        "image_count": len(images),
        "image_inputs": [
            {
                "sort_order": image.get("sortOrder"),
                "role_hint": "primary" if index == 0 else "reference",
                "filename": ((image.get("asset") or {}).get("filename")) if isinstance(image, dict) else None,
                "mime_type": ((image.get("asset") or {}).get("mimeType")) if isinstance(image, dict) else None,
            }
            for index, image in enumerate(images[:6])
            if isinstance(image, dict)
        ],
    }


def fallback_prompt_tagging_result(garment: dict[str, Any]) -> dict[str, Any]:
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
        "tagger_model": str(source_meta.get("tagger_model") or ("fallback" if provider == "fallback" else effective_gemini_model())),
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


def create_generation_input_version(task_id: int, hero_product_info_context: dict[str, Any] | None = None) -> int:
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
              (task_id, version_number, name_snapshot, image_snapshot_json, model_snapshot_json, user_requirement_json, hero_product_info_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                version_number,
                task["name"],
                dumps(snapshot),
                dumps(model_snapshot),
                dumps(build_user_requirement_context(task)),
                dumps(hero_product_info_context or build_hero_product_info_context(task)),
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
    error_code: str | None = None,
) -> None:
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_attempts
            SET status = ?, completed_at = ?, failed_at = ?, cancelled_at = ?, response_snapshot_json = ?, output_asset_id = ?,
                error_code = CASE WHEN ? = 'SUCCEEDED' THEN NULL ELSE COALESCE(?, error_code) END,
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
    request_snapshot["candidate_retrieval"] = candidate_snapshot
    return input_row, candidate_snapshot, request_snapshot


async def select_styling_with_gemini(input_row: Any, candidate_snapshot: dict[str, Any], request_snapshot: dict[str, Any], attempt_id: int | None = None) -> dict[str, Any]:
    if not is_gemini_configured():
        raise RuntimeError(f"{gemini_not_configured_message()}AI Stylist 不能进入生产选搭。")
    if not request_snapshot.get("prompt"):
        request_snapshot = attach_ai_stylist_prompt_snapshot(request_snapshot, candidate_snapshot)
    if attempt_id:
        update_attempt_request_snapshot(attempt_id, request_snapshot)
    prompt = str(request_snapshot.get("prompt") or "")
    images: list[dict[str, Any]] = []
    for item in loads(input_row["image_snapshot_json"], [])[:8]:
        asset = (item.get("asset") or {})
        image_input = gemini_vision_input(asset.get("objectKey"), asset.get("mimeType"))
        if image_input:
            images.append(image_input)
    try:
        text = await generate_gemini_json_text(
            prompt,
            images,
            system_instruction=str(request_snapshot.get("systemInstruction") or AI_STYLIST_SYSTEM_INSTRUCTION),
        )
        raw = parse_gemini_json_object(text)
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
            "hero_product_info": dumps(request_snapshot.get("hero_product_info") or {}),
            "ai_styling_user_instruction": dumps(request_snapshot.get("ai_styling_user_instruction") or {}),
            "user_requirement": dumps(request_snapshot.get("user_requirement") or {}),
        },
    )
    prompt = append_runtime_input_block(prompt, "runtime.hero_product_info", request_snapshot.get("hero_product_info") if isinstance(request_snapshot.get("hero_product_info"), dict) else {})
    prompt = append_runtime_input_block(prompt, "runtime.ai_styling_user_instruction", request_snapshot.get("ai_styling_user_instruction") if isinstance(request_snapshot.get("ai_styling_user_instruction"), dict) else {})
    return {**request_snapshot, "promptConfigKey": AI_STYLING_PROMPT_CONFIG_KEY, "prompt": prompt}


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


def build_accessory_boundary() -> dict[str, Any]:
    return {
        "allow_empty": True,
        "default": ["低存在感简洁鞋，可省略包和首饰"],
        "avoid_default": ["夸张首饰", "传统公文包模板", "遮挡主推款卖点的配饰"],
        "bag_rule": "包可省略；如出现，保持低存在感且不得遮挡主推款。",
        "jewelry_rule": "首饰可省略；如出现，只作轻微风格辅助，不做特写。",
    }


def build_phase_one_brand_style_evidence() -> list[dict[str, Any]]:
    return []


def build_ai_stylist_request_snapshot(input_row: Any, candidate_snapshot: dict[str, Any]) -> dict[str, Any]:
    user_requirement = loads(row_value(input_row, "user_requirement_json"), build_user_requirement_context({}))
    hero_product_info = loads(row_value(input_row, "hero_product_info_json"), build_hero_product_info_context({}))
    return {
        "schema_version": "ai-stylist-request-v1",
        **gemini_request_meta(),
        "systemInstruction": AI_STYLIST_SYSTEM_INSTRUCTION,
        "input_version": {
            "id": input_row["id"],
            "taskId": input_row["task_id"],
            "nameSnapshot": input_row["name_snapshot"],
            "imageSnapshot": loads(input_row["image_snapshot_json"], []),
            "heroProductInfo": hero_product_info,
            "userRequirement": user_requirement,
        },
        "candidate_retrieval": candidate_snapshot,
        "hero_product_info": hero_product_info,
        "ai_styling_user_instruction": {},
        "user_requirement": user_requirement,
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


def shoot_script_result_for_prompt(shoot_script: dict[str, Any]) -> dict[str, Any]:
    script = shoot_script if isinstance(shoot_script, dict) else {}
    shots = script.get("shots") if isinstance(script.get("shots"), list) else []
    return {
        "schema_version": "shoot-script-storyboard-context-v1",
        "global_directing": strip_internal_prompt_fields(script.get("global_directing") if isinstance(script.get("global_directing"), dict) else {}),
        "styling_risks": strip_internal_prompt_fields(script.get("styling_risks") if isinstance(script.get("styling_risks"), list) else []),
        "resolved_accessories": strip_internal_prompt_fields(script.get("resolved_accessories") if isinstance(script.get("resolved_accessories"), list) else []),
        "shots": [
            strip_internal_prompt_fields({
                "shot_index": shot.get("shot_index"),
                "shot_role": shot.get("shot_role"),
                "shot_role_label": shot.get("shot_role_label"),
                "framing": shot.get("framing"),
                "crop_range": shot.get("crop_range"),
                "model_scale": shot.get("model_scale"),
                "camera_angle": shot.get("camera_angle"),
                "body_position": shot.get("body_position"),
                "pose": shot.get("pose"),
                "movement": shot.get("movement"),
                "main_focus": shot.get("main_focus"),
                "visible_garments": shot.get("visible_garments"),
                "forbidden_occlusion_zones": shot.get("forbidden_occlusion_zones"),
                "composition_notes": shot.get("composition_notes"),
                "accessory_instructions": shot.get("accessory_instructions"),
                "negative_constraints": shot.get("negative_constraints"),
            })
            for shot in shots[:6]
            if isinstance(shot, dict)
        ],
        "user_visible_summary": strip_internal_prompt_fields(script.get("user_visible_summary") if isinstance(script.get("user_visible_summary"), dict) else {}),
    }


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
        "schema_version": "final-image-shot-context-v1",
        "preview_grid_cell": shot_index,
        "styling": {
            "hero_understanding": styling_result_for_prompt(styling_plan).get("hero_understanding") or {},
            "selected_outfit": styling_result_for_prompt(styling_plan).get("selected_outfit") or {},
        },
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


def build_shoot_script_request_snapshot(task_id: int, styling_id: int, input_version_id: int, user_instruction_context: dict[str, Any] | None = None) -> dict[str, Any]:
    styling_plan = get_styling_plan_by_id(styling_id)
    with get_db() as db:
        input_row = db.execute("SELECT * FROM generation_task_input_versions WHERE id = ?", (input_version_id,)).fetchone()
        styling_row = db.execute("SELECT * FROM generation_stylings WHERE id = ?", (styling_id,)).fetchone()
    selected_snapshot = loads(styling_row["selected_material_snapshot_json"], []) if styling_row else []
    selected_material_image_refs = selected_material_image_refs_for_request(selected_snapshot)
    candidate_snapshot = loads(styling_row["candidate_retrieval_snapshot_json"], {}) if styling_row and "candidate_retrieval_snapshot_json" in styling_row.keys() else {}
    user_requirement = loads(
        row_value(styling_row, "user_requirement_json"),
        loads(row_value(input_row, "user_requirement_json"), build_user_requirement_context({})),
    )
    hero_product_info = loads(row_value(input_row, "hero_product_info_json"), build_hero_product_info_context({}))
    shoot_script_user_instruction = user_instruction_context or build_stage_user_instruction_context("shoot_script")
    return {
        "schema_version": "shoot-script-request-v1",
        **gemini_request_meta(),
        "systemInstruction": AI_DIRECTOR_SYSTEM_INSTRUCTION,
        "task_id": task_id,
        "input_version": {
            "id": input_version_id,
            "name_snapshot": input_row["name_snapshot"] if input_row else "",
            "image_snapshot": loads(input_row["image_snapshot_json"], []) if input_row else [],
            "model_snapshot": loads(input_row["model_snapshot_json"], {}) if input_row and "model_snapshot_json" in input_row.keys() else {},
            "hero_product_info": hero_product_info,
        },
        "styling_id": styling_id,
        "hero_product_info": hero_product_info,
        "shoot_script_user_instruction": shoot_script_user_instruction,
        "user_requirement": user_requirement,
        "styling_plan": styling_plan,
        "styling_result": styling_result_for_prompt(styling_plan, selected_snapshot),
        "selected_material_snapshot": selected_material_image_refs,
        "base_shot_rules": [],
        "accessory_boundary": candidate_snapshot.get("accessory_boundary") if isinstance(candidate_snapshot, dict) else build_accessory_boundary(),
        "output_schema": "shoot-script-v1",
    }


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


async def generate_shoot_script_with_gemini(request_snapshot: dict[str, Any]) -> dict[str, Any]:
    if not is_gemini_configured():
        raise RuntimeError(f"{gemini_not_configured_message()}拍摄剧本不能进入生产生成。")
    if not request_snapshot.get("prompt"):
        request_snapshot = attach_shoot_script_prompt_snapshot(request_snapshot)
    prompt = str(request_snapshot.get("prompt") or "")
    image_inputs: list[dict[str, Any]] = []
    model_snapshot = ((request_snapshot.get("input_version") or {}).get("model_snapshot") or {}) if isinstance(request_snapshot.get("input_version"), dict) else {}
    for item in (model_snapshot.get("imageInputs") if isinstance(model_snapshot, dict) else []) or []:
        object_key = item.get("objectKey") if isinstance(item, dict) else None
        image_input = gemini_vision_input(object_key, "image/png")
        if image_input:
            image_inputs.append(image_input)
    for item in ((request_snapshot.get("input_version") or {}).get("image_snapshot") or [])[:6]:
        asset = item.get("asset") if isinstance(item, dict) else {}
        image_input = gemini_vision_input(asset.get("objectKey") if isinstance(asset, dict) else None, asset.get("mimeType") if isinstance(asset, dict) else None)
        if image_input:
            image_inputs.append(image_input)
    for material in request_snapshot.get("selected_material_snapshot", [])[:4]:
        images = material.get("images") if isinstance(material, dict) else []
        first = images[0] if images else {}
        asset = first.get("asset") if isinstance(first, dict) else {}
        image_input = gemini_vision_input(asset.get("objectKey") if isinstance(asset, dict) else None, asset.get("mimeType") if isinstance(asset, dict) else None)
        if image_input:
            image_inputs.append(image_input)
    try:
        text = await generate_gemini_json_text(
            prompt,
            image_inputs,
            system_instruction=str(request_snapshot.get("systemInstruction") or AI_DIRECTOR_SYSTEM_INSTRUCTION),
        )
        return parse_gemini_json_object(text)
    except Exception as exc:
        raise RuntimeError(f"Gemini 拍摄剧本调用失败：{exc}") from exc


def attach_shoot_script_prompt_snapshot(request_snapshot: dict[str, Any]) -> dict[str, Any]:
    input_version = request_snapshot.get("input_version") if isinstance(request_snapshot.get("input_version"), dict) else {}
    model_snapshot = input_version.get("model_snapshot") if isinstance(input_version.get("model_snapshot"), dict) else {}
    model_identity_context = model_identity_context_for_prompt(model_snapshot)
    prompt = render_prompt_template(
        SHOOT_SCRIPT_PROMPT_CONFIG_KEY,
        {
            "styling_result": dumps(request_snapshot.get("styling_result") or styling_result_for_prompt(request_snapshot.get("styling_plan") if isinstance(request_snapshot.get("styling_plan"), dict) else {})),
            "hero_product_info": dumps(request_snapshot.get("hero_product_info") or {}),
            "shoot_script_user_instruction": dumps(request_snapshot.get("shoot_script_user_instruction") or {}),
            "user_requirement": dumps(request_snapshot.get("user_requirement") or {}),
            "model_identity_context": dumps(model_identity_context),
        },
    )
    prompt = append_runtime_input_block(prompt, "runtime.hero_product_info", request_snapshot.get("hero_product_info") if isinstance(request_snapshot.get("hero_product_info"), dict) else {})
    prompt = append_runtime_input_block(prompt, "runtime.shoot_script_user_instruction", request_snapshot.get("shoot_script_user_instruction") if isinstance(request_snapshot.get("shoot_script_user_instruction"), dict) else {})
    prompt = append_model_identity_contract(prompt, model_identity_context)
    return {**request_snapshot, "promptConfigKey": SHOOT_SCRIPT_PROMPT_CONFIG_KEY, "prompt": prompt}


def normalize_shoot_script(raw: dict[str, Any], request_snapshot: dict[str, Any]) -> dict[str, Any]:
    styling_plan = request_snapshot.get("styling_plan") if isinstance(request_snapshot.get("styling_plan"), dict) else {}
    base_plans = request_snapshot.get("base_shot_rules") if isinstance(request_snapshot.get("base_shot_rules"), list) else []
    hero = styling_plan.get("hero_understanding") if isinstance(styling_plan.get("hero_understanding"), dict) else {}
    source = raw if isinstance(raw, dict) else {}
    input_version = request_snapshot.get("input_version") if isinstance(request_snapshot.get("input_version"), dict) else {}
    model_snapshot = input_version.get("model_snapshot") if isinstance(input_version.get("model_snapshot"), dict) else {}
    model_identity_context = model_identity_context_for_prompt(model_snapshot)
    global_directing = source.get("global_directing") if isinstance(source.get("global_directing"), dict) else {}
    fallback_consistency_rules: list[str] = []
    fallback_product_fidelity_rules: list[str] = []
    fallback_negative_constraints: list[str] = []
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
        "model_identity_contract": model_identity_context.get("identity_contract", {}),
        "global_directing": {
            "rules": normalize_shoot_script_global_rules(global_directing.get("rules")),
            "brand_tone": str(global_directing.get("brand_tone") or ""),
            "background": str(global_directing.get("background") or ""),
            "lighting": str(global_directing.get("lighting") or ""),
            "model_direction": str(global_directing.get("model_direction") or ""),
            "hair_makeup_boundary": str(model_identity_context.get("hair_makeup_boundary") or global_directing.get("hair_makeup_boundary") or ""),
            "accessory_boundary": str(global_directing.get("accessory_boundary") or ""),
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


def normalize_shoot_script_global_rules(value: Any) -> list[dict[str, str]]:
    if isinstance(value, list):
        rules = [
            {"label": str(item.get("label") or ""), "value": str(item.get("value") or "")}
            for item in value
            if isinstance(item, dict) and str(item.get("label") or "").strip() and str(item.get("value") or "").strip()
        ]
        if rules:
            return rules[:40]
    return []


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
        "acceptance_criteria": ensure_string_list(source.get("acceptance_criteria") or source.get("acceptanceCriteria")) or ["主推款品类、颜色、版型和长度清楚。", "搭配服装服务主推款，不抢视觉中心。"],
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
    return build_builtin_category_shot_template(hero_category)


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


def serialize_new_shot_plan(row: Any) -> dict[str, Any]:
    plan = loads(row["plan_json"], {})
    return {"id": row["id"], "sortOrder": row["sort_order"], "shotType": row["shot_type"], "plan": plan}


def model_identity_context_for_prompt(model_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    snapshot = model_snapshot if isinstance(model_snapshot, dict) else {}
    profile = snapshot.get("profile") if isinstance(snapshot.get("profile"), dict) else {}
    image_inputs = snapshot.get("imageInputs") if isinstance(snapshot.get("imageInputs"), list) else []
    contract = {
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
    return {
        "schema_version": "model-identity-context-v1",
        "identity_contract": contract,
        "hair_makeup_boundary": (
            "以数字模特参考图为唯一模特身份事实源；保持同一人的脸型、五官、头型、发长、发型轮廓、刘海/碎发形态、肤色和气质。"
            "不得根据品牌妆发想象改成低马尾、盘发、长发、浓妆或任何参考图中不存在的造型。"
        ),
    }


def append_model_identity_contract(prompt: str, model_identity_context: dict[str, Any]) -> str:
    return (
        prompt.rstrip()
        + "\n\n【强制模特身份契约】\n"
        + dumps(model_identity_context)
        + "\n模特身份契约优先级高于任何品牌妆发、姿态或剧本想象；只能改变服装、姿势、机位和光线，不得改变模特本人身份、脸型、五官、头型、发长、发型轮廓和刘海形态。\n"
    )


def image_reference_manifest_for_prompt(image_inputs: list[dict[str, Any]]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(image_inputs, start=1):
        role = str(item.get("role") or "")
        fact_source = {
            "model_identity": "模特身份事实源，最高身份优先级",
            "hero_garment": "主推款商品事实源",
            "styling_garment": "搭配服装商品事实源",
            "preview_grid": "分镜姿势、构图、景别、背景和光线参考，不是模特或商品事实源",
        }.get(role, "辅助参考图")
        entries.append(
            {
                "reference_index": index,
                "role": role,
                "fact_source": fact_source,
                "filename": str(item.get("filename") or ""),
                "objectKey": str(item.get("objectKey") or ""),
                "angle": str(item.get("angle") or ""),
            }
        )
    return {
        "schema_version": "image-reference-manifest-v1",
        "priority_rules": [
            "model_identity references define the person identity and override storyboard or style text",
            "preview_grid is composition-only and must never override model_identity, hero_garment, or styling_garment facts",
            "hero_garment and styling_garment references define product facts",
        ],
        "references": entries,
    }


def append_image_reference_manifest(prompt: str, image_inputs: list[dict[str, Any]]) -> str:
    return (
        prompt.rstrip()
        + "\n\n【输入参考图角色清单】\n"
        + dumps(image_reference_manifest_for_prompt(image_inputs))
        + "\n必须按 reference_index 理解 provider reference_images 的顺序。模特身份参考图不是风格参考，而是人物身份事实源。"
        + "不得根据拍摄剧本、分镜预览或品牌妆发文字改变模特脸型、五官、头型、发长、发型轮廓、刘海/碎发形态、肤色和气质。\n"
    )


def build_new_preview_task_package(
    task_id: int,
    styling_id: int,
    shoot_script_id: int,
    shot_plans: list[dict[str, Any]],
    preview_id: int | None = None,
) -> dict[str, Any]:
    styling_plan = get_styling_plan_by_id(styling_id)
    shoot_script = get_shoot_script_json_by_id(shoot_script_id)
    with get_db() as db:
        if preview_id is not None:
            preview_row = db.execute(
                """
                SELECT input_version_id, user_instruction_json
                FROM generation_previews
                WHERE id = ? AND task_id = ? AND styling_id = ? AND shoot_script_id = ?
                """,
                (preview_id, task_id, styling_id, shoot_script_id),
            ).fetchone()
        else:
            preview_row = db.execute(
                """
                SELECT input_version_id, user_instruction_json
                FROM generation_previews
                WHERE task_id = ? AND styling_id = ? AND shoot_script_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (task_id, styling_id, shoot_script_id),
            ).fetchone()
        input_version_id = int(preview_row["input_version_id"]) if preview_row and preview_row["input_version_id"] else None
        if input_version_id is None:
            styling_row = db.execute("SELECT input_version_id FROM generation_stylings WHERE id = ?", (styling_id,)).fetchone()
            input_version_id = int(styling_row["input_version_id"]) if styling_row and styling_row["input_version_id"] else None
        input_row = db.execute(
            "SELECT * FROM generation_task_input_versions WHERE id = ?",
            (input_version_id,),
        ).fetchone() if input_version_id is not None else None
    image_inputs = build_new_image_inputs(task_id, styling_id, include_preview=False, input_version_id=input_version_id)
    hero_product_info = loads(row_value(input_row, "hero_product_info_json"), build_hero_product_info_context({}))
    storyboard_preview_user_instruction = loads(row_value(preview_row, "user_instruction_json"), build_stage_user_instruction_context("storyboard_preview"))
    return {
        "generation": {"stage": "preview_grid"},
        "prompt": build_new_preview_prompt(shot_plans, styling_plan, shoot_script, image_inputs, hero_product_info, storyboard_preview_user_instruction),
        "imageInputs": image_inputs,
        "inputVersionId": input_version_id,
        "heroProductInfo": hero_product_info,
        "storyboardPreviewUserInstruction": storyboard_preview_user_instruction,
        "stylingPlan": styling_plan,
        "shootScript": shoot_script,
        "shotPlans": shot_plans,
    }


def build_new_preview_prompt(
    shot_plans: list[dict[str, Any]],
    styling_plan: dict[str, Any],
    shoot_script: dict[str, Any],
    image_inputs: list[dict[str, Any]] | None = None,
    hero_product_info: dict[str, Any] | None = None,
    storyboard_preview_user_instruction: dict[str, Any] | None = None,
) -> str:
    prompt = render_prompt_template(
        "assembly.storyboard_preview",
        {
            "shoot_script_result": dumps(shoot_script_result_for_prompt(shoot_script)),
            "image_reference_manifest": dumps(image_reference_manifest_for_prompt(image_inputs or [])),
            "hero_product_info": dumps(hero_product_info or {}),
            "storyboard_preview_user_instruction": dumps(storyboard_preview_user_instruction or {}),
        },
    )
    prompt = append_runtime_input_block(prompt, "runtime.hero_product_info", hero_product_info or {})
    prompt = append_runtime_input_block(prompt, "runtime.storyboard_preview_user_instruction", storyboard_preview_user_instruction or {})
    return append_image_reference_manifest(prompt, image_inputs or [])


def build_new_final_task_package(task_id: int, result_image_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT gri.*, gsp.plan_json, gp.preview_asset_id, grs.styling_id, grs.shoot_script_id,
                   grs.input_version_id, grs.user_instruction_json AS result_set_user_instruction_json
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
    image_inputs = build_new_image_inputs(
        task_id,
        int(row["styling_id"]),
        include_preview=True,
        preview_asset_id=row["preview_asset_id"],
        input_version_id=int(row["input_version_id"]),
    )
    with get_db() as db:
        input_row = db.execute("SELECT hero_product_info_json FROM generation_task_input_versions WHERE id = ?", (row["input_version_id"],)).fetchone()
    hero_product_info = loads(row_value(input_row, "hero_product_info_json"), build_hero_product_info_context({}))
    final_image_user_instruction = loads(
        row_value(row, "user_instruction_json"),
        loads(row_value(row, "result_set_user_instruction_json"), build_stage_user_instruction_context("final_image")),
    )
    return {
        "generation": {"stage": "final_image"},
        "prompt": build_new_final_prompt(int(row["sort_order"]), shot_plan, styling_plan, shoot_script, image_inputs, hero_product_info, final_image_user_instruction),
        "imageInputs": image_inputs,
        "heroProductInfo": hero_product_info,
        "finalImageUserInstruction": final_image_user_instruction,
        "stylingPlan": styling_plan,
        "shootScript": shoot_script,
        "shotPlan": shot_plan,
    }


def build_new_final_prompt(
    shot_index: int,
    shot_plan: dict[str, Any],
    styling_plan: dict[str, Any],
    shoot_script: dict[str, Any],
    image_inputs: list[dict[str, Any]] | None = None,
    hero_product_info: dict[str, Any] | None = None,
    final_image_user_instruction: dict[str, Any] | None = None,
) -> str:
    prompt = render_prompt_template(
        "assembly.final_image",
        {
            "current_shot_context": dumps(current_shot_context_for_prompt(shot_index, shot_plan, styling_plan, shoot_script)),
            "image_reference_manifest": dumps(image_reference_manifest_for_prompt(image_inputs or [])),
            "hero_product_info": dumps(hero_product_info or {}),
            "final_image_user_instruction": dumps(final_image_user_instruction or {}),
        },
    )
    prompt = append_runtime_input_block(prompt, "runtime.hero_product_info", hero_product_info or {})
    prompt = append_runtime_input_block(prompt, "runtime.final_image_user_instruction", final_image_user_instruction or {})
    return append_image_reference_manifest(prompt, image_inputs or [])


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


def build_new_image_inputs(
    task_id: int,
    styling_id: int,
    include_preview: bool,
    preview_asset_id: int | None = None,
    input_version_id: int | None = None,
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
    model_inputs = model_snapshot.get("imageInputs", []) if isinstance(model_snapshot, dict) else []
    if not model_inputs:
        model_inputs = get_default_model_image_inputs(limit=2)
    inputs.extend({**item, "role": "model_identity"} for item in model_inputs[:2] if isinstance(item, dict))
    if include_preview and preview_asset_id:
        asset = get_media_asset_by_id(preview_asset_id)
        if asset:
            inputs.append(image_input_from_asset(asset, "preview_grid"))
    hero_snapshot = loads(input_version["image_snapshot_json"], []) if input_version else []
    inputs.extend(image_input_from_asset(item.get("asset"), "hero_garment") for item in hero_snapshot[:3])
    selected_materials = loads(styling["selected_material_snapshot_json"], []) if styling else []
    remaining = max(0, 10 - len(inputs))
    for garment in selected_materials:
        if remaining <= 0:
            break
        images = garment.get("images", [])
        if images:
            inputs.append(image_input_from_asset(images[0]["asset"], "styling_garment"))
            remaining -= 1
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
            if name not in rows:
                diagnostics["missingPlaceholders"].append(name)
                return f"[[缺失分段:{name}]]"
            if name in rows and str(rows[name]["group_type"] or "") != "segment":
                diagnostics["missingPlaceholders"].append(name)
                return f"[[缺失分段:{name}]]"
            if name not in diagnostics["usedSegments"]:
                diagnostics["usedSegments"].append(name)
            segment_content = prompt_config_content(name, rows) or ""
            if not segment_content.strip():
                diagnostics["missingPlaceholders"].append(f"{name} (空内容)")
            return render_text(segment_content, [*stack, name])
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
    elif not source.strip():
        diagnostics["missingPlaceholders"].append(f"{key} (空内容)")
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
        "styling_garment": dumps({"code": "LOOK-A", "image_count": 3, "image_roles": ["primary", "reference", "reference"]}),
        "candidate_retrieval": dumps([{"code": "LOOK-A", "ai_tags": {"一级品类": ["上装"], "色彩": ["白色"], "版型": ["合体"], "风格语义": "轻量上装服务主推裤装。"}}]),
        "hero_product_info": dumps({"info": "夏季主推裤装，重点展示腰头和裤脚。"}),
        "ai_styling_user_instruction": dumps({"stage": "ai_styling", "instruction": "不要厚重外套，优先轻盈通勤。"}),
        "shoot_script_user_instruction": dumps({"stage": "shoot_script", "instruction": "腰线相关镜头要更明确。"}),
        "storyboard_preview_user_instruction": dumps({"stage": "storyboard_preview", "instruction": "分镜姿态更舒展。"}),
        "final_image_user_instruction": dumps({"stage": "final_image", "instruction": "成片整体更干净，减少配饰存在感。"}),
        "user_requirement": dumps({"combined_requirement": "重点展示腰线，避免厚重外套。", "priority_policy": {"conflict_handling": "无法满足时说明原因。"}}),
        "styling_result": dumps({"hero_understanding": {"category": "裤装"}, "selected_material_codes": ["LOOK-A"], "reason_summary": "轻量上装服务主推裤装。"}),
        "model_identity_context": dumps({"identity_contract": {"model_id": "Face-01", "must_preserve": ["same face", "same hair"]}}),
        "image_reference_manifest": dumps({"references": [{"reference_index": 1, "role": "model_identity"}, {"reference_index": 2, "role": "hero_garment"}]}),
        "shoot_script_result": dumps({"global_directing": {"background": "浅灰棚拍"}, "shots": [{"shot_index": 1, "shot_role": "hero_full_body", "main_focus": "主推款整体比例"}]}),
        "current_shot_context": dumps({"preview_grid_cell": 1, "current_shot": {"shot_role": "hero_full_body", "main_focus": "主推款整体比例"}, "styling": {"selected_material_codes": ["LOOK-A"]}}),
    }


def available_prompt_placeholders() -> list[dict[str, str]]:
    return [
        {"name": "segment.brand_info", "scope": "segment", "description": "品牌信息分段 Prompt"},
        {"name": "runtime.styling_garment", "scope": "runtime", "description": "AI 打标使用的当前搭配服装 code 和图片输入摘要"},
        {"name": "runtime.candidate_retrieval", "scope": "runtime", "description": "搭配服装库 READY 服饰的全量 AI 标签事实卡"},
        {"name": "runtime.hero_product_info", "scope": "runtime", "description": "跟随主推款输入版本冻结的商品信息和业务事实补充"},
        {"name": "runtime.ai_styling_user_instruction", "scope": "runtime", "description": "AI 智能选搭本次执行的用户介入要求"},
        {"name": "runtime.shoot_script_user_instruction", "scope": "runtime", "description": "拍摄剧本本次执行的用户介入要求"},
        {"name": "runtime.storyboard_preview_user_instruction", "scope": "runtime", "description": "分镜预览本次执行的用户介入要求"},
        {"name": "runtime.final_image_user_instruction", "scope": "runtime", "description": "最终成片或单张重生本次执行的用户介入要求"},
        {"name": "runtime.styling_result", "scope": "runtime", "description": "AI 选搭导演上下文，使用搭配服装 code 标识"},
        {"name": "runtime.model_identity_context", "scope": "runtime", "description": "AI Director 使用的数字模特身份契约和参考图上下文"},
        {"name": "runtime.image_reference_manifest", "scope": "runtime", "description": "Image-2 输入参考图序号、角色和事实源优先级"},
        {"name": "runtime.shoot_script_result", "scope": "runtime", "description": "拍摄剧本结果，用于分镜预览"},
        {"name": "runtime.current_shot_context", "scope": "runtime", "description": "当前成片单镜头所需的选搭、剧本和分镜信息"},
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


def maybe_schedule_auto_run_for_task(task_id: int) -> None:
    row = get_generation_task_row(task_id)
    if row_value(row, "auto_run_target") == AUTO_RUN_TARGET_FINAL and row["status"] not in RUNNING_GENERATION_TASK_STATUSES:
        schedule_auto_run_for_task(task_id)


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
            if task["status"] in {"CANCELLED", "ABANDONED"}:
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
            if step == "shoot_script":
                await run_create_generation_shoot_script(task_id, trigger="auto_run")
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
    if not task["active_styling_id"]:
        return "styling"
    if not row_value(task, "active_shoot_script_id"):
        return "shoot_script"
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
        auto_rows = db.execute(
            """
            SELECT id FROM generation_tasks
            WHERE deleted_at IS NULL
              AND auto_run_target = ?
              AND status NOT IN ('STYLING_RUNNING', 'SCRIPT_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING', 'CANCELLED', 'ABANDONED')
            ORDER BY updated_at, id
            LIMIT 5
            """,
            (AUTO_RUN_TARGET_FINAL,),
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
                SELECT gri.id, gri.active_attempt_id FROM generation_result_images_new gri
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
        maybe_schedule_auto_run_for_task(int(row["task_id"]))

    for row in auto_rows:
        key = auto_run_job_key(int(row["id"]))
        if key in active_background_generation_jobs:
            continue
        schedule_auto_run_for_task(int(row["id"]))


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


async def generate_image_asset(task_package: dict[str, Any], output_key: str, attempt_id: int | None = None) -> tuple[int, dict[str, Any]]:
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
    if payload is None:
        progress.begin(PHASE_PREPARE)
        image_input_total = len(task_package.get("imageInputs") or [])
        progress.begin(PHASE_UPLOAD, completed=0, total=image_input_total)

        def report_upload(completed: int, total: int) -> None:
            progress.update(PHASE_UPLOAD, completed=completed, total=total)

        payload = await asyncio.to_thread(run_image_provider_submit, task_package, output_key, report_upload)
        persist_attempt_provider_progress(attempt_id, payload)

    if payload.get("status") != "SUCCEEDED" or not payload.get("outputPath"):
        if payload.get("status") not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            payload = await poll_provider_until_terminal(payload, output_key, attempt_id, progress)
        if payload.get("status") != "SUCCEEDED":
            error_code = ERROR_PROVIDER_FAILED if payload.get("status") in {"FAILED", "CANCELLED"} else ERROR_PROVIDER_TIMEOUT
            raise ImageProviderError(
                f"图片生成未成功，providerRequestId={payload.get('providerRequestId')}: {payload}",
                payload,
                error_code=error_code,
            )
        if not payload.get("outputPath"):
            payload = await deliver_with_retries(payload, output_key, attempt_id, progress)
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
            consecutive_errors += 1
            current_phase = PHASE_GENERATE if progress.snapshot.get("current") == PHASE_GENERATE else PHASE_QUEUE
            progress.update(current_phase, statusCheckErrors=consecutive_errors, lastStatusCheckError=str(exc)[:500])
            continue
        consecutive_errors = 0
        payload = {**payload, **latest}
        persist_attempt_provider_progress(attempt_id, payload)
        provider_status = str(payload.get("providerStatus") or "").upper()
        if provider_status in {"IN_PROGRESS", "PROCESSING", "RUNNING"}:
            progress.begin(PHASE_GENERATE)
        if payload.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
    return payload


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
) -> int:
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
        task_package["invocation"] = {"mode": trigger, "target": "final_image"}
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
                final_image_generation_state_sql(),
                (image_id,),
            ).fetchone()
            if final_image_generation_cancelled(current, result_set_id):
                finish_attempt(attempt_id, "CANCELLED", response_snapshot=provider_response, error_message=str(exc))
                persist_attempt_provider_request_id(attempt_id, provider_response)
                return 0
        finish_attempt(attempt_id, "FAILED", response_snapshot=provider_response, error_message=str(exc), error_code=classify_attempt_error(exc))
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
        "heroProductInfo": row_value(row, "hero_product_info_text") or row_value(row, "user_requirement_text", ""),
        "userRequirement": row_value(row, "user_requirement_text", ""),
        "stageUserInputs": {
            "aiStyling": row_value(row, "ai_styling_user_instruction_text", ""),
            "shootScript": row_value(row, "shoot_script_user_instruction_text", ""),
            "storyboardPreview": row_value(row, "storyboard_preview_user_instruction_text", ""),
            "finalImage": row_value(row, "final_image_user_instruction_text", ""),
        },
        "currentInputVersionId": row["current_input_version_id"],
        "activeStylingId": row["active_styling_id"],
        "activeShootScriptId": row["active_shoot_script_id"] if "active_shoot_script_id" in row.keys() else None,
        "activePreviewId": row["active_preview_id"],
        "activeStoryboardPreviewId": row["active_storyboard_preview_id"] if "active_storyboard_preview_id" in row.keys() else row["active_preview_id"],
        "activeResultSetId": row["active_result_set_id"],
        "autoRunTarget": row_value(row, "auto_run_target"),
        "autoRunRequestedAt": row_value(row, "auto_run_requested_at"),
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
        "heroProductInfo": loads(row_value(row, "hero_product_info_json"), build_hero_product_info_context({})),
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
    if not include_debug:
        script = strip_debug_response_fields(script)
    item = {
        "id": row["id"],
        "taskId": row["task_id"],
        "inputVersionId": row["input_version_id"],
        "stylingId": row["styling_id"],
        "status": row["status"],
        "script": script,
        "summary": loads(row["summary_json"], {}) if "summary_json" in row.keys() else script.get("user_visible_summary", {}),
        "userInstruction": loads(row_value(row, "user_instruction_json"), {}),
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
        "shootScriptId": row["shoot_script_id"] if "shoot_script_id" in row.keys() else None,
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
        "shootScriptId": row["shoot_script_id"] if "shoot_script_id" in row.keys() else None,
        "previewId": row["preview_id"] if "preview_id" in row.keys() else None,
        "userInstruction": loads(row_value(row, "user_instruction_json"), {}),
        "images": [
            {
                "id": image["id"],
                "sortOrder": image["sort_order"],
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
        "errorCode": row_value(row, "error_code"),
        "errorMessage": row["error_message"],
        "progress": loads(row_value(row, "progress_json"), None),
        "outputAssetId": row["output_asset_id"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def get_task_ai_operations(task_id: int) -> list[dict[str, Any]]:
    with get_db() as db:
        task = db.execute("SELECT * FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return []
        rows = db.execute(
            """
            SELECT ga.* FROM generation_attempts ga
            WHERE (ga.owner_type = 'generation_styling'
                    AND ga.owner_id = ?
                    AND (
                      (? = 'STYLING_RUNNING' AND ga.status IN ('RUNNING', 'SUBMITTED', 'IN_QUEUE', 'IN_PROGRESS'))
                      OR ga.id IN (
                        SELECT attempt_id FROM generation_stylings
                        WHERE id = ? AND attempt_id IS NOT NULL
                      )
                    ))
               OR (ga.owner_type = 'generation_shoot_script' AND ga.owner_id = ?)
               OR (ga.owner_type = 'generation_preview' AND ga.owner_id = ?)
               OR (ga.owner_type = 'generation_result_image' AND ga.owner_id IN (
                    SELECT gri.id FROM generation_result_images_new gri
                    WHERE gri.result_set_id = ?
                  ))
            ORDER BY ga.created_at DESC, ga.id DESC
            LIMIT 30
            """,
            (
                task_id,
                task["status"],
                task["active_styling_id"],
                row_value(task, "active_shoot_script_id"),
                row_value(task, "active_storyboard_preview_id") or task["active_preview_id"],
                task["active_result_set_id"],
            ),
        ).fetchall()
    return [serialize_attempt_summary(row) for row in rows]


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
