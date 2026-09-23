/**
 * Model provider abstraction — TypeScript counterpart to provider.py's admission/timeout/retry
 * layer. FR-008 (cache modes) is Python-only for this release per the task's Milestone
 * Applicability Matrix (M3, Python-implementing-tasks only) — TypeScript parity (FR-011) does
 * not require porting the memoization backend, only the cache-key formula itself (cacheKey.ts,
 * T077). This is named `BoundedProvider`, not `MemoizedProvider`, so as not to overclaim a
 * caching behavior it does not implement.
 */

import { BudgetExceeded, ProviderTimeout } from "./errors.js";

export interface ProviderRequest {
  operation: string; // "synthesis" | "indexing" | "tree_navigation"
  prompt: string;
  evidenceContext: string;
}

export interface ProviderResponse {
  text: string;
  inputTokens?: number | null;
  outputTokens?: number | null;
  usageUnknown?: boolean;
}

export interface Provider {
  complete(request: ProviderRequest): Promise<ProviderResponse>;
}

/** Shared physical attempt budget for every step in one operation. */
export class CallBudget {
  calls = 0;
  retries = 0;
  inputTokens = 0;
  outputTokens = 0;
  usageUnknown = false;
  constructor(readonly limit: number) {}
  admit(retry: boolean): void {
    if (this.calls >= this.limit) throw new BudgetExceeded("request provider-attempt budget exhausted");
    this.calls++;
    if (retry) this.retries++;
  }
  record(response: ProviderResponse): void {
    if (response.inputTokens == null || response.outputTokens == null || response.usageUnknown) {
      this.usageUnknown = true;
    } else {
      this.inputTokens += response.inputTokens;
      this.outputTokens += response.outputTokens;
    }
  }
}

export class FakeProvider implements Provider {
  callCount = 0;
  private responses: ProviderResponse[];
  private failTimes: number;
  private failError: Error | null;

  constructor(opts: { responses?: ProviderResponse[]; failTimes?: number; failError?: Error } = {}) {
    this.responses = [...(opts.responses ?? [])];
    this.failTimes = opts.failTimes ?? 0;
    this.failError = opts.failError ?? null;
  }

  async complete(_request: ProviderRequest): Promise<ProviderResponse> {
    this.callCount++;
    if (this.callCount <= this.failTimes) {
      throw this.failError ?? new ProviderTimeout("simulated provider failure");
    }
    if (this.responses.length > 0) return this.responses.shift()!;
    return { text: "" };
  }
}

export interface BoundedProviderConfig {
  perProviderConcurrency: number;
  providerCallDeadlineS: number;
  maxProviderAttemptsPerOp: number;
}

/** Admission (bounded concurrency) + per-call deadline + retry, wrapping one `Provider`. */
export class BoundedProvider {
  private inner: Provider;
  private config: BoundedProviderConfig;
  private inFlight = 0;
  private waiters: (() => void)[] = [];

  constructor(inner: Provider, config: BoundedProviderConfig) {
    this.inner = inner;
    this.config = config;
  }

  private async acquire(deadlineAtMs: number): Promise<void> {
    if (Date.now() >= deadlineAtMs) throw new ProviderTimeout("provider admission exceeded request deadline");
    if (this.inFlight < this.config.perProviderConcurrency) {
      this.inFlight++;
      return;
    }
    await new Promise<void>((resolve, reject) => {
      const waiter = () => { clearTimeout(timer); resolve(); };
      const timer = setTimeout(() => {
        const position = this.waiters.indexOf(waiter);
        if (position !== -1) this.waiters.splice(position, 1);
        reject(new ProviderTimeout("provider admission exceeded request deadline"));
      }, Math.max(0, deadlineAtMs - Date.now()));
      this.waiters.push(waiter);
    });
  }

  private release(): void {
    const next = this.waiters.shift();
    if (next) next(); // Transfer the existing slot to this waiter.
    else this.inFlight--;
  }

  async complete(request: ProviderRequest, deadlineAtMs: number, budget?: CallBudget): Promise<ProviderResponse> {
    await this.acquire(deadlineAtMs);
    try {
      let attempt = 0;
      let lastError: Error | null = null;
      while (true) {
        attempt++;
        if (Date.now() >= deadlineAtMs) {
          throw lastError ?? new ProviderTimeout("deadline already passed before first attempt");
        }
        const remainingMs = deadlineAtMs - Date.now();
        const callTimeoutMs = Math.min(this.config.providerCallDeadlineS * 1000, Math.max(remainingMs, 0));
        try {
          budget?.admit(attempt > 1);
          const response = await withTimeout(this.inner.complete(request), callTimeoutMs, request.operation);
          budget?.record(response);
          return response;
        } catch (err) {
          if (budget && !(err instanceof BudgetExceeded)) budget.usageUnknown = true;
          if (!(err instanceof ProviderTimeout) || attempt >= this.config.maxProviderAttemptsPerOp) throw err;
          lastError = err;
          const backoffMs = Math.min(2000, 50 * 2 ** (attempt - 1)) * (0.5 + Math.random() * 0.5);
          await new Promise((r) => setTimeout(r, Math.min(backoffMs, deadlineAtMs - Date.now())));
        }
      }
    } finally {
      this.release();
    }
  }
}

function withTimeout<T>(promise: Promise<T>, ms: number, operation: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new ProviderTimeout(`provider call for operation ${operation} exceeded its deadline`)),
      ms,
    );
    promise.then(
      (v) => {
        clearTimeout(timer);
        resolve(v);
      },
      (e) => {
        clearTimeout(timer);
        reject(e);
      },
    );
  });
}
