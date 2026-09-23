"""HistoryStore: open()/aclose() and the durable schema (spec/storage-format.md).

Check order (contracts/operations.md `open()` Check order, `/plan-eng-review` finding
2026-09-22): configuration is validated FIRST, before any store file is created or opened;
then the loaded SQLite runtime/FTS5 support is verified; then `schema_version` is checked;
only then is the store created (fresh path) or resumed (existing path) transactionally.

The public API is async throughout (PRD §7.2: `async with ContextIndex.open(...)`), backed
by APSW's native async support (io_worker.py) rather than a hand-rolled thread pool.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field

import apsw
import apsw.aio

from ._ids import prefixed_id
from .cache import MemoCache, build_cache
from .config import Config, resolve
from .errors import (
    RuntimeCompatibilityError,
    SchemaVersionError,
    StoreBusy,
    StoreCorrupt,
    StoreError,
)
from .io_worker import IOWorker, fetchall, fetchone, open_worker_connection
from .models import Message
from .provider import UsageLog

# get_messages() pagination (contracts/operations.md `search()`/`get_messages()`/`view_node()`
# Pagination/size limits).
DEFAULT_GET_MESSAGES_LIMIT = 500
MAX_GET_MESSAGES_LIMIT = 5_000


@dataclass(frozen=True)
class NodeView:
    """Minimal read-only projection of a `nodes` row (data-model.md Node) — just enough to
    type `view_node()`'s result until T052 formalizes the full Node/Chunk/NodeChunk models."""

    node_id: str
    parent_id: str | None
    sibling_order: int
    message_range: str
    title: str | None
    summary: str | None
    state: str
    index_revision: int

# Required SQLite runtime (research.md §1: fixes the WAL-reset corruption bug present from
# 3.7.0 through 3.51.2). Checked against the LOADED runtime, independent of which driver
# version is installed (spec/storage-format.md).
MIN_SQLITE_VERSION = (3, 51, 3)

# This is the first schema version for the first release — there is no prior version to
# migrate from (spec/storage-format.md Migrations and recovery). A future schema change
# increments this and requires an explicit, versioned migration path.
SCHEMA_VERSION = 1

_SCHEMA_DDL = """
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
"""


def map_storage_error(exc: apsw.Error) -> Exception:
    """Map a raw APSW exception to the fixed 15-code taxonomy (contracts/result-schemas.md
    error table: StoreBusy/StoreCorrupt/StoreError). Used to wrap write-transaction bodies so a
    caller never sees a raw `apsw.*Error` (T033, Failure-Injection.md F2)."""
    if isinstance(exc, apsw.BusyError):
        return StoreBusy(str(exc))
    if isinstance(exc, (apsw.CorruptError, apsw.NotADBError)):
        return StoreCorrupt(str(exc))
    if isinstance(exc, (apsw.FullError, apsw.IOError)):
        return StoreError(str(exc))
    return StoreError(str(exc))


def _open_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


def _parse_version(version_string: str) -> tuple[int, int, int]:
    parts = version_string.split(".")[:3]
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


async def _check_runtime(connection: apsw.AsyncConnection) -> None:
    """Verify the LOADED SQLite runtime and FTS5 support — independent of which driver
    version is installed (spec/storage-format.md)."""
    loaded_version = _parse_version(apsw.sqlite_lib_version())
    if loaded_version < MIN_SQLITE_VERSION:
        raise RuntimeCompatibilityError(
            f"loaded SQLite {'.'.join(map(str, loaded_version))} is older than the required "
            f"{'.'.join(map(str, MIN_SQLITE_VERSION))} (WAL-reset fix) — no default backport "
            "allowance; see spec/storage-format.md"
        )
    cursor = await connection.execute("PRAGMA compile_options")
    rows = [row async for row in cursor]
    compile_options = {row[0] for row in rows}
    # APSW's amalgamation reports ENABLE_FTS5 in compile_options when built with FTS5.
    if not any("FTS5" in opt for opt in compile_options):
        raise RuntimeCompatibilityError("loaded SQLite build lacks FTS5 support")


