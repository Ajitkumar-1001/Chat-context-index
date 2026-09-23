/** Historical context for any host chat/RAG/agent loop; never executes recorded tools. */
import { renderEvidenceContext } from "./contextAssembly.js";
import { VersionConflict } from "./errors.js";
import { BoundedProvider } from "./provider.js";
import { RetrievalResult, retrieve, currentCacheGeneration } from "./retrieve.js";
import { Message } from "./models.js";
import { HistoryStore } from "./store.js";

export interface ContextItem {
  messageId: string; seq: number; sourcePointer: string; excerpt: string;
}
export interface Context {
  text: string; items: ContextItem[]; omittedCandidates: number; truncatedExcerpts: number;
  tokenCount: number | null; retrieval?: RetrievalResult;
}
export interface ContextOptions {
  recentMessages?: number; maxMessages?: number; maxChars?: number; excerptChars?: number;
  mode?: "auto" | "tree" | "lexical"; provider?: BoundedProvider;
  maxTokens?: number; tokenCounter?: (text: string) => number;
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
  let truncated = 0;
  for (const item of unique.values()) {
    if (selected.length >= maxMessages) break;
    const excerpt = Array.from(item.excerpt).slice(0, excerptChars).join("");
    const bounded = { ...item, excerpt };
    const text = render([...selected, bounded]);
    if (Array.from(text).length > maxChars || (options.tokenCounter && options.tokenCounter(text) > options.maxTokens!)) continue;
    selected.push(bounded);
    if (excerpt !== item.excerpt) truncated++;
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
  const result = await retrieve(store, query, options.mode ?? "auto", maxMessages, { provider: options.provider });
  const end = result.snapshot.snapshotMaxSeq;
  const recent = recentMessages ? await store.getMessages(Math.max(1, end - recentMessages + 1), end, recentMessages) : [];
  const candidates = recent.reverse().flatMap(messageItems);
  candidates.push(...result.evidence.map(e => ({ messageId: e.messageId, seq: e.seq, sourcePointer: e.sourcePointer, excerpt: e.excerpt })));
  const context = packContext(candidates, options);
  if (await currentCacheGeneration(store) !== result.snapshot.cacheGeneration) throw new VersionConflict("history was cleared while preparing conversation context");
  return { ...context, retrieval: result };
}
