# Quick Start: No Redis

The default configuration needs no external service — the authoritative history and the memo
cache are both local SQLite files.

## Python

```bash
pip install chat-context-index
```

```python
import asyncio
from cci.store import HistoryStore
from cci.ingest import ingest
from cci.models import InputMessage
from cci.retrieve import retrieve

async def main():
    store = await HistoryStore.open("history.db")  # cache_backend defaults to "sqlite"
    await ingest(
        store, store.history_id,
        [InputMessage(role="user", content="What's our refund policy?")],
        source_id="session-1", idempotency_key="turn-1",
    )
    result = await retrieve(store, "refund policy")  # no model call
    print(result.evidence)
    await store.aclose()

asyncio.run(main())
```

## TypeScript

```bash
npm install chat-context-index
```

```javascript
import { HistoryStore, ingest, retrieve } from "chat-context-index";

const store = await HistoryStore.open("history.db");
await ingest(
  store, store.historyId,
  [{ role: "user", content: "What's our refund policy?" }],
  "session-1", "turn-1",
);
const result = await retrieve(store, "refund policy"); // no model call
console.log(result.evidence);
await store.close();
```

See `examples/python/basic_usage.py` and `examples/typescript/basic-usage.mjs` for a full
ingest → index → retrieve → ask → export/import → clear walkthrough.

## What works with zero configuration

`open()`, `ingest()`, `search()`, `get_messages()`/`getMessages()`, `retrieve()` in lexical mode,
`export()`, `import_history()`/`importHistory()`, `clear_history()`/`clearHistory()`, and
`stats()` all work with no provider and no cache backend beyond the default local SQLite memo
file. `index()` and `ask()` require a configured model provider (see
`retrieval-only-integration.md` if you only need the no-model half).
