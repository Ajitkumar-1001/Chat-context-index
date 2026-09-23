"""Python CLI (contracts/operations.md; FR-012): `init`, `ingest`, `index`, `search`,
`retrieve`, `ask`, `tree`, `stats`, `export`, `import`, `clear` — a thin, non-interactive
wrapper over the library API. `init` is a convenience first-run wrapper around `open()`'s
creation path, not a second store-creation mechanism.

Destructive commands (`clear`, `import`) require an explicit `--confirm` flag — never an
interactive prompt (T068). `clear` additionally requires `--history-id` (not auto-filled from
the just-opened store): naming it explicitly is the safety check that catches a stale operator
assumption about which store `--store` currently points at.

`--provider openai` is a minimal, thin adapter over the `openai` SDK (an optional extra, lazily
imported here only) for `index`/`ask` — its live network behavior is not exercised by this
project's automated test suite (no API key in that environment); without `--provider`, `index`/
`ask` correctly raise `ConfigurationError`, matching the library's own documented behavior for
an unconfigured model-requiring operation.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from typing import Any

from .ask import ask
from .clear import clear_history
from .errors import CciError
from .export import export
from .import_history import import_history
from .index import index
from .ingest import ingest
from .io_worker import fetchall
from .models import InputMessage
from .provider import MemoizedProvider, ProviderRequest, ProviderResponse
from .retrieve import retrieve
from .search import search
from .stats import stats
from .store import HistoryStore


def _to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


def _print_json(obj: Any) -> None:
    print(json.dumps(_to_jsonable(obj), indent=2, sort_keys=True))


class _OpenAIProvider:
    """Minimal OpenAI-compatible adapter (PRD §6.2's "provider SDKs behind extras"). Lazily
    imports `openai` only when `--provider openai` is actually selected (PRD §13.2)."""

    def __init__(self, model: str) -> None:
        import openai  # lazy import — never loaded merely by importing this module

        self._client = openai.AsyncOpenAI()
        self._model = model

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": request.prompt},
                {"role": "user", "content": request.evidence_context},
            ],
        )
        choice = response.choices[0]
        usage = response.usage
        return ProviderResponse(
            text=choice.message.content or "",
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            usage_unknown=usage is None,
        )


def _build_provider(store: HistoryStore, args: argparse.Namespace) -> MemoizedProvider | None:
    if args.provider is None:
        return None
    if args.provider == "openai":
        inner = _OpenAIProvider(model=args.model or "gpt-4o-mini")
    else:
        raise SystemExit(f"unknown --provider {args.provider!r}")
    return MemoizedProvider(
        inner=inner, config=store.config, cache=store.cache, usage_log=store.usage_log,
    )


def _config_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides = {
        "cache_backend": args.cache_backend,
        "application_namespace": args.application_namespace,
        "redis_url": args.redis_url,
    }
    return {k: v for k, v in overrides.items() if v is not None}


def _read_messages(path: str) -> list[InputMessage]:
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    records = json.loads(raw)
    if not isinstance(records, list):
        raise SystemExit("messages file must contain a JSON array of message objects")
    return [
        InputMessage(
            role=r["role"],
            content=r.get("content"),
            external_id=r.get("external_id"),
            metadata=r.get("metadata"),
            tool_calls=r.get("tool_calls"),
            tool_call_id=r.get("tool_call_id"),
        )
        for r in records
    ]


async def _list_root_nodes(store: HistoryStore) -> list[dict]:
    async with store.write_lock:
        async with store.connection:
            cursor = await store.connection.execute(
                "SELECT node_id, title, summary, message_range FROM nodes "
                "WHERE history_id = ? AND parent_id IS NULL ORDER BY sibling_order",
                (store.history_id,),
            )
            rows = await fetchall(cursor)
    return [
        {"node_id": r[0], "title": r[1], "summary": r[2], "message_range": r[3]} for r in rows
    ]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cci", description="cci: conversation memory CLI")
    parser.add_argument("--store", required=True, help="path to the history store file")
    parser.add_argument("--config-file", default=None, help="TOML config file path")
    parser.add_argument("--cache-backend", default=None, choices=["none", "sqlite", "redis"])
    parser.add_argument("--application-namespace", default=None)
    parser.add_argument("--redis-url", default=None)
    parser.add_argument("--provider", default=None, choices=["openai"], help="for index/ask")
    parser.add_argument("--model", default=None, help="model name, with --provider")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")

    p_ingest = sub.add_parser("ingest")
    p_ingest.add_argument("--messages-file", required=True, help="JSON array file, or - for stdin")
    p_ingest.add_argument("--source-id", required=True)
    p_ingest.add_argument("--idempotency-key", required=True)

    p_index = sub.add_parser("index")
    p_index.add_argument("--rebuild", action="store_true")

    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)

    p_retrieve = sub.add_parser("retrieve")
    p_retrieve.add_argument("query")
    p_retrieve.add_argument("--mode", default="auto", choices=["lexical", "tree", "auto"])

    p_ask = sub.add_parser("ask")
    p_ask.add_argument("query")
    p_ask.add_argument("--mode", default="auto", choices=["lexical", "tree", "auto"])

    p_tree = sub.add_parser("tree")
    p_tree.add_argument("node_id", nargs="?", default=None)

    sub.add_parser("stats")

    p_export = sub.add_parser("export")
    p_export.add_argument("--dest", required=True)

    p_import = sub.add_parser("import")
    p_import.add_argument("--src", required=True)
    p_import.add_argument("--confirm", action="store_true", help="required — no interactive prompt")

    p_clear = sub.add_parser("clear")
    p_clear.add_argument("--history-id", required=True)
    p_clear.add_argument("--confirm", action="store_true", help="required — no interactive prompt")

    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "import":
        if not args.confirm:
            print("error: `import` is destructive — pass --confirm", file=sys.stderr)
            return 2
        store = await HistoryStore.open(
            args.store, config=_config_overrides(args) or None, config_file=args.config_file,
        )
        try:
            report = await import_history(store, args.src)
            _print_json(report)
        finally:
            await store.aclose()
        return 0

    store = await HistoryStore.open(
        args.store, config=_config_overrides(args) or None, config_file=args.config_file,
    )
    try:
        if args.command == "init":
            print(json.dumps({"history_id": store.history_id}))
        elif args.command == "ingest":
            messages = _read_messages(args.messages_file)
            receipt = await ingest(
                store, store.history_id, messages, args.source_id, args.idempotency_key,
            )
            _print_json(receipt)
        elif args.command == "index":
            provider = _build_provider(store, args)
            index_report = await index(store, provider=provider, rebuild=args.rebuild)
            _print_json(index_report)
        elif args.command == "search":
            search_result = await search(store, args.query, limit=args.limit)
            _print_json(search_result)
        elif args.command == "retrieve":
            retrieval_result = await retrieve(store, args.query, mode=args.mode)
            _print_json(retrieval_result)
        elif args.command == "ask":
            provider = _build_provider(store, args)
            answer_result = await ask(store, args.query, provider=provider, mode=args.mode)
            _print_json(answer_result)
        elif args.command == "tree":
            if args.node_id is None:
                _print_json(await _list_root_nodes(store))
            else:
                view = await store.view_node(args.node_id)
                _print_json(view) if view is not None else print("null")
        elif args.command == "stats":
            _print_json(await stats(store))
        elif args.command == "export":
            manifest = await export(store, args.dest)
            _print_json(manifest)
        elif args.command == "clear":
            if not args.confirm:
                print("error: `clear` is destructive — pass --confirm", file=sys.stderr)
                return 2
            clear_report = await clear_history(store, args.history_id)
            _print_json(clear_report)
        else:
            print(f"unknown command {args.command!r}", file=sys.stderr)
            return 2
    finally:
        await store.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except CciError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
