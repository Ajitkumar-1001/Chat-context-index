# Local Linux release checks — September 23, 2026

**PASS:** all six CI command groups ran in a local Linux arm64 Docker container with Python
3.12.14 and Node 22.23.2. The suite passed **165 Python tests and 8 native TypeScript tests**,
including real Redis failure injection and fresh installed-package checks. There were no skips.

- [Command results and source fingerprints](linux-release-checks.json)
- [Python test results](tests.xml)
- [Fresh installed-package report](package-memory.json)
- [Lint](check-1.txt), [types](check-2.txt), [TypeScript build](check-3.txt),
  [Python suite](check-4.txt), [TypeScript suite](check-5.txt), [package verification](check-6.txt)

The installed-package verifier built new archives, checked their licenses/documentation and
TypeScript declarations, and passed all four Python/TypeScript writer-reader combinations in
separate processes. Runtime source fingerprints match those used for the live evaluation.
Archive hashes differ from the earlier macOS-built candidates; both sets are identified in their
own reports. The new archives are locally available under `dist/linux-release`.

The [Dockerfile](Dockerfile) and [command runner](linux_release_check.py) record the environment
and commands. The build used a temporary source snapshot with no API env file, credentials, or
Git metadata. Docker's socket and host networking were enabled for the existing tests to create,
stop, restart, and remove their own disposable Redis containers. The checks made no model calls.

This is a local reproduction of CI commands, not execution of GitHub's workflow or proof of its
protected publishing configuration. The separate [10,000-message benchmark](../../../benchmarks/T083-linux-container-report.json)
also passed its timing thresholds in a Python 3.11 Linux container. Both containers share the
developer's host; the dedicated Linux performance gate remains open.
