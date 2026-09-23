/**
 * ingest() (contracts/operations.md `ingest()`; spec/normalization.md) — mirrors ingest.py.
 *
 * Check order (spec/normalization.md, resolving CHK002): the batch/receipt-level
 * IdempotencyConflict check runs before any per-message MessageConflict check.
 */

import { IdempotencyConflict, InputValidationError, MessageConflict } from "./errors.js";
import { prefixedId } from "./ids.js";
import {
  IngestReceipt,
  InputMessage,
  MAX_BATCH_BYTES,
  MAX_MESSAGES_PER_BATCH,
  MAX_RECORD_BYTES,
  SUPPORTED_ROLES,
  canonicalPayloadJson,
  computeRequestHash,
  messagePayload,
  payloadHash as computePayloadHash,
  renderTextProjection,
} from "./models.js";
import { HistoryStore } from "./store.js";
import { mapStorageError } from "./ioWorker.js";

const ADAPTER_VERSION = "native-1";

function validateBatch(messages: InputMessage[]): void {
  if (messages.length === 0) throw new InputValidationError("ingest() requires at least one message");
  if (messages.length > MAX_MESSAGES_PER_BATCH) {
    throw new InputValidationError(
      `batch has ${messages.length} messages, exceeds the ${MAX_MESSAGES_PER_BATCH} limit`,
    );
  }
  let totalBytes = 0;
  messages.forEach((m, i) => {
    if (!SUPPORTED_ROLES.has(m.role)) {
      throw new InputValidationError(
        `message index ${i} has unsupported role ${m.role}; supported roles are ` +
          `${[...SUPPORTED_ROLES].sort().join(", ")}`,
      );
    }
    const recordBytes = Buffer.byteLength(canonicalPayloadJson(messagePayload(m)), "utf-8");
    if (recordBytes > MAX_RECORD_BYTES) {
      throw new InputValidationError(
        `message index ${i} is ${recordBytes} bytes, exceeds the ${MAX_RECORD_BYTES} per-record limit`,
      );
    }
    totalBytes += recordBytes;
  });
  if (totalBytes > MAX_BATCH_BYTES) {
    throw new InputValidationError(`batch is ${totalBytes} bytes, exceeds the ${MAX_BATCH_BYTES} batch limit`);
  }
}

export async function ingest(
  store: HistoryStore,
  historyId: string,
  messages: InputMessage[],
  sourceId: string,
  idempotencyKey: string,
  sessionMetadata?: Record<string, unknown> | null,
): Promise<IngestReceipt> {
  validateBatch(messages);
  const requestHash = computeRequestHash(messages, sourceId, ADAPTER_VERSION, sessionMetadata);

  await store.beginWrite();
  try {
    return await store.withLock(() =>
      ingestLocked(store, historyId, messages, sourceId, idempotencyKey, sessionMetadata ?? null, requestHash),
    );
  } finally {
    store.endWrite();
  }
}

