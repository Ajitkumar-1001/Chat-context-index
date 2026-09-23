"""search() (contracts/operations.md `search()`/`get_messages()`/`view_node()`).

Never calls a model. Queries are parameterized FTS5 MATCH expressions built from sanitized
tokens — never a raw/unbounded FTS5 expression (a user query containing `"`, `-`, `*`, `:`,
`(`, or FTS5 keywords like `OR`/`NOT`/`NEAR` would otherwise error or change the query's
semantics). An empty/non-searchable query returns a typed empty result with a diagnostic,
never a raised error and never a model call (FR-005).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .io_worker import fetchall
from .models import source_pointer_for_offset
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


def _sanitize_fts_query(query: str) -> str:
    """Tokenizes on whitespace, double-quotes each token (escaping internal `"`), joins with
    spaces — the only shape ever passed to FTS5 MATCH. This turns FTS5 syntax characters
    (`-`, `*`, `:`, `(`, keywords) into literal token content rather than query operators."""
    tokens = query.split()
    quoted = []
    for tok in tokens:
        escaped = tok.replace('"', '""')
        if escaped:
            quoted.append(f'"{escaped}"')
    return " ".join(quoted)


def _excerpt_for(text_projection: str, first_token: str, window: int = 200) -> tuple[str, int]:
    """Best-effort excerpt centered on the first matched token; returns (excerpt, offset)."""
    idx = text_projection.lower().find(first_token.lower())
    if idx < 0:
        idx = 0
    start = max(0, idx - window // 2)
    end = min(len(text_projection), idx + window // 2)
    return text_projection[start:end], start


async def search(store: HistoryStore, query: str, limit: int = DEFAULT_LIMIT) -> SearchResult:
    sanitized = _sanitize_fts_query(query)
    if not sanitized:
        return SearchResult(
            candidates=[],
            diagnostics=[Diagnostic(code="empty_or_nonsearchable_query", stage="search")],
        )

    cursor = await store.connection.execute(
        "SELECT m.message_id, m.seq, m.original_payload, m.text_projection "
        "FROM message_fts f JOIN messages m ON m.message_id = f.message_id "
        "WHERE f.history_id = ? AND message_fts MATCH ? "
        "ORDER BY m.seq LIMIT ?",
        (store.history_id, sanitized, limit),
    )
    rows = await fetchall(cursor)

    first_token = query.split()[0] if query.split() else ""
    candidates: list[LexicalCandidate] = []
    for message_id, seq, original_payload_json, text_projection in rows:
        import json

        payload = json.loads(original_payload_json)
        excerpt, offset = _excerpt_for(text_projection, first_token)
        source_pointer = source_pointer_for_offset(payload.get("content"), offset)
        if isinstance(payload.get("content"), list):
            # A structured excerpt must stay inside the field named by its pointer.
            match = max(0, text_projection.lower().find(first_token.lower()))
            source_pointer = source_pointer_for_offset(payload["content"], match)
            if source_pointer.endswith("/text"):
                block = payload["content"][int(source_pointer.split("/")[2])]
                excerpt, _ = _excerpt_for(block["text"], first_token)
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
