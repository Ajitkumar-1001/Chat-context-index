"""Compare source-evidence coverage on a small, synthetic development fixture.

Run with cci installed: python evaluations/brand_memory.py --out evaluations/results/brand-memory.json
No provider calls, answer-quality judgments, token estimates, or production performance claims.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "brand-memory.json"
sys.path.insert(0, str(ROOT / "examples" / "python"))

import apsw
import cci
from cci.config import Config
from cci.ingest import ingest
from cci.models import InputMessage
from cci.retrieve import retrieve
from cci.store import HistoryStore
from conversation_context import ContextItem, message_item, pack_context, prepare_context

MAX_MESSAGES = 8
RECENT_MESSAGES = 4
MAX_CHARS = 4_000
STRATEGIES = ("recent_only", "lexical_only", "recent_plus_lexical")


def _source_digest() -> str:
    digest = hashlib.sha256()
    package = Path(cci.__file__).parent
    for path in sorted(package.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    digest.update((ROOT / "examples/python/conversation_context.py").read_bytes())
    return digest.hexdigest()


async def evaluate() -> dict:
    source_digest = _source_digest()
    fixture_bytes = FIXTURE.read_bytes()
    fixture = json.loads(fixture_bytes)
    storage_checks = []
    cases = []

    with tempfile.TemporaryDirectory(prefix="cci-evaluation-") as directory:
        async with AsyncExitStack() as stack:
            stores = {}
            records = {}
            for brand, inputs in fixture["histories"].items():
                path = str(Path(directory) / f"{brand}.sqlite")
                messages = [InputMessage(**record) for record in inputs]
                async with await HistoryStore.open(path, config=Config(cache_backend="none")) as store:
                    history_id = store.history_id
                    await ingest(store, history_id, messages, "evaluation", "fixture-v1")
                    before = await store.get_messages(1, len(messages), limit=len(messages))

                store = await stack.enter_async_context(
                    await HistoryStore.open(path, config=Config(cache_backend="none"))
                )
                replay = await ingest(store, store.history_id, messages, "evaluation", "fixture-v1")
                after = await store.get_messages(1, len(messages) + 1, limit=len(messages) + 1)
                storage_checks.append({
                    "history": brand,
                    "same_history_after_reopen": store.history_id == history_id,
                    "same_messages_after_reopen": before == after,
                    "retry_replays_without_duplicates": replay.replayed and len(after) == len(inputs),
                })
                stores[brand] = store
                records[brand] = after

            for case in fixture["cases"]:
                store = stores[case["history"]]
                messages = records[case["history"]]
                source_ids = {message.message_id: message.external_id for message in messages}
                lexical = await retrieve(store, case["query"], mode="lexical", max_selected_chunks=MAX_MESSAGES)
                contexts = {
                    "recent_only": pack_context([
                        item for message in reversed(messages[-MAX_MESSAGES:])
                        if (item := message_item(message))
                    ], max_messages=MAX_MESSAGES, max_chars=MAX_CHARS),
                    "lexical_only": pack_context([
                        ContextItem(e.message_id, e.seq, e.source_pointer, e.excerpt)
                        for e in lexical.evidence
                    ], max_messages=MAX_MESSAGES, max_chars=MAX_CHARS),
                    "recent_plus_lexical": await prepare_context(
                        store, case["query"], recent_messages=RECENT_MESSAGES,
                        max_messages=MAX_MESSAGES, max_chars=MAX_CHARS,
                    ),
                }
                results = {}
                for name, context in contexts.items():
                    covered = [
                        target["external_id"] for target in case["evidence"]
                        if any(
                            source_ids.get(item.message_id) == target["external_id"]
                            and target["contains"] in item.excerpt
                            for item in context.items
                        )
                    ]
                    results[name] = {
                        "selected_sources": [source_ids.get(item.message_id) for item in context.items],
                        "covered_sources": covered,
                        "covered_evidence_units": len(covered),
                        "required_evidence_units": len(case["evidence"]),
                        "complete_evidence": len(covered) == len(case["evidence"]) if case["evidence"] else None,
                        "rendered_chars": len(context.text),
                        "empty_context": not context.items,
                        "omitted_candidates": context.omitted_candidates,
                        "truncated_excerpts": context.truncated_excerpts,
                        "within_budget": len(context.text) <= MAX_CHARS and len(context.items) <= MAX_MESSAGES,
                        "selected_history_only": all(item.message_id in source_ids for item in context.items),
                        "forbidden_text_absent": all(value not in context.text for value in case.get("forbidden_text", [])),
                    }
                cases.append({"id": case["id"], "history": case["history"], "query": case["query"], "strategies": results})

    if _source_digest() != source_digest:
        raise RuntimeError("implementation changed during evaluation; rerun against a stable checkout")
    summaries = {}
    for strategy in STRATEGIES:
        results = [case["strategies"][strategy] for case in cases]
        answerable = [result for result in results if result["required_evidence_units"]]
        summaries[strategy] = {
            "answerable_cases": len(answerable),
            "cases_with_complete_evidence": sum(result["complete_evidence"] for result in answerable),
            "covered_evidence_units": sum(result["covered_evidence_units"] for result in answerable),
            "required_evidence_units": sum(result["required_evidence_units"] for result in answerable),
            "context_budget_violations": sum(not result["within_budget"] for result in results),
            "history_scope_violations": sum(not result["selected_history_only"] or not result["forbidden_text_absent"] for result in results),
        }
    return {
        "fixture": fixture["name"],
        "scope": fixture["description"],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": platform.python_version(), "platform": platform.system(), "machine": platform.machine(), "apsw": apsw.apsw_version(), "sqlite": apsw.sqlite_lib_version()},
        "fingerprints": {"implementation_sha256": source_digest, "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest()},
        "limits": {"max_messages": MAX_MESSAGES, "max_rendered_chars": MAX_CHARS, "max_excerpt_chars": 200, "combined_recent_reserve": RECENT_MESSAGES},
        "provider_calls": 0,
        "not_measured": ["answer correctness", "semantic citation support", "model abstention", "tokens", "provider cost", "latency comparison", "production CloneIQ behavior", "TypeScript parity"],
        "storage_checks": storage_checks,
        "summary": summaries,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the complete JSON report")
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(report["scope"])
    for name, result in report["summary"].items():
        print(f"{name}: complete evidence in {result['cases_with_complete_evidence']}/{result['answerable_cases']} answerable cases; history violations={result['history_scope_violations']}; budget violations={result['context_budget_violations']}")
    print("Provider calls: 0. Answer correctness and model abstention: NOT RUN.")
    storage_ok = all(all(value for key, value in check.items() if key != "history") for check in report["storage_checks"])
    invariants_ok = all(not r["history_scope_violations"] and not r["context_budget_violations"] for r in report["summary"].values())
    # Recall misses are measured findings. They must remain in the report, not become a
    # threshold tuned on this development fixture. Lifecycle/boundary failures are fatal.
    return 0 if storage_ok and invariants_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
