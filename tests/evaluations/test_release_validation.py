"""Release-runner regressions use invented data; no held-out labels or network."""

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from cci.provider import ProviderRequest
from openai import RateLimitError
from openai.types.chat import ChatCompletion

SPEC = importlib.util.spec_from_file_location(
    "unit_release_validation",
    Path(__file__).resolve().parents[2] / "evaluations/held_out/release_validation.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def plan() -> dict:
    return {
        "schema_version": 1,
        "kind": "native",
        "split": "development",
        "trials": 1,
        "fixture": "invented.json",
        "fixture_sha256": "0" * 64,
        "wheel_sha256": "0" * 64,
        "source_hashes": {},
        "profile": {
            "provider": "unit",
            "requested_model": "unit-model",
            "endpoint": "http://localhost/v1",
            "protocol": "openai_chat_completions",
            "token_limit_field": "max_tokens",
            "response_format": "json_object",
        },
        "resolved_models": ["unit-model"],
        "allocation_id": "unit-001",
        "allocation_book_sha256": "0" * 64,
        "limits": {
            "concurrency": 2,
            "max_calls": 100,
            "max_reserved_tokens": 500_000,
            "max_estimated_usd": 1,
            "run_timeout_s": 60,
            "call_timeout_s": 1,
            "max_output_tokens_per_call": 128,
            "max_input_bytes_per_call": 100_000,
            "minimum_request_interval_s": 0,
        },
        "pricing": {"input_usd_per_million": 0.3, "output_usd_per_million": 2.5},
        "package_config": {
            "cache_backend": "none",
            "max_provider_attempts_per_op": 1,
            "per_provider_concurrency": 1,
            "target_chunk_size_scalars": 100,
            "max_evidence_text_scalars": 4000,
        },
    }


def completion(
    text: str = "{}", *, usage: bool = True, model: str = "unit-model", output_tokens: int = 4
) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "unit",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": output_tokens,
                "total_tokens": 12 + output_tokens,
            }
            if usage
            else None,
        }
    )


class Adapter:
    def __init__(self, text: str = "{}") -> None:
        self.text = text
        self.requests: list[ProviderRequest] = []

    async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
        self.requests.append(request)
        return completion(self.text)


def case() -> object:
    return MODULE.Case(
        trial=1,
        history="unit",
        mode="auto",
        query_id="q1",
        answerable=True,
        status="answered",
        answer="The Norwegian capital.",
        citations=["e1"],
        evidence=[
            {
                "evidence_id": "e1",
                "message_id": "original-m1",
                "seq": 1,
                "source_pointer": "/content",
                "excerpt": "The target is Oslo.",
            }
        ],
        evidence_recall=1.0,
        citations_valid=True,
    )


def query() -> dict:
    return {
        "query_id": "q1",
        "query_text": "Where is the target?",
        "history_label": "unit",
        "split": "development",
        "answerable": True,
        "required_evidence_units": [],
        "answer_rubric": {"correct_value": "Oslo", "must_not_cite_values": ["Berlin"]},
    }


def ledger(tmp_path: Path, adapter: object, settings: dict | None = None) -> object:
    return MODULE.ReleaseLedger(
        adapter, MODULE.Plan.model_validate(settings or plan()), tmp_path / "calls.jsonl"
    )


def test_valid_id_with_unsupported_excerpt_fails_semantic_support(tmp_path: Path) -> None:
    row = case()
    adapter = Adapter(
        json.dumps({"correct": True, "citations": [{"evidence_id": "e1", "supports_claim": False}]})
    )
    asyncio.run(MODULE.review_case(row, query(), ledger(tmp_path, adapter)))
    summary = MODULE.summarize([row], real_provider=True)
    assert summary["answer_correctness"] == 1
    assert summary["citation_validity"] == 1
    assert summary["citation_support"] == 0
    assert not summary["quality_pass"]
    payload = json.loads(adapter.requests[0].evidence_context)
    assert payload["citations"][0]["evidence_id"] == "e1"
    assert payload["citations"][0]["message_id"] == "original-m1"


