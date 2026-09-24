/**
 * search() (contracts/operations.md `search()`/`get_messages()`/`view_node()`) — mirrors
 * search.py. Never calls a model. Parameterized FTS5 MATCH expressions built from sanitized
 * tokens — never a raw/unbounded FTS5 expression.
 */

import { createHash } from "node:crypto";
import { codePointLength } from "./models.js";
import { excerptForQuery, ftsTermExpression, queryTerms, relevance } from "./relevance.js";
import { HistoryStore } from "./store.js";

const DEFAULT_LIMIT = 20;
const CORRECTION_TERMS = queryTerms("correction corrected reconsidered revised instead retired change changed moved actually superseded");

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

function isCorrection(text: string): boolean {
  return relevance(text, CORRECTION_TERMS) > 0;
}

function sourceAnchor(source: LexicalCandidate, queryKeys: Set<string>): string | undefined {
  const eligible: { term: string; named: boolean; position: number }[] = [];
  for (const match of source.excerpt.matchAll(/[\p{L}\p{N}]+/gu)) {
    const raw = match[0], term = queryTerms(raw)[0];
    if (!term || queryKeys.has(term) || (Array.from(term).length < 5 && !/\p{N}/u.test(term))) continue;
    eligible.push({ term, named: /\p{Lu}/u.test(raw), position: match.index ?? 0 });
  }
  eligible.sort((a, b) => Number(b.named) - Number(a.named) || b.position - a.position);
  return eligible[0]?.term;
}

interface CandidateRow {
  message_id: string;
  seq: number;
  original_payload: string;
  text_projection: string;
}

function candidateFromRow(row: CandidateRow, terms: string[]): LexicalCandidate | null {
  const payload = JSON.parse(row.original_payload);
  const parts: [string, string][] = typeof payload.content === "string" ? [["/content", payload.content]] :
    Array.isArray(payload.content) ? payload.content.flatMap((b: { type?: string; text?: unknown }, i: number) =>
      b && b.type === "text" && typeof b.text === "string" ? [[`/content/${i}/text`, b.text] as [string, string]] : []) : [];
  if (!parts.length) return null;
  parts.sort((a, b) => relevance(b[1], terms) - relevance(a[1], terms));
  if (!relevance(parts[0][1], terms)) return null;
  const [sourcePointer, text] = parts[0];
  return {
    messageId: row.message_id, seq: row.seq, sourcePointer,
    excerpt: excerptForQuery(text, terms, 200),
    contentHash: createHash("sha256").update(row.text_projection, "utf-8").digest("hex"),
  };
}

export async function search(store: HistoryStore, query: string, limit = DEFAULT_LIMIT): Promise<SearchResult> {
  if (!Number.isInteger(limit) || limit < 1 || limit > 5000) throw new RangeError("require 1 <= search limit <= 5000");
  const terms = queryTerms(query);
  if (!terms.length) {
    return { candidates: [], diagnostics: [{ code: "empty_or_nonsearchable_query", stage: "search", retryable: false }] };
  }

  const matches = terms.map(() => "SELECT message_id FROM message_fts WHERE history_id = ? AND message_fts MATCH ?").join(" UNION ALL ");
  const parameters: (string | number)[] = terms.flatMap(term => [store.historyId, ftsTermExpression(term)]);
  parameters.push(limit);
  const rows = await store.connection.all<CandidateRow>(
    "SELECT m.message_id, m.seq, m.original_payload, m.text_projection FROM messages m " +
      "JOIN (SELECT message_id, count(*) AS matches FROM (" + matches + ") " +
      "GROUP BY message_id) ranked ON ranked.message_id = m.message_id " +
      "ORDER BY ranked.matches DESC, m.seq DESC LIMIT ?", parameters,
  );

  const candidates: LexicalCandidate[] = rows.flatMap(row => candidateFromRow(row, terms) ?? []);

  return { candidates, diagnostics: [] };
}

export async function linkedCorrections(
  store: HistoryStore, sources: LexicalCandidate[], query: string, maxSeq: number, limit: number,
): Promise<{ candidates: LexicalCandidate[]; limited: boolean }> {
  const queryKeys = new Set(queryTerms(query));
  const anchorsBySource = sources.slice(0, 4).map(source => {
    const anchor = sourceAnchor(source, queryKeys);
    return { seq: source.seq, terms: anchor ? [anchor] : [] };
  });
  const anchors = [...new Set(anchorsBySource.flatMap(source => source.terms))];
  if (!anchors.length) return { candidates: [], limited: false };
  const expression = `(${anchors.map(ftsTermExpression).join(" OR ")}) AND (${CORRECTION_TERMS.map(ftsTermExpression).join(" OR ")})`;
  const rowLimit = Math.min(128, Math.max(32, limit * 8));
  const rows = await store.connection.all<CandidateRow>(
    "SELECT m.message_id, m.seq, m.original_payload, m.text_projection FROM message_fts " +
      "JOIN messages m ON m.message_id = message_fts.message_id " +
      "WHERE message_fts.history_id = ? AND message_fts MATCH ? AND m.seq <= ? " +
      "ORDER BY m.seq DESC LIMIT ?",
    [store.historyId, expression, maxSeq, rowLimit],
  );
  const candidates = rows.flatMap(row => {
    const candidate = candidateFromRow(row, [...anchors, ...CORRECTION_TERMS]);
    return candidate && isCorrection(candidate.excerpt) && anchorsBySource.some(source =>
      candidate.seq > source.seq && relevance(candidate.excerpt, source.terms)) ? [candidate] : [];
  });
  return { candidates, limited: rows.length >= rowLimit };
}

export { codePointLength };
