# Quick Start: With Redis

Redis mode adds a shared memo cache across processes, with an automatic SQLite fallback and a
circuit breaker — history is never stored in Redis, and Redis failures never affect correctness
(constitution Principle III).

Redis support is **Python-only** for this release (FR-008 is scoped to the Python
implementation; TypeScript parity covers the cache-key formula for cross-language conformance,
not the Redis backend itself — see `supported-runtimes-and-storage.md`).

## Python

```bash
pip install "chat-context-index[redis]"
```

```python
import asyncio
from cci.store import HistoryStore
from cci.index import index
from cci.provider import MemoizedProvider

async def main():
    store = await HistoryStore.open(
        "history.db",
        config={
            "cache_backend": "redis",
            "application_namespace": "my-app",   # required in redis mode
            "redis_url": "redis://localhost:6379",
            "redis_fallback": "sqlite",          # default: falls back to local SQLite
        },
    )
    provider = MemoizedProvider(inner=my_llm_adapter, config=store.config, cache=store.cache)
    report = await index(store, provider=provider)  # eligible calls (classification/summary)
    print(report)                                    # are memoized across processes
    await store.aclose()

asyncio.run(main())
```

## Configuration defaults

| Setting | Default |
|---|---|
| Redis fallback | `sqlite` |
| Memo lifetime | 86,400 s |
| Cache operation timeout | 100 ms |
| Total cache overhead budget per memoized op | 250 ms |
| Circuit breaker | opens after 3 consecutive failures, probes again after 30 s |

## What happens when Redis is unreachable

Every cache lookup/write is bounded by the 250 ms total overhead budget; on failure or timeout,
the operation falls through to the local SQLite fallback (or a fresh computation) — correctness
is unaffected, only latency degrades. `clear_history()` reports `cache_purge_pending: true`
(instead of failing) when the scoped Redis purge cannot complete, and records the retired scope
for later bounded cleanup — it never claims a completed purge it didn't perform, and it never
runs an unscoped `FLUSHDB`/`FLUSHALL`/`KEYS *`.
