"""quickstart.md Scenario 3 — Lexical retrieval without a model (User Story 2, M1/M2; AT-08,
AT-09).

With no model provider configured, ingest a small history. retrieve() for a query with a known
match returns citable original excerpts with routing.actual_mode: lexical and zero provider
calls in usage; a query with no match returns status: empty with an explicit diagnostic, not a
fabricated answer.
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


def test_lexical_retrieve_known_match_and_no_match():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lexical.db")
            # No provider configured anywhere in this test — Config defaults have none.
            store = await HistoryStore.open(path)
            await ingest(
                store, store.history_id,
                [
                    InputMessage(role="user", content="cobalt telescope observation log"),
                    InputMessage(role="assistant", content="acknowledged the reading"),
                ],
                "src-1", "key-1",
            )

            known = await retrieve(store, "cobalt telescope")
            assert known.status == "ok"
            assert known.routing.actual_mode == "lexical"
            assert known.usage.current_provider_calls == 0
            assert known.usage.usage_unknown is False
            assert len(known.evidence) == 1
            evidence = known.evidence[0]
            assert "cobalt" in evidence.excerpt
            assert evidence.source_pointer == "/content"
            assert evidence.message_id

            unmatched = await retrieve(store, "xyzzy nonexistent term")
            assert unmatched.status == "empty"
            assert unmatched.evidence == []
            assert len(unmatched.diagnostics) == 1
            assert unmatched.usage.current_provider_calls == 0

            await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_lexical_retrieve_known_match_and_no_match()
    print("Scenario 3 (lexical retrieval without a model) passed")
