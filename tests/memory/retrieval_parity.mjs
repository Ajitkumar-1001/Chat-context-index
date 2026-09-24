import { readFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { HistoryStore, ingest, index, search, retrieve, prepareContext, BoundedProvider } from "../../packages/typescript/dist/index.js";

const { cases } = JSON.parse(await readFile(new URL("../../spec/fixtures/retrieval-parity.json", import.meta.url), "utf8"));
const output = {};

for (const fixture of cases) {
  const dir = await mkdtemp(path.join(tmpdir(), "cci-retrieval-parity-"));
  const store = await HistoryStore.open(path.join(dir, "history.db"), { cacheBackend: "none" });
  try {
    await ingest(store, store.historyId, fixture.messages, "parity", "seed");
    const mode = fixture.mode ?? "lexical";
    const provider = mode === "tree" ? new BoundedProvider({
      complete: async request => ({
        text: JSON.stringify(request.operation === "indexing" ?
          { title: "Conversation", summary: "Planning decisions." } :
          { node_ids: [...request.evidenceContext.matchAll(/<<<CCI_EVIDENCE id=(\S+)/g)].map(match => match[1]) }),
        inputTokens: 10, outputTokens: 10,
      }),
    }, store.config) : undefined;
    if (provider && (await index(store, provider)).status !== "complete") throw new Error("tree index did not complete");
    const lexical = await search(store, fixture.query, fixture.max_messages);
    const result = await retrieve(store, fixture.query, mode, fixture.max_messages, { provider });
    const context = await prepareContext(store, fixture.query, {
      mode, provider, recentMessages: fixture.recent_messages ?? 0, maxMessages: fixture.max_messages,
      ...(fixture.max_chars === undefined ? {} : { maxChars: fixture.max_chars }),
    });
    const ids = (await store.getMessages(1, fixture.messages.length)).map(message => message.messageId);
    let rendered = context.text;
    ids.forEach((id, i) => { rendered = rendered.replaceAll(id, `m_${i + 1}`); });
    const selectedSpans = [];
    for (const id of result.routing.selectedChunkIds) {
      const row = await store.connection.get("SELECT source_message_span FROM chunks WHERE chunk_id = ?", [id]);
      if (!row) throw new Error(`missing selected chunk ${id}`);
      selectedSpans.push(row.source_message_span);
    }
    output[fixture.id] = {
      search: {
        candidates: lexical.candidates.map(c => ({
          messageId: `m_${c.seq}`, seq: c.seq, sourcePointer: c.sourcePointer,
          excerpt: c.excerpt, contentHash: c.contentHash,
        })),
        diagnostics: lexical.diagnostics,
      },
      retrieval: {
        contractVersion: result.contractVersion, status: result.status,
        snapshot: result.snapshot, routing: { ...result.routing, selectedChunkIds: selectedSpans },
        coverage: result.coverage, diagnostics: result.diagnostics, usage: result.usage,
        evidence: result.evidence.map(e => ({
          evidenceId: e.evidenceId, messageId: `m_${e.seq}`, seq: e.seq, sourcePointer: e.sourcePointer,
          excerpt: e.excerpt, contentHash: e.contentHash,
        })),
      },
      context: {
        text: rendered,
        items: context.items.map(item => ({ messageId: `m_${item.seq}`, seq: item.seq, sourcePointer: item.sourcePointer, excerpt: item.excerpt })),
        omittedCandidates: context.omittedCandidates, truncatedExcerpts: context.truncatedExcerpts,
        tokenCount: context.tokenCount,
      },
    };
  } finally {
    await store.close();
    await rm(dir, { recursive: true, force: true });
  }
}
process.stdout.write(JSON.stringify(output));
