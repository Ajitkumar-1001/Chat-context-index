"""Child-process helper for Failure-Injection F1 (commit ambiguity). Opens a store, ingests a
fixed batch, and hard-exits (`os._exit`) at the requested barrier — simulating a real process
crash (power loss/kill -9), not a caught exception, per
spec/fixtures/failure-injection-harness.md's harness-boundaries rule: "Test process termination
in a child process, so restart assertions inspect actual committed on-disk storage."

Usage: crash_ingest.py <db_path> <before_commit|after_commit>
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "python", "src"))

from cci import ingest as ingest_module
from cci.ingest import ingest
from cci.models import InputMessage
from cci.store import HistoryStore

SOURCE_ID = "crash-src"
IDEMPOTENCY_KEY = "crash-key"


async def main(path: str, kill_point: str) -> None:
    def _crash() -> None:
        os._exit(17)

    if kill_point == "before_commit":
        ingest_module._before_commit_barrier = _crash
    elif kill_point == "after_commit":
        ingest_module._after_commit_barrier = _crash
    else:
        raise ValueError(f"unknown kill_point: {kill_point!r}")

    store = await HistoryStore.open(path)
    messages = [InputMessage(role="user", content="crash-test message")]
    await ingest(store, store.history_id, messages, SOURCE_ID, IDEMPOTENCY_KEY)
    # Only reached if the barrier failed to crash the process — signal that to the parent.
    print("NO_CRASH")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
