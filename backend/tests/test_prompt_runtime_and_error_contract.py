from __future__ import annotations

import pytest

from app import main as app_main
from app.generation_progress import (
    ERROR_AUTH_FAILED,
    ERROR_CONTENT_POLICY,
    ERROR_INSUFFICIENT_BALANCE,
    ERROR_INVALID_REQUEST,
    ERROR_PROVIDER_GENERATION_FAILED,
    ERROR_RATE_LIMITED,
    ERROR_SUBMISSION_UNKNOWN,
    normalize_toapis_error,
    provider_error_presentation,
)
from app.prompt_render import (
    render_image_reference_spans,
    render_user_instruction_line,
    render_user_requirement_line,
)


def test_runtime_image_reference_spans_follow_input_order_and_roles() -> None:
    spans = render_image_reference_spans(
        [
            {"role": "model_identity"},
            {"role": "hero_garment"},
            {"role": "styling_garment"},
            {"role": "preview_grid"},
            {"role": "shoot_environment"},
            {"role": "garment"},
        ]
    )

    assert spans == {
        "model_identity": "图1",
        "garment": "图2-6",
        "preview_grid": "图4",
        "shoot_environment": "图5",
        "shot_background": "",
        "shot_pose": "",
    }


def test_direct_final_runtime_labels_background_pose_model_and_garments() -> None:
    runtime = app_main.build_final_prompt_runtime(
        3,
        "top_shot_3",
        {"hero_understanding": {"shot_category_family": "top"}},
        [
            {"role": "shot_background"},
            {"role": "shot_pose"},
            {"role": "model_identity"},
            {"role": "hero_garment"},
            {"role": "styling_garment"},
        ],
        {"instruction": "减少配饰"},
        {"upload_requirement": "保留后片结构"},
    )

    assert runtime["ref_shot_background"] == "图1"
    assert runtime["ref_shot_pose"] == "图2"
    assert runtime["ref_model_identity"] == "图3"
    assert runtime["ref_garment"] == "图4-5"
    assert runtime["shot_index"] == "3"
    assert "ref_preview_grid" not in runtime
    assert "ref_shoot_environment" not in runtime


def test_user_visible_instruction_lines_are_business_text_not_prompt_json() -> None:
    assert render_user_instruction_line({"instruction": "  减少配饰。。  "}) == "减少配饰。"
    assert render_user_requirement_line({"upload_requirement": "重点展示腰线；；"}) == "重点展示腰线；"
    assert render_user_instruction_line({"instruction": ""}) == ""


def test_preview_runtime_contains_only_dynamic_business_facts() -> None:
    runtime = app_main.build_preview_prompt_runtime(
        {
            "hero_understanding": {"shot_category_family": "top"},
            "selected_material_codes": ["CARDIGAN-1"],
            "selected_outfit": {"wearing_logic": "薄针织外搭，不能遮挡主推款门襟"},
            "accessory_boundary": "低存在感耳饰",
        },
        [{"role": "model_identity"}, {"role": "hero_garment"}, {"role": "styling_garment"}, {"role": "shoot_environment"}],
        {"instruction": "姿态更舒展"},
        {"upload_requirement": "主推款纽扣要清晰"},
    )

    assert runtime["category_family"] == "top"
    assert runtime["ref_model_identity"] == "图1"
    assert runtime["ref_garment"] == "图2-3"
    assert runtime["ref_shoot_environment"] == "图4"
    assert runtime["storyboard_preview_user_instruction"] == "姿态更舒展"
    assert "six_shot" not in runtime
    assert "shot_plan" not in runtime


def test_final_runtime_marks_exactly_one_current_shot_and_limits_to_five_slots() -> None:
    runtime = app_main.build_final_prompt_runtime(
        4,
        "top_grid_4",
        {"hero_understanding": {"shot_category_family": "top"}},
        [{"role": "shot_background"}, {"role": "shot_pose"}, {"role": "model_identity"}, {"role": "hero_garment"}],
        {"instruction": "背景更干净"},
        {"upload_requirement": "保留衣长比例"},
    )

    assert runtime["ref_shot_background"] == "图1"
    assert runtime["ref_shot_pose"] == "图2"
    assert runtime["ref_model_identity"] == "图3"
    assert runtime["ref_garment"] == "图4"
    assert "preview_grid_cell" not in runtime
    assert "ref_preview_grid" not in runtime
    assert [runtime[f"is_shot_{index}"] for index in range(1, 6)] == ["", "", "", "1", ""]
    assert "is_shot_6" not in runtime


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (400, {"error": {"code": "invalid_request", "message": "too many refs"}}, ERROR_INVALID_REQUEST),
        (401, {"error": {"code": "unauthorized", "message": "bad key"}}, ERROR_AUTH_FAILED),
        (402, {"error": {"code": "insufficient_quota", "message": "no balance"}}, ERROR_INSUFFICIENT_BALANCE),
        (422, {"error": {"code": "content_policy_violation", "message": "blocked"}}, ERROR_CONTENT_POLICY),
        (429, {"error": {"code": "rate_limit_exceeded", "message": "slow down"}}, ERROR_RATE_LIMITED),
        (
            500,
            {"error": {"code": "", "message": "rate_limit_check_failed (request id: test-request)", "type": "new_api_error"}},
            ERROR_RATE_LIMITED,
        ),
        (200, {"error": {"code": "generation_failed", "message": "failed"}}, ERROR_PROVIDER_GENERATION_FAILED),
    ],
)
def test_toapis_error_normalization_returns_stable_codes(status_code: int, payload: dict[str, object], expected: str) -> None:
    assert normalize_toapis_error(payload, status_code=status_code)["code"] == expected


def test_embedded_upstream_json_is_extracted_for_user_recoverable_input_errors() -> None:
    payload = {
        "error": {
            "code": "generation_failed",
            "message": 'W8X API returned 400: {"error":{"code":"invalid_request","message":"reference images limited","param":"image"}}',
        }
    }

    normalized = normalize_toapis_error(payload, status_code=200)

    assert normalized["code"] == ERROR_INVALID_REQUEST
    assert normalized["upstreamCode"] == "invalid_request"
    assert normalized["upstreamMessage"] == "reference images limited"


def test_submission_unknown_copy_promises_no_duplicate_paid_generation() -> None:
    presentation = provider_error_presentation(ERROR_SUBMISSION_UNKNOWN, "read timeout after submit")

    assert presentation["retryable"] is False
    assert "避免重复计费" in presentation["message"]
