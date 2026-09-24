# Supported Runtimes and Storage Topology

## Runtimes

| | Python | TypeScript |
|---|---|---|
| Version | 3.11–3.14 | Node.js 22.x / 24.x |
| SQLite driver | APSW (embeds SQLite 3.53.4) | `better-sqlite3` (embeds SQLite 3.53.4) |
| Required SQLite floor | 3.51.3 (WAL-reset fix) | 3.51.3 (WAL-reset fix) |
| Model memoization | none / sqlite / redis | no memoized provider backend this release |
| CLI | `cci` | none this release |

## Platform

Linux x64 and macOS arm64 are the targeted platforms. All 16 hosted compatibility combinations
passed for the 0.1.0 candidate; see the [CI evidence](../evaluations/results/release-readiness/hosted-ci-20260924.json).
One owning application process per history uses embedded local storage.
Cross-runtime access to the same
store file is **sequential, not concurrent**: close in one runtime before opening in the other
(single-writer WAL; see `backup-and-migration.md` for the reopen/backup implications).

## Storage topology

- **Authoritative history**: one SQLite file per history (WAL journal mode,
  `synchronous=FULL`, FTS5-indexed), created at the path you pass to `open()`.
- **Memo cache**: a separate SQLite file (`<history-path>.memo.sqlite3`) is used by default and
  as the fallback under Redis mode. It is never the same file as the history, and it is safe to
  delete while the store is closed — it holds nothing that cannot be recomputed.
- **Redis** (optional, Python only): keys are scoped as `cci:memo:v2:<scope_digest>:<request_digest>`.
  `clear_history()`'s Redis purge is bounded and scoped (`SCAN` + bounded `DEL` batches over the
  owned prefix) — never `KEYS *`, `FLUSHDB`, or `FLUSHALL`.

## Schema versioning

`schema_version` is checked on every `open()`. This is schema version 1 — the first release —
so there is no prior version to migrate from; opening a store with a newer `schema_version` than
this build supports raises `SchemaVersionError` without modifying the file.
