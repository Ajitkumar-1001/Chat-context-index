"""PRD §16.3 cache experiments: `none`, SQLite, and Redis memo on the same effective requests.

A deterministic fake provider answers instantly, so the latencies here are local cache overhead,
not provider-time savings. Real-provider effects remain NOT RUN in this script. The workload uses
dev-split history `dev-h01` (never the held-out split): 80% ingested and indexed, then cold,
warm-identical, changed-request, identical rebuild, append-and-reindex, and (Redis only) outage
phases of `index()` and `ask(mode="tree")`.

Checks (exit 1 on failure):
  - warm identical: zero new indexing/navigation calls with a memo; synthesis is still called
  - every backend returns the same answers as `none` for the same phase
  - Redis outage: same answers as `none`; added wait stays within the configured cache budget

Usage: python cache_experiments.py [--out report.json] [--artifact ...]   (Redis needs Docker)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "evaluations" / "held_out" / "fixture.json"
HISTORY = "dev-h01"
CONTAINER = "cci-bench-cache-experiments"
_IDS = re.compile(r"<<<CCI_EVIDENCE id=(\S+)")


class CountingFake:
    """Fixed summaries, keeps every offered node, cites the first evidence block."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.input_tokens = 0

    async def complete(self, request):
        from cci.provider import ProviderResponse

        self.calls[request.operation] += 1
        ids = _IDS.findall(request.evidence_context)
        if request.operation == "indexing":
            value = {"title": "Development conversation", "summary": "Planning decisions and maintenance."}
        elif request.operation == "tree_navigation":
            value = {"node_ids": ids}
        else:
            value = {"answer": "fake answer" if ids else None, "citations": ids[:1]}
        tokens = (len(request.prompt) + len(request.evidence_context)) // 4  # fake estimate
        self.input_tokens += tokens
        return ProviderResponse(json.dumps(value), input_tokens=tokens, output_tokens=10)


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))] if ordered else 0.0


def _start_redis() -> str:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--rm", "-p", "0:6379", "--name", CONTAINER, "redis:7-alpine"],
        check=True,
        capture_output=True,
    )
    import redis

    for _ in range(100):
        port = subprocess.run(["docker", "port", CONTAINER, "6379"], capture_output=True, text=True).stdout
        if port.strip():
            url = f"redis://localhost:{port.strip().splitlines()[0].rsplit(':', 1)[-1]}"
            try:
                redis.from_url(url).ping()
                return url
            except redis.ConnectionError:
                pass
        time.sleep(0.1)
    raise RuntimeError("redis container did not become ready")


async def _run_backend(backend: str, redis_url: str | None, directory: str) -> dict:
    from cci.ask import ask
    from cci.config import Config
    from cci.index import index
    from cci.ingest import ingest
    from cci.models import InputMessage
    from cci.provider import MemoizedProvider
    from cci.stats import stats
    from cci.store import HistoryStore

    fixture = json.loads(FIXTURE.read_text())
    messages = next(h for h in fixture["histories"] if h["history_label"] == HISTORY)["messages"]
    queries = [q["query_text"] for q in fixture["queries"] if q["history_label"] == HISTORY]
    changed = [f"{q} Answer in one sentence." for q in queries]
    split = int(len(messages) * 0.8)
    path = str(Path(directory) / f"{backend}.db")
    config = Config(cache_backend=backend, application_namespace=f"bench-{backend}", redis_url=redis_url)
    phases: dict[str, dict] = {}

    async with await HistoryStore.open(path, config=config) as store:
        fake = CountingFake()
        provider = MemoizedProvider(
            inner=fake, config=store.config, cache=store.cache, usage_log=store.usage_log
        )

        async def add(rows: list[dict], offset: int) -> None:
            for n, raw in enumerate(rows, offset):
                message = InputMessage(
                    role=raw["role"],
                    content=raw["content"],
                    tool_calls=raw.get("tool_calls"),
                    tool_call_id=raw.get("tool_call_id"),
                )
                await ingest(store, store.history_id, [message], raw["source_id"], f"m{n}")

        async def build(rebuild: bool = False) -> list[str]:
            for _ in range(64):
                if (await index(store, provider, rebuild=rebuild)).status == "complete":
                    return ["complete"]
                rebuild = False
            raise RuntimeError("index did not complete")

        async def asks(texts: list[str]) -> tuple[list[str], list[float]]:
            outputs, latencies = [], []
            for text in texts:
                start = time.perf_counter()
                result = await ask(store, text, provider=provider, mode="tree")
                latencies.append((time.perf_counter() - start) * 1000)
                # The fake's answer text is constant; the selected evidence is what must match.
                outputs.append(
                    json.dumps([result.status, [e.seq for e in result.evidence], len(result.citations)])
                )
            return outputs, latencies

        async def phase(name: str, action) -> None:
            calls, tokens, before = Counter(fake.calls), fake.input_tokens, await stats(store)
            start = time.perf_counter()
            outputs, latencies = await action()
            wall_ms = (time.perf_counter() - start) * 1000
            after = await stats(store)
            phases[name] = {
                "provider_calls": {
                    op: fake.calls[op] - calls[op] for op in ("indexing", "tree_navigation", "synthesis")
                },
                "current_input_tokens_fake_estimate": fake.input_tokens - tokens,
                "memo_hits": after.memo_hits - before.memo_hits,
                "memo_misses": after.memo_misses - before.memo_misses,
                "memo_errors": after.memo_errors - before.memo_errors,
                "reused_operation_usage": after.reused_operation_usage_count
                - before.reused_operation_usage_count,
                "provider_errors": after.provider_errors - before.provider_errors,
                "wall_ms": wall_ms,
                "ask_ms": {
                    "p50": _percentile(latencies, 50),
                    "p95": _percentile(latencies, 95),
                    "max": max(latencies, default=0.0),
                    "trials": len(latencies),
                },
                "outputs": outputs,
            }

        await add(messages[:split], 0)
        await phase("cold_index", lambda: _timed_only(build()))
        await phase("cold_ask", lambda: asks(queries))
        await phase("warm_ask", lambda: asks(queries))
        await phase("changed_ask", lambda: asks(changed))
        await phase("warm_index_rebuild", lambda: _timed_only(build(rebuild=True)))
        # A rebuild publishes new node ids, so navigation requests after it are new requests.
        await phase("post_rebuild_ask", lambda: asks(queries))
        await add(messages[split:], split)
        await phase("append_index", lambda: _timed_only(build()))
        await phase("append_ask", lambda: asks(queries))

        storage: dict[str, int] = {
            "memo_sqlite_bytes": sum(p.stat().st_size for p in Path(directory).glob(f"{backend}.db.memo*")),
        }
        if backend == "redis":
            import redis

            client = redis.from_url(redis_url)
            keys = list(client.scan_iter(count=1000))
            storage.update(
                redis_keys=len(keys), redis_key_bytes=sum(client.memory_usage(k) or 0 for k in keys)
            )
            subprocess.run(["docker", "pause", CONTAINER], check=True, capture_output=True)
            try:
                await phase("outage_ask", lambda: asks(queries))
            finally:
                subprocess.run(["docker", "unpause", CONTAINER], capture_output=True)
        return {
            "phases": phases,
            "storage": storage,
            "config": {
                "cache_operation_timeout_ms": store.config.cache_operation_timeout_ms,
                "cache_overhead_budget_ms": store.config.cache_overhead_budget_ms,
                "redis_fallback": store.config.redis_fallback,
            },
        }


