from pathlib import Path
import json

import pytest
from fastapi import HTTPException

from app import main as app_main
from app.db import get_db, migration_pose_shot_sections
from app.prompt_render import extract_prompt_section
from test_pose_workflow import prepare_task, seed_pose, mock_selection, run_selection, shot_section


@pytest.mark.parametrize("family", ["top", "bottom", "one_piece"])
def test_each_shot_uses_only_the_same_section_as_final_generation(family):
    key = f"assembly.final_image_{family}"
    content = (Path(__file__).resolve().parents[1] / "prompt_templates" / f"final_image_{family}.txt").read_text()
    rows = {key: {"content": content, "group_type": "assembly"}}
    runtime = {**app_main.sample_prompt_runtime(), "category_family": family}
    shots = app_main.render_pose_shot_requirements(runtime, rows)
    for shot in shots:
        current = {**runtime, **{f"is_shot_{i}": "1" if i == shot["shotIndex"] else "" for i in range(1, 6)}}
        full, diagnostics = app_main.render_prompt_config(key, current, rows=rows)
        assert not diagnostics["missingPlaceholders"]
        assert shot["requirements"].strip() == extract_prompt_section(full, "机位&景别")
        assert "## 摄影真实感" not in shot["requirements"]
        assert "## 参考图职责" not in shot["requirements"]
    if family == "bottom":
        assert "大腿中段" in shots[1]["requirements"]
        assert "头部、颈部、肩胸" in shots[1]["requirements"]


@pytest.mark.parametrize("content", [
    "完整成片正文，没有章节",
    shot_section() + "\n## 机位&景别\n重复章节",
    shot_section().replace("镜头2范围要求", ""),
    shot_section().replace("{{#runtime.is_shot_2}}镜头2范围要求\n{{/runtime.is_shot_2}}", "公共说明"),
    shot_section().replace("镜头2范围要求", "{{runtime.unknown}}"),
])
def test_invalid_sections_never_fall_back_to_full_prompt(content):
    rows = {"assembly.final_image_top": {"content": content, "group_type": "assembly"}}
    with pytest.raises(HTTPException) as exc:
        app_main.render_pose_shot_requirements({"category_family": "top"}, rows)
    assert exc.value.status_code == 409


def test_preview_execution_and_frozen_rules_share_section_without_rewriting_history(client, monkeypatch):
    ids = prepare_task()
    _, asset_id, code = seed_pose()
    calls = mock_selection(monkeypatch, code, asset_id)
    run_selection(ids["task_id"])
    with get_db() as db:
        attempt = db.execute("SELECT * FROM generation_attempts WHERE owner_type = 'generation_pose_selection' ORDER BY id DESC LIMIT 1").fetchone()
        original = attempt["request_snapshot_json"]
        facts = json.loads(original)
        frozen = db.execute("SELECT plan_json FROM generation_pose_selections WHERE task_id = ?", (ids["task_id"],)).fetchone()[0]
    shots = json.loads(facts["runtime"]["pose_shots"])
    assert all("穿法" not in s["requirements"] and "范围要求" in s["requirements"] for s in shots)
    assert json.loads(frozen)["shotRequirements"] == shots
    # The historical context preview makes no model call and renders the exact
    # same selection request when the maintained configuration is unchanged.
    preview = client.post("/api/prompt-configs/assembly.pose_selection/preview", json={
        "content": "{{runtime.pose_phase}}\n{{runtime.pose_candidates}}\n{{runtime.pose_image_manifest}}\n{{runtime.pose_shots}}",
        "attemptId": attempt["id"],
    })
    assert preview.status_code == 200, preview.text
    assert not preview.json()["diagnostics"]["missingPlaceholders"]
    assert preview.json()["rendered"] == calls[0][0]
    assert len(calls) == 1
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content = ? WHERE key = 'assembly.final_image_top'", ("修改后的完整成片正文" + shot_section().replace("范围要求", "新取景要求"),))
    preview = client.post("/api/prompt-configs/assembly.pose_selection/preview", json={"content": "{{runtime.pose_shots}}", "attemptId": attempt["id"]})
    assert "新取景要求" in preview.json()["rendered"]
    assert preview.json()["historicalPrompt"] == calls[0][0]
    with get_db() as db:
        assert db.execute("SELECT request_snapshot_json FROM generation_attempts WHERE id = ?", (attempt["id"],)).fetchone()[0] == original
        assert db.execute("SELECT plan_json FROM generation_pose_selections WHERE task_id = ?", (ids["task_id"],)).fetchone()[0] == frozen


def test_upgrade_fills_only_missing_shot_and_is_idempotent():
    missing = "自定义全局规则\n" + shot_section().replace("{{#runtime.is_shot_2}}镜头2范围要求\n{{/runtime.is_shot_2}}", "") + "\n## 其他自定义\n保留其他正文"
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content = ? WHERE key = 'assembly.final_image_bottom'", (missing,))
        migration_pose_shot_sections(db)
        updated = db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_bottom'").fetchone()[0]
        assert "自定义全局规则" in updated and updated.endswith("保留其他正文")
        assert "腰头完整入画" in updated
        rows = {"assembly.final_image_bottom": {"content": updated, "group_type": "assembly"}}
        assert len(app_main.render_pose_shot_requirements({"category_family": "bottom"}, rows)) == 5
        migration_pose_shot_sections(db)
        assert db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_bottom'").fetchone()[0] == updated
        for custom in ("", "自定义且没有章节", shot_section()):
            db.execute("UPDATE prompt_configs SET content = ? WHERE key = 'assembly.final_image_bottom'", (custom,))
            migration_pose_shot_sections(db)
            assert db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_bottom'").fetchone()[0] == custom


def test_invalid_config_is_reported_in_preview_before_attempt_or_model_call(client, monkeypatch):
    ids = prepare_task()
    for index in range(5):
        seed_pose(f"姿势{index}")
    async def forbidden(*args, **kwargs):
        raise AssertionError("invalid configuration must never invoke provider")
    monkeypatch.setattr(app_main, "generate_gemini_json_text", forbidden)
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content = ? WHERE key = 'assembly.final_image_top'", (shot_section().replace("镜头2范围要求", ""),))
        count = db.execute("SELECT COUNT(*) FROM generation_attempts").fetchone()[0]
    response = client.post(f'/api/generation-tasks/{ids["task_id"]}/select-poses', json={})
    assert response.status_code == 409
    assert "镜头 2" in response.json()["detail"]
    preview = client.post("/api/prompt-configs/assembly.pose_selection/preview", json={"content": "{{runtime.pose_shots}}"})
    # Samples use the bottom family, so validate the top draft explicitly too.
    preview = client.post("/api/prompt-configs/assembly.final_image_top/preview", json={"content": shot_section().replace("镜头2范围要求", "")})
    assert any("镜头 2" in message for message in preview.json()["diagnostics"]["missingPlaceholders"])
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM generation_attempts").fetchone()[0] == count
        assert db.execute("SELECT status FROM generation_tasks WHERE id = ?", (ids["task_id"],)).fetchone()[0] == "STYLING_READY"


def test_upgrade_preserves_existing_separate_shot_two_body():
    template = (Path(__file__).resolve().parents[1] / "prompt_templates" / "final_image_bottom.txt").read_text()
    legacy = template.replace("## 机位&景别", "## 当前镜头：腰臀至大腿中段近景", 1)
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content = ? WHERE key = 'assembly.final_image_bottom'", (legacy,))
        migration_pose_shot_sections(db)
        assert db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_image_bottom'").fetchone()[0] == template
