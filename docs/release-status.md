# Release status — September 25, 2026 UTC

Release is **blocked**. Measured candidate source
`e762f7bd0033e3ae8326f647f14c901438c74220` passed all 21 jobs in the merged-main
[CI run 36082860041](https://github.com/Ajitkumar-1001/Chat-context-index/actions/runs/36082860041):
511 Python tests, 88 Node tests, and all 16 package combinations. Its Python wheel is
`219fd1e7c189e5cd54cdfa9bfaef004cf6a8ac60aef3041177015a8ef7571d79`.
These results apply to that source and its recorded artifacts; later changes require their own validation.

The [candidate verification](../evaluations/results/production-readiness-20260924/execution/main-candidate-verification-20260925.json)
binds those results to its source and archive hashes. Its installed Python wheel also passed a
[fresh live smoke](../evaluations/results/production-readiness-20260924/execution/live-smoke-candidate-004-summary.json)
with original evidence recovered after reopening SQLite and all actual token usage recorded.
No registry package has been published. The GitHub
[prerelease](https://github.com/Ajitkumar-1001/Chat-context-index/releases/tag/pre-release) has no assets.

| Gate | Evidence and remaining work |
| --- | --- |
| Python regression | **511 passed** for measured source `e762f7bd`, including Redis, evaluator, benchmark and release-control checks |
| TypeScript regression | **88 passed** for the same retained source; TypeScript typecheck and package builds passed |
| Static checks | Passed in run 36082860041 for source `e762f7bd` |
| Cross-language retrieval | Shared fixture compares ordered `search()`, `retrieve()`, and prepared context output, including correction and duplicate cases |
| Cache failure accounting | Python `stats()` reports Redis errors and SQLite fallback lookups/hits; real-Redis failure tests exercise these counters. TypeScript has no Redis memo backend |
| Installed packages | The retained wheel, source distribution and npm archive passed nine writer/reader combinations, consumer typechecking and package checks. [Report](../evaluations/results/production-readiness-20260924/execution/main-candidate-package-memory-20260925.json) |
| Hosted matrix | **16 cells / 144 writer-reader checks passed** for source `e762f7bd`: Python 3.11–3.14, Node 22/24, Ubuntu x64 and macOS arm64. [Verification](../evaluations/results/production-readiness-20260924/execution/main-candidate-verification-20260925.json) |
| CI artifacts and security | Retained run passed security scans and candidate inspection. Its artifact approval manifest remains **DRAFT_NOT_APPROVED**; publication requires independent approval of exact hashes |
| Linux performance | **10k filesystem-cold target failed** on GitHub-hosted Ubuntu: Python 638.076 ms / TypeScript 703.665 ms p95 versus 100 ms. Warm search passed at 65.321 / 64.822 ms; process-cold search passed at 70.136 / 81.434 ms. **100k scale observations completed:** warm search p95 675.461 / 669.343 ms, filesystem-cold p95 5,101.497 / 6,535.564 ms, and concurrency-16 search p95 11,918.093 / 9,826.439 ms. All recorded concurrency trials had zero errors. [Run 36083318975](https://github.com/Ajitkumar-1001/Chat-context-index/actions/runs/36083318975) completed measurement coverage, not a full performance pass. The original fixed 4-CPU/8-GiB reference gate remains **NOT_QUALIFIED** |
| Installed-package live smoke | **Passed for candidate wheel `219fd1e7…`:** eight calls, 1,066 input / 308 output tokens (1,374 total), zero errors/retries/unknown usage, $0.0010898 estimated cost. Unchanged indexing made zero calls; reopening preserved original sequence 1, its `/content` pointer, and the Oslo evidence. [Fresh run](../evaluations/results/production-readiness-20260924/execution/live-smoke-candidate-004-summary.json) |
| Quality, cost, human review | Three frozen real-model trials, real-provider cache measurements, and independent answer review remain open; sustained provider capacity and the next paid allocation remain unverified. Two frozen [developer-adoption trial bundles](validation/developer-adoption.md) are prepared with exact candidate archives and blank **NOT_RUN** records; two unfamiliar real participants still need to complete them |
| Registry setup and final approval | GitHub release protections configured. PyPI pending publisher configured for `Ajitkumar-1001/Chat-context-index`, `release.yml`, environment `release`; first OIDC publication remains untested. npm sign-in as `ajitkumar1001` was observed, but 2FA is incomplete; package ownership, the initial publication/bootstrap, and trusted publishing remain unresolved. The package lookup returned 404. Final source/artifact approval is unset |
| Website availability | Vercel production deployment `6651358929` reported success, but its generated URL and the repository homepage redirect to Vercel login. Public availability remains unverified |

The [hosted performance summary](../evaluations/results/production-readiness-20260924/execution/hosted-performance-36083318975-summary.json)
binds all four observations to the same candidate archives. Each job completed 3,000 concurrent-search
trials and 64 concurrent batch-ingest trials without errors. The 100k benchmark CLIs returned exit code 1
because warm search exceeded 100 ms; their successful workflow jobs establish complete scale observations,
not passing latency. All hosts exposed 4 CPUs and approximately 15.61 GiB RAM, with CPU models varying by job.

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
architecture. The retained merged-main run passed all 21 jobs for source
`e762f7bd`. Hosted package compatibility
does not establish the separate Linux performance gate.
