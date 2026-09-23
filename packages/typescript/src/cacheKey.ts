/**
 * Shared cache-key derivation (spec/cache-format.md; PRD §8.1) — mirrors cache_key.py exactly.
 * INV-08: this formula has no language-specific step — the same canonical request and scope
 * MUST produce the same key in Python and TypeScript (verified by T077's conformance test
 * against cache_key.py's identical canonicalization, using the shared mutation-pairs fixture).
 *
 * Canonicalization: recursively sort object keys, compact separators, UTF-8 output, reject
 * non-finite numbers before hashing — the same JCS-adjacent scheme as cache_key.py (not strict
 * RFC 8785 for astral-plane key names — an already-documented, shared gap, not a new one).
 */

import { createHash } from "node:crypto";
import { InputValidationError } from "./errors.js";

const CACHE_KEY_PREFIX = "cci:memo:v2:";

function rejectNonFinite(obj: unknown): void {
  if (typeof obj === "number") {
    if (!Number.isFinite(obj)) {
      throw new InputValidationError(`non-finite number in cache-key input: ${obj}`);
    }
  } else if (Array.isArray(obj)) {
    for (const v of obj) rejectNonFinite(v);
  } else if (obj !== null && typeof obj === "object") {
    for (const v of Object.values(obj as Record<string, unknown>)) rejectNonFinite(v);
  }
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

/** JCS-style canonical serialization: recursively sorted object keys, no whitespace, UTF-8. */
export function canonicalize(obj: unknown): Buffer {
  rejectNonFinite(obj);
  const canonicalText = JSON.stringify(sortKeysDeep(obj));
  return Buffer.from(canonicalText, "utf-8");
}

function sha256Hex(data: Buffer): string {
  return createHash("sha256").update(data).digest("hex");
}

export function scopeDigest(
  applicationNamespace: string,
  storeInstanceId: string,
  historyId: string,
  cacheGeneration: number,
): string {
  const payload = {
    application_namespace: applicationNamespace,
    store_instance_id: storeInstanceId,
    history_id: historyId,
    cache_generation: cacheGeneration,
  };
  return sha256Hex(canonicalize(payload));
}

export function requestDigest(effectiveRequest: Record<string, unknown>): string {
  return sha256Hex(canonicalize(effectiveRequest));
}

export function cacheKey(scopeDigestHex: string, requestDigestHex: string): string {
  return `${CACHE_KEY_PREFIX}${scopeDigestHex}:${requestDigestHex}`;
}
