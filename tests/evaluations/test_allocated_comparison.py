"""Allocated comparison boundaries use invented fixtures and no network."""

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cci.provider import ProviderRequest
from openai import APIConnectionError, APITimeoutError
from openai.types.chat import ChatCompletion

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "test_allocated_comparison_runner", ROOT / "evaluations/run_allocated_comparison.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def response(*, model="unit-model", usage=True):
    return ChatCompletion.model_validate({
        "id": "unit", "object": "chat.completion", "created": 1, "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": '{"answer":null,"citations":[]}'
        }}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
        if usage else None,
    })


class Adapter:
    def __init__(self, settings, *, model="unit-model", usage=True):
        self.settings = settings
        self.client = SimpleNamespace(base_url=settings.base_url + "/", max_retries=0, timeout=1,
                                      _client=SimpleNamespace(follow_redirects=False))
        self.calls = 0
        self.model, self.usage = model, usage

    async def completion(self, request, output_limit):
        self.calls += 1
        return response(model=self.model, usage=self.usage)


@pytest.fixture
def prepared_files(tmp_path, monkeypatch):
    # Copy source bytes only. No held-out fixture or outcome is copied/read.
    for name in MODULE.FROZEN_SOURCES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "require_isolated", lambda: None)
    original_require = MODULE.runner.smoke.require
    monkeypatch.setattr(MODULE.runner.smoke, "require", lambda value, code:
                        None if code == "run_with_python_I" else original_require(value, code))
    settings = MODULE.runner.smoke.ModelSettings("unit", "unit-model", "http://localhost/v1", "")
    def no_unmocked_client(*args, **kwargs):
        pytest.fail("offline tests must explicitly supply a mocked provider client")
    monkeypatch.setattr(MODULE.runner.smoke.ModelSettings, "make_client", no_unmocked_client)
    profile = settings.public_settings()
    for key, value in {"CCI_PROVIDER": "unit", "CCI_MODEL": "unit-model",
                       "CCI_BASE_URL": "http://localhost/v1", "CCI_TOKEN_LIMIT_FIELD": "max_tokens",
                       "CCI_RESPONSE_FORMAT": "json_object", "CCI_API_KEY": ""}.items():
        monkeypatch.setenv(key, value)
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"invented wheel; installed provenance is a test double")
    monkeypatch.setattr(MODULE.runner.smoke, "package_provenance", lambda path: {
        "wheel_sha256": MODULE.digest(path), "installed_sources_match_wheel": True,
        "version": "unit", "isolated_python": True,
    })
    fixture = {"histories": [{"history_label": "invented", "split": "held_out", "messages": [
        {"source_id": "unit", "role": "user", "content": "The city is Oslo."},
        {"source_id": "unit", "role": "user", "content": "Use a backup."},
    ]}], "queries": [{"query_id": f"invented-{i}", "history_label": "invented",
                         "split": "held_out", "query_text": "Which city?", "answerable": i < 32,
                         "required_evidence_units": [], "answer_rubric": None} for i in range(40)]}
    fixture_path = tmp_path / MODULE.FIXTURE
    fixture_path.write_text(json.dumps(fixture))
    limits = {"max_calls": 1000, "max_reserved_tokens": 5_000_000, "max_estimated_usd": 10.0,
              "max_output_tokens_per_call": 128, "max_input_bytes_per_call": 100_000,
              "call_timeout_s": 1.0, "run_timeout_s": 60.0, "concurrency": 1,
              "minimum_request_interval_s": 0.0}
    pricing = {"input_usd_per_million": 0.3, "output_usd_per_million": 2.5}
    inner = {"schema_version": 1, "trials": 3, "strategies": list(MODULE.STRATEGIES),
             "provider": "unit", "model": "unit-model", "fixture_sha256": MODULE.digest(fixture_path),
             "wheel_sha256": MODULE.digest(wheel),
             "source_hashes": {name: MODULE.digest(tmp_path / name) for name in MODULE.INNER_SOURCES},
             "initial_history_percent": 80,
             "context": {"recent_messages": 4, "max_messages": 8, "max_chars": 4000, "excerpt_chars": 200},
             "package_config": {"cache_backend": "none", "max_provider_attempts_per_op": 1,
                                "per_provider_concurrency": 1, "target_chunk_size_scalars": 100},
             "limits": limits, "pricing": pricing, "quality_gates": MODULE.QUALITY_GATES,
             "comparison_max_quality_loss": 0.05}
    inner_path = tmp_path / "comparison-plan.json"
    inner_path.write_text(json.dumps(inner))
    book = tmp_path / MODULE.CANONICAL_BOOK
    book.parent.mkdir(parents=True)
    baseline = book.with_name("baseline.json")
    baseline.write_text(json.dumps({"accounting": {"remaining": {
        "calls": 1100, "reserved_tokens": 5_100_000, "estimated_usd": 10.04}}}))
    ready = book.with_name("provider-readiness.json")
    ready.write_text(json.dumps({
        "status": "AVAILABLE", "profile_sha256": MODULE.runner.profile_digest(profile)
    }))
    envelope = {key: limits[key] for key in ("max_calls", "max_reserved_tokens", "max_estimated_usd")}
    envelope.update(pricing=pricing, profile_sha256=MODULE.runner.profile_digest(profile))
    book.write_text(json.dumps({"baseline_sha256": MODULE.digest(baseline),
                               "provider_readiness_sha256": MODULE.digest(ready),
                               "allocations": {"comparison-001": envelope,
                                   "consumed-smoke": dict(envelope, max_calls=10,
                                                          max_reserved_tokens=100000,
                                                          max_estimated_usd=0.04)}}))
    plan = {"schema_version": 1, "comparison_plan": "comparison-plan.json",
            "comparison_plan_sha256": MODULE.digest(inner_path), "wheel_sha256": MODULE.digest(wheel),
            "source_hashes": {name: MODULE.digest(tmp_path / name) for name in MODULE.FROZEN_SOURCES},
            "profile": profile, "resolved_models": ["unit-model"], "limits": limits, "pricing": pricing,
            "allocation_id": "comparison-001", "allocation_book_sha256": MODULE.digest(book)}
    plan_path = tmp_path / "outer-plan.json"
    plan_path.write_text(json.dumps(plan))
    return SimpleNamespace(root=tmp_path, plan=plan, inner=inner, plan_path=plan_path,
                           inner_path=inner_path, wheel=wheel, book=book, settings=settings,
                           out=tmp_path / "result")


