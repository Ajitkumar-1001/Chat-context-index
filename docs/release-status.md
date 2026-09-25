# Release status — September 25, 2026 UTC

Release is **blocked**. The latest retained successful CI candidate is pull-request merge source
`6dd96d7d2c712d152779733a2087a166f50b231e`, tested in
[run 36064740436](https://github.com/Ajitkumar-1001/Chat-context-index/actions/runs/36064740436).
It passed 430 Python tests, 88 Node tests, and all 16 package combinations. Its Python wheel is
`219fd1e7c189e5cd54cdfa9bfaef004cf6a8ac60aef3041177015a8ef7571d79`.
These retained results do not establish that newer main or preparation-branch changes have passed CI.
The newer main run failed a checksum-only secret-scanner false positive; a targeted suppression
is prepared and fresh validation remains pending.

The [candidate verification](../evaluations/results/production-readiness-20260924/execution/follow-up-hosted-verification.json)
binds those results to its source and archive hashes. The successful earlier live smoke applies
only to wheel `a3286616…`; no live smoke is recorded for wheel `219fd1e7…`.
No registry package has been published. The GitHub
[prerelease](https://github.com/Ajitkumar-1001/Chat-context-index/releases/tag/pre-release) has no assets.

| Gate | Evidence and remaining work |
| --- | --- |
| Python regression | **430 passed** for retained source `6dd96d7d`, including Redis, evaluator and release-control checks |
| TypeScript regression | **88 passed** for the same retained source; TypeScript typecheck and package builds passed |
| Static checks | Passed in retained run 36064740436; newer preparation changes require fresh hosted validation |
| Cross-language retrieval | Shared fixture compares ordered `search()`, `retrieve()`, and prepared context output, including correction and duplicate cases |
| Cache failure accounting | Python `stats()` reports Redis errors and SQLite fallback lookups/hits; real-Redis failure tests exercise these counters. TypeScript has no Redis memo backend |
| Installed packages | The retained wheel, source distribution and npm archive passed nine writer/reader combinations, consumer typechecking and package checks. [Report](../evaluations/results/production-readiness-20260924/execution/follow-up-hosted-evidence/package-memory.json) |
| Hosted matrix | **16 cells / 144 writer-reader checks passed** for source `6dd96d7d`: Python 3.11–3.14, Node 22/24, Ubuntu x64 and macOS arm64. [Verification](../evaluations/results/production-readiness-20260924/execution/follow-up-hosted-verification.json) |
| CI artifacts and security | Retained run passed security scans and candidate inspection. Its artifact approval manifest remains **DRAFT_NOT_APPROVED**; publication requires independent approval of exact hashes |
| Linux performance | **Not run for the newer candidate.** Hosted Linux benchmark preparation preserves warm/process-cold/filesystem-cold targets and full scale/concurrency trials. The original fixed 4-CPU/8-GiB reference gate has not been closed. Earlier [macOS measurements](benchmark-reproduction.md) are historical |
| Installed-package live smoke | **Not run for wheel `219fd1e7…`.** Historical wheel `a3286616…` passed: eight calls, 1,058 input / 301 output tokens and $0.0010699 estimated cost. [Historical run](../evaluations/results/production-readiness-20260924/live-smoke-retry-003-summary.json) |
| Quality, cost, human review | Three frozen real-model trials, real-provider cache measurements, independent answer review, and two unfamiliar-developer integration trials remain open |
| Registry setup and final approval | GitHub release protections configured. PyPI pending publisher configured for `Ajitkumar-1001/Chat-context-index`, `release.yml`, environment `release`; first OIDC publication remains untested. npm package lookup returns 404 and ownership/publisher identity remains unverified. Final source/artifact approval is unset |
| Website availability | Vercel production deployment `6651358929` reported success, but its generated URL and the repository homepage redirect to Vercel login. Public availability remains unverified |

The retained package report records its exact source and archive SHA-256 values. It tests deterministic
memory behavior with no model calls, so it establishes installation and cross-runtime
compatibility, not answer quality or token savings. The earlier
[evidence-selection record](../evaluations/evidence-selection/README.md) and
[historical live reports](../evaluations/held_out/reports/README.md) apply to previous artifacts.
Provider attempts with unknown usage remain unknown; they are not counted as free calls.

Python's local cache is SQLite by default. Its optional Redis cache falls back to SQLite
when configured; [`stats()`](quickstart-redis.md) exposes failed Redis operations and
fallback lookups/hits without message content. The TypeScript runtime currently has no
memoization backend. The [local](quickstart-no-redis.md) and [Redis](quickstart-redis.md)
quick starts show the provider wiring.

Run the local checks with Python development and Redis dependencies installed, npm dependencies
installed, and Docker available for the disposable Redis tests:

```bash
python -m pytest -q tests/conformance tests/memory tests/examples tests/evaluations tests/benchmarks tests/release
node --test tests/memory/*.test.mjs
python -m ruff check packages/python/src/cci
python -m mypy --config-file packages/python/pyproject.toml packages/python/src/cci --ignore-missing-imports
npm --prefix packages/typescript run build
python tests/packaging/verify_packages.py --report evaluations/results/package-memory.json
```

The CI matrix uses GitHub's published [`macos-15` arm64 and `ubuntu-24.04` x64 runner
labels](https://github.com/actions/runner-images#available-images). Each job verifies its
architecture. The retained pull-request merge run passed all 21 jobs. It is evidence for source
`6dd96d7d`, not a claim that newer main changes are green. Hosted package compatibility
does not establish the separate Linux performance gate.
