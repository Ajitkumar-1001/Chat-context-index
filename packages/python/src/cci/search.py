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
from dataclasses import dataclass, field

from .io_worker import fetchall
from .relevance import excerpt_for_query, query_terms, relevance
from .store import HistoryStore

DEFAULT_LIMIT = 20


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


async def search(store: HistoryStore, query: str, limit: int = DEFAULT_LIMIT) -> SearchResult:
    if not isinstance(limit, int) or not 1 <= limit <= 5000:
        raise ValueError("require 1 <= search limit <= 5000")
    terms = query_terms(query)
    if not terms:
        return SearchResult(
            candidates=[],
            diagnostics=[Diagnostic(code="empty_or_nonsearchable_query", stage="search")],
        )

    # Count distinct term matches before LIMIT. Recency breaks ties so repeated older matches
    # cannot crowd a later correction out of the candidate pool. Each MATCH is one quoted term.
    matches = " UNION ALL ".join(
        "SELECT message_id FROM message_fts WHERE history_id = ? AND message_fts MATCH ?"
        for _ in terms
    )
    parameters: list[str | int] = []
    for term in terms:
        parameters.extend((store.history_id, '"' + term.replace('"', '""') + '"'))
    parameters.append(limit)
    cursor = await store.connection.execute(
        "SELECT m.message_id, m.seq, m.original_payload, m.text_projection FROM messages m "
        "JOIN (SELECT message_id, count(*) AS matches FROM (" + matches + ") "
        "GROUP BY message_id) ranked ON ranked.message_id = m.message_id "
        "ORDER BY ranked.matches DESC, m.seq DESC LIMIT ?", parameters,
    )
    rows = await fetchall(cursor)

    candidates: list[LexicalCandidate] = []
    for message_id, seq, original_payload_json, text_projection in rows:
        payload = json.loads(original_payload_json)
        content = payload.get("content")
        parts = [("/content", content)] if isinstance(content, str) else [
            (f"/content/{i}/text", block["text"]) for i, block in enumerate(content or [])
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
        if not parts:
            continue
        source_pointer, text = max(parts, key=lambda part: relevance(part[1], terms))
        excerpt = excerpt_for_query(text, terms, 200)
        content_hash = hashlib.sha256(text_projection.encode("utf-8")).hexdigest()
        candidates.append(
            LexicalCandidate(
                message_id=message_id,
                seq=seq,
                source_pointer=source_pointer,
                excerpt=excerpt,
                content_hash=content_hash,
            )
        )

    return SearchResult(candidates=candidates, diagnostics=[])
