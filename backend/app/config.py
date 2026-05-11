from functools import lru_cache
from pathlib import Path
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")
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
    app_name = os.getenv("APP_NAME", "AI Model Image MVP")
    app_env = os.getenv("APP_ENV", "development").lower()
    data_dir = Path(os.getenv("APP_DATA_DIR", BASE_DIR / "data"))
    database_path = Path(os.getenv("APP_DATABASE_PATH", data_dir / "app.sqlite3"))
    log_dir = Path(os.getenv("APP_LOG_DIR", data_dir / "logs"))
    s3_endpoint_url = os.getenv("APP_S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    s3_public_endpoint_url = os.getenv("APP_S3_PUBLIC_ENDPOINT_URL", s3_endpoint_url)
    s3_bucket = os.getenv("APP_S3_BUCKET", os.getenv("APP_STORAGE_BUCKET", "ruyi-dream"))
    s3_region = os.getenv("APP_S3_REGION", "auto")
    s3_access_key_id = os.getenv("APP_S3_ACCESS_KEY_ID", os.getenv("RUSTFS_ACCESS_KEY", ""))
    s3_secret_access_key = os.getenv("APP_S3_SECRET_ACCESS_KEY", os.getenv("RUSTFS_SECRET_KEY", ""))
    s3_force_path_style = os.getenv("APP_S3_FORCE_PATH_STYLE", "true").lower() in {"1", "true", "yes", "on"}
    s3_presign_expires_seconds = int(os.getenv("APP_S3_PRESIGN_EXPIRES_SECONDS", "900"))
    max_upload_image_bytes = int(os.getenv("APP_MAX_UPLOAD_IMAGE_BYTES", str(15 * 1024 * 1024)))
    max_upload_image_pixels = int(os.getenv("APP_MAX_UPLOAD_IMAGE_PIXELS", str(25_000_000)))
    max_garment_images_per_sku = int(os.getenv("APP_MAX_GARMENT_IMAGES_PER_SKU", "20"))
    max_head_references_per_profile = int(os.getenv("APP_MAX_HEAD_REFERENCES_PER_PROFILE", "20"))
    gemini_api_key = os.getenv("GEMINI_API_KEY", "")
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
    image_provider = os.getenv("GPT_IMAGE2_PROVIDER", "fal").lower()
    gpt_image2_model_id = os.getenv("GPT_IMAGE2_MODEL_ID", "openai/gpt-image-2/edit")
    gpt_image2_api_base_url = os.getenv("GPT_IMAGE2_API_BASE_URL", "https://fal.run/openai/gpt-image-2/edit")
    gpt_image2_queue_url = os.getenv("GPT_IMAGE2_QUEUE_URL", "https://queue.fal.run/openai/gpt-image-2/edit")
    fal_key = os.getenv("FAL_KEY", "")
    gpt_image2_quality = os.getenv("GPT_IMAGE2_QUALITY", "high")
    gpt_image2_output_format = os.getenv("GPT_IMAGE2_OUTPUT_FORMAT", "png")
    generation_poll_interval_seconds = float(os.getenv("GENERATION_POLL_INTERVAL_SECONDS", "5"))
    cors_origins = [
        origin.strip()
        for origin in os.getenv(
            "BACKEND_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ]

    def validate(self) -> None:
        missing = []
        if not self.s3_access_key_id:
            missing.append("APP_S3_ACCESS_KEY_ID or RUSTFS_ACCESS_KEY")
        if not self.s3_secret_access_key:
            missing.append("APP_S3_SECRET_ACCESS_KEY or RUSTFS_SECRET_KEY")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Missing required object storage credential(s): {joined}")

        if self.app_env in {"prod", "production"}:
            self._validate_production_credential("access key", self.s3_access_key_id)
            self._validate_production_credential("secret key", self.s3_secret_access_key)

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


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    return settings
