"""Contract test: export/import identity preservation, per Contract-Fixtures.md's Export and
import identity section (spec/fixtures/export-import-identity.json E1-E3; AT-19).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.errors import InputValidationError, StoreNotEmpty
from cci.export import export
from cci.import_history import import_history
from cci.io_worker import fetchall
from cci.ingest import ingest
from cci.models import InputMessage
from cci.store import HistoryStore


def test_e1_round_trip_identity_preserved_new_history_id():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            src_path = os.path.join(d, "src.db")
            dst_path = os.path.join(d, "dst.db")
            jsonl_path = os.path.join(d, "export.jsonl")

            src = await HistoryStore.open(src_path)
            original_history_id = src.history_id
            messages = [
                InputMessage(role="user", content="first", external_id="e1"),
                InputMessage(role="assistant", content="second"),
                InputMessage(role="user", content="first"),  # identical text, distinct identity
            ]
            await ingest(src, original_history_id, messages, "src-1", "key-1")
            await export(src, jsonl_path)

            cursor = await src.connection.execute(
                "SELECT message_id, seq, source_id, original_payload FROM messages ORDER BY seq"
            )
            original_rows = await fetchall(cursor)
            await src.aclose()

            dst = await HistoryStore.open(dst_path)
            report = await import_history(dst, jsonl_path)
            assert report.imported_count == 3
            await dst.aclose()

            # close/reopen the target after import
            dst2 = await HistoryStore.open(dst_path)
            assert dst2.history_id != original_history_id, "new_on_import: history_id"

            cursor = await dst2.connection.execute(
                "SELECT message_id, seq, source_id, original_payload FROM messages ORDER BY seq"
            )
            imported_rows = await fetchall(cursor)

            assert [r[0] for r in imported_rows] == [r[0] for r in original_rows], (
                "preserved: original message IDs"
            )
            assert [r[1] for r in imported_rows] == [r[1] for r in original_rows], (
                "preserved: sequence order"
            )
            assert [r[2] for r in imported_rows] == [r[2] for r in original_rows], (
                "preserved: source identities"
            )
            assert [r[3] for r in imported_rows] == [r[3] for r in original_rows], (
                "preserved: original strings"
            )
            await dst2.aclose()

    asyncio.run(scenario())


def test_e2_import_into_nonempty_target_rejected():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            src_path = os.path.join(d, "src.db")
            dst_path = os.path.join(d, "dst.db")
            jsonl_path = os.path.join(d, "export.jsonl")

            src = await HistoryStore.open(src_path)
            await ingest(
                src, src.history_id, [InputMessage(role="user", content="x")], "src-1", "key-1"
            )
            await export(src, jsonl_path)
            await src.aclose()

            dst = await HistoryStore.open(dst_path)
            await ingest(
                dst, dst.history_id, [InputMessage(role="user", content="already here")],
                "src-2", "key-2",
            )
            try:
                await import_history(dst, jsonl_path)
                raise AssertionError("expected StoreNotEmpty")
            except StoreNotEmpty:
                pass
            await dst.aclose()

    asyncio.run(scenario())


def test_e3_manifest_record_count_mismatch_rejected_before_commit():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            src_path = os.path.join(d, "src.db")
            dst_path = os.path.join(d, "dst.db")
            jsonl_path = os.path.join(d, "export.jsonl")

            src = await HistoryStore.open(src_path)
            await ingest(
                src, src.history_id, [InputMessage(role="user", content="x")], "src-1", "key-1"
            )
            await export(src, jsonl_path)
            await src.aclose()

            import json

            with open(jsonl_path) as f:
                lines = f.readlines()
            manifest = json.loads(lines[-1])
            manifest["checksum"] = "0" * 64
            lines[-1] = json.dumps(manifest) + "\n"
            tampered_path = os.path.join(d, "tampered.jsonl")
            with open(tampered_path, "w") as f:
                f.writelines(lines)

            dst = await HistoryStore.open(dst_path)
            try:
                await import_history(dst, tampered_path)
                raise AssertionError("expected InputValidationError")
            except InputValidationError:
                pass

            cursor = await dst.connection.execute("SELECT COUNT(*) FROM messages")
            (count,) = (await fetchall(cursor))[0]
            assert count == 0, "manifest mismatch commits nothing"
            await dst.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_e1_round_trip_identity_preserved_new_history_id()
    test_e2_import_into_nonempty_target_rejected()
    test_e3_manifest_record_count_mismatch_rejected_before_commit()
    print("Export/import identity checks passed")
