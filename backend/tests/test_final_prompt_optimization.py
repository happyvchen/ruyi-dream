from __future__ import annotations

import asyncio
import copy

import pytest

from app import main
from app.db import dumps, get_db, loads, now_iso, init_db
from app.prompt_optimization import OPTIMIZER_KEY, optimizer_input, parse_optimized_prompts
from conftest import insert_media_asset, insert_ready_generation_task_with_preview


REAL_VISION_REFERENCE_INPUT = main.vision_reference_input

TEMPLATE = """生成一张棚拍成片，{{runtime.final_image_user_instruction}}
【参考图职责与优先级】
图1 是母版，图2 是姿势，图3 是商品。保留模特身份。
【当前镜头的商品表达】
当前为镜头 {{runtime.shot_index}}，保留商品结构。
【反伪影成像要求】
保留真实纹理，不添加颗粒。
"""

FLEXIBLE_TEMPLATE = """生成当前镜头 {{runtime.shot_index}} 的成片。
【要求】
保留商品结构。{{runtime.final_image_user_instruction}}
【参考图职责】
图1 是母版，图2 是姿势，图3 是商品。
"""


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("测试不得调用外部 AI")
    monkeypatch.setattr(main, "generate_gemini_json_text", forbidden)
    monkeypatch.setattr(main, "generate_image_asset", forbidden)
    monkeypatch.setattr(main, "snapshot_default_model", lambda: {})
    monkeypatch.setattr(main, "vision_reference_input", lambda item: {"data": item["objectKey"].encode(), "mime_type": "image/png"})
    monkeypatch.setattr(main, "build_direct_final_image_inputs", lambda *args: [
        {"role": role, "objectKey": f"tests/{role}.png"} for role in ("shot_background", "shot_pose", "hero_garment")
    ])


def create_run(client, enabled=True, template=TEMPLATE):
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content = ? WHERE key IN (?, ?, ?)", (template, *main.FINAL_IMAGE_PROMPT_CONFIG_KEYS.values()))
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
        return dumps({"shots": [{"shotId": shot["shotId"], "optimizedText": "输出单张商品棚拍图。"} for shot in inputs["shots"]]})

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
    assert len(calls[0][1]["images"]) == 3
    assert all([r["visionImageIndex"] for r in shot["references"]] == [1, 2, 3] for shot in calls[0][0]["shots"])
    assert len(images) == 5
    for package in images:
        assert package["prompt"] == "输出单张商品棚拍图。"
        assert "【参考图职责与优先级】" in package["originalPrompt"]
        assert "错误的新模板" not in package["prompt"]
        assert "changeSummary" not in package["prompt"]
    assert main.get_generation_task_row(ids["task_id"])["status"] == "COMPLETED"
    assert main.get_task_attempt_summary_page(ids["task_id"], "generation_result_set", 10)["total"] == 1


