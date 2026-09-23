# Benchmark Reproduction

`benchmarks/reference_fixture_benchmark.py` measures the three performance goals named in
plan.md (SC-007–009) against a deterministic 10,000-message synthetic fixture (seed 7, ~1 KiB
avg / 4 KiB max message size):

| Metric | Threshold (p95) |
|---|---|
| Store open, no model work | ≤ 2 s |
| 100-message batch ingest, excluding indexing | ≤ 500 ms |
| Lexical search, top-8 | ≤ 100 ms |

## Running it

The benchmark measures whatever `cci` build is importable in the interpreter you run it with —
run it against an **installed artifact**, not the source checkout, so the numbers reflect what a
real install experiences:

```bash
# Build and install the wheel into a fresh virtual environment
cd packages/python
python -m build --wheel
python -m venv /tmp/cci-preview-venv
/tmp/cci-preview-venv/bin/pip install dist/chat_context_index-*.whl

# Run the benchmark against that installed wheel
/tmp/cci-preview-venv/bin/python ../../benchmarks/reference_fixture_benchmark.py \
    --out report.json
```

The script prints and optionally saves a JSON report including the interpreter path, platform,
and whether each threshold passed — labeled `Python-preview` (a wheel build, not yet a release
artifact) unless you are running it as part of M5's release-candidate benchmark (T083), which
supersedes the preview numbers.

## Honesty requirements (constitution Principle IV)

- The report always states which platform it actually ran on. Per plan.md, the reference
  numbers are meant to come from a dedicated Linux runner; a report generated elsewhere (for
  example, macOS during local development) must say so explicitly rather than imply the
  Linux-runner result — `report["environment"]["dedicated_linux_runner"]` records this.
  `benchmarks/T070-python-preview-report.json` in this repository is one such run, generated on
  macOS arm64 during M3, not the dedicated Linux runner.
- A threshold miss is reported as `"pass": false` for that metric, not silently omitted or
  re-run until it passes.
