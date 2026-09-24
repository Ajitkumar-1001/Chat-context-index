# Preregistered human answer review, version 1

Status: **PREPARED; NOT RUN**. Freeze this document and `prepare_review.py` by SHA-256
in the next evaluation manifest before collecting new held-out outcomes. This protocol
has been written against evaluator schemas and invented test cases, without inspecting
new held-out answers. Do not change it after seeing results; retain any amendment as a
new version and explain why it was needed.

## Scope and reproducible selection

Run selection separately for native `ask()` (lexical and auto) and memory comparison
(full history, recent/lexical, and tree). Each has three trials and all 40 held-out
queries. The native and comparison citation namespaces differ: native answers cite
`evidence_id`; comparison answers cite original `message_id`. Keep both identities.

Mandatory review includes **every non-passing case**, including missing/unexecuted rows,
exceptions, partial/suppressed output, missing or failed model review, incomplete
evidence recall, wrong answers, invalid citations, and unsupported citations. For an
absent-answer query only the surface's explicit abstention status is a passing case.
An answerable case passes this sampling screen only if evidence recall is 1, it was
answered with structurally valid citations, the model judge marked it correct, and
every cited item was reviewed as supporting its claim. This conservative case screen
does not change the aggregate release thresholds.

Additionally sample passing cases within each **surface × trial × strategy × category**:

- `correction`: answerable with a nonempty rubric `must_not_cite_values`.
- `old_fact`: other answerable queries in this historical-memory fixture.
- `absent`: queries marked unanswerable.

For each nonempty passing stratum choose `min(N, max(3, ceil(0.20 × N)))` cases. Sort by
the SHA-256 of UTF-8 `7|<surface>:<trial>:<strategy>:<query_id>`, breaking ties by case ID.
Seed `7`, the category rule, and the sample size are fixed. Retain counts for empty
strata; do not substitute easier cases. Selection is independent of row order and
does not consume randomness from the model evaluator.

After answer generation and model judging have finished, export a packet:

```bash
python3 docs/validation/prepare_review.py \
  --surface native --report /absolute/path/to/native/report.json \
  --fixture evaluations/held_out/fixture.json \
  --out /absolute/path/to/native-human-packet.json
python3 docs/validation/prepare_review.py \
  --surface comparison --report /absolute/path/to/comparison.json \
  --fixture evaluations/held_out/fixture.json \
  --out /absolute/path/to/comparison-human-packet.json
```

The exporter verifies the fixture hash in each report's plan, checks expected slots,
retains missing cases, rejects duplicate/unknown rows, records input hashes, and refuses
to overwrite an existing packet. It includes query/rubric, required original spans,
returned evidence and citations, answer/status, and model judgments. A partial report
can yield a review packet; it cannot establish a complete evaluation. Never feed the
packet, rubric, or reviewer comments back to answer generation or implementation tuning.

## Actual review

1. Assign a real human reviewer who did not generate or implement the evaluated answers.
   Record a stable ID, role/relationship, UTC start/end, and a signed/date-stamped
   attestation. The owner privately retains the ID-to-person mapping and original
   attestation. An AI agent or model judge cannot fill that role.
2. First inspect the question, rubric, original history/spans, returned evidence, answer,
   and citation identity. Record the initial rubric decision and a support decision for
   **each** cited item before reading the model judgment. This is an auditable instruction,
   not a claim that the packet UI blinds the reviewer; it includes the judgment.
3. For answerable cases record correctness against the whole rubric, evidence coverage,
   stale-fact use, citation structural validity, and semantic support. A relevant-looking
   quote does not support an unrelated claim. For absent cases assess abstention; execution
   failure or suppressed output does not count as a correct abstention.
4. Compare with the model judgment and record agreement or disagreement and a concrete
   reason with the original pointer/span. Do not erase the initial judgment.
5. Every disagreement or uncertain human judgment needs a second real human's independent
   decision and dated adjudication. If unresolved, mark `UNRESOLVED` and keep release
   blocked. Retain both judgments. A substantiated false pass triggers review of the
   remaining passing cases in the same stratum; preserve that expanded sample separately.

The fixture's original histories are the authority if a selected excerpt omits important
context. Word-for-word copies may satisfy annotated spans under the accepted scoring
rule; stale superseded statements may not. Copies keep their original distinct IDs.

Copy [the review record template](human-review-record.template.json) once per packet.
Fill one decision per selected case. A citation assessment records the exact citation ID,
validity, support, and explanation. For cases without citations use an empty list and an
explicit no-citation disposition; never infer 100% support. Preserve every failure.

## Acceptance checks

The coordinator independently verifies all of these before recording R08 complete:

- Packet, report, fixture, candidate, evaluator, and protocol hashes match the frozen
  manifest; the full expected result universe and selected IDs can be reproduced.
- Both evaluation surfaces and all three trials are represented. Native lexical/auto
  and all three comparison strategies remain separately identifiable.
- Every selected case, plus required expanded review, has an actual human decision;
  there are no missing/duplicate case IDs, unanswered citation assessments, or unresolved
  disagreements. Reviewer attestations and their evidence hashes are present and verified.
- Model judgments, initial human decisions, adjudication, and any overrides remain
  distinguishable. Recomputed per-trial metrics retain all expected queries and every
  failure in their denominators. Unsampled model judgments remain labeled as such.
- The existing quality gates still hold in every required trial after substantiated
  corrections: recall ≥0.85, correctness ≥0.80, ≥7/8 correct abstentions, citation validity
  1.00, support ≥0.90, and auto recall no worse than matched-budget lexical recall.
- Cost claims separately satisfy complete token/cost accounting and the frozen comparison
  rule. Human agreement does not repair incomplete usage or failed execution.

`record_status` can become `COMPLETE` only after those checks. This is completion of human
review, **not** automatic release approval: failed product gates still block release.
