# chat-context-index (native TypeScript)

Persistent conversation memory for RAG chats and agents. Save original messages in SQLite,
then prepare recent context plus relevant older evidence for your application's model.

Pre-release candidate. Node.js 22 or later is required; this package uses ESM.
Install a locally built archive with `npm install /path/to/chat-context-index-0.1.0.tgz`.

```typescript
import { HistoryStore, ingest, prepareContext } from "chat-context-index";

const memory = await HistoryStore.open("conversation.db", { cacheBackend: "none" });
try {
  await ingest(memory, memory.historyId,
    [{ role: "user", content: "Deploy the service in Oslo." }], "chat", "turn-1");
} finally { await memory.close(); }

// This can run in a later process using the same durable file.
const resumed = await HistoryStore.open("conversation.db", { cacheBackend: "none" });
try {
  const context = await prepareContext(resumed, "Oslo", { maxChars: 2000 });
  console.log(context.text); // Pass alongside your instructions, documents, and question.
} finally { await resumed.close(); }
```

Without a provider, retrieval uses local keyword search. For tree retrieval, explicitly call
`index(memory, provider)` and pass the same `BoundedProvider` adapter to `prepareContext`.
The host supplies the adapter; importing the package makes no model calls. Context limits
include rendered source labels; an optional tokenizer callback can enforce a model-specific
memory token budget. Public TypeScript declarations are included.

Use one owning application process per history on durable local storage. The host controls
authentication, history ownership, turn scheduling, and execution checkpoints. Conversation
memory does not resume unfinished tools. Real-model recall and total dollar savings remain
unverified.

[Integration example](https://github.com/Ajitkumar-1001/Chat-context-index/blob/main/examples/typescript/rag-chat.mjs)
and [source repository](https://github.com/Ajitkumar-1001/Chat-context-index).
Licensed under Apache-2.0; the license text is included in the archive.
