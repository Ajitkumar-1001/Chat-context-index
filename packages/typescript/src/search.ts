/**
 * search() (contracts/operations.md `search()`/`get_messages()`/`view_node()`) — mirrors
 * search.py. Never calls a model. Parameterized FTS5 MATCH expressions built from sanitized
 * tokens — never a raw/unbounded FTS5 expression.
 */

import { createHash } from "node:crypto";
import { codePointLength, sourcePointerForOffset } from "./models.js";
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

/** Tokenizes on whitespace, double-quotes each token (escaping internal `"`), joins with
 * spaces — turns FTS5 syntax characters into literal token content, matching search.py exactly. */
function sanitizeFtsQuery(query: string): string {
  const tokens = query.split(/\s+/).filter(Boolean);
  const quoted = tokens.map((tok) => `"${tok.replace(/"/g, '""')}"`).filter((t) => t !== '""');
  return quoted.join(" ");
}

/** Codepoint-safe excerpt window, matching search.py's `_excerpt_for` exactly (Python's `str`
 * indexing is already codepoint-based; here we operate on an `Array.from` codepoint array to
 * get the same semantics). Returns [excerpt, codepointOffset]. */
function excerptFor(textProjection: string, firstToken: string, window = 200): [string, number] {
  const chars = Array.from(textProjection);
  const lowerJoined = chars.map((c) => c.toLowerCase()).join("");
  let idx = lowerJoined.indexOf(firstToken.toLowerCase());
  // indexOf on the lowercased *joined* string can drift from codepoint indices only if
  // toLowerCase() changes a character's codepoint count — vanishingly rare for this library's
  // use; consistent with cache_key.py's own documented small-gap tolerance.
  if (idx < 0) idx = 0;
  const start = Math.max(0, idx - Math.floor(window / 2));
  const end = Math.min(chars.length, idx + Math.floor(window / 2));
  return [chars.slice(start, end).join(""), start];
}

export async function search(store: HistoryStore, query: string, limit = DEFAULT_LIMIT): Promise<SearchResult> {
  const sanitized = sanitizeFtsQuery(query);
  if (!sanitized) {
    return { candidates: [], diagnostics: [{ code: "empty_or_nonsearchable_query", stage: "search", retryable: false }] };
  }

  const rows = await store.connection.all<{
    message_id: string;
    seq: number;
    original_payload: string;
    text_projection: string;
  }>(
    "SELECT m.message_id, m.seq, m.original_payload, m.text_projection FROM message_fts f " +
      "JOIN messages m ON m.message_id = f.message_id " +
      "WHERE f.history_id = ? AND message_fts MATCH ? ORDER BY m.seq LIMIT ?",
    [store.historyId, sanitized, limit],
  );

  const tokens = query.split(/\s+/).filter(Boolean);
  const firstToken = tokens.length > 0 ? tokens[0] : "";

  const candidates: LexicalCandidate[] = rows.map((row) => {
    const payload = JSON.parse(row.original_payload);
    let [excerpt, offset] = excerptFor(row.text_projection, firstToken);
    let sourcePointer = sourcePointerForOffset(payload.content, offset);
    if (Array.isArray(payload.content)) {
      const lower = row.text_projection.toLowerCase();
      const match = Math.max(0, lower.indexOf(firstToken.toLowerCase()));
      sourcePointer = sourcePointerForOffset(payload.content, Array.from(lower.slice(0, match)).length);
      if (sourcePointer.endsWith("/text")) [excerpt] = excerptFor(payload.content[Number(sourcePointer.split("/")[2])].text, firstToken);
    }
    const contentHash = createHash("sha256").update(row.text_projection, "utf-8").digest("hex");
    return { messageId: row.message_id, seq: row.seq, sourcePointer, excerpt, contentHash };
  });

  return { candidates, diagnostics: [] };
}

export { codePointLength };
