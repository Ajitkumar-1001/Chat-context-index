/** Adapter for an existing host's document retriever and model. Serialize turns per history. */
import { ingest, prepareContext } from "chat-context-index";

export async function chatTurn(store, question, turnId, { retrieveDocuments, generate, memoryProvider }) {
  const memory = await prepareContext(store, question, { provider: memoryProvider });
  await ingest(store, store.historyId, [{ role: "user", content: question }], "rag-chat", `${turnId}:user`);
  const documents = await retrieveDocuments(question);
  const answer = await generate({ question, documents, conversationMemory: memory.text });
  await ingest(store, store.historyId, [{ role: "assistant", content: answer }], "rag-chat", `${turnId}:assistant`);
  return { answer, memory };
}
