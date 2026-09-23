"""Privacy/usage test (AT-20), per spec/fixtures/failure-injection-harness.md "Release checks":
collect default events during hits, retries, and failures; assert no credentials, prompts, or
message bodies appear; reused usage stays separate from current usage; absent provider usage is
marked unknown.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.errors import ProviderTimeout
from cci.index import index
from cci.ingest import ingest
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderResponse
from cci.stats import stats
from cci.store import HistoryStore

_SECRET_PROMPT_MARKER = "sk-live-CREDENTIAL-should-never-appear-in-diagnostics"
_SECRET_MESSAGE_BODY = "my password is hunter2, do not log this"


def test_default_events_collected_with_no_content_or_credential_leakage():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await HistoryStore.open(os.path.join(d, "priv1.db"), config={"cache_backend": "sqlite"})
            await ingest(
                store, store.history_id,
                [InputMessage(role="user", content=_SECRET_MESSAGE_BODY)],
                "src-1", "key-1",
            )

            # A hit: run the same eligible operation twice.
            fake_hit = FakeProvider(
                responses=[ProviderResponse(text='{"title": "T", "summary": "S"}')]
            )
            provider_hit = MemoizedProvider(
                inner=fake_hit, config=store.config, cache=store.cache, usage_log=store.usage_log,
            )
            await index(store, provider=provider_hit, rebuild=True)
            await index(store, provider=provider_hit, rebuild=True)  # warm-cache hit

            # A retry-then-succeed, on a fresh history so it produces a real cache miss+retry.
            store2 = await HistoryStore.open(os.path.join(d, "priv2.db"), config={"cache_backend": "sqlite"})
            await ingest(
                store2, store2.history_id,
                [InputMessage(role="user", content="unrelated message for the retry case")],
                "src-1", "key-1",
            )
            fake_retry = FakeProvider(
                fail_times=1,
                fail_error=ProviderTimeout(_SECRET_PROMPT_MARKER),
                responses=[ProviderResponse(text='{"title": "T2", "summary": "S2"}')],
            )
            provider_retry = MemoizedProvider(
                inner=fake_retry, config=store2.config, cache=store2.cache,
                usage_log=store2.usage_log,
            )
            await index(store2, provider=provider_retry, rebuild=True)

            # A failure: exhausts retries.
            store3 = await HistoryStore.open(os.path.join(d, "priv3.db"), config={"cache_backend": "sqlite"})
            await ingest(
                store3, store3.history_id,
                [InputMessage(role="user", content="a message whose indexing will fail")],
                "src-1", "key-1",
            )
            fake_fail = FakeProvider(fail_times=999, fail_error=ProviderTimeout(_SECRET_PROMPT_MARKER))
            provider_fail = MemoizedProvider(
                inner=fake_fail, config=store3.config, cache=store3.cache,
                usage_log=store3.usage_log,
            )
            try:
                await index(store3, provider=provider_fail, rebuild=True)
                raise AssertionError("expected a provider failure")
            except ProviderTimeout:
                pass

            hit_result = await stats(store)
            retry_result = await stats(store2)
            fail_result = await stats(store3)

            assert hit_result.memo_hits >= 1, "a hit must be collected"
            assert retry_result.provider_retries >= 1, "a retry must be collected"
            assert fail_result.provider_errors >= 1, "a failure must be collected"

            for result in (hit_result, retry_result, fail_result):
                rendered = repr(result)
                assert _SECRET_MESSAGE_BODY not in rendered
                assert _SECRET_PROMPT_MARKER not in rendered
                assert "password" not in rendered
                assert "hunter2" not in rendered

            await store.aclose()
            await store2.aclose()
            await store3.aclose()

    asyncio.run(scenario())


def test_reused_usage_stays_separate_from_current_usage():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await HistoryStore.open(os.path.join(d, "priv4.db"), config={"cache_backend": "sqlite"})
            await ingest(
                store, store.history_id,
                [InputMessage(role="user", content="cacheable content for reuse")],
                "src-1", "key-1",
            )
            fake = FakeProvider(
                responses=[
                    ProviderResponse(text='{"title": "T", "summary": "S"}', input_tokens=10, output_tokens=5)
                ]
            )
            provider = MemoizedProvider(
                inner=fake, config=store.config, cache=store.cache, usage_log=store.usage_log,
            )
            first = await index(store, provider=provider, rebuild=True)
            second = await index(store, provider=provider, rebuild=True)

            assert first.provider_usage.memo_hits == 0
            assert first.provider_usage.current_provider_calls == 1
            # The reused (second, cache-served) call is counted separately from current
            # (fresh) provider calls — never folded into the same counter.
            assert second.provider_usage.memo_hits == 1
            assert second.provider_usage.current_provider_calls == 0

            result = await stats(store)
            assert result.reused_operation_usage_count >= 1

            await store.aclose()

    asyncio.run(scenario())


def test_absent_provider_usage_is_marked_unknown():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await HistoryStore.open(os.path.join(d, "priv5.db"), config={"cache_backend": "sqlite"})
            await ingest(
                store, store.history_id,
                [InputMessage(role="user", content="a message with no token usage reported")],
                "src-1", "key-1",
            )
            # FakeProvider's default response reports no token counts — the fake never sets
            # usage_unknown explicitly, but a real double lacking usage data would; this mirrors
            # the "no usage back from the model" case via an explicit ProviderResponse.
            fake = FakeProvider(
                responses=[
                    ProviderResponse(text='{"title": "T", "summary": "S"}', usage_unknown=True)
                ]
            )
            provider = MemoizedProvider(
                inner=fake, config=store.config, cache=store.cache, usage_log=store.usage_log,
            )
            report = await index(store, provider=provider, rebuild=True)
            assert report.provider_usage.usage_unknown is True

            result = await stats(store)
            assert result.usage_unknown is True

            await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_default_events_collected_with_no_content_or_credential_leakage()
    test_reused_usage_stays_separate_from_current_usage()
    test_absent_provider_usage_is_marked_unknown()
    print("AT-20 diagnostics privacy checks passed")
