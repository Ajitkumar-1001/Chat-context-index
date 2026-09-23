"""ingest() (contracts/operations.md `ingest()`; spec/normalization.md).

Check order (spec/normalization.md, `/speckit-clarify` CHK002, verified by Contract-Fixtures.md
I2): the batch/receipt-level IdempotencyConflict check runs before any per-message
MessageConflict check.

Crash-recovery contract (contracts/operations.md `ingest()`): if the caller never observes
the receipt, retrying with the identical source_id/idempotency_key/input safely replays the
already-committed receipt — this is a direct consequence of the check order above, not
separate code: a retry is indistinguishable from the original call until the replay check
runs.

Write coordination: the whole idempotency-check-through-commit sequence runs under
`store.write_lock` (one AsyncConnection cannot run two concurrent transactions, and serializing
here also closes a same-key replay race) and is bracketed by `store.begin_write()`/`end_write()`
so `clear_history()` can quiesce in-flight writers (data-model.md Snapshot/Generation lifecycle
case 3).
"""

from __future__ import annotations

import time
from typing import Any

import apsw

from ._ids import prefixed_id
from .errors import IdempotencyConflict, InputValidationError, MessageConflict
from .io_worker import fetchone
from .models import (
    MAX_BATCH_BYTES,
    MAX_MESSAGES_PER_BATCH,
    MAX_RECORD_BYTES,
    SUPPORTED_ROLES,
    IngestReceipt,
    InputMessage,
    canonical_payload_json,
    compute_request_hash,
    message_payload,
    render_text_projection,
)
from .models import (
    payload_hash as compute_payload_hash,
)
from .store import HistoryStore, map_storage_error

ADAPTER_VERSION = "native-1"


def _before_commit_barrier() -> None:
    """No-op by default. Failure-injection tests monkeypatch this to terminate the process
    right before commit (Failure-Injection.md F1's commit-ambiguity kill point). Synchronous
    and called without `await` — a real process crash (`os._exit`) needs no async coordination.
    For an async pause (not a crash), see `_pause_before_commit_barrier` below."""


def _after_commit_barrier() -> None:
    """No-op by default. F1's "after commit but before the receipt reaches the caller" kill
    point."""


async def _pause_before_commit_barrier() -> None:
    """No-op by default, awaited. F-writer/T087's clear-quiescence pause point: failure-
    injection tests monkeypatch this with an async function to pause an in-flight `ingest()`
    at its commit barrier — while it still holds `store.write_lock` and counts as an in-flight
    writer — so a concurrent `clear_history()` can be exercised against it (Failure-Injection.md
    F-writer; data-model.md Snapshot/Generation lifecycle case 3)."""


def _validate_batch(messages: list[InputMessage]) -> None:
    """Validate the complete bounded batch before opening its write transaction (FR-002).
    Batch limits quoted verbatim (data-model.md): "<= 1,000 messages and <= 8 MiB encoded
    JSON per batch, <= 256 KiB per individual record."
    """
    if not messages:
        raise InputValidationError("ingest() requires at least one message")
    if len(messages) > MAX_MESSAGES_PER_BATCH:
        raise InputValidationError(
            f"batch has {len(messages)} messages, exceeds the {MAX_MESSAGES_PER_BATCH} limit"
        )

    total_bytes = 0
    for i, m in enumerate(messages):
        if m.role not in SUPPORTED_ROLES:
            raise InputValidationError(
                f"message index {i} has unsupported role {m.role!r}; supported roles are "
                f"{sorted(SUPPORTED_ROLES)}"
            )
        # Measured on the same serialized payload that gets stored/hashed, not a repr of
        # just `content` — the limit is "encoded JSON per record" (data-model.md), and the
        # record includes role/metadata/tool_calls too.
        record_bytes = len(canonical_payload_json(message_payload(m)).encode("utf-8"))
        if record_bytes > MAX_RECORD_BYTES:
            raise InputValidationError(
                f"message index {i} is {record_bytes} bytes, exceeds the {MAX_RECORD_BYTES} "
                "per-record limit"
            )
        total_bytes += record_bytes
    if total_bytes > MAX_BATCH_BYTES:
        raise InputValidationError(
            f"batch is {total_bytes} bytes, exceeds the {MAX_BATCH_BYTES} batch limit"
        )


async def ingest(
    store: HistoryStore,
    history_id: str,
    messages: list[InputMessage],
    source_id: str,
    idempotency_key: str,
    session_metadata: dict[str, Any] | None = None,
) -> IngestReceipt:
    """No model call is part of this operation (constitution Principle V)."""
    _validate_batch(messages)
    request_hash = compute_request_hash(messages, source_id, ADAPTER_VERSION, session_metadata)

    await store.begin_write()
    try:
        async with store.write_lock:
            return await _ingest_locked(
                store, history_id, messages, source_id, idempotency_key, session_metadata,
                request_hash,
            )
    finally:
        await store.end_write()


