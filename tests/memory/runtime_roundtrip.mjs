// Shared native-runtime fixture. No network calls or Python bridge inside the TS package.
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { HistoryStore, ingest, index, prepareContext, BoundedProvider } from "../../packages/typescript/dist/index.js";

export const fixture = JSON.parse(fs.readFileSync(new URL("../../spec/fixtures/tree-memory.json", import.meta.url), "utf8"));
export class Router {
  requests = [];
  async complete(request) {
    this.requests.push(request);
    const blocks = [...request.evidenceContext.matchAll(/<<<CCI_EVIDENCE id=(\S+) source=\S+\n([\s\S]*?)\nCCI_EVIDENCE_END>>>/g)];
    const result = request.operation === "indexing" ?
      { title: "Conversation topics", summary: blocks.map(b => b[2]).join(" | ") } :
      { node_ids: blocks.filter(b => b[2].includes(fixture.navigation_marker)).map(b => b[1]) };
    return { text: JSON.stringify(result), inputTokens: 11, outputTokens: 7 };
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [phase, path] = process.argv.slice(2);
  const store = await HistoryStore.open(path, { cacheBackend: "none", treeMaxChildren: 2, targetChunkSizeScalars: 1 });
  try {
    const provider = new BoundedProvider(new Router(), store.config);
    if (phase === "seed") {
      await ingest(store, store.historyId, fixture.messages, "test", "seed");
      await index(store, provider);
    }
    const context = await prepareContext(store, fixture.query, { mode: "tree", provider });
    console.log(JSON.stringify({ historyId: store.historyId, text: context.text,
      sequences: context.items.map(i => i.seq), chunks: context.retrieval.routing.selectedChunkIds,
      calls: context.retrieval.usage.currentProviderCalls }));
  } finally { await store.close(); }
}
