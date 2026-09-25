/**
 * HistoryStore: open()/close() and the durable schema (spec/storage-format.md) — mirrors
 * store.py. DDL is lifted verbatim from store.py's `_SCHEMA_DDL`, not retyped, so a Python-
 * created store and a TypeScript-created store are byte-identical in structure (AT-11, AT-17
 * cross-runtime requirement).
 *
 * Check order (contracts/operations.md `open()` Check order): configuration is validated FIRST,
 * before any store file is created or opened; then the loaded SQLite runtime/FTS5 support is
 * verified; then `schema_version` is checked; only then is the store created (fresh path) or
 * resumed (existing path) transactionally.
 */

import { closeSync, existsSync, openSync, realpathSync, statSync } from "node:fs";
import { isAbsolute } from "node:path";
import Database from "better-sqlite3";
import { Config, resolveConfig, validateConfig } from "./config.js";
import { InputValidationError, RuntimeCompatibilityError, SchemaVersionError, StoreCorrupt } from "./errors.js";
import { IOWorker, mapStorageError } from "./ioWorker.js";
import { Message } from "./models.js";
import { prefixedId } from "./ids.js";
import { LEXICAL_METADATA_DDL, backfillSearchMetadata, preserveRebuildRowids, requireSearchMetadata, validateMessageFts, validateSearchMetadata } from "./searchMetadata.js";

// get_messages() pagination (contracts/operations.md `search()`/`get_messages()`/`view_node()`
// Pagination/size limits) — same defaults as store.py.
export const DEFAULT_GET_MESSAGES_LIMIT = 500;
export const MAX_GET_MESSAGES_LIMIT = 5_000;

// Required SQLite runtime (research.md §1: fixes the WAL-reset corruption bug present from
// 3.7.0 through 3.51.2) — same floor as store.py, checked against the LOADED runtime.
const MIN_SQLITE_VERSION: [number, number, number] = [3, 51, 3];

export const SCHEMA_VERSION = 2;

// Lifted verbatim from store.py's `_SCHEMA_DDL` (spec/storage-format.md) — same tables, same
// columns, same constraints, so a store created by either language is structurally identical.
const SCHEMA_DDL = `
CREATE TABLE IF NOT EXISTS store_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    history_id TEXT NOT NULL UNIQUE,
    store_instance_id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    history_revision INTEGER NOT NULL DEFAULT 0,
    search_metadata_revision INTEGER NOT NULL DEFAULT 0,
    index_revision INTEGER NOT NULL DEFAULT 0,
    cache_generation INTEGER NOT NULL DEFAULT 0,
    index_committed_seq INTEGER NOT NULL DEFAULT 0,
    index_pending_seq INTEGER NOT NULL DEFAULT 0,
    seq_high_water_mark INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    history_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    external_id TEXT,
    idempotency_key TEXT,
    position_in_batch INTEGER,
    role TEXT NOT NULL,
    original_payload TEXT NOT NULL,
    text_projection TEXT,
    payload_hash TEXT NOT NULL,
    session_metadata TEXT,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_seq ON messages(history_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_identity_external
    ON messages(history_id, source_id, external_id)
    WHERE external_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_identity_batch
    ON messages(history_id, source_id, idempotency_key, position_in_batch)
    WHERE external_id IS NULL;

CREATE TABLE IF NOT EXISTS ingest_receipts (
    history_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    inserted_seq_start INTEGER,
    inserted_seq_end INTEGER,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    unsupported_block_count INTEGER NOT NULL DEFAULT 0,
    indexing_status TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (history_id, source_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS exchanges (
    exchange_id TEXT PRIMARY KEY,
    history_id TEXT NOT NULL,
    normalization_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    history_id TEXT NOT NULL,
    source_message_span TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    rendering_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    history_id TEXT NOT NULL,
    parent_id TEXT,
    sibling_order INTEGER NOT NULL,
    message_range TEXT NOT NULL,
    title TEXT,
    summary TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    index_revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS node_chunks (
    node_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    chunk_order INTEGER NOT NULL,
    PRIMARY KEY (node_id, chunk_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    message_id UNINDEXED, history_id UNINDEXED, text
);
CREATE VIRTUAL TABLE IF NOT EXISTS summary_fts USING fts5(
    node_id UNINDEXED, history_id UNINDEXED, text
);

${LEXICAL_METADATA_DDL}

CREATE TABLE IF NOT EXISTS pending_cache_purges (
    scope_id TEXT PRIMARY KEY,
    requested_at REAL NOT NULL
);
`;

