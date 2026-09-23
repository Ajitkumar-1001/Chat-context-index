# Storage Format and Migration Contract (frozen at M0)

Source: data-model.md store-scoped entities (PRD §9.1); contracts/operations.md `open()` Check
order. Logical/shared — both native implementations serialize per this contract; this document
does not choose a language-specific ORM or table DDL.

## Runtime configuration

WAL mode, foreign keys, bounded busy timeout (5,000 ms default), `synchronous=FULL`. WAL permits
concurrent reads under a single-writer constraint — not a multi-host/network-filesystem solution.

Required SQLite runtime: **≥ 3.51.3** (fixes the WAL-reset corruption bug present from 3.7.0
through 3.51.2 — research.md §1). Verified via the loaded runtime at every `open()`, independent
of which driver is installed (contracts/operations.md `open()`).

## `open()` check order (contracts/operations.md, `/plan-eng-review` finding 2026-09-22)

1. Validate configuration first — including a missing `application_namespace` under Redis mode —
   **before any store file is created or opened**. Pure, synchronous, no I/O.
2. Verify the loaded SQLite runtime and FTS5 support; `RuntimeCompatibilityError` if it fails.
3. Check `schema_version`; `SchemaVersionError` without modifying the file if newer and
   unsupported.
4. Create (fresh path) or resume (existing path) `store_meta` transactionally.

## Logical entities

| Entity | Key fields | Responsibility |
|---|---|---|
| `store_meta` | `history_id` (`t_<ULID>`), `store_instance_id`, `schema_version`, `history_revision`, `index_revision`, `cache_generation`, `index_coverage` | Singleton row per store |
| `messages` | `message_id` (`m_<ULID>`), `seq` (monotonic, history-local), identity fields, `role`, `original_payload`, `text_projection`, `payload_hash`, `session_metadata` | Authoritative message store |
| `ingest_receipts` | `receipt_key` (`history_id`,`source_id`,`idempotency_key`), `request_hash`, counts | Idempotency/replay |
| `exchanges` | Derived membership, `normalization_version` | Grouping, not independently identified |
| `chunks` | `chunk_id` (`c_<ULID>`), `source_message_span`, `content_hash`, `rendering_version` | Retrieval units |
| `nodes` | `node_id` (`n_<ULID>`), `parent_id`, `sibling_order`, `message_range`, `title`, `summary`, `state`, `index_revision` | Topic tree |
| `node_chunks` | Ordered `node_id` → `chunk_id` references | Leaf-to-chunk link table |
| `message_fts` / `summary_fts` | Rebuildable FTS5 indexes | Distinct evidence types |
| `pending_cache_purges` | `scope_id`, `requested_at` | Retired scope IDs awaiting external cleanup — no conversation payloads |
| Separate memo database | See `cache-format.md` | Never the history source of truth |

Schemas, migrations, and serializers are shared, cross-language specifications — enforced with
foreign keys, uniqueness constraints, and transactionally enforced revisions, not application-code
checks alone.

## State transitions

- `history_revision`, `index_revision`: only ever increase, only inside their owning commit
  (ingest; tree-publish respectively).
- `cache_generation`: only increases, only inside `clear_history()`'s commit.
- `seq`: monotonic, history-local, inclusive ranges; not reused after `clear_history` (high-water
  mark retained, live data removed).

## Migrations and recovery (PRD §9.3)

New databases initialize transactionally. Opening a newer, unsupported schema raises
`SchemaVersionError` without modifying the file. **No prior schema version exists for this first
release** — there is nothing to migrate from yet, so no migration procedure is implemented in M0;
this is a scope statement, not a silently missing requirement. When a second schema version
exists (post-v1), a supported older schema will require an explicit, versioned migration with a
documented backup/recovery path; migrations must never silently reset history.

Copying only the live SQLite main file is not a backup procedure. A documented driver backup
operation or a clean, verified closed-store copy is required (documentation task: tasks.md T079).

## In-flight writes during `clear_history()` (data-model.md Snapshot/Generation lifecycle case 3)

An `ingest()`, `index()` tree-publish, or `import_history()` already running when
`clear_history()` begins gating new work is not cancelled — it has the 10-second quiescence
window to reach its natural commit. If it commits in time, the result is honest (pre-clear
generation) and is swept away with the rest of that generation once the clear's own commit
rotates `cache_generation` immediately after. If it has not committed when the window expires,
`clear_history()` fails explicitly with `BudgetExceeded` and the write continues unaffected.
