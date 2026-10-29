from __future__ import annotations

import asyncio
import copy
from io import BytesIO
from zipfile import ZipFile

import pytest

from app import main, image_provider
from app.db import get_db, dumps, loads, migration_published_image_versions
from app.generation_records import adapt_replay_references, capture_published_image
from conftest import insert_media_asset, insert_ready_generation_task_with_preview
from test_invalidation_export_and_recovery_contract import create_result_set


def populated_group(ids=None):
    ids = ids or insert_ready_generation_task_with_preview()
    group, images = create_result_set(ids['task_id'], ids['styling_id'], ids['preview_id'], ['SUCCEEDED'] * 5)
    packages = []
    for index, image in enumerate(images):
        package = {'prompt': f'Frozen shot {index}', 'imageInputs': [
            {'role': 'hero_garment', 'objectKey': f'original/hero-{index}.png'},
            {'role': 'model_identity', 'objectKey': f'original/model-{index}.png'}],
            'generation': {'stage': 'final_image'}}
        packages.append(package)
        attempt = main.create_attempt('generation_result_image', image, 'image-2', request_snapshot=package)
        with get_db() as db:
            asset = db.execute('SELECT current_asset_id FROM generation_result_images_new WHERE id=?', (image,)).fetchone()[0]
            main.finish_attempt(attempt, 'SUCCEEDED', output_asset_id=asset, db=db)
            db.execute('UPDATE generation_result_images_new SET active_attempt_id=? WHERE id=?', (attempt, image))
            capture_published_image(db, image)
    with get_db() as db:
        db.execute("UPDATE generation_result_sets SET status='SUCCEEDED' WHERE id=?", (group,))
        db.execute("UPDATE generation_tasks SET status='COMPLETED' WHERE id=?", (ids['task_id'],))
    return ids, group, images, packages


def test_migration_is_idempotent_and_preserves_per_shot_facts(client):
    ids, group, images, packages = populated_group()
    with get_db() as db:
        db.execute('DELETE FROM generation_image_versions')
        # Simulate old group-level pointer changes: each attempt remains authoritative.
        db.execute("UPDATE generation_task_input_versions SET model_snapshot_json='{}' WHERE task_id=?", (ids['task_id'],))
        before = [tuple(row) for row in db.execute('SELECT * FROM generation_attempts')]
        migration_published_image_versions(db)
        versions = [tuple(row) for row in db.execute('SELECT * FROM generation_image_versions')]
        migration_published_image_versions(db)
        assert [tuple(row) for row in db.execute('SELECT * FROM generation_image_versions')] == versions
        assert [tuple(row) for row in db.execute('SELECT * FROM generation_attempts')] == before
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert [main.build_new_final_task_package(ids['task_id'], image)['imageInputs'] for image in images] == [p['imageInputs'] for p in packages]


@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancel', 'superseded_attempt'])
def test_retry_retains_published_image_and_only_publishes_active_success(client, monkeypatch, outcome):
    ids, group, images, packages = populated_group()
    original = main.get_generation_result_set(group)
    selected = original['images'][0]['versions'][0]['id']
    response = client.post(f'/api/generation-result-images-new/{images[0]}/retry', json={'versionId': selected, 'userInstruction': '只减少配饰'})
    assert response.status_code == 200, response.text
    attempt = response.json()['item']['resultSet']['images'][0]['activeAttemptId']
    output = insert_media_asset('new.png')
    calls = []
    async def generate(package, *_args, **_kwargs):
        calls.append(copy.deepcopy(package))
        if outcome == 'failure':
            raise RuntimeError('Mock failure')
        if outcome == 'cancel':
            main.cancel_generation_task(ids['task_id'])
        if outcome == 'superseded_attempt':
            replacement = main.create_attempt('generation_result_image', images[0], 'image-2', request_snapshot=packages[0])
            with get_db() as db:
                db.execute('UPDATE generation_result_images_new SET active_attempt_id=? WHERE id=?', (replacement, images[0]))
        return output, {'status': 'succeeded'}
    monkeypatch.setattr(main, 'generate_image_asset', generate)
    monkeypatch.setattr(main, 'snapshot_default_model', lambda: pytest.fail('Must not read current model'))
    asyncio.run(main.run_final_generation(ids['task_id'], group, [images[0]], {images[0]: attempt}))
    after = main.get_generation_result_set(group)
    assert calls[0]['imageInputs'] == packages[0]['imageInputs']
    assert '只减少配饰' in calls[0]['prompt']
    assert after['images'][1:] == original['images'][1:]
    shot = after['images'][0]
    assert len(shot['versions']) == (2 if outcome == 'success' else 1)
    assert shot['asset']['id'] == (output if outcome == 'success' else original['images'][0]['asset']['id'])
    with get_db() as db:
        exported = main.get_final_image_export_overview(db, ids['task_id'])
    assert original['images'][0]['asset']['id'] in {v['mediaAssetId'] for v in exported['downloadable']}


