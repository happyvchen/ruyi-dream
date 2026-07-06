"""生成 attempt 的阶段进度与失败分类。

设计要点（对应 shoot_script_workflow.md 状态机约束）：

- 阶段进度只是 attempt 上的展示元数据，不是新的业务状态。
  `generation_result_images_new.status` / `generation_attempts.status`
  仍然只有 QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED。
- 失败必须分类。"provider 生成失败"（重试要重新消耗额度）、
  "成片回传失败"（provider 已成功，结果可零成本重新拉取）和
  "provider 超时" 是完全不同的业务事实，前端据此提供不同的重试动作。
- 所有写入走独立的 sqlite 连接，可以从 asyncio 线程池 worker 中安全调用。
"""
from __future__ import annotations

import threading
from typing import Any

from .db import dumps, get_db, loads, now_iso

# 阶段常量：按业务顺序排列。
PHASE_PREPARE = "prepare"
PHASE_UPLOAD = "upload"
PHASE_QUEUE = "queue"
PHASE_GENERATE = "generate"
PHASE_DELIVER = "deliver"

PHASE_ORDER: tuple[str, ...] = (PHASE_PREPARE, PHASE_UPLOAD, PHASE_QUEUE, PHASE_GENERATE, PHASE_DELIVER)

# 失败分类常量，落到 generation_attempts.error_code。
ERROR_UPLOAD_FAILED = "UPLOAD_FAILED"
ERROR_PROVIDER_FAILED = "PROVIDER_FAILED"
ERROR_PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
ERROR_DELIVERY_FAILED = "DELIVERY_FAILED"


class GenerationPhaseError(RuntimeError):
    """带失败分类和 provider 快照的生成错误。

    error_code 取上面的 ERROR_* 常量；provider_response 保留 providerRequestId
    等现场，保证回传失败后仍可零成本重新拉取结果。
    """

    def __init__(self, message: str, error_code: str, provider_response: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.provider_response = provider_response or {}


def classify_attempt_error(exc: BaseException) -> str | None:
    """从异常上提取失败分类；没有分类信息时返回 None（保持既有行为）。"""
    code = getattr(exc, "error_code", None)
    return str(code) if code else None


class AttemptProgressRecorder:
    """把阶段进度持久化到 generation_attempts.progress_json。

    progress_json 结构::

        {
          "current": "deliver",           # 当前阶段；结束后为 None
          "phases": [
            {"phase": "upload", "startedAt": "...", "endedAt": "...",
             "detail": {"completed": 2, "total": 4}},
            ...
          ]
        }

    attempt 被后台恢复（进程重启后 resume）时会先读回已有进度，
    已结束的阶段不会被覆盖。
    """

    def __init__(self, attempt_id: int | None):
        self._attempt_id = attempt_id
        self._lock = threading.Lock()
        self._progress: dict[str, Any] = {"current": None, "phases": []}
        if attempt_id:
            with get_db() as db:
                row = db.execute(
                    "SELECT progress_json FROM generation_attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
            existing = loads(row["progress_json"], None) if row else None
            if isinstance(existing, dict) and isinstance(existing.get("phases"), list):
                self._progress = existing

    @property
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"current": self._progress.get("current"), "phases": list(self._progress.get("phases") or [])}

    def begin(self, phase: str, **detail: Any) -> None:
        """进入一个阶段；自动结束上一个未结束阶段。幂等：重复 begin 同一阶段只更新 detail。"""
        timestamp = now_iso()
        with self._lock:
            entry = self._find(phase)
            if entry is not None and not entry.get("endedAt"):
                if detail:
                    entry["detail"] = {**(entry.get("detail") or {}), **detail}
            else:
                self._close_open_phases(timestamp)
                self._progress["phases"].append(
                    {"phase": phase, "startedAt": timestamp, "endedAt": None, "detail": detail or {}}
                )
            self._progress["current"] = phase
            self._persist()

    def update(self, phase: str, **detail: Any) -> None:
        """更新当前阶段 detail（上传张数、重试次数等）；阶段不存在时等价于 begin。"""
        with self._lock:
            entry = self._find(phase)
            if entry is None or entry.get("endedAt"):
                self._lock_free_begin(phase, detail)
            else:
                entry["detail"] = {**(entry.get("detail") or {}), **detail}
                self._progress["current"] = phase
            self._persist()

    def finish(self) -> None:
        """全部完成：关闭所有未结束阶段，current 置空。"""
        timestamp = now_iso()
        with self._lock:
            self._close_open_phases(timestamp)
            self._progress["current"] = None
            self._persist()

    # -- internal ----------------------------------------------------------

    def _lock_free_begin(self, phase: str, detail: dict[str, Any]) -> None:
        timestamp = now_iso()
        self._close_open_phases(timestamp)
        self._progress["phases"].append(
            {"phase": phase, "startedAt": timestamp, "endedAt": None, "detail": detail or {}}
        )
        self._progress["current"] = phase

    def _find(self, phase: str) -> dict[str, Any] | None:
        for entry in reversed(self._progress.get("phases") or []):
            if entry.get("phase") == phase:
                return entry
        return None

    def _close_open_phases(self, timestamp: str) -> None:
        for entry in self._progress.get("phases") or []:
            if not entry.get("endedAt"):
                entry["endedAt"] = timestamp

    def _persist(self) -> None:
        if not self._attempt_id:
            return
        with get_db() as db:
            db.execute(
                "UPDATE generation_attempts SET progress_json = ?, updated_at = ? WHERE id = ?",
                (dumps(self._progress), now_iso(), self._attempt_id),
            )
