"""Cancellation at provider boundaries; all requests are local fakes."""
import asyncio
from contextlib import contextmanager

import pytest

from app import gemini_provider, image_provider, main
from app.db import get_db
from app.generation_progress import ERROR_RATE_LIMITED, ERROR_TASK_NOT_FOUND
from conftest import insert_generation_task


@pytest.mark.parametrize("boundary", ["before_call", "slot_wait", "retry_wait"])
def test_text_cancel_prevents_next_http_request(monkeypatch, boundary):
    cancelled = boundary == "before_call"
    calls = []

    @contextmanager
    def slot():
        nonlocal cancelled
        if boundary == "slot_wait":
            cancelled = True
        yield

    def post(*args, **kwargs):
        calls.append(1)
        if boundary == "retry_wait" and len(calls) == 1:
            raise gemini_provider.GeminiProviderError("429", ERROR_RATE_LIMITED)
        return "{}"

    def sleep(seconds):
        nonlocal cancelled
        cancelled = True

    monkeypatch.setattr(gemini_provider, "toapis_concurrency_slot", slot)
    monkeypatch.setattr(gemini_provider, "_post_toapis_chat", post)
    monkeypatch.setattr(gemini_provider.time, "sleep", sleep)
    with pytest.raises(RuntimeError):
        gemini_provider._post_toapis_chat_with_retry({}, should_cancel=lambda: cancelled)
    assert len(calls) == (1 if boundary == "retry_wait" else 0)


@pytest.mark.parametrize("boundary", ["before_call", "upload", "slot_wait", "retry_wait"])
def test_image_submission_boundaries_obey_cancel(monkeypatch, boundary):
    task_id = insert_generation_task(status="STYLING_RUNNING")
    attempt_id = main.create_attempt("generation_styling", task_id, "image-2")
    calls = []
    monkeypatch.setattr(main.settings, "toapis_api_key", "fake")
    monkeypatch.setattr(main.settings, "image_provider", "toapis")

    def cancel():
        main.cancel_generation_task(task_id)

    @contextmanager
    def slot():
        if boundary == "slot_wait":
            cancel()
        yield

    def upload(*args):
        if boundary == "upload":
            cancel()
        return [], []

    def post(*args, **kwargs):
        calls.append(1)
        if boundary == "retry_wait" and len(calls) == 1:
            raise image_provider.ImageProviderError("429", error_code=ERROR_RATE_LIMITED, response_received=True)
        raise RuntimeError("request sent despite cancellation")

    monkeypatch.setattr(image_provider, "prepare_toapis_image_inputs", upload)
    monkeypatch.setattr(image_provider, "toapis_concurrency_slot", slot)
    monkeypatch.setattr(image_provider, "post_json", post)
    monkeypatch.setattr(image_provider.time, "sleep", lambda seconds: cancel())
    if boundary == "before_call":
        cancel()
    with pytest.raises(RuntimeError):
        asyncio.run(main.generate_image_asset({"prompt": "test", "imageInputs": []}, "unused", attempt_id))
    assert len(calls) == (1 if boundary == "retry_wait" else 0)


