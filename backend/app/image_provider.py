from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from .config import get_settings


class ImageGenerationProvider:
    async def generate(self, task_package: dict[str, Any], output_path: Path) -> dict[str, Any]:
        raise NotImplementedError


class GPTImage2PlaceholderProvider(ImageGenerationProvider):
    async def generate(self, task_package: dict[str, Any], output_path: Path) -> dict[str, Any]:
        settings = get_settings()
        if settings.gpt_image2_api_base_url and settings.gpt_image2_api_key:
            # The third-party GPT Image-2 API is intentionally not bound yet.
            # Keep the request shape stable so a real adapter can replace this class.
            provider_mode = "third-party-configured-placeholder"
        else:
            provider_mode = "local-placeholder"

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render_placeholder_svg(task_package), encoding="utf-8")
        return {
            "provider": "gpt-image-2-placeholder",
            "mode": provider_mode,
            "outputPath": str(output_path),
            "note": "真实三方平台未确定，当前写入占位图并完整保留任务包与日志。",
        }


def render_placeholder_svg(task_package: dict[str, Any]) -> str:
    shot = task_package.get("shot", {})
    garment = task_package.get("garment", {})
    title = escape(str(shot.get("title", "生成图")))
    frame = escape(str(shot.get("frame", "")))
    focus = escape(str(shot.get("focus", "")))
    category = escape(str(garment.get("category", "待识别品类")))
    summary = escape(str(garment.get("summary", "Lily 白/浅灰棚拍风格")))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1200" viewBox="0 0 1200 1200">
  <rect width="1200" height="1200" fill="#f7f7f5"/>
  <rect x="96" y="96" width="1008" height="1008" rx="8" fill="#ffffff" stroke="#d8d8d2" stroke-width="2"/>
  <rect x="300" y="180" width="600" height="760" rx="300" fill="#eeeeea"/>
  <path d="M600 250 C520 250 470 310 470 390 L470 850 C470 920 530 970 600 970 C670 970 730 920 730 850 L730 390 C730 310 680 250 600 250Z" fill="#d9d9d2"/>
  <circle cx="600" cy="240" r="82" fill="#cfcfc8"/>
  <text x="600" y="1020" text-anchor="middle" font-family="Arial, sans-serif" font-size="42" fill="#2c2c2c">{title}</text>
  <text x="600" y="1074" text-anchor="middle" font-family="Arial, sans-serif" font-size="28" fill="#666">{category} · {frame}</text>
  <text x="600" y="1120" text-anchor="middle" font-family="Arial, sans-serif" font-size="24" fill="#777">{focus}</text>
  <text x="600" y="1160" text-anchor="middle" font-family="Arial, sans-serif" font-size="20" fill="#999">{summary}</text>
</svg>
"""


def get_image_provider() -> ImageGenerationProvider:
    return GPTImage2PlaceholderProvider()
