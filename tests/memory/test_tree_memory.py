"""Real SQLite trees with a deterministic provider double, not an LLM quality benchmark."""
import asyncio
import json
import re
import subprocess
import time
from pathlib import Path
from dataclasses import replace

import pytest

from cci.clear import clear_history
from cci.ask import ask
from cci.config import Config
from cci.errors import ProviderTimeout, VersionConflict
from cci.index import index
from cci.ingest import ingest
from cci.io_worker import fetchall
from cci.memory import ContextItem, pack_context, prepare_context
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderRequest, ProviderResponse
from cci.retrieve import retrieve
from cci.store import HistoryStore

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "spec/fixtures/tree-memory.json").read_text())


class Router:
    """Echo summaries and select a fixture marker. No semantic-quality claim."""
    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        blocks = re.findall(r"<<<CCI_EVIDENCE id=(\S+) source=\S+\n(.*?)\nCCI_EVIDENCE_END>>>", request.evidence_context, re.S)
        if request.operation == "indexing":
            result = {"title": "Conversation topics", "summary": " | ".join(text for _, text in blocks)}
        else:
            result = {"node_ids": [nid for nid, text in blocks if "ORCHID" in text]}
        return ProviderResponse(text=json.dumps(result), input_tokens=11, output_tokens=7)


async def seed(path, **overrides):
    config = Config(cache_backend="none", target_chunk_size_scalars=1, tree_max_children=2, **overrides)
    store = await HistoryStore.open(str(path), config=config)
    await ingest(store, store.history_id, [InputMessage(**m) for m in FIXTURE["messages"]], "test", "seed")
    return store


async def nodes(store):
    return await fetchall(await store.connection.execute("SELECT node_id, parent_id, sibling_order, title, summary FROM nodes"))


def test_persisted_multilevel_tree_incremental_reuse_and_paraphrase_retrieval(tmp_path):
    async def run():
        path = tmp_path / "memory.db"
        store = await seed(path)
        router = Router()
        provider = MemoizedProvider(router, store.config)
        report = await index(store, provider)
        assert report.status == "complete" and report.provider_usage.current_provider_calls == 14
        assert report.provider_usage.input_tokens == 14 * 11
        original_nodes = await nodes(store)
        assert len(original_nodes) == 14
        assert len([n for n in original_nodes if n[1] is None]) == 2
        assert all(sum(n[1] == parent[0] for n in original_nodes) <= 2 for parent in original_nodes)
        assert (await index(store, provider)).provider_usage.current_provider_calls == 0
        await ingest(store, store.history_id, [InputMessage(role="assistant", content="Next step: prepare the deployment checklist.")], "test", "next")
        update = await index(store, provider)
        assert update.provider_usage.current_provider_calls == 2  # one leaf, one changed ancestor
        updated = {n[0]: n for n in await nodes(store)}
        assert all(updated[n[0]][3:] == n[3:] for n in original_nodes)
        await store.aclose()
        async with await HistoryStore.open(str(path), config=Config(cache_backend="none", tree_max_children=2)) as reopened:
            provider = MemoizedProvider(router, reopened.config)
            lexical = await retrieve(reopened, "Where should the service launch?", mode="lexical")
            assert not lexical.evidence
            context = await prepare_context(reopened, "Where should the service launch?", provider=provider, mode="tree")
            assert "Oslo" in context.text and "deployment checklist" in context.text
            assert context.retrieval.routing.actual_mode == "tree"
            assert context.retrieval.routing.selected_chunk_ids
            assert 0 < context.retrieval.usage.current_provider_calls <= 4
            assert all(e.source_pointer == "/content" for e in context.retrieval.evidence)
            assert "Conversation topics" not in context.text  # summaries never become evidence
    asyncio.run(run())


@pytest.mark.parametrize("output", ['{"node_ids":["n_foreign"]}', '{"node_ids":"bad"}', 'not json'])
def test_unknown_or_invalid_model_ids_fall_back_to_original_evidence(tmp_path, output):
    async def run():
        async with await seed(tmp_path / "bad.db") as store:
            await index(store, MemoizedProvider(Router(), store.config))
            fake = FakeProvider([ProviderResponse(output)])
            result = await retrieve(store, "ORCHID", mode="tree", provider=MemoizedProvider(fake, store.config))
            assert result.routing.actual_mode == "lexical" and not result.routing.selected_chunk_ids
            assert len(result.evidence) == 1 and "Oslo" in result.evidence[0].excerpt
            assert any(d.code == "invalid_tree_selection" for d in result.diagnostics)
    asyncio.run(run())


