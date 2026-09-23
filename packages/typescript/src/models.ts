/**
 * Message and IngestReceipt models (spec/normalization.md; data-model.md) — mirrors models.py.
 *
 * Identity rule, quoted verbatim (data-model.md): "identity is (history_id, source_id,
 * external_id) when external_id is supplied, else (history_id, source_id, idempotency_key,
 * position_in_batch). Equal original_payload/text_projection across two different identities
 * is expected and MUST NOT be collapsed." (INV-02)
 *
 * Offset computation (T074; contracts/result-schemas.md TypeScript implementation note):
 * JavaScript strings are UTF-16-indexed by default — `.length`/naive indexing splits surrogate
 * pairs and produces the wrong offset. Every length/offset computation here iterates by code
 * point (`Array.from(string)`), never by UTF-16 code unit. Python's `len(str)` already counts
 * Unicode scalar values natively, which is why models.py needs no equivalent special-casing —
 * this is the one place the two languages' string semantics genuinely diverge.
 */

import { createHash } from "node:crypto";

export const SUPPORTED_ROLES = new Set(["system", "developer", "user", "assistant", "tool"]);

// Batch limits (data-model.md, quoted verbatim): "<= 1,000 messages and <= 8 MiB encoded JSON
// per batch, <= 256 KiB per individual record."
export const MAX_MESSAGES_PER_BATCH = 1_000;
export const MAX_BATCH_BYTES = 8 * 1024 * 1024;
export const MAX_RECORD_BYTES = 256 * 1024;

export type ContentBlock = { type?: string; text?: string; [key: string]: unknown };
export type MessageContent = string | ContentBlock[] | null;

export interface InputMessage {
  role: string;
  content: MessageContent;
  externalId?: string | null;
  metadata?: Record<string, unknown> | null;
  toolCalls?: Record<string, unknown>[] | null;
  toolCallId?: string | null;
}

/** Codepoint-safe length — the TypeScript-specific counterpart to Python's native `len(str)`. */
export function codePointLength(text: string): number {
  return Array.from(text).length;
}

export function messagePayload(m: InputMessage): Record<string, unknown> {
  const payload: Record<string, unknown> = { role: m.role, content: m.content };
  if (m.metadata != null) payload.metadata = m.metadata;
  if (m.toolCalls != null) payload.tool_calls = m.toolCalls;
  if (m.toolCallId != null) payload.tool_call_id = m.toolCallId;
  return payload;
}

/** Deterministic, recursively-sorted-key JSON — matches models.py's `canonical_payload_json`. */
export function canonicalPayloadJson(payload: unknown): string {
  return JSON.stringify(sortKeysDeep(payload));
}

function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeysDeep);
  if (value !== null && typeof value === "object") {
    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(value as object).sort()) {
      sorted[key] = sortKeysDeep((value as Record<string, unknown>)[key]);
    }
    return sorted;
  }
  return value;
}

export function renderTextProjection(content: MessageContent): string | null {
  if (content == null) return null;
  if (typeof content === "string") return content;
  const parts = content
    .filter((block) => block && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text as string);
  return parts.length > 0 ? parts.join("\n") : null;
}

/**
 * Maps a codepoint offset within `renderTextProjection(content)` back to the original field it
 * came from (contracts/result-schemas.md `Evidence.source_pointer`).
 */
export function sourcePointerForOffset(content: MessageContent, offset: number): string {
  if (Array.isArray(content)) {
    let cursor = 0;
    for (let i = 0; i < content.length; i++) {
      const block = content[i];
      if (block && block.type === "text" && typeof block.text === "string") {
        const len = codePointLength(block.text);
        if (cursor <= offset && offset < cursor + len) {
          return `/content/${i}/text`;
        }
        cursor += len + 1; // +1 for the joining "\n" (itself exactly 1 code point)
      }
    }
  }
  return "/content";
}

export function payloadHash(originalPayload: unknown): string {
  return createHash("sha256").update(canonicalPayloadJson(originalPayload)).digest("hex");
}

export interface Message {
  messageId: string;
  seq: number;
  historyId: string;
  sourceId: string;
  externalId: string | null;
  idempotencyKey: string | null;
  positionInBatch: number | null;
  role: string;
  originalPayload: Record<string, unknown>;
  textProjection: string | null;
  payloadHash: string;
  sessionMetadata: Record<string, unknown> | null;
}

export interface IngestReceipt {
  historyId: string;
  sourceId: string;
  idempotencyKey: string;
  requestHash: string;
  insertedSeqStart: number | null;
  insertedSeqEnd: number | null;
  skippedCount: number;
  unsupportedBlockCount: number;
  indexingStatus: string;
  replayed: boolean;
}

export function computeRequestHash(
  messages: InputMessage[],
  sourceId: string,
  adapterVersion: string,
  sessionMetadata: Record<string, unknown> | null | undefined,
): string {
  const canonical = canonicalPayloadJson({
    messages: messages.map((m) => ({
      role: m.role,
      content: m.content,
      external_id: m.externalId ?? null,
      metadata: m.metadata ?? null,
    })),
    source_id: sourceId,
    adapter_version: adapterVersion,
    session_metadata: sessionMetadata ?? null,
  });
  return createHash("sha256").update(canonical).digest("hex");
}
