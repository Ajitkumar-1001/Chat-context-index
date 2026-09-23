"""Compatibility imports for the original lexical-only development evaluation.

Applications should import cci.memory directly. The old example intentionally retains its
lexical-only policy so its three-strategy comparison stays reproducible.
"""

from cci.memory import Context, ContextItem, message_item, pack_context
from cci.memory import prepare_context as _prepare_context


async def prepare_context(store, query, **options):
    return await _prepare_context(store, query, mode="lexical", **options)
