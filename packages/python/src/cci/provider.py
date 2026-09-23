"""Model provider abstraction (plan.md Summary: `MemoizedProvider` — admission, timeout, retry,
memoization — strictly outside any database write transaction; PRD §6.2).

Memoization (T057-T059, US5/M3): only classification/summary/routing operations are eligible
(spec/cache-format.md Eligibility) — `_CACHEABLE_OPERATIONS` below. `synthesis` (ask()'s final
answer) is never cached (INV-06). A cache lookup/write is bounded by
`config.cache_overhead_budget_ms` total and never allowed to fail the underlying operation — a
cache error degrades to a fresh computation, never an error surfaced to the caller (constitution
Principle III).

`FakeProvider` is the "deterministic provider double" spec/fixtures/failure-injection-harness.md
requires for the M2 conformance suite — never a mock of the storage/FTS layer, only of the
network-facing model call.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .cache import CachedValue, CACHE_FORMAT_VERSION, CacheScope, MemoCache
from .cache_key import cache_key, request_digest, scope_digest
from .config import Config
from .errors import BudgetExceeded, ProviderTimeout
from .retry import async_retry

_CACHEABLE_OPERATIONS = frozenset({"indexing", "tree_navigation"})
_OPERATION_VERSION = 1
_SCHEMA_VERSION = 1


@dataclass
class UsageLog:
    """In-memory, process-lifetime-only counters (contracts/operations.md `stats()`: "counts/
    timings/outcomes"; AT-20: "collect default events during hits, retries, and failures").
    Never stores prompts, message bodies, or credentials — counters only (FR-010). Not
    persisted across `close()`/reopen — `stats()` is a live diagnostic, not a durable audit log
    (no requirement or test in this task range asks for the latter)."""

    provider_calls: int = 0
    provider_retries: int = 0
    provider_errors: int = 0
    memo_hits: int = 0
    memo_misses: int = 0
    memo_errors: int = 0
    reused_operation_usage_count: int = 0
    usage_unknown_count: int = 0


@dataclass(frozen=True)
class ProviderRequest:
    operation: str  # "synthesis" | "indexing" | "tree_navigation"
    prompt: str
    evidence_context: str  # already rendered by context_assembly.render_evidence_context


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_unknown: bool = False
    from_cache: bool = False
    reused_operation_usage: dict | None = None


@dataclass
class CallBudget:
    """Per-request physical attempt limit, shared across navigation/indexing steps."""

    limit: int
    calls: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_unknown: bool = False
    memo_hits: int = 0
    memo_misses: int = 0

    def admit(self, retry: bool) -> None:
        if self.calls >= self.limit:
            raise BudgetExceeded("request provider-attempt budget exhausted")
        self.calls += 1
        self.retries += int(retry)

    def record(self, response: ProviderResponse) -> None:
        if response.from_cache:
            self.memo_hits += 1
        elif response.input_tokens is None or response.output_tokens is None or response.usage_unknown:
            self.usage_unknown = True
        else:
            self.input_tokens += response.input_tokens
            self.output_tokens += response.output_tokens


class Provider(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...


class FakeProvider:
    """Scriptable deterministic double: succeeds, fails N times then succeeds, or hangs past
    any caller-enforced deadline. Never touches storage/FTS — a model-call double only."""

    def __init__(
        self,
        responses: list[ProviderResponse] | None = None,
        fail_times: int = 0,
        fail_error: Exception | None = None,
        hang: bool = False,
        entered: asyncio.Event | None = None,
        resume: asyncio.Event | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._fail_times = fail_times
        self._fail_error = fail_error
        self._hang = hang
        self.call_count = 0
        # Set right before hanging/pausing — lets a test await deterministic entry into the
        # call instead of racing it with a sleep (spec/fixtures/failure-injection-harness.md:
        # "never a timing race based only on sleep").
        self.entered = entered or asyncio.Event()
        # When set, `complete()` waits on it before returning — a controlled pause point for
        # tests that need to run something concurrently while a call is in flight, then let it
        # resume (as opposed to `hang`, which never returns).
        self._resume = resume

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.call_count += 1
        if self._resume is not None:
            self.entered.set()
            await self._resume.wait()
        if self._hang:
            self.entered.set()
            await asyncio.sleep(3600)
        if self.call_count <= self._fail_times:
            raise self._fail_error if self._fail_error is not None else ProviderTimeout(
                "simulated provider failure"
            )
        if self._responses:
            return self._responses.pop(0)
        return ProviderResponse(text="")


@dataclass
class MemoizedProvider:
    """Admission (bounded per-provider concurrency) + per-call deadline + retry + memoization,
    wrapping one `Provider`. Strictly called outside any database write transaction
    (constitution Principle V). `cache=None` (the default) preserves the pre-T057 passthrough
    behavior exactly — existing callers that never pass `cache_scope` are unaffected."""

    inner: Provider
    config: Config
    cache: MemoCache | None = None
    usage_log: UsageLog | None = None
    _semaphore: asyncio.Semaphore = field(init=False)
    _now: Callable[[], float] = field(default=time.time, repr=False)

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.config.per_provider_concurrency)

    def _cache_key(self, request: ProviderRequest, scope: CacheScope) -> tuple[str, str, str]:
        effective_request = {
            "operation": request.operation,
            "operation_version": _OPERATION_VERSION,
            "prompt": request.prompt,
            "evidence_context": request.evidence_context,
        }
        sd = scope_digest(
            scope.application_namespace or "", scope.store_instance_id, scope.history_id,
            scope.cache_generation,
        )
        rd = request_digest(effective_request)
        return cache_key(sd, rd), sd, rd

    async def complete(
        self,
        request: ProviderRequest,
        deadline_at: float,
        cache_scope: CacheScope | None = None,
        budget: CallBudget | None = None,
    ) -> ProviderResponse:
        cacheable = (
            self.cache is not None
            and cache_scope is not None
            and request.operation in _CACHEABLE_OPERATIONS
        )

        key = sd = rd = None
        if cacheable:
            key, sd, rd = self._cache_key(request, cache_scope)
            now = self._now()
            try:
                cached = await asyncio.wait_for(
                    self.cache.get(key, now=now),
                    timeout=self.config.cache_overhead_budget_ms / 1000,
                )
            except Exception:
                # A cache error degrades to a fresh computation, never an error surfaced to the
                # caller (constitution Principle III) — cache health never blocks history/answer
                # correctness.
                cached = None
                if self.usage_log is not None:
                    self.usage_log.memo_errors += 1
            if (
                cached is not None
                and cached.cache_format_version == CACHE_FORMAT_VERSION
                and cached.scope_digest == sd
                and cached.request_digest == rd
            ):
                if self.usage_log is not None:
                    self.usage_log.memo_hits += 1
                    if cached.reused_operation_usage is not None:
                        self.usage_log.reused_operation_usage_count += 1
                response = ProviderResponse(
                    text=cached.payload.get("text", ""),
                    input_tokens=cached.payload.get("input_tokens"),
                    output_tokens=cached.payload.get("output_tokens"),
                    usage_unknown=cached.payload.get("usage_unknown", False),
                    from_cache=True,
                    reused_operation_usage=cached.reused_operation_usage,
                )
                if budget is not None:
                    budget.record(response)
                return response
            if cacheable and self.usage_log is not None:
                self.usage_log.memo_misses += 1
            if budget is not None:
                budget.memo_misses += 1

        attempts = 0

        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=max(0, deadline_at - time.monotonic()))
        except asyncio.TimeoutError as exc:
            raise ProviderTimeout("provider admission exceeded request deadline") from exc
        try:
            async def attempt() -> ProviderResponse:
                nonlocal attempts
                if budget is not None:
                    budget.admit(retry=attempts > 0)
                attempts += 1
                remaining = deadline_at - time.monotonic()
                call_timeout = min(self.config.provider_call_deadline_s, max(remaining, 0))
                try:
                    return await asyncio.wait_for(self.inner.complete(request), timeout=call_timeout)
                except asyncio.TimeoutError as exc:
                    if budget is not None:
                        budget.usage_unknown = True
                    raise ProviderTimeout(
                        f"provider call for operation {request.operation!r} exceeded its deadline"
                    ) from exc
                except Exception:
                    if budget is not None:
                        budget.usage_unknown = True
                    raise

            try:
                response = await async_retry(
                    attempt,
                    deadline_at=deadline_at,
                    max_attempts=self.config.max_provider_attempts_per_op,
                )
            except Exception:
                if self.usage_log is not None:
                    self.usage_log.provider_errors += 1
                    self.usage_log.provider_retries += max(0, attempts - 1)
                raise
        finally:
            self._semaphore.release()

        if self.usage_log is not None:
            self.usage_log.provider_calls += 1
            self.usage_log.provider_retries += max(0, attempts - 1)
            if response.usage_unknown:
                self.usage_log.usage_unknown_count += 1

        if cacheable and key is not None:
            # Never cached: only a successful response reaches here (a raised error propagates
            # out of complete() above, never reaching this write) — INV-06.
            now = self._now()
            value = CachedValue(
                cache_format_version=CACHE_FORMAT_VERSION,
                scope_digest=sd,
                request_digest=rd,
                operation_version=_OPERATION_VERSION,
                schema_version=_SCHEMA_VERSION,
                created_at=now,
                absolute_expiry=now + self.config.memo_lifetime_s,
                payload={
                    "text": response.text,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "usage_unknown": response.usage_unknown,
                },
                reused_operation_usage=(
                    None
                    if response.usage_unknown
                    else {"input_tokens": response.input_tokens, "output_tokens": response.output_tokens}
                ),
            )
            try:
                await asyncio.wait_for(
                    self.cache.set(key, value), timeout=self.config.cache_overhead_budget_ms / 1000
                )
            except Exception:
                pass  # a cache write failure never fails the underlying operation (Principle III)

        if budget is not None:
            budget.record(response)
        return response
