/**
 * retrieve() (contracts/operations.md `retrieve()`; data-model.md Snapshot) — mirrors
 * retrieve.py, including bounded tree navigation and a provider-free lexical fallback.
 */

import { VersionConflict } from "./errors.js";
import { Diagnostic, LexicalCandidate, linkedCorrections, search } from "./search.js";
import { HistoryStore } from "./store.js";
import { BoundedProvider, CallBudget } from "./provider.js";
import { renderEvidenceContext } from "./contextAssembly.js";
import { navigateTree } from "./treeRetrieval.js";

export const CONTRACT_VERSION = 1;

/** No-op by default. Failure-injection tests monkeypatch this to pause a request between
 * Snapshot capture (lock released) and the emission-time generation check — mirrors
 * retrieve.py's `_mid_retrieve_barrier`. Exported mutable so tests can reassign it, matching the
 * Python monkeypatch pattern used by this project's failure-injection suite. */
export let midRetrieveBarrier: () => Promise<void> = async () => undefined;
export function setMidRetrieveBarrier(fn: () => Promise<void>): void {
  midRetrieveBarrier = fn;
}

export interface Snapshot {
  historyRevision: number;
  indexRevision: number;
  snapshotMaxSeq: number;
  cacheGeneration: number;
}

export interface Evidence {
  evidenceId: string;
  messageId: string;
  seq: number;
  sourcePointer: string;
  excerpt: string;
  contentHash: string;
}

export interface Coverage {
  coverageLimited: boolean;
  indexDegraded: boolean;
  omittedExcerptCount: number;
}

export interface Routing {
  requestedMode: string;
  actualMode: string;
  candidateCount: number;
  selectedChunkIds: string[];
}

export interface Usage {
  currentProviderCalls: number;
  retries: number;
  usageUnknown: boolean;
  memoHits: number;
  memoMisses: number;
  memoErrors: number;
  stageMs: Record<string, number>;
  inputTokens?: number | null;
  outputTokens?: number | null;
}

/** Evidence IDs encode retrieval priority; evidenceRank is the only parser. */
export function evidenceId(rank: number): string {
  return `ev_${rank}`;
}

export function evidenceRank(id: string): number {
  const rank = Number(id.startsWith("ev_") ? id.slice(3) : id);
  if (!Number.isInteger(rank)) throw new RangeError(`invalid evidence id: ${id}`);
  return rank;
}

export function usageFromBudget(budget: CallBudget): Usage {
  return { ...emptyUsage(), currentProviderCalls: budget.calls, retries: budget.retries,
    usageUnknown: budget.usageUnknown,
    inputTokens: budget.usageUnknown ? null : budget.inputTokens,
    outputTokens: budget.usageUnknown ? null : budget.outputTokens };
}

export function emptyUsage(): Usage {
  return { currentProviderCalls: 0, retries: 0, usageUnknown: false, memoHits: 0, memoMisses: 0, memoErrors: 0, stageMs: {} };
}

export interface RetrievalResult {
  contractVersion: number;
  historyId: string;
  status: string;
  snapshot: Snapshot;
  evidence: Evidence[];
  routing: Routing;
  coverage: Coverage;
  diagnostics: Diagnostic[];
  usage: Usage;
}

export async function captureSnapshot(store: HistoryStore): Promise<Snapshot> {
  const row = await store.connection.get<{
    history_revision: number;
    index_revision: number;
    cache_generation: number;
    seq_high_water_mark: number;
  }>("SELECT history_revision, index_revision, cache_generation, seq_high_water_mark FROM store_meta WHERE id = 1");
  if (!row) throw new Error("store_meta always has exactly one row once open() has succeeded");
  return {
    historyRevision: row.history_revision,
    indexRevision: row.index_revision,
    snapshotMaxSeq: row.seq_high_water_mark,
    cacheGeneration: row.cache_generation,
  };
}

async function hasTree(store: HistoryStore): Promise<boolean> {
  const row = await store.connection.get<{ cnt: number }>(
    "SELECT COUNT(*) as cnt FROM nodes WHERE history_id = ?",
    [store.historyId],
  );
  return (row?.cnt ?? 0) > 0;
}

export async function currentCacheGeneration(store: HistoryStore): Promise<number> {
  const row = await store.connection.get<{ cache_generation: number }>("SELECT cache_generation FROM store_meta WHERE id = 1");
  if (!row) throw new Error("store_meta always has exactly one row once open() has succeeded");
  return row.cache_generation;
}

export async function retrieve(
  store: HistoryStore,
  query: string,
  mode = "auto",
  maxSelectedChunks?: number,
  options: { provider?: BoundedProvider; deadlineS?: number; budget?: CallBudget } = {},
): Promise<RetrievalResult> {
  return (await retrieveWithLinks(store, query, mode, maxSelectedChunks, options)).result;
}

type Keyed = { messageId: string; sourcePointer: string };
const candidateKey = (c: Keyed) => JSON.stringify([c.messageId, c.sourcePointer]);

/**
 * retrieve() plus which returned evidence is a later mention linked to which source; mirrors
 * retrieve.py retrieve_with_links. Priority (plan-eng-review D17): each lexical hit, then its
 * linked later mention, then tree-only candidates in navigator order. maxSelectedChunks bounds
 * lexical and linked items only (D5); tree candidates keep their chunk and excerpt-budget bounds.
 */
