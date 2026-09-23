/**
 * Configuration (contracts/operations.md `open()`; PRD §7.4). Same defaults and precedence as
 * config.py: explicit constructor value -> selected config file -> named environment binding ->
 * documented default. No scanning of arbitrary working directories for secrets.
 */

import { ConfigurationError } from "./errors.js";

export interface Config {
  cacheBackend: "none" | "sqlite" | "redis";
  redisFallback: "sqlite" | "none";
  applicationNamespace: string | null;
  redisUrl: string | null;
  memoLifetimeS: number;
  cacheOperationTimeoutMs: number;
  cacheOverheadBudgetMs: number;
  redisCircuitBreakerFailures: number;
  redisCircuitBreakerProbeS: number;
  sqliteBusyTimeoutMs: number;
  treeMaxChildren: number;
  targetChunkSizeScalars: number;
  maxSelectedChunks: number;
  maxEvidenceTextScalars: number;
  maxTreeNavigationCalls: number;
  perProviderConcurrency: number;
  maxProviderAttemptsPerOp: number;
  providerAttemptLimitRetrieve: number;
  providerAttemptLimitAsk: number;
  providerAttemptLimitIndex: number;
  requestDeadlineRetrieveS: number;
  requestDeadlineAskS: number;
  requestDeadlineIndexS: number;
  clearHistoryQuiescenceDeadlineS: number;
  providerCallDeadlineS: number;
  payloadLogging: boolean;
}

export const DEFAULTS: Config = {
  cacheBackend: "sqlite",
  redisFallback: "sqlite",
  applicationNamespace: null,
  redisUrl: null,
  memoLifetimeS: 86_400,
  cacheOperationTimeoutMs: 100,
  cacheOverheadBudgetMs: 250,
  redisCircuitBreakerFailures: 3,
  redisCircuitBreakerProbeS: 30,
  sqliteBusyTimeoutMs: 5_000,
  treeMaxChildren: 10,
  targetChunkSizeScalars: 6_000,
  maxSelectedChunks: 8,
  maxEvidenceTextScalars: 24_000,
  maxTreeNavigationCalls: 4,
  perProviderConcurrency: 4,
  maxProviderAttemptsPerOp: 3,
  providerAttemptLimitRetrieve: 8,
  providerAttemptLimitAsk: 10,
  providerAttemptLimitIndex: 64,
  requestDeadlineRetrieveS: 60,
  requestDeadlineAskS: 90,
  requestDeadlineIndexS: 300,
  clearHistoryQuiescenceDeadlineS: 10,
  providerCallDeadlineS: 30,
  payloadLogging: false,
};

const ENV_PREFIX = "CCI_";

function envOverrides(): Partial<Config> {
  const overrides: Partial<Config> = {};
  for (const key of Object.keys(DEFAULTS) as (keyof Config)[]) {
    const envName = ENV_PREFIX + key.replace(/[A-Z]/g, (c) => "_" + c).toUpperCase();
    const raw = process.env[envName];
    if (raw === undefined) continue;
    const defaultValue = DEFAULTS[key];
    if (typeof defaultValue === "boolean") {
      (overrides as any)[key] = raw.toLowerCase() === "1" || raw.toLowerCase() === "true";
    } else if (typeof defaultValue === "number") {
      (overrides as any)[key] = Number(raw);
    } else {
      (overrides as any)[key] = raw;
    }
  }
  return overrides;
}

export function validateConfig(config: Config): void {
  if (!Number.isInteger(config.treeMaxChildren) || config.treeMaxChildren < 2 || config.treeMaxChildren > 128) {
    throw new ConfigurationError("treeMaxChildren must be an integer between 2 and 128");
  }
  for (const key of ["targetChunkSizeScalars", "maxSelectedChunks", "maxEvidenceTextScalars",
    "providerAttemptLimitIndex", "providerAttemptLimitRetrieve", "providerAttemptLimitAsk", "maxProviderAttemptsPerOp"] as const) {
    if (!Number.isInteger(config[key]) || config[key] <= 0) throw new ConfigurationError(`${key} must be a positive integer`);
  }
  if (!Number.isInteger(config.maxTreeNavigationCalls) || config.maxTreeNavigationCalls < 0) {
    throw new ConfigurationError("maxTreeNavigationCalls must be a non-negative integer");
  }
  if (!["none", "sqlite", "redis"].includes(config.cacheBackend)) {
    throw new ConfigurationError(
      `cacheBackend must be one of 'none', 'sqlite', 'redis'; got ${config.cacheBackend}`,
    );
  }
  if (config.cacheBackend === "redis" && !config.applicationNamespace) {
    throw new ConfigurationError(
      "applicationNamespace is required when cacheBackend='redis' (PRD §7.4) — checked at " +
        "open(), before any store file is created",
    );
  }
  if (config.cacheBackend === "redis" && !config.redisUrl) {
    throw new ConfigurationError(
      "redisUrl is required when cacheBackend='redis' — checked at open(), before any store " +
        "file is created",
    );
  }
}

export function resolveConfig(overrides?: Partial<Config>): Config {
  const resolved: Config = { ...DEFAULTS, ...envOverrides(), ...(overrides ?? {}) };
  validateConfig(resolved);
  return resolved;
}
