import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { readFileSync, renameSync, symlinkSync, unlinkSync } from "node:fs";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { HistoryStore, BoundedProvider, clearHistory, exportHistory, importHistory, index, ingest, retrieve, search, cciErrors } from "../../packages/typescript/dist/index.js";
import { IOWorker } from "../../packages/typescript/dist/ioWorker.js";

const require = createRequire(new URL("../../packages/typescript/package.json", import.meta.url));
const Database = require("better-sqlite3");

async function session(fn) {
  const dir = await mkdtemp(path.join(tmpdir(), "cci-search-meta-"));
  try { await fn(dir, path.join(dir, "history.db")); }
  finally { await rm(dir, { recursive: true, force: true }); }
}

function inspect(dbPath, fn) {
  const db = new Database(dbPath, { fileMustExist: true });
  try { return fn(db); } finally { db.close(); }
}

async function seed(dbPath) {
  const store = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  await ingest(store, store.historyId, [
    { role: "user", externalId: "one", content: "The queue for billing is Kafka." },
    { role: "user", content: "Kafka is out; Pulsar replaces it." },
    { role: "assistant", content: "" },
    { role: "user", content: [{ type: "image", url: "local-image" }] },
  ], "test", "seed");
  await store.close();
}

function legacy(dbPath) {
  const tables = ["store_meta", "messages", "ingest_receipts", "exchanges", "chunks", "nodes", "node_chunks", "summary_fts", "pending_cache_purges"];
  const rows = inspect(dbPath, db => new Map([...tables.map(table => [table, db.prepare(`SELECT * FROM ${table}`).all()]),
    ["message_fts", db.prepare("SELECT rowid,* FROM message_fts").all()]]));
  const legacyPath = dbPath + ".v1";
  const db = new Database(legacyPath);
  try {
    db.exec(readFileSync(new URL("../../spec/fixtures/storage-v1.sql", import.meta.url), "utf8"));
    for (const [table, records] of rows) for (const original of records) {
      const row = { ...original };
      if (table === "store_meta") { delete row.search_metadata_revision; row.schema_version = 1; }
      const fields = Object.keys(row);
      db.prepare(`INSERT INTO ${table} (${fields.join(",")}) VALUES (${fields.map(() => "?").join(",")})`).run(...Object.values(row));
    }
  } finally { db.close(); }
  renameSync(legacyPath, dbPath);
}

test("schema 2 seals compact metadata alongside writes and preserves replay/clear behavior", () => session(async (_dir, dbPath) => {
  await seed(dbPath);
  const store = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  try {
    const meta = await store.connection.get("SELECT * FROM store_meta");
    assert.equal(meta.schema_version, 2);
    assert.equal(meta.search_metadata_revision, meta.history_revision);
    const mapped = await store.connection.all("SELECT l.*, f.message_id AS fts_id FROM lexical_message_meta l JOIN message_fts f ON f.rowid=l.fts_rowid ORDER BY l.seq");
    assert.deepEqual(mapped.map(r => r.seq), [1, 2, 3]);
    assert.ok(mapped.every(r => r.message_id === r.fts_id && r.history_id === store.historyId));
    const before = await search(store, "queue billing");
    assert.deepEqual(before.candidates.map(c => c.seq), [1]);
    assert.deepEqual((await retrieve(store, "queue billing")).evidence.map(c => c.seq), [1, 2]);
    await ingest(store, store.historyId, [{ role: "user", externalId: "one", content: "The queue for billing is Kafka." }], "test", "skipped");
    const skipped = await store.connection.get("SELECT * FROM store_meta");
    assert.equal(skipped.search_metadata_revision, skipped.history_revision);
    assert.equal(skipped.history_revision, meta.history_revision + 1);
    await ingest(store, store.historyId, [{ role: "user", externalId: "one", content: "The queue for billing is Kafka." }], "test", "skipped");
    assert.deepEqual(await store.connection.get("SELECT * FROM store_meta"), skipped);
    await clearHistory(store, store.historyId);
    assert.equal((await store.connection.get("SELECT count(*) AS n FROM lexical_message_meta")).n, 0);
    assert.deepEqual((await search(store, "Kafka")).candidates, []);
    const receipt = await ingest(store, store.historyId, [{ role: "user", content: "new Kafka" }], "test", "after");
    assert.equal(receipt.insertedSeqStart, 5);
    const after = await store.connection.get("SELECT * FROM store_meta");
    assert.equal(after.search_metadata_revision, after.history_revision);
  } finally { await store.close(); }
}));

