"""成片多模态提示词整理的文本与图片编号契约；摄影规则始终来自可维护模板。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

OPTIMIZER_KEY = "assembly.final_prompt_optimizer"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def optimizer_reference_identity(item: dict[str, Any]) -> str:
    """A crop or a grid is a different visual attachment, even with the same source."""
    return json.dumps({
        "role": item.get("role"), "collageRule": item.get("collageRule"),
        "providerCrop": item.get("providerCrop"), "tileLabels": item.get("tileLabels"),
        "providerCanvas": item.get("providerCanvas"),
        "sources": [optimizer_reference_identity(source) for source in item["collageItems"]]
        if item.get("collageItems") else [item.get("objectKey") or item.get("providerUrl") or item.get("url")],
    }, sort_keys=True, ensure_ascii=False)


def optimizer_package_batches(packages: dict[str, dict[str, Any]], max_images: int) -> list[dict[str, Any]]:
    """Keep each shot intact; split only when the shared attachment union exceeds the limit."""
    batches, batch, seen = [], {}, set()
    for key, package in packages.items():
        identities = {optimizer_reference_identity(item) for item in package["imageInputs"]}
        if len(identities) > max_images:
            raise ValueError(f"镜头 {key} 的整理参考图超过 {max_images} 张")
        if batch and len(seen | identities) > max_images:
            batches.append(batch)
            batch, seen = {}, set()
        batch[key] = package
        seen.update(identities)
    if batch:
        batches.append(batch)
    return batches


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