async def _ingest_locked(
    store: HistoryStore,
    history_id: str,
    messages: list[InputMessage],
    source_id: str,
    idempotency_key: str,
    session_metadata: dict[str, Any] | None,
    request_hash: str,
) -> IngestReceipt:
    connection = store.connection

    # Check order (CHK002): IdempotencyConflict (batch/receipt-level) before any
    # per-message MessageConflict check. Runs inside write_lock so a same-key race between
    # two concurrent ingest() calls cannot both pass this check.
    cursor = await connection.execute(
        "SELECT request_hash, inserted_seq_start, inserted_seq_end, skipped_count, "
        "unsupported_block_count, indexing_status FROM ingest_receipts "
        "WHERE history_id = ? AND source_id = ? AND idempotency_key = ?",
        (history_id, source_id, idempotency_key),
    )
    existing = await fetchone(cursor)
    if existing is not None:
        existing_hash = existing[0]
        if existing_hash == request_hash:
            return IngestReceipt(
                history_id=history_id,
                source_id=source_id,
                idempotency_key=idempotency_key,
                request_hash=existing_hash,
                inserted_seq_start=existing[1],
                inserted_seq_end=existing[2],
                skipped_count=existing[3],
                unsupported_block_count=existing[4],
                indexing_status=existing[5],
                replayed=True,
            )
        raise IdempotencyConflict(
            f"idempotency_key {idempotency_key!r} was already used for a different request "
            f"(history_id={history_id!r}, source_id={source_id!r})"
        )

    # Per-message MessageConflict check: an existing external_id with changed content.
    for m in messages:
        if m.external_id is None:
            continue
        cursor = await connection.execute(
            "SELECT original_payload FROM messages "
            "WHERE history_id = ? AND source_id = ? AND external_id = ?",
            (history_id, source_id, m.external_id),
        )
        row = await fetchone(cursor)
        if row is not None:
            existing_payload = row[0]
            new_payload_str = canonical_payload_json(message_payload(m))
            if existing_payload != new_payload_str:
                raise MessageConflict(
                    f"external_id {m.external_id!r} already exists with different content "
                    f"(history_id={history_id!r}, source_id={source_id!r})"
                )

    try:
        # Atomic commit: messages + message_fts + receipt + history_revision bump.
        async with connection:
            cursor = await connection.execute(
                "SELECT seq_high_water_mark FROM store_meta WHERE id = 1"
            )
            row = await fetchone(cursor)
            next_seq = (row[0] if row else 0) + 1

            seq_start = next_seq
            inserted = 0
            skipped = 0
            for idx, m in enumerate(messages):
                existing_identity = None
                if m.external_id is not None:
                    cursor = await connection.execute(
                        "SELECT message_id FROM messages "
                        "WHERE history_id = ? AND source_id = ? AND external_id = ?",
                        (history_id, source_id, m.external_id),
                    )
                    existing_identity = await fetchone(cursor)
                if existing_identity is not None:
                    # Identical existing message (content already verified equal above) is
                    # skipped, not re-inserted (FR-002).
                    skipped += 1
                    continue

                payload = message_payload(m)
                payload_str = canonical_payload_json(payload)
                phash = compute_payload_hash(payload)
                message_id = prefixed_id("m")
                text_projection = render_text_projection(m.content)
                await connection.execute(
                    "INSERT INTO messages (message_id, seq, history_id, source_id, external_id, "
                    "idempotency_key, position_in_batch, role, original_payload, "
                    "text_projection, payload_hash, session_metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        message_id,
                        next_seq,
                        history_id,
                        source_id,
                        m.external_id,
                        idempotency_key if m.external_id is None else None,
                        idx if m.external_id is None else None,
                        m.role,
                        payload_str,
                        text_projection,
                        phash,
                        _json_or_none(session_metadata),
                        time.time(),
                    ),
                )
                if text_projection is not None:
                    await connection.execute(
                        "INSERT INTO message_fts (message_id, history_id, text) VALUES (?, ?, ?)",
                        (message_id, history_id, text_projection),
                    )
                next_seq += 1
                inserted += 1

            seq_end = next_seq - 1 if inserted else None

            await connection.execute(
                "INSERT INTO ingest_receipts (history_id, source_id, idempotency_key, "
                "request_hash, inserted_seq_start, inserted_seq_end, skipped_count, "
                "unsupported_block_count, indexing_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    history_id,
                    source_id,
                    idempotency_key,
                    request_hash,
                    seq_start if inserted else None,
                    seq_end,
                    skipped,
                    0,
                    "pending",
                ),
            )
            await connection.execute(
                "UPDATE store_meta SET history_revision = history_revision + 1, "
                "seq_high_water_mark = ? WHERE id = 1",
                (next_seq - 1,),
            )

            _before_commit_barrier()
            await _pause_before_commit_barrier()
    except apsw.Error as exc:
        raise map_storage_error(exc) from exc

    _after_commit_barrier()

    return IngestReceipt(
        history_id=history_id,
        source_id=source_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        inserted_seq_start=seq_start if inserted else None,
        inserted_seq_end=seq_end,
        skipped_count=skipped,
        unsupported_block_count=0,
        indexing_status="pending",
        replayed=False,
    )


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"))