def test_all_groups_remain_in_normal_record_and_export_after_restart(client, monkeypatch):
    ids, older, _, _ = populated_group()
    _, newer, _, _ = populated_group(ids)
    monkeypatch.setattr(main.settings, 'user_password', 'user-pass')
    monkeypatch.setattr(main.settings, 'admin_password', 'admin-pass')
    for role in ('user', 'admin'):
        client.cookies.set('ruyi_session', main.create_session_cookie(role))
        item = client.get(f'/api/generation-records/{ids["task_id"]}').json()['item']
        assert [group['id'] for group in item['resultSets']] == [newer, older]
        assert all(len(shot['versions']) == 1 for group in item['resultSets'] for shot in group['images'])
    response = client.post(f'/api/generation-tasks/{ids["task_id"]}/auto-run-final', json={'restart': True})
    assert response.status_code == 200, response.text
    item = client.get(f'/api/generation-records/{ids["task_id"]}').json()['item']
    assert len(item['resultSets']) == 2 and item['poseWorkflowEnabled']
    with get_db() as db:
        assert len(main.get_final_image_export_overview(db, ids['task_id'])['downloadable']) == 10
    archive = client.get('/api/generation-task-exports/archive', params={'ids': str(ids['task_id'])})
    assert archive.status_code == 200, archive.text
    with ZipFile(BytesIO(archive.content)) as zipped:
        assert zipped.testzip() is None
        assert len(zipped.namelist()) == len(set(zipped.namelist()))
        assert len([n for n in zipped.namelist() if n.endswith('.jpg') or n.endswith('.png')]) == 10


def test_group_regeneration_copies_facts_and_restores_selected_context(client, monkeypatch):
    ids, older, images, packages = populated_group()
    _, newer, _, _ = populated_group(ids)
    with get_db() as db:
        db.execute('DELETE FROM shot_preset_slots')
        db.execute("UPDATE generation_stylings SET styling_plan_json='{}' WHERE id=?", (ids['styling_id'],))
    monkeypatch.setattr(main, 'snapshot_default_model', lambda: pytest.fail('Current model must not be read'))
    response = client.post(f'/api/generation-result-sets/{older}/regenerate')
    assert response.status_code == 200, response.text
    cloned = response.json()['item']['resultSet']
    assert cloned['id'] not in {older, newer}
    assert [main.build_new_final_task_package(ids['task_id'], i['id'])['imageInputs'] for i in cloned['images']] == [p['imageInputs'] for p in packages]
    with get_db() as db:
        old = db.execute('SELECT * FROM generation_result_sets WHERE id=?', (older,)).fetchone()
        new = db.execute('SELECT * FROM generation_result_sets WHERE id=?', (cloned['id'],)).fetchone()
        for key in ('input_version_id', 'styling_id', 'preview_id', 'pose_selection_id'):
            assert old[key] == new[key]
        assert db.execute('SELECT count(*) FROM generation_image_versions').fetchone()[0] == 10


def test_continue_only_failed_slots_without_requiring_current_category(client):
    ids, group, images, _ = populated_group()
    before = main.get_generation_result_set(group)['images']
    with get_db() as db:
        db.execute("UPDATE generation_stylings SET styling_plan_json='{}' WHERE id=?", (ids['styling_id'],))
        db.execute("UPDATE generation_result_sets SET status='SUPERSEDED' WHERE id=?", (group,))
        db.execute("UPDATE generation_result_images_new SET status='FAILED' WHERE id=?", (images[2],))
    response = client.post(f'/api/generation-result-sets/{group}/continue')
    assert response.status_code == 200, response.text
    after = main.get_generation_result_set(group)['images']
    assert after[2]['status'] == 'QUEUED'
    assert [after[i] for i in [0, 1, 3, 4]] == [before[i] for i in [0, 1, 3, 4]]


def test_missing_original_facts_remain_viewable_but_are_not_fabricated(client):
    ids = insert_ready_generation_task_with_preview()
    group, images = create_result_set(ids['task_id'], ids['styling_id'], ids['preview_id'], ['SUCCEEDED'] * 5)
    response = client.post(f'/api/generation-result-images-new/{images[0]}/retry')
    assert response.status_code == 409 and '输入记录不完整' in response.text
    assert main.get_generation_result_set(group)['images'][0]['asset']
    with get_db() as db:
        assert len(main.get_final_image_export_overview(db, ids['task_id'])['downloadable']) == 5
        assert db.execute('SELECT count(*) FROM generation_attempts').fetchone()[0] == 0


def test_version_and_group_scope_cannot_cross_tasks(client):
    ids, group, images, _ = populated_group()
    other, other_group, other_images, _ = populated_group()
    version = main.get_generation_result_set(other_group)['images'][0]['versions'][0]['id']
    assert client.post(f'/api/generation-result-images-new/{images[0]}/retry', json={'versionId': version}).status_code == 404
    response = client.post('/api/generation-task-exports/precheck', json={'ids': [ids['task_id']], 'resultSetId': other_group})
    assert response.status_code == 404


