"""Copied outside the checkout and run with an installed wheel in isolated Python."""

import asyncio
import json
import re
import sys
from pathlib import Path

import cci
from cci.index import index
from cci.ingest import ingest
from cci.memory import pack_context, prepare_context
from cci.models import InputMessage
from cci.provider import MemoizedProvider, ProviderResponse
from cci.retrieve import retrieve
from cci.store import HistoryStore

assert Path(cci.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert sys.prefix != sys.base_prefix
assert "redis" not in sys.modules
FIXTURE = json.loads(Path("tree-memory.json").read_text())


class Router:
    """Deterministic routing double; this check makes no model-quality claim."""

    async def complete(self, request):
        blocks = re.findall(
            r"<<<CCI_EVIDENCE id=(\S+) source=\S+\n(.*?)\nCCI_EVIDENCE_END>>>",
            request.evidence_context, re.S,
        )
        if request.operation == "indexing":
            result = {"title": "Conversation topics", "summary": " | ".join(text for _, text in blocks)}
        else:
            result = {"node_ids": [nid for nid, text in blocks if FIXTURE["navigation_marker"] in text]}
        return ProviderResponse(text=json.dumps(result))


async def main():
    phase, path = sys.argv[1:]
    async with await HistoryStore.open(path, config={
        "cache_backend": "none", "tree_max_children": 2, "target_chunk_size_scalars": 1,
    }) as store:
        provider = MemoizedProvider(Router(), store.config)
        if phase == "seed":
            await ingest(store, store.history_id, [InputMessage(**m) for m in FIXTURE["messages"]],
                         "package-check", "initial")
            report = await index(store, provider)
            assert report.status == "complete" and report.provider_usage.current_provider_calls > 0
            assert (await index(store, provider)).provider_usage.current_provider_calls == 0
            # Leave a recent, unindexed turn to verify continuation alongside older tree evidence.
            await ingest(store, store.history_id, [InputMessage(**FIXTURE["next_message"])],
                         "package-check", "next")

        messages = await store.get_messages(1, 100)
        expected = FIXTURE["messages"] + [FIXTURE["next_message"]]
        assert [m.original_payload["content"] for m in messages] == [m["content"] for m in expected]
        result = {"history_id": store.history_id, "message_ids": [m.message_id for m in messages],
                  "package_path": cci.__file__}
        if phase == "read":
            assert not (await retrieve(store, FIXTURE["query"], mode="lexical")).evidence
            context = await prepare_context(
                store, FIXTURE["query"], mode="tree", provider=provider,
                recent_messages=2, max_messages=4, max_chars=1200, excerpt_chars=200,
                max_tokens=1000, token_counter=lambda text: len(text.encode("utf8")),
            )
            assert context.retrieval.routing.actual_mode == "tree"
            assert context.retrieval.routing.selected_chunk_ids
            assert 0 < context.retrieval.usage.current_provider_calls <= 4
            assert context.token_count == len(context.text.encode("utf8")) <= 1000
            assert len(context.text) <= 1200 and len(context.items) <= 4
            assert FIXTURE["required_source_seq"] in [item.seq for item in context.items]
            assert len(messages) in [item.seq for item in context.items]
            assert "Conversation topics" not in context.text
            for item in context.items:
                original = messages[item.seq - 1]
                assert item.message_id == original.message_id and item.source_pointer == "/content"
                assert item.excerpt in original.original_payload["content"]
            tiny = pack_context(context.items, max_chars=1)
            assert tiny.text == "" and tiny.omitted_candidates == len(context.items)
            result.update(text=context.text, sequences=[item.seq for item in context.items],
                          chunks=context.retrieval.routing.selected_chunk_ids)
        print(json.dumps(result))


asyncio.run(main())
