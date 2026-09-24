"""Run the frozen comparison inside one exclusive canonical release allocation.

Default: zero-call, zero-write preflight. Execution requires --execute, Python -I,
an installed candidate wheel, and existing AVAILABLE capacity evidence. This wrapper
does not grant budget, change canonical records, resume a run, or alter the evaluator.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic import Field, model_validator

# Python -I does not suppress bytecode writes by dynamically loaded evaluators.
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "allocated_release_validation", ROOT / "evaluations/held_out/release_validation.py"
)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)

CANONICAL_BOOK = "evaluations/results/release-readiness/allocations.json"
FIXTURE = "evaluations/held_out/fixture.json"
INNER_SOURCES = runner.FROZEN_SOURCES[:3]
FROZEN_SOURCES = (*runner.FROZEN_SOURCES, "evaluations/run_allocated_comparison.py")
STRATEGIES = ("full_history", "recent_lexical", "tree")
QUALITY_GATES = {
    "evidence_recall": 0.85,
    "answer_correctness": 0.8,
    "abstention_count": 7,
    "citation_support": 0.9,
    "citation_validity": 1.0,
}
PROFILE_FIELDS = {
    "provider", "requested_model", "endpoint", "protocol", "token_limit_field", "response_format"
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Plan(runner.StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    comparison_plan: str
    comparison_plan_sha256: str
    wheel_sha256: str
    source_hashes: dict[str, str]
    profile: dict[str, str]
    resolved_models: list[str] = Field(min_length=1)
    allocation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    allocation_book_sha256: str
    limits: runner.Limits
    pricing: runner.Pricing

    @model_validator(mode="after")
    def validate_protocol(self):
        if set(self.profile) != PROFILE_FIELDS or any(not value for value in self.profile.values()):
            raise ValueError("complete_provider_profile_required")
        if any(not model for model in self.resolved_models):
            raise ValueError("resolved_model_allowlist_required")
        if len(set(self.resolved_models)) != len(self.resolved_models):
            raise ValueError("duplicate_resolved_model")
        if self.limits.concurrency != 1:
            raise ValueError("allocated_comparison_requires_serial_dispatch")
        minimum = (self.limits.max_calls - 1) * self.limits.minimum_request_interval_s
        if self.limits.run_timeout_s <= minimum + self.limits.call_timeout_s:
            raise ValueError("run_timeout_cannot_fit_frozen_dispatch_schedule")
        return self


@dataclass(frozen=True)
class Prepared:
    plan: Plan
    plan_path: Path
    plan_sha256: str
    wheel: Path
    book: Path
    output: Path
    settings: object
    provenance: dict
    inner_plan: dict
    claim: Path
    ready: bool


def require_isolated() -> None:
    if not sys.flags.isolated:
        raise ValueError("run_with_python_I")


def verify_inner(plan: Plan) -> dict:
    path = ROOT / plan.comparison_plan
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != plan.comparison_plan_sha256:
        raise ValueError("frozen_comparison_plan_changed")
    inner = json.loads(raw)
    if inner["limits"] != plan.limits.model_dump():
        raise ValueError("inner_comparison_limits_differ")
    if any(inner["pricing"].get(key) != value for key, value in plan.pricing.model_dump().items()):
        raise ValueError("inner_comparison_pricing_differs")
    if inner["provider"] != plan.profile["provider"] or inner["model"] != plan.profile["requested_model"]:
        raise ValueError("inner_comparison_profile_differs")
    if inner["wheel_sha256"] != plan.wheel_sha256 or inner["fixture_sha256"] != digest(ROOT / FIXTURE):
        raise ValueError("frozen_comparison_fixture_or_wheel_changed")
    if set(inner["source_hashes"]) != set(INNER_SOURCES) or any(
        expected != plan.source_hashes[name] for name, expected in inner["source_hashes"].items()
    ):
        raise ValueError("inner_evaluator_hashes_differ")
    if (
        inner["trials"] != 3
        or inner["strategies"] != list(STRATEGIES)
        or inner["quality_gates"] != QUALITY_GATES
        or inner["comparison_max_quality_loss"] != 0.05
        or inner["initial_history_percent"] != 80
        or inner["context"] != {
            "recent_messages": 4, "max_messages": 8, "max_chars": 4000, "excerpt_chars": 200
        }
    ):
        raise ValueError("comparison_release_protocol_changed")
    config = runner.TypeAdapter(runner.Config).validate_python(inner["package_config"])
    config.validate()
    if config.cache_backend != "none" or config.max_provider_attempts_per_op != 1:
        raise ValueError("comparison_requires_isolated_cache_and_no_retries")
    return inner


def prepare(plan_path: Path, wheel: Path, book: Path, output: Path, settings: object) -> Prepared:
    """Validate identities and canonical balances without a claim, output, or client."""
    require_isolated()
    raw = plan_path.read_bytes()
    plan = Plan.model_validate_json(raw)
    if settings.public_settings() != plan.profile:
        raise ValueError("frozen_provider_profile_changed")
    if digest(wheel) != plan.wheel_sha256:
        raise ValueError("frozen_wheel_changed")
    if set(plan.source_hashes) != set(FROZEN_SOURCES):
        raise ValueError("all_evaluator_and_wrapper_hashes_required")
    if any(digest(ROOT / name) != expected for name, expected in plan.source_hashes.items()):
        raise ValueError("frozen_evaluator_or_wrapper_changed")
    inner_plan = verify_inner(plan)
    provenance = runner.smoke.package_provenance(wheel)
    if book.resolve() != (ROOT / CANONICAL_BOOK).resolve():
        raise ValueError("use_canonical_allocation_book")
    book = (ROOT / CANONICAL_BOOK).resolve()
    claim = runner.verify_allocation(plan, book)
    ready = runner.provider_ready(plan, book)
    if output.exists() or claim.exists():
        raise FileExistsError("run_or_allocation_already_used")
    return Prepared(plan, plan_path, hashlib.sha256(raw).hexdigest(), wheel, book, output,
                    settings, provenance, inner_plan, claim, ready)


class ProviderProfileChanged(runner.RunStopped):
    pass


def verify_transport(adapter: object, plan: Plan) -> None:
    """Check the actual SDK transport as well as its immutable public settings."""
    try:
        client = adapter.client
        timeout = client.timeout
        components = [timeout] if isinstance(timeout, (int, float)) else [
            getattr(timeout, name) for name in ("connect", "read", "write", "pool")
        ]
        if (
            adapter.settings.public_settings() != plan.profile
            or str(client.base_url).rstrip("/") != plan.profile["endpoint"]
            or client.max_retries != 0
            or client._client.follow_redirects is not False
            or any(value != plan.limits.call_timeout_s for value in components)
        ):
            raise ProviderProfileChanged("provider_profile_changed")
    except (AttributeError, TypeError):
        raise ProviderProfileChanged("provider_profile_changed") from None


class GuardedAdapter:
    def __init__(self, inner: object, plan: Plan) -> None:
        self.inner, self.plan = inner, plan

    async def completion(self, request, output_limit):
        # Repeat after pacing/admission, immediately before dispatch.
        verify_transport(self.inner, self.plan)
        if output_limit != self.plan.limits.max_output_tokens_per_call:
            raise ProviderProfileChanged("provider_output_limit_changed")
        return await self.inner.completion(request, output_limit)


class AllocatedLedger(runner.ReleaseLedger):
    """Reuse durable release reservations, model validation and interruption handling."""

    def __init__(self, adapter: object, plan: Plan, journal: Path) -> None:
        self.transport, self.outer_plan = adapter, plan
        super().__init__(GuardedAdapter(adapter, plan), plan, journal)

    async def complete(self, request, scope):
        if self.stopped:
            raise runner.RunStopped(self.stopped)
        start = len(self.calls)
        try:
            verify_transport(self.transport, self.outer_plan)
            return await super().complete(request, scope)
        except ProviderProfileChanged:
            self.stopped = "provider_profile_changed"
            raise
        finally:
            for call in self.calls[start:]:
                # The frozen superclass checks model only after parsing succeeds.
                if call["resolved_model"] is not None and call["resolved_model"] not in self.resolved_models:
                    self.stopped = "resolved_model_changed"
                    if call["error_type"] != "ResolvedModelMismatch":
                        call.update(status="error", error_type="ResolvedModelMismatch")
                        self.event(call)
                # SDK timeouts/connection drops arrive as ProviderError, not TimeoutError.
                if call["status"] == "error" and any(
                    call[key] is None for key in ("input_tokens", "output_tokens")
                ):
                    self.stopped = self.stopped or "request_interrupted_usage_unknown"


def make_http_client(timeout_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)


class GuardedSettings:
    """Use the frozen SDK setup with redirects disabled at its HTTP transport."""

    def __init__(self, settings, plan) -> None:
        self.inner, self.plan = settings, plan
        if settings.public_settings() != plan.profile:
            raise ValueError("frozen_provider_profile_changed")

    def __getattr__(self, name):
        return getattr(self.inner, name)

    @asynccontextmanager
    async def make_client(self, *, timeout_s):
        if timeout_s != self.plan.limits.call_timeout_s:
            raise ValueError("provider_timeout_changed")
        async with (
            make_http_client(timeout_s) as transport,
            self.inner.make_client(timeout_s=timeout_s, http_client=transport) as client,
        ):
            yield client


async def execute(prepared: Prepared) -> int:
    """Claim once, invoke an isolated module copy, retain accounting even on interruption."""
    # Recheck files/readiness immediately before claiming; do not trust stale preflight.
    if digest(prepared.plan_path) != prepared.plan_sha256:
        raise ValueError("outer_plan_changed_after_preflight")
    current = prepare(prepared.plan_path, prepared.wheel, prepared.book, prepared.output, prepared.settings)
    if current.plan_sha256 != prepared.plan_sha256:
        raise ValueError("outer_plan_changed_after_preflight")
    if not current.ready:
        raise ValueError("provider_capacity_not_verified")
    plan, output = current.plan, current.output
    runner.claim_run(output, current.claim, plan)
    binding = {
        "schema_version": 1, "status": "CLAIMED_NOT_COMPLETE", "allocation_id": plan.allocation_id,
        "outer_plan": plan.model_dump(), "outer_plan_sha256": current.plan_sha256,
        "package": current.provenance,
    }
    runner.comparison.write_report(output / "allocation-binding.json", binding)
    ledgers = []
    exit_code = 1
    error_type = None
    try:
        # This module has its own globals. The imported native/historical modules stay untouched.
        core = runner.load_module("allocated_comparison_execution", ROOT / INNER_SOURCES[0])

        def ledger_factory(adapter, inner, journal):
            if inner != current.inner_plan:
                raise ValueError("comparison_plan_changed_at_execution")
            ledger = AllocatedLedger(adapter, plan, journal)
            ledgers.append(ledger)
            return ledger

        core.Ledger = ledger_factory
        class FrozenSettings:
            @staticmethod
            def from_env(environment):
                return GuardedSettings(runner.smoke.ModelSettings.from_env(environment), plan)

        helpers = SimpleNamespace(
            require=runner.smoke.require, package_provenance=runner.smoke.package_provenance,
            load_dotenv=runner.smoke.load_dotenv, ModelSettings=FrozenSettings,
            ChatCompletionsProvider=runner.smoke.ChatCompletionsProvider,
        )
        fixture = json.loads((ROOT / FIXTURE).read_text())
        runner.native_queries(fixture, "held_out")
        args = argparse.Namespace(
            comparison_plan=str(ROOT / plan.comparison_plan), out=str(output / "report.json"),
            wheel=str(current.wheel), trials=3, env_file=None, provider=None, model=None,
            base_url=None, check_config=False,
        )
        exit_code = await core.run(args, helpers, fixture, runner.scoring._score_evidence_recall)
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
        error_type = type(exc).__name__
        raise
    finally:
        calls = [call for ledger in ledgers for call in ledger.calls]
        reservations = [ledger.reserved() for ledger in ledgers]
        unknown = sum(call["input_tokens"] is None or call["output_tokens"] is None for call in calls)
        stopped = next((ledger.stopped for ledger in ledgers if ledger.stopped), None)
        complete = exit_code == 0 and error_type is None and not stopped and not unknown and len(ledgers) == 1
        accounting = {
            "schema_version": 1, "status": "COMPLETE" if complete else "INCOMPLETE",
            "allocation_id": plan.allocation_id, "allocation_reusable": False,
            "outer_plan_sha256": current.plan_sha256, "comparison_plan_sha256": plan.comparison_plan_sha256,
            "calls": len(calls), "calls_with_unknown_usage": unknown,
            "reserved_tokens": sum(value[0] for value in reservations),
            "reserved_estimated_usd": sum(value[1] for value in reservations),
            "complete_cost_estimate": not unknown,
            "stopped_reason": stopped, "error_type": error_type,
            "limits": plan.limits.model_dump(),
            "report_sha256": digest(output / "report.json") if (output / "report.json").exists() else None,
            "journal_sha256": digest(output / "report.calls.jsonl")
            if (output / "report.calls.jsonl").exists() else None,
            "reconciliation": "Retain the consumed envelope and claim. Journal events update call_id; "
            "do not count pending/final events as separate requests. Unknown usage retains reservations.",
        }
        runner.comparison.write_report(output / "allocation-accounting.json", accounting)
    return 0 if accounting["status"] == "COMPLETE" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--allocation-book", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        if args.env_file:
            runner.smoke.require(args.env_file.is_file(), "env_file_missing")
            runner.smoke.load_dotenv(args.env_file, override=False)
        settings = runner.smoke.ModelSettings.from_env(os.environ)
        prepared = prepare(args.plan, args.wheel, args.allocation_book, args.out, settings)
        if not args.execute:
            print(json.dumps({"status": "PREFLIGHT_PASS", "provider_calls": 0,
                              "execution_ready": prepared.ready, "package": prepared.provenance}))
            return 0
        return asyncio.run(execute(prepared))
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - Sanitize all CLI errors.
        # Never serialize exception messages, provider bodies or environment credentials.
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
