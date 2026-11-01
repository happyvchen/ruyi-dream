"""Recovery contract: temporary DB and mocked network/storage; no paid calls."""
import asyncio
import pytest
from app import main, image_provider
from app.db import get_db
from app.generation_progress import (
    ERROR_AUTH_FAILED, ERROR_DELIVERY_FAILED, ERROR_GATEWAY_5XX,
    ERROR_PROVIDER_GENERATION_FAILED, ERROR_SUBMISSION_UNKNOWN, ERROR_TASK_NOT_FOUND,
)
from conftest import insert_media_asset
from test_generation_record_compatibility import populated_group

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(main.settings, 'image_provider', 'toapis')
    monkeypatch.setattr(main.settings, 'toapis_api_key', 'test-key')
    for field in ('generation_submit_retry_limit', 'generation_unknown_submission_retry_limit', 'generation_status_retry_limit', 'generation_delivery_retry_limit'):
        monkeypatch.setattr(main.settings, field, 0)
    monkeypatch.setattr(main, 'insert_generated_media_asset', lambda *a: insert_media_asset('recovered.png'))
    for name in ('get_json', 'post_json', 'store_delivered_image'):
        monkeypatch.setattr(image_provider, name, lambda *a, **kw: pytest.fail('unmocked remote/storage operation'))
    async def no_wait(*args): pass
    monkeypatch.setattr(main.asyncio, 'sleep', no_wait)

def failed_shot(snapshot=None, code=ERROR_DELIVERY_FAILED):
    ids, group, images, packages = populated_group()
    attempt = main.create_attempt('generation_result_image', images[0], 'image-2', request_snapshot=packages[0])
    snapshot = snapshot if snapshot is not None else {'provider': 'toapis', 'status': 'SUBMITTED', 'providerRequestId': 'original'}
    main.persist_attempt_provider_progress(attempt, snapshot)
    main.finish_attempt(attempt, 'FAILED', response_snapshot=snapshot, error_code=code)
    with get_db() as db:
        db.execute("UPDATE generation_result_images_new SET status='FAILED', active_attempt_id=? WHERE id=?", (attempt, images[0]))
        db.execute("UPDATE generation_result_sets SET status='PARTIAL' WHERE id=?", (group,))
        db.execute("UPDATE generation_tasks SET status='FINAL_PARTIAL' WHERE id=?", (ids['task_id'],))
    return ids, group, images, attempt

def active(image):
    with get_db() as db:
        return db.execute('SELECT active_attempt_id FROM generation_result_images_new WHERE id=?', (image,)).fetchone()[0]

def run(ids, group, image):
    return asyncio.run(main._generate_single_final_image_unlocked(ids['task_id'], group, image, attempt_id=active(image)))

@pytest.mark.parametrize('initial', ['id', 'business_id', 'output', 'base64'])
def test_retry_recovers_without_submitting(client, monkeypatch, initial):
    snapshots = {
        'id': {'provider': 'toapis', 'status': 'SUBMITTED', 'providerRequestId': 'original'},
        'business_id': {'provider': 'toapis', 'status': 'SUBMISSION_UNKNOWN', 'providerRequest': {'client_business_id': 'business-original'}},
        'output': {'provider': 'toapis', 'status': 'SUCCEEDED', 'outputPath': 'saved/original.png'},
        'base64': {'provider': 'apiyi', 'status': 'SUCCEEDED', 'providerStatusResponse': {'data': [{'b64_json': 'saved'}]}},
    }
    ids, group, images, original = failed_shot(snapshots[initial], ERROR_SUBMISSION_UNKNOWN)
    checks, deliveries = [], []
    def check(snapshot, key):
        checks.append(snapshot['providerRequestId'])
        return {'status': 'IN_PROGRESS' if len(checks) == 1 else 'SUCCEEDED', 'providerStatus': 'in_progress'}
    def deliver(snapshot, key):
        deliveries.append(snapshot)
        return {'outputPath': key}
    monkeypatch.setattr(main, 'run_image_provider_check', check)
    monkeypatch.setattr(main, 'run_image_provider_deliver', deliver)
    monkeypatch.setattr(main, 'run_image_provider_submit', lambda *a: pytest.fail('must recover, not submit'))
    before = main.get_generation_result_set(group)['images'][1:]
    response = client.post(f'/api/generation-result-images-new/{images[0]}/retry')
    assert response.status_code == 200, response.text
    assert client.post(f'/api/generation-result-images-new/{images[0]}/retry').status_code == 409
    run(ids, group, images[0])
    assert main.get_generation_result_set(group)['images'][0]['status'] == 'SUCCEEDED'
    assert main.get_generation_result_set(group)['images'][1:] == before
    assert len(checks) == (2 if initial in {'id', 'business_id'} else 0)
    assert len(deliveries) == (0 if initial == 'output' else 1)
    with get_db() as db:
        assert db.execute('SELECT status FROM generation_attempts WHERE id=?', (original,)).fetchone()[0] == 'FAILED'
        assert db.execute('SELECT retry_of_attempt_id FROM generation_attempts WHERE id=?', (active(images[0]),)).fetchone()[0] == original

