# chat-context-index (Python import: `cci`)

Persistent conversation memory for RAG chats and agents. Save original messages in SQLite,
then prepare recent context plus retrieved older evidence for your application's model.

Pre-release candidate. Python 3.11–3.14 is required. Install a locally built wheel with
`python -m pip install /path/to/chat_context_index-0.1.0-py3-none-any.whl`.

```python
import asyncio
from cci import HistoryStore
from cci.ingest import ingest
from cci.memory import prepare_context
from cci.models import InputMessage

async def main():
    async with await HistoryStore.open("conversation.db", config={"cache_backend": "none"}) as memory:
        await ingest(memory, memory.history_id,
                     [InputMessage(role="user", content="Deploy the service in Oslo.")],
                     source_id="chat", idempotency_key="turn-1")

    # This can run in a later process using the same durable file.
    async with await HistoryStore.open("conversation.db", config={"cache_backend": "none"}) as memory:
        context = await prepare_context(memory, "Oslo", max_chars=2000)
        print(context.text)  # Pass this history alongside your instructions, documents, and question.

asyncio.run(main())
```

Without a provider, retrieval uses local keyword search. For tree retrieval, explicitly build
the index with `cci.index.index` and pass a `cci.provider.MemoizedProvider` to indexing and
`prepare_context`. The host supplies the provider adapter; importing the package makes no
model calls. Context limits include rendered source labels; an optional tokenizer callback
can enforce a model-specific memory token budget.

Use one owning application process per history on durable local storage. The host controls
authentication, history ownership, turn scheduling, and execution checkpoints. Conversation
memory does not resume unfinished tools. Real-model evaluation exposes a source-selection gap
under default context limits; answer quality and total dollar savings remain unverified.
See the [evaluation report](https://github.com/Ajitkumar-1001/Chat-context-index/blob/main/evaluations/held_out/reports/README.md).

[Integration example](https://github.com/Ajitkumar-1001/Chat-context-index/blob/main/examples/python/rag_chat.py)
and [source repository](https://github.com/Ajitkumar-1001/Chat-context-index).
Licensed under Apache-2.0; the license text is included in the wheel.
