"""Failure injection F1 (commit ambiguity) and F2 (storage failure) for `ingest()`
(spec/fixtures/failure-injection-harness.md; AT-02, AT-04, AT-12).

F1 uses a real child-process crash (`_helpers/crash_ingest.py`) so restart assertions inspect
actual committed on-disk storage, not in-memory state, per the harness's own boundary rule.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

import apsw

from cci.errors import StoreCorrupt, StoreError
from cci.ingest import ingest
from cci.io_worker import fetchall
from cci.models import InputMessage
from cci.store import HistoryStore

_HELPER = os.path.join(os.path.dirname(__file__), "_helpers", "crash_ingest.py")
SOURCE_ID = "crash-src"
IDEMPOTENCY_KEY = "crash-key"


def _run_crash_child(path: str, kill_point: str) -> None:
    result = subprocess.run(
        [sys.executable, _HELPER, path, kill_point], capture_output=True, timeout=30
    )
    assert result.returncode == 17, (
        f"expected the child to hard-exit(17) at its barrier, got {result.returncode}: "
        f"{result.stdout!r} {result.stderr!r}"
    )
    assert b"NO_CRASH" not in result.stdout


def test_f1_terminate_before_commit_then_retry_inserts_once():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f1_before.db")
            # Create the store first so the child resumes an existing file rather than racing
            # its own fresh-init transaction against the crash barrier.
            seed = await HistoryStore.open(path)
            history_id = seed.history_id
            await seed.aclose()

            _run_crash_child(path, "before_commit")

            store = await HistoryStore.open(path)
            cursor = await store.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE history_id = ?", (history_id,)
            )
            (count_before_retry,) = (await fetchall(cursor))[0]
            assert count_before_retry == 0, "before commit: absent"

            receipt = await ingest(
                store, history_id, [InputMessage(role="user", content="crash-test message")],
                SOURCE_ID, IDEMPOTENCY_KEY,
            )
            assert receipt.replayed is False
            assert receipt.inserted_seq_start == 1, "retry inserts once"
            await store.aclose()

    asyncio.run(scenario())


def test_f1_terminate_after_commit_then_retry_replays():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f1_after.db")
            seed = await HistoryStore.open(path)
            history_id = seed.history_id
            await seed.aclose()

            _run_crash_child(path, "after_commit")

            store = await HistoryStore.open(path)
            cursor = await store.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE history_id = ?", (history_id,)
            )
            (count_before_retry,) = (await fetchall(cursor))[0]
            assert count_before_retry == 1, "after commit: complete"

            receipt = await ingest(
                store, history_id, [InputMessage(role="user", content="crash-test message")],
                SOURCE_ID, IDEMPOTENCY_KEY,
            )
            assert receipt.replayed is True, "retry replays its receipt"

            cursor = await store.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE history_id = ?", (history_id,)
            )
            (count_after_retry,) = (await fetchall(cursor))[0]
            assert count_after_retry == 1, "replay does not duplicate the committed message"
            await store.aclose()

    asyncio.run(scenario())


class _FailingConnectionProxy:
    """Wraps a real AsyncConnection, injecting a fault into `execute()` on demand.
    `apsw.Connection`'s attributes are read-only from Python, so the fault can't be
    monkeypatched onto the real connection object directly — this proxy is substituted for
    `store._connection` instead, for the duration of one test."""

    def __init__(self, real, should_fail):
        self._real = real
        self._should_fail = should_fail
        self._call_count = 0

    async def execute(self, sql, *args, **kwargs):
        self._call_count += 1
        if self._should_fail(sql, self._call_count):
            raise apsw.FullError("simulated: database or disk is full")
        return await self._real.execute(sql, *args, **kwargs)

    async def __aenter__(self):
        return await self._real.__aenter__()

    async def __aexit__(self, *exc_info):
        return await self._real.__aexit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_f2_storage_write_failure_raises_typed_error_no_partial_commit():
    """Fail a history write during a bounded ingestion batch (simulated full-storage
    condition: an injected fault, documented as such since a real full-disk state isn't
    reproducible in this environment)."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f2_write_fail.db")
            store = await HistoryStore.open(path)
            history_id = store.history_id

            real_connection = store._connection

            def should_fail(sql: str, call_count: int) -> bool:
                return "INSERT INTO messages" in sql and call_count > 3

            store._connection = _FailingConnectionProxy(real_connection, should_fail)
            try:
                try:
                    await ingest(
                        store, history_id,
                        [InputMessage(role="user", content=f"m{i}") for i in range(3)],
                        "f2-src", "f2-key",
                    )
                    raise AssertionError("expected a typed storage error")
                except StoreError:
                    pass
            finally:
                store._connection = real_connection

            cursor = await store.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE history_id = ?", (history_id,)
            )
            (count,) = (await fetchall(cursor))[0]
            assert count == 0, "a rejected write must not leave a partial batch committed"

            cursor = await store.connection.execute(
                "SELECT COUNT(*) FROM ingest_receipts WHERE history_id = ?", (history_id,)
            )
            (receipt_count,) = (await fetchall(cursor))[0]
            assert receipt_count == 0, "no fake/successful receipt on a failed write"

            await store.aclose()

    asyncio.run(scenario())


def test_f2_corrupted_store_raises_typed_error_not_fake_empty_history():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f2_corrupt.db")
            with open(path, "wb") as f:
                f.write(b"not a sqlite database, deliberately corrupted for F2" * 20)

            try:
                await HistoryStore.open(path)
                raise AssertionError("expected StoreCorrupt, not a fake empty history")
            except StoreCorrupt:
                pass

    asyncio.run(scenario())


if __name__ == "__main__":
    test_f1_terminate_before_commit_then_retry_inserts_once()
    test_f1_terminate_after_commit_then_retry_replays()
    test_f2_storage_write_failure_raises_typed_error_no_partial_commit()
    test_f2_corrupted_store_raises_typed_error_not_fake_empty_history()
    print("F1/F2 failure-injection checks passed")
