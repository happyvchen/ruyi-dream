from __future__ import annotations

import asyncio
import json

import pytest

from app import main as app_main
from app.db import dumps, get_db, init_db, now_iso
from app.pose_selection import pose_plan_from_shortlist, validate_pose_plan, validate_pose_shortlist
from conftest import insert_media_asset, insert_ready_generation_task_with_preview


def seed_pose(name="自然站姿", status="READY"):
    asset_id = insert_media_asset(f"{name}.jpg")
    with get_db() as db:
        pose_id = int(db.execute("INSERT INTO poses (code, name, status, ai_tags_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"POSE-{asset_id}", name, status, dumps({"核心动作": ["一手自然下垂"]}), now_iso(), now_iso())).lastrowid)
        db.execute("INSERT INTO pose_images (pose_id, media_asset_id) VALUES (?, ?)", (pose_id, asset_id))
    return pose_id, asset_id, f"POSE-{asset_id}"


def prepare_task():
    ids = insert_ready_generation_task_with_preview(status="STYLING_READY")
    background = insert_media_asset("shared-background.jpg")
    with get_db() as db:
        db.execute("INSERT INTO shoot_environment_references (media_asset_id, created_at, updated_at) VALUES (?, ?, ?)", (background, now_iso(), now_iso()))
        db.execute("UPDATE prompt_configs SET content = '镜头{{runtime.shot_index}} {{runtime.shot_styling}}' WHERE key LIKE 'assembly.final_image_%'")
        db.execute("UPDATE generation_tasks SET pose_workflow_enabled = 1 WHERE id = ?", (ids["task_id"],))
        db.execute("UPDATE prompt_configs SET content = ? WHERE key = 'assembly.pose_selection'",
            ("{{runtime.pose_phase}}\n{{runtime.pose_candidates}}\n{{runtime.pose_image_manifest}}\n{{runtime.pose_shots}}",))
        db.execute("UPDATE prompt_configs SET content = '{{runtime.pose_context}}' WHERE key = 'assembly.pose_tagging'")
        hero_id = db.execute("SELECT media_asset_id FROM generation_task_images WHERE task_id = ? LIMIT 1", (ids["task_id"],)).fetchone()[0]
    hero = app_main.get_media_asset_by_id(hero_id)
    with get_db() as db:
        db.execute("UPDATE generation_task_input_versions SET image_snapshot_json = ? WHERE task_id = ?", (dumps([{"asset": hero, "sortOrder": 0}]), ids["task_id"]))
    return ids


def mock_selection(monkeypatch, code, image_id, calls=None, after_shortlist=None):
    calls = calls if calls is not None else []
    monkeypatch.setattr(app_main, "vision_reference_input", lambda item: {"key": item["objectKey"]})
    choices = [(code, image_id)] + [(code, asset) for _, asset, code in [seed_pose(f"备用姿势{n}") for n in range(2, 6)]]
    async def generate(prompt, images, **kwargs):
        calls.append((prompt, images))
        assert prompt.startswith("shortlist")
        assert len(calls) == 1
        if after_shortlist: after_shortlist()
        return dumps({"summary": "五镜头姿势", "shots": [{"shotIndex": n, "poseCode": choices[n-1][0], "imageId": choices[n-1][1],
            "reason": "与镜头和商品适配", "direction": f"镜头{n}保持原图肩胸支撑与手部接触"} for n in range(1, 6)]})
    monkeypatch.setattr(app_main, "generate_gemini_json_text", generate)
    return calls


def run_selection(task_id):
    return asyncio.run(app_main.run_select_generation_poses(task_id, trigger="manual_step"))


def test_pose_migration_is_idempotent_and_keeps_prompt_slots_empty():
    pose_id, image_id, _ = seed_pose()
    init_db()
    with get_db() as db:
        assert db.execute("SELECT id FROM poses WHERE id = ?", (pose_id,)).fetchone()
        assert db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.pose_selection'").fetchone()[0] == ""
        assert db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = '076'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = '077'").fetchone()[0] == 1


def test_new_tasks_require_pose_selection_and_legacy_tasks_keep_their_workflow(client, monkeypatch):
    _, asset_id, _ = seed_pose()
    async def save(_files): return [app_main.get_media_asset_by_id(asset_id)]
    monkeypatch.setattr(app_main, "save_uploads_as_media_assets", save)
    created = client.post('/api/generation-tasks', data={"name": "NEW"}, files={"files": ("hero.jpg", b"image", "image/jpeg")})
    assert created.status_code == 200
    assert created.json()["item"]["poseWorkflowEnabled"] is True
    legacy = insert_ready_generation_task_with_preview(status="STYLING_READY")
    assert app_main.get_generation_task_by_id(legacy["task_id"])["poseWorkflowEnabled"] is False


def test_pose_upload_and_read_do_not_call_ai(client, monkeypatch):
    asset_id = insert_media_asset()
    async def save(_files): return [app_main.get_media_asset_by_id(asset_id)]
    async def forbidden(*_args, **_kwargs): raise AssertionError("read/upload must not call AI")
    monkeypatch.setattr(app_main, "save_uploads_as_media_assets", save)
    monkeypatch.setattr(app_main, "generate_gemini_json_text", forbidden)
    result = client.post('/api/poses', data={"name": " 行走 "}, files={"files": ("pose.jpg", b"image", "image/jpeg")})
    assert result.status_code == 200
    item = result.json()["item"]
    assert item["name"] == "行走" and item["status"] == "DRAFT"
    assert client.get('/api/poses?q=行走').json()["total"] == 1
    assert client.get(f'/api/poses/{item["id"]}').status_code == 200


@pytest.mark.parametrize("status", ["DRAFT", "READY"])
def test_pose_recognition_is_explicit_and_saves_name_and_tags(client, monkeypatch, status):
    monkeypatch.setattr(app_main.settings, "gemini_provider", "toapis")
    monkeypatch.setattr(app_main.settings, "gemini_toapis_model", "deepseek-v4.1-flash")
    pose_id, _, code = seed_pose(name="原始文件名", status=status)
    prepare_task()
    monkeypatch.setattr(app_main, "gemini_vision_input", lambda *_: {"image": "pose"})
    raw = {"name": "  正面插兜站姿  ", "tags": {"姿势描述": "正对镜头，左手插兜", "核心动作": ["左手插兜"], "扩展标签": ["按原样保留"]}}
    async def generate(*_args): return dumps(raw)
    monkeypatch.setattr(app_main, "generate_gemini_json_text", generate)
    response = client.post(f'/api/poses/{pose_id}/recognize')
    assert response.status_code == 200 and response.json()["item"]["status"] == "TAGGING_RUNNING"
    assert client.post(f'/api/poses/{pose_id}/recognize').status_code == 409
    with get_db() as db:
        attempt = db.execute("SELECT * FROM generation_attempts WHERE owner_type = 'pose_tagging'").fetchone()
    snapshot = json.loads(attempt["request_snapshot_json"])
    assert attempt["provider"] == snapshot["provider"] == "deepseek"
    assert snapshot["model"] == "deepseek-v4.1-flash" and snapshot["channel"] == "toapis"
    asyncio.run(app_main.run_pose_recognition(pose_id, attempt["id"], snapshot["pose"], snapshot["prompt"]))
    item = client.get(f'/api/poses/{pose_id}').json()["item"]
    assert item["name"] == "正面插兜站姿" and item["code"] == code
    assert item["aiTags"] == raw["tags"] and item["status"] == "READY"
    assert client.get('/api/poses?q=正面插兜站姿').json()["total"] == 1
    with get_db() as db:
        row = db.execute("SELECT * FROM poses WHERE id = ?", (pose_id,)).fetchone()
        saved_attempt = db.execute("SELECT * FROM generation_attempts WHERE id = ?", (attempt["id"],)).fetchone()
    assert row["name"] == "正面插兜站姿" and json.loads(row["ai_tags_json"]) == raw["tags"]
    assert saved_attempt["status"] == "SUCCEEDED" and json.loads(saved_attempt["response_snapshot_json"]) == raw



def test_pose_selection_views_only_products_background_and_freezes_five_shots(client, monkeypatch):
    monkeypatch.setattr(app_main.settings, "gemini_provider", "toapis")
    monkeypatch.setattr(app_main.settings, "gemini_toapis_model", "deepseek-v4.1-flash")
    ids = prepare_task()
    pose_id, asset_id, code = seed_pose()
    calls = mock_selection(monkeypatch, code, asset_id)
    before = app_main.get_generation_task_by_id(ids["task_id"])["detailRevision"]
    task = run_selection(ids["task_id"])["item"]
    assert task["status"] == "POSE_SELECTION_READY"
    with get_db() as db:
        attempt = db.execute("SELECT * FROM generation_attempts WHERE owner_type = 'generation_pose_selection'").fetchone()
    snapshot = json.loads(attempt["request_snapshot_json"])
    assert attempt["provider"] == snapshot["provider"] == "deepseek"
    assert snapshot["model"] == "deepseek-v4.1-flash" and snapshot["channel"] == "toapis"
    assert len(calls) == 1 and len(calls[0][1]) == 2  # hero + shared background, no pose originals
    assert len(task["poseSelection"]["shots"]) == 5
    assert task["detailRevision"] > before
    assert app_main.next_auto_final_step(app_main.get_generation_task_row(ids["task_id"])) == "final"
    assert client.get(f'/api/generation-tasks/{ids["task_id"]}/attempts?ownerType=generation_pose_selection').json()["total"] == 1
    old_master = task["poseSelection"]["shots"][0]["background"]["id"]
    replacement = insert_media_asset('new-master.jpg')
    with get_db() as db:
        db.execute("UPDATE shot_preset_slots SET background_asset_id = ?, pose_asset_id = NULL WHERE category_family = 'top' AND shot_index = 1", (replacement,))
    generated = client.post(f'/api/generation-tasks/{ids["task_id"]}/generate-final')
    assert generated.status_code == 200
    with get_db() as db:
        result = db.execute("SELECT * FROM generation_result_sets WHERE id = ?", (generated.json()["item"]["activeResultSetId"],)).fetchone()
        shots = db.execute("SELECT * FROM generation_result_set_shot_presets WHERE result_set_id = ? ORDER BY shot_index", (result["id"],)).fetchall()
    assert result["pose_selection_id"] == task["poseSelection"]["id"]
    assert shots[0]["background_asset_id"] == old_master
    assert len({row["pose_asset_id"] for row in shots}) == 5
    assert shots[0]["pose_asset_id"] == asset_id
    assert {row["background_asset_id"] for row in shots} == {old_master}
    assert result["input_version_id"] == app_main.get_generation_task_row(ids["task_id"])["current_input_version_id"]


def test_new_final_generation_cannot_skip_pose_selection(client):
    ids = prepare_task()
    assert client.post(f'/api/generation-tasks/{ids["task_id"]}/generate-final').status_code == 409
    assert app_main.next_auto_final_step(app_main.get_generation_task_row(ids["task_id"])) == "pose"


def test_empty_or_archived_pose_library_cannot_start_selection(client):
    ids = prepare_task()
    pose_id, _, _ = seed_pose()
    client.post('/api/poses/manage', json={"ids": [pose_id], "action": "archive"})
    response = client.post(f'/api/generation-tasks/{ids["task_id"]}/select-poses')
    assert response.status_code == 409 and "暂无" in response.json()["detail"]
    assert app_main.get_generation_task_row(ids["task_id"])["status"] == "STYLING_READY"


def test_cancel_after_shortlist_never_calls_visual_or_creates_selection(monkeypatch):
    ids = prepare_task()
    _, asset_id, code = seed_pose()
    calls = mock_selection(monkeypatch, code, asset_id, after_shortlist=lambda: app_main.cancel_generation_task(ids["task_id"]))
    task = run_selection(ids["task_id"])["item"]
    assert task["status"] == "CANCELLED" and task["poseSelection"] is None and len(calls) == 1


def test_pose_cancel_during_transport_backoff_stops_next_request(monkeypatch):
    from app import gemini_provider
    from app.generation_progress import ERROR_RATE_LIMITED

    ids = prepare_task()
    for index in range(5):
        seed_pose(f"cancel-pose-{index}")
    monkeypatch.setattr(app_main.settings, "gemini_provider", "toapis")
    monkeypatch.setattr(app_main.settings, "toapis_api_key", "fake")
    monkeypatch.setattr(app_main, "vision_reference_input", lambda item: {"data": b"test", "mime_type": "image/png"})
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        raise gemini_provider.GeminiProviderError("429", ERROR_RATE_LIMITED)

    monkeypatch.setattr(gemini_provider, "_post_toapis_chat", post)
    monkeypatch.setattr(gemini_provider.time, "sleep", lambda seconds: app_main.cancel_generation_task(ids["task_id"]))
    task = run_selection(ids["task_id"])["item"]
    assert len(calls) == 1
    assert task["status"] == "CANCELLED" and task["poseSelection"] is None


def test_archival_during_selection_is_rejected(monkeypatch):
    ids = prepare_task()
    pose_id, asset_id, code = seed_pose()
    mock_selection(monkeypatch, code, asset_id, after_shortlist=lambda: app_main.manage_poses(app_main.PoseManageRequest(ids=[pose_id], action="archive")))
    task = run_selection(ids["task_id"])["item"]
    assert task["status"] == "FAILED" and task["poseSelection"] is None


def test_pose_purge_preserves_historical_images_and_task(client, monkeypatch):
    ids = prepare_task()
    pose_id, asset_id, code = seed_pose()
    mock_selection(monkeypatch, code, asset_id)
    run_selection(ids["task_id"])
    impact = client.post('/api/poses/delete-impact', json={"ids": [pose_id]}).json()
    assert impact["referencingTaskCount"] == 1 and impact["mediaAssetCount"] == 0
    assert client.post('/api/poses/manage', json={"ids": [pose_id], "action": "purge", "confirmPhrase": "wrong"}).status_code == 400
    result = client.post('/api/poses/manage', json={"ids": [pose_id], "action": "purge", "confirmPhrase": "自然站姿"})
    assert result.status_code == 200
    task = app_main.get_generation_task_by_id(ids["task_id"])
    assert task["poseSelection"]["shots"][0]["sourceAvailable"] is False
    assert app_main.get_media_asset_by_id(asset_id)
    assert client.post(f'/api/generation-tasks/{ids["task_id"]}/generate-final').status_code == 200


def test_input_change_invalidates_pose_selection_and_preserves_history(monkeypatch):
    ids = prepare_task()
    _, asset_id, code = seed_pose()
    mock_selection(monkeypatch, code, asset_id)
    selected = run_selection(ids["task_id"])["item"]
    with get_db() as db:
        app_main.mark_generation_task_input_changed(db, ids["task_id"], now_iso(), "test")
    task = app_main.get_generation_task_by_id(ids["task_id"])
    assert task["poseSelection"] is None
    assert app_main.get_generation_pose_selection(selected["poseSelection"]["id"])


def test_purge_task_after_pose_selection_cleans_own_aggregate(client, monkeypatch):
    ids = prepare_task()
    pose_id, asset_id, code = seed_pose()
    mock_selection(monkeypatch, code, asset_id)
    run_selection(ids["task_id"])
    response = client.request('DELETE', f'/api/generation-tasks/{ids["task_id"]}', json={"purge": True, "confirmName": "READY-LOOK"})
    assert response.status_code == 200, response.text
    assert client.get(f'/api/poses/{pose_id}').status_code == 200
    assert app_main.get_media_asset_by_id(asset_id)


def test_interrupted_pose_workflow_can_resume(monkeypatch):
    ids = prepare_task()
    for n in range(5): seed_pose(f"姿势{n}")
    app_main.claim_pose_selection(ids["task_id"], None, "manual_step")
    app_main.recover_interrupted_pose_jobs()
    row = app_main.get_generation_task_row(ids["task_id"])
    assert row["status"] == "FAILED"
    with get_db() as db:
        assert app_main.interrupted_generation_resume_status(db, ids["task_id"], "FAILED") == "STYLING_READY"


def test_pose_reference_drafts_are_admin_only_and_do_not_save(client, monkeypatch):
    monkeypatch.setattr(app_main.settings, 'user_password', 'test-user')
    assert client.get('/api/poses').status_code == 401
    client.cookies.set('ruyi_session', app_main.create_session_cookie('user'))
    assert client.get('/api/poses').status_code == 200
    assert client.get('/api/pose-prompt-drafts/tagging').status_code == 403
    client.cookies.set('ruyi_session', app_main.create_session_cookie('admin'))
    assert client.get('/api/pose-prompt-drafts/tagging').status_code == 200
    with get_db() as db:
        assert db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.pose_tagging'").fetchone()[0] == ''


def test_pose_schema_rejects_missing_shots_unknown_codes_and_cross_pose_images():
    shortlist = {"shots": [{"shotIndex": i, "candidateCodes": ["P"]} for i in range(1, 6)]}
    valid = validate_pose_shortlist(shortlist, {"P"})
    with pytest.raises(ValueError): validate_pose_shortlist(shortlist, {"Q"})
    with pytest.raises(ValueError): validate_pose_shortlist({"shots": shortlist["shots"][:-1]}, {"P"})
    plan = {"shots": [{"shotIndex": i, "poseCode": "P", "imageId": 9, "reason": "适配", "direction": "动作"} for i in range(1, 6)]}
    with pytest.raises(ValueError): validate_pose_plan(plan, {"P": {8}})
    with pytest.raises(ValueError, match="五个不同姿势"): validate_pose_plan(plan, {"P": {9}})
    for i, shot in enumerate(plan["shots"], 1):
        shot["poseCode"] = f"P{i}"
        shot["imageId"] = i
    images = {f"P{i}": {i} for i in range(1, 6)}
    assert len(validate_pose_plan(plan, images)) == 5
    with pytest.raises(ValueError): validate_pose_plan({"shots": plan["shots"][:-1]}, images)
    with pytest.raises(ValueError): validate_pose_plan(plan, {"Q": {9}})
    plan["shots"][0]["imageId"] = 2
    with pytest.raises(ValueError, match="图片不属于"): validate_pose_plan(plan, images)


def test_recognition_dispatch_runs_on_an_event_loop(client, monkeypatch):
    pose_id, _, _ = seed_pose(status="DRAFT")
    prepare_task()
    dispatched = []
    def dispatch(coro, **kwargs):
        try:
            dispatched.append(asyncio.get_running_loop())
        finally:
            coro.close()
    monkeypatch.setattr(app_main, "schedule_background_generation", dispatch)
    assert client.post(f'/api/poses/{pose_id}/recognize').status_code == 200
    assert dispatched


def test_purge_task_releases_deleted_pose_only_after_last_reference(client, monkeypatch):
    ids = prepare_task()
    pose_id, asset_id, code = seed_pose()
    mock_selection(monkeypatch, code, asset_id)
    run_selection(ids["task_id"])
    assert client.post('/api/poses/manage', json={"ids": [pose_id], "action": "purge", "confirmPhrase": "自然站姿"}).status_code == 200
    assert app_main.get_media_asset_by_id(asset_id)
    response = client.request('DELETE', f'/api/generation-tasks/{ids["task_id"]}', json={"purge": True, "confirmName": "READY-LOOK"})
    assert response.status_code == 200, response.text
    assert app_main.get_media_asset_by_id(asset_id) is None


def test_pose_selection_claim_rejects_duplicate_requests(client, monkeypatch):
    ids = prepare_task()
    for n in range(5): seed_pose(f"姿势{n}")
    first = client.post(f'/api/generation-tasks/{ids["task_id"]}/select-poses')
    assert first.status_code == 200
    assert first.json()["item"]["status"] == "POSE_SELECTION_RUNNING"
    assert client.post(f'/api/generation-tasks/{ids["task_id"]}/select-poses').status_code == 409
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM generation_attempts WHERE owner_type = 'generation_pose_selection'").fetchone()[0] == 1


def test_failed_pose_reselection_retries_pose_instead_of_reusing_old_selection(monkeypatch):
    ids = prepare_task()
    _, asset_id, code = seed_pose()
    mock_selection(monkeypatch, code, asset_id)
    first = run_selection(ids['task_id'])['item']['poseSelection']['id']
    async def fail(*_args): raise RuntimeError('temporary network failure')
    monkeypatch.setattr(app_main, 'generate_gemini_json_text', fail)
    second = run_selection(ids['task_id'])['item']
    assert second['poseSelection']['id'] == first
    assert second['status'] == 'FAILED'
    assert app_main.next_auto_final_step(app_main.get_generation_task_row(ids['task_id'])) == 'pose'


def test_final_prompt_receives_selected_direction_without_legacy_pose_slots(client, monkeypatch):
    ids = prepare_task()
    _, asset_id, code = seed_pose()
    with get_db() as db:
        db.execute("UPDATE shot_preset_slots SET pose_asset_id = NULL")
        db.execute(
            "UPDATE prompt_configs SET content = '{{runtime.shot_styling}}{{runtime.shot_pose}}' WHERE key = 'assembly.final_image_top'"
        )
    mock_selection(monkeypatch, code, asset_id)
    run_selection(ids['task_id'])
    response = client.post(f'/api/generation-tasks/{ids["task_id"]}/generate-final')
    assert response.status_code == 200, response.text
    image_id = response.json()['item']['resultSet']['images'][0]['id']
    captured = []
    def inputs(styling_id, input_id, preset):
        captured.append(dict(preset))
        return []
    monkeypatch.setattr(app_main, 'build_direct_final_image_inputs', inputs)
    package = app_main.build_new_final_task_package(ids['task_id'], image_id)
    assert captured[0]['pose_asset_id'] == asset_id
    assert '镜头1保持原图肩胸支撑与手部接触' in package['prompt']
    assert package['promptRuntime']['shot_pose'] == '镜头1保持原图肩胸支撑与手部接触'


def test_single_image_pose_rejects_multiple_uploads_append_and_removal(client, monkeypatch):
    pose_id, asset_id, _ = seed_pose()
    async def forbidden(_files): raise AssertionError("invalid request must not upload")
    monkeypatch.setattr(app_main, "save_uploads_as_media_assets", forbidden)
    files = [("files", ("a.jpg", b"a", "image/jpeg")), ("files", ("b.jpg", b"b", "image/jpeg"))]
    assert client.post('/api/poses', data={"name": "不合并"}, files=files).status_code == 400
    assert client.put(f'/api/poses/{pose_id}/image', files=files).status_code == 400
    assert client.post(f'/api/poses/{pose_id}/images', files=files[:1]).status_code == 409
    image_id = app_main.get_pose(pose_id)["item"]["images"][0]["id"]
    assert client.delete(f'/api/poses/{pose_id}/images/{image_id}').status_code == 409
    assert app_main.get_pose(pose_id)["item"]["images"][0]["asset"]["id"] == asset_id


def test_replacing_single_image_invalidates_tags_but_preserves_frozen_history(client, monkeypatch):
    ids = prepare_task()
    pose_id, asset_id, code = seed_pose()
    mock_selection(monkeypatch, code, asset_id)
    before = run_selection(ids["task_id"])["item"]["poseSelection"]
    new_asset = insert_media_asset('replacement.jpg')
    async def save(_files): return [app_main.get_media_asset_by_id(new_asset)]
    async def forbidden(*_args, **_kwargs): raise AssertionError("replacement must not call AI")
    monkeypatch.setattr(app_main, "save_uploads_as_media_assets", save)
    monkeypatch.setattr(app_main, "generate_gemini_json_text", forbidden)
    response = client.put(f'/api/poses/{pose_id}/image', files={"files": ("replacement.jpg", b"image", "image/jpeg")})
    assert response.status_code == 200, response.text
    item = response.json()["item"]
    assert len(item["images"]) == 1 and item["images"][0]["asset"]["id"] == new_asset
    assert item["status"] == "DRAFT"
    assert app_main.get_media_asset_by_id(asset_id)
    assert app_main.get_generation_task_by_id(ids["task_id"])["poseSelection"] == before


def test_single_image_migration_splits_legacy_poses_without_losing_media_or_history(monkeypatch):
    import sqlite3
    from app.db import migration_single_image_poses
    ids = prepare_task()
    pose_id, first_asset, code = seed_pose()
    second_asset = insert_media_asset('second.jpg')
    with get_db() as db:
        db.execute("DROP INDEX idx_pose_single_image")
        db.execute("INSERT INTO pose_images (pose_id, media_asset_id, sort_order) VALUES (?, ?, 1)", (pose_id, second_asset))
    mock_selection(monkeypatch, code, second_asset)
    selection = run_selection(ids["task_id"])["item"]["poseSelection"]
    attempt_id = app_main.create_attempt("pose_tagging", pose_id, "gemini", {"legacy": "snapshot"})
    with get_db() as db:
        db.execute("UPDATE poses SET status = 'TAGGING_RUNNING', tag_attempt_id = ? WHERE id = ?", (attempt_id, pose_id))
        snapshots = [row[0] for row in db.execute("SELECT pose_snapshot_json FROM generation_pose_selection_shots")]
        db.execute("UPDATE poses SET archived_at = '2026-10-01', archive_reason = 'user_archived' WHERE id = ?", (pose_id,))
        migration_single_image_poses(db)
        migration_single_image_poses(db)
        poses = db.execute("SELECT * FROM poses WHERE archived_at IS NOT NULL ORDER BY id").fetchall()
        links = db.execute("SELECT * FROM pose_images WHERE media_asset_id IN (?, ?) ORDER BY id", (first_asset, second_asset)).fetchall()
        assert len(poses) == 2 and len(links) == 2
        assert poses[0]["id"] == pose_id and poses[0]["code"] == code
        assert all(row["status"] == 'DRAFT' and row["archived_at"] == '2026-10-01' and row["ai_tags_json"] is None for row in poses)
        assert {row["media_asset_id"] for row in links} == {first_asset, second_asset}
        attempt = db.execute("SELECT * FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
        assert attempt["status"] == "FAILED" and json.loads(attempt["request_snapshot_json"]) == {"legacy": "snapshot"}
        assert [row[0] for row in db.execute("SELECT pose_snapshot_json FROM generation_pose_selection_shots")] == snapshots
        assert {row[0] for row in db.execute("SELECT pose_id FROM generation_pose_selection_shots WHERE shot_index = 1")} == {poses[1]["id"]}
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO pose_images (pose_id, media_asset_id) VALUES (?, ?)", (pose_id, second_asset))
    assert app_main.get_media_asset_by_id(first_asset) and app_main.get_media_asset_by_id(second_asset)
    assert app_main.get_generation_task_by_id(ids["task_id"])["poseSelection"]["shots"] == selection["shots"]


@pytest.mark.parametrize("raw", [
    {"tags": {"动作": "站姿"}},
    {"name": "   ", "tags": {"动作": "站姿"}},
    {"name": 123, "tags": {"动作": "站姿"}},
    {"name": "姿" * 81, "tags": {"动作": "站姿"}},
    {"name": "新名字"},
    {"name": "新名字", "tags": {}},
    {"name": "新名字", "tags": ["站姿"]},
    {"name": "新名字", "tags": None},
    {"name": "新名字", "ai_tags": {"动作": "旧格式"}},
])
def test_invalid_pose_recognition_keeps_existing_name_and_tags(client, monkeypatch, raw):
    pose_id, _, _ = seed_pose(name="保留原名")
    original = app_main.get_pose(pose_id)["item"]
    prepare_task()
    monkeypatch.setattr(app_main, "gemini_vision_input", lambda *_: {"image": "pose"})
    async def generate(*_args): return dumps(raw)
    monkeypatch.setattr(app_main, "generate_gemini_json_text", generate)
    assert client.post(f'/api/poses/{pose_id}/recognize').status_code == 200
    with get_db() as db:
        attempt = db.execute("SELECT * FROM generation_attempts WHERE owner_type = 'pose_tagging'").fetchone()
    snapshot = json.loads(attempt["request_snapshot_json"])
    asyncio.run(app_main.run_pose_recognition(pose_id, attempt["id"], snapshot["pose"], snapshot["prompt"]))
    item = app_main.get_pose(pose_id)["item"]
    assert item["status"] == "TAGGING_FAILED"
    assert item["name"] == original["name"] and item["aiTags"] == original["aiTags"]
    with get_db() as db:
        assert db.execute("SELECT status FROM generation_attempts WHERE id = ?", (attempt["id"],)).fetchone()[0] == 'FAILED'


@pytest.mark.parametrize('family', ['top', 'bottom', 'one_piece'])
def test_shared_flow_needs_no_masters_and_freezes_rules_background_and_input(client, monkeypatch, family):
    from pathlib import Path
    ids = prepare_task()
    _, asset, code = seed_pose()
    calls = mock_selection(monkeypatch, code, asset)
    key = f'assembly.final_image_{family}'
    template = (Path(__file__).resolve().parents[1] / 'prompt_templates' / f'final_image_{family}.txt').read_text()
    with get_db() as db:
        plan = json.loads(db.execute('SELECT styling_plan_json FROM generation_stylings WHERE id = ?', (ids['styling_id'],)).fetchone()[0])
        plan['hero_understanding']['shot_category_family'] = family
        db.execute('UPDATE generation_stylings SET styling_plan_json = ? WHERE id = ?', (dumps(plan), ids['styling_id']))
        db.execute('UPDATE prompt_configs SET content = ? WHERE key = ?', (template, key))
        db.execute('DELETE FROM shot_preset_slots')
    selected = run_selection(ids['task_id'])['item']['poseSelection']
    assert selected['workflow'] == 'shared_background_v1'
    assert len({shot['code'] for shot in selected['shots']}) == 5
    assert '镜头 5｜正背双视图' in calls[0][0] and '镜头 2｜' in calls[0][0]
    background = selected['shots'][0]['background']['id']
    frozen_input = app_main.get_generation_task_row(ids['task_id'])['current_input_version_id']
    replacement = insert_media_asset('new-shared-background.jpg')
    async def save(_files): return [app_main.get_media_asset_by_id(replacement)]
    monkeypatch.setattr(app_main, 'save_uploads_as_media_assets', save)
    assert client.post('/api/shoot-environment', files={'file': ('new.jpg', b'image', 'image/jpeg')}).status_code == 200
    assert app_main.get_media_asset_by_id(background)
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content = '后续修改' WHERE key = ?", (key,))
        db.execute("UPDATE prompt_configs SET content = '优化规则', enabled = 1 WHERE key = 'assembly.final_prompt_optimizer'")
    def forbidden_snapshot(): raise AssertionError('final must retain the model version used for styling')
    monkeypatch.setattr(app_main, 'snapshot_default_model', forbidden_snapshot)
    response = client.post(f'/api/generation-tasks/{ids["task_id"]}/generate-final')
    assert response.status_code == 200, response.text
    result = response.json()['item']['resultSet']
    packages = [app_main.build_new_final_task_package(ids['task_id'], image['id']) for image in result['images']]
    assert {p['imageInputs'][0]['assetId'] for p in packages} == {background}
    assert len({p['imageInputs'][1]['assetId'] for p in packages}) == 5
    for i, package in enumerate(packages, 1):
        assert f'镜头 {i}｜' in package['prompt']
        assert all(f'镜头 {other}｜' not in package['prompt'] for other in range(1, 6) if other != i)
        assert '后续修改' not in package['prompt']
        assert '不取图中人物、服装、道具、机位、景别和构图' in package['prompt']
        assert 'shot_global_directing' not in package['promptRuntime']
        assert 'shot_current' not in package['promptRuntime']
    with get_db() as db:
        row = db.execute('SELECT input_version_id, prompt_preparation_json FROM generation_result_sets WHERE id = ?', (result['id'],)).fetchone()
    assert row['input_version_id'] == frozen_input
    preparation = json.loads(row['prompt_preparation_json'])
    assert preparation['mode'] == 'optimized'  # operator's switch at creation, not at pose selection
    assert preparation['configRows'][key]['content'] == template
    assert preparation['configRows']['assembly.final_prompt_optimizer']['content'] == '优化规则'


def test_missing_shared_background_blocks_selection_before_ai(client, monkeypatch):
    ids = prepare_task()
    seed_pose()
    with get_db() as db:
        db.execute('DELETE FROM shoot_environment_references')
    async def forbidden(*args, **kwargs): raise AssertionError('must not call AI without background')
    monkeypatch.setattr(app_main, 'generate_gemini_json_text', forbidden)
    response = client.post(f'/api/generation-tasks/{ids["task_id"]}/select-poses')
    assert response.status_code == 409 and '统一拍摄背景' in response.json()['detail']
    assert app_main.get_generation_task_row(ids['task_id'])['status'] == 'STYLING_READY'


def test_single_pose_call_sees_outfit_images_and_rejects_duplicate_poses(monkeypatch):
    ids = prepare_task()
    _, asset, code = seed_pose()
    clothing_id = insert_media_asset('selected-clothing.jpg')
    garment = {'code': 'GARMENT-1', 'images': [{'asset': app_main.get_media_asset_by_id(clothing_id)}]}
    with get_db() as db:
        db.execute('UPDATE generation_stylings SET selected_material_snapshot_json = ? WHERE id = ?', (dumps([garment]), ids['styling_id']))
    calls = mock_selection(monkeypatch, code, asset)
    run_selection(ids['task_id'])
    assert len(calls) == 1 and len(calls[0][1]) == 3
    assert all(any(image['key'] == 'tests/selected-clothing.jpg' for image in images) for _, images in calls)
    first_selection = app_main.get_generation_task_by_id(ids['task_id'])['poseSelection']['id']
    async def duplicate(prompt, images, **kwargs):
        return dumps({'shots': [{'shotIndex': n, 'poseCode': code, 'imageId': asset, 'reason': 'duplicate', 'direction': 'same action'} for n in range(1, 6)]})
    monkeypatch.setattr(app_main, 'generate_gemini_json_text', duplicate)
    failed = run_selection(ids['task_id'])['item']
    assert failed['status'] == 'FAILED' and failed['poseSelection']['id'] == first_selection
    with get_db() as db:
        error = db.execute("SELECT error_message FROM generation_attempts WHERE owner_type = 'generation_pose_selection' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert '五个不同姿势' in error


def test_insufficient_distinct_poses_rejected_before_provider(client):
    ids = prepare_task()
    seed_pose()
    response = client.post(f'/api/generation-tasks/{ids["task_id"]}/select-poses')
    assert response.status_code == 409 and '至少补齐五个' in response.json()['detail']
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM generation_attempts WHERE owner_type = 'generation_pose_selection'").fetchone()[0] == 0


@pytest.mark.parametrize('hero_count,garment_count', [(7, 6), (10, 9)])
def test_pose_reference_budget_preserves_all_products_in_single_call(monkeypatch, hero_count, garment_count):
    from io import BytesIO
    from PIL import Image
    from app import image_provider

    ids = prepare_task()
    poses = [seed_pose(f'预算姿势{n}') for n in range(8)]
    heroes = [{'asset': app_main.get_media_asset_by_id(insert_media_asset(f'hero-{n}.jpg'))}
              for n in range(hero_count)]
    garments = [{'asset': app_main.get_media_asset_by_id(insert_media_asset(f'garment-{n}.jpg'))}
                for n in range(garment_count)]
    with get_db() as db:
        db.execute('UPDATE generation_task_input_versions SET image_snapshot_json = ? WHERE task_id = ?',
                   (dumps(heroes), ids['task_id']))
        db.execute('UPDATE generation_stylings SET selected_material_snapshot_json = ? WHERE id = ?',
                   (dumps([{'code': 'GARMENT', 'images': garments}]), ids['styling_id']))
    content = BytesIO()
    Image.new('RGB', (40, 80), 'red').save(content, 'PNG')
    read = lambda *_args: (content.getvalue(), 'image/png')
    monkeypatch.setattr(app_main, 'gemini_channel', lambda: 'toapis')
    monkeypatch.setattr(app_main, 'reference_source_bytes', read)
    monkeypatch.setattr(image_provider, 'reference_source_bytes', read)
    monkeypatch.setattr(image_provider, 'REFERENCE_COLLAGE_TILE_SIZE', 80)
    calls = []

    async def generate(prompt, images, **kwargs):
        assert len(images) <= 16
        manifest = json.loads(prompt.splitlines()[2])
        assert [entry['order'] for entry in manifest] == list(range(1, len(images) + 1))
        entries = [source for entry in manifest for source in entry.get('sources', [entry])]
        expected = hero_count + garment_count + 1
        assert len(entries) == expected
        assert len({entry['imageId'] for entry in entries}) == expected
        for item, entry in zip(images, manifest):
            with Image.open(BytesIO(item['data'])) as image:
                if entry['role'] == 'reference_board':
                    # Label header occupies its own area, preserving the complete source image.
                    assert image.height == 116
                    assert image.getpixel((40, 76))[0] > 200
                    assert image.crop((0, 0, 80, 36)).convert("L").getextrema()[0] < 100
        calls.append(len(images))
        assert all(e['role'] != 'pose' for e in entries)
        return dumps({'shots': [{'shotIndex': n + 1, 'poseCode': poses[n][2], 'imageId': poses[n][1],
                                 'reason': '适合', 'direction': '保持姿势'} for n in range(5)]})

    monkeypatch.setattr(app_main, 'generate_gemini_json_text', generate)
    task = run_selection(ids['task_id'])['item']
    assert task['status'] == 'POSE_SELECTION_READY'
    assert len(calls) == 1
    with get_db() as db:
        snapshot = json.loads(db.execute(
            "SELECT request_snapshot_json FROM generation_attempts WHERE owner_type = 'generation_pose_selection'"
        ).fetchone()[0])
    assert len(snapshot['shortlistImageManifest']) == hero_count + garment_count + 1
    assert len(snapshot['shortlistProviderImageManifest']) == calls[0]
    assert 'visualPrompt' not in snapshot and 'visualRuntime' not in snapshot
    assert snapshot['selectionMode'] == 'shortlist_only'
    assert len(task['poseSelection']['shots']) == 5


@pytest.mark.parametrize('count', [16, 17, 22, 33, 64, 65])
def test_pose_board_capacity_never_drops_sources(count):
    from app.pose_selection import pack_pose_references
    inputs = [{'objectKey': f'{n}.jpg'} for n in range(count)]
    manifest = [{'order': n + 1, 'imageId': n, 'role': 'pose'} for n in range(count)]
    if count > 64:
        with pytest.raises(ValueError, match='参考图片过多'):
            pack_pose_references(inputs, manifest, 16)
        assert pack_pose_references(inputs, manifest, None) == (inputs, manifest)
        return
    packed, mapped = pack_pose_references(inputs, manifest, 16)
    assert len(packed) <= 16
    assert [source for item in packed for source in item.get('collageItems', [item])] == inputs
    assert [source['imageId'] for item in mapped for source in item.get('sources', [item])] == list(range(count))
    assert [item['order'] for item in mapped] == list(range(1, len(packed) + 1))
    assert all(len(item.get('collageItems', [])) <= 4 for item in packed)


def test_legacy_shortlist_first_choices_become_final_without_visual_call(monkeypatch):
    ids = prepare_task()
    poses = [seed_pose(f"初筛姿势{n}") for n in range(6)]
    calls = []
    monkeypatch.setattr(app_main, "vision_reference_input", lambda item: {"key": item["objectKey"]})
    async def generate(prompt, images, **kwargs):
        calls.append(prompt)
        assert len(calls) == 1
        return dumps({"shots": [{"shotIndex": i + 1, "candidateCodes": [p[2], poses[5][2]],
                                 "reason": f"初筛理由{i}"} for i, p in enumerate(poses[:5])]})
    monkeypatch.setattr(app_main, "generate_gemini_json_text", generate)
    task = run_selection(ids["task_id"])["item"]
    assert task["status"] == "POSE_SELECTION_READY"
    assert len(calls) == 1
    assert [shot["code"] for shot in task["poseSelection"]["shots"]] == [p[2] for p in poses[:5]]
    assert task["poseSelection"]["shots"][0]["reason"] == "初筛理由0"
    assert json.loads(task["poseSelection"]["shots"][0]["direction"]) == {"核心动作": ["一手自然下垂"]}


def test_legacy_shortlist_does_not_substitute_duplicate_first_choices():
    library = [{"code": f"P{i}", "images": [{"asset": {"id": i}}], "aiTags": {"动作": "站立"}} for i in range(6)]
    raw = {"shots": [{"shotIndex": i + 1, "candidateCodes": ["P0", f"P{i+1}"], "reason": "初筛"} for i in range(5)]}
    plan = pose_plan_from_shortlist(raw, library)
    with pytest.raises(ValueError, match="五个不同姿势"):
        validate_pose_plan(plan, {p["code"]: {p["images"][0]["asset"]["id"]} for p in library})


def test_pose_direction_renders_action_facts_without_selection_meta() -> None:
    from app.prompt_render import render_pose_direction

    direction = json.dumps(
        {
            "姿势描述": "模特正对镜头平视直立站立",
            "核心动作": ["正面站立", "单手插侧袋"],
            "头部视线": ["视线平视正前方"],
            "展示优势": ["全面平整展示服装正面版型"],
            "适配加分规则": ["目标服装含侧插袋设计时加分"],
            "适配排除规则": ["服装完全无口袋时排除"],
            "存疑信息": ["小腿下段不可见"],
        },
        ensure_ascii=False,
    )

    rendered = render_pose_direction(direction)

    assert "模特正对镜头平视直立站立" in rendered
    assert "核心动作：正面站立、单手插侧袋" in rendered
    assert "适配排除规则" not in rendered
    assert "展示优势" not in rendered
    assert "存疑信息" not in rendered
    # 历史非 JSON 快照原样返回。
    assert render_pose_direction("保持原图肩胸支撑") == "保持原图肩胸支撑"
    assert render_pose_direction("") == ""
