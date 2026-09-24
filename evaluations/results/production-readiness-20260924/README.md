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
- [Reference-runner tooling](../../../docs/reference-performance-runner.md) verifies
  hardware/reservation and installed archives, measures Python and TypeScript at 10k/100k,
  and includes process-cold and filesystem-cold checks. Seven gate tests and installed
  helper smokes pass; [local evidence](performance-preparation/README.md) is not a reference run.
- [Human validation materials](../../../docs/validation/README.md) include a preregistered
  sample, offline packet exporter, independent adoption tasks, and empty attestation records.
  Eleven invented-data tests pass; no human completion has been substituted by an agent.

## Live smoke and accounting

The [first installed-package smoke](live-smoke-summary.json) stopped at its frozen
20-second timeout, retaining a 1,061-token reservation. A subsequent read-only model-list
request confirmed the endpoint was reachable and the configured model was listed.

A [separately allocated follow-up](live-smoke-extended-summary.json) used a 60-second
timeout within the original cumulative allowance. Two indexing calls succeeded and reported
151 input tokens plus 66 output tokens; a third returned HTTP 503 without usage. The smoke
did not reach original-evidence retrieval. Across both runs there were four generation
attempts, two with unknown usage; the combined charged/reserved amount is 2,345 tokens.
Both allocations remain consumed. No further generation requests or held-out trials were
made. Operational journals and the prior cumulative allowance remain local; unknown usage
is never counted as free, and these partial tokens cannot establish complete cost savings.

The configured model's pricing was checked against
[Google's pricing documentation](https://ai.google.dev/gemini-api/docs/pricing).
Account-specific limits require the owner's provider account information; see
[Google's rate-limit documentation](https://ai.google.dev/gemini-api/docs/rate-limits).

## Remaining gates

1. Establish provider responsiveness/capacity, reconcile the consumed allocation, then
   complete a new bounded installed smoke with actual token usage. The full evaluation
   forecast exceeds the prior request allowance; additional budget is awaiting the owner.
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
