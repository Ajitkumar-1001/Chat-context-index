"""Cross-language conformance suite (T075; AT-11): runs shared fixture cases (T012's
ingestion-identity.json I1/I2/I5, unicode-source-mapping.json U1/U2) against both packed
artifacts (Python wheel preview install, TypeScript npm tarball) installed outside the source
checkout — asserting equivalent normalized results, not merely "both happen to pass their own
suite."
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_PY_DIST_DIR = os.path.join(_REPO_ROOT, "packages", "python", "dist")
_TS_PACKAGE_DIR = os.path.join(_REPO_ROOT, "packages", "typescript")
_TS_TARBALL = os.path.join(_TS_PACKAGE_DIR, "chat-context-index-0.1.0.tgz")

_PY_PREVIEW_PYTHON: str | None = None
_NODE_CONSUMER_DIR: str | None = None
_TMP_ROOT: str | None = None


def _find_wheel() -> str:
    candidates = [f for f in os.listdir(_PY_DIST_DIR) if f.endswith(".whl")] if os.path.isdir(_PY_DIST_DIR) else []
    if not candidates:
        subprocess.run(
            [sys.executable, "-m", "build", "--wheel"],
            cwd=os.path.join(_REPO_ROOT, "packages", "python"), check=True, capture_output=True,
        )
        candidates = [f for f in os.listdir(_PY_DIST_DIR) if f.endswith(".whl")]
    return os.path.join(_PY_DIST_DIR, candidates[0])


def setup_module(module) -> None:
    """Installs the Python wheel and the TypeScript tarball into fresh locations outside the
    checkout — built once per test session, reused by every test in this module."""
    global _PY_PREVIEW_PYTHON, _NODE_CONSUMER_DIR, _TMP_ROOT
    _TMP_ROOT = tempfile.mkdtemp(prefix="cci-conformance-")

    py_venv_dir = os.path.join(_TMP_ROOT, "py-preview-venv")
    subprocess.run([sys.executable, "-m", "venv", py_venv_dir], check=True, capture_output=True)
    _PY_PREVIEW_PYTHON = os.path.join(py_venv_dir, "bin", "python")
    wheel = _find_wheel()
    subprocess.run([_PY_PREVIEW_PYTHON, "-m", "pip", "install", "--quiet", wheel], check=True, capture_output=True)

    if not os.path.exists(_TS_TARBALL):
        subprocess.run(["npm", "pack"], cwd=_TS_PACKAGE_DIR, check=True, capture_output=True)
    _NODE_CONSUMER_DIR = os.path.join(_TMP_ROOT, "ts-preview-consumer")
    os.makedirs(_NODE_CONSUMER_DIR, exist_ok=True)
    with open(os.path.join(_NODE_CONSUMER_DIR, "package.json"), "w") as f:
        json.dump({"name": "cci-conformance-consumer", "version": "0.0.0", "type": "module", "private": True}, f)
    subprocess.run(["npm", "install", _TS_TARBALL], cwd=_NODE_CONSUMER_DIR, check=True, capture_output=True)


def teardown_module(module) -> None:
    import shutil

    if _TMP_ROOT:
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)


def _run_python(script_body: str) -> dict:
    result = subprocess.run(
        [_PY_PREVIEW_PYTHON, "-c", script_body], capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"python preview script failed: {result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def _run_node(script_body: str) -> dict:
    script_path = os.path.join(_NODE_CONSUMER_DIR, "_case.mjs")
    with open(script_path, "w") as f:
        f.write(script_body)
    result = subprocess.run(["node", script_path], cwd=_NODE_CONSUMER_DIR, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"node preview script failed: {result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_i1_distinct_identities_with_identical_content_remain_distinct_and_replay_is_idempotent():
    py_script = f"""
import asyncio, json, tempfile, os
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.models import InputMessage

async def main():
    with tempfile.TemporaryDirectory() as d:
        store = await HistoryStore.open(os.path.join(d, "i1.db"))
        msgs = [InputMessage(role="user", external_id="u-1", content="Okay"), InputMessage(role="user", external_id="u-2", content="Okay")]
        first = await ingest(store, store.history_id, msgs, "chat-a", "batch-1")
        second = await ingest(store, store.history_id, msgs, "chat-a", "batch-1")
        print(json.dumps({{
            "first_inserted_range": [first.inserted_seq_start, first.inserted_seq_end],
            "first_replayed": first.replayed,
            "second_replayed": second.replayed,
            "second_matches_first": second.inserted_seq_start == first.inserted_seq_start and second.inserted_seq_end == first.inserted_seq_end,
        }}))
        await store.aclose()

