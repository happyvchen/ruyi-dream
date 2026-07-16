from __future__ import annotations

from contextlib import contextmanager
import threading
from typing import Iterator

from . import config


_lock = threading.Lock()
_limit: int | None = None
_semaphore: threading.BoundedSemaphore | None = None


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
    semaphore = _current_semaphore()
    semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()
