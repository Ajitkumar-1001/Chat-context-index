"""Sequential cross-runtime test (AT-11/AT-17), per Failure-Injection.md "Release checks":
after Python closes a committed store, open it in TypeScript and repeat in reverse — run from
an installed wheel and npm tarball outside the checkout. These are sequential opens, not
concurrent cross-runtime writes.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "packages", "python", "src"))

from cci.ingest import ingest
from cci.models import InputMessage
from cci.store import HistoryStore

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_TS_PACKAGE_DIR = os.path.join(_REPO_ROOT, "packages", "typescript")
_TS_TARBALL = os.path.join(_TS_PACKAGE_DIR, "chat-context-index-0.1.0.tgz")

_NODE_CONSUMER_DIR: str | None = None


def setup_module(module) -> None:
    """Installs the packed TypeScript tarball into a fresh consumer directory outside the
    checkout — built once per test session, reused by every test in this module."""
    global _NODE_CONSUMER_DIR
    if not os.path.exists(_TS_TARBALL):
        subprocess.run(["npm", "pack"], cwd=_TS_PACKAGE_DIR, check=True, capture_output=True)
    _NODE_CONSUMER_DIR = tempfile.mkdtemp(prefix="cci-ts-consumer-")
    with open(os.path.join(_NODE_CONSUMER_DIR, "package.json"), "w") as f:
        json.dump({"name": "cci-cross-runtime-consumer", "version": "0.0.0", "type": "module", "private": True}, f)
    subprocess.run(
        ["npm", "install", _TS_TARBALL], cwd=_NODE_CONSUMER_DIR, check=True, capture_output=True,
    )


def teardown_module(module) -> None:
    if _NODE_CONSUMER_DIR:
        shutil.rmtree(_NODE_CONSUMER_DIR, ignore_errors=True)


def _run_node_script(script_body: str) -> dict:
    script_path = os.path.join(_NODE_CONSUMER_DIR, "_script.mjs")
    with open(script_path, "w") as f:
        f.write(script_body)
    result = subprocess.run(
        ["node", script_path], cwd=_NODE_CONSUMER_DIR, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node script failed: {result.stderr}")
    return json.loads(result.stdout)


def test_python_creates_typescript_reads():
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "py-to-ts.db")
            store = await HistoryStore.open(path)
            history_id = store.history_id
            await ingest(
                store, history_id,
                [
                    InputMessage(role="user", content="created in python, read in typescript"),
                    InputMessage(role="assistant", content="A😀éZ unicode round trip"),
                ],
                "src-1", "key-1",
            )
            await store.aclose()

            result = _run_node_script(f"""
import {{ HistoryStore }} from 'chat-context-index';
const store = await HistoryStore.open({json.dumps(path)});
const messages = await store.getMessages(1, 1000);
console.log(JSON.stringify({{
  historyId: store.historyId,
  count: messages.length,
  contents: messages.map(m => m.originalPayload.content),
}}));
await store.close();
""")
            assert result["historyId"] == history_id, "TypeScript reads the same history_id Python wrote"
            assert result["count"] == 2
            assert result["contents"] == [
                "created in python, read in typescript",
                "A😀éZ unicode round trip",
            ]

    asyncio.run(scenario())


def test_typescript_creates_python_reads():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ts-to-py.db")

        result = _run_node_script(f"""
import {{ HistoryStore, ingest }} from 'chat-context-index';
const store = await HistoryStore.open({json.dumps(path)});
const receipt = await ingest(store, store.historyId, [
  {{ role: 'user', content: 'created in typescript, read in python' }},
  {{ role: 'assistant', content: 'A😀éZ unicode round trip' }},
], 'src-1', 'key-1');
console.log(JSON.stringify({{ historyId: store.historyId, insertedSeqEnd: receipt.insertedSeqEnd }}));
await store.close();
""")
        history_id = result["historyId"]
        assert result["insertedSeqEnd"] == 2

        async def read_back() -> None:
            store = await HistoryStore.open(path)
            assert store.history_id == history_id, "Python reads the same history_id TypeScript wrote"
            messages = await store.get_messages(1, 1000)
            assert len(messages) == 2
            assert messages[0].original_payload["content"] == "created in typescript, read in python"
            assert messages[1].original_payload["content"] == "A😀éZ unicode round trip"
            await store.aclose()

        asyncio.run(read_back())


if __name__ == "__main__":
    setup_module(None)
    try:
        test_python_creates_typescript_reads()
        test_typescript_creates_python_reads()
        print("AT-11/AT-17 cross-runtime checks passed")
    finally:
        teardown_module(None)
