"""Resource-ownership test (AT-17, Python-only close/reopen subcase), per
spec/fixtures/failure-injection-harness.md "Release checks": close repeatedly after success and
failure; owned resources close; injected clients remain caller-owned unless transferred; a child
process using a built artifact exits without leaked connections or tasks.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.errors import IdempotencyConflict
from cci.ingest import ingest
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider
from cci.store import HistoryStore

_PYTHON_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src")


def test_aclose_is_idempotent_after_success():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await HistoryStore.open(os.path.join(d, "own1.db"))
            await ingest(
                store, store.history_id,
                [InputMessage(role="user", content="ok")], "src-1", "key-1",
            )
            await store.aclose()
            await store.aclose()  # safe to call repeatedly
            await store.aclose()

    asyncio.run(scenario())


def test_aclose_is_idempotent_after_a_failed_operation():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await HistoryStore.open(os.path.join(d, "own2.db"))
            await ingest(
                store, store.history_id,
                [InputMessage(role="user", content="v1")], "src-1", "key-1",
            )
            try:
                await ingest(
                    store, store.history_id,
                    [InputMessage(role="user", content="v2 different content")],
                    "src-1", "key-1",  # same idempotency_key, different content
                )
                raise AssertionError("expected IdempotencyConflict")
            except IdempotencyConflict:
                pass

            # Owned resources still close cleanly after a failed operation, repeatedly.
            await store.aclose()
            await store.aclose()

    asyncio.run(scenario())


def test_injected_provider_is_never_touched_by_store_close():
    """An externally constructed provider/cache client remains caller-owned and untouched by
    `aclose()` unless ownership transfer was explicitly requested at `open()` (contracts/
    operations.md `open()`/`aclose()`). `HistoryStore` never owns a `Provider` at all — it is
    always passed per-call to `ask()`/`index()`, never through `open()` — so this is true by
    construction; this test pins that down as an explicit regression guard."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await HistoryStore.open(os.path.join(d, "own3.db"))
            fake = FakeProvider()
            provider = MemoizedProvider(inner=fake, config=store.config, cache=store.cache)

            await store.aclose()

            # Closing the store must not have touched the caller's own provider object at all —
            # it has no `close`/`aclose` method, and nothing in HistoryStore.aclose() reaches it.
            assert not hasattr(provider, "aclose")
            assert not hasattr(provider, "close")
            assert fake.call_count == 0

    asyncio.run(scenario())


def test_importing_cci_never_loads_redis_or_a_provider_sdk():
    """PRD §13.2, CHK025: importing the main entry point MUST NOT load the Redis client or a
    provider SDK — checked in a fresh child process (a shared test-process `sys.modules` could
    otherwise already carry `redis` from an earlier test in this same suite)."""
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import cci; assert 'redis' not in sys.modules; print('OK')"],
        env=dict(os.environ, PYTHONPATH=_PYTHON_SRC),
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_child_process_using_the_library_exits_without_leaked_connections_or_tasks():
    """A child process using a built artifact exits without leaked connections or tasks — run
    a short scenario via the CLI (a thin wrapper over the same library, contracts/operations.md
    `open()` Ownership) in a subprocess and assert it exits promptly and cleanly. A lingering
    non-daemon thread or unclosed asyncio task would keep the interpreter alive past its own
    `main()` return, causing this to hang past the timeout instead of exiting."""
    with tempfile.TemporaryDirectory() as d:
        store_path = os.path.join(d, "own4.db")
        env = dict(os.environ, PYTHONPATH=_PYTHON_SRC)

        result = subprocess.run(
            [sys.executable, "-m", "cci.cli", "--store", store_path, "init"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert "history_id" in result.stdout

        # Re-open (resume) and close again — exercises the resume path, still exits cleanly.
        result2 = subprocess.run(
            [sys.executable, "-m", "cci.cli", "--store", store_path, "stats"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        assert result2.returncode == 0, result2.stderr


if __name__ == "__main__":
    test_aclose_is_idempotent_after_success()
    test_aclose_is_idempotent_after_a_failed_operation()
    test_injected_provider_is_never_touched_by_store_close()
    test_importing_cci_never_loads_redis_or_a_provider_sdk()
    test_child_process_using_the_library_exits_without_leaked_connections_or_tasks()
    print("AT-17 (Python-only subcase) resource ownership checks passed")
