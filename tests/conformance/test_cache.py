"""quickstart.md Scenario 6 — Cache modes, warm and cold (User Story 5, M3; AT-15, AT-16).

1. Configure cache: sqlite. Run an eligible operation twice -> second run makes zero new
   provider calls.
2. Change the operation's effective model input -> misses (different request_digest).
3. Force the cache backend unreachable -> completes correctly within the cache time budget,
   degraded performance, unaffected correctness.

`index()` is the only currently-implemented cacheable operation (`indexing` is in
`_CACHEABLE_OPERATIONS`; `ask()`'s `synthesis` never is, INV-06) — it is the exerciser for all
three steps here.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.cache import CachedValue, NoneCache
from cci.index import index
from cci.ingest import ingest
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderResponse
from cci.stats import stats
from cci.store import HistoryStore


async def _seeded_store(path: str, **config) -> HistoryStore:
    store = await HistoryStore.open(path, config=config or None)
    await ingest(
        store, store.history_id,
        [InputMessage(role="user", content="what is rayleigh scattering")],
        "src-1", "key-1",
    )
    return store


def test_warm_sqlite_cache_makes_zero_new_provider_calls_on_second_run():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "cache1.db"), cache_backend="sqlite")
            assert os.path.exists(os.path.join(d, "cache1.db.memo.sqlite3")) is False, (
                "the memo file is created lazily on first cache write, not at open()"
            )

            fake = FakeProvider(
                responses=[ProviderResponse(text='{"title": "T", "summary": "S"}')]
            )
            provider = MemoizedProvider(inner=fake, config=store.config, cache=store.cache)

            first = await index(store, provider=provider, rebuild=True)
            assert fake.call_count == 1
            assert first.provider_usage.memo_misses == 1

            second = await index(store, provider=provider, rebuild=True)
            assert fake.call_count == 1, "warm cache: zero new provider calls"
            assert second.provider_usage.memo_hits == 1
            assert second.provider_usage.current_provider_calls == 0

            await store.aclose()

    asyncio.run(scenario())


def test_changed_effective_input_is_a_structural_miss():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "cache2.db"), cache_backend="sqlite")
            fake = FakeProvider(
                responses=[
                    ProviderResponse(text='{"title": "T1", "summary": "S1"}'),
                    ProviderResponse(text='{"title": "T2", "summary": "S2"}'),
                ]
            )
            provider = MemoizedProvider(inner=fake, config=store.config, cache=store.cache)

            await index(store, provider=provider, rebuild=True)
            assert fake.call_count == 1

            # A different effective input (new message appended, changing the chunk content and
            # therefore the request_digest) must miss, never reuse the prior entry.
            await ingest(
                store, store.history_id,
                [InputMessage(role="user", content="a different follow-up question entirely")],
                "src-1", "key-2",
            )
            report = await index(store, provider=provider, rebuild=True)
            assert fake.call_count == 2, "a changed effective input is a structural miss"
            assert report.provider_usage.memo_misses == 1

            await store.aclose()

    asyncio.run(scenario())


def test_unreachable_cache_backend_completes_correctly_within_budget():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "cache3.db"), cache_backend="sqlite")
            fake = FakeProvider(
                responses=[ProviderResponse(text='{"title": "T", "summary": "S"}')]
            )

            class _BrokenCache:
                async def get(self, key: str, *, now: float):
                    raise ConnectionError("cache backend unreachable")

                async def set(self, key: str, value) -> None:
                    raise ConnectionError("cache backend unreachable")

                async def aclose(self) -> None:
                    return None

            provider = MemoizedProvider(inner=fake, config=store.config, cache=_BrokenCache())

            report = await index(store, provider=provider, rebuild=True)
            assert report.status == "complete", (
                "an unreachable cache degrades performance, never correctness"
            )
            assert fake.call_count == 1
            assert report.committed_coverage.start_seq == 1

            await store.aclose()

    asyncio.run(scenario())


def test_none_cache_backend_never_hits():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "cache4.db"), cache_backend="none")
            fake = FakeProvider(
                responses=[
                    ProviderResponse(text='{"title": "T1", "summary": "S1"}'),
                    ProviderResponse(text='{"title": "T2", "summary": "S2"}'),
                ]
            )
            provider = MemoizedProvider(inner=fake, config=store.config, cache=store.cache)
            assert isinstance(store.cache, NoneCache)

            await index(store, provider=provider, rebuild=True)
            await index(store, provider=provider, rebuild=True)
            assert fake.call_count == 2, "cache_backend='none' never caches"

            await store.aclose()

    asyncio.run(scenario())


def test_redis_errors_and_sqlite_fallback_are_counted_without_content():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await HistoryStore.open(os.path.join(d, "cache5.db"), config={
                "cache_backend": "redis", "application_namespace": "cache-metrics",
                "redis_url": "redis://127.0.0.1:6379", "redis_circuit_breaker_failures": 1,
            })
            try:
                value = CachedValue(
                    cache_format_version=2, scope_digest="scope", request_digest="request",
                    operation_version=1, schema_version=1, created_at=90,
                    absolute_expiry=200, payload={"text": "private payload"},
                )
                await store.cache._fallback.set("key", value)
                await store.cache._client.aclose()

                class _BrokenRedis:
                    async def get(self, key: str):
                        raise ConnectionError("private connection detail")

                    async def aclose(self) -> None:
                        return None

                store.cache._client = _BrokenRedis()
                assert await store.cache.get("key", now=100) == value
                assert await store.cache.get("key", now=101) == value
                result = await stats(store)
                assert (result.redis_errors, result.sqlite_fallback_lookups,
                        result.sqlite_fallback_hits) == (1, 2, 2)
                assert "private payload" not in repr(result)
                assert "private connection detail" not in repr(result)
            finally:
                await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_warm_sqlite_cache_makes_zero_new_provider_calls_on_second_run()
    test_changed_effective_input_is_a_structural_miss()
    test_unreachable_cache_backend_completes_correctly_within_budget()
    test_none_cache_backend_never_hits()
    print("Scenario 6 (cache modes) checks passed")
