from app.db import get_db
from conftest import insert_generation_task
from scripts import repair_media_metadata as repair


def test_repair_is_bounded_resumable_and_reentrant(monkeypatch):
    task_id = insert_generation_task(image_count=3)
    calls = []
    def metadata(key):
        # Open another connection during IO: no database write lock held here.
        with get_db() as db:
            db.execute("UPDATE generation_tasks SET updated_at = updated_at WHERE id = ?", (task_id,))
        calls.append(key)
        return {"thumbKey": key + '.thumb', "previewKey": key + '.preview', "sizeBytes": 10,
                "thumbSizeBytes": 1, "previewSizeBytes": 2, "updatedAt": 'test'}
    monkeypatch.setattr(repair, 'media_metadata', metadata)
    assert repair.repair_media_assets(2) == 2
    assert len(calls) == 2
    assert repair.repair_media_assets(2) == 1
    assert repair.repair_media_assets(2) == 0
    assert repair.repair_media_assets(1, verify=True) == 1