def test_retry_attempts_share_request_budget_and_navigation_is_bounded(tmp_path):
    async def run():
        async with await seed(tmp_path / "bounded.db", provider_attempt_limit_retrieve=1) as store:
            await index(store, MemoizedProvider(Router(), store.config))
            fake = FakeProvider(fail_times=10, fail_error=ProviderTimeout("fixture timeout"))
            result = await retrieve(store, "ORCHID", mode="tree", provider=MemoizedProvider(fake, store.config))
            assert fake.call_count == result.usage.current_provider_calls == 1
            assert result.usage.usage_unknown and result.evidence
            limited_store_config = replace(store.config, max_tree_navigation_calls=1)
            store.config = limited_store_config
            router = Router()
            result = await retrieve(store, "ORCHID", mode="tree", provider=MemoizedProvider(router, store.config))
            assert len(router.requests) == 1 and result.coverage.coverage_limited
            assert any(d.code == "tree_navigation_limited" for d in result.diagnostics)
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["clear", "reindex", "append"])
def test_navigation_releases_database_lock_and_obeys_snapshot(tmp_path, operation):
    async def run():
        async with await seed(tmp_path / "race.db") as store:
            await index(store, MemoizedProvider(Router(), store.config))
            roots = [n[0] for n in await nodes(store) if n[1] is None]
            entered, resume = asyncio.Event(), asyncio.Event()
            fake = FakeProvider([ProviderResponse(json.dumps({"node_ids": roots}))], entered=entered, resume=resume)
            task = asyncio.create_task(retrieve(store, "ORCHID", mode="tree", provider=MemoizedProvider(fake, store.config)))
            await asyncio.wait_for(entered.wait(), 2)
            if operation == "clear":
                await clear_history(store, store.history_id)
            elif operation == "reindex":
                await index(store, MemoizedProvider(Router(), store.config), rebuild=True)
            else:
                await ingest(store, store.history_id, [InputMessage(role="user", content="LATER_ONLY")], "test", "later")
            resume.set()
            if operation == "clear":
                with pytest.raises(VersionConflict):
                    await task
            else:
                result = await task
                assert all(e.seq <= 8 and "LATER_ONLY" not in e.excerpt for e in result.evidence)
                if operation == "reindex":
                    assert not result.routing.selected_chunk_ids
                    assert any(d.code == "tree_revision_changed" for d in result.diagnostics)
    asyncio.run(run())


def test_public_context_token_counter_and_structured_sources(tmp_path):
    async def run():
        async with await HistoryStore.open(str(tmp_path / "blocks.db"), config=Config(cache_backend="none")) as store:
            content = [{"type": "text", "text": "First block."}, {"type": "image", "url": "ignored"}, {"type": "text", "text": "ORCHID 🌍 second block."}]
            await ingest(store, store.history_id, [InputMessage(role="user", content=content)], "test", "blocks")
            context = await prepare_context(store, "ORCHID", max_tokens=1000, token_counter=lambda s: len(s.encode("utf8")))
            evidence = (await retrieve(store, "ORCHID", mode="lexical")).evidence[0]
            assert evidence.source_pointer == "/content/2/text" and evidence.excerpt == content[2]["text"]
            assert context.token_count == len(context.text.encode("utf8")) <= 1000
            assert {i.source_pointer for i in context.items} == {"/content/0/text", "/content/2/text"}
            assert (await store.get_messages(1, 1))[0].original_payload["content"] == content
            with pytest.raises(ValueError):
                await prepare_context(store, "ORCHID", max_tokens=100)
    asyncio.run(run())
    packed = pack_context([ContextItem("m_one", 1, "/content", "🌍" * 100)], max_chars=1000,
                          max_tokens=250, token_counter=lambda s: len(s.encode("utf8")))
    assert packed.token_count <= 250 and packed.omitted_candidates == 1


