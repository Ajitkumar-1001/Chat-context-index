"""Hosted measurements cannot silently close the fixed-hardware reference gate."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "benchmarks/run_hosted_benchmark.py"
SPEC = importlib.util.spec_from_file_location("hosted_benchmark", SCRIPT)
assert SPEC and SPEC.loader
hosted = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hosted)


def report(messages=10_000):
    cold = {"total_messages": messages, "raw_trials": [{"observed_messages": messages}] * 20,
            "first_search_top8_ms": {"p95": 90}, "store_open_ms": {"p95": 20}}
    return {
        "fixture": {"total_messages": messages, "seed": 7},
        "results": {
            "store_open_ms": {"trials": 20, "pass": True, "p50": 10, "p95": 20},
            "batch_ingest_100_ms": {"trials": messages // 100, "pass": True, "p50": 20, "p95": 30},
            "lexical_search_top8_ms": {"trials": 1000, "pass": True, "p50": 50, "p95": 90},
        },
        "concurrency": {
            "search_top8_ms": {
                str(c): {"trials": 1000, "errors": 0, "p50": 50, "p95": 90} for c in (1, 4, 16)
            },
            "ingest_100_ms": {
                str(c): {"trials": 32, "errors": 0, "p50": 20, "p95": 30} for c in (4, 16)
            },
        },
        "process_cold": dict(cold), "filesystem_cold": dict(cold),
        "environment": {"dedicated_linux_runner": False},
        "fixture_stats": {"store_bytes_after_generation": 1024},
        "resources": {"peak_rss_bytes": 4096},
    }


def test_complete_hosted_measurement_does_not_close_reference_gate():
    result = hosted.assess(report(), 10_000, 0)
    assert result["status"] == "PASS_HOSTED_OBSERVATION"
    assert result["fixed_reference_gate"] == "NOT_QUALIFIED"


def test_scale_cli_threshold_exit_is_not_misclassified_as_incomplete():
    value = report(100_000)
    value["results"]["lexical_search_top8_ms"].update({"pass": False, "p95": 500})
    value["filesystem_cold"]["first_search_top8_ms"] = {"p95": 600}
    assert hosted.assess(value, 100_000, 1)["status"] == "PASS_HOSTED_OBSERVATION"


@pytest.mark.parametrize("mode", ["process_cold", "filesystem_cold"])
def test_cold_latency_target_is_retained_at_10k(mode):
    value = report()
    value[mode]["first_search_top8_ms"] = {"p95": 101}
    assert hosted.assess(value, 10_000, 0)["status"] == "FAIL_HOSTED_TARGETS"


@pytest.mark.parametrize("code", [None, 0, 1, 2, -9])
def test_missing_report_never_passes(code):
    assert hosted.assess(None, 10_000, code)["status"] == "INCOMPLETE"


def test_abnormal_exit_never_passes_even_with_complete_report():
    assert hosted.assess(report(), 10_000, 2)["status"] == "INCOMPLETE"


@pytest.mark.parametrize("messages", [10_000, 100_000])
def test_unexplained_exit_one_cannot_pass(messages):
    assert hosted.assess(report(messages), messages, 1)["status"] == "INCOMPLETE"


@pytest.mark.parametrize("series,level", [("search_top8_ms", "16"), ("ingest_100_ms", "4")])
@pytest.mark.parametrize("percentile", ["p50", "p95"])
@pytest.mark.parametrize("invalid", [None, -1, float("nan"), float("inf")])
def test_concurrency_timings_must_be_present_finite_and_nonnegative(series, level, percentile, invalid):
    value = report(100_000)
    metric = value["concurrency"][series][level]
    if invalid is None:
        del metric[percentile]
    else:
        metric[percentile] = invalid
    assert hosted.assess(value, 100_000, 0)["status"] == "INCOMPLETE"


@pytest.mark.parametrize("field", ["peak_rss_bytes", "max_rss_kib"])
@pytest.mark.parametrize("invalid", [None, 0, -1, float("nan"), float("inf")])
def test_rss_requires_finite_positive_measurement(field, invalid):
    value = report(100_000)
    value["resources"] = {field: invalid}
    assert hosted.assess(value, 100_000, 0)["status"] == "INCOMPLETE"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf")])
def test_database_size_requires_finite_positive_measurement(invalid):
    value = report(100_000)
    value["fixture_stats"]["store_bytes_after_generation"] = invalid
    assert hosted.assess(value, 100_000, 0)["status"] == "INCOMPLETE"


@pytest.mark.parametrize("missing", ["work", "source", "ci_run", "benchmark"])
def test_missing_inputs_leave_incomplete_manifest_without_execution(tmp_path, monkeypatch, missing):
    work, source, output = tmp_path / "work", tmp_path / "source", tmp_path / "output"
    work.mkdir()
    (source / "benchmarks").mkdir(parents=True)
    for name in ("reference_fixture_benchmark.py", "reference_fixture_benchmark.mjs",
                 "generate_reference_fixture.py"):
        (source / "benchmarks" / name).write_text("synthetic input\n")
    ci_run_path = tmp_path / "candidate-run.json"
    ci_run_path.write_text("{}")
    if missing == "work":
        work = tmp_path / "absent-work"
    elif missing == "source":
        source = tmp_path / "absent-source"
    elif missing == "ci_run":
        ci_run_path.unlink()
    else:
        (source / "benchmarks/reference_fixture_benchmark.mjs").unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("missing inputs must abort before installation or benchmark execution")

    monkeypatch.setattr(hosted.subprocess, "run", forbidden)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--candidate", str(tmp_path / "candidate"),
                        "--source", str(source), "--ci-run", str(ci_run_path), "--sha", "a" * 40,
                        "--run-id", "123", "--repository", "owner/repo", "--language", "python",
                        "--messages", "10000", "--work-dir", str(work), "--out-dir", str(output)])
    assert hosted.main() == 2
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "INCOMPLETE"
    assert manifest["fixed_reference_gate"] == "NOT_QUALIFIED"
    assert manifest["commands"] == [] and manifest["model_calls"] == 0
    assert any("FileNotFoundError" in failure for failure in manifest["failures"])
    assert manifest["finished_at"] and manifest["benchmark_sha256"] is None


def test_incomplete_trial_count_is_reported_as_incomplete():
    value = report()
    value["concurrency"]["search_top8_ms"]["16"]["trials"] = 999
    assert hosted.assess(value, 10_000, 0)["status"] == "INCOMPLETE"


@pytest.mark.parametrize("mutation", ["missing_cold", "partial", "errors", "claim", "size", "memory", "nan"])
def test_partial_malformed_or_misleading_reports_do_not_pass(mutation):
    value = report()
    if mutation == "missing_cold":
        del value["filesystem_cold"]
    elif mutation == "partial":
        value["concurrency"]["search_top8_ms"]["16"]["trials"] = 999
    elif mutation == "errors":
        value["concurrency"]["ingest_100_ms"]["4"]["errors"] = 1
    elif mutation == "claim":
        value["environment"]["dedicated_linux_runner"] = True
    elif mutation == "size":
        value["fixture_stats"]["store_bytes_after_generation"] = 0
    elif mutation == "memory":
        value["resources"] = {}
    else:
        value["results"]["lexical_search_top8_ms"]["p95"] = float("nan")
    assert hosted.assess(value, 10_000, 0)["status"] != "PASS_HOSTED_OBSERVATION"


def ci_run():
    return {"id": 123, "head_sha": "a" * 40, "status": "completed", "conclusion": "success",
            "path": ".github/workflows/ci.yml", "repository": {"full_name": "owner/repo"}}


def test_successful_candidate_ci_identity():
    hosted.verify_ci_run(ci_run(), "123", "a" * 40, "owner/repo")


@pytest.mark.parametrize("field,value", [
    ("id", 124), ("head_sha", "b" * 40), ("status", "in_progress"),
    ("conclusion", "failure"), ("path", ".github/workflows/other.yml"),
    ("repository", {"full_name": "other/repo"}),
])
def test_wrong_or_unsuccessful_ci_cannot_supply_candidate(field, value):
    run = ci_run()
    run[field] = value
    with pytest.raises(ValueError):
        hosted.verify_ci_run(run, "123", "a" * 40, "owner/repo")