async def _timed_only(awaitable) -> tuple[list[str], list[float]]:
    return await awaitable, []


def _checks(results: dict) -> list[dict]:
    checks = []
    none = results["none"]["phases"]
    for backend in ("sqlite", "redis"):
        phases = results[backend]["phases"]
        warm = [
            phases["warm_index_rebuild"]["provider_calls"]["indexing"],
            phases["warm_ask"]["provider_calls"]["tree_navigation"],
        ]
        checks.append(
            {
                "check": f"{backend}: warm identical makes zero indexing/navigation calls",
                "observed": warm,
                "pass": warm == [0, 0],
            }
        )
        synth = phases["warm_ask"]["provider_calls"]["synthesis"]
        checks.append(
            {
                "check": f"{backend}: warm ask still calls synthesis once per question",
                "observed": synth,
                "pass": synth == phases["warm_ask"]["ask_ms"]["trials"],
            }
        )
        same = all(phases[p]["outputs"] == none[p]["outputs"] for p in none)
        checks.append({"check": f"{backend}: answers identical to none in every phase", "pass": same})
    outage, config = results["redis"]["phases"]["outage_ask"], results["redis"]["config"]
    checks.append(
        {
            "check": "redis outage: answers identical to none after append",
            "pass": outage["outputs"] == none["append_ask"]["outputs"],
        }
    )
    per_ask = math.ceil(none["append_ask"]["provider_calls"]["tree_navigation"] / outage["ask_ms"]["trials"])
    bound = none["append_ask"]["ask_ms"]["max"] + per_ask * 2 * config["cache_overhead_budget_ms"]
    checks.append(
        {
            "check": "redis outage: slowest ask <= none slowest + get/set budget per cacheable call",
            "observed_ms": outage["ask_ms"]["max"],
            "bound_ms": bound,
            "pass": outage["ask_ms"]["max"] <= bound,
        }
    )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--artifact", default=None, help="artifact identity, e.g. git revision and wheel sha256"
    )
    args = parser.parse_args()

    import apsw
    import cci
    import redis

    redis_url = _start_redis()
    try:
        server = redis.from_url(redis_url).info("server")["redis_version"]
        with tempfile.TemporaryDirectory() as d:
            results = {
                b: asyncio.run(_run_backend(b, redis_url if b == "redis" else None, d))
                for b in ("none", "sqlite", "redis")
            }
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)

    checks = _checks(results)
    for result in results.values():
        for phase in result["phases"].values():
            phase["outputs_distinct"] = len(set(phase.pop("outputs")))
    report = {
        "workload": {
            "history": HISTORY,
            "split": "dev",
            "initial_fraction": 0.8,
            "mode": "tree",
            "provider": "deterministic fake, zero latency; tokens are a characters/4 estimate",
        },
        "environment": {
            "artifact": args.artifact,
            "cci_module_path": cci.__file__,
            "python_version": sys.version,
            "platform": platform.platform(),
            "sqlite_library_version": apsw.sqlite_lib_version(),
            "redis_server": server,
            "redis_py": redis.__version__,
            "dedicated_linux_runner": False,
        },
        "not_run": [
            "real-provider latency, usage, and savings (§16.3 'real providers' half)",
            "Redis error/fallback rate: RedisMemoCache handles Redis failures internally; stats() omits it",
        ],
        "results": results,
        "checks": checks,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0 if all(c["pass"] for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
