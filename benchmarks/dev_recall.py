"""Development-split evidence recall for `prepare_context` (PRD §16.1 scorer; no model calls).

Scores the 60 answerable dev queries with the release scorer (`_score_evidence_recall`) at the
held-out comparison plan's context limits. `--mode tree` uses a fake navigator that keeps every
offered node, so it measures loss after perfect navigation, not real navigation quality.
Uses only the dev split. This is iteration evidence, not SC-011 release evidence.

Usage: python dev_recall.py [--mode lexical|tree] [--out report.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

HELD_OUT_DIR = Path(__file__).resolve().parents[1] / "evaluations" / "held_out"
CONTEXT = {"recent_messages": 4, "max_messages": 8, "max_chars": 4000, "excerpt_chars": 200}


class KeepAllNodes:
    async def complete(self, request):
        from cci.provider import ProviderResponse

        if request.operation == "indexing":
            value = {"title": "Development conversation", "summary": "Planning decisions and maintenance."}
        else:
            value = {"node_ids": re.findall(r"<<<CCI_EVIDENCE id=(\S+)", request.evidence_context)}
        return ProviderResponse(json.dumps(value), input_tokens=10, output_tokens=10)


async def _run(mode: str) -> dict:
    import cci
    from cci.config import Config
    from cci.index import index
    from cci.ingest import ingest
    from cci.memory import prepare_context
    from cci.models import InputMessage
    from cci.provider import MemoizedProvider
    from cci.store import HistoryStore

    sys.path.insert(0, str(HELD_OUT_DIR))
    from run_evaluation import _score_evidence_recall as score

    fixture = json.loads((HELD_OUT_DIR / "fixture.json").read_text())
    histories = {h["history_label"]: h for h in fixture["histories"] if h["split"] == "dev"}
    queries = [q for q in fixture["queries"] if q["split"] == "dev" and q["answerable"]]
    rows = []
    with tempfile.TemporaryDirectory() as d:
        for label, history in histories.items():
            config = Config(cache_backend="none", application_namespace=label)
            async with await HistoryStore.open(str(Path(d) / f"{label}.db"), config=config) as store:
                for n, raw in enumerate(history["messages"]):
                    message = InputMessage(
                        role=raw["role"],
                        content=raw["content"],
                        tool_calls=raw.get("tool_calls"),
                        tool_call_id=raw.get("tool_call_id"),
                    )
                    await ingest(store, store.history_id, [message], raw["source_id"], f"m{n}")
                provider = None
                if mode == "tree":
                    provider = MemoizedProvider(KeepAllNodes(), store.config)
                    for _ in range(64):
                        if (await index(store, provider)).status == "complete":
                            break
                for query in (q for q in queries if q["history_label"] == label):
                    context = await prepare_context(
                        store, query["query_text"], mode=mode, provider=provider, **CONTEXT
                    )
                    units = query["required_evidence_units"]
                    rows.append(
                        {
                            "query_id": query["query_id"],
                            "retrieval_recall": score(units, history["messages"], context.retrieval.evidence),
                            "context_recall": score(units, history["messages"], context.items),
                        }
                    )
    mean = lambda key: sum(r[key] for r in rows) / len(rows)  # noqa: E731
    return {
        "mode": mode,
        "split": "dev",
        "context": CONTEXT,
        "cci_module_path": cci.__file__,
        "queries": len(rows),
        "retrieval_recall": mean("retrieval_recall"),
        "context_recall": mean("context_recall"),
        "misses": [r["query_id"] for r in rows if r["context_recall"] < 1],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=("lexical", "tree"), default="lexical")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    report = asyncio.run(_run(args.mode))
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
