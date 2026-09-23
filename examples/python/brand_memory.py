"""A local, synthetic brand-memory example with a real process restart and no model calls.

Run after installing cci: python examples/python/brand_memory.py
The parent creates a temporary directory, a child seeds two histories, and a different child
reads them. No user database is opened or modified by the default command.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from cci.config import Config
from cci.ingest import ingest
from cci.models import InputMessage
from cci.store import HistoryStore
from conversation_context import prepare_context

FIXTURE = Path(__file__).resolve().parents[2] / "evaluations/fixtures/brand-memory.json"


async def run_phase(phase: str, directory: Path) -> None:
    histories = json.loads(FIXTURE.read_text(encoding="utf-8"))["histories"]
    for brand, records in histories.items():
        async with await HistoryStore.open(
            str(directory / f"{brand}.sqlite"), config=Config(cache_backend="none"),
        ) as store:
            if phase == "seed":
                await ingest(store, store.history_id, [InputMessage(**record) for record in records], "example", "initial-history")
                print(f"Saved {len(records)} messages for {brand}.", flush=True)
            else:
                context = await prepare_context(store, "How much does enrollment cost?")
                print(f"\n{brand}: context recovered in a new process; {len(context.text)} rendered characters.")
                print(context.text)
    if phase == "read":
        print("\nThis is historical context for an application's model, not a generated answer.")
        print("The recent correction is included; general paraphrase retrieval and answer correctness are not established.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["seed", "read"], help=argparse.SUPPRESS)
    parser.add_argument("--directory", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.phase:
        if args.directory is None:
            parser.error("a child phase requires --directory")
        asyncio.run(run_phase(args.phase, args.directory))
        return
    with tempfile.TemporaryDirectory(prefix="cci-brand-example-") as directory:
        for phase in ("seed", "read"):
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "--phase", phase, "--directory", directory], check=True, timeout=30)


if __name__ == "__main__":
    main()
