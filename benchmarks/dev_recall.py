"""Development evidence recall for `prepare_context` with the pre-registered bar (plan-eng-review D18).

No model calls. `--mode tree` uses a fake navigator that keeps every offered node, so it measures
loss after perfect navigation, not real navigation quality. Iteration evidence only: this never
reads the held-out split and never produces SC-011 release evidence.

Scoring follows SC-011 as clarified on 2026-09-23: required evidence counts when a returned
excerpt contains the acceptable span of the annotated message or of any word-for-word copy of it
in the same history. `context_recall_strict` keeps the original annotated-message-only scorer.

Bar (D18): dev context recall >= 0.85 in both modes, dev correction-case recall >= 0.85, zero
correction cases whose context shows a superseded value without the current one, paraphrase
"names the old value" recall >= 0.80, and against a baseline report: at most 2 points worse on
dev, not worse on paraphrase.

Usage:
  python dev_recall.py [--mode lexical|tree] [--fixture PATH] [--baseline report.json] [--out report.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV_FIXTURE = ROOT / "evaluations" / "held_out" / "fixture.json"
CONTEXT = {"recent_messages": 4, "max_messages": 8, "max_chars": 4000, "excerpt_chars": 200}
FLOORS = {"context_recall": 0.85, "correction_context_recall": 0.85, "names_old_value_recall": 0.80}


class KeepAllNodes:
    async def complete(self, request):
        from cci.provider import ProviderResponse

        if request.operation == "indexing":
            value = {"title": "Development conversation", "summary": "Planning decisions and maintenance."}
        else:
            value = {"node_ids": re.findall(r"<<<CCI_EVIDENCE id=(\S+)", request.evidence_context)}
        return ProviderResponse(json.dumps(value), input_tokens=10, output_tokens=10)


def _covered(units: list[dict], messages: list[dict], items: list, copies: bool) -> float:
    """Fraction of units whose acceptable span appears in an item from an acceptable message."""
    if not units:
        return 1.0
    covered = 0
    for unit in units:
        target = messages[unit["message_index"]]["content"]
        span = target[unit["acceptable_span"]["start"] : unit["acceptable_span"]["end"]]
        seqs = (
            {i + 1 for i, m in enumerate(messages) if m["content"] == target}
            if copies
            else {unit["message_index"] + 1}
        )
        covered += any(item.seq in seqs and span in item.excerpt for item in items)
    return covered / len(units)


async def _run(mode: str, fixture_path: Path) -> dict:
    import cci
    from cci.config import Config
    from cci.index import index
    from cci.ingest import ingest
    from cci.memory import prepare_context
    from cci.models import InputMessage
    from cci.provider import MemoizedProvider
    from cci.store import HistoryStore

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    split = "dev" if fixture_path == DEV_FIXTURE else "paraphrase"
    histories = {h["history_label"]: h for h in fixture["histories"] if h["split"] == split}
    queries = [q for q in fixture["queries"] if q["split"] == split and q["answerable"]]
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
                    units, messages = query["required_evidence_units"], history["messages"]
                    stale_values = (query.get("answer_rubric") or {}).get("must_not_cite_values") or []
                    context_recall = _covered(units, messages, context.items, copies=True)
                    rows.append(
                        {
                            "query_id": query["query_id"],
                            "bucket": query.get("bucket"),
                            "correction_case": bool(stale_values),
                            "retrieval_recall": _covered(
                                units, messages, context.retrieval.evidence, copies=True
                            ),
                            "context_recall": context_recall,
                            "context_recall_strict": _covered(units, messages, context.items, copies=False),
                            "stale_only": bool(stale_values)
                            and context_recall < 1
                            and any(
                                value in item.excerpt for item in context.items for value in stale_values
                            ),
                        }
                    )

    def mean(key: str, subset: list[dict]) -> float | None:
        return sum(r[key] for r in subset) / len(subset) if subset else None

    corrections = [r for r in rows if r["correction_case"]]
    return {
        "mode": mode,
        "fixture": str(fixture_path.relative_to(ROOT)),
        "split": split,
        "context": CONTEXT,
        "cci_module_path": cci.__file__,
        "queries": len(rows),
        "retrieval_recall": mean("retrieval_recall", rows),
        "context_recall": mean("context_recall", rows),
        "context_recall_strict": mean("context_recall_strict", rows),
        "correction_cases": len(corrections),
        "correction_context_recall": mean("context_recall", corrections),
        "stale_only_contexts": [r["query_id"] for r in rows if r["stale_only"]],
        "bucket_context_recall": {
            bucket: mean("context_recall", [r for r in rows if r["bucket"] == bucket])
            for bucket in sorted({r["bucket"] for r in rows if r["bucket"]})
        },
        "misses": [r["query_id"] for r in rows if r["context_recall"] < 1],
    }


def _bar(report: dict, baseline: dict | None) -> dict:
    bar: dict[str, dict] = {}
    if report["split"] == "dev":
        bar["context_recall"] = {"value": report["context_recall"], "floor": FLOORS["context_recall"]}
        bar["correction_context_recall"] = {
            "value": report["correction_context_recall"],
            "floor": FLOORS["correction_context_recall"],
        }
        bar["stale_only_contexts"] = {"value": len(report["stale_only_contexts"]), "max": 0}
    else:
        bar["names_old_value_recall"] = {
            "value": report["bucket_context_recall"].get("names_old_value"),
            "floor": FLOORS["names_old_value_recall"],
        }
    if baseline is not None:
        bar["vs_baseline"] = {
            "value": report["context_recall"] - baseline["context_recall"],
            "min": -0.02 if report["split"] == "dev" else 0.0,
            "baseline": baseline.get("cci_module_path"),
        }
    for check in bar.values():
        value = check["value"]
        if value is None:
            check["pass"] = False
        elif "floor" in check:
            check["pass"] = value >= check["floor"]
        elif "max" in check:
            check["pass"] = value <= check["max"]
        else:
            check["pass"] = value >= check["min"] - 1e-9
    return bar


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=("lexical", "tree"), default="lexical")
    parser.add_argument("--fixture", type=Path, default=DEV_FIXTURE)
    parser.add_argument("--baseline", type=Path, default=None, help="report from the same mode and fixture")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    report = asyncio.run(_run(args.mode, args.fixture.resolve()))
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    report["bar"] = _bar(report, baseline)
    report["bar_pass"] = all(check["pass"] for check in report["bar"].values())
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0 if report["bar_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
