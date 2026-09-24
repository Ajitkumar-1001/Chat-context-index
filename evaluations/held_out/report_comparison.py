"""Render existing comparison measurements; does not make calls or change scoring."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any


def percent(value: float | None) -> str:
    return "unknown" if value is None else f"{100 * value:.1f}%"


def render(report: dict[str, Any], report_path: Path, prior: list[dict[str, Any]]) -> str:
    summaries = report["summaries"]
    generated = {
        (s["trial"], s["strategy"]): sum(
            r["status"] in ("answered", "abstained", "invalid_citations")
            for r in report["query_results"]
            if (r["trial"], r["strategy"]) == (s["trial"], s["strategy"])
        )
        for s in summaries
    }
    finished_trials = sorted(
        {
            s["trial"]
            for s in summaries
            if all(
                generated[(other["trial"], other["strategy"])] == other["queries_expected"]
                for other in summaries
                if other["trial"] == s["trial"]
            )
        }
    )
    display_summaries = [s for s in summaries if s["trial"] in finished_trials]
    reviewed_count = sum(r.get("review", {}).get("status") == "ok" for r in report["query_results"])
    execution_note = (
        f"Answer generation finished for trials {', '.join(map(str, finished_trials)) or 'none'}. "
    )
    if report["status"] == "INCOMPLETE":
        execution_note += f"Remaining work was interrupted: `{report.get('stopped_reason')}`. "
    if "continuation" in report:
        execution_note += (
            "The continuation retained the original failed request and attempted only work with no "
            "recorded dispatch; recovery indexing is included in usage. "
        )
    execution_note += f"Completed model rubric reviews: {reviewed_count}. "
    if any(s["review_errors"] for s in summaries):
        execution_note += (
            "Correctness remains unverified for unreviewed answers. Raw zero scores for them are "
            "conservative gate values, not measured accuracy."
        )
    lines = [
        "# ContIndex held-out memory comparison",
        "",
        f"Execution: **{report['status']}**. Tree quality: **{report['quality_status']}**. "
        f"Lower-cost claim: **{report['lower_cost_claim']}**.",
        "",
        f"Model: `{report['provider_settings']['requested_model']}`. "
        "Planned workload: three trials of the same 40 synthetic held-out queries, "
        "with 32 answerable and 8 absent-answer cases. "
        "These are 40 unique cases, not 120 independent questions. "
        "All strategies use the same host answer prompt.",
        "",
        execution_note,
        "",
        "## Results",
        "",
        f"This table covers the {len(finished_trials)} completed answer-generation trials only. "
        "Recall averages their scores; absent-answer counts combine them. "
        "Every planned trial appears in the next table and remains in the raw report. "
        "Failed and unexecuted cases remain in denominators. Correctness and semantic citation support use "
        "a planned separate call to the same model after answers are collected, "
        "not independent human review.",
        "",
        "| Strategy | Original evidence recall | Reviewed correctness | Correct abstentions | "
        "Unsupported answers on absent cases | Quality trials passed |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for strategy in report["plan"]["strategies"]:
        rows = [s for s in display_summaries if s["strategy"] == strategy]
        if not rows:
            continue
        correctness = (
            "unverified"
            if any(s["review_errors"] for s in rows)
            else percent(mean(s["reviewed_answer_correctness"] for s in rows))
        )
        lines.append(
            f"| `{strategy}` | {percent(mean(s['macro_evidence_recall'] for s in rows))} | "
            f"{correctness} | "
            f"{sum(s['correct_abstentions'] for s in rows)}/{sum(s['absent_cases'] for s in rows)} | "
            f"{sum(s['unsupported_answers_on_absent_cases'] for s in rows)} | "
            f"{sum(s['quality_status'] == 'PASS' for s in rows)}/{len(rows)} |"
        )
    lines += [
        "",
        "Abstaining on an answerable case is scored as incorrect without a model review. "
        "Generated answers require rubric review to count as correct.",
        "",
        "| Trial | Strategy | Model outputs | Recall | Correctness | "
        "Citation validity | Citation support | Quality |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for s in summaries:
        count = generated[(s["trial"], s["strategy"])]
        recall = percent(s["macro_evidence_recall"]) if count == s["queries_expected"] else "incomplete"
        correctness = (
            "unverified"
            if s["review_errors"] or count != s["queries_expected"]
            else percent(s["reviewed_answer_correctness"])
        )
        lines.append(
            f"| {s['trial']} | `{s['strategy']}` | {count}/{s['queries_expected']} | {recall} | "
            f"{correctness} | {percent(s['citation_validity'])} | "
            f"{percent(s['citation_support_precision'])} | {s['quality_status']} |"
        )
    lines += [
        "",
        "Correction cases in the completed generation trials:",
        "",
        "| Strategy | Correction cases | Required original evidence recall |",
        "| --- | ---: | ---: |",
    ]
    for strategy in report["plan"]["strategies"]:
        corrections = [
            r
            for r in report["query_results"]
            if r["trial"] in finished_trials and r["strategy"] == strategy and r["correction_case"]
        ]
        recall = percent(mean(r["evidence_recall"] for r in corrections)) if corrections else "not applicable"
        lines.append(f"| `{strategy}` | {len(corrections)} | {recall} |")
    lines += [
        "",
        "## Usage and cost",
        "",
        "Application usage includes initial indexing, appended-history updates, navigation, and answers. "
        "It includes the incomplete third trial and database-recovery calls, so these totals are not a "
        "complete-workload cost comparison. "
        "Review usage is evaluation overhead and is shown separately. These are observed API tokens; "
        "price estimates use standard list prices, without cache discounts or free-tier allowances. "
        "They are not billing receipts. An asterisk marks incomplete usage.",
        "",
        "| Strategy | Application calls | Input tokens | Output tokens | Standard price estimate | "
        "Review calls | Review price estimate |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for strategy in report["plan"]["strategies"]:
        rows = [s for s in summaries if s["strategy"] == strategy]
        app = [s["application_usage"] for s in rows]
        review = [s["review_usage"] for s in rows]
        marker = "*" if any(u["usage_unknown"] for u in app) else ""
        review_marker = "*" if any(u["usage_unknown"] for u in review) else ""
        lines.append(
            f"| `{strategy}` | {sum(u['calls'] for u in app)} | "
            f"{sum(u['input_tokens_observed'] for u in app):,} | "
            f"{sum(u['output_tokens_observed'] for u in app):,} | "
            f"${sum(u['standard_price_estimate_observed_usd'] for u in app):.6f}{marker} | "
            f"{sum(u['calls'] for u in review)} | "
            f"${sum(u['standard_price_estimate_observed_usd'] for u in review):.6f}{review_marker} |"
        )
    rates = report["plan"]["pricing"]
    lines += [
        "",
        "Actual usage by operation across all three trials:",
        "",
        "| Operation | Calls | Input tokens | Output tokens |",
        "| --- | ---: | ---: | ---: |",
    ]
    for label, phase, operation in (
        ("Initial tree indexing", "initial_index", "indexing"),
        ("Appended-history indexing", "update_index", "indexing"),
        ("Recovery initial indexing", "recovery_initial_index", "indexing"),
        ("Recovery update indexing", "recovery_update_index", "indexing"),
        ("Tree navigation", "query", "tree_navigation"),
        ("Host answers, all strategies", "query", "synthesis"),
        ("Rubric and citation review", "review", "rubric_review"),
    ):
        calls = [c for c in report["calls"] if c["phase"] == phase and c["operation"] == operation]
        marker = "*" if any(c["input_tokens"] is None or c["output_tokens"] is None for c in calls) else ""
        lines.append(
            f"| {label} | {len(calls)} | {sum(c['input_tokens'] or 0 for c in calls):,}{marker} | "
            f"{sum(c['output_tokens'] or 0 for c in calls):,}{marker} |"
        )
    lines += [
        "",
        f"Rates: ${rates['input_usd_per_million']:.2f}/million input tokens and "
        f"${rates['output_usd_per_million']:.2f}/million output tokens, verified "
        f"{rates['verified_date']}. [Google pricing]({rates['source']}).",
        "",
        "A cheaper run qualifies only if all quality gates pass, usage is complete, and recall/correctness "
        "are within five percentage points of the baseline in every trial. No savings percentage from "
        "an ineligible comparison is presented as a supported product claim.",
        "",
        "## Run history and limits",
        "",
        f"The combined run made {report['usage']['calls']} calls over {report['elapsed_s']:.1f} "
        "seconds of wall time, including pauses between attempts. "
        f"Observed input/output tokens: {report['usage']['input_tokens_observed']:,}/"
        f"{report['usage']['output_tokens_observed']:,}. "
        f"Unknown usage: `{report['usage']['usage_unknown']}`. "
        "The standard price estimate, including review, is "
        f"${report['usage']['standard_price_estimate_observed_usd']:.6f}.",
        "",
    ]
    if "continuation" in report:
        continuation = report["continuation"]
        lines += [
            f"The combined call ledger carries forward {continuation['preserved_parent_calls']} "
            "calls from [v2](../comparison-live-v2.json); those calls must not be added again. "
            f"The continuation completed {continuation['completed_queries']} of "
            f"{continuation['undispatched_queries']} previously undispatched queries. "
            "Both HTTP 429 failures and their unknown usage remain recorded.",
            "",
        ]
    for attempt in prior:
        if "usage" in attempt:
            usage = attempt["usage"]
            lines.append(
                f"- Earlier attempt: {attempt['status']}, stopped reason "
                f"`{attempt.get('stopped_reason')}`; {usage['calls']} calls, "
                f"{usage['input_tokens_observed']:,} input and {usage['output_tokens_observed']:,} "
                f"output tokens observed; unknown usage `{usage['usage_unknown']}`."
            )
        else:
            lines.append(
                f"- Diagnostic request: {attempt['status']}; "
                f"{attempt.get('input_tokens')} input and {attempt.get('output_tokens')} output tokens."
            )
    all_inputs = report["usage"]["input_tokens_observed"] + sum(
        p["usage"]["input_tokens_observed"] if "usage" in p else p.get("input_tokens") or 0 for p in prior
    )
    all_outputs = report["usage"]["output_tokens_observed"] + sum(
        p["usage"]["output_tokens_observed"] if "usage" in p else p.get("output_tokens") or 0 for p in prior
    )
    all_calls = report["usage"]["calls"] + sum(p["usage"]["calls"] if "usage" in p else 1 for p in prior)
    all_estimate = (
        all_inputs * rates["input_usd_per_million"] + all_outputs * rates["output_usd_per_million"]
    ) / 1e6
    lines += [
        "",
        f"All attempts combined: **{all_calls} calls**, **{all_inputs:,} input tokens** and "
        f"**{all_outputs:,} output tokens** observed, with a standard price estimate of "
        f"**${all_estimate:.6f}** for known usage. "
        "The [accounting audit](../accounting-integrity.json) verifies the original $3, "
        "1,200-call and 6-million-token client allowances, including reservations for unknown usage.",
        "",
        "The separate v1 attempt and diagnostic requests are excluded from the main strategy comparison. "
        "Their observed usage and retained reservations count against the overall evaluation budget. "
        "Unknown usage prevents an exact all-attempt billing total.",
        "",
        "The operational continuation changes pacing, remaining allowances, and database reconstruction "
        "after rate-limit failures. "
        "The fixture, model, answer prompt, retrieval settings, and scoring "
        "were not tuned to held-out results.",
        "",
        "Correction-case correctness and semantic citation support remain unverified. The failed call "
        "was not retried or credited as an abstention. This run cannot close the three-trial release gate "
        "or establish a lower-cost claim.",
        "",
        "## Independent development findings",
        "",
        "A separate [development probe](../../results/context-selection-probe.json) uses 28 invented "
        "messages and deterministic selection of the only tree leaf, with no external model calls. "
        "Raw tree retrieval includes the required message at sequence 13. Default context assembly "
        "drops it and keeps sequences 1–4 and 25–28. This demonstrates loss during source selection "
        "even when tree navigation reaches the correct leaf; it does not explain every held-out miss.",
        "",
        "The same probe finds the source with the keyword `codename`, while its natural-language "
        "question returns no lexical candidates. Source code joins every query token with implicit "
        "FTS AND semantics. Both findings point to development work on source relevance before "
        "context limits are applied. The benchmark implementation was left unchanged.",
        "",
        "The [local Linux checks](../../results/linux-release/README.md) passed 165 Python tests, "
        "8 native TypeScript tests, and fresh-package checks in all four writer-reader combinations. "
        "A shared Linux container also passed the reference timing thresholds. GitHub release "
        "environments and a dedicated Linux performance run remain separate gates.",
        "",
        "## Reproduction and scope",
        "",
        f"- Raw report: [`{report_path.name}`](../{report_path.name}).",
        "- Frozen plan and evaluator/wheel/fixture hashes are embedded in the raw report.",
        "- Run commands and method: [evaluation README](../README.md#budgeted-memory-comparison).",
        "- Each history: 80% initial ingestion/indexing, 20% append/update, SQLite close/reopen, "
        "then five queries. Both bounded strategies use four recent items, eight total items, "
        "4,000 context characters, and 200-character excerpts.",
        "- Every trial has a fresh database and disabled memoization. Tree fallback modes, exact "
        "original excerpts, per-call usage, and review judgments are in the raw report.",
        "- This measures the Python memory integration with a common host prompt. Native `ask()` quality, "
        "native TypeScript live-model evaluation, independent human review, and real customer workloads "
        "remain outside this report.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--prior", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    if report["status"] == "RUNNING":
        raise ValueError("wait_for_completed_report")
    text = render(report, args.report, [json.loads(path.read_text()) for path in args.prior])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
