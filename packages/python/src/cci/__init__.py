"""cci (chat-context-index): persistent, retrievable conversation memory for AI applications.

Deliberately does not re-export the operation functions (`ask`, `ingest`, `index`, `retrieve`,
`search`, `stats`, `export`) at package level: each shares its name with its own submodule
(`cci.ask` module vs. its `ask` function, etc.), and `from .ask import ask` here would rebind
the `cci.ask` attribute to the function, shadowing the submodule — breaking any caller that does
`import cci.retrieve as x` (this project's failure-injection tests monkeypatch module-level
barriers exactly that way). Import operations from their own submodule instead:
`from cci.retrieve import retrieve`.

`HistoryStore` has no such collision (the contract's `ContextIndex` — `HistoryStore.open()` is
`ContextIndex.open()`, naming only) and is safe to re-export here.
"""

from __future__ import annotations

from .store import HistoryStore

__all__ = ["HistoryStore"]
