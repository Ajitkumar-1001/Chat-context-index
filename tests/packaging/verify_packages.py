"""Build fresh artifacts and verify installed memory APIs outside the source checkout.

Run with Python build tooling and npm development dependencies installed. Network access
may be needed to install runtime dependencies. No model credentials or provider calls are used.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def run(args, cwd, *, consumer=False):
    env = os.environ.copy()
    if consumer:
        for name in ("PYTHONPATH", "PYTHONHOME", "NODE_PATH", "NODE_OPTIONS"):
            env.pop(name, None)
        for name in list(env):
            if name.startswith(("CCI_", "OPENAI_", "ANTHROPIC_", "GEMINI_")):
                env.pop(name)
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=300)
    if result.returncode:
        sys.stderr.write(result.stdout + result.stderr)
        result.check_returncode()
    return result.stdout


def source_fingerprint(directory, pattern):
    digest = hashlib.sha256()
    for path in sorted(directory.glob(pattern)):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def wheel_payload(path: Path) -> dict[str, bytes]:
    """Compare installed content, independent of zip timestamps and RECORD ordering."""
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()
                if not name.endswith("/RECORD")}


def verify(work, artifacts_out):
    assert not work.resolve().is_relative_to(ROOT)
    artifacts = work / "artifacts"
    artifacts.mkdir()
    run([sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", artifacts], ROOT / "packages/python")
    run(["npm", "run", "build"], ROOT / "packages/typescript")
    packed = json.loads(run(["npm", "pack", "--json", "--pack-destination", artifacts],
                            ROOT / "packages/typescript"))
    wheels = list(artifacts.glob("*.whl"))
    assert len(wheels) == len(packed) == 1
    wheel, tarball = wheels[0], artifacts / packed[0]["filename"]
    sdists = list(artifacts.glob("*.tar.gz"))
    assert len(sdists) == 1
    sdist = sdists[0]
    extracted = work / "sdist-source"
    extracted.mkdir()
    with tarfile.open(sdist) as archive:
        archive.extractall(extracted, filter="data")
    sources = list(extracted.iterdir())
    assert len(sources) == 1 and sources[0].is_dir()
    rebuilt_dir = artifacts / "wheel-from-sdist"
    # build creates a fresh isolated PEP 517 environment; only sdist content is present here.
    run([sys.executable, "-m", "build", "--wheel", "--outdir", rebuilt_dir], sources[0])
    rebuilt_wheels = list(rebuilt_dir.glob("*.whl"))
    assert len(rebuilt_wheels) == 1
    rebuilt_wheel = rebuilt_wheels[0]
    assert wheel_payload(wheel) == wheel_payload(rebuilt_wheel), "sdist rebuilt different installed content"
    license_text = (ROOT / "LICENSE").read_bytes()
    upstream_text = (ROOT / "UPSTREAM.md").read_bytes()
    with zipfile.ZipFile(wheel) as archive:
        assert "cci/py.typed" in archive.namelist()
        assert archive.read("cci/UPSTREAM.md") == upstream_text
        license_paths = [name for name in archive.namelist() if name.endswith("/licenses/LICENSE")]
        assert len(license_paths) == 1 and archive.read(license_paths[0]) == license_text
        metadata_path = next(name for name in archive.namelist() if name.endswith("/METADATA"))
        metadata = archive.read(metadata_path).decode()
        assert "prepare_context" in metadata and f"Version: {packed[0]['version']}\n" in metadata
    with tarfile.open(tarball) as archive:
        assert archive.extractfile("package/LICENSE").read() == license_text
        assert archive.extractfile("package/UPSTREAM.md").read() == upstream_text
        assert b"prepareContext" in archive.extractfile("package/README.md").read()

    node_dir = work / "node-consumer"
    node_dir.mkdir()
    commands = {}
    for label, artifact in (("python", wheel), ("python_sdist", rebuilt_wheel)):
        python_dir = work / f"{label}-consumer"
        python_dir.mkdir()
        run([sys.executable, "-m", "venv", python_dir / "venv"], work)
        python = python_dir / "venv/bin/python"
        run([python, "-m", "pip", "install", "--disable-pip-version-check", artifact], python_dir, consumer=True)
        shutil.copyfile(HERE / "memory_consumer.py", python_dir / "memory_consumer.py")
        shutil.copyfile(ROOT / "spec/fixtures/tree-memory.json", python_dir / "tree-memory.json")
        commands[label] = ([python, "-I", "memory_consumer.py"], python_dir)
        run([python, "-I", "-c", "import importlib.util; "
             "assert all(importlib.util.find_spec(n) is None for n in ('redis', 'openai', 'anthropic'))"],
            python_dir, consumer=True)
    (node_dir / "package.json").write_text(json.dumps({
        "name": "cci-installed-memory-check", "version": "0.0.0", "private": True, "type": "module",
    }))
    run(["npm", "install", "--omit=optional", "--no-audit", "--no-fund", tarball], node_dir, consumer=True)
    shutil.copyfile(HERE / "memory_consumer.mjs", node_dir / "memory_consumer.mjs")
    shutil.copyfile(ROOT / "spec/fixtures/tree-memory.json", node_dir / "tree-memory.json")
    shutil.copyfile(HERE / "memory_types.ts", node_dir / "memory_types.ts")
    run(["node", ROOT / "packages/typescript/node_modules/typescript/bin/tsc", "--noEmit", "--strict",
         "--target", "ES2022", "--module", "NodeNext", "--moduleResolution", "NodeNext", "memory_types.ts"],
        node_dir, consumer=True)

    commands["typescript"] = (["node", "memory_consumer.mjs"], node_dir)
    for label, (command, cwd) in commands.items():
        run([*command, "default", work / f"{label}-default.db"], cwd, consumer=True)
    checks = []
    for writer in commands:
        db = work / f"{writer}.db"
        command, cwd = commands[writer]
        seed = json.loads(run([*command, "seed", db], cwd, consumer=True))
        reads = []
        for reader, (command, cwd) in commands.items():
            # Each invocation starts a new process; no live store or provider survives seed.
            result = json.loads(run([*command, "read", db], cwd, consumer=True))
            assert result["history_id"] == seed["history_id"]
            assert result["message_ids"] == seed["message_ids"]
            reads.append(result)
            checks.append({"writer": writer, "reader": reader, "status": "PASS",
                           "package_path": result["package_path"], "source_sequences": result["sequences"]})
        for key in ("text", "sequences", "chunks"):
            assert all(read[key] == reads[0][key] for read in reads[1:]), f"cross-runtime mismatch: {key}"

    if artifacts_out:
        artifacts_out.mkdir(parents=True, exist_ok=True)
        for artifact in (wheel, tarball, sdist, rebuilt_wheel):
            destination = artifacts_out / artifact.relative_to(artifacts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact, destination)

    return {
        "status": "PASS", "timestamp": datetime.now(UTC).isoformat(),
        "method": (
            "Fresh wheel/sdist-derived-wheel/npm installs outside checkout; separate seed/read processes; "
            "deterministic provider"
        ),
        "python": platform.python_version(), "node": run(["node", "--version"], work).strip(),
        "platform": platform.platform(), "typescript_consumer_typecheck": "PASS",
        "machine": platform.machine(),
        "ci": {key: os.environ.get(key) for key in
               ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_REPOSITORY", "RUNNER_ARCH")},
        "sdist_installed_content_matches_wheel": "PASS",
        "default_cache_without_optional_dependencies_or_credentials": "PASS",
        "package_documentation_and_license": "PASS",
        "source_sha256": {
            "python": source_fingerprint(ROOT / "packages/python/src/cci", "*.py"),
            "typescript": source_fingerprint(ROOT / "packages/typescript/src", "*.ts"),
        },
        "artifacts": [{"filename": str(p.relative_to(artifacts)),
                       "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                      for p in (wheel, tarball, sdist, rebuilt_wheel)],
        "checks": checks,
        "not_measured": ["real-model retrieval quality", "answer correctness", "model tokens or cost"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write the completed verification report as JSON")
    parser.add_argument("--artifacts-out", type=Path, help="Copy verified wheel and npm archive here")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cci-installed-memory-") as directory:
        report = verify(Path(directory), args.artifacts_out)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