export interface NodeView {
  nodeId: string;
  parentId: string | null;
  siblingOrder: number;
  messageRange: string;
  title: string | null;
  summary: string | null;
  state: string;
  indexRevision: number;
}

export interface SearchMaintenanceOptions {
  backupPath: string;
  config?: Partial<Config>;
}

function checkRuntime(): void {
  // A short-lived in-process check (independent of the worker thread's own connection) —
  // verifies the LOADED SQLite runtime/FTS5 support before ever touching the store file.
  const probe = new Database(":memory:");
  try {
    const versionRow = probe.prepare("select sqlite_version() as v").get() as { v: string };
    const parts = versionRow.v.split(".").slice(0, 3).map(Number) as [number, number, number];
    const older =
      parts[0] < MIN_SQLITE_VERSION[0] ||
      (parts[0] === MIN_SQLITE_VERSION[0] &&
        (parts[1] < MIN_SQLITE_VERSION[1] ||
          (parts[1] === MIN_SQLITE_VERSION[1] && parts[2] < MIN_SQLITE_VERSION[2])));
    if (older) {
      throw new RuntimeCompatibilityError(
        `loaded SQLite ${versionRow.v} is older than the required ${MIN_SQLITE_VERSION.join(".")} ` +
          "(WAL-reset fix) — no default backport allowance; see spec/storage-format.md",
      );
    }
    const options = probe.pragma("compile_options", { simple: false }) as { compile_options: string }[];
    if (!options.some((o) => o.compile_options.includes("FTS5"))) {
      throw new RuntimeCompatibilityError("loaded SQLite build lacks FTS5 support");
    }
  } finally {
    probe.close();
  }
}

export class HistoryStore {
  readonly path: string;
  readonly config: Config;
  readonly historyId: string;
  readonly storeInstanceId: string;
  private readonly io: IOWorker;
  private closed = false;

  // Write coordination (clear_history() gating; same design as store.py): a single-writer
  // serialization point plus a gate/counter so clearHistory() can quiesce in-flight writers.
  private writeQueue: Promise<void> = Promise.resolve();
  private writeGateOpen = true;
  private writeGateWaiters: (() => void)[] = [];
  private inflightWriters = 0;
  private writerDrainWaiters: (() => void)[] = [];

  private constructor(path: string, config: Config, historyId: string, storeInstanceId: string, io: IOWorker) {
    this.path = path;
    this.config = config;
    this.historyId = historyId;
    this.storeInstanceId = storeInstanceId;
    this.io = io;
  }

  get connection(): IOWorker {
    return this.io;
  }

  /** Serializes all connection access through one queue — the TypeScript counterpart to
   * store.py's `write_lock` (a single worker thread/connection cannot safely run two
   * overlapping transactions). */
  async withLock<T>(fn: () => Promise<T>): Promise<T> {
    let release!: () => void;
    const previous = this.writeQueue;
    this.writeQueue = new Promise((resolve) => (release = resolve));
    await previous;
    try {
      return await fn();
    } finally {
      release();
    }
  }

  async beginWrite(): Promise<void> {
    if (!this.writeGateOpen) {
      await new Promise<void>((resolve) => this.writeGateWaiters.push(resolve));
    }
    this.inflightWriters++;
  }

  endWrite(): void {
    this.inflightWriters--;
    if (this.inflightWriters === 0) {
      const waiters = this.writerDrainWaiters;
      this.writerDrainWaiters = [];
      waiters.forEach((w) => w());
    }
  }

  closeWriteGate(): void {
    this.writeGateOpen = false;
  }

  openWriteGate(): void {
    this.writeGateOpen = true;
    const waiters = this.writeGateWaiters;
    this.writeGateWaiters = [];
    waiters.forEach((w) => w());
  }