def prepare(files):
    return MODULE.prepare(files.plan_path, files.wheel, files.book, files.out, files.settings)


def save_plan(files):
    files.plan_path.write_text(json.dumps(files.plan))


def test_default_preflight_is_zero_effect_and_retains_blocked_readiness(prepared_files):
    f = prepared_files
    prepared = prepare(f)
    assert prepared.ready and not f.out.exists() and not prepared.claim.exists()
    ready = f.book.with_name("provider-readiness.json")
    data = json.loads(ready.read_text())
    data["status"] = "BLOCKED"
    ready.write_text(json.dumps(data))
    book = json.loads(f.book.read_text())
    book["provider_readiness_sha256"] = MODULE.digest(ready)
    f.book.write_text(json.dumps(book))
    f.plan["allocation_book_sha256"] = MODULE.digest(f.book)
    save_plan(f)
    prepared = prepare(f)
    assert not prepared.ready
    with pytest.raises(ValueError, match="capacity"):
        asyncio.run(MODULE.execute(prepared))
    assert not f.out.exists() and not prepared.claim.exists()


@pytest.mark.parametrize("change", ["endpoint", "protocol", "token_limit_field", "response_format"])
def test_full_profile_mismatch_prevents_claim(prepared_files, change):
    f = prepared_files
    f.plan["profile"][change] = "changed"
    save_plan(f)
    with pytest.raises(ValueError, match="profile"):
        prepare(f)
    assert not f.out.exists()


@pytest.mark.parametrize("change", ["inner_bytes", "source", "wheel", "inner_limits", "inner_price",
                                    "budget_sum", "readiness_bytes", "fixture", "missing_source"])
def test_changed_pins_caps_and_shared_budget_fail_closed(prepared_files, change):
    f = prepared_files
    if change == "inner_bytes":
        f.inner_path.write_text(f.inner_path.read_text() + " ")
    elif change == "source":
        (f.root / MODULE.FROZEN_SOURCES[-1]).write_text("changed")
    elif change == "wheel":
        f.wheel.write_bytes(b"changed")
    elif change in ("inner_limits", "inner_price"):
        f.inner["limits" if change == "inner_limits" else "pricing"][
            "max_calls" if change == "inner_limits" else "input_usd_per_million"] *= 2
        f.inner_path.write_text(json.dumps(f.inner))
        f.plan["comparison_plan_sha256"] = MODULE.digest(f.inner_path)
        save_plan(f)
    elif change == "budget_sum":
        book = json.loads(f.book.read_text())
        book["allocations"]["native-001"] = book["allocations"]["comparison-001"]
        f.book.write_text(json.dumps(book))
        f.plan["allocation_book_sha256"] = MODULE.digest(f.book)
        save_plan(f)
    elif change == "readiness_bytes":
        f.book.with_name("provider-readiness.json").write_text("{}")
    elif change == "fixture":
        (f.root / MODULE.FIXTURE).write_text("{}")
    else:
        f.plan["source_hashes"].pop(MODULE.FROZEN_SOURCES[0])
        save_plan(f)
    with pytest.raises(ValueError):
        prepare(f)
    assert not f.out.exists()


