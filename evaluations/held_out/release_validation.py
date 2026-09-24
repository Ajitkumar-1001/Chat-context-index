"""Bounded release validation, composing the unchanged historical evaluators.

Run from an installed wheel with Python -I. The default CLI action is a zero-request
preflight. Execution consumes one named, exclusive allocation from allocations.json.
Native answers are generated before labels reach a separate reviewer.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Literal, Protocol, TypedDict, cast

from cci.ask import ask
from cci.cache import CachedValue
from cci.config import Config
from cci.errors import CciError
from cci.errors import ProviderError as ProviderError
from cci.provider import MemoizedProvider, ProviderRequest, ProviderResponse
from cci.store import HistoryStore
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

ROOT = Path(__file__).resolve().parents[2]
FROZEN_SOURCES = (
    "evaluations/held_out/comparison.py",
    "evaluations/held_out/run_evaluation.py",
    "evaluations/live_smoke.py",
    "evaluations/held_out/release_validation.py",
)


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load evaluator: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


comparison = load_module("release_comparison", ROOT / FROZEN_SOURCES[0])
scoring = load_module("release_scoring", ROOT / FROZEN_SOURCES[1])
smoke = scoring._model_helpers()
RunStopped = comparison.RunStopped


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class Limits(StrictModel):
    max_calls: int = Field(gt=0)
    max_reserved_tokens: int = Field(gt=0)
    max_estimated_usd: float = Field(gt=0)
    max_output_tokens_per_call: int = Field(gt=0)
    max_input_bytes_per_call: int = Field(gt=0)
    call_timeout_s: float = Field(gt=0)
    run_timeout_s: float = Field(gt=0)
    concurrency: int = Field(gt=0)
    minimum_request_interval_s: float = Field(ge=0)


class Pricing(StrictModel):
    input_usd_per_million: float = Field(ge=0)
    output_usd_per_million: float = Field(ge=0)


class Plan(StrictModel):
    schema_version: Literal[1]
    kind: Literal["native", "smoke", "cache"]
    split: Literal["development", "held_out"]
    trials: int = Field(ge=1)
    fixture: str
    fixture_sha256: str
    wheel_sha256: str
    source_hashes: dict[str, str]
    profile: dict[str, str]
    resolved_models: list[str]
    allocation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    allocation_book_sha256: str
    limits: Limits
    pricing: Pricing
    package_config: dict[str, object]
    cache_backends: list[Literal["none", "sqlite", "redis"]] = Field(
        default_factory=lambda: cast(list[Literal["none", "sqlite", "redis"]], ["none", "sqlite", "redis"]),
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_protocol(self) -> Plan:
        if not self.resolved_models or any(not model for model in self.resolved_models):
            raise ValueError("resolved model allowlist is required")
        config = self.config()
        config.validate()
        if config.max_provider_attempts_per_op != 1 or config.cache_backend != "none":
            raise ValueError("evaluation requires one attempt per operation and isolated caches")
        if self.kind != "native" and (self.split != "development" or self.trials != 1):
            raise ValueError("smoke/cache use one development trial")
        if self.split == "held_out" and self.trials != 3:
            raise ValueError("release quality requires three trials")
        if len(set(self.cache_backends)) != len(self.cache_backends):
            raise ValueError("duplicate cache backends")
        return self

    def config(self) -> Config:
        return TypeAdapter(Config).validate_python(self.package_config)


class Adapter(Protocol):
    async def completion(self, request: ProviderRequest, output_limit: int) -> ChatCompletion: ...


class ReleaseLedger(comparison.Ledger):  # type: ignore[name-defined]  # Frozen module loaded under Python -I.
    """Reuse admission/accounting; add resolved-model checks and fail-closed timeouts."""

    def __init__(self, adapter: Adapter, plan: Plan, journal: Path) -> None:
        super().__init__(adapter, plan.model_dump(), journal)
        self.resolved_models = set(plan.resolved_models)

    def event(self, call: dict[str, object]) -> None:
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(call) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    async def complete(self, request: ProviderRequest, scope: dict[str, object]) -> ProviderResponse:
        start = len(self.calls)
        try:
            response = await super().complete(request, scope)
            for call in self.calls[start:]:
                if call["status"] == "ok" and call["resolved_model"] not in self.resolved_models:
                    self.stopped = "resolved_model_changed"
                    call.update(status="error", error_type="ResolvedModelMismatch")
                    self.event(call)
                    raise RunStopped(self.stopped)
            return cast(ProviderResponse, response)
        except (TimeoutError, asyncio.CancelledError):
            self.stopped = self.stopped or "request_interrupted_usage_unknown"
            raise
        finally:
            # The frozen ledger rejects zero/negative usage; retain its full reservation too.
            for call in self.calls[start:]:
                for key in ("input_tokens", "output_tokens"):
                    if call[key] is not None and (type(call[key]) is not int or call[key] <= 0):
                        call[key] = None
                        self.event(call)


class Evidence(TypedDict):
    evidence_id: str
    message_id: str
    seq: int
    source_pointer: str
    excerpt: str


class CitationReview(TypedDict):
    evidence_id: str
    supports_claim: bool


class Review(TypedDict, total=False):
    status: str
    correct: bool
    citations: list[CitationReview]
    error_type: str


class Query(TypedDict):
    query_id: str
    query_text: str
    history_label: str
    split: str
    answerable: bool
    required_evidence_units: list[dict[str, object]]
    answer_rubric: dict[str, object] | None


@dataclass(slots=True)
class Case:
    trial: int
    history: str
    mode: str
    query_id: str
    answerable: bool
    status: str = "not_run"
    answer: str | None = None
    citations: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    evidence_recall: float = 0.0
    citations_valid: bool = False
    review: Review = field(default_factory=lambda: Review(status="not_reviewed"))
    error_type: str | None = None
    native_result: dict[str, object] = field(default_factory=dict)

    def scope(self, phase: str) -> dict[str, object]:
        return {
            "trial": self.trial,
            "history": self.history,
            "strategy": self.mode,
            "query_id": self.query_id,
            "phase": phase,
        }


async def review_case(row: Case, query: Query, ledger: ReleaseLedger) -> None:
    if row.status != "answered":
        row.review = {"status": "not_needed"}
        return
    evidence = {item["evidence_id"]: item for item in row.evidence}
    if (
        not row.citations_valid
        or not row.citations
        or len(evidence) != len(row.evidence)
        or len(set(row.citations)) != len(row.citations)
        or not set(row.citations) <= evidence.keys()
    ):
        row.review = {"status": "error", "error_type": "InvalidNativeCitations"}
        return
    payload = {
        "question": query["query_text"],
        "answer": row.answer,
        "rubric": query["answer_rubric"],
        "answerable": query["answerable"],
        "citations": [evidence[cid] for cid in row.citations],
    }
    try:
        response = await ledger.complete(
            ProviderRequest("rubric_review", comparison.REVIEW_PROMPT, json.dumps(payload)),
            row.scope("review"),
        )
        row.review = {"status": "ok", **comparison.parse_review(response.text, row.citations)}
    except (CciError, RunStopped, TimeoutError, ValueError) as exc:
        row.review = {"status": "error", "error_type": type(exc).__name__}


def summarize(rows: list[Case], *, real_provider: bool) -> dict[str, object]:
    """Rows include every preregistered slot, including failures and unattempted cases."""
    answered = [row for row in rows if row.status == "answered"]
    answerable = [row for row in rows if row.answerable]
    absent = [row for row in rows if not row.answerable]
    complete = bool(rows) and all(
        row.status == "insufficient_evidence" or (row.status == "answered" and row.review["status"] == "ok")
        for row in rows
    )
    reviewed = [row for row in answered if row.review["status"] == "ok"]
    citation_count = sum(len(row.citations) for row in answered)
    support = None
    if citation_count and len(reviewed) == len(answered):
        support = (
            sum(c["supports_claim"] for row in reviewed for c in row.review["citations"]) / citation_count
        )
    validity = sum(row.citations_valid for row in answered) / len(answered) if answered else None
    recall = sum(row.evidence_recall for row in answerable) / len(answerable) if answerable else 0.0
    correctness = (
        sum(
            row.status == "answered"
            and row.citations_valid
            and row.review["status"] == "ok"
            and row.review["correct"]
            for row in answerable
        )
        / len(answerable)
        if answerable
        else 0.0
    )
    abstentions = sum(row.status == "insufficient_evidence" for row in absent)
    return {
        "expected_queries": len(rows),
        "answerable_queries": len(answerable),
        "absent_queries": len(absent),
        "execution_complete": complete,
        "evidence_recall": recall,
        "answer_correctness": correctness,
        "correct_abstentions": abstentions,
        "citation_validity": validity,
        "citation_support": support,
        "status_counts": {
            status: sum(row.status == status for row in rows)
            for status in sorted({row.status for row in rows})
        },
        "quality_pass": bool(
            real_provider
            and complete
            and recall >= 0.85
            and correctness >= 0.8
            and abstentions >= 7
            and validity == 1
            and support is not None
            and support >= 0.9
        ),
    }


def profile_digest(profile: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_pins(plan: Plan, wheel: Path, public_profile: dict[str, str]) -> None:
    if plan.profile != public_profile:
        raise ValueError("frozen_provider_profile_changed")
    if comparison.digest(wheel) != plan.wheel_sha256:
        raise ValueError("frozen_wheel_changed")
    if comparison.digest(ROOT / plan.fixture) != plan.fixture_sha256:
        raise ValueError("frozen_fixture_changed")
    if set(plan.source_hashes) != set(FROZEN_SOURCES):
        raise ValueError("all_evaluator_hashes_required")
    for name, expected in plan.source_hashes.items():
        if comparison.digest(ROOT / name) != expected:
            raise ValueError("frozen_evaluator_changed")


def verify_allocation(plan: Plan, book_path: Path) -> Path:
    """Disjoint envelopes share a single canonical book beside the audited baseline."""
    if book_path.name != "allocations.json" or comparison.digest(book_path) != plan.allocation_book_sha256:
        raise ValueError("frozen_allocation_book_changed")
    book = json.loads(book_path.read_text())
    baseline = book_path.with_name("baseline.json")
    if comparison.digest(baseline) != book["baseline_sha256"]:
        raise ValueError("accounting_baseline_changed")
    remaining = json.loads(baseline.read_text())["accounting"]["remaining"]
    allocations = book["allocations"]
    allocation = allocations[plan.allocation_id]
    if Pricing.model_validate(allocation["pricing"]) != plan.pricing:
        raise ValueError("allocation_pricing_changed")
    for limit, balance in (
        ("max_calls", "calls"),
        ("max_reserved_tokens", "reserved_tokens"),
        ("max_estimated_usd", "estimated_usd"),
    ):
        values = [item[limit] for item in allocations.values()]
        if (
            not values
            or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values)
            or sum(values) > remaining[balance]
        ):
            raise ValueError("allocations_exceed_remaining_allowance")
        if getattr(plan.limits, limit) > allocations[plan.allocation_id][limit]:
            raise ValueError("run_exceeds_named_allocation")
    if allocations[plan.allocation_id]["profile_sha256"] != profile_digest(plan.profile):
        raise ValueError("allocation_provider_profile_changed")
    return book_path.parent / "allocation-claims" / plan.allocation_id


def claim_run(output: Path, claim: Path, plan: Plan) -> None:
    """Exclusive mkdir protects even simultaneous invocations and differently named outputs."""
    if output.exists():
        raise FileExistsError("run_output_exists")
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.mkdir()  # Consumed even on interruption; reconciliation must retain unknown usage.
    comparison.write_report(
        claim / "claim.json",
        {
            "output": str(output.resolve()),
            "allocation_id": plan.allocation_id,
            "claimed_at_utc": datetime.now(UTC).isoformat(),
            "limits": plan.limits.model_dump(),
        },
    )
    output.mkdir(parents=True, exist_ok=False)


def provider_ready(plan: Plan, book_path: Path) -> bool:
    book = json.loads(book_path.read_text())
    path = book_path.with_name("provider-readiness.json")
    if comparison.digest(path) != book["provider_readiness_sha256"]:
        raise ValueError("provider_readiness_record_changed")
    record = json.loads(path.read_text())
    if record["profile_sha256"] != profile_digest(plan.profile):
        raise ValueError("provider_readiness_profile_changed")
    return record["status"] == "AVAILABLE"


def native_queries(fixture: dict[str, object], split: str) -> list[Query]:
    queries = [q for q in cast(list[Query], fixture["queries"]) if q["split"] == split]
    if not queries or len({q["query_id"] for q in queries}) != len(queries):
        raise ValueError("missing_or_duplicate_query_slots")
    if split == "held_out" and (len(queries) != 40 or sum(q["answerable"] for q in queries) != 32):
        raise ValueError("held_out_query_denominators_changed")
    return queries


async def native_case(
    store: HistoryStore, row: Case, query: Query, messages: list[dict[str, object]], ledger: ReleaseLedger
) -> None:
    try:
        result = await ask(
            store,
            query["query_text"],
            mode=row.mode,
            provider=MemoizedProvider(comparison.ScopedProvider(ledger, row.scope("query")), store.config),
        )
        row.native_result = asdict(result)
        row.status, row.answer = result.status, result.answer
        row.citations = [citation.evidence_id for citation in result.citations]
        row.evidence = [cast(Evidence, asdict(item)) for item in result.evidence]
        ids = {item.evidence_id for item in result.evidence}
        row.citations_valid = (
            bool(row.citations)
            and len(ids) == len(result.evidence)
            and len(set(row.citations)) == len(row.citations)
            and set(row.citations) <= ids
        )
        row.evidence_recall = scoring._score_evidence_recall(
            query["required_evidence_units"], messages, result.evidence
        )
    except (CciError, RunStopped, TimeoutError, ValueError) as exc:
        row.status, row.error_type = "error", type(exc).__name__


class UnavailableCache:
    """An explicit cache-boundary fault; never represented as a real Redis outage."""

    async def get(self, key: str, *, now: float) -> CachedValue | None:
        raise OSError("injected_cache_unavailable")

    async def set(self, key: str, value: CachedValue) -> None:
        raise OSError("injected_cache_unavailable")

    async def clear(self) -> None:
        raise OSError("injected_cache_unavailable")

    async def aclose(self) -> None:
        return None


async def cache_experiment(
    plan: Plan,
    fixture: dict[str, object],
    ledger: ReleaseLedger,
    directory: Path,
    results: list[object],
    checkpoint: Callable[[], None],
    redis_url: str | None,
) -> None:
    if "redis" in plan.cache_backends:
        if not redis_url:
            raise ValueError("cache_experiment_requires_explicit_redis_url")
        from redis.asyncio import from_url

        async with from_url(redis_url, socket_connect_timeout=1, socket_timeout=1) as client:
            async with asyncio.timeout(2):
                await client.ping()  # Require real Redis before any model spending.
    messages = cast(list[dict[str, object]], fixture["messages"])
    for backend in plan.cache_backends:
        config = replace(
            plan.config(),
            cache_backend=backend,
            redis_url=redis_url,
            application_namespace=f"release-cache-{directory.name}",
            tree_max_children=2,
            target_chunk_size_scalars=1,
        )
        async with await HistoryStore.open(str(directory / f"{backend}.db"), config=config) as store:
            await comparison.append_messages(store, messages)
            scope = {"phase": "initial_index", "strategy": backend}
            provider = MemoizedProvider(comparison.ScopedProvider(ledger, scope), config, cache=store.cache)
            await comparison.build_index(store, provider)
            checkpoint()
            for phase in ("cold", "warm", "changed_query", "append", "cache_boundary_outage"):
                if ledger.stopped:
                    raise RunStopped(ledger.stopped)
                scope["phase"] = phase
                start = len(ledger.calls)
                if phase == "append":
                    await comparison.append_messages(
                        store, [cast(dict[str, object], fixture["append_message"])], offset=len(messages)
                    )
                    await comparison.build_index(store, provider)
                if phase == "cache_boundary_outage":
                    provider.cache = UnavailableCache()
                query = str(fixture["changed_query"] if phase == "changed_query" else fixture["query"])
                result = await ask(store, query, provider=provider, mode="tree")
                required_seq = cast(int, fixture["required_source_seq"])
                original = (await store.get_messages(required_seq, required_seq))[0]
                if (
                    result.status != "answered"
                    or result.routing.actual_mode != "tree"
                    or not any(
                        e.message_id == original.message_id
                        and e.excerpt in original.original_payload["content"]
                        for e in result.evidence
                    )
                ):
                    raise ValueError("cache_experiment_original_evidence_missing")
                results.append(
                    {
                        "backend": backend,
                        "phase": phase,
                        "result": asdict(result),
                        "physical_usage": comparison.usage_summary(ledger.calls[start:], ledger),
                        "outage_method": "cache_protocol_fault" if phase == "cache_boundary_outage" else None,
                    }
                )
                checkpoint()


async def execute(
    plan: Plan,
    fixture: dict[str, object],
    adapter: Adapter,
    output: Path,
    *,
    real_provider: bool,
    provenance: dict[str, object] | None = None,
    redis_url: str | None = None,
) -> dict[str, object]:
    """Execute in a directory claimed by preflight. Cancellation saves evidence, then propagates."""
    if (output / "report.json").exists() or (output / "calls.jsonl").exists():
        raise FileExistsError("run_output_exists")
    ledger = ReleaseLedger(adapter, plan, output / "calls.jsonl")
    queries = native_queries(fixture, plan.split) if plan.kind == "native" else []
    rows = [
        Case(
            trial=trial,
            history=q["history_label"],
            mode=mode,
            query_id=q["query_id"],
            answerable=q["answerable"],
        )
        for trial in range(1, plan.trials + 1)
        for q in queries
        for mode in ("lexical", "auto")
    ]
    report: dict[str, object] = {
        "schema_version": 1,
        "evaluation": f"bounded_{plan.kind}",
        "status": "INCOMPLETE",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "plan": plan.model_dump(),
        "protocol_sha256": hashlib.sha256(plan.model_dump_json().encode()).hexdigest(),
        "provider_profile_sha256": profile_digest(plan.profile),
        "package": provenance,
        "real_provider": real_provider,
        "sdk_retries": 0,
        "histories": [],
        "checks": {},
        "cache_results": [],
        "human_review": "NOT_RUN",
        "lower_cost_claim": False,
    }

    def checkpoint() -> None:
        report.update(
            query_results=[asdict(row) for row in rows],
            calls=ledger.calls,
            usage=comparison.usage_summary(ledger.calls, ledger),
            reserved_tokens=ledger.reserved()[0],
            reserved_estimated_usd=ledger.reserved()[1],
            stopped_reason=ledger.stopped,
        )
        comparison.write_report(output / "report.json", report)

    checkpoint()
    try:
        async with asyncio.timeout(plan.limits.run_timeout_s):
            with tempfile.TemporaryDirectory(prefix="cci-release-eval-") as directory:
                if plan.kind == "smoke":
                    await smoke.exercise_fixture(
                        comparison.ScopedProvider(ledger, {"phase": "smoke"}),
                        fixture,
                        Path(directory) / "smoke.db",
                        report["checks"],
                    )
                elif plan.kind == "native":
                    histories = cast(list[dict[str, object]], fixture["histories"])
                    for trial in range(1, plan.trials + 1):
                        for number, history in enumerate(h for h in histories if h["split"] == plan.split):
                            if ledger.stopped:
                                raise RunStopped(ledger.stopped)
                            label = str(history["history_label"])
                            messages = cast(list[dict[str, object]], history["messages"])
                            db = Path(directory) / f"{trial}-{number}.db"
                            config = plan.config()
                            async with await HistoryStore.open(str(db), config=config) as store:
                                await comparison.append_messages(store, messages)
                                original_ids = [
                                    m.message_id for m in await store.get_messages(1, len(messages))
                                ]
                                provider = MemoizedProvider(
                                    comparison.ScopedProvider(
                                        ledger,
                                        {
                                            "trial": trial,
                                            "history": label,
                                            "phase": "index",
                                            "strategy": "shared",
                                        },
                                    ),
                                    config,
                                )
                                indexed = await comparison.build_index(store, provider)
                                cast(list[object], report["histories"]).append(
                                    {
                                        "trial": trial,
                                        "history": label,
                                        "index": indexed,
                                    }
                                )
                                checkpoint()
                            async with await HistoryStore.open(str(db), config=config) as store:
                                if original_ids != [
                                    m.message_id for m in await store.get_messages(1, len(messages))
                                ]:
                                    raise ValueError("original_identity_changed_after_reopen")
                                for row in (r for r in rows if r.trial == trial and r.history == label):
                                    if ledger.stopped:
                                        raise RunStopped(ledger.stopped)
                                    query = next(q for q in queries if q["query_id"] == row.query_id)
                                    await native_case(store, row, query, messages, ledger)
                                    checkpoint()
                    # Only this post-generation phase receives labels.
                    for row in rows:
                        if ledger.stopped:
                            raise RunStopped(ledger.stopped)
                        await review_case(
                            row, next(q for q in queries if q["query_id"] == row.query_id), ledger
                        )
                        checkpoint()
                else:
                    await cache_experiment(
                        plan,
                        fixture,
                        ledger,
                        Path(directory),
                        cast(list[object], report["cache_results"]),
                        checkpoint,
                        redis_url,
                    )
        report["status"] = "COMPLETE"
    except asyncio.CancelledError:
        ledger.stopped = ledger.stopped or "evaluation_cancelled"
        report["error_type"] = "CancelledError"
        raise
    except Exception as exc:
        # Boundary: persist partial output without logging exception messages/provider bodies.
        report["error_type"] = type(exc).__name__
    finally:
        summaries = []
        complete = True
        for trial in range(1, plan.trials + 1):
            modes = (
                {
                    mode: summarize(
                        [r for r in rows if r.trial == trial and r.mode == mode],
                        real_provider=real_provider and plan.split == "held_out",
                    )
                    for mode in ("lexical", "auto")
                }
                if rows
                else {}
            )
            if modes:
                complete = complete and all(bool(m["execution_complete"]) for m in modes.values())
                auto_not_worse = cast(float, modes["auto"]["evidence_recall"]) >= cast(
                    float, modes["lexical"]["evidence_recall"]
                )
                summaries.append(
                    {
                        "trial": trial,
                        "modes": modes,
                        "auto_recall_not_below_lexical": auto_not_worse,
                        "quality_pass": bool(modes["auto"]["quality_pass"] and auto_not_worse),
                    }
                )
        report["summaries"] = summaries
        if not complete or ledger.stopped or any(call["status"] != "ok" for call in ledger.calls):
            report["status"] = "INCOMPLETE"
        report["quality_pass"] = bool(
            report["status"] == "COMPLETE" and summaries and all(s["quality_pass"] for s in summaries)
        )
        report["completed_at_utc"] = datetime.now(UTC).isoformat()
        checkpoint()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--allocation-book", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--execute", action="store_true", help="Consume the named allocation; default is preflight"
    )
    args = parser.parse_args()
    try:
        plan = Plan.model_validate_json(args.plan.read_text())
        smoke.require(bool(sys.flags.isolated), "run_with_python_I")
        if args.env_file:
            smoke.require(args.env_file.is_file(), "env_file_missing")
            smoke.load_dotenv(args.env_file, override=False)
        settings = smoke.ModelSettings.from_env(os.environ)
        verify_pins(plan, args.wheel, settings.public_settings())
        provenance = smoke.package_provenance(args.wheel)
        if args.allocation_book.resolve() != ROOT / "evaluations/results/release-readiness/allocations.json":
            raise ValueError("use_canonical_allocation_book")
        claim = verify_allocation(plan, args.allocation_book)
        ready = provider_ready(plan, args.allocation_book)
        if args.out.exists() or claim.exists():
            raise FileExistsError("run_or_allocation_already_used")
        fixture = json.loads((ROOT / plan.fixture).read_text())
        if plan.kind == "native":
            native_queries(fixture, plan.split)
            if (
                plan.split == "held_out"
                and (ROOT / plan.fixture).resolve() != ROOT / "evaluations/held_out/fixture.json"
            ):
                raise ValueError("release_requires_original_held_out_fixture")
        elif plan.kind == "smoke":
            fixture = smoke.read_fixture(ROOT / plan.fixture)
        elif "redis" in plan.cache_backends and not os.environ.get("CCI_EVAL_REDIS_URL"):
            raise ValueError("cache_experiment_requires_explicit_redis_url")
        if not args.execute:
            print(
                json.dumps(
                    {
                        "status": "PREFLIGHT_PASS",
                        "execution_ready": ready,
                        "provider_calls": 0,
                        "package": provenance,
                    }
                )
            )
            return 0
        if not ready:
            raise ValueError("provider_capacity_not_verified")
        claim_run(args.out, claim, plan)

        async def run_live() -> dict[str, object]:
            async with settings.make_client(timeout_s=plan.limits.call_timeout_s) as client:
                adapter = smoke.ChatCompletionsProvider(
                    client, settings, max_output_tokens=plan.limits.max_output_tokens_per_call
                )
                return await execute(
                    plan,
                    fixture,
                    adapter,
                    args.out,
                    real_provider=True,
                    provenance=provenance,
                    redis_url=os.environ.get("CCI_EVAL_REDIS_URL"),
                )

        report = asyncio.run(run_live())
        print(json.dumps({key: report[key] for key in ("status", "quality_pass", "usage", "stopped_reason")}))
        passed = report["quality_pass"] if plan.split == "held_out" else report["status"] == "COMPLETE"
        return 0 if passed else 1
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