test("stale search metadata fails reads and mutations without sealing old-writer data", () => session(async (_dir, dbPath) => {
  await seed(dbPath);
  const store = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  try {
    await store.connection.exec("UPDATE store_meta SET history_revision=history_revision+1");
    const before = await store.connection.get("SELECT * FROM store_meta");
    await assert.rejects(search(store, "Kafka"), cciErrors.StoreCorrupt);
    await assert.rejects(retrieve(store, "Kafka"), cciErrors.StoreCorrupt);
    await assert.rejects(ingest(store, store.historyId, [{ role: "user", content: "must not insert" }], "test", "blocked"), cciErrors.StoreCorrupt);
    await assert.rejects(clearHistory(store, store.historyId), cciErrors.StoreCorrupt);
    assert.deepEqual(await store.connection.get("SELECT * FROM store_meta"), before);
    assert.equal((await store.connection.get("SELECT count(*) AS n FROM messages")).n, 4);
  } finally { await store.close(); }
}));

test("derived-index write failure rolls back messages, receipt, FTS and revision together", () => session(async (_dir, dbPath) => {
  const store = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  const run = store.connection.run.bind(store.connection);
  store.connection.run = async (sql, params) => {
    if (sql.includes("INSERT INTO lexical_message_meta")) throw new Error("injected metadata write failure");
    return run(sql, params);
  };
  try {
    await assert.rejects(ingest(store, store.historyId, [{ role: "user", content: "rollback marker" }], "test", "failed"), /metadata write failure/);
    for (const table of ["messages", "message_fts", "lexical_message_meta", "ingest_receipts"]) {
      assert.equal((await store.connection.get(`SELECT count(*) AS n FROM ${table}`)).n, 0);
    }
    assert.deepEqual(await store.connection.get("SELECT history_revision, search_metadata_revision FROM store_meta"), { history_revision: 0, search_metadata_revision: 0 });
  } finally { await store.close(); }
}));

test("explicit v1 migration keeps originals and revisions and creates a private pre-migration backup", () => session(async (dir, dbPath) => {
  await seed(dbPath); legacy(dbPath);
  const before = inspect(dbPath, db => ({ meta: db.prepare("SELECT * FROM store_meta").get(), messages: db.prepare("SELECT * FROM messages ORDER BY seq").all(), receipts: db.prepare("SELECT * FROM ingest_receipts").all() }));
  await assert.rejects(async () => {
    const unexpected = await HistoryStore.open(dbPath, { cacheBackend: "none" });
    await unexpected.close();
  }, cciErrors.SchemaVersionError);
  const backupPath = path.join(dir, "before.db");
  await HistoryStore.migrate(dbPath, { backupPath, config: { cacheBackend: "none" } });
  assert.equal((await stat(backupPath)).mode & 0o777, 0o600);
  assert.deepEqual(inspect(backupPath, db => ({ meta: db.prepare("SELECT * FROM store_meta").get(), messages: db.prepare("SELECT * FROM messages ORDER BY seq").all(), receipts: db.prepare("SELECT * FROM ingest_receipts").all() })), before);
  const store = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  try {
    const after = await store.connection.get("SELECT * FROM store_meta");
    assert.equal(after.schema_version, 2);
    assert.equal(after.search_metadata_revision, before.meta.history_revision);
    delete after.schema_version; delete after.search_metadata_revision;
    const previous = { ...before.meta }; delete previous.schema_version;
    assert.deepEqual(after, previous);
    assert.deepEqual(await store.connection.all("SELECT * FROM messages ORDER BY seq"), before.messages);
    assert.deepEqual((await search(store, "billing queue")).candidates.map(c => c.seq), [1]);
  } finally { await store.close(); }
  await HistoryStore.migrate(dbPath, { backupPath }); // v2 validates and does not overwrite the backup.
  assert.equal(inspect(backupPath, db => db.prepare("SELECT schema_version AS v FROM store_meta").get().v), 1);
}));

