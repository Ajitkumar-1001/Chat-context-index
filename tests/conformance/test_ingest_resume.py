"""quickstart.md Scenario 1 — Preserve and resume (User Story 1, M1; AT-01, AT-02).

open -> ingest (two distinct messages with identical text) -> close -> reopen -> verify every
message's content, metadata, identity, and order match exactly; the two identical-text messages
remain two distinct entries.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.io_worker import fetchall
from cci.models import InputMessage
from cci.ingest import ingest
from cci.store import HistoryStore


def test_preserve_and_resume():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "resume.db")

            store = await HistoryStore.open(path)
            history_id = store.history_id
            messages = [
                InputMessage(role="user", content="same text", metadata={"lang": "en"}),
                InputMessage(role="user", content="same text", metadata={"lang": "en"}),
                InputMessage(role="assistant", content="a distinct reply"),
            ]
            receipt = await ingest(store, history_id, messages, "src-1", "key-1")
            assert receipt.inserted_seq_start == 1
            assert receipt.inserted_seq_end == 3
            assert receipt.skipped_count == 0
            await store.aclose()

            store2 = await HistoryStore.open(path)
            assert store2.history_id == history_id, "history_id must be stable across reopen"

            cursor = await store2.connection.execute(
                "SELECT seq, role, original_payload FROM messages WHERE history_id = ? ORDER BY seq",
                (history_id,),
            )
            rows = await fetchall(cursor)

            assert len(rows) == 3, "the two identical-text messages remain two distinct entries"
            assert [r[0] for r in rows] == [1, 2, 3], "order matches ingest order exactly"
            assert rows[0][1] == "user" and rows[1][1] == "user" and rows[2][1] == "assistant"
            assert rows[0][2] == rows[1][2], "identical content is preserved verbatim for both"
            assert rows[0][2] != rows[2][2]
            assert json.loads(rows[0][2])["metadata"] == {"lang": "en"}, (
                "per-message metadata round-trips through original_payload, not just content"
            )

            # T030 crash-recovery contract: retrying the identical source_id/idempotency_key
            # /input after a reopen safely replays the already-committed receipt.
            replay = await ingest(store2, history_id, messages, "src-1", "key-1")
            assert replay.replayed is True
            assert replay.inserted_seq_start == receipt.inserted_seq_start
            assert replay.inserted_seq_end == receipt.inserted_seq_end

            cursor = await store2.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE history_id = ?", (history_id,)
            )
            (count_after_replay,) = (await fetchall(cursor))[0]
            assert count_after_replay == 3, "a post-reopen replay must not write new messages"

            await store2.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_preserve_and_resume()
    print("Scenario 1 (preserve and resume) passed")
