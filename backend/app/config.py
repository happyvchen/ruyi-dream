from functools import lru_cache
from pathlib import Path
import hashlib
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BASE_DIR.parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(BASE_DIR / ".env.local", override=True)
PLACEHOLDER_SECRET_VALUES = {
    "admin",
    "change-me",
    "changeme",
    "replace-me",
    "replace-with-access-key",
    "replace-with-secret-key",
    "password",
    "secret",
    "test",
    "example",
    "your-access-key",
    "your-secret-key",
    "DQugmV9crHkkYYjG",
}
MIN_PRODUCTION_CREDENTIAL_LENGTH = 12
DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174"


class Settings:
    app_name = os.getenv("APP_NAME", "Ruyi-Dream")
    app_env = os.getenv("APP_ENV", "development").lower()
    data_dir = Path(os.getenv("APP_DATA_DIR", BASE_DIR / "data"))
    database_path = Path(os.getenv("APP_DATABASE_PATH", data_dir / "app.sqlite3"))
    log_dir = Path(os.getenv("APP_LOG_DIR", data_dir / "logs"))
    s3_endpoint_url = os.getenv("APP_S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    s3_public_endpoint_url = os.getenv("APP_S3_PUBLIC_ENDPOINT_URL", s3_endpoint_url)
    s3_bucket = os.getenv("APP_S3_BUCKET", os.getenv("APP_STORAGE_BUCKET", "ruyi-dream"))
    s3_region = os.getenv("APP_S3_REGION", "auto")
    s3_access_key_id = os.getenv("APP_S3_ACCESS_KEY_ID", "")
    s3_secret_access_key = os.getenv("APP_S3_SECRET_ACCESS_KEY", "")
    s3_force_path_style = os.getenv("APP_S3_FORCE_PATH_STYLE", "true").lower() in {"1", "true", "yes", "on"}
    s3_presign_expires_seconds = int(os.getenv("APP_S3_PRESIGN_EXPIRES_SECONDS", "900"))
    direct_upload_cors_origins = [
        origin.strip().rstrip("/")
        for origin in os.getenv("APP_DIRECT_UPLOAD_CORS_ORIGINS", os.getenv("BACKEND_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)).split(",")
        if origin.strip()
    ]
    max_upload_image_bytes = int(os.getenv("APP_MAX_UPLOAD_IMAGE_BYTES", str(50 * 1024 * 1024)))
    max_upload_image_pixels = int(os.getenv("APP_MAX_UPLOAD_IMAGE_PIXELS", str(100_000_000)))
    image_decode_safety_pixels = int(os.getenv("APP_IMAGE_DECODE_SAFETY_PIXELS", str(200_000_000)))
    normalized_image_max_edge = int(os.getenv("APP_NORMALIZED_IMAGE_MAX_EDGE", "8000"))
    max_head_references_per_profile = int(os.getenv("APP_MAX_HEAD_REFERENCES_PER_PROFILE", "20"))
    api_access_token = os.getenv("APP_API_ACCESS_TOKEN", os.getenv("APP_ACCESS_TOKEN", ""))
    user_username = os.getenv("APP_USER_USERNAME", "user")
    user_password_hash = os.getenv("APP_USER_PASSWORD_HASH", "")
    user_password = os.getenv("APP_USER_PASSWORD", "password" if not user_password_hash else "")
    admin_username = os.getenv("APP_ADMIN_USERNAME", "admin")
    admin_password_hash = os.getenv("APP_ADMIN_PASSWORD_HASH", "")
    admin_password = os.getenv("APP_ADMIN_PASSWORD", "password" if not admin_password_hash else "")
    session_secret = os.getenv("APP_SESSION_SECRET", "")
    session_ttl_seconds = int(os.getenv("APP_SESSION_TTL_SECONDS", str(12 * 60 * 60)))
    session_cookie_secure = os.getenv("APP_SESSION_COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}
    trust_reverse_proxy_auth = os.getenv("APP_TRUST_REVERSE_PROXY_AUTH", "false").lower() in {"1", "true", "yes", "on"}
    gemini_api_key = os.getenv("GEMINI_API_KEY", "")
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
    # Gemini 调用通道：direct=google-genai 直连；toapis=ToAPIs OpenAI 兼容中转。
    gemini_provider = (os.getenv("GEMINI_PROVIDER") or "direct").strip().lower() or "direct"
    # ToAPIs 通道使用的模型 ID；留空则复用 GEMINI_MODEL。
    gemini_toapis_model = (os.getenv("GEMINI_TOAPIS_MODEL") or "").strip()
    gemini_toapis_timeout_seconds = int(os.getenv("GEMINI_TOAPIS_TIMEOUT_SECONDS") or "300")
    # requests 超时元组第一个值不只是连接超时:urllib3 在发送请求体阶段也沿用它。
    # 请求体含 base64 图片可达数 MB,部署环境上行慢时过小的值会触发
    # "The write operation timed out",且请求未到达 ToAPIs、不计费。
    gemini_toapis_connect_timeout_seconds = int(os.getenv("GEMINI_TOAPIS_CONNECT_TIMEOUT_SECONDS") or "60")
    # Gemini 聊天通道"零产出瞬时错误"安全重试参数：只在 502/连接错/429 等零计费失败时
    # 重发整个请求，指数退避 + 抖动。重试次数为 0 表示禁用重试。
    gemini_retry_limit = int(os.getenv("GEMINI_RETRY_LIMIT") or "3")
    gemini_retry_backoff_base_seconds = float(os.getenv("GEMINI_RETRY_BACKOFF_BASE_SECONDS") or "2")
    gemini_retry_backoff_cap_seconds = float(os.getenv("GEMINI_RETRY_BACKOFF_CAP_SECONDS") or "30")
    # WAF(Cloudflare 1010)熔断前的有限重试次数。
    gemini_waf_retry_limit = int(os.getenv("GEMINI_WAF_RETRY_LIMIT") or "2")
    # 上层 JSON 解析失败(MALFORMED_OUTPUT)时自动重问 Gemini 的次数(0=不重问)。
    gemini_malformed_reask_limit = int(os.getenv("GEMINI_MALFORMED_REASK_LIMIT") or "1")
    # 打标并发闸门与瞬时错误自动重试次数。
    tagging_max_concurrency = int(os.getenv("TAGGING_MAX_CONCURRENCY") or "3")
    tagging_auto_retry_limit = int(os.getenv("TAGGING_AUTO_RETRY_LIMIT") or "1")
    image_provider = os.getenv("GPT_IMAGE2_PROVIDER", "toapis").lower()
    gpt_image2_model_id = os.getenv("GPT_IMAGE2_MODEL_ID", "gpt-image-2")
    gpt_image2_api_base_url = os.getenv("GPT_IMAGE2_API_BASE_URL", "https://fal.run/openai/gpt-image-2/edit")
    gpt_image2_queue_url = os.getenv("GPT_IMAGE2_QUEUE_URL", "https://queue.fal.run/openai/gpt-image-2/edit")
    fal_key = os.getenv("FAL_KEY", "")
    toapis_api_key = os.getenv("TOAPIS_API_KEY", "")
    toapis_base_url = os.getenv("TOAPIS_BASE_URL", "https://toapis.com")
    # Image-2 创建任务单次请求超时；独立于生成轮询总预算。
    toapis_image_submit_timeout_seconds = int(os.getenv("TOAPIS_IMAGE_SUBMIT_TIMEOUT_SECONDS", "180"))
    # 创建请求响应未知时的用户无感自动恢复：单镜头重试次数、退避和同批熔断阈值。
    generation_unknown_submission_retry_limit = int(os.getenv("GENERATION_UNKNOWN_SUBMISSION_RETRY_LIMIT", "1"))
    generation_unknown_submission_retry_delay_seconds = float(os.getenv("GENERATION_UNKNOWN_SUBMISSION_RETRY_DELAY_SECONDS", "15"))
    generation_unknown_submission_batch_circuit_breaker = int(
        os.getenv("GENERATION_UNKNOWN_SUBMISSION_BATCH_CIRCUIT_BREAKER", "2")
    )
    gpt_image2_quality = os.getenv("GPT_IMAGE2_QUALITY", "high")
    gpt_image2_output_format = os.getenv("GPT_IMAGE2_OUTPUT_FORMAT", "png")
    gpt_image2_size = os.getenv("GPT_IMAGE2_SIZE", "1:1")
    # 分镜预览画布比例：3:2 横版，让 3 列 × 2 行的 6 个分格各自为 1:1，与成片画幅对齐。
    # toapis 路径直接用比例串；fal 路径没有 3:2 枚举，使用显式宽高（见 gpt_image2_preview_fal_image_size）。
    gpt_image2_preview_size = os.getenv("GPT_IMAGE2_PREVIEW_SIZE", "3:2")
    gpt_image2_preview_fal_image_size = os.getenv("GPT_IMAGE2_PREVIEW_FAL_IMAGE_SIZE", "1536x1024")
    gpt_image2_resolution = os.getenv("GPT_IMAGE2_RESOLUTION", "1K")
    generation_poll_interval_seconds = float(os.getenv("GENERATION_POLL_INTERVAL_SECONDS", "5"))
    generation_lock_ttl_seconds = int(os.getenv("GENERATION_LOCK_TTL_SECONDS", "120"))
    generation_image_timeout_seconds = int(os.getenv("GENERATION_IMAGE_TIMEOUT_SECONDS", str(30 * 60)))
    generation_submit_retry_limit = int(os.getenv("GENERATION_SUBMIT_RETRY_LIMIT", "3"))
    # 一键成片（auto-run）并发闸门：同时自动推进的任务数上限，超出的任务按请求时间排队。
    generation_auto_run_max_concurrency = int(os.getenv("GENERATION_AUTO_RUN_MAX_CONCURRENCY", "2"))
    generation_status_retry_limit = int(os.getenv("GENERATION_STATUS_RETRY_LIMIT", "6"))
    # 参考图上传阶段总预算（不含单文件请求自身的超时）。
    generation_upload_timeout_seconds = int(os.getenv("GENERATION_UPLOAD_TIMEOUT_SECONDS", str(5 * 60)))
    # 成片回传（下载+入库+缩略图）阶段总预算；provider 已成功，此阶段内自动退避重试。
    generation_delivery_timeout_seconds = int(os.getenv("GENERATION_DELIVERY_TIMEOUT_SECONDS", str(15 * 60)))
    # 成片单次下载请求超时。低带宽环境下宁可"较短超时+多次重试"，避免单次 300s 挂死。
    generation_download_request_timeout_seconds = int(os.getenv("GENERATION_DOWNLOAD_REQUEST_TIMEOUT_SECONDS", "120"))
    # 参考图 provider 上传缓存 TTL：同一 objectKey 在 TTL 内复用已上传 URL。
    provider_upload_cache_ttl_seconds = int(os.getenv("PROVIDER_UPLOAD_CACHE_TTL_SECONDS", str(6 * 60 * 60)))
    cors_origins = [
        origin.strip()
        for origin in os.getenv(
            "BACKEND_CORS_ORIGINS",
            DEFAULT_CORS_ORIGINS,
        ).split(",")
        if origin.strip()
    ]

    def validate(self) -> None:
        missing = []
        if not self.s3_access_key_id:
            missing.append("APP_S3_ACCESS_KEY_ID")
        if not self.s3_secret_access_key:
            missing.append("APP_S3_SECRET_ACCESS_KEY")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Missing required object storage credential(s): {joined}")
        if self.session_ttl_seconds <= 0:
            raise RuntimeError("APP_SESSION_TTL_SECONDS must be greater than 0.")
        if self.generation_lock_ttl_seconds <= 0:
            raise RuntimeError("GENERATION_LOCK_TTL_SECONDS must be greater than 0.")
        if self.generation_image_timeout_seconds <= 0:
            raise RuntimeError("GENERATION_IMAGE_TIMEOUT_SECONDS must be greater than 0.")
        if self.toapis_image_submit_timeout_seconds <= 0:
            raise RuntimeError("TOAPIS_IMAGE_SUBMIT_TIMEOUT_SECONDS must be greater than 0.")
        if self.generation_unknown_submission_retry_limit < 0:
            raise RuntimeError("GENERATION_UNKNOWN_SUBMISSION_RETRY_LIMIT must be greater than or equal to 0.")
        if self.generation_unknown_submission_retry_delay_seconds < 0:
            raise RuntimeError("GENERATION_UNKNOWN_SUBMISSION_RETRY_DELAY_SECONDS must be greater than or equal to 0.")
        if self.generation_unknown_submission_batch_circuit_breaker <= 0:
            raise RuntimeError("GENERATION_UNKNOWN_SUBMISSION_BATCH_CIRCUIT_BREAKER must be greater than 0.")
        if self.generation_submit_retry_limit <= 0:
            raise RuntimeError("GENERATION_SUBMIT_RETRY_LIMIT must be greater than 0.")
        if self.generation_status_retry_limit <= 0:
            raise RuntimeError("GENERATION_STATUS_RETRY_LIMIT must be greater than 0.")
        if self.generation_auto_run_max_concurrency <= 0:
            raise RuntimeError("GENERATION_AUTO_RUN_MAX_CONCURRENCY must be greater than 0.")
        if self.generation_upload_timeout_seconds <= 0:
            raise RuntimeError("GENERATION_UPLOAD_TIMEOUT_SECONDS must be greater than 0.")
        if self.generation_delivery_timeout_seconds <= 0:
            raise RuntimeError("GENERATION_DELIVERY_TIMEOUT_SECONDS must be greater than 0.")
        if self.generation_download_request_timeout_seconds <= 0:
            raise RuntimeError("GENERATION_DOWNLOAD_REQUEST_TIMEOUT_SECONDS must be greater than 0.")
        if self.provider_upload_cache_ttl_seconds <= 0:
            raise RuntimeError("PROVIDER_UPLOAD_CACHE_TTL_SECONDS must be greater than 0.")
        if self.max_upload_image_bytes <= 0 or self.max_upload_image_pixels <= 0:
            raise RuntimeError("Image upload byte and pixel limits must be greater than 0.")
        if self.image_decode_safety_pixels < self.max_upload_image_pixels:
            raise RuntimeError("APP_IMAGE_DECODE_SAFETY_PIXELS must be greater than or equal to APP_MAX_UPLOAD_IMAGE_PIXELS.")
        if self.normalized_image_max_edge <= 0:
            raise RuntimeError("APP_NORMALIZED_IMAGE_MAX_EDGE must be greater than 0.")
        if self.gemini_provider not in {"direct", "toapis"}:
            raise RuntimeError("GEMINI_PROVIDER must be one of: direct, toapis.")
        if self.gemini_toapis_timeout_seconds <= 0:
            raise RuntimeError("GEMINI_TOAPIS_TIMEOUT_SECONDS must be greater than 0.")
        if self.gemini_toapis_connect_timeout_seconds <= 0:
            raise RuntimeError("GEMINI_TOAPIS_CONNECT_TIMEOUT_SECONDS must be greater than 0.")
        # 重试/重问次数允许为 0（表示禁用）；其余退避/并发参数必须 > 0。
        if self.gemini_retry_limit < 0:
            raise RuntimeError("GEMINI_RETRY_LIMIT must be greater than or equal to 0.")
        if self.gemini_retry_backoff_base_seconds <= 0:
            raise RuntimeError("GEMINI_RETRY_BACKOFF_BASE_SECONDS must be greater than 0.")
        if self.gemini_retry_backoff_cap_seconds <= 0:
            raise RuntimeError("GEMINI_RETRY_BACKOFF_CAP_SECONDS must be greater than 0.")
        if self.gemini_waf_retry_limit < 0:
            raise RuntimeError("GEMINI_WAF_RETRY_LIMIT must be greater than or equal to 0.")
        if self.gemini_malformed_reask_limit < 0:
            raise RuntimeError("GEMINI_MALFORMED_REASK_LIMIT must be greater than or equal to 0.")
        if self.tagging_max_concurrency <= 0:
            raise RuntimeError("TAGGING_MAX_CONCURRENCY must be greater than 0.")
        if self.tagging_auto_retry_limit < 0:
            raise RuntimeError("TAGGING_AUTO_RETRY_LIMIT must be greater than or equal to 0.")

        if self.app_env in {"prod", "production"}:
            self._validate_production_credential("access key", self.s3_access_key_id)
            self._validate_production_credential("secret key", self.s3_secret_access_key)
            if self.api_access_token:
                self._validate_production_credential("API access token", self.api_access_token)
            if self.user_password:
                self._validate_production_credential("user password", self.user_password)
            if self.user_password_hash:
                self._validate_password_hash("APP_USER_PASSWORD_HASH", self.user_password_hash)
            if self.admin_password:
                self._validate_production_credential("admin password", self.admin_password)
            if self.admin_password_hash:
                self._validate_password_hash("APP_ADMIN_PASSWORD_HASH", self.admin_password_hash)
            if self.session_secret:
                self._validate_production_credential("session secret", self.session_secret)
            if not self.trust_reverse_proxy_auth and not self.is_gate_enabled():
                raise RuntimeError(
                    "Production must configure at least one fixed app account, "
                    "or explicitly set APP_TRUST_REVERSE_PROXY_AUTH=true."
                )
            if self.is_gate_enabled() and not self.session_secret:
                raise RuntimeError("Production app gate requires APP_SESSION_SECRET.")
        if not self.user_username.strip():
            raise RuntimeError("APP_USER_USERNAME must not be empty.")
        if not self.admin_username.strip():
            raise RuntimeError("APP_ADMIN_USERNAME must not be empty.")
        if self.user_username.strip().lower() == self.admin_username.strip().lower():
            raise RuntimeError("APP_USER_USERNAME and APP_ADMIN_USERNAME must be different.")

    def _validate_production_credential(self, label: str, value: str) -> None:
        normalized = value.strip().lower()
        if value != value.strip():
            raise RuntimeError(f"Production object storage {label} must not contain leading or trailing whitespace.")
        if len(value) < MIN_PRODUCTION_CREDENTIAL_LENGTH:
            raise RuntimeError(
                f"Production object storage {label} must be at least "
                f"{MIN_PRODUCTION_CREDENTIAL_LENGTH} characters."
            )
        if normalized in PLACEHOLDER_SECRET_VALUES or normalized.startswith("replace-with-"):
            raise RuntimeError(f"Production object storage {label} must not use a placeholder or default value.")

    def _validate_password_hash(self, label: str, value: str) -> None:
        if len(value) != 64:
            raise RuntimeError(f"{label} must be a SHA-256 hex digest.")
        try:
            int(value, 16)
        except ValueError as exc:
            raise RuntimeError(f"{label} must be a SHA-256 hex digest.") from exc

    def is_gate_enabled(self) -> bool:
        return bool(self.user_password or self.user_password_hash or self.admin_password or self.admin_password_hash)

    def is_admin_account_enabled(self) -> bool:
        return bool(self.admin_password or self.admin_password_hash)

    def effective_session_secret(self) -> str:
        seed = (
            self.session_secret
            or self.user_password
            or self.user_password_hash
            or self.admin_password
            or self.admin_password_hash
            or "dev-session"
        )
        return hashlib.sha256(f"{seed}:ruyi-session".encode("utf-8")).hexdigest()

    def is_image_provider_configured(self) -> bool:
        if self.image_provider == "toapis":
            return bool(self.toapis_api_key)
        if self.image_provider == "fal":
            return bool(self.gpt_image2_queue_url and self.fal_key)
        return False


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    return settings