def test_delayed_old_worker_cannot_claim_a_newer_retry(client, monkeypatch):
    ids, group, images, _ = populated_group()
    old_attempt = main.get_generation_result_set(group)['images'][0]['activeAttemptId']
    response = client.post(f'/api/generation-result-images-new/{images[0]}/retry')
    assert response.status_code == 200
    before = main.get_generation_result_set(group)['images'][0]
    async def forbidden(*args, **kwargs): pytest.fail('Stale worker must not submit')
    monkeypatch.setattr(main, 'generate_image_asset', forbidden)
    asyncio.run(main._generate_single_final_image_unlocked(ids['task_id'], group, images[0], attempt_id=old_attempt))
    assert main.get_generation_result_set(group)['images'][0] == before


def test_reference_transport_preserves_all_sources_and_is_idempotent(monkeypatch):
    from PIL import Image
    original = {'prompt': 'Use image 7 as model identity.', 'imageInputs': [
        {'objectKey': f'original-{index}.png', 'role': 'model_identity'} for index in range(8)]}
    package = adapt_replay_references(original)
    assert len(original['imageInputs']) == 8 and len(package['imageInputs']) == 6
    assert package['originalImageInputs'] == original['imageInputs']
    assert package['imageInputs'][5]['collageItems'] == original['imageInputs'][5:]
    assert adapt_replay_references(package) == package
    assert image_provider.build_toapis_request_snapshot(package, [f'url-{i}' for i in range(6)])['prompt'] == package['prompt']
    seen = []
    def read(source, *_args):
        seen.append(source['objectKey'])
        output = BytesIO()
        Image.new('RGB', (30, 40), (255, 0, 0)).save(output, 'PNG')
        return output.getvalue(), 'image/png'
    monkeypatch.setattr(image_provider, 'reference_source_bytes', read)
    content, _, _ = image_provider.build_reference_collage_bytes(package['imageInputs'][5])
    with Image.open(BytesIO(content)) as image:
        image.verify()
    assert seen == [p['objectKey'] for p in original['imageInputs'][5:]]


def test_versions_and_execution_snapshots_protect_media_after_attempt_removal(client):
    ids, group, images, packages = populated_group()
    retained = insert_media_asset('retained.png', 'retained/source.png')
    with get_db() as db:
        db.execute('UPDATE generation_result_images_new SET execution_snapshot_json=? WHERE id=?',
                   (dumps({'imageInputs': [{'objectKey': 'retained/source.png'}]}), images[0]))
        db.execute('DELETE FROM generation_attempts')
        assert main.media_asset_snapshot_reference_count(db, 'retained/source.png') > 0
        main.release_media_asset_if_unreferenced(db, retained)
        assert db.execute('SELECT deleted_at FROM media_assets WHERE id=?', (retained,)).fetchone()[0] is None
        assert db.execute('SELECT count(*) FROM generation_image_versions').fetchone()[0] == 5
    assert main.model_asset_file_is_referenced('retained/source.png')
    assert main.model_asset_dir_is_referenced('retained')


@pytest.mark.parametrize('concurrent_change', [False, True])
def test_pose_selection_accepts_old_input_mismatch_but_rejects_concurrent_changes(client, monkeypatch, concurrent_change):
    from test_pose_workflow import prepare_task, seed_pose, mock_selection, run_selection
    ids = prepare_task()
    _, asset, code = seed_pose()
    with get_db() as db:
        original_input = db.execute('SELECT current_input_version_id FROM generation_tasks WHERE id=?', (ids['task_id'],)).fetchone()[0]
        old_input = dict(db.execute('SELECT * FROM generation_task_input_versions WHERE id=?', (original_input,)).fetchone())
        old_input.pop('id')
        old_input['version_number'] += 1
        columns = ','.join(old_input)
        placeholders = ','.join('?' for _ in old_input)
        changed_input = db.execute(f'INSERT INTO generation_task_input_versions ({columns}) VALUES ({placeholders})', tuple(old_input.values())).lastrowid
        db.execute('UPDATE generation_tasks SET current_input_version_id=? WHERE id=?', (changed_input, ids['task_id']))
    def concurrent_edit():
        if concurrent_change:
            with get_db() as db:
                db.execute('UPDATE generation_tasks SET current_input_version_id=? WHERE id=?', (original_input, ids['task_id']))
    mock_selection(monkeypatch, code, asset, after_shortlist=concurrent_edit)
    run_selection(ids['task_id'])
    task = main.get_generation_task_row(ids['task_id'])
    assert bool(task['active_pose_selection_id']) is (not concurrent_change)
    if not concurrent_change:
        assert task['current_input_version_id'] == original_input
