"""Run installed artifacts on an operator-reserved Linux reference host; fail closed elsewhere.

No provisioning or model calls. --preflight inventories the host without installation or cache drops.
An operator attestation complements observed hardware; this script cannot prove absence of noisy
neighbors on a cloud hypervisor. Review both the reservation evidence and the machine report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def inspect_host(work: Path, attestation: dict) -> tuple[dict, list[str]]:
    observed = {"system": platform.system(), "machine": platform.machine(),
                "platform": platform.platform(), "cpu_count": os.cpu_count(),
                "work_directory": str(work), "attestation": attestation}
    failures = []
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        return observed, ["requires Linux x86_64; current host is not the reference runner"]
    observed["affinity_cpu_count"] = len(os.sched_getaffinity(0))
    observed["memory_bytes"] = int(next(line.split()[1] for line in
        Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:"))) * 1024
    observed["load_average"] = list(os.getloadavg())
    observed["boot_id_sha256"] = sha256(Path("/proc/sys/kernel/random/boot_id"))
    observed["cgroup"] = Path("/proc/self/cgroup").read_text()
    observed["cpu_details"] = capture(["lscpu", "--json"])
    observed["mount"] = json.loads(capture(["findmnt", "-J", "-T", str(work), "-o", "SOURCE,FSTYPE,TARGET"]))
    source = observed["mount"]["filesystems"][0]["source"].split("[")[0]
    if not source.startswith("/dev/"):
        failures.append("work directory is not on an identifiable local block device")
    else:
        observed["block_device"] = json.loads(capture([
            "lsblk", "-J", "-o", "NAME,TYPE,ROTA,SIZE,MODEL", source]))
        if any(bool(device["rota"]) for device in observed["block_device"]["blockdevices"]):
            failures.append("work directory is on a rotational device")
    container = subprocess.run(["systemd-detect-virt", "--container"], capture_output=True, text=True)
    observed["container_detection"] = container.stdout.strip()
    if container.returncode == 0 or Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        failures.append("container host is not accepted as dedicated reference evidence")
    if observed["cpu_count"] != 4 or observed["affinity_cpu_count"] != 4:
        failures.append("requires exactly four host CPUs and four available CPUs")
    if not 7 * 1024**3 <= observed["memory_bytes"] <= 8.5 * 1024**3:
        failures.append("requires an 8-GiB host (allowing OS-reserved memory)")
    for name in ("runner_id", "operator", "reservation_id", "isolation_evidence"):
        if not isinstance(attestation.get(name), str) or not attestation[name].strip():
            failures.append(f"missing operator attestation: {name}")
    for name in ("exclusive", "local_ssd"):
        if attestation.get(name) is not True:
            failures.append(f"operator must attest {name}=true")
    try:
        start = datetime.fromisoformat(attestation["reservation_start_utc"])
        end = datetime.fromisoformat(attestation["reservation_end_utc"])
        if start.tzinfo is None or end.tzinfo is None or not start <= datetime.now(UTC) <= end:
            failures.append("exclusive runner reservation is not currently active")
    except (KeyError, ValueError, TypeError):
        failures.append("missing/invalid reservation_start_utc and reservation_end_utc")
    return observed, failures


def validate_report(report: dict, messages: int) -> list[str]:
    """A threshold summary alone must not pass an incomplete or wrong-size run."""
    failures = []
    if report["fixture"]["total_messages"] != messages or report["fixture"]["seed"] != 7:
        failures.append("wrong fixture")
    for name, expected in (("store_open_ms", 20), ("batch_ingest_100_ms", messages // 100),
                           ("lexical_search_top8_ms", 1000)):
        metric = report["results"][name]
        if metric["trials"] != expected:
            failures.append(f"incomplete {name}")
        if messages == 10_000 and not metric["pass"]:
            failures.append(f"reference threshold failed: {name}")
    for level in ("1", "4", "16"):
        metric = report["concurrency"]["search_top8_ms"][level]
        if metric["trials"] != 1000 or metric["errors"] != 0:
            failures.append(f"incomplete/failed concurrent search {level}")
    for level in ("4", "16"):
        metric = report["concurrency"]["ingest_100_ms"][level]
        if metric["trials"] != 32 or metric["errors"] != 0:
            failures.append(f"incomplete/failed concurrent ingest {level}")
    for name in ("process_cold", "filesystem_cold"):
        cold = report.get(name) or {}
        if cold.get("total_messages") != messages or len(cold.get("raw_trials", [])) != 20:
            failures.append(f"incomplete/wrong-size {name}")
            continue
        if any(trial.get("observed_messages") != messages for trial in cold["raw_trials"]):
            failures.append(f"measured history size differs in {name}")
        if messages == 10_000:
            if cold["first_search_top8_ms"]["p95"] > 100 or cold["store_open_ms"]["p95"] > 2000:
                failures.append(f"reference threshold failed: {name}")
    return failures


def verify_installed(wheel: Path, tarball: Path, site: Path, package: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.endswith("/") or name.endswith(".dist-info/RECORD"):
                continue
            if (site / name).read_bytes() != archive.read(name):
                raise ValueError(f"installed wheel mismatch: {name}")
    with tarfile.open(tarball) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            relative = Path(member.name).relative_to("package")
            stream = archive.extractfile(member)
            assert stream is not None
            if (package / relative).read_bytes() != stream.read():
                raise ValueError(f"installed npm mismatch: {relative}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--npm-tarball", required=True, type=Path)
    parser.add_argument("--runner-attestation", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path,
                        help="existing directory on reserved local SSD")
    parser.add_argument("--out-dir", required=True, type=Path, help="new report directory")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--allow-drop-page-cache", action="store_true",
                        help="authorize host-wide sync + drop_caches=3 during the exclusive reservation")
    args = parser.parse_args()
    wheel, tarball = args.wheel.resolve(), args.npm_tarball.resolve()
    work, output = args.work_dir.resolve(strict=True), args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    attestation = json.loads(args.runner_attestation.read_text())
    inputs = {str(path): sha256(path) for path in (wheel, tarball, args.runner_attestation,
        Path(__file__), BENCHMARKS / "reference_fixture_benchmark.py",
        BENCHMARKS / "reference_fixture_benchmark.mjs", BENCHMARKS / "generate_reference_fixture.py")}
    report = {"status": "BLOCKED", "started_at": datetime.now(UTC).isoformat(),
              "source_commit": capture(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
              "tracked_changes": capture(["git", "-C", str(ROOT), "status", "--short"]),
              "input_sha256": inputs, "model_calls": 0, "reports": {}, "failures": []}
    try:
        observed, failures = inspect_host(work, attestation)
        report["environment_before"] = observed
        report["failures"] = failures
        if failures or args.preflight:
            report["status"] = "BLOCKED" if failures else "PREFLIGHT_ONLY"
            return 2 if failures else 0
        if not args.allow_drop_page_cache:
            report["failures"].append("execution requires --allow-drop-page-cache on the reserved host")
            return 2
        with tempfile.TemporaryDirectory(prefix="cci-reference-", dir=work) as temporary:
            scratch = Path(temporary)
            env = {**os.environ, "TMPDIR": str(scratch)}

            def run(name: str, command: list[str]) -> int:
                with (output / f"{name}.log").open("w") as log:
                    return subprocess.run(command, cwd=scratch, env=env, stdout=log,
                                          stderr=subprocess.STDOUT, check=False).returncode

            python = scratch / "venv/bin/python"
            for name, command in (
                ("venv", [sys.executable, "-m", "venv", str(scratch / "venv")]),
                ("install-wheel", [str(python), "-m", "pip", "install", str(wheel)]),
                ("install-npm", ["npm", "install", "--prefix", str(scratch / "npm"), "--omit=optional",
                                 "--no-audit", "--no-fund", str(tarball)]),
            ):
                if run(name, command):
                    raise RuntimeError(f"{name} failed; inspect its retained log")
            site = Path(capture([str(python), "-I", "-c",
                "import pathlib,cci; print(pathlib.Path(cci.__file__).resolve().parent.parent)"]))
            package = scratch / "npm/node_modules/chat-context-index"
            verify_installed(wheel, tarball, site, package)
            report["installed_artifact_contents_verified"] = True
            report["python_dependencies"] = capture([str(python), "-m", "pip", "freeze"])
            report["node_version"] = capture(["node", "--version"])
            report["npm_dependencies"] = capture(["npm", "ls", "--prefix", str(scratch / "npm"), "--json"])
            for count in (10_000, 100_000):
                fixture = scratch / f"fixture-{count}.json"
                subprocess.run([sys.executable, str(BENCHMARKS / "generate_reference_fixture.py"),
                    "--messages", str(count), "--queries", "1000", "--out", str(fixture)], check=True)
                report.setdefault("fixture_sha256", {})[str(count)] = sha256(fixture)
                common = ["--concurrency", "1,4,16", "--cold-trials", "20",
                          "--filesystem-cold-trials", "20", "--label", "dedicated-reference-candidate"]
                for language, command, artifact in (
                    ("python", [str(python), "-I", str(BENCHMARKS / "reference_fixture_benchmark.py"),
                                "--messages", str(count), "--queries", "1000"], wheel),
                    ("typescript", ["node", str(BENCHMARKS / "reference_fixture_benchmark.mjs"),
                                    "--fixture", str(fixture), "--package", str(package / "dist/index.js")],
                     tarball),
                ):
                    name = f"{language}-{count}"
                    destination = output / f"{name}.json"
                    code = run(name, [*command, *common, "--artifact", inputs[str(artifact)],
                                     "--out", str(destination)])
                    if not destination.exists():
                        raise RuntimeError(f"{name} exited {code} without a report")
                    failures = validate_report(json.loads(destination.read_text()), count)
                    if code not in (0, 1):
                        failures.append(f"unexpected benchmark exit {code}")
                    report["reports"][name] = {"sha256": sha256(destination), "failures": failures}
                    report["failures"].extend(f"{name}: {failure}" for failure in failures)
        report["environment_after"], after_failures = inspect_host(work, attestation)
        report["failures"].extend(after_failures)
        if any(sha256(Path(name)) != digest for name, digest in inputs.items()):
            report["failures"].append("benchmark inputs changed during execution")
        report["status"] = "FAIL" if report["failures"] else "PASS_PENDING_RUNNER_EVIDENCE_REVIEW"
        return 1 if report["failures"] else 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, RuntimeError) as error:
        report["failures"].append(f"{type(error).__name__}: {error}")
        return 2
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_json(output / "manifest.json", report)
        print(json.dumps({"status": report["status"], "manifest": str(output / "manifest.json"),
                          "failures": report["failures"]}))


if __name__ == "__main__":
    raise SystemExit(main())
