"""Failure injection F11 (sequence after clear), F10 (local-work half, and — since T062 — the
full case with a real disposable Redis), and F-writer/T087 (in-flight `ingest()` during clear's
quiescence window), per spec/fixtures/failure-injection-harness.md (AT-18).

F10's local-work-half case pauses a *reader* (data-model.md Snapshot/Generation lifecycle case
2 — `retrieve()`/`ask()` at read emission); `index()` tree-publish (case 1) is exercised
separately by T055's F4 test, not here.

The full F10 case (T063, US6b/M3) additionally makes Redis unreachable during the clear's
external purge attempt: `logical_clear_complete=true` and `cache_purge_pending=true`, a durable
pending scope permits later bounded cleanup, and unrelated Redis scopes survive — verified
against a real disposable Redis container (harness boundaries: a mock cannot prove Redis
reconnection/command behavior).

F-writer/T087 pauses a *writer* (`ingest()`, at its commit barrier) instead of a reader —
data-model.md Snapshot/Generation lifecycle case 3, resolving `/speckit-analyze` finding G2.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

import cci.ingest as ingest_module
import cci.retrieve as retrieve_module
from cci.clear import clear_history
from cci.errors import BudgetExceeded, VersionConflict
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


def test_f_writer_ingest_commits_within_window_is_swept_away_with_the_generation():
    """T087 case (a): the in-flight `ingest()` commits within the 10s window — its receipt is
    honest (a normal, successful commit) and its data is swept away with the rest of the
    pre-clear generation once the clear commits."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fwriter-a.db")
            store = await HistoryStore.open(path)
            history_id = store.history_id

            paused = asyncio.Event()
            resume = asyncio.Event()

            async def pausing_barrier() -> None:
                paused.set()
                await resume.wait()

            ingest_module._pause_before_commit_barrier = pausing_barrier
            try:
                ingest_task = asyncio.create_task(
                    ingest(
                        store, history_id,
                        [InputMessage(role="user", content="in-flight during clear")],
                        "src-1", "key-1",
                    )
                )
                await paused.wait()

                # The paused ingest already passed clear's gate and is counted in-flight —
                # clear_history() must wait for it, not reject or race past it.
                clear_task = asyncio.create_task(clear_history(store, history_id))
                resume.set()

                receipt = await ingest_task
                clear_report = await clear_task

                assert receipt.replayed is False
                assert receipt.inserted_seq_start == 1, "the receipt is honest about what it committed"
                assert clear_report.logical_clear_complete is True

                messages = await store.get_messages(1, 100)
                assert len(messages) == 0, (
                    "the in-flight write's data is swept away with the rest of the pre-clear "
                    "generation once the clear commits"
                )
            finally:
                async def _noop() -> None:
                    return None

                ingest_module._pause_before_commit_barrier = _noop

            await store.aclose()

    asyncio.run(scenario())


def test_f_writer_window_expires_clear_fails_and_ingest_continues_unaffected():
    """T087 case (b): the quiescence window expires before the in-flight `ingest()` commits —
    `clear_history()` fails explicitly with `BudgetExceeded`, and the `ingest()` is never
    cancelled — it continues unaffected and its commit still succeeds once resumed."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fwriter-b.db")
            store = await HistoryStore.open(
                path, config={"clear_history_quiescence_deadline_s": 0.2},
            )
            history_id = store.history_id

            paused = asyncio.Event()
            resume = asyncio.Event()

            async def pausing_barrier() -> None:
                paused.set()
                await resume.wait()

            ingest_module._pause_before_commit_barrier = pausing_barrier
            try:
                ingest_task = asyncio.create_task(
                    ingest(
                        store, history_id,
                        [InputMessage(role="user", content="still in flight when clear times out")],
                        "src-1", "key-1",
                    )
                )
                await paused.wait()

                try:
                    await clear_history(store, history_id)
                    raise AssertionError("expected BudgetExceeded — the window must expire first")
                except BudgetExceeded:
                    pass

                # The clear's failure must not cancel or otherwise affect the still-running write.
                resume.set()
                receipt = await ingest_task
                assert receipt.replayed is False
                assert receipt.inserted_seq_start == 1, "the ingest() continues unaffected and still commits"

                messages = await store.get_messages(1, 100)
                assert len(messages) == 1, "the write that outlived the failed clear is still present"
            finally:
                async def _noop() -> None:
                    return None

                ingest_module._pause_before_commit_barrier = _noop

            await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_f11_sequence_after_clear_exceeds_old_high_water_mark()
    test_f10_reader_paused_mid_request_cannot_publish_retired_content()
    test_f_writer_ingest_commits_within_window_is_swept_away_with_the_generation()
    test_f_writer_window_expires_clear_fails_and_ingest_continues_unaffected()
    setup_module(None)
    try:
        test_f10_full_case_redis_unreachable_reports_pending_purge_and_preserves_unrelated_scopes()
    finally:
        teardown_module(None)
    print("F10/F11 failure-injection checks passed")
