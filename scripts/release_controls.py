"""Verify the exact CI-installed release archives; inventory licenses without publishing."""

import argparse
import hashlib
import json
import re
import shutil
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fingerprint(directory, pattern):
    digest = hashlib.sha256()
    for path in sorted(directory.glob(pattern)):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def unpack(artifacts, output):
    """Expand wheels explicitly: scanners do not necessarily recognize their ZIP extension."""
    paths = sorted(path for path in artifacts.rglob("*") if path.is_file())
    require(paths, "No archives to scan")
    output.mkdir()
    for index, path in enumerate(paths):
        destination = output / str(index)
        destination.mkdir()
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as archive:
                require(all((destination / name).resolve().is_relative_to(destination.resolve())
                            for name in archive.namelist()), "Unsafe wheel member path")
                archive.extractall(destination)
        elif path.name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(path) as archive:
                archive.extractall(destination, filter="data")
        else:
            raise ValueError(f"Unexpected archive type: {path.name}")
    return {"status": "PASS", "archives_unpacked": len(paths)}


def verify_artifacts(candidate, sha, run_id, tag, output, root=ROOT):
    """Reject stale reports, missing checks, changed archives, and unsafe paths before copying."""
    report_path = candidate / "reports/package-memory.json"
    report = json.loads(report_path.read_text())
    require(json.loads((candidate / "reports/artifact-secrets.json").read_text()) == [],
            "Archive secret scan did not pass")
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "Expected a full source commit")
    require(report.get("status") == "PASS", "Package verification did not pass")
    require(report["ci"]["GITHUB_SHA"] == sha, "Report source commit mismatch")
    require(report["ci"]["GITHUB_RUN_ID"] == str(run_id), "Report CI run mismatch")
    for field in ("sdist_installed_content_matches_wheel", "package_documentation_and_license",
                  "default_cache_without_optional_dependencies_or_credentials",
                  "typescript_consumer_typecheck"):
        require(report.get(field) == "PASS", f"Missing passing check: {field}")
    runtimes = {"python", "python_sdist", "typescript"}
    checks = report["checks"]
    require(len(checks) == 9 and all(check.get("status") == "PASS" for check in checks)
            and {(check["writer"], check["reader"]) for check in checks}
            == {(writer, reader) for writer in runtimes for reader in runtimes},
            "Incomplete installed-package matrix")
    for language, directory, pattern in (
        ("python", root / "packages/python/src/cci", "*.py"),
        ("typescript", root / "packages/typescript/src", "*.ts"),
    ):
        require(report["source_sha256"][language] == fingerprint(directory, pattern),
                f"{language} sources differ from installed-package verification")
    artifacts_dir = candidate / "verified-artifacts"
    entries = report["artifacts"]
    require(len(entries) == 4, "Expected wheel, source archive, npm archive and rebuilt wheel")
    paths = []
    for entry in entries:
        relative = Path(entry["filename"])
        require(not relative.is_absolute() and ".." not in relative.parts, "Unsafe artifact path")
        path = artifacts_dir / relative
        require(path.is_file() and not path.is_symlink()
                and path.resolve().is_relative_to(artifacts_dir.resolve()), "Missing or unsafe archive")
        require(hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"],
                f"Archive checksum mismatch: {relative}")
        paths.append(path)
    require(len(set(paths)) == 4, "Duplicate artifact entry")
    actual = {path for path in artifacts_dir.rglob("*") if path.is_file()}
    require(actual == set(paths), "Unexpected file in release artifacts")
    wheel = [path for path in paths if path.parent == artifacts_dir and path.suffix == ".whl"]
    sdist = [path for path in paths if path.parent == artifacts_dir and path.name.endswith(".tar.gz")]
    npm = [path for path in paths if path.parent == artifacts_dir and path.suffix == ".tgz"]
    rebuilt = [path for path in paths if path.parent == artifacts_dir / "wheel-from-sdist"
               and path.suffix == ".whl"]
    require(len(wheel) == len(sdist) == len(npm) == len(rebuilt) == 1, "Unexpected archive layout")
    expected_license = (root / "LICENSE").read_bytes()
    expected_upstream = (root / "UPSTREAM.md").read_bytes()
    with zipfile.ZipFile(wheel[0]) as archive:
        metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        require(len(metadata) == 1, "Missing wheel metadata")
        python_meta = BytesParser().parsebytes(archive.read(metadata[0]))
        licenses = [name for name in archive.namelist() if name.endswith(".dist-info/licenses/LICENSE")]
        require(len(licenses) == 1 and archive.read(licenses[0]) == expected_license,
                "Wheel license mismatch")
        require(archive.read("cci/UPSTREAM.md") == expected_upstream, "Wheel attribution mismatch")
    with tarfile.open(npm[0]) as archive:
        npm_meta = json.load(archive.extractfile("package/package.json"))
        require(archive.extractfile("package/LICENSE").read() == expected_license,
                "npm license mismatch")
        require(archive.extractfile("package/UPSTREAM.md").read() == expected_upstream,
                "npm attribution mismatch")
    with tarfile.open(sdist[0]) as archive:
        metadata = [member for member in archive.getmembers()
                    if len(Path(member.name).parts) == 2 and member.name.endswith("/PKG-INFO")]
        require(len(metadata) == 1, "Missing source distribution metadata")
        source_meta = BytesParser().parsebytes(archive.extractfile(metadata[0]).read())
    version = npm_meta["version"]
    require(tag == f"v{version}" and python_meta["Version"] == source_meta["Version"] == version,
            "Release tag and package versions differ")
    require(python_meta["Name"] == source_meta["Name"] == npm_meta["name"] == "chat-context-index",
            "Unexpected package identity")
    require(npm_meta.get("repository", {}).get("url")
            == "git+https://github.com/Ajitkumar-1001/Chat-context-index.git", "npm repository mismatch")
    require(npm_meta.get("license") == python_meta["License-Expression"] == "Apache-2.0",
            "Package license metadata mismatch")
    # Fail if the destination exists: never publish a stale archive left by an earlier run.
    output.mkdir()
    for registry, registry_paths in (("python", [wheel[0], sdist[0]]), ("npm", npm)):
        destination = output / registry
        destination.mkdir()
        for path in registry_paths:
            shutil.copyfile(path, destination / path.name)
    manifest = {"status": "PASS", "sha": sha, "run_id": str(run_id), "version": version,
                "package_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                "artifacts": entries, "rebuilt_for_publication": False}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def licenses(root=ROOT):
    """Check own package license and inventory dependency declarations for reviewer inspection."""
    python = tomllib.loads((root / "packages/python/pyproject.toml").read_text())["project"]
    npm = json.loads((root / "packages/typescript/package.json").read_text())
    require(python["license"] == npm["license"] == "Apache-2.0", "Package license differs")
    require(python["version"] == npm["version"], "Package versions differ")
    for path in (root / "packages/python/LICENSE", root / "packages/typescript/LICENSE"):
        require(path.read_bytes() == (root / "LICENSE").read_bytes(), "Packaged license differs")
    for path in (root / "packages/python/src/cci/UPSTREAM.md", root / "packages/typescript/UPSTREAM.md"):
        require(path.read_bytes() == (root / "UPSTREAM.md").read_bytes(), "Packaged attribution differs")
    lock = json.loads((root / "packages/typescript/package-lock.json").read_text())
    inventory = [{"path": name, "version": item.get("version"), "license": item.get("license")}
                 for name, item in lock["packages"].items() if name]
    require(all(item["license"] for item in inventory), "Missing npm dependency license metadata")
    return {"status": "PASS", "scope": "package license/attribution and npm declaration completeness",
            "npm_dependencies": inventory,
            "dependency_license_scope": "Declaration inventory; compatibility is not inferred automatically"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    artifacts = subparsers.add_parser("artifacts")
    artifacts.add_argument("--candidate", type=Path, required=True)
    artifacts.add_argument("--sha", required=True)
    artifacts.add_argument("--run-id", required=True)
    artifacts.add_argument("--tag", required=True)
    artifacts.add_argument("--output", type=Path, required=True)
    inventory = subparsers.add_parser("licenses")
    inventory.add_argument("--output", type=Path, required=True)
    expand = subparsers.add_parser("unpack")
    expand.add_argument("--artifacts", type=Path, required=True)
    expand.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "artifacts":
        result = verify_artifacts(args.candidate, args.sha, args.run_id, args.tag, args.output)
    elif args.command == "unpack":
        result = unpack(args.artifacts, args.output)
    else:
        result = licenses()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "npm_dependencies"}))


if __name__ == "__main__":
    main()
