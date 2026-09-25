"""clear_history() (contracts/operations.md `clear_history()`; data-model.md Snapshot/
Generation lifecycle case 3; spec/cache-format.md Purge scoping).

Sequence (contracts/operations.md): gate new local work -> quiesce in-flight local operations
(bounded by the 10s default deadline) -> atomically delete live history/index/FTS data and
rotate `cache_generation` -> local cache deletion -> scoped external (Redis) purge attempt.

Every store file holds exactly one history (`store_meta` is a singleton row), so the atomic
delete step truncates every store-scoped table unconditionally rather than filtering by
`history_id`.

`seq_high_water_mark` is deliberately NOT reset here — data-model.md Message.seq: "not reused
after clear_history (high-water mark retained)" (Failure-Injection.md F11).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import apsw

from .cache import RedisMemoCache
from .cache_key import scope_digest
from .errors import BudgetExceeded, InputValidationError
from .search_metadata import assert_search_metadata, immediate_transaction
from .store import HistoryStore, map_storage_error

_CLEARED_TABLES = (
    "lexical_message_meta",
    "messages",
    "message_fts",
    "summary_fts",
    "ingest_receipts",
    "exchanges",
    "chunks",
    "nodes",
    "node_chunks",
)

# The external purge attempt's own bound — distinct from the 10s local quiescence deadline
# above (that one bounds waiting for in-flight writers; this one bounds a best-effort Redis
# cleanup that isn't itself required for the clear to be reported complete). No config default
# names this value; a fixed generous bound is a reasonable default (spec.md Assumptions: every
# proposed default here is configurable, not immutable).
_PURGE_ATTEMPT_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class ClearReport:
    logical_clear_complete: bool
    cache_purge_pending: bool


async def _record_pending_purge(store: HistoryStore, scope_id: str) -> None:
    async with store.write_lock:
        try:
            async with store.connection:
                await store.connection.execute(
                    "INSERT OR REPLACE INTO pending_cache_purges (scope_id, requested_at) "
                    "VALUES (?, ?)",
                    (scope_id, time.time()),
                )
        except apsw.Error as exc:
            raise map_storage_error(exc) from exc


async def clear_history(store: HistoryStore, expected_history_id: str) -> ClearReport:
    """`expected_history_id` must be the exact current history_id — a stale or wrong ID is
    rejected, not treated as a no-op."""
    if expected_history_id != store.history_id:
        raise InputValidationError(
            f"expected_history_id {expected_history_id!r} does not match the open store's "
            f"history_id {store.history_id!r} — not treated as a no-op"
        )

    await store.close_write_gate()
    old_cache_generation: int | None = None
    try:
        drained = await store.wait_for_writers_to_drain(
            store.config.clear_history_quiescence_deadline_s
        )
        if not drained:
            # The clear itself fails; the still-running write is left unaffected — never the
            # reverse (`/speckit-analyze` finding G1).
            raise BudgetExceeded(
                f"clear_history() quiescence deadline "
                f"({store.config.clear_history_quiescence_deadline_s}s) exceeded waiting for "
                "in-flight local writers to commit; the write(s) continue unaffected"
            )

        async with store.write_lock:
            connection = store.connection
            try:
                async with immediate_transaction(connection):
                    await assert_search_metadata(connection)
                    cursor = await connection.execute(
                        "SELECT cache_generation FROM store_meta WHERE id = 1"
                    )
                    row = [r async for r in cursor][0]
                    old_cache_generation = row[0]

                    for table in _CLEARED_TABLES:
                        await connection.execute(f"DELETE FROM {table}")
                    await connection.execute(
                        "UPDATE store_meta SET cache_generation = cache_generation + 1, "
                        "history_revision = history_revision + 1, "
                        "search_metadata_revision = search_metadata_revision + 1, "
                        "index_revision = index_revision + 1, "
                        "index_committed_seq = 0, index_pending_seq = 0 WHERE id = 1"
                    )
            except apsw.Error as exc:
                raise map_storage_error(exc) from exc
    finally:
        # Resume admitting new writers on both success and BudgetExceeded failure paths.
        await store.open_write_gate()

    # Local cache deletion (INV-11: the invalidation barrier — cache_generation's rotation
    # above — already took effect for future library access before this step even runs).
    await store.cache.clear()

    cache_purge_pending = False
    if isinstance(store.cache, RedisMemoCache):
        retired_scope_digest = scope_digest(
            store.config.application_namespace or "", store.store_instance_id,
            store.history_id, old_cache_generation,
        )
        prefix = f"cci:memo:v2:{retired_scope_digest}:"
        try:
            await asyncio.wait_for(store.cache.purge_prefix(prefix), timeout=_PURGE_ATTEMPT_TIMEOUT_S)
        except Exception:
            # A timeout is never reported as a completed purge (spec/cache-format.md Purge
            # scoping) — record a durable pending scope for later bounded cleanup instead.
            cache_purge_pending = True
            await _record_pending_purge(store, retired_scope_digest)

    return ClearReport(logical_clear_complete=True, cache_purge_pending=cache_purge_pending)