@dataclass
class HistoryStore:
    """One opened history. `open()`/`aclose()` own the underlying connection and I/O worker."""

    path: str
    config: Config
    history_id: str
    store_instance_id: str
    _connection: apsw.AsyncConnection
    _io_worker: IOWorker
    cache: MemoCache
    usage_log: UsageLog = field(default_factory=UsageLog)
    _closed: bool = False

    # Write coordination (clear_history() gating; `/plan-eng-review`-equivalent finding
    # 2026-09-22 during T036 design: ingest()/import_history()/index() tree-publish all write
    # on one AsyncConnection, which cannot run two `async with connection:` transactions
    # concurrently — a lock serializes them; the gate/counter let clear_history() quiesce
    # in-flight writers within its 10s deadline (data-model.md Snapshot/Generation lifecycle
    # case 3) without needing to know each writer's specific operation.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _write_gate: asyncio.Event = field(default_factory=_open_event)
    _inflight_writers: int = 0
    _writer_cv: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def begin_write(self) -> None:
        """Call before starting a local write operation (ingest/import/tree-publish). Blocks
        while a clear_history() gate is closed; once past, this call counts as "already past
        the gate" for that clear's quiescence window."""
        await self._write_gate.wait()
        async with self._writer_cv:
            self._inflight_writers += 1

    async def end_write(self) -> None:
        async with self._writer_cv:
            self._inflight_writers -= 1
            self._writer_cv.notify_all()

    async def close_write_gate(self) -> None:
        """clear_history(): block new writers from starting."""
        self._write_gate.clear()

    async def open_write_gate(self) -> None:
        """clear_history(): resume admitting new writers, on both success and BudgetExceeded
        failure paths (a failed clear must not gate writers forever)."""
        self._write_gate.set()

    async def wait_for_writers_to_drain(self, timeout_s: float) -> bool:
        """True if `_inflight_writers` reached 0 within `timeout_s`; False on timeout."""
        async with self._writer_cv:
            try:
                await asyncio.wait_for(
                    self._writer_cv.wait_for(lambda: self._inflight_writers == 0),
                    timeout=timeout_s,
                )
                return True
            except TimeoutError:
                return False

    @classmethod
    async def open(
        cls,
        path: str,
        config: dict | Config | None = None,
        config_file: str | None = None,
    ) -> HistoryStore:
        # (1) Validate configuration FIRST, before any store file is created or opened.
        if isinstance(config, Config):
            resolved_config = config
            resolved_config.validate()
        else:
            resolved_config = resolve(overrides=config, config_file=config_file)

        is_fresh = not os.path.exists(path) or os.path.getsize(path) == 0

        # (2)+(3)+(4): open the connection (this is where SQLite actually touches the file),
        # then runtime-check and schema-check BEFORE any write, per the frozen check order.
        connection = await open_worker_connection(path)
        try:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute("PRAGMA foreign_keys=ON")
            await connection.execute("PRAGMA synchronous=FULL")
            await connection.execute(
                f"PRAGMA busy_timeout={resolved_config.sqlite_busy_timeout_ms}"
            )

            await _check_runtime(connection)

            if is_fresh:
                history_id, store_instance_id = await cls._init_fresh(connection)
            else:
                history_id, store_instance_id = await cls._resume_existing(connection)

            # Cache construction can fail after SQLite has opened its connection and worker.
            io_worker = IOWorker(connection)
            cache = build_cache(resolved_config, path)
        except apsw.Error as exc:
            # A raw APSW error (e.g. NotADBError on a corrupted/garbage file) is mapped to the
            # typed taxonomy — never leaked to the caller as-is (Failure-Injection.md F2).
            await connection.aclose()
            raise map_storage_error(exc) from exc
        except Exception:
            await connection.aclose()
            raise

        return cls(
            path=path,
            config=resolved_config,
            history_id=history_id,
            store_instance_id=store_instance_id,
            _connection=connection,
            _io_worker=io_worker,
            cache=cache,
        )

    @staticmethod
    async def _init_fresh(connection: apsw.AsyncConnection) -> tuple[str, str]:
        history_id = prefixed_id("t")
        store_instance_id = prefixed_id("si")
        async with connection:
            await connection.execute(_SCHEMA_DDL)
            await connection.execute(
                "INSERT INTO store_meta "
                "(id, history_id, store_instance_id, schema_version) VALUES (1, ?, ?, ?)",
                (history_id, store_instance_id, SCHEMA_VERSION),
            )
        return history_id, store_instance_id

    @staticmethod
    async def _resume_existing(connection: apsw.AsyncConnection) -> tuple[str, str]:
        cursor = await connection.execute(
            "SELECT history_id, store_instance_id, schema_version FROM store_meta WHERE id = 1"
        )
        row = await fetchone(cursor)
        if row is None:
            # Existing file, but no store_meta row: initialize as fresh within the same file.
            return await HistoryStore._init_fresh(connection)
        history_id, store_instance_id, schema_version = row
        if schema_version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"store schema_version {schema_version} is newer than this build supports "
                f"({SCHEMA_VERSION}) — no file modification"
            )
        if schema_version < SCHEMA_VERSION:
            # No prior schema version exists for the first release (spec/storage-format.md) —
            # this branch has no real migration to run yet; documented here for when it does.
            raise SchemaVersionError(
                f"store schema_version {schema_version} is older than this build "
                f"({SCHEMA_VERSION}) and no migration path exists yet"
            )
        return history_id, store_instance_id

    @property
    def connection(self) -> apsw.AsyncConnection:
        return self._connection

    @property
    def io_worker(self) -> IOWorker:
        return self._io_worker

    async def get_messages(
        self, start_seq: int, end_seq: int, limit: int = DEFAULT_GET_MESSAGES_LIMIT
    ) -> list[Message]:
        """Pagination/size limits apply; truncation never modifies the stored record — this
        reads full rows within the requested range, bounded only by `limit` (contracts/
        operations.md `search()`/`get_messages()`/`view_node()`)."""
        bounded_limit = min(limit, MAX_GET_MESSAGES_LIMIT)
        cursor = await self._connection.execute(
            "SELECT message_id, seq, source_id, external_id, idempotency_key, "
            "position_in_batch, role, original_payload, text_projection, payload_hash, "
            "session_metadata FROM messages WHERE history_id = ? AND seq >= ? AND seq <= ? "
            "ORDER BY seq LIMIT ?",
            (self.history_id, start_seq, end_seq, bounded_limit),
        )
        rows = await fetchall(cursor)
        return [self._row_to_message(row) for row in rows]

    def _row_to_message(self, row: tuple) -> Message:
        (
            message_id, seq, source_id, external_id, idempotency_key, position_in_batch,
            role, original_payload, text_projection, payload_hash, session_metadata,
        ) = row
        return Message(
            message_id=message_id,
            seq=seq,
            history_id=self.history_id,
            source_id=source_id,
            external_id=external_id,
            idempotency_key=idempotency_key,
            position_in_batch=position_in_batch,
            role=role,
            original_payload=json.loads(original_payload),
            text_projection=text_projection,
            payload_hash=payload_hash,
            session_metadata=json.loads(session_metadata) if session_metadata else None,
        )

    async def view_node(self, node_id: str) -> NodeView | None:
        """Never calls a model. Returns None (typed empty result) when no such node exists —
        the only current caller is on a store with no tree yet (`index()` is T053, out of this
        batch's scope), so this always returns None for now; the query itself is already
        correct for when nodes exist."""
        cursor = await self._connection.execute(
            "SELECT node_id, parent_id, sibling_order, message_range, title, summary, state, "
            "index_revision FROM nodes WHERE history_id = ? AND node_id = ?",
            (self.history_id, node_id),
        )
        row = await fetchone(cursor)
        if row is None:
            return None
        return NodeView(
            node_id=row[0], parent_id=row[1], sibling_order=row[2], message_range=row[3],
            title=row[4], summary=row[5], state=row[6], index_revision=row[7],
        )

    async def aclose(self) -> None:
        """Close owned resources (the I/O worker, which owns the connection; the memo cache).
        Safe to call repeatedly (AT-17). Injected clients are not this store's concern —
        HistoryStore only owns what it created in open()."""
        if self._closed:
            return
        await self.cache.aclose()
        await self._io_worker.aclose()
        self._closed = True

    async def __aenter__(self) -> HistoryStore:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
