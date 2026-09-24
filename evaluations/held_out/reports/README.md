# ContIndex held-out memory comparison

Execution: **INCOMPLETE**. Tree quality: **FAIL**. Lower-cost claim: **NOT_ESTABLISHED**.

Model: `gemini-3.5-flash-lite`. Planned workload: three trials of the same 40 synthetic held-out queries, with 32 answerable and 8 absent-answer cases. These are 40 unique cases, not 120 independent questions. All strategies use the same host answer prompt.

Answer generation finished for trials 1, 2. Remaining work was interrupted: `provider_http_429`. The continuation retained the original failed request and attempted only work with no recorded dispatch; recovery indexing is included in usage. Completed model rubric reviews: 0. Correctness remains unverified for unreviewed answers. Raw zero scores for them are conservative gate values, not measured accuracy.

## Results

This table covers the 2 completed answer-generation trials only. Recall averages their scores; absent-answer counts combine them. Every planned trial appears in the next table and remains in the raw report. Failed and unexecuted cases remain in denominators. Correctness and semantic citation support use a planned separate call to the same model after answers are collected, not independent human review.

| Strategy | Original evidence recall | Reviewed correctness | Correct abstentions | Unsupported answers on absent cases | Quality trials passed |
| --- | ---: | ---: | ---: | ---: | ---: |
| `full_history` | 100.0% | unverified | 16/16 | 0 | 0/2 |
| `recent_lexical` | 0.0% | 0.0% | 16/16 | 0 | 0/2 |
| `tree` | 3.1% | unverified | 16/16 | 0 | 0/2 |

Abstaining on an answerable case is scored as incorrect without a model review. Generated answers require rubric review to count as correct.

| Trial | Strategy | Model outputs | Recall | Correctness | Citation validity | Citation support | Quality |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | `full_history` | 40/40 | 100.0% | unverified | 100.0% | unknown | FAIL |
| 1 | `recent_lexical` | 40/40 | 0.0% | 0.0% | unknown | unknown | FAIL |
| 1 | `tree` | 40/40 | 3.1% | unverified | 100.0% | unknown | FAIL |
| 2 | `full_history` | 40/40 | 100.0% | unverified | 97.5% | unknown | FAIL |
| 2 | `recent_lexical` | 40/40 | 0.0% | 0.0% | unknown | unknown | FAIL |
| 2 | `tree` | 40/40 | 3.1% | unverified | 100.0% | unknown | FAIL |
| 3 | `full_history` | 19/40 | incomplete | unverified | 100.0% | unknown | FAIL |
| 3 | `recent_lexical` | 18/40 | incomplete | unverified | unknown | unknown | FAIL |
| 3 | `tree` | 18/40 | incomplete | unverified | 100.0% | unknown | FAIL |

Correction cases in the completed generation trials:

| Strategy | Correction cases | Required original evidence recall |
| --- | ---: | ---: |
| `full_history` | 52 | 100.0% |
| `recent_lexical` | 52 | 0.0% |
| `tree` | 52 | 0.0% |

## Usage and cost

Application usage includes initial indexing, appended-history updates, navigation, and answers. It includes the incomplete third trial and database-recovery calls, so these totals are not a complete-workload cost comparison. Review usage is evaluation overhead and is shown separately. These are observed API tokens; price estimates use standard list prices, without cache discounts or free-tier allowances. They are not billing receipts. An asterisk marks incomplete usage.

| Strategy | Application calls | Input tokens | Output tokens | Standard price estimate | Review calls | Review price estimate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `full_history` | 99 | 2,064,031 | 4,676 | $0.630899 | 0 | $0.000000 |
| `recent_lexical` | 99 | 29,983 | 1,034 | $0.011580* | 0 | $0.000000 |
| `tree` | 276 | 384,468 | 9,162 | $0.138245* | 0 | $0.000000 |

Actual usage by operation across all three trials:

