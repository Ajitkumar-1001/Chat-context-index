# Supported Runtimes and Storage Topology

## Runtimes

| | Python | TypeScript |
|---|---|---|
| Version | 3.11–3.14 | Node.js 22.x / 24.x |
| SQLite driver | APSW (embeds SQLite 3.53.4) | `better-sqlite3` (embeds SQLite 3.53.4) |
| Required SQLite floor | 3.51.3 (WAL-reset fix) | 3.51.3 (WAL-reset fix) |
| Cache backends | none / sqlite / redis | none / sqlite (cache-key formula only is cross-language; the Redis backend itself is Python-only this release) |
| CLI | `cci` (thin wrapper, FR-012) | none this release |

## Platform

Linux x64 and macOS arm64. One owning application process per history — this is embedded local
storage, not a distributed or hosted deployment (PRD §9.2). Cross-runtime access to the same
store file is **sequential, not concurrent**: close in one runtime before opening in the other
(single-writer WAL; see `backup-and-migration.md` for the reopen/backup implications).

## Storage topology

- **Authoritative history**: one SQLite file per history (WAL journal mode,
  `synchronous=FULL`, FTS5-indexed), created at the path you pass to `open()`.
- **Memo cache**: a separate SQLite file (`<history-path>.memo.sqlite3`) is used by default and
  as the fallback under Redis mode. It is never the same file as the history, and it is safe to
  delete at any time — it holds nothing that cannot be recomputed (constitution Principle III).
- **Redis** (optional, Python only): keys are scoped as `cci:memo:v2:<scope_digest>:<request_digest>`.
  `clear_history()`'s Redis purge is bounded and scoped (`SCAN` + bounded `DEL` batches over the
  owned prefix) — never `KEYS *`, `FLUSHDB`, or `FLUSHALL`.

## Schema versioning

`schema_version` is checked on every `open()`. This is schema version 1 — the first release —
so there is no prior version to migrate from; opening a store with a newer `schema_version` than
this build supports raises `SchemaVersionError` without modifying the file.
