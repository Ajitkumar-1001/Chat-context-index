"""Release uploads must reject stale, incomplete or changed installed-package evidence."""

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
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
              "schema_v1_migration": "PASS",
              "migration_checks": [{"migrator": writer, "reader": reader, "status": "PASS"}
                                   for writer in runtimes for reader in runtimes],
              "checks": [{"writer": writer, "reader": reader, "status": "PASS"}
                         for writer in runtimes for reader in runtimes],
              "artifacts": [{"filename": str(path.relative_to(artifacts)),
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                            for path in (wheel, sdist, npm, rebuilt)]}
    report_path = candidate / "reports/package-memory.json"
    report_path.parent.mkdir()
    report_path.write_text(json.dumps(report))
    (report_path.parent / "artifact-secrets.json").write_text("[]")
    approval = tmp_path / "approved.json"
    approval.write_text(json.dumps({"schema_version": 1, "source_sha": SHA, "version": "0.1.0",
                                    "artifacts": report["artifacts"]}))
    digest = hashlib.sha256(approval.read_bytes()).hexdigest()
    return candidate, root, tmp_path / "publish", report_path, approval, digest


def verify(candidate, **overrides):
    path, root, output, _, approval, digest = candidate
    options = {"sha": SHA, "run_id": "123", "tag": "v0.1.0",
               "approval": approval, "approval_sha256": digest} | overrides
    return CONTROLS.verify_artifacts(path, **options, output=output, root=root)


def test_publish_copies_verified_archives_without_rebuilding(candidate):
    report = verify(candidate)
    assert report["rebuilt_for_publication"] is False
    source, _, output, _ = candidate[:4]
    assert report["approval_manifest_sha256"] == candidate[5]
    for registry in ("python", "npm"):
        for path in (output / registry).iterdir():
            assert path.read_bytes() == (source / "verified-artifacts" / path.name).read_bytes()
    assert len(list((output / "python").iterdir())) == 2
    assert len(list((output / "npm").iterdir())) == 1


@pytest.mark.parametrize("mutation", ["missing", "failed", "incomplete", "duplicate", "failed_cell"])
def test_schema_migration_evidence_is_required(candidate, mutation):
    report_path = candidate[3]
    report = json.loads(report_path.read_text())
    if mutation == "missing":
        del report["schema_v1_migration"]
    elif mutation == "failed":
        report["schema_v1_migration"] = "FAIL"
    elif mutation == "incomplete":
        report["migration_checks"].pop()
    elif mutation == "duplicate":
        report["migration_checks"][-1] = report["migration_checks"][0]
    else:
        report["migration_checks"][0]["status"] = "FAIL"
    report_path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="migration"):
        verify(candidate)
    assert not candidate[2].exists()


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


def test_changed_archive_and_matching_report_are_rejected(candidate):
    path = next((candidate[0] / "verified-artifacts").glob("*.tgz"))
    path.write_bytes(path.read_bytes() + b"tampering")
    report = json.loads(candidate[3].read_text())
    for entry in report["artifacts"]:
        if entry["filename"] == path.name:
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    candidate[3].write_text(json.dumps(report))
    with pytest.raises(ValueError, match="approved artifacts"):
        verify(candidate)
    assert not candidate[2].exists()


def test_archive_changed_during_copy_cannot_pass_publication(candidate, monkeypatch):
    copyfile = CONTROLS.shutil.copyfile

    def changed_copy(source, destination):
        source.write_bytes(source.read_bytes() + b"changed after verification")
        return copyfile(source, destination)

    monkeypatch.setattr(CONTROLS.shutil, "copyfile", changed_copy)
    with pytest.raises(ValueError, match="Copied archive checksum mismatch"):
        verify(candidate)
    assert not (candidate[2] / "manifest.json").exists()


@pytest.mark.parametrize("options", [
    {"approval": None}, {"approval_sha256": None}, {"approval_sha256": ""},
    {"approval_sha256": "a" * 63}, {"approval_sha256": "A" * 64},
])
def test_missing_or_invalid_approval_is_rejected(candidate, options):
    with pytest.raises(ValueError, match="artifact approval"):
        verify(candidate, **options)
    assert not candidate[2].exists()


def test_publication_api_without_approval_fails_closed(candidate):
    with pytest.raises(ValueError, match="artifact approval"):
        CONTROLS.verify_artifacts(candidate[0], SHA, "123", "v0.1.0", candidate[2], root=candidate[1])
    assert not candidate[2].exists()


def test_substituted_approval_manifest_is_rejected(candidate):
    approved = json.loads(candidate[4].read_text())
    approved["artifacts"][0]["sha256"] = "b" * 64
    candidate[4].write_text(json.dumps(approved))
    with pytest.raises(ValueError, match="Approval manifest digest mismatch"):
        verify(candidate)
    assert not candidate[2].exists()


@pytest.mark.parametrize("location", ["candidate", "symlink", "missing"])
def test_approval_must_be_separate_regular_file(candidate, tmp_path, location):
    path = tmp_path / "other.json"
    if location == "candidate":
        path = candidate[0] / "approval.json"
        path.write_bytes(candidate[4].read_bytes())
    elif location == "symlink":
        path.symlink_to(candidate[4])
    with pytest.raises(ValueError, match="separate file"):
        verify(candidate, approval=path)
    assert not candidate[2].exists()


