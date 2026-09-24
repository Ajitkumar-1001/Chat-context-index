"""Comparison regressions use invented unit data, never held-out labels."""

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from cci.config import Config
from cci.provider import MemoizedProvider, ProviderRequest, ProviderResponse
from cci.store import HistoryStore
from openai import RateLimitError
from openai.types.chat import ChatCompletion

SPEC = importlib.util.spec_from_file_location(
    "unit_comparison",
    Path(__file__).resolve().parents[2] / "evaluations/held_out/comparison.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def plan() -> dict:
    return {
        "trials": 1,
        "comparison_max_quality_loss": 0.05,
        "initial_history_percent": 50,
        "context": {"recent_messages": 1, "max_messages": 2, "max_chars": 4_000, "excerpt_chars": 200},
        "package_config": {"cache_backend": "none", "target_chunk_size_scalars": 100},
        "limits": {
            "concurrency": 2,
            "max_calls": 100,
            "max_reserved_tokens": 500_000,
            "max_estimated_usd": 3,
            "run_timeout_s": 60,
            "call_timeout_s": 1,
            "max_output_tokens_per_call": 128,
            "max_input_bytes_per_call": 100_000,
        },
        "pricing": {"input_usd_per_million": 0.3, "output_usd_per_million": 2.5},
        "quality_gates": {
            "evidence_recall": 0.85,
            "answer_correctness": 0.8,
            "abstention_count": 1,
            "citation_support": 0.9,
        },
    }


def completion(text: str = "{}", *, usage: bool = True, finish: str = "stop") -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "unit",
            "object": "chat.completion",
            "created": 1,
            "model": "unit-model",
            "choices": [
                {"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": text}}
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16} if usage else None,
        }
    )


class Adapter:
    async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
        return completion()


def scope() -> dict:
    return {"trial": 1, "history": "unit", "strategy": "tree", "query_id": None, "phase": "query"}


def test_concurrent_budget_admission_cannot_overspend_call_limit(tmp_path: Path) -> None:
    async def run() -> None:
        settings = plan()
        settings["limits"]["max_calls"] = 1
        ledger = MODULE.Ledger(Adapter(), settings, tmp_path / "calls.jsonl")
        request = ProviderRequest("synthesis", "JSON", "unit")
        results = await asyncio.gather(
            ledger.complete(request, scope()), ledger.complete(request, scope()), return_exceptions=True
        )
        assert sum(isinstance(value, ProviderResponse) for value in results) == 1
        assert sum(isinstance(value, MODULE.RunStopped) for value in results) == 1
        assert len(ledger.calls) == 1
        entries = [json.loads(line) for line in ledger.journal.read_text().splitlines()]
        assert entries[0]["status"] == "pending" and entries[-1]["status"] == "ok"

    asyncio.run(run())


@pytest.mark.parametrize("limit", ["max_reserved_tokens", "max_estimated_usd"])
def test_reservation_rejects_work_before_dispatch(limit: str, tmp_path: Path) -> None:
    async def run() -> None:
        settings = plan()
        settings["limits"][limit] = 0
        ledger = MODULE.Ledger(Adapter(), settings, tmp_path / "calls.jsonl")
        with pytest.raises(MODULE.RunStopped):
            await ledger.complete(ProviderRequest("synthesis", "JSON", "unit"), scope())
        assert not ledger.calls and not ledger.journal.exists()

    asyncio.run(run())


