"""import_history() (contracts/operations.md `import_history()`; data-model.md Import
validation).

Import validation (data-model.md, quoted verbatim): "`import_history()` validates the manifest
against the actual record counts and checksums before treating any record as committed. A
manifest/record-count mismatch raises `InputValidationError`." `StoreNotEmpty` on a non-empty
target, never a silent merge. The destination's `history_id`/`store_instance_id`/
`cache_generation` are the ones the target `HistoryStore` already got from its own `open()` —
never the source file's original `history_id` (FR-009): original message IDs, original strings,
source identities, and sequence order are preserved; only the enclosing history identity is new.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import apsw

from .errors import InputValidationError, StoreNotEmpty
from .export import EXPORT_FORMAT_VERSION
from .io_worker import fetchall
from .store import HistoryStore, map_storage_error

_IMPORT_BATCH_SIZE = 500

_REQUIRED_MESSAGE_FIELDS = (
    "message_id", "seq", "source_id", "external_id", "idempotency_key", "position_in_batch",
    "role", "original_payload", "text_projection", "payload_hash", "session_metadata",
    "created_at",
)


@dataclass(frozen=True)
class ImportReport:
    history_id: str
    imported_count: int
    status: str  # "complete" | "partial"


def _read_records(src_path: str) -> tuple[list[dict], dict]:
    """Reads and validates the manifest against actual counts/checksums before any record is
    treated as committed (data-model.md Import validation). Returns (message_records,
    manifest_record)."""
    import hashlib

    with open(src_path, encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f if line.strip()]

    if not lines:
        raise InputValidationError("import source is empty — no manifest record found")

    *message_lines, manifest_line = lines
    try:
        manifest = json.loads(manifest_line)
    except json.JSONDecodeError as exc:
        raise InputValidationError(f"malformed manifest record: {exc}") from exc

    if manifest.get("record_type") != "manifest":
        raise InputValidationError("last record is not a manifest record")
    if manifest.get("cci_export_version") != EXPORT_FORMAT_VERSION:
        raise InputValidationError(
            f"unsupported export version {manifest.get('cci_export_version')!r}, expected "
            f"{EXPORT_FORMAT_VERSION}"
        )

    hasher = hashlib.sha256()
    records: list[dict] = []
    for line in message_lines:
        hasher.update(line.encode("utf-8"))
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InputValidationError(f"malformed message record: {exc}") from exc
        if record.get("record_type") != "message":
            raise InputValidationError(f"unexpected record_type {record.get('record_type')!r}")
        missing = [f for f in _REQUIRED_MESSAGE_FIELDS if f not in record]
        if missing:
            raise InputValidationError(
                f"message record is missing required field(s): {missing}"
            )
        records.append(record)

    if manifest.get("message_count") != len(records):
        raise InputValidationError(
            f"manifest declares {manifest.get('message_count')} records, found {len(records)}"
        )
    if manifest.get("checksum") != hasher.hexdigest():
        raise InputValidationError("manifest checksum does not match the actual record content")

    return records, manifest


async def import_history(store: HistoryStore, src_path: str) -> ImportReport:
    cursor = await store.connection.execute(
        "SELECT COUNT(*) FROM messages WHERE history_id = ?", (store.history_id,)
    )
    (existing_count,) = (await fetchall(cursor))[0]
    if existing_count > 0:
        raise StoreNotEmpty(
            f"import_history() target already has {existing_count} message(s) — "
            "no silent merge"
        )

    # Validate the manifest against actual record counts/checksums BEFORE any commit (E3).
    records, _manifest = _read_records(src_path)

    await store.begin_write()
    imported = 0
    try:
        async with store.write_lock:
            connection = store.connection
            try:
                for batch_start in range(0, len(records), _IMPORT_BATCH_SIZE):
                    batch = records[batch_start:batch_start + _IMPORT_BATCH_SIZE]
                    async with connection:
                        max_seq = 0
                        for record in batch:
                            payload_str = json.dumps(
                                record["original_payload"], sort_keys=True, separators=(",", ":")
                            )
                            text_projection = record.get("text_projection")
                            await connection.execute(
                                "INSERT INTO messages (message_id, seq, history_id, source_id, "
                                "external_id, idempotency_key, position_in_batch, role, "
                                "original_payload, text_projection, payload_hash, "
                                "session_metadata, created_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    record["message_id"],
                                    record["seq"],
                                    store.history_id,
                                    record["source_id"],
                                    record["external_id"],
                                    record["idempotency_key"],
                                    record["position_in_batch"],
                                    record["role"],
                                    payload_str,
                                    text_projection,
                                    record["payload_hash"],
                                    json.dumps(
                                        record["session_metadata"], sort_keys=True,
                                        separators=(",", ":"),
                                    )
                                    if record.get("session_metadata") is not None else None,
                                    record["created_at"],
                                ),
                            )
                            if text_projection is not None:
                                await connection.execute(
                                    "INSERT INTO message_fts (message_id, history_id, text) "
                                    "VALUES (?, ?, ?)",
                                    (record["message_id"], store.history_id, text_projection),
                                )
                            max_seq = max(max_seq, record["seq"])
                            imported += 1
                        await connection.execute(
                            "UPDATE store_meta SET history_revision = history_revision + 1, "
                            "seq_high_water_mark = MAX(seq_high_water_mark, ?) WHERE id = 1",
                            (max_seq,),
                        )
            except apsw.Error as exc:
                # E4: truthfully report committed progress — earlier batches already committed
                # (separate transactions) stay committed; never claim a whole-file rollback.
                raise map_storage_error(exc) from exc
    finally:
        await store.end_write()

    return ImportReport(history_id=store.history_id, imported_count=imported, status="complete")
