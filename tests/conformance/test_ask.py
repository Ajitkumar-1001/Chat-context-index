"""quickstart.md Scenario 5 — Evidence-backed ask() (User Story 3, M2; AT-10, AT-13).
Also covers spec/fixtures/retrieval-evidence.json R3/R4.

1. Ask a question with no supporting evidence -> insufficient_evidence, answer: null, zero
   provider calls.
2. Ask a question with clear supporting evidence -> an answer whose every citation resolves to
   an evidence_id present in that same response's evidence[].
3. Invalid citations are structurally rejected; a repair-then-still-invalid answer is
   suppressed, never returned to the caller.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.ask import ask
from cci.errors import ConfigurationError
from cci.ingest import ingest
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderResponse
from cci.store import HistoryStore


async def _seeded_store(path: str) -> HistoryStore:
    store = await HistoryStore.open(path)
    await ingest(
        store, store.history_id,
        [InputMessage(role="user", content="the sky is blue because of rayleigh scattering")],
        "src-1", "key-1",
    )
    return store


def test_no_evidence_returns_insufficient_evidence_zero_provider_calls():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "ask1.db"))
            fake = FakeProvider()
            provider = MemoizedProvider(inner=fake, config=store.config)

            result = await ask(
                store, "a question with no supporting evidence anywhere in the history",
                provider=provider,
            )
            assert result.status == "insufficient_evidence"
            assert result.answer is None
            assert fake.call_count == 0, "no evidence found skips synthesis entirely"
            await store.aclose()

    asyncio.run(scenario())


def test_cited_answer_resolves_within_current_response_evidence():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "ask2.db"))
            fake = FakeProvider(
                responses=[
                    ProviderResponse(
                        text=json.dumps(
                            {"answer": "Rayleigh scattering.", "citations": ["ev_1"]}
                        )
                    )
                ]
            )
            provider = MemoizedProvider(inner=fake, config=store.config)

            result = await ask(store, "rayleigh scattering", provider=provider)
            assert result.status == "answered"
            assert result.answer is not None
            evidence_ids = {e.evidence_id for e in result.evidence}
            assert all(c.evidence_id in evidence_ids for c in result.citations), (
                "every citation resolves to an evidence_id present in this same response's "
                "evidence[]"
            )
            assert result.usage.current_provider_calls == fake.call_count, (
                "usage is never fabricated as zero — it reflects ask()'s own provider calls"
            )
            await store.aclose()

    asyncio.run(scenario())


def test_ask_without_provider_when_evidence_exists_is_configuration_error():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "ask3.db"))
            try:
                await ask(store, "rayleigh scattering", provider=None)
                raise AssertionError("expected ConfigurationError")
            except ConfigurationError:
                pass
            await store.aclose()

    asyncio.run(scenario())


def test_invalid_citation_repaired_then_still_invalid_is_suppressed():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "ask4.db"))
            fake = FakeProvider(
                responses=[
                    ProviderResponse(text=json.dumps({"answer": "x", "citations": ["ev_999"]})),
                    ProviderResponse(text=json.dumps({"answer": "y", "citations": ["ev_999"]})),
                ]
            )
            provider = MemoizedProvider(inner=fake, config=store.config)

            result = await ask(store, "rayleigh scattering", provider=provider)
            assert result.status == "partial"
            assert result.answer is None, "a still-invalid answer is never returned to the caller"
            assert fake.call_count == 2, "at most one repair attempt"
            await store.aclose()

    asyncio.run(scenario())


def test_malformed_model_output_is_suppressed_not_answered():
    """Malformed/empty model output must not be emitted as an 'answered' result — an unbacked
    or unparseable response takes the same repair-then-suppress path as an invalid citation."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "ask6.db"))
            fake = FakeProvider(
                responses=[
                    ProviderResponse(text="not json"),
                    ProviderResponse(text="still not json"),
                ]
            )
            provider = MemoizedProvider(inner=fake, config=store.config)

            result = await ask(store, "rayleigh scattering", provider=provider)
            assert result.status == "partial"
            assert result.answer is None
            assert fake.call_count == 2
            await store.aclose()

    asyncio.run(scenario())


def test_answer_with_zero_citations_is_suppressed_as_unbacked_claim():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "ask7.db"))
            fake = FakeProvider(
                responses=[
                    ProviderResponse(text=json.dumps({"answer": "some claim", "citations": []})),
                    ProviderResponse(text=json.dumps({"answer": "some claim", "citations": []})),
                ]
            )
            provider = MemoizedProvider(inner=fake, config=store.config)

            result = await ask(store, "rayleigh scattering", provider=provider)
            assert result.status == "partial"
            assert result.answer is None
            await store.aclose()

    asyncio.run(scenario())


def test_invalid_citation_repaired_successfully_is_answered():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            store = await _seeded_store(os.path.join(d, "ask5.db"))
            fake = FakeProvider(
                responses=[
                    ProviderResponse(text=json.dumps({"answer": "x", "citations": ["ev_999"]})),
                    ProviderResponse(text=json.dumps({"answer": "y", "citations": ["ev_1"]})),
                ]
            )
            provider = MemoizedProvider(inner=fake, config=store.config)

            result = await ask(store, "rayleigh scattering", provider=provider)
            assert result.status == "answered"
            assert result.answer == "y"
            await store.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_no_evidence_returns_insufficient_evidence_zero_provider_calls()
    test_cited_answer_resolves_within_current_response_evidence()
    test_ask_without_provider_when_evidence_exists_is_configuration_error()
    test_invalid_citation_repaired_then_still_invalid_is_suppressed()
    test_malformed_model_output_is_suppressed_not_answered()
    test_answer_with_zero_citations_is_suppressed_as_unbacked_claim()
    test_invalid_citation_repaired_successfully_is_answered()
    print("Scenario 5 (evidence-backed ask()) checks passed")
