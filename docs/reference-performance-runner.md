# Dedicated Linux performance run

This closes T091/R10 only after an actual reserved Linux x64 run and review of the runner
evidence. Local macOS results and Docker on a shared host remain development evidence.
The automation is ready; a qualifying runner reservation has not been supplied.

Provide an exclusively reserved Linux x64 host with 4 vCPUs, 8 GiB RAM, and local SSD storage.
It needs Python 3.11–3.14 with venv, Node 22 or 24, npm, git, util-linux (`findmnt`, `lsblk`,
`lscpu`), systemd's `systemd-detect-virt`, and package download access. Keep the complete
checkout at the candidate commit and copy the exact CI-verified wheel and npm tarball onto it.
No package is built or published by this runner; no model credentials or calls are required.

An administrator supplies a reservation attestation, for example:

```json
{
  "runner_id": "replace-with-inventory-identity",
  "operator": "replace-with-responsible-operator",
  "reservation_id": "replace-with-reservation-record",
  "reservation_start_utc": "2026-09-24T12:00:00+00:00",
  "reservation_end_utc": "2026-09-24T18:00:00+00:00",
  "exclusive": true,
  "local_ssd": true,
  "isolation_evidence": "replace-with-reservation-record-or-host-allocation-evidence"
}
```

Use actual dates and attributable evidence. A label or this file alone is not proof of
isolation. The script records observed CPU count/affinity, physical memory, container detection,
block-device rotation, mount, CPU details, load averages, boot identity hash, and reservation
before and after execution. The final reviewer verifies the inventory/reservation evidence;
the strongest automated success status is `PASS_PENDING_RUNNER_EVIDENCE_REVIEW`.

First run preflight, which neither installs packages nor drops caches:

```bash
python benchmarks/run_reference_gate.py \
  --wheel /srv/candidates/chat_context_index-0.1.0-py3-none-any.whl \
  --npm-tarball /srv/candidates/chat-context-index-0.1.0.tgz \
  --runner-attestation /srv/runner-reservation.json \
  --work-dir /srv/local-ssd \
  --out-dir /srv/evidence/performance-preflight \
  --preflight
```

On that exclusive host, the operator must permit host-wide page-cache eviction: root execution
or passwordless `sudo -n tee /proc/sys/vm/drop_caches` is required. This affects every workload
on the host; it is the reason for the exclusive reservation. Run the same command with a fresh
`--out-dir` and replace `--preflight` with `--allow-drop-page-cache`. The script never changes sudo
configuration, provisions a machine, or accepts a container as the reference runner.

The runner installs the wheel and npm archive into temporary environments outside the checkout
on the selected SSD, checks installed package bytes against the archives, and records dependency
versions, artifact/benchmark/fixture SHA-256 hashes and source commit/working-tree state. It then
runs both languages at 10,000 and 100,000 messages, seed 7, with 1,000 searches per concurrency
level (1, 4, 16), 100-message ingestion, 20 reopen trials, 20 fresh-process trials, and 20 trials
with `sync` plus Linux `drop_caches=3` before a fresh process. All search/open trials precede
concurrent writers, so they measure exactly the declared history size. Older local reports
ran cold/open measurements after the additional concurrent writes; retain them as historical
development observations and use the corrected protocol for the new gate.

Reference p95 limits are search 100 ms, ingestion 500 ms, and reopen 2,000 ms. The runner also
requires the reference process-cold and filesystem-cold search/open limits to pass. Concurrent
request latency and the 100,000-message scale point are reported separately; they must complete
without errors, but the 10,000-message single-request thresholds are not transferred to them.
Report measured sustainable concurrency from these results rather than promising 100 ms at
16 simultaneous requests. Scale reports include peak RSS and generated database size; the
TypeScript RSS includes its in-memory fixture, as described in benchmark-reproduction.md.

Retain the entire output directory: manifest, four raw JSON reports, and execution/install logs.
An incomplete run, expired reservation, changed input, failed threshold, or missing cold pass
blocks the gate. Compare artifact hashes with the final release manifest before approval.
The `dedicated_linux_runner: false` field in standalone benchmark reports is deliberately
unchanged: only the wrapper's observed metadata and reviewed reservation establish that claim.
