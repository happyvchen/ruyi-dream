from __future__ import annotations

import pytest

from app import main as app_main
from app.db import PROMPT_CONFIG_CATALOG, dumps, get_db, now_iso
from app.generation_progress import ERROR_AUTH_FAILED, ERROR_CONN_RESET, ERROR_DELIVERY_FAILED, ERROR_UPLOAD_FAILED
from conftest import insert_generation_task, insert_ready_generation_task_with_preview


def styling_plan(family: str | None = "top") -> dict[str, object]:
    hero: dict[str, object] = {"category": "西装马甲", "sub_category": "suit_vest"}
    if family is not None:
        hero["shot_category_family"] = family
    return {"hero_understanding": hero, "selected_material_ids": []}


def test_prompt_catalog_contains_current_workflow_slots_only() -> None:
    keys = {row[0] for row in PROMPT_CONFIG_CATALOG}

    assert {
        "segment.brand_info",
        "assembly.garment_image_selection",
        "assembly.ai_tagging",
        "assembly.hero_image_selection",
        "assembly.ai_styling",
        "assembly.final_image_top",
        "assembly.final_image_bottom",
        "assembly.final_image_one_piece",
    } <= keys

    assert "assembly.shoot_script" not in keys
    assert "assembly.storyboard_preview" not in keys
    assert "assembly.storyboard_preview_top" not in keys
    assert "assembly.final_image" not in keys


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("top", "top"),
        ("上装", "top"),
        ("bottom", "bottom"),
        ("裤装", "bottom"),
        ("one-piece", "one_piece"),
        ("连衣裙", "one_piece"),
    ],
)
def test_shot_category_family_accepts_only_the_three_prompt_families(raw: str, expected: str) -> None:
    assert app_main.normalize_shot_category_family(raw) == expected


def test_missing_shot_category_family_fails_instead_of_falling_back_to_product_category() -> None:
    with pytest.raises(RuntimeError, match="shot_category_family"):
        app_main.storyboard_category_family(styling_plan(family=None))


def test_invalid_shot_category_family_fails_instead_of_guessing_from_sub_category() -> None:
    with pytest.raises(RuntimeError, match="shot_category_family"):
        app_main.storyboard_category_family(styling_plan(family="vest"))


@pytest.mark.parametrize(
    ("family", "final_key"),
    [
        ("top", "assembly.final_image_top"),
        ("bottom", "assembly.final_image_bottom"),
        ("one_piece", "assembly.final_image_one_piece"),
    ],
)
def test_prompt_key_routing_uses_styling_plan_family(family: str, final_key: str) -> None:
    plan = styling_plan(family)

    assert app_main.final_image_prompt_config_key(plan) == final_key


@pytest.mark.parametrize("family", ["top", "bottom", "one_piece"])
def test_final_image_slots_are_fixed_to_five_direct_shots(family: str) -> None:
    assert app_main.final_image_slot_roles(styling_plan(family)) == [
        f"{family}_shot_1",
        f"{family}_shot_2",
        f"{family}_shot_3",
        f"{family}_shot_4",
        f"{family}_shot_5",
    ]


def test_generate_final_creates_five_queued_images_and_never_a_sixth(client) -> None:
    ids = insert_ready_generation_task_with_preview(family="bottom")

    response = client.post(f"/api/generation-tasks/{ids['task_id']}/generate-final")

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["status"] == "FINAL_RUNNING"
    result_images = item["resultSet"]["images"]
    assert [image["sortOrder"] for image in result_images] == [1, 2, 3, 4, 5]
    assert [image["shotRole"] for image in result_images] == [
        "bottom_shot_1",
        "bottom_shot_2",
        "bottom_shot_3",
        "bottom_shot_4",
        "bottom_shot_5",
    ]
    assert all(image["status"] == "QUEUED" for image in result_images)


