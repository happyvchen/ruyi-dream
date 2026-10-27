"""成片多模态提示词整理的文本与图片编号契约；摄影规则始终来自可维护模板。"""
from __future__ import annotations

import hashlib
from typing import Any

OPTIMIZER_KEY = "assembly.final_prompt_optimizer"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def optimizer_input(packages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"shots": [
        {"shotId": key, "shotIndex": package["promptRuntime"]["shot_index"],
         "sourcePrompt": package["prompt"],
         "references": [{"index": index, "role": item["role"]} for index, item in enumerate(package["imageInputs"], 1)]}
        for key, package in packages.items()
    ]}


def parse_optimized_prompts(raw: dict[str, Any], packages: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    """按镜头取回模型正文，不判断内容、不拼接或改写提示词。"""
    try:
        texts = {str(shot["shotId"]): shot["optimizedText"] for shot in raw["shots"]}
    except (KeyError, TypeError) as exc:
        raise ValueError("无法读取模型返回的镜头提示词") from exc
    result: dict[str, dict[str, str]] = {}
    for key in packages:
        body = texts.get(key)
        if not isinstance(body, str) or not body.strip():
            raise ValueError(f"模型未返回镜头 {key} 的提示词正文")
        result[key] = {"optimizedText": body, "prompt": body}
    return result
