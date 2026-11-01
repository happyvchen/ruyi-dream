from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from io import BytesIO
import random

import pytest
import requests
from PIL import Image

from app import config, deepseek_inputs, gemini_provider as provider
from app.generation_progress import (
    ERROR_AUTH_FAILED, ERROR_INSUFFICIENT_BALANCE, ERROR_INVALID_REQUEST,
    ERROR_MALFORMED_OUTPUT,
)


@pytest.fixture()
def settings(monkeypatch):
    settings = config.Settings()
    settings.gemini_provider = 'deepseek'
    settings.deepseek_api_key = 'official-test-key'
    settings.deepseek_base_url = 'https://api.deepseek.com'
    settings.deepseek_model = 'deepseek-flash'
    settings.deepseek_timeout_seconds = 120
    settings.deepseek_connect_timeout_seconds = 20
    settings.deepseek_max_concurrency = 2
    settings.toapis_api_key = 'toapis-test-key'
    settings.gemini_api_key = 'google-test-key'
    settings.gemini_retry_limit = 2
    settings.gemini_retry_backoff_base_seconds = 1
    settings.gemini_retry_backoff_cap_seconds = 2
    settings.gemini_retry_time_budget_seconds = 300
    monkeypatch.setattr(provider, 'get_settings', lambda: settings)
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: pytest.fail('Unexpected network request'))
    monkeypatch.setattr(provider, 'toapis_concurrency_slot', lambda: pytest.fail('Official API used ToAPIs slot'))
    return settings


class Response:
    def __init__(self, status=200, payload=None, chunks=None):
        self.status_code = status
        self.ok = status < 400
        self.payload = payload
        self.chunks = chunks
        self.text = json.dumps(payload)
        self.headers = {'Content-Type': 'text/event-stream' if chunks is not None else 'application/json'}
        self.closed = False

    def json(self):
        return self.payload

    def iter_lines(self, **kwargs):
        yield b': keep-alive'
        for chunk in self.chunks:
            yield ('data: ' + json.dumps(chunk, ensure_ascii=False)).encode('utf-8')
        yield b'data: [DONE]'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


def call(**kwargs):
    return asyncio.run(provider.generate_gemini_json_text('Return JSON', **kwargs))


def image_input(size=(40, 40), fmt='PNG'):
    image = Image.frombytes('RGB', size, random.Random(42).randbytes(size[0] * size[1] * 3))
    output = BytesIO()
    image.save(output, fmt)
    return {'data': output.getvalue(), 'mime_type': 'intentionally/wrong'}


@pytest.mark.parametrize('base_url', ['https://api.deepseek.com', 'https://api.deepseek.com/v1/'])
def test_official_multimodal_stream_and_attribution(settings, monkeypatch, base_url):
    settings.deepseek_base_url = base_url
    response = Response(chunks=[
        {'choices': [{'delta': {'reasoning_content': 'analysis'}}]},
        {'choices': [{'delta': {'content': '{"结果":"成功"}'}}]},
        {'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': {'completion_tokens': 20}},
    ])
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        return Response(payload={'id': 'file-api-test'}) if url.endswith('/files') else response

    monkeypatch.setattr(requests, 'post', post)
    original = image_input()
    result = call(images=[original], system_instruction='System rules')
    assert json.loads(result) == {'结果': '成功'}
    assert len(calls) == 2
    upload_url, upload_request = calls[0]
    assert upload_url == base_url.rstrip('/') + '/files'
    assert upload_request['files']['file'][1:] == (original['data'], 'image/png')
    url, request = calls[1]
    assert url == base_url.rstrip('/') + '/chat/completions'
    assert request['headers']['Authorization'] == 'Bearer official-test-key'
    assert request['timeout'] == (20, 120)
    assert request['stream'] is True
    payload = request['json']
    assert payload['model'] == 'deepseek-flash'
    assert payload['messages'][0] == {'role': 'system', 'content': 'System rules'}
    assert payload['messages'][1]['content'][1] == {'type': 'file', 'file_id': 'file-api-test'}
    assert payload['stream_options'] == {'include_usage': True}
    assert response.closed
    assert provider.gemini_request_meta() == {
        'provider': 'deepseek', 'channel': 'deepseek', 'gemini_provider': 'deepseek', 'model': 'deepseek-flash',
    }


