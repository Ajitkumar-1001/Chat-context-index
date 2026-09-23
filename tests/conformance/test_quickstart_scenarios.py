"""T080: runs quickstart.md's Scenarios 1-9 end-to-end against the published-artifact
candidates (Python wheel, TypeScript npm tarball) installed outside the source checkout.
Scenario 10 (held-out quality gates) is M5-only (T081/T082), out of this task's range.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_PY_DIST_DIR = os.path.join(_REPO_ROOT, "packages", "python", "dist")
_TS_PACKAGE_DIR = os.path.join(_REPO_ROOT, "packages", "typescript")
_TS_TARBALL = os.path.join(_TS_PACKAGE_DIR, "chat-context-index-0.1.0.tgz")

_PY: str | None = None
_NODE_DIR: str | None = None
_TMP: str | None = None


def _find_or_build_wheel() -> str:
    candidates = [f for f in os.listdir(_PY_DIST_DIR) if f.endswith(".whl")] if os.path.isdir(_PY_DIST_DIR) else []
    if not candidates:
        subprocess.run(
            [sys.executable, "-m", "build", "--wheel"],
            cwd=os.path.join(_REPO_ROOT, "packages", "python"), check=True, capture_output=True,
        )
        candidates = [f for f in os.listdir(_PY_DIST_DIR) if f.endswith(".whl")]
    return os.path.join(_PY_DIST_DIR, candidates[0])


def setup_module(module) -> None:
    global _PY, _NODE_DIR, _TMP
    _TMP = tempfile.mkdtemp(prefix="cci-quickstart-")

    venv_dir = os.path.join(_TMP, "py-venv")
    subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True, capture_output=True)
    _PY = os.path.join(venv_dir, "bin", "python")
    subprocess.run([_PY, "-m", "pip", "install", "--quiet", _find_or_build_wheel()], check=True, capture_output=True)

    if not os.path.exists(_TS_TARBALL):
        subprocess.run(["npm", "pack"], cwd=_TS_PACKAGE_DIR, check=True, capture_output=True)
    _NODE_DIR = os.path.join(_TMP, "ts-consumer")
    os.makedirs(_NODE_DIR, exist_ok=True)
    with open(os.path.join(_NODE_DIR, "package.json"), "w") as f:
        json.dump({"name": "cci-quickstart-consumer", "version": "0.0.0", "type": "module", "private": True}, f)
    subprocess.run(["npm", "install", _TS_TARBALL], cwd=_NODE_DIR, check=True, capture_output=True)


def teardown_module(module) -> None:
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


def _py(script: str) -> dict:
    result = subprocess.run([_PY, "-c", script], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


def _node(script: str) -> dict:
    path = os.path.join(_NODE_DIR, "_qs.mjs")
    with open(path, "w") as f:
        f.write(script)
    result = subprocess.run(["node", path], cwd=_NODE_DIR, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_scenario_1_preserve_and_resume():
    r = _py("""
import asyncio, json, tempfile, os
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.models import InputMessage

async def main():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s1.db")
        store = await HistoryStore.open(path)
        receipt = await ingest(store, store.history_id, [
            InputMessage(role="user", external_id="a", content="same text"),
            InputMessage(role="user", external_id="b", content="same text"),
        ], "src-1", "key-1")
        await store.aclose()
        reopened = await HistoryStore.open(path)
        messages = await reopened.get_messages(1, 100)
        print(json.dumps({
            "history_id_matches": reopened.history_id == store.history_id,
            "count": len(messages),
            "distinct_ids": len({m.message_id for m in messages}) == 2,
            "seq_order": [m.seq for m in messages] == sorted(m.seq for m in messages),
        }))
        await reopened.aclose()

asyncio.run(main())
""")
    assert r == {"history_id_matches": True, "count": 2, "distinct_ids": True, "seq_order": True}


def test_scenario_2_idempotent_replay_and_conflict_rejection():
    r = _py("""
import asyncio, json, tempfile, os
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.models import InputMessage, MAX_MESSAGES_PER_BATCH
from cci.errors import IdempotencyConflict, InputValidationError