asyncio.run(main())
"""
    ts_script = """
import { HistoryStore, ingest } from 'chat-context-index';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';

const d = fs.mkdtempSync(path.join(os.tmpdir(), 'i1-ts-'));
const store = await HistoryStore.open(path.join(d, 'i1.db'));
const msgs = [{ role: 'user', externalId: 'u-1', content: 'Okay' }, { role: 'user', externalId: 'u-2', content: 'Okay' }];
const first = await ingest(store, store.historyId, msgs, 'chat-a', 'batch-1');
const second = await ingest(store, store.historyId, msgs, 'chat-a', 'batch-1');
console.log(JSON.stringify({
  first_inserted_range: [first.insertedSeqStart, first.insertedSeqEnd],
  first_replayed: first.replayed,
  second_replayed: second.replayed,
  second_matches_first: second.insertedSeqStart === first.insertedSeqStart && second.insertedSeqEnd === first.insertedSeqEnd,
}));
await store.close();
"""
    py_result = _run_python(py_script)
    ts_result = _run_node(ts_script)

    for label, result in (("python", py_result), ("typescript", ts_result)):
        assert result["first_inserted_range"] == [1, 2], label
        assert result["first_replayed"] is False, label
        assert result["second_replayed"] is True, label
        assert result["second_matches_first"] is True, label


def test_i2_idempotency_conflict_wins_over_message_conflict():
    py_script = """
import asyncio, json, tempfile, os
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.models import InputMessage
from cci.errors import IdempotencyConflict

async def main():
    with tempfile.TemporaryDirectory() as d:
        store = await HistoryStore.open(os.path.join(d, "i2.db"))
        await ingest(store, store.history_id, [
            InputMessage(role="user", external_id="u-1", content="Okay"),
            InputMessage(role="user", external_id="u-2", content="Okay"),
        ], "chat-a", "batch-1")
        try:
            await ingest(store, store.history_id, [
                InputMessage(role="user", external_id="u-1", content="Okay, changed"),
                InputMessage(role="user", external_id="u-2", content="Okay"),
            ], "chat-a", "batch-1")
            result = "no_error"
        except IdempotencyConflict:
            result = "IdempotencyConflict"
        print(json.dumps({"result": result}))
        await store.aclose()

asyncio.run(main())
"""
    ts_script = """
import { HistoryStore, ingest, cciErrors } from 'chat-context-index';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';

const d = fs.mkdtempSync(path.join(os.tmpdir(), 'i2-ts-'));
const store = await HistoryStore.open(path.join(d, 'i2.db'));
await ingest(store, store.historyId, [
  { role: 'user', externalId: 'u-1', content: 'Okay' },
  { role: 'user', externalId: 'u-2', content: 'Okay' },
], 'chat-a', 'batch-1');
let result = 'no_error';
try {
  await ingest(store, store.historyId, [
    { role: 'user', externalId: 'u-1', content: 'Okay, changed' },
    { role: 'user', externalId: 'u-2', content: 'Okay' },
  ], 'chat-a', 'batch-1');
} catch (e) {
  result = e instanceof cciErrors.IdempotencyConflict ? 'IdempotencyConflict' : e.constructor.name;
}
console.log(JSON.stringify({ result }));
await store.close();
"""
    assert _run_python(py_script)["result"] == "IdempotencyConflict"
    assert _run_node(ts_script)["result"] == "IdempotencyConflict"


def test_i5_source_scoped_identity_both_messages_retained():
    py_script = """
import asyncio, json, tempfile, os
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.models import InputMessage

async def main():
    with tempfile.TemporaryDirectory() as d:
        store = await HistoryStore.open(os.path.join(d, "i5.db"))
        await ingest(store, store.history_id, [InputMessage(role="user", external_id="u-1", content="From chat-a")], "chat-a", "batch-1")
        await ingest(store, store.history_id, [InputMessage(role="user", external_id="u-1", content="From chat-b")], "chat-b", "batch-1")
        messages = await store.get_messages(1, 100)
        print(json.dumps({"count": len(messages), "contents": sorted(m.original_payload["content"] for m in messages)}))
        await store.aclose()

asyncio.run(main())
"""
    ts_script = """
