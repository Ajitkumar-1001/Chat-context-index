# Remaining release execution plan

Prepared September 24, 2026. Target: release the Python and TypeScript packages with
matching, reviewable evidence. This plan authorizes no additional spending or publication.

The [current release record](release-status.md) already establishes 16 hosted package
combinations, 266 Python tests, 21 Node tests, security checks, and configured release
protections. The [installed Python smoke](../evaluations/results/production-readiness-20260924/live-smoke-retry-003-summary.json)
passed: exact original evidence after reopen, eight calls, 1,058 input / 301 output tokens,
and no unknown usage. Reuse that smoke only for an identical wheel hash. Do not repeat
completed checks unless changed inputs or a demonstrated concern require it.

## 1. Supply the prerequisites

Owner: project/account owner. Automation: agent prepares and verifies the records.

| Input | Required before | Acceptance |
| --- | --- | --- |
| Evaluation allowance | More model calls | Explicit aggregate request, reserved-token, and estimated-dollar ceilings; new disjoint subrun allocations; previous usage and unknown reservations carried forward |
| Provider capacity | Native/comparison/cache execution | Account quota and applicable request/token limits support the frozen schedule; record pacing and stop conditions |
| Reference runner | Performance execution | Exclusive Linux x64 host, 4 vCPU, 8 GiB, local SSD, access/reservation evidence, and authorization for host page-cache eviction |
| Human reviewer | Answer review | An actual independent reviewer; second reviewer available if disagreement or uncertainty requires adjudication |
| Two unfamiliar developers | Adoption trials | One Python participant and one TypeScript participant; actual independent work and attestations |
| Registry owners | Publishing setup | Authenticated owners for the intended npm/PyPI package identities; no credentials in reports |
| Website destination | Deployment | Confirm the intended hosting project and access; no hosting destination is assumed |

The remaining evaluation scenario is **1,966 requests**: 933 native, 933 comparison,
and up to 100 cache-experiment calls. Only **674 request slots**, 3,122,324 reserved tokens,
and $2.0649456 remain unallocated under the existing book. This leaves a scenario shortfall
of 1,292 requests; the scenario is not a guaranteed upper bound or token/cost forecast.
Approve a revised allowance with headroom before freezing subruns. Reaching any limit
stops execution; it does not permit reducing trials or silently recycling spent allocations.

**Done when:** named people/accounts/runner are recorded and each paid subrun has an
approved allowance. Missing inputs block their dependent work, not independent preparation.

## 2. Bind publication to approved archives, then freeze the candidate

Owner: agent implements/verifies; project owner controls approval values.

1. Reconcile current local commits and pending website edits; preserve concurrent work.
   Complete required publisher/workflow/package-metadata changes before selecting the candidate.
2. Fix the remaining approval gap in `.github/workflows/release.yml` and
   `scripts/release_controls.py`. Tag CI builds archives and the current guard validates
   them against that run's own report. `RELEASE_APPROVED_SHA` alone does not prove they
   equal the archives used in evaluation, performance testing, and adoption.
3. Require publication archives to match an independently approved filename/SHA-256
   manifest bound to the approved source. Missing approval or any changed archive must
   block publication, even when the current CI report and source SHA are internally valid.
   Keep the existing same-run checks and avoid rebuilding in publication jobs.
4. Add focused regressions for matching approval, missing approval, changed archive bytes,
   wrong source, and a substituted approval manifest. Run relevant release checks and CI.
5. Retain one candidate manifest: source commit, CI run, package versions, wheel/sdist/npm
   hashes, runtime/dependency identities, fixture/evaluator hashes, provider/model/pricing,
   allocations, and the preregistered human protocol/exporter hashes.

**Done when:** the guard rejects an unapproved artifact despite otherwise passing CI,
the intended candidate passes required checks, and every subsequent run has explicit pins.

## 3. Execute dedicated Linux performance

Owner: runner operator supplies access/reservation; agent runs automation; human verifies
the reservation/isolation evidence. Prefer this before expensive model trials because
a runtime performance fix can invalidate later quality results.

Use [the existing runner procedure](reference-performance-runner.md) and
`benchmarks/run_reference_gate.py`: zero-effect preflight first, then execution on the
authorized exclusive host. Install the exact frozen Python wheel and npm archive.

- Both languages: 10,000 and 100,000 messages; concurrency 1, 4, and 16.
- Complete warm, reopen, fresh-process, and filesystem-cold measurements with the
  existing workloads and denominators; retain errors and all raw reports.
- At the 10k reference point: search p95 ≤100 ms, ingestion p95 ≤500 ms, and reopen
  p95 ≤2,000 ms, including the prescribed cold search/open checks.
- Concurrency and 100k scale results must complete without errors. Report their actual
  latency/resource use; do not apply the single-request threshold to them automatically.

**Done when:** automated thresholds pass and the actual reservation/hardware evidence is
reviewed. `PASS_PENDING_RUNNER_EVIDENCE_REVIEW` alone does not close this gate.

## 4. Run the three-trial model evaluations and cache measurements

Owner: agent, using the authorized provider allocation.

1. Before dispatch, freeze all inputs and run zero-call preflights. Update canonical
   capacity/accounting records only from actual evidence; retain earlier snapshots.
2. Use `evaluations/held_out/release_validation.py` for native lexical/auto `ask()`:
   all 40 held-out queries, three trials, isolated histories and memo namespaces.