def test_single_retry_reuses_frozen_prompt_and_changes_only_selected_instruction(client, monkeypatch):
    ids, result_set, image_ids = create_run(client)
    calls, images = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    input_version = main.get_generation_task_row(ids["task_id"])["current_input_version_id"]
    first = copy.deepcopy(images[0])
    for instruction, expected_calls in [("", 1), ("请减少配饰", 1)]:
        response = client.post(f"/api/generation-result-images-new/{image_ids[0]}/retry", json={"userInstruction": instruction})
        assert response.status_code == 200, response.text
        attempt_id = response.json()["item"]["resultSet"]["images"][0]["activeAttemptId"]
        asyncio.run(main.run_final_generation(ids["task_id"], result_set, [image_ids[0]], {image_ids[0]: attempt_id}))
        assert len(calls) == expected_calls
    assert len(calls[-1][0]["shots"]) == 5
    assert all(package['imageInputs'] == first['imageInputs'] for package in images[5:])
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
    attempt_id = main.read_final_preparation(result_set)["attemptId"]
    with get_db() as db:
        assert db.execute("SELECT error_code FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()[0] == "MALFORMED_OUTPUT"
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
        return dumps({"shots": [{"shotId": shot["shotId"], "optimizedText": "输出单张棚拍图"} for shot in loads(prompt)["shots"]]})
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


@pytest.mark.parametrize("old_result", ["success", "failure"])
def test_cancelled_image_attempt_cannot_replace_restarted_image(client, monkeypatch, old_result):
    ids, result_set, image_ids = create_run(client, enabled=False)
    image_id = image_ids[0]
    restarted = []
    old_asset = insert_media_asset("late-old-result.png")

    async def generate(*args, **kwargs):
        main.cancel_generation_task(ids["task_id"])
        main.continue_active_final_result_set(ids["task_id"], result_set, ids["styling_id"], ids["preview_id"])
        new_attempt = main.create_attempt("generation_result_image", image_id, "image-2")
        main.lease_generation_result_image(image_id, new_attempt)
        restarted.append(new_attempt)
        if old_result == "failure":
            raise RuntimeError("old provider failed after restart")
        return old_asset, {"providerRequestId": "old-provider-task"}

    monkeypatch.setattr(main, "generate_image_asset", generate)
    asyncio.run(main.generate_single_final_image(ids["task_id"], result_set, image_id))
    with get_db() as db:
        row = db.execute("SELECT status, active_attempt_id, current_asset_id FROM generation_result_images_new WHERE id = ?", (image_id,)).fetchone()
        old = db.execute("SELECT status, output_asset_id FROM generation_attempts WHERE owner_type = 'generation_result_image' AND owner_id = ? ORDER BY id LIMIT 1", (image_id,)).fetchone()
    assert row["status"] == "RUNNING" and row["active_attempt_id"] == restarted[0]
    assert row["current_asset_id"] is None
    assert old["status"] == "CANCELLED"
    if old_result == "success":
        assert old["output_asset_id"] == old_asset


def test_cancel_optimization_during_transport_backoff_sends_no_more_requests(client, monkeypatch):
    from app import gemini_provider
    from app.generation_progress import ERROR_RATE_LIMITED

    ids, result_set, image_ids = create_run(client)
    calls = []
    monkeypatch.setattr(main.settings, "gemini_provider", "toapis")
    monkeypatch.setattr(main.settings, "toapis_api_key", "fake")
    monkeypatch.setattr(main, "generate_gemini_json_text", gemini_provider.generate_gemini_json_text)

    def post(*args, **kwargs):
        calls.append(1)
        raise gemini_provider.GeminiProviderError("429", ERROR_RATE_LIMITED)

    monkeypatch.setattr(gemini_provider, "_post_toapis_chat", post)
    monkeypatch.setattr(gemini_provider.time, "sleep", lambda seconds: main.cancel_generation_task(ids["task_id"]))
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    assert len(calls) == 1
    assert main.get_generation_task_row(ids["task_id"])["status"] == "CANCELLED"


@pytest.mark.parametrize("raw", [None, {}, {"shots": []}, {"shots": [{"shotId": "1", "optimizedText": " "}]}])
def test_unreadable_or_missing_text_remains_a_call_failure(raw):
    with pytest.raises(ValueError):
        parse_optimized_prompts(raw, {"1": {}})


def test_optimizer_output_is_forwarded_without_content_checks(client, monkeypatch):
    ids, result_set, image_ids = create_run(client)
    calls, images = mock_success(monkeypatch)
    # Legacy extra fields, apparent conflicts, references and placeholders are not gates.
    body = "  正背双视图，参考图8，{{原文表达}}。\n" + "商品要求" * 6100
    async def optimize(prompt, **kwargs):
        return dumps({"shots": [
            {"shotId": shot["shotId"], "optimizedText": body, "conflicts": ["双视图与不要拼图冲突"]}
            for shot in reversed(loads(prompt)["shots"])
        ]})
    monkeypatch.setattr(main, "generate_gemini_json_text", optimize)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    assert len(images) == 5
    assert all(image["prompt"] == body for image in images)
    assert main.get_generation_task_row(ids["task_id"])["status"] == "COMPLETED"


def test_migration_keeps_empty_disabled_slot_and_does_not_overwrite_saved_rules():
    with get_db() as db:
        row = db.execute("SELECT * FROM prompt_configs WHERE key = ?", (OPTIMIZER_KEY,)).fetchone()
        assert row["content"] == "" and row["enabled"] == 0
        db.execute("UPDATE prompt_configs SET content = '人工规则', enabled = 1 WHERE key = ?", (OPTIMIZER_KEY,))
    init_db()
    with get_db() as db:
        row = db.execute("SELECT * FROM prompt_configs WHERE key = ?", (OPTIMIZER_KEY,)).fetchone()
        assert row["content"] == "人工规则" and row["enabled"] == 1


@pytest.mark.parametrize("template", [TEMPLATE, FLEXIBLE_TEMPLATE])
def test_admin_preview_and_save_never_call_llm_test_is_explicit_and_preserves_task(client, monkeypatch, template):
    ids, result_set, image_ids = create_run(client, template=template)
    source = main.build_new_final_task_package(ids["task_id"], image_ids[0])
    source_attempt = main.create_attempt("generation_result_image", image_ids[0], "image-2", source)
    main.finish_attempt(source_attempt, "FAILED")
    before = main.read_final_preparation(result_set)
    config = client.get("/api/prompt-configs").json()
    optimizer = next(item for item in config["items"] if item["key"] == OPTIMIZER_KEY)
    assert "optimizedText" in optimizer["suggestedContent"]
    assert "lockedConstraints" not in optimizer["suggestedContent"]
    assert "conflicts" not in optimizer["suggestedContent"]
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


@pytest.mark.parametrize("template", [TEMPLATE, FLEXIBLE_TEMPLATE, "【锁定约束】\n\n【要求】\n保留商品。"])
def test_full_template_is_sent_without_extracting_sections(client, monkeypatch, template):
    ids, result_set, image_ids = create_run(client, template=template)
    calls, images = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    assert len(calls) == 1 and len(images) == 5
    sent = {shot["shotId"]: shot for shot in calls[0][0]["shots"]}
    for key, package in main.read_final_preparation(result_set)["packages"].items():
        assert sent[key]["sourcePrompt"] == package["originalPrompt"]
        assert "lockedConstraints" not in sent[key]
        assert package["prompt"] == "输出单张商品棚拍图。"


def test_continue_old_heading_failure_uses_frozen_flexible_template(client, monkeypatch):
    ids, result_set, image_ids = create_run(client, template=FLEXIBLE_TEMPLATE)
    # Reproduce an old failed result set without calling a provider or rewriting its snapshot.
    with monkeypatch.context() as old_code:
        def reject_old_headings(_packages):
            raise ValueError("成片模板的锁定约束标题缺失或重复")
        old_code.setattr(main, "optimizer_input", reject_old_headings)
        asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    assert main.read_final_preparation(result_set)["status"] == "FAILED"
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content = '后续模板' WHERE key = 'assembly.final_image_top'")
    pending = main.continue_active_final_result_set(ids["task_id"], result_set, ids["styling_id"], ids["preview_id"])
    calls, images = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, pending))
    assert len(calls) == 1 and len(images) == 5
    assert all("lockedConstraints" not in shot for shot in calls[0][0]["shots"])
    assert all("【参考图职责】" in image["originalPrompt"] and "后续模板" not in image["originalPrompt"] for image in images)
    assert main.get_generation_task_row(ids["task_id"])["status"] == "COMPLETED"


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


