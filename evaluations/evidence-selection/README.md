# Evidence selection: implementation and verification

The Python and native TypeScript implementations now retain query-relevant original messages
before applying retrieval and context limits. This addresses the independently reproduced
selection failure. Implementation checks are **PASS**; release quality remains **NOT VERIFIED**
for this build because its live smoke stopped on provider HTTP 429 during indexing.

## Problem and decision

The [original development probe](../results/context-selection-probe.json) selected the correct
tree leaf, but prepared context omitted the needed message at sequence 13. Chronological
selection filled the older-evidence slots with sequences 1–4. Lexical search also required all
question tokens, so an ordinary question missed a source that a single keyword retrieved.

Use deterministic matching at the evidence-selection boundary:

1. Extract at most 32 distinct terms from the first 4,096 Unicode scalars of the question.
   Matching removes combining accents, normalizes case, and omits a small English function-word
   list. All FTS terms are quoted parameters; user input cannot become FTS syntax.
2. Rank lexical matches by the number of distinct matched terms, then descending sequence,
   **before** limiting candidates. Repeated old statements receive no term-frequency advantage.
3. Within a long original field, choose a contiguous window covering the most distinct terms;
   later equally scoring windows win. Keep original text and its exact source-field pointer.
4. Rank retrieved originals before the global evidence budget. Prepared context keeps its recent
   reserve, then admits ranked evidence. Deduplicate by message ID and source pointer, and render
   the selected records chronologically.

Increasing context limits would spend more host-model tokens without addressing selection.
An additional model reranker would add provider work and another failure boundary. The chosen
local rule fixes the development regressions with no added model requests or dependencies.
It does not establish semantic similarity or automatically decide which conflicting fact is true.

## Contracts and rollout

Public entry points remain `search`, `retrieve`, and `prepare_context` / `prepareContext`.
`pack_context(..., query="...")` and `packContext(items, { query: "..." })` optionally choose
matching excerpts; omitting the query preserves prefix truncation and caller priority order.
The context APIs pass the question automatically.

Search now returns relevance-first candidates and validates a limit of 1–5,000. Prepared output
still uses conversation order. Original payloads, message identities, source pointers, storage
schema, tree prompts, cache inputs, snapshot invalidation, and provider-call limits are unchanged.
Rendered labels, escaped delimiters, and optional tokenizer callbacks still count toward budgets.
No migration or reindex is required; reinstall the new wheel or npm archive. A rollback can
reinstall the prior candidate against the same store.

Lexical matching remains dependent on shared words; no-overlap paraphrases and languages beyond
the English function-word policy are not validated here. Recency is only a tie-breaker, not proof
that a later statement is a correction. Source retrieval is not an answer-quality score.

## Executed checks

All checks use development data or package contracts, never reserved labels for tuning.
The six [shared regression histories](../../spec/fixtures/evidence-selection.json) cover a fact
inside a leaf, a later correction after many old matches, structured Unicode text, a small
evidence budget, identical text with distinct identities, and a matching span in a recent record.
Both lexical and tree paths retain required original spans while enforcing ordering and budgets.

| Check | Observed result |
| --- | --- |
| Python development regressions | 17 pass; the 12 lexical/tree cases failed on the old implementation |
| Native TypeScript memory suites | 21 pass, including all 12 shared cases and empty/absent search |
| Full Linux Python suites | 190 pass, zero skipped, including real Redis and conformance |
| Static checks | Ruff, mypy (26 source files), and TypeScript build pass |
| Fresh installed packages | All four writer-reader combinations pass on macOS and Linux; strict TypeScript consumer check passes |
| Installed-wheel development probe | Required sequence 13 survives raw retrieval and prepared context; the natural question finds its lexical source |
| Unchanged brand development fixture | Recent 6/8; lexical 7/8 (previously 4/8); combined 8/8 (previously 7/8); zero scope or context-budget violations |
| Shared Linux, 10,000 messages | Search p95 54.56 ms; 100-message ingest p95 27.83 ms; reopen p95 1.53 ms |

Evidence: [installed probe](../results/context-selection-fixed-installed.json),
[brand results](../results/brand-memory-evidence-selection.json),
[macOS package verification](../results/package-memory-evidence-selection.json), and
[Linux commands, test counts, packages, and performance](../results/linux-evidence-selection/README.md).
These performance measurements use a shared local Docker host, not the dedicated release runner.

The verified macOS artifacts are in `dist/evidence-selection/` (generated, not committed):

| Artifact | SHA-256 |
| --- | --- |
| `chat_context_index-0.1.0-py3-none-any.whl` | `81c35955507719b919bd04341d85a6ea863c3976a6278068adcbd7540b3f7254` |
| `chat-context-index-0.1.0.tgz` | `ed53f730f5cdc40920347c802189736108e4d1b10b452de504c91a27fd324053` |

Python source fingerprint: `8bc076a88a6218358a2c829b6a5c0d59073f4b6daa559e01ee8f5fad874b56c5`.
TypeScript source fingerprint: `3dccbbce528339e9107ff3d51fa834cd0ac430c103a49422f1c8e0c6804455ff`.

## Live verification and remaining gate

The [preflight](../results/model-config-evidence-selection.json) verified configuration and exact
installed-wheel source identity without model calls. The subsequent
[live smoke](../results/live-smoke-evidence-selection.json) used the unchanged four-message
development fixture and `gemini-3.5-flash-lite`. Its first indexing call succeeded with 76 input
and 31 output tokens; the second returned HTTP 429 with unknown usage. Retrieval was not reached.
There were no retries. The run preserves both attempts and never logs credentials or raw errors.
The [accounting update](../results/evidence-selection-accounting.json) includes this smoke and the
prior comparison ledger: 496 attempts, four calls with unknown usage, and conservative reservations
within the original allowance. It does not substitute unknown usage with zero.

The previous successful live smoke and unsuccessful held-out comparison used the old wheel.
They do not validate this build. The frozen held-out fixture and evaluator remain unchanged.
After provider quota permits a bounded smoke, the next quality check is a newly frozen evaluation
of this wheel at the existing three-trial thresholds, including rubric review and complete usage
accounting. Prior failed-call usage must remain unknown and charged against its reservations.
Native `ask()` quality, independent review, hosted CI, dedicated performance verification, and
registry configuration remain separate release gates. No package was published.

## Reproduce offline checks

With repository development dependencies and Docker available:

```bash
python -m pytest tests/conformance tests/memory tests/examples tests/evaluations -q
npm --prefix packages/typescript run build
node --test tests/memory/*.test.mjs
python tests/packaging/verify_packages.py \
  --report /tmp/cci-packages.json --artifacts-out /tmp/cci-artifacts
python evaluations/context_selection_probe.py --out /tmp/cci-context-probe.json
python evaluations/brand_memory.py --out /tmp/cci-brand-memory.json
python benchmarks/reference_fixture_benchmark.py --out /tmp/cci-performance.json \
  --label development-verification
```

Run the probe with the freshly installed wheel's Python interpreter and `-I` to verify installed
behavior. The probe's `prepared_context_contains_required_source` field must be true; its static
interpretation describes the original failure mechanism, not the result of every run.
