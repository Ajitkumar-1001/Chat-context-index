"""Linking branches a whole-history fixture cannot reach: the snapshot bound and the row cap."""

import asyncio
from pathlib import Path

from cci.config import Config
from cci.ingest import ingest
from cci.models import InputMessage
from cci.search import linked_corrections, search
from cci.store import HistoryStore

QUERY = "Which queue handles billing?"


async def _linked(path: Path, contents: list[str], max_seq: int, limit: int):
    async with await HistoryStore.open(str(path), config=Config(cache_backend="none")) as store:
        await ingest(
            store, store.history_id, [InputMessage(role="user", content=c) for c in contents], "t", "k"
        )
        hits = (await search(store, QUERY, limit=limit)).candidates
        pairs, limited = await linked_corrections(store, hits, QUERY, max_seq, limit)
        return [(source.seq, linked.seq) for source, linked in pairs], limited


def test_messages_past_the_snapshot_are_never_linked(tmp_path: Path) -> None:
    contents = ["The queue for billing is Kafka.", "Kafka is out; Pulsar replaces it."]
    assert asyncio.run(_linked(tmp_path / "a.db", contents, max_seq=1, limit=2)) == ([], False)
    assert asyncio.run(_linked(tmp_path / "b.db", contents, max_seq=2, limit=2)) == ([(1, 2)], False)


def test_row_cap_reports_limited_and_keeps_the_newest(tmp_path: Path) -> None:
    contents = ["The queue for billing is Kafka."] + [f"Kafka note {n}." for n in range(34)]
    pairs, limited = asyncio.run(_linked(tmp_path / "c.db", contents, max_seq=len(contents), limit=1))
    assert pairs == [(1, len(contents))]
    assert limited is True