def test_single_retry_does_not_depend_on_optimizer_availability(client, monkeypatch):
    ids, result_set, image_ids = create_run(client)
    mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, image_ids))
    response = client.post(f"/api/generation-result-images-new/{image_ids[0]}/retry", json={"userInstruction": "修改要求"})
    attempt = response.json()["item"]["resultSet"]["images"][0]["activeAttemptId"]
    async def bad(*args, **kwargs): raise AssertionError('Frozen retries must not call the optimizer')
    monkeypatch.setattr(main, "generate_gemini_json_text", bad)
    asyncio.run(main.run_final_generation(ids["task_id"], result_set, [image_ids[0]], {image_ids[0]: attempt}))
    with get_db() as db:
        row = db.execute("SELECT status, error_code FROM generation_attempts WHERE id = ?", (attempt,)).fetchone()
    assert row["status"] == "SUCCEEDED" and row["error_code"] is None
    with get_db() as db:
        assert db.execute('SELECT count(*) FROM generation_image_versions WHERE image_id=?', (image_ids[0],)).fetchone()[0] == 2


def test_visual_mapping_keeps_local_numbers_and_deduplicates_shared_boards(monkeypatch):
    loaded = []
    monkeypatch.setattr(main, 'vision_reference_input', lambda item: loaded.append(item) or {'data': b'image', 'mime_type': 'image/png'})
    board = main.reference_sheet_image_input('hero_garment', [{'objectKey': 'a'}, {'objectKey': 'b'}], filename='board.jpg', max_items=2, board_kind='hero_overview')
    packages = {str(i): {'prompt': f'shot{i}', 'promptRuntime': {'shot_index': str(i)}, 'imageInputs': [
        {'role': 'shot_background', 'objectKey': 'background'},
        {'role': 'shot_pose', 'objectKey': f'pose{i}'},
        {'role': 'model_identity', 'objectKey': 'model'}, board,
        {'role': 'styling_garment', 'objectKey': 'clothing'},
    ]} for i in range(1, 6)}
    inputs = optimizer_input(packages)
    images = main.optimizer_visual_inputs(packages, inputs)
    assert len(images) == 9  # background + five poses + model + hero board + outfit
    assert len([item for item in loaded if item.get('collageItems')]) == 1
    for shot in inputs['shots']:
        assert [r['index'] for r in shot['references']] == [1, 2, 3, 4, 5]
        for reference in shot['references']:
            supplied = loaded[reference['visionImageIndex'] - 1]
            assert supplied == packages[shot['shotId']]['imageInputs'][reference['index'] - 1]


