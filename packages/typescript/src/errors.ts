/**
 * Typed error codes (spec/schemas/error-codes.json; contracts/result-schemas.md).
 * Fixed set of 15 codes, identical to the Python implementation (errors.py) — same codes, same
 * conditions, same retryable classification, in both languages (contracts/operations.md
 * Python/TypeScript Behavior Mapping "Typed errors").
 */

export class CciError extends Error {
  readonly retryable: boolean = false;
  constructor(message: string) {
    super(message);
    this.name = new.target.name;
  }
}

export class ConfigurationError extends CciError {}
export class InputValidationError extends CciError {}
export class IdempotencyConflict extends CciError {}
export class MessageConflict extends CciError {}
export class StoreBusy extends CciError {
  readonly retryable = true;
}
export class StoreNotEmpty extends CciError {}
export class StoreError extends CciError {}
export class StoreCorrupt extends CciError {}
export class SchemaVersionError extends CciError {}
export class RuntimeCompatibilityError extends CciError {}
export class VersionConflict extends CciError {}
export class ProviderError extends CciError {}
export class ProviderTimeout extends CciError {
  readonly retryable = true;
}
export class BudgetExceeded extends CciError {}
export class Cancelled extends CciError {}
