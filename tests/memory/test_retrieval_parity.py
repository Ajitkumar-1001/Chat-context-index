"""Exact, ordered lexical retrieval and context parity against the native TypeScript runtime."""

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "packages/python/src"))

from cci.config import Config
from cci.index import index
from cci.ingest import ingest
from cci.memory import prepare_context
from cci.models import InputMessage
from cci.provider import (
    MemoizedProvider,
    ProviderRequest,
    ProviderResponse,
)
from cci.retrieve import retrieve
from cci.search import search
from cci.store import HistoryStore

CASES = json.loads((ROOT / "spec/fixtures/retrieval-parity.json").read_text())["cases"]


class _SelectAll:
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        value = {"title": "Conversation", "summary": "Planning decisions."} if request.operation == "indexing" else {
            "node_ids": re.findall(r"<<<CCI_EVIDENCE id=(\S+)", request.evidence_context)
        }
        return ProviderResponse(json.dumps(value), input_tokens=10, output_tokens=10)


def _python_results(tmp_path: Path) -> dict:
    async def run() -> dict:
        output = {}
        for fixture in CASES:
            async with await HistoryStore.open(
                str(tmp_path / f"{fixture['id']}.db"), config=Config(cache_backend="none")
            ) as store:
                await ingest(store, store.history_id, [InputMessage(**m) for m in fixture["messages"]], "parity", "seed")
                mode = fixture.get("mode", "lexical")
                provider = MemoizedProvider(_SelectAll(), store.config) if mode == "tree" else None
                if provider:
                    assert (await index(store, provider)).status == "complete"
                lexical = await search(store, fixture["query"], limit=fixture["max_messages"])
                result = await retrieve(store, fixture["query"], mode=mode, provider=provider,
                                        max_selected_chunks=fixture["max_messages"])
                context = await prepare_context(
                    store, fixture["query"], mode=mode, provider=provider,
                    recent_messages=fixture.get("recent_messages", 0),
                    max_messages=fixture["max_messages"],
                )
                messages = await store.get_messages(1, len(fixture["messages"]))
                rendered = context.text
                for message in messages:
                    rendered = rendered.replace(message.message_id, f"m_{message.seq}")
                selected_spans = []
                for chunk_id in result.routing.selected_chunk_ids:
                    cursor = await store.connection.execute(
                        "SELECT source_message_span FROM chunks WHERE chunk_id = ?", (chunk_id,)
                    )
                    row = await cursor.fetchone()
                    assert row is not None
                    selected_spans.append(row[0])
                output[fixture["id"]] = {
                    "search": {
                        "candidates": [
                            {
                                "messageId": f"m_{c.seq}", "seq": c.seq,
                                "sourcePointer": c.source_pointer, "excerpt": c.excerpt,
                                "contentHash": c.content_hash,
                            }
                            for c in lexical.candidates
                        ],
                        "diagnostics": [
                            {"code": d.code, "stage": d.stage, "retryable": d.retryable}
                            for d in lexical.diagnostics
                        ],
                    },
                    "retrieval": {
                        "contractVersion": result.contract_version,
                        "status": result.status,
                        "snapshot": {
                            "historyRevision": result.snapshot.history_revision,
                            "indexRevision": result.snapshot.index_revision,
                            "snapshotMaxSeq": result.snapshot.snapshot_max_seq,
                            "cacheGeneration": result.snapshot.cache_generation,
                        },
                        "routing": {
                            "requestedMode": result.routing.requested_mode,
                            "actualMode": result.routing.actual_mode,
                            "candidateCount": result.routing.candidate_count,
                            "selectedChunkIds": selected_spans,
                        },
                        "coverage": {
                            "coverageLimited": result.coverage.coverage_limited,
                            "indexDegraded": result.coverage.index_degraded,
                            "omittedExcerptCount": result.coverage.omitted_excerpt_count,
                        },
                        "diagnostics": [
                            {"code": d.code, "stage": d.stage, "retryable": d.retryable}
                            for d in result.diagnostics
                        ],
                        "usage": {
                            "currentProviderCalls": result.usage.current_provider_calls,
                            "retries": result.usage.retries,
                            "usageUnknown": result.usage.usage_unknown,
                            "memoHits": result.usage.memo_hits,
                            "memoMisses": result.usage.memo_misses,
                            "memoErrors": result.usage.memo_errors,
                            "stageMs": result.usage.stage_ms,
                            "inputTokens": result.usage.input_tokens,
                            "outputTokens": result.usage.output_tokens,
                        },
                        "evidence": [
                            {
                                "evidenceId": e.evidence_id, "messageId": f"m_{e.seq}", "seq": e.seq,
                                "sourcePointer": e.source_pointer, "excerpt": e.excerpt,
                                "contentHash": e.content_hash,
                            }
                            for e in result.evidence
                        ],
                    },
                    "context": {
                        "text": rendered,
                        "items": [
                            {"messageId": f"m_{item.seq}", "seq": item.seq,
                             "sourcePointer": item.source_pointer, "excerpt": item.excerpt}
                            for item in context.items
                        ],
                        "omittedCandidates": context.omitted_candidates,
                        "truncatedExcerpts": context.truncated_excerpts,
                        "tokenCount": context.token_count,
                    },
                }
        return output

    return asyncio.run(run())


def test_exact_python_typescript_retrieval_parity(tmp_path: Path) -> None:
    subprocess.run(["npm", "run", "build"], cwd=ROOT / "packages/typescript", check=True, capture_output=True)
    node = subprocess.run(
        ["node", str(ROOT / "tests/memory/retrieval_parity.mjs")],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=60,
    )
    typescript = json.loads(node.stdout)
    python = _python_results(tmp_path)
    assert python == typescript

    for fixture in CASES:
        result = python[fixture["id"]]
        if "expected_search" in fixture:
            assert [c["seq"] for c in result["search"]["candidates"]] == fixture["expected_search"], fixture["id"]
        evidence = result["retrieval"]["evidence"]
        assert [e["seq"] for e in evidence] == fixture["expected_retrieval"], fixture["id"]
        assert [e["seq"] for e in result["context"]["items"]] == fixture["expected_context"], fixture["id"]
        if "expected_priority" in fixture:
            assert [e["seq"] for e in sorted(evidence, key=lambda e: int(e["evidenceId"].split("_")[1]))] == fixture["expected_priority"]
        if "expected_pointer" in fixture:
            assert evidence[0]["sourcePointer"] == fixture["expected_pointer"]
        if "expected_contains" in fixture:
            assert fixture["expected_contains"] in evidence[0]["excerpt"]
        if "expected_candidate_count" in fixture:
            assert result["retrieval"]["routing"]["candidateCount"] == fixture["expected_candidate_count"]
        if "expected_selected_spans" in fixture:
            assert result["retrieval"]["routing"]["selectedChunkIds"] == fixture["expected_selected_spans"]
        assert all(len(e["excerpt"]) <= 200 for e in evidence)
