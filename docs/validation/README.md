# Human release validation

These instructions prepare release gates R08 (human answer review) and R11 / SC-010
(two unfamiliar developers). **Neither gate has run.** A script, agent, template, or
successful automated installation is not a participant or a human approval.

Use the [review protocol](human-review.md) for held-out answers and the
[adoption task sheet](developer-adoption.md) for developer trials. No one has been
contacted through this kit. The owner supplies the people and retains their actual
attestations; public records may use stable pseudonyms.

Before a session, the coordinator supplies the exact approved candidate archives and
a read-only copy of its documentation. Copy each blank record into a new session
directory; do not fill the templates with invented results. Preserve failed attempts,
timestamps, artifact hashes, submitted programs, and sanitized terminal output.
Keep credentials and personal contact details out of the evidence directory.

The current runtime is identified by
[the package report](../../evaluations/results/release-readiness/package-memory-20260924.json).
Final package hashes must be filled from the rebuilt release manifest: packaging
metadata is being updated, so previous archive hashes are not final adoption pins.
Documentation changes after a failed adoption attempt require a new documentation
fingerprint and another independent attempt of the affected path.

Prepared here:

- `prepare_review.py`: deterministic, offline selection and export from immutable native
  or comparison reports. It does not call a model, alter scores, or mark review complete.
- `human-review-record.template.json`: actual reviewer decisions and adjudication.
- `adoption-record.template.json`: one real developer's environment, steps, outputs,
  interventions, and attestation. Two distinct records are required.
- Acceptance checks in the two protocols. Blank fields, missing logs, agent-only work,
  or absent human attestations keep the relevant gate **NOT RUN / INCOMPLETE**.

New execution records belong under
`evaluations/results/production-readiness-20260924/` (or a later uniquely named run).
Do not replace prior results or edit the frozen evaluator to obtain favorable scores.
