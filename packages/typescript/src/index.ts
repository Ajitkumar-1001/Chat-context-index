/**
 * Public entry point (contracts/operations.md) — re-exports the operations named there.
 * `HistoryStore` is this implementation's name for the contract's `ContextIndex` (naming only,
 * matching the Python package's own __init__.py note).
 */

export { HistoryStore } from "./store.js";
export { prepareContext, packContext } from "./memory.js";
export type { Context, ContextItem, ContextOptions } from "./memory.js";
export { BoundedProvider } from "./provider.js";
export type { Provider, ProviderRequest, ProviderResponse } from "./provider.js";
export type { InputMessage } from "./models.js";
export type { NodeView, SearchMaintenanceOptions } from "./store.js";
export { ingest } from "./ingest.js";
export { search } from "./search.js";
export type { SearchResult, LexicalCandidate, Diagnostic } from "./search.js";
export { retrieve } from "./retrieve.js";
export type { RetrievalResult, Snapshot, Evidence } from "./retrieve.js";
export { ask } from "./ask.js";
export type { AnswerResult } from "./ask.js";
export { indexOperation as index } from "./indexOperation.js";
export type { IndexReport } from "./indexOperation.js";
export { exportHistory } from "./export.js";
export type { ExportManifest } from "./export.js";
export { importHistory } from "./importHistory.js";
export type { ImportReport } from "./importHistory.js";
export { clearHistory } from "./clear.js";
export type { ClearReport } from "./clear.js";
export * as cciErrors from "./errors.js";
