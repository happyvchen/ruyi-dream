from functools import lru_cache
from pathlib import Path
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")


class Settings:
    app_name = "Lily AI Model Image MVP"
    storage_dir = Path(os.getenv("LILY_STORAGE_DIR", BASE_DIR / "storage"))
    database_path = Path(os.getenv("LILY_DATABASE_PATH", storage_dir / "lily_mvp.sqlite3"))
    media_url_prefix = "/media"
    default_model_asset_dir = Path("/Users/fushuai/Desktop/项目/Lily/Face-01")
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
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    (settings.storage_dir / "uploads").mkdir(parents=True, exist_ok=True)
    (settings.storage_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (settings.storage_dir / "logs").mkdir(parents=True, exist_ok=True)
    return settings
