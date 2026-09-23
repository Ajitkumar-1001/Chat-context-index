"""retrieve() (contracts/operations.md `retrieve()`; data-model.md Snapshot).

Snapshot rule: captures `Snapshot` in one short read transaction at the start of the request;
original records and lexical candidates are materialized before that transaction closes. This
milestone's `HistoryStore` owns a single AsyncConnection (no separate reader connections yet —
a known limitation, not fixed here), so this transaction is serialized through the same
`write_lock` every writer uses rather than running concurrently with one.

Emission-time VersionConflict (data-model.md Snapshot/Generation lifecycle case 2): if a
concurrent `clear_history()` commits a new `cache_generation` after this request captured its
Snapshot but before it emits its result, the request fails with `VersionConflict` rather than
return or cache a stale-generation result.

`tree`/`auto` routing modes fall back to lexical evidence with `index_degraded` reported when
no tree exists yet — true for every request in this milestone, since `index()` is T053 (out of
this batch's scope). The fallback path is real production behavior (FR-006), not a stub.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import VersionConflict
from .io_worker import fetchone
from .search import Diagnostic, search
from .store import HistoryStore

CONTRACT_VERSION = 1


async def _mid_retrieve_barrier() -> None:
    """No-op by default. Failure-injection tests monkeypatch this to pause a request between
    Snapshot capture (lock released) and the emission-time generation check — the exact window
    data-model.md Snapshot/Generation lifecycle case 2 (F10's reader case) concerns."""


@dataclass(frozen=True)
class Snapshot:
    history_revision: int
    index_revision: int
    snapshot_max_seq: int
    cache_generation: int


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    message_id: str
    seq: int
    source_pointer: str
    excerpt: str
    content_hash: str


@dataclass(frozen=True)
class Coverage:
    coverage_limited: bool
    index_degraded: bool
    omitted_excerpt_count: int


@dataclass(frozen=True)
class Routing:
    requested_mode: str
    actual_mode: str
    candidate_count: int
    selected_chunk_ids: list[str]


@dataclass(frozen=True)
class Usage:
    current_provider_calls: int = 0
    retries: int = 0
    usage_unknown: bool = False
    memo_hits: int = 0
    memo_misses: int = 0
    memo_errors: int = 0
    stage_ms: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalResult:
    contract_version: int
    history_id: str
    status: str  # ok | empty | partial
    snapshot: Snapshot
    evidence: list[Evidence]
    routing: Routing
    coverage: Coverage
    diagnostics: list[Diagnostic]
    usage: Usage


async def _capture_snapshot(store: HistoryStore) -> Snapshot:
    cursor = await store.connection.execute(
        "SELECT history_revision, index_revision, cache_generation, seq_high_water_mark "
        "FROM store_meta WHERE id = 1"
    )
    row = await fetchone(cursor)
    assert row is not None  # store_meta always has exactly one row once open() has succeeded
    history_revision, index_revision, cache_generation, seq_high_water_mark = row
    return Snapshot(
        history_revision=history_revision,
        index_revision=index_revision,
        snapshot_max_seq=seq_high_water_mark,
        cache_generation=cache_generation,
    )


async def _has_tree(store: HistoryStore) -> bool:
    cursor = await store.connection.execute(
        "SELECT COUNT(*) FROM nodes WHERE history_id = ?", (store.history_id,)
    )
    row = await fetchone(cursor)
    assert row is not None
    return row[0] > 0


async def _current_cache_generation(store: HistoryStore) -> int:
    cursor = await store.connection.execute("SELECT cache_generation FROM store_meta WHERE id = 1")
    row = await fetchone(cursor)
    assert row is not None
    return row[0]


async def retrieve(
    store: HistoryStore,
    query: str,
    mode: str = "auto",
    max_selected_chunks: int | None = None,
) -> RetrievalResult:
    limit = max_selected_chunks or store.config.max_selected_chunks

    async with store.write_lock:
        async with store.connection:
            snapshot = await _capture_snapshot(store)
            search_result = await search(store, query, limit=limit)
            tree_exists = await _has_tree(store)

    index_degraded = not tree_exists
    # No tree exists at all in this milestone (index() is T053) — every mode degrades to
    # lexical. Once a tree exists, `tree`/`auto` would route through the shared
    # context-assembly renderer (T086) for navigation prompts instead of this fallback.
    actual_mode = "lexical"

    diagnostics = list(search_result.diagnostics)
    evidence = [
        Evidence(
            evidence_id=f"ev_{i + 1}",
            message_id=c.message_id,
            seq=c.seq,
            source_pointer=c.source_pointer,
            excerpt=c.excerpt,
            content_hash=c.content_hash,
        )
        for i, c in enumerate(search_result.candidates)
    ]

    status = "ok" if evidence else "empty"
    if status == "empty" and not diagnostics:
        diagnostics = [Diagnostic(code="no_matching_evidence", stage="retrieve")]

    await _mid_retrieve_barrier()

    # Emission-time VersionConflict (data-model.md case 2): re-check cache_generation right
    # before returning.
    current_generation = await _current_cache_generation(store)
    if current_generation != snapshot.cache_generation:
        raise VersionConflict(
            f"cache_generation changed from {snapshot.cache_generation} to "
            f"{current_generation} between snapshot capture and result emission — a concurrent "
            "clear_history() invalidated this request"
        )

    return RetrievalResult(
        contract_version=CONTRACT_VERSION,
        history_id=store.history_id,
        status=status,
        snapshot=snapshot,
        evidence=evidence,
        routing=Routing(
            requested_mode=mode,
            actual_mode=actual_mode,
            candidate_count=len(search_result.candidates),
            selected_chunk_ids=[],
        ),
        coverage=Coverage(
            coverage_limited=len(search_result.candidates) >= limit,
            index_degraded=index_degraded,
            omitted_excerpt_count=0,
        ),
        diagnostics=diagnostics,
        usage=Usage(),
    )