@pytest.mark.parametrize('outcome', ['missing', 'failed', 'network', 'auth', 'delivery', 'expired'])
def test_only_confirmed_unavailable_results_regenerate(client, monkeypatch, outcome):
    ids, group, images, _ = failed_shot()
    submissions = []
    def check(snapshot, key):
        errors = {'missing': ERROR_TASK_NOT_FOUND, 'network': ERROR_GATEWAY_5XX, 'auth': ERROR_AUTH_FAILED}
        if outcome in errors:
            raise image_provider.ImageProviderError('query failed', error_code=errors[outcome])
        if outcome == 'failed':
            return {'status': 'FAILED', 'providerError': {'code': ERROR_PROVIDER_GENERATION_FAILED}}
        return {'status': 'SUCCEEDED'}
    def deliver(snapshot, key):
        raise image_provider.ImageProviderError('download failed', error_code=ERROR_TASK_NOT_FOUND if outcome == 'expired' else ERROR_DELIVERY_FAILED)
    def submit(package, key, progress, cancel):
        submissions.append(package)
        assert main.get_attempt_response_snapshot(active(images[0]))['clientBusinessId']
        return {'provider': 'toapis', 'providerRequestId': 'new', 'status': 'SUCCEEDED', 'outputPath': key}
    monkeypatch.setattr(main, 'run_image_provider_check', check)
    monkeypatch.setattr(main, 'run_image_provider_deliver', deliver)
    monkeypatch.setattr(main, 'run_image_provider_submit', submit)
    assert client.post(f'/api/generation-result-images-new/{images[0]}/retry').status_code == 200
    if outcome in {'missing', 'failed', 'expired'}:
        run(ids, group, images[0])
        assert len(submissions) == 1
        assert submissions[0]['prompt'] == 'Frozen shot 0'
    else:
        with pytest.raises(image_provider.ImageProviderError): run(ids, group, images[0])
        assert not submissions
        assert main.get_attempt_provider_resume_snapshot(active(images[0]))['providerRequestId'] == 'original'

def test_redeliver_never_regenerates_even_if_provider_failed(client, monkeypatch):
    ids, group, images, _ = failed_shot()
    monkeypatch.setattr(main, 'run_image_provider_check', lambda *a: {'status': 'FAILED', 'providerError': {'code': ERROR_PROVIDER_GENERATION_FAILED}})
    monkeypatch.setattr(main, 'run_image_provider_submit', lambda *a: pytest.fail('redelivery is not generation'))
    assert client.post(f'/api/generation-result-images-new/{images[0]}/redeliver').status_code == 200
    with pytest.raises(image_provider.ImageProviderError): run(ids, group, images[0])

def test_untraceable_submission_requires_explicit_regeneration(client):
    ids, group, images, original = failed_shot({'provider': 'apiyi', 'httpRequestId': 'trace-only', 'status': 'SUBMISSION_UNKNOWN'}, ERROR_SUBMISSION_UNKNOWN)
    assert main.get_generation_result_set(group)['images'][0]['recoveryRequiresRegeneration']
    assert client.post(f'/api/generation-result-images-new/{images[0]}/retry').status_code == 409
    assert client.post(f'/api/generation-result-sets/{group}/continue').status_code == 200
    assert active(images[0]) == original
    assert client.post(f'/api/generation-result-images-new/{images[0]}/retry', json={'forceRegenerate': True}).status_code == 200
    assert main.get_attempt_provider_resume_snapshot(active(images[0])) is None

