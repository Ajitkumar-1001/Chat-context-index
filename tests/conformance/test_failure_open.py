"""Failure injection F3 / AT-17 — rejected opens and resource cleanup.

A newer-unsupported schema and a rejected runtime/FTS5 configuration are tested separately.
Each case inspects the disposable store before and after to verify no history/schema mutation
or silent reset happens on a rejected open.

The runtime/FTS5 case is SIMULATED at the `_check_runtime` boundary (a real incompatible SQLite
build is not available in this environment — the installed APSW's amalgamation genuinely
supports FTS5 and meets MIN_SQLITE_VERSION). This proves `HistoryStore.open()` correctly aborts
without mutation when that check fails; it does NOT prove `_check_runtime` itself detects every
real incompatible build. A real-build run is NOT RUN and remains open work.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

import apsw
import pytest

from cci import store as store_module
from cci.errors import RuntimeCompatibilityError, SchemaVersionError
from cci.io_worker import open_worker_connection
from cci.store import HistoryStore


def _read_store_meta_row(path: str) -> tuple:
    conn = apsw.Connection(path)
    try:
        row = next(conn.execute("SELECT history_id, store_instance_id, schema_version FROM store_meta WHERE id = 1"))
        return row
    finally:
        conn.close()


def test_newer_schema_version_rejected_without_mutation():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "newer_schema.db")

            # Seed a store with a schema_version newer than this build supports.
            conn = await open_worker_connection(path)
            async with conn:
                await conn.execute(store_module._SCHEMA_DDL)
                await conn.execute(
                    "INSERT INTO store_meta (id, history_id, store_instance_id, schema_version) "
                    "VALUES (1, ?, ?, ?)",
                    ("t_seed", "si_seed", store_module.SCHEMA_VERSION + 1),
                )
            await conn.aclose()

            before = _read_store_meta_row(path)
            assert before == ("t_seed", "si_seed", store_module.SCHEMA_VERSION + 1)

            try:
                await HistoryStore.open(path)
                raise AssertionError("expected SchemaVersionError")
            except SchemaVersionError:
                pass

            after = _read_store_meta_row(path)
            assert after == before, "rejected open must not mutate store_meta"

    asyncio.run(scenario())


def test_rejected_runtime_fts5_configuration_without_mutation():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bad_runtime.db")
            assert not os.path.exists(path)

            original_check_runtime = store_module._check_runtime

            async def failing_check_runtime(connection):
                raise RuntimeCompatibilityError("simulated: loaded SQLite build lacks FTS5 support")

            store_module._check_runtime = failing_check_runtime
            try:
                try:
                    await HistoryStore.open(path)
                    raise AssertionError("expected RuntimeCompatibilityError")
                except RuntimeCompatibilityError:
                    pass
            finally:
                store_module._check_runtime = original_check_runtime

            # A rejected runtime check must not leave behind a schema-initialized store
            # (silent reset would show up as a populated store_meta row here).
            assert os.path.exists(path), "APSW creates the file on connect, per SQLite semantics"
            conn = apsw.Connection(path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='store_meta'"
                    )
                }
            finally:
                conn.close()
            assert tables == set(), "no schema/history mutation on a rejected runtime check"

    asyncio.run(scenario())


@pytest.mark.parametrize("existing_history", [False, True], ids=["fresh", "existing"])
def test_malformed_redis_url_closes_connection_and_worker(tmp_path, existing_history):
    """AT-17: cache construction must not leak resources when opening or resuming a store."""
    pytest.importorskip("redis.asyncio")

    async def scenario() -> None:
        path = str(tmp_path / "bad_redis.db")
        expected_meta = None
        if existing_history:
            async with await HistoryStore.open(path, config={"cache_backend": "none"}):
                pass
            expected_meta = _read_store_meta_row(path)

        connections_before = set(apsw.connections())
        threads_before = set(threading.enumerate())
        try:
            with pytest.raises(ValueError, match="Redis URL must specify"):
                await HistoryStore.open(path, config={
                    "cache_backend": "redis",
                    "application_namespace": "ns",
                    "redis_url": "::: not a valid redis url :::",
                })

            workers = set(threading.enumerate()) - threads_before
            for worker in workers:
                worker.join(timeout=2)
            assert not set(apsw.connections()) - connections_before, "failed open leaked SQLite"
            assert not any(worker.is_alive() for worker in workers), "failed open leaked a worker"
        finally:
            # Keep a failing regression from leaving APSW's non-daemon worker in the test process.
            for connection in set(apsw.connections()) - connections_before:
                await connection.aclose()
            for worker in set(threading.enumerate()) - threads_before:
                worker.join(timeout=2)

        async with await HistoryStore.open(path, config={"cache_backend": "none"}):
            if expected_meta is not None:
                assert _read_store_meta_row(path) == expected_meta

    asyncio.run(scenario())


if __name__ == "__main__":
    test_newer_schema_version_rejected_without_mutation()
    test_rejected_runtime_fts5_configuration_without_mutation()
    print("F3 failure-injection checks passed")
