"""Pack visual references and validate identities/coverage, without aesthetic scoring."""
from __future__ import annotations

import json
from typing import Any


def pack_pose_references(inputs: list[dict[str, Any]], manifest: list[dict[str, Any]],
                         limit: int | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep every source, using numbered, uncropped boards only above the channel limit."""
    if len(inputs) != len(manifest) or (limit is not None and limit < 1):
        raise ValueError("姿势参考图清单与输入不匹配")
    if limit is None or len(inputs) <= limit:
        return list(inputs), list(manifest)
    size = (len(inputs) + limit - 1) // limit
    if size > 4:
        raise ValueError("姿势选搭参考图片过多，每张参考板最多容纳四张原图，请减少商品参考图")
    packed, mapped = [], []
    for start in range(0, len(inputs), size):
        sources = inputs[start:start + size]
        entries = manifest[start:start + size]
        order = len(packed) + 1
        if len(sources) == 1:
            packed.append(sources[0])
            mapped.append({**entries[0], "sourceOrder": entries[0]["order"], "order": order})
            continue
        labels = [f"{order}.{cell}" for cell in range(1, len(sources) + 1)]
        packed.append({"role": "pose_selection_reference_board", "collageItems": sources,
                       "tileLabels": labels, "filename": f"pose-selection-{order}.jpg"})
        mapped.append({"order": order, "role": "reference_board", "layout": "row-major",
                       "sources": [{**entry, "sourceOrder": entry["order"], "order": order,
                                    "cell": cell, "label": labels[cell - 1]}
                                   for cell, entry in enumerate(entries, 1)]})
    return packed, mapped


def validate_pose_shortlist(value: Any, available_codes: set[str]) -> list[dict[str, Any]]:
    shots = value.get("shots") if isinstance(value, dict) else None
    if not isinstance(shots, list) or len(shots) != 5:
        raise ValueError("姿势候选必须覆盖五个镜头")
    seen: set[int] = set()
    result = []
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError("姿势候选格式不正确")
        index, codes = shot.get("shotIndex"), shot.get("candidateCodes")
        if type(index) is not int or index not in range(1, 6) or index in seen:
            raise ValueError("姿势候选镜头编号缺失、重复或不合法")
        if not isinstance(codes, list) or not 1 <= len(codes) <= 4:
            raise ValueError(f"镜头 {index} 没有合适姿势，或候选超过四个；请补充姿势素材或调整拍摄要求")
        if any(not isinstance(code, str) or code not in available_codes for code in codes) or len(set(codes)) != len(codes):
            raise ValueError("姿势候选引用了不存在或重复的姿势")
        seen.add(index)
        result.append({"shotIndex": index, "candidateCodes": codes, "reason": str(shot.get("reason") or "")})
    return sorted(result, key=lambda shot: shot["shotIndex"])


def pose_plan_from_shortlist(value: Any, library: list[dict[str, Any]]) -> dict[str, Any]:
    """Accept final plans, or promote the first choices of a legacy shortlist."""
    shots = value.get("shots") if isinstance(value, dict) else None
    if isinstance(shots, list) and shots and all(isinstance(shot, dict) and "poseCode" in shot for shot in shots):
        return value
    by_code = {pose["code"]: pose for pose in library}
    shortlist = validate_pose_shortlist(value, set(by_code))
    result = []
    for shot in shortlist:
        code = shot["candidateCodes"][0]
        pose = by_code[code]
        images = pose["images"]
        if len(images) != 1:
            raise ValueError("初筛姿势必须对应唯一一张图片")
        result.append({"shotIndex": shot["shotIndex"], "poseCode": code,
                       "imageId": images[0]["asset"]["id"], "reason": shot["reason"],
                       "direction": json.dumps(pose["aiTags"], ensure_ascii=False)})
    return {**value, "shots": result}


def validate_pose_plan(value: Any, images_by_code: dict[str, set[int]]) -> list[dict[str, Any]]:
    shots = value.get("shots") if isinstance(value, dict) else None
    if not isinstance(shots, list) or len(shots) != 5:
        raise ValueError("姿势定选必须覆盖五个镜头")
    seen: set[int] = set()
    result = []
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError("姿势定选格式不正确")
        index, code, image = shot.get("shotIndex"), shot.get("poseCode"), shot.get("imageId")
        if type(index) is not int or index not in range(1, 6) or index in seen:
            raise ValueError("姿势定选镜头编号缺失、重复或不合法")
        if not isinstance(code, str) or code not in images_by_code:
            raise ValueError("姿势定选必须来自当前可用姿势库")
        if type(image) is not int or image not in images_by_code.get(code, set()):
            raise ValueError("姿势定选图片不属于所选姿势")
        # Current selection returns identities and a reason only. Retain optional
        # historical direction for display/audit, never invent action text here.
        reason, direction = shot.get("reason"), shot.get("direction", "")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("姿势定选缺少选择理由")
        if not isinstance(direction, str):
            raise ValueError("姿势定选的历史动作说明格式不正确")
        seen.add(index)
        result.append({"shotIndex": index, "poseCode": code, "imageId": image,
                       "reason": reason.strip(), "direction": direction.strip()})
    if len({shot["poseCode"] for shot in result}) != 5:
        raise ValueError("五个镜头需要选择五个不同姿势，请补充合适候选或重新选择")
    return sorted(result, key=lambda shot: shot["shotIndex"])
