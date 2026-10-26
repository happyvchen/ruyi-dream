from __future__ import annotations

import asyncio
import copy

import pytest

from app import main
from app.db import dumps, get_db, loads, now_iso, init_db
from app.prompt_optimization import OPTIMIZER_KEY, locked_constraints, validate_optimized_prompts
from conftest import insert_media_asset, insert_ready_generation_task_with_preview


TEMPLATE = """生成一张棚拍成片，{{runtime.final_image_user_instruction}}
【参考图职责与优先级】
图1 是母版，图2 是姿势，图3 是商品。保留模特身份。
【当前镜头的商品表达】
当前为镜头 {{runtime.shot_index}}，保留商品结构。
【反伪影成像要求】
保留真实纹理，不添加颗粒。
"""


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("测试不得调用外部 AI")
    monkeypatch.setattr(main, "generate_gemini_json_text", forbidden)
    monkeypatch.setattr(main, "generate_image_asset", forbidden)
    monkeypatch.setattr(main, "snapshot_default_model", lambda: {})
    monkeypatch.setattr(main, "build_direct_final_image_inputs", lambda *args: [
        {"role": role, "objectKey": f"tests/{role}.png"} for role in ("shot_background", "shot_pose", "hero_garment")
    ])


def create_run(client, enabled=True):
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content = ? WHERE key IN (?, ?, ?)", (TEMPLATE, *main.FINAL_IMAGE_PROMPT_CONFIG_KEYS.values()))
        db.execute("UPDATE prompt_configs SET content = '整理规则 v1', enabled = ? WHERE key = ?", (int(enabled), OPTIMIZER_KEY))
    ids = insert_ready_generation_task_with_preview(status="STYLING_READY")
    response = client.post(f"/api/generation-tasks/{ids['task_id']}/generate-final")
    assert response.status_code == 200, response.text
    item = response.json()["item"]
    return ids, item["resultSet"]["id"], [image["id"] for image in item["resultSet"]["images"]]


def mock_success(monkeypatch):
    calls, images = [], []

    async def optimize(prompt, **kwargs):
        inputs = loads(prompt)
        calls.append((inputs, kwargs))
        return dumps({"shots": [{"shotId": shot["shotId"], "optimizedText": "输出单张商品棚拍图。", "changeSummary": "合并重复要求", "conflicts": []} for shot in inputs["shots"]]})

    async def generate(package, output_key, **kwargs):
        images.append(copy.deepcopy(package))
        return insert_media_asset(f"result-{len(images)}.png"), {"status": "succeeded"}

    monkeypatch.setattr(main, "generate_gemini_json_text", optimize)
    monkeypatch.setattr(main, "generate_image_asset", generate)
    return calls, images


def test_once_per_group_freezes_configuration_and_sends_only_final_text(client, monkeypatch):
    ids, result_set, image_ids = create_run(client)
    calls, images = mock_success(monkeypatch)
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content = '后续修改' WHERE key = ?", (OPTIMIZER_KEY,))
        db.execute("UPDATE prompt_configs SET content = '错误的新模板' WHERE key = 'assembly.final_image_top'")
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    assert len(calls) == 1 and len(calls[0][0]["shots"]) == 5
    assert calls[0][1]["system_instruction"] == "整理规则 v1"
    assert len(images) == 5
    for package in images:
        assert package["prompt"].startswith("输出单张商品棚拍图。")
        assert locked_constraints(package["originalPrompt"]) in package["prompt"]
        assert "错误的新模板" not in package["prompt"]
        assert "changeSummary" not in package["prompt"]
    assert main.get_generation_task_row(ids["task_id"])["status"] == "COMPLETED"
    assert main.get_task_attempt_summary_page(ids["task_id"], "generation_result_set", 10)["total"] == 1


def test_unchanged_single_retry_reuses_optimized_prompt_changed_instruction_reoptimizes_one(client, monkeypatch):
    ids, result_set, image_ids = create_run(client)
    calls, images = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    input_version = main.get_generation_task_row(ids["task_id"])["current_input_version_id"]
    first = copy.deepcopy(images[0])
    for instruction, expected_calls in [("", 1), ("请减少配饰", 2)]:
        response = client.post(f"/api/generation-result-images-new/{image_ids[0]}/retry", json={"userInstruction": instruction})
        assert response.status_code == 200, response.text
        attempt_id = response.json()["item"]["resultSet"]["images"][0]["activeAttemptId"]
        asyncio.run(main.run_final_generation(ids["task_id"], result_set, [image_ids[0]], {image_ids[0]: attempt_id}))
        assert len(calls) == expected_calls
    assert len(calls[-1][0]["shots"]) == 1
    assert images[5]["prompt"] == first["prompt"]
    assert images[-1]["finalImageUserInstruction"]["instruction"] == "请减少配饰"
    assert main.get_generation_task_row(ids["task_id"])["current_input_version_id"] == input_version