| Operation | Calls | Input tokens | Output tokens |
| --- | ---: | ---: | ---: |
| Initial tree indexing | 57 | 231,701 | 4,027 |
| Appended-history indexing | 20 | 58,437 | 1,470 |
| Recovery initial indexing | 3 | 9,802* | 136* |
| Recovery update indexing | 0 | 0 | 0 |
| Tree navigation | 98 | 44,213 | 2,126 |
| Host answers, all strategies | 296 | 2,134,329* | 7,113* |
| Rubric and citation review | 0 | 0 | 0 |

Rates: $0.30/million input tokens and $2.50/million output tokens, verified 2026-09-23. [Google pricing](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.5-flash-lite).

A cheaper run qualifies only if all quality gates pass, usage is complete, and recall/correctness are within five percentage points of the baseline in every trial. No savings percentage from an ineligible comparison is presented as a supported product claim.

## Run history and limits

The combined run made 474 calls over 12172.4 seconds of wall time, including pauses between attempts. Observed input/output tokens: 2,478,482/14,872. Unknown usage: `True`. The standard price estimate, including review, is $0.780725.

The combined call ledger carries forward 471 calls from [v2](../comparison-live-v2.json); those calls must not be added again. The continuation completed 0 of 64 previously undispatched queries. Both HTTP 429 failures and their unknown usage remain recorded.

- Earlier attempt: INCOMPLETE, stopped reason `provider_http_429`; 18 calls, 58,767 input and 731 output tokens observed; unknown usage `True`.
- Diagnostic request: PASS; 12 input and 5 output tokens.
- Diagnostic request: PASS; 7 input and 11 output tokens.

All attempts combined: **494 calls**, **2,537,268 input tokens** and **15,619 output tokens** observed, with a standard price estimate of **$0.800228** for known usage. The [accounting audit](../accounting-integrity.json) verifies the original $3, 1,200-call and 6-million-token client allowances, including reservations for unknown usage.

The separate v1 attempt and diagnostic requests are excluded from the main strategy comparison. Their observed usage and retained reservations count against the overall evaluation budget. Unknown usage prevents an exact all-attempt billing total.

The operational continuation changes pacing, remaining allowances, and database reconstruction after rate-limit failures. The fixture, model, answer prompt, retrieval settings, and scoring were not tuned to held-out results.

Correction-case correctness and semantic citation support remain unverified. The failed call was not retried or credited as an abstention. This run cannot close the three-trial release gate or establish a lower-cost claim.

## Independent development findings

A separate [development probe](../../results/context-selection-probe.json) uses 28 invented messages and deterministic selection of the only tree leaf, with no external model calls. Raw tree retrieval includes the required message at sequence 13. Default context assembly drops it and keeps sequences 1–4 and 25–28. This demonstrates loss during source selection even when tree navigation reaches the correct leaf; it does not explain every held-out miss.

The same probe finds the source with the keyword `codename`, while its natural-language question returns no lexical candidates. Source code joins every query token with implicit FTS AND semantics. Both findings point to development work on source relevance before context limits are applied. The benchmark implementation was left unchanged.

The [local Linux checks](../../results/linux-release/README.md) passed 165 Python tests, 8 native TypeScript tests, and fresh-package checks in all four writer-reader combinations. A shared Linux container also passed the reference timing thresholds. GitHub release environments and a dedicated Linux performance run remain separate gates.

## Reproduction and scope

- Raw report: [`comparison-live-v3.json`](../comparison-live-v3.json).
- Frozen plan and evaluator/wheel/fixture hashes are embedded in the raw report.
- Run commands and method: [evaluation README](../README.md#budgeted-memory-comparison).
- Each history: 80% initial ingestion/indexing, 20% append/update, SQLite close/reopen, then five queries. Both bounded strategies use four recent items, eight total items, 4,000 context characters, and 200-character excerpts.
- Every trial has a fresh database and disabled memoization. Tree fallback modes, exact original excerpts, per-call usage, and review judgments are in the raw report.
- This measures the Python memory integration with a common host prompt. Native `ask()` quality, native TypeScript live-model evaluation, independent human review, and real customer workloads remain outside this report.
