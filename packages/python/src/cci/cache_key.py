"""Shared cache-key derivation (spec/cache-format.md; PRD §8.1).

Implements JSON Canonicalization Scheme (RFC 8785) closely enough for this library's own
inputs (operation descriptors, message content, IDs — all strings/ints/bools/nulls/nested
objects/arrays; precision-sensitive values are represented as strings per spec/cache-format.md,
which sidesteps JCS's more intricate ECMAScript float-formatting rules). No new dependency —
canonicalization is: recursively sort object keys, compact separators, UTF-8 output, reject
non-finite numbers and invalid input before hashing.

INV-08: this formula has no language-specific step. The TypeScript implementation (M4) MUST
produce byte-identical canonical output for the same input — verified by T012's cache-key
mutation-pairs fixture once both languages exist (contracts/operations.md T077).
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .errors import InputValidationError

_CACHE_KEY_PREFIX = "cci:memo:v2:"


def _reject_non_finite(obj: Any) -> None:
    """Recursively reject NaN/Infinity (spec/cache-format.md: reject before memo lookup/write)."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise InputValidationError(f"non-finite number in cache-key input: {obj!r}")
    elif isinstance(obj, dict):
        for v in obj.values():
            _reject_non_finite(v)
    elif isinstance(obj, list):
        for v in obj:
            _reject_non_finite(v)


def canonicalize(obj: Any) -> bytes:
    """JCS-style canonical serialization: recursively sorted object keys, no whitespace,
    UTF-8 encoded. Raises InputValidationError on non-finite numbers."""
    _reject_non_finite(obj)
    # sort_keys=True sorts by Python's native string comparison (Unicode code point order).
    # This matches RFC 8785 §3.2.3 for the BMP; astral-plane key names (very unlikely for
    # this library's own field names) are the one documented gap in strict RFC fidelity.
    canonical_text = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,  # belt-and-suspenders on top of _reject_non_finite
    )
    return canonical_text.encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scope_digest(
    application_namespace: str,
    store_instance_id: str,
    history_id: str,
    cache_generation: int,
) -> str:
    """SHA256(JCS({application_namespace, store_instance_id, history_id, cache_generation}))."""
    payload = {
        "application_namespace": application_namespace,
        "store_instance_id": store_instance_id,
        "history_id": history_id,
        "cache_generation": cache_generation,
    }
    return _sha256_hex(canonicalize(payload))


def request_digest(effective_request: dict[str, Any]) -> str:
    """SHA256(JCS(effective_request))."""
    return _sha256_hex(canonicalize(effective_request))


def cache_key(scope_digest_hex: str, request_digest_hex: str) -> str:
    """'cci:memo:v2:' + scope_digest + ':' + request_digest"""
    return f"{_CACHE_KEY_PREFIX}{scope_digest_hex}:{request_digest_hex}"
