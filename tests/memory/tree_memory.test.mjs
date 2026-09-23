import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { HistoryStore, ingest, index, retrieve, ask, prepareContext, packContext, BoundedProvider, clearHistory, cciErrors } from "../../packages/typescript/dist/index.js";
import { Router, fixture } from "./runtime_roundtrip.mjs";

async function usingStore(run) {
  const dir = await mkdtemp(path.join(tmpdir(), "cci-tree-"));
  const store = await HistoryStore.open(path.join(dir, "memory.db"), { cacheBackend: "none", treeMaxChildren: 2, targetChunkSizeScalars: 1 });
  try { await ingest(store, store.historyId, fixture.messages, "test", "seed"); await run(store); }
  finally { await store.close(); await rm(dir, { recursive: true, force: true }); }
}

test("native hierarchy reuses branches and prepares historical context", () => usingStore(async store => {
  const router = new Router(), provider = new BoundedProvider(router, store.config);
  assert.equal((await index(store, provider)).providerUsage.currentProviderCalls, 14);
  const nodes = await store.connection.all("SELECT * FROM nodes");
  assert.equal(nodes.length, 14);
  assert.equal((await index(store, provider)).providerUsage.currentProviderCalls, 0);
  await ingest(store, store.historyId, [fixture.next_message], "test", "next");
  assert.equal((await index(store, provider)).providerUsage.currentProviderCalls, 2);
  const after = new Map((await store.connection.all("SELECT * FROM nodes")).map(n => [n.node_id, n]));
  for (const node of nodes) assert.equal(after.get(node.node_id).summary, node.summary);
  const context = await prepareContext(store, fixture.query, { mode: "tree", provider });
  assert.match(context.text, /Oslo/);
  assert.match(context.text, /deployment checklist/);
  assert.equal(context.retrieval.routing.actualMode, "tree");
  assert.ok(context.retrieval.usage.currentProviderCalls <= 4);
  assert.equal(context.retrieval.usage.inputTokens, context.retrieval.usage.currentProviderCalls * 11);
  assert.ok(!context.text.includes("Conversation topics"));
}));

test("invalid model IDs cannot manufacture evidence", () => usingStore(async store => {
  await index(store, new BoundedProvider(new Router(), store.config));
  const provider = new BoundedProvider({ complete: async () => ({ text: '{"node_ids":["n_foreign"]}' }) }, store.config);
  const result = await retrieve(store, "ORCHID", "tree", undefined, { provider });
  assert.equal(result.routing.actualMode, "lexical");
  assert.deepEqual(result.routing.selectedChunkIds, []);
  assert.match(result.evidence[0].excerpt, /Oslo/);
  assert.ok(result.diagnostics.some(d => d.code === "invalid_tree_selection"));
}));

test("text budgets count Unicode and rendered evidence labels", () => {
  const candidate = { messageId: "m_one", seq: 1, sourcePointer: "/content", excerpt: "🌍".repeat(100) };
  const context = packContext([candidate], { maxChars: 1000, maxTokens: 250, tokenCounter: text => Buffer.byteLength(text) });
  assert.ok(context.tokenCount <= 250);
  assert.equal(context.omittedCandidates, 1);
  assert.throws(() => packContext([candidate], { maxTokens: 5 }));
});

test("clear during navigation rejects stale memory without holding the database lock", () => usingStore(async store => {
  await index(store, new BoundedProvider(new Router(), store.config));
  let resume, entered;
  const ready = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { resume = resolve; });
  const provider = new BoundedProvider({ complete: async () => { entered(); await gate; return { text: '{"node_ids":[]}' }; } }, store.config);
  const pending = retrieve(store, "ORCHID", "tree", undefined, { provider });
  await ready;
  await clearHistory(store, store.historyId);
  resume();
  await assert.rejects(pending, /history was cleared/);
}));

test("ask shares the navigation and synthesis attempt budget", () => usingStore(async store => {
  await index(store, new BoundedProvider(new Router(), store.config));
  const router = new Router();
  const provider = new BoundedProvider({ complete: async request => request.operation === "synthesis" ?
    { text: '{"answer":"Oslo","citations":["ev_1"]}', inputTokens: 11, outputTokens: 7 } : router.complete(request) }, store.config);
  const result = await ask(store, fixture.query, provider, "tree");
  assert.equal(result.status, "answered");
  assert.equal(result.usage.currentProviderCalls, 4);
  assert.equal(result.usage.inputTokens, 44);
}));

test("structured lexical excerpts stay within the cited source field", () => usingStore(async store => {
  await ingest(store, store.historyId, [{ role: "user", content: [{ type: "text", text: "First 🌍 block" }, { type: "text", text: "TULIP launch target" }] }], "test", "blocks");
  const result = await retrieve(store, "TULIP", "lexical");
  assert.equal(result.evidence[0].sourcePointer, "/content/1/text");
  assert.equal(result.evidence[0].excerpt, "TULIP launch target");
}));

test("physical retries cannot exceed the request budget", () => usingStore(async store => {
  await index(store, new BoundedProvider(new Router(), store.config));
  store.config.providerAttemptLimitRetrieve = 1;
  let calls = 0;
  const provider = new BoundedProvider({ complete: async () => { calls++; throw new cciErrors.ProviderTimeout("fixture"); } }, store.config);
  const result = await retrieve(store, "ORCHID", "tree", undefined, { provider });
  assert.equal(calls, 1);
  assert.equal(result.usage.currentProviderCalls, 1);
  assert.ok(result.usage.usageUnknown);
  assert.match(result.evidence[0].excerpt, /Oslo/);
}));
