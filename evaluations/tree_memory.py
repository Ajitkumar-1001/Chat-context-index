"""Reproducible tree overhead measurement. Deterministic double; no model-quality claims."""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import tempfile

import apsw
import cci
from cci.config import Config
from cci.context_assembly import EvidenceBlock, render_evidence_context
from cci.index import index
from cci.ingest import ingest
from cci.memory import prepare_context
from cci.models import InputMessage
from cci.provider import MemoizedProvider, ProviderResponse
from cci.store import HistoryStore


class MeasuredDouble:
    """Fixture-marker routing only. Record characters actually sent through the provider API."""
    def __init__(self):
        self.calls = []

    async def complete(self, request):
        blocks = re.findall(r"<<<CCI_EVIDENCE id=(\S+) source=\S+\n(.*?)\nCCI_EVIDENCE_END>>>", request.evidence_context, re.S)
        payload = {"title": "Fixture topics", "summary": " | ".join(text for _, text in blocks)[:1200]} if request.operation == "indexing" else {
            "node_ids": [nid for nid, text in blocks if "ORCHID" in text]}
        output = json.dumps(payload)
        self.calls.append({"operation": request.operation, "input_chars": len(request.prompt) + len(request.evidence_context), "output_chars": len(output)})
        return ProviderResponse(output)  # Real token usage is unavailable; never fabricate it.


def fingerprint():
    root = Path(cci.__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


async def evaluate():
    before = fingerprint()
    config = Config(cache_backend="none", tree_max_children=4, target_chunk_size_scalars=1800)
    texts = [
        ("ORCHID: The deployment target is Oslo. " if i == 0 else f"Synthetic unrelated note {i}. ")
        + "Background detail for a long conversation. " * 35
        for i in range(128)
    ]
    with tempfile.TemporaryDirectory(prefix="cci-tree-evaluation-") as directory:
        path = str(Path(directory) / "memory.sqlite")
        async with await HistoryStore.open(path, config=config) as store:
            await ingest(store, store.history_id, [InputMessage(role="user", content=t) for t in texts], "tree-evaluation", "seed")
            recorder = MeasuredDouble()
            provider = MemoizedProvider(recorder, config)
            reports = []
            for _ in range(128):
                report = await index(store, provider)
                reports.append(asdict(report))
                if report.status == "complete":
                    break
            else:
                raise AssertionError("index did not complete within the fixture's bounded loop")
            assert (await index(store, provider)).provider_usage.current_provider_calls == 0
            indexing_calls = list(recorder.calls)
        async with await HistoryStore.open(path, config=config) as reopened:
            recorder = MeasuredDouble()
            context = await prepare_context(reopened, "Where should the service launch?", mode="tree",
                                            provider=MemoizedProvider(recorder, config), recent_messages=2,
                                            max_chars=6000, excerpt_chars=2000)
            messages = await reopened.get_messages(1, 128, limit=128)
            replay = render_evidence_context([EvidenceBlock(m.message_id, "/content", m.text_projection) for m in messages])
            assert any(item.seq == 1 and "Oslo" in item.excerpt for item in context.items)
            assert any(item.seq == 128 for item in context.items)
            assert len(context.text) <= 6000
            routing_chars = sum(c["input_chars"] for c in recorder.calls)
            indexing_chars = sum(c["input_chars"] for c in indexing_calls)
            per_query = routing_chars + len(context.text)
            saving = len(replay) - per_query
            result = {
                "method": "128 synthetic messages; deterministic marker router; same evidence renderer for full replay and selected context",
                "environment": {"python": platform.python_version(), "apsw": apsw.apswversion(), "sqlite": apsw.sqlitelibversion()},
                "source_sha256": before,
                "fixture_sha256": hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest(),
                "history_messages": 128, "source_sequences_returned": [i.seq for i in context.items],
                "indexing": {"provider_calls": len(indexing_calls), "input_chars": indexing_chars,
                             "output_chars": sum(c["output_chars"] for c in indexing_calls), "batches": reports,
                             "unchanged_rerun_provider_calls": 0},
                "query": {"routing": asdict(context.retrieval.routing), "usage": asdict(context.retrieval.usage),
                          "navigation_input_chars": routing_chars, "navigation_output_chars": sum(c["output_chars"] for c in recorder.calls),
                          "memory_context_chars": len(context.text), "full_history_context_chars": len(replay),
                          "navigation_plus_memory_input_chars": per_query,
                          "input_character_break_even_queries": math.ceil(indexing_chars / saving) if saving > 0 else None},
                "not_measured": ["semantic retrieval quality", "generated-answer correctness", "actual model tokens",
                                 "provider prices or dollar savings", "latency comparison", "prompt caching discounts"],
                "limitations": "Character accounting is not token or billing accounting. Both baselines omit common answer instructions, documents, query, and answer output. Indexing and routing use a deterministic double.",
            }
    if fingerprint() != before:
        raise RuntimeError("implementation changed during evaluation; rerun before recording results")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"indexing_calls": report["indexing"]["provider_calls"], **report["query"]}, indent=2))
