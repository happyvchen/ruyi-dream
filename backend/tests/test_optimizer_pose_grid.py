import copy
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app import image_provider
from app.db import get_db, migration_optimizer_pose_grid
from app.prompt_optimization import POSE_GRID_LAYOUT, optimizer_package_batches, optimizer_reference_plan
from test_final_prompt_optimization import fourteen_inputs


def packages(indices=range(1, 6)):
    return {str(i): {"prompt": f"镜头 {i} 原始正文", "promptRuntime": {"shot_index": str(i)},
                    "imageInputs": fourteen_inputs(None, None, {"shot_index": i})} for i in indices}


def test_full_group_has_fourteen_attachments_and_exact_final_reference_mapping():
    source = packages()
    original = copy.deepcopy(source)
    inputs, images = optimizer_reference_plan(source, POSE_GRID_LAYOUT)
    assert source == original
    assert len(optimizer_package_batches(source, 16, POSE_GRID_LAYOUT)) == 1
    assert len(images) == 14
    assert [item["role"] for item in images] == [item["role"] for item in source["1"]["imageInputs"]]
    board = images[1]
    assert board["collageRule"]["cells"] == [1, 2, 3, 4, 5]
    assert board["tileLabels"] == ["SHOT 1", "SHOT 2", "SHOT 3", "SHOT 4", "SHOT 5", "EMPTY"]
    for shot in inputs["shots"]:
        for ref in shot["references"]:
            final_image = original[shot["shotId"]]["imageInputs"][ref["index"] - 1]
            attachment = images[ref["visionImageIndex"] - 1]
            if ref["role"] == "shot_pose":
                assert ref["cell"] == int(shot["shotIndex"])
                assert attachment["collageItems"][ref["cell"] - 1] == final_image
            else:
                assert attachment == final_image and "cell" not in ref
    assert inputs["imageManifest"][1]["emptyCells"] == [6]


def test_pending_subset_keeps_original_cells_and_never_mutates_final_images():
    source = packages([5, 2])
    source["2"]["imageInputs"][1]["providerCrop"] = {"kind": "keep_top_ratio", "ratio": 0.45}
    inputs, images = optimizer_reference_plan(source, POSE_GRID_LAYOUT)
    board = images[1]
    assert board["collageRule"]["cells"] == [2, 5]
    assert board["collageItems"][0]["providerCrop"]["ratio"] == 0.45
    assert board["tileLabels"] == ["EMPTY", "SHOT 2", "EMPTY", "EMPTY", "SHOT 5", "EMPTY"]
    assert [s["references"][1]["cell"] for s in inputs["shots"]] == [5, 2]
    board["collageItems"][0]["providerCrop"]["ratio"] = 1
    assert source["2"]["imageInputs"][1]["providerCrop"]["ratio"] == 0.45


def test_grid_renders_real_crops_and_preserves_both_views_in_fifth_cell(monkeypatch):
    monkeypatch.setattr(image_provider, "REFERENCE_COLLAGE_TILE_SIZE", 200)
    # Top red/bottom blue verifies that the per-shot crop is applied before fitting.
    portrait = Image.new("RGB", (100, 200), "blue")
    portrait.paste(Image.new("RGB", (100, 100), "red"), (0, 0))
    dual = Image.new("RGB", (200, 100), "green")
    dual.paste(Image.new("RGB", (100, 100), "yellow"), (100, 0))
    def raw(item, deadline=None, **kwargs):
        out = BytesIO()
        (portrait if item["objectKey"] == "pose-2" else dual).save(out, "PNG")
        return out.getvalue(), "image/png"
    monkeypatch.setattr(image_provider, "reference_source_bytes", raw)
    source = packages([2, 5])
    source["2"]["imageInputs"][1]["providerCrop"] = {"kind": "keep_top_ratio", "ratio": 0.5}
    _, images = optimizer_reference_plan(source, POSE_GRID_LAYOUT)
    content, _, _ = image_provider.build_reference_collage_bytes(images[1])
    board = Image.open(BytesIO(content))
    assert board.size == (600, 472)
    assert board.getpixel((300, 190))[0] > 240  # bottom of cell 2 is still red
    assert board.getpixel((250, 372))[1] > 100  # fifth cell left view is green
    assert board.getpixel((350, 372))[0] > 240  # fifth cell right view is yellow
    assert min(board.getpixel((500, 372))) > 245  # sixth cell is empty
    assert min(board.getpixel((100, 136))) > 245  # absent first shot stays empty


