# Quick Start: With Redis

Redis mode adds a shared memo cache across processes, with an automatic SQLite fallback and a
circuit breaker — history is never stored in Redis, and Redis failures never affect correctness.

Redis support is **Python-only** for this release. The TypeScript package has no memoized
provider backend yet; see [runtime support](supported-runtimes-and-storage.md).

## Python

```bash
python -m pip install '/path/to/chat_context_index-0.1.0-py3-none-any.whl[redis]'
```

```python
from cci.store import HistoryStore
from cci.index import index
from cci.provider import MemoizedProvider
from cci.stats import stats

async def index_with_redis(my_llm_adapter):
    async with await HistoryStore.open(
        "history.db",
        config={
            "cache_backend": "redis",
            "application_namespace": "my-app",   # required in redis mode
            "redis_url": "redis://localhost:6379",
            "redis_fallback": "sqlite",          # default: falls back to local SQLite
        },
    ) as store:
        provider = MemoizedProvider(my_llm_adapter, store.config, cache=store.cache,
                                    usage_log=store.usage_log)
        report = await index(store, provider=provider)  # eligible summary calls are memoized
        counters = await stats(store)
        return report, counters
```

Pass your application-owned provider adapter to `index_with_redis` after ingesting messages.
`counters.redis_errors` counts failed Redis commands. `sqlite_fallback_lookups` counts reads
that reached the local fallback, and `sqlite_fallback_hits` counts those that found a live
entry. A Redis miss can reach the fallback without being an error. These process-lifetime
counters reset when the store reopens and contain no message text or credentials.

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