async def main():
    with tempfile.TemporaryDirectory() as d:
        store = await HistoryStore.open(os.path.join(d, "s2.db"))
        await ingest(store, store.history_id, [InputMessage(role="user", content="x")], "src-1", "key-1")
        replay = await ingest(store, store.history_id, [InputMessage(role="user", content="x")], "src-1", "key-1")
        conflict = "no_error"
        try:
            await ingest(store, store.history_id, [InputMessage(role="user", content="y")], "src-1", "key-1")
        except IdempotencyConflict:
            conflict = "IdempotencyConflict"
        oversize = "no_error"
        try:
            await ingest(store, store.history_id, [InputMessage(role="user", content="z")] * (MAX_MESSAGES_PER_BATCH + 1), "src-1", "key-2")
        except InputValidationError:
            oversize = "InputValidationError"
        print(json.dumps({"replayed": replay.replayed, "conflict": conflict, "oversize": oversize}))
        await store.aclose()

asyncio.run(main())
""")
    assert r == {"replayed": True, "conflict": "IdempotencyConflict", "oversize": "InputValidationError"}


def test_scenario_3_lexical_retrieval_without_a_model():
    r = _py("""
import asyncio, json, tempfile, os
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.models import InputMessage
from cci.retrieve import retrieve

async def main():
    with tempfile.TemporaryDirectory() as d:
        store = await HistoryStore.open(os.path.join(d, "s3.db"))
        await ingest(store, store.history_id, [InputMessage(role="user", content="refund policy details")], "src-1", "key-1")
        hit = await retrieve(store, "refund policy")
        miss = await retrieve(store, "unrelated nonexistent query xyz")
        print(json.dumps({
            "hit_mode": hit.routing.actual_mode,
            "hit_provider_calls": hit.usage.current_provider_calls,
            "miss_status": miss.status,
        }))
        await store.aclose()

asyncio.run(main())
""")
    assert r == {"hit_mode": "lexical", "hit_provider_calls": 0, "miss_status": "empty"}


def test_scenario_4_explicit_indexing_idempotent_rerun():
    r = _py("""
import asyncio, json, tempfile, os
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.index import index
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderResponse

async def main():
    with tempfile.TemporaryDirectory() as d:
        store = await HistoryStore.open(os.path.join(d, "s4.db"))
        await ingest(store, store.history_id, [InputMessage(role="user", content="topic content")], "src-1", "key-1")
        fake = FakeProvider(responses=[ProviderResponse(text='{"title":"T","summary":"S"}')])
        provider = MemoizedProvider(inner=fake, config=store.config, cache=store.cache)
        first = await index(store, provider=provider)
        second = await index(store, provider=provider)
        print(json.dumps({
            "first_status": first.status,
            "second_calls": fake.call_count,
            "second_status": second.status,
        }))
        await store.aclose()

asyncio.run(main())
""")
    assert r == {"first_status": "complete", "second_calls": 1, "second_status": "complete"}


def test_scenario_5_evidence_backed_ask():
    r = _py("""
import asyncio, json, tempfile, os
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.ask import ask
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderResponse

async def main():
    with tempfile.TemporaryDirectory() as d:
        store = await HistoryStore.open(os.path.join(d, "s5.db"))
        await ingest(store, store.history_id, [InputMessage(role="user", content="rayleigh scattering explains sky color")], "src-1", "key-1")
        provider = MemoizedProvider(inner=FakeProvider(), config=store.config, cache=store.cache)
        no_evidence = await ask(store, "completely unrelated query terms", provider=provider)
        fake2 = FakeProvider(responses=[ProviderResponse(text='{"answer":"x","citations":["ev_1"]}')])
        provider2 = MemoizedProvider(inner=fake2, config=store.config, cache=store.cache)
        answered = await ask(store, "rayleigh scattering", provider=provider2)
        print(json.dumps({
            "no_evidence_status": no_evidence.status,
            "no_evidence_answer": no_evidence.answer,
            "answered_status": answered.status,
            "citation_resolves": all(c.evidence_id in {e.evidence_id for e in answered.evidence} for c in answered.citations),
        }))
        await store.aclose()

asyncio.run(main())
""")
    assert r == {
        "no_evidence_status": "insufficient_evidence", "no_evidence_answer": None,
        "answered_status": "answered", "citation_resolves": True,
    }


def test_scenario_6_cache_modes_warm_and_cold():
    r = _py("""
import asyncio, json, tempfile, os
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.index import index
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderResponse

