"""Failure injection F4 (stale proposal), per spec/fixtures/failure-injection-harness.md
(AT-07, AT-12 concurrent-indexing subcase).

Pause indexing after its model input captures the generation/history/index revision tuple
(the revision tuple is captured before any provider call, so pausing inside the provider call
satisfies exactly this), commit an ingestion, release the proposal. It cannot publish against
the old revision tuple — `VersionConflict` is permitted (contracts/operations.md `index()`
Errors) — and newly committed raw history remains searchable throughout.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.errors import VersionConflict
from cci.index import index
from cci.ingest import ingest
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderResponse
from cci.search import search
from cci.store import HistoryStore


def test_f4_stale_proposal_cannot_publish_against_changed_revisions():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f4.db")
            store = await HistoryStore.open(path)
            await ingest(
                store, store.history_id,
                [InputMessage(role="user", content="original marker phrase")],
                "src-1", "key-1",
            )

            entered = asyncio.Event()
            resume = asyncio.Event()
            fake = FakeProvider(
                responses=[ProviderResponse(text='{"title": "T", "summary": "S"}')],
                entered=entered,
                resume=resume,
            )
            provider = MemoizedProvider(inner=fake, config=store.config)

            index_task = asyncio.create_task(index(store, provider=provider))
            # Indexing's revision tuple is captured before this call runs, so waiting for entry
            # into the (paused) provider call is exactly "after its model input captures
            # generation/history/index revisions."
            await entered.wait()

            # Commit an ingestion while the proposal is paused — this bumps
            # history_revision, invalidating the paused proposal's captured tuple.
            await ingest(
                store, store.history_id,
                [InputMessage(role="user", content="concurrently appended marker phrase")],
                "src-1", "key-2",
            )

            resume.set()
            try:
                await index_task
                raised = False
            except VersionConflict:
                raised = True
            assert raised, (
                "a stale proposal must not publish against changed revisions — "
                "VersionConflict is the permitted outcome here"
            )

            # Newly committed raw history remains searchable throughout — the rejected
            # tree-publish never touched the messages/FTS tables.
            result = await search(store, "concurrently")
            assert len(result.candidates) == 1

            result2 = await search(store, "original")
            assert len(result2.candidates) == 1

            # No tree was published from the rejected proposal.
            cursor = await store.connection.execute(
                "SELECT COUNT(*) FROM nodes WHERE history_id = ?", (store.history_id,)
            )
            row = [r async for r in cursor][0]
            assert row[0] == 0, "the rejected proposal must not have published any node"

            await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_f4_stale_proposal_cannot_publish_against_changed_revisions()
    print("F4 failure-injection check (stale proposal) passed")
