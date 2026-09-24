"""Verify provider capacity with one allocated installed-package development smoke.

This entry point can diagnose an unavailable provider without declaring it available
first. It preserves the frozen runner, accounting, fixture, and package checks. It
cannot run held-out evaluations or retry a consumed allocation. Default: zero calls.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "capacity_release_validation", ROOT / "evaluations/held_out/release_validation.py"
)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


def validate_capacity_plan(plan) -> None:
    if plan.kind != "smoke" or plan.split != "development" or plan.trials != 1:
        raise ValueError("capacity_check_requires_development_smoke")
    limits = plan.limits
    if (
        limits.max_calls > 10
        or limits.max_reserved_tokens > 100_000
        or limits.max_estimated_usd > 0.04
        or limits.concurrency != 1
        or limits.minimum_request_interval_s < 5
    ):
        raise ValueError("capacity_check_exceeds_smoke_envelope")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        runner.smoke.require(bool(sys.flags.isolated), "run_with_python_I")
        runner.smoke.require(args.env_file.is_file(), "env_file_missing")
        runner.smoke.load_dotenv(args.env_file, override=False)
        plan = runner.Plan.model_validate_json(args.plan.read_text())
        validate_capacity_plan(plan)
        settings = runner.smoke.ModelSettings.from_env(os.environ)
        runner.verify_pins(plan, args.wheel, settings.public_settings())
        provenance = runner.smoke.package_provenance(args.wheel)
        book = ROOT / "evaluations/results/release-readiness/allocations.json"
        claim = runner.verify_allocation(plan, book)
        previously_ready = runner.provider_ready(plan, book)  # Verify the frozen record too.
        fixture = runner.smoke.read_fixture(ROOT / plan.fixture)
        if args.out.exists() or claim.exists():
            raise FileExistsError("run_or_allocation_already_used")
        probe = {
            "source_sha256": runner.comparison.digest(Path(__file__)),
            "previously_ready": previously_ready,
            "purpose": "bounded_capacity_verification",
            "scope": "This smoke cannot establish account-wide quota or sustained capacity.",
        }
        if not args.execute:
            print(json.dumps({"status": "PREFLIGHT_PASS", "provider_calls": 0, "probe": probe}))
            return 0
        runner.claim_run(args.out, claim, plan)
        runner.comparison.write_report(args.out / "capacity-intent.json", probe)

        async def run():
            async with settings.make_client(timeout_s=plan.limits.call_timeout_s) as client:
                adapter = runner.smoke.ChatCompletionsProvider(
                    client, settings, max_output_tokens=plan.limits.max_output_tokens_per_call
                )
                return await runner.execute(
                    plan, fixture, adapter, args.out, real_provider=True, provenance=provenance
                )

        report = asyncio.run(run())
        passed = report["status"] == "COMPLETE" and not report["usage"]["usage_unknown"]
        probe["status"] = "SMOKE_PASS" if passed else "BLOCKED"
        report["capacity_verification"] = probe
        runner.comparison.write_report(args.out / "report.json", report)
        print(json.dumps({key: report[key] for key in ("status", "usage", "stopped_reason")}))
        return 0 if passed else 1
    except (Exception, KeyboardInterrupt) as exc:
        # Provider exception text and configuration values can contain credentials.
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