@pytest.mark.parametrize("change", ["missing_pose", "duplicate_pose", "invalid_index", "too_many_shared"])
def test_invalid_grid_rejected_before_provider_instead_of_splitting(change):
    source = packages()
    if change == "missing_pose":
        source["1"]["imageInputs"].pop(1)
    elif change == "duplicate_pose":
        source["1"]["imageInputs"].append(source["1"]["imageInputs"][1])
    elif change == "invalid_index":
        source["1"]["promptRuntime"]["shot_index"] = "6"
    else:
        source["1"]["imageInputs"].extend({"role": "hero_garment", "objectKey": f"extra{i}"} for i in range(3))
    with pytest.raises(ValueError):
        optimizer_package_batches(source, 16, POSE_GRID_LAYOUT)


def test_legacy_transport_keeps_old_batches():
    source = packages()
    assert [len(batch) for batch in optimizer_package_batches(source, 16)] == [3, 2]
    assert len(optimizer_reference_plan(source)[1]) == 18


def test_upgrade_appends_editable_guide_once_preserving_custom_rules_and_empty_slots(monkeypatch):
    original_read_text = Path.read_text

    def read_without_authoring_template(path, *args, **kwargs):
        if path.name == "final_prompt_optimizer.txt":
            raise AssertionError("Released migration must not depend on the editable authoring template")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_without_authoring_template)
    with get_db() as db:
        db.execute("UPDATE prompt_configs SET content = '自定义规则\n保留原样', enabled = 1 WHERE key = 'assembly.final_prompt_optimizer'")
        migration_optimizer_pose_grid(db)
        row = db.execute("SELECT * FROM prompt_configs WHERE key = 'assembly.final_prompt_optimizer'").fetchone()
        content = row["content"]
        assert content.startswith("自定义规则\n保留原样\n\n") and row["enabled"] == 1
        assert "references.cell" in content and "references.index" in content
        assert "第五格若含正背两个视图" in content
        migration_optimizer_pose_grid(db)
        assert db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_prompt_optimizer'").fetchone()[0] == content
        db.execute("UPDATE prompt_configs SET content = '' WHERE key = 'assembly.final_prompt_optimizer'")
        migration_optimizer_pose_grid(db)
        assert db.execute("SELECT content FROM prompt_configs WHERE key = 'assembly.final_prompt_optimizer'").fetchone()[0] == ""


def test_original_geometry_keeps_full_pose_and_does_not_pad_attachment(monkeypatch):
    monkeypatch.setattr(image_provider, "REFERENCE_COLLAGE_TILE_SIZE", 200)
    portrait = Image.new("RGB", (100, 200), "blue")
    portrait.paste(Image.new("RGB", (100, 100), "red"), (0, 0))
    out = BytesIO()
    portrait.save(out, "PNG")
    monkeypatch.setattr(image_provider, "reference_source_bytes", lambda *args, **kwargs: (out.getvalue(), "image/png"))
    source = packages([2])
    source["2"]["referenceGeometry"] = "original_v1"
    _, images = optimizer_reference_plan(source, POSE_GRID_LAYOUT)
    grid = images[1]
    assert "providerCanvas" not in grid
    assert "providerCrop" not in grid["collageItems"][0]
    direct, _ = image_provider.render_reference_input_bytes(grid["collageItems"][0])
    assert direct == out.getvalue()  # Neither cropping, padding nor re-encoding.
    content, _ = image_provider.render_reference_input_bytes(grid)
    board = Image.open(BytesIO(content))
    assert board.size == (600, 472)  # Layout only; no extra square canvas.
    assert board.getpixel((300, 80))[0] > 240
    assert board.getpixel((300, 210))[2] > 240  # Lower body survives.