3. Use the existing frozen comparison runner for full history, recent/lexical memory,
   and tree memory: all 40 queries and all three trials, matched model/prompt/budgets.
   Allocate its allowance separately; never let two runners each spend the whole balance.
4. Run the separate development cache experiment with none/SQLite/Redis, covering
   cold/warm reuse, changed requests, appended history, and Redis failure/fallback.
   Cache savings must not contaminate the isolated held-out quality trials.
5. Retain indexing, updates, navigation, answers, repair/review, and failed-call usage.
   Unknown usage retains reservations and prevents a complete cost claim. Preserve
   missing/failed cases in denominators; stop on quota/auth failures or a budget limit.

**Done when:** all expected trials, cases, judging, and cache scenarios are complete,
accounted for, and tied to the frozen inputs. This establishes execution completeness;
product quality still has to pass the checks below and human review.

## 5. Complete human review and assess quality/cost claims

Owner: independent human reviewer; agent exports packets and recomputes metrics.

Follow [the frozen review protocol](validation/human-review.md). Review every nonpassing
case plus the preregistered passing sample across both evaluation surfaces and all trials.
Keep initial human decisions, model judgments, citations, and adjudication separate.
Unresolved disagreements require a second human and keep the gate open.

| Quality criterion, in each required native-auto/tree trial | Pass condition |
| --- | --- |
| Evidence recall across 32 answerable queries | ≥0.85 |
| Reviewed answer correctness | ≥0.80 |
| Correct abstention | ≥7 of 8 absent-answer queries |
| Citation structural validity | 1.00 |
| Reviewed citation support | ≥0.90 |
| Native auto versus matched-budget lexical recall | Auto is not lower |

A lower-cost claim additionally needs complete usage, passing quality, lower application
cost than full history in all three comparison trials, and at most five percentage points
less recall/correctness. If this fails, publish **cost savings not established**; do not
change the thresholds. Report lexical/full-history baselines separately.

**Done when:** actual attestations and every required decision exist, all disagreements
are resolved, and recomputed product thresholds pass. Review completion alone is not a
quality pass. A failure requires a separate development reproduction; do not tune against
held-out answers. If the holdout becomes a tuning target, use a fresh frozen confirmation
protocol rather than presenting another run on the same exposed data as independent evidence.

## 6. Finalize documentation and run independent adoption

Owner: agent prepares documentation; two unfamiliar developers perform the trials;
human coordinator signs acceptance.

Update result/limitation wording from reviewed evidence, then freeze the supplied
documentation snapshot. Follow [the adoption tasks](validation/developer-adoption.md)
using exact candidate archives outside the checkout. Participants must independently
install, ingest, explicitly index, retrieve the correction as original evidence, and
reopen the database in a separate process with identical IDs, pointers, and text.

**Done when:** both languages have actual successful participant programs, transcripts,
artifact/document hashes, and attestations. Preserve failed attempts and interventions.
A documentation fix requires a new snapshot and an affected-path repeat with an unfamiliar
participant. Agent-generated solutions do not count as independent adoption.

## 7. Complete registry setup and final evidence approval

Registry setup can proceed alongside runner preparation and evaluation: verify ownership
and configure both trusted publisher identities for the intended repository, workflow,
and environment. Verify supported publishing-tool versions and registry settings at
execution time; validate the npm OIDC path before removing its current token fallback.
For a new package, use the registry's supported bootstrap flow; any bootstrap publication
is a publication action and must wait for its concrete approved artifacts.

Assemble one final evidence manifest containing candidate hashes, CI/security/license
results, complete evaluation/accounting, human judgments, Linux runner/results, adoption
records, and registry/protection identities. Required findings and failed gates stay open.

**Done when:** every release gate has attributable passing evidence for the intended
candidate, claims match those results, and the owner approves the exact source and archive
manifest. Only then set the approval controls used by the release workflow.

## 8. Publish packages and deploy the result pages

Owner: agent executes authorized delivery; owner performs protected environment approval.

- Reconcile and commit/push pending source, public evidence, README, and website changes.
  Private keys, provider journals, personal attestations, and account records stay private.
- Create the approved version tag. The workflow runs its CI/security gates, compares
  retained publication archives against the approved manifest, and pauses for required
  environment approval. Publish those archives to PyPI/npm without a publication-job rebuild.
- Install both published packages in clean environments outside the checkout; verify
  version, archive identity, provenance where available, and provider-free persistence/retrieval.
  If one registry succeeds and the other fails, retain that partial status and resume only
  the missing publication; never overwrite a published version or move its tag silently.
- Build/check the website, deploy to the confirmed hosting destination, and verify the
  live result text, evidence links, package versions, and installation instructions.
  The current truthful status page can be deployed earlier; package-install claims change
  only after registry verification.

**Done when:** both registry installs work, published bytes match approval, the website
is live with correct claims, and the final report links the packages and evidence.

## Change handling and parallel work

Freeze package inputs before the corresponding checks. A runtime, dependency, prompt, or
protocol change invalidates affected evidence. A packaged-document change changes archive
identity: rebuild and verify installation/contents, then explicitly review whether unchanged
runtime evidence can carry forward. Supplied instructional changes invalidate affected
adoption results. Never silently attach old results to a new candidate.

Account/registry setup, participant scheduling, runner reservation, and truthful website
delivery can progress in parallel. Prefer performance before paid quality evaluation;
human review follows generation/judging, and adoption follows finalized reviewed docs.
Final approval and package publication depend on all required gates. No release date is
committed until provider capacity, runner access, and human availability are established.
