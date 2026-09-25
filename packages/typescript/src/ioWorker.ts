/**
 * Bounded, owned I/O worker (plan.md Summary; contracts/operations.md `open()` Ownership) —
 * main-thread-side wrapper around one dbWorker.ts worker thread. This module owns forwarding
 * calls to that thread and provides the fetch helpers the rest of the package uses, so every
 * other module works with plain values, never a raw Worker message.
 *
 * Sequential awaits from the caller (every operation module here awaits each call before
 * issuing the next) preserve transaction atomicity across `exec("BEGIN")` .. `exec("COMMIT")`
 * pairs of messages, since the worker's message queue is FIFO and this package's single-writer
 * design (one HistoryStore, one in-flight write at a time via `writeLock`) never interleaves two
 * transactions on the same worker.
 */

import { Worker } from "node:worker_threads";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { CciError, StoreBusy, StoreCorrupt, StoreError } from "./errors.js";

const WORKER_PATH = path.join(path.dirname(fileURLToPath(import.meta.url)), "dbWorker.js");

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
}

/** Maps a raw better-sqlite3 error to the fixed 15-code taxonomy — mirrors store.py's
 * `map_storage_error`. Used so a caller never sees a raw sqlite error code as-is. */
export function mapStorageError(err: { message: string; code?: string }): Error {
  if (err instanceof CciError) return err;
  const msg = err.message ?? String(err);
  const code = err.code ?? "";
  if (code === "SQLITE_BUSY" || /database is locked/i.test(msg)) return new StoreBusy(msg);
  // Extended codes (e.g. SQLITE_CORRUPT_VTAB from FTS5) are corruption too, as apsw.CorruptError treats them.
  if (code.startsWith("SQLITE_CORRUPT") || code === "SQLITE_NOTADB" || /malformed|not a database/i.test(msg)) {
    return new StoreCorrupt(msg);
  }
  return new StoreError(msg);
}

export class IOWorker {
  private worker: Worker;
  private nextId = 1;
  private pending = new Map<number, PendingRequest>();
  private closed = false;

  private constructor(worker: Worker) {
    this.worker = worker;
    this.worker.on("message", (msg: { id: number; result?: unknown; error?: { message: string; code?: string } }) => {
      const pending = this.pending.get(msg.id);
      if (!pending) return;
      this.pending.delete(msg.id);
      if (msg.error) pending.reject(mapStorageError(msg.error));
      else pending.resolve(msg.result);
    });
  }

  static async open(dbPath: string, options: { readonly?: boolean; fileMustExist?: boolean } = {}): Promise<IOWorker> {
    const worker = new Worker(WORKER_PATH, { workerData: { path: dbPath, options } });
    try {
      await new Promise<void>((resolve, reject) => {
        const fail = (error: Error) => reject(mapStorageError(error));
        worker.once("error", fail);
        worker.once("message", () => { worker.off("error", fail); resolve(); });
      });
    } catch (error) {
      await worker.terminate();
      throw error;
    }
    return new IOWorker(worker);
  }

  private send(type: "exec" | "run" | "all" | "get" | "close", sql?: string, params?: unknown[]): Promise<unknown> {
    if (this.closed) return Promise.reject(new StoreError("IOWorker is closed"));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage({ id, type, sql, params });
    });
  }

  exec(sql: string): Promise<void> {
    return this.send("exec", sql) as Promise<void>;
  }

  run(sql: string, params: unknown[] = []): Promise<{ changes: number; lastInsertRowid: number }> {
    return this.send("run", sql, params) as Promise<{ changes: number; lastInsertRowid: number }>;
  }

  all<T = Record<string, unknown>>(sql: string, params: unknown[] = []): Promise<T[]> {
    return this.send("all", sql, params) as Promise<T[]>;
  }

  get<T = Record<string, unknown>>(sql: string, params: unknown[] = []): Promise<T | null> {
    return this.send("get", sql, params) as Promise<T | null>;
  }

  /** Safe to call repeatedly (AT-17). */
  async close(): Promise<void> {
    if (this.closed) return;
    try {
      await this.send("close");
    } finally {
      this.closed = true;
      await this.worker.terminate();
    }
  }
}