def test_missing_official_key_never_falls_back(settings):
    settings.deepseek_api_key = ''
    assert not provider.is_gemini_configured()
    with pytest.raises(RuntimeError, match='DEEPSEEK_API_KEY'):
        call()


@pytest.mark.parametrize('status,code', [(400, ERROR_INVALID_REQUEST), (401, ERROR_AUTH_FAILED),
    (402, ERROR_INSUFFICIENT_BALANCE), (422, ERROR_INVALID_REQUEST)])
def test_official_terminal_errors_not_retried(settings, monkeypatch, status, code):
    calls = []
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: calls.append(1) or Response(status, {'error': {'message': 'rejected'}}))
    with pytest.raises(provider.GeminiProviderError) as error:
        call()
    assert error.value.error_code == code
    assert error.value.provider_response['providerError']['code'] == code
    assert 'DeepSeek 官方' in str(error.value)
    assert 'TOAPIS_API_KEY' not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize('status', [429, 500, 503])
def test_official_retry_and_json_fallback(settings, monkeypatch, status):
    failure = Response(status, {'error': {'message': 'busy'}})
    failure.headers['Retry-After'] = '7'
    responses = [failure, Response(payload={'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]})]
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: responses.pop(0))
    waits, snapshots = [], []
    monkeypatch.setattr(provider.time, 'sleep', waits.append)
    assert json.loads(call(on_retry=snapshots.append)) == {'ok': True}
    assert waits == ([7] if status == 429 else [1])
    assert snapshots[0]['transportRetries'] == 1
    assert not responses


def test_official_retry_can_be_deferred(settings, monkeypatch):
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: Response(429, {'error': {'message': 'busy'}}))
    snapshots = []
    with pytest.raises(provider.GeminiRetryDeferred):
        call(defer_retry=True, initial_retry_count=1, on_retry=snapshots.append)
    assert snapshots[0]['transportRetries'] == 2


def test_official_cancellation_after_slot_admission(settings, monkeypatch):
    cancelled = False

    @contextmanager
    def slot():
        nonlocal cancelled
        cancelled = True
        yield

    monkeypatch.setattr(provider, '_chat_concurrency_slot', slot)
    with pytest.raises(RuntimeError, match='取消'):
        call(should_cancel=lambda: cancelled)


def test_official_truncation_not_retried(settings, monkeypatch):
    calls = []
    response = Response(chunks=[{'choices': [{'delta': {'content': '{"partial":'}, 'finish_reason': 'length'}]}])
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: calls.append(1) or response)
    with pytest.raises(provider.GeminiProviderError) as error:
        call()
    assert error.value.error_code == ERROR_MALFORMED_OUTPUT
    assert len(calls) == 1


def test_official_input_limits_fail_before_network(settings, monkeypatch):
    with pytest.raises(provider.GeminiProviderError, match='最多 16'):
        call(images=[{'data': b'image'}] * 17)
    monkeypatch.setattr(provider, 'DEEPSEEK_MAX_REQUEST_BYTES', 1)
    with pytest.raises(provider.GeminiProviderError, match='48 MiB'):
        call()


@pytest.mark.parametrize('field', ['deepseek_timeout_seconds', 'deepseek_connect_timeout_seconds', 'deepseek_max_concurrency'])
def test_official_config_validation(settings, field):
    settings.validate()
    setattr(settings, field, 0)
    with pytest.raises(RuntimeError, match=field.upper()):
        settings.validate()


def test_switch_back_to_toapis_preserves_model_key_url_and_slot(settings, monkeypatch):
    from app import image_provider

    settings.gemini_provider = 'toapis'
    settings.gemini_toapis_model = 'deepseek-v4.1-flash'
    settings.toapis_base_url = 'https://toapis.example'
    monkeypatch.setattr(image_provider, 'get_settings', lambda: settings)
    slots, calls = [], []

    @contextmanager
    def slot():
        slots.append('entered')
        yield
        slots.append('released')

    monkeypatch.setattr(provider, 'toapis_concurrency_slot', slot)
    monkeypatch.setattr(requests, 'post', lambda url, **kw: calls.append((url, kw)) or Response(
        payload={'choices': [{'message': {'content': '{}'}}]}))
    assert call() == '{}'
    url, request = calls[0]
    assert url == 'https://toapis.example/v1/chat/completions'
    assert request['headers']['Authorization'] == 'Bearer toapis-test-key'
    assert request['json']['model'] == 'deepseek-v4.1-flash'
    assert slots == ['entered', 'released']
    assert provider.gemini_request_meta()['channel'] == 'toapis'