def test_copied_allocation_book_is_rejected(prepared_files):
    f = prepared_files
    copy = f.root / "allocations.json"
    shutil.copyfile(f.book, copy)
    with pytest.raises(ValueError, match="canonical"):
        MODULE.prepare(f.plan_path, f.wheel, copy, f.out, f.settings)


@pytest.mark.parametrize("change", ["endpoint", "retries", "timeout", "settings"])
def test_provider_profile_is_guarded_before_dispatch(prepared_files, change):
    f = prepared_files
    adapter = Adapter(f.settings)
    if change == "endpoint":
        adapter.client.base_url = "https://other.invalid/v1"
    elif change == "retries":
        adapter.client.max_retries = 1
    elif change == "timeout":
        adapter.client.timeout = 10
    else:
        adapter.settings = MODULE.runner.smoke.ModelSettings("unit", "other", "http://localhost/v1", "")
    ledger = MODULE.AllocatedLedger(adapter, prepare(f).plan, f.root / "calls.jsonl")
    with pytest.raises(MODULE.runner.RunStopped):
        asyncio.run(ledger.complete(ProviderRequest("synthesis", "unit", "unit"), {}))
    assert adapter.calls == 0 and not ledger.calls
    assert ledger.stopped == "provider_profile_changed"


@pytest.mark.parametrize("kind", ["model", "usage", "timeout", "cancel"])
def test_model_and_interruption_failures_stop_dispatch_and_keep_journal(prepared_files, kind):
    f = prepared_files
    class Failure(Adapter):
        async def completion(self, request, output_limit):
            self.calls += 1
            if kind == "timeout":
                raise TimeoutError()
            if kind == "cancel":
                raise asyncio.CancelledError()
            return response(model="wrong" if kind == "model" else "unit-model", usage=kind != "usage")
    adapter = Failure(f.settings)
    ledger = MODULE.AllocatedLedger(adapter, prepare(f).plan, f.root / "calls.jsonl")
    async def run():
        with pytest.raises((MODULE.runner.RunStopped, TimeoutError, asyncio.CancelledError)):
            await ledger.complete(ProviderRequest("synthesis", "unit", "unit"), {})
        with pytest.raises(MODULE.runner.RunStopped):
            await ledger.complete(ProviderRequest("synthesis", "unit", "unit"), {})
    asyncio.run(run())
    assert adapter.calls == 1 and len(ledger.calls) == 1 and ledger.stopped
    if kind != "model":
        assert ledger.reserved()[0] == 8 + 512 + 128
    events = [json.loads(line) for line in ledger.journal.read_text().splitlines()]
    assert events[0]["status"] == "pending" and events[-1]["status"] == "error"


def test_complete_frozen_comparison_is_claimed_once_and_reconciled(prepared_files, monkeypatch):
    f = prepared_files
    @asynccontextmanager
    async def client(self, *, timeout_s, http_client=None):
        yield SimpleNamespace(base_url=self.base_url, max_retries=0, timeout=timeout_s, _client=http_client)
    monkeypatch.setattr(MODULE.runner.smoke.ModelSettings, "make_client", client)
    class FakeProvider(Adapter):
        def __init__(self, client, settings):
            super().__init__(settings)
            self.client = client
        async def completion(self, request, output_limit):
            value = response()
            if request.operation == "indexing":
                value.choices[0].message.content = '{"title":"City","summary":"A city was recorded."}'
            elif request.operation == "tree_navigation":
                value.choices[0].message.content = '{"node_ids":[]}'
            return value
    monkeypatch.setattr(MODULE.runner.smoke, "ChatCompletionsProvider", FakeProvider)
    prepared = prepare(f)
    result = asyncio.run(MODULE.execute(prepared))
    assert result == 0
    assert prepared.claim.is_dir()
    report = json.loads((f.out / "report.json").read_text())
    accounting = json.loads((f.out / "allocation-accounting.json").read_text())
    assert len(report["query_results"]) == 360
    assert accounting["status"] == "COMPLETE"
    assert accounting["calls"] == len(report["calls"]) > 0
    assert accounting["calls_with_unknown_usage"] == 0
    assert not report["lower_cost_claim"].startswith("SUPPORTED")
    # A different output path still cannot recycle the allocation.
    f.out = f.root / "another-output"
    with pytest.raises(FileExistsError):
        prepare(f)


