import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { HistoryStore, ingest, index, retrieve, search, prepareContext, BoundedProvider } from "../../packages/typescript/dist/index.js";

const { cases } = JSON.parse(await readFile(new URL("../../spec/fixtures/evidence-selection.json", import.meta.url), "utf8"));
const router = {
  complete: async request => ({ text: JSON.stringify(request.operation === "indexing" ?
    { title: "Development conversation", summary: "Planning decisions and maintenance." } :
    { node_ids: [...request.evidenceContext.matchAll(/<<<CCI_EVIDENCE id=(\S+)/g)].map(m => m[1]) }), inputTokens: 10, outputTokens: 10 }),
};

for (const fixture of cases) for (const mode of ["lexical", "tree"]) {
  test(`${mode}: ${fixture.id} preserves relevant originals within limits`, async () => {
    const dir = await mkdtemp(path.join(tmpdir(), "cci-selection-"));
    const store = await HistoryStore.open(path.join(dir, "history.db"), { cacheBackend: "none" });
    try {
      await ingest(store, store.historyId, fixture.messages, "unit", "seed");
      const provider = new BoundedProvider(router, store.config);
      assert.equal((await index(store, provider)).status, "complete");
      store.config.maxEvidenceTextScalars = fixture.evidence_budget ?? 24000;
      const result = await retrieve(store, fixture.query, mode, undefined, { provider });
      const context = await prepareContext(store, fixture.query, { mode, provider });
      for (const required of fixture.required) for (const items of [result.evidence, context.items]) {
        assert.ok(items.some(item => item.seq === required.seq && item.sourcePointer === required.pointer && item.excerpt.includes(required.contains)));
      }
      assert.ok(context.items.length <= 8 && [...context.text].length <= 4000);
      assert.deepEqual(context.items.map(i => i.seq), context.items.map(i => i.seq).sort((a, b) => a - b));
      for (const seq of [37, 38, 39, 40]) assert.ok(context.items.some(item => item.seq === seq));
      assert.equal(new Set(context.items.map(i => `${i.messageId}:${i.sourcePointer}`)).size, context.items.length);
      assert.equal(context.retrieval.usage.currentProviderCalls, mode === "tree" ? 1 : 0);
      for (const item of context.items) {
        const content = fixture.messages[item.seq - 1].content;
        const original = typeof content === "string" ? content : content[Number(item.sourcePointer.split("/")[2])].text;
        assert.ok(original.includes(item.excerpt));
      }
    } finally { await store.close(); await rm(dir, { recursive: true, force: true }); }
  });
}

test("empty, punctuation, stop-word, and absent queries produce no lexical evidence", async () => {
  const dir = await mkdtemp(path.join(tmpdir(), "cci-search-"));
  const store = await HistoryStore.open(path.join(dir, "history.db"), { cacheBackend: "none" });
  try {
    await ingest(store, store.historyId, [{ role: "user", content: "A release codename exists." }], "unit", "one");
    for (const query of ["", "   ", '" : * ()', "where is the", "NONEXISTENT_7342", "\u0301"]) {
      assert.deepEqual((await search(store, query)).candidates, []);
    }
  } finally { await store.close(); await rm(dir, { recursive: true, force: true }); }
});
