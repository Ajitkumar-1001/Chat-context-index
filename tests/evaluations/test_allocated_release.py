"""Native/cache guard composition, using only invented data and mock clients."""

import asyncio
import importlib.util
import json
import re
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError
from test_allocated_comparison import MODULE as GUARDS
from test_allocated_comparison import (  # noqa: F401 - pytest fixture
    prepared_files,
    response,
)

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "unit_allocated_release", ROOT / "evaluations/run_allocated_release.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def release_files(request, monkeypatch):
    f = request.getfixturevalue("prepared_files")
    monkeypatch.setattr(MODULE, "ROOT", f.root)
    monkeypatch.setattr(MODULE, "shared", GUARDS)
    monkeypatch.setattr(GUARDS.runner, "ROOT", f.root)
    shutil.copyfile(
        ROOT / "evaluations/run_allocated_release.py", f.root / "evaluations/run_allocated_release.py"
    )
    fixture = {"histories": [{"history_label": "unit", "split": "development", "messages": [
        {"role": "user", "content": "The target is Oslo.", "source_id": "unit"}
    ]}], "queries": [{"query_id": "q1", "query_text": "Where is the target?", "history_label": "unit",
                       "split": "development", "answerable": True, "required_evidence_units": [],
                       "answer_rubric": {"correct_value": "Oslo"}}]}
    fixture_path = f.root / "development.json"
    fixture_path.write_text(json.dumps(fixture))
    f.release = {key: f.plan[key] for key in ["schema_version", "wheel_sha256", "profile", "resolved_models",
                                            "allocation_id", "allocation_book_sha256", "limits", "pricing"]}
    f.release.update(kind="native", split="development", trials=1, fixture="development.json",
                     fixture_sha256=GUARDS.digest(fixture_path), cache_backends=["none", "sqlite"],
                     source_hashes={
                         name: GUARDS.digest(f.root / name) for name in GUARDS.runner.FROZEN_SOURCES
                     },
                     package_config={"cache_backend": "none", "max_provider_attempts_per_op": 1,
                                     "per_provider_concurrency": 1, "target_chunk_size_scalars": 100})
    f.release_path = f.root / "release-plan.json"
    f.release_path.write_text(json.dumps(f.release))
    f.outer = {"schema_version": 1, "release_plan": "release-plan.json",
               "release_plan_sha256": GUARDS.digest(f.release_path),
               "guard_source_hashes": {name: GUARDS.digest(f.root / name) for name in MODULE.GUARD_SOURCES}}
    f.plan_path.write_text(json.dumps(f.outer))
    return f


def prepare(f):
    return MODULE.prepare(f.plan_path, f.wheel, f.book, f.out, f.settings)


def resave(f):
    f.release_path.write_text(json.dumps(f.release))
    f.outer["release_plan_sha256"] = GUARDS.digest(f.release_path)
    f.plan_path.write_text(json.dumps(f.outer))


def install_mock(monkeypatch, failure=None):
    @asynccontextmanager
    async def client(self, *, timeout_s, http_client):
        yield SimpleNamespace(base_url=self.base_url, max_retries=0, timeout=timeout_s, _client=http_client)
    monkeypatch.setattr(GUARDS.runner.smoke.ModelSettings, "make_client", client)
    class Provider:
        def __init__(self, client, settings, **kwargs):
            self.client, self.settings = client, settings
        async def completion(self, request, output_limit):
            if failure == "timeout":
                raise APITimeoutError(request=httpx.Request("POST", "https://unit.invalid"))
            value = response(model="wrong" if failure == "model" else "unit-model")
            if failure == "model":
                value.choices[0].finish_reason = "length"
                return value
            ids = re.findall(r"<<<CCI_EVIDENCE id=(\S+)", request.evidence_context)
            if request.operation == "indexing":
                data = {"title": "Target", "summary": "Target is Oslo."}
            elif request.operation == "tree_navigation":
                data = {"node_ids": ids}
            elif request.operation == "rubric_review":
                payload = json.loads(request.evidence_context)
                data = {"correct": True, "citations": [
                    {"evidence_id": c["evidence_id"], "supports_claim": True} for c in payload["citations"]
                ]}
            else:
                data = {"answer": "Oslo", "citations": ids[:1]}
            value.choices[0].message.content = json.dumps(data)
            return value
    monkeypatch.setattr(GUARDS.runner.smoke, "ChatCompletionsProvider", Provider)


def test_native_preflight_is_zero_effect_and_checks_guard_pins(release_files):
    f = release_files
    prepared = prepare(f)
    assert prepared.ready and not prepared.claim.exists() and not f.out.exists()
    (f.root / MODULE.GUARD_SOURCES[0]).write_text("changed")
    with pytest.raises(ValueError, match="guard"):
        prepare(f)


def test_native_claim_path_uses_canonical_book_after_symlink_resolution(release_files):
    f = release_files
    alias = f.root / "allocations.json"
    alias.symlink_to(f.book)
    prepared = MODULE.prepare(f.plan_path, f.wheel, alias, f.out, f.settings)
    assert prepared.book == f.book.resolve()
    assert prepared.claim == f.book.parent / "allocation-claims/comparison-001"


