"""Copied outside the checkout and run with an installed wheel in isolated Python."""

import asyncio
import json
import re
import sys
from pathlib import Path

import cci
from cci.errors import SchemaVersionError
from cci.index import index
from cci.ingest import ingest
from cci.memory import pack_context, prepare_context
from cci.models import InputMessage
from cci.provider import MemoizedProvider, ProviderResponse
from cci.retrieve import retrieve
from cci.search import search
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
    if phase == "migrate":
        try:
            unexpected = await HistoryStore.open(path, config={"cache_backend": "none"})
        except SchemaVersionError:
            pass
        else:
            await unexpected.aclose()
            raise AssertionError("v1 open must require explicit migration")
        backup = path + ".v1.bak"
        await HistoryStore.migrate(path, backup_path=backup, config={"cache_backend": "none"})
        backup_bytes = Path(backup).read_bytes()
        await HistoryStore.migrate(path, backup_path=backup, config={"cache_backend": "none"})
        assert Path(backup).read_bytes() == backup_bytes
        return
    if phase in ("migration-append", "migration-read"):
        async with await HistoryStore.open(path, config={"cache_backend": "none"}) as store:
            if phase == "migration-append":
                await ingest(
                    store, store.history_id, [InputMessage(role="user", content="Continue in Oslo.")],
                    "after-migration", "continue",
                )
            messages = await store.get_messages(1, 100)
            assert [m.seq for m in messages] == [5, 9, 20, 41]
            assert [m.message_id for m in messages[:3]] == ["m_v1_first", "m_v1_tool", "m_v1_later"]
            result = await search(store, "Oslo", limit=8)
            assert [c.seq for c in result.candidates] == [41, 20, 5]
            print(json.dumps({"history_id": store.history_id, "store_instance_id": store.store_instance_id,
                              "message_ids": [m.message_id for m in messages],
                              "sequences": [c.seq for c in result.candidates],
                              "excerpts": [c.excerpt for c in result.candidates]}))
        return
    if phase == "default":
        async with await HistoryStore.open(path) as store:
            assert store.config.cache_backend == "sqlite"
            await ingest(store, store.history_id, [InputMessage(role="user", content="Target is Oslo.")],
                         "default-check", "initial")
            assert (await retrieve(store, "Oslo", mode="lexical")).evidence
            assert "redis" not in sys.modules and "openai" not in sys.modules
        return
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
