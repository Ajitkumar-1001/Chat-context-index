"""MemoCache: disabled/local-sqlite/Redis-with-sqlite-fallback (FR-008; spec/cache-format.md).

Swappable without touching application code — `HistoryStore.open()` selects the backend from
`config.cache_backend`; callers never construct a backend directly (User Story 5's goal).

`redis` is imported lazily, inside `RedisMemoCache.__init__` only — importing this module (or
`cci`'s main entry point) at cache_backend='none'/'sqlite' MUST NOT load the Redis client
(PRD §13.2, CHK025).

Clock is injectable throughout (`now` passed explicitly to `get()`) — F8's t=100/150/161 expiry
scenario needs a controlled clock, never sleep-based timing
(spec/fixtures/failure-injection-harness.md).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

CACHE_FORMAT_VERSION = 2  # matches the "v2" prefix in cache_key.py (ADR-004)


@dataclass(frozen=True)
class CacheScope:
    application_namespace: str | None
    store_instance_id: str
    history_id: str
    cache_generation: int


@dataclass(frozen=True)
class CachedValue:
    """spec/cache-format.md Cached value. `scope_digest`/`request_digest` are stored alongside
    the payload so a reader can independently verify a fetched entry actually belongs to the key
    it was fetched under (F9: a corrupted/wrong-scope record is rejected, not trusted)."""

    cache_format_version: int
    scope_digest: str
    request_digest: str
    operation_version: int
    schema_version: int
    created_at: float
    absolute_expiry: float
    payload: dict[str, Any]
    reused_operation_usage: dict[str, Any] | None = None


class MemoCache(Protocol):
    async def get(self, key: str, *, now: float) -> CachedValue | None: ...
    async def set(self, key: str, value: CachedValue) -> None: ...
    async def clear(self) -> None: ...
    async def aclose(self) -> None: ...


def _to_json(value: CachedValue) -> str:
    return json.dumps(
        {
            "cache_format_version": value.cache_format_version,
            "scope_digest": value.scope_digest,
            "request_digest": value.request_digest,
            "operation_version": value.operation_version,
            "schema_version": value.schema_version,
            "created_at": value.created_at,
            "absolute_expiry": value.absolute_expiry,
            "payload": value.payload,
            "reused_operation_usage": value.reused_operation_usage,
        }
    )


def _from_json(raw: str | bytes) -> CachedValue | None:
    """Malformed/corrupted records are bypassed, never raised (F9) — returns None."""
    try:
        d = json.loads(raw)
        return CachedValue(
            cache_format_version=d["cache_format_version"],
            scope_digest=d["scope_digest"],
            request_digest=d["request_digest"],
            operation_version=d["operation_version"],
            schema_version=d["schema_version"],
            created_at=d["created_at"],
            absolute_expiry=d["absolute_expiry"],
            payload=d["payload"],
            reused_operation_usage=d.get("reused_operation_usage"),
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


class NoneCache:
    """cache_backend='none': always a miss; writes are no-ops."""

    async def get(self, key: str, *, now: float) -> CachedValue | None:
        return None

    async def set(self, key: str, value: CachedValue) -> None:
        return None

    async def clear(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class SqliteMemoCache:
    """cache_backend='sqlite' (also the fallback backend under 'redis' mode). A disposable local
    file, physically separate from the authoritative history store (constitution Principle III).

    ponytail: opens a fresh stdlib `sqlite3` connection per operation rather than holding one
    open — this is a small disposable KV cache, not the durable/FTS5 history store, so there's
    no worker-thread/WAL machinery to justify here. Add connection reuse if profiling ever shows
    this file's open/close overhead matters.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.execute("CREATE TABLE IF NOT EXISTS memo (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
        conn.commit()
        return conn

    async def get(self, key: str, *, now: float) -> CachedValue | None:
        async with self._lock:
            def _read() -> str | None:
                conn = self._open()
                try:
                    row = conn.execute("SELECT value_json FROM memo WHERE key = ?", (key,)).fetchone()
                    return row[0] if row else None
                finally:
                    conn.close()

            raw = await asyncio.to_thread(_read)
        if raw is None:
            return None
        value = _from_json(raw)
        if value is None or value.absolute_expiry <= now:
            return None
        return value

    async def set(self, key: str, value: CachedValue) -> None:
        async with self._lock:
            def _write() -> None:
                conn = self._open()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO memo (key, value_json) VALUES (?, ?)",
                        (key, _to_json(value)),
                    )
                    conn.commit()
                finally:
                    conn.close()

            await asyncio.to_thread(_write)

    async def clear(self) -> None:
        """`clear_history()`'s local cache deletion step — this file only ever serves one
        history, so a full wipe is simple and correct (no generation-scoped partial deletion
        needed)."""
        async with self._lock:
            def _wipe() -> None:
                conn = self._open()
                try:
                    conn.execute("DELETE FROM memo")
                    conn.commit()
                finally:
                    conn.close()

            await asyncio.to_thread(_wipe)

    async def aclose(self) -> None:
        return None


