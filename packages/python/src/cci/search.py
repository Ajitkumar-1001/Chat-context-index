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
import re
from dataclasses import dataclass, field

from .io_worker import fetchall
from .relevance import excerpt_for_query, fts_term_expression, query_terms, relevance
from .store import HistoryStore

DEFAULT_LIMIT = 20
_CORRECTION_TERMS = query_terms(
    "correction corrected reconsidered revised instead retired change changed moved actually superseded"
)


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


def is_correction(text: str) -> bool:
    return relevance(text, _CORRECTION_TERMS) > 0


def _source_anchor(source: LexicalCandidate, query_keys: set[str]) -> str | None:
    """Prefer a source-specific name; otherwise use the last non-query content word."""
    eligible: list[tuple[bool, int, str]] = []
    for match in re.finditer(r"\w+", source.excerpt):
        raw = match.group()
        terms = query_terms(raw)
        if not terms:
            continue
        term = terms[0]
        if term in query_keys or (len(term) < 5 and not any(c.isdigit() for c in term)):
            continue
        eligible.append((any(c.isupper() for c in raw), match.start(), term))
    return max(eligible)[2] if eligible else None


def _candidate_from_row(row: tuple, terms: list[str]) -> LexicalCandidate | None:
    message_id, seq, original_payload_json, text_projection = row
    payload = json.loads(original_payload_json)
    content = payload.get("content")
    parts = [("/content", content)] if isinstance(content, str) else [
        (f"/content/{i}/text", block["text"]) for i, block in enumerate(content or [])
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    if not parts:
        return None
    source_pointer, text = max(parts, key=lambda part: relevance(part[1], terms))
    if not relevance(text, terms):
        return None
    return LexicalCandidate(
        message_id=message_id,
        seq=seq,
        source_pointer=source_pointer,
        excerpt=excerpt_for_query(text, terms, 200),
        content_hash=hashlib.sha256(text_projection.encode("utf-8")).hexdigest(),
    )


async def search(store: HistoryStore, query: str, limit: int = DEFAULT_LIMIT) -> SearchResult:
    if not isinstance(limit, int) or not 1 <= limit <= 5000:
        raise ValueError("require 1 <= search limit <= 5000")
    terms = query_terms(query)
    if not terms:
        return SearchResult(
            candidates=[],
            diagnostics=[Diagnostic(code="empty_or_nonsearchable_query", stage="search")],
        )

    # Count distinct inflection-key matches before LIMIT; frequency within one message does not score.
    matches = " UNION ALL ".join(
        "SELECT message_id FROM message_fts WHERE history_id = ? AND message_fts MATCH ?"
        for _ in terms
    )
    parameters: list[str | int] = []
    for term in terms:
        parameters.extend((store.history_id, fts_term_expression(term)))
    parameters.append(limit)
    cursor = await store.connection.execute(
        "SELECT m.message_id, m.seq, m.original_payload, m.text_projection FROM messages m "
        "JOIN (SELECT message_id, count(*) AS matches FROM (" + matches + ") "
        "GROUP BY message_id) ranked ON ranked.message_id = m.message_id "
        "ORDER BY ranked.matches DESC, m.seq DESC LIMIT ?", parameters,
    )
    rows = await fetchall(cursor)

    return SearchResult(
        candidates=[c for row in rows if (c := _candidate_from_row(row, terms))], diagnostics=[]
    )


async def linked_corrections(
    store: HistoryStore, sources: list[LexicalCandidate], query: str, max_seq: int, limit: int,
) -> tuple[list[LexicalCandidate], bool]:
    """Find explicit later revisions sharing an original source's non-query anchor."""
    query_keys = set(query_terms(query))
    anchors_by_source = []
    for source in sources[:4]:
        anchor = _source_anchor(source, query_keys)
        anchors_by_source.append((source.seq, [anchor] if anchor else []))
    anchors = list(dict.fromkeys(term for _, terms in anchors_by_source for term in terms))
    if not anchors:
        return [], False
    expression = "(" + " OR ".join(fts_term_expression(term) for term in anchors) + ") AND (" + \
        " OR ".join(fts_term_expression(term) for term in _CORRECTION_TERMS) + ")"
    row_limit = min(128, max(32, limit * 8))
    cursor = await store.connection.execute(
        "SELECT m.message_id, m.seq, m.original_payload, m.text_projection FROM message_fts "
        "JOIN messages m ON m.message_id = message_fts.message_id "
        "WHERE message_fts.history_id = ? AND message_fts MATCH ? AND m.seq <= ? "
        "ORDER BY m.seq DESC LIMIT ?",
        (store.history_id, expression, max_seq, row_limit),
    )
    rows = await fetchall(cursor)
    found = []
    for row in rows:
        candidate = _candidate_from_row(row, anchors + _CORRECTION_TERMS)
        if candidate is None or not is_correction(candidate.excerpt):
            continue
        if any(
            candidate.seq > seq and relevance(candidate.excerpt, terms)
            for seq, terms in anchors_by_source if terms
        ):
            found.append(candidate)
    return found, len(rows) >= row_limit
