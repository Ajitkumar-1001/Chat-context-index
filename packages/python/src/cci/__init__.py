"""cci (chat-context-index): persistent, retrievable conversation memory for AI applications.

Public re-exports of the operations named in contracts/operations.md. `HistoryStore` is this
implementation's name for the contract's `ContextIndex` — `HistoryStore.open()` is the
`ContextIndex.open()` entry point (no behavioral divergence, naming only).
"""

from __future__ import annotations

from .ask import ask
from .clear import clear_history
from .export import export
from .import_history import import_history
from .index import index
from .ingest import ingest
from .retrieve import retrieve
from .search import search
from .stats import stats
from .store import HistoryStore

__all__ = [
    "HistoryStore",
    "ask",
    "clear_history",
    "export",
    "import_history",
    "index",
    "ingest",
    "retrieve",
    "search",
    "stats",
]
