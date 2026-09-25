"""Measure one installed candidate on a GitHub-hosted Linux VM, without a reference-host claim.

The existing fixed-hardware reference gate remains unchanged. This runner records observed
hosted targets separately; it neither approves a release nor calls a model provider.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

TOOLING = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controls = load("hosted_release_controls", TOOLING / "scripts/release_controls.py")
reference = load("hosted_reference_validation", TOOLING / "benchmarks/run_reference_gate.py")


def verify_ci_run(run: dict, run_id: str, sha: str, repository: str) -> None:
    controls.require(str(run.get("id")) == run_id and run.get("head_sha") == sha,
                     "Candidate CI run/source mismatch")
    controls.require(run.get("status") == "completed" and run.get("conclusion") == "success",
                     "Candidate CI did not complete successfully")
    controls.require(run.get("path") == ".github/workflows/ci.yml"
                     and run.get("repository", {}).get("full_name") == repository,
                     "Candidate run is not this repository's CI workflow")


def observe_host(work: Path) -> dict:
    """Capture actual resources, including unsuccessful probes; never invent isolation evidence."""
    result = {"system": platform.system(), "machine": platform.machine(),
              "platform": platform.platform(), "cpu_count": os.cpu_count(),
              "affinity_cpu_count": len(os.sched_getaffinity(0)),
              "load_average": list(os.getloadavg()),
              "memory_bytes": int(next(line.split()[1] for line in
                  Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:"))) * 1024,
              "runner": {key: os.environ.get(key) for key in
                  ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "GITHUB_REPOSITORY",
                   "RUNNER_NAME", "RUNNER_ARCH", "RUNNER_ENVIRONMENT", "ImageOS", "ImageVersion")}}
    for name, command in (
        ("cpu_details", ["lscpu", "--json"]),
        ("mount", ["findmnt", "-J", "-T", str(work), "-o", "SOURCE,FSTYPE,TARGET"]),
        ("block_devices", ["lsblk", "-J", "-o", "NAME,TYPE,ROTA,SIZE,MODEL"]),
        ("container_detection", ["systemd-detect-virt", "--container"]),
    ):
        response = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        result[name] = {"exit_code": response.returncode, "stdout": response.stdout.strip(),
                        "stderr": response.stderr.strip()}
    return result


def finite_nonnegative(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def assess(report: dict | None, messages: int, code: int | None) -> dict:
    """Keep incomplete execution and hosted target results distinct from reference qualification."""
    result = {"status": "INCOMPLETE", "fixed_reference_gate": "NOT_QUALIFIED",
              "claim_scope": "observed GitHub-hosted hardware only", "failures": []}
    if report is None or code not in (0, 1):
        result["failures"] = ["missing benchmark report or abnormal benchmark exit"]
        return result
    try:
        failures = reference.validate_report(report, messages)
        timings = list(report["results"].items())
        for series, levels in (("search_top8_ms", ("1", "4", "16")), ("ingest_100_ms", ("4", "16"))):
            timings.extend((f"{series} concurrency {level}", report["concurrency"][series][level])
                           for level in levels)
        for name, metric in timings:
            if not isinstance(metric, dict) or not all(
                finite_nonnegative(metric.get(key)) for key in ("p50", "p95")
            ):
                failures.append(f"incomplete/invalid timing: {name}")
        for cold_mode in ("process_cold", "filesystem_cold"):
            cold = report[cold_mode]
            for name, threshold in (("first_search_top8_ms", 100), ("store_open_ms", 2000)):
                value = cold[name]["p95"]
                if not finite_nonnegative(value):
                    failures.append(f"incomplete/invalid {cold_mode} timing")
                elif messages == 10_000 and value > threshold:
                    failures.append(f"hosted target exceeded: {cold_mode} {name}")
        # Both unchanged benchmark CLIs return 1 only when one of these targets fails.
        # At 100k that is an observation, but an unexplained nonzero exit is incomplete.
        cli_passes = []
        for name, threshold in (("store_open_ms", 2000), ("batch_ingest_100_ms", 500),
                                ("lexical_search_top8_ms", 100)):
            metric = report["results"][name]
            if finite_nonnegative(metric["p95"]):
                passed = metric["p95"] <= threshold
                cli_passes.append(passed)
                if metric.get("pass") is not passed:
                    failures.append(f"incomplete/inconsistent threshold result: {name}")
                if messages == 10_000 and not passed:
                    failures.append(f"hosted target exceeded: {name}")
        if len(cli_passes) == 3 and code != (0 if all(cli_passes) else 1):
            failures.append("incomplete benchmark exit does not match threshold results")
        if report["environment"].get("dedicated_linux_runner") is not False:
            failures.append("unexpected dedicated-reference claim")
        size = report["fixture_stats"]["store_bytes_after_generation"]
        if not finite_nonnegative(size) or size == 0:
            failures.append("incomplete/invalid database size")
        memory = report["resources"].get("peak_rss_bytes", report["resources"].get("max_rss_kib", 0))
        if not finite_nonnegative(memory) or memory == 0:
            failures.append("incomplete/invalid memory measurement")
    except (KeyError, TypeError, ValueError) as error:
        result["failures"] = [f"incomplete benchmark schema: {type(error).__name__}"]
        return result
    result["failures"] = list(dict.fromkeys(failures))
    incomplete = any("incomplete" in failure or "wrong fixture" in failure for failure in failures)
    result["status"] = ("INCOMPLETE" if incomplete else
                        "FAIL_HOSTED_TARGETS" if failures else "PASS_HOSTED_OBSERVATION")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ci-run", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--language", choices=("python", "typescript"), required=True)
    parser.add_argument("--messages", type=int, choices=(10_000, 100_000), required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--allow-drop-page-cache", action="store_true")
    args = parser.parse_args()
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"status": "INCOMPLETE", "fixed_reference_gate": "NOT_QUALIFIED",
                "claim_scope": "observed GitHub-hosted hardware only", "model_calls": 0,
                "candidate_sha": args.sha, "candidate_ci_run_id": args.run_id,
                "started_at": datetime.now(UTC).isoformat(), "commands": [], "failures": [],
                "execution_deadline_seconds": 4800}
    deadline = time.monotonic() + 4800
    benchmark_path = output / "benchmark.json"
    inputs = {}
    try:
        work = args.work_dir.resolve(strict=True)
        source = args.source.resolve(strict=True)
        for path in (
            Path(__file__), TOOLING / "benchmarks/run_reference_gate.py",
            TOOLING / "scripts/release_controls.py", source / "benchmarks/reference_fixture_benchmark.py",
            source / "benchmarks/reference_fixture_benchmark.mjs",
            source / "benchmarks/generate_reference_fixture.py", args.ci_run.resolve(),
        ):
            inputs[path] = reference.sha256(path)
        controls.require(platform.system() == "Linux" and platform.machine() == "x86_64",
                         "Hosted performance execution requires Linux x86_64")
        controls.require(os.environ.get("GITHUB_ACTIONS") == "true"
                         and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
                         "Execution must run in its own GitHub-hosted VM job")
        controls.require(args.allow_drop_page_cache, "Explicit VM page-cache eviction permission is required")
        verify_ci_run(json.loads(args.ci_run.read_text()), args.run_id, args.sha, args.repository)
        controls.require(reference.capture(["git", "-C", str(source), "rev-parse", "HEAD"]) == args.sha,
                         "Source checkout differs from candidate commit")
        controls.require(not reference.capture(["git", "-C", str(source), "status", "--porcelain",
                                                "--untracked-files=no"]),
                         "Candidate source has tracked edits")
        version = json.loads((source / "packages/typescript/package.json").read_text())["version"]
        identity, archives = controls.inspect_candidate(args.candidate, args.sha, args.run_id,
                                                        f"v{version}", root=source)
        manifest["candidate"] = identity
        wheel = archives["python"][0].resolve()
        tarball = archives["npm"][0].resolve()
        inputs.update({path: reference.sha256(path) for path in (wheel, tarball)})
        manifest["environment_before"] = observe_host(work)
        controls.require(manifest["environment_before"]["container_detection"]["exit_code"] == 1,
                         "Container detection must confirm a non-container VM")
        manifest["tooling_commit"] = reference.capture(["git", "-C", str(TOOLING), "rev-parse", "HEAD"])
        with tempfile.TemporaryDirectory(prefix="cci-hosted-", dir=work) as temporary:
            scratch = Path(temporary)
            env = {**os.environ, "TMPDIR": str(scratch)}

            def run(name: str, command: list[str], timeout: int = 600) -> int:
                remaining = deadline - time.monotonic()
                controls.require(remaining > 0, "Hosted benchmark execution deadline exhausted")
                effective_timeout = min(timeout, remaining)
                entry = {"name": name, "command": command, "exit_code": None,
                         "timeout_seconds": effective_timeout}
                manifest["commands"].append(entry)
                with (output / f"{name}.log").open("w") as log:
                    try:
                        entry["exit_code"] = subprocess.run(command, cwd=scratch, env=env, stdout=log,
                            stderr=subprocess.STDOUT, timeout=effective_timeout, check=False).returncode
                    finally:
                        reference.write_json(output / "manifest.json", manifest)
                return entry["exit_code"]

            python = scratch / "venv/bin/python"
            for name, command in (
                ("venv", [sys.executable, "-m", "venv", str(scratch / "venv")]),
                ("install-wheel", [str(python), "-m", "pip", "install", str(wheel)]),
                ("install-npm", ["npm", "install", "--prefix", str(scratch / "npm"), "--omit=optional",
                                 "--no-audit", "--no-fund", str(tarball)]),
            ):
                controls.require(run(name, command) == 0, f"{name} failed; retained log has details")
            site = Path(reference.capture([str(python), "-I", "-c",
                "import pathlib,cci; print(pathlib.Path(cci.__file__).resolve().parent.parent)"]))
            package = scratch / "npm/node_modules/chat-context-index"
            reference.verify_installed(wheel, tarball, site, package)
            manifest["installed_artifact_contents_verified"] = True
            manifest["python_dependencies"] = reference.capture([str(python), "-m", "pip", "freeze"])
            manifest["node_version"] = reference.capture(["node", "--version"])
            manifest["npm_dependencies"] = json.loads(reference.capture(
                ["npm", "ls", "--prefix", str(scratch / "npm"), "--json"]))
            fixture = scratch / "fixture.json"
            controls.require(run("fixture", [sys.executable,
                str(source / "benchmarks/generate_reference_fixture.py"), "--messages", str(args.messages),
                "--queries", "1000", "--out", str(fixture)]) == 0, "Fixture generation failed")
            manifest["fixture_sha256"] = reference.sha256(fixture)
            if args.language == "python":
                command = [str(python), "-I", str(source / "benchmarks/reference_fixture_benchmark.py"),
                           "--messages", str(args.messages), "--queries", "1000"]
                artifact = wheel
            else:
                command = ["node", str(source / "benchmarks/reference_fixture_benchmark.mjs"),
                           "--fixture", str(fixture), "--package", str(package / "dist/index.js")]
                artifact = tarball
            code = run("benchmark", [*command, "--concurrency", "1,4,16", "--cold-trials", "20",
                "--filesystem-cold-trials", "20", "--label", "github-hosted-observation",
                "--artifact", inputs[artifact], "--out", str(benchmark_path)], timeout=4800)
            report = json.loads(benchmark_path.read_text()) if benchmark_path.exists() else None
            if report is not None:
                controls.require(report.get("environment", {}).get("artifact") == inputs[artifact],
                                 "Benchmark report artifact identity mismatch")
            manifest.update(assess(report, args.messages, code))
        manifest["environment_after"] = observe_host(work)
        controls.require(all(reference.sha256(path) == digest for path, digest in inputs.items()),
                         "Inputs changed during execution")
        return 0 if manifest["status"] == "PASS_HOSTED_OBSERVATION" else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        manifest["status"] = "INCOMPLETE"
        manifest["failures"].append(f"{type(error).__name__}: {error}")
        return 2
    finally:
        if "environment_before" in manifest and "environment_after" not in manifest:
            try:
                manifest["environment_after"] = observe_host(work)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                manifest["environment_after_error"] = type(error).__name__
        manifest["input_sha256"] = {str(path): digest for path, digest in inputs.items()}
        manifest["benchmark_sha256"] = reference.sha256(benchmark_path) if benchmark_path.exists() else None
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        reference.write_json(output / "manifest.json", manifest)
        print(json.dumps({"status": manifest["status"], "manifest": str(output / "manifest.json"),
                          "fixed_reference_gate": "NOT_QUALIFIED", "failures": manifest["failures"]}))


if __name__ == "__main__":
    raise SystemExit(main())
