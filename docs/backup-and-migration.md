# Backup and Migration

## Backing up a history

The authoritative history is one SQLite file (WAL mode). To back it up safely:

1. **Preferred — closed-store copy**: call `aclose()`/`close()`, then copy the file (and its
   `-wal`/`-shm` sidecar files if present) with your OS's normal file-copy tools. This is always
   safe and requires no special API.
2. **Online backup while open**: use your SQLite driver's own online-backup API
   (APSW's `backup()` in Python; `better-sqlite3`'s `.backup()` in TypeScript) against the same
   connection this library owns, rather than copying the raw file while it may be mid-write.
   `cci` does not wrap this in its own API this release — use the driver directly against
   `store.connection` (Python) if you need a live backup without stopping writes.

The memo cache file (`<history-path>.memo.sqlite3`) never needs backing up — it holds nothing
that cannot be recomputed.

## Migration

This is schema version 1, the first release — there is no prior schema version to migrate from.
`open()` checks `schema_version` before touching any data: a store with a newer, unsupported
`schema_version` raises `SchemaVersionError` without modifying the file, so a downgrade attempt
is always safe (the file is left exactly as it was).

## Cross-runtime migration (Python ↔ TypeScript)

Both languages read and write the identical schema (same DDL, same identity rules, same
canonical JSON encoding) — closing a store in one language and opening it in the other works
with no conversion step, as long as access is sequential (see
`supported-runtimes-and-storage.md`). There is no "export from Python, import into TypeScript"
step required; the same file works directly in both.

## Portable migration via export/import

`export()`/`import_history()` (or their TypeScript equivalents) give you a portable, versioned
JSONL format independent of the SQLite file layout — useful for moving a history to a different
storage location, a different major version once one exists, or into a fresh store when you
specifically want a new `history_id`/`cache_generation` (e.g. to intentionally break stale cache
references). `import_history()` validates the manifest's record count and checksum before
committing anything, and refuses to import into a non-empty target (`StoreNotEmpty`) rather than
silently merging.
