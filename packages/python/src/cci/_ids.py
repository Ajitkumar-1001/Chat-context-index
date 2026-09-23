"""Minimal ULID generation (spec/storage-format.md ID formats: t_/m_/c_/n_<ULID>).

No new dependency — ULIDs are a small, well-defined format (Crockford base32 over a
48-bit millisecond timestamp + 80 bits of randomness); implementing the ~20 lines directly
is smaller than adding a package for it.
"""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_CROCKFORD[rem])
    return "".join(reversed(chars))


def new_ulid() -> str:
    """26-character Crockford-base32 ULID: 10 chars timestamp (ms) + 16 chars randomness."""
    ts_ms = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(ts_ms, 10) + _encode(randomness, 16)


def prefixed_id(prefix: str) -> str:
    """e.g. prefixed_id('t') -> 't_01H...'"""
    return f"{prefix}_{new_ulid()}"
