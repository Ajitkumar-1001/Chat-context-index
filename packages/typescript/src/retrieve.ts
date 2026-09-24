/**
 * retrieve() (contracts/operations.md `retrieve()`; data-model.md Snapshot) — mirrors
 * retrieve.py, including bounded tree navigation and a provider-free lexical fallback.
 */

import { VersionConflict } from "./errors.js";
import { Diagnostic, search } from "./search.js";
import { HistoryStore } from "./store.js";
import { BoundedProvider, CallBudget } from "./provider.js";
import { renderEvidenceContext } from "./contextAssembly.js";
import { navigateTree } from "./treeRetrieval.js";
import { queryTerms, relevance } from "./relevance.js";

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
  const limit = maxSelectedChunks ?? store.config.maxSelectedChunks;
  if (!["auto", "tree", "lexical"].includes(mode) || !Number.isInteger(limit) || limit < 1 || limit > 5000) {
    throw new RangeError("require a valid retrieval mode and 1 <= maxSelectedChunks <= 5000");
  }
  const deadlineAtMs = Date.now() + (options.deadlineS ?? store.config.requestDeadlineRetrieveS) * 1000;
  const budget = options.budget ?? new CallBudget(store.config.providerAttemptLimitRetrieve);

  const [snapshot, searchResult, treeExists] = await store.withLock(async () => {
    const snap = await captureSnapshot(store);
    const sr = await search(store, query, limit);
    const tree = await hasTree(store);
    return [snap, sr, tree] as const;
  });

  let indexDegraded = !treeExists;
  let actualMode = "lexical";

  let diagnostics = [...searchResult.diagnostics];
  let candidates = [...searchResult.candidates];
  let selectedChunks: string[] = [];
  let limited = candidates.length >= limit;
  if (mode !== "lexical" && options.provider && treeExists && query.trim()) {
    const tree = await navigateTree(store, query, snapshot, options.provider, budget, deadlineAtMs, limit);
    candidates = [...tree.candidates, ...candidates];
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
    candidates = [...searchResult.candidates]; selectedChunks = []; actualMode = "lexical";
    indexDegraded = true;
    diagnostics.push({ code: "tree_revision_changed", stage: "retrieve", retryable: false });
  }
  const unique = new Map<string, typeof candidates[number]>();
  const terms = queryTerms(query);
  candidates = candidates.map(candidate => ({ candidate, score: relevance(candidate.excerpt, terms) }))
    .sort((a, b) => b.score - a.score || b.candidate.seq - a.candidate.seq).map(({ candidate }) => candidate);
  for (const c of candidates) if (!unique.has(`${c.messageId}:${c.sourcePointer}`)) unique.set(`${c.messageId}:${c.sourcePointer}`, c);
  const evidence: Evidence[] = [...unique.values()].map((c, i) => ({
    evidenceId: `ev_${i + 1}`,
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
  bounded.sort((a, b) => a.seq - b.seq);

  const status = bounded.length > 0 ? "ok" : "empty";
  if (status === "empty" && diagnostics.length === 0) {
    diagnostics = [{ code: "no_matching_evidence", stage: "retrieve", retryable: false }];
  }

  return {
    contractVersion: CONTRACT_VERSION,
    historyId: store.historyId,
    status,
    snapshot,
    evidence: bounded,
    routing: { requestedMode: mode, actualMode, candidateCount: unique.size, selectedChunkIds: selectedChunks },
    coverage: {
      coverageLimited: limited || omitted > 0,
      indexDegraded,
      omittedExcerptCount: omitted,
    },
    diagnostics,
    usage: usageFromBudget(budget),
  };
}
