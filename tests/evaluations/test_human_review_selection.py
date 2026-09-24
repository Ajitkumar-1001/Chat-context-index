"""Human-review sampling tests use invented data, never reserved held-out labels."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "human_review_selection", Path(__file__).resolve().parents[2] / "docs/validation/prepare_review.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def invented(surface="native"):
    queries = [
        {"query_id": f"q{i}", "history_label": "invented", "split": "held_out",
         "answerable": True, "answer_rubric": {"must_not_cite_values": []}}
        for i in range(20)
    ]
    rows = [
        {"trial": trial, "mode" if surface == "native" else "strategy": strategy,
         "query_id": query["query_id"], "history": "invented", "answerable": True,
         "status": "answered", "answer": "invented", "evidence_recall": 1.0,
         "citations": ["ev1"], "citations_valid": True,
         "review": {"status": "ok", "correct": True,
                    "citations": [{"evidence_id": "ev1", "supports_claim": True}]}}
        for trial in range(1, 4) for strategy in MODULE.STRATEGIES[surface] for query in queries
    ]
    return {"plan": {"trials": 3, "strategies": list(MODULE.STRATEGIES[surface])},
            "query_results": rows}, {"queries": queries}


@pytest.mark.parametrize("surface", ["native", "comparison"])
def test_reproducible_stratified_pass_sample(surface):
    report, fixture = invented(surface)
    selected, strata = MODULE.select_cases(report, fixture, surface)
    assert len(selected) == 3 * len(MODULE.STRATEGIES[surface]) * 4
    assert all(row["human_decision"] is None for row in selected)
    assert all(row["selected_passing"] == 4 for row in strata if row["category"] == "old_fact")
    report["query_results"].reverse()
    fixture["queries"].reverse()
    assert MODULE.select_cases(report, fixture, surface) == (selected, strata)


def test_failures_and_missing_slots_never_disappear():
    report, fixture = invented()
    report["query_results"][0]["review"]["correct"] = False
    report["query_results"][1]["evidence_recall"] = 0.5
    report["query_results"][2]["review"]["citations"][0]["supports_claim"] = False
    report["query_results"].pop(3)
    selected, _ = MODULE.select_cases(report, fixture, "native")
    cases = {row["case_id"]: row for row in selected}
    for i in range(4):
        assert cases[f"native:1:lexical:q{i}"]["selection_reason"] == "mandatory_nonpassing_or_missing"
    assert cases["native:1:lexical:q3"]["result"]["status"] == "not_run"


@pytest.mark.parametrize("change", ["duplicate", "unknown", "wrong_history", "wrong_answerable"])
def test_bad_slot_identity_is_rejected(change):
    report, fixture = invented()
    if change == "duplicate":
        report["query_results"].append(copy.deepcopy(report["query_results"][0]))
    elif change == "unknown":
        report["query_results"][0]["query_id"] = "not-in-fixture"
    elif change == "wrong_history":
        report["query_results"][0]["history"] = "other-history"
    else:
        report["query_results"][0]["answerable"] = False
    with pytest.raises(ValueError):
        MODULE.select_cases(report, fixture, "native")


def test_absent_cases_require_explicit_abstention_and_no_answer_or_citations():
    query = {"answerable": False}
    assert MODULE.category(query) == "absent"
    assert MODULE.passing({"status": "insufficient_evidence"}, query, "native")
    assert MODULE.passing({"status": "abstained"}, query, "comparison")
    for row in ({"status": "error"}, {"status": "partial"},
                {"status": "insufficient_evidence", "answer": "invented"},
                {"status": "insufficient_evidence", "citations": ["ev1"]}):
        assert not MODULE.passing(row, query, "native")


def test_incomplete_or_wrong_citation_review_is_mandatory():
    report, fixture = invented()
    row = report["query_results"][0]
    row["review"]["citations"] = [{"evidence_id": "wrong", "supports_claim": True}]
    assert not MODULE.passing(row, fixture["queries"][0], "native")
    correction = {"answerable": True, "answer_rubric": {"must_not_cite_values": ["old"]}}
    assert MODULE.category(correction) == "correction"


def test_report_cannot_substitute_a_different_fixture(tmp_path):
    report, _ = invented()
    report["plan"]["fixture_sha256"] = "0" * 64
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text("{}")
    with pytest.raises(ValueError, match="fixture hash"):
        MODULE.prepare(report_path, fixture_path, "native")


def test_export_preserves_missing_cases_original_spans_and_unreviewed_status(tmp_path, monkeypatch):
    fixture = {
        "histories": [{"history_label": "invented", "messages": [
            {"content": "The invented fact is blue.", "source_id": "test", "seq_in_history": 1}
        ]}],
        "queries": [
            {"query_id": f"q{i}", "history_label": "invented", "split": "held_out",
             "answerable": i < 32, "query_text": "What is the invented fact?",
             "answer_rubric": {"correct_value": "blue", "must_not_cite_values": []} if i < 32 else None,
             "required_evidence_units": [{"unit_id": "u1", "message_index": 0,
                                          "acceptable_span": {"start": 21, "end": 25}}] if i < 32 else []}
            for i in range(40)
        ],
    }
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture))
    report = {"plan": {"trials": 3, "fixture_sha256": MODULE.digest(fixture_path)},
              "query_results": [], "status": "INCOMPLETE"}
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    packet = MODULE.prepare(report_path, fixture_path, "native")
    assert packet["human_review_status"] == "NOT_RUN"
    assert packet["expected_cases"] == packet["selected_cases"] == 240
    assert packet["reported_cases"] == 0
    assert packet["report"]["sha256"] == MODULE.digest(report_path)
    assert packet["items"][0]["required_original_evidence"][0]["acceptable_text"] == "blue"
    assert all(item["human_decision"] is None for item in packet["items"])
    output_path = tmp_path / "packet.json"
    monkeypatch.setattr("sys.argv", ["prepare_review.py", "--surface", "native", "--report",
                                     str(report_path), "--fixture", str(fixture_path),
                                     "--out", str(output_path)])
    MODULE.main()
    original = output_path.read_bytes()
    with pytest.raises(FileExistsError):
        MODULE.main()
    assert output_path.read_bytes() == original
