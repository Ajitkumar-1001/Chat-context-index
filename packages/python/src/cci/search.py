"""search() (contracts/operations.md `search()`/`get_messages()`/`view_node()`).

Never calls a model. Queries are parameterized FTS5 MATCH expressions built from sanitized
tokens — never a raw/unbounded FTS5 expression (a user query containing `"`, `-`, `*`, `:`,
`(`, or FTS5 keywords like `OR`/`NOT`/`NEAR` would otherwise error or change the query's
semantics). An empty/non-searchable query returns a typed empty result with a diagnostic,
never a raised error and never a model call (FR-005).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field

import apsw

from .io_worker import fetchall
from .relevance import best_anchor, excerpt_for_query, fts_term_expression, query_terms, relevance, text_keys
from .search_metadata import assert_search_metadata
from .store import HistoryStore, map_storage_error

DEFAULT_LIMIT = 20
MAX_LINKED = 2


@dataclass(frozen=True)
class LexicalCandidate:
    message_id: str
    seq: int
    source_pointer: str
    excerpt: str
    content_hash: str


@dataclass(frozen=True)
class Diagnostic:
    code: str
    stage: str
    retryable: bool = False


@dataclass(frozen=True)
class SearchResult:
    candidates: list[LexicalCandidate] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def _text_parts(payload: dict) -> list[tuple[str, str]]:
    content = payload.get("content")
    return [("/content", content)] if isinstance(content, str) else [
        (f"/content/{i}/text", block["text"]) for i, block in enumerate(content or [])
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]


def _candidate_from_row(
    row: tuple, terms: list[str], excerpt_terms: list[str] | None = None,
) -> LexicalCandidate | None:
    """The text part matching most `terms`, excerpted around `excerpt_terms` (default: `terms`)."""
    message_id, seq, original_payload_json, text_projection = row
    parts = _text_parts(json.loads(original_payload_json))
    if not parts:
        return None
    source_pointer, text = max(parts, key=lambda part: relevance(part[1], terms))
    if not relevance(text, terms):
        return None
    return LexicalCandidate(
        message_id=message_id,
        seq=seq,
        source_pointer=source_pointer,
        excerpt=excerpt_for_query(text, excerpt_terms or terms, 200),
        content_hash=hashlib.sha256(text_projection.encode("utf-8")).hexdigest(),
    )


async def search(store: HistoryStore, query: str, limit: int = DEFAULT_LIMIT) -> SearchResult:
    async with store.write_lock:
        try:
            async with store.connection:
                return await _search_in_snapshot(store, query, limit)
        except apsw.Error as exc:
            raise map_storage_error(exc) from exc


async def _search_in_snapshot(store: HistoryStore, query: str, limit: int = DEFAULT_LIMIT) -> SearchResult:
    """The caller owns the connection lock and read snapshot (also used by retrieve)."""
    if not isinstance(limit, int) or not 1 <= limit <= 5000:
        raise ValueError("require 1 <= search limit <= 5000")
    await assert_search_metadata(store.connection)
    terms = query_terms(query)
    if not terms:
        return SearchResult(
            candidates=[],
            diagnostics=[Diagnostic(code="empty_or_nonsearchable_query", stage="search")],
        )

    # Count distinct inflection-key matches before LIMIT; frequency within one message does not score.
    matches = " UNION ALL ".join(
        "SELECT rowid AS fts_rowid FROM message_fts WHERE message_fts MATCH ?"
        for _ in terms
    )
    parameters: list[str | int] = [fts_term_expression(term) for term in terms]
    parameters.extend((store.history_id, limit))
    cursor = await store.connection.execute(
        "WITH top AS MATERIALIZED (SELECT x.message_id,x.seq,count(*) AS matches FROM (" + matches + ") "
        "hits JOIN lexical_message_meta x ON x.fts_rowid=hits.fts_rowid WHERE x.history_id=? "
        "GROUP BY x.message_id,x.seq ORDER BY matches DESC,x.seq DESC LIMIT ?) "
        "SELECT m.message_id,m.seq,m.original_payload,m.text_projection FROM top "
        "JOIN messages m ON m.message_id=top.message_id ORDER BY top.matches DESC,top.seq DESC", parameters,
    )
    rows = await fetchall(cursor)

    return SearchResult(
        candidates=[c for row in rows if (c := _candidate_from_row(row, terms))], diagnostics=[]
    )


async def linked_corrections(
    store: HistoryStore, sources: list[LexicalCandidate], query: str, max_seq: int, limit: int,
) -> tuple[list[tuple[LexicalCandidate, LexicalCandidate]], bool]:
    """Pair top hits with the newest later message naming the hit's anchor (plan-eng-review D4).

    No correction vocabulary: a later statement that names the same rare, name-like word is linked
    whatever its phrasing. Each of the top 4 sources links at most one message, `MAX_LINKED` in
    total; word-for-word copies of the source and messages past the snapshot are never linked.
    """
    tops = sources[:4]
    if not tops:
        return [], False
    query_list = query_terms(query)
    query_keys = set(query_list)
    # Rarity counts distinct hit texts: verbatim copies are one statement (SC-011 clarification).
    distinct_texts = {source.content_hash: source.excerpt for source in sources}
    hit_counts = Counter(key for excerpt in distinct_texts.values() for key in text_keys(excerpt))
    cursor = await store.connection.execute(
        "SELECT message_id, original_payload FROM messages WHERE message_id IN ("
        + ",".join("?" for _ in tops) + ")", [source.message_id for source in tops],
    )
    payloads = {message_id: json.loads(payload) for message_id, payload in await fetchall(cursor)}
    anchors: list[tuple[LexicalCandidate, str]] = []
    for source in tops:
        text = dict(_text_parts(payloads.get(source.message_id, {}))).get(source.source_pointer, "")
        anchor = best_anchor(text, query_keys, hit_counts)
        if anchor:
            anchors.append((source, anchor))
    if not anchors:
        return [], False
    row_limit = min(128, max(32, limit * 8))
    # FTS5 walks descending rowids and stops at the limit before payload hydration.
    # Rowids can differ from seq in migrated stores, so recovery preserves usable rowids:
    # the bounded subset is selected by rowid, then the selected rows are sorted by seq.
    cursor = await store.connection.execute(
        "SELECT m.message_id, m.seq, m.original_payload, m.text_projection FROM ("
        "SELECT message_id FROM message_fts WHERE message_fts MATCH ? AND history_id = ? "
        "ORDER BY rowid DESC LIMIT ?) f JOIN messages m ON m.message_id = f.message_id "
        "WHERE m.seq <= ? ORDER BY m.seq DESC",
        (" OR ".join(dict.fromkeys(fts_term_expression(a) for _, a in anchors)), store.history_id,
         row_limit, max_seq),
    )
    rows = await fetchall(cursor)
    links: list[tuple[LexicalCandidate, LexicalCandidate]] = []
    linked_ids: set[str] = set()
    for source, anchor in anchors:
        if len(links) >= MAX_LINKED:
            break
        for row in rows:  # newest first
            if row[1] <= source.seq or row[0] in linked_ids:
                continue
            linked = _candidate_from_row(row, [anchor], [anchor, *query_list])
            if linked is not None and linked.content_hash != source.content_hash:
                links.append((source, linked))
                linked_ids.add(linked.message_id)
                break
    return links, len(rows) >= row_limit
