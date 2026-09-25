# Backup and Migration

## Backing up a history

The authoritative history is one SQLite file (WAL mode). To back it up safely:

1. **Preferred — closed-store copy**: call `aclose()`/`close()`, then copy the file (and its
   `-wal`/`-shm` sidecar files if present) with your OS's normal file-copy tools. This is always
   safe and requires no special API.
2. **Online backup while open**: use your SQLite driver's own online-backup API
   (APSW's `backup()` in Python) against the same connection this library owns, rather than
   copying the raw file while it may be mid-write. `cci` does not wrap this in its own API this
   release — use the driver directly against `store.connection` (Python) if you need a live
   backup without stopping writes. TypeScript's `store.connection` has no backup method; use
   the closed-store copy.

The memo cache file (`<history-path>.memo.sqlite3`) never needs backing up — it holds nothing
that cannot be recomputed.

## Migration

New stores use **schema version 2**, which adds a compact, derived lexical search index.
Original messages, identifiers, sequence numbers, tree data, receipts and cache generations
are preserved. The portable JSONL export format remains version 1.

Opening an existing schema-version-1 store raises `SchemaVersionError` and requires an
explicit migration. Before migrating, stop every process using that history, including
older SDK versions. Use a new, absolute backup path; existing backup files are never overwritten.

```python
import asyncio
from pathlib import Path
from cci import HistoryStore

asyncio.run(HistoryStore.migrate(
    "conversation.db", backup_path=str(Path("conversation.before-v2.db").absolute())
))
```

```typescript
import { HistoryStore } from "chat-context-index";
import { resolve } from "node:path";

await HistoryStore.migrate("conversation.db", {
  backupPath: resolve("conversation.before-v2.db"),
});
```

Migration takes a SQLite writer lock, creates a consistent backup through a separate
connection, checks FTS integrity and the existing FTS-to-message mapping, then creates the compact index
and updates the schema version in one transaction. The backup uses SQLite's
[`VACUUM INTO`](https://www.sqlite.org/lang_vacuum.html#vacuum_with_an_into_clause)
with full synchronization. No model calls or memo cache are involved.

Failure rolls back the history changes; an interrupted backup may leave an incomplete
backup file, which must not be treated as a successful backup. Repeating migration on a
valid version-2 store makes no changes and leaves the original backup intact. Unsupported
versions are rejected. Once migration succeeds, use upgraded SDKs for every subsequent
access; an older SDK cannot open a version-2 store.

To restore, stop all processes and restore the backup to a **different database path**.
Do not copy it over an open database or combine it with the migrated store's WAL/SHM files.
Use the older SDK with the version-1 backup, or migrate that backup using a fresh backup
filename before opening it with the new SDK.

## Recovering a stale search index

The compact index records the history revision it covers. Search and history mutations
raise `StoreCorrupt` if that revision is stale, including when an already-open older process
wrote after migration. Stop every process first, then explicitly rebuild the derived search
index with another new backup filename:

```python
asyncio.run(HistoryStore.rebuild_search_index(
    "conversation.db", backup_path=str(Path("conversation.before-rebuild.db").absolute())
))
```

```typescript
await HistoryStore.rebuildSearchIndex("conversation.db", {
  backupPath: resolve("conversation.before-rebuild.db"),
});
```

Rebuilding reconstructs FTS and its compact mapping from stored message projections in one
transaction. It preserves original payloads, identities and history/index/cache revisions.
It cannot repair damaged original messages. Ordinary opening or ingestion never silently
repairs or marks a stale index current.

Recovery preserves FTS row identities from a valid existing FTS mapping, or from a valid
compact mapping if the FTS mapping is damaged. If neither mapping is usable, it assigns
new row identities in message sequence order. In that corruption fallback, bounded
correction selection can change; original message identities and sequences stay unchanged.

## Cross-runtime migration (Python ↔ TypeScript)

Both languages read and write the identical schema (same DDL, same identity rules, same
canonical JSON encoding) — closing a store in one language and opening it in the other works
with no conversion step, as long as access is sequential: close the store in one runtime before
opening it in the other. There is no "export from Python, import into TypeScript"
step required; the same file works directly in both.

## Portable migration via export/import

`export()`/`import_history()` (or their TypeScript equivalents) give you a portable, versioned
JSONL format independent of the SQLite file layout — useful for moving a history to a different
storage location, a different major version once one exists, or into a fresh store when you
specifically want a new `history_id`/`cache_generation` (e.g. to intentionally break stale cache
references). `import_history()` validates the manifest's record count and checksum before
committing anything, and refuses to import into a non-empty target (`StoreNotEmpty`) rather than
silently merging.
