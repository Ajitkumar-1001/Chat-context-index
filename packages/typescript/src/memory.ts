/** Historical context for any host chat/RAG/agent loop; never executes recorded tools. */
import { renderEvidenceContext } from "./contextAssembly.js";
import { VersionConflict } from "./errors.js";
import { BoundedProvider } from "./provider.js";
import { RetrievalResult, retrieveWithLinks, currentCacheGeneration, evidenceRank } from "./retrieve.js";
import { Message } from "./models.js";
import { HistoryStore } from "./store.js";
import { excerptForQuery, queryTerms } from "./relevance.js";

export interface ContextItem {
  messageId: string; seq: number; sourcePointer: string; excerpt: string;
}
export interface Context {
  text: string; items: ContextItem[]; omittedCandidates: number; truncatedExcerpts: number;
  tokenCount: number | null; retrieval?: RetrievalResult;
}
export interface ContextOptions {
  query?: string;
  recentMessages?: number; maxMessages?: number; maxChars?: number; excerptChars?: number;
  mode?: "auto" | "tree" | "lexical"; provider?: BoundedProvider;
  maxTokens?: number; tokenCounter?: (text: string) => number;
  /** `${messageId}:${sourcePointer}` of a candidate -> newer candidates that restate it (plan-eng-review D15). */
  links?: Map<string, string[]>;
}

export function messageItem(message: Message): ContextItem | null {
  return messageItems(message)[0] ?? null;
}

function messageItems(message: Message): ContextItem[] {
  const content = message.originalPayload.content;
  const parts: [string, string][] = typeof content === "string" ? [["/content", content]] :
    Array.isArray(content) ? content.flatMap((b, i) => b && b.type === "text" && typeof b.text === "string" ? [[`/content/${i}/text`, b.text] as [string, string]] : []) : [];
  return parts.filter(([, text]) => text).map(([sourcePointer, excerpt]) => ({ messageId: message.messageId, seq: message.seq, sourcePointer, excerpt }));
}

function render(items: ContextItem[]): string {
  return renderEvidenceContext([...items].sort((a, b) => a.seq - b.seq).map(item => ({
    evidenceId: item.messageId, sourcePointer: item.sourcePointer, excerpt: item.excerpt,
  })));
}

export function packContext(candidates: ContextItem[], options: ContextOptions = {}): Context {
  const maxMessages = options.maxMessages ?? 8, maxChars = options.maxChars ?? 4000, excerptChars = options.excerptChars ?? 200;
  if (![maxMessages, maxChars, excerptChars].every(n => Number.isInteger(n) && n > 0)) throw new RangeError("context limits must be positive integers");
  if ((options.maxTokens === undefined) !== (options.tokenCounter === undefined) || (options.maxTokens !== undefined && options.maxTokens <= 0)) {
    throw new RangeError("supply a positive maxTokens and the model tokenCounter together");
  }
  const unique = new Map<string, ContextItem>();
  for (const item of candidates) {
    const key = `${item.messageId}:${item.sourcePointer}`;
    if (!unique.has(key)) unique.set(key, item);
  }
  const selected: ContextItem[] = [];
  const admitted = new Set<string>(), considered = new Set<string>();
  const terms = queryTerms(options.query ?? "");
  let truncated = 0;
  const admit = (keys: string[]): boolean => {
    const items = keys.map(k => unique.get(k)!);
    const bounded = items.map(item => ({ ...item, excerpt: excerptForQuery(item.excerpt, terms, excerptChars) }));
    const text = render([...selected, ...bounded]);
    if (selected.length + bounded.length > maxMessages || Array.from(text).length > maxChars ||
      (options.tokenCounter && options.tokenCounter(text) > options.maxTokens!)) return false;
    selected.push(...bounded);
    keys.forEach(k => admitted.add(k));
    bounded.forEach((b, i) => { if (b.excerpt !== items[i].excerpt) truncated++; });
    return true;
  };
  // A source with newer restatements is packed with them; when both cannot fit, only the newer ones
  // are packed, and the source is never packed without them (mirrors memory.py pack_context).
  for (const key of unique.keys()) {
    if (selected.length >= maxMessages) break;
    if (considered.has(key)) continue;
    const newer = (options.links?.get(key) ?? []).filter(k => unique.has(k) && k !== key);
    considered.add(key);
    if (newer.some(k => considered.has(k) && !admitted.has(k))) continue;
    const pending = newer.filter(k => !admitted.has(k));
    if (admit([key, ...pending])) { pending.forEach(k => considered.add(k)); continue; }
    for (const k of [...pending].sort((a, b) => unique.get(b)!.seq - unique.get(a)!.seq)) {
      considered.add(k);
      admit([k]);
    }
  }
  selected.sort((a, b) => a.seq - b.seq);
  const text = render(selected);
  return { text, items: selected, omittedCandidates: unique.size - selected.length,
    truncatedExcerpts: truncated, tokenCount: options.tokenCounter ? options.tokenCounter(text) : null };
}

export async function prepareContext(store: HistoryStore, query: string, options: ContextOptions = {}): Promise<Context> {
  const recentMessages = options.recentMessages ?? 4, maxMessages = options.maxMessages ?? 8;
  if (!Number.isInteger(recentMessages) || recentMessages < 0 || recentMessages > maxMessages || maxMessages > 5000) {
    throw new RangeError("require 0 <= recentMessages <= maxMessages <= 5000");
  }
  packContext([], options); // Validate before spending on navigation.
  const { result, links } = await retrieveWithLinks(store, query, options.mode ?? "auto", maxMessages, { provider: options.provider });
  const end = result.snapshot.snapshotMaxSeq;
  const recent = recentMessages ? await store.getMessages(Math.max(1, end - recentMessages + 1), end, recentMessages) : [];
  const candidates = recent.reverse().flatMap(messageItems);
  // Retrieval IDs retain selection priority after evidence is rendered chronologically.
  const ranked = [...result.evidence].sort((a, b) => evidenceRank(a.evidenceId) - evidenceRank(b.evidenceId));
  const groups = new Map<string, typeof ranked>();
  for (const evidence of ranked) {
    const key = JSON.stringify([evidence.contentHash, evidence.sourcePointer]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(evidence);
  }
  const first: typeof ranked = [], copies: typeof ranked = [];
  const representative = new Map<string, string>();
  const packKey = (e: { messageId: string; sourcePointer: string }) => `${e.messageId}:${e.sourcePointer}`;
  for (const group of groups.values()) {
    const original = group.reduce((a, b) => a.seq <= b.seq ? a : b);
    first.push(original);
    copies.push(...group.filter(evidence => evidence !== original));
    for (const evidence of group) representative.set(JSON.stringify([evidence.messageId, evidence.sourcePointer]), packKey(original));
  }
  candidates.push(...[...first, ...copies].map(e => ({ messageId: e.messageId, seq: e.seq, sourcePointer: e.sourcePointer, excerpt: e.excerpt })));
  // A copy's newer statement also governs the copy chosen to represent it.
  const packLinks = new Map<string, string[]>();
  for (const [source, newer] of links) {
    const from = representative.get(source)!, targets = packLinks.get(from) ?? [];
    for (const k of newer) { const to = representative.get(k)!; if (!targets.includes(to)) targets.push(to); }
    packLinks.set(from, targets);
  }
  const context = packContext(candidates, { ...options, query, links: packLinks });
  if (await currentCacheGeneration(store) !== result.snapshot.cacheGeneration) throw new VersionConflict("history was cleared while preparing conversation context");
  return { ...context, retrieval: result };
}
