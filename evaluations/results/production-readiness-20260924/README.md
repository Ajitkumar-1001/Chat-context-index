# Production readiness execution — September 24, 2026

**BLOCKED for production release.** Release hardening is merged and verified in hosted CI;
real-model quality, reference performance, actual human validation, and registry
identities are not established. This work created no registry publication or release tag.

## Completed work

- The updated release/security workflows and npm repository metadata passed all
  [16 hosted package combinations](hosted-verification.json), 266 Python tests, 21 Node tests,
  lint/typechecks, and security scans. All 21 jobs passed in
  [run 36043232899](https://github.com/Ajitkumar-1001/Chat-context-index/actions/runs/36043232899)
  on main commit `b3bb918be1edb21bddb735ad92b5a52d59deafb7`.
- Release publishing now verifies source commit, workflow run, tag/version, package identity,
  and archive checksums before copying the exact CI-installed artifacts. It requires an
  explicit `RELEASE_APPROVED_SHA` in addition to protected environment approval.
- The downloaded CI candidate passed that guard for version `0.1.0`. The
  [manifest](hosted-artifact-manifest.json) binds all four archive hashes to the run and
  source commit; the three publication archives were copied without rebuilding.
  This verification did not approve or publish a release.
- GitHub's `release` environment now requires owner approval and accepts only `v*` tags.
  An active ruleset limits version-tag creation, updates, and deletion to the owner.
  The final source approval variable has not been set.
- [Fresh package verification](release-controls/package-memory.json) passed all nine
  writer/reader combinations after adding npm repository identity metadata. The Python
  wheel is unchanged; the npm/source archive identities are newly recorded in that report.
- [Release-control checks](release-controls/README.md) passed 16 regression tests,
  dependency audits, secret scans of history/source/unpacked archives, and the scoped
  license/attribution review. No secrets or known dependency vulnerabilities were found.
- A bounded capacity diagnostic reuses the frozen evaluator without declaring provider
  capacity available first. Eight diagnostic tests and the existing 31 runner tests pass.
- The [fresh installed-package live smoke](live-smoke-retry-003-summary.json) passed with
  exact original-evidence retrieval after reopen, eight successful model calls, and complete
  actual token usage. The frozen fixture, evaluator, and CI-verified wheel were unchanged.
- Reference-runner tooling (`benchmarks/run_reference_gate.py`) verifies
  hardware/reservation and installed archives, measures Python and TypeScript at 10k/100k,
  and includes process-cold and filesystem-cold checks. Seven gate tests and installed
  helper smokes pass; [local evidence](performance-preparation/README.md) is not a reference run.
- Human validation materials, since removed from the repository, included a preregistered
  sample, offline packet exporter, independent adoption tasks, and empty attestation records.
  Eleven invented-data tests passed; no human completion has been substituted by an agent.

## Live smoke and accounting

The [first installed-package smoke](live-smoke-summary.json) stopped at its frozen
20-second timeout, retaining a 1,061-token reservation. A subsequent read-only model-list
request confirmed the endpoint was reachable and the configured model was listed.

A [separately allocated follow-up](live-smoke-extended-summary.json) used a 60-second
timeout within the original cumulative allowance. Two indexing calls succeeded and reported
151 input tokens plus 66 output tokens; a third returned HTTP 503 without usage. The smoke
did not reach original-evidence retrieval. Across both runs there were four generation
attempts, two with unknown usage; the combined charged/reserved amount is 2,345 tokens.
Both allocations remain consumed; their unknown usage is never counted as free.

The user then authorized one fresh bounded attempt. [Retry 003](live-smoke-retry-003-summary.json)
**passed**: six indexing calls and two tree-navigation calls reported **1,058 input tokens and
301 output tokens**, with no errors, retries, or unknown usage. After reopening SQLite, the
installed package returned the exact original message, “The deployment target is Oslo.”,
with sequence 1, its message ID, and `/content` source pointer. Lexical retrieval found no
evidence; unchanged indexing made zero calls.

The new run's standard-price estimate is **$0.0010699**, below its $0.04 ceiling. This is an
estimate from API token counts, not an invoice or cost-savings result. Across these three
runs there were 12 attempts, two with unknown usage, and 3,704 charged/reserved tokens.
All three allocations remain consumed. The cumulative accounting and allocation-book
snapshots remain local. No held-out or cache trials were started, and this small smoke
does not establish sustained provider capacity.

The configured model's pricing was checked against
[Google's pricing documentation](https://ai.google.dev/gemini-api/docs/pricing).
Account-specific limits require the owner's provider account information; see
[Google's rate-limit documentation](https://ai.google.dev/gemini-api/docs/rate-limits).

## Remaining gates

1. Approve sufficient evaluation budget and verify sustained account capacity. The installed
   smoke and accounting reconciliation are complete. The remaining evaluation forecast is
   1,966 requests against 674 unallocated request slots; unused slots in consumed smoke
   envelopes have not been silently reused. Additional budget is awaiting the owner.
2. Freeze final candidate/protocol/allocations and run three native and comparison trials,
   real-provider cache measurements, and actual human answer review.
3. Execute the reference workload on the dedicated Linux x64 4-vCPU/8-GiB local-SSD runner.
   Runner access and exclusive reservation/page-cache eviction authorization are outstanding.
4. Obtain two actual unfamiliar developers' successful integration records.
5. Verify PyPI/npm package ownership and trusted publisher identities, then bind final
   human approval to exact release artifacts. npm currently has no authenticated session.

Publication remains blocked while any required evidence is missing. Prepared scripts,
blank human records, and a zero-request preflight do not close their execution gates.

The [gate record](readiness.json) records each original request, its evidence, and the
remaining external input. Local copies of hosted package and scan reports are retained
under `hosted-evidence/` so verification does not depend on GitHub artifact retention.