async function ingestLocked(
  store: HistoryStore,
  historyId: string,
  messages: InputMessage[],
  sourceId: string,
  idempotencyKey: string,
  sessionMetadata: Record<string, unknown> | null,
  requestHash: string,
): Promise<IngestReceipt> {
  const io = store.connection;

  const existing = await io.get<{
    request_hash: string;
    inserted_seq_start: number | null;
    inserted_seq_end: number | null;
    skipped_count: number;
    unsupported_block_count: number;
    indexing_status: string;
  }>(
    "SELECT request_hash, inserted_seq_start, inserted_seq_end, skipped_count, " +
      "unsupported_block_count, indexing_status FROM ingest_receipts " +
      "WHERE history_id = ? AND source_id = ? AND idempotency_key = ?",
    [historyId, sourceId, idempotencyKey],
  );
  if (existing) {
    if (existing.request_hash === requestHash) {
      return {
        historyId,
        sourceId,
        idempotencyKey,
        requestHash: existing.request_hash,
        insertedSeqStart: existing.inserted_seq_start,
        insertedSeqEnd: existing.inserted_seq_end,
        skippedCount: existing.skipped_count,
        unsupportedBlockCount: existing.unsupported_block_count,
        indexingStatus: existing.indexing_status,
        replayed: true,
      };
    }
    throw new IdempotencyConflict(
      `idempotency_key ${idempotencyKey} was already used for a different request ` +
        `(history_id=${historyId}, source_id=${sourceId})`,
    );
  }

  for (const m of messages) {
    if (m.externalId == null) continue;
    const row = await io.get<{ original_payload: string }>(
      "SELECT original_payload FROM messages WHERE history_id = ? AND source_id = ? AND external_id = ?",
      [historyId, sourceId, m.externalId],
    );
    if (row) {
      const newPayloadStr = canonicalPayloadJson(messagePayload(m));
      if (row.original_payload !== newPayloadStr) {
        throw new MessageConflict(
          `external_id ${m.externalId} already exists with different content ` +
            `(history_id=${historyId}, source_id=${sourceId})`,
        );
      }
    }
  }

  let seqStart = 0;
  let seqEnd: number | null = null;
  let inserted = 0;
  let skipped = 0;

  try {
    await io.exec("BEGIN IMMEDIATE");

    const highWaterRow = await io.get<{ seq_high_water_mark: number }>(
      "SELECT seq_high_water_mark FROM store_meta WHERE id = 1",
    );
    let nextSeq = (highWaterRow?.seq_high_water_mark ?? 0) + 1;
    seqStart = nextSeq;

    for (let idx = 0; idx < messages.length; idx++) {
      const m = messages[idx];
      let existingIdentity: unknown = null;
      if (m.externalId != null) {
        existingIdentity = await io.get(
          "SELECT message_id FROM messages WHERE history_id = ? AND source_id = ? AND external_id = ?",
          [historyId, sourceId, m.externalId],
        );
      }
      if (existingIdentity) {
        skipped++;
        continue;
      }

      const payload = messagePayload(m);
      const payloadStr = canonicalPayloadJson(payload);
      const phash = computePayloadHash(payload);
      const messageId = prefixedId("m");
      const textProjection = renderTextProjection(m.content);

      await io.run(
        "INSERT INTO messages (message_id, seq, history_id, source_id, external_id, " +
          "idempotency_key, position_in_batch, role, original_payload, text_projection, " +
          "payload_hash, session_metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
          messageId,
          nextSeq,
          historyId,
          sourceId,
          m.externalId ?? null,
          m.externalId == null ? idempotencyKey : null,
          m.externalId == null ? idx : null,
          m.role,
          payloadStr,
          textProjection,
          phash,
          sessionMetadata ? JSON.stringify(sessionMetadata) : null,
          Date.now() / 1000,
        ],
      );
      if (textProjection != null) {
        await io.run("INSERT INTO message_fts (message_id, history_id, text) VALUES (?, ?, ?)", [
          messageId,
          historyId,
          textProjection,
        ]);
      }
      nextSeq++;
      inserted++;
    }

    seqEnd = inserted > 0 ? nextSeq - 1 : null;

    await io.run(
      "INSERT INTO ingest_receipts (history_id, source_id, idempotency_key, request_hash, " +
        "inserted_seq_start, inserted_seq_end, skipped_count, unsupported_block_count, " +
        "indexing_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
      [
        historyId,
        sourceId,
        idempotencyKey,
        requestHash,
        inserted > 0 ? seqStart : null,
        seqEnd,
        skipped,
        0,
        "pending",
      ],
    );
    await io.run("UPDATE store_meta SET history_revision = history_revision + 1, seq_high_water_mark = ? WHERE id = 1", [
      nextSeq - 1,
    ]);

    await io.exec("COMMIT");
  } catch (err) {
    await io.exec("ROLLBACK").catch(() => undefined);
    throw mapStorageError(err as { message: string; code?: string });
  }

  return {
    historyId,
    sourceId,
    idempotencyKey,
    requestHash,
    insertedSeqStart: inserted > 0 ? seqStart : null,
    insertedSeqEnd: seqEnd,
    skippedCount: skipped,
    unsupportedBlockCount: 0,
    indexingStatus: "pending",
    replayed: false,
  };
}
