# Linux verification of the evidence-selection fix

**PASS:** seven command groups completed in a local Linux arm64 Docker container using the
updated Python and TypeScript source. This is a local reproduction of CI checks, not a hosted
GitHub Actions run or a dedicated performance runner.

- Python: 190 tests passed, zero failures or skips, including real disposable Redis containers,
  conformance, memory, examples, and evaluation accounting. [JUnit evidence](tests.xml).
- TypeScript: 21 native tests passed, zero failures or skips, including shared evidence-selection cases.
- Ruff, mypy (26 source files), and the TypeScript build passed.
- Fresh wheel/npm installs passed all four cross-runtime writer-reader combinations and the
  strict TypeScript consumer check. [Package report](package-memory.json).
- The unchanged 10,000-message benchmark passed all three thresholds: lexical top-8 p95
  54.56 ms (100 ms limit), 100-message ingest p95 27.83 ms (500 ms), and reopen p95 1.53 ms
  (2,000 ms). [Performance report](performance.json).

Environment: Linux `6.10.14-linuxkit-aarch64`, Python 3.12.14, Node 22.23.2.
The [machine-readable command report](linux-release-checks.json) records each command, exit
code, and source fingerprint. The [runner](linux_release_check.py) writes local logs under `/out`;
logs and generated package binaries are ignored by Git. No model was called in these checks.

The existing image `cci-linux-release-check:20260923` supplied Linux dependencies and native
Node modules. The current source, tests, shared fixtures, and evaluators were mounted read-only;
`PYTHONPATH=/work/packages/python/src` selected current Python source. Native TypeScript was
rebuilt inside the container. Package consumers removed source-import overrides and installed
newly built artifacts outside the checkout.

The container used host networking and `/var/run/docker.sock` for the existing Redis failure
tests. It ran `python evaluations/results/linux-evidence-selection/linux_release_check.py`
from `/work`, with this results directory mounted at `/out`.
