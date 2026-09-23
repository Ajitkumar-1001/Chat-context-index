/**
 * Runnable example: open a history, ingest, index, retrieve, ask, export/import, clear.
 *
 * No Redis required (FR-008's cache backend is Python-only for this release; TypeScript parity
 * covers store/ingest/search/retrieve/ask/index/export/clear per FR-011).
 *
 * Run with:
 *   npm install chat-context-index
 *   node basic-usage.mjs
 */

import {
  HistoryStore, ingest, index, retrieve, ask, exportHistory, importHistory, clearHistory,
} from "chat-context-index";
import { BoundedProvider } from "chat-context-index/dist/provider.js";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/** A trivial real `Provider` for this example — never call a real LLM without your own
 * credentials/config. Replace with an OpenAI/Anthropic-backed adapter for real use. */
class EchoProvider {
  async complete(request) {
    if (request.operation === "indexing") {
      return { text: JSON.stringify({ title: "Conversation", summary: "A short exchange." }) };
    }
    return { text: JSON.stringify({ answer: "See the evidence above.", citations: ["ev_1"] }) };
  }
}

async function main() {
  const dir = mkdtempSync(join(tmpdir(), "cci-example-"));
  const dbPath = join(dir, "history.db");

  const store = await HistoryStore.open(dbPath);
  console.log(`opened history ${store.historyId}`);

  const receipt = await ingest(
    store, store.historyId,
    [
      { role: "user", content: "What causes the sky to look blue?" },
      { role: "assistant", content: "Rayleigh scattering of sunlight." },
    ],
    "cli-session-1", "turn-1",
  );
  console.log(`ingested seq ${receipt.insertedSeqStart}-${receipt.insertedSeqEnd}`);

  const provider = new BoundedProvider(new EchoProvider(), {
    perProviderConcurrency: 4, providerCallDeadlineS: 30, maxProviderAttemptsPerOp: 3,
  });

  const report = await index(store, provider);
  console.log(`index status=${report.status} committed=${JSON.stringify(report.committedCoverage)}`);

  const result = await retrieve(store, "blue sky");
  console.log(`retrieve found ${result.evidence.length} evidence item(s)`);

  // Lexical search is exact-token AND-matched at this milestone — the query's tokens must all
  // appear together in one message.
  const answer = await ask(store, "sky blue", provider);
  console.log(`ask status=${answer.status} answer=${JSON.stringify(answer.answer)}`);

  const exportPath = join(dir, "export.jsonl");
  const manifest = await exportHistory(store, exportPath);
  console.log(`exported ${manifest.messageCount} message(s), checksum ${manifest.checksum.slice(0, 12)}...`);

  const target = await HistoryStore.open(join(dir, "imported.db"));
  const importReport = await importHistory(target, exportPath);
  console.log(`imported ${importReport.importedCount} message(s) into new history ${target.historyId}`);
  await target.close();

  const clearReport = await clearHistory(store, store.historyId);
  console.log(`cleared: logicalClearComplete=${clearReport.logicalClearComplete}`);

  await store.close();
}

main();
