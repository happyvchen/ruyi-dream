"""成片多模态提示词整理的文本与图片编号契约；摄影规则始终来自可维护模板。"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

OPTIMIZER_KEY = "assembly.final_prompt_optimizer"
POSE_GRID_LAYOUT = "pose_grid_v1"


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


def optimizer_package_batches(packages: dict[str, dict[str, Any]], max_images: int,
                              layout: str | None = None) -> list[dict[str, Any]]:
    """Send the pose-grid group once; preserve frozen legacy batching when unversioned."""
    if layout == POSE_GRID_LAYOUT:
        _, images = optimizer_reference_plan(packages, layout)
        if len(images) > max_images:
            raise ValueError(f"整组整理参考图超过 {max_images} 张，请检查共享输入；不拆分五镜头")
        return [packages] if packages else []
    if layout is not None:
        raise ValueError(f"未知的整理参考图布局：{layout}")
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


def optimizer_reference_plan(packages: dict[str, dict[str, Any]], layout: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Same attachment numbering for local preview and sending; no image I/O."""
    inputs = optimizer_input(packages)
    if layout not in (None, POSE_GRID_LAYOUT):
        raise ValueError(f"未知的整理参考图布局：{layout}")
    grid, cells = None, {}
    if layout == POSE_GRID_LAYOUT and packages:
        sources, slots = [], []
        for key, package in sorted(packages.items(), key=lambda pair: int(pair[1]["promptRuntime"]["shot_index"])):
            index = int(package["promptRuntime"]["shot_index"])
            poses = [item for item in package["imageInputs"] if item.get("role") == "shot_pose"]
            if index not in range(1, 6) or index in slots or len(poses) != 1:
                raise ValueError("姿势宫格要求每个镜头有唯一姿势参考，镜头号为 1 至 5 且不重复")
            sources.append(copy.deepcopy(poses[0]))
            slots.append(index)
            cells[key] = index
        grid = {"role": "shot_pose", "filename": "optimizer-poses.jpg",
                "referenceBoardKind": "optimizer_pose_grid", "collageItems": sources,
                "collageRule": {"kind": "optimizer_pose_grid_3x2", "cells": slots, "notPersisted": True},
                "tileLabels": [f"SHOT {i}" if i in slots else "EMPTY" for i in range(1, 7)],
                "providerCanvas": "square_white_v1"}
    manifest, seen, images = [], {}, []
    for shot in inputs["shots"]:
        for reference, item in zip(shot["references"], packages[shot["shotId"]]["imageInputs"]):
            if grid and item.get("role") == "shot_pose":
                item = grid
                reference["cell"] = cells[shot["shotId"]]
            identity = optimizer_reference_identity(item)
            if identity not in seen:
                seen[identity] = len(seen) + 1
                entry = {"visionImageIndex": seen[identity], "role": item["role"]}
                if item is grid:
                    entry.update(referenceBoardKind="optimizer_pose_grid", columns=3, rows=2,
                        cells=[{"cell": cell, "shotId": key, "shotIndex": cell, "label": f"SHOT {cell}"} for key, cell in cells.items()],
                        emptyCells=[i for i in range(1, 7) if i not in cells.values()])
                manifest.append(entry)
                images.append(copy.deepcopy(item))
            reference["visionImageIndex"] = seen[identity]
    inputs["imageManifest"] = manifest
    return inputs, images


def optimizer_mapped_input(packages: dict[str, dict[str, Any]], layout: str | None = None) -> dict[str, Any]:
    return optimizer_reference_plan(packages, layout)[0]


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