test("migration errors roll back schema and never overwrite or create missing stores", () => session(async (dir, dbPath) => {
  const missing = path.join(dir, "missing.db");
  await assert.rejects(HistoryStore.migrate(missing, { backupPath: path.join(dir, "unused.db") }));
  await assert.rejects(stat(missing), { code: "ENOENT" });
  await seed(dbPath); legacy(dbPath);
  const existing = path.join(dir, "existing.db"); await writeFile(existing, "keep me");
  await assert.rejects(HistoryStore.migrate(dbPath, { backupPath: existing }));
  assert.equal(await readFile(existing, "utf8"), "keep me");
  await assert.rejects(HistoryStore.migrate(dbPath, { backupPath: "relative.db" }));
  inspect(dbPath, db => db.exec("UPDATE message_fts SET text='mismatched index text' WHERE rowid=1"));
  const backupPath = path.join(dir, "broken-before.db");
  await assert.rejects(HistoryStore.migrate(dbPath, { backupPath }), cciErrors.StoreCorrupt);
  assert.equal(inspect(dbPath, db => db.prepare("SELECT schema_version AS v FROM store_meta").get().v), 1);
  assert.equal(inspect(dbPath, db => db.prepare("SELECT count(*) AS n FROM sqlite_master WHERE name='lexical_message_meta'").get().n), 0);
  assert.equal(inspect(backupPath, db => db.prepare("SELECT schema_version AS v FROM store_meta").get().v), 1);
}));

test("explicit rebuild restores FTS and metadata without changing original state", () => session(async (dir, dbPath) => {
  await seed(dbPath);
  const before = inspect(dbPath, db => ({ messages: db.prepare("SELECT * FROM messages ORDER BY seq").all(), meta: db.prepare("SELECT * FROM store_meta").get() }));
  inspect(dbPath, db => { db.exec("DELETE FROM message_fts; DELETE FROM lexical_message_meta; UPDATE store_meta SET search_metadata_revision=-1"); });
  const backupPath = path.join(dir, "before-rebuild.db");
  await HistoryStore.rebuildSearchIndex(dbPath, { backupPath, config: { cacheBackend: "none" } });
  assert.equal(inspect(backupPath, db => db.prepare("SELECT count(*) AS n FROM message_fts").get().n), 0);
  assert.deepEqual(inspect(dbPath, db => ({ messages: db.prepare("SELECT * FROM messages ORDER BY seq").all(), meta: db.prepare("SELECT * FROM store_meta").get() })), before);
  const store = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  try { assert.deepEqual((await search(store, "billing")).candidates.map(c => c.seq), [1]); }
  finally { await store.close(); }
}));

test("import maps actual FTS rowids, preserves imported sequences and seals each batch", () => session(async (dir, dbPath) => {
  await seed(dbPath);
  const store = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  const exportPath = path.join(dir, "history.jsonl");
  try { await exportHistory(store, exportPath); } finally { await store.close(); }
  const lines = (await readFile(exportPath, "utf8")).trimEnd().split("\n");
  const manifest = JSON.parse(lines.pop());
  const reordered = [lines[2], lines[1], lines[0], lines[3]];
  manifest.checksum = createHash("sha256").update(reordered.join("")).digest("hex");
  await writeFile(exportPath, [...reordered, JSON.stringify(manifest)].join("\n") + "\n");
  const target = await HistoryStore.open(path.join(dir, "imported.db"), { cacheBackend: "none" });
  try {
    await importHistory(target, exportPath);
    const meta = await target.connection.get("SELECT * FROM store_meta");
    assert.equal(meta.search_metadata_revision, meta.history_revision);
    const rows = await target.connection.all("SELECT l.seq,l.fts_rowid,l.message_id,f.message_id AS actual_id FROM lexical_message_meta l JOIN message_fts f ON f.rowid=l.fts_rowid ORDER BY l.seq");
    assert.deepEqual(rows.map(r => r.seq), [1, 2, 3]);
    assert.ok(rows.every(r => r.message_id === r.actual_id));
    assert.deepEqual(rows.map(r => r.fts_rowid), [3, 2, 1]);
    assert.deepEqual((await search(target, "billing")).candidates.map(c => c.seq), [1]);
  } finally { await target.close(); }
}));

