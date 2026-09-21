from contextlib import contextmanager
import gzip
import json
import logging

import pytest
from app import main
from app.db import get_db, now_iso, init_db
from conftest import insert_generation_task, insert_media_asset, insert_ready_generation_task_with_preview


def revision(task_id):
    return main.get_generation_task_status(task_id)["detailRevision"]


def test_status_is_small_and_never_assembles_detail(client, monkeypatch):
    task_id = insert_generation_task(status="FINAL_RUNNING")
    monkeypatch.setattr(main, "get_generation_task_by_id", lambda *a, **k: pytest.fail("detail assembled"))
    response = client.get(f"/api/generation-tasks/{task_id}/status")
    assert response.status_code == 200
    assert len(response.content) < 2048
    assert response.json()["progress"] == {"completed": 0, "total": 5}
    assert client.get("/api/generation-tasks/99999/status").status_code == 404


def test_revision_content_and_operational_boundaries(client):
    task_id = insert_generation_task(status="STYLING_RUNNING")
    before = revision(task_id)
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET updated_at = 'heartbeat', lease_owner = 'worker', lease_expires_at = 'lease', auto_run_retry_at = 'later', auto_run_retry_count = 3 WHERE id = ?", (task_id,))
    assert revision(task_id) == before
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET ai_styling_user_instruction_text = '修身' WHERE id = ?", (task_id,))
        inside = db.execute("SELECT detail_revision FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()[0]
        assert inside > before
    before = revision(task_id)
    with pytest.raises(RuntimeError), get_db() as db:
        db.execute("UPDATE generation_tasks SET name = 'rollback' WHERE id = ?", (task_id,))
        raise RuntimeError("rollback")
    assert revision(task_id) == before
    assert client.post(f"/api/generation-tasks/{task_id}/cancel").status_code == 200
    assert revision(task_id) > before
    before = revision(task_id)
    init_db()
    assert revision(task_id) == before  # migration is reentrant


def test_revision_styling_result_retry_input_and_media():
    ids = insert_ready_generation_task_with_preview(ensure_presets=False)
    task_id = ids["task_id"]
    timestamp = now_iso()
    before = revision(task_id)
    with get_db() as db:
        db.execute("UPDATE generation_stylings SET styling_plan_json = '{\"new\":true}' WHERE id = ?", (ids["styling_id"],))
    assert revision(task_id) > before
    with get_db() as db:
        task = db.execute("SELECT * FROM generation_tasks WHERE id = ?", (task_id,)).fetchone()
        result_id = db.execute("INSERT INTO generation_result_sets(task_id,input_version_id,styling_id,status,created_at,updated_at) VALUES (?,?,?,'RUNNING',?,?)", (task_id, task["current_input_version_id"], ids["styling_id"], timestamp, timestamp)).lastrowid
        db.execute("UPDATE generation_tasks SET active_result_set_id = ?, status = 'FINAL_RUNNING' WHERE id = ?", (result_id, task_id))
        image_id = db.execute("INSERT INTO generation_result_images_new(result_set_id,sort_order,created_at,updated_at) VALUES (?,1,?,?)", (result_id,timestamp,timestamp)).lastrowid
    asset_id = insert_media_asset()
    before = revision(task_id)
    with get_db() as db:
        db.execute("UPDATE generation_result_images_new SET status = 'SUCCEEDED', current_asset_id = ? WHERE id = ?", (asset_id,image_id))
    assert revision(task_id) > before
    assert main.get_generation_task_status(task_id)["progress"]["completed"] == 1
    before = revision(task_id)
    with get_db() as db:
        db.execute("UPDATE generation_result_images_new SET locked_by = 'worker', locked_until = 'later', run_attempts = 2 WHERE id = ?", (image_id,))
    assert revision(task_id) == before
    with get_db() as db:
        db.execute("UPDATE generation_result_images_new SET status = 'QUEUED' WHERE id = ?", (image_id,))
    assert revision(task_id) > before
    before = revision(task_id)
    with get_db() as db:
        db.execute("UPDATE generation_task_images SET media_asset_id = ? WHERE task_id = ?", (asset_id,task_id))
    assert revision(task_id) > before
    before = revision(task_id)
    with get_db() as db:
        db.execute("UPDATE media_assets SET preview_key = 'derived.webp' WHERE id = ?", (asset_id,))
    assert revision(task_id) > before
    before = revision(task_id)
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET status = 'COMPLETED' WHERE id = ?", (task_id,))
    assert revision(task_id) > before


def test_attempt_heartbeats_do_not_invalidate_but_errors_do():
    task_id = insert_generation_task(status="STYLING_RUNNING")
    with get_db() as db:
        attempt_id = db.execute("INSERT INTO generation_attempts(owner_type,owner_id,provider,created_at,updated_at) VALUES ('generation_styling',?,'mock',?,?)", (task_id,now_iso(),now_iso())).lastrowid
    before = revision(task_id)
    with get_db() as db:
        db.execute("UPDATE generation_attempts SET progress_json = '{\"current\":\"queue\"}', updated_at = 'heartbeat' WHERE id = ?", (attempt_id,))
    assert revision(task_id) == before
    with get_db() as db:
        db.execute("UPDATE generation_attempts SET status = 'FAILED', error_code = 'SYSTEM_ERROR', error_message = 'mock failure' WHERE id = ?", (attempt_id,))
    assert revision(task_id) > before


@pytest.mark.parametrize("variant", ["thumb", "preview"])
def test_missing_derivative_never_redirects_to_original(client, monkeypatch, variant):
    asset_id = insert_media_asset(object_key="media/test/original.jpg")
    monkeypatch.setattr(main, "object_exists", lambda key: key.endswith("original.jpg"))
    monkeypatch.setattr(main, "presigned_get_url", lambda *a, **k: pytest.fail("original fallback"))
    asset = main.get_media_asset_by_id(asset_id)
    assert asset[f"{variant}Status"] == "MISSING"
    assert asset[f"{variant}Url"] is None
    response = client.get("/api/media", params={"key": asset["objectKey"], "variant": variant}, follow_redirects=False)
    assert response.status_code == 404
    assert response.headers["cache-control"] == "private, no-store"
    with get_db() as db:
        db.execute(f"UPDATE media_assets SET {variant}_key = 'dangling.webp' WHERE id = ?", (asset_id,))
    assert client.get("/api/media", params={"key": asset["objectKey"], "variant": variant}, follow_redirects=False).status_code == 404


def test_list_queries_are_bounded_and_gzip_budget(client, monkeypatch, caplog):
    for index in range(30):
        insert_generation_task(name=f"LOOK-{index}", batch_id=f"batch-{index}" if index < 15 else None)
    counts = []
    original_get_db = main.get_db
    @contextmanager
    def measured_db():
        with original_get_db() as db:
            db.set_trace_callback(lambda sql: counts.append(sql) if sql.lstrip().upper().startswith("SELECT") else None)
            yield db
    monkeypatch.setattr(main, "get_db", measured_db)
    monkeypatch.setattr(main, "get_generation_task_by_id", lambda *a, **k: pytest.fail("heavy list"))
    caplog.set_level(logging.INFO, logger="uvicorn.error.performance")
    response = client.get("/api/generation-tasks?limit=30&secret=DO_NOT_LOG")
    assert len(counts) <= 6
    assert response.headers["content-encoding"] == "gzip"
    assert len(gzip.compress(response.content, compresslevel=5)) < 100_000
    assert "DO_NOT_LOG" not in " ".join(r.message for r in caplog.records if r.name == "uvicorn.error.performance")
    assert "response_bytes=" in caplog.text
    for item in response.json()["items"]:
        assert set(item["summary"]) == {"cover"}
        assert not {"styling", "inputVersion", "attempts", "stageUserInputs"} & item.keys()
    counts.clear()
    result = client.get("/api/generation-record-workspace?limit=30")
    assert result.status_code == 200
    assert len(counts) <= 6
    assert len(json.dumps(result.json()).encode()) < 100_000


def test_download_cache_control_is_preserved(client, monkeypatch):
    asset_id = insert_media_asset()
    monkeypatch.setattr(main, "object_exists", lambda key: True)
    seen = {}
    def presign(key, **kwargs):
        seen.update(kwargs)
        return "https://storage.test/download"
    monkeypatch.setattr(main, "presigned_get_url", presign)
    response = client.get("/api/media", params={"key": main.get_media_asset_by_id(asset_id)["objectKey"], "download": 1}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["cache-control"] == "private, no-store"
    assert seen["response_cache_control"] == "private, no-store"
    assert "content-encoding" not in response.headers


def test_same_fixture_legacy_and_light_list_measurement(monkeypatch):
    """Same isolated objects, actual HEAD serializer vs current serializer; no provider."""
    import ast
    import subprocess
    try:
        legacy_source = subprocess.check_output(["git", "show", "HEAD:backend/app/main.py"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("legacy comparison requires a Git checkout")
    names = {"serialize_generation_task", "get_generation_task_by_id", "get_generation_task_summary"}
    parsed = ast.parse(legacy_source)
    definitions = [node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if len(definitions) != len(names):
        pytest.skip("pre-P0 implementation is no longer present in HEAD")
    # Keep this benchmark valid after committing: HEAD may already be lightweight.
    if "task = get_generation_task_by_id(task_id" not in ast.unparse(next(node for node in definitions if node.name == "get_generation_task_summary")):
        pytest.skip("HEAD already uses lightweight summaries")
    namespace = dict(vars(main))
    exec(compile(ast.Module(body=definitions, type_ignores=[]), '<legacy-list-benchmark>', 'exec'), namespace)
    for index in range(30):
        insert_generation_task(name=f"LOOK-{index}", image_count=2)
    with get_db() as db:
        db.execute("UPDATE media_assets SET thumb_key = object_key || '.thumb', preview_key = object_key || '.preview'")
        rows = db.execute("SELECT * FROM generation_tasks ORDER BY id").fetchall()
    statements = []
    @contextmanager
    def traced():
        with get_db() as db:
            db.set_trace_callback(lambda sql: statements.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
            yield db
    monkeypatch.setattr(main, 'get_db', traced)
    namespace['get_db'] = traced
    legacy = [namespace['serialize_generation_task'](row, include_detail=False) for row in rows]
    legacy_queries = len(statements)
    statements.clear()
    current = main.generation_task_summaries(rows)
    def sizes(value):
        body = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        return {"jsonBytes": len(body), "gzipBytes": len(gzip.compress(body, compresslevel=5))}
    report = {"fixture": "30 draft tasks, 2 registered images per task, no attempts",
              "legacy": {**sizes(legacy), "selects": legacy_queries},
              "current": {**sizes(current), "selects": len(statements)},
              "statusBytes": len(json.dumps(main.get_generation_task_status(rows[0]['id'])).encode())}
    print('P0_MEASUREMENT=' + json.dumps(report))
    assert report['current']['jsonBytes'] < report['legacy']['jsonBytes']
    assert report['current']['selects'] < report['legacy']['selects']


def test_media_reparenting_invalidates_both_aggregates():
    first = insert_generation_task(image_count=1)
    second = insert_generation_task(image_count=0)
    previous = (revision(first), revision(second))
    with get_db() as db:
        db.execute("UPDATE generation_task_images SET task_id = ? WHERE task_id = ?", (second, first))
    assert revision(first) > previous[0]
    assert revision(second) > previous[1]


def test_har_summary_redacts_sources_and_counts_put_overlap():
    import runpy
    from pathlib import Path
    summarize = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/loadtest/summarize-har.py'))['summarize']
    entries = [{"startedDateTime": "2026-09-21T00:00:00Z", "time": 1000,
                "request": {"url": f"https://private.test/media/{index}?secret=NEVER_OUTPUT", "method": "PUT", "bodySize": 100},
                "response": {"status": 200, "bodySize": 0}} for index in range(3)]
    report = summarize(entries)
    assert report['peakConcurrentPUT'] == 3
    assert report['groups']['upload']['uploadBytes'] == 300
    assert 'NEVER_OUTPUT' not in json.dumps(report)
    assert 'private.test' not in json.dumps(report)