def test_cancellation_retains_unknown_usage_and_full_reservation(tmp_path: Path) -> None:
    async def run() -> None:
        entered = asyncio.Event()

        class Hanging:
            async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
                entered.set()
                await asyncio.Event().wait()
                return completion()

        ledger = MODULE.Ledger(Hanging(), plan(), tmp_path / "calls.jsonl")
        task = asyncio.create_task(ledger.complete(ProviderRequest("synthesis", "JSON", "unit"), scope()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        call = ledger.calls[0]
        assert call["status"] == "error" and call["input_tokens"] is None
        assert ledger.reserved()[0] == call["reserved_input_tokens"] + call["reserved_output_tokens"]
        assert MODULE.usage_summary(ledger.calls, ledger)["usage_unknown"]

    asyncio.run(run())


def test_missing_usage_stops_future_spend(tmp_path: Path) -> None:
    class Missing:
        async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
            return completion(usage=False)

    async def run() -> None:
        ledger = MODULE.Ledger(Missing(), plan(), tmp_path / "calls.jsonl")
        for _ in range(2):
            with pytest.raises(MODULE.RunStopped):
                await ledger.complete(ProviderRequest("synthesis", "JSON", "unit"), scope())
        assert len(ledger.calls) == 1
        assert not MODULE.usage_summary(ledger.calls, ledger)["complete_cost_estimate"]

    asyncio.run(run())


def test_truncation_is_a_failure_but_returned_usage_is_retained(tmp_path: Path) -> None:
    class Truncated:
        async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
            return completion(finish="length")

    async def run() -> None:
        ledger = MODULE.Ledger(Truncated(), plan(), tmp_path / "calls.jsonl")
        with pytest.raises(MODULE.ProviderError):
            await ledger.complete(ProviderRequest("synthesis", "JSON", "unit"), scope())
        assert ledger.calls[0]["input_tokens"] == 12 and ledger.calls[0]["status"] == "error"

    asyncio.run(run())


def test_explicit_abstention_is_distinct_from_invalid_or_uncited_answers() -> None:
    assert MODULE.parse_answer('{"answer":null,"citations":[]}', set())["status"] == "abstained"
    assert MODULE.parse_answer('{"answer":"Oslo","citations":[]}', set())["status"] == "invalid_citations"
    assert not MODULE.parse_answer('{"answer":"Oslo","citations":["wrong"]}', {"unit"})["citations_valid"]
    with pytest.raises(ValueError):
        MODULE.parse_answer('{"answer":null,"citations":["unit"]}', {"unit"})
    with pytest.raises(ValueError):
        MODULE.parse_review('{"correct":true,"citations":[]}', ["unit"])


def test_failures_and_unexecuted_cases_remain_in_scoring_denominators(tmp_path: Path) -> None:
    queries = [{"answerable": True}, {"answerable": True}, {"answerable": False}]
    row = {
        "trial": 1,
        "strategy": "tree",
        "status": "answered",
        "answerable": True,
        "evidence_recall": 1.0,
        "correction_case": True,
        "answer": "Oslo",
        "citations": ["unit"],
        "evidence": [{"message_id": "unit"}],
        "review": {
            "status": "ok",
            "correct": True,
            "citations": [{"evidence_id": "unit", "supports_claim": True}],
        },
    }
    ledger = MODULE.Ledger(Adapter(), plan(), tmp_path / "calls.jsonl")
    results = MODULE.summarize([row], queries, ledger, plan())
    tree = next(r for r in results if r["strategy"] == "tree")
    assert tree["macro_evidence_recall"] == tree["reviewed_answer_correctness"] == 0.5
    assert tree["correct_abstentions"] == 0 and tree["quality_status"] == "FAIL"
    assert not tree["gates"]["complete_execution"]


def test_full_history_retains_every_source_and_labels_never_enter_answer_prompt(tmp_path: Path) -> None:
    async def run() -> None:
        messages = [
            {"source_id": "unit", "role": "user", "content": "Initial city: Rome."},
            {"source_id": "unit", "role": "user", "content": "Correction: city is Oslo."},
        ]
        requests: list[ProviderRequest] = []

        class Recording:
            async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
                requests.append(request)
                return completion('{"answer":null,"citations":[]}')

        ledger = MODULE.Ledger(Recording(), plan(), tmp_path / "calls.jsonl")
        async with await HistoryStore.open(
            str(tmp_path / "memory.db"), config=Config(cache_backend="none")
        ) as store:
            await MODULE.append_messages(store, messages[:1])
            await MODULE.append_messages(store, messages[1:], offset=1)
            provider = MemoizedProvider(MODULE.ScopedProvider(ledger, scope()), store.config)
            context = await MODULE.get_context(store, "Which city?", "full_history", provider, plan())
            assert [item.seq for item in context.items] == [1, 2]
            query = {
                "query_id": "unit-q",
                "query_text": "Which city?",
                "answerable": True,
                "required_evidence_units": [],
                "answer_rubric": {
                    "correct_value": "SECRET_LABEL",
                    "must_not_cite_values": ["OTHER_SECRET_LABEL"],
                },
            }
            result = await MODULE.query_case(
                store,
                query,
                {"history_label": "unit", "messages": messages},
                "full_history",
                1,
                ledger,
                plan(),
                lambda *args: 1.0,
            )
            assert result["status"] == "abstained"
            assert all("SECRET_LABEL" not in (r.prompt + r.evidence_context) for r in requests)

    asyncio.run(run())


def test_provider_rejection_stops_new_calls_and_does_not_log_error_body(tmp_path: Path) -> None:
    class Rejected:
        async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
            response = httpx.Response(429, request=httpx.Request("POST", "https://unit.invalid"))
            raise RateLimitError("PRIVATE_PROVIDER_BODY", response=response, body={"secret": "PRIVATE_KEY"})

    async def run() -> None:
        ledger = MODULE.Ledger(Rejected(), plan(), tmp_path / "calls.jsonl")
        with pytest.raises(MODULE.ProviderError):
            await ledger.complete(ProviderRequest("synthesis", "JSON", "unit"), scope())
        with pytest.raises(MODULE.RunStopped):
            await ledger.complete(ProviderRequest("synthesis", "JSON", "unit"), scope())
        assert len(ledger.calls) == 1 and ledger.calls[0]["http_status"] == 429
        assert "PRIVATE" not in ledger.journal.read_text()

    asyncio.run(run())


def test_comparison_lifecycle_covers_initial_index_updates_and_reopen(tmp_path: Path) -> None:
    class LifecycleAdapter:
        async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
            if request.operation == "indexing":
                return completion('{"title":"City discussion","summary":"A city decision was discussed."}')
            if request.operation == "tree_navigation":
                return completion('{"node_ids":[]}')
            return completion('{"answer":null,"citations":[]}')

    async def run() -> None:
        history = {
            "history_label": "unit-history",
            "messages": [
                {"source_id": "unit", "role": "user", "content": "The city is Rome."},
                {"source_id": "unit", "role": "user", "content": "Correction: the city is Oslo."},
            ],
        }
        query = {
            "query_id": "unit-query",
            "query_text": "Which city?",
            "answerable": True,
            "required_evidence_units": [],
            "answer_rubric": {"correct_value": "Oslo", "must_not_cite_values": ["Rome"]},
        }
        settings = plan()
        ledger = MODULE.Ledger(LifecycleAdapter(), settings, tmp_path / "calls.jsonl")
        report = {"histories": [], "query_results": []}
        await MODULE.run_history(
            history, [query], 1, tmp_path, ledger, settings, report, lambda *args: 0.0, lambda: None
        )
        lifecycle = report["histories"][0]
        assert lifecycle["initial_index"]["committed_coverage"]["end_seq"] == 1
        assert lifecycle["update_index"]["committed_coverage"]["end_seq"] == 2
        assert lifecycle["unchanged_index_calls"] == 0 and lifecycle["reopen_preserved_originals"]
        assert {r["strategy"] for r in report["query_results"]} == set(MODULE.STRATEGIES)
        assert any(c["phase"] == "initial_index" for c in ledger.calls)
        assert any(c["phase"] == "update_index" for c in ledger.calls)
        assert all(c["status"] == "ok" for c in ledger.calls)

    asyncio.run(run())


def test_cheaper_failed_strategy_cannot_support_a_savings_claim() -> None:
    def summary(strategy: str, status: str, cost: float, complete: bool = True) -> dict:
        return {
            "trial": 1,
            "strategy": strategy,
            "quality_status": status,
            "macro_evidence_recall": 1.0,
            "reviewed_answer_correctness": 1.0,
            "application_usage": {
                "complete_cost_estimate": complete,
                "standard_price_estimate_observed_usd": cost,
            },
        }

    baseline = summary("full_history", "PASS", 1.0)
    failed = MODULE.compare_costs([summary("tree", "FAIL", 0.1), baseline], plan())[0]
    assert failed["observed_price_reduction_fraction"] == 0.9
    assert not failed["lower_cost_at_required_quality"]
    missing = MODULE.compare_costs([summary("tree", "PASS", 0.1, False), baseline], plan())[0]
    assert missing["observed_price_reduction_fraction"] is None
    assert not missing["lower_cost_at_required_quality"]
    assert MODULE.compare_costs([summary("tree", "PASS", 0.1), baseline], plan())[0][
        "lower_cost_at_required_quality"
    ]


def test_concurrent_dispatches_respect_shared_request_pacing(tmp_path: Path) -> None:
    timestamps = []

    class Paced:
        async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
            timestamps.append(time.monotonic())
            return completion()

    async def run() -> None:
        settings = plan()
        settings["limits"]["minimum_request_interval_s"] = 0.03
        ledger = MODULE.Ledger(Paced(), settings, tmp_path / "calls.jsonl")
        request = ProviderRequest("synthesis", "JSON", "unit")
        await asyncio.gather(ledger.complete(request, scope()), ledger.complete(request, scope()))
        assert timestamps[1] - timestamps[0] >= 0.025

    asyncio.run(run())


def test_quota_diagnostics_exclude_provider_prose_and_project_ids() -> None:
    body = {
        "message": "PRIVATE_KEY",
        "details": [
            {
                "violations": [
                    {
                        "quotaId": "GenerateRequestsPerMinutePerProject-FreeTier",
                        "quotaValue": "15",
                        "quotaDimensions": {"project": "PRIVATE_PROJECT"},
                    }
                ]
            },
            {"retryDelay": "12.5s"},
        ],
    }
    error = RateLimitError(
        "PRIVATE_KEY",
        body=body,
        response=httpx.Response(429, request=httpx.Request("POST", "https://unit.invalid")),
    )
    result = MODULE.quota_details(error)
    assert result == {"quotas": [{"kind": "requests_per_minute", "limit": 15}], "retry_after_s": [12.5]}
    assert "PRIVATE" not in json.dumps(result)


CONTINUATION_SPEC = importlib.util.spec_from_file_location(
    "unit_continuation",
    Path(__file__).resolve().parents[2] / "evaluations/held_out/continue_comparison.py",
)
CONTINUATION = importlib.util.module_from_spec(CONTINUATION_SPEC)
sys.modules[CONTINUATION_SPEC.name] = CONTINUATION
CONTINUATION_SPEC.loader.exec_module(CONTINUATION)


def test_continuation_excludes_every_dispatched_query_and_completed_answer() -> None:
    def row(query_id: str, status: str = "error", error: str = "RunStopped") -> dict:
        return dict(scope(), query_id=query_id, status=status, error_type=error)

    report = {
        "query_results": [
            row("blocked"),
            row("sent", error="ProviderError"),
            row("done", "answered"),
            row("routed"),
            row("invalid", "invalid_citations"),
        ],
        "calls": [dict(row("sent"), operation="synthesis"), dict(row("routed"), operation="tree_navigation")],
    }
    before = json.dumps(report)
    assert CONTINUATION.pending_rows(report) == [0]
    assert json.dumps(report) == before


def test_continuation_dispatch_identity_includes_trial_and_strategy() -> None:
    base = dict(scope(), query_id="same", status="error", error_type="RunStopped")
    report = {
        "query_results": [base, dict(base, trial=2), dict(base, strategy="full_history")],
        "calls": [dict(base, operation="synthesis")],
    }
    assert CONTINUATION.pending_rows(report) == [1, 2]


@pytest.mark.parametrize("phase", ["initial_index", "update_index", "query", "unchanged_index"])
def test_recovery_ledger_charges_without_mutating_original_scope(phase: str) -> None:
    class Recorder:
        calls = []

        async def complete(self, request: ProviderRequest, current: dict) -> dict:
            return current

    original = dict(scope(), phase=phase)
    ledger = CONTINUATION.RecoveryLedger(Recorder())
    current = asyncio.run(ledger.complete(ProviderRequest("indexing", "unit", "unit"), original))
    assert original["phase"] == phase
    assert current["phase"] == ("recovery_" + phase if phase in ("initial_index", "update_index") else phase)
    assert ledger.calls is ledger.inner.calls


def test_continuation_keeps_parent_failures_in_budget(tmp_path: Path) -> None:
    async def run() -> None:
        settings = plan()
        settings["limits"]["max_calls"] = 1
        ledger = MODULE.Ledger(Adapter(), settings, tmp_path / "calls.jsonl")
        failed = dict(
            scope(),
            status="error",
            input_tokens=None,
            output_tokens=None,
            reserved_input_tokens=1000,
            reserved_output_tokens=128,
        )
        ledger.calls.append(failed)
        with pytest.raises(MODULE.RunStopped):
            await ledger.complete(ProviderRequest("synthesis", "unit", "unit"), scope())
        assert ledger.calls == [failed]
        assert ledger.reserved()[0] == 1128
        assert MODULE.usage_summary(ledger.calls, ledger)["usage_unknown"]

    asyncio.run(run())


def test_frozen_harness_rebuilds_history_without_answer_calls(tmp_path: Path) -> None:
    class IndexOnly:
        async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
            assert request.operation == "indexing"
            return completion('{"title":"Unit","summary":"A small development conversation."}')

    async def run() -> None:
        settings = plan()
        history = {
            "history_label": "invented",
            "messages": [
                {"role": "user", "content": "The unit color is blue.", "source_id": "unit"},
                {"role": "user", "content": "The unit shape is round.", "source_id": "unit"},
            ],
        }
        ledger = MODULE.Ledger(IndexOnly(), settings, tmp_path / "calls.jsonl")
        report = {"histories": [], "query_results": []}
        await MODULE.run_history(
            history,
            [],
            1,
            tmp_path,
            CONTINUATION.RecoveryLedger(ledger),
            settings,
            report,
            lambda *_: 0.0,
            lambda: None,
        )
        assert report["query_results"] == []
        lifecycle = report["histories"][0]
        assert lifecycle["reopen_preserved_originals"] and lifecycle["unchanged_index_calls"] == 0
        assert {c["phase"] for c in ledger.calls} == {"recovery_initial_index", "recovery_update_index"}
        assert all(c["status"] == "ok" for c in ledger.calls)

    asyncio.run(run())