def test_bad_optimizer_output_blocks_all_images_and_is_retryable_by_user(client, monkeypatch):
    ids, result_set, image_ids = create_run(client)
    async def bad(*args, **kwargs):
        return '{"shots": []}'
    monkeypatch.setattr(main, "generate_gemini_json_text", bad)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    assert main.read_final_preparation(result_set)["status"] == "FAILED"
    assert main.get_generation_task_row(ids["task_id"])["status"] == "FINAL_PARTIAL"
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM generation_attempts WHERE owner_type = 'generation_result_image'").fetchone()[0] == 0
        assert {row[0] for row in db.execute("SELECT status FROM generation_result_images_new")} == {"FAILED"}
    pending = main.continue_active_final_result_set(ids["task_id"], result_set, ids["styling_id"], ids["preview_id"])
    calls, images = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, pending))
    assert len(calls) == 1 and len(images) == 5


def test_cancel_during_optimization_preserves_valid_output_without_submitting_images(client, monkeypatch):
    ids, result_set, image_ids = create_run(client)
    async def cancel(prompt, **kwargs):
        with get_db() as db:
            main.cancel_running_task_attempts(db, ids["task_id"], now_iso())
            db.execute("UPDATE generation_tasks SET status = 'CANCELLED' WHERE id = ?", (ids["task_id"],))
            db.execute("UPDATE generation_result_sets SET status = 'CANCELLED' WHERE id = ?", (result_set,))
            db.execute("UPDATE generation_result_images_new SET status = 'CANCELLED' WHERE result_set_id = ?", (result_set,))
        return dumps({"shots": [{"shotId": shot["shotId"], "optimizedText": "输出单张棚拍图", "changeSummary": "整理", "conflicts": []} for shot in loads(prompt)["shots"]]})
    monkeypatch.setattr(main, "generate_gemini_json_text", cancel)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    assert main.get_generation_task_row(ids["task_id"])["status"] == "CANCELLED"
    assert len(main.read_final_preparation(result_set)["packages"]) == 5
    pending = main.continue_active_final_result_set(ids["task_id"], result_set, ids["styling_id"], ids["preview_id"])
    calls, images = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, pending))
    assert not calls and len(images) == 5


def test_disabled_mode_does_not_call_optimizer(client, monkeypatch):
    ids, result_set, image_ids = create_run(client, enabled=False)
    calls, images = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    assert not calls and len(images) == 5
    assert "promptOptimization" not in images[0]


@pytest.mark.parametrize("change", ["duplicate", "missing", "reference", "placeholder", "conflict", "empty"])
def test_output_contract_rejects_invalid_results(change):
    package = {"prompt": TEMPLATE.replace("{{runtime.shot_index}}", "1").replace("{{runtime.final_image_user_instruction}}", ""), "imageInputs": [{"role": "hero_garment"}], "promptRuntime": {"shot_index": "1"}}
    packages = {"1": package, "2": package}
    raw = {"shots": [{"shotId": key, "optimizedText": "棚拍图片", "changeSummary": "整理", "conflicts": []} for key in packages]}
    if change == "duplicate": raw["shots"][1]["shotId"] = "1"
    if change == "missing": raw["shots"].pop()
    if change == "reference": raw["shots"][0]["optimizedText"] = "使用图1-8"
    if change == "placeholder": raw["shots"][0]["optimizedText"] = "{{runtime.unknown}}"
    if change == "conflict": raw["shots"][0]["conflicts"] = ["裁切冲突"]
    if change == "empty": raw["shots"][0]["optimizedText"] = " "
    with pytest.raises(ValueError): validate_optimized_prompts(raw, packages)


def test_migration_keeps_empty_disabled_slot_and_does_not_overwrite_saved_rules():
    with get_db() as db:
        row = db.execute("SELECT * FROM prompt_configs WHERE key = ?", (OPTIMIZER_KEY,)).fetchone()
        assert row["content"] == "" and row["enabled"] == 0
        db.execute("UPDATE prompt_configs SET content = '人工规则', enabled = 1 WHERE key = ?", (OPTIMIZER_KEY,))
    init_db()
    with get_db() as db:
        row = db.execute("SELECT * FROM prompt_configs WHERE key = ?", (OPTIMIZER_KEY,)).fetchone()
        assert row["content"] == "人工规则" and row["enabled"] == 1


