from __future__ import annotations

import pytest

from app import main as app_main
from app.db import PROMPT_CONFIG_CATALOG
from conftest import insert_ready_generation_task_with_preview


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
        "assembly.storyboard_preview_top",
        "assembly.storyboard_preview_bottom",
        "assembly.storyboard_preview_one_piece",
        "assembly.final_image_top",
        "assembly.final_image_bottom",
        "assembly.final_image_one_piece",
    } <= keys

    assert "assembly.shoot_script" not in keys
    assert "assembly.storyboard_preview" not in keys
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
    ("family", "storyboard_key", "final_key"),
    [
        ("top", "assembly.storyboard_preview_top", "assembly.final_image_top"),
        ("bottom", "assembly.storyboard_preview_bottom", "assembly.final_image_bottom"),
        ("one_piece", "assembly.storyboard_preview_one_piece", "assembly.final_image_one_piece"),
    ],
)
def test_prompt_key_routing_uses_styling_plan_family(family: str, storyboard_key: str, final_key: str) -> None:
    plan = styling_plan(family)

    assert app_main.storyboard_prompt_config_key(plan) == storyboard_key
    assert app_main.final_image_prompt_config_key(plan) == final_key


@pytest.mark.parametrize("family", ["top", "bottom", "one_piece"])
def test_final_image_slots_are_fixed_to_first_five_storyboard_cells(family: str) -> None:
    assert app_main.final_image_slot_roles(styling_plan(family)) == [
        f"{family}_grid_1",
        f"{family}_grid_2",
        f"{family}_grid_3",
        f"{family}_grid_4",
        f"{family}_grid_5",
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
        "bottom_grid_1",
        "bottom_grid_2",
        "bottom_grid_3",
        "bottom_grid_4",
        "bottom_grid_5",
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