def test_unreviewed_answer_is_incomplete_and_never_correct() -> None:
    summary = MODULE.summarize([case()], real_provider=True)
    assert summary["answer_correctness"] == 0
    assert summary["citation_support"] is None
    assert not summary["execution_complete"]
    assert not summary["quality_pass"]


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"evidence_id": "wrong", "supports_claim": True}],
        [{"evidence_id": "e1", "supports_claim": True}, {"evidence_id": "e1", "supports_claim": True}],
    ],
)
def test_malformed_judge_cannot_pass(entries: list, tmp_path: Path) -> None:
    row = case()
    asyncio.run(
        MODULE.review_case(
            row, query(), ledger(tmp_path, Adapter(json.dumps({"correct": True, "citations": entries})))
        )
    )
    assert row.review["status"] == "error"
    assert not MODULE.summarize([row], real_provider=True)["execution_complete"]


@pytest.mark.parametrize("correct", [True, False])
def test_review_decides_paraphrases_and_stale_values_not_substrings(correct: bool, tmp_path: Path) -> None:
    row = case()
    row.answer = "The Norwegian capital." if correct else "Berlin."
    asyncio.run(
        MODULE.review_case(
            row,
            query(),
            ledger(
                tmp_path,
                Adapter(
                    json.dumps(
                        {"correct": correct, "citations": [{"evidence_id": "e1", "supports_claim": correct}]}
                    )
                ),
            ),
        )
    )
    assert MODULE.summarize([row], real_provider=True)["answer_correctness"] == int(correct)


@pytest.mark.parametrize(
    "status,abstentions", [("partial", 0), ("error", 0), ("not_run", 0), ("insufficient_evidence", 1)]
)
def test_only_native_abstention_counts(status: str, abstentions: int) -> None:
    row = MODULE.Case(
        trial=1, history="unit", mode="auto", query_id="absent", answerable=False, status=status
    )
    summary = MODULE.summarize([row], real_provider=True)
    assert summary["correct_abstentions"] == abstentions
    assert summary["citation_support"] is None
    assert not summary["quality_pass"]


def test_fake_execution_cannot_close_quality_gate() -> None:
    row = case()
    row.review = {
        "status": "ok",
        "correct": True,
        "citations": [{"evidence_id": "e1", "supports_claim": True}],
    }
    rows = [row] + [
        MODULE.Case(
            trial=1,
            history="unit",
            mode="auto",
            query_id=f"a{i}",
            answerable=False,
            status="insufficient_evidence",
        )
        for i in range(8)
    ]
    assert MODULE.summarize(rows, real_provider=True)["quality_pass"]
    assert not MODULE.summarize(rows, real_provider=False)["quality_pass"]


@pytest.mark.parametrize("failure", ["quota", "usage", "output_cap", "model"])
def test_provider_failure_stops_all_later_dispatch(failure: str, tmp_path: Path) -> None:
    class Failing(Adapter):
        async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
            self.requests.append(request)
            if failure == "quota":
                response = httpx.Response(429, request=httpx.Request("POST", "http://localhost"))
                raise RateLimitError("SECRET ACCOUNT BODY", response=response, body={"message": "SECRET"})
            return completion(
                usage=failure != "usage",
                model="wrong" if failure == "model" else "unit-model",
                output_tokens=129 if failure == "output_cap" else 4,
            )

    async def run() -> None:
        adapter = Failing()
        account = ledger(tmp_path, adapter)
        request = ProviderRequest("synthesis", "JSON", "unit")
        for _ in range(2):
            with pytest.raises((MODULE.RunStopped, MODULE.ProviderError)):
                await account.complete(request, {"phase": "unit"})
        assert len(adapter.requests) == len(account.calls) == 1
        assert account.reserved()[0] > 0
        assert "SECRET" not in account.journal.read_text()
        if failure == "model":
            assert account.calls[0]["input_tokens"] == 12

    asyncio.run(run())


def test_concurrent_call_budget_is_shared(tmp_path: Path) -> None:
    async def run() -> None:
        settings = plan()
        settings["limits"]["max_calls"] = 1
        adapter = Adapter()
        account = ledger(tmp_path, adapter, settings)
        results = await asyncio.gather(
            *[account.complete(ProviderRequest("synthesis", "JSON", "unit"), {}) for _ in range(3)],
            return_exceptions=True,
        )
        assert len(adapter.requests) == 1
        assert sum(isinstance(item, MODULE.RunStopped) for item in results) == 2

    asyncio.run(run())


