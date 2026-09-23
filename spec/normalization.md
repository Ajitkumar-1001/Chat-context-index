# Normalization Contract (frozen at M0)

Source: data-model.md Message identity rule; spec.md FR-002/FR-003; PRD §5 FR-02/FR-03.

## Message identity

Identity is `(history_id, source_id, external_id)` when `external_id` is supplied, else
`(history_id, source_id, idempotency_key, position_in_batch)`.

Equal `original_payload`/`text_projection` across two different identities is expected and MUST
NOT be collapsed — equal text never implies equal identity (INV-02).

## Idempotency

`ingest()`'s `request_hash` is computed over: ordered messages, source identity, adapter version,
and supplied session metadata.

- Same key + same `request_hash` → replay (`replayed=true`, no write).
- Same key + different `request_hash` → `IdempotencyConflict`, before any write.
- Existing `external_id` + changed content → `MessageConflict`, before any write.

**Check order** (contracts/operations.md `ingest()` Check order, `/speckit-clarify` CHK002): the
batch/receipt-level `IdempotencyConflict` check runs before any per-message `MessageConflict`
check — verified by Contract-Fixtures.md case I2 (a batch that reuses a key with a changed
message raises `IdempotencyConflict`, not `MessageConflict`).

## Supported input

- Native schema roles: `system`, `developer`, `user`, `assistant`, `tool`. Unknown roles require
  an explicit adapter mapping or are rejected.
- Content: a string, a documented structured-block array, or null only when a supported
  tool-call-only record makes that meaningful.
- Unknown structured blocks: stored as opaque data, marked unsupported for interpretation, never
  silently discarded.
- Adapters: OpenAI-style and Anthropic-style message lists within the explicitly documented
  supported subset.

## Batch limits

≤ 1,000 messages and ≤ 8 MiB encoded JSON per batch; ≤ 256 KiB per individual record. Validated
before opening the write transaction. Oversize/malformed input → `InputValidationError`, indexed,
before any part of that batch commits.

## Ordering and exchange grouping

`seq` is authoritative order, monotonic, history-local, inclusive ranges. Existing source IDs may
be replayed in their stored relative order followed by new records — append-only, not a
transcript-reordering API. Out-of-order historical imports retain append order and original
timestamps rather than renumbering earlier messages.

Every message belongs to exactly one `Exchange`, in original order, without modifying the
original record. `normalization_version` is shared across languages so exchange re-derivation is
deterministic.

## Unicode

Excerpt offsets count Unicode scalar values, never UTF-16 code units or encoded bytes.
TypeScript implementations MUST iterate by code point (`Array.from(string)`, `for...of`, or the
string iterator protocol) — never `string[i]`/`.length` indexing, which splits surrogate pairs.
Conformance fixture: `spec/fixtures/` Unicode case (`"A😀éZ"`, 5 scalar values).

## Raw/projection separation

Original accepted payload and original string values are stored separately from generated
summaries and deterministic text projections. Search rendering may introduce labels for tool
records but must retain a mapping back to source fields. Unknown blocks and unsupported media are
never interpreted as facts.
