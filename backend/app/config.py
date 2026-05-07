from functools import lru_cache
from pathlib import Path
import os


BASE_DIR = Path(__file__).resolve().parents[1]


class Settings:
    app_name = "Lily AI Model Image MVP"
    storage_dir = Path(os.getenv("LILY_STORAGE_DIR", BASE_DIR / "storage"))
    database_path = Path(os.getenv("LILY_DATABASE_PATH", storage_dir / "lily_mvp.sqlite3"))
    media_url_prefix = "/media"
    default_model_asset_dir = Path("/Users/fushuai/Desktop/项目/Lily/Face-01")
    gemini_api_key = os.getenv("GEMINI_API_KEY", "")
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
    gpt_image2_api_base_url = os.getenv("GPT_IMAGE2_API_BASE_URL", "")
    gpt_image2_api_key = os.getenv("GPT_IMAGE2_API_KEY", "")
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

