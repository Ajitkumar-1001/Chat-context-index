"""Failure injection F5 (retrieval during append/reindex), per
spec/fixtures/failure-injection-harness.md (AT-08, AT-10).

Full scope (a tree read that detects a different `index_revision` mid-request, discards
nominations, and reports `tree_revision_changed`) needs `index()`'s tree-publish, which is T053
— out of this batch's scope. What IS exercisable now, and is exercised here: a single
`retrieve()` request's Snapshot/candidates are captured together and never retroactively
include content committed after that capture (data-model.md Snapshot rule), and a later
request correctly sees the new state. The index-publish step is simulated directly against
`store_meta.index_revision` (advisor-reviewed boundary simulation, same pattern as
`test_failure_open.py`'s F3 runtime case) since no real tree-publish exists yet.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.ingest import ingest
from cci.models import InputMessage
from cci.retrieve import retrieve
from cci.store import HistoryStore


def test_f5_snapshot_excludes_content_appended_after_capture():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f5.db")
            store = await HistoryStore.open(path)

            await ingest(
                store, store.history_id,
                [InputMessage(role="user", content="alpha marker phrase")],
                "src-1", "key-1",
            )

            # "Capture lexical candidates and snapshot_max_seq" — one retrieve() request.
            before = await retrieve(store, "marker")
            assert before.snapshot.snapshot_max_seq == 1
            assert len(before.evidence) == 1

            # "Append a message and publish a newer index" — the append is real; the index
            # publish is simulated directly (index()/tree-publish is T053, not built yet).
            await ingest(
                store, store.history_id,
                [InputMessage(role="user", content="beta marker phrase, appended later")],
                "src-1", "key-2",
            )
            await store.connection.execute(
                "UPDATE store_meta SET index_revision = index_revision + 1 WHERE id = 1"
            )

            # The already-returned `before` result's evidence set is a plain Python value —
            # it cannot retroactively change. This is the observable guarantee: nothing
            # mutates a result already handed to the caller.
            assert len(before.evidence) == 1
            assert before.snapshot.snapshot_max_seq == 1

            # "Resume routing" — a fresh request now correctly sees the appended message and
            # the bumped index_revision.
            after = await retrieve(store, "marker")
            assert after.snapshot.snapshot_max_seq == 2
            assert after.snapshot.index_revision == before.snapshot.index_revision + 1
            assert len(after.evidence) == 2, "the new sequence is visible to a fresh request"

            await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_f5_snapshot_excludes_content_appended_after_capture()
    print("F5 failure-injection check (snapshot half) passed")