def fixture() -> dict:
    q = query()
    q["answer_rubric"]["correct_value"] = "REVIEW_ONLY_LABEL"
    return {
        "histories": [
            {
                "history_label": "unit",
                "split": "development",
                "messages": [{"role": "user", "content": "The target is Oslo.", "source_id": "unit"}],
            }
        ],
        "queries": [q],
    }


class NativeAdapter(Adapter):
    async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
        self.requests.append(request)
        if request.operation == "rubric_review":
            payload = json.loads(request.evidence_context)
            value = {
                "correct": True,
                "citations": [
                    {"evidence_id": c["evidence_id"], "supports_claim": True} for c in payload["citations"]
                ],
            }
        elif request.operation == "indexing":
            value = {"title": "Target", "summary": "Target is Oslo."}
        elif request.operation == "tree_navigation":
            ids = re.findall(r"<<<CCI_EVIDENCE id=(\S+)", request.evidence_context)
            value = {"node_ids": ids}
        else:
            ids = re.findall(r"<<<CCI_EVIDENCE id=(\S+)", request.evidence_context)
            value = {"answer": "Oslo", "citations": ids[:1]}
        return completion(json.dumps(value))


def test_native_run_preserves_sources_and_separates_labels_and_review(tmp_path: Path) -> None:
    adapter = NativeAdapter()
    report = asyncio.run(
        MODULE.execute(MODULE.Plan.model_validate(plan()), fixture(), adapter, tmp_path, real_provider=False)
    )
    assert report["status"] == "COMPLETE"
    assert not report["quality_pass"]
    assert len(report["query_results"]) == 2
    assert {r["mode"] for r in report["query_results"]} == {"lexical", "auto"}
    for row in report["query_results"]:
        assert row["status"] == "answered" and row["review"]["correct"]
        assert row["evidence"][0]["message_id"] != row["evidence"][0]["evidence_id"]
    operations = [r.operation for r in adapter.requests]
    assert operations[-2:] == ["rubric_review", "rubric_review"]
    assert "rubric_review" not in operations[:-2]
    for request in adapter.requests:
        assert ("REVIEW_ONLY_LABEL" in request.evidence_context) == (request.operation == "rubric_review")
    assert report["usage"]["calls"] == len(adapter.requests)
    assert report["usage"]["input_tokens_observed"] == 12 * len(adapter.requests)


@pytest.mark.parametrize("cancel", [False, True])
def test_interruption_checkpoints_all_slots_and_unknown_usage(cancel: bool, tmp_path: Path) -> None:
    async def run() -> None:
        entered = asyncio.Event()

        class Hanging(Adapter):
            async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
                entered.set()
                await asyncio.Event().wait()
                return completion()

        settings = plan()
        settings["limits"]["run_timeout_s"] = 0.05
        task = asyncio.create_task(
            MODULE.execute(
                MODULE.Plan.model_validate(settings), fixture(), Hanging(), tmp_path, real_provider=False
            )
        )
        await entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        report = json.loads((tmp_path / "report.json").read_text())
        assert report["status"] == "INCOMPLETE" and not report["quality_pass"]
        assert len(report["query_results"]) == 2
        assert report["usage"]["usage_unknown"] and report["usage"]["calls"] == 1
        assert report["reserved_tokens"] > 128
        events = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        assert events[0]["status"] == "pending" and events[-1]["status"] == "error"

    asyncio.run(run())


@pytest.mark.parametrize("field", ["max_reserved_tokens", "max_estimated_usd"])
def test_token_and_money_limit_prevent_dispatch(field: str, tmp_path: Path) -> None:
    settings = plan()
    settings["limits"][field] = 1 if field == "max_reserved_tokens" else 0.0000001
    adapter = Adapter()
    account = ledger(tmp_path, adapter, settings)
    with pytest.raises(MODULE.RunStopped):
        asyncio.run(account.complete(ProviderRequest("synthesis", "JSON", "unit"), {}))
    assert not adapter.requests


