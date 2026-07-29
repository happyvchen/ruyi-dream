from __future__ import annotations

from contextlib import contextmanager
import contextvars
import threading
from typing import Iterator

from . import config


_lock = threading.Lock()
_limit: int | None = None
_semaphore: threading.BoundedSemaphore | None = None
_slot_depth: contextvars.ContextVar[int] = contextvars.ContextVar("toapis_concurrency_slot_depth", default=0)


def _current_semaphore() -> threading.BoundedSemaphore:
    global _limit, _semaphore
    limit = max(1, config.get_settings().toapis_max_concurrency)
    with _lock:
        if _semaphore is None or _limit != limit:
            _limit = limit
            _semaphore = threading.BoundedSemaphore(limit)
        return _semaphore


@contextmanager
def toapis_concurrency_slot() -> Iterator[None]:
    if _slot_depth.get() > 0:
        yield
        return
    semaphore = _current_semaphore()
    semaphore.acquire()
    token = _slot_depth.set(_slot_depth.get() + 1)
    try:
        yield
    finally:
        _slot_depth.reset(token)
        semaphore.release()
