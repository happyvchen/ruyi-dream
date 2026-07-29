from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import main as app_main
from app.db import get_db
from app.gemini_provider import GeminiProviderError
from app.generation_progress import (
    ERROR_RATE_LIMITED,
    ERROR_SYSTEM_ERROR,
    ERROR_UPLOAD_REQUIREMENT_NOT_MET,
    classify_attempt_error,
)
from conftest import insert_styling_garment


def test_user_garment_contract_exposes_current_safe_error_code_without_provider_details(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    garment_id = insert_styling_garment(code="RATE-LIMITED-LOOK", status="TAGGING_FAILED")

    async def fail_before_attempt(_garment_id: int) -> dict:
        raise GeminiProviderError("provider request id must stay private", ERROR_RATE_LIMITED)

    monkeypatch.setattr(app_main, "tag_styling_garment_with_gemini", fail_before_attempt)
    asyncio.run(app_main._run_styling_garment_tagging_guarded(garment_id))

    list_item = client.get("/api/styling-garments").json()["items"][0]
    detail_item = app_main.get_styling_garment_by_id(garment_id, include_debug=False)
    admin_item = app_main.get_styling_garment_by_id(garment_id, include_debug=True)

    assert list_item["taggingErrorCode"] == ERROR_RATE_LIMITED
    assert detail_item["taggingErrorCode"] == ERROR_RATE_LIMITED
    assert list_item["tagError"] is None
    assert detail_item["tagError"] is None
    assert admin_item["tagError"] == "provider request id must stay private"
    assert list_item["tagAttempts"] == []
    assert detail_item["tagAttempts"] == []


def test_new_failure_does_not_reuse_an_older_failed_attempt(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    garment_id = insert_styling_garment(code="LATEST-ATTEMPT-WINS", status="TAGGING_FAILED")
    failed_attempt_id = app_main.create_attempt("styling_garment_tagging", garment_id, "gemini")
    app_main.finish_attempt(failed_attempt_id, "FAILED", error_code=ERROR_RATE_LIMITED)
    with get_db() as db:
        db.execute(
            "UPDATE styling_garments SET status = 'TAGGING_RUNNING', tag_error = NULL, tag_error_code = NULL WHERE id = ?",
            (garment_id,),
        )

    async def fail_before_attempt(_garment_id: int) -> dict:
        raise RuntimeError("current local validation failure")

    monkeypatch.setattr(app_main, "tag_styling_garment_with_gemini", fail_before_attempt)
    asyncio.run(app_main._run_styling_garment_tagging_guarded(garment_id))

    item = client.get("/api/styling-garments").json()["items"][0]

    assert item["taggingErrorCode"] == ERROR_SYSTEM_ERROR
    assert item["tagError"] is None


def test_empty_selected_files_returns_safe_upload_requirement_error_to_user(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    garment_id = insert_styling_garment(code="NO-USABLE-PHOTO", status="TAGGING_RUNNING")

    async def reject_empty_selection(_garment_id: int) -> dict:
        return app_main.normalize_prompt_tagging_result(
            {"selected_files": [], "ai_tags": {}},
            {"images": []},
        )

    monkeypatch.setattr(app_main, "tag_styling_garment_with_gemini", reject_empty_selection)
    asyncio.run(app_main._run_styling_garment_tagging_guarded(garment_id))

    list_item = client.get("/api/styling-garments").json()["items"][0]
    detail_item = app_main.get_styling_garment_by_id(garment_id, include_debug=False)

    assert list_item["taggingErrorCode"] == ERROR_UPLOAD_REQUIREMENT_NOT_MET
    assert detail_item["taggingErrorCode"] == ERROR_UPLOAD_REQUIREMENT_NOT_MET
    assert list_item["tagError"] is None
    assert detail_item["tagError"] is None


@pytest.mark.parametrize("raw", [{"ai_tags": {}}, {"selected_files": "look.jpg", "ai_tags": {}}])
def test_missing_or_wrong_type_selected_files_remains_a_model_contract_error(raw: dict) -> None:
    with pytest.raises(app_main.AIStylistContractError) as caught:
        app_main.normalize_prompt_tagging_result(raw, {"images": []})

    assert not isinstance(caught.value, app_main.UploadRequirementNotMetError)
    assert classify_attempt_error(caught.value) is None


def test_stale_cleanup_does_not_fail_a_tagging_job_waiting_for_the_local_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    garment_id = insert_styling_garment(code="QUEUED-LOOK", status="TAGGING_RUNNING")
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        db.execute("UPDATE styling_garments SET updated_at = ? WHERE id = ?", (stale_time, garment_id))

    async def no_op_tagging(_garment_id: int) -> None:
        return None

    async def scenario() -> None:
        app_main._tagging_semaphore = asyncio.Semaphore(0)
        monkeypatch.setattr(app_main, "_run_styling_garment_tagging_guarded", no_op_tagging)
        task = asyncio.create_task(app_main.run_styling_garment_tagging(garment_id))
        await asyncio.sleep(0)

        app_main.expire_stale_styling_garment_tagging()
        with get_db() as db:
            status = db.execute("SELECT status FROM styling_garments WHERE id = ?", (garment_id,)).fetchone()["status"]
        assert status == "TAGGING_RUNNING"

        app_main._tagging_semaphore.release()
        await task

    try:
        asyncio.run(scenario())
    finally:
        app_main._tagging_semaphore = None
        with app_main._active_tagging_jobs_lock:
            app_main._active_styling_garment_tagging_ids.discard(garment_id)
