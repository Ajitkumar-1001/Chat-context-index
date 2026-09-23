"""Failure injection F11 (sequence after clear) and F10 (local-work half, and — since T062 —
the full case with a real disposable Redis), per spec/fixtures/failure-injection-harness.md
(AT-18).

F10's local-work-half case pauses a *reader* (data-model.md Snapshot/Generation lifecycle case
2 — `retrieve()`/`ask()` at read emission); the writer case (F-writer/T087) is out of scope for
this batch and lives in T087. `index()` tree-publish (case 1) doesn't exist until T053, so it
isn't a candidate reader/writer case available in this milestone either.

The full F10 case (T063, US6b/M3) additionally makes Redis unreachable during the clear's
external purge attempt: `logical_clear_complete=true` and `cache_purge_pending=true`, a durable
pending scope permits later bounded cleanup, and unrelated Redis scopes survive — verified
against a real disposable Redis container (harness boundaries: a mock cannot prove Redis
reconnection/command behavior).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

import cci.retrieve as retrieve_module
from cci.clear import clear_history
from cci.errors import VersionConflict
from cci.index import index
from cci.io_worker import fetchall
from cci.ingest import ingest
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderResponse
from cci.retrieve import retrieve
from cci.store import HistoryStore

_CONTAINER_NAME = "cci-test-redis-f10"
REDIS_URL: str | None = None


_FIXED_PORT = 16739  # fixed, not dynamic (0:6379): a `docker start` after `docker stop`
# reassigns a NEW ephemeral host port for a `-p 0:...` mapping, which would silently strand
# this module's cached REDIS_URL/store.cache._client after this test's stop/start cycle.


def setup_module(module) -> None:
    global REDIS_URL
    subprocess.run(["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True)
    # No --rm here (unlike test_failure_cache.py's container): this test stops/starts the
    # container mid-test to simulate Redis becoming unreachable and later reconnecting — --rm
    # would auto-remove it on `docker stop`, breaking the restart. teardown_module removes it
    # explicitly instead.
    subprocess.run(
        [
            "docker", "run", "-d", "-p", f"{_FIXED_PORT}:6379", "--name", _CONTAINER_NAME,
            "redis:7-alpine",
        ],
        check=True, capture_output=True,
    )
    REDIS_URL = f"redis://localhost:{_FIXED_PORT}"

    import redis as redis_sync

    for _ in range(50):
        try:
            redis_sync.from_url(REDIS_URL).ping()
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("redis container did not become ready in time")


def teardown_module(module) -> None:
    subprocess.run(["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True)


def test_f11_sequence_after_clear_exceeds_old_high_water_mark():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f11.db")
            store = await HistoryStore.open(path)
            history_id = store.history_id

            messages = [InputMessage(role="user", content=f"m{i}") for i in range(4)]
            first = await ingest(store, history_id, messages, "src-1", "key-1")
            highest_before_clear = first.inserted_seq_end
            assert highest_before_clear == 4

            await clear_history(store, history_id)

            cursor = await store.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE history_id = ?", (history_id,)
            )
            (count_after_clear,) = (await fetchall(cursor))[0]
            assert count_after_clear == 0, "old raw content cannot be retrieved after clear"

            cursor = await store.connection.execute(
                "SELECT COUNT(*) FROM message_fts"
            )
            (fts_count_after_clear,) = (await fetchall(cursor))[0]
            assert fts_count_after_clear == 0, "old FTS content cannot be retrieved after clear"

            second = await ingest(
                store, history_id, [InputMessage(role="user", content="post-clear")],
                "src-1", "key-2",
            )
            assert second.inserted_seq_start > highest_before_clear, (
                "new sequence values exceed the old high-water mark"
            )

            await store.aclose()

    asyncio.run(scenario())


def test_f10_reader_paused_mid_request_cannot_publish_retired_content():
    """F10 local-work half, reader case (data-model.md Snapshot/Generation lifecycle case 2):
    pause a `retrieve()` request between Snapshot capture and emission, request `clear_history`
    concurrently, allow quiescence to settle, then resume the paused read. The logical clear
    gates new work and rotates generation atomically; the paused reader cannot publish/return
    retired content — it must fail `VersionConflict` at emission rather than return a
    pre-clear result."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f10.db")
            store = await HistoryStore.open(path)
            history_id = store.history_id
            await ingest(
                store, history_id, [InputMessage(role="user", content="pre-clear marker")],
                "src-1", "key-1",
            )

            resume_reader = asyncio.Event()
            reader_paused = asyncio.Event()

            async def pausing_barrier() -> None:
                reader_paused.set()
                await resume_reader.wait()

            retrieve_module._mid_retrieve_barrier = pausing_barrier
            try:
                reader_task = asyncio.create_task(retrieve(store, "marker"))
                await reader_paused.wait()

                # The reader has captured its Snapshot and released write_lock (it's now
                # parked in the barrier) — a clear can gate/quiesce/commit concurrently.
                clear_report = await clear_history(store, history_id)
                assert clear_report.logical_clear_complete is True

                resume_reader.set()
                try:
                    await reader_task
                    raise AssertionError(
                        "expected VersionConflict — the paused reader must not publish "
                        "retired content"
                    )
                except VersionConflict:
                    pass
            finally:
                async def _noop() -> None:
                    return None

                retrieve_module._mid_retrieve_barrier = _noop

            # A new reader after the clear sees the post-clear generation and completes.
            resumed = await retrieve(store, "marker")
            assert resumed.status == "empty", "pre-clear content is gone after the clear"

            await store.aclose()

    asyncio.run(scenario())