import { HistoryStore, ingest } from 'chat-context-index';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';

const d = fs.mkdtempSync(path.join(os.tmpdir(), 'i5-ts-'));
const store = await HistoryStore.open(path.join(d, 'i5.db'));
await ingest(store, store.historyId, [{ role: 'user', externalId: 'u-1', content: 'From chat-a' }], 'chat-a', 'batch-1');
await ingest(store, store.historyId, [{ role: 'user', externalId: 'u-1', content: 'From chat-b' }], 'chat-b', 'batch-1');
const messages = await store.getMessages(1, 100);
console.log(JSON.stringify({ count: messages.length, contents: messages.map(m => m.originalPayload.content).sort() }));
await store.close();
"""
    py_result = _run_python(py_script)
    ts_result = _run_node(ts_script)
    expected = {"count": 2, "contents": ["From chat-a", "From chat-b"]}
    assert py_result == expected
    assert ts_result == expected


def test_u1_unicode_scalar_offset_agreement():
    """U1: "A😀éZ" is 5 Unicode scalar values; the emoji spans exactly 1 scalar value at
    offset 1 (never 2, as UTF-16 surrogate-pair-naive counting would produce)."""
    py_script = r"""
import json
from cci.models import source_pointer_for_offset

fixture = "A\U0001F600éZ"  # A, emoji, e, combining acute, Z = 5 scalar values
content = [{"type": "text", "text": fixture}]
pointer_at_1 = source_pointer_for_offset(content, 1)  # the emoji's own offset
print(json.dumps({"scalar_length": len(fixture), "pointer_at_1": pointer_at_1}))
"""
    ts_script = r"""
import { codePointLength } from 'chat-context-index/dist/models.js';
const fixture = "A\u{1F600}éZ";
console.log(JSON.stringify({ scalar_length: codePointLength(fixture) }));
"""
    py_result = _run_python(py_script)
    assert py_result["scalar_length"] == 5, "Python len(str) already counts scalar values natively"
    assert py_result["pointer_at_1"] == "/content/0/text"

    ts_result = _run_node(ts_script)
    assert ts_result["scalar_length"] == 5, "TypeScript codePointLength must agree with Python's native len()"


def test_u2_precomposed_vs_combining_forms_are_distinct_and_both_preserved():
    py_script = """
import asyncio, json, tempfile, os
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.models import InputMessage

async def main():
    with tempfile.TemporaryDirectory() as d:
        store = await HistoryStore.open(os.path.join(d, "u2.db"))
        await ingest(store, store.history_id, [
            InputMessage(role="user", content="é"),
            InputMessage(role="user", content="é"),
        ], "src-1", "key-1")
        messages = await store.get_messages(1, 10)
        print(json.dumps({
            "count": len(messages),
            "distinct": messages[0].original_payload["content"] != messages[1].original_payload["content"],
            "contents": [m.original_payload["content"] for m in messages],
        }))
        await store.aclose()

asyncio.run(main())
"""
    ts_script = r"""
import { HistoryStore, ingest } from 'chat-context-index';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';

const d = fs.mkdtempSync(path.join(os.tmpdir(), 'u2-ts-'));
const store = await HistoryStore.open(path.join(d, 'u2.db'));
await ingest(store, store.historyId, [
  { role: 'user', content: 'é' },
  { role: 'user', content: 'é' },
], 'src-1', 'key-1');
const messages = await store.getMessages(1, 10);
console.log(JSON.stringify({
  count: messages.length,
  distinct: messages[0].originalPayload.content !== messages[1].originalPayload.content,
  contents: messages.map(m => m.originalPayload.content),
}));
await store.close();
"""
    py_result = _run_python(py_script)
    ts_result = _run_node(ts_script)
    assert py_result == {"count": 2, "distinct": True, "contents": ["é", "é"]}
    assert ts_result == py_result, "both languages must preserve the two forms identically, byte for byte"


if __name__ == "__main__":
    setup_module(None)
    try:
        test_i1_distinct_identities_with_identical_content_remain_distinct_and_replay_is_idempotent()
        test_i2_idempotency_conflict_wins_over_message_conflict()
        test_i5_source_scoped_identity_both_messages_retained()
        test_u1_unicode_scalar_offset_agreement()
        test_u2_precomposed_vs_combining_forms_are_distinct_and_both_preserved()
        print("T075 cross-language conformance checks passed")
    finally:
        teardown_module(None)
