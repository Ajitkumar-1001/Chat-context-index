"""Configuration loading and precedence (contracts/operations.md `open()`; PRD §7.4).

Precedence: explicit constructor value -> selected config file -> named environment
binding -> documented default. No scanning of arbitrary working directories for secrets —
a config file is only read from an explicitly supplied path.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from .errors import ConfigurationError

# Proposed, code-enforced defaults (contracts/result-schemas.md Config defaults) — configurable,
# not immutable constants (spec.md Assumptions).
DEFAULTS: dict[str, Any] = {
    "cache_backend": "sqlite",
    "redis_fallback": "sqlite",
    "memo_lifetime_s": 86_400,
    "cache_operation_timeout_ms": 100,
    "cache_overhead_budget_ms": 250,
    "redis_circuit_breaker_failures": 3,
    "redis_circuit_breaker_probe_s": 30,
    "sqlite_busy_timeout_ms": 5_000,
    "tree_max_children": 10,
    "target_chunk_size_scalars": 6_000,
    "max_selected_chunks": 8,
    "max_evidence_text_scalars": 24_000,
    "max_tree_navigation_calls": 4,
    "per_provider_concurrency": 4,
    "max_provider_attempts_per_op": 3,
    "provider_attempt_limit_retrieve": 8,
    "provider_attempt_limit_ask": 10,
    "provider_attempt_limit_index": 64,
    "request_deadline_retrieve_s": 60,
    "request_deadline_ask_s": 90,
    "request_deadline_index_s": 300,
    # data-model.md Snapshot/Generation lifecycle: anchored at 2x sqlite_busy_timeout_ms.
    "clear_history_quiescence_deadline_s": 10,
    "provider_call_deadline_s": 30,
    "payload_logging": False,
    "application_namespace": None,
    "redis_url": None,
}

_ENV_PREFIX = "CCI_"


@dataclass(frozen=True)
class Config:
    """Resolved configuration for one `open()` call. Immutable once resolved."""

    cache_backend: str = DEFAULTS["cache_backend"]
    redis_fallback: str = DEFAULTS["redis_fallback"]
    application_namespace: str | None = DEFAULTS["application_namespace"]
    memo_lifetime_s: int = DEFAULTS["memo_lifetime_s"]
    cache_operation_timeout_ms: int = DEFAULTS["cache_operation_timeout_ms"]
    cache_overhead_budget_ms: int = DEFAULTS["cache_overhead_budget_ms"]
    redis_circuit_breaker_failures: int = DEFAULTS["redis_circuit_breaker_failures"]
    redis_circuit_breaker_probe_s: int = DEFAULTS["redis_circuit_breaker_probe_s"]
    sqlite_busy_timeout_ms: int = DEFAULTS["sqlite_busy_timeout_ms"]
    tree_max_children: int = DEFAULTS["tree_max_children"]
    target_chunk_size_scalars: int = DEFAULTS["target_chunk_size_scalars"]
    max_selected_chunks: int = DEFAULTS["max_selected_chunks"]
    max_evidence_text_scalars: int = DEFAULTS["max_evidence_text_scalars"]
    max_tree_navigation_calls: int = DEFAULTS["max_tree_navigation_calls"]
    per_provider_concurrency: int = DEFAULTS["per_provider_concurrency"]
    max_provider_attempts_per_op: int = DEFAULTS["max_provider_attempts_per_op"]
    provider_attempt_limit_retrieve: int = DEFAULTS["provider_attempt_limit_retrieve"]
    provider_attempt_limit_ask: int = DEFAULTS["provider_attempt_limit_ask"]
    provider_attempt_limit_index: int = DEFAULTS["provider_attempt_limit_index"]
    request_deadline_retrieve_s: int = DEFAULTS["request_deadline_retrieve_s"]
    request_deadline_ask_s: int = DEFAULTS["request_deadline_ask_s"]
    request_deadline_index_s: int = DEFAULTS["request_deadline_index_s"]
    clear_history_quiescence_deadline_s: int = DEFAULTS["clear_history_quiescence_deadline_s"]
    provider_call_deadline_s: int = DEFAULTS["provider_call_deadline_s"]
    payload_logging: bool = DEFAULTS["payload_logging"]
    redis_url: str | None = DEFAULTS["redis_url"]

    def validate(self) -> None:
        """Raise ConfigurationError for anything invalid. Called by open() BEFORE any store
        file is created or opened (contracts/operations.md `open()` Check order)."""
        if not isinstance(self.tree_max_children, int) or not 2 <= self.tree_max_children <= 128:
            raise ConfigurationError("tree_max_children must be an integer between 2 and 128")
        for name in ("target_chunk_size_scalars", "max_selected_chunks", "max_evidence_text_scalars",
                     "provider_attempt_limit_index", "provider_attempt_limit_retrieve",
                     "provider_attempt_limit_ask", "max_provider_attempts_per_op"):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise ConfigurationError(f"{name} must be a positive integer")
        if not isinstance(self.max_tree_navigation_calls, int) or self.max_tree_navigation_calls < 0:
            raise ConfigurationError("max_tree_navigation_calls must be a non-negative integer")
        if self.cache_backend not in ("none", "sqlite", "redis"):
            raise ConfigurationError(
                f"cache_backend must be one of 'none', 'sqlite', 'redis'; got {self.cache_backend!r}"
            )
        if self.cache_backend == "redis" and not self.application_namespace:
            raise ConfigurationError(
                "application_namespace is required when cache_backend='redis' "
                "(PRD §7.4) — checked at open(), before any store file is created"
            )
        if self.cache_backend == "redis" and not self.redis_url:
            raise ConfigurationError(
                "redis_url is required when cache_backend='redis' — checked at open(), before "
                "any store file is created"
            )


def _load_config_file(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise ConfigurationError(f"config file not found: {p}")
    with p.open("rb") as f:
        return tomllib.load(f)


def _env_overrides() -> dict[str, Any]:
    """Named environment bindings, e.g. CCI_CACHE_BACKEND, CCI_APPLICATION_NAMESPACE."""
    overrides: dict[str, Any] = {}
    for f in fields(Config):
        env_name = _ENV_PREFIX + f.name.upper()
        if env_name in os.environ:
            raw = os.environ[env_name]
            if f.type is bool or f.type == "bool":
                overrides[f.name] = raw.lower() in ("1", "true", "yes")
            elif f.type is int or f.type == "int":
                overrides[f.name] = int(raw)
            else:
                overrides[f.name] = raw
    return overrides


def resolve(
    overrides: dict[str, Any] | None = None,
    config_file: str | Path | None = None,
) -> Config:
    """Resolve a Config per PRD §7.4 precedence: explicit constructor value (`overrides`) ->
    selected config file -> named environment binding -> documented default."""
    merged: dict[str, Any] = dict(DEFAULTS)
    merged.pop("application_namespace")  # already a field default; avoid dup kwarg below
    resolved_kwargs: dict[str, Any] = {}

    file_values = _load_config_file(config_file) if config_file else {}
    env_values = _env_overrides()
    explicit = overrides or {}

    for f in fields(Config):
        if f.name in explicit:
            resolved_kwargs[f.name] = explicit[f.name]
        elif f.name in file_values:
            resolved_kwargs[f.name] = file_values[f.name]
        elif f.name in env_values:
            resolved_kwargs[f.name] = env_values[f.name]
        # else: dataclass field default (DEFAULTS) applies automatically.

    cfg = Config(**resolved_kwargs)
    cfg.validate()
    return cfg
