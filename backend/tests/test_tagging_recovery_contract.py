from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from app import main as app_main
from app.db import get_db
from app.db import run_migrations
from app.gemini_provider import GeminiProviderError
from app.generation_progress import (
    ERROR_RATE_LIMITED,
    ERROR_SYSTEM_ERROR,
    ERROR_UPLOAD_REQUIREMENT_NOT_MET,
    classify_attempt_error,
)
from conftest import insert_styling_garment


def test_migration_065_repairs_database_where_063_was_already_recorded() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    db.executemany(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, '2026-08-11T00:00:00Z')",
        [(version, "already_applied") for version in [
            "050", "051", "052", "053", "054", "055", "056", "057",
            "058", "059", "060", "061", "062", "063", "064",
        ]],
    )
    db.execute(
        """CREATE TABLE styling_garments (
               id INTEGER PRIMARY KEY,
               status TEXT NOT NULL,
               tag_retry_at TEXT,
               tag_retry_state_json TEXT,
               tag_active_attempt_id INTEGER
           )"""
    )

    run_migrations(db)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(styling_garments)")}
    versions = {row["version"] for row in db.execute("SELECT version FROM schema_migrations")}

    assert "tag_retry_lease_until" in columns
    assert "065" in versions


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


def test_stale_unowned_tagging_job_is_scheduled_for_process_recovery() -> None:
    garment_id = insert_styling_garment(code="CRASH-RECOVERY", status="TAGGING_RUNNING")
    attempt_id = app_main.create_attempt("styling_garment_tagging", garment_id, "gemini")
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        db.execute(
            "UPDATE styling_garments SET updated_at = ?, tag_active_attempt_id = ? WHERE id = ?",
            (stale_time, attempt_id, garment_id),
        )

    app_main.expire_stale_styling_garment_tagging()

    with get_db() as db:
        row = db.execute(
            "SELECT status, tag_retry_at, tag_retry_state_json, tag_active_attempt_id FROM styling_garments WHERE id = ?",
            (garment_id,),
        ).fetchone()
        attempt = db.execute("SELECT status FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
    state = app_main.loads(row["tag_retry_state_json"], {})
    assert row["status"] == "TAGGING_RUNNING"
    assert row["tag_retry_at"] is not None
    assert row["tag_active_attempt_id"] == attempt_id
    assert state["kind"] == "process"
    assert state["processRetries"] == 1
    assert attempt["status"] == "RUNNING"


def test_stale_process_retry_exhaustion_preserves_terminal_summary() -> None:
    garment_id = insert_styling_garment(code="CRASH-EXHAUSTED", status="TAGGING_RUNNING")
    attempt_id = app_main.create_attempt("styling_garment_tagging", garment_id, "gemini")
    retry_limit = app_main.settings.gemini_retry_limit
    state = {"kind": "process", "processRetries": retry_limit, "retryLimit": retry_limit}
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        db.execute(
            """UPDATE styling_garments
               SET updated_at = ?, tag_active_attempt_id = ?, tag_retry_state_json = ? WHERE id = ?""",
            (stale_time, attempt_id, app_main.dumps(state), garment_id),
        )

    app_main.expire_stale_styling_garment_tagging()

    item = app_main.get_styling_garment_by_id(garment_id, include_debug=True)
    assert item["status"] == "TAGGING_FAILED"
    assert item["tagRetryAt"] is None
    assert item["tagRetryCount"] == retry_limit
    assert item["tagRetryKind"] == "process"
    attempt = item["tagAttempts"][0]
    assert attempt["status"] == "FAILED"
    assert attempt["responseSnapshot"]["processRetries"] == retry_limit


def test_tagging_missing_ai_tags_is_corrected_and_reasked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    garment_id = insert_styling_garment(code="CONTRACT-REASK", status="TAGGING_RUNNING")
    garment = {
        "id": garment_id,
        "code": "CONTRACT-REASK",
        "images": [
            {
                "id": 1,
                "sortOrder": 0,
                "asset": {"filename": "front.jpg", "objectKey": "", "mimeType": "image/jpeg"},
            }
        ],
    }
    prompts: list[str] = []
    results = iter(
        [
            '{"selected_files":["image:1:front.jpg"]}',
            '{"selected_files":["image:1:front.jpg"],"ai_tags":{"一级品类":["上装"]}}',
        ]
    )

    async def generate(prompt: str, _images: list[dict], **_kwargs: object) -> str:
        prompts.append(prompt)
        return next(results)

    monkeypatch.setattr(app_main, "get_styling_garment_by_id", lambda _id: garment)
    monkeypatch.setattr(app_main, "build_prompt_tagging_prompt", lambda _garment: "BASE TAGGING PROMPT")
    monkeypatch.setattr(app_main, "build_prompt_tagging_request_snapshot", lambda *_args: {})
    monkeypatch.setattr(app_main, "is_gemini_configured", lambda: True)
    monkeypatch.setattr(app_main, "generate_gemini_json_text", generate)
    monkeypatch.setattr(app_main, "generation_retry_backoff_seconds", lambda _attempt: 0.0)

    with pytest.raises(app_main.GeminiRetryDeferred):
        asyncio.run(app_main.tag_styling_garment_with_gemini(garment_id))
    with get_db() as db:
        retry_row = db.execute(
            "SELECT tag_retry_at, tag_retry_state_json, tag_active_attempt_id FROM styling_garments WHERE id = ?",
            (garment_id,),
        ).fetchone()
    retry_state = app_main.loads(retry_row["tag_retry_state_json"], {})
    attempt_id = int(retry_row["tag_active_attempt_id"])

    result = asyncio.run(app_main.tag_styling_garment_with_gemini(garment_id))
    app_main.persist_styling_garment_tagging_result(garment_id, result)

    assert result["ai_tags"] == {"一级品类": ["上装"]}
    assert result["attempt_id"] == attempt_id
    assert len(prompts) == 2
    assert prompts[0] == "BASE TAGGING PROMPT"
    assert "上一次返回未通过系统契约校验" in prompts[1]
    assert "缺少 ai_tags 对象" in prompts[1]
    assert retry_row["tag_retry_at"] is not None
    assert retry_state["status"] == "TAGGING_CONTRACT_RETRY_WAIT"
    assert retry_state["contractRetries"] == 1
    with get_db() as db:
        attempt = db.execute("SELECT status, response_snapshot_json FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
    assert attempt["status"] == "SUCCEEDED"
    assert app_main.loads(attempt["response_snapshot_json"], {})["contractRetries"] == 1


def test_provider_success_snapshot_is_replayed_without_a_second_gemini_call_after_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    garment_id = insert_styling_garment(code="SUCCESS-REPLAY", status="TAGGING_RUNNING")
    garment = {
        "id": garment_id,
        "code": "SUCCESS-REPLAY",
        "images": [{"id": 1, "sortOrder": 0, "asset": {"filename": "front.jpg", "objectKey": "", "mimeType": "image/jpeg"}}],
    }
    calls = 0

    async def generate(_prompt: str, _images: list[dict], **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return '{"selected_files":["image:1:front.jpg"],"ai_tags":{"一级品类":["上装"]}}'

    monkeypatch.setattr(app_main, "get_styling_garment_by_id", lambda _id: garment)
    monkeypatch.setattr(app_main, "build_prompt_tagging_prompt", lambda _garment: "BASE")
    monkeypatch.setattr(app_main, "build_prompt_tagging_request_snapshot", lambda *_args: {})
    monkeypatch.setattr(app_main, "is_gemini_configured", lambda: True)
    monkeypatch.setattr(app_main, "generate_gemini_json_text", generate)
    monkeypatch.setattr(app_main, "generation_retry_backoff_seconds", lambda _attempt: 0.0)

    first_result = asyncio.run(app_main.tag_styling_garment_with_gemini(garment_id))
    attempt_id = int(first_result["attempt_id"])
    with get_db() as db:
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
        db.execute("UPDATE styling_garments SET updated_at = ? WHERE id = ?", (stale_time, garment_id))
    app_main.expire_stale_styling_garment_tagging()

    replayed_result = asyncio.run(app_main.tag_styling_garment_with_gemini(garment_id))
    app_main.persist_styling_garment_tagging_result(garment_id, replayed_result)

    assert calls == 1
    assert replayed_result["attempt_id"] == attempt_id
    with get_db() as db:
        garment_row = db.execute("SELECT status FROM styling_garments WHERE id = ?", (garment_id,)).fetchone()
        attempt_row = db.execute("SELECT status FROM generation_attempts WHERE id = ?", (attempt_id,)).fetchone()
    assert garment_row["status"] in {"READY", "TAGGING_READY"}
    assert attempt_row["status"] == "SUCCEEDED"


def test_generation_poller_resumes_due_persisted_tagging_retry(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    garment_id = insert_styling_garment(code="PERSISTED-RETRY", status="TAGGING_RUNNING")
    retry_state = {
        "kind": "transport",
        "transportRetries": 2,
        "retryLimit": 10,
        "retryAt": "2000-01-01T00:00:00Z",
    }
    with get_db() as db:
        db.execute(
            "UPDATE styling_garments SET tag_retry_at = ?, tag_retry_state_json = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", app_main.dumps(retry_state), garment_id),
        )
    resumed: list[int] = []

    async def fake_run(target_id: int) -> None:
        resumed.append(target_id)

    async def scenario() -> None:
        monkeypatch.setattr(app_main, "run_styling_garment_tagging", fake_run)
        app_main.resume_due_styling_garment_tagging_retries()
        await asyncio.sleep(0)

    try:
        asyncio.run(scenario())
        assert resumed == [garment_id]
        with get_db() as db:
            claimed = db.execute("SELECT tag_retry_lease_until FROM styling_garments WHERE id = ?", (garment_id,)).fetchone()
        assert claimed["tag_retry_lease_until"] is not None
        item = client.get(f"/api/styling-garments/{garment_id}").json()["item"]
        assert item["tagRetryAt"] is None
        assert item["tagRetryCount"] == 2
        assert item["tagRetryLimit"] == 10
        assert item["tagRetryKind"] == "transport"
    finally:
        with app_main._active_tagging_jobs_lock:
            app_main._active_styling_garment_tagging_ids.discard(garment_id)


def test_recovery_execution_crash_enters_the_next_process_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    garment_id = insert_styling_garment(code="RECOVERY-CRASH-AGAIN", status="TAGGING_RUNNING")
    state = {
        "kind": "process",
        "processRetries": 1,
        "retryLimit": app_main.settings.gemini_retry_limit,
        "retryAt": "2000-01-01T00:00:00Z",
    }
    with get_db() as db:
        db.execute(
            "UPDATE styling_garments SET tag_retry_at = ?, tag_retry_state_json = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", app_main.dumps(state), garment_id),
        )

    async def crash_without_cleanup(_garment_id: int) -> None:
        return None

    async def simulate_claim_then_process_crash() -> None:
        monkeypatch.setattr(app_main, "run_styling_garment_tagging", crash_without_cleanup)
        app_main.resume_due_styling_garment_tagging_retries()
        await asyncio.sleep(0)

    asyncio.run(simulate_claim_then_process_crash())
    with app_main._active_tagging_jobs_lock:
        app_main._active_styling_garment_tagging_ids.discard(garment_id)
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with get_db() as db:
        claimed = db.execute("SELECT tag_retry_at FROM styling_garments WHERE id = ?", (garment_id,)).fetchone()
        assert claimed["tag_retry_at"] is None
        db.execute("UPDATE styling_garments SET updated_at = ? WHERE id = ?", (stale_time, garment_id))

    app_main.expire_stale_styling_garment_tagging()

    with get_db() as db:
        row = db.execute("SELECT tag_retry_at, tag_retry_state_json FROM styling_garments WHERE id = ?", (garment_id,)).fetchone()
    retry_state = app_main.loads(row["tag_retry_state_json"], {})
    assert row["tag_retry_at"] is not None
    assert retry_state["processRetries"] == 2


def test_json_reask_keeps_one_transport_retry_budget_across_contract_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_counts: list[int] = []

    async def generate(_prompt: str, _images: list[dict] | None, **kwargs: object) -> str:
        initial_count = int(kwargs.get("initial_retry_count") or 0)
        initial_counts.append(initial_count)
        on_retry = kwargs.get("on_retry")
        assert callable(on_retry)
        on_retry({"transportRetries": initial_count + 1})
        return "not-json" if len(initial_counts) == 1 else "{}"

    monkeypatch.setattr(app_main, "generate_gemini_json_text", generate)
    monkeypatch.setattr(app_main, "generation_retry_backoff_seconds", lambda _attempt: 0.0)

    result = asyncio.run(app_main.generate_gemini_json_object_with_reask("return json"))

    assert result == {}
    assert initial_counts == [0, 1]


def test_ai_stylist_keeps_one_transport_retry_budget_across_contract_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_counts: list[int] = []
    normalized = 0

    async def generate(_prompt: str, _images: list[dict], **kwargs: object) -> str:
        initial_count = int(kwargs.get("initial_retry_count") or 0)
        initial_counts.append(initial_count)
        on_retry = kwargs.get("on_retry")
        assert callable(on_retry)
        on_retry({"transportRetries": initial_count + 1})
        return "{}"

    def normalize(_raw: object, _input: object, _candidates: dict) -> dict:
        nonlocal normalized
        normalized += 1
        if normalized == 1:
            raise app_main.AIStylistContractError("missing styling_plan")
        return {"outfit": []}

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(app_main, "is_gemini_configured", lambda: True)
    monkeypatch.setattr(app_main, "generate_gemini_json_text", generate)
    monkeypatch.setattr(app_main, "normalize_ai_stylist_result", normalize)
    monkeypatch.setattr(app_main, "validate_styling_plan", lambda *_args: None)
    monkeypatch.setattr(app_main.asyncio, "sleep", no_wait)

    result = asyncio.run(
        app_main.select_styling_with_gemini(
            {"image_snapshot_json": "[]"},
            {"candidate_material_ids": []},
            {"prompt": "BASE", "systemInstruction": None},
        )
    )

    assert result == {"outfit": []}
    assert initial_counts == [0, 1]
