// Linking branches a whole-history fixture cannot reach; mirrors test_linked_corrections.py.
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { HistoryStore, ingest, search } from "../../packages/typescript/dist/index.js";
import { linkedCorrections } from "../../packages/typescript/dist/search.js";

const QUERY = "Which queue handles billing?";

async function linked(contents, maxSeq, limit) {
  const dir = await mkdtemp(path.join(tmpdir(), "cci-linked-"));
  const store = await HistoryStore.open(path.join(dir, "history.db"), { cacheBackend: "none" });
  try {
    await ingest(store, store.historyId, contents.map(content => ({ role: "user", content })), "t", "k");
    const hits = (await search(store, QUERY, limit)).candidates;
    const { pairs, limited } = await linkedCorrections(store, hits, QUERY, maxSeq, limit);
    return { pairs: pairs.map(([source, link]) => [source.seq, link.seq]), limited };
  } finally {
    await store.close();
    await rm(dir, { recursive: true, force: true });
  }
}

test("messages past the snapshot are never linked", async () => {
  const contents = ["The queue for billing is Kafka.", "Kafka is out; Pulsar replaces it."];
  assert.deepEqual(await linked(contents, 1, 2), { pairs: [], limited: false });
  assert.deepEqual(await linked(contents, 2, 2), { pairs: [[1, 2]], limited: false });
});

test("row cap reports limited and keeps the newest", async () => {
  const contents = ["The queue for billing is Kafka.", ...Array.from({ length: 34 }, (_, n) => `Kafka note ${n}.`)];
  assert.deepEqual(await linked(contents, contents.length, 1), { pairs: [[1, contents.length]], limited: true });
});