  async waitForWritersToDrain(timeoutS: number): Promise<boolean> {
    if (this.inflightWriters === 0) return true;
    return new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => resolve(false), timeoutS * 1000);
      this.writerDrainWaiters.push(() => {
        clearTimeout(timer);
        resolve(true);
      });
    });
  }

  static async open(dbPath: string, overrides?: Partial<Config>): Promise<HistoryStore> {
    // (1) Validate configuration FIRST, before any store file is created or opened.
    const config = resolveConfig(overrides);
    validateConfig(config);

    const isFresh = !existsSync(dbPath) || statSync(dbPath).size === 0;

    // (2) Verify the loaded SQLite runtime/FTS5 support before touching the file.
    checkRuntime();

    const io = await IOWorker.open(dbPath);
    try {
      await io.exec("PRAGMA journal_mode=WAL");
      await io.exec("PRAGMA foreign_keys=ON");
      await io.exec("PRAGMA synchronous=FULL");
      await io.exec(`PRAGMA busy_timeout=${config.sqliteBusyTimeoutMs}`);

      let historyId: string;
      let storeInstanceId: string;
      if (isFresh) {
        [historyId, storeInstanceId] = await initFresh(io);
      } else {
        [historyId, storeInstanceId] = await resumeExisting(io);
      }
      return new HistoryStore(dbPath, config, historyId, storeInstanceId, io);
    } catch (err) {
      await io.close();
      throw err;
    }
  }

  /** Explicit, offline v1→v2 migration; every actual change first creates a new SQLite backup. */
  static async migrate(dbPath: string, options: SearchMaintenanceOptions): Promise<void> {
    await maintainSearchIndex(dbPath, options, false);
  }

  /** Explicit, offline recovery from authoritative messages; never changes originals or revisions. */
  static async rebuildSearchIndex(dbPath: string, options: SearchMaintenanceOptions): Promise<void> {
    await maintainSearchIndex(dbPath, options, true);
  }

  async getMessages(startSeq: number, endSeq: number, limit = DEFAULT_GET_MESSAGES_LIMIT): Promise<Message[]> {
    const bounded = Math.min(limit, MAX_GET_MESSAGES_LIMIT);
    const rows = await this.io.all<Record<string, unknown>>(
      "SELECT message_id, seq, source_id, external_id, idempotency_key, position_in_batch, " +
        "role, original_payload, text_projection, payload_hash, session_metadata FROM messages " +
        "WHERE history_id = ? AND seq >= ? AND seq <= ? ORDER BY seq LIMIT ?",
      [this.historyId, startSeq, endSeq, bounded],
    );
    return rows.map((r) => rowToMessage(r, this.historyId));
  }

  async viewNode(nodeId: string): Promise<NodeView | null> {
    const row = await this.io.get<Record<string, unknown>>(
      "SELECT node_id, parent_id, sibling_order, message_range, title, summary, state, " +
        "index_revision FROM nodes WHERE history_id = ? AND node_id = ?",
      [this.historyId, nodeId],
    );
    if (!row) return null;
    return {
      nodeId: row.node_id as string,
      parentId: (row.parent_id as string) ?? null,
      siblingOrder: row.sibling_order as number,
      messageRange: row.message_range as string,
      title: (row.title as string) ?? null,
      summary: (row.summary as string) ?? null,
      state: row.state as string,
      indexRevision: row.index_revision as number,
    };
  }

  /** Safe to call repeatedly (AT-17). */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    await this.io.close();
  }
}

function rowToMessage(row: Record<string, unknown>, historyId: string): Message {
  return {
    messageId: row.message_id as string,
    seq: row.seq as number,
    historyId,
    sourceId: row.source_id as string,
    externalId: (row.external_id as string) ?? null,
    idempotencyKey: (row.idempotency_key as string) ?? null,
    positionInBatch: (row.position_in_batch as number) ?? null,
    role: row.role as string,
    originalPayload: JSON.parse(row.original_payload as string),
    textProjection: (row.text_projection as string) ?? null,
    payloadHash: row.payload_hash as string,
    sessionMetadata: row.session_metadata ? JSON.parse(row.session_metadata as string) : null,
  };
}

async function initFresh(io: IOWorker): Promise<[string, string]> {
  const historyId = prefixedId("t");
  const storeInstanceId = prefixedId("si");
  try {
    await io.exec("BEGIN IMMEDIATE");
    await io.exec(SCHEMA_DDL);
    await io.run(
      "INSERT INTO store_meta (id, history_id, store_instance_id, schema_version) VALUES (1, ?, ?, ?)",
      [historyId, storeInstanceId, SCHEMA_VERSION],
    );
    await io.exec("COMMIT");
  } catch (err) {
    await io.exec("ROLLBACK").catch(() => undefined);
    throw mapStorageError(err as { message: string; code?: string });
  }
  return [historyId, storeInstanceId];
}

