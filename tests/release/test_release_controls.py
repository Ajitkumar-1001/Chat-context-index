"""Release uploads must reject stale, incomplete or changed installed-package evidence."""

import hashlib
import importlib.util
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "release_controls", Path(__file__).resolve().parents[2] / "scripts/release_controls.py"
)
CONTROLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROLS)
SHA = "a" * 40


def tar(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, data in entries.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "LICENSE").write_bytes(b"Apache license fixture")
    (root / "UPSTREAM.md").write_bytes(b"Attribution fixture")
    sources = {"python": ("packages/python/src/cci", "memory.py"),
               "typescript": ("packages/typescript/src", "memory.ts")}
    fingerprints = {}
    for language, (directory, name) in sources.items():
        path = root / directory
        path.mkdir(parents=True)
        (path / name).write_text("fixture")
        fingerprints[language] = CONTROLS.fingerprint(path, "*")
    candidate = tmp_path / "candidate"
    artifacts = candidate / "verified-artifacts"
    artifacts.mkdir(parents=True)
    metadata = b"Name: chat-context-index\nVersion: 0.1.0\nLicense-Expression: Apache-2.0\n"
    wheel = artifacts / "chat_context_index-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("chat_context_index-0.1.0.dist-info/METADATA", metadata)
        archive.writestr("chat_context_index-0.1.0.dist-info/licenses/LICENSE", b"Apache license fixture")
        archive.writestr("cci/UPSTREAM.md", b"Attribution fixture")
    rebuilt = artifacts / "wheel-from-sdist" / wheel.name
    rebuilt.parent.mkdir()
    rebuilt.write_bytes(wheel.read_bytes())
    sdist = artifacts / "chat_context_index-0.1.0.tar.gz"
    tar(sdist, {"chat_context_index-0.1.0/PKG-INFO": metadata})
    npm = artifacts / "chat-context-index-0.1.0.tgz"
    tar(npm, {"package/package.json": json.dumps({
        "name": "chat-context-index", "version": "0.1.0", "license": "Apache-2.0",
        "repository": {"url": "git+https://github.com/Ajitkumar-1001/Chat-context-index.git"},
    }).encode(), "package/LICENSE": b"Apache license fixture",
        "package/UPSTREAM.md": b"Attribution fixture"})
    runtimes = ("python", "python_sdist", "typescript")
    report = {"status": "PASS", "ci": {"GITHUB_SHA": SHA, "GITHUB_RUN_ID": "123"},
              "source_sha256": fingerprints,
              "sdist_installed_content_matches_wheel": "PASS",
              "default_cache_without_optional_dependencies_or_credentials": "PASS",
              "package_documentation_and_license": "PASS", "typescript_consumer_typecheck": "PASS",
              "checks": [{"writer": writer, "reader": reader, "status": "PASS"}
                         for writer in runtimes for reader in runtimes],
              "artifacts": [{"filename": str(path.relative_to(artifacts)),
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                            for path in (wheel, sdist, npm, rebuilt)]}
    report_path = candidate / "reports/package-memory.json"
    report_path.parent.mkdir()
    report_path.write_text(json.dumps(report))
    (report_path.parent / "artifact-secrets.json").write_text("[]")
    return candidate, root, tmp_path / "publish", report_path


def verify(candidate, **overrides):
    path, root, output, _ = candidate
    options = {"sha": SHA, "run_id": "123", "tag": "v0.1.0"} | overrides
    return CONTROLS.verify_artifacts(path, **options, output=output, root=root)


def test_publish_copies_verified_archives_without_rebuilding(candidate):
    report = verify(candidate)
    assert report["rebuilt_for_publication"] is False
    source, _, output, _ = candidate
    for registry in ("python", "npm"):
        for path in (output / registry).iterdir():
            assert path.read_bytes() == (source / "verified-artifacts" / path.name).read_bytes()
    assert len(list((output / "python").iterdir())) == 2
    assert len(list((output / "npm").iterdir())) == 1


@pytest.mark.parametrize("options", [{"sha": "b" * 40}, {"run_id": "456"}, {"tag": "v0.2.0"}])
def test_stale_source_run_or_version_is_rejected(candidate, options):
    with pytest.raises(ValueError):
        verify(candidate, **options)
    assert not candidate[2].exists()


def test_changed_archive_is_rejected(candidate):
    path = next((candidate[0] / "verified-artifacts").glob("*.tgz"))
    path.write_bytes(path.read_bytes() + b"tampering")
    with pytest.raises(ValueError, match="checksum"):
        verify(candidate)
    assert not candidate[2].exists()


def test_archive_scan_findings_block_publication(candidate):
    (candidate[3].parent / "artifact-secrets.json").write_text('[{"RuleID": "example"}]')
    with pytest.raises(ValueError, match="secret scan"):
        verify(candidate)
    assert not candidate[2].exists()


@pytest.mark.parametrize(
    "mutation", ["status", "missing_check", "duplicate_check", "path", "duplicate_artifact"]
)
def test_incomplete_or_unsafe_evidence_is_rejected(candidate, mutation):
    report_path = candidate[3]
    report = json.loads(report_path.read_text())
    if mutation == "status":
        report["status"] = "FAIL"
    elif mutation == "missing_check":
        report["checks"].pop()
    elif mutation == "duplicate_check":
        report["checks"][-1] = report["checks"][0]
    elif mutation == "path":
        report["artifacts"][0]["filename"] = "../outside.whl"
    else:
        report["artifacts"][-1] = report["artifacts"][0]
    report_path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        verify(candidate)
    assert not candidate[2].exists()


def test_new_source_edits_are_rejected(candidate):
    (candidate[1] / "packages/python/src/cci/memory.py").write_text("changed")
    with pytest.raises(ValueError, match="sources differ"):
        verify(candidate)


def test_extra_archive_is_rejected(candidate):
    (candidate[0] / "verified-artifacts/old.whl").write_bytes(b"stale")
    with pytest.raises(ValueError, match="Unexpected file"):
        verify(candidate)


def test_existing_publish_directory_is_rejected(candidate):
    candidate[2].mkdir()
    with pytest.raises(FileExistsError):
        verify(candidate)


def test_wheels_are_expanded_for_secret_scanning(candidate, tmp_path):
    output = tmp_path / "scan"
    assert CONTROLS.unpack(candidate[0] / "verified-artifacts", output)["archives_unpacked"] == 4
    assert len(list(output.rglob("UPSTREAM.md"))) == 3


def test_wheel_path_traversal_is_rejected(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with zipfile.ZipFile(artifacts / "unsafe.whl", "w") as archive:
        archive.writestr("../../outside", b"unsafe")
    with pytest.raises(ValueError, match="Unsafe wheel"):
        CONTROLS.unpack(artifacts, tmp_path / "scan")
    assert not (tmp_path / "outside").exists()