def test_official_concurrency_slot_released_on_failure(settings):
    with pytest.raises(RuntimeError):
        with provider._chat_concurrency_slot():
            semaphore = provider._deepseek_semaphore
            assert semaphore.acquire(blocking=False)
            assert not semaphore.acquire(blocking=False)
            semaphore.release()
            raise RuntimeError('request failure')
    assert semaphore.acquire(blocking=False)
    assert semaphore.acquire(blocking=False)
    assert not semaphore.acquire(blocking=False)
    semaphore.release()
    semaphore.release()


def test_all_images_use_files_without_altering_text_pixels_or_order(settings, monkeypatch):
    monkeypatch.setattr(deepseek_inputs, 'MAX_BODY_BYTES', 10000)
    prompt, system = '保留原文\n' * 30, '完整系统规则  '
    images = [image_input(), image_input((50, 50))]
    plan = deepseek_inputs.plan_request(prompt, images, system, 'deepseek-flash')
    assert list(plan.uploads) == [0, 1]
    uploaded = []
    plan.resolve_files(lambda item: uploaded.append(item) or f'file-api-{len(uploaded)}')
    assert [item['data'] for item in uploaded] == [image['data'] for image in images]
    assert deepseek_inputs.body_size(plan.payload) <= 10000
    assert plan.payload['messages'][0]['content'] == system
    content = plan.payload['messages'][1]['content']
    assert content[0]['text'] == prompt
    assert content[1:] == [{'type': 'file', 'file_id': 'file-api-1'}, {'type': 'file', 'file_id': 'file-api-2'}]
    assert not any(f['resized'] for f in plan.facts)


