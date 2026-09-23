# Retrieval-Only Integration

If you only need durable history and lexical recall — no cited-answer synthesis, no topic
indexing — you never need to configure a model provider at all.

```python
store = await HistoryStore.open("history.db")
await ingest(store, store.history_id, messages, source_id="s1", idempotency_key="k1")
result = await retrieve(store, "refund policy")
for e in result.evidence:
    print(e.excerpt, e.source_pointer)
```

`retrieve()` never requires a provider — `ConfigurationError` is only raised by the two
model-requiring operations, `index()` and `ask()` (contracts/operations.md: an operation in the
"No" column for "Requires model config" must not fail merely because no provider is configured).

## What you get without a provider

- **Durable, identity-preserving storage** — `ingest()`, `get_messages()`, `export()`,
  `import_history()`, `clear_history()`.
- **Deterministic lexical search** — `search()`, and `retrieve()` in its lexical fallback path
  (`routing.actual_mode: "lexical"`, reported alongside `coverage.index_degraded: true` since no
  topic tree exists without `index()`).
- **Diagnostics** — `stats()`, with zero provider-related counters.

## What you lose without a provider

- No topic tree (`index()` never ran), so `retrieve()`'s `tree`/`auto` modes degrade to lexical
  candidates only — still correct, just without hierarchical navigation over very large
  histories.
- No cited-answer synthesis (`ask()`) — build your own prompt from `retrieve()`'s evidence and
  call your model directly if you want a narrower integration than `ask()`'s built-in
  one-pass-plus-repair pipeline.