class RedisMemoCache:
    """cache_backend='redis': Redis primary, SQLite fallback (`redis_fallback`, default
    'sqlite'). Refill mechanism (spec/cache-format.md): a SQLite-fallback hit MAY refill Redis,
    but only for the *remaining* time until the entry's original `absolute_expiry` — never a
    fresh full-lifetime write.

    Circuit breaker (Config defaults): opens after `redis_circuit_breaker_failures` consecutive
    operational failures; while open, Redis is skipped entirely (fallback-only) until
    `redis_circuit_breaker_probe_s` has elapsed, then one probe attempt is allowed. A legitimate
    miss (key absent) never counts as a failure.

    `self.commands` records every Redis command issued — an inspection seam for tests to verify
    purge/refill scoping (Failure-Injection.md F10/F-writer: "inspect purge commands as well as
    resulting keys").
    """

    def __init__(
        self,
        redis_url: str,
        fallback: MemoCache,
        *,
        operation_timeout_ms: int,
        circuit_breaker_failures: int,
        circuit_breaker_probe_s: float,
        now: Callable[[], float] = time.time,
    ) -> None:
        import redis.asyncio as redis_asyncio  # lazy import (PRD §13.2, CHK025)

        self._client = redis_asyncio.from_url(
            redis_url,
            socket_timeout=operation_timeout_ms / 1000,
            socket_connect_timeout=operation_timeout_ms / 1000,
        )
        self._fallback = fallback
        self._breaker_failures = circuit_breaker_failures
        self._breaker_probe_s = circuit_breaker_probe_s
        self._now = now
        self._consecutive_failures = 0
        self._circuit_open_until: float | None = None
        self.redis_errors = 0
        self.sqlite_fallback_lookups = 0
        self.sqlite_fallback_hits = 0
        self.commands: list[tuple[Any, ...]] = []

    def _record(self, *parts: Any) -> None:
        self.commands.append(parts)

    def _available(self) -> bool:
        if self._circuit_open_until is None:
            return True
        return self._now() >= self._circuit_open_until

    def _note_failure(self) -> None:
        self.redis_errors += 1
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._breaker_failures:
            self._circuit_open_until = self._now() + self._breaker_probe_s

    def _note_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = None

    async def get(self, key: str, *, now: float) -> CachedValue | None:
        if self._available():
            try:
                self._record("GET", key)
                raw = await self._client.get(key)
                self._note_success()
                if raw is not None:
                    value = _from_json(raw)
                    if value is not None and value.absolute_expiry > now:
                        return value
                    return None
            except Exception:
                self._note_failure()

        # Redis unavailable, circuit open, or miss: fall through to the SQLite fallback.
        sqlite_fallback = isinstance(self._fallback, SqliteMemoCache)
        if sqlite_fallback:
            self.sqlite_fallback_lookups += 1
        value = await self._fallback.get(key, now=now)
        if value is None:
            return None
        if sqlite_fallback:
            self.sqlite_fallback_hits += 1

        remaining = value.absolute_expiry - now
        if remaining > 0 and self._available():
            try:
                self._record("SET", key, int(remaining))
                await self._client.set(key, _to_json(value), ex=max(1, int(remaining)))
                self._note_success()
            except Exception:
                self._note_failure()
        return value

    async def set(self, key: str, value: CachedValue) -> None:
        await self._fallback.set(key, value)
        if self._available():
            ttl = max(1, int(value.absolute_expiry - value.created_at))
            try:
                self._record("SET", key, ttl)
                await self._client.set(key, _to_json(value), ex=ttl)
                self._note_success()
            except Exception:
                self._note_failure()

    async def clear(self) -> None:
        """`clear_history()`'s local cache deletion step — wipes the local SQLite fallback only.
        The scoped external (Redis) purge is a separate step (`purge_prefix`), since only the
        caller knows the retired generation's scope prefix to purge."""
        await self._fallback.clear()

    async def purge_prefix(self, prefix: str, *, batch_size: int = 200) -> None:
        """Bounded cursor iteration + bounded deletion batches over the owned prefix — never
        `KEYS *`, `FLUSHDB`, or `FLUSHALL` (spec/cache-format.md Purge scoping)."""
        cursor = 0
        try:
            while True:
                self._record("SCAN", cursor, f"{prefix}*")
                cursor, keys = await self._client.scan(cursor=cursor, match=f"{prefix}*", count=batch_size)
                if keys:
                    self._record("DEL", *keys)
                    await self._client.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            self._note_failure()
            raise

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._fallback.aclose()


def build_cache(config: Any, store_path: str) -> MemoCache:
    """Selects a backend from `config.cache_backend` — the FR-008 "swap without touching
    application code" mechanism. `store_path`-derived sibling file keeps the SQLite memo file
    physically separate from the authoritative history file at the same path
    (constitution Principle III)."""
    if config.cache_backend == "none":
        return NoneCache()

    sqlite_cache = SqliteMemoCache(f"{store_path}.memo.sqlite3")
    if config.cache_backend == "sqlite":
        return sqlite_cache

    fallback: MemoCache = sqlite_cache if config.redis_fallback == "sqlite" else NoneCache()
    return RedisMemoCache(
        config.redis_url,
        fallback,
        operation_timeout_ms=config.cache_operation_timeout_ms,
        circuit_breaker_failures=config.redis_circuit_breaker_failures,
        circuit_breaker_probe_s=config.redis_circuit_breaker_probe_s,
    )
