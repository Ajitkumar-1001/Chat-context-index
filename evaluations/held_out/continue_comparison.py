"""Continue undispatched work with the frozen comparison functions; retain every sent attempt."""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any


def module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("evaluation_module_missing")
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


def identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row[k] for k in ("trial", "history", "strategy", "query_id"))


def pending_rows(report: dict[str, Any]) -> list[int]:
    dispatched = {identity(c) for c in report["calls"] if c["phase"] == "query"}
    return [
        number
        for number, row in enumerate(report["query_results"])
        if row["status"] == "error"
        and row.get("error_type") == "RunStopped"
        and identity(row) not in dispatched
    ]


class RecoveryLedger:
    """Charge rebuilding a previously completed index separately, within application usage."""

    def __init__(self, ledger: Any) -> None:
        self.inner = ledger

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.inner.calls

    async def complete(self, request: Any, scope: dict[str, Any]) -> Any:
        if scope["phase"] in ("initial_index", "update_index"):
            scope = dict(scope, phase="recovery_" + scope["phase"])
        return await self.inner.complete(request, scope)


async def run(args: argparse.Namespace) -> int:
    root = Path(__file__).parents[2]
    frozen = module("cci_frozen_comparison", Path(__file__).with_name("comparison.py"))
    harness = module("cci_frozen_harness", Path(__file__).with_name("run_evaluation.py"))
    helpers = harness._model_helpers()
    plan = json.loads(args.plan.read_text())
    parent = json.loads(args.from_report.read_text())
    fixture = json.loads(Path(__file__).with_name("fixture.json").read_text())
    helpers.require(bool(sys.flags.isolated), "run_with_python_I")
    helpers.require(parent["status"] == "INCOMPLETE", "parent_must_be_incomplete")
    helpers.require(frozen.digest(args.from_report) == plan["parent_report_sha256"], "parent_changed")
    helpers.require(
        frozen.digest(args.from_report.with_suffix(".calls.jsonl")) == plan["parent_journal_sha256"],
        "journal_changed",
    )
    helpers.require(not any(c["status"] == "pending" for c in parent["calls"]), "parent_still_running")
    helpers.require(
        not args.out.exists() and not args.out.with_suffix(".calls.jsonl").exists(), "output_exists"
    )
    helpers.require(
        frozen.digest(Path(__file__).with_name("fixture.json")) == plan["fixture_sha256"], "fixture_changed"
    )
    for relative, expected in plan["source_hashes"].items():
        helpers.require(frozen.digest(root / relative) == expected, "frozen_source_changed")
    for field in (
        "model",
        "provider",
        "trials",
        "strategies",
        "initial_history_percent",
        "context",
        "package_config",
        "quality_gates",
        "comparison_max_quality_loss",
        "pricing",
    ):
        helpers.require(plan[field] == parent["plan"][field], "quality_protocol_changed")
    provenance = helpers.package_provenance(args.wheel)
    helpers.require(provenance["wheel_sha256"] == plan["wheel_sha256"], "wheel_changed")
    helpers.load_dotenv(args.env_file, override=False)
    settings = helpers.ModelSettings.from_env(dict(os.environ))
    helpers.require(
        settings.provider == plan["provider"] and settings.model == plan["model"], "model_changed"
    )
    helpers.require(settings.public_settings() == parent["provider_settings"], "model_settings_changed")
    indices = pending_rows(parent)
    report = copy.deepcopy(parent)
    report.update(
        plan=plan,
        plan_sha256=frozen.digest(args.plan),
        package=provenance,
        provider_settings=settings.public_settings(),
        status="NOT_RUN",
        continuation={
            "parent_report": str(args.from_report),
            "parent_report_sha256": plan["parent_report_sha256"],
            "started_utc": datetime.now(UTC).isoformat(),
            "undispatched_queries": len(indices),
            "completed_queries": 0,
            "preserved_parent_calls": len(parent["calls"]),
            "policy": (
                "Never retry a dispatched query or review. "
                "Preserve completed answers and the HTTP 429 failure. "
                "Rebuild lost temporary databases; charge recovery indexing to tree application usage. "
                "Use frozen prompts, retrieval and scoring functions."
            ),
        },
    )
    for key in (
        "summaries",
        "comparisons",
        "completed_utc",
        "elapsed_s",
        "quality_status",
        "lower_cost_claim",
        "stopped_reason",
    ):
        report.pop(key, None)
    if args.check_config:
        frozen.write_report(args.out, report)
        print(f"NOT_RUN: {len(indices)} undispatched queries; all sent attempts retained")
        return 2
    async with settings.make_client(timeout_s=plan["limits"]["call_timeout_s"]) as client:
        args.out.with_suffix(".calls.jsonl").write_bytes(
            args.from_report.with_suffix(".calls.jsonl").read_bytes()
        )
        ledger = frozen.Ledger(
            helpers.ChatCompletionsProvider(client, settings), plan, args.out.with_suffix(".calls.jsonl")
        )
        ledger.calls.extend(copy.deepcopy(parent["calls"]))
        report["calls"] = ledger.calls

        def checkpoint() -> None:
            report["usage"] = frozen.usage_summary(ledger.calls, ledger)
            report["budget_tokens_reserved"], report["budget_estimated_usd_reserved"] = ledger.reserved()
            frozen.write_report(args.out, report)

        report["status"] = "RUNNING"
        checkpoint()
        histories = {h["history_label"]: h for h in fixture["histories"] if h["split"] == "held_out"}
        queries = {q["query_id"]: q for q in fixture["queries"] if q["split"] == "held_out"}
        groups = sorted(
            {(parent["query_results"][i]["trial"], parent["query_results"][i]["history"]) for i in indices}
        )
        try:
            async with asyncio.timeout(plan["limits"]["run_timeout_s"]):
                with tempfile.TemporaryDirectory(prefix="cci-held-out-continuation-") as temporary:
                    directory = Path(temporary)
                    for trial, label in groups:
                        if ledger.stopped:
                            break
                        previous = next(
                            h for h in report["histories"] if h["trial"] == trial and h["history"] == label
                        )
                        had_index = all(
                            previous[p]["status"] == "complete" for p in ("initial_index", "update_index")
                        )
                        rebuilt: dict[str, Any] = {"histories": [], "query_results": []}
                        await frozen.run_history(
                            histories[label],
                            [],
                            trial,
                            directory,
                            RecoveryLedger(ledger) if had_index else ledger,
                            plan,
                            rebuilt,
                            harness._score_evidence_recall,
                            checkpoint,
                        )
                        replacement = rebuilt["histories"][0]
                        replacement["continuation_rebuilt"] = True
                        replacement["prior_index_complete"] = had_index
                        report["histories"][report["histories"].index(previous)] = replacement
                        if ledger.stopped:
                            break
                        config = frozen.Config(
                            **plan["package_config"], application_namespace=f"comparison-{trial}-{label}"
                        )
                        async with await frozen.HistoryStore.open(
                            str(directory / f"trial-{trial}-{label}.db"), config=config
                        ) as store:
                            for i in indices:
                                old = parent["query_results"][i]
                                if (old["trial"], old["history"]) != (trial, label):
                                    continue
                                if ledger.stopped:
                                    break
                                row = await frozen.query_case(
                                    store,
                                    queries[old["query_id"]],
                                    histories[label],
                                    old["strategy"],
                                    trial,
                                    ledger,
                                    plan,
                                    harness._score_evidence_recall,
                                )
                                row["continuation"] = {"parent_status": "RunStopped", "parent_dispatches": 0}
                                report["query_results"][i] = row
                                report["continuation"]["completed_queries"] += 1
                                checkpoint()
                        print(
                            f"continued {label}; queries={report['continuation']['completed_queries']}/"
                            f"{len(indices)}; calls={len(ledger.calls)}",
                            flush=True,
                        )
                # Review only after completing generation, using the unchanged reviewer.
                reviewed = {identity(c) for c in ledger.calls if c["phase"] == "review"}
                for row in report["query_results"]:
                    if ledger.stopped:
                        break
                    if identity(row) not in reviewed:
                        await frozen.review_case(row, queries[row["query_id"]], ledger)
                        checkpoint()
                report["status"] = "INCOMPLETE" if ledger.stopped else "COMPLETE_WITH_FAILURES"
        except (Exception, asyncio.CancelledError) as exc:
            report["status"] = "INCOMPLETE"
            report["errors"].append(type(exc).__name__)
        finally:
            report["stopped_reason"] = ledger.stopped
            report["summaries"] = frozen.summarize(
                report["query_results"], list(queries.values()), ledger, plan, report["histories"]
            )
            report["comparisons"] = frozen.compare_costs(report["summaries"], plan)
            report["completed_utc"] = datetime.now(UTC).isoformat()
            report["elapsed_s"] = round(
                (datetime.now(UTC) - datetime.fromisoformat(report["started_utc"])).total_seconds(), 3
            )
            report["quality_status"] = (
                "PASS"
                if all(s["quality_status"] == "PASS" for s in report["summaries"] if s["strategy"] == "tree")
                else "FAIL"
            )
            report["lower_cost_claim"] = "NOT_ESTABLISHED"
            checkpoint()
    print(f"{report['status']}; tree quality {report['quality_status']}: {args.out}")
    return 1 if report["status"] == "INCOMPLETE" else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("from-report", "plan", "env-file", "wheel", "out"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--check-config", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