def test_optimizer_reads_actual_board_and_original_image_bytes(monkeypatch):
    from io import BytesIO
    from PIL import Image
    from app import image_provider
    # Restore the real adapter that the module's no-external-calls fixture replaces.
    monkeypatch.setattr(main, "vision_reference_input", REAL_VISION_REFERENCE_INPUT)
    # All image I/O is replaced below: no storage or external provider access.
    output = BytesIO()
    Image.new('RGB', (40, 60), (200, 100, 30)).save(output, format='PNG')
    content = output.getvalue()
    monkeypatch.setattr(image_provider, 'reference_source_bytes', lambda item, deadline=None: (content, 'image/png'))
    source = {'role': 'hero_garment', 'objectKey': 'original.png'}
    assert main.vision_reference_input(source)['data'] == content
    assert main.vision_reference_input(source)['mime_type'] == 'image/png'
    board = main.reference_sheet_image_input('hero_garment', [source, {**source, 'objectKey': 'detail.png'}], filename='board.jpg', max_items=2, board_kind='hero_overview')
    expected, _, mime = image_provider.build_reference_collage_bytes(board)
    actual = main.vision_reference_input(board)
    assert actual['data'] == expected and actual['mime_type'] == mime
    with Image.open(BytesIO(actual['data'])) as decoded:
        assert decoded.width > 0 and decoded.height > 0


def fourteen_inputs(_styling, _version, preset):
    return [
        {'role': 'shot_background', 'objectKey': 'background'},
        {'role': 'shot_pose', 'objectKey': f'pose-{preset["shot_index"]}'},
        *[{'role': 'model_identity', 'objectKey': f'model-{i}'} for i in range(2)],
        *[{'role': 'hero_garment', 'objectKey': f'hero-{i}'} for i in range(6)],
        *[{'role': 'styling_garment', 'objectKey': f'styling-{i}'} for i in range(4)],
    ]


def test_full_fourteen_reference_group_splits_optimizer_and_preserves_frozen_retries(client, monkeypatch):
    monkeypatch.setattr(main, 'build_direct_final_image_inputs', fourteen_inputs)
    ids, group, image_ids = create_run(client)
    calls, images = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids['task_id'], group, image_ids))
    assert [len(call[0]['shots']) for call in calls] == [3, 2]
    assert [len(call[1]['images']) for call in calls] == [16, 15]
    assert len(images) == 5 and all(len(p['imageInputs']) == 14 for p in images)
    for inputs, kwargs in calls:
        for shot in inputs['shots']:
            assert [r['index'] for r in shot['references']] == list(range(1, 15))
            assert max(r['visionImageIndex'] for r in shot['references']) <= len(kwargs['images'])
    attempt = main.read_final_preparation(group)['attemptId']
    with get_db() as db:
        row = db.execute('SELECT * FROM generation_attempts WHERE id=?', (attempt,)).fetchone()
    assert len(loads(row['request_snapshot_json'])['batchRequests']) == 2
    assert len(loads(row['response_snapshot_json'])['shots']) == 5
    response = client.post(f'/api/generation-result-images-new/{image_ids[0]}/retry')
    assert response.status_code == 200
    active = response.json()['item']['resultSet']['images'][0]['activeAttemptId']
    asyncio.run(main.run_final_generation(ids['task_id'], group, [image_ids[0]], {image_ids[0]: active}))
    assert len(calls) == 2
    assert images[-1]['imageInputs'] == images[0]['imageInputs']
    assert images[-1]['prompt'] == images[0]['prompt']


