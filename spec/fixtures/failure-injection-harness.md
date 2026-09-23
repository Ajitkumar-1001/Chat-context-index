# Failure-Injection Harness Design (frozen at M0)

Source: `vault-context/05-Implementation/Failure-Injection.md` (PRD §5, §8–10, §15). Proposed
fault scenarios and test procedure — no harness or results exist yet. Companion to
`spec/fixtures/` (case data) and the milestone gates in `Implementation-Plan.md`. Adds test
*procedure* to the behavior already specified in `contracts/operations.md` and
`data-model.md`.

## Harness boundaries

- Use a disposable **real** SQLite store and a deterministic provider double. A mock cannot
  prove real commit/WAL/FTS behavior.
- Control operation ordering with explicit barriers around transaction commit, provider
  completion, and index publication — never a timing race based only on `sleep`.
- Test process termination in a **child process**, so restart assertions inspect actual
  committed on-disk storage, not in-memory state. A process-crash test does not establish
  power-loss durability.
- Redis integration uses a disposable **real** Redis deployment plus bounded connection/command
  fault injection. A mock can test control flow but cannot prove Redis expiry, reconnection, or
  command behavior.
- Keep injected failures and clocks reproducible; record the configured deadline and cache
  budget for every run (PRD §15).
- Never test against a shared production cache.

## M1 — History must survive failure

| Case | Inject at this boundary | Required observation |
|---|---|---|
| **F1 — Commit ambiguity** | In separate runs: terminate before ingestion commit, and terminate after commit but before the receipt reaches the caller. Reopen and retry the same batch key. | Before commit: batch absent, retry inserts once. After commit: batch complete, retry replays its receipt (`ingest()`'s crash-recovery contract, contracts/operations.md). Raw messages, FTS, receipt, revision, and pending-index state agree; no partial batch (AT-02, AT-04). |
| **F2 — Storage failure** | Fail a history write, or simulate full storage, during a bounded ingestion batch. Separately test a corrupted store. | Typed storage failure (`StoreError`/`StoreCorrupt`); never a successful receipt or fake empty history. Rejected writes do not expose a partial batch. Cache health cannot mask authoritative-store failure (AT-04, AT-12). |
| **F3 — Incompatible open** | Present a newer-unsupported schema, and separately a rejected runtime/FTS configuration. | `SchemaVersionError` or `RuntimeCompatibilityError` as appropriate; inspect the disposable store before/after and verify no history/schema mutation or silent reset (AT-12). |

## M2 — Index and retrieval snapshots stay valid

1. **Stale proposal (F4, AT-07/12)**: pause indexing after its model input captures
   generation/history/index revisions. Commit an ingestion, then release the proposal. It cannot
   publish against the old revision tuple. Bounded recomputation or `VersionConflict` is
   permitted; newly committed raw history remains searchable. Terminate a separate run before
   publication and verify the previous committed tree still covers its original range.
2. **Retrieval during append/reindex (F5, AT-08/10)**: capture lexical candidates and
   `snapshot_max_seq`, pause routing, then append a message and publish a newer index. Resume
   routing. The result excludes the new sequence; later tree reads that detect a different index
   revision discard nominations, use captured lexical candidates, and report
   `tree_revision_changed`. Raw source lookups remain within the captured snapshot.
3. **Provider failure by stage (F6, AT-08/14)**: fail indexing, routing, and synthesis in separate
   runs. Index failure preserves committed messages and pending coverage; routing failure
   discloses lexical fallback; synthesis failure after permitted retries raises a typed provider
   error. Do not turn every provider failure into the same empty result.
4. **Deadline/cancellation (F7, AT-14)**: exhaust a request while waiting for a semaphore, cache
   attempt, retry, or provider result. Assert no new dispatch after the limit, no cache entry from
   incomplete output, no unvalidated answer. Usable retrieval may be partial; no useful work
   yields `BudgetExceeded`. Cancellation propagates and releases request resources;
   already-dispatched provider work may still be billed.

## M3 — Cache failure cannot rewrite history

| Case | Concrete experiment | Required observation |
|---|---|---|
| **F8 — Original expiry** | Create a valid memo at controlled time 100 with absolute expiry 160. At time 150, miss Redis and hit SQLite fallback; then read at 161. | Refill lifetime is at most the remaining 10 seconds (spec/cache-format.md Refill mechanism). At 161 neither backend supplies an eligible hit. Fallback never resets the lifetime (AT-15/16). |
| **F9 — Slow/corrupt Redis** | Separately stall a command, reject authentication, evict an entry, and replace its payload with a malformed or wrong-scope value. | Operational errors/malformed records bypassed within the configured total cache budget. A valid SQLite fallback or fresh computation produces the expected fake-provider result. A legitimate miss does not count as a circuit-breaker failure; history is unchanged (AT-16). |
| **F10 — Clear during work** | Pause local work (readers: model routing/synthesis, per case 1/2 of the generation lifecycle — see `spec/storage-format.md`'s in-flight-writes note for the writer case, F-analog below), request clear with the exact history ID, make Redis unreachable. Allow quiescence to settle; attempt completion from the retired generation. Reconnect later. | Logical clear gates new work and rotates generation atomically. Late reader/tree-publish work cannot publish retired content. Report `logical_clear_complete=true` and `cache_purge_pending=true`; a durable pending scope permits later bounded cleanup. Unrelated Redis scopes survive (AT-18). |
| **F11 — Sequence after clear** | Record the highest committed sequence, clear, then ingest again. | New sequence values exceed the old high-water mark. Old raw records, FTS results, and cached-generation content cannot be retrieved through the library (AT-18). |
| **F-writer — In-flight `ingest()` during clear** (`/speckit-analyze` finding G2, 2026-09-22) | Pause an `ingest()` at its commit barrier, request clear with the exact history ID. | (a) If the `ingest()` commits within the 10-second window: honest receipt, then swept away with the rest of the pre-clear generation. (b) If the window expires first: `clear_history()` fails explicitly with `BudgetExceeded`; the `ingest()` continues unaffected — never cancelled (data-model.md Snapshot/Generation lifecycle case 3; AT-18). |

For F10/F-writer, inspect purge commands as well as resulting keys: deletion must be scoped and
bounded. Logical clear does not prove erasure from backups, free pages, providers, or responses
previously returned to the application.

## Release checks across these scenarios

- **Usage/logging (AT-20)**: collect default events during hits, retries, and failures. Assert no
  credentials, prompts, or message bodies; reused usage stays separate from current usage; absent
  provider usage is marked unknown.
- **Resource ownership (AT-17)**: close repeatedly after success and failure. Owned resources
  close; injected clients remain caller-owned unless transferred. A child process using a built
  artifact exits without leaked connections or tasks.
- **Cross-runtime replay (AT-11/17)**: after Python closes a committed store, open it in
  TypeScript and repeat in reverse. Run from an installed wheel and npm tarball outside the
  checkout. These are sequential opens, not concurrent cross-runtime writes.

Record a result only after execution: case ID, source commit, installed artifact, loaded SQLite
and driver versions, seed/fault point, observed result, sanitized evidence. **M0 review accepts
this test design; milestone completion still requires the actual passing runs** — this document
being written does not itself satisfy any AT.
