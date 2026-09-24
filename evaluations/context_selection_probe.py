"""Independent development probe of candidate loss during bounded context assembly.

Uses invented messages and a deterministic provider that selects the only offered leaf.
No reserved evaluation labels, credentials, or external model requests are used.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

import cci
from cci.config import Config
from cci.index import index
from cci.ingest import ingest
from cci.memory import prepare_context
from cci.models import InputMessage
from cci.provider import MemoizedProvider, ProviderRequest, ProviderResponse
from cci.retrieve import retrieve
from cci.search import search
from cci.store import HistoryStore


class SelectLeaf:
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        value: dict[str, object]
        if request.operation == "indexing":
            value = {"title": "Release planning", "summary": "A release codename was chosen."}
        elif request.operation == "tree_navigation":
            value = {"node_ids": re.findall(r"<<<CCI_EVIDENCE id=([^\s]+)", request.evidence_context)}
        else:
            raise ValueError("unexpected_probe_operation")
        return ProviderResponse(json.dumps(value))


async def run() -> dict:
    expected_seq = 13
    fact = "Decision: the release codename is AZURE-MOTH."
    query = "Which release codename was chosen?"
    messages = [InputMessage(role="user", content=f"Routine progress note number {i}.") for i in range(28)]
    messages[expected_seq - 1] = InputMessage(role="user", content=fact)
    with tempfile.TemporaryDirectory(prefix="cci-context-probe-") as directory:
        async with await HistoryStore.open(
            str(Path(directory) / "history.db"),
            config=Config(cache_backend="none"),
        ) as store:
            await ingest(store, store.history_id, messages, "development-probe", "initial")
            provider = MemoizedProvider(SelectLeaf(), store.config)
            indexed = await index(store, provider)
            raw = await retrieve(store, query, mode="tree", provider=provider)
            context = await prepare_context(store, query, mode="tree", provider=provider)
            keyword = await search(store, "codename")
            natural = await search(store, query)
            return {
                "scope": "Invented messages; deterministic leaf selection; zero real-model calls.",
                "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "runtime_package": cci.__file__,
                "message_count": len(messages),
                "expected_seq": expected_seq,
                "expected_original": fact,
                "query": query,
                "index_status": indexed.status,
                "tree_mode": raw.routing.actual_mode,
                "raw_retrieval_contains_required_source": any(e.seq == expected_seq for e in raw.evidence),
                "prepared_context_contains_required_source": any(
                    e.seq == expected_seq for e in context.items
                ),
                "prepared_source_sequences": [e.seq for e in context.items],
                "prepared_context": asdict(context),
                "keyword_search_candidates": len(keyword.candidates),
                "natural_question_search_candidates": len(natural.candidates),
                "interpretation": "A selected leaf can contain the needed source while bounded context "
                "omits it. This probes a mechanism, not the cause of every held-out miss.",
            }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run())
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "prepared_context"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
