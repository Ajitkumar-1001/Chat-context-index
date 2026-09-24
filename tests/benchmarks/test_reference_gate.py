"""Prevent incomplete or mislabeled measurements from closing the reference gate."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "benchmarks/run_reference_gate.py"
SPEC = importlib.util.spec_from_file_location("reference_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def report(messages=10_000):
    cold = {"total_messages": messages, "raw_trials": [{"observed_messages": messages}] * 20,
            "first_search_top8_ms": {"p95": 90}, "store_open_ms": {"p95": 20}}
    return {
        "fixture": {"total_messages": messages, "seed": 7},
        "results": {"store_open_ms": {"trials": 20, "pass": True},
                    "batch_ingest_100_ms": {"trials": messages // 100, "pass": True},
                    "lexical_search_top8_ms": {"trials": 1000, "pass": True}},
        "concurrency": {
            "search_top8_ms": {str(c): {"trials": 1000, "errors": 0} for c in (1, 4, 16)},
            "ingest_100_ms": {str(c): {"trials": 32, "errors": 0} for c in (4, 16)},
        },
        "process_cold": dict(cold), "filesystem_cold": dict(cold),
    }


def test_complete_reference_report_passes():
    assert gate.validate_report(report(), 10_000) == []


@pytest.mark.parametrize("mode", ["process_cold", "filesystem_cold"])
def test_cold_search_must_measure_declared_history_and_pass(mode):
    value = report()
    value[mode]["total_messages"] = 16_400
    assert f"incomplete/wrong-size {mode}" in gate.validate_report(value, 10_000)
    value = report()
    value[mode]["first_search_top8_ms"] = {"p95": 101}
    assert f"reference threshold failed: {mode}" in gate.validate_report(value, 10_000)
    value = report()
    value[mode]["raw_trials"] = [{"observed_messages": 16_400}] * 20
    assert f"measured history size differs in {mode}" in gate.validate_report(value, 10_000)


def test_missing_cache_drop_and_partial_queries_fail():
    value = report()
    del value["filesystem_cold"]
    value["concurrency"]["search_top8_ms"]["4"]["trials"] = 999
    value["concurrency"]["ingest_100_ms"]["16"]["errors"] = 1
    failures = gate.validate_report(value, 10_000)
    assert "incomplete/wrong-size filesystem_cold" in failures
    assert "incomplete/failed concurrent search 4" in failures
    assert "incomplete/failed concurrent ingest 16" in failures


def test_scale_is_not_the_10k_latency_gate_but_still_requires_complete_trials():
    value = report(100_000)
    value["results"]["lexical_search_top8_ms"]["pass"] = False
    value["process_cold"]["first_search_top8_ms"] = {"p95": 500}
    assert gate.validate_report(value, 100_000) == []
    value["process_cold"]["raw_trials"] = []
    assert gate.validate_report(value, 100_000)


def test_operator_label_cannot_turn_a_mac_into_reference_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(gate.platform, "system", lambda: "Darwin")
    observed, failures = gate.inspect_host(tmp_path, {"exclusive": True, "local_ssd": True})
    assert observed["system"] == "Darwin"
    assert failures


def test_modified_installed_artifact_is_rejected(tmp_path):
    wheel = tmp_path / "candidate.whl"
    with gate.zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("cci/__init__.py", "original")
    site = tmp_path / "site"
    (site / "cci").mkdir(parents=True)
    (site / "cci/__init__.py").write_text("changed")
    with pytest.raises(ValueError, match="installed wheel mismatch"):
        gate.verify_installed(wheel, tmp_path / "candidate.tgz", site, tmp_path)