def test_pin_checks_reject_changed_wheel_profile_and_evaluator(tmp_path: Path) -> None:
    wheel = tmp_path / "test.whl"
    wheel.write_bytes(b"artifact")
    settings = plan()
    settings.update(
        wheel_sha256=MODULE.comparison.digest(wheel),
        fixture="evaluations/fixtures/live-smoke.json",
        fixture_sha256=MODULE.comparison.digest(MODULE.ROOT / "evaluations/fixtures/live-smoke.json"),
        source_hashes={p: MODULE.comparison.digest(MODULE.ROOT / p) for p in MODULE.FROZEN_SOURCES},
    )
    protocol = MODULE.Plan.model_validate(settings)
    MODULE.verify_pins(protocol, wheel, settings["profile"])
    for key in ("endpoint", "requested_model", "token_limit_field", "response_format"):
        with pytest.raises(ValueError, match="profile"):
            MODULE.verify_pins(protocol, wheel, {**settings["profile"], key: "changed"})
    wheel.write_bytes(b"different")
    with pytest.raises(ValueError, match="wheel"):
        MODULE.verify_pins(protocol, wheel, settings["profile"])
    wheel.write_bytes(b"artifact")
    settings["source_hashes"][MODULE.FROZEN_SOURCES[0]] = "0" * 64
    with pytest.raises(ValueError, match="evaluator"):
        MODULE.verify_pins(MODULE.Plan.model_validate(settings), wheel, settings["profile"])


def test_allocation_cannot_be_reused_or_oversubscribed(tmp_path: Path) -> None:
    settings = plan()
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {"accounting": {"remaining": {"calls": 100, "reserved_tokens": 500_000, "estimated_usd": 1}}}
        )
    )
    book = {
        "baseline_sha256": MODULE.comparison.digest(baseline),
        "allocations": {
            "unit-001": {
                "max_calls": 100,
                "max_reserved_tokens": 500_000,
                "max_estimated_usd": 1,
                "profile_sha256": MODULE.profile_digest(settings["profile"]),
                "pricing": settings["pricing"],
            }
        },
    }
    path = tmp_path / "allocations.json"
    path.write_text(json.dumps(book))
    settings["allocation_book_sha256"] = MODULE.comparison.digest(path)
    protocol = MODULE.Plan.model_validate(settings)
    claim = MODULE.verify_allocation(protocol, path)
    MODULE.claim_run(tmp_path / "run", claim, protocol)
    with pytest.raises(FileExistsError):
        MODULE.claim_run(tmp_path / "different-output", claim, protocol)
    with pytest.raises(FileExistsError):
        MODULE.claim_run(tmp_path / "run", tmp_path / "another-claim", protocol)
    book["allocations"]["unit-002"] = book["allocations"]["unit-001"]
    path.write_text(json.dumps(book))
    settings["allocation_book_sha256"] = MODULE.comparison.digest(path)
    with pytest.raises(ValueError, match="allowance"):
        MODULE.verify_allocation(MODULE.Plan.model_validate(settings), path)


def test_invalid_native_citations_are_not_sent_for_review(tmp_path: Path) -> None:
    row = case()
    row.citations = ["wrong"]
    adapter = Adapter()
    asyncio.run(MODULE.review_case(row, query(), ledger(tmp_path, adapter)))
    assert row.review["status"] == "error" and not adapter.requests


def test_cache_experiment_charges_physical_calls_and_keeps_synthesis_uncached(tmp_path: Path) -> None:
    settings = plan()
    settings.update(kind="cache", cache_backends=["none", "sqlite"])
    data = {
        "messages": [
            {"role": "user", "content": "The target is Oslo.", "source_id": "unit"},
            {"role": "user", "content": "Use CSV files.", "source_id": "unit"},
        ],
        "query": "Where is the target?",
        "changed_query": "Which city is the target?",
        "append_message": {"role": "user", "content": "Add a backup copy.", "source_id": "unit"},
        "required_source_seq": 1,
    }
    adapter = NativeAdapter()
    report = asyncio.run(
        MODULE.execute(MODULE.Plan.model_validate(settings), data, adapter, tmp_path, real_provider=False)
    )
    assert report["status"] == "COMPLETE", report
    assert len(report["cache_results"]) == 10
    sqlite = {r["phase"]: r for r in report["cache_results"] if r["backend"] == "sqlite"}
    assert sqlite["warm"]["physical_usage"]["calls"] == 1  # Native synthesis is always fresh.
    assert sqlite["cold"]["physical_usage"]["calls"] > 1
    assert sqlite["changed_query"]["physical_usage"]["calls"] > 1
    assert sqlite["append"]["physical_usage"]["calls"] > 1
    assert sqlite["cache_boundary_outage"]["physical_usage"]["calls"] > 1
    assert report["usage"]["calls"] == len(adapter.requests)
    assert not report["lower_cost_claim"] and not report["quality_pass"]


