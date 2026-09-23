"""export() (contracts/operations.md `export()`/`import_history()`; data-model.md Import
validation).

Bounded-stream JSONL: one committed snapshot (a single short read transaction captures the
range up front, then streams — no long-held read transaction), a checksum manifest as the
trailing line. Excludes secrets, memo entries, and private diagnostics by construction — the
memo cache lives in a separate SQLite file/Redis entirely (data-model.md Cache-scoped
entities), never touched here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import apsw

from .io_worker import fetchall
from .store import HistoryStore, map_storage_error

EXPORT_FORMAT_VERSION = 1


@dataclass(frozen=True)
class ExportManifest:
    cci_export_version: int
    history_id: str
    message_count: int
    checksum: str


def _message_row_to_record(row: tuple) -> dict:
    (
        message_id, seq, source_id, external_id, idempotency_key, position_in_batch,
        role, original_payload, text_projection, payload_hash, session_metadata, created_at,
    ) = row
    return {
        "record_type": "message",
        "message_id": message_id,
        "seq": seq,
        "source_id": source_id,
        "external_id": external_id,
        "idempotency_key": idempotency_key,
        "position_in_batch": position_in_batch,
        "role": role,
        "original_payload": json.loads(original_payload),
        "text_projection": text_projection,
        "payload_hash": payload_hash,
        "session_metadata": json.loads(session_metadata) if session_metadata else None,
        "created_at": created_at,
    }


async def export(store: HistoryStore, dest_path: str) -> ExportManifest:
    """Streams `store`'s current committed history to `dest_path` as JSONL, one message
    record per line, followed by a trailing manifest line. `write_lock` serializes this read
    transaction against the store's single AsyncConnection like every other transaction here —
    a second concurrent `async with connection:` would otherwise fail
    ("transaction within a transaction")."""
    async with store.write_lock:
        try:
            async with store.connection:
                cursor = await store.connection.execute(
                    "SELECT message_id, seq, source_id, external_id, idempotency_key, "
                    "position_in_batch, role, original_payload, text_projection, "
                    "payload_hash, session_metadata, created_at FROM messages "
                    "WHERE history_id = ? ORDER BY seq",
                    (store.history_id,),
                )
                rows = await fetchall(cursor)
        except apsw.Error as exc:
            raise map_storage_error(exc) from exc

    hasher = hashlib.sha256()
    with open(dest_path, "w", encoding="utf-8") as f:
        for row in rows:
            line = json.dumps(_message_row_to_record(row), sort_keys=True, separators=(",", ":"))
            hasher.update(line.encode("utf-8"))
            f.write(line + "\n")

        manifest = ExportManifest(
            cci_export_version=EXPORT_FORMAT_VERSION,
            history_id=store.history_id,
            message_count=len(rows),
            checksum=hasher.hexdigest(),
        )
        f.write(
            json.dumps(
                {
                    "record_type": "manifest",
                    "cci_export_version": manifest.cci_export_version,
                    "history_id": manifest.history_id,
                    "message_count": manifest.message_count,
                    "checksum": manifest.checksum,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )

    return manifest
