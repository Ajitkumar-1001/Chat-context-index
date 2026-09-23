/** Adapter for an existing host's document retriever and model. Serialize turns per history. */
// Repository example: run the package build first. In an installed host application,
// import these same exports from "chat-context-index".
import { ingest, prepareContext } from "../../packages/typescript/dist/index.js";

export async function chatTurn(store, question, turnId, { retrieveDocuments, generate, memoryProvider }) {
  const memory = await prepareContext(store, question, { provider: memoryProvider });
  await ingest(store, store.historyId, [{ role: "user", content: question }], "rag-chat", `${turnId}:user`);
  const documents = await retrieveDocuments(question);
  const answer = await generate({ question, documents, conversationMemory: memory.text });
  await ingest(store, store.historyId, [{ role: "assistant", content: answer }], "rag-chat", `${turnId}:assistant`);
  return { answer, memory };
}