@pytest.mark.parametrize('interrupt', ['failure', 'cancel'])
def test_optimizer_batch_progress_survives_failure_or_cancel(client, monkeypatch, interrupt):
    monkeypatch.setattr(main, 'build_direct_final_image_inputs', fourteen_inputs)
    ids, group, image_ids = create_run(client)
    calls = []
    async def optimize(prompt, **kwargs):
        shots = loads(prompt)['shots']
        calls.append(shots)
        if interrupt == 'failure' and len(calls) == 2:
            raise RuntimeError('second batch unavailable')
        if interrupt == 'cancel':
            with get_db() as db:
                main.cancel_running_task_attempts(db, ids['task_id'], now_iso())
                db.execute("UPDATE generation_tasks SET status='CANCELLED' WHERE id=?", (ids['task_id'],))
                db.execute("UPDATE generation_result_sets SET status='CANCELLED' WHERE id=?", (group,))
                db.execute("UPDATE generation_result_images_new SET status='CANCELLED' WHERE result_set_id=?", (group,))
        return dumps({'shots': [{'shotId': shot['shotId'], 'optimizedText': 'saved first batch'} for shot in shots]})
    monkeypatch.setattr(main, 'generate_gemini_json_text', optimize)
    asyncio.run(main.run_final_generation(ids['task_id'], group, image_ids))
    assert len(calls) == (2 if interrupt == 'failure' else 1)
    assert len(main.read_final_preparation(group)['packages']) == 3
    pending = main.continue_active_final_result_set(ids['task_id'], group, ids['styling_id'], ids['preview_id'])
    resumed, images = mock_success(monkeypatch)
    asyncio.run(main.run_final_generation(ids['task_id'], group, pending))
    assert len(resumed) == 1 and len(resumed[0][0]['shots']) == 2
    assert len(images) == 5
    assert all(images[i]['prompt'] == 'saved first batch' for i in range(3))


def test_optimizer_keeps_different_crops_distinct_and_reads_cropped_pixels(monkeypatch):
    from io import BytesIO
    from PIL import Image
    output = BytesIO()
    Image.new('RGB', (100, 200), 'red').save(output, 'PNG')
    from app import image_provider
    monkeypatch.setattr(image_provider, 'reference_source_bytes', lambda *args: (output.getvalue(), 'image/png'))
    monkeypatch.setattr(main, 'vision_reference_input', REAL_VISION_REFERENCE_INPUT)
    packages = {str(i): {'prompt': 'pose', 'promptRuntime': {'shot_index': str(i)}, 'imageInputs': [
        {'role': 'shot_pose', 'objectKey': 'same.png', 'providerCrop': {'kind': 'keep_top_ratio', 'ratio': ratio}},
    ]} for i, ratio in enumerate([0.45, 0.78], 1)}
    inputs = optimizer_input(packages)
    images = main.optimizer_visual_inputs(packages, inputs)
    assert [Image.open(BytesIO(x['data'])).size for x in images] == [(100, 90), (100, 156)]


def test_canvas_policy_frozen_for_new_sets_but_not_legacy(client, monkeypatch):
    from app.image_provider import SQUARE_WHITE_CANVAS
    from app.prompt_optimization import optimizer_reference_identity
    ids, result_set, image_ids = create_run(client)
    preparation = main.read_final_preparation(result_set)
    assert preparation['referenceCanvas'] == SQUARE_WHITE_CANVAS
    monkeypatch.setattr(main, 'build_direct_final_image_inputs', lambda *args: [
        {'role': 'model_identity', 'collageItems': [{'objectKey': 'portrait.png'}]},
    ])
    package = main.build_new_final_task_package(ids['task_id'], image_ids[0])
    item = package['imageInputs'][0]
    assert item['providerCanvas'] == item['collageItems'][0]['providerCanvas'] == SQUARE_WHITE_CANVAS
    del preparation['referenceCanvas']
    main.save_final_preparation(result_set, preparation)
    legacy = main.build_new_final_task_package(ids['task_id'], image_ids[0])['imageInputs'][0]
    assert 'providerCanvas' not in legacy and 'providerCanvas' not in legacy['collageItems'][0]
    assert optimizer_reference_identity(item) != optimizer_reference_identity(legacy)
