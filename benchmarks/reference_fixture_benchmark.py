"""Reference-fixture benchmark (plan.md Performance Goals; PRD §12; SC-007-009).

Fixture: 10,000 synthetic messages, seed 7, ~1 KiB avg / 4 KiB max (plan.md Scale/Scope).
Measures (against whatever `cci` is importable in the running interpreter — a wheel-installed
"Python preview" per T070, or the release-candidate artifact per T083):

  - SC-009: store open p95 <= 2s, no model work
  - SC-008: 100-message batch ingest p95 <= 500ms, excluding indexing
  - SC-007: lexical search p95 <= 100ms at top-8

Usage: python reference_fixture_benchmark.py [--out report.json]

PRD §12 full workload (opt-in; defaults reproduce the T070/T083 workload exactly):

  python reference_fixture_benchmark.py --queries 1000 --concurrency 1,4,16 --cold-trials 20

`--messages` changes history size for scale points; it is not the reference fixture.
Process-cold means a fresh interpreter and SQLite connection; the OS page cache is not dropped.
On an exclusively reserved Linux runner, --filesystem-cold-trials also drops the host page cache
before each fresh-process measurement. This requires root or passwordless sudo for tee.

Constitution Principle IV: this script only reports what it actually measured, honestly labeled
(interpreter, platform, `cci` install location) — it never claims a threshold was met without
having run the measurement in this process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SEED = 7
TOTAL_MESSAGES = 10_000
BATCH_SIZE = 100
SEARCH_QUERIES = 50
OPEN_TRIALS = 20
CONCURRENT_INGEST_BATCHES = 32

_WORDS = (
    "the quick brown fox jumps over lazy dog conversation memory retrieval index "
    "evidence citation snapshot generation cache history message topic summary "
    "provider synthesis chunk node tree lexical search sqlite redis budget deadline"
).split()

# Runs in a fresh interpreter: import, open, and first search with no warm process state.
_PROCESS_COLD = r"""
import asyncio, json, resource, sys, time
t0 = time.perf_counter()
from cci.search import search
from cci.store import HistoryStore
t1 = time.perf_counter()
async def main():
    start = time.perf_counter()
    store = await HistoryStore.open(sys.argv[1])
    opened = time.perf_counter()
    await search(store, sys.argv[2], limit=8)
    searched = time.perf_counter()
    from cci.io_worker import fetchone
    count = await fetchone(await store.connection.execute("SELECT COUNT(*) FROM messages"))
    await store.aclose()
    print(json.dumps({"import_ms": (t1 - t0) * 1000, "open_ms": (opened - start) * 1000,
                      "first_search_ms": (searched - opened) * 1000,
                      "ru_maxrss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      "observed_messages": count[0]}))
asyncio.run(main())
"""


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


def _summary(values: list[float], **extra: object) -> dict:
    return {"p50": _percentile(values, 50), "p95": _percentile(values, 95), "trials": len(values), **extra}


def _rss_bytes(ru_maxrss: int) -> int:
    return ru_maxrss if sys.platform == "darwin" else ru_maxrss * 1024  # macOS bytes, Linux KiB


def _store_bytes(directory: str) -> int:
    return sum(p.stat().st_size for p in Path(directory).glob("benchmark.db*"))


def _drop_page_cache() -> None:
    if sys.platform != "linux":
        raise RuntimeError("filesystem-cold trials require an exclusively reserved Linux host")
    os.sync()
    command = ["tee", "/proc/sys/vm/drop_caches"]
    if os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    subprocess.run(command, input="3\n", text=True, capture_output=True, check=True)


async def _timed_rounds(calls: list, concurrency: int) -> tuple[list[float], float, int]:
    """Issue `calls` (zero-arg coroutine factories) `concurrency` at a time; per-call latency."""
    latencies: list[float] = []
    errors = 0

    async def one(call) -> None:
        nonlocal errors
        start = time.perf_counter()
        try:
            await call()
        except Exception:
            errors += 1
        latencies.append((time.perf_counter() - start) * 1000)

    started = time.perf_counter()
    for i in range(0, len(calls), concurrency):
        if concurrency == 1:
            await one(calls[i])  # same direct-await timing as the T070/T083 workload
        else:
            await asyncio.gather(*(one(call) for call in calls[i:i + concurrency]))
    return latencies, time.perf_counter() - started, errors


async def _run(
    out_path: str | None,
    label: str,
    queries: int = SEARCH_QUERIES,
    total_messages: int = TOTAL_MESSAGES,
    concurrency: tuple[int, ...] = (1,),
    cold_trials: int = 0,
    artifact: str | None = None,
    filesystem_cold_trials: int = 0,
) -> dict:
    import apsw
    import cci
    from cci.ingest import ingest
    from cci.models import InputMessage
    from cci.search import search
    from cci.store import HistoryStore

    rng = random.Random(SEED)
    message_bytes: list[int] = []

    def batch() -> list[InputMessage]:
        contents = [_synthetic_content(rng) for _ in range(BATCH_SIZE)]
        message_bytes.extend(len(c.encode("utf-8")) for c in contents)
        return [
            InputMessage(role="user" if i % 2 == 0 else "assistant", content=content)
            for i, content in enumerate(contents)
        ]

    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "benchmark.db")

        # SC-008: 100-message batch ingest p95, excluding indexing.
        store = await HistoryStore.open(path)
        cache_backend = store.config.cache_backend
        ingest_latencies_ms: list[float] = []
        sent = 0
        source_id = "bench-src"
        generation_started = time.perf_counter()
        while sent < total_messages:
            messages = batch()
            start = time.perf_counter()
            await ingest(store, store.history_id, messages, source_id, f"batch-{sent // BATCH_SIZE}")
            ingest_latencies_ms.append((time.perf_counter() - start) * 1000)
            sent += BATCH_SIZE
        generation_s = time.perf_counter() - generation_started
        generated_bytes = _store_bytes(d)
        fixture_bytes = list(message_bytes)

        # SC-007: lexical search p95 at top-8, over one fixed query list per concurrency level.
        query_list = [" ".join(rng.sample(_WORDS, 2)) for _ in range(queries)]
        search_by_concurrency: dict[str, dict] = {}
        for level in concurrency:
            calls = [lambda q=q: search(store, q, limit=8) for q in query_list]
            latencies, elapsed, errors = await _timed_rounds(calls, level)
            search_by_concurrency[str(level)] = _summary(
                latencies, errors=errors, throughput_per_s=len(calls) / elapsed
            )
        search_latencies_ms = []
        if "1" not in search_by_concurrency:
            for query in query_list:
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

        cold: list[dict] = []
        for i in range(cold_trials):
            completed = subprocess.run(
                [sys.executable, "-I", "-c", _PROCESS_COLD, path, query_list[i % len(query_list)]],
                cwd=d, capture_output=True, text=True, check=True,
            )
            cold.append(json.loads(completed.stdout))

        filesystem_cold: list[dict] = []
        for i in range(filesystem_cold_trials):
            _drop_page_cache()
            completed = subprocess.run(
                [sys.executable, "-I", "-c", _PROCESS_COLD, path, query_list[i % len(query_list)]],
                cwd=d, capture_output=True, text=True, check=True,
            )
            filesystem_cold.append(json.loads(completed.stdout))

        # Run writers last: warm/reopen/cold searches must all see exactly the declared fixture.
        ingest_by_concurrency: dict[str, dict] = {}
        store = await HistoryStore.open(path)
        for level in (c for c in concurrency if c > 1):
            batches = [batch() for _ in range(CONCURRENT_INGEST_BATCHES)]
            calls = [
                lambda m=m, k=f"concurrent-{level}-{n}": ingest(store, store.history_id, m, source_id, k)
                for n, m in enumerate(batches)
            ]
            latencies, elapsed, errors = await _timed_rounds(calls, level)
            ingest_by_concurrency[str(level)] = _summary(
                latencies, errors=errors, throughput_messages_per_s=len(calls) * BATCH_SIZE / elapsed
            )
        await store.aclose()

    search_c1 = search_by_concurrency.get("1") or _summary(search_latencies_ms)
    report = {
        "fixture": {"total_messages": total_messages, "seed": SEED, "batch_size": BATCH_SIZE},
        "environment": {
            "label": label,
            "cci_module_path": cci.__file__,
            "python_version": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "dedicated_linux_runner": False,
            "artifact": artifact,
            "apsw_version": apsw.apsw_version(),
            "sqlite_library_version": apsw.sqlite_lib_version(),
            "cpu_count": os.cpu_count(),
            "physical_memory_bytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
            "cache_backend": cache_backend,
            "filesystem_cold": "sync + drop_caches=3 before each child" if filesystem_cold else "NOT RUN",
        },
        "fixture_stats": {
            "reference_fixture": total_messages == TOTAL_MESSAGES,
            "message_bytes_avg": sum(fixture_bytes) / len(fixture_bytes),
            "message_bytes_max": max(fixture_bytes),
            "generation_s": generation_s,
            "store_bytes_after_generation": generated_bytes,
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
                "p50": search_c1["p50"],
                "p95": search_c1["p95"],
                "threshold_p95_ms": 100,
                "trials": search_c1["trials"],
            },
        },
        "resources": {"peak_rss_bytes": _rss_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)},
    }
    if concurrency != (1,):
        report["concurrency"] = {
            "search_top8_ms": search_by_concurrency, "ingest_100_ms": ingest_by_concurrency,
        }
    if cold:
        report["process_cold"] = {
            "import_ms": _summary([c["import_ms"] for c in cold]),
            "store_open_ms": _summary([c["open_ms"] for c in cold]),
            "first_search_top8_ms": _summary([c["first_search_ms"] for c in cold]),
            "peak_rss_bytes_max": max(_rss_bytes(c["ru_maxrss"]) for c in cold),
            "total_messages": total_messages,
            "raw_trials": cold,
        }
    if filesystem_cold:
        report["filesystem_cold"] = {
            "import_ms": _summary([c["import_ms"] for c in filesystem_cold]),
            "store_open_ms": _summary([c["open_ms"] for c in filesystem_cold]),
            "first_search_top8_ms": _summary([c["first_search_ms"] for c in filesystem_cold]),
            "total_messages": total_messages,
            "raw_trials": filesystem_cold,
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
    parser.add_argument("--queries", type=int, default=SEARCH_QUERIES, help="PRD §12 fixed set: 1000")
    parser.add_argument("--messages", type=int, default=TOTAL_MESSAGES,
                        help="scale point; not the reference fixture")
    parser.add_argument("--concurrency", default="1", help="comma-separated levels, e.g. 1,4,16")
    parser.add_argument("--cold-trials", type=int, default=0,
                        help="fresh-interpreter open + first search trials")
    parser.add_argument("--artifact", default=None,
                        help="artifact identity, e.g. git revision and wheel sha256")
    parser.add_argument("--filesystem-cold-trials", type=int, default=0,
                        help="Linux only: drop the HOST page cache before each trial; reserved host required")
    args = parser.parse_args()
    report = asyncio.run(_run(
        args.out, args.label, args.queries, args.messages,
        tuple(int(c) for c in args.concurrency.split(",")), args.cold_trials, args.artifact,
        args.filesystem_cold_trials,
    ))
    all_pass = all(r["pass"] for r in report["results"].values())
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
