"""quickstart.md Scenario 4 — Explicit indexing, idempotent re-run (User Story 4, M2; AT-06, AT-07).

1. index() builds a valid tree covering the pending range.
2. A second no-change call makes zero model calls and reports the same committed coverage.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.errors import ConfigurationError
from cci.index import index
from cci.ingest import ingest
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderResponse
from cci.store import HistoryStore


async def _seeded_store(path: str) -> HistoryStore:
    store = await HistoryStore.open(path)
    await ingest(
        store, store.history_id,
        [
            InputMessage(role="user", content="what is rayleigh scattering"),
            InputMessage(role="assistant", content="it's why the sky looks blue"),
        ],
        "src-1", "key-1",
    )
    return store


def test_index_builds_a_valid_tree_covering_pending_range():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "idx1.db"))
            fake = FakeProvider(
                responses=[ProviderResponse(text='{"title": "Sky color", "summary": "Rayleigh scattering explanation."}')]
            )
            provider = MemoizedProvider(inner=fake, config=store.config)

            report = await index(store, provider=provider)
            assert report.status == "complete"
            assert report.committed_coverage.start_seq == 1
            assert report.committed_coverage.end_seq == 2
            assert report.pending_coverage.start_seq is None
            assert fake.call_count == 1

            cursor = await store.connection.execute(
                "SELECT node_id, title, summary FROM nodes WHERE history_id = ?",
                (store.history_id,),
            )
            rows = [row async for row in cursor]
            assert len(rows) == 1
            assert rows[0][1] == "Sky color"

            node_id = rows[0][0]
            view = await store.view_node(node_id)
            assert view is not None
            assert view.title == "Sky color"

            await store.aclose()

    asyncio.run(scenario())


def test_second_no_change_call_makes_zero_model_calls():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "idx2.db"))
            fake = FakeProvider(
                responses=[ProviderResponse(text='{"title": "T", "summary": "S"}')]
            )
            provider = MemoizedProvider(inner=fake, config=store.config)

            first = await index(store, provider=provider)
            assert fake.call_count == 1

            second = await index(store, provider=provider)
            assert fake.call_count == 1, "a second no-change call makes zero model calls"
            assert second.status == "complete"
            assert second.committed_coverage == first.committed_coverage

            await store.aclose()

    asyncio.run(scenario())


def test_index_without_provider_is_configuration_error():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "idx3.db"))
            try:
                await index(store, provider=None)
                raise AssertionError("expected ConfigurationError")
            except ConfigurationError:
                pass
            await store.aclose()

    asyncio.run(scenario())


def test_index_on_empty_store_is_zero_calls_and_empty_coverage():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await HistoryStore.open(os.path.join(d, "idx4.db"))
            fake = FakeProvider()
            provider = MemoizedProvider(inner=fake, config=store.config)

            report = await index(store, provider=provider)
            assert fake.call_count == 0
            assert report.committed_coverage.start_seq is None
            await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_index_builds_a_valid_tree_covering_pending_range()
    test_second_no_change_call_makes_zero_model_calls()
    test_index_without_provider_is_configuration_error()
    test_index_on_empty_store_is_zero_calls_and_empty_coverage()
    print("Scenario 4 (index()) checks passed")
