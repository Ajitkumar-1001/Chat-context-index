/** Derived lexical metadata shared with Python's schema-v2 storage contract. */
import { SchemaVersionError, StoreCorrupt, StoreError } from "./errors.js";
import { IOWorker } from "./ioWorker.js";

export const LEXICAL_METADATA_DDL = `
CREATE TABLE lexical_message_meta (
    fts_rowid INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    history_id TEXT NOT NULL,
    seq INTEGER NOT NULL
);
`;

/** Call inside the operation's read/write transaction; never silently advance a stale seal. */
export async function requireSearchMetadata(io: IOWorker): Promise<number> {
  try {
    const row = await io.get<{ schema_version: number; history_revision: number; search_metadata_revision: number }>(
      "SELECT schema_version, history_revision, search_metadata_revision FROM store_meta WHERE id = 1",
    );
    if (!row) throw new StoreCorrupt("missing store metadata");
    if (row.schema_version !== 2) throw new SchemaVersionError("search requires schema 2; explicitly migrate an older store");
    if (!Number.isInteger(row.history_revision) || row.history_revision < 0 || row.history_revision !== row.search_metadata_revision) {
      throw new StoreCorrupt("search metadata revision is stale; close the store and explicitly rebuildSearchIndex() with a backup");
    }
    await io.all("SELECT fts_rowid, message_id, history_id, seq FROM lexical_message_meta LIMIT 0");
    return row.history_revision;
  } catch (error) {
    if (error instanceof StoreError && /no such (table|column)|has no column|syntax error/i.test(error.message)) {
      throw new StoreCorrupt("missing or invalid search metadata; explicit recovery is required");
    }
    throw error;
  }
}

export async function insertSearchMetadata(
  io: IOWorker, rowid: number, messageId: string, historyId: string, seq: number,
): Promise<void> {
  await io.run("INSERT INTO lexical_message_meta (fts_rowid, message_id, history_id, seq) VALUES (?, ?, ?, ?)",
    [rowid, messageId, historyId, seq]);
}

export async function backfillSearchMetadata(io: IOWorker): Promise<void> {
  await io.exec(
    "INSERT INTO lexical_message_meta (fts_rowid, message_id, history_id, seq) " +
    "SELECT f.rowid, m.message_id, m.history_id, m.seq FROM message_fts f " +
    "JOIN messages m ON m.message_id = f.message_id",
  );
}

/** Full offline validation. Counts plus unique-ID/rowid constraints establish a bijection. */
export async function validateMessageFts(io: IOWorker): Promise<number> {
  const expected = (await io.get<{ n: number }>(
    "SELECT count(*) AS n FROM messages WHERE text_projection IS NOT NULL",
  ))!.n;
  const fts = (await io.get<{ n: number; identities: number }>(
    "SELECT count(*) AS n, count(DISTINCT message_id) AS identities FROM message_fts",
  ))!;
  if (fts.n !== expected || fts.identities !== expected) {
    throw new StoreCorrupt("search metadata/FTS counts do not match authoritative messages");
  }
  const invalidHistory = await io.get(
    "SELECT 1 FROM messages WHERE history_id IS NOT (SELECT history_id FROM store_meta WHERE id = 1) " +
    "OR typeof(seq) <> 'integer' OR seq < 1 LIMIT 1",
  );
  const invalidFts = await io.get(
    "SELECT 1 FROM message_fts f LEFT JOIN messages m ON m.message_id = f.message_id " +
    "WHERE m.message_id IS NULL OR m.text_projection IS NULL OR f.history_id IS NOT m.history_id " +
    "OR f.text IS NOT m.text_projection LIMIT 1",
  );
  if (invalidHistory || invalidFts) {
    throw new StoreCorrupt("FTS identity, history, or text mismatch");
  }
  await io.run("INSERT INTO message_fts(message_fts) VALUES('integrity-check')");
  return expected;
}

export async function validateSearchMetadata(io: IOWorker): Promise<void> {
  const expected = await validateMessageFts(io);
  const mapped = (await io.get<{ n: number }>("SELECT count(*) AS n FROM lexical_message_meta"))!.n;
  if (mapped !== expected) throw new StoreCorrupt("search metadata count does not match authoritative messages");
  const invalidMap = await io.get(
    "SELECT 1 FROM lexical_message_meta x LEFT JOIN message_fts f ON f.rowid = x.fts_rowid " +
    "LEFT JOIN messages m ON m.message_id = x.message_id WHERE f.rowid IS NULL OR m.message_id IS NULL " +
    "OR x.message_id IS NOT f.message_id OR x.history_id IS NOT m.history_id " +
    "OR x.history_id IS NOT f.history_id OR x.seq IS NOT m.seq LIMIT 1",
  );
  if (invalidMap) {
    throw new StoreCorrupt("search metadata/FTS identity, history, sequence, or text mismatch");
  }
}

function unavailableDerivedIndex(error: unknown): boolean {
  return error instanceof StoreCorrupt ||
    (error instanceof StoreError && /no such (table|column)|has no column|malformed schema/i.test(error.message));
}

/** Preserve correction ordering when either existing derived structure still maps original IDs. */
export async function preserveRebuildRowids(io: IOWorker): Promise<void> {
  await io.exec("CREATE TEMP TABLE cci_rebuild_rowids (fts_rowid INTEGER PRIMARY KEY, message_id TEXT NOT NULL UNIQUE)");
  const expected = (await io.get<{ n: number }>("SELECT count(*) AS n FROM messages WHERE text_projection IS NOT NULL"))!.n;
  try {
    const counts = (await io.get<{ n: number; ids: number }>("SELECT count(*) AS n, count(DISTINCT message_id) AS ids FROM message_fts"))!;
    const invalid = await io.get(
      "SELECT 1 FROM message_fts f LEFT JOIN messages m ON m.message_id = f.message_id " +
      "WHERE m.message_id IS NULL OR m.text_projection IS NULL OR f.history_id IS NOT m.history_id LIMIT 1",
    );
    if (counts.n === expected && counts.ids === expected && !invalid) {
      await io.exec("INSERT INTO cci_rebuild_rowids (fts_rowid, message_id) SELECT rowid, message_id FROM message_fts");
      return;
    }
  } catch (error) { if (!unavailableDerivedIndex(error)) throw error; }
  try {
    const counts = (await io.get<{ n: number; ids: number; rowids: number }>(
      "SELECT count(*) AS n, count(DISTINCT message_id) AS ids, count(DISTINCT fts_rowid) AS rowids FROM lexical_message_meta",
    ))!;
    const invalid = await io.get(
      "SELECT 1 FROM lexical_message_meta x LEFT JOIN messages m ON m.message_id = x.message_id " +
      "WHERE m.message_id IS NULL OR m.text_projection IS NULL OR x.history_id IS NOT m.history_id " +
      "OR x.seq IS NOT m.seq OR typeof(x.fts_rowid) <> 'integer' LIMIT 1",
    );
    if (counts.n === expected && counts.ids === expected && counts.rowids === expected && !invalid) {
      await io.exec("INSERT INTO cci_rebuild_rowids (fts_rowid, message_id) SELECT fts_rowid, message_id FROM lexical_message_meta");
      return;
    }
  } catch (error) { if (!unavailableDerivedIndex(error)) throw error; }
  // Both derived mappings are unusable; this explicit recovery reconstructs insertion order by seq.
  await io.exec("INSERT INTO cci_rebuild_rowids (fts_rowid, message_id) " +
    "SELECT row_number() OVER (ORDER BY seq), message_id FROM messages WHERE text_projection IS NOT NULL");
}
