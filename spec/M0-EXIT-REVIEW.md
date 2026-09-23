# M0 Exit Review

**Date**: 2026-09-22 · **Task**: T015 · **Depends on**: T001–T014, all complete.

Per constitution Principle IV: this reports evidence, not inferred success. Every claim below was
checked, not assumed.

## Scope reviewed

- `UPSTREAM.md` (T001) and driver/namespace decisions (T002–T006, recorded in `research.md` §10)
- Repository layout (T007)
- `spec/schemas/` — `retrieval-result.schema.json`, `answer-result.schema.json`,
  `index-report.schema.json`, `error-codes.json` (T008)
- `spec/cache-format.md`, `spec/storage-format.md`, `spec/normalization.md` (T009–T011)
- `spec/fixtures/` — `ingestion-identity.json`, `unicode-source-mapping.json`,
  `retrieval-evidence.json`, `cache-key-mutations.json`, `export-import-identity.json`,
  `prompt-injection.json`, `failure-injection-harness.md` (T012–T014)

## Checks performed

| Check | Method | Result |
|---|---|---|
| All `spec/` JSON files are syntactically valid | `python3 -m json.tool` on each of the 10 JSON files | PASS — 10/10 valid |
| `spec/schemas/error-codes.json`'s 15 codes exactly match `contracts/result-schemas.md`'s error table | Programmatic diff of extracted code names from both files | PASS — exact match, 15/15 |
| Contract-Fixtures.md's full case list is represented | Manual cross-check: I1–I6 (ingestion-identity.json), Unicode section (unicode-source-mapping.json), candidate-selection/evidence section (retrieval-evidence.json), 8-row cache-key mutation table (cache-key-mutations.json), export/import section (export-import-identity.json) | PASS — all present |
| PRD §15's prompt-injection fixture gap (flagged by `/plan-eng-review`) is closed | `prompt-injection.json` covers quoted-instructions (P1) and cached-injection-strings (P2) — the two sub-requirements absent from Contract-Fixtures.md; JSON-delimiters (P3) added for full §15 coverage; model-selected-unknown-IDs and malformed-tool-records cross-referenced to existing coverage (tree-routing validation, unicode-source-mapping.json U4/U5) | PASS |
| Failure-Injection.md's F1–F11 are represented, plus the newly-specified in-flight-ingest case | `failure-injection-harness.md` covers F1–F11 verbatim plus F-writer (`/speckit-analyze` finding G2) | PASS |
| CHK002 (identity precedence) is reflected in the frozen contract, not just spec.md | `ingestion-identity.json` I2 case explicitly asserts `IdempotencyConflict` over `MessageConflict`, cites the resolution | PASS |
| CHK016/CHK017 (in-flight-write semantics during clear) are reflected in the frozen contract | `storage-format.md`'s "In-flight writes during clear_history()" section and `failure-injection-harness.md`'s F-writer case both state the confirmed semantics (commit-or-`BudgetExceeded`, never cancelled) | PASS |

## Identity and data-loss semantics — explicit statement

Per this review's own gate ("undefined identity/data-loss semantics must block M0 exit and M1"):

- **Identity** (INV-02, CHK002): fully specified and now frozen into `spec/normalization.md` and
  `spec/fixtures/ingestion-identity.json`. No open question remains.
- **Data-loss** (INV-01, INV-10, INV-11, CHK016/CHK017): fully specified and now frozen into
  `spec/storage-format.md`'s generation-lifecycle section and the F-writer fixture case. No open
  question remains.

**Not an identity/data-loss item, explicitly not blocking**: `index()`'s tree-publish commit check
(`history_revision`/`index_revision`) does not currently reference `cache_generation` — flagged in
`checklists/readiness.md` for `/plan-eng-review`, not resolved here. This is a routing/staleness
question, not a message-identity or history-preservation question, so it does not meet this
review's blocking bar — but it should not be forgotten before M2 implementation.

## M0 exit determination

**PASS.** Spec fixtures (T012–T014) are reviewed; no unresolved identity or data-loss semantics
remain. M1 may begin (T016 onward).
