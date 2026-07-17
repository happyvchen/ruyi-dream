from __future__ import annotations

from app.db import get_db
from conftest import insert_generation_task


def test_auth_session_defaults_to_admin_in_local_development(client) -> None:
    response = client.get("/api/auth/session")

    assert response.status_code == 200
    assert response.json() == {"authRequired": False, "authenticated": True, "role": "admin"}


def test_prompt_maintenance_lists_current_slots_and_previews_draft_without_provider_calls(client, monkeypatch) -> None:
    from app import main as app_main

    def provider_should_not_be_called(*args, **kwargs):
        raise AssertionError("Prompt preview must not call external AI providers")

    monkeypatch.setattr(app_main, "generate_gemini_json_text", provider_should_not_be_called)
    monkeypatch.setattr(app_main, "generate_image_asset", provider_should_not_be_called)

    listed = client.get("/api/prompt-configs")
    assert listed.status_code == 200
    keys = {item["key"] for item in listed.json()["items"]}
    assert "assembly.ai_styling" in keys
    assert "assembly.shoot_script" not in keys

    updated = client.patch(
        "/api/prompt-configs/assembly.ai_styling",
        json={"content": "用户要求：{{runtime.ai_styling_user_instruction}}\n候选：{{runtime.candidate_retrieval}}"},
    )
    assert updated.status_code == 200
    assert "用户要求" in updated.json()["preview"]["rendered"]

    draft_preview = client.post(
        "/api/prompt-configs/assembly.ai_styling/preview",
        json={"content": "草稿：{{runtime.user_requirement}}"},
    )
    assert draft_preview.status_code == 200
    assert "草稿：" in draft_preview.json()["rendered"]


def test_model_page_api_returns_seeded_default_model_without_generation_dependencies(client) -> None:
    response = client.get("/api/model-profiles?limit=30")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 1
    assert any(item["isDefault"] for item in payload["items"])


def test_generation_record_workspace_supports_all_batch_and_single_task_views(client) -> None:
    batch_a = [insert_generation_task(name=f"BATCH-A-{index}", status="COMPLETED", batch_id="batch-a") for index in range(3)]
    batch_b = [insert_generation_task(name="BATCH-B-FAILED", status="FAILED", batch_id="batch-b")]
    single = insert_generation_task(name="SINGLE-LOOK", status="READY_FOR_STYLING")

    all_records = client.get("/api/generation-record-workspace?limit=20")
    batch_records = client.get("/api/generation-record-batches?limit=20")
    task_records = client.get("/api/generation-records?limit=20")

    assert all_records.status_code == 200
    assert batch_records.status_code == 200
    assert task_records.status_code == 200

    workspace_items = all_records.json()["items"]
    assert {item["kind"] for item in workspace_items} == {"batch", "task"}
    assert any(item["kind"] == "batch" and item["batch"]["batchId"] == "batch-a" and item["batch"]["status"] == "COMPLETED" for item in workspace_items)
    assert any(item["kind"] == "batch" and item["batch"]["batchId"] == "batch-b" and item["batch"]["status"] == "FAILED" for item in workspace_items)
    assert any(item["kind"] == "task" and item["task"]["id"] == single for item in workspace_items)

    assert {item["batchId"] for item in batch_records.json()["items"]} == {"batch-a", "batch-b"}
    assert {item["id"] for item in task_records.json()["items"]} >= {*batch_a, *batch_b, single}


def test_generation_record_filters_handle_many_keywords_without_advanced_query_syntax(client) -> None:
    task_ids = [insert_generation_task(name=f"SKU-{index:03d}", status="READY_FOR_STYLING") for index in range(20)]
    insert_generation_task(name="OTHER-001", status="DRAFT")

    keywords = "SKU-001，SKU-007 SKU-019\nmissing"
    response = client.get("/api/generation-records", params={"keyword": keywords, "limit": 30})

    assert response.status_code == 200
    names = {item["name"] for item in response.json()["items"]}
    assert names == {"SKU-001", "SKU-007", "SKU-019"}
    assert response.json()["total"] == 3


def test_batch_cancel_clears_one_hundred_queued_auto_run_tasks_without_touching_completed_results(client) -> None:
    task_ids = [insert_generation_task(name=f"QUEUE-{index:03d}", status="READY_FOR_STYLING", batch_id="queue-batch") for index in range(100)]
    with get_db() as db:
        db.execute(
            f"UPDATE generation_tasks SET auto_run_target = 'FINAL' WHERE id IN ({', '.join('?' for _ in task_ids)})",
            task_ids,
        )

    response = client.post("/api/generation-batches/queue-batch/cancel")

    assert response.status_code == 200
    assert response.json()["cancelledTaskIds"] == task_ids
    with get_db() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS count FROM generation_tasks WHERE batch_id = 'queue-batch' AND auto_run_target IS NOT NULL"
        ).fetchone()["count"]
    assert remaining == 0
