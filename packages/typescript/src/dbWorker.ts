/**
 * Dedicated worker-thread body — owns one better-sqlite3 connection (WiseLibs/better-sqlite3
 * docs/threads.md documented pattern: keep synchronous SQLite I/O off the main event loop by
 * running it in a worker thread; ioWorker.ts is the main-thread-side pool of exactly one such
 * worker per HistoryStore, forwarding calls to it — the TypeScript counterpart to io_worker.py's
 * APSW-async-backed worker).
 */

import { parentPort, workerData } from "node:worker_threads";
import Database from "better-sqlite3";

interface Request {
  id: number;
  type: "exec" | "run" | "all" | "get" | "close";
  sql?: string;
  params?: unknown[];
}

if (!parentPort) throw new Error("dbWorker must be run as a worker_thread");

const db = new Database(workerData.path as string);

parentPort.on("message", (req: Request) => {
  try {
    let result: unknown;
    switch (req.type) {
      case "exec":
        db.exec(req.sql!);
        result = null;
        break;
      case "run": {
        const info = db.prepare(req.sql!).run(...(req.params ?? []));
        result = { changes: info.changes, lastInsertRowid: Number(info.lastInsertRowid) };
        break;
      }
      case "all":
        result = db.prepare(req.sql!).all(...(req.params ?? []));
        break;
      case "get":
        result = db.prepare(req.sql!).get(...(req.params ?? [])) ?? null;
        break;
      case "close":
        db.close();
        result = null;
        break;
    }
    parentPort!.postMessage({ id: req.id, result });
  } catch (err) {
    parentPort!.postMessage({
      id: req.id,
      error: { message: (err as Error).message, code: (err as { code?: string }).code },
    });
  }
});
