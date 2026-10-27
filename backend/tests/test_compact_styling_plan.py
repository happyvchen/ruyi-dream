from copy import deepcopy
import asyncio
from pathlib import Path

import pytest

from app import main as app_main
from app.db import dumps, get_db


def compact_plan():
    return {
        "hero_understanding": {"category": "半身裙", "shot_category_family": "bottom",
                               "hero_visual_summary": "浅色高腰半身裙", "hero_selling_points": ["高腰剪裁"]},
        "selected_materials": [{"material_code": "TOP-01", "wearing_method": "自然下摆"}],
        "empty_reason": "",
        "generated_accessories": ["米色简洁低跟鞋"],
        "wearing_notes": "保持裙腰线清晰。",
        "reason_summary": "轻薄上装呼应裙装色彩，保持简洁的都市造型。",
    }


def candidates():
    return {"candidate_material_ids": [41], "library_tag_cards": [{"id": 41, "code": "TOP-01", "ai_tags": {}}]}


def normalize(source):
    raw = {"selected_files": ["image:11:image-11"], "styling_plan": source}
    plan = app_main.normalize_ai_stylist_result(raw, {"image_snapshot_json": dumps([{"id": 11}])}, candidates())
    app_main.validate_styling_plan(plan, candidates())
    return plan


@pytest.mark.parametrize("legacy_version", [None, "styling-plan-v2"])
def test_final_choice_preserves_identity_accessories_wearing_and_routing(legacy_version):
    source = compact_plan()
    if legacy_version:
        source["schema_version"] = legacy_version
    plan = normalize(source)
    assert plan["schema_version"] == "styling-plan-v2"
    assert plan["selected_material_ids"] == [41]
    assert plan["selected_hero_image_ids"] == [11]
    assert plan["model_raw_output"]["styling_plan"] == source
    assert app_main.storyboard_category_family(plan) == "bottom"
    assert len(plan["outfit_candidates"]) == 1
    assert app_main.get_selected_outfit_scores(plan) == {}
    assert plan["quality_signals"] == []
    line = app_main.build_shot_styling_line(plan)
    assert "TOP-01穿法：自然下摆" in line
    assert "米色简洁低跟鞋" in line


def test_unversioned_compact_prompt_response_succeeds_without_paid_reask(monkeypatch):
    # Task 1480's active Prompt lists the compact fields but omits schema_version.
    source = compact_plan()
    calls = []

    async def generate(prompt, images, **kwargs):
        calls.append(prompt)
        return dumps({"selected_files": ["image:11:image-11"], "styling_plan": source})

    monkeypatch.setattr(app_main, "is_gemini_configured", lambda: True)
    monkeypatch.setattr(app_main, "generate_gemini_json_text", generate)
    monkeypatch.setattr(app_main.settings, "ai_stylist_contract_retry_limit", 0)
    plan = asyncio.run(app_main.select_styling_with_gemini(
        {"image_snapshot_json": dumps([{"id": 11}])}, candidates(), {"prompt": "compact contract"}))
    assert len(calls) == 1
    assert plan["schema_version"] == "styling-plan-v2"
    assert plan["selected_material_ids"] == [41]
    assert "schema_version" not in plan["model_raw_output"]["styling_plan"]
    assert "米色简洁低跟鞋" in app_main.build_shot_styling_line(plan)


@pytest.mark.parametrize("change", [
    {"selected_materials": [{"material_code": "INVENTED", "wearing_method": "单穿"}]},
    {"generated_accessories": "鞋"},
    {"scores": {}},
    {"empty_reason": "complete_look"},
])
def test_unversioned_compact_contract_still_validates_all_business_fields(change):
    source = compact_plan()
    source.update(change)
    with pytest.raises(app_main.AIStylistContractError):
        normalize(source)


