"""Failure injection F3 — incompatible open (spec/fixtures/failure-injection-harness.md F3, AT-12).

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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

import apsw

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


if __name__ == "__main__":
    test_newer_schema_version_rejected_without_mutation()
    test_rejected_runtime_fts5_configuration_without_mutation()
    print("F3 failure-injection checks passed")