export async function retrieveWithLinks(
  store: HistoryStore,
  query: string,
  mode = "auto",
  maxSelectedChunks?: number,
  options: { provider?: BoundedProvider; deadlineS?: number; budget?: CallBudget } = {},
): Promise<{ result: RetrievalResult; links: Map<string, string[]> }> {
  const limit = maxSelectedChunks ?? store.config.maxSelectedChunks;
  if (!["auto", "tree", "lexical"].includes(mode) || !Number.isInteger(limit) || limit < 1 || limit > 5000) {
    throw new RangeError("require a valid retrieval mode and 1 <= maxSelectedChunks <= 5000");
  }
  const deadlineAtMs = Date.now() + (options.deadlineS ?? store.config.requestDeadlineRetrieveS) * 1000;
  const budget = options.budget ?? new CallBudget(store.config.providerAttemptLimitRetrieve);

  const [snapshot, searchResult, linking, treeExists] = await store.withLock(async () => {
    const snap = await captureSnapshot(store);
    const sr = await search(store, query, limit);
    const linked = await linkedCorrections(store, sr.candidates, query, snap.snapshotMaxSeq, limit);
    const tree = await hasTree(store);
    return [snap, sr, linked, tree] as const;
  });

  let indexDegraded = !treeExists;
  let actualMode = "lexical";
  let diagnostics = [...searchResult.diagnostics];
  // Links follow the source text, so every verbatim copy of a superseded statement (including a
  // newer copy that outranks the linked one) carries the later mention with it.
  const linkedByText = new Map<string, LexicalCandidate[]>();
  for (const [source, linked] of linking.pairs) {
    linkedByText.set(source.contentHash, [...(linkedByText.get(source.contentHash) ?? []), linked]);
  }
  const lexical = new Map<string, LexicalCandidate>();
  const linkedTo = new Map<string, LexicalCandidate[]>();
  for (const hit of searchResult.candidates) {
    if (!lexical.has(candidateKey(hit))) lexical.set(candidateKey(hit), hit);
    for (const linked of linkedByText.get(hit.contentHash) ?? []) {
      linkedTo.set(candidateKey(hit), [...(linkedTo.get(candidateKey(hit)) ?? []), linked]);
      if (!lexical.has(candidateKey(linked))) lexical.set(candidateKey(linked), linked);
    }
  }
  let selectedChunks: string[] = [];
  let treeCandidates: LexicalCandidate[] = [];
  let limited = searchResult.candidates.length >= limit || linking.limited || lexical.size > limit;
  if (mode !== "lexical" && options.provider && treeExists && query.trim()) {
    const tree = await navigateTree(store, query, snapshot, options.provider, budget, deadlineAtMs, limit);
    treeCandidates = tree.candidates;
    selectedChunks = tree.selected;
    diagnostics.push(...tree.diagnostics);
    indexDegraded = tree.diagnostics.length > 0;
    limited ||= tree.limited;
    actualMode = selectedChunks.length || !tree.diagnostics.length ? mode : "lexical";
  } else if (mode === "tree" && treeExists && !options.provider) {
    diagnostics.push({ code: "tree_provider_missing", stage: "retrieve", retryable: false });
    indexDegraded = true;
  }
  await midRetrieveBarrier();
  const current = await store.withLock(() => captureSnapshot(store));
  if (current.cacheGeneration !== snapshot.cacheGeneration) {
    throw new VersionConflict("history was cleared between snapshot capture and result emission");
  }
  if (selectedChunks.length && current.indexRevision !== snapshot.indexRevision) {
    treeCandidates = []; selectedChunks = []; actualMode = "lexical";
    indexDegraded = true;
    diagnostics.push({ code: "tree_revision_changed", stage: "retrieve", retryable: false });
  }
  // Lexical items first (a duplicate keeps its query-centered lexical excerpt), then tree-only
  // items; a lexical item past the limit returns only if a selected chunk contains it.
  const ordered = new Map([...lexical.entries()].slice(0, limit));
  for (const candidate of selectedChunks.length ? treeCandidates : []) {
    const k = candidateKey(candidate);
    if (!ordered.has(k)) ordered.set(k, lexical.get(k) ?? candidate);
  }
  const evidence: Evidence[] = [...ordered.values()].map((c, i) => ({
    evidenceId: evidenceId(i + 1),
    messageId: c.messageId,
    seq: c.seq,
    sourcePointer: c.sourcePointer,
    excerpt: c.excerpt,
    contentHash: c.contentHash,
  }));
  const bounded: Evidence[] = [];
  for (const item of evidence) {
    if (Array.from(renderEvidenceContext([...bounded, item])).length <= store.config.maxEvidenceTextScalars) bounded.push(item);
  }
  const omitted = evidence.length - bounded.length;
  const returned = new Set(bounded.map(candidateKey));
  const links = new Map<string, string[]>();
  for (const [source, linked] of linkedTo) {
    const keys = linked.map(candidateKey).filter(k => returned.has(k));
    if (returned.has(source) && keys.length) links.set(source, keys);
  }
  bounded.sort((a, b) => a.seq - b.seq);

  const status = bounded.length > 0 ? "ok" : "empty";
  if (status === "empty" && diagnostics.length === 0) {
    diagnostics = [{ code: "no_matching_evidence", stage: "retrieve", retryable: false }];
  }

  const result: RetrievalResult = {
    contractVersion: CONTRACT_VERSION,
    historyId: store.historyId,
    status,
    snapshot,
    evidence: bounded,
    routing: { requestedMode: mode, actualMode, candidateCount: ordered.size, selectedChunkIds: selectedChunks },
    coverage: {
      coverageLimited: limited || omitted > 0,
      indexDegraded,
      omittedExcerptCount: omitted,
    },
    diagnostics,
    usage: usageFromBudget(budget),
  };
  return { result, links };
}