@pytest.mark.parametrize('fmt,mime', [('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('WEBP', 'image/webp'), ('GIF', 'image/gif')])
def test_supported_format_detected_without_reencoding(settings, fmt, mime):
    original = image_input(fmt=fmt)
    plan = deepseek_inputs.plan_request('text', [original], None, 'deepseek-flash')
    assert plan.uploads[0]['mime_type'] == mime
    assert plan.uploads[0]['data'] == original['data']


@pytest.mark.parametrize('count,expected', [(14, (64, 32)), (15, (32, 16)), (16, (32, 16))])
def test_dimension_rule_changes_at_fifteen_images(settings, monkeypatch, count, expected):
    monkeypatch.setattr(deepseek_inputs, 'MAX_EDGE', 64)
    monkeypatch.setattr(deepseek_inputs, 'MANY_IMAGE_MAX_EDGE', 32)
    original = image_input((64, 32))
    plan = deepseek_inputs.plan_request('text', [original] * count, None, 'deepseek-flash')
    for fact in plan.facts:
        assert fact['dimensions'] == expected
        assert fact['resized'] == (count >= 15)
    with Image.open(BytesIO(plan.uploads[0]['data'])) as decoded:
        assert decoded.size == expected
        assert decoded.format == 'PNG'


def test_single_and_total_byte_limits_independent_of_body(settings, monkeypatch):
    image = image_input()
    n = len(image['data'])
    monkeypatch.setattr(deepseek_inputs, 'MAX_FILE_BYTES', n)
    assert list(deepseek_inputs.plan_request('text', [image], None, 'deepseek-flash').uploads) == [0]
    monkeypatch.setattr(deepseek_inputs, 'MAX_TOTAL_BYTES', n * 2)
    assert len(deepseek_inputs.plan_request('text', [image] * 2, None, 'deepseek-flash').uploads) == 2
    monkeypatch.setattr(deepseek_inputs, 'MAX_TOTAL_BYTES', n)
    with pytest.raises(ValueError, match='200 MiB'):
        deepseek_inputs.plan_request('text', [image] * 2, None, 'deepseek-flash')
    monkeypatch.setattr(deepseek_inputs, 'MAX_FILE_BYTES', n - 1)
    with pytest.raises(ValueError, match='单图 64 MiB'):
        deepseek_inputs.plan_request('text', [image], None, 'deepseek-flash')


def test_invalid_format_model_and_oversized_text_fail_before_upload(settings, monkeypatch):
    with pytest.raises(provider.GeminiProviderError, match='格式'):
        call(images=[image_input(fmt='BMP')])
    with pytest.raises(provider.GeminiProviderError, match='解码'):
        call(images=[{'data': b'broken'}])
    settings.deepseek_model = 'deepseek-v4-pro'
    with pytest.raises(provider.GeminiProviderError, match='deepseek-flash'):
        call(images=[image_input()])
    settings.deepseek_model = 'deepseek-flash'
    monkeypatch.setattr(deepseek_inputs, 'MAX_BODY_BYTES', 1)
    with pytest.raises(provider.GeminiProviderError, match='未截断提示词'):
        call(images=[image_input()])


def test_file_uploads_reused_after_upload_failure_and_chat_retry(settings, monkeypatch):
    images = [image_input(), image_input((50, 50))]
    uploads, chats, waits = [], [], []
    monkeypatch.setattr(provider.time, 'sleep', waits.append)

    def post(url, **kw):
        assert kw['headers']['Authorization'] == 'Bearer official-test-key'
        if url.endswith('/files'):
            uploads.append(kw)
            assert kw['data'] == {'purpose': 'user_data', 'expires_after[anchor]': 'created_at',
                                  'expires_after[seconds]': '86400'}
            if len(uploads) == 2:
                return Response(429, {'error': {'message': 'busy'}})
            return Response(payload={'id': f'file-api-{len(uploads)}'})
        chats.append(kw['json'])
        if len(chats) == 1:
            return Response(503, {'error': {'message': 'busy'}})
        return Response(payload={'choices': [{'message': {'content': '{}'}}]})

    monkeypatch.setattr(requests, 'post', post)
    assert call(images=images) == '{}'
    assert len(uploads) == 3 and len(chats) == 2
    assert uploads[0]['files']['file'][1] == images[0]['data']
    assert uploads[1]['files']['file'][1] == uploads[2]['files']['file'][1] == images[1]['data']
    assert chats[-1]['messages'][0]['content'][1:] == [
        {'type': 'file', 'file_id': 'file-api-1'}, {'type': 'file', 'file_id': 'file-api-3'}]
    assert waits == [1, 2]  # shared budget, no nested retry loop


def test_cancel_after_upload_prevents_next_upload_and_chat(settings, monkeypatch):
    cancelled, calls = False, []

    def post(url, **kw):
        nonlocal cancelled
        calls.append(url)
        cancelled = True
        return Response(payload={'id': 'file-api-test'})

    monkeypatch.setattr(requests, 'post', post)
    with pytest.raises(RuntimeError, match='取消'):
        call(images=[image_input()] * 2, should_cancel=lambda: cancelled)
    assert calls == ['https://api.deepseek.com/files']


@pytest.mark.parametrize('status,code', [(400, ERROR_INVALID_REQUEST), (401, ERROR_AUTH_FAILED),
    (402, ERROR_INSUFFICIENT_BALANCE), (413, ERROR_INVALID_REQUEST), (422, ERROR_INVALID_REQUEST)])
def test_upload_terminal_errors_do_not_submit_chat(settings, monkeypatch, status, code):
    calls = []
    monkeypatch.setattr(requests, 'post', lambda url, **kw: calls.append(url) or Response(status, {'error': {'message': 'rejected'}}))
    with pytest.raises(provider.GeminiProviderError) as error:
        call(images=[image_input()])
    assert error.value.error_code == code
    assert calls == ['https://api.deepseek.com/files']


def test_toapis_never_uses_official_input_policy(settings, monkeypatch):
    from app import image_provider
    settings.gemini_provider = 'toapis'
    monkeypatch.setattr(image_provider, 'get_settings', lambda: settings)
    monkeypatch.setattr(deepseek_inputs, 'plan_request', lambda *a: pytest.fail('DeepSeek policy leaked'))
    monkeypatch.setattr(provider, '_post_toapis_chat_with_retry', lambda payload, **kw: json.dumps(payload))
    payload = json.loads(call(images=[{'data': b'original', 'mime_type': 'image/webp'}]))
    assert payload['messages'][0]['content'][1] == {
        'type': 'image_url', 'image_url': {'url': 'data:image/webp;base64,b3JpZ2luYWw='}}
