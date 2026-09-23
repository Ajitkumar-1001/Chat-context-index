/**
 * clearHistory() (contracts/operations.md `clear_history()`; data-model.md Snapshot/Generation
 * lifecycle case 3) — mirrors clear.py's local half. The Redis-purge subcase is Python-only for
 * this release (FR-008/cache modes are M3, Python-only per the task's Milestone Applicability
 * Matrix — TypeScript parity's own scope, FR-011, does not require porting the cache backend).
 */

import { BudgetExceeded, InputValidationError } from "./errors.js";
import { HistoryStore } from "./store.js";
import { mapStorageError } from "./ioWorker.js";

const CLEARED_TABLES = [
  "messages", "message_fts", "summary_fts", "ingest_receipts", "exchanges", "chunks", "nodes", "node_chunks",
];

export interface ClearReport {
  logicalClearComplete: boolean;
  cachePurgePending: boolean;
}

export async function clearHistory(store: HistoryStore, expectedHistoryId: string): Promise<ClearReport> {
  if (expectedHistoryId !== store.historyId) {
    throw new InputValidationError(
      `expected_history_id ${expectedHistoryId} does not match the open store's history_id ` +
        `${store.historyId} — not treated as a no-op`,
    );
  }

  store.closeWriteGate();
  try {
    const drained = await store.waitForWritersToDrain(store.config.clearHistoryQuiescenceDeadlineS);
    if (!drained) {
      throw new BudgetExceeded(
        `clear_history() quiescence deadline (${store.config.clearHistoryQuiescenceDeadlineS}s) ` +
          "exceeded waiting for in-flight local writers to commit; the write(s) continue unaffected",
      );
    }

    await store.withLock(async () => {
      const io = store.connection;
      try {
        await io.exec("BEGIN IMMEDIATE");
        for (const table of CLEARED_TABLES) {
          await io.exec(`DELETE FROM ${table}`);
        }
        await io.exec(
          "UPDATE store_meta SET cache_generation = cache_generation + 1, " +
            "history_revision = history_revision + 1, index_revision = index_revision + 1, " +
            "index_committed_seq = 0, index_pending_seq = 0 WHERE id = 1",
        );
        await io.exec("COMMIT");
      } catch (err) {
        await io.exec("ROLLBACK").catch(() => undefined);
        throw mapStorageError(err as { message: string; code?: string });
      }
    });
  } finally {
    store.openWriteGate();
  }

  return { logicalClearComplete: true, cachePurgePending: false };
}
