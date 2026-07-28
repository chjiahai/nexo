"""Transient-error classification + bounded retry.

Network I/O (WeCom download, remote upload) is single-shot by default; a flaky
network yields a user-visible failure with no second chance. This module gives
callers a way to mark a failure as *transient* (likely to succeed on retry) and
a `retry` helper that re-issues the operation with exponential backoff.

Only `TransientError` is retried — a permanent failure (4xx, missing config) is
propagated immediately so it can't burn the retry budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class TransientError(Exception):
    """A failure likely to succeed on retry (network blip, 5xx, timeout)."""


async def retry(
    factory: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Retry a transient operation with exponential backoff.

    `factory` returns a *fresh* coroutine each attempt (so a retry actually
    re-issues the request, not resumes a completed one). Only `TransientError`
    is retried; any other exception propagates immediately. Backoff is
    `base_delay * 2**attempt` (0.5s, 1s, ...).
    """
    last: TransientError | None = None
    for attempt in range(attempts):
        try:
            return await factory()
        except TransientError as exc:
            last = exc
            if attempt + 1 >= attempts:
                break
            await asyncio.sleep(base_delay * (2 ** attempt))
    assert last is not None  # the loop only exits here after a TransientError
    raise last