def test_required_isolation_is_not_inferred(monkeypatch):
    monkeypatch.setattr(MODULE.sys, "flags", SimpleNamespace(isolated=0))
    with pytest.raises(ValueError, match="python_I"):
        MODULE.require_isolated()


def test_cli_defaults_to_preflight_without_constructing_client(prepared_files, monkeypatch, capsys):
    f = prepared_files
    def forbidden(*args, **kwargs):
        pytest.fail("preflight constructed a provider client")
    monkeypatch.setattr(MODULE.runner.smoke.ModelSettings, "make_client", forbidden)
    monkeypatch.setattr(sys, "argv", ["run_allocated_comparison.py", "--plan", str(f.plan_path),
                        "--wheel", str(f.wheel), "--allocation-book", str(f.book), "--out", str(f.out)])
    assert MODULE.main() == 0
    assert json.loads(capsys.readouterr().out)["provider_calls"] == 0
    assert not f.out.exists()
    assert not (f.book.parent / "allocation-claims").exists()


def test_preflight_cannot_authorize_subsequently_changed_outer_plan(prepared_files):
    f = prepared_files
    prepared = prepare(f)
    f.plan_path.write_text(f.plan_path.read_text() + " ")
    with pytest.raises(ValueError, match="outer_plan_changed"):
        asyncio.run(MODULE.execute(prepared))
    assert not f.out.exists() and not prepared.claim.exists()


@pytest.mark.parametrize("failure", ["timeout", "model", "usage"])
def test_interrupted_comparison_keeps_claim_partial_report_and_reservations(
    prepared_files, monkeypatch, failure
):
    f = prepared_files
    @asynccontextmanager
    async def client(self, *, timeout_s, http_client=None):
        yield SimpleNamespace(base_url=self.base_url, max_retries=0, timeout=timeout_s, _client=http_client)
    monkeypatch.setattr(MODULE.runner.smoke.ModelSettings, "make_client", client)
    class FailureProvider(Adapter):
        def __init__(self, client, settings):
            super().__init__(settings)
            self.client = client
        async def completion(self, request, output_limit):
            if failure == "timeout":
                raise TimeoutError("PRIVATE provider details")
            return response(model="unexpected" if failure == "model" else "unit-model",
                            usage=failure != "usage")
    monkeypatch.setattr(MODULE.runner.smoke, "ChatCompletionsProvider", FailureProvider)
    prepared = prepare(f)
    assert asyncio.run(MODULE.execute(prepared)) == 1
    accounting = json.loads((f.out / "allocation-accounting.json").read_text())
    report = json.loads((f.out / "report.json").read_text())
    assert prepared.claim.is_dir() and accounting["allocation_reusable"] is False
    assert accounting["status"] == report["status"] == "INCOMPLETE"
    assert accounting["calls"] == 1 and accounting["reserved_tokens"] > 0
    if failure != "model":
        assert accounting["calls_with_unknown_usage"] == 1
        assert accounting["complete_cost_estimate"] is False
    assert accounting["journal_sha256"] == MODULE.digest(f.out / "report.calls.jsonl")
    assert all("PRIVATE" not in path.read_text() for path in f.out.glob("*.json*"))


def test_guard_rechecks_transport_after_pacing_and_enforces_output_cap(prepared_files):
    f = prepared_files
    plan = prepare(f).plan
    adapter = Adapter(f.settings)
    guarded = MODULE.GuardedAdapter(adapter, plan)
    async def run():
        with pytest.raises(MODULE.ProviderProfileChanged):
            await guarded.completion(ProviderRequest("synthesis", "unit", "unit"), 129)
        adapter.client.base_url = "https://changed.invalid/v1"
        with pytest.raises(MODULE.ProviderProfileChanged):
            await guarded.completion(ProviderRequest("synthesis", "unit", "unit"), 128)
    asyncio.run(run())
    assert adapter.calls == 0


