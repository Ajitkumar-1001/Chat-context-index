-- Frozen schema-v1 DDL from candidate e762f7bd0033e3ae8326f647f14c901438c74220.
-- Migration tests must preserve this prior format independently of the current DDL.
CREATE TABLE IF NOT EXISTS store_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
    history_id TEXT NOT NULL UNIQUE,
    store_instance_id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    history_revision INTEGER NOT NULL DEFAULT 0,
    index_revision INTEGER NOT NULL DEFAULT 0,
    cache_generation INTEGER NOT NULL DEFAULT 0,
    index_committed_seq INTEGER NOT NULL DEFAULT 0,
    index_pending_seq INTEGER NOT NULL DEFAULT 0,
    seq_high_water_mark INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    history_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    external_id TEXT,
    idempotency_key TEXT,
    position_in_batch INTEGER,
    role TEXT NOT NULL,
    original_payload TEXT NOT NULL,   -- JSON, stored verbatim
    text_projection TEXT,             -- nullable only for supported tool-call-only records
    payload_hash TEXT NOT NULL,
    session_metadata TEXT,            -- JSON, optional
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_seq ON messages(history_id, seq);
-- Identity uniqueness (data-model.md Message identity rule): enforced by two partial
-- unique indexes matching the two identity modes, not application checks alone
-- (constitution Principle I).
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_identity_external
    ON messages(history_id, source_id, external_id)
    WHERE external_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_identity_batch
    ON messages(history_id, source_id, idempotency_key, position_in_batch)
    WHERE external_id IS NULL;

CREATE TABLE IF NOT EXISTS ingest_receipts (
    history_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    inserted_seq_start INTEGER,
    inserted_seq_end INTEGER,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    unsupported_block_count INTEGER NOT NULL DEFAULT 0,
    indexing_status TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (history_id, source_id, idempotency_key)
);

-- Exchange grouping (FR-003: every message belongs to exactly one exchange) is NOT
-- computed by this milestone's ingest() — the exact boundary algorithm is underspecified
-- in the reviewed artifacts (data-model.md only states the invariant, not the rule) and is
-- deliberately deferred rather than invented here. Table created now for schema
-- completeness (spec/storage-format.md lists it as a store-scoped entity).
CREATE TABLE IF NOT EXISTS exchanges (
    exchange_id TEXT PRIMARY KEY,
    history_id TEXT NOT NULL,
    normalization_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    history_id TEXT NOT NULL,
    source_message_span TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    rendering_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    history_id TEXT NOT NULL,
    parent_id TEXT,
    sibling_order INTEGER NOT NULL,
    message_range TEXT NOT NULL,
    title TEXT,
    summary TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    index_revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS node_chunks (
    node_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    chunk_order INTEGER NOT NULL,
    PRIMARY KEY (node_id, chunk_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    message_id UNINDEXED, history_id UNINDEXED, text
);
CREATE VIRTUAL TABLE IF NOT EXISTS summary_fts USING fts5(
    node_id UNINDEXED, history_id UNINDEXED, text
);

CREATE TABLE IF NOT EXISTS pending_cache_purges (
    scope_id TEXT PRIMARY KEY,
    requested_at REAL NOT NULL
);
