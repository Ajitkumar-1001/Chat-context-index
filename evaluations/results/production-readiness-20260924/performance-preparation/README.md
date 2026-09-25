# Performance gate preparation, 2026-09-24

**T091/R10 remains BLOCKED: no dedicated Linux x64 reference runner is available.**
The current host is Darwin arm64. `local-preflight/manifest.json` records the observed host
and rejects it before installation, benchmarking, or cache eviction. There were zero model
calls, no provisioning, and no filesystem page-cache drops.

Completed preparation:

- `benchmarks/run_reference_gate.py` installs and verifies wheel/npm package bytes, records
  hashes, dependencies, observed hardware and operator reservation, and requires complete
  10,000/100,000-message Python and TypeScript measurements, including filesystem-cold runs.
- Both language benchmarks now run reopen/cold trials before concurrent writers. Fresh child
  processes also count stored rows after measurement, so a mislabeled history size is rejected.
- The runner retains failures and all logs/reports. A passing automated run still requires review
  of attributable runner-reservation evidence and matching final release artifacts.

Executed local checks:

| Check | Result |
| --- | --- |
| `pytest tests/benchmarks/test_reference_gate.py -q` | 7 passed |
| Ruff on new runner, Python benchmark and focused tests | Passed |
| `node --check benchmarks/reference_fixture_benchmark.mjs` | Passed |
| Installed Python helper smoke | Passed |
| Installed TypeScript helper smoke | Passed |
| Reference host preflight on current Mac | Correctly BLOCKED, exit 2 |

The two helper smoke JSON reports are **development validation of the scripts**, not release
performance evidence: 1,000 messages, 20 queries, concurrency 1/4/16, two process-cold trials,
and no filesystem-cold measurements. Both cold children independently observed exactly 1,000
stored messages. These small runs used existing local installations, not a newly frozen final
candidate, and do not close any reference latency gate. The Linux hardware and privileged
cache-drop execution paths cannot be validated by these Mac checks.

Next action: supply the exclusive 4-vCPU/8-GiB/Linux-x64/local-SSD host and reservation evidence,
then run `benchmarks/run_reference_gate.py` with the final
CI-verified archives. No CI workflow was added because no qualifying self-hosted runner identity
or inventory has been supplied.