test("migration preserves actual FTS rowids and validates corrupt v2 without silently repairing", () => session(async (dir, dbPath) => {
  await seed(dbPath); legacy(dbPath);
  inspect(dbPath, db => db.exec("UPDATE message_fts SET rowid=rowid+100"));
  await HistoryStore.migrate(dbPath, { backupPath: path.join(dir, "legacy.db") });
  assert.deepEqual(inspect(dbPath, db => db.prepare("SELECT fts_rowid,seq FROM lexical_message_meta ORDER BY seq").all()),
    [{ fts_rowid: 101, seq: 1 }, { fts_rowid: 102, seq: 2 }, { fts_rowid: 103, seq: 3 }]);
  inspect(dbPath, db => db.exec("UPDATE lexical_message_meta SET seq=999 WHERE fts_rowid=101"));
  const unused = path.join(dir, "must-not-create.db");
  await assert.rejects(HistoryStore.migrate(dbPath, { backupPath: unused }), cciErrors.StoreCorrupt);
  await assert.rejects(stat(unused), { code: "ENOENT" });
  assert.equal(inspect(dbPath, db => db.prepare("SELECT seq FROM lexical_message_meta WHERE fts_rowid=101").get().seq), 999);
}));

test("rebuild recovers missing derived tables and rejects unknown schema versions", () => session(async (dir, dbPath) => {
  await seed(dbPath);
  inspect(dbPath, db => db.exec("DROP TABLE message_fts; DROP TABLE lexical_message_meta"));
  await HistoryStore.rebuildSearchIndex(dbPath, { backupPath: path.join(dir, "missing-derived.db") });
  const store = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  try { assert.deepEqual((await search(store, "queue billing")).candidates.map(c => c.seq), [1]); }
  finally { await store.close(); }
  inspect(dbPath, db => db.exec("UPDATE store_meta SET schema_version=99"));
  for (const operation of ["migrate", "rebuildSearchIndex"]) {
    const backupPath = path.join(dir, operation + ".db");
    await assert.rejects(HistoryStore[operation](dbPath, { backupPath }), cciErrors.SchemaVersionError);
    await assert.rejects(stat(backupPath), { code: "ENOENT" });
  }
  assert.equal(inspect(dbPath, db => db.prepare("SELECT schema_version AS v FROM store_meta").get().v), 99);
}));

test("public search and retrieval keep their revision guard and evidence in one read snapshot", () => session(async (_dir, dbPath) => {
  await seed(dbPath);
  const store = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  const get = store.connection.get.bind(store.connection);
  let changed = false;
  store.connection.get = async (sql, params) => {
    const result = await get(sql, params);
    if (!changed && sql.includes("search_metadata_revision")) {
      changed = true;
      inspect(dbPath, db => db.exec("UPDATE store_meta SET history_revision=history_revision+1"));
    }
    return result;
  };
  try {
    assert.deepEqual((await search(store, "queue billing")).candidates.map(c => c.seq), [1]);
    await assert.rejects(search(store, "queue billing"), cciErrors.StoreCorrupt);
    await store.connection.exec("UPDATE store_meta SET search_metadata_revision=history_revision");
    changed = false;
    const result = await retrieve(store, "queue billing");
    assert.deepEqual(result.evidence.map(c => c.seq), [1, 2]);
    await assert.rejects(retrieve(store, "queue billing"), cciErrors.StoreCorrupt);
  } finally { await store.close(); }
}));

test("import detects a foreign writer between committed batches without merging the next batch", () => session(async (dir, dbPath) => {
  const source = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  const exportPath = path.join(dir, "batch.jsonl");
  try {
    await ingest(source, source.historyId, Array.from({ length: 501 }, (_, n) => ({ role: "user", content: `batch ${n}` })), "test", "many");
    await exportHistory(source, exportPath);
  } finally { await source.close(); }
  const targetPath = path.join(dir, "target.db");
  const target = await HistoryStore.open(targetPath, { cacheBackend: "none" });
  const exec = target.connection.exec.bind(target.connection);
  let changed = false;
  target.connection.exec = async sql => {
    await exec(sql);
    if (!changed && sql === "COMMIT") {
      changed = true;
      inspect(targetPath, db => db.exec("UPDATE store_meta SET history_revision=history_revision+1, search_metadata_revision=search_metadata_revision+1"));
    }
  };
  try {
    await assert.rejects(importHistory(target, exportPath), cciErrors.VersionConflict);
    assert.equal((await target.connection.get("SELECT count(*) AS n FROM messages")).n, 500);
    assert.equal((await target.connection.get("SELECT count(*) AS n FROM lexical_message_meta")).n, 500);
  } finally { await target.close(); }
}));

