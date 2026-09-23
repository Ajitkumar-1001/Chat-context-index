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

With an explicit provider, tree/auto modes navigate the persisted hierarchy. Without one,
retrieval stays local. Tree summaries guide selection; only original messages become evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time

from .context_assembly import EvidenceBlock, render_evidence_context
from .errors import VersionConflict
from .io_worker import fetchone
from .search import Diagnostic, search
from .store import HistoryStore
from .provider import CallBudget, MemoizedProvider

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
    input_tokens: int | None = None
    output_tokens: int | None = None


def usage_from_budget(budget: CallBudget) -> Usage:
    return Usage(
        current_provider_calls=budget.calls, retries=budget.retries,
        usage_unknown=budget.usage_unknown, memo_hits=budget.memo_hits, memo_misses=budget.memo_misses,
        input_tokens=None if budget.usage_unknown else budget.input_tokens,
        output_tokens=None if budget.usage_unknown else budget.output_tokens,
    )


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
    *,
    provider: MemoizedProvider | None = None,
    deadline_s: float | None = None,
    _budget: CallBudget | None = None,
) -> RetrievalResult:
    limit = store.config.max_selected_chunks if max_selected_chunks is None else max_selected_chunks
    if mode not in ("auto", "tree", "lexical") or not 1 <= limit <= 5000:
        raise ValueError("require a valid retrieval mode and 1 <= max_selected_chunks <= 5000")
    deadline_at = time.monotonic() + (deadline_s if deadline_s is not None else store.config.request_deadline_retrieve_s)
    budget = _budget if _budget is not None else CallBudget(store.config.provider_attempt_limit_retrieve)

    async with store.write_lock:
        async with store.connection:
            snapshot = await _capture_snapshot(store)
            search_result = await search(store, query, limit=limit)
            tree_exists = await _has_tree(store)

    index_degraded = not tree_exists
    actual_mode = "lexical"
    diagnostics = list(search_result.diagnostics)
    candidates = list(search_result.candidates)
    selected_chunks = []
    limited = len(candidates) >= limit
    if mode != "lexical" and provider is not None and tree_exists and query.strip():
        from .tree_retrieval import navigate_tree

        tree_candidates, selected_chunks, tree_diagnostics, tree_limited = await navigate_tree(
            store, query, snapshot, provider, budget, deadline_at, limit,
        )
        diagnostics.extend(tree_diagnostics)
        index_degraded = bool(tree_diagnostics)
        limited |= tree_limited
        actual_mode = mode if selected_chunks or not tree_diagnostics else "lexical"
        # Lexical evidence remains available for the unindexed tail and routing misses.
        candidates = tree_candidates + candidates
    elif mode == "tree" and tree_exists and provider is None:
        diagnostics.append(Diagnostic("tree_provider_missing", "retrieve"))
        index_degraded = True

    await _mid_retrieve_barrier()
    async with store.write_lock:
        current = await _capture_snapshot(store)
    if current.cache_generation != snapshot.cache_generation:
        raise VersionConflict("history was cleared between snapshot capture and result emission")
    if selected_chunks and current.index_revision != snapshot.index_revision:
        candidates = list(search_result.candidates)
        selected_chunks = []
        actual_mode = "lexical"
        index_degraded = True
        diagnostics.append(Diagnostic("tree_revision_changed", "retrieve"))

    unique = {}
    for candidate in candidates:
        unique.setdefault((candidate.message_id, candidate.source_pointer), candidate)
    evidence = [
        Evidence(
            evidence_id=f"ev_{i + 1}",
            message_id=c.message_id,
            seq=c.seq,
            source_pointer=c.source_pointer,
            excerpt=c.excerpt,
            content_hash=c.content_hash,
        )
        for i, c in enumerate(unique.values())
    ]
    bounded = []
    for item in evidence:
        if len(render_evidence_context([
            EvidenceBlock(e.evidence_id, e.source_pointer, e.excerpt) for e in bounded + [item]
        ])) <= store.config.max_evidence_text_scalars:
            bounded.append(item)
    omitted = len(evidence) - len(bounded)
    evidence = sorted(bounded, key=lambda e: e.seq)

    status = "ok" if evidence else "empty"
    if status == "empty" and not diagnostics:
        diagnostics = [Diagnostic(code="no_matching_evidence", stage="retrieve")]

    return RetrievalResult(
        contract_version=CONTRACT_VERSION,
        history_id=store.history_id,
        status=status,
        snapshot=snapshot,
        evidence=evidence,
        routing=Routing(
            requested_mode=mode,
            actual_mode=actual_mode,
            candidate_count=len(unique),
            selected_chunk_ids=selected_chunks,
        ),
        coverage=Coverage(
            coverage_limited=limited or omitted > 0,
            index_degraded=index_degraded,
            omitted_excerpt_count=omitted,
        ),
        diagnostics=diagnostics,
        usage=usage_from_budget(budget),
    )
