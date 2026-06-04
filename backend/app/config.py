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
    max_upload_image_bytes = int(os.getenv("APP_MAX_UPLOAD_IMAGE_BYTES", str(15 * 1024 * 1024)))
    max_upload_image_pixels = int(os.getenv("APP_MAX_UPLOAD_IMAGE_PIXELS", str(25_000_000)))
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
    image_provider = os.getenv("GPT_IMAGE2_PROVIDER", "toapis").lower()
    gpt_image2_model_id = os.getenv("GPT_IMAGE2_MODEL_ID", "gpt-image-2")
    gpt_image2_api_base_url = os.getenv("GPT_IMAGE2_API_BASE_URL", "https://fal.run/openai/gpt-image-2/edit")
    gpt_image2_queue_url = os.getenv("GPT_IMAGE2_QUEUE_URL", "https://queue.fal.run/openai/gpt-image-2/edit")
    fal_key = os.getenv("FAL_KEY", "")
    toapis_api_key = os.getenv("TOAPIS_API_KEY", "")
    toapis_base_url = os.getenv("TOAPIS_BASE_URL", "https://toapis.com")
    gpt_image2_quality = os.getenv("GPT_IMAGE2_QUALITY", "high")
    gpt_image2_output_format = os.getenv("GPT_IMAGE2_OUTPUT_FORMAT", "png")
    gpt_image2_size = os.getenv("GPT_IMAGE2_SIZE", "1:1")
    gpt_image2_resolution = os.getenv("GPT_IMAGE2_RESOLUTION", "1K")
    generation_poll_interval_seconds = float(os.getenv("GENERATION_POLL_INTERVAL_SECONDS", "5"))
    generation_lock_ttl_seconds = int(os.getenv("GENERATION_LOCK_TTL_SECONDS", "120"))
    generation_image_timeout_seconds = int(os.getenv("GENERATION_IMAGE_TIMEOUT_SECONDS", str(30 * 60)))
    generation_submit_retry_limit = int(os.getenv("GENERATION_SUBMIT_RETRY_LIMIT", "3"))
    generation_status_retry_limit = int(os.getenv("GENERATION_STATUS_RETRY_LIMIT", "6"))
    cors_origins = [
        origin.strip()
        for origin in os.getenv(
            "BACKEND_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174",
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
        if self.generation_submit_retry_limit <= 0:
            raise RuntimeError("GENERATION_SUBMIT_RETRY_LIMIT must be greater than 0.")
        if self.generation_status_retry_limit <= 0:
            raise RuntimeError("GENERATION_STATUS_RETRY_LIMIT must be greater than 0.")

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