def test_continue_generation_keeps_succeeded_slots_and_requeues_only_failed_or_missing(client) -> None:
    ids = insert_ready_generation_task_with_preview(family="top")
    first = client.post(f"/api/generation-tasks/{ids['task_id']}/generate-final")
    result_set_id = first.json()["item"]["activeResultSetId"]
    image_ids = [image["id"] for image in first.json()["item"]["resultSet"]["images"]]

    from app.db import get_db, now_iso

    with get_db() as db:
        timestamp = now_iso()
        db.execute("UPDATE generation_tasks SET status = 'FINAL_PARTIAL' WHERE id = ?", (ids["task_id"],))
        db.execute("UPDATE generation_result_sets SET status = 'PARTIAL' WHERE id = ?", (result_set_id,))
        db.execute("UPDATE generation_result_images_new SET status = 'SUCCEEDED', updated_at = ? WHERE id IN (?, ?)", (timestamp, image_ids[0], image_ids[1]))
        db.execute("UPDATE generation_result_images_new SET status = 'FAILED', updated_at = ? WHERE id IN (?, ?)", (timestamp, image_ids[2], image_ids[3]))
        db.execute("DELETE FROM generation_result_images_new WHERE id = ?", (image_ids[4],))

    response = client.post(f"/api/generation-tasks/{ids['task_id']}/auto-run-final")

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["autoRunTarget"] == "FINAL"
    # The queue is scheduled asynchronously, but the explicit continue path must keep prior success state.
    continued = app_main.continue_active_final_result_set(
        ids["task_id"],
        result_set_id,
        ids["styling_id"],
        ids["preview_id"],
        {"stage": "final_image", "instruction": "继续补齐失败镜头"},
    )
    assert len(continued or []) == 3

    with get_db() as db:
        rows = db.execute(
            "SELECT sort_order, status FROM generation_result_images_new WHERE result_set_id = ? ORDER BY sort_order",
            (result_set_id,),
        ).fetchall()
    assert [(row["sort_order"], row["status"]) for row in rows] == [
        (1, "SUCCEEDED"),
        (2, "SUCCEEDED"),
        (3, "QUEUED"),
        (4, "QUEUED"),
        (5, "QUEUED"),
    ]


