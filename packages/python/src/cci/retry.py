"""Shared retry policy (contracts/result-schemas.md Retryable column; PRD §10).

Only transient errors (StoreBusy, ProviderTimeout) retry, with bounded backoff and jitter.
Every other code is permanent and never retried. All waiting (queue, semaphore, cache,
retry, database) consumes the same request deadline — retry() takes that deadline and stops
retrying once it's exhausted, raising the last error rather than starting a new attempt.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, TypeVar

from .errors import CciError, ProviderTimeout, StoreBusy

T = TypeVar("T")

# Codes this policy will retry. Kept as an explicit tuple (not `err.retryable`) so a future
# error class added to errors.py without updating this list fails loudly (unexpected code
# falls through to "no retry"), rather than silently becoming retryable.
_TRANSIENT: tuple[type[CciError], ...] = (StoreBusy, ProviderTimeout)

_BASE_BACKOFF_S = 0.05
_MAX_BACKOFF_S = 2.0
_MAX_ATTEMPTS = 3  # contracts/result-schemas.md: "Provider attempts per logical operation: <= 3"


def retry(
    fn: Callable[[], T],
    *,
    deadline_at: float,
    max_attempts: int = _MAX_ATTEMPTS,
    _sleep: Callable[[float], None] = time.sleep,
    _monotonic: Callable[[], float] = time.monotonic,
    _rand: Callable[[], float] = random.random,
) -> T:
    """Call `fn()`, retrying on transient errors with bounded backoff+jitter, until either
    it succeeds, a non-transient error is raised (propagated immediately, no retry), the
    deadline (`_monotonic()` timestamp) passes, or `max_attempts` is reached."""
    attempt = 0
    last_error: CciError | None = None
    while True:
        attempt += 1
        if _monotonic() >= deadline_at:
            if last_error is not None:
                raise last_error
            raise StoreBusy("deadline already passed before first attempt")
        try:
            return fn()
        except _TRANSIENT as err:
            last_error = err
            if attempt >= max_attempts:
                raise
            backoff = min(_MAX_BACKOFF_S, _BASE_BACKOFF_S * (2 ** (attempt - 1)))
            jittered = backoff * (0.5 + _rand() * 0.5)
            remaining = deadline_at - _monotonic()
            if remaining <= 0:
                raise
            _sleep(min(jittered, remaining))


async def async_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    deadline_at: float,
    max_attempts: int = _MAX_ATTEMPTS,
    deadline_exceeded_error: type[CciError] = ProviderTimeout,
    _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    _monotonic: Callable[[], float] = time.monotonic,
    _rand: Callable[[], float] = random.random,
) -> T:
    """Async counterpart of `retry()` — same policy (only StoreBusy/ProviderTimeout retry,
    bounded backoff+jitter, shared deadline), for provider calls made from async code
    (MemoizedProvider). `deadline_exceeded_error` (default `ProviderTimeout`, this function's
    only current caller) is raised only when the deadline was already passed before `fn` ever
    ran once — `retry()`'s storage-oriented `StoreBusy` default would be the wrong code here."""
    attempt = 0
    last_error: CciError | None = None
    while True:
        attempt += 1
        if _monotonic() >= deadline_at:
            if last_error is not None:
                raise last_error
            raise deadline_exceeded_error("deadline already passed before first attempt")
        try:
            return await fn()
        except _TRANSIENT as err:
            last_error = err
            if attempt >= max_attempts:
                raise
            backoff = min(_MAX_BACKOFF_S, _BASE_BACKOFF_S * (2 ** (attempt - 1)))
            jittered = backoff * (0.5 + _rand() * 0.5)
            remaining = deadline_at - _monotonic()
            if remaining <= 0:
                raise
            await _sleep(min(jittered, remaining))
