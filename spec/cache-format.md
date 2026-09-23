# Cache Format Contract (frozen at M0)

Source: contracts/result-schemas.md Cache key section (PRD §8.1); data-model.md Cached
Computation entity. This is the shared, cross-language-identical memo format (INV-08).

## Cache key

```text
key = "cci:memo:v2:" + scope_digest + ":" + request_digest

scope_digest = SHA256(JCS({
  application_namespace,
  store_instance_id,
  history_id,
  cache_generation
}))

request_digest = SHA256(JCS(effective_request))
```

The `v2` prefix is deliberately incompatible with an earlier Python-specific JSON memo format
(ADR-004) — a different key derivation on purpose, not an oversight. Old-format entries are
treated as misses, never heuristically converted.

`effective_request` includes: operation + operation version, prompt version,
normalization/rendering version, output-schema digest, provider/deployment identity, model,
configured `deployment_revision`, all effective generation settings, complete messages, response
format, supplied tool definitions/context.

`effective_request` excludes: nondeterministic transport IDs, API keys, request timestamps that
don't affect behavior.

## Canonicalization (JCS, RFC 8785)

- Defaults are filled before hashing (omission vs. explicit null is not conflated unless the
  shared schema explicitly allows the equivalence).
- Non-finite numbers, invalid Unicode, and ambiguous duplicate JSON keys are rejected before memo
  lookup/write.
- Identities and numeric values requiring precision beyond the safe JSON integer domain are
  represented as strings.
- No lowercasing, trimming, or paraphrasing of query content to manufacture additional hits.

This formula has no language-specific step — the same canonical request and scope MUST produce
the same key in Python and TypeScript (INV-08).

## Cached value

Stored fields: `cache_format_version`, `scope_digest`, `request_digest`, operation/schema
versions, creation time, `absolute_expiry`, the validated payload, and optional original provider
usage (`reused_operation_usage`).

A hit requires: compatible versions, matching digests, unexpired data, valid payload schema, and a
still-current history/cache generation (INV-01, INV-11).

## Eligibility (INV-06, INV-07)

Cacheable: classification, summaries, and routing results, only when their complete effective
inputs can be represented in the key.

Never cached: final answers, failed requests, incomplete streams, rate-limit errors, malformed
results, user authorization decisions.

A changed effective input, model, prompt version, or operation version produces a different
`request_digest` — a structural miss, not something the system has to separately detect and
invalidate after the fact.

## Refill mechanism (data-model.md Cached Computation)

A SQLite-fallback hit MAY refill Redis, but only for the *remaining* time until the entry's
original `absolute_expiry` — never a fresh full-lifetime write. Worked example: an entry created
at t=100 with `absolute_expiry=160`; a Redis-miss/SQLite-hit at t=150 refills Redis with ≤10
seconds remaining, not a new 86,400 s default; at t=161 neither backend supplies an eligible hit.

## Config defaults (contracts/result-schemas.md Config defaults — proposed, code-enforced, not
immutable)

| Setting | Default |
|---|---|
| Cache backend | `sqlite` |
| Redis fallback | `sqlite` when Redis mode selected |
| Memo lifetime | 86,400 s |
| Cache operation timeout | 100 ms |
| Total cache overhead budget per memoized op | 250 ms |
| Redis circuit breaker | Open after 3 failures; probe after 30 s |

## Purge scoping (FR-009, `clear_history()`)

Redis cleanup uses bounded cursor iteration over the owned prefix and bounded deletion batches —
never `KEYS *`, `FLUSHDB`, or `FLUSHALL`. A timeout is never reported as a completed purge.