@pytest.mark.parametrize("kind", ["native", "cache"])
@pytest.mark.parametrize("failure", [None, "timeout", "model"])
def test_native_and_cache_use_hardened_transport_and_ledger(release_files, monkeypatch, kind, failure):
    f = release_files
    if kind == "cache":
        fixture = {"messages": [{"role": "user", "content": "The target is Oslo.", "source_id": "unit"},
                                 {"role": "user", "content": "Use CSV files.", "source_id": "unit"}],
                   "query": "Where is the target?", "changed_query": "Which city is the target?",
                   "append_message": {"role": "user", "content": "Add a backup copy.", "source_id": "unit"},
                   "required_source_seq": 1}
        (f.root / "development.json").write_text(json.dumps(fixture))
        f.release.update(kind="cache", fixture_sha256=GUARDS.digest(f.root / "development.json"))
        resave(f)
    install_mock(monkeypatch, failure)
    prepared = prepare(f)
    result = asyncio.run(MODULE.execute(prepared))
    accounting = json.loads((f.out / "allocation-accounting.json").read_text())
    report = json.loads((f.out / "report.json").read_text())
    assert prepared.claim.is_dir() and not accounting["allocation_reusable"]
    assert not report["quality_pass"]  # Development evidence cannot close a held-out gate.
    if failure:
        assert result == 1 and accounting["status"] == "INCOMPLETE"
        assert accounting["calls"] == 1 and accounting["reserved_tokens"] > 0
        assert accounting["calls_with_unknown_usage"] == (1 if failure == "timeout" else 0)
    else:
        assert result == 0 and accounting["status"] == "COMPLETE"
        assert len(report["query_results"] if kind == "native" else report["cache_results"]) == (
            2 if kind == "native" else 10
        )
    with pytest.raises(FileExistsError):
        MODULE.prepare(f.plan_path, f.wheel, f.book, f.root / "another-output", f.settings)


def test_native_capacity_and_changed_plan_remain_blocking(release_files):
    f = release_files
    prepared = prepare(f)
    f.plan_path.write_text(f.plan_path.read_text() + " ")
    with pytest.raises(ValueError, match="outer_plan_changed"):
        asyncio.run(MODULE.execute(prepared))
    assert not prepared.claim.exists()


def test_cache_redis_url_required_before_claim(release_files, monkeypatch):
    f = release_files
    f.release.update(kind="cache", cache_backends=["none", "sqlite", "redis"])
    resave(f)
    monkeypatch.delenv("CCI_EVAL_REDIS_URL", raising=False)
    with pytest.raises(ValueError, match="redis_url"):
        prepare(f)
    assert not f.out.exists()


def test_client_cleanup_failure_cannot_publish_complete_accounting(release_files, monkeypatch):
    f = release_files
    install_mock(monkeypatch)
    original_client = GUARDS.runner.smoke.ModelSettings.make_client
    @asynccontextmanager
    async def failed_cleanup(self, *, timeout_s, http_client):
        async with original_client(self, timeout_s=timeout_s, http_client=http_client) as client:
            yield client
        raise RuntimeError("PRIVATE cleanup failure")
    monkeypatch.setattr(GUARDS.runner.smoke.ModelSettings, "make_client", failed_cleanup)
    prepared = prepare(f)
    with pytest.raises(RuntimeError):
        asyncio.run(MODULE.execute(prepared))
    accounting = json.loads((f.out / "allocation-accounting.json").read_text())
    assert prepared.claim.is_dir() and accounting["calls"] > 0
    assert accounting["status"] == "INCOMPLETE"
    assert accounting["error_type"] == "RuntimeError"
    assert "PRIVATE" not in (f.out / "allocation-accounting.json").read_text()


def block_capacity(f):
    readiness = f.book.with_name("provider-readiness.json")
    value = json.loads(readiness.read_text())
    value["status"] = "BLOCKED"
    readiness.write_text(json.dumps(value))
    book = json.loads(f.book.read_text())
    book["provider_readiness_sha256"] = GUARDS.digest(readiness)
    f.book.write_text(json.dumps(book))
    f.release["allocation_book_sha256"] = GUARDS.digest(f.book)
    resave(f)


@pytest.fixture
def smoke_files(release_files):
    f = release_files
    fixture_path = f.root / "evaluations/fixtures/live-smoke.json"
    fixture_path.parent.mkdir(parents=True)
    # Visible development data only; never copy the held-out fixture.
    shutil.copyfile(ROOT / "evaluations/fixtures/live-smoke.json", fixture_path)
    f.release.update(kind="smoke", fixture="evaluations/fixtures/live-smoke.json",
                     fixture_sha256=GUARDS.digest(fixture_path), limits={
                         "max_calls": 10, "max_reserved_tokens": 100_000, "max_estimated_usd": 0.04,
                         "max_output_tokens_per_call": 256, "max_input_bytes_per_call": 8000,
                         "call_timeout_s": 1.0, "run_timeout_s": 240.0,
                         "concurrency": 1, "minimum_request_interval_s": 5.0,
                     })
    f.outer["capacity_probe"] = True
    block_capacity(f)
    return f


