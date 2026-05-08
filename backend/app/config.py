from functools import lru_cache
from pathlib import Path
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")


class Settings:
    app_name = os.getenv("APP_NAME", "AI Model Image MVP")
    data_dir = Path(os.getenv("APP_DATA_DIR", BASE_DIR / "data"))
    database_path = Path(os.getenv("APP_DATABASE_PATH", data_dir / "app.sqlite3"))
    log_dir = Path(os.getenv("APP_LOG_DIR", data_dir / "logs"))
    s3_endpoint_url = os.getenv("APP_S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    s3_public_endpoint_url = os.getenv("APP_S3_PUBLIC_ENDPOINT_URL", s3_endpoint_url)
    s3_bucket = os.getenv("APP_S3_BUCKET", os.getenv("APP_STORAGE_BUCKET", "app-media"))
    s3_region = os.getenv("APP_S3_REGION", "auto")
    s3_access_key_id = os.getenv("APP_S3_ACCESS_KEY_ID", os.getenv("RUSTFS_ACCESS_KEY", "admin"))
    s3_secret_access_key = os.getenv("APP_S3_SECRET_ACCESS_KEY", os.getenv("RUSTFS_SECRET_KEY", "DQugmV9crHkkYYjG"))
    s3_force_path_style = os.getenv("APP_S3_FORCE_PATH_STYLE", "true").lower() in {"1", "true", "yes", "on"}
    s3_presign_expires_seconds = int(os.getenv("APP_S3_PRESIGN_EXPIRES_SECONDS", "900"))
    gemini_api_key = os.getenv("GEMINI_API_KEY", "")
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
    image_provider = os.getenv("GPT_IMAGE2_PROVIDER", "placeholder").lower()
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


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    return settings