def test_continue_uses_same_recovery_policy(client):
    ids, group, images, original = failed_shot()
    assert client.post(f'/api/generation-result-sets/{group}/continue').status_code == 200
    snapshot = main.get_attempt_provider_resume_snapshot(active(images[0]))
    assert snapshot['providerRequestId'] == 'original'
    assert snapshot['recovery']['sourceAttemptId'] == original
    assert snapshot['recovery']['allowRegenerate']

def test_recovery_routes_to_original_provider_and_blocks_changed_credentials(monkeypatch):
    snapshot = {**main.image_provider_context(), 'providerRequestId': 'original'}
    providers = []
    class Provider:
        async def check(self, row): return {'status': 'SUCCEEDED'}
    monkeypatch.setattr(main, 'get_image_provider', lambda name: providers.append(name) or Provider())
    monkeypatch.setattr(main.settings, 'image_provider', 'apiyi')
    assert main.run_image_provider_check(snapshot, 'out')['status'] == 'SUCCEEDED'
    assert providers == ['toapis']
    monkeypatch.setattr(main.settings, 'toapis_api_key', 'different-account')
    with pytest.raises(image_provider.ImageProviderError) as exc: main.run_image_provider_check(snapshot, 'out')
    assert exc.value.error_code == ERROR_AUTH_FAILED
    assert providers == ['toapis']

def test_recovery_fallback_has_one_submission_budget(client, monkeypatch):
    ids, group, images, _ = failed_shot()
    submissions = []
    failed = {'status': 'FAILED', 'providerError': {'code': ERROR_PROVIDER_GENERATION_FAILED}}
    monkeypatch.setattr(main.settings, 'generation_submit_retry_limit', 10)
    monkeypatch.setattr(main, 'run_image_provider_check', lambda *a: failed)
    monkeypatch.setattr(main, 'run_image_provider_submit', lambda *a: submissions.append(1) or {**failed, 'providerRequestId': 'new'})
    assert client.post(f'/api/generation-result-images-new/{images[0]}/retry').status_code == 200
    with pytest.raises(image_provider.ImageProviderError): run(ids, group, images[0])
    assert submissions == [1]
    snapshot = main.get_attempt_provider_resume_snapshot(active(images[0]))
    assert snapshot['recovery']['fallbackUsed']
    with pytest.raises(image_provider.ImageProviderError):
        asyncio.run(main.generate_image_asset({'prompt': 'frozen', 'imageInputs': [{}]}, 'out', active(images[0])))
    assert submissions == [1]

def test_saved_download_url_is_refreshed(monkeypatch):
    calls = []
    provider = image_provider.GPTImage2ToapisProvider()
    async def check(row): return {'status': 'SUCCEEDED', 'providerStatusResponse': {'url': 'fresh'}}
    def store(key, url, response):
        calls.append(url)
        if url == 'old': raise main.GenerationPhaseError('expired URL', ERROR_DELIVERY_FAILED)
        return {'outputPath': key}
    monkeypatch.setattr(provider, 'check', check)
    monkeypatch.setattr(image_provider, 'store_delivered_image', store)
    assert asyncio.run(provider.deliver({'output_path': 'out'}, {'url': 'old'}))['outputPath'] == 'out'
    assert calls == ['old', 'fresh']


