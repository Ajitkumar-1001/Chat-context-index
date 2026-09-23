"""Reference-fixture benchmark (plan.md Performance Goals; PRD §12; SC-007-009).

Fixture: 10,000 synthetic messages, seed 7, ~1 KiB avg / 4 KiB max (plan.md Scale/Scope).
Measures (against whatever `cci` is importable in the running interpreter — a wheel-installed
"Python preview" per T070, or the release-candidate artifact per T083):

  - SC-009: store open p95 <= 2s, no model work
  - SC-008: 100-message batch ingest p95 <= 500ms, excluding indexing
  - SC-007: lexical search p95 <= 100ms at top-8

Usage: python reference_fixture_benchmark.py [--out report.json]

Constitution Principle IV: this script only reports what it actually measured, honestly labeled
(interpreter, platform, `cci` install location) — it never claims a threshold was met without
having run the measurement in this process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import random
import sys
import tempfile
import time
from pathlib import Path

SEED = 7
TOTAL_MESSAGES = 10_000
BATCH_SIZE = 100
SEARCH_QUERIES = 50
OPEN_TRIALS = 20

_WORDS = (
    "the quick brown fox jumps over lazy dog conversation memory retrieval index "
    "evidence citation snapshot generation cache history message topic summary "
    "provider synthesis chunk node tree lexical search sqlite redis budget deadline"
).split()


def _synthetic_content(rng: random.Random) -> str:
    # ~1 KiB avg, up to ~4 KiB max (plan.md Scale/Scope).
    target_words = rng.randint(30, 220)
    return " ".join(rng.choice(_WORDS) for _ in range(target_words))


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


async def _run(out_path: str | None, label: str) -> dict:
    import cci
    from cci.ingest import ingest
    from cci.models import InputMessage
    from cci.search import search
    from cci.store import HistoryStore

    rng = random.Random(SEED)

    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "benchmark.db")

        # SC-008: 100-message batch ingest p95, excluding indexing.
        store = await HistoryStore.open(path)
        ingest_latencies_ms: list[float] = []
        sent = 0
        source_id = "bench-src"
        while sent < TOTAL_MESSAGES:
            batch = [
                InputMessage(role="user" if i % 2 == 0 else "assistant", content=_synthetic_content(rng))
                for i in range(BATCH_SIZE)
            ]
            start = time.perf_counter()
            await ingest(store, store.history_id, batch, source_id, f"batch-{sent // BATCH_SIZE}")
            ingest_latencies_ms.append((time.perf_counter() - start) * 1000)
            sent += BATCH_SIZE

        # SC-007: lexical search p95 at top-8.
        search_latencies_ms: list[float] = []
        for _ in range(SEARCH_QUERIES):
            query = " ".join(rng.sample(_WORDS, 2))
            start = time.perf_counter()
            await search(store, query, limit=8)
            search_latencies_ms.append((time.perf_counter() - start) * 1000)

        await store.aclose()

        # SC-009: store open p95, no model work — reopening the now-populated store.
        open_latencies_ms: list[float] = []
        for _ in range(OPEN_TRIALS):
            start = time.perf_counter()
            reopened = await HistoryStore.open(path)
            open_latencies_ms.append((time.perf_counter() - start) * 1000)
            await reopened.aclose()

    report = {
        "fixture": {"total_messages": TOTAL_MESSAGES, "seed": SEED, "batch_size": BATCH_SIZE},
        "environment": {
            "label": label,
            "cci_module_path": cci.__file__,
            "python_version": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "dedicated_linux_runner": False,
        },
        "results": {
            "store_open_ms": {
                "p50": _percentile(open_latencies_ms, 50),
                "p95": _percentile(open_latencies_ms, 95),
                "threshold_p95_ms": 2000,
                "trials": len(open_latencies_ms),
            },
            "batch_ingest_100_ms": {
                "p50": _percentile(ingest_latencies_ms, 50),
                "p95": _percentile(ingest_latencies_ms, 95),
                "threshold_p95_ms": 500,
                "trials": len(ingest_latencies_ms),
            },
            "lexical_search_top8_ms": {
                "p50": _percentile(search_latencies_ms, 50),
                "p95": _percentile(search_latencies_ms, 95),
                "threshold_p95_ms": 100,
                "trials": len(search_latencies_ms),
            },
        },
    }
    report["results"]["store_open_ms"]["pass"] = (
        report["results"]["store_open_ms"]["p95"] <= 2000
    )
    report["results"]["batch_ingest_100_ms"]["pass"] = (
        report["results"]["batch_ingest_100_ms"]["p95"] <= 500
    )
    report["results"]["lexical_search_top8_ms"]["pass"] = (
        report["results"]["lexical_search_top8_ms"]["p95"] <= 100
    )

    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if out_path:
        Path(out_path).write_text(text + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="optional path to write the JSON report")
    parser.add_argument(
        "--label", default="Python-preview",
        help="honest label for this run, e.g. 'Python-preview' (T070) or 'release-candidate' (T083)",
    )
    args = parser.parse_args()
    report = asyncio.run(_run(args.out, args.label))
    all_pass = all(r["pass"] for r in report["results"].values())
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
