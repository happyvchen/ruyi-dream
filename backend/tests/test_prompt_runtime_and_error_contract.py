from __future__ import annotations

from pathlib import Path

import pytest

from app import main as app_main
from app.generation_progress import (
    ERROR_AUTH_FAILED,
    ERROR_CONTENT_POLICY,
    ERROR_INSUFFICIENT_BALANCE,
    ERROR_INVALID_REQUEST,
    ERROR_PROVIDER_GENERATION_FAILED,
    ERROR_PROVIDER_TRANSIENT_UNKNOWN,
    ERROR_RATE_LIMITED,
    ERROR_SUBMISSION_UNKNOWN,
    ERROR_WAF_BLOCK,
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
            {"role": "hero_garment"},
        ]
    )

    assert spans == {
        "model_identity": "图1",
        "hero_garment": "图2、图6",
        "styling_garment": "图3",
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
    assert runtime["ref_hero_garment"] == "图4"
    assert runtime["ref_styling_garment"] == "图5"
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
    assert runtime["ref_hero_garment"] == "图2"
    assert runtime["ref_styling_garment"] == "图3"
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
    assert runtime["ref_hero_garment"] == "图4"
    assert runtime["ref_styling_garment"] == ""
    assert "preview_grid_cell" not in runtime
    assert "ref_preview_grid" not in runtime
    assert [runtime[f"is_shot_{index}"] for index in range(1, 6)] == ["", "", "", "1", ""]
    assert "is_shot_6" not in runtime


@pytest.mark.parametrize("filename", ["final_image_top.txt", "final_image_bottom.txt", "final_image_one_piece.txt"])
def test_final_prompt_templates_separate_background_pose_and_shot_controls(filename: str) -> None:
    template = (Path(__file__).resolve().parents[1] / "prompt_templates" / filename).read_text(encoding="utf-8")

    assert "整组共用的背景与光线参考" in template
    assert "不取图中人物、服装、道具、机位、景别和构图" in template
    assert "不取它的脸、发型、身材、服装、背景、光线" in template
    assert "也不取它的取景、机位和裁切" in template
    assert "执行可辨的身体朝向、重心、肩线、手位" in template
    assert "唯一的拍摄母版" not in template
    assert "中央 3:4 竖向裁切安全区" in template
    assert "中央 2:3" not in template
    # 真实摄影质感要求：正向措辞必须存在，抹平质感的反伪影措辞必须消失。
    assert "这是真实拍摄的商业棚拍照片" in template
    assert "人物脚下有真实的落地阴影" in template
    assert "保留毛孔、细纹、细小绒毛" in template
    assert "不要磨皮、不要匀肤、不要塑料或蜡质感" in template
    assert "均匀的商业棚拍成像" not in template
    assert "材质纹理只来自真实服装本身" not in template
    # 商品保真与瑕疵约束。
    assert "大小比例" in template and "商品图" in template
    assert "不得出现任何文字、字母、logo" in template
    assert "两只鞋不同款或鞋跟高度不一致" in template


@pytest.mark.parametrize(
    ("prompt_key", "filename", "family"),
    [
        ("assembly.final_image_top", "final_image_top.txt", "top"),
        ("assembly.final_image_bottom", "final_image_bottom.txt", "bottom"),
        ("assembly.final_image_one_piece", "final_image_one_piece.txt", "one_piece"),
    ],
)
def test_final_prompt_templates_render_shot_five_without_legacy_coordinates(
    monkeypatch, prompt_key: str, filename: str, family: str
) -> None:
    template = (Path(__file__).resolve().parents[1] / "prompt_templates" / filename).read_text(encoding="utf-8")
    runtime = app_main.build_final_prompt_runtime(
        5,
        "top_shot_5",
        {"hero_understanding": {"shot_category_family": family}},
        [
            {"role": "shot_background"},
            {"role": "shot_pose"},
            {"role": "model_identity"},
            {"role": "hero_garment"},
        ],
        {},
        {},
    )
    monkeypatch.setattr(app_main, "prompt_config_content", lambda key, rows=None: template if key == prompt_key else None)

    rendered = app_main.render_prompt_template(prompt_key, runtime)

    assert "中央 3:4 竖向裁切安全区" in rendered
    assert "中央 2:3" not in rendered
    assert "16.7%" not in rendered
    assert "镜头 1｜" not in rendered
    assert "图4 是主推款的唯一商品事实源" in rendered
    assert "是搭配款的唯一商品事实源" not in rendered


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


def test_toapis_unknown_provider_route_is_treated_as_transient_instead_of_invalid_input() -> None:
    payload = {
        "error": {
            "code": "model_not_found",
            "type": "invalid_request_error",
            "param": "model",
            "message": "unknown provider for model gemini-3.5-flash-low",
        }
    }

    normalized = normalize_toapis_error(payload, status_code=400)

    assert normalized["code"] == ERROR_PROVIDER_TRANSIENT_UNKNOWN
    assert normalized["upstreamCode"] == "model_not_found"


@pytest.mark.parametrize(
    "error",
    [
        {"type": "invalid_request_error", "message": "unknown provider for model gemini-3.5-flash-low"},
        {"code": "invalid_request", "message": "unknown provider for model gemini-3.5-flash-low"},
    ],
)
def test_toapis_unknown_provider_route_retries_even_without_model_not_found_code(
    error: dict[str, str],
) -> None:
    normalized = normalize_toapis_error({"error": error}, status_code=400)

    assert normalized["code"] == ERROR_PROVIDER_TRANSIENT_UNKNOWN


def test_toapis_real_model_not_found_remains_a_non_retryable_invalid_request() -> None:
    payload = {
        "error": {
            "code": "model_not_found",
            "type": "invalid_request_error",
            "param": "model",
            "message": "model does not exist",
        }
    }

    assert normalize_toapis_error(payload, status_code=400)["code"] == ERROR_INVALID_REQUEST


def test_unclassified_zero_output_toapis_http_error_defaults_to_transient() -> None:
    payload = {"error": {"code": "edge_route_unavailable", "message": "temporary route failure"}}

    assert normalize_toapis_error(payload, status_code=418)["code"] == ERROR_PROVIDER_TRANSIENT_UNKNOWN


def test_transient_unknown_terminal_presentation_does_not_claim_retry_is_still_running() -> None:
    presentation = provider_error_presentation(ERROR_PROVIDER_TRANSIENT_UNKNOWN)

    assert "已自动重试" in presentation["message"]
    assert "正在" not in presentation["message"]


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, ERROR_AUTH_FAILED),
        (403, ERROR_AUTH_FAILED),
        (422, ERROR_CONTENT_POLICY),
    ],
)
def test_unknown_provider_words_never_override_hard_stop_http_statuses(status_code: int, expected: str) -> None:
    payload = {
        "error": {
            "code": "model_not_found",
            "message": "unknown provider for model gemini-test-low",
        }
    }

    assert normalize_toapis_error(payload, status_code=status_code)["code"] == expected


