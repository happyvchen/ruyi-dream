from __future__ import annotations

import asyncio
import copy
import json

import pytest

from app import main
from app.call_facts import compact_request, compact_response, model_batches, optimizer_source_packages, slim_database_facts
from app.db import dumps, get_db, init_db, loads
from app.image_provider import build_fal_request_snapshot, build_toapis_request_snapshot
from test_final_prompt_optimization import create_run, fourteen_inputs, mock_success, no_external_calls


def test_legacy_compaction_keeps_sent_text_and_reconstructs_sources():
    images = [{"role": "hero_garment", "objectKey": "original/服装.png", "providerCrop": {"ratio": .7}}]
    package = {"prompt": " 原始正文\n", "imageInputs": images, "promptRuntime": {"shot_index": "1"},
               "promptConfigSnapshot": {"large": "x" * 10000}, "promptConfigContent": "旧模板", "unknown": "keep"}
    prompt = dumps({"shots": [{"shotId": "1", "sourcePrompt": package["prompt"],
        "references": [{"index": 1, "visionImageIndex": 1}]}]})
    request = {"systemInstruction": "规则\n", "sourcePackages": {"1": package}, "prompt": prompt,
               "batchRequests": [{"batchIndex": 1, "shotIds": ["1"], "prompt": prompt}]}
    original = copy.deepcopy(request)
    slim = compact_request(request)
    assert request == original
    assert len(dumps(slim)) < len(dumps(request)) / 4
    assert slim["batchRequests"] == request["batchRequests"]
    assert slim["systemInstruction"] == request["systemInstruction"]
    assert "prompt" not in slim and "prompt" not in slim["sourcePackages"]["1"]
    restored = optimizer_source_packages(slim)["1"]
    assert restored["prompt"] == package["prompt"]
    assert restored["imageInputs"] == images and restored["unknown"] == "keep"
    assert "promptConfigSnapshot" not in restored
    assert compact_request(slim) == slim
    assert model_batches(slim, {})[0]["images"] == images


def test_different_or_malformed_evidence_is_not_deduplicated():
    request = {"prompt": "different", "batchRequests": [{"prompt": "invalid-json"}],
               "sourcePackages": {"1": {"prompt": "keep"}}, "unknown": [1, 2]}
    assert compact_request(request) == request
    for malformed in [None, {}, "invalid", [None]]:
        assert compact_request({"batchRequests": malformed}) == {"batchRequests": malformed}
    response = {"raw_text": "different", "shots": {"1": "different"}, "batchResponses": [{"raw_text": "invalid"}]}
    assert compact_response(response) == response


def test_batches_pair_responses_by_index_and_keep_missing_response():
    request = {"systemInstruction": "sys", "batchRequests": [
        {"batchIndex": 1, "prompt": "first", "shotIds": ["1"]},
        {"batchIndex": 2, "prompt": "second", "shotIds": ["2"]}]}
    response = {"batchResponses": [{"batchIndex": 2, "raw_text": "second response"}]}
    batches = model_batches(request, response)
    assert batches[0]["hasResponse"] is False and batches[0]["output"] is None
    assert batches[1]["prompt"] == "second" and batches[1]["output"] == "second response"