test("migration backup includes committed WAL content while the writer connection remains open", () => session(async (dir, dbPath) => {
  await seed(dbPath); legacy(dbPath);
  const writer = new Database(dbPath);
  writer.pragma("journal_mode=WAL");
  writer.pragma("wal_autocheckpoint=0");
  const content = "Committed WAL evidence is preserved.";
  try {
    writer.exec("BEGIN IMMEDIATE");
    writer.prepare("UPDATE messages SET original_payload=?,text_projection=? WHERE seq=1")
      .run(JSON.stringify({ role: "user", content }), content);
    writer.prepare("UPDATE message_fts SET text=? WHERE message_id=(SELECT message_id FROM messages WHERE seq=1)").run(content);
    writer.exec("UPDATE store_meta SET history_revision=history_revision+1; COMMIT");
    assert.ok((await stat(dbPath + "-wal")).size > 0);
    const backupPath = path.join(dir, "wal-backup.db");
    await HistoryStore.migrate(dbPath, { backupPath });
    const backup = inspect(backupPath, db => ({
      version: db.prepare("SELECT schema_version AS v FROM store_meta").get().v,
      text: db.prepare("SELECT text_projection FROM messages WHERE seq=1").get().text_projection,
    }));
    assert.deepEqual(backup, { version: 1, text: content });
  } finally { writer.close(); }
}));

test("import rechecks the initially empty destination inside its first transaction", () => session(async (dir, dbPath) => {
  await seed(dbPath);
  const source = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  const exportPath = path.join(dir, "import.jsonl");
  try { await exportHistory(source, exportPath); } finally { await source.close(); }
  const target = await HistoryStore.open(path.join(dir, "target.db"), { cacheBackend: "none" });
  const begin = target.beginWrite.bind(target);
  let changed = false;
  target.beginWrite = async () => {
    await begin();
    if (!changed) {
      changed = true;
      await ingest(target, target.historyId, [{ role: "user", content: "racing writer" }], "other", "one");
    }
  };
  try {
    await assert.rejects(importHistory(target, exportPath), cciErrors.StoreNotEmpty);
    assert.equal((await target.connection.get("SELECT count(*) AS n FROM messages")).n, 1);
    assert.deepEqual((await search(target, "racing")).candidates.map(c => c.seq), [1]);
  } finally { await target.close(); }
}));

test("stale metadata rejects indexing before any provider request", () => session(async (_dir, dbPath) => {
  await seed(dbPath);
  const store = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  let calls = 0;
  const provider = new BoundedProvider({ complete: async () => { calls++; return { text: "summary" }; } }, store.config);
  try {
    await store.connection.exec("UPDATE store_meta SET history_revision=history_revision+1");
    await assert.rejects(index(store, provider), cciErrors.StoreCorrupt);
    assert.equal(calls, 0);
    assert.equal((await store.connection.get("SELECT count(*) AS n FROM nodes")).n, 0);
  } finally { await store.close(); }
}));

test("rebuild rejects an invalid authoritative revision and rolls back its changes", () => session(async (dir, dbPath) => {
  await seed(dbPath);
  inspect(dbPath, db => db.exec("UPDATE store_meta SET history_revision=-1; DELETE FROM lexical_message_meta"));
  const before = inspect(dbPath, db => db.prepare("SELECT * FROM store_meta").get());
  await assert.rejects(HistoryStore.rebuildSearchIndex(dbPath, { backupPath: path.join(dir, "invalid-revision.db") }), cciErrors.StoreCorrupt);
  assert.deepEqual(inspect(dbPath, db => db.prepare("SELECT * FROM store_meta").get()), before);
  assert.equal(inspect(dbPath, db => db.prepare("SELECT count(*) AS n FROM lexical_message_meta").get().n), 0);
}));