def test_f10_full_case_redis_unreachable_reports_pending_purge_and_preserves_unrelated_scopes():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f10-full.db")
            store = await HistoryStore.open(
                path,
                config={
                    "cache_backend": "redis",
                    "application_namespace": "ns-f10-full",
                    "redis_url": REDIS_URL,
                },
            )
            history_id = store.history_id
            await store.cache._client.flushdb()  # isolate from any other test sharing this port

            await ingest(
                store, history_id, [InputMessage(role="user", content="pre-clear marker")],
                "src-1", "key-1",
            )
            fake = FakeProvider(responses=[ProviderResponse(text='{"title": "T", "summary": "S"}')])
            provider = MemoizedProvider(inner=fake, config=store.config, cache=store.cache)
            await index(store, provider=provider, rebuild=True)  # populates a real memo entry

            # An unrelated scope's entry, as if from a different history/application sharing
            # this Redis instance — must survive this clear untouched.
            await store.cache._client.set("cci:memo:v2:unrelated-scope:req1", "unrelated-payload")

            # Combine with the local-work-half pattern: pause a reader during the clear.
            resume_reader = asyncio.Event()
            reader_paused = asyncio.Event()

            async def pausing_barrier() -> None:
                reader_paused.set()
                await resume_reader.wait()

            retrieve_module._mid_retrieve_barrier = pausing_barrier
            try:
                reader_task = asyncio.create_task(retrieve(store, "marker"))
                await reader_paused.wait()

                # Make Redis unreachable for the duration of the clear's purge attempt.
                subprocess.run(["docker", "stop", _CONTAINER_NAME], capture_output=True)
                try:
                    clear_report = await clear_history(store, history_id)
                finally:
                    subprocess.run(["docker", "start", _CONTAINER_NAME], capture_output=True)
                    for _ in range(50):
                        try:
                            await store.cache._client.ping()
                            break
                        except Exception:
                            await asyncio.sleep(0.1)

                assert clear_report.logical_clear_complete is True
                assert clear_report.cache_purge_pending is True, (
                    "Redis unreachable: the purge attempt must not be reported as completed"
                )

                resume_reader.set()
                try:
                    await reader_task
                    raise AssertionError("expected VersionConflict")
                except VersionConflict:
                    pass
            finally:
                async def _noop() -> None:
                    return None

                retrieve_module._mid_retrieve_barrier = _noop

            # A durable pending scope permits later bounded cleanup.
            cursor = await store.connection.execute("SELECT scope_id FROM pending_cache_purges")
            rows = await fetchall(cursor)
            assert len(rows) == 1, "the retired scope must be durably recorded for later cleanup"

            # Unrelated Redis scopes survive — the purge never touched them (it never even
            # succeeded), and no blanket FLUSHDB/FLUSHALL-style deletion ever runs.
            survived = await store.cache._client.get("cci:memo:v2:unrelated-scope:req1")
            assert survived == b"unrelated-payload"

            # Inspect purge commands: a SCAN was attempted, scoped to the retired scope's own
            # prefix, before failing — never an unscoped KEYS */FLUSHDB/FLUSHALL.
            scan_commands = [c for c in store.cache.commands if c[0] == "SCAN"]
            assert scan_commands, "a bounded SCAN attempt must have been recorded"
            assert all(rows[0][0] in c[2] for c in scan_commands), "SCAN must be scoped to the retired scope's prefix"
            assert not any(c[0] in ("FLUSHDB", "FLUSHALL") for c in store.cache.commands)

            await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_f11_sequence_after_clear_exceeds_old_high_water_mark()
    test_f10_reader_paused_mid_request_cannot_publish_retired_content()
    setup_module(None)
    try:
        test_f10_full_case_redis_unreachable_reports_pending_purge_and_preserves_unrelated_scopes()
    finally:
        teardown_module(None)
    print("F10/F11 failure-injection checks passed")
