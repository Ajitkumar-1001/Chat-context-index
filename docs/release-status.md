# Release status — September 24, 2026 UTC

Release is **blocked**. Local regression, package installation, and cross-language checks pass
for the current working tree. Hosted CI, reference performance, live quality and cost evidence,
and developer adoption remain open. No package was published.

| Gate | Current evidence |
| --- | --- |
| Python regression | 224 passed locally on macOS arm64 / Python 3.12.11, including disposable Redis and clean-install tests |
| TypeScript regression | 21 native memory tests passed; TypeScript build passed on Node 22.23.1 |
| Static checks | Ruff and mypy passed for the Python package |
| Cross-language retrieval | Shared fixture compares ordered `search()`, `retrieve()`, and prepared context output, including correction and duplicate cases |
| Cache failure accounting | Python `stats()` reports Redis errors and SQLite fallback lookups/hits; real-Redis failure tests exercise these counters. TypeScript has no Redis memo backend |
| Fresh local packages | Wheel, source distribution, and npm tarball rebuilt and installed outside the checkout; all nine writer/reader combinations, TypeScript consumer typecheck, package documentation, and default operation passed. [Current report](../evaluations/results/release-readiness/package-memory-20260924.json) |
| Hosted matrix | **Not run for this working tree.** CI defines 16 Python/Node/OS combinations; the local GitHub CLI token is invalid, so no run IDs or per-cell artifact hashes are available |
| Reference performance | **Not run.** Updated installed packages meet the three warm single-request thresholds in [local macOS runs](benchmark-reproduction.md), but Python's process-cold first-search p95 was 112.6 ms and search latency grows at higher concurrency. The dedicated Linux x64 runner, filesystem-cold pass, and scale point remain outstanding |
| Installed-package live smoke | **Blocked by provider capacity.** The preceding candidate encountered HTTP 429 during indexing; capacity and usage accounting for the current artifacts are unverified |
| Quality, cost, human review | **Not run for these artifacts.** Three frozen trials, real-provider cache measurements, answer review, and two unfamiliar-developer integration trials remain open |

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
python -m pytest -q tests/conformance tests/memory tests/examples tests/evaluations
node --test tests/memory/*.test.mjs
python -m ruff check packages/python/src/cci
python -m mypy --config-file packages/python/pyproject.toml packages/python/src/cci --ignore-missing-imports
npm --prefix packages/typescript run build
python tests/packaging/verify_packages.py --report evaluations/results/package-memory.json
```

The CI matrix uses GitHub's published [`macos-15` arm64 and `ubuntu-24.04` x64 runner
labels](https://github.com/actions/runner-images#available-images). Each job verifies its
architecture. Local macOS results do not substitute for a hosted matrix or the dedicated
reference Linux performance run.
