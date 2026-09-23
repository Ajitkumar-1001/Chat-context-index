/**
 * import_history() (contracts/operations.md `import_history()`; data-model.md Import
 * validation) — mirrors import_history.py. Validates the manifest against actual record
 * counts/checksums before treating any record as committed. `StoreNotEmpty` on a non-empty
 * target, never a silent merge. Assigns the target's own history_id (never the source file's).
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { InputValidationError, StoreNotEmpty } from "./errors.js";
import { EXPORT_FORMAT_VERSION } from "./export.js";
import { HistoryStore } from "./store.js";
import { mapStorageError } from "./ioWorker.js";

const IMPORT_BATCH_SIZE = 500;
const REQUIRED_MESSAGE_FIELDS = [
  "message_id", "seq", "source_id", "external_id", "idempotency_key", "position_in_batch",
  "role", "original_payload", "text_projection", "payload_hash", "session_metadata", "created_at",
];

export interface ImportReport {
  historyId: string;
  importedCount: number;
  status: string;
}

function readRecords(srcPath: string): [Record<string, unknown>[], Record<string, unknown>] {
  const lines = readFileSync(srcPath, "utf-8")
    .split("\n")
    .map((l) => l.trimEnd())
    .filter((l) => l.length > 0);
  if (lines.length === 0) throw new InputValidationError("import source is empty — no manifest record found");

  const manifestLine = lines[lines.length - 1];
  const messageLines = lines.slice(0, -1);

  let manifest: Record<string, unknown>;
  try {
    manifest = JSON.parse(manifestLine);
  } catch (e) {
    throw new InputValidationError(`malformed manifest record: ${(e as Error).message}`);
  }
  if (manifest.record_type !== "manifest") throw new InputValidationError("last record is not a manifest record");
  if (manifest.cci_export_version !== EXPORT_FORMAT_VERSION) {
    throw new InputValidationError(
      `unsupported export version ${manifest.cci_export_version}, expected ${EXPORT_FORMAT_VERSION}`,
    );
  }

  const hasher = createHash("sha256");
  const records: Record<string, unknown>[] = [];
  for (const line of messageLines) {
    hasher.update(line, "utf-8");
    let record: Record<string, unknown>;
    try {
      record = JSON.parse(line);
    } catch (e) {
      throw new InputValidationError(`malformed message record: ${(e as Error).message}`);
    }
    if (record.record_type !== "message") {
      throw new InputValidationError(`unexpected record_type ${record.record_type}`);
    }
    const missing = REQUIRED_MESSAGE_FIELDS.filter((f) => !(f in record));
    if (missing.length > 0) {
      throw new InputValidationError(`message record is missing required field(s): ${missing.join(", ")}`);
    }
    records.push(record);
  }

  if (manifest.message_count !== records.length) {
    throw new InputValidationError(`manifest declares ${manifest.message_count} records, found ${records.length}`);
  }
  if (manifest.checksum !== hasher.digest("hex")) {
    throw new InputValidationError("manifest checksum does not match the actual record content");
  }

  return [records, manifest];
}

export async function importHistory(store: HistoryStore, srcPath: string): Promise<ImportReport> {
  const existingRow = await store.connection.get<{ cnt: number }>(
    "SELECT COUNT(*) as cnt FROM messages WHERE history_id = ?",
    [store.historyId],
  );
  if ((existingRow?.cnt ?? 0) > 0) {
    throw new StoreNotEmpty(
      `import_history() target already has ${existingRow?.cnt} message(s) — no silent merge`,
    );
  }

  const [records] = readRecords(srcPath);

  await store.beginWrite();
  let imported = 0;
  try {
    await store.withLock(async () => {
      const io = store.connection;
      try {
        for (let batchStart = 0; batchStart < records.length; batchStart += IMPORT_BATCH_SIZE) {
          const batch = records.slice(batchStart, batchStart + IMPORT_BATCH_SIZE);
          await io.exec("BEGIN IMMEDIATE");
          let maxSeq = 0;
          for (const record of batch) {
            const payloadStr = JSON.stringify(record.original_payload);
            const textProjection = (record.text_projection as string | null) ?? null;
            await io.run(
              "INSERT INTO messages (message_id, seq, history_id, source_id, external_id, " +
                "idempotency_key, position_in_batch, role, original_payload, text_projection, " +
                "payload_hash, session_metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              [
                record.message_id,
                record.seq,
                store.historyId,
                record.source_id,
                record.external_id ?? null,
                record.idempotency_key ?? null,
                record.position_in_batch ?? null,
                record.role,
                payloadStr,
                textProjection,
                record.payload_hash,
                record.session_metadata != null ? JSON.stringify(record.session_metadata) : null,
                record.created_at,
              ],
            );
            if (textProjection != null) {
              await io.run("INSERT INTO message_fts (message_id, history_id, text) VALUES (?, ?, ?)", [
                record.message_id,
                store.historyId,
                textProjection,
              ]);
            }
            maxSeq = Math.max(maxSeq, record.seq as number);
            imported++;
          }
          await io.run(
            "UPDATE store_meta SET history_revision = history_revision + 1, " +
              "seq_high_water_mark = MAX(seq_high_water_mark, ?) WHERE id = 1",
            [maxSeq],
          );
          await io.exec("COMMIT");
        }
      } catch (err) {
        await io.exec("ROLLBACK").catch(() => undefined);
        throw mapStorageError(err as { message: string; code?: string });
      }
    });
  } finally {
    store.endWrite();
  }

  return { historyId: store.historyId, importedCount: imported, status: "complete" };
}