def test_symlink_alias_cannot_create_an_alternative_claim_namespace(prepared_files):
    f = prepared_files
    alias = f.root / "alias"
    alias.mkdir()
    (alias / "allocations.json").symlink_to(f.book)
    for name in ("baseline.json", "provider-readiness.json"):
        shutil.copyfile(f.book.with_name(name), alias / name)
    prepared = MODULE.prepare(f.plan_path, f.wheel, alias / "allocations.json", f.out, f.settings)
    assert prepared.book == f.book.resolve()
    assert prepared.claim == f.book.parent / "allocation-claims/comparison-001"
    MODULE.runner.claim_run(f.out, prepared.claim, prepared.plan)
    with pytest.raises(FileExistsError):
        MODULE.prepare(f.plan_path, f.wheel, f.book, f.root / "second-output", f.settings)


@pytest.mark.parametrize(
    "failure", ["wrong_model_truncated", "wrong_model_empty", "sdk_timeout", "connection"]
)
def test_unsuccessful_responses_stop_future_dispatch(prepared_files, failure):
    f = prepared_files
    class Failure(Adapter):
        async def completion(self, request, output_limit):
            self.calls += 1
            if failure in ("sdk_timeout", "connection"):
                cls = APITimeoutError if failure == "sdk_timeout" else APIConnectionError
                raise cls(request=httpx.Request("POST", "https://unit.invalid"))
            value = response(model="wrong")
            if failure == "wrong_model_truncated":
                value.choices[0].finish_reason = "length"
            else:
                value.choices[0].message.content = ""
            return value
    adapter = Failure(f.settings)
    ledger = MODULE.AllocatedLedger(adapter, prepare(f).plan, f.root / "calls.jsonl")
    async def run():
        with pytest.raises((MODULE.runner.RunStopped, MODULE.runner.ProviderError)):
            await ledger.complete(ProviderRequest("synthesis", "unit", "unit"), {})
        with pytest.raises(MODULE.runner.RunStopped):
            await ledger.complete(ProviderRequest("synthesis", "unit", "unit"), {})
    asyncio.run(run())
    assert adapter.calls == 1 and ledger.stopped
    assert ledger.calls[0]["status"] == "error"
    if failure.startswith("wrong_model"):
        assert ledger.stopped == "resolved_model_changed"
        assert ledger.calls[0]["input_tokens"] == 12  # Preserve returned usage despite failed output.
    else:
        assert ledger.calls[0]["input_tokens"] is None
        assert ledger.reserved()[0] == 648


def test_cli_imports_do_not_create_bytecode_in_frozen_source_tree(prepared_files):
    f = prepared_files
    result = subprocess.run([sys.executable, "-I", str(f.root / "evaluations/run_allocated_comparison.py"),
                             "--help"], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert not list(f.root.rglob("__pycache__"))


def test_outer_plan_cannot_be_swapped_between_revalidation_reads(prepared_files, monkeypatch):
    f = prepared_files
    prepared = prepare(f)
    original_prepare = MODULE.prepare
    def replacement(*args):
        f.plan_path.write_text(f.plan_path.read_text() + " ")
        return original_prepare(*args)
    monkeypatch.setattr(MODULE, "prepare", replacement)
    with pytest.raises(ValueError, match="outer_plan_changed"):
        asyncio.run(MODULE.execute(prepared))
    assert not prepared.claim.exists() and not f.out.exists()


def test_http_redirect_never_dispatches_a_second_unreserved_request(prepared_files, monkeypatch):
    f = prepared_files
    dispatched = []
    def redirect(request):
        dispatched.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://other.invalid/v1/chat/completions"})
    original_factory = MODULE.make_http_client
    monkeypatch.setattr(MODULE, "make_http_client", lambda timeout_s: httpx.AsyncClient(
        timeout=timeout_s, follow_redirects=False, transport=httpx.MockTransport(redirect)))
    def sdk_client(self, *, timeout_s, http_client):
        return MODULE.runner.smoke.AsyncOpenAI(api_key="unit", base_url=self.base_url,
                                               timeout=timeout_s, max_retries=0, http_client=http_client)
    monkeypatch.setattr(MODULE.runner.smoke.ModelSettings, "make_client", sdk_client)
    plan = prepare(f).plan
    async def run():
        async with original_factory(1) as default_transport:
            assert default_transport.follow_redirects is False
        settings = MODULE.GuardedSettings(f.settings, plan)
        async with settings.make_client(timeout_s=1) as client:
            adapter = MODULE.runner.smoke.ChatCompletionsProvider(client, settings)
            ledger = MODULE.AllocatedLedger(adapter, plan, f.root / "calls.jsonl")
            with pytest.raises(MODULE.runner.ProviderError):
                await ledger.complete(ProviderRequest("synthesis", "unit", "unit"), {})
            assert ledger.stopped and len(ledger.calls) == 1
    asyncio.run(run())
    assert dispatched == ["http://localhost/v1/chat/completions"]
