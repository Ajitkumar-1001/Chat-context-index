"""Typed error codes (spec/schemas/error-codes.json; contracts/result-schemas.md).

Fixed set of 15 codes. Each carries a `retryable` classification so a caller (or this
library's own retry policy in retry.py) does not have to re-derive it from prose.
"""

from __future__ import annotations


class CciError(Exception):
    """Base class for every error this library raises. Never raised directly."""

    retryable: bool = False


class ConfigurationError(CciError):
    """No suitable provider configured for a model-requiring operation, or invalid config
    (e.g. a missing application_namespace under Redis mode). Fails early, before partial work."""

    retryable = False


class InputValidationError(CciError):
    """Invalid/oversize ingest batch; import manifest/record-count mismatch. No partial commit."""

    retryable = False


class IdempotencyConflict(CciError):
    """Same ingest key, different request hash. Raised before any write."""

    retryable = False


class MessageConflict(CciError):
    """Existing external_id, changed content. Raised before any write."""

    retryable = False


class StoreBusy(CciError):
    """Storage-level contention (e.g. SQLite busy timeout). Transient — retryable."""

    retryable = True


class StoreNotEmpty(CciError):
    """import_history() into a non-empty target. No silent merge."""

    retryable = False


class StoreError(CciError):
    """General storage error. Never a fake empty/successful history."""

    retryable = False


class StoreCorrupt(CciError):
    """Detected storage corruption. Never disguised as empty. Requires recovery, not retry."""

    retryable = False


class SchemaVersionError(CciError):
    """Newer-unsupported schema at open(). No file modification."""

    retryable = False


class RuntimeCompatibilityError(CciError):
    """Loaded SQLite runtime fails the required version/feature check. Raised at open()."""

    retryable = False


class VersionConflict(CciError):
    """Stale generation at tree-publish commit, or at read emission when a concurrent
    clear_history invalidated an in-flight retrieve()/ask(). Bounded-retried internally only
    at tree-publish; raised directly (not retried) at read emission."""

    retryable = False


class ProviderError(CciError):
    """Provider failure after retries already exhausted. Not retried again by this library."""

    retryable = False


class ProviderTimeout(CciError):
    """Provider call exceeded its deadline. Transient — retryable within the provider-attempt
    budget."""

    retryable = True


class BudgetExceeded(CciError):
    """Budget exhausted before useful work; clear_history quiescence deadline exceeded.
    Retrying will not recover an exhausted budget."""

    retryable = False


class Cancelled(CciError):
    """Caller-initiated cancellation. A new call is the caller's choice, not a retry."""

    retryable = False


ALL_CODES: tuple[type[CciError], ...] = (
    ConfigurationError,
    InputValidationError,
    IdempotencyConflict,
    MessageConflict,
    StoreBusy,
    StoreNotEmpty,
    StoreError,
    StoreCorrupt,
    SchemaVersionError,
    RuntimeCompatibilityError,
    VersionConflict,
    ProviderError,
    ProviderTimeout,
    BudgetExceeded,
    Cancelled,
)
