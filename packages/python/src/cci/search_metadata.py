"""Compact, derived lexical metadata and its storage-v2 consistency boundary."""

from __future__ import annotations

from contextlib import asynccontextmanager

import apsw

from .errors import SchemaVersionError, StoreCorrupt
from .io_worker import fetchone

SEARCH_METADATA_DDL = """
CREATE TABLE lexical_message_meta (
    fts_rowid INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    history_id TEXT NOT NULL,
    seq INTEGER NOT NULL
);
"""


@asynccontextmanager
async def immediate_transaction(connection: apsw.AsyncConnection):
    """Take writer exclusion before inspecting revisions, including during maintenance."""
    try:
        await connection.execute("BEGIN IMMEDIATE")
        yield
        await connection.execute("COMMIT")
    except BaseException:
        try:
            await connection.execute("ROLLBACK")
        except apsw.Error:
            pass  # BEGIN may itself have failed; preserve the original failure.
        raise


async def assert_search_metadata(connection: apsw.AsyncConnection) -> int:
    """Bounded check; caller owns the read snapshot or IMMEDIATE write transaction."""
    try:
        row = await fetchone(
            await connection.execute(
                "SELECT schema_version,history_revision,search_metadata_revision FROM store_meta WHERE id=1"
            )
        )
        if row is None:
            raise StoreCorrupt("missing store metadata")
        if row[0] != 2:
            raise SchemaVersionError("search requires store schema 2; explicitly migrate an older store")
        if not isinstance(row[1], int) or row[1] < 0 or row[1] != row[2]:
            raise StoreCorrupt("stale search metadata; close writers and explicitly rebuild_search_index")
        await connection.execute(
            "SELECT fts_rowid,message_id,history_id,seq FROM lexical_message_meta LIMIT 0"
        )
        return row[1]
    except apsw.SQLError as exc:
        raise StoreCorrupt("missing or invalid search metadata; explicit recovery is required") from exc


async def validate_search_metadata(connection: apsw.AsyncConnection, *, include_map: bool = True) -> None:
    """Full validation for explicit maintenance, never an ordinary open/search scan."""
    meta = await fetchone(await connection.execute("SELECT history_id FROM store_meta WHERE id=1"))
    if meta is None:
        raise StoreCorrupt("missing store metadata")
    invalid_history = await fetchone(
        await connection.execute(
            "SELECT 1 FROM messages WHERE history_id IS NOT ? OR typeof(seq)<>'integer' OR seq<1 LIMIT 1",
            (meta[0],),
        )
    )
    if invalid_history:
        raise StoreCorrupt("invalid authoritative message history or sequence")
    expected_row = await fetchone(
        await connection.execute("SELECT count(*) FROM messages WHERE text_projection IS NOT NULL")
    )
    assert expected_row is not None
    expected = expected_row[0]
    counts = await fetchone(
        await connection.execute("SELECT count(*),count(DISTINCT message_id) FROM message_fts")
    )
    invalid_fts = await fetchone(
        await connection.execute(
            "SELECT 1 FROM message_fts f LEFT JOIN messages m ON m.message_id=f.message_id "
            "WHERE m.message_id IS NULL OR m.text_projection IS NULL "
            "OR f.history_id IS NOT m.history_id OR f.text IS NOT m.text_projection LIMIT 1"
        )
    )
    if counts != (expected, expected) or invalid_fts:
        raise StoreCorrupt("FTS rows do not match authoritative message projections")
    # Content rows can remain intact while the posting lists are damaged. This explicit
    # maintenance check validates both; ordinary search/open never performs the full scan.
    await connection.execute("INSERT INTO message_fts(message_fts,rank) VALUES('integrity-check',1)")
    if not include_map:
        return
    mapped = await fetchone(await connection.execute("SELECT count(*) FROM lexical_message_meta"))
    invalid_map = await fetchone(
        await connection.execute(
            "SELECT 1 FROM lexical_message_meta x "
            "LEFT JOIN message_fts f ON f.rowid=x.fts_rowid "
            "LEFT JOIN messages m ON m.message_id=x.message_id "
            "WHERE f.rowid IS NULL OR m.message_id IS NULL OR x.message_id IS NOT f.message_id "
            "OR x.history_id IS NOT f.history_id OR x.history_id IS NOT m.history_id "
            "OR x.seq IS NOT m.seq LIMIT 1"
        )
    )
    if mapped != (expected,) or invalid_map:
        raise StoreCorrupt("compact search metadata does not match FTS and authoritative messages")


async def backfill_search_metadata(connection: apsw.AsyncConnection) -> None:
    await connection.execute(
        "INSERT INTO lexical_message_meta(fts_rowid,message_id,history_id,seq) "
        "SELECT f.rowid,m.message_id,m.history_id,m.seq "
        "FROM message_fts f JOIN messages m ON m.message_id=f.message_id"
    )


async def preserve_rebuild_rowids(connection: apsw.AsyncConnection) -> None:
    """Retain bounded-correction ordering whenever a complete derived identity map survives."""
    await connection.execute(
        "CREATE TEMP TABLE cci_rebuild_rowids(fts_rowid INTEGER PRIMARY KEY,message_id TEXT NOT NULL UNIQUE)"
    )
    expected_row = await fetchone(
        await connection.execute("SELECT count(*) FROM messages WHERE text_projection IS NOT NULL")
    )
    assert expected_row is not None
    expected = expected_row[0]
    for compact in (False, True):
        table = "lexical_message_meta" if compact else "message_fts"
        rowid = "fts_rowid" if compact else "rowid"
        try:
            counts = await fetchone(
                await connection.execute(
                    f"SELECT count(*),count(DISTINCT message_id),count(DISTINCT {rowid}) FROM {table}"
                )
            )
            extra = " OR x.seq IS NOT m.seq OR typeof(x.fts_rowid)<>'integer'" if compact else ""
            invalid = await fetchone(
                await connection.execute(
                    f"SELECT 1 FROM {table} x LEFT JOIN messages m ON m.message_id=x.message_id "
                    "WHERE m.message_id IS NULL OR m.text_projection IS NULL "
                    "OR x.history_id IS NOT m.history_id" + extra + " LIMIT 1"
                )
            )
        except (apsw.SQLError, apsw.CorruptError):
            continue  # Missing/corrupt derived identity; do not mask IO/busy failures.
        if counts == (expected, expected, expected) and invalid is None:
            await connection.execute(f"INSERT INTO cci_rebuild_rowids SELECT {rowid},message_id FROM {table}")
            return
    # Neither complete identity map survived. Original IDs/seq remain authoritative;
    # deterministic reconstruction can change the bounded correction heuristic's subset.
    await connection.execute(
        "INSERT INTO cci_rebuild_rowids SELECT ROW_NUMBER() OVER(ORDER BY seq),message_id "
        "FROM messages WHERE text_projection IS NOT NULL"
    )


async def insert_search_metadata(
    connection: apsw.AsyncConnection,
    message_id: str,
    history_id: str,
    seq: int,
) -> None:
    # Must immediately follow the FTS insert on the same serialized connection.
    rowid = await connection.last_insert_rowid()
    await connection.execute(
        "INSERT INTO lexical_message_meta(fts_rowid,message_id,history_id,seq) VALUES(?,?,?,?)",
        (rowid, message_id, history_id, seq),
    )
