/**
 * export() (contracts/operations.md `export()`/`import_history()`) — mirrors export.py.
 * Bounded-stream JSONL: one committed snapshot, a checksum manifest as the trailing line.
 */

import { createHash } from "node:crypto";
import { writeFileSync } from "node:fs";
import { HistoryStore } from "./store.js";
import { mapStorageError } from "./ioWorker.js";

export const EXPORT_FORMAT_VERSION = 1;

export interface ExportManifest {
  cciExportVersion: number;
  historyId: string;
  messageCount: number;
  checksum: string;
}

function messageRowToRecord(row: Record<string, unknown>): Record<string, unknown> {
  return {
    record_type: "message",
    message_id: row.message_id,
    seq: row.seq,
    source_id: row.source_id,
    external_id: row.external_id ?? null,
    idempotency_key: row.idempotency_key ?? null,
    position_in_batch: row.position_in_batch ?? null,
    role: row.role,
    original_payload: JSON.parse(row.original_payload as string),
    text_projection: row.text_projection ?? null,
    payload_hash: row.payload_hash,
    session_metadata: row.session_metadata ? JSON.parse(row.session_metadata as string) : null,
    created_at: row.created_at,
  };
}

function canonicalLine(obj: Record<string, unknown>): string {
  const sorted: Record<string, unknown> = {};
  for (const key of Object.keys(obj).sort()) sorted[key] = obj[key];
  return JSON.stringify(sorted);
}

export async function exportHistory(store: HistoryStore, destPath: string): Promise<ExportManifest> {
  let rows: Record<string, unknown>[];
  try {
    rows = await store.withLock(() =>
      store.connection.all<Record<string, unknown>>(
        "SELECT message_id, seq, source_id, external_id, idempotency_key, position_in_batch, " +
          "role, original_payload, text_projection, payload_hash, session_metadata, created_at " +
          "FROM messages WHERE history_id = ? ORDER BY seq",
        [store.historyId],
      ),
    );
  } catch (err) {
    throw mapStorageError(err as { message: string; code?: string });
  }

  const hasher = createHash("sha256");
  const lines: string[] = [];
  for (const row of rows) {
    const line = canonicalLine(messageRowToRecord(row));
    hasher.update(line, "utf-8");
    lines.push(line);
  }

  const manifest: ExportManifest = {
    cciExportVersion: EXPORT_FORMAT_VERSION,
    historyId: store.historyId,
    messageCount: rows.length,
    checksum: hasher.digest("hex"),
  };
  lines.push(
    canonicalLine({
      record_type: "manifest",
      cci_export_version: manifest.cciExportVersion,
      history_id: manifest.historyId,
      message_count: manifest.messageCount,
      checksum: manifest.checksum,
    }),
  );

  writeFileSync(destPath, lines.join("\n") + "\n", "utf-8");
  return manifest;
}