@pytest.mark.parametrize("field,value,reason", [
    ("source_sha", "b" * 40, "Approval source commit mismatch"),
    ("version", "0.2.0", "Approval package version mismatch"),
])
def test_approval_is_bound_to_source_and_version(candidate, field, value, reason):
    approved = json.loads(candidate[4].read_text())
    approved[field] = value
    candidate[4].write_text(json.dumps(approved))
    with pytest.raises(ValueError, match=reason):
        verify(candidate, approval_sha256=hashlib.sha256(candidate[4].read_bytes()).hexdigest())
    assert not candidate[2].exists()


@pytest.mark.parametrize("mutation", [
    "invalid_json", "duplicate_key", "duplicate_entry_key", "list", "schema_version", "boolean_schema",
    "extra_field", "missing_entry", "duplicate_entry", "extra_entry_field", "unsafe_path", "empty_path",
    "noncanonical_path", "backslash_path", "bad_digest", "nonstr_digest", "nonstr_name", "nondict_entry",
    "nonstr_entries", "renamed_archive",
])
def test_malformed_or_ambiguous_approval_is_rejected(candidate, mutation):
    approved = json.loads(candidate[4].read_text())
    entries = approved["artifacts"]
    if mutation == "list":
        approved = []
    elif mutation == "schema_version":
        approved["schema_version"] = 2
    elif mutation == "boolean_schema":
        approved["schema_version"] = True
    elif mutation == "extra_field":
        approved["approved"] = True
    elif mutation == "missing_entry":
        entries.pop()
    elif mutation == "duplicate_entry":
        entries[-1] = entries[0]
    elif mutation == "extra_entry_field":
        entries[0]["approved"] = True
    elif mutation in {"unsafe_path", "empty_path", "noncanonical_path", "backslash_path"}:
        entries[0]["filename"] = {"unsafe_path": "../outside.whl", "empty_path": "",
                                  "noncanonical_path": "./archive.whl",
                                  "backslash_path": "directory\\archive.whl"}[mutation]
    elif mutation == "bad_digest":
        entries[0]["sha256"] = "z" * 64
    elif mutation == "nonstr_digest":
        entries[0]["sha256"] = None
    elif mutation == "nonstr_name":
        entries[0]["filename"] = None
    elif mutation == "nondict_entry":
        entries[0] = None
    elif mutation == "nonstr_entries":
        approved["artifacts"] = None
    elif mutation == "renamed_archive":
        entries[0]["filename"] = "different.whl"
    data = json.dumps(approved)
    if mutation == "invalid_json":
        data = "{"
    elif mutation == "duplicate_key":
        data = '{"version":"0.2.0",' + data[1:]
    elif mutation == "duplicate_entry_key":
        data = data.replace('"sha256":', '"sha256":"invalid","sha256":', 1)
    candidate[4].write_text(data)
    with pytest.raises(ValueError):
        verify(candidate, approval_sha256=hashlib.sha256(candidate[4].read_bytes()).hexdigest())
    assert not candidate[2].exists()


def test_approval_entry_order_does_not_change_artifact_identity(candidate):
    approved = json.loads(candidate[4].read_text())
    approved["artifacts"].reverse()
    candidate[4].write_text(json.dumps(approved))
    assert verify(candidate, approval_sha256=hashlib.sha256(candidate[4].read_bytes()).hexdigest())


def cli(candidate, command, *extra):
    script = candidate[1] / "scripts/release_controls.py"
    script.parent.mkdir(exist_ok=True)
    script.write_bytes(Path(CONTROLS.__file__).read_bytes())
    return subprocess.run([sys.executable, str(script), command,
                           "--candidate", str(candidate[0]), "--sha", SHA, "--run-id", "123",
                           "--tag", "v0.1.0", "--output", str(candidate[2]), *extra],
                          capture_output=True, text=True, check=False)


@pytest.mark.parametrize("approval_flags", ["none", "manifest_only", "digest_only", "wrong_digest"])
def test_cli_publication_without_valid_approval_fails_closed(candidate, approval_flags):
    args = []
    if approval_flags in {"manifest_only", "wrong_digest"}:
        args += ["--approval", str(candidate[4])]
    if approval_flags in {"digest_only", "wrong_digest"}:
        args += ["--approval-sha256", "0" * 64]
    result = cli(candidate, "artifacts", *args)
    assert result.returncode != 0
    assert "approval" in result.stderr.lower()
    assert not candidate[2].exists()


def test_cli_copies_exact_approved_archives(candidate):
    result = cli(candidate, "artifacts", "--approval", str(candidate[4]), "--approval-sha256", candidate[5])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["approval_manifest_sha256"] == candidate[5]
    for path in (candidate[2] / "python").iterdir():
        assert path.read_bytes() == (candidate[0] / "verified-artifacts" / path.name).read_bytes()


def test_cli_draft_is_reviewable_without_creating_publication_archives(candidate):
    result = cli(candidate, "approval-manifest")
    assert result.returncode == 0, result.stderr
    draft = candidate[2]
    assert draft.is_file()
    assert not draft.read_bytes().endswith(b"\n")
    status = json.loads(result.stdout)
    assert status["status"] == "DRAFT_NOT_APPROVED"
    assert status["approval_manifest_sha256"] == hashlib.sha256(draft.read_bytes()).hexdigest()
    approved = json.loads(candidate[4].read_text())
    approved["artifacts"].sort(key=lambda item: item["filename"])
    assert json.loads(draft.read_text()) == approved
    with pytest.raises(ValueError, match="artifact approval"):
        CONTROLS.verify_artifacts(candidate[0], SHA, "123", "v0.1.0", draft.parent / "unauthorized",
                                  root=candidate[1])
    assert not (draft.parent / "unauthorized").exists()


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
