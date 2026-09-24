# Upstream Attribution

`cci` (`chat-context-index`) is a derivative work extending the conceptual foundation of
[VectifyAI/ChatIndex](https://github.com/VectifyAI/ChatIndex): a temporally ordered hierarchy of
conversation topics, summaries, and original conversational segments, with retrieval at different
levels of detail.

## Upstream commit

Frozen against commit [`7df2c92`](https://github.com/VectifyAI/ChatIndex/commit/7df2c9208db6f113f85a6c09295bec7f0f2114e7)
("Update README.md"), authored 2025-11-29, on the `main` branch. Verified as still current
`HEAD` when checked on 2026-09-22. This attribution is pinned to that commit; it does
not claim the upstream project has remained unchanged.

## License

Upstream is licensed Apache-2.0. This derivative retains the Apache-2.0 license (see `LICENSE`),
preserves applicable upstream notices, and identifies the work's relationship to ChatIndex.

## Reused components

The conceptual foundation retained from ChatIndex: a topic-tree hierarchy over conversation
history, with retrieval that can operate at multiple levels of detail (raw messages, chunks,
topic summaries).

The current hierarchy and bounded router are independent implementations of that idea.
ContIndex groups consecutive summarized chunks and reuses unchanged branches; it does not
port ChatIndex's LLM topic-boundary classification or autonomous retrieval tool loop. Generated
summaries guide retrieval, while returned evidence resolves to original message fields.

## Major modifications and independent contribution

This derivative is independently maintained and contributes work not present upstream:
dependable persistence with explicit lossless-preservation guarantees (SQLite + FTS5 as
authoritative history, never the cache), explicit lifecycle and failure behavior
(typed errors, atomic transactions, generation-based deletion), optional exact-request cache
adapters, evidence and citation validation, native Python and TypeScript packages with no
subprocess bridge, and cross-language compatibility checks.

This project does not imply VectifyAI endorsement of this derivative.

## Independent maintenance status

This project is maintained independently of VectifyAI/ChatIndex; it does not track or
automatically incorporate upstream changes beyond the commit recorded above.
