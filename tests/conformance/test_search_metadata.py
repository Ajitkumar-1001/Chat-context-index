"""Storage-v2 migration, compact retrieval, and stale-writer failure boundaries."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

import apsw
import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "packages/python/src"))

from cci.clear import clear_history
from cci.errors import SchemaVersionError, StoreCorrupt, StoreError
from cci.ingest import ingest
from cci.models import InputMessage
from cci.retrieve import retrieve
from cci.search import search
from cci.store import HistoryStore


def seed_v1(path: Path) -> None:
    with apsw.Connection(str(path)) as db:
        db.execute((ROOT / "spec/fixtures/storage-v1.sql").read_text())
        db.execute(
            "INSERT INTO store_meta(id,history_id,store_instance_id,schema_version,"
            "history_revision,index_revision,cache_generation,seq_high_water_mark) "
            "VALUES(1,'h_fixture','si_fixture',1,7,3,2,9)"
        )
        for message_id, seq, text, rowid in (
            ("m_old", 1, "cobalt plan old", 7),
            ("m_tool", 2, None, None),
            ("m_empty", 4, "", 23),
            ("m_new", 9, "cobalt plan newer", 41),
        ):
            payload = json.dumps({"role": "user", "content": text}, separators=(",", ":"))
            db.execute(
                "INSERT INTO messages(message_id,seq,history_id,source_id,external_id,role,"
                "original_payload,text_projection,payload_hash,created_at) "
                "VALUES(?,?,'h_fixture','fixture',?,'user',?,?,?,123)",
                (message_id, seq, message_id, payload, text, "frozen-" + message_id),
            )
            if text is not None:
                db.execute(
                    "INSERT INTO message_fts(rowid,message_id,history_id,text) VALUES(?,?,'h_fixture',?)",
                    (rowid, message_id, text),
                )


def original_state(path: Path) -> tuple:
    with apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READONLY) as db:
        return (
            list(db.execute("SELECT * FROM messages ORDER BY seq")),
            list(
                db.execute(
                    "SELECT history_id,store_instance_id,history_revision,index_revision,"
                    "cache_generation,seq_high_water_mark FROM store_meta"
                )
            ),
            list(db.execute("SELECT rowid,* FROM message_fts ORDER BY rowid")),
        )


def test_v1_open_requires_explicit_migration(tmp_path):
    path = tmp_path / "v1.db"
    seed_v1(path)
    before = original_state(path)

    async def run():
        with pytest.raises(SchemaVersionError, match="migrat"):
            await HistoryStore.open(str(path), config={"cache_backend": "none"})

    asyncio.run(run())
    assert original_state(path) == before


def test_migrate_preserves_originals_actual_rowids_and_private_backup(tmp_path):
    path, backup = tmp_path / "v1.db", tmp_path / "backup.db"
    seed_v1(path)
    before = original_state(path)

    async def run():
        await HistoryStore.migrate(str(path), backup_path=str(backup), config={"cache_backend": "none"})
        assert original_state(path) == original_state(backup) == before
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        with apsw.Connection(str(path)) as db:
            assert db.execute(
                "SELECT schema_version,history_revision,search_metadata_revision FROM store_meta"
            ).fetchone() == (2, 7, 7)
            assert list(
                db.execute("SELECT fts_rowid,message_id,seq FROM lexical_message_meta ORDER BY fts_rowid")
            ) == [(7, "m_old", 1), (23, "m_empty", 4), (41, "m_new", 9)]
        unused = tmp_path / "not-needed.db"
        await HistoryStore.migrate(str(path), backup_path=str(unused))
        assert not unused.exists(), "already-current migration is a validated no-op"
        async with await HistoryStore.open(str(path), config={"cache_backend": "none"}) as store:
            assert [c.seq for c in (await search(store, "cobalt plan", 2)).candidates] == [9, 1]
            assert [e.seq for e in (await retrieve(store, "cobalt plan", mode="lexical")).evidence] == [1, 9]

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["existing", "alias", "symlink"])
def test_migration_refuses_backup_overwrite_or_alias(tmp_path, kind):
    path = tmp_path / "v1.db"
    seed_v1(path)
    backup = tmp_path / "backup.db"
    if kind == "existing":
        backup.write_bytes(b"keep me")
    elif kind == "alias":
        backup = path
    else:
        backup.symlink_to(path)
    before = original_state(path)

    async def run():
        with pytest.raises(StoreError):
            await HistoryStore.migrate(str(path), backup_path=str(backup))

    asyncio.run(run())
    assert original_state(path) == before
    if kind == "existing":
        assert backup.read_bytes() == b"keep me"


def test_migration_missing_file_does_not_create_it(tmp_path):
    path, backup = tmp_path / "missing.db", tmp_path / "backup.db"

    async def run():
        with pytest.raises(StoreError):
            await HistoryStore.migrate(str(path), backup_path=str(backup))

    asyncio.run(run())
    assert not path.exists() and not backup.exists()


@pytest.mark.parametrize("operation", ["search", "retrieve", "ingest", "clear"])
def test_legacy_writer_cannot_hide_records_or_be_resealed(tmp_path, operation):
    path = tmp_path / "v1.db"
    seed_v1(path)
    old = apsw.Connection(str(path))

    async def run():
        await HistoryStore.migrate(str(path), backup_path=str(tmp_path / "backup.db"))
        async with await HistoryStore.open(str(path), config={"cache_backend": "none"}) as store:
            # The legacy handle predates migration and only knows the v1 revision column.
            with old:
                old.execute("UPDATE store_meta SET history_revision=history_revision+1")
            with pytest.raises(StoreCorrupt):
                if operation == "search":
                    await search(store, "cobalt")
                elif operation == "retrieve":
                    await retrieve(store, "cobalt", mode="lexical")
                elif operation == "ingest":
                    await ingest(
                        store, store.history_id, [InputMessage(role="user", content="new")], "src", "key"
                    )
                else:
                    await clear_history(store, store.history_id)
            assert old.execute(
                "SELECT history_revision,search_metadata_revision FROM store_meta"
            ).fetchone() == (8, 7)
            assert old.execute("SELECT count(*) FROM messages").fetchone()[0] == 4

    try:
        asyncio.run(run())
    finally:
        old.close()


def test_explicit_rebuild_repairs_derived_state_only(tmp_path):
    path = tmp_path / "v1.db"
    seed_v1(path)

    async def run():
        await HistoryStore.migrate(str(path), backup_path=str(tmp_path / "migration.db"))
        before = original_state(path)[:2]
        with apsw.Connection(str(path)) as db:
            db.execute("DELETE FROM message_fts WHERE message_id='m_new'")
            db.execute("DELETE FROM lexical_message_meta WHERE message_id='m_old'")
            db.execute("UPDATE store_meta SET search_metadata_revision=0")
        await HistoryStore.rebuild_search_index(str(path), backup_path=str(tmp_path / "recovery.db"))
        assert original_state(path)[:2] == before
        async with await HistoryStore.open(str(path), config={"cache_backend": "none"}) as store:
            assert [c.seq for c in (await search(store, "cobalt", 2)).candidates] == [9, 1]
            await clear_history(store, store.history_id)
            await ingest(
                store, store.history_id, [InputMessage(role="user", content="cobalt next")], "src", "next"
            )
            assert [c.seq for c in (await search(store, "cobalt")).candidates] == [10]

    asyncio.run(run())


@pytest.mark.parametrize("failure", [apsw.FullError, asyncio.CancelledError])
def test_migration_failure_and_cancellation_roll_back_schema_and_close_workers(
    tmp_path, monkeypatch, failure
):
    import cci.store as store_module

    path = tmp_path / "v1.db"
    seed_v1(path)
    before = original_state(path)
    existing_connections = set(apsw.connections())
    validate = store_module.validate_search_metadata

    async def fail_after_backfill(connection, *, include_map=True):
        await validate(connection, include_map=include_map)
        if include_map:
            raise failure("injected after backfill")

    monkeypatch.setattr(store_module, "validate_search_metadata", fail_after_backfill)

    async def run():
        error = asyncio.CancelledError if failure is asyncio.CancelledError else StoreError
        with pytest.raises(error):
            await HistoryStore.migrate(str(path), backup_path=str(tmp_path / "backup.db"))

    asyncio.run(run())
    assert original_state(path) == original_state(tmp_path / "backup.db") == before
    with apsw.Connection(str(path)) as db:
        assert db.execute("SELECT schema_version FROM store_meta").fetchone() == (1,)
        assert not list(db.execute("SELECT 1 FROM sqlite_schema WHERE name='lexical_message_meta'"))
        assert "search_metadata_revision" not in [r[1] for r in db.execute("PRAGMA table_info(store_meta)")]
    db.close()
    assert not set(apsw.connections()) - existing_connections


@pytest.mark.parametrize("corruption", ["duplicate", "orphan", "wrong_text", "wrong_history"])
def test_migration_rejects_invalid_fts_without_upgrading(tmp_path, corruption):
    path = tmp_path / "v1.db"
    seed_v1(path)
    with apsw.Connection(str(path)) as db:
        if corruption == "duplicate":
            db.execute(
                "INSERT INTO message_fts(message_id,history_id,text) "
                "VALUES('m_old','h_fixture','cobalt plan old')"
            )
        elif corruption == "orphan":
            db.execute(
                "INSERT INTO message_fts(message_id,history_id,text) VALUES('missing','h_fixture','cobalt')"
            )
        else:
            column = "text" if corruption == "wrong_text" else "history_id"
            db.execute(f"UPDATE message_fts SET {column}='wrong' WHERE message_id='m_old'")
    before = original_state(path)

    async def run():
        with pytest.raises(StoreCorrupt):
            await HistoryStore.migrate(str(path), backup_path=str(tmp_path / "backup.db"))

    asyncio.run(run())
    assert original_state(path) == before
    with apsw.Connection(str(path)) as db:
        assert db.execute("SELECT schema_version FROM store_meta").fetchone() == (1,)


class ConnectionProxy:
    def __init__(self, real, callback):
        self.real = real
        self.callback = callback

    async def execute(self, sql, *args, **kwargs):
        await self.callback(sql)
        return await self.real.execute(sql, *args, **kwargs)

    def __getattr__(self, key):
        return getattr(self.real, key)


def test_ingest_map_failure_rolls_back_messages_fts_receipt_and_revisions(tmp_path):
    async def run():
        async with await HistoryStore.open(
            str(tmp_path / "fresh.db"), config={"cache_backend": "none"}
        ) as store:
            real = store._connection

            async def fault(sql):
                if "INSERT INTO lexical_message_meta" in sql:
                    raise apsw.FullError("injected map write failure")

            store._connection = ConnectionProxy(real, fault)
            try:
                with pytest.raises(StoreError):
                    await ingest(
                        store, store.history_id, [InputMessage(role="user", content="cobalt")], "s", "k"
                    )
            finally:
                store._connection = real
            for table in ("messages", "message_fts", "lexical_message_meta", "ingest_receipts"):
                assert (await (await real.execute(f"SELECT count(*) FROM {table}")).fetchone()) == (0,)
            assert (
                await (
                    await real.execute("SELECT history_revision,search_metadata_revision FROM store_meta")
                ).fetchone()
            ) == (0, 0)

    asyncio.run(run())


def test_import_rejects_foreign_revision_between_committed_batches(tmp_path):
    from cci.errors import VersionConflict
    from cci.export import export
    from cci.import_history import import_history

    async def run():
        exported = tmp_path / "source.jsonl"
        async with await HistoryStore.open(
            str(tmp_path / "source.db"), config={"cache_backend": "none"}
        ) as source:
            await ingest(
                source,
                source.history_id,
                [InputMessage(role="user", content=f"cobalt {i}") for i in range(501)],
                "s",
                "k",
            )
            await export(source, str(exported))
        path = tmp_path / "target.db"
        async with await HistoryStore.open(str(path), config={"cache_backend": "none"}) as store:
            real = store._connection
            begins = 0

            async def foreign_write(sql):
                nonlocal begins
                if sql == "BEGIN IMMEDIATE":
                    begins += 1
                    if begins == 2:
                        with apsw.Connection(str(path)) as other:
                            other.execute(
                                "UPDATE store_meta SET history_revision=history_revision+1,"
                                "search_metadata_revision=search_metadata_revision+1"
                            )

            store._connection = ConnectionProxy(real, foreign_write)
            try:
                with pytest.raises(VersionConflict):
                    await import_history(store, str(exported))
            finally:
                store._connection = real
            for table in ("messages", "message_fts", "lexical_message_meta"):
                assert (await (await real.execute(f"SELECT count(*) FROM {table}")).fetchone()) == (500,)

    asyncio.run(run())


def test_search_revision_and_rows_share_one_snapshot(tmp_path, monkeypatch):
    import cci.search as search_module

    async def run():
        path = tmp_path / "snapshot.db"
        async with await HistoryStore.open(str(path), config={"cache_backend": "none"}) as reader:
            await ingest(
                reader, reader.history_id, [InputMessage(role="user", content="cobalt old")], "s", "old"
            )
            async with await HistoryStore.open(str(path), config={"cache_backend": "none"}) as writer:
                guard = search_module.assert_search_metadata

                async def append_after_guard(connection):
                    revision = await guard(connection)
                    await ingest(
                        writer,
                        writer.history_id,
                        [InputMessage(role="user", content="cobalt new")],
                        "s",
                        "new",
                    )
                    return revision

                monkeypatch.setattr(search_module, "assert_search_metadata", append_after_guard)
                assert [c.seq for c in (await search(reader, "cobalt")).candidates] == [1]
                monkeypatch.setattr(search_module, "assert_search_metadata", guard)
                assert [c.seq for c in (await search(reader, "cobalt")).candidates] == [2, 1]

    asyncio.run(run())


async def export_fixture(tmp_path, count):
    from cci.export import export

    destination = tmp_path / "messages.jsonl"
    async with await HistoryStore.open(
        str(tmp_path / "export-source.db"), config={"cache_backend": "none"}
    ) as source:
        if count:
            await ingest(
                source,
                source.history_id,
                [InputMessage(role="user", content=f"cobalt {i}") for i in range(count)],
                "s",
                "k",
            )
        await export(source, str(destination))
    return destination


@pytest.mark.parametrize("failure", ["stale_before_first_batch", "map_write_in_second_batch"])
def test_import_guard_and_late_failure_preserve_committed_boundaries(tmp_path, failure):
    from cci.import_history import import_history

    async def run():
        exported = await export_fixture(tmp_path, 501)
        async with await HistoryStore.open(
            str(tmp_path / "import-target.db"), config={"cache_backend": "none"}
        ) as store:
            real = store._connection
            inserts = 0

            async def fault(sql):
                nonlocal inserts
                if "INSERT INTO lexical_message_meta" in sql:
                    inserts += 1
                    if inserts == 501:
                        raise apsw.FullError("injected second-batch metadata failure")

            if failure == "stale_before_first_batch":
                await real.execute("UPDATE store_meta SET history_revision=1")
            else:
                store._connection = ConnectionProxy(real, fault)
            try:
                with pytest.raises(StoreCorrupt if failure == "stale_before_first_batch" else StoreError):
                    await import_history(store, str(exported))
            finally:
                store._connection = real
            expected = 0 if failure == "stale_before_first_batch" else 500
            for table in ("messages", "message_fts", "lexical_message_meta"):
                assert await (await real.execute(f"SELECT count(*) FROM {table}")).fetchone() == (expected,)
            revisions = await (
                await real.execute("SELECT history_revision,search_metadata_revision FROM store_meta")
            ).fetchone()
            assert revisions == ((1, 0) if expected == 0 else (1, 1))

    asyncio.run(run())


@pytest.mark.parametrize("count", [0, 1])
def test_import_rechecks_emptiness_after_input_validation(tmp_path, monkeypatch, count):
    import cci.import_history as module
    from cci.errors import StoreNotEmpty

    async def run():
        exported = await export_fixture(tmp_path, count)
        path = tmp_path / "target.db"
        async with await HistoryStore.open(str(path), config={"cache_backend": "none"}) as store:
            read = module._read_records

            def insert_after_validation(src_path):
                result = read(src_path)
                other = apsw.Connection(str(path))
                try:
                    with other:
                        other.execute(
                            "INSERT INTO messages(message_id,seq,history_id,source_id,role,"
                            "original_payload,payload_hash,created_at) "
                            "VALUES('foreign',1,?,'s','user','{}','h',1)",
                            (store.history_id,),
                        )
                        other.execute(
                            "UPDATE store_meta SET history_revision=1,search_metadata_revision=1,"
                            "seq_high_water_mark=1"
                        )
                finally:
                    other.close()
                return result

            monkeypatch.setattr(module, "_read_records", insert_after_validation)
            with pytest.raises(StoreNotEmpty):
                await module.import_history(store, str(exported))
            assert await (await store.connection.execute("SELECT message_id FROM messages")).fetchone() == (
                "foreign",
            )

    asyncio.run(run())


@pytest.mark.parametrize("table", ["lexical_message_meta", "message_fts"])
def test_rebuild_recovers_missing_derived_table(tmp_path, table):
    path = tmp_path / "v1.db"
    seed_v1(path)

    async def run():
        await HistoryStore.migrate(str(path), backup_path=str(tmp_path / "v1-backup.db"))
        db = apsw.Connection(str(path))
        try:
            db.execute(f"DROP TABLE {table}")
        finally:
            db.close()
        await HistoryStore.rebuild_search_index(str(path), backup_path=str(tmp_path / "v2-backup.db"))
        async with await HistoryStore.open(str(path), config={"cache_backend": "none"}) as store:
            assert [c.seq for c in (await search(store, "cobalt")).candidates] == [9, 1]

    asyncio.run(run())


def test_current_migration_validates_and_never_repairs(tmp_path):
    path = tmp_path / "v1.db"
    seed_v1(path)

    async def run():
        await HistoryStore.migrate(str(path), backup_path=str(tmp_path / "first-backup.db"))
        db = apsw.Connection(str(path))
        try:
            db.execute("DELETE FROM lexical_message_meta WHERE message_id='m_new'")
        finally:
            db.close()
        new_backup = tmp_path / "unused.db"
        with pytest.raises(StoreCorrupt):
            await HistoryStore.migrate(str(path), backup_path=str(new_backup))
        assert not new_backup.exists()
        db = apsw.Connection(str(path))
        try:
            assert db.execute("SELECT count(*) FROM lexical_message_meta").fetchone() == (2,)
        finally:
            db.close()

    asyncio.run(run())


@pytest.mark.parametrize("method,version", [("migrate", 0), ("migrate", 3), ("rebuild_search_index", 1)])
def test_maintenance_rejects_unknown_or_wrong_version_without_backup(tmp_path, method, version):
    path = tmp_path / "version.db"
    seed_v1(path)
    db = apsw.Connection(str(path))
    try:
        db.execute("UPDATE store_meta SET schema_version=?", (version,))
    finally:
        db.close()
    before = original_state(path)
    backup = tmp_path / "unused.db"

    async def run():
        with pytest.raises(SchemaVersionError):
            await getattr(HistoryStore, method)(str(path), backup_path=str(backup))

    asyncio.run(run())
    assert original_state(path) == before and not backup.exists()


def test_migration_backup_contains_committed_wal_changes(tmp_path):
    path = tmp_path / "wal.db"
    seed_v1(path)
    owner = apsw.Connection(str(path))
    owner.execute("PRAGMA journal_mode=WAL")
    with owner:
        owner.execute("UPDATE messages SET text_projection='cobalt WAL' WHERE message_id='m_old'")
        owner.execute("UPDATE message_fts SET text='cobalt WAL' WHERE message_id='m_old'")
        owner.execute("UPDATE store_meta SET history_revision=8")
    assert Path(str(path) + "-wal").stat().st_size > 0
    before = original_state(path)

    async def run():
        backup = tmp_path / "wal-backup.db"
        await HistoryStore.migrate(str(path), backup_path=str(backup))
        assert original_state(backup) == original_state(path) == before

    try:
        asyncio.run(run())
    finally:
        owner.close()


def test_clear_failure_rolls_back_compact_rows_and_revision(tmp_path):
    async def run():
        async with await HistoryStore.open(
            str(tmp_path / "clear.db"), config={"cache_backend": "none"}
        ) as store:
            await ingest(store, store.history_id, [InputMessage(role="user", content="cobalt")], "s", "k")
            real = store._connection

            async def fault(sql):
                if sql == "DELETE FROM messages":
                    raise apsw.FullError("injected after compact-map deletion")

            store._connection = ConnectionProxy(real, fault)
            try:
                with pytest.raises(StoreError):
                    await clear_history(store, store.history_id)
            finally:
                store._connection = real
            assert [c.seq for c in (await search(store, "cobalt")).candidates] == [1]
            assert await (
                await real.execute(
                    "SELECT history_revision,search_metadata_revision,cache_generation FROM store_meta"
                )
            ).fetchone() == (1, 1, 0)

    asyncio.run(run())


@pytest.mark.parametrize("stale", [False, True])
def test_empty_import_checks_revision_without_advancing_it(tmp_path, stale):
    from cci.import_history import import_history

    async def run():
        exported = await export_fixture(tmp_path, 0)
        async with await HistoryStore.open(
            str(tmp_path / "empty.db"), config={"cache_backend": "none"}
        ) as store:
            if stale:
                await store.connection.execute("UPDATE store_meta SET history_revision=1")
                with pytest.raises(StoreCorrupt):
                    await import_history(store, str(exported))
            else:
                result = await import_history(store, str(exported))
                assert result.imported_count == 0
            assert await (
                await store.connection.execute(
                    "SELECT history_revision,search_metadata_revision FROM store_meta"
                )
            ).fetchone() == (int(stale), 0)

    asyncio.run(run())


def test_relative_backup_path_rejected_before_filesystem_work(tmp_path, monkeypatch):
    from cci.errors import InputValidationError

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "source.db"
    seed_v1(path)
    before = original_state(path)

    async def run():
        with pytest.raises(InputValidationError):
            await HistoryStore.migrate(str(path), backup_path="relative.db")

    asyncio.run(run())
    assert original_state(path) == before and not (tmp_path / "relative.db").exists()


def test_migration_rejects_corrupt_fts_postings(tmp_path):
    path = tmp_path / "v1.db"
    seed_v1(path)
    db = apsw.Connection(str(path))
    try:
        db.execute("DELETE FROM message_fts_data WHERE id>10")
    finally:
        db.close()

    async def run():
        with pytest.raises(StoreCorrupt):
            await HistoryStore.migrate(str(path), backup_path=str(tmp_path / "backup.db"))

    asyncio.run(run())
    db = apsw.Connection(str(path))
    try:
        assert db.execute("SELECT schema_version FROM store_meta").fetchone() == (1,)
    finally:
        db.close()


def test_rebuild_preserves_bounded_correction_rowid_order(tmp_path):
    path = tmp_path / "v1.db"
    seed_v1(path)
    db = apsw.Connection(str(path))
    try:
        with db:
            db.execute("DELETE FROM messages")
            db.execute("DELETE FROM message_fts")
            for seq in range(1, 42):
                text = "deployment plan for Oslo" if seq == 1 else f"Oslo note number {seq}"
                payload = json.dumps({"role": "user", "content": text})
                db.execute(
                    "INSERT INTO messages(message_id,seq,history_id,source_id,role,original_payload,"
                    "text_projection,payload_hash,created_at) VALUES(?,?,'h_fixture','s','user',?,?,?,1)",
                    (f"m_{seq}", seq, payload, text, f"hash_{seq}"),
                )
                db.execute(
                    "INSERT INTO message_fts(rowid,message_id,history_id,text) VALUES(?,?,'h_fixture',?)",
                    (100 - seq, f"m_{seq}", text),
                )
            db.execute("UPDATE store_meta SET seq_high_water_mark=41")
    finally:
        db.close()

    async def selected():
        async with await HistoryStore.open(str(path), config={"cache_backend": "none"}) as store:
            result = await retrieve(store, "deployment plan", mode="lexical", max_selected_chunks=2)
            return [e.seq for e in result.evidence]

    async def run():
        await HistoryStore.migrate(str(path), backup_path=str(tmp_path / "v1-backup.db"))
        before = await selected()
        assert before == [1, 32]
        await HistoryStore.rebuild_search_index(str(path), backup_path=str(tmp_path / "v2-backup.db"))
        assert await selected() == before

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["postings", "fts_identity", "both_identities"])
def test_rebuild_identity_sources_and_deterministic_fallback(tmp_path, damage):
    path = tmp_path / "v1.db"
    seed_v1(path)

    async def run():
        await HistoryStore.migrate(str(path), backup_path=str(tmp_path / "v1-backup.db"))
        db = apsw.Connection(str(path))
        try:
            if damage == "postings":
                db.execute("DELETE FROM message_fts_data WHERE id>10")
            else:
                db.execute("UPDATE message_fts SET message_id='missing' WHERE message_id='m_new'")
                if damage == "both_identities":
                    db.execute("UPDATE lexical_message_meta SET history_id='wrong'")
        finally:
            db.close()
        await HistoryStore.rebuild_search_index(str(path), backup_path=str(tmp_path / "repair.db"))
        db = apsw.Connection(str(path))
        try:
            expected = (
                [(1, "m_old"), (2, "m_empty"), (3, "m_new")]
                if damage == "both_identities"
                else [(7, "m_old"), (23, "m_empty"), (41, "m_new")]
            )
            assert list(db.execute("SELECT rowid,message_id FROM message_fts ORDER BY rowid")) == expected
        finally:
            db.close()
        async with await HistoryStore.open(str(path), config={"cache_backend": "none"}) as store:
            assert [c.seq for c in (await search(store, "cobalt")).candidates] == [9, 1]

    asyncio.run(run())