@pytest.mark.parametrize("provider", ["toapis", "apiyi"])
def test_continue_delivery_failure_preserves_provider_attempt_for_redelivery(client, provider) -> None:
    ids = insert_ready_generation_task_with_preview(family="top")
    first = client.post(f"/api/generation-tasks/{ids['task_id']}/generate-final")
    item = first.json()["item"]
    result_set_id = item["activeResultSetId"]
    image_rows = item["resultSet"]["images"]
    failed_image_id = image_rows[0]["id"]
    timestamp = now_iso()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO generation_attempts
              (owner_type, owner_id, provider, provider_request_id, status, error_code,
               started_at, failed_at, created_at, updated_at)
            VALUES ('generation_result_image', ?, 'image-2', 'provider-task-1', 'FAILED', ?, ?, ?, ?, ?)
            """,
            (failed_image_id, ERROR_DELIVERY_FAILED, timestamp, timestamp, timestamp, timestamp),
        )
        attempt_id = int(cursor.lastrowid)
        if provider == "apiyi":
            db.execute("UPDATE generation_attempts SET provider_request_id = NULL, response_snapshot_json = ? WHERE id = ?", (
                dumps({"provider": "apiyi", "status": "SUCCEEDED", "providerStatusResponse": {"data": [{"b64_json": "cGFpZA=="}]}}),
                attempt_id,
            ))
        db.execute("UPDATE generation_tasks SET status = 'FINAL_PARTIAL' WHERE id = ?", (ids["task_id"],))
        db.execute("UPDATE generation_result_sets SET status = 'PARTIAL' WHERE id = ?", (result_set_id,))
        db.execute(
            "UPDATE generation_result_images_new SET status = 'SUCCEEDED' WHERE result_set_id = ? AND id != ?",
            (result_set_id, failed_image_id),
        )
        db.execute(
            "UPDATE generation_result_images_new SET status = 'FAILED', active_attempt_id = ? WHERE id = ?",
            (attempt_id, failed_image_id),
        )

    continued = app_main.continue_active_final_result_set(
        ids["task_id"], result_set_id, ids["styling_id"], ids["preview_id"], {"stage": "final_image"}
    )

    assert continued == [failed_image_id]
    with get_db() as db:
        image = db.execute(
            "SELECT status, active_attempt_id FROM generation_result_images_new WHERE id = ?",
            (failed_image_id,),
        ).fetchone()
    assert image["status"] == "QUEUED"
    assert image["active_attempt_id"] == attempt_id


def test_upload_failure_is_persisted_for_poller_and_restores_stage(client) -> None:
    task_id = insert_generation_task(status="FAILED")
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            "UPDATE generation_tasks SET auto_run_target = 'FINAL', auto_run_requested_at = ? WHERE id = ?",
            (timestamp, task_id),
        )
        db.execute(
            """
            INSERT INTO generation_attempts
              (owner_type, owner_id, provider, status, error_code, started_at, failed_at, created_at, updated_at)
            VALUES ('generation_styling', ?, 'gemini', 'FAILED', ?, ?, ?, ?, ?)
            """,
            (task_id, ERROR_UPLOAD_FAILED, timestamp, timestamp, timestamp, timestamp),
        )

    assert app_main.schedule_auto_run_retry(task_id, [ERROR_UPLOAD_FAILED]) is True

    response = client.get(f"/api/generation-tasks/{task_id}")
    assert response.status_code == 200
    item = response.json()["item"]
    assert item["status"] == "READY_FOR_STYLING"
    assert item["autoRunTarget"] == "FINAL"
    assert item["autoRunRetryCount"] == 1
    assert item["autoRunRetryLimit"] == 10
    assert item["autoRunRetryAt"] is not None


def test_hard_auto_run_failure_is_not_scheduled() -> None:
    task_id = insert_generation_task(status="FAILED")

    assert app_main.schedule_auto_run_retry(task_id, [ERROR_AUTH_FAILED]) is False

    with get_db() as db:
        task = db.execute("SELECT auto_run_retry_at, auto_run_retry_count FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
    assert task["auto_run_retry_at"] is None
    assert task["auto_run_retry_count"] == 0


def test_provider_retry_budget_is_not_multiplied_by_auto_run() -> None:
    task_id = insert_generation_task(status="FAILED")

    assert app_main.schedule_auto_run_retry(task_id, [ERROR_CONN_RESET]) is False


def test_auto_run_stops_after_ten_scheduled_retries() -> None:
    task_id = insert_generation_task(status="FAILED")
    with get_db() as db:
        db.execute(
            "UPDATE generation_tasks SET auto_run_target = 'FINAL', auto_run_retry_count = 10 WHERE id = ?",
            (task_id,),
        )

    assert app_main.schedule_auto_run_retry(task_id, [ERROR_UPLOAD_FAILED]) is False

    with get_db() as db:
        task = db.execute("SELECT auto_run_retry_at, auto_run_retry_count FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
    assert task["auto_run_retry_at"] is None
    assert task["auto_run_retry_count"] == 10


def test_due_auto_run_retry_is_consumed_without_resetting_count() -> None:
    task_id = insert_generation_task(status="READY_FOR_STYLING")
    with get_db() as db:
        db.execute(
            """
            UPDATE generation_tasks
            SET auto_run_target = 'FINAL', auto_run_retry_at = '2000-01-01T00:00:00Z', auto_run_retry_count = 3
            WHERE id = ?
            """,
            (task_id,),
        )

    assert app_main.consume_due_auto_run_retry(task_id) is True

    with get_db() as db:
        task = db.execute("SELECT auto_run_retry_at, auto_run_retry_count FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
    assert task["auto_run_retry_at"] is None
    assert task["auto_run_retry_count"] == 3


def test_poller_recovers_interrupted_auto_run_styling_after_process_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = insert_generation_task(status="STYLING_RUNNING")
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            "UPDATE generation_tasks SET auto_run_target = 'FINAL', auto_run_requested_at = ? WHERE id = ?",
            (timestamp, task_id),
        )
        cursor = db.execute(
            """
            INSERT INTO generation_attempts
              (owner_type, owner_id, provider, status, started_at, created_at, updated_at)
            VALUES ('generation_styling', ?, 'gemini', 'RUNNING', ?, ?, ?)
            """,
            (task_id, timestamp, timestamp, timestamp),
        )
        attempt_id = int(cursor.lastrowid)
    monkeypatch.setattr(app_main, "pump_auto_run_queue", lambda: None)

    app_main.refresh_pending_new_generation_jobs()

    with get_db() as db:
        task = db.execute(
            "SELECT status, auto_run_target, auto_run_retry_at, auto_run_retry_count FROM generation_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        attempt = db.execute("SELECT status FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
    assert task["status"] == "READY_FOR_STYLING"
    assert task["auto_run_target"] == "FINAL"
    assert task["auto_run_retry_at"] is not None
    assert task["auto_run_retry_count"] == 1
    assert attempt["status"] == "FAILED"


def test_generation_retry_backoff_caps_at_four_minutes() -> None:
    assert [app_main.generation_retry_backoff_seconds(index) for index in range(1, 7)] == [
        30,
        60,
        120,
        240,
        240,
        240,
    ]
