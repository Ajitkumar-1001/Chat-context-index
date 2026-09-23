"""Runnable example: open a history, ingest, index, retrieve, ask, export/import, clear.

No Redis required (cache_backend defaults to 'sqlite'). Run with:
    pip install chat-context-index
    python basic_usage.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from cci.ask import ask
from cci.clear import clear_history
from cci.export import export
from cci.import_history import import_history
from cci.index import index
from cci.ingest import ingest
from cci.models import InputMessage
from cci.provider import MemoizedProvider, ProviderRequest, ProviderResponse
from cci.retrieve import retrieve
from cci.store import HistoryStore


class EchoProvider:
    """A trivial real `Provider` implementation for this example — never call a real LLM
    without your own credentials/config. Replace with an OpenAI/Anthropic-backed adapter for
    real use (PRD §6.2's "provider SDKs behind extras")."""

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        if request.operation == "indexing":
            return ProviderResponse(text=json.dumps({"title": "Conversation", "summary": "A short exchange."}))
        return ProviderResponse(text=json.dumps({"answer": "See the evidence above.", "citations": ["ev_1"]}))


async def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        db_path = str(Path(d) / "history.db")

        # 1. Open (creates on first use).
        store = await HistoryStore.open(db_path)
        print(f"opened history {store.history_id}")

        # 2. Ingest — no model call, ever.
        receipt = await ingest(
            store, store.history_id,
            [
                InputMessage(role="user", content="What causes the sky to look blue?"),
                InputMessage(role="assistant", content="Rayleigh scattering of sunlight."),
            ],
            source_id="cli-session-1", idempotency_key="turn-1",
        )
        print(f"ingested seq {receipt.inserted_seq_start}-{receipt.inserted_seq_end}")

        provider = MemoizedProvider(inner=EchoProvider(), config=store.config, cache=store.cache)

        # 3. Index (explicit, host-invoked — never triggered by ingest()).
        report = await index(store, provider=provider)
        print(f"index status={report.status} committed={report.committed_coverage}")

        # 4. Retrieve without a model — always available.
        result = await retrieve(store, "blue sky")
        print(f"retrieve found {len(result.evidence)} evidence item(s)")

        # 5. Ask — retrieval + cited synthesis. Lexical search is exact-token AND-matched at
        # this milestone (no semantic ranking), so the query's tokens must all appear together
        # in one message — "sky blue", not a full natural-language question.
        answer = await ask(store, "sky blue", provider=provider)
        print(f"ask status={answer.status} answer={answer.answer!r}")

        # 6. Export / import round-trip.
        export_path = str(Path(d) / "export.jsonl")
        manifest = await export(store, export_path)
        print(f"exported {manifest.message_count} message(s), checksum {manifest.checksum[:12]}...")

        target = await HistoryStore.open(str(Path(d) / "imported.db"))
        import_report = await import_history(target, export_path)
        print(f"imported {import_report.imported_count} message(s) into new history {target.history_id}")
        await target.aclose()

        # 7. Clear — logical deletion, generation-scoped.
        clear_report = await clear_history(store, store.history_id)
        print(f"cleared: logical_clear_complete={clear_report.logical_clear_complete}")

        await store.aclose()


if __name__ == "__main__":
    asyncio.run(main())
