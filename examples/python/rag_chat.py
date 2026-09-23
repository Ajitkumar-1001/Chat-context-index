"""Wire ContIndex into an existing application's RAG chat loop.

The host supplies document retrieval, generation, authorization, stable turn IDs, and
per-history serialization. Index separately with cci.index.index on the host's schedule.
"""
from cci.ingest import ingest
from cci.memory import prepare_context
from cci.models import InputMessage


async def chat_turn(store, question, turn_id, *, retrieve_documents, generate, memory_provider=None):
    # Capture prior context before saving this new user message.
    memory = await prepare_context(store, question, provider=memory_provider)
    await ingest(store, store.history_id, [InputMessage(role="user", content=question)],
                 source_id="rag-chat", idempotency_key=f"{turn_id}:user")
    documents = await retrieve_documents(question)
    # The host's generation adapter keeps trusted instructions separate from both data inputs.
    answer = await generate(question=question, documents=documents, conversation_memory=memory.text)
    await ingest(store, store.history_id, [InputMessage(role="assistant", content=answer)],
                 source_id="rag-chat", idempotency_key=f"{turn_id}:assistant")
    return answer, memory
