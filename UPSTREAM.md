# Upstream Attribution

`cci` (`chat-context-index`) is a derivative work extending the conceptual foundation of
[VectifyAI/ChatIndex](https://github.com/VectifyAI/ChatIndex): a temporally ordered hierarchy of
conversation topics, summaries, and original conversational segments, with retrieval at different
levels of detail (PRD §2.2).

## Upstream commit

Frozen against commit [`7df2c92`](https://github.com/VectifyAI/ChatIndex/commit/7df2c9208db6f113f85a6c09295bec7f0f2114e7)
("Update README.md"), authored 2025-11-29, on the `main` branch. Verified as still current
`HEAD` at M0 execution time (2026-09-22) — re-checked via the GitHub API immediately before
recording this file; `research.md` §6 records the same commit as a pre-implementation candidate.

## License

Upstream is licensed Apache-2.0. This derivative retains the Apache-2.0 license (see `LICENSE`),
preserves applicable upstream notices, and identifies modified files per Apache-2.0's
redistribution requirements (PRD §13.5).

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
authoritative history, never the cache — ADR-001), explicit lifecycle and failure behavior
(typed errors, atomic transactions, generation-based deletion), optional exact-request cache
adapters (ADR-004), evidence and citation-validation contracts (INV-04, INV-05), native
Python and TypeScript package distribution with no subprocess bridge (ADR-003), and
cross-language compatibility guarantees (constitution Principle VII).

This project does not imply VectifyAI endorsement of this derivative.

## Independent maintenance status

This project is maintained independently of VectifyAI/ChatIndex; it does not track or
automatically incorporate upstream changes beyond the commit recorded above.