def test_admin_preview_and_save_never_call_llm_test_is_explicit_and_preserves_task(client, monkeypatch):
    ids, result_set, image_ids = create_run(client)
    source = main.build_new_final_task_package(ids["task_id"], image_ids[0])
    source_attempt = main.create_attempt("generation_result_image", image_ids[0], "image-2", source)
    main.finish_attempt(source_attempt, "FAILED")
    before = main.read_final_preparation(result_set)
    config = client.get("/api/prompt-configs").json()
    optimizer = next(item for item in config["items"] if item["key"] == OPTIMIZER_KEY)
    assert "lockedConstraints" in optimizer["suggestedContent"]
    preview = client.post(f"/api/prompt-configs/{OPTIMIZER_KEY}/preview", json={"content": "新规则", "attemptId": source_attempt})
    assert preview.status_code == 200, preview.text
    saved = client.patch(f"/api/prompt-configs/{OPTIMIZER_KEY}", json={"content": "新规则", "enabled": False})
    assert saved.status_code == 200 and not saved.json()["item"]["enabled"]
    calls, images = mock_success(monkeypatch)
    tested = client.post(f"/api/prompt-configs/{OPTIMIZER_KEY}/test-optimization", json={"content": "测试草稿", "attemptId": source_attempt})
    assert tested.status_code == 200, tested.text
    assert len(calls) == 1 and not images
    assert calls[0][1]["system_instruction"] == "测试草稿"
    assert tested.json()["items"][0]["originalPrompt"] == source["prompt"]
    assert main.read_final_preparation(result_set) == before
    assert main.get_task_attempt_detail(ids["task_id"], tested.json()["attemptId"])["requestFacts"]["invocation"]["mode"] == "optimization_test"
    monkeypatch.setattr(main, "get_request_role", lambda request: "user")
    assert client.post(f"/api/prompt-configs/{OPTIMIZER_KEY}/test-optimization", json={"content": "测试草稿", "attemptId": source_attempt}).status_code == 403
    assert len(calls) == 1


def test_purge_removes_optimization_attempts_with_their_result_set(client, monkeypatch):
    ids, result_set, image_ids = create_run(client)
    calls, images = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    with get_db() as db:
        main.purge_generation_tasks(db, [ids["task_id"]], "test_cleanup")
        assert db.execute("SELECT COUNT(*) FROM generation_attempts WHERE owner_type = 'generation_result_set'").fetchone()[0] == 0


def test_revision_changes_when_preparation_changes(client):
    ids, result_set, _ = create_run(client)
    previous = client.get(f"/api/generation-tasks/{ids['task_id']}/status").json()["detailRevision"]
    preparation = main.read_final_preparation(result_set)
    preparation["status"] = "RUNNING"
    main.save_final_preparation(result_set, preparation)
    assert client.get(f"/api/generation-tasks/{ids['task_id']}/status").json()["detailRevision"] > previous


def test_provider_recovery_bypasses_optimizer_and_keeps_original_request(client, monkeypatch):
    ids, result_set, image_ids = create_run(client)
    source = main.build_new_final_task_package(ids["task_id"], image_ids[0])
    source["prompt"] = "已提交的冻结提示词"
    attempt = main.create_attempt("generation_result_image", image_ids[0], "image-2", source)
    with get_db() as db:
        db.execute("UPDATE generation_attempts SET provider_request_id = 'existing-provider-id' WHERE id = ?", (attempt,))
        db.execute("UPDATE generation_result_images_new SET active_attempt_id = ? WHERE id = ?", (attempt, image_ids[0]))
        db.execute("UPDATE generation_result_images_new SET status = 'SUCCEEDED' WHERE result_set_id = ? AND id != ?", (result_set, image_ids[0]))
    calls, images = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, [image_ids[0]], {image_ids[0]: attempt}))
    assert not calls and images[0]["prompt"] == "已提交的冻结提示词"


def test_continue_uses_frozen_presets_even_when_live_presets_are_removed(client, monkeypatch):
    ids, result_set, image_ids = create_run(client)
    async def bad(*args, **kwargs): return '{"shots": []}'
    monkeypatch.setattr(main, "generate_gemini_json_text", bad)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    with get_db() as db:
        db.execute("DELETE FROM shot_preset_slots")
    response = asyncio.run(main.run_generate_final_images(ids["task_id"], continue_existing=True))
    assert response["item"]["activeResultSetId"] == result_set


def test_single_retry_preparation_failure_closes_pending_image_attempt(client, monkeypatch):
    ids, result_set, image_ids = create_run(client)
    mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    response = client.post(f"/api/generation-result-images-new/{image_ids[0]}/retry", json={"userInstruction": "修改要求"})
    attempt = response.json()["item"]["resultSet"]["images"][0]["activeAttemptId"]
    async def bad(*args, **kwargs): return '{"shots": []}'
    monkeypatch.setattr(main, "generate_gemini_json_text", bad)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, [image_ids[0]], {image_ids[0]: attempt}))
    with get_db() as db:
        row = db.execute("SELECT status, error_code FROM generation_attempts WHERE id = ?", (attempt,)).fetchone()
    assert row["status"] == "FAILED" and row["error_code"] == "PROMPT_PREPARATION_FAILED"
