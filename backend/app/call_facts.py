"""Compact execution facts without changing sent text, media or recovery state.

Only known redundant fields are removed. Unknown fields and malformed historical
payloads are deliberately retained. No database/provider dependency.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any


def compact_package(package: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(package)
    if isinstance(result.get("prompt"), str) and result["prompt"].strip() and isinstance(result.get("imageInputs"), list) and result["imageInputs"]:
        # Executable text is authoritative. These configuration copies are never
        # used for replay; pending renders keep their shared result-set configRows.
        result.pop("promptConfigSnapshot", None)
        result.pop("promptConfigContent", None)
        optimization = result.get("promptOptimization")
        if isinstance(optimization, dict):
            for key in ("prompt", "optimizedText"):
                if optimization.get(key) == result["prompt"]:
                    optimization.pop(key)
    return result


def batch_shots(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    shots = {}
    batches = snapshot.get("batchRequests")
    for batch in batches if isinstance(batches, list) else []:
        try:
            body = json.loads(batch["prompt"])
            for shot in body["shots"]:
                if isinstance(shot.get("sourcePrompt"), str):
                    shots[str(shot["shotId"])] = shot
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return shots


def optimizer_source_packages(snapshot: dict[str, Any]) -> dict[str, Any]:
    packages = copy.deepcopy(snapshot.get("sourcePackages", {}))
    if not isinstance(packages, dict):
        return {}
    for key, shot in batch_shots(snapshot).items():
        if isinstance(packages.get(key), dict):
            packages[key].setdefault("prompt", shot["sourcePrompt"])
    return packages


def compact_request(snapshot: dict[str, Any]) -> dict[str, Any]:
    result = compact_package(snapshot)
    if result.get("promptConfigKey") == "assembly.pose_selection" and isinstance(result.get("prompt"), str):
        # Pose execution keeps its render config in memory; the successful pose
        # selection owns the frozen final rules. Audit needs only sent facts.
        result.pop("promptConfigSnapshot", None)
    sources = result.get("sourcePackages")
    if isinstance(sources, dict):
        shots = batch_shots(result)
        for key, package in sources.items():
            if not isinstance(package, dict):
                continue
            package = compact_package(package)
            if key in shots and package.get("prompt") == shots[key]["sourcePrompt"]:
                package.pop("prompt")
            sources[key] = package
    batches = result.get("batchRequests")
    if isinstance(batches, list) and batches and isinstance(batches[0], dict):
        if result.get("prompt") == batches[0].get("prompt"):
            result.pop("prompt", None)
    return result


def compact_response(snapshot: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(snapshot)
    plan = result.get("plan")
    if "shortlist" in result and isinstance(plan, dict) and plan.get("workflow") == "shared_background_v1":
        plan.pop("promptConfigSnapshot", None)
    batches = result.get("batchResponses")
    if not isinstance(batches, list) or not batches or not all(isinstance(b, dict) for b in batches):
        return result
    if result.get("raw_text") == batches[-1].get("raw_text"):
        result.pop("raw_text", None)
    # Discard only a proven exact parsed projection, never a different response.
    parsed = {}
    try:
        for batch in batches:
            for shot in json.loads(batch["raw_text"])["shots"]:
                text = shot["optimizedText"]
                parsed[str(shot["shotId"])] = {"optimizedText": text, "prompt": text}
    except (ValueError, KeyError, TypeError, AttributeError):
        return result
    if result.get("shots") == parsed:
        result.pop("shots", None)
    return result


def referenced_configs(rows: dict[str, Any], roots: list[str]) -> dict[str, Any]:
    """Freeze only selected templates and explicitly referenced dependencies."""
    result, pending = {}, list(roots)
    while pending:
        key = pending.pop()
        if key in result or key not in rows:
            continue
        row = rows[key]
        content = str(row["content"] or "")
        result[key] = {"content": content, "group_type": row["group_type"]}
        pending.extend(re.findall(r"\{\{\s*[#^/]?((?:segment|assembly)\.[\w.]+)\s*\}\}", content))
    return result


def compact_preparation(preparation: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(preparation)
    if isinstance(result.get("packages"), dict):
        result["packages"] = {key: compact_package(value) if isinstance(value, dict) else value
                              for key, value in result["packages"].items()}
    return result


def historical_batch_images(request: dict[str, Any], batch: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(batch.get("images"), list):
        return batch["images"]
    images = {}
    try:
        for shot in json.loads(batch["prompt"])["shots"]:
            sources = request["sourcePackages"][str(shot["shotId"])]["imageInputs"]
            for ref in shot["references"]:
                local_index = int(ref["index"])
                index = int(ref["visionImageIndex"])
                if not 1 <= local_index <= len(sources) or index < 1:
                    return []
                image = sources[local_index - 1]
                if index in images and images[index] != image:
                    return []
                images[index] = image
        if sorted(images) == list(range(1, len(images) + 1)):
            return [images[index] for index in sorted(images)]
    except (ValueError, TypeError, KeyError, IndexError):
        pass
    return []


def model_batches(request: dict[str, Any], response: dict[str, Any]) -> list[dict[str, Any]]:
    requests = request.get("batchRequests")
    responses = response.get("batchResponses")
    outputs = {b.get("batchIndex"): b for b in responses if isinstance(b, dict)} if isinstance(responses, list) else {}
    return [{"batchIndex": b.get("batchIndex"), "shotIds": b.get("shotIds", []),
             "systemInstruction": request.get("systemInstruction", ""), "prompt": b.get("prompt", ""),
             "images": historical_batch_images(request, b),
             "output": outputs.get(b.get("batchIndex"), {}).get("raw_text"),
             "hasResponse": b.get("batchIndex") in outputs}
            for b in requests if isinstance(b, dict)] if isinstance(requests, list) else []


def slim_database_facts(db: Any) -> dict[str, int]:
    """Transactional, idempotent upgrade; rows/IDs/media/statuses stay intact."""
    report = {"rowsChanged": 0, "bytesBefore": 0, "bytesAfter": 0, "malformedSkipped": 0}
    targets = (
        ("generation_attempts", "request_snapshot_json", compact_request),
        ("generation_attempts", "response_snapshot_json", compact_response),
        ("generation_image_versions", "request_snapshot_json", compact_request),
        ("generation_result_images_new", "execution_snapshot_json", compact_request),
        ("generation_result_sets", "prompt_preparation_json", compact_preparation),
    )
    for table, column, compact in targets:
        columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
        if column not in columns:
            continue
        after_id = 0
        while rows := db.execute(f'SELECT id, "{column}" FROM "{table}" WHERE id > ? ORDER BY id LIMIT 100', (after_id,)).fetchall():
            for row in rows:
                after_id, raw = row[0], row[1]
                if not raw:
                    continue
                try:
                    value = json.loads(raw)
                except (TypeError, ValueError):
                    report["malformedSkipped"] += 1
                    continue
                if not isinstance(value, dict):
                    continue
                result = compact(value)
                report["bytesBefore"] += len(raw.encode())
                updated = json.dumps(result, ensure_ascii=False) if result != value else raw
                report["bytesAfter"] += len(updated.encode())
                if updated != raw:
                    db.execute(f'UPDATE "{table}" SET "{column}" = ? WHERE id = ?', (updated, after_id))
                    report["rowsChanged"] += 1
    return report
