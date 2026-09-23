// Copied into a fresh npm consumer; package imports must resolve inside node_modules.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { HistoryStore, ingest, index, retrieve, prepareContext, packContext, BoundedProvider } from "chat-context-index";

const packagePath = fs.realpathSync(fileURLToPath(import.meta.resolve("chat-context-index")));
assert(packagePath.startsWith(path.resolve("node_modules", "chat-context-index") + path.sep));
const fixture = JSON.parse(fs.readFileSync("tree-memory.json", "utf8"));
class Router {
  async complete(request) {
    const blocks = [...request.evidenceContext.matchAll(/<<<CCI_EVIDENCE id=(\S+) source=\S+\n([\s\S]*?)\nCCI_EVIDENCE_END>>>/g)];
    const result = request.operation === "indexing"
      ? { title: "Conversation topics", summary: blocks.map(b => b[2]).join(" | ") }
      : { node_ids: blocks.filter(b => b[2].includes(fixture.navigation_marker)).map(b => b[1]) };
    return { text: JSON.stringify(result) };
  }
}

const [phase, dbPath] = process.argv.slice(2);
const store = await HistoryStore.open(dbPath, {
  cacheBackend: "none", treeMaxChildren: 2, targetChunkSizeScalars: 1,
});
try {
  const provider = new BoundedProvider(new Router(), store.config);
  if (phase === "seed") {
    await ingest(store, store.historyId, fixture.messages, "package-check", "initial");
    const report = await index(store, provider);
    assert.equal(report.status, "complete");
    assert(report.providerUsage.currentProviderCalls > 0);
    assert.equal((await index(store, provider)).providerUsage.currentProviderCalls, 0);
    await ingest(store, store.historyId, [fixture.next_message], "package-check", "next");
  }
  const messages = await store.getMessages(1, 100);
  assert.deepEqual(messages.map(m => m.originalPayload.content),
    [...fixture.messages, fixture.next_message].map(m => m.content));
  const result = { history_id: store.historyId, message_ids: messages.map(m => m.messageId), package_path: packagePath };
  if (phase === "read") {
    assert.equal((await retrieve(store, fixture.query, "lexical")).evidence.length, 0);
    const context = await prepareContext(store, fixture.query, {
      mode: "tree", provider, recentMessages: 2, maxMessages: 4, maxChars: 1200,
      excerptChars: 200, maxTokens: 1000, tokenCounter: text => Buffer.byteLength(text, "utf8"),
    });
    assert.equal(context.retrieval.routing.actualMode, "tree");
    assert(context.retrieval.routing.selectedChunkIds.length > 0);
    assert(context.retrieval.usage.currentProviderCalls > 0 && context.retrieval.usage.currentProviderCalls <= 4);
    assert.equal(context.tokenCount, Buffer.byteLength(context.text, "utf8"));
    assert(context.tokenCount <= 1000 && [...context.text].length <= 1200 && context.items.length <= 4);
    assert(context.items.some(item => item.seq === fixture.required_source_seq));
    assert(context.items.some(item => item.seq === messages.length));
    assert(!context.text.includes("Conversation topics"));
    for (const item of context.items) {
      const original = messages[item.seq - 1];
      assert.equal(item.messageId, original.messageId);
      assert.equal(item.sourcePointer, "/content");
      assert(original.originalPayload.content.includes(item.excerpt));
    }
    const tiny = packContext(context.items, { maxChars: 1 });
    assert.equal(tiny.text, "");
    assert.equal(tiny.omittedCandidates, context.items.length);
    Object.assign(result, { text: context.text, sequences: context.items.map(item => item.seq),
      chunks: context.retrieval.routing.selectedChunkIds });
  }
  console.log(JSON.stringify(result));
} finally { await store.close(); }
