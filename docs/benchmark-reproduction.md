# Benchmark reproduction

The Python and TypeScript benchmarks measure a 10,000-message synthetic history generated
with seed 7. Messages average about 893 bytes. Both use the same messages and 1,000 two-term
queries when TypeScript reads the exported fixture. The measured single-request p95 goals are:

| Operation | p95 goal |
| --- | ---: |
| Reopen the store without model work | 2,000 ms |
| Ingest a 100-message batch without indexing | 500 ms |
| Lexical search for top eight results | 100 ms |

Run against installed wheel and npm artifacts. The following commands assume the wheel is
installed in `/tmp/cci-python-bench`, the npm tarball in `/tmp/cci-node-bench`, and the
working directory is the repository root:

```bash
/tmp/cci-python-bench/bin/python -I benchmarks/reference_fixture_benchmark.py \
  --queries 1000 --concurrency 1,4,16 --cold-trials 20 --out python-report.json
python benchmarks/generate_reference_fixture.py \
  --queries 1000 --out /tmp/cci-reference-fixture.json
node benchmarks/reference_fixture_benchmark.mjs \
  --fixture /tmp/cci-reference-fixture.json \
  --package /tmp/cci-node-bench/node_modules/chat-context-index/dist/index.js \
  --concurrency 1,4,16 --cold-trials 20 --out typescript-report.json
```

Both report ingestion, search, and reopen times; per-request latency and throughput at
concurrency 1/4/16; store size; memory; and 20 fresh-process import/open/search trials.
The TypeScript process holds the exported JSON fixture in memory, so its peak RSS includes
the fixture and is not directly comparable with Python's generated-on-the-fly RSS.
The process-cold trial starts a new interpreter but **does not drop the OS page cache**.
The [dedicated runner guide](reference-performance-runner.md) provides installed-artifact
automation and explicit filesystem-cold trials, with hardware and reservation verification.
Record that filesystem-cold run on the dedicated Linux x64 reference runner, along
with the maximum tested history size, disk growth, peak resident memory, and sustainable
concurrency. A report from macOS or a shared container is development evidence only.

The current [local Python report](../evaluations/results/release-readiness/performance-macos-20260924.json)
and [local TypeScript report](../evaluations/results/release-readiness/performance-typescript-macos-20260924.json)
use installed 0.1.0 candidates on macOS arm64. They meet all three single-request p95 goals;
per-request search latency grows substantially at higher concurrency. Python's 20-trial
process-cold first-search p95 was 112.6 ms, above the 100 ms search target; TypeScript's was
99.0 ms. These are local observations. The dedicated Linux reference gate remains open.

## Retrieval and context timing

`search()` alone does not exercise correction linking or context packing. `--paths` also times
`retrieve(mode="lexical")` and `prepare_context(mode="lexical")` over the same 1,000 queries,
warm at each `--concurrency` level and as a first call in each fresh process:

```bash
/tmp/cci-python-bench/bin/python -I benchmarks/reference_fixture_benchmark.py \
  --queries 1000 --cold-trials 20 --paths search,retrieve,prepare --out python-paths-report.json
```

The report adds `paths.retrieve_lexical_ms` and `paths.prepare_context_lexical_ms`. Compare a
change against the previous installed wheel on the same machine, run back to back; absolute
numbers from different hosts are not comparable.

## Development recall bar

`benchmarks/dev_recall.py` scores `prepare_context` on the development split, or on the
hand-written [paraphrase fixture](../evaluations/paraphrase/fixture.json), without model calls. It
never reads the held-out split. A word-for-word copy of the annotated message counts as retrieved
evidence. `--mode tree` uses a fake navigator that keeps every offered node, so it measures loss
after perfect navigation, not navigation quality.

```bash
/tmp/cci-python-bench/bin/python -I benchmarks/dev_recall.py --mode lexical --out dev-lexical.json
/tmp/cci-python-bench/bin/python -I benchmarks/dev_recall.py --mode tree --out dev-tree.json
/tmp/cci-python-bench/bin/python -I benchmarks/dev_recall.py --mode lexical \
  --fixture evaluations/paraphrase/fixture.json --out paraphrase-lexical.json
```

Each report ends with `bar` and `bar_pass`, and the command exits 1 when the bar fails. On the
development split the bar requires context recall and correction-case recall of at least 0.85 and
no context that shows a superseded value without its correction. On the paraphrase fixture it
requires recall of at least 0.80 for corrections that name the old value; corrections that do not
are reported only. Add `--baseline <report.json>` from the same mode and fixture to require no
more than a 2-point drop on the development split and no drop on the paraphrase fixture. Current
installed-wheel results are in [`evaluations/results/phase15-convergence/`](../evaluations/results/phase15-convergence).