test("migration detects corrupt FTS postings and explicit v2 rebuild repairs them", () => session(async (dir, dbPath) => {
  await seed(dbPath); legacy(dbPath);
  inspect(dbPath, db => { db.unsafeMode(true); db.exec("DELETE FROM message_fts_data WHERE id>10"); });
  await assert.rejects(HistoryStore.migrate(dbPath, { backupPath: path.join(dir, "corrupt-v1.db") }), cciErrors.StoreCorrupt);
  assert.equal(inspect(dbPath, db => db.prepare("SELECT schema_version AS v FROM store_meta").get().v), 1);
  assert.equal(inspect(dbPath, db => db.prepare("SELECT count(*) AS n FROM sqlite_master WHERE name='lexical_message_meta'").get().n), 0);
  const currentPath = path.join(dir, "current.db");
  await seed(currentPath);
  inspect(currentPath, db => { db.unsafeMode(true); db.exec("DELETE FROM message_fts_data WHERE id>10"); });
  await HistoryStore.rebuildSearchIndex(currentPath, { backupPath: path.join(dir, "corrupt-v2.db") });
  const store = await HistoryStore.open(currentPath, { cacheBackend: "none" });
  try { assert.deepEqual((await search(store, "queue billing")).candidates.map(c => c.seq), [1]); }
  finally { await store.close(); }
}));

test("migration freezes the source identity before a symlink can be retargeted", () => session(async (dir, dbPath) => {
  const otherPath = path.join(dir, "other.db"), alias = path.join(dir, "alias.db");
  await seed(dbPath); legacy(dbPath);
  await seed(otherPath); legacy(otherPath);
  const sourceHistory = inspect(dbPath, db => db.prepare("SELECT history_id FROM store_meta").get().history_id);
  symlinkSync(dbPath, alias);
  const open = IOWorker.open;
  let retargeted = false;
  IOWorker.open = async (...args) => {
    const io = await open(...args);
    if (!retargeted) { retargeted = true; unlinkSync(alias); symlinkSync(otherPath, alias); }
    return io;
  };
  const backupPath = path.join(dir, "canonical-backup.db");
  try { await HistoryStore.migrate(alias, { backupPath }); }
  finally { IOWorker.open = open; }
  assert.equal(inspect(dbPath, db => db.prepare("SELECT schema_version AS v FROM store_meta").get().v), 2);
  assert.equal(inspect(otherPath, db => db.prepare("SELECT schema_version AS v FROM store_meta").get().v), 1);
  assert.equal(inspect(backupPath, db => db.prepare("SELECT history_id FROM store_meta").get().history_id), sourceHistory);
}));

test("rebuild preserves bounded correction selection using FTS rowids or the surviving compact map", () => session(async (dir, dbPath) => {
  const source = await HistoryStore.open(dbPath, { cacheBackend: "none" });
  try {
    await ingest(source, source.historyId, Array.from({ length: 41 }, (_, n) => ({
      role: "user", content: n === 0 ? "deployment plan for Oslo" : `Oslo note number ${n + 1}`,
    })), "test", "many");
  } finally { await source.close(); }
  legacy(dbPath);
  inspect(dbPath, db => db.exec("UPDATE message_fts SET rowid=100-rowid"));
  await HistoryStore.migrate(dbPath, { backupPath: path.join(dir, "ordered-v1.db") });
  const evidence = async () => {
    const store = await HistoryStore.open(dbPath, { cacheBackend: "none" });
    try { return (await retrieve(store, "deployment plan", "lexical", 2)).evidence.map(c => c.seq); }
    finally { await store.close(); }
  };
  assert.deepEqual(await evidence(), [1, 32]);
  const rowids = inspect(dbPath, db => db.prepare("SELECT fts_rowid,message_id FROM lexical_message_meta ORDER BY fts_rowid").all());
  for (const dropFts of [false, true]) {
    if (dropFts) inspect(dbPath, db => db.exec("DROP TABLE message_fts"));
    await HistoryStore.rebuildSearchIndex(dbPath, { backupPath: path.join(dir, `ordered-rebuild-${dropFts}.db`) });
    assert.deepEqual(await evidence(), [1, 32]);
    assert.deepEqual(inspect(dbPath, db => db.prepare("SELECT fts_rowid,message_id FROM lexical_message_meta ORDER BY fts_rowid").all()), rowids);
  }
}));
