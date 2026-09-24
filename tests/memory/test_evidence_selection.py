"""Public memory regressions using shared, invented development histories."""

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from cci.config import Config
from cci.index import index
from cci.ingest import ingest
from cci.memory import prepare_context
from cci.models import InputMessage
from cci.provider import MemoizedProvider, ProviderRequest, ProviderResponse
from cci.retrieve import retrieve
from cci.search import search
from cci.store import HistoryStore

CASES = json.loads((Path(__file__).parents[2] / "spec/fixtures/evidence-selection.json").read_text())["cases"]


class SelectLeaves:
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        if request.operation == "indexing":
            value = {"title": "Development conversation", "summary": "Planning decisions and maintenance."}
        else:
            assert request.operation == "tree_navigation"
            value = {"node_ids": re.findall(r"<<<CCI_EVIDENCE id=(\S+)", request.evidence_context)}
        return ProviderResponse(json.dumps(value), input_tokens=10, output_tokens=10)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
@pytest.mark.parametrize("mode", ["lexical", "tree"])
def test_relevant_originals_survive_selection_and_limits(tmp_path: Path, case: dict, mode: str) -> None:
    async def run() -> None:
        async with await HistoryStore.open(str(tmp_path / "history.db"), config=Config(cache_backend="none")) as store:
            await ingest(store, store.history_id, [InputMessage(**m) for m in case["messages"]], "unit", "seed")
            provider = MemoizedProvider(SelectLeaves(), store.config)
            assert (await index(store, provider)).status == "complete"
            store.config = replace(store.config, max_evidence_text_scalars=case.get("evidence_budget", 24000))
            result = await retrieve(store, case["query"], mode=mode, provider=provider)
            context = await prepare_context(store, case["query"], mode=mode, provider=provider)
            for required in case["required"]:
                for items in (result.evidence, context.items):
                    assert any(item.seq == required["seq"] and item.source_pointer == required["pointer"]
                               and required["contains"] in item.excerpt for item in items)
            assert len(context.items) <= 8 and len(context.text) <= 4000
            assert context.items == sorted(context.items, key=lambda item: item.seq)
            assert {37, 38, 39, 40} <= {item.seq for item in context.items}
            assert len({(item.message_id, item.source_pointer) for item in context.items}) == len(context.items)
            assert context.retrieval.usage.current_provider_calls == (1 if mode == "tree" else 0)
            for item in context.items:
                content = case["messages"][item.seq - 1]["content"]
                original = content if isinstance(content, str) else content[int(item.source_pointer.split("/")[2])]["text"]
                assert item.excerpt in original
    asyncio.run(run())


@pytest.mark.parametrize("query", ["", "   ", '" : * ()', "where is the", "NONEXISTENT_7342"])
def test_empty_or_absent_search_does_not_manufacture_evidence(tmp_path: Path, query: str) -> None:
    async def run() -> None:
        async with await HistoryStore.open(str(tmp_path / "empty.db"), config=Config(cache_backend="none")) as store:
            await ingest(store, store.history_id, [InputMessage(role="user", content="A release codename exists.")], "unit", "one")
            assert not (await search(store, query)).candidates
    asyncio.run(run())
