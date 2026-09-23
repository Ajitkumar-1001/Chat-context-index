"""quickstart.md Scenario 2 — Idempotent replay and conflict rejection (User Story 1, M1;
AT-02, AT-03, AT-05).

1. Repeat an ingest with the identical idempotency_key and identical input -> replays the
   original receipt, no new write.
2. Repeat with the same key but different input -> IdempotencyConflict, no write.
3. Submit a batch 1 message over the configured size limit -> InputValidationError before any
   part of that batch commits.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.errors import IdempotencyConflict, InputValidationError
from cci.io_worker import fetchall
from cci.models import MAX_MESSAGES_PER_BATCH, MAX_RECORD_BYTES, InputMessage
from cci.ingest import ingest
from cci.store import HistoryStore


def test_idempotent_replay_and_conflict_rejection():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "idempotency.db")
            store = await HistoryStore.open(path)
            history_id = store.history_id

            original = [InputMessage(role="user", content="original input")]
            first = await ingest(store, history_id, original, "src-1", "key-1")
            assert first.replayed is False

            # Step 1: identical key, identical input -> replay, no new write.
            replay = await ingest(store, history_id, original, "src-1", "key-1")
            assert replay.replayed is True
            assert replay.inserted_seq_start == first.inserted_seq_start
            assert replay.inserted_seq_end == first.inserted_seq_end

            cursor = await store.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE history_id = ?", (history_id,)
            )
            (count_after_replay,) = (await fetchall(cursor))[0]
            assert count_after_replay == 1, "replay must not write a new message"

            # Step 2: same key, different input -> IdempotencyConflict, no write.
            different = [InputMessage(role="user", content="different input")]
            try:
                await ingest(store, history_id, different, "src-1", "key-1")
                raise AssertionError("expected IdempotencyConflict")
            except IdempotencyConflict:
                pass

            cursor = await store.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE history_id = ?", (history_id,)
            )
            (count_after_conflict,) = (await fetchall(cursor))[0]
            assert count_after_conflict == 1, "a rejected conflicting request commits nothing"

            # Step 3: one message over the configured batch-count limit -> InputValidationError
            # before any part of the batch commits.
            oversize = [
                InputMessage(role="user", content=f"m{i}") for i in range(MAX_MESSAGES_PER_BATCH + 1)
            ]
            try:
                await ingest(store, history_id, oversize, "src-1", "key-oversize")
                raise AssertionError("expected InputValidationError")
            except InputValidationError:
                pass

            cursor = await store.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE history_id = ?", (history_id,)
            )
            (count_after_oversize,) = (await fetchall(cursor))[0]
            assert count_after_oversize == 1, "oversize batch must not commit any part of itself"

            # Step 3b: a single record over the per-record byte limit, measured on the
            # serialized payload actually stored/hashed (not just raw content length).
            oversize_record = [InputMessage(role="user", content="x" * (MAX_RECORD_BYTES + 1))]
            try:
                await ingest(store, history_id, oversize_record, "src-1", "key-oversize-record")
                raise AssertionError("expected InputValidationError")
            except InputValidationError:
                pass

            cursor = await store.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE history_id = ?", (history_id,)
            )
            (count_after_oversize_record,) = (await fetchall(cursor))[0]
            assert count_after_oversize_record == 1, "oversize single record must not commit"

            await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_idempotent_replay_and_conflict_rejection()
    print("Scenario 2 (idempotent replay and conflict rejection) passed")
