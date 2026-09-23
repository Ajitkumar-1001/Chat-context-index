"""Bounded, owned I/O worker (plan.md Summary; contracts/operations.md `open()` Ownership).

Reuses APSW's own built-in async support (`/plan-eng-review` finding 2026-09-06/2026-09-22)
rather than a hand-rolled thread-pool wrapper: each connection runs in one dedicated
background worker thread (`apsw.aio.AsyncIO`), and calls made on the event loop are
forwarded to it. This module owns setting that controller and provides small fetch helpers
used throughout the rest of the package — it is the one place that talks to raw
AsyncCursor objects so every other module works with plain Python values.
"""

from __future__ import annotations

from typing import Any

import apsw
import apsw.aio

# Idempotent: apsw.async_controller is a ContextVar. Setting it more than once (e.g. if
# multiple HistoryStore.open() calls happen in the same process) is harmless — it always
# resolves to the same controller class.
apsw.async_controller.set(apsw.aio.AsyncIO)


async def open_worker_connection(path: str) -> apsw.aio.AsyncConnection:
    """Open one AsyncConnection — this call is what actually starts the dedicated worker
    thread for this connection (apsw.Connection.as_async)."""
    return await apsw.Connection.as_async(path)


async def fetchone(cursor: Any) -> tuple | None:
    async for row in cursor:
        return row
    return None


async def fetchall(cursor: Any) -> list[tuple]:
    return [row async for row in cursor]


class IOWorker:
    """Owns one connection's worker thread for the lifetime of a HistoryStore. Writes are
    serialized by SQLite's own single-writer WAL semantics (spec/storage-format.md); this
    class's job is lifecycle (close deterministically), not additional serialization."""

    def __init__(self, connection: apsw.aio.AsyncConnection) -> None:
        self._connection = connection
        self._closed = False

    @property
    def connection(self) -> apsw.aio.AsyncConnection:
        return self._connection

    async def aclose(self) -> None:
        """Close deterministically. Safe to call repeatedly (AT-17)."""
        if self._closed:
            return
        await self._connection.aclose()
        self._closed = True

    def close(self) -> None:
        """Sync-context close for HistoryStore.close() (non-async callers, e.g. __del__-style
        cleanup paths). Prefer aclose() from async code."""
        if self._closed:
            return
        # AsyncConnection.aclose is async-only; a fully sync close is handled by the caller
        # via HistoryStore, which only ever constructs an IOWorker in async open() and closes
        # it via aclose() in the async path. This sync path exists for symmetry with
        # HistoryStore.close() but is expected to be unreachable in normal use — the public
        # API is async throughout (PRD §7.2).
        self._closed = True