def test_restart_after_submission_uses_durable_business_id(client, monkeypatch):
    ids, group, images, _ = failed_shot()
    assert client.post(f'/api/generation-result-images-new/{images[0]}/retry', json={'forceRegenerate': True}).status_code == 200
    request_id = active(images[0])
    def interrupted(package, key, progress, cancel):
        progress(2, 2)
        snapshot = main.get_attempt_response_snapshot(request_id)
        assert snapshot['status'] == 'SUBMITTING'
        assert snapshot['clientBusinessId'] == package['clientBusinessId']
        raise SystemExit('process interrupted before response persisted')
    monkeypatch.setattr(main, 'run_image_provider_submit', interrupted)
    with pytest.raises(SystemExit): run(ids, group, images[0])
    saved_id = main.get_attempt_provider_resume_snapshot(request_id)['providerRequestId']
    checked = []
    def check(snapshot, key):
        checked.append(snapshot['providerRequestId'])
        return {'status': 'SUCCEEDED', 'providerRequestId': 'recovered-server-id'}
    monkeypatch.setattr(main, 'run_image_provider_submit', lambda *a: pytest.fail('restart must not submit'))
    monkeypatch.setattr(main, 'run_image_provider_check', check)
    monkeypatch.setattr(main, 'run_image_provider_deliver', lambda snapshot, key: {'outputPath': key})
    run(ids, group, images[0])
    assert checked == [saved_id]


def test_cancel_after_recovery_lookup_never_submits(client, monkeypatch):
    ids, group, images, _ = failed_shot()
    assert client.post(f'/api/generation-result-images-new/{images[0]}/retry').status_code == 200
    def check(*args):
        main.cancel_generation_task(ids['task_id'])
        raise image_provider.ImageProviderError('Task not found', error_code=ERROR_TASK_NOT_FOUND)
    monkeypatch.setattr(main, 'run_image_provider_check', check)
    monkeypatch.setattr(main, 'run_image_provider_submit', lambda *a: pytest.fail('cancelled'))
    assert run(ids, group, images[0]) == 0
    assert main.get_generation_result_set(group)['images'][0]['status'] == 'CANCELLED'


@pytest.mark.parametrize('confirmed', [False, True])
def test_only_structured_task_not_found_allows_regeneration(monkeypatch, confirmed):
    def get(*args, **kwargs):
        raise image_provider.ImageProviderError('404', {'providerResponse':
            {'error': {'code': 404, 'message': 'Task not found'}} if confirmed else {'message': '<html>404</html>'}},
            error_code=ERROR_TASK_NOT_FOUND)
    monkeypatch.setattr(image_provider, 'get_json', get)
    with pytest.raises(image_provider.ImageProviderError) as exc:
        asyncio.run(image_provider.GPTImage2ToapisProvider().check({'provider_request_id': 'original'}))
    assert (exc.value.error_code == ERROR_TASK_NOT_FOUND) is confirmed


@pytest.mark.parametrize('expired', [False, True])
def test_missing_result_url_requires_confirmed_expiration(monkeypatch, expired):
    provider = image_provider.GPTImage2ToapisProvider()
    async def check(row):
        return {'status': 'SUCCEEDED', 'providerStatusResponse': {'expires_at': 1 if expired else 9999999999}}
    monkeypatch.setattr(provider, 'check', check)
    with pytest.raises((image_provider.ImageProviderError, main.GenerationPhaseError)) as exc:
        asyncio.run(provider.deliver({'output_path': 'out'}, {}))
    assert exc.value.error_code == (ERROR_TASK_NOT_FOUND if expired else ERROR_DELIVERY_FAILED)


def test_manual_continue_ignores_exhausted_automatic_retry_counter(client):
    ids, group, images, original = failed_shot()
    with get_db() as db:
        db.execute('UPDATE generation_tasks SET auto_run_retry_count=10 WHERE id=?', (ids['task_id'],))
    assert client.post(f'/api/generation-result-sets/{group}/continue').status_code == 200
    assert active(images[0]) != original
    assert main.get_attempt_provider_resume_snapshot(active(images[0]))['providerRequestId'] == 'original'


def test_recovery_without_original_inputs_can_still_register_saved_output(client, monkeypatch):
    ids, group, images, original = failed_shot({'provider': 'apiyi', 'status': 'SUCCEEDED', 'outputPath': 'saved/original.png'})
    with get_db() as db:
        db.execute("UPDATE generation_attempts SET request_snapshot_json='{}' WHERE id=?", (original,))
    monkeypatch.setattr(main, 'build_new_final_task_package', lambda *a: pytest.fail('no new prompt required'))
    assert client.post(f'/api/generation-result-images-new/{images[0]}/retry').status_code == 200
    run(ids, group, images[0])
    assert main.get_generation_result_set(group)['images'][0]['status'] == 'SUCCEEDED'
