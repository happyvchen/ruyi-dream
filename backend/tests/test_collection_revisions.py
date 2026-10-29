from app import main as app_main
from app.db import get_db, init_db, now_iso
from conftest import insert_generation_task, insert_media_asset


def revision(client, scope):
    response = client.get(f'/api/collection-revisions/{scope}')
    assert response.status_code == 200
    assert len(response.content) < 100
    return response.json()['revision']


def test_pose_changes_and_deletion_advance_revision_but_reads_and_heartbeats_do_not(client):
    before = revision(client, 'poses')
    with get_db() as db:
        pose_id = db.execute("INSERT INTO poses(code, name, status, created_at, updated_at) VALUES ('p', '站姿', 'DRAFT', ?, ?)", (now_iso(), now_iso())).lastrowid
    created = revision(client, 'poses')
    assert created > before
    assert revision(client, 'poses') == created
    with get_db() as db:
        db.execute("UPDATE poses SET updated_at = ?, name = name WHERE id = ?", (now_iso(), pose_id))
    assert revision(client, 'poses') == created
    with get_db() as db:
        db.execute("UPDATE poses SET status = 'READY', ai_tags_json = '{}' WHERE id = ?", (pose_id,))
    ready = revision(client, 'poses')
    assert ready > created
    with get_db() as db:
        db.execute('DELETE FROM poses WHERE id = ?', (pose_id,))
    assert revision(client, 'poses') > ready


def test_revision_is_transactional_and_migration_preserves_counters(client):
    before = revision(client, 'records')
    task_id = insert_generation_task(image_count=0)
    created = revision(client, 'records')
    assert created > before
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET name = 'rolled back' WHERE id = ?", (task_id,))
        db.rollback()
    assert revision(client, 'records') == created
    init_db()
    assert revision(client, 'records') == created


def test_media_changes_invalidate_visible_collections(client):
    asset_id = insert_media_asset()
    before = {scope: revision(client, scope) for scope in ('poses', 'garments', 'records', 'models')}
    with get_db() as db:
        db.execute("UPDATE media_assets SET thumb_key = 'derived/thumb.jpg' WHERE id = ?", (asset_id,))
    assert all(revision(client, scope) > value for scope, value in before.items())


def test_garment_and_model_changes_are_isolated(client):
    before = {scope: revision(client, scope) for scope in ('poses', 'garments', 'records', 'models')}
    with get_db() as db:
        db.execute("INSERT INTO styling_garments(code, status, created_at, updated_at) VALUES ('g', 'DRAFT', ?, ?)", (now_iso(), now_iso()))
    assert revision(client, 'garments') > before['garments']
    assert revision(client, 'poses') == before['poses']
    assert revision(client, 'models') == before['models']
    with get_db() as db:
        db.execute("UPDATE model_profiles SET name = name || ' updated'")
    assert revision(client, 'models') > before['models']


def test_pose_revision_respects_admin_access_and_rejects_unknown_scope(client, monkeypatch):
    monkeypatch.setattr(app_main, 'get_request_role', lambda request: 'user')
    assert client.get('/api/collection-revisions/poses').status_code == 403
    assert client.get('/api/collection-revisions/garments').status_code == 200
    assert client.get('/api/collection-revisions/unknown').status_code == 404
