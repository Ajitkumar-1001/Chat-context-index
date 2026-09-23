"""Failure injection F8 (original expiry) and F9 (slow/corrupt Redis), per
spec/fixtures/failure-injection-harness.md (AT-15, AT-16).

Real disposable Redis deployment (harness boundaries: "A mock can test control flow but cannot
prove Redis expiry, reconnection, or command behavior") — this module starts one Docker
container per test run (`setup_module`/`teardown_module`) and tears it down afterward.

F8 does not rely on real wall-clock sleeps for its t=100/150/161 scenario: Redis's own TTL is
only a storage-hygiene mechanism here — the authoritative expiry check is our own
`value.absolute_expiry > now` comparison against the injected `now`, so a fake logical clock is
sufficient (spec/cache-format.md Refill mechanism).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.cache import CACHE_FORMAT_VERSION, CachedValue, RedisMemoCache, SqliteMemoCache
from cci.index import index
from cci.ingest import ingest
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderResponse
from cci.search import search
from cci.store import HistoryStore

_CONTAINER_NAME = "cci-test-redis-f8f9"
REDIS_URL: str | None = None


def setup_module(module) -> None:
    global REDIS_URL
    subprocess.run(["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True)
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "-p", "0:6379", "--name", _CONTAINER_NAME,
            "redis:7-alpine", "redis-server", "--enable-debug-command", "yes",
        ],
        check=True, capture_output=True,
    )
    port = None
    for _ in range(50):
        result = subprocess.run(
            ["docker", "port", _CONTAINER_NAME, "6379"], capture_output=True, text=True
        )
        if result.stdout.strip():
            port = result.stdout.strip().rsplit(":", 1)[-1]
            break
        time.sleep(0.1)
    if port is None:
        raise RuntimeError("redis container did not report a mapped port in time")
    REDIS_URL = f"redis://localhost:{port}"

    import redis as redis_sync

    for _ in range(50):
        try:
            redis_sync.from_url(REDIS_URL).ping()
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("redis container did not become ready in time")


def teardown_module(module) -> None:
    subprocess.run(["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True)


def _make_cache(tmpdir: str, **overrides) -> RedisMemoCache:
    fallback = SqliteMemoCache(os.path.join(tmpdir, "fallback.sqlite3"))
    kwargs = dict(operation_timeout_ms=200, circuit_breaker_failures=3, circuit_breaker_probe_s=30)
    kwargs.update(overrides)
    return RedisMemoCache(REDIS_URL, fallback, **kwargs)


def test_f8_refill_lifetime_bounded_by_original_expiry():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            cache = _make_cache(d)
            key = "cci:memo:v2:scope123:req456"
            value = CachedValue(
                cache_format_version=CACHE_FORMAT_VERSION,
                scope_digest="scope123", request_digest="req456",
                operation_version=1, schema_version=1,
                created_at=100, absolute_expiry=160,
                payload={"text": "cached answer"},
            )
            await cache.set(key, value)

            # "Miss Redis" at t=150: the entry is deliberately evicted from Redis only, leaving
            # the SQLite fallback copy intact.
            await cache._client.delete(key)

            hit_at_150 = await cache.get(key, now=150)
            assert hit_at_150 is not None
            assert hit_at_150.absolute_expiry == 160

            set_commands = [c for c in cache.commands if c[0] == "SET"]
            assert set_commands, "the fallback hit must have refilled Redis"
            refill_ttl = set_commands[-1][2]
            assert refill_ttl <= 10, f"refill ttl {refill_ttl} exceeds the remaining 10s"

            miss_at_161 = await cache.get(key, now=161)
            assert miss_at_161 is None, "at 161 neither backend supplies an eligible hit"

            await cache.aclose()

    asyncio.run(scenario())


def test_f9_stalled_command_bypassed_within_budget():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            cache = _make_cache(d, operation_timeout_ms=50)
            key = "cci:memo:v2:s:r"
            value = CachedValue(
                cache_format_version=CACHE_FORMAT_VERSION, scope_digest="s", request_digest="r",
                operation_version=1, schema_version=1, created_at=100, absolute_expiry=999_999,
                payload={"text": "fallback answer"},
            )
            await cache._fallback.set(key, value)  # only in the fallback

            import redis.asyncio as redis_asyncio

            stall_client = redis_asyncio.from_url(REDIS_URL)
            stall_task = asyncio.create_task(stall_client.execute_command("DEBUG", "SLEEP", "0.3"))
            await asyncio.sleep(0.05)  # let the stall command start executing server-side

            start = time.monotonic()
            result = await cache.get(key, now=200)
            elapsed = time.monotonic() - start
            assert result is not None and result.payload["text"] == "fallback answer"
            assert elapsed < 0.3, "a stalled command must be bypassed, not waited out"

            await stall_task
            await stall_client.aclose()
            await cache.aclose()

    asyncio.run(scenario())


def test_f9_rejected_authentication_bypassed_to_fallback():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            fallback = SqliteMemoCache(os.path.join(d, "fallback.sqlite3"))
            bad_url = REDIS_URL.replace("redis://", "redis://:wrongpassword@")
            cache = RedisMemoCache(
                bad_url, fallback,
                operation_timeout_ms=200, circuit_breaker_failures=3, circuit_breaker_probe_s=30,
            )
            key = "cci:memo:v2:s:r2"
            value = CachedValue(
                cache_format_version=CACHE_FORMAT_VERSION, scope_digest="s", request_digest="r2",
                operation_version=1, schema_version=1, created_at=100, absolute_expiry=999_999,
                payload={"text": "fallback answer 2"},
            )
            await fallback.set(key, value)

            result = await cache.get(key, now=200)
            assert result is not None and result.payload["text"] == "fallback answer 2"

            await cache.aclose()

    asyncio.run(scenario())


def test_f9_legitimate_miss_does_not_trip_circuit_breaker():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            cache = _make_cache(d)
            key = "cci:memo:v2:s:nonexistent"
            for _ in range(5):
                result = await cache.get(key, now=100)
                assert result is None
            assert cache._consecutive_failures == 0, "a legitimate miss is never a breaker failure"

            await cache.aclose()

    asyncio.run(scenario())


def test_f9_malformed_record_is_bypassed_not_raised():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            cache = _make_cache(d)
            await cache._client.set("cci:memo:v2:s:malformed", "not-json-at-all")
            result = await cache.get("cci:memo:v2:s:malformed", now=100)
            assert result is None, "malformed records are bypassed, never raised"
            await cache.aclose()

    asyncio.run(scenario())


def test_f9_wrong_scope_value_is_rejected_by_memoized_provider():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f9.db")
            store = await HistoryStore.open(
                path,
                config={
                    "cache_backend": "redis",
                    "application_namespace": "ns-f9",
                    "redis_url": REDIS_URL,
                },
            )
            # This module shares one Redis container across tests — flush it so a leftover key
            # from an earlier test can't be swept up by this test's broad "cci:memo:*" scan.
            await store.cache._client.flushdb()
            await ingest(
                store, store.history_id,
                [InputMessage(role="user", content="wrong scope test message")],
                "src-1", "key-1",
            )
            fake = FakeProvider(
                responses=[
                    ProviderResponse(text='{"title": "T1", "summary": "S1"}'),
                    ProviderResponse(text='{"title": "T2", "summary": "S2"}'),
                ]
            )
            provider = MemoizedProvider(inner=fake, config=store.config, cache=store.cache)

            await index(store, provider=provider, rebuild=True)
            assert fake.call_count == 1

            # Corrupt the just-written Redis entry's embedded scope_digest directly — simulates
            # a wrong-scope record surfacing at the expected key.
            cursor = 0
            while True:
                cursor, keys = await store.cache._client.scan(cursor=cursor, match="cci:memo:*")
                for k in keys:
                    raw = await store.cache._client.get(k)
                    record = json.loads(raw)
                    record["scope_digest"] = "corrupted-scope"
                    await store.cache._client.set(k, json.dumps(record))
                if cursor == 0:
                    break

            await index(store, provider=provider, rebuild=True)
            assert fake.call_count == 2, (
                "a wrong-scope record must be rejected, forcing a fresh computation"
            )

            # History is unchanged throughout.
            result = await search(store, "wrong")
            assert len(result.candidates) == 1

            await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    setup_module(None)
    try:
        test_f8_refill_lifetime_bounded_by_original_expiry()
        test_f9_stalled_command_bypassed_within_budget()
        test_f9_rejected_authentication_bypassed_to_fallback()
        test_f9_legitimate_miss_does_not_trip_circuit_breaker()
        test_f9_malformed_record_is_bypassed_not_raised()
        test_f9_wrong_scope_value_is_rejected_by_memoized_provider()
        print("F8/F9 failure-injection checks passed")
    finally:
        teardown_module(None)