async def main():
    with tempfile.TemporaryDirectory() as d:
        store = await HistoryStore.open(os.path.join(d, "s6.db"), config={"cache_backend": "sqlite"})
        await ingest(store, store.history_id, [InputMessage(role="user", content="cache scenario content")], "src-1", "key-1")
        fake = FakeProvider(responses=[ProviderResponse(text='{"title":"T","summary":"S"}')])
        provider = MemoizedProvider(inner=fake, config=store.config, cache=store.cache)
        await index(store, provider=provider, rebuild=True)
        warm = await index(store, provider=provider, rebuild=True)
        print(json.dumps({"warm_calls": fake.call_count, "warm_hits": warm.provider_usage.memo_hits}))
        await store.aclose()

asyncio.run(main())
""")
    assert r == {"warm_calls": 1, "warm_hits": 1}


def test_scenario_7_export_import_clear():
    r = _py("""
import asyncio, json, tempfile, os
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.export import export
from cci.import_history import import_history
from cci.clear import clear_history
from cci.models import InputMessage
from cci.errors import StoreNotEmpty

async def main():
    with tempfile.TemporaryDirectory() as d:
        store = await HistoryStore.open(os.path.join(d, "s7.db"))
        await ingest(store, store.history_id, [InputMessage(role="user", content="exportable content")], "src-1", "key-1")
        manifest = await export(store, os.path.join(d, "s7.jsonl"))

        target = await HistoryStore.open(os.path.join(d, "s7-target.db"))
        report = await import_history(target, os.path.join(d, "s7.jsonl"))

        nonempty_rejected = "no_error"
        try:
            await import_history(target, os.path.join(d, "s7.jsonl"))
        except StoreNotEmpty:
            nonempty_rejected = "StoreNotEmpty"

        clear_report = await clear_history(store, store.history_id)
        messages_after_clear = await store.get_messages(1, 100)

        print(json.dumps({
            "imported_matches_exported": report.imported_count == manifest.message_count,
            "nonempty_rejected": nonempty_rejected,
            "clear_complete": clear_report.logical_clear_complete,
            "empty_after_clear": len(messages_after_clear) == 0,
        }))
        await store.aclose()
        await target.aclose()

asyncio.run(main())
""")
    assert r == {
        "imported_matches_exported": True, "nonempty_rejected": "StoreNotEmpty",
        "clear_complete": True, "empty_after_clear": True,
    }


def test_scenario_8_diagnostics_without_content_leakage():
    r = _py("""
import asyncio, json, tempfile, os
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.stats import stats
from cci.models import InputMessage

async def main():
    with tempfile.TemporaryDirectory() as d:
        store = await HistoryStore.open(os.path.join(d, "s8.db"))
        await ingest(store, store.history_id, [InputMessage(role="user", content="a secret credential sk-abc123")], "src-1", "key-1")
        result = await stats(store)
        rendered = repr(result)
        print(json.dumps({
            "message_count": result.history_message_count,
            "no_leak": "sk-abc123" not in rendered and "secret" not in rendered,
        }))
        await store.aclose()

asyncio.run(main())
""")
    assert r == {"message_count": 1, "no_leak": True}


def test_scenario_9_cross_language_conformance():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s9.db")
        py_result = _py(f"""
import asyncio, json
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.models import InputMessage

async def main():
    store = await HistoryStore.open({json.dumps(path)})
    await ingest(store, store.history_id, [InputMessage(role="user", content="cross-language A😀éZ")], "src-1", "key-1")
    print(json.dumps({{"history_id": store.history_id}}))
    await store.aclose()

asyncio.run(main())
""")
        ts_result = _node(f"""
import {{ HistoryStore }} from 'chat-context-index';
const store = await HistoryStore.open({json.dumps(path)});
const messages = await store.getMessages(1, 100);
console.log(JSON.stringify({{
  historyIdMatches: store.historyId === {json.dumps(py_result["history_id"])},
  content: messages[0].originalPayload.content,
}}));
await store.close();
""")
        assert ts_result["historyIdMatches"] is True
        assert ts_result["content"] == "cross-language A😀éZ"


if __name__ == "__main__":
    setup_module(None)
    try:
        test_scenario_1_preserve_and_resume()
        test_scenario_2_idempotent_replay_and_conflict_rejection()
        test_scenario_3_lexical_retrieval_without_a_model()
        test_scenario_4_explicit_indexing_idempotent_rerun()
        test_scenario_5_evidence_backed_ask()
        test_scenario_6_cache_modes_warm_and_cold()
        test_scenario_7_export_import_clear()
        test_scenario_8_diagnostics_without_content_leakage()
        test_scenario_9_cross_language_conformance()
        print("T080: quickstart.md Scenarios 1-9 passed against published-artifact candidates")
    finally:
        teardown_module(None)
