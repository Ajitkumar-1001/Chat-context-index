"""Evaluation accounting regressions using a tiny fixture, never reserved held-out labels."""

import argparse
import asyncio
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cci.errors import ProviderError
from cci.provider import Provider, ProviderRequest, ProviderResponse
from cci.retrieve import Evidence, Usage
from openai import AsyncOpenAI

SPEC = importlib.util.spec_from_file_location(
    "held_out_harness", Path(__file__).resolve().parents[2] / "evaluations/held_out/run_evaluation.py",
)
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)


def test_real_provider_selection_is_shared_with_smoke_and_client_is_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    helpers = HARNESS._model_helpers()
    original_make_client = helpers.ModelSettings.make_client
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "generativelanguage.googleapis.com"
        body = json.loads(request.content)
        assert body["model"] == "unit-model" and body["max_tokens"] == 1_024
        return httpx.Response(200, json={
            "id": "unit", "object": "chat.completion", "created": 1, "model": "unit-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "{}"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        })

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def make_client(settings: object, *, timeout_s: int) -> AsyncOpenAI:
        return original_make_client(settings, timeout_s=timeout_s, http_client=http_client)

    async def trial(fixture: dict, factory: Callable[[], Provider], trial_index: int) -> dict:
        for operation in ("indexing", "tree_navigation", "synthesis"):
            response = await factory().complete(ProviderRequest(operation, "Return JSON", "unit evidence"))
            assert response.input_tokens == 12 and response.output_tokens == 4
        return {"macro_evidence_recall": 0, "abstention_correct_count": 0,
                "structurally_valid_citation_rate": None, "errors": []}

    monkeypatch.setenv("CCI_API_KEY", "unit-key")
    monkeypatch.setattr(helpers.ModelSettings, "make_client", make_client)
    monkeypatch.setattr(HARNESS, "_load_fixture", lambda: {"_meta": {"fixture": "unit-only"}})
    monkeypatch.setattr(HARNESS, "run_trial", trial)
    output = tmp_path / "unit-report.json"
    args = argparse.Namespace(provider="gemini", model="unit-model", base_url=None,
                              env_file=None, trials=1, out=str(output))
    assert asyncio.run(HARNESS.main_async(args)) == 0
    assert len(requests) == 3 and http_client.is_closed
    report = json.loads(output.read_text())
    assert report["provider_settings"]["provider"] == "gemini"
    assert "unit-key" not in output.read_text()


def fixture():
    queries = []
    for i in range(3):
        queries.append({
            "query_id": f"q{i}", "query_text": f"question-{i}", "history_label": "unit-history",
            "split": "held_out", "answerable": i < 2,
            "required_evidence_units": [{"message_index": 0, "acceptable_span": {"start": 0, "end": 4}}],
            "answer_rubric": {"correct_value": "Oslo", "must_not_cite_values": []},
        })
    return {"histories": [{"history_label": "unit-history", "split": "held_out", "messages": [
        {"source_id": "unit", "role": "user", "content": "Oslo"},
    ]}], "queries": queries}


@pytest.mark.parametrize("failed", [False, True], ids=["unanswered", "failed"])
@pytest.mark.parametrize("absent_status", ["insufficient_evidence", "partial"])
def test_all_answerable_queries_stay_in_denominator_and_indexing_finishes(monkeypatch, failed, absent_status):
    index_calls = []

    async def index(store, provider):
        index_calls.append(True)
        return SimpleNamespace(
            status="partial" if len(index_calls) == 1 else "complete",
            committed_coverage=SimpleNamespace(end_seq=1),
        )

    async def ask(store, query, provider):
        if failed and query == "question-1":
            raise ProviderError("unit fixture failure")
        answered = query == "question-0"
        return SimpleNamespace(
            answer="Oslo" if answered else None, status="answered" if answered else absent_status,
            citations=[], usage=Usage(),
            evidence=[Evidence("ev_1", "unit-id", 1, "/content", "Oslo", "unit-hash")] if answered else [],
        )

    monkeypatch.setattr(HARNESS, "index", index)
    monkeypatch.setattr(HARNESS, "ask", ask)
    result = asyncio.run(HARNESS.run_trial(fixture(), HARNESS._ScriptedIndexingProvider, 1))
    assert len(index_calls) == 2
    assert result["answer_correctness_proxy"] == result["macro_evidence_recall"] == 0.5
    assert result["answerable_queries_scored"] == 2 and result["answers_produced"] == 1
    assert result["absent_total"] == 1
    assert result["abstention_correct_count"] == int(absent_status == "insufficient_evidence")
    assert result["structurally_valid_citation_rate"] is None
    assert len(result["query_results"]) == 3 and len(result["errors"]) == int(failed)


def test_accounting_keeps_observed_tokens_separate_from_missing_usage():
    class Provider:
        async def complete(self, request):
            if request.operation == "indexing":
                return ProviderResponse("summary", input_tokens=17, output_tokens=5)
            return ProviderResponse("navigation")

    async def run():
        usage = {}
        provider = HARNESS._MeasuredProvider(Provider(), usage)
        await provider.complete(ProviderRequest("indexing", "", ""))
        await provider.complete(ProviderRequest("tree_navigation", "", ""))
        assert usage["indexing"] == {
            "attempts": 1, "input_tokens_observed": 17, "output_tokens_observed": 5, "usage_unknown": False,
        }
        assert usage["tree_navigation"]["usage_unknown"]

    asyncio.run(run())


def test_fake_provider_cannot_pass_a_quality_gate():
    assert "NOT RUN" in HARNESS._apply_sc011_gates({})["status"]
    gates = HARNESS._apply_sc011_gates({
        "macro_evidence_recall": 1.0, "abstention_correct_count": 8,
        "structurally_valid_citation_rate": None, "errors": [],
    }, real_provider=True)
    assert not gates["citation_validity_pass"]
    assert "NOT RUN" in gates["answer_rubric_correctness"]
