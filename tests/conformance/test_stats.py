"""Contract test: `stats()` zero-valued baseline on a fresh store (contracts/operations.md,
quoted verbatim: "On a freshly opened store with no prior activity, `stats()` returns
zero-valued counts across every field — never an error"), no content/credential leakage.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.ingest import ingest
from cci.models import InputMessage
from cci.stats import stats
from cci.store import HistoryStore


def test_zero_valued_baseline_on_fresh_store():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await HistoryStore.open(os.path.join(d, "stats1.db"))
            result = await stats(store)
            for field_ in dataclasses.fields(result):
                value = getattr(result, field_.name)
                assert value in (0, False), f"{field_.name} is {value!r}, expected a zero value"
            await store.aclose()

    asyncio.run(scenario())


def test_stats_reflects_durable_counts_no_error():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await HistoryStore.open(os.path.join(d, "stats2.db"))
            await ingest(
                store, store.history_id,
                [InputMessage(role="user", content="secret credential AKIA1234567890 shhh")],
                "src-1", "key-1",
            )
            result = await stats(store)
            assert result.history_message_count == 1
            assert result.ingest_receipt_count == 1

            # No message content, prompt text, or credential ever appears in stats() output —
            # it exposes only counts/identifiers/revisions, never payload fields at all.
            rendered = repr(result)
            assert "secret" not in rendered
            assert "AKIA1234567890" not in rendered

            await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_zero_valued_baseline_on_fresh_store()
    test_stats_reflects_durable_counts_no_error()
    print("stats() zero-valued baseline checks passed")
