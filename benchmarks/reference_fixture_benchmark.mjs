/** TypeScript counterpart to reference_fixture_benchmark.py, using its exported fixture. */

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { performance } from "node:perf_hooks";

const script = fileURLToPath(import.meta.url);
const argv = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, i, all) => {
  if (value.startsWith("--")) pairs.push([value.slice(2), all[i + 1]]);
  return pairs;
}, []));
const packagePath = resolve(argv.package ?? join(dirname(script), "../packages/typescript/dist/index.js"));

function percentile(values, pct) {
  const ordered = [...values].sort((a, b) => a - b);
  return ordered[Math.min(ordered.length - 1, Math.round((pct / 100) * (ordered.length - 1)))];
}

function summary(values, extra = {}) {
  return { p50: percentile(values, 50), p95: percentile(values, 95), trials: values.length, ...extra };
}

async function coldChild() {
  const t0 = performance.now();
  const { HistoryStore, search } = await import(pathToFileURL(packagePath).href);
  const t1 = performance.now();
  const store = await HistoryStore.open(argv.db, { cacheBackend: "none" });
  const t2 = performance.now();
  await search(store, argv.query, 8);
  const t3 = performance.now();
  const count = await store.connection.get("SELECT COUNT(*) AS count FROM messages");
  await store.close();
  process.stdout.write(JSON.stringify({ import_ms: t1 - t0, open_ms: t2 - t1,
    first_search_ms: t3 - t2, max_rss_kib: process.resourceUsage().maxRSS,
    observed_messages: count.count }) + "\n");
}

async function timedCalls(calls, concurrency) {
  const values = [];
  let errors = 0;
  const started = performance.now();
  for (let i = 0; i < calls.length; i += concurrency) {
    await Promise.all(calls.slice(i, i + concurrency).map(async call => {
      const t = performance.now();
      try { await call(); } catch { errors += 1; }
      values.push(performance.now() - t);
    }));
  }
  return summary(values, { errors, throughput_per_s: calls.length * 1000 / (performance.now() - started) });
}

function dropPageCache() {
  if (process.platform !== "linux") throw new Error("filesystem-cold trials require a reserved Linux host");
  const synced = spawnSync("sync", [], { encoding: "utf8" });
  if (synced.status !== 0) throw new Error(`sync failed: ${synced.stderr}`);
  const root = process.getuid() === 0;
  const dropped = spawnSync(root ? "tee" : "sudo",
    root ? ["/proc/sys/vm/drop_caches"] : ["-n", "tee", "/proc/sys/vm/drop_caches"],
    { input: "3\n", encoding: "utf8" });
  if (dropped.status !== 0) throw new Error(`page-cache drop failed: ${dropped.stderr}`);
}

