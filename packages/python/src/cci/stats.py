"""stats() (contracts/operations.md `stats()`; FR-010).

Structured counts/timings/outcomes. Identifiers and content hashes only by default — never
prompts or message bodies; there is no verbose/payload-tracing opt-in in this codebase, so none
is exposed here (nothing to leak). On a freshly opened store with no prior activity, every field
is zero-valued — never an error.

Read-only and not generation-scoped (contracts/operations.md `clear_history()`: "`stats()` is
read-only and not generation-scoped; it needs no gating").

Provider/cache counters (`provider_calls`, `memo_hits`, etc.) come from `store.usage_log` — an
in-memory, process-lifetime-only accumulator (provider.py `UsageLog`) fed by `MemoizedProvider`.
They reset on reopen; the message/node/chunk counts below are durable (read fresh from the
store every call).
"""

from __future__ import annotations

from dataclasses import dataclass

from .io_worker import fetchone
from .store import HistoryStore


@dataclass(frozen=True)
class Stats:
    history_message_count: int
    ingest_receipt_count: int
    node_count: int
    chunk_count: int
    history_revision: int
    index_revision: int
    cache_generation: int
    index_committed_seq: int
    pending_cache_purge_count: int
    provider_calls: int
    provider_retries: int
    provider_errors: int
    memo_hits: int
    memo_misses: int
    memo_errors: int
    reused_operation_usage_count: int
    usage_unknown: bool


async def _count(store: HistoryStore, table: str, *, scoped: bool = True) -> int:
    query = f"SELECT COUNT(*) FROM {table}"
    params: tuple = ()
    if scoped:
        query += " WHERE history_id = ?"
        params = (store.history_id,)
    cursor = await store.connection.execute(query, params)
    row = await fetchone(cursor)
    assert row is not None
    return row[0]


async def stats(store: HistoryStore) -> Stats:
    async with store.write_lock:
        async with store.connection:
            message_count = await _count(store, "messages")
            receipt_count = await _count(store, "ingest_receipts")
            node_count = await _count(store, "nodes")
            chunk_count = await _count(store, "chunks")
            pending_purge_count = await _count(store, "pending_cache_purges", scoped=False)

            cursor = await store.connection.execute(
                "SELECT history_revision, index_revision, cache_generation, "
                "index_committed_seq FROM store_meta WHERE id = 1"
            )
            row = await fetchone(cursor)
            assert row is not None
            history_revision, index_revision, cache_generation, index_committed_seq = row

    log = store.usage_log
    return Stats(
        history_message_count=message_count,
        ingest_receipt_count=receipt_count,
        node_count=node_count,
        chunk_count=chunk_count,
        history_revision=history_revision,
        index_revision=index_revision,
        cache_generation=cache_generation,
        index_committed_seq=index_committed_seq,
        pending_cache_purge_count=pending_purge_count,
        provider_calls=log.provider_calls,
        provider_retries=log.provider_retries,
        provider_errors=log.provider_errors,
        memo_hits=log.memo_hits,
        memo_misses=log.memo_misses,
        memo_errors=log.memo_errors,
        reused_operation_usage_count=log.reused_operation_usage_count,
        usage_unknown=log.usage_unknown_count > 0,
    )