def test_execute_refuses_overwriting_prior_evidence(tmp_path: Path) -> None:
    (tmp_path / "calls.jsonl").write_text("historical\n")
    with pytest.raises(FileExistsError):
        asyncio.run(
            MODULE.execute(
                MODULE.Plan.model_validate(plan()), fixture(), Adapter(), tmp_path, real_provider=False
            )
        )
    assert (tmp_path / "calls.jsonl").read_text() == "historical\n"


def test_native_synthesis_repair_is_charged_separately(tmp_path: Path) -> None:
    class RepairAdapter(NativeAdapter):
        async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
            if request.operation == "synthesis" and not any(
                r.operation == "synthesis" for r in self.requests
            ):
                self.requests.append(request)
                return completion('{"answer":"Oslo","citations":["invalid"]}')
            return await super().completion(request, output_limit)

    report = asyncio.run(
        MODULE.execute(
            MODULE.Plan.model_validate(plan()), fixture(), RepairAdapter(), tmp_path, real_provider=False
        )
    )
    assert report["status"] == "COMPLETE"
    assert sum(c["operation"] == "synthesis" for c in report["calls"]) == 3
    assert all(c["input_tokens"] == 12 for c in report["calls"])


def test_bounded_smoke_keeps_original_fixture_assertions(tmp_path: Path) -> None:
    settings = plan()
    settings["kind"] = "smoke"
    data = MODULE.smoke.read_fixture(MODULE.ROOT / "evaluations/fixtures/live-smoke.json")
    report = asyncio.run(
        MODULE.execute(
            MODULE.Plan.model_validate(settings), data, NativeAdapter(), tmp_path, real_provider=False
        )
    )
    assert report["status"] == "COMPLETE", report
    assert report["checks"]["expected_original_evidence_retrieved"]
    assert report["checks"]["reopen_preserved_originals"]
    assert report["checks"]["unchanged_index_calls"] == 0
    assert report["usage"]["calls"] > 0 and not report["usage"]["usage_unknown"]


def test_quota_interruption_of_native_run_cannot_shrink_denominators(tmp_path: Path) -> None:
    class Quota(Adapter):
        async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion:
            self.requests.append(request)
            raise RateLimitError(
                "PRIVATE",
                body={},
                response=httpx.Response(429, request=httpx.Request("POST", "http://localhost")),
            )

    adapter = Quota()
    report = asyncio.run(
        MODULE.execute(MODULE.Plan.model_validate(plan()), fixture(), adapter, tmp_path, real_provider=False)
    )
    assert report["status"] == "INCOMPLETE" and not report["quality_pass"]
    assert len(report["query_results"]) == 2 and len(adapter.requests) == 1
    assert report["usage"]["usage_unknown"]
    assert all(m["expected_queries"] == 1 for m in report["summaries"][0]["modes"].values())


def test_unknown_provider_capacity_and_changed_profile_are_not_ready(tmp_path: Path) -> None:
    protocol = MODULE.Plan.model_validate(plan())
    readiness = tmp_path / "provider-readiness.json"
    readiness.write_text(
        json.dumps({"status": "BLOCKED", "profile_sha256": MODULE.profile_digest(protocol.profile)})
    )
    book = tmp_path / "allocations.json"
    book.write_text(json.dumps({"provider_readiness_sha256": MODULE.comparison.digest(readiness)}))
    assert not MODULE.provider_ready(protocol, book)
    readiness.write_text(json.dumps({"status": "AVAILABLE", "profile_sha256": "changed"}))
    with pytest.raises(ValueError, match="record"):
        MODULE.provider_ready(protocol, book)
    book.write_text(json.dumps({"provider_readiness_sha256": MODULE.comparison.digest(readiness)}))
    with pytest.raises(ValueError, match="profile"):
        MODULE.provider_ready(protocol, book)