def test_cancelling_styling_during_contract_backoff_stops_next_model_call(monkeypatch):
    from conftest import insert_generation_task
    task_id = insert_generation_task(status="STYLING_RUNNING")
    attempt_id = app_main.create_attempt("generation_styling", task_id, "deepseek")
    calls = []

    async def generate(prompt, images, **kwargs):
        calls.append(prompt)
        return "{}"

    async def cancel_during_backoff(_seconds):
        app_main.cancel_generation_task(task_id)

    monkeypatch.setattr(app_main, "is_gemini_configured", lambda: True)
    monkeypatch.setattr(app_main, "generate_gemini_json_text", generate)
    monkeypatch.setattr(app_main.settings, "ai_stylist_contract_retry_limit", 2)
    monkeypatch.setattr(app_main.settings, "ai_stylist_contract_retry_delay_seconds", 1)
    monkeypatch.setattr(app_main.asyncio, "sleep", cancel_during_backoff)
    with pytest.raises(RuntimeError, match="取消"):
        asyncio.run(app_main.select_styling_with_gemini(
            {"task_id": task_id, "image_snapshot_json": "[]"}, candidates(),
            {"prompt": "compact contract"}, attempt_id=attempt_id))
    assert len(calls) == 1
    with get_db() as db:
        assert db.execute("SELECT status FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()[0] == "CANCELLED"


@pytest.mark.parametrize("reason", ["complete_look", "no_suitable_match", "user_requested"])
def test_empty_choice_is_successful_and_preserves_distinct_reason(reason):
    source = compact_plan()
    source.update(selected_materials=[], empty_reason=reason, generated_accessories=[])
    plan = normalize(source)
    assert plan["empty_reason"] == reason
    assert plan["selected_material_ids"] == []
    assert app_main.selected_outfit_from_plan(plan)["generated_accessories"] == []


@pytest.mark.parametrize("change", [
    {"selected_materials": [{"material_code": "INVENTED", "wearing_method": "单穿"}]},
    {"selected_materials": [{"material_code": "TOP-01", "wearing_method": "单穿"}] * 2},
    {"selected_materials": []},
    {"empty_reason": "complete_look"},
    {"generated_accessories": "一双鞋"},
    {"wearing_notes": None},
    {"reason_summary": ""},
    {"scores": {"brand_fit_score": 1}},
])
def test_malformed_compact_output_is_rejected_without_silent_empty_fallback(change):
    source = compact_plan()
    source.update(deepcopy(change))
    with pytest.raises(app_main.AIStylistContractError):
        normalize(source)


def test_invalid_family_and_missing_required_field_are_rejected():
    source = compact_plan()
    source["hero_understanding"]["shot_category_family"] = "vest"
    with pytest.raises(app_main.AIStylistContractError):
        normalize(source)
    source = compact_plan()
    del source["empty_reason"]
    with pytest.raises(app_main.AIStylistContractError):
        normalize(source)


def test_old_candidate_contract_remains_readable():
    source = compact_plan()
    legacy = {"hero_understanding": source["hero_understanding"],
              "outfit_candidates": [{"outfit_key": "old", "selected_materials": source["selected_materials"],
                                     "generated_accessories": source["generated_accessories"],
                                     "scores": {"brand_fit_score": .8}}],
              "selected_outfit_key": "old"}
    plan = normalize(legacy)
    assert plan["schema_version"] == "styling-plan-v1"
    assert plan["selected_material_ids"] == [41]
    assert app_main.get_selected_outfit_scores(plan) == {"brand_fit_score": .8}


@pytest.mark.parametrize("instruction", ["", "不要包袋"])
def test_actual_prompt_assembly_uses_one_contract_and_complete_runtime(instruction):
    root = Path(__file__).resolve().parents[1] / "prompt_templates"
    with get_db() as db:
        for key, filename in [("assembly.ai_styling", "ai_styling.txt"), ("assembly.hero_image_selection", "hero_image_selection.txt")]:
            db.execute("UPDATE prompt_configs SET content = ? WHERE key = ?", ((root / filename).read_text(), key))
        db.execute("UPDATE prompt_configs SET content = '时尚、自信、精致的都市女装。' WHERE key = 'segment.brand_info'")
    snapshot = app_main.attach_ai_stylist_prompt_snapshot({
        "input_version": {"imageSnapshot": [{"id": 11}]},
        "ai_styling_user_instruction": {"instruction": instruction},
        "user_requirement": {"upload_requirement": "主推款是半身裙"},
    }, candidates())
    prompt = snapshot["prompt"]
    assert "{{" not in prompt and "[[缺失" not in prompt
    assert '"image:11:image-11"' in prompt and "TOP-01" in prompt
    assert prompt.count("【唯一输出契约】") == 1
    assert "outfit_candidates" not in prompt and "aesthetic_score" not in prompt
    for technical_field in ("schema_version", "plan_source_mode", "selected_outfit_key", "tagger_model", "tagger_provider"):
        assert technical_field not in prompt
    assert ("【本次用户要求】" in prompt) == bool(instruction)
    assert "主推款是半身裙" in prompt
    assert len(prompt) < 3000
