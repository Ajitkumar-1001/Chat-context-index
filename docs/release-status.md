# Release status — September 24, 2026 UTC

Release is **blocked**. Local regression, package installation, cross-language checks, and all
16 hosted package combinations passed for the preceding 0.1.0 candidate. The new release
hardening and npm metadata change have fresh local checks; their hosted validation is pending.
Reference performance, live quality/cost evidence, and developer adoption remain open. PyPI and npm returned 404 for
version 0.1.0 at this check. A GitHub [prerelease](https://github.com/Ajitkumar-1001/Chat-context-index/releases/tag/pre-release)
exists without assets; it did not run the tag-triggered registry release workflow.

The [production-readiness execution record](../evaluations/results/production-readiness-20260924/README.md)
tracks the current work, including protected releases, exact-artifact publication checks,
security scanning, prepared reference/human validation, and a failed bounded capacity attempt.

| Gate | Current evidence |
| --- | --- |
| Python regression | 224 passed locally on macOS arm64 / Python 3.12.11, including disposable Redis and clean-install tests |
| TypeScript regression | 21 native memory tests passed; TypeScript build passed on Node 22.23.1 |
| Static checks | Ruff and mypy passed for the Python package |
| Cross-language retrieval | Shared fixture compares ordered `search()`, `retrieve()`, and prepared context output, including correction and duplicate cases |
| Cache failure accounting | Python `stats()` reports Redis errors and SQLite fallback lookups/hits; real-Redis failure tests exercise these counters. TypeScript has no Redis memo backend |
| Fresh local packages | Wheel, source distribution, and npm tarball rebuilt and installed outside the checkout after npm repository metadata changed; all nine writer/reader combinations, TypeScript consumer typecheck, package documentation, and default operation passed. [Current report](../evaluations/results/production-readiness-20260924/release-controls/package-memory.json) |
| Hosted matrix | **Passed:** all 16 Python 3.11–3.14 / Node 22 and 24 / Ubuntu x64 and macOS arm64 package combinations passed with nine writer/reader checks each. Every cell produced the same wheel, sdist, and npm archive hashes as the local package report. [Per-cell evidence](../evaluations/results/release-readiness/hosted-ci-20260924.json), [PR run](https://github.com/Ajitkumar-1001/Chat-context-index/actions/runs/36034703337), [merged-main run](https://github.com/Ajitkumar-1001/Chat-context-index/actions/runs/36034722343) |
| Reference performance | **Not run.** Updated installed packages meet the three warm single-request thresholds in [local macOS runs](benchmark-reproduction.md), but Python's process-cold first-search p95 was 112.6 ms and search latency grows at higher concurrency. The dedicated Linux x64 runner, filesystem-cold pass, and scale point remain outstanding |
| Installed-package live smoke | **INCOMPLETE:** the first attempt timed out; a separately allocated 60-second follow-up recorded 151 input/66 output tokens from two indexing calls before HTTP 503 on the third. Retrieval was not reached. Both failed calls retain unknown-usage reservations; no further generation requests were made. [Latest attempt](../evaluations/results/production-readiness-20260924/live-smoke-extended-summary.json) |
| Quality, cost, human review | **Not run for these artifacts.** Three frozen trials, real-provider cache measurements, answer review, and two unfamiliar-developer integration trials remain open |
| Release protection | GitHub owner approval and tag-only deployment policy configured on `release`; active ruleset protects `v*` tags. Exact-archive workflow and final source-approval gate implemented; registry ownership/trusted publisher setup and final approval remain open |

The package report records fresh source and archive SHA-256 values. It tests deterministic
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
architecture. PR #2 merged before its CI jobs finished; the PR run and a separate post-merge
main run subsequently passed. Hosted package compatibility does not substitute for the dedicated
reference Linux performance run.
