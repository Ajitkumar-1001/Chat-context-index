"""Provider-neutral historical context for a host chat, RAG, or agent loop.

The host authorizes histories, calls its answer model, and persists execution checkpoints.
This API restores conversational context; it never replays tools or resumes a task executor.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from cci.context_assembly import EvidenceBlock, render_evidence_context
from cci.errors import VersionConflict
from cci.models import Message
from cci.provider import MemoizedProvider
from cci.relevance import excerpt_for_query, query_terms
from cci.retrieve import Evidence, RetrievalResult, evidence_rank, retrieve_with_links
from cci.stats import stats
from cci.store import HistoryStore


@dataclass(frozen=True)
class ContextItem:
    message_id: str
    seq: int
    source_pointer: str
    excerpt: str


@dataclass(frozen=True)
class Context:
    text: str
    items: list[ContextItem]
    omitted_candidates: int
    truncated_excerpts: int
    token_count: int | None = None
    retrieval: RetrievalResult | None = None


def message_item(message: Message) -> ContextItem | None:
    return next(iter(message_items(message)), None)


def message_items(message: Message) -> list[ContextItem]:
    content = message.original_payload.get("content")
    parts = [("/content", content)] if isinstance(content, str) else [
        (f"/content/{i}/text", b["text"]) for i, b in enumerate(content or [])
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]
    return [ContextItem(message.message_id, message.seq, pointer, text) for pointer, text in parts if text]


def _render(items: list[ContextItem]) -> str:
    return render_evidence_context([
        EvidenceBlock(
            evidence_id=item.message_id,
            source_pointer=item.source_pointer,
            excerpt=item.excerpt,
        )
        for item in sorted(items, key=lambda item: item.seq)
    ])


def pack_context(
    candidates: list[ContextItem],
    *,
    max_messages: int = 8,
    max_chars: int = 4_000,
    excerpt_chars: int = 200,
    max_tokens: int | None = None,
    token_counter: Callable[[str], int] | None = None,
    query: str = "",
    links: Mapping[tuple[str, str], Sequence[tuple[str, str]]] | None = None,
) -> Context:
    """Select in caller priority order, render in conversation order, and count omissions.

    The character budget includes the evidence labels and escaped delimiters. It is not a
    tokenizer estimate. Omission counts refer to supplied candidates, not the entire history.
    `links` maps a candidate's (message_id, source_pointer) to newer candidates that restate it.
    Such a source is packed with them; when both cannot fit, only the newer ones are packed, and
    the source is never packed without them (plan-eng-review D15).
    """
    if any(not isinstance(n, int) or n <= 0 for n in (max_messages, max_chars, excerpt_chars)):
        raise ValueError("context limits must be positive integers")

    if (max_tokens is None) != (token_counter is None) or (max_tokens is not None and max_tokens <= 0):
        raise ValueError("supply a positive max_tokens and the model token_counter together")

    unique: dict[tuple[str, str], ContextItem] = {}
    for item in candidates:
        unique.setdefault((item.message_id, item.source_pointer), item)
    selected: list[ContextItem] = []
    admitted: set[tuple[str, str]] = set()
    considered: set[tuple[str, str]] = set()
    truncated = 0
    terms = query_terms(query)

    def admit(keys: list[tuple[str, str]]) -> bool:
        nonlocal truncated
        items = [unique[key] for key in keys]
        bounded = [
            ContextItem(
                i.message_id, i.seq, i.source_pointer, excerpt_for_query(i.excerpt, terms, excerpt_chars)
            )
            for i in items
        ]
        text = _render(selected + bounded)
        if len(selected) + len(bounded) > max_messages or len(text) > max_chars or (
            token_counter is not None and max_tokens is not None and token_counter(text) > max_tokens
        ):
            return False
        selected.extend(bounded)
        admitted.update(keys)
        truncated += sum(len(b.excerpt) < len(i.excerpt) for b, i in zip(bounded, items, strict=True))
        return True

    for key in unique:
        if len(selected) >= max_messages:
            break
        if key in considered:
            continue
        newer = [k for k in (links or {}).get(key, ()) if k in unique and k != key]
        considered.add(key)
        if any(k in considered and k not in admitted for k in newer):
            continue  # its newer statement was already left out; never pack the source alone
        pending = [k for k in newer if k not in admitted]
        if admit([key, *pending]):
            considered.update(pending)
            continue
        for k in sorted(pending, key=lambda k: unique[k].seq, reverse=True):
            considered.add(k)
            admit([k])

    selected.sort(key=lambda item: item.seq)
    return Context(
        text=_render(selected),
        items=selected,
        omitted_candidates=len(unique) - len(selected),
        truncated_excerpts=truncated,
        token_count=token_counter(_render(selected)) if token_counter else None,
    )


async def prepare_context(
    store: HistoryStore,
    query: str,
    *,
    recent_messages: int = 4,
    max_messages: int = 8,
    max_chars: int = 4_000,
    excerpt_chars: int = 200,
    mode: str = "auto",
    provider: MemoizedProvider | None = None,
    max_tokens: int | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> Context:
    """Reserve room for recent messages, then add relevant older evidence.

    Recent records are read only through the retrieval snapshot's sequence boundary. A clear
    committed during assembly invalidates this result. Historical text is never an executable
    tool exchange. Pass the target model tokenizer
    to enforce an optional token budget on this memory text, including its evidence labels.
    """
    if (
        not isinstance(recent_messages, int)
        or not 0 <= recent_messages <= max_messages
        or max_messages > 5_000
    ):
        raise ValueError("require 0 <= recent_messages <= max_messages <= 5000")
    if max_messages <= 0 or max_chars <= 0:
        raise ValueError("context limits must be positive")

    # Validate packing limits before spending on navigation.
    pack_context([], max_messages=max_messages, max_chars=max_chars, excerpt_chars=excerpt_chars,
                 max_tokens=max_tokens, token_counter=token_counter)
    result, links = await retrieve_with_links(
        store, query, mode=mode, max_selected_chunks=max_messages, provider=provider,
    )
    end_seq = result.snapshot.snapshot_max_seq
    recent = await store.get_messages(
        max(1, end_seq - recent_messages + 1), end_seq, limit=recent_messages,
    ) if recent_messages else []
    # Newest first for selection priority; pack_context restores chronological display order.
    candidates = [item for message in reversed(recent) for item in message_items(message)]
    # Retrieval IDs retain selection priority after evidence is rendered chronologically.
    ranked = sorted(result.evidence, key=lambda e: evidence_rank(e.evidence_id))
    # Give each distinct text one chance before copies consume the remaining slots.
    # The earliest source of identical text is its representative; a later changed
    # correction is different text and keeps its own higher-priority slot.
    groups: dict[tuple[str, str], list[Evidence]] = {}
    for evidence in ranked:
        groups.setdefault((evidence.content_hash, evidence.source_pointer), []).append(evidence)
    first: list[Evidence] = []
    copies: list[Evidence] = []
    representative: dict[tuple[str, str], tuple[str, str]] = {}
    for group in groups.values():
        original = min(group, key=lambda e: e.seq)
        first.append(original)
        copies.extend(e for e in group if e is not original)
        for e in group:
            representative[(e.message_id, e.source_pointer)] = (original.message_id, original.source_pointer)
    candidates.extend(
        ContextItem(e.message_id, e.seq, e.source_pointer, e.excerpt) for e in first + copies
    )
    # A copy's newer statement also governs the copy chosen to represent it.
    pack_links: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for source, newer in links.items():
        targets = pack_links.setdefault(representative.get(source, source), [])
        targets.extend(representative.get(k, k) for k in newer if representative.get(k, k) not in targets)
    context = pack_context(candidates, max_messages=max_messages, max_chars=max_chars,
                           excerpt_chars=excerpt_chars, max_tokens=max_tokens, token_counter=token_counter,
                           query=query, links=pack_links)
    if (await stats(store)).cache_generation != result.snapshot.cache_generation:
        raise VersionConflict("history was cleared while preparing conversation context")
    return replace(context, retrieval=result)
