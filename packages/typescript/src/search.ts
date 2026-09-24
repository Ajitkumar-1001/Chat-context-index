/**
 * search() (contracts/operations.md `search()`/`get_messages()`/`view_node()`) — mirrors
 * search.py. Never calls a model. Parameterized FTS5 MATCH expressions built from sanitized
 * tokens — never a raw/unbounded FTS5 expression.
 */

import { createHash } from "node:crypto";
import { codePointLength } from "./models.js";
import { excerptForQuery, queryTerms, relevance } from "./relevance.js";
import { HistoryStore } from "./store.js";

const DEFAULT_LIMIT = 20;

export interface LexicalCandidate {
  messageId: string;
  seq: number;
  sourcePointer: string;
  excerpt: string;
  contentHash: string;
}

export interface Diagnostic {
  code: string;
  stage: string;
  retryable: boolean;
}

export interface SearchResult {
  candidates: LexicalCandidate[];
  diagnostics: Diagnostic[];
}

export async function search(store: HistoryStore, query: string, limit = DEFAULT_LIMIT): Promise<SearchResult> {
  if (!Number.isInteger(limit) || limit < 1 || limit > 5000) throw new RangeError("require 1 <= search limit <= 5000");
  const terms = queryTerms(query);
  if (!terms.length) {
    return { candidates: [], diagnostics: [{ code: "empty_or_nonsearchable_query", stage: "search", retryable: false }] };
  }

  const matches = terms.map(() => "SELECT message_id FROM message_fts WHERE history_id = ? AND message_fts MATCH ?").join(" UNION ALL ");
  const parameters: (string | number)[] = terms.flatMap(term => [store.historyId, `"${term.replace(/"/g, '""')}"`]);
  parameters.push(limit);
  const rows = await store.connection.all<{
    message_id: string;
    seq: number;
    original_payload: string;
    text_projection: string;
  }>(
    "SELECT m.message_id, m.seq, m.original_payload, m.text_projection FROM messages m " +
      "JOIN (SELECT message_id, count(*) AS matches FROM (" + matches + ") " +
      "GROUP BY message_id) ranked ON ranked.message_id = m.message_id " +
      "ORDER BY ranked.matches DESC, m.seq DESC LIMIT ?", parameters,
  );

  const candidates: LexicalCandidate[] = rows.flatMap((row) => {
    const payload = JSON.parse(row.original_payload);
    const parts: [string, string][] = typeof payload.content === "string" ? [["/content", payload.content]] :
      Array.isArray(payload.content) ? payload.content.flatMap((b: { type?: string; text?: unknown }, i: number) =>
        b && b.type === "text" && typeof b.text === "string" ? [[`/content/${i}/text`, b.text] as [string, string]] : []) : [];
    if (!parts.length) return [];
    parts.sort((a, b) => relevance(b[1], terms) - relevance(a[1], terms));
    const [sourcePointer, text] = parts[0], excerpt = excerptForQuery(text, terms, 200);
    const contentHash = createHash("sha256").update(row.text_projection, "utf-8").digest("hex");
    return [{ messageId: row.message_id, seq: row.seq, sourcePointer, excerpt, contentHash }];
  });

  return { candidates, diagnostics: [] };
}

export { codePointLength };