def test_bounded_smoke_preflight_does_not_assert_capacity_or_consume_claim(smoke_files):
    f = smoke_files
    prepared = prepare(f)
    assert prepared.outer.capacity_probe and not prepared.ready
    assert not prepared.claim.exists() and not f.out.exists()


@pytest.mark.parametrize("kind", ["native", "cache"])
def test_capacity_probe_cannot_bypass_readiness_for_other_kinds(release_files, kind):
    f = release_files
    f.release["kind"] = kind
    f.outer["capacity_probe"] = True
    block_capacity(f)
    with pytest.raises(ValueError, match="capacity_probe_requires_development_smoke"):
        prepare(f)
    assert not f.out.exists()


@pytest.mark.parametrize("kind", ["native", "cache"])
def test_existing_native_cache_plans_still_require_capacity(release_files, kind):
    f = release_files
    f.release["kind"] = kind
    block_capacity(f)
    prepared = prepare(f)
    assert not prepared.outer.capacity_probe
    with pytest.raises(ValueError, match="capacity_not_verified"):
        asyncio.run(MODULE.execute(prepared))
    assert not prepared.claim.exists() and not f.out.exists()


@pytest.mark.parametrize("field,value", [("split", "held_out"), ("trials", 3)])
def test_smoke_probe_rejects_non_development_protocol(smoke_files, field, value):
    f = smoke_files
    f.release[field] = value
    resave(f)
    with pytest.raises(ValueError, match="one development trial"):
        prepare(f)
    assert not f.out.exists()


@pytest.mark.parametrize("field,value", [
    ("max_calls", 11), ("max_reserved_tokens", 100_001), ("max_estimated_usd", 0.041),
    ("max_output_tokens_per_call", 257), ("max_input_bytes_per_call", 8001),
    ("call_timeout_s", 61.0), ("run_timeout_s", 241.0),
    ("concurrency", 2), ("minimum_request_interval_s", 4.9),
])
def test_smoke_probe_cannot_expand_its_envelope(smoke_files, field, value):
    f = smoke_files
    f.release["limits"][field] = value
    resave(f)
    with pytest.raises(ValueError, match="smoke_envelope"):
        prepare(f)
    assert not f.out.exists()


def test_smoke_probe_requires_original_development_fixture(smoke_files):
    f = smoke_files
    alias = f.root / "other-fixture.json"
    shutil.copyfile(f.root / f.release["fixture"], alias)
    f.release["fixture"] = str(alias)
    resave(f)
    with pytest.raises(ValueError, match="original_development_fixture"):
        prepare(f)


def test_smoke_without_explicit_probe_still_requires_capacity(smoke_files):
    f = smoke_files
    f.outer["capacity_probe"] = False
    resave(f)
    prepared = prepare(f)
    with pytest.raises(ValueError, match="capacity_not_verified"):
        asyncio.run(MODULE.execute(prepared))
    assert not prepared.claim.exists() and not f.out.exists()


@pytest.mark.parametrize("failure", [None, "timeout", "model"])
def test_smoke_uses_hardened_dispatch_and_preserves_blocked_capacity(smoke_files, monkeypatch, failure):
    f = smoke_files
    install_mock(monkeypatch, failure)
    # Pacing is covered by the shared ledger tests; keep this integration offline and fast.
    async def no_pacing(*args):
        return None
    monkeypatch.setattr(GUARDS.runner.comparison.asyncio, "sleep", no_pacing)
    prepared = prepare(f)
    result = asyncio.run(MODULE.execute(prepared))
    accounting = json.loads((f.out / "allocation-accounting.json").read_text())
    report = json.loads((f.out / "report.json").read_text())
    assert prepared.claim.is_dir() and not accounting["allocation_reusable"]
    assert not accounting["provider_capacity_verified_before_run"]
    assert accounting["capacity_probe"]
    assert json.loads(f.book.with_name("provider-readiness.json").read_text())["status"] == "BLOCKED"
    assert not report["quality_pass"] and not report["lower_cost_claim"]
    if failure:
        assert result == 1 and accounting["status"] == "INCOMPLETE"
        assert accounting["calls"] == 1 and accounting["reserved_tokens"] > 0
        assert accounting["calls_with_unknown_usage"] == (1 if failure == "timeout" else 0)
    else:
        assert result == 0 and accounting["status"] == "COMPLETE"
        assert report["checks"]["expected_original_evidence_retrieved"]
        assert report["checks"]["reopen_preserved_originals"]
        assert report["checks"]["lexical_evidence_count"] == 0
        assert report["checks"]["unchanged_index_calls"] == 0
        assert accounting["calls_with_unknown_usage"] == 0
    with pytest.raises(FileExistsError):
        MODULE.prepare(f.plan_path, f.wheel, f.book, f.root / "retry", f.settings)
