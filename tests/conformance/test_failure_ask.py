"""Failure injection F6 (provider failure by stage, synthesis-stage case) and F7
(deadline/cancellation), per spec/fixtures/failure-injection-harness.md (AT-08, AT-14).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.ask import ask
from cci.clear import clear_history
from cci.errors import BudgetExceeded, ProviderTimeout, VersionConflict
from cci.ingest import ingest
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderResponse
from cci.store import HistoryStore


async def _seeded_store(path: str) -> HistoryStore:
    store = await HistoryStore.open(path)
    await ingest(
        store, store.history_id,
        [InputMessage(role="user", content="rayleigh scattering explains the sky's color")],
        "src-1", "key-1",
    )
    return store


def test_f6_synthesis_failure_after_retries_raises_typed_provider_error():
    """Fail synthesis after permitted retries — assert a typed provider error is raised, not
    an empty result."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "f6.db"))
            fake = FakeProvider(fail_times=99, fail_error=ProviderTimeout("simulated synthesis failure"))
            provider = MemoizedProvider(inner=fake, config=store.config)

            try:
                await ask(store, "rayleigh scattering", provider=provider)
                raise AssertionError("expected a typed provider error, not an empty result")
            except ProviderTimeout:
                pass

            assert fake.call_count == store.config.max_provider_attempts_per_op, (
                "the provider was retried up to its permitted attempt budget, not zero and "
                "not unbounded"
            )
            await store.aclose()

    asyncio.run(scenario())


def test_f7_budget_exhausted_before_useful_work_raises_budget_exceeded():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "f7a.db"))
            fake = FakeProvider()
            provider = MemoizedProvider(inner=fake, config=store.config)

            try:
                # A deadline already in the past (relative to the real clock) at call time —
                # deterministic without needing to stage a fake clock across two calls.
                await ask(
                    store, "rayleigh scattering", provider=provider, deadline_s=-1_000_000.0,
                )
                raise AssertionError("expected BudgetExceeded")
            except BudgetExceeded:
                pass
            assert fake.call_count == 0, "no dispatch after the limit — before any useful work"
            await store.aclose()

    asyncio.run(scenario())


def test_f7_budget_exhausted_with_evidence_returns_usable_partial_result():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "f7b.db"))
            fake = FakeProvider()
            provider = MemoizedProvider(inner=fake, config=store.config)

            # A clock that reports "not yet expired" for the first call (the pre-retrieve
            # check) and "expired" from the second call onward (the post-evidence check) —
            # deterministically reproduces "evidence already found, deadline hits before
            # synthesis" without timing-dependent sleeps.
            calls = {"n": 0}

            def staged_clock() -> float:
                # Call 1: deadline_at computation. Call 2: the pre-retrieve check (must still
                # read "not expired"). Call 3+: the post-evidence check (must read "expired").
                calls["n"] += 1
                return 0.0 if calls["n"] <= 2 else 1_000_000.0

            result = await ask(
                store, "rayleigh scattering", provider=provider, deadline_s=500_000.0,
                _monotonic=staged_clock,
            )
            assert result.status == "partial"
            assert result.answer is None, "no unvalidated answer"
            assert len(result.evidence) >= 1, (
                "usable partial retrieval on budget-exhausted-with-evidence"
            )
            assert fake.call_count == 0, "no new dispatch after the limit"
            await store.aclose()

    asyncio.run(scenario())


def test_f7_cancellation_propagates_and_releases_semaphore():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "f7c.db"))
            fake = FakeProvider(hang=True)
            provider = MemoizedProvider(inner=fake, config=store.config)

            task = asyncio.create_task(
                ask(store, "rayleigh scattering", provider=provider, deadline_s=60)
            )
            await asyncio.wait_for(fake.entered.wait(), timeout=5)
            task.cancel()
            try:
                await task
                raise AssertionError("expected CancelledError to propagate")
            except asyncio.CancelledError:
                pass

            # The semaphore was released by cancellation — a fresh call reusing the SAME
            # `provider` (same MemoizedProvider, same semaphore) can still acquire it and
            # complete normally, proving no resource leaked.
            fake._responses.append(
                ProviderResponse(text=json.dumps({"answer": "ok", "citations": ["ev_1"]}))
            )
            fake._hang = False
            result = await asyncio.wait_for(
                ask(store, "rayleigh scattering", provider=provider), timeout=5
            )
            assert result.status == "answered"
            await store.aclose()

    asyncio.run(scenario())


def test_emission_time_version_conflict_when_clear_races_a_paused_synthesis_call():
    """data-model.md Snapshot/Generation lifecycle case 2, `ask()`'s own re-check: a
    `clear_history()` that commits while a synthesis provider call is in flight must not let
    `ask()` publish an answered/partial result built on the retired generation."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ask_vc.db")
            store = await HistoryStore.open(path)
            history_id = store.history_id
            await ingest(
                store, history_id, [InputMessage(role="user", content="rayleigh scattering")],
                "src-1", "key-1",
            )

            resume = asyncio.Event()
            fake = FakeProvider(
                resume=resume,
                responses=[
                    ProviderResponse(text=json.dumps({"answer": "y", "citations": ["ev_1"]}))
                ],
            )
            provider = MemoizedProvider(inner=fake, config=store.config)

            task = asyncio.create_task(
                ask(store, "rayleigh scattering", provider=provider, deadline_s=60)
            )
            await asyncio.wait_for(fake.entered.wait(), timeout=5)

            # ask() has captured its Snapshot and is parked inside the provider call — it holds
            # no write_lock right now, so a clear can run concurrently.
            clear_report = await clear_history(store, history_id)
            assert clear_report.logical_clear_complete is True

            resume.set()
            try:
                await task
                raise AssertionError(
                    "expected VersionConflict — a paused ask() must not publish a result built "
                    "on the retired generation"
                )
            except VersionConflict:
                pass
            await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_f6_synthesis_failure_after_retries_raises_typed_provider_error()
    test_f7_budget_exhausted_before_useful_work_raises_budget_exceeded()
    test_f7_budget_exhausted_with_evidence_returns_usable_partial_result()
    test_f7_cancellation_propagates_and_releases_semaphore()
    test_emission_time_version_conflict_when_clear_races_a_paused_synthesis_call()
    print("F6/F7 failure-injection checks passed")