def test_migration_preserves_replay_and_frozen_pending_rules(client, monkeypatch):
    ids, group, image_ids = create_run(client)
    calls, images = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], group, image_ids))
    before = main.shot_replay_package(ids["task_id"], image_ids[0])
    with get_db() as db:
        row = db.execute("SELECT id, request_snapshot_json FROM generation_attempts WHERE owner_type='generation_result_image' ORDER BY id LIMIT 1").fetchone()
        legacy = loads(row["request_snapshot_json"])
        legacy.update(promptConfigSnapshot={"unused": {"content": "x" * 40000}}, promptConfigContent="old template")
        db.execute("UPDATE generation_attempts SET request_snapshot_json=? WHERE id=?", (dumps(legacy), row["id"]))
        preparation = loads(db.execute("SELECT prompt_preparation_json FROM generation_result_sets WHERE id=?", (group,)).fetchone()[0])
        preparation["configRows"].pop("assembly.final_prompt_optimizer_input")
        db.execute("UPDATE generation_result_sets SET prompt_preparation_json=? WHERE id=?", (dumps(preparation), group))
        db.execute("DELETE FROM schema_migrations WHERE version='089'")
        db.execute("UPDATE prompt_configs SET content='自定义 {{runtime.original_prompt}}' WHERE key='assembly.image_retry'")
    init_db()
    after = main.shot_replay_package(ids["task_id"], image_ids[0])
    assert (after["prompt"], after["imageInputs"]) == (before["prompt"], before["imageInputs"])
    assert main.read_final_preparation(group)["configRows"]["assembly.final_image_top"] == preparation["configRows"]["assembly.final_image_top"]
    assert main.prompt_config_content("assembly.image_retry") == "自定义 {{runtime.original_prompt}}"
    with get_db() as db:
        assert slim_database_facts(db)["rowsChanged"] == 0
        assert len(db.execute("SELECT id FROM generation_image_versions").fetchall()) == 5
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert len(calls) == 1  # Migration and replay never invoke the optimizer.


def test_optimizer_preview_matches_actual_batches_without_image_reads(client, monkeypatch):
    monkeypatch.setattr(main, "build_direct_final_image_inputs", fourteen_inputs)
    ids, group, image_ids = create_run(client)
    calls, _ = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], group, image_ids))
    attempt = main.read_final_preparation(group)["attemptId"]
    def no_image_read(*args):
        pytest.fail("Preview must not read images")
    monkeypatch.setattr(main, "vision_reference_input", no_image_read)
    preview = client.post("/api/prompt-configs/assembly.final_prompt_optimizer/preview", json={"content": "整理规则 v1", "attemptId": attempt})
    assert preview.status_code == 200, preview.text
    batches = preview.json()["modelBatches"]
    assert len(batches) == len(calls) == 1
    for batch, (inputs, kwargs) in zip(batches, calls):
        assert loads(batch["prompt"]) == inputs
        assert batch["systemInstruction"] == kwargs["system_instruction"]
        assert len(batch["images"]) == len(kwargs["images"])
        assert all(set(item) == {"visionImageIndex", "role"} for item in inputs["imageManifest"] if not item.get("referenceBoardKind"))
        assert inputs["imageManifest"][1]["referenceBoardKind"] == "optimizer_pose_grid"
        assert "objectKey" not in batch["prompt"]
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_attempts WHERE id=?", (attempt,)).fetchone()
    detail = main.serialize_attempt_detail(row)
    assert len(detail["modelBatches"]) == 1
    assert "prompt" not in loads(row["request_snapshot_json"])
    assert "raw_text" not in loads(row["response_snapshot_json"])
    # Slimmed old optimizer history is still a valid explicit test context.
    monkeypatch.setattr(main, "vision_reference_input", lambda item: {"data": b"mock", "mime_type": "image/png"})
    response = client.post("/api/prompt-configs/assembly.final_prompt_optimizer/test-optimization", json={"content": "新规则", "attemptId": attempt})
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 5


def test_prompt_wrappers_are_editable_and_provider_preserves_exact_text(client):
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content='前缀 {{runtime.original_prompt}} / {{runtime.contract_error}}' WHERE key='assembly.contract_correction'")
        db.execute("UPDATE prompt_configs SET content='挑图' WHERE key='assembly.garment_image_selection'")
        db.execute("UPDATE prompt_configs SET content='打标' WHERE key='assembly.ai_tagging'")
    assert main.build_gemini_contract_correction_prompt("正文", "错误") == "前缀 正文 / 错误\n"
    assert main.build_prompt_tagging_prompt({}) == main.render_prompt_template("assembly.tagging_request", main.prompt_tagging_runtime({}))
    text = "  模型实际正文\n\n"
    for build in (build_fal_request_snapshot, build_toapis_request_snapshot):
        assert build({"prompt": text}, ["mock-url"])["prompt"] == text
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content='' WHERE key='assembly.contract_correction'")
    with pytest.raises(RuntimeError):
        main.build_gemini_contract_correction_prompt("正文", "错误")