async function resumeExisting(io: IOWorker): Promise<[string, string]> {
  const row = await io.get<{ history_id: string; store_instance_id: string; schema_version: number }>(
    "SELECT history_id, store_instance_id, schema_version FROM store_meta WHERE id = 1",
  );
  if (!row) {
    // Existing file, but no store_meta row: initialize as fresh within the same file.
    return initFresh(io);
  }
  if (row.schema_version > SCHEMA_VERSION) {
    throw new SchemaVersionError(
      `store schema_version ${row.schema_version} is newer than this build supports ` +
        `(${SCHEMA_VERSION}) — no file modification`,
    );
  }
  if (row.schema_version < SCHEMA_VERSION) {
    throw new SchemaVersionError(
      `store schema_version ${row.schema_version} is older than this build (${SCHEMA_VERSION}) ` +
        "— explicitly migrate() with a new backupPath before opening",
    );
  }
  await requireSearchMetadata(io);
  return [row.history_id, row.store_instance_id];
}

async function createMaintenanceBackup(ioPath: string, backupPath: string): Promise<void> {
  if (typeof backupPath !== "string" || !isAbsolute(backupPath)) {
    throw new InputValidationError("backupPath must be a new absolute path");
  }
  // Exclusive creation also rejects symlinks and an existing zero-length file.
  const fd = openSync(backupPath, "wx", 0o600);
  closeSync(fd);
  const reader = await IOWorker.open(ioPath, { readonly: true, fileMustExist: true });
  try {
    await reader.exec("PRAGMA synchronous=FULL");
    // The main connection already holds BEGIN IMMEDIATE, so no writer can race this snapshot.
    await reader.run("VACUUM main INTO ?", [backupPath]);
  } finally { await reader.close(); }
}

async function maintainSearchIndex(dbPath: string, options: SearchMaintenanceOptions, rebuild: boolean): Promise<void> {
  const config = resolveConfig(options?.config);
  checkRuntime();
  // Resolve once: later cwd changes or a retargeted source symlink must not redirect the backup.
  try { dbPath = realpathSync(dbPath); }
  catch (error) { throw mapStorageError(error as { message: string; code?: string }); }
  // Maintenance never creates a missing history or silently initializes an unrelated database.
  const io = await IOWorker.open(dbPath, { fileMustExist: true });
  try {
    await io.exec("PRAGMA synchronous=FULL");
    await io.exec(`PRAGMA busy_timeout=${config.sqliteBusyTimeoutMs}`);
    await io.exec("BEGIN IMMEDIATE");
    try {
      const meta = await io.get<{ schema_version: number }>("SELECT schema_version FROM store_meta WHERE id = 1");
      if (!meta) throw new StoreCorrupt("missing store_meta row");
      if (rebuild ? meta.schema_version !== SCHEMA_VERSION : ![1, SCHEMA_VERSION].includes(meta.schema_version)) {
        throw new SchemaVersionError(`unsupported schema_version ${meta.schema_version} for ${rebuild ? 'rebuildSearchIndex' : 'migrate'}`);
      }
      if (!rebuild && meta.schema_version === SCHEMA_VERSION) {
        await requireSearchMetadata(io);
        await validateSearchMetadata(io);
      } else {
        await createMaintenanceBackup(dbPath, options?.backupPath);
        if (rebuild) {
          await preserveRebuildRowids(io);
          await io.exec("DROP TABLE IF EXISTS message_fts; DROP TABLE IF EXISTS lexical_message_meta");
          await io.exec("CREATE VIRTUAL TABLE message_fts USING fts5(message_id UNINDEXED, history_id UNINDEXED, text)");
          await io.exec(LEXICAL_METADATA_DDL);
          await io.exec("INSERT INTO message_fts (rowid, message_id, history_id, text) " +
            "SELECT saved.fts_rowid, m.message_id, m.history_id, m.text_projection " +
            "FROM cci_rebuild_rowids saved JOIN messages m ON m.message_id = saved.message_id ORDER BY saved.fts_rowid");
          await io.exec("DROP TABLE cci_rebuild_rowids");
        } else {
          await io.exec("ALTER TABLE store_meta ADD COLUMN search_metadata_revision INTEGER NOT NULL DEFAULT 0");
          await io.exec(LEXICAL_METADATA_DDL);
        }
        await validateMessageFts(io);
        await backfillSearchMetadata(io);
        await validateSearchMetadata(io);
        await io.exec("UPDATE store_meta SET search_metadata_revision = history_revision WHERE id = 1");
        if (!rebuild) await io.exec(`UPDATE store_meta SET schema_version = ${SCHEMA_VERSION} WHERE id = 1`);
      }
      await requireSearchMetadata(io);
      await io.exec("COMMIT");
    } catch (err) {
      await io.exec("ROLLBACK").catch(() => undefined);
      throw mapStorageError(err as { message: string; code?: string });
    }
  } finally { await io.close(); }
}
