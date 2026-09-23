# Conversation memory development evaluation

The original evaluation below measures **source-evidence selection**, using synthetic brand conversations and real
SQLite storage. It makes zero model calls. It is a development probe, not a held-out benchmark,
answer-quality evaluation, release gate, or assessment of UgenticAI's private product.

## Run

From the repository root, with Python 3.11–3.14:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ./packages/python
.venv/bin/python evaluations/brand_memory.py --out evaluations/results/brand-memory.json
.venv/bin/python examples/python/brand_memory.py
```

The evaluator and demo use their own temporary databases. The demo seeds histories in one child
process and reads them from another. The evaluator closes and reopens each history and retries
the original ingest batch before collecting results. It never changes a caller's existing store.

The checked-in [report](results/brand-memory.json) records the Python/SQLite versions, fixture
hash, implementation hash, selection limits, and per-case results. The implementation fingerprint
covers the imported Python package and the example context helper. A source change during an
evaluation aborts the run. Exit code 0 means lifecycle and boundary checks passed; it does not
mean every recall case passed.

## Method

The [fixture](fixtures/brand-memory.json) contains 23 messages across two histories, eight
answerable queries, and two queries without supporting evidence. Every answerable query names
an expected source record and a required text span. An evidence unit counts only if that source
is selected and the actual excerpt includes the annotated span.

| Strategy | Selection |
|---|---|
| `recent_only` | Latest eight messages, newest first for inclusion |
| `lexical_only` | Current `retrieve(..., mode="lexical")` candidates |
| `recent_plus_lexical` | Latest four messages, then lexical candidates, with duplicates removed |

All strategies use the same renderer and limits: at most eight records, 200 characters per
excerpt, and 4,000 characters in the complete rendered evidence, including labels and delimiter
escaping. Selected records are displayed in conversation order. The combined strategy can use
fewer than eight slots when lexical retrieval finds nothing. No query rewriting or semantic
model runs behind the comparison.

## Recorded result

Observed with Python 3.12.11, APSW 3.53.4.0, SQLite 3.53.4 on macOS arm64:

| Case | Recent only | Lexical only | Recent + lexical |
|---|:---:|:---:|:---:|
| Old founder story | Miss | Found | Found |
| Old brand voice | Miss | Found | Found |
| Correction after many old matches | Found | Miss | Found |
| Recent price paraphrase | Found | Miss | Found |
| Recent follow-up | Found | Miss | Found |
| Literal timezone query | Found | Found | Found |
| Timezone outside recent reserve | Found | Miss | Miss |
| Second brand's price | Found | Found | Found |
| **Complete source evidence** | **6 / 8** | **4 / 8** | **7 / 8** |

Zero history-scope or context-budget violations were observed in the ten cases. Both histories
retained their IDs and original ordered records after reopen; replaying the same batch added no
duplicates. History separation here means the host opened the correct independent database.
It does not establish an authorization system or isolation under arbitrary application misuse.

The missing-answer cases report whether context is empty and whether another history's records
appear. Recent context can be useful even when it does not answer a specific question, so an
empty/nonempty context is not scored as model abstention. No answer was generated.

## What the misses tell us

- Chronological lexical results can exhaust the candidate limit with older repeated statements,
  excluding a later correction.
- A recent window supports follow-ups and recent paraphrases but loses older relevant facts.
- Combining the two helps some cases and regresses another: the timezone preference is outside
  the four-message reserve, and the natural-language query misses the literal stored wording.
- Preserving a correction as evidence does not prove a model will apply it correctly.

The dataset is small, deliberately diagnostic, and was visible during implementation. Its
fractions are descriptive counts, not population accuracy estimates. It must not substitute for
the separate held-out evaluation and release criteria already specified for the library.

## Boundary and lifecycle checks

With the package and pytest installed, run:

```bash
.venv/bin/python -m pip install pytest==9.1.1
.venv/bin/python -m pytest -q tests/examples/test_conversation_context.py
.venv/bin/python -m pytest -q tests/conformance/test_ingest_resume.py tests/conformance/test_ingest_idempotency.py tests/conformance/test_retrieve_lexical.py tests/conformance/test_failure_ingest.py
```

Observed locally: **5 example tests passed**, and **7 existing conformance tests passed**. The
checks cover preserved original text after excerpting, bounded rendered context, escaped
historical delimiters, selected-history separation, exclusion of a later append, invalidation
after a concurrent clear, safe replay, and actual process crashes around ingestion commit.

These runs exercised the source checkout using an existing local dependency environment. The
setup commands above describe fresh reproduction; a fresh package installation, live-provider
evaluation, Redis behavior, and TypeScript package parity were **NOT RUN** as part of this work.

## Tree memory mechanics

The additional [tree evaluator](tree_memory.py) exercises actual multi-level indexing, a no-change
rerun, reopening the store, navigation, and context packing. Its provider double echoes summaries
and selects a fixture marker. It tests mechanics, not whether an LLM understands a paraphrase.
It makes no network calls. Run with the installed Python package:

```bash
python evaluations/tree_memory.py --out evaluations/results/tree-memory.json
npm --prefix packages/typescript run build
python -m pytest -q tests/memory tests/examples
node --test tests/memory/tree_memory.test.mjs
```

The [report](results/tree-memory.json) records source/environment fingerprints, 128 synthetic
messages, actual request character counts, indexing batches, and original-source sequences.
It reopens the same history before retrieving the old deployment decision and recent records.

| Stage | Observed calls / input characters |
|---|---:|
| Initial indexing, three bounded batches | 171 calls / 459,836 characters |
| Indexing with no new messages | 0 calls |
| Tree navigation | 4 calls / 19,492 characters |
| Final selected memory context | 4,868 characters |
| Full-history evidence baseline | 207,260 characters |

Navigation plus selected memory is 24,360 input characters for this query. Including initial
indexing, the input-character calculation crosses the full-replay baseline at three identical
queries. **This is not a billing break-even:** model tokenization, output tokens, routing quality,
different model prices, prefix-cache discounts, updates, and latency can change the outcome.
The report includes output character counts but never fabricates missing token usage. Both
baselines omit common answer instructions, document results, the question, and answer output.

Native tests use [the shared fixture](../spec/fixtures/tree-memory.json) against real SQLite.
They cover hierarchy reuse, reopen across runtimes, source grounding, bad IDs, bounded attempts,
token-counter hooks, and invalidation during navigation. These are source-checkout checks;
fresh-wheel/tarball compatibility and live-model evaluations remain separate release gates.