def test_cloudflare_1010_waf_fingerprint_remains_retryable_before_generic_403_auth() -> None:
    payload = {
        "error": {
            "code": "access_denied",
            "message": "Cloudflare error code 1010",
        }
    }

    assert normalize_toapis_error(payload, status_code=403)["code"] == ERROR_WAF_BLOCK


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


def test_removed_runtime_fields_are_not_exposed() -> None:
    removed = {"shot_global_directing", "shot_current", "ref_garment"}
    runtime = app_main.build_final_prompt_runtime(1, "top_shot_1", {}, [], {}, {})
    assert removed.isdisjoint(runtime)
    assert removed.isdisjoint(app_main.sample_prompt_runtime())
    names = {item["name"] for item in app_main.available_prompt_placeholders()}
    assert {f"runtime.{name}" for name in removed}.isdisjoint(names)
    assert {"runtime.ref_hero_garment", "runtime.ref_styling_garment"} <= names


@pytest.mark.parametrize("styling_count", [0, 1])
def test_six_reference_roles_render_without_crossing_garment_boundaries(styling_count: int) -> None:
    roles = ["shot_background", "shot_pose", "model_identity", "hero_garment", "hero_garment"]
    roles += ["styling_garment"] * styling_count
    runtime = app_main.build_final_prompt_runtime(1, "top_shot_1", {}, [{"role": role} for role in roles], {}, {})
    assert runtime["ref_hero_garment"] == "图4-5"
    assert runtime["ref_styling_garment"] == ("图6" if styling_count else "")


def test_prompt_placeholder_migration_preserves_custom_content_and_is_idempotent() -> None:
    from app.db import get_db, migration_split_garment_prompt_references
    legacy = "自定义摄影规则\n{{ runtime.shot_global_directing }}{{runtime.shot_current}}\n{{runtime.ref_garment}} 是商品事实源。"
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content = ?, updated_at = 'before' WHERE key = 'assembly.final_image_top'", (legacy,))
        migration_split_garment_prompt_references(db)
        first = dict(db.execute("SELECT * FROM prompt_configs WHERE key = 'assembly.final_image_top'").fetchone())
        migration_split_garment_prompt_references(db)
        second = dict(db.execute("SELECT * FROM prompt_configs WHERE key = 'assembly.final_image_top'").fetchone())
    assert first == second
    assert "自定义摄影规则" in first["content"]
    assert "{{runtime.ref_hero_garment}}" in first["content"]
    assert "{{runtime.ref_styling_garment}}" in first["content"]
    assert "shot_global_directing" not in first["content"]
    assert "shot_current" not in first["content"]
    assert "ref_garment" not in first["content"]


@pytest.mark.parametrize("hero,styling,expected", [("图4-5", "图6", "主推款 图4-5；搭配款 图6"), ("图4", "", "主推款 图4"), ("", "", "无商品")])
def test_frozen_prompt_upgrade_renders_split_roles_without_mutating_snapshot(hero, styling, expected) -> None:
    key = "assembly.final_image_top"
    content = "{{runtime.shot_current}}{{runtime.shot_global_directing}}{{#runtime.ref_garment}}{{runtime.ref_garment}}{{/runtime.ref_garment}}{{^runtime.ref_garment}}无商品{{/runtime.ref_garment}}"
    rows = {key: {"content": content, "group_type": "assembly"}}
    result, diagnostics = app_main.render_prompt_config(key, {"ref_hero_garment": hero, "ref_styling_garment": styling}, rows=rows)
    assert result.strip() == expected
    assert not diagnostics["missingPlaceholders"]
    assert rows[key]["content"] == content