@pytest.mark.parametrize("failure", ["generation", "unknown_submission"])
def test_cancel_during_image_backoff_never_resubmits(monkeypatch, failure):
    task_id = insert_generation_task(status="STYLING_RUNNING")
    attempt_id = main.create_attempt("generation_styling", task_id, "image-2")
    calls = []
    monkeypatch.setattr(main.settings, "toapis_api_key", "fake")

    def submit(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("unexpected paid resubmission")
        if failure == "unknown_submission":
            raise image_provider.ProviderSubmissionUnknownError("timeout")
        return {"status": "FAILED", "providerRequestId": "fake",
                "providerError": {"code": ERROR_RATE_LIMITED}}

    async def cancel_on_sleep(seconds):
        main.cancel_generation_task(task_id)

    def check(*args):
        raise image_provider.ImageProviderError("404", error_code=ERROR_TASK_NOT_FOUND)

    monkeypatch.setattr(main, "run_image_provider_submit", submit)
    monkeypatch.setattr(main, "run_image_provider_check", check)
    monkeypatch.setattr(main.asyncio, "sleep", cancel_on_sleep)
    with pytest.raises(RuntimeError):
        asyncio.run(main.generate_image_asset({"imageInputs": []}, "unused", attempt_id))
    assert len(calls) == 1


def test_old_styling_failure_cannot_fail_restarted_task(monkeypatch):
    task_id = insert_generation_task(status="READY_FOR_STYLING")
    monkeypatch.setattr(main, "snapshot_default_model", lambda: {})
    monkeypatch.setattr(main, "build_ai_stylist_context", lambda version: ({}, {}, {}))
    monkeypatch.setattr(main, "attach_ai_stylist_prompt_snapshot", lambda request, candidates: request)
    new_attempt = []

    async def select(*args, **kwargs):
        main.cancel_generation_task(task_id)
        # Another request starts a new run before the old provider returns.
        with get_db() as db:
            db.execute("UPDATE generation_tasks SET status = 'STYLING_RUNNING' WHERE id = ?", (task_id,))
        new_attempt.append(main.create_attempt("generation_styling", task_id, "deepseek"))
        raise RuntimeError("old request returned after cancellation")

    monkeypatch.setattr(main, "select_styling_with_gemini", select)
    asyncio.run(main.run_select_generation_styling(task_id))
    assert main.get_generation_task_row(task_id)["status"] == "STYLING_RUNNING"
    with get_db() as db:
        statuses = db.execute("SELECT status FROM generation_attempts ORDER BY id").fetchall()
    assert [row[0] for row in statuses] == ["CANCELLED", "RUNNING"]


def test_cancelled_queue_job_cannot_start_model_work(client, monkeypatch):
    task_id = insert_generation_task(status="READY_FOR_STYLING")
    with get_db() as db:
        db.execute("UPDATE generation_tasks SET auto_run_target = ? WHERE id = ?", (main.AUTO_RUN_TARGET_FINAL, task_id))
    response = client.post(f"/api/generation-tasks/{task_id}/cancel-auto-run")
    assert response.status_code == 200

    async def forbidden(*args, **kwargs):
        pytest.fail("cancelled queued job started")

    monkeypatch.setattr(main, "run_select_generation_styling", forbidden)
    asyncio.run(main.run_auto_final_generation(task_id))


@pytest.mark.parametrize("channel", ["toapis", "direct"])
@pytest.mark.parametrize("cancel_entry", ["task", "batch"])
def test_styling_cancel_reaches_transport_retry(monkeypatch, channel, cancel_entry):
    task_id = insert_generation_task(status="STYLING_RUNNING", batch_id="cancel-test")
    attempt_id = main.create_attempt("generation_styling", task_id, "deepseek")
    calls = []
    monkeypatch.setattr(main.settings, "gemini_provider", channel)
    monkeypatch.setattr(main.settings, "toapis_api_key", "fake")
    monkeypatch.setattr(main.settings, "gemini_api_key", "fake")

    def request(*args, **kwargs):
        calls.append(1)
        raise gemini_provider.GeminiProviderError("429", ERROR_RATE_LIMITED)

    def cancel(seconds):
        if cancel_entry == "batch":
            main.cancel_generation_batch("cancel-test")
        else:
            main.cancel_generation_task(task_id)

    async def direct(*args):
        return request()

    async def sleep(seconds):
        cancel(seconds)

    monkeypatch.setattr(gemini_provider, "_post_toapis_chat", request)
    monkeypatch.setattr(gemini_provider, "_generate_via_google", direct)
    monkeypatch.setattr(gemini_provider.time, "sleep", cancel)
    monkeypatch.setattr(gemini_provider.asyncio, "sleep", sleep)
    with pytest.raises(RuntimeError, match="取消"):
        asyncio.run(main.select_styling_with_gemini(
            {"task_id": task_id, "image_snapshot_json": "[]"}, {},
            {"prompt": "test"}, attempt_id=attempt_id))
    assert len(calls) == 1
