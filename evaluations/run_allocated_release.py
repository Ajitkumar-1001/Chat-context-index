"""Guard the unchanged release evaluator with exclusive allocation checks.

The outer plan pins the original release plan plus both guard sources. Default
preflight makes no requests or writes; --execute consumes its canonical claim once.
An explicit capacity_probe permits only a bounded development smoke while provider
readiness is blocked. It cannot establish sustained capacity or release quality.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "allocated_shared_guards", ROOT / "evaluations/run_allocated_comparison.py"
)
assert spec is not None and spec.loader is not None
shared = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = shared
spec.loader.exec_module(shared)

GUARD_SOURCES = ("evaluations/run_allocated_comparison.py", "evaluations/run_allocated_release.py")


class Plan(shared.runner.StrictModel):
    schema_version: Literal[1]
    release_plan: str
    release_plan_sha256: str
    guard_source_hashes: dict[str, str]
    capacity_probe: bool = False


@dataclass(frozen=True)
class Prepared:
    outer: Plan
    plan: object
    plan_path: Path
    plan_sha256: str
    wheel: Path
    book: Path
    output: Path
    settings: object
    provenance: dict
    claim: Path
    ready: bool


def prepare(plan_path: Path, wheel: Path, book: Path, output: Path, settings: object) -> Prepared:
    shared.require_isolated()
    raw = plan_path.read_bytes()
    outer = Plan.model_validate_json(raw)
    if set(outer.guard_source_hashes) != set(GUARD_SOURCES) or any(
        shared.digest(ROOT / name) != expected for name, expected in outer.guard_source_hashes.items()
    ):
        raise ValueError("frozen_guard_sources_changed")
    release_raw = (ROOT / outer.release_plan).read_bytes()
    if hashlib.sha256(release_raw).hexdigest() != outer.release_plan_sha256:
        raise ValueError("frozen_release_plan_changed")
    plan = shared.runner.Plan.model_validate_json(release_raw)
    if outer.capacity_probe and plan.kind != "smoke":
        raise ValueError("capacity_probe_requires_development_smoke")
    if plan.kind == "smoke":
        limits = plan.limits
        if (
            limits.max_calls > 10 or limits.max_reserved_tokens > 100_000
            or limits.max_estimated_usd > 0.04 or limits.max_output_tokens_per_call > 256
            or limits.max_input_bytes_per_call > 8000 or limits.call_timeout_s > 60
            or limits.run_timeout_s > 240 or limits.concurrency != 1
            or limits.minimum_request_interval_s < 5
        ):
            raise ValueError("capacity_check_exceeds_smoke_envelope")
        if (ROOT / plan.fixture).resolve() != (ROOT / "evaluations/fixtures/live-smoke.json").resolve():
            raise ValueError("smoke_requires_original_development_fixture")
        shared.runner.smoke.read_fixture(ROOT / plan.fixture)
    if plan.limits.concurrency != 1:
        raise ValueError("guard_requires_serial_dispatch")
    minimum = (plan.limits.max_calls - 1) * plan.limits.minimum_request_interval_s
    if plan.limits.run_timeout_s <= minimum + plan.limits.call_timeout_s:
        raise ValueError("run_timeout_cannot_fit_frozen_dispatch_schedule")
    shared.runner.verify_pins(plan, wheel, settings.public_settings())
    if plan.split == "held_out" and (ROOT / plan.fixture).resolve() != (ROOT / shared.FIXTURE).resolve():
        raise ValueError("release_requires_original_held_out_fixture")
    if plan.kind == "cache" and "redis" in plan.cache_backends and not os.environ.get("CCI_EVAL_REDIS_URL"):
        raise ValueError("cache_experiment_requires_explicit_redis_url")
    provenance = shared.runner.smoke.package_provenance(wheel)
    canonical = (ROOT / shared.CANONICAL_BOOK).resolve()
    if book.resolve() != canonical:
        raise ValueError("use_canonical_allocation_book")
    claim = shared.runner.verify_allocation(plan, canonical)
    ready = shared.runner.provider_ready(plan, canonical)
    if output.exists() or claim.exists():
        raise FileExistsError("run_or_allocation_already_used")
    return Prepared(outer, plan, plan_path, hashlib.sha256(raw).hexdigest(), wheel, canonical,
                    output, settings, provenance, claim, ready)


async def execute(prepared: Prepared) -> int:
    current = prepare(prepared.plan_path, prepared.wheel, prepared.book, prepared.output, prepared.settings)
    if current.plan_sha256 != prepared.plan_sha256:
        raise ValueError("outer_plan_changed_after_preflight")
    if not current.ready and not current.outer.capacity_probe:
        raise ValueError("provider_capacity_not_verified")
    plan, output = current.plan, current.output
    shared.runner.claim_run(output, current.claim, plan)
    shared.runner.comparison.write_report(output / "allocation-binding.json", {
        "schema_version": 1, "allocation_id": plan.allocation_id,
        "outer_plan": current.outer.model_dump(), "outer_plan_sha256": current.plan_sha256,
        "provider_capacity_verified_before_run": current.ready,
        "package": current.provenance,
    })
    ledgers = []
    report = None
    error_type = None
    try:
        core = shared.runner.load_module(
            "guarded_native_cache_execution", ROOT / "evaluations/held_out/release_validation.py"
        )

        def ledger_factory(adapter, selected_plan, journal):
            if selected_plan.model_dump() != plan.model_dump():
                raise ValueError("release_plan_changed_at_execution")
            ledger = shared.AllocatedLedger(adapter, plan, journal)
            ledgers.append(ledger)
            return ledger

        core.ReleaseLedger = ledger_factory
        fixture_raw = (ROOT / plan.fixture).read_bytes()
        if hashlib.sha256(fixture_raw).hexdigest() != plan.fixture_sha256:
            raise ValueError("frozen_fixture_changed")
        fixture = json.loads(fixture_raw)
        if plan.kind == "native":
            core.native_queries(fixture, plan.split)
        settings = shared.GuardedSettings(current.settings, plan)
        async with settings.make_client(timeout_s=plan.limits.call_timeout_s) as client:
            adapter = shared.runner.smoke.ChatCompletionsProvider(
                client, settings, max_output_tokens=plan.limits.max_output_tokens_per_call
            )
            report = await core.execute(
                plan, fixture, adapter, output, real_provider=True, provenance=current.provenance,
                redis_url=os.environ.get("CCI_EVAL_REDIS_URL"),
            )
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
        error_type = type(exc).__name__
        raise
    finally:
        calls = [call for ledger in ledgers for call in ledger.calls]
        reserved = [ledger.reserved() for ledger in ledgers]
        unknown = sum(call["input_tokens"] is None or call["output_tokens"] is None for call in calls)
        stopped = next((ledger.stopped for ledger in ledgers if ledger.stopped), None)
        complete = bool(
            report and report["status"] == "COMPLETE" and error_type is None
            and not stopped and not unknown and len(ledgers) == 1
        )
        accounting = {
            "schema_version": 1, "status": "COMPLETE" if complete else "INCOMPLETE",
            "capacity_probe": current.outer.capacity_probe,
            "provider_capacity_verified_before_run": current.ready,
            "allocation_id": plan.allocation_id, "allocation_reusable": False,
            "outer_plan_sha256": current.plan_sha256,
            "release_plan_sha256": current.outer.release_plan_sha256,
            "calls": len(calls), "calls_with_unknown_usage": unknown,
            "reserved_tokens": sum(value[0] for value in reserved),
            "reserved_estimated_usd": sum(value[1] for value in reserved),
            "complete_cost_estimate": not unknown, "stopped_reason": stopped, "error_type": error_type,
            "limits": plan.limits.model_dump(),
            "report_sha256": (
                shared.digest(output / "report.json") if (output / "report.json").exists() else None
            ),
            "journal_sha256": (
                shared.digest(output / "calls.jsonl") if (output / "calls.jsonl").exists() else None
            ),
        }
        shared.runner.comparison.write_report(output / "allocation-accounting.json", accounting)
    passed = complete and (plan.split != "held_out" or report["quality_pass"])
    return 0 if passed else 1


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
            shared.runner.smoke.require(args.env_file.is_file(), "env_file_missing")
            shared.runner.smoke.load_dotenv(args.env_file, override=False)
        settings = shared.runner.smoke.ModelSettings.from_env(os.environ)
        prepared = prepare(args.plan, args.wheel, args.allocation_book, args.out, settings)
        if not args.execute:
            print(json.dumps({"status": "PREFLIGHT_PASS", "provider_calls": 0,
                              "execution_ready": prepared.ready or prepared.outer.capacity_probe,
                              "capacity_probe": prepared.outer.capacity_probe,
                              "provider_capacity_verified": prepared.ready,
                              "package": prepared.provenance}))
            return 0
        return asyncio.run(execute(prepared))
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - Sanitize CLI/provider failures.
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
