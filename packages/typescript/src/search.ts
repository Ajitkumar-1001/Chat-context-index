/**
 * search() (contracts/operations.md `search()`/`get_messages()`/`view_node()`) — mirrors
 * search.py. Never calls a model. Parameterized FTS5 MATCH expressions built from sanitized
 * tokens — never a raw/unbounded FTS5 expression.
 */

import { createHash } from "node:crypto";
import { codePointLength } from "./models.js";
import { bestAnchor, excerptForQuery, ftsTermExpression, queryTerms, relevance, textKeys } from "./relevance.js";
import { HistoryStore } from "./store.js";

const DEFAULT_LIMIT = 20;
const MAX_LINKED = 2;

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

interface CandidateRow {
  message_id: string;
  seq: number;
  original_payload: string;
  text_projection: string;
}

function textParts(payload: { content?: unknown }): [string, string][] {
  return typeof payload.content === "string" ? [["/content", payload.content]] :
    Array.isArray(payload.content) ? payload.content.flatMap((b: { type?: string; text?: unknown }, i: number) =>
      b && b.type === "text" && typeof b.text === "string" ? [[`/content/${i}/text`, b.text] as [string, string]] : []) : [];
}

/** The text part matching most `terms`, excerpted around `excerptTerms` (default: `terms`). */
function candidateFromRow(row: CandidateRow, terms: string[], excerptTerms?: string[]): LexicalCandidate | null {
  const parts = textParts(JSON.parse(row.original_payload));
  if (!parts.length) return null;
  parts.sort((a, b) => relevance(b[1], terms) - relevance(a[1], terms));
  if (!relevance(parts[0][1], terms)) return null;
  const [sourcePointer, text] = parts[0];
  return {
    messageId: row.message_id, seq: row.seq, sourcePointer,
    excerpt: excerptForQuery(text, excerptTerms ?? terms, 200),
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

/**
 * Pair top hits with the newest later message naming the hit's anchor; mirrors search.py
 * linked_corrections (plan-eng-review D4). No correction vocabulary; copies of the source and
 * messages past the snapshot are never linked; at most MAX_LINKED pairs.
 */
export async function linkedCorrections(
  store: HistoryStore, sources: LexicalCandidate[], query: string, maxSeq: number, limit: number,
): Promise<{ pairs: [LexicalCandidate, LexicalCandidate][]; limited: boolean }> {
  const tops = sources.slice(0, 4);
  if (!tops.length) return { pairs: [], limited: false };
  const queryList = queryTerms(query), queryKeys = new Set(queryList);
  // Rarity counts distinct hit texts: verbatim copies are one statement (SC-011 clarification).
  const distinctTexts = new Map(sources.map(source => [source.contentHash, source.excerpt]));
  const hitCounts = new Map<string, number>();
  for (const excerpt of distinctTexts.values()) for (const k of textKeys(excerpt)) hitCounts.set(k, (hitCounts.get(k) ?? 0) + 1);
  const payloadRows = await store.connection.all<{ message_id: string; original_payload: string }>(
    `SELECT message_id, original_payload FROM messages WHERE message_id IN (${tops.map(() => "?").join(",")})`,
    tops.map(source => source.messageId),
  );
  const payloads = new Map(payloadRows.map(row => [row.message_id, JSON.parse(row.original_payload)]));
  const anchors: [LexicalCandidate, string][] = [];
  for (const source of tops) {
    const text = new Map(textParts(payloads.get(source.messageId) ?? {})).get(source.sourcePointer) ?? "";
    const anchor = bestAnchor(text, queryKeys, hitCounts);
    if (anchor) anchors.push([source, anchor]);
  }
  if (!anchors.length) return { pairs: [], limited: false };
  const rowLimit = Math.min(128, Math.max(32, limit * 8));
  // ponytail: FTS5 walks rowid (insertion order == seq order per history) newest-first and stops at
  // the limit instead of joining and sorting every match; mirrors search.py.
  const rows = await store.connection.all<CandidateRow>(
    "SELECT m.message_id, m.seq, m.original_payload, m.text_projection FROM (" +
      "SELECT message_id FROM message_fts WHERE message_fts MATCH ? AND history_id = ? " +
      "ORDER BY rowid DESC LIMIT ?) f JOIN messages m ON m.message_id = f.message_id " +
      "WHERE m.seq <= ? ORDER BY m.seq DESC",
    [[...new Set(anchors.map(([, anchor]) => ftsTermExpression(anchor)))].join(" OR "), store.historyId, rowLimit, maxSeq],
  );
  const pairs: [LexicalCandidate, LexicalCandidate][] = [];
  const linkedIds = new Set<string>();
  for (const [source, anchor] of anchors) {
    if (pairs.length >= MAX_LINKED) break;
    for (const row of rows) { // newest first
      if (row.seq <= source.seq || linkedIds.has(row.message_id)) continue;
      const linked = candidateFromRow(row, [anchor], [anchor, ...queryList]);
      if (linked && linked.contentHash !== source.contentHash) {
        pairs.push([source, linked]);
        linkedIds.add(linked.messageId);
        break;
      }
    }
  }
  return { pairs, limited: rows.length >= rowLimit };
}

export { codePointLength };