async function main() {
  if (argv["cold-child"]) return coldChild();
  if (!argv.fixture) throw new Error("pass --fixture from generate_reference_fixture.py");
  const raw = readFileSync(argv.fixture);
  const fixture = JSON.parse(raw);
  const { HistoryStore, ingest, search } = await import(pathToFileURL(packagePath).href);
  const directory = mkdtempSync(join(tmpdir(), "cci-ts-benchmark-"));
  const db = join(directory, "benchmark.db");
  try {
    const store = await HistoryStore.open(db, { cacheBackend: "none" });
    const batchSize = fixture.batch_size;
    const ingests = [];
    const generationStart = performance.now();
    for (let i = 0; i < fixture.messages.length; i += batchSize) {
      const t = performance.now();
      await ingest(store, store.historyId, fixture.messages.slice(i, i + batchSize),
        "bench-src", `batch-${i / batchSize}`);
      ingests.push(performance.now() - t);
    }
    const generationS = (performance.now() - generationStart) / 1000;
    const storeBytes = [db, `${db}-wal`, `${db}-shm`].filter(existsSync)
      .reduce((total, filename) => total + statSync(filename).size, 0);
    const levels = (argv.concurrency ?? "1,4,16").split(",").map(Number);
    const searches = {};
    for (const level of levels) {
      searches[level] = await timedCalls(fixture.queries.map(query => () => search(store, query, 8)), level);
    }
    await store.close();

    const opens = [];
    for (let i = 0; i < 20; i++) {
      const t = performance.now();
      const reopened = await HistoryStore.open(db, { cacheBackend: "none" });
      opens.push(performance.now() - t);
      await reopened.close();
    }
    const cold = [];
    for (let i = 0; i < Number(argv["cold-trials"] ?? 20); i++) {
      const child = spawnSync(process.execPath, [script, "--cold-child", "1", "--package", packagePath,
        "--db", db, "--query", fixture.queries[i % fixture.queries.length]], { encoding: "utf8" });
      if (child.status !== 0) throw new Error(`cold child failed: ${child.stderr}`);
      cold.push(JSON.parse(child.stdout));
    }
    const filesystemCold = [];
    for (let i = 0; i < Number(argv["filesystem-cold-trials"] ?? 0); i++) {
      dropPageCache();
      const child = spawnSync(process.execPath, [script, "--cold-child", "1", "--package", packagePath,
        "--db", db, "--query", fixture.queries[i % fixture.queries.length]], { encoding: "utf8" });
      if (child.status !== 0) throw new Error(`filesystem-cold child failed: ${child.stderr}`);
      filesystemCold.push(JSON.parse(child.stdout));
    }
    // Writer load runs last so all search/open trials use the declared history size.
    const concurrentIngest = {};
    const writer = await HistoryStore.open(db, { cacheBackend: "none" });
    for (const level of levels.filter(level => level > 1)) {
      const batches = fixture.concurrent_batches[String(level)];
      if (!batches) throw new Error(`fixture has no concurrent batches for ${level}`);
      concurrentIngest[level] = await timedCalls(batches.map((batch, i) => () =>
        ingest(writer, writer.historyId, batch, "bench-src", `concurrent-${level}-${i}`)), level);
      concurrentIngest[level].throughput_messages_per_s =
        concurrentIngest[level].throughput_per_s * batchSize;
      delete concurrentIngest[level].throughput_per_s;
    }
    await writer.close();
    const result = {
      fixture: { total_messages: fixture.messages.length, seed: fixture.seed, batch_size: batchSize,
        queries: fixture.queries.length, sha256: createHash("sha256").update(raw).digest("hex") },
      environment: { label: argv.label ?? "typescript-local", platform: process.platform,
        machine: process.arch, node: process.version, package_path: packagePath,
        artifact: argv.artifact ?? null, dedicated_linux_runner: false,
        filesystem_cold: filesystemCold.length ? "sync + drop_caches=3 before each child" : "NOT RUN" },
      fixture_stats: { message_bytes_avg: fixture.messages.reduce((n, m) =>
        n + Buffer.byteLength(m.content), 0) / fixture.messages.length,
        message_bytes_max: Math.max(...fixture.messages.map(m => Buffer.byteLength(m.content))),
        generation_s: generationS, store_bytes_after_generation: storeBytes },
      results: { store_open_ms: { ...summary(opens), threshold_p95_ms: 2000 },
        batch_ingest_100_ms: { ...summary(ingests), threshold_p95_ms: 500 },
        lexical_search_top8_ms: { ...searches[1], threshold_p95_ms: 100 } },
      concurrency: { search_top8_ms: searches, ingest_100_ms: concurrentIngest },
      resources: { max_rss_kib: process.resourceUsage().maxRSS },
      process_cold: cold.length ? { import_ms: summary(cold.map(c => c.import_ms)),
        store_open_ms: summary(cold.map(c => c.open_ms)),
        first_search_top8_ms: summary(cold.map(c => c.first_search_ms)),
        max_rss_kib: Math.max(...cold.map(c => c.max_rss_kib)),
        total_messages: fixture.messages.length, raw_trials: cold } : null,
      filesystem_cold: filesystemCold.length ? {
        import_ms: summary(filesystemCold.map(c => c.import_ms)),
        store_open_ms: summary(filesystemCold.map(c => c.open_ms)),
        first_search_top8_ms: summary(filesystemCold.map(c => c.first_search_ms)),
        total_messages: fixture.messages.length, raw_trials: filesystemCold } : null,
    };
    for (const metric of Object.values(result.results)) metric.pass = metric.p95 <= metric.threshold_p95_ms;
    const text = JSON.stringify(result, null, 2) + "\n";
    process.stdout.write(text);
    if (argv.out) writeFileSync(argv.out, text);
    if (Object.values(result.results).some(metric => !metric.pass)) process.exitCode = 1;
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

await main();
