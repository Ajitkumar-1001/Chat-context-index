# Release status — September 24, 2026 UTC

Release is **blocked**. The updated release controls, security workflow, regression suite,
and all 16 hosted package combinations passed on main commit
`b3bb918be1edb21bddb735ad92b5a52d59deafb7`. The exact retained CI archives also passed the
publication guard locally, without publishing. Reference performance, live quality/cost
evidence, human review, developer adoption, and registry setup remain open. PyPI and npm returned 404 for
version 0.1.0 at this check. A GitHub [prerelease](https://github.com/Ajitkumar-1001/Chat-context-index/releases/tag/pre-release)
exists without assets; it did not run the tag-triggered registry release workflow.

The [production-readiness execution record](../evaluations/results/production-readiness-20260924/README.md)
tracks the current work, including protected releases, exact-artifact publication checks,
security scanning, prepared reference/human validation, and two incomplete bounded capacity attempts.

| Gate | Current evidence |
| --- | --- |
| Python regression | **266 passed** in the current hosted conformance job, including Redis, evaluator, performance-gate, and release-control checks |
| TypeScript regression | **21 passed** in the current hosted native memory suite; TypeScript typecheck and package builds passed |
| Static checks | Hosted Python lint/typecheck, TypeScript typecheck, and bounded evaluator static checks passed |
| Cross-language retrieval | Shared fixture compares ordered `search()`, `retrieve()`, and prepared context output, including correction and duplicate cases |
| Cache failure accounting | Python `stats()` reports Redis errors and SQLite fallback lookups/hits; real-Redis failure tests exercise these counters. TypeScript has no Redis memo backend |
| Fresh local packages | Wheel, source distribution, and npm tarball rebuilt and installed outside the checkout after npm repository metadata changed; all nine writer/reader combinations, TypeScript consumer typecheck, package documentation, and default operation passed. [Current report](../evaluations/results/production-readiness-20260924/release-controls/package-memory.json) |
| Hosted matrix | **Passed:** all 16 Python 3.11–3.14 / Node 22 and 24 / Ubuntu x64 and macOS arm64 package combinations passed with nine writer/reader checks each. [Current verification](../evaluations/results/production-readiness-20260924/hosted-verification.json), [merged-main run](https://github.com/Ajitkumar-1001/Chat-context-index/actions/runs/36043232899) |
| CI artifacts and security | The retained wheel, sdist, npm tarball, and rebuilt wheel passed the source/run/version/checksum guard; publication archives were copied without rebuilding. Hosted secret and dependency scans passed. [Archive manifest](../evaluations/results/production-readiness-20260924/hosted-artifact-manifest.json), [scan evidence](../evaluations/results/production-readiness-20260924/hosted-verification.json) |
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
architecture. The current post-merge main run includes the release-hardening changes and
passed all 21 jobs. Hosted package compatibility does not substitute for the dedicated
reference Linux performance run.
