"""Message and IngestReceipt models (spec/normalization.md; data-model.md).

Identity rule, quoted verbatim (data-model.md): "identity is (history_id, source_id,
external_id) when external_id is supplied, else (history_id, source_id, idempotency_key,
position_in_batch). Equal original_payload/text_projection across two different identities
is expected and MUST NOT be collapsed." (INV-02)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SUPPORTED_ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})

# Batch limits (data-model.md, quoted verbatim): "<= 1,000 messages and <= 8 MiB encoded
# JSON per batch, <= 256 KiB per individual record."
MAX_MESSAGES_PER_BATCH = 1_000
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 256 * 1024


@dataclass(frozen=True)
class InputMessage:
    """One message as submitted to ingest(), before it becomes a stored Message."""

    role: str
    content: str | list[dict[str, Any]] | None
    external_id: str | None = None
    metadata: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


def message_payload(m: InputMessage) -> dict[str, Any]:
    """The full `original_payload`, "stored verbatim" (data-model.md Message) — every
    submitted field that isn't already its own identity/storage column. `external_id` is
    identity, not payload, so it is excluded here."""
    payload: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.metadata is not None:
        payload["metadata"] = m.metadata
    if m.tool_calls is not None:
        payload["tool_calls"] = m.tool_calls
    if m.tool_call_id is not None:
        payload["tool_call_id"] = m.tool_call_id
    return payload


def canonical_payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def render_text_projection(content: str | list[dict[str, Any]] | None) -> str | None:
    """Deterministic text rendering for search (data-model.md Message.text_projection),
    separate from `original_payload` (spec/normalization.md Raw/projection separation).
    Nullable only for records with no renderable text (a supported tool-call-only record, or
    a structured-block message with no text-type block)."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts) if parts else None


def source_pointer_for_offset(content: str | list[dict[str, Any]] | None, offset: int) -> str:
    """Maps an offset within `render_text_projection(content)` back to the original field it
    came from (contracts/result-schemas.md `Evidence.source_pointer`; U3 fixture: `/content`,
    `/content/0/text`)."""
    if isinstance(content, list):
        cursor = 0
        for i, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if cursor <= offset < cursor + len(text):
                    return f"/content/{i}/text"
                cursor += len(text) + 1  # +1 for the joining "\n"
    return "/content"


def payload_hash(original_payload: dict[str, Any]) -> str:
    """For change detection, not identity (data-model.md Message)."""
    return hashlib.sha256(canonical_payload_json(original_payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Message:
    """One accepted, immutable conversational record (spec.md Key Entities)."""

    message_id: str
    seq: int
    history_id: str
    source_id: str
    external_id: str | None
    idempotency_key: str | None
    position_in_batch: int | None
    role: str
    original_payload: dict[str, Any]
    text_projection: str | None
    payload_hash: str
    session_metadata: dict[str, Any] | None


@dataclass(frozen=True)
class IngestReceipt:
    """The durable record of one committed ingest call (spec.md Key Entities)."""

    history_id: str
    source_id: str
    idempotency_key: str
    request_hash: str
    inserted_seq_start: int | None
    inserted_seq_end: int | None
    skipped_count: int
    unsupported_block_count: int
    indexing_status: str
    replayed: bool = False


@dataclass(frozen=True)
class Chunk:
    """One content-addressed span of source messages (spec.md Key Entities; data-model.md
    Chunk). `content_hash` identifies change, not identity — an unchanged chunk retains its
    `chunk_id` and hash across re-indexing."""

    chunk_id: str
    history_id: str
    source_message_span: str  # "<start_seq>-<end_seq>", inclusive
    content_hash: str
    rendering_version: int = 1


@dataclass(frozen=True)
class Node:
    """One topic-tree node (data-model.md Node).

    Validation rule, quoted verbatim (data-model.md): "a tree-publish only commits when the
    expected `store_meta.history_revision` and `index_revision` still match at commit time;
    otherwise retried a bounded number of times or reported `VersionConflict`."
    """

    node_id: str
    history_id: str
    parent_id: str | None
    sibling_order: int
    message_range: str  # "<start_seq>-<end_seq>", inclusive
    title: str | None
    summary: str | None
    state: str
    index_revision: int


@dataclass(frozen=True)
class NodeChunk:
    """Ordered link: leaf Node -> ordered Chunk references (data-model.md NodeChunk)."""

    node_id: str
    chunk_id: str
    chunk_order: int


def compute_request_hash(
    messages: list[InputMessage],
    source_id: str,
    adapter_version: str,
    session_metadata: dict[str, Any] | None,
) -> str:
    """Over ordered messages + source identity + adapter version + session metadata
    (data-model.md IngestReceipt; PRD FR-02)."""
    canonical = json.dumps(
        {
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "external_id": m.external_id,
                    "metadata": m.metadata,
                }
                for m in messages
            ],
            "source_id": source_id,
            "adapter_version": adapter_version,
            "session_metadata": session_metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
