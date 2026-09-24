"""Export the preregistered human-review sample; never score or approve a release."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

STRATEGIES = {
    "native": ("lexical", "auto"),
    "comparison": ("full_history", "recent_lexical", "tree"),
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def category(query: dict) -> str:
    if not query["answerable"]:
        return "absent"
    return "correction" if (query.get("answer_rubric") or {}).get("must_not_cite_values") else "old_fact"


def passing(row: dict, query: dict, surface: str) -> bool:
    if not query["answerable"]:
        abstention = "insufficient_evidence" if surface == "native" else "abstained"
        return row.get("status") == abstention and row.get("answer") is None and not row.get("citations")
    review = row.get("review", {})
    cited = row.get("citations", [])
    judgments = review.get("citations", [])
    return (
        row.get("status") == "answered"
        and row.get("evidence_recall") == 1.0
        and row.get("citations_valid") is True
        and bool(cited)
        and len(set(cited)) == len(cited)
        and review.get("status") == "ok"
        and review.get("correct") is True
        and len(judgments) == len(cited)
        and {item.get("evidence_id") for item in judgments} == set(cited)
        and all(item.get("supports_claim") is True for item in judgments)
    )


def select_cases(report: dict, fixture: dict, surface: str) -> tuple[list[dict], list[dict]]:
    """Construct every expected slot, then select all failures and fixed stratified passes."""
    plan = report["plan"]
    if plan["trials"] != 3:
        raise ValueError("human release review requires three planned trials")
    strategies = STRATEGIES[surface]
    if surface == "comparison" and tuple(plan["strategies"]) != strategies:
        raise ValueError("comparison strategies differ from the preregistered scope")
    queries = [query for query in fixture["queries"] if query["split"] == "held_out"]
    if not queries or len({q["query_id"] for q in queries}) != len(queries):
        raise ValueError("held-out query IDs must be nonempty and unique")
    expected = {
        (trial, strategy, query["query_id"]): query
        for trial in range(1, 4) for strategy in strategies for query in queries
    }
    rows = {}
    for row in report["query_results"]:
        key = (row["trial"], row["mode" if surface == "native" else "strategy"], row["query_id"])
        if key not in expected or key in rows:
            raise ValueError("unknown or duplicate result slot")
        query = expected[key]
        if row["history"] != query["history_label"] or row["answerable"] != query["answerable"]:
            raise ValueError("result identity disagrees with fixture")
        rows[key] = row

    strata = defaultdict(list)
    selected = []
    counts = {}
    for (trial, strategy, query_id), query in expected.items():
        row = rows.get((trial, strategy, query_id), {"status": "not_run"})
        stratum = (trial, strategy, category(query))
        counts.setdefault(stratum, {"failures": 0, "passing": 0, "selected_passing": 0})
        item = {
            "case_id": f"{surface}:{trial}:{strategy}:{query_id}",
            "trial": trial, "strategy": strategy, "category": category(query),
            "query": query, "result": row, "human_decision": None,
        }
        if passing(row, query, surface):
            strata[stratum].append(item)
            counts[stratum]["passing"] += 1
        else:
            item["selection_reason"] = "mandatory_nonpassing_or_missing"
            selected.append(item)
            counts[stratum]["failures"] += 1
    for stratum, candidates in strata.items():
        candidates.sort(key=lambda item: (
            hashlib.sha256(f"7|{item['case_id']}".encode()).hexdigest(), item["case_id"]
        ))
        size = min(len(candidates), max(3, math.ceil(0.2 * len(candidates))))
        counts[stratum]["selected_passing"] = size
        for item in candidates[:size]:
            item["selection_reason"] = "stratified_passing_sample"
            selected.append(item)
    summary = [
        {"trial": trial, "strategy": strategy, "category": kind,
         **counts.get((trial, strategy, kind), {"failures": 0, "passing": 0, "selected_passing": 0})}
        for trial in range(1, 4) for strategy in strategies
        for kind in ("correction", "old_fact", "absent")
    ]
    return sorted(selected, key=lambda item: item["case_id"]), summary


def prepare(report_path: Path, fixture_path: Path, surface: str) -> dict:
    report = json.loads(report_path.read_text())
    fixture_hash = digest(fixture_path)
    if report["plan"]["fixture_sha256"] != fixture_hash:
        raise ValueError("fixture hash does not match the frozen report plan")
    fixture = json.loads(fixture_path.read_text())
    held = [q for q in fixture["queries"] if q["split"] == "held_out"]
    if len(held) != 40 or sum(q["answerable"] for q in held) != 32:
        raise ValueError("release fixture must contain 32 answerable and 8 absent queries")
    selected, strata = select_cases(report, fixture, surface)
    histories = {history["history_label"]: history for history in fixture["histories"]}
    for item in selected:
        query = item["query"]
        messages = histories[query["history_label"]]["messages"]
        item["required_original_evidence"] = []
        for unit in query["required_evidence_units"]:
            original = messages[unit["message_index"]]
            span = unit["acceptable_span"]
            item["required_original_evidence"].append({
                "annotation": unit, "original_message": original,
                "acceptable_text": original["content"][span["start"]:span["end"]],
            })
    return {
        "schema_version": 1, "human_review_status": "NOT_RUN", "surface": surface,
        "sampling_seed": 7, "protocol_sha256": digest(Path(__file__).with_name("human-review.md")),
        "selector_sha256": digest(Path(__file__)),
        "report": {"path": str(report_path), "sha256": digest(report_path), "status": report.get("status")},
        "fixture": {"path": str(fixture_path), "sha256": fixture_hash},
        "frozen_plan": report["plan"], "package": report.get("package"),
        "expected_cases": 3 * 40 * len(STRATEGIES[surface]),
        "reported_cases": len(report["query_results"]), "selected_cases": len(selected),
        "strata": strata, "items": selected,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=STRATEGIES, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    packet = prepare(args.report, args.fixture, args.surface)
    with args.out.open("x") as output:
        json.dump(packet, output, indent=2)
        output.write("\n")
    print(f"PREPARED: {packet['selected_cases']} cases; human review NOT_RUN")


if __name__ == "__main__":
    main()
