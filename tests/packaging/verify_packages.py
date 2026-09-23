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


def verify(work, artifacts_out):
    assert not work.resolve().is_relative_to(ROOT)
    artifacts = work / "artifacts"
    artifacts.mkdir()
    run([sys.executable, "-m", "build", "--wheel", "--outdir", artifacts], ROOT / "packages/python")
    run(["npm", "run", "build"], ROOT / "packages/typescript")
    packed = json.loads(run(["npm", "pack", "--json", "--pack-destination", artifacts],
                            ROOT / "packages/typescript"))
    wheels = list(artifacts.glob("*.whl"))
    assert len(wheels) == len(packed) == 1
    wheel, tarball = wheels[0], artifacts / packed[0]["filename"]
    license_text = (ROOT / "LICENSE").read_bytes()
    with zipfile.ZipFile(wheel) as archive:
        license_paths = [name for name in archive.namelist() if name.endswith("/licenses/LICENSE")]
        assert len(license_paths) == 1 and archive.read(license_paths[0]) == license_text
        metadata_path = next(name for name in archive.namelist() if name.endswith("/METADATA"))
        metadata = archive.read(metadata_path).decode()
        assert "prepare_context" in metadata and f"Version: {packed[0]['version']}\n" in metadata
    with tarfile.open(tarball) as archive:
        assert archive.extractfile("package/LICENSE").read() == license_text
        assert b"prepareContext" in archive.extractfile("package/README.md").read()

    python_dir, node_dir = work / "python-consumer", work / "node-consumer"
    python_dir.mkdir()
    node_dir.mkdir()
    run([sys.executable, "-m", "venv", python_dir / "venv"], work)
    python = python_dir / "venv/bin/python"
    run([python, "-m", "pip", "install", "--disable-pip-version-check", wheel], python_dir, consumer=True)
    (node_dir / "package.json").write_text(json.dumps({
        "name": "cci-installed-memory-check", "version": "0.0.0", "private": True, "type": "module",
    }))
    run(["npm", "install", "--no-audit", "--no-fund", tarball], node_dir, consumer=True)
    for directory, filename in [(python_dir, "memory_consumer.py"), (node_dir, "memory_consumer.mjs")]:
        shutil.copyfile(HERE / filename, directory / filename)
        shutil.copyfile(ROOT / "spec/fixtures/tree-memory.json", directory / "tree-memory.json")
    shutil.copyfile(HERE / "memory_types.ts", node_dir / "memory_types.ts")
    run(["node", ROOT / "packages/typescript/node_modules/typescript/bin/tsc", "--noEmit", "--strict",
         "--target", "ES2022", "--module", "NodeNext", "--moduleResolution", "NodeNext", "memory_types.ts"],
        node_dir, consumer=True)

    commands = {"python": ([python, "-I", "memory_consumer.py"], python_dir),
                "typescript": (["node", "memory_consumer.mjs"], node_dir)}
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
            assert reads[0][key] == reads[1][key], f"cross-runtime mismatch: {key}"

    if artifacts_out:
        artifacts_out.mkdir(parents=True, exist_ok=True)
        for artifact in (wheel, tarball):
            shutil.copyfile(artifact, artifacts_out / artifact.name)

    return {
        "status": "PASS", "timestamp": datetime.now(UTC).isoformat(),
        "method": (
            "Fresh wheel/npm installs outside checkout; separate seed/read processes; deterministic provider"
        ),
        "python": platform.python_version(), "node": run(["node", "--version"], work).strip(),
        "platform": platform.platform(), "typescript_consumer_typecheck": "PASS",
        "package_documentation_and_license": "PASS",
        "source_sha256": {
            "python": source_fingerprint(ROOT / "packages/python/src/cci", "*.py"),
            "typescript": source_fingerprint(ROOT / "packages/typescript/src", "*.ts"),
        },
        "artifacts": [{"filename": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                      for p in (wheel, tarball)],
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