@pytest.mark.parametrize("writer", ["python", "typescript"])
def test_persisted_tree_is_readable_across_native_runtimes(tmp_path, writer):
    async def run():
        path = tmp_path / "shared.db"
        if writer == "python":
            async with await seed(path) as store:
                await index(store, MemoizedProvider(Router(), store.config))
                history_id = store.history_id
            result = subprocess.run(["node", str(ROOT / "tests/memory/runtime_roundtrip.mjs"), "read", str(path)],
                                    check=True, capture_output=True, text=True, timeout=20)
            context = json.loads(result.stdout)
            assert context["historyId"] == history_id
            assert "Oslo" in context["text"] and context["chunks"] and context["calls"] <= 4
        else:
            result = subprocess.run(["node", str(ROOT / "tests/memory/runtime_roundtrip.mjs"), "seed", str(path)],
                                    check=True, capture_output=True, text=True, timeout=20)
            original = json.loads(result.stdout)
            async with await HistoryStore.open(str(path), config=Config(cache_backend="none", tree_max_children=2)) as store:
                context = await prepare_context(store, FIXTURE["query"], provider=MemoizedProvider(Router(), store.config))
                assert store.history_id == original["historyId"]
                assert "Oslo" in context.text and context.retrieval.routing.selected_chunk_ids == original["chunks"]
    asyncio.run(run())


@pytest.mark.parametrize("limit", [3, 4])
def test_ask_shares_navigation_and_synthesis_budget(tmp_path, limit):
    class AnswerRouter(Router):
        async def complete(self, request):
            if request.operation == "synthesis":
                assert "Oslo" in request.evidence_context
                return ProviderResponse('{"answer":"Oslo", "citations":["ev_1"]}', input_tokens=11, output_tokens=7)
            return await super().complete(request)
    async def run():
        async with await seed(tmp_path / "answer.db", provider_attempt_limit_ask=limit) as store:
            await index(store, MemoizedProvider(Router(), store.config))
            result = await ask(store, FIXTURE["query"], MemoizedProvider(AnswerRouter(), store.config), mode="tree")
            assert result.usage.current_provider_calls == limit
            assert result.status == ("answered" if limit == 4 else "partial")
            assert result.usage.input_tokens == limit * 11
    asyncio.run(run())


def test_partial_index_reserves_parent_summary_budget(tmp_path):
    async def run():
        async with await seed(tmp_path / "partial.db", provider_attempt_limit_index=4) as store:
            provider = MemoizedProvider(Router(), store.config)
            previous_end = 0
            for _ in range(8):
                report = await index(store, provider)
                assert report.provider_usage.current_provider_calls <= 4
                assert report.committed_coverage.end_seq > previous_end
                previous_end = report.committed_coverage.end_seq
                tree = await nodes(store)
                assert len([n for n in tree if n[1] is None]) <= 2
                if report.status == "complete":
                    break
            assert previous_end == 8
    asyncio.run(run())


def test_provider_admission_wait_obeys_request_deadline():
    async def run():
        entered, resume = asyncio.Event(), asyncio.Event()
        fake = FakeProvider(entered=entered, resume=resume)
        provider = MemoizedProvider(fake, Config(per_provider_concurrency=1))
        request = ProviderRequest("tree_navigation", "fixture", "")
        first = asyncio.create_task(provider.complete(request, deadline_at=time.monotonic() + 2))
        await entered.wait()
        try:
            with pytest.raises(ProviderTimeout, match="admission"):
                await provider.complete(request, deadline_at=time.monotonic() + 0.02)
            assert fake.call_count == 1
        finally:
            resume.set()
            await first
    asyncio.run(run())


def test_rag_adapter_uses_host_documents_and_persists_completed_turn(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("rag_chat_example", ROOT / "examples/python/rag_chat.py")
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    async def run():
        async with await seed(tmp_path / "rag.db") as store:
            async def documents(question):
                return ["Current deployment runbook from the host's document store."]
            async def generate(**request):
                assert "Oslo" in request["conversation_memory"]
                assert request["documents"] == await documents(request["question"])
                saved = await store.get_messages(9, 9)
                assert saved[0].role == "user"  # Durable even if generation now fails.
                return "Use Oslo and follow the current runbook."
            answer, _ = await example.chat_turn(store, "ORCHID", "turn-one", retrieve_documents=documents, generate=generate)
            assert (await store.get_messages(10, 10))[0].original_payload["content"] == answer
    asyncio.run(run())


def test_synthesis_retry_budget_exhaustion_keeps_evidence(tmp_path):
    class FailingAnswer(Router):
        async def complete(self, request):
            if request.operation == "synthesis":
                raise ProviderTimeout("fixture")
            return await super().complete(request)
    async def run():
        async with await seed(tmp_path / "retry-answer.db", provider_attempt_limit_ask=4) as store:
            await index(store, MemoizedProvider(Router(), store.config))
            result = await ask(store, FIXTURE["query"], MemoizedProvider(FailingAnswer(), store.config), mode="tree")
            assert result.status == "partial" and result.answer is None and result.evidence
            assert result.reason == "provider_budget_exhausted" and result.usage.current_provider_calls == 4
    asyncio.run(run())
